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

    disabled = PetSettings(follow_antigravity=False, follow_gemini_app=False, follow_antigravity_cli=False)
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


def test_cli_probe_accepts_low_thread_processes_and_rejects_other_apps(monkeypatch) -> None:
    from services import followed_app_probe as probe

    monkeypatch.setattr(probe, "_snapshot_process_entries", lambda: [
        (20, "AGY.exe", 1), (21, "agy", 2),
        (22, "Antigravity.exe", 60), (23, "node.exe", 30), (24, "agy-helper.exe", 5),
    ])
    assert FollowedAppProbe.sample_antigravity_cli_activity() == _snapshot(True, {20, 21}, {20, 21})
    monkeypatch.setattr(probe, "_snapshot_process_entries", lambda: [])
    assert FollowedAppProbe.sample_antigravity_cli_activity() == _snapshot(False)


def test_cli_probe_falls_back_when_native_snapshot_fails(monkeypatch) -> None:
    from types import SimpleNamespace
    from services import followed_app_probe as probe

    def fail():
        raise OSError("snapshot unavailable")

    monkeypatch.setattr(probe, "_snapshot_process_entries", fail)
    monkeypatch.setattr(probe.psutil, "process_iter", lambda attrs: [
        SimpleNamespace(info={"pid": 31, "name": "agy.exe"}),
        SimpleNamespace(info={"pid": 32, "name": "node.exe"}),
    ])
    assert FollowedAppProbe.sample_antigravity_cli_activity() == _snapshot(True, {31}, {31})


def test_cli_toggle_is_independent_and_persists(tmp_path) -> None:
    from services.app_follow_service import AppFollowService
    from services.settings_service import SettingsService

    service = SettingsService(tmp_path)
    settings = PetSettings(follow_antigravity=False, follow_gemini_app=False)
    assert settings.follow_antigravity_cli
    assert service.load().follow_antigravity_cli
    service.save(settings)
    loaded = service.load()
    assert loaded.follow_antigravity_cli
    assert AppFollowService.any_follow_enabled(loaded)
    assert FollowedAppProbe.detect_running_followed_app_key(
        loaded, sample_cli=lambda: _snapshot(True, {20}, {20})
    ) == "antigravity-cli"
    loaded.follow_antigravity_cli = False
    service.save(loaded)
    assert not service.load().follow_antigravity_cli
    assert not AppFollowService.any_follow_enabled(loaded)
    assert FollowedAppProbe.detect_running_followed_app_key(
        loaded, sample_cli=lambda: (_ for _ in ()).throw(AssertionError("disabled CLI sampled"))
    ) is None


def test_cli_gui_watcher_start_exit_and_restart(monkeypatch) -> None:
    from unittest.mock import Mock
    from PySide6.QtWidgets import QApplication
    from services.antigravity_watcher_service import AntigravityWatcherService

    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(FollowedAppProbe, "sample_antigravity_activity", lambda: _snapshot(False))
    monkeypatch.setattr(FollowedAppProbe, "sample_gemini_activity", lambda: _snapshot(False))
    snapshots = iter([
        _snapshot(False), _snapshot(True, {20}, {20}), _snapshot(True, {20}, {20}),
        _snapshot(False), _snapshot(True, {21}, {21}),
    ])
    monkeypatch.setattr(FollowedAppProbe, "sample_antigravity_cli_activity", lambda: next(snapshots))
    watcher = AntigravityWatcherService()
    watcher.on_antigravity_cli_started = Mock()
    watcher.on_antigravity_started = Mock()
    watcher.start()
    try:
        for _ in range(4):
            watcher._poll()
        assert watcher.on_antigravity_cli_started.call_count == 2
        watcher.on_antigravity_started.assert_not_called()
    finally:
        watcher.dispose()


def test_cli_background_wakes_on_existing_process_and_later_start(tmp_path, monkeypatch) -> None:
    from unittest.mock import Mock
    from services import app_follow_service as follow
    from services.settings_service import SettingsService

    service = SettingsService(tmp_path)
    service.save(PetSettings(follow_antigravity=False, follow_gemini_app=False, follow_antigravity_cli=True))
    monkeypatch.setattr(follow, "Win32Mutex", Mock())
    monkeypatch.setattr(follow, "Win32Event", Mock())
    monkeypatch.setattr(follow.AppFollowService, "is_stop_all_signaled", lambda *args: False)
    monkeypatch.setattr(follow.AppFollowService, "is_main_instance_running", lambda *args: False)
    # 只模拟信号与进程快照，不创建启动项或启动真实桌宠。
    import ctypes
    monkeypatch.setattr(ctypes, "WinDLL", Mock())
    for from_exit in (False, True):
        waits = iter([-1, -1, 0])
        monkeypatch.setattr(follow, "wait_any_events", lambda *args: next(waits))
        snapshots = iter([_snapshot(False), _snapshot(True, {20}, {20})])
        launch = Mock(return_value=True)
        monkeypatch.setattr(follow.AppFollowService, "_launch_wake_target", launch)
        options = follow.FollowWatcherOptions(
            from_exit=from_exit,
            custom_settings_folder=str(tmp_path),
            sample_antigravity=lambda: _snapshot(False),
            sample_gemini=lambda: _snapshot(False),
            sample_antigravity_cli=(lambda: next(snapshots)) if from_exit else (lambda: _snapshot(True, {20}, {20})),
        )
        assert follow.AppFollowService.run_follow_watcher_core(options) == 0
        assert launch.call_args.args[1] == ["--woken-by:antigravity-cli"]


def test_cli_gui_wake_respects_setting() -> None:
    from types import SimpleNamespace
    from unittest.mock import Mock
    from app import PetApplicationCoordinator

    coord = SimpleNamespace(_settings=PetSettings(follow_antigravity_cli=False), _show_pet_with_wake_greeting=Mock())
    PetApplicationCoordinator._on_antigravity_cli_started(coord)
    coord._show_pet_with_wake_greeting.assert_not_called()
    coord._settings.follow_antigravity_cli = True
    PetApplicationCoordinator._on_antigravity_cli_started(coord)
    coord._show_pet_with_wake_greeting.assert_called_once_with("antigravity-cli")


def test_cli_wake_signal_reaches_existing_main_instance(monkeypatch) -> None:
    from types import SimpleNamespace
    from unittest.mock import Mock
    import app as app_module

    events = []

    def event_factory(*args, **kwargs):
        event = Mock()
        events.append(event)
        return event

    monkeypatch.setattr(app_module, "Win32Event", event_factory)
    signals = iter([5, 3])
    monkeypatch.setattr(app_module, "wait_any_events", lambda *args, **kwargs: next(signals))
    coord = SimpleNamespace(
        _ipc_stop_flag=SimpleNamespace(is_set=lambda: False),
        _is_exiting=False, _ipc_stop_event=Mock(), _ipc_bridge=Mock(),
    )
    app_module.PetApplicationCoordinator._ipc_listener_loop(coord)
    coord._ipc_bridge.wake_requested.emit.assert_called_once_with("antigravity-cli")
    coord._ipc_bridge.close_requested.emit.assert_called_once_with()
    for event in events:
        event.close.assert_called_once_with()
