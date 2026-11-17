"""设置窗口：左侧功能分类导航 + 右侧卡片内容，并提供全部设置汇总与搜索。"""

from __future__ import annotations

import os
from typing import Callable, Optional

from PySide6.QtCore import (
    Property,
    QEasingCurve,
    QEvent,
    QPointF,
    QPropertyAnimation,
    QRect,
    QRectF,
    QSize,
    Qt,
    QThread,
    QTimer,
    QUrl,
    Signal,
)
from PySide6.QtGui import (
    QColor,
    QCursor,
    QDesktopServices,
    QIcon,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
)
from PySide6.QtWidgets import (
    QAbstractButton,
    QApplication,
    QButtonGroup,
    QComboBox,
    QFrame,
    QGraphicsDropShadowEffect,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSlider,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from models.app_version import AppVersion
from models.pet_settings import (
    DEFAULT_GEMINI_MODEL,
    THINKING_LEVEL_OPTIONS,
    PetSettings,
    PetStyleMode,
    format_model_display_name,
    format_thinking_level_label,
    normalize_gemini_model,
    normalize_thinking_level,
)
from services.app_follow_service import AppFollowService
from services.app_logger import AppLogger
from services.followed_app_probe import FollowedAppProbe
from services.gemini_chat_service import GeminiChatService
from services.settings_service import SettingsService
from services.tray_icon_service import TrayIconService
from services.update_service import UpdateService
from themes.settings_theme import SETTINGS_WINDOW_QSS


class _ModelsRefreshWorker(QThread):
    """后台使用当前 API Key 获取官方可用模型。"""

    completed = Signal(object, str)

    def __init__(self, api_key: str, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._api_key = api_key

    def run(self) -> None:
        try:
            models = GeminiChatService.fetch_api_key_models(self._api_key)
            self.completed.emit(models, "")
        except Exception as exc:
            self.completed.emit([], str(exc))


def _make_icon(kind: str, size: int = 18, color: str = "#A79FD4") -> QIcon:
    """用路径绘制单色线性图标，避免不同系统的 emoji 字体风格不一致。"""
    scale = 2.0
    pix = QPixmap(int(size * scale), int(size * scale))
    pix.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pix)
    try:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.scale(scale, scale)
        pen = QPen(QColor(color))
        pen.setWidthF(1.4)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)

        if kind == "all":
            for x, y in ((3.4, 3.4), (10.6, 3.4), (3.4, 10.6), (10.6, 10.6)):
                painter.drawRoundedRect(QRectF(x, y, 4.2, 4.2), 1.2, 1.2)
        elif kind == "general":
            # 三条滑杆，直观表达“可调参数”
            for y, knob_x in ((5.2, 12.2), (9.0, 6.6), (12.8, 10.0)):
                painter.drawLine(QPointF(3.0, y), QPointF(15.0, y))
                painter.setBrush(QColor("#241D47"))
                painter.drawEllipse(QPointF(knob_x, y), 1.7, 1.7)
                painter.setBrush(Qt.BrushStyle.NoBrush)
        elif kind == "appearance":
            painter.drawRoundedRect(QRectF(2.6, 3.4, 12.8, 11.2), 2.0, 2.0)
            painter.drawEllipse(QPointF(6.4, 7.0), 1.3, 1.3)
            painter.drawPolyline(
                [
                    QPointF(4.4, 13.2),
                    QPointF(8.0, 9.6),
                    QPointF(10.6, 12.0),
                    QPointF(12.2, 10.4),
                    QPointF(14.0, 12.2),
                ]
            )
        elif kind == "character":
            painter.drawEllipse(QPointF(9.0, 6.4), 3.0, 3.0)
            shoulders = QPainterPath()
            shoulders.moveTo(3.4, 15.2)
            shoulders.cubicTo(4.2, 11.4, 6.4, 10.2, 9.0, 10.2)
            shoulders.cubicTo(11.6, 10.2, 13.8, 11.4, 14.6, 15.2)
            painter.drawPath(shoulders)
        elif kind == "behavior":
            heart = QPainterPath()
            heart.moveTo(9.0, 15.4)
            heart.cubicTo(3.0, 11.4, 2.0, 7.4, 4.5, 5.4)
            heart.cubicTo(6.3, 3.9, 8.0, 4.9, 9.0, 6.5)
            heart.cubicTo(10.0, 4.9, 11.7, 3.9, 13.5, 5.4)
            heart.cubicTo(16.0, 7.4, 15.0, 11.4, 9.0, 15.4)
            painter.drawPath(heart)
        elif kind == "chat":
            painter.drawRoundedRect(QRectF(2.6, 3.4, 12.8, 9.4), 2.6, 2.6)
            painter.drawPolyline([QPointF(6.0, 12.8), QPointF(5.2, 15.8), QPointF(9.2, 12.8)])
        elif kind == "startup":
            bolt = QPainterPath()
            bolt.moveTo(10.6, 2.6)
            bolt.lineTo(4.8, 10.2)
            bolt.lineTo(8.6, 10.2)
            bolt.lineTo(7.4, 15.4)
            bolt.lineTo(13.2, 7.8)
            bolt.lineTo(9.4, 7.8)
            bolt.closeSubpath()
            painter.drawPath(bolt)
        elif kind == "logs":
            painter.drawRoundedRect(QRectF(4.0, 2.6, 10.0, 12.8), 1.8, 1.8)
            for y in (6.4, 9.0, 11.6):
                painter.drawLine(QPointF(6.4, y), QPointF(11.6, y))
        elif kind == "about":
            painter.drawEllipse(QPointF(9.0, 9.0), 6.6, 6.6)
            painter.setBrush(QColor(color))
            painter.drawEllipse(QPointF(9.0, 6.2), 0.8, 0.8)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawLine(QPointF(9.0, 8.7), QPointF(9.0, 12.4))
        elif kind == "search":
            painter.setPen(QPen(QColor(color), 1.6))
            painter.drawEllipse(QPointF(7.2, 7.2), 4.0, 4.0)
            painter.drawLine(QPointF(10.2, 10.2), QPointF(13.2, 13.2))
    finally:
        painter.end()
    return QIcon(pix)


class ToggleSwitch(QAbstractButton):
    """深色胶囊开关：紫色轨道表示开启，灰紫色表示关闭。"""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setCheckable(True)
        self.setFixedSize(40, 22)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._offset = 0.0
        self._animating = False
        self._anim = QPropertyAnimation(self, b"offset", self)
        self._anim.setDuration(140)
        self._anim.setEasingCurve(QEasingCurve.Type.OutCubic)

    def _get_offset(self) -> float:
        return self._offset

    def _set_offset(self, value: float) -> None:
        self._offset = max(0.0, min(1.0, float(value)))
        self.update()

    offset = Property(float, _get_offset, _set_offset)

    def setChecked(self, checked: bool) -> None:  # noqa: N802 - Qt 命名
        super().setChecked(checked)
        if not self._animating:
            self._anim.stop()
            self._offset = 1.0 if checked else 0.0
            self.update()

    def nextCheckState(self) -> None:  # noqa: N802 - Qt 命名
        target = not self.isChecked()
        self._animating = True
        try:
            super().setChecked(target)
        finally:
            self._animating = False
        self._anim.stop()
        self._anim.setStartValue(self._offset)
        self._anim.setEndValue(1.0 if target else 0.0)
        self._anim.start()

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        track = QRectF(0.5, 0.5, self.width() - 1.0, self.height() - 1.0)
        radius = track.height() / 2.0
        off_color = QColor("#332A5B")
        on_color = QColor("#7C5CFF")
        ratio = self._offset
        track_color = QColor(
            int(off_color.red() + (on_color.red() - off_color.red()) * ratio),
            int(off_color.green() + (on_color.green() - off_color.green()) * ratio),
            int(off_color.blue() + (on_color.blue() - off_color.blue()) * ratio),
        )
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(track_color)
        painter.drawRoundedRect(track, radius, radius)

        diameter = self.height() - 6.0
        knob_x = 3.0 + (self.width() - 6.0 - diameter) * self._offset
        painter.setBrush(QColor("#F6F3FF"))
        painter.setPen(QPen(QColor(0, 0, 0, 60), 1.0))
        painter.drawEllipse(QRectF(knob_x, 3.0, diameter, diameter))

        if self.hasFocus():
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(QColor(167, 139, 250, 160), 1.2))
            painter.drawRoundedRect(QRectF(0.6, 0.6, self.width() - 1.2, self.height() - 1.2), radius, radius)
        painter.end()


