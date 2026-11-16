"""配置（user_config.json、内嵌 Gemini API Key）及 Windows 启动目录快捷方式管理。"""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
from typing import Any, Callable, Optional

from models.app_version import (
    USER_CONFIG_DIR_NAME,
    get_app_base_dir,
    get_bundled_resource_dir,
    get_user_config_dir,
)
from models.pet_settings import (
    DEFAULT_GEMINI_MODEL,
    DEFAULT_OAUTH_MODEL,
    DEFAULT_THINKING_LEVEL,
    ChatAuthMode,
    DockSide,
    PetSettings,
    PetStyleMode,
    normalize_gemini_model,
    normalize_oauth_model,
    normalize_thinking_level,
)
from services.win32_ipc import (
    get_windows_startup_dir,
    is_startup_entry_enabled,
    set_startup_entry_enabled,
)

STARTUP_SHORTCUT_NAME = "GeminiDesktopPet.lnk"


def _parse_bool(raw: Any, current: bool) -> bool:
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, (int, float)):
        return bool(raw)
    if isinstance(raw, str):
        low = raw.strip().lower()
        if low in ("true", "1", "yes", "on"):
            return True
        if low in ("false", "0", "no", "off"):
            return False
    return current


def _parse_optional_float(raw: Any, _current: Optional[float]) -> Optional[float]:
    if raw is None:
        return None
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        return round(float(raw), 2)
    if isinstance(raw, str):
        stripped = raw.strip()
        if not stripped:
            return None
        try:
            return round(float(stripped), 2)
        except ValueError:
            return None
    return None


def _make_clamped_float(min_val: float, max_val: float) -> Callable[[Any, float], float]:
    def _parser(raw: Any, current: float) -> float:
        if raw is None or isinstance(raw, bool):
            return current
        try:
            text = raw.strip() if isinstance(raw, str) else raw
            if text == "":
                return current
            return round(max(min_val, min(max_val, float(text))), 2)
        except (TypeError, ValueError):
            return current

    return _parser


def _parse_nonneg_int(raw: Any, current: int) -> int:
    if raw is None or isinstance(raw, bool):
        return current
    try:
        text = raw.strip() if isinstance(raw, str) else raw
        if text == "":
            return current
        return max(0, int(text))
    except (TypeError, ValueError):
        return current


def _make_optional_clamped_int(min_val: int, max_val: int) -> Callable[[Any, Optional[int]], Optional[int]]:
    def _parser(raw: Any, _current: Optional[int]) -> Optional[int]:
        if raw is None or isinstance(raw, bool):
            return None
        try:
            text = raw.strip() if isinstance(raw, str) else raw
            if text == "":
                return None
            return max(min_val, min(max_val, int(round(float(text)))))
        except (TypeError, ValueError):
            return None

    return _parser


def _parse_nonempty_str(raw: Any, current: str) -> str:
    if raw is None:
        return current
    stripped = str(raw).strip()
    return stripped if stripped else current


@dataclass(frozen=True)
class _SettingFieldSpec:
    """声明式配置映射：统一描述 user_config.json 分组键、属性转换器与可选输入别名。"""

    attr: str
    section: str
    json_key: str
    parse_value: Callable[[Any, Any], Any]
    serialize_value: Callable[[Any], Any]
    aliases: tuple[str, ...] = ()


