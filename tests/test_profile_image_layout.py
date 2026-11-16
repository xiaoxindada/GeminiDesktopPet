"""设定图在布局收缩、切页和刷新后必须保持完整。"""
import pytest
from PySide6.QtCore import QRectF
from PySide6.QtGui import QColor, QPainter, QPixmap
from PySide6.QtWidgets import QApplication

from models.pet_settings import PetSettings
from services.sprite_manager import SpriteManager
from views.profile_window import ProfileWindow, _FitImageLabel


@pytest.fixture(scope="module")
def app():
    instance = QApplication.instance() or QApplication([])
    yield instance


def test_image_edges_survive_resize_and_source_replacement(app):
    label = _FitImageLabel()
    try:
        for width, height in ((240, 600), (900, 300)):
            source = QPixmap(width, height)
            source.fill(QColor("white"))
            painter = QPainter(source)
            painter.fillRect(0, 0, width, height // 5, QColor("red"))
            painter.fillRect(0, height * 4 // 5, width, height // 5, QColor("blue"))
            painter.end()
            label.setPixmap(source)
            for size in ((260, 400), (180, 130), (360, 500), (260, 400)):
                label.resize(*size)
                label.show()
                app.processEvents()
                target = label._image_rect()
                assert QRectF(label.contentsRect()).contains(target)
                assert target.width() / target.height() == pytest.approx(width / height)
                assert label.pixmap().size() == source.size()
                # 检查原图顶部和底部标记都实际绘制出来，而非仅验证尺寸。
                rendered = label.grab().toImage()
                dpr = rendered.devicePixelRatio()
                x = int(target.center().x() * dpr)
                top = int((target.top() + target.height() * .1) * dpr)
                bottom = int((target.top() + target.height() * .9) * dpr)
                assert rendered.pixelColor(x, top) == QColor("red")
                assert rendered.pixelColor(x, bottom) == QColor("blue")
    finally:
        label.close()


def test_profile_and_turnaround_fit_after_tab_switch_and_refresh(app):
    sprites = SpriteManager()
    sprites.preload_all()
    window = ProfileWindow(sprites, PetSettings(), lambda state, style: None)
    try:
        window.show()
        for size in ((880, 660), (720, 540), (1200, 900), (880, 660)):
            window.resize(*size)
            for tab, label in ((0, window._standee_lbl), (1, window._turnaround_lbl),
                               (0, window._standee_lbl)):
                window._tabs.setCurrentIndex(tab)
                window.refresh_profile()
                app.processEvents()
                target = label._image_rect()
                assert not target.isEmpty()
                assert QRectF(label.contentsRect()).contains(target)
                source = label.pixmap()
                assert target.width() / target.height() == pytest.approx(source.width() / source.height())
                assert target.left() >= 12 and target.top() >= 12
    finally:
        window.close()
