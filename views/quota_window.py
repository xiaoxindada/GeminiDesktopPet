"""Antigravity 剩余额度紧凑悬浮气泡卡片窗口。"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from PySide6.QtCore import QPoint, QRect, QThread, QTimer, Qt, Signal
from PySide6.QtGui import QColor, QFontMetrics, QMouseEvent
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QGraphicsDropShadowEffect,
    QHBoxLayout,
    QLabel,
    QLayout,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from services.antigravity_quota_service import (
    AntigravityQuotaService,
    QuotaBucketInfo,
    QuotaSnapshot,
)
from services.tray_icon_service import TrayIconService


class _QuotaFetchWorker(QThread):
    finished_snapshot = Signal(object)

    def __init__(self, service: AntigravityQuotaService, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._service = service

    def run(self) -> None:
        snap = self._service.fetch_quota(cancel_check=self.isInterruptionRequested)
        if not self.isInterruptionRequested():
            self.finished_snapshot.emit(snap)


class QuotaWindow(QWidget):
    """紧贴桌宠一侧显示的无边框暗紫星夜卡片，实时呈现 Antigravity 额度。"""

    query_started = Signal()
    query_finished = Signal(object)

    def __init__(
        self,
        quota_service: AntigravityQuotaService,
        anchor_pet: Optional[QWidget] = None,
        auto_close_after: bool = True,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._quota_service = quota_service
        self._anchor_pet = anchor_pet
        self._auto_close_enabled = auto_close_after
        self._user_dragged = False
        self._drag_offset: Optional[QPoint] = None
        self._worker: Optional[_QuotaFetchWorker] = None
        self._last_snapshot: Optional[QuotaSnapshot] = None
        self._full_meta_text: str = "套餐 -  ·  --:--"

        self.setWindowTitle("✦ Antigravity 剩余额度")
        self.setWindowIcon(TrayIconService.load_app_icon())
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.Tool
            | Qt.WindowType.WindowStaysOnTopHint
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)

        self._auto_close_timer = QTimer(self)
        self._auto_close_timer.setInterval(8000)
        self._auto_close_timer.setSingleShot(True)
        self._auto_close_timer.timeout.connect(self.close)

        self._loading_dots = 0
        self._loading_timer = QTimer(self)
        self._loading_timer.setInterval(260)
        self._loading_timer.timeout.connect(self._tick_loading_dots)

        self._build_ui()
        self._reset_to_loading_view()

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(14, 14, 14, 14)
        # 强制顶级窗口严格随内容尺寸收缩/扩展，避免载入小卡片后无法撑开真实高度
        outer.setSizeConstraint(QLayout.SizeConstraint.SetFixedSize)

        self._card = QFrame()
        self._card.setObjectName("QuotaCard")
        self._card.setStyleSheet(
            """
            QFrame#QuotaCard {
                background-color: rgba(24, 19, 46, 245);
                border: 1.3px solid #5B47A6;
                border-radius: 16px;
                color: #F6F3FF;
                font-family: "Microsoft YaHei UI", "Segoe UI";
            }
            """
        )
        shadow = QGraphicsDropShadowEffect(self._card)
        shadow.setBlurRadius(20)
        shadow.setOffset(0, 4)
        shadow.setColor(QColor(8, 5, 22, 160))
        self._card.setGraphicsEffect(shadow)

        card_layout = QVBoxLayout(self._card)
        card_layout.setContentsMargins(16, 13, 16, 13)
        card_layout.setSpacing(0)

        self._loading_widget = QWidget()
        self._loading_widget.setMinimumWidth(222)
        self._loading_widget.setMaximumWidth(300)
        ll = QHBoxLayout(self._loading_widget)
        ll.setContentsMargins(0, 0, 0, 0)
        ll.setSpacing(7)

        star = QLabel("✦")
        star.setStyleSheet("color: #A78BFA; font-size: 12px; border: none; background: transparent;")
        ll.addWidget(star)

        self._loading_label = QLabel("让我看看额度还有多少。。。。。。")
        self._loading_label.setStyleSheet("color: #F6F3FF; font-size: 13px; border: none; background: transparent;")
        ll.addWidget(self._loading_label, 1)

        close_loading_btn = self._make_ghost_icon_btn("✕", "关闭", 22, 11)
        close_loading_btn.clicked.connect(self.close)
        ll.addWidget(close_loading_btn)
        card_layout.addWidget(self._loading_widget)

        self._quota_widget = QWidget()
        self._quota_widget.setFixedWidth(340)
        self._quota_widget.hide()
        ql = QVBoxLayout(self._quota_widget)
        ql.setContentsMargins(0, 0, 0, 0)
        ql.setSpacing(8)

        hdr = QHBoxLayout()
        hdr.setSpacing(6)
        hdr_star = QLabel("✦")
        hdr_star.setStyleSheet("color: #A78BFA; font-size: 12px; border: none; background: transparent;")
        hdr.addWidget(hdr_star)
        self._header_title = QLabel("Antigravity 剩余额度")
        self._header_title.setStyleSheet(
            "color: #F6F3FF; font-size: 14px; font-weight: bold; border: none; background: transparent;"
        )
        hdr.addWidget(self._header_title, 1)

        self._refresh_btn = self._make_ghost_icon_btn("↻", "刷新额度", 24, 13)
        self._refresh_btn.clicked.connect(lambda: self.start_refresh(show_loading_if_empty=True))
        hdr.addWidget(self._refresh_btn)

        close_btn = self._make_ghost_icon_btn("✕", "关闭", 24, 11)
        close_btn.clicked.connect(self.close)
        hdr.addWidget(close_btn)
        ql.addLayout(hdr)

        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._scroll.setStyleSheet(
            """
            QScrollArea { background: transparent; border: none; }
            QScrollBar:vertical {
                background: transparent;
                width: 5px;
                margin: 2px 0px;
            }
            QScrollBar::handle:vertical {
                background: #4E3F8E;
                border-radius: 2px;
                min-height: 20px;
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

        self._content_container = QWidget()
        self._content_container.setStyleSheet("background: transparent; border: none;")
        self._content_layout = QVBoxLayout(self._content_container)
        self._content_layout.setContentsMargins(0, 0, 0, 0)
        self._content_layout.setSpacing(6)
        self._scroll.setWidget(self._content_container)
        ql.addWidget(self._scroll)

        ftr = QHBoxLayout()
        ftr.setContentsMargins(0, 2, 0, 0)
        ftr.setSpacing(6)
        self._meta_label = QLabel("套餐 -  ·  --:--")
        self._meta_label.setMinimumWidth(0)
        self._meta_label.setStyleSheet("color: #877DB7; font-size: 10.5px; border: none; background: transparent;")
        ftr.addWidget(self._meta_label, 1)

        self._copy_btn = QPushButton("复制")
        self._copy_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._copy_btn.setToolTip("复制额度摘要")
        self._copy_btn.setStyleSheet(
            """
            QPushButton {
                background: transparent;
                color: #9B92C7;
                border: none;
                border-radius: 6px;
                padding: 2px 6px;
                font-size: 10.5px;
            }
            QPushButton:hover { background: #2A214E; color: #D6CCFF; }
            QPushButton:pressed { background: #4B3A8F; color: #FFFFFF; }
            """
        )
        self._copy_btn.setFixedWidth(36)
        self._copy_btn.clicked.connect(self._on_copy_clicked)
        ftr.addWidget(self._copy_btn)
        ql.addLayout(ftr)

        card_layout.addWidget(self._quota_widget)
        outer.addWidget(self._card)

    @staticmethod
    def _make_ghost_icon_btn(text: str, tooltip: str, size: int, font_px: int) -> QPushButton:
        btn = QPushButton(text)
        btn.setFixedSize(size, size)
        btn.setToolTip(tooltip)
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn.setStyleSheet(
            f"""
            QPushButton {{
                background: transparent;
                color: #9B92C7;
                border: none;
                border-radius: 8px;
                font-size: {font_px}px;
                padding: 0px;
            }}
            QPushButton:hover {{ background: #2D2454; color: #F6F3FF; }}
            QPushButton:pressed {{ background: #4B3A8F; color: #FFFFFF; }}
            """
        )
        return btn

    def ensure_on_top(self) -> None:
        """保持额度卡片位于桌宠前方。"""
        if not self.isVisible():
            return
        self.raise_()

    def _get_anchor_obstacles(self, sprite_rect: QRect) -> list[QRect]:
        if self._anchor_pet is None:
            return [sprite_rect]
        if hasattr(self._anchor_pet, "get_visual_obstacle_rects"):
            rects = list(self._anchor_pet.get_visual_obstacle_rects())
            return rects if rects else [sprite_rect]
        return [sprite_rect]

    def ensure_clear_of_anchor(self) -> None:
        if not self.isVisible():
            return
        self.ensure_on_top()
        if (
            not self._user_dragged
            and self._anchor_pet is not None
            and self._anchor_pet.isVisible()
            and hasattr(self._anchor_pet, "get_visual_sprite_bounds")
        ):
            sr = self._anchor_pet.get_visual_sprite_bounds()
            obstacles = self._get_anchor_obstacles(sr)
            shadow_margin = 14
            card_rect = self.frameGeometry().adjusted(
                shadow_margin, shadow_margin, -shadow_margin, -shadow_margin
            )
            if any(card_rect.intersects(obs.adjusted(-6, -6, 6, 6)) for obs in obstacles):
                self.reposition_near_anchor()

    def set_anchor(self, anchor: Optional[QWidget]) -> None:
        self._anchor_pet = anchor
        self._user_dragged = False
        if self.isVisible():
            self.reposition_near_anchor()

    def set_auto_close_enabled(self, enabled: bool) -> None:
        self._auto_close_enabled = enabled
        if enabled:
            self._auto_close_timer.start()
        else:
            self._auto_close_timer.stop()

    def show_loading_card(self) -> None:
        self._user_dragged = False
        self._reset_to_loading_view()
        self._activate_and_resize()
        self.reposition_near_anchor()
        self.show()
        self.ensure_on_top()

    def start_refresh(self, show_loading_if_empty: bool = True) -> None:
        self._auto_close_timer.stop()
        if show_loading_if_empty or self._last_snapshot is None:
            self._reset_to_loading_view()
            self._activate_and_resize()
            if not self._user_dragged:
                self.reposition_near_anchor()

        self._refresh_btn.setEnabled(False)
        self._refresh_btn.setText("…")

        if self._worker is not None and self._worker.isRunning():
            return

        self.query_started.emit()
        worker = _QuotaFetchWorker(self._quota_service, self)
        worker.finished_snapshot.connect(self._on_fetch_finished)
        self._worker = worker
        worker.start()

    def _on_fetch_finished(self, snap: QuotaSnapshot) -> None:
        self._refresh_btn.setEnabled(True)
        self._refresh_btn.setText("↻")
        self.apply_snapshot(snap)
        self.query_finished.emit(snap)

    def apply_snapshot(self, snapshot: QuotaSnapshot) -> None:
        self._last_snapshot = snapshot
        self._render_snapshot(snapshot)

    def _reset_to_loading_view(self) -> None:
        self._quota_widget.hide()
        self._loading_widget.show()
        self._loading_dots = 0
        self._loading_label.setText("让我看看额度还有多少。。。。。。")
        self._loading_timer.start()

    def _tick_loading_dots(self) -> None:
        self._loading_dots = (self._loading_dots + 1) % 6
        self._loading_label.setText("让我看看额度还有多少" + "。" * (1 + self._loading_dots))

    def _clear_content(self) -> None:
        while self._content_layout.count():
            item = self._content_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.hide()
                w.setParent(None)
                w.deleteLater()

    def _render_snapshot(self, snap: QuotaSnapshot) -> None:
        self._loading_timer.stop()
        self._loading_widget.hide()
        self._quota_widget.show()

        if not snap.success:
            self._render_error_card(snap.error_message or "未知错误")
            return

        self._header_title.setText("Antigravity 剩余额度")
        self._copy_btn.show()

        plan_txt = snap.account_plan or "套餐未知"
        fetched_local = snap.fetched_at.astimezone().strftime("%H:%M")
        full_meta = f"{plan_txt} · {fetched_local}"
        self._full_meta_text = full_meta
        self._update_elided_meta_text()
        tip_lines = [full_meta]
        if snap.account_email:
            tip_lines.append(f"账号：{snap.account_email}")
        if snap.source_detail:
            tip_lines.append(f"数据源：{snap.source_detail}")
        self._meta_label.setToolTip("\n".join(tip_lines))

        self._clear_content()

        for gi, group in enumerate(snap.groups):
            if gi > 0:
                sep = QFrame()
                sep.setFixedHeight(1)
                sep.setStyleSheet("background-color: #2C2352; border: none;")
                self._content_layout.addWidget(sep)

            if len(snap.groups) > 1:
                g_hdr = QLabel(self._format_group_title(group.name))
                g_hdr.setStyleSheet(
                    "color: #C4B5FD; font-size: 11.5px; font-weight: bold; border: none; background: transparent;"
                )
                if group.description:
                    g_hdr.setToolTip(group.description)
                self._content_layout.addWidget(g_hdr)

            ordered = sorted(
                group.buckets,
                key=lambda b: 0 if b.window.lower() == "5h" else (1 if b.window.lower() == "weekly" else 2),
            )
            for bucket in ordered:
                self._content_layout.addWidget(self._build_bucket_row(bucket, group.description))

        self._sync_scroll_height_and_resize()
        if not self._user_dragged and self._anchor_pet is not None and self.isVisible():
            self.reposition_near_anchor()
        self.ensure_on_top()
        if self._auto_close_enabled and not self.underMouse():
            self._auto_close_timer.start()

    def _render_error_card(self, message: str) -> None:
        self._header_title.setText("Antigravity 额度提示")
        self._copy_btn.hide()
        self._full_meta_text = f"{datetime.now().strftime('%H:%M:%S')} 查询失败"
        self._meta_label.setText(self._full_meta_text)
        self._meta_label.setToolTip("")
        self._clear_content()

        title = QLabel("⚠ 暂时无法读取额度")
        title.setStyleSheet("color: #FDA4AF; font-size: 13px; font-weight: bold; border: none; background: transparent;")
        self._content_layout.addWidget(title)

        msg = QLabel(message)
        msg.setWordWrap(True)
        msg.setStyleSheet("color: #E6E0FF; font-size: 12px; border: none; background: transparent;")
        self._content_layout.addWidget(msg)

        retry_btn = QPushButton("↻ 重新查询")
        retry_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        retry_btn.setStyleSheet(
            """
            QPushButton {
                background-color: #2D2454;
                color: #F4F0FF;
                border: 1px solid #5B47A6;
                border-radius: 10px;
                padding: 5px 12px;
                font-size: 12px;
            }
            QPushButton:hover { background-color: #4B3A8F; }
            QPushButton:pressed { background-color: #6D4AFF; }
            """
        )
        retry_btn.clicked.connect(lambda: self.start_refresh(show_loading_if_empty=True))
        self._content_layout.addWidget(retry_btn, 0, Qt.AlignmentFlag.AlignLeft)

        self._sync_scroll_height_and_resize()
        if not self._user_dragged and self._anchor_pet is not None and self.isVisible():
            self.reposition_near_anchor()
        self.ensure_on_top()
        if self._auto_close_enabled and not self.underMouse():
            self._auto_close_timer.start()

    def _update_elided_meta_text(self) -> None:
        """在 QSS 字体生效后按剩余宽度平滑省略底部套餐元信息。"""
        self._meta_label.ensurePolished()
        if self._meta_label.isVisible() and self._meta_label.width() > 40:
            avail_w = self._meta_label.width()
        else:
            copy_w = self._copy_btn.sizeHint().width() if self._copy_btn.isVisibleTo(self._quota_widget) else 0
            reserved = (copy_w + 6) if copy_w > 0 else 0
            avail_w = max(160, self._quota_widget.width() - reserved)
        fm = QFontMetrics(self._meta_label.font())
        elided = fm.elidedText(self._full_meta_text, Qt.TextElideMode.ElideRight, avail_w)
        self._meta_label.setText(elided)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._update_elided_meta_text()
        self.ensure_on_top()

    def _activate_and_resize(self) -> None:
        for layout in (
            self._content_layout,
            self._quota_widget.layout(),
            self._card.layout(),
            self.layout(),
        ):
            if layout is not None:
                layout.invalidate()
                layout.activate()
        self.adjustSize()
        if self.layout() is not None:
            target = self.layout().totalSizeHint()
            if target.isValid() and target.width() > 0 and target.height() > 0:
                self.resize(target)

    def _screen_available_geometry(self) -> QRect:
        if self._anchor_pet is not None and self._anchor_pet.isVisible():
            center = self._anchor_pet.frameGeometry().center()
            screen = QApplication.screenAt(center) or self._anchor_pet.screen()
            if screen is not None:
                return screen.availableGeometry()
        screen = self.screen() or QApplication.primaryScreen()
        return screen.availableGeometry() if screen is not None else QRect(0, 0, 1920, 1080)

    def _sync_scroll_height_and_resize(self) -> None:
        """按实际内容高度同步 QScrollArea 高度，常见分组全量展示且不显示多余滚动条。"""
        avail_h = self._screen_available_geometry().height()
        max_scroll_h = max(240, min(620, avail_h - 160))
        # 显式 show/polish 新建子控件，避免窗口已显示时二次刷新取到 0 高度
        for i in range(self._content_layout.count()):
            w = self._content_layout.itemAt(i).widget()
            if w is not None:
                w.ensurePolished()
                w.show()
                for child in w.findChildren(QWidget):
                    child.ensurePolished()
                    child.show()

        self._content_layout.setContentsMargins(0, 0, 0, 0)
        self._content_container.setFixedWidth(self._quota_widget.width())
        self._content_layout.invalidate()
        self._content_layout.activate()
        content_h = max(
            36,
            self._content_layout.totalSizeHint().height() + 4,
            self._content_container.sizeHint().height() + 4,
        )

        needs_scroll = content_h > max_scroll_h
        if needs_scroll:
            self._content_layout.setContentsMargins(0, 0, 6, 0)
            self._content_container.setFixedWidth(max(120, self._quota_widget.width() - 6))
            self._content_layout.invalidate()
            self._content_layout.activate()
            content_h = max(
                content_h,
                self._content_layout.totalSizeHint().height() + 4,
                self._content_container.sizeHint().height() + 4,
            )

        self._content_container.setMinimumWidth(0)
        self._content_container.setMaximumWidth(16777215)
        self._scroll.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded if needs_scroll else Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self._scroll.setFixedHeight(min(content_h, max_scroll_h))
        self._activate_and_resize()
        self._update_elided_meta_text()

    def _build_bucket_row(self, bucket: QuotaBucketInfo, group_desc: Optional[str]) -> QWidget:
        pct = max(0.0, min(100.0, bucket.remaining_fraction * 100.0))
        pct_str = f"{pct:g}" if abs(pct - round(pct)) < 0.05 else f"{pct:.1f}"
        row = QWidget()
        row.setStyleSheet("background: transparent; border: none;")
        row.setToolTip(self._build_bucket_tooltip(bucket, group_desc))
        vl = QVBoxLayout(row)
        vl.setContentsMargins(0, 0, 0, 4)
        vl.setSpacing(4)

        lbl = QLabel(f"{self._format_window_label(bucket)} · 剩余 {pct_str}%")
        lbl.setWordWrap(True)
        lbl.setStyleSheet("color: #F5F2FF; font-size: 13px; border: none; background: transparent;")
        vl.addWidget(lbl)

        bar = QProgressBar()
        bar.setRange(0, 1000)
        bar.setValue(int(round(bucket.remaining_fraction * 1000)))
        bar.setTextVisible(False)
        bar.setFixedHeight(12)
        chunk_grad = (
            "qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #7C5CFF, stop:0.55 #A78BFA, stop:1 #C4B5FD)"
            if pct >= 25.0
            else "qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #DB2777, stop:0.55 #F472B6, stop:1 #FB7185)"
        )
        bar.setStyleSheet(
            f"""
            QProgressBar {{
                background-color: #251D4A;
                border: 1px solid #493A85;
                border-radius: 6px;
            }}
            QProgressBar::chunk {{
                background: {chunk_grad};
                border-radius: 5px;
            }}
            """
        )
        vl.addWidget(bar)

        reset_line = self._format_short_reset_line(bucket)
        if reset_line:
            rl = QLabel(reset_line)
            rl.setWordWrap(True)
            rl.setStyleSheet("color: #A59AD1; font-size: 11.5px; border: none; background: transparent;")
            vl.addWidget(rl)

        return row

    def reposition_near_anchor(self) -> None:
        self._activate_and_resize()
        target = self.layout().totalSizeHint() if self.layout() is not None else self.size()
        w = max(250, self.width(), target.width())
        h = max(68, self.height(), target.height())
        shadow_margin = 14
        vis_w = max(120, w - shadow_margin * 2)
        vis_h = max(40, h - shadow_margin * 2)
        gap = 8

        if self._anchor_pet is not None and self._anchor_pet.isVisible() and hasattr(
            self._anchor_pet, "get_visual_sprite_bounds"
        ):
            sr = self._anchor_pet.get_visual_sprite_bounds()
            obstacles = self._get_anchor_obstacles(sr)
            avoid_union = obstacles[0]
            for obs in obstacles[1:]:
                avoid_union = avoid_union.united(obs)

            cx = sr.x() + sr.width() // 2
            cy = sr.y() + sr.height() // 2
            screen = QApplication.screenAt(QPoint(cx, cy)) or QApplication.primaryScreen()
            area = screen.availableGeometry() if screen is not None else QRect(0, 0, 1920, 1080)

            prefer_left = cx >= area.x() + area.width() // 2
            # 优先贴合角色头部高度，避免默认沉底或被头顶浮层推离人物
            head_y = max(
                area.y() + 6,
                min(
                    sr.top() + min(24, max(0, int(round(sr.height() * 0.06)))),
                    area.bottom() - vis_h - 6,
                ),
            )
            left_head_x = sr.left() - gap - vis_w
            right_head_x = sr.right() + 1 + gap
            side_order = (left_head_x, right_head_x) if prefer_left else (right_head_x, left_head_x)

            raw_candidates: list[tuple[int, int]] = []
            for sx in side_order:
                raw_candidates.append((sx, head_y))
                for obs in obstacles[1:]:
                    raw_candidates.append((sx, obs.bottom() + 1 + gap))

            left_union = (avoid_union.left() - gap - vis_w, head_y)
            right_union = (avoid_union.right() + 1 + gap, head_y)
            raw_candidates.extend([left_union, right_union] if prefer_left else [right_union, left_union])

            for sx in side_order:
                for obs in obstacles[1:]:
                    raw_candidates.append((sx, obs.top() - gap - vis_h))

            above_sprite = (sr.center().x() - vis_w // 2, avoid_union.top() - gap - vis_h)
            below_sprite = (sr.center().x() - vis_w // 2, avoid_union.bottom() + 1 + gap)
            raw_candidates.extend([above_sprite, below_sprite])

            best_pos = (area.x() + 8, area.y() + 8)
            best_score: Optional[tuple[int, int, int, int]] = None
            for idx, (cand_x, cand_y) in enumerate(raw_candidates):
                clamped_x = max(area.x() + 6, min(cand_x, area.right() - vis_w - 6))
                clamped_y = max(area.y() + 6, min(cand_y, area.bottom() - vis_h - 6))
                cand_rect = QRect(clamped_x, clamped_y, vis_w, vis_h)
                obs_area = 0
                for obs in obstacles:
                    inter = cand_rect.intersected(obs)
                    if not inter.isEmpty():
                        obs_area += inter.width() * inter.height()
                inter_sprite = cand_rect.intersected(sr)
                sprite_area = (
                    inter_sprite.width() * inter_sprite.height()
                    if not inter_sprite.isEmpty()
                    else 0
                )
                clamp_shift = abs(clamped_x - cand_x) + abs(clamped_y - cand_y)
                score = (obs_area, sprite_area, clamp_shift, idx)
                if best_score is None or score < best_score:
                    best_score = score
                    best_pos = (clamped_x, clamped_y)
                if obs_area == 0 and clamp_shift == 0:
                    break

            self.move(best_pos[0] - shadow_margin, best_pos[1] - shadow_margin)
        else:
            area = self._screen_available_geometry()
            self.move(
                max(area.x() + 8, area.right() - vis_w - 12) - shadow_margin,
                max(area.y() + 8, area.bottom() - vis_h - 12) - shadow_margin,
            )
        self.ensure_on_top()

    @staticmethod
    def _format_group_title(name: str) -> str:
        if name == "Gemini Models":
            return "Gemini 模型组"
        if name == "Claude and GPT models":
            return "Claude / GPT 模型组"
        return name

    @staticmethod
    def _format_window_label(b: QuotaBucketInfo) -> str:
        if b.window.lower() == "5h":
            return "5小时"
        if b.window.lower() == "weekly" and "argon" not in b.name.lower():
            return "一周"
        return b.name

    @staticmethod
    def _format_short_reset_line(b: QuotaBucketInfo) -> str:
        if not b.reset_time:
            return b.description or ""
        lt = b.reset_time.astimezone()
        return f"重置: {lt.month}月{lt.day}日 {lt.strftime('%H:%M')}"

    @staticmethod
    def _build_bucket_tooltip(b: QuotaBucketInfo, group_desc: Optional[str]) -> str:
        countdown = ""
        if b.reset_time:
            lt = b.reset_time.astimezone()
            now = datetime.now(lt.tzinfo)
            secs = (lt - now).total_seconds()
            ts = lt.strftime("%Y-%m-%d %H:%M")
            if secs <= 0:
                countdown = f"即将刷新 ({ts})"
            elif secs >= 86400:
                days = int(secs // 86400)
                hours = int((secs % 86400) // 3600)
                countdown = f"{days} 天 {hours} 小时后重置 ({ts})"
            elif secs >= 3600:
                hours = int(secs // 3600)
                mins = int((secs % 3600) // 60)
                countdown = f"{hours} 小时 {mins} 分钟后重置 ({ts})"
            else:
                mins = max(1, int((secs + 59) // 60))
                countdown = f"{mins} 分钟后重置 ({ts})"

        parts = [b.name]
        if countdown:
            parts.append(countdown)
        if group_desc:
            parts.append(group_desc)
        return "\n".join(parts)

    def _on_copy_clicked(self) -> None:
        if self._last_snapshot is None or not self._last_snapshot.success:
            return
        QApplication.clipboard().setText(self._last_snapshot.format_clipboard_text())
        self._copy_btn.setText("✓ 已复制")
        QTimer.singleShot(1500, lambda: self._copy_btn.setText("复制"))

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._user_dragged = True
            self._drag_offset = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self._drag_offset is not None and (event.buttons() & Qt.MouseButton.LeftButton):
            self.move(event.globalPosition().toPoint() - self._drag_offset)
            event.accept()
            return
        super().mouseMoveEvent(event)

    def enterEvent(self, event) -> None:
        self._auto_close_timer.stop()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        if self._auto_close_enabled and self._loading_widget.isHidden():
            self._auto_close_timer.start()
        super().leaveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        self._drag_offset = None
        super().mouseReleaseEvent(event)

    def closeEvent(self, event) -> None:
        self._auto_close_timer.stop()
        self._loading_timer.stop()
        if self._worker is not None and self._worker.isRunning():
            self._worker.requestInterruption()
            self._worker.wait(500)
        super().closeEvent(event)
