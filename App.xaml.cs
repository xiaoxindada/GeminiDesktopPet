using System.Threading;
using System.Windows;
using GeminiDesktopPet.Models;
using GeminiDesktopPet.Services;
using GeminiDesktopPet.Views;

namespace GeminiDesktopPet;

public partial class App : System.Windows.Application
{
    private const string MutexName = "GeminiDesktopPet_SingleInstance_Mutex";
    private const string WakeEventName = "GeminiDesktopPet_WakeSignal_Event";
    private const string CloseEventName = "GeminiDesktopPet_CloseSignal_Event";

    private Mutex? _instanceMutex;
    private bool _ownsMutex;
    private EventWaitHandle? _wakeEvent;
    private EventWaitHandle? _closeEvent;
    private CancellationTokenSource? _wakeCts;

    private SettingsService _settingsService = null!;
    private PetSettings _settings = null!;
    private SpriteManager _spriteManager = null!;
    private SoundService _soundService = null!;
    private SystemMonitorService _systemMonitor = null!;
    private GeminiChatService _chatService = null!;
    private AntigravityWatcherService _agWatcher = null!;
    private AntigravityQuotaService _quotaService = null!;
    private VbsFollowService _vbsFollowService = null!;
    private TrayIconService _trayService = null!;
    private PetWindow _petWindow = null!;

    private ProfileWindow? _profileWindow;
    private SettingsWindow? _settingsWindow;
    private QuotaWindow? _quotaWindow;

    protected override void OnStartup(StartupEventArgs e)
    {
        base.OnStartup(e);

        if (e.Args.Length >= 2 && e.Args[0] == "--smoke-render")
        {
            RunSmokeRender(e.Args[1]);
            return;
        }

        if (e.Args.Any(a => string.Equals(a, "--close-pet", StringComparison.OrdinalIgnoreCase)))
        {
            try
            {
                using var closeEv = EventWaitHandle.OpenExisting(CloseEventName);
                closeEv.Set();
            }
            catch
            {
            }
            Shutdown(0);
            return;
        }

        if (e.Args.Any(a => string.Equals(a, "--follow-flag:on", StringComparison.OrdinalIgnoreCase)))
        {
            var svc = new SettingsService();
            var cfg = svc.Load();
            if (!cfg.FollowAntigravity && !cfg.FollowGeminiApp)
            {
                cfg.FollowAntigravity = true;
                cfg.FollowGeminiApp = true;
                svc.Save(cfg);
            }
            Shutdown(0);
            return;
        }

        if (e.Args.Any(a => string.Equals(a, "--follow-flag:off", StringComparison.OrdinalIgnoreCase)))
        {
            var svc = new SettingsService();
            var cfg = svc.Load();
            cfg.FollowAntigravity = false;
            cfg.FollowGeminiApp = false;
            svc.Save(cfg);
            Shutdown(0);
            return;
        }

        bool followStartupArg = e.Args.Any(a => string.Equals(a, "--follow-antigravity", StringComparison.OrdinalIgnoreCase));
        string? wokenByArg = e.Args
            .FirstOrDefault(a => a.StartsWith("--woken-by:", StringComparison.OrdinalIgnoreCase))
            ?.Split(':', 2)
            .ElementAtOrDefault(1)
            ?.Trim()
            .ToLowerInvariant();

        _instanceMutex = new Mutex(true, MutexName, out bool createdNew);
        _ownsMutex = createdNew;
        if (!createdNew)
        {
            if (!followStartupArg)
            {
                try
                {
                    if (!string.IsNullOrWhiteSpace(wokenByArg))
                        new VbsFollowService(new SettingsService()).WriteWakeReason(wokenByArg);

                    using var ev = EventWaitHandle.OpenExisting(WakeEventName);
                    ev.Set();
                }
                catch
                {
                    // Existing instance may be starting up or shutting down.
                }
            }
            Shutdown();
            return;
        }

        DispatcherUnhandledException += (_, args) =>
        {
            try
            {
                string logDir = System.IO.Path.Combine(
                    Environment.GetFolderPath(Environment.SpecialFolder.ApplicationData),
                    "GeminiDesktopPet");
                System.IO.Directory.CreateDirectory(logDir);
                System.IO.File.AppendAllText(
                    System.IO.Path.Combine(logDir, "error.log"),
                    $"[{DateTime.Now:yyyy-MM-dd HH:mm:ss}] {args.Exception}\n\n");
            }
            catch { }
        };

        _settingsService = new SettingsService();
        _settings = _settingsService.Load();
        _vbsFollowService = new VbsFollowService(_settingsService);
        _vbsFollowService.SyncAndApply(_settings);

        if (followStartupArg && !_settings.FollowAntigravity && !_settings.FollowGeminiApp && !_settingsService.IsFollowWatcherRunEnabled())
        {
            Shutdown();
            return;
        }

        _wakeEvent = new EventWaitHandle(false, EventResetMode.AutoReset, WakeEventName);
        _closeEvent = new EventWaitHandle(false, EventResetMode.AutoReset, CloseEventName);
        _wakeCts = new CancellationTokenSource();
        StartWakeListener(_wakeCts.Token);
        StartCloseListener(_wakeCts.Token);

        _spriteManager = new SpriteManager();
        _soundService = new SoundService { Enabled = _settings.SoundEnabled };
        _systemMonitor = new SystemMonitorService();
        _chatService = new GeminiChatService();
        _agWatcher = new AntigravityWatcherService();
        _quotaService = new AntigravityQuotaService();

        _petWindow = new PetWindow(
            _settings,
            _settingsService,
            _spriteManager,
            _soundService,
            _systemMonitor,
            _chatService);

        _trayService = new TrayIconService();
        _trayService.SyncState(_settings);

        WireEvents();
        _agWatcher.Start();

        if (followStartupArg && !AntigravityWatcherService.CheckAnyFollowedTargetRunning(_settings))
        {
            // Stay quietly in standby (tray icon active) until a followed app launches or user clicks tray.
            _petWindow.ApplySettingsToWindow();
            return;
        }

        _petWindow.Show();
        string? wakeReason = wokenByArg ?? _vbsFollowService.ConsumeWakeReason();
        string? wakeGreeting = FormatWakeGreeting(wakeReason, followStartupArg);
        if (!string.IsNullOrEmpty(wakeGreeting))
            _petWindow.ShowAndGreet(wakeGreeting);
    }