class _Card:
    """统一设置卡片的行布局：标题/说明在左，控件在右，行间自动加分隔线。"""

    def __init__(self, parent_layout: QVBoxLayout, heading: Optional[str] = None) -> None:
        self.frame = QFrame()
        self.frame.setObjectName("CardPanel")
        self.layout = QVBoxLayout(self.frame)
        self.layout.setContentsMargins(16, 12, 16, 12)
        self.layout.setSpacing(0)
        if heading:
            label = QLabel(heading)
            label.setObjectName("CardHeading")
            self.layout.addWidget(label)
            self.layout.addSpacing(2)
        self._rows = 0
        parent_layout.addWidget(self.frame)

    def add_row(self, control: QWidget, title: str, desc: str = "") -> QWidget:
        if self._rows:
            divider = QFrame()
            divider.setObjectName("RowDivider")
            divider.setFixedHeight(1)
            self.layout.addWidget(divider)

        row = QWidget()
        row.setObjectName("CardHost")
        hbox = QHBoxLayout(row)
        hbox.setContentsMargins(0, 9, 0, 9)
        hbox.setSpacing(18)

        text_col = QVBoxLayout()
        text_col.setSpacing(2)
        title_lbl = QLabel(title)
        title_lbl.setObjectName("RowTitle")
        text_col.addWidget(title_lbl)
        if desc:
            desc_lbl = QLabel(desc)
            desc_lbl.setObjectName("RowDesc")
            desc_lbl.setWordWrap(True)
            text_col.addWidget(desc_lbl)
        hbox.addLayout(text_col, 1)
        hbox.addWidget(control, 0, Qt.AlignmentFlag.AlignVCenter)

        self.layout.addWidget(row)
        self._rows += 1
        return row

    def add_widget(self, widget: QWidget, top: int = 8) -> None:
        self.layout.addSpacing(top)
        self.layout.addWidget(widget)

    def add_buttons(self, widgets: list[QWidget], top: int = 10) -> None:
        host = QWidget()
        host.setObjectName("CardHost")
        hbox = QHBoxLayout(host)
        hbox.setContentsMargins(0, 0, 0, 0)
        hbox.setSpacing(8)
        for widget in widgets:
            hbox.addWidget(widget)
        hbox.addStretch(1)
        self.add_widget(host, top)


