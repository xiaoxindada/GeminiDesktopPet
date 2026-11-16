"""Google 个人账号 OAuth 登录弹窗（支持用户自建客户端 PKCE 授权、读取本地授权与凭据导入）。"""

from __future__ import annotations

from pathlib import Path
from typing import Optional
import webbrowser

from PySide6.QtCore import QObject, Qt, QThread, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from services.oauth_auth_service import (
    OAuthAuthService,
    OAuthClientConfig,
    OAuthLoopbackFlow,
    OAuthSession,
    parse_manual_oauth_input,
    parse_oauth_client_json,
)
from services.tray_icon_service import TrayIconService
from themes.starlight_theme import STARLIGHT_WINDOW_QSS


class _CallbackWaitWorker(QThread):
    """后台监听本地回环回调；捕获授权码后自动兑换 Token 并拉取账号信息。"""

    callback_captured = Signal()
    succeeded = Signal(object)
    failed = Signal(str)

    def __init__(
        self,
        oauth_service: OAuthAuthService,
        flow: OAuthLoopbackFlow,
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        self._oauth_service = oauth_service
        self._flow = flow

    def run(self) -> None:
        try:
            code = self._flow.wait_for_code()
            if self.isInterruptionRequested():
                return
            self.callback_captured.emit()
            session = self._oauth_service.exchange_code_for_session(
                code,
                self._flow.redirect_uri,
                client_config=OAuthClientConfig(self._flow.client_id, self._flow.client_secret),
                code_verifier=self._flow.code_verifier,
            )
            if not self.isInterruptionRequested():
                self.succeeded.emit(session)
        except Exception as exc:
            msg = str(exc)
            if not self.isInterruptionRequested() and "已取消" not in msg:
                self.failed.emit(msg)


class _ManualExchangeWorker(QThread):
    """后台兑换用户手动粘贴的回调链接 / Authorization Code。"""

    succeeded = Signal(object)
    failed = Signal(str)

    def __init__(
        self,
        oauth_service: OAuthAuthService,
        flow: OAuthLoopbackFlow,
        raw_input: str,
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        self._oauth_service = oauth_service
        self._flow = flow
        self._raw_input = raw_input

    def run(self) -> None:
        try:
            code, extracted_state = parse_manual_oauth_input(self._raw_input)
            if extracted_state is not None and extracted_state != self._flow.state:
                raise RuntimeError("OAuth state 校验不匹配（CSRF 防护），请使用最新授权链接重试")
            session = self._oauth_service.exchange_code_for_session(
                code,
                self._flow.redirect_uri,
                client_config=OAuthClientConfig(self._flow.client_id, self._flow.client_secret),
                code_verifier=self._flow.code_verifier,
            )
            if not self.isInterruptionRequested():
                self.succeeded.emit(session)
        except Exception as exc:
            if not self.isInterruptionRequested():
                self.failed.emit(str(exc))


class _RefreshOrLocalWorker(QThread):
    """后台执行本机授权读取、JSON 凭据文件导入或 Refresh Token 登录。"""

    succeeded = Signal(object)
    failed = Signal(str)

    def __init__(
        self,
        oauth_service: OAuthAuthService,
        mode: str,
        payload: str = "",
        client_config: Optional[OAuthClientConfig] = None,
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        self._oauth_service = oauth_service
        self._mode = mode
        self._payload = payload
        self._client_config = client_config

    def run(self) -> None:
        try:
            if self._mode == "discover":
                session = self._oauth_service.discover_and_import_local_session()
            elif self._mode == "file":
                session = self._oauth_service.import_session_from_file(self._payload)
            else:
                session = self._oauth_service.login_with_refresh_token(
                    self._payload,
                    client_config=self._client_config,
                )
            if not self.isInterruptionRequested():
                self.succeeded.emit(session)
        except Exception as exc:
            if not self.isInterruptionRequested():
                self.failed.emit(str(exc))


class OAuthLoginDialog(QDialog):
    """支持用户自带 OAuth 客户端授权、本地 ADC/CLI 读取与凭据导入的登录弹窗。"""

    def __init__(
        self,
        oauth_service: OAuthAuthService,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._oauth_service = oauth_service
        self._flow: Optional[OAuthLoopbackFlow] = None
        self._wait_worker: Optional[_CallbackWaitWorker] = None
        self._manual_worker: Optional[_ManualExchangeWorker] = None
        self._import_worker: Optional[_RefreshOrLocalWorker] = None
        self._busy = False
        self._login_succeeded = False
        self._logged_in_session: Optional[OAuthSession] = None
        self._copy_reset_timer = QTimer(self)
        self._copy_reset_timer.setSingleShot(True)
        self._copy_reset_timer.setInterval(1500)
        self._copy_reset_timer.timeout.connect(self._reset_copy_button_text)

        self.setWindowTitle("🔐 Google OAuth 授权登录")
        self.setWindowIcon(TrayIconService.load_app_icon())
        self.setModal(True)
        self.resize(520, 640)
        self.setMinimumSize(480, 560)
        self.setStyleSheet(STARLIGHT_WINDOW_QSS)

        self._build_ui()
        self._load_client_config_into_inputs()
        if self._oauth_service.get_client_config() is not None:
            self._prepare_oauth_flow()

    @property
    def logged_in_session(self) -> Optional[OAuthSession]:
        return self._logged_in_session

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(20, 18, 20, 18)
        root.setSpacing(10)

        title = QLabel("Google OAuth 授权登录（可选）")
        title.setStyleSheet("font-size: 18px; font-weight: bold; color: #F4F2FF; background: transparent;")
        root.addWidget(title)

        badge = QLabel(
            "优先读取本机已有的标准 Google OAuth / ADC 授权；若本机尚未授权，可导入你自己的 client_secret.json 自主登录，凭证仅保存在本地。"
        )
        badge.setWordWrap(True)
        badge.setStyleSheet(
            "background-color: #211B3D; color: #C4B5FD; border: 1px solid #3C316E; "
            "border-radius: 8px; padding: 6px 10px; font-size: 11.5px;"
        )
        root.addWidget(badge)

        self._status_banner = QLabel("")
        self._status_banner.setWordWrap(True)
        self._status_banner.setVisible(False)
        root.addWidget(self._status_banner)

        self._tabs = QTabWidget()
        self._tabs.currentChanged.connect(self._on_tab_changed)

        # --- Tab 1: 用户自主 OAuth 授权 ---
        oauth_tab = QWidget()
        oauth_layout = QVBoxLayout(oauth_tab)
        oauth_layout.setContentsMargins(4, 10, 4, 4)
        oauth_layout.setSpacing(10)

        client_card = QFrame()
        client_card.setObjectName("TipCard")
        cl = QVBoxLayout(client_card)
        cl.setContentsMargins(12, 10, 12, 10)
        cl.setSpacing(6)

        client_hdr = QHBoxLayout()
        client_title = QLabel("Google OAuth 桌面客户端凭据（自备 · 保存于本地）")
        client_title.setStyleSheet("color: #E9E4FF; font-size: 12px; font-weight: bold; background: transparent;")
        client_hdr.addWidget(client_title, 1)

        self._import_client_btn = QPushButton("📄 导入 client_secret.json")
        self._import_client_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._import_client_btn.clicked.connect(self._on_import_client_json_clicked)
        client_hdr.addWidget(self._import_client_btn)
        cl.addLayout(client_hdr)

        self._client_id_edit = QLineEdit()
        self._client_id_edit.setPlaceholderText("输入 Client ID（例：xxxx.apps.googleusercontent.com）")
        cl.addWidget(self._client_id_edit)

        self._client_secret_edit = QLineEdit()
        self._client_secret_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self._client_secret_edit.setPlaceholderText("输入 Client Secret（桌面应用可选或随 JSON 导入）")
        cl.addWidget(self._client_secret_edit)

        oauth_layout.addWidget(client_card)

        hero_card = QFrame()
        hero_card.setObjectName("CardPanel")
        hl = QVBoxLayout(hero_card)
        hl.setContentsMargins(14, 12, 14, 12)
        hl.setSpacing(8)

        rec_desc = QLabel("配置 Client ID 后点击下方按钮，将拉起默认浏览器完成 Google OAuth 登录并自动接收回调。")
        rec_desc.setWordWrap(True)
        rec_desc.setStyleSheet("color: #A9A0D2; font-size: 11.5px; background: transparent;")
        hl.addWidget(rec_desc)

        self._start_oauth_btn = QPushButton("🚀 开始 OAuth 授权")
        self._start_oauth_btn.setObjectName("PrimaryBtn")
        self._start_oauth_btn.setMinimumHeight(36)
        self._start_oauth_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._start_oauth_btn.clicked.connect(self._on_start_oauth_clicked)
        hl.addWidget(self._start_oauth_btn)

        url_lbl = QLabel("授权链接")
        url_lbl.setStyleSheet("color: #B8AFDE; font-size: 11.5px; background: transparent;")
        hl.addWidget(url_lbl)

        url_row = QHBoxLayout()
        url_row.setSpacing(6)
        self._auth_url_edit = QLineEdit()
        self._auth_url_edit.setReadOnly(True)
        self._auth_url_edit.setPlaceholderText("请先导入 client_secret.json 或填写 Client ID 后生成授权链接...")
        url_row.addWidget(self._auth_url_edit, 1)

        self._copy_url_btn = QPushButton("📋 复制")
        self._copy_url_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._copy_url_btn.clicked.connect(self._on_copy_url_clicked)
        url_row.addWidget(self._copy_url_btn)
        hl.addLayout(url_row)

        self._continue_btn = QPushButton("✅ 我已授权，继续")
        self._continue_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._continue_btn.clicked.connect(self._on_continue_clicked)
        hl.addWidget(self._continue_btn)

        oauth_layout.addWidget(hero_card)

        manual_card = QFrame()
        manual_card.setObjectName("TipCard")
        ml = QVBoxLayout(manual_card)
        ml.setContentsMargins(12, 10, 12, 10)
        ml.setSpacing(6)

        manual_lbl = QLabel("浏览器没有自动跳转？请粘贴回调链接或 Authorization Code：")
        manual_lbl.setWordWrap(True)
        manual_lbl.setStyleSheet("color: #C4B5FD; font-size: 11.5px; background: transparent;")
        ml.addWidget(manual_lbl)

        manual_row = QHBoxLayout()
        manual_row.setSpacing(6)
        self._manual_input = QLineEdit()
        self._manual_input.setPlaceholderText("粘贴回调链接或 Code...")
        self._manual_input.returnPressed.connect(self._on_manual_submit_clicked)
        manual_row.addWidget(self._manual_input, 1)

        self._manual_submit_btn = QPushButton("🔗 提交")
        self._manual_submit_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._manual_submit_btn.clicked.connect(self._on_manual_submit_clicked)
        manual_row.addWidget(self._manual_submit_btn)
        ml.addLayout(manual_row)

        oauth_layout.addWidget(manual_card)
        oauth_layout.addStretch(1)
        self._tabs.addTab(oauth_tab, "OAuth 授权")

        # --- Tab 2: 读取本地授权 / Token ---
        local_tab = QWidget()
        local_layout = QVBoxLayout(local_tab)
        local_layout.setContentsMargins(4, 10, 4, 4)
        local_layout.setSpacing(10)

        discover_card = QFrame()
        discover_card.setObjectName("CardPanel")
        dl = QVBoxLayout(discover_card)
        dl.setContentsMargins(14, 12, 14, 12)
        dl.setSpacing(8)

        discover_desc = QLabel(
            "如果本机已通过 gcloud / Google CLI 完成过 OAuth 授权，可直接一键检测并优先使用本地凭据，无需重复登录。"
        )
        discover_desc.setWordWrap(True)
        discover_desc.setStyleSheet("color: #B8AFDE; font-size: 11.5px; background: transparent;")
        dl.addWidget(discover_desc)

        local_btn_row = QHBoxLayout()
        local_btn_row.setSpacing(8)
        self._detect_local_btn = QPushButton("🔍 读取本机已有 OAuth 授权")
        self._detect_local_btn.setObjectName("PrimaryBtn")
        self._detect_local_btn.setMinimumHeight(34)
        self._detect_local_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._detect_local_btn.clicked.connect(self._on_detect_local_clicked)
        local_btn_row.addWidget(self._detect_local_btn, 1)

        self._import_json_file_btn = QPushButton("📂 选择凭据 JSON...")
        self._import_json_file_btn.setMinimumHeight(34)
        self._import_json_file_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._import_json_file_btn.clicked.connect(self._on_import_cred_file_clicked)
        local_btn_row.addWidget(self._import_json_file_btn)
        dl.addLayout(local_btn_row)
        local_layout.addWidget(discover_card)

        token_card = QFrame()
        token_card.setObjectName("CardPanel")
        tl = QVBoxLayout(token_card)
        tl.setContentsMargins(14, 12, 14, 12)
        tl.setSpacing(8)

        token_desc = QLabel(
            "也可直接粘贴本地凭据 JSON（含 client_id / client_secret / refresh_token 或 access_token）或 1//... Refresh Token（需先在左侧填入 Client ID）："
        )
        token_desc.setWordWrap(True)
        token_desc.setStyleSheet("color: #B8AFDE; font-size: 11.5px; background: transparent;")
        tl.addWidget(token_desc)

        self._refresh_token_edit = QPlainTextEdit()
        self._refresh_token_edit.setPlaceholderText("粘贴完整凭据 JSON 或 1//... Refresh Token...")
        self._refresh_token_edit.setMinimumHeight(130)
        tl.addWidget(self._refresh_token_edit)

        self._refresh_submit_btn = QPushButton("🔑 验证并导入")
        self._refresh_submit_btn.setMinimumHeight(34)
        self._refresh_submit_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._refresh_submit_btn.clicked.connect(self._on_refresh_token_submit)
        tl.addWidget(self._refresh_submit_btn)

        local_layout.addWidget(token_card)
        local_layout.addStretch(1)
        self._tabs.addTab(local_tab, "读取本地 / Token")

        root.addWidget(self._tabs, 1)

        footer = QHBoxLayout()
        footer.addStretch(1)
        self._cancel_btn = QPushButton("取消")
        self._cancel_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._cancel_btn.clicked.connect(self.reject)
        footer.addWidget(self._cancel_btn)
        root.addLayout(footer)

    def _load_client_config_into_inputs(self) -> None:
        cfg = self._oauth_service.get_client_config()
        if cfg is not None and cfg.is_valid():
            self._client_id_edit.setText(cfg.client_id)
            self._client_secret_edit.setText(cfg.client_secret)

    def _current_client_config_from_ui(self) -> Optional[OAuthClientConfig]:
        cid = self._client_id_edit.text().strip()
        csec = self._client_secret_edit.text().strip()
        if cid:
            return OAuthClientConfig(client_id=cid, client_secret=csec)
        return self._oauth_service.get_client_config()

    def _persist_client_from_ui(self) -> Optional[OAuthClientConfig]:
        cid = self._client_id_edit.text().strip()
        csec = self._client_secret_edit.text().strip()
        if cid:
            cfg = OAuthClientConfig(client_id=cid, client_secret=csec)
            self._oauth_service.save_client_config(cfg)
            return cfg
        return self._oauth_service.get_client_config()

    def _set_status(self, level: str, text: str) -> None:
        if not text:
            self._status_banner.setVisible(False)
            return
        styles = {
            "loading": "background-color: #211D47; color: #C4B5FD; border: 1px solid #6D4AFF;",
            "success": "background-color: #14362B; color: #6EE7B7; border: 1px solid #10B981;",
            "error": "background-color: #3B1B2B; color: #FCA5A5; border: 1px solid #F43F5E;",
        }
        style = styles.get(level, styles["loading"])
        self._status_banner.setStyleSheet(f"{style} border-radius: 8px; padding: 8px 10px; font-size: 12px;")
        self._status_banner.setText(text)
        self._status_banner.setVisible(True)

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        disabled = busy or self._login_succeeded
        self._start_oauth_btn.setEnabled(not disabled)
        self._import_client_btn.setEnabled(not disabled)
        self._manual_submit_btn.setEnabled(not disabled)
        self._detect_local_btn.setEnabled(not disabled)
        self._import_json_file_btn.setEnabled(not disabled)
        self._refresh_submit_btn.setEnabled(not disabled)
        self._continue_btn.setEnabled(not self._login_succeeded)
        self._cancel_btn.setEnabled(not self._login_succeeded)

    def _prepare_oauth_flow(self, client_config: Optional[OAuthClientConfig] = None) -> bool:
        self._teardown_flow_only()
        cfg = client_config or self._current_client_config_from_ui()
        if cfg is None or not cfg.is_valid():
            self._auth_url_edit.clear()
            return False

        try:
            flow = self._oauth_service.start_loopback_flow(client_config=cfg, enable_listener=True)
        except Exception:
            flow = self._oauth_service.start_loopback_flow(client_config=cfg, enable_listener=False)

        self._flow = flow
        self._auth_url_edit.setText(flow.auth_url)
        self._auth_url_edit.setCursorPosition(0)

        worker = _CallbackWaitWorker(self._oauth_service, flow, parent=self)
        worker.callback_captured.connect(self._on_callback_captured)
        worker.succeeded.connect(self._on_login_succeeded)
        worker.failed.connect(self._on_callback_failed)
        self._wait_worker = worker
        worker.start()
        return True

    def _teardown_flow_only(self) -> None:
        if self._flow is not None:
            self._flow.cancel()
            self._flow = None
        if self._wait_worker is not None:
            self._wait_worker.requestInterruption()
            self._wait_worker.wait(1200)
            self._wait_worker.deleteLater()
            self._wait_worker = None

    def _on_tab_changed(self, idx: int) -> None:
        if self._login_succeeded or self._busy:
            return
        self._set_status("", "")
        if idx == 0 and self._flow is None and self._current_client_config_from_ui() is not None:
            self._prepare_oauth_flow()

    def _on_import_client_json_clicked(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "选择 Google client_secret.json",
            "",
            "JSON Files (*.json);;All Files (*)",
        )
        if not path:
            return
        try:
            raw_text = Path(path).read_text(encoding="utf-8-sig")
            cfg = parse_oauth_client_json(raw_text)
            self._oauth_service.save_client_config(cfg)
            self._client_id_edit.setText(cfg.client_id)
            self._client_secret_edit.setText(cfg.client_secret)
            self._prepare_oauth_flow(cfg)
            self._set_status("success", "已导入 OAuth 客户端配置，现在可点击「开始 OAuth 授权」。")
        except Exception as exc:
            self._set_status("error", f"导入客户端 JSON 失败：{exc}")

    def _on_start_oauth_clicked(self) -> None:
        try:
            cfg = self._persist_client_from_ui()
        except Exception as exc:
            self._set_status("error", str(exc))
            return
        if cfg is None or not cfg.is_valid():
            self._set_status(
                "error",
                "请先导入 Google client_secret.json 或填入 Client ID；如果本机已有授权，可切换到「读取本地 / Token」页直接导入。",
            )
            return

        if self._flow is None or self._flow.client_id != cfg.client_id or self._flow.client_secret != cfg.client_secret:
            self._prepare_oauth_flow(cfg)
        assert self._flow is not None

        url = self._flow.auth_url
        opened = QDesktopServices.openUrl(QUrl(url))
        if not opened:
            try:
                opened = webbrowser.open(url)
            except Exception:
                opened = False

        if opened:
            self._start_oauth_btn.setText("⏳ 正在等待浏览器授权回调...")
            self._set_status("loading", "已在默认浏览器打开 Google 登录页，完成授权后将自动回传并登录。")
        else:
            self._set_status(
                "error",
                "未能自动唤起浏览器，请点击「复制」并在浏览器中手动打开授权链接。",
            )

    def _on_copy_url_clicked(self) -> None:
        if self._flow is None:
            cfg = self._current_client_config_from_ui()
            if cfg is None or not cfg.is_valid():
                self._set_status("error", "请先填写或导入 OAuth Client ID 再生成授权链接。")
                return
            self._prepare_oauth_flow(cfg)
        url = self._auth_url_edit.text().strip()
        if not url:
            return
        cb = QApplication.clipboard()
        if cb is not None:
            cb.setText(url)
        self._copy_url_btn.setText("✓ 已复制")
        self._copy_reset_timer.start()

    def _reset_copy_button_text(self) -> None:
        self._copy_url_btn.setText("📋 复制")

    def _on_continue_clicked(self) -> None:
        if self._login_succeeded:
            return
        if self._flow is None:
            cfg = self._current_client_config_from_ui()
            if cfg is None or not cfg.is_valid():
                self._set_status("error", "请先填写或导入 OAuth Client ID。")
                return
            self._prepare_oauth_flow(cfg)
            self._set_status("loading", "已重新准备监听端口，请在浏览器完成授权或粘贴回调链接。")
            return
        if self._flow.has_result():
            self._set_status("loading", "已收到授权回调，正在完成 Token 兑换...")
        else:
            self._set_status(
                "loading",
                "仍在等待浏览器回调中；若页面未自动跳转，请把浏览器地址栏的回调链接或 Code 粘贴到下方提交。",
            )

    def _on_callback_captured(self) -> None:
        if self._login_succeeded:
            return
        self._set_busy(True)
        self._set_status("loading", "已捕获 Google 授权回调，正在换取凭据与账号信息...")

    def _on_callback_failed(self, err_msg: str) -> None:
        if self._login_succeeded:
            return
        self._set_busy(False)
        self._start_oauth_btn.setText("🚀 开始 OAuth 授权")
        self._set_status("error", f"OAuth 授权失败：{err_msg}")
        self._prepare_oauth_flow()

    def _on_manual_submit_clicked(self) -> None:
        raw_input = self._manual_input.text().strip()
        if not raw_input:
            self._set_status("error", "请先粘贴回调链接或 Authorization Code")
            return
        cfg = self._persist_client_from_ui()
        if cfg is None or not cfg.is_valid():
            self._set_status("error", "请先导入或填写 OAuth Client ID")
            return
        if self._flow is None:
            self._prepare_oauth_flow(cfg)
        assert self._flow is not None

        self._set_busy(True)
        self._set_status("loading", "正在提交 Authorization Code 并换取 Token...")
        worker = _ManualExchangeWorker(self._oauth_service, self._flow, raw_input, parent=self)
        worker.succeeded.connect(self._on_login_succeeded)
        worker.failed.connect(self._on_manual_failed)
        self._manual_worker = worker
        worker.start()

    def _on_manual_failed(self, err_msg: str) -> None:
        if self._manual_worker is not None:
            self._manual_worker.deleteLater()
            self._manual_worker = None
        if self._login_succeeded:
            return
        self._set_busy(False)
        self._set_status("error", f"提交失败：{err_msg}")
        self._prepare_oauth_flow()

    def _on_detect_local_clicked(self) -> None:
        self._set_busy(True)
        self._set_status("loading", "正在扫描并验证本机已有 OAuth 授权...")
        worker = _RefreshOrLocalWorker(self._oauth_service, mode="discover", parent=self)
        worker.succeeded.connect(self._on_login_succeeded)
        worker.failed.connect(self._on_import_failed)
        self._import_worker = worker
        worker.start()

    def _on_import_cred_file_clicked(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "选择本地 Google OAuth / ADC 凭据 JSON",
            "",
            "JSON Files (*.json);;All Files (*)",
        )
        if not path:
            return
        self._set_busy(True)
        self._set_status("loading", "正在导入并验证所选凭据文件...")
        worker = _RefreshOrLocalWorker(self._oauth_service, mode="file", payload=path, parent=self)
        worker.succeeded.connect(self._on_login_succeeded)
        worker.failed.connect(self._on_import_failed)
        self._import_worker = worker
        worker.start()

    def _on_refresh_token_submit(self) -> None:
        raw_input = self._refresh_token_edit.toPlainText().strip()
        if not raw_input:
            self._set_status("error", "请先粘贴凭据 JSON 或 Refresh Token")
            return
        cfg = self._current_client_config_from_ui()
        self._set_busy(True)
        self._set_status("loading", "正在验证并导入凭据...")
        worker = _RefreshOrLocalWorker(
            self._oauth_service,
            mode="token",
            payload=raw_input,
            client_config=cfg,
            parent=self,
        )
        worker.succeeded.connect(self._on_login_succeeded)
        worker.failed.connect(self._on_import_failed)
        self._import_worker = worker
        worker.start()

    def _on_import_failed(self, err_msg: str) -> None:
        if self._import_worker is not None:
            self._import_worker.deleteLater()
            self._import_worker = None
        if self._login_succeeded:
            return
        self._set_busy(False)
        self._set_status("error", err_msg)

    def _on_login_succeeded(self, session: OAuthSession) -> None:
        if self._login_succeeded:
            return
        self._login_succeeded = True
        self._logged_in_session = session
        self._set_busy(False)
        self._teardown_flow_only()
        tier_text = f" · {session.subscription_tier}" if session.subscription_tier else ""
        self._set_status("success", f"✅ OAuth 授权就绪：{session.email}{tier_text}")
        QTimer.singleShot(850, self.accept)

    def _cleanup_all_workers(self) -> None:
        self._teardown_flow_only()
        for attr in ("_manual_worker", "_import_worker"):
            worker = getattr(self, attr, None)
            if worker is not None:
                worker.requestInterruption()
                worker.wait(1200)
                worker.deleteLater()
                setattr(self, attr, None)

    def reject(self) -> None:
        self._cleanup_all_workers()
        super().reject()

    def closeEvent(self, event) -> None:
        self._cleanup_all_workers()
        super().closeEvent(event)
