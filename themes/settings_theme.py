"""设置窗口主题：沿用 Starlight 深紫星辰风格，适配左侧导航 + 卡片布局。"""

from __future__ import annotations

from themes.starlight_theme import STARLIGHT_WINDOW_QSS

SETTINGS_WINDOW_QSS = STARLIGHT_WINDOW_QSS + """
QFrame#SettingsShell {
    background-color: #141126;
    border: 1px solid #3A2E6B;
    border-radius: 14px;
}
QWidget#SettingsHeader,
QWidget#SettingsContent,
QWidget#SettingsFooter,
QWidget#PageInner,
QWidget#CardHost,
QWidget#ControlHost,
QFrame#SettingsSidebar,
QStackedWidget {
    background: transparent;
}
QLabel {
    background: transparent;
}
QLabel#WindowTitle {
    font-size: 19px;
    font-weight: 600;
    color: #F4F2FF;
}
QLabel#PageTitle {
    font-size: 17px;
    font-weight: 600;
    color: #F4F2FF;
}
QLabel#PageDesc {
    font-size: 12px;
    color: #9D95C8;
}
QLabel#CardHeading {
    font-size: 13px;
    font-weight: 600;
    color: #C4B5FD;
}
QLabel#RowTitle {
    font-size: 13px;
    color: #EAE6FF;
}
QLabel#RowDesc {
    font-size: 11.5px;
    color: #9D95C8;
}
QLabel#ValueLabel {
    color: #C4B5FD;
    font-size: 12.5px;
}
QLabel#CategoryTag {
    color: #8A82B4;
    font-size: 11.5px;
}
QLabel#EmptyHint {
    color: #8A82B4;
    font-size: 12.5px;
}
QFrame#RowDivider {
    background-color: #2E2656;
    border: none;
}
QFrame#SummaryRow {
    background: transparent;
    border: none;
    border-radius: 8px;
}
QFrame#SummaryRow:hover {
    background-color: #251E46;
}
QPushButton#NavBtn {
    text-align: left;
    padding: 8px 10px;
    border: none;
    border-radius: 8px;
    background: transparent;
    color: #B9B2DE;
    font-size: 13px;
}
QPushButton#NavBtn:hover {
    background-color: #241D47;
}
QPushButton#NavBtn:checked {
    background-color: #2E2456;
    color: #FFFFFF;
    font-weight: 600;
}
QPushButton#GhostBtn {
    background-color: #2B2252;
    color: #F5F2FF;
    border: 1px solid #4E3F8D;
    padding: 5px 12px;
}
QPushButton#CloseBtn {
    background: transparent;
    border: none;
    border-radius: 6px;
    color: #B9B2DE;
    font-size: 15px;
    padding: 0;
}
QPushButton#CloseBtn:hover {
    background-color: #2B2252;
    color: #FFFFFF;
}
QFrame#SearchBox {
    background-color: #120E24;
    border: 1px solid #463980;
    border-radius: 10px;
}
QLineEdit#SearchEdit {
    background: transparent;
    border: none;
    padding: 7px 0;
    font-size: 13px;
}
"""
