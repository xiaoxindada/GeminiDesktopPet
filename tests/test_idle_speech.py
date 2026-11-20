"""无操作随机台词、动作匹配与可视化角色设定持久化测试。"""

from __future__ import annotations

import json
import random
import time
from unittest.mock import patch

import pytest
from PySide6.QtWidgets import QApplication

from models.character_lore import (
    BUILTIN_PRESETS,
    DEFAULT_LINES,
    CharacterLore,
)
from models.character_profile import CharacterProfile
from models.pet_settings import DockSide, PetSettings
from models.pet_state import PetState
from services.character_profile_service import CharacterProfileService
from services.gemini_chat_service import GeminiChatService
from services.settings_service import SettingsService
from services.sound_service import SoundService
from services.sprite_manager import SpriteManager
from services.system_monitor_service import SystemMonitorService
from views.character_editor_window import CharacterEditorWindow
from views.pet_window import PetWindow


@pytest.fixture(scope="module")
def app():
    instance = QApplication.instance() or QApplication([])
    instance.setQuitOnLastWindowClosed(False)
    yield instance


@pytest.fixture(scope="module")
def sprites(app):
    manager = SpriteManager()
    manager.preload_all()
    return manager


def _make_pet(
    tmp_path,
    sprites: SpriteManager,
    settings: PetSettings,
) -> PetWindow:
    sound = SoundService()
    sound.enabled = False
    pet = PetWindow(
        settings,
        SettingsService(str(tmp_path)),
        sprites,
        sound,
        SystemMonitorService(),
        GeminiChatService(),
    )
    return pet


def test_character_lore_match_pose_and_pick_idle_speech():
    try:
        CharacterLore.set_active_profile(None)
        expected_chatter = [
            PetState.PROUD,
            PetState.LISTENING,
            PetState.READING,
            PetState.PUFFED,
            PetState.ASKING,
            PetState.FLOAT,
        ]
        for line, expected in zip(DEFAULT_LINES["chatter"], expected_chatter):
            matched = CharacterLore.match_pose_for_line("chatter", line)
            assert matched == expected

        rng = random.Random(42)
        for side, expected_pose in (
            (DockSide.LEFT, PetState.EDGE_LEFT_PEEK),
            (DockSide.RIGHT, PetState.EDGE_RIGHT_PEEK),
        ):
            line, pose = CharacterLore.pick_idle_speech(docked_side=side, rng=rng)
            assert line in DEFAULT_LINES["edge"]
            assert pose == expected_pose

        edge_states = {
            PetState.EDGE_LEFT,
            PetState.EDGE_LEFT_PEEK,
            PetState.EDGE_LEFT_SLEEP,
            PetState.EDGE_RIGHT,
            PetState.EDGE_RIGHT_PEEK,
            PetState.EDGE_RIGHT_SLEEP,
        }
        prev_line = None
        prev_pose = None
        for _ in range(30):
            line, pose = CharacterLore.pick_idle_speech(
                docked_side=DockSide.NONE,
                allow_running=False,
                exclude_line=prev_line,
                exclude_state=prev_pose,
                rng=rng,
            )
            assert pose not in edge_states
            assert pose != PetState.RUNNING
            assert line != prev_line
            assert pose != prev_pose
            prev_line = line
            prev_pose = pose

        # 自定义台词在筛选下应被正确抽取并匹配对应姿态
        CharacterLore.set_active_profile(
            CharacterProfile(
                bubble_activity="lively",
                lines={
                    "hum": ["♪ 自定义小曲调 ✦"],
                    "chatter": ["快来画画香蕉吧！"],
                },
            )
        )
        assert CharacterLore.get_bubble_activity_preset().id == "lively"
        assert CharacterLore.match_pose_for_line("chatter", "快来画画香蕉吧！") == PetState.DRAWING

        # 验证预设气泡活跃度配置
        gentle = next(p for p in BUILTIN_PRESETS if p.id == "gentle").create_profile()
        genki = next(p for p in BUILTIN_PRESETS if p.id == "genki").create_profile()
        assert CharacterLore.get_bubble_activity_preset(gentle.bubble_activity).id == "quiet"
        assert CharacterLore.get_bubble_activity_preset(genki.bubble_activity).id == "lively"
    finally:
        CharacterLore.set_active_profile(None)


