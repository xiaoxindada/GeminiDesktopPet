"""Google OAuth 2.0 本地授权优先读取、用户自建客户端授权（PKCE）与凭据持久化服务。"""

from __future__ import annotations

import base64
from dataclasses import asdict, dataclass, field
from datetime import datetime
import hashlib
import http.server
import json
import os
from pathlib import Path
import re
import secrets
import socket
import threading
import time
from typing import Any, Callable, Optional
import urllib.parse
import uuid

import requests

from models.app_version import get_user_config_dir
from services.app_logger import AppLogger

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_URL = "https://www.googleapis.com/oauth2/v2/userinfo"
GEMINI_API_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"

# 采用 Google Cloud / Gemini API 官方文档公开标准作用域，兼容用户自建 OAuth 客户端与 ADC
OAUTH_SCOPES: tuple[str, ...] = (
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/userinfo.profile",
    "https://www.googleapis.com/auth/cloud-platform",
    "https://www.googleapis.com/auth/generative-language.retriever",
)

HUB_USER_AGENT = "GeminiDesktopPet/1.0"
TOKEN_REFRESH_SKEW_SECONDS = 900

V1_INTERNAL_CHAT_BASE_URLS: tuple[str, ...] = (
    "https://daily-cloudcode-pa.googleapis.com/v1internal",
    "https://daily-cloudcode-pa.sandbox.googleapis.com/v1internal",
)

V1_INTERNAL_METADATA_BASE_URLS: tuple[str, ...] = (
    "https://daily-cloudcode-pa.googleapis.com/v1internal",
    "https://daily-cloudcode-pa.sandbox.googleapis.com/v1internal",
    "https://cloudcode-pa.googleapis.com/v1internal",
)

_OAUTH_SUCCESS_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head><meta charset="utf-8"><title>授权成功</title></head>
<body style="font-family: 'Microsoft YaHei UI', sans-serif; background: #141126; color: #F4F2FF; text-align: center; padding: 60px 20px;">
  <h1 style="color: #34D399;">✅ Google 账号授权成功</h1>
  <p style="color: #C4B5FD;">你可以关闭此页面并返回「Gemini 娘 · 桌面萌宠」了喵✦</p>
  <script>setTimeout(function(){ window.close(); }, 1800);</script>
</body>
</html>"""

_OAUTH_FAIL_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head><meta charset="utf-8"><title>授权失败</title></head>
<body style="font-family: 'Microsoft YaHei UI', sans-serif; background: #141126; color: #F4F2FF; text-align: center; padding: 60px 20px;">
  <h1 style="color: #FB7185;">❌ 授权未完成</h1>
  <p style="color: #C4B5FD;">未能获取有效授权码，请返回应用重试或改用手动提交回调链接。</p>
</body>
</html>"""

_REFRESH_TOKEN_RE = re.compile(r"1//[a-zA-Z0-9_\-]+")


class OAuthError(RuntimeError):
    """OAuth 授权、刷新或上游 API 调用错误。"""

    def __init__(self, message: str, status_code: Optional[int] = None) -> None:
        super().__init__(message)
        self.code = status_code


@dataclass(frozen=True)
class OAuthClientConfig:
    """用户自带 Google OAuth 2.0 桌面客户端配置（绝不在源码中硬编码）。"""

    client_id: str
    client_secret: str = ""

    def is_valid(self) -> bool:
        return bool((self.client_id or "").strip())


@dataclass
class OAuthSession:
    """持久化于本地配置目录的 OAuth 个人/本地授权凭据与元数据。"""

    email: str = ""
    display_name: str = ""
    access_token: str = ""
    refresh_token: str = ""
    expiry_timestamp: float = 0.0
    project_id: str = ""
    subscription_tier: str = ""
    available_models: list[str] = field(default_factory=list)
    updated_at: float = 0.0
    client_id: str = ""
    client_secret: str = ""
    auth_source: str = ""
    api_backend: str = ""

    def is_logged_in(self, now: Optional[float] = None) -> bool:
        if (self.refresh_token or "").strip():
            return True
        if (self.access_token or "").strip():
            cur = time.time() if now is None else now
            return self.expiry_timestamp <= 0 or self.expiry_timestamp > cur + 30.0
        return False

    def can_refresh(self, fallback_client: Optional[OAuthClientConfig] = None) -> bool:
        if not (self.refresh_token or "").strip():
            return False
        if (self.client_id or "").strip():
            return True
        return bool(fallback_client is not None and fallback_client.is_valid())

    def masked_email(self) -> str:
        return mask_email(self.email)


def mask_email(email: str) -> str:
    """脱敏邮箱用于日志输出，避免在日志文件中暴露完整个人账号。"""
    cleaned = (email or "").strip()
    if not cleaned or "@" not in cleaned:
        return "***" if cleaned else ""
    local, domain = cleaned.split("@", 1)
    if len(local) <= 2:
        masked_local = local[0] + "*"
    else:
        masked_local = local[:2] + "***"
    return f"{masked_local}@{domain}"


def _resolve_home_dir() -> Path:
    env_home = (os.environ.get("USERPROFILE") or os.environ.get("HOME") or "").strip()
    return Path(env_home) if env_home else Path.home()


def _resolve_appdata_dir() -> Path:
    env_appdata = (os.environ.get("APPDATA") or "").strip()
    if env_appdata:
        return Path(env_appdata)
    return _resolve_home_dir() / "AppData" / "Roaming"


