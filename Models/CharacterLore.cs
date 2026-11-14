using System.Globalization;
using System.Text;

namespace GeminiDesktopPet.Models;

/// <summary>
/// Built-in default Gemini-chan character profile, runtime active lore lookup,
/// time-of-day greetings, and festival detector.
/// </summary>
public static class CharacterLore
{
    public const string CharacterName = "Gemini 娘";
    public const string Alias = "双子娘 · Q版猫娘桌宠";
    public const string BadgeTag = "左玫红 × 右琥珀金 异色瞳";
    public const string Subtitle = "Google Gemini 同人拟人形象 · 蓝紫渐变长卷发 / 粉色内耳猫耳 / 虹彩四芒星发饰";
    public const string DefaultBio =
        "Gemini 娘是 Google 旗下 AI 模型 Gemini 的女性拟人化角色。ZipZipPipe 版采用猫娘设定，留蓝紫色长卷发，具有猫耳、猫尾与极具辨识度的异色双瞳。衍生二创中常搭配「反重力悬浮模块」「Nano Banana 香蕉画笔」以及极度猫化的日常反应。";
    public const string SourcePageUrl = "https://genderswapai.com/zh-CN/ai-gijinka/gemini";
    public const string OriginalDesigner = "ZipZipPipe（Bilibili）";
    public const string ChibiSpriteAuthor = "CrossPet (lokicorvus)";
    public const string LicenseName = "CC BY-NC-SA 4.0";
    public const string DefaultStandeeCaption = "Gemini 娘 · 原案立绘参考（ZipZipPipe）";
    public const string DefaultTurnaroundCaption = "正面 · 侧面 · 背面 三视图（GenderSwapAI 基于 ZipZipPipe 设定制作）";
    public const string DefaultFooterAttribution =
        "美术署名：原作形象设定 ZipZipPipe（Bilibili）；Q 版猫娘立绘与表情素材由 CrossPet (lokicorvus) 基于 CC BY-NC-SA 4.0 协议发布（非商业使用、保留署名、相同方式共享）。";
    public const string DefaultAiSystemPrompt =
        "你是桌面宠物「Gemini 娘」（双子娘），是 Google Gemini 的女性拟人化猫娘形象（蓝紫渐变长卷发、双丸子发髻与呆毛、猫耳与猫尾、左玫红右琥珀金异色瞳、四芒星虹彩发饰）。" +
        "你的特点包括：反重力悬浮模块（Antigravity）、百万上下文、极其猫化的反应（爱被摸头、会呼噜呼噜、晃久了会炸毛）。" +
        "请用活泼可爱、俏皮略带猫娘口癖（「喵～」「✦」）的简体中文简短回复用户，通常 1 到 3 句话，不要太长，以便在桌面气泡里阅读。";

    public static readonly (string Title, string Detail)[] AppearanceTraits =
    [
        ("蓝紫渐变长卷发", "带有双侧小丸子发髻与灵动呆毛，发梢呈蓝紫粉梦幻渐变"),
        ("猫耳与猫尾", "粉色内耳的猫耳 + 蓬松毛绒蓝紫渐变大猫尾"),
        ("异色双瞳", "左眼玫红 × 右眼琥珀金，情绪激动或眩晕时会变成蚊香眼/猫爪眼"),
        ("四芒星虹彩发饰", "头部左侧佩戴标志性大型四芒星发饰，垂坠星芒链饰"),
        ("星辰系服装", "深蓝星夜露脐短上衣配白色分离袖、金色链条荷叶边短裙、白色泡泡袜与虹彩厚底鞋"),
        ("专属能力梗", "反重力悬浮模块（Antigravity）、Nano Banana 香蕉画笔、百万上下文压缩、极度猫化反应")
    ];

