using System.Globalization;
using System.IO;
using System.Security.Cryptography;
using System.Text;
using GeminiDesktopPet.Models;
using Microsoft.Win32;

namespace GeminiDesktopPet.Services;

/// <summary>
/// Persists all pet settings to config.ini (auto-generated on first launch, read directly afterwards)
/// and manages optional HKCU autostart entries.
/// </summary>
public sealed class SettingsService
{
    public const string RunRegistryPath = @"Software\Microsoft\Windows\CurrentVersion\Run";
    public const string AppRunValueName = "GeminiDesktopPet";
    public const string LegacyAntigravityFollowRunValueName = "GeminiDesktopPet_AntigravityFollower";
    public const string LegacyAntigravityFollowAltValueName = "GeminiDesktopPet_AntigravityFollow";
    public const string FollowWatcherRunValueName = "GeminiDesktopPet_FollowWatcher";

    private readonly string _settingsFilePath;
    private readonly string _apiKeySecretPath;

    public string SettingsFolder { get; }
    public string SettingsFilePath => _settingsFilePath;

    public SettingsService(string? customSettingsFolder = null)
    {
        if (!string.IsNullOrWhiteSpace(customSettingsFolder))
        {
            SettingsFolder = customSettingsFolder;
            Directory.CreateDirectory(SettingsFolder);
            _settingsFilePath = Path.Combine(SettingsFolder, "config.ini");
        }
        else
        {
            string appData = Environment.GetFolderPath(Environment.SpecialFolder.ApplicationData);
            SettingsFolder = Path.Combine(appData, "GeminiDesktopPet");
            Directory.CreateDirectory(SettingsFolder);

            string appConfigDir = ResolveDefaultConfigDirectory();
            _settingsFilePath = Path.Combine(appConfigDir, "config.ini");
        }
        _apiKeySecretPath = Path.Combine(SettingsFolder, "apikey.dat");
    }

    public static string ResolveDefaultConfigDirectory()
        => AppContext.BaseDirectory.TrimEnd(Path.DirectorySeparatorChar, Path.AltDirectorySeparatorChar);

    public PetSettings Load()
    {
        try
        {
            if (File.Exists(_settingsFilePath))
            {
                string iniContent = File.ReadAllText(_settingsFilePath, Encoding.UTF8);
                var loaded = ParseIni(iniContent);
                loaded.Scale = Math.Clamp(loaded.Scale, 0.5, 2.0);
                loaded.Opacity = Math.Clamp(loaded.Opacity, 0.35, 1.0);
                loaded.GeminiApiKey = LoadEncryptedApiKey();
                return loaded;
            }
        }
        catch (Exception ex)
        {
            AppLogger.Warn("Settings", $"读取 config.ini 失败，回退默认值：{ex.Message}");
        }

        // 首次启动时若 config.ini 不存在，自动生成默认配置文件
        var defaults = new PetSettings
        {
            GeminiApiKey = LoadEncryptedApiKey()
        };
        Save(defaults);
        return defaults;
    }

    public void Save(PetSettings settings)
    {
        try
        {
            string? parentDir = Path.GetDirectoryName(_settingsFilePath);
            if (!string.IsNullOrWhiteSpace(parentDir))
                Directory.CreateDirectory(parentDir);

            string iniContent = FormatIni(settings);
            File.WriteAllText(_settingsFilePath, iniContent, new UTF8Encoding(encoderShouldEmitUTF8Identifier: false));
            SaveEncryptedApiKey(settings.GeminiApiKey);

            string legacyJson = Path.Combine(SettingsFolder, "settings.json");
            if (File.Exists(legacyJson))
                File.Delete(legacyJson);
        }
        catch (Exception ex)
        {
            AppLogger.Error("Settings", "保存 config.ini 配置文件失败", ex);
        }
    }

    private string LoadEncryptedApiKey()
    {
        try
        {
            if (!File.Exists(_apiKeySecretPath))
                return string.Empty;

            byte[] encrypted = File.ReadAllBytes(_apiKeySecretPath);
            byte[] plain = ProtectedData.Unprotect(encrypted, null, DataProtectionScope.CurrentUser);
            return Encoding.UTF8.GetString(plain);
        }
        catch
        {
            return string.Empty;
        }
    }

