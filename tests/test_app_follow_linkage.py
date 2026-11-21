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

    QApplication.instance() or QApplication([])
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


def test_background_watcher_exits_when_gui_takes_over(tmp_path, monkeypatch) -> None:
    from unittest.mock import Mock
    from services import app_follow_service as follow

    mutex = Mock()
    monkeypatch.setattr(follow, "Win32Mutex", Mock(return_value=mutex))
    monkeypatch.setattr(follow, "Win32Event", Mock())
    monkeypatch.setattr(follow, "wait_any_events", Mock(return_value=-1))
    monkeypatch.setattr(follow.AppFollowService, "is_stop_all_signaled", Mock(return_value=False))
    monkeypatch.setattr(follow.AppFollowService, "is_main_instance_running", Mock(side_effect=[False, True]))
    launch = Mock()
    monkeypatch.setattr(follow.AppFollowService, "_launch_wake_target", launch)
    options = follow.FollowWatcherOptions(
        from_exit=True,
        custom_settings_folder=str(tmp_path),
        sample_antigravity=lambda: _snapshot(False),
        sample_gemini=lambda: _snapshot(False),
        sample_antigravity_cli=lambda: _snapshot(False),
    )

    assert follow.AppFollowService.run_follow_watcher_core(options) == 0
    launch.assert_not_called()
    mutex.release.assert_called_once_with()


def test_stopping_watcher_waits_and_never_kills_its_gui_child(monkeypatch) -> None:
    from unittest.mock import Mock, call
    from services import app_follow_service as follow

    lifecycle = Mock()
    watcher = lifecycle.watcher
    monkeypatch.setattr(follow, "Win32Event", Mock(return_value=lifecycle.event))
    lifecycle.read_marker.return_value = watcher
    monkeypatch.setattr(follow.AppFollowService, "try_get_live_watcher_process", lifecycle.read_marker)
    lifecycle.wait.side_effect = [False, True]
    monkeypatch.setattr(follow.AppFollowService, "wait_for_process_exit", lifecycle.wait)

    follow.AppFollowService.stop_follow_watcher()

    assert lifecycle.mock_calls == [
        call.read_marker(),
        call.event.set(),
        call.wait(watcher, 800),
        call.watcher.kill(),
        call.wait(watcher, 1000),
        call.event.close(),
    ]


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


def test_manual_exit_releases_main_mutex_before_handoff_and_wakes_on_relaunch(tmp_path) -> None:
    import uuid
    from types import SimpleNamespace
    from unittest.mock import Mock
    from app import PetApplicationCoordinator
    from services.app_follow_service import AppFollowService, FollowWatcherOptions
    from services.settings_service import SettingsService
    from services.win32_ipc import Win32Event, Win32Mutex

    suffix = uuid.uuid4().hex
    main_mutex_name = f"GeminiDesktopPet_TestMain_{suffix}"
    watcher_mutex_name = f"GeminiDesktopPet_TestWatcher_{suffix}"
    stop_watcher_name = f"GeminiDesktopPet_TestStopWatcher_{suffix}"
    stop_all_name = f"GeminiDesktopPet_TestStopAll_{suffix}"

    settings = PetSettings(
        follow_antigravity=True,
        follow_antigravity_cli=True,
        follow_gemini_app=True,
    )
    SettingsService(tmp_path).save(settings)

    main_mutex = Win32Mutex(main_mutex_name)
    assert main_mutex.acquire()

    woken_args: list[str] = []
    # 退出时目标应用仍在运行；随后关闭并再次启动，后台守护应只在再次启动时唤醒。
    ag_states = iter([
        _snapshot(True, {100}, {100}),
        _snapshot(True, {100}, {100}),
        _snapshot(False),
        _snapshot(True, {200}, {200}),
    ])

    def fake_spawn_watcher(from_exit: bool) -> bool:
        assert from_exit is True
        # 交接时主实例互斥体必须已释放，否则新拉起的守护会立刻退出。
        assert not AppFollowService.is_main_instance_running(main_mutex_name)

        def wake_and_stop(arg_str: str) -> bool:
            woken_args.append(arg_str)
            Win32Event.open_and_set(stop_watcher_name)
            return True

        options = FollowWatcherOptions(
            from_exit=from_exit,
            custom_settings_folder=str(tmp_path),
            main_mutex_name=main_mutex_name,
            watcher_mutex_name=watcher_mutex_name,
            stop_watcher_event_name=stop_watcher_name,
            stop_all_event_name=stop_all_name,
            poll_interval_ms=15,
            sample_antigravity=lambda: next(ag_states, _snapshot(True, {200}, {200})),
            sample_gemini=lambda: _snapshot(False),
            sample_antigravity_cli=lambda: _snapshot(False),
            custom_wake_launcher=wake_and_stop,
        )
        assert AppFollowService.run_follow_watcher_core(options) == 0
        return True

    follow_service = SimpleNamespace(
        stop_follow_watcher=Mock(),
        clear_main_pid=Mock(),
        any_follow_enabled=AppFollowService.any_follow_enabled,
        is_stop_all_signaled=lambda: False,
        spawn_follow_watcher=Mock(side_effect=fake_spawn_watcher),
    )
    coord = SimpleNamespace(
        _is_exiting=False,
        _shutdown_workers_timed_out=False,
        _quota_win=None,
        _profile_win=None,
        _editor_win=None,
        _settings_win=None,
        _update_win=None,
        _pet=Mock(findChildren=Mock(return_value=[])),
        findChildren=Mock(return_value=[]),
        _ipc_stop_flag=Mock(),
        _ipc_stop_event=Mock(),
        _ipc_thread=None,
        _follow_service=follow_service,
        _watcher=Mock(),
        _tray=Mock(),
        _sound=Mock(),
        _main_mutex=main_mutex,
        _settings=settings,
        _app=Mock(),
    )
    coord.gracefully_exit = lambda allow_respawn_watcher=True: PetApplicationCoordinator.gracefully_exit(
        coord, allow_respawn_watcher=allow_respawn_watcher
    )

    try:
        PetApplicationCoordinator._on_user_exit(coord)
        follow_service.spawn_follow_watcher.assert_called_once_with(from_exit=True)
        assert woken_args == ["--woken-by:antigravity"]
        coord._app.quit.assert_called_once_with()
    finally:
        main_mutex.release()