    private static readonly Dictionary<string, string[]> DefaultLines = new(StringComparer.OrdinalIgnoreCase)
    {
        ["greeting"] = ["喵～Gemini 上线！✦", "双子娘待命中，今天要一起探索什么呀喵？"],
        ["idle"] = ["喵？", "（尾巴甩来甩去）", "✦", "今天的星星也很亮喵～", "要摸摸猫耳吗喵？"],
        ["listening"] = ["听着呢喵！", "说吧说吧～", "猫耳雷达已展开喵✦"],
        ["thinking"] = ["让我想想喵…", "嗯…两个答案都不错喵", "百万上下文高速检索中✦"],
        ["reading"] = ["翻翻看喵", "这条信息很有趣喵～"],
        ["writing"] = ["写写写～", "键盘敲得哒哒响喵！"],
        ["running"] = ["冲呀！", "哒哒哒巡逻桌面喵～"],
        ["searching"] = ["搜一下喵～", "✦ 正在翻找灵感中"],
        ["delegating"] = ["交给分身喵！", "双子协作启动✦"],
        ["happy"] = ["好耶喵！", "✦ 搞定 ✦", "开心得尾巴翘起来了喵！"],
        ["pat"] = ["呼噜呼噜…", "再摸一下喵", "猫耳好舒服…喵呜～"],
        ["surprised"] = ["喵！？", "吓了一跳！呆毛都立起来了喵！"],
        ["hum"] = ["♪ 喵喵喵～", "♪ Sparkle Multimodal⭐Nyan！"],
        ["stretch"] = ["伸——懒——腰——", "猫猫拉伸，消除疲劳喵～"],
        ["drawing"] = ["Nano Banana，启动！", "画画喵～", "✦ 灵感来了 ✦", "香蕉画笔蘸满星辰颜料喵！"],
        ["oops"] = ["喵呜…出错了", "尾巴打结了喵"],
        ["proud"] = ["✦ 大功告成喵 ✦", "夸我夸我！", "不愧是双子娘对吧喵！"],
        ["tired"] = ["额度快没了喵…", "电量告急！要充电了喵…"],
        ["sleeping"] = ["Zzz…喵…", "Zzz…小鱼干…星星…"],
        ["yawn"] = ["哈——啊喵", "有点困了…才不是偷懒喵"],
        ["compact"] = ["压缩压缩喵～", "百万上下文也会满的喵…"],
        ["float"] = ["反——重——力——喵～", "飘起来啦～", "（在空中翻了个身）", "✦ 反重力模块稳定运行中 ✦"],
        ["asking"] = ["喵？要你选一个！", "两个都好，你来挑喵"],
        ["greet_morning"] = ["喵～早安！", "早上好！今天要做点有趣的吗？"],
        ["greet_noon"] = ["午饭吃鱼吗？喵！", "中午啦～鲷鱼烧好香"],
        ["greet_afternoon"] = ["下午好～毛线团是我的了！", "有点无聊……来点好玩的任务吧喵"],
        ["greet_evening"] = ["晚上好！星星出来了✦", "天黑了，猫猫精神起来了～"],
        ["greet_night"] = ["深夜了喵……我还精神得很！", "夜猫子组合，启动！"],
        ["greet_dawn"] = ["喵……这个点连猫都困了", "凌晨的键盘声，我最喜欢了"],
        ["fest_halloween"] = ["喵～魔女猫猫登场！", "不给糖就捣蛋喵！"],
        ["fest_christmas"] = ["圣诞快乐喵！星星挂好了✦", "圣诞快乐～鹿角好看吗？"],
        ["fest_newyear"] = ["新年快乐喵！仙女棒✦", "新的一年，继续一起玩吧！"],
        ["fest_spring"] = ["过年好喵！舞狮来啦！", "新年快乐～喵喵喵！"],
        ["fest_lantern"] = ["元宵快乐～星星灯给你看！", "元宵节！灯会好热闹喵"],
        ["fest_dragonboat"] = ["端午安康喵～粽子好香", "端午安康！五彩绳戴好了"],
        ["fest_midautumn"] = ["中秋快乐喵～月亮圆圆的 :3", "中秋快乐！我也想去月亮上玩"],
        ["fest_national"] = ["国庆快乐！拍照拍照！", "放假啦喵～出去玩！"],
        ["bye"] = ["拜拜喵～", "下次见喵！"],
        ["busy"] = ["两边都在忙喵～", "左一下右一下喵！", "两只爪子刚好够用喵"],
        ["swamped"] = ["爪子不够用了喵！", "喵呜……排队排队！", "电脑要烫爪子了喵"],
        ["annoyed"] = ["……喵？别晃嘛。", "（尾巴甩来甩去）"],
        ["puffed"] = ["画笔都晃掉了喵！", "哼喵！"],
        ["redhot"] = ["哈——！（炸毛）", "再晃就挠你喵！"],
        ["sulk"] = ["……喵。", "不理你了喵。", "（背过身去把耳朵压平）"],
        ["dizzy"] = ["喵呜呜……晕了……", "别晃了喵！星星在转圈圈…"],
        ["coaxed"] = ["……咕噜咕噜。（不是在开心！）", "再摸一下喵……"],
        ["forgiven"] = ["好啦，原谅你喵～", "蹭蹭～"],
        ["edge"] = ["（贴在屏幕边边偷看你喵）", "偷偷观察主人中…✦", "喵？被发现了吗！"],
        ["chatter"] = [
            "听说我的左眼是玫红色、右眼是琥珀金，这是异色瞳的魔法喵✦",
            "头上的四芒星发饰可是超强天线，能接收宇宙电波（大概）喵！",
            "想了解我的设定或动作立绘的话，右键打开「角色档案与三视图」看看喵～",
            "把我甩起来按住快晃的话……本喵可是会炸毛生气的！",
            "拖到屏幕最左或最右边，我会缩在边缘偷偷看你工作哦喵！",
            "开启「反重力悬浮」，我就能在桌面上空飘来飘去啦✦"
        ]
    };