    private void WireEvents()
    {
        _petWindow.OpenProfileRequested += OpenProfileWindow;
        _petWindow.OpenSettingsRequested += OpenSettingsWindow;
        _petWindow.OpenQuotaRequested += OpenQuotaWindow;
        _petWindow.StandbyRequested += EnterStandbyMode;
        _petWindow.FollowAntigravityChanged += SetFollowAntigravity;
        _petWindow.FollowGeminiAppChanged += SetFollowGeminiApp;
        _petWindow.SettingsSynced += () =>
        {
            _trayService.SyncState(_settings);
            _settingsWindow?.RefreshFromSettings();
        };
        _petWindow.ExitRequested += GracefullyExit;

        _trayService.ShowAndGreetRequested += () => _petWindow.ShowAndGreet();
        _trayService.OpenChatRequested += () =>
        {
            if (!_petWindow.IsVisible) _petWindow.Show();
            _petWindow.OpenChatDialog();
        };
        _trayService.PatRequested += () =>
        {
            if (!_petWindow.IsVisible) _petWindow.Show();
            _petWindow.TriggerPat();
        };
        _trayService.ResetPositionRequested += () =>
        {
            if (!_petWindow.IsVisible) _petWindow.Show();
            _petWindow.MoveToBottomRightCorner();
            _petWindow.ShowAndGreet();
        };
        _trayService.StandbyForAntigravityRequested += EnterStandbyMode;
        _trayService.StyleToggleRequested += () =>
        {
            if (!_petWindow.IsVisible) _petWindow.Show();
            _petWindow.ToggleStyleMode();
        };
        _trayService.AntiGravityToggled += enabled =>
        {
            _settings.AntiGravityMode = enabled;
            if (enabled) _settings.DockedSide = DockSide.None;
            _settingsService.Save(_settings);
            _petWindow.ApplySettingsToWindow();
            _trayService.SyncState(_settings);
            _settingsWindow?.RefreshFromSettings();
        };
        _trayService.AutoWalkToggled += enabled =>
        {
            _settings.AutoWalk = enabled;
            _settingsService.Save(_settings);
            _petWindow.ApplySettingsToWindow();
            _trayService.SyncState(_settings);
            _settingsWindow?.RefreshFromSettings();
        };
        _trayService.SpeechBubbleToggled += enabled =>
        {
            _settings.ShowSpeechBubble = enabled;
            _settingsService.Save(_settings);
            _petWindow.SetSpeechBubbleEnabled(enabled);
            _trayService.SyncState(_settings);
            _settingsWindow?.RefreshFromSettings();
        };
        _trayService.FollowAntigravityToggled += SetFollowAntigravity;
        _trayService.FollowGeminiAppToggled += SetFollowGeminiApp;
        _trayService.AlwaysOnTopToggled += enabled =>
        {
            _settings.AlwaysOnTop = enabled;
            _settingsService.Save(_settings);
            _petWindow.ApplySettingsToWindow();
            _trayService.SyncState(_settings);
            _settingsWindow?.RefreshFromSettings();
        };
        _trayService.ClickThroughToggled += enabled =>
        {
            _settings.ClickThrough = enabled;
            _settingsService.Save(_settings);
            _petWindow.ApplySettingsToWindow();
            _trayService.SyncState(_settings);
            if (!enabled)
                _petWindow.ShowAndGreet();
        };
        _trayService.SoundToggled += enabled =>
        {
            _settings.SoundEnabled = enabled;
            _settingsService.Save(_settings);
            _petWindow.ApplySettingsToWindow();
            _trayService.SyncState(_settings);
            _settingsWindow?.RefreshFromSettings();
        };
        _trayService.OpenProfileRequested += OpenProfileWindow;
        _trayService.OpenSettingsRequested += OpenSettingsWindow;
        _trayService.OpenQuotaRequested += OpenQuotaWindow;
        _trayService.ExitRequested += GracefullyExit;

        _agWatcher.AntigravityStarted += () => HandleFollowedAppStarted(_settings.FollowAntigravity, "antigravity");
        _agWatcher.AntigravityExited += HandleFollowedAppExited;
        _agWatcher.GeminiAppStarted += () => HandleFollowedAppStarted(_settings.FollowGeminiApp, "gemini");
        _agWatcher.GeminiAppExited += HandleFollowedAppExited;
    }

