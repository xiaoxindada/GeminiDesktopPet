"""Starlight 深紫色星辰主题 QSS 样式表与色板定义。"""

from __future__ import annotations

STARLIGHT_MENU_QSS = """
QMenu {
    background-color: rgba(24, 19, 46, 245);
    color: #F6F3FF;
    border: 1px solid #5B49A8;
    border-radius: 12px;
    padding: 6px 4px;
    font-family: "Microsoft YaHei UI", "Segoe UI";
    font-size: 12px;
}
QMenu::item {
    background-color: transparent;
    color: #F6F3FF;
    padding: 7px 24px 7px 14px;
    margin: 1px 4px;
    border-radius: 7px;
}
QMenu::item:selected {
    background-color: #3C2E75;
    color: #FFFFFF;
}
QMenu::item:disabled {
    color: #7D75A8;
}
QMenu::separator {
    height: 1px;
    background-color: #382D69;
    margin: 4px 10px;
}
QMenu::indicator {
    width: 14px;
    height: 14px;
    margin-left: 6px;
}
QMenu::indicator:checked {
    background-color: #7C5CFF;
    border: 1px solid #A78BFA;
    border-radius: 4px;
}
"""

STARLIGHT_WINDOW_QSS = """
QWidget {
    background-color: #141126;
    color: #F4F2FF;
    font-family: "Microsoft YaHei UI", "Segoe UI";
    font-size: 12px;
}
QFrame#CardPanel {
    background-color: #1D1836;
    border: 1px solid #342B61;
    border-radius: 12px;
}
QFrame#HeaderCard {
    background-color: #1E1938;
    border: 1px solid #3C316E;
    border-radius: 14px;
}
QFrame#FooterCard {
    background-color: #1A1530;
    border: 1px solid #342A60;
    border-radius: 10px;
}
QFrame#TipCard {
    background-color: #19152E;
    border: 1px solid #2E2656;
    border-radius: 10px;
}
QPushButton {
    background-color: #2B2252;
    color: #F5F2FF;
    border: 1px solid #4E3F8D;
    border-radius: 8px;
    padding: 6px 12px;
    font-size: 12px;
}
QPushButton:hover {
    background-color: #3C2E75;
    border-color: #7C5CFF;
}
QPushButton:pressed {
    background-color: #533FA0;
}
QPushButton:disabled {
    background-color: #201A3B;
    color: #7D75A8;
    border-color: #342B5E;
}
QPushButton#PrimaryBtn {
    background-color: #6D4AFF;
    color: #FFFFFF;
    border: 1px solid #8B6EFF;
    font-weight: bold;
}
QPushButton#PrimaryBtn:hover {
    background-color: #7F5FFF;
}
QPushButton#DangerBtn {
    background-color: #3D1F35;
    color: #FCA5A5;
    border: 1px solid #6B2D53;
}
QPushButton#DangerBtn:hover {
    background-color: #542847;
    border-color: #FB7185;
}
QLineEdit, QPlainTextEdit, QTextEdit {
    background-color: #120E24;
    color: #F6F3FF;
    border: 1px solid #463980;
    border-radius: 8px;
    padding: 6px 8px;
    selection-background-color: #6D4AFF;
}
QLineEdit:focus, QPlainTextEdit:focus, QTextEdit:focus {
    border-color: #7C5CFF;
}
QComboBox {
    background-color: #1B1633;
    color: #F6F3FF;
    border: 1px solid #4B3C8A;
    border-radius: 8px;
    padding: 5px 10px;
    min-height: 22px;
}
QComboBox:hover {
    border-color: #7C5CFF;
}
QComboBox QAbstractItemView {
    background-color: #1E183A;
    color: #F6F3FF;
    border: 1px solid #6D54D4;
    selection-background-color: #4B3A8F;
    selection-color: #FFFFFF;
    outline: none;
}
QCheckBox {
    spacing: 8px;
    color: #EAE6FF;
}
QCheckBox::indicator {
    width: 15px;
    height: 15px;
    border-radius: 4px;
    border: 1px solid #5B49A8;
    background-color: #120E24;
}
QCheckBox::indicator:checked {
    background-color: #7C5CFF;
    border-color: #A78BFA;
}
QSlider::groove:horizontal {
    height: 6px;
    background: #251E46;
    border-radius: 3px;
}
QSlider::sub-page:horizontal {
    background: #7C5CFF;
    border-radius: 3px;
}
QSlider::handle:horizontal {
    background: #C4B5FD;
    border: 2px solid #7C5CFF;
    width: 14px;
    height: 14px;
    margin: -5px 0;
    border-radius: 8px;
}
QScrollArea {
    border: none;
    background: transparent;
}
QScrollBar:vertical {
    background: rgba(255, 255, 255, 10);
    width: 8px;
    border-radius: 4px;
    margin: 2px;
}
QScrollBar::handle:vertical {
    background: #4C3D8A;
    border-radius: 4px;
    min-height: 24px;
}
QScrollBar::handle:vertical:hover {
    background: #6D55C4;
}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
    height: 0px;
}
QTabWidget::pane {
    border: none;
    background: transparent;
}
QTabBar::tab {
    background: transparent;
    color: #B9B2DE;
    padding: 8px 14px;
    margin-right: 8px;
    border-radius: 10px;
    font-weight: bold;
    font-size: 13px;
}
QTabBar::tab:hover {
    background-color: #251D47;
}
QTabBar::tab:selected {
    background-color: #32285E;
    color: #FFFFFF;
}
QProgressBar {
    background-color: #241D44;
    border: none;
    border-radius: 4px;
    text-align: center;
}
QProgressBar::chunk {
    background-color: #7C5CFF;
    border-radius: 4px;
}
QToolTip {
    background-color: #1A1533;
    color: #F6F3FF;
    border: 1px solid #6D54D4;
    border-radius: 6px;
    padding: 4px 8px;
}
"""