def test_pet_idle_speech_matches_pose_and_respects_guards(app, sprites, tmp_path):
    CharacterLore.set_active_profile(None)
    settings = PetSettings(
        show_speech_bubble=True,
        auto_walk=False,
        chat_sync_enabled=False,
    )
    pet = _make_pet(tmp_path, sprites, settings)
    try:
        pet.show()
        pet._frame_timer.stop()
        pet._ai_timer.stop()
        pet._idle_speech_timer.stop()
        app.processEvents()

        # 刚上线还在等待间隔内，不触发闲聊
        pet._hide_speech_bubble()
        pet._override_pose = None
        pet._last_pet_activity_monotonic = time.monotonic()
        pet._idle_speech_next_delay_sec = 15.0
        pet._idle_speech_timer_tick()
        assert not pet._speech_bubble.isVisible()

        # 超过无操作间隔后触发台词并同步切换到匹配动作
        pet._last_pet_activity_monotonic = time.monotonic() - 30.0
        pet._last_idle_speech_monotonic = pet._last_pet_activity_monotonic
        with patch.object(
            CharacterLore,
            "pick_idle_speech",
            return_value=("Nano Banana，启动！", PetState.DRAWING),
        ):
            pet._idle_speech_timer_tick()
        app.processEvents()
        assert pet._speech_bubble.isVisible()
        assert pet._bubble_text_lbl.text() == "Nano Banana，启动！"
        assert pet._override_pose == PetState.DRAWING
        assert pet._override_is_autonomous is True
        assert pet._idle_speech_bubble_active is True

        # 闲聊气泡显示期间，常规 _ai_timer_tick 不会覆盖动作
        pet._ai_timer_tick()
        assert pet._override_pose == PetState.DRAWING

        # 抽到 RUNNING 且允许漫步时应进入行走状态
        pet._hide_speech_bubble()
        settings.auto_walk = True
        pet._last_pet_activity_monotonic = time.monotonic() - 30.0
        pet._last_idle_speech_monotonic = pet._last_pet_activity_monotonic
        with patch.object(
            CharacterLore,
            "pick_idle_speech",
            return_value=("哒哒哒巡逻桌面喵～", PetState.RUNNING),
        ):
            pet._idle_speech_timer_tick()
        assert pet._speech_bubble.isVisible()
        assert pet._is_walking is True
        assert pet._current_state == PetState.RUNNING
        pet._is_walking = False

        # 禁用条件：关闭气泡 / 打盹 / 聊天 / 同步播报期间均不触发
        for blocker in ("bubble_off", "doze", "chat", "sync"):
            pet._hide_speech_bubble()
            settings.show_speech_bubble = blocker != "bubble_off"
            settings.doze_mode = blocker == "doze"
            pet._is_chat_open = blocker == "chat"
            pet._sync_bubble_active = blocker == "sync"
            pet._last_pet_activity_monotonic = time.monotonic() - 60.0
            pet._last_idle_speech_monotonic = pet._last_pet_activity_monotonic
            pet._idle_speech_timer_tick()
            assert not pet._speech_bubble.isVisible(), blocker
        settings.show_speech_bubble = True
        settings.doze_mode = False
        pet._is_chat_open = False
        pet._sync_bubble_active = False

        # lively vs quiet 气泡活跃程度影响空闲触发延迟上限
        CharacterLore.set_active_profile(CharacterProfile(bubble_activity="lively"))
        pet.sync_character_profile(CharacterLore.get_active_profile())
        assert 5.0 <= pet._idle_speech_next_delay_sec <= 10.0

        CharacterLore.set_active_profile(CharacterProfile(bubble_activity="quiet"))
        pet.sync_character_profile(CharacterLore.get_active_profile())
        assert 30.0 <= pet._idle_speech_next_delay_sec <= 60.0
    finally:
        CharacterLore.set_active_profile(None)
        pet.close()
        pet.deleteLater()
        app.processEvents()


def test_character_editor_bubble_activity_and_action_lines_persist(app, tmp_path):
    CharacterLore.set_active_profile(None)
    svc = CharacterProfileService(str(tmp_path))
    applied: list[CharacterProfile] = []
    editor = CharacterEditorWindow(svc, lambda p: applied.append(p))
    try:
        editor.show()
        app.processEvents()

        # 1. 选择气泡活跃度为偏活跃
        lively_idx = editor._bubble_activity_combo.findData("lively")
        assert lively_idx >= 0
        editor._bubble_activity_combo.setCurrentIndex(lively_idx)

        # 2. 编辑第一个分类 (chatter) 后切换到另一个分类 (hum) 编辑，保留两者改动
        chatter_idx = editor._action_line_category_combo.findData("chatter")
        hum_idx = editor._action_line_category_combo.findData("hum")
        drawing_idx = editor._action_line_category_combo.findData("drawing")
        assert chatter_idx >= 0 and hum_idx >= 0 and drawing_idx >= 0

        editor._action_line_category_combo.setCurrentIndex(chatter_idx)
        editor._action_line_edit.setPlainText("自定义碎碎念台词一\n自定义碎碎念台词二")

        editor._action_line_category_combo.setCurrentIndex(hum_idx)
        editor._action_line_edit.setPlainText("♪ 自定义哼歌台词")

        # 3. 切到 drawing 清空后点还原默认，可恢复内置台词
        editor._action_line_category_combo.setCurrentIndex(drawing_idx)
        editor._action_line_edit.setPlainText("")
        editor._reset_action_line_btn.click()
        assert "Nano Banana，启动！" in editor._action_line_edit.toPlainText()

        # 4. 保存并重新从磁盘加载验证
        editor._on_save()
        app.processEvents()
        assert applied
        assert applied[-1].bubble_activity == "lively"
        assert applied[-1].lines["chatter"] == ["自定义碎碎念台词一", "自定义碎碎念台词二"]
        assert applied[-1].lines["hum"] == ["♪ 自定义哼歌台词"]

        saved_json = json.loads((tmp_path / "character.json").read_text(encoding="utf-8"))
        assert saved_json["bubbleActivity"] == "lively"
        reloaded = svc.load()
        assert reloaded.bubble_activity == "lively"
        assert reloaded.lines["chatter"] == ["自定义碎碎念台词一", "自定义碎碎念台词二"]
        assert reloaded.lines["hum"] == ["♪ 自定义哼歌台词"]

        # 5. 清空某类台词后保存，应自动恢复该类内置默认台词
        editor._action_line_category_combo.setCurrentIndex(hum_idx)
        editor._action_line_edit.setPlainText("")
        editor._on_save()
        app.processEvents()
        assert svc.load().lines["hum"] == DEFAULT_LINES["hum"]
        assert editor._action_line_edit.toPlainText().splitlines() == DEFAULT_LINES["hum"]
    finally:
        CharacterLore.set_active_profile(None)
        editor.close()
        editor.deleteLater()
        app.processEvents()