def test_exit_skips_respawn_when_follow_disabled_updating_or_stop_all() -> None:
    from types import SimpleNamespace
    from unittest.mock import Mock
    from app import PetApplicationCoordinator
    from services.app_follow_service import AppFollowService

    cases = [
        (PetSettings(follow_antigravity=False, follow_antigravity_cli=False, follow_gemini_app=False), True, False),
        (PetSettings(follow_antigravity=True, follow_antigravity_cli=True, follow_gemini_app=True), False, False),
        (PetSettings(follow_antigravity=True, follow_antigravity_cli=True, follow_gemini_app=True), True, True),
    ]
    for settings, allow_respawn, stop_all in cases:
        follow_service = SimpleNamespace(
            stop_follow_watcher=Mock(),
            clear_main_pid=Mock(),
            any_follow_enabled=AppFollowService.any_follow_enabled,
            is_stop_all_signaled=Mock(return_value=stop_all),
            spawn_follow_watcher=Mock(return_value=True),
        )
        coord = SimpleNamespace(
            _is_exiting=False,
            _shutdown_workers_timed_out=False,
            _quota_win=None,
            _profile_win=None,
            _editor_win=None,
            _settings_win=None,
            _update_win=None,
            _pet=Mock(findChildren=Mock(return_value=[])),
            findChildren=Mock(return_value=[]),
            _ipc_stop_flag=Mock(),
            _ipc_stop_event=Mock(),
            _ipc_thread=None,
            _follow_service=follow_service,
            _watcher=Mock(),
            _tray=Mock(),
            _sound=Mock(),
            _main_mutex=Mock(),
            _settings=settings,
            _app=Mock(),
        )

        PetApplicationCoordinator.gracefully_exit(coord, allow_respawn_watcher=allow_respawn)
        follow_service.stop_follow_watcher.assert_called_once_with()
        follow_service.spawn_follow_watcher.assert_not_called()
        coord._main_mutex.release.assert_called_once_with()
        coord._app.quit.assert_called_once_with()