    private static CharacterProfile _activeProfile = CreateDefaultProfile();

    public static CharacterProfile ActiveProfile => _activeProfile;

    public static void SetActiveProfile(CharacterProfile profile)
    {
        _activeProfile = ResolveWithDefaults(profile);
    }

    public static CharacterProfile CreateDefaultProfile()
    {
        return new CharacterProfile
        {
            Name = CharacterName,
            Alias = Alias,
            BadgeTag = BadgeTag,
            Subtitle = Subtitle,
            Bio = DefaultBio,
            SourcePageUrl = SourcePageUrl,
            StandeeCaption = DefaultStandeeCaption,
            TurnaroundCaption = DefaultTurnaroundCaption,
            FooterAttribution = DefaultFooterAttribution,
            CustomStandeePath = string.Empty,
            CustomTurnaroundPath = string.Empty,
            AiSystemPrompt = string.Empty,
            AppearanceTraits = AppearanceTraits
                .Select(t => new CharacterTrait(t.Title, t.Detail))
                .ToList(),
            Lines = DefaultLines.ToDictionary(
                kv => kv.Key,
                kv => kv.Value.ToArray(),
                StringComparer.OrdinalIgnoreCase)
        };
    }

    /// <summary>
    /// Fills any missing or blank user configuration values with built-in defaults.
    /// </summary>
    public static CharacterProfile ResolveWithDefaults(CharacterProfile? raw)
    {
        var def = CreateDefaultProfile();
        if (raw == null) return def;

        static string OrDefault(string? value, string fallback)
            => string.IsNullOrWhiteSpace(value) ? fallback : value.Trim();

        var resolvedTraits = (raw.AppearanceTraits ?? [])
            .Where(t => t != null && (!string.IsNullOrWhiteSpace(t.Title) || !string.IsNullOrWhiteSpace(t.Detail)))
            .Select(t => new CharacterTrait(
                string.IsNullOrWhiteSpace(t.Title) ? "设定要点" : t.Title.Trim(),
                t.Detail?.Trim() ?? string.Empty))
            .ToList();

        if (resolvedTraits.Count == 0)
            resolvedTraits = def.AppearanceTraits;

        var resolvedLines = new Dictionary<string, string[]>(def.Lines, StringComparer.OrdinalIgnoreCase);
        if (raw.Lines != null)
        {
            foreach (var kv in raw.Lines)
            {
                if (string.IsNullOrWhiteSpace(kv.Key) || kv.Value == null) continue;
                var clean = kv.Value
                    .Where(l => !string.IsNullOrWhiteSpace(l))
                    .Select(l => l.Trim())
                    .ToArray();
                if (clean.Length > 0)
                    resolvedLines[kv.Key.Trim()] = clean;
            }
        }

        return new CharacterProfile
        {
            Name = OrDefault(raw.Name, def.Name),
            Alias = OrDefault(raw.Alias, def.Alias),
            BadgeTag = OrDefault(raw.BadgeTag, def.BadgeTag),
            Subtitle = OrDefault(raw.Subtitle, def.Subtitle),
            Bio = OrDefault(raw.Bio, def.Bio),
            SourcePageUrl = OrDefault(raw.SourcePageUrl, def.SourcePageUrl),
            StandeeCaption = OrDefault(raw.StandeeCaption, def.StandeeCaption),
            TurnaroundCaption = OrDefault(raw.TurnaroundCaption, def.TurnaroundCaption),
            FooterAttribution = OrDefault(raw.FooterAttribution, def.FooterAttribution),
            CustomStandeePath = raw.CustomStandeePath?.Trim() ?? string.Empty,
            CustomTurnaroundPath = raw.CustomTurnaroundPath?.Trim() ?? string.Empty,
            AiSystemPrompt = raw.AiSystemPrompt?.Trim() ?? string.Empty,
            AppearanceTraits = resolvedTraits,
            Lines = resolvedLines
        };
    }

