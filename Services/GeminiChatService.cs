using System.Net.Http;
using System.Text;
using System.Text.Json;
using GeminiDesktopPet.Models;

namespace GeminiDesktopPet.Services;

public sealed record ChatReply(string Text, PetState? SuggestedState = null);

/// <summary>
/// Provides offline Gemini-chan persona chat & command handling, plus optional live Gemini API chat.
/// </summary>
public sealed class GeminiChatService
{
    private static readonly HttpClient Http = new()
    {
        Timeout = TimeSpan.FromSeconds(25)
    };

    public async Task<ChatReply> AskAsync(string userInput, PetSettings settings, SystemHealthSnapshot? health)
    {
        string text = userInput.Trim();
        if (string.IsNullOrEmpty(text))
            return new ChatReply("喵？想和我聊什么呀～✦", PetState.Listening);

        // Local quick intents work offline instantaneously
        if (TryHandleLocalIntent(text, settings, health, out var localReply))
            return localReply;

        // If user provided a Gemini API key, call live Gemini API
        if (!string.IsNullOrWhiteSpace(settings.GeminiApiKey))
        {
            try
            {
                string aiText = await CallGeminiApiAsync(text, settings);
                return new ChatReply(aiText, PetState.Happy);
            }
            catch (Exception ex)
            {
                return new ChatReply($"喵呜…连线失败了（{ex.Message}），先用本地模式陪你喵！", PetState.Oops);
            }
        }

        // Rich offline fallback responses
        return new ChatReply(GenerateOfflineChatter(text), PetState.Thinking);
    }

    private static bool TryHandleLocalIntent(string input, PetSettings settings, SystemHealthSnapshot? health, out ChatReply reply)
    {
        string lower = input.ToLowerInvariant();

        if (ContainsAny(lower, "摸摸", "摸头", "乖", "pat", "rua"))
        {
            reply = new ChatReply(CharacterLore.PickLine("pat"), PetState.Pat);
            return true;
        }
        if (ContainsAny(lower, "反重力", "飘", "飞", "float", "antigravity"))
        {
            reply = new ChatReply("反——重——力——模块启动喵！✦", PetState.Float);
            return true;
        }
        if (ContainsAny(lower, "睡", "晚安", "困", "sleep"))
        {
            reply = new ChatReply("那我先打个盹喵…Zzz…", PetState.Sleeping);
            return true;
        }
        if (ContainsAny(lower, "早安", "早上好", "上午好"))
        {
            reply = new ChatReply(CharacterLore.PickLine("greet_morning"), PetState.GreetMorning);
            return true;
        }
        if (ContainsAny(lower, "午安", "中午", "午饭"))
        {
            reply = new ChatReply(CharacterLore.PickLine("greet_noon"), PetState.GreetNoon);
            return true;
        }
        if (ContainsAny(lower, "晚上好"))
        {
            reply = new ChatReply(CharacterLore.PickLine("greet_evening"), PetState.GreetEvening);
            return true;
        }
        if (ContainsAny(lower, "几点", "时间", "日期"))
        {
            var now = DateTime.Now;
            reply = new ChatReply($"现在是 {now:HH:mm}（{now:yyyy-MM-dd}）喵！记得照顾好眼睛和爪子哦✦", PetState.Asking);
            return true;
        }
        if (ContainsAny(lower, "cpu", "电量", "电脑", "状态", "内存"))
        {
            if (health != null)
            {
                string bat = health.HasBattery
                    ? $"，电池 {health.BatteryPercent}%{(health.IsCharging ? "（充电中）" : "")}"
                    : "";
                reply = new ChatReply(
                    $"当前 CPU 占用约 {health.CpuPercent:F0}%{bat}，好感度 {settings.Favorability}✦",
                    health.CpuPercent > 75 ? PetState.SwampedLeft : PetState.Proud);
                return true;
            }
        }
        if (ContainsAny(lower, "你是谁", "设定", "形象", "异色瞳", "猫耳", "介绍"))
        {
            var profile = CharacterLore.ActiveProfile;
            string intro = string.Equals(profile.Name, CharacterLore.CharacterName, StringComparison.Ordinal)
                && string.Equals(profile.Bio, CharacterLore.DefaultBio, StringComparison.Ordinal)
                ? "我是 Gemini 娘（双子娘）喵！留着蓝紫渐变长卷发和猫耳，左眼玫红、右眼琥珀金，最喜欢戴四芒星发饰在桌面上陪你工作聊天啦✦"
                : $"我是「{profile.Name}」（{profile.Alias}）！{profile.Bio}";
            reply = new ChatReply(intro, PetState.Proud);
            return true;
        }

        reply = default!;
        return false;
    }

    private static string GenerateOfflineChatter(string input)
    {
        if (input.EndsWith('?') || input.EndsWith('？') || input.Contains("吗") || input.Contains("呢"))
        {
            return Random.Shared.Next(3) switch
            {
                0 => "让我想想喵…嗯，我觉得很有道理✦（想让我真正联网回答，可以在「设置」里填上 Gemini API Key 哦）",
                1 => "两个选项都挺不错的喵～不如听听你的直觉？✦",
                _ => "猫耳天线正在思考中…离线模式下我也可以陪你聊天、摸头、巡逻和撒娇喵！"
            };
        }

        return Random.Shared.Next(4) switch
        {
            0 => "收到喵～已经放进我的百万上下文小本本里了✦",
            1 => "喵呜！听起来很有趣呢～今天也一起加油吧！",
            2 => "（尾巴轻轻晃了晃）我在听着呢喵～随时可以摸摸我哦✦",
            _ => "好耶喵！需要我切到「反重力悬浮」或打开「角色档案」陪你吗？"
        };
    }

    private static async Task<string> CallGeminiApiAsync(string prompt, PetSettings settings)
    {
        string model = string.IsNullOrWhiteSpace(settings.GeminiModel) ? "gemini-2.5-flash" : settings.GeminiModel.Trim();
        string url = $"https://generativelanguage.googleapis.com/v1beta/models/{Uri.EscapeDataString(model)}:generateContent?key={Uri.EscapeDataString(settings.GeminiApiKey.Trim())}";

        var payload = new
        {
            systemInstruction = new
            {
                parts = new[] { new { text = CharacterLore.BuildEffectiveSystemPrompt() } }
            },
            contents = new[]
            {
                new
                {
                    role = "user",
                    parts = new[] { new { text = prompt } }
                }
            },
            generationConfig = new
            {
                maxOutputTokens = 240,
                temperature = 0.85
            }
        };

        string json = JsonSerializer.Serialize(payload);
        using var req = new HttpRequestMessage(HttpMethod.Post, url)
        {
            Content = new StringContent(json, Encoding.UTF8, "application/json")
        };

        using var resp = await Http.SendAsync(req);
        string body = await resp.Content.ReadAsStringAsync();
        if (!resp.IsSuccessStatusCode)
            throw new InvalidOperationException($"HTTP {(int)resp.StatusCode}");

        using var doc = JsonDocument.Parse(body);
        if (doc.RootElement.TryGetProperty("candidates", out var candidates) && candidates.GetArrayLength() > 0)
        {
            var first = candidates[0];
            if (first.TryGetProperty("content", out var content) &&
                content.TryGetProperty("parts", out var parts) &&
                parts.GetArrayLength() > 0 &&
                parts[0].TryGetProperty("text", out var textProp))
            {
                return textProp.GetString()?.Trim() ?? "喵～✦";
            }
        }

        return "喵～✦";
    }

    private static bool ContainsAny(string haystack, params string[] needles)
        => needles.Any(n => haystack.Contains(n, StringComparison.Ordinal));
}
