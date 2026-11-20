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
            for step in (6.0,) * 8 + (-6.0,) * 8:
                clock[0] += 0.06
                gx += step
                pet._on_stage_mouse_move(
                    make_mouse(QEvent.Type.MouseMove, local_center, QPointF(gx, gy), Qt.MouseButton.LeftButton)
                )
            assert pet._override_pose == PetState.FLOAT
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
            for cycle in half_cycles:
                for step in cycle:
                    clock[0] += 0.015
                    gx += step
                    pet._on_stage_mouse_move(
                        make_mouse(QEvent.Type.MouseMove, local_center, QPointF(gx, gy), Qt.MouseButton.LeftButton)
                    )
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

            # 3. 启用粒子后，垂直晃动同样触发炸毛并产生怒气粒子；半空松手进入 DIZZY 且触发重力下落
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
            for sign in (1, -1, 1, -1, 1):
                for _ in range(6):
                    clock[0] += 0.015
                    gy += sign * 5.0
                    pet._on_stage_mouse_move(
                        make_mouse(QEvent.Type.MouseMove, local_center, QPointF(gx, gy), Qt.MouseButton.LeftButton)
                    )
            assert pet._override_pose == PetState.REDHOT
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
            hf_stages: list[PetState] = []
            for sign in (1, -1, 1, -1, 1, -1, 1, -1):
                for _ in range(3):
                    clock[0] += 0.011
                    gx += sign * 4.0
                    pet._on_stage_mouse_move(
                        make_mouse(QEvent.Type.MouseMove, local_center, QPointF(gx, gy), Qt.MouseButton.LeftButton)
                    )
                    if pet._override_pose in (PetState.ANNOYED, PetState.PUFFED, PetState.REDHOT):
                        if not hf_stages or hf_stages[-1] != pet._override_pose:
                            hf_stages.append(pet._override_pose)
            assert hf_stages == [PetState.ANNOYED, PetState.PUFFED, PetState.REDHOT]
            assert pet._is_shaking is True

            # 持续高频晃动期间即使计时阈值到达，主循环也绝不回退到反重力动作 FLOAT
            pet._override_until_utc = datetime.now(timezone.utc) - timedelta(seconds=0.1)
            pet._frame_timer_tick()
            assert pet._override_pose == PetState.REDHOT
            assert pet._override_pose != PetState.FLOAT

            # 真正停止晃动超过保持窗口且能量消退后，恢复正常拖拽悬浮 FLOAT
            clock[0] += 1.0
            pet._shake_energy = 0.0
            pet._override_until_utc = datetime.now(timezone.utc) - timedelta(seconds=0.1)
            pet._frame_timer_tick()
            assert pet._is_shaking is False
            assert pet._override_pose == PetState.FLOAT
            pet._on_stage_mouse_release(
                make_mouse(QEvent.Type.MouseButtonRelease, local_center, QPointF(gx, gy), Qt.MouseButton.LeftButton)
            )

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
