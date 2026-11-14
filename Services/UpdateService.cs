using System.Diagnostics;
using System.IO;
using System.IO.Compression;
using System.Net;
using System.Net.Http;
using System.Text;
using System.Text.Json;
using System.Text.RegularExpressions;
using GeminiDesktopPet.Models;

namespace GeminiDesktopPet.Services;

public sealed record ReleaseAssetInfo(string Name, string DownloadUrl, long SizeBytes);

public sealed record UpdateCheckResult(
    bool Succeeded,
    bool UpdateAvailable,
    string CurrentVersion,
    string LatestTag,
    string LatestVersion,
    string ReleaseTitle,
    string ChangelogMarkdown,
    string ReleaseHtmlUrl,
    DateTimeOffset? PublishedAt,
    ReleaseAssetInfo? PackageAsset,
    string StatusMessage,
    bool NoPublishedReleases = false);

/// <summary>
/// Checks upstream GitHub Releases (always pointing to latest release), downloads release ZIPs,
/// and applies portable self-updates safely without locking the application directory.
/// </summary>
public sealed class UpdateService
{
    public const string DefaultReleasesUrl = "https://github.com/xiaoxindada/GeminiDesktopPet/releases";

    private static readonly HttpClient Http = CreateHttpClient();

    private static HttpClient CreateHttpClient()
    {
        var handler = new HttpClientHandler
        {
            AllowAutoRedirect = true,
            AutomaticDecompression = DecompressionMethods.GZip | DecompressionMethods.Deflate
        };
        var client = new HttpClient(handler)
        {
            Timeout = TimeSpan.FromSeconds(25)
        };
        client.DefaultRequestHeaders.UserAgent.ParseAdd($"GeminiDesktopPet/{AppVersion.Current}");
        return client;
    }

    public static string NormalizeReleasesUrl(string? raw)
    {
        if (TryParseGitHubRepo(raw, out string owner, out string repo))
            return $"https://github.com/{owner}/{repo}/releases";
        return DefaultReleasesUrl;
    }

    public static string GetLatestTrackingUrl(string? raw)
        => NormalizeReleasesUrl(raw).TrimEnd('/') + "/latest";

    private static bool TryParseGitHubRepo(string? raw, out string owner, out string repo)
    {
        owner = "xiaoxindada";
        repo = "GeminiDesktopPet";
        if (string.IsNullOrWhiteSpace(raw))
            return true;

        if (!Uri.TryCreate(raw.Trim(), UriKind.Absolute, out var uri))
            return false;

        if (!uri.Host.Equals("github.com", StringComparison.OrdinalIgnoreCase))
            return false;

        string[] segments = uri.AbsolutePath.Split('/', StringSplitOptions.RemoveEmptyEntries);
        if (segments.Length < 2)
            return false;

        owner = segments[0];
        repo = segments[1].EndsWith(".git", StringComparison.OrdinalIgnoreCase)
            ? segments[1][..^4]
            : segments[1];
        return !string.IsNullOrWhiteSpace(owner) && !string.IsNullOrWhiteSpace(repo);
    }