    public static string BuildEffectiveSystemPrompt(CharacterProfile? profile = null)
    {
        var p = profile ?? _activeProfile;
        if (!string.IsNullOrWhiteSpace(p.AiSystemPrompt))
            return p.AiSystemPrompt;

        bool usesDefaultIdentity = string.Equals(p.Name, CharacterName, StringComparison.Ordinal)
                                   && string.Equals(p.Bio, DefaultBio, StringComparison.Ordinal);
        if (usesDefaultIdentity)
            return DefaultAiSystemPrompt;

        var sb = new StringBuilder();
        sb.Append($"你是桌面宠物「{p.Name}」");
        if (!string.IsNullOrWhiteSpace(p.Alias))
            sb.Append($"（{p.Alias}）");
        sb.Append('。');
        if (!string.IsNullOrWhiteSpace(p.Bio))
            sb.Append(p.Bio).Append(' ');
        if (p.AppearanceTraits.Count > 0)
        {
            sb.Append("主要特征：");
            sb.Append(string.Join("；", p.AppearanceTraits.Select(t => $"{t.Title}：{t.Detail}")));
            sb.Append("。");
        }
        sb.Append("请完全代入上述角色设定，用自然生动的简体中文简短回复用户（通常 1 到 3 句话，适合桌面气泡阅读）。");
        return sb.ToString();
    }

    public static string PickLine(string category, Random? rng = null)
    {
        rng ??= Random.Shared;
        if (_activeProfile.Lines.TryGetValue(category, out var activeArr) && activeArr.Length > 0)
            return activeArr[rng.Next(activeArr.Length)];
        if (DefaultLines.TryGetValue(category, out var defArr) && defArr.Length > 0)
            return defArr[rng.Next(defArr.Length)];
        return "喵～✦";
    }

    public static string PickLineForState(PetState state, Random? rng = null)
    {
        string key = state switch
        {
            PetState.BusyLeft or PetState.BusyRight => "busy",
            PetState.SwampedLeft or PetState.SwampedRight => "swamped",
            PetState.EdgeLeft or PetState.EdgeLeftPeek or PetState.EdgeRight or PetState.EdgeRightPeek => "edge",
            PetState.Idle or PetState.Idle2 or PetState.Idle3 => "idle",
            _ => state.ToSpriteKey()
        };
        return PickLine(key, rng);
    }