    private void HandleFollowedAppStarted(bool followEnabled, string appKey)
    {
        _settingsWindow?.UpdateAntigravityBadge();
        if (!followEnabled) return;

        string msg = appKey == "gemini"
            ? (_petWindow.IsVisible
                ? "Gemini 桌面 App 启动啦！本双子娘同源同步在线协助你喵✦"
                : "检测到 Gemini 桌面 App 启动！Gemini 娘已同步上线陪你喵✦")
            : (_petWindow.IsVisible
                ? "Antigravity 启动啦！双子娘随时待命协助你喵✦"
                : "检测到 Antigravity 启动！Gemini 娘已同步上线陪你喵✦");
        _petWindow.ShowAndGreet(msg);
    }

    private void HandleFollowedAppExited()
    {
        _settingsWindow?.UpdateAntigravityBadge();
        if ((_settings.FollowAntigravity || _settings.FollowGeminiApp)
            && _settings.SleepWhenAntigravityExits
            && _petWindow.IsVisible
            && !AntigravityWatcherService.CheckAnyFollowedTargetRunning(_settings))
        {
            if (!_vbsFollowService.IsWatcherRunning())
                EnterStandbyMode();
        }
    }

    private void SetFollowAntigravity(bool enabled)
        => UpdateFollowSetting(() => _settings.FollowAntigravity = enabled);

    private void SetFollowGeminiApp(bool enabled)
        => UpdateFollowSetting(() => _settings.FollowGeminiApp = enabled);

    private void UpdateFollowSetting(Action apply)
    {
        apply();
        _settingsService.Save(_settings);
        _vbsFollowService.SyncAndApply(_settings, forceRestart: true);
        _petWindow.ApplySettingsToWindow();
        _trayService.SyncState(_settings);
        _settingsWindow?.RefreshFromSettings();
    }

