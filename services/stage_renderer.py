"""桌宠舞台渲染器与 A/B 双槽交叉淡化控制器。

将贴图归一化矩形计算、A/B 槽快退低残影交叉淡化（90ms）、骨骼步行渲染
及晕眩星光环绘制从 PetWindow 中独立解耦，统一消费 ActionClip 与 TransformSnapshot。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
import math
from typing import Optional, Sequence

from PySide6.QtCore import QRect, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPen, QPixmap

from models.pet_settings import DockSide, PetStyleMode
from models.pet_state import PetState
from services.action_clip import ActionClip, analyze_pixmap_metrics
from services.pet_physics import StageParticle, TransformSnapshot, clamp
from services.sprite_manager import WalkRigAssets, draw_pixmap_smooth


CROSSFADE_DURATION_SEC = 0.09
_UI_FONT_CACHE: dict[tuple[int, bool], QFont] = {}


def _cached_ui_font(size: int, *, pixel: bool = False) -> QFont:
    """缓存舞台特效符号使用的粗体 UI 字体，避免每帧为每个粒子/星芒反复构造 QFont。"""
    key = (max(8, int(size)), bool(pixel))
    font = _UI_FONT_CACHE.get(key)
    if font is None:
        font = QFont("Microsoft YaHei UI")
        font.setBold(True)
        if pixel:
            font.setPixelSize(key[0])
        else:
            font.setPointSize(key[0])
        _UI_FONT_CACHE[key] = font
    return font


def draw_pixmap_uniform(painter: QPainter, pixmap: Optional[QPixmap], target: QRectF) -> None:
    """按原始宽高比将 QPixmap 等比缩放并居中绘制，并自动裁去大面积空透明边以降低 CPU 旋转混合开销。"""
    if pixmap is None or pixmap.isNull() or target.width() <= 0 or target.height() <= 0:
        return
    pw = float(pixmap.width())
    ph = float(pixmap.height())
    if pw <= 0 or ph <= 0:
        return
    ratio = min(target.width() / pw, target.height() / ph)
    dw = pw * ratio
    dh = ph * ratio
    dx = target.x() + (target.width() - dw) * 0.5
    dy = target.y() + (target.height() - dh) * 0.5
    src = analyze_pixmap_metrics(pixmap).content_rect
    if src.width() < pixmap.width() or src.height() < pixmap.height():
        sub_target = QRectF(
            dx + src.x() * ratio,
            dy + src.y() * ratio,
            src.width() * ratio,
            src.height() * ratio,
        )
        draw_pixmap_smooth(painter, pixmap, sub_target, src)
        return
    draw_pixmap_smooth(painter, pixmap, QRectF(dx, dy, dw, dh))


def compute_local_sprite_rect(
    *,
    stage_w: float,
    stage_h: float,
    sprite_h: float,
    docked_side: DockSide,
    style_mode: PetStyleMode,
    use_std_walk_rig: bool,
    use_chibi_walk_rig: bool,
    std_rig_size: tuple[float, float],
    chibi_rig_size: tuple[float, float],
    active_clip: Optional[ActionClip],
    active_pixmap: Optional[QPixmap],
    dock_progress: float = 1.0,
) -> QRectF:
    """按统一脚底基线与躯干重心计算局部渲染矩形，避免切换动作时忽大忽小或横跳。"""
    offset_x_frac = 0.0
    offset_y_frac = 0.0
    anchor_x = 0.5

    if use_std_walk_rig:
        rw, rh = std_rig_size
    elif use_chibi_walk_rig:
        rw, rh = chibi_rig_size
    elif active_pixmap is not None and not active_pixmap.isNull():
        if active_clip is not None:
            content = active_clip.content_rect(active_pixmap)
            anchor_x = active_clip.anchor_x_ratio(active_pixmap)
            meta = active_clip.metadata
            render_scale = meta.render_scale
            offset_x_frac = meta.offset_x
            offset_y_frac = meta.offset_y
        else:
            metrics = analyze_pixmap_metrics(active_pixmap)
            content = metrics.content_rect
            anchor_x = metrics.anchor_x_norm
            render_scale = 1.0

        cw = float(content.width())
        ch = float(content.height())
        # 仅在静态宽幅低重心动作（或显式自定义缩放）生效缩放，兼容外部自定义图替换
        rh = sprite_h * render_scale if (ch > 0 and (render_scale == 1.0 or style_mode == PetStyleMode.STANDARD or (cw / ch) > 0.68)) else sprite_h
        rw = rh * (cw / ch) if ch > 0 else rh * 0.85
    else:
        rh = sprite_h
        rw = min(stage_w, rh * 0.85)

    if docked_side == DockSide.LEFT:
        # 贴边时按三次缓动进度从屏幕外侧滑入，消除硬切突兀感
        slide_out = (1.0 - max(0.0, min(1.0, dock_progress))) * rw * 0.55
        sx = -slide_out
    elif docked_side == DockSide.RIGHT:
        slide_out = (1.0 - max(0.0, min(1.0, dock_progress))) * rw * 0.55
        sx = stage_w - rw + slide_out
    else:
        # 非贴边：以躯干/脚底视觉重心对齐舞台中线，并温和夹在画布内防裁切
        desired_sx = stage_w * 0.5 - anchor_x * rw + rw * offset_x_frac
        if rw <= stage_w:
            sx = max(0.0, min(stage_w - rw, desired_sx))
        else:
            sx = (stage_w - rw) * 0.5

    sy = stage_h - rh + rh * offset_y_frac
    return QRectF(sx, sy, rw, rh)


@dataclass
class VisualRenderSlot:
    """单槽渲染快照（用于 A/B 交叉淡化保存离场画面）。"""

    slot_key: str
    clip: Optional[ActionClip]
    pixmap: Optional[QPixmap]
    use_chibi_rig: bool
    use_std_rig: bool
    sprite_rect: QRectF
    transform: TransformSnapshot
    chibi_kinematics: dict[str, float]
    std_kinematics: dict[str, float]
    docked_side: DockSide
    style_mode: PetStyleMode
    stage_size: tuple[int, int] = (0, 0)
    anchor_x_norm: float = 0.5
    is_dragging: bool = False
    elapsed_sec: float = 0.0


class StageCrossfader:
    """管理 A/B 双槽短时交叉淡化（默认 90ms），新图快入、旧图快退以避免双重曝光残影。"""

    def __init__(self, duration_sec: float = CROSSFADE_DURATION_SEC) -> None:
        self._duration = max(0.02, float(duration_sec))
        self._outgoing: Optional[VisualRenderSlot] = None
        self._elapsed = self._duration
        self._active_key: Optional[str] = None
        self._active_style: Optional[PetStyleMode] = None
        self._active_dock: Optional[DockSide] = None

    @property
    def is_active(self) -> bool:
        return self._outgoing is not None and self._elapsed < self._duration

    @property
    def outgoing(self) -> Optional[VisualRenderSlot]:
        return self._outgoing if self.is_active else None

    def clear(self) -> None:
        self._outgoing = None
        self._elapsed = self._duration

    def translate_outgoing(self, dx: float, dy: float) -> None:
        """拖拽补偿移动窗口时反向平移离场槽矩形，保证淡出残影留在原屏幕位置不跳移。"""
        if self._outgoing is not None:
            self._outgoing.sprite_rect.translate(dx, dy)

    @staticmethod
    def _is_geometry_compatible(prev_rect: QRectF, next_rect: Optional[QRectF]) -> bool:
        """新旧立绘轮廓尺寸差异过大（如站姿↔蹲姿）时禁止叠画，避免悬空头顶/肢体残影。"""
        if next_rect is None or prev_rect.isEmpty() or next_rect.isEmpty():
            return True
        prev_h = max(1.0, prev_rect.height())
        prev_w = max(1.0, prev_rect.width())
        h_ratio = abs(next_rect.height() - prev_h) / prev_h
        w_ratio = abs(next_rect.width() - prev_w) / prev_w
        return h_ratio <= 0.12 and w_ratio <= 0.24

    def notify_transition(
        self,
        *,
        new_key: str,
        new_style: PetStyleMode,
        new_dock: DockSide,
        previous_slot: Optional[VisualRenderSlot],
        stage_size: tuple[int, int] = (0, 0),
        is_dragging: bool = False,
        new_use_rig: bool = False,
        new_sprite_rect: Optional[QRectF] = None,
        new_facing_sign: Optional[float] = None,
        allow_blend: bool = True,
    ) -> None:
        """检测到视觉内容变化时按兼容性决定是否短时淡化；跨骨架/大幅变尺寸/翻转/抛掷时直接清槽防重影。"""
        if self._active_key is None:
            self._active_key = new_key
            self._active_style = new_style
            self._active_dock = new_dock
            return

        if new_key == self._active_key and new_style == self._active_style and new_dock == self._active_dock:
            return

        same_stage = (
            stage_size == (0, 0)
            or previous_slot is None
            or previous_slot.stage_size == (0, 0)
            or previous_slot.stage_size == stage_size
        )
        prev_use_rig = bool(
            previous_slot is not None
            and (previous_slot.use_chibi_rig or previous_slot.use_std_rig)
        )
        same_facing = (
            new_facing_sign is None
            or previous_slot is None
            or (previous_slot.transform.facing_sign * new_facing_sign) > 0.0
        )
        geom_compatible = (
            previous_slot is not None
            and self._is_geometry_compatible(previous_slot.sprite_rect, new_sprite_rect)
        )
        can_blend = (
            allow_blend
            and previous_slot is not None
            and same_stage
            and self._active_style == new_style
            and self._active_dock == new_dock
            and previous_slot.is_dragging == is_dragging
            and prev_use_rig == new_use_rig
            and not new_use_rig
            and same_facing
            and geom_compatible
        )
        self._active_key = new_key
        self._active_style = new_style
        self._active_dock = new_dock

        if can_blend:
            self._outgoing = replace(
                previous_slot,
                transform=replace(previous_slot.transform),
                chibi_kinematics=dict(previous_slot.chibi_kinematics),
                std_kinematics=dict(previous_slot.std_kinematics),
                sprite_rect=QRectF(previous_slot.sprite_rect),
            )
            self._elapsed = 0.0
        else:
            self.clear()

    def advance(self, dt: float) -> None:
        if self._outgoing is None:
            return
        self._elapsed += max(0.0, dt)
        if self._elapsed >= self._duration:
            self.clear()

    def opacities(self) -> tuple[float, float]:
        """返回 (outgoing_alpha, incoming_alpha)：旧图二次方快退、新图极速拉满，兼顾不发虚与零拖影。"""
        if not self.is_active:
            return (0.0, 1.0)
        p = clamp(self._elapsed / self._duration, 0.0, 1.0)
        rem = 1.0 - p
        out_alpha = rem * rem
        in_alpha = 1.0 - (rem ** 4)
        return (out_alpha, in_alpha)


class PetStageRenderer:
    """无状态舞台绘制器：负责绘出单槽（静态/动态 WebP/骨骼）、晕眩光环与粒子。"""

    @staticmethod
    def pivot_for_rect(
        sprite_rect: QRectF,
        docked_side: DockSide,
        anchor_x_norm: float = 0.5,
    ) -> tuple[float, float]:
        if docked_side == DockSide.LEFT:
            pivot_x = sprite_rect.left()
        elif docked_side == DockSide.RIGHT:
            pivot_x = sprite_rect.right()
        else:
            # 锚定在视觉躯干重心，保证镜像翻转与倾斜旋转不产生横向跳移
            pivot_x = sprite_rect.left() + sprite_rect.width() * anchor_x_norm
        return (pivot_x, sprite_rect.bottom())

    @staticmethod
    def head_crown_anchor(
        sprite_rect: QRectF,
        style_mode: PetStyleMode,
        visual_state: Optional[PetState],
        anchor_x_norm: float = 0.5,
    ) -> tuple[float, float, float]:
        """返回角色头顶中心与横向半轴，结合归一化重心适配 Q 版与标准立绘。"""
        is_std = style_mode == PetStyleMode.STANDARD
        cx = sprite_rect.left() + sprite_rect.width() * anchor_x_norm
        if is_std and visual_state == PetState.DIZZY and abs(anchor_x_norm - 0.5) < 0.02:
            cx -= sprite_rect.width() * 0.07
        cy = sprite_rect.top() + sprite_rect.height() * (0.03 if is_std else 0.055)
        rx = sprite_rect.width() * (0.24 if is_std else 0.34)
        return cx, cy, rx

    @classmethod
    def paint_slot(
        cls,
        painter: QPainter,
        slot: VisualRenderSlot,
        walk_rig: WalkRigAssets,
        *,
        opacity: float = 1.0,
        live_transform: Optional[TransformSnapshot] = None,
    ) -> None:
        if opacity <= 0.005:
            return
        sprite_rect = slot.sprite_rect
        pivot_x, pivot_y = cls.pivot_for_rect(sprite_rect, slot.docked_side, slot.anchor_x_norm)
        tf = slot.transform
        if live_transform is not None:
            # 离场淡出槽同步实时位移、倾角与缩放，只保留自身水平朝向，避免淡化时身影分裂错位
            tf = TransformSnapshot(
                facing_sign=slot.transform.facing_sign,
                scale_x=live_transform.scale_x,
                scale_y=live_transform.scale_y,
                angle=live_transform.angle,
                trans_x=live_transform.trans_x,
                trans_y=live_transform.trans_y,
            )
        effective_sx = tf.facing_sign * tf.scale_x

        painter.save()
        if opacity < 0.999:
            painter.setOpacity(painter.opacity() * opacity)
        painter.translate(pivot_x + tf.trans_x, pivot_y + tf.trans_y)
        painter.rotate(tf.angle)
        painter.scale(effective_sx, tf.scale_y)
        painter.translate(-pivot_x, -pivot_y)

        if slot.use_std_rig:
            cls._paint_std_walk_rig(painter, sprite_rect, walk_rig, slot.std_kinematics)
        elif slot.use_chibi_rig:
            cls._paint_chibi_walk_rig(painter, sprite_rect, walk_rig, slot.chibi_kinematics)
        else:
            pm = slot.pixmap
            if pm is not None and not pm.isNull():
                content = (
                    slot.clip.content_rect(pm)
                    if slot.clip is not None
                    else analyze_pixmap_metrics(pm).content_rect
                )
                draw_pixmap_smooth(painter, pm, sprite_rect, content)

        painter.restore()

    @staticmethod
    def _paint_chibi_walk_rig(
        painter: QPainter,
        rect: QRectF,
        rig: WalkRigAssets,
        k: dict[str, float],
    ) -> None:
        rw, rh = rect.width(), rect.height()
        rx, ry = rect.x(), rect.y()
        local_rect = QRectF(0.0, 0.0, rw, rh)

        def draw_part(
            pm: Optional[QPixmap],
            px: float,
            py: float,
            angle: float,
            ty: float = 0.0,
            tx: float = 0.0,
        ) -> None:
            if pm is None:
                return
            painter.save()
            painter.translate(rx + px * rw + tx, ry + py * rh + ty)
            painter.rotate(angle)
            painter.translate(-px * rw, -py * rh)
            draw_pixmap_uniform(painter, pm, local_rect)
            painter.restore()

        arm_ty = k.get("arm_ty", 0.0)
        if rig.chibi_tail is not None:
            painter.save()
            painter.translate(rx + 0.48 * rw, ry + 0.56 * rh + k["body_ty"])
            painter.rotate(k["body_rot"])
            painter.translate(-0.48 * rw, -0.56 * rh)
            painter.translate(0.648 * rw, 0.750 * rh)
            painter.rotate(k.get("tail_rot", 0.0))
            painter.translate(-0.648 * rw, -0.750 * rh)
            draw_pixmap_uniform(painter, rig.chibi_tail, local_rect)
            painter.restore()
        draw_part(rig.chibi_arm_l, 0.3250, 0.5083, k["arm_l_rot"], arm_ty)
        draw_part(rig.chibi_leg_r, 0.5172, 0.7233, k["leg_r_rot"], k["leg_r_ty"], k.get("leg_r_tx", 0.0))
        draw_part(rig.chibi_leg_l, 0.3538, 0.7233, k["leg_l_rot"], k["leg_l_ty"], k.get("leg_l_tx", 0.0))
        draw_part(rig.chibi_body, 0.48, 0.56, k["body_rot"], k["body_ty"])
        draw_part(rig.chibi_arm_r, 0.5643, 0.5483, k["arm_r_rot"], arm_ty)

    @staticmethod
    def _paint_std_walk_rig(
        painter: QPainter,
        rect: QRectF,
        rig: WalkRigAssets,
        k: dict[str, float],
    ) -> None:
        rw, rh = rect.width(), rect.height()
        rx, ry = rect.x(), rect.y()
        local_rect = QRectF(0.0, 0.0, rw, rh)
        hip_tx = k.get("hip_tx", 0.0)
        hip_ty = k.get("hip_ty", 0.0)

        def draw_leg(
            thigh: Optional[QPixmap],
            shin: Optional[QPixmap],
            hip_deg: float,
            knee_deg: float,
        ) -> None:
            painter.save()
            painter.translate(rx + 0.3086 * rw + hip_tx, ry + 0.5578 * rh + hip_ty)
            painter.rotate(hip_deg)
            painter.translate(-0.3086 * rw, -0.5578 * rh)
            draw_pixmap_uniform(painter, thigh, local_rect)

            painter.save()
            painter.translate(0.3472 * rw, 0.7022 * rh)
            painter.rotate(knee_deg)
            painter.translate(-0.3472 * rw, -0.7022 * rh)
            draw_pixmap_uniform(painter, shin, local_rect)
            painter.restore()
            painter.restore()

        # 后侧手臂绘制在最底层，挂接在躯干变换内保证肩部随前倾/起伏不脱节
        if rig.std_back_arm is not None:
            painter.save()
            painter.translate(rx + 0.5 * rw + hip_tx, ry + 0.46 * rh + k["body_ty"])
            painter.rotate(k["body_rot"])
            painter.translate(-0.5 * rw, -0.46 * rh)
            painter.translate(0.295 * rw, 0.242 * rh)
            painter.rotate(k.get("back_arm_rot", 0.0))
            painter.translate(-0.347 * rw, -0.242 * rh)
            draw_pixmap_uniform(painter, rig.std_back_arm, local_rect)
            painter.restore()

        draw_leg(
            rig.std_back_thigh or rig.std_front_thigh,
            rig.std_back_shin or rig.std_front_shin,
            k["back_hip"],
            k["back_knee"],
        )
        draw_leg(rig.std_front_thigh, rig.std_front_shin, k["front_hip"], k["front_knee"])
        if rig.std_body is not None:
            painter.save()
            painter.translate(rx + 0.5 * rw + hip_tx, ry + 0.46 * rh + k["body_ty"])
            painter.rotate(k["body_rot"])
            painter.translate(-0.5 * rw, -0.46 * rh)
            # 尾巴绘于躯干/衣摆之后并继承躯干前倾起伏，根部稳藏在衣摆下无缝摇摆
            if rig.std_tail is not None:
                painter.save()
                painter.translate(0.525 * rw, 0.538 * rh)
                painter.rotate(k.get("tail_rot", 0.0))
                painter.translate(-0.525 * rw, -0.538 * rh)
                draw_pixmap_uniform(painter, rig.std_tail, local_rect)
                painter.restore()
            draw_pixmap_uniform(painter, rig.std_body, local_rect)
            if rig.std_front_arm is not None:
                painter.save()
                painter.translate(0.347 * rw, 0.242 * rh)
                painter.rotate(k.get("front_arm_rot", 0.0))
                painter.translate(-0.347 * rw, -0.242 * rh)
                draw_pixmap_uniform(painter, rig.std_front_arm, local_rect)
                painter.restore()
            painter.restore()

    @classmethod
    def paint_dizzy_halo(
        cls,
        painter: QPainter,
        *,
        sprite_rect: QRectF,
        pivot_x: float,
        pivot_y: float,
        transform: TransformSnapshot,
        anim_time: float,
        scale: float,
        style_mode: PetStyleMode,
        visual_state: Optional[PetState],
        anchor_x_norm: float,
        front_layer: bool,
    ) -> None:
        """在角色头顶绘制前后分层的绕圈旋转星光与晕眩轨迹环（随头部摇摆联动）。"""
        local_hx, local_hy, rx = cls.head_crown_anchor(
            sprite_rect, style_mode, visual_state, anchor_x_norm
        )
        effective_sx = transform.facing_sign * transform.scale_x
        dx = (local_hx - pivot_x) * effective_sx
        dy = (local_hy - pivot_y) * transform.scale_y
        rad = math.radians(transform.angle)
        cos_a = math.cos(rad)
        sin_a = math.sin(rad)
        cx = pivot_x + transform.trans_x + dx * cos_a - dy * sin_a
        cy = max(24.0 * scale, pivot_y + transform.trans_y + dx * sin_a + dy * cos_a)
        ry = max(8.0, rx * 0.34)
        ring_tilt = transform.angle * 0.75 + 6.0 * math.sin(anim_time * 3.8)

        painter.save()
        painter.translate(cx, cy)
        painter.rotate(ring_tilt)

        start_deg = 180 if front_layer else 0
        outer_rect = QRectF(-rx, -ry, rx * 2.0, ry * 2.0)
        inner_rect = QRectF(-rx * 0.78, -ry * 0.78, rx * 1.56, ry * 1.56)

        outer_color = QColor("#C4B5FD")
        outer_color.setAlpha(185 if front_layer else 105)
        outer_pen = QPen(outer_color, max(1.4, 2.0 * scale))
        outer_pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(outer_pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawArc(outer_rect, start_deg * 16, 180 * 16)

        painter.save()
        painter.rotate(-6.0 + 4.0 * math.sin(anim_time * 5.2))
        inner_color = QColor("#FDE047")
        inner_color.setAlpha(155 if front_layer else 85)
        inner_pen = QPen(inner_color, max(1.1, 1.45 * scale))
        inner_pen.setStyle(Qt.PenStyle.DashLine)
        inner_pen.setDashOffset(-anim_time * 8.0)
        painter.setPen(inner_pen)
        painter.drawArc(inner_rect, start_deg * 16, 180 * 16)
        painter.restore()

        orbit_stars = (
            ("★", "#FDE047"),
            ("✦", "#C4B5FD"),
            ("✧", "#38BDF8"),
            ("★", "#F9A8D4"),
            ("✦", "#FDE047"),
        )
        base_phase = anim_time * 4.4
        for idx, (glyph, color_hex) in enumerate(orbit_stars):
            theta = base_phase + (2.0 * math.pi * idx) / len(orbit_stars)
            sin_t = math.sin(theta)
            is_front = sin_t > 0.0
            if is_front != front_layer:
                continue
            px = rx * math.cos(theta)
            py = ry * sin_t
            depth = 0.5 + 0.5 * sin_t
            font_px = max(12, int(round((13.0 + 9.0 * depth) * scale)))
            alpha = 0.48 + 0.50 * depth

            color = QColor(color_hex)
            color.setAlphaF(min(1.0, alpha))
            font = _cached_ui_font(font_px, pixel=True)

            painter.save()
            painter.translate(px, py)
            painter.rotate(math.degrees(theta) * 0.45)

            if front_layer:
                glow = QColor(color_hex)
                glow.setAlphaF(0.16 * depth)
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(glow)
                r = font_px * 0.42
                painter.drawEllipse(QRectF(-r, -r, r * 2.0, r * 2.0))

            painter.setPen(color)
            painter.setFont(font)
            box = float(font_px) * 1.9
            painter.drawText(
                QRectF(-box * 0.5, -box * 0.5, box, box),
                Qt.AlignmentFlag.AlignCenter,
                glyph,
            )
            painter.restore()

        painter.restore()

    @staticmethod
    def paint_particles(painter: QPainter, particles: Sequence[StageParticle]) -> None:
        """统一绘制舞台漂浮粒子叠加层，复用字体缓存降低每帧分配开销。"""
        if not particles:
            return
        for pt in particles:
            alpha = max(0.0, min(1.0, 1.0 - (pt.life / pt.max_life)))
            if alpha <= 0.01:
                continue
            color = QColor(pt.color_hex)
            color.setAlphaF(alpha)
            painter.save()
            painter.translate(pt.x + 8.0, pt.y + 8.0)
            painter.rotate(pt.angle)
            painter.translate(-8.0, -8.0)
            painter.setPen(color)
            painter.setFont(_cached_ui_font(pt.font_size, pixel=False))
            painter.drawText(
                QRectF(0.0, 0.0, 48.0, 32.0),
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop,
                pt.glyph,
            )
            painter.restore()