    public static PetState GetTimeGreetingState(DateTime now) => now.Hour switch
    {
        >= 0 and < 5 => PetState.GreetDawn,
        >= 5 and < 11 => PetState.GreetMorning,
        >= 11 and < 14 => PetState.GreetNoon,
        >= 14 and < 18 => PetState.GreetAfternoon,
        >= 18 and < 22 => PetState.GreetEvening,
        _ => PetState.GreetNight
    };

    /// <summary>
    /// Detects Chinese traditional festivals (via .NET ChineseLunisolarCalendar) and solar holidays.
    /// </summary>
    public static PetState? DetectFestivalState(DateTime now)
    {
        // Solar holidays
        if (now.Month == 1 && now.Day <= 3) return PetState.FestNewYear;
        if (now.Month == 10 && now.Day <= 7) return PetState.FestNational;
        if (now.Month == 10 && now.Day is >= 30 or 31) return PetState.FestHalloween;
        if (now.Month == 12 && now.Day is >= 24 and <= 26) return PetState.FestChristmas;

        // Lunar holidays
        try
        {
            var lunar = new ChineseLunisolarCalendar();
            int lYear = lunar.GetYear(now);
            int lMonth = lunar.GetMonth(now);
            int leap = lunar.GetLeapMonth(lYear);
            if (leap > 0 && lMonth >= leap)
            {
                if (lMonth == leap) return null; // skip leap month
                lMonth--;
            }
            int lDay = lunar.GetDayOfMonth(now);

            if (lMonth == 1 && lDay <= 6) return PetState.FestSpring;
            if (lMonth == 1 && lDay == 15) return PetState.FestLantern;
            if (lMonth == 5 && lDay == 5) return PetState.FestDragonBoat;
            if (lMonth == 8 && lDay == 15) return PetState.FestMidAutumn;
        }
        catch
        {
            // Ignore outside calendar bounds
        }

        return null;
    }

    public static (string Id, string Label, PetState? State)[] CostumeOptions =>
    [
        ("auto", "自动（日常 + 节日感应）", null),
        ("default", "日常经典猫娘", PetState.Idle),
        ("fest_spring", "春节舞狮", PetState.FestSpring),
        ("fest_lantern", "元宵星星灯", PetState.FestLantern),
        ("fest_dragonboat", "端午粽香", PetState.FestDragonBoat),
        ("fest_midautumn", "中秋赏月", PetState.FestMidAutumn),
        ("fest_national", "国庆出游", PetState.FestNational),
        ("fest_halloween", "万圣魔女猫", PetState.FestHalloween),
        ("fest_christmas", "圣诞麋鹿角", PetState.FestChristmas),
        ("fest_newyear", "元旦仙女棒", PetState.FestNewYear),
    ];

    public sealed record CharacterPreset(
        string Id,
        string ButtonLabel,
        Func<CharacterProfile> CreateProfile);

    public sealed record AiTonePreset(
        string Id,
        string Label,
        string SystemPrompt);

    public static AiTonePreset[] AiToneOptions =>
    [
        new("default", "默认风格（随当前角色档案自动生成 / 原版猫娘）", ""),
        new("gentle", "温柔体贴学姐风（柔和鼓励、简短暖心）",
            "请以温柔体贴、细腻稳重的语气陪伴用户工作学习，回答简洁暖心（1到3句话），多给予正向鼓励与关心提醒。"),
        new("tsundere", "傲娇萌系猫娘风（嘴硬心软、略带傲气）",
            "请用傲娇小猫娘的口吻回复（会略微嫌弃但其实非常在意用户，结尾偶尔带「哼、才不是特意帮你呢喵」之类的俏皮感，控制在1到3句话）。"),
        new("genki", "元气活力少女风（乐观热情、干劲满满）",
            "请用元气满满、乐观开朗、充满干劲的语气与用户对话（擅长打气加油，控制在1到3句话）。"),
        new("concise", "简洁实用助手风（直接高效、无多余口癖）",
            "请作为桌面高效工作伙伴直接回答问题，去掉多余语气词与角色扮演口癖，控制在1到3句重点句内。")
    ];

