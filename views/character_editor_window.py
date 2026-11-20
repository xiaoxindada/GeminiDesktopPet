"""可视化角色设定工坊窗口：性格预设、立绘替换、AI 人设与台词自定义。"""

from __future__ import annotations

import copy
import os
from typing import Callable, Optional

from PySide6.QtCore import Qt, QTimer, QUrl
from PySide6.QtGui import QDesktopServices, QPixmap
from PySide6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from models.app_version import get_bundled_resource_dir
from models.character_lore import (
    AI_TONE_OPTIONS,
    BUBBLE_ACTIVITY_OPTIONS,
    BUILTIN_PRESETS,
    DEFAULT_LINES,
    IDLE_EDITOR_LINE_CATEGORIES,
    CharacterLore,
)
from models.character_profile import CharacterProfile
from services.character_profile_service import CharacterProfileService
from services.tray_icon_service import TrayIconService
from themes.starlight_theme import STARLIGHT_WINDOW_QSS


class CharacterEditorWindow(QWidget):
    """无需手写 JSON 的可视化角色自定义编辑器，保存后即时生效。"""

    def __init__(
        self,
        profile_service: CharacterProfileService,
        on_profile_applied: Callable[[CharacterProfile], None],
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._service = profile_service
        self._on_applied = on_profile_applied
        self._suppress_tone = False
        self._suppress_action_line_sync = False
        self._custom_standee_path = ""
        self._working_profile: CharacterProfile = self._service.load()
        self._editable_lines: dict[str, list[str]] = {}
        self._current_action_line_category: str = IDLE_EDITOR_LINE_CATEGORIES[0][0]

        self.setWindowTitle("✏️ Gemini 娘 · 自定义角色设定与台词工坊")
        self.setWindowIcon(TrayIconService.load_app_icon())
        self.resize(780, 720)
        self.setMinimumSize(680, 580)
        self.setStyleSheet(STARLIGHT_WINDOW_QSS)

        self._build_ui()
        self._load_into_form(self._working_profile)

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(18, 18, 18, 18)
        root.setSpacing(12)

        # Header
        header = QFrame()
        header.setObjectName("HeaderCard")
        hl = QHBoxLayout(header)
        hl.setContentsMargins(16, 12, 16, 12)
        hl_left = QVBoxLayout()
        t = QLabel("✏️ 可视化角色设定工坊")
        t.setStyleSheet("font-size: 19px; font-weight: bold; color: #FFFFFF; background: transparent;")
        hl_left.addWidget(t)
        sub = QLabel("无需手写配置文件：点选预设模板或直接填写名称、性格、台词与立绘即可定制；留空部分将自动使用默认 Gemini 娘设定。")
        sub.setWordWrap(True)
        sub.setStyleSheet("color: #B5AED8; font-size: 12px; background: transparent;")
        hl_left.addWidget(sub)
        hl.addLayout(hl_left, 1)

        reset_btn = QPushButton("↺ 恢复默认设定")
        reset_btn.setObjectName("DangerBtn")
        reset_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        reset_btn.clicked.connect(self._on_reset_defaults)
        hl.addWidget(reset_btn)
        root.addWidget(header)

        # Scroll body
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        body = QWidget()
        bl = QVBoxLayout(body)
        bl.setContentsMargins(0, 0, 6, 0)
        bl.setSpacing(12)

        # ① Presets card
        c1 = QFrame()
        c1.setObjectName("CardPanel")
        c1_l = QVBoxLayout(c1)
        c1_l.setContentsMargins(14, 14, 14, 14)
        c1_t = QLabel("① 一键性格预设（点击可即时预览并继续微调）")
        c1_t.setStyleSheet("font-size: 13.5px; font-weight: bold; color: #C4B5FD; background: transparent;")
        c1_l.addWidget(c1_t)
        p_row = QHBoxLayout()
        p_row.setSpacing(8)
        for preset in BUILTIN_PRESETS:
            btn = QPushButton(preset.button_label)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.clicked.connect(lambda _=False, pr=preset: self._apply_preset(pr))
            p_row.addWidget(btn)
        p_row.addStretch(1)
        c1_l.addLayout(p_row)
        bl.addWidget(c1)

        # ② Standee preview + Core identity
        c2 = QFrame()
        c2.setObjectName("CardPanel")
        c2_l = QHBoxLayout(c2)
        c2_l.setContentsMargins(14, 14, 14, 14)
        c2_l.setSpacing(16)

        standee_box = QFrame()
        standee_box.setFixedWidth(182)
        standee_box.setStyleSheet("background-color: #130F26; border: 1px solid #2E2554; border-radius: 12px;")
        sbl = QVBoxLayout(standee_box)
        sbl.setContentsMargins(10, 10, 10, 10)
        self._standee_preview = QLabel()
        self._standee_preview.setFixedHeight(186)
        self._standee_preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._standee_preview.setStyleSheet("background: transparent; border: none;")
        sbl.addWidget(self._standee_preview)
        pick_standee = QPushButton("🖼 更换展示立绘...")
        pick_standee.setCursor(Qt.CursorShape.PointingHandCursor)
        pick_standee.clicked.connect(self._pick_standee)
        sbl.addWidget(pick_standee)
        clear_standee = QPushButton("用回默认立绘")
        clear_standee.setCursor(Qt.CursorShape.PointingHandCursor)
        clear_standee.clicked.connect(self._clear_standee)
        sbl.addWidget(clear_standee)
        c2_l.addWidget(standee_box)

        core_col = QVBoxLayout()
        core_col.setSpacing(6)
        c2_t = QLabel("② 基本角色信息")
        c2_t.setStyleSheet("font-size: 13.5px; font-weight: bold; color: #C4B5FD; background: transparent;")
        core_col.addWidget(c2_t)

        r_name = QHBoxLayout()
        r_name.setSpacing(10)
        n_col = QVBoxLayout()
        n_col.addWidget(self._sub_label("角色昵称（气泡 & 档案显示）"))
        self._name_edit = QLineEdit()
        n_col.addWidget(self._name_edit)
        r_name.addLayout(n_col, 1)

        a_col = QVBoxLayout()
        a_col.addWidget(self._sub_label("副称号 / 小标签"))
        self._alias_edit = QLineEdit()
        a_col.addWidget(self._alias_edit)
        r_name.addLayout(a_col, 1)
        core_col.addLayout(r_name)

        core_col.addWidget(self._sub_label("角色性格与背景简介"))
        self._bio_edit = QPlainTextEdit()
        self._bio_edit.setMinimumHeight(68)
        self._bio_edit.setMaximumHeight(92)
        core_col.addWidget(self._bio_edit)

        core_col.addWidget(self._sub_label("特征标签 / 顶部简介（可选，留空自动沿用默认）"))
        r_tag = QHBoxLayout()
        r_tag.setSpacing(10)
        self._badge_edit = QLineEdit()
        self._badge_edit.setFixedWidth(190)
        self._subtitle_edit = QLineEdit()
        r_tag.addWidget(self._badge_edit)
        r_tag.addWidget(self._subtitle_edit, 1)
        core_col.addLayout(r_tag)
        c2_l.addLayout(core_col, 1)
        bl.addWidget(c2)

        # ③ AI Tone, Bubble Activity & Lines
        c3 = QFrame()
        c3.setObjectName("CardPanel")
        c3_l = QVBoxLayout(c3)
        c3_l.setContentsMargins(14, 14, 14, 14)
        c3_l.setSpacing(8)
        c3_t = QLabel("③ 说话风格、气泡活跃度与台词")
        c3_t.setStyleSheet("font-size: 13.5px; font-weight: bold; color: #C4B5FD; background: transparent;")
        c3_l.addWidget(c3_t)

        tone_row = QHBoxLayout()
        tone_lbl = QLabel("AI 对话说话风格")
        tone_lbl.setFixedWidth(125)
        tone_lbl.setStyleSheet("color: #D4CEF5; background: transparent;")
        tone_row.addWidget(tone_lbl)
        self._tone_combo = QComboBox()
        for opt in AI_TONE_OPTIONS:
            self._tone_combo.addItem(opt.label, opt)
        self._tone_combo.currentIndexChanged.connect(self._on_tone_changed)
        tone_row.addWidget(self._tone_combo, 1)
        c3_l.addLayout(tone_row)

        act_row = QHBoxLayout()
        act_lbl = QLabel("气泡活跃程度")
        act_lbl.setFixedWidth(125)
        act_lbl.setStyleSheet("color: #D4CEF5; background: transparent;")
        act_row.addWidget(act_lbl)
        self._bubble_activity_combo = QComboBox()
        self._bubble_activity_combo.setToolTip(
            "开启「弹出默认气泡」后，控制无操作时随机说话与切换动作的频率"
        )
        for opt in BUBBLE_ACTIVITY_OPTIONS:
            self._bubble_activity_combo.addItem(opt.label, opt.id)
        act_row.addWidget(self._bubble_activity_combo, 1)
        c3_l.addLayout(act_row)

        c3_l.addWidget(self._sub_label("AI 性格人设细节（选上方预设会自动填充；留空则自动根据角色信息生成）"))
        self._ai_prompt_edit = QPlainTextEdit()
        self._ai_prompt_edit.setMinimumHeight(60)
        self._ai_prompt_edit.setMaximumHeight(90)
        c3_l.addWidget(self._ai_prompt_edit)

        lines_row = QHBoxLayout()
        lines_row.setSpacing(10)
        self._greeting_edit = self._make_line_col(lines_row, "打招呼说的话（每行一条）", 82)
        self._pat_edit = self._make_line_col(lines_row, "摸头时说的话（每行一条）", 82)
        self._idle_edit = self._make_line_col(lines_row, "平时发呆说的话（每行一条）", 82)
        c3_l.addLayout(lines_row)

        action_hdr = QHBoxLayout()
        action_hdr.setSpacing(8)
        action_lbl = QLabel("随机气泡与动作台词")
        action_lbl.setFixedWidth(125)
        action_lbl.setStyleSheet("color: #D4CEF5; background: transparent;")
        action_hdr.addWidget(action_lbl)
        self._action_line_category_combo = QComboBox()
        for cat_key, cat_label in IDLE_EDITOR_LINE_CATEGORIES:
            self._action_line_category_combo.addItem(cat_label, cat_key)
        self._action_line_category_combo.currentIndexChanged.connect(
            self._on_action_line_category_changed
        )
        action_hdr.addWidget(self._action_line_category_combo, 1)
        self._reset_action_line_btn = QPushButton("↺ 还原该类默认")
        self._reset_action_line_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._reset_action_line_btn.clicked.connect(self._reset_current_action_lines)
        action_hdr.addWidget(self._reset_action_line_btn)
        c3_l.addLayout(action_hdr)
        action_hint = self._sub_label(
            "格式注意：一行一句纯文本（无需引号/序号，空行忽略，留空保存恢复默认）；切换上方分类可编辑对应动作台词。"
        )
        action_hint.setWordWrap(True)
        c3_l.addWidget(action_hint)
        self._action_line_edit = QPlainTextEdit()
        self._action_line_edit.setFixedHeight(92)
        c3_l.addWidget(self._action_line_edit)
        bl.addWidget(c3)

        # ④ Collapsible Advanced
        c4 = QFrame()
        c4.setObjectName("TipCard")
        c4_l = QVBoxLayout(c4)
        c4_l.setContentsMargins(12, 10, 12, 10)
        self._adv_toggle = QPushButton("▶ ④ 展开更多设定（自定义三视图 / JSON 文件）")
        self._adv_toggle.setCursor(Qt.CursorShape.PointingHandCursor)
        self._adv_toggle.setStyleSheet(
            "text-align: left; background: transparent; border: none; color: #C4B5FD; font-weight: bold; font-size: 12.5px; padding: 2px;"
        )
        self._adv_toggle.clicked.connect(self._toggle_advanced)
        c4_l.addWidget(self._adv_toggle)

        self._adv_panel = QWidget()
        self._adv_panel.hide()
        adv_l = QVBoxLayout(self._adv_panel)
        adv_l.setContentsMargins(4, 10, 4, 4)
        adv_l.setSpacing(8)

        adv_l.addWidget(self._sub_label("自定义角色三视图路径（留空使用内置原案图）"))
        turn_row = QHBoxLayout()
        turn_row.setSpacing(6)
        self._turnaround_edit = QLineEdit()
        turn_row.addWidget(self._turnaround_edit, 1)
        pick_turn = QPushButton("浏览...")
        pick_turn.setCursor(Qt.CursorShape.PointingHandCursor)
        pick_turn.clicked.connect(self._pick_turnaround)
        turn_row.addWidget(pick_turn)
        clr_turn = QPushButton("清空")
        clr_turn.setCursor(Qt.CursorShape.PointingHandCursor)
        clr_turn.clicked.connect(lambda: self._turnaround_edit.setText(""))
        turn_row.addWidget(clr_turn)
        adv_l.addLayout(turn_row)

        json_row = QHBoxLayout()
        open_json = QPushButton("📄 打开 character.json 文件")
        open_json.setCursor(Qt.CursorShape.PointingHandCursor)
        open_json.clicked.connect(self._open_json_file)
        json_row.addWidget(open_json)
        reload_json = QPushButton("🔄 从文件重载")
        reload_json.setCursor(Qt.CursorShape.PointingHandCursor)
        reload_json.clicked.connect(self._reload_from_file)
        json_row.addWidget(reload_json)
        json_row.addStretch(1)
        adv_l.addLayout(json_row)

        c4_l.addWidget(self._adv_panel)
        bl.addWidget(c4)

        scroll.setWidget(body)
        root.addWidget(scroll, 1)

        # Bottom action bar
        bar = QFrame()
        bar.setObjectName("FooterCard")
        bar_l = QHBoxLayout(bar)
        bar_l.setContentsMargins(12, 10, 12, 10)
        self._status_lbl = QLabel()
        self._status_lbl.setStyleSheet("color: #6EE7B7; font-size: 12px; background: transparent;")
        bar_l.addWidget(self._status_lbl, 1)

        close_btn = QPushButton("关闭")
        close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        close_btn.clicked.connect(self.close)
        bar_l.addWidget(close_btn)

        save_btn = QPushButton("💾 保存并立即生效")
        save_btn.setObjectName("PrimaryBtn")
        save_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        save_btn.clicked.connect(self._on_save)
        bar_l.addWidget(save_btn)
        root.addWidget(bar)

    @staticmethod
    def _sub_label(text: str) -> QLabel:
        lbl = QLabel(text)
        lbl.setStyleSheet("color: #B9B2DE; font-size: 11.5px; background: transparent;")
        return lbl

    def _make_line_col(self, parent_row: QHBoxLayout, title: str, height: int) -> QPlainTextEdit:
        col = QVBoxLayout()
        col.addWidget(self._sub_label(title))
        edit = QPlainTextEdit()
        edit.setFixedHeight(height)
        col.addWidget(edit)
        parent_row.addLayout(col, 1)
        return edit

    def _toggle_advanced(self) -> None:
        visible = not self._adv_panel.isVisible()
        self._adv_panel.setVisible(visible)
        prefix = "▼" if visible else "▶"
        self._adv_toggle.setText(
            f"{prefix} ④ 展开更多设定（自定义三视图 / JSON 文件）"
        )

    def _flush_current_action_lines(self) -> None:
        cat = (self._current_action_line_category or "").strip().lower()
        if not cat:
            return
        vals = self._parse_lines_box(self._action_line_edit)
        if vals:
            self._editable_lines[cat] = vals
        else:
            self._editable_lines.pop(cat, None)

    def _load_action_line_category(self, category: str) -> None:
        cat = (category or "").strip().lower()
        if not cat:
            return
        self._current_action_line_category = cat
        vals = self._editable_lines.get(cat) or DEFAULT_LINES.get(cat) or []
        self._action_line_edit.setPlainText("\n".join(vals))

    def _on_action_line_category_changed(self, idx: int) -> None:
        if self._suppress_action_line_sync or idx < 0:
            return
        self._flush_current_action_lines()
        cat = self._action_line_category_combo.itemData(idx)
        if cat:
            self._load_action_line_category(str(cat))

    def _reset_current_action_lines(self) -> None:
        cat = (self._current_action_line_category or "").strip().lower()
        if not cat:
            return
        defaults = list(DEFAULT_LINES.get(cat) or [])
        if defaults:
            self._editable_lines[cat] = defaults
        else:
            self._editable_lines.pop(cat, None)
        self._action_line_edit.setPlainText("\n".join(defaults))
        self._flash_status("已还原当前分类默认台词（记得点右下角保存生效）")

    def _load_into_form(self, raw: CharacterProfile) -> None:
        p = CharacterLore.resolve_with_defaults(raw)
        self._working_profile = copy.deepcopy(p)
        self._editable_lines = {k.lower(): list(v) for k, v in p.lines.items()}
        self._custom_standee_path = p.custom_standee_path

        self._name_edit.setText(p.name)
        self._alias_edit.setText(p.alias)
        self._badge_edit.setText(p.badge_tag)
        self._subtitle_edit.setText(p.subtitle)
        self._bio_edit.setPlainText(p.bio)
        self._turnaround_edit.setText(p.custom_turnaround_path)

        self._suppress_tone = True
        self._ai_prompt_edit.setPlainText(p.ai_system_prompt)
        matched_idx = 0
        for idx, opt in enumerate(AI_TONE_OPTIONS):
            if opt.system_prompt == p.ai_system_prompt.strip():
                matched_idx = idx
                break
        self._tone_combo.setCurrentIndex(matched_idx)
        self._suppress_tone = False

        act_id = CharacterLore.normalize_bubble_activity(p.bubble_activity)
        act_idx = self._bubble_activity_combo.findData(act_id)
        if act_idx >= 0:
            self._bubble_activity_combo.setCurrentIndex(act_idx)

        self._set_lines_box(self._greeting_edit, p.lines, "greeting")
        self._set_lines_box(self._pat_edit, p.lines, "pat")
        self._set_lines_box(self._idle_edit, p.lines, "idle")

        self._suppress_action_line_sync = True
        try:
            cur_cat = (
                self._action_line_category_combo.currentData()
                or IDLE_EDITOR_LINE_CATEGORIES[0][0]
            )
            self._load_action_line_category(str(cur_cat))
        finally:
            self._suppress_action_line_sync = False

        self._refresh_standee_preview()

    @staticmethod
    def _set_lines_box(box: QPlainTextEdit, lines: dict[str, list[str]], key: str) -> None:
        vals = lines.get(key) or []
        box.setPlainText("\n".join(vals))

    @staticmethod
    def _parse_lines_box(box: QPlainTextEdit) -> list[str]:
        return [s.strip() for s in box.toPlainText().splitlines() if s.strip()]

    def _refresh_standee_preview(self) -> None:
        default_img = os.path.join(
            get_bundled_resource_dir(), "Assets", "Profile", "gemini_standee.png"
        )
        path = (
            self._custom_standee_path
            if self._custom_standee_path and os.path.isfile(self._custom_standee_path)
            else default_img
        )
        if os.path.isfile(path):
            pix = QPixmap(path)
            if not pix.isNull():
                self._standee_preview.setPixmap(
                    pix.scaled(
                        156,
                        180,
                        Qt.AspectRatioMode.KeepAspectRatio,
                        Qt.TransformationMode.SmoothTransformation,
                    )
                )

    def _apply_preset(self, preset) -> None:
        self._load_into_form(preset.create_profile())
        self._flash_status(f"✦ 已载入「{preset.button_label}」模板（记得点右下角保存生效）")

    def _on_tone_changed(self, idx: int) -> None:
        if self._suppress_tone or idx < 0:
            return
        opt = self._tone_combo.itemData(idx)
        if opt is not None:
            self._ai_prompt_edit.setPlainText(opt.system_prompt)

    def _pick_standee(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "选择自定义立绘图片", "", "图片文件 (*.png *.jpg *.jpeg *.webp *.bmp)"
        )
        if path:
            self._custom_standee_path = path
            self._refresh_standee_preview()
            self._flash_status("✓ 已更新展示立绘（保存后生效）")

    def _clear_standee(self) -> None:
        self._custom_standee_path = ""
        self._refresh_standee_preview()
        self._flash_status("已还原默认 Gemini 娘立绘")

    def _pick_turnaround(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "选择自定义三视图图片", "", "图片文件 (*.png *.jpg *.jpeg *.webp *.bmp)"
        )
        if path:
            self._turnaround_edit.setText(path)

    def _collect_profile(self) -> CharacterProfile:
        self._flush_current_action_lines()
        lines = {k: list(v) for k, v in self._editable_lines.items()}

        def apply_category(cat: str, box: QPlainTextEdit) -> None:
            vals = self._parse_lines_box(box)
            if vals:
                lines[cat] = vals
            else:
                lines.pop(cat, None)

        apply_category("greeting", self._greeting_edit)
        apply_category("pat", self._pat_edit)
        apply_category("idle", self._idle_edit)

        return CharacterProfile(
            name=self._name_edit.text().strip(),
            alias=self._alias_edit.text().strip(),
            badge_tag=self._badge_edit.text().strip(),
            subtitle=self._subtitle_edit.text().strip(),
            bio=self._bio_edit.toPlainText().strip(),
            source_page_url="",
            standee_caption=self._working_profile.standee_caption,
            turnaround_caption=self._working_profile.turnaround_caption,
            footer_attribution=self._working_profile.footer_attribution,
            custom_standee_path=self._custom_standee_path.strip(),
            custom_turnaround_path=self._turnaround_edit.text().strip(),
            ai_system_prompt=self._ai_prompt_edit.toPlainText().strip(),
            bubble_activity=CharacterLore.normalize_bubble_activity(
                self._bubble_activity_combo.currentData()
            ),
            lines=lines,
        )

    def _on_save(self) -> None:
        profile = self._collect_profile()
        self._service.save(profile)
        resolved = CharacterLore.get_active_profile()
        self._working_profile = copy.deepcopy(resolved)
        self._editable_lines = {k.lower(): list(v) for k, v in resolved.lines.items()}
        self._load_action_line_category(self._current_action_line_category)
        self._on_applied(resolved)
        self._flash_status("✓ 已保存并在桌面实时生效！")

    def _on_reset_defaults(self) -> None:
        ans = QMessageBox.question(
            self,
            "恢复默认角色设定",
            "确定要恢复为原版 Gemini 娘设定并清空自定义配置吗？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if ans != QMessageBox.StandardButton.Yes:
            return
        def_profile = self._service.reset()
        self._load_into_form(def_profile)
        self._on_applied(def_profile)
        self._flash_status("✓ 已恢复原版 Gemini 娘设定并生效！")

    def _open_json_file(self) -> None:
        if not os.path.exists(self._service.character_file_path):
            self._service.save(self._collect_profile())
        try:
            os.startfile(self._service.character_file_path)
        except OSError:
            QDesktopServices.openUrl(QUrl.fromLocalFile(self._service.character_file_path))

    def _reload_from_file(self) -> None:
        loaded = self._service.load()
        CharacterLore.set_active_profile(loaded)
        self._load_into_form(loaded)
        self._on_applied(loaded)
        self._flash_status("✓ 已从 character.json 重新加载并生效！")

    def _flash_status(self, msg: str) -> None:
        self._status_lbl.setText(msg)
        QTimer.singleShot(3200, lambda: self._status_lbl.setText("") if self._status_lbl.text() == msg else None)
