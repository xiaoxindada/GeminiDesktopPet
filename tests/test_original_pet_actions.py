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


def test_poke_bristle_dizzy_shake_sulk_and_coax(app, sprites, tmp_path):
    import math
    from PySide6.QtGui import QPainter

    settings = PetSettings(
        auto_walk=False,
        gravity_enabled=True,
        particles_enabled=False,
        show_speech_bubble=False,
    )
    sound = MagicMock()
    pet = PetWindow(settings, SettingsService(str(tmp_path)), sprites, sound, MagicMock(), MagicMock())
    quota_hits: list[int] = []
    pet.open_quota_requested.connect(lambda: quota_hits.append(1))
    clock = [100.0]

    def fake_mono() -> float:
        return clock[0]

    def make_mouse(etype: QEvent.Type, local: QPointF, gpos: QPointF, btn: Qt.MouseButton) -> QMouseEvent:
        button = (
            btn
            if etype in (QEvent.Type.MouseButtonPress, QEvent.Type.MouseButtonDblClick, QEvent.Type.MouseButtonRelease)
            else Qt.MouseButton.NoButton
        )
        buttons = btn if etype != QEvent.Type.MouseButtonRelease else Qt.MouseButton.NoButton
        return QMouseEvent(
            etype,
            local,
            gpos,
            button,
            buttons,
            Qt.KeyboardModifier.NoModifier,
        )

    def click_point(local_pt: QPointF, *, is_dbl: bool = False) -> None:
        gpos = QPointF(pet.x() + local_pt.x(), pet.y() + local_pt.y())
        pet._on_stage_mouse_press(
            make_mouse(QEvent.Type.MouseButtonPress, local_pt, gpos, Qt.MouseButton.LeftButton)
        )
        if is_dbl:
            pet._on_stage_mouse_double_click(
                make_mouse(QEvent.Type.MouseButtonDblClick, local_pt, gpos, Qt.MouseButton.LeftButton)
            )
        pet._on_stage_mouse_release(
            make_mouse(QEvent.Type.MouseButtonRelease, local_pt, gpos, Qt.MouseButton.LeftButton)
        )

    def finish_current_pose() -> None:
        pet._override_until_utc = datetime.now(timezone.utc) - timedelta(seconds=0.1)
        pet._frame_timer_tick()

    try:
        screen_area = app.primaryScreen().availableGeometry()
        start_x = screen_area.center().x() - pet.width() // 2
        floor_top = screen_area.bottom() + 1 - pet.height()
        pet.move(start_x, floor_top)
        pet.show()
        pet._frame_timer.stop()
        pet._ai_timer.stop()
        pet._idle_speech_timer.stop()
        app.processEvents()

        sprite_rect = pet._get_local_sprite_rect()
        body_local = QPointF(sprite_rect.center().x(), sprite_rect.top() + sprite_rect.height() * 0.72)
        head_local = QPointF(sprite_rect.center().x(), sprite_rect.top() + sprite_rect.height() * 0.22)

        with patch("views.pet_window.time.monotonic", side_effect=fake_mono):
            # 1. 连戳身体进入 ANNOYED（第 2 击为 Qt 双击事件但不误开额度），播完前不可被戳/摸头/双击/拖拽打断
            for idx in range(3):
                clock[0] += 0.16
                click_point(body_local, is_dbl=(idx == 1))
                if idx < 2:
                    assert pet._poke_stage == 0
            assert pet._override_pose == PetState.ANNOYED
            assert not quota_hits
            assert not pet._quota_double_click_timer.isActive()

            # ANNOYED 播放期间：再戳身体、摸头、双击头部、轻拖都不会中断 ANNOYED
            clock[0] += 0.2
            click_point(body_local)
            assert pet._override_pose == PetState.ANNOYED
            clock[0] += 0.2
            click_point(head_local, is_dbl=True)
            assert pet._override_pose == PetState.ANNOYED
            assert not quota_hits
            gx = float(pet.x() + body_local.x())
            gy = float(pet.y() + body_local.y())
            pet._on_stage_mouse_press(
                make_mouse(QEvent.Type.MouseButtonPress, body_local, QPointF(gx, gy), Qt.MouseButton.LeftButton)
            )
            clock[0] += 0.08
            gx += 16.0
            pet._on_stage_mouse_move(
                make_mouse(QEvent.Type.MouseMove, body_local, QPointF(gx, gy), Qt.MouseButton.LeftButton)
            )
            assert pet._override_pose == PetState.ANNOYED
            pet._on_stage_mouse_release(
                make_mouse(QEvent.Type.MouseButtonRelease, body_local, QPointF(gx, gy), Qt.MouseButton.LeftButton)
            )
            assert pet._override_pose == PetState.ANNOYED

            # ANNOYED 完整播完后，在怒气记忆期内再戳身体顺阶进入 PUFFED，且 PUFFED 播完前同样不可中断
            clock[0] += 1.5
            finish_current_pose()
            assert pet._override_pose is None
            assert pet._poke_stage == 1

            clock[0] += 0.4
            click_point(body_local)
            assert pet._override_pose == PetState.PUFFED
            clock[0] += 0.2
            click_point(body_local)
            pet.trigger_pat()
            assert pet._override_pose == PetState.PUFFED

            # PUFFED 完整播完后，再戳身体爆发 REDHOT（关闭粒子时不产生怒气/火花粒子，音效只播一次）
            clock[0] += 1.8
            finish_current_pose()
            assert pet._override_pose is None
            assert pet._poke_stage == 2

            clock[0] += 0.4
            click_point(body_local)
            assert pet._override_pose == PetState.REDHOT
            assert pet._is_sulk is True
            assert pet._pending_sulk_after_pose is True
            assert pet._bristle_burst == pytest.approx(1.0)
            angry_calls = [
                c for c in sound.play.call_args_list if c.args and c.args[0] == PetSoundEffect.ANGRY
            ]
            assert len(angry_calls) == 1
            assert not pet._particles

            # REDHOT 播完前不被摸头或继续戳击打断；启用粒子后炸毛动效弹跳膨胀且舞台绘制正常
            clock[0] += 0.2
            pet.trigger_pat()
            click_point(body_local)
            assert pet._override_pose == PetState.REDHOT
            assert pet._is_sulk is True

            settings.particles_enabled = True
            pet._bristle_burst = 0.65
            pet._spawn_bristle_particles(3)
            pet._update_sprite_transforms()
            assert pet._sprite_trans_y < 0.0
            assert pet._sprite_scale_x > 1.05 and pet._sprite_scale_y > 1.05
            assert any(pt.glyph in ("💢", "⚡", "✦", "✧") for pt in pet._particles)
            canvas = QPixmap(pet.size())
            canvas.fill(Qt.GlobalColor.transparent)
            painter = QPainter(canvas)
            pet._paint_stage(painter)
            painter.end()

            # 2. 炸毛播完衔接 SULK（持续 5 秒）；连点触发的生气播完回常态前完全禁止点击（含戳身体、点头部、双击与轻拖）
            clock[0] += 2.6
            finish_current_pose()
            assert pet._override_pose == PetState.SULK
            assert pet._is_sulk is True
            assert pet._is_action_playback_locked() is True
            sulk_remaining = (pet._override_until_utc - datetime.now(timezone.utc)).total_seconds()
            assert 4.0 < sulk_remaining <= 5.1

            pet._poke_recoil = 0.0
            for _ in range(3):
                clock[0] += 0.25
                click_point(body_local)
                assert pet._override_pose == PetState.SULK
                assert pet._is_sulk is True
                assert pet._poke_recoil == pytest.approx(0.0)
                assert pet._sulk_poke_count == 0
                assert not quota_hits

            clock[0] += 0.2
            click_point(head_local)
            assert pet._override_pose == PetState.SULK
            assert pet._is_sulk is True
            clock[0] += 0.2
            click_point(head_local, is_dbl=True)
            assert pet._override_pose == PetState.SULK
            assert not quota_hits
            pet.trigger_pat()
            assert pet._override_pose == PetState.SULK
            assert pet._is_sulk is True

            gx = float(pet.x() + body_local.x())
            gy = float(pet.y() + body_local.y())
            pet._on_stage_mouse_press(
                make_mouse(QEvent.Type.MouseButtonPress, body_local, QPointF(gx, gy), Qt.MouseButton.LeftButton)
            )
            clock[0] += 0.08
            gx += 16.0
            pet._on_stage_mouse_move(
                make_mouse(QEvent.Type.MouseMove, body_local, QPointF(gx, gy), Qt.MouseButton.LeftButton)
            )
            assert pet._override_pose == PetState.SULK
            pet._on_stage_mouse_release(
                make_mouse(QEvent.Type.MouseButtonRelease, body_local, QPointF(gx, gy), Qt.MouseButton.LeftButton)
            )
            assert pet._override_pose == PetState.SULK
            assert pet._is_sulk is True
            assert pet._is_action_playback_locked() is True

            # 3. SULK 锁定期间按下左键、直到 5 秒播放结束后再松手，也不偷跑触发点击；播完回常态后恢复可点击
            clock[0] += 1.0
            gx = float(pet.x() + body_local.x())
            gy = float(pet.y() + body_local.y())
            pet._on_stage_mouse_press(
                make_mouse(QEvent.Type.MouseButtonPress, body_local, QPointF(gx, gy), Qt.MouseButton.LeftButton)
            )
            clock[0] += 4.5
            finish_current_pose()
            assert pet._override_pose is None
            assert pet._is_sulk is False
            assert pet._is_action_playback_locked() is False
            pet._on_stage_mouse_release(
                make_mouse(QEvent.Type.MouseButtonRelease, body_local, QPointF(gx, gy), Qt.MouseButton.LeftButton)
            )
            assert pet._override_pose is None
            assert pet._poke_count == 0
            assert not quota_hits

            clock[0] += 0.3
            click_point(body_local)
            assert pet._poke_count == 1
            assert pet._override_pose is not None
            assert pet._is_action_playback_locked() is False
            pet._override_pose = None
            pet._poke_count = 0

            # 4. 常态下双击身体停顿后正常触发额度；双击头部立即触发额度
            clock[0] += 3.0
            click_point(body_local)
            clock[0] += 0.18
            click_point(body_local, is_dbl=True)
            assert pet._quota_double_click_timer.isActive()
            pet._quota_double_click_timer.stop()
            pet._fire_pending_body_double_click_quota()
            assert len(quota_hits) == 1

            clock[0] += 1.0
            click_point(head_local, is_dbl=True)
            assert len(quota_hits) == 2

            # 5. 慢速拖拽往返保持正常反重力反馈 FLOAT，不误触生气或眩晕
            pet._override_pose = None
            pet._particles.clear()
            pet.move(start_x, screen_area.top() + 80)
            clock[0] += 1.0
            gx = float(pet.x() + body_local.x())
            gy = float(pet.y() + body_local.y())
            pet._on_stage_mouse_press(
                make_mouse(QEvent.Type.MouseButtonPress, body_local, QPointF(gx, gy), Qt.MouseButton.LeftButton)
            )
            for step in (6.0,) * 8 + (-6.0,) * 8:
                clock[0] += 0.06
                gx += step
                pet._on_stage_mouse_move(
                    make_mouse(QEvent.Type.MouseMove, body_local, QPointF(gx, gy), Qt.MouseButton.LeftButton)
                )
            assert pet._override_pose == PetState.FLOAT
            assert pet._is_shaking is False
            assert pet._shake_stage == 0
            pet._on_stage_mouse_release(
                make_mouse(QEvent.Type.MouseButtonRelease, body_local, QPointF(gx, gy), Qt.MouseButton.LeftButton)
            )
            pet._is_falling = False
            pet._override_pose = None

            # 6. 完整主流程：摇晃 -> 烦恼(播完) -> 鼓嘴(播完) -> 炸毛(REDHOT) -> 松手下落眩晕(DIZZY) -> 生气(SULK，5秒锁播) -> 播完恢复常态
            pet.move(start_x, screen_area.top() + 80)
            clock[0] += 1.0
            gx = float(pet.x() + body_local.x())
            gy = float(pet.y() + body_local.y())
            pet._on_stage_mouse_press(
                make_mouse(QEvent.Type.MouseButtonPress, body_local, QPointF(gx, gy), Qt.MouseButton.LeftButton)
            )

            def shake_burst(cycles: int = 3) -> None:
                nonlocal gx
                for _ in range(cycles):
                    for step in (6.0, 6.0, 6.0, 6.0, -6.0, -6.0, -6.0, -6.0):
                        clock[0] += 0.015
                        gx += step
                        pet._on_stage_mouse_move(
                            make_mouse(QEvent.Type.MouseMove, body_local, QPointF(gx, gy), Qt.MouseButton.LeftButton)
                        )

            # 首次持续剧烈摇晃进入 ANNOYED，且 ANNOYED 播完前不被中途切到 PUFFED 或 REDHOT
            shake_burst(4)
            assert pet._override_pose == PetState.ANNOYED
            assert pet._shake_stage == 1
            assert pet._is_shaking is True

            # ANNOYED 完整播完后，仍在摇晃期同一帧顺阶进入 PUFFED（不闪回 FLOAT），且 PUFFED 播完前不可中断
            clock[0] += 0.1
            finish_current_pose()
            assert pet._override_pose == PetState.PUFFED
            assert pet._shake_stage == 2
            shake_burst(2)
            assert pet._override_pose == PetState.PUFFED

            # PUFFED 完整播完后，同一帧顺阶爆发 REDHOT（炸毛）并具备膨胀弹跳动效，持续摇晃期间绝不闪回 FLOAT
            clock[0] += 0.1
            finish_current_pose()
            assert pet._override_pose == PetState.REDHOT
            assert pet._shake_stage == 3
            assert pet._bristle_burst > 0.85
            pet._bristle_burst = 0.65
            pet._update_sprite_transforms()
            assert pet._sprite_scale_x > 1.05 and pet._sprite_scale_y > 1.05
            shake_burst(2)
            finish_current_pose()
            assert pet._override_pose == PetState.REDHOT
            assert pet._override_pose != PetState.FLOAT

            # 炸毛状态下松手 -> 进入下落眩晕 DIZZY（含重力下落、Q 弹落地与 3D 绕头星光光环绘制）
            pet._on_stage_mouse_release(
                make_mouse(QEvent.Type.MouseButtonRelease, body_local, QPointF(gx, gy), Qt.MouseButton.LeftButton)
            )
            assert pet._override_pose == PetState.DIZZY
            assert pet._is_sulk is True
            assert pet._pending_sulk_after_pose is True
            assert pet._is_falling is True

            for _ in range(45):
                if not pet._is_falling:
                    break
                pet._last_tick_utc = datetime.now(timezone.utc) - timedelta(seconds=0.04)
                pet._frame_timer_tick()
            assert pet._is_falling is False
            assert pet._landing_squash > 0.0
            assert pet._override_pose == PetState.DIZZY

            for style in (PetStyleMode.CHIBI, PetStyleMode.STANDARD):
                settings.style_mode = style
                pet._refresh_sprite_visual()
                pet._update_sprite_transforms()
                dizzy_canvas = QPixmap(pet.size())
                dizzy_canvas.fill(Qt.GlobalColor.transparent)
                p = QPainter(dizzy_canvas)
                pet._paint_stage(p)
                p.end()
            settings.style_mode = PetStyleMode.CHIBI
            pet._refresh_sprite_visual()

            # 眩晕播完前不可被摸头或戳击打断；播完后直接衔接生气 SULK（持续 5 秒，期间同样屏蔽所有交互）
            clock[0] += 1.0
            pet.trigger_pat()
            click_point(body_local)
            assert pet._override_pose == PetState.DIZZY

            clock[0] += 2.0
            finish_current_pose()
            assert pet._override_pose == PetState.SULK
            assert pet._is_sulk is True
            assert pet._is_action_playback_locked() is True
            dizzy_sulk_remaining = (pet._override_until_utc - datetime.now(timezone.utc)).total_seconds()
            assert 4.0 < dizzy_sulk_remaining <= 5.1

            pats_before = settings.total_pats
            fav_before = settings.favorability
            quota_before = len(quota_hits)
            pet._poke_recoil = 0.0
            pet.trigger_pat()
            click_point(body_local)
            click_point(head_local)
            click_point(head_local, is_dbl=True)
            assert pet._override_pose == PetState.SULK
            assert pet._is_sulk is True
            assert settings.total_pats == pats_before
            assert settings.favorability == fav_before
            assert pet._poke_recoil == pytest.approx(0.0)
            assert len(quota_hits) == quota_before

            clock[0] += 5.0
            finish_current_pose()
            assert pet._override_pose is None
            assert pet._is_sulk is False
            assert pet._is_action_playback_locked() is False

            pet.trigger_pat()
            assert pet._override_pose == PetState.PAT
            assert settings.total_pats == pats_before + 1
            pet._override_pose = None

            # 7. 拖拽画圈同样驱动 烦恼 -> 鼓嘴 -> 炸毛；完全炸毛后中途停止晃动，
            #    未满 2 秒保持炸毛，满 2 秒后自动回到反重力 FLOAT，松手按正常下落 -> 落地 Q 弹处理
            pet.move(start_x, screen_area.top() + 80)
            clock[0] += 1.5
            cx = float(pet.x() + body_local.x())
            cy = float(pet.y() + body_local.y())
            radius = 65.0
            gx = cx + radius
            gy = cy
            pet._on_stage_mouse_press(
                make_mouse(QEvent.Type.MouseButtonPress, body_local, QPointF(gx, gy), Qt.MouseButton.LeftButton)
            )
            for step_idx in range(1, 56):
                clock[0] += 0.025
                theta = (2.0 * math.pi * 1.6 * step_idx) / 55.0
                gx = cx + radius * math.cos(theta)
                gy = cy + radius * math.sin(theta)
                pet._on_stage_mouse_move(
                    make_mouse(QEvent.Type.MouseMove, body_local, QPointF(gx, gy), Qt.MouseButton.LeftButton)
                )
            assert pet._override_pose == PetState.ANNOYED
            finish_current_pose()
            assert pet._override_pose == PetState.PUFFED
            finish_current_pose()
            assert pet._override_pose == PetState.REDHOT
            assert pet._shake_stage == 3

            # 停晃 1.5 秒（< 2 秒）：仍处于完全炸毛态，不提前切回反重力
            clock[0] += 1.5
            pet._frame_timer_tick()
            assert pet._override_pose == PetState.REDHOT
            assert pet._is_shaking is True

            # 停晃累计满 2 秒（即使残余能量尚未自然归零）：自动恢复反重力 FLOAT，松手正常下落 Q 弹
            clock[0] += 0.6
            pet._frame_timer_tick()
            assert pet._is_shaking is False
            assert pet._shake_stage == 0
            assert pet._override_pose == PetState.FLOAT

            pet._landing_squash = 0.0
            pet._on_stage_mouse_release(
                make_mouse(QEvent.Type.MouseButtonRelease, body_local, QPointF(gx, gy), Qt.MouseButton.LeftButton)
            )
            assert pet._is_falling is True
            assert pet._override_pose is None
            assert pet._is_sulk is False
            for _ in range(50):
                if not pet._is_falling:
                    break
                pet._last_tick_utc = datetime.now(timezone.utc) - timedelta(seconds=0.04)
                pet._frame_timer_tick()
            assert pet._is_falling is False
            assert pet._landing_squash > 0.0
            assert pet._override_pose is None
            assert pet._is_sulk is False

            # 8. 正常高空拖拽与松手落地：拖拽显示反重力 FLOAT，松手正常下落并 Q 弹落地，不触发眩晕或生气
            pet._is_sulk = False
            pet._override_pose = None
            pet._is_falling = False
            pet._landing_squash = 0.0
            high_y = screen_area.top() + 40
            pet.move(start_x, high_y)
            clock[0] += 1.5
            gx = float(pet.x() + body_local.x())
            gy = float(pet.y() + body_local.y())
            pet._on_stage_mouse_press(
                make_mouse(QEvent.Type.MouseButtonPress, body_local, QPointF(gx, gy), Qt.MouseButton.LeftButton)
            )
            clock[0] += 0.1
            gx += 10.0
            pet._on_stage_mouse_move(
                make_mouse(QEvent.Type.MouseMove, body_local, QPointF(gx, gy), Qt.MouseButton.LeftButton)
            )
            assert pet._override_pose == PetState.FLOAT
            pet._on_stage_mouse_release(
                make_mouse(QEvent.Type.MouseButtonRelease, body_local, QPointF(gx, gy), Qt.MouseButton.LeftButton)
            )
            assert pet._is_falling is True
            assert pet._override_pose is None
            for _ in range(50):
                if not pet._is_falling:
                    break
                pet._last_tick_utc = datetime.now(timezone.utc) - timedelta(seconds=0.04)
                pet._frame_timer_tick()
            assert pet._is_falling is False
            assert pet._landing_squash > 0.0
            assert pet._override_pose is None
            assert pet._is_sulk is False

            # 9. 普通反重力模式下的正常点击与拖拽反馈正常
            clock[0] += 2.0
            settings.anti_gravity_mode = True
            pet._override_pose = None
            pet._is_falling = False
            gx = float(pet.x() + body_local.x())
            gy = float(pet.y() + body_local.y())
            pet._on_stage_mouse_press(
                make_mouse(QEvent.Type.MouseButtonPress, body_local, QPointF(gx, gy), Qt.MouseButton.LeftButton)
            )
            clock[0] += 0.08
            gx += 16.0
            pet._on_stage_mouse_move(
                make_mouse(QEvent.Type.MouseMove, body_local, QPointF(gx, gy), Qt.MouseButton.LeftButton)
            )
            assert pet._is_shaking is False
            assert pet._override_pose == PetState.FLOAT
            pet._on_stage_mouse_release(
                make_mouse(QEvent.Type.MouseButtonRelease, body_local, QPointF(gx, gy), Qt.MouseButton.LeftButton)
            )
            assert pet._is_falling is False
            assert pet._resolve_resting_state() == PetState.FLOAT
    finally:
        pet.close()
        pet.deleteLater()
        app.processEvents()
