"""GitHub Releases 自动检查更新、安装包下载与无黑框原地替换引擎。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import os
import re
import shutil
import subprocess
import sys
import time
from typing import Callable, Optional
from urllib.parse import urlparse
import zipfile

import psutil
import requests

from models.app_version import AppVersion, get_app_base_dir, get_user_config_dir
from services.app_follow_service import AppFollowService
from services.app_logger import AppLogger
from services.win32_ipc import STOP_WATCHER_EVENT_NAME, subprocess_hidden_kwargs

DEFAULT_RELEASES_URL = "https://github.com/xiaoxindada/GeminiDesktopPet/releases"


@dataclass(frozen=True)
class ReleaseAssetInfo:
    name: str
    download_url: str
    size_bytes: int = 0


@dataclass(frozen=True)
class UpdateCheckResult:
    succeeded: bool
    update_available: bool
    current_version: str
    latest_tag: str
    latest_version: str
    release_title: str
    changelog_markdown: str
    release_html_url: str
    published_at: Optional[datetime]
    package_asset: Optional[ReleaseAssetInfo]
    status_message: str
    no_published_releases: bool = False


class UpdateService:
    """追踪上游 GitHub Releases 最新版本，支持下载 ZIP 并安全执行原地替换重启。"""

    def __init__(self) -> None:
        self._headers = {
            "User-Agent": f"GeminiDesktopPet/{AppVersion.current()}",
        }

    @staticmethod
    def _try_parse_github_repo(raw: Optional[str]) -> Optional[tuple[str, str]]:
        if not raw or not raw.strip():
            return "xiaoxindada", "GeminiDesktopPet"
        try:
            parsed = urlparse(raw.strip())
            if parsed.scheme not in ("http", "https") or parsed.netloc.lower() != "github.com":
                return None
            segs = [s for s in parsed.path.split("/") if s]
            if len(segs) < 2:
                return None
            owner = segs[0]
            repo = segs[1][:-4] if segs[1].lower().endswith(".git") else segs[1]
            if owner and repo:
                return owner, repo
        except Exception:
            pass
        return None

    @classmethod
    def normalize_releases_url(cls, raw: Optional[str]) -> str:
        parsed = cls._try_parse_github_repo(raw)
        if parsed:
            return f"https://github.com/{parsed[0]}/{parsed[1]}/releases"
        return DEFAULT_RELEASES_URL

    @classmethod
    def get_latest_tracking_url(cls, raw: Optional[str]) -> str:
        return cls.normalize_releases_url(raw).rstrip("/") + "/latest"

    def check_for_update(self, custom_releases_url: Optional[str] = None) -> UpdateCheckResult:
        current = AppVersion.current()
        parsed = self._try_parse_github_repo(custom_releases_url)
        if not parsed:
            return UpdateCheckResult(
                False,
                False,
                current,
                "",
                "",
                "",
                "",
                DEFAULT_RELEASES_URL,
                None,
                None,
                "上游追踪地址格式无效，请使用 GitHub Releases 地址。",
            )

        owner, repo = parsed
        canonical = f"https://github.com/{owner}/{repo}/releases"
        latest_page = f"{canonical}/latest"
        api_latest = f"https://api.github.com/repos/{owner}/{repo}/releases/latest"

        try:
            headers = {**self._headers, "Accept": "application/vnd.github+json"}
            resp = requests.get(api_latest, headers=headers, timeout=25)
            if resp.status_code == 404:
                return self._check_releases_list_fallback(owner, repo, canonical, current)
            if resp.ok:
                return self._build_result_from_release(resp.json(), canonical, current)
            return self._check_html_redirect_fallback(owner, repo, latest_page, current)
        except Exception as exc:
            try:
                return self._check_html_redirect_fallback(owner, repo, latest_page, current)
            except Exception as inner:
                AppLogger.warn("Update", f"检查更新失败 ({latest_page}): {inner or exc}")
                return UpdateCheckResult(
                    False,
                    False,
                    current,
                    "",
                    "",
                    "",
                    "",
                    latest_page,
                    None,
                    None,
                    f"检查更新失败：{inner}",
                )

    def _check_releases_list_fallback(
        self, owner: str, repo: str, canonical: str, current: str
    ) -> UpdateCheckResult:
        api_list = f"https://api.github.com/repos/{owner}/{repo}/releases?per_page=1"
        headers = {**self._headers, "Accept": "application/vnd.github+json"}
        resp = requests.get(api_list, headers=headers, timeout=25)
        if resp.status_code == 404:
            return UpdateCheckResult(
                True,
                False,
                current,
                "",
                "",
                "",
                "",
                canonical,
                None,
                None,
                f"上游 Releases 暂无已发布版本（当前版本 v{current}）。",
                no_published_releases=True,
            )
        if not resp.ok:
            return self._check_html_redirect_fallback(owner, repo, canonical + "/latest", current)
        arr = resp.json()
        if not isinstance(arr, list) or not arr:
            return UpdateCheckResult(
                True,
                False,
                current,
                "",
                "",
                "",
                "",
                canonical,
                None,
                None,
                f"上游 Releases 暂无已发布版本（当前版本 v{current}）。",
                no_published_releases=True,
            )
        return self._build_result_from_release(arr[0], canonical, current)

    def _check_html_redirect_fallback(
        self, owner: str, repo: str, latest_page: str, current: str
    ) -> UpdateCheckResult:
        resp = requests.get(latest_page, headers=self._headers, allow_redirects=True, timeout=25)
        final_url = resp.url or latest_page
        m = re.search(r"/releases/tag/([^/?#]+)", final_url, re.IGNORECASE)
        if not m:
            if resp.ok:
                return UpdateCheckResult(
                    True,
                    False,
                    current,
                    "",
                    "",
                    "",
                    "",
                    f"https://github.com/{owner}/{repo}/releases",
                    None,
                    None,
                    f"上游 Releases 暂无已发布版本（当前版本 v{current}）。",
                    no_published_releases=True,
                )
            return UpdateCheckResult(
                False,
                False,
                current,
                "",
                "",
                "",
                "",
                final_url,
                None,
                None,
                f"检查更新失败：HTTP {resp.status_code}",
            )

        tag = m.group(1)
        latest_ver = AppVersion.normalize(tag)
        has_newer = bool(tag) and AppVersion.is_newer_version(latest_ver, current)
        preferred = self._extract_zip_asset_from_html(resp.text)
        status = (
            f"发现新版本 {AppVersion.format_tag(latest_ver)}（当前 v{current}）"
            if has_newer
            else f"当前已是最新版本 v{current}（上游最新：{tag}）"
        )
        return UpdateCheckResult(
            True,
            has_newer,
            current,
            tag,
            latest_ver,
            tag,
            "（通过网页重定向检测到版本，详情可点击打开 Release 页面查看。）",
            final_url,
            None,
            preferred,
            status,
        )

    @classmethod
    def _extract_zip_asset_from_html(cls, html: str) -> Optional[ReleaseAssetInfo]:
        matches = re.findall(r'href="(/(?:[^"]+)/releases/download/[^"]+\.zip)"', html, re.IGNORECASE)
        assets = [
            ReleaseAssetInfo(os.path.basename(rel), "https://github.com" + rel, 0) for rel in matches
        ]
        return cls._pick_preferred_asset(assets)

    @classmethod
    def _build_result_from_release(
        cls, root: dict, canonical: str, current: str
    ) -> UpdateCheckResult:
        tag = str(root.get("tag_name") or "").strip()
        title = str(root.get("name") or tag).strip()
        body = str(root.get("body") or "").strip()
        html_url = str(root.get("html_url") or f"{canonical}/latest").strip()

        published_at: Optional[datetime] = None
        pub_raw = root.get("published_at")
        if isinstance(pub_raw, str) and pub_raw:
            try:
                published_at = datetime.fromisoformat(pub_raw.replace("Z", "+00:00")).astimezone()
            except Exception:
                pass

        assets: list[ReleaseAssetInfo] = []
        for item in root.get("assets") or []:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "")
            dl_url = str(item.get("browser_download_url") or "")
            size = int(item.get("size") or 0)
            if name and dl_url:
                assets.append(ReleaseAssetInfo(name, dl_url, size))

        preferred = cls._pick_preferred_asset(assets)
        latest_ver = AppVersion.normalize(tag)
        has_newer = bool(tag) and AppVersion.is_newer_version(latest_ver, current)

        if not tag:
            status = "未读取到有效的上游版本标签。"
        elif has_newer:
            status = f"发现新版本 {AppVersion.format_tag(latest_ver)}（当前 v{current}）"
        else:
            status = f"当前已是最新版本 v{current}（上游最新：{tag}）"

        changelog = body or "（该版本未填写文字版更新说明，可前往 GitHub Release 页面查看详情。）"
        return UpdateCheckResult(
            True,
            has_newer,
            current,
            tag,
            latest_ver,
            title or tag,
            changelog,
            html_url,
            published_at,
            preferred,
            status,
        )

    @staticmethod
    def _pick_preferred_asset(assets: list[ReleaseAssetInfo]) -> Optional[ReleaseAssetInfo]:
        zip_assets = [a for a in assets if a.name.lower().endswith(".zip")]
        if not zip_assets:
            return None
        full = next(
            (
                a
                for a in zip_assets
                if "win-x64.zip" in a.name.lower() and "-fd.zip" not in a.name.lower()
            ),
            None,
        )
        if full is not None:
            return full
        return next((a for a in zip_assets if "win" in a.name.lower()), zip_assets[0])

    @staticmethod
    def can_apply_in_place_update() -> tuple[bool, str]:
        if not getattr(sys, "frozen", False):
            return (
                False,
                "当前正从 Python 源码目录运行，请使用打包发布目录进行原地自动更新，或点击浏览器打开 Releases 手动下载。",
            )
        base_dir = get_app_base_dir()
        try:
            probe = os.path.join(base_dir, f".update_write_test_{os.getpid()}.tmp")
            with open(probe, "w", encoding="utf-8") as fp:
                fp.write("ok")
            os.remove(probe)
            return True, ""
        except Exception as exc:
            return False, f"应用目录没有写入权限，无法自动覆盖更新：{exc}"

    @staticmethod
    def get_updates_root_dir() -> str:
        """更新包下载与解压暂存根目录（统一放在 ~/.GeminiDesktopPet/updates）。"""
        return os.path.join(str(get_user_config_dir()), "updates")

    def download_asset(
        self,
        asset: ReleaseAssetInfo,
        version_tag: str,
        progress_cb: Optional[Callable[[int, Optional[int]], None]] = None,
        cancel_check: Optional[Callable[[], bool]] = None,
    ) -> str:
        safe_tag = self._sanitize_path_segment(version_tag.strip() or "latest")
        work_dir = os.path.join(self.get_updates_root_dir(), safe_tag)
        os.makedirs(work_dir, exist_ok=True)

        zip_path = os.path.join(work_dir, self._sanitize_path_segment(asset.name))
        if os.path.exists(zip_path):
            os.remove(zip_path)

        with requests.get(
            asset.download_url, headers=self._headers, stream=True, timeout=600
        ) as resp:
            resp.raise_for_status()
            cl = resp.headers.get("Content-Length")
            total = int(cl) if cl and cl.isdigit() else (asset.size_bytes if asset.size_bytes > 0 else None)
            downloaded = 0
            with open(zip_path, "wb") as fp:
                for chunk in resp.iter_content(chunk_size=81920):
                    if cancel_check and cancel_check():
                        raise RuntimeError("用户取消了下载。")
                    if chunk:
                        fp.write(chunk)
                        downloaded += len(chunk)
                        if progress_cb:
                            progress_cb(downloaded, total)
        return zip_path

    @staticmethod
    def extract_and_launch_updater(
        zip_path: str,
        trigger_graceful_exit: Callable[[], None],
        before_exit_cleanup: Optional[Callable[[], None]] = None,
    ) -> None:
        update_root = os.path.dirname(zip_path) or UpdateService.get_updates_root_dir()
        staging_dir = os.path.join(update_root, "staging")
        if os.path.exists(staging_dir):
            shutil.rmtree(staging_dir, ignore_errors=True)
        os.makedirs(staging_dir, exist_ok=True)

        with zipfile.ZipFile(zip_path, "r") as zf:
            zf.extractall(staging_dir)

        payload_dir = staging_dir
        if not os.path.isfile(os.path.join(payload_dir, "GeminiDesktopPet.exe")):
            subdirs = [
                os.path.join(staging_dir, d)
                for d in os.listdir(staging_dir)
                if os.path.isdir(os.path.join(staging_dir, d))
            ]
            if len(subdirs) == 1 and os.path.isfile(os.path.join(subdirs[0], "GeminiDesktopPet.exe")):
                payload_dir = subdirs[0]

        staging_exe = os.path.join(payload_dir, "GeminiDesktopPet.exe")
        if not os.path.isfile(staging_exe):
            raise RuntimeError("更新包中未找到 GeminiDesktopPet.exe，已取消安装。")

        target_app_dir = get_app_base_dir().rstrip("\\/")
        clean_payload_dir = payload_dir.rstrip("\\/")
        if os.path.normcase(clean_payload_dir) == os.path.normcase(target_app_dir):
            raise RuntimeError("更新暂存目录与当前应用目录相同，已取消更新。")

        if before_exit_cleanup:
            before_exit_cleanup()

        subprocess.Popen(
            [staging_exe, "--apply-update", str(os.getpid()), target_app_dir],
            cwd=clean_payload_dir,
            **subprocess_hidden_kwargs(),
        )
        trigger_graceful_exit()

    @classmethod
    def apply_staged_update_and_relaunch(cls, parent_pid: int, target_app_dir: str) -> bool:
        try:
            if not target_app_dir or not os.path.isdir(target_app_dir):
                return False
            source_dir = get_app_base_dir().rstrip("\\/")
            clean_target = target_app_dir.rstrip("\\/")
            if os.path.normcase(source_dir) == os.path.normcase(clean_target):
                return False

            AppLogger.info("Update", f"开始原地更新替换：parentPid={parent_pid}, targetDir={clean_target}")
            stop_all = AppFollowService.acquire_stop_all_signal()
            try:
                AppFollowService.try_signal_named_event(STOP_WATCHER_EVENT_NAME)
                cls._wait_pid_exit(parent_pid, 15.0)

                norm_target = os.path.normcase(os.path.abspath(clean_target)) + os.sep
                self_pid = os.getpid()
                for proc in psutil.process_iter(["pid", "name"]):
                    try:
                        if proc.pid == self_pid:
                            continue
                        if (proc.info.get("name") or "").lower() != "geminidesktoppet.exe":
                            continue
                        try:
                            exe_norm = os.path.normcase(os.path.abspath(proc.exe()))
                        except Exception:
                            exe_norm = ""
                        if proc.pid != parent_pid and not exe_norm.startswith(norm_target):
                            continue
                        if not AppFollowService.wait_for_process_exit(proc, 1200):
                            AppFollowService.kill_process_tree_safe(proc)
                            AppFollowService.wait_for_process_exit(proc, 2000)
                    except (psutil.NoSuchProcess, psutil.AccessDenied):
                        continue
            finally:
                stop_all.close()

            AppFollowService.clear_all_pid_markers()
            time.sleep(0.25)

            cls._copy_dir_with_retry(source_dir, clean_target)
            AppLogger.info("Update", f"原地文件覆盖完成，准备重新启动：{clean_target}")

            target_exe = os.path.join(clean_target, "GeminiDesktopPet.exe")
            if os.path.isfile(target_exe):
                subprocess.Popen([target_exe], cwd=clean_target)
            return True
        except Exception as exc:
            AppLogger.error("Update", "应用原地更新失败", exc)
            return False

    @staticmethod
    def cleanup_old_staging_folders() -> None:
        try:
            updates_root = UpdateService.get_updates_root_dir()
            if not os.path.isdir(updates_root):
                return
            cur_base = os.path.normcase(os.path.abspath(get_app_base_dir())) + os.sep
            upd_prefix = os.path.normcase(os.path.abspath(updates_root)) + os.sep
            if cur_base.startswith(upd_prefix):
                return
            for entry in os.listdir(updates_root):
                full = os.path.join(updates_root, entry)
                if os.path.isdir(full):
                    shutil.rmtree(full, ignore_errors=True)
        except Exception:
            pass

    @staticmethod
    def _wait_pid_exit(pid: int, timeout_s: float) -> None:
        if pid <= 0:
            return
        try:
            proc = psutil.Process(pid)
            try:
                proc.wait(timeout=timeout_s)
            except psutil.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=3.0)
        except Exception:
            pass

    @classmethod
    def _copy_dir_with_retry(cls, source_dir: str, target_dir: str) -> None:
        os.makedirs(target_dir, exist_ok=True)
        for root, dirs, files in os.walk(source_dir):
            rel = os.path.relpath(root, source_dir)
            dst_root = target_dir if rel == "." else os.path.join(target_dir, rel)
            os.makedirs(dst_root, exist_ok=True)
            for fn in files:
                src_file = os.path.join(root, fn)
                dst_file = os.path.join(dst_root, fn)
                cls._copy_file_with_retry(src_file, dst_file)

    @staticmethod
    def _copy_file_with_retry(src_file: str, dst_file: str) -> None:
        for attempt in range(1, 11):
            try:
                shutil.copy2(src_file, dst_file)
                return
            except (OSError, PermissionError):
                if attempt == 10:
                    raise
                time.sleep(0.25)

    @staticmethod
    def _sanitize_path_segment(val: str) -> str:
        return re.sub(r'[<>:"/\\|?*]+', "_", val).strip() or "latest"
