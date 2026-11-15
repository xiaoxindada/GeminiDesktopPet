using System.Diagnostics;
using System.IO;
using GeminiDesktopPet.Services;

namespace GeminiDesktopPet;

internal static class Program
{
    [STAThread]
    private static int Main(string[] args)
    {
        if (args.Length >= 3
            && string.Equals(args[0], "--apply-update", StringComparison.OrdinalIgnoreCase)
            && int.TryParse(args[1], out int parentPid))
        {
            var svc = new SettingsService();
            var cfg = svc.Load();
            AppLogger.Initialize(cfg.EnableRuntimeLog, args[2]);
            AppFollowService.SignalStopFollowWatcher();
            UpdateService.ApplyStagedUpdateAndRelaunch(parentPid, args[2]);
            return 0;
        }

        if (args.Any(a => string.Equals(a, "--close-pet", StringComparison.OrdinalIgnoreCase)))
        {
            bool closedMainGui = AppFollowService.TrySignalNamedEvent(AppFollowService.CloseEventName);
            if (!closedMainGui)
            {
                var svc = new SettingsService();
                var cfg = svc.Load();
                if (AppFollowService.AnyFollowEnabled(cfg))
                    AppFollowService.SpawnFollowWatcher(fromExit: true);
                else
                    AppFollowService.SignalStopFollowWatcher();
            }
            return 0;
        }

        if (args.Any(a => string.Equals(a, "--enable-follow", StringComparison.OrdinalIgnoreCase)
                       || string.Equals(a, "--follow-flag:on", StringComparison.OrdinalIgnoreCase)))
        {
            return HandleEnableFollowCommand();
        }

        if (args.Any(a => string.Equals(a, "--disable-follow", StringComparison.OrdinalIgnoreCase)
                       || string.Equals(a, "--follow-flag:off", StringComparison.OrdinalIgnoreCase)))
        {
            var svc = new SettingsService();
            var cfg = svc.Load();
            cfg.FollowAntigravity = false;
            cfg.FollowGeminiApp = false;
            svc.Save(cfg);
            new AppFollowService(svc).DisableFollow();
            AppFollowService.TrySignalNamedEvent(AppFollowService.ReloadFollowEventName);
            return 0;
        }

        if (args.Any(a => string.Equals(a, "--follow-watcher", StringComparison.OrdinalIgnoreCase)))
        {
            bool fromExit = args.Any(a => string.Equals(a, "--from-exit", StringComparison.OrdinalIgnoreCase));
            return AppFollowService.RunFollowWatcher(fromExit);
        }

        var app = new App();
        app.InitializeComponent();
        return app.Run();
    }

    private static int HandleEnableFollowCommand()
    {
        var svc = new SettingsService();
        var cfg = svc.Load();
        if (!cfg.FollowAntigravity && !cfg.FollowGeminiApp)
        {
            cfg.FollowAntigravity = true;
            cfg.FollowGeminiApp = true;
            svc.Save(cfg);
        }

        var followSvc = new AppFollowService(svc);
        followSvc.SyncAndApply(cfg);

        bool signaledRunning = AppFollowService.TrySignalNamedEvent(AppFollowService.ReloadFollowEventName);
        if (!signaledRunning && !string.IsNullOrWhiteSpace(Environment.ProcessPath))
        {
            try
            {
                AppFollowService.SignalStopFollowWatcher();
                string exePath = Environment.ProcessPath!;
                Process.Start(new ProcessStartInfo(exePath, "--follow-antigravity")
                {
                    WorkingDirectory = Path.GetDirectoryName(exePath) ?? AppContext.BaseDirectory,
                    UseShellExecute = true
                });
            }
            catch
            {
                return 2;
            }
        }

        return 0;
    }
}
