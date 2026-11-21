"""立绘与骨骼部件精灵资源管理器（使用 PySide6 QPixmap 缓存）。"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
import os
from typing import Optional

from PySide6.QtCore import QRect, QRectF, Qt
from PySide6.QtGui import QImage, QPainter, QPixmap

from models.app_version import get_bundled_resource_dir
from models.pet_settings import PetStyleMode
from models.pet_state import PetState

_SMOOTH_DOWNSCALE_CACHE_MAX = 96
_smooth_downscale_cache: OrderedDict[tuple[int, int, int, int, int, int, int], QPixmap] = OrderedDict()


def _get_smooth_downscaled_pixmap(
    pixmap: QPixmap,
    source: QRect,
    phys_w: int,
    phys_h: int,
) -> QPixmap:
    """用预乘 Alpha 面积平均滤波缓存缩图，避免高分辨率标准立绘单步双线性缩小产生锯齿毛刺。"""
    key = (int(pixmap.cacheKey()), source.x(), source.y(), source.width(), source.height(), phys_w, phys_h)
    cached = _smooth_downscale_cache.get(key)
    if cached is not None:
        _smooth_downscale_cache.move_to_end(key)
        return cached

    full_rect = QRect(0, 0, pixmap.width(), pixmap.height())
    image = pixmap.toImage().convertToFormat(QImage.Format.Format_ARGB32_Premultiplied)
    if source != full_rect:
        image = image.copy(source)
    scaled_img = image.scaled(
        phys_w,
        phys_h,
        Qt.AspectRatioMode.IgnoreAspectRatio,
        Qt.TransformationMode.SmoothTransformation,
    )
    scaled_pm = QPixmap.fromImage(scaled_img)
    _smooth_downscale_cache[key] = scaled_pm
    while len(_smooth_downscale_cache) > _SMOOTH_DOWNSCALE_CACHE_MAX:
        _smooth_downscale_cache.popitem(last=False)
    return scaled_pm


def draw_pixmap_smooth(
    painter: QPainter,
    pixmap: Optional[QPixmap],
    target: QRectF,
    source: Optional[QRect] = None,
) -> None:
    """按设备物理像素尺寸高质量绘制贴图，大倍率缩小时自动使用面积平均滤波。"""
    if pixmap is None or pixmap.isNull() or target.width() <= 0 or target.height() <= 0:
        return
    full_rect = QRect(0, 0, pixmap.width(), pixmap.height())
    src = full_rect if source is None else source.intersected(full_rect)
    if src.isEmpty():
        return

    device = painter.device()
    dpr = max(1.0, float(device.devicePixelRatioF())) if device is not None else 1.0
    phys_w = max(1, int(round(target.width() * dpr)))
    phys_h = max(1, int(round(target.height() * dpr)))

    if src.width() > phys_w or src.height() > phys_h:
        scaled = _get_smooth_downscaled_pixmap(pixmap, src, phys_w, phys_h)
        painter.drawPixmap(target, scaled, QRectF(0.0, 0.0, float(scaled.width()), float(scaled.height())))
        return

    painter.drawPixmap(target, pixmap, QRectF(src))


@dataclass(frozen=True)
class WalkRigAssets:
    chibi_body: Optional[QPixmap] = None
    chibi_arm_l: Optional[QPixmap] = None
    chibi_arm_r: Optional[QPixmap] = None
    chibi_leg_l: Optional[QPixmap] = None
    chibi_leg_r: Optional[QPixmap] = None
    std_body: Optional[QPixmap] = None
    std_front_thigh: Optional[QPixmap] = None
    std_front_shin: Optional[QPixmap] = None
    std_back_thigh: Optional[QPixmap] = None
    std_back_shin: Optional[QPixmap] = None

    @property
    def chibi_complete(self) -> bool:
        return all(getattr(self, name) is not None for name in (
            "chibi_body", "chibi_arm_l", "chibi_arm_r", "chibi_leg_l", "chibi_leg_r",
        ))

    @property
    def standard_complete(self) -> bool:
        return all(getattr(self, name) is not None for name in (
            "std_body", "std_front_thigh", "std_front_shin",
        ))


class SpriteManager:
    """加载原版动作贴图与两种比例的骨架资源。"""

    def __init__(self, assets_root: Optional[str] = None) -> None:
        base = assets_root or os.path.join(get_bundled_resource_dir(), "Assets")
        self._sprites_dir = os.path.join(base, "Sprites")
        self._standard_dir = os.path.join(base, "Standard")
        self._walkrig_dir = os.path.join(base, "WalkRig")
        self._cache: dict[PetState, QPixmap] = {}
        self._standard_cache: dict[str, QPixmap] = {}
        self._fallback: Optional[QPixmap] = None
        self._standard_fallback: Optional[QPixmap] = None
        self.walk_rig = WalkRigAssets()

    def preload_all(self) -> None:
        self._fallback = self._load_pixmap(os.path.join(self._sprites_dir, "idle.png"))
        for state in PetState:
            key = state.to_sprite_key()
            pix = self._load_pixmap(os.path.join(self._sprites_dir, f"{key}.png"))
            if pix is not None:
                self._cache[state] = pix

        # 预加载标准立绘分面贴图（standee / front / side / back / edge_*）
        self._standard_fallback = self._load_pixmap(os.path.join(self._standard_dir, "standee.png"))
        for std_key in (
            "standee",
            "front",
            "side",
            "back",
            "edge_left",
            "edge_left_peek",
            "edge_right",
            "edge_right_peek",
        ):
            pix = self._load_pixmap(os.path.join(self._standard_dir, f"{std_key}.png"))
            if pix is not None:
                self._standard_cache[std_key] = pix

        self.walk_rig = WalkRigAssets(
            chibi_body=self._load_pixmap(os.path.join(self._walkrig_dir, "chibi_body.png")),
            chibi_arm_l=self._load_pixmap(os.path.join(self._walkrig_dir, "chibi_arm_l.png")),
            chibi_arm_r=self._load_pixmap(os.path.join(self._walkrig_dir, "chibi_arm_r.png")),
            chibi_leg_l=self._load_pixmap(os.path.join(self._walkrig_dir, "chibi_leg_l.png")),
            chibi_leg_r=self._load_pixmap(os.path.join(self._walkrig_dir, "chibi_leg_r.png")),
            std_body=self._load_pixmap(os.path.join(self._walkrig_dir, "std_body.png")),
            std_front_thigh=self._load_pixmap(os.path.join(self._walkrig_dir, "std_thigh.png")),
            std_front_shin=self._load_pixmap(os.path.join(self._walkrig_dir, "std_shin.png")),
            std_back_thigh=self._load_pixmap(os.path.join(self._walkrig_dir, "std_thigh_back.png")),
            std_back_shin=self._load_pixmap(os.path.join(self._walkrig_dir, "std_shin_back.png")),
        )
        self._ensure_base_poses()

    def _ensure_base_poses(self) -> None:
        rig = self.walk_rig
        if self._fallback is None and rig.chibi_complete:
            self._fallback = self._compose((rig.chibi_arm_l, rig.chibi_leg_r,
                rig.chibi_leg_l, rig.chibi_body, rig.chibi_arm_r))
        if self._standard_fallback is None and rig.standard_complete:
            self._standard_fallback = self._compose((
                rig.std_back_thigh or rig.std_front_thigh, rig.std_back_shin or rig.std_front_shin,
                rig.std_front_thigh, rig.std_front_shin, rig.std_body))
        # 单风格资源包也保持原版形象，允许后续只提供一张基础图。
        self._fallback = self._fallback or self._standard_fallback
        self._standard_fallback = self._standard_fallback or self._fallback

    @staticmethod
    def _compose(parts: tuple[QPixmap, ...]) -> QPixmap:
        result = QPixmap(parts[0].size())
        result.fill(Qt.GlobalColor.transparent)
        painter = QPainter(result)
        for part in parts:
            painter.drawPixmap(result.rect(), part)
        painter.end()
        return result

    def get(self, state: PetState, style_mode: PetStyleMode = PetStyleMode.CHIBI) -> Optional[QPixmap]:
        if style_mode == PetStyleMode.STANDARD:
            std_pix = self._get_standard_for_state(state)
            if std_pix is not None:
                return std_pix
        # 缺图按动作语义复用原版，再回落基础姿态；新增动作自然覆盖此路径。
        groups = (
            (PetState.PAT, PetState.HAPPY, PetState.COAXED, PetState.FORGIVEN, PetState.PROUD),
            (PetState.THINKING, PetState.READING, PetState.WRITING, PetState.DRAWING, PetState.LISTENING),
            (PetState.SLEEPING, PetState.EDGE_LEFT_SLEEP, PetState.EDGE_RIGHT_SLEEP, PetState.TIRED, PetState.YAWN),
            (PetState.RUNNING, PetState.SEARCHING),
            (PetState.ANNOYED, PetState.PUFFED, PetState.REDHOT, PetState.SULK),
            (PetState.EDGE_LEFT_PEEK, PetState.EDGE_LEFT),
            (PetState.EDGE_RIGHT_PEEK, PetState.EDGE_RIGHT),
        )
        for candidate in (state,) + next((group for group in groups if state in group), ()):
            pix = self._cache.get(candidate)
            if pix is not None:
                return pix
        return self._fallback

    def _get_standard_for_state(self, state: PetState) -> Optional[QPixmap]:
        exact = self._standard_cache.get(state.value)
        if exact is None:
            exact = self._load_pixmap(os.path.join(self._standard_dir, state.value + ".png"))
            if exact is not None:
                self._standard_cache[state.value] = exact
        if exact is not None:
            return exact
        if state in (PetState.SLEEPING, PetState.EDGE_LEFT_SLEEP, PetState.EDGE_RIGHT_SLEEP):
            # 睡眠只能优先复用原版的闭眼素材，背面图不代表睡眠。
            for key in ("sleeping", "edge_left_sleep", "edge_right_sleep"):
                sleeping = self._standard_cache.get(key)
                if sleeping is None:
                    sleeping = self._load_pixmap(os.path.join(self._standard_dir, key + ".png"))
                    if sleeping is not None:
                        self._standard_cache[key] = sleeping
                if sleeping is not None:
                    return sleeping
            # 缺少睡姿素材时保留当前比例的基础图。
            return self._standard_fallback
        if state == PetState.EDGE_LEFT:
            key = "edge_left"
        elif state == PetState.EDGE_LEFT_PEEK:
            key = "edge_left_peek"
        elif state == PetState.EDGE_RIGHT:
            key = "edge_right"
        elif state == PetState.EDGE_RIGHT_PEEK:
            key = "edge_right_peek"
        elif state in (PetState.RUNNING, PetState.SEARCHING):
            key = "side"
        elif state in (PetState.SULK, PetState.BYE, PetState.TIRED):
            key = "back"
        elif state in (
            PetState.IDLE2,
            PetState.IDLE3,
            PetState.THINKING,
            PetState.READING,
            PetState.WRITING,
            PetState.LISTENING,
            PetState.ANNOYED,
            PetState.PUFFED,
            PetState.REDHOT,
            PetState.COMPACT,
        ):
            key = "front"
        else:
            key = "standee"
        return self._standard_cache.get(key) or self._standard_fallback

    @staticmethod
    def _load_pixmap(path: str) -> Optional[QPixmap]:
        if not os.path.exists(path):
            return None
        pix = QPixmap(path)
        return None if pix.isNull() else pix
