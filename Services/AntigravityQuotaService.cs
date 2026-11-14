using System.Diagnostics;
using System.IO;
using System.Text;
using System.Text.Json;

namespace GeminiDesktopPet.Services;

public sealed record QuotaBucketInfo(
    string Id,
    string Name,
    string Window,
    double RemainingFraction,
    DateTimeOffset? ResetTime,
    string? Description);

public sealed record QuotaGroupInfo(
    string Name,
    string? Description,
    IReadOnlyList<QuotaBucketInfo> Buckets);

public sealed record QuotaSnapshot(
    bool Success,
    string? ErrorMessage,
    string? AgyPath,
    string? ActiveCliModel,
    int? RemainingCredits,
    string? UpgradeUri,
    string? OverviewDescription,
    DateTimeOffset FetchedAt,
    IReadOnlyList<QuotaGroupInfo> Groups)
{
    public string FormatBubbleSummary()
    {
        if (!Success || Groups.Count == 0)
            return ErrorMessage ?? "暂时无法读取 Antigravity 额度喵～";

        var parts = new List<string>();
        foreach (var g in Groups)
        {
            var b5h = g.Buckets.FirstOrDefault(b => b.Window.Equals("5h", StringComparison.OrdinalIgnoreCase));
            var bWk = g.Buckets.FirstOrDefault(b => b.Window.Equals("weekly", StringComparison.OrdinalIgnoreCase));
            string shortName = g.Name.Replace(" models", "", StringComparison.OrdinalIgnoreCase)
                                     .Replace(" Models", "", StringComparison.OrdinalIgnoreCase);

            if (b5h != null && bWk != null)
                parts.Add($"{shortName}: 5h {b5h.RemainingFraction * 100:0.#}% / 周 {bWk.RemainingFraction * 100:0.#}%");
            else if (bWk != null)
                parts.Add($"{shortName}: {bWk.RemainingFraction * 100:0.#}%");
            else if (g.Buckets.Count > 0)
                parts.Add($"{shortName}: {g.Buckets[0].RemainingFraction * 100:0.#}%");
        }

        string summary = parts.Count > 0 ? string.Join(" · ", parts) : "已读取额度";
        if (RemainingCredits.HasValue)
            summary += $" · AI Credits: {RemainingCredits.Value}";
        return $"反重力额度✦ {summary}";
    }

    public string FormatClipboardText()
    {
        var sb = new StringBuilder();
        sb.AppendLine($"Antigravity 反重力额度快照 ({FetchedAt.ToLocalTime():yyyy-MM-dd HH:mm:ss})");
        if (!string.IsNullOrWhiteSpace(ActiveCliModel))
            sb.AppendLine($"当前模型: {ActiveCliModel}");
        if (RemainingCredits.HasValue)
            sb.AppendLine($"AI Credits: {RemainingCredits.Value}");
        sb.AppendLine();

        foreach (var g in Groups)
        {
            sb.AppendLine($"[{g.Name}]");
            if (!string.IsNullOrWhiteSpace(g.Description))
                sb.AppendLine($"  {g.Description}");
            foreach (var b in g.Buckets)
            {
                string resetStr = b.ResetTime.HasValue
                    ? b.ResetTime.Value.ToLocalTime().ToString("yyyy-MM-dd HH:mm")
                    : "-";
                sb.AppendLine($"  - {b.Name} ({b.Window}): {b.RemainingFraction * 100:0.0}% 剩余 | 重置: {resetStr}");
            }
        }
        return sb.ToString().TrimEnd();
    }
}

/// <summary>
/// Queries the logged-in Antigravity account's model quotas and AI credits
/// through the official Antigravity CLI (`agy --print /quota --output-format json`).
/// </summary>
public sealed class AntigravityQuotaService
{
    public string? ResolveAgyExecutable()
    {
        string localAppData = Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData);
        string userProfile = Environment.GetFolderPath(Environment.SpecialFolder.UserProfile);

        string[] explicitCandidates =
        [
            Path.Combine(localAppData, "agy", "bin", "agy.exe"),
            Path.Combine(localAppData, "agy", "bin", "agy.cmd"),
            Path.Combine(userProfile, ".gemini", "antigravity", "bin", "agy.exe")
        ];

        foreach (string candidate in explicitCandidates)
        {
            if (File.Exists(candidate))
                return candidate;
        }

        string? pathEnv = Environment.GetEnvironmentVariable("PATH");
        if (!string.IsNullOrWhiteSpace(pathEnv))
        {
            foreach (string dir in pathEnv.Split(Path.PathSeparator, StringSplitOptions.RemoveEmptyEntries))
            {
                try
                {
                    string exe = Path.Combine(dir.Trim(), "agy.exe");
                    if (File.Exists(exe)) return exe;
                    string cmd = Path.Combine(dir.Trim(), "agy.cmd");
                    if (File.Exists(cmd)) return cmd;
                }
                catch
                {
                }
            }
        }

