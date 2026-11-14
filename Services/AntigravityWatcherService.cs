using System.Diagnostics;
using System.Windows.Threading;

namespace GeminiDesktopPet.Services;

/// <summary>
/// Lightweight monitor that detects when Antigravity or Gemini Desktop App starts or closes on Windows.
/// </summary>
public sealed class AntigravityWatcherService : IDisposable
{
    private const string AntigravityProcessName = "Antigravity";
    private const string GeminiProcessName = "Gemini";

    private readonly DispatcherTimer _timer;
    private bool _wasAgRunning;
    private bool _wasGeminiRunning;
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
        _wasAgRunning = CheckProcessRunning();
        _wasGeminiRunning = CheckGeminiAppRunning();
        _initialized = true;
        _timer.Start();
    }

    public void Stop() => _timer.Stop();

    public static bool CheckProcessRunning() => IsNamedProcessRunning(AntigravityProcessName);

    public static bool CheckGeminiAppRunning() => IsNamedProcessRunning(GeminiProcessName);

    public static bool CheckAnyFollowedTargetRunning(Models.PetSettings settings)
        => (settings.FollowAntigravity && CheckProcessRunning())
        || (settings.FollowGeminiApp && CheckGeminiAppRunning());

    private static bool IsNamedProcessRunning(string processName)
    {
        Process[]? procs = null;
        try
        {
            procs = Process.GetProcessesByName(processName);
            return procs.Length > 0;
        }
        catch
        {
            return false;
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
    }

    private void Poll()
    {
        bool agRunning = CheckProcessRunning();
        bool gemRunning = CheckGeminiAppRunning();

        if (!_initialized)
        {
            _wasAgRunning = agRunning;
            _wasGeminiRunning = gemRunning;
            _initialized = true;
            return;
        }

        if (agRunning != _wasAgRunning)
        {
            _wasAgRunning = agRunning;
            if (agRunning) AntigravityStarted?.Invoke();
            else AntigravityExited?.Invoke();
        }

        if (gemRunning != _wasGeminiRunning)
        {
            _wasGeminiRunning = gemRunning;
            if (gemRunning) GeminiAppStarted?.Invoke();
            else GeminiAppExited?.Invoke();
        }
    }

    public void Dispose() => _timer.Stop();
}
