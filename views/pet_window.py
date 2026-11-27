"""Main translucent desktop pet window with 60fps animations, walk rigs, bubbles, and menus."""

from __future__ import annotations

import ctypes
import math
import os
from collections import deque
import random
import threading
import time
import uuid
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
    QPainterPath,
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
    QWidgetAction,
)

from models import (
    API_PROVIDER_AGENT_PLATFORM,
    API_PROVIDER_GEMINI_DEVELOPER,
    CharacterLore,
    CharacterProfile,
    DEFAULT_GEMINI_MODEL,
    DockSide,
    PetSettings,
    PetState,
    PetStyleMode,
    format_api_provider_label,
    format_model_display_name,
    format_thinking_level_label,
    get_supported_thinking_levels,
    normalize_api_provider,
    normalize_gemini_model,
    normalize_thinking_level,
    resolve_effective_thinking_level,
)
from services.action_clip import ActionClip, analyze_pixmap_metrics
from services.antigravity_chat_collector import AntigravityChatCollector, SyncEvent
from services.antigravity_chat_sync_service import AntigravityChatSyncService
from services.app_logger import AppLogger
from services.chat_history_service import ChatHistoryService
from services.chat_sync_state import ChatSyncState
from services.gemini_chat_service import ChatReply, GeminiChatService
from services.pet_physics import (
    DragTrailEstimator,
    ParticleSystem,
    PetMotionEngine,
    PoseMotionInput,
    ThrowBodyState,
    TransformSnapshot,
    landing_squash_depth,
    step_throw_physics,
)
from services.settings_service import SettingsService
from services.shared_menu import MENU_SEPARATOR, populate_shared_menu
from services.sound_service import PetSoundEffect, SoundService
from services.sprite_manager import SpriteManager, WalkRigAssets
from services.stage_renderer import (
    PetStageRenderer,
    StageCrossfader,
    VisualRenderSlot,
    compute_local_sprite_rect,
)
from services.system_monitor_service import SystemMetrics, SystemMonitorService
from themes import STARLIGHT_MENU_QSS


# Win32 tool-window style helper.
_GWL_EXSTYLE = -20
_WS_EX_TOOLWINDOW = 0x00000080

# 反重力播报：采集间隔、续写刷新的最小间隔（秒）与积压上限。
# 采集和刷新都压在百毫秒级，靠「只解尾部步骤」把开销hold住，延迟才像实时。
_SYNC_POLL_MS = 120
_SYNC_MIN_INTERVAL = 0.25
_SYNC_REPLACE_INTERVAL = 0.12
_SYNC_QUEUE_LIMIT = 10
_SYNC_HEARTBEAT_SEC = 60.0
_SYNC_BUBBLE_IDLE_MS = 3000
# 反重力播报联动动作：每轮新对话随机选定前 4 个忙碌动作之一并在完成前固定，完成后切开心（hum）动作；
# Q 版与标准立绘共用同一组 PetState，由 SpriteManager 按当前形象加载对应贴图。
_SYNC_BUSY_POSES: tuple[PetState, ...] = (
    PetState.BUSY_LEFT,
    PetState.BUSY_RIGHT,
    PetState.SWAMPED_LEFT,
    PetState.SWAMPED_RIGHT,
)
_SYNC_DONE_POSE = PetState.HUM
_SYNC_DONE_POSE_DURATION_S = 4.5

# 无操作随机台词：定时器检测周期（毫秒），具体间隔由当前角色档案的气泡活跃程度决定
_IDLE_SPEECH_CHECK_MS = 1000

# 打盹 Zzz 的落点比例（相对贴图内容矩形：横向从中心右偏、纵向从顶部下移）。
# Q 版头部约占贴图高度 1/3、宽度 2/3，0.18H 正好停在头顶右侧发际（参考图效果）；
# 标准立绘头高只有约 1/9（实测 1603px 图高约 175px），沿用 0.18H 会掉到肩颈，
# 所以按头部占比把纵向缩到 0.08H，横向一并收到发际内侧 0.12W。
_DOZE_ZZZ_ANCHOR: dict[PetStyleMode, tuple[float, float]] = {
    PetStyleMode.CHIBI: (0.18, 0.18),
    PetStyleMode.STANDARD: (0.12, 0.08),
}

# 身体连戳炸毛检测：连续点击身体按阶梯升级 烦恼 -> 鼓嘴 -> 炸毛 -> 生气，每阶段（含生气）播完回常态后才可继续点击
_POKE_COMBO_WINDOW_S = 2.0
_POKE_STAGE_MEMORY_S = 4.5
_POKE_ANNOYED_COUNT = 3
_POKE_PUFFED_COUNT = 4
_POKE_REDHOT_COUNT = 5
_BODY_DOUBLE_CLICK_QUOTA_DELAY_MS = 360
_ANNOYED_POSE_DURATION_S = 1.8
_PUFFED_POSE_DURATION_S = 2.0
_REDHOT_POSE_DURATION_S = 2.6
_SULK_POSE_DURATION_S = 5.0

# 拖拽摇晃/画圈炸毛检测：融合双轴快速折返甩动与连续同向画圈角位移，
# 完整流程：摇晃 -> 烦恼(ANNOYED) -> 鼓嘴(PUFFED) -> 炸毛(REDHOT) -> 松手下落眩晕(DIZZY) -> 生气(SULK)
_SHAKE_STROKE_MIN_PX = 10.0
_SHAKE_MIN_SPEED_PX_S = 240.0
_SHAKE_STROKE_MAX_AGE_S = 0.45
_SHAKE_REVERSAL_COOLDOWN_S = 0.045
_SHAKE_IDLE_RESET_S = 0.32
_SHAKE_WHIP_GAIN = 0.85
_SHAKE_SPIN_SEG_MIN_PX = 14.0
_SHAKE_SPIN_MIN_SPEED_PX_S = 180.0
_SHAKE_SPIN_WARMUP_RAD = math.radians(60.0)
_SHAKE_SPIN_GAIN_PER_RAD = 0.60
_SHAKE_DECAY_PER_SEC = 0.65
_SHAKE_ENERGY_MAX = 5.0
_SHAKE_ANNOYED_THRESHOLD = 1.0
_SHAKE_PUFFED_THRESHOLD = 2.0
_SHAKE_REDHOT_THRESHOLD = 3.0
_SHAKE_ANNOYED_DURATION_S = 0.70
_SHAKE_PUFFED_DURATION_S = 0.75
_SHAKE_STOP_RECOVER_S = 2.0
_SHAKE_REDHOT_DURATION_S = _SHAKE_STOP_RECOVER_S
_SHAKE_HOLD_ACTIVE_S = 0.65
_SHAKE_RELEASE_GRACE_S = 1.0
_DIZZY_POSE_DURATION_S = 2.8
_POST_DIZZY_PAT_GRACE_S = 0.75
_HOVER_PAT_COOLDOWN_S = 0.40


def _is_primary_mouse_button_held() -> bool:
    """查询系统主鼠标按键的真实物理按下状态，防止漏收松开事件导致拖拽句柄卡死。"""
    if os.name == "nt":
        try:
            user32 = ctypes.windll.user32
            vk = 0x02 if user32.GetSystemMetrics(23) else 0x01
            return bool(user32.GetAsyncKeyState(vk) & 0x8000)
        except Exception:
            pass
    return bool(QGuiApplication.mouseButtons() & Qt.MouseButton.LeftButton)


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

    def focusOutEvent(self, event) -> None:
        self._owner._on_stage_mouse_grab_lost()
        super().focusOutEvent(event)

    def event(self, ev: QEvent) -> bool:
        if ev.type() in (QEvent.Type.UngrabMouse, QEvent.Type.WindowDeactivate):
            self._owner._on_stage_mouse_grab_lost()
        return super().event(ev)


class _SpeechBubbleWindow(QFrame):
    """独立无边框对话与气泡浮窗，支持鼠标拖动八向边缘缩放大小。"""

    RESIZE_MARGIN = 8

    def __init__(self, owner: "PetWindow") -> None:
        flags = (
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.Tool
            | Qt.WindowType.WindowStaysOnTopHint
        )
        super().__init__(owner, flags)
        self._owner = owner
        self.setObjectName("SpeechBubble")
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setMouseTracking(True)

        self._resizing_edges: Optional[tuple[bool, bool, bool, bool]] = None
        self._resize_start_global = QPoint()
        self._resize_start_geom = QRect()
        self._drag_start_global = QPoint()
        self._drag_start_pos = QPoint()
        self._dragging = False

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
            if self._owner._is_chat_open:
                self._dragging = True
                self._drag_start_global = event.globalPosition().toPoint()
                self._drag_start_pos = self.pos()
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

        if self._dragging:
            delta = event.globalPosition().toPoint() - self._drag_start_global
            pos = self._owner._clamp_bubble_pos_to_screen(self._drag_start_pos + delta, self.size())
            self.move(pos)
            self._owner._on_bubble_moved_by_user(pos)
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
        if self._dragging and event.button() == Qt.MouseButton.LeftButton:
            self._dragging = False
            if QWidget.mouseGrabber() is self:
                self.releaseMouse()
            self._owner._on_bubble_moved_by_user(self.pos())
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


class _ChatActionButton(QPushButton):
    """用矢量图标绘制会话删除和文件打开按钮，避免系统字体替换图标。"""

    def __init__(self, action: str, parent: QWidget) -> None:
        super().__init__(parent)
        self._action = action
        self.setObjectName("ChatDeleteButton" if action == "delete" else "ChatOpenFileButton")
        if action == "delete":
            self.setFixedSize(28, 28)
        else:
            self.setFixedSize(92, 28)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setAccessibleName("删除对话" if action == "delete" else "打开当前会话文件")

    def paintEvent(self, _event: QEvent) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        active = self.isEnabled()
        hovered = active and self.underMouse()
        pressed = active and self.isDown()
        fill = "#4B3A8F" if pressed else "#302451" if hovered else "#211A3B"
        border = "#A78BFA" if hovered else "#66539E"
        foreground = "#FFFFFF" if hovered else "#DDD2FF"
        if not active:
            foreground, border = "#82779E", "#4B416A"
        painter.setBrush(QColor(fill))
        painter.setPen(QPen(QColor(border), 1.2))
        painter.drawRoundedRect(QRectF(self.rect()).adjusted(0.8, 0.8, -0.8, -0.8), 7, 7)

        pen = QPen(QColor(foreground), 1.5)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        if self._action == "delete":
            painter.translate(8, 6)
            painter.drawLine(1, 4, 11, 4)
            painter.drawLine(4, 2, 8, 2)
            bin_outline = QPainterPath()
            bin_outline.moveTo(2, 6)
            bin_outline.lineTo(3, 15)
            bin_outline.quadTo(3, 17, 5, 17)
            bin_outline.lineTo(7, 17)
            bin_outline.quadTo(9, 17, 9, 15)
            bin_outline.lineTo(10, 6)
            painter.drawPath(bin_outline)
            painter.drawLine(5, 8, 5, 14)
            painter.drawLine(7, 8, 7, 14)
        else:
            painter.translate(11, 6)
            file_outline = QPainterPath()
            file_outline.moveTo(2, 1)
            file_outline.lineTo(9, 1)
            file_outline.lineTo(13, 5)
            file_outline.lineTo(13, 17)
            file_outline.lineTo(2, 17)
            file_outline.closeSubpath()
            painter.drawPath(file_outline)
            painter.drawLine(9, 1, 9, 5)
            painter.drawLine(9, 5, 13, 5)
            painter.drawLine(4, 10, 11, 10)
            painter.drawLine(4, 13, 9, 13)
            painter.resetTransform()
            painter.setPen(QColor(foreground))
            font = painter.font()
            font.setPointSize(9)
            painter.setFont(font)
            painter.drawText(QRect(32, 0, self.width() - 36, self.height()),
                             Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                             "打开文件")
        painter.end()


