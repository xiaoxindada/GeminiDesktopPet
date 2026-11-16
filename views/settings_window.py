"""偏好设置窗口：外观、物理行为、应用联动守护、Gemini API、更新源与运行日志管理。"""

from __future__ import annotations

import os
from typing import Callable, Optional

from PySide6.QtCore import Qt, QThread, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices
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
    BUILTIN_OAUTH_MODELS,
    DEFAULT_OAUTH_MODEL,
    ChatAuthMode,
    PetSettings,
    PetStyleMode,
    normalize_oauth_model,
)
from services.app_follow_service import AppFollowService
from services.app_logger import AppLogger
from services.followed_app_probe import FollowedAppProbe
from services.oauth_auth_service import OAuthAuthService
from services.settings_service import SettingsService
from services.tray_icon_service import TrayIconService
from services.update_service import UpdateService
from themes.starlight_theme import STARLIGHT_WINDOW_QSS
from views.oauth_login_dialog import OAuthLoginDialog


class _OAuthModelsRefreshWorker(QThread):
    """后台刷新当前已登录个人账号的可用模型清单。"""

    completed = Signal(object, str)

    def __init__(self, oauth_service: OAuthAuthService, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._oauth_service = oauth_service

    def run(self) -> None:
        try:
            models = self._oauth_service.refresh_models_for_current_session()
            self.completed.emit(models, "")
        except Exception as exc:
            self.completed.emit([], str(exc))


class SettingsWindow(QWidget):
    """实时同步全部 user_config.json 与跟随联动的偏好设置面板。"""

    def __init__(
        self,
        settings: PetSettings,
        settings_service: SettingsService,
        app_follow_service: AppFollowService,
        on_settings_applied: Callable[[], None],
        on_open_profile_editor: Optional[Callable[[], None]] = None,
        on_open_update_center: Optional[Callable[[], None]] = None,
        oauth_service: Optional[OAuthAuthService] = None,
        on_oauth_changed: Optional[Callable[[], None]] = None,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._settings = settings
        self._settings_service = settings_service
        self._follow_service = app_follow_service
        self._on_applied = on_settings_applied
        self._on_open_editor = on_open_profile_editor
        self._on_open_update = on_open_update_center
        self._oauth_service = oauth_service or OAuthAuthService(settings_service.settings_folder)
        self._on_oauth_changed = on_oauth_changed
        self._models_refresh_worker: Optional[_OAuthModelsRefreshWorker] = None
        self._loading = True
        self._self_updating = False

        self.setWindowTitle("⚙️ Gemini 娘 · 偏好设置")
        self.setWindowIcon(TrayIconService.load_app_icon())
        self.resize(510, 740)
        self.setMinimumSize(460, 560)
        self.setStyleSheet(STARLIGHT_WINDOW_QSS)

        self._build_ui()
        self.sync_from_settings()
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
        c_fol = self._make_card(root, "应用启动联动（原生后台待机守护）")
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
            "勾选后由主程序与轻量后台监听原生检测 Antigravity.exe / Gemini.exe（含退出桌宠后冷启动），并在当前用户启动目录建立 .lnk 快捷方式，不写入注册表或外部 VBS 脚本。"
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

        self._chk_sleep_exit = self._add_check(
            c_fol, "被跟随应用全部关闭时自动隐藏待机（下次开启软件时再唤醒）"
        )

        fol_btns = QHBoxLayout()
        fol_btns.setSpacing(8)
        sync_btn = QPushButton("🔄 启用并同步开机联动")
        sync_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        sync_btn.clicked.connect(self._on_sync_follow_btn)
        fol_btns.addWidget(sync_btn)

        stop_btn = QPushButton("⏹ 停用联动与开机待机")
        stop_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        stop_btn.clicked.connect(self._on_stop_follow_btn)
        fol_btns.addWidget(stop_btn)

        cfg_dir_btn = QPushButton("📂 打开配置目录")
        cfg_dir_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        cfg_dir_btn.clicked.connect(self._open_settings_folder)
        fol_btns.addWidget(cfg_dir_btn)
        c_fol.addLayout(fol_btns)

        # Gemini API / OAuth Chat card
        c_api = self._make_card(root, "可选：联网对话 (OAuth 个人账号 / Gemini API)")
        api_desc = QLabel(
            "不登录或填写也可完整使用离线互动、摸头、反重力悬浮与角色档案；可选通过 Google 个人账号 OAuth 登录（免 API Key）或填写 Gemini API Key，在气泡中实时对话。"
        )
        api_desc.setWordWrap(True)
        api_desc.setStyleSheet("color: #9D95C8; font-size: 11.5px; background: transparent;")
        c_api.addWidget(api_desc)

        self._auth_mode_combo = QComboBox()
        self._auth_mode_combo.addItem("自动（已登录 OAuth 时优先 OAuth，否则 API Key）", ChatAuthMode.AUTO)
        self._auth_mode_combo.addItem("OAuth 个人账号登录（可选 · 免 API Key）", ChatAuthMode.OAUTH)
        self._auth_mode_combo.addItem("Gemini API Key", ChatAuthMode.API_KEY)
        self._auth_mode_combo.currentIndexChanged.connect(self._on_api_changed)
        self._add_labeled_row(c_api, "对话连接模式", self._auth_mode_combo)

        # OAuth 个人账号区域
        oauth_box = QFrame()
        oauth_box.setObjectName("TipCard")
        ol = QVBoxLayout(oauth_box)
        ol.setContentsMargins(10, 10, 10, 10)
        ol.setSpacing(8)

        oauth_hdr = QHBoxLayout()
        oauth_hdr.setSpacing(8)
        self._oauth_status_lbl = QLabel("个人账号：未登录（可选）")
        self._oauth_status_lbl.setWordWrap(True)
        self._oauth_status_lbl.setStyleSheet("color: #E9E4FF; font-size: 12px; background: transparent;")
        oauth_hdr.addWidget(self._oauth_status_lbl, 1)

        self._oauth_tier_badge = self._make_status_badge("未登录")
        oauth_hdr.addWidget(self._oauth_tier_badge)
        ol.addLayout(oauth_hdr)

        oauth_btns = QHBoxLayout()
        oauth_btns.setSpacing(6)
        self._oauth_detect_btn = QPushButton("🔍 读取本地授权")
        self._oauth_detect_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._oauth_detect_btn.clicked.connect(self._on_detect_local_oauth_clicked)
        oauth_btns.addWidget(self._oauth_detect_btn)

        self._oauth_login_btn = QPushButton("🔐 获取 OAuth 授权...")
        self._oauth_login_btn.setObjectName("PrimaryBtn")
        self._oauth_login_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._oauth_login_btn.clicked.connect(self._on_oauth_login_clicked)
        oauth_btns.addWidget(self._oauth_login_btn)

        self._oauth_refresh_models_btn = QPushButton("🔄 刷新模型")
        self._oauth_refresh_models_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._oauth_refresh_models_btn.clicked.connect(self._on_refresh_oauth_models_clicked)
        oauth_btns.addWidget(self._oauth_refresh_models_btn)

        self._oauth_logout_btn = QPushButton("🚪 退出登录")
        self._oauth_logout_btn.setObjectName("DangerBtn")
        self._oauth_logout_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._oauth_logout_btn.clicked.connect(self._on_oauth_logout_clicked)
        oauth_btns.addWidget(self._oauth_logout_btn)
        oauth_btns.addStretch(1)
        ol.addLayout(oauth_btns)

        self._oauth_model_combo = QComboBox()
        self._oauth_model_combo.setEditable(True)
        self._oauth_model_combo.lineEdit().setPlaceholderText(DEFAULT_OAUTH_MODEL)
        self._oauth_model_combo.currentTextChanged.connect(self._on_api_changed)
        self._add_labeled_row(ol, "OAuth 对话模型", self._oauth_model_combo)
        c_api.addWidget(oauth_box)

        c_api.addWidget(self._field_lbl("Gemini API Key（API Key 模式使用）"))
        self._api_key_edit = QLineEdit()
        self._api_key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self._api_key_edit.setPlaceholderText("AIza...")
        self._api_key_edit.textChanged.connect(self._on_api_changed)
        c_api.addWidget(self._api_key_edit)

        c_api.addWidget(self._field_lbl("API Key 模型名称"))
        self._model_edit = QLineEdit()
        self._model_edit.setPlaceholderText("gemini-4-hydrogen")
        self._model_edit.textChanged.connect(self._on_api_changed)
        c_api.addWidget(self._model_edit)

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
        outer.addWidget(scroll)

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

    def sync_from_settings(self) -> None:
        if self._self_updating:
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
            self._chk_sleep_exit.setChecked(s.sleep_when_antigravity_exits)

            auth_idx = next(
                (
                    i
                    for i in range(self._auth_mode_combo.count())
                    if ChatAuthMode.parse(self._auth_mode_combo.itemData(i)) == s.chat_auth_mode
                ),
                0,
            )
            self._auth_mode_combo.setCurrentIndex(auth_idx)
            self._sync_oauth_panel()
            self._api_key_edit.setText(s.gemini_api_key or "")
            self._model_edit.setText(s.gemini_model or "gemini-4-hydrogen")
            self._chk_autocheck.setChecked(s.auto_check_updates)
            self._upstream_btn.setText(
                f"上游追踪源：{UpdateService.normalize_releases_url(s.update_releases_url)}"
            )
            self._chk_log.setChecked(s.enable_runtime_log)
        finally:
            self._loading = False

    def _on_any_changed(self) -> None:
        if self._loading:
            return
        s = self._settings
        # QComboBox 会把 str 枚举数据转成普通 str，需解析回 PetStyleMode 才能正常序列化保存
        s.style_mode = PetStyleMode.parse(self._style_combo.currentData())
        s.scale = max(0.6, min(1.8, self._scale_slider.value() / 100.0))
        s.opacity = max(0.4, min(1.0, self._opacity_slider.value() / 100.0))
        self._scale_val_lbl.setText(f"{s.scale:.2f}x")
        self._opacity_val_lbl.setText(f"{int(round(s.opacity * 100))}%")

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
        s.sleep_when_antigravity_exits = self._chk_sleep_exit.isChecked()
        s.auto_check_updates = self._chk_autocheck.isChecked()
        s.enable_runtime_log = self._chk_log.isChecked()
        AppLogger.set_enabled(s.enable_runtime_log)

        self._settings_service.save(s)
        self._emit_applied()

    def _on_api_changed(self) -> None:
        if self._loading:
            return
        self._settings.chat_auth_mode = ChatAuthMode.parse(self._auth_mode_combo.currentData())
        self._settings.oauth_model = normalize_oauth_model(self._oauth_model_combo.currentText())
        self._settings.gemini_api_key = self._api_key_edit.text().strip()
        self._settings.gemini_model = self._model_edit.text().strip() or "gemini-4-hydrogen"
        self._settings_service.save(self._settings)
        self._emit_applied()

    def _emit_applied(self) -> None:
        self._self_updating = True
        try:
            self._on_applied()
        finally:
            self._self_updating = False

    def _sync_oauth_panel(self) -> None:
        session = self._oauth_service.load_session(auto_discover=True)
        logged_in = session.is_logged_in()
        if logged_in:
            source_map = {
                "local_adc": "本地 ADC",
                "local_cli": "本地 CLI",
                "manual_import": "本地导入",
                "oauth_login": "OAuth 登录",
            }
            source_tag = source_map.get(session.auth_source, "本地授权")
            disp = f"{session.display_name} ({session.email})" if session.display_name else session.email
            self._oauth_status_lbl.setText(f"授权状态：已就绪 · {disp}")
            tier = session.subscription_tier or source_tag
            self._oauth_tier_badge.setText(tier)
            self._oauth_login_btn.setText("🔄 重新授权 / 导入...")
        else:
            self._oauth_status_lbl.setText("授权状态：未检测到本地 OAuth 授权（可选）")
            self._oauth_tier_badge.setText("未授权")
            self._oauth_login_btn.setText("🔐 获取 OAuth 授权...")

        self._oauth_refresh_models_btn.setVisible(logged_in)
        self._oauth_logout_btn.setVisible(logged_in)

        # 更新可用模型下拉框列表并保持当前选择
        current_model = normalize_oauth_model(self._settings.oauth_model)
        candidates: list[str] = []
        for m in [*(session.available_models or []), *BUILTIN_OAUTH_MODELS, current_model]:
            cleaned = (m or "").strip()
            if cleaned and cleaned not in candidates:
                candidates.append(cleaned)

        self._oauth_model_combo.blockSignals(True)
        try:
            self._oauth_model_combo.clear()
            for m in candidates:
                self._oauth_model_combo.addItem(m)
            self._oauth_model_combo.setCurrentText(current_model)
        finally:
            self._oauth_model_combo.blockSignals(False)

    def _on_detect_local_oauth_clicked(self) -> None:
        try:
            session = self._oauth_service.discover_and_import_local_session()
        except Exception as exc:
            QMessageBox.information(
                self,
                "未发现可用本地授权",
                f"{exc}\n\n你也可以点击「🔐 获取 OAuth 授权...」自主导入凭据或完成浏览器登录。",
            )
            return
        self._apply_logged_in_session(session)

    def _apply_logged_in_session(self, session) -> None:
        self._settings.chat_auth_mode = ChatAuthMode.OAUTH
        # 若账户返回了动态模型列表且当前默认模型不在其中，自动选首个可用模型避免 404
        if session.available_models and self._settings.oauth_model not in session.available_models:
            preferred = next(
                (m for m in (DEFAULT_OAUTH_MODEL, "gemini-3.8-flash", "gemini-4-hydrogen", "gemini-4-argon") if m in session.available_models),
                session.available_models[0],
            )
            self._settings.oauth_model = preferred
        self._settings_service.save(self._settings)
        self.sync_from_settings()
        if self._on_oauth_changed:
            self._on_oauth_changed()
        self._on_applied()

    def _on_oauth_login_clicked(self) -> None:
        dlg = OAuthLoginDialog(self._oauth_service, parent=self)
        if dlg.exec() == OAuthLoginDialog.DialogCode.Accepted and dlg.logged_in_session is not None:
            self._apply_logged_in_session(dlg.logged_in_session)

    def _on_refresh_oauth_models_clicked(self) -> None:
        if self._models_refresh_worker is not None and self._models_refresh_worker.isRunning():
            return
        self._oauth_refresh_models_btn.setEnabled(False)
        self._oauth_refresh_models_btn.setText("⏳ 刷新中...")
        worker = _OAuthModelsRefreshWorker(self._oauth_service, parent=self)
        worker.completed.connect(self._on_oauth_models_refreshed)
        self._models_refresh_worker = worker
        worker.start()

    def _on_oauth_models_refreshed(self, models: list[str], err_msg: str) -> None:
        if self._models_refresh_worker is not None:
            self._models_refresh_worker.deleteLater()
            self._models_refresh_worker = None
        self._oauth_refresh_models_btn.setEnabled(True)
        self._oauth_refresh_models_btn.setText("🔄 刷新模型")
        if err_msg:
            QMessageBox.warning(self, "刷新模型失败", f"暂时无法拉取个人账号可用模型列表：{err_msg}")
            return
        self._sync_oauth_panel()
        QMessageBox.information(
            self,
            "模型列表已更新",
            f"已同步个人账号可用模型，共 {len(models)} 个候选模型。",
        )

    def _on_oauth_logout_clicked(self) -> None:
        self._oauth_service.clear_session()
        if self._settings.chat_auth_mode == ChatAuthMode.OAUTH:
            self._settings.chat_auth_mode = ChatAuthMode.AUTO
        self._settings_service.save(self._settings)
        self.sync_from_settings()
        if self._on_oauth_changed:
            self._on_oauth_changed()
        self._on_applied()


    def _on_follow_changed(self) -> None:
        if self._loading:
            return
        self._settings.follow_antigravity = self._chk_follow_ag.isChecked()
        self._settings.follow_gemini_app = self._chk_follow_gem.isChecked()
        self._settings_service.save(self._settings)
        self._follow_service.sync_and_apply(self._settings)
        self._on_applied()
        self._refresh_follow_badges()

    def _on_sync_follow_btn(self) -> None:
        if not AppFollowService.any_follow_enabled(self._settings):
            self._settings.follow_antigravity = True
            self._settings.follow_gemini_app = True
            self._loading = True
            self._chk_follow_ag.setChecked(True)
            self._chk_follow_gem.setChecked(True)
            self._loading = False
        self._settings_service.save(self._settings)
        self._follow_service.sync_and_apply(self._settings)
        self._on_applied()
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
        self._settings_service.save(self._settings)
        self._follow_service.disable_follow()
        self._on_applied()
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
