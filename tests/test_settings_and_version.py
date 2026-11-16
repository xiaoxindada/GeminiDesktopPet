"""配置持久化（user_config.json）、额度窗口布局防遮挡、隐窗执行及打包版本规范测试。"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile
import tomllib
import unittest
from unittest.mock import patch

from PySide6.QtCore import QRect
from PySide6.QtWidgets import QApplication

from models.app_version import AppVersion, get_app_base_dir, get_bundled_resource_dir
from models.pet_settings import (
    DEFAULT_GEMINI_MODEL,
    DockSide,
    PetSettings,
    PetStyleMode,
    normalize_gemini_model,
)
from services.antigravity_quota_service import (
    AntigravityQuotaService,
    QuotaBucketInfo,
    QuotaGroupInfo,
    QuotaSnapshot,
)
from services.character_profile_service import CharacterProfileService
from services.settings_service import SettingsService
from views.quota_window import QuotaWindow

REPO_ROOT = Path(__file__).resolve().parent.parent


class TestSettingsAndProjectConfig(unittest.TestCase):
    def test_settings_json_roundtrip_and_migration(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            svc = SettingsService(tmp)
            original = PetSettings(
                style_mode=PetStyleMode.STANDARD,
                scale=1.35,
                opacity=0.82,
                always_on_top=False,
                show_speech_bubble=True,
                auto_walk=False,
                anti_gravity_mode=True,
                doze_mode=True,
                last_left=640.5,
                last_top=320.25,
                chat_width=420,
                chat_height=230,
                docked_side=DockSide.RIGHT,
                total_pats=17,
                favorability=68,
                gemini_model="gemini-4-argon",
                update_releases_url="https://example.com/releases/%20test%E5%90%8D",
                auto_check_updates=False,
                skipped_update_version="v0.2.9",
                enable_runtime_log=True,
            )
            svc.save(original)
            self.assertTrue(svc.settings_path.is_file())
            raw_json = json.loads(svc.settings_path.read_text(encoding="utf-8"))
            self.assertEqual(raw_json["appearance"]["style_mode"], PetStyleMode.STANDARD.value)
            self.assertEqual(raw_json["window"]["docked_side"], DockSide.RIGHT.value)

            loaded = svc.load()
            self.assertEqual(loaded.style_mode, PetStyleMode.STANDARD)
            self.assertAlmostEqual(loaded.scale, 1.35, places=2)
            self.assertAlmostEqual(loaded.opacity, 0.82, places=2)
            self.assertFalse(loaded.always_on_top)
            self.assertTrue(loaded.show_speech_bubble)
            self.assertFalse(loaded.auto_walk)
            self.assertTrue(loaded.anti_gravity_mode)
            self.assertTrue(loaded.doze_mode)
            self.assertAlmostEqual(loaded.last_left or 0.0, 640.5, places=2)
            self.assertAlmostEqual(loaded.last_top or 0.0, 320.25, places=2)
            self.assertEqual(loaded.chat_width, 420)
            self.assertEqual(loaded.chat_height, 230)
            self.assertEqual(loaded.docked_side, DockSide.RIGHT)
            self.assertEqual(loaded.total_pats, 17)
            self.assertEqual(loaded.favorability, 68)
            self.assertEqual(loaded.gemini_model, "gemini-4-argon")
            self.assertEqual(loaded.update_releases_url, "https://example.com/releases/%20test%E5%90%8D")
            self.assertFalse(loaded.auto_check_updates)
            self.assertEqual(loaded.skipped_update_version, "v0.2.9")
            self.assertTrue(loaded.enable_runtime_log)

            # 支持 UTF-8 BOM 与节内键读取及过期模型名自动升级
            bom_json = "\ufeff" + json.dumps(
                {
                    "appearance": {"style_mode": "chibi", "scale": 5.0},
                    "window": {"last_left": None, "last_top": ""},
                    "gemini": {"model": "gemini-2.0-flash"},
                }
            )
            svc.settings_path.write_text(bom_json, encoding="utf-8")
            reloaded = svc.load()
            self.assertEqual(reloaded.style_mode, PetStyleMode.CHIBI)
            self.assertAlmostEqual(reloaded.scale, 2.2, places=2)
            self.assertIsNone(reloaded.last_left)
            self.assertIsNone(reloaded.last_top)
            self.assertEqual(reloaded.gemini_model, DEFAULT_GEMINI_MODEL)
            self.assertEqual(normalize_gemini_model("gemini-2.5-pro"), "gemini-2.5-pro")
            self.assertEqual(normalize_gemini_model("gemini-4-hydrogen"), DEFAULT_GEMINI_MODEL)
            from models import normalize_thinking_level

            self.assertEqual(normalize_thinking_level("Extra"), "xhigh")

            # 旧 API Key 默认占位符 gemini-4-hydrogen 在加载旧版 user_config.json 时迁移到默认模型，保留思考强度
            svc.settings_path.write_text(
                json.dumps(
                    {
                        "gemini": {
                            "model": "gemini-4-hydrogen",
                            "thinking_level": "Extra",
                        }
                    }
                ),
                encoding="utf-8",
            )
            migrated = svc.load()
            self.assertEqual(migrated.gemini_model, DEFAULT_GEMINI_MODEL)
            self.assertEqual(migrated.thinking_level, "xhigh")

        # 验证默认配置目录为 %USERPROFILE%\.GeminiDesktopPet\user_config.json
        with tempfile.TemporaryDirectory() as tmp_home:
            with patch.dict(os.environ, {"USERPROFILE": tmp_home}):
                default_svc = SettingsService()
                expected_dir = Path(tmp_home) / ".GeminiDesktopPet"
                self.assertEqual(default_svc.settings_folder, expected_dir)
                self.assertEqual(default_svc.settings_path, expected_dir / "user_config.json")
                default_svc.save(PetSettings(style_mode=PetStyleMode.STANDARD, scale=1.25))
                self.assertTrue(default_svc.settings_path.is_file())
                loaded_default = default_svc.load()
                self.assertEqual(loaded_default.style_mode, PetStyleMode.STANDARD)
                self.assertAlmostEqual(loaded_default.scale, 1.25, places=2)

    def test_api_key_in_user_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            svc = SettingsService(tmp)
            sample_key = "test-secret-token-value-12345"
            svc.save(PetSettings(gemini_api_key=sample_key))
            raw_json = json.loads(svc.settings_path.read_text(encoding="utf-8"))
            self.assertEqual(raw_json["gemini"]["api_key"], sample_key)
            self.assertEqual(svc.load().gemini_api_key, sample_key)

            # Support user-friendly ai_key alias directly in user_config.json
            raw_json["gemini"].pop("api_key", None)
            raw_json["gemini"]["ai_key"] = "alias-key-67890"
            svc.settings_path.write_text(json.dumps(raw_json), encoding="utf-8")
            self.assertEqual(svc.load().gemini_api_key, "alias-key-67890")

            svc.save(PetSettings(gemini_api_key=""))
            self.assertEqual(svc.load().gemini_api_key, "")

    def test_character_profile_roundtrip_and_version(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            prof_svc = CharacterProfileService(tmp)
            profile = prof_svc.load()
            profile.alias = "测试星酱"
            prof_svc.save(profile)
            self.assertEqual(prof_svc.load().alias, "测试星酱")
            reset = prof_svc.reset()
            self.assertTrue(bool(reset.name))

        from models.app_version import APP_VERSION

        self.assertFalse((REPO_ROOT / "VERSION").exists())
        self.assertEqual(AppVersion.current(), AppVersion.normalize(APP_VERSION))
        self.assertTrue(Path(get_app_base_dir()).is_dir())
        self.assertTrue((Path(get_bundled_resource_dir()) / "Assets").is_dir())

    def test_pyproject_and_bat_script(self) -> None:
        pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        deps: list[str] = pyproject["project"]["dependencies"]
        self.assertTrue(any(d.startswith("PySide6-Essentials") for d in deps))
        self.assertFalse(any(d.split(">=")[0].split("==")[0].strip() == "PySide6" for d in deps))
        self.assertEqual(
            pyproject["tool"]["setuptools"]["dynamic"]["version"]["attr"],
            "models.app_version.APP_VERSION",
        )

        for bat_name in ("BuildRelease.bat",):
            bat_bytes = (REPO_ROOT / bat_name).read_bytes()
            self.assertFalse(any(b >= 128 for b in bat_bytes), f"{bat_name} must be pure ASCII")
            self.assertNotIn(b"\n", bat_bytes.replace(b"\r\n", b""), f"{bat_name} must use CRLF")
        self.assertFalse((REPO_ROOT / "GeminiDesktopPet.spec").exists())
        self.assertTrue((REPO_ROOT / "clean_config.py").is_file())
        self.assertTrue((REPO_ROOT / "tools" / "release_package.py").is_file())
        self.assertFalse((REPO_ROOT / "CleanConfig.bat").exists())
        self.assertFalse((REPO_ROOT / "tools" / "build_release.py").exists())
        self.assertFalse((REPO_ROOT / "tools" / "security_check.py").exists())
        bat_text = (REPO_ROOT / "BuildRelease.bat").read_text(encoding="ascii")
        self.assertNotIn('"%PYTHON_EXE%" -c', bat_text)
        self.assertIn("-m tools.release_package generate-spec", bat_text)
        gitignore_lines = {
            line.strip()
            for line in (REPO_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.strip().startswith("#")
        }
        self.assertIn("build/", gitignore_lines)
        self.assertIn("*.spec", gitignore_lines)
        self.assertNotIn("!GeminiDesktopPet.spec", gitignore_lines)

    def test_release_package_tool_helpers(self) -> None:
        import hashlib
        import zipfile
        from tools.release_package import (
            EXPECTED_STAGE_ENTRIES,
            REQUIRED_BUILD_FILES,
            create_release_archive,
            finalize_stage_dir,
            resolve_release_version,
            write_pyinstaller_spec,
        )

        with tempfile.TemporaryDirectory() as tmp_root:
            root = Path(tmp_root)
            repo = root / "repo"
            stage = root / "stage" / "GeminiDesktopPet"
            repo.mkdir(parents=True)
            stage.mkdir(parents=True)

            for name in REQUIRED_BUILD_FILES:
                (repo / name).write_text(f"sample {name}\n", encoding="utf-8")

            ver_file = root / "build" / "version.txt"
            version = resolve_release_version(repo, ver_file)
            self.assertEqual(ver_file.read_text(encoding="ascii"), version)

            spec_file = root / "build" / "pyinstaller" / "GeminiDesktopPet.spec"
            generated_spec = write_pyinstaller_spec(repo, spec_file)
            self.assertEqual(generated_spec, spec_file)
            spec_text = spec_file.read_text(encoding="utf-8")
            self.assertIn(repr(str(repo.resolve())), spec_text)
            self.assertIn('name="GeminiDesktopPet"', spec_text)
            self.assertIn('name="CleanConfig"', spec_text)

            # 模拟 PyInstaller 产出的可执行文件与 _internal 目录
            (stage / "GeminiDesktopPet.exe").write_bytes(b"exe")
            (stage / "CleanConfig.exe").write_bytes(b"clean")
            (stage / "_internal").mkdir()
            (stage / "_internal" / "runtime.txt").write_text("ok", encoding="utf-8")

            entries = finalize_stage_dir(repo, stage)
            self.assertEqual(entries, sorted(EXPECTED_STAGE_ENTRIES))

            # 多出未预期文件时应拒绝通过布局校验
            (stage / "unexpected.tmp").write_text("bad", encoding="utf-8")
            with self.assertRaises(RuntimeError):
                finalize_stage_dir(repo, stage)
            (stage / "unexpected.tmp").unlink()

            summary_path = root / "out" / "git-commit-summary.md"
            zip_path = root / "out" / f"GeminiDesktopPet-v{version}-win-x64.zip"
            out_zip, size_bytes, digest = create_release_archive(
                repo, stage, version, summary_path, zip_path
            )
            self.assertEqual(out_zip, zip_path)
            self.assertTrue(summary_path.is_file())
            self.assertEqual(size_bytes, zip_path.stat().st_size)
            self.assertEqual(digest, hashlib.sha256(zip_path.read_bytes()).hexdigest())
            with zipfile.ZipFile(zip_path) as zf:
                names = set(zf.namelist())
            self.assertIn("GeminiDesktopPet.exe", names)
            self.assertIn("CleanConfig.exe", names)
            self.assertIn("_internal/runtime.txt", names)
            self.assertIn("README.md", names)

    def test_all_runtime_files_unified_under_userprofile(self) -> None:
        from models.app_version import get_user_config_dir
        from services import app_follow_service
        from services.app_logger import AppLogger
        from services.config_cleanup_service import ConfigCleanupService
        from services.sound_service import SoundService
        from services.update_service import UpdateService

        with tempfile.TemporaryDirectory() as tmp_home:
            expected_dir = Path(tmp_home) / ".GeminiDesktopPet"
            with patch.dict(os.environ, {"USERPROFILE": tmp_home}):
                self.assertEqual(get_user_config_dir(), expected_dir)
                self.assertEqual(SettingsService().settings_path, expected_dir / "user_config.json")
                self.assertEqual(
                    Path(CharacterProfileService().character_file_path),
                    expected_dir / "character.json",
                )
                self.assertEqual(
                    Path(app_follow_service._get_pid_marker_path("main.pid")),
                    expected_dir / "main.pid",
                )

                # 验证运行日志目录与日志文件统一在 ~/.GeminiDesktopPet/Logs
                AppLogger.initialize(True)
                try:
                    self.assertEqual(Path(AppLogger.log_directory()), expected_dir / "Logs")
                    AppLogger.info("Test", "unified userprofile log check")
                    log_files = list((expected_dir / "Logs").glob("*.log"))
                    self.assertTrue(log_files)
                finally:
                    AppLogger.initialize(False)

                # 验证合成音效缓存统一在 ~/.GeminiDesktopPet/sounds
                sound_svc = SoundService()
                sound_svc.dispose()
                self.assertTrue((expected_dir / "sounds" / "chime.wav").is_file())

                # 验证更新缓存与旧暂存清理统一在 ~/.GeminiDesktopPet/updates
                updates_root = Path(UpdateService.get_updates_root_dir())
                self.assertEqual(updates_root, expected_dir / "updates")
                stale_dir = updates_root / "v0.0.1" / "staging"
                stale_dir.mkdir(parents=True, exist_ok=True)
                UpdateService.cleanup_old_staging_folders()
                self.assertFalse((updates_root / "v0.0.1").exists())

                resolved = ConfigCleanupService.resolve_default_targets()
                self.assertEqual(resolved.user_config_dir, expected_dir)

    def test_python_config_cleanup_service_and_cli(self) -> None:
        import clean_config
        from services.config_cleanup_service import CleanupTargets, ConfigCleanupService

        with tempfile.TemporaryDirectory() as tmp_root:
            root = Path(tmp_root)
            user_cfg = root / "home" / ".GeminiDesktopPet"
            app_dir = root / "app"

            for folder in (
                user_cfg,
                user_cfg / "Logs",
                user_cfg / "sounds",
                user_cfg / "updates" / "v0.2.3",
                app_dir,
            ):
                folder.mkdir(parents=True, exist_ok=True)

            (user_cfg / "user_config.json").write_text("{}", encoding="utf-8")
            (user_cfg / "character.json").write_text("{}", encoding="utf-8")
            (user_cfg / "main.pid").write_text("1234|0", encoding="utf-8")
            (user_cfg / "Logs" / "2026-11-15.log").write_text("log", encoding="utf-8")
            (user_cfg / "sounds" / "chime.wav").write_bytes(b"RIFF")
            (user_cfg / "updates" / "v0.2.3" / "pkg.zip").write_bytes(b"zip")
            # 模拟主程序二进制与文档，清理绝不应误删程序本体文件
            (app_dir / "GeminiDesktopPet.exe").write_bytes(b"exe")
            (app_dir / "README.md").write_text("readme", encoding="utf-8")

            targets = CleanupTargets(
                user_config_dir=user_cfg,
                startup_shortcuts=(
                    root / "startup" / "GeminiDesktopPet.lnk",
                    root / "startup" / "GeminiDesktopPet_FollowWatcher.lnk",
                ),
            )
            for shortcut in targets.startup_shortcuts:
                shortcut.parent.mkdir(parents=True, exist_ok=True)
                shortcut.write_bytes(b"lnk")

            report = ConfigCleanupService.run(
                targets,
                stop_instances=False,
            )

            self.assertFalse(report.failed_paths)
            self.assertFalse(user_cfg.exists())
            for shortcut in targets.startup_shortcuts:
                self.assertFalse(shortcut.exists())
            self.assertTrue((app_dir / "GeminiDesktopPet.exe").is_file())
            self.assertTrue((app_dir / "README.md").is_file())

        self.assertEqual(clean_config.main(["--version"]), 0)

    def test_startup_uses_shortcut_without_registry_writes(self) -> None:
        from services.app_follow_service import AppFollowService

        with tempfile.TemporaryDirectory() as tmp_startup:
            startup_dir = Path(tmp_startup)
            with (
                patch("services.win32_ipc.get_windows_startup_dir", return_value=startup_dir),
            ):
                self.assertFalse(SettingsService.is_startup_enabled())
                self.assertTrue(SettingsService.set_startup_enabled(True))
                main_lnk = startup_dir / "GeminiDesktopPet.lnk"
                self.assertTrue(main_lnk.is_file())
                self.assertGreater(main_lnk.stat().st_size, 100)
                self.assertTrue(SettingsService.is_startup_enabled())

                self.assertTrue(AppFollowService.set_follow_watcher_run(True))
                follow_lnk = startup_dir / "GeminiDesktopPet_FollowWatcher.lnk"
                self.assertTrue(follow_lnk.is_file())
                self.assertTrue(AppFollowService.is_follow_watcher_run_enabled())

                self.assertTrue(SettingsService.set_startup_enabled(False))
                self.assertTrue(AppFollowService.set_follow_watcher_run(False))
                self.assertFalse(main_lnk.exists())
                self.assertFalse(follow_lnk.exists())
                self.assertFalse(SettingsService.is_startup_enabled())
                self.assertFalse(AppFollowService.is_follow_watcher_run_enabled())

    def test_quota_window_full_display_and_non_overlapping_placement(self) -> None:
        app = QApplication.instance() or QApplication([])
        _ = app

        class _FakePet:
            def isVisible(self) -> bool:
                return True

            def frameGeometry(self) -> QRect:
                return QRect(1540, 560, 360, 440)

            def screen(self):
                return None

            def get_visual_sprite_bounds(self) -> QRect:
                return QRect(1670, 740, 180, 260)

            def get_visual_ui_bounds(self) -> QRect:
                return QRect(1550, 600, 320, 400)

        win = QuotaWindow(AntigravityQuotaService(), anchor_pet=_FakePet())
        try:
            win.show_loading_card()
            app.processEvents()
            loading_h = win.height()

            snap = QuotaSnapshot(
                success=True,
                error_message=None,
                source_detail="Antigravity app · pid 1234 · http://127.0.0.1:56789",
                account_email=None,
                account_plan="Google AI Pro",
                overview_description=None,
                fetched_at=datetime.now(timezone.utc),
                groups=[
                    QuotaGroupInfo(
                        "Gemini Models",
                        "Gemini pool",
                        [
                            QuotaBucketInfo("w", "Weekly Limit Remaining", "weekly", 0.48, datetime.now(timezone.utc)),
                            QuotaBucketInfo("5h", "Five Hour Limit Remaining", "5h", 0.634, datetime.now(timezone.utc)),
                        ],
                    ),
                    QuotaGroupInfo(
                        "Claude and GPT models",
                        "Claude/GPT pool",
                        [
                            QuotaBucketInfo("w2", "Weekly Limit Remaining", "weekly", 1.0, datetime.now(timezone.utc)),
                            QuotaBucketInfo("5h2", "Five Hour Limit Remaining", "5h", 0.92, datetime.now(timezone.utc)),
                        ],
                    ),
                    QuotaGroupInfo(
                        "Gemini 4 Argon",
                        "Argon pool",
                        [
                            QuotaBucketInfo("arg", "Gemini 4 Argon Weekly Limit", "weekly", 0.75, datetime.now(timezone.utc)),
                        ],
                    ),
                ],
            )
            win.apply_snapshot(snap)
            app.processEvents()

            self.assertGreater(win.height(), loading_h + 250)
            self.assertGreaterEqual(win._quota_widget.height(), win._scroll.height() + 40)
            shadow = 14
            card_rect = QRect(win.x() + shadow, win.y() + shadow, win.width() - shadow * 2, win.height() - shadow * 2)
            self.assertFalse(card_rect.intersects(_FakePet().get_visual_ui_bounds()))
        finally:
            win.close()
            win.deleteLater()
            app.processEvents()


if __name__ == "__main__":
    unittest.main()