def test_frozen_watcher_stages_to_user_dir_and_decouples_from_app_folder(tmp_path, monkeypatch) -> None:
    import os
    from pathlib import Path
    from unittest.mock import Mock
    from services import app_follow_service as follow
    from follow_watcher import parse_watcher_args

    app_dir = tmp_path / "app"
    internal_dir = app_dir / "_internal"
    internal_dir.mkdir(parents=True)
    main_exe = app_dir / "GeminiDesktopPet.exe"
    main_exe.write_bytes(b"main-exe")
    bundled_watcher = internal_dir / "FollowWatcher.exe"
    bundled_watcher.write_bytes(b"watcher-v1")
    user_cfg = tmp_path / "user_cfg"

    monkeypatch.setattr(follow.sys, "frozen", True, raising=False)
    monkeypatch.setattr(follow.sys, "executable", str(main_exe))
    monkeypatch.setattr(follow, "get_app_base_dir", lambda: str(app_dir))
    monkeypatch.setattr(follow, "get_bundled_resource_dir", lambda: str(internal_dir))
    monkeypatch.setattr(follow, "get_user_config_dir", lambda: user_cfg)

    cmd, cwd = follow.AppFollowService.get_follow_watcher_launch_spec(from_exit=True)
    staged_exe = user_cfg / "watcher" / "GeminiDesktopPet_FollowWatcher.exe"
    assert staged_exe.is_file()
    assert staged_exe.read_bytes() == b"watcher-v1"
    assert cmd == [
        str(staged_exe),
        "--follow-watcher",
        "--from-exit",
        "--target-exe",
        os.path.abspath(str(main_exe)),
    ]
    assert Path(cwd) == staged_exe.parent
    assert os.path.normcase(cwd) != os.path.normcase(str(app_dir))

    from_exit, parsed_target = parse_watcher_args(cmd[1:])
    assert from_exit is True
    assert parsed_target == os.path.abspath(str(main_exe))

    # 覆盖更新后重新同步应更新用户目录中的守护副本
    bundled_watcher.write_bytes(b"watcher-v2-updated")
    assert follow.AppFollowService.ensure_staged_watcher_exe() == staged_exe
    assert staged_exe.read_bytes() == b"watcher-v2-updated"

    # 开机启动快捷方式也应指向用户目录副本并解耦工作目录
    shortcut_mock = Mock(return_value=True)
    monkeypatch.setattr(follow, "set_startup_entry_enabled", shortcut_mock)
    assert follow.AppFollowService.set_follow_watcher_run(True) is True
    assert shortcut_mock.call_args.kwargs["target_override"] == str(staged_exe)
    assert shortcut_mock.call_args.kwargs["working_dir"] == str(staged_exe.parent)
    assert shortcut_mock.call_args.kwargs["args"] == [
        "--follow-watcher",
        "--target-exe",
        os.path.abspath(str(main_exe)),
    ]

    # 停用联动清理副本
    follow.AppFollowService.cleanup_staged_watcher_exe()
    assert not staged_exe.exists()


def test_decoupled_watcher_auto_exits_when_target_exe_removed(tmp_path, monkeypatch) -> None:
    from unittest.mock import Mock
    from services import app_follow_service as follow
    from services.settings_service import SettingsService

    settings_dir = tmp_path / "cfg"
    SettingsService(settings_dir).save(
        PetSettings(follow_antigravity=True, follow_antigravity_cli=False, follow_gemini_app=False)
    )
    app_dir = tmp_path / "app"
    app_dir.mkdir()
    target_exe = app_dir / "GeminiDesktopPet.exe"
    target_exe.write_bytes(b"exe")

    mutex = Mock()
    monkeypatch.setattr(follow, "Win32Mutex", Mock(return_value=mutex))
    monkeypatch.setattr(follow, "Win32Event", Mock())
    monkeypatch.setattr(follow.AppFollowService, "is_stop_all_signaled", lambda *args: False)
    monkeypatch.setattr(follow.AppFollowService, "is_main_instance_running", lambda *args: False)

    poll_count = 0

    def wait_and_delete(*args, **kwargs):
        nonlocal poll_count
        poll_count += 1
        if poll_count == 2 and target_exe.exists():
            target_exe.unlink()
            app_dir.rmdir()
        return -1

    monkeypatch.setattr(follow, "wait_any_events", wait_and_delete)
    launch = Mock()
    monkeypatch.setattr(follow.AppFollowService, "_launch_wake_target", launch)

    options = follow.FollowWatcherOptions(
        from_exit=True,
        target_path=str(target_exe),
        custom_settings_folder=str(settings_dir),
        sample_antigravity=lambda: _snapshot(False),
        sample_gemini=lambda: _snapshot(False),
        sample_antigravity_cli=lambda: _snapshot(False),
    )
    assert follow.AppFollowService.run_follow_watcher_core(options) == 0
    assert poll_count == 2
    assert not app_dir.exists()
    launch.assert_not_called()
    mutex.release.assert_called_once_with()

    # 目标文件启动时就已不存在时直接退出，不再获取守护锁
    mutex2 = Mock()
    monkeypatch.setattr(follow, "Win32Mutex", Mock(return_value=mutex2))
    assert follow.AppFollowService.run_follow_watcher_core(options) == 0
    mutex2.acquire.assert_not_called()
