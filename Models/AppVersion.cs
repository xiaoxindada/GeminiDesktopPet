using System.IO;
using System.Reflection;

namespace GeminiDesktopPet.Models;

/// <summary>
/// Resolves and compares semantic application versions independently of solution/project files.
/// </summary>
public static class AppVersion
{
    public const string FallbackVersion = "0.1.0";

    public static string Current { get; } = ResolveCurrentVersion();

    public static string CurrentDisplay => FormatTag(Current);

    public static string Normalize(string? raw)
    {
        if (string.IsNullOrWhiteSpace(raw))
            return FallbackVersion;

        string trimmed = raw.Trim();
        if (trimmed.StartsWith('v') || trimmed.StartsWith('V'))
            trimmed = trimmed[1..].Trim();

        int plusIdx = trimmed.IndexOf('+');
        if (plusIdx >= 0)
            trimmed = trimmed[..plusIdx].Trim();

        return string.IsNullOrWhiteSpace(trimmed) ? FallbackVersion : trimmed;
    }

    public static string FormatTag(string? raw) => "v" + Normalize(raw);

    public static bool IsNewerVersion(string? candidate, string? baseline = null)
        => Compare(candidate, baseline ?? Current) > 0;

    /// <summary>
    /// Compares two semantic version strings (ignoring leading 'v' and '+build' metadata).
    /// Returns &gt; 0 when left &gt; right, &lt; 0 when left &lt; right, 0 when equal.
    /// </summary>
    public static int Compare(string? left, string? right)
    {
        var (lNums, lPre) = Parse(left);
        var (rNums, rPre) = Parse(right);

        int count = Math.Max(lNums.Length, rNums.Length);
        for (int i = 0; i < count; i++)
        {
            int lv = i < lNums.Length ? lNums[i] : 0;
            int rv = i < rNums.Length ? rNums[i] : 0;
            if (lv != rv)
                return lv.CompareTo(rv);
        }

        bool lHasPre = !string.IsNullOrEmpty(lPre);
        bool rHasPre = !string.IsNullOrEmpty(rPre);
        if (!lHasPre && !rHasPre) return 0;
        if (!lHasPre) return 1;  // stable > prerelease
        if (!rHasPre) return -1;

        return ComparePrerelease(lPre, rPre);
    }

    private static (int[] Numbers, string Prerelease) Parse(string? raw)
    {
        string norm = Normalize(raw);
        int dashIdx = norm.IndexOf('-');
        string core = dashIdx >= 0 ? norm[..dashIdx] : norm;
        string pre = dashIdx >= 0 ? norm[(dashIdx + 1)..].Trim() : string.Empty;

        string[] parts = core.Split('.', StringSplitOptions.RemoveEmptyEntries);
        int[] nums = new int[Math.Max(parts.Length, 3)];
        for (int i = 0; i < parts.Length; i++)
        {
            if (int.TryParse(parts[i], out int val))
                nums[i] = val;
        }
        return (nums, pre);
    }

    private static int ComparePrerelease(string left, string right)
    {
        string[] lParts = left.Split('.', StringSplitOptions.RemoveEmptyEntries);
        string[] rParts = right.Split('.', StringSplitOptions.RemoveEmptyEntries);
        int min = Math.Min(lParts.Length, rParts.Length);
        for (int i = 0; i < min; i++)
        {
            bool lNum = int.TryParse(lParts[i], out int li);
            bool rNum = int.TryParse(rParts[i], out int ri);
            int cmp = (lNum, rNum) switch
            {
                (true, true) => li.CompareTo(ri),
                (true, false) => -1,
                (false, true) => 1,
                _ => string.Compare(lParts[i], rParts[i], StringComparison.OrdinalIgnoreCase)
            };
            if (cmp != 0) return cmp;
        }
        return lParts.Length.CompareTo(rParts.Length);
    }

    private static string ResolveCurrentVersion()
    {
        var infoAttr = typeof(AppVersion).Assembly.GetCustomAttribute<AssemblyInformationalVersionAttribute>();
        if (!string.IsNullOrWhiteSpace(infoAttr?.InformationalVersion))
            return Normalize(infoAttr.InformationalVersion);

        string versionFile = Path.Combine(AppContext.BaseDirectory, "VERSION");
        if (File.Exists(versionFile))
        {
            try
            {
                string fileVer = File.ReadAllText(versionFile).Trim();
                if (!string.IsNullOrWhiteSpace(fileVer))
                    return Normalize(fileVer);
            }
            catch
            {
                // Fall back to assembly/default version.
            }
        }

        var asmVer = typeof(AppVersion).Assembly.GetName().Version;
        if (asmVer != null && (asmVer.Major > 0 || asmVer.Minor > 0 || asmVer.Build > 0))
            return $"{asmVer.Major}.{asmVer.Minor}.{Math.Max(0, asmVer.Build)}";

        return FallbackVersion;
    }
}
