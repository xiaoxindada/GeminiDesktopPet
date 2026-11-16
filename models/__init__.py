"""Data models and character lore for GeminiDesktopPet."""

from .app_version import (
    USER_CONFIG_DIR_NAME,
    AppVersion,
    get_app_base_dir,
    get_bundled_resource_dir,
    get_user_config_dir,
)
from .character_lore import (
    AI_TONE_OPTIONS,
    APPEARANCE_TRAITS,
    BUILTIN_PRESETS,
    COSTUME_OPTIONS,
    DEFAULT_LINES,
    AiTonePreset,
    CharacterLore,
    CharacterPreset,
    CostumeOption,
)
from .character_profile import CharacterProfile, CharacterTrait
from .pet_settings import (
    DEFAULT_GEMINI_MODEL,
    DEFAULT_THINKING_LEVEL,
    THINKING_LEVEL_LABELS,
    THINKING_LEVEL_OPTIONS,
    DockSide,
    PetSettings,
    PetStyleMode,
    format_model_display_name,
    format_thinking_level_label,
    get_supported_thinking_levels,
    normalize_gemini_model,
    normalize_thinking_level,
)
from .pet_state import PetState

__all__ = [
    "AI_TONE_OPTIONS",
    "APPEARANCE_TRAITS",
    "AiTonePreset",
    "AppVersion",
    "BUILTIN_PRESETS",
    "COSTUME_OPTIONS",
    "CharacterLore",
    "CharacterPreset",
    "CharacterProfile",
    "CharacterTrait",
    "CostumeOption",
    "DEFAULT_GEMINI_MODEL",
    "DEFAULT_LINES",
    "DEFAULT_THINKING_LEVEL",
    "DockSide",
    "PetSettings",
    "PetState",
    "PetStyleMode",
    "THINKING_LEVEL_LABELS",
    "THINKING_LEVEL_OPTIONS",
    "USER_CONFIG_DIR_NAME",
    "format_model_display_name",
    "format_thinking_level_label",
    "get_app_base_dir",
    "get_bundled_resource_dir",
    "get_supported_thinking_levels",
    "get_user_config_dir",
    "normalize_gemini_model",
    "normalize_thinking_level",
]
