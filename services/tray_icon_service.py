"""Windows 原生托盘图标；托盘菜单复用桌面右键菜单的动作清单，并追加唤醒类专用操作。"""

from __future__ import annotations

import os
from typing import Callable, Optional

from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QAction, QFont, QIcon
from PySide6.QtWidgets import QApplication, QMenu, QStyle, QSystemTrayIcon

from models.app_version import get_bundled_resource_dir
from services.shared_menu import populate_shared_menu


class TrayIconService(QObject):
    """封装 QSystemTrayIcon 与 Starlight 深色托盘菜单。"""

    show_and_greet_requested = Signal()
    open_chat_requested = Signal()
    reset_position_requested = Signal()

    def __init__(
        self,
        parent: Optional[QObject] = None,
        menu_qss: str = "",
        shared_menu_spec: Optional[Callable[[], list[object]]] = None,
        shared_menu_refresh: Optional[Callable[[], None]] = None,
        update_action: Optional[QAction] = None,
    ) -> None:
        super().__init__(parent)
        self._disposed = False
        self._icon = self.load_app_icon()
        self._shared_menu_spec = shared_menu_spec
        self._shared_menu_refresh = shared_menu_refresh
        self._update_action = update_action

        self._menu = QMenu()
        if menu_qss:
            self._menu.setStyleSheet(menu_qss)

        # 托盘专用操作：唤醒、聊天、额度与归位
        greet_act = QAction("✦ 呼唤 Gemini 娘", self._menu)
        bold_font = QFont(self._menu.font())
        bold_font.setBold(True)
        greet_act.setFont(bold_font)
        greet_act.triggered.connect(self.show_and_greet_requested)
        self._menu.addAction(greet_act)

        self._add_action("💬 聊天模式 / 选模型对话", self.open_chat_requested.emit)
        self._add_action("📍 回到屏幕右下角", self.reset_position_requested.emit)

        # 桌面右键菜单的同一批 QAction，勾选状态与文本自动同步
        if shared_menu_spec is not None:
            self._menu.addSeparator()
            populate_shared_menu(self._menu, shared_menu_spec())
            if shared_menu_refresh is not None:
                self._menu.aboutToShow.connect(shared_menu_refresh)

        self._tray = QSystemTrayIcon(self._icon, self)
        self._tray.setToolTip("Gemini 娘 · 桌面萌宠（双击唤醒）")
        self._tray.setContextMenu(self._menu)
        self._tray.activated.connect(self._on_activated)
        self._tray.show()

    def _add_action(self, text: str, cb: Callable[[], None]) -> QAction:
        act = QAction(text, self._menu)
        act.triggered.connect(lambda _checked=False, fn=cb: fn())
        self._menu.addAction(act)
        return act

    def _on_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason == QSystemTrayIcon.ActivationReason.DoubleClick:
            self.show_and_greet_requested.emit()

    def set_available_update(self, latest_tag: Optional[str]) -> None:
        if self._disposed or self._update_action is None:
            return
        if latest_tag and latest_tag.strip():
            self._update_action.setText(f"🆕 发现新版本 {latest_tag}（点击查看更新）")
        else:
            self._update_action.setText("🆕 检查更新...")

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
