"""原版骨架动作、打盹资源与设置兼容回归。"""
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest
from PySide6.QtCore import QEvent, QPointF, Qt
from PySide6.QtGui import QMouseEvent, QPixmap
from PySide6.QtWidgets import QApplication

from models.pet_settings import DockSide, PetSettings, PetStyleMode
from models.pet_state import PetState
from services.sound_service import PetSoundEffect
from services.sprite_manager import SpriteManager
from services.settings_service import SettingsService
from views.pet_window import PetWindow


@pytest.fixture(scope="module")
def app():
    instance = QApplication.instance() or QApplication([])
    yield instance


@pytest.fixture(scope="module")
def sprites(app):
    manager = SpriteManager()
    manager.preload_all()
    return manager


def test_standard_pose_joints_animate_and_sleep_is_static(app, sprites, tmp_path):
    from math import isfinite
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QPainter

    settings = PetSettings(style_mode=PetStyleMode.STANDARD, auto_walk=False)
    pet = PetWindow(settings, SettingsService(str(tmp_path)), sprites,
                    MagicMock(), MagicMock(), MagicMock())
    try:
        for state in PetState:
            pet._set_temporary_pose(state, 2)
            assert not pet._use_std_walk_rig
            assert pet._active_static_pixmap.toImage() == sprites.get(state, PetStyleMode.STANDARD).toImage()
        # 真正停止行走后必须恢复正面，而不是继续绘制侧面骨架。
        pet._override_pose = None
        pet._is_sleeping = False
        pet._is_walking = True
        pet._refresh_sprite_visual()
        assert pet._use_std_walk_rig
        pet._anim_time = 0.8
        pet._update_sprite_transforms()
        assert all(isfinite(v) for v in pet._std_kinematics.values())
        canvas = QPixmap(pet.size())
        canvas.fill(Qt.GlobalColor.transparent)
        painter = QPainter(canvas)
        pet._paint_stage(painter)
        painter.end()
        assert not canvas.isNull()
        pet._is_walking = False
        pet._refresh_sprite_visual()
        assert not pet._use_std_walk_rig
        assert pet._active_static_pixmap.cacheKey() == sprites.get(PetState.IDLE, PetStyleMode.STANDARD).cacheKey()
        for style in PetStyleMode:
            for side, state in ((DockSide.LEFT, PetState.EDGE_LEFT_SLEEP), (DockSide.RIGHT, PetState.EDGE_RIGHT_SLEEP)):
                settings.style_mode = style
                settings.docked_side = side
                settings.doze_mode = True
                pet._override_pose = None
                pet._is_hovering = True
                pet._refresh_sprite_visual()
                assert pet._is_dozing()
                assert pet._resolve_resting_state() == state
                assert not pet._use_std_walk_rig
                assert pet._active_static_pixmap.hasAlphaChannel()
                awake = PetState.EDGE_LEFT if side == DockSide.LEFT else PetState.EDGE_RIGHT
                assert pet._active_static_pixmap.cacheKey() != sprites.get(awake, style).cacheKey()
    finally:
        pet.close()
        pet.deleteLater()
        app.processEvents()


def test_retired_settings_are_ignored_and_removed_on_save(tmp_path):
    import json
    service = SettingsService(str(tmp_path))
    service.settings_path.write_text(json.dumps({
        "costume": {"costume_override": "fest_christmas", "auto_festival_costume": True},
        "behavior": {"doze_mode": True},
    }), encoding="utf-8")
    settings = service.load()
    assert settings.doze_mode
    assert not hasattr(settings, "costume_override")
    service.save(settings)
    saved = json.loads(service.settings_path.read_text(encoding="utf-8"))
    assert "costume" not in saved

