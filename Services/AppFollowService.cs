using System.IO;
using GeminiDesktopPet.Models;

namespace GeminiDesktopPet.Services;

/// <summary>
/// Manages native process-based app-follow standby mode and HKCU Run registration,
/// removing any legacy Startup VBScript or auxiliary files from earlier versions.
/// </summary>
public sealed class AppFollowService
{
    private const string LegacyStartupScriptName = "GeminiDesktopPet_FollowWatcher.vbs";

    private static readonly string[] LegacyAuxiliaryFiles =
    [
        "pet_path.txt",
        "start_bat_path.txt",
        "stop_bat_path.txt",
        "watcher_token.txt",
        "watcher.lock",
        "wake_reason.txt",
        "FollowAppsWatcher.vbs",
        "StopFollowWatcher.vbs"
    ];

    private readonly SettingsService _settingsService;

    public string StartupFolderPath { get; }

    public AppFollowService(SettingsService settingsService)
    {
        _settingsService = settingsService;
        Directory.CreateDirectory(settingsService.SettingsFolder);

        StartupFolderPath = Environment.GetFolderPath(Environment.SpecialFolder.Startup);
        if (string.IsNullOrWhiteSpace(StartupFolderPath))
        {
            StartupFolderPath = Path.Combine(
                Environment.GetFolderPath(Environment.SpecialFolder.ApplicationData),
                "Microsoft", "Windows", "Start Menu", "Programs", "Startup");
        }
    }

    public static bool AnyFollowEnabled(PetSettings settings)
        => settings.FollowAntigravity || settings.FollowGeminiApp;

    public bool IsFollowLogonEnabled()
        => _settingsService.IsFollowWatcherRunEnabled() || _settingsService.IsAutoStartEnabled();

    /// <summary>
    /// Syncs standard HKCU Run standby registration with settings and removes any legacy VBS/txt files.
    /// </summary>
    public void SyncAndApply(PetSettings settings)
    {
        CleanupLegacyFiles();
        bool enabled = AnyFollowEnabled(settings);
        _settingsService.SetFollowWatcherRun(enabled);
        AppLogger.Info("AppFollow", $"同步应用联动状态：Antigravity={settings.FollowAntigravity}, GeminiApp={settings.FollowGeminiApp}, RunEntry={enabled}");
    }

    public void DisableFollow()
    {
        CleanupLegacyFiles();
        _settingsService.SetFollowWatcherRun(false);
        AppLogger.Info("AppFollow", "已停用应用跟随联动并清除开机待机入口");
    }

    public void CleanupLegacyFiles()
    {
        try
        {
            if (Directory.Exists(StartupFolderPath))
            {
                string legacyStartupVbs = Path.Combine(StartupFolderPath, LegacyStartupScriptName);
                if (File.Exists(legacyStartupVbs))
                    File.Delete(legacyStartupVbs);
            }
        }
        catch
        {
            // Ignore transient IO or permission errors during legacy cleanup.
        }

        string folder = _settingsService.SettingsFolder;
        if (!Directory.Exists(folder))
            return;

        foreach (string fileName in LegacyAuxiliaryFiles)
        {
            string fullPath = Path.Combine(folder, fileName);
            if (File.Exists(fullPath))
            {
                try { File.Delete(fullPath); }
                catch { }
            }
        }
    }
}
