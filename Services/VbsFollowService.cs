using System.Diagnostics;
using System.IO;
using GeminiDesktopPet.Models;

namespace GeminiDesktopPet.Services;

/// <summary>
/// Manages dynamic shell:startup VBScript follow watcher generation and teardown
/// via Scripts\EnableFollow.bat and Scripts\DisableFollow.bat.
/// </summary>
public sealed class VbsFollowService
{
    private readonly SettingsService _settingsService;
    private readonly string _petPathFilePath;
    private readonly string _startBatFilePath;
    private readonly string _stopBatFilePath;
    private readonly string _tokenFilePath;
    private readonly string _lockFilePath;
    private readonly string _wakeReasonFilePath;

    public string StartupFolderPath { get; }
    public string WatcherScriptPath { get; }
    public string StopScriptPath => ResolveBundledScript("DisableFollow.bat") ?? string.Empty;
    public string? ProjectRootDirectory { get; }

    public VbsFollowService(SettingsService settingsService)
    {
        _settingsService = settingsService;
        string appDataFolder = settingsService.SettingsFolder;
        Directory.CreateDirectory(appDataFolder);

        StartupFolderPath = Environment.GetFolderPath(Environment.SpecialFolder.Startup);
        if (string.IsNullOrWhiteSpace(StartupFolderPath))
        {
            StartupFolderPath = Path.Combine(
                Environment.GetFolderPath(Environment.SpecialFolder.ApplicationData),
                "Microsoft", "Windows", "Start Menu", "Programs", "Startup");
        }
        Directory.CreateDirectory(StartupFolderPath);

        WatcherScriptPath = Path.Combine(StartupFolderPath, "GeminiDesktopPet_FollowWatcher.vbs");
        _petPathFilePath = Path.Combine(appDataFolder, "pet_path.txt");
        _startBatFilePath = Path.Combine(appDataFolder, "start_bat_path.txt");
        _stopBatFilePath = Path.Combine(appDataFolder, "stop_bat_path.txt");
        _tokenFilePath = Path.Combine(appDataFolder, "watcher_token.txt");
        _lockFilePath = Path.Combine(appDataFolder, "watcher.lock");
        _wakeReasonFilePath = Path.Combine(appDataFolder, "wake_reason.txt");
        ProjectRootDirectory = DetectProjectRoot();
    }

    public static bool AnyFollowEnabled(PetSettings settings)
        => settings.FollowAntigravity || settings.FollowGeminiApp;

    public bool IsStartupScriptPresent()
        => File.Exists(WatcherScriptPath);

    public bool IsWatcherRunning()
    {
        if (!File.Exists(_lockFilePath))
            return false;

        try
        {
            using var fs = new FileStream(_lockFilePath, FileMode.Open, FileAccess.ReadWrite, FileShare.None);
            return false;
        }
        catch (IOException)
        {
            return true;
        }
        catch
        {
            return false;
        }
    }

    public string? ConsumeWakeReason()
    {
        try
        {
            if (!File.Exists(_wakeReasonFilePath))
                return null;

            string text = File.ReadAllText(_wakeReasonFilePath).Trim();
            File.Delete(_wakeReasonFilePath);
            return string.IsNullOrWhiteSpace(text) ? null : text;
        }
        catch
        {
            return null;
        }
    }

    public void WriteWakeReason(string reason)
    {
        try
        {
            if (!string.IsNullOrWhiteSpace(reason))
                File.WriteAllText(_wakeReasonFilePath, reason.Trim());
        }
        catch
        {
        }
    }

    public void EnsureScriptsGenerated(PetSettings settings)
    {
        string? exePath = Environment.ProcessPath;
        if (!string.IsNullOrWhiteSpace(exePath) && File.Exists(exePath))
        {
            try { File.WriteAllText(_petPathFilePath, exePath + Environment.NewLine); }
            catch { }
        }

        string? startBat = ResolveBundledScript("StartPet.bat");
        if (!string.IsNullOrWhiteSpace(startBat))
        {
            try { File.WriteAllText(_startBatFilePath, startBat + Environment.NewLine); }
            catch { }
        }

        string? stopBat = ResolveBundledScript("StopPet.bat");
        if (!string.IsNullOrWhiteSpace(stopBat))
        {
            try { File.WriteAllText(_stopBatFilePath, stopBat + Environment.NewLine); }
            catch { }
        }

        if (AnyFollowEnabled(settings) && !File.Exists(WatcherScriptPath))
        {
            string? enableBat = ResolveBundledScript("EnableFollow.bat");
            RunBatchFile(enableBat, "/from-app /no-launch", waitMs: 3000);
        }
    }

