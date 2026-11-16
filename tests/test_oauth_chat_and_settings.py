"""OAuth 本地授权优先读取、用户自建客户端 PKCE 授权、聊天服务与设置联动测试。"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch
import urllib.parse

if os.name != "nt" and not os.environ.get("DISPLAY"):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication
import requests

from models.pet_settings import ChatAuthMode, DEFAULT_OAUTH_MODEL, PetSettings
from models.pet_state import PetState
from services.app_follow_service import AppFollowService
from services.gemini_chat_service import GeminiChatService
from services.oauth_auth_service import (
    OAuthAuthService,
    OAuthClientConfig,
    OAuthError,
    OAuthLoopbackFlow,
    OAuthSession,
    TOKEN_REFRESH_SKEW_SECONDS,
    extract_refresh_token_from_input,
    mask_email,
    parse_manual_oauth_input,
    parse_oauth_client_json,
)
from services.settings_service import SettingsService
from views.oauth_login_dialog import OAuthLoginDialog
from views.settings_window import SettingsWindow


class _DummyResp:
    def __init__(
        self,
        status_code: int = 200,
        json_data: object = None,
        text: str = "",
        lines: list[str] | None = None,
    ) -> None:
        self.status_code = status_code
        self._json_data = json_data
        self.text = text
        self._lines = lines or []

    @property
    def ok(self) -> bool:
        return 200 <= self.status_code < 300

    def json(self) -> object:
        if self._json_data is not None:
            return self._json_data
        return json.loads(self.text)

    def iter_lines(self, decode_unicode: bool = True):
        del decode_unicode
        for ln in self._lines:
            yield ln

    def close(self) -> None:
        return None


def _isolated_env(home_dir: str, appdata_dir: str) -> dict[str, str]:
    return {
        "USERPROFILE": home_dir,
        "HOME": home_dir,
        "APPDATA": appdata_dir,
        "GOOGLE_APPLICATION_CREDENTIALS": "",
        "CLOUDSDK_CONFIG": "",
        "GEMINI_DESKTOP_PET_OAUTH_CLIENT_ID": "",
        "GEMINI_DESKTOP_PET_OAUTH_CLIENT_SECRET": "",
        "GOOGLE_OAUTH_CLIENT_ID": "",
        "GOOGLE_OAUTH_CLIENT_SECRET": "",
    }


class TestOAuthAndChat(unittest.TestCase):
    @staticmethod
    def _ensure_app() -> QApplication:
        app = QApplication.instance() or QApplication([])
        app.setQuitOnLastWindowClosed(False)
        return app

    def test_no_hardcoded_oauth_client_secrets_and_parse_client_json(self) -> None:
        src = (Path(__file__).resolve().parents[1] / "services" / "oauth_auth_service.py").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("PERSONAL_CLIENT_ID", src)
        self.assertNotIn("PERSONAL_CLIENT_SECRET", src)
        self.assertNotIn("apps.googleusercontent.com", src)

        installed_cfg = parse_oauth_client_json(
            json.dumps({"installed": {"client_id": "demo-installed-id", "client_secret": "demo-secret"}})
        )
        self.assertEqual(installed_cfg, OAuthClientConfig("demo-installed-id", "demo-secret"))

        web_cfg = parse_oauth_client_json(
            json.dumps({"web": {"client_id": "demo-web-id"}})
        )
        self.assertEqual(web_cfg, OAuthClientConfig("demo-web-id", ""))

        with self.assertRaises(OAuthError):
            parse_oauth_client_json('{"installed": {}}')

    def test_manual_oauth_input_and_refresh_token_parsing(self) -> None:
        code, state = parse_manual_oauth_input(
            "http://localhost:8045/oauth-callback?state=st-123&code=4%2F0A_sample_code&scope=email"
        )
        self.assertEqual(code, "4/0A_sample_code")
        self.assertEqual(state, "st-123")

        code2, state2 = parse_manual_oauth_input(
            "localhost:9001/oauth-callback?code=4%2F0A_schemeless&state=st-999"
        )
        self.assertEqual(code2, "4/0A_schemeless")
        self.assertEqual(state2, "st-999")

        code3, state3 = parse_manual_oauth_input("  4%2F0A_encoded_only  ")
        self.assertEqual(code3, "4/0A_encoded_only")
        self.assertIsNone(state3)

        with self.assertRaises(OAuthError):
            parse_manual_oauth_input("http://localhost:8045/oauth-callback?error=access_denied")
        with self.assertRaises(OAuthError):
            parse_manual_oauth_input("   ")

        self.assertEqual(
            extract_refresh_token_from_input("1//0gTestRefreshToken_ABC123"),
            "1//0gTestRefreshToken_ABC123",
        )
        self.assertEqual(
            extract_refresh_token_from_input('[{"refresh_token": "1//0gArrayToken_XYZ"}]'),
            "1//0gArrayToken_XYZ",
        )
        with self.assertRaises(OAuthError):
            extract_refresh_token_from_input("not-a-valid-token")

    def test_loopback_flow_pkce_and_state_validation(self) -> None:
        callback_hits = 0

        def _on_hit() -> None:
            nonlocal callback_hits
            callback_hits += 1

        flow_bad = OAuthLoopbackFlow(
            client_id="demo-desktop-client",
            enable_listener=True,
            on_callback_received=_on_hit,
        )
        try:
            parsed_url = urllib.parse.urlparse(flow_bad.auth_url)
            qs = urllib.parse.parse_qs(parsed_url.query)
            self.assertEqual(qs["client_id"], ["demo-desktop-client"])
            self.assertEqual(qs["response_type"], ["code"])
            self.assertEqual(qs["access_type"], ["offline"])
            self.assertEqual(qs["prompt"], ["consent"])
            self.assertEqual(qs["state"], [flow_bad.state])
            self.assertEqual(qs["code_challenge_method"], ["S256"])
            expected_challenge = (
                base64.urlsafe_b64encode(hashlib.sha256(flow_bad.code_verifier.encode("ascii")).digest())
                .rstrip(b"=")
                .decode("ascii")
            )
            self.assertEqual(qs["code_challenge"], [expected_challenge])

            bad_resp = requests.get(
                flow_bad.redirect_uri,
                params={"code": "sample-code", "state": "wrong-state"},
                timeout=3,
            )
            self.assertEqual(bad_resp.status_code, 400)
            self.assertEqual(callback_hits, 1)
            with self.assertRaises(OAuthError):
                flow_bad.wait_for_code(timeout_s=1.0)
        finally:
            flow_bad.cancel()

        flow_ok = OAuthLoopbackFlow(client_id="demo-desktop-client", enable_listener=True)
        try:
            ok_resp = requests.get(
                flow_ok.redirect_uri,
                params={"code": "4/0A_loopback_ok", "state": flow_ok.state},
                timeout=3,
            )
            self.assertEqual(ok_resp.status_code, 200)
            self.assertEqual(flow_ok.wait_for_code(timeout_s=2.0), "4/0A_loopback_ok")
        finally:
            flow_ok.cancel()

    def test_local_oauth_discovery_priority_and_logout_suppression(self) -> None:
        with (
            tempfile.TemporaryDirectory() as tmp_cfg,
            tempfile.TemporaryDirectory() as tmp_home,
            tempfile.TemporaryDirectory() as tmp_appdata,
        ):
            with patch.dict(os.environ, _isolated_env(tmp_home, tmp_appdata)):
                svc = OAuthAuthService(tmp_cfg)
                self.assertFalse(svc.is_logged_in())

                # 1. 优先读取标准 gcloud ADC 授权
                adc_path = Path(tmp_appdata) / "gcloud" / "application_default_credentials.json"
                adc_path.parent.mkdir(parents=True, exist_ok=True)
                adc_path.write_text(
                    json.dumps(
                        {
                            "type": "authorized_user",
                            "client_id": "adc-client-id",
                            "client_secret": "adc-client-secret",
                            "refresh_token": "1//adc-refresh-token",
                            "quota_project_id": "adc-proj-01",
                        }
                    ),
                    encoding="utf-8",
                )
                session = svc.load_session()
                self.assertTrue(session.is_logged_in())
                self.assertEqual(session.auth_source, "local_adc")
                self.assertEqual(session.project_id, "adc-proj-01")
                self.assertEqual(session.client_id, "adc-client-id")

                # 2. 用户显式退出登录后不立即自动重新挂载，直到主动触发重新读取
                svc.clear_session()
                self.assertFalse(svc.is_logged_in())
                self.assertFalse(svc.session_path.exists())

                def _fake_post(url: str, **kwargs):
                    if "oauth2.googleapis.com/token" in url:
                        data = kwargs.get("data") or {}
                        self.assertEqual(data.get("client_id"), "adc-client-id")
                        return _DummyResp(200, {"access_token": "acc_from_adc", "expires_in": 3600})
                    raise AssertionError(f"Unexpected POST url: {url}")

                def _fake_get(url: str, **kwargs):
                    del kwargs
                    if "userinfo" in url:
                        return _DummyResp(200, {"email": "adc@example.com", "name": "ADC User"})
                    if "generativelanguage.googleapis.com/v1beta/models" in url:
                        return _DummyResp(
                            200,
                            {
                                "models": [
                                    {"name": "models/gemini-3.8-flash", "supportedGenerationMethods": ["generateContent"]},
                                    {"name": "models/text-embedding-004", "supportedGenerationMethods": ["embedContent"]},
                                ]
                            },
                        )
                    raise AssertionError(f"Unexpected GET url: {url}")

                with (
                    patch("services.oauth_auth_service.requests.post", side_effect=_fake_post),
                    patch("services.oauth_auth_service.requests.get", side_effect=_fake_get),
                ):
                    imported = svc.discover_and_import_local_session()
                    self.assertEqual(imported.email, "adc@example.com")
                    self.assertEqual(imported.api_backend, "generativelanguage")
                    self.assertEqual(imported.available_models, ["gemini-3.8-flash"])
                    self.assertTrue(svc.session_path.is_file())

                # 3. 无 ADC 时回退检测标准 Gemini CLI oauth_creds.json
                svc.clear_session()
                adc_path.unlink()
                cli_dir = Path(tmp_home) / ".gemini"
                cli_dir.mkdir(parents=True, exist_ok=True)
                (cli_dir / "google_accounts.json").write_text(
                    json.dumps({"active": "cli@example.com"}),
                    encoding="utf-8",
                )
                (cli_dir / "oauth_creds.json").write_text(
                    json.dumps(
                        {
                            "access_token": "cli_valid_access_token",
                            "expiry_date": (time.time() + 1800) * 1000,
                        }
                    ),
                    encoding="utf-8",
                )
                svc._optout_path.unlink(missing_ok=True)
                cli_session = svc.load_session()
                self.assertTrue(cli_session.is_logged_in())
                self.assertEqual(cli_session.auth_source, "local_cli")
                self.assertEqual(cli_session.email, "cli@example.com")

    def test_oauth_service_exchange_refresh_persistence_and_masking(self) -> None:
        self.assertEqual(mask_email("alice@example.com"), "al***@example.com")
        self.assertEqual(mask_email("ab@example.com"), "a*@example.com")

        with (
            tempfile.TemporaryDirectory() as tmp,
            tempfile.TemporaryDirectory() as tmp_home,
            tempfile.TemporaryDirectory() as tmp_appdata,
        ):
            with patch.dict(os.environ, _isolated_env(tmp_home, tmp_appdata)):
                svc = OAuthAuthService(tmp)
                svc.save_client_config(OAuthClientConfig("user-client-id", "user-client-secret"))
                self.assertFalse(svc.is_logged_in())

                def _fake_post(url: str, **kwargs):
                    if "oauth2.googleapis.com/token" in url:
                        data = kwargs.get("data") or {}
                        self.assertEqual(data.get("client_id"), "user-client-id")
                        if data.get("grant_type") == "authorization_code":
                            self.assertEqual(data.get("code_verifier"), "verifier-123")
                            return _DummyResp(
                                200,
                                {
                                    "access_token": "acc_initial",
                                    "refresh_token": "1//dummy_refresh_token",
                                    "expires_in": 3600,
                                },
                            )
                        return _DummyResp(200, {"access_token": "acc_refreshed", "expires_in": 3600})
                    raise AssertionError(f"Unexpected POST url: {url}")

                def _fake_get(url: str, **kwargs):
                    del kwargs
                    if "userinfo" in url:
                        return _DummyResp(200, {"email": "demo@example.com", "name": "Demo User"})
                    if "generativelanguage.googleapis.com/v1beta/models" in url:
                        return _DummyResp(
                            200,
                            {
                                "models": [
                                    {"name": "models/gemini-3.8-flash", "supportedGenerationMethods": ["generateContent"]},
                                    {"name": "models/gemini-4-argon", "supportedGenerationMethods": ["generateContent"]},
                                ]
                            },
                        )
                    raise AssertionError(f"Unexpected GET url: {url}")

                with (
                    patch("services.oauth_auth_service.requests.post", side_effect=_fake_post),
                    patch("services.oauth_auth_service.requests.get", side_effect=_fake_get),
                ):
                    session = svc.exchange_code_for_session(
                        "4/0A_code",
                        "http://localhost:9999/oauth-callback",
                        code_verifier="verifier-123",
                    )
                    self.assertEqual(session.email, "demo@example.com")
                    self.assertEqual(session.display_name, "Demo User")
                    self.assertEqual(session.api_backend, "generativelanguage")
                    self.assertEqual(session.available_models, ["gemini-3.8-flash", "gemini-4-argon"])
                    self.assertTrue(svc.session_path.is_file())

                    # 未临近过期时直接复用本地缓存 token，不触发网络刷新
                    fresh = svc.ensure_fresh_access_token()
                    self.assertEqual(fresh.access_token, "acc_initial")

                    # 模拟即将过期（进入 900s 缓冲期）触发自动刷新
                    session.expiry_timestamp = time.time() + TOKEN_REFRESH_SKEW_SECONDS - 10
                    svc.save_session(session)
                    reloaded_svc = OAuthAuthService(tmp)
                    refreshed = reloaded_svc.ensure_fresh_access_token()
                    self.assertEqual(refreshed.access_token, "acc_refreshed")
                    self.assertEqual(refreshed.refresh_token, "1//dummy_refresh_token")

                    reloaded_svc.clear_session()
                    self.assertFalse(reloaded_svc.is_logged_in())
                    self.assertFalse(reloaded_svc.session_path.exists())

    def test_chat_service_oauth_mode_multiturn_retry_and_fallback(self) -> None:
        with (
            tempfile.TemporaryDirectory() as tmp,
            tempfile.TemporaryDirectory() as tmp_home,
            tempfile.TemporaryDirectory() as tmp_appdata,
        ):
            with patch.dict(os.environ, _isolated_env(tmp_home, tmp_appdata)):
                oauth_svc = OAuthAuthService(tmp)
                oauth_svc.save_session(
                    OAuthSession(
                        email="demo@example.com",
                        access_token="expired_acc",
                        refresh_token="1//dummy_refresh",
                        expiry_timestamp=time.time() + 3600,
                        project_id="demo-proj",
                        client_id="demo-cid",
                        auth_source="oauth_login",
                        api_backend="generativelanguage",
                    )
                )
                chat = GeminiChatService(oauth_service=oauth_svc)
                settings = PetSettings(
                    chat_auth_mode=ChatAuthMode.OAUTH,
                    oauth_model="gemini-3.8-flash",
                )

                # 本地快捷指令不走网络请求
                local = chat.ask("摸摸头", settings)
                self.assertEqual(local.suggested_state, PetState.PAT)

                captured_payloads: list[dict] = []
                call_count = 0

                def _fake_chat_post(url: str, **kwargs):
                    nonlocal call_count
                    call_count += 1
                    captured_payloads.append(
                        {"url": url, "json": kwargs.get("json"), "headers": kwargs.get("headers")}
                    )
                    # 第 1 次请求模拟 401，验证自动 force_refresh 后重试
                    if call_count == 1:
                        return _DummyResp(401, text='{"error":{"message":"invalid_token"}}')
                    # 第 2 次请求返回标准 Gemini REST SSE
                    if call_count == 2:
                        self.assertIn("generativelanguage.googleapis.com/v1beta/models/gemini-3.8-flash:streamGenerateContent", url)
                        return _DummyResp(
                            200,
                            lines=[
                                'data: {"candidates":[{"content":{"parts":[{"text":"思考过程","thought":true}]}}]}',
                                'data: {"candidates":[{"content":{"parts":[{"text":"你好呀"},{"text":"主人喵～✦"}]}}]}',
                                "data: [DONE]",
                            ],
                        )
                    # 第 3 次请求（第二轮对话）验证携带多轮 contents 上下文
                    return _DummyResp(
                        200,
                        lines=[
                            'data: {"candidates":[{"content":{"parts":[{"text":"记得喵✦"}]}}]}',
                            "data: [DONE]",
                        ],
                    )

                def _fake_refresh(_rt: str, _cfg: OAuthClientConfig):
                    return {"access_token": "fresh_acc_token", "expires_in": 3600}

                with (
                    patch("services.gemini_chat_service.requests.post", side_effect=_fake_chat_post),
                    patch.object(oauth_svc, "_refresh_token", side_effect=_fake_refresh),
                ):
                    reply1 = chat.ask("今天天气怎么样？", settings)
                    self.assertEqual(reply1.text, "你好呀主人喵～✦")
                    self.assertEqual(reply1.suggested_state, PetState.HAPPY)
                    self.assertEqual(captured_payloads[1]["headers"]["Authorization"], "Bearer fresh_acc_token")
                    self.assertEqual(captured_payloads[1]["headers"]["x-goog-user-project"], "demo-proj")

                    reply2 = chat.ask("还记得我刚才问什么吗？", settings)
                    self.assertEqual(reply2.text, "记得喵✦")
                    last_body = captured_payloads[-1]["json"]
                    self.assertEqual(len(last_body["contents"]), 3)
                    self.assertEqual(last_body["contents"][0]["role"], "user")
                    self.assertEqual(last_body["contents"][1]["role"], "model")
                    self.assertEqual(last_body["contents"][2]["parts"][0]["text"], "还记得我刚才问什么吗？")

                    # 退出登录后优雅回退为离线对话，不报错崩溃
                    oauth_svc.clear_session()
                    offline = chat.ask("现在能聊吗？", settings)
                    self.assertEqual(offline.suggested_state, PetState.THINKING)

    def test_sse_utf8_decoding_without_charset_prevents_mojibake(self) -> None:
        # 当 SSE 响应头未携带 charset 时，requests 会默认设为 ISO-8859-1；验证仍正确按 UTF-8 解码中文
        sse_bytes = (
            'data: {"candidates":[{"content":{"parts":[{"text":"你好呀，"},{"text":"主人喵～✦"}]}}]}\n\n'
            "data: [DONE]\n\n"
        ).encode("utf-8")
        resp = requests.Response()
        resp.status_code = 200
        resp.headers["Content-Type"] = "text/event-stream"
        resp.encoding = "ISO-8859-1"
        resp.raw = io.BytesIO(sse_bytes)
        resp._content_consumed = False

        decoded = GeminiChatService._parse_sse_candidates_response(resp, wrapped_in_response=False)
        self.assertEqual(decoded, "你好呀，主人喵～✦")

        err_resp = requests.Response()
        err_resp.status_code = 400
        err_resp.encoding = "ISO-8859-1"
        err_resp._content = json.dumps({"error": {"message": "参数错误：模型不可用"}}, ensure_ascii=False).encode("utf-8")
        self.assertEqual(GeminiChatService._read_error_response(err_resp), "参数错误：模型不可用")

    def test_both_oauth_and_api_key_prefers_oauth(self) -> None:
        with (
            tempfile.TemporaryDirectory() as tmp,
            tempfile.TemporaryDirectory() as tmp_home,
            tempfile.TemporaryDirectory() as tmp_appdata,
        ):
            with patch.dict(os.environ, _isolated_env(tmp_home, tmp_appdata)):
                oauth_svc = OAuthAuthService(tmp)
                oauth_svc.save_session(
                    OAuthSession(
                        email="priority@example.com",
                        access_token="acc_valid",
                        refresh_token="1//priority_rt",
                        expiry_timestamp=time.time() + 3600,
                        client_id="cid",
                        auth_source="oauth_login",
                        api_backend="generativelanguage",
                    )
                )
                chat = GeminiChatService(oauth_service=oauth_svc)
                settings = PetSettings(
                    chat_auth_mode=ChatAuthMode.AUTO,
                    gemini_api_key="dummy-api-key-value",
                    oauth_model="gemini-2.5-flash",
                )
                self.assertEqual(chat.resolve_chat_provider(settings), "oauth")

                def _fake_oauth_post(url: str, **kwargs):
                    del url, kwargs
                    return _DummyResp(
                        200,
                        lines=['data: {"candidates":[{"content":{"parts":[{"text":"优先走了OAuth喵✦"}]}}]}'],
                    )

                with patch("services.gemini_chat_service.requests.post", side_effect=_fake_oauth_post) as mock_post, patch.object(
                    chat, "_call_gemini_api"
                ) as mock_api_key:
                    reply = chat.ask("现在走哪个通道？", settings)
                    self.assertEqual(reply.text, "优先走了OAuth喵✦")
                    mock_post.assert_called_once()
                    mock_api_key.assert_not_called()

                # 显式选择 API_KEY 模式时允许手动切换，退出 OAuth 后 AUTO 自动回落 API_KEY
                api_only = PetSettings(chat_auth_mode=ChatAuthMode.API_KEY, gemini_api_key="dummy-api-key-value")
                self.assertEqual(chat.resolve_chat_provider(api_only), "api_key")
                oauth_svc.clear_session()
                self.assertEqual(chat.resolve_chat_provider(settings), "api_key")

    def test_settings_roundtrip_and_oauth_ui_smoke(self) -> None:
        app = self._ensure_app()
        with (
            tempfile.TemporaryDirectory() as tmp,
            tempfile.TemporaryDirectory() as tmp_home,
            tempfile.TemporaryDirectory() as tmp_appdata,
        ):
            with patch.dict(os.environ, _isolated_env(tmp_home, tmp_appdata)):
                svc = SettingsService(tmp)
                settings = PetSettings(
                    chat_auth_mode=ChatAuthMode.OAUTH,
                    oauth_model="gemini-4-argon",
                )
                svc.save(settings)
                raw = json.loads(svc.settings_path.read_text(encoding="utf-8"))
                self.assertEqual(raw["gemini"]["auth_mode"], "oauth")
                self.assertEqual(raw["gemini"]["oauth_model"], "gemini-4-argon")

                loaded = svc.load()
                self.assertEqual(loaded.chat_auth_mode, ChatAuthMode.OAUTH)
                self.assertEqual(loaded.oauth_model, "gemini-4-argon")
                self.assertEqual(SettingsService(Path(tmp) / "empty").load().oauth_model, DEFAULT_OAUTH_MODEL)

                oauth_svc = OAuthAuthService(tmp)
                oauth_svc.save_session(
                    OAuthSession(
                        email="demo@example.com",
                        display_name="Demo",
                        access_token="acc",
                        refresh_token="1//demo",
                        expiry_timestamp=time.time() + 3600,
                        subscription_tier="FREE",
                        available_models=["gemini-3.8-flash", "gemini-4-argon"],
                        client_id="demo-cid",
                        auth_source="oauth_login",
                    )
                )

                follow_svc = AppFollowService(svc)
                with patch("views.settings_window.FollowedAppProbe.sample_antigravity_activity") as mock_ag, patch(
                    "views.settings_window.FollowedAppProbe.sample_gemini_activity"
                ) as mock_gem:
                    mock_ag.return_value = MagicMock(is_running=False)
                    mock_gem.return_value = MagicMock(is_running=False)
                    win = SettingsWindow(
                        settings=loaded,
                        settings_service=svc,
                        app_follow_service=follow_svc,
                        on_settings_applied=lambda: win.sync_from_settings(),
                        oauth_service=oauth_svc,
                    )
                    try:
                        self.assertIn("demo@example.com", win._oauth_status_lbl.text())
                        self.assertEqual(win._oauth_model_combo.currentText(), "gemini-4-argon")
                        win._oauth_model_combo.setEditText("")
                        self.assertEqual(win._oauth_model_combo.currentText(), "")
                        win._oauth_model_combo.setEditText("gemini-3.8-flash")
                        self.assertEqual(loaded.oauth_model, "gemini-3.8-flash")
                        win._on_oauth_logout_clicked()
                        app.processEvents()
                        self.assertFalse(oauth_svc.is_logged_in())
                        self.assertIn("未检测到", win._oauth_status_lbl.text())
                    finally:
                        win.close()
                        win.deleteLater()
                        app.processEvents()

                # 无内置硬编码客户端时也能安全打开登录弹窗，填入 Client ID 后可生成授权链接并完成手动提交
                dlg = OAuthLoginDialog(oauth_svc)
                try:
                    self.assertEqual(dlg._auth_url_edit.text(), "")
                    dlg._client_id_edit.setText("user-custom-client-id")
                    dlg._prepare_oauth_flow(OAuthClientConfig("user-custom-client-id"))
                    self.assertTrue(
                        dlg._auth_url_edit.text().startswith("https://accounts.google.com/o/oauth2/v2/auth?")
                    )
                    assert dlg._flow is not None
                    expected_state = dlg._flow.state
                    with patch.object(
                        oauth_svc,
                        "exchange_code_for_session",
                        return_value=OAuthSession(email="manual@example.com", refresh_token="1//manual_rt"),
                    ):
                        dlg._manual_input.setText(
                            f"http://localhost:8045/oauth-callback?code=4%2F0A_manual&state={expected_state}"
                        )
                        dlg._on_manual_submit_clicked()
                        deadline = time.time() + 2.0
                        while dlg.logged_in_session is None and time.time() < deadline:
                            app.processEvents()
                            time.sleep(0.02)
                        self.assertIsNotNone(dlg.logged_in_session)
                        assert dlg.logged_in_session is not None
                        self.assertEqual(dlg.logged_in_session.email, "manual@example.com")
                    dlg.reject()
                    app.processEvents()
                finally:
                    dlg.deleteLater()
                    app.processEvents()


if __name__ == "__main__":
    unittest.main()
