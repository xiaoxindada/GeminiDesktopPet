using System.Diagnostics;
using System.IO;
using GeminiDesktopPet.Models;

namespace GeminiDesktopPet.Services;

public sealed class FollowWatcherOptions
{
    public bool FromExit { get; init; }
    public string? CustomSettingsFolder { get; init; }
    public string MainMutexName { get; init; } = AppFollowService.MainMutexName;
    public string WatcherMutexName { get; init; } = AppFollowService.WatcherMutexName;
    public string StopWatcherEventName { get; init; } = AppFollowService.StopWatcherEventName;
    public int PollIntervalMs { get; init; } = 1200;
    public Func<AppActivitySnapshot>? SampleAntigravity { get; init; }
    public Func<AppActivitySnapshot>? SampleGemini { get; init; }
    public Func<string, bool>? CustomWakeLauncher { get; init; }
}

/// <summary>
/// Manages native process-based app-follow standby mode, background cold-start watcher handoff,
/// and HKCU Run registration.
/// </summary>
public sealed class AppFollowService
{
    public const string MainMutexName = "GeminiDesktopPet_SingleInstance_Mutex";
    public const string WatcherMutexName = "GeminiDesktopPet_FollowWatcher_Mutex";
    public const string WakeEventName = "GeminiDesktopPet_WakeSignal_Event";
    public const string WakeAgEventName = "GeminiDesktopPet_WakeAgSignal_Event";
    public const string WakeGeminiEventName = "GeminiDesktopPet_WakeGeminiSignal_Event";
    public const string CloseEventName = "GeminiDesktopPet_CloseSignal_Event";
    public const string StopWatcherEventName = "GeminiDesktopPet_StopFollowWatcher_Event";

    private const int WatcherPollIntervalMs = 1200;

    private readonly SettingsService _settingsService;

    public AppFollowService(SettingsService settingsService)
    {
        _settingsService = settingsService;
        Directory.CreateDirectory(settingsService.SettingsFolder);
    }

    public static bool AnyFollowEnabled(PetSettings settings)
        => settings.FollowAntigravity || settings.FollowGeminiApp;

    public bool IsFollowLogonEnabled()
        => _settingsService.IsFollowWatcherRunEnabled() || _settingsService.IsAutoStartEnabled();

    public void SyncAndApply(PetSettings settings)
    {
        bool enabled = AnyFollowEnabled(settings);
        _settingsService.SetFollowWatcherRun(enabled);
        if (enabled)
        {
            if (!IsFollowWatcherRunning())
                SpawnFollowWatcher(fromExit: true);
        }
        else
        {
            SignalStopFollowWatcher();
        }
        AppLogger.Info("AppFollow", $"同步应用联动状态：Antigravity={settings.FollowAntigravity}, GeminiApp={settings.FollowGeminiApp}, RunEntry={enabled}");
    }

