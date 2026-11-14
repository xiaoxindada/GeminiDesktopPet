using System.Globalization;

namespace GeminiDesktopPet.Models;

/// <summary>
/// Character profile, lore lines, time-of-day greetings, and festival detector
/// faithful to Gender Swap AI's Gemini-chan profile (ZipZipPipe + CrossPet lore).
/// </summary>
public static class CharacterLore
{
    public const string CharacterName = "Gemini 娘";
    public const string Alias = "双子娘 · Gemini";
    public const string SourcePageUrl = "https://genderswapai.com/zh-CN/ai-gijinka/gemini";
    public const string OriginalDesigner = "ZipZipPipe（Bilibili）";
    public const string ChibiSpriteAuthor = "CrossPet (lokicorvus)";
    public const string LicenseName = "CC BY-NC-SA 4.0";

    public static readonly (string Title, string Detail)[] AppearanceTraits =
    [
        ("蓝紫渐变长卷发", "带有双侧小丸子发髻与灵动呆毛，发梢呈蓝紫粉梦幻渐变"),
        ("猫耳与猫尾", "粉色内耳的猫耳 + 蓬松毛绒蓝紫渐变大猫尾"),
        ("异色双瞳", "左眼玫红 × 右眼琥珀金，情绪激动或眩晕时会变成蚊香眼/猫爪眼"),
        ("四芒星虹彩发饰", "头部左侧佩戴标志性大型四芒星发饰，垂坠星芒链饰"),
        ("星辰系服装", "深蓝星夜露脐短上衣配白色分离袖、金色链条荷叶边短裙、白色泡泡袜与虹彩厚底鞋"),
        ("专属能力梗", "反重力悬浮模块（Antigravity）、Nano Banana 香蕉画笔、百万上下文压缩、极度猫化反应")
    ];

    private static readonly Dictionary<string, string[]> Lines = new()
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

    public static string PickLine(string category, Random? rng = null)
    {
        rng ??= Random.Shared;
        if (Lines.TryGetValue(category, out var arr) && arr.Length > 0)
            return arr[rng.Next(arr.Length)];
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
}