    public async Task<UpdateCheckResult> CheckForUpdateAsync(string? customReleasesUrl = null, CancellationToken ct = default)
    {
        string current = AppVersion.Current;
        if (!TryParseGitHubRepo(customReleasesUrl, out string owner, out string repo))
        {
            return new UpdateCheckResult(
                false, false, current, "", "", "", "", DefaultReleasesUrl, null, null,
                "上游追踪地址格式无效，请使用 GitHub Releases 地址。");
        }

        string canonicalReleasesUrl = $"https://github.com/{owner}/{repo}/releases";
        string latestPageUrl = $"{canonicalReleasesUrl}/latest";
        string apiLatestUrl = $"https://api.github.com/repos/{owner}/{repo}/releases/latest";

        try
        {
            using var req = new HttpRequestMessage(HttpMethod.Get, apiLatestUrl);
            req.Headers.Accept.ParseAdd("application/vnd.github+json");

            using var resp = await Http.SendAsync(req, ct);
            if (resp.StatusCode == HttpStatusCode.NotFound)
            {
                return await CheckReleasesListFallbackAsync(owner, repo, canonicalReleasesUrl, current, ct);
            }

            if (resp.IsSuccessStatusCode)
            {
                string json = await resp.Content.ReadAsStringAsync(ct);
                using var doc = JsonDocument.Parse(json);
                return BuildResultFromReleaseElement(doc.RootElement, canonicalReleasesUrl, current);
            }

            if (resp.StatusCode == HttpStatusCode.Forbidden || (int)resp.StatusCode == 429)
            {
                return await CheckHtmlRedirectFallbackAsync(owner, repo, latestPageUrl, current, ct);
            }

            return await CheckHtmlRedirectFallbackAsync(owner, repo, latestPageUrl, current, ct);
        }
        catch (OperationCanceledException) when (ct.IsCancellationRequested)
        {
            throw;
        }
        catch
        {
            // Retry via direct GitHub /releases/latest web redirect before failing
            try
            {
                return await CheckHtmlRedirectFallbackAsync(owner, repo, latestPageUrl, current, ct);
            }
            catch (Exception ex)
            {
                AppLogger.Warn("Update", $"检查更新失败 ({latestPageUrl}): {ex.Message}");
                return new UpdateCheckResult(
                    false, false, current, "", "", "", "", latestPageUrl, null, null,
                    $"检查更新失败：{ex.Message}");
            }
        }
    }

    private async Task<UpdateCheckResult> CheckReleasesListFallbackAsync(
        string owner,
        string repo,
        string canonicalReleasesUrl,
        string current,
        CancellationToken ct)
    {
        string apiListUrl = $"https://api.github.com/repos/{owner}/{repo}/releases?per_page=1";
        using var req = new HttpRequestMessage(HttpMethod.Get, apiListUrl);
        req.Headers.Accept.ParseAdd("application/vnd.github+json");

        using var resp = await Http.SendAsync(req, ct);
        if (resp.StatusCode == HttpStatusCode.NotFound)
        {
            return new UpdateCheckResult(
                true, false, current, "", "", "", "", canonicalReleasesUrl, null, null,
                $"上游 Releases 暂无已发布版本（当前版本 v{current}）。",
                NoPublishedReleases: true);
        }

        if (!resp.IsSuccessStatusCode)
            return await CheckHtmlRedirectFallbackAsync(owner, repo, canonicalReleasesUrl + "/latest", current, ct);

        string json = await resp.Content.ReadAsStringAsync(ct);
        using var doc = JsonDocument.Parse(json);
        if (doc.RootElement.ValueKind != JsonValueKind.Array || doc.RootElement.GetArrayLength() == 0)
        {
            return new UpdateCheckResult(
                true, false, current, "", "", "", "", canonicalReleasesUrl, null, null,
                $"上游 Releases 暂无已发布版本（当前版本 v{current}）。",
                NoPublishedReleases: true);
        }

        return BuildResultFromReleaseElement(doc.RootElement[0], canonicalReleasesUrl, current);
    }

