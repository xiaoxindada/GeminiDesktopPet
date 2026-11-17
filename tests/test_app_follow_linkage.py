"""主类型启动与联动：跟随 Antigravity / Gemini 桌面 App 启动的探测、跳变与冷却判定。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from models.pet_settings import PetSettings
from services.followed_app_probe import (
    AppActivitySnapshot,
    FollowedAppProbe,
    ProcessProbeInfo,
)


def _snapshot(running: bool, active=(), primary=()) -> AppActivitySnapshot:
    return AppActivitySnapshot(bool(running), set(active), set(primary))


def test_follow_toggles_decide_which_app_wakes_the_pet() -> None:
    both = PetSettings(follow_antigravity=True, follow_gemini_app=True)
    sampled: list[str] = []

    def antigravity_running() -> AppActivitySnapshot:
        sampled.append("antigravity")
        return _snapshot(True, {11}, {11})

    def gemini_running() -> AppActivitySnapshot:
        sampled.append("gemini")
        return _snapshot(True, {7}, {7})

    def gemini_idle() -> AppActivitySnapshot:
        sampled.append("gemini")
        return _snapshot(False)

    assert FollowedAppProbe.detect_running_followed_app_key(both, antigravity_running, gemini_idle) == "antigravity"
    assert sampled == ["antigravity"]

    only_gemini = PetSettings(follow_antigravity=False, follow_gemini_app=True)
    sampled.clear()
    assert FollowedAppProbe.detect_running_followed_app_key(only_gemini, antigravity_running, gemini_running) == "gemini"
    assert "antigravity" not in sampled

    disabled = PetSettings(follow_antigravity=False, follow_gemini_app=False)
    sampled.clear()
    assert FollowedAppProbe.detect_running_followed_app_key(disabled, antigravity_running, gemini_running) is None
    assert sampled == []


def test_activity_snapshot_ignores_orphan_workers_without_main_window() -> None:
    snapshot = FollowedAppProbe.build_snapshot_from_probes(
        [
            ProcessProbeInfo(1, False, 6),
            ProcessProbeInfo(2, False, 25),
            ProcessProbeInfo(3, True, 4),
            ProcessProbeInfo(4, False, 60),
        ]
    )
    assert snapshot.is_running
    assert snapshot.active_pids == {2, 3, 4}
    assert snapshot.primary_pids == {3, 4}

    orphan_only = FollowedAppProbe.build_snapshot_from_probes([ProcessProbeInfo(9, False, 3)])
    assert not orphan_only.is_running
    assert orphan_only.active_pids == set()
    assert orphan_only.primary_pids == set()


def test_start_and_exit_transitions_track_running_state() -> None:
    now = datetime(2026, 11, 17, 6, 0, tzinfo=timezone.utc)

    started, exited, running, known, last_start = FollowedAppProbe.evaluate_transition(
        _snapshot(True, {5}, {5}), False, set(), None, now
    )
    assert (started, exited, running) == (True, False, True)
    assert known == {5}
    assert last_start == now

    started_again, still_exited, still_running, same_known, same_last = FollowedAppProbe.evaluate_transition(
        _snapshot(True, {5}, {5}), True, {5}, now, now + timedelta(seconds=2)
    )
    assert (started_again, still_exited, still_running) == (False, False, True)
    assert same_known == {5}
    assert same_last == now

    re_started, gone, not_running, cleared, kept_last = FollowedAppProbe.evaluate_transition(
        _snapshot(False), True, {5}, now, now + timedelta(seconds=3)
    )
    assert (re_started, gone, not_running) == (False, True, False)
    assert cleared == set()
    assert kept_last == now


def test_new_primary_process_only_wakes_after_cooldown() -> None:
    now = datetime(2026, 11, 17, 6, 0, tzinfo=timezone.utc)

    # 已有主进程仍在运行时，6 秒内新出现的 primary 进程视为子进程波动，不唤醒
    started, *_ = FollowedAppProbe.evaluate_transition(
        _snapshot(True, {5, 9}, {5, 9}), True, {5}, now, now + timedelta(seconds=2)
    )
    assert started is False

    # 超过 6 秒冷却后，新的 primary 进程视为应用重新启动，触发唤醒
    restarted, *_ = FollowedAppProbe.evaluate_transition(
        _snapshot(True, {5, 9}, {5, 9}), True, {5}, now, now + timedelta(seconds=8)
    )
    assert restarted is True
