using System.IO;
using System.Runtime.InteropServices;
using System.Text;
using GeminiDesktopPet.Models;

namespace GeminiDesktopPet.Services;

/// <summary>
/// Thread-safe daily runtime logger disabled by default; writes standard structured entries to Logs\yyyy-MM-dd.log when enabled.
/// </summary>
public static class AppLogger
{
    private static readonly object Sync = new();
    private static readonly UTF8Encoding Utf8NoBom = new(false);

    private static string _logDir = Path.GetFullPath(Path.Combine(AppContext.BaseDirectory, "Logs"));
    private static volatile bool _enabled;

    public static bool IsEnabled => _enabled;
    public static string LogFolderPath => _logDir;

    public static string GetDailyLogFilePath(DateTime? localNow = null)
    {
        DateTime ts = localNow ?? DateTime.Now;
        return Path.Combine(_logDir, $"{ts:yyyy-MM-dd}.log");
    }

    public static void Initialize(bool enabled, string? baseFolderOverride = null)
    {
        lock (Sync)
        {
            string baseDir = string.IsNullOrWhiteSpace(baseFolderOverride)
                ? AppContext.BaseDirectory
                : baseFolderOverride;
            _logDir = Path.GetFullPath(Path.Combine(baseDir, "Logs"));

            _enabled = enabled;
            if (_enabled)
            {
                WriteEntryLocked("INFO", "App", BuildSessionBanner("会话日志已启动"));
            }
        }
    }

    public static void SetEnabled(bool enabled)
    {
        lock (Sync)
        {
            if (_enabled == enabled)
                return;

            if (enabled)
            {
                _enabled = true;
                WriteEntryLocked("INFO", "App", BuildSessionBanner("用户开启运行日志"));
            }
            else
            {
                WriteEntryLocked("INFO", "App", "=== 用户关闭运行日志 ===");
                _enabled = false;
            }
        }
    }

    public static void Debug(string category, string message) => Write("DEBUG", category, message);
    public static void Info(string category, string message) => Write("INFO", category, message);
    public static void Warn(string category, string message) => Write("WARN", category, message);
    public static void Error(string category, string message, Exception? ex = null) => Write("ERROR", category, message, ex);

    private static void Write(string level, string category, string message, Exception? ex = null)
    {
        if (!_enabled) return;
        lock (Sync)
        {
            if (_enabled)
                WriteEntryLocked(level, category, message, ex);
        }
    }

    private static string BuildSessionBanner(string action)
    {
        return $"=== {action} | App=GeminiDesktopPet v{AppVersion.Current} | OS={Environment.OSVersion.VersionString} | Arch={RuntimeInformation.OSArchitecture} | Runtime={RuntimeInformation.FrameworkDescription} ===";
    }

    private static string[] SplitLines(string text)
    {
        return text.Replace("\r\n", "\n").Replace('\r', '\n').Split('\n');
    }

    private static void WriteEntryLocked(string level, string category, string message, Exception? ex = null)
    {
        try
        {
            Directory.CreateDirectory(_logDir);
            DateTimeOffset now = DateTimeOffset.Now;
            string filePath = GetDailyLogFilePath(now.LocalDateTime);
            string normalizedCategory = string.IsNullOrWhiteSpace(category) ? "General" : category.Trim();
            string[] messageLines = SplitLines(message ?? string.Empty);

            var sb = new StringBuilder(256);
            sb.Append(now.ToString("yyyy-MM-dd HH:mm:ss.fff zzz"));
            sb.Append(" [").Append(level).Append(']');
            sb.Append(" [PID:").Append(Environment.ProcessId)
              .Append(" TID:").Append(Environment.CurrentManagedThreadId).Append(']');
            sb.Append(" [").Append(normalizedCategory).Append("] ");
            sb.AppendLine(messageLines[0]);

            for (int i = 1; i < messageLines.Length; i++)
            {
                sb.Append("    ").AppendLine(messageLines[i]);
            }

            if (ex != null)
            {
                foreach (string line in SplitLines(ex.ToString()))
                {
                    sb.Append("    ").AppendLine(line);
                }
            }

            File.AppendAllText(filePath, sb.ToString(), Utf8NoBom);
        }
        catch
        {
            // Logging must never throw or interrupt app behavior.
        }
    }
}