    private async Task<UpdateCheckResult> CheckHtmlRedirectFallbackAsync(
        string owner,
        string repo,
        string latestPageUrl,
        string current,
        CancellationToken ct)
    {
        using var req = new HttpRequestMessage(HttpMethod.Get, latestPageUrl);
        using var resp = await Http.SendAsync(req, HttpCompletionOption.ResponseHeadersRead, ct);
        string finalUrl = resp.RequestMessage?.RequestUri?.ToString() ?? latestPageUrl;

        var tagMatch = Regex.Match(finalUrl, @"/releases/tag/([^/?#]+)", RegexOptions.IgnoreCase);
        if (!tagMatch.Success)
        {
            if (resp.IsSuccessStatusCode)
            {
                return new UpdateCheckResult(
                    true, false, current, "", "", "", "", latestPageUrl, null, null,
                    $"上游 Releases 暂无已发布版本（当前版本 v{current}）。",
                    NoPublishedReleases: true);
            }

            return new UpdateCheckResult(
                false, false, current, "", "", "", "", latestPageUrl, null, null,
                $"无法访问 GitHub Releases（HTTP {(int)resp.StatusCode}）。");
        }

        string tag = Uri.UnescapeDataString(tagMatch.Groups[1].Value);
        string latestVer = AppVersion.Normalize(tag);
        bool hasNewer = AppVersion.IsNewerVersion(latestVer, current);

        ReleaseAssetInfo? asset = null;
        try
        {
            string expandedUrl = $"https://github.com/{owner}/{repo}/releases/expanded_assets/{Uri.EscapeDataString(tag)}";
            string html = await Http.GetStringAsync(expandedUrl, ct);
            asset = ParseFirstZipAssetFromHtml(html, owner, repo, tag);
        }
        catch
        {
            // Construct standard package fallback URL
        }

        asset ??= new ReleaseAssetInfo(
            $"GeminiDesktopPet-v{latestVer}-win-x64.zip",
            $"https://github.com/{owner}/{repo}/releases/download/{Uri.EscapeDataString(tag)}/GeminiDesktopPet-v{latestVer}-win-x64.zip",
            0);

        string status = hasNewer
            ? $"发现新版本 {AppVersion.FormatTag(latestVer)}（当前 v{current}）"
            : $"当前已是最新版本 v{current}";

        return new UpdateCheckResult(
            true,
            hasNewer,
            current,
            tag,
            latestVer,
            $"GeminiDesktopPet {tag}",
            "（当前通过网页重定向检查到最新版本，详细 Changelog 可点击「浏览器打开 Releases」查看。）",
            finalUrl,
            null,
            asset,
            status);
    }

    private static ReleaseAssetInfo? ParseFirstZipAssetFromHtml(string html, string owner, string repo, string tag)
    {
        var matches = Regex.Matches(
            html,
            @"href=""(/(?:[^""]+)/releases/download/[^""]+\.zip)""",
            RegexOptions.IgnoreCase);
        var assets = new List<ReleaseAssetInfo>();
        foreach (Match m in matches)
        {
            string rel = m.Groups[1].Value;
            string name = Path.GetFileName(rel);
            assets.Add(new ReleaseAssetInfo(name, "https://github.com" + rel, 0));
        }
        return PickPreferredAsset(assets);
    }

    private static UpdateCheckResult BuildResultFromReleaseElement(JsonElement root, string canonicalReleasesUrl, string current)
    {
        string tag = root.TryGetProperty("tag_name", out var tagProp) ? (tagProp.GetString() ?? string.Empty).Trim() : string.Empty;
        string title = root.TryGetProperty("name", out var nameProp) ? (nameProp.GetString() ?? tag).Trim() : tag;
        string body = root.TryGetProperty("body", out var bodyProp) ? (bodyProp.GetString() ?? string.Empty).Trim() : string.Empty;
        string htmlUrl = root.TryGetProperty("html_url", out var urlProp)
            ? (urlProp.GetString() ?? $"{canonicalReleasesUrl}/latest").Trim()
            : $"{canonicalReleasesUrl}/latest";

        DateTimeOffset? publishedAt = null;
        if (root.TryGetProperty("published_at", out var pubProp)
            && DateTimeOffset.TryParse(pubProp.GetString(), out var parsedDate))
        {
            publishedAt = parsedDate.ToLocalTime();
        }

        var assets = new List<ReleaseAssetInfo>();
        if (root.TryGetProperty("assets", out var assetsProp) && assetsProp.ValueKind == JsonValueKind.Array)
        {
            foreach (var item in assetsProp.EnumerateArray())
            {
                string name = item.TryGetProperty("name", out var nProp) ? nProp.GetString() ?? "" : "";
                string dlUrl = item.TryGetProperty("browser_download_url", out var dProp) ? dProp.GetString() ?? "" : "";
                long size = item.TryGetProperty("size", out var sProp) && sProp.TryGetInt64(out long s) ? s : 0;
                if (!string.IsNullOrWhiteSpace(name) && !string.IsNullOrWhiteSpace(dlUrl))
                    assets.Add(new ReleaseAssetInfo(name, dlUrl, size));
            }
        }

        var preferredAsset = PickPreferredAsset(assets);
        string latestVer = AppVersion.Normalize(tag);
        bool hasNewer = !string.IsNullOrWhiteSpace(tag) && AppVersion.IsNewerVersion(latestVer, current);

        string status = string.IsNullOrWhiteSpace(tag)
            ? "未读取到有效的上游版本标签。"
            : hasNewer
                ? $"发现新版本 {AppVersion.FormatTag(latestVer)}（当前 v{current}）"
                : $"当前已是最新版本 v{current}（上游最新：{tag}）";

        string changelog = string.IsNullOrWhiteSpace(body)
            ? "（该版本未填写文字版更新说明，可前往 GitHub Release 页面查看详情。）"
            : body;

        return new UpdateCheckResult(
            true,
            hasNewer,
            current,
            tag,
            latestVer,
            string.IsNullOrWhiteSpace(title) ? tag : title,
            changelog,
            htmlUrl,
            publishedAt,
            preferredAsset,
            status);
    }