def _generate_pkce_pair() -> tuple[str, str]:
    """生成符合 RFC 7636 的 PKCE verifier 与 S256 challenge。"""
    verifier = secrets.token_urlsafe(64)[:96]
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def _parse_expiry_timestamp(raw: Any) -> float:
    if raw is None or isinstance(raw, bool):
        return 0.0
    if isinstance(raw, (int, float)):
        val = float(raw)
        return val / 1000.0 if val > 1e11 else val
    if isinstance(raw, str):
        cleaned = raw.strip()
        if not cleaned:
            return 0.0
        try:
            num = float(cleaned)
            return num / 1000.0 if num > 1e11 else num
        except ValueError:
            pass
        try:
            return datetime.fromisoformat(cleaned.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return 0.0
    return 0.0


def parse_oauth_client_json(raw_input: str | dict[str, Any]) -> OAuthClientConfig:
    """解析 Google Cloud 下载的 client_secret*.json 或扁平 OAuth 客户端 JSON。"""
    data: Any = raw_input
    if isinstance(raw_input, str):
        text = raw_input.strip()
        if not text:
            raise OAuthError("OAuth 客户端配置 JSON 不能为空")
        try:
            data = json.loads(text)
        except Exception as exc:
            raise OAuthError(f"解析 OAuth 客户端 JSON 失败：{exc}") from exc

    if not isinstance(data, dict):
        raise OAuthError("OAuth 客户端配置必须为 JSON 对象")

    node: dict[str, Any] = data
    for wrapper in ("installed", "web", "client"):
        sub = data.get(wrapper)
        if isinstance(sub, dict):
            node = sub
            break

    client_id = str(node.get("client_id") or node.get("clientId") or "").strip()
    client_secret = str(node.get("client_secret") or node.get("clientSecret") or "").strip()
    if not client_id:
        raise OAuthError("未在 JSON 中找到有效的 client_id")
    return OAuthClientConfig(client_id=client_id, client_secret=client_secret)


def build_personal_auth_url(
    redirect_uri: str,
    state: str,
    client_id: str,
    *,
    code_challenge: Optional[str] = None,
) -> str:
    """使用用户配置的 Client ID 构造 Google OAuth 2.0 授权链接（支持 PKCE）。"""
    cid = (client_id or "").strip()
    if not cid:
        raise OAuthError("请先配置 OAuth Client ID（可导入 Google client_secret.json）")
    params = {
        "client_id": cid,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(OAUTH_SCOPES),
        "access_type": "offline",
        "prompt": "consent",
        "include_granted_scopes": "true",
        "state": state,
    }
    if code_challenge:
        params["code_challenge"] = code_challenge
        params["code_challenge_method"] = "S256"
    return f"{GOOGLE_AUTH_URL}?{urllib.parse.urlencode(params)}"


def parse_manual_oauth_input(raw_input: str) -> tuple[str, Optional[str]]:
    """解析用户手动粘贴的回调 URL、无协议地址、查询串或原始 Authorization Code。"""
    trimmed = (raw_input or "").strip()
    if not trimmed:
        raise OAuthError("授权码或回调链接不能为空")

    def _extract_from_parsed(parsed: urllib.parse.ParseResult) -> Optional[tuple[str, Optional[str]]]:
        qs = urllib.parse.parse_qs(parsed.query, keep_blank_values=False)
        err_vals = [v.strip() for v in qs.get("error", []) if v.strip()]
        if err_vals:
            raise OAuthError(f"OAuth 回调返回错误：{err_vals[0]}")
        codes = [v.strip() for v in qs.get("code", []) if v.strip()]
        if not codes:
            return None
        states = [v.strip() for v in qs.get("state", []) if v.strip()]
        return codes[0], (states[0] if states else None)

    if trimmed.startswith(("http://", "https://")):
        parsed = urllib.parse.urlparse(trimmed)
        res = _extract_from_parsed(parsed)
        if res is not None:
            return res
        raise OAuthError("回调链接中未包含 code 参数")

    if (
        trimmed.startswith(("localhost:", "127.0.0.1:", "[::1]:"))
        or "code=" in trimmed
        or "error=" in trimmed
    ):
        if trimmed.startswith(("?", "/")):
            candidate = f"http://localhost{trimmed}"
        elif "://" in trimmed:
            candidate = trimmed
        elif trimmed.startswith(("localhost", "127.0.0.1", "[::1]")):
            candidate = f"http://{trimmed}"
        else:
            candidate = f"http://localhost/?{trimmed.lstrip('&')}"
        parsed = urllib.parse.urlparse(candidate)
        res = _extract_from_parsed(parsed)
        if res is not None:
            return res

    decoded = urllib.parse.unquote(trimmed).strip()
    if not decoded:
        raise OAuthError("授权码格式无效")
    return decoded, None


def extract_refresh_token_from_input(raw_input: str) -> str:
    """从纯文本、单条 JSON 对象或账号 JSON 数组中提取 Refresh Token。"""
    text = (raw_input or "").strip()
    if not text:
        raise OAuthError("请输入 Refresh Token")

    if text.startswith(("{", "[")):
        try:
            parsed = json.loads(text)
            candidates: list[Any] = parsed if isinstance(parsed, list) else [parsed]
            for item in candidates:
                if isinstance(item, dict):
                    rt = item.get("refresh_token") or item.get("refreshToken")
                    if isinstance(rt, str) and rt.strip():
                        return rt.strip()
        except Exception:
            pass

    match = _REFRESH_TOKEN_RE.search(text)
    if match:
        return match.group(0)
    raise OAuthError("未识别到有效的 Google Refresh Token（通常以 1// 开头）")


class _IPv6HTTPServer(http.server.HTTPServer):
    address_family = socket.AF_INET6
    allow_reuse_address = True

    def server_bind(self) -> None:
        self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
        super().server_bind()


class _IPv4HTTPServer(http.server.HTTPServer):
    address_family = socket.AF_INET
    allow_reuse_address = True


class OAuthLoopbackFlow:
    """管理单次 OAuth PKCE 授权流：在 IPv4/IPv6 回环端口等待回调并支持手动提交。"""

    def __init__(
        self,
        *,
        client_id: str,
        client_secret: str = "",
        enable_listener: bool = True,
        on_callback_received: Optional[Callable[[], None]] = None,
    ) -> None:
        cleaned_id = (client_id or "").strip()
        if not cleaned_id:
            raise OAuthError("请先配置 OAuth Client ID（可导入 Google client_secret.json）")
        self.client_id: str = cleaned_id
        self.client_secret: str = (client_secret or "").strip()
        self.state: str = str(uuid.uuid4())
        self.code_verifier, self.code_challenge = _generate_pkce_pair()
        self._on_callback_received = on_callback_received
        self._done_event = threading.Event()
        self._lock = threading.Lock()
        self._cancelled = False
        self._code: Optional[str] = None
        self._error: Optional[str] = None
        self._servers: list[http.server.HTTPServer] = []
        self._threads: list[threading.Thread] = []

        if enable_listener:
            self.redirect_uri = self._start_loopback_servers()
        else:
            self.redirect_uri = "http://localhost:8045/oauth-callback"
        self.auth_url = build_personal_auth_url(
            self.redirect_uri,
            self.state,
            self.client_id,
            code_challenge=self.code_challenge,
        )

    def _make_handler_class(self) -> type[http.server.BaseHTTPRequestHandler]:
        flow = self

        class _CallbackHandler(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                parsed = urllib.parse.urlparse(self.path)
                if parsed.path not in ("/oauth-callback", "/auth/callback", "/"):
                    self.send_response(404)
                    self.end_headers()
                    return

                qs = urllib.parse.parse_qs(parsed.query, keep_blank_values=False)
                err_vals = [v.strip() for v in qs.get("error", []) if v.strip()]
                code_vals = [v.strip() for v in qs.get("code", []) if v.strip()]
                state_vals = [v.strip() for v in qs.get("state", []) if v.strip()]

                if err_vals:
                    ok = False
                    flow._resolve_from_callback(None, f"Google 授权被拒绝或失败：{err_vals[0]}")
                elif not code_vals:
                    ok = False
                    flow._resolve_from_callback(None, "回调请求中缺少 Authorization Code")
                elif (state_vals[0] if state_vals else None) != flow.state:
                    ok = False
                    flow._resolve_from_callback(None, "OAuth state 校验不匹配（CSRF 防护）")
                else:
                    ok = True
                    flow._resolve_from_callback(code_vals[0], None)

                body = (_OAUTH_SUCCESS_HTML if ok else _OAUTH_FAIL_HTML).encode("utf-8")
                self.send_response(200 if ok else 400)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format: str, *args: object) -> None:  # noqa: A002
                return

        return _CallbackHandler

    def _start_loopback_servers(self) -> str:
        handler_cls = self._make_handler_class()
        srv_v6: Optional[http.server.HTTPServer] = None
        srv_v4: Optional[http.server.HTTPServer] = None
        port = 0

        # 浏览器解析 localhost 时可能优先走 ::1 或 127.0.0.1，尽量同端口双栈监听
        try:
            srv_v6 = _IPv6HTTPServer(("::1", 0), handler_cls)
            port = int(srv_v6.server_address[1])
            try:
                srv_v4 = _IPv4HTTPServer(("127.0.0.1", port), handler_cls)
            except OSError:
                srv_v4 = None
        except OSError:
            srv_v6 = None
            try:
                srv_v4 = _IPv4HTTPServer(("127.0.0.1", 0), handler_cls)
                port = int(srv_v4.server_address[1])
                try:
                    srv_v6 = _IPv6HTTPServer(("::1", port), handler_cls)
                except OSError:
                    srv_v6 = None
            except OSError:
                srv_v4 = None

        for srv in (srv_v4, srv_v6):
            if srv is not None:
                srv.timeout = 0.5
                self._servers.append(srv)
                th = threading.Thread(
                    target=srv.serve_forever,
                    kwargs={"poll_interval": 0.2},
                    name="GeminiPetOAuthCallback",
                    daemon=True,
                )
                th.start()
                self._threads.append(th)

        if srv_v4 is not None and srv_v6 is not None:
            return f"http://localhost:{port}/oauth-callback"
        if srv_v4 is not None:
            return f"http://127.0.0.1:{port}/oauth-callback"
        if srv_v6 is not None:
            return f"http://[::1]:{port}/oauth-callback"
        return "http://localhost:8045/oauth-callback"

    def _resolve_from_callback(self, code: Optional[str], error: Optional[str]) -> None:
        notify = False
        with self._lock:
            if self._done_event.is_set() or self._cancelled:
                return
            self._code = code
            self._error = error
            self._done_event.set()
            notify = True
        if notify and self._on_callback_received is not None:
            try:
                self._on_callback_received()
            except Exception:
                pass

    def has_result(self) -> bool:
        return self._done_event.is_set()

    def wait_for_code(self, timeout_s: Optional[float] = None) -> str:
        if not self._done_event.wait(timeout=timeout_s):
            raise OAuthError("等待 Google OAuth 授权回调超时")
        with self._lock:
            if self._cancelled:
                raise OAuthError("OAuth 授权已取消")
            if self._error:
                raise OAuthError(self._error)
            if not self._code:
                raise OAuthError("未能从回调中获取授权码")
            return self._code

    def cancel(self) -> None:
        with self._lock:
            if self._cancelled:
                return
            self._cancelled = True
            if not self._done_event.is_set():
                self._error = "OAuth 授权已取消"
                self._done_event.set()
            servers = list(self._servers)
            self._servers.clear()

        for srv in servers:
            try:
                srv.shutdown()
            except Exception:
                pass
            try:
                srv.server_close()
            except Exception:
                pass


class OAuthAuthService:
    """优先读取本机已有 Google OAuth 授权，并支持用户自建客户端授权、Token 刷新与模型发现。"""

    def __init__(self, custom_config_dir: Optional[str | os.PathLike[str]] = None) -> None:
        folder = Path(custom_config_dir) if custom_config_dir else get_user_config_dir()
        self.config_dir: Path = folder
        self.session_path: Path = folder / "oauth_session.json"
        self.client_config_path: Path = folder / "oauth_client.json"
        self._optout_path: Path = folder / ".oauth_local_disabled"
        self._lock = threading.RLock()
        self._cached_session: Optional[OAuthSession] = None

    def get_client_config(
        self, override: Optional[OAuthClientConfig] = None
    ) -> Optional[OAuthClientConfig]:
        if override is not None and override.is_valid():
            return OAuthClientConfig(
                client_id=override.client_id.strip(),
                client_secret=override.client_secret.strip(),
            )

        if self.client_config_path.is_file():
            try:
                raw = json.loads(self.client_config_path.read_text(encoding="utf-8-sig"))
                cfg = parse_oauth_client_json(raw)
                if cfg.is_valid():
                    return cfg
            except Exception as exc:
                AppLogger.warn("OAuth", f"读取 oauth_client.json 失败：{exc}")

        env_id = (
            os.environ.get("GEMINI_DESKTOP_PET_OAUTH_CLIENT_ID")
            or os.environ.get("GOOGLE_OAUTH_CLIENT_ID")
            or ""
        ).strip()
        if env_id:
            env_secret = (
                os.environ.get("GEMINI_DESKTOP_PET_OAUTH_CLIENT_SECRET")
                or os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET")
                or ""
            ).strip()
            return OAuthClientConfig(client_id=env_id, client_secret=env_secret)

        # 若当前会话或本机 ADC 自身携带了客户端凭据，也可直接复用以完成刷新
        session = self._read_saved_session_file()
        if session is not None and (session.client_id or "").strip():
            return OAuthClientConfig(
                client_id=session.client_id.strip(),
                client_secret=session.client_secret.strip(),
            )
        discovered = self.discover_local_session()
        if discovered is not None and (discovered.client_id or "").strip():
            return OAuthClientConfig(
                client_id=discovered.client_id.strip(),
                client_secret=discovered.client_secret.strip(),
            )
        return None

    def save_client_config(self, config: OAuthClientConfig) -> OAuthClientConfig:
        cleaned = OAuthClientConfig(
            client_id=(config.client_id or "").strip(),
            client_secret=(config.client_secret or "").strip(),
        )
        if not cleaned.is_valid():
            raise OAuthError("OAuth Client ID 不能为空")
        with self._lock:
            self.config_dir.mkdir(parents=True, exist_ok=True)
            tmp_path = self.client_config_path.with_suffix(".json.tmp")
            payload = json.dumps(asdict(cleaned), ensure_ascii=False, indent=2) + "\n"
            tmp_path.write_text(payload, encoding="utf-8")
            os.replace(tmp_path, self.client_config_path)
        return cleaned

    def clear_client_config(self) -> None:
        with self._lock:
            if self.client_config_path.is_file():
                try:
                    self.client_config_path.unlink()
                except OSError as exc:
                    AppLogger.warn("OAuth", f"清理 oauth_client.json 失败：{exc}")

    def _read_saved_session_file(self) -> Optional[OAuthSession]:
        if not self.session_path.is_file():
            return None
        try:
            raw = json.loads(self.session_path.read_text(encoding="utf-8-sig"))
        except Exception as exc:
            AppLogger.warn("OAuth", f"读取 oauth_session.json 失败：{exc}")
            return None
        if not isinstance(raw, dict):
            return None
        return self._session_from_dict(raw)

    @staticmethod
    def _session_from_dict(raw: dict[str, Any]) -> OAuthSession:
        return OAuthSession(
            email=str(raw.get("email") or "").strip(),
            display_name=str(raw.get("display_name") or "").strip(),
            access_token=str(raw.get("access_token") or "").strip(),
            refresh_token=str(raw.get("refresh_token") or "").strip(),
            expiry_timestamp=float(raw.get("expiry_timestamp") or 0.0),
            project_id=str(raw.get("project_id") or "").strip(),
            subscription_tier=str(raw.get("subscription_tier") or "").strip(),
            available_models=[
                str(m).strip()
                for m in (raw.get("available_models") or [])
                if isinstance(m, str) and m.strip()
            ],
            updated_at=float(raw.get("updated_at") or 0.0),
            client_id=str(raw.get("client_id") or "").strip(),
            client_secret=str(raw.get("client_secret") or "").strip(),
            auth_source=str(raw.get("auth_source") or "").strip(),
            api_backend=str(raw.get("api_backend") or "").strip(),
        )

    def _clear_optout_marker(self) -> None:
        if self._optout_path.is_file():
            try:
                self._optout_path.unlink()
            except OSError:
                pass

    def load_session(self, *, auto_discover: bool = True) -> OAuthSession:
        with self._lock:
            if self._cached_session is not None and self._cached_session.is_logged_in():
                if self.session_path.is_file() or (
                    self._cached_session.auth_source.startswith("local_")
                    and not self._optout_path.is_file()
                ):
                    return self._cached_session

            saved = self._read_saved_session_file()
            if saved is not None and saved.is_logged_in():
                self._cached_session = saved
                return saved

            if auto_discover and not self._optout_path.is_file():
                discovered = self.discover_local_session()
                if discovered is not None and discovered.is_logged_in():
                    self._cached_session = discovered
                    return discovered

            empty = OAuthSession()
            self._cached_session = empty
            return empty

    def save_session(self, session: OAuthSession) -> OAuthSession:
        with self._lock:
            self._clear_optout_marker()
            self.config_dir.mkdir(parents=True, exist_ok=True)
            session.updated_at = time.time()
            tmp_path = self.session_path.with_suffix(".json.tmp")
            payload = json.dumps(asdict(session), ensure_ascii=False, indent=2) + "\n"
            tmp_path.write_text(payload, encoding="utf-8")
            os.replace(tmp_path, self.session_path)
            self._cached_session = session
            return session

    def clear_session(self, *, suppress_local_auto: bool = True) -> None:
        with self._lock:
            self._cached_session = OAuthSession()
            if self.session_path.is_file():
                try:
                    self.session_path.unlink()
                except OSError as exc:
                    AppLogger.warn("OAuth", f"清理 oauth_session.json 失败：{exc}")
            if suppress_local_auto and self.discover_local_session() is not None:
                try:
                    self.config_dir.mkdir(parents=True, exist_ok=True)
                    self._optout_path.write_text("disabled\n", encoding="utf-8")
                except OSError:
                    pass

    def is_logged_in(self) -> bool:
        return self.load_session().is_logged_in()

    def discover_local_session(self) -> Optional[OAuthSession]:
        """检查当前环境公开标准的本地 Google OAuth 凭据（ADC 或 Gemini CLI 凭证）。"""
        adc_candidates: list[Path] = []
        env_gac = (os.environ.get("GOOGLE_APPLICATION_CREDENTIALS") or "").strip()
        if env_gac:
            adc_candidates.append(Path(env_gac))

        cloudsdk_dir = (os.environ.get("CLOUDSDK_CONFIG") or "").strip()
        if cloudsdk_dir:
            adc_candidates.append(Path(cloudsdk_dir) / "application_default_credentials.json")

        home_dir = _resolve_home_dir()
        if os.name == "nt":
            adc_candidates.append(_resolve_appdata_dir() / "gcloud" / "application_default_credentials.json")
        adc_candidates.append(home_dir / ".config" / "gcloud" / "application_default_credentials.json")

        seen_paths: set[str] = set()
        for path in adc_candidates:
            key = str(path)
            if key in seen_paths or not path.is_file():
                continue
            seen_paths.add(key)
            sess = self._try_load_adc_file(path)
            if sess is not None and sess.is_logged_in():
                return sess

        cli_creds_path = home_dir / ".gemini" / "oauth_creds.json"
        if cli_creds_path.is_file():
            sess = self._try_load_gemini_cli_creds(cli_creds_path, home_dir / ".gemini" / "google_accounts.json")
            if sess is not None and sess.is_logged_in():
                return sess

        return None

    def _try_load_adc_file(self, path: Path) -> Optional[OAuthSession]:
        try:
            raw = json.loads(path.read_text(encoding="utf-8-sig"))
        except Exception:
            return None
        if not isinstance(raw, dict):
            return None
        if str(raw.get("type") or "").strip() != "authorized_user":
            return None
        refresh_token = str(raw.get("refresh_token") or "").strip()
        client_id = str(raw.get("client_id") or "").strip()
        if not refresh_token or not client_id:
            return None
        return OAuthSession(
            email="本地 ADC 授权",
            display_name="Application Default Credentials",
            refresh_token=refresh_token,
            project_id=str(raw.get("quota_project_id") or "").strip(),
            subscription_tier="ADC",
            client_id=client_id,
            client_secret=str(raw.get("client_secret") or "").strip(),
            auth_source="local_adc",
            api_backend="generativelanguage",
        )

    def _try_load_gemini_cli_creds(
        self, creds_path: Path, accounts_path: Path
    ) -> Optional[OAuthSession]:
        try:
            raw = json.loads(creds_path.read_text(encoding="utf-8-sig"))
        except Exception:
            return None
        if not isinstance(raw, dict):
            return None

        access_token = str(raw.get("access_token") or "").strip()
        refresh_token = str(raw.get("refresh_token") or "").strip()
        expiry_ts = _parse_expiry_timestamp(raw.get("expiry_date") or raw.get("expiry_timestamp"))
        client_id = str(raw.get("client_id") or raw.get("clientId") or "").strip()
        client_secret = str(raw.get("client_secret") or raw.get("clientSecret") or "").strip()

        active_email = ""
        if accounts_path.is_file():
            try:
                acc = json.loads(accounts_path.read_text(encoding="utf-8-sig"))
                if isinstance(acc, dict) and isinstance(acc.get("active"), str):
                    active_email = acc["active"].strip()
            except Exception:
                pass

        now = time.time()
        has_valid_access = bool(access_token) and (expiry_ts <= 0 or expiry_ts > now + 60.0)
        fallback_client = self.get_client_config() if (not client_id and self.client_config_path.is_file()) else None
        can_refresh = bool(refresh_token and (client_id or (fallback_client is not None and fallback_client.is_valid())))
        if not has_valid_access and not can_refresh:
            return None

        return OAuthSession(
            email=active_email or "本地 Gemini CLI 授权",
            display_name="Gemini CLI OAuth",
            access_token=access_token,
            refresh_token=refresh_token,
            expiry_timestamp=expiry_ts,
            subscription_tier="Local CLI",
            client_id=client_id,
            client_secret=client_secret,
            auth_source="local_cli",
            api_backend="generativelanguage",
        )

    def discover_and_import_local_session(self) -> OAuthSession:
        """强制扫描本地已有 OAuth 授权，若存在则验证或直接导入保存到本地会话。"""
        with self._lock:
            self._clear_optout_marker()
            discovered = self.discover_local_session()
            if discovered is None or not discovered.is_logged_in():
                raise OAuthError(
                    "未在本机发现可用的 OAuth 授权（已检查 gcloud ADC 与本地 CLI 凭证），请下方自主授权登录"
                )
            self._cached_session = discovered

        client_cfg = self._resolve_client_for_session(discovered)
        if discovered.refresh_token and client_cfg is not None:
            try:
                token_data = self._refresh_token(discovered.refresh_token, client_cfg)
                new_access = str(token_data.get("access_token") or "").strip()
                expires_in = int(token_data.get("expires_in") or 3600)
                new_refresh = str(token_data.get("refresh_token") or "").strip() or discovered.refresh_token
                return self._build_and_persist_session(
                    access_token=new_access,
                    refresh_token=new_refresh,
                    expires_in=expires_in,
                    client_config=client_cfg,
                    auth_source=discovered.auth_source or "local_adc",
                    project_hint=discovered.project_id,
                    email_hint=discovered.email,
                    display_hint=discovered.display_name,
                    tier_hint=discovered.subscription_tier,
                )
            except OAuthError:
                if discovered.access_token and (
                    discovered.expiry_timestamp <= 0 or discovered.expiry_timestamp > time.time() + 60.0
                ):
                    return self.save_session(discovered)
                raise

        return self.save_session(discovered)

    def import_session_from_json(self, raw_input: str) -> OAuthSession:
        """解析本地导出的 OAuth / ADC JSON 字符串并激活登录。"""
        text = (raw_input or "").strip()
        if not text:
            raise OAuthError("请提供或粘贴凭据内容")
        try:
            parsed = json.loads(text)
        except Exception as exc:
            raise OAuthError(f"凭据 JSON 格式错误：{exc}") from exc

        if isinstance(parsed, list):
            parsed = next((item for item in parsed if isinstance(item, dict)), None)
        if not isinstance(parsed, dict):
            raise OAuthError("凭据 JSON 必须是对象格式")

        refresh_token = str(parsed.get("refresh_token") or parsed.get("refreshToken") or "").strip()
        access_token = str(parsed.get("access_token") or parsed.get("accessToken") or "").strip()
        client_id = str(parsed.get("client_id") or parsed.get("clientId") or "").strip()
        client_secret = str(parsed.get("client_secret") or parsed.get("clientSecret") or "").strip()
        project_id = str(
            parsed.get("quota_project_id") or parsed.get("project_id") or ""
        ).strip()
        expiry_ts = _parse_expiry_timestamp(parsed.get("expiry_timestamp") or parsed.get("expiry_date"))

        cfg = OAuthClientConfig(client_id=client_id, client_secret=client_secret) if client_id else self.get_client_config()
        if refresh_token and cfg is not None and cfg.is_valid():
            token_data = self._refresh_token(refresh_token, cfg)
            new_access = str(token_data.get("access_token") or "").strip()
            expires_in = int(token_data.get("expires_in") or 3600)
            new_refresh = str(token_data.get("refresh_token") or "").strip() or refresh_token
            return self._build_and_persist_session(
                access_token=new_access,
                refresh_token=new_refresh,
                expires_in=expires_in,
                client_config=cfg,
                auth_source="manual_import",
                project_hint=project_id,
                email_hint=str(parsed.get("email") or "").strip(),
                display_hint=str(parsed.get("display_name") or "").strip(),
            )

        if access_token and (expiry_ts <= 0 or expiry_ts > time.time() + 30.0):
            email, display_name = self.fetch_user_info(access_token)
            models, backend, resolved_pid, tier = self._probe_backend_and_models(access_token, project_id)
            session = OAuthSession(
                email=str(parsed.get("email") or "").strip() or email,
                display_name=str(parsed.get("display_name") or "").strip() or display_name,
                access_token=access_token,
                refresh_token=refresh_token,
                expiry_timestamp=expiry_ts,
                project_id=resolved_pid,
                subscription_tier=tier or "Imported",
                available_models=models,
                client_id=client_id,
                client_secret=client_secret,
                auth_source="manual_import",
                api_backend=backend,
            )
            return self.save_session(session)

        if refresh_token:
            raise OAuthError(
                "检测到 Refresh Token，但缺少关联的 OAuth Client ID。请先在「OAuth 授权」页配置客户端，或粘贴包含 client_id/client_secret 的完整 ADC JSON。"
            )
        raise OAuthError("JSON 中未包含有效的 refresh_token 或未过期的 access_token")

    def import_session_from_file(self, file_path: str | Path) -> OAuthSession:
        path = Path(file_path)
        if not path.is_file():
            raise OAuthError("所选凭据文件不存在")
        return self.import_session_from_json(path.read_text(encoding="utf-8-sig"))

    def start_loopback_flow(
        self,
        *,
        client_config: Optional[OAuthClientConfig] = None,
        enable_listener: bool = True,
        on_callback_received: Optional[Callable[[], None]] = None,
    ) -> OAuthLoopbackFlow:
        cfg = self.get_client_config(client_config)
        if cfg is None or not cfg.is_valid():
            raise OAuthError("请先导入 Google client_secret.json 或配置 OAuth Client ID")
        return OAuthLoopbackFlow(
            client_id=cfg.client_id,
            client_secret=cfg.client_secret,
            enable_listener=enable_listener,
            on_callback_received=on_callback_received,
        )

    def complete_flow_with_code(
        self,
        flow: OAuthLoopbackFlow,
        raw_input: str,
        *,
        from_manual: bool = False,
    ) -> OAuthSession:
        if from_manual:
            code, extracted_state = parse_manual_oauth_input(raw_input)
            if extracted_state is not None and extracted_state != flow.state:
                raise OAuthError("OAuth state 校验不匹配（CSRF 防护），请重新生成授权链接")
        else:
            code = raw_input.strip()
        return self.exchange_code_for_session(
            code,
            flow.redirect_uri,
            client_config=OAuthClientConfig(flow.client_id, flow.client_secret),
            code_verifier=flow.code_verifier,
        )

    def exchange_code_for_session(
        self,
        code: str,
        redirect_uri: str,
        *,
        client_config: Optional[OAuthClientConfig] = None,
        code_verifier: Optional[str] = None,
    ) -> OAuthSession:
        cfg = self.get_client_config(client_config)
        if cfg is None or not cfg.is_valid():
            raise OAuthError("缺少 OAuth Client 配置，请先导入或填写 Client ID")
        token_data = self._exchange_code(
            code,
            redirect_uri,
            cfg,
            code_verifier=code_verifier,
        )
        refresh_token = str(token_data.get("refresh_token") or "").strip()
        access_token = str(token_data.get("access_token") or "").strip()
        expires_in = int(token_data.get("expires_in") or 3600)
        return self._build_and_persist_session(
            access_token=access_token,
            refresh_token=refresh_token,
            expires_in=expires_in,
            client_config=cfg,
            auth_source="oauth_login",
        )

    def login_with_refresh_token(
        self,
        raw_input: str,
        *,
        client_config: Optional[OAuthClientConfig] = None,
    ) -> OAuthSession:
        text = (raw_input or "").strip()
        if text.startswith(("{", "[")):
            return self.import_session_from_json(text)

        refresh_token = extract_refresh_token_from_input(text)
        cfg = self.get_client_config(client_config)
        if cfg is None or not cfg.is_valid():
            raise OAuthError(
                "仅粘贴 1//... Refresh Token 时需先在「OAuth 授权」页配置或导入你的 Client ID/Secret；也可直接粘贴包含 client_id 的 ADC JSON。"
            )
        token_data = self._refresh_token(refresh_token, cfg)
        access_token = str(token_data.get("access_token") or "").strip()
        expires_in = int(token_data.get("expires_in") or 3600)
        new_refresh = str(token_data.get("refresh_token") or "").strip() or refresh_token
        return self._build_and_persist_session(
            access_token=access_token,
            refresh_token=new_refresh,
            expires_in=expires_in,
            client_config=cfg,
            auth_source="manual_import",
        )

    def _resolve_client_for_session(self, session: OAuthSession) -> Optional[OAuthClientConfig]:
        if (session.client_id or "").strip():
            return OAuthClientConfig(
                client_id=session.client_id.strip(),
                client_secret=session.client_secret.strip(),
            )
        return self.get_client_config()

    def _probe_backend_and_models(
        self, access_token: str, project_hint: str = ""
    ) -> tuple[list[str], str, str, str]:
        gemini_models = self.fetch_gemini_api_models(access_token, project_hint or None)
        if gemini_models:
            return gemini_models, "generativelanguage", project_hint, ""

        project_id, tier = self.fetch_project_and_tier(access_token)
        if project_id:
            v1_models = self.fetch_v1internal_models(access_token, project_id)
            if v1_models:
                return v1_models, "v1internal", project_id, tier or ""
            return [], "v1internal", project_id, tier or ""

        return [], "generativelanguage", project_hint, ""

    def _build_and_persist_session(
        self,
        *,
        access_token: str,
        refresh_token: str,
        expires_in: int,
        client_config: OAuthClientConfig,
        auth_source: str,
        project_hint: str = "",
        email_hint: str = "",
        display_hint: str = "",
        tier_hint: str = "",
    ) -> OAuthSession:
        email, display_name = self.fetch_user_info(access_token)
        if email == "oauth-user@google" and email_hint:
            email = email_hint
        if display_name == "Google OAuth 账号" and display_hint:
            display_name = display_hint

        models, backend, resolved_project, tier = self._probe_backend_and_models(access_token, project_hint)

        session = OAuthSession(
            email=email,
            display_name=display_name,
            access_token=access_token,
            refresh_token=refresh_token,
            expiry_timestamp=time.time() + max(60, expires_in),
            project_id=resolved_project,
            subscription_tier=tier or tier_hint,
            available_models=models,
            client_id=client_config.client_id,
            client_secret=client_config.client_secret,
            auth_source=auth_source,
            api_backend=backend,
        )
        self.save_session(session)
        AppLogger.info(
            "OAuth",
            f"OAuth 账号就绪 ({auth_source}): {mask_email(email)} (backend={backend}, models={len(models)})",
        )
        return session

    def ensure_fresh_access_token(self, *, force_refresh: bool = False) -> OAuthSession:
        with self._lock:
            session = self.load_session()
            if not session.is_logged_in():
                raise OAuthError("尚未获取到可用 Google OAuth 授权", status_code=401)

            now = time.time()
            if (
                not force_refresh
                and session.access_token
                and (session.expiry_timestamp <= 0 or session.expiry_timestamp > now + TOKEN_REFRESH_SKEW_SECONDS)
            ):
                return session

            if not (session.refresh_token or "").strip():
                if not force_refresh and session.access_token and session.expiry_timestamp > now + 30.0:
                    return session
                raise OAuthError("本地 Access Token 已过期且缺少 Refresh Token，请重新授权登录", status_code=401)

            cfg = self._resolve_client_for_session(session)
            if cfg is None or not cfg.is_valid():
                raise OAuthError(
                    "当前会话缺少 OAuth Client 配置，无法自动刷新 Token；请重新授权或导入完整凭据 JSON",
                    status_code=401,
                )

            AppLogger.info("OAuth", f"正在刷新 OAuth Access Token：{mask_email(session.email)}")
            token_data = self._refresh_token(session.refresh_token, cfg)
            new_access = str(token_data.get("access_token") or "").strip()
            if not new_access:
                raise OAuthError("刷新 OAuth Token 失败：未返回 access_token", status_code=401)

            expires_in = int(token_data.get("expires_in") or 3600)
            new_refresh = str(token_data.get("refresh_token") or "").strip() or session.refresh_token

            updated = OAuthSession(
                email=session.email,
                display_name=session.display_name,
                access_token=new_access,
                refresh_token=new_refresh,
                expiry_timestamp=time.time() + max(60, expires_in),
                project_id=session.project_id,
                subscription_tier=session.subscription_tier,
                available_models=list(session.available_models),
                client_id=cfg.client_id,
                client_secret=cfg.client_secret,
                auth_source=session.auth_source or "oauth_login",
                api_backend=session.api_backend or "generativelanguage",
            )
            return self.save_session(updated)

    def refresh_models_for_current_session(self) -> list[str]:
        session = self.ensure_fresh_access_token()
        models, backend, resolved_project, tier = self._probe_backend_and_models(
            session.access_token, session.project_id
        )
        with self._lock:
            latest = self.load_session()
            if latest.is_logged_in():
                if resolved_project:
                    latest.project_id = resolved_project
                if tier:
                    latest.subscription_tier = tier
                if backend:
                    latest.api_backend = backend
                if models:
                    latest.available_models = models
                self.save_session(latest)
        return models

    @staticmethod
    def _exchange_code(
        code: str,
        redirect_uri: str,
        client_config: OAuthClientConfig,
        *,
        code_verifier: Optional[str] = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "client_id": client_config.client_id,
            "code": code,
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
        }
        if client_config.client_secret:
            payload["client_secret"] = client_config.client_secret
        if code_verifier:
            payload["code_verifier"] = code_verifier

        try:
            resp = requests.post(
                GOOGLE_TOKEN_URL,
                data=payload,
                headers={"User-Agent": HUB_USER_AGENT},
                timeout=(10, 30),
            )
        except requests.RequestException as exc:
            raise OAuthError(f"连接 Google 授权服务器失败：{exc}，请检查网络或代理设置") from exc

        if not resp.ok:
            raise OAuthError(
                f"授权码兑换 Token 失败 (HTTP {resp.status_code})：{OAuthAuthService._extract_oauth_error_detail(resp)}",
                status_code=resp.status_code,
            )
        try:
            data = resp.json()
        except Exception as exc:
            raise OAuthError(f"解析 Google Token 响应失败：{exc}") from exc
        if not isinstance(data, dict) or not data.get("access_token"):
            raise OAuthError("Google Token 响应缺少 access_token")
        return data

    @staticmethod
    def _refresh_token(refresh_token: str, client_config: OAuthClientConfig) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "client_id": client_config.client_id,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        }
        if client_config.client_secret:
            payload["client_secret"] = client_config.client_secret

        try:
            resp = requests.post(
                GOOGLE_TOKEN_URL,
                data=payload,
                headers={"User-Agent": HUB_USER_AGENT},
                timeout=(10, 30),
            )
        except requests.RequestException as exc:
            raise OAuthError(f"刷新 OAuth Token 网络失败：{exc}，请检查网络或代理设置") from exc

        if not resp.ok:
            detail = OAuthAuthService._extract_oauth_error_detail(resp)
            if "invalid_grant" in detail.lower():
                raise OAuthError(
                    "OAuth 登录凭据已过期或被撤销，请在设置中重新登录 Google 账号",
                    status_code=resp.status_code,
                )
            raise OAuthError(
                f"刷新 OAuth Token 失败 (HTTP {resp.status_code})：{detail}",
                status_code=resp.status_code,
            )
        try:
            data = resp.json()
        except Exception as exc:
            raise OAuthError(f"解析刷新 Token 响应失败：{exc}") from exc
        if not isinstance(data, dict) or not data.get("access_token"):
            raise OAuthError("刷新响应缺少 access_token")
        return data

    @staticmethod
    def fetch_user_info(access_token: str) -> tuple[str, str]:
        try:
            resp = requests.get(
                GOOGLE_USERINFO_URL,
                headers={"Authorization": f"Bearer {access_token}"},
                timeout=(10, 15),
            )
        except requests.RequestException:
            return "oauth-user@google", "Google OAuth 账号"

        if not resp.ok:
            return "oauth-user@google", "Google OAuth 账号"
        try:
            data = resp.json()
        except Exception:
            return "oauth-user@google", "Google OAuth 账号"

        email = str(data.get("email") or "").strip() or "oauth-user@google"
        name = str(data.get("name") or "").strip()
        if not name:
            given = str(data.get("given_name") or "").strip()
            family = str(data.get("family_name") or "").strip()
            name = f"{given} {family}".strip() or "Google OAuth 账号"
        return email, name

    @staticmethod
    def fetch_gemini_api_models(
        access_token: str, project_id: Optional[str] = None
    ) -> list[str]:
        headers: dict[str, str] = {"Authorization": f"Bearer {access_token}"}
        if project_id:
            headers["x-goog-user-project"] = project_id
        try:
            resp = requests.get(
                f"{GEMINI_API_BASE_URL}/models",
                params={"pageSize": 100},
                headers=headers,
                timeout=(8, 15),
            )
        except requests.RequestException:
            return []
        if not resp.ok:
            return []
        try:
            data = resp.json()
        except Exception:
            return []
        raw_models = data.get("models") if isinstance(data, dict) else None
        if not isinstance(raw_models, list):
            return []

        result: list[str] = []
        for item in raw_models:
            if not isinstance(item, dict):
                continue
            methods = item.get("supportedGenerationMethods") or []
            if "generateContent" not in methods:
                continue
            name = str(item.get("name") or "").strip()
            if name.startswith("models/"):
                name = name[len("models/") :]
            if name and "embedding" not in name.lower() and name not in result:
                result.append(name)
        return result

    @staticmethod
    def fetch_project_and_tier(access_token: str) -> tuple[Optional[str], Optional[str]]:
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
            "User-Agent": HUB_USER_AGENT,
        }
        body = {"metadata": {"ideType": "ANTIGRAVITY"}}

        for base_url in V1_INTERNAL_METADATA_BASE_URLS:
            url = f"{base_url}:loadCodeAssist"
            try:
                resp = requests.post(url, json=body, headers=headers, timeout=(8, 12))
            except requests.RequestException:
                continue
            if not resp.ok:
                continue
            try:
                data = resp.json()
            except Exception:
                continue
            if not isinstance(data, dict):
                continue

            project_id = data.get("cloudaicompanionProject")
            pid = str(project_id).strip() if isinstance(project_id, str) and project_id.strip() else None
            tier = OAuthAuthService._extract_subscription_tier(data)
            return pid, tier

        return None, None

    @staticmethod
    def _extract_subscription_tier(data: dict[str, Any]) -> str:
        raw_tier = ""
        for field_name in ("paidTier", "currentTier"):
            tier_obj = data.get(field_name)
            if isinstance(tier_obj, dict):
                raw_tier = str(tier_obj.get("id") or tier_obj.get("name") or "").strip()
                if raw_tier:
                    break
        if not raw_tier and isinstance(data.get("allowedTiers"), list):
            for tier_obj in data["allowedTiers"]:
                if isinstance(tier_obj, dict) and (
                    tier_obj.get("id") == "free-tier" or tier_obj.get("isDefault") is True
                ):
                    raw_tier = str(tier_obj.get("id") or tier_obj.get("name") or "").strip()
                    break

        low = raw_tier.lower()
        if "ultra" in low:
            return "ULTRA"
        if "pro" in low:
            return "PRO"
        return "FREE"

    @staticmethod
    def fetch_v1internal_models(access_token: str, project_id: Optional[str] = None) -> list[str]:
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
            "User-Agent": HUB_USER_AGENT,
        }
        payload: dict[str, Any] = {"project": project_id} if project_id else {}

        for base_url in V1_INTERNAL_METADATA_BASE_URLS:
            url = f"{base_url}:fetchAvailableModels"
            try:
                resp = requests.post(url, json=payload, headers=headers, timeout=(8, 15))
            except requests.RequestException:
                continue
            if not resp.ok:
                continue
            try:
                data = resp.json()
            except Exception:
                continue
            if not isinstance(data, dict):
                continue

            models_map = data.get("models")
            if not isinstance(models_map, dict):
                continue

            recommended_order: list[str] = []
            seen: set[str] = set()
            for sort_item in data.get("agentModelSorts") or []:
                if not isinstance(sort_item, dict):
                    continue
                for grp in sort_item.get("groups") or []:
                    if not isinstance(grp, dict):
                        continue
                    for mid in grp.get("modelIds") or []:
                        if isinstance(mid, str) and mid.strip() and mid not in seen:
                            seen.add(mid)
                            recommended_order.append(mid.strip())

            filtered: list[str] = []
            for mid in [*recommended_order, *sorted(models_map.keys())]:
                info = models_map.get(mid)
                if not isinstance(info, dict):
                    continue
                if info.get("disabled") is True or info.get("isInternal") is True:
                    continue
                low = mid.lower()
                if low.startswith(("tab_", "chat_")) or low.endswith("-image"):
                    continue
                if mid not in filtered:
                    filtered.append(mid)
            if filtered:
                return filtered
        return []

    @staticmethod
    def fetch_available_models(access_token: str, project_id: Optional[str] = None) -> list[str]:
        models = OAuthAuthService.fetch_gemini_api_models(access_token, project_id)
        if models:
            return models
        return OAuthAuthService.fetch_v1internal_models(access_token, project_id)

    @staticmethod
    def _extract_oauth_error_detail(resp: requests.Response) -> str:
        try:
            body = resp.json()
            if isinstance(body, dict):
                err = body.get("error_description") or body.get("error")
                if isinstance(err, str) and err.strip():
                    return err.strip()
        except Exception:
            pass
        text = (resp.text or "").strip()
        return text[:160] if len(text) > 160 else (text or "未知错误")