    public void DisableFollow()
    {
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

    public static bool SignalStopFollowWatcher(string? stopEventName = null)
        => TrySignalNamedEvent(stopEventName ?? StopWatcherEventName);

    public static bool IsMainInstanceRunning(string? mainMutexName = null)
    {
        try
        {
            if (Mutex.TryOpenExisting(mainMutexName ?? MainMutexName, out var mainMutex))
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

    public static bool IsFollowWatcherRunning(string? watcherMutexName = null)
    {
        try
        {
            if (Mutex.TryOpenExisting(watcherMutexName ?? WatcherMutexName, out var watcherMutex))
            {
                watcherMutex.Dispose();
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
    /// always wakes the pet whenever the main GUI is not currently running.
    /// </summary>
    public static int RunFollowWatcher(bool fromExit)
        => RunFollowWatcherCore(new FollowWatcherOptions { FromExit = fromExit, PollIntervalMs = WatcherPollIntervalMs });

    public static int RunFollowWatcherCore(FollowWatcherOptions options)
    {
        var settingsService = new SettingsService(options.CustomSettingsFolder);
        var settings = settingsService.Load();
        if (string.IsNullOrWhiteSpace(options.CustomSettingsFolder))
            AppLogger.Initialize(settings.EnableRuntimeLog);

        if (!AnyFollowEnabled(settings))
        {
            AppLogger.Info("FollowWatcher", "未启用任何跟随应用联动，后台监听退出");
            return 0;
        }

        using var watcherMutex = new Mutex(true, options.WatcherMutexName, out bool ownsWatcherMutex);
        if (!ownsWatcherMutex)
        {
            AppLogger.Info("FollowWatcher", "已有后台监听实例在运行，当前进程退出");
            return 0;
        }

        using var stopEvent = new EventWaitHandle(false, EventResetMode.ManualReset, options.StopWatcherEventName, out bool createdStopEvent);
        if (!createdStopEvent)
            stopEvent.Reset();

        Func<AppActivitySnapshot> sampleAg = options.SampleAntigravity ?? AntigravityWatcherService.SampleAntigravityActivity;
        Func<AppActivitySnapshot> sampleGem = options.SampleGemini ?? AntigravityWatcherService.SampleGeminiActivity;

        if (!options.FromExit && !IsMainInstanceRunning(options.MainMutexName))
        {
            string? alreadyRunningKey = AntigravityWatcherService.DetectRunningFollowedAppKey(settings, sampleAg, sampleGem);
            if (!string.IsNullOrEmpty(alreadyRunningKey))
            {
                AppLogger.Info("FollowWatcher", $"冷启动监听检测到目标已运行，立即唤醒主程序：{alreadyRunningKey}");
                if (LaunchWakeTarget(options, $"--woken-by:{alreadyRunningKey}", stopEvent))
                    return 0;
            }
        }

        // Baseline current active app processes so already-open apps do not immediately trigger
        // until the next fresh launch or primary process creation.
        var initialAg = sampleAg();
        var initialGem = sampleGem();
        bool wasAgRunning = initialAg.IsRunning;
        bool wasGeminiRunning = initialGem.IsRunning;
        HashSet<int> knownAgPids = new(initialAg.ActivePids);
        HashSet<int> knownGeminiPids = new(initialGem.ActivePids);
        DateTime lastAgStartUtc = initialAg.IsRunning ? DateTime.UtcNow : DateTime.MinValue;
        DateTime lastGeminiStartUtc = initialGem.IsRunning ? DateTime.UtcNow : DateTime.MinValue;

        string settingsFilePath = settingsService.SettingsFilePath;
        DateTime lastSettingsWriteUtc = GetFileLastWriteUtcSafe(settingsFilePath);
        int pollMs = Math.Max(15, options.PollIntervalMs);

        AppLogger.Info(
            "FollowWatcher",
            $"后台冷启动监听已启动：fromExit={options.FromExit}, baseline=(AG:{wasAgRunning}[{knownAgPids.Count}], Gemini:{wasGeminiRunning}[{knownGeminiPids.Count}]), follow=(AG:{settings.FollowAntigravity}, Gemini:{settings.FollowGeminiApp})");

        while (!stopEvent.WaitOne(pollMs))
        {
            DateTime currentWriteUtc = GetFileLastWriteUtcSafe(settingsFilePath);
            if (currentWriteUtc != lastSettingsWriteUtc)
            {
                lastSettingsWriteUtc = currentWriteUtc;
                settings = settingsService.Load();
                if (string.IsNullOrWhiteSpace(options.CustomSettingsFolder))
                    AppLogger.SetEnabled(settings.EnableRuntimeLog);
                if (!AnyFollowEnabled(settings))
                {
                    AppLogger.Info("FollowWatcher", "检测到设置已关闭全部跟随联动，后台监听退出");
                    break;
                }
            }

            var agSnap = sampleAg();
            var gemSnap = sampleGem();

            AntigravityWatcherService.EvaluateTransition(
                agSnap, ref wasAgRunning, ref knownAgPids, ref lastAgStartUtc, out bool agStarted, out _);
            AntigravityWatcherService.EvaluateTransition(
                gemSnap, ref wasGeminiRunning, ref knownGeminiPids, ref lastGeminiStartUtc, out bool geminiStarted, out _);

            bool wakeByAg = settings.FollowAntigravity && agStarted;
            bool wakeByGemini = settings.FollowGeminiApp && geminiStarted;

            if (!wakeByAg && !wakeByGemini)
                continue;

            if (IsMainInstanceRunning(options.MainMutexName))
                continue;

            string appKey = wakeByAg ? "antigravity" : "gemini";
            AppLogger.Info("FollowWatcher", $"检测到冷启动目标应用：{appKey}，正在唤醒桌宠主程序");

            if (LaunchWakeTarget(options, $"--woken-by:{appKey}", stopEvent))
                return 0;
        }

        AppLogger.Info("FollowWatcher", "后台冷启动监听已收到停止信号并退出");
        return 0;
    }

    private static bool LaunchWakeTarget(FollowWatcherOptions options, string arguments, EventWaitHandle stopEvent)
    {
        if (options.CustomWakeLauncher != null)
        {
            options.CustomWakeLauncher(arguments);
            return stopEvent.WaitOne(0);
        }

        using var spawned = TryLaunchPetWithArgs(arguments);
        if (spawned == null)
            return false;

        return WaitForMainTakeoverOrStop(spawned, stopEvent, options.MainMutexName);
    }

    private static Process? TryLaunchPetWithArgs(string arguments)
    {
        string? exePath = Environment.ProcessPath;
        if (string.IsNullOrWhiteSpace(exePath))
            return null;

        try
        {
            return Process.Start(new ProcessStartInfo
            {
                FileName = exePath,
                Arguments = arguments,
                WorkingDirectory = Path.GetDirectoryName(exePath) ?? AppContext.BaseDirectory,
                UseShellExecute = false
            });
        }
        catch (Exception ex)
        {
            AppLogger.Warn("FollowWatcher", $"启动桌宠进程失败 ({arguments})：{ex.Message}");
            return null;
        }
    }

    private static bool WaitForMainTakeoverOrStop(Process spawned, EventWaitHandle stopEvent, string mainMutexName)
    {
        for (int i = 0; i < 60; i++)
        {
            if (stopEvent.WaitOne(120))
                return true;
            if (IsMainInstanceRunning(mainMutexName))
                return false;

            try
            {
                if (spawned.HasExited)
                    return stopEvent.WaitOne(0);
            }
            catch
            {
            }
        }
        return stopEvent.WaitOne(0);
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
}