    public void SyncAndApply(PetSettings settings, bool forceRestart = false)
    {
        bool anyEnabled = AnyFollowEnabled(settings);

        // Migrate off legacy HKCU Run registry entries in favor of dynamic shell:startup VBS
        _settingsService.SetFollowWatcherRun(false, null);

        if (anyEnabled)
        {
            EnsureScriptsGenerated(settings);
            if (forceRestart || !File.Exists(WatcherScriptPath) || !IsWatcherRunning())
            {
                StartWatcherProcess();
            }
        }
        else
        {
            StopWatcher();
        }
    }

    public bool StartWatcherProcess()
    {
        try
        {
            string? enableBat = ResolveBundledScript("EnableFollow.bat");
            if (string.IsNullOrWhiteSpace(enableBat) || !File.Exists(enableBat))
                return false;

            try
            {
                if (File.Exists(_tokenFilePath) &&
                    File.ReadAllText(_tokenFilePath).Trim().Equals("STOP", StringComparison.OrdinalIgnoreCase))
                {
                    File.Delete(_tokenFilePath);
                }
            }
            catch
            {
            }

            RunBatchFile(enableBat, "/from-app", waitMs: 3500);

            // Give wscript.exe a brief moment to acquire watcher.lock for UI badge feedback
            for (int i = 0; i < 8; i++)
            {
                if (File.Exists(WatcherScriptPath) && IsWatcherRunning())
                    return true;
                Thread.Sleep(100);
            }

            return File.Exists(WatcherScriptPath);
        }
        catch
        {
            return false;
        }
    }

    public void StopWatcher()
    {
        try
        {
            try { File.WriteAllText(_tokenFilePath, "STOP"); }
            catch { }

            string? disableBat = ResolveBundledScript("DisableFollow.bat");
            if (!string.IsNullOrWhiteSpace(disableBat) && File.Exists(disableBat))
            {
                RunBatchFile(disableBat, "/from-app", waitMs: 4000);
            }

            if (File.Exists(WatcherScriptPath))
            {
                try { File.Delete(WatcherScriptPath); }
                catch { }
            }
        }
        catch
        {
        }
    }

    private static void RunBatchFile(string? batPath, string arguments, int waitMs)
    {
        if (string.IsNullOrWhiteSpace(batPath) || !File.Exists(batPath))
            return;

        using var proc = Process.Start(new ProcessStartInfo
        {
            FileName = "cmd.exe",
            Arguments = $"/d /c \"\"{batPath}\" {arguments}\"",
            WorkingDirectory = Path.GetDirectoryName(batPath) ?? Environment.CurrentDirectory,
            UseShellExecute = false,
            CreateNoWindow = true,
            WindowStyle = ProcessWindowStyle.Hidden
        });

        proc?.WaitForExit(waitMs);
    }

    private string? ResolveBundledScript(string fileName)
    {
        string baseDir = AppContext.BaseDirectory;
        string[] candidates =
        [
            Path.Combine(baseDir, "Scripts", fileName),
            Path.Combine(baseDir, fileName),
            ProjectRootDirectory is not null ? Path.Combine(ProjectRootDirectory, "Scripts", fileName) : string.Empty
        ];

        foreach (string c in candidates)
        {
            if (!string.IsNullOrWhiteSpace(c) && File.Exists(c))
                return c;
        }
        return null;
    }

    private static string? DetectProjectRoot()
    {
        try
        {
            var dir = new DirectoryInfo(AppContext.BaseDirectory);
            for (int i = 0; i < 6 && dir != null; i++)
            {
                if (File.Exists(Path.Combine(dir.FullName, "GeminiDesktopPet.csproj")) ||
                    File.Exists(Path.Combine(dir.FullName, "GeminiDesktopPet.slnx")))
                {
                    return dir.FullName;
                }
                dir = dir.Parent;
            }
        }
        catch
        {
        }
        return null;
    }
}
