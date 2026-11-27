"""GUI 运行期间的 Antigravity / Gemini 桌面端启动与退出后台轮询监听服务。"""

from __future__ import annotations

from datetime import datetime, timezone
import threading
from typing import Callable, Optional

from PySide6.QtCore import QObject, Qt, Signal

from services.followed_app_probe import AppActivitySnapshot, FollowedAppProbe

_DEFAULT_SAMPLE_AG = FollowedAppProbe.sample_antigravity_activity
_DEFAULT_SAMPLE_GEMINI = FollowedAppProbe.sample_gemini_activity
_DEFAULT_SAMPLE_CLI = FollowedAppProbe.sample_antigravity_cli_activity


class _WatcherSignalBridge(QObject):
    """将后台轮询线程检测到的应用启动事件安全转发回 Qt 主线程。"""

    antigravity_started = Signal()
    gemini_started = Signal()
    antigravity_cli_started = Signal()


class AntigravityWatcherService:
    """在后台线程每 1.5 秒监测 Antigravity 与 Gemini 桌面端启停，避免阻塞 GUI 渲染循环。"""

    def __init__(
        self,
        interval_ms: int = 1500,
        *,
        enabled_flags_provider: Optional[Callable[[], tuple[bool, bool, bool]]] = None,
    ) -> None:
        self._interval_sec = max(0.1, float(interval_ms) / 1000.0)
        self._enabled_flags_provider = enabled_flags_provider
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

        self._bridge = _WatcherSignalBridge()
        self._bridge.antigravity_started.connect(
            self._dispatch_antigravity_started, Qt.ConnectionType.QueuedConnection
        )
        self._bridge.gemini_started.connect(
            self._dispatch_gemini_started, Qt.ConnectionType.QueuedConnection
        )
        self._bridge.antigravity_cli_started.connect(
            self._dispatch_cli_started, Qt.ConnectionType.QueuedConnection
        )

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

    def _dispatch_antigravity_started(self) -> None:
        if self.on_antigravity_started:
            self.on_antigravity_started()

    def _dispatch_gemini_started(self) -> None:
        if self.on_gemini_app_started:
            self.on_gemini_app_started()

    def _dispatch_cli_started(self) -> None:
        if self.on_antigravity_cli_started:
            self.on_antigravity_cli_started()

    def _enabled_flags(self) -> tuple[bool, bool, bool]:
        if self._enabled_flags_provider is None:
            return (True, True, True)
        try:
            ag, gem, cli = self._enabled_flags_provider()
            return (bool(ag), bool(gem), bool(cli))
        except Exception:
            return (True, True, True)

    def _sample_snapshots(
        self,
    ) -> tuple[AppActivitySnapshot, AppActivitySnapshot, AppActivitySnapshot]:
        check_ag, check_gem, check_cli = self._enabled_flags()
        if not (check_ag or check_gem or check_cli):
            return (
                AppActivitySnapshot(False, set(), set()),
                AppActivitySnapshot(False, set(), set()),
                AppActivitySnapshot(False, set(), set()),
            )
        # 未被外部测试覆写单采样方法时走合并单次系统快照，将 5 次 Win32 枚举压缩为 2 次。
        use_batch = (
            FollowedAppProbe.sample_antigravity_activity is _DEFAULT_SAMPLE_AG
            and FollowedAppProbe.sample_gemini_activity is _DEFAULT_SAMPLE_GEMINI
            and FollowedAppProbe.sample_antigravity_cli_activity is _DEFAULT_SAMPLE_CLI
        )
        if use_batch:
            return FollowedAppProbe.sample_all_activities(
                check_antigravity=check_ag,
                check_gemini=check_gem,
                check_cli=check_cli,
            )
        return (
            FollowedAppProbe.sample_antigravity_activity()
            if check_ag
            else AppActivitySnapshot(False, set(), set()),
            FollowedAppProbe.sample_gemini_activity()
            if check_gem
            else AppActivitySnapshot(False, set(), set()),
            FollowedAppProbe.sample_antigravity_cli_activity()
            if check_cli
            else AppActivitySnapshot(False, set(), set()),
        )

    def start(self) -> None:
        ag, gem, cli = self._sample_snapshots()
        now = datetime.now(timezone.utc)
        with self._lock:
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

        self._stop_event.clear()
        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(
                target=self._background_loop,
                name="GeminiDesktopPetAppWatcher",
                daemon=True,
            )
            self._thread.start()

    def _background_loop(self) -> None:
        # 首次先等待一个完整轮询周期，与原 QTimer 行为一致且不干扰测试中紧跟 start() 的手动 _poll()。
        while not self._stop_event.wait(self._interval_sec):
            try:
                self._poll(emit_via_signal=True)
            except Exception:
                pass

    def stop(self) -> None:
        self._stop_event.set()
        thread = self._thread
        self._thread = None
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=0.35)

    def dispose(self) -> None:
        self.stop()

    def _poll(self, *, emit_via_signal: bool = False) -> None:
        if not self._initialized:
            return
        ag_snap, gem_snap, cli_snap = self._sample_snapshots()
        now = datetime.now(timezone.utc)

        with self._lock:
            if not self._initialized:
                return
            (
                ag_started,
                _,
                self._was_ag_running,
                self._known_ag_pids,
                self._last_ag_start_utc,
            ) = FollowedAppProbe.evaluate_transition(
                ag_snap, self._was_ag_running, self._known_ag_pids, self._last_ag_start_utc, now
            )
            (
                gem_started,
                _,
                self._was_gemini_running,
                self._known_gemini_pids,
                self._last_gemini_start_utc,
            ) = FollowedAppProbe.evaluate_transition(
                gem_snap, self._was_gemini_running, self._known_gemini_pids, self._last_gemini_start_utc, now
            )
            (
                cli_started,
                _,
                self._was_cli_running,
                self._known_cli_pids,
                self._last_cli_start_utc,
            ) = FollowedAppProbe.evaluate_transition(
                cli_snap, self._was_cli_running, self._known_cli_pids, self._last_cli_start_utc, now
            )

        if emit_via_signal:
            if self._stop_event.is_set():
                return
            if cli_started:
                self._bridge.antigravity_cli_started.emit()
            if ag_started:
                self._bridge.antigravity_started.emit()
            if gem_started:
                self._bridge.gemini_started.emit()
            return

        if cli_started and self.on_antigravity_cli_started:
            self.on_antigravity_cli_started()

        if ag_started and self.on_antigravity_started:
            self.on_antigravity_started()

        if gem_started and self.on_gemini_app_started:
            self.on_gemini_app_started()
