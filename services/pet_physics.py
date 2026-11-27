"""桌宠纯物理与平滑运动曲线引擎（无 Qt 窗口依赖，便于独立测试）。

参考 dsh-pet 的过阻尼弹簧、Q 弹挤压曲线与子步抛掷积分，以及 bailong 的
三次缓动贴边过渡，为渲染与窗口移动提供统一、帧率无关的运动学数值。
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import math
import random
from typing import Optional

from models.pet_settings import DockSide
from models.pet_state import PetState


# 过阻尼弹簧参数：ζ = c / (2 * sqrt(k)) ≈ 1.06，无过冲回弹
DRAG_SPRING_K = 220.0
DRAG_SPRING_C = 31.5

# 拖拽轨迹速度估算窗口参数（提高抛掷死区，避免普通挪动误甩出）
TRAIL_KEEP_SEC = 0.20
RELEASE_WINDOW_SEC = 0.15
RELEASE_STALE_SEC = 0.14
MIN_SPAN_SEC = 0.024
SEG_MIN_DT_SEC = 0.010
THROW_DEAD_ZONE_SPEED = 680.0
MAX_THROW_SPEED = 1550.0
PEAK_SPEED_WEIGHT = 0.35

# 重力、反弹与地面摩擦参数（温和自然的落速与轻弹）
DEFAULT_GRAVITY = 1350.0
WALL_RESTITUTION = 0.48
FLOOR_RESTITUTION = 0.28
FLOOR_BOUNCE_MIN_VY = 620.0
GROUND_FRICTION = 5.6
REST_TANGENT_SPEED = 20.0
SUBSTEP_MAX_PX = 14.0
SUBSTEP_LIMIT = 16

# Q 弹挤压参数（舒缓柔弹，避免抽搐感）
SQUASH_DURATION_SEC = 0.36
POKE_SQUASH_DURATION_SEC = 0.32
SQUASH_SOFT_SPEED = 320.0
SQUASH_HARD_SPEED = 1450.0
SQUASH_MIN_DEPTH = 0.08
SQUASH_MAX_DEPTH = 0.19
SQUASH_SPRING_OMEGA = 17.5
SQUASH_SPRING_ZETA = 0.46
# 欠阻尼弹簧单位初速的峰值偏移系数 exp(-ζ / sqrt(1 - ζ²) * arccos(ζ))，用于把深度精准映射为初速冲量
_SQUASH_PEAK_FACTOR = math.exp(
    -SQUASH_SPRING_ZETA / math.sqrt(1.0 - SQUASH_SPRING_ZETA * SQUASH_SPRING_ZETA) * math.acos(SQUASH_SPRING_ZETA)
)

# 贴边缓动时长
DOCK_SLIDE_DURATION_SEC = 0.34


def clamp(value: float, low: float, high: float) -> float:
    return low if value < low else high if value > high else value


def damp_exp(current: float, target: float, speed: float, dt: float) -> float:
    """帧率无关的指数阻尼逼近，用于在状态切换时平滑过渡变换参数。"""
    if dt <= 0.0 or speed <= 0.0:
        return current
    factor = 1.0 - math.exp(-speed * dt)
    return current + (target - current) * factor


def ease_out_cubic(progress: float) -> float:
    u = clamp(progress, 0.0, 1.0)
    return 1.0 - (1.0 - u) ** 3


def smoothstep(progress: float) -> float:
    u = clamp(progress, 0.0, 1.0)
    return u * u * (3.0 - 2.0 * u)


def soft_clamp_speed(speed: float, max_speed: float = MAX_THROW_SPEED) -> float:
    """渐近软限速：任意力度下保持单调可区分，但平滑收敛至上限。"""
    if speed <= 0.0 or max_speed <= 0.0:
        return 0.0
    return max_speed * (1.0 - math.exp(-speed / max_speed))


def landing_squash_depth(impact_vy: float) -> float:
    """将竖直落地冲击速度（px/s）映射为下压深度（0.11~0.28）。"""
    t = clamp(
        (abs(impact_vy) - SQUASH_SOFT_SPEED) / (SQUASH_HARD_SPEED - SQUASH_SOFT_SPEED),
        0.0,
        1.0,
    )
    return SQUASH_MIN_DEPTH + t * (SQUASH_MAX_DEPTH - SQUASH_MIN_DEPTH)


def squash_scale_xy(progress: float, depth: float = 0.20) -> tuple[float, float]:
    """全程 C1 连续的三段余弦 Q 弹挤压曲线（锚定脚底）：吸能下压 → 柔弹过冲 → 稳态归位。

    在谷底（compress_split）与过冲峰值（overshoot_peak）处一阶导数均为 0，
    彻底消除旧版 ease-in 接 easeOutBack 在回弹拐点处的速度断层与跳帧感。
    """
    u = clamp(progress, 0.0, 1.0)
    d = clamp(depth, 0.0, 0.42)
    if d <= 1e-4 or u <= 0.0 or u >= 1.0:
        return (1.0, 1.0)

    compress_split = 0.38
    overshoot_peak = 0.74
    overshoot_amp = d * 0.24

    if u < compress_split:
        p = u / compress_split
        # 触地瞬间初速最大、向谷底平滑减速至 0，自然吸收冲击动量
        sy = 1.0 - d * math.sin(p * (math.pi * 0.5))
    elif u < overshoot_peak:
        p = (u - compress_split) / (overshoot_peak - compress_split)
        ease = 0.5 - 0.5 * math.cos(math.pi * p)
        sy = (1.0 - d) + (d + overshoot_amp) * ease
    else:
        p = (u - overshoot_peak) / (1.0 - overshoot_peak)
        ease = 0.5 - 0.5 * math.cos(math.pi * p)
        sy = (1.0 + overshoot_amp) - overshoot_amp * ease

    # 近似面积守恒：纵向压扁时横向舒展，纵向过冲拉长时横向略收窄
    sx = clamp(1.0 + (1.0 - sy) * 0.72, 0.82, 1.28)
    return (sx, sy)


class SquashSpring:
    """二阶欠阻尼果冻缩放弹簧：连续吸收落地、弹跳、撞墙与戳击冲量，杜绝二次触地硬重置闪跳。"""

    def __init__(
        self,
        omega: float = SQUASH_SPRING_OMEGA,
        zeta: float = SQUASH_SPRING_ZETA,
    ) -> None:
        self.omega = float(omega)
        self.zeta = float(zeta)
        self.offset = 0.0  # sy - 1.0（负值为纵向压扁，正值为纵向拉长）
        self.vel = 0.0

    @property
    def is_active(self) -> bool:
        return abs(self.offset) > 0.0008 or abs(self.vel) > 0.008

    def reset(self) -> None:
        self.offset = 0.0
        self.vel = 0.0

    def trigger_squash(self, depth: float) -> None:
        """注入向下压扁速度冲量；若当前仍在形变中，在现有状态上平滑叠加而不硬重置。"""
        d = clamp(depth, 0.0, 0.30)
        if d <= 1e-4:
            return
        target_vel = -(d * self.omega) / _SQUASH_PEAK_FACTOR
        # 已有同向压缩速度时温和增强，避免二次触地叠出过大变形
        if self.vel < 0.0:
            self.vel = max(target_vel * 1.15, self.vel + target_vel * 0.65)
        else:
            self.vel = target_vel

    def trigger_stretch(self, amount: float) -> None:
        """注入轻微纵向拉伸/横向收窄冲量（用于撞侧墙或轻弹起跳瞬间，配合运动方向不打架）。"""
        a = clamp(amount, 0.0, 0.16)
        if a <= 1e-4:
            return
        target_vel = (a * self.omega) / _SQUASH_PEAK_FACTOR
        if self.vel > 0.0:
            self.vel = min(target_vel * 1.15, self.vel + target_vel * 0.6)
        else:
            self.vel = target_vel

    def step(self, dt: float) -> tuple[float, float]:
        """采用固定子步半隐式欧拉推进弹簧，返回面积近似守恒的 (scale_x, scale_y)。"""
        step_dt = min(max(dt, 0.0), 0.05)
        if step_dt > 0.0 and self.is_active:
            substeps = max(1, min(8, int(math.ceil(step_dt * 240.0))))
            sub_dt = step_dt / float(substeps)
            w2 = self.omega * self.omega
            damp = 2.0 * self.zeta * self.omega
            for _ in range(substeps):
                accel = -w2 * self.offset - damp * self.vel
                self.vel += accel * sub_dt
                self.offset += self.vel * sub_dt
                if self.offset < -0.26:
                    self.offset = -0.26
                    if self.vel < 0.0:
                        self.vel *= -0.25
                elif self.offset > 0.15:
                    self.offset = 0.15
                    if self.vel > 0.0:
                        self.vel *= -0.25
            if not self.is_active:
                self.offset = 0.0
                self.vel = 0.0

        sy = clamp(1.0 + self.offset, 0.74, 1.15)
        sx = clamp(1.0 + (1.0 - sy) * 0.72, 0.84, 1.24)
        return (sx, sy)


def spring_step_1d(
    vel: float,
    pos: float,
    target: float,
    dt: float,
    k: float = DRAG_SPRING_K,
    c: float = DRAG_SPRING_C,
) -> tuple[float, float]:
    """单轴过阻尼弹簧步进，返回更新后的 (pos, vel)。"""
    step_dt = min(max(dt, 0.0), 1.0 / 30.0)
    if step_dt <= 0.0:
        return (pos, vel)
    accel = (target - pos) * k - vel * c
    next_vel = vel + accel * step_dt
    next_pos = pos + next_vel * step_dt
    if abs(target - next_pos) < 0.35 and abs(next_vel) < 6.0:
        return (target, 0.0)
    return (next_pos, next_vel)


class DragTrailEstimator:
    """记录拖拽末段指针轨迹并估算松手抛掷初速，过滤高回报率鼠标微抖。"""

    def __init__(self) -> None:
        self._samples: deque[tuple[float, float, float]] = deque()

    def clear(self) -> None:
        self._samples.clear()

    def add_sample(self, x: float, y: float, now_mono: float) -> None:
        self._samples.append((x, y, now_mono))
        cutoff = now_mono - TRAIL_KEEP_SEC
        while len(self._samples) > 2 and self._samples[0][2] < cutoff:
            self._samples.popleft()

    def estimate_release_velocity(self, now_mono: float) -> tuple[float, float]:
        if len(self._samples) < 2:
            return (0.0, 0.0)

        last_x, last_y, last_t = self._samples[-1]
        # 松手前短暂停顿视为温柔放下，不带残余甩出速度
        if (now_mono - last_t) > RELEASE_STALE_SEC:
            return (0.0, 0.0)

        window_start = last_t - RELEASE_WINDOW_SEC
        window = [s for s in self._samples if s[2] >= window_start]
        if len(window) < 2:
            window = list(self._samples)[-2:]

        first_x, first_y, first_t = window[0]
        span = last_t - first_t
        if span < MIN_SPAN_SEC:
            return (0.0, 0.0)

        avg_vx = (last_x - first_x) / span
        avg_vy = (last_y - first_y) / span
        avg_speed = math.hypot(avg_vx, avg_vy)

        # 计算有效分段峰值速度，弥补快甩集中在末段被窗口均值拉低的问题
        peak_speed = avg_speed
        anchor_x, anchor_y, anchor_t = window[0]
        for sx, sy, st in window[1:]:
            seg_dt = st - anchor_t
            if seg_dt < SEG_MIN_DT_SEC:
                continue
            seg_spd = math.hypot(sx - anchor_x, sy - anchor_y) / seg_dt
            if seg_spd > peak_speed:
                peak_speed = seg_spd
            anchor_x, anchor_y, anchor_t = sx, sy, st

        raw_speed = avg_speed * (1.0 - PEAK_SPEED_WEIGHT) + peak_speed * PEAK_SPEED_WEIGHT
        if raw_speed < THROW_DEAD_ZONE_SPEED or avg_speed <= 1e-3:
            return (0.0, 0.0)

        clamped_speed = soft_clamp_speed(raw_speed, MAX_THROW_SPEED)
        scale = clamped_speed / avg_speed
        return (avg_vx * scale, avg_vy * scale)


@dataclass
class ThrowBodyState:
    x: float
    y: float
    vx: float
    vy: float


@dataclass(frozen=True)
class ThrowStepResult:
    x: float
    y: float
    vx: float
    vy: float
    landed_impact_vy: float
    bounced_wall: bool
    at_rest: bool
    bounced_floor: bool = False
    bounced_wall_side: int = 0  # -1=左壁, 1=右壁, 2=顶边, 0=未碰壁


def step_throw_physics(
    state: ThrowBodyState,
    dt: float,
    *,
    min_x: float,
    max_x: float,
    min_y: float,
    floor_y: float,
    gravity: float = DEFAULT_GRAVITY,
    wall_restitution: float = WALL_RESTITUTION,
    floor_restitution: float = FLOOR_RESTITUTION,
    ground_friction: float = GROUND_FRICTION,
) -> ThrowStepResult:
    """带子步积分的 2D 重力下落、侧壁反弹与落地摩擦步进。"""
    step_dt = min(max(dt, 0.001), 0.05)
    est_disp = max(abs(state.vx) * step_dt, abs(state.vy) * step_dt + 0.5 * gravity * step_dt * step_dt)
    substeps = max(1, min(SUBSTEP_LIMIT, int(math.ceil(est_disp / SUBSTEP_MAX_PX))))
    sub_dt = step_dt / float(substeps)

    x = state.x
    y = state.y
    vx = state.vx
    vy = state.vy
    landed_impact_vy = 0.0
    bounced_wall = False
    bounced_floor = False
    bounced_wall_side = 0

    for _ in range(substeps):
        was_grounded = y >= floor_y - 0.5 and abs(vy) < 1.0
        if not was_grounded:
            vy += gravity * sub_dt

        x += vx * sub_dt
        y += vy * sub_dt

        # 左右边界反弹
        if x < min_x:
            x = min_x
            if vx < 0.0:
                vx = -vx * wall_restitution
                bounced_wall = True
                bounced_wall_side = -1
        elif x > max_x:
            x = max_x
            if vx > 0.0:
                vx = -vx * wall_restitution
                bounced_wall = True
                bounced_wall_side = 1

        # 顶边碰撞
        if y < min_y:
            y = min_y
            if vy < 0.0:
                vy = -vy * wall_restitution
                bounced_wall = True
                if bounced_wall_side == 0:
                    bounced_wall_side = 2

        # 地面碰撞与触地衰减
        if y >= floor_y:
            y = floor_y
            if vy > 0.0:
                if not was_grounded:
                    landed_impact_vy = max(landed_impact_vy, vy)
                if vy >= FLOOR_BOUNCE_MIN_VY and abs(vx) > 90.0:
                    # 仅在明显抛掷且高冲击时做一次轻弹，普通竖直松手直接柔落
                    vy = -vy * floor_restitution
                    bounced_floor = True
                else:
                    vy = 0.0
            # 触地切向摩擦指数衰减
            if abs(vx) > 0.0:
                vx *= math.exp(-ground_friction * sub_dt)
                if abs(vx) < REST_TANGENT_SPEED:
                    vx = 0.0

    at_rest = y >= floor_y - 0.5 and abs(vy) < 1.0 and abs(vx) < REST_TANGENT_SPEED
    if at_rest:
        y = floor_y
        vx = 0.0
        vy = 0.0

    return ThrowStepResult(
        x=x,
        y=y,
        vx=vx,
        vy=vy,
        landed_impact_vy=landed_impact_vy,
        bounced_wall=bounced_wall,
        at_rest=at_rest,
        bounced_floor=bounced_floor,
        bounced_wall_side=bounced_wall_side,
    )


@dataclass
class TransformSnapshot:
    """渲染层使用的单帧完整 2D 变换快照（将水平朝向与缩放幅度分离）。"""

    facing_sign: float = 1.0
    scale_x: float = 1.0
    scale_y: float = 1.0
    angle: float = 0.0
    trans_x: float = 0.0
    trans_y: float = 0.0


class TransformDamper:
    """平滑阻尼状态容器：消除状态切换时的角度/位移/缩放跳变，并驱动步行与贴边缓动。"""

    def __init__(self) -> None:
        self.current = TransformSnapshot()
        self.initialized = False
        self.walk_blend = 0.0
        self.dock_progress = 1.0
        self._dock_elapsed = DOCK_SLIDE_DURATION_SEC

    def reset_dock_slide(self) -> None:
        """重新触发一次贴边三次缓动滑入动画。"""
        self._dock_elapsed = 0.0
        self.dock_progress = 0.0

    def step_dock_slide(self, dt: float, *, active: bool) -> float:
        if not active:
            self._dock_elapsed = DOCK_SLIDE_DURATION_SEC
            self.dock_progress = 1.0
            return 1.0
        if self._dock_elapsed < DOCK_SLIDE_DURATION_SEC:
            self._dock_elapsed = min(DOCK_SLIDE_DURATION_SEC, self._dock_elapsed + max(0.0, dt))
            self.dock_progress = ease_out_cubic(self._dock_elapsed / DOCK_SLIDE_DURATION_SEC)
        else:
            self.dock_progress = 1.0
        return self.dock_progress

    def step_walk_blend(self, dt: float, *, walking: bool, ramp_speed: float = 4.0) -> float:
        target = 1.0 if walking else 0.0
        self.walk_blend = damp_exp(self.walk_blend, target, ramp_speed, dt)
        if abs(self.walk_blend - target) < 0.005:
            self.walk_blend = target
        return self.walk_blend

    def step(
        self,
        *,
        facing_sign: float,
        scale_x: float,
        scale_y: float,
        angle: float,
        trans_x: float,
        trans_y: float,
        dt: float,
        speed: float = 7.5,
        snap: bool = False,
    ) -> TransformSnapshot:
        """将当前变换阻尼逼近到目标值；dt<=0 时保留现有阻尼态，仅 snap 或首帧才直接对齐。"""
        sign = -1.0 if facing_sign < 0.0 else 1.0
        mag_sx = abs(scale_x)
        if snap or not self.initialized:
            self.current = TransformSnapshot(
                facing_sign=sign,
                scale_x=mag_sx,
                scale_y=scale_y,
                angle=angle,
                trans_x=trans_x,
                trans_y=trans_y,
            )
            self.initialized = True
            return self.current

        cur = self.current
        cur.facing_sign = sign
        if dt <= 0.0:
            return cur
        cur.scale_x = damp_exp(cur.scale_x, mag_sx, speed, dt)
        cur.scale_y = damp_exp(cur.scale_y, scale_y, speed, dt)
        cur.angle = damp_exp(cur.angle, angle, speed, dt)
        cur.trans_x = damp_exp(cur.trans_x, trans_x, speed, dt)
        cur.trans_y = damp_exp(cur.trans_y, trans_y, speed, dt)
        return cur


# ==================== Walk Rig Kinematics ====================


def default_chibi_walk_kinematics() -> dict[str, float]:
    return {
        "leg_l_rot": 0.0,
        "leg_l_ty": 0.0,
        "leg_r_rot": 0.0,
        "leg_r_ty": 0.0,
        "arm_l_rot": 0.0,
        "arm_r_rot": 0.0,
        "body_ty": 0.0,
        "body_rot": 0.0,
    }


def default_std_walk_kinematics() -> dict[str, float]:
    return {
        "front_hip": 0.0,
        "front_knee": 0.0,
        "back_hip": 0.0,
        "back_knee": 0.0,
        "body_ty": 0.0,
        "body_rot": 0.0,
    }


def compute_chibi_walk_kinematics(phase: float, scale: float, blend: float = 1.0) -> dict[str, float]:
    """计算 Q 版五部件步行骨架单帧关节转角与躯干起伏。"""
    b = clamp(blend, 0.0, 1.0)
    stride = math.sin(phase)
    lift_l = max(0.0, math.cos(phase)) * b
    lift_r = max(0.0, -math.cos(phase)) * b
    return {
        "leg_l_rot": (-6.0 + 16.0 * stride) * b,
        "leg_l_ty": -lift_l * 4.5 * scale,
        "leg_r_rot": (8.0 - 16.0 * stride) * b,
        "leg_r_ty": -lift_r * 4.5 * scale,
        "arm_l_rot": (-6.0 - 15.0 * stride) * b,
        "arm_r_rot": (4.0 + 15.0 * stride) * b,
        "body_ty": -3.2 * (1.0 - math.cos(2.0 * phase)) * 0.5 * scale * b,
        "body_rot": (-1.8 + 1.3 * math.sin(2.0 * phase)) * b,
    }


def compute_std_walk_kinematics(phase: float, scale: float, blend: float = 1.0) -> dict[str, float]:
    """计算标准立绘分段腿部与躯干步行骨架单帧关节转角。"""
    b = clamp(blend, 0.0, 1.0)
    front = phase
    back = phase + math.pi
    return {
        "front_hip": 17.5 * math.sin(front) * b,
        "front_knee": (-32.0 * max(0.0, math.cos(front - 0.1)) - 5.0 * max(0.0, math.sin(front))) * b,
        "back_hip": 17.5 * math.sin(back) * b,
        "back_knee": (-32.0 * max(0.0, math.cos(back - 0.1)) - 5.0 * max(0.0, math.sin(back))) * b,
        "body_ty": -2.8 * (1.0 - math.cos(2.0 * phase)) * 0.5 * scale * b,
        "body_rot": (-2.2 + 1.1 * math.sin(2.0 * phase)) * b,
    }


# ==================== Secondary Springs & Unified Motion Engine ====================


class PokeTiltSpring:
    """二阶欠阻尼戳击歪头弹簧：连戳身体时平滑叠加摆角，消除旧进度条硬重置跳变。"""

    def __init__(self, omega: float = 19.0, zeta: float = 0.50) -> None:
        self.omega = float(omega)
        self.zeta = float(zeta)
        self.angle = 0.0
        self.vel = 0.0

    @property
    def is_active(self) -> bool:
        return abs(self.angle) > 0.03 or abs(self.vel) > 0.3

    def reset(self) -> None:
        self.angle = 0.0
        self.vel = 0.0

    def trigger(self, direction: float, peak_deg: float = 3.2) -> None:
        sign = -1.0 if direction < 0.0 else 1.0
        target_vel = sign * (abs(peak_deg) * self.omega) / _SQUASH_PEAK_FACTOR
        self.vel = clamp(self.vel * 0.35 + target_vel, -180.0, 180.0)

    def step(self, dt: float) -> float:
        step_dt = min(max(dt, 0.0), 0.05)
        if step_dt > 0.0 and self.is_active:
            substeps = max(1, min(6, int(math.ceil(step_dt * 240.0))))
            sub_dt = step_dt / float(substeps)
            w2 = self.omega * self.omega
            damp = 2.0 * self.zeta * self.omega
            for _ in range(substeps):
                accel = -w2 * self.angle - damp * self.vel
                self.vel += accel * sub_dt
                self.angle += self.vel * sub_dt
            self.angle = clamp(self.angle, -9.0, 9.0)
            if not self.is_active:
                self.angle = 0.0
                self.vel = 0.0
        return self.angle


class BristleBurstController:
    """统一管理鼓嘴/炸毛瞬间的弹跳与弹性膨胀曲线，消除拖拽与站立两套重复计算。"""

    def __init__(self, decay_rate: float = 1.8) -> None:
        self.decay_rate = float(decay_rate)
        self.remaining = 0.0

    def trigger(self, strength: float = 1.0, *, keep_higher: bool = False) -> None:
        val = clamp(strength, 0.0, 1.0)
        self.remaining = max(self.remaining, val) if keep_higher else val

    def reset(self) -> None:
        self.remaining = 0.0

    def advance(self, dt: float) -> None:
        if self.remaining > 0.001 and dt > 0.0:
            self.remaining = max(0.0, self.remaining - dt * self.decay_rate)
        elif self.remaining <= 0.001:
            self.remaining = 0.0

    def sample(
        self,
        *,
        scale: float,
        is_redhot: bool,
        is_dragging: bool,
    ) -> tuple[float, float, float, float]:
        """返回叠加增量 (delta_ty, mul_sx, mul_sy, delta_angle)。"""
        if self.remaining <= 0.001:
            return (0.0, 1.0, 1.0, 0.0)
        p = 1.0 - self.remaining
        hop = math.sin(p * math.pi) * (1.0 - 0.3 * p)
        expand = math.sin(p * math.pi * 1.5) * math.exp(-2.6 * p)
        burst_amp = 1.0 if is_redhot else 0.55
        hop_px = 18.0 if is_dragging else 22.0
        return (
            -hop_px * scale * hop * burst_amp,
            1.0 + 0.11 * expand * burst_amp,
            1.0 + 0.12 * expand * burst_amp,
            math.sin(p * math.pi * 4.0) * 5.0 * self.remaining * burst_amp,
        )


@dataclass(frozen=True)
class PoseMotionInput:
    """单帧姿态运动学计算所需的纯状态快照（无 Qt 窗口依赖）。"""

    anim_time: float
    dt: float
    scale: float
    docked_side: DockSide
    current_state: PetState
    override_pose: Optional[PetState]
    is_dragging: bool
    is_sync_working_locked: bool
    is_walking: bool
    walk_dir: int
    mouse_down: bool
    is_falling: bool
    throw_vx: float
    is_sleeping: bool
    is_dozing: bool
    anti_gravity_mode: bool
    is_animated_clip: bool
    clip_source_facing: int = 1
    use_std_walk_rig: bool = False
    use_chibi_walk_rig: bool = False
    std_rig_complete: bool = False
    chibi_rig_complete: bool = False
    snap: bool = False


class PetMotionEngine:
    """桌宠统一运动学与次级物理引擎。

    整合姿态指数阻尼、二阶果冻缩放弹簧、连戳歪头弹簧、炸毛突发曲线、
    帧率无关拖拽倾角衰减、步行骨架运动学与亚像素位移残差，输出单帧 TransformSnapshot。
    """

    def __init__(self) -> None:
        self.damper = TransformDamper()
        self.squash_spring = SquashSpring()
        self.poke_tilt_spring = PokeTiltSpring()
        self.bristle_burst = BristleBurstController()
        self.drag_tilt_vx = 0.0
        self.subpixel_x = 0.0
        self.subpixel_y = 0.0
        self.chibi_kinematics = default_chibi_walk_kinematics()
        self.std_kinematics = default_std_walk_kinematics()
        self.snapshot = TransformSnapshot()

    def set_subpixel_offset(self, ox: float, oy: float) -> None:
        self.subpixel_x = clamp(ox, -0.75, 0.75)
        self.subpixel_y = clamp(oy, -0.75, 0.75)

    def clear_subpixel_offset(self) -> None:
        self.subpixel_x = 0.0
        self.subpixel_y = 0.0

    def push_drag_velocity_x(self, vx: float) -> None:
        """拖拽横向速度低通平滑，驱动角色随手势微倾。"""
        self.drag_tilt_vx = self.drag_tilt_vx * 0.72 + vx * 0.28

    def decay_drag_tilt(self, dt: float) -> None:
        """使用帧率无关指数阻尼衰减拖拽倾角速度，替代旧版线性 (1 - dt * 12)。"""
        if self.drag_tilt_vx != 0.0:
            self.drag_tilt_vx = damp_exp(self.drag_tilt_vx, 0.0, 12.0, dt)
            if abs(self.drag_tilt_vx) <= 1.0:
                self.drag_tilt_vx = 0.0

    def trigger_poke(self, depth: float, direction: float) -> None:
        """连戳反馈：向缩放与歪头双弹簧注入连续冲量，连续点击零闪跳。"""
        self.squash_spring.trigger_squash(depth)
        self.poke_tilt_spring.trigger(direction, peak_deg=3.2)

    def reset_transient_effects(self, *, clear_drag_tilt: bool = False) -> None:
        self.squash_spring.reset()
        self.poke_tilt_spring.reset()
        self.clear_subpixel_offset()
        if clear_drag_tilt:
            self.drag_tilt_vx = 0.0

    def evaluate(self, state: PoseMotionInput) -> TransformSnapshot:
        """根据当前业务姿态快照推进所有阻尼/弹簧/骨骼状态并返回最终渲染变换。"""
        self.bristle_burst.advance(state.dt)
        facing_sign = 1.0
        sx = 1.0
        sy = 1.0
        angle = 0.0
        tx = 0.0
        ty = 0.0
        # 高频震颤与突发弹跳叠加项（不经低通阻尼以免糊掉炸毛冲击感）
        impulse_tx = 0.0
        impulse_ty = 0.0
        impulse_angle = 0.0
        impulse_sx = 1.0
        impulse_sy = 1.0
        s = state.scale
        t = state.anim_time
        walk_blend = self.damper.walk_blend

        if state.docked_side != DockSide.NONE:
            if not state.is_animated_clip:
                sy = 1.0 + 0.011 * math.sin(t * 2.5)
            side_sign = 1.0 if state.docked_side == DockSide.LEFT else -1.0
            angle = side_sign * 0.8 * math.sin(t * 2.1)
            dock_rem = 1.0 - self.damper.dock_progress
            if dock_rem > 0.001:
                angle += side_sign * 8.0 * dock_rem
        elif state.is_dragging and not state.is_sync_working_locked:
            angle = clamp(self.drag_tilt_vx * 0.009, -12.0, 12.0)
            sy = 1.02
            if state.override_pose in (PetState.ANNOYED, PetState.PUFFED, PetState.REDHOT):
                if state.override_pose == PetState.REDHOT:
                    impulse_tx = (math.sin(t * 64.0) * 0.7 + math.cos(t * 98.0) * 0.3) * (5.6 * s)
                    impulse_ty = math.sin(t * 74.0) * (1.4 * s)
                    impulse_angle = math.cos(t * 52.0) * 4.8
                    sx = 1.06
                    sy = 1.06
                elif state.override_pose == PetState.PUFFED:
                    impulse_tx = math.sin(t * 48.0) * (3.8 * s)
                    impulse_angle = math.cos(t * 38.0) * 3.2
                    sx = 1.035
                    sy = 1.035
                else:
                    impulse_tx = math.sin(t * 36.0) * (2.4 * s)
                    impulse_angle = math.cos(t * 28.0) * 2.0

                b_ty, b_sx, b_sy, b_ang = self.bristle_burst.sample(
                    scale=s,
                    is_redhot=(state.override_pose == PetState.REDHOT),
                    is_dragging=True,
                )
                impulse_ty += b_ty
                impulse_sx *= b_sx
                impulse_sy *= b_sy
                impulse_angle += b_ang
            elif state.override_pose == PetState.DIZZY:
                tx = math.cos(t * 7.2) * 5.0 * s
                ty = math.sin(t * 7.2) * 2.5 * s
                angle += math.sin(t * 7.2) * 8.0
        elif state.is_walking:
            if state.is_animated_clip:
                facing_sign = 1.0 if (state.walk_dir * state.clip_source_facing) >= 0 else -1.0
            else:
                facing_sign = -1.0 if state.walk_dir > 0 else 1.0
            if not state.mouse_down:
                if state.use_std_walk_rig and state.std_rig_complete:
                    self.std_kinematics = compute_std_walk_kinematics(t * 5.4, s, walk_blend)
                elif state.use_chibi_walk_rig and state.chibi_rig_complete:
                    self.chibi_kinematics = compute_chibi_walk_kinematics(t * 5.8, s, walk_blend)
                elif not state.is_animated_clip:
                    ty = -abs(math.sin(t * 8.2)) * 6.0 * walk_blend
                    angle = math.sin(t * 8.2) * 2.4 * state.walk_dir * walk_blend
        elif state.override_pose in (PetState.ANNOYED, PetState.PUFFED, PetState.REDHOT):
            if state.override_pose == PetState.REDHOT:
                impulse_tx = (math.sin(t * 62.0) * 0.7 + math.cos(t * 97.0) * 0.3) * (4.2 * s)
                impulse_ty = math.sin(t * 74.0) * (1.2 * s)
                impulse_angle = math.sin(t * 52.0) * 3.8
                sx = 1.055
                sy = 1.055
            elif state.override_pose == PetState.PUFFED:
                impulse_tx = math.sin(t * 46.0) * (2.6 * s)
                impulse_angle = math.sin(t * 36.0) * 2.4
                sx = 1.03
                sy = 1.03
            else:
                impulse_tx = math.sin(t * 34.0) * (1.5 * s)
                impulse_angle = math.sin(t * 24.0) * 1.3
                sy = 1.0 + 0.012 * math.sin(t * 8.0)

            b_ty, b_sx, b_sy, b_ang = self.bristle_burst.sample(
                scale=s,
                is_redhot=(state.override_pose == PetState.REDHOT),
                is_dragging=False,
            )
            impulse_ty += b_ty
            impulse_sx *= b_sx
            impulse_sy *= b_sy
            impulse_angle += b_ang

            if state.anti_gravity_mode:
                ty += -12.0 * s + 5.0 * s * math.sin(t * 2.1)
        elif state.override_pose == PetState.DIZZY:
            sway = math.sin(t * 4.6)
            angle = 9.0 * sway + 2.4 * math.sin(t * 9.2)
            tx = 6.5 * s * math.cos(t * 4.6)
            ty = 2.5 * s * abs(sway)
            sy = 1.0 - 0.022 * abs(sway)
            sx = 1.0 + 0.018 * abs(sway)
            if state.anti_gravity_mode:
                ty += -14.0 * s + 6.0 * s * math.sin(t * 2.1)
        elif state.override_pose == PetState.THINKING:
            if not state.is_animated_clip:
                sy = 1.0 + 0.014 * math.sin(t * 2.2)
                angle = 1.6 * math.sin(t * 1.4)
        elif (
            state.anti_gravity_mode
            or state.current_state == PetState.FLOAT
            or state.override_pose == PetState.FLOAT
        ):
            ty = -14.0 + 7.5 * math.sin(t * 1.7)
            angle = 2.6 * math.sin(t * 1.2)
            if not state.is_animated_clip:
                sy = 1.0 + 0.013 * math.sin(t * 1.7 + 0.8)
        elif state.is_sleeping or (state.is_dozing and state.override_pose is None):
            if not state.is_animated_clip:
                sy = 1.0 + 0.020 * math.sin(t * 1.2)
        else:
            if not state.is_animated_clip:
                sy = 1.0 + 0.012 * math.sin(t * 1.9)
                sx = 1.0 - 0.005 * math.sin(t * 1.9)

        if state.use_std_walk_rig and not state.is_walking:
            self.std_kinematics = compute_std_walk_kinematics(t * 5.4, s, walk_blend)
        if state.is_dozing:
            if not state.is_animated_clip:
                sy = 1.0 + 0.016 * math.sin(t * 1.2)
            if state.docked_side != DockSide.NONE:
                side_sign = 1.0 if state.docked_side == DockSide.LEFT else -1.0
                angle = side_sign * 1.2 * math.sin(t * 1.2)

        # 飞行抛掷期间按水平速度叠加坡度倾角，增强惯性立体感
        if state.is_falling and abs(state.throw_vx) > 40.0:
            angle += clamp(state.throw_vx * 0.009, -14.0, 14.0)

        # 基础姿态走帧率无关指数阻尼，抛掷飞行期提高收敛速度让撞墙倾角反转更干脆
        damp_speed = 14.5 if state.is_falling else 7.5
        damped = self.damper.step(
            facing_sign=facing_sign,
            scale_x=sx,
            scale_y=sy,
            angle=angle,
            trans_x=tx,
            trans_y=ty,
            dt=state.dt,
            speed=damp_speed,
            snap=state.snap,
        )

        final_sx = damped.scale_x * impulse_sx
        final_sy = damped.scale_y * impulse_sy
        final_angle = damped.angle + impulse_angle
        final_tx = damped.trans_x + impulse_tx + self.subpixel_x
        final_ty = damped.trans_y + impulse_ty + self.subpixel_y

        # 落地/弹跳/撞墙/戳击统一经由二阶果冻缩放弹簧与歪头弹簧叠加，全程 C1 连续无闪跳
        if self.squash_spring.is_active:
            ssx, ssy = self.squash_spring.step(state.dt)
            final_sx *= ssx
            final_sy *= ssy
        if self.poke_tilt_spring.is_active:
            final_angle += self.poke_tilt_spring.step(state.dt)

        self.snapshot = TransformSnapshot(
            facing_sign=damped.facing_sign,
            scale_x=final_sx,
            scale_y=final_sy,
            angle=final_angle,
            trans_x=final_tx,
            trans_y=final_ty,
        )
        return self.snapshot


# ==================== Pure Particle Physics System ====================


@dataclass
class StageParticle:
    """无 Qt 依赖的舞台特效粒子状态（由 PetStageRenderer 统一渲染）。"""

    glyph: str
    color_hex: str
    x: float
    y: float
    vx: float
    vy: float
    life: float
    max_life: float
    spin: float
    angle: float = 0.0
    font_size: int = 16


class ParticleSystem:
    """管理舞台漂浮文字/符号粒子的生成、容量上限与帧率无关欧拉积分。"""

    def __init__(self, max_particles: int = 36) -> None:
        self._max_particles = max(1, int(max_particles))
        self.particles: list[StageParticle] = []

    def clear(self) -> None:
        self.particles.clear()

    def spawn(
        self,
        glyph: str,
        color_hex: str,
        x: float,
        y: float,
        vx: float,
        vy: float,
        max_life: float,
    ) -> None:
        if len(self.particles) > self._max_particles:
            return
        self.particles.append(
            StageParticle(
                glyph=glyph,
                color_hex=color_hex,
                x=x,
                y=y,
                vx=vx,
                vy=vy,
                life=0.0,
                max_life=max(0.05, float(max_life)),
                spin=(random.random() - 0.5) * 90.0,
                font_size=13 if glyph == "Zzz" else 16,
            )
        )

    def spawn_star_burst(self, center_x: float, center_y: float, count: int) -> None:
        if count <= 0:
            return
        palette = ("#A78BFA", "#38BDF8", "#F472B6", "#FBBF24")
        for i in range(count):
            ang = (2.0 * math.pi * i) / count + random.random() * 0.4
            spd = 35.0 + random.random() * 55.0
            self.spawn(
                "✦",
                palette[i % len(palette)],
                center_x,
                center_y,
                math.cos(ang) * spd,
                math.sin(ang) * spd - 20.0,
                1.15,
            )

    def spawn_bristle(self, cx: float, cy: float, head_rx: float, intensity: int) -> None:
        """按怒气强度在头顶耳尖上方与两侧爆发怒气/火花粒子，避免遮挡面部表情。"""
        if intensity <= 0:
            return
        top_y = cy - 22.0
        if intensity == 1:
            self.spawn("💢", "#F87171", cx + head_rx * 0.85, top_y, 18.0, -36.0, 0.85)
            return
        if intensity == 2:
            self.spawn("💢", "#F87171", cx + head_rx * 0.90, top_y - 2.0, 26.0, -42.0, 0.90)
            self.spawn("⚡", "#FB923C", cx - head_rx * 1.05, top_y + 2.0, -28.0, -38.0, 0.85)
            return

        burst_specs = (
            ("💢", "#F87171"),
            ("⚡", "#FACC15"),
            ("✦", "#FB923C"),
            ("✧", "#FDE047"),
            ("⚡", "#FACC15"),
            ("💢", "#F43F5E"),
        )
        for idx, (glyph, color) in enumerate(burst_specs):
            ang = -math.pi * 0.95 + (math.pi * 0.90 * idx) / max(1, len(burst_specs) - 1)
            spd = 58.0 + (idx % 2) * 24.0
            sx = cx - 8.0 + math.cos(ang) * (head_rx * 1.05)
            sy = top_y + math.sin(ang) * 16.0
            self.spawn(glyph, color, sx, sy, math.cos(ang) * spd, math.sin(ang) * spd - 30.0, 0.95)

    def spawn_dizzy(self, cx: float, cy: float, head_rx: float, count: int) -> None:
        """生成头顶飘散的晕眩星光粒子。"""
        if count <= 0:
            return
        palette = (
            ("★", "#FDE047"),
            ("✦", "#C4B5FD"),
            ("✧", "#38BDF8"),
            ("✦", "#F9A8D4"),
        )
        for i in range(count):
            glyph, color = palette[i % len(palette)]
            ang = (2.0 * math.pi * i) / max(1, count) + random.random() * 0.4
            spd = 26.0 + random.random() * 28.0
            self.spawn(
                glyph,
                color,
                cx - 6.0 + math.cos(ang) * (head_rx * 0.85),
                cy - 18.0 + math.sin(ang) * 8.0,
                math.cos(ang) * spd,
                -24.0 - random.random() * 18.0,
                1.05,
            )

    def step(self, dt: float) -> None:
        if not self.particles or dt <= 0.0:
            return
        alive: list[StageParticle] = []
        for pt in self.particles:
            pt.life += dt
            if pt.life >= pt.max_life:
                continue
            pt.x += pt.vx * dt
            pt.y += pt.vy * dt
            pt.angle += pt.spin * dt
            alive.append(pt)
        self.particles = alive
