"""启动更新检查的配置传递回归测试。"""

from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from app import PetApplicationCoordinator
from models.pet_settings import PetSettings


class TestStartupUpdate(unittest.TestCase):
    def test_startup_check_uses_configured_releases_url(self) -> None:
        # 使用真实配置模型，避免模拟对象掩盖不存在的配置字段。
        for url in (PetSettings().update_releases_url, "https://example.com/releases"):
            with self.subTest(url=url):
                coordinator = SimpleNamespace(
                    _is_exiting=False,
                    _settings=PetSettings(update_releases_url=url),
                    _update_service=Mock(),
                    _on_silent_update_checked=Mock(),
                )
                with patch("app._SilentUpdateWorker") as worker:
                    PetApplicationCoordinator._start_silent_update_check(coordinator)

                worker.assert_called_once_with(
                    coordinator._update_service, url, parent=coordinator
                )
                worker.return_value.checked.connect.assert_called_once_with(
                    coordinator._on_silent_update_checked
                )
                worker.return_value.start.assert_called_once_with()

    def test_exit_does_not_start_update_check(self) -> None:
        with patch("app._SilentUpdateWorker") as worker:
            PetApplicationCoordinator._start_silent_update_check(
                SimpleNamespace(_is_exiting=True)
            )
        worker.assert_not_called()


if __name__ == "__main__":
    unittest.main()
