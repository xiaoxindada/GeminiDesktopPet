"""Main translucent desktop pet window with 60fps animations, walk rigs, bubbles, and menus."""

from __future__ import annotations

import ctypes
import math
import os
import random
from dataclasses import dataclass
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
    QColor,
    QCursor,
    QFont,
    QGuiApplication,
    QMouseEvent,
    QPainter,
    QPixmap,
)
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from models import (
    COSTUME_OPTIONS,
    CharacterLore,
    CharacterProfile,
    DockSide,
    PetSettings,
    PetState,
    PetStyleMode,
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
from services.sprite_manager import SpriteManager
from services.win32_ipc import set_click_through_ex_style
from themes import STARLIGHT_MENU_QSS


# Win32 Z-order & extended style helpers to update topmost without recreating Qt HWND
_HWND_TOPMOST = -1
_HWND_NOTOPMOST = -2
_SWP_NOMOVE = 0x0002
_SWP_NOSIZE = 0x0001
_SWP_NOACTIVATE = 0x0010
_GWL_EXSTYLE = -20
_WS_EX_TOOLWINDOW = 0x00000080


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

    reply_ready = Signal(object)

    def __init__(
        self,
        chat: GeminiChatService,
        user_input: str,
        settings: PetSettings,
        metrics: SystemMetrics,
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        self._chat = chat
        self._user_input = user_input
        self._settings = settings
        self._metrics = metrics

    def run(self) -> None:
        try:
            reply = self._chat.ask(self._user_input, self._settings, self._metrics)
        except Exception as ex:
            reply = ChatReply(
                text=f"Gemini 刚才走神了喵：{ex}",
                suggested_state=PetState.OOPS,
            )
        self.reply_ready.emit(reply)


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
    auto_start_changed = Signal(bool)
    follow_antigravity_changed = Signal(bool)
    follow_gemini_app_changed = Signal(bool)
    settings_synced = Signal()
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
        self._last_monitor_bubble_utc = datetime.min.replace(tzinfo=timezone.utc)
        self._first_shown = False
        self._closing_handled = False
        self._active_chat_workers: list[_ChatWorker] = []

        # Build UI layers, menu, and timers
        self._ui_ready = False
        self._build_ui()
        self._build_context_menu()
        self._build_timers()
        self._ui_ready = True
        self._stage_top_panel.installEventFilter(self)
        self._speech_bubble.installEventFilter(self)
        self._chat_input.installEventFilter(self)

    # ==================== UI Construction ====================

    def _build_ui(self) -> None:
        self._stage_canvas = _PetStageCanvas(self)

        # Top stage container (SpeechBubble + QuickBar) overlaid in upper region
        self._stage_top_panel = QWidget(self)
        self._stage_top_panel.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self._stage_top_panel.setMouseTracking(True)

        top_layout = QVBoxLayout(self._stage_top_panel)
        top_layout.setContentsMargins(0, 0, 0, 0)
        top_layout.setSpacing(6)

        # --- Speech Bubble ---
        self._speech_bubble = QFrame(self._stage_top_panel)
        self._speech_bubble.setObjectName("SpeechBubble")
        self._speech_bubble.setMaximumWidth(288)
        self._speech_bubble.setMouseTracking(True)
        self._speech_bubble.setStyleSheet(
            """
            QFrame#SpeechBubble {
                background-color: rgba(23, 19, 45, 237);
                border: 1.5px solid #7C5CFF;
                border-radius: 14px;
            }
            """
        )

        bubble_layout = QVBoxLayout(self._speech_bubble)
        bubble_layout.setContentsMargins(12, 9, 12, 9)
        bubble_layout.setSpacing(4)

        header_row = QHBoxLayout()
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
        header_row.addStretch(1)

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
        bubble_layout.addLayout(header_row)

        self._bubble_scroll = QScrollArea(self._speech_bubble)
        self._bubble_scroll.setWidgetResizable(True)
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

        self._bubble_text_lbl = QLabel("喵～Gemini 上线！")
        self._bubble_text_lbl.setWordWrap(True)
        self._bubble_text_lbl.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        self._bubble_text_lbl.setStyleSheet(
            "color: #F6F3FF; font-size: 12.5px; background: transparent;"
        )
        self._bubble_scroll.setWidget(self._bubble_text_lbl)
        bubble_layout.addWidget(self._bubble_scroll)

        # Inline Chat Input Panel
        self._chat_input_panel = QWidget(self._speech_bubble)
        self._chat_input_panel.setStyleSheet("background: transparent;")
        chat_row = QHBoxLayout(self._chat_input_panel)
        chat_row.setContentsMargins(0, 4, 0, 0)
        chat_row.setSpacing(6)

        self._chat_input = QLineEdit(self._chat_input_panel)
        self._chat_input.setPlaceholderText("想问什么或打个指令试试…")
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
        bubble_layout.addWidget(self._chat_input_panel)

        self._speech_bubble.setVisible(False)
        top_layout.addWidget(self._speech_bubble, 0, Qt.AlignmentFlag.AlignHCenter)

        # --- Quick Hover Action Bar ---
        self._quick_bar = QFrame(self._stage_top_panel)
        self._quick_bar.setObjectName("QuickBar")
        self._quick_bar.setStyleSheet(
            """
            QFrame#QuickBar {
                background-color: rgba(26, 21, 52, 230);
                border: 1px solid #4D3D8A;
                border-radius: 12px;
            }
            QFrame#QuickBar QPushButton {
                background-color: #2D2454;
                color: #EFEAFF;
                border: 1px solid #544399;
                border-radius: 10px;
                padding: 4px 6px;
                font-size: 11px;
            }
            QFrame#QuickBar QPushButton:hover {
                background-color: #4B3A8F;
            }
            QFrame#QuickBar QPushButton:pressed {
                background-color: #6D4AFF;
            }
            """
        )
        qb_layout = QVBoxLayout(self._quick_bar)
        qb_layout.setContentsMargins(6, 5, 6, 5)
        qb_layout.setSpacing(4)

        def make_pill(text: str, tip: str, slot) -> QPushButton:
            btn = QPushButton(text, self._quick_bar)
            btn.setToolTip(tip)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
            btn.clicked.connect(slot)
            return btn

        row1 = QHBoxLayout()
        row1.setContentsMargins(0, 0, 0, 0)
        row1.setSpacing(4)
        row1.addStretch(1)
        self._quick_doze_btn = make_pill("💤 打盹模式", "开关打盹模式（原地睡觉待机）", self._toggle_doze_mode)
        row1.addWidget(self._quick_doze_btn)
        self._quick_bubble_btn = make_pill("💭 自动气泡", "开关自动弹出对话气泡", self._toggle_speech_bubble)
        row1.addWidget(self._quick_bubble_btn)
        self._quick_pin_btn = make_pill("📌 固定(不乱跑)", "固定在桌面不乱跑", self._toggle_pin_on_desktop)
        row1.addWidget(self._quick_pin_btn)
        row1.addStretch(1)
        qb_layout.addLayout(row1)

        row2 = QHBoxLayout()
        row2.setContentsMargins(0, 0, 0, 0)
        row2.setSpacing(4)
        row2.addStretch(1)
        row2.addWidget(make_pill("💬 对话", "打开对话输入", self._toggle_chat))
        self._quick_float_btn = make_pill("✨ 悬浮", "切换反重力悬浮", self._toggle_anti_gravity)
        row2.addWidget(self._quick_float_btn)
        row2.addWidget(make_pill("📖 档案", "查看角色设定与三视图", self.open_profile_requested.emit))
        self._quick_style_btn = make_pill("🎀 切换形象", "切换 Q 版 / 标准版形象", self.toggle_style_mode)
        row2.addWidget(self._quick_style_btn)
        row2.addStretch(1)
        qb_layout.addLayout(row2)

        row3 = QHBoxLayout()
        row3.setContentsMargins(0, 0, 0, 0)
        row3.setSpacing(4)
        row3.addStretch(1)
        row3.addWidget(make_pill("✏️ 角色设定", "可视化自定义角色设定", self.open_character_editor_requested.emit))
        self._quick_log_btn = make_pill("📝 运行日志", "开关运行日志记录（排错用）", self._toggle_runtime_log)
        row3.addWidget(self._quick_log_btn)
        row3.addWidget(make_pill("🆕 检查更新", "检查 GitHub Releases 更新与更新日志", self.open_update_requested.emit))
        row3.addStretch(1)
        qb_layout.addLayout(row3)

        self._quick_bar.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
        self._quick_bar.setVisible(False)
        top_layout.addWidget(self._quick_bar, 0, Qt.AlignmentFlag.AlignHCenter)
        self._top_layout = top_layout

    def _build_context_menu(self) -> None:
        menu = QMenu(self)
        menu.setStyleSheet(STARLIGHT_MENU_QSS)
        self._context_menu = menu

        act_pat = menu.addAction("♥ 摸摸猫耳")
        act_pat.triggered.connect(self.trigger_pat)

        act_chat = menu.addAction("💬 打开宠物对话框 / 问问题")
        act_chat.triggered.connect(self._toggle_chat)

        self._act_show_bubble = menu.addAction("💭 自动弹出对话气泡")
        self._act_show_bubble.setCheckable(True)
        self._act_show_bubble.triggered.connect(self._on_menu_show_speech_bubble)

        act_quota = menu.addAction("📊 查看反重力额度")
        act_quota.triggered.connect(self.open_quota_requested)

        menu.addSeparator()

        style_menu = menu.addMenu("🎀 形象切换")
        style_group = QActionGroup(style_menu)
        style_group.setExclusive(True)
        self._act_style_chibi = style_menu.addAction("Q 版猫娘形象 (Chibi · 默认)")
        self._act_style_chibi.setCheckable(True)
        self._act_style_chibi.setActionGroup(style_group)
        self._act_style_chibi.triggered.connect(lambda: self._set_style_mode(PetStyleMode.CHIBI))

        self._act_style_standard = style_menu.addAction("标准立绘形象 (Standard)")
        self._act_style_standard.setCheckable(True)
        self._act_style_standard.setActionGroup(style_group)
        self._act_style_standard.triggered.connect(lambda: self._set_style_mode(PetStyleMode.STANDARD))

        self._act_anti_gravity = menu.addAction("✨ 反重力悬浮模式")
        self._act_anti_gravity.setCheckable(True)
        self._act_anti_gravity.triggered.connect(self._toggle_anti_gravity)

        self._act_pin_on_desktop = menu.addAction("📌 固定在桌面（不乱跑）")
        self._act_pin_on_desktop.setCheckable(True)
        self._act_pin_on_desktop.triggered.connect(self._on_menu_pin_on_desktop)

        self._act_doze = menu.addAction("💤 打盹模式（原地睡觉待机）")
        self._act_doze.setCheckable(True)
        self._act_doze.triggered.connect(self._on_menu_doze_mode)

        menu.aboutToShow.connect(self._sync_auto_start_action)

        self._act_auto_start = menu.addAction("⚡ Windows 开机自动启动")
        self._act_auto_start.setCheckable(True)
        self._act_auto_start.triggered.connect(self._on_menu_auto_start)

        self._act_follow_ag = menu.addAction("🚀 跟随 Antigravity 启动而启动")
        self._act_follow_ag.setCheckable(True)
        self._act_follow_ag.triggered.connect(self._on_menu_follow_antigravity)

        self._act_follow_gemini = menu.addAction("🌟 跟随 Gemini 桌面 App 启动而启动")
        self._act_follow_gemini.setCheckable(True)
        self._act_follow_gemini.triggered.connect(self._on_menu_follow_gemini_app)

        pose_menu = menu.addMenu("🎭 动作表现")
        poses = [
            ("思考中 (thinking)", "thinking"),
            ("Nano Banana 画笔 (drawing)", "drawing"),
            ("哼小曲 (hum)", "hum"),
            ("伸懒腰 (stretch)", "stretch"),
            ("打哈欠 (yawn)", "yawn"),
            ("打盹睡觉 (sleeping)", "sleeping"),
            ("反重力飘浮 (float)", "float"),
            ("得意叉腰 (proud)", "proud"),
            ("超开心 (happy)", "happy"),
            ("压缩上下文 (compact)", "compact"),
        ]
        for label, key in poses:
            act = pose_menu.addAction(label)
            act.triggered.connect(lambda _checked=False, k=key: self.trigger_external_sprite_preview(k))

        self._costume_menu = menu.addMenu("🎊 节日换装")
        self._costume_actions: list[tuple[str, Optional[PetState], QAction]] = []
        costume_group = QActionGroup(self._costume_menu)
        costume_group.setExclusive(True)
        for opt in COSTUME_OPTIONS:
            act = self._costume_menu.addAction(opt.label)
            act.setCheckable(True)
            act.setActionGroup(costume_group)
            act.setChecked(self._settings.costume_override.lower() == opt.id.lower())
            act.triggered.connect(
                lambda _checked=False, cid=opt.id, cst=opt.state: self._on_costume_picked(cid, cst)
            )
            self._costume_actions.append((opt.id, opt.state, act))

        menu.addSeparator()

        act_profile = menu.addAction("📖 角色档案与三视图")
        act_profile.triggered.connect(self.open_profile_requested)

        act_editor = menu.addAction("✏️ 自定义角色设定...")
        act_editor.triggered.connect(self.open_character_editor_requested)

        menu.addSeparator()

        scale_menu = menu.addMenu("📏 缩放尺寸")
        for lbl, val in [
            ("小巧 (0.75x)", 0.75),
            ("标准 (1.00x)", 1.0),
            ("放大 (1.30x)", 1.3),
            ("超大 (1.60x)", 1.6),
        ]:
            act = scale_menu.addAction(lbl)
            act.triggered.connect(lambda _checked=False, sc=val: self._on_menu_scale(sc))

        dock_menu = menu.addMenu("📍 贴靠屏幕边缘")
        act_dock_l = dock_menu.addAction("贴靠左边偷看")
        act_dock_l.triggered.connect(lambda: self._on_menu_dock("left"))
        act_dock_r = dock_menu.addAction("贴靠右边偷看")
        act_dock_r.triggered.connect(lambda: self._on_menu_dock("right"))
        act_dock_reset = dock_menu.addAction("回到地面右下角")
        act_dock_reset.triggered.connect(lambda: self._on_menu_dock("reset"))

        act_ct = menu.addAction("👻 开启鼠标穿透（托盘可取消）")
        act_ct.triggered.connect(self._on_menu_click_through)

        act_settings = menu.addAction("⚙️ 偏好设置...")
        act_settings.triggered.connect(self.open_settings_requested)

        self._act_runtime_log = menu.addAction("📝 记录运行日志（排错用）")
        self._act_runtime_log.setCheckable(True)
        self._act_runtime_log.triggered.connect(self._on_menu_runtime_log)

        act_update = menu.addAction("🆕 检查更新...")
        act_update.triggered.connect(self.open_update_requested)

        menu.addSeparator()

        act_exit = menu.addAction("🌙 让 Gemini 娘休息（退出）")
        act_exit.triggered.connect(self.exit_requested)

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
                greet_state = (
                    CharacterLore.detect_festival_state(now)
                    or CharacterLore.get_time_greeting_state(now)
                )
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
        super().closeEvent(event)

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if not getattr(self, "_ui_ready", False):
            return super().eventFilter(watched, event)
        etype = event.type()
        if watched is self._speech_bubble:
            if etype == QEvent.Type.Enter:
                self._is_bubble_hovered = True
                self._on_hover_entered()
            elif etype == QEvent.Type.Leave:
                self._is_bubble_hovered = False
                if not self._is_chat_open and self._speech_bubble.isVisible():
                    self._bubble_timer.stop()
                    self._bubble_timer.start(2500)
                self._schedule_hover_leave_check()
        elif watched is self._stage_top_panel:
            if etype == QEvent.Type.Enter:
                self._on_hover_entered()
            elif etype == QEvent.Type.Leave:
                self._schedule_hover_leave_check()
        elif watched is self._chat_input and etype == QEvent.Type.KeyPress:
            if getattr(event, "key", lambda: None)() == Qt.Key.Key_Escape:
                self._is_chat_open = False
                self._chat_input_panel.setVisible(False)
                self._speech_bubble.setVisible(False)
                self._relayout_stage_top()
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
        self._quick_bar.setVisible(True)
        self._relayout_stage_top()
        if self._settings.docked_side != DockSide.NONE and self._override_pose is None:
            self._refresh_sprite_visual()

    def _schedule_hover_leave_check(self) -> None:
        # 延迟收起 QuickBar 并检查光标是否真正离开窗口与浮层区域
        self._hover_grace_timer.stop()
        self._hover_grace_timer.start()

    def _on_hover_grace_timeout(self) -> None:
        global_pos = QCursor.pos()
        local_pos = self.mapFromGlobal(global_pos)
        # 仅将角色精灵区、可见快捷栏、气泡和右键菜单视为悬浮命中区，避免透明留白让快捷栏卡死不收起
        sprite_hit = self._get_local_sprite_rect().adjusted(-8.0, -8.0, 8.0, 8.0).contains(
            QPointF(local_pos)
        )
        still_inside = sprite_hit or self._context_menu.isVisible()
        if not still_inside and self._quick_bar.isVisible():
            qb_rect = QRect(self._quick_bar.mapTo(self, QPoint(0, 0)), self._quick_bar.size()).adjusted(
                -6, -6, 6, 6
            )
            still_inside = qb_rect.contains(local_pos)
        if not still_inside and self._speech_bubble.isVisible():
            sb_rect = QRect(self._speech_bubble.mapTo(self, QPoint(0, 0)), self._speech_bubble.size()).adjusted(
                -6, -6, 6, 6
            )
            still_inside = sb_rect.contains(local_pos)
        if not still_inside:
            self._is_hovering = False
            if not self._is_chat_open and not self._is_bubble_hovered:
                self._quick_bar.setVisible(False)
                self._relayout_stage_top()
            if self._settings.docked_side != DockSide.NONE and self._override_pose is None:
                self._refresh_sprite_visual()

    # ==================== Settings & Layout Sync ====================

    def _sprite_height_px(self) -> float:
        s = max(0.5, min(2.0, float(self._settings.scale)))
        base_h = 300.0 if self._settings.style_mode == PetStyleMode.STANDARD else 240.0
        return base_h * s

    def _chibi_rig_size(self) -> tuple[float, float]:
        s = max(0.5, min(2.0, float(self._settings.scale)))
        h = 240.0 * s
        return (h * (555.0 / 640.0), h)

    def _std_rig_size(self) -> tuple[float, float]:
        s = max(0.5, min(2.0, float(self._settings.scale)))
        h = 300.0 * s
        return (h * (337.0 / 977.0), h)

    def apply_settings_to_window(self) -> None:
        old_bottom = float(self.y() + self.height()) if self._first_shown else None

        s = max(0.5, min(2.0, float(self._settings.scale)))
        sprite_h = self._sprite_height_px()
        self.setWindowOpacity(max(0.35, min(1.0, float(self._settings.opacity))))
        self._sound.enabled = bool(self._settings.sound_enabled)

        pinned = not self._settings.auto_walk
        if pinned and self._is_walking:
            self._is_walking = False

        # 先同步快捷栏文案再测算最小窗口宽度，确保任何缩放倍率/字体下快捷栏均不裁剪
        self._act_style_chibi.setChecked(self._settings.style_mode == PetStyleMode.CHIBI)
        self._act_style_standard.setChecked(self._settings.style_mode == PetStyleMode.STANDARD)
        self._act_anti_gravity.setChecked(self._settings.anti_gravity_mode)
        self._act_pin_on_desktop.setChecked(pinned)
        self._act_doze.setChecked(self._settings.doze_mode)
        self._apply_doze_transition()
        self._act_show_bubble.setChecked(self._settings.show_speech_bubble)
        self._sync_auto_start_action()
        self._act_follow_ag.setChecked(self._settings.follow_antigravity)
        self._act_follow_gemini.setChecked(self._settings.follow_gemini_app)
        self._act_runtime_log.setChecked(self._settings.enable_runtime_log)
        for cid, _cst, act in self._costume_actions:
            act.setChecked(cid.lower() == self._settings.costume_override.lower())

        self._quick_bubble_btn.setText("💭 气泡已开" if self._settings.show_speech_bubble else "💭 自动气泡")
        self._quick_bubble_btn.setToolTip(
            "当前已开启自动弹出对话气泡（点击关闭）"
            if self._settings.show_speech_bubble
            else "当前已关闭自动弹出对话气泡（点击开启）"
        )
        self._quick_style_btn.setText("🎀 切换形象")
        self._quick_style_btn.setToolTip(
            "当前：标准立绘版（点击切换至 Q 版）"
            if self._settings.style_mode == PetStyleMode.STANDARD
            else "当前：Q 版猫娘（点击切换至标准版）"
        )
        self._quick_float_btn.setText("🌟 已悬浮" if self._settings.anti_gravity_mode else "✨ 悬浮")
        self._quick_pin_btn.setText("📌 已固定" if pinned else "📌 固定(不乱跑)")
        self._quick_pin_btn.setToolTip("已固定在桌面（点击恢复漫步）" if pinned else "固定在桌面不乱跑")
        self._quick_doze_btn.setText("💤 打盹中" if self._settings.doze_mode else "💤 打盹模式")
        self._quick_doze_btn.setToolTip(
            "当前正在打盹待机（点击唤醒）"
            if self._settings.doze_mode
            else "开启打盹模式，原地睡觉待机"
        )
        self._quick_log_btn.setText("📝 日志已开" if self._settings.enable_runtime_log else "📝 运行日志")
        daily_name = datetime.now().strftime("%Y-%m-%d.log")
        log_file = os.path.join(AppLogger.log_directory(), daily_name)
        self._quick_log_btn.setToolTip(
            f"当前已开启运行日志，写入 {log_file}（点击关闭）"
            if self._settings.enable_runtime_log
            else "当前已关闭运行日志记录（点击开启排错日志）"
        )

        self._quick_bar.ensurePolished()
        if self._quick_bar.layout() is not None:
            self._quick_bar.layout().activate()
        quick_min_w = self._quick_bar.sizeHint().width() + 24
        new_w = max(360, int(round(380.0 * s)), quick_min_w)
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
        ):
            self._bubble_timer.stop()
            self._speech_bubble.setVisible(False)

        self._update_dock_alignment()
        self._apply_win32_extended_styles()
        self._refresh_sprite_visual()
        self._relayout_stage_top()
        self._stage_canvas.update()

    def _apply_win32_extended_styles(self) -> None:
        hwnd = int(self.winId())
        if not hwnd:
            return
        _ensure_tool_window_ex_style(hwnd)
        _apply_win32_topmost(hwnd, bool(self._settings.always_on_top))
        set_click_through_ex_style(hwnd, bool(self._settings.click_through))
        self.visual_bounds_changed.emit()

    def _update_dock_alignment(self) -> None:
        if self._settings.docked_side == DockSide.LEFT:
            align = Qt.AlignmentFlag.AlignLeft
        elif self._settings.docked_side == DockSide.RIGHT:
            align = Qt.AlignmentFlag.AlignRight
        else:
            align = Qt.AlignmentFlag.AlignHCenter

        self._top_layout.setAlignment(self._speech_bubble, align)
        self._top_layout.setAlignment(self._quick_bar, align)
        self._relayout_stage_top()

    def _relayout_stage_top(self) -> None:
        w = self.width()
        h = self.height()
        sprite_h = self._sprite_height_px()
        avail_h = max(80, int(h - sprite_h - 4))
        max_top_w = max(240, w - 12)

        self._quick_bar.adjustSize()
        bubble_max_w = min(288, max_top_w)
        self._speech_bubble.setMaximumWidth(bubble_max_w)
        quick_h = self._quick_bar.sizeHint().height() + 8 if self._quick_bar.isVisible() else 0
        chat_h = self._chat_input_panel.sizeHint().height() + 6 if self._chat_input_panel.isVisible() else 0
        # 限制气泡文字滚动区高度，并将打开的输入框高度纳入预算，防止挤压底部角色区域
        max_scroll_h = max(28, avail_h - quick_h - chat_h - 48)
        label_w = max(120, min(260, bubble_max_w - 28))
        self._bubble_text_lbl.setMaximumWidth(label_w)
        text_h = (
            self._bubble_text_lbl.heightForWidth(label_w)
            if self._bubble_text_lbl.hasHeightForWidth()
            else self._bubble_text_lbl.sizeHint().height()
        )
        self._bubble_scroll.setFixedHeight(min(max(22, text_h + 2), max_scroll_h))

        self._stage_top_panel.setFixedWidth(max_top_w)
        self._top_layout.activate()
        self._stage_top_panel.adjustSize()
        top_h = self._stage_top_panel.height()
        if self._settings.docked_side == DockSide.LEFT:
            top_x = 6
        elif self._settings.docked_side == DockSide.RIGHT:
            top_x = max(6, w - self._stage_top_panel.width() - 6)
        else:
            top_x = (w - self._stage_top_panel.width()) // 2
        top_y = max(2, int(round(h - sprite_h - 2 - top_h)))
        self._stage_top_panel.move(top_x, top_y)
        self._stage_top_panel.raise_()
        self.visual_bounds_changed.emit()

    def _on_costume_picked(self, costume_id: str, state: Optional[PetState]) -> None:
        self._settings.costume_override = costume_id
        self._settings.auto_festival_costume = costume_id == "auto"
        for cid, _cst, act in self._costume_actions:
            act.setChecked(cid == costume_id)
        self._settings_service.save(self._settings)
        self.settings_synced.emit()

        state_to_show = (
            state
            or CharacterLore.detect_festival_state(datetime.now())
            or PetState.IDLE
        )
        self._set_temporary_pose(
            state_to_show,
            5.0,
            CharacterLore.pick_line_for_state(state_to_show),
        )
        self._spawn_star_burst(self.width() * 0.5, self.height() - self._sprite_height_px() * 0.55, 7)

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
        area = self._get_work_area(self.x() + self.width() * 0.5, self.y() + self.height() * 0.5)
        new_x = area.right() + 1 - self.width() - 28
        new_y = area.bottom() + 1 - self.height()
        self.move(int(round(new_x)), int(round(new_y)))
        self._update_dock_alignment()
        self._settings_service.save(self._settings)

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
            pw = float(self._active_static_pixmap.width())
            ph = float(self._active_static_pixmap.height())
            rh = sprite_h
            rw = rh * (pw / ph) if ph > 0 else rh * 0.85
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
        """返回角色精灵与当前可见快捷栏、气泡的独立全局矩形，避免合框误占空位。"""
        rects = [self.get_visual_sprite_bounds()]
        for overlay in (self._quick_bar, self._speech_bubble):
            if overlay.isVisible() and overlay.width() > 0 and overlay.height() > 0:
                rects.append(QRect(overlay.mapToGlobal(QPoint(0, 0)), overlay.size()))
        return rects

    def get_visual_ui_bounds(self) -> QRect:
        """返回包含角色精灵及当前可见快捷栏、气泡的全局可视区域包围盒。"""
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
        show_quick_bar: bool = False,
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

        self._quick_bar.setVisible(show_quick_bar)
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

        if self._override_pose is not None and now >= self._override_until_utc:
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
            sprite_half = sprite_rect.width() * 0.5 if sprite_rect.width() > 10 else 85.0 * self._settings.scale
            min_left = area.left() - (self.width() * 0.5 - sprite_half) + 8.0
            max_left = (area.right() + 1) - (self.width() * 0.5 + sprite_half) - 8.0

            speed = 72.0 * self._settings.scale
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
        s = max(0.5, min(2.0, float(self._settings.scale)))

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
            rig = self._sprites.walk_rig
            if self._settings.style_mode == PetStyleMode.STANDARD and rig.std_body is not None:
                p = self._anim_time * 6.8
                self._update_std_walk_rig(p, s)
            elif rig.chibi_body is not None:
                p = self._anim_time * 7.6
                self._update_chibi_walk_rig(p, s)
            else:
                ty = -abs(math.sin(self._anim_time * 11.0)) * 7.0
                angle = math.sin(self._anim_time * 11.0) * 2.8 * self._walk_dir
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
        elif self._is_sleeping or self._is_dozing():
            sy = 1.0 + 0.024 * math.sin(self._anim_time * 1.4)
            if self._settings.particles_enabled and random.random() < 0.03:
                self._spawn_particle(
                    "Zzz",
                    "#93C5FD",
                    self.width() * 0.58,
                    self.height() - self._sprite_height_px() * 0.78,
                    12.0,
                    -22.0,
                    1.8,
                )
        else:
            sy = 1.0 + 0.014 * math.sin(self._anim_time * 2.4)
            sx = 1.0 - 0.006 * math.sin(self._anim_time * 2.4)

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

        # 非穿透模式下在角色有效区域填充 alpha=1 底色，保证发丝镂空区也可正常拖拽
        if not self._settings.click_through:
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
            _draw_pixmap_uniform(painter, self._active_static_pixmap, sprite_rect)

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
        rig = self._sprites.walk_rig
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
        rig = self._sprites.walk_rig
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

        if rig.std_body is not None:
            painter.save()
            painter.translate(rx + 0.5 * rw, ry + 0.46 * rh + k["body_ty"])
            painter.rotate(k["body_rot"])
            painter.translate(-0.5 * rw, -0.46 * rh)
            _draw_pixmap_uniform(painter, rig.std_body, local_rect)
            painter.restore()

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

        use_walk_rig = (
            self._is_walking
            and self._override_pose is None
            and not self._is_dragging
            and self._settings.docked_side == DockSide.NONE
        )
        rig = self._sprites.walk_rig

        if use_walk_rig and self._settings.style_mode == PetStyleMode.STANDARD and rig.std_body is not None:
            self._use_std_walk_rig = True
            self._use_chibi_walk_rig = False
            self._active_static_pixmap = None
            return

        if use_walk_rig and self._settings.style_mode == PetStyleMode.CHIBI and rig.chibi_body is not None:
            self._use_std_walk_rig = False
            self._use_chibi_walk_rig = True
            self._active_static_pixmap = None
            return

        self._use_std_walk_rig = False
        self._use_chibi_walk_rig = False
        self._active_static_pixmap = self._sprites.get(active, self._settings.style_mode)

    def _resolve_resting_state(self) -> PetState:
        if self._settings.docked_side == DockSide.LEFT:
            return PetState.EDGE_LEFT_PEEK if self._is_hovering else PetState.EDGE_LEFT
        if self._settings.docked_side == DockSide.RIGHT:
            return PetState.EDGE_RIGHT_PEEK if self._is_hovering else PetState.EDGE_RIGHT
        if self._settings.doze_mode:
            return PetState.SLEEPING
        if self._settings.anti_gravity_mode:
            return PetState.FLOAT

        if self._settings.costume_override.lower() != "auto":
            for opt in COSTUME_OPTIONS:
                if opt.id == self._settings.costume_override and opt.state is not None:
                    return opt.state
        elif self._settings.auto_festival_costume:
            fest = CharacterLore.detect_festival_state(datetime.now())
            if fest is not None:
                return fest

        return PetState.IDLE

    def _set_temporary_pose(
        self,
        state: PetState,
        duration_sec: float,
        bubble_message: Optional[str] = None,
        force_bubble: bool = False,
    ) -> None:
        self._is_walking = False
        self._is_sleeping = state == PetState.SLEEPING
        self._override_pose = state
        self._override_until_utc = datetime.now(timezone.utc) + timedelta(seconds=duration_sec)
        self._refresh_sprite_visual()
        self._stage_canvas.update()

        if bubble_message and bubble_message.strip():
            self.show_bubble(bubble_message, duration_sec, force=force_bubble)

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

        if self._is_sulk:
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
        sprite_w = sprite_rect.width() if sprite_rect.width() > 10 else 180.0 * self._settings.scale
        sprite_left = self.x() + (self.width() - sprite_w) * 0.5
        sprite_right = sprite_left + sprite_w
        snap_margin = 38.0 * max(0.5, min(2.0, float(self._settings.scale)))
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

    def show_bubble(self, message: str, duration_sec: float, force: bool = False) -> None:
        if not force and not self._settings.show_speech_bubble and not self._is_chat_open:
            return

        self._is_forced_bubble_active = force
        self._bubble_text_lbl.setText(message)
        self._speech_bubble.setVisible(True)
        self._relayout_stage_top()
        self._bubble_timer.stop()
        self._bubble_timer.start(max(200, int(round(duration_sec * 1000.0))))

    def _on_bubble_timeout(self) -> None:
        self._is_forced_bubble_active = False
        if not self._is_chat_open and not self._is_bubble_hovered:
            self._speech_bubble.setVisible(False)
            self._relayout_stage_top()

    def open_chat_dialog(self) -> None:
        self._is_chat_open = True
        if not self._speech_bubble.isVisible():
            self._bubble_text_lbl.setText("想聊什么呀喵？输入文字或指令试试✦")
        self._speech_bubble.setVisible(True)
        self._chat_input_panel.setVisible(True)
        self._relayout_stage_top()
        self._bubble_timer.stop()
        self.activateWindow()
        self.raise_()
        self._chat_input.setFocus()
        self._set_temporary_pose(PetState.LISTENING, 10.0)

    def set_speech_bubble_enabled(self, enabled: bool) -> None:
        self._settings.show_speech_bubble = enabled
        self.apply_settings_to_window()
        if enabled:
            self.show_bubble("已开启宠物对话气泡，会自动陪你聊天喵✦", 4.0, force=True)
        elif not self._is_chat_open:
            self._chat_input_panel.setVisible(False)
            self._speech_bubble.setVisible(False)
            self._bubble_timer.stop()
            self._relayout_stage_top()

    def _toggle_chat(self) -> None:
        if not self._is_chat_open:
            self.open_chat_dialog()
            return
        self._is_chat_open = False
        self._chat_input_panel.setVisible(False)
        if self._settings.show_speech_bubble:
            self._bubble_timer.stop()
            self._bubble_timer.start(3000)
        else:
            self._bubble_timer.stop()
            self._speech_bubble.setVisible(False)
        self._relayout_stage_top()

    def _close_bubble_click(self) -> None:
        self._is_chat_open = False
        self._is_forced_bubble_active = False
        self._chat_input_panel.setVisible(False)
        self._speech_bubble.setVisible(False)
        self._bubble_timer.stop()
        self._relayout_stage_top()

    def _submit_chat(self) -> None:
        text = self._chat_input.text().strip()
        if not text:
            return
        self._chat_input.clear()
        self._set_temporary_pose(PetState.THINKING, 15.0, "让我想想喵…✦")

        metrics = self._sys_monitor.sample()
        worker = _ChatWorker(self._chat, text, self._settings, metrics, parent=self)
        self._active_chat_workers.append(worker)
        worker.reply_ready.connect(self._on_chat_reply_ready)
        worker.finished.connect(lambda w=worker: self._cleanup_chat_worker(w))
        worker.start()

    def _cleanup_chat_worker(self, worker: _ChatWorker) -> None:
        if worker in self._active_chat_workers:
            self._active_chat_workers.remove(worker)
        worker.deleteLater()

    def _on_chat_reply_ready(self, reply: ChatReply) -> None:
        target_pose = reply.suggested_state or PetState.HAPPY
        if target_pose == PetState.FLOAT and not self._settings.anti_gravity_mode:
            self._settings.anti_gravity_mode = True
            self.apply_settings_to_window()
            self.settings_synced.emit()
        elif target_pose == PetState.PAT:
            self.trigger_pat()

        self._set_temporary_pose(target_pose, 7.0, reply.text)
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

    def _toggle_speech_bubble(self) -> None:
        self.set_speech_bubble_enabled(not self._settings.show_speech_bubble)
        self._settings_service.save(self._settings)
        self.settings_synced.emit()

    def _on_menu_show_speech_bubble(self, checked: bool) -> None:
        self.set_speech_bubble_enabled(checked)
        self._settings_service.save(self._settings)
        self.settings_synced.emit()

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

    def _toggle_pin_on_desktop(self) -> None:
        next_pinned = self._settings.auto_walk
        self._set_pin_on_desktop_enabled(next_pinned)
        self.show_bubble(
            "已开启「固定在桌面」～乖乖待在原地陪你喵📌"
            if next_pinned
            else "已关闭「固定在桌面」，恢复桌面自由漫步喵～✦",
            3.0,
        )

    def _on_menu_pin_on_desktop(self, checked: bool) -> None:
        self._set_pin_on_desktop_enabled(checked)

    def _is_dozing(self) -> bool:
        """打盹模式下处于常态睡姿（无临时动作、未贴边）时返回 True，用于呼吸与 Zzz 特效。"""
        return (
            bool(self._settings.doze_mode)
            and self._override_pose is None
            and self._settings.docked_side == DockSide.NONE
        )

    def _apply_doze_transition(self) -> None:
        """打盹模式开关变化时播放入睡/睡醒过渡；菜单、托盘与偏好设置的改动都会经过这里。"""
        doze = bool(self._settings.doze_mode)
        if doze == self._doze_applied:
            return
        self._doze_applied = doze
        self._is_walking = False
        self._is_sleeping = False
        self._current_state = self._resolve_resting_state()
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
        self.set_doze_mode_enabled(checked)

    def _toggle_doze_mode(self) -> None:
        self.set_doze_mode_enabled(not self._settings.doze_mode)

    def _on_menu_scale(self, scale: float) -> None:
        old_bottom = float(self.y() + self.height())
        self._settings.scale = scale
        self.apply_settings_to_window()
        if self._settings.docked_side != DockSide.NONE:
            self._reanchor_docked_position()
        else:
            self.move(self.x(), int(round(old_bottom - self.height())))
            self._clamp_inside_nearest_work_area()
        self._settings_service.save(self._settings)
        self.settings_synced.emit()

    def _on_menu_dock(self, tag: str) -> None:
        area = self._get_work_area(self.x() + self.width() * 0.5, self.y() + self.height() * 0.5)
        if tag == "left":
            self._settings.docked_side = DockSide.LEFT
            self._settings.anti_gravity_mode = False
            self.move(area.left(), int(round(area.top() + (area.height() - self.height()) * 0.55)))
            self._update_dock_alignment()
            self._set_temporary_pose(PetState.EDGE_LEFT_PEEK, 4.0, CharacterLore.pick_line("edge"))
        elif tag == "right":
            self._settings.docked_side = DockSide.RIGHT
            self._settings.anti_gravity_mode = False
            self.move(
                area.right() + 1 - self.width(),
                int(round(area.top() + (area.height() - self.height()) * 0.55)),
            )
            self._update_dock_alignment()
            self._set_temporary_pose(PetState.EDGE_RIGHT_PEEK, 4.0, CharacterLore.pick_line("edge"))
        elif tag == "reset":
            self.move_to_bottom_right_corner()
            self.apply_settings_to_window()
            self.show_and_greet()
            return

        self._settings_service.save(self._settings)
        self.settings_synced.emit()

    def _on_menu_click_through(self) -> None:
        self._settings.click_through = True
        self._settings_service.save(self._settings)
        self.apply_settings_to_window()
        self.settings_synced.emit()
        self.show_bubble("已开启鼠标穿透！可在右下角托盘图标右键取消喵～", 4.5)

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
        if self._settings.docked_side != DockSide.NONE:
            self._reanchor_docked_position()
            dock_pose = (
                PetState.EDGE_LEFT_PEEK
                if self._settings.docked_side == DockSide.LEFT
                else PetState.EDGE_RIGHT_PEEK
            )
            self._set_temporary_pose(dock_pose, 4.2, msg)
        else:
            self._set_temporary_pose(PetState.HAPPY, 4.2, msg)

        self._sound.play(PetSoundEffect.SPARKLE)
        self._spawn_star_burst(self.width() * 0.5, self.height() - self._sprite_height_px() * 0.55, 8)

    def _sync_auto_start_action(self) -> None:
        self._act_auto_start.blockSignals(True)
        try:
            self._act_auto_start.setChecked(SettingsService.is_startup_enabled())
        finally:
            self._act_auto_start.blockSignals(False)

    def _on_menu_auto_start(self, checked: bool) -> None:
        self.auto_start_changed.emit(bool(checked))

    def _on_menu_follow_antigravity(self, checked: bool) -> None:
        self.follow_antigravity_changed.emit(checked)
        self.show_bubble(
            "已开启「跟随 Antigravity 启动而启动」(原生守护已上线) 喵✦"
            if checked
            else "已关闭跟随 Antigravity 启动喵～",
            4.0,
        )

    def _on_menu_follow_gemini_app(self, checked: bool) -> None:
        self.follow_gemini_app_changed.emit(checked)
        self.show_bubble(
            "已开启「跟随 Gemini 桌面 App 启动而启动」(原生守护已上线) 喵✦"
            if checked
            else "已关闭跟随 Gemini 桌面 App 启动喵～",
            4.0,
        )