    private void EnterStandbyMode()
    {
        if (!_settings.FollowAntigravity && !_settings.FollowGeminiApp)
            SetFollowAntigravity(true);

        _petWindow.Hide();
        _trayService.ShowBalloon("Gemini 娘待机中 💤", "已隐藏等待 Antigravity / Gemini 桌面 App 启动后自动唤醒（双击托盘图标可随时呼唤）。");
    }

    private void StartWakeListener(CancellationToken token)
    {
        Task.Run(() =>
        {
            while (!token.IsCancellationRequested)
            {
                if (_wakeEvent?.WaitOne(400) == true && !token.IsCancellationRequested)
                {
                    string? reason = _vbsFollowService.ConsumeWakeReason();
                    string? greeting = FormatWakeGreeting(reason, false);
                    Dispatcher.Invoke(() =>
                    {
                        if (!string.IsNullOrEmpty(greeting))
                            _petWindow.ShowAndGreet(greeting);
                        else
                            _petWindow.ShowAndGreet();
                    });
                }
            }
        }, token);
    }

    private void StartCloseListener(CancellationToken token)
    {
        Task.Run(() =>
        {
            while (!token.IsCancellationRequested)
            {
                if (_closeEvent?.WaitOne(400) == true && !token.IsCancellationRequested)
                {
                    Dispatcher.Invoke(GracefullyExit);
                    break;
                }
            }
        }, token);
    }

    private static string? FormatWakeGreeting(string? reason, bool followStartupArg)
    {
        return reason switch
        {
            "antigravity" => "检测到 Antigravity 启动！Gemini 娘已由 Startup VBS + bat 同步拉起喵✦",
            "gemini" => "检测到 Gemini 桌面 App 启动！Gemini 娘已由 Startup VBS + bat 同步拉起喵✦",
            _ => followStartupArg ? "检测到联动软件已启动！Gemini 娘同步上线喵✦" : null
        };
    }

    private void OpenProfileWindow()
    {
        if (_profileWindow == null || !_profileWindow.IsLoaded)
        {
            _profileWindow = new ProfileWindow(_spriteManager, _settings, key =>
            {
                if (!_petWindow.IsVisible) _petWindow.Show();
                _petWindow.TriggerExternalSpritePreview(key);
            });
        }
        _profileWindow.Show();
        _profileWindow.Activate();
    }

    private void OpenSettingsWindow()
    {
        if (_settingsWindow == null || !_settingsWindow.IsLoaded)
        {
            _settingsWindow = new SettingsWindow(_settings, _settingsService, _vbsFollowService);
            _settingsWindow.SettingsChanged += () =>
            {
                _petWindow.ApplySettingsToWindow();
                _trayService.SyncState(_settings);
            };
        }
        _settingsWindow.RefreshFromSettings();
        _settingsWindow.Show();
        _settingsWindow.Activate();
    }

    private void OpenQuotaWindow()
    {
        bool created = false;
        if (_quotaWindow == null || !_quotaWindow.IsLoaded)
        {
            _quotaWindow = new QuotaWindow(_quotaService, autoFetch: true);
            _quotaWindow.PrepareForFirstRenderReveal(_petWindow);
            created = true;
        }
        else
        {
            _quotaWindow.PositionNearPet(_petWindow);
        }

        if (!_quotaWindow.IsVisible)
            _quotaWindow.Show();

        if (!created)
        {
            _quotaWindow.PositionNearPet(_petWindow);
            _ = _quotaWindow.RefreshQuotaAsync();
        }
    }

    private void GracefullyExit()
    {
        _settingsService.Save(_settings);
        _wakeCts?.Cancel();
        _agWatcher.Dispose();
        _trayService.Dispose();
        _profileWindow?.Close();
        _settingsWindow?.Close();
        _quotaWindow?.Close();
        _petWindow.Close();
        Shutdown();
    }

