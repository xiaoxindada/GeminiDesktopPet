"""本地快捷互动、离线陪伴与 Gemini 官方 API Key 对话服务。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import hashlib
import random
from typing import Any, Optional

from models.character_lore import CharacterLore
from models.pet_settings import (
    DEFAULT_GEMINI_MODEL,
    PetSettings,
    normalize_gemini_model,
    normalize_thinking_level,
    resolve_effective_thinking_level,
)
from models.pet_state import PetState
from services.app_logger import AppLogger
from services.system_monitor_service import SystemMetrics

_MAX_HISTORY_MESSAGES = 20
_GEMINI_API_BASE_URL = "https://generativelanguage.googleapis.com"


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
        self._api_fallback_history: list[dict[str, Any]] = []
        self._last_api_error = ""
        self._failed_key_hash = ""
        self._restored_conversation = False

    @staticmethod
    def is_api_key_error(exc_or_msg: Exception | str) -> bool:
        code = getattr(exc_or_msg, "status_code", getattr(exc_or_msg, "code", None))
        low = str(exc_or_msg).lower()
        return code in (401, 403) or any(term in low for term in (
            "api key not valid", "api_key_invalid", "api key 无效", "permission_denied",
            "unauthenticated", "http 401", "http 403",
        ))

    @staticmethod
    def _key_hash(api_key: str) -> str:
        return hashlib.sha256(api_key.strip().encode("utf-8")).hexdigest()[:16]

    def clear_api_error(self) -> None:
        self._last_api_error = ""
        self._failed_key_hash = ""

    def get_api_error(self, settings: PetSettings) -> str:
        if settings.chat_offline_mode:
            return ""
        if settings.gemini_api_key.strip() and self._failed_key_hash == self._key_hash(settings.gemini_api_key):
            return self._last_api_error
        return ""

    def _record_api_error(self, exc: Exception, api_key: str) -> None:
        if self.is_api_key_error(exc):
            self._last_api_error = self._format_api_error(exc)
            self._failed_key_hash = self._key_hash(api_key)

    def reset_conversation(self) -> None:
        self._last_interaction_id = None
        self._last_session_sig = None
        self._api_fallback_history = []
        self._restored_conversation = False
        self.clear_api_error()

    def restore_conversation(self, messages: list[dict]) -> None:
        """从本地消息恢复上下文，不持久化远端会话标识或连接凭据。"""
        self.reset_conversation()
        self._api_fallback_history = [
            {"role": "user" if message["role"] == "user" else "model",
             "parts": [{"text": message["text"]}]}
            for message in messages
            if message.get("role") in ("user", "assistant") and message.get("text")
        ][-_MAX_HISTORY_MESSAGES:]
        self._restored_conversation = True

    @staticmethod
    def resolve_chat_provider(settings: PetSettings) -> Optional[str]:
        if settings.chat_offline_mode:
            return None
        return "api_key" if (settings.gemini_api_key or "").strip() else None

    def fetch_models_for_chat(self, settings: PetSettings) -> list[str]:
        if settings.chat_offline_mode:
            return []
        try:
            models = self.fetch_api_key_models(settings.gemini_api_key)
        except Exception as exc:
            self._record_api_error(exc, settings.gemini_api_key)
            raise
        self.clear_api_error()
        return models

    @staticmethod
    def _create_api_client(api_key: str, timeout_ms: int):
        from google import genai

        # 显式指定官方 API Key 渠道和地址，避免环境配置切换后端或中转地址。
        return genai.Client(
            vertexai=False,
            api_key=api_key,
            http_options={"base_url": _GEMINI_API_BASE_URL, "timeout": timeout_ms},
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
        api_key = (settings.gemini_api_key or "").strip()
        # 手动离线优先于已保存的 Key，确保聊天不会隐式恢复联网。
        if self.resolve_chat_provider(settings) is None:
            local_reply = self._try_handle_local_intent(text, settings, metrics)
            return local_reply or ChatReply(self._generate_offline_chatter(text), PetState.THINKING)

        strict_cmd = self._try_handle_strict_pet_command(text)
        if strict_cmd is not None:
            return strict_cmd
        model = normalize_gemini_model(settings.gemini_model)
        # 模型不支持的思考档位按该模型默认档发送，避免界面显示与请求行为不一致。
        thinking_level = resolve_effective_thinking_level(model, settings.thinking_level)
        try:
            ai_text = self._call_gemini_api(text, api_key, model, thinking_level=thinking_level)
            self.clear_api_error()
            return ChatReply(ai_text, PetState.HAPPY)
        except Exception as exc:
            AppLogger.error("Chat", f"Gemini API 调用失败 (Model={model}, Thinking={thinking_level})", exc)
            self._record_api_error(exc, api_key)
            return ChatReply(
                f"喵呜…请求失败了（{self._format_api_error(exc)}），请检查 API Key、模型或网络后重试～",
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

        try:
            with cls._create_api_client(cleaned_key, 15000) as client:
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
        from google.genai import errors, types

        level = normalize_thinking_level(thinking_level)
        system_prompt = CharacterLore.build_effective_system_prompt()
        session_sig = (api_key, model, system_prompt)
        if self._last_session_sig != session_sig:
            self._last_interaction_id = None
            if not self._restored_conversation:
                self._api_fallback_history = []
            self._last_session_sig = session_sig

        # 使用官方 google-genai >= 3.2 Interactions API，并按每轮重传 system_instruction 与 generation_config
        with self._create_api_client(api_key, 25000) as client:
            # 本地恢复的会话通过官方多轮 contents 继续，避免误接到另一远端会话。
            if self._restored_conversation or (level == "off" and "2.5" in (model or "").lower()):
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
        if len(self._api_fallback_history) > _MAX_HISTORY_MESSAGES:
            self._api_fallback_history = self._api_fallback_history[-_MAX_HISTORY_MESSAGES:]
        return final_text

    @staticmethod
    def _format_api_error(exc: Exception) -> str:
        code = getattr(exc, "code", None)
        if code is None:
            code = getattr(exc, "status_code", None)
        msg = str(exc).strip()
        low = msg.lower()
        if code == 401 or code == 403:
            return "API Key 无效或权限不足"
        if code == 429:
            return "触发频率或额度限制"
        if code is not None:
            return f"HTTP {code}"
        return msg[:80] if len(msg) > 80 else (msg or type(exc).__name__)
