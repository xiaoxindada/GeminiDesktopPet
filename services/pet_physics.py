"""桌宠纯物理与平滑运动曲线引擎（无 Qt 窗口依赖，便于独立测试）。

参考 dsh-pet 的过阻尼弹簧、Q 弹挤压曲线与子步抛掷积分，以及 bailong 的
三次缓动贴边过渡，为渲染与窗口移动提供统一、帧率无关的运动学数值。
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from enum import Enum
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


# ==================== Walk Locomotion & Grounded Gait Kinematics ====================

# 行走速度与速度-步频锁定参数（在 1.0x 缩放下的基准值）
# 标准立绘：90 px/s ÷ 84 px/周期 ≈ 1.07 周期/秒（2.14 步/秒，ω ≈ 6.73 rad/s），贴近自然从容的日常步行节奏
STD_WALK_CRUISE_SPEED = 90.0
STD_WALK_STRIDE_CYCLE_PX = 84.0
STD_WALK_FALLBACK_OMEGA = 6.75

# Q 版：84 px/s ÷ 64 px/周期 ≈ 1.31 周期/秒（2.62 步/秒，ω ≈ 8.25 rad/s），契合短腿比例且自然不急促
CHIBI_WALK_CRUISE_SPEED = 84.0
CHIBI_WALK_STRIDE_CYCLE_PX = 64.0
CHIBI_WALK_FALLBACK_OMEGA = 8.25

WALK_ACCEL_PX_S2 = 280.0
WALK_DECEL_PX_S2 = 340.0
WALK_TURN_PAUSE_SEC = 0.12
WALK_EDGE_LOOKAHEAD_PX = 20.0


class WalkLocomotionState(str, Enum):
    """桌面漫步状态机：支持平滑加速、巡航、触边减速转身与优雅收步落地。"""

    IDLE = "idle"
    WALKING = "walking"
    TURNING = "turning"
    STOPPING = "stopping"


@dataclass(frozen=True)
class WalkStepResult:
    """行走状态机单帧推进结果（供窗口位移与骨架运动学共享）。"""

    x: float
    velocity_x: float
    direction: int
    stride_phase: float
    gait_blend: float
    speed_ratio: float
    is_active: bool
    finished_stop: bool


class WalkLocomotionController:
    """速度-步态耦合行走状态机。

    通过加减速曲线、边界预判转身与按真实位移积分的步态相位，彻底消除
    腿部慢动作、起步滑步、撞边瞬移翻转以及停步瞬间硬切回待机的问题。
    """

    def __init__(self) -> None:
        self.state = WalkLocomotionState.IDLE
        self.direction = 1
        self.velocity_x = 0.0
        self.stride_phase = 0.0
        self.gait_blend = 0.0
        self.speed_ratio = 0.0
        self.pace_factor = 1.0
        self._turn_wait_sec = 0.0
        self._pending_turn_dir = 1

    @property
    def is_active(self) -> bool:
        return self.state != WalkLocomotionState.IDLE

    def start(
        self,
        direction: Optional[int] = None,
        *,
        x: float = 0.0,
        min_x: float = 0.0,
        max_x: float = 1000.0,
        scale: float = 1.0,
        pace_factor: Optional[float] = None,
    ) -> int:
        """进入行走态：靠近屏幕边缘时优先朝开阔方向迈步，并注入起步初速让首步立即响应。"""
        s = max(0.4, float(scale))
        edge_margin = 95.0 * s
        if direction in (-1, 1):
            chosen_dir = int(direction)
        else:
            near_left = (x - min_x) < edge_margin
            near_right = (max_x - x) < edge_margin
            if near_left and not near_right:
                chosen_dir = 1
            elif near_right and not near_left:
                chosen_dir = -1
            else:
                chosen_dir = 1 if random.randint(0, 1) == 0 else -1

        was_idle = self.state == WalkLocomotionState.IDLE
        self.direction = chosen_dir
        self.state = WalkLocomotionState.WALKING
        self._turn_wait_sec = 0.0
        self.pace_factor = (
            clamp(float(pace_factor), 0.85, 1.25)
            if pace_factor is not None
            else random.uniform(0.95, 1.06)
        )
        if was_idle:
            # 从预备跨步相位起跳并给柔和初速，起步自然衔接不突兀
            self.stride_phase = 0.12 * 2.0 * math.pi
            self.velocity_x = float(chosen_dir) * 20.0 * s
            self.gait_blend = 0.28
            self.speed_ratio = 0.22
        return chosen_dir

    def request_stop(self) -> None:
        """请求自然收步停下（用于自主漫步结束），减速并归正双腿后再切回待机。"""
        if self.state in (WalkLocomotionState.WALKING, WalkLocomotionState.TURNING):
            self.state = WalkLocomotionState.STOPPING

    def cancel(self) -> None:
        """用户点击/拖拽/切换动作时立刻终止行走并清零速度。"""
        self.state = WalkLocomotionState.IDLE
        self.velocity_x = 0.0
        self.gait_blend = 0.0
        self.speed_ratio = 0.0
        self._turn_wait_sec = 0.0

    def step(
        self,
        dt: float,
        *,
        x: float,
        min_x: float,
        max_x: float,
        scale: float,
        is_standard: bool,
        move_window_scale: float = 1.0,
    ) -> WalkStepResult:
        """推进一帧行走位移与速度锁定步态相位。"""
        step_dt = min(max(float(dt), 0.0), 0.05)
        s = max(0.4, float(scale))
        if self.state == WalkLocomotionState.IDLE:
            self.velocity_x = 0.0
            self.gait_blend = 0.0
            self.speed_ratio = 0.0
            return WalkStepResult(x, 0.0, self.direction, self.stride_phase, 0.0, 0.0, False, False)

        base_speed = (STD_WALK_CRUISE_SPEED if is_standard else CHIBI_WALK_CRUISE_SPEED) * s * self.pace_factor
        stride_cycle_px = (STD_WALK_STRIDE_CYCLE_PX if is_standard else CHIBI_WALK_STRIDE_CYCLE_PX) * s
        accel = WALK_ACCEL_PX_S2 * s
        decel = WALK_DECEL_PX_S2 * s

        next_x = float(x)
        finished_stop = False

        if self.state == WalkLocomotionState.WALKING:
            target_vx = float(self.direction) * base_speed * clamp(move_window_scale, 0.0, 1.0)
            dv = target_vx - self.velocity_x
            max_dv = (accel if abs(target_vx) >= abs(self.velocity_x) else decel) * step_dt
            if abs(dv) <= max_dv:
                self.velocity_x = target_vx
            else:
                self.velocity_x += math.copysign(max_dv, dv)

            # 预判接近屏幕左右边界：提前柔和刹车进入转身拍，替代生硬的一帧镜像折返
            brake_dist = (self.velocity_x * self.velocity_x) / max(1.0, 2.0 * decel) + WALK_EDGE_LOOKAHEAD_PX * s
            if self.direction < 0 and (next_x - min_x) <= brake_dist:
                self.state = WalkLocomotionState.TURNING
                self._pending_turn_dir = 1
                self._turn_wait_sec = WALK_TURN_PAUSE_SEC
            elif self.direction > 0 and (max_x - next_x) <= brake_dist:
                self.state = WalkLocomotionState.TURNING
                self._pending_turn_dir = -1
                self._turn_wait_sec = WALK_TURN_PAUSE_SEC

        elif self.state == WalkLocomotionState.TURNING:
            if abs(self.velocity_x) > 1.0:
                brake = decel * 1.15 * step_dt
                if abs(self.velocity_x) <= brake:
                    self.velocity_x = 0.0
                else:
                    self.velocity_x -= math.copysign(brake, self.velocity_x)
            else:
                self.velocity_x = 0.0
                self._turn_wait_sec -= step_dt
                if self._turn_wait_sec <= 0.0:
                    self.direction = self._pending_turn_dir
                    self.state = WalkLocomotionState.WALKING
                    # 转身后给轻微起跑冲量，衔接更干脆
                    self.velocity_x = float(self.direction) * base_speed * 0.28

        elif self.state == WalkLocomotionState.STOPPING:
            brake = decel * step_dt
            if abs(self.velocity_x) <= brake:
                self.velocity_x = 0.0
            else:
                self.velocity_x -= math.copysign(brake, self.velocity_x)

        next_x += self.velocity_x * step_dt
        if next_x <= min_x:
            next_x = min_x
            if self.state == WalkLocomotionState.WALKING and self.direction < 0:
                self.velocity_x = 0.0
                self.state = WalkLocomotionState.TURNING
                self._pending_turn_dir = 1
                self._turn_wait_sec = WALK_TURN_PAUSE_SEC
            elif self.velocity_x < 0.0:
                self.velocity_x = 0.0
        elif next_x >= max_x:
            next_x = max_x
            if self.state == WalkLocomotionState.WALKING and self.direction > 0:
                self.velocity_x = 0.0
                self.state = WalkLocomotionState.TURNING
                self._pending_turn_dir = -1
                self._turn_wait_sec = WALK_TURN_PAUSE_SEC
            elif self.velocity_x > 0.0:
                self.velocity_x = 0.0

        # 核心：步态相位直接按真实地面位移积分，速度变化时步频自动匹配，彻底杜绝滑步与腿慢感
        speed_mag = abs(self.velocity_x)
        if speed_mag > 0.5 and stride_cycle_px > 1.0:
            self.stride_phase = (self.stride_phase + (speed_mag / stride_cycle_px) * (2.0 * math.pi) * step_dt) % (
                2.0 * math.pi
            )

        self.speed_ratio = clamp(speed_mag / max(1.0, base_speed), 0.0, 1.25)
        if self.state == WalkLocomotionState.WALKING:
            target_blend = clamp(0.35 + 0.65 * self.speed_ratio, 0.0, 1.0)
            self.gait_blend = damp_exp(self.gait_blend, target_blend, 14.0, step_dt)
        elif self.state == WalkLocomotionState.TURNING:
            target_blend = clamp(self.speed_ratio * 0.85, 0.0, 1.0)
            self.gait_blend = damp_exp(self.gait_blend, target_blend, 16.0, step_dt)
        else:
            # 收步阶段把腿部幅度快速收敛回站姿，完成后再释放回 IDLE
            self.gait_blend = damp_exp(self.gait_blend, 0.0, 15.0, step_dt)
            if speed_mag < 4.0 and self.gait_blend < 0.04:
                self.cancel()
                finished_stop = True

        return WalkStepResult(
            x=next_x,
            velocity_x=self.velocity_x,
            direction=self.direction,
            stride_phase=self.stride_phase,
            gait_blend=self.gait_blend,
            speed_ratio=self.speed_ratio,
            is_active=self.is_active,
            finished_stop=finished_stop,
        )


def _std_leg_gait_angles(phase: float) -> tuple[float, float]:
    """基于正常步行生物力学 3 阶傅里叶平滑拟合计算标准立绘单腿 (hip_deg, knee_deg)。

    全程 C∞ 无限阶光滑，彻底消除分段折角与急停顿挫（“噔噔感”）：
    - 髋角：+17.5°（着地前伸）平滑过渡到 -14.5°（后蹬离地）；
    - 膝角：保留自然双峰屈膝（着地缓冲 -9° → 支撑期伸直 -2° → 摆动期柔和折膝 -37°）。
    """
    hip = 1.5 + 16.0 * math.cos(phase) + 1.4 * math.sin(2.0 * phase)
    knee = (
        -13.583
        + 2.701 * math.cos(phase)
        + 13.733 * math.sin(phase)
        + 6.187 * math.cos(2.0 * phase)
        - 5.287 * math.sin(2.0 * phase)
        - 0.178 * math.cos(3.0 * phase)
        - 2.375 * math.sin(3.0 * phase)
    )
    return (hip, min(0.0, knee))


def default_chibi_walk_kinematics() -> dict[str, float]:
    return {
        "leg_l_rot": 0.0,
        "leg_l_tx": 0.0,
        "leg_l_ty": 0.0,
        "leg_r_rot": 0.0,
        "leg_r_tx": 0.0,
        "leg_r_ty": 0.0,
        "arm_l_rot": 0.0,
        "arm_r_rot": 0.0,
        "arm_ty": 0.0,
        "tail_rot": 0.0,
        "body_ty": 0.0,
        "body_rot": 0.0,
    }


def default_std_walk_kinematics() -> dict[str, float]:
    return {
        "front_hip": 0.0,
        "front_knee": 0.0,
        "back_hip": 0.0,
        "back_knee": 0.0,
        "front_arm_rot": 0.0,
        "back_arm_rot": 0.0,
        "tail_rot": 0.0,
        "hip_tx": 0.0,
        "hip_ty": 0.0,
        "body_ty": 0.0,
        "body_rot": 0.0,
    }


def compute_chibi_walk_kinematics(
    phase: float,
    scale: float,
    blend: float = 1.0,
    speed_ratio: float = 1.0,
) -> dict[str, float]:
    """计算 Q 版五部件步行骨架单帧关节转角与柔和起伏。

    全程使用 C∞ 平滑谐波与升余弦抬脚曲线（无 max 折角），补偿原图左右腿基准倾角与
    3/4 视角髋距，并让双臂相对同侧腿呈自然对侧摆动与轻微钟摆滞后。
    """
    b = clamp(blend, 0.0, 1.0)
    spd = clamp(speed_ratio, 0.0, 1.25)
    s_l = math.sin(phase)
    s_r = -s_l
    # 升余弦平方抬脚曲线：离地与落地导数均为 0，彻底消除顿挫感
    lift_l = ((0.5 + 0.5 * math.cos(phase)) ** 2) * b
    lift_r = ((0.5 - 0.5 * math.cos(phase)) ** 2) * b
    # 倒立摆双倍频起伏：过支撑中点轻抬，落地缓冲柔和下沉
    bounce = 0.5 + 0.5 * math.cos(2.0 * phase)
    body_ty = -3.4 * bounce * scale * b
    # 光滑骨盆微扭补偿（无折角），使后半步左右腿也清晰对称迈开
    cross_push = 0.5 * (1.0 - s_l) * b
    leg_l_tx = (2.0 * (-s_l) + 7.5 * cross_push) * scale * b
    leg_r_tx = (-2.0 * (-s_l) - 10.5 * cross_push) * scale * b
    # 手臂相对腿部带 0.26 rad 柔和钟摆滞后；猫尾带更大次级惯性滞后（0.75 rad）轻快摇摆
    arm_wave = math.sin(phase - 0.26)
    tail_rot = (2.0 + 10.5 * math.sin(phase - 0.75) + 2.8 * math.cos(2.0 * phase - 1.1)) * b
    return {
        "leg_l_rot": (-3.5 + 17.0 * s_l) * b,
        "leg_l_tx": leg_l_tx,
        "leg_l_ty": -lift_l * 5.8 * scale,
        "leg_r_rot": (11.0 + 19.0 * s_r) * b,
        "leg_r_tx": leg_r_tx,
        "leg_r_ty": -lift_r * 6.2 * scale,
        "arm_l_rot": (-4.0 - 20.0 * arm_wave) * b,
        "arm_r_rot": (4.0 + 20.0 * arm_wave) * b,
        "arm_ty": body_ty * 0.85,
        "tail_rot": tail_rot,
        "body_ty": body_ty,
        "body_rot": (-1.8 * max(0.35, spd) + 1.4 * math.sin(2.0 * phase - 0.3)) * b,
    }


def compute_std_walk_kinematics(
    phase: float,
    scale: float,
    blend: float = 1.0,
    speed_ratio: float = 1.0,
) -> dict[str, float]:
    """计算标准立绘腿部、对侧双臂与躯干的 C∞ 平滑步行运动学。

    用光滑双倍频正弦质心包络替代分段 max()，消除双脚交替瞬间的竖直折角跳变（“噔噔感”），
    同时保证鞋底贴齐任务栏底边不裁切，并输出带自然钟摆滞后的前后对侧摆臂转角。
    """
    b = clamp(blend, 0.0, 1.0)
    spd = clamp(speed_ratio, 0.0, 1.25)
    raw_f_hip, raw_f_knee = _std_leg_gait_angles(phase)
    raw_b_hip, raw_b_knee = _std_leg_gait_angles(phase + math.pi)
    front_hip = raw_f_hip * b
    front_knee = raw_f_knee * b
    back_hip = raw_b_hip * b
    back_knee = raw_b_knee * b

    # 拟合自双点 2-link 正运动学的 C∞ 光滑质心竖直起伏（范围约 [-3.7, -0.3] * scale），零折角且不穿底
    hip_ty = (-2.00 + 0.60 * math.cos(2.0 * phase) + 1.63 * math.sin(2.0 * phase)) * scale * b
    torso_lag_ty = 0.9 * scale * math.sin(2.0 * phase - 0.75) * b
    hip_tx = -1.0 * scale * math.cos(2.0 * phase) * b
    body_rot = (-2.2 * max(0.3, spd) + 1.0 * math.sin(2.0 * phase - 0.5)) * b

    # 自然对侧摆臂：左腿前迈（cos>0）时左臂向后摆（负角）、右臂向前探（正角），带 ~17° 放松钟摆滞后
    front_arm_rot = (1.5 - 17.0 * math.cos(phase - 0.30)) * b
    back_arm_rot = (2.5 + 18.0 * math.cos(phase - 0.30)) * b
    # 猫尾随步态与骨盆起伏做柔和次级惯性摆动（滞后 ~50°），轻盈灵动且不穿插衣摆/小腿
    tail_rot = (-2.0 + 8.0 * math.sin(phase - 0.85) + 2.2 * math.sin(2.0 * phase - 1.3)) * b
    return {
        "front_hip": front_hip,
        "front_knee": front_knee,
        "back_hip": back_hip,
        "back_knee": back_knee,
        "front_arm_rot": front_arm_rot,
        "back_arm_rot": back_arm_rot,
        "tail_rot": tail_rot,
        "hip_tx": hip_tx,
        "hip_ty": hip_ty,
        "body_ty": hip_ty + torso_lag_ty,
        "body_rot": body_rot,
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
    walk_phase: Optional[float] = None
    walk_gait_blend: Optional[float] = None
    walk_speed_ratio: Optional[float] = None
    snap: bool = False


class PetMotionEngine:
    """桌宠统一运动学与次级物理引擎。

    整合姿态指数阻尼、二阶果冻缩放弹簧、连戳歪头弹簧、炸毛突发曲线、
    帧率无关拖拽倾角衰减、速度锁定行走状态机、步行骨架运动学与亚像素位移残差，输出单帧 TransformSnapshot。
    """

    def __init__(self) -> None:
        self.damper = TransformDamper()
        self.walk = WalkLocomotionController()
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
            eff_blend = walk_blend if state.walk_gait_blend is None else state.walk_gait_blend
            eff_speed = 1.0 if state.walk_speed_ratio is None else state.walk_speed_ratio
            if not state.mouse_down:
                if state.use_std_walk_rig and state.std_rig_complete:
                    std_phase = t * STD_WALK_FALLBACK_OMEGA if state.walk_phase is None else state.walk_phase
                    self.std_kinematics = compute_std_walk_kinematics(std_phase, s, eff_blend, eff_speed)
                elif state.use_chibi_walk_rig and state.chibi_rig_complete:
                    chibi_phase = t * CHIBI_WALK_FALLBACK_OMEGA if state.walk_phase is None else state.walk_phase
                    self.chibi_kinematics = compute_chibi_walk_kinematics(chibi_phase, s, eff_blend, eff_speed)
                elif not state.is_animated_clip:
                    fallback_phase = t * 9.8 if state.walk_phase is None else state.walk_phase
                    ty = -abs(math.sin(fallback_phase)) * 6.5 * s * eff_blend
                    angle = math.sin(fallback_phase) * 2.8 * state.walk_dir * eff_blend
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
            std_phase = t * STD_WALK_FALLBACK_OMEGA if state.walk_phase is None else state.walk_phase
            eff_blend = walk_blend if state.walk_gait_blend is None else state.walk_gait_blend
            self.std_kinematics = compute_std_walk_kinematics(std_phase, s, eff_blend, 0.0)
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
