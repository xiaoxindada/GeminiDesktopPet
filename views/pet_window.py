"""Main translucent desktop pet window with 60fps animations, walk rigs, bubbles, and menus."""

from __future__ import annotations

import ctypes
import math
import os
from collections import deque
from pathlib import Path
import random
import time
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import Optional

from PySide6.QtCore import (
    QEvent,
    QObject,
    QPoint,
    QPointF,
    QRect,
    QRectF,
    QSize,
    Qt,
    QThread,
    QTimer,
    Signal,
)
from PySide6.QtGui import (
    QAction,
    QActionGroup,
    QBitmap,
    QColor,
    QCursor,
    QFont,
    QFontMetrics,
    QGuiApplication,
    QImage,
    QMouseEvent,
    QPainter,
    QPen,
    QPixmap,
    QRegion,
)
from PySide6.QtWidgets import (
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QSplitter,
    QMenu,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from models import (
    CharacterLore,
    CharacterProfile,
    DEFAULT_GEMINI_MODEL,
    DockSide,
    PetSettings,
    PetState,
    PetStyleMode,
    format_model_display_name,
    format_thinking_level_label,
    get_supported_thinking_levels,
    normalize_gemini_model,
    normalize_thinking_level,
    resolve_effective_thinking_level,
)
from services import (
    AppLogger,
    ChatReply,
    GeminiChatService,
    PetSoundEffect,
    SettingsService,
    SoundService,
    SystemMetrics,
    SystemMonitorService,
)
from services.antigravity_chat_collector import AntigravityChatCollector, SyncEvent
from services.antigravity_chat_sync_service import AntigravityChatSyncService
from services.chat_history_service import ChatHistoryService
from services.chat_sync_state import ChatSyncState
from services.shared_menu import MENU_SEPARATOR, populate_shared_menu
from services.sprite_manager import SpriteManager, WalkRigAssets
from themes import STARLIGHT_MENU_QSS


# Win32 Z-order & extended style helpers to update topmost without recreating Qt HWND
_HWND_TOPMOST = -1
_HWND_NOTOPMOST = -2
_SWP_NOMOVE = 0x0002
_SWP_NOSIZE = 0x0001
_SWP_NOACTIVATE = 0x0010
_GWL_EXSTYLE = -20
_WS_EX_TOOLWINDOW = 0x00000080

# 反重力播报：采集间隔、续写刷新的最小间隔（秒）与积压上限。
# 采集和刷新都压在百毫秒级，靠「只解尾部步骤」把开销hold住，延迟才像实时。
_SYNC_POLL_MS = 120
_SYNC_MIN_INTERVAL = 0.25
_SYNC_REPLACE_INTERVAL = 0.12
_SYNC_QUEUE_LIMIT = 10
_SYNC_HEARTBEAT_SEC = 60.0

# 打盹 Zzz 的落点比例（相对贴图内容矩形：横向从中心右偏、纵向从顶部下移）。
# Q 版头部约占贴图高度 1/3、宽度 2/3，0.18H 正好停在头顶右侧发际（参考图效果）；
# 标准立绘头高只有约 1/9（实测 1603px 图高约 175px），沿用 0.18H 会掉到肩颈，
# 所以按头部占比把纵向缩到 0.08H，横向一并收到发际内侧 0.12W。
_DOZE_ZZZ_ANCHOR: dict[PetStyleMode, tuple[float, float]] = {
    PetStyleMode.CHIBI: (0.18, 0.18),
    PetStyleMode.STANDARD: (0.12, 0.08),
}


def _apply_win32_topmost(hwnd: int, topmost: bool) -> None:
    if os.name != "nt" or not hwnd:
        return
    try:
        user32 = ctypes.windll.user32
        insert_after = ctypes.c_void_p(_HWND_TOPMOST if topmost else _HWND_NOTOPMOST)
        user32.SetWindowPos(
            ctypes.c_void_p(hwnd),
            insert_after,
            0,
            0,
            0,
            0,
            _SWP_NOMOVE | _SWP_NOSIZE | _SWP_NOACTIVATE,
        )
    except Exception:
        pass


def _ensure_tool_window_ex_style(hwnd: int) -> None:
    if os.name != "nt" or not hwnd:
        return
    try:
        user32 = ctypes.windll.user32
        ex_style = int(user32.GetWindowLongW(ctypes.c_void_p(hwnd), _GWL_EXSTYLE))
        if not (ex_style & _WS_EX_TOOLWINDOW):
            user32.SetWindowLongW(
                ctypes.c_void_p(hwnd),
                _GWL_EXSTYLE,
                ex_style | _WS_EX_TOOLWINDOW,
            )
    except Exception:
        pass


@dataclass
class _Particle:
    glyph: str
    color: QColor
    x: float
    y: float
    vx: float
    vy: float
    life: float
    max_life: float
    spin: float
    angle: float = 0.0
    font_size: int = 16


class _ChatWorker(QThread):
    """Background worker calling GeminiChatService without freezing the 60fps pet loop."""

    reply_ready = Signal(object, object, object)

    def __init__(
        self,
        chat: GeminiChatService,
        user_input: str,
        settings: PetSettings,
        metrics: SystemMetrics,
        target_info: Optional[tuple] = None,
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        self._chat = chat
        self._user_input = user_input
        self._settings = settings
        self._metrics = metrics
        self._target_info = target_info

    def run(self) -> None:
        try:
            reply = self._chat.ask(self._user_input, self._settings, self._metrics)
        except Exception as ex:
            reply = ChatReply(
                text=f"Gemini 刚才走神了喵：{ex}",
                suggested_state=PetState.OOPS,
            )
        self.reply_ready.emit(reply, self._target_info, self)


class _ChatModelsDiscoveryWorker(QThread):
    """后台通过 API Key 获取官方模型，使用请求开始时的配置快照。"""

    completed = Signal(object, str, bool)

    def __init__(self, chat: GeminiChatService, settings: PetSettings, *, explicit: bool = False, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._chat = chat
        self._settings = replace(settings)
        self._explicit = explicit

    def run(self) -> None:
        try:
            models = self._chat.fetch_models_for_chat(self._settings)
            self.completed.emit(models, "", self._explicit)
        except Exception as exc:
            self.completed.emit([], str(exc), self._explicit)


class _PetStageCanvas(QWidget):
    """Full-window transparent stage painting sprite/walk-rig transforms and particles.

    Keeping one full-window QPainter stage lets levitation (transY < 0), tilt,
    landing squash, and upward particles render without Qt widget-bounds clipping,
    while painting alpha=1 hit-backdrop ONLY inside the active sprite bounds keeps
    surrounding empty sky pixels click-through to the desktop.
    """

    def __init__(self, owner: "PetWindow") -> None:
        super().__init__(owner)
        self._owner = owner
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setMouseTracking(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def paintEvent(self, event) -> None:
        del event
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        self._owner._paint_stage(painter)
        painter.end()

    def mousePressEvent(self, event: QMouseEvent) -> None:
        self._owner._on_stage_mouse_press(event)

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:
        self._owner._on_stage_mouse_double_click(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        self._owner._on_stage_mouse_move(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        self._owner._on_stage_mouse_release(event)


class _SpeechBubbleWindow(QFrame):
    """独立无边框对话与气泡浮窗，支持鼠标拖动八向边缘缩放大小。"""

    RESIZE_MARGIN = 8

    def __init__(self, owner: "PetWindow") -> None:
        flags = Qt.WindowType.FramelessWindowHint | Qt.WindowType.Tool
        if owner._settings.always_on_top:
            flags |= Qt.WindowType.WindowStaysOnTopHint
        super().__init__(owner, flags)
        self._owner = owner
        self.setObjectName("SpeechBubble")
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setMouseTracking(True)

        self._resizing_edges: Optional[tuple[bool, bool, bool, bool]] = None
        self._resize_start_global = QPoint()
        self._resize_start_geom = QRect()

    def is_resizing(self) -> bool:
        return self._resizing_edges is not None

    def minimumSizeHint(self) -> QSize:
        min_w, min_h = self._owner._bubble_min_size()
        return QSize(min_w, min_h)

    def _hit_test_edges(self, pos: QPoint) -> tuple[bool, bool, bool, bool]:
        r = self.rect()
        m = self.RESIZE_MARGIN
        return (
            pos.x() <= m,
            pos.y() <= m,
            pos.x() >= r.width() - m,
            pos.y() >= r.height() - m,
        )

    def _update_resize_cursor(self, pos: QPoint) -> None:
        left, top, right, bottom = self._hit_test_edges(pos)
        if (left and top) or (right and bottom):
            self.setCursor(Qt.CursorShape.SizeFDiagCursor)
        elif (right and top) or (left and bottom):
            self.setCursor(Qt.CursorShape.SizeBDiagCursor)
        elif left or right:
            self.setCursor(Qt.CursorShape.SizeHorCursor)
        elif top or bottom:
            self.setCursor(Qt.CursorShape.SizeVerCursor)
        else:
            self.unsetCursor()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._owner._sync_bubble_window_styles()

    def leaveEvent(self, event) -> None:
        if self._resizing_edges is None:
            self.unsetCursor()
        super().leaveEvent(event)

    def keyPressEvent(self, event) -> None:
        if event.key() == Qt.Key.Key_Escape and self._owner._is_chat_open:
            self._owner._close_bubble_click()
            event.accept()
            return
        super().keyPressEvent(event)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            edges = self._hit_test_edges(event.position().toPoint())
            if any(edges):
                self._resizing_edges = edges
                self._resize_start_global = event.globalPosition().toPoint()
                self._resize_start_geom = self.geometry()
                self._owner._bubble_timer.stop()
                self.grabMouse()
                event.accept()
                return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self._resizing_edges is not None:
            delta = event.globalPosition().toPoint() - self._resize_start_global
            g = QRect(self._resize_start_geom)
            min_w, min_h = self._owner._bubble_min_size()
            area = self._owner._get_work_area(float(g.center().x()), float(g.center().y()))
            max_w, max_h = self._owner._bubble_max_size(float(g.center().x()), float(g.center().y()))
            left, top, right, bottom = self._resizing_edges

            if left:
                new_left = max(area.left() + 4, g.right() + 1 - max_w, min(g.left() + delta.x(), g.right() + 1 - min_w))
                g.setLeft(new_left)
            if right:
                new_right = min(area.right() - 4, g.left() - 1 + max_w, max(g.right() + delta.x(), g.left() - 1 + min_w))
                g.setRight(new_right)
            if top:
                new_top = max(area.top() + 4, g.bottom() + 1 - max_h, min(g.top() + delta.y(), g.bottom() + 1 - min_h))
                g.setTop(new_top)
            if bottom:
                new_bottom = min(area.bottom() - 4, g.top() - 1 + max_h, max(g.bottom() + delta.y(), g.top() - 1 + min_h))
                g.setBottom(new_bottom)

            self.setGeometry(g)
            self._owner._on_bubble_resized_by_user(g)
            event.accept()
            return

        self._update_resize_cursor(event.position().toPoint())
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if self._resizing_edges is not None and event.button() == Qt.MouseButton.LeftButton:
            self._resizing_edges = None
            if QWidget.mouseGrabber() is self:
                self.releaseMouse()
            self._update_resize_cursor(event.position().toPoint())
            self._owner._on_bubble_resize_finished(self.geometry())
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def paintEvent(self, event) -> None:
        del event
        w = self.width()
        h = self.height()
        if w <= 2 or h <= 2:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        # 顶层 WA_TranslucentBackground 窗口在 Qt 中不会自动绘制自身 QSS 背景，显式绘制圆角卡片与边框
        bg_rect = QRectF(0.75, 0.75, float(w) - 1.5, float(h) - 1.5)
        painter.setBrush(QColor(23, 19, 45, 242))
        painter.setPen(QPen(QColor("#7C5CFF"), 1.5))
        painter.drawRoundedRect(bg_rect, 14.0, 14.0)

        if w >= 40 and h >= 40:
            grip_pen = QPen(QColor(167, 139, 250, 165), 1.3)
            grip_pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            painter.setPen(grip_pen)
            # 右下角绘制极简缩放指示纹，提示八向边缘均可拖拽缩放
            painter.drawLine(w - 11, h - 5, w - 5, h - 11)
            painter.drawLine(w - 8, h - 5, w - 5, h - 8)
        painter.end()


def _draw_pixmap_uniform(painter: QPainter, pixmap: Optional[QPixmap], target: QRectF) -> None:
    """按原始宽高比将 QPixmap 等比缩放并居中绘制到目标矩形内。"""
    if pixmap is None or pixmap.isNull() or target.width() <= 0 or target.height() <= 0:
        return
    pw = float(pixmap.width())
    ph = float(pixmap.height())
    if pw <= 0 or ph <= 0:
        return
    ratio = min(target.width() / pw, target.height() / ph)
    dw = pw * ratio
    dh = ph * ratio
    dx = target.x() + (target.width() - dw) * 0.5
    dy = target.y() + (target.height() - dh) * 0.5
    painter.drawPixmap(QRectF(dx, dy, dw, dh), pixmap, QRectF(0.0, 0.0, pw, ph))


class PetWindow(QWidget):
    """桌面宠物主窗口，负责透明角色渲染、交互物理循环、菜单与状态联动。"""

    open_profile_requested = Signal()
    open_character_editor_requested = Signal()
    open_settings_requested = Signal()
    open_quota_requested = Signal()
    open_update_requested = Signal()
    settings_synced = Signal()
    follow_antigravity_cli_requested = Signal(bool)
    visual_bounds_changed = Signal()
    exit_requested = Signal()

    def __init__(
        self,
        settings: PetSettings,
        settings_service: SettingsService,
        sprites: SpriteManager,
        sound: SoundService,
        sys_monitor: SystemMonitorService,
        chat: GeminiChatService,
    ) -> None:
        super().__init__(None)
        self._settings = settings
        self._settings_service = settings_service
        # 设置窗口拖动缩放/透明度滑杆时的临时预览值，不写回配置
        self._preview_scale: Optional[float] = None
        self._preview_opacity: Optional[float] = None
        # 贴图实际内容边界（去掉透明留白），按 pixmap 缓存
        self._sprite_content_cache: dict[int, QRect] = {}
        # 未保存尺寸时使用最大预设，已手动调整的尺寸继续保留。
        _, default_chat_width, default_chat_height = self._CHAT_WINDOW_SIZE_PRESETS[-1]
        if settings.chat_width is None:
            settings.chat_width = default_chat_width
        if settings.chat_height is None:
            settings.chat_height = default_chat_height
        self._chat_archive = ChatHistoryService(settings_service.settings_path.parent)
        self._history_expanded = False
        self._archive_error = ""
        self._chat_readonly = False
        self._ag_sync_service = AntigravityChatSyncService()
        self._ag_sync_state = ChatSyncState()
        self._ag_collector = AntigravityChatCollector(self._ag_sync_service, self._ag_sync_state)
        self._ag_mirror_active = False
        # 消费端只认状态叶子差量：基线记各叶子的编号，队列按节奏逐条播。
        self._ag_baseline: dict[str, int] = {}
        self._ag_playback: deque[SyncEvent] = deque()
        self._ag_last_shown = 0.0
        self._ag_last_replace = 0.0
        # 同步气泡的独占期：这段时间内闲聊不来抢同一个气泡。
        self._sync_guard_until = 0.0
        self._sync_thinking_visible = False
        self._sync_thinking_pinned = False
        # 同步播报时的固定气泡宽度：不锁宽的话每长一个字就重排一次，看着像在抖。
        self._sync_bubble_width: Optional[int] = None
        # 心跳：按时间写日志，确认轮询是否真的在跑。
        self._ag_dbg_ticks = 0
        self._ag_dbg_last = time.monotonic()
        self._ag_sync_timer = QTimer(self)
        self._ag_sync_timer.setInterval(_SYNC_POLL_MS)
        self._ag_sync_timer.timeout.connect(self._refresh_antigravity_sync)
        self._ag_sync_timer.start()
        self._sprites = sprites
        self._sound = sound
        self._sys_monitor = sys_monitor
        self._chat = chat

        # 无边框透明置顶工具窗口属性配置
        flags = (
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.Tool
        )
        if settings.always_on_top:
            flags |= Qt.WindowType.WindowStaysOnTopHint
        self.setWindowFlags(flags)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, False)
        self.setMouseTracking(True)
        self.setWindowTitle("Gemini 娘")

        # Animation & particle state
        self._particles: list[_Particle] = []
        self._anim_time = 0.0
        self._last_tick_utc = datetime.now(timezone.utc)

        # Sprite & behavioral state
        self._current_state = PetState.IDLE
        self._override_pose: Optional[PetState] = None
        self._override_until_utc = datetime.min.replace(tzinfo=timezone.utc)
        self._active_static_pixmap: Optional[QPixmap] = None
        self._use_chibi_walk_rig = False
        self._use_std_walk_rig = False
        self._is_walking = False
        self._walk_dir = 1  # 1 = right, -1 = left
        self._is_sleeping = False
        # 已生效的打盹模式状态，用于检测开关变化并播放入睡/睡醒过渡
        self._doze_applied = bool(settings.doze_mode)
        self._is_sulk = False
        self._sulk_pats_needed = 0

        # Transform state updated every frame
        self._sprite_scale_x = 1.0
        self._sprite_scale_y = 1.0
        self._sprite_angle = 0.0
        self._sprite_trans_x = 0.0
        self._sprite_trans_y = 0.0
        self._chibi_kinematics = {
            "leg_l_rot": 0.0,
            "leg_l_ty": 0.0,
            "leg_r_rot": 0.0,
            "leg_r_ty": 0.0,
            "arm_l_rot": 0.0,
            "arm_r_rot": 0.0,
            "body_ty": 0.0,
            "body_rot": 0.0,
        }
        self._std_kinematics = {
            "front_hip": 0.0,
            "front_knee": 0.0,
            "back_hip": 0.0,
            "back_knee": 0.0,
            "body_ty": 0.0,
            "body_rot": 0.0,
        }

        # Drag, shake & gravity physics
        self._mouse_down = False
        self._is_dragging = False
        self._drag_start_global = QPointF()
        self._win_start_left = 0.0
        self._win_start_top = 0.0
        self._last_move_global = QPointF()
        self._last_move_utc = datetime.now(timezone.utc)
        self._prev_vx = 0.0
        self._shake_energy = 0.0
        self._is_falling = False
        self._fall_velocity_y = 0.0
        self._landing_squash = 0.0

        # Headpat tracking
        self._last_pat_local = QPointF()
        self._pat_travel = 0.0
        self._last_pat_sound_utc = datetime.min.replace(tzinfo=timezone.utc)

        # Hover & bubble/chat state
        self._is_hovering = False
        self._is_bubble_hovered = False
        self._suppress_loaded_default_greeting = False
        self._is_forced_bubble_active = False
        self._is_chat_open = False
        self._transient_bubble_size: Optional[QSize] = None
        self._bubble_custom_offset: Optional[QPoint] = None
        self._last_monitor_bubble_utc = datetime.min.replace(tzinfo=timezone.utc)
        self._first_shown = False
        self._closing_handled = False
        self._active_chat_workers: list[_ChatWorker] = []
        self._waiting_chat_workers: set[_ChatWorker] = set()
        self._chat_models_discovery_worker: Optional[_ChatModelsDiscoveryWorker] = None
        self._chat_message_rows: list[QWidget] = []
        self._chat_model_menu: Optional[QMenu] = None

        # Build UI layers, menu, and timers
        self._ui_ready = False
        self._build_ui()
        self._build_context_menu()
        self._build_timers()
        self._ui_ready = True
        self._speech_bubble.installEventFilter(self)
        for child in self._speech_bubble.findChildren(QWidget):
            child.installEventFilter(self)
        self._chat_input.installEventFilter(self)

    # ==================== UI Construction ====================

    def _build_ui(self) -> None:
        self._stage_canvas = _PetStageCanvas(self)

        # --- Floating Resizable Speech Bubble / Chat Window ---
        self._speech_bubble = _SpeechBubbleWindow(self)
        self._speech_bubble.setFrameShape(QFrame.Shape.NoFrame)

        self._bubble_layout = QVBoxLayout(self._speech_bubble)
        self._bubble_layout.setContentsMargins(12, 10, 12, 10)
        self._bubble_layout.setSpacing(6)

        self._bubble_header_widget = QWidget(self._speech_bubble)
        self._bubble_header_widget.setStyleSheet("background: transparent;")
        self._bubble_header_widget.setCursor(Qt.CursorShape.ArrowCursor)
        header_row = QHBoxLayout(self._bubble_header_widget)
        header_row.setContentsMargins(0, 0, 0, 0)
        header_row.setSpacing(0)

        star_lbl = QLabel("✦")
        star_lbl.setStyleSheet("color: #A78BFA; font-size: 11px; background: transparent;")
        header_row.addWidget(star_lbl)
        header_row.addSpacing(5)

        self._bubble_char_name_lbl = QLabel("Gemini 娘")
        self._bubble_char_name_lbl.setStyleSheet(
            "color: #C4B5FD; font-size: 11px; font-weight: bold; background: transparent;"
        )
        header_row.addWidget(self._bubble_char_name_lbl)

        self._bond_badge_lbl = QLabel(" · ♥ 20")
        self._bond_badge_lbl.setStyleSheet(
            "color: #F472B6; font-size: 10.5px; background: transparent;"
        )
        header_row.addWidget(self._bond_badge_lbl)

        self._chat_channel_badge = QPushButton("离线模式")
        self._chat_channel_badge.setObjectName("ChatChannelBadge")
        self._chat_channel_badge.setCursor(Qt.CursorShape.PointingHandCursor)
        self._chat_channel_badge.setToolTip("Gemini API Key 连接状态（点击获取模型或打开设置）")
        self._chat_channel_badge.setStyleSheet(
            "QPushButton#ChatChannelBadge { "
            "background-color: rgba(251, 191, 36, 36); color: #FDE68A; "
            "border: 1px solid rgba(251, 191, 36, 90); border-radius: 7px; "
            "font-size: 9.5px; font-weight: bold; padding: 1px 7px; margin-left: 6px; }"
        )
        self._chat_channel_badge.clicked.connect(self._open_chat_connection_menu)
        self._chat_channel_badge.setVisible(False)
        self._last_chat_api_notice: str = ""
        header_row.addWidget(self._chat_channel_badge)
        header_row.addStretch(1)

        self._chat_clear_btn = QPushButton("＋")
        self._chat_clear_btn.setFixedSize(18, 18)
        self._chat_clear_btn.setToolTip("开始新对话（旧会话保存在历史对话中）")
        self._chat_clear_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._chat_clear_btn.setStyleSheet(
            """
            QPushButton {
                background: transparent;
                color: #9B92C7;
                border: none;
                font-size: 11px;
                padding: 0px;
            }
            QPushButton:hover {
                color: #FFFFFF;
            }
            """
        )
        self._chat_clear_btn.clicked.connect(self._clear_chat_history)
        self._chat_clear_btn.setVisible(False)
        header_row.addWidget(self._chat_clear_btn)

        self._bubble_settings_btn = QPushButton("⚙")
        self._bubble_settings_btn.setFixedSize(18, 18)
        self._bubble_settings_btn.setToolTip("设置 Gemini 官方 API Key")
        self._bubble_settings_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._bubble_settings_btn.setStyleSheet(
            """
            QPushButton {
                background: transparent;
                color: #9B92C7;
                border: none;
                font-size: 11px;
                padding: 0px;
            }
            QPushButton:hover {
                color: #FFFFFF;
            }
            """
        )
        self._bubble_settings_btn.clicked.connect(self.open_settings_requested.emit)
        self._bubble_settings_btn.setVisible(False)
        header_row.addWidget(self._bubble_settings_btn)

        close_bubble_btn = QPushButton("✕")
        close_bubble_btn.setFixedSize(18, 18)
        close_bubble_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        close_bubble_btn.setStyleSheet(
            """
            QPushButton {
                background: transparent;
                color: #9B92C7;
                border: none;
                font-size: 10px;
                padding: 0px;
            }
            QPushButton:hover {
                color: #FFFFFF;
            }
            """
        )
        close_bubble_btn.clicked.connect(self._close_bubble_click)
        header_row.addWidget(close_bubble_btn)
        self._bubble_layout.addWidget(self._bubble_header_widget)

        # Compact Chat Mode Model & Size Bar
        self._chat_toolbar = QWidget(self._speech_bubble)
        self._chat_toolbar.setStyleSheet("background: transparent;")
        self._chat_toolbar.setCursor(Qt.CursorShape.ArrowCursor)
        toolbar_row = QHBoxLayout(self._chat_toolbar)
        toolbar_row.setContentsMargins(0, 0, 0, 0)
        toolbar_row.setSpacing(6)

        self._chat_model_btn = QPushButton(
            f"{format_model_display_name(DEFAULT_GEMINI_MODEL)} · 自动   ▾", self._chat_toolbar
        )
        self._chat_model_btn.setObjectName("ChatModelBtn")
        self._chat_model_btn.setToolTip("点击切换当前对话模型与思考强度")
        self._chat_model_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._chat_model_btn.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._chat_model_btn.setStyleSheet(
            """
            QPushButton#ChatModelBtn {
                background-color: #16112C;
                color: #F4F1FF;
                border: 1px solid #4E3F8E;
                border-radius: 7px;
                padding: 3px 9px;
                font-size: 11px;
                text-align: left;
            }
            QPushButton#ChatModelBtn:hover {
                border-color: #7C5CFF;
                background-color: #1F183D;
            }
            QPushButton#ChatModelBtn:pressed {
                background-color: #2B2154;
            }
            """
        )
        self._chat_model_btn.clicked.connect(self._open_chat_model_menu)
        toolbar_row.addWidget(self._chat_model_btn, 1)

        self._chat_size_btn = QPushButton("📐 小", self._chat_toolbar)
        self._chat_size_btn.setObjectName("ChatSizeBtn")
        self._chat_size_btn.setToolTip("切换聊天窗口大小：小 / 中 / 大（也可拖拽边缘自由缩放）")
        self._chat_size_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._chat_size_btn.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self._chat_size_btn.setStyleSheet(
            """
            QPushButton#ChatSizeBtn {
                background-color: #16112C;
                color: #D8CCFF;
                border: 1px solid #4E3F8E;
                border-radius: 7px;
                padding: 3px 8px;
                font-size: 10.5px;
            }
            QPushButton#ChatSizeBtn:hover {
                border-color: #7C5CFF;
                color: #FFFFFF;
                background-color: #1F183D;
            }
            """
        )
        self._chat_size_btn.clicked.connect(self.cycle_chat_window_size)
        toolbar_row.addWidget(self._chat_size_btn)

        self._chat_toolbar.setVisible(False)
        self._bubble_layout.addWidget(self._chat_toolbar)

        self._chat_session_bar = QWidget(self._speech_bubble)
        self._chat_session_bar.setStyleSheet("background: transparent;")
        session_row = QHBoxLayout(self._chat_session_bar)
        session_row.setContentsMargins(0, 0, 0, 0)
        session_row.setSpacing(6)
        self._history_btn = QPushButton("☰ 历史对话")
        self._history_btn.setCheckable(True)
        self._history_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._history_btn.clicked.connect(self._toggle_chat_history)
        self._new_chat_btn = QPushButton("＋ 新对话")
        self._new_chat_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._new_chat_btn.clicked.connect(self._clear_chat_history)
        self._chat_session_bar.setStyleSheet("""
            QWidget { background: transparent; }
            QPushButton { background: #16112C; color: #D8CCFF; border: 1px solid #4E3F8E;
                          border-radius: 7px; padding: 4px 9px; font-size: 11px; }
            QPushButton:hover, QPushButton:checked { background: #35285E; border-color: #7C5CFF; }
            QPushButton:disabled { color: #73698E; }
        """)
        session_row.addWidget(self._history_btn)
        self._task_folder_btn = QPushButton("选择任务文件夹")
        self._task_folder_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._task_folder_btn.setToolTip("为当前会话关联任务文件夹")
        self._task_folder_btn.clicked.connect(self._choose_task_folder)
        session_row.addWidget(self._task_folder_btn)
        session_row.addStretch(1)
        session_row.addWidget(self._new_chat_btn)
        self._chat_session_bar.hide()
        self._bubble_layout.addWidget(self._chat_session_bar)

        # 列表与原消息卡片共用同一窗口；收起列表后恢复紧凑聊天布局。
        self._chat_splitter = QSplitter(Qt.Orientation.Horizontal, self._speech_bubble)
        self._chat_splitter.setChildrenCollapsible(False)
        self._chat_splitter.setHandleWidth(6)
        self._chat_splitter.setStyleSheet("QSplitter::handle { background: #352B55; }")
        self._history_list = QListWidget(self._chat_splitter)
        self._history_list.setMinimumWidth(140)
        self._history_list.setMaximumWidth(240)
        self._history_list.setTextElideMode(Qt.TextElideMode.ElideRight)
        self._history_list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._history_list.setStyleSheet("""
            QListWidget { background: #19142C; color: #CEC3ED; border: 1px solid #4E3F8E;
                          border-radius: 8px; outline: none; font-size: 11px; }
            QListWidget::item { padding: 10px 7px; border-bottom: 1px solid #302746; }
            QListWidget::item:selected { background: #443371; color: #FFFFFF; }
            QListWidget::item:hover { background: #302448; }
        """)
        self._history_list.currentItemChanged.connect(self._select_chat_session)
        self._history_list.hide()

        self._bubble_scroll = QScrollArea(self._speech_bubble)
        self._bubble_scroll.setWidgetResizable(True)
        self._bubble_scroll.setMinimumHeight(24)
        self._bubble_scroll.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self._bubble_scroll.setCursor(Qt.CursorShape.ArrowCursor)
        self._bubble_scroll.viewport().setCursor(Qt.CursorShape.ArrowCursor)
        self._bubble_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._bubble_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._bubble_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self._bubble_scroll.setStyleSheet(
            """
            QScrollArea { background: transparent; border: none; }
            QScrollArea > QWidget > QWidget { background: transparent; }
            QScrollBar:vertical {
                background: transparent;
                width: 4px;
                margin: 2px 0px;
            }
            QScrollBar::handle:vertical {
                background: #4E3F8E;
                border-radius: 2px;
                min-height: 16px;
            }
            QScrollBar::handle:vertical:hover {
                background: #6D4AFF;
            }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
                height: 0px;
            }
            QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {
                background: transparent;
            }
            """
        )

        self._bubble_content = QWidget()
        self._bubble_content.setStyleSheet("background: transparent;")
        self._bubble_content_layout = QVBoxLayout(self._bubble_content)
        self._bubble_content_layout.setContentsMargins(0, 0, 0, 0)
        self._bubble_content_layout.setSpacing(6)

        self._bubble_text_lbl = QLabel("喵～Gemini 上线！")
        self._bubble_text_lbl.setTextFormat(Qt.TextFormat.PlainText)
        self._bubble_text_lbl.setWordWrap(True)
        self._bubble_text_lbl.setCursor(Qt.CursorShape.ArrowCursor)
        self._bubble_text_lbl.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self._bubble_text_lbl.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        self._bubble_text_lbl.setStyleSheet(
            "color: #F6F3FF; font-size: 12.5px; padding: 1px 4px 2px 0px; background: transparent;"
        )
        self._bubble_content_layout.addWidget(self._bubble_text_lbl)

        self._chat_messages_panel = QWidget(self._bubble_content)
        self._chat_messages_panel.setStyleSheet("background: transparent;")
        self._chat_messages_layout = QVBoxLayout(self._chat_messages_panel)
        self._chat_messages_layout.setContentsMargins(0, 2, 4, 2)
        self._chat_messages_layout.setSpacing(8)
        self._chat_messages_panel.setVisible(False)
        self._bubble_content_layout.addWidget(self._chat_messages_panel)
        self._bubble_content_layout.addStretch(1)

        self._bubble_scroll.setWidget(self._bubble_content)
        self._chat_splitter.addWidget(self._bubble_scroll)
        self._chat_splitter.setStretchFactor(1, 1)
        self._bubble_layout.addWidget(self._chat_splitter, 1)

        # Inline Chat Input Panel
        self._chat_input_panel = QWidget(self._speech_bubble)
        self._chat_input_panel.setStyleSheet("background: transparent;")
        self._chat_input_panel.setCursor(Qt.CursorShape.ArrowCursor)
        chat_row = QHBoxLayout(self._chat_input_panel)
        chat_row.setContentsMargins(0, 2, 0, 0)
        chat_row.setSpacing(6)

        self._chat_input = QLineEdit(self._chat_input_panel)
        self._chat_input.setMinimumWidth(140)
        self._chat_input.setPlaceholderText("输入聊天内容或指令…")
        self._chat_input.setStyleSheet(
            """
            QLineEdit {
                background-color: #100C20;
                color: #FFFFFF;
                border: 1px solid #544399;
                border-radius: 6px;
                padding: 5px 7px;
                font-size: 12px;
            }
            QLineEdit:focus {
                border-color: #7C5CFF;
            }
            """
        )
        self._chat_input.returnPressed.connect(self._submit_chat)
        chat_row.addWidget(self._chat_input, 1)

        send_btn = QPushButton("发送", self._chat_input_panel)
        send_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        send_btn.setStyleSheet(
            """
            QPushButton {
                background-color: #6D4AFF;
                color: #FFFFFF;
                border: 1px solid #7C5CFF;
                border-radius: 10px;
                padding: 4px 10px;
                font-size: 11px;
            }
            QPushButton:hover { background-color: #7C5CFF; }
            QPushButton:pressed { background-color: #5437D6; }
            """
        )
        send_btn.clicked.connect(self._submit_chat)
        chat_row.addWidget(send_btn)

        self._chat_input_panel.setVisible(False)
        self._bubble_layout.addWidget(self._chat_input_panel)

        self._chat_path_panel = QWidget(self._speech_bubble)
        path_layout = QVBoxLayout(self._chat_path_panel)
        path_layout.setContentsMargins(0, 0, 0, 0)
        path_layout.setSpacing(2)
        self._chat_save_status = QLabel("当前对话保存路径 · 首条消息后保存")
        self._chat_save_status.setStyleSheet("color: #A89FD6; font-size: 10px; background: transparent;")
        path_layout.addWidget(self._chat_save_status)
        self._chat_path = QLineEdit()
        self._chat_path.setReadOnly(True)
        self._chat_path.setMinimumWidth(0)
        self._chat_path.setStyleSheet(
            "QLineEdit { color: #A89FD6; background: #19142C; border: none; "
            "border-radius: 4px; padding: 3px 5px; font-size: 10px; }"
        )
        self._chat_path.setToolTip("当前会话文件；可选中复制，长路径可左右滚动查看")
        path_layout.addWidget(self._chat_path)
        task_row = QHBoxLayout()
        task_row.setContentsMargins(0, 0, 0, 0)
        task_label = QLabel("任务文件夹")
        task_label.setStyleSheet("color: #A89FD6; font-size: 10px; background: transparent;")
        task_row.addWidget(task_label)
        self._task_folder_path = QLineEdit()
        self._task_folder_path.setReadOnly(True)
        self._task_folder_path.setMinimumWidth(0)
        self._task_folder_path.setPlaceholderText("尚未选择")
        self._task_folder_path.setStyleSheet(self._chat_path.styleSheet())
        task_row.addWidget(self._task_folder_path, 1)
        path_layout.addLayout(task_row)
        self._chat_path_panel.hide()
        self._bubble_layout.addWidget(self._chat_path_panel)
        self._update_chat_save_path()


        self._speech_bubble.setVisible(False)

    def _build_context_menu(self) -> None:
        # 桌面菜单与托盘菜单共用同一批 QAction，状态天然同步，不再维护两套实现。
        self._scale_actions: dict[float, QAction] = {}
        for lbl, val in [
            ("小巧 (0.75x)", 0.75),
            ("标准 (1.00x)", 1.0),
            ("放大 (1.30x)", 1.3),
            ("超大 (1.60x)", 1.6),
        ]:
            action = QAction(lbl, self)
            action.setCheckable(True)
            action.triggered.connect(lambda _checked=False, sc=val: self._on_menu_scale(sc))
            self._scale_actions[val] = action

        self._style_actions: dict[PetStyleMode, QAction] = {}
        style_group = QActionGroup(self)
        style_group.setExclusive(True)
        for mode, label in (
            (PetStyleMode.CHIBI, "Q 版猫娘形象"),
            (PetStyleMode.STANDARD, "标准立绘形象"),
        ):
            action = QAction(label, self)
            action.setCheckable(True)
            action.setActionGroup(style_group)
            action.triggered.connect(lambda _checked=False, m=mode: self._set_style_mode(m))
            self._style_actions[mode] = action

        # 聊天模式入口：桌面右键与托盘共用同一个 QAction，避免两套菜单文案走样
        self._act_chat = self._new_menu_action(
            "💬 聊天模式 / 选模型对话",
            slot=self.open_chat_mode,
            tip="打开 AI 聊天模式（支持多轮对话与模型选择）",
        )
        self._act_pin_on_desktop = self._new_menu_action(
            "📌 固定（自由漫步）",
            checkable=True,
            slot=self._on_menu_pin_on_desktop,
            tip="勾选后固定在桌面不乱跑；取消勾选恢复自由漫步",
        )
        self._act_doze = self._new_menu_action(
            "💤 打盹模式",
            checkable=True,
            slot=self._on_menu_doze_mode,
            tip="原地睡觉待机，停止漫步与闲聊",
        )
        self._act_show_bubble = self._new_menu_action(
            "💭 自动弹出对话气泡",
            checkable=True,
            slot=self._on_menu_show_speech_bubble,
            tip="关闭后仅在聊天或重要提示时显示气泡",
        )
        self._act_sync_chat = self._new_menu_action(
            "💬 实时同步反重力对话",
            checkable=True,
            slot=self._on_menu_sync_chat,
            tip="把 Antigravity 的新消息实时播报到气泡",
        )
        self._act_follow_cli = self._new_menu_action(
            "🔗 跟随 Antigravity CLI 启动",
            checkable=True,
            slot=self.follow_antigravity_cli_requested,
            tip="Antigravity CLI 启动时自动唤醒桌宠",
        )
        self._act_runtime_log = self._new_menu_action(
            "📝 记录运行日志",
            checkable=True,
            slot=self._on_menu_runtime_log,
            tip="按当天日期写入 YYYY-MM-DD.log，排错时开启",
        )
        self._act_quota = self._new_menu_action(
            "📊 查看反重力额度", slot=self.open_quota_requested
        )
        self._act_reset_position = self._new_menu_action(
            "📍 回到屏幕右下角", slot=self.reset_to_bottom_right
        )
        self._act_editor = self._new_menu_action(
            "✏️ 自定义角色设定...", slot=self.open_character_editor_requested
        )
        self._act_profile = self._new_menu_action(
            "📖 角色档案", slot=self.open_profile_requested
        )
        self._act_update = self._new_menu_action("🆕 检查更新...", slot=self.open_update_requested)
        self._act_update.setData("update")
        self._act_settings = self._new_menu_action("⚙️ 设置...", slot=self.open_settings_requested)
        self._act_exit = self._new_menu_action("🌙 让 Gemini 娘休息（退出）", slot=self.exit_requested)

        menu = QMenu(self)
        menu.setStyleSheet(STARLIGHT_MENU_QSS)
        populate_shared_menu(menu, self.shared_menu_spec())
        menu.aboutToShow.connect(self._sync_context_menu_checks)
        self._context_menu = menu

    def _new_menu_action(
        self,
        text: str,
        *,
        checkable: bool = False,
        slot: Optional[object] = None,
        tip: str = "",
    ) -> QAction:
        action = QAction(text, self)
        action.setCheckable(checkable)
        if tip:
            action.setToolTip(tip)
        if slot is not None:
            action.triggered.connect(slot)
        return action

    def shared_menu_spec(self) -> list[object]:
        """桌面菜单与托盘菜单共用的动作清单（QAction 实例由两边共享）。"""
        return [
            self._act_chat,
            self._act_pin_on_desktop,
            self._act_reset_position,
            self._act_doze,
            self._act_show_bubble,
            self._act_sync_chat,
            self._act_follow_cli,
            MENU_SEPARATOR,
            ("🎀 形象切换", list(self._style_actions.values())),
            ("📏 缩放大小", list(self._scale_actions.values())),
            self._act_runtime_log,
            self._act_quota,
            MENU_SEPARATOR,
            self._act_editor,
            self._act_profile,
            self._act_update,
            self._act_settings,
            MENU_SEPARATOR,
            self._act_exit,
        ]

    @property
    def shared_update_action(self) -> QAction:
        return self._act_update

    def sync_shared_menu(self) -> None:
        """托盘菜单弹出前也调用这里，保证与桌面菜单勾选一致。"""
        self._sync_context_menu_checks()

    def _build_timers(self) -> None:
        self._frame_timer = QTimer(self)
        self._frame_timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._frame_timer.setInterval(16)
        self._frame_timer.timeout.connect(self._frame_timer_tick)

        self._ai_timer = QTimer(self)
        self._ai_timer.setInterval(4500)
        self._ai_timer.timeout.connect(self._ai_timer_tick)

        self._bubble_timer = QTimer(self)
        self._bubble_timer.setSingleShot(True)
        self._bubble_timer.timeout.connect(self._on_bubble_timeout)

        self._monitor_timer = QTimer(self)
        self._monitor_timer.setInterval(30000)
        self._monitor_timer.timeout.connect(self._monitor_timer_tick)

        self._hover_grace_timer = QTimer(self)
        self._hover_grace_timer.setSingleShot(True)
        self._hover_grace_timer.setInterval(380)
        self._hover_grace_timer.timeout.connect(self._on_hover_grace_timeout)

    # ==================== Lifecycle Events ====================

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if not self._first_shown:
            self._first_shown = True
            self.sync_character_profile(CharacterLore.get_active_profile())
            self.apply_settings_to_window()
            self._restore_or_init_position()
            self._sys_monitor.sample()  # prime baseline

            if self._suppress_loaded_default_greeting:
                self._suppress_loaded_default_greeting = False
            else:
                now = datetime.now()
                greet_state = CharacterLore.get_time_greeting_state(now)
                self._set_temporary_pose(
                    greet_state,
                    5.5,
                    CharacterLore.pick_line_for_state(greet_state),
                )
                self._sound.play(PetSoundEffect.SPARKLE)
                self._spawn_star_burst(self.width() * 0.5, self.height() * 0.65, 8)

            self._last_tick_utc = datetime.now(timezone.utc)
            self._frame_timer.start()
            self._ai_timer.start()
            self._monitor_timer.start()
            if self._settings.chat_sync_enabled:
                # 上次退出前开着播报：等窗口真正可见再恢复，避免气泡被吞。
                QTimer.singleShot(0, self._auto_resume_antigravity_sync)
        else:
            self._apply_win32_extended_styles()

    def closeEvent(self, event) -> None:
        if not self._closing_handled:
            self._closing_handled = True
            if self._first_shown:
                self._settings.last_left = float(self.x())
                self._settings.last_top = float(self.y())
            self._settings_service.save(self._settings)

        self._frame_timer.stop()
        self._ai_timer.stop()
        self._bubble_timer.stop()
        self._monitor_timer.stop()
        self._hover_grace_timer.stop()
        for worker in list(self._active_chat_workers):
            if worker.isRunning():
                worker.wait(400)
        if (
            self._chat_models_discovery_worker is not None
            and self._chat_models_discovery_worker.isRunning()
        ):
            self._chat_models_discovery_worker.wait(400)
        if getattr(self, "_ui_ready", False):
            self._speech_bubble.close()
            self._speech_bubble.deleteLater()
        super().closeEvent(event)

    def hideEvent(self, event) -> None:
        if getattr(self, "_ui_ready", False):
            self._is_chat_open = False
            self._is_forced_bubble_active = False
            self._sync_chat_controls()
            self._hide_speech_bubble()
        super().hideEvent(event)

    def moveEvent(self, event) -> None:
        super().moveEvent(event)
        if getattr(self, "_ui_ready", False):
            self._reposition_speech_bubble_on_move()

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if not getattr(self, "_ui_ready", False):
            return super().eventFilter(watched, event)
        etype = event.type()
        if (
            etype == QEvent.Type.MouseButtonPress
            and isinstance(watched, QWidget)
            and (watched is self._speech_bubble or self._speech_bubble.isAncestorOf(watched))
            and self._sync_thinking_visible
        ):
            # 点击阅读后仍接收思维续写，但不让结尾气泡替换内容或重新启动关闭计时。
            self._sync_thinking_pinned = True
            self._bubble_timer.stop()
        if watched is self._speech_bubble:
            if etype == QEvent.Type.Enter:
                self._is_bubble_hovered = True
                self._on_hover_entered()
            elif etype == QEvent.Type.Leave:
                self._is_bubble_hovered = False
                if (
                    not self._is_chat_open
                    and not self._sync_thinking_pinned
                    and self._speech_bubble.isVisible()
                    and not self._speech_bubble.is_resizing()
                ):
                    self._bubble_timer.stop()
                    self._bubble_timer.start(2500)
                self._schedule_hover_leave_check()
        elif watched is self._chat_input and etype == QEvent.Type.KeyPress:
            if getattr(event, "key", lambda: None)() == Qt.Key.Key_Escape:
                self._close_bubble_click()
                return True
        return super().eventFilter(watched, event)

    def enterEvent(self, event) -> None:
        self._on_hover_entered()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        self._schedule_hover_leave_check()
        super().leaveEvent(event)

    def _on_hover_entered(self) -> None:
        self._hover_grace_timer.stop()
        self._is_hovering = True
        if self._settings.docked_side != DockSide.NONE and self._override_pose is None:
            self._refresh_sprite_visual()

    def _schedule_hover_leave_check(self) -> None:
        # 延迟检查光标是否真正离开窗口与浮层区域
        self._hover_grace_timer.stop()
        self._hover_grace_timer.start()

    def _on_hover_grace_timeout(self) -> None:
        global_pos = QCursor.pos()
        local_pos = self.mapFromGlobal(global_pos)
        # 仅将角色精灵区、气泡和右键菜单视为悬浮命中区，避免透明留白让状态卡住
        sprite_hit = self._get_local_sprite_rect().adjusted(-8.0, -8.0, 8.0, 8.0).contains(
            QPointF(local_pos)
        )
        still_inside = sprite_hit or self._context_menu.isVisible()
        if not still_inside and self._speech_bubble.isVisible():
            if self._speech_bubble.is_resizing():
                still_inside = True
            else:
                sb_rect = QRect(
                    self._speech_bubble.mapToGlobal(QPoint(0, 0)),
                    self._speech_bubble.size(),
                ).adjusted(-6, -6, 6, 6)
                still_inside = sb_rect.contains(global_pos)
        if not still_inside:
            self._is_hovering = False
            if self._settings.docked_side != DockSide.NONE and self._override_pose is None:
                self._refresh_sprite_visual()

    # ==================== Settings & Layout Sync ====================

    def _effective_scale(self) -> float:
        value = self._preview_scale if self._preview_scale is not None else self._settings.scale
        return max(0.5, min(2.0, float(value)))

    def _effective_opacity(self) -> float:
        value = (
            self._preview_opacity
            if self._preview_opacity is not None
            else self._settings.opacity
        )
        return max(0.35, min(1.0, float(value)))

    def set_appearance_preview(
        self,
        scale: Optional[float] = None,
        opacity: Optional[float] = None,
    ) -> None:
        """设置窗口拖动缩放/透明度时临时预览，不写入配置；传 None 恢复已保存值。"""
        self._preview_scale = None if scale is None else max(0.5, min(2.0, float(scale)))
        self._preview_opacity = None if opacity is None else max(0.35, min(1.0, float(opacity)))
        self.apply_settings_to_window()

    def _sprite_height_px(self) -> float:
        s = self._effective_scale()
        base_h = 300.0 if self._settings.style_mode == PetStyleMode.STANDARD else 240.0
        return base_h * s

    def _chibi_rig_size(self) -> tuple[float, float]:
        s = self._effective_scale()
        h = 240.0 * s
        return (h * (555.0 / 640.0), h)

    def _std_rig_size(self) -> tuple[float, float]:
        s = self._effective_scale()
        h = 300.0 * s
        return (h * (337.0 / 977.0), h)

    def apply_settings_to_window(self) -> None:
        old_bottom = float(self.y() + self.height()) if self._first_shown else None

        s = self._effective_scale()
        sprite_h = self._sprite_height_px()
        opacity = self._effective_opacity()
        self.setWindowOpacity(opacity)
        self._speech_bubble.setWindowOpacity(opacity)
        self._sound.enabled = bool(self._settings.sound_enabled)

        pinned = not self._settings.auto_walk
        if pinned and self._is_walking:
            self._is_walking = False

        # 同步右键菜单勾选状态与气泡展示
        self._apply_doze_transition()
        self._sync_context_menu_checks()
        new_w = max(360, int(round(380.0 * s)))
        new_h = max(380, int(round(sprite_h + 200.0)))

        self.setFixedSize(new_w, new_h)
        self._stage_canvas.setGeometry(0, 0, new_w, new_h)

        if old_bottom is not None:
            if self._settings.docked_side == DockSide.NONE:
                self.move(self.x(), int(round(old_bottom - new_h)))
                self._clamp_inside_nearest_work_area()
            else:
                self._reanchor_docked_position()

        self._bond_badge_lbl.setText(f" · ♥ {self._settings.favorability}")
        if (
            not self._settings.show_speech_bubble
            and not self._is_chat_open
            and not self._is_forced_bubble_active
            and not self._ag_mirror_active
        ):
            self._hide_speech_bubble()

        self._update_dock_alignment()
        self._apply_win32_extended_styles()
        self._refresh_sprite_visual()
        self._sync_chat_controls()
        self._relayout_stage_top()
        self._stage_canvas.update()

    def _apply_win32_extended_styles(self) -> None:
        hwnd = int(self.winId())
        if not hwnd:
            return
        _ensure_tool_window_ex_style(hwnd)
        _apply_win32_topmost(hwnd, bool(self._settings.always_on_top))
        self._sync_bubble_window_styles()
        self.visual_bounds_changed.emit()

    def _sync_bubble_window_styles(self) -> None:
        if not self._speech_bubble.isVisible():
            return
        bubble_hwnd = int(self._speech_bubble.winId())
        if not bubble_hwnd:
            return
        _ensure_tool_window_ex_style(bubble_hwnd)
        _apply_win32_topmost(bubble_hwnd, bool(self._settings.always_on_top))

    def _update_dock_alignment(self) -> None:
        # 贴边状态变化后气泡需要重新贴近精灵摆放
        self._relayout_stage_top()

    def _bubble_min_size(self) -> tuple[int, int]:
        if self._is_chat_open:
            return (560, 360) if self._history_expanded else (320, 280)
        return (220, 86)

    def _bubble_max_size(self, center_x: float, center_y: float) -> tuple[int, int]:
        area = self._get_work_area(center_x, center_y)
        min_w, min_h = self._bubble_min_size()
        return (
            min(760, max(min_w, area.width() - 24)),
            min(620, max(min_h, area.height() - 40)),
        )

    def _auto_fit_bubble_height(self, target_w: int, min_h: int, max_h: int) -> int:
        margins = self._bubble_layout.contentsMargins()
        spacing = max(0, self._bubble_layout.spacing())
        label_w = max(140, target_w - margins.left() - margins.right() - 8)
        if self._is_chat_open and not self._chat_messages_panel.isHidden():
            panel_h = (
                self._chat_messages_panel.heightForWidth(label_w)
                if self._chat_messages_panel.hasHeightForWidth()
                else self._chat_messages_panel.sizeHint().height()
            )
            scroll_h = min(max(130, panel_h + 14), 320)
        else:
            text_h = (
                self._bubble_text_lbl.heightForWidth(label_w)
                if self._bubble_text_lbl.hasHeightForWidth()
                else self._bubble_text_lbl.sizeHint().height()
            )
            min_scroll = 56 if self._is_chat_open else 26
            scroll_h = min(max(min_scroll, text_h + 12), 260)
        header_h = max(18, self._bubble_header_widget.sizeHint().height())
        chrome_h = margins.top() + margins.bottom() + header_h + spacing
        if not self._chat_toolbar.isHidden():
            chrome_h += max(24, self._chat_toolbar.sizeHint().height()) + spacing
        if not self._chat_input_panel.isHidden():
            chrome_h += max(30, self._chat_input_panel.sizeHint().height()) + spacing
        for panel in (self._chat_session_bar, self._chat_path_panel):
            if not panel.isHidden():
                chrome_h += panel.sizeHint().height() + spacing
        return max(min_h, min(max_h, chrome_h + scroll_h))

    def _compute_bubble_target_size(self) -> QSize:
        min_w, min_h = self._bubble_min_size()
        max_w, max_h = self._bubble_max_size(
            self.x() + self.width() * 0.5, self.y() + self.height() * 0.5
        )
        if self._is_chat_open:
            target_w = (
                int(self._settings.chat_width)
                if self._settings.chat_width is not None
                else 360
            )
            target_w = max(min_w, min(max_w, target_w))
            if self._settings.chat_height is not None:
                target_h = max(min_h, min(max_h, int(self._settings.chat_height)))
            else:
                target_h = self._auto_fit_bubble_height(target_w, min_h, max_h)
            return QSize(target_w, target_h)

        if self._transient_bubble_size is not None:
            return QSize(
                max(min_w, min(max_w, self._transient_bubble_size.width())),
                max(min_h, min(max_h, self._transient_bubble_size.height())),
            )

        if self._sync_bubble_width is not None:
            # 同步播报期间锁宽：文本每长一点都按最长行重算宽度，气泡会左右抽动。
            target_w = max(min_w, min(max_w, self._sync_bubble_width))
            target_h = self._auto_fit_bubble_height(target_w, min_h, max_h)
            return QSize(target_w, target_h)

        fm = QFontMetrics(self._bubble_text_lbl.font())
        text = self._bubble_text_lbl.text() or ""
        longest_line = max((fm.horizontalAdvance(ln) for ln in text.splitlines()), default=0)
        target_w = max(240, min(330, longest_line + 52))
        target_w = max(min_w, min(max_w, target_w))
        target_h = self._auto_fit_bubble_height(target_w, min_h, max_h)
        return QSize(target_w, target_h)

    def _fit_bubble_size_to_placement(self, size: QSize) -> QSize:
        """默认摆放时按精灵上方或下方可用空间收缩高度，避免气泡压住立绘。"""
        if not self._is_chat_open:
            return size
        gap = 8
        sprite_rect = self.get_visual_sprite_bounds()
        anchor_top = sprite_rect.top()
        area = self._get_work_area(
            self.x() + self.width() * 0.5, self.y() + self.height() * 0.5
        )
        space_above = anchor_top - gap - (area.top() + 6)
        space_below = (area.bottom() - 6) - (sprite_rect.bottom() + 1 + gap)
        available = max(space_above, space_below)
        if size.height() <= available:
            return size
        min_h = self._bubble_min_size()[1]
        return QSize(size.width(), max(min_h, min(available, size.height())))

    def _compute_bubble_default_pos(self, bubble_size: QSize) -> QPoint:
        gap = 8
        bw = bubble_size.width()
        bh = bubble_size.height()
        sprite_rect = self.get_visual_sprite_bounds()
        anchor_rect = sprite_rect

        if self._settings.docked_side == DockSide.LEFT:
            target_x = anchor_rect.left()
        elif self._settings.docked_side == DockSide.RIGHT:
            target_x = anchor_rect.right() + 1 - bw
        else:
            target_x = anchor_rect.center().x() - bw // 2

        target_y = anchor_rect.top() - gap - bh
        area = self._get_work_area(float(anchor_rect.center().x()), float(anchor_rect.center().y()))
        # 若角色贴近屏幕顶端导致上方空间不足，优先放到精灵下方避免遮挡头顶
        if target_y < area.top() + 6 and sprite_rect.bottom() + 1 + gap + bh <= area.bottom() - 6:
            target_y = sprite_rect.bottom() + 1 + gap

        target_x = max(area.left() + 6, min(target_x, area.right() + 1 - bw - 6))
        target_y = max(area.top() + 6, min(target_y, area.bottom() + 1 - bh - 6))
        return QPoint(target_x, target_y)

    def _clamp_bubble_pos_to_screen(self, pos: QPoint, size: QSize) -> QPoint:
        area = self._get_work_area(self.x() + self.width() * 0.5, self.y() + self.height() * 0.5)
        cx = max(area.left() + 6, min(pos.x(), area.right() + 1 - size.width() - 6))
        cy = max(area.top() + 6, min(pos.y(), area.bottom() + 1 - size.height() - 6))
        return QPoint(cx, cy)

    def _reposition_speech_bubble_on_move(self) -> None:
        if not self._speech_bubble.isVisible() or self._speech_bubble.is_resizing():
            return
        bsize = self._speech_bubble.size()
        if self._bubble_custom_offset is not None:
            pos = self._clamp_bubble_pos_to_screen(self.pos() + self._bubble_custom_offset, bsize)
        else:
            pos = self._compute_bubble_default_pos(bsize)
        if self._speech_bubble.pos() != pos:
            self._speech_bubble.move(pos)
            self.visual_bounds_changed.emit()

    def _on_bubble_resized_by_user(self, geom: QRect) -> None:
        self._bubble_custom_offset = geom.topLeft() - self.pos()
        if self._is_chat_open:
            self._settings.chat_width = geom.width()
            self._settings.chat_height = geom.height()
            self._update_chat_size_btn_label()
        else:
            self._transient_bubble_size = geom.size()
        self.visual_bounds_changed.emit()

    def _on_bubble_resize_finished(self, geom: QRect) -> None:
        self._on_bubble_resized_by_user(geom)
        if self._is_chat_open:
            self._settings_service.save(self._settings)
        elif not self._sync_thinking_pinned and not self._is_bubble_hovered and self._speech_bubble.isVisible():
            self._bubble_timer.stop()
            self._bubble_timer.start(2500)

    def _relayout_stage_top(self) -> None:
        if self._speech_bubble.isVisible() and not self._speech_bubble.is_resizing():
            min_w, min_h = self._bubble_min_size()
            max_w, max_h = self._bubble_max_size(
                self.x() + self.width() * 0.5, self.y() + self.height() * 0.5
            )
            self._speech_bubble.setMinimumSize(min_w, min_h)
            self._speech_bubble.setMaximumSize(max_w, max_h)
            target_size = self._compute_bubble_target_size()
            # 用户手动调整过尺寸/位置时保留其选择，仅默认摆放时按可用空间收缩。
            if self._bubble_custom_offset is None:
                target_size = self._fit_bubble_size_to_placement(target_size)
            if self._speech_bubble.size() != target_size:
                self._speech_bubble.resize(target_size)
            if self._bubble_custom_offset is not None:
                target_pos = self._clamp_bubble_pos_to_screen(
                    self.pos() + self._bubble_custom_offset, target_size
                )
            else:
                target_pos = self._compute_bubble_default_pos(target_size)
            if self._speech_bubble.pos() != target_pos:
                self._speech_bubble.move(target_pos)
            self._speech_bubble.raise_()

        self.visual_bounds_changed.emit()

    # ==================== Position & Monitors ====================

    def _restore_or_init_position(self) -> None:
        if self._settings.last_left is not None and self._settings.last_top is not None:
            self.move(int(round(self._settings.last_left)), int(round(self._settings.last_top)))
            if self._settings.docked_side != DockSide.NONE:
                self._reanchor_docked_position()
            else:
                self._clamp_inside_nearest_work_area()
        else:
            self.move_to_bottom_right_corner()

    def move_to_bottom_right_corner(self) -> None:
        self._settings.docked_side = DockSide.NONE
        self._is_falling = False
        self._bubble_custom_offset = None
        area = self._get_work_area(self.x() + self.width() * 0.5, self.y() + self.height() * 0.5)
        new_x = area.right() + 1 - self.width() - 28
        new_y = area.bottom() + 1 - self.height()
        self.move(int(round(new_x)), int(round(new_y)))
        self._update_dock_alignment()
        self._settings_service.save(self._settings)

    def reset_to_bottom_right(self) -> None:
        """从桌面/托盘菜单归位到右下角并打个招呼。"""
        if not self.isVisible():
            self.show()
        self.move_to_bottom_right_corner()
        self.show_and_greet("回到桌面右下角就位啦喵✦")

    def _reanchor_docked_position(self) -> None:
        if self._settings.docked_side == DockSide.NONE:
            return
        area = self._get_work_area(self.x() + self.width() * 0.5, self.y() + self.height() * 0.5)
        new_x = self.x()
        if self._settings.docked_side == DockSide.LEFT:
            new_x = area.left()
        elif self._settings.docked_side == DockSide.RIGHT:
            new_x = area.right() + 1 - self.width()
        max_y = max(area.top(), area.bottom() + 1 - self.height())
        new_y = max(area.top(), min(max_y, self.y()))
        self.move(int(round(new_x)), int(round(new_y)))

    def _get_work_area(self, center_x: float, center_y: float) -> QRect:
        pt = QPoint(int(round(center_x)), int(round(center_y)))
        screen = QGuiApplication.screenAt(pt) or self.screen() or QGuiApplication.primaryScreen()
        return screen.availableGeometry() if screen else QRect(0, 0, 1920, 1080)

    def _clamp_inside_nearest_work_area(self) -> None:
        area = self._get_work_area(self.x() + self.width() * 0.5, self.y() + self.height() * 0.5)
        max_x = max(area.left(), area.right() + 1 - self.width())
        max_y = max(area.top(), area.bottom() + 1 - self.height())
        cx = max(area.left(), min(max_x, self.x()))
        cy = max(area.top(), min(max_y, self.y()))
        self.move(int(round(cx)), int(round(cy)))

    def _pixmap_content_rect(self, pixmap: QPixmap) -> QRect:
        """返回贴图去掉透明留白后的实际内容边界，标准版与 Q 版各自适配。"""
        key = int(pixmap.cacheKey())
        cached = self._sprite_content_cache.get(key)
        if cached is not None:
            return cached
        bounds = QRect(0, 0, pixmap.width(), pixmap.height())
        try:
            image = pixmap.toImage().convertToFormat(QImage.Format.Format_ARGB32)
            region = QRegion(QBitmap.fromImage(image.createAlphaMask()))
            tight = region.boundingRect()
            if not tight.isEmpty():
                # 留 2px 余量，避免把半透明的发丝/光晕边缘裁掉
                bounds = tight.adjusted(-2, -2, 2, 2).intersected(bounds)
        except Exception:
            pass
        self._sprite_content_cache[key] = bounds
        return bounds

    def _get_local_sprite_rect(self) -> QRectF:
        """Returns active sprite or walk rig rectangle in local PetWindow coordinates."""
        w = float(self.width())
        h = float(self.height())
        sprite_h = self._sprite_height_px()

        if self._use_std_walk_rig:
            rw, rh = self._std_rig_size()
        elif self._use_chibi_walk_rig:
            rw, rh = self._chibi_rig_size()
        elif self._active_static_pixmap is not None and not self._active_static_pixmap.isNull():
            content = self._pixmap_content_rect(self._active_static_pixmap)
            cw = float(content.width())
            ch = float(content.height())
            rh = sprite_h
            rw = rh * (cw / ch) if ch > 0 else rh * 0.85
        else:
            rh = sprite_h
            rw = min(w, rh * 0.85)

        if self._settings.docked_side == DockSide.LEFT:
            sx = 0.0
        elif self._settings.docked_side == DockSide.RIGHT:
            sx = w - rw
        else:
            sx = (w - rw) * 0.5
        sy = h - rh
        return QRectF(sx, sy, rw, rh)

    def get_visual_sprite_bounds(self) -> QRect:
        """Returns tight global bounds of the currently visible character sprite."""
        local = self._get_local_sprite_rect()
        return QRect(
            int(round(self.x() + local.x())),
            int(round(self.y() + local.y())),
            max(1, int(round(local.width()))),
            max(1, int(round(local.height()))),
        )

    def get_visual_obstacle_rects(self) -> list[QRect]:
        """返回角色精灵与当前可见气泡的独立全局矩形，避免合框误占空位。"""
        rects = [self.get_visual_sprite_bounds()]
        if self._speech_bubble.isVisible() and self._speech_bubble.width() > 0:
            rects.append(
                QRect(
                    self._speech_bubble.mapToGlobal(QPoint(0, 0)),
                    self._speech_bubble.size(),
                )
            )
        return rects

    def get_visual_ui_bounds(self) -> QRect:
        """返回包含角色精灵及当前可见气泡的全局可视区域包围盒。"""
        rects = self.get_visual_obstacle_rects()
        bounds = rects[0]
        for rect in rects[1:]:
            bounds = bounds.united(rect)
        return bounds

    # ==================== Public Greeting / Preview ====================

    def show_and_greet(self, custom_message: Optional[str] = None) -> None:
        if not self._first_shown:
            self._suppress_loaded_default_greeting = True
            self._restore_or_init_position()

        if not self.isVisible():
            self.show()

        force_wake = bool(custom_message and custom_message.strip())
        if self._settings.docked_side != DockSide.NONE:
            self._reanchor_docked_position()
            dock_pose = (
                PetState.EDGE_LEFT_PEEK
                if self._settings.docked_side == DockSide.LEFT
                else PetState.EDGE_RIGHT_PEEK
            )
            self._set_temporary_pose(
                dock_pose,
                5.0,
                custom_message or CharacterLore.pick_line("edge"),
                force_bubble=force_wake,
            )
        else:
            self._clamp_inside_nearest_work_area()
            greet = CharacterLore.get_time_greeting_state(datetime.now())
            self._set_temporary_pose(
                greet,
                5.0,
                custom_message or CharacterLore.pick_line_for_state(greet),
                force_bubble=force_wake,
            )

        self._sound.play(PetSoundEffect.SPARKLE)
        self._spawn_star_burst(self.width() * 0.5, self.height() - self._sprite_height_px() * 0.55, 7)

    def trigger_external_sprite_preview(self, sprite_key: str) -> None:
        parsed = PetState.from_sprite_key(sprite_key)
        if parsed is not None:
            self._set_temporary_pose(parsed, 4.5, CharacterLore.pick_line_for_state(parsed))
        else:
            self._override_pose = None
            self._use_chibi_walk_rig = False
            self._use_std_walk_rig = False
            self._active_static_pixmap = self._sprites.get_by_key(sprite_key, self._settings.style_mode)
            self.show_bubble(f"动作预览：{sprite_key} ✦", 4.0)
            self._stage_canvas.update()
        self._sound.play(PetSoundEffect.CHIME)
        self._spawn_star_burst(self.width() * 0.5, self.height() - self._sprite_height_px() * 0.5, 5)

    def configure_smoke_preview(
        self,
        style_mode: PetStyleMode,
        walking: bool,
        anim_time_sec: float,
        dock_side: DockSide,
    ) -> None:
        self._override_pose = None
        self._is_dragging = False
        self._is_falling = False
        self._is_sleeping = False
        self._is_sulk = False
        self._landing_squash = 0.0

        self._settings.style_mode = style_mode
        self._settings.docked_side = dock_side
        if dock_side != DockSide.NONE:
            self._settings.anti_gravity_mode = False

        self._is_walking = walking and dock_side == DockSide.NONE
        self._walk_dir = -1
        self._anim_time = anim_time_sec
        if dock_side == DockSide.LEFT:
            self._current_state = PetState.EDGE_LEFT_PEEK
        elif dock_side == DockSide.RIGHT:
            self._current_state = PetState.EDGE_RIGHT_PEEK
        elif self._is_walking:
            self._current_state = PetState.RUNNING
        else:
            self._current_state = PetState.IDLE

        self.apply_settings_to_window()
        if dock_side != DockSide.NONE:
            self._reanchor_docked_position()

        self._relayout_stage_top()
        self._update_sprite_transforms()
        self._stage_canvas.update()

    # ==================== 60fps Loop & Kinematics ====================

    def _frame_timer_tick(self) -> None:
        now = datetime.now(timezone.utc)
        dt = max(0.001, min(0.05, (now - self._last_tick_utc).total_seconds()))
        self._last_tick_utc = now
        self._anim_time += dt
        if self._shake_energy > 0:
            self._shake_energy = max(0.0, self._shake_energy - dt * 1.4)

        if self._waiting_chat_workers:
            # 模型正在回复期间锁定展示“思考中”形象，防止长耗时思考请求被超时定时器打断
            if self._override_pose != PetState.THINKING:
                self._is_walking = False
                self._is_sleeping = False
                self._override_pose = PetState.THINKING
                self._refresh_sprite_visual()
        elif self._override_pose is not None and now >= self._override_until_utc:
            self._override_pose = None
            if self._is_sulk:
                self._is_sulk = False
            self._refresh_sprite_visual()

        # Gravity fall physics
        if (
            self._is_falling
            and not self._is_dragging
            and not self._settings.anti_gravity_mode
            and self._settings.docked_side == DockSide.NONE
        ):
            area = self._get_work_area(self.x() + self.width() * 0.5, self.y() + self.height() * 0.5)
            floor_top = float(area.bottom() + 1 - self.height())
            self._fall_velocity_y += 2300.0 * dt
            next_top = float(self.y()) + self._fall_velocity_y * dt

            if next_top >= floor_top:
                self.move(self.x(), int(round(floor_top)))
                self._is_falling = False
                self._fall_velocity_y = 0.0
                self._landing_squash = 1.0
                self._sound.play(PetSoundEffect.BOING)
            else:
                self.move(self.x(), int(round(next_top)))

        # Autonomous horizontal walk along taskbar
        if (
            self._is_walking
            and not self._is_dragging
            and not self._is_falling
            and self._settings.docked_side == DockSide.NONE
        ):
            area = self._get_work_area(self.x() + self.width() * 0.5, self.y() + self.height() * 0.5)
            sprite_rect = self._get_local_sprite_rect()
            sprite_half = sprite_rect.width() * 0.5 if sprite_rect.width() > 10 else 85.0 * self._effective_scale()
            min_left = area.left() - (self.width() * 0.5 - sprite_half) + 8.0
            max_left = (area.right() + 1) - (self.width() * 0.5 + sprite_half) - 8.0

            speed = 72.0 * self._effective_scale()
            next_left = float(self.x()) + self._walk_dir * speed * dt
            if next_left <= min_left:
                next_left = min_left
                self._walk_dir = 1
            elif next_left >= max_left:
                next_left = max_left
                self._walk_dir = -1
            self.move(int(round(next_left)), self.y())

        if self._landing_squash > 0.001:
            self._landing_squash = max(0.0, self._landing_squash - dt * 4.5)

        self._update_sprite_transforms()
        self._update_particles(dt)
        self._stage_canvas.update()

    def _update_sprite_transforms(self) -> None:
        sx = 1.0
        sy = 1.0
        angle = 0.0
        tx = 0.0
        ty = 0.0
        s = self._effective_scale()

        if self._settings.docked_side != DockSide.NONE:
            sy = 1.0 + 0.011 * math.sin(self._anim_time * 2.5)
            angle = (1.0 if self._settings.docked_side == DockSide.LEFT else -1.0) * 0.8 * math.sin(
                self._anim_time * 2.1
            )
        elif self._is_dragging:
            angle = max(-16.0, min(16.0, self._prev_vx * 0.012))
            sy = 1.02
            if self._shake_energy > 1.8:
                tx = math.sin(self._anim_time * 48.0) * 4.0
        elif self._is_walking:
            sx = -1.0 if self._walk_dir > 0 else 1.0
            rig = self._active_walk_rig()
            if self._settings.style_mode == PetStyleMode.STANDARD and rig.standard_complete:
                p = self._anim_time * 6.8
                self._update_std_walk_rig(p, s)
            elif self._settings.style_mode == PetStyleMode.CHIBI and rig.chibi_complete:
                p = self._anim_time * 7.6
                self._update_chibi_walk_rig(p, s)
            else:
                ty = -abs(math.sin(self._anim_time * 11.0)) * 7.0
                angle = math.sin(self._anim_time * 11.0) * 2.8 * self._walk_dir
        elif self._override_pose == PetState.THINKING:
            sy = 1.0 + 0.016 * math.sin(self._anim_time * 3.0)
            angle = 1.8 * math.sin(self._anim_time * 1.8)
            if self._settings.particles_enabled and random.random() < 0.04:
                self._spawn_particle(
                    "💭",
                    "#C4B5FD",
                    self.width() * 0.62,
                    self.height() - self._sprite_height_px() * 0.84,
                    8.0,
                    -20.0,
                    1.3,
                )
        elif self._settings.anti_gravity_mode or self._current_state == PetState.FLOAT:
            ty = -16.0 + 8.5 * math.sin(self._anim_time * 2.1)
            angle = 3.2 * math.sin(self._anim_time * 1.4)
            sy = 1.0 + 0.015 * math.sin(self._anim_time * 2.1 + 0.8)
            if self._settings.particles_enabled and random.random() < 0.08:
                self._spawn_particle(
                    "✦",
                    "#A78BFA",
                    self.width() * 0.5 + (random.random() - 0.5) * 140.0,
                    self.height() - 40.0 - random.random() * 140.0,
                    0.0,
                    -28.0,
                    1.3,
                )
        elif self._is_sleeping or (self._is_dozing() and self._override_pose is None):
            sy = 1.0 + 0.024 * math.sin(self._anim_time * 1.4)
        else:
            sy = 1.0 + 0.014 * math.sin(self._anim_time * 2.4)
            sx = 1.0 - 0.006 * math.sin(self._anim_time * 2.4)

        if self._use_std_walk_rig and not self._is_walking:
            self._update_std_walk_rig(self._anim_time * 6.8, self._effective_scale())
        if self._is_dozing():
            sy = 1.0 + 0.018 * math.sin(self._anim_time * 1.4)
            if self._settings.docked_side != DockSide.NONE:
                angle = (1 if self._settings.docked_side == DockSide.LEFT else -1) * 1.2 * math.sin(self._anim_time * 1.4)

        # Zzz 属于打盹反馈，贴边时也要出现，所以放在贴边/睡姿分支之外
        dozing = self._is_sleeping or (self._is_dozing() and self._override_pose is None)
        if dozing and random.random() < 0.04:
            zzz_x, zzz_y = self._doze_zzz_anchor()
            self._spawn_particle(
                "Zzz",
                "#93C5FD",
                zzz_x,
                zzz_y,
                12.0,
                -22.0,
                1.8,
            )

        if self._landing_squash > 0.001:
            ls = math.sin(self._landing_squash * math.pi)
            sy *= 1.0 - 0.14 * ls
            sx *= 1.0 + 0.12 * ls

        self._sprite_scale_x = sx
        self._sprite_scale_y = sy
        self._sprite_angle = angle
        self._sprite_trans_x = tx
        self._sprite_trans_y = ty

    def _update_chibi_walk_rig(self, p: float, scale: float) -> None:
        stride = math.sin(p)
        lift_l = max(0.0, math.cos(p))
        lift_r = max(0.0, -math.cos(p))

        self._chibi_kinematics = {
            "leg_l_rot": -6.0 + 16.0 * stride,
            "leg_l_ty": -lift_l * 4.5 * scale,
            "leg_r_rot": 8.0 - 16.0 * stride,
            "leg_r_ty": -lift_r * 4.5 * scale,
            "arm_l_rot": -6.0 - 15.0 * stride,
            "arm_r_rot": 4.0 + 15.0 * stride,
            "body_ty": -3.2 * (1.0 - math.cos(2.0 * p)) * 0.5 * scale,
            "body_rot": -1.8 + 1.3 * math.sin(2.0 * p),
        }

    def _update_std_walk_rig(self, p: float, scale: float) -> None:
        front = p
        back = p + math.pi
        self._std_kinematics = {
            "front_hip": 17.5 * math.sin(front),
            "front_knee": -32.0 * max(0.0, math.cos(front - 0.1)) - 5.0 * max(0.0, math.sin(front)),
            "back_hip": 17.5 * math.sin(back),
            "back_knee": -32.0 * max(0.0, math.cos(back - 0.1)) - 5.0 * max(0.0, math.sin(back)),
            "body_ty": -2.8 * (1.0 - math.cos(2.0 * p)) * 0.5 * scale,
            "body_rot": -2.2 + 1.1 * math.sin(2.0 * p),
        }

    def _paint_stage(self, painter: QPainter) -> None:
        sprite_rect = self._get_local_sprite_rect()

        # 在角色有效区域填充 alpha=1 底色，保证发丝镂空区也可正常拖拽
        painter.fillRect(sprite_rect, QColor(0, 0, 0, 1))

        if self._settings.docked_side == DockSide.LEFT:
            pivot_x = sprite_rect.left()
        elif self._settings.docked_side == DockSide.RIGHT:
            pivot_x = sprite_rect.right()
        else:
            pivot_x = sprite_rect.center().x()
        pivot_y = sprite_rect.bottom()

        painter.save()
        painter.translate(pivot_x + self._sprite_trans_x, pivot_y + self._sprite_trans_y)
        painter.rotate(self._sprite_angle)
        painter.scale(self._sprite_scale_x, self._sprite_scale_y)
        painter.translate(-pivot_x, -pivot_y)

        if self._use_std_walk_rig:
            self._paint_std_walk_rig(painter, sprite_rect)
        elif self._use_chibi_walk_rig:
            self._paint_chibi_walk_rig(painter, sprite_rect)
        else:
            pixmap = self._active_static_pixmap
            if pixmap is not None and not pixmap.isNull():
                content = self._pixmap_content_rect(pixmap)
                painter.drawPixmap(sprite_rect, pixmap, QRectF(content))

        painter.restore()

        # Paint floating particle overlay
        for pt in self._particles:
            alpha = max(0.0, min(1.0, 1.0 - (pt.life / pt.max_life)))
            color = QColor(pt.color)
            color.setAlphaF(alpha)
            painter.save()
            painter.translate(pt.x + 8.0, pt.y + 8.0)
            painter.rotate(pt.angle)
            painter.translate(-8.0, -8.0)
            painter.setPen(color)
            painter.setFont(QFont("Microsoft YaHei UI", pt.font_size, QFont.Weight.Bold))
            painter.drawText(QRectF(0.0, 0.0, 48.0, 32.0), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop, pt.glyph)
            painter.restore()

    def _paint_chibi_walk_rig(self, painter: QPainter, rect: QRectF) -> None:
        rig = self._active_walk_rig()
        k = self._chibi_kinematics
        rw, rh = rect.width(), rect.height()
        rx, ry = rect.x(), rect.y()
        local_rect = QRectF(0.0, 0.0, rw, rh)

        def draw_part(pm: Optional[QPixmap], px: float, py: float, angle: float, ty: float = 0.0) -> None:
            if pm is None:
                return
            painter.save()
            painter.translate(rx + px * rw, ry + py * rh + ty)
            painter.rotate(angle)
            painter.translate(-px * rw, -py * rh)
            _draw_pixmap_uniform(painter, pm, local_rect)
            painter.restore()

        draw_part(rig.chibi_arm_l, 0.3250, 0.5083, k["arm_l_rot"])
        draw_part(rig.chibi_leg_r, 0.5172, 0.7233, k["leg_r_rot"], k["leg_r_ty"])
        draw_part(rig.chibi_leg_l, 0.3538, 0.7233, k["leg_l_rot"], k["leg_l_ty"])
        draw_part(rig.chibi_body, 0.48, 0.56, k["body_rot"], k["body_ty"])
        draw_part(rig.chibi_arm_r, 0.5643, 0.5483, k["arm_r_rot"])

    def _paint_std_walk_rig(self, painter: QPainter, rect: QRectF) -> None:
        rig = self._active_walk_rig()
        k = self._std_kinematics
        rw, rh = rect.width(), rect.height()
        rx, ry = rect.x(), rect.y()
        local_rect = QRectF(0.0, 0.0, rw, rh)

        def draw_leg(thigh: Optional[QPixmap], shin: Optional[QPixmap], hip_deg: float, knee_deg: float) -> None:
            painter.save()
            painter.translate(rx + 0.3086 * rw, ry + 0.5578 * rh)
            painter.rotate(hip_deg)
            painter.translate(-0.3086 * rw, -0.5578 * rh)
            _draw_pixmap_uniform(painter, thigh, local_rect)

            painter.save()
            painter.translate(0.3472 * rw, 0.7022 * rh)
            painter.rotate(knee_deg)
            painter.translate(-0.3472 * rw, -0.7022 * rh)
            _draw_pixmap_uniform(painter, shin, local_rect)
            painter.restore()
            painter.restore()

        draw_leg(rig.std_back_thigh or rig.std_front_thigh, rig.std_back_shin or rig.std_front_shin, k["back_hip"], k["back_knee"])
        draw_leg(rig.std_front_thigh, rig.std_front_shin, k["front_hip"], k["front_knee"])

        if rig.standard_complete:
            painter.save()
            painter.translate(rx + 0.5 * rw, ry + 0.46 * rh + k["body_ty"])
            painter.rotate(k["body_rot"])
            painter.translate(-0.5 * rw, -0.46 * rh)
            _draw_pixmap_uniform(painter, rig.std_body, local_rect)
            painter.restore()

    def _active_walk_rig(self) -> WalkRigAssets:
        return self._sprites.walk_rig

    def _refresh_sprite_visual(self) -> None:
        active = self._override_pose if self._override_pose is not None else self._current_state
        if (
            self._override_pose is None
            and not self._is_walking
            and not self._is_sleeping
            and not self._is_dragging
            and not self._is_sulk
        ):
            active = self._resolve_resting_state()

        if self._settings.doze_mode and self._settings.docked_side != DockSide.NONE:
            active = self._resolve_resting_state()
        self._visual_state = active
        use_walk_rig = (
            self._is_walking
            and self._override_pose is None
            and not self._is_dragging
            and self._settings.docked_side == DockSide.NONE
        )
        rig = self._active_walk_rig()

        if use_walk_rig and self._settings.style_mode == PetStyleMode.STANDARD and rig.standard_complete:
            self._use_std_walk_rig = True
            self._use_chibi_walk_rig = False
            self._active_static_pixmap = None
            return

        if use_walk_rig and self._settings.style_mode == PetStyleMode.CHIBI and rig.chibi_complete:
            self._use_std_walk_rig = False
            self._use_chibi_walk_rig = True
            self._active_static_pixmap = None
            return

        # 骨架只用于实际移动；动作预览和搜索状态使用各自的独立立绘。
        self._use_std_walk_rig = False
        self._use_chibi_walk_rig = False
        self._active_static_pixmap = self._sprites.get(active, self._settings.style_mode)


    def _resolve_resting_state(self) -> PetState:
        # 打盹优先于悬停探头，左右贴边都保持闭眼。
        if self._settings.doze_mode:
            return {
                DockSide.LEFT: PetState.EDGE_LEFT_SLEEP,
                DockSide.RIGHT: PetState.EDGE_RIGHT_SLEEP,
            }.get(self._settings.docked_side, PetState.SLEEPING)
        if self._settings.docked_side == DockSide.LEFT:
            return PetState.EDGE_LEFT_PEEK if self._is_hovering else PetState.EDGE_LEFT
        if self._settings.docked_side == DockSide.RIGHT:
            return PetState.EDGE_RIGHT_PEEK if self._is_hovering else PetState.EDGE_RIGHT
        if self._settings.doze_mode:
            return PetState.SLEEPING
        if self._settings.anti_gravity_mode:
            return PetState.FLOAT

        return PetState.IDLE

    def _set_temporary_pose(
        self,
        state: PetState,
        duration_sec: float,
        bubble_message: Optional[str] = None,
        force_bubble: bool = False,
    ) -> None:
        self._is_walking = False
        self._is_sleeping = state in (PetState.SLEEPING, PetState.EDGE_LEFT_SLEEP, PetState.EDGE_RIGHT_SLEEP)
        self._override_pose = state
        self._override_until_utc = datetime.now(timezone.utc) + timedelta(seconds=duration_sec)
        self._refresh_sprite_visual()
        self._stage_canvas.update()

        if bubble_message and bubble_message.strip():
            self.show_bubble(bubble_message, duration_sec, force=force_bubble)

    # ==================== Quota Lookup Feedback ====================

    def react_quota_lookup_started(self) -> None:
        """额度查询开始：Q 版与标准立绘统一切到查找姿势，查询提示交给额度窗口自身。"""
        self._set_temporary_pose(PetState.SEARCHING, 12.0)

    def react_quota_lookup_finished(self, snapshot: object) -> None:
        """额度查询结束：成功切开心姿势，失败切沮丧姿势；额度结果只在看板展示，不弹气泡。"""
        if bool(getattr(snapshot, "success", False)):
            self._set_temporary_pose(PetState.HAPPY, 4.0)
        else:
            self._set_temporary_pose(PetState.OOPS, 4.5)

    # ==================== Autonomous AI & System Monitor ====================

    def _ai_timer_tick(self) -> None:
        if (
            self._mouse_down
            or self._is_dragging
            or self._is_falling
            or self._override_pose is not None
            or self._is_sulk
            or self._is_chat_open
        ):
            return

        # 打盹模式：保持原地睡觉待机，不漫步、不做随机动作也不闲聊
        if self._settings.doze_mode:
            self._is_walking = False
            self._current_state = self._resolve_resting_state()
            self._refresh_sprite_visual()
            return

        if self._settings.docked_side != DockSide.NONE:
            if not self._is_hovering and random.random() < 0.35:
                peek = (
                    PetState.EDGE_LEFT_PEEK
                    if self._settings.docked_side == DockSide.LEFT
                    else PetState.EDGE_RIGHT_PEEK
                )
                msg = CharacterLore.pick_line("edge") if random.random() < 0.4 else None
                self._set_temporary_pose(peek, 3.2, msg)
            return

        if self._is_walking:
            self._is_walking = False
            self._current_state = self._resolve_resting_state()
            self._refresh_sprite_visual()
            return

        # 仅在打盹模式下入睡，关闭时不再随机打盹
        self._is_sleeping = False
        roll = random.random()

        if self._settings.auto_walk and not self._settings.anti_gravity_mode and roll < 0.36:
            self._is_walking = True
            self._walk_dir = 1 if random.randint(0, 1) == 0 else -1
            self._current_state = PetState.RUNNING
            self._refresh_sprite_visual()
            if random.random() < 0.25:
                self.show_bubble(CharacterLore.pick_line("running"), 2.5)
        elif roll < 0.62:
            quirks = [
                PetState.IDLE2,
                PetState.IDLE3,
                PetState.HUM,
                PetState.STRETCH,
                PetState.YAWN,
                PetState.THINKING,
                PetState.DRAWING,
            ]
            pick = random.choice(quirks)
            msg = CharacterLore.pick_line_for_state(pick) if random.random() < 0.3 else None
            self._set_temporary_pose(pick, 4.2, msg)
        elif roll < 0.72 and not self._settings.anti_gravity_mode:
            self._set_temporary_pose(PetState.FLOAT, 4.5, CharacterLore.pick_line("float"))
        else:
            self._current_state = self._resolve_resting_state()
            self._refresh_sprite_visual()
            if random.random() < 0.12:
                self.show_bubble(CharacterLore.pick_line("chatter"), 5.0)

    def _monitor_timer_tick(self) -> None:
        if (
            not self._settings.system_monitor_enabled
            or self._settings.doze_mode
            or self._is_dragging
            or self._is_sulk
        ):
            return

        snap = self._sys_monitor.sample()
        now = datetime.now(timezone.utc)
        if (now - self._last_monitor_bubble_utc).total_seconds() < 180.0:
            return

        if snap.has_battery and not snap.is_charging and snap.battery_percent <= 20:
            self._last_monitor_bubble_utc = now
            self._set_temporary_pose(
                PetState.TIRED,
                5.5,
                f"电量只剩 {snap.battery_percent}% 了喵…快充电呀！",
            )
        elif snap.cpu_percent >= 88.0:
            self._last_monitor_bubble_utc = now
            swamped = PetState.SWAMPED_LEFT if random.randint(0, 1) == 0 else PetState.SWAMPED_RIGHT
            self._set_temporary_pose(
                swamped,
                5.0,
                f"CPU 飙到 {snap.cpu_percent:.0f}% 了！电脑要烫爪子了喵！",
            )

    # ==================== Mouse Drag, Shake & Headpat ====================

    def _on_stage_mouse_press(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.RightButton:
            self._context_menu.popup(event.globalPosition().toPoint())
            event.accept()
            return

        if event.button() != Qt.MouseButton.LeftButton:
            return

        was_walking = self._is_walking
        self._mouse_down = True
        self._is_dragging = False
        self._is_falling = False
        self._is_walking = False
        self._is_sleeping = False
        self._shake_energy = 0.0
        self._prev_vx = 0.0
        if was_walking:
            self._refresh_sprite_visual()

        gpos = event.globalPosition()
        self._drag_start_global = QPointF(gpos.x(), gpos.y())
        self._last_move_global = QPointF(gpos.x(), gpos.y())
        self._last_move_utc = datetime.now(timezone.utc)
        self._win_start_left = float(self.x())
        self._win_start_top = float(self.y())
        self._stage_canvas.grabMouse()
        event.accept()

    def _on_stage_mouse_double_click(self, event: QMouseEvent) -> None:
        if event.button() != Qt.MouseButton.LeftButton:
            return
        self._mouse_down = False
        self._is_dragging = False
        self._is_walking = False
        if self._stage_canvas.mouseGrabber() is self._stage_canvas:
            self._stage_canvas.releaseMouse()
        self.open_quota_requested.emit()
        event.accept()

    def _on_stage_mouse_move(self, event: QMouseEvent) -> None:
        local_pos = event.position()
        sprite_rect = self._get_local_sprite_rect()

        if self._mouse_down:
            cur_g = event.globalPosition()
            dx = cur_g.x() - self._drag_start_global.x()
            dy = cur_g.y() - self._drag_start_global.y()

            if not self._is_dragging and math.hypot(dx, dy) > 5.0:
                self._is_dragging = True
                if self._settings.docked_side != DockSide.NONE:
                    self._settings.docked_side = DockSide.NONE
                    self._update_dock_alignment()
                self._override_pose = PetState.FLOAT
                self._refresh_sprite_visual()

            if self._is_dragging:
                self.move(int(round(self._win_start_left + dx)), int(round(self._win_start_top + dy)))

                now = datetime.now(timezone.utc)
                dt = max(0.005, (now - self._last_move_utc).total_seconds())
                vx = (cur_g.x() - self._last_move_global.x()) / dt
                vy = (cur_g.y() - self._last_move_global.y()) / dt
                speed = math.hypot(vx, vy)

                if speed > 460.0 and (self._prev_vx * vx < -12000.0):
                    self._shake_energy += 0.65
                    self._react_to_shake()

                self._prev_vx = vx
                self._last_move_global = QPointF(cur_g.x(), cur_g.y())
                self._last_move_utc = now
            return

        # 悬停头部（上半部分 48% 区域）抚摸检测
        hit_h = sprite_rect.height()
        if self._settings.docked_side == DockSide.NONE and hit_h > 20.0:
            rel_y = (local_pos.y() - sprite_rect.y()) / hit_h
            if 0.0 <= rel_y <= 0.48 and sprite_rect.left() <= local_pos.x() <= sprite_rect.right():
                dist = math.hypot(
                    local_pos.x() - self._last_pat_local.x(),
                    local_pos.y() - self._last_pat_local.y(),
                )
                if dist < 80.0:
                    self._pat_travel += dist
                self._last_pat_local = QPointF(local_pos.x(), local_pos.y())

                if self._pat_travel >= 60.0:
                    self._pat_travel = 0.0
                    self.trigger_pat()
            else:
                self._pat_travel = 0.0

    def _on_stage_mouse_release(self, event: QMouseEvent) -> None:
        if event.button() != Qt.MouseButton.LeftButton or not self._mouse_down:
            return
        self._mouse_down = False
        if self._stage_canvas.mouseGrabber() is self._stage_canvas:
            self._stage_canvas.releaseMouse()

        if not self._is_dragging:
            local_pos = event.position()
            sprite_rect = self._get_local_sprite_rect()
            rel_y = (
                (local_pos.y() - sprite_rect.y()) / sprite_rect.height()
                if sprite_rect.height() > 0
                else 0.5
            )
            if rel_y <= 0.45 and self._settings.docked_side == DockSide.NONE:
                self.trigger_pat()
            elif self._settings.docked_side != DockSide.NONE:
                peek = (
                    PetState.EDGE_LEFT_PEEK
                    if self._settings.docked_side == DockSide.LEFT
                    else PetState.EDGE_RIGHT_PEEK
                )
                self._set_temporary_pose(peek, 3.5, CharacterLore.pick_line("edge"))
                self._sound.play(PetSoundEffect.CHIME)
            elif self._is_sulk:
                self.show_bubble("……哼！（摸摸头才原谅你喵）", 3.5)
            else:
                pose = random.choice([PetState.HAPPY, PetState.ASKING, PetState.SURPRISED])
                self.show_bubble(CharacterLore.pick_line("chatter"), 4.5)
                self._set_temporary_pose(pose, 2.4)
                self._sound.play(PetSoundEffect.CHIME)
            return

        self._is_dragging = False

        # 剧烈摇晃触发头晕 -> 闹脾气序列
        if self._shake_energy >= 2.2:
            self._is_sulk = True
            self._sulk_pats_needed = 2
            self._set_temporary_pose(PetState.DIZZY, 2.6, CharacterLore.pick_line("dizzy"))
            QTimer.singleShot(2600, self._enter_sulk_after_dizzy)
        else:
            self._override_pose = None
            self._try_edge_dock_or_gravity_drop()

        self._settings.last_left = float(self.x())
        self._settings.last_top = float(self.y())
        self._settings_service.save(self._settings)
        self.visual_bounds_changed.emit()

    def _enter_sulk_after_dizzy(self) -> None:
        if self.isVisible() and self._is_sulk and not self._is_dragging:
            self._set_temporary_pose(PetState.SULK, 14.0, CharacterLore.pick_line("sulk"))

    def _react_to_shake(self) -> None:
        if self._shake_energy >= 3.4:
            self._set_temporary_pose(PetState.REDHOT, 2.2, CharacterLore.pick_line("redhot"))
            self._sound.play(PetSoundEffect.ANGRY)
            self._spawn_particle(
                "💢",
                "#F87171",
                self.width() * 0.65,
                self.height() - self._sprite_height_px() * 0.85,
                18.0,
                -32.0,
                0.8,
            )
        elif self._shake_energy >= 2.1:
            self._set_temporary_pose(PetState.PUFFED, 2.0, CharacterLore.pick_line("puffed"))
        elif self._shake_energy >= 1.0:
            self._set_temporary_pose(PetState.ANNOYED, 1.8, CharacterLore.pick_line("annoyed"))

    def trigger_pat(self) -> None:
        self._settings.total_pats += 1
        self._settings.favorability = min(9999, self._settings.favorability + 1)
        self._bond_badge_lbl.setText(f" · ♥ {self._settings.favorability}")

        if self._waiting_chat_workers:
            # 正在等待模型回复时保持思考动作不被鼠标划过头顶打断
            pass
        elif self._is_sulk:
            self._sulk_pats_needed -= 1
            if self._sulk_pats_needed > 0:
                self._set_temporary_pose(PetState.COAXED, 4.0, CharacterLore.pick_line("coaxed"))
            else:
                self._is_sulk = False
                self._set_temporary_pose(PetState.FORGIVEN, 3.5, CharacterLore.pick_line("forgiven"))
        else:
            self._set_temporary_pose(PetState.PAT, 2.8, CharacterLore.pick_line("pat"))

        now = datetime.now(timezone.utc)
        if (now - self._last_pat_sound_utc).total_seconds() > 0.45:
            self._last_pat_sound_utc = now
            self._sound.play(PetSoundEffect.PURR)
            self._settings_service.save(self._settings)

        if self._settings.particles_enabled:
            sh = self._sprite_height_px()
            self._spawn_particle(
                "♥",
                "#F472B6",
                self.width() * 0.5 + (random.random() - 0.5) * 80.0,
                self.height() - sh * 0.88,
                (random.random() - 0.5) * 30.0,
                -45.0,
                1.1,
            )
            self._spawn_particle(
                "✦",
                "#C4B5FD",
                self.width() * 0.5 + (random.random() - 0.5) * 90.0,
                self.height() - sh * 0.82,
                (random.random() - 0.5) * 36.0,
                -52.0,
                1.0,
            )

    def _try_edge_dock_or_gravity_drop(self) -> None:
        area = self._get_work_area(self.x() + self.width() * 0.5, self.y() + self.height() * 0.5)
        sprite_rect = self._get_local_sprite_rect()
        sprite_w = sprite_rect.width() if sprite_rect.width() > 10 else 180.0 * self._effective_scale()
        sprite_left = self.x() + (self.width() - sprite_w) * 0.5
        sprite_right = sprite_left + sprite_w
        snap_margin = 38.0 * self._effective_scale()
        max_y = max(area.top(), area.bottom() + 1 - self.height())

        if self._settings.edge_dock_enabled and (self.x() <= area.left() + 10 or sprite_left <= area.left() + snap_margin):
            self._settings.docked_side = DockSide.LEFT
            self.move(area.left(), max(area.top(), min(max_y, self.y())))
            self._update_dock_alignment()
            self._current_state = PetState.EDGE_LEFT_PEEK
            self._refresh_sprite_visual()
            self.show_bubble(CharacterLore.pick_line("edge"), 3.5)
            self._sound.play(PetSoundEffect.CHIME)
            return

        if self._settings.edge_dock_enabled and (
            self.x() + self.width() >= area.right() + 1 - 10
            or sprite_right >= area.right() + 1 - snap_margin
        ):
            self._settings.docked_side = DockSide.RIGHT
            self.move(area.right() + 1 - self.width(), max(area.top(), min(max_y, self.y())))
            self._update_dock_alignment()
            self._current_state = PetState.EDGE_RIGHT_PEEK
            self._refresh_sprite_visual()
            self.show_bubble(CharacterLore.pick_line("edge"), 3.5)
            self._sound.play(PetSoundEffect.CHIME)
            return

        if (
            not self._settings.anti_gravity_mode
            and self._settings.gravity_enabled
            and self.y() + self.height() < area.bottom() + 1 - 2
        ):
            self._fall_velocity_y = 0.0
            self._is_falling = True
        else:
            self._clamp_inside_nearest_work_area()
            self._refresh_sprite_visual()

    # ==================== Speech Bubble & Inline Gemini Chat ====================

    def _hide_speech_bubble(self) -> None:
        self._sync_thinking_pinned = False
        self._sync_thinking_visible = False
        self._sync_guard_until = 0.0
        self._bubble_timer.stop()
        self._bubble_custom_offset = None
        self._transient_bubble_size = None
        self._sync_bubble_width = None
        self._speech_bubble.setVisible(False)
        self._speech_bubble.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)

    def _resolve_active_chat_channel_and_models(self) -> tuple[str, str, list[str], str]:
        """返回 API Key 连接状态、官方模型缓存及已选模型。"""
        current = normalize_gemini_model(self._settings.gemini_model)
        cached = GeminiChatService.get_cached_api_key_models(self._settings.gemini_api_key)
        models = list(dict.fromkeys([*cached, current]))
        if self._chat.get_api_error(self._settings):
            badge = "⚠️ API Key 不可用"
        elif self._chat.resolve_chat_provider(self._settings):
            badge = "在线 · API Key"
        else:
            badge = "离线模式"
        return "api_key", badge, models, current

    _CHAT_WINDOW_SIZE_PRESETS: tuple[tuple[str, int, int], ...] = (
        # 各挡均容纳展开的历史列表，避免小、中挡被最小尺寸压成同一大小。
        ("小", 560, 360),
        ("中", 660, 480),
        ("大", 760, 600),
    )

    def _update_chat_size_btn_label(self) -> None:
        w = self._settings.chat_width
        h = self._settings.chat_height
        label = "自动"
        if w is not None and h is not None:
            matched = next(
                (name for name, pw, ph in self._CHAT_WINDOW_SIZE_PRESETS if abs(w - pw) <= 10 and abs(h - ph) <= 10),
                None,
            )
            label = matched if matched else "自"
        self._chat_size_btn.setText(f"📐 {label}")

    def cycle_chat_window_size(self) -> None:
        """在聊天模式中一键循环切换 小 / 中 / 大 窗口尺寸并持久化记忆。"""
        current_w = self._settings.chat_width or self._speech_bubble.width()
        current_h = self._settings.chat_height or self._speech_bubble.height()
        next_idx = 0
        for i, (_name, pw, ph) in enumerate(self._CHAT_WINDOW_SIZE_PRESETS):
            if abs(current_w - pw) <= 10 and abs(current_h - ph) <= 10:
                next_idx = (i + 1) % len(self._CHAT_WINDOW_SIZE_PRESETS)
                break
        else:
            next_idx = next(
                (i for i, (_, pw, ph) in enumerate(self._CHAT_WINDOW_SIZE_PRESETS)
                 if pw * ph > current_w * current_h),
                0,
            )

        _, target_w, target_h = self._CHAT_WINDOW_SIZE_PRESETS[next_idx]
        # 保存所选预设；实际显示尺寸由布局按屏幕限制，避免截断后丢失当前挡位。
        self._settings.chat_width = target_w
        self._settings.chat_height = target_h
        self._settings_service.save(self._settings)
        self._update_chat_size_btn_label()
        self._relayout_stage_top()
        QTimer.singleShot(0, self._scroll_chat_to_bottom)

    def _sync_chat_controls(self) -> None:
        in_chat = bool(self._is_chat_open)
        self._chat_channel_badge.setVisible(in_chat)
        self._chat_clear_btn.setVisible(in_chat)
        self._bubble_settings_btn.setVisible(in_chat)
        self._chat_toolbar.setVisible(in_chat)
        self._chat_session_bar.setVisible(in_chat)
        self._chat_path_panel.setVisible(in_chat)
        self._history_list.setVisible(in_chat and self._history_expanded)
        busy = bool(self._waiting_chat_workers)
        self._history_list.setEnabled(not busy)
        self._new_chat_btn.setEnabled(not busy)
        self._task_folder_btn.setEnabled(not busy)
        self._chat_clear_btn.setEnabled(not busy)
        self._chat_input_panel.setEnabled(not busy and not self._chat_readonly)
        self._chat_input_panel.setVisible(in_chat)
        self._chat_input.setPlaceholderText(
            "反重力实时同步中（只读）" if self._chat_readonly else "输入聊天内容或指令…"
        )
        if not in_chat:
            return

        _channel, badge_text, _models, current = self._resolve_active_chat_channel_and_models()
        error = self._chat.get_api_error(self._settings)
        self._chat_channel_badge.setText(badge_text)
        if error:
            color, rgb = "#FCA5A5", "248, 113, 113"
            tooltip = f"{error}\n点击可打开 API Key 设置或重新获取模型"
        elif self._chat.resolve_chat_provider(self._settings):
            self._last_chat_api_notice = ""
            color, rgb = "#93C5FD", "96, 165, 250"
            tooltip = "使用 Gemini 官方 API Key（点击可获取模型或打开设置）"
        else:
            color, rgb = "#FDE68A", "251, 191, 36"
            tooltip = (
                "当前处于离线模式，点击可切换为官方 API Key 联网对话"
                if self._settings.chat_offline_mode
                else "未填写 Gemini API Key，当前使用离线陪伴；点击可打开设置"
            )
        self._chat_channel_badge.setToolTip(tooltip)
        self._chat_channel_badge.setStyleSheet(
            "QPushButton#ChatChannelBadge { "
            f"background-color: rgba({rgb}, 36); color: {color}; "
            f"border: 1px solid rgba({rgb}, 95); border-radius: 7px; "
            "font-size: 9.5px; font-weight: bold; padding: 1px 7px; margin-left: 6px; }"
            f"QPushButton#ChatChannelBadge:hover {{ border-color: {color}; }}"
        )
        thinking = resolve_effective_thinking_level(current, self._settings.thinking_level)
        self._chat_model_btn.setProperty("target_field", "gemini_model")
        self._chat_model_btn.setProperty("current_model_id", current)
        self._chat_model_btn.setProperty("current_thinking_level", thinking)
        self._chat_model_btn.setText(
            f"{format_model_display_name(current)}  ·  {format_thinking_level_label(thinking)}   ▾"
        )
        self._update_chat_size_btn_label()

    def _effective_chat_offline(self) -> bool:
        """未填写 API Key 时实际按离线运行，用于同步状态徽章与菜单勾选。"""
        return self._settings.chat_offline_mode or not (self._settings.gemini_api_key or "").strip()

    def _populate_connection_menu(self, menu: QMenu) -> None:
        modes = QActionGroup(menu)
        modes.setExclusive(True)
        effective_offline = self._effective_chat_offline()
        for offline, label in ((True, "🌙 离线陪伴"), (False, "🌐 在线 · 官方 API Key")):
            action = menu.addAction(label)
            action.setCheckable(True)
            action.setChecked(effective_offline == offline)
            action.setData(("chat_mode", "offline" if offline else "api_key"))
            modes.addAction(action)
            action.triggered.connect(
                lambda _checked=False, value=offline, act=action: self._select_chat_mode_from_menu(act, value)
            )
        menu.addSeparator()
        refresh = menu.addAction("🔄 获取 / 刷新官方模型")
        refresh.setEnabled(not self._settings.chat_offline_mode)
        refresh.triggered.connect(lambda: self._refresh_chat_models_async(explicit=True))
        settings = menu.addAction("⚙️ 设置 API Key...")
        settings.triggered.connect(self.open_settings_requested)

    def _select_chat_mode_from_menu(self, action: QAction, offline: bool) -> None:
        """点击菜单后按实际生效模式回写勾选，避免无 Key 时在线挡被误选。"""
        self.select_chat_offline_mode(offline)
        group = action.actionGroup()
        if group is None:
            return
        effective_offline = self._effective_chat_offline()
        for item in group.actions():
            item.setChecked(
                item.data() == ("chat_mode", "offline" if effective_offline else "api_key")
            )

    def select_chat_offline_mode(self, offline: bool) -> None:
        previous = self._settings.chat_offline_mode
        if previous == offline:
            # 无 Key 时在线挡实际仍走离线，重复点击时提示补全配置。
            if not offline and not (self._settings.gemini_api_key or "").strip():
                self._show_chat_status_notice(
                    "✦ 在线模式需要 Gemini API Key，请先在设置中填写", force=True
                )
            return
        self._settings.chat_offline_mode = offline
        try:
            self._settings_service.save(self._settings)
        except Exception as exc:
            self._settings.chat_offline_mode = previous
            self._show_chat_status_notice(f"⚠️ 保存聊天模式失败：{exc}", is_error=True, force=True)
            return
        self._chat.reset_conversation()
        self._sync_chat_controls()
        self.settings_synced.emit()
        if offline:
            notice = "✦ 已切换为离线陪伴，可以继续聊天和使用本地互动指令"
        elif self._settings.gemini_api_key.strip():
            notice = "✦ 已切换为官方 API Key 在线模式"
        else:
            notice = "请先在设置中填写 Gemini API Key，当前继续使用离线陪伴"
        self._show_chat_status_notice(notice, force=True)

    def _open_chat_connection_menu(self) -> None:
        menu = QMenu(self._speech_bubble)
        menu.setStyleSheet(STARLIGHT_MENU_QSS)
        hdr = menu.addAction("聊天模式")
        hdr.setEnabled(False)
        menu.addSeparator()
        self._populate_connection_menu(menu)
        self._chat_model_menu = menu
        anchor = self._chat_channel_badge.mapToGlobal(QPoint(0, self._chat_channel_badge.height() + 4))
        menu.popup(anchor)

    def _build_chat_model_menu(self) -> QMenu:
        """构建级联式模型、思考强度与 API Key 设置菜单（不展示新/旧标签）。"""
        channel, badge_text, models, current = self._resolve_active_chat_channel_and_models()
        current_thinking = normalize_thinking_level(self._settings.thinking_level)

        menu = QMenu(self._speech_bubble)
        menu.setStyleSheet(
            STARLIGHT_MENU_QSS
            + """
            QMenu {
                min-width: 245px;
                padding: 6px 4px;
            }
            QMenu::item {
                padding: 6px 24px 6px 14px;
            }
            """
        )

        status = menu.addAction(badge_text)
        status.setEnabled(False)
        connection_menu = menu.addMenu("🌐 聊天模式")
        connection_menu.setStyleSheet(STARLIGHT_MENU_QSS)
        self._populate_connection_menu(connection_menu)

        menu.addSeparator()
        hdr = menu.addAction("模型")
        hdr.setEnabled(False)
        menu.addSeparator()

        for model_id in models:
            disp = format_model_display_name(model_id)
            is_cur_model = model_id == current
            levels = get_supported_thinking_levels(model_id)
            effective_cur_thinking = current_thinking if current_thinking in levels else levels[0]
            if is_cur_model:
                title = f"✓ {disp}   {format_thinking_level_label(effective_cur_thinking)}"
            else:
                title = disp

            sub = menu.addMenu(title)
            sub.setStyleSheet(STARLIGHT_MENU_QSS)
            sub.setProperty("model_id", model_id)
            for lvl in levels:
                lvl_text = "自动 (默认)" if lvl == "auto" else format_thinking_level_label(lvl)
                act = sub.addAction(lvl_text)
                act.setCheckable(True)
                act.setChecked(is_cur_model and lvl == effective_cur_thinking)
                act.setData((model_id, lvl))
                act.triggered.connect(
                    lambda _checked=False, m=model_id, lv=lvl: self.select_chat_model(m, lv)
                )

        menu.addSeparator()
        act_sync = menu.addAction("💬 实时同步反重力对话")
        act_sync.setCheckable(True)
        act_sync.setChecked(self._ag_mirror_active)
        act_sync.triggered.connect(self._on_menu_sync_chat)
        menu.addSeparator()
        act_refresh = menu.addAction("🔄 获取 / 刷新官方模型")
        act_refresh.setEnabled(not self._settings.chat_offline_mode)
        act_refresh.triggered.connect(
            lambda: self._refresh_chat_models_async(explicit=True)
        )
        act_settings = menu.addAction("⚙️ API Key 与模型设置...")
        act_settings.triggered.connect(self.open_settings_requested)
        self._chat_model_menu = menu
        return menu

    def _open_chat_model_menu(self) -> None:
        if self._chat.resolve_chat_provider(self._settings) and not GeminiChatService.get_cached_api_key_models(self._settings.gemini_api_key):
            self._refresh_chat_models_async()
        menu = self._build_chat_model_menu()
        anchor_pos = self._chat_model_btn.mapToGlobal(QPoint(0, self._chat_model_btn.height() + 4))
        menu.popup(anchor_pos)

    def _show_chat_status_notice(
        self, notice_text: str, *, is_error: bool = False, force: bool = False
    ) -> None:
        if not self._is_chat_open:
            return
        if not force and is_error and notice_text == self._last_chat_api_notice:
            return
        if is_error:
            self._last_chat_api_notice = notice_text
        if self._chat_message_rows:
            self._append_chat_turn(role="system", sender="", text=notice_text)
        else:
            self._chat_messages_panel.setVisible(False)
            self._bubble_text_lbl.setVisible(True)
            color = "#FCA5A5" if is_error else "#F6F3FF"
            self._bubble_text_lbl.setStyleSheet(
                f"color: {color}; font-size: 12.5px; padding: 1px 4px 2px 0px; background: transparent;"
            )
            self._bubble_text_lbl.setText(notice_text)
        self._bubble_timer.stop()
        self._relayout_stage_top()

    def _refresh_chat_models_async(self, *, explicit: bool = False) -> None:
        if self._settings.chat_offline_mode:
            return
        if self._chat_models_discovery_worker is not None and self._chat_models_discovery_worker.isRunning():
            return
        if not self._settings.gemini_api_key.strip():
            if explicit:
                self._show_chat_status_notice("请先在设置中填写 Gemini API Key", force=True)
            return
        worker = _ChatModelsDiscoveryWorker(self._chat, self._settings, explicit=explicit, parent=self)
        worker.completed.connect(self._on_chat_models_discovered)
        self._chat_models_discovery_worker = worker
        worker.start()

    def _on_chat_models_discovered(self, models: list[str], err_msg: str, explicit: bool = False) -> None:
        worker = self._chat_models_discovery_worker
        if worker is not None:
            worker.finished.connect(worker.deleteLater)
            if worker.isFinished():
                worker.deleteLater()
            self._chat_models_discovery_worker = None
            if worker._settings.gemini_api_key != self._settings.gemini_api_key:
                return
        self._sync_chat_controls()
        # 切换离线后忽略先前已发出的模型请求结果，避免出现联网成功提示。
        if self._settings.chat_offline_mode:
            return
        if err_msg:
            if explicit or self._chat.get_api_error(self._settings):
                self._show_chat_status_notice(f"⚠️ 获取官方模型失败：{err_msg}", is_error=True, force=explicit)
            return
        if not models:
            if explicit:
                self._show_chat_status_notice("⚠️ 未返回可用模型，请检查 API Key 和模型权限", force=True)
            return
        self._last_chat_api_notice = ""
        if self._is_chat_open and (explicit or self._chat_message_rows):
            self._append_chat_turn(
                role="system", sender="",
                text=f"✦ 已同步 {len(models)} 个官方可用模型，点击上方模型按钮可切换",
            )
            self._relayout_stage_top()

    def select_chat_model(
        self, model_id: str, thinking_level: Optional[str] = None
    ) -> None:
        """切换当前渠道模型及思考强度，立即保存配置并更新聊天工具栏。"""
        cleaned = (model_id or "").strip()
        if not cleaned:
            return

        normalized_model = normalize_gemini_model(cleaned)
        model_changed = self._settings.gemini_model != normalized_model
        self._settings.gemini_model = normalized_model

        thinking_changed = False
        if thinking_level is not None:
            normalized_lvl = normalize_thinking_level(thinking_level)
            if self._settings.thinking_level != normalized_lvl:
                self._settings.thinking_level = normalized_lvl
                thinking_changed = True
        else:
            normalized_lvl = normalize_thinking_level(self._settings.thinking_level)

        if not model_changed and not thinking_changed:
            return

        self._settings_service.save(self._settings)
        if model_changed:
            self._chat.reset_conversation()
        self._sync_chat_controls()
        if self._chat_message_rows:
            self._append_chat_turn(
                role="system",
                sender="",
                text=(
                    f"✦ 已切换为 {format_model_display_name(normalized_model)}"
                    f" · {format_thinking_level_label(normalized_lvl)}"
                ),
            )
        self.settings_synced.emit()

    def _scroll_chat_to_bottom(self) -> None:
        bar = self._bubble_scroll.verticalScrollBar()
        bar.setValue(bar.maximum())

    def _append_chat_turn(
        self,
        *,
        role: str,
        sender: str,
        text: str,
        meta: str = "",
    ) -> tuple[QWidget, Optional[QLabel], QLabel]:
        """向标准聊天消息列表追加一条消息卡片（用户靠右、助手靠左、提示居中）。"""
        self._bubble_text_lbl.setVisible(False)
        self._chat_messages_panel.setVisible(True)

        row = QWidget(self._chat_messages_panel)
        row.setStyleSheet("background: transparent;")
        row_layout = QHBoxLayout(row)
        row_layout.setContentsMargins(0, 0, 0, 0)
        row_layout.setSpacing(6)

        if role == "system":
            notice_lbl = QLabel(text, row)
            notice_lbl.setTextFormat(Qt.TextFormat.PlainText)
            notice_lbl.setWordWrap(True)
            notice_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            notice_lbl.setStyleSheet(
                "color: #A89FD6; font-size: 10.5px; padding: 2px 6px; "
                "background-color: rgba(45, 36, 84, 140); border-radius: 8px;"
            )
            row_layout.addStretch(1)
            row_layout.addWidget(notice_lbl)
            row_layout.addStretch(1)
            self._chat_messages_layout.addWidget(row)
            self._chat_message_rows.append(row)
            QTimer.singleShot(0, self._scroll_chat_to_bottom)
            return row, None, notice_lbl

        is_user = role == "user"
        card = QFrame(row)
        card.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Preferred)
        if is_user:
            card.setStyleSheet(
                """
                QFrame {
                    background-color: #2A4B8D;
                    border: 1px solid #4E78CC;
                    border-radius: 10px;
                }
                """
            )
        else:
            card.setStyleSheet(
                """
                QFrame {
                    background-color: rgba(38, 31, 70, 235);
                    border: 1px solid #524296;
                    border-radius: 10px;
                }
                """
            )

        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(9, 6, 9, 6)
        card_layout.setSpacing(3)

        meta_lbl = QLabel(meta or sender, card)
        meta_lbl.setTextFormat(Qt.TextFormat.PlainText)
        meta_lbl.setStyleSheet(
            f"color: {'#CFE2FF' if is_user else '#C4B5FD'}; "
            "font-size: 10px; font-weight: bold; background: transparent; border: none;"
        )
        card_layout.addWidget(meta_lbl)

        body_lbl = QLabel(text, card)
        body_lbl.setTextFormat(Qt.TextFormat.PlainText)
        body_lbl.setWordWrap(True)
        body_lbl.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        body_lbl.setStyleSheet(
            f"color: {'#FFFFFF' if is_user else '#F6F3FF'}; "
            "font-size: 12.5px; background: transparent; border: none;"
        )
        card_layout.addWidget(body_lbl)

        if is_user:
            row_layout.addStretch(1)
            row_layout.addSpacing(26)
            row_layout.addWidget(card)
        else:
            row_layout.addWidget(card)
            row_layout.addSpacing(26)
            row_layout.addStretch(1)

        self._chat_messages_layout.addWidget(row)
        self._chat_message_rows.append(row)
        QTimer.singleShot(0, self._scroll_chat_to_bottom)
        return row, meta_lbl, body_lbl

    def _choose_task_folder(self) -> None:
        if self._waiting_chat_workers:
            return
        folder = QFileDialog.getExistingDirectory(
            self._speech_bubble, "选择任务文件夹", self._chat_archive.task_folder,
        )
        if not folder:
            return
        try:
            self._chat_archive.set_task_folder(folder)
        except (OSError, ValueError):
            self._chat_save_status.setText("任务文件夹保存失败，请检查目录后重试")
            return
        self._archive_error = ""
        self._update_chat_save_path()
        self._refresh_chat_sessions()
        self._relayout_stage_top()

    def _update_chat_save_path(self) -> None:
        self._task_folder_path.setText(self._chat_archive.task_folder)
        self._task_folder_path.setCursorPosition(0)
        self._task_folder_path.setToolTip(self._chat_archive.task_folder or "尚未选择任务文件夹")
        path = self._chat_archive.session_path()
        self._chat_path.setText(str(path))
        self._chat_path.setCursorPosition(0)
        self._chat_path.setToolTip(str(path))
        if self._archive_error:
            self._chat_save_status.setText("保存失败 · 请检查配置目录是否可写")
        else:
            self._chat_save_status.setText(
                "当前对话保存路径 · 已保存" if path.exists()
                else "当前对话保存路径 · 首条消息后保存"
            )

    def _save_chat_message(self, role: str, text: str, session_id=None) -> None:
        try:
            self._chat_archive.append(role, text, session_id=session_id)
            self._archive_error = ""
        except (OSError, ValueError) as exc:
            self._archive_error = str(exc)
        self._update_chat_save_path()
        self._refresh_chat_sessions()

    def _refresh_chat_sessions(self) -> None:
        self._history_list.blockSignals(True)
        try:
            self._history_list.clear()
            records = self._chat_archive.sessions()
            current = self._chat_archive.session_id
            if not any(record["id"] == current for record in records):
                records.insert(0, {"id": current, "title": "新对话", "updated": "尚未发送消息"})
            for record in records:
                title = " ".join(str(record.get("title", "未命名会话")).split())
                item = QListWidgetItem(title + "\n" + str(record.get("updated", "")).replace("T", " "))
                item.setData(Qt.ItemDataRole.UserRole, record["id"])
                item.setToolTip(title)
                self._history_list.addItem(item)
                if record["id"] == current:
                    self._history_list.setCurrentItem(item)
        finally:
            self._history_list.blockSignals(False)

    def _toggle_chat_history(self, checked: bool) -> None:
        self._history_expanded = checked
        self._history_btn.setChecked(checked)
        self._refresh_chat_sessions()
        self._sync_chat_controls()
        self._relayout_stage_top()
        if checked:
            self._chat_splitter.setSizes([175, 360])

    def _show_chat_history(self) -> None:
        if not self.isVisible():
            self.show()
        self.open_chat_dialog()
        self._toggle_chat_history(True)

    def _clear_chat_rows(self) -> None:
        for row in self._chat_message_rows:
            self._chat_messages_layout.removeWidget(row)
            row.deleteLater()
        self._chat_message_rows.clear()

    def _select_chat_session(self, item, previous=None) -> None:
        if item is None or self._waiting_chat_workers:
            return
        session_id = item.data(Qt.ItemDataRole.UserRole)
        if session_id == self._chat_archive.session_id:
            if self._ag_mirror_active:
                self.stop_antigravity_sync(persist=False)
            return
        self._ag_mirror_active = False
        self._ag_collector.reset()
        self._ag_playback.clear()
        self._ag_baseline = {}
        try:
            record = self._chat_archive.load(session_id)
        except (OSError, ValueError):
            self._refresh_chat_sessions()
            self._chat_save_status.setText("无法读取所选会话，当前对话保持不变")
            return
        self._chat_archive.session_id = session_id
        self._chat_archive.task_folder = record.get("task_folder", "")
        self._chat.restore_conversation(record["messages"])
        self._chat_input.clear()
        self._last_chat_api_notice = ""
        self._archive_error = ""
        self._chat_readonly = record.get("source") == "antigravity"
        # 浏览历史时保留全部卡片，不沿用旧版只显示最近 60 行的限制。
        if not record["messages"]:
            self._bubble_text_lbl.setText("任务文件夹已就绪，想聊什么呀喵？✦")
        self._render_chat_messages(record)
        self._update_chat_save_path()
        self._sync_chat_controls()
        self._chat_input.setFocus()

    def _clear_chat_history(self) -> None:
        if self._waiting_chat_workers:
            return
        self._chat_archive.new_session()
        self._chat_readonly = False
        self._ag_mirror_active = False
        self._ag_collector.reset()
        self._ag_playback.clear()
        self._ag_baseline = {}
        self._archive_error = ""
        self._last_chat_api_notice = ""
        self._clear_chat_rows()
        self._chat.reset_conversation()
        self._chat_input.clear()
        self._chat_messages_panel.setVisible(False)
        self._bubble_text_lbl.setStyleSheet(
            "color: #F6F3FF; font-size: 12.5px; padding: 1px 4px 2px 0px; background: transparent;"
        )
        self._bubble_text_lbl.setText("已开始新对话，旧会话已保存在历史对话中✦")
        self._bubble_text_lbl.setVisible(True)
        self._update_chat_save_path()
        self._refresh_chat_sessions()
        self._sync_chat_controls()
        self._relayout_stage_top()
        self._chat_input.setFocus()

    # ==================== Antigravity 对话气泡播报 ====================

    def toggle_antigravity_sync(self, _checked: bool = False) -> None:
        AppLogger.info(
            "ChatSync",
            f"点击播报：active={self._ag_mirror_active}, "
            f"bubble={self._speech_bubble.isVisible() if self._speech_bubble else 'n/a'}",
        )
        self.set_antigravity_sync_enabled(not self._ag_mirror_active)

    def set_antigravity_sync_enabled(self, enabled: bool, *, silent: bool = False) -> None:
        """按目标状态开启/停止实时同步；菜单复选框、托盘与设置窗口共用一个入口。"""
        want = bool(enabled)
        if want and not self._ag_mirror_active:
            self.start_antigravity_sync(silent=silent)
        elif not want and self._ag_mirror_active:
            self.stop_antigravity_sync(silent=silent)
        # 启动失败（没找到会话等）时回落到实际运行态，保证三处勾选状态一致。
        self._settings.chat_sync_enabled = self._ag_mirror_active
        self._settings_service.save(self._settings)
        self._sync_context_menu_checks()
        self.settings_synced.emit()

    def _on_menu_sync_chat(self, checked: bool) -> None:
        self.set_antigravity_sync_enabled(checked)

    def start_antigravity_sync(self, *, silent: bool = False) -> None:
        """开启反重力对话播报：新消息直接显示在桌宠气泡里。

        silent=True 用于启动时恢复上次的开关，不弹开启提示、不盖掉问候气泡。
        """
        try:
            conversation = self._ag_sync_service.latest_conversation()
            if conversation is None or not conversation.messages:
                if not silent:
                    self.show_bubble("没有找到可播报的反重力对话喵～", 3.5)
                AppLogger.warn("ChatSync", "没有找到可播报的反重力对话")
                return
            self._ag_mirror_active = True
            self._ag_collector.reset()
            self._ag_collector.watch(conversation)
            self._ag_playback.clear()
            self._ag_last_shown = 0.0
            self._ag_last_replace = 0.0
            self._ag_dbg_ticks = 0
            self._ag_dbg_last = time.monotonic()
            # 首拉只记基线：已有的历史不重放，只播之后的新内容。
            self._ag_baseline = self._ag_sync_state.counters()
            self._settings.chat_sync_enabled = True
            self._settings_service.save(self._settings)
            if not silent:
                self.show_bubble("反重力对话播报已开启喵～新消息会出现在这里", 4.0)
            AppLogger.info(
                "ChatSync",
                f"开始播报：{conversation.source_id[:8]}, 基线={len(conversation.messages)}",
            )
        except Exception as exc:
            self._ag_mirror_active = False
            self._ag_collector.reset()
            self._ag_playback.clear()
            self._ag_baseline = {}
            AppLogger.error("ChatSync", "启动反重力播报失败", exc)
            if not silent:
                self.show_bubble(f"反重力播报启动失败：{exc}", 5.0, force=True)

    def stop_antigravity_sync(self, *, silent: bool = False, persist: bool = True) -> None:
        """停止播报。persist=False 用于界面切换这类临时停止，不改用户偏好。"""
        self._sync_thinking_pinned = False
        self._sync_thinking_visible = False
        self._ag_mirror_active = False
        self._ag_collector.reset()
        self._ag_playback.clear()
        self._ag_baseline = {}
        self._ag_last_replace = 0.0
        if persist:
            self._settings.chat_sync_enabled = False
            self._settings_service.save(self._settings)
        if not silent:
            self.show_bubble("已停止反重力播报喵～", 3.0)

    def _auto_resume_antigravity_sync(self) -> None:
        """启动时恢复上次的播报开关：静默开启，不覆盖问候气泡。"""
        if self._ag_mirror_active or not self._settings.chat_sync_enabled:
            return
        # 找不到会话时静默回落，避免托盘/菜单/设置窗口出现"勾着却没运行"的状态。
        self.set_antigravity_sync_enabled(True, silent=True)

    def _refresh_antigravity_sync(self) -> None:
        """十几拍一次的状态采集：读库 → 取差量 → 播队列。"""
        if not self._ag_mirror_active:
            return
        self._ag_dbg_ticks += 1
        now = time.monotonic()
        if now - self._ag_dbg_last >= _SYNC_HEARTBEAT_SEC:
            # 定时器一旦挂掉这里就断流，看日志能立刻分辨是没变化还是没在跑。
            self._ag_dbg_last = now
            AppLogger.info(
                "ChatSync",
                f"心跳 ticks={self._ag_dbg_ticks} 队列={len(self._ag_playback)} "
                f"{self._ag_collector.stats}",
            )
        try:
            self._ag_collector.poll()
            self._consume_antigravity_changes()
            self._play_antigravity_queue()
        except Exception as exc:
            AppLogger.error("ChatSync", "反重力播报刷新失败", exc)

    def _consume_antigravity_changes(self) -> None:
        """读状态差量：续写就地刷新，新消息进播放队列。"""
        for name, data in self._ag_sync_state.take_since(self._ag_baseline):
            if name == "status":
                if data.get("switched"):
                    self.show_bubble(f"✦ 已切换到新的反重力对话：{data.get('title', '')}", 3.0)
                continue
            items = (data.get("items") or []) if isinstance(data, dict) else []
            if not items:
                continue
            if data.get("replace"):
                newest = items[-1]
                if self._ag_playback:
                    # 队列末条就是这条消息，换成最新文本，保持播放顺序。
                    self._ag_playback[-1] = newest
                elif not self._is_chat_open:
                    # 续写刷新走气泡；聊天面板开着时每条都追加系统行，会堆重复卡片。
                    now = time.monotonic()
                    if not self._ag_last_replace or now - self._ag_last_replace >= _SYNC_REPLACE_INTERVAL:
                        self._ag_last_replace = now
                        self._show_sync_event(newest)
                continue
            self._ag_playback.extend(items)
            # 积压时丢最旧的，气泡追最新内容而不是慢慢补历史。
            while len(self._ag_playback) > _SYNC_QUEUE_LIMIT:
                self._ag_playback.popleft()

    def _play_antigravity_queue(self) -> None:
        """逐条播；你自己发的话不等节奏，立刻上气泡，其余按最小间隔排。"""
        if not self._ag_playback:
            return
        now = time.monotonic()
        head = self._ag_playback[0]
        if (
            head.role != "user"
            and self._ag_last_shown
            and now - self._ag_last_shown < _SYNC_MIN_INTERVAL
        ):
            return
        self._ag_last_shown = now
        event = self._ag_playback.popleft()
        self._show_sync_event(event)
        AppLogger.info("ChatSync", f"播报：{event.role}/{event.kind} {event.text[:24]}")

    def _show_sync_event(self, event: SyncEvent) -> None:
        if self._sync_thinking_pinned and event.kind != "thinking":
            return
        text = event.text.strip()
        if not text:
            return
        if event.kind == "thinking":
            message = f"💭 {text}"
        elif event.kind == "error":
            message = f"⚠ {text}"
        elif event.kind == "tool":
            message = f"⚙ {text}"
        elif event.role == "user":
            message = f"你：{text}"
        else:
            message = text
        self._sync_thinking_visible = event.kind == "thinking"
        duration = max(3.5, min(9.0, len(message) / 28.0))
        # 播报期间锁一个固定宽度：文本每长一点都按最长行重算宽度，视觉上会一直抖。
        self._sync_bubble_width = 320
        self.show_bubble(message, duration, force=self._is_chat_open, from_sync=True)
        self._sync_guard_until = time.monotonic() + duration

    def _render_chat_messages(self, record: dict) -> None:
        self._clear_chat_rows()
        self._chat_messages_panel.setVisible(bool(record["messages"]))
        self._bubble_text_lbl.setVisible(not record["messages"])
        for message in record["messages"]:
            sender = "你" if message["role"] == "user" else "Gemini 娘"
            self._append_chat_turn(
                role=message["role"], sender=sender, text=message["text"],
                meta=f"{sender} · {message.get('time', '').replace('T', ' ')}",
            )
        self._relayout_stage_top()

    def show_bubble(
        self, message: str, duration_sec: float, force: bool = False, from_sync: bool = False
    ) -> None:
        if self._sync_thinking_pinned and not from_sync:
            return
        if not from_sync:
            # 闲聊/宠物自己的回复恢复自适应宽度，别跟着同步播报的锁宽走。
            self._sync_bubble_width = None
        if not from_sync and not force and time.monotonic() < self._sync_guard_until:
            # 正在播反重力对话，闲聊别插话把内容顶掉；强制气泡（宠物自己的回复）照常显示。
            return
        # 实时同步播报绕过"自动弹出对话气泡"开关：同步一开，新消息始终能上气泡。
        if (
            not force
            and not from_sync
            and not self._settings.show_speech_bubble
            and not self._is_chat_open
        ):
            return
        if not self.isVisible():
            return
        if not from_sync:
            self._sync_thinking_visible = False
        if self._is_chat_open:
            if not force:
                return
            self._is_forced_bubble_active = True
            if self._chat_message_rows:
                self._append_chat_turn(role="system", sender="", text=message)
            else:
                self._chat_messages_panel.setVisible(False)
                self._bubble_text_lbl.setStyleSheet(
                    "color: #F6F3FF; font-size: 12.5px; padding: 1px 4px 2px 0px; background: transparent;"
                )
                self._bubble_text_lbl.setVisible(True)
                self._bubble_text_lbl.setText(message)
            self._sync_chat_controls()
            self._speech_bubble.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, False)
            self._speech_bubble.setVisible(True)
            self._sync_bubble_window_styles()
            self._relayout_stage_top()
            self._bubble_timer.stop()
            return

        self._is_forced_bubble_active = force
        self._chat_messages_panel.setVisible(False)
        self._bubble_text_lbl.setStyleSheet(
            "color: #F6F3FF; font-size: 12.5px; padding: 1px 4px 2px 0px; background: transparent;"
        )
        self._bubble_text_lbl.setVisible(True)
        scroll_position = self._bubble_scroll.verticalScrollBar().value()
        self._bubble_text_lbl.setText(message)
        self._speech_bubble.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self._speech_bubble.setVisible(True)
        self._sync_bubble_window_styles()
        self._relayout_stage_top()
        self._bubble_timer.stop()
        if self._sync_thinking_pinned:
            # 续写时保留用户正在阅读的位置，不自动滚到新增内容末尾。
            self._bubble_scroll.verticalScrollBar().setValue(scroll_position)
        else:
            self._bubble_timer.start(max(200, int(round(duration_sec * 1000.0))))

    def _on_bubble_timeout(self) -> None:
        if self._sync_thinking_pinned:
            return
        self._is_forced_bubble_active = False
        if (
            not self._is_chat_open
            and not self._is_bubble_hovered
            and not self._speech_bubble.is_resizing()
        ):
            self._hide_speech_bubble()
            self._relayout_stage_top()

    def open_chat_mode(self) -> None:
        """从桌面/托盘菜单进入聊天模式：先保证桌宠可见，再展开聊天面板。"""
        self._sync_thinking_pinned = False
        self._sync_thinking_visible = False
        if not self.isVisible():
            self.show()
        self.open_chat_dialog()
        self.raise_()

    def open_chat_dialog(self) -> None:
        self._is_chat_open = True
        self._toggle_chat_history(True)
        invalid_reason = self._chat.get_api_error(self._settings)
        if self._chat_message_rows:
            self._bubble_text_lbl.setVisible(False)
            self._chat_messages_panel.setVisible(True)
            if invalid_reason:
                self._show_chat_status_notice(
                    f"⚠️ API Key 不可用（{invalid_reason}），请检查 API Key 后重新获取模型",
                    is_error=True,
                    force=False,
                )
            QTimer.singleShot(0, self._scroll_chat_to_bottom)
        else:
            self._chat_messages_panel.setVisible(False)
            self._bubble_text_lbl.setVisible(True)
            if invalid_reason:
                notice = (
                    f"⚠️ API Key 不可用（{invalid_reason}）\n"
                    "请在设置中检查 Gemini API Key，再点击顶部状态重新获取模型。"
                )
                self._last_chat_api_notice = notice
                self._bubble_text_lbl.setStyleSheet(
                    "color: #FCA5A5; font-size: 12.5px; padding: 1px 4px 2px 0px; background: transparent;"
                )
                self._bubble_text_lbl.setText(notice)
            elif not self._speech_bubble.isVisible() or self._bubble_text_lbl.text().startswith("⚠️ API Key 不可用"):
                self._bubble_text_lbl.setStyleSheet(
                    "color: #F6F3FF; font-size: 12.5px; padding: 1px 4px 2px 0px; background: transparent;"
                )
                self._bubble_text_lbl.setText("想聊什么呀喵？输入文字或指令试试✦")
        self._speech_bubble.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, False)
        self._speech_bubble.setVisible(True)
        self._sync_bubble_window_styles()
        self._relayout_stage_top()
        self._bubble_timer.stop()
        self._speech_bubble.raise_()
        self._speech_bubble.activateWindow()
        self._chat_input.setFocus()
        self._set_temporary_pose(PetState.LISTENING, 10.0)

    def set_speech_bubble_enabled(self, enabled: bool) -> None:
        self._settings.show_speech_bubble = enabled
        self.apply_settings_to_window()
        if enabled:
            self.show_bubble("已开启宠物对话气泡，会自动陪你聊天喵✦", 4.0, force=True)
        elif not self._is_chat_open and not self._ag_mirror_active:
            self._sync_chat_controls()
            self._hide_speech_bubble()
            self._relayout_stage_top()

    def _on_menu_show_speech_bubble(self, checked: bool) -> None:
        self.set_speech_bubble_enabled(bool(checked))
        self._settings_service.save(self._settings)
        self.settings_synced.emit()

    def _toggle_chat(self) -> None:
        if not self._is_chat_open:
            self.open_chat_dialog()
            return
        self._is_chat_open = False
        self._sync_chat_controls()
        self._sync_bubble_window_styles()
        if self._settings.show_speech_bubble and not self._chat_message_rows:
            self._bubble_timer.stop()
            self._bubble_timer.start(3000)
        else:
            self._hide_speech_bubble()
        self._relayout_stage_top()

    def _close_bubble_click(self) -> None:
        self._is_chat_open = False
        self._is_forced_bubble_active = False
        self._sync_chat_controls()
        self._hide_speech_bubble()
        self._relayout_stage_top()

    def _submit_chat(self) -> None:
        if self._chat_readonly:
            self._chat_save_status.setText("反重力实时同步中，输入已禁用喵")
            return
        text = self._chat_input.text().strip()
        if not text or self._waiting_chat_workers:
            return
        self._chat_input.clear()
        self._is_chat_open = True
        self._sync_chat_controls()

        now_str = datetime.now().strftime("%H:%M")
        char_name = (self._bubble_char_name_lbl.text() or "").strip() or "Gemini 娘"
        provider = self._chat.resolve_chat_provider(self._settings)
        _, _, _, active_model = self._resolve_active_chat_channel_and_models()
        thinking_lvl = resolve_effective_thinking_level(active_model, self._settings.thinking_level)
        if provider:
            shown_model = active_model
            disp_model = format_model_display_name(shown_model)
            model_tag = (
                f"{disp_model} · {format_thinking_level_label(thinking_lvl)}"
                if thinking_lvl != "auto"
                else disp_model
            )
        else:
            model_tag = "离线陪伴"

        self._save_chat_message("user", text)
        self._append_chat_turn(role="user", sender="你", text=text, meta=f"你 · {now_str}")
        pending_target = self._append_chat_turn(
            role="assistant",
            sender=char_name,
            text="正在思考中…✦",
            meta=f"✦ {char_name} · {model_tag} · {now_str}",
        )

        self._speech_bubble.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, False)
        self._speech_bubble.setVisible(True)
        self._sync_bubble_window_styles()
        self._bubble_timer.stop()
        self._relayout_stage_top()
        QTimer.singleShot(0, self._scroll_chat_to_bottom)

        metrics = self._sys_monitor.sample()
        target_info = (pending_target, char_name, model_tag, self._chat_archive.session_id)
        worker = _ChatWorker(
            self._chat,
            text,
            self._settings,
            metrics,
            target_info=target_info,
            parent=self,
        )
        self._active_chat_workers.append(worker)
        self._waiting_chat_workers.add(worker)
        self._sync_chat_controls()
        self._set_temporary_pose(PetState.THINKING, 90.0)
        worker.reply_ready.connect(self._on_chat_reply_ready)
        worker.finished.connect(lambda w=worker: self._cleanup_chat_worker(w))
        worker.start()

    def _cleanup_chat_worker(self, worker: _ChatWorker) -> None:
        self._waiting_chat_workers.discard(worker)
        if worker in self._active_chat_workers:
            self._active_chat_workers.remove(worker)
        worker.deleteLater()

    def _on_chat_reply_ready(
        self,
        reply: ChatReply,
        target_info: Optional[tuple] = None,
        worker: Optional[_ChatWorker] = None,
    ) -> None:
        if worker is not None:
            self._waiting_chat_workers.discard(worker)
        else:
            self._waiting_chat_workers.clear()

        target_pose = reply.suggested_state or PetState.HAPPY
        if target_pose == PetState.FLOAT and not self._settings.anti_gravity_mode:
            self._settings.anti_gravity_mode = True
            self.apply_settings_to_window()
            self.settings_synced.emit()
        elif target_pose == PetState.PAT:
            self.trigger_pat()

        self._set_temporary_pose(target_pose, 7.0)
        if self._is_chat_open:
            self._sync_chat_controls()
        now_str = datetime.now().strftime("%H:%M")

        if target_info is not None:
            pending_target, char_name, model_tag = target_info[:3]
            session_id = target_info[3] if len(target_info) > 3 else self._chat_archive.session_id
            self._save_chat_message("assistant", reply.text, session_id=session_id)
            if session_id != self._chat_archive.session_id:
                return
            row, meta_lbl, body_lbl = pending_target
            if row in self._chat_message_rows:
                body_lbl.setText(reply.text)
                if meta_lbl is not None:
                    meta_lbl.setText(f"✦ {char_name} · {model_tag} · {now_str}")
                if self._is_chat_open:
                    self._bubble_timer.stop()
                    self._relayout_stage_top()
                    QTimer.singleShot(0, self._scroll_chat_to_bottom)
                else:
                    self.show_bubble(reply.text, 7.0, force=True)
                self._sound.play(PetSoundEffect.SPARKLE)
                return

        if self._is_chat_open:
            char_name = (self._bubble_char_name_lbl.text() or "").strip() or "Gemini 娘"
            provider = self._chat.resolve_chat_provider(self._settings)
            _, _, _, active_model = self._resolve_active_chat_channel_and_models()
            thinking_lvl = normalize_thinking_level(self._settings.thinking_level)
            if provider:
                shown_model = active_model
                disp_model = format_model_display_name(shown_model)
                model_tag = (
                    f"{disp_model} · {format_thinking_level_label(thinking_lvl)}"
                    if thinking_lvl != "auto"
                    else disp_model
                )
            else:
                model_tag = "离线陪伴"
            self._append_chat_turn(
                role="assistant",
                sender=char_name,
                text=reply.text,
                meta=f"✦ {char_name} · {model_tag} · {now_str}",
            )
            self._bubble_timer.stop()
            self._relayout_stage_top()
            QTimer.singleShot(0, self._scroll_chat_to_bottom)
        else:
            self.show_bubble(reply.text, 7.0, force=True)
        self._sound.play(PetSoundEffect.SPARKLE)

    # ==================== Particles ====================

    def _spawn_star_burst(self, center_x: float, center_y: float, count: int) -> None:
        if not self._settings.particles_enabled:
            return
        palette = ["#A78BFA", "#38BDF8", "#F472B6", "#FBBF24"]
        for i in range(count):
            ang = (2.0 * math.pi * i) / count + random.random() * 0.4
            spd = 35.0 + random.random() * 55.0
            self._spawn_particle(
                "✦",
                palette[i % len(palette)],
                center_x,
                center_y,
                math.cos(ang) * spd,
                math.sin(ang) * spd - 20.0,
                1.15,
            )

    def _spawn_particle(
        self,
        glyph: str,
        color_hex: str,
        x: float,
        y: float,
        vx: float,
        vy: float,
        max_life: float,
    ) -> None:
        if len(self._particles) > 36:
            return
        self._particles.append(
            _Particle(
                glyph=glyph,
                color=QColor(color_hex),
                x=x,
                y=y,
                vx=vx,
                vy=vy,
                life=0.0,
                max_life=max_life,
                spin=(random.random() - 0.5) * 90.0,
                font_size=13 if glyph == "Zzz" else 16,
            )
        )

    def _update_particles(self, dt: float) -> None:
        alive: list[_Particle] = []
        for p in self._particles:
            p.life += dt
            if p.life >= p.max_life:
                continue
            p.x += p.vx * dt
            p.y += p.vy * dt
            p.angle += p.spin * dt
            alive.append(p)
        self._particles = alive

    # ==================== Menu & Quick Action Slots ====================

    def _toggle_anti_gravity(self) -> None:
        self._settings.anti_gravity_mode = not self._settings.anti_gravity_mode
        if self._settings.anti_gravity_mode:
            self._settings.docked_side = DockSide.NONE
        else:
            self._is_falling = True

        self._settings_service.save(self._settings)
        self.apply_settings_to_window()
        self.settings_synced.emit()

        if self._settings.anti_gravity_mode:
            self._set_temporary_pose(PetState.FLOAT, 4.0, CharacterLore.pick_line("float"))
            self._sound.play(PetSoundEffect.SPARKLE)
            self._spawn_star_burst(self.width() * 0.5, self.height() - self._sprite_height_px() * 0.5, 9)
        else:
            self.show_bubble("反重力模块关闭，安全降落喵～", 3.0)

    def _set_pin_on_desktop_enabled(self, pinned: bool) -> None:
        self._settings.auto_walk = not pinned
        if pinned and self._is_walking:
            self._is_walking = False
        self._settings_service.save(self._settings)
        self.apply_settings_to_window()
        self.settings_synced.emit()

    def _on_menu_pin_on_desktop(self, checked: bool) -> None:
        self._set_pin_on_desktop_enabled(checked)

    def _is_dozing(self) -> bool:
        """常态睡姿包括左右贴边，用于慢呼吸和 Zzz 特效。"""
        return (
            bool(self._settings.doze_mode)
            and self._override_pose is None
        )

    def _doze_zzz_anchor(self) -> tuple[float, float]:
        """打盹 Zzz 的落点：横向按画风贴到头顶右侧（贴边时从边缘向内收），纵向按头部占比。"""
        rect = self._get_local_sprite_rect()
        x_ratio, y_ratio = _DOZE_ZZZ_ANCHOR.get(
            self._settings.style_mode, _DOZE_ZZZ_ANCHOR[PetStyleMode.CHIBI]
        )
        if self._settings.docked_side == DockSide.LEFT:
            x = rect.right() - rect.width() * 0.28
        elif self._settings.docked_side == DockSide.RIGHT:
            x = rect.left() + rect.width() * 0.28
        else:
            x = rect.center().x() + rect.width() * x_ratio
        return x, rect.top() + rect.height() * y_ratio

    def _apply_doze_transition(self) -> None:
        """打盹模式开关变化时播放入睡/睡醒过渡；菜单、托盘与设置窗口的改动都会经过这里。"""
        doze = bool(self._settings.doze_mode)
        if doze == self._doze_applied:
            return
        self._doze_applied = doze
        self._is_walking = False
        self._is_sleeping = False
        self._current_state = self._resolve_resting_state()
        if self._settings.docked_side != DockSide.NONE:
            self._override_pose = None
            self._refresh_sprite_visual()
            return
        if not self._first_shown or not self.isVisible():
            return
        if doze:
            # 先打个哈欠，临时动作结束后自动回落到睡姿
            self._set_temporary_pose(PetState.YAWN, 2.5, "那我在这里打个盹待机喵…Zzz")
        else:
            self._set_temporary_pose(PetState.STRETCH, 3.0, "睡醒啦～伸个懒腰继续陪你喵✦")

    def set_doze_mode_enabled(self, enabled: bool) -> None:
        if bool(enabled) == bool(self._settings.doze_mode):
            return
        self._settings.doze_mode = bool(enabled)
        self._settings_service.save(self._settings)
        self.apply_settings_to_window()
        self.settings_synced.emit()

    def _on_menu_doze_mode(self, checked: bool) -> None:
        self.set_doze_mode_enabled(bool(checked))

    def _on_menu_scale(self, scale: float) -> None:
        old_bottom = float(self.y() + self.height())
        # 从菜单直接选缩放时结束设置窗口的临时预览，避免预览值覆盖本次选择
        self._preview_scale = None
        self._preview_opacity = None
        self._settings.scale = scale
        self.apply_settings_to_window()
        if self._settings.docked_side != DockSide.NONE:
            self._reanchor_docked_position()
        else:
            self.move(self.x(), int(round(old_bottom - self.height())))
            self._clamp_inside_nearest_work_area()
        self._settings_service.save(self._settings)
        self.settings_synced.emit()

    def _sync_scale_actions(self) -> None:
        """右键菜单弹出前按当前缩放同步勾选，避免与设置窗口里的滑杆不一致。"""
        current = float(self._settings.scale)
        for value, action in getattr(self, "_scale_actions", {}).items():
            action.blockSignals(True)
            try:
                action.setChecked(abs(value - current) < 0.001)
            finally:
                action.blockSignals(False)

    def _sync_context_menu_checks(self) -> None:
        """统一刷新右键菜单的勾选状态，保证菜单、托盘与设置窗口显示一致。"""
        states = (
            ("_act_pin_on_desktop", not self._settings.auto_walk),
            ("_act_doze", self._settings.doze_mode),
            ("_act_show_bubble", self._settings.show_speech_bubble),
            ("_act_sync_chat", self._ag_mirror_active),
            ("_act_follow_cli", self._settings.follow_antigravity_cli),
            ("_act_runtime_log", self._settings.enable_runtime_log),
        )
        for attr, checked in states:
            action = getattr(self, attr, None)
            if action is None:
                continue
            action.blockSignals(True)
            try:
                action.setChecked(bool(checked))
            finally:
                action.blockSignals(False)

        current_style = self._settings.style_mode
        for mode, action in getattr(self, "_style_actions", {}).items():
            action.blockSignals(True)
            try:
                action.setChecked(mode == current_style)
            finally:
                action.blockSignals(False)
        self._sync_scale_actions()

    def _set_runtime_log_enabled(self, enabled: bool) -> None:
        self._settings.enable_runtime_log = enabled
        AppLogger.set_enabled(enabled)
        self._settings_service.save(self._settings)
        self.apply_settings_to_window()
        self.settings_synced.emit()

        daily_name = datetime.now().strftime("%Y-%m-%d.log")
        log_file = os.path.join(AppLogger.log_directory(), daily_name)
        self.show_bubble(
            f"已开启运行日志，将写入 {log_file} 📝"
            if enabled
            else "已关闭运行日志记录喵～",
            4.0,
            force=True,
        )

    def _toggle_runtime_log(self) -> None:
        self._set_runtime_log_enabled(not self._settings.enable_runtime_log)

    def _on_menu_runtime_log(self, checked: bool) -> None:
        self._set_runtime_log_enabled(checked)

    def sync_character_profile(self, profile: CharacterProfile) -> None:
        self.setWindowTitle(profile.name)
        self._bubble_char_name_lbl.setText(profile.name)

    def toggle_style_mode(self) -> None:
        next_mode = (
            PetStyleMode.STANDARD
            if self._settings.style_mode == PetStyleMode.CHIBI
            else PetStyleMode.CHIBI
        )
        self._set_style_mode(next_mode)

    def _set_style_mode(self, mode: PetStyleMode) -> None:
        self._settings.style_mode = mode
        self._settings_service.save(self._settings)
        self.apply_settings_to_window()
        self.settings_synced.emit()

        msg = (
            "已切换为「标准立绘版」形象喵～修长比例登场✦"
            if mode == PetStyleMode.STANDARD
            else "已切换为「Q 版猫娘」形象喵～软萌模式✦"
        )
        # 形象选择持久化，但不改变正在执行的动作、打盹或行走状态。
        if self._settings.docked_side != DockSide.NONE:
            self._reanchor_docked_position()
        self._refresh_sprite_visual()
        self._update_sprite_transforms()
        self._stage_canvas.update()
        self.show_bubble(msg, 4.2)

        self._sound.play(PetSoundEffect.SPARKLE)
        self._spawn_star_burst(self.width() * 0.5, self.height() - self._sprite_height_px() * 0.55, 8)
