using System.Diagnostics;
using System.IO;
using System.Threading;
using System.Windows;
using GeminiDesktopPet.Models;
using GeminiDesktopPet.Services;
using GeminiDesktopPet.Views;

namespace GeminiDesktopPet;

public partial class App : System.Windows.Application
{
    private Mutex? _instanceMutex;
    private bool _ownsMutex;
    private EventWaitHandle? _wakeEvent;
    private EventWaitHandle? _wakeAgEvent;
    private EventWaitHandle? _wakeGeminiEvent;
    private EventWaitHandle? _closeEvent;
    private CancellationTokenSource? _wakeCts;
    private bool _isExiting;
    private bool _suppressWatcherOnExit;

    private SettingsService _settingsService = null!;
    private CharacterProfileService _characterService = null!;
    private PetSettings _settings = null!;
    private SpriteManager _spriteManager = null!;
    private SoundService _soundService = null!;
    private SystemMonitorService _systemMonitor = null!;
    private GeminiChatService _chatService = null!;
    private AntigravityWatcherService _agWatcher = null!;
    private AntigravityQuotaService _quotaService = null!;
    private UpdateService _updateService = null!;
    private AppFollowService _followService = null!;
    private TrayIconService _trayService = null!;
    private PetWindow _petWindow = null!;

    private ProfileWindow? _profileWindow;
    private CharacterEditorWindow? _characterEditorWindow;
    private SettingsWindow? _settingsWindow;
    private QuotaWindow? _quotaWindow;
    private UpdateWindow? _updateWindow;
    private UpdateCheckResult? _lastUpdateResult;