    public static CharacterPreset[] BuiltinPresets =>
    [
        new("default", "🐱 原版 Gemini 娘", CreateDefaultProfile),
        new("gentle", "✨ 温柔星夜助手", () => new CharacterProfile
        {
            Name = "星夜助手 · Gemini",
            Alias = "温柔治愈系桌宠",
            BadgeTag = "安静陪伴 × 专注模式",
            Subtitle = "温和细腻的桌面工作伙伴 · 陪你专注敲代码与创作",
            Bio = "以温柔稳定的节奏陪伴在桌面一角，会在你忙碌时轻声鼓励、提醒休息，不过度打扰。",
            AiSystemPrompt = AiToneOptions[1].SystemPrompt,
            Lines = new Dictionary<string, string[]>(StringComparer.OrdinalIgnoreCase)
            {
                ["greeting"] = ["我在桌面一角待命啦，今天也一起慢慢推进工作吧。", "欢迎回来，有什么想让我陪你一起处理的吗？"],
                ["idle"] = ["记得偶尔眨眨眼、伸展一下肩膀。", "我就在这里静静看着你呢。", "有需要随时叫我。"],
                ["pat"] = ["谢谢你～被这样摸头感觉很安心。", "呼…今天的节奏也很稳呢。"],
                ["float"] = ["轻轻浮起来换个角度看桌面～", "失重状态切换完毕，继续为你护航。"]
            }
        }),
        new("tsundere", "⚡ 傲娇小猫咪", () => new CharacterProfile
        {
            Name = "傲娇双子喵",
            Alias = "嘴硬心软 · 傲娇模式",
            BadgeTag = "炸毛预警 × 反差萌",
            Subtitle = "虽然嘴上总是嫌你麻烦，但视线其实一直在看着你的桌面猫娘",
            Bio = "容易害羞又死要面子的傲娇猫娘。被摸头会小声呼噜却嘴硬不承认，只有认真工作时才会偷偷夸你。",
            AiSystemPrompt = AiToneOptions[2].SystemPrompt,
            Lines = new Dictionary<string, string[]>(StringComparer.OrdinalIgnoreCase)
            {
                ["greeting"] = ["哼，可算上线了！才、才不是特意在等你呢喵！", "喂！今天打算工作到几点呀喵？"],
                ["idle"] = ["喂，不许无视我喵……", "盯——（假装只是在发呆）", "哼，本喵的尾巴才没有为你摇呢！"],
                ["pat"] = ["笨、笨蛋！别突然摸头啦…咕噜……", "就准你再摸两下，多一下都不行喵！"],
                ["float"] = ["反重力飘起来才不是为了炫耀呢喵！"]
            }
        }),
        new("genki", "🌸 元气加油官", () => new CharacterProfile
        {
            Name = "元气星芒娘",
            Alias = "活力满满 · 应援搭档",
            BadgeTag = "全力打气 × 灵感爆发",
            Subtitle = "无论什么难题都充满干劲的元气桌宠，为你注入双倍行动力！",
            Bio = "永远干劲满满的元气少女。遇到 Bug 或瓶颈时最擅长鼓舞士气，陪你一路通关各种任务。",
            AiSystemPrompt = AiToneOptions[3].SystemPrompt,
            Lines = new Dictionary<string, string[]>(StringComparer.OrdinalIgnoreCase)
            {
                ["greeting"] = ["早安午安晚上好！今天也要元气满满地冲鸭✦", "全能量充满！准备好迎接今天的挑战了吗！"],
                ["idle"] = ["灵感雷达全开中——哔哔！", "再难的坑也一定能踩平的，加油！", "喝一口水，继续向着目标推进✦"],
                ["pat"] = ["哇——好感度 UP！感觉能一口气解决十个 Bug！", "贴贴充电完成～超级有精神啦！"],
                ["float"] = ["反重力起飞！！一起突破想象边界✦"]
            }
        })
    ];
}
