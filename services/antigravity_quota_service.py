"""基于官方 Antigravity CLI (agy) 的模型配额与 AI Credits 查询服务。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import subprocess
import time
from typing import Optional

from services.app_logger import AppLogger
from services.win32_ipc import subprocess_hidden_kwargs


@dataclass(frozen=True)
class QuotaBucketInfo:
    id: str
    name: str
    window: str
    remaining_fraction: float
    reset_time: Optional[datetime] = None
    description: Optional[str] = None


@dataclass(frozen=True)
class QuotaGroupInfo:
    name: str
    description: Optional[str] = None
    buckets: list[QuotaBucketInfo] = field(default_factory=list)


@dataclass(frozen=True)
class QuotaSnapshot:
    success: bool
    error_message: Optional[str]
    agy_path: Optional[str]
    active_cli_model: Optional[str]
    remaining_credits: Optional[int]
    upgrade_uri: Optional[str]
    overview_description: Optional[str]
    fetched_at: datetime
    groups: list[QuotaGroupInfo] = field(default_factory=list)

    def format_bubble_summary(self) -> str:
        if not self.success or not self.groups:
            return self.error_message or "暂时无法读取 Antigravity 额度喵～"

        parts: list[str] = []
        for g in self.groups:
            b5h = next((b for b in g.buckets if b.window.lower() == "5h"), None)
            bwk = next((b for b in g.buckets if b.window.lower() == "weekly"), None)
            short_name = g.name.replace(" models", "").replace(" Models", "").strip()
            if b5h is not None and bwk is not None:
                parts.append(
                    f"{short_name}: 5h {b5h.remaining_fraction * 100:.0f}% / 周 {bwk.remaining_fraction * 100:.0f}%"
                )
            elif bwk is not None:
                parts.append(f"{short_name}: {bwk.remaining_fraction * 100:.0f}%")
            elif g.buckets:
                parts.append(f"{short_name}: {g.buckets[0].remaining_fraction * 100:.0f}%")

        summary = " · ".join(parts) if parts else "已读取额度"
        if self.remaining_credits is not None:
            summary += f" · AI Credits: {self.remaining_credits}"
        return f"反重力额度✦ {summary}"

    def format_clipboard_text(self) -> str:
        local_dt = self.fetched_at.astimezone()
        lines = [f"Antigravity 反重力额度快照 ({local_dt.strftime('%Y-%m-%d %H:%M:%S')})"]
        if self.active_cli_model:
            lines.append(f"当前模型: {self.active_cli_model}")
        if self.remaining_credits is not None:
            lines.append(f"AI Credits: {self.remaining_credits}")
        lines.append("")

        for g in self.groups:
            lines.append(f"[{g.name}]")
            if g.description:
                lines.append(f"  {g.description}")
            for b in g.buckets:
                reset_str = b.reset_time.astimezone().strftime("%Y-%m-%d %H:%M") if b.reset_time else "-"
                lines.append(
                    f"  - {b.name} ({b.window}): {b.remaining_fraction * 100:.1f}% 剩余 | 重置: {reset_str}"
                )
        return "\n".join(lines).rstrip()


class AntigravityQuotaService:
    """调用本地官方 agy CLI 查询各模型 5h/weekly 剩余额度与 AI Credits。"""

    @staticmethod
    def resolve_agy_executable() -> Optional[str]:
        local_app_data = Path(os.environ.get("LOCALAPPDATA") or Path.home())
        user_profile = Path(os.environ.get("USERPROFILE") or Path.home())
        candidates = (
            local_app_data / "agy" / "bin" / "agy.exe",
            local_app_data / "agy" / "bin" / "agy.cmd",
            user_profile / ".gemini" / "antigravity" / "bin" / "agy.exe",
        )
        for cand in candidates:
            if cand.is_file():
                return str(cand)
        found = shutil.which("agy.exe") or shutil.which("agy.cmd") or shutil.which("agy")
        if found:
            found_path = Path(found)
            if found_path.suffix.lower() in (".cmd", ".bat"):
                exe_sibling = found_path.with_suffix(".exe")
                if exe_sibling.is_file():
                    return str(exe_sibling)
        return found

    @staticmethod
    def read_active_cli_model() -> Optional[str]:
        try:
            settings_path = Path.home() / ".gemini" / "antigravity-cli" / "settings.json"
            if not settings_path.is_file():
                return None
            data = json.loads(settings_path.read_text(encoding="utf-8"))
            model = data.get("model")
            return str(model) if isinstance(model, str) and model else None
        except Exception:
            return None

    def fetch_quota(self, cancel_check: Optional[callable] = None) -> QuotaSnapshot:  # type: ignore[valid-type]
        agy_path = self.resolve_agy_executable()
        active_model = self.read_active_cli_model()
        now = datetime.now(timezone.utc)

        if not agy_path:
            AppLogger.warn("Quota", "未在本机找到 agy.exe / agy.cmd")
            return QuotaSnapshot(
                False,
                "未在本机找到官方 Antigravity CLI (agy.exe)。请先安装并登录 Antigravity CLI 或桌面端。",
                None,
                active_model,
                None,
                None,
                None,
                now,
                [],
            )

        q_code, q_out, q_err = self._run_agy_command(
            agy_path, ["--print", "/quota", "--output-format", "json", "--print-timeout", "15s"], timeout_s=18.0
        )
        quota_ok, overview, groups, json_err = self._try_parse_quota_json(q_out)
        if not quota_ok and not (cancel_check and cancel_check()):
            time.sleep(0.35)
            q_code, q_out, q_err = self._run_agy_command(
                agy_path, ["--print", "/quota", "--output-format", "json", "--print-timeout", "15s"], timeout_s=18.0
            )
            quota_ok, overview, groups, json_err = self._try_parse_quota_json(q_out)

        remaining_credits: Optional[int] = None
        upgrade_uri: Optional[str] = None
        if not (cancel_check and cancel_check()):
            _, c_out, _ = self._run_agy_command(
                agy_path, ["--print", "/credits", "--output-format", "json", "--print-timeout", "10s"], timeout_s=12.0
            )
            remaining_credits, upgrade_uri = self._try_parse_credits_json(c_out)

        if quota_ok:
            AppLogger.info(
                "Quota",
                f"额度查询成功：分组={len(groups)}, 模型={active_model or 'unknown'}, Credits={remaining_credits if remaining_credits is not None else 'n/a'}",
            )
            return QuotaSnapshot(
                True,
                None,
                agy_path,
                active_model,
                remaining_credits,
                upgrade_uri,
                overview,
                datetime.now(timezone.utc),
                groups,
            )

        detail = (
            json_err
            or (q_err or "").strip()
            or f"agy 查询返回失败 (退出码 {q_code})。请确认已在 Antigravity / agy 中完成 Google 账号登录。"
        )
        AppLogger.warn("Quota", f"额度查询失败 (agyPath={agy_path}, exitCode={q_code}): {detail}")
        return QuotaSnapshot(
            False,
            detail,
            agy_path,
            active_model,
            remaining_credits,
            upgrade_uri,
            None,
            datetime.now(timezone.utc),
            [],
        )

    @staticmethod
    def _run_agy_command(agy_path: str, args: list[str], timeout_s: float) -> tuple[int, str, str]:
        try:
            is_cmd = agy_path.lower().endswith((".cmd", ".bat"))
            comspec = os.environ.get("ComSpec", "cmd.exe")
            cmd = [comspec, "/d", "/c", agy_path, *args] if is_cmd else [agy_path, *args]
            proc = subprocess.Popen(
                cmd,
                cwd=str(Path.home()),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                **subprocess_hidden_kwargs(),
            )
            try:
                stdout, stderr = proc.communicate(timeout=timeout_s)
                return proc.returncode or 0, stdout or "", stderr or ""
            except subprocess.TimeoutExpired:
                proc.kill()
                try:
                    proc.communicate(timeout=2)
                except Exception:
                    pass
                return -1, "", "查询 agy CLI 超时"
        except Exception as exc:
            return -1, "", str(exc)

    @staticmethod
    def _extract_json_object(raw: str) -> Optional[str]:
        if not raw or not raw.strip():
            return None
        start = raw.find("{")
        end = raw.rfind("}")
        if start < 0 or end <= start:
            return None
        return raw[start : end + 1]

    @classmethod
    def _try_parse_quota_json(
        cls, raw: str
    ) -> tuple[bool, Optional[str], list[QuotaGroupInfo], Optional[str]]:
        json_str = cls._extract_json_object(raw)
        if not json_str:
            return False, None, [], None
        try:
            root = json.loads(json_str)
            status = root.get("status")
            if isinstance(status, str) and status.upper() != "SUCCESS":
                resp = root.get("response")
                return False, None, [], str(resp) if isinstance(resp, str) else None

            cmd = root.get("command") or {}
            data = cmd.get("data") or {}
            if not isinstance(data, dict):
                return False, None, [], None

            overview = data.get("description") if isinstance(data.get("description"), str) else None
            raw_groups = data.get("groups")
            if not isinstance(raw_groups, list):
                return False, None, [], None

            groups: list[QuotaGroupInfo] = []
            for g in raw_groups:
                if not isinstance(g, dict):
                    continue
                g_name = str(g.get("name") or "Models")
                g_desc = g.get("description") if isinstance(g.get("description"), str) else None
                buckets: list[QuotaBucketInfo] = []
                for b in g.get("buckets") or []:
                    if not isinstance(b, dict):
                        continue
                    bid = str(b.get("id") or "")
                    bname = str(b.get("name") or bid)
                    bwin = str(b.get("window") or "")
                    bdesc = b.get("description") if isinstance(b.get("description"), str) else None
                    raw_frac = b.get("remaining_fraction")
                    frac = max(0.0, min(1.0, float(raw_frac))) if isinstance(raw_frac, (int, float)) else 0.0
                    reset_dt: Optional[datetime] = None
                    raw_rt = b.get("reset_time")
                    if isinstance(raw_rt, str) and raw_rt:
                        try:
                            reset_dt = datetime.fromisoformat(raw_rt.replace("Z", "+00:00"))
                        except Exception:
                            reset_dt = None
                    buckets.append(QuotaBucketInfo(bid, bname, bwin, frac, reset_dt, bdesc))
                groups.append(QuotaGroupInfo(g_name, g_desc, buckets))

            return len(groups) > 0, overview, groups, None
        except Exception as exc:
            return False, None, [], f"解析额度返回数据失败：{exc}"

    @classmethod
    def _try_parse_credits_json(cls, raw: str) -> tuple[Optional[int], Optional[str]]:
        json_str = cls._extract_json_object(raw)
        if not json_str:
            return None, None
        try:
            root = json.loads(json_str)
            data = (root.get("command") or {}).get("data") or {}
            credits_val = data.get("remaining_credits")
            credits_int = int(credits_val) if isinstance(credits_val, (int, float)) else None
            uri = data.get("upgrade_uri")
            return credits_int, str(uri) if isinstance(uri, str) and uri else None
        except Exception:
            return None, None