    private void RunSmokeRender(string outDir)
    {
        System.IO.Directory.CreateDirectory(outDir);
        string isolatedSettingsDir = System.IO.Path.Combine(outDir, "_smoke_settings");
        var settingsService = new SettingsService(isolatedSettingsDir);
        var settings = new PetSettings();
        var sprites = new SpriteManager();
        var sound = new SoundService { Enabled = false };
        var monitor = new SystemMonitorService();
        var chat = new GeminiChatService();

        var petChibi = new PetWindow(settings, settingsService, sprites, sound, monitor, chat);
        var stdSettings = new PetSettings { StyleMode = PetStyleMode.Standard };
        var petStd = new PetWindow(stdSettings, settingsService, sprites, sound, monitor, chat);
        var profile = new ProfileWindow(sprites, settings);
        var vbsFollow = new VbsFollowService(settingsService);
        var settingsWin = new SettingsWindow(settings, settingsService, vbsFollow);
        var quotaSvc = new AntigravityQuotaService();
        var quotaWin = new QuotaWindow(quotaSvc, autoFetch: false, enableAutoClose: false);
        quotaWin.ShowActivated = false;
        quotaWin.Left = -2000;
        quotaWin.Top = 100;
        quotaWin.Show();
        quotaWin.UpdateLayout();
        RenderWindowShot(quotaWin, System.IO.Path.Combine(outDir, "quota_loading.png"));
        quotaWin.Hide();

        try
        {
            using var cts = new CancellationTokenSource(TimeSpan.FromSeconds(25));
            var liveSnap = quotaSvc.FetchQuotaAsync(cts.Token).GetAwaiter().GetResult();
            quotaWin.ApplySnapshot(liveSnap);
        }
        catch
        {
        }

        Window[] windows = [petChibi, petStd, profile, settingsWin, quotaWin];
        string[] names = ["pet_chibi.png", "pet_standard.png", "profile.png", "settings.png", "quota.png"];

        for (int i = 0; i < windows.Length; i++)
        {
            var win = windows[i];
            win.ShowActivated = false;
            win.Left = -2000;
            win.Top = 100;
            win.Show();
            win.UpdateLayout();
            RenderWindowShot(win, System.IO.Path.Combine(outDir, names[i]));
            win.Hide();
        }

        // Additional visual regression shots for walk rigs and Standard edge docking
        petChibi.ShowActivated = false;
        petChibi.Left = -2000;
        petChibi.Top = 100;
        petChibi.Show();
        petChibi.ConfigureSmokePreview(PetStyleMode.Chibi, walking: true, animTimeSec: 0.20, DockSide.None);
        RenderWindowShot(petChibi, System.IO.Path.Combine(outDir, "pet_chibi_walk.png"));
        petChibi.Hide();

        petStd.ShowActivated = false;
        petStd.Left = -2000;
        petStd.Top = 100;
        petStd.Show();
        petStd.ConfigureSmokePreview(PetStyleMode.Standard, walking: true, animTimeSec: 0.25, DockSide.None);
        RenderWindowShot(petStd, System.IO.Path.Combine(outDir, "pet_std_walk.png"));

        petStd.ConfigureSmokePreview(PetStyleMode.Standard, walking: false, animTimeSec: 0.0, DockSide.Left);
        RenderWindowShot(petStd, System.IO.Path.Combine(outDir, "pet_std_left.png"));

        petStd.ConfigureSmokePreview(PetStyleMode.Standard, walking: false, animTimeSec: 0.0, DockSide.Right);
        RenderWindowShot(petStd, System.IO.Path.Combine(outDir, "pet_std_right.png"));
        petStd.Hide();

        Shutdown(0);
    }

    private static void RenderWindowShot(Window win, string filePath)
    {
        int w = Math.Max(1, (int)Math.Ceiling(win.ActualWidth));
        int h = Math.Max(1, (int)Math.Ceiling(win.ActualHeight));
        var rtb = new System.Windows.Media.Imaging.RenderTargetBitmap(w, h, 96, 96, System.Windows.Media.PixelFormats.Pbgra32);
        rtb.Render(win);
        var enc = new System.Windows.Media.Imaging.PngBitmapEncoder();
        enc.Frames.Add(System.Windows.Media.Imaging.BitmapFrame.Create(rtb));
        using var fs = System.IO.File.Create(filePath);
        enc.Save(fs);
    }

    protected override void OnExit(ExitEventArgs e)
    {
        _wakeCts?.Cancel();
        _wakeEvent?.Dispose();
        _closeEvent?.Dispose();
        if (_ownsMutex)
        {
            try { _instanceMutex?.ReleaseMutex(); } catch { }
        }
        _instanceMutex?.Dispose();
        base.OnExit(e);
    }
}
