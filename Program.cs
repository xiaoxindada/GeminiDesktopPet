using System.Diagnostics;
using System.IO;
using GeminiDesktopPet.Services;

namespace GeminiDesktopPet;

internal static class Program
{
    [STAThread]
    private static int Main(string[] args)
    {
        if (args.Length >= 1
            && string.Equals(args[0], "--test-follow-startup", StringComparison.OrdinalIgnoreCase))
        {
            string? reportPath = args.Length >= 2 ? args[1] : null;
            return FollowStartupSelfTest.RunAll(reportPath);
        }

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
            var svc = new SettingsService();
            var cfg = svc.Load();
            bool followEnabled = AppFollowService.AnyFollowEnabled(cfg);
            if (!followEnabled)
                AppFollowService.SignalStopFollowWatcher();

            AppFollowService.TrySignalNamedEvent(AppFollowService.CloseEventName);

            for (int i = 0; i < 30; i++)
            {
                if (!AppFollowService.IsMainInstanceRunning())
                    break;
                Thread.Sleep(50);
            }

            if (followEnabled && !AppFollowService.IsFollowWatcherRunning())
                AppFollowService.SpawnFollowWatcher(fromExit: true);

            return 0;
        }

        if (args.Any(a => string.Equals(a, "--follow-watcher", StringComparison.OrdinalIgnoreCase)
                       || string.Equals(a, "--follow-antigravity", StringComparison.OrdinalIgnoreCase)))
        {
            bool fromExit = args.Any(a => string.Equals(a, "--from-exit", StringComparison.OrdinalIgnoreCase));
            return AppFollowService.RunFollowWatcher(fromExit);
        }

        var app = new App();
        app.InitializeComponent();
        return app.Run();
    }
}
