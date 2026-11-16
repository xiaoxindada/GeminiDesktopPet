"""PySide6 GUI 主协调器：单实例互斥、唤醒事件监听、托盘菜单与六大窗口协调。"""

from __future__ import annotations

import os
import sys
import threading
from typing import Optional

from PySide6.QtCore import QObject, QThread, QTimer, Qt, Signal, Slot
from PySide6.QtWidgets import QApplication

from models import (
    CharacterLore,
    CharacterProfile,
    PetSettings,
    PetState,
    PetStyleMode,
)
from services import (
    AppFollowService,
    AppLogger,
    CharacterProfileService,
    GeminiChatService,
    SettingsService,
    SoundService,
    SystemMonitorService,
    UpdateCheckResult,
    UpdateService,
)
from services.antigravity_quota_service import AntigravityQuotaService
from services.antigravity_watcher_service import AntigravityWatcherService
from services.sprite_manager import SpriteManager
from services.tray_icon_service import TrayIconService
from services.win32_ipc import (
    bring_window_to_front,
    CLOSE_EVENT_NAME,
    MAIN_MUTEX_NAME,
    WAKE_AG_EVENT_NAME,
    WAKE_EVENT_NAME,
    WAKE_GEMINI_EVENT_NAME,
    Win32Event,
    Win32Mutex,
    ensure_app_user_model_id,
    wait_any_events,
)
from themes import STARLIGHT_MENU_QSS
from views import (
    CharacterEditorWindow,
    PetWindow,
    ProfileWindow,
    QuotaWindow,
    SettingsWindow,
    UpdateWindow,
)


class _IpcSignalBridge(QObject):
    """将后台 Win32 命名事件监听线程的信号安全切回 Qt 主线程。"""

    wake_requested = Signal(str)
    close_requested = Signal()


class _SilentUpdateWorker(QThread):
    """启动 4 秒后静默检查 GitHub Releases 最新版本。"""

    checked = Signal(object)

    def __init__(
        self, update_service: UpdateService, releases_url: str, parent: Optional[QObject] = None
    ) -> None:
        super().__init__(parent)
        self._update_service = update_service
        self._releases_url = releases_url

    def run(self) -> None:
        try:
            res = self._update_service.check_for_update(self._releases_url)
            self.checked.emit(res)
        except Exception as exc:
            AppLogger.warn("Update", f"Silent startup update check failed: {exc}")