    private void SaveEncryptedApiKey(string apiKey)
    {
        try
        {
            Directory.CreateDirectory(SettingsFolder);
            if (string.IsNullOrWhiteSpace(apiKey))
            {
                if (File.Exists(_apiKeySecretPath))
                    File.Delete(_apiKeySecretPath);
                return;
            }

            byte[] plain = Encoding.UTF8.GetBytes(apiKey.Trim());
            byte[] encrypted = ProtectedData.Protect(plain, null, DataProtectionScope.CurrentUser);
            File.WriteAllBytes(_apiKeySecretPath, encrypted);
        }
        catch
        {
        }
    }

    private static string FormatIni(PetSettings s)
    {
        var sb = new StringBuilder();
        sb.AppendLine("; Gemini 娘桌面萌宠配置文件 (首次启动自动生成，后续启动直接读取)");
        sb.AppendLine();
        sb.AppendLine("[Follow]");
        sb.AppendLine($"FollowAntigravity={FormatBool(s.FollowAntigravity)}");
        sb.AppendLine($"FollowGeminiApp={FormatBool(s.FollowGeminiApp)}");
        sb.AppendLine($"SleepWhenAntigravityExits={FormatBool(s.SleepWhenAntigravityExits)}");
        sb.AppendLine();
        sb.AppendLine("[Appearance]");
        sb.AppendLine($"StyleMode={s.StyleMode}");
        sb.AppendLine($"Scale={s.Scale.ToString("0.##", CultureInfo.InvariantCulture)}");
        sb.AppendLine($"Opacity={s.Opacity.ToString("0.##", CultureInfo.InvariantCulture)}");
        sb.AppendLine($"CostumeOverride={s.CostumeOverride}");
        sb.AppendLine($"AutoFestivalCostume={FormatBool(s.AutoFestivalCostume)}");
        sb.AppendLine();
        sb.AppendLine("[Behavior]");
        sb.AppendLine($"AlwaysOnTop={FormatBool(s.AlwaysOnTop)}");
        sb.AppendLine($"AutoWalk={FormatBool(s.AutoWalk)}");
        sb.AppendLine($"AntiGravityMode={FormatBool(s.AntiGravityMode)}");
        sb.AppendLine($"GravityEnabled={FormatBool(s.GravityEnabled)}");
        sb.AppendLine($"EdgeDockEnabled={FormatBool(s.EdgeDockEnabled)}");
        sb.AppendLine($"SoundEnabled={FormatBool(s.SoundEnabled)}");
        sb.AppendLine($"ParticlesEnabled={FormatBool(s.ParticlesEnabled)}");
        sb.AppendLine($"ClickThrough={FormatBool(s.ClickThrough)}");
        sb.AppendLine($"SystemMonitorEnabled={FormatBool(s.SystemMonitorEnabled)}");
        sb.AppendLine($"ShowSpeechBubble={FormatBool(s.ShowSpeechBubble)}");
        sb.AppendLine();
        sb.AppendLine("[Position]");
        sb.AppendLine($"LastLeft={(s.LastLeft.HasValue && !double.IsNaN(s.LastLeft.Value) && !double.IsInfinity(s.LastLeft.Value) ? s.LastLeft.Value.ToString("0.##", CultureInfo.InvariantCulture) : string.Empty)}");
        sb.AppendLine($"LastTop={(s.LastTop.HasValue && !double.IsNaN(s.LastTop.Value) && !double.IsInfinity(s.LastTop.Value) ? s.LastTop.Value.ToString("0.##", CultureInfo.InvariantCulture) : string.Empty)}");
        sb.AppendLine($"DockedSide={s.DockedSide}");
        sb.AppendLine();
        sb.AppendLine("[Bond]");
        sb.AppendLine($"TotalPats={s.TotalPats}");
        sb.AppendLine($"Favorability={s.Favorability}");
        sb.AppendLine();
        sb.AppendLine("[AI]");
        sb.AppendLine($"GeminiModel={s.GeminiModel}");
        sb.AppendLine();
        sb.AppendLine("[Update]");
        sb.AppendLine($"UpdateReleasesUrl={s.UpdateReleasesUrl}");
        sb.AppendLine($"AutoCheckUpdates={FormatBool(s.AutoCheckUpdates)}");
        sb.AppendLine($"SkippedUpdateVersion={s.SkippedUpdateVersion}");
        sb.AppendLine();
        sb.AppendLine("[Diagnostics]");
        sb.AppendLine($"EnableRuntimeLog={FormatBool(s.EnableRuntimeLog)}");
        return sb.ToString();
    }

    private static PetSettings ParseIni(string iniContent)
    {
        var s = new PetSettings();
        foreach (string rawLine in iniContent.Split(['\r', '\n'], StringSplitOptions.RemoveEmptyEntries))
        {
            string line = rawLine.Trim();
            if (line.Length == 0 || line.StartsWith(';') || line.StartsWith('#') || (line.StartsWith('[') && line.EndsWith(']')))
                continue;

            int eqIdx = line.IndexOf('=');
            if (eqIdx <= 0)
                continue;

            string key = line[..eqIdx].Trim();
            string val = line[(eqIdx + 1)..].Trim();

            switch (key.ToLowerInvariant())
            {
                case "stylemode":
                    if (Enum.TryParse<PetStyleMode>(val, ignoreCase: true, out var styleMode))
                        s.StyleMode = styleMode;
                    break;
                case "scale":
                    if (double.TryParse(val, NumberStyles.Float, CultureInfo.InvariantCulture, out double scale))
                        s.Scale = scale;
                    break;
                case "opacity":
                    if (double.TryParse(val, NumberStyles.Float, CultureInfo.InvariantCulture, out double opacity))
                        s.Opacity = opacity;
                    break;
                case "alwaysontop":
                    if (TryParseBool(val, out bool alwaysOnTop)) s.AlwaysOnTop = alwaysOnTop;
                    break;
                case "autowalk":
                    if (TryParseBool(val, out bool autoWalk)) s.AutoWalk = autoWalk;
                    break;
                case "antigravitymode":
                    if (TryParseBool(val, out bool antiGravity)) s.AntiGravityMode = antiGravity;
                    break;
                case "gravityenabled":
                    if (TryParseBool(val, out bool gravity)) s.GravityEnabled = gravity;
                    break;
                case "edgedockenabled":
                    if (TryParseBool(val, out bool edgeDock)) s.EdgeDockEnabled = edgeDock;
                    break;
                case "soundenabled":
                    if (TryParseBool(val, out bool sound)) s.SoundEnabled = sound;
                    break;
                case "particlesenabled":
                    if (TryParseBool(val, out bool particles)) s.ParticlesEnabled = particles;
                    break;
                case "clickthrough":
                    if (TryParseBool(val, out bool clickThrough)) s.ClickThrough = clickThrough;
                    break;
                case "systemmonitorenabled":
                    if (TryParseBool(val, out bool sysMon)) s.SystemMonitorEnabled = sysMon;
                    break;
                case "showspeechbubble":
                    if (TryParseBool(val, out bool showBubble)) s.ShowSpeechBubble = showBubble;
                    break;
                case "autofestivalcostume":
                    if (TryParseBool(val, out bool autoFest)) s.AutoFestivalCostume = autoFest;
                    break;
                case "followantigravity":
                    if (TryParseBool(val, out bool followAg)) s.FollowAntigravity = followAg;
                    break;
                case "followgeminiapp":
                    if (TryParseBool(val, out bool followGem)) s.FollowGeminiApp = followGem;
                    break;
                case "sleepwhenantigravityexits":
                    if (TryParseBool(val, out bool sleepExit)) s.SleepWhenAntigravityExits = sleepExit;
                    break;
                case "costumeoverride":
                    if (!string.IsNullOrWhiteSpace(val)) s.CostumeOverride = val;
                    break;
                case "lastleft":
                    s.LastLeft = double.TryParse(val, NumberStyles.Float, CultureInfo.InvariantCulture, out double left) && !double.IsNaN(left) && !double.IsInfinity(left)
                        ? left
                        : null;
                    break;
                case "lasttop":
                    s.LastTop = double.TryParse(val, NumberStyles.Float, CultureInfo.InvariantCulture, out double top) && !double.IsNaN(top) && !double.IsInfinity(top)
                        ? top
                        : null;
                    break;
                case "dockedside":
                    if (Enum.TryParse<DockSide>(val, ignoreCase: true, out var dockSide))
                        s.DockedSide = dockSide;
                    break;
                case "totalpats":
                    if (int.TryParse(val, NumberStyles.Integer, CultureInfo.InvariantCulture, out int totalPats))
                        s.TotalPats = totalPats;
                    break;
                case "favorability":
                    if (int.TryParse(val, NumberStyles.Integer, CultureInfo.InvariantCulture, out int fav))
                        s.Favorability = fav;
                    break;
                case "geminimodel":
                    if (!string.IsNullOrWhiteSpace(val)) s.GeminiModel = val;
                    break;
                case "updatereleasesurl":
                    if (!string.IsNullOrWhiteSpace(val)) s.UpdateReleasesUrl = val;
                    break;
                case "autocheckupdates":
                    if (TryParseBool(val, out bool autoUpdate)) s.AutoCheckUpdates = autoUpdate;
                    break;
                case "skippedupdateversion":
                    s.SkippedUpdateVersion = val;
                    break;
                case "enableruntimelog":
                    if (TryParseBool(val, out bool runtimeLog)) s.EnableRuntimeLog = runtimeLog;
                    break;
            }
        }
        return s;
    }

    private static string FormatBool(bool value) => value ? "true" : "false";

    private static bool TryParseBool(string value, out bool result)
    {
        if (bool.TryParse(value, out result))
            return true;
        if (value == "1" || string.Equals(value, "yes", StringComparison.OrdinalIgnoreCase) || string.Equals(value, "on", StringComparison.OrdinalIgnoreCase))
        {
            result = true;
            return true;
        }
        if (value == "0" || string.Equals(value, "no", StringComparison.OrdinalIgnoreCase) || string.Equals(value, "off", StringComparison.OrdinalIgnoreCase))
        {
            result = false;
            return true;
        }
        result = false;
        return false;
    }

    public bool IsAutoStartEnabled()
        => HasRunValue(AppRunValueName);

    public void SetAutoStart(bool enabled)
    {
        string? exePath = Environment.ProcessPath;
        SetRunValue(AppRunValueName, enabled && !string.IsNullOrWhiteSpace(exePath) ? $"\"{exePath}\"" : null);
    }

    public bool IsFollowWatcherRunEnabled()
        => HasRunValue(FollowWatcherRunValueName);

    /// <summary>
    /// Registers or removes the standard HKCU Run standby entry (--follow-watcher)
    /// without dropping external scripts into the Startup folder.
    /// </summary>
    public void SetFollowWatcherRun(bool enabled)
    {
        SetRunValue(LegacyAntigravityFollowRunValueName, null);
        SetRunValue(LegacyAntigravityFollowAltValueName, null);
        string? exePath = Environment.ProcessPath;
        string? command = enabled && !string.IsNullOrWhiteSpace(exePath)
            ? $"\"{exePath}\" --follow-watcher"
            : null;
        SetRunValue(FollowWatcherRunValueName, command);
    }

    private static bool HasRunValue(string valueName)
    {
        try
        {
            using var key = Registry.CurrentUser.OpenSubKey(RunRegistryPath, false);
            return key?.GetValue(valueName) is string val && !string.IsNullOrWhiteSpace(val);
        }
        catch
        {
            return false;
        }
    }

    private static void SetRunValue(string valueName, string? command)
    {
        try
        {
            using var key = Registry.CurrentUser.OpenSubKey(RunRegistryPath, true);
            if (key == null) return;

            if (!string.IsNullOrWhiteSpace(command))
                key.SetValue(valueName, command);
            else
                key.DeleteValue(valueName, false);
        }
        catch
        {
            // Ignore registry errors.
        }
    }
}
