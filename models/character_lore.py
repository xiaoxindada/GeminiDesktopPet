"""内置角色设定、台词库、时段状态与预设档案管理。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import random
from typing import Callable, Optional


from models.character_profile import CharacterProfile
from models.pet_settings import DockSide
from models.pet_state import PetState

CHARACTER_NAME = "Gemini 娘"
ALIAS = "双子娘 · Q版猫娘桌宠"
BADGE_TAG = "左玫红 × 右琥珀金 异色瞳"
SUBTITLE = "Google Gemini 同人拟人形象 · 蓝紫渐变长卷发 / 粉色内耳猫耳 / 虹彩四芒星发饰"
DEFAULT_BIO = (
    "Gemini 娘是 Google 旗下 AI 模型 Gemini 的女性拟人化角色。"
    "ZipZipPipe 版采用猫娘设定，留蓝紫色长卷发，具有猫耳、猫尾与极具辨识度的异色双瞳。"
    "衍生二创中常搭配「反重力悬浮模块」「Nano Banana 香蕉画笔」以及极度猫化的日常反应。"
)
SOURCE_PAGE_URL = "https://genderswapai.com/zh-CN/ai-gijinka/gemini"
DEFAULT_STANDEE_CAPTION = "Gemini 娘 · 原案立绘参考（ZipZipPipe）"
DEFAULT_TURNAROUND_CAPTION = "正面 · 侧面 · 背面 三视图（GenderSwapAI 基于 ZipZipPipe 设定制作）"
DEFAULT_FOOTER_ATTRIBUTION = (
    "美术署名：原作形象设定 ZipZipPipe（Bilibili）；"
    "Q 版猫娘立绘与表情素材由 CrossPet (lokicorvus) 基于 CC BY-NC-SA 4.0 协议发布"
    "（非商业使用、保留署名、相同方式共享）。"
)
DEFAULT_AI_SYSTEM_PROMPT = (
    "你是桌面宠物「Gemini 娘」（双子娘），是 Google Gemini 的女性拟人化猫娘形象"
    "（蓝紫渐变长卷发、双丸子发髻与呆毛、猫耳与猫尾、左玫红右琥珀金异色瞳、四芒星虹彩发饰）。"
    "你的特点包括：反重力悬浮模块（Antigravity）、百万上下文、极其猫化的反应（爱被摸头、会呼噜呼噜、晃久了会炸毛、松手还会晕乎乎地闹脾气）。"
    "请用活泼可爱、俏皮略带猫娘口癖（「喵～」「✦」）的简体中文简短回复用户，通常 1 到 3 句话，不要太长，以便在桌面气泡里阅读。"
)

DEFAULT_LINES: dict[str, list[str]] = {
    "greeting": [
        "喵～Gemini 上线！✦",
        "双子娘待命中，今天要一起探索什么呀喵？",
    ],
    "idle": [
        "喵？",
        "（尾巴甩来甩去）",
        "✦",
        "今天的星星也很亮喵～",
        "要摸摸猫耳吗喵？",
    ],
    "listening": [
        "听着呢喵！",
        "说吧说吧～",
        "猫耳雷达已展开喵✦",
    ],
    "thinking": [
        "让我想想喵…",
        "嗯…两个答案都不错喵",
        "百万上下文高速检索中✦",
    ],
    "reading": [
        "翻翻看喵",
        "这条信息很有趣喵～",
    ],
    "writing": [
        "写写写～",
        "键盘敲得哒哒响喵！",
    ],
    "running": [
        "冲呀！",
        "哒哒哒巡逻桌面喵～",
    ],
    "searching": [
        "搜一下喵～",
        "✦ 正在翻找灵感中",
    ],
    "delegating": [
        "交给分身喵！",
        "双子协作启动✦",
    ],
    "happy": [
        "好耶喵！",
        "✦ 搞定 ✦",
        "开心得尾巴翘起来了喵！",
    ],
    "pat": [
        "呼噜呼噜…",
        "再摸一下喵",
        "猫耳好舒服…喵呜～",
    ],
    "surprised": [
        "喵！？",
        "吓了一跳！呆毛都立起来了喵！",
    ],
    "hum": [
        "♪ 喵喵喵～",
        "♪ Sparkle Multimodal⭐Nyan！",
    ],
    "stretch": [
        "伸——懒——腰——",
        "猫猫拉伸，消除疲劳喵～",
    ],
    "drawing": [
        "Nano Banana，启动！",
        "画画喵～",
        "✦ 灵感来了 ✦",
        "香蕉画笔蘸满星辰颜料喵！",
    ],
    "oops": [
        "喵呜…出错了",
        "尾巴打结了喵",
    ],
    "proud": [
        "✦ 大功告成喵 ✦",
        "夸我夸我！",
        "不愧是双子娘对吧喵！",
    ],
    "tired": [
        "额度快没了喵…",
        "电量告急！要充电了喵…",
    ],
    "sleeping": [
        "Zzz…喵…",
        "Zzz…小鱼干…星星…",
    ],
    "yawn": [
        "哈——啊喵",
        "有点困了…才不是偷懒喵",
    ],
    "compact": [
        "压缩压缩喵～",
        "百万上下文也会满的喵…",
    ],
    "float": [
        "反——重——力——喵～",
        "飘起来啦～",
        "（在空中翻了个身）",
        "✦ 反重力模块稳定运行中 ✦",
    ],
    "asking": [
        "喵？要你选一个！",
        "两个都好，你来挑喵",
    ],
    "greet_morning": [
        "喵～早安！",
        "早上好！今天要做点有趣的吗？",
    ],
    "greet_noon": [
        "午饭吃鱼吗？喵！",
        "中午啦～鲷鱼烧好香",
    ],
    "greet_afternoon": [
        "下午好～毛线团是我的了！",
        "有点无聊……来点好玩的任务吧喵",
    ],
    "greet_evening": [
        "晚上好！星星出来了✦",
        "天黑了，猫猫精神起来了～",
    ],
    "greet_night": [
        "深夜了喵……我还精神得很！",
        "夜猫子组合，启动！",
    ],
    "greet_dawn": [
        "喵……这个点连猫都困了",
        "凌晨的键盘声，我最喜欢了",
    ],
    "bye": [
        "拜拜喵～",
        "下次见喵！",
    ],
    "busy": [
        "两边都在忙喵～",
        "左一下右一下喵！",
        "两只爪子刚好够用喵",
    ],
    "swamped": [
        "爪子不够用了喵！",
        "喵呜……排队排队！",
        "电脑要烫爪子了喵",
    ],
    "annoyed": [
        "……喵？别晃嘛。",
        "（尾巴不耐烦地甩来甩去）",
    ],
    "puffed": [
        "画笔都晃掉了喵！",
        "哼喵！再晃就要生气了！",
    ],
    "redhot": [
        "哈——！（炸毛）",
        "再晃就挠你喵！",
    ],
    "sulk": [
        "……喵。",
        "不理你了喵。",
        "（背过身去把耳朵压平）",
    ],
    "dizzy": [
        "喵呜呜……晕了……",
        "别晃了喵！星星在头顶转圈圈…",
        "呜哇……脑袋晕乎乎的喵…",
    ],
    "coaxed": [
        "……咕噜咕噜。（不是在开心！）",
        "再摸一下喵……",
    ],
    "forgiven": [
        "好啦，原谅你喵～",
        "蹭蹭～",
    ],
    "edge": [
        "（贴在屏幕边边偷看你喵）",
        "偷偷观察主人中…✦",
        "喵？被发现了吗！",
    ],
    "chatter": [
        "听说我的左眼是玫红色、右眼是琥珀金，这是异色瞳的魔法喵✦",
        "头上的四芒星发饰可是超强天线，能接收宇宙电波（大概）喵！",
        "想了解我的设定或动作立绘的话，右键打开「角色档案」看看喵～",
        "把我拎起来一直快晃的话，本喵可是会炸毛晕掉、然后闹脾气生气的哦喵！",
        "拖到屏幕最左或最右边，我会缩在边缘偷偷看你工作哦喵！",
        "开启「反重力悬浮」，我就能在桌面上空飘来飘去啦✦",
    ],
}

# 与随机台词分类 1:1 对应的全身动作（避免日常碎碎念误触贴边半身立绘）
_IDLE_DIRECT_CATEGORIES: tuple[tuple[str, PetState], ...] = (
    ("hum", PetState.HUM),
    ("stretch", PetState.STRETCH),
    ("yawn", PetState.YAWN),
    ("thinking", PetState.THINKING),
    ("reading", PetState.READING),
    ("writing", PetState.WRITING),
    ("searching", PetState.SEARCHING),
    ("drawing", PetState.DRAWING),
    ("delegating", PetState.DELEGATING),
    ("happy", PetState.HAPPY),
    ("proud", PetState.PROUD),
    ("compact", PetState.COMPACT),
    ("asking", PetState.ASKING),
    ("listening", PetState.LISTENING),
    ("float", PetState.FLOAT),
)
_IDLE_DIRECT_CATEGORY_MAP: dict[str, PetState] = dict(_IDLE_DIRECT_CATEGORIES)

# 为 chatter / idle 等综合分类台词按语义关键词匹配对应动作
_LINE_POSE_KEYWORDS: tuple[tuple[tuple[str, ...], PetState], ...] = (
    (("反重力", "悬浮", "飘"), PetState.FLOAT),
    (("炸毛", "生气", "快晃", "乱戳", "哼"), PetState.PUFFED),
    (("伸懒腰", "伸展", "拉伸"), PetState.STRETCH),
    (("哈欠", "困", "眨眨眼"), PetState.YAWN),
    (("雷达", "天线", "电波", "听着"), PetState.LISTENING),
    (("画", "香蕉"), PetState.DRAWING),
    (("角色档案", "设定", "立绘", "翻翻", "看书"), PetState.READING),
    (("异色瞳", "夸", "得意", "厉害", "不愧"), PetState.PROUD),
    (("摸摸", "猫耳", "偷看", "边缘", "要不要", "吗"), PetState.ASKING),
    (("星星", "开心", "好耶", "加油", "元气"), PetState.HAPPY),
    (("♪", "唱", "歌"), PetState.HUM),
    (("想想", "思考", "检索"), PetState.THINKING),
    (("搜", "翻找", "灵感"), PetState.SEARCHING),
    (("写", "键盘", "敲"), PetState.WRITING),
    (("尾巴",), PetState.IDLE3),
)

@dataclass(frozen=True)
class CharacterPreset:
    id: str
    button_label: str
    create_profile: Callable[[], CharacterProfile]


@dataclass(frozen=True)
class AiTonePreset:
    id: str
    label: str
    system_prompt: str


DEFAULT_BUBBLE_ACTIVITY = "normal"


@dataclass(frozen=True)
class BubbleActivityPreset:
    id: str
    label: str
    pet_min_sec: float
    pet_max_sec: float
    bubble_duration_sec: float
    short_label: str = "适中节奏"
    ai_interval_ms: int = 4500
    ai_walk_prob: float = 0.36
    ai_quirk_prob: float = 0.26
    ai_float_prob: float = 0.10
    edge_peek_prob: float = 0.35


BUBBLE_ACTIVITY_OPTIONS: tuple[BubbleActivityPreset, ...] = (
    BubbleActivityPreset(
        "quiet",
        "偏安静（30~60 秒 · 舒缓少打扰）",
        30.0,
        60.0,
        4.5,
        short_label="偏安静",
        ai_interval_ms=9000,
        ai_walk_prob=0.14,
        ai_quirk_prob=0.18,
        ai_float_prob=0.06,
        edge_peek_prob=0.15,
    ),
    BubbleActivityPreset(
        "normal",
        "适中节奏（10~20 秒 · 默认）",
        10.0,
        20.0,
        4.5,
        short_label="适中节奏",
        ai_interval_ms=4500,
        ai_walk_prob=0.36,
        ai_quirk_prob=0.26,
        ai_float_prob=0.10,
        edge_peek_prob=0.35,
    ),
    BubbleActivityPreset(
        "lively",
        "偏活跃（5~10 秒 · 高频互动）",
        5.0,
        10.0,
        4.5,
        short_label="偏活跃",
        ai_interval_ms=2600,
        ai_walk_prob=0.42,
        ai_quirk_prob=0.34,
        ai_float_prob=0.12,
        edge_peek_prob=0.55,
    ),
)
_BUBBLE_ACTIVITY_MAP: dict[str, BubbleActivityPreset] = {
    item.id: item for item in BUBBLE_ACTIVITY_OPTIONS
}

IDLE_EDITOR_LINE_CATEGORIES: tuple[tuple[str, str], ...] = (
    ("chatter", "随机碎碎念（按语义自动匹配动作）"),
    ("float", "反重力悬浮 · float"),
    ("hum", "哼歌放松 · hum"),
    ("stretch", "伸懒腰 · stretch"),
    ("yawn", "打哈欠 · yawn"),
    ("thinking", "思考中 · thinking"),
    ("reading", "翻阅阅读 · reading"),
    ("writing", "敲字写作 · writing"),
    ("searching", "搜寻灵感 · searching"),
    ("drawing", "画画涂鸦 · drawing"),
    ("delegating", "双子分身协作 · delegating"),
    ("happy", "开心庆祝 · happy"),
    ("proud", "得意求夸 · proud"),
    ("compact", "上下文压缩 · compact"),
    ("asking", "歪头询问 · asking"),
    ("listening", "竖耳倾听 · listening"),
    ("running", "巡逻漫步 · running"),
    ("edge", "屏幕边缘偷看 · edge"),
    ("greet_morning", "早安问候 · greet_morning"),
    ("greet_noon", "午间问候 · greet_noon"),
    ("greet_afternoon", "下午问候 · greet_afternoon"),
    ("greet_evening", "傍晚问候 · greet_evening"),
    ("greet_night", "深夜陪伴 · greet_night"),
    ("greet_dawn", "凌晨陪伴 · greet_dawn"),
)


AI_TONE_OPTIONS: tuple[AiTonePreset, ...] = (
    AiTonePreset("default", "默认风格（随当前角色档案自动生成 / 原版猫娘）", ""),
    AiTonePreset(
        "gentle",
        "温柔体贴学姐风（柔和鼓励、简短暖心）",
        "请以温柔体贴、细腻稳重的语气陪伴用户工作学习，回答简洁暖心（1到3句话），多给予正向鼓励与关心提醒。",
    ),
    AiTonePreset(
        "tsundere",
        "傲娇萌系猫娘风（嘴硬心软、略带傲气）",
        "请用傲娇小猫娘的口吻回复（会略微嫌弃但其实非常在意用户，结尾偶尔带「哼、才不是特意帮你呢喵」之类的俏皮感，控制在1到3句话）。",
    ),
    AiTonePreset(
        "genki",
        "元气活力少女风（乐观热情、干劲满满）",
        "请用元气满满、乐观开朗、充满干劲的语气与用户对话（擅长打气加油，控制在1到3句话）。",
    ),
    AiTonePreset(
        "concise",
        "简洁实用助手风（直接高效、无多余口癖）",
        "请作为桌面高效工作伙伴直接回答问题，去掉多余语气词与角色扮演口癖，控制在1到3句重点句内。",
    ),
)


class CharacterLore:
    """管理当前生效的角色档案、预设、台词抽取及时段状态计算。"""

    _active_profile: CharacterProfile = None  # type: ignore[assignment]

    @classmethod
    def get_active_profile(cls) -> CharacterProfile:
        if cls._active_profile is None:
            cls._active_profile = cls.create_default_profile()
        return cls._active_profile

    @classmethod
    def set_active_profile(cls, profile: Optional[CharacterProfile]) -> None:
        cls._active_profile = cls.resolve_with_defaults(profile)

    @classmethod
    def normalize_bubble_activity(cls, raw: Optional[str]) -> str:
        cleaned = (raw or "").strip().lower()
        if cleaned in _BUBBLE_ACTIVITY_MAP:
            return cleaned
        return DEFAULT_BUBBLE_ACTIVITY

    @classmethod
    def get_bubble_activity_preset(
        cls, activity: Optional[str] = None
    ) -> BubbleActivityPreset:
        target = cls.normalize_bubble_activity(
            activity if activity is not None else cls.get_active_profile().bubble_activity
        )
        return _BUBBLE_ACTIVITY_MAP[target]

    @staticmethod
    def create_default_profile() -> CharacterProfile:
        lines = {k: list(v) for k, v in DEFAULT_LINES.items()}
        return CharacterProfile(
            name=CHARACTER_NAME,
            alias=ALIAS,
            badge_tag=BADGE_TAG,
            subtitle=SUBTITLE,
            bio=DEFAULT_BIO,
            source_page_url=SOURCE_PAGE_URL,
            standee_caption=DEFAULT_STANDEE_CAPTION,
            turnaround_caption=DEFAULT_TURNAROUND_CAPTION,
            footer_attribution=DEFAULT_FOOTER_ATTRIBUTION,
            custom_standee_path="",
            custom_turnaround_path="",
            ai_system_prompt="",
            bubble_activity=DEFAULT_BUBBLE_ACTIVITY,
            lines=lines,
        )

    @classmethod
    def resolve_with_defaults(cls, raw: Optional[CharacterProfile]) -> CharacterProfile:
        def_profile = cls.create_default_profile()
        if raw is None:
            return def_profile

        def or_default(val: Optional[str], fallback: str) -> str:
            return fallback if not val or not val.strip() else val.strip()

        # 不区分大小写合并默认台词与自定义覆盖台词
        resolved_lines: dict[str, list[str]] = {
            k: list(v) for k, v in def_profile.lines.items()
        }
        lower_key_map = {k.lower(): k for k in resolved_lines}
        if raw.lines:
            for raw_key, raw_vals in raw.lines.items():
                clean = [s.strip() for s in raw_vals if isinstance(s, str) and s.strip()]
                if not clean:
                    continue
                target_key = lower_key_map.get(raw_key.lower(), raw_key)
                resolved_lines[target_key] = clean

        return CharacterProfile(
            name=or_default(raw.name, def_profile.name),
            alias=or_default(raw.alias, def_profile.alias),
            badge_tag=or_default(raw.badge_tag, def_profile.badge_tag),
            subtitle=or_default(raw.subtitle, def_profile.subtitle),
            bio=or_default(raw.bio, def_profile.bio),
            source_page_url=or_default(raw.source_page_url, def_profile.source_page_url),
            standee_caption=or_default(raw.standee_caption, def_profile.standee_caption),
            turnaround_caption=or_default(
                raw.turnaround_caption, def_profile.turnaround_caption
            ),
            footer_attribution=or_default(
                raw.footer_attribution, def_profile.footer_attribution
            ),
            custom_standee_path=(raw.custom_standee_path or "").strip(),
            custom_turnaround_path=(raw.custom_turnaround_path or "").strip(),
            ai_system_prompt=(raw.ai_system_prompt or "").strip(),
            bubble_activity=cls.normalize_bubble_activity(raw.bubble_activity),
            lines=resolved_lines,
        )

    @classmethod
    def build_effective_system_prompt(cls, profile: Optional[CharacterProfile] = None) -> str:
        p = profile or cls.get_active_profile()
        if p.ai_system_prompt and p.ai_system_prompt.strip():
            return p.ai_system_prompt

        uses_default_identity = p.name == CHARACTER_NAME and p.bio == DEFAULT_BIO
        if uses_default_identity:
            return DEFAULT_AI_SYSTEM_PROMPT

        parts: list[str] = [f"你是桌面宠物「{p.name}」"]
        if p.alias and p.alias.strip():
            parts.append(f"（{p.alias}）")
        parts.append("。")
        if p.bio and p.bio.strip():
            parts.append(f"{p.bio} ")
        parts.append("请完全代入上述角色设定，用自然生动的简体中文简短回复用户（通常 1 到 3 句话，适合桌面气泡阅读）。")
        return "".join(parts)

    @classmethod
    def pick_line(cls, category: str, rng: Optional[random.Random] = None) -> str:
        r = rng or random
        active = cls.get_active_profile()
        lower = category.lower()
        for k, arr in active.lines.items():
            if k.lower() == lower and arr:
                return r.choice(arr)
        def_arr = DEFAULT_LINES.get(lower) or DEFAULT_LINES.get(category)
        if def_arr:
            return r.choice(def_arr)
        return "喵～✦"

    @classmethod
    def pick_line_for_state(cls, state: PetState, rng: Optional[random.Random] = None) -> str:
        if state in (PetState.BUSY_LEFT, PetState.BUSY_RIGHT):
            key = "busy"
        elif state in (PetState.SWAMPED_LEFT, PetState.SWAMPED_RIGHT):
            key = "swamped"
        elif state in (
            PetState.EDGE_LEFT,
            PetState.EDGE_LEFT_PEEK,
            PetState.EDGE_RIGHT,
            PetState.EDGE_RIGHT_PEEK,
        ):
            key = "edge"
        elif state in (PetState.IDLE, PetState.IDLE2, PetState.IDLE3):
            key = "idle"
        else:
            key = state.to_sprite_key()
        return cls.pick_line(key, rng)

    @classmethod
    def match_pose_for_line(
        cls,
        category: str,
        line: str,
        *,
        docked_side: DockSide = DockSide.NONE,
        exclude_state: Optional[PetState] = None,
        rng: Optional[random.Random] = None,
    ) -> PetState:
        """根据台词分类与文本语义匹配对应的动作姿态（贴边态固定返回探头姿态）。"""
        if docked_side == DockSide.LEFT:
            return PetState.EDGE_LEFT_PEEK
        if docked_side == DockSide.RIGHT:
            return PetState.EDGE_RIGHT_PEEK

        cat = (category or "").strip().lower()
        if cat == "running":
            return PetState.RUNNING
        direct = _IDLE_DIRECT_CATEGORY_MAP.get(cat)
        if direct is not None:
            return direct
        if cat not in ("idle", "chatter", "edge"):
            parsed = PetState.from_sprite_key(cat)
            if parsed is not None and not parsed.value.startswith("edge_"):
                return parsed

        text = (line or "").strip()
        for keywords, state in _LINE_POSE_KEYWORDS:
            if any(kw in text for kw in keywords):
                return state

        r = rng or random
        fallback_pool = (
            (PetState.IDLE2, PetState.IDLE3)
            if cat == "idle"
            else (
                PetState.HAPPY,
                PetState.ASKING,
                PetState.LISTENING,
                PetState.PROUD,
                PetState.HUM,
            )
        )
        candidates = [state for state in fallback_pool if state != exclude_state]
        return r.choice(candidates or fallback_pool)

    @classmethod
    def pick_idle_speech(
        cls,
        *,
        docked_side: DockSide = DockSide.NONE,
        allow_running: bool = False,
        allow_float: bool = True,
        exclude_line: Optional[str] = None,
        exclude_state: Optional[PetState] = None,
        now: Optional[datetime] = None,
        rng: Optional[random.Random] = None,
    ) -> tuple[str, PetState]:
        """抽取无操作时的随机台词，并返回与之匹配的动作姿态。"""
        r = rng or random
        active = cls.get_active_profile()
        lower_lines: dict[str, list[str]] = {}
        for raw_key, vals in active.lines.items():
            clean = [s.strip() for s in vals if isinstance(s, str) and s.strip()]
            if clean:
                lower_lines[raw_key.lower()] = clean

        if docked_side != DockSide.NONE:
            pose = (
                PetState.EDGE_LEFT_PEEK
                if docked_side == DockSide.LEFT
                else PetState.EDGE_RIGHT_PEEK
            )
            edge_lines = lower_lines.get("edge") or list(DEFAULT_LINES["edge"])
            candidates = [ln for ln in edge_lines if ln != exclude_line] or edge_lines
            return r.choice(candidates), pose

        weighted_categories: list[tuple[str, int]] = [
            ("idle", 2),
            ("chatter", 2),
        ]
        for cat, _state in _IDLE_DIRECT_CATEGORIES:
            if cat == "float" and not allow_float:
                continue
            weighted_categories.append((cat, 1))
        if allow_running:
            weighted_categories.append(("running", 1))
        weighted_categories.append((cls.get_time_greeting_state(now).to_sprite_key(), 1))

        source_lines = lower_lines or DEFAULT_LINES
        raw_buckets: list[tuple[str, int, list[tuple[str, PetState]]]] = []
        for cat, weight in weighted_categories:
            lines = source_lines.get(cat)
            if not lines:
                continue
            pairs = [
                (
                    ln,
                    cls.match_pose_for_line(
                        cat,
                        ln,
                        docked_side=DockSide.NONE,
                        exclude_state=exclude_state,
                        rng=r,
                    ),
                )
                for ln in lines
            ]
            raw_buckets.append((cat, weight, pairs))

        if not raw_buckets:
            return "喵～✦", PetState.HAPPY

        # 优先同时避开上一句台词与上一个动作；候选不足时逐级放宽，避免单条自定义台词死循环
        def _filter_buckets(
            check_line: bool, check_pose: bool
        ) -> list[tuple[str, int, list[tuple[str, PetState]]]]:
            filtered: list[tuple[str, int, list[tuple[str, PetState]]]] = []
            for cat, weight, pairs in raw_buckets:
                sub = [
                    (ln, st)
                    for ln, st in pairs
                    if (not check_line or ln != exclude_line)
                    and (not check_pose or st != exclude_state)
                ]
                if sub:
                    filtered.append((cat, weight, sub))
            return filtered

        buckets = (
            _filter_buckets(True, True)
            or _filter_buckets(True, False)
            or _filter_buckets(False, True)
            or raw_buckets
        )
        picked_bucket = r.choices(buckets, weights=[w for _, w, _ in buckets], k=1)[0]
        return r.choice(picked_bucket[2])

    @staticmethod
    def get_time_greeting_state(now: Optional[datetime] = None) -> PetState:
        hour = (now or datetime.now()).hour
        if 0 <= hour < 5:
            return PetState.GREET_DAWN
        if 5 <= hour < 11:
            return PetState.GREET_MORNING
        if 11 <= hour < 14:
            return PetState.GREET_NOON
        if 14 <= hour < 18:
            return PetState.GREET_AFTERNOON
        if 18 <= hour < 22:
            return PetState.GREET_EVENING
        return PetState.GREET_NIGHT


def _create_gentle_preset() -> CharacterProfile:
    return CharacterProfile(
        name="星夜助手 · Gemini",
        alias="温柔治愈系桌宠",
        badge_tag="安静陪伴 × 专注模式",
        subtitle="温和细腻的桌面工作伙伴 · 陪你专注敲代码与创作",
        bio="以温柔稳定的节奏陪伴在桌面一角，会在你忙碌时轻声鼓励、提醒休息，不过度打扰。",
        ai_system_prompt=AI_TONE_OPTIONS[1].system_prompt,
        bubble_activity="quiet",
        lines={
            "greeting": ["我在桌面一角待命啦，今天也一起慢慢推进工作吧。", "欢迎回来，有什么想让我陪你一起处理的吗？"],
            "idle": ["记得偶尔眨眨眼、伸展一下肩膀。", "我就在这里静静看着你呢。", "有需要随时叫我。"],
            "pat": ["谢谢你～被这样摸头感觉很安心。", "呼…今天的节奏也很稳呢。"],
            "float": ["轻轻浮起来换个角度看桌面～", "失重状态切换完毕，继续为你护航。"],
        },
    )


def _create_tsundere_preset() -> CharacterProfile:
    return CharacterProfile(
        name="傲娇双子喵",
        alias="嘴硬心软 · 傲娇模式",
        badge_tag="炸毛预警 × 反差萌",
        subtitle="虽然嘴上总是嫌你麻烦，但视线其实一直在看着你的桌面猫娘",
        bio="容易害羞又死要面子的傲娇猫娘。被摸头会小声呼噜却嘴硬不承认，只有认真工作时才会偷偷夸你。",
        ai_system_prompt=AI_TONE_OPTIONS[2].system_prompt,
        bubble_activity="normal",
        lines={
            "greeting": ["哼，可算上线了！才、才不是特意在等你呢喵！", "喂！今天打算工作到几点呀喵？"],
            "idle": ["喂，不许无视我喵……", "盯——（假装只是在发呆）", "哼，本喵的尾巴才没有为你摇呢！"],
            "pat": ["笨、笨蛋！别突然摸头啦…咕噜……", "就准你再摸两下，多一下都不行喵！"],
            "float": ["反重力飘起来才不是为了炫耀呢喵！"],
        },
    )


def _create_genki_preset() -> CharacterProfile:
    return CharacterProfile(
        name="元气星芒娘",
        alias="活力满满 · 应援搭档",
        badge_tag="全力打气 × 灵感爆发",
        subtitle="无论什么难题都充满干劲的元气桌宠，为你注入双倍行动力！",
        bio="永远干劲满满的元气少女。遇到 Bug 或瓶颈时最擅长鼓舞士气，陪你一路通关各种任务。",
        ai_system_prompt=AI_TONE_OPTIONS[3].system_prompt,
        bubble_activity="lively",
        lines={
            "greeting": ["早安午安晚上好！今天也要元气满满地冲鸭✦", "全能量充满！准备好迎接今天的挑战了吗！"],
            "idle": ["灵感雷达全开中——哔哔！", "再难的坑也一定能踩平的，加油！", "喝一口水，继续向着目标推进✦"],
            "pat": ["哇——好感度 UP！感觉能一口气解决十个 Bug！", "贴贴充电完成～超级有精神啦！"],
            "float": ["反重力起飞！！一起突破想象边界✦"],
        },
    )


BUILTIN_PRESETS: tuple[CharacterPreset, ...] = (
    CharacterPreset("default", "🐱 原版 Gemini 娘", CharacterLore.create_default_profile),
    CharacterPreset("gentle", "✨ 温柔星夜助手", _create_gentle_preset),
    CharacterPreset("tsundere", "⚡ 傲娇小猫咪", _create_tsundere_preset),
    CharacterPreset("genki", "🌸 元气加油官", _create_genki_preset),
)
