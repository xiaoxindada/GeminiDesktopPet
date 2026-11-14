using System.IO;
using System.Text.Json;
using GeminiDesktopPet.Models;
using Microsoft.Win32;

namespace GeminiDesktopPet.Services;

/// <summary>
/// Persists pet settings to %AppData%\GeminiDesktopPet\settings.json and manages optional HKCU autostart entries.
/// </summary>
public sealed class SettingsService
{
    private const string RunRegistryPath = @"Software\Microsoft\Windows\CurrentVersion\Run";
    private const string AppRunValueName = "GeminiDesktopPet";
    private const string LegacyAntigravityFollowRunValueName = "GeminiDesktopPet_AntigravityFollower";
    private const string FollowWatcherRunValueName = "GeminiDesktopPet_FollowWatcher";

    private static readonly JsonSerializerOptions JsonOptions = new()
    {
        WriteIndented = true
    };

    private readonly string _settingsFilePath;

    public string SettingsFolder { get; }

    public SettingsService(string? customSettingsFolder = null)
    {
        if (!string.IsNullOrWhiteSpace(customSettingsFolder))
        {
            SettingsFolder = customSettingsFolder;
        }
        else
        {
            string appData = Environment.GetFolderPath(Environment.SpecialFolder.ApplicationData);
            SettingsFolder = Path.Combine(appData, "GeminiDesktopPet");
        }
        Directory.CreateDirectory(SettingsFolder);
        _settingsFilePath = Path.Combine(SettingsFolder, "settings.json");
    }

    public PetSettings Load()
    {
        try
        {
            if (File.Exists(_settingsFilePath))
            {
                string json = File.ReadAllText(_settingsFilePath);
                var loaded = JsonSerializer.Deserialize<PetSettings>(json, JsonOptions);
                if (loaded != null)
                {
                    loaded.Scale = Math.Clamp(loaded.Scale, 0.5, 2.0);
                    loaded.Opacity = Math.Clamp(loaded.Opacity, 0.35, 1.0);
                    return loaded;
                }
            }
        }
        catch (Exception ex)
        {
            AppLogger.Warn("Settings", $"读取设置失败，回退默认值：{ex.Message}");
        }
        return new PetSettings();
    }

    public void Save(PetSettings settings)
    {
        try
        {
            string json = JsonSerializer.Serialize(settings, JsonOptions);
            File.WriteAllText(_settingsFilePath, json);
        }
        catch (Exception ex)
        {
            AppLogger.Error("Settings", "保存设置文件失败", ex);
        }
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
    /// Registers or removes the standard HKCU Run standby entry (--follow-antigravity)
    /// without dropping external scripts into the Startup folder.
    /// </summary>
    public void SetFollowWatcherRun(bool enabled)
    {
        SetRunValue(LegacyAntigravityFollowRunValueName, null);
        string? exePath = Environment.ProcessPath;
        string? command = enabled && !string.IsNullOrWhiteSpace(exePath)
            ? $"\"{exePath}\" --follow-antigravity"
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
