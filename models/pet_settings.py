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


DEFAULT_GEMINI_MODEL = "gemini-3.1-flash-lite"
DEFAULT_THINKING_LEVEL = "auto"

DEPRECATED_MODEL_PREFIXES = ("gemini-2.0-", "gemini-1.5-", "gemini-1.0-")
LEGACY_GEMINI_DEFAULT_MODELS = ("gemini-4-hydrogen",)

THINKING_LEVEL_OPTIONS: tuple[tuple[str, str], ...] = (
    ("auto", "自动"),
    ("off", "Off"),
    ("minimal", "Minimal"),
    ("low", "Low"),
    ("medium", "Medium"),
    ("high", "High"),
    ("xhigh", "Extra"),
)
THINKING_LEVEL_LABELS: dict[str, str] = dict(THINKING_LEVEL_OPTIONS)


def format_model_display_name(model_id: str | None) -> str:
    """将动态获取的模型 ID 格式化为简洁可读名称（不写死静态清单，也不显示新/旧标签）。"""
    cleaned = (model_id or "").strip() or DEFAULT_GEMINI_MODEL
    if cleaned.startswith("models/"):
        cleaned = cleaned[len("models/") :]
    low = cleaned.lower()

    if low == "gemini-pro-orchestrator-g1-pro":
        return "Orchestrator (Argon)"
    if low == "gpt-oss-120b-medium":
        return "GPT-OSS 120B (Medium)"

    parts = cleaned.split("-")
    # 合并 claude-opus-5-5 -> Claude Opus 5.5
    if low.startswith("claude-") and len(parts) >= 4 and parts[-1].isdigit() and parts[-2].isdigit():
        ver = f"{parts[-2]}.{parts[-1]}"
        body = " ".join(p.capitalize() for p in parts[:-2])
        return f"{body} {ver}"

    formatted: list[str] = []
    for p in parts:
        pl = p.lower()
        if pl == "preview":
            formatted.append("(Preview)")
        elif pl == "it":
            formatted.append("IT")
        elif pl == "er":
            formatted.append("ER")
        elif pl == "customtools":
            formatted.append("CustomTools")
        elif pl == "flash":
            formatted.append("Flash")
        elif pl == "lite":
            if formatted and formatted[-1] == "Flash":
                formatted[-1] = "Flash-Lite"
            else:
                formatted.append("Lite")
        elif pl == "gpt":
            formatted.append("GPT")
        elif pl == "oss":
            formatted.append("OSS")
        elif pl.endswith("b") and pl[:-1].isdigit():
            formatted.append(p.upper())
        elif any(ch.isdigit() for ch in p):
            formatted.append(p.upper() if len(p) <= 3 and p[0].isalpha() else p)
        else:
            formatted.append(p.capitalize())
    return " ".join(formatted)


def normalize_thinking_level(level: str | None) -> str:
    """规范化思考强度枚举值，兼容 extra/default 等别名。"""
    cleaned = (level or "").strip().lower()
    if not cleaned or cleaned in ("auto", "default", "unspecified", "thinking_level_unspecified"):
        return "auto"
    if cleaned in ("extra", "xhigh", "max"):
        return "xhigh"
    if cleaned in ("off", "none", "disabled", "0"):
        return "off"
    if cleaned in ("minimal", "min"):
        return "minimal"
    if cleaned in ("low", "medium", "high"):
        return cleaned
    return "auto"


def format_thinking_level_label(level: str | None) -> str:
    normalized = normalize_thinking_level(level)
    return THINKING_LEVEL_LABELS.get(normalized, "自动")


def resolve_effective_thinking_level(model_id: str | None, level: str | None) -> str:
    """返回当前模型可用的思考档位，未支持的档位回落到该模型的默认档。"""
    normalized = normalize_thinking_level(level)
    supported = get_supported_thinking_levels(model_id)
    return normalized if normalized in supported else supported[0]


def get_supported_thinking_levels(model_id: str | None) -> tuple[str, ...]:
    """根据模型系列返回支持的思考强度档位（兼容官方公开模型与动态发现模型）。"""
    low = (model_id or "").strip().lower()
    if not low:
        return ("auto", "low", "medium", "high")
    if low.startswith(("gemini-4-hydrogen", "gemini-4-carbon")):
        return ("auto", "low", "medium", "high", "xhigh")
    if low.startswith(("gemini-4-argon", "gemini-3.8-flash", "claude-")):
        return ("auto", "low", "medium", "high")
    if low.startswith(
        (
            "gemini-3.7-flash",
            "gemini-3.6-flash",
            "gemini-3.5-flash",
            "gemini-3-flash-preview",
            "gemini-robotics-er-2",
            "gemma-4",
        )
    ):
        return ("auto", "minimal", "low", "medium", "high")
    if low.startswith("gemini-3-pro-preview"):
        return ("auto", "low", "high")
    if low.startswith(("gemini-3.1-pro", "gemini-3.1-flash-lite")):
        return ("auto", "low", "medium", "high")
    if low.startswith(("gemini-2.5-flash", "gemini-flash-latest", "gemini-flash-lite-latest")):
        return ("auto", "off", "low", "medium", "high")
    if low.startswith(("gemini-2.5-pro", "gemini-pro-latest")):
        return ("auto", "low", "medium", "high")
    if low.startswith(("gemini-pro-orchestrator", "gpt-oss-")):
        return ("auto",)
    return ("auto", "minimal", "low", "medium", "high", "xhigh")


def normalize_gemini_model(model: str | None) -> str:
    """Normalize API Key model name and upgrade deprecated or non-API default values."""
    cleaned = (model or "").strip()
    if not cleaned:
        return DEFAULT_GEMINI_MODEL
    lowered = cleaned.lower()
    if lowered.startswith(DEPRECATED_MODEL_PREFIXES) or lowered in LEGACY_GEMINI_DEFAULT_MODELS:
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
    system_monitor_enabled: bool = True
    show_speech_bubble: bool = False
    follow_antigravity: bool = True
    follow_gemini_app: bool = True
    # 反重力对话播报：记住上次的开关状态，重启桌宠后自动恢复
    chat_sync_enabled: bool = False


    last_left: float | None = None
    last_top: float | None = None
    # 用户拖拽缩放后的对话窗口记忆尺寸（None 表示按内容自动适配）
    chat_width: int | None = None
    chat_height: int | None = None
    docked_side: DockSide = DockSide.NONE

    total_pats: int = 0
    favorability: int = 20

    chat_offline_mode: bool = False
    gemini_api_key: str = ""
    gemini_model: str = DEFAULT_GEMINI_MODEL
    thinking_level: str = DEFAULT_THINKING_LEVEL

    update_releases_url: str = "https://github.com/xiaoxindada/GeminiDesktopPet/releases"
    auto_check_updates: bool = True
    skipped_update_version: str = ""

    enable_runtime_log: bool = False
