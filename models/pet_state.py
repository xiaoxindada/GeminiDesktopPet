"""Semantic animation/mood states for Gemini-chan mapped 1:1 to her chibi sprites."""

from __future__ import annotations

from enum import Enum


class PetState(str, Enum):
    IDLE = "idle"
    IDLE2 = "idle-2"
    IDLE3 = "idle-3"
    LISTENING = "listening"
    THINKING = "thinking"
    READING = "reading"
    WRITING = "writing"
    RUNNING = "running"
    SEARCHING = "searching"
    DELEGATING = "delegating"
    HAPPY = "happy"
    PAT = "pat"
    SURPRISED = "surprised"
    HUM = "hum"
    STRETCH = "stretch"
    DRAWING = "drawing"
    OOPS = "oops"
    PROUD = "proud"
    TIRED = "tired"
    SLEEPING = "sleeping"
    YAWN = "yawn"
    COMPACT = "compact"
    FLOAT = "float"
    ASKING = "asking"
    GREET_DAWN = "greet_dawn"
    GREET_MORNING = "greet_morning"
    GREET_NOON = "greet_noon"
    GREET_AFTERNOON = "greet_afternoon"
    GREET_EVENING = "greet_evening"
    GREET_NIGHT = "greet_night"
    FEST_SPRING = "fest_spring"
    FEST_LANTERN = "fest_lantern"
    FEST_DRAGON_BOAT = "fest_dragonboat"
    FEST_MID_AUTUMN = "fest_midautumn"
    FEST_NATIONAL = "fest_national"
    FEST_HALLOWEEN = "fest_halloween"
    FEST_CHRISTMAS = "fest_christmas"
    FEST_NEW_YEAR = "fest_newyear"
    BYE = "bye"
    BUSY_LEFT = "busy_left"
    BUSY_RIGHT = "busy_right"
    SWAMPED_LEFT = "swamped_left"
    SWAMPED_RIGHT = "swamped_right"
    ANNOYED = "annoyed"
    PUFFED = "puffed"
    REDHOT = "redhot"
    SULK = "sulk"
    DIZZY = "dizzy"
    COAXED = "coaxed"
    FORGIVEN = "forgiven"
    EDGE_LEFT = "edge_left"
    EDGE_LEFT_PEEK = "edge_left_peek"
    EDGE_RIGHT = "edge_right"
    EDGE_RIGHT_PEEK = "edge_right_peek"
    UPDATE = "update"

    def to_sprite_key(self) -> str:
        return self.value

    @classmethod
    def from_sprite_key(cls, key: str) -> PetState | None:
        norm = key.strip().lower().replace("-", "").replace("_", "")
        for member in cls:
            if member.value.replace("-", "").replace("_", "").lower() == norm:
                return member
            if member.name.replace("_", "").lower() == norm:
                return member
        return None
