"""角色自定义档案与台词库的 JSON 持久化读写。"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional

from models.app_version import get_user_config_dir
from models.character_lore import CharacterLore
from models.character_profile import CharacterProfile


class CharacterProfileService:
    """加载、保存、重置及导入导出 character.json。"""

    def __init__(self, settings_folder: Optional[str | os.PathLike[str]] = None) -> None:
        folder = Path(settings_folder) if settings_folder else get_user_config_dir()
        self._folder: Path = folder
        self.character_file_path: str = str(folder / "character.json")

    def load(self) -> CharacterProfile:
        target = Path(self.character_file_path)
        if target.is_file():
            try:
                data = json.loads(target.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    return CharacterLore.resolve_with_defaults(self._from_dict(data))
            except Exception:
                pass
        return CharacterLore.create_default_profile()

    def save(self, profile: CharacterProfile) -> None:
        resolved = CharacterLore.resolve_with_defaults(profile)
        self._folder.mkdir(parents=True, exist_ok=True)
        Path(self.character_file_path).write_text(
            json.dumps(self._to_dict(resolved), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        CharacterLore.set_active_profile(resolved)

    def reset(self) -> CharacterProfile:
        target = Path(self.character_file_path)
        try:
            target.unlink(missing_ok=True)
        except OSError:
            pass
        def_profile = CharacterLore.create_default_profile()
        CharacterLore.set_active_profile(def_profile)
        return def_profile

    def export_to_file(self, file_path: str | os.PathLike[str], profile: CharacterProfile) -> None:
        resolved = CharacterLore.resolve_with_defaults(profile)
        target = Path(file_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(self._to_dict(resolved), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def import_from_file(self, file_path: str | os.PathLike[str]) -> CharacterProfile:
        data = json.loads(Path(file_path).read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("Invalid character JSON")
        return CharacterLore.resolve_with_defaults(self._from_dict(data))

    @staticmethod
    def _to_dict(p: CharacterProfile) -> dict:
        return {
            "name": p.name,
            "alias": p.alias,
            "badgeTag": p.badge_tag,
            "subtitle": p.subtitle,
            "bio": p.bio,
            "sourcePageUrl": p.source_page_url,
            "standeeCaption": p.standee_caption,
            "turnaroundCaption": p.turnaround_caption,
            "footerAttribution": p.footer_attribution,
            "customStandeePath": p.custom_standee_path,
            "customTurnaroundPath": p.custom_turnaround_path,
            "aiSystemPrompt": p.ai_system_prompt,
            "bubbleActivity": CharacterLore.normalize_bubble_activity(p.bubble_activity),
            "lines": p.lines,
        }

    @staticmethod
    def _from_dict(d: dict) -> CharacterProfile:
        def get_str(key: str) -> str:
            val = d.get(key)
            return str(val) if val is not None else ""

        lines: dict[str, list[str]] = {}
        raw_lines = d.get("lines")
        if isinstance(raw_lines, dict):
            for k, v in raw_lines.items():
                if isinstance(v, list):
                    lines[str(k)] = [str(x) for x in v if x is not None]

        return CharacterProfile(
            name=get_str("name"),
            alias=get_str("alias"),
            badge_tag=get_str("badgeTag"),
            subtitle=get_str("subtitle"),
            bio=get_str("bio"),
            source_page_url=get_str("sourcePageUrl"),
            standee_caption=get_str("standeeCaption"),
            turnaround_caption=get_str("turnaroundCaption"),
            footer_attribution=get_str("footerAttribution"),
            custom_standee_path=get_str("customStandeePath"),
            custom_turnaround_path=get_str("customTurnaroundPath"),
            ai_system_prompt=get_str("aiSystemPrompt"),
            bubble_activity=CharacterLore.normalize_bubble_activity(
                get_str("bubbleActivity") or get_str("bubble_activity")
            ),
            lines=lines,
        )
