"""Services package for GeminiDesktopPet (lazy-safe exports where Qt is optional)."""

from services.app_follow_service import AppFollowService, FollowWatcherOptions
from services.app_logger import AppLogger
from services.character_profile_service import CharacterProfileService
from services.followed_app_probe import (
    AppActivitySnapshot,
    FollowedAppProbe,
    ProcessProbeInfo,
)
from services.gemini_chat_service import ChatReply, GeminiChatService
from services.oauth_auth_service import (
    OAuthAuthService,
    OAuthClientConfig,
    OAuthError,
    OAuthLoopbackFlow,
    OAuthSession,
    mask_email,
    parse_oauth_client_json,
)
from services.settings_service import SettingsService
from services.config_cleanup_service import CleanupReport, CleanupTargets, ConfigCleanupService
from services.sound_service import PetSoundEffect, SoundService
from services.system_monitor_service import SystemMetrics, SystemMonitorService
from services.update_service import (
    DEFAULT_RELEASES_URL,
    ReleaseAssetInfo,
    UpdateCheckResult,
    UpdateService,
)

__all__ = [
    "AppActivitySnapshot",
    "AppFollowService",
    "AppLogger",
    "CharacterProfileService",
    "ChatReply",
    "CleanupReport",
    "CleanupTargets",
    "ConfigCleanupService",
    "DEFAULT_RELEASES_URL",
    "FollowWatcherOptions",
    "FollowedAppProbe",
    "GeminiChatService",
    "OAuthAuthService",
    "OAuthClientConfig",
    "OAuthError",
    "OAuthLoopbackFlow",
    "OAuthSession",
    "parse_oauth_client_json",
    "PetSoundEffect",
    "ProcessProbeInfo",
    "ReleaseAssetInfo",
    "SettingsService",
    "SoundService",
    "SystemMetrics",
    "SystemMonitorService",
    "UpdateCheckResult",
    "UpdateService",
    "mask_email",
]
