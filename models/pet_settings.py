"""Persistent pet settings and enums."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class DockSide(str, Enum):
    NONE = "None"
    LEFT = "Left"
    RIGHT = "Right"

    @classmethod
    def parse(cls, raw: str | None) -> DockSide:
        if raw:
            low = raw.strip().lower()
            for item in cls:
                if item.value.lower() == low:
                    return item
        return cls.NONE


class PetStyleMode(str, Enum):
    CHIBI = "Chibi"
    STANDARD = "Standard"

    @classmethod
    def parse(cls, raw: str | None) -> PetStyleMode:
        if raw:
            low = raw.strip().lower()
            for item in cls:
                if item.value.lower() == low:
                    return item
        return cls.CHIBI


DEFAULT_GEMINI_MODEL = "gemini-4-hydrogen"
DEPRECATED_MODEL_PREFIXES = ("gemini-2.5-", "gemini-2.0-", "gemini-1.5-", "gemini-1.0-")


def normalize_gemini_model(model: str | None) -> str:
    """Normalize configured model name and upgrade deprecated Gemini versions."""
    cleaned = (model or "").strip()
    if not cleaned:
        return DEFAULT_GEMINI_MODEL
    if cleaned.lower().startswith(DEPRECATED_MODEL_PREFIXES):
        return DEFAULT_GEMINI_MODEL
    return cleaned


@dataclass
class PetSettings:
    """Persists user configuration and bond progression for Gemini-chan."""

    style_mode: PetStyleMode = PetStyleMode.CHIBI
    scale: float = 1.0
    opacity: float = 1.0
    always_on_top: bool = True
    auto_walk: bool = True
    anti_gravity_mode: bool = False
    # 打盹模式：开启后原地睡觉待机，关闭时不会自行入睡
    doze_mode: bool = False
    gravity_enabled: bool = True
    edge_dock_enabled: bool = True
    sound_enabled: bool = True
    particles_enabled: bool = True
    click_through: bool = False
    system_monitor_enabled: bool = True
    show_speech_bubble: bool = False
    auto_festival_costume: bool = True
    follow_antigravity: bool = True
    follow_gemini_app: bool = True
    sleep_when_antigravity_exits: bool = False

    costume_override: str = "auto"

    last_left: float | None = None
    last_top: float | None = None
    # 用户拖拽缩放后的对话窗口记忆尺寸（None 表示按内容自动适配）
    chat_width: int | None = None
    chat_height: int | None = None
    docked_side: DockSide = DockSide.NONE

    total_pats: int = 0
    favorability: int = 20

    gemini_api_key: str = ""
    gemini_model: str = DEFAULT_GEMINI_MODEL

    update_releases_url: str = "https://github.com/xiaoxindada/GeminiDesktopPet/releases"
    auto_check_updates: bool = True
    skipped_update_version: str = ""

    enable_runtime_log: bool = False