_SETTINGS_SCHEMA: tuple[_SettingFieldSpec, ...] = (
    _SettingFieldSpec(
        "style_mode",
        "appearance",
        "style_mode",
        lambda raw, _cur: PetStyleMode.parse(str(raw)) if raw is not None else _cur,
        lambda v: v.value,
    ),
    _SettingFieldSpec(
        "scale",
        "appearance",
        "scale",
        _make_clamped_float(0.45, 2.2),
        lambda v: round(float(v), 2),
    ),
    _SettingFieldSpec(
        "opacity",
        "appearance",
        "opacity",
        _make_clamped_float(0.35, 1.0),
        lambda v: round(float(v), 2),
    ),
    _SettingFieldSpec("always_on_top", "appearance", "always_on_top", _parse_bool, bool),
    _SettingFieldSpec("show_speech_bubble", "appearance", "show_speech_bubble", _parse_bool, bool),
    _SettingFieldSpec("auto_walk", "behavior", "auto_walk", _parse_bool, bool),
    _SettingFieldSpec("anti_gravity_mode", "behavior", "anti_gravity_mode", _parse_bool, bool),
    _SettingFieldSpec("doze_mode", "behavior", "doze_mode", _parse_bool, bool),
    _SettingFieldSpec("gravity_enabled", "behavior", "gravity_enabled", _parse_bool, bool),
    _SettingFieldSpec("edge_dock_enabled", "behavior", "edge_dock_enabled", _parse_bool, bool),
    _SettingFieldSpec("sound_enabled", "behavior", "sound_enabled", _parse_bool, bool),
    _SettingFieldSpec("particles_enabled", "behavior", "particles_enabled", _parse_bool, bool),
    _SettingFieldSpec("click_through", "behavior", "click_through", _parse_bool, bool),
    _SettingFieldSpec("system_monitor_enabled", "behavior", "system_monitor_enabled", _parse_bool, bool),
    _SettingFieldSpec("follow_antigravity", "behavior", "follow_antigravity", _parse_bool, bool),
    _SettingFieldSpec("follow_gemini_app", "behavior", "follow_gemini_app", _parse_bool, bool),
    _SettingFieldSpec("auto_festival_costume", "costume", "auto_festival_costume", _parse_bool, bool),
    _SettingFieldSpec(
        "costume_override", "costume", "costume_override", _parse_nonempty_str, lambda v: v or "auto"
    ),
    _SettingFieldSpec(
        "last_left",
        "window",
        "last_left",
        _parse_optional_float,
        lambda v: round(float(v), 2) if v is not None else None,
    ),
    _SettingFieldSpec(
        "last_top",
        "window",
        "last_top",
        _parse_optional_float,
        lambda v: round(float(v), 2) if v is not None else None,
    ),
    _SettingFieldSpec(
        "chat_width",
        "window",
        "chat_width",
        _make_optional_clamped_int(280, 760),
        lambda v: int(v) if v is not None else None,
    ),
    _SettingFieldSpec(
        "chat_height",
        "window",
        "chat_height",
        _make_optional_clamped_int(136, 620),
        lambda v: int(v) if v is not None else None,
    ),
    _SettingFieldSpec(
        "docked_side",
        "window",
        "docked_side",
        lambda raw, _cur: DockSide.parse(str(raw)) if raw is not None else _cur,
        lambda v: v.value,
    ),
    _SettingFieldSpec("total_pats", "stats", "total_pats", _parse_nonneg_int, int),
    _SettingFieldSpec("favorability", "stats", "favorability", _parse_nonneg_int, int),
    _SettingFieldSpec(
        "chat_auth_mode",
        "gemini",
        "auth_mode",
        lambda raw, _cur: ChatAuthMode.parse(str(raw)) if raw is not None else _cur,
        lambda v: v.value,
    ),
    _SettingFieldSpec(
        "gemini_api_key",
        "gemini",
        "api_key",
        lambda raw, _cur: "" if raw is None else str(raw).strip(),
        lambda v: (v or "").strip(),
        ("ai_key",),
    ),
    _SettingFieldSpec(
        "gemini_model",
        "gemini",
        "model",
        lambda raw, _cur: normalize_gemini_model(str(raw)) if raw is not None else _cur,
        lambda v: v or DEFAULT_GEMINI_MODEL,
    ),
    _SettingFieldSpec(
        "oauth_model",
        "gemini",
        "oauth_model",
        lambda raw, _cur: normalize_oauth_model(str(raw)) if raw is not None else _cur,
        lambda v: v or DEFAULT_OAUTH_MODEL,
    ),
    _SettingFieldSpec(
        "thinking_level",
        "gemini",
        "thinking_level",
        lambda raw, _cur: normalize_thinking_level(str(raw)) if raw is not None else _cur,
        lambda v: normalize_thinking_level(v) or DEFAULT_THINKING_LEVEL,
    ),
    _SettingFieldSpec(
        "update_releases_url", "updates", "releases_url", _parse_nonempty_str, lambda v: v or ""
    ),
    _SettingFieldSpec("auto_check_updates", "updates", "auto_check", _parse_bool, bool),
    _SettingFieldSpec(
        "skipped_update_version",
        "updates",
        "skipped_version",
        lambda raw, _cur: str(raw).strip() if raw is not None else "",
        lambda v: v or "",
    ),
    _SettingFieldSpec("enable_runtime_log", "logging", "enable_runtime_log", _parse_bool, bool),
)


