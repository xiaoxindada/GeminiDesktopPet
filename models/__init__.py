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
    DockSide,
    PetSettings,
    PetStyleMode,
    normalize_gemini_model,
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
    "DockSide",
    "PetSettings",
    "PetState",
    "PetStyleMode",
    "USER_CONFIG_DIR_NAME",
    "get_app_base_dir",
    "get_bundled_resource_dir",
    "get_user_config_dir",
    "normalize_gemini_model",
]