    protected override void OnStartup(StartupEventArgs e)
    {
        base.OnStartup(e);

        bool followStartupArg = e.Args.Any(a => string.Equals(a, "--follow-antigravity", StringComparison.OrdinalIgnoreCase));
        string? wokenByArg = e.Args
            .FirstOrDefault(a => a.StartsWith("--woken-by:", StringComparison.OrdinalIgnoreCase))
            ?.Split(':', 2)
            .ElementAtOrDefault(1)
            ?.Trim()
            .ToLowerInvariant();

        _instanceMutex = new Mutex(true, AppFollowService.MainMutexName, out bool createdNew);
        _ownsMutex = createdNew;
        if (!createdNew)
        {
            if (!followStartupArg)
            {
                string targetEventName = wokenByArg switch
                {
                    "antigravity" => AppFollowService.WakeAgEventName,
                    "gemini" => AppFollowService.WakeGeminiEventName,
                    _ => AppFollowService.WakeEventName
                };
                AppFollowService.TrySignalNamedEvent(targetEventName);
            }
            Shutdown();
            return;
        }

        DispatcherUnhandledException += (_, args) =>
        {
            AppLogger.Error("App", "捕获到未处理 UI 异常", args.Exception);
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
        _characterService = new CharacterProfileService(_settingsService.SettingsFolder);
        _settings = _settingsService.Load();
        AppLogger.Initialize(_settings.EnableRuntimeLog);
        AppLogger.Info("App", $"主程序启动：args=[{string.Join(" ", e.Args)}], followStartup={followStartupArg}, standbyFollow=({_settings.FollowAntigravity}/{_settings.FollowGeminiApp})");
        _followService = new AppFollowService(_settingsService);
        _followService.SyncAndApply(_settings);

        if (followStartupArg && !_settings.FollowAntigravity && !_settings.FollowGeminiApp && !_settingsService.IsFollowWatcherRunEnabled())
        {
            Shutdown();
            return;
        }

        _wakeEvent = new EventWaitHandle(false, EventResetMode.AutoReset, AppFollowService.WakeEventName);
        _wakeAgEvent = new EventWaitHandle(false, EventResetMode.AutoReset, AppFollowService.WakeAgEventName);
        _wakeGeminiEvent = new EventWaitHandle(false, EventResetMode.AutoReset, AppFollowService.WakeGeminiEventName);
        _closeEvent = new EventWaitHandle(false, EventResetMode.AutoReset, AppFollowService.CloseEventName);
        _wakeCts = new CancellationTokenSource();
        StartWakeListener(_wakeCts.Token);
        StartCloseListener(_wakeCts.Token);

        _spriteManager = new SpriteManager();
        _soundService = new SoundService { Enabled = _settings.SoundEnabled };
        _systemMonitor = new SystemMonitorService();
        _chatService = new GeminiChatService();
        _agWatcher = new AntigravityWatcherService();
        _quotaService = new AntigravityQuotaService();
        _updateService = new UpdateService();
        UpdateService.CleanupOldStagingFolders();

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
        if (_settings.AutoCheckUpdates && _wakeCts != null)
            _ = ScheduleBackgroundUpdateCheckAsync(_wakeCts.Token);

        if (followStartupArg && !AntigravityWatcherService.CheckAnyFollowedTargetRunning(_settings))
        {
            // Stay quietly in standby (tray icon active) until a followed app launches or user clicks tray.
            _petWindow.ApplySettingsToWindow();
            return;
        }

        string? detectedWakeKey = wokenByArg
            ?? (followStartupArg ? AntigravityWatcherService.DetectRunningFollowedAppKey(_settings) : null);
        string? wakeGreeting = FormatWakeGreeting(detectedWakeKey, followStartupArg);
        if (!string.IsNullOrEmpty(wakeGreeting))
            _petWindow.ShowAndGreet(wakeGreeting);
        else
            _petWindow.Show();
    }

    private void WireEvents()
    {
        _characterService.ProfileChanged += profile =>
        {
            _petWindow.SyncCharacterProfile(profile);
            _profileWindow?.ApplyProfile(profile);
        };

        _petWindow.OpenProfileRequested += OpenProfileWindow;
        _petWindow.OpenCharacterEditorRequested += OpenCharacterEditorWindow;
        _petWindow.OpenSettingsRequested += OpenSettingsWindow;
        _petWindow.OpenQuotaRequested += OpenQuotaWindow;
        _petWindow.OpenUpdateRequested += OpenUpdateWindow;
        _petWindow.StandbyRequested += EnterStandbyMode;
        _petWindow.FollowAntigravityChanged += SetFollowAntigravity;
        _petWindow.FollowGeminiAppChanged += SetFollowGeminiApp;
        _petWindow.SettingsSynced += () =>
        {
            _trayService.SyncState(_settings);
            _settingsWindow?.RefreshFromSettings();
        };
        _petWindow.Closed += (_, _) => GracefullyExit();
        SessionEnding += (_, _) =>
        {
            _suppressWatcherOnExit = true;
            GracefullyExit();
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
        _trayService.PinOnDesktopToggled += pinned =>
        {
            _settings.AutoWalk = !pinned;
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
        _trayService.RuntimeLogToggled += enabled =>
        {
            _settings.EnableRuntimeLog = enabled;
            AppLogger.SetEnabled(enabled);
            _settingsService.Save(_settings);
            _petWindow.ApplySettingsToWindow();
            _trayService.SyncState(_settings);
            _settingsWindow?.RefreshFromSettings();
        };
        _trayService.OpenProfileRequested += OpenProfileWindow;
        _trayService.OpenCharacterEditorRequested += OpenCharacterEditorWindow;
        _trayService.OpenSettingsRequested += OpenSettingsWindow;
        _trayService.OpenQuotaRequested += OpenQuotaWindow;
        _trayService.OpenUpdateRequested += OpenUpdateWindow;
        _trayService.ExitRequested += GracefullyExit;

        _agWatcher.AntigravityStarted += () => HandleFollowedAppStarted(_settings.FollowAntigravity, "antigravity");
        _agWatcher.AntigravityExited += HandleFollowedAppExited;
        _agWatcher.GeminiAppStarted += () => HandleFollowedAppStarted(_settings.FollowGeminiApp, "gemini");
        _agWatcher.GeminiAppExited += HandleFollowedAppExited;
    }

    private void HandleFollowedAppStarted(bool followEnabled, string appKey)
    {
        AppLogger.Info("Watcher", $"检测到目标应用启动：app={appKey}, followEnabled={followEnabled}, visible={_petWindow.IsVisible}");
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
        _followService.SyncAndApply(_settings);
        _petWindow.ApplySettingsToWindow();
        _trayService.SyncState(_settings);
        _settingsWindow?.RefreshFromSettings();
    }

    private void EnterStandbyMode()
    {
        if (!_settings.FollowAntigravity && !_settings.FollowGeminiApp)
            SetFollowAntigravity(true);

        AppLogger.Info("App", "进入托盘隐藏待机模式");
        _petWindow.Hide();
        _trayService.ShowBalloon("Gemini 娘待机中 💤", "已隐藏等待 Antigravity / Gemini 桌面 App 启动后自动唤醒（双击托盘图标可随时呼唤）。");
    }

    private void StartWakeListener(CancellationToken token)
    {
        if (_wakeEvent == null || _wakeAgEvent == null || _wakeGeminiEvent == null) return;
        WaitHandle[] handles = [_wakeEvent, _wakeAgEvent, _wakeGeminiEvent, token.WaitHandle];
        Task.Run(() =>
        {
            while (!token.IsCancellationRequested)
            {
                int idx = WaitHandle.WaitAny(handles, 400);
                if (idx == WaitHandle.WaitTimeout || idx == 3 || token.IsCancellationRequested)
                    continue;

                string? reason = idx switch
                {
                    1 => "antigravity",
                    2 => "gemini",
                    _ => null
                };

                try
                {
                    if (!Dispatcher.HasShutdownStarted)
                    {
                        Dispatcher.Invoke(() =>
                        {
                            string? greeting = FormatWakeGreeting(reason, false);
                            if (!string.IsNullOrEmpty(greeting))
                                _petWindow.ShowAndGreet(greeting);
                            else
                                _petWindow.ShowAndGreet();
                        });
                    }
                }
                catch
                {
                    break;
                }
            }
        }, token);
    }

    private void StartCloseListener(CancellationToken token)
    {
        if (_closeEvent == null) return;
        WaitHandle[] handles = [_closeEvent, token.WaitHandle];
        Task.Run(() =>
        {
            while (!token.IsCancellationRequested)
            {
                if (WaitHandle.WaitAny(handles, 400) != 0 || token.IsCancellationRequested)
                    continue;

                try
                {
                    if (!Dispatcher.HasShutdownStarted)
                        Dispatcher.Invoke(GracefullyExit);
                }
                catch
                {
                    break;
                }
                break;
            }
        }, token);
    }

    private static string? FormatWakeGreeting(string? reason, bool followStartupArg)
    {
        return reason switch
        {
            "antigravity" => "检测到 Antigravity 启动！Gemini 娘已同步拉起喵✦",
            "gemini" => "检测到 Gemini 桌面 App 启动！Gemini 娘已同步拉起喵✦",
            _ => followStartupArg ? "检测到联动软件已启动！Gemini 娘同步上线喵✦" : null
        };
    }

    private void OpenProfileWindow()
    {
        if (_profileWindow == null || !_profileWindow.IsLoaded)
        {
            _profileWindow = new ProfileWindow(_spriteManager, _settings, _characterService, key =>
            {
                if (!_petWindow.IsVisible) _petWindow.Show();
                _petWindow.TriggerExternalSpritePreview(key);
            });
            _profileWindow.OpenCharacterEditorRequested += OpenCharacterEditorWindow;
            _profileWindow.Closed += (_, _) => _profileWindow = null;
        }
        else
        {
            _profileWindow.ApplyProfile(_characterService.Current);
        }

        _profileWindow.Show();
        _profileWindow.Activate();
    }

    private void OpenCharacterEditorWindow()
    {
        if (_characterEditorWindow == null || !_characterEditorWindow.IsLoaded)
        {
            _characterEditorWindow = new CharacterEditorWindow(_spriteManager, _characterService);
            _characterEditorWindow.Closed += (_, _) => _characterEditorWindow = null;
        }
        else
        {
            _characterEditorWindow.RefreshFromService();
        }

        _characterEditorWindow.Show();
        _characterEditorWindow.Activate();
    }

    private void OpenSettingsWindow()
    {
        if (_settingsWindow == null || !_settingsWindow.IsLoaded)
        {
            _settingsWindow = new SettingsWindow(_settings, _settingsService, _followService);
            _settingsWindow.SettingsChanged += () =>
            {
                _petWindow.ApplySettingsToWindow();
                _trayService.SyncState(_settings);
            };
            _settingsWindow.OpenUpdateRequested += OpenUpdateWindow;
            _settingsWindow.OpenProfileEditorRequested += OpenCharacterEditorWindow;
            _settingsWindow.Closed += (_, _) => _settingsWindow = null;
        }
        _settingsWindow.RefreshFromSettings();
        _settingsWindow.UpdateLatestStatusBadge(_lastUpdateResult);
        _settingsWindow.Show();
        _settingsWindow.Activate();
    }

    private void OpenUpdateWindow()
    {
        bool created = false;
        if (_updateWindow == null || !_updateWindow.IsLoaded)
        {
            _updateWindow = new UpdateWindow(
                _updateService,
                _settings,
                _settingsService,
                GracefullyExit,
                _lastUpdateResult,
                beforeExitCleanup: () =>
                {
                    _suppressWatcherOnExit = true;
                    AppFollowService.SignalStopFollowWatcher();
                });
            _updateWindow.CheckCompleted += result => HandleUpdateResult(result, notifyUser: false);
            _updateWindow.Closed += (_, _) => _updateWindow = null;
            created = true;
        }
        _updateWindow.Show();
        _updateWindow.Activate();

        if (!created)
            _ = _updateWindow.RunCheckAsync();
    }

    private async Task ScheduleBackgroundUpdateCheckAsync(CancellationToken token)
    {
        try
        {
            await Task.Delay(TimeSpan.FromSeconds(4), token);
            var result = await _updateService.CheckForUpdateAsync(_settings.UpdateReleasesUrl, token);
            if (!token.IsCancellationRequested && !Dispatcher.HasShutdownStarted)
                await Dispatcher.InvokeAsync(() => HandleUpdateResult(result, notifyUser: true));
        }
        catch
        {
            // Startup check is strictly silent on cancellation or offline network.
        }
    }

    private void HandleUpdateResult(UpdateCheckResult result, bool notifyUser)
    {
        AppLogger.Info("Update", $"更新检查完成：ok={result.Succeeded}, avail={result.UpdateAvailable}, latest={result.LatestTag}, msg={result.StatusMessage}");
        _lastUpdateResult = result;
        _trayService.SetAvailableUpdate(result.Succeeded && result.UpdateAvailable ? result.LatestTag : null);
        _settingsWindow?.UpdateLatestStatusBadge(result);

        if (notifyUser
            && result.Succeeded
            && result.UpdateAvailable
            && !string.IsNullOrWhiteSpace(result.LatestTag)
            && !string.Equals(result.LatestTag, _settings.SkippedUpdateVersion, StringComparison.OrdinalIgnoreCase))
        {
            _trayService.ShowBalloon(
                $"发现新版本 {result.LatestTag} ✦",
                "上游 GitHub Releases 已发布新版本，点击托盘菜单「发现新版本」即可查看 Changelog 并更新。");
        }
    }

    private void OpenQuotaWindow()
    {
        bool created = false;
        if (_quotaWindow == null || !_quotaWindow.IsLoaded)
        {
            _quotaWindow = new QuotaWindow(_quotaService, autoFetch: true);
            _quotaWindow.PrepareForFirstRenderReveal(_petWindow);
            _quotaWindow.Closed += (_, _) => _quotaWindow = null;
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
        if (_isExiting) return;
        _isExiting = true;

        AppLogger.Info("App", "主程序优雅退出");
        _settingsService.Save(_settings);
        _wakeCts?.Cancel();
        _agWatcher.Dispose();
        _trayService.Dispose();
        try { _profileWindow?.Close(); } catch { }
        try { _characterEditorWindow?.Close(); } catch { }
        try { _settingsWindow?.ForceClose(); } catch { }
        try { _quotaWindow?.Close(); } catch { }
        try { _updateWindow?.Close(); } catch { }
        try { _petWindow.Close(); } catch { }

        bool handoffFollowWatcher = !_suppressWatcherOnExit
            && AppFollowService.AnyFollowEnabled(_settings)
            && !AppFollowService.IsFollowWatcherRunning();
        ReleaseSingleInstanceMutex();
        if (handoffFollowWatcher)
            AppFollowService.SpawnFollowWatcher(fromExit: true);

        Shutdown(0);
    }

    private void ReleaseSingleInstanceMutex()
    {
        if (_ownsMutex)
        {
            try { _instanceMutex?.ReleaseMutex(); } catch { }
            _ownsMutex = false;
        }
        _instanceMutex?.Dispose();
        _instanceMutex = null;
    }

    protected override void OnExit(ExitEventArgs e)
    {
        _wakeCts?.Cancel();
        _wakeCts?.Dispose();
        _wakeEvent?.Dispose();
        _wakeAgEvent?.Dispose();
        _wakeGeminiEvent?.Dispose();
        _closeEvent?.Dispose();
        ReleaseSingleInstanceMutex();
        base.OnExit(e);
    }
}
