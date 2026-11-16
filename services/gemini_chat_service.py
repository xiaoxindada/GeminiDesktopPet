"""本地快捷意图对话、离线台词陪伴，以及支持 OAuth 个人账号 / Gemini API Key 的实时对话服务。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
import random
import time
from typing import Any, Optional
import uuid

import requests

from models.character_lore import CharacterLore
from models.pet_settings import (
    DEFAULT_GEMINI_MODEL,
    DEFAULT_OAUTH_MODEL,
    ChatAuthMode,
    PetSettings,
    normalize_gemini_model,
    normalize_oauth_model,
)
from models.pet_state import PetState
from services.app_logger import AppLogger
from services.oauth_auth_service import (
    GEMINI_API_BASE_URL,
    HUB_USER_AGENT,
    V1_INTERNAL_CHAT_BASE_URLS,
    OAuthAuthService,
    OAuthError,
    OAuthSession,
)
from services.system_monitor_service import SystemMetrics

_MAX_OAUTH_HISTORY_MESSAGES = 20


@dataclass(frozen=True)
class ChatReply:
    text: str
    suggested_state: Optional[PetState] = None


class GeminiChatService:
    """结合本地即时指令、Google 个人账号 OAuth (v1internal) 与官方 google-genai Interactions API 的聊天服务。"""

    def __init__(self, oauth_service: Optional[OAuthAuthService] = None) -> None:
        self._oauth_service = oauth_service
        self._last_interaction_id: Optional[str] = None
        self._last_session_sig: Optional[tuple[str, str, str]] = None
        self._oauth_history: list[dict[str, Any]] = []
        self._oauth_session_sig: Optional[tuple[str, str, str]] = None
        self._oauth_session_id: str = str(uuid.uuid4())

    def reset_conversation(self) -> None:
        self._last_interaction_id = None
        self._last_session_sig = None
        self._oauth_history = []
        self._oauth_session_sig = None
        self._oauth_session_id = str(uuid.uuid4())

    def resolve_chat_provider(self, settings: PetSettings) -> Optional[str]:
        has_oauth = self._oauth_service is not None and self._oauth_service.is_logged_in()
        has_api_key = bool((settings.gemini_api_key or "").strip())
        mode = settings.chat_auth_mode

        if mode == ChatAuthMode.OAUTH:
            return "oauth" if has_oauth else None
        if mode == ChatAuthMode.API_KEY:
            return "api_key" if has_api_key else None
        # 自动模式下当 OAuth 与 API Key 同时可用时，优先使用 OAuth
        if has_oauth and has_api_key:
            return "oauth"
        if has_oauth:
            return "oauth"
        if has_api_key:
            return "api_key"
        return None

    def _resolve_chat_provider(self, settings: PetSettings) -> Optional[str]:
        return self.resolve_chat_provider(settings)

    def ask(
        self,
        user_input: str,
        settings: PetSettings,
        metrics: Optional[SystemMetrics] = None,
    ) -> ChatReply:
        text = (user_input or "").strip()
        if not text:
            return ChatReply("喵？想和我聊什么呀～✦", PetState.LISTENING)

        local_reply = self._try_handle_local_intent(text, settings, metrics)
        if local_reply is not None:
            return local_reply

        provider = self._resolve_chat_provider(settings)
        if provider == "oauth":
            model = normalize_oauth_model(settings.oauth_model) or DEFAULT_OAUTH_MODEL
            try:
                ai_text = self._call_oauth_chat(text, model)
                return ChatReply(ai_text, PetState.HAPPY)
            except Exception as exc:
                AppLogger.error("Chat", f"OAuth 对话调用失败 (Model={model})", exc)
                return ChatReply(
                    f"喵呜…连线失败了（{self._format_api_error(exc)}），先用本地模式陪你喵！",
                    PetState.OOPS,
                )

        if provider == "api_key":
            api_key = (settings.gemini_api_key or "").strip()
            model = normalize_gemini_model(settings.gemini_model) or DEFAULT_GEMINI_MODEL
            try:
                ai_text = self._call_gemini_api(text, api_key, model)
                return ChatReply(ai_text, PetState.HAPPY)
            except Exception as exc:
                AppLogger.error("Chat", f"Gemini API 调用失败 (Model={model})", exc)
                return ChatReply(
                    f"喵呜…连线失败了（{self._format_api_error(exc)}），先用本地模式陪你喵！",
                    PetState.OOPS,
                )

        return ChatReply(self._generate_offline_chatter(text), PetState.THINKING)

    @staticmethod
    def _contains_any(haystack: str, *needles: str) -> bool:
        return any(n in haystack for n in needles)

    def _try_handle_local_intent(
        self,
        text: str,
        settings: PetSettings,
        metrics: Optional[SystemMetrics],
    ) -> Optional[ChatReply]:
        lower = text.lower()

        if self._contains_any(lower, "摸摸", "摸头", "乖", "pat", "rua"):
            return ChatReply(CharacterLore.pick_line("pat"), PetState.PAT)

        if self._contains_any(lower, "反重力", "飘", "飞", "float", "antigravity"):
            return ChatReply("反——重——力——模块启动喵！✦", PetState.FLOAT)

        if self._contains_any(lower, "睡", "晚安", "困", "sleep"):
            return ChatReply("那我先打个盹喵…Zzz…", PetState.SLEEPING)

        if self._contains_any(lower, "早安", "早上好", "上午好"):
            return ChatReply(CharacterLore.pick_line("greet_morning"), PetState.GREET_MORNING)

        if self._contains_any(lower, "午安", "中午", "午饭"):
            return ChatReply(CharacterLore.pick_line("greet_noon"), PetState.GREET_NOON)

        if self._contains_any(lower, "晚上好"):
            return ChatReply(CharacterLore.pick_line("greet_evening"), PetState.GREET_EVENING)

        if self._contains_any(lower, "几点", "时间", "日期"):
            now = datetime.now()
            return ChatReply(
                f"现在是 {now.strftime('%H:%M')}（{now.strftime('%Y-%m-%d')}）喵！记得照顾好眼睛和爪子哦✦",
                PetState.ASKING,
            )

        if self._contains_any(lower, "cpu", "电量", "电脑", "状态", "内存") and metrics is not None:
            bat = (
                f"，电池 {metrics.battery_percent}%{'（充电中）' if metrics.is_charging else ''}"
                if metrics.has_battery
                else ""
            )
            state = PetState.SWAMPED_LEFT if metrics.cpu_percent > 75 else PetState.PROUD
            return ChatReply(
                f"当前 CPU 占用约 {metrics.cpu_percent:.0f}%{bat}，好感度 {settings.favorability}✦",
                state,
            )

        if self._contains_any(lower, "你是谁", "设定", "形象", "异色瞳", "猫耳", "介绍"):
            from models.character_lore import CHARACTER_NAME, DEFAULT_BIO

            profile = CharacterLore.get_active_profile()
            if profile.name == CHARACTER_NAME and profile.bio == DEFAULT_BIO:
                intro = "我是 Gemini 娘（双子娘）喵！留着蓝紫渐变长卷发和猫耳，左眼玫红、右眼琥珀金，最喜欢戴四芒星发饰在桌面上陪你工作聊天啦✦"
            else:
                intro = f"我是「{profile.name}」（{profile.alias}）！{profile.bio}"
            return ChatReply(intro, PetState.PROUD)

        return None

    @staticmethod
    def _generate_offline_chatter(text: str) -> str:
        if text.endswith(("?", "？")) or "吗" in text or "呢" in text:
            return random.choice(
                [
                    "让我想想喵…嗯，我觉得很有道理✦（想让我真正联网回答，可以在「设置」里登录 Google 个人账号或填上 Gemini API Key 哦）",
                    "两个选项都挺不错的喵～不如听听你的直觉？✦",
                    "猫耳天线正在思考中…离线模式下我也可以陪你聊天、摸头、巡逻和撒娇喵！",
                ]
            )
        return random.choice(
            [
                "收到喵～已经放进我的百万上下文小本本里了✦",
                "喵呜！听起来很有趣呢～今天也一起加油吧！",
                "（尾巴轻轻晃了晃）我在听着呢喵～随时可以摸摸我哦✦",
                "好耶喵！需要我切到「反重力悬浮」或打开「角色档案」陪你吗？",
            ]
        )

    def _call_oauth_chat(self, prompt: str, model: str) -> str:
        if self._oauth_service is None:
            raise OAuthError("OAuth 服务未初始化", status_code=401)

        session = self._oauth_service.ensure_fresh_access_token()
        system_prompt = CharacterLore.build_effective_system_prompt()
        sig = (session.email, model, system_prompt)
        if self._oauth_session_sig != sig:
            self._oauth_history = []
            self._oauth_session_sig = sig
            self._oauth_session_id = str(uuid.uuid4())

        contents = [*self._oauth_history, {"role": "user", "parts": [{"text": prompt}]}]
        if session.api_backend == "v1internal":
            reply_text = self._request_v1internal_sse(
                session,
                model=model,
                contents=contents,
                system_prompt=system_prompt,
            )
        else:
            reply_text = self._request_generativelanguage_sse(
                session,
                model=model,
                contents=contents,
                system_prompt=system_prompt,
            )

        final_text = reply_text.strip() or "喵～✦"
        self._oauth_history.append({"role": "user", "parts": [{"text": prompt}]})
        self._oauth_history.append({"role": "model", "parts": [{"text": final_text}]})
        if len(self._oauth_history) > _MAX_OAUTH_HISTORY_MESSAGES:
            self._oauth_history = self._oauth_history[-_MAX_OAUTH_HISTORY_MESSAGES:]
        return final_text

    def _request_generativelanguage_sse(
        self,
        session: OAuthSession,
        *,
        model: str,
        contents: list[dict[str, Any]],
        system_prompt: str,
    ) -> str:
        """通过 Google 官方 Gemini v1beta REST + Bearer OAuth Token 发起流式对话。"""
        assert self._oauth_service is not None
        model_id = model[len("models/") :] if model.startswith("models/") else model
        url = f"{GEMINI_API_BASE_URL}/models/{model_id}:streamGenerateContent?alt=sse"
        payload = {
            "contents": contents,
            "systemInstruction": {"parts": [{"text": system_prompt}]},
            "generationConfig": {"maxOutputTokens": 2048},
        }

        refreshed_once = False
        while True:
            headers: dict[str, str] = {
                "Authorization": f"Bearer {session.access_token}",
                "Content-Type": "application/json",
            }
            if session.project_id:
                headers["x-goog-user-project"] = session.project_id
            try:
                resp = requests.post(
                    url,
                    json=payload,
                    headers=headers,
                    stream=True,
                    timeout=(10, 45),
                )
            except requests.RequestException as exc:
                raise OAuthError(f"连接 Gemini 对话服务失败：{exc}") from exc

            if (
                resp.status_code == 401
                and not refreshed_once
                and session.can_refresh(self._oauth_service.get_client_config())
            ):
                resp.close()
                refreshed_once = True
                session = self._oauth_service.ensure_fresh_access_token(force_refresh=True)
                continue

            if not resp.ok:
                err_text = self._read_error_response(resp)
                resp.close()
                raise OAuthError(err_text, status_code=resp.status_code)

            try:
                return self._parse_sse_candidates_response(resp, wrapped_in_response=False)
            finally:
                resp.close()

    def _request_v1internal_sse(
        self,
        session: OAuthSession,
        *,
        model: str,
        contents: list[dict[str, Any]],
        system_prompt: str,
    ) -> str:
        assert self._oauth_service is not None
        payload = self._build_v1internal_payload(
            project_id=(session.project_id or "").strip(),
            session_id=self._oauth_session_id,
            model=model,
            contents=contents,
            system_prompt=system_prompt,
        )

        # 使用 Daily -> Sandbox 顺序调用 v1internal:streamGenerateContent?alt=sse，并在 401 时强制刷新一次 Token 重试
        refreshed_once = False
        last_error: Optional[Exception] = None

        for base_url in V1_INTERNAL_CHAT_BASE_URLS:
            url = f"{base_url}:streamGenerateContent?alt=sse"
            while True:
                headers = {
                    "Authorization": f"Bearer {session.access_token}",
                    "Content-Type": "application/json",
                    "User-Agent": HUB_USER_AGENT,
                }
                try:
                    resp = requests.post(
                        url,
                        json=payload,
                        headers=headers,
                        stream=True,
                        timeout=(10, 45),
                    )
                except requests.RequestException as exc:
                    last_error = OAuthError(f"连接上游对话服务失败：{exc}")
                    break

                if (
                    resp.status_code == 401
                    and not refreshed_once
                    and session.can_refresh(self._oauth_service.get_client_config())
                ):
                    resp.close()
                    refreshed_once = True
                    session = self._oauth_service.ensure_fresh_access_token(force_refresh=True)
                    continue

                if not resp.ok:
                    err_text = self._read_error_response(resp)
                    resp.close()
                    err = OAuthError(err_text, status_code=resp.status_code)
                    if self._should_try_next_v1internal_endpoint(resp.status_code, base_url):
                        last_error = err
                        break
                    raise err

                try:
                    return self._parse_sse_candidates_response(resp, wrapped_in_response=True)
                finally:
                    resp.close()

        if last_error is not None:
            raise last_error
        raise OAuthError("上游对话端点均不可用")

    @staticmethod
    def _build_v1internal_payload(
        *,
        project_id: str,
        session_id: str,
        model: str,
        contents: list[dict[str, Any]],
        system_prompt: str,
    ) -> dict[str, Any]:
        conv_hex = hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:16]
        traj_hex = uuid.uuid4().hex[:8]
        ts_ms = int(time.time() * 1000)
        request_id = f"agent/{conv_hex}/{ts_ms}/{traj_hex}/{len(contents)}"
        return {
            "project": project_id,
            "requestId": request_id,
            "request": {
                "contents": contents,
                "systemInstruction": {
                    "role": "user",
                    "parts": [{"text": system_prompt}],
                },
                "generationConfig": {
                    "maxOutputTokens": 2048,
                },
                "sessionId": session_id,
            },
            "model": model,
            "userAgent": "antigravity",
        }

    @staticmethod
    def _should_try_next_v1internal_endpoint(status_code: int, base_url: str) -> bool:
        if status_code == 400 and "sandbox" in base_url.lower():
            return True
        return status_code in (404, 408) or 500 <= status_code < 600

    @staticmethod
    def _decode_response_text(resp: requests.Response) -> str:
        raw_bytes = getattr(resp, "content", None)
        if isinstance(raw_bytes, (bytes, bytearray)):
            return bytes(raw_bytes).decode("utf-8", errors="replace")
        return str(getattr(resp, "text", "") or "")

    @staticmethod
    def _read_error_response(resp: requests.Response) -> str:
        raw = GeminiChatService._decode_response_text(resp)
        try:
            obj = json.loads(raw)
            if isinstance(obj, dict):
                err = obj.get("error")
                if isinstance(err, dict):
                    msg = err.get("message") or err.get("status")
                    if isinstance(msg, str) and msg.strip():
                        return msg.strip()
        except Exception:
            pass
        cleaned = raw.strip()
        return cleaned[:160] if len(cleaned) > 160 else (cleaned or f"HTTP {resp.status_code}")

    @staticmethod
    def _extract_chunk_parts(
        chunk: dict[str, Any], *, wrapped_in_response: bool = True
    ) -> list[str]:
        actual = chunk["response"] if (wrapped_in_response and isinstance(chunk.get("response"), dict)) else chunk
        if isinstance(actual.get("error"), dict):
            err_obj = actual["error"]
            code = err_obj.get("code")
            msg = str(err_obj.get("message") or err_obj.get("status") or "上游返回错误").strip()
            raise OAuthError(msg, status_code=int(code) if isinstance(code, int) else None)

        candidates = actual.get("candidates")
        if not isinstance(candidates, list) or not candidates:
            return []
        first_cand = candidates[0]
        if not isinstance(first_cand, dict):
            return []
        content = first_cand.get("content")
        if not isinstance(content, dict):
            return []
        parts = content.get("parts")
        if not isinstance(parts, list):
            return []

        texts: list[str] = []
        for part in parts:
            if not isinstance(part, dict):
                continue
            # 仅提取非思维链的正文文本输出到桌宠对话气泡
            if part.get("thought") is True:
                continue
            txt = part.get("text")
            if isinstance(txt, str) and txt:
                texts.append(txt)
        return texts

    @staticmethod
    def _parse_sse_candidates_response(
        resp: requests.Response, *, wrapped_in_response: bool = True
    ) -> str:
        # 响应头缺少 charset 时 requests 默认按 ISO-8859-1 解码会导致中文乱码，强制读取原始字节按 UTF-8 解码
        answer_parts: list[str] = []
        raw_lines: list[str] = []
        seen_sse_data = False

        for raw_line in resp.iter_lines(decode_unicode=False):
            if not raw_line:
                continue
            line = (
                bytes(raw_line).decode("utf-8", errors="replace").strip()
                if isinstance(raw_line, (bytes, bytearray))
                else str(raw_line).strip()
            )
            if not line:
                continue
            raw_lines.append(line)
            if not line.startswith("data:"):
                continue
            seen_sse_data = True
            json_part = line[5:].strip()
            if not json_part or json_part == "[DONE]":
                continue
            try:
                chunk = json.loads(json_part)
            except Exception:
                continue
            if isinstance(chunk, dict):
                answer_parts.extend(
                    GeminiChatService._extract_chunk_parts(chunk, wrapped_in_response=wrapped_in_response)
                )

        if not seen_sse_data and raw_lines:
            try:
                parsed = json.loads("\n".join(raw_lines))
                items = parsed if isinstance(parsed, list) else [parsed]
                for item in items:
                    if isinstance(item, dict):
                        answer_parts.extend(
                            GeminiChatService._extract_chunk_parts(item, wrapped_in_response=wrapped_in_response)
                        )
            except Exception:
                pass

        return "".join(answer_parts).strip()

    def _call_gemini_api(self, prompt: str, api_key: str, model: str) -> str:
        from google import genai
        from google.genai import errors

        system_prompt = CharacterLore.build_effective_system_prompt()
        session_sig = (api_key, model, system_prompt)
        if self._last_session_sig != session_sig:
            self._last_interaction_id = None
            self._last_session_sig = session_sig

        # 使用官方 google-genai >= 3.2 Interactions API，并按每轮重传 system_instruction 与 generation_config
        with genai.Client(api_key=api_key, http_options={"timeout": 25000}) as client:
            kwargs: dict = {
                "model": model,
                "input": prompt,
                "system_instruction": system_prompt,
                "generation_config": {"max_output_tokens": 240},
            }
            if self._last_interaction_id:
                kwargs["previous_interaction_id"] = self._last_interaction_id

            try:
                interaction = client.interactions.create(**kwargs)
            except errors.APIError as api_err:
                # 若上一轮会话 ID 失效（过期或切换 Key），清掉上下文后单轮重试
                if self._last_interaction_id and getattr(api_err, "code", None) in (400, 404):
                    self._last_interaction_id = None
                    kwargs.pop("previous_interaction_id", None)
                    interaction = client.interactions.create(**kwargs)
                else:
                    raise

        if getattr(interaction, "id", None):
            self._last_interaction_id = interaction.id

        output_text = getattr(interaction, "output_text", None)
        if isinstance(output_text, str) and output_text.strip():
            return output_text.strip()
        return "喵～✦"

    @staticmethod
    def _format_api_error(exc: Exception) -> str:
        code = getattr(exc, "code", None)
        msg = str(exc).strip()
        low = msg.lower()
        if isinstance(exc, OAuthError):
            if "validation_required" in low or "verify your account" in low or "validation_url" in low:
                return "当前 Google 账号需先完成安全验证"
            if code in (401, 403):
                return "OAuth 授权无效或权限不足，请在设置中重新登录"
            if code == 404:
                return "当前模型不可用，请在设置中切换模型"
            if code == 429:
                return "触发个人账号额度或频率限制"
            return msg[:80] if len(msg) > 80 else (msg or "OAuth 对话请求失败")

        if code == 401 or code == 403:
            return "API Key 无效或权限不足"
        if code == 429:
            return "触发频率或额度限制"
        if code is not None:
            return f"HTTP {code}"
        return msg[:80] if len(msg) > 80 else (msg or type(exc).__name__)
