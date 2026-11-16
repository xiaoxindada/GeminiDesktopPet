"""Windows 原生系统托盘图标与快捷菜单管理，鼠标穿透模式下也可完整控制系统。"""

from __future__ import annotations

import os
from typing import Callable, Optional

from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QAction, QFont, QIcon
from PySide6.QtWidgets import QApplication, QMenu, QStyle, QSystemTrayIcon

from models.app_version import AppVersion, get_bundled_resource_dir
from models.pet_settings import PetSettings, PetStyleMode
from services.settings_service import SettingsService


class TrayIconService(QObject):
    """封装 QSystemTrayIcon 与 Starlight 深色右键托盘菜单。"""

    show_and_greet_requested = Signal()
    open_chat_requested = Signal()
    reset_position_requested = Signal()
    style_toggle_requested = Signal()
    anti_gravity_toggled = Signal(bool)
    pin_on_desktop_toggled = Signal(bool)
    doze_mode_toggled = Signal(bool)
    speech_bubble_toggled = Signal(bool)
    auto_start_toggled = Signal(bool)
    follow_antigravity_toggled = Signal(bool)
    follow_gemini_app_toggled = Signal(bool)
    click_through_toggled = Signal(bool)
    always_on_top_toggled = Signal(bool)
    sound_toggled = Signal(bool)
    runtime_log_toggled = Signal(bool)
    open_profile_requested = Signal()
    open_character_editor_requested = Signal()
    open_settings_requested = Signal()
    open_quota_requested = Signal()
    sync_antigravity_toggled = Signal(bool)
    open_update_requested = Signal()
    exit_requested = Signal()

    def __init__(self, parent: Optional[QObject] = None, menu_qss: str = "") -> None:
        super().__init__(parent)
        self._disposed = False
        self._icon = self.load_app_icon()

        self._menu = QMenu()
        if menu_qss:
            self._menu.setStyleSheet(menu_qss)

        greet_act = QAction("✦ 呼唤 Gemini 娘", self._menu)
        bold_font = QFont(self._menu.font())
        bold_font.setBold(True)
        greet_act.setFont(bold_font)
        greet_act.triggered.connect(self.show_and_greet_requested)
        self._menu.addAction(greet_act)

        self._add_action("📊 查看反重力额度", self.open_quota_requested.emit)
        self._sync_act = self._add_check("💬 实时同步反重力对话", self.sync_antigravity_toggled.emit)
        self._add_action("💬 聊天模式 / 选模型对话", self.open_chat_requested.emit)

        self._style_action = self._add_action(
            "🎀 形象切换：当前 Q 版 → 切至标准版", self.style_toggle_requested.emit
        )
        self._add_action("📍 回到屏幕右下角", self.reset_position_requested.emit)
        self._menu.addSeparator()

        self._anti_gravity_act = self._add_check("✨ 反重力悬浮模式", self.anti_gravity_toggled.emit)
        self._pin_act = self._add_check("📌 固定在桌面（不乱跑）", self.pin_on_desktop_toggled.emit)
        self._doze_act = self._add_check("💤 打盹模式（原地睡觉待机）", self.doze_mode_toggled.emit)
        self._bubble_act = self._add_check("💭 自动弹出对话气泡", self.speech_bubble_toggled.emit)
        self._autostart_act = self._add_check(
            "⚡ Windows 开机自动启动", self.auto_start_toggled.emit
        )
        self._menu.aboutToShow.connect(self.sync_auto_start_state)
        self._follow_ag_act = self._add_check(
            "🚀 跟随 Antigravity 启动而启动", self.follow_antigravity_toggled.emit
        )
        self._follow_gemini_act = self._add_check(
            "🌟 跟随 Gemini 桌面 App 启动而启动", self.follow_gemini_app_toggled.emit
        )
        self._always_on_top_act = self._add_check("📌 窗口置顶", self.always_on_top_toggled.emit)
        self._click_through_act = self._add_check("👻 鼠标穿透模式", self.click_through_toggled.emit)
        self._sound_act = self._add_check("🔔 互动音效", self.sound_toggled.emit)
        self._runtime_log_act = self._add_check(
            "📝 记录运行日志（排错用）", self.runtime_log_toggled.emit
        )

        self._menu.addSeparator()
        self._add_action("📖 角色档案与三视图", self.open_profile_requested.emit)
        self._add_action("✏️ 自定义角色设定...", self.open_character_editor_requested.emit)
        self._add_action("⚙️ 偏好设置...", self.open_settings_requested.emit)
        self._update_act = self._add_action(
            f"🆕 检查更新 ({AppVersion.current_display()})", self.open_update_requested.emit
        )

        self._menu.addSeparator()
        self._add_action("🌙 让 Gemini 娘休息（退出）", self.exit_requested.emit)

        self._tray = QSystemTrayIcon(self._icon, self)
        self._tray.setToolTip("Gemini 娘 · 桌面萌宠 (双击/右键看额度)")
        self._tray.setContextMenu(self._menu)
        self._tray.activated.connect(self._on_activated)
        self._tray.show()

    def _add_action(self, text: str, cb: Callable[[], None]) -> QAction:
        act = QAction(text, self._menu)
        act.triggered.connect(lambda _checked=False, fn=cb: fn())
        self._menu.addAction(act)
        return act

    def _add_check(self, text: str, cb: Callable[[bool], None]) -> QAction:
        act = QAction(text, self._menu)
        act.setCheckable(True)
        act.triggered[bool].connect(lambda checked, fn=cb: fn(bool(checked)))
        self._menu.addAction(act)
        return act

    def _on_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason == QSystemTrayIcon.ActivationReason.DoubleClick:
            self.show_and_greet_requested.emit()

    def sync_auto_start_state(self) -> None:
        if self._disposed:
            return
        self._autostart_act.blockSignals(True)
        try:
            self._autostart_act.setChecked(SettingsService.is_startup_enabled())
        finally:
            self._autostart_act.blockSignals(False)

    def sync_state(self, settings: PetSettings) -> None:
        if self._disposed:
            return
        self.sync_auto_start_state()
        self._style_action.setText(
            "🎀 形象切换：当前标准版 → 切至 Q 版"
            if settings.style_mode == PetStyleMode.STANDARD
            else "🎀 形象切换：当前 Q 版 → 切至标准版"
        )
        self._anti_gravity_act.setChecked(settings.anti_gravity_mode)
        self._sync_act.setChecked(settings.chat_sync_enabled)
        self._pin_act.setChecked(not settings.auto_walk)
        self._doze_act.setChecked(settings.doze_mode)
        self._bubble_act.setChecked(settings.show_speech_bubble)
        self._follow_ag_act.setChecked(settings.follow_antigravity)
        self._follow_gemini_act.setChecked(settings.follow_gemini_app)
        self._always_on_top_act.setChecked(settings.always_on_top)
        self._click_through_act.setChecked(settings.click_through)
        self._sound_act.setChecked(settings.sound_enabled)
        self._runtime_log_act.setChecked(settings.enable_runtime_log)

    def set_available_update(self, latest_tag: Optional[str]) -> None:
        if self._disposed:
            return
        if latest_tag and latest_tag.strip():
            self._update_act.setText(f"🆕 发现新版本 {latest_tag}（点击查看更新）")
        else:
            self._update_act.setText(f"🆕 检查更新 ({AppVersion.current_display()})")

    def show_balloon(self, title: str, text: str) -> None:
        if self._disposed:
            return
        try:
            self._tray.showMessage(
                title, text, QSystemTrayIcon.MessageIcon.Information, 2500
            )
        except Exception:
            pass

    @staticmethod
    def load_app_icon() -> QIcon:
        ico_path = os.path.join(get_bundled_resource_dir(), "Assets", "AppIcon.ico")
        if os.path.isfile(ico_path):
            icon = QIcon(ico_path)
            if not icon.isNull():
                return icon
        app = QApplication.instance()
        if app is not None:
            return app.style().standardIcon(QStyle.StandardPixmap.SP_ComputerIcon)
        return QIcon()

    def dispose(self) -> None:
        if self._disposed:
            return
        self._disposed = True
        try:
            self._tray.hide()
            self._tray.setContextMenu(None)
        except Exception:
            pass
        try:
            self._menu.close()
        except Exception:
            pass
