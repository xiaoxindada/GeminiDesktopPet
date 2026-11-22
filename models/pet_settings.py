"""Persistent pet settings and enums."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import json
import re
from typing import Optional


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

API_PROVIDER_GEMINI_DEVELOPER = "gemini_developer"
API_PROVIDER_AGENT_PLATFORM = "agent_platform"
DEFAULT_API_PROVIDER = API_PROVIDER_GEMINI_DEVELOPER

DEFAULT_AGENT_PLATFORM_LOCATION = "global"
AGENT_PLATFORM_LOCATION_PRESETS: tuple[str, ...] = (
    "global",
    "us-central1",
    "us-east4",
    "us-west1",
    "europe-west1",
    "europe-west4",
    "asia-northeast1",
    "asia-southeast1",
    "us",
    "eu",
)
DEFAULT_AGENT_PLATFORM_API_VERSION = "v1beta1"
AGENT_PLATFORM_API_VERSION_OPTIONS: tuple[tuple[str, str], ...] = (
    ("v1beta1", "v1beta1（默认 · 支持预览模型）"),
    ("v1", "v1（稳定版 GA）"),
)

_AGENT_PROJECT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_AGENT_LOCATION_RE = re.compile(r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$")

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


def normalize_api_provider(provider: str | None) -> str:
    """规范化 API 渠道标识，仅在官方 Gemini API 与 Agent Platform API 间切换。"""
    cleaned = (provider or "").strip().lower()
    if cleaned in (
        "agent_platform",
        "agent-platform",
        "agent",
        "enterprise",
        "vertex",
        "vertexai",
        "vertex_ai",
    ):
        return API_PROVIDER_AGENT_PLATFORM
    return API_PROVIDER_GEMINI_DEVELOPER


def format_api_provider_label(provider: str | None, *, short: bool = False) -> str:
    if normalize_api_provider(provider) == API_PROVIDER_AGENT_PLATFORM:
        return "Agent Platform" if short else "Agent Platform API"
    return "Gemini API"


def normalize_agent_platform_location(location: str | None) -> str:
    cleaned = (location or "").strip().lower()
    return cleaned or DEFAULT_AGENT_PLATFORM_LOCATION


def normalize_agent_platform_api_version(api_version: str | None) -> str:
    cleaned = (api_version or "").strip().lower()
    return "v1" if cleaned == "v1" else DEFAULT_AGENT_PLATFORM_API_VERSION


def validate_agent_platform_project(project: str | None) -> Optional[str]:
    """校验 Google Cloud Project ID 格式，留空表示使用 Express 模式。"""
    cleaned = (project or "").strip()
    if not cleaned:
        return None
    low = cleaned.lower()
    if low.endswith(".apps.googleusercontent.com"):
        return (
            "填写的是 OAuth Client ID（*.apps.googleusercontent.com），不是 Google Cloud Project ID；"
            "Express 模式 Key 请直接留空，项目模式请填项目 ID（如 my-project-123）或纯数字项目编号"
        )
    if low.endswith(".gserviceaccount.com") or "@" in cleaned:
        return (
            "填写的是完整服务账号邮箱，不是 Project ID；"
            "请仅截取 @ 与 .iam.gserviceaccount.com 之间的部分（Express 模式 Key 请直接留空）"
        )
    if cleaned.startswith(("AIza", "AQ.")):
        return "请不要将 API Key 填在 Project ID 栏；Express 模式 Key 请直接留空"
    if ".." in cleaned or not _AGENT_PROJECT_RE.match(cleaned):
        return "Project ID 格式无效，仅支持字母、数字及 . _ : -，不能包含空白或路径符号"
    return None


def validate_agent_platform_location(location: str | None) -> Optional[str]:
    """校验官方区域标识符，防止将主机/URL 片段注入到 aiplatform 域名。"""
    cleaned = (location or "").strip().lower()
    if not cleaned:
        return None
    if len(cleaned) < 2 or len(cleaned) > 40 or not _AGENT_LOCATION_RE.match(cleaned):
        return "区域 Location 格式无效，请填写如 global、us-central1、asia-northeast1 等官方区域名"
    return None


def parse_agent_platform_json_config(raw_text: str) -> tuple[str, Optional[str]]:
    """仅从官方 client_secret_*.json 提取 project_id（与可选 location），忽略全部 OAuth 凭据字段。"""
    data = json.loads(raw_text)
    if not isinstance(data, dict):
        raise ValueError("文件内容不是有效的 JSON 对象")

    raw_project = ""
    for container in (data, data.get("installed"), data.get("web")):
        if isinstance(container, dict):
            candidate = container.get("project_id")
            if isinstance(candidate, (str, int)) and not isinstance(candidate, bool):
                stripped = str(candidate).strip()
                if stripped:
                    raw_project = stripped
                    break

    if not raw_project:
        raise ValueError("JSON 中未找到 project_id 字段（请确认选择了官方 client_secret_*.json）")

    project_err = validate_agent_platform_project(raw_project)
    if project_err:
        raise ValueError(project_err)

    raw_location = data.get("location")
    parsed_location: Optional[str] = None
    if isinstance(raw_location, str) and raw_location.strip():
        if validate_agent_platform_location(raw_location) is None:
            parsed_location = normalize_agent_platform_location(raw_location)

    return raw_project, parsed_location


def strip_gemini_model_prefix(model_id: str | None) -> str:
    """移除官方模型 ID 前缀（models/ 或 publishers/google/models/）。"""
    cleaned = (model_id or "").strip()
    if "/publishers/google/models/" in cleaned:
        return cleaned.split("/publishers/google/models/", 1)[1]
    if cleaned.startswith("publishers/google/models/"):
        return cleaned[len("publishers/google/models/") :]
    if cleaned.startswith("models/"):
        return cleaned[len("models/") :]
    return cleaned


def format_model_display_name(model_id: str | None) -> str:
    """将动态获取的模型 ID 格式化为简洁可读名称（不写死静态清单，也不显示新/旧标签）。"""
    cleaned = strip_gemini_model_prefix(model_id) or DEFAULT_GEMINI_MODEL
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
    auto_walk: bool = True
    anti_gravity_mode: bool = False
    # 打盹模式：开启后原地睡觉待机，关闭时不会自行入睡
    doze_mode: bool = False
    gravity_enabled: bool = True
    edge_dock_enabled: bool = True
    sound_enabled: bool = True
    particles_enabled: bool = True
    system_monitor_enabled: bool = True
    show_speech_bubble: bool = True
    follow_antigravity: bool = True
    follow_antigravity_cli: bool = True
    follow_gemini_app: bool = True
    # 反重力对话播报默认开启，重启时恢复已保存的开关状态。
    chat_sync_enabled: bool = True


    last_left: float | None = None
    last_top: float | None = None
    # 用户拖拽缩放后的对话窗口记忆尺寸（None 表示按内容自动适配）
    chat_width: int | None = None
    chat_height: int | None = None
    docked_side: DockSide = DockSide.NONE

    total_pats: int = 0
    favorability: int = 20

    chat_offline_mode: bool = False
    api_provider: str = DEFAULT_API_PROVIDER
    gemini_api_key: str = ""
    agent_platform_api_key: str = ""
    agent_platform_project: str = ""
    agent_platform_location: str = DEFAULT_AGENT_PLATFORM_LOCATION
    agent_platform_api_version: str = DEFAULT_AGENT_PLATFORM_API_VERSION
    gemini_model: str = DEFAULT_GEMINI_MODEL
    thinking_level: str = DEFAULT_THINKING_LEVEL

    update_releases_url: str = "https://github.com/xiaoxindada/GeminiDesktopPet/releases"
    auto_check_updates: bool = True
    skipped_update_version: str = ""

    enable_runtime_log: bool = False
