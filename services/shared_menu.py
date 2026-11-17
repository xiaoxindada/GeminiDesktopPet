"""桌面右键菜单与托盘菜单共用的动作清单渲染，避免维护两套菜单实现。"""

from __future__ import annotations

from typing import Any

from PySide6.QtGui import QAction
from PySide6.QtWidgets import QMenu

# 共用清单里的分隔符占位（QAction 之外的唯一标记类型）
MENU_SEPARATOR = object()


def populate_shared_menu(menu: QMenu, spec: list[Any]) -> None:
    """按共用清单渲染菜单；QAction 实例可同时挂在多个菜单上，勾选状态自动同步。"""
    for item in spec:
        if item is MENU_SEPARATOR:
            menu.addSeparator()
        elif isinstance(item, tuple):
            label, actions = item
            submenu = menu.addMenu(label)
            for action in actions:
                submenu.addAction(action)
        elif isinstance(item, QAction):
            menu.addAction(item)
