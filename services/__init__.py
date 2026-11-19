"""Services package for GeminiDesktopPet (lazy exports keep headless helpers lightweight)."""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from services.app_follow_service import AppFollowService, FollowWatcherOptions
    from services.app_logger import AppLogger
    from services.character_profile_service import CharacterProfileService
    from services.config_cleanup_service import CleanupReport, CleanupTargets, ConfigCleanupService
    from services.followed_app_probe import (
        AppActivitySnapshot,
        FollowedAppProbe,
        ProcessProbeInfo,
    )
    from services.gemini_chat_service import ChatReply, GeminiChatService
    from services.settings_service import SettingsService
    from services.sound_service import PetSoundEffect, SoundService
    from services.system_monitor_service import SystemMetrics, SystemMonitorService
    from services.update_service import (
        DEFAULT_RELEASES_URL,
        ReleaseAssetInfo,
        UpdateCheckResult,
        UpdateService,
    )

_EXPORTS: dict[str, tuple[str, str]] = {
    "AppActivitySnapshot": ("services.followed_app_probe", "AppActivitySnapshot"),
    "AppFollowService": ("services.app_follow_service", "AppFollowService"),
    "AppLogger": ("services.app_logger", "AppLogger"),
    "CharacterProfileService": ("services.character_profile_service", "CharacterProfileService"),
    "ChatReply": ("services.gemini_chat_service", "ChatReply"),
    "CleanupReport": ("services.config_cleanup_service", "CleanupReport"),
    "CleanupTargets": ("services.config_cleanup_service", "CleanupTargets"),
    "ConfigCleanupService": ("services.config_cleanup_service", "ConfigCleanupService"),
    "DEFAULT_RELEASES_URL": ("services.update_service", "DEFAULT_RELEASES_URL"),
    "FollowWatcherOptions": ("services.app_follow_service", "FollowWatcherOptions"),
    "FollowedAppProbe": ("services.followed_app_probe", "FollowedAppProbe"),
    "GeminiChatService": ("services.gemini_chat_service", "GeminiChatService"),
    "PetSoundEffect": ("services.sound_service", "PetSoundEffect"),
    "ProcessProbeInfo": ("services.followed_app_probe", "ProcessProbeInfo"),
    "ReleaseAssetInfo": ("services.update_service", "ReleaseAssetInfo"),
    "SettingsService": ("services.settings_service", "SettingsService"),
    "SoundService": ("services.sound_service", "SoundService"),
    "SystemMetrics": ("services.system_monitor_service", "SystemMetrics"),
    "SystemMonitorService": ("services.system_monitor_service", "SystemMonitorService"),
    "UpdateCheckResult": ("services.update_service", "UpdateCheckResult"),
    "UpdateService": ("services.update_service", "UpdateService"),
}

__all__ = list(_EXPORTS)


def __getattr__(name: str) -> Any:
    target = _EXPORTS.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module_name, attr_name = target
    value = getattr(importlib.import_module(module_name), attr_name)
    globals()[name] = value
    return value