    private static ReleaseAssetInfo? PickPreferredAsset(IReadOnlyList<ReleaseAssetInfo> assets)
    {
        var zipAssets = assets.Where(a => a.Name.EndsWith(".zip", StringComparison.OrdinalIgnoreCase)).ToArray();
        if (zipAssets.Length == 0)
            return null;

        bool isSelfContained = File.Exists(Path.Combine(AppContext.BaseDirectory, "coreclr.dll"));
        if (isSelfContained)
        {
            var full = zipAssets.FirstOrDefault(a =>
                a.Name.Contains("win-x64.zip", StringComparison.OrdinalIgnoreCase) &&
                !a.Name.Contains("-fd.zip", StringComparison.OrdinalIgnoreCase));
            if (full != null) return full;
        }
        else
        {
            var fd = zipAssets.FirstOrDefault(a =>
                a.Name.Contains("-fd.zip", StringComparison.OrdinalIgnoreCase));
            if (fd != null) return fd;
        }

        return zipAssets.FirstOrDefault(a => a.Name.Contains("win", StringComparison.OrdinalIgnoreCase)) ?? zipAssets[0];
    }

    public static bool CanApplyInPlaceUpdate(out string reason)
    {
        string baseDir = AppContext.BaseDirectory;
        var dir = new DirectoryInfo(baseDir);
        for (int depth = 0; depth < 5 && dir != null; depth++, dir = dir.Parent)
        {
            if (File.Exists(Path.Combine(dir.FullName, "GeminiDesktopPet.csproj")))
            {
                reason = "当前正从源码编译输出目录运行，请使用发布包目录进行原地自动更新，或点击浏览器打开 Releases 手动下载。";
                return false;
            }
        }

        try
        {
            string probe = Path.Combine(baseDir, $".update_write_test_{Environment.ProcessId}.tmp");
            File.WriteAllText(probe, "ok");
            File.Delete(probe);
            reason = string.Empty;
            return true;
        }
        catch (Exception ex)
        {
            reason = $"应用目录没有写入权限，无法自动覆盖更新：{ex.Message}";
            return false;
        }
    }