class _SummaryRow(QFrame):
    """全部设置汇总行，点击可跳到对应分类页。"""

    activated = Signal(int)

    def __init__(
        self,
        title: str,
        desc: str,
        category: str,
        target_index: int,
        on_activate: Optional[Callable[[], None]] = None,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("SummaryRow")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._target = target_index
        self._on_activate = on_activate

        hbox = QHBoxLayout(self)
        hbox.setContentsMargins(10, 8, 10, 8)
        hbox.setSpacing(10)

        text_col = QVBoxLayout()
        text_col.setSpacing(1)
        title_lbl = QLabel(title)
        title_lbl.setObjectName("RowTitle")
        desc_lbl = QLabel(desc)
        desc_lbl.setObjectName("RowDesc")
        text_col.addWidget(title_lbl)
        text_col.addWidget(desc_lbl)
        hbox.addLayout(text_col, 1)

        category_lbl = QLabel(category)
        category_lbl.setObjectName("CategoryTag")
        hbox.addWidget(category_lbl, 0, Qt.AlignmentFlag.AlignVCenter)

        self.value_label = QLabel("")
        self.value_label.setObjectName("ValueLabel")
        self.value_label.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        self.value_label.setMinimumWidth(96)
        hbox.addWidget(self.value_label, 0, Qt.AlignmentFlag.AlignVCenter)

        arrow_lbl = QLabel("›")
        arrow_lbl.setStyleSheet("color: #6B5FA0; font-size: 15px; background: transparent;")
        hbox.addWidget(arrow_lbl, 0, Qt.AlignmentFlag.AlignVCenter)

        for label in (title_lbl, desc_lbl, category_lbl, self.value_label, arrow_lbl):
            label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
            label.setCursor(Qt.CursorShape.PointingHandCursor)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        if event.button() == Qt.MouseButton.LeftButton:
            if self._on_activate is not None:
                self._on_activate()
            else:
                self.activated.emit(self._target)
        super().mouseReleaseEvent(event)


class SettingsWindow(QWidget):
    """集中管理全部偏好的设置面板。"""

    _PAGE_ALL = 0
    _PAGE_GENERAL = 1
    _PAGE_APPEARANCE = 2
    _PAGE_CHARACTER = 3
    _PAGE_BEHAVIOR = 4
    _PAGE_CHAT = 5
    _PAGE_STARTUP = 6
    _PAGE_LOGS = 7
    _PAGE_ABOUT = 8

    _NAV_ITEMS = (
        ("all", "全部设置"),
        ("general", "通用设置"),
        ("appearance", "外观形象"),
        ("character", "角色设定"),
        ("behavior", "行为互动"),
        ("chat", "对话与模型"),
        ("startup", "启动与联动"),
        ("logs", "日志与排查"),
        ("about", "关于与更新"),
    )

    def __init__(
        self,
        settings: PetSettings,
        settings_service: SettingsService,
        app_follow_service: AppFollowService,
        on_settings_applied: Callable[[], None],
        on_open_update_center: Optional[Callable[[], None]] = None,
        on_open_character_editor: Optional[Callable[[], None]] = None,
        on_open_profile: Optional[Callable[[], None]] = None,
        on_preview_appearance: Optional[Callable[[Optional[float], Optional[float]], None]] = None,
        on_antigravity_sync_changed: Optional[Callable[[bool], None]] = None,
        on_auto_start_changed: Optional[Callable[[bool], None]] = None,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._settings = settings
        self._settings_service = settings_service
        self._follow_service = app_follow_service
        self._on_applied = on_settings_applied
        self._on_open_update = on_open_update_center
        self._on_open_editor = on_open_character_editor
        self._on_open_profile = on_open_profile
        self._on_preview_appearance = on_preview_appearance
        self._preview_active = False
        self._on_antigravity_sync_changed = on_antigravity_sync_changed
        self._on_auto_start_changed = on_auto_start_changed
        self._models_refresh_worker: Optional[_ModelsRefreshWorker] = None
        self._draft_gemini_model: str = DEFAULT_GEMINI_MODEL
        self._loading = True
        self._self_updating = False
        self._dirty = False
        self._drag_offset = None
        self._positioned = False
        self._summary_rows: list[tuple[_SummaryRow, str, Callable[[], str]]] = []

        self.setWindowTitle("Gemini 娘 · 设置")
        self.setWindowIcon(TrayIconService.load_app_icon())
        self.setWindowFlags(Qt.WindowType.Window | Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setStyleSheet(SETTINGS_WINDOW_QSS)

        screen = QApplication.primaryScreen()
        available = screen.availableGeometry() if screen else QRect(0, 0, 1920, 1080)
        width = min(980, max(840, available.width() - 60))
        height = min(700, max(560, available.height() - 60))
        self.setFixedSize(width, height)

        self._build_ui()
        self.sync_from_settings(force=True)
        self._loading = False

        self._status_timer = QTimer(self)
        self._status_timer.setInterval(2500)
        self._status_timer.timeout.connect(self._refresh_follow_badges)
        self._status_timer.start()
        self._refresh_follow_badges()

    # ==================== UI Construction ====================

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(10, 10, 10, 10)

        shell = QFrame()
        shell.setObjectName("SettingsShell")
        outer.addWidget(shell)
        shadow = QGraphicsDropShadowEffect(shell)
        shadow.setBlurRadius(32)
        shadow.setOffset(0, 8)
        shadow.setColor(QColor(0, 0, 0, 60))
        shell.setGraphicsEffect(shadow)

        shell_layout = QVBoxLayout(shell)
        shell_layout.setContentsMargins(0, 0, 0, 0)
        shell_layout.setSpacing(0)

        # ---- 顶栏：标题 + 打开配置文件 + 关闭 ----
        self._header = QWidget()
        self._header.setObjectName("SettingsHeader")
        self._header.setFixedHeight(58)
        header_layout = QHBoxLayout(self._header)
        header_layout.setContentsMargins(22, 10, 14, 4)
        header_layout.setSpacing(8)

        self._title_lbl = QLabel("设置")
        self._title_lbl.setObjectName("WindowTitle")
        header_layout.addWidget(self._title_lbl)
        header_layout.addStretch(1)

        open_config_btn = QPushButton("打开配置文件")
        open_config_btn.setObjectName("GhostBtn")
        open_config_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        open_config_btn.setFixedHeight(30)
        open_config_btn.clicked.connect(self._open_settings_file)
        header_layout.addWidget(open_config_btn)

        close_btn = QPushButton("✕")
        close_btn.setObjectName("CloseBtn")
        close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        close_btn.setFixedSize(30, 30)
        close_btn.setToolTip("关闭设置")
        close_btn.clicked.connect(self.close)
        header_layout.addWidget(close_btn)

        self._header.installEventFilter(self)
        self._title_lbl.installEventFilter(self)
        shell_layout.addWidget(self._header)

        # ---- 主体：左侧分类 + 右侧内容 ----
        body = QWidget()
        body_layout = QHBoxLayout(body)
        body_layout.setContentsMargins(0, 0, 0, 0)
        body_layout.setSpacing(0)

        sidebar = QFrame()
        sidebar.setObjectName("SettingsSidebar")
        sidebar.setFixedWidth(196)
        sidebar_layout = QVBoxLayout(sidebar)
        sidebar_layout.setContentsMargins(10, 4, 10, 14)
        sidebar_layout.setSpacing(2)

        content = QWidget()
        content.setObjectName("SettingsContent")
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(6, 2, 20, 14)
        content_layout.setSpacing(0)

        self._stack = QStackedWidget()
        self._stack.addWidget(self._build_all_page())
        self._stack.addWidget(self._build_general_page())
        self._stack.addWidget(self._build_appearance_page())
        self._stack.addWidget(self._build_character_page())
        self._stack.addWidget(self._build_behavior_page())
        self._stack.addWidget(self._build_chat_page())
        self._stack.addWidget(self._build_startup_page())
        self._stack.addWidget(self._build_logs_page())
        self._stack.addWidget(self._build_about_page())

        self._nav_group = QButtonGroup(self)
        self._nav_group.setExclusive(True)
        self._nav_buttons: list[QPushButton] = []
        for index, (icon_kind, text) in enumerate(self._NAV_ITEMS):
            btn = QPushButton(text)
            btn.setObjectName("NavBtn")
            btn.setCheckable(True)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setIcon(_make_icon(icon_kind))
            btn.setIconSize(QSize(18, 18))
            btn.setMinimumHeight(38)
            btn.clicked.connect(lambda _checked=False, i=index: self._select_page(i))
            self._nav_group.addButton(btn, index)
            self._nav_buttons.append(btn)
            sidebar_layout.addWidget(btn)
        sidebar_layout.addStretch(1)
        self._nav_buttons[self._PAGE_ALL].setChecked(True)

        content_layout.addWidget(self._stack, 1)

        # ---- 底部：状态 + 还原/确认 ----
        footer = QFrame()
        footer.setObjectName("SettingsFooter")
        footer_layout = QHBoxLayout(footer)
        footer_layout.setContentsMargins(2, 10, 0, 0)
        footer_layout.setSpacing(8)

        self._save_feedback_lbl = QLabel("✓ 当前配置已生效")
        self._save_feedback_lbl.setWordWrap(True)
        self._save_feedback_lbl.setStyleSheet(
            "color: #A59EC9; font-size: 12px; background: transparent;"
        )
        footer_layout.addWidget(self._save_feedback_lbl, 1)

        self._discard_btn = QPushButton("还原修改")
        self._discard_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._discard_btn.setEnabled(False)
        self._discard_btn.clicked.connect(self._on_discard_clicked)
        footer_layout.addWidget(self._discard_btn)

        self._confirm_btn = QPushButton("确认修改")
        self._confirm_btn.setObjectName("PrimaryBtn")
        self._confirm_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._confirm_btn.clicked.connect(self.confirm_and_save)
        footer_layout.addWidget(self._confirm_btn)

        content_layout.addWidget(footer)

        body_layout.addWidget(sidebar)
        body_layout.addWidget(content, 1)
        shell_layout.addWidget(body, 1)

    def _new_page(self, title: str, desc: str) -> tuple[QScrollArea, QVBoxLayout]:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        inner = QWidget()
        inner.setObjectName("PageInner")
        layout = QVBoxLayout(inner)
        layout.setContentsMargins(0, 4, 8, 12)
        layout.setSpacing(12)

        title_lbl = QLabel(title)
        title_lbl.setObjectName("PageTitle")
        layout.addWidget(title_lbl)

        desc_lbl = QLabel(desc)
        desc_lbl.setObjectName("PageDesc")
        desc_lbl.setWordWrap(True)
        layout.addWidget(desc_lbl)
        layout.addSpacing(2)

        scroll.setWidget(inner)
        return scroll, layout

    # ---- 全部设置：搜索 + 汇总 ----

    def _build_all_page(self) -> QWidget:
        page = QWidget()
        page_layout = QVBoxLayout(page)
        page_layout.setContentsMargins(0, 4, 0, 0)
        page_layout.setSpacing(10)

        title_lbl = QLabel("全部设置")
        title_lbl.setObjectName("PageTitle")
        page_layout.addWidget(title_lbl)
        desc_lbl = QLabel("所有开关与选项都在这里，搜索后点击即可跳到对应分类。")
        desc_lbl.setObjectName("PageDesc")
        desc_lbl.setWordWrap(True)
        page_layout.addWidget(desc_lbl)

        search_box = QFrame()
        search_box.setObjectName("SearchBox")
        search_box.setFixedHeight(38)
        search_layout = QHBoxLayout(search_box)
        search_layout.setContentsMargins(12, 0, 12, 0)
        search_layout.setSpacing(8)
        search_icon = QLabel()
        search_icon.setFixedSize(16, 16)
        search_icon.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        icon_pixmap = _make_icon("search", size=16, color="#A59EC9").pixmap(QSize(16, 16), 1.0)
        search_icon.setPixmap(icon_pixmap)
        search_layout.addWidget(search_icon, 0, Qt.AlignmentFlag.AlignVCenter)

        self._search_edit = QLineEdit()
        self._search_edit.setObjectName("SearchEdit")
        self._search_edit.setPlaceholderText("搜索设置项，例如：气泡、缩放、API Key")
        self._search_edit.setClearButtonEnabled(True)
        self._search_edit.textChanged.connect(self._filter_summary)
        search_layout.addWidget(self._search_edit, 1)
        page_layout.addWidget(search_box)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        inner = QWidget()
        inner.setObjectName("PageInner")
        layout = QVBoxLayout(inner)
        layout.setContentsMargins(0, 2, 8, 12)
        layout.setSpacing(12)
        scroll.setWidget(inner)
        page_layout.addWidget(scroll, 1)

        card = _Card(layout)
        specs = [
            (self._PAGE_BEHAVIOR, "自动弹出对话气泡", "按需主动说话", "行为互动",
             lambda: self._on_off(self._settings.show_speech_bubble)),
            (self._PAGE_GENERAL, "互动音效", "摸摸头与提示音", "通用设置",
             lambda: self._on_off(self._settings.sound_enabled)),
            (self._PAGE_GENERAL, "电脑状态感知", "低电量打盹与高负载提醒", "通用设置",
             lambda: self._on_off(self._settings.system_monitor_enabled)),
            (self._PAGE_APPEARANCE, "形象版本", "Q 版或标准立绘", "外观形象",
             lambda: "Q 版猫娘" if self._settings.style_mode == PetStyleMode.CHIBI else "标准立绘"),
            (self._PAGE_APPEARANCE, "人物缩放", "桌宠显示大小", "外观形象",
             lambda: f"{self._settings.scale:.2f}x"),
            (self._PAGE_APPEARANCE, "透明度", "整体不透明度", "外观形象",
             lambda: f"{int(round(self._settings.opacity * 100))}%"),
            (self._PAGE_BEHAVIOR, "固定在桌面（自由漫步）", "固定待机或自由漫步", "行为互动",
             lambda: "自由漫步" if self._settings.auto_walk else "已固定"),
            (self._PAGE_BEHAVIOR, "打盹模式", "原地睡觉待机", "行为互动",
             lambda: self._on_off(self._settings.doze_mode)),
            (self._PAGE_BEHAVIOR, "反重力悬浮模式", "常驻漂浮", "行为互动",
             lambda: self._on_off(self._settings.anti_gravity_mode)),
            (self._PAGE_BEHAVIOR, "重力下落与 Q 弹着陆", "松手后自然落地", "行为互动",
             lambda: self._on_off(self._settings.gravity_enabled)),
            (self._PAGE_BEHAVIOR, "四芒星与爱心粒子", "互动粒子特效", "行为互动",
             lambda: self._on_off(self._settings.particles_enabled)),
            (self._PAGE_CHAT, "Gemini API Key", "官方在线对话凭据", "对话与模型",
             lambda: "已配置" if (self._settings.gemini_api_key or "").strip() else "未配置"),
            (self._PAGE_CHAT, "对话模型", "当前使用的 Gemini 模型", "对话与模型",
             lambda: format_model_display_name(self._settings.gemini_model)),
            (self._PAGE_CHAT, "思考强度", "模型推理深度", "对话与模型",
             lambda: format_thinking_level_label(self._settings.thinking_level)),
            (self._PAGE_CHAT, "实时同步 Antigravity 对话", "新消息实时播报到气泡", "对话与模型",
             lambda: self._on_off(self._settings.chat_sync_enabled)),
            (self._PAGE_STARTUP, "Windows 开机自动启动", "登录后静默启动", "启动与联动",
             lambda: self._on_off(self._startup_enabled())),
            (self._PAGE_STARTUP, "跟随 Antigravity 启动", "检测到启动时自动唤醒", "启动与联动",
             lambda: self._on_off(self._settings.follow_antigravity)),
            (self._PAGE_STARTUP, "跟随 Antigravity CLI 启动", "检测到启动时自动唤醒", "启动与联动",
             lambda: self._on_off(self._settings.follow_antigravity_cli)),
            (self._PAGE_STARTUP, "跟随 Gemini 桌面 App 启动", "检测到启动时自动唤醒", "启动与联动",
             lambda: self._on_off(self._settings.follow_gemini_app)),
            (self._PAGE_LOGS, "运行日志", "按日期写入排错日志", "日志与排查",
             lambda: self._on_off(self._settings.enable_runtime_log)),
            (self._PAGE_ABOUT, "启动时自动检查更新", "静默检查上游 Release", "关于与更新",
             lambda: self._on_off(self._settings.auto_check_updates)),
        ]

        for target, title, desc, category, getter in specs:
            row = _SummaryRow(title, desc, category, target)
            row.activated.connect(self._select_page)
            card.layout.addWidget(row)
            haystack = f"{title} {desc} {category}".lower()
            self._summary_rows.append((row, haystack, getter))

        # 右键菜单里的快捷操作也汇入「全部设置」，按所属分类标注
        action_specs = [
            ("自定义角色设定", "修改人设、台词与形象素材", "角色设定", self._handle_open_editor),
            ("角色档案", "查看角色设定与三视图", "角色设定", self._handle_open_profile),
            ("检查更新", "查看更新日志并升级", "关于与更新", self._handle_open_update),
        ]
        for title, desc, category, callback in action_specs:
            row = _SummaryRow(title, desc, category, -1, on_activate=callback)
            row.activated.connect(self._select_page)
            card.layout.addWidget(row)
            self._summary_rows.append((row, f"{title} {desc} {category}".lower(), lambda: "打开"))

        self._summary_empty = QLabel("没有找到匹配的设置项，换个关键词试试。")
        self._summary_empty.setObjectName("EmptyHint")
        self._summary_empty.setContentsMargins(10, 10, 10, 10)
        self._summary_empty.setVisible(False)
        card.layout.addWidget(self._summary_empty)

        layout.addStretch(1)
        return page

    @staticmethod
    def _on_off(flag: bool) -> str:
        return "已开启" if flag else "已关闭"

    def _startup_enabled(self) -> bool:
        try:
            return SettingsService.is_startup_enabled()
        except Exception:
            return False

    def _filter_summary(self, text: str) -> None:
        keyword = (text or "").strip().lower()
        visible = 0
        for row, haystack, _getter in self._summary_rows:
            matched = not keyword or keyword in haystack
            row.setVisible(matched)
            visible += 1 if matched else 0
        self._summary_empty.setVisible(visible == 0)

    def _refresh_summary_values(self) -> None:
        for row, _haystack, getter in self._summary_rows:
            try:
                value = getter()
            except Exception:
                value = "--"
            row.value_label.setText(value)

    def _select_page(self, index: int) -> None:
        if 0 <= index < self._stack.count():
            self._stack.setCurrentIndex(index)
            self._nav_buttons[index].setChecked(True)

    # ---- 通用设置 ----

    def _build_general_page(self) -> QWidget:
        scroll, layout = self._new_page("通用设置", "桌宠互动与状态提醒开关。")

        card = _Card(layout, "互动反馈")
        self._chk_sound = self._make_switch()
        card.add_row(self._chk_sound, "互动音效", "摸摸头、弹跳与提示音的开关")
        self._chk_sysmon = self._make_switch()
        card.add_row(self._chk_sysmon, "电脑状态感知", "低电量时打盹、CPU 占用过高时提醒")

        layout.addStretch(1)
        return scroll

    # ---- 外观形象 ----

    def _build_appearance_page(self) -> QWidget:
        scroll, layout = self._new_page("外观形象", "角色版本、显示尺寸与整体透明度。")

        card = _Card(layout, "角色形象")
        self._style_combo = QComboBox()
        self._style_combo.setFixedWidth(250)
        self._style_combo.addItem("Q 版猫娘形象 (Chibi · 可爱)", PetStyleMode.CHIBI)
        self._style_combo.addItem("标准立绘形象 (Standard · 比例还原)", PetStyleMode.STANDARD)
        self._style_combo.currentIndexChanged.connect(self._on_any_changed)
        card.add_row(self._style_combo, "形象版本", "Q 版更软萌，标准立绘更接近原画比例")

        self._scale_slider = QSlider(Qt.Orientation.Horizontal)
        self._scale_slider.setRange(60, 180)
        self._scale_slider.setSingleStep(5)
        self._scale_slider.setFixedWidth(250)
        self._scale_val_lbl = QLabel("1.00x")
        self._scale_val_lbl.setObjectName("ValueLabel")
        self._scale_val_lbl.setFixedWidth(46)
        self._scale_val_lbl.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        self._scale_slider.valueChanged.connect(self._on_any_changed)
        self._scale_slider.valueChanged.connect(self._on_appearance_preview)
        card.add_row(self._wrap_slider(self._scale_slider, self._scale_val_lbl), "人物缩放", "调整桌宠在桌面上的显示大小")

        self._opacity_slider = QSlider(Qt.Orientation.Horizontal)
        self._opacity_slider.setRange(40, 100)
        self._opacity_slider.setSingleStep(5)
        self._opacity_slider.setFixedWidth(250)
        self._opacity_val_lbl = QLabel("100%")
        self._opacity_val_lbl.setObjectName("ValueLabel")
        self._opacity_val_lbl.setFixedWidth(46)
        self._opacity_val_lbl.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        self._opacity_slider.valueChanged.connect(self._on_any_changed)
        self._opacity_slider.valueChanged.connect(self._on_appearance_preview)
        card.add_row(self._wrap_slider(self._opacity_slider, self._opacity_val_lbl), "透明度", "数值越低越透明")

        layout.addStretch(1)
        return scroll

    # ---- 角色设定 ----

    def _build_character_page(self) -> QWidget:
        """角色设定与角色档案的入口，与桌面右键菜单保持同一套内容。"""
        scroll, layout = self._new_page("角色设定", "自定义人设、台词与形象素材，或查看当前角色档案。")

        card = _Card(layout, "角色内容")
        card.add_row(
            self._make_open_button(self._handle_open_editor),
            "自定义角色设定",
            "修改人设、台词与形象素材",
        )
        card.add_row(
            self._make_open_button(self._handle_open_profile),
            "角色档案",
            "查看角色设定与三视图",
        )

        layout.addStretch(1)
        return scroll

    # ---- 行为互动 ----

    def _build_behavior_page(self) -> QWidget:
        scroll, layout = self._new_page("行为互动", "漫步、悬浮、打盹、气泡与物理特效。")

        card = _Card(layout, "桌面行为")
        self._chk_pin = self._make_switch()
        card.add_row(
            self._chk_pin,
            "固定在桌面（自由漫步）",
            "开启后原地待机不乱跑；关闭时自由漫步",
        )
        self._chk_doze = self._make_switch()
        card.add_row(self._chk_doze, "打盹模式", "停止漫步与闲聊，原地睡觉待机")
        self._chk_antigrav = self._make_switch()
        card.add_row(self._chk_antigrav, "反重力悬浮模式", "脱离地面常驻漂浮")
        self._chk_bubble = self._make_switch()
        card.add_row(self._chk_bubble, "自动弹出对话气泡", "关闭时仅在聊天或重要提示时显示气泡")

        card = _Card(layout, "物理与特效")
        self._chk_gravity = self._make_switch()
        card.add_row(self._chk_gravity, "重力下落与 Q 弹着陆", "拖动松手后自然下落并回弹")
        self._chk_particles = self._make_switch()
        card.add_row(self._chk_particles, "四芒星与爱心粒子", "互动时出现的星芒与爱心特效")

        layout.addStretch(1)
        return scroll

    # ---- 对话与模型 ----

    def _build_chat_page(self) -> QWidget:
        scroll, layout = self._new_page("对话与模型", "Gemini 官方 API Key、对话模型与实时播报。")

        card = _Card(layout, "Gemini 官方 API")
        hint = QLabel("不填写 Key 时仍可离线互动；Key 只保存在本地 user_config.json 中。")
        hint.setObjectName("RowDesc")
        hint.setWordWrap(True)
        card.add_widget(hint, top=2)

        self._api_key_edit = QLineEdit()
        self._api_key_edit.setFixedWidth(320)
        self._api_key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self._api_key_edit.setPlaceholderText("AIza...（API Key 模式使用）")
        self._api_key_edit.textChanged.connect(self._on_api_changed)
        card.add_row(self._api_key_edit, "Gemini API Key", "官方在线对话凭据")

        model_host = QWidget()
        model_host.setObjectName("ControlHost")
        model_row = QHBoxLayout(model_host)
        model_row.setContentsMargins(0, 0, 0, 0)
        model_row.setSpacing(6)
        self._model_combo = QComboBox()
        self._model_combo.setEditable(True)
        self._model_combo.setFixedWidth(220)
        self._model_combo.lineEdit().setPlaceholderText(DEFAULT_GEMINI_MODEL)
        self._model_combo.currentTextChanged.connect(self._on_model_text_changed)
        model_row.addWidget(self._model_combo)
        self._api_refresh_models_btn = QPushButton("获取官方模型")
        self._api_refresh_models_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._api_refresh_models_btn.setToolTip("使用填写的 Gemini API Key 从 Google 官方接口获取模型列表")
        self._api_refresh_models_btn.clicked.connect(self._on_refresh_models_clicked)
        model_row.addWidget(self._api_refresh_models_btn)
        card.add_row(model_host, "对话模型", "获取后可选择官方可用模型")

        self._api_models_hint_lbl = QLabel("")
        self._api_models_hint_lbl.setWordWrap(True)
        self._api_models_hint_lbl.setStyleSheet("color: #A59EC9; font-size: 11px; background: transparent;")
        self._api_models_hint_lbl.setVisible(False)
        card.add_widget(self._api_models_hint_lbl, top=6)

        self._thinking_combo = QComboBox()
        self._thinking_combo.setFixedWidth(250)
        for val, short_label in THINKING_LEVEL_OPTIONS:
            desc_map = {
                "auto": "自动（模型默认动态思考）",
                "off": "Off · 关闭思考（2.5 系列直接关闭，其余使用极简）",
                "minimal": "Minimal · 极简思考（最快响应）",
                "low": "Low · 轻度思考（低延迟与低开销）",
                "medium": "Medium · 均衡思考（推荐日常与编码）",
                "high": "High · 深度思考（复杂推理与数理）",
                "xhigh": "Extra · 极限思考（最大推理深度）",
            }
            self._thinking_combo.addItem(desc_map.get(val, short_label), val)
        self._thinking_combo.currentIndexChanged.connect(self._on_api_changed)
        card.add_row(self._thinking_combo, "思考强度", "越高越适合复杂推理，响应也更慢")

        card = _Card(layout, "对话播报")
        self._chk_ag_sync = self._make_switch()
        card.add_row(
            self._chk_ag_sync,
            "实时同步 Antigravity 对话",
            "跟随 Antigravity 当前对话，把新消息实时播报到气泡",
        )

        layout.addStretch(1)
        return scroll

    # ---- 启动与联动 ----

    def _build_startup_page(self) -> QWidget:
        scroll, layout = self._new_page("启动与联动", "开机自启，以及跟随 Antigravity、Antigravity CLI / Gemini 桌面端唤醒。")

        card = _Card(layout, "联动状态")
        badge_host = QWidget()
        badge_host.setObjectName("CardHost")
        badge_row = QHBoxLayout(badge_host)
        badge_row.setContentsMargins(0, 0, 0, 0)
        badge_row.setSpacing(6)
        self._ag_badge = self._make_status_badge("检测中...")
        self._cli_badge = self._make_status_badge("检测中...")
        self._gem_badge = self._make_status_badge("检测中...")
        self._watch_badge = self._make_status_badge("联动状态检测中...")
        badge_row.addWidget(self._ag_badge)
        badge_row.addWidget(self._gem_badge)
        badge_row.addWidget(self._watch_badge)
        badge_row.addStretch(1)
        card.add_widget(badge_host, top=2)
        card.add_widget(self._cli_badge, top=2)

        desc = QLabel(
            "开启对应开关后，目标应用启动时会自动唤醒桌宠；关闭桌宠后仍可在后台等待下次启动。"
        )
        desc.setObjectName("RowDesc")
        desc.setWordWrap(True)
        card.add_widget(desc, top=6)

        card = _Card(layout, "启动行为")
        self._chk_autostart = self._make_switch()
        card.add_row(self._chk_autostart, "Windows 开机自动启动", "登录系统后静默启动桌宠")
        self._chk_follow_ag = self._make_switch()
        card.add_row(self._chk_follow_ag, "跟随 Antigravity 启动", "检测到 Antigravity 启动时自动唤醒")
        self._chk_follow_cli = self._make_switch()
        card.add_row(self._chk_follow_cli, "跟随 Antigravity CLI 启动", "检测到 Antigravity CLI 启动时自动唤醒")
        self._chk_follow_gem = self._make_switch()
        card.add_row(self._chk_follow_gem, "跟随 Gemini 桌面 App 启动", "检测到 Gemini 启动时自动唤醒")

        sync_btn = QPushButton("启用并同步联动")
        sync_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        sync_btn.clicked.connect(self._on_sync_follow_btn)
        stop_btn = QPushButton("停用应用联动")
        stop_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        stop_btn.clicked.connect(self._on_stop_follow_btn)
        cfg_dir_btn = QPushButton("打开配置目录")
        cfg_dir_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        cfg_dir_btn.clicked.connect(self._open_settings_folder)
        card.add_buttons([sync_btn, stop_btn, cfg_dir_btn])

        layout.addStretch(1)
        return scroll

    # ---- 日志与排查 ----

    def _build_logs_page(self) -> QWidget:
        scroll, layout = self._new_page("日志与排查", "运行日志开关、日志位置与配置文件入口。")

        card = _Card(layout, "运行日志")
        self._chk_log = self._make_switch()
        card.add_row(
            self._chk_log,
            "开启运行日志",
            "按当天日期写入 YYYY-MM-DD.log，遇到异常时可开启后复现问题",
        )
        self._log_path_lbl = QLabel(f"当天日志：{os.path.join(AppLogger.log_directory(), 'YYYY-MM-DD.log')}")
        self._log_path_lbl.setObjectName("RowDesc")
        self._log_path_lbl.setWordWrap(True)
        card.add_widget(self._log_path_lbl, top=2)

        open_log_btn = QPushButton("打开日志目录")
        open_log_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        open_log_btn.clicked.connect(self._open_log_folder)
        card.add_buttons([open_log_btn])

        layout.addStretch(1)
        return scroll

    # ---- 关于与更新 ----

    def _build_about_page(self) -> QWidget:
        scroll, layout = self._new_page("关于与更新", "版本信息、上游追踪源与更新检查。")

        card = _Card(layout, "版本")
        version_host = QWidget()
        version_host.setObjectName("CardHost")
        version_row = QHBoxLayout(version_host)
        version_row.setContentsMargins(0, 0, 0, 0)
        version_row.setSpacing(10)
        ver_badge = QLabel(AppVersion.current_display())
        ver_badge.setStyleSheet(
            "background-color: #2D2357; color: #D8CCFF; border-radius: 6px; padding: 3px 8px; font-size: 11.5px;"
        )
        version_row.addWidget(ver_badge)
        self._upstream_btn = QPushButton()
        self._upstream_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._upstream_btn.setStyleSheet(
            "text-align: left; background: transparent; border: none; color: #9D95C8; "
            "font-size: 11.5px; text-decoration: underline; padding: 0;"
        )
        self._upstream_btn.clicked.connect(
            lambda: QDesktopServices.openUrl(
                QUrl(UpdateService.get_latest_tracking_url(self._settings.update_releases_url))
            )
        )
        version_row.addWidget(self._upstream_btn, 1)
        card.add_widget(version_host, top=2)

        self._chk_autocheck = self._make_switch()
        card.add_row(self._chk_autocheck, "启动时自动检查更新", "静默检查上游最新 Release")

        chk_upd_btn = QPushButton("检查更新与更新日志")
        chk_upd_btn.setObjectName("PrimaryBtn")
        chk_upd_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        chk_upd_btn.clicked.connect(lambda: self._on_open_update() if self._on_open_update else None)
        self._upd_status_lbl = QLabel("始终追踪上游最新版")
        self._upd_status_lbl.setObjectName("RowDesc")

        upd_host = QWidget()
        upd_host.setObjectName("CardHost")
        upd_row = QHBoxLayout(upd_host)
        upd_row.setContentsMargins(0, 0, 0, 0)
        upd_row.setSpacing(10)
        upd_row.addWidget(chk_upd_btn)
        upd_row.addWidget(self._upd_status_lbl, 1)
        card.add_widget(upd_host, top=10)

        layout.addStretch(1)
        return scroll

    # ---- 小组件 ----

    def _make_switch(self) -> ToggleSwitch:
        switch = ToggleSwitch()
        switch.toggled.connect(self._on_any_changed)
        return switch

    @staticmethod
    def _make_open_button(callback: Callable[[], None]) -> QPushButton:
        button = QPushButton("打开")
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.setFixedWidth(64)
        button.clicked.connect(callback)
        return button

    @staticmethod
    def _wrap_slider(slider: QSlider, value_label: QLabel) -> QWidget:
        host = QWidget()
        host.setObjectName("ControlHost")
        row = QHBoxLayout(host)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)
        row.addWidget(slider)
        row.addWidget(value_label)
        return host

    @staticmethod
    def _make_status_badge(text: str) -> QLabel:
        label = QLabel(text)
        label.setStyleSheet(
            "background-color: #252048; color: #C4B5FD; border-radius: 6px; padding: 3px 8px; font-size: 11px;"
        )
        return label

    # ==================== 状态同步 ====================

    def has_unsaved_changes(self) -> bool:
        return bool(self._dirty)

    def _set_feedback(self, text: str, *, kind: str = "neutral") -> None:
        color_map = {
            "neutral": "#A59EC9",
            "dirty": "#FBBF24",
            "success": "#34D399",
            "error": "#F87171",
        }
        color = color_map.get(kind, "#A59EC9")
        weight = "600" if kind != "neutral" else "normal"
        self._save_feedback_lbl.setStyleSheet(
            f"color: {color}; font-size: 12px; font-weight: {weight}; background: transparent;"
        )
        self._save_feedback_lbl.setText(text)

    def _mark_dirty(self) -> None:
        if self._loading:
            return
        self._dirty = True
        self._discard_btn.setEnabled(True)
        self._set_feedback("● 有未保存的修改，请点击「确认修改」生效", kind="dirty")

    def _sync_model_combo(self, selected_model: Optional[str] = None) -> None:
        current = normalize_gemini_model(
            selected_model if selected_model is not None else self._draft_gemini_model
        )
        cached = GeminiChatService.get_cached_api_key_models(self._api_key_edit.text().strip())
        candidates = list(dict.fromkeys([*cached, current]))
        self._model_combo.blockSignals(True)
        try:
            self._model_combo.clear()
            self._model_combo.addItems(candidates)
            self._model_combo.setCurrentText(current)
        finally:
            self._model_combo.blockSignals(False)

    def sync_from_settings(self, force: bool = True, clear_feedback: bool = True) -> None:
        if self._self_updating:
            return
        if not force and self._dirty:
            return
        if force:
            self._cancel_appearance_preview()
        self._loading = True
        try:
            s = self._settings
            self._style_combo.setCurrentIndex(0 if s.style_mode == PetStyleMode.CHIBI else 1)
            self._scale_slider.setValue(int(round(s.scale * 100)))
            self._scale_val_lbl.setText(f"{s.scale:.2f}x")
            self._opacity_slider.setValue(int(round(s.opacity * 100)))
            self._opacity_val_lbl.setText(f"{int(round(s.opacity * 100))}%")

            self._chk_bubble.setChecked(s.show_speech_bubble)
            self._chk_sound.setChecked(s.sound_enabled)
            self._chk_sysmon.setChecked(s.system_monitor_enabled)

            self._chk_pin.setChecked(not s.auto_walk)
            self._chk_doze.setChecked(s.doze_mode)
            self._chk_antigrav.setChecked(s.anti_gravity_mode)
            self._chk_gravity.setChecked(s.gravity_enabled)
            self._chk_particles.setChecked(s.particles_enabled)

            self._api_key_edit.setText(s.gemini_api_key or "")
            self._draft_gemini_model = normalize_gemini_model(s.gemini_model or DEFAULT_GEMINI_MODEL)
            self._sync_model_combo()
            cur_thinking = normalize_thinking_level(getattr(s, "thinking_level", "auto"))
            t_idx = next(
                (
                    i
                    for i in range(self._thinking_combo.count())
                    if self._thinking_combo.itemData(i) == cur_thinking
                ),
                0,
            )
            self._thinking_combo.setCurrentIndex(t_idx)
            self._chk_ag_sync.setChecked(s.chat_sync_enabled)

            self._chk_autostart.setChecked(self._startup_enabled())
            self._chk_follow_ag.setChecked(s.follow_antigravity)
            self._chk_follow_cli.setChecked(s.follow_antigravity_cli)
            self._chk_follow_gem.setChecked(s.follow_gemini_app)

            self._chk_log.setChecked(s.enable_runtime_log)
            self._chk_autocheck.setChecked(s.auto_check_updates)
            self._upstream_btn.setText(
                f"上游追踪源：{UpdateService.normalize_releases_url(s.update_releases_url)}"
            )

            self._refresh_summary_values()
            self._filter_summary(self._search_edit.text())
            self._dirty = False
            self._discard_btn.setEnabled(False)
            if clear_feedback:
                self._set_feedback("✓ 当前配置已生效", kind="neutral")
        finally:
            self._loading = False

    def _on_any_changed(self) -> None:
        if self._loading:
            return
        scale = max(0.6, min(1.8, self._scale_slider.value() / 100.0))
        opacity = max(0.4, min(1.0, self._opacity_slider.value() / 100.0))
        self._scale_val_lbl.setText(f"{scale:.2f}x")
        self._opacity_val_lbl.setText(f"{int(round(opacity * 100))}%")
        self._mark_dirty()

    def _on_appearance_preview(self, _value: int = 0) -> None:
        """拖动缩放/透明度滑杆时实时预览，真正的落盘仍由确认修改完成。"""
        if self._loading or self._on_preview_appearance is None:
            return
        scale = max(0.6, min(1.8, self._scale_slider.value() / 100.0))
        opacity = max(0.4, min(1.0, self._opacity_slider.value() / 100.0))
        self._preview_active = True
        self._on_preview_appearance(scale, opacity)

    def _cancel_appearance_preview(self) -> None:
        """撤销临时预览，让桌宠回到已保存的缩放与透明度。"""
        if not self._preview_active:
            return
        self._preview_active = False
        if self._on_preview_appearance is not None:
            self._on_preview_appearance(None, None)

    def _on_model_text_changed(self, text: str) -> None:
        if self._loading:
            return
        self._draft_gemini_model = text
        self._mark_dirty()

    def _on_api_changed(self) -> None:
        if self._loading:
            return
        self._sync_model_combo(self._model_combo.currentText())
        self._mark_dirty()

    def _commit_ui_to_settings(self) -> None:
        s = self._settings
        # QComboBox 会把 str 枚举数据转成普通 str，需解析回 PetStyleMode 才能正常序列化保存
        s.style_mode = PetStyleMode.parse(self._style_combo.currentData())
        s.scale = max(0.6, min(1.8, self._scale_slider.value() / 100.0))
        s.opacity = max(0.4, min(1.0, self._opacity_slider.value() / 100.0))

        s.show_speech_bubble = self._chk_bubble.isChecked()
        s.sound_enabled = self._chk_sound.isChecked()
        s.system_monitor_enabled = self._chk_sysmon.isChecked()

        s.auto_walk = not self._chk_pin.isChecked()
        s.doze_mode = self._chk_doze.isChecked()
        s.anti_gravity_mode = self._chk_antigrav.isChecked()
        s.gravity_enabled = self._chk_gravity.isChecked()
        s.particles_enabled = self._chk_particles.isChecked()

        self._draft_gemini_model = self._model_combo.currentText()
        s.gemini_api_key = self._api_key_edit.text().strip()
        s.gemini_model = normalize_gemini_model(self._draft_gemini_model)
        s.thinking_level = normalize_thinking_level(self._thinking_combo.currentData())
        s.chat_sync_enabled = self._chk_ag_sync.isChecked()

        s.follow_antigravity = self._chk_follow_ag.isChecked()
        s.follow_antigravity_cli = self._chk_follow_cli.isChecked()
        s.follow_gemini_app = self._chk_follow_gem.isChecked()

        s.auto_check_updates = self._chk_autocheck.isChecked()
        s.enable_runtime_log = self._chk_log.isChecked()

    def confirm_and_save(self) -> bool:
        """校验并提交界面草稿到运行态与 user_config.json，成功/失败均展示明确状态提示。"""
        snapshot = dict(vars(self._settings))
        follow_changed = (
            self._settings.follow_antigravity != self._chk_follow_ag.isChecked()
            or self._settings.follow_antigravity_cli != self._chk_follow_cli.isChecked()
            or self._settings.follow_gemini_app != self._chk_follow_gem.isChecked()
        )
        sync_before = bool(self._settings.chat_sync_enabled)
        want_autostart = self._chk_autostart.isChecked()
        autostart_before = self._startup_enabled()

        self._commit_ui_to_settings()
        try:
            self._settings_service.save(self._settings)
            if follow_changed:
                self._follow_service.sync_and_apply(self._settings)
            AppLogger.set_enabled(self._settings.enable_runtime_log)
        except Exception as exc:
            vars(self._settings).update(snapshot)
            self._cancel_appearance_preview()
            self._dirty = True
            self._discard_btn.setEnabled(True)
            AppLogger.error("[Settings] save failed", exc)
            err_msg = str(exc).strip() or type(exc).__name__
            self._set_feedback(f"❌ 保存失败：{err_msg}", kind="error")
            return False

        # 实时同步保存的是用户意图；暂无可同步会话时回调保留启用状态并等待重试。
        if (
            self._on_antigravity_sync_changed is not None
            and bool(self._settings.chat_sync_enabled) != sync_before
        ):
            self._on_antigravity_sync_changed(bool(self._settings.chat_sync_enabled))

        self.sync_from_settings(force=True, clear_feedback=False)

        # 开机自启属于系统级设置，不写入 user_config.json，按需交给主程序处理后再回读实际状态。
        if want_autostart != autostart_before and self._on_auto_start_changed is not None:
            self._on_auto_start_changed(want_autostart)
            self.sync_from_settings(force=True, clear_feedback=False)

        self._set_feedback("✅ 设置保存成功，已生效", kind="success")
        self._emit_applied()
        self._refresh_follow_badges()
        return True

    def _on_discard_clicked(self) -> None:
        self.sync_from_settings(force=True, clear_feedback=False)
        self._set_feedback("已还原未保存的修改", kind="neutral")

    def _emit_applied(self) -> None:
        self._self_updating = True
        try:
            self._on_applied()
        finally:
            self._self_updating = False

    # ==================== 模型获取 ====================

    def _on_refresh_models_clicked(self) -> None:
        if self._models_refresh_worker is not None and self._models_refresh_worker.isRunning():
            return
        api_key = self._api_key_edit.text().strip()
        if not api_key:
            self._api_models_hint_lbl.setStyleSheet("color: #FBBF24; font-size: 11px; background: transparent;")
            self._api_models_hint_lbl.setText("⚠️ 请先填写 Gemini API Key 后获取官方模型")
            self._api_models_hint_lbl.setVisible(True)
            return

        self._api_refresh_models_btn.setEnabled(False)
        self._api_refresh_models_btn.setText("获取中...")
        self._api_models_hint_lbl.setStyleSheet("color: #A59EC9; font-size: 11px; background: transparent;")
        self._api_models_hint_lbl.setText("正在从 Google 官方接口获取可用对话模型...")
        self._api_models_hint_lbl.setVisible(True)
        worker = _ModelsRefreshWorker(api_key, parent=self)
        worker.completed.connect(self._on_models_refreshed)
        self._models_refresh_worker = worker
        worker.start()

    def _on_models_refreshed(self, models: list[str], err_msg: str) -> None:
        worker = self._models_refresh_worker
        if worker is not None:
            worker.finished.connect(worker.deleteLater)
            if worker.isFinished():
                worker.deleteLater()
            self._models_refresh_worker = None
        self._api_refresh_models_btn.setEnabled(True)
        self._api_refresh_models_btn.setText("获取官方模型")
        # 请求期间若更换了 Key，不把旧 Key 的结果显示在新配置中。
        if worker is not None and worker._api_key != self._api_key_edit.text().strip():
            self._api_models_hint_lbl.setText("API Key 已修改，请重新获取模型")
            return
        if err_msg:
            self._api_models_hint_lbl.setStyleSheet("color: #F87171; font-size: 11px; background: transparent;")
            self._api_models_hint_lbl.setText(f"❌ 获取官方模型失败：{err_msg}")
        elif not models:
            self._api_models_hint_lbl.setStyleSheet("color: #FBBF24; font-size: 11px; background: transparent;")
            self._api_models_hint_lbl.setText("⚠️ 未返回可用对话模型，可保留当前或手动填写模型名")
        else:
            self._sync_model_combo(self._model_combo.currentText())
            self._api_models_hint_lbl.setStyleSheet("color: #34D399; font-size: 11px; background: transparent;")
            self._api_models_hint_lbl.setText(f"✅ 已获取 {len(models)} 个官方可用模型，选择后请点击「确认修改」生效")
        self._api_models_hint_lbl.setVisible(True)

    # ==================== 应用联动 ====================

    def _on_sync_follow_btn(self) -> None:
        self._settings.follow_antigravity = self._chk_follow_ag.isChecked()
        self._settings.follow_antigravity_cli = self._chk_follow_cli.isChecked()
        self._settings.follow_gemini_app = self._chk_follow_gem.isChecked()
        if not AppFollowService.any_follow_enabled(self._settings):
            self._settings.follow_antigravity = True
            self._settings.follow_antigravity_cli = True
            self._settings.follow_gemini_app = True
            self._loading = True
            self._chk_follow_ag.setChecked(True)
            self._chk_follow_cli.setChecked(True)
            self._chk_follow_gem.setChecked(True)
            self._loading = False
        try:
            self._settings_service.save(self._settings)
            self._follow_service.sync_and_apply(self._settings)
        except Exception as exc:
            self._set_feedback(f"❌ 保存失败：{exc}", kind="error")
            return
        self._set_feedback("✅ 已启用应用联动并保存设置", kind="success")
        self._emit_applied()
        self._refresh_follow_badges()
        QMessageBox.information(
            self,
            "已启用应用联动",
            "已通过原生后台守护与用户启动目录快捷方式启用开机静默待机联动（不新建注册表项，也不生成外部 VBS 文件）。当所选的 Antigravity、Antigravity CLI 或 Gemini 桌面端启动时，Gemini 娘会自动出现。",
        )

    def _on_stop_follow_btn(self) -> None:
        self._loading = True
        self._chk_follow_ag.setChecked(False)
        self._chk_follow_cli.setChecked(False)
        self._chk_follow_gem.setChecked(False)
        self._loading = False
        self._settings.follow_antigravity = False
        self._settings.follow_antigravity_cli = False
        self._settings.follow_gemini_app = False
        try:
            self._settings_service.save(self._settings)
            self._follow_service.disable_follow()
        except Exception as exc:
            self._set_feedback(f"❌ 保存失败：{exc}", kind="error")
            return
        self._set_feedback("✅ 已停用应用联动并保存设置", kind="success")
        self._emit_applied()
        self._refresh_follow_badges()

    def _refresh_follow_badges(self) -> None:
        try:
            ag_on = FollowedAppProbe.sample_antigravity_activity().is_running
            cli_on = FollowedAppProbe.sample_antigravity_cli_activity().is_running
            gem_on = FollowedAppProbe.sample_gemini_activity().is_running
            watch_on = AppFollowService.is_follow_watcher_running()
            run_on = AppFollowService.is_follow_watcher_run_enabled()
        except Exception as exc:
            AppLogger.warn(f"[Settings] refresh follow badges failed: {exc}")
            return

        self._ag_badge.setText(f"Antigravity: {'运行中 ✦' if ag_on else '未运行'}")
        self._cli_badge.setText(f"Antigravity CLI: {'运行中 ✦' if cli_on else '未运行'}")
        self._gem_badge.setText(f"Gemini App: {'运行中 ✦' if gem_on else '未运行'}")
        self._watch_badge.setText(
            f"原生守护: {'运行中' if watch_on else ('开机待机已就绪' if run_on else '未启用')}"
        )

    # ==================== 目录与窗口行为 ====================

    def _handle_open_editor(self) -> None:
        if self._on_open_editor:
            self._on_open_editor()

    def _handle_open_profile(self) -> None:
        if self._on_open_profile:
            self._on_open_profile()

    def _handle_open_update(self) -> None:
        if self._on_open_update:
            self._on_open_update()

    def _open_settings_folder(self) -> None:
        os.makedirs(self._settings_service.settings_folder, exist_ok=True)
        os.startfile(self._settings_service.settings_folder)

    def _open_log_folder(self) -> None:
        os.makedirs(AppLogger.log_directory(), exist_ok=True)
        os.startfile(AppLogger.log_directory())

    def _open_settings_file(self) -> None:
        if not os.path.exists(self._settings_service.settings_path):
            self._settings_service.save(self._settings)
        os.startfile(self._settings_service.settings_path)

    def eventFilter(self, watched, event) -> bool:  # noqa: N802 - Qt 命名
        # 无边框窗口：拖动顶栏空白处移动窗口
        if watched in (self._header, self._title_lbl):
            etype = event.type()
            if etype == QEvent.Type.MouseButtonPress and event.button() == Qt.MouseButton.LeftButton:
                self._drag_offset = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
                return True
            if (
                etype == QEvent.Type.MouseMove
                and self._drag_offset is not None
                and event.buttons() & Qt.MouseButton.LeftButton
            ):
                self.move(event.globalPosition().toPoint() - self._drag_offset)
                return True
            if etype == QEvent.Type.MouseButtonRelease:
                self._drag_offset = None
                return True
        return super().eventFilter(watched, event)

    def keyPressEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        if event.key() == Qt.Key.Key_Escape:
            self.close()
            return
        super().keyPressEvent(event)

    def showEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        super().showEvent(event)
        if not self._positioned:
            self._positioned = True
            screen = QApplication.screenAt(QCursor.pos()) or QApplication.primaryScreen()
            if screen is not None:
                area = screen.availableGeometry()
                self.move(area.center() - self.rect().center())
        if not self._status_timer.isActive():
            self._status_timer.start()
        self._refresh_follow_badges()

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        # 关闭未保存的窗口时撤销预览，并把界面草稿还原成已保存配置
        if self._dirty:
            self.sync_from_settings(force=True)
        self._status_timer.stop()
        if self._models_refresh_worker is not None and self._models_refresh_worker.isRunning():
            self._models_refresh_worker.requestInterruption()
            self._models_refresh_worker.wait(1000)
        super().closeEvent(event)