class PetApplicationCoordinator(QObject):
    """应用生命周期协调器，统领桌宠主窗体、守护服务、托盘与子窗口。"""

    def __init__(
        self,
        qt_app: QApplication,
        args: list[str],
        main_mutex: Win32Mutex,
    ) -> None:
        super().__init__()
        self._app = qt_app
        self._args = args
        self._main_mutex = main_mutex
        self._is_exiting = False
        self._ipc_stop_flag = threading.Event()
        self._ipc_stop_event = Win32Event(None, manual_reset=True, initial_state=False)
        self._ipc_thread: Optional[threading.Thread] = None
        self._latest_update_result: Optional[UpdateCheckResult] = None
        self._silent_update_worker: Optional[_SilentUpdateWorker] = None

        # 解析启动模式参数
        low_args = [a.lower() for a in args]
        self._woken_by: Optional[str] = None
        for a in low_args:
            if a.startswith("--woken-by:"):
                self._woken_by = a.split(":", 1)[1].strip() or None
                break

        # 初始化核心配置与服务
        self._settings_service = SettingsService()
        self._settings: PetSettings = self._settings_service.load()
        AppLogger.initialize(self._settings.enable_runtime_log)
        AppLogger.info("App", f"GUI starting. args={args}")
        UpdateService.cleanup_old_staging_folders()

        self._profile_service = CharacterProfileService(self._settings_service.settings_folder)
        CharacterLore.set_active_profile(self._profile_service.load())

        self._follow_service = AppFollowService(self._settings_service)
        self._follow_service.sync_and_apply(self._settings)
        self._follow_service.record_main_pid(os.getpid())

        # IPC 跨进程事件桥接优先初始化（显式 QueuedConnection 保证跨线程安全切回主 GUI 线程）
        self._ipc_bridge = _IpcSignalBridge(self)
        self._ipc_bridge.wake_requested.connect(
            self._on_external_wake, Qt.ConnectionType.QueuedConnection
        )
        self._ipc_bridge.close_requested.connect(
            self._on_external_close, Qt.ConnectionType.QueuedConnection
        )
        self._start_ipc_listener()

        self._sprites = SpriteManager()
        self._sprites.preload_all()

        self._sound = SoundService()
        self._sound.enabled = self._settings.sound_enabled
        self._sys_monitor = SystemMonitorService()
        self._chat = GeminiChatService()
        self._quota_service = AntigravityQuotaService()
        self._update_service = UpdateService()
        self._watcher = AntigravityWatcherService()

        # 子窗口引用缓存
        self._quota_win: Optional[QuotaWindow] = None
        self._profile_win: Optional[ProfileWindow] = None
        self._editor_win: Optional[CharacterEditorWindow] = None
        self._settings_win: Optional[SettingsWindow] = None
        self._update_win: Optional[UpdateWindow] = None

        # 创建主桌宠窗口与系统托盘
        self._pet = PetWindow(
            settings=self._settings,
            settings_service=self._settings_service,
            sprites=self._sprites,
            sound=self._sound,
            sys_monitor=self._sys_monitor,
            chat=self._chat,
        )
        self._tray = TrayIconService(parent=self, menu_qss=STARLIGHT_MENU_QSS)
        self._tray.sync_state(self._settings)

        self._wire_signals()
        self._start_watcher()

        # 若由联动监听唤醒则播放欢迎提示，否则直接显示主桌宠窗口
        if self._woken_by:
            self._show_pet_with_wake_greeting(self._woken_by)
        else:
            self._pet.show()

        # 启动后 4 秒静默检查 GitHub Releases 更新
        QTimer.singleShot(4000, self._start_silent_update_check)

    def _wire_signals(self) -> None:
        # PetWindow signals
        self._pet.open_profile_requested.connect(self.open_profile_window)
        self._pet.open_character_editor_requested.connect(self.open_character_editor_window)
        self._pet.open_settings_requested.connect(self.open_settings_window)
        self._pet.open_quota_requested.connect(self.toggle_quota_window)
        self._pet.open_update_requested.connect(self.open_update_window)
        self._pet.auto_start_changed.connect(self._set_auto_start)
        self._pet.follow_antigravity_changed.connect(self._set_follow_antigravity)
        self._pet.follow_gemini_app_changed.connect(self._set_follow_gemini_app)
        self._pet.settings_synced.connect(self._sync_all_views_from_settings)
        self._pet.visual_bounds_changed.connect(self._keep_quota_window_clear)
        self._pet.exit_requested.connect(lambda: self.gracefully_exit(allow_respawn_watcher=True))

        # TrayIconService signals
        self._tray.show_and_greet_requested.connect(self._wake_from_tray_or_greet)
        self._tray.open_chat_requested.connect(self._open_chat_from_tray)
        self._tray.reset_position_requested.connect(self._reset_position_from_tray)
        self._tray.style_toggle_requested.connect(self._pet.toggle_style_mode)
        self._tray.anti_gravity_toggled.connect(self._set_anti_gravity_from_tray)
        self._tray.pin_on_desktop_toggled.connect(self._set_pin_from_tray)
        self._tray.doze_mode_toggled.connect(self._pet.set_doze_mode_enabled)
        self._tray.speech_bubble_toggled.connect(self._set_bubble_from_tray)
        self._tray.auto_start_toggled.connect(self._set_auto_start)
        self._tray.follow_antigravity_toggled.connect(self._set_follow_antigravity)
        self._tray.follow_gemini_app_toggled.connect(self._set_follow_gemini_app)
        self._tray.click_through_toggled.connect(self._set_click_through_from_tray)
        self._tray.always_on_top_toggled.connect(self._set_always_on_top_from_tray)
        self._tray.sound_toggled.connect(self._set_sound_from_tray)
        self._tray.runtime_log_toggled.connect(self._set_runtime_log_from_tray)
        self._tray.open_profile_requested.connect(self.open_profile_window)
        self._tray.open_character_editor_requested.connect(self.open_character_editor_window)
        self._tray.open_settings_requested.connect(self.open_settings_window)
        self._tray.open_quota_requested.connect(self.toggle_quota_window)
        self._tray.open_update_requested.connect(self.open_update_window)
        self._tray.exit_requested.connect(lambda: self.gracefully_exit(allow_respawn_watcher=True))

    # ==================== IPC Event Listener ====================

    def _start_ipc_listener(self) -> None:
        self._ipc_thread = threading.Thread(
            target=self._ipc_listener_loop,
            name="GeminiDesktopPetIpcListener",
            daemon=True,
        )
        self._ipc_thread.start()

    def _ipc_listener_loop(self) -> None:
        ev_wake = Win32Event(WAKE_EVENT_NAME, manual_reset=False, initial_state=False)
        ev_wake_ag = Win32Event(WAKE_AG_EVENT_NAME, manual_reset=False, initial_state=False)
        ev_wake_gem = Win32Event(WAKE_GEMINI_EVENT_NAME, manual_reset=False, initial_state=False)
        ev_close = Win32Event(CLOSE_EVENT_NAME, manual_reset=False, initial_state=False)

        events = [
            ev_wake,
            ev_wake_ag,
            ev_wake_gem,
            ev_close,
            self._ipc_stop_event,
        ]
        try:
            while not self._ipc_stop_flag.is_set() and not self._is_exiting:
                idx = wait_any_events(events, timeout_ms=1000)
                if idx < 0 or idx == 4:
                    continue
                if idx == 0:
                    self._ipc_bridge.wake_requested.emit("")
                elif idx == 1:
                    self._ipc_bridge.wake_requested.emit("antigravity")
                elif idx == 2:
                    self._ipc_bridge.wake_requested.emit("gemini")
                elif idx == 3:
                    self._ipc_bridge.close_requested.emit()
                    break
        finally:
            ev_wake.close()
            ev_wake_ag.close()
            ev_wake_gem.close()
            ev_close.close()

    @Slot(str)
    def _on_external_wake(self, app_key: str) -> None:
        AppLogger.info("IPC", f"External wake event received: app_key={app_key!r}")
        if app_key:
            self._show_pet_with_wake_greeting(app_key)
        else:
            self._pet.show_and_greet()
            self._pet.raise_()
            self._pet.activateWindow()

    @Slot()
    def _on_external_close(self) -> None:
        self.gracefully_exit(allow_respawn_watcher=True)

    # ==================== Antigravity / Gemini Watcher ====================

    def _start_watcher(self) -> None:
        self._watcher.on_antigravity_started = self._on_antigravity_started
        self._watcher.on_gemini_app_started = self._on_gemini_app_started
        self._watcher.start()

    def _on_antigravity_started(self) -> None:
        AppLogger.info("Watcher", "Watcher detected Antigravity started.")
        if self._settings.follow_antigravity:
            self._show_pet_with_wake_greeting("antigravity")

    def _on_gemini_app_started(self) -> None:
        AppLogger.info("Watcher", "Watcher detected Gemini desktop app started.")
        if self._settings.follow_gemini_app:
            self._show_pet_with_wake_greeting("gemini")

    def _show_pet_with_wake_greeting(self, app_key: str) -> None:
        if app_key == "antigravity":
            msg = "检测到 Antigravity 启动啦，Gemini 娘上线陪你一起写超棒的代码喵✦"
        elif app_key == "gemini":
            msg = "检测到 Gemini 桌面 App 启动了喵～随时在线陪你一起思考和聊天✦"
        else:
            msg = None
        self._pet.show_and_greet(msg)
        self._pet.raise_()

    # ==================== Tray Handlers ====================

    def _wake_from_tray_or_greet(self) -> None:
        self._pet.show_and_greet()
        self._pet.raise_()

    def _open_chat_from_tray(self) -> None:
        if not self._pet.isVisible():
            self._pet.show()
        self._pet.open_chat_dialog()

    def _reset_position_from_tray(self) -> None:
        if not self._pet.isVisible():
            self._pet.show()
        self._pet.move_to_bottom_right_corner()
        self._pet.show_and_greet("回到桌面右下角就位啦喵✦")

    def _set_anti_gravity_from_tray(self, enabled: bool) -> None:
        if enabled != self._settings.anti_gravity_mode:
            self._pet._toggle_anti_gravity()

    def _set_pin_from_tray(self, pinned: bool) -> None:
        self._settings.auto_walk = not pinned
        self._settings_service.save(self._settings)
        self._sync_all_views_from_settings()

    def _set_bubble_from_tray(self, enabled: bool) -> None:
        self._pet.set_speech_bubble_enabled(enabled)
        self._settings_service.save(self._settings)
        self._sync_all_views_from_settings()

    def _set_auto_start(self, enabled: bool) -> None:
        ok = SettingsService.set_startup_enabled(bool(enabled))
        actual = SettingsService.is_startup_enabled()
        self._sync_all_views_from_settings()
        if not ok:
            AppLogger.warn(f"[App] set_startup_enabled({enabled}) failed; current={actual}")
            if self._pet.isVisible():
                self._pet.show_bubble(
                    "Windows 开机自动启动设置失败，请检查启动目录权限或安全软件拦截喵～",
                    4.5,
                )
            return
        if self._pet.isVisible():
            self._pet.show_bubble(
                "已开启 Windows 开机自动启动 喵✦"
                if actual
                else "已关闭 Windows 开机自动启动喵～",
                4.0,
            )

    def _set_follow_antigravity(self, enabled: bool) -> None:
        self._settings.follow_antigravity = enabled
        self._settings_service.save(self._settings)
        self._follow_service.sync_and_apply(self._settings)
        self._sync_all_views_from_settings()

    def _set_follow_gemini_app(self, enabled: bool) -> None:
        self._settings.follow_gemini_app = enabled
        self._settings_service.save(self._settings)
        self._follow_service.sync_and_apply(self._settings)
        self._sync_all_views_from_settings()

    def _set_click_through_from_tray(self, enabled: bool) -> None:
        self._settings.click_through = enabled
        self._settings_service.save(self._settings)
        self._sync_all_views_from_settings()
        if enabled:
            self._pet.show_bubble("已开启鼠标穿透！可在右下角托盘图标右键取消喵～", 4.5, force=True)
        else:
            self._pet.show_bubble("已取消鼠标穿透，现在可以抓抓摸摸我啦喵✦", 3.5, force=True)

    def _set_always_on_top_from_tray(self, enabled: bool) -> None:
        self._settings.always_on_top = enabled
        self._settings_service.save(self._settings)
        self._sync_all_views_from_settings()

    def _set_sound_from_tray(self, enabled: bool) -> None:
        self._settings.sound_enabled = enabled
        self._settings_service.save(self._settings)
        self._sync_all_views_from_settings()

    def _set_runtime_log_from_tray(self, enabled: bool) -> None:
        self._settings.enable_runtime_log = enabled
        AppLogger.set_enabled(enabled)
        self._settings_service.save(self._settings)
        self._sync_all_views_from_settings()

    def _sync_all_views_from_settings(self) -> None:
        self._pet.apply_settings_to_window()
        self._tray.sync_state(self._settings)
        if self._settings_win is not None and self._settings_win.isVisible():
            self._settings_win.sync_from_settings(force=False)
        if self._profile_win is not None and self._profile_win.isVisible():
            self._profile_win.refresh_profile(self._settings)

    # ==================== Secondary Windows Coordination ====================

    def toggle_quota_window(self) -> None:
        if self._quota_win is not None and self._quota_win.isVisible():
            self._quota_win.close()
            return
        if self._quota_win is None:
            self._quota_win = QuotaWindow(
                quota_service=self._quota_service,
                anchor_pet=self._pet,
                auto_close_after=True,
            )
        else:
            self._quota_win.set_anchor(self._pet)
            self._quota_win.set_auto_close_enabled(True)
        self._quota_win.show_loading_card()
        self._quota_win.start_refresh(show_loading_if_empty=False)
        self._quota_win.ensure_on_top()

    def _keep_quota_window_clear(self) -> None:
        if self._quota_win is not None and self._quota_win.isVisible():
            self._quota_win.ensure_clear_of_anchor()

    def open_profile_window(self) -> None:
        if self._profile_win is None:
            self._profile_win = ProfileWindow(
                sprite_manager=self._sprites,
                settings=self._settings,
                on_preview_state=self._on_profile_preview_state,
                on_open_editor=self.open_character_editor_window,
            )
        self._profile_win.refresh_profile(self._settings)
        self._show_and_focus(self._profile_win)

    def _on_profile_preview_state(self, state: PetState, style: PetStyleMode) -> None:
        # 图鉴演示明确携带形象版本，沿用正常切换入口保存用户选择。
        if self._settings.style_mode != style:
            self._pet._set_style_mode(style)
        if not self._pet.isVisible():
            self._pet.show()
        self._pet.trigger_external_sprite_preview(state.to_sprite_key())

    def open_character_editor_window(self) -> None:
        if self._editor_win is None:
            self._editor_win = CharacterEditorWindow(
                profile_service=self._profile_service,
                on_profile_applied=self._on_character_profile_applied,
            )
        self._show_and_focus(self._editor_win)

    def _on_character_profile_applied(self, profile: CharacterProfile) -> None:
        CharacterLore.set_active_profile(profile)
        self._pet.sync_character_profile(profile)
        if self._profile_win is not None and self._profile_win.isVisible():
            self._profile_win.refresh_profile(self._settings)
        self._pet.show_bubble(f"已应用新角色设定「{profile.name}」喵✦", 4.0, force=True)

    def open_settings_window(self) -> None:
        try:
            if self._settings_win is None:
                self._settings_win = SettingsWindow(
                    settings=self._settings,
                    settings_service=self._settings_service,
                    app_follow_service=self._follow_service,
                    on_settings_applied=self._sync_all_views_from_settings,
                    on_open_profile_editor=self.open_character_editor_window,
                    on_open_update_center=self.open_update_window,
                )
            self._settings_win.sync_from_settings()
            self._show_and_focus(self._settings_win)
        except Exception as exc:
            AppLogger.error(f"[App] open settings window failed: {exc}")

    def open_update_window(self) -> None:
        if self._update_win is None:
            self._update_win = UpdateWindow(
                update_service=self._update_service,
                settings_service=self._settings_service,
                settings=self._settings,
                on_request_graceful_exit=lambda: self.gracefully_exit(allow_respawn_watcher=False),
                before_updater_cleanup=self._before_updater_cleanup,
                initial_result=self._latest_update_result,
            )
        self._show_and_focus(self._update_win)
        if self._latest_update_result is None:
            self._update_win.start_check()

    @staticmethod
    def _show_and_focus(win) -> None:
        was_minimized = bool(win.isMinimized() or (win.windowState() & Qt.WindowState.WindowMinimized))
        if was_minimized:
            win.setWindowState(
                (win.windowState() & ~Qt.WindowState.WindowMinimized) | Qt.WindowState.WindowActive
            )
            win.showNormal()
        else:
            win.show()

        # 若窗口曾被拖出屏幕可见区或屏幕分辨率变化，显示前拉回工作区内避免“看不见以为没打开”
        screen = win.screen() or QApplication.primaryScreen()
        if screen is not None:
            avail = screen.availableGeometry()
            frame = win.frameGeometry()
            if not avail.intersects(frame):
                win.move(
                    avail.left() + max(0, (avail.width() - win.width()) // 2),
                    avail.top() + max(0, (avail.height() - win.height()) // 2),
                )
            else:
                nx = max(avail.left(), min(win.x(), avail.right() - min(win.width(), avail.width())))
                ny = max(avail.top(), min(win.y(), avail.bottom() - min(win.height(), avail.height())))
                if nx != win.x() or ny != win.y():
                    win.move(nx, ny)

        def _bring_front() -> None:
            try:
                if not win.isVisible():
                    return
                win.raise_()
                win.activateWindow()
                bring_window_to_front(int(win.winId()))
            except RuntimeError:
                pass

        _bring_front()
        # 托盘/Tool 右键菜单关闭瞬间 Windows 可能将焦点还给原前台窗口，下一帧补一次置前
        QTimer.singleShot(0, _bring_front)

    # ==================== Silent Update Check ====================

    def _start_silent_update_check(self) -> None:
        if self._is_exiting:
            return
        self._silent_update_worker = _SilentUpdateWorker(
            self._update_service, self._settings.update_releases_url, parent=self
        )
        self._silent_update_worker.checked.connect(self._on_silent_update_checked)
        self._silent_update_worker.start()

    def _on_silent_update_checked(self, result: UpdateCheckResult) -> None:
        self._latest_update_result = result
        if result.succeeded and result.update_available:
            self._tray.set_available_update(result.latest_tag)
            if self._pet.isVisible():
                self._pet.show_bubble(
                    f"发现新版本 {result.latest_tag} 啦！点击「检查更新」即可查看更新日志并一键升级喵✨",
                    7.0,
                )
        else:
            self._tray.set_available_update(None)

    # ==================== Cleanup & Exit ====================

    def _before_updater_cleanup(self) -> None:
        # 安装覆盖更新前关闭守护进程避免占用 EXE/DLL 句柄
        self._follow_service.signal_stop_follow_watcher()
        self._follow_service.wait_and_stop_live_watcher(2500)

    def gracefully_exit(self, allow_respawn_watcher: bool = True) -> None:
        if self._is_exiting:
            return
        self._is_exiting = True
        AppLogger.info("App", f"Graceful exit started (allow_respawn_watcher={allow_respawn_watcher}).")

        self._ipc_stop_flag.set()
        self._ipc_stop_event.set()
        self._watcher.dispose()

        if self._silent_update_worker is not None and self._silent_update_worker.isRunning():
            self._silent_update_worker.wait(500)

        for win in (
            self._quota_win,
            self._profile_win,
            self._editor_win,
            self._settings_win,
            self._update_win,
            self._pet,
        ):
            if win is not None:
                try:
                    win.close()
                    win.deleteLater()
                except Exception:
                    pass

        if self._pet is not None and getattr(self._pet, "_first_shown", False):
            self._settings.last_left = float(self._pet.x())
            self._settings.last_top = float(self._pet.y())
        self._settings_service.save(self._settings)

        self._tray.dispose()
        self._sound.dispose()
        self._follow_service.clear_main_pid(os.getpid())
        self._ipc_stop_event.close()

        # 若仍启用跟随联动且非全局停止指令，则拉起无窗 --follow-watcher --from-exit 接力守护
        if (
            allow_respawn_watcher
            and self._follow_service.any_follow_enabled(self._settings)
            and not self._follow_service.is_stop_all_signaled()
        ):
            self._follow_service.spawn_follow_watcher(from_exit=True)

        self._main_mutex.release()
        self._app.processEvents()
        self._app.quit()


def run_gui_app(args: list[str]) -> int:
    """GUI 主程序入口（从 cli.py 在完成非 GUI 子命令分流后延迟加载）。"""
    ensure_app_user_model_id("GeminiDesktopPet.DesktopPet")

    main_mutex = Win32Mutex(MAIN_MUTEX_NAME)
    if not main_mutex.acquire():
        # 已有主实例正在运行：发送对应的跨进程唤醒信号后直接退出
        low_args = [a.lower() for a in args]
        woken_by = ""
        for a in low_args:
            if a.startswith("--woken-by:"):
                woken_by = a.split(":", 1)[1].strip()
                break
        if woken_by == "antigravity":
            AppFollowService.try_signal_named_event(WAKE_AG_EVENT_NAME)
        elif woken_by == "gemini":
            AppFollowService.try_signal_named_event(WAKE_GEMINI_EVENT_NAME)
        else:
            AppFollowService.try_signal_named_event(WAKE_EVENT_NAME)
        main_mutex.release()
        return 0

    qt_app = QApplication(sys.argv[:1])
    qt_app.setQuitOnLastWindowClosed(False)
    qt_app.setApplicationName("GeminiDesktopPet")
    qt_app.setApplicationDisplayName("Gemini 娘桌宠")
    qt_app.setWindowIcon(TrayIconService.load_app_icon())

    coordinator = PetApplicationCoordinator(qt_app, args, main_mutex)
    # 保持强引用避免 GC 回收 coordinator
    qt_app.setProperty("_gemini_pet_coordinator", coordinator)
    return int(qt_app.exec())
