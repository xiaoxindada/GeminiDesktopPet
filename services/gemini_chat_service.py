"""本地快捷意图对话、离线台词陪伴，以及基于 google-genai Interactions API 的实时 Gemini 对话服务。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import random
from typing import Optional

from models.character_lore import CharacterLore
from models.pet_settings import (
    DEFAULT_GEMINI_MODEL,
    PetSettings,
    normalize_gemini_model,
)
from models.pet_state import PetState
from services.app_logger import AppLogger
from services.system_monitor_service import SystemMetrics


@dataclass(frozen=True)
class ChatReply:
    text: str
    suggested_state: Optional[PetState] = None


class GeminiChatService:
    """结合本地即时指令与官方 google-genai Interactions API（gemini-4-hydrogen）的聊天服务。"""

    def __init__(self) -> None:
        self._last_interaction_id: Optional[str] = None
        self._last_session_sig: Optional[tuple[str, str, str]] = None

    def reset_conversation(self) -> None:
        self._last_interaction_id = None
        self._last_session_sig = None

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

        api_key = (settings.gemini_api_key or "").strip()
        if api_key:
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
        if code == 401 or code == 403:
            return "API Key 无效或权限不足"
        if code == 429:
            return "触发频率或额度限制"
        if code is not None:
            return f"HTTP {code}"
        msg = str(exc).strip()
        return msg[:80] if len(msg) > 80 else (msg or type(exc).__name__)
