using System.Diagnostics;
using System.Windows.Threading;

namespace GeminiDesktopPet.Services;

public readonly record struct ProcessProbeInfo(
    int Pid,
    bool HasMainWindow,
    int ThreadCount);

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

    // Orphaned headless Node/MCP worker processes (e.g. Antigravity.exe --import=...) keep ~10-12 threads
    // and MainWindowHandle == 0 after the IDE closes. Active Electron app instances have a main window
    // or >= 22 Chromium threads, and the primary Electron browser process has >= 38 threads.
    public const int MinActiveElectronThreadCount = 22;
    public const int MinPrimaryBrowserThreadCount = 38;

    private readonly DispatcherTimer _timer;
    private bool _wasAgRunning;
    private bool _wasGeminiRunning;
    private HashSet<int> _knownAgPids = [];
    private HashSet<int> _knownGeminiPids = [];
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
        _knownAgPids = new HashSet<int>(ag.ActivePids);
        _knownGeminiPids = new HashSet<int>(gem.ActivePids);
        _lastAgStartUtc = ag.IsRunning ? DateTime.UtcNow : DateTime.MinValue;
        _lastGeminiStartUtc = gem.IsRunning ? DateTime.UtcNow : DateTime.MinValue;
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

    public static string? DetectRunningFollowedAppKey(
        Models.PetSettings settings,
        Func<AppActivitySnapshot>? sampleAg = null,
        Func<AppActivitySnapshot>? sampleGemini = null)
    {
        if (settings.FollowAntigravity && (sampleAg?.Invoke() ?? SampleAntigravityActivity()).IsRunning)
            return "antigravity";
        if (settings.FollowGeminiApp && (sampleGemini?.Invoke() ?? SampleGeminiActivity()).IsRunning)
            return "gemini";
        return null;
    }

    public static bool CheckAnyFollowedTargetRunning(Models.PetSettings settings)
        => DetectRunningFollowedAppKey(settings) != null;

    /// <summary>
    /// Classifies a candidate process based on its main window handle and thread count,
    /// filtering out low-thread orphaned headless Node workers.
    /// </summary>
    public static bool ClassifyProcessCandidate(bool hasMainWindow, int threadCount, out bool isPrimary)
    {
        bool isActive = hasMainWindow || threadCount >= MinActiveElectronThreadCount;
        isPrimary = isActive && (hasMainWindow || threadCount >= MinPrimaryBrowserThreadCount);
        return isActive;
    }

    public static AppActivitySnapshot BuildSnapshotFromProbes(IEnumerable<ProcessProbeInfo> probes)
    {
        var activePids = new HashSet<int>();
        var primaryPids = new HashSet<int>();
        foreach (var probe in probes)
        {
            if (ClassifyProcessCandidate(probe.HasMainWindow, probe.ThreadCount, out bool isPrimary))
            {
                activePids.Add(probe.Pid);
                if (isPrimary)
                    primaryPids.Add(probe.Pid);
            }
        }
        return new AppActivitySnapshot(activePids.Count > 0, activePids, primaryPids);
    }

    public static AppActivitySnapshot SampleAppActivity(string processName)
    {
        var probes = new List<ProcessProbeInfo>();
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
                    probes.Add(new ProcessProbeInfo(p.Id, hasWindow, threadCount));
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

        return BuildSnapshotFromProbes(probes);
    }

    /// <summary>
    /// Evaluates start/exit state transitions while preventing duplicate triggers during
    /// multi-stage Electron startup or child GPU/renderer thread spikes.
    /// </summary>
    public static void EvaluateTransition(
        AppActivitySnapshot current,
        ref bool wasRunning,
        ref HashSet<int> knownPids,
        ref DateTime lastStartUtc,
        out bool started,
        out bool exited,
        DateTime? nowUtc = null)
    {
        DateTime now = nowUtc ?? DateTime.UtcNow;
        knownPids.IntersectWith(current.ActivePids);

        bool hasKnownPrimaryAlive = current.PrimaryPids.Overlaps(knownPids);
        bool hasBrandNewPrimary = false;
        foreach (int pid in current.PrimaryPids)
        {
            if (!knownPids.Contains(pid))
            {
                hasBrandNewPrimary = true;
                break;
            }
        }

        bool cooldownElapsed = (now - lastStartUtc).TotalSeconds >= 6.0;
        started = (current.IsRunning && !wasRunning)
            || (hasBrandNewPrimary && (!hasKnownPrimaryAlive || cooldownElapsed));
        exited = !current.IsRunning && wasRunning;

        if (started)
        {
            lastStartUtc = now;
            knownPids = new HashSet<int>(current.ActivePids);
        }
        else if (current.PrimaryPids.Count > 0)
        {
            knownPids.UnionWith(current.ActivePids);
        }
        else if (!current.IsRunning)
        {
            knownPids.Clear();
        }

        wasRunning = current.IsRunning;
    }

    private void Poll()
    {
        var ag = SampleAntigravityActivity();
        var gem = SampleGeminiActivity();

        if (!_initialized)
        {
            _wasAgRunning = ag.IsRunning;
            _wasGeminiRunning = gem.IsRunning;
            _knownAgPids = new HashSet<int>(ag.ActivePids);
            _knownGeminiPids = new HashSet<int>(gem.ActivePids);
            _lastAgStartUtc = ag.IsRunning ? DateTime.UtcNow : DateTime.MinValue;
            _lastGeminiStartUtc = gem.IsRunning ? DateTime.UtcNow : DateTime.MinValue;
            _initialized = true;
            return;
        }

        EvaluateTransition(ag, ref _wasAgRunning, ref _knownAgPids, ref _lastAgStartUtc, out bool agStarted, out bool agExited);
        if (agStarted) AntigravityStarted?.Invoke();
        else if (agExited) AntigravityExited?.Invoke();

        EvaluateTransition(gem, ref _wasGeminiRunning, ref _knownGeminiPids, ref _lastGeminiStartUtc, out bool gemStarted, out bool gemExited);
        if (gemStarted) GeminiAppStarted?.Invoke();
        else if (gemExited) GeminiAppExited?.Invoke();
    }

    public void Dispose() => _timer.Stop();
}
