"""原版骨架动作、打盹资源与设置兼容回归。"""
from unittest.mock import MagicMock

import pytest
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import QApplication

from models.pet_settings import DockSide, PetSettings, PetStyleMode
from models.pet_state import PetState
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
