"""角色台词抽取、动作匹配与角色档案持久化单元测试。"""

from __future__ import annotations

import json
import random

from models.character_lore import (
    BUILTIN_PRESETS,
    DEFAULT_LINES,
    CharacterLore,
)
from models.character_profile import CharacterProfile
from models.pet_settings import DockSide
from models.pet_state import PetState
from services.character_profile_service import CharacterProfileService


def test_character_lore_match_pose_and_pick_idle_speech() -> None:
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


def test_character_profile_service_persists_activity_and_lines(tmp_path) -> None:
    try:
        CharacterLore.set_active_profile(None)
        svc = CharacterProfileService(str(tmp_path))
        profile = CharacterProfile(
            bubble_activity="lively",
            lines={
                "chatter": ["自定义碎碎念台词一", "自定义碎碎念台词二"],
                "hum": ["♪ 自定义哼歌台词"],
            },
        )
        svc.save(profile)

        saved_json = json.loads((tmp_path / "character.json").read_text(encoding="utf-8"))
        assert saved_json["bubbleActivity"] == "lively"

        reloaded = svc.load()
        assert reloaded.bubble_activity == "lively"
        assert reloaded.lines["chatter"] == ["自定义碎碎念台词一", "自定义碎碎念台词二"]
        assert reloaded.lines["hum"] == ["♪ 自定义哼歌台词"]
        assert reloaded.lines["drawing"] == DEFAULT_LINES["drawing"]

        # 清空某类台词后保存，应自动恢复该类内置默认台词
        reloaded.lines["hum"] = []
        svc.save(reloaded)
        assert svc.load().lines["hum"] == DEFAULT_LINES["hum"]
    finally:
        CharacterLore.set_active_profile(None)