def test_gallery_styles_preview_and_selection_persist(app, sprites, tmp_path):
    from types import SimpleNamespace
    from PySide6.QtWidgets import QPushButton
    from views.profile_window import ProfileWindow
    from app import PetApplicationCoordinator
    from services.tray_icon_service import TrayIconService

    settings = PetSettings(auto_walk=False)
    service = SettingsService(str(tmp_path))
    pet = PetWindow(settings, service, sprites, MagicMock(), MagicMock(), MagicMock())
    coordinator = SimpleNamespace(_settings=settings, _pet=pet)
    profile = ProfileWindow(sprites, settings,
        lambda state, style: PetApplicationCoordinator._on_profile_preview_state(coordinator, state, style))
    tray = TrayIconService()
    try:
        assert len(profile._gallery_cards) == len(PetState) * 2
        for menu in (pet._context_menu, tray._menu):
            assert not any("摸摸猫耳" in action.text() for action in menu.actions())
        for style in (PetStyleMode.STANDARD, PetStyleMode.CHIBI):
            button = profile.findChild(QPushButton, f"preview_{style.value}_happy")
            assert button is not None
            button.click()
            assert settings.style_mode == style
            assert service.load().style_mode == style
            assert pet._override_pose == PetState.HAPPY
            assert not pet._use_std_walk_rig
            assert pet._active_static_pixmap.cacheKey() == sprites.get(PetState.HAPPY, style).cacheKey()
            pet._override_pose = None
            pet._refresh_sprite_visual()
            assert settings.style_mode == style
        # 手动切换形象不丢失正在执行的思考动作或打盹模式。
        pet._set_temporary_pose(PetState.THINKING, 20)
        pet._set_style_mode(PetStyleMode.STANDARD)
        assert pet._override_pose == PetState.THINKING
        assert not pet._use_std_walk_rig
        pet.set_doze_mode_enabled(True)
        pet._override_pose = None
        pet._set_style_mode(PetStyleMode.CHIBI)
        assert pet._override_pose is None
        assert pet._resolve_resting_state() == PetState.SLEEPING
    finally:
        tray.dispose()
        profile.close()
        pet.close()
        pet.deleteLater()
        app.processEvents()


