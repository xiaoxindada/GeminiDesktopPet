"""本地快捷意图对话、离线台词陪伴，以及支持 OAuth 个人账号 / Gemini API Key 的实时对话服务。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import random
import subprocess
import tempfile
import time
from typing import Any, Optional
import uuid

import requests

from models.app_version import get_user_config_dir
from models.character_lore import CharacterLore
from models.pet_settings import (
    DEFAULT_GEMINI_MODEL,
    DEFAULT_OAUTH_MODEL,
    DEFAULT_THINKING_LEVEL,
    ChatAuthMode,
    PetSettings,
    normalize_gemini_model,
    normalize_oauth_model,
    normalize_thinking_level,
)
from models.pet_state import PetState
from services.antigravity_quota_service import AntigravityQuotaService
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
    """结合本地快捷动作、OAuth / Antigravity / Gemini API Key 多端实时模型的对话服务。"""

    _api_models_cache: dict[str, list[str]] = {}
    _agy_models_cache: Optional[list[str]] = None

    def __init__(self, oauth_service: Optional[OAuthAuthService] = None) -> None:
        self._oauth_service = oauth_service
        self._last_interaction_id: Optional[str] = None
        self._last_session_sig: Optional[tuple[str, str, str]] = None
        self._api_fallback_history: list[dict[str, Any]] = []
        self._oauth_history: list[dict[str, Any]] = []
        self._oauth_session_sig: Optional[tuple[str, str, str]] = None
        self._oauth_session_id: str = str(uuid.uuid4())
        self._agy_conversation_id: Optional[str] = None
        self._agy_session_sig: Optional[tuple[str, str]] = None

    @property
    def oauth_service(self) -> Optional[OAuthAuthService]:
        return self._oauth_service

    def reset_conversation(self) -> None:
        self._last_interaction_id = None
        self._last_session_sig = None
        self._api_fallback_history = []
        self._oauth_history = []
        self._oauth_session_sig = None
        self._oauth_session_id = str(uuid.uuid4())
        self._agy_conversation_id = None
        self._agy_session_sig = None

    @staticmethod
    def has_agy_cli() -> bool:
        return bool(AntigravityQuotaService.resolve_agy_executable())

    @classmethod
    def get_cached_agy_models(cls) -> list[str]:
        return list(cls._agy_models_cache) if cls._agy_models_cache else []

    @classmethod
    def clear_agy_models_cache(cls) -> None:
        cls._agy_models_cache = None

    @staticmethod
    def _normalize_agy_model_entry(raw_id: str) -> str:
        cleaned = (raw_id or "").strip()
        if not cleaned:
            return ""
        low = cleaned.lower()
        for prefix in ("gemini-4-argon-", "gemini-4-hydrogen-", "gemini-4-carbon-", "gemini-3.8-flash-", "claude-opus-5-5-", "claude-sonnet-5-5-"):
            if low.startswith(prefix):
                suffix = low[len(prefix) :]
                if suffix in ("low", "medium", "high", "xhigh"):
                    return cleaned[: len(prefix) - 1]
        return cleaned

    @classmethod
    def discover_agy_models(cls, force_refresh: bool = False) -> list[str]:
        if cls._agy_models_cache is not None and not force_refresh:
            return list(cls._agy_models_cache)
        agy_path = AntigravityQuotaService.resolve_agy_executable()
        if not agy_path:
            return []

        cmd = [agy_path, "models"]
        if agy_path.lower().endswith((".cmd", ".bat")):
            cmd = [os.environ.get("ComSpec", "cmd.exe"), "/d", "/c", *cmd]
        creationflags = 0x08000000 if os.name == "nt" else 0
        try:
            proc = subprocess.Popen(
                cmd,
                cwd=tempfile.gettempdir(),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                creationflags=creationflags,
            )
            out, _ = proc.communicate(timeout=15.0)
        except Exception:
            return []

        if proc.returncode != 0 or not out:
            return []

        discovered: list[str] = []
        for line in out.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            model_id = stripped.split("\t", 1)[0].split(None, 1)[0].strip()
            normalized = cls._normalize_agy_model_entry(model_id)
            if normalized and normalized not in discovered:
                discovered.append(normalized)
        if discovered:
            cls._agy_models_cache = list(discovered)
        return discovered

    def resolve_chat_provider(self, settings: PetSettings) -> Optional[str]:
        has_oauth = self._oauth_service is not None and self._oauth_service.is_logged_in()
        oauth_opted_out = self._oauth_service is not None and self._oauth_service.is_local_opted_out()
        has_api_key = bool((settings.gemini_api_key or "").strip())
        has_agy = (not oauth_opted_out) and self.has_agy_cli()
        mode = settings.chat_auth_mode

        if mode == ChatAuthMode.OAUTH:
            if has_oauth:
                return "oauth"
            return "agy" if has_agy else None
        if mode == ChatAuthMode.API_KEY:
            return "api_key" if has_api_key else None
        # 自动模式下优先直连 OAuth -> 已填 API Key -> 本机已登录的 Antigravity CLI
        if has_oauth:
            return "oauth"
        if has_api_key:
            return "api_key"
        if has_agy:
            return "agy"
        return None

    def _resolve_chat_provider(self, settings: PetSettings) -> Optional[str]:
        return self.resolve_chat_provider(settings)

    def _try_handle_strict_pet_command(self, text: str) -> Optional[ChatReply]:
        """当真实模型已接入时，仅响应明确的短宠物动作口令，避免截胡用户的正常提问。"""
        cleaned = (text or "").strip().lower().rstrip("！!~～。.")
        if cleaned in ("/pat", "摸摸", "摸摸头", "摸摸猫耳", "rua"):
            return ChatReply(CharacterLore.pick_line("pat"), PetState.PAT)
        if cleaned in ("/float", "开启反重力", "反重力", "反重力悬浮", "反重力模式"):
            return ChatReply("反——重——力——模块启动喵！✦", PetState.FLOAT)
        if cleaned in ("/sleep", "去打盹", "打盹模式"):
            return ChatReply("那我先打个盹喵…Zzz…", PetState.SLEEPING)
        return None

    def ask(
        self,
        user_input: str,
        settings: PetSettings,
        metrics: Optional[SystemMetrics] = None,
    ) -> ChatReply:
        text = (user_input or "").strip()
        if not text:
            return ChatReply("喵？想和我聊什么呀～✦", PetState.LISTENING)

        provider = self._resolve_chat_provider(settings)
        if provider is None:
            local_reply = self._try_handle_local_intent(text, settings, metrics)
            if local_reply is not None:
                return local_reply
            return ChatReply(self._generate_offline_chatter(text), PetState.THINKING)

        strict_cmd = self._try_handle_strict_pet_command(text)
        if strict_cmd is not None:
            return strict_cmd

        thinking_level = normalize_thinking_level(getattr(settings, "thinking_level", DEFAULT_THINKING_LEVEL))
        if provider == "oauth":
            model = normalize_oauth_model(settings.oauth_model) or DEFAULT_OAUTH_MODEL
            try:
                ai_text = self._call_oauth_chat(text, model, thinking_level=thinking_level)
                return ChatReply(ai_text, PetState.HAPPY)
            except Exception as exc:
                AppLogger.error("Chat", f"OAuth 对话调用失败 (Model={model}, Thinking={thinking_level})", exc)
                return ChatReply(
                    f"喵呜…连线失败了（{self._format_api_error(exc)}），先用本地模式陪你喵！",
                    PetState.OOPS,
                )

        if provider == "agy":
            model = normalize_oauth_model(settings.oauth_model) or DEFAULT_OAUTH_MODEL
            try:
                ai_text = self._call_agy_chat(text, model, thinking_level=thinking_level)
                return ChatReply(ai_text, PetState.HAPPY)
            except Exception as exc:
                AppLogger.error("Chat", f"Antigravity CLI 对话调用失败 (Model={model}, Thinking={thinking_level})", exc)
                return ChatReply(
                    f"喵呜…请求模型失败了（{self._format_api_error(exc)}），请检查模型选择或网络连线～",
                    PetState.OOPS,
                )

        if provider == "api_key":
            api_key = (settings.gemini_api_key or "").strip()
            model = normalize_gemini_model(settings.gemini_model) or DEFAULT_GEMINI_MODEL
            try:
                ai_text = self._call_gemini_api(text, api_key, model, thinking_level=thinking_level)
                return ChatReply(ai_text, PetState.HAPPY)
            except Exception as exc:
                AppLogger.error("Chat", f"Gemini API 调用失败 (Model={model}, Thinking={thinking_level})", exc)
                return ChatReply(
                    f"喵呜…连线失败了（{self._format_api_error(exc)}），先用本地模式陪你喵！",
                    PetState.OOPS,
                )

        return ChatReply(self._generate_offline_chatter(text), PetState.THINKING)

    @staticmethod
    def resolve_agy_model_and_effort(
        model: str,
        thinking_level: str = "auto",
        available_models: Optional[list[str]] = None,
    ) -> tuple[str, Optional[str]]:
        """将当前所选模型和思考强度映射为 agy CLI 可执行的参数组合。"""
        cleaned = (model or "").strip()
        if cleaned.startswith("models/"):
            cleaned = cleaned[len("models/") :]
        low = cleaned.lower()

        known_agy = {m.lower(): m for m in (available_models or []) if m}
        direct_agy_prefixes = (
            "gemini-4-argon",
            "gemini-4-hydrogen",
            "gemini-4-carbon",
            "gemini-3.8-flash",
            "gemini-pro-orchestrator",
            "claude-",
            "gpt-oss-",
        )

        if low in known_agy:
            resolved_model = known_agy[low]
        elif low.startswith(direct_agy_prefixes):
            resolved_model = cleaned
        elif "pro" in low:
            resolved_model = known_agy.get("gemini-4-argon", "gemini-4-argon")
        else:
            resolved_model = known_agy.get("gemini-3.8-flash", "gemini-3.8-flash")

        lvl = normalize_thinking_level(thinking_level)
        rlow = resolved_model.lower()
        if lvl == "auto" or rlow.startswith(("gemini-pro-orchestrator", "gpt-oss-")):
            effort: Optional[str] = None
        elif lvl in ("off", "minimal", "low"):
            effort = "low"
        elif lvl == "medium":
            effort = "medium"
        elif lvl == "high":
            effort = "high"
        elif lvl == "xhigh":
            effort = "xhigh" if rlow.startswith(("gemini-4-hydrogen", "gemini-4-carbon")) else "high"
        else:
            effort = None
        return resolved_model, effort

    @staticmethod
    def _get_agy_chat_sandbox_dir() -> str:
        try:
            sandbox = Path(get_user_config_dir()) / "chat_sandbox"
            sandbox.mkdir(parents=True, exist_ok=True)
            return str(sandbox)
        except Exception:
            return tempfile.gettempdir()

    @staticmethod
    def _run_agy_chat_once(
        agy_path: str,
        *,
        model: str,
        effort: Optional[str],
        conversation_id: Optional[str],
        prompt: str,
    ) -> tuple[int, str, str]:
        base_args = [
            "--model",
            model,
            "--output-format",
            "json",
            "--disable-slash-commands",
            "--print-timeout",
            "60s",
        ]
        if effort:
            base_args.extend(["--effort", effort])
        if conversation_id:
            base_args.extend(["--conversation", conversation_id])
        base_args.append(f"--print={prompt}")

        if agy_path.lower().endswith((".cmd", ".bat")):
            cmd = [os.environ.get("ComSpec", "cmd.exe"), "/d", "/c", agy_path, *base_args]
        else:
            cmd = [agy_path, *base_args]
        creationflags = 0x08000000 if os.name == "nt" else 0
        proc = subprocess.Popen(
            cmd,
            cwd=GeminiChatService._get_agy_chat_sandbox_dir(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=creationflags,
        )
        try:
            out, err = proc.communicate(timeout=65.0)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
            raise RuntimeError("模型响应超时")
        return proc.returncode, out or "", err or ""

    @staticmethod
    def _parse_agy_json_output(out: str, err: str) -> tuple[Optional[str], Optional[str], Optional[str]]:
        text = (out or "").strip()
        for candidate in (text, *reversed(text.splitlines())):
            cand = candidate.strip()
            if not cand.startswith("{"):
                continue
            try:
                data = json.loads(cand)
            except Exception:
                continue
            if not isinstance(data, dict):
                continue
            status = str(data.get("status") or "").strip().upper()
            conv_id = str(data.get("conversation_id") or "").strip() or None
            resp_text = data.get("response") or data.get("result")
            err_msg = data.get("error")
            if status == "ERROR" or (err_msg and not resp_text):
                return conv_id, None, str(err_msg or "模型调用失败").strip()
            if isinstance(resp_text, str) and resp_text.strip():
                return conv_id, resp_text.strip(), None
        if text and not text.startswith("{"):
            return None, text, None
        return None, None, (err or text or "模型无响应").strip()[:120]

    def _call_agy_chat(self, prompt: str, model: str, thinking_level: str = "auto") -> str:
        agy_path = AntigravityQuotaService.resolve_agy_executable()
        if not agy_path:
            raise RuntimeError("未找到本机 Antigravity CLI")

        resolved_model, effort = self.resolve_agy_model_and_effort(
            model, thinking_level, self.get_cached_agy_models()
        )
        system_prompt = CharacterLore.build_effective_system_prompt()
        sig = (resolved_model, system_prompt)
        if self._agy_session_sig != sig:
            self._agy_conversation_id = None
            self._agy_session_sig = sig

        def build_effective_prompt(conv_id: Optional[str]) -> str:
            if conv_id:
                return prompt
            return (
                f"[角色设定（请在后续对话中保持以下人设与口吻自然回复，直接回答用户，不修改任何文件）]\n"
                f"{system_prompt}\n\n"
                f"用户：{prompt}"
            )

        conv_id = self._agy_conversation_id
        code, out, err = self._run_agy_chat_once(
            agy_path,
            model=resolved_model,
            effort=effort,
            conversation_id=conv_id,
            prompt=build_effective_prompt(conv_id),
        )
        new_conv_id, reply_text, err_detail = self._parse_agy_json_output(out, err)

        # 若旧会话失效或模型不接受特定 effort 参数，分别降级重试一次
        if reply_text is None and conv_id:
            self._agy_conversation_id = None
            code, out, err = self._run_agy_chat_once(
                agy_path,
                model=resolved_model,
                effort=effort,
                conversation_id=None,
                prompt=build_effective_prompt(None),
            )
            new_conv_id, reply_text, err_detail = self._parse_agy_json_output(out, err)

        if reply_text is None and effort is not None and err_detail and any(
            tok in err_detail.lower() for tok in ("effort", "model", "invalid", "xhigh")
        ):
            fallback_effort = "high" if effort == "xhigh" else None
            code, out, err = self._run_agy_chat_once(
                agy_path,
                model=resolved_model,
                effort=fallback_effort,
                conversation_id=self._agy_conversation_id,
                prompt=build_effective_prompt(self._agy_conversation_id),
            )
            new_conv_id, reply_text, err_detail = self._parse_agy_json_output(out, err)

        if reply_text is None:
            raise RuntimeError(err_detail or f"模型调用失败 (退出码 {code})")

        if new_conv_id:
            self._agy_conversation_id = new_conv_id
        return reply_text

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

    @staticmethod
    def _build_rest_thinking_config(model: str, thinking_level: str) -> Optional[dict[str, Any]]:
        level = normalize_thinking_level(thinking_level)
        if level == "auto":
            return None
        if level == "off":
            if "2.5" in (model or "").lower():
                return {"thinkingBudget": 0}
            return {"thinkingLevel": "MINIMAL"}
        return {"thinkingLevel": level.upper()}

    @staticmethod
    def _downgrade_rest_thinking_config(gen_cfg: dict[str, Any], err_text: str) -> bool:
        tcfg = gen_cfg.get("thinkingConfig")
        if not isinstance(tcfg, dict):
            return False
        low = (err_text or "").lower()
        if not any(tok in low for tok in ("thinking", "level", "budget", "xhigh", "unsupported", "invalid")):
            return False
        if tcfg.get("thinkingLevel") == "XHIGH":
            tcfg["thinkingLevel"] = "HIGH"
            return True
        gen_cfg.pop("thinkingConfig", None)
        return True

    def _call_oauth_chat(self, prompt: str, model: str, thinking_level: str = "auto") -> str:
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
                thinking_level=thinking_level,
            )
        else:
            reply_text = self._request_generativelanguage_sse(
                session,
                model=model,
                contents=contents,
                system_prompt=system_prompt,
                thinking_level=thinking_level,
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
        thinking_level: str = "auto",
    ) -> str:
        """通过 Google 官方 Gemini v1beta REST + Bearer OAuth Token 发起流式对话。"""
        assert self._oauth_service is not None
        model_id = model[len("models/") :] if model.startswith("models/") else model
        url = f"{GEMINI_API_BASE_URL}/models/{model_id}:streamGenerateContent?alt=sse"
        gen_cfg: dict[str, Any] = {"maxOutputTokens": 2048}
        tcfg = self._build_rest_thinking_config(model_id, thinking_level)
        if tcfg is not None:
            gen_cfg["thinkingConfig"] = tcfg
        payload = {
            "contents": contents,
            "systemInstruction": {"parts": [{"text": system_prompt}]},
            "generationConfig": gen_cfg,
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
                if resp.status_code == 400 and self._downgrade_rest_thinking_config(gen_cfg, err_text):
                    continue
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
        thinking_level: str = "auto",
    ) -> str:
        assert self._oauth_service is not None
        payload = self._build_v1internal_payload(
            project_id=(session.project_id or "").strip(),
            session_id=self._oauth_session_id,
            model=model,
            contents=contents,
            system_prompt=system_prompt,
            thinking_level=thinking_level,
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
                    req_obj = payload.get("request")
                    gen_cfg = req_obj.get("generationConfig") if isinstance(req_obj, dict) else None
                    if (
                        resp.status_code == 400
                        and isinstance(gen_cfg, dict)
                        and self._downgrade_rest_thinking_config(gen_cfg, err_text)
                    ):
                        continue
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
        thinking_level: str = "auto",
    ) -> dict[str, Any]:
        conv_hex = hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:16]
        traj_hex = uuid.uuid4().hex[:8]
        ts_ms = int(time.time() * 1000)
        request_id = f"agent/{conv_hex}/{ts_ms}/{traj_hex}/{len(contents)}"
        gen_cfg: dict[str, Any] = {"maxOutputTokens": 2048}
        tcfg = GeminiChatService._build_rest_thinking_config(model, thinking_level)
        if tcfg is not None:
            gen_cfg["thinkingConfig"] = tcfg
        return {
            "project": project_id,
            "requestId": request_id,
            "request": {
                "contents": contents,
                "systemInstruction": {
                    "role": "user",
                    "parts": [{"text": system_prompt}],
                },
                "generationConfig": gen_cfg,
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

    @staticmethod
    def _extract_chat_model_id(name: str, actions: Any) -> Optional[str]:
        cleaned = (name or "").strip()
        if cleaned.startswith("models/"):
            cleaned = cleaned[len("models/") :]
        if not cleaned:
            return None

        low = cleaned.lower()
        excluded_tokens = (
            "embedding",
            "aqa",
            "imagen",
            "veo",
            "lyria",
            "-tts",
            "-transcribe",
            "-live",
            "native-audio",
            "-image",
            "bisheng",
        )
        if any(token in low for token in excluded_tokens):
            return None

        if isinstance(actions, (list, tuple, set)):
            action_set = {str(a).strip() for a in actions}
            if action_set and not ({"generateContent", "createInteraction"} & action_set):
                return None

        return cleaned

    @classmethod
    def fetch_api_key_models(cls, api_key: str) -> list[str]:
        """使用 Gemini API Key 从官方 models.list() 获取支持对话的模型 ID 列表。"""
        cleaned_key = (api_key or "").strip()
        if not cleaned_key:
            raise ValueError("请先填写 Gemini API Key")

        from google import genai

        try:
            with genai.Client(api_key=cleaned_key, http_options={"timeout": 15000}) as client:
                raw_models = list(client.models.list(config={"page_size": 100}))
        except Exception as exc:
            raise RuntimeError(cls._format_api_error(exc)) from exc

        discovered: list[str] = []
        for item in raw_models:
            if isinstance(item, dict):
                name = str(item.get("name") or "")
                actions = item.get("supported_actions") or item.get("supportedGenerationMethods")
            else:
                name = str(getattr(item, "name", "") or "")
                actions = getattr(item, "supported_actions", None)
                if actions is None:
                    actions = getattr(item, "supportedGenerationMethods", None)

            model_id = cls._extract_chat_model_id(name, actions)
            if model_id and model_id not in discovered:
                discovered.append(model_id)

        # 默认模型若在官方列表中则置顶，其余保持直观排序
        if DEFAULT_GEMINI_MODEL in discovered:
            discovered.remove(DEFAULT_GEMINI_MODEL)
            discovered.insert(0, DEFAULT_GEMINI_MODEL)
        key_hash = hashlib.sha256(cleaned_key.encode("utf-8")).hexdigest()[:16]
        cls._api_models_cache[key_hash] = list(discovered)
        return discovered

    @classmethod
    def get_cached_api_key_models(cls, api_key: str) -> list[str]:
        cleaned_key = (api_key or "").strip()
        if not cleaned_key:
            return []
        key_hash = hashlib.sha256(cleaned_key.encode("utf-8")).hexdigest()[:16]
        return list(cls._api_models_cache.get(key_hash, []))

    @staticmethod
    def _build_interactions_thinking_level(model: str, thinking_level: str) -> Optional[str]:
        level = normalize_thinking_level(thinking_level)
        if level == "auto":
            return None
        if level == "off":
            return "minimal" if "2.5" not in (model or "").lower() else None
        return level

    @staticmethod
    def _build_sdk_thinking_config(types_mod: Any, model: str, thinking_level: str) -> Any:
        level = normalize_thinking_level(thinking_level)
        if level == "auto":
            return None
        thinking_cfg_cls = getattr(types_mod, "ThinkingConfig", None)
        thinking_lvl_enum = getattr(types_mod, "ThinkingLevel", None)
        if thinking_cfg_cls is None:
            return None
        if level == "off":
            if "2.5" in (model or "").lower():
                return thinking_cfg_cls(thinking_budget=0)
            if thinking_lvl_enum is not None:
                return thinking_cfg_cls(thinking_level=thinking_lvl_enum.MINIMAL)
            return thinking_cfg_cls(thinking_level="MINIMAL")
        if thinking_lvl_enum is not None:
            enum_member = getattr(thinking_lvl_enum, level.upper(), None)
            if enum_member is not None:
                return thinking_cfg_cls(thinking_level=enum_member)
        return thinking_cfg_cls(thinking_level=level.upper())

    def _call_gemini_api(
        self, prompt: str, api_key: str, model: str, thinking_level: str = "auto"
    ) -> str:
        from google import genai
        from google.genai import errors, types

        level = normalize_thinking_level(thinking_level)
        system_prompt = CharacterLore.build_effective_system_prompt()
        session_sig = (api_key, model, system_prompt)
        if self._last_session_sig != session_sig:
            self._last_interaction_id = None
            self._api_fallback_history = []
            self._last_session_sig = session_sig

        # 使用官方 google-genai >= 3.2 Interactions API，并按每轮重传 system_instruction 与 generation_config
        with genai.Client(api_key=api_key, http_options={"timeout": 25000}) as client:
            if level == "off" and "2.5" in (model or "").lower():
                return self._call_gemini_generate_content_fallback(
                    client,
                    types,
                    prompt=prompt,
                    model=model,
                    system_prompt=system_prompt,
                    thinking_level=level,
                )

            gen_config: dict[str, Any] = {"max_output_tokens": 2048}
            inter_level = self._build_interactions_thinking_level(model, level)
            if inter_level is not None:
                gen_config["thinking_level"] = inter_level

            kwargs: dict = {
                "model": model,
                "input": prompt,
                "system_instruction": system_prompt,
                "generation_config": gen_config,
            }
            if self._last_interaction_id:
                kwargs["previous_interaction_id"] = self._last_interaction_id

            try:
                interaction = client.interactions.create(**kwargs)
            except errors.APIError as api_err:
                code = getattr(api_err, "code", None)
                # 若上一轮会话 ID 失效（过期或切换 Key），清掉上下文后单轮重试
                if self._last_interaction_id and code in (400, 404):
                    self._last_interaction_id = None
                    kwargs.pop("previous_interaction_id", None)
                    try:
                        interaction = client.interactions.create(**kwargs)
                    except errors.APIError as retry_err:
                        if getattr(retry_err, "code", None) in (400, 404):
                            return self._call_gemini_generate_content_fallback(
                                client,
                                types,
                                prompt=prompt,
                                model=model,
                                system_prompt=system_prompt,
                                thinking_level=level,
                            )
                        raise
                elif code in (400, 404):
                    return self._call_gemini_generate_content_fallback(
                        client,
                        types,
                        prompt=prompt,
                        model=model,
                        system_prompt=system_prompt,
                        thinking_level=level,
                    )
                else:
                    raise

        if getattr(interaction, "id", None):
            self._last_interaction_id = interaction.id

        output_text = getattr(interaction, "output_text", None)
        if isinstance(output_text, str) and output_text.strip():
            return output_text.strip()
        return "喵～✦"

    def _call_gemini_generate_content_fallback(
        self,
        client: Any,
        types_mod: Any,
        *,
        prompt: str,
        model: str,
        system_prompt: str,
        thinking_level: str = "auto",
    ) -> str:
        """对仅开放 generateContent 而未接入 Interactions 或需要 ThinkingConfig 的模型提供支持与回退。"""
        from google.genai import errors

        contents = [*self._api_fallback_history, {"role": "user", "parts": [{"text": prompt}]}]
        cfg_kwargs: dict[str, Any] = {
            "system_instruction": system_prompt,
            "max_output_tokens": 2048,
        }
        tcfg = self._build_sdk_thinking_config(types_mod, model, thinking_level)
        if tcfg is not None:
            cfg_kwargs["thinking_config"] = tcfg

        try:
            resp = client.models.generate_content(
                model=model,
                contents=contents,
                config=types_mod.GenerateContentConfig(**cfg_kwargs),
            )
        except errors.APIError as err:
            err_low = str(err).lower()
            if "thinking_config" in cfg_kwargs and getattr(err, "code", None) == 400 and any(
                tok in err_low for tok in ("thinking", "level", "budget", "xhigh")
            ):
                cfg_kwargs.pop("thinking_config", None)
                resp = client.models.generate_content(
                    model=model,
                    contents=contents,
                    config=types_mod.GenerateContentConfig(**cfg_kwargs),
                )
            else:
                raise

        final_text = (getattr(resp, "text", None) or "").strip() or "喵～✦"
        self._api_fallback_history.append({"role": "user", "parts": [{"text": prompt}]})
        self._api_fallback_history.append({"role": "model", "parts": [{"text": final_text}]})
        if len(self._api_fallback_history) > _MAX_OAUTH_HISTORY_MESSAGES:
            self._api_fallback_history = self._api_fallback_history[-_MAX_OAUTH_HISTORY_MESSAGES:]
        return final_text

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
