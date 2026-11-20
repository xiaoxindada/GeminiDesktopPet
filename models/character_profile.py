"""Character profile data structures persisted to character.json."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class CharacterProfile:
    """User-configurable character profile & persona lines persisted to ~/.GeminiDesktopPet/character.json."""

    name: str = ""
    alias: str = ""
    badge_tag: str = ""
    subtitle: str = ""
    bio: str = ""
    source_page_url: str = ""
    standee_caption: str = ""
    turnaround_caption: str = ""
    footer_attribution: str = ""
    custom_standee_path: str = ""
    custom_turnaround_path: str = ""
    ai_system_prompt: str = ""
    bubble_activity: str = "normal"
    lines: dict[str, list[str]] = field(default_factory=dict)
