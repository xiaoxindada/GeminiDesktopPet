"""GUI 运行期间的 Antigravity / Gemini 桌面端启动与退出轮询监听服务。"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Callable, Optional

from PySide6.QtCore import QTimer

from models.pet_settings import PetSettings
from services.followed_app_probe import AppActivitySnapshot, FollowedAppProbe


class AntigravityWatcherService:
    """基于 QTimer 每 1.5 秒监测 Antigravity 与 Gemini 桌面端启停事件。"""

    def __init__(self, interval_ms: int = 1500) -> None:
        self._timer = QTimer()
        self._timer.setInterval(interval_ms)
        self._timer.timeout.connect(self._poll)

        self._was_ag_running = False
        self._was_cli_running = False
        self._known_cli_pids: set[int] = set()
        self._last_cli_start_utc: Optional[datetime] = None
        self.on_antigravity_cli_started: Optional[Callable[[], None]] = None
        self._was_gemini_running = False
        self._known_ag_pids: set[int] = set()
        self._known_gemini_pids: set[int] = set()
        self._last_ag_start_utc: Optional[datetime] = None
        self._last_gemini_start_utc: Optional[datetime] = None
        self._initialized = False

        self.on_antigravity_started: Optional[Callable[[], None]] = None
        self.on_gemini_app_started: Optional[Callable[[], None]] = None

    @property
    def is_running(self) -> bool:
        return self._was_ag_running

    @property
    def is_antigravity_running(self) -> bool:
        return self._was_ag_running

    @property
    def is_gemini_app_running(self) -> bool:
        return self._was_gemini_running

    def start(self) -> None:
        ag = FollowedAppProbe.sample_antigravity_activity()
        cli = FollowedAppProbe.sample_antigravity_cli_activity()
        gem = FollowedAppProbe.sample_gemini_activity()
        now = datetime.now(timezone.utc)
        self._was_ag_running = ag.is_running
        self._was_gemini_running = gem.is_running
        self._known_ag_pids = set(ag.active_pids)
        self._known_gemini_pids = set(gem.active_pids)
        self._last_ag_start_utc = now if ag.is_running else None
        self._last_gemini_start_utc = now if gem.is_running else None
        self._was_cli_running = cli.is_running
        self._known_cli_pids = set(cli.active_pids)
        self._last_cli_start_utc = now if cli.is_running else None
        self._initialized = True
        self._timer.start()

    def stop(self) -> None:
        self._timer.stop()

    def dispose(self) -> None:
        self.stop()

    @staticmethod
    def detect_running_followed_app_key(settings: PetSettings) -> Optional[str]:
        return FollowedAppProbe.detect_running_followed_app_key(settings)

    @staticmethod
    def check_any_followed_target_running(settings: PetSettings) -> bool:
        return FollowedAppProbe.detect_running_followed_app_key(settings) is not None

    def _poll(self) -> None:
        if not self._initialized:
            return
        ag_snap: AppActivitySnapshot = FollowedAppProbe.sample_antigravity_activity()
        gem_snap: AppActivitySnapshot = FollowedAppProbe.sample_gemini_activity()
        now = datetime.now(timezone.utc)

        (
            ag_started,
            ag_exited,
            self._was_ag_running,
            self._known_ag_pids,
            self._last_ag_start_utc,
        ) = FollowedAppProbe.evaluate_transition(
            ag_snap, self._was_ag_running, self._known_ag_pids, self._last_ag_start_utc, now
        )
        (
            gem_started,
            gem_exited,
            self._was_gemini_running,
            self._known_gemini_pids,
            self._last_gemini_start_utc,
        ) = FollowedAppProbe.evaluate_transition(
            gem_snap, self._was_gemini_running, self._known_gemini_pids, self._last_gemini_start_utc, now
        )

        cli_snap = FollowedAppProbe.sample_antigravity_cli_activity()
        (
            cli_started,
            _,
            self._was_cli_running,
            self._known_cli_pids,
            self._last_cli_start_utc,
        ) = FollowedAppProbe.evaluate_transition(
            cli_snap, self._was_cli_running, self._known_cli_pids, self._last_cli_start_utc, now
        )
        if cli_started and self.on_antigravity_cli_started:
            self.on_antigravity_cli_started()

        if ag_started and self.on_antigravity_started:
            self.on_antigravity_started()

        if gem_started and self.on_gemini_app_started:
            self.on_gemini_app_started()