    public async Task<string> DownloadAssetAsync(
        ReleaseAssetInfo asset,
        string versionTag,
        IProgress<(long DownloadedBytes, long? TotalBytes)>? progress,
        CancellationToken ct)
    {
        string safeTag = SanitizePathSegment(string.IsNullOrWhiteSpace(versionTag) ? "latest" : versionTag);
        string workDir = Path.Combine(
            Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
            "GeminiDesktopPet",
            "updates",
            safeTag);
        Directory.CreateDirectory(workDir);

        string zipPath = Path.Combine(workDir, SanitizePathSegment(asset.Name));
        if (File.Exists(zipPath))
            File.Delete(zipPath);

        using var dlClient = new HttpClient { Timeout = TimeSpan.FromMinutes(10) };
        dlClient.DefaultRequestHeaders.UserAgent.ParseAdd($"GeminiDesktopPet/{AppVersion.Current}");

        using var resp = await dlClient.GetAsync(asset.DownloadUrl, HttpCompletionOption.ResponseHeadersRead, ct);
        resp.EnsureSuccessStatusCode();

        long? totalBytes = resp.Content.Headers.ContentLength ?? (asset.SizeBytes > 0 ? asset.SizeBytes : null);
        await using var netStream = await resp.Content.ReadAsStreamAsync(ct);
        await using var fs = new FileStream(zipPath, FileMode.Create, FileAccess.Write, FileShare.None, 81920, true);

        byte[] buffer = new byte[81920];
        long downloaded = 0;
        int read;
        while ((read = await netStream.ReadAsync(buffer, ct)) > 0)
        {
            await fs.WriteAsync(buffer.AsMemory(0, read), ct);
            downloaded += read;
            progress?.Report((downloaded, totalBytes));
        }

        return zipPath;
    }

    public static void ExtractAndLaunchUpdater(
        string zipPath,
        string versionTag,
        Action triggerGracefulExit,
        Action? beforeExitCleanup = null)
    {
        string updateWorkRoot = Path.GetDirectoryName(zipPath)
            ?? Path.Combine(Path.GetTempPath(), "GeminiDesktopPet_Update");
        string stagingDir = Path.Combine(updateWorkRoot, "staging");
        if (Directory.Exists(stagingDir))
            Directory.Delete(stagingDir, true);
        Directory.CreateDirectory(stagingDir);

        ZipFile.ExtractToDirectory(zipPath, stagingDir, overwriteFiles: true);

        string payloadDir = stagingDir;
        if (!File.Exists(Path.Combine(payloadDir, "GeminiDesktopPet.exe")))
        {
            var nested = Directory.GetDirectories(stagingDir);
            if (nested.Length == 1 && File.Exists(Path.Combine(nested[0], "GeminiDesktopPet.exe")))
                payloadDir = nested[0];
        }

        if (!File.Exists(Path.Combine(payloadDir, "GeminiDesktopPet.exe")))
            throw new InvalidOperationException("更新包中未找到 GeminiDesktopPet.exe，已取消安装。");

        string targetAppDir = AppContext.BaseDirectory.TrimEnd('\\', '/');
        string cleanPayloadDir = payloadDir.TrimEnd('\\', '/');
        string stagingExePath = Path.Combine(cleanPayloadDir, "GeminiDesktopPet.exe");

        if (string.Equals(cleanPayloadDir, targetAppDir, StringComparison.OrdinalIgnoreCase))
            throw new InvalidOperationException("更新暂存目录与当前应用目录相同，已取消更新。");

        beforeExitCleanup?.Invoke();

        // Launch the extracted GUI executable directly to perform managed file replacement after exit (no batch/cmd scripts).
        var psi = new ProcessStartInfo
        {
            FileName = stagingExePath,
            WorkingDirectory = cleanPayloadDir,
            UseShellExecute = false
        };
        psi.ArgumentList.Add("--apply-update");
        psi.ArgumentList.Add(Environment.ProcessId.ToString());
        psi.ArgumentList.Add(targetAppDir);
        Process.Start(psi);

        triggerGracefulExit();
    }

