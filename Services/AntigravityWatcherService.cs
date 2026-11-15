using System.Diagnostics;
using System.Windows.Threading;

namespace GeminiDesktopPet.Services;

public readonly record struct AppActivitySnapshot(
    bool IsRunning,
    HashSet<int> ActivePids,
    HashSet<int> PrimaryPids);

/// <summary>
/// Lightweight monitor that detects when Antigravity or Gemini Desktop App starts or closes on Windows.
/// </summary>
public sealed class AntigravityWatcherService : IDisposable
{
    private const string AntigravityProcessName = "Antigravity";
    private const string GeminiProcessName = "Gemini";

    // Orphaned headless Node/MCP worker processes (e.g. Antigravity.exe --import=...) keep ~10-11 threads
    // and MainWindowHandle == 0 after the IDE closes. Active Electron app instances have a main window
    // or >= 22 Chromium threads, and the primary Electron browser process has >= 38 threads.
    private const int MinActiveElectronThreadCount = 22;
    private const int MinPrimaryBrowserThreadCount = 38;

    private readonly DispatcherTimer _timer;
    private bool _wasAgRunning;
    private bool _wasGeminiRunning;
    private HashSet<int> _knownAgActivePids = [];
    private HashSet<int> _knownGeminiActivePids = [];
    private DateTime _lastAgStartUtc = DateTime.MinValue;
    private DateTime _lastGeminiStartUtc = DateTime.MinValue;
    private bool _initialized;

    public bool IsRunning => _wasAgRunning;
    public bool IsAntigravityRunning => _wasAgRunning;
    public bool IsGeminiAppRunning => _wasGeminiRunning;

    public event Action? AntigravityStarted;
    public event Action? AntigravityExited;
    public event Action? GeminiAppStarted;
    public event Action? GeminiAppExited;

    public AntigravityWatcherService(TimeSpan? pollInterval = null)
    {
        _timer = new DispatcherTimer
        {
            Interval = pollInterval ?? TimeSpan.FromSeconds(1.5)
        };
        _timer.Tick += (_, _) => Poll();
    }

    public void Start()
    {
        var ag = SampleAntigravityActivity();
        var gem = SampleGeminiActivity();
        _wasAgRunning = ag.IsRunning;
        _wasGeminiRunning = gem.IsRunning;
        _knownAgActivePids = ag.ActivePids;
        _knownGeminiActivePids = gem.ActivePids;
        _initialized = true;
        _timer.Start();
    }

    public void Stop() => _timer.Stop();

    public static AppActivitySnapshot SampleAntigravityActivity()
        => SampleAppActivity(AntigravityProcessName);

    public static AppActivitySnapshot SampleGeminiActivity()
        => SampleAppActivity(GeminiProcessName);

    public static bool CheckProcessRunning() => SampleAntigravityActivity().IsRunning;

    public static bool CheckGeminiAppRunning() => SampleGeminiActivity().IsRunning;

    public static string? DetectRunningFollowedAppKey(Models.PetSettings settings)
    {
        if (settings.FollowAntigravity && CheckProcessRunning())
            return "antigravity";
        if (settings.FollowGeminiApp && CheckGeminiAppRunning())
            return "gemini";
        return null;
    }

    public static bool CheckAnyFollowedTargetRunning(Models.PetSettings settings)
        => DetectRunningFollowedAppKey(settings) != null;

    public static AppActivitySnapshot SampleAppActivity(string processName)
    {
        var activePids = new HashSet<int>();
        var primaryPids = new HashSet<int>();
        Process[]? procs = null;
        try
        {
            procs = Process.GetProcessesByName(processName);
            foreach (var p in procs)
            {
                try
                {
                    bool hasWindow = p.MainWindowHandle != IntPtr.Zero;
                    int threadCount = p.Threads.Count;
                    if (hasWindow || threadCount >= MinActiveElectronThreadCount)
                    {
                        activePids.Add(p.Id);
                        if (hasWindow || threadCount >= MinPrimaryBrowserThreadCount)
                            primaryPids.Add(p.Id);
                    }
                }
                catch
                {
                }
            }
        }
        catch
        {
        }
        finally
        {
            if (procs != null)
            {
                foreach (var p in procs)
                {
                    try { p.Dispose(); } catch { }
                }
            }
        }

        return new AppActivitySnapshot(activePids.Count > 0, activePids, primaryPids);
    }

    public static void EvaluateTransition(
        AppActivitySnapshot current,
        ref bool wasRunning,
        ref HashSet<int> knownActivePids,
        ref DateTime lastStartUtc,
        out bool started,
        out bool exited)
    {
        DateTime now = DateTime.UtcNow;
        bool hasBrandNewPrimary = false;
        foreach (int pid in current.PrimaryPids)
        {
            if (!knownActivePids.Contains(pid))
            {
                hasBrandNewPrimary = true;
                break;
            }
        }

        bool cooldownElapsed = (now - lastStartUtc).TotalSeconds >= 4.0;
        started = (current.IsRunning && !wasRunning) || (hasBrandNewPrimary && cooldownElapsed);
        exited = !current.IsRunning && wasRunning;

        if (started)
            lastStartUtc = now;

        wasRunning = current.IsRunning;
        knownActivePids = current.ActivePids;
    }

    private void Poll()
    {
        var ag = SampleAntigravityActivity();
        var gem = SampleGeminiActivity();

        if (!_initialized)
        {
            _wasAgRunning = ag.IsRunning;
            _wasGeminiRunning = gem.IsRunning;
            _knownAgActivePids = ag.ActivePids;
            _knownGeminiActivePids = gem.ActivePids;
            _initialized = true;
            return;
        }

        EvaluateTransition(ag, ref _wasAgRunning, ref _knownAgActivePids, ref _lastAgStartUtc, out bool agStarted, out bool agExited);
        if (agStarted) AntigravityStarted?.Invoke();
        else if (agExited) AntigravityExited?.Invoke();

        EvaluateTransition(gem, ref _wasGeminiRunning, ref _knownGeminiActivePids, ref _lastGeminiStartUtc, out bool gemStarted, out bool gemExited);
        if (gemStarted) GeminiAppStarted?.Invoke();
        else if (gemExited) GeminiAppExited?.Invoke();
    }

    public void Dispose() => _timer.Stop();
}
