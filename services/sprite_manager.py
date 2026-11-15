"""立绘与骨骼部件精灵资源管理器（使用 PySide6 QPixmap 缓存）。"""

from __future__ import annotations

from dataclasses import dataclass
import os
from typing import Optional

from PySide6.QtGui import QPixmap

from models.app_version import get_bundled_resource_dir
from models.pet_settings import PetStyleMode
from models.pet_state import PetState


@dataclass(frozen=True)
class WalkRigAssets:
    chibi_body: Optional[QPixmap] = None
    chibi_arm_l: Optional[QPixmap] = None
    chibi_arm_r: Optional[QPixmap] = None
    chibi_leg_l: Optional[QPixmap] = None
    chibi_leg_l_back: Optional[QPixmap] = None
    chibi_leg_r: Optional[QPixmap] = None
    chibi_leg_r_back: Optional[QPixmap] = None
    std_body: Optional[QPixmap] = None
    std_front_thigh: Optional[QPixmap] = None
    std_front_shin: Optional[QPixmap] = None
    std_back_thigh: Optional[QPixmap] = None
    std_back_shin: Optional[QPixmap] = None


class SpriteManager:
    """加载并缓存全部 55 态精灵贴图及 Q 版/标准版分离行走骨骼图。"""

    def __init__(self, assets_root: Optional[str] = None) -> None:
        base = assets_root or os.path.join(get_bundled_resource_dir(), "Assets")
        self._sprites_dir = os.path.join(base, "Sprites")
        self._standard_dir = os.path.join(base, "Standard")
        self._walkrig_dir = os.path.join(base, "WalkRig")
        self._cache: dict[PetState, QPixmap] = {}
        self._key_cache: dict[str, QPixmap] = {}
        self._standard_cache: dict[str, QPixmap] = {}
        self._fallback: Optional[QPixmap] = None
        self._standard_fallback: Optional[QPixmap] = None
        self._avatar: Optional[QPixmap] = None
        self.walk_rig = WalkRigAssets()

    def preload_all(self) -> None:
        self._fallback = self._load_pixmap(os.path.join(self._sprites_dir, "idle.png"))
        for state in PetState:
            key = state.to_sprite_key()
            pix = self._load_pixmap(os.path.join(self._sprites_dir, f"{key}.png"))
            if pix is not None:
                self._cache[state] = pix
                self._key_cache[key.lower()] = pix
            elif self._fallback is not None:
                self._cache[state] = self._fallback

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

        avatar = self._load_pixmap(os.path.join(self._sprites_dir, "avatar.png"))
        self._avatar = avatar or self._fallback

        self.walk_rig = WalkRigAssets(
            chibi_body=self._load_pixmap(os.path.join(self._walkrig_dir, "chibi_body.png")),
            chibi_arm_l=self._load_pixmap(os.path.join(self._walkrig_dir, "chibi_arm_l.png")),
            chibi_arm_r=self._load_pixmap(os.path.join(self._walkrig_dir, "chibi_arm_r.png")),
            chibi_leg_l=self._load_pixmap(os.path.join(self._walkrig_dir, "chibi_leg_l.png")),
            chibi_leg_l_back=self._load_pixmap(os.path.join(self._walkrig_dir, "chibi_leg_l_back.png")),
            chibi_leg_r=self._load_pixmap(os.path.join(self._walkrig_dir, "chibi_leg_r.png")),
            chibi_leg_r_back=self._load_pixmap(os.path.join(self._walkrig_dir, "chibi_leg_r_back.png")),
            std_body=self._load_pixmap(os.path.join(self._walkrig_dir, "std_body.png")),
            std_front_thigh=self._load_pixmap(os.path.join(self._walkrig_dir, "std_thigh.png")),
            std_front_shin=self._load_pixmap(os.path.join(self._walkrig_dir, "std_shin.png")),
            std_back_thigh=self._load_pixmap(os.path.join(self._walkrig_dir, "std_thigh_back.png")),
            std_back_shin=self._load_pixmap(os.path.join(self._walkrig_dir, "std_shin_back.png")),
        )

    def get(self, state: PetState, style_mode: PetStyleMode = PetStyleMode.CHIBI) -> Optional[QPixmap]:
        if style_mode == PetStyleMode.STANDARD:
            std_pix = self._get_standard_for_state(state)
            if std_pix is not None:
                return std_pix
        pix = self._cache.get(state)
        if pix is not None:
            return pix
        return self._fallback

    def get_by_key(self, key: str, style_mode: PetStyleMode = PetStyleMode.CHIBI) -> Optional[QPixmap]:
        parsed = PetState.from_sprite_key(key)
        if parsed is not None:
            return self.get(parsed, style_mode)
        low = key.strip().lower()
        if low in self._key_cache:
            return self._key_cache[low]
        pix = self._load_pixmap(os.path.join(self._sprites_dir, f"{low}.png"))
        if pix is not None:
            self._key_cache[low] = pix
            return pix
        return self._fallback

    def _get_standard_for_state(self, state: PetState) -> Optional[QPixmap]:
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
        elif state in (PetState.SULK, PetState.SLEEPING, PetState.BYE, PetState.TIRED):
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

    def get_avatar(self) -> Optional[QPixmap]:
        return self._avatar or self._fallback

    @staticmethod
    def _load_pixmap(path: str) -> Optional[QPixmap]:
        if not os.path.exists(path):
            return None
        pix = QPixmap(path)
        return None if pix.isNull() else pix
