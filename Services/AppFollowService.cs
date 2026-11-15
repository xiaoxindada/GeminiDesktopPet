using System.Diagnostics;
using System.IO;
using GeminiDesktopPet.Models;

namespace GeminiDesktopPet.Services;

/// <summary>
/// Manages native process-based app-follow standby mode, background cold-start watcher handoff,
/// and HKCU Run registration, removing any legacy Startup VBScript or auxiliary files.
/// </summary>
public sealed class AppFollowService
{
    public const string MainMutexName = "GeminiDesktopPet_SingleInstance_Mutex";
    public const string WatcherMutexName = "GeminiDesktopPet_FollowWatcher_Mutex";
    public const string WakeEventName = "GeminiDesktopPet_WakeSignal_Event";
    public const string WakeAgEventName = "GeminiDesktopPet_WakeAgSignal_Event";
    public const string WakeGeminiEventName = "GeminiDesktopPet_WakeGeminiSignal_Event";
    public const string ReloadFollowEventName = "GeminiDesktopPet_ReloadFollowSignal_Event";
    public const string CloseEventName = "GeminiDesktopPet_CloseSignal_Event";
    public const string StopWatcherEventName = "GeminiDesktopPet_StopFollowWatcher_Event";

    private const string LegacyStartupScriptName = "GeminiDesktopPet_FollowWatcher.vbs";
    private const int WatcherPollIntervalMs = 1200;

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
        if (!enabled)
            SignalStopFollowWatcher();
        AppLogger.Info("AppFollow", $"同步应用联动状态：Antigravity={settings.FollowAntigravity}, GeminiApp={settings.FollowGeminiApp}, RunEntry={enabled}");
    }

    public void DisableFollow()
    {
        CleanupLegacyFiles();
        _settingsService.SetFollowWatcherRun(false);
        SignalStopFollowWatcher();
        AppLogger.Info("AppFollow", "已停用应用跟随联动并清除开机待机入口");
    }

    public static bool TrySignalNamedEvent(string eventName)
    {
        try
        {
            using var ev = EventWaitHandle.OpenExisting(eventName);
            ev.Set();
            return true;
        }
        catch
        {
            return false;
        }
    }

    public static bool SignalStopFollowWatcher()
        => TrySignalNamedEvent(StopWatcherEventName);

    public static bool IsMainInstanceRunning()
    {
        try
        {
            if (Mutex.TryOpenExisting(MainMutexName, out var mainMutex))
            {
                mainMutex.Dispose();
                return true;
            }
        }
        catch
        {
        }
        return false;
    }

    /// <summary>
    /// Spawns the lightweight native follow-watcher process (--follow-watcher).
    /// Must be called after the main GUI releases MainMutexName when handing off on exit.
    /// </summary>
    public static bool SpawnFollowWatcher(bool fromExit)
    {
        string args = fromExit ? "--follow-watcher --from-exit" : "--follow-watcher";
        using var proc = TryLaunchPetWithArgs(args);
        if (proc == null)
        {
            AppLogger.Warn("AppFollow", $"启动后台冷启动监听进程失败：args={args}");
            return false;
        }

        AppLogger.Info("AppFollow", $"已交接至后台冷启动监听进程：args={args}");
        return true;
    }

    /// <summary>
    /// Runs the UI-free managed watcher loop so cold-starting Antigravity or Gemini
    /// still wakes the pet even after the user exited the main window/tray.
    /// </summary>
    public static int RunFollowWatcher(bool fromExit)
    {
        var settingsService = new SettingsService();
        var settings = settingsService.Load();
        AppLogger.Initialize(settings.EnableRuntimeLog);
        new AppFollowService(settingsService).CleanupLegacyFiles();

        if (!AnyFollowEnabled(settings))
        {
            AppLogger.Info("FollowWatcher", "未启用任何跟随应用联动，后台监听退出");
            return 0;
        }

        using var watcherMutex = new Mutex(true, WatcherMutexName, out bool ownsWatcherMutex);
        if (!ownsWatcherMutex)
        {
            AppLogger.Info("FollowWatcher", "已有后台监听实例在运行，当前进程退出");
            return 0;
        }

        using var stopEvent = new EventWaitHandle(false, EventResetMode.ManualReset, StopWatcherEventName, out bool createdStopEvent);
        if (!createdStopEvent)
            stopEvent.Reset();

        // Exit immediately if the main GUI already acquired the single-instance mutex.
        if (IsMainInstanceRunning())
        {
            AppLogger.Info("FollowWatcher", "主程序已运行，后台监听让位退出");
            return 0;
        }

        if (!fromExit)
        {
            string? alreadyRunningKey = AntigravityWatcherService.DetectRunningFollowedAppKey(settings);
            if (!string.IsNullOrEmpty(alreadyRunningKey))
            {
                AppLogger.Info("FollowWatcher", $"冷启动监听检测到目标已运行，立即唤醒主程序：{alreadyRunningKey}");
                using var spawned = TryLaunchPetWithArgs($"--woken-by:{alreadyRunningKey}");
                if (spawned != null)
                    WaitForMainTakeoverOrStop(spawned, stopEvent);
                return 0;
            }
        }

        // Baseline current active app processes on exit-handoff so closing the pet while an app
        // is already open waits for the next fresh app launch rather than immediately reopening.
        var initialAg = fromExit ? AntigravityWatcherService.SampleAntigravityActivity() : new AppActivitySnapshot(false, [], []);
        var initialGem = fromExit ? AntigravityWatcherService.SampleGeminiActivity() : new AppActivitySnapshot(false, [], []);
        bool wasAgRunning = initialAg.IsRunning;
        bool wasGeminiRunning = initialGem.IsRunning;
        HashSet<int> knownAgPids = initialAg.ActivePids;
        HashSet<int> knownGeminiPids = initialGem.ActivePids;
        DateTime lastAgStartUtc = DateTime.MinValue;
        DateTime lastGeminiStartUtc = DateTime.MinValue;

        string settingsFilePath = Path.Combine(settingsService.SettingsFolder, "settings.json");
        DateTime lastSettingsWriteUtc = GetFileLastWriteUtcSafe(settingsFilePath);

        AppLogger.Info(
            "FollowWatcher",
            $"后台冷启动监听已启动：fromExit={fromExit}, baseline=(AG:{wasAgRunning}[{knownAgPids.Count}], Gemini:{wasGeminiRunning}[{knownGeminiPids.Count}]), follow=(AG:{settings.FollowAntigravity}, Gemini:{settings.FollowGeminiApp})");

        while (!stopEvent.WaitOne(WatcherPollIntervalMs))
        {
            if (IsMainInstanceRunning())
            {
                AppLogger.Info("FollowWatcher", "检测到主程序启动，后台监听自动退出");
                break;
            }

            DateTime currentWriteUtc = GetFileLastWriteUtcSafe(settingsFilePath);
            if (currentWriteUtc != lastSettingsWriteUtc)
            {
                lastSettingsWriteUtc = currentWriteUtc;
                settings = settingsService.Load();
                AppLogger.SetEnabled(settings.EnableRuntimeLog);
                if (!AnyFollowEnabled(settings))
                {
                    AppLogger.Info("FollowWatcher", "检测到设置已关闭全部跟随联动，后台监听退出");
                    break;
                }
            }

            var agSnap = AntigravityWatcherService.SampleAntigravityActivity();
            var gemSnap = AntigravityWatcherService.SampleGeminiActivity();

            AntigravityWatcherService.EvaluateTransition(
                agSnap, ref wasAgRunning, ref knownAgPids, ref lastAgStartUtc, out bool agStarted, out _);
            AntigravityWatcherService.EvaluateTransition(
                gemSnap, ref wasGeminiRunning, ref knownGeminiPids, ref lastGeminiStartUtc, out bool geminiStarted, out _);

            bool wakeByAg = settings.FollowAntigravity && agStarted;
            bool wakeByGemini = settings.FollowGeminiApp && geminiStarted;

            if (!wakeByAg && !wakeByGemini)
                continue;

            string appKey = wakeByAg ? "antigravity" : "gemini";
            AppLogger.Info("FollowWatcher", $"检测到冷启动目标应用：{appKey}，正在唤醒桌宠主程序");

            using var spawned = TryLaunchPetWithArgs($"--woken-by:{appKey}");
            if (spawned == null)
                continue;

            if (WaitForMainTakeoverOrStop(spawned, stopEvent))
                return 0;
        }

        AppLogger.Info("FollowWatcher", "后台冷启动监听已收到停止信号并退出");
        return 0;
    }

    private static Process? TryLaunchPetWithArgs(string arguments)
    {
        string? exePath = Environment.ProcessPath;
        if (string.IsNullOrWhiteSpace(exePath))
            return null;

        try
        {
            return Process.Start(new ProcessStartInfo(exePath, arguments)
            {
                WorkingDirectory = Path.GetDirectoryName(exePath) ?? AppContext.BaseDirectory,
                UseShellExecute = true
            });
        }
        catch (Exception ex)
        {
            AppLogger.Warn("FollowWatcher", $"启动桌宠进程失败 ({arguments})：{ex.Message}");
            return null;
        }
    }

    private static bool WaitForMainTakeoverOrStop(Process spawned, EventWaitHandle stopEvent)
    {
        for (int i = 0; i < 60; i++)
        {
            if (stopEvent.WaitOne(120) || IsMainInstanceRunning())
                return true;

            try
            {
                if (spawned.HasExited)
                    return stopEvent.WaitOne(0) || IsMainInstanceRunning();
            }
            catch
            {
            }
        }
        return stopEvent.WaitOne(0) || IsMainInstanceRunning();
    }

    private static DateTime GetFileLastWriteUtcSafe(string path)
    {
        try
        {
            return File.Exists(path) ? File.GetLastWriteTimeUtc(path) : DateTime.MinValue;
        }
        catch
        {
            return DateTime.MinValue;
        }
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