    public static bool ApplyStagedUpdateAndRelaunch(int parentPid, string targetAppDir)
    {
        try
        {
            if (string.IsNullOrWhiteSpace(targetAppDir) || !Directory.Exists(targetAppDir))
                return false;

            string sourceDir = AppContext.BaseDirectory.TrimEnd('\\', '/');
            string cleanTargetDir = targetAppDir.TrimEnd('\\', '/');
            if (string.Equals(sourceDir, cleanTargetDir, StringComparison.OrdinalIgnoreCase))
                return false;

            AppLogger.Info("Update", $"开始原地更新替换：parentPid={parentPid}, targetDir={cleanTargetDir}");
            WaitForProcessExit(parentPid, TimeSpan.FromSeconds(15));
            Thread.Sleep(350);

            CopyDirectoryWithRetry(sourceDir, cleanTargetDir);
            AppLogger.Info("Update", $"原地文件覆盖完成，准备重新启动：{cleanTargetDir}");

            string targetExe = Path.Combine(cleanTargetDir, "GeminiDesktopPet.exe");
            if (File.Exists(targetExe))
            {
                Process.Start(new ProcessStartInfo(targetExe)
                {
                    WorkingDirectory = cleanTargetDir,
                    UseShellExecute = true
                });
            }
            return true;
        }
        catch (Exception ex)
        {
            AppLogger.Error("Update", "应用原地更新失败", ex);
            return false;
        }
    }

    public static void CleanupOldStagingFolders()
    {
        try
        {
            string updatesRoot = Path.Combine(
                Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
                "GeminiDesktopPet",
                "updates");
            if (!Directory.Exists(updatesRoot))
                return;

            string currentBase = Path.GetFullPath(AppContext.BaseDirectory)
                .TrimEnd(Path.DirectorySeparatorChar, Path.AltDirectorySeparatorChar) + Path.DirectorySeparatorChar;
            string updatesPrefix = Path.GetFullPath(updatesRoot)
                .TrimEnd(Path.DirectorySeparatorChar, Path.AltDirectorySeparatorChar) + Path.DirectorySeparatorChar;

            // Never delete staging files while currently executing from inside the updates folder.
            if (currentBase.StartsWith(updatesPrefix, StringComparison.OrdinalIgnoreCase))
                return;

            foreach (string dir in Directory.GetDirectories(updatesRoot))
            {
                try { Directory.Delete(dir, recursive: true); }
                catch { }
            }
        }
        catch
        {
            // Ignore cleanup errors on startup.
        }
    }

    private static void WaitForProcessExit(int pid, TimeSpan timeout)
    {
        if (pid <= 0) return;
        try
        {
            using var proc = Process.GetProcessById(pid);
            if (!proc.HasExited && !proc.WaitForExit((int)timeout.TotalMilliseconds))
            {
                proc.Kill();
                proc.WaitForExit(3000);
            }
        }
        catch
        {
            // Process already exited.
        }
    }

    private static void CopyDirectoryWithRetry(string sourceDir, string targetDir)
    {
        Directory.CreateDirectory(targetDir);

        foreach (string dirPath in Directory.GetDirectories(sourceDir, "*", SearchOption.AllDirectories))
        {
            string rel = Path.GetRelativePath(sourceDir, dirPath);
            Directory.CreateDirectory(Path.Combine(targetDir, rel));
        }

        foreach (string srcFile in Directory.GetFiles(sourceDir, "*", SearchOption.AllDirectories))
        {
            string rel = Path.GetRelativePath(sourceDir, srcFile);
            string dstFile = Path.Combine(targetDir, rel);
            string? dstParent = Path.GetDirectoryName(dstFile);
            if (!string.IsNullOrWhiteSpace(dstParent))
                Directory.CreateDirectory(dstParent);

            CopyFileWithRetry(srcFile, dstFile);
        }
    }

    private static void CopyFileWithRetry(string sourceFile, string destFile)
    {
        const int MaxAttempts = 10;
        for (int attempt = 1; attempt <= MaxAttempts; attempt++)
        {
            try
            {
                File.Copy(sourceFile, destFile, overwrite: true);
                return;
            }
            catch (Exception ex) when (ex is IOException || ex is UnauthorizedAccessException)
            {
                if (attempt == MaxAttempts)
                    throw;
                Thread.Sleep(250);
            }
        }
    }

    private static string SanitizePathSegment(string value)
    {
        foreach (char c in Path.GetInvalidFileNameChars())
            value = value.Replace(c, '_');
        return value.Trim();
    }
}