        return null;
    }

    public string? ReadActiveCliModel()
    {
        try
        {
            string settingsPath = Path.Combine(
                Environment.GetFolderPath(Environment.SpecialFolder.UserProfile),
                ".gemini", "antigravity-cli", "settings.json");
            if (!File.Exists(settingsPath)) return null;

            using var doc = JsonDocument.Parse(File.ReadAllText(settingsPath, Encoding.UTF8));
            if (doc.RootElement.TryGetProperty("model", out var modelEl) && modelEl.ValueKind == JsonValueKind.String)
                return modelEl.GetString();
        }
        catch
        {
        }
        return null;
    }

    public async Task<QuotaSnapshot> FetchQuotaAsync(CancellationToken ct = default)
    {
        string? agyPath = ResolveAgyExecutable();
        string? activeModel = ReadActiveCliModel();
        var now = DateTimeOffset.Now;

        if (string.IsNullOrWhiteSpace(agyPath))
        {
            AppLogger.Warn("Quota", "未在本机找到 agy.exe / agy.cmd");
            return new QuotaSnapshot(
                false,
                "未在本机找到官方 Antigravity CLI (agy.exe)。请先安装并登录 Antigravity CLI 或桌面端。",
                null,
                activeModel,
                null,
                null,
                null,
                now,
                Array.Empty<QuotaGroupInfo>());
        }

        var (qCode, qOut, qErr) = await RunAgyCommandAsync(agyPath, "--print /quota --output-format json --print-timeout 15s", ct).ConfigureAwait(false);
        bool quotaOk = TryParseQuotaJson(qOut, out string? overview, out var groups, out string? jsonErr);
        if (!quotaOk && !ct.IsCancellationRequested)
        {
            // One automatic retry avoids transient CLI/network EOF spikes
            await Task.Delay(350, ct).ConfigureAwait(false);
            (qCode, qOut, qErr) = await RunAgyCommandAsync(agyPath, "--print /quota --output-format json --print-timeout 15s", ct).ConfigureAwait(false);
            quotaOk = TryParseQuotaJson(qOut, out overview, out groups, out jsonErr);
        }

        int? remainingCredits = null;
        string? upgradeUri = null;
        if (!ct.IsCancellationRequested)
        {
            var (_, cOut, _) = await RunAgyCommandAsync(agyPath, "--print /credits --output-format json --print-timeout 10s", ct).ConfigureAwait(false);
            TryParseCreditsJson(cOut, out remainingCredits, out upgradeUri);
        }

        if (quotaOk)
        {
            AppLogger.Info("Quota", $"额度查询成功：分组={groups.Count}, 模型={activeModel ?? "unknown"}, Credits={remainingCredits?.ToString() ?? "n/a"}");
            return new QuotaSnapshot(
                true,
                null,
                agyPath,
                activeModel,
                remainingCredits,
                upgradeUri,
                overview,
                DateTimeOffset.Now,
                groups);
        }

        string detail = !string.IsNullOrWhiteSpace(jsonErr)
            ? jsonErr
            : !string.IsNullOrWhiteSpace(qErr)
                ? qErr.Trim()
                : $"agy 查询返回失败 (退出码 {qCode})。请确认已在 Antigravity / agy 中完成 Google 账号登录。";

        AppLogger.Warn("Quota", $"额度查询失败 (agyPath={agyPath}, exitCode={qCode}): {detail}");
        return new QuotaSnapshot(
            false,
            detail,
            agyPath,
            activeModel,
            remainingCredits,
            upgradeUri,
            null,
            DateTimeOffset.Now,
            Array.Empty<QuotaGroupInfo>());
    }

    private static async Task<(int ExitCode, string StdOut, string StdErr)> RunAgyCommandAsync(
        string agyPath,
        string arguments,
        CancellationToken ct)
    {
        try
        {
            bool isCmd = agyPath.EndsWith(".cmd", StringComparison.OrdinalIgnoreCase) ||
                         agyPath.EndsWith(".bat", StringComparison.OrdinalIgnoreCase);

            var psi = new ProcessStartInfo
            {
                FileName = isCmd ? "cmd.exe" : agyPath,
                Arguments = isCmd ? $"/d /c \"\"{agyPath}\" {arguments}\"" : arguments,
                WorkingDirectory = Environment.GetFolderPath(Environment.SpecialFolder.UserProfile),
                UseShellExecute = false,
                CreateNoWindow = true,
                RedirectStandardInput = true,
                RedirectStandardOutput = true,
                RedirectStandardError = true,
                StandardOutputEncoding = Encoding.UTF8,
                StandardErrorEncoding = Encoding.UTF8
            };

            using var proc = new Process { StartInfo = psi };
            if (!proc.Start())
                return (-1, string.Empty, "无法启动 agy 进程。");

            try { proc.StandardInput.Close(); } catch { }

            var outTask = proc.StandardOutput.ReadToEndAsync(ct);
            var errTask = proc.StandardError.ReadToEndAsync(ct);
            await proc.WaitForExitAsync(ct).ConfigureAwait(false);

            string stdout = await outTask.ConfigureAwait(false);
            string stderr = await errTask.ConfigureAwait(false);
            return (proc.ExitCode, stdout, stderr);
        }
        catch (OperationCanceledException)
        {
            throw;
        }
        catch (Exception ex)
        {
            return (-1, string.Empty, ex.Message);
        }
    }

    private static string? ExtractJsonObject(string raw)
    {
        if (string.IsNullOrWhiteSpace(raw)) return null;
        int start = raw.IndexOf('{');
        int end = raw.LastIndexOf('}');
        if (start < 0 || end <= start) return null;
        return raw.Substring(start, end - start + 1);
    }

    private static bool TryParseQuotaJson(
        string raw,
        out string? overviewDescription,
        out List<QuotaGroupInfo> groups,
        out string? errorMessage)
    {
        overviewDescription = null;
        groups = new List<QuotaGroupInfo>();
        errorMessage = null;

        string? json = ExtractJsonObject(raw);
        if (json == null) return false;

        try
        {
            using var doc = JsonDocument.Parse(json);
            var root = doc.RootElement;

            if (root.TryGetProperty("status", out var statusEl) &&
                statusEl.ValueKind == JsonValueKind.String &&
                !string.Equals(statusEl.GetString(), "SUCCESS", StringComparison.OrdinalIgnoreCase))
            {
                if (root.TryGetProperty("response", out var errResp) && errResp.ValueKind == JsonValueKind.String)
                    errorMessage = errResp.GetString();
                return false;
            }

            if (!root.TryGetProperty("command", out var cmdEl) ||
                !cmdEl.TryGetProperty("data", out var dataEl))
            {
                return false;
            }

            if (dataEl.TryGetProperty("description", out var descEl) && descEl.ValueKind == JsonValueKind.String)
                overviewDescription = descEl.GetString();

            if (!dataEl.TryGetProperty("groups", out var groupsEl) || groupsEl.ValueKind != JsonValueKind.Array)
                return false;

            foreach (var gEl in groupsEl.EnumerateArray())
            {
                string gName = gEl.TryGetProperty("name", out var gn) ? gn.GetString() ?? "Models" : "Models";
                string? gDesc = gEl.TryGetProperty("description", out var gd) && gd.ValueKind == JsonValueKind.String ? gd.GetString() : null;
                var buckets = new List<QuotaBucketInfo>();

                if (gEl.TryGetProperty("buckets", out var bArray) && bArray.ValueKind == JsonValueKind.Array)
                {
                    foreach (var bEl in bArray.EnumerateArray())
                    {
                        string id = bEl.TryGetProperty("id", out var idEl) ? idEl.GetString() ?? "" : "";
                        string name = bEl.TryGetProperty("name", out var nEl) ? nEl.GetString() ?? id : id;
                        string window = bEl.TryGetProperty("window", out var wEl) ? wEl.GetString() ?? "" : "";
                        string? desc = bEl.TryGetProperty("description", out var bdEl) && bdEl.ValueKind == JsonValueKind.String ? bdEl.GetString() : null;
                        double frac = bEl.TryGetProperty("remaining_fraction", out var fEl) && fEl.ValueKind == JsonValueKind.Number
                            ? Math.Clamp(fEl.GetDouble(), 0.0, 1.0)
                            : 0.0;

                        DateTimeOffset? resetTime = null;
                        if (bEl.TryGetProperty("reset_time", out var rtEl) &&
                            rtEl.ValueKind == JsonValueKind.String &&
                            DateTimeOffset.TryParse(rtEl.GetString(), out var parsedTime))
                        {
                            resetTime = parsedTime;
                        }

                        buckets.Add(new QuotaBucketInfo(id, name, window, frac, resetTime, desc));
                    }
                }

                groups.Add(new QuotaGroupInfo(gName, gDesc, buckets));
            }

            return groups.Count > 0;
        }
        catch (Exception ex)
        {
            errorMessage = $"解析额度返回数据失败：{ex.Message}";
            return false;
        }
    }

    private static void TryParseCreditsJson(string raw, out int? credits, out string? upgradeUri)
    {
        credits = null;
        upgradeUri = null;
        string? json = ExtractJsonObject(raw);
        if (json == null) return;

        try
        {
            using var doc = JsonDocument.Parse(json);
            if (doc.RootElement.TryGetProperty("command", out var cmdEl) &&
                cmdEl.TryGetProperty("data", out var dataEl))
            {
                if (dataEl.TryGetProperty("remaining_credits", out var crEl) && crEl.TryGetInt32(out int val))
                    credits = val;
                if (dataEl.TryGetProperty("upgrade_uri", out var uriEl) && uriEl.ValueKind == JsonValueKind.String)
                    upgradeUri = uriEl.GetString();
            }
        }
        catch
        {
        }
    }
}