class PetWindow(QWidget):
    """桌面宠物主窗口，负责透明角色渲染、交互物理循环、菜单与状态联动。"""

    open_profile_requested = Signal()
    open_character_editor_requested = Signal()
    open_bubble_editor_requested = Signal()
    open_settings_requested = Signal()
    open_chat_settings_requested = Signal()
    open_quota_requested = Signal()
    open_update_requested = Signal()
    settings_synced = Signal()
    visual_bounds_changed = Signal()
    exit_requested = Signal()
    _clip_decoded_signal = Signal()
    _ag_silent_connect_ready = Signal(object)

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
        # 新消息自动贴底开关：布局范围真正更新后再补滚，用户手动滚动时解除。
        self._chat_scroll_auto = False
        self._ag_sync_service = AntigravityChatSyncService()
        self._ag_sync_state = ChatSyncState()
        self._ag_collector = AntigravityChatCollector(self._ag_sync_service, self._ag_sync_state)
        self._ag_mirror_active = False
        self._ag_next_retry_at = 0.0
        self._ag_sync_session_id: Optional[str] = None
        self._ag_sync_events: list[SyncEvent] = []
        self._ag_sync_labels: list[QLabel] = []
        self._ag_viewing_sync = False
        self._ag_regular_draft = ""
        self._ag_pending_handoff = False
        # 消费端只认状态叶子差量：基线记各叶子的编号，队列按节奏逐条播。
        self._ag_baseline: dict[str, int] = {}
        self._ag_playback: deque[SyncEvent] = deque()
        self._ag_last_shown = 0.0
        self._ag_last_replace = 0.0
        self._ag_pending_replace: Optional[SyncEvent] = None
        self._ag_current_source_id = ""
        self._sync_thinking_visible = False
        self._sync_thinking_pinned = False
        self._sync_bubble_active = False
        self._sync_waiting_for_reply = False
        self._sync_busy_active = False
        self._sync_turn_pose: Optional[PetState] = None
        self._sync_last_busy_pose: Optional[PetState] = None
        self._sync_working_sessions: set[str] = set()
        # 心跳：按时间写日志，确认轮询是否真的在跑；SQLite 与文件扫描在后台线程执行，主线程仅消费内存队列。
        self._ag_dbg_ticks = 0
        self._ag_dbg_last = time.monotonic()
        self._ag_poll_stop = threading.Event()
        self._ag_poll_thread: Optional[threading.Thread] = None
        self._ag_reconnect_inflight = False
        self._ag_silent_connect_ready.connect(
            self._on_ag_silent_connect_ready, Qt.ConnectionType.QueuedConnection
        )
        self._ag_sync_timer = QTimer(self)
        self._ag_sync_timer.setInterval(_SYNC_POLL_MS)
        self._ag_sync_timer.timeout.connect(self._refresh_antigravity_sync)
        self._ag_sync_timer.start()
        self._sprites = sprites
        self._sound = sound
        self._sys_monitor = sys_monitor
        self._chat = chat

        # 桌宠与独立气泡处于同一置顶层，避免聊天窗口被其他应用遮挡。
        flags = (
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.Tool
            | Qt.WindowType.WindowStaysOnTopHint
        )
        self.setWindowFlags(flags)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, False)
        self.setMouseTracking(True)
        self.setWindowTitle("Gemini 娘")

        # Animation & particle state（优先用单调高精度时钟计 dt，同时兼容测试通过覆写 _last_tick_utc 注入步长）
        self._particle_system = ParticleSystem()
        self._anim_time = 0.0
        self._last_tick_utc = datetime.now(timezone.utc)
        self._last_tick_utc_recorded = self._last_tick_utc
        self._last_tick_perf = time.perf_counter()
        self._frame_target_period_s = 1.0 / 60.0
        self._frame_next_deadline_perf: Optional[float] = None
        self._last_bubble_reposition_mono = 0.0

        # Sprite & behavioral state
        self._current_state = PetState.IDLE
        self._override_pose: Optional[PetState] = None
        # 区分自动闲置动作与交互临时动作：自动动作持续到下次决策直接衔接，不提前回落默认待机
        self._override_is_autonomous = False
        # 区分反重力同步联动动作与交互动作，避免同步动作污染连戳/摇晃或误回收正常动作
        self._override_is_sync = False
        # 仅烦恼/鼓嘴/炸毛/眩晕/生气(5秒)播放期锁定，避免随机闲聊命中 PUFFED 误锁交互
        self._override_locked = False
        # 炸毛（REDHOT）或眩晕（DIZZY）结束后衔接闹脾气（SULK）的待续标志
        self._pending_sulk_after_pose = False
        self._override_until_utc = datetime.min.replace(tzinfo=timezone.utc)
        self._active_clip: Optional[ActionClip] = None
        self._clip_elapsed_sec = 0.0
        self._active_static_pixmap: Optional[QPixmap] = None
        self._use_chibi_walk_rig = False
        self._use_std_walk_rig = False
        self._crossfader = StageCrossfader()
        self._last_render_slot: Optional[VisualRenderSlot] = None
        self._motion_engine = PetMotionEngine()
        self._transform_damper = self._motion_engine.damper
        self._final_transform = TransformSnapshot()
        self._is_walking = False
        self._walk_dir = 1  # 1 = right, -1 = left
        self._is_sleeping = False
        # 已生效的打盹模式状态，用于检测开关变化并播放入睡/睡醒过渡
        self._doze_applied = bool(settings.doze_mode)
        self._is_sulk = False

        # Body poke -> bristle state
        self._poke_count = 0
        self._poke_stage = 0
        self._poke_last_mono = 0.0
        self._poke_stage_until_mono = 0.0
        self._bristle_last_particle_mono = 0.0
        self._bristle_last_sound_mono = 0.0
        self._press_is_double_click = False
        self._press_started_while_locked = False

        # Drag, shake (whip + circular spin) & gravity/throw physics
        self._mouse_down = False
        self._mouse_pressed_physically = False
        self._is_dragging = False
        self._is_shaking = False
        self._shake_stage = 0
        self._shake_peak_energy = 0.0
        self._drag_start_global = QPointF()
        self._win_start_left = 0.0
        self._win_start_top = 0.0
        self._last_move_global = QPointF()
        self._drag_trail = DragTrailEstimator()
        self._whip_energy = 0.0
        self._spin_energy = 0.0
        self._shake_last_reversal_mono = 0.0
        self._shake_last_active_mono = 0.0
        self._dizzy_last_particle_mono = 0.0
        self._last_move_mono = 0.0
        # 轴向甩动状态：[已确立方向(-1/0/1), 当前方向累计位移, 当前方向起点时间戳, 最近活跃时间戳]
        self._shake_axis_x: list[float | int] = [0, 0.0, 0.0, 0.0]
        self._shake_axis_y: list[float | int] = [0, 0.0, 0.0, 0.0]
        # 画圈转角状态：按最小位移步段累计连续同向偏转弧度，兼容高回报率鼠标
        self._spin_seg_dx = 0.0
        self._spin_seg_dy = 0.0
        self._spin_seg_start_mono = 0.0
        self._spin_prev_vec: Optional[tuple[float, float]] = None
        self._spin_last_vec_mono = 0.0
        self._spin_curve_dir = 0
        self._spin_curve_rad = 0.0
        self._is_falling = False
        self._throw_vx = 0.0
        self._fall_velocity_y = 0.0
        self._throw_pos_x: Optional[float] = None
        self._throw_pos_y: Optional[float] = None
        self._walk_pos_x: Optional[float] = None

        # Headpat tracking
        self._last_pat_local = QPointF()
        self._pat_travel = 0.0
        self._pat_hover_blocked_until_mono = 0.0
        self._last_hover_pat_mono = 0.0
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
        self._speech_bubble_enabled_applied = bool(settings.show_speech_bubble)
        self._idle_speech_bubble_active = False
        self._last_pet_activity_monotonic = time.monotonic()
        self._last_idle_speech_monotonic = self._last_pet_activity_monotonic
        idle_prof = self._idle_speech_profile()
        self._idle_speech_next_delay_sec = random.uniform(
            idle_prof.pet_min_sec, idle_prof.pet_max_sec
        )
        self._last_idle_speech_line: Optional[str] = None
        self._last_idle_speech_state: Optional[PetState] = None
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
        self._clip_decoded_signal.connect(
            self._on_animated_clip_decoded,
            Qt.ConnectionType.QueuedConnection,
        )
        self._sprites.add_clip_ready_listener(self._clip_decoded_signal.emit)

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
        self._bubble_settings_btn.clicked.connect(self.open_chat_settings_requested.emit)
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
        session_row.addWidget(self._new_chat_btn)
        session_row.addStretch(1)
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
            QListWidget::item { border-bottom: 1px solid #302746; }
            QListWidget::item:selected { background: #443371; }
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
        # 用户拖动滑块或滚轮阅读历史时解除自动贴底，新消息到来时再重新贴底。
        self._bubble_scroll.verticalScrollBar().actionTriggered.connect(self._on_chat_scroll_user_action)

        self._bubble_content = QWidget()
        self._bubble_content.setStyleSheet("background: transparent;")
        self._bubble_content_layout = QVBoxLayout(self._bubble_content)
        self._bubble_content_layout.setContentsMargins(0, 0, 0, 0)
        self._bubble_content_layout.setSpacing(6)

        self._bubble_text_lbl = QLabel("喵～Gemini 上线！")
        self._bubble_text_lbl.setTextFormat(Qt.TextFormat.PlainText)
        self._bubble_text_lbl.setWordWrap(True)
        self._bubble_text_lbl.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
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
        path_row = QHBoxLayout()
        path_row.setContentsMargins(0, 0, 0, 0)
        path_row.setSpacing(4)
        path_row.addWidget(self._chat_path, 1)
        self._chat_open_file_btn = _ChatActionButton("open", self._chat_path_panel)
        self._chat_open_file_btn.setToolTip("用系统默认程序打开当前会话文件")
        self._chat_open_file_btn.clicked.connect(self._open_chat_session_file)
        path_row.addWidget(self._chat_open_file_btn)
        path_layout.addLayout(path_row)
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
            "📌 固定位置",
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
            "💭 弹出默认气泡",
            checkable=True,
            slot=self._on_menu_show_speech_bubble,
            tip="关闭后仅在聊天或重要提示时显示气泡",
        )
        self._act_bubble_editor = self._new_menu_action(
            "📜 气泡台词与活跃度...",
            slot=self.open_bubble_editor_requested,
            tip="打开角色设定工坊并直接定位到气泡活跃度与随机台词定义",
        )
        self._act_sync_chat = self._new_menu_action(
            "💬 实时同步 Antigravity 对话",
            checkable=True,
            slot=self._on_menu_sync_chat,
            tip="把 Antigravity 的新消息实时播报到气泡",
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
        self._sync_context_menu_checks()

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
            self._act_bubble_editor,
            self._act_sync_chat,
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
        self._ai_timer.setInterval(self._effective_ai_interval_ms())
        self._ai_timer.timeout.connect(self._ai_timer_tick)

        self._bubble_timer = QTimer(self)
        self._bubble_timer.setSingleShot(True)
        self._bubble_timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._bubble_timer.timeout.connect(self._on_bubble_timeout)

        self._monitor_timer = QTimer(self)
        self._monitor_timer.setInterval(30000)
        self._monitor_timer.timeout.connect(self._monitor_timer_tick)

        self._hover_grace_timer = QTimer(self)
        self._hover_grace_timer.setSingleShot(True)
        self._hover_grace_timer.setInterval(380)
        self._hover_grace_timer.timeout.connect(self._on_hover_grace_timeout)

        self._idle_speech_timer = QTimer(self)
        self._idle_speech_timer.setInterval(_IDLE_SPEECH_CHECK_MS)
        self._idle_speech_timer.timeout.connect(self._idle_speech_timer_tick)

        self._quota_double_click_timer = QTimer(self)
        self._quota_double_click_timer.setSingleShot(True)
        self._quota_double_click_timer.setInterval(_BODY_DOUBLE_CLICK_QUOTA_DELAY_MS)
        self._quota_double_click_timer.timeout.connect(self._fire_pending_body_double_click_quota)

        # 位置/好感等高频交互变更走 500ms 防抖写盘，避免拖拽松手抛掷第一帧同步磁盘 I/O 卡顿
        self._settings_save_timer = QTimer(self)
        self._settings_save_timer.setSingleShot(True)
        self._settings_save_timer.setInterval(500)
        self._settings_save_timer.timeout.connect(self._flush_pending_settings_save)

    def _schedule_settings_save(self) -> None:
        if hasattr(self, "_settings_save_timer"):
            self._settings_save_timer.start()

    def _flush_pending_settings_save(self) -> None:
        try:
            self._settings_service.save(self._settings)
        except OSError as exc:
            AppLogger.error("PetWindow", "异步防抖保存桌宠配置失败", exc)

    def _sync_frame_target_period(self) -> None:
        """按当前屏幕刷新率计算对齐 VSync 的目标帧周期（60~120Hz 1:1，高刷屏取整数分频防拍频抖动）。"""
        screen = self.screen() or QGuiApplication.primaryScreen()
        hz = float(screen.refreshRate()) if screen is not None else 60.0
        if hz < 30.0 or hz > 480.0:
            hz = 60.0
        if hz > 120.5:
            divisor = max(2, int(math.ceil(hz / 120.0)))
            hz = hz / float(divisor)
        self._frame_target_period_s = 1.0 / max(48.0, min(120.0, hz))

    def _pace_next_frame_tick(self, tick_start_perf: float) -> None:
        """按目标刷新周期用误差累加器微调下一次定时器间隔，消除固定 16ms 在 60Hz 屏上的拍频掉帧。"""
        if not self._frame_timer.isActive():
            self._frame_next_deadline_perf = None
            return
        period = self._frame_target_period_s
        now = time.perf_counter()
        if self._frame_next_deadline_perf is None:
            self._frame_next_deadline_perf = tick_start_perf + period
        else:
            self._frame_next_deadline_perf += period
            if self._frame_next_deadline_perf < now - period:
                self._frame_next_deadline_perf = now + period
        delay_ms = max(1, min(33, int(round((self._frame_next_deadline_perf - now) * 1000.0))))
        self._frame_timer.setInterval(delay_ms)

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

            self._sync_frame_target_period()
            self._last_tick_utc = datetime.now(timezone.utc)
            self._last_tick_utc_recorded = self._last_tick_utc
            self._last_tick_perf = time.perf_counter()
            self._frame_next_deadline_perf = None
            self._mark_pet_user_activity()
            self._frame_timer.setInterval(max(1, int(round(self._frame_target_period_s * 1000.0))))
            self._frame_timer.start()
            self._ai_timer.start()
            self._monitor_timer.start()
            self._idle_speech_timer.start()
            if self._settings.chat_sync_enabled:
                # 等窗口真正可见再启动播报，避免气泡被吞。
                QTimer.singleShot(0, self._auto_resume_antigravity_sync)
        else:
            self._sync_frame_target_period()
            self._apply_win32_extended_styles()

    def closeEvent(self, event) -> None:
        if hasattr(self, "_settings_save_timer"):
            self._settings_save_timer.stop()
        if not self._closing_handled:
            self._closing_handled = True
            if self._first_shown:
                self._settings.last_left = float(self.x())
                self._settings.last_top = float(self.y())
            try:
                self._settings_service.save(self._settings)
            except OSError as exc:
                AppLogger.error("PetWindow", "退出时保存桌宠位置失败", exc)

        self._frame_timer.stop()
        self._ai_timer.stop()
        self._bubble_timer.stop()
        self._monitor_timer.stop()
        self._hover_grace_timer.stop()
        self._idle_speech_timer.stop()
        self._quota_double_click_timer.stop()
        self._ag_sync_timer.stop()
        self._stop_ag_poll_thread()
        self._sprites.remove_clip_ready_listener(self._clip_decoded_signal.emit)
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
        if self._stage_canvas.mouseGrabber() is self._stage_canvas:
            self._stage_canvas.releaseMouse()
        self._mouse_down = False
        self._mouse_pressed_physically = False
        self._is_dragging = False
        self._drag_trail.clear()
        self._is_falling = False
        self._throw_vx = 0.0
        self._fall_velocity_y = 0.0
        self._throw_pos_x = None
        self._throw_pos_y = None
        self._walk_pos_x = None
        self._motion_engine.reset_transient_effects(clear_drag_tilt=True)
        self._motion_engine.bristle_burst.reset()
        self._particle_system.clear()
        self._is_shaking = False
        self._shake_stage = 0
        self._shake_peak_energy = 0.0
        self._press_is_double_click = False
        self._press_started_while_locked = False
        if hasattr(self, "_quota_double_click_timer"):
            self._quota_double_click_timer.stop()
        if getattr(self, "_ui_ready", False):
            self._is_chat_open = False
            self._is_forced_bubble_active = False
            self._ag_pending_replace = None
            self._clear_sync_pose_state(release_pose=True)
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
            self._sync_bubble_active
            and not self._is_chat_open
            and etype in (
                QEvent.Type.MouseButtonPress, QEvent.Type.MouseButtonRelease,
                QEvent.Type.MouseButtonDblClick, QEvent.Type.MouseMove,
                QEvent.Type.Wheel, QEvent.Type.KeyPress,
            )
            and isinstance(watched, QWidget)
            and (watched is self._speech_bubble or self._speech_bubble.isAncestorOf(watched))
        ):
            self._mark_pet_user_activity()
            if etype == QEvent.Type.MouseButtonPress and self._sync_thinking_visible:
                # 点击阅读后继续接收思维续写，保留正在阅读的内容直至倒计时结束。
                self._sync_thinking_pinned = True
            self._restart_sync_bubble_timeout()
        if watched is self._speech_bubble:
            if etype == QEvent.Type.Enter:
                self._is_bubble_hovered = True
                self._on_hover_entered()
            elif etype == QEvent.Type.Leave:
                self._is_bubble_hovered = False
                if (
                    not self._is_chat_open
                    and not self._sync_bubble_active
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
        self._mark_pet_user_activity()
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
                self._restart_ai_timer()

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
            self._restart_ai_timer()

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
        bubble_enabled_now = bool(self._settings.show_speech_bubble)
        if bubble_enabled_now != self._speech_bubble_enabled_applied:
            self._speech_bubble_enabled_applied = bubble_enabled_now
            if bubble_enabled_now:
                self._mark_pet_user_activity()
        # 停止播报后的设置刷新也要保留正在倒计时的同步气泡。
        if (
            not self._settings.show_speech_bubble
            and not self._is_chat_open
            and not self._is_forced_bubble_active
            and not self._ag_mirror_active
            and not self._sync_bubble_active
        ):
            self._hide_speech_bubble()

        self._update_dock_alignment()
        self._apply_win32_extended_styles()
        self._sprites.warm_render_cache(
            self._settings.style_mode,
            sprite_h,
            max(1.0, float(self.devicePixelRatioF())),
        )
        self._refresh_sprite_visual()
        self._sync_chat_controls()
        self._relayout_stage_top()
        self._stage_canvas.update()

    def _apply_win32_extended_styles(self) -> None:
        hwnd = int(self.winId())
        if not hwnd:
            return
        _ensure_tool_window_ex_style(hwnd)
        self._sync_bubble_window_styles()
        self.visual_bounds_changed.emit()

    def _sync_bubble_window_styles(self) -> None:
        if not self._speech_bubble.isVisible():
            return
        bubble_hwnd = int(self._speech_bubble.winId())
        if not bubble_hwnd:
            return
        _ensure_tool_window_ex_style(bubble_hwnd)

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

        fm = QFontMetrics(self._bubble_text_lbl.font())
        text = self._bubble_text_lbl.text() or ""
        longest_line = max((fm.horizontalAdvance(ln) for ln in text.splitlines()), default=0)
        target_w = max(300, min(400, longest_line + 52))
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
        area = self._get_work_area(pos.x() + size.width() * 0.5, pos.y() + size.height() * 0.5)
        cx = max(area.left() + 6, min(pos.x(), area.right() + 1 - size.width() - 6))
        cy = max(area.top() + 6, min(pos.y(), area.bottom() + 1 - size.height() - 6))
        return QPoint(cx, cy)

    def _on_bubble_moved_by_user(self, pos: QPoint) -> None:
        self._bubble_custom_offset = pos - self.pos()
        self.visual_bounds_changed.emit()

    def _reposition_speech_bubble_on_move(self, *, force: bool = False) -> None:
        if not self._speech_bubble.isVisible() or self._speech_bubble.is_resizing():
            return
        # 抛掷高速飞行/弹跳期间适度节流独立气泡窗跟位，避免每帧同步移动两个分层透明窗拖慢主循环
        if self._is_falling and not force:
            now_mono = time.monotonic()
            if (now_mono - self._last_bubble_reposition_mono) < 0.032:
                return
            self._last_bubble_reposition_mono = now_mono
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
        elif self._sync_bubble_active:
            self._restart_sync_bubble_timeout()
        elif not self._is_bubble_hovered and self._speech_bubble.isVisible():
            self._bubble_timer.stop()
            self._bubble_timer.start(2500)

    def _relayout_stage_top(self, *, reveal_bubble: bool = False) -> None:
        if (reveal_bubble or self._speech_bubble.isVisible()) and not self._speech_bubble.is_resizing():
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
            if reveal_bubble:
                self._speech_bubble.show()
            self._speech_bubble.raise_()
            # 重新排版气泡后，保持已打开的聊天菜单处于气泡前方。
            menu = getattr(self, "_chat_model_menu", None)
            if menu is not None and menu.isVisible():
                menu.raise_()

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
        self._throw_vx = 0.0
        self._fall_velocity_y = 0.0
        self._throw_pos_x = None
        self._throw_pos_y = None
        self._bubble_custom_offset = None
        if self._override_is_autonomous:
            self._override_pose = None
            self._override_is_autonomous = False
        self._current_state = self._resolve_resting_state()
        area = self._get_work_area(self.x() + self.width() * 0.5, self.y() + self.height() * 0.5)
        new_x = area.right() + 1 - self.width() - 28
        new_y = area.bottom() + 1 - self.height()
        self.move(int(round(new_x)), int(round(new_y)))
        self._update_dock_alignment()
        self._refresh_sprite_visual()
        self._restart_ai_timer()
        self._settings.last_left = float(self.x())
        self._settings.last_top = float(self.y())
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
        area, _, _, _, min_y, max_y = self._visible_sprite_work_bounds()
        new_x = self.x()
        if self._settings.docked_side == DockSide.LEFT:
            new_x = area.left()
        elif self._settings.docked_side == DockSide.RIGHT:
            new_x = area.right() + 1 - self.width()
        new_y = max(min_y, min(max_y, float(self.y())))
        self.move(int(round(new_x)), int(round(new_y)))

    def _get_work_area(self, center_x: float, center_y: float) -> QRect:
        pt = QPoint(int(round(center_x)), int(round(center_y)))
        screen = QGuiApplication.screenAt(pt) or self.screen() or QGuiApplication.primaryScreen()
        return screen.availableGeometry() if screen else QRect(0, 0, 1920, 1080)

    def _visible_sprite_work_bounds(self) -> tuple[QRect, QRectF, float, float, float, float]:
        """按角色实际可见贴图四边计算工作区与窗口可移动边界 (area, sprite_rect, min_x, max_x, min_y, max_y)。

        PetWindow 顶部与左右两侧包含用于悬浮、倾斜和粒子的透明舞台留白；
        抛掷碰撞反弹与越界夹紧须对齐可见角色四边，避免角色尚未碰到屏幕边缘就在透明留白处提前反弹。
        """
        sprite_rect = self._get_local_sprite_rect()
        if sprite_rect.width() <= 10.0 or sprite_rect.height() <= 10.0:
            fallback_w = 180.0 * self._effective_scale()
            fallback_h = self._sprite_height_px()
            sprite_rect = QRectF(
                (self.width() - fallback_w) * 0.5,
                float(self.height()) - fallback_h,
                fallback_w,
                fallback_h,
            )
        area = self._get_work_area(
            self.x() + sprite_rect.center().x(),
            self.y() + sprite_rect.center().y(),
        )
        min_x = float(area.left()) - sprite_rect.left()
        max_x = float(area.right() + 1) - sprite_rect.right()
        if max_x < min_x:
            mid_x = float(area.left()) + (float(area.width()) - float(self.width())) * 0.5
            min_x = max_x = mid_x
        min_y = float(area.top()) - sprite_rect.top()
        max_y = float(area.bottom() + 1) - sprite_rect.bottom()
        if max_y < min_y:
            min_y = max_y
        return area, sprite_rect, min_x, max_x, min_y, max_y

    def _clamp_inside_nearest_work_area(self) -> None:
        _, _, min_x, max_x, min_y, max_y = self._visible_sprite_work_bounds()
        cx = max(min_x, min(max_x, float(self.x())))
        cy = max(min_y, min(max_y, float(self.y())))
        self.move(int(round(cx)), int(round(cy)))

    def _on_animated_clip_decoded(self) -> None:
        """后台动态 WebP 解码完成后切回 GUI 线程立即触发重绘。"""
        if getattr(self, "_ui_ready", False) and self.isVisible():
            self._stage_canvas.update()

    def _resolve_active_frame(self) -> Optional[QPixmap]:
        """按当前播放进度获取活动帧：透明动态 WebP 取当前时刻帧，静态图直接返回缓存。"""
        clip = self._active_clip
        if clip is not None and clip.is_animated:
            dpr = max(1.0, float(self.devicePixelRatioF()))
            target_h = int(round(self._sprite_height_px() * dpr))
            frame = clip.frame_at(self._clip_elapsed_sec, target_height=target_h)
            if frame is not None and not frame.isNull():
                return frame
        if self._active_static_pixmap is not None and not self._active_static_pixmap.isNull():
            return self._active_static_pixmap
        return clip.preview_pixmap() if clip is not None else None

    def _get_local_sprite_rect(self) -> QRectF:
        """Returns active sprite or walk rig rectangle in local PetWindow coordinates."""
        frame = self._resolve_active_frame()
        return compute_local_sprite_rect(
            stage_w=float(self.width()),
            stage_h=float(self.height()),
            sprite_h=self._sprite_height_px(),
            docked_side=self._settings.docked_side,
            style_mode=self._settings.style_mode,
            use_std_walk_rig=self._use_std_walk_rig,
            use_chibi_walk_rig=self._use_chibi_walk_rig,
            std_rig_size=self._std_rig_size(),
            chibi_rig_size=self._chibi_rig_size(),
            active_clip=self._active_clip,
            active_pixmap=frame,
            dock_progress=self._transform_damper.dock_progress,
        )

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

    # ==================== Public Greeting / Preview ====================

    def show_and_greet(self, custom_message: Optional[str] = None) -> None:
        self._mark_pet_user_activity()
        if not self._first_shown:
            self._suppress_loaded_default_greeting = True
            self._restore_or_init_position()

        if not self.isVisible():
            self.show()

        if self._is_sync_working_locked():
            return

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

    def preview_state(self, state: PetState, style: Optional[PetStyleMode] = None) -> None:
        """从动作图鉴演示指定动作，若同时切换形象则先锁定目标动作，避免闪回默认立绘。"""
        if self._is_sync_working_locked():
            if style is not None and self._settings.style_mode != style:
                self._settings.style_mode = style
                self._settings_service.save(self._settings)
                self.apply_settings_to_window()
                self.settings_synced.emit()
                if self._settings.docked_side != DockSide.NONE:
                    self._reanchor_docked_position()
            return
        self._mark_pet_user_activity()
        self._is_walking = False
        self._is_sleeping = state in (
            PetState.SLEEPING,
            PetState.EDGE_LEFT_SLEEP,
            PetState.EDGE_RIGHT_SLEEP,
        )
        self._override_pose = state
        self._override_is_autonomous = False
        self._override_is_sync = False
        self._override_locked = False
        self._pending_sulk_after_pose = False
        self._override_until_utc = datetime.now(timezone.utc) + timedelta(seconds=4.5)
        if state == PetState.REDHOT:
            self._motion_engine.bristle_burst.trigger(1.0)
        elif state == PetState.PUFFED:
            self._motion_engine.bristle_burst.trigger(0.55)

        if style is not None and self._settings.style_mode != style:
            self._settings.style_mode = style
            self._settings_service.save(self._settings)
            self.apply_settings_to_window()
            self.settings_synced.emit()
            if self._settings.docked_side != DockSide.NONE:
                self._reanchor_docked_position()

        if not self.isVisible():
            if not self._first_shown:
                self._suppress_loaded_default_greeting = True
            self.show()

        self._refresh_sprite_visual()
        self._update_sprite_transforms()
        self._stage_canvas.update()
        self.show_bubble(CharacterLore.pick_line_for_state(state), 4.5)
        self._sound.play(PetSoundEffect.CHIME)
        self._spawn_star_burst(self.width() * 0.5, self.height() - self._sprite_height_px() * 0.5, 5)

    # ==================== 60fps Loop & Kinematics ====================

    def _frame_timer_tick(self) -> None:
        now = datetime.now(timezone.utc)
        now_perf = time.perf_counter()
        if self._last_tick_utc != self._last_tick_utc_recorded:
            dt = max(0.001, min(0.05, (now - self._last_tick_utc).total_seconds()))
        else:
            dt = max(0.001, min(0.05, now_perf - self._last_tick_perf))
        self._last_tick_utc = now
        self._last_tick_utc_recorded = now
        self._last_tick_perf = now_perf
        self._anim_time += dt
        self._clip_elapsed_sec += dt
        self._crossfader.advance(dt)
        self._transform_damper.step_dock_slide(
            dt, active=self._settings.docked_side != DockSide.NONE
        )
        walk_blend = self._transform_damper.step_walk_blend(
            dt,
            walking=self._is_walking and not self._mouse_down and not self._is_dragging,
        )
        if self._whip_energy > 0.0:
            self._whip_energy = max(0.0, self._whip_energy - dt * _SHAKE_DECAY_PER_SEC)
        if self._spin_energy > 0.0:
            self._spin_energy = max(0.0, self._spin_energy - dt * _SHAKE_DECAY_PER_SEC)
        self._motion_engine.decay_drag_tilt(dt)

        # 鼠标抓握看门狗：防止气泡弹出或切动作导致 Qt 丢失 grabMouse 时拖拽卡死或句柄脱离
        if self._mouse_down and self._mouse_pressed_physically:
            left_held = self._is_primary_mouse_button_held()
            if not left_held:
                self._recover_lost_mouse_release()
            elif self._is_dragging:
                self._sync_drag_to_global_pos(QPointF(QCursor.pos()))
            elif self._stage_canvas.mouseGrabber() is not self._stage_canvas:
                cur_g = QPointF(QCursor.pos())
                if math.hypot(
                    cur_g.x() - self._drag_start_global.x(),
                    cur_g.y() - self._drag_start_global.y(),
                ) > 5.0:
                    if self.isVisible():
                        self._stage_canvas.grabMouse()
                    self._start_drag_at_global_pos(cur_g)

        if self._waiting_chat_workers and not self._is_sync_working_locked():
            # 模型正在回复期间锁定展示“思考中”形象，防止长耗时思考请求被超时定时器打断
            if self._override_pose != PetState.THINKING:
                self._is_walking = False
                self._is_sleeping = False
                self._override_pose = PetState.THINKING
                self._override_is_autonomous = False
                self._override_is_sync = False
                self._override_locked = False
                self._pending_sulk_after_pose = False
                self._refresh_sprite_visual()
        elif (
            self._is_dragging
            and self._is_shaking
            and self._override_pose == PetState.REDHOT
            and self._shake_last_active_mono > 0.0
            and (time.monotonic() - self._shake_last_active_mono) >= _SHAKE_STOP_RECOVER_S
        ):
            # 完全炸毛后中途停止晃动满 2 秒：消退摇晃状态并回到正常反重力拖拽流程（松手正常下落 Q 弹）
            self._reset_drag_shake_tracking()
            self._override_pose = PetState.FLOAT
            self._override_is_autonomous = False
            self._override_is_sync = False
            self._override_locked = False
            if not self._is_sulk:
                self._pending_sulk_after_pose = False
            self._override_until_utc = datetime.max.replace(tzinfo=timezone.utc)
            if (
                self._speech_bubble.isVisible()
                and not self._is_chat_open
                and not self._sync_bubble_active
            ):
                self._hide_speech_bubble()
            self._refresh_sprite_visual()
        elif (
            self._override_pose is not None
            and not self._override_is_autonomous
            and now >= self._override_until_utc
        ):
            finished_pose = self._override_pose
            was_sync_pose = self._override_is_sync
            if self._is_dragging:
                now_mono = time.monotonic()
                # 当前阶段动作刚播完且仍在持续摇晃时，在同一帧顺阶推进 烦恼 -> 鼓嘴 -> 炸毛
                if self._advance_drag_shake_stage_if_ready(now_mono):
                    pass
                elif not self._is_drag_float_allowed(now_mono):
                    self._override_until_utc = now + timedelta(seconds=1.2)
                else:
                    self._reset_drag_shake_tracking()
                    if self._override_pose != PetState.FLOAT:
                        self._override_pose = PetState.FLOAT
                        self._override_is_autonomous = False
                        self._override_is_sync = False
                        self._override_locked = False
                        if finished_pose == PetState.SULK and self._is_sulk:
                            self._is_sulk = False
                        if not self._is_sulk:
                            self._pending_sulk_after_pose = False
                        self._override_until_utc = datetime.max.replace(tzinfo=timezone.utc)
                        self._refresh_sprite_visual()
            elif self._pending_sulk_after_pose and self._is_sulk and self.isVisible():
                # 炸毛或眩晕播完后在同一帧衔接闹脾气，避免定时竞态闪回默认待机。
                self._pending_sulk_after_pose = False
                self._override_locked = False
                self._poke_count = 0
                self._poke_stage = 0
                self._poke_stage_until_mono = 0.0
                self._enter_sulk_state()
            else:
                self._pending_sulk_after_pose = False
                self._override_pose = None
                self._override_is_autonomous = False
                self._override_is_sync = False
                self._override_locked = False
                if finished_pose == PetState.ANNOYED and not was_sync_pose and self._poke_count > 0:
                    # 连戳触发的烦恼播完后保留短暂怒气记忆，再次戳身体时顺阶进入鼓嘴
                    self._poke_stage = 1
                    self._poke_stage_until_mono = time.monotonic() + _POKE_STAGE_MEMORY_S
                elif finished_pose == PetState.PUFFED and not was_sync_pose and self._poke_count > 0:
                    # 连戳触发的鼓嘴播完后保留短暂怒气记忆，再次戳身体时顺阶爆发炸毛
                    self._poke_stage = 2
                    self._poke_stage_until_mono = time.monotonic() + _POKE_STAGE_MEMORY_S
                elif not was_sync_pose:
                    self._poke_count = 0
                    self._poke_stage = 0
                    self._poke_stage_until_mono = 0.0
                if self._is_sulk:
                    self._is_sulk = False
                self._refresh_sprite_visual()
                self._restart_ai_timer()

        if (
            not self._is_dragging
            and not self._waiting_chat_workers
            and self._is_sulk
            and self._override_pose is None
            and not self._pending_sulk_after_pose
        ):
            # 兜底清除无对应闹脾气姿态的残留标记，避免状态卡死
            self._is_sulk = False

        # 反重力实时同步进行中：在当前轮次未完成前保持本轮随机选定的忙碌动作固定，不中途切回待机或换动作。
        if (
            self._sync_busy_active
            and self._sync_turn_pose is not None
            and self._can_apply_sync_pose()
            and (self._override_pose is None or self._override_is_autonomous)
        ):
            self._apply_sync_pose(self._sync_turn_pose, None)

        pending_move: Optional[tuple[int, int]] = None
        was_falling_this_tick = False

        # Gravity fall & throw bounce physics
        if (
            self._is_falling
            and not self._is_dragging
            and not self._settings.anti_gravity_mode
            and self._settings.docked_side == DockSide.NONE
        ):
            was_falling_this_tick = True
            self._walk_pos_x = None
            _, _, min_x, max_x, min_y, floor_top = self._visible_sprite_work_bounds()
            cur_x = float(self.x())
            cur_y = float(self.y())
            if (
                self._throw_pos_x is not None
                and self._throw_pos_y is not None
                and abs(self._throw_pos_x - cur_x) <= 1.5
                and abs(self._throw_pos_y - cur_y) <= 1.5
            ):
                cur_x = self._throw_pos_x
                cur_y = self._throw_pos_y

            res = step_throw_physics(
                ThrowBodyState(
                    x=cur_x,
                    y=cur_y,
                    vx=self._throw_vx,
                    vy=self._fall_velocity_y,
                ),
                dt,
                min_x=min_x,
                max_x=max_x,
                min_y=min_y,
                floor_y=floor_top,
            )
            self._throw_vx = res.vx
            self._fall_velocity_y = res.vy
            self._throw_pos_x = res.x
            self._throw_pos_y = res.y

            target_ix = int(round(res.x))
            target_iy = int(round(res.y))
            if target_ix != self.x() or target_iy != self.y():
                pending_move = (target_ix, target_iy)
            # 将取整余数补偿进画布变换，消除轻弹顶点与低速滑行末段的整数像素步进感
            if res.at_rest:
                self._motion_engine.clear_subpixel_offset()
            else:
                self._motion_engine.set_subpixel_offset(res.x - target_ix, res.y - target_iy)

            if res.bounced_wall:
                if res.bounced_wall_side in (-1, 1):
                    # 撞击侧壁时叠加轻幅横向受压/纵向舒展弹簧冲量，增强撞墙回弹干脆感
                    self._motion_engine.squash_spring.trigger_stretch(0.055)
                elif res.bounced_wall_side == 2:
                    self._motion_engine.squash_spring.trigger_squash(0.065)

            if res.landed_impact_vy > 0.0:
                depth = landing_squash_depth(res.landed_impact_vy)
                if res.bounced_floor:
                    # 高冲击首次触地并立刻起跳时仅给短促轻弹舒展，避免窗口上升却在半空持续下压打架
                    self._motion_engine.squash_spring.trigger_stretch(min(0.075, depth * 0.42))
                    self._sound.play(PetSoundEffect.BOING)
                else:
                    # 真正落稳地面时向二阶弹簧注入连续下压冲量，即使二次触地也不会硬重置闪跳
                    self._motion_engine.squash_spring.trigger_squash(depth)
                    if res.landed_impact_vy >= 160.0:
                        self._sound.play(PetSoundEffect.BOING)
                if self._override_pose == PetState.DIZZY:
                    self._spawn_dizzy_particles(4)

            if res.at_rest:
                self._is_falling = False
                self._throw_vx = 0.0
                self._fall_velocity_y = 0.0
                self._throw_pos_x = None
                self._throw_pos_y = None
                self._motion_engine.clear_subpixel_offset()
                self._settings.last_left = float(target_ix)
                self._settings.last_top = float(target_iy)
                self._schedule_settings_save()
                self._current_state = self._resolve_resting_state()
                self._refresh_sprite_visual()
                self._restart_ai_timer()

        # Autonomous horizontal walk along taskbar (with smooth ease-in/out & clip move-window sync)
        if (
            self._is_walking
            and not self._mouse_down
            and not self._is_dragging
            and not self._is_falling
            and self._settings.docked_side == DockSide.NONE
        ):
            _, _, min_x, max_x, _, _ = self._visible_sprite_work_bounds()
            min_left = min_x + 8.0
            max_left = max_x - 8.0
            if max_left < min_left:
                min_left = max_left = (min_x + max_x) * 0.5

            move_scale = walk_blend
            if (
                self._active_clip is not None
                and self._active_clip.is_animated
                and not self._use_chibi_walk_rig
                and not self._use_std_walk_rig
            ):
                # 透明动态步行素材按 move_start_ratio / move_stop_ratio 前后摇窗口控制位移，防止滑步
                meta = self._active_clip.metadata
                dur = self._active_clip.duration_sec
                if dur > 0.0 and (meta.move_start_ratio > 0.0 or meta.move_stop_ratio < 1.0):
                    phase = (self._clip_elapsed_sec % dur) / dur
                    if phase < meta.move_start_ratio or phase > meta.move_stop_ratio:
                        move_scale = 0.0

            cur_walk_x = float(self.x())
            if self._walk_pos_x is not None and abs(self._walk_pos_x - cur_walk_x) <= 1.5:
                cur_walk_x = self._walk_pos_x
            speed = 60.0 * self._effective_scale() * move_scale
            next_left = cur_walk_x + self._walk_dir * speed * dt
            if next_left <= min_left:
                next_left = min_left
                self._walk_dir = 1
            elif next_left >= max_left:
                next_left = max_left
                self._walk_dir = -1
            self._walk_pos_x = next_left
            walk_ix = int(round(next_left))
            if walk_ix != self.x():
                pending_move = (walk_ix, self.y())
            self._motion_engine.set_subpixel_offset(next_left - walk_ix, 0.0)
        elif not was_falling_this_tick:
            self._walk_pos_x = None
            self._motion_engine.clear_subpixel_offset()

        # 先完成本帧变换与粒子物理推进，再紧邻执行窗口移动与画布刷新，避免位移与挤压/倾角跨帧撕裂
        self._update_sprite_transforms(dt)
        self._particle_system.step(dt)
        if pending_move is not None:
            self.move(pending_move[0], pending_move[1])
            if was_falling_this_tick and not self._is_falling:
                self._reposition_speech_bubble_on_move(force=True)
        if pending_move is not None and was_falling_this_tick and self.isVisible():
            # 抛掷弹跳位移帧同步重绘，让 SetWindowPos 与 UpdateLayeredWindow 背靠背提交，消除错拍抖动
            self._stage_canvas.repaint()
        else:
            self._stage_canvas.update()
        self._pace_next_frame_tick(now_perf)

    def _emit_ambient_particles(self) -> None:
        """按当前姿态周期/概率生成怒气、晕眩、思考、漂浮与打盹 Zzz 氛围粒子。"""
        if self._settings.docked_side == DockSide.NONE:
            if self._override_pose == PetState.REDHOT and not (
                self._is_dragging and self._is_sync_working_locked()
            ):
                now_mono = time.monotonic()
                if (
                    self._settings.particles_enabled
                    and (now_mono - self._bristle_last_particle_mono) >= 0.28
                ):
                    self._spawn_bristle_particles(1)
                    self._bristle_last_particle_mono = now_mono
            elif not self._is_dragging and not self._is_walking:
                if self._override_pose == PetState.DIZZY:
                    now_mono = time.monotonic()
                    if (
                        self._settings.particles_enabled
                        and (now_mono - self._dizzy_last_particle_mono) >= 0.42
                    ):
                        self._spawn_dizzy_particles(1)
                        self._dizzy_last_particle_mono = now_mono
                elif self._override_pose == PetState.THINKING:
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
                elif (
                    self._settings.anti_gravity_mode
                    or self._current_state == PetState.FLOAT
                    or self._override_pose == PetState.FLOAT
                ):
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

        # Zzz 属于打盹反馈，贴边时也要出现，独立于贴边分支之外
        dozing = self._is_sleeping or (self._is_dozing() and self._override_pose is None)
        if dozing and random.random() < 0.04:
            zzz_x, zzz_y = self._doze_zzz_anchor()
            self._spawn_particle("Zzz", "#93C5FD", zzz_x, zzz_y, 12.0, -22.0, 1.8)

    def _update_sprite_transforms(self, dt: float = 0.0, *, snap: bool = False) -> None:
        """组装纯姿态状态输入并委托 PetMotionEngine 计算最终 2D 变换与步行骨架关节。"""
        rig = self._active_walk_rig()
        is_animated_clip = (
            self._active_clip is not None
            and self._active_clip.is_animated
            and not self._use_chibi_walk_rig
            and not self._use_std_walk_rig
        )
        motion_input = PoseMotionInput(
            anim_time=self._anim_time,
            dt=dt,
            scale=self._effective_scale(),
            docked_side=self._settings.docked_side,
            current_state=self._current_state,
            override_pose=self._override_pose,
            is_dragging=self._is_dragging,
            is_sync_working_locked=self._is_sync_working_locked(),
            is_walking=self._is_walking,
            walk_dir=self._walk_dir,
            mouse_down=self._mouse_down,
            is_falling=self._is_falling,
            throw_vx=self._throw_vx,
            is_sleeping=self._is_sleeping,
            is_dozing=self._is_dozing(),
            anti_gravity_mode=bool(self._settings.anti_gravity_mode),
            is_animated_clip=is_animated_clip,
            clip_source_facing=(
                self._active_clip.metadata.source_facing if self._active_clip is not None else 1
            ),
            use_std_walk_rig=self._use_std_walk_rig,
            use_chibi_walk_rig=self._use_chibi_walk_rig,
            std_rig_complete=rig.standard_complete,
            chibi_rig_complete=rig.chibi_complete,
            snap=snap,
        )
        self._final_transform = self._motion_engine.evaluate(motion_input)
        self._emit_ambient_particles()

    def _head_click_ratio(self) -> float:
        return 0.26 if self._settings.style_mode == PetStyleMode.STANDARD else 0.45

    def _head_hover_ratio(self) -> float:
        return 0.29 if self._settings.style_mode == PetStyleMode.STANDARD else 0.48

    def _active_anchor_x(self, frame: Optional[QPixmap] = None) -> float:
        if self._use_chibi_walk_rig or self._use_std_walk_rig:
            return 0.5
        if self._active_clip is not None:
            return self._active_clip.anchor_x_ratio(frame)
        return analyze_pixmap_metrics(frame).anchor_x_norm if frame is not None else 0.5

    def _head_crown_anchor(self, sprite_rect: QRectF) -> tuple[float, float, float]:
        """返回角色头顶中心与横向半轴，结合归一化重心适配 Q 版与标准立绘。"""
        frame = self._resolve_active_frame()
        return PetStageRenderer.head_crown_anchor(
            sprite_rect,
            self._settings.style_mode,
            getattr(self, "_visual_state", None),
            self._active_anchor_x(frame),
        )

    def _paint_stage(self, painter: QPainter) -> None:
        frame = self._resolve_active_frame()
        sprite_rect = compute_local_sprite_rect(
            stage_w=float(self.width()),
            stage_h=float(self.height()),
            sprite_h=self._sprite_height_px(),
            docked_side=self._settings.docked_side,
            style_mode=self._settings.style_mode,
            use_std_walk_rig=self._use_std_walk_rig,
            use_chibi_walk_rig=self._use_chibi_walk_rig,
            std_rig_size=self._std_rig_size(),
            chibi_rig_size=self._chibi_rig_size(),
            active_clip=self._active_clip,
            active_pixmap=frame,
            dock_progress=self._transform_damper.dock_progress,
        )

        anchor_x = self._active_anchor_x(frame)
        # 高速抛掷/弹跳期间禁止保留淡出旧槽，避免空中拖出双层残影
        if self._is_falling and self._crossfader.is_active:
            self._crossfader.clear()
        outgoing = self._crossfader.outgoing
        # 在角色有效区域（包含漂浮位移与淡化过渡并集）填充 alpha=1 底色，防止动作切换或悬空时鼠标句柄穿透丢失
        hit_rect = QRectF(sprite_rect).united(
            sprite_rect.translated(self._final_transform.trans_x, self._final_transform.trans_y)
        )
        if outgoing is not None:
            hit_rect = hit_rect.united(outgoing.sprite_rect).united(
                outgoing.sprite_rect.translated(
                    self._final_transform.trans_x, self._final_transform.trans_y
                )
            )
        hit_rect = hit_rect.adjusted(-6.0, -6.0, 6.0, 6.0).intersected(
            QRectF(0.0, 0.0, float(self.width()), float(self.height()))
        )
        painter.fillRect(hit_rect, QColor(0, 0, 0, 1))

        pivot_x, pivot_y = PetStageRenderer.pivot_for_rect(
            sprite_rect, self._settings.docked_side, anchor_x
        )
        show_dizzy_halo = (
            getattr(self, "_visual_state", None) == PetState.DIZZY
            and self._settings.particles_enabled
            and self._settings.docked_side == DockSide.NONE
        )
        if show_dizzy_halo:
            PetStageRenderer.paint_dizzy_halo(
                painter,
                sprite_rect=sprite_rect,
                pivot_x=pivot_x,
                pivot_y=pivot_y,
                transform=self._final_transform,
                anim_time=self._anim_time,
                scale=self._effective_scale(),
                style_mode=self._settings.style_mode,
                visual_state=getattr(self, "_visual_state", None),
                anchor_x_norm=anchor_x,
                front_layer=False,
            )

        current_slot = VisualRenderSlot(
            slot_key=getattr(self, "_active_slot_key", ""),
            clip=self._active_clip,
            pixmap=frame,
            use_chibi_rig=self._use_chibi_walk_rig,
            use_std_rig=self._use_std_walk_rig,
            sprite_rect=sprite_rect,
            transform=self._final_transform,
            chibi_kinematics=self._motion_engine.chibi_kinematics,
            std_kinematics=self._motion_engine.std_kinematics,
            docked_side=self._settings.docked_side,
            style_mode=self._settings.style_mode,
            stage_size=(self.width(), self.height()),
            anchor_x_norm=anchor_x,
            is_dragging=self._is_dragging,
            elapsed_sec=self._clip_elapsed_sec,
        )
        self._last_render_slot = current_slot

        rig = self._active_walk_rig()
        out_alpha, in_alpha = self._crossfader.opacities()
        if outgoing is not None and out_alpha > 0.02:
            PetStageRenderer.paint_slot(
                painter,
                outgoing,
                rig,
                opacity=out_alpha,
                live_transform=self._final_transform,
            )
        PetStageRenderer.paint_slot(painter, current_slot, rig, opacity=in_alpha)

        if show_dizzy_halo:
            PetStageRenderer.paint_dizzy_halo(
                painter,
                sprite_rect=sprite_rect,
                pivot_x=pivot_x,
                pivot_y=pivot_y,
                transform=self._final_transform,
                anim_time=self._anim_time,
                scale=self._effective_scale(),
                style_mode=self._settings.style_mode,
                visual_state=getattr(self, "_visual_state", None),
                anchor_x_norm=anchor_x,
                front_layer=True,
            )

        PetStageRenderer.paint_particles(painter, self._particle_system.particles)

    def _active_walk_rig(self) -> WalkRigAssets:
        return self._sprites.walk_rig

    def _get_local_drag_anchor(self) -> QPointF:
        """返回用于拖拽锚定补偿的角色躯干中心（横向按视觉重心、纵向按内容中心）。"""
        rect = self._get_local_sprite_rect()
        pivot_x, _ = PetStageRenderer.pivot_for_rect(
            rect,
            self._settings.docked_side,
            self._active_anchor_x(self._resolve_active_frame()),
        )
        return QPointF(pivot_x, rect.center().y())

    def _refresh_sprite_visual(self, *, compensate_drag: bool = True) -> None:
        if self._override_pose is not None:
            active = self._override_pose
        elif self._is_dragging:
            active = PetState.FLOAT
        elif (
            not self._is_walking
            and not self._is_sleeping
            and not self._is_sulk
        ):
            active = self._resolve_resting_state()
        else:
            active = self._current_state
            if self._settings.docked_side == DockSide.NONE and active in (
                PetState.EDGE_LEFT,
                PetState.EDGE_LEFT_PEEK,
                PetState.EDGE_LEFT_SLEEP,
                PetState.EDGE_RIGHT,
                PetState.EDGE_RIGHT_PEEK,
                PetState.EDGE_RIGHT_SLEEP,
            ):
                active = self._resolve_resting_state()

        if self._settings.doze_mode and self._settings.docked_side != DockSide.NONE:
            active = self._resolve_resting_state()
        self._visual_state = active

        style = self._settings.style_mode
        dock = self._settings.docked_side
        stage_size = (self.width(), self.height())
        # 拖拽期间若发生动作切换（如蹲姿 SULK <-> FLOAT），记录切换前躯干锚点以便补偿窗口坐标
        drag_anchor_before = (
            self._get_local_drag_anchor()
            if compensate_drag and self._is_dragging and dock == DockSide.NONE
            else None
        )
        has_animated_walk = self._sprites.has_animated_clip(PetState.RUNNING, style)
        use_walk_rig = (
            self._is_walking
            and not has_animated_walk
            and self._override_pose is None
            and not self._is_dragging
            and dock == DockSide.NONE
        )
        rig = self._active_walk_rig()

        if use_walk_rig and style == PetStyleMode.STANDARD and rig.standard_complete:
            slot_key = "rig:standard"
            self._crossfader.notify_transition(
                new_key=slot_key,
                new_style=style,
                new_dock=dock,
                previous_slot=self._last_render_slot,
                stage_size=stage_size,
                is_dragging=self._is_dragging,
                new_use_rig=True,
                allow_blend=False,
            )
            self._active_slot_key = slot_key
            self._use_std_walk_rig = True
            self._use_chibi_walk_rig = False
            self._active_clip = None
            self._active_static_pixmap = None
            return

        if use_walk_rig and style == PetStyleMode.CHIBI and rig.chibi_complete:
            slot_key = "rig:chibi"
            self._crossfader.notify_transition(
                new_key=slot_key,
                new_style=style,
                new_dock=dock,
                previous_slot=self._last_render_slot,
                stage_size=stage_size,
                is_dragging=self._is_dragging,
                new_use_rig=True,
                allow_blend=False,
            )
            self._active_slot_key = slot_key
            self._use_std_walk_rig = False
            self._use_chibi_walk_rig = True
            self._active_clip = None
            self._active_static_pixmap = None
            return

        # 骨架只用于无动态素材时的实际移动；其余状态统一经 ActionClip 渲染并按几何兼容性决定是否短时淡化。
        clip = self._sprites.get_clip(active, style)
        slot_key = clip.clip_id if clip is not None else f"state:{style.value}:{active.value}"
        if clip is not self._active_clip:
            self._clip_elapsed_sec = 0.0
        self._active_slot_key = slot_key
        self._use_std_walk_rig = False
        self._use_chibi_walk_rig = False
        self._active_clip = clip
        self._active_static_pixmap = clip.preview_pixmap() if clip is not None else None
        next_sprite_rect = self._get_local_sprite_rect()
        # 抛掷飞行与高频震颤炸毛直接清槽切换，避免动态位移叠加双层重影
        allow_crossfade = (not self._is_falling) and active != PetState.REDHOT
        self._crossfader.notify_transition(
            new_key=slot_key,
            new_style=style,
            new_dock=dock,
            previous_slot=self._last_render_slot,
            stage_size=stage_size,
            is_dragging=self._is_dragging,
            new_use_rig=False,
            new_sprite_rect=next_sprite_rect,
            allow_blend=allow_crossfade,
        )

        # 拖拽中切动作若角色躯干锚点发生偏移，立即补偿窗口锚点并反向修正淡出残影，保持紧贴鼠标不跳移
        if drag_anchor_before is not None and self._is_dragging and dock == DockSide.NONE:
            drag_anchor_after = self._get_local_drag_anchor()
            shift_x = drag_anchor_before.x() - drag_anchor_after.x()
            shift_y = drag_anchor_before.y() - drag_anchor_after.y()
            if abs(shift_x) > 0.5 or abs(shift_y) > 0.5:
                self._win_start_left += shift_x
                self._win_start_top += shift_y
                self._crossfader.translate_outgoing(-shift_x, -shift_y)
                dx = self._last_move_global.x() - self._drag_start_global.x()
                dy = self._last_move_global.y() - self._drag_start_global.y()
                self.move(
                    int(round(self._win_start_left + dx)),
                    int(round(self._win_start_top + dy)),
                )


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
        if self._settings.anti_gravity_mode:
            return PetState.FLOAT

        return PetState.IDLE

    def _effective_ai_interval_ms(self) -> int:
        prof = self._idle_speech_profile()
        if self._is_walking:
            # 偏安静模式下单次漫步不超过 4.5 秒，避免长距离来回走动显得躁动
            return min(prof.ai_interval_ms, 4500)
        return prof.ai_interval_ms

    def _sync_ai_timer_interval(self) -> None:
        interval = self._effective_ai_interval_ms()
        if self._ai_timer.interval() != interval:
            self._ai_timer.setInterval(interval)

    def _restart_ai_timer(self) -> None:
        """回到常态待机后重新计时，避免刚恢复默认动作就立刻切到下一个自动动作。"""
        self._sync_ai_timer_interval()
        if self._ai_timer.isActive():
            self._ai_timer.start()

    def _set_temporary_pose(
        self,
        state: PetState,
        duration_sec: float,
        bubble_message: Optional[str] = None,
        force_bubble: bool = False,
        *,
        pending_sulk: bool = False,
        locked: bool = False,
    ) -> None:
        self._is_walking = False
        self._is_sleeping = state in (PetState.SLEEPING, PetState.EDGE_LEFT_SLEEP, PetState.EDGE_RIGHT_SLEEP)
        self._override_pose = state
        self._override_is_autonomous = False
        self._override_is_sync = False
        self._override_locked = locked
        self._pending_sulk_after_pose = pending_sulk
        self._override_until_utc = datetime.now(timezone.utc) + timedelta(seconds=duration_sec)
        self._refresh_sprite_visual()
        self._update_sprite_transforms()
        self._stage_canvas.update()

        if bubble_message and bubble_message.strip():
            self.show_bubble(bubble_message, duration_sec, force=force_bubble)

    def _set_autonomous_pose(
        self,
        state: PetState,
        bubble_message: Optional[str] = None,
        *,
        duration_sec: float = 4.2,
    ) -> None:
        """设置持续到下一次自动决策的闲置动作，避免动作切换间隙闪回默认待机。"""
        self._is_walking = False
        self._is_sleeping = False
        self._override_pose = state
        self._override_is_autonomous = True
        self._override_is_sync = False
        self._override_locked = False
        self._pending_sulk_after_pose = False
        self._override_until_utc = datetime.max.replace(tzinfo=timezone.utc)
        self._refresh_sprite_visual()
        self._update_sprite_transforms()
        self._stage_canvas.update()

        if bubble_message and bubble_message.strip():
            self.show_bubble(bubble_message, duration_sec)

    # ==================== Quota Lookup Feedback ====================

    def react_quota_lookup_started(self) -> None:
        """额度查询开始：Q 版与标准立绘统一切到查找姿势，查询提示交给额度窗口自身。"""
        self._mark_pet_user_activity()
        self._set_temporary_pose(PetState.SEARCHING, 12.0)

    def react_quota_lookup_finished(self, snapshot: object) -> None:
        """额度查询结束：成功切开心姿势，失败切沮丧姿势；额度结果只在看板展示，不弹气泡。"""
        self._mark_pet_user_activity()
        if bool(getattr(snapshot, "success", False)):
            self._set_temporary_pose(PetState.HAPPY, 4.0)
        else:
            self._set_temporary_pose(PetState.OOPS, 4.5)

    # ==================== Autonomous AI & System Monitor ====================

    @staticmethod
    def _idle_speech_profile():
        return CharacterLore.get_bubble_activity_preset()

    def _mark_pet_user_activity(self) -> None:
        prof = self._idle_speech_profile()
        self._last_pet_activity_monotonic = time.monotonic()
        self._idle_speech_next_delay_sec = random.uniform(
            prof.pet_min_sec, prof.pet_max_sec
        )

    def _can_trigger_idle_speech(self) -> bool:
        if (
            not self.isVisible()
            or self._settings.doze_mode
            or self._mouse_down
            or self._is_dragging
            or self._is_falling
            or self._is_sulk
            or self._is_chat_open
            or bool(self._waiting_chat_workers)
            or (self._override_pose is not None and not self._override_is_autonomous)
            or self._sync_bubble_active
            or self._sync_thinking_pinned
            or self._sync_waiting_for_reply
            or self._sync_busy_active
            or bool(self._ag_playback)
            or self._ag_pending_replace is not None
            or self._is_forced_bubble_active
            or self._speech_bubble.isVisible()
            or self._context_menu.isVisible()
            or (self._settings.docked_side != DockSide.NONE and self._is_hovering)
        ):
            return False
        return True

    def _idle_speech_timer_tick(self) -> None:
        if not self._can_trigger_idle_speech():
            return
        now_mono = time.monotonic()
        idle_since = max(self._last_pet_activity_monotonic, self._last_idle_speech_monotonic)
        if now_mono - idle_since < self._idle_speech_next_delay_sec:
            return
        self._trigger_idle_speech()

    def _trigger_idle_speech(self) -> bool:
        """抽取随机台词并切换到匹配动作；仅开启默认气泡时弹出台词气泡。"""
        prof = self._idle_speech_profile()
        docked = self._settings.docked_side
        allow_running = (
            self._settings.auto_walk
            and not self._settings.anti_gravity_mode
            and docked == DockSide.NONE
        )
        line, pose = CharacterLore.pick_idle_speech(
            docked_side=docked,
            allow_running=allow_running,
            allow_float=True,
            exclude_line=self._last_idle_speech_line,
            exclude_state=self._last_idle_speech_state,
        )
        now_mono = time.monotonic()
        self._last_idle_speech_line = line
        self._last_idle_speech_state = pose
        self._last_idle_speech_monotonic = now_mono
        self._idle_speech_next_delay_sec = random.uniform(
            prof.pet_min_sec, prof.pet_max_sec
        )
        bubble_msg = line if self._settings.show_speech_bubble else None

        if docked != DockSide.NONE:
            self._set_temporary_pose(pose, prof.bubble_duration_sec, bubble_msg)
        elif pose == PetState.RUNNING:
            self._override_pose = None
            self._override_is_autonomous = False
            self._override_is_sync = False
            self._pending_sulk_after_pose = False
            self._is_sleeping = False
            self._is_walking = True
            self._walk_dir = 1 if random.randint(0, 1) == 0 else -1
            self._current_state = PetState.RUNNING
            self._refresh_sprite_visual()
            self._update_sprite_transforms()
            self._stage_canvas.update()
            if bubble_msg:
                self.show_bubble(bubble_msg, prof.bubble_duration_sec)
        else:
            self._set_autonomous_pose(pose, bubble_msg, duration_sec=prof.bubble_duration_sec)

        bubble_shown = (
            bool(bubble_msg)
            and self._speech_bubble.isVisible()
            and not self._is_chat_open
            and not self._sync_bubble_active
        )
        self._idle_speech_bubble_active = bubble_shown
        self._restart_ai_timer()
        return True

    def _ai_timer_tick(self) -> None:
        self._sync_ai_timer_interval()
        if (
            self._mouse_down
            or self._is_dragging
            or self._is_falling
            or (self._override_pose is not None and not self._override_is_autonomous)
            or self._sync_busy_active
            or self._is_sulk
            or self._is_chat_open
            or (self._idle_speech_bubble_active and self._speech_bubble.isVisible())
        ):
            return

        # 打盹模式：保持原地睡觉待机，不漫步、不做随机动作也不闲聊
        if self._settings.doze_mode:
            self._is_walking = False
            self._override_pose = None
            self._override_is_autonomous = False
            self._override_is_sync = False
            self._current_state = self._resolve_resting_state()
            self._refresh_sprite_visual()
            self._update_sprite_transforms()
            self._stage_canvas.update()
            return

        prof = self._idle_speech_profile()
        if self._settings.docked_side != DockSide.NONE:
            if self._override_is_autonomous:
                self._override_pose = None
                self._override_is_autonomous = False
                self._refresh_sprite_visual()
                self._update_sprite_transforms()
                self._stage_canvas.update()
            if not self._is_hovering and random.random() < prof.edge_peek_prob:
                peek = (
                    PetState.EDGE_LEFT_PEEK
                    if self._settings.docked_side == DockSide.LEFT
                    else PetState.EDGE_RIGHT_PEEK
                )
                self._set_temporary_pose(peek, 3.2)
            return

        if self._is_walking:
            self._is_walking = False
            self._override_pose = None
            self._override_is_autonomous = False
            self._current_state = self._resolve_resting_state()
            self._refresh_sprite_visual()
            self._update_sprite_transforms()
            self._stage_canvas.update()
            self._sync_ai_timer_interval()
            return

        # 仅在打盹模式下入睡，关闭时不再随机打盹
        self._is_sleeping = False
        prev_pose = self._override_pose if self._override_is_autonomous else None
        roll = random.random()
        walk_cutoff = prof.ai_walk_prob
        quirk_cutoff = walk_cutoff + prof.ai_quirk_prob
        float_cutoff = quirk_cutoff + prof.ai_float_prob

        if self._settings.auto_walk and not self._settings.anti_gravity_mode and roll < walk_cutoff:
            self._override_pose = None
            self._override_is_autonomous = False
            self._is_walking = True
            self._walk_dir = 1 if random.randint(0, 1) == 0 else -1
            self._current_state = PetState.RUNNING
            self._refresh_sprite_visual()
            self._update_sprite_transforms()
            self._stage_canvas.update()
            self._sync_ai_timer_interval()
        elif roll < quirk_cutoff:
            quirks = [
                PetState.IDLE2,
                PetState.IDLE3,
                PetState.HUM,
                PetState.STRETCH,
                PetState.YAWN,
                PetState.THINKING,
                PetState.DRAWING,
            ]
            candidates = [state for state in quirks if state != prev_pose] or quirks
            pick = random.choice(candidates)
            self._set_autonomous_pose(pick)
        elif roll < float_cutoff and not self._settings.anti_gravity_mode:
            self._set_autonomous_pose(PetState.FLOAT)
        else:
            self._override_pose = None
            self._override_is_autonomous = False
            self._current_state = self._resolve_resting_state()
            self._refresh_sprite_visual()
            self._update_sprite_transforms()
            self._stage_canvas.update()

    def _monitor_timer_tick(self) -> None:
        if (
            not self._settings.system_monitor_enabled
            or self._settings.doze_mode
            or self._is_dragging
            or self._is_sulk
            or self._is_action_playback_locked()
            or self._is_sync_working_locked()
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

    # ==================== Mouse Drag, Shake Bristle, Dizzy & Headpat ====================

    def _current_shake_energy(self) -> float:
        return max(self._whip_energy, self._spin_energy)

    def _reset_drag_shake_tracking(self) -> None:
        self._is_shaking = False
        self._shake_stage = 0
        self._shake_peak_energy = 0.0
        self._motion_engine.drag_tilt_vx = 0.0
        self._whip_energy = 0.0
        self._spin_energy = 0.0
        self._shake_last_reversal_mono = 0.0
        self._shake_last_active_mono = 0.0
        self._dizzy_last_particle_mono = 0.0
        self._shake_axis_x = [0, 0.0, 0.0, 0.0]
        self._shake_axis_y = [0, 0.0, 0.0, 0.0]
        self._reset_spin_segment_state()

    def _reset_spin_segment_state(self) -> None:
        self._spin_seg_dx = 0.0
        self._spin_seg_dy = 0.0
        self._spin_seg_start_mono = 0.0
        self._spin_prev_vec = None
        self._spin_last_vec_mono = 0.0
        self._spin_curve_dir = 0
        self._spin_curve_rad = 0.0

    def _is_action_playback_locked(self) -> bool:
        """判断当前是否处于不可中断的交互动作播放期（烦恼/鼓嘴/炸毛/眩晕/生气）。"""
        return (
            self._override_locked
            and self._override_pose is not None
            and not self._override_is_autonomous
            and datetime.now(timezone.utc) < self._override_until_utc
        )

    def _is_sync_working_locked(self) -> bool:
        """反重力同步工作进行中屏蔽除查看额度与贴边外的交互动作，直至本轮完成释放。"""
        return (
            self._sync_busy_active
            and self._sync_turn_pose is not None
            and not self._settings.doze_mode
            and not (self._is_chat_open and not self._ag_viewing_sync)
        )

    def _is_drag_float_allowed(self, now_mono: Optional[float] = None) -> bool:
        """判断当前拖拽是否应展示正常反重力悬浮反馈（完全炸毛停晃满 2 秒恢复 FLOAT，锁定/摇晃活跃期禁切）。"""
        now = time.monotonic() if now_mono is None else now_mono
        if self._is_sync_working_locked():
            return False
        if self._is_shaking and self._override_pose == PetState.REDHOT:
            if (
                self._shake_last_active_mono > 0.0
                and (now - self._shake_last_active_mono) >= _SHAKE_STOP_RECOVER_S
            ):
                return True
            return False
        if self._is_action_playback_locked():
            return False
        if (
            self._is_shaking
            and self._shake_last_active_mono > 0.0
            and (now - self._shake_last_active_mono) <= _SHAKE_HOLD_ACTIVE_S
        ):
            return False
        return True

    @staticmethod
    def _update_drag_whip_axis(
        axis_state: list[float | int],
        step: float,
        now_mono: float,
        prev_move_mono: float,
    ) -> bool:
        """识别单轴高速折返摇晃：新方向行程需达到有效位移与速度，兼容高回报率鼠标且抗微噪。"""
        if abs(step) < 1e-6:
            return False

        last_fast_dir = int(axis_state[0])
        accum = float(axis_state[1])
        stroke_start = float(axis_state[2])
        last_fast_mono = float(axis_state[3])
        step_dir = 1 if step > 0.0 else -1

        if accum == 0.0 or (accum > 0.0 and step_dir < 0) or (accum < 0.0 and step_dir > 0):
            accum = step
            stroke_start = (
                prev_move_mono
                if prev_move_mono > 0.0
                and (now_mono - prev_move_mono) <= _SHAKE_IDLE_RESET_S
                else now_mono
            )
        else:
            accum += step

        axis_state[1] = accum
        axis_state[2] = stroke_start

        if abs(accum) < _SHAKE_STROKE_MIN_PX:
            return False

        elapsed = max(0.012, now_mono - stroke_start)
        stroke_speed = abs(accum) / elapsed
        if stroke_speed < _SHAKE_MIN_SPEED_PX_S:
            axis_state[1] = 0.0
            axis_state[2] = now_mono
            return False

        is_reversal = (
            last_fast_dir != 0
            and step_dir != last_fast_dir
            and (now_mono - last_fast_mono) <= _SHAKE_STROKE_MAX_AGE_S
        )
        axis_state[0] = step_dir
        axis_state[3] = now_mono
        if is_reversal:
            axis_state[1] = 0.0
            axis_state[2] = now_mono
            return True

        if abs(accum) >= _SHAKE_STROKE_MIN_PX * 2.0:
            axis_state[1] = step_dir * _SHAKE_STROKE_MIN_PX
            axis_state[2] = now_mono - (
                _SHAKE_STROKE_MIN_PX / max(stroke_speed, _SHAKE_MIN_SPEED_PX_S)
            )
        return False

    def _update_drag_spin_step(
        self,
        step_x: float,
        step_y: float,
        now_mono: float,
        prev_move_mono: float,
    ) -> bool:
        """按最小位移步段累计连续同向画圈弧度，超过热身角后转为摇晃能量。"""
        if self._spin_seg_start_mono <= 0.0:
            self._spin_seg_start_mono = (
                prev_move_mono
                if prev_move_mono > 0.0
                and (now_mono - prev_move_mono) <= _SHAKE_IDLE_RESET_S
                else now_mono
            )
        self._spin_seg_dx += step_x
        self._spin_seg_dy += step_y
        seg_len = math.hypot(self._spin_seg_dx, self._spin_seg_dy)
        if seg_len < _SHAKE_SPIN_SEG_MIN_PX:
            return False

        elapsed = max(0.012, now_mono - self._spin_seg_start_mono)
        speed = seg_len / elapsed
        ux = self._spin_seg_dx / seg_len
        uy = self._spin_seg_dy / seg_len
        self._spin_seg_dx = 0.0
        self._spin_seg_dy = 0.0
        self._spin_seg_start_mono = now_mono

        if speed < _SHAKE_SPIN_MIN_SPEED_PX_S:
            self._spin_prev_vec = None
            self._spin_curve_dir = 0
            self._spin_curve_rad = 0.0
            return False

        gained = False
        if (
            self._spin_prev_vec is not None
            and (now_mono - self._spin_last_vec_mono) <= _SHAKE_IDLE_RESET_S
        ):
            px, py = self._spin_prev_vec
            cross = px * uy - py * ux
            dot = max(-1.0, min(1.0, px * ux + py * uy))
            dtheta = math.atan2(cross, dot)
            # 过滤直线抖动（<4.5°）和 180° 急折返（交给双轴摇晃分支处理，避免重复计分）
            if 0.08 <= abs(dtheta) <= 2.15:
                turn_dir = 1 if dtheta > 0.0 else -1
                if self._spin_curve_dir != turn_dir:
                    self._spin_curve_dir = turn_dir
                    self._spin_curve_rad = abs(dtheta)
                else:
                    prev_rad = self._spin_curve_rad
                    self._spin_curve_rad += abs(dtheta)
                    if self._spin_curve_rad > _SHAKE_SPIN_WARMUP_RAD:
                        effective_rad = self._spin_curve_rad - max(prev_rad, _SHAKE_SPIN_WARMUP_RAD)
                        if effective_rad > 0.0:
                            self._spin_energy = min(
                                _SHAKE_ENERGY_MAX,
                                self._spin_energy + effective_rad * _SHAKE_SPIN_GAIN_PER_RAD,
                            )
                            gained = True
            elif abs(dtheta) > 2.15:
                self._spin_curve_dir = 0
                self._spin_curve_rad = 0.0
        else:
            self._spin_curve_dir = 0
            self._spin_curve_rad = 0.0

        self._spin_prev_vec = (ux, uy)
        self._spin_last_vec_mono = now_mono
        return gained

    def _target_drag_shake_stage(self, now_mono: float) -> int:
        recent = (
            self._shake_last_active_mono > 0.0
            and (now_mono - self._shake_last_active_mono) <= _SHAKE_HOLD_ACTIVE_S
        )
        effective_energy = (
            max(self._current_shake_energy(), self._shake_peak_energy)
            if recent
            else self._current_shake_energy()
        )
        if effective_energy >= _SHAKE_REDHOT_THRESHOLD:
            return 3
        if effective_energy >= _SHAKE_PUFFED_THRESHOLD:
            return 2
        if effective_energy >= _SHAKE_ANNOYED_THRESHOLD:
            return 1
        return 0

    def _apply_drag_shake_stage(self, stage: int, now_mono: float) -> None:
        """进入指定的拖拽摇晃炸毛阶段，每个阶段播放完前均锁定不可中途打断。"""
        self._is_shaking = True
        self._shake_stage = stage
        self._shake_last_active_mono = now_mono
        if stage == 3:
            self._motion_engine.bristle_burst.trigger(1.0)
            self._set_temporary_pose(
                PetState.REDHOT,
                _SHAKE_REDHOT_DURATION_S,
                CharacterLore.pick_line("redhot"),
                locked=True,
            )
            if (now_mono - self._bristle_last_sound_mono) >= 0.65:
                self._bristle_last_sound_mono = now_mono
                self._sound.play(PetSoundEffect.ANGRY)
            self._spawn_bristle_particles(3)
            self._bristle_last_particle_mono = now_mono
        elif stage == 2:
            self._motion_engine.bristle_burst.trigger(0.55, keep_higher=True)
            self._set_temporary_pose(
                PetState.PUFFED,
                _SHAKE_PUFFED_DURATION_S,
                CharacterLore.pick_line("puffed"),
                locked=True,
            )
            self._spawn_bristle_particles(2)
            self._bristle_last_particle_mono = now_mono
        else:
            self._set_temporary_pose(
                PetState.ANNOYED,
                _SHAKE_ANNOYED_DURATION_S,
                CharacterLore.pick_line("annoyed"),
                locked=True,
            )
            self._spawn_bristle_particles(1)
            self._bristle_last_particle_mono = now_mono

    def _advance_drag_shake_stage_if_ready(self, now_mono: float) -> bool:
        """上一摇晃阶段动作完整播完后，如仍在摇晃则在同一帧推进下一阶段（烦恼 -> 鼓嘴 -> 炸毛）。"""
        if not self._is_dragging or self._is_action_playback_locked():
            return False
        target = self._target_drag_shake_stage(now_mono)
        if target <= self._shake_stage:
            return False
        next_stage = min(target, self._shake_stage + 1)
        self._apply_drag_shake_stage(next_stage, now_mono)
        return True

    def _process_drag_shake_step(
        self,
        step_x: float,
        step_y: float,
        now_mono: float,
        prev_move_mono: float,
    ) -> bool:
        # 同步工作期或正在播放眩晕/生气等非摇晃锁定动作期间，不被拖拽摇晃中途打断
        if self._is_sync_working_locked():
            return False
        if self._is_action_playback_locked() and self._override_pose not in (
            PetState.ANNOYED,
            PetState.PUFFED,
            PetState.REDHOT,
        ):
            return False
        if (
            prev_move_mono > 0.0
            and (now_mono - prev_move_mono) > _SHAKE_IDLE_RESET_S
        ):
            self._shake_axis_x = [0, 0.0, 0.0, 0.0]
            self._shake_axis_y = [0, 0.0, 0.0, 0.0]
            self._reset_spin_segment_state()

        rev_x = self._update_drag_whip_axis(
            self._shake_axis_x, step_x, now_mono, prev_move_mono
        )
        rev_y = self._update_drag_whip_axis(
            self._shake_axis_y, step_y, now_mono, prev_move_mono
        )
        spin_gained = self._update_drag_spin_step(
            step_x, step_y, now_mono, prev_move_mono
        )

        whip_gained = False
        if rev_x or rev_y:
            self._shake_last_active_mono = now_mono
            if (now_mono - self._shake_last_reversal_mono) >= _SHAKE_REVERSAL_COOLDOWN_S:
                self._shake_last_reversal_mono = now_mono
                self._whip_energy = min(
                    _SHAKE_ENERGY_MAX, self._whip_energy + _SHAKE_WHIP_GAIN
                )
                whip_gained = True

        if spin_gained:
            self._shake_last_active_mono = now_mono

        if whip_gained or spin_gained:
            self._shake_peak_energy = max(
                self._shake_peak_energy, self._current_shake_energy()
            )

        if self._is_shaking and (rev_x or rev_y or spin_gained):
            if self._override_pose == PetState.REDHOT:
                self._override_until_utc = max(
                    self._override_until_utc,
                    datetime.now(timezone.utc) + timedelta(seconds=_SHAKE_STOP_RECOVER_S),
                )
                if (now_mono - self._bristle_last_particle_mono) >= 0.30:
                    self._spawn_bristle_particles(1)
                    self._bristle_last_particle_mono = now_mono

        if not (whip_gained or spin_gained):
            return False

        # 当前阶段尚未播完时只累计摇晃能量，不中途打断；播完后才推进下一阶段
        if self._is_action_playback_locked():
            return False

        return self._advance_drag_shake_stage_if_ready(now_mono)

    def _trigger_dizzy_state(
        self,
        now_mono: float,
        *,
        particle_count: int = 5,
    ) -> None:
        """炸毛松手后进入下落眩晕 -> 生气序列（眩晕与生气期间均锁定播完，播完自动回常态）。"""
        self._is_sulk = True
        self._poke_count = 0
        self._poke_stage = 0
        self._poke_stage_until_mono = 0.0
        self._pat_hover_blocked_until_mono = now_mono + _POST_DIZZY_PAT_GRACE_S
        self._set_temporary_pose(
            PetState.DIZZY,
            _DIZZY_POSE_DURATION_S,
            CharacterLore.pick_line("dizzy"),
            pending_sulk=True,
            locked=True,
        )
        self._spawn_dizzy_particles(particle_count)
        self._dizzy_last_particle_mono = now_mono

    def _spawn_bristle_particles(self, intensity: int) -> None:
        """按怒气强度在角色耳尖上方与两侧爆发怒气/火花粒子，避免遮挡面部表情。"""
        if not self._settings.particles_enabled or intensity <= 0:
            return
        sprite_rect = self._get_local_sprite_rect()
        cx, cy, head_rx = self._head_crown_anchor(sprite_rect)
        self._particle_system.spawn_bristle(cx, cy, head_rx, intensity)

    def _spawn_dizzy_particles(self, count: int) -> None:
        """生成头顶飘散的晕眩星光粒子，与 3D 绕头星光光环风格统一。"""
        if not self._settings.particles_enabled or count <= 0:
            return
        sprite_rect = self._get_local_sprite_rect()
        cx, cy, head_rx = self._head_crown_anchor(sprite_rect)
        self._particle_system.spawn_dizzy(cx, cy, head_rx, count)

    def _fire_pending_body_double_click_quota(self) -> None:
        if (
            self._mouse_down
            or self._is_dragging
            or self._is_sulk
            or self._poke_stage > 0
            or self._is_action_playback_locked()
        ):
            return
        self._poke_count = 0
        self.open_quota_requested.emit()

    def _begin_left_press_tracking(self, event: QMouseEvent, *, is_double_click: bool = False) -> None:
        now_mono = time.monotonic()
        self._quota_double_click_timer.stop()
        last_shake_mono = max(
            self._shake_last_reversal_mono, self._shake_last_active_mono
        )
        continuing_shake = (
            self._override_pose in (PetState.ANNOYED, PetState.PUFFED, PetState.REDHOT)
            and not self._override_is_sync
            and datetime.now(timezone.utc) < self._override_until_utc
            and last_shake_mono > 0.0
            and (now_mono - last_shake_mono) <= _SHAKE_RELEASE_GRACE_S
        )

        self._mouse_down = True
        self._mouse_pressed_physically = self._is_primary_mouse_button_held()
        self._press_is_double_click = is_double_click
        self._press_started_while_locked = self._is_action_playback_locked()
        self._is_dragging = False
        self._drag_trail.clear()
        if not self._press_started_while_locked:
            self._is_falling = False
            self._throw_vx = 0.0
            self._fall_velocity_y = 0.0
            self._throw_pos_x = None
            self._throw_pos_y = None
        self._is_sleeping = False
        self._pat_travel = 0.0
        if not continuing_shake:
            self._reset_drag_shake_tracking()
        else:
            # 刚处于摇晃生气/炸毛状态又立即点按续晃时保留阶段，避免起步闪回反重力动作
            self._is_shaking = True
            self._drag_tilt_vx = 0.0
            self._shake_axis_x = [0, 0.0, 0.0, 0.0]
            self._shake_axis_y = [0, 0.0, 0.0, 0.0]
            self._reset_spin_segment_state()

        gpos = event.globalPosition()
        self._drag_start_global = QPointF(gpos.x(), gpos.y())
        self._last_move_global = QPointF(gpos.x(), gpos.y())
        self._last_move_mono = now_mono
        self._win_start_left = float(self.x())
        self._win_start_top = float(self.y())
        self._stage_canvas.grabMouse()

    def _on_stage_mouse_press(self, event: QMouseEvent) -> None:
        self._mark_pet_user_activity()
        if event.button() == Qt.MouseButton.RightButton:
            self._quota_double_click_timer.stop()
            self._context_menu.popup(event.globalPosition().toPoint())
            event.accept()
            return

        if event.button() != Qt.MouseButton.LeftButton:
            return

        self._begin_left_press_tracking(event, is_double_click=False)
        event.accept()

    def _on_stage_mouse_double_click(self, event: QMouseEvent) -> None:
        if event.button() != Qt.MouseButton.LeftButton:
            return
        self._mark_pet_user_activity()
        self._quota_double_click_timer.stop()

        # 锁定动作播放期间禁止被双击查额度或再次点按打断
        if self._is_action_playback_locked():
            if not self._mouse_down:
                self._begin_left_press_tracking(event, is_double_click=True)
            else:
                self._press_is_double_click = True
                self._press_started_while_locked = True
            event.accept()
            return

        local_pos = event.position()
        sprite_rect = self._get_local_sprite_rect()
        rel_y = (
            (local_pos.y() - sprite_rect.y()) / sprite_rect.height()
            if sprite_rect.height() > 0
            else 0.5
        )
        head_ratio = self._head_click_ratio()

        # 贴边、同步工作期或常态下双击头部直接呼出额度；双击身体或闹脾气期间转入点击释放统一判定，防连戳误开额度
        direct_quota = (
            self._settings.docked_side != DockSide.NONE
            or self._is_sync_working_locked()
            or (rel_y <= head_ratio and not self._is_sulk)
        )
        if direct_quota:
            self._mouse_down = False
            self._press_is_double_click = False
            self._is_dragging = False
            self._is_shaking = False
            self._is_walking = False
            if self._stage_canvas.mouseGrabber() is self._stage_canvas:
                self._stage_canvas.releaseMouse()
            self.open_quota_requested.emit()
            if self._use_std_walk_rig or self._use_chibi_walk_rig:
                self._refresh_sprite_visual()
                self._update_sprite_transforms()
                self._stage_canvas.update()
            event.accept()
            return

        if not self._mouse_down:
            self._begin_left_press_tracking(event, is_double_click=True)
        else:
            self._press_is_double_click = True
        event.accept()

    def _start_drag_at_global_pos(self, cur_g: QPointF) -> None:
        """进入拖拽状态，处理贴边脱离或姿态切换的窗口锚点补偿并立即对齐鼠标。"""
        now_mono = time.monotonic()
        dx = cur_g.x() - self._drag_start_global.x()
        dy = cur_g.y() - self._drag_start_global.y()
        self._quota_double_click_timer.stop()
        self._press_is_double_click = False
        self._motion_engine.reset_transient_effects()
        self._is_walking = False
        self._walk_pos_x = None
        self._is_dragging = True
        self._is_falling = False
        self._throw_vx = 0.0
        self._fall_velocity_y = 0.0
        self._throw_pos_x = None
        self._throw_pos_y = None
        self._drag_trail.clear()
        self._drag_trail.add_sample(cur_g.x(), cur_g.y(), now_mono)
        self._override_is_autonomous = False
        if not self._is_action_playback_locked():
            self._pending_sulk_after_pose = False
        if self._is_drag_float_allowed(now_mono):
            self._override_pose = PetState.FLOAT
            self._override_is_sync = False
            self._override_locked = False
            self._override_until_utc = datetime.max.replace(tzinfo=timezone.utc)
        if self._settings.docked_side != DockSide.NONE:
            old_local_rect = self._get_local_sprite_rect()
            self._settings.docked_side = DockSide.NONE
            # 同步工作期间从侧边拖回桌面时立即恢复本轮固定忙碌动作，再按新锚点对齐鼠标
            if (
                self._is_sync_working_locked()
                and self._override_pose is None
                and self._sync_turn_pose is not None
            ):
                self._override_pose = self._sync_turn_pose
                self._override_is_autonomous = False
                self._override_is_sync = True
                self._override_locked = False
                self._override_until_utc = datetime.max.replace(tzinfo=timezone.utc)
            self._current_state = self._resolve_resting_state()
            self._refresh_sprite_visual(compensate_drag=False)
            # 从贴边半身切换到居中完整立绘时单次补偿窗口偏移并立即落位，避免角色瞬移脱离鼠标。
            new_anchor = self._get_local_drag_anchor()
            self._win_start_left += old_local_rect.center().x() - new_anchor.x()
            self._win_start_top += old_local_rect.center().y() - new_anchor.y()
            self._update_dock_alignment()
        else:
            self._refresh_sprite_visual()
        self.move(
            int(round(self._win_start_left + dx)),
            int(round(self._win_start_top + dy)),
        )
        self._update_sprite_transforms()
        self._stage_canvas.update()

    def _on_stage_mouse_move(self, event: QMouseEvent) -> None:
        local_pos = event.position()
        sprite_rect = self._get_local_sprite_rect()

        if self._mouse_down:
            cur_g = event.globalPosition()
            dx = cur_g.x() - self._drag_start_global.x()
            dy = cur_g.y() - self._drag_start_global.y()

            if not self._is_dragging and math.hypot(dx, dy) > 5.0:
                self._start_drag_at_global_pos(cur_g)

            if self._is_dragging:
                self._sync_drag_to_global_pos(cur_g)
            return

        # 悬停头部抚摸检测（按画风适配头部区域占比；同步工作期及锁定动作播放期间不积累或触发摸头）
        hit_h = sprite_rect.height()
        if self._settings.docked_side == DockSide.NONE and hit_h > 20.0:
            now_mono = time.monotonic()
            if (
                now_mono < self._pat_hover_blocked_until_mono
                or self._is_action_playback_locked()
                or self._is_sync_working_locked()
            ):
                self._pat_travel = 0.0
                self._last_pat_local = QPointF(local_pos.x(), local_pos.y())
                return
            rel_y = (local_pos.y() - sprite_rect.y()) / hit_h
            if 0.0 <= rel_y <= self._head_hover_ratio() and sprite_rect.left() <= local_pos.x() <= sprite_rect.right():
                dist = math.hypot(
                    local_pos.x() - self._last_pat_local.x(),
                    local_pos.y() - self._last_pat_local.y(),
                )
                if dist < 80.0:
                    self._pat_travel += dist
                self._last_pat_local = QPointF(local_pos.x(), local_pos.y())

                if (
                    self._pat_travel >= 60.0
                    and (now_mono - self._last_hover_pat_mono) >= _HOVER_PAT_COOLDOWN_S
                ):
                    self._pat_travel = 0.0
                    self._last_hover_pat_mono = now_mono
                    self.trigger_pat()
            else:
                self._pat_travel = 0.0

    def _enter_bristle_redhot(self, now_mono: float, *, recoil_dir: float = 1.0) -> None:
        """进入炸毛爆发态：瞬间弹跳膨胀、火花怒气爆发，播完后衔接生气（5 秒）并锁定至播完回常态。"""
        self._poke_stage = 3
        self._poke_stage_until_mono = 0.0
        self._is_sulk = True
        self._motion_engine.trigger_poke(0.18, recoil_dir)
        self._motion_engine.bristle_burst.trigger(1.0)
        self._set_temporary_pose(
            PetState.REDHOT,
            _REDHOT_POSE_DURATION_S,
            CharacterLore.pick_line("redhot"),
            pending_sulk=True,
            locked=True,
        )
        if (now_mono - self._bristle_last_sound_mono) >= 0.65:
            self._bristle_last_sound_mono = now_mono
            self._sound.play(PetSoundEffect.ANGRY)
        self._spawn_bristle_particles(3)
        self._bristle_last_particle_mono = now_mono

    def _handle_body_poke(
        self,
        now_mono: float,
        local_pos: QPointF,
        sprite_rect: QRectF,
        *,
        is_double_click: bool,
    ) -> None:
        """处理点击身体交互：单点闲聊，连戳按阶段播完递进 烦恼 -> 鼓嘴 -> 炸毛 -> 闹脾气。"""
        if (
            self._waiting_chat_workers
            or self._is_action_playback_locked()
            or self._is_sync_working_locked()
        ):
            return

        recoil_dir = -1.0 if local_pos.x() >= sprite_rect.center().x() else 1.0

        # 烦恼或鼓嘴完整播完后的短暂怒气记忆期内再次戳身体，顺阶进入下一阶段动作
        if self._poke_stage > 0 and now_mono <= self._poke_stage_until_mono:
            self._poke_last_mono = now_mono
            if self._poke_stage == 1:
                self._poke_stage = 2
                self._poke_stage_until_mono = 0.0
                self._poke_count = _POKE_PUFFED_COUNT
                self._motion_engine.trigger_poke(0.15, recoil_dir)
                self._motion_engine.bristle_burst.trigger(0.55, keep_higher=True)
                self._set_temporary_pose(
                    PetState.PUFFED,
                    _PUFFED_POSE_DURATION_S,
                    CharacterLore.pick_line("puffed"),
                    locked=True,
                )
                self._spawn_bristle_particles(2)
                self._bristle_last_particle_mono = now_mono
                return
            if self._poke_stage == 2:
                self._poke_count = _POKE_REDHOT_COUNT
                self._enter_bristle_redhot(now_mono, recoil_dir=recoil_dir)
                return

        if self._poke_stage > 0 and now_mono > self._poke_stage_until_mono:
            self._poke_stage = 0
            self._poke_count = 0
            self._poke_stage_until_mono = 0.0

        if (
            self._poke_last_mono <= 0.0
            or (now_mono - self._poke_last_mono) > _POKE_COMBO_WINDOW_S
        ):
            self._poke_count = 0

        self._poke_count += 1
        self._poke_last_mono = now_mono

        # 同位快速第 2 击先延时判定是否为“双击查额度”；若第 3 击紧随而至则取消额度并进入烦恼态
        if is_double_click and self._poke_stage == 0 and self._poke_count == 2:
            self._motion_engine.trigger_poke(0.08, recoil_dir)
            self._quota_double_click_timer.start()
            self._update_sprite_transforms()
            self._stage_canvas.update()
            return

        if self._poke_count >= _POKE_ANNOYED_COUNT:
            self._poke_stage = 1
            self._poke_stage_until_mono = 0.0
            self._motion_engine.trigger_poke(0.12, recoil_dir)
            self._set_temporary_pose(
                PetState.ANNOYED,
                _ANNOYED_POSE_DURATION_S,
                CharacterLore.pick_line("annoyed"),
                locked=True,
            )
            self._spawn_bristle_particles(1)
            self._bristle_last_particle_mono = now_mono
        else:
            self._motion_engine.trigger_poke(0.09, recoil_dir)
            line = CharacterLore.pick_line("chatter")
            pose = CharacterLore.match_pose_for_line("chatter", line)
            self._set_temporary_pose(pose, 4.5, line)
            self._sound.play(PetSoundEffect.CHIME)

    def _sync_drag_to_global_pos(self, cur_g: QPointF) -> None:
        """按全局鼠标坐标 1:1 更新窗口位置与摇晃轨迹，确保人物句柄与鼠标永不脱节。"""
        dx = cur_g.x() - self._drag_start_global.x()
        dy = cur_g.y() - self._drag_start_global.y()
        step_x = cur_g.x() - self._last_move_global.x()
        step_y = cur_g.y() - self._last_move_global.y()
        if abs(step_x) < 1e-6 and abs(step_y) < 1e-6:
            return

        now_mono = time.monotonic()
        prev_move_mono = self._last_move_mono
        dt = max(0.004, now_mono - prev_move_mono) if prev_move_mono > 0.0 else 0.016
        self._last_move_global = QPointF(cur_g.x(), cur_g.y())
        self._last_move_mono = now_mono
        self._drag_trail.add_sample(cur_g.x(), cur_g.y(), now_mono)

        self.move(
            int(round(self._win_start_left + dx)),
            int(round(self._win_start_top + dy)),
        )
        vx = step_x / dt
        self._motion_engine.push_drag_velocity_x(vx)
        self._process_drag_shake_step(step_x, step_y, now_mono, prev_move_mono)

    @staticmethod
    def _is_primary_mouse_button_held() -> bool:
        return _is_primary_mouse_button_held()

    def _on_stage_mouse_grab_lost(self) -> None:
        """处理切动作/气泡弹出或焦点变化导致的 Qt 抓握丢失，避免拖拽卡死或句柄失联。"""
        if not self._mouse_down or not self._mouse_pressed_physically:
            return
        if self._is_primary_mouse_button_held():
            # 用户左键仍在按下：尝试恢复抓握，后续由帧循环光标轮询继续无缝跟随
            if self.isVisible() and self._stage_canvas.mouseGrabber() is not self._stage_canvas:
                self._stage_canvas.grabMouse()
            return
        self._recover_lost_mouse_release()

    def _recover_lost_mouse_release(self) -> None:
        """左键已物理松开但漏收 mouseReleaseEvent 时，安全收尾当前点击或拖拽。"""
        if not self._mouse_down:
            return
        self._mouse_down = False
        self._mouse_pressed_physically = False
        self._press_is_double_click = False
        self._press_started_while_locked = False
        self._pat_travel = 0.0
        if self._stage_canvas.mouseGrabber() is self._stage_canvas:
            self._stage_canvas.releaseMouse()
        if not self._is_dragging:
            self._is_shaking = False
            self._shake_stage = 0
            self._shake_peak_energy = 0.0
            self._drag_tilt_vx = 0.0
            self._drag_trail.clear()
            return
        self._finish_drag_release(time.monotonic())

    def _finish_drag_release(self, now_mono: float) -> None:
        self._is_dragging = False
        release_vel = self._drag_trail.estimate_release_velocity(now_mono)
        self._drag_trail.clear()
        last_shake_mono = max(
            self._shake_last_reversal_mono, self._shake_last_active_mono
        )
        recent_shake = (
            last_shake_mono > 0.0
            and (now_mono - last_shake_mono) <= _SHAKE_RELEASE_GRACE_S
        )
        stopped_recovered = (
            self._is_shaking
            and self._override_pose == PetState.REDHOT
            and self._shake_last_active_mono > 0.0
            and (now_mono - self._shake_last_active_mono) >= _SHAKE_STOP_RECOVER_S
        )
        # 完整摇晃流程：摇晃进入炸毛（REDHOT）后松手 -> 下落眩晕（DIZZY）-> 生气（SULK）；
        # 若停晃已满 2 秒则视为已恢复正常流程，松手按正常反重力 -> 下落 Q 弹处理。
        bristled_on_release = (
            not stopped_recovered
            and self._shake_stage >= 3
            and (
                self._override_pose == PetState.REDHOT
                or recent_shake
                or self._current_shake_energy() >= _SHAKE_PUFFED_THRESHOLD
            )
        )

        if self._is_sync_working_locked():
            # 同步工作期放行贴边吸附；未贴边时仅平稳回落并保持本轮固定忙碌动作，不触发眩晕或悬浮。
            self._try_edge_dock_or_gravity_drop(
                release_vel=release_vel,
                refresh_resting=False,
                sync_working=True,
            )
        elif bristled_on_release:
            self._trigger_dizzy_state(now_mono, particle_count=5)
            self._clamp_and_apply_gravity_after_release(refresh_resting=False)
        elif not stopped_recovered and self._is_action_playback_locked():
            # 未到炸毛的烦恼/鼓嘴或其他未播完锁定动作：松手保持播完不中断并正常落地
            self._clamp_and_apply_gravity_after_release(refresh_resting=False)
        else:
            self._is_sulk = False
            self._override_pose = None
            self._override_is_autonomous = False
            self._override_is_sync = False
            self._override_locked = False
            self._pending_sulk_after_pose = False
            self._current_state = self._resolve_resting_state()
            self._try_edge_dock_or_gravity_drop(release_vel=release_vel)
            self._restart_ai_timer()

        self._is_shaking = False
        self._shake_stage = 0
        self._shake_peak_energy = 0.0
        self._drag_tilt_vx = 0.0
        self._settings.last_left = float(self.x())
        self._settings.last_top = float(self.y())
        # 防抖延迟落盘，避免抛掷松手起飞第一帧同步磁盘写阻塞主渲染线程
        self._schedule_settings_save()
        self.visual_bounds_changed.emit()

    def _on_stage_mouse_release(self, event: QMouseEvent) -> None:
        if event.button() != Qt.MouseButton.LeftButton or not self._mouse_down:
            return
        self._mark_pet_user_activity()
        self._mouse_down = False
        self._mouse_pressed_physically = False
        is_double_click = self._press_is_double_click
        self._press_is_double_click = False
        press_started_while_locked = self._press_started_while_locked
        self._press_started_while_locked = False
        self._pat_travel = 0.0
        self._last_pat_local = QPointF(event.position().x(), event.position().y())
        if self._stage_canvas.mouseGrabber() is self._stage_canvas:
            self._stage_canvas.releaseMouse()

        now_mono = time.monotonic()
        if not self._is_dragging:
            self._is_shaking = False
            self._shake_stage = 0
            self._shake_peak_energy = 0.0
            self._drag_tilt_vx = 0.0
            was_walking = self._is_walking
            self._is_walking = False
            # 当前交互动作尚未播完（或按下时仍处于锁定状态）及同步工作期，点击不中断或偷跑触发动作
            if (
                press_started_while_locked
                or self._is_action_playback_locked()
                or self._is_sync_working_locked()
            ):
                return
            local_pos = event.position()
            sprite_rect = self._get_local_sprite_rect()
            rel_y = (
                (local_pos.y() - sprite_rect.y()) / sprite_rect.height()
                if sprite_rect.height() > 0
                else 0.5
            )
            if rel_y <= self._head_click_ratio() and self._settings.docked_side == DockSide.NONE:
                self.trigger_pat()
            elif self._settings.docked_side != DockSide.NONE:
                peek = (
                    PetState.EDGE_LEFT_PEEK
                    if self._settings.docked_side == DockSide.LEFT
                    else PetState.EDGE_RIGHT_PEEK
                )
                self._set_temporary_pose(peek, 3.5, CharacterLore.pick_line("edge"))
                self._sound.play(PetSoundEffect.CHIME)
            else:
                self._handle_body_poke(
                    now_mono,
                    local_pos,
                    sprite_rect,
                    is_double_click=is_double_click,
                )
            if was_walking and (self._use_std_walk_rig or self._use_chibi_walk_rig):
                self._refresh_sprite_visual()
                self._update_sprite_transforms()
                self._stage_canvas.update()
            return

        self._finish_drag_release(now_mono)

    def _enter_sulk_state(self) -> None:
        if self.isVisible() and self._is_sulk and not self._is_dragging:
            self._set_temporary_pose(
                PetState.SULK,
                _SULK_POSE_DURATION_S,
                CharacterLore.pick_line("sulk"),
                locked=True,
            )

    def trigger_pat(self) -> None:
        # 烦恼/鼓嘴/炸毛/眩晕/生气(5秒)及反重力同步工作期禁止被摸头中途打断
        if self._is_action_playback_locked() or self._is_sync_working_locked():
            return
        self._mark_pet_user_activity()
        self._quota_double_click_timer.stop()
        self._poke_count = 0
        self._poke_stage = 0
        self._poke_stage_until_mono = 0.0
        self._settings.total_pats += 1
        self._settings.favorability = min(9999, self._settings.favorability + 1)
        self._bond_badge_lbl.setText(f" · ♥ {self._settings.favorability}")

        if not self._waiting_chat_workers:
            # 正在等待模型回复时保持思考动作不被鼠标划过头顶打断
            self._set_temporary_pose(PetState.PAT, 2.8, CharacterLore.pick_line("pat"))

        now = datetime.now(timezone.utc)
        if (now - self._last_pat_sound_utc).total_seconds() > 0.45:
            self._last_pat_sound_utc = now
            self._sound.play(PetSoundEffect.PURR)
            self._schedule_settings_save()

        if self._settings.particles_enabled:
            sprite_rect = self._get_local_sprite_rect()
            cx, cy, head_rx = self._head_crown_anchor(sprite_rect)
            self._spawn_particle(
                "♥",
                "#F472B6",
                cx + (random.random() - 0.5) * head_rx * 1.4,
                cy + 6.0,
                (random.random() - 0.5) * 30.0,
                -45.0,
                1.1,
            )
            self._spawn_particle(
                "✦",
                "#C4B5FD",
                cx + (random.random() - 0.5) * head_rx * 1.6,
                cy + 14.0,
                (random.random() - 0.5) * 36.0,
                -52.0,
                1.0,
            )

    def _clamp_and_apply_gravity_after_release(
        self,
        *,
        refresh_resting: bool = False,
        release_vel: tuple[float, float] = (0.0, 0.0),
    ) -> None:
        """拖拽松手后保证角色留在工作区内，并按设置触发重力下落或轻抛惯性反弹。"""
        # 先切换到松手后的目标贴图再测可见边界，避免拿拖拽态 FLOAT 尺寸夹紧静态 IDLE 导致边缘错位
        if refresh_resting:
            self._current_state = self._resolve_resting_state()
        self._refresh_sprite_visual()

        _, _, min_x, max_x, min_y, floor_top = self._visible_sprite_work_bounds()
        clamped_x = int(round(max(min_x, min(max_x, float(self.x())))))
        clamped_y = int(round(max(min_y, min(floor_top, float(self.y())))))
        if clamped_x != self.x() or clamped_y != self.y():
            self.move(clamped_x, clamped_y)

        rvx, rvy = release_vel
        has_throw = math.hypot(rvx, rvy) > 1.0
        above_floor = float(self.y()) < floor_top - 2.0
        if (
            not self._settings.anti_gravity_mode
            and self._settings.gravity_enabled
            and (above_floor or has_throw)
        ):
            self._throw_vx = rvx
            self._fall_velocity_y = rvy
            self._throw_pos_x = float(self.x())
            self._throw_pos_y = float(self.y())
            self._is_falling = True
        else:
            self._throw_vx = 0.0
            self._fall_velocity_y = 0.0
            self._throw_pos_x = None
            self._throw_pos_y = None
            self._clamp_inside_nearest_work_area()

        self._stage_canvas.update()

    def _try_edge_dock_or_gravity_drop(
        self,
        *,
        release_vel: tuple[float, float] = (0.0, 0.0),
        refresh_resting: bool = True,
        sync_working: bool = False,
    ) -> None:
        area, sprite_rect, _, _, min_y, max_y = self._visible_sprite_work_bounds()
        sprite_left = float(self.x()) + sprite_rect.left()
        sprite_right = float(self.x()) + sprite_rect.right()
        snap_margin = 38.0 * self._effective_scale()
        rvx, _ = release_vel

        # 按角色可见边界判定贴边；向外甩出时不误吸回侧边栏，避免干扰抛掷飞行
        if (
            self._settings.edge_dock_enabled
            and rvx <= 0.0
            and sprite_left <= area.left() + snap_margin
        ):
            self._settings.docked_side = DockSide.LEFT
            if sync_working and self._override_is_sync:
                self._override_pose = None
                self._override_is_autonomous = False
                self._override_is_sync = False
                self._override_locked = False
            self._transform_damper.reset_dock_slide()
            self.move(area.left(), int(round(max(min_y, min(max_y, float(self.y()))))))
            self._current_state = (
                self._resolve_resting_state() if self._settings.doze_mode else PetState.EDGE_LEFT_PEEK
            )
            self._refresh_sprite_visual()
            self._update_sprite_transforms()
            self._update_dock_alignment()
            self._stage_canvas.update()
            if not sync_working:
                self.show_bubble(CharacterLore.pick_line("edge"), 3.5)
            self._sound.play(PetSoundEffect.CHIME)
            return

        if (
            self._settings.edge_dock_enabled
            and rvx >= 0.0
            and sprite_right >= area.right() + 1 - snap_margin
        ):
            self._settings.docked_side = DockSide.RIGHT
            if sync_working and self._override_is_sync:
                self._override_pose = None
                self._override_is_autonomous = False
                self._override_is_sync = False
                self._override_locked = False
            self._transform_damper.reset_dock_slide()
            self.move(
                area.right() + 1 - self.width(),
                int(round(max(min_y, min(max_y, float(self.y()))))),
            )
            self._current_state = (
                self._resolve_resting_state() if self._settings.doze_mode else PetState.EDGE_RIGHT_PEEK
            )
            self._refresh_sprite_visual()
            self._update_sprite_transforms()
            self._update_dock_alignment()
            self._stage_canvas.update()
            if not sync_working:
                self.show_bubble(CharacterLore.pick_line("edge"), 3.5)
            self._sound.play(PetSoundEffect.CHIME)
            return

        self._clamp_and_apply_gravity_after_release(
            refresh_resting=refresh_resting,
            release_vel=(0.0, 0.0) if sync_working else release_vel,
        )

    # ==================== Speech Bubble & Inline Gemini Chat ====================

    def _hide_speech_bubble(self) -> None:
        was_idle_speech = self._idle_speech_bubble_active
        self._idle_speech_bubble_active = False
        if was_idle_speech:
            prof = self._idle_speech_profile()
            now_mono = time.monotonic()
            self._last_idle_speech_monotonic = now_mono
            self._idle_speech_next_delay_sec = random.uniform(
                prof.pet_min_sec, prof.pet_max_sec
            )
        self._sync_thinking_pinned = False
        self._sync_thinking_visible = False
        self._sync_waiting_for_reply = False
        self._bubble_timer.stop()
        self._bubble_custom_offset = None
        self._transient_bubble_size = None
        self._sync_bubble_active = False
        self._disable_chat_scroll_auto()
        self._speech_bubble.setVisible(False)
        self._speech_bubble.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)

    def _resolve_active_chat_channel_and_models(self) -> tuple[str, list[str], str]:
        """返回当前 API 渠道连接状态、官方模型缓存及已选模型。"""
        current = normalize_gemini_model(self._settings.gemini_model)
        cached = GeminiChatService.get_cached_models_for_settings(self._settings)
        models = list(dict.fromkeys([*cached, current]))
        active_provider = normalize_api_provider(self._settings.api_provider)
        provider = self._chat.resolve_chat_provider(self._settings)
        if self._chat.get_api_error(self._settings):
            badge = f"⚠️ {format_api_provider_label(active_provider, short=True)} 不可用"
        elif provider == API_PROVIDER_AGENT_PLATFORM:
            badge = "在线 · Agent Platform"
        elif provider == API_PROVIDER_GEMINI_DEVELOPER:
            badge = "在线 · Gemini API"
        else:
            badge = "离线模式"
        return badge, models, current

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
        self._chat_path_panel.setVisible(in_chat and not self._ag_viewing_sync)
        self._history_list.setVisible(in_chat and self._history_expanded)
        busy = bool(self._waiting_chat_workers)
        self._history_list.setEnabled(not busy)
        self._new_chat_btn.setEnabled(not busy)
        self._task_folder_btn.setEnabled(not busy and not self._ag_viewing_sync)
        self._chat_clear_btn.setEnabled(not busy)
        self._chat_input_panel.setEnabled(not busy and not self._chat_readonly)
        self._chat_input_panel.setVisible(in_chat)
        self._chat_input.setPlaceholderText(
            "Antigravity 实时同步中（只读）" if self._chat_readonly else "输入聊天内容或指令…"
        )
        if not in_chat:
            self._chat_messages_panel.setVisible(False)
            self._bubble_text_lbl.setVisible(True)
            return

        badge_text, _models, current = self._resolve_active_chat_channel_and_models()
        error = self._chat.get_api_error(self._settings)
        active_provider = normalize_api_provider(self._settings.api_provider)
        active_label = format_api_provider_label(active_provider)
        provider = self._chat.resolve_chat_provider(self._settings)
        self._chat_channel_badge.setText(badge_text)
        if error:
            color, rgb = "#FCA5A5", "248, 113, 113"
            tooltip = f"{error}\n点击可切换 API Key 或打开设置"
        elif provider == API_PROVIDER_AGENT_PLATFORM:
            self._last_chat_api_notice = ""
            color, rgb = "#C4B5FD", "167, 139, 250"
            proj = (self._settings.agent_platform_project or "").strip()
            mode_hint = f"项目 {proj}" if proj else "Express 模式"
            tooltip = f"使用 Agent Platform API Key（{mode_hint}，点击可切换 Key 或打开设置）"
        elif provider == API_PROVIDER_GEMINI_DEVELOPER:
            self._last_chat_api_notice = ""
            color, rgb = "#93C5FD", "96, 165, 250"
            tooltip = "使用 Gemini API Key（点击可切换 Key 或打开设置）"
        else:
            color, rgb = "#FDE68A", "251, 191, 36"
            tooltip = (
                f"当前处于离线模式，点击可切换为在线模式（当前渠道：{active_label}）"
                if self._settings.chat_offline_mode
                else f"未填写 {active_label} Key，当前使用离线陪伴；点击可切换渠道或打开设置"
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

    def _effective_chat_mode(self) -> str:
        """当前所选渠道未填写 Key 或处于手动离线时返回 'offline'，否则返回生效渠道标识。"""
        if self._settings.chat_offline_mode or not GeminiChatService.has_active_api_key(
            self._settings
        ):
            return "offline"
        return normalize_api_provider(self._settings.api_provider)

    def _populate_connection_menu(self, menu: QMenu) -> None:
        self._populate_chat_mode_actions(menu)
        menu.addSeparator()
        settings = menu.addAction("⚙️ API Key 与模型设置...")
        settings.triggered.connect(self.open_chat_settings_requested)

    def _populate_chat_mode_actions(self, menu: QMenu) -> None:
        modes = QActionGroup(menu)
        modes.setExclusive(True)
        effective_mode = self._effective_chat_mode()
        dev_ready = bool((self._settings.gemini_api_key or "").strip())
        agent_ready = bool((self._settings.agent_platform_api_key or "").strip())
        options = (
            ("offline", "🌙 离线陪伴"),
            (
                API_PROVIDER_GEMINI_DEVELOPER,
                "🌐 在线 · Gemini API" + ("" if dev_ready else "（未配置）"),
            ),
            (
                API_PROVIDER_AGENT_PLATFORM,
                "☁️ 在线 · Agent Platform API" + ("" if agent_ready else "（未配置）"),
            ),
        )
        for mode, label in options:
            action = menu.addAction(label)
            action.setCheckable(True)
            action.setChecked(effective_mode == mode)
            action.setData(("chat_mode", mode))
            modes.addAction(action)
            action.triggered.connect(
                lambda _checked=False, value=mode, act=action: self._select_chat_mode_from_menu(
                    act, value
                )
            )

    def _select_chat_mode_from_menu(self, action: QAction, mode: str) -> None:
        """点击菜单后按实际生效模式回写勾选，避免无 Key 时在线挡被误选。"""
        self.select_chat_mode(mode)
        group = action.actionGroup()
        if group is None:
            return
        effective_mode = self._effective_chat_mode()
        for item in group.actions():
            item.setChecked(item.data() == ("chat_mode", effective_mode))

    def select_chat_mode(self, mode: str) -> None:
        if self._ag_viewing_sync:
            self._return_to_regular_chat()
        want_offline = mode == "offline"
        prev_offline = self._settings.chat_offline_mode
        prev_provider = normalize_api_provider(self._settings.api_provider)
        want_provider = prev_provider if want_offline else normalize_api_provider(mode)

        if not want_offline:
            has_target_key = (
                bool((self._settings.agent_platform_api_key or "").strip())
                if want_provider == API_PROVIDER_AGENT_PLATFORM
                else bool((self._settings.gemini_api_key or "").strip())
            )
            if not has_target_key:
                self._show_chat_status_notice(
                    f"✦ 请先在「设置 → 对话与模型」填写 {format_api_provider_label(want_provider)} Key",
                    force=True,
                )
                return

        if prev_offline == want_offline and (want_offline or prev_provider == want_provider):
            return

        self._settings.chat_offline_mode = want_offline
        self._settings.api_provider = want_provider
        try:
            self._settings_service.save(self._settings)
        except Exception as exc:
            self._settings.chat_offline_mode = prev_offline
            self._settings.api_provider = prev_provider
            self._show_chat_status_notice(f"⚠️ 保存聊天模式失败：{exc}", is_error=True, force=True)
            return
        self._chat.reset_conversation()
        self._sync_chat_controls()
        self.settings_synced.emit()
        if want_offline:
            notice = "✦ 已切换为离线陪伴，可以继续聊天和使用本地互动指令"
        elif want_provider == API_PROVIDER_AGENT_PLATFORM:
            notice = "✦ 已切换为 Agent Platform API 在线模式"
        else:
            notice = "✦ 已切换为 Gemini API 在线模式"
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
        """构建模型与思考强度菜单。"""
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

        menu.setProperty("chat_model_list", True)
        self._populate_chat_model_menu(menu)
        self._chat_model_menu = menu
        return menu

    def _populate_chat_model_menu(self, menu: QMenu) -> None:
        _badge_text, models, current = self._resolve_active_chat_channel_and_models()
        current_thinking = normalize_thinking_level(self._settings.thinking_level)
        menu.clear()
        # QWidgetAction 内的按钮不会触发 QMenu 的自动关闭行为。
        refresh = QWidgetAction(menu)
        refresh.setText("🔄 获取 / 刷新官方模型")
        button = QPushButton(refresh.text(), menu)
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.setStyleSheet("QPushButton { text-align: left; color: #F6F3FF; background: transparent; border: 0; border-radius: 7px; padding: 7px 24px 7px 14px; margin: 1px 4px; } QPushButton:hover { background: #3C2E75; } QPushButton:disabled { color: #7D75A8; }")
        refresh.setDefaultWidget(button)
        menu.addAction(refresh)
        busy = self._chat_models_discovery_worker is not None and self._chat_models_discovery_worker.isRunning()
        refresh.setEnabled(not self._settings.chat_offline_mode and not busy)
        button.clicked.connect(lambda: self._refresh_chat_models_async(explicit=True))
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


    def _open_chat_model_menu(self) -> None:
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
        if self._is_chat_open and self._chat_message_rows:
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
        if not GeminiChatService.has_active_api_key(self._settings):
            if explicit:
                label = format_api_provider_label(self._settings.api_provider)
                self._show_chat_status_notice(f"请先在设置中填写 {label} Key", force=True)
            return
        worker = _ChatModelsDiscoveryWorker(self._chat, self._settings, explicit=explicit, parent=self)
        worker.completed.connect(self._on_chat_models_discovered)
        self._chat_models_discovery_worker = worker
        worker.start()
        menu = self._chat_model_menu
        if menu is not None and menu.property("chat_model_list") and menu.isVisible():
            menu.actions()[0].setEnabled(False)

    def _on_chat_models_discovered(self, models: list[str], err_msg: str, explicit: bool = False) -> None:
        worker = self._chat_models_discovery_worker
        if worker is not None:
            worker.finished.connect(worker.deleteLater)
            if worker.isFinished():
                worker.deleteLater()
            self._chat_models_discovery_worker = None
            if GeminiChatService.connection_signature_for_settings(
                worker._settings
            ) != GeminiChatService.connection_signature_for_settings(self._settings):
                return
        self._sync_chat_controls()
        menu = self._chat_model_menu
        if menu is not None and menu.property("chat_model_list") and menu.isVisible():
            self._populate_chat_model_menu(menu)
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
        if self._is_chat_open and self._chat_message_rows:
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
        if not self._is_chat_open:
            return
        # 新消息布局完成前，此刻的 maximum
        # 可能还是旧值导致滚不到底：先按当前范围滚一次，再保持监听
        # rangeChanged，待滚动范围真正更新后继续贴底（用户主动滚动时解除）。
        bar = self._bubble_scroll.verticalScrollBar()
        bar.setValue(bar.maximum())
        if not self._chat_scroll_auto:
            self._chat_scroll_auto = True
            bar.rangeChanged.connect(self._on_chat_scroll_range_changed)

    def _on_chat_scroll_range_changed(self, _minimum: int, _maximum: int) -> None:
        # 滚动范围真正变化后补滚一次，窗口继续调整导致范围再变时仍保持贴底。
        bar = self._bubble_scroll.verticalScrollBar()
        bar.setValue(bar.maximum())

    def _on_chat_scroll_user_action(self, _action: int) -> None:
        # 用户拖动滑块或滚轮阅读历史时解除自动贴底，避免新内容把视口抢走。
        self._disable_chat_scroll_auto()

    def _disable_chat_scroll_auto(self) -> None:
        if not self._chat_scroll_auto:
            return
        self._chat_scroll_auto = False
        bar = self._bubble_scroll.verticalScrollBar()
        bar.rangeChanged.disconnect(self._on_chat_scroll_range_changed)

    def _append_chat_turn(
        self,
        *,
        role: str,
        sender: str,
        text: str,
        meta: str = "",
    ) -> tuple[QWidget, Optional[QLabel], QLabel]:
        """向标准聊天消息列表追加一条消息卡片（用户靠右、助手靠左、提示居中）。"""
        # 仅在聊天模式打开时切换为消息卡片区，避免普通气泡被切入聊天视图。
        if self._is_chat_open:
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
        path = self._chat_archive.session_path()
        self._chat_path.setText(str(path))
        self._chat_path.setCursorPosition(0)
        self._chat_path.setToolTip(str(path))
        saved = path.is_file()
        self._chat_open_file_btn.setEnabled(saved)
        self._chat_open_file_btn.setToolTip(
            "用系统默认程序打开当前会话文件" if saved
            else "首条消息保存后可打开会话文件"
        )
        if self._archive_error:
            self._chat_save_status.setText("保存失败 · 请检查配置目录是否可写")
        else:
            self._chat_save_status.setText(
                "当前对话保存路径 · 已保存" if saved
                else "当前对话保存路径 · 首条消息后保存"
            )

    def _open_chat_session_file(self) -> None:
        path = self._chat_archive.session_path()
        if not path.is_file():
            self._update_chat_save_path()
            return
        try:
            os.startfile(str(path))
        except OSError:
            self._chat_save_status.setText("打开会话文件失败，请检查文件是否被占用")

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
            if self._ag_sync_session_id is not None:
                records.insert(0, {
                    "id": self._ag_sync_session_id,
                    "title": "Antigravity 实时对话",
                    "updated": "同步中",
                })
            # 历史列表只展示已落盘的会话；未发送消息的新会话留在聊天区。
            selected = False
            for record in records:
                title = " ".join(str(record.get("title", "未命名会话")).split())
                # 自定义行负责全部文字和按钮；条目本身不能再绘制一份文本。
                item = QListWidgetItem()
                item.setData(Qt.ItemDataRole.UserRole, record["id"])
                item.setData(Qt.ItemDataRole.AccessibleTextRole, f"{title} {record.get('updated', '')}")
                item.setToolTip(title)
                self._history_list.addItem(item)
                row_widget = self._make_history_row_widget(
                    record["id"], title, str(record.get("updated", "")).replace("T", " ")
                )
                item.setSizeHint(row_widget.sizeHint())
                self._history_list.setItemWidget(item, row_widget)
                if (self._ag_viewing_sync and record["id"] == self._ag_sync_session_id) or (
                    not self._ag_viewing_sync and record["id"] == current
                ):
                    self._history_list.setCurrentItem(item)
                    selected = True
            if not selected:
                self._history_list.setCurrentRow(-1)
        finally:
            self._history_list.blockSignals(False)

    def _make_history_row_widget(self, session_id: str, title: str, updated: str) -> QWidget:
        """历史行只保留一份标题与时间；临时同步行不显示删除按钮。"""
        wrapper = QWidget(self._history_list)
        wrapper.setStyleSheet("background: transparent;")
        row = QHBoxLayout(wrapper)
        row.setContentsMargins(9, 7, 7, 7)
        row.setSpacing(6)

        text_col = QVBoxLayout()
        text_col.setContentsMargins(0, 0, 0, 0)
        text_col.setSpacing(1)
        title_lbl = QLabel(title)
        title_lbl.setMinimumWidth(0)
        title_lbl.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        title_lbl.setToolTip(title)
        title_lbl.setStyleSheet("color: #D8CCFF; font-size: 11px; background: transparent;")
        time_lbl = QLabel(updated)
        time_lbl.setMinimumWidth(0)
        time_lbl.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        time_lbl.setStyleSheet("color: #A89FD6; font-size: 10px; background: transparent;")
        text_col.addWidget(title_lbl)
        text_col.addWidget(time_lbl)
        row.addLayout(text_col, 1)
        if session_id != self._ag_sync_session_id:
            close_btn = _ChatActionButton("delete", wrapper)
            close_btn.setToolTip("删除该对话")
            # QPushButton 会消费鼠标事件，不会传播到列表触发条目选中切换。
            close_btn.clicked.connect(lambda _checked=False, sid=session_id: self._delete_session(sid))
            row.addWidget(close_btn, 0, Qt.AlignmentFlag.AlignVCenter)
        return wrapper

    def _toggle_chat_history(self, checked: bool) -> None:
        self._history_expanded = checked
        self._history_btn.setChecked(checked)
        self._refresh_chat_sessions()
        self._sync_chat_controls()
        self._relayout_stage_top()
        if checked:
            self._chat_splitter.setSizes([175, 360])

    def _clear_chat_rows(self) -> None:
        for row in self._chat_message_rows:
            self._chat_messages_layout.removeWidget(row)
            row.deleteLater()
        self._chat_message_rows.clear()
        self._ag_sync_labels.clear()

    def _select_chat_session(self, item, previous=None) -> None:
        if item is None or self._waiting_chat_workers:
            return
        session_id = item.data(Qt.ItemDataRole.UserRole)
        if session_id == self._ag_sync_session_id:
            self._show_sync_chat_session()
            return
        if session_id == self._chat_archive.session_id:
            if self._ag_viewing_sync:
                self._return_to_regular_chat()
            return
        try:
            record = self._chat_archive.load(session_id)
        except (OSError, ValueError):
            self._refresh_chat_sessions()
            self._chat_save_status.setText("无法读取所选会话，当前对话保持不变")
            return
        self._ag_viewing_sync = False
        self._ag_pending_handoff = False
        self._ag_regular_draft = ""
        self._chat_archive.session_id = session_id
        self._chat_archive.task_folder = record.get("task_folder", "")
        self._chat.restore_conversation(record["messages"])
        self._chat_input.clear()
        self._last_chat_api_notice = ""
        self._archive_error = ""
        self._chat_readonly = record.get("source") == "antigravity"
        # 浏览历史时保留全部卡片，不沿用旧版只显示最近 60 行的限制。
        if not record["messages"]:
            self._bubble_text_lbl.setText("想聊什么呀喵？✦")
        self._render_chat_messages(record)
        self._update_chat_save_path()
        self._sync_chat_controls()
        self._chat_input.setFocus()

    def _show_sync_chat_session(self) -> None:
        """将同步播报放到内存会话，保留普通聊天的存档与上下文。"""
        if self._ag_sync_session_id is None or self._waiting_chat_workers:
            return
        if not self._ag_viewing_sync:
            self._ag_regular_draft = self._chat_input.text()
        self._sync_thinking_pinned = False
        self._ag_viewing_sync = True
        self._chat_readonly = True
        self._chat_input.clear()
        self._render_sync_chat_events()
        self._refresh_chat_sessions()
        self._sync_chat_controls()
        self._relayout_stage_top()

    def _render_sync_chat_events(self) -> None:
        self._clear_chat_rows()
        self._chat_messages_panel.setVisible(False)
        self._bubble_text_lbl.setVisible(True)
        self._bubble_text_lbl.setText("Antigravity 实时对话 · 等待新消息✦")
        for event in self._ag_sync_events:
            self._append_sync_chat_event(event)
        self._relayout_stage_top()

    def _append_sync_chat_event(self, event: SyncEvent) -> None:
        if event.kind == "message" and event.role == "user":
            role, sender = "user", "你"
        elif event.kind == "message" and event.role == "assistant":
            role, sender = "assistant", "Antigravity"
        else:
            role, sender = "system", ""
        _, _, label = self._append_chat_turn(role=role, sender=sender, text=self._sync_event_text(event))
        self._ag_sync_labels.append(label)

    def _return_to_regular_chat(self) -> None:
        if not self._ag_viewing_sync:
            return
        self._ag_viewing_sync = False
        self._chat_readonly = False
        path = self._chat_archive.session_path()
        if path.is_file():
            try:
                record = self._chat_archive.load(self._chat_archive.session_id)
                self._chat.restore_conversation(record["messages"])
                self._render_chat_messages(record)
            except (OSError, ValueError):
                self._clear_chat_rows()
                self._chat.reset_conversation()
        else:
            self._clear_chat_rows()
            self._chat.reset_conversation()
            if self._is_chat_open:
                self._chat_messages_panel.setVisible(False)
                self._bubble_text_lbl.setVisible(True)
                self._bubble_text_lbl.setText("想聊什么呀喵？输入文字或指令试试✦")
        self._chat_input.setText(self._ag_regular_draft)
        self._ag_regular_draft = ""
        self._update_chat_save_path()
        self._refresh_chat_sessions()
        self._sync_chat_controls()
        if self._is_chat_open:
            self._relayout_stage_top()
            self._chat_input.setFocus()

    def _clear_chat_history(self) -> None:
        if self._waiting_chat_workers:
            return
        self._reset_chat_session("已开始新对话，旧会话已保存在历史对话中✦")

    def _reset_chat_session(self, notice: str) -> None:
        """新建与删除当前会话共用同一套界面和上下文重置。"""
        self._chat_archive.new_session()
        self._chat_readonly = False
        self._ag_viewing_sync = False
        self._ag_pending_handoff = False
        self._ag_regular_draft = ""
        self._archive_error = ""
        self._last_chat_api_notice = ""
        self._clear_chat_rows()
        self._chat.reset_conversation()
        self._chat_input.clear()
        self._chat_messages_panel.setVisible(False)
        self._bubble_text_lbl.setStyleSheet(
            "color: #F6F3FF; font-size: 12.5px; padding: 1px 4px 2px 0px; background: transparent;"
        )
        self._bubble_text_lbl.setText(notice)
        self._bubble_text_lbl.setVisible(True)
        self._update_chat_save_path()
        self._refresh_chat_sessions()
        self._sync_chat_controls()
        self._relayout_stage_top()
        self._chat_input.setFocus()

    def _delete_session(self, session_id: str) -> None:
        """删除指定会话并刷新历史列表。"""
        if self._waiting_chat_workers:
            return
        if session_id == self._ag_sync_session_id:
            return
        # 尚未落盘的新对话没有文件可删。
        if (
            session_id == self._chat_archive.session_id
            and not self._chat_archive.session_path().exists()
        ):
            return
        try:
            self._chat_archive.delete_session(session_id)
        except (OSError, ValueError):
            self._chat_save_status.setText("删除对话失败，请检查配置目录权限")
            return
        if session_id == self._chat_archive.session_id:
            self._reset_chat_session("已删除所选对话，开始新对话喵✦")
            return
        self._refresh_chat_sessions()

    # ==================== Antigravity 对话气泡播报 ====================

    def set_antigravity_sync_enabled(self, enabled: bool, *, silent: bool = False) -> None:
        """保存用户的同步意图；会话暂不可用时由轮询稍后重试。"""
        want = bool(enabled)
        previous = self._settings.chat_sync_enabled
        self._settings.chat_sync_enabled = want
        try:
            self._settings_service.save(self._settings)
        except OSError as exc:
            self._settings.chat_sync_enabled = previous
            self._sync_context_menu_checks()
            AppLogger.error("ChatSync", "保存实时同步设置失败", exc)
            if not silent:
                self.show_bubble("保存实时同步设置失败，请检查配置目录", 4.0, force=True)
            return
        self.apply_antigravity_sync_preference(want, silent=silent)
        self.settings_synced.emit()

    def apply_antigravity_sync_preference(self, enabled: bool, *, silent: bool = False) -> None:
        """应用已保存的播报开关，供菜单和设置窗口共用。"""
        if enabled and not self._ag_mirror_active:
            self.start_antigravity_sync(silent=silent)
            self._ag_next_retry_at = time.monotonic() + 10.0
        elif not enabled:
            if self._ag_mirror_active or self._ag_sync_session_id is not None:
                self.stop_antigravity_sync(silent=silent)
            self._ag_next_retry_at = 0.0
        self._sync_context_menu_checks()

    def _on_menu_sync_chat(self, checked: bool) -> None:
        self.set_antigravity_sync_enabled(checked)

    def _start_ag_poll_thread(self) -> None:
        """在后台守护线程周期性执行会话库扫描与增量 SQLite 读取，彻底移出 GUI 主线程。"""
        if self._ag_poll_thread is not None and self._ag_poll_thread.is_alive():
            return
        self._ag_poll_stop.clear()
        interval_sec = max(0.05, float(_SYNC_POLL_MS) / 1000.0)

        def _loop() -> None:
            while not self._ag_poll_stop.wait(interval_sec):
                if not self._ag_mirror_active:
                    continue
                try:
                    self._ag_collector.poll()
                except Exception as exc:
                    AppLogger.error("ChatSync", "反重力后台采集失败", exc)

        self._ag_poll_thread = threading.Thread(
            target=_loop,
            name="GeminiDesktopPetChatSyncPoll",
            daemon=True,
        )
        self._ag_poll_thread.start()

    def _stop_ag_poll_thread(self) -> None:
        self._ag_poll_stop.set()
        thread = self._ag_poll_thread
        self._ag_poll_thread = None
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=0.35)

    def _probe_antigravity_sync_async(self) -> None:
        """后台静默探测可用会话，避免未连接时每 10 秒在主线程全目录扫库造成掉帧。"""
        if self._ag_reconnect_inflight or self._ag_mirror_active:
            return
        self._ag_reconnect_inflight = True

        def _worker() -> None:
            conversation = None
            try:
                conversation = self._ag_sync_service.latest_conversation()
            except Exception as exc:
                AppLogger.warn("ChatSync", f"后台探测反重力会话失败：{exc}")
            self._ag_silent_connect_ready.emit(conversation)

        threading.Thread(
            target=_worker,
            name="GeminiDesktopPetChatSyncProbe",
            daemon=True,
        ).start()

    def _on_ag_silent_connect_ready(self, conversation: object) -> None:
        self._ag_reconnect_inflight = False
        if (
            self._closing_handled
            or self._ag_mirror_active
            or not self._settings.chat_sync_enabled
            or conversation is None
            or not getattr(conversation, "messages", None)
        ):
            return
        self._bind_antigravity_conversation(conversation, silent=True)

    def _bind_antigravity_conversation(self, conversation, *, silent: bool) -> None:
        self._ag_mirror_active = True
        self._ag_current_source_id = conversation.source_id
        self._ag_collector.reset()
        self._ag_collector.watch(conversation)
        self._ag_playback.clear()
        self._ag_last_shown = 0.0
        self._ag_last_replace = 0.0
        self._ag_pending_replace = None
        self._clear_sync_pose_state(release_pose=False)
        self._ag_dbg_ticks = 0
        self._ag_dbg_last = time.monotonic()
        # 首拉只记基线：已有的历史不重放，只播之后的新内容；若此时已有并发会话正在运行则同步进入工作态。
        self._ag_baseline = self._ag_sync_state.counters()
        initial_working = set(self._ag_collector.working_ids)
        if initial_working:
            self._apply_sync_working_snapshot(
                initial_working, completed=False, has_pending_events=False
            )
        self._start_ag_poll_thread()
        if self._is_chat_open:
            self._ag_sync_session_id = f"sync:{uuid.uuid4().hex}"
            self._ag_sync_events.clear()
            if silent:
                # 后台静默重连到可同步对话时仅刷新会话列表并继续收消息，不自动跳转。
                self._refresh_chat_sessions()
            elif self._waiting_chat_workers:
                self._ag_pending_handoff = True
            else:
                self._show_sync_chat_session()
        if not silent and not self._is_chat_open:
            self.show_bubble("Antigravity 对话播报已开启喵～新消息会出现在这里", 4.0)
        AppLogger.info(
            "ChatSync",
            f"开始播报：{conversation.source_id[:8]}, 基线={len(conversation.messages)}",
        )

    def start_antigravity_sync(self, *, silent: bool = False) -> None:
        """开启反重力对话播报：新消息直接显示在桌宠气泡里。

        silent=True 用于启动时恢复上次的开关，不弹开启提示、不盖掉问候气泡。
        """
        try:
            conversation = self._ag_sync_service.latest_conversation()
            if conversation is None or not conversation.messages:
                if not silent:
                    if self._is_chat_open:
                        self._chat_save_status.setText("等待可同步的 Antigravity 对话")
                    else:
                        self.show_bubble("等待可同步的 Antigravity 对话喵～", 3.5)
                    AppLogger.warn("ChatSync", "尚无可同步的反重力对话，稍后重试")
                return
            self._bind_antigravity_conversation(conversation, silent=silent)
        except Exception as exc:
            self._ag_mirror_active = False
            self._stop_ag_poll_thread()
            self._ag_collector.reset()
            self._ag_playback.clear()
            self._ag_pending_replace = None
            self._clear_sync_pose_state(release_pose=True)
            self._ag_baseline = {}
            self._ag_sync_session_id = None
            self._ag_sync_events.clear()
            self._ag_pending_handoff = False
            if self._ag_viewing_sync:
                self._return_to_regular_chat()
            AppLogger.error("ChatSync", "启动反重力播报失败", exc)
            if not silent:
                self.show_bubble(f"Antigravity 播报启动失败：{exc}", 5.0, force=True)

    def stop_antigravity_sync(self, *, silent: bool = False) -> None:
        """停止运行中的播报，并移除内存中的同步会话。"""
        self._sync_thinking_pinned = False
        self._sync_thinking_visible = False
        self._ag_mirror_active = False
        self._stop_ag_poll_thread()
        self._ag_current_source_id = ""
        self._ag_collector.reset()
        self._ag_playback.clear()
        self._ag_baseline = {}
        self._ag_last_replace = 0.0
        self._ag_pending_replace = None
        self._clear_sync_pose_state(release_pose=True)
        self._ag_sync_session_id = None
        self._ag_sync_events.clear()
        self._ag_pending_handoff = False
        if self._sync_bubble_active:
            self._sync_waiting_for_reply = False
            self._restart_sync_bubble_timeout()
        if self._ag_viewing_sync:
            self._return_to_regular_chat()
        else:
            self._refresh_chat_sessions()
        if not silent and not self._is_chat_open:
            self.show_bubble("已停止 Antigravity 播报喵～", 3.0)

    def _auto_resume_antigravity_sync(self) -> None:
        """启动时恢复上次的播报开关：后台探测会话，不阻塞主渲染循环也不覆盖问候气泡。"""
        if self._ag_mirror_active or not self._settings.chat_sync_enabled:
            return
        self._ag_next_retry_at = time.monotonic() + 10.0
        self._probe_antigravity_sync_async()

    def _refresh_antigravity_sync(self) -> None:
        """主线程轻量消费循环：仅读内存差量并播放气泡队列，所有扫盘与 SQLite 读取均在后台线程完成。"""
        if not self._ag_mirror_active:
            now = time.monotonic()
            if (
                self.isVisible()
                and self._settings.chat_sync_enabled
                and not self._is_falling
                and not self._is_dragging
                and now >= self._ag_next_retry_at
            ):
                self._ag_next_retry_at = now + 10.0
                self._probe_antigravity_sync_async()
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
            self._consume_antigravity_changes()
            self._play_antigravity_queue()
            # 兜底一致性巡检：队列已播完且采集器已绑定实库并确认无工作流程时，自动回归常态防止卡死。
            if (
                self._sync_busy_active
                and self._ag_collector.is_watching
                and not self._ag_playback
                and self._ag_pending_replace is None
                and not self._ag_collector.working_ids
            ):
                self._apply_sync_working_snapshot(
                    set(), completed=False, has_pending_events=False
                )
        except Exception as exc:
            AppLogger.error("ChatSync", "反重力播报刷新失败", exc)

    def _consume_antigravity_changes(self) -> None:
        """读状态差量：汇总多会话工作状态、续写就地刷新、新消息进播放队列。"""
        changes = dict(self._ag_sync_state.take_since(self._ag_baseline))
        if not changes:
            return

        status_data = changes.get("status")
        work_data = changes.get("work")
        events_data = changes.get("events")
        has_new_events = bool(
            isinstance(events_data, dict) and (events_data.get("items") or [])
        )

        if isinstance(status_data, dict):
            self._ag_current_source_id = str(status_data.get("source_id") or "")

        if isinstance(work_data, dict):
            active_ids = {
                str(sid) for sid in (work_data.get("active_ids") or ()) if str(sid)
            }
            self._apply_sync_working_snapshot(
                active_ids,
                completed=bool(work_data.get("completed")),
                has_pending_events=has_new_events
                or bool(self._ag_playback)
                or self._ag_pending_replace is not None,
            )

        if isinstance(status_data, dict) and status_data.get("switched"):
            self._ag_pending_replace = None
            if not self._sync_working_sessions:
                self._clear_sync_pose_state(release_pose=True)
            if self._sync_bubble_active:
                self._sync_waiting_for_reply = False
                self._restart_sync_bubble_timeout()
            self.show_bubble(
                f"✦ 已切换到新的 Antigravity 对话：{status_data.get('title', '')}", 3.0
            )

        if not isinstance(events_data, dict):
            return
        items = events_data.get("items") or []
        if not items:
            return
        if events_data.get("replace"):
            newest = items[-1]
            if self._ag_playback:
                # 队列末条就是这条消息，换成最新文本，保持播放顺序。
                self._ag_playback[-1] = newest
                self._ag_pending_replace = None
            elif not self._is_chat_open or self._ag_sync_session_id is not None:
                # 续写刷新走气泡；聊天面板开着时每条都追加系统行，会堆重复卡片。
                now = time.monotonic()
                if not self._ag_last_replace or now - self._ag_last_replace >= _SYNC_REPLACE_INTERVAL:
                    self._ag_last_replace = now
                    self._ag_pending_replace = None
                    self._show_sync_event(newest)
                else:
                    # 节流窗口内的末次续写先暂存，避免节流后丢失最终刷新内容。
                    self._ag_pending_replace = newest
            return
        if self._ag_pending_replace is not None and not self._ag_playback:
            pending = self._ag_pending_replace
            self._ag_pending_replace = None
            self._ag_last_replace = time.monotonic()
            self._show_sync_event(pending)
        self._ag_playback.extend(items)
        # 积压时丢最旧的，气泡追最新内容而不是慢慢补历史。
        while len(self._ag_playback) > _SYNC_QUEUE_LIMIT:
            self._ag_playback.popleft()

    def _play_antigravity_queue(self) -> None:
        """逐条播；你自己发的话不等节奏，立刻上气泡，其余按最小间隔排。"""
        now = time.monotonic()
        if not self._ag_playback:
            if (
                self._ag_pending_replace is not None
                and (not self._ag_last_replace or now - self._ag_last_replace >= _SYNC_REPLACE_INTERVAL)
            ):
                pending = self._ag_pending_replace
                self._ag_pending_replace = None
                self._ag_last_replace = now
                self._show_sync_event(pending)
            return
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

    def _can_apply_sync_pose(self) -> bool:
        """仅在不与打盹、贴边、普通对话、拖拽及锁定交互动作冲突时允许同步联动动作。"""
        if (
            not self.isVisible()
            or self._settings.doze_mode
            or self._settings.docked_side != DockSide.NONE
            or self._mouse_down
            or self._is_dragging
            or self._is_falling
            or self._is_sulk
            or bool(self._waiting_chat_workers)
            or self._is_action_playback_locked()
        ):
            return False
        # 聊天面板打开但用户正在查看普通聊天时，不抢占普通聊天的角色动作。
        if self._is_chat_open and not self._ag_viewing_sync:
            return False
        return True

    def _pick_sync_busy_pose(self) -> PetState:
        candidates = [pose for pose in _SYNC_BUSY_POSES if pose != self._sync_last_busy_pose]
        pick = random.choice(candidates or _SYNC_BUSY_POSES)
        self._sync_last_busy_pose = pick
        return pick

    def _apply_sync_pose(self, state: PetState, duration_sec: Optional[float]) -> None:
        """应用反重力同步联动动作，不触发额外台词气泡以免干扰实时消息。"""
        self._is_walking = False
        self._is_sleeping = False
        self._override_pose = state
        self._override_is_autonomous = False
        self._override_is_sync = True
        self._override_locked = False
        self._pending_sulk_after_pose = False
        self._override_until_utc = (
            datetime.max.replace(tzinfo=timezone.utc)
            if duration_sec is None
            else datetime.now(timezone.utc) + timedelta(seconds=duration_sec)
        )
        self._refresh_sprite_visual()
        self._update_sprite_transforms()
        self._stage_canvas.update()
        # 不同动作立绘宽度略有差异，气泡已显示时同步刷新贴合位置。
        if self._speech_bubble.isVisible() and not self._is_chat_open:
            self._reposition_speech_bubble_on_move()

    def _clear_sync_pose_state(self, *, release_pose: bool = True) -> None:
        """清理反重力同步的本轮固定忙碌状态；仅回收由同步设置的动作，不打断正常交互动作。"""
        self._sync_busy_active = False
        self._sync_turn_pose = None
        self._sync_working_sessions.clear()
        if release_pose and self._override_is_sync and self._override_pose is not None:
            self._override_pose = None
            self._override_is_autonomous = False
            self._override_is_sync = False
            self._override_locked = False
            self._pending_sulk_after_pose = False
            self._refresh_sprite_visual()
            self._update_sprite_transforms()
            self._stage_canvas.update()
            self._restart_ai_timer()

    def _apply_sync_working_snapshot(
        self,
        active_ids: set[str],
        *,
        completed: bool,
        has_pending_events: bool,
    ) -> None:
        """按采集器汇总的多会话工作状态同步当前活跃会话集合，等全部完成才释放工作态。"""
        was_working = self._sync_busy_active or bool(self._sync_working_sessions)
        self._sync_working_sessions = set(active_ids)
        if active_ids:
            if self._sync_turn_pose is None:
                self._sync_turn_pose = self._pick_sync_busy_pose()
            self._sync_busy_active = True
            if self._can_apply_sync_pose():
                can_take_pose = (
                    self._override_pose is None
                    or self._override_is_autonomous
                    or self._override_is_sync
                    or (
                        self._is_chat_open
                        and self._ag_viewing_sync
                        and self._override_pose == PetState.LISTENING
                    )
                )
                if can_take_pose and not (
                    self._override_is_sync and self._override_pose == self._sync_turn_pose
                ):
                    self._apply_sync_pose(self._sync_turn_pose, None)
            return

        if has_pending_events:
            # 本拍还有待播的完成事件，交给事件出队时同步切开心动作并刷新气泡。
            return

        if was_working:
            self._sync_busy_active = False
            self._sync_turn_pose = None
            if self._sync_bubble_active and self._sync_waiting_for_reply:
                self._sync_waiting_for_reply = False
                self._restart_sync_bubble_timeout()
            if completed:
                if self._can_apply_sync_pose():
                    self._apply_sync_pose(_SYNC_DONE_POSE, _SYNC_DONE_POSE_DURATION_S)
            else:
                self._clear_sync_pose_state(release_pose=True)

    def _update_sync_pose_for_event(self, event: SyncEvent) -> None:
        """每轮新对话随机选定前 4 个忙碌动作之一并在全部工作会话完成前固定，全部完成后切开心（hum）；取消则回归常态。"""
        session_key = event.source_id or self._ag_current_source_id or "_default"
        is_stopped = bool(event.stopped)
        is_completion = (
            not is_stopped
            and event.role == "assistant"
            and event.kind == "message"
            and not event.in_progress
        )
        is_user_turn = not is_stopped and event.role == "user" and event.kind == "message"
        is_working_stage = not is_stopped and (
            event.kind in ("thinking", "tool", "error") or event.in_progress
        )

        still_working = False
        if is_stopped:
            self._sync_working_sessions.discard(session_key)
            still_working = bool(self._sync_working_sessions)
            if still_working:
                self._sync_busy_active = True
                if self._sync_turn_pose is None:
                    self._sync_turn_pose = self._pick_sync_busy_pose()
            else:
                # 全部工作会话已被取消或终止：直接回归正常待机并释放交互，不切开心动作。
                self._clear_sync_pose_state(release_pose=True)
                return
        elif is_completion:
            self._sync_working_sessions.discard(session_key)
            still_working = bool(self._sync_working_sessions)
            if still_working:
                # 仍有其他并发对话正在工作：保持当前固定工作动作与交互屏蔽，不提前退出。
                self._sync_busy_active = True
                if self._sync_turn_pose is None:
                    self._sync_turn_pose = self._pick_sync_busy_pose()
            else:
                self._sync_busy_active = False
                self._sync_turn_pose = None
        elif is_user_turn:
            was_working = bool(self._sync_working_sessions)
            self._sync_working_sessions.add(session_key)
            # 仅在当前无其他进行中会话（全新一轮）或尚未选定动作时随机选定一个，并发加入时保持已固定的动作。
            if (not was_working and not event.replace) or self._sync_turn_pose is None:
                self._sync_turn_pose = self._pick_sync_busy_pose()
            self._sync_busy_active = True
            still_working = True
        elif is_working_stage:
            self._sync_working_sessions.add(session_key)
            if self._sync_turn_pose is None:
                self._sync_turn_pose = self._pick_sync_busy_pose()
            self._sync_busy_active = True
            still_working = True

        if not self._can_apply_sync_pose():
            # 全部完成时若当前处于贴边/拖拽/下落等无法切开心动作的场景，回收旧无限期工作动作避免永久卡态
            if is_completion and not still_working and self._override_is_sync:
                self._clear_sync_pose_state(release_pose=True)
            return

        can_take_pose = (
            self._override_pose is None
            or self._override_is_autonomous
            or self._override_is_sync
            or (
                self._is_chat_open
                and self._ag_viewing_sync
                and self._override_pose == PetState.LISTENING
            )
        )

        if is_completion and not still_working:
            # 全部工作会话完成：切开心动作（hum）；流式续写分片期间保持开心动作并顺延展示时长。
            if event.replace and not can_take_pose:
                return
            self._apply_sync_pose(_SYNC_DONE_POSE, _SYNC_DONE_POSE_DURATION_S)
            return

        if (still_working or is_user_turn or is_working_stage) and self._sync_turn_pose is not None:
            if not can_take_pose:
                return
            if self._override_is_sync and self._override_pose == self._sync_turn_pose:
                self._override_until_utc = datetime.max.replace(tzinfo=timezone.utc)
                return
            self._apply_sync_pose(self._sync_turn_pose, None)

    @staticmethod
    def _sync_event_text(event: SyncEvent) -> str:
        text = event.text.strip()
        if event.kind == "thinking":
            return f"💭 {text}"
        if event.kind == "error":
            return f"⚠ {text}"
        if event.kind == "tool":
            return f"⚙ {text}"
        if event.role == "user":
            return f"你：{text}"
        return text

    def _show_sync_event(self, event: SyncEvent) -> None:
        if not event.text.strip():
            return
        self._mark_pet_user_activity()
        message = self._sync_event_text(event)
        if self._ag_sync_session_id is not None:
            replace_last = bool(event.replace and self._ag_sync_events and (
                self._ag_sync_events[-1].kind == event.kind
                and self._ag_sync_events[-1].role == event.role
            ))
            if replace_last:
                self._ag_sync_events[-1] = event
            else:
                self._ag_sync_events.append(event)
            # 非实时会话视图或非聊天模式下仅在后台持续记录同步消息，不操作聊天消息卡片区。
            if self._is_chat_open and self._ag_viewing_sync:
                if replace_last and self._ag_sync_labels:
                    # 续写只更新当前文本，保留卡片、布局及用户的滚动位置。
                    label = self._ag_sync_labels[-1]
                    if label.text() != message:
                        label.setText(message)
                else:
                    self._append_sync_chat_event(event)
        self._update_sync_pose_for_event(event)
        if self._is_chat_open or not self.isVisible():
            return
        is_completion = event.role == "assistant" and event.kind == "message"
        if event.role == "user" or is_completion:
            self._sync_thinking_pinned = False
        # 仅在思考、工具执行或流式生成未完成时保持常亮；新一轮用户消息、完成回复、取消及错误提示按空闲倒计时自动关闭。
        self._sync_waiting_for_reply = (
            not event.stopped
            and (event.kind in ("thinking", "tool") or event.in_progress)
        )
        if not self._sync_thinking_pinned or event.kind == "thinking":
            preserve_scroll = bool(
                event.replace or (event.kind == "thinking" and self._sync_thinking_visible)
            )
            self._sync_thinking_visible = event.kind == "thinking"
            self._is_forced_bubble_active = False
            self._show_non_chat_bubble(
                message, from_sync=True, preserve_scroll=preserve_scroll
            )
        self._restart_sync_bubble_timeout()

    def _render_chat_messages(self, record: dict) -> None:
        self._clear_chat_rows()
        if self._is_chat_open:
            self._chat_messages_panel.setVisible(bool(record["messages"]))
            self._bubble_text_lbl.setVisible(not record["messages"])
        for message in record["messages"]:
            sender = "你" if message["role"] == "user" else "Gemini 娘"
            self._append_chat_turn(
                role=message["role"], sender=sender, text=message["text"],
                meta=f"{sender} · {message.get('time', '').replace('T', ' ')}",
            )
        if self._is_chat_open:
            self._relayout_stage_top()

    def show_bubble(self, message: str, duration_sec: float, force: bool = False) -> None:
        if self._sync_thinking_pinned or (self._sync_bubble_active and not force):
            return
        if not force and not self._settings.show_speech_bubble and not self._is_chat_open:
            return
        if not self.isVisible():
            return
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
        self._sync_thinking_visible = False
        self._show_non_chat_bubble(message)
        self._bubble_timer.start(max(200, int(round(duration_sec * 1000.0))))

    def _show_non_chat_bubble(
        self,
        message: str,
        *,
        from_sync: bool = False,
        preserve_scroll: bool = False,
    ) -> None:
        self._idle_speech_bubble_active = False
        already_showing_sync = (
            from_sync and self._sync_bubble_active and self._speech_bubble.isVisible()
        )
        self._sync_bubble_active = from_sync
        self._disable_chat_scroll_auto()
        if not self._chat_messages_panel.isHidden():
            self._chat_messages_panel.setVisible(False)
        if not already_showing_sync or self._bubble_text_lbl.isHidden():
            self._bubble_text_lbl.setStyleSheet(
                "color: #F6F3FF; font-size: 12.5px; padding: 1px 4px 2px 0px; background: transparent;"
            )
            self._bubble_text_lbl.setVisible(True)
        text_changed = self._bubble_text_lbl.text() != message
        if text_changed:
            bar = self._bubble_scroll.verticalScrollBar()
            saved_scroll = bar.value() if (already_showing_sync and preserve_scroll) else None
            self._bubble_text_lbl.setText(message)
            if already_showing_sync:
                # 同步消息在非聊天模式下沿用普通气泡自适应尺寸；续写保留滚动位置，新消息回到顶部。
                self._bubble_scroll.setWidgetResizable(True)
                self._relayout_stage_top()
                bar.setValue(
                    min(saved_scroll, bar.maximum())
                    if saved_scroll is not None
                    else bar.minimum()
                )
        elif already_showing_sync:
            self._relayout_stage_top()
            if not preserve_scroll:
                bar = self._bubble_scroll.verticalScrollBar()
                bar.setValue(bar.minimum())
        if not already_showing_sync:
            self._speech_bubble.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
            self._relayout_stage_top(reveal_bubble=True)
            bar = self._bubble_scroll.verticalScrollBar()
            bar.setValue(bar.minimum())

    def _restart_sync_bubble_timeout(self) -> None:
        if (
            self._sync_waiting_for_reply
            or self._ag_playback
            or self._ag_pending_replace is not None
            or self._speech_bubble.is_resizing()
        ):
            self._bubble_timer.stop()
        else:
            self._bubble_timer.start(_SYNC_BUBBLE_IDLE_MS)

    def _on_bubble_timeout(self) -> None:
        self._is_forced_bubble_active = False
        if self._is_chat_open or self._speech_bubble.is_resizing():
            return
        if self._sync_bubble_active:
            if (
                self._sync_waiting_for_reply
                or self._ag_playback
                or self._ag_pending_replace is not None
                or self._bubble_scroll.verticalScrollBar().isSliderDown()
            ):
                return
        elif self._is_bubble_hovered:
            return
        self._hide_speech_bubble()
        self._relayout_stage_top()

    def open_chat_mode(self) -> None:
        """从桌面/托盘菜单进入聊天模式：先保证桌宠可见，再展开聊天面板。"""
        if not self.isVisible():
            self.show()
        self.open_chat_dialog()
        self.raise_()
        self._speech_bubble.raise_()

    def open_chat_dialog(self) -> None:
        self._mark_pet_user_activity()
        self._idle_speech_bubble_active = False
        self._is_chat_open = True
        self._sync_thinking_pinned = False
        self._sync_thinking_visible = False
        self._sync_bubble_active = False
        self._sync_waiting_for_reply = False
        self._disable_chat_scroll_auto()
        if self._ag_mirror_active and self._ag_sync_session_id is None:
            self._ag_sync_session_id = f"sync:{uuid.uuid4().hex}"
            self._ag_sync_events.clear()
            if self._waiting_chat_workers:
                self._ag_pending_handoff = True
            else:
                self._show_sync_chat_session()
        elif self._ag_viewing_sync and self._ag_sync_session_id is not None:
            # 非聊天模式下收到新同步事件只记录数据不改聊天卡片，重新进入时按最新事件重绘。
            self._render_sync_chat_events()
        self._toggle_chat_history(True)
        # 未填写 Key 不会产生请求错误，进入聊天时也需要明确显示配置提示。
        active_label = format_api_provider_label(self._settings.api_provider)
        invalid_reason = "" if self._ag_viewing_sync else (
            f"尚未填写 {active_label} Key"
            if not self._settings.chat_offline_mode
            and not GeminiChatService.has_active_api_key(self._settings)
            else self._chat.get_api_error(self._settings)
        )
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
                # 保留当前提示文字，普通欢迎文字不应继承错误提示的红色。
                is_error_notice = self._bubble_text_lbl.text().startswith("⚠️ API Key 不可用")
                color = "#FCA5A5" if is_error_notice else "#F6F3FF"
                self._bubble_text_lbl.setStyleSheet(
                    f"color: {color}; font-size: 12.5px; padding: 1px 4px 2px 0px; background: transparent;"
                )
            elif not self._ag_viewing_sync and (
                not self._speech_bubble.isVisible()
                or self._bubble_text_lbl.text().startswith("⚠️ API Key 不可用")
            ):
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
        if not self._is_sync_working_locked():
            self._set_temporary_pose(PetState.LISTENING, 10.0)

    def set_speech_bubble_enabled(self, enabled: bool) -> None:
        self._mark_pet_user_activity()
        self._settings.show_speech_bubble = enabled
        self.apply_settings_to_window()
        if enabled:
            self.show_bubble("已开启宠物对话气泡，会自动陪你聊天喵✦", 4.0, force=True)
        elif not self._is_chat_open and not self._ag_mirror_active and not self._sync_bubble_active:
            self._sync_chat_controls()
            self._hide_speech_bubble()
            self._relayout_stage_top()

    def _on_menu_show_speech_bubble(self, checked: bool) -> None:
        self.set_speech_bubble_enabled(bool(checked))
        self._settings_service.save(self._settings)
        self.settings_synced.emit()

    def _close_bubble_click(self) -> None:
        self._mark_pet_user_activity()
        was_chat_open = self._is_chat_open
        was_sync_bubble = self._sync_bubble_active and not was_chat_open
        self._is_chat_open = False
        self._is_forced_bubble_active = False
        self._sync_chat_controls()
        self._hide_speech_bubble()
        if was_sync_bubble and self._sync_busy_active:
            # 用户手动关闭非聊天同步气泡时解除忙碌固定态，避免中断后的回合长期停在忙碌动作。
            self._clear_sync_pose_state(release_pose=True)
        self._relayout_stage_top()
        if was_chat_open and self._override_pose is None:
            self._restart_ai_timer()

    def _submit_chat(self) -> None:
        self._mark_pet_user_activity()
        if self._chat_readonly:
            self._chat_save_status.setText("Antigravity 实时同步中，输入已禁用喵")
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
        _, _, active_model = self._resolve_active_chat_channel_and_models()
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
        if not self._is_sync_working_locked():
            self._set_temporary_pose(PetState.THINKING, 90.0)
        worker.reply_ready.connect(self._on_chat_reply_ready)
        worker.finished.connect(lambda w=worker: self._cleanup_chat_worker(w))
        worker.start()

    def _cleanup_chat_worker(self, worker: _ChatWorker) -> None:
        self._waiting_chat_workers.discard(worker)
        if worker in self._active_chat_workers:
            self._active_chat_workers.remove(worker)
        worker.deleteLater()
        if self._ag_pending_handoff and not self._waiting_chat_workers:
            self._ag_pending_handoff = False
            if self._ag_mirror_active and self._is_chat_open:
                self._show_sync_chat_session()

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

        if not self._is_sync_working_locked():
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
            _, _, active_model = self._resolve_active_chat_channel_and_models()
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
        self._particle_system.spawn_star_burst(center_x, center_y, count)

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
        self._particle_system.spawn(glyph, color_hex, x, y, vx, vy, max_life)

    # ==================== Menu & Quick Action Slots ====================

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
        if doze:
            self._clear_sync_pose_state(release_pose=False)
        self._is_walking = False
        self._is_sleeping = False
        self._current_state = self._resolve_resting_state()
        if self._settings.docked_side != DockSide.NONE:
            self._override_pose = None
            self._override_is_autonomous = False
            self._override_is_sync = False
            self._pending_sulk_after_pose = False
            self._refresh_sprite_visual()
            return
        if not self._first_shown or not self.isVisible():
            if self._override_is_autonomous:
                self._override_pose = None
                self._override_is_autonomous = False
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
        pinned = not self._settings.auto_walk
        # 共用 QAction 的文案和勾选同时更新，两个菜单都能收到变更通知。
        self._act_pin_on_desktop.setChecked(pinned)
        status = "已固定" if pinned else "关闭后自由漫步"
        self._act_pin_on_desktop.setText(f"📌 固定位置（{status}）")
        states = (
            ("_act_doze", self._settings.doze_mode),
            ("_act_show_bubble", self._settings.show_speech_bubble),
            ("_act_sync_chat", self._settings.chat_sync_enabled),
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

        self._act_sync_chat.setToolTip(
            "已启用，等待可同步的 Antigravity 对话" if self._settings.chat_sync_enabled and not self._ag_mirror_active
            else "把 Antigravity 的新消息实时播报到气泡"
        )

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

    def _on_menu_runtime_log(self, checked: bool) -> None:
        self._set_runtime_log_enabled(checked)

    def sync_character_profile(self, profile: CharacterProfile) -> None:
        self.setWindowTitle(profile.name)
        self._bubble_char_name_lbl.setText(profile.name)
        self._mark_pet_user_activity()
        self._restart_ai_timer()

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
        self._update_sprite_transforms(0.0, snap=True)
        self._stage_canvas.update()
        self.show_bubble(msg, 4.2)

        self._sound.play(PetSoundEffect.SPARKLE)
        self._spawn_star_burst(self.width() * 0.5, self.height() - self._sprite_height_px() * 0.55, 8)