class SettingsService:
    """基于标准库 json 在 ~/.GeminiDesktopPet 持久化读写 user_config.json（含 gemini.api_key）。"""

    def __init__(self, custom_settings_folder: Optional[str | os.PathLike[str]] = None) -> None:
        folder = Path(custom_settings_folder) if custom_settings_folder else get_user_config_dir()
        self.settings_folder: Path = folder
        self.settings_path: Path = folder / "user_config.json"

    def _load_json_into(self, json_path: Path, settings: PetSettings) -> bool:
        raw_obj = json.loads(json_path.read_text(encoding="utf-8-sig"))
        if not isinstance(raw_obj, dict):
            return False
        self._apply_json_dict(raw_obj, settings)
        return True

    def load(self) -> PetSettings:
        settings = PetSettings()
        if self.settings_path.is_file():
            try:
                self._load_json_into(self.settings_path, settings)
            except Exception:
                pass
        settings.gemini_model = normalize_gemini_model(settings.gemini_model)
        settings.oauth_model = normalize_oauth_model(settings.oauth_model)
        settings.thinking_level = normalize_thinking_level(settings.thinking_level)
        return settings

    def save(self, settings: PetSettings) -> None:
        self.settings_folder.mkdir(parents=True, exist_ok=True)
        settings.gemini_model = normalize_gemini_model(settings.gemini_model)
        settings.oauth_model = normalize_oauth_model(settings.oauth_model)
        settings.thinking_level = normalize_thinking_level(settings.thinking_level)
        settings.gemini_api_key = (settings.gemini_api_key or "").strip()
        self.settings_path.write_text(self._serialize_json(settings), encoding="utf-8")

    @staticmethod
    def _serialize_json(s: PetSettings) -> str:
        payload: dict[str, dict[str, Any]] = {}
        for spec in _SETTINGS_SCHEMA:
            sec_dict = payload.setdefault(spec.section, {})
            sec_dict[spec.json_key] = spec.serialize_value(getattr(s, spec.attr))
        return json.dumps(payload, ensure_ascii=False, indent=2) + "\n"

    @staticmethod
    def _apply_json_dict(raw_obj: dict[str, Any], s: PetSettings) -> None:
        top_ci = {str(k).strip().lower(): v for k, v in raw_obj.items()}
        nested_ci: dict[str, dict[str, Any]] = {}
        for k, v in raw_obj.items():
            if isinstance(v, dict):
                nested_ci[str(k).strip().lower()] = {
                    str(sk).strip().lower(): sv for sk, sv in v.items()
                }

        for spec in _SETTINGS_SCHEMA:
            candidate_found = False
            candidate_val: Any = None

            sec_map = nested_ci.get(spec.section.lower())
            if sec_map is not None:
                for key_alias in (spec.json_key, spec.attr, *spec.aliases):
                    if key_alias and key_alias.lower() in sec_map:
                        candidate_val = sec_map[key_alias.lower()]
                        candidate_found = True
                        break

            if not candidate_found:
                for flat_alias in (spec.attr, spec.json_key, *spec.aliases):
                    if flat_alias and flat_alias.lower() in top_ci:
                        val = top_ci[flat_alias.lower()]
                        if not isinstance(val, dict):
                            candidate_val = val
                            candidate_found = True
                            break

            if candidate_found:
                try:
                    setattr(s, spec.attr, spec.parse_value(candidate_val, getattr(s, spec.attr)))
                except Exception:
                    pass

    @classmethod
    def get_startup_shortcut_path(cls) -> Path:
        return get_windows_startup_dir() / STARTUP_SHORTCUT_NAME

    @classmethod
    def is_startup_enabled(cls) -> bool:
        return is_startup_entry_enabled(STARTUP_SHORTCUT_NAME)

    @classmethod
    def set_startup_enabled(cls, enable: bool) -> bool:
        icon_candidate = Path(get_bundled_resource_dir()) / "Assets" / "AppIcon.ico"
        return set_startup_entry_enabled(
            STARTUP_SHORTCUT_NAME,
            enable,
            description="Gemini 娘桌面萌宠",
            icon_path=str(icon_candidate) if icon_candidate.is_file() else None,
            working_dir=get_app_base_dir(),
        )
