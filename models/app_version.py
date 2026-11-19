"""Semantic version resolution and comparison defined directly in application code."""

from __future__ import annotations

import sys
from pathlib import Path


def get_app_base_dir() -> str:
    """Return the application directory (executable folder when frozen, repo root otherwise)."""
    if getattr(sys, "frozen", False):
        return str(Path(sys.executable).resolve().parent)
    return str(Path(__file__).resolve().parents[1])


USER_CONFIG_DIR_NAME = ".GeminiDesktopPet"


def get_user_config_dir() -> Path:
    """Return the user-level data & configuration directory (Path.home() / '.GeminiDesktopPet')."""
    return Path.home() / USER_CONFIG_DIR_NAME


def get_bundled_resource_dir() -> str:
    """Return the directory containing bundled Assets/ and packaged docs (supports PyInstaller _MEIPASS and _internal)."""
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass and Path(meipass).is_dir():
        return str(meipass)
    base = Path(get_app_base_dir())
    if (base / "Assets").is_dir():
        return str(base)
    internal = base / "_internal"
    if (internal / "Assets").is_dir() or internal.is_dir():
        return str(internal)
    return str(base)


APP_VERSION = "0.4.1"


class AppVersion:
    """Resolves and compares semantic application versions defined in code."""

    FALLBACK_VERSION = APP_VERSION
    _current_cache: str | None = None

    @classmethod
    def current(cls) -> str:
        if cls._current_cache is None:
            cls._current_cache = cls._resolve_current_version()
        return cls._current_cache

    @classmethod
    def current_display(cls) -> str:
        return cls.format_tag(cls.current())

    @classmethod
    def normalize(cls, raw: str | None) -> str:
        if not raw or not raw.strip():
            return cls.FALLBACK_VERSION

        trimmed = raw.strip()
        if trimmed.startswith(("v", "V")):
            trimmed = trimmed[1:].strip()

        plus_idx = trimmed.find("+")
        if plus_idx >= 0:
            trimmed = trimmed[:plus_idx].strip()

        return trimmed if trimmed else cls.FALLBACK_VERSION

    @classmethod
    def format_tag(cls, raw: str | None) -> str:
        return "v" + cls.normalize(raw)

    @classmethod
    def is_newer_version(cls, candidate: str | None, baseline: str | None = None) -> bool:
        return cls.compare(candidate, baseline if baseline is not None else cls.current()) > 0

    @classmethod
    def compare(cls, left: str | None, right: str | None) -> int:
        """Compare two semver strings (>0 when left > right, <0 when left < right, 0 when equal)."""
        l_nums, l_pre = cls._parse(left)
        r_nums, r_pre = cls._parse(right)

        count = max(len(l_nums), len(r_nums))
        for i in range(count):
            lv = l_nums[i] if i < len(l_nums) else 0
            rv = r_nums[i] if i < len(r_nums) else 0
            if lv != rv:
                return 1 if lv > rv else -1

        l_has_pre = bool(l_pre)
        r_has_pre = bool(r_pre)
        if not l_has_pre and not r_has_pre:
            return 0
        if not l_has_pre:
            return 1  # stable > prerelease
        if not r_has_pre:
            return -1

        return cls._compare_prerelease(l_pre, r_pre)

    @classmethod
    def _parse(cls, raw: str | None) -> tuple[list[int], str]:
        norm = cls.normalize(raw)
        dash_idx = norm.find("-")
        core = norm[:dash_idx] if dash_idx >= 0 else norm
        pre = norm[dash_idx + 1 :].strip() if dash_idx >= 0 else ""

        parts = [p for p in core.split(".") if p]
        nums = [0] * max(len(parts), 3)
        for i, p in enumerate(parts):
            try:
                nums[i] = int(p)
            except ValueError:
                pass
        return nums, pre

    @staticmethod
    def _compare_prerelease(left: str, right: str) -> int:
        l_parts = [p for p in left.split(".") if p]
        r_parts = [p for p in right.split(".") if p]
        for l_part, r_part in zip(l_parts, r_parts):
            l_num = l_part.isdigit()
            r_num = r_part.isdigit()
            if l_num and r_num:
                li, ri = int(l_part), int(r_part)
                if li != ri:
                    return 1 if li > ri else -1
            elif l_num and not r_num:
                return -1
            elif not l_num and r_num:
                return 1
            else:
                ll, rl = l_part.lower(), r_part.lower()
                if ll != rl:
                    return 1 if ll > rl else -1
        if len(l_parts) == len(r_parts):
            return 0
        return 1 if len(l_parts) > len(r_parts) else -1

    @classmethod
    def _resolve_current_version(cls) -> str:
        return cls.normalize(APP_VERSION)
