"""本地快捷互动、离线陪伴与 Gemini 官方 API Key（Gemini API / Agent Platform）对话服务。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import hashlib
import random
import re
from typing import Any, Optional

from models.character_lore import CharacterLore
from models.pet_settings import (
    API_PROVIDER_AGENT_PLATFORM,
    API_PROVIDER_GEMINI_DEVELOPER,
    DEFAULT_AGENT_PLATFORM_API_VERSION,
    DEFAULT_AGENT_PLATFORM_LOCATION,
    DEFAULT_API_PROVIDER,
    DEFAULT_GEMINI_MODEL,
    PetSettings,
    format_api_provider_label,
    normalize_agent_platform_api_version,
    normalize_agent_platform_location,
    normalize_api_provider,
    normalize_gemini_model,
    normalize_thinking_level,
    resolve_effective_thinking_level,
    strip_gemini_model_prefix,
    validate_agent_platform_location,
    validate_agent_platform_project,
)
from models.pet_state import PetState
from services.app_logger import AppLogger
from services.system_monitor_service import SystemMetrics

_MAX_HISTORY_MESSAGES = 20
_GEMINI_API_BASE_URL = "https://generativelanguage.googleapis.com"
_AGENT_PLATFORM_GLOBAL_BASE_URL = "https://aiplatform.googleapis.com"
_AGENT_PLATFORM_MULTI_REGIONS = frozenset({"us", "eu"})
_DEFAULT_RETRY_OPTIONS: dict[str, Any] = {
    "attempts": 2,
    "initial_delay": 0.4,
    "max_delay": 1.5,
}
_SECRET_TOKEN_RE = re.compile(
    r"(?:AIza[0-9A-Za-z_-]{10,}|AQ\.[0-9A-Za-z._-]{10,}|(?<=key=)[^\s&\"']+)"
)
_TLS_NETWORK_ERROR_TOKENS: tuple[str, ...] = (
    "unexpected_eof_while_reading",
    "eof occurred in violation of protocol",
    "remoteprotocolerror",
    "connecterror",
    "ssl",
)
_TIMEOUT_ERROR_TOKENS: tuple[str, ...] = ("timeout", "timed out")
# Agent Platform 纯 API Key 不支持 models.list()，通过零生成的 countTokens 顺序探测以下官方常用模型。
_AGENT_PLATFORM_PROBE_MODELS: tuple[str, ...] = (
    DEFAULT_GEMINI_MODEL,
    "gemini-4-hydrogen",
    "gemini-4-argon",
    "gemini-4-carbon",
    "gemini-3.8-flash",
    "gemini-3.7-flash",
    "gemini-3.5-flash",
    "gemini-3.5-flash-lite",
    "gemini-3.1-pro-preview",
    "gemini-3-flash-preview",
    "gemini-2.5-flash",
    "gemini-2.5-pro",
    "gemini-2.5-flash-lite",
)


@dataclass(frozen=True)
class ApiConnectionConfig:
    """当前生效的官方 API 连接配置快照。"""

    provider: str = DEFAULT_API_PROVIDER
    api_key: str = ""
    project: str = ""
    location: str = DEFAULT_AGENT_PLATFORM_LOCATION
    api_version: str = DEFAULT_AGENT_PLATFORM_API_VERSION


@dataclass(frozen=True)
class ChatReply:
    text: str
    suggested_state: Optional[PetState] = None


class GeminiChatService:
    """通过官方 Gemini SDK 和用户填写的 API Key 提供实时对话。"""

    _api_models_cache: dict[str, list[str]] = {}

    def __init__(self) -> None:
        self._last_interaction_id: Optional[str] = None
        self._last_session_sig: Optional[tuple[str, str, str]] = None
        self._interactions_unsupported_sig: Optional[tuple[str, str, str]] = None
        self._api_fallback_history: list[dict[str, Any]] = []
        self._last_api_error = ""
        self._failed_key_hash = ""
        self._restored_conversation = False

    @staticmethod
    def _extract_http_status_code(exc: Any) -> Optional[int]:
        code = getattr(exc, "code", None)
        if code is None:
            code = getattr(exc, "status_code", None)
        if code is None:
            response = getattr(exc, "response", None)
            if response is not None:
                code = getattr(response, "status_code", None)
        return code if isinstance(code, int) else None

    @staticmethod
    def _is_api_key_unsupported_for_endpoint(exc_or_msg: Exception | str) -> bool:
        low = str(exc_or_msg).lower()
        return any(
            term in low
            for term in (
                "api keys are not supported",
                "api_key_not_supported",
                "api key not supported",
            )
        )

    @staticmethod
    def _is_transient_network_error(exc_or_msg: Exception | str) -> bool:
        low = (
            exc_or_msg.lower()
            if isinstance(exc_or_msg, str)
            else f"{type(exc_or_msg).__name__} {exc_or_msg}".lower()
        )
        return any(
            term in low for term in (*_TLS_NETWORK_ERROR_TOKENS, *_TIMEOUT_ERROR_TOKENS)
        )

    @staticmethod
    def _extract_error_detail(exc: Exception) -> str:
        """提取异常中的诊断消息并脱敏潜在密钥串，避免直接暴露凭据片段。"""
        raw = getattr(exc, "message", None)
        text = raw.strip() if isinstance(raw, str) else ""
        if not text:
            for attr in ("details", "body"):
                payload = getattr(exc, attr, None)
                if isinstance(payload, dict):
                    err_obj = payload.get("error")
                    candidate = (
                        err_obj.get("message")
                        if isinstance(err_obj, dict)
                        else payload.get("message")
                    )
                    if isinstance(candidate, str) and candidate.strip():
                        text = candidate.strip()
                        break
        if not text:
            return ""
        collapsed = " ".join(text.split())
        sanitized = _SECRET_TOKEN_RE.sub("[REDACTED]", collapsed)
        return sanitized[:140] if len(sanitized) > 140 else sanitized

    @classmethod
    def _is_project_wide_403(cls, exc: Exception) -> bool:
        if cls._extract_http_status_code(exc) != 403:
            return False
        low = f"{cls._extract_error_detail(exc)} {exc}".lower()
        return (
            "billing" in low
            or "service_disabled" in low
            or "has not been used in project" in low
            or "api_key_service_blocked" in low
            or ("permission" in low and "denied" in low)
        )

    @staticmethod
    def _get_interactions_api_error_types() -> tuple[type[BaseException], ...]:
        """兼容 google.genai.errors 与 _gaos compat_errors 两套异常基类。"""
        from google.genai import errors

        error_types: list[type[BaseException]] = [errors.APIError]
        try:
            from google.genai._gaos.google_genai import compat_errors

            compat_err = getattr(compat_errors, "APIError", None)
            if isinstance(compat_err, type) and issubclass(compat_err, BaseException):
                error_types.append(compat_err)
            genai_err = getattr(compat_errors, "GenAiError", None)
            if isinstance(genai_err, type) and issubclass(genai_err, BaseException):
                error_types.append(genai_err)
        except Exception:
            pass
        return tuple(dict.fromkeys(error_types))

    @classmethod
    def build_connection_config(
        cls, settings: PetSettings, *, provider: Optional[str] = None
    ) -> ApiConnectionConfig:
        eff_provider = normalize_api_provider(
            provider if provider is not None else getattr(settings, "api_provider", DEFAULT_API_PROVIDER)
        )
        if eff_provider == API_PROVIDER_AGENT_PLATFORM:
            return ApiConnectionConfig(
                provider=eff_provider,
                api_key=(getattr(settings, "agent_platform_api_key", "") or "").strip(),
                project=(getattr(settings, "agent_platform_project", "") or "").strip(),
                location=(getattr(settings, "agent_platform_location", "") or "").strip()
                or DEFAULT_AGENT_PLATFORM_LOCATION,
                api_version=normalize_agent_platform_api_version(
                    getattr(settings, "agent_platform_api_version", DEFAULT_AGENT_PLATFORM_API_VERSION)
                ),
            )
        return ApiConnectionConfig(
            provider=API_PROVIDER_GEMINI_DEVELOPER,
            api_key=(getattr(settings, "gemini_api_key", "") or "").strip(),
        )

    @classmethod
    def has_active_api_key(cls, settings: PetSettings) -> bool:
        return bool(cls.build_connection_config(settings).api_key)

    @staticmethod
    def connection_signature(config: ApiConnectionConfig) -> str:
        cleaned_key = (config.api_key or "").strip()
        if not cleaned_key:
            return ""
        provider = normalize_api_provider(config.provider)
        if provider == API_PROVIDER_AGENT_PLATFORM:
            payload = "|".join(
                (
                    provider,
                    cleaned_key,
                    (config.project or "").strip(),
                    normalize_agent_platform_location(config.location),
                    normalize_agent_platform_api_version(config.api_version),
                )
            )
        else:
            payload = f"{API_PROVIDER_GEMINI_DEVELOPER}|{cleaned_key}"
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]

    @classmethod
    def connection_signature_for_settings(cls, settings: PetSettings) -> str:
        return cls.connection_signature(cls.build_connection_config(settings))

    @staticmethod
    def resolve_agent_platform_endpoint(
        project: str,
        location: str,
        api_version: str = DEFAULT_AGENT_PLATFORM_API_VERSION,
    ) -> tuple[str, Optional[str], Optional[str], str]:
        """校验参数并返回 Agent Platform 官方 (base_url, project, location, api_version)。"""
        cleaned_project = (project or "").strip()
        project_err = validate_agent_platform_project(cleaned_project)
        if project_err:
            raise ValueError(project_err)

        raw_location = (location or "").strip().lower()
        location_err = validate_agent_platform_location(raw_location)
        if location_err:
            raise ValueError(location_err)

        norm_version = normalize_agent_platform_api_version(api_version)
        # Express 模式（未填写 Project ID）时不能只传 location，否则 SDK 会拼出 projects/None/...
        if not cleaned_project:
            return (_AGENT_PLATFORM_GLOBAL_BASE_URL, None, None, norm_version)

        eff_location = normalize_agent_platform_location(raw_location)
        if eff_location == "global":
            base_url = _AGENT_PLATFORM_GLOBAL_BASE_URL
        elif eff_location in _AGENT_PLATFORM_MULTI_REGIONS:
            base_url = f"https://aiplatform.{eff_location}.rep.googleapis.com"
        else:
            base_url = f"https://{eff_location}-aiplatform.googleapis.com"
        return (base_url, cleaned_project, eff_location, norm_version)

    @classmethod
    def describe_connection_endpoint(cls, config: ApiConnectionConfig) -> str:
        provider = normalize_api_provider(config.provider)
        if provider == API_PROVIDER_GEMINI_DEVELOPER:
            return f"官方地址：{_GEMINI_API_BASE_URL}（Gemini API）"
        try:
            base_url, proj, loc, ver = cls.resolve_agent_platform_endpoint(
                config.project, config.location, config.api_version
            )
        except ValueError as exc:
            return f"配置校验提示：{exc}"
        if not proj:
            return f"官方地址：{base_url}/{ver}（Express 模式 · 仅需 API Key）"
        return f"官方地址：{base_url}/{ver}（项目 {proj} · 区域 {loc}）"

    @classmethod
    def is_api_key_error(cls, exc_or_msg: Exception | str) -> bool:
        code = cls._extract_http_status_code(exc_or_msg)
        low = str(exc_or_msg).lower()
        return code in (401, 403) or any(
            term in low
            for term in (
                "api key not valid",
                "api_key_invalid",
                "api key 无效",
                "permission_denied",
                "unauthenticated",
                "http 401",
                "http 403",
                "api keys are not supported",
                "api_key_not_supported",
            )
        )

    def clear_api_error(self) -> None:
        self._last_api_error = ""
        self._failed_key_hash = ""

    def get_api_error(self, settings: PetSettings) -> str:
        if settings.chat_offline_mode:
            return ""
        sig = self.connection_signature_for_settings(settings)
        if sig and self._failed_key_hash == sig:
            return self._last_api_error
        return ""

    def _record_api_error(self, exc: Exception, config: ApiConnectionConfig) -> None:
        is_key_err = self.is_api_key_error(exc) or (
            exc.__cause__ is not None and self.is_api_key_error(exc.__cause__)
        )
        if is_key_err:
            source_exc = (
                exc.__cause__
                if isinstance(exc.__cause__, Exception)
                and self.is_api_key_error(exc.__cause__)
                and not self.is_api_key_error(exc)
                else exc
            )
            self._last_api_error = self._format_api_error(source_exc, provider=config.provider)
            self._failed_key_hash = self.connection_signature(config)

    def reset_conversation(self) -> None:
        self._last_interaction_id = None
        self._last_session_sig = None
        self._interactions_unsupported_sig = None
        self._api_fallback_history = []
        self._restored_conversation = False
        self.clear_api_error()

    def restore_conversation(self, messages: list[dict]) -> None:
        """从本地消息恢复上下文，不持久化远端会话标识或连接凭据。"""
        self.reset_conversation()
        self._api_fallback_history = [
            {
                "role": "user" if message["role"] == "user" else "model",
                "parts": [{"text": message["text"]}],
            }
            for message in messages
            if message.get("role") in ("user", "assistant") and message.get("text")
        ][-_MAX_HISTORY_MESSAGES:]
        self._restored_conversation = True

    @classmethod
    def resolve_chat_provider(cls, settings: PetSettings) -> Optional[str]:
        if settings.chat_offline_mode:
            return None
        config = cls.build_connection_config(settings)
        return config.provider if config.api_key else None

    def fetch_models_for_chat(self, settings: PetSettings) -> list[str]:
        if settings.chat_offline_mode:
            return []
        config = self.build_connection_config(settings)
        extra = (settings.gemini_model,) if (settings.gemini_model or "").strip() else ()
        try:
            models = self.fetch_models_for_config(config, extra_models=extra)
        except Exception as exc:
            self._record_api_error(exc, config)
            raise
        self.clear_api_error()
        return models

    @classmethod
    def _create_api_client(cls, config: ApiConnectionConfig, timeout_ms: int):
        from google import genai

        cleaned_key = (config.api_key or "").strip()
        if normalize_api_provider(config.provider) == API_PROVIDER_AGENT_PLATFORM:
            base_url, project, location, api_version = cls.resolve_agent_platform_endpoint(
                config.project, config.location, config.api_version
            )
            # 显式传入 official base_url、api_version 与重试参数，避免并发/网络抖动触发 SSL EOF。
            kwargs: dict[str, Any] = {
                "enterprise": True,
                "api_key": cleaned_key,
                "http_options": {
                    "base_url": base_url,
                    "api_version": api_version,
                    "timeout": timeout_ms,
                    "retry_options": dict(_DEFAULT_RETRY_OPTIONS),
                },
            }
            if project:
                kwargs["project"] = project
                kwargs["location"] = location
            return genai.Client(**kwargs)

        # 显式指定官方 Gemini API 渠道和地址，避免环境配置切换后端或中转地址。
        return genai.Client(
            vertexai=False,
            api_key=cleaned_key,
            http_options={
                "base_url": _GEMINI_API_BASE_URL,
                "timeout": timeout_ms,
                "retry_options": dict(_DEFAULT_RETRY_OPTIONS),
            },
        )

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
        # 手动离线优先于已保存的 Key，确保聊天不会隐式恢复联网。
        if self.resolve_chat_provider(settings) is None:
            local_reply = self._try_handle_local_intent(text, settings, metrics)
            return local_reply or ChatReply(self._generate_offline_chatter(text), PetState.THINKING)

        strict_cmd = self._try_handle_strict_pet_command(text)
        if strict_cmd is not None:
            return strict_cmd
        config = self.build_connection_config(settings)
        model = normalize_gemini_model(settings.gemini_model)
        # 模型不支持的思考档位按该模型默认档发送，避免界面显示与请求行为不一致。
        thinking_level = resolve_effective_thinking_level(model, settings.thinking_level)
        try:
            ai_text = self._call_gemini_api(text, config, model, thinking_level=thinking_level)
            self.clear_api_error()
            return ChatReply(ai_text, PetState.HAPPY)
        except Exception as exc:
            AppLogger.error(
                "Chat",
                f"Gemini API 调用失败 (Provider={config.provider}, Model={model}, Thinking={thinking_level})",
                exc,
            )
            self._record_api_error(exc, config)
            return ChatReply(
                f"喵呜…请求失败了（{self._format_api_error(exc, provider=config.provider)}），请检查 API Key、模型或网络后重试～",
                PetState.OOPS,
            )

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
                    "让我想想喵…嗯，我觉得很有道理✦（想让我真正联网回答，可以在「设置」里填上 Gemini API Key 哦）",
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

    @classmethod
    def _extract_chat_model_id(cls, name: str, actions: Any) -> Optional[str]:
        cleaned = strip_gemini_model_prefix(name)
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
            "bison",
            "gecko",
            "chirp",
            "medlm",
            "decision",
            "nano-banana",
            "omni-",
        )
        if any(token in low for token in excluded_tokens):
            return None

        if isinstance(actions, (list, tuple, set)):
            action_set = {str(a).strip() for a in actions}
            if action_set and not ({"generateContent", "createInteraction"} & action_set):
                return None

        return cleaned

    @classmethod
    def _probe_agent_platform_models(
        cls,
        client: Any,
        config: ApiConnectionConfig,
        *,
        extra_models: tuple[str, ...] = (),
    ) -> list[str]:
        """当 Agent Platform models.list 不可用时，复用单连接顺序探测支持 count_tokens 的模型。"""
        candidates: list[str] = []
        for raw_id in (*_AGENT_PLATFORM_PROBE_MODELS, *extra_models):
            cleaned = strip_gemini_model_prefix(raw_id)
            if cleaned and cleaned not in candidates:
                candidates.append(cleaned)

        discovered: list[str] = []
        errors_seen: list[Exception] = []
        consecutive_net_errors = 0

        # 复用单连接顺序探测，避免 httpx2 多线程共享连接池触发 SSL EOF，并在鉴权/连错时提前退出。
        for model_id in candidates:
            try:
                client.models.count_tokens(model=model_id, contents="hi")
                discovered.append(model_id)
                consecutive_net_errors = 0
            except Exception as exc:
                errors_seen.append(exc)
                low = str(exc).lower()
                if (
                    cls._extract_http_status_code(exc) == 401
                    or cls._is_api_key_unsupported_for_endpoint(exc)
                    or cls._is_project_wide_403(exc)
                    or "api_key_invalid" in low
                    or "api key not valid" in low
                ):
                    break
                if cls._is_transient_network_error(exc):
                    consecutive_net_errors += 1
                    if consecutive_net_errors >= 2:
                        break
                else:
                    consecutive_net_errors = 0

        if discovered:
            return discovered

        if errors_seen:
            unsupported_err = next(
                (err for err in errors_seen if cls._is_api_key_unsupported_for_endpoint(err)),
                None,
            )
            if (config.project or "").strip() and unsupported_err is not None:
                raise RuntimeError(
                    "当前项目路径不支持纯 API Key（若使用 Express 模式 Key，请清空 Project ID 后重试）"
                ) from unsupported_err

            key_err = next((err for err in errors_seen if cls.is_api_key_error(err)), None)
            if key_err is not None:
                raise RuntimeError(
                    cls._format_api_error(key_err, provider=API_PROVIDER_AGENT_PLATFORM)
                ) from key_err

            if all(cls._extract_http_status_code(err) == 404 for err in errors_seen):
                raise RuntimeError(
                    "未在当前配置下探测到可用模型（HTTP 404），请检查 Project ID、区域 Location 或 API 版本，也可直接在对话模型框手动输入模型 ID"
                ) from errors_seen[0]

            raise RuntimeError(
                cls._format_api_error(errors_seen[0], provider=API_PROVIDER_AGENT_PLATFORM)
            ) from errors_seen[0]

        raise RuntimeError("未能探测到可用模型，请直接在对话模型框手动输入模型 ID")

    @classmethod
    def fetch_models_for_config(
        cls,
        config: ApiConnectionConfig,
        *,
        extra_models: tuple[str, ...] = (),
    ) -> list[str]:
        """使用指定官方 API 连接配置获取支持对话的模型 ID 列表。"""
        provider = normalize_api_provider(config.provider)
        cleaned_key = (config.api_key or "").strip()
        if not cleaned_key:
            raise ValueError(f"请先填写 {format_api_provider_label(provider)} Key")

        if provider == API_PROVIDER_AGENT_PLATFORM:
            cls.resolve_agent_platform_endpoint(
                config.project, config.location, config.api_version
            )

        norm_config = ApiConnectionConfig(
            provider=provider,
            api_key=cleaned_key,
            project=(config.project or "").strip(),
            location=config.location,
            api_version=config.api_version,
        )

        discovered: list[str] = []
        with cls._create_api_client(norm_config, 15000) as client:
            raw_models: Optional[list[Any]] = None
            try:
                raw_models = list(client.models.list(config={"page_size": 100}))
            except Exception as exc:
                code = cls._extract_http_status_code(exc)
                if provider == API_PROVIDER_AGENT_PLATFORM and (
                    code in (401, 403, 404, 405, 501)
                    or cls._is_api_key_unsupported_for_endpoint(exc)
                ):
                    discovered = cls._probe_agent_platform_models(
                        client, norm_config, extra_models=extra_models
                    )
                else:
                    raise RuntimeError(cls._format_api_error(exc, provider=provider)) from exc

            if raw_models is not None:
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
        sig = cls.connection_signature(norm_config)
        if sig:
            cls._api_models_cache[sig] = list(discovered)
        return discovered

    @classmethod
    def fetch_api_key_models(cls, api_key: str) -> list[str]:
        """兼容单 Key 调用的官方模型列表获取入口。"""
        return cls.fetch_models_for_config(
            ApiConnectionConfig(provider=API_PROVIDER_GEMINI_DEVELOPER, api_key=api_key)
        )

    @classmethod
    def get_cached_models_for_config(cls, config: ApiConnectionConfig) -> list[str]:
        sig = cls.connection_signature(config)
        if not sig:
            return []
        return list(cls._api_models_cache.get(sig, []))

    @classmethod
    def get_cached_models_for_settings(cls, settings: PetSettings) -> list[str]:
        return cls.get_cached_models_for_config(cls.build_connection_config(settings))

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

    @classmethod
    def _should_fallback_from_interactions(
        cls, exc: BaseException, *, is_agent_platform: bool
    ) -> bool:
        code = cls._extract_http_status_code(exc)
        if code in (400, 404, 405, 501):
            return True
        return bool(
            is_agent_platform
            and code in (401, 403)
            and cls._is_api_key_unsupported_for_endpoint(exc)
        )

    def _call_gemini_api(
        self,
        prompt: str,
        config: ApiConnectionConfig,
        model: str,
        thinking_level: str = "auto",
    ) -> str:
        from google.genai import types

        level = normalize_thinking_level(thinking_level)
        system_prompt = CharacterLore.build_effective_system_prompt()
        config_sig = self.connection_signature(config)
        session_sig = (config_sig, model, system_prompt)
        if self._last_session_sig != session_sig:
            self._last_interaction_id = None
            if not self._restored_conversation:
                self._api_fallback_history = []
            self._last_session_sig = session_sig

        is_agent_platform = normalize_api_provider(config.provider) == API_PROVIDER_AGENT_PLATFORM
        is_agent_express = is_agent_platform and not (config.project or "").strip()
        interaction_errors = self._get_interactions_api_error_types()

        # 使用官方 google-genai >= 3.2 Interactions API，并按每轮重传 system_instruction 与 generation_config
        with self._create_api_client(config, 25000) as client:
            # Express 模式仅开放 generateContent；恢复会话或 2.5 关闭思考也直接走 generateContent 多轮。
            if (
                is_agent_express
                or self._restored_conversation
                or (level == "off" and "2.5" in (model or "").lower())
                or self._interactions_unsupported_sig == session_sig
            ):
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

            kwargs: dict[str, Any] = {
                "model": model,
                "input": prompt,
                "system_instruction": system_prompt,
                "generation_config": gen_config,
            }
            if self._last_interaction_id:
                kwargs["previous_interaction_id"] = self._last_interaction_id

            interaction = None
            failed_err: Optional[BaseException] = None
            try:
                interaction = client.interactions.create(**kwargs)
            except interaction_errors as api_err:
                failed_err = api_err
                code = self._extract_http_status_code(api_err)
                # 若上一轮会话 ID 失效（过期或切换 Key），清掉上下文后单轮重试
                if self._last_interaction_id and code in (400, 404):
                    self._last_interaction_id = None
                    kwargs.pop("previous_interaction_id", None)
                    try:
                        interaction = client.interactions.create(**kwargs)
                        failed_err = None
                    except interaction_errors as retry_err:
                        failed_err = retry_err

            if failed_err is not None:
                if self._should_fallback_from_interactions(
                    failed_err, is_agent_platform=is_agent_platform
                ):
                    if is_agent_platform:
                        self._interactions_unsupported_sig = session_sig
                    return self._call_gemini_generate_content_fallback(
                        client,
                        types,
                        prompt=prompt,
                        model=model,
                        system_prompt=system_prompt,
                        thinking_level=level,
                    )
                raise failed_err

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
            if (
                "thinking_config" in cfg_kwargs
                and self._extract_http_status_code(err) == 400
                and any(tok in err_low for tok in ("thinking", "level", "budget", "xhigh"))
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
        if len(self._api_fallback_history) > _MAX_HISTORY_MESSAGES:
            self._api_fallback_history = self._api_fallback_history[-_MAX_HISTORY_MESSAGES:]
        return final_text

    @classmethod
    def _format_api_error(cls, exc: Exception, *, provider: Optional[str] = None) -> str:
        code = cls._extract_http_status_code(exc)
        msg = str(exc).strip()
        if cls._is_api_key_unsupported_for_endpoint(exc):
            return "当前接口不支持纯 API Key 鉴权（请确认 Key 类型或填写 Express / 授权 API Key）"
        if code in (401, 403):
            detail = cls._extract_error_detail(exc)
            detail_low = detail.lower()
            if "billing" in detail_low:
                return f"API Key 权限受限（HTTP {code}）：当前项目未启用结算（Billing），请在 Google Cloud 绑定结算账号"
            if "has not been used in project" in detail_low or "service_disabled" in detail_low:
                return f"API Key 权限受限（HTTP {code}）：当前项目未启用 Agent Platform API（aiplatform.googleapis.com）"
            if "api_key_service_blocked" in detail_low or (
                "requests to this api" in detail_low and "blocked" in detail_low
            ):
                return f"API Key 权限受限（HTTP {code}）：该密钥的 API 限制未放行当前接口（{detail}）"
            if "permission" in detail_low and "denied" in detail_low:
                return f"API Key 权限不足（HTTP {code}）：请为绑定服务账号授予 Agent Platform User（roles/aiplatform.user）角色（{detail}）"
            if detail:
                return f"API Key 无效或权限不足（HTTP {code}：{detail}）"
            return f"API Key 无效或权限不足（HTTP {code}）"
        if code == 404 and provider == API_PROVIDER_AGENT_PLATFORM:
            return "未找到模型或项目区域（HTTP 404），请检查模型名、Project ID 与 Location"
        if code == 429:
            return "触发频率或额度限制"
        if code is not None:
            return f"HTTP {code}"
        low = f"{type(exc).__name__} {msg}".lower()
        if any(tok in low for tok in _TLS_NETWORK_ERROR_TOKENS):
            return "TLS/网络连接中断（SSL EOF），请检查代理或网络后重试"
        if any(tok in low for tok in _TIMEOUT_ERROR_TOKENS):
            return "网络请求超时，请检查代理或网络后重试"
        return msg[:80] if len(msg) > 80 else (msg or type(exc).__name__)
