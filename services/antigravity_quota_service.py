"""优先通过 Antigravity 桌面端查询额度，仅 CLI 运行时使用其内置查询命令。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import shutil
import subprocess
import time
from typing import Any, Callable, Optional

import psutil
import requests

from services.app_logger import AppLogger
from services.followed_app_probe import FollowedAppProbe
from services.win32_ipc import subprocess_hidden_kwargs

try:
    import urllib3

    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
except Exception:
    pass

_LS_SERVICE = "exa.language_server_pb.LanguageServerService"
_RPC_HOST = "127.0.0.1"
_CONNECT_TIMEOUT_S = 2.0
_READ_TIMEOUT_S = 5.0
_KIND_ORDER = {"app": 0, "ide": 1}
_IDE_MARKERS = (
    "antigravity ide.app",
    "--app_data_dir antigravity-ide",
    "--app_data_dir=antigravity-ide",
    "extensions\\antigravity\\bin\\language_server",
    "extensions/antigravity/bin/language_server",
)
# Hub 版把端口与令牌写在 daemon 目录的 hub_<port>.json 里。
_HUB_DIRS = (
    Path.home() / ".gemini" / "antigravity" / "daemon",
    Path.home() / ".gemini" / "antigravity-cli" / "daemon",
)
_PROBE_METADATA = {
    "ideName": "antigravity",
    "extensionName": "antigravity",
    "ideVersion": "unknown",
    "locale": "en",
}
_LEGACY_POOL_ORDER = ("Gemini Pro", "Gemini Flash", "Claude", "Gemini 4 Argon")

# 回环地址不应走系统代理，否则本地 RPC 会被转发到代理端口。
_SESSION = requests.Session()
_SESSION.trust_env = False


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
    # 数据源描述（进程类型 / pid / 回环端口），用于卡片提示与复制文本。
    source_detail: Optional[str]
    account_email: Optional[str]
    account_plan: Optional[str]
    overview_description: Optional[str]
    fetched_at: datetime
    groups: list[QuotaGroupInfo] = field(default_factory=list)

    def format_clipboard_text(self) -> str:
        local_dt = self.fetched_at.astimezone()
        lines = [f"Antigravity 额度快照 ({local_dt.strftime('%Y-%m-%d %H:%M:%S')})"]
        if self.account_plan:
            lines.append(f"套餐: {self.account_plan}")
        if self.account_email:
            lines.append(f"账号: {self.account_email}")
        if self.source_detail:
            lines.append(f"数据源: {self.source_detail}")
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


@dataclass(frozen=True)
class _AntigravityProcess:
    pid: int
    kind: str
    csrf_token: str
    extension_port: Optional[int] = None
    hub_port: Optional[int] = None


@dataclass(frozen=True)
class _RpcEndpoint:
    scheme: str
    port: int
    csrf_token: str


@dataclass(frozen=True)
class _QuotaSource:
    label: str
    endpoints: tuple[_RpcEndpoint, ...]


def _first_text(value: Any) -> Optional[str]:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _clamp_fraction(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, str):
        try:
            value = float(value)
        except ValueError:
            return None
    if not isinstance(value, (int, float)):
        return None
    number = float(value)
    if number != number:
        return None
    return max(0.0, min(1.0, number))


def _parse_time(value: Any) -> Optional[datetime]:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        seconds = float(value) / 1000.0 if float(value) > 20_000_000_000 else float(value)
        try:
            return datetime.fromtimestamp(seconds, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    return None


def _unwrap_payload(body: dict[str, Any]) -> dict[str, Any]:
    """Connect-RPC 常把业务字段包在 response / summary 里，剥一层。"""
    for key in ("response", "summary"):
        inner = body.get(key)
        if isinstance(inner, dict):
            return inner
    return body


def _normalize_group_name(raw: Any) -> str:
    text = _first_text(raw) or "Models"
    low = text.lower()
    if low in ("gemini", "gemini model", "gemini models"):
        return "Gemini Models"
    if "claude" in low or "gpt" in low:
        return "Claude and GPT models"
    return text


def _bucket_window(raw: dict[str, Any]) -> Optional[str]:
    text = " ".join(str(raw.get(key) or "") for key in ("window", "bucketId", "displayName", "name")).lower()
    text = text.replace("_", "-")
    if "weekly" in text:
        return "weekly"
    if any(token in text for token in ("session", "5h", "5-hour", "five hour", "five-hour")):
        return "5h"
    return None


def _bucket_fraction(raw: dict[str, Any]) -> Optional[float]:
    fraction = _clamp_fraction(raw.get("remainingFraction"))
    if fraction is not None:
        return fraction
    remaining = raw.get("remaining")
    if isinstance(remaining, dict):
        fraction = _clamp_fraction(remaining.get("remainingFraction"))
        if fraction is None and remaining.get("case") == "remainingFraction":
            fraction = _clamp_fraction(remaining.get("value"))
    return fraction


def _parse_bucket(raw: dict[str, Any]) -> Optional[QuotaBucketInfo]:
    window = _bucket_window(raw)
    if window is None:
        return None
    fraction = _bucket_fraction(raw)
    if fraction is None:
        return None
    name = (
        _first_text(raw.get("displayName"))
        or _first_text(raw.get("name"))
        or _first_text(raw.get("bucketId"))
        or window
    )
    bucket_id = _first_text(raw.get("bucketId")) or _first_text(raw.get("id")) or name
    reset = _parse_time(raw.get("resetTime") if raw.get("resetTime") is not None else raw.get("reset_time"))
    description = raw.get("description") if isinstance(raw.get("description"), str) else None
    return QuotaBucketInfo(bucket_id, name, window, fraction, reset, description)


def _parse_quota_summary(body: dict[str, Any]) -> tuple[Optional[str], list[QuotaGroupInfo]]:
    root = _unwrap_payload(body)
    overview = root.get("description") if isinstance(root.get("description"), str) else None
    raw_groups = root.get("groups")
    if not isinstance(raw_groups, list):
        return overview, []

    groups: list[QuotaGroupInfo] = []
    for raw_group in raw_groups:
        if not isinstance(raw_group, dict):
            continue
        buckets: list[QuotaBucketInfo] = []
        for raw_bucket in raw_group.get("buckets") or []:
            if isinstance(raw_bucket, dict):
                bucket = _parse_bucket(raw_bucket)
                if bucket is not None:
                    buckets.append(bucket)
        if not buckets:
            continue
        description = raw_group.get("description") if isinstance(raw_group.get("description"), str) else None
        groups.append(
            QuotaGroupInfo(
                _normalize_group_name(raw_group.get("displayName") or raw_group.get("name")),
                description,
                buckets,
            )
        )
    return overview, groups


def _legacy_pool_name(text: str) -> Optional[str]:
    low = (text or "").lower()
    if not low or "placeholder" in low:
        return None
    if "argon" in low:
        return "Gemini 4 Argon"
    if "gemini" in low and "pro" in low:
        return "Gemini Pro"
    if "gemini" in low and "flash" in low:
        return "Gemini Flash"
    if "claude" in low or "gpt" in low:
        return "Claude"
    return None


def _legacy_groups(root: dict[str, Any]) -> list[QuotaGroupInfo]:
    """旧版 Antigravity 只回逐模型 quotaInfo，这里按模型族池化成固定分组。"""
    configs = root.get("clientModelConfigs")
    if not isinstance(configs, list):
        cascade = root.get("cascadeModelConfigData")
        configs = cascade.get("clientModelConfigs") if isinstance(cascade, dict) else None
    if not isinstance(configs, list):
        return []

    pools: dict[str, tuple[float, Optional[datetime]]] = {}
    for config in configs:
        if not isinstance(config, dict):
            continue
        quota_info = config.get("quotaInfo")
        if not isinstance(quota_info, dict):
            continue
        fraction = _clamp_fraction(quota_info.get("remainingFraction"))
        if fraction is None:
            continue
        alias = config.get("modelOrAlias")
        model_id = str(alias.get("model") or "") if isinstance(alias, dict) else ""
        pool = _legacy_pool_name(model_id or str(config.get("label") or config.get("displayName") or ""))
        if pool is None:
            continue
        reset = _parse_time(
            quota_info.get("resetTime") if quota_info.get("resetTime") is not None else quota_info.get("reset_time")
        )
        previous = pools.get(pool)
        # 同池多个模型取最保守值；剩余量相同时取更早的重置时间。
        if previous is None or fraction < previous[0]:
            pools[pool] = (fraction, reset or (previous[1] if previous else None))
        elif reset is not None and (previous[1] is None or reset < previous[1]):
            pools[pool] = (previous[0], reset)

    groups: list[QuotaGroupInfo] = []
    for name in _LEGACY_POOL_ORDER:
        if name not in pools:
            continue
        fraction, reset = pools[name]
        groups.append(QuotaGroupInfo(name, None, [QuotaBucketInfo("legacy", name, "weekly", fraction, reset, None)]))
    return groups


def _parse_user_status(body: dict[str, Any]) -> tuple[Optional[str], Optional[str], list[QuotaGroupInfo]]:
    root = _unwrap_payload(body)
    user = root.get("userStatus") if isinstance(root.get("userStatus"), dict) else root
    email = _first_text(user.get("email"))

    plan: Optional[str] = None
    for container in (user, root):
        tier = container.get("userTier") or container.get("paidTier") or container.get("currentTier")
        if isinstance(tier, dict):
            plan = _first_text(tier.get("name")) or _first_text(tier.get("displayName")) or _first_text(tier.get("id"))
        if plan:
            break
        plan_info = container.get("planInfo")
        if isinstance(plan_info, dict):
            for key in ("planDisplayName", "displayName", "productName", "planName", "planShortName"):
                plan = _first_text(plan_info.get(key))
                if plan:
                    break
        if plan:
            break

    return email, plan, _legacy_groups(root)


def _extract_flag(cmdline: list[str], flag: str) -> Optional[str]:
    prefix = f"{flag}="
    for index, part in enumerate(cmdline):
        if part.startswith(prefix):
            value = part[len(prefix) :].strip().strip('"').strip("'")
            return value or None
        if part == flag and index + 1 < len(cmdline):
            value = cmdline[index + 1].strip().strip('"').strip("'")
            return value or None
    return None


def _extract_port(cmdline: list[str], flag: str) -> Optional[int]:
    raw = _extract_flag(cmdline, flag)
    if not raw:
        return None
    try:
        port = int(raw)
    except ValueError:
        return None
    return port if 0 < port < 65536 else None


def _port_from_address(address: Optional[str]) -> Optional[int]:
    text = (address or "").strip()
    if not text:
        return None
    tail = text.rsplit(":", 1)[-1]
    try:
        port = int(tail)
    except ValueError:
        return None
    return port if 0 < port < 65536 else None


def _detect_process_kind(name: str, cmdline: list[str]) -> Optional[str]:
    exe = (name or "").strip().lower()
    command = " ".join(cmdline).strip().lower() or exe
    if not any(token in exe or token in command for token in ("language_server", "language-server")):
        return None
    if any(marker in command for marker in _IDE_MARKERS):
        return "ide"
    if "antigravity" in command or "antigravity" in exe:
        return "app"
    return None


def _listening_ports(pid: int) -> list[int]:
    connections: list[Any] = []
    try:
        connections = psutil.Process(pid).net_connections(kind="tcp")
    except (psutil.Error, OSError):
        connections = []
    if not connections:
        try:
            connections = [conn for conn in psutil.net_connections(kind="tcp") if conn.pid == pid]
        except (psutil.Error, OSError):
            connections = []
    ports: set[int] = set()
    for conn in connections:
        if conn.status == psutil.CONN_LISTEN and conn.laddr:
            ports.add(int(conn.laddr.port))
    return sorted(ports)


def _listening_port_set() -> set[int]:
    """一次性拿到本机所有 LISTEN 端口，用来剔除已退出的 hub 残留记录。"""
    try:
        return {
            int(conn.laddr.port)
            for conn in psutil.net_connections(kind="tcp")
            if conn.status == psutil.CONN_LISTEN and conn.laddr
        }
    except (psutil.Error, OSError):
        return set()


def _pid_looks_like_antigravity(pid: int) -> bool:
    try:
        name = psutil.Process(pid).name().strip().lower()
    except (psutil.Error, OSError):
        return False
    return "language_server" in name or "language-server" in name or name.startswith(("antigravity", "agy"))


def _hub_file_sources(listening: set[int]) -> list[_QuotaSource]:
    """读取 Hub 写在 daemon 目录的 hub_<port>.json（显式给了端口与令牌）。"""
    entries: list[tuple[float, _QuotaSource]] = []
    for directory in _HUB_DIRS:
        try:
            files = list(directory.glob("hub_*.json"))
        except OSError:
            continue
        for path in files:
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                mtime = path.stat().st_mtime
            except (OSError, ValueError):
                continue
            pid = data.get("pid")
            token = _first_text(data.get("csrfToken"))
            if not isinstance(pid, int) or not token:
                continue
            # 端口必须真的在监听，且 pid 不能被系统复用给无关进程。
            if not _pid_looks_like_antigravity(pid):
                continue
            endpoints: list[_RpcEndpoint] = []
            for scheme, key in (("https", "httpsPort"), ("http", "httpPort")):
                port = data.get(key)
                if isinstance(port, int) and port in listening:
                    endpoints.append(_RpcEndpoint(scheme, port, token))
            if endpoints:
                entries.append((mtime, _QuotaSource(f"Antigravity hub · pid {pid}", tuple(endpoints))))
    entries.sort(key=lambda item: item[0], reverse=True)
    return [source for _, source in entries]


def _process_sources(listening: set[int]) -> list[_QuotaSource]:
    found: list[tuple[int, int, _QuotaSource]] = []
    for proc in psutil.process_iter(["pid", "name", "cmdline"]):
        try:
            info = proc.info
            pid = int(info.get("pid") or proc.pid)
            name = str(info.get("name") or "")
            cmdline = [str(part) for part in (info.get("cmdline") or [])]
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue

        kind = _detect_process_kind(name, cmdline)
        csrf_token = _extract_flag(cmdline, "--csrf_token")
        if kind is not None and csrf_token:
            process = _AntigravityProcess(
                pid=pid,
                kind=kind,
                csrf_token=csrf_token,
                extension_port=_extract_port(cmdline, "--extension_server_port"),
                hub_port=_extract_port(cmdline, "--hub_port") or _extract_port(cmdline, "--hub-port"),
            )
            endpoints = tuple(endpoint for endpoint in _endpoints_for(process) if endpoint.port in listening)
            if endpoints:
                found.append(
                    (
                        _KIND_ORDER.get(kind, 9),
                        pid,
                        _QuotaSource(f"Antigravity {kind} · pid {pid}", endpoints),
                    )
                )
            continue

        # Hub 版把令牌与语言服务器地址放在进程环境变量里。
        if not name.lower().startswith(("antigravity", "agy")):
            continue
        try:
            env = proc.environ()
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess, OSError):
            continue
        env_token = _first_text(env.get("ANTIGRAVITY_CSRF_TOKEN"))
        env_port = _port_from_address(env.get("ANTIGRAVITY_LS_ADDRESS"))
        if env_token and env_port and env_port in listening:
            found.append(
                (
                    _KIND_ORDER.get("app", 9),
                    pid,
                    _QuotaSource(f"Antigravity sidecar · pid {pid}", (_RpcEndpoint("http", env_port, env_token),)),
                )
            )

    found.sort(key=lambda item: (item[0], item[1]))
    return [source for _, _, source in found]


def _endpoints_for(process: _AntigravityProcess) -> list[_RpcEndpoint]:
    endpoints: list[_RpcEndpoint] = []
    seen: set[tuple[str, int, str]] = set()

    def add(scheme: str, port: Optional[int]) -> None:
        if not port or not (0 < int(port) < 65536):
            return
        key = (scheme, int(port), process.csrf_token)
        if key in seen:
            return
        seen.add(key)
        endpoints.append(_RpcEndpoint(scheme, int(port), process.csrf_token))

    # 显式 hub 端口优先，其次监听端口，最后扩展服务端口。
    add("https", process.hub_port)
    add("http", process.hub_port)
    for port in _listening_ports(process.pid):
        add("https", port)
        add("http", port)
    add("http", process.extension_port)
    return endpoints


class AntigravityQuotaService:
    """发现运行中的 Antigravity 语言服务器，通过回环 RPC 读取 5h / 每周额度。"""

    @staticmethod
    def _discover_sources() -> list[_QuotaSource]:
        listening = _listening_port_set()
        sources = _hub_file_sources(listening) + _process_sources(listening)
        deduped: list[_QuotaSource] = []
        seen: set[tuple[str, int, str]] = set()
        for source in sources:
            endpoints = tuple(
                endpoint for endpoint in source.endpoints if (endpoint.scheme, endpoint.port, endpoint.csrf_token) not in seen
            )
            if not endpoints:
                continue
            seen.update((endpoint.scheme, endpoint.port, endpoint.csrf_token) for endpoint in endpoints)
            deduped.append(_QuotaSource(source.label, endpoints))
        return deduped

    @staticmethod
    def _post(endpoint: _RpcEndpoint, method: str, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        url = f"{endpoint.scheme}://{_RPC_HOST}:{endpoint.port}/{_LS_SERVICE}/{method}"
        headers = {
            "content-type": "application/json",
            "connect-protocol-version": "1",
            "x-codeium-csrf-token": endpoint.csrf_token,
        }
        response = _SESSION.post(
            url,
            json=payload,
            headers=headers,
            timeout=(_CONNECT_TIMEOUT_S, _READ_TIMEOUT_S),
            verify=False,
        )
        try:
            body = response.json()
        except ValueError:
            body = {}
        return response.status_code, body if isinstance(body, dict) else {}

    def _fetch_user_status(self, endpoint: _RpcEndpoint) -> tuple[Optional[str], Optional[str], list[QuotaGroupInfo]]:
        try:
            status, body = self._post(endpoint, "GetUserStatus", {"metadata": _PROBE_METADATA})
        except requests.RequestException:
            return None, None, []
        if status != 200:
            return None, None, []
        return _parse_user_status(body)

    @staticmethod
    def _cli_failure(message: str) -> QuotaSnapshot:
        return QuotaSnapshot(
            False, message, "Antigravity CLI", None, None, None,
            datetime.now(timezone.utc), [],
        )

    @staticmethod
    def _find_cli_executable() -> Optional[str]:
        # 优先复用当前运行实例的安装位置，兼容启动桌宠后才安装 CLI 的情况。
        for proc in psutil.process_iter(["name", "exe"]):
            try:
                if (proc.info.get("name") or "").lower() in {"agy", "agy.exe"}:
                    executable = proc.info.get("exe")
                    if executable and Path(executable).is_file():
                        return str(executable)
            except (psutil.Error, OSError):
                continue
        return shutil.which("agy")

    @staticmethod
    def _parse_cli_usage(output: str) -> list[QuotaGroupInfo]:
        # /usage 的 JSON response 是 TSV：分组、额度窗口、剩余百分比、重置时间。
        grouped: dict[str, list[QuotaBucketInfo]] = {}
        for line in output.splitlines():
            columns = line.split("\t")
            if len(columns) != 4:
                continue
            group, name, remaining, reset = (part.strip() for part in columns)
            window = _bucket_window({"name": name})
            if not group or not name or not window or not remaining.endswith("%"):
                continue
            try:
                percent = float(remaining[:-1])
            except ValueError:
                continue
            if not math.isfinite(percent) or not 0 <= percent <= 100:
                continue
            grouped.setdefault(_normalize_group_name(group), []).append(
                QuotaBucketInfo(name, name, window, percent / 100.0, _parse_time(reset))
            )
        return [QuotaGroupInfo(name, buckets=buckets) for name, buckets in grouped.items()]

    def _fetch_cli_quota(self, cancel_check: Optional[Callable[[], bool]]) -> QuotaSnapshot:
        if cancel_check and cancel_check():
            return self._cli_failure("额度查询已取消。")
        try:
            executable = self._find_cli_executable()
        except (psutil.Error, OSError):
            executable = None
        if not executable:
            return self._cli_failure("无法找到正在运行的 Antigravity CLI，请确认 CLI 安装完整后重试。")
        process = None
        try:
            # 仅调用 CLI 内置的额度命令，不发起模型对话；输出不写日志以避免泄露账号信息。
            process = subprocess.Popen(
                [executable, "-p", "/usage", "--output-format", "json", "--print-timeout", "20s"],
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                text=True, encoding="utf-8", errors="replace", **subprocess_hidden_kwargs(),
            )
            deadline = time.monotonic() + 30.0
            while True:
                if cancel_check and cancel_check():
                    return self._cli_failure("额度查询已取消。")
                if time.monotonic() >= deadline:
                    return self._cli_failure("Antigravity CLI 额度查询超时，请稍后重试。")
                try:
                    output, _ = process.communicate(timeout=0.2)
                    break
                except subprocess.TimeoutExpired:
                    continue
            try:
                body = json.loads(output)
            except ValueError:
                return self._cli_failure("Antigravity CLI 返回了无法识别的额度数据，请更新 CLI 后重试。")
            if process.returncode != 0 or not isinstance(body, dict) or body.get("status") != "SUCCESS":
                return self._cli_failure("Antigravity CLI 额度查询失败，请在 CLI 中确认 /usage 可正常使用后重试。")
            response = body.get("response")
            groups = self._parse_cli_usage(response) if isinstance(response, str) else []
            if not groups:
                return self._cli_failure("Antigravity CLI 未返回可用的额度数据，请更新 CLI 后重试。")
            return QuotaSnapshot(
                True, None, "Antigravity CLI", None, None, None,
                datetime.now(timezone.utc), groups,
            )
        except OSError:
            return self._cli_failure("无法启动 Antigravity CLI 额度查询，请检查安装后重试。")
        finally:
            # 取消或超时仅回收本次查询进程，不影响用户正在使用的 CLI 会话。
            if process is not None:
                try:
                    if process.poll() is None:
                        process.kill()
                    process.communicate(timeout=2.0)
                except (OSError, subprocess.TimeoutExpired):
                    pass
                finally:
                    if process.stdout is not None:
                        process.stdout.close()

    def fetch_quota(self, cancel_check: Optional[Callable[[], bool]] = None) -> QuotaSnapshot:
        if cancel_check and cancel_check():
            return self._cli_failure("额度查询已取消。")
        # 桌面端优先；只有 CLI 独立运行时才委托它查询，保持原桌面端路径不变。
        if (
            not FollowedAppProbe.sample_antigravity_activity().is_running
            and FollowedAppProbe.sample_antigravity_cli_activity().is_running
        ):
            return self._fetch_cli_quota(cancel_check)
        sources = self._discover_sources()
        if not sources:
            AppLogger.warn("Quota", "未检测到 Antigravity 语言服务器")
            return QuotaSnapshot(
                False,
                "未检测到 Antigravity 的额度服务。请先打开 Antigravity 桌面端、CLI 或 IDE 后再查看额度。",
                None,
                None,
                None,
                None,
                datetime.now(timezone.utc),
                [],
            )

        last_error = ""
        for source in sources:
            if cancel_check and cancel_check():
                break
            rejected = False
            for endpoint in source.endpoints:
                if cancel_check and cancel_check():
                    break
                try:
                    status, body = self._post(endpoint, "RetrieveUserQuotaSummary", {"forceRefresh": True})
                except requests.RequestException:
                    continue
                if status in (401, 403):
                    # 同一来源换端口也没用，直接看下一个来源。
                    last_error = "Antigravity 本地接口拒绝了请求（CSRF 校验失败），请重启 Antigravity 后再试。"
                    rejected = True
                    break
                if status != 200:
                    last_error = f"Antigravity 本地接口返回 HTTP {status}。"
                    continue

                overview, groups = _parse_quota_summary(body)
                email, plan, legacy_groups = self._fetch_user_status(endpoint)
                if not groups:
                    groups = legacy_groups
                if not groups:
                    last_error = "Antigravity 没有返回额度数据，请确认已在应用内登录账号。"
                    continue

                source_detail = f"{source.label} · {endpoint.scheme}://{_RPC_HOST}:{endpoint.port}"
                AppLogger.info(
                    "Quota",
                    f"额度查询成功：来源={source.label}, 分组={len(groups)}, 套餐={plan or 'unknown'}",
                )
                return QuotaSnapshot(
                    True,
                    None,
                    source_detail,
                    email,
                    plan,
                    overview,
                    datetime.now(timezone.utc),
                    groups,
                )
            if rejected:
                continue

        detail = last_error or "Antigravity 额度服务未响应，请确认 Antigravity 正在运行后重试。"
        AppLogger.warn("Quota", f"额度查询失败：{detail}")
        return QuotaSnapshot(
            False,
            detail,
            None,
            None,
            None,
            None,
            datetime.now(timezone.utc),
            [],
        )
