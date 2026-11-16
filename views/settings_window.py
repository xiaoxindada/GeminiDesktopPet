"""偏好设置窗口：外观、物理行为、应用联动守护、Gemini API、更新源与运行日志管理。"""

from __future__ import annotations

import os
from typing import Callable, Optional

from PySide6.QtCore import Qt, QThread, QTimer, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from models.app_version import AppVersion
from models.character_lore import COSTUME_OPTIONS
from models.pet_settings import (
    DEFAULT_GEMINI_MODEL,
    THINKING_LEVEL_OPTIONS,
    PetSettings,
    PetStyleMode,
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
from themes.starlight_theme import STARLIGHT_WINDOW_QSS


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


class SettingsWindow(QWidget):
    """手动确认保存全部 user_config.json 与跟随联动的偏好设置面板。"""

    def __init__(
        self,
        settings: PetSettings,
        settings_service: SettingsService,
        app_follow_service: AppFollowService,
        on_settings_applied: Callable[[], None],
        on_open_profile_editor: Optional[Callable[[], None]] = None,
        on_open_update_center: Optional[Callable[[], None]] = None,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._settings = settings
        self._settings_service = settings_service
        self._follow_service = app_follow_service
        self._on_applied = on_settings_applied
        self._on_open_editor = on_open_profile_editor
        self._on_open_update = on_open_update_center
        self._models_refresh_worker: Optional[_ModelsRefreshWorker] = None
        self._draft_gemini_model: str = DEFAULT_GEMINI_MODEL
        self._loading = True
        self._self_updating = False
        self._dirty = False

        self.setWindowTitle("⚙️ Gemini 娘 · 偏好设置")
        self.setWindowIcon(TrayIconService.load_app_icon())
        self.resize(510, 760)
        self.setMinimumSize(460, 580)
        self.setStyleSheet(STARLIGHT_WINDOW_QSS)

        self._build_ui()
        self.sync_from_settings(force=True)
        self._loading = False

        self._status_timer = QTimer(self)
        self._status_timer.setInterval(2500)
        self._status_timer.timeout.connect(self._refresh_follow_badges)
        self._status_timer.start()
        self._refresh_follow_badges()

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        container = QWidget()
        root = QVBoxLayout(container)
        root.setContentsMargins(20, 18, 20, 20)
        root.setSpacing(12)

        title = QLabel("✦ 桌面宠偏好设置")
        title.setStyleSheet("font-size: 20px; font-weight: bold; color: #F4F2FF; background: transparent;")
        root.addWidget(title)

        # Appearance card
        c_app = self._make_card(root, "形象与外观")
        self._style_combo = QComboBox()
        self._style_combo.addItem("Q 版猫娘形象 (Chibi · 可爱)", PetStyleMode.CHIBI)
        self._style_combo.addItem("标准立绘形象 (Standard · 比例还原)", PetStyleMode.STANDARD)
        self._style_combo.currentIndexChanged.connect(self._on_any_changed)
        self._add_labeled_row(c_app, "形象版本", self._style_combo)

        self._scale_slider = QSlider(Qt.Orientation.Horizontal)
        self._scale_slider.setRange(60, 180)
        self._scale_slider.setSingleStep(5)
        self._scale_val_lbl = QLabel("1.00x")
        self._scale_val_lbl.setFixedWidth(48)
        self._scale_val_lbl.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self._scale_val_lbl.setStyleSheet("color: #A78BFA; background: transparent;")
        self._scale_slider.valueChanged.connect(self._on_any_changed)
        self._add_slider_row(c_app, "人物缩放", self._scale_slider, self._scale_val_lbl)

        self._opacity_slider = QSlider(Qt.Orientation.Horizontal)
        self._opacity_slider.setRange(40, 100)
        self._opacity_slider.setSingleStep(5)
        self._opacity_val_lbl = QLabel("100%")
        self._opacity_val_lbl.setFixedWidth(48)
        self._opacity_val_lbl.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self._opacity_val_lbl.setStyleSheet("color: #A78BFA; background: transparent;")
        self._opacity_slider.valueChanged.connect(self._on_any_changed)
        self._add_slider_row(c_app, "透明度", self._opacity_slider, self._opacity_val_lbl)

        self._costume_combo = QComboBox()
        for opt in COSTUME_OPTIONS:
            self._costume_combo.addItem(opt.label, opt.id)
        self._costume_combo.currentIndexChanged.connect(self._on_any_changed)
        self._add_labeled_row(c_app, "装扮主题", self._costume_combo)

        ed_btn = QPushButton("✏️ 自定义角色设定 / 台词 / 人设...")
        ed_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        ed_btn.clicked.connect(self._handle_open_editor)
        c_app.addWidget(ed_btn, 0, Qt.AlignmentFlag.AlignLeft)

        # Behavior card
        c_beh = self._make_card(root, "行为与物理互动")
        self._chk_topmost = self._add_check(c_beh, "窗口始终置顶（覆盖其他应用之上）")
        self._chk_pin = self._add_check(c_beh, "固定在桌面（不乱跑）")
        self._chk_pin.setToolTip("开启后停止自由漫步，仍可鼠标拖动位置")
        self._chk_doze = self._add_check(c_beh, "打盹模式（原地睡觉待机，关闭时不会自行入睡）")
        self._chk_doze.setToolTip("开启后停止漫步、随机动作与闲聊气泡；摸头或拖动后会继续打盹")
        self._chk_antigrav = self._add_check(c_beh, "反重力悬浮模式（Antigravity 常驻漂浮）")
        self._chk_gravity = self._add_check(c_beh, "松手重力下落与 Q 弹着陆")
        self._chk_edgedock = self._add_check(c_beh, "拖动到屏幕左右边缘时贴边偷看")
        self._chk_particles = self._add_check(c_beh, "四芒星✦与爱心♥粒子特效")
        self._chk_sound = self._add_check(c_beh, "互动音效（呼噜、星铃、弹跳）")
        self._chk_sysmon = self._add_check(c_beh, "电脑状态感知（低电量打盹 / 高 CPU 提示）")
        self._chk_bubble = self._add_check(c_beh, "自动弹出宠物对话气泡（默认关闭，需要时在菜单中打开）")

        # Follow launch card
        c_fol = self._make_card(root, "应用启动联动（后台唤醒守护）")
        badge_row = QHBoxLayout()
        badge_row.setSpacing(6)
        self._ag_badge = self._make_status_badge("检测中...")
        self._gem_badge = self._make_status_badge("检测中...")
        self._watch_badge = self._make_status_badge("联动状态检测中...")
        badge_row.addWidget(self._ag_badge)
        badge_row.addWidget(self._gem_badge)
        badge_row.addWidget(self._watch_badge)
        badge_row.addStretch(1)
        c_fol.addLayout(badge_row)

        desc = QLabel(
            "勾选后由主程序与轻量后台监听原生检测 Antigravity.exe / Gemini.exe（含退出桌宠后冷启动唤醒），并在当前用户启动目录建立 .lnk 快捷方式，不写入注册表或外部 VBS 脚本。"
        )
        desc.setWordWrap(True)
        desc.setStyleSheet("color: #9D95C8; font-size: 11.5px; background: transparent;")
        c_fol.addWidget(desc)

        self._chk_follow_ag = QCheckBox("跟随 Antigravity 启动而启动")
        self._chk_follow_ag.toggled.connect(self._on_follow_changed)
        c_fol.addWidget(self._chk_follow_ag)

        self._chk_follow_gem = QCheckBox("跟随 Gemini 桌面 App 启动而启动")
        self._chk_follow_gem.toggled.connect(self._on_follow_changed)
        c_fol.addWidget(self._chk_follow_gem)

        fol_btns = QHBoxLayout()
        fol_btns.setSpacing(8)
        sync_btn = QPushButton("🔄 启用并同步开机联动")
        sync_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        sync_btn.clicked.connect(self._on_sync_follow_btn)
        fol_btns.addWidget(sync_btn)

        stop_btn = QPushButton("⏹ 停用应用联动")
        stop_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        stop_btn.clicked.connect(self._on_stop_follow_btn)
        fol_btns.addWidget(stop_btn)

        cfg_dir_btn = QPushButton("📂 打开配置目录")
        cfg_dir_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        cfg_dir_btn.clicked.connect(self._open_settings_folder)
        fol_btns.addWidget(cfg_dir_btn)
        c_fol.addLayout(fol_btns)

        # 官方 API Key 对话设置。
        c_api = self._make_card(root, "可选：Gemini 官方 API 对话")
        api_hint = QLabel("填写 Gemini API Key 可开启联网对话；不填写时仍可使用离线互动、摸头与角色档案。")
        api_hint.setWordWrap(True)
        api_hint.setStyleSheet("color: #9D95C8; font-size: 11.5px; background: transparent;")
        c_api.addWidget(api_hint)

        model_row_host = QWidget()
        model_row_host.setStyleSheet("background: transparent;")
        api_model_row = QHBoxLayout(model_row_host)
        api_model_row.setContentsMargins(0, 0, 0, 0)
        api_model_row.setSpacing(6)
        self._model_combo = QComboBox()
        self._model_combo.setEditable(True)
        self._model_combo.lineEdit().setPlaceholderText(DEFAULT_GEMINI_MODEL)
        self._model_combo.currentTextChanged.connect(self._on_model_text_changed)
        api_model_row.addWidget(self._model_combo, 1)

        self._api_refresh_models_btn = QPushButton("🔄 获取官方模型")
        self._api_refresh_models_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._api_refresh_models_btn.setToolTip("使用填写的 Gemini API Key 从 Google 官方接口获取模型列表")
        self._api_refresh_models_btn.clicked.connect(self._on_refresh_models_clicked)
        api_model_row.addWidget(self._api_refresh_models_btn)
        self._add_labeled_row(c_api, "对话模型", model_row_host)

        self._api_models_hint_lbl = QLabel("")
        self._api_models_hint_lbl.setWordWrap(True)
        self._api_models_hint_lbl.setStyleSheet("color: #9D95C8; font-size: 11px; background: transparent;")
        self._api_models_hint_lbl.setVisible(False)
        c_api.addWidget(self._api_models_hint_lbl)

        self._api_key_edit = QLineEdit()
        self._api_key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self._api_key_edit.setPlaceholderText("AIza...（API Key 模式使用）")
        self._api_key_edit.textChanged.connect(self._on_api_changed)
        self._add_labeled_row(c_api, "Gemini API Key", self._api_key_edit)

        self._thinking_combo = QComboBox()
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
        self._add_labeled_row(c_api, "思考强度", self._thinking_combo)

        # Updates card
        c_upd = self._make_card(root, "版本与软件更新")
        upd_hdr = QHBoxLayout()
        upd_hdr.addStretch(1)
        ver_badge = QLabel(AppVersion.current_display())
        ver_badge.setStyleSheet(
            "background-color: #2D2357; color: #D8CCFF; border-radius: 8px; padding: 3px 8px; font-size: 11.5px;"
        )
        upd_hdr.addWidget(ver_badge)
        c_upd.addLayout(upd_hdr)

        self._upstream_btn = QPushButton()
        self._upstream_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._upstream_btn.setStyleSheet(
            "text-align: left; background: transparent; border: none; color: #9D95C8; font-size: 11.5px; text-decoration: underline; padding: 0;"
        )
        self._upstream_btn.clicked.connect(
            lambda: QDesktopServices.openUrl(
                QUrl(UpdateService.get_latest_tracking_url(self._settings.update_releases_url))
            )
        )
        c_upd.addWidget(self._upstream_btn)

        self._chk_autocheck = self._add_check(c_upd, "启动时自动检查上游最新 Release 更新")

        upd_row = QHBoxLayout()
        chk_upd_btn = QPushButton("🆕 检查更新与 Changelog...")
        chk_upd_btn.setObjectName("PrimaryBtn")
        chk_upd_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        chk_upd_btn.clicked.connect(lambda: self._on_open_update() if self._on_open_update else None)
        upd_row.addWidget(chk_upd_btn)
        self._upd_status_lbl = QLabel("始终追踪上游最新版")
        self._upd_status_lbl.setStyleSheet("color: #A59EC9; font-size: 11.5px; background: transparent;")
        upd_row.addWidget(self._upd_status_lbl, 1)
        c_upd.addLayout(upd_row)

        # Logging card
        c_log = self._make_card(root, "运行日志与问题排查")
        log_desc = QLabel(
            "默认关闭。遇到异常或准备提交反馈时可开启，运行过程与报错将按当天日期写入 .log 文件。"
        )
        log_desc.setWordWrap(True)
        log_desc.setStyleSheet("color: #9D95C8; font-size: 11.5px; background: transparent;")
        c_log.addWidget(log_desc)

        self._chk_log = self._add_check(c_log, "开启运行日志（按当天日期写入 YYYY-MM-DD.log）")
        self._log_path_lbl = QLabel(f"当天日志：{os.path.join(AppLogger.log_directory(), 'YYYY-MM-DD.log')}")
        self._log_path_lbl.setWordWrap(True)
        self._log_path_lbl.setStyleSheet("color: #A59EC9; font-size: 11px; background: transparent;")
        c_log.addWidget(self._log_path_lbl)

        log_btns = QHBoxLayout()
        open_log_btn = QPushButton("📂 打开日志目录")
        open_log_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        open_log_btn.clicked.connect(self._open_log_folder)
        log_btns.addWidget(open_log_btn)
        open_settings_btn = QPushButton("📄 打开 user_config.json 配置")
        open_settings_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        open_settings_btn.clicked.connect(self._open_settings_file)
        log_btns.addWidget(open_settings_btn)
        log_btns.addStretch(1)
        c_log.addLayout(log_btns)

        # Tip card
        tip = QFrame()
        tip.setObjectName("TipCard")
        tl = QVBoxLayout(tip)
        tl.setContentsMargins(12, 12, 12, 12)
        tip_lbl = QLabel(
            "互动小贴士：在猫耳与头顶区域来回滑动鼠标可触发「摸摸头」；按住快速左右甩动会让 Gemini 娘晕头或炸毛生气；右键点击角色可切换 Q 版 / 标准版形象及全部功能。"
        )
        tip_lbl.setWordWrap(True)
        tip_lbl.setStyleSheet("color: #ACA4D6; font-size: 11.5px; background: transparent;")
        tl.addWidget(tip_lbl)
        root.addWidget(tip)

        scroll.setWidget(container)
        outer.addWidget(scroll, 1)

        footer = QFrame()
        footer.setObjectName("SettingsFooter")
        footer.setStyleSheet(
            """
            QFrame#SettingsFooter {
                background-color: #16112B;
                border-top: 1px solid #352A63;
            }
            """
        )
        fl = QHBoxLayout(footer)
        fl.setContentsMargins(16, 10, 16, 10)
        fl.setSpacing(8)

        self._save_feedback_lbl = QLabel("✓ 当前配置已生效")
        self._save_feedback_lbl.setWordWrap(True)
        self._save_feedback_lbl.setStyleSheet(
            "color: #A59EC9; font-size: 12px; background: transparent;"
        )
        fl.addWidget(self._save_feedback_lbl, 1)

        self._discard_btn = QPushButton("↩ 还原修改")
        self._discard_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._discard_btn.setEnabled(False)
        self._discard_btn.clicked.connect(self._on_discard_clicked)
        fl.addWidget(self._discard_btn)

        self._confirm_btn = QPushButton("💾 确认修改")
        self._confirm_btn.setObjectName("PrimaryBtn")
        self._confirm_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._confirm_btn.clicked.connect(self.confirm_and_save)
        fl.addWidget(self._confirm_btn)

        outer.addWidget(footer)

    @staticmethod
    def _make_card(parent_layout: QVBoxLayout, heading: str) -> QVBoxLayout:
        card = QFrame()
        card.setObjectName("CardPanel")
        vl = QVBoxLayout(card)
        vl.setContentsMargins(14, 14, 14, 14)
        vl.setSpacing(8)
        lbl = QLabel(heading)
        lbl.setStyleSheet("font-size: 14px; font-weight: bold; color: #C4B5FD; background: transparent;")
        vl.addWidget(lbl)
        parent_layout.addWidget(card)
        return vl

    @staticmethod
    def _add_labeled_row(parent: QVBoxLayout, label: str, widget: QWidget) -> None:
        row = QHBoxLayout()
        lbl = QLabel(label)
        lbl.setFixedWidth(105)
        lbl.setStyleSheet("color: #DAD3FA; background: transparent;")
        row.addWidget(lbl)
        row.addWidget(widget, 1)
        parent.addLayout(row)

    @staticmethod
    def _add_slider_row(parent: QVBoxLayout, label: str, slider: QSlider, val_lbl: QLabel) -> None:
        row = QHBoxLayout()
        lbl = QLabel(label)
        lbl.setFixedWidth(105)
        lbl.setStyleSheet("color: #DAD3FA; background: transparent;")
        row.addWidget(lbl)
        row.addWidget(slider, 1)
        row.addWidget(val_lbl)
        parent.addLayout(row)

    def _add_check(self, parent: QVBoxLayout, text: str) -> QCheckBox:
        chk = QCheckBox(text)
        chk.toggled.connect(self._on_any_changed)
        parent.addWidget(chk)
        return chk

    @staticmethod
    def _field_lbl(text: str) -> QLabel:
        l = QLabel(text)
        l.setStyleSheet("color: #D4CEF5; font-size: 12px; background: transparent;")
        return l

    @staticmethod
    def _make_status_badge(text: str) -> QLabel:
        l = QLabel(text)
        l.setStyleSheet(
            "background-color: #252048; color: #C4B5FD; border-radius: 8px; padding: 3px 8px; font-size: 11px;"
        )
        return l

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
        weight = "bold" if kind != "neutral" else "normal"
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
        self._loading = True
        try:
            s = self._settings
            self._style_combo.setCurrentIndex(0 if s.style_mode == PetStyleMode.CHIBI else 1)
            self._scale_slider.setValue(int(round(s.scale * 100)))
            self._scale_val_lbl.setText(f"{s.scale:.2f}x")
            self._opacity_slider.setValue(int(round(s.opacity * 100)))
            self._opacity_val_lbl.setText(f"{int(round(s.opacity * 100))}%")

            idx = next((i for i, opt in enumerate(COSTUME_OPTIONS) if opt.id == s.costume_override), 0)
            self._costume_combo.setCurrentIndex(idx)

            self._chk_topmost.setChecked(s.always_on_top)
            self._chk_pin.setChecked(not s.auto_walk)
            self._chk_doze.setChecked(s.doze_mode)
            self._chk_antigrav.setChecked(s.anti_gravity_mode)
            self._chk_gravity.setChecked(s.gravity_enabled)
            self._chk_edgedock.setChecked(s.edge_dock_enabled)
            self._chk_particles.setChecked(s.particles_enabled)
            self._chk_sound.setChecked(s.sound_enabled)
            self._chk_sysmon.setChecked(s.system_monitor_enabled)
            self._chk_bubble.setChecked(s.show_speech_bubble)

            self._chk_follow_ag.setChecked(s.follow_antigravity)
            self._chk_follow_gem.setChecked(s.follow_gemini_app)

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
            self._chk_autocheck.setChecked(s.auto_check_updates)
            self._upstream_btn.setText(
                f"上游追踪源：{UpdateService.normalize_releases_url(s.update_releases_url)}"
            )
            self._chk_log.setChecked(s.enable_runtime_log)
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

        costume_id = self._costume_combo.currentData() or "auto"
        s.costume_override = costume_id
        s.auto_festival_costume = costume_id == "auto"

        s.always_on_top = self._chk_topmost.isChecked()
        s.auto_walk = not self._chk_pin.isChecked()
        s.doze_mode = self._chk_doze.isChecked()
        s.anti_gravity_mode = self._chk_antigrav.isChecked()
        s.gravity_enabled = self._chk_gravity.isChecked()
        s.edge_dock_enabled = self._chk_edgedock.isChecked()
        s.particles_enabled = self._chk_particles.isChecked()
        s.sound_enabled = self._chk_sound.isChecked()
        s.system_monitor_enabled = self._chk_sysmon.isChecked()
        s.show_speech_bubble = self._chk_bubble.isChecked()
        s.follow_antigravity = self._chk_follow_ag.isChecked()
        s.follow_gemini_app = self._chk_follow_gem.isChecked()

        self._draft_gemini_model = self._model_combo.currentText()
        s.gemini_api_key = self._api_key_edit.text().strip()
        s.gemini_model = normalize_gemini_model(self._draft_gemini_model)
        s.thinking_level = normalize_thinking_level(self._thinking_combo.currentData())
        s.auto_check_updates = self._chk_autocheck.isChecked()
        s.enable_runtime_log = self._chk_log.isChecked()

    def confirm_and_save(self) -> bool:
        """校验并提交界面草稿到运行态与 user_config.json，成功/失败均展示明确状态提示。"""
        snapshot = dict(vars(self._settings))
        follow_changed = (
            self._settings.follow_antigravity != self._chk_follow_ag.isChecked()
            or self._settings.follow_gemini_app != self._chk_follow_gem.isChecked()
        )
        self._commit_ui_to_settings()
        try:
            self._settings_service.save(self._settings)
            if follow_changed:
                self._follow_service.sync_and_apply(self._settings)
            AppLogger.set_enabled(self._settings.enable_runtime_log)
        except Exception as exc:
            vars(self._settings).update(snapshot)
            self._dirty = True
            self._discard_btn.setEnabled(True)
            AppLogger.error("[Settings] save failed", exc)
            err_msg = str(exc).strip() or type(exc).__name__
            self._set_feedback(f"❌ 保存失败：{err_msg}", kind="error")
            return False

        self.sync_from_settings(force=True, clear_feedback=False)
        self._set_feedback("✅ 偏好设置保存成功，已生效", kind="success")
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
        self._api_refresh_models_btn.setText("⏳ 获取中...")
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
        self._api_refresh_models_btn.setText("🔄 获取官方模型")
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
            self._api_models_hint_lbl.setText(f"✅ 已获取 {len(models)} 个官方可用模型，选择后请点击下方「确认修改」生效")
        self._api_models_hint_lbl.setVisible(True)

    def _on_follow_changed(self) -> None:
        if self._loading:
            return
        self._mark_dirty()

    def _on_sync_follow_btn(self) -> None:
        self._settings.follow_antigravity = self._chk_follow_ag.isChecked()
        self._settings.follow_gemini_app = self._chk_follow_gem.isChecked()
        if not AppFollowService.any_follow_enabled(self._settings):
            self._settings.follow_antigravity = True
            self._settings.follow_gemini_app = True
            self._loading = True
            self._chk_follow_ag.setChecked(True)
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
            "已通过原生后台守护与用户启动目录快捷方式启用开机静默待机联动（不新建注册表项，也不生成外部 VBS 文件）。当 Antigravity 或 Gemini 桌面端启动时，Gemini 娘会自动出现。",
        )

    def _on_stop_follow_btn(self) -> None:
        self._loading = True
        self._chk_follow_ag.setChecked(False)
        self._chk_follow_gem.setChecked(False)
        self._loading = False
        self._settings.follow_antigravity = False
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
            gem_on = FollowedAppProbe.sample_gemini_activity().is_running
            watch_on = AppFollowService.is_follow_watcher_running()
            run_on = AppFollowService.is_follow_watcher_run_enabled()
        except Exception as exc:
            AppLogger.warn(f"[Settings] refresh follow badges failed: {exc}")
            return

        self._ag_badge.setText(f"Antigravity: {'运行中 ✦' if ag_on else '未运行'}")
        self._gem_badge.setText(f"Gemini App: {'运行中 ✦' if gem_on else '未运行'}")
        self._watch_badge.setText(
            f"原生守护: {'运行中' if watch_on else ('开机待机已就绪' if run_on else '未启用')}"
        )

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

    def _handle_open_editor(self) -> None:
        if self._on_open_editor:
            self._on_open_editor()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if not self._status_timer.isActive():
            self._status_timer.start()
        self._refresh_follow_badges()

    def closeEvent(self, event) -> None:
        self._status_timer.stop()
        if self._models_refresh_worker is not None and self._models_refresh_worker.isRunning():
            self._models_refresh_worker.requestInterruption()
            self._models_refresh_worker.wait(1000)
        super().closeEvent(event)