def test_drag_shake_puffed_dizzy_sulk_and_coax(app, sprites, tmp_path):
    settings = PetSettings(
        auto_walk=False,
        gravity_enabled=True,
        particles_enabled=False,
        show_speech_bubble=False,
    )
    sound = MagicMock()
    pet = PetWindow(settings, SettingsService(str(tmp_path)), sprites, sound, MagicMock(), MagicMock())
    clock = [100.0]

    def fake_mono() -> float:
        return clock[0]

    def make_mouse(etype: QEvent.Type, local: QPointF, gpos: QPointF, btn: Qt.MouseButton) -> QMouseEvent:
        button = btn if etype in (QEvent.Type.MouseButtonPress, QEvent.Type.MouseButtonRelease) else Qt.MouseButton.NoButton
        buttons = btn if etype != QEvent.Type.MouseButtonRelease else Qt.MouseButton.NoButton
        return QMouseEvent(
            etype,
            local,
            gpos,
            button,
            buttons,
            Qt.KeyboardModifier.NoModifier,
        )

    try:
        screen_area = (app.primaryScreen()).availableGeometry()
        start_x = screen_area.center().x() - pet.width() // 2
        start_y = screen_area.top() + 80
        pet.move(start_x, start_y)
        pet.show()
        pet._frame_timer.stop()
        pet._ai_timer.stop()
        pet._idle_speech_timer.stop()
        app.processEvents()

        local_center = QPointF(pet.width() * 0.5, pet.height() * 0.6)
        gx = float(start_x + pet.width() * 0.5)
        gy = float(start_y + pet.height() * 0.6)

        with patch("views.pet_window.time.monotonic", side_effect=fake_mono):
            # 1. 慢速普通拖拽往返不应误触炸毛或生气
            pet._on_stage_mouse_press(
                make_mouse(QEvent.Type.MouseButtonPress, local_center, QPointF(gx, gy), Qt.MouseButton.LeftButton)
            )
            # 普通拖动刚越过启动距离即可很快悬浮。
            gx += 6.0
            clock[0] += 0.06
            pet._on_stage_mouse_move(
                make_mouse(QEvent.Type.MouseMove, local_center, QPointF(gx, gy), Qt.MouseButton.LeftButton)
            )
            clock[0] += 0.07
            pet._frame_timer_tick()
            assert pet._visual_state == PetState.FLOAT
            for step in (6.0,) * 7 + (-6.0,) * 8:
                clock[0] += 0.06
                gx += step
                pet._on_stage_mouse_move(
                    make_mouse(QEvent.Type.MouseMove, local_center, QPointF(gx, gy), Qt.MouseButton.LeftButton)
                )
                pet._frame_timer_tick()
            assert pet._visual_state == PetState.FLOAT
            assert pet._shake_stage == 0
            pet._on_stage_mouse_release(
                make_mouse(QEvent.Type.MouseButtonRelease, local_center, QPointF(gx, gy), Qt.MouseButton.LeftButton)
            )

            # 单向快速拖动立即跟手；按住停下后仍能悬浮。
            pet.move(start_x, start_y)
            pet._on_stage_mouse_press(
                make_mouse(QEvent.Type.MouseButtonPress, local_center, QPointF(gx, gy), Qt.MouseButton.LeftButton)
            )
            clock[0] += 0.02
            gx += 24.0
            pet._on_stage_mouse_move(
                make_mouse(QEvent.Type.MouseMove, local_center, QPointF(gx, gy), Qt.MouseButton.LeftButton)
            )
            assert pet.x() == start_x + 24
            clock[0] += 0.15
            pet._frame_timer_tick()
            assert pet._visual_state != PetState.FLOAT
            clock[0] += 0.05
            pet._frame_timer_tick()
            assert pet._visual_state == PetState.FLOAT
            assert pet._shake_stage == 0
            pet._on_stage_mouse_release(
                make_mouse(QEvent.Type.MouseButtonRelease, local_center, QPointF(gx, gy), Qt.MouseButton.LeftButton)
            )

            # 2. 高回报率（小步长+减速+重复零位移）水平晃动依次触发 ANNOYED -> PUFFED -> REDHOT，且音效不刷屏、粒子遵守开关
            pet.move(start_x, start_y)
            clock[0] += 1.0
            gx = float(start_x + pet.width() * 0.5)
            gy = float(start_y + pet.height() * 0.6)
            pet._on_stage_mouse_press(
                make_mouse(QEvent.Type.MouseButtonPress, local_center, QPointF(gx, gy), Qt.MouseButton.LeftButton)
            )
            seen_stages: list[PetState] = []
            half_cycles = [
                (5.0, 5.0, 5.0, 5.0, 5.0, 2.0, 0.0),
                (-5.0, -5.0, -5.0, -5.0, -5.0, -2.0, 0.0),
                (5.0, 5.0, 5.0, 5.0, 5.0, 2.0, 0.0),
                (-5.0, -5.0, -5.0, -5.0, -5.0, -2.0, 0.0),
                (5.0, 5.0, 5.0, 5.0, 5.0, 2.0, 0.0),
                (-5.0, -5.0, -5.0, -5.0, -5.0, -2.0, 0.0),
            ]
            # 长按后才开始快速移动，识别窗口应从实际拖动起算。
            clock[0] += 0.8
            for cycle in half_cycles:
                for step in cycle:
                    clock[0] += 0.015
                    gx += step
                    pet._on_stage_mouse_move(
                        make_mouse(QEvent.Type.MouseMove, local_center, QPointF(gx, gy), Qt.MouseButton.LeftButton)
                    )
                    assert pet._visual_state != PetState.FLOAT
                    pet._frame_timer_tick()
                    assert pet._visual_state != PetState.FLOAT
                    if pet._override_pose in (PetState.ANNOYED, PetState.PUFFED, PetState.REDHOT):
                        if not seen_stages or seen_stages[-1] != pet._override_pose:
                            seen_stages.append(pet._override_pose)

            assert seen_stages == [PetState.ANNOYED, PetState.PUFFED, PetState.REDHOT]
            angry_calls = [
                c for c in sound.play.call_args_list if c.args and c.args[0] == PetSoundEffect.ANGRY
            ]
            assert len(angry_calls) == 1
            assert not any(pt.glyph == "💢" for pt in pet._particles)
            pet._on_stage_mouse_release(
                make_mouse(QEvent.Type.MouseButtonRelease, local_center, QPointF(gx, gy), Qt.MouseButton.LeftButton)
            )

            assert pet._visual_state == PetState.DIZZY
            assert pet._pending_sulk_after_dizzy and pet._is_sulk
            assert pet._is_falling
            for _ in range(60):
                pet._last_tick_utc = datetime.now(timezone.utc) - timedelta(seconds=0.05)
                pet._frame_timer_tick()
                assert pet._visual_state == PetState.DIZZY
                if not pet._is_falling:
                    break
            assert not pet._is_falling
            sound.play.assert_any_call(PetSoundEffect.BOING)
            assert sound.play.call_args_list.count(angry_calls[0]) == 1
            pet._override_until_utc = datetime.now(timezone.utc) - timedelta(seconds=0.1)
            pet._frame_timer_tick()
            assert pet._visual_state == PetState.SULK

            # 3. 持续普通频率的垂直摇晃产生怒气粒子，松手进入眩晕和重力下落。
            settings.particles_enabled = True
            pet._is_sulk = False
            pet._is_falling = False
            pet._override_pose = None
            pet.move(start_x, start_y)
            clock[0] += 1.5
            gx = float(start_x + pet.width() * 0.5)
            gy = float(start_y + pet.height() * 0.6)
            pet._on_stage_mouse_press(
                make_mouse(QEvent.Type.MouseButtonPress, local_center, QPointF(gx, gy), Qt.MouseButton.LeftButton)
            )
            for sign in (1, -1) * 5:
                for _ in range(6):
                    clock[0] += 0.035
                    gy += sign * 10.0
                    pet._on_stage_mouse_move(
                        make_mouse(QEvent.Type.MouseMove, local_center, QPointF(gx, gy), Qt.MouseButton.LeftButton)
                    )
                    pet._frame_timer_tick()
                    assert pet._visual_state not in (PetState.FLOAT, PetState.REDHOT)
            assert pet._override_pose == PetState.PUFFED
            assert any(pt.glyph == "💢" for pt in pet._particles)

            pet._on_stage_mouse_release(
                make_mouse(QEvent.Type.MouseButtonRelease, local_center, QPointF(gx, gy), Qt.MouseButton.LeftButton)
            )
            assert pet._override_pose == PetState.DIZZY
            assert pet._is_sulk is True
            assert pet._pending_sulk_after_dizzy is True
            assert pet._is_falling is True

            # 4. 松手后惯性划过头部不立即抵消 DIZZY
            sprite_rect = pet._get_local_sprite_rect()
            head_y = sprite_rect.top() + sprite_rect.height() * 0.25
            for offset_x in (0.0, 25.0, 50.0, 75.0, 100.0):
                clock[0] += 0.05
                hx = sprite_rect.left() + 10.0 + offset_x
                pet._on_stage_mouse_move(
                    make_mouse(
                        QEvent.Type.MouseMove,
                        QPointF(hx, head_y),
                        QPointF(pet.x() + hx, pet.y() + head_y),
                        Qt.MouseButton.NoButton,
                    )
                )
            assert pet._override_pose == PetState.DIZZY
            assert pet._is_sulk is True

            # 5. 头晕结束后主循环直接衔接 SULK；闹脾气期间轻拖松手仍保持可见 SULK，不隐形卡死
            pet._is_falling = False
            pet._override_until_utc = datetime.now(timezone.utc) - timedelta(seconds=0.1)
            pet._frame_timer_tick()
            assert pet._override_pose == PetState.SULK
            assert pet._is_sulk is True

            clock[0] += 1.0
            pet.move(start_x, start_y)
            pet._on_stage_mouse_press(
                make_mouse(QEvent.Type.MouseButtonPress, local_center, QPointF(gx, gy), Qt.MouseButton.LeftButton)
            )
            clock[0] += 0.1
            gx += 12.0
            pet._on_stage_mouse_move(
                make_mouse(QEvent.Type.MouseMove, local_center, QPointF(gx, gy), Qt.MouseButton.LeftButton)
            )
            pet._on_stage_mouse_release(
                make_mouse(QEvent.Type.MouseButtonRelease, local_center, QPointF(gx, gy), Qt.MouseButton.LeftButton)
            )
            assert pet._is_sulk is True
            assert pet._override_pose == PetState.SULK

            # 6. 两次摸头安抚依次进入 COAXED -> FORGIVEN 并解除闹脾气
            pet.trigger_pat()
            assert pet._is_sulk is True
            assert pet._override_pose == PetState.COAXED
            pet.trigger_pat()
            assert pet._is_sulk is False
            assert pet._override_pose == PetState.FORGIVEN

            # 7. 高频小幅抖动（12px 半周期、35ms 换向）稳定进 REDHOT 且不会在持续晃动中误切回 FLOAT
            pet._override_pose = None
            pet._is_falling = False
            pet.move(start_x, start_y)
            clock[0] += 2.0
            gx = float(start_x + pet.width() * 0.5)
            gy = float(start_y + pet.height() * 0.6)
            pet._on_stage_mouse_press(
                make_mouse(QEvent.Type.MouseButtonPress, local_center, QPointF(gx, gy), Qt.MouseButton.LeftButton)
            )

            def shake_to_redhot(*, from_float=False):
                nonlocal gx
                hf_stages: list[PetState] = []
                for sign in (1, -1) * 4:
                    for _ in range(3):
                        clock[0] += 0.011
                        gx += sign * 4.0
                        pet._on_stage_mouse_move(
                            make_mouse(QEvent.Type.MouseMove, local_center, QPointF(gx, gy), Qt.MouseButton.LeftButton)
                        )
                        if not from_float or hf_stages:
                            assert pet._visual_state != PetState.FLOAT
                        pet._frame_timer_tick()
                        if not from_float or hf_stages:
                            assert pet._visual_state != PetState.FLOAT
                        if pet._override_pose in (PetState.ANNOYED, PetState.PUFFED, PetState.REDHOT):
                            if not hf_stages or hf_stages[-1] != pet._override_pose:
                                hf_stages.append(pet._override_pose)
                assert hf_stages == [PetState.ANNOYED, PetState.PUFFED, PetState.REDHOT]
                assert pet._is_shaking is True

            shake_to_redhot()

            # 短暂停顿容许继续换向；停稳后无需新的鼠标事件即可结束立绘与抖动。
            clock[0] += 0.08
            pet._frame_timer_tick()
            assert pet._visual_state == PetState.REDHOT
            clock[0] += 0.12
            pet._frame_timer_tick()
            assert not pet._is_shaking
            assert pet._visual_state == PetState.IDLE
            assert pet._sprite_trans_x == 0.0

            # 保持按住，反复停顿和续晃；暂停时的细微鼠标抖动不应播放悬浮。
            for pause in (0.24, 0.72):
                shake_to_redhot()
                clock[0] += pause
                pet._frame_timer_tick()
                assert pet._visual_state == PetState.IDLE
                for delta in (1.0, -1.0) * 4:
                    gx += delta
                    clock[0] += 0.025
                    pet._on_stage_mouse_move(
                        make_mouse(QEvent.Type.MouseMove, local_center, QPointF(gx, gy), Qt.MouseButton.LeftButton)
                    )
                    pet._frame_timer_tick()
                    assert pet._visual_state == PetState.IDLE

            # 停晃后轻轻移开即可悬浮，不必重新拖满一个摇晃行程。
            for _ in range(3):
                clock[0] += 0.035
                gx += 2.0
                pet._on_stage_mouse_move(
                    make_mouse(QEvent.Type.MouseMove, local_center, QPointF(gx, gy), Qt.MouseButton.LeftButton)
                )
                pet._frame_timer_tick()
            assert pet._visual_state == PetState.FLOAT

            # 单向加速和一次快速修正都属于正常拖动，立绘应稳定。
            for delta in (16.0, 16.0, 16.0, -12.0):
                clock[0] += 0.025
                gx += delta
                pet._on_stage_mouse_move(
                    make_mouse(QEvent.Type.MouseMove, local_center, QPointF(gx, gy), Qt.MouseButton.LeftButton)
                )
                pet._frame_timer_tick()
                assert pet._visual_state == PetState.FLOAT
            clock[0] += 0.22
            pet._frame_timer_tick()
            assert pet._visual_state == PetState.FLOAT

            # 晃动直接改为平移，保持鼠标按下时从炸毛直接衔接悬浮。
            shake_to_redhot(from_float=True)
            for _ in range(12):
                clock[0] += 0.035
                gx -= 3.0
                pet._on_stage_mouse_move(
                    make_mouse(QEvent.Type.MouseMove, local_center, QPointF(gx, gy), Qt.MouseButton.LeftButton)
                )
                pet._frame_timer_tick()
                assert pet._visual_state in (PetState.REDHOT, PetState.FLOAT)
            assert pet._visual_state == PetState.FLOAT

            # 若停稳后的松手先于下一帧，也应安静落地。
            shake_to_redhot(from_float=True)
            clock[0] += 0.20
            sound.reset_mock()
            pet._on_stage_mouse_release(
                make_mouse(QEvent.Type.MouseButtonRelease, local_center, QPointF(gx, gy), Qt.MouseButton.LeftButton)
            )

            assert pet._is_falling
            for _ in range(60):
                pet._last_tick_utc = datetime.now(timezone.utc) - timedelta(seconds=0.05)
                pet._frame_timer_tick()
                assert pet._visual_state not in (PetState.DIZZY, PetState.SULK)
                assert not pet._pending_sulk_after_dizzy
                assert pet._landing_squash == 0.0
                if not pet._is_falling:
                    break
            assert not pet._is_falling
            assert pet.y() + pet.height() == screen_area.bottom() + 1
            sound.play.assert_not_called()

            # 8. 处于未过期 ANNOYED 时立即再次点按并拖动，起步不闪回 FLOAT；普通反重力模式拖拽与松手悬浮正常
            pet._is_sulk = False
            pet._pending_sulk_after_dizzy = False
            pet._set_temporary_pose(PetState.ANNOYED, 1.8)
            pet._shake_stage = 1
            pet._shake_energy = 1.2
            pet._shake_last_active_mono = clock[0]
            clock[0] += 0.1
            pet._on_stage_mouse_press(
                make_mouse(QEvent.Type.MouseButtonPress, local_center, QPointF(gx, gy), Qt.MouseButton.LeftButton)
            )
            clock[0] += 0.02
            gx += 10.0
            pet._on_stage_mouse_move(
                make_mouse(QEvent.Type.MouseMove, local_center, QPointF(gx, gy), Qt.MouseButton.LeftButton)
            )
            assert pet._override_pose == PetState.ANNOYED
            pet._on_stage_mouse_release(
                make_mouse(QEvent.Type.MouseButtonRelease, local_center, QPointF(gx, gy), Qt.MouseButton.LeftButton)
            )

            # 普通反重力模式下的正常点击与拖拽反馈正常
            clock[0] += 2.0
            settings.anti_gravity_mode = True
            pet._override_pose = None
            pet._is_falling = False
            pet._on_stage_mouse_press(
                make_mouse(QEvent.Type.MouseButtonPress, local_center, QPointF(gx, gy), Qt.MouseButton.LeftButton)
            )
            clock[0] += 0.08
            gx += 16.0
            pet._on_stage_mouse_move(
                make_mouse(QEvent.Type.MouseMove, local_center, QPointF(gx, gy), Qt.MouseButton.LeftButton)
            )
            clock[0] += 0.15
            pet._frame_timer_tick()
            assert pet._is_shaking is False
            assert pet._override_pose == PetState.FLOAT
            pet._on_stage_mouse_release(
                make_mouse(QEvent.Type.MouseButtonRelease, local_center, QPointF(gx, gy), Qt.MouseButton.LeftButton)
            )
            assert pet._is_falling is False
            assert pet._resolve_resting_state() == PetState.FLOAT
    finally:
        pet.close()
        pet.deleteLater()
        app.processEvents()
