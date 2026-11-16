"""角色档案、立绘三视图与 Q 版、标准版动作图鉴窗口。"""

from __future__ import annotations

import os
from typing import Callable, Optional

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QColor, QDesktopServices, QPixmap
from PySide6.QtWidgets import (
    QFrame,
    QGraphicsDropShadowEffect,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from models.app_version import get_bundled_resource_dir
from models.character_lore import CharacterLore
from models.pet_settings import PetSettings, PetStyleMode
from models.pet_state import PetState
from services.sprite_manager import SpriteManager
from services.tray_icon_service import TrayIconService
from themes.starlight_theme import STARLIGHT_WINDOW_QSS


class ProfileWindow(QWidget):
    """展示当前生效角色档案、三视图设定及可点击试演的动作图鉴。"""

    def __init__(
        self,
        sprite_manager: SpriteManager,
        settings: PetSettings,
        on_preview_state: Callable[[PetState, PetStyleMode], None],
        on_open_editor: Optional[Callable[[], None]] = None,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._sprites = sprite_manager
        self._settings = settings
        self._on_preview_state = on_preview_state
        self._on_open_editor = on_open_editor

        self.setWindowTitle("Gemini 娘 · 角色档案与设定图鉴")
        self.setWindowIcon(TrayIconService.load_app_icon())
        self.resize(880, 660)
        self.setMinimumSize(720, 540)
        self.setStyleSheet(STARLIGHT_WINDOW_QSS)

        self._build_ui()
        self.refresh_profile()

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(20, 20, 20, 20)
        root.setSpacing(14)

        header = QFrame()
        header.setObjectName("HeaderCard")
        hl = QHBoxLayout(header)
        hl.setContentsMargins(18, 14, 18, 14)

        left_col = QVBoxLayout()
        top_badges = QHBoxLayout()
        top_badges.setSpacing(8)
        self._name_lbl = QLabel("✦ Gemini 娘")
        self._name_lbl.setStyleSheet("font-size: 22px; font-weight: bold; color: #FFFFFF; background: transparent;")
        top_badges.addWidget(self._name_lbl)

        self._alias_badge = QLabel("双子娘 · Q版猫娘桌宠")
        self._alias_badge.setStyleSheet(
            "background-color: #3B2B72; color: #D8CCFF; border-radius: 8px; padding: 3px 8px; font-size: 11.5px;"
        )
        top_badges.addWidget(self._alias_badge)

        self._tag_badge = QLabel("左玫红 × 右琥珀金 异色瞳")
        self._tag_badge.setStyleSheet(
            "background-color: #3B1A3A; color: #F9A8D4; border-radius: 8px; padding: 3px 8px; font-size: 11.5px;"
        )
        top_badges.addWidget(self._tag_badge)
        top_badges.addStretch(1)
        left_col.addLayout(top_badges)

        self._subtitle_lbl = QLabel()
        self._subtitle_lbl.setWordWrap(True)
        self._subtitle_lbl.setStyleSheet("color: #B5AED8; font-size: 12.5px; margin-top: 6px; background: transparent;")
        left_col.addWidget(self._subtitle_lbl)
        hl.addLayout(left_col, 1)

        right_col = QVBoxLayout()
        right_col.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self._bond_lbl = QLabel()
        self._bond_lbl.setStyleSheet("color: #FBBF24; font-size: 13px; font-weight: bold; background: transparent;")
        self._bond_lbl.setAlignment(Qt.AlignmentFlag.AlignRight)
        right_col.addWidget(self._bond_lbl)

        self._source_link_btn = QPushButton("来源：genderswapai.com/zh-CN/ai-gijinka/gemini")
        self._source_link_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._source_link_btn.setStyleSheet(
            "background: transparent; border: none; color: #8E86BA; font-size: 11px; text-decoration: underline; padding: 2px;"
        )
        self._source_link_btn.clicked.connect(self._open_source_url)
        right_col.addWidget(self._source_link_btn, 0, Qt.AlignmentFlag.AlignRight)

        edit_btn = QPushButton("✏️ 自定义角色设定...")
        edit_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        edit_btn.setStyleSheet(
            "background-color: #32285E; color: #FFFFFF; border: 1px solid #5B49A8; border-radius: 8px; padding: 5px 10px; font-size: 11.5px;"
        )
        edit_btn.clicked.connect(self._handle_edit_clicked)
        right_col.addWidget(edit_btn, 0, Qt.AlignmentFlag.AlignRight)
        hl.addLayout(right_col)

        root.addWidget(header)

        self._tabs = QTabWidget()
        self._tabs.addTab(self._build_profile_tab(), "✦ 角色档案与设定")
        self._tabs.addTab(self._build_turnaround_tab(), "📐 角色三视图参考")
        self._gallery_cards = []
        self._gallery_tabs = {}
        for style, title in (
            (PetStyleMode.CHIBI, "🐱 Q版动作演示"),
            (PetStyleMode.STANDARD, "✦ 标准版动作演示"),
        ):
            self._gallery_tabs[style] = self._tabs.addTab(self._build_gallery_tab(style), title)
        root.addWidget(self._tabs, 1)

        footer = QFrame()
        footer.setObjectName("FooterCard")
        fl = QVBoxLayout(footer)
        fl.setContentsMargins(12, 8, 12, 8)
        self._footer_lbl = QLabel()
        self._footer_lbl.setWordWrap(True)
        self._footer_lbl.setStyleSheet("color: #9B93C4; font-size: 11.5px; background: transparent;")
        fl.addWidget(self._footer_lbl)
        root.addWidget(footer)

    def _build_profile_tab(self) -> QWidget:
        w = QWidget()
        hl = QHBoxLayout(w)
        hl.setContentsMargins(0, 12, 0, 0)
        hl.setSpacing(16)

        standee_card = QFrame()
        standee_card.setObjectName("CardPanel")
        standee_card.setFixedWidth(300)
        sl = QVBoxLayout(standee_card)
        sl.setContentsMargins(12, 12, 12, 12)

        img_box = QFrame()
        img_box.setStyleSheet("background-color: #130F26; border: 1px solid #2D2454; border-radius: 10px;")
        ibl = QVBoxLayout(img_box)
        ibl.setContentsMargins(6, 6, 6, 6)
        self._standee_lbl = QLabel()
        self._standee_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._standee_lbl.setStyleSheet("border: none; background: transparent;")
        shadow = QGraphicsDropShadowEffect(self._standee_lbl)
        shadow.setBlurRadius(16)
        shadow.setColor(QColor("#7C5CFF"))
        shadow.setOffset(0, 2)
        self._standee_lbl.setGraphicsEffect(shadow)
        ibl.addWidget(self._standee_lbl, 1)
        sl.addWidget(img_box, 1)

        self._standee_caption_lbl = QLabel()
        self._standee_caption_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._standee_caption_lbl.setWordWrap(True)
        self._standee_caption_lbl.setStyleSheet("color: #A59EC9; font-size: 11.5px; margin-top: 6px; background: transparent;")
        sl.addWidget(self._standee_caption_lbl)
        hl.addWidget(standee_card)

        right_scroll = QScrollArea()
        right_scroll.setWidgetResizable(True)
        right_inner = QWidget()
        self._traits_host_layout = QVBoxLayout(right_inner)
        self._traits_host_layout.setContentsMargins(0, 0, 6, 0)
        self._traits_host_layout.setSpacing(10)

        bio_card = QFrame()
        bio_card.setObjectName("CardPanel")
        bl = QVBoxLayout(bio_card)
        bl.setContentsMargins(14, 14, 14, 14)
        bio_title = QLabel("角色简介")
        bio_title.setStyleSheet("font-size: 15px; font-weight: bold; color: #E9E4FF; background: transparent;")
        bl.addWidget(bio_title)
        self._bio_lbl = QLabel()
        self._bio_lbl.setWordWrap(True)
        self._bio_lbl.setStyleSheet("font-size: 13px; color: #CFC8EE; background: transparent; margin-top: 4px;")
        bl.addWidget(self._bio_lbl)
        self._traits_host_layout.addWidget(bio_card)

        traits_hdr = QLabel("外貌与设定要点")
        traits_hdr.setStyleSheet("font-size: 15px; font-weight: bold; color: #E9E4FF; background: transparent;")
        self._traits_host_layout.addWidget(traits_hdr)

        self._traits_container = QVBoxLayout()
        self._traits_container.setSpacing(8)
        self._traits_host_layout.addLayout(self._traits_container)
        self._traits_host_layout.addStretch(1)

        right_scroll.setWidget(right_inner)
        hl.addWidget(right_scroll, 1)
        return w

    def _build_turnaround_tab(self) -> QWidget:
        w = QFrame()
        w.setObjectName("CardPanel")
        vl = QVBoxLayout(w)
        vl.setContentsMargins(14, 14, 14, 14)
        self._turnaround_lbl = QLabel()
        self._turnaround_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._turnaround_lbl.setStyleSheet("background: transparent; border: none;")
        shadow = QGraphicsDropShadowEffect(self._turnaround_lbl)
        shadow.setBlurRadius(18)
        shadow.setColor(QColor("#7C5CFF"))
        shadow.setOffset(0, 2)
        self._turnaround_lbl.setGraphicsEffect(shadow)
        vl.addWidget(self._turnaround_lbl, 1)

        self._turnaround_caption_lbl = QLabel()
        self._turnaround_caption_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._turnaround_caption_lbl.setWordWrap(True)
        self._turnaround_caption_lbl.setStyleSheet("color: #B5AED8; font-size: 12px; margin-top: 6px; background: transparent;")
        vl.addWidget(self._turnaround_caption_lbl)
        return w

    def _build_gallery_tab(self, style: PetStyleMode) -> QWidget:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        container = QWidget()
        grid = QGridLayout(container)
        grid.setContentsMargins(0, 12, 6, 0)
        grid.setSpacing(10)

        cols = 6
        for idx, state in enumerate(PetState):
            pix = self._sprites.get(state, style)
            if pix is None:
                continue
            card = QPushButton()
            card.setCursor(Qt.CursorShape.PointingHandCursor)
            card.setFixedSize(118, 144)
            card.setStyleSheet(
                """
                QPushButton {
                    background-color: #1D1836;
                    border: 1px solid #362C63;
                    border-radius: 12px;
                    padding: 6px;
                }
                QPushButton:hover {
                    background-color: #2A2250;
                    border-color: #7C5CFF;
                }
                """
            )
            cl = QVBoxLayout(card)
            cl.setContentsMargins(6, 6, 6, 6)
            cl.setSpacing(6)
            img = QLabel()
            img.setAlignment(Qt.AlignmentFlag.AlignCenter)
            img.setStyleSheet("background: transparent; border: none;")
            img.setPixmap(pix.scaled(98, 102, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))
            cl.addWidget(img, 1)
            self._gallery_cards.append((state, style, img))

            lbl = QLabel(state.to_sprite_key())
            lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            lbl.setStyleSheet("color: #D7D0F5; font-size: 11px; background: transparent; border: none;")
            cl.addWidget(lbl)

            card.setObjectName(f"preview_{style.value}_{state.value}")
            card.setToolTip("以此形象演示动作，并保留形象选择")
            card.clicked.connect(lambda _=False, st=state, mode=style: self._on_preview_state(st, mode))
            grid.addWidget(card, idx // cols, idx % cols)

        scroll.setWidget(container)
        return scroll

    def refresh_profile(self, settings: Optional[PetSettings] = None) -> None:
        if settings is not None:
            self._settings = settings
        # 正在浏览动作页时跟随桌宠形象切换；档案和三视图页不跳转。
        if self._tabs.currentIndex() in self._gallery_tabs.values():
            self._tabs.setCurrentIndex(self._gallery_tabs[self._settings.style_mode])
        for state, style, label in self._gallery_cards:
            pix = self._sprites.get(state, style)
            if pix is not None:
                label.setPixmap(pix.scaled(98, 102, Qt.AspectRatioMode.KeepAspectRatio,
                                          Qt.TransformationMode.SmoothTransformation))
        p = CharacterLore.get_active_profile()

        self._name_lbl.setText(f"✦ {p.name}")
        self._alias_badge.setText(p.alias)
        self._tag_badge.setText(p.badge_tag)
        self._subtitle_lbl.setText(p.subtitle)
        self._bio_lbl.setText(p.bio)
        self._standee_caption_lbl.setText(p.standee_caption)
        self._turnaround_caption_lbl.setText(p.turnaround_caption)
        self._footer_lbl.setText(p.footer_attribution)
        self._bond_lbl.setText(f"好感度 {self._settings.favorability} ✦ 累计摸头 {self._settings.total_pats} 次")

        source_url = (p.source_page_url or "").strip()
        short_url = source_url.removeprefix("https://").removeprefix("http://")
        self._source_link_btn.setText(f"来源：{short_url}" if short_url else "")
        self._source_link_btn.setVisible(bool(short_url))

        res_dir = os.path.join(get_bundled_resource_dir(), "Assets", "Profile")
        standee_path = p.custom_standee_path if os.path.isfile(p.custom_standee_path) else os.path.join(res_dir, "gemini_standee.png")
        turn_path = p.custom_turnaround_path if os.path.isfile(p.custom_turnaround_path) else os.path.join(res_dir, "gemini_turnaround.png")

        self._standee_lbl.setPixmap(QPixmap(standee_path).scaled(260, 400, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))
        self._turnaround_lbl.setPixmap(QPixmap(turn_path).scaled(780, 420, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))

        while self._traits_container.count():
            item = self._traits_container.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()

        for trait in p.appearance_traits:
            card = QFrame()
            card.setObjectName("CardPanel")
            tl = QVBoxLayout(card)
            tl.setContentsMargins(12, 10, 12, 10)
            t_lbl = QLabel(trait.title)
            t_lbl.setStyleSheet("color: #C4B5FD; font-size: 13.5px; font-weight: bold; background: transparent;")
            tl.addWidget(t_lbl)
            d_lbl = QLabel(trait.detail)
            d_lbl.setWordWrap(True)
            d_lbl.setStyleSheet("color: #BDB6DF; font-size: 12.5px; background: transparent; margin-top: 2px;")
            tl.addWidget(d_lbl)
            self._traits_container.addWidget(card)

    def _open_source_url(self) -> None:
        url = CharacterLore.get_active_profile().source_page_url
        if url:
            QDesktopServices.openUrl(QUrl(url))

    def _handle_edit_clicked(self) -> None:
        if self._on_open_editor:
            self._on_open_editor()
