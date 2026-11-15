"""GitHub Releases 检查更新、Changelog 查看与一键下载更新窗口。"""

from __future__ import annotations

from typing import Callable, Optional

from PySide6.QtCore import Qt, QThread, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from models.app_version import AppVersion
from models.pet_settings import PetSettings
from services.settings_service import SettingsService
from services.tray_icon_service import TrayIconService
from services.update_service import (
    ReleaseAssetInfo,
    UpdateCheckResult,
    UpdateService,
)
from themes.starlight_theme import STARLIGHT_WINDOW_QSS


class _CheckWorker(QThread):
    completed = Signal(object)

    def __init__(self, service: UpdateService, url: str, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._service = service
        self._url = url

    def run(self) -> None:
        res = self._service.check_for_update(self._url)
        self.completed.emit(res)


class _DownloadWorker(QThread):
    progress = Signal(int, object)
    succeeded = Signal(str)
    failed = Signal(str)

    def __init__(
        self,
        service: UpdateService,
        asset: ReleaseAssetInfo,
        tag: str,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._service = service
        self._asset = asset
        self._tag = tag

    def run(self) -> None:
        try:
            path = self._service.download_asset(
                self._asset,
                self._tag,
                progress_cb=lambda d, tot: self.progress.emit(d, tot),
                cancel_check=self.isInterruptionRequested,
            )
            self.succeeded.emit(path)
        except Exception as exc:
            self.failed.emit(str(exc))


class UpdateWindow(QWidget):
    """检查 GitHub Releases 最新版本并展示完整 Changelog、下载进度与一键原地安装。"""

    def __init__(
        self,
        update_service: UpdateService,
        settings_service: SettingsService,
        settings: PetSettings,
        on_request_graceful_exit: Callable[[], None],
        before_updater_cleanup: Optional[Callable[[], None]] = None,
        initial_result: Optional[UpdateCheckResult] = None,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._update_service = update_service
        self._settings_service = settings_service
        self._settings = settings
        self._trigger_exit = on_request_graceful_exit
        self._before_cleanup = before_updater_cleanup
        self._last_result: Optional[UpdateCheckResult] = initial_result
        self._check_worker: Optional[_CheckWorker] = None
        self._dl_worker: Optional[_DownloadWorker] = None

        self.setWindowTitle("🆕 Gemini 娘 · 检查与更新")
        self.setWindowIcon(TrayIconService.load_app_icon())
        self.resize(560, 580)
        self.setMinimumSize(480, 480)
        self.setStyleSheet(STARLIGHT_WINDOW_QSS)

        self._build_ui()
        if initial_result is not None:
            self._apply_result(initial_result)
        else:
            self.start_check()

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(20, 20, 20, 20)
        root.setSpacing(12)

        # Header
        header = QFrame()
        header.setObjectName("HeaderCard")
        hl = QHBoxLayout(header)
        hl.setContentsMargins(16, 14, 16, 14)

        hl_left = QVBoxLayout()
        top_row = QHBoxLayout()
        top_row.setSpacing(8)
        t_lbl = QLabel("✦ 软件更新中心")
        t_lbl.setStyleSheet("font-size: 19px; font-weight: bold; color: #FFFFFF; background: transparent;")
        top_row.addWidget(t_lbl)

        self._cur_badge = QLabel(f"当前 {AppVersion.current_display()}")
        self._cur_badge.setStyleSheet(
            "background-color: #32285E; color: #D8CCFF; border-radius: 8px; padding: 3px 8px; font-size: 11.5px;"
        )
        top_row.addWidget(self._cur_badge)

        self._latest_badge = QLabel("最新待检测")
        self._latest_badge.setStyleSheet(
            "background-color: #252048; color: #B9B2DE; border-radius: 8px; padding: 3px 8px; font-size: 11.5px;"
        )
        top_row.addWidget(self._latest_badge)
        top_row.addStretch(1)
        hl_left.addLayout(top_row)

        self._upstream_btn = QPushButton(
            f"上游追踪源：{UpdateService.normalize_releases_url(self._settings.update_releases_url)}"
        )
        self._upstream_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._upstream_btn.setStyleSheet(
            "text-align: left; background: transparent; border: none; color: #9B92D0; font-size: 11.5px; text-decoration: underline; padding: 2px 0;"
        )
        self._upstream_btn.clicked.connect(
            lambda: QDesktopServices.openUrl(
                QUrl(UpdateService.get_latest_tracking_url(self._settings.update_releases_url))
            )
        )
        hl_left.addWidget(self._upstream_btn)
        hl.addLayout(hl_left, 1)

        self._refresh_btn = QPushButton("🔄 重新检查")
        self._refresh_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._refresh_btn.clicked.connect(self.start_check)
        hl.addWidget(self._refresh_btn)
        root.addWidget(header)

        # Status card
        status_card = QFrame()
        status_card.setObjectName("CardPanel")
        sl = QVBoxLayout(status_card)
        sl.setContentsMargins(14, 11, 14, 11)
        self._status_lbl = QLabel("正在连接 GitHub Releases 检查最新版本...")
        self._status_lbl.setWordWrap(True)
        self._status_lbl.setStyleSheet("font-size: 13.5px; font-weight: bold; color: #E9E4FF; background: transparent;")
        sl.addWidget(self._status_lbl)

        self._meta_lbl = QLabel("将自动解析最新版本（始终指向 /releases/latest）")
        self._meta_lbl.setWordWrap(True)
        self._meta_lbl.setStyleSheet("font-size: 11.5px; color: #A59EC9; background: transparent;")
        sl.addWidget(self._meta_lbl)
        root.addWidget(status_card)

        # Changelog card
        chg_card = QFrame()
        chg_card.setObjectName("CardPanel")
        cl = QVBoxLayout(chg_card)
        cl.setContentsMargins(12, 12, 12, 12)
        chg_hdr = QLabel("📋 更新日志 / Changelog")
        chg_hdr.setStyleSheet("font-size: 13px; font-weight: bold; color: #C4B5FD; background: transparent;")
        cl.addWidget(chg_hdr)
        self._changelog = QPlainTextEdit()
        self._changelog.setReadOnly(True)
        self._changelog.setPlainText("正在读取更新说明与 Git 提交摘要...")
        cl.addWidget(self._changelog, 1)
        root.addWidget(chg_card, 1)

        # Progress bar
        self._progress_panel = QWidget()
        self._progress_panel.hide()
        pl = QVBoxLayout(self._progress_panel)
        pl.setContentsMargins(0, 0, 0, 0)
        pl.setSpacing(4)
        p_top = QHBoxLayout()
        self._dl_status_lbl = QLabel("准备下载更新包...")
        self._dl_status_lbl.setStyleSheet("color: #C4B5FD; font-size: 12px; background: transparent;")
        p_top.addWidget(self._dl_status_lbl, 1)
        self._dl_pct_lbl = QLabel("0%")
        self._dl_pct_lbl.setStyleSheet("color: #6EE7B7; font-size: 12px; background: transparent;")
        p_top.addWidget(self._dl_pct_lbl)
        pl.addLayout(p_top)

        self._progress_bar = QProgressBar()
        self._progress_bar.setRange(0, 100)
        self._progress_bar.setTextVisible(False)
        self._progress_bar.setFixedHeight(8)
        pl.addWidget(self._progress_bar)
        root.addWidget(self._progress_panel)

        # Bottom action bar
        btm = QHBoxLayout()
        open_web = QPushButton("🌐 浏览器打开 Releases")
        open_web.setCursor(Qt.CursorShape.PointingHandCursor)
        open_web.clicked.connect(self._open_release_page)
        btm.addWidget(open_web)

        self._skip_btn = QPushButton("⏭ 跳过此版本")
        self._skip_btn.hide()
        self._skip_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._skip_btn.clicked.connect(self._on_skip_version)
        btm.addWidget(self._skip_btn)
        btm.addStretch(1)

        self._install_btn = QPushButton("⬇ 下载并原地更新")
        self._install_btn.setObjectName("PrimaryBtn")
        self._install_btn.hide()
        self._install_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._install_btn.clicked.connect(self._on_download_and_install)
        btm.addWidget(self._install_btn)

        close_btn = QPushButton("关闭")
        close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        close_btn.clicked.connect(self.close)
        btm.addWidget(close_btn)
        root.addLayout(btm)

    def start_check(self) -> None:
        if self._check_worker is not None and self._check_worker.isRunning():
            return
        self._refresh_btn.setEnabled(False)
        self._status_lbl.setText("正在连接 GitHub Releases 检查最新版本...")
        self._meta_lbl.setText("将自动解析最新版本（始终指向 /releases/latest）")
        worker = _CheckWorker(self._update_service, self._settings.update_releases_url, self)
        worker.completed.connect(self._on_check_completed)
        self._check_worker = worker
        worker.start()

    def _on_check_completed(self, result: UpdateCheckResult) -> None:
        self._refresh_btn.setEnabled(True)
        self._last_result = result
        self._apply_result(result)

    def _apply_result(self, r: UpdateCheckResult) -> None:
        self._status_lbl.setText(r.status_message)
        self._changelog.setPlainText(r.changelog_markdown or "暂无说明。")

        if r.latest_version:
            self._latest_badge.setText(f"最新 v{r.latest_version}")
        elif r.no_published_releases:
            self._latest_badge.setText("暂无 Release")
        else:
            self._latest_badge.setText("检测失败")

        if r.published_at:
            pub = r.published_at.strftime("%Y-%m-%d %H:%M")
            asset_info = (
                f" · 安装包：{r.package_asset.name}"
                if r.package_asset
                else " · （无 ZIP 安装包资产）"
            )
            self._meta_lbl.setText(f"发布时间：{pub}{asset_info}")
        else:
            self._meta_lbl.setText(r.release_title or r.release_html_url)

        show_install = r.succeeded and r.update_available and r.package_asset is not None
        self._install_btn.setVisible(show_install)
        self._skip_btn.setVisible(r.succeeded and r.update_available and bool(r.latest_version))

    def _open_release_page(self) -> None:
        url = (
            self._last_result.release_html_url
            if self._last_result and self._last_result.release_html_url
            else UpdateService.get_latest_tracking_url(self._settings.update_releases_url)
        )
        QDesktopServices.openUrl(QUrl(url))

    def _on_skip_version(self) -> None:
        if not self._last_result or not self._last_result.latest_version:
            return
        self._settings.skipped_update_version = self._last_result.latest_version
        self._settings_service.save(self._settings)
        self._skip_btn.hide()
        self._status_lbl.setText(f"已跳过版本 v{self._last_result.latest_version} 的自动弹窗提醒。")

    def _on_download_and_install(self) -> None:
        if not self._last_result or not self._last_result.package_asset:
            return
        can_update, reason = UpdateService.can_apply_in_place_update()
        if not can_update:
            QMessageBox.information(self, "无法原地自动更新", reason)
            return

        self._install_btn.setEnabled(False)
        self._refresh_btn.setEnabled(False)
        self._progress_panel.show()
        self._progress_bar.setValue(0)
        self._dl_status_lbl.setText("正在下载更新包...")

        worker = _DownloadWorker(
            self._update_service,
            self._last_result.package_asset,
            self._last_result.latest_tag or "latest",
            self,
        )
        worker.progress.connect(self._on_dl_progress)
        worker.succeeded.connect(self._on_dl_succeeded)
        worker.failed.connect(self._on_dl_failed)
        self._dl_worker = worker
        worker.start()

    def _on_dl_progress(self, downloaded: int, total: Optional[int]) -> None:
        mb = downloaded / (1024 * 1024)
        if total and total > 0:
            pct = max(0, min(100, int(downloaded * 100 / total)))
            self._progress_bar.setValue(pct)
            self._dl_pct_lbl.setText(f"{pct}%")
            self._dl_status_lbl.setText(f"正在下载更新包... ({mb:.1f} MB / {total / (1024 * 1024):.1f} MB)")
        else:
            self._dl_pct_lbl.setText(f"{mb:.1f} MB")

    def _on_dl_succeeded(self, zip_path: str) -> None:
        self._dl_status_lbl.setText("下载完成，正在解压并准备重启覆盖安装...")
        self._progress_bar.setValue(100)
        try:
            UpdateService.extract_and_launch_updater(
                zip_path,
                trigger_graceful_exit=self._trigger_exit,
                before_exit_cleanup=self._before_cleanup,
            )
        except Exception as exc:
            self._on_dl_failed(str(exc))

    def _on_dl_failed(self, err: str) -> None:
        self._install_btn.setEnabled(True)
        self._refresh_btn.setEnabled(True)
        self._progress_panel.hide()
        QMessageBox.warning(self, "更新下载或安装失败", err)
