"""PySide6 UI windows for GeminiDesktopPet."""

from views.character_editor_window import CharacterEditorWindow
from views.oauth_login_dialog import OAuthLoginDialog
from views.pet_window import PetWindow
from views.profile_window import ProfileWindow
from views.quota_window import QuotaWindow
from views.settings_window import SettingsWindow
from views.update_window import UpdateWindow

__all__ = [
    "CharacterEditorWindow",
    "OAuthLoginDialog",
    "PetWindow",
    "ProfileWindow",
    "QuotaWindow",
    "SettingsWindow",
    "UpdateWindow",
]
