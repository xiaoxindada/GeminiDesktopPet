namespace GeminiDesktopPet.Models;

public enum DockSide
{
    None,
    Left,
    Right
}

public enum PetStyleMode
{
    Chibi,
    Standard
}

/// <summary>
/// Persists user configuration and bond progression for Gemini-chan.
/// </summary>
public sealed class PetSettings
{
    public PetStyleMode StyleMode { get; set; } = PetStyleMode.Chibi;
    public double Scale { get; set; } = 1.0;
    public double Opacity { get; set; } = 1.0;
    public bool AlwaysOnTop { get; set; } = true;
    public bool AutoWalk { get; set; } = true;
    public bool AntiGravityMode { get; set; } = false;
    public bool GravityEnabled { get; set; } = true;
    public bool EdgeDockEnabled { get; set; } = true;
    public bool SoundEnabled { get; set; } = true;
    public bool ParticlesEnabled { get; set; } = true;
    public bool ClickThrough { get; set; } = false;
    public bool SystemMonitorEnabled { get; set; } = true;
    public bool ShowSpeechBubble { get; set; } = false;
    public bool AutoFestivalCostume { get; set; } = true;
    public bool FollowAntigravity { get; set; } = false;
    public bool FollowGeminiApp { get; set; } = false;
    public bool SleepWhenAntigravityExits { get; set; } = false;

    /// <summary>
    /// Optional manual costume/theme override (e.g. "auto", "default", "fest_christmas", etc.).
    /// </summary>
    public string CostumeOverride { get; set; } = "auto";

    public double? LastLeft { get; set; }
    public double? LastTop { get; set; }
    public DockSide DockedSide { get; set; } = DockSide.None;

    public int TotalPats { get; set; } = 0;
    public int Favorability { get; set; } = 20;

    /// <summary>
    /// Optional user-provided Gemini API key for live AI chat inside the speech bubble.
    /// </summary>
    public string GeminiApiKey { get; set; } = string.Empty;
    public string GeminiModel { get; set; } = "gemini-2.5-flash";

    /// <summary>
    /// Upstream GitHub Releases tracking source (always resolves latest release).
    /// </summary>
    public string UpdateReleasesUrl { get; set; } = "https://github.com/xiaoxindada/GeminiDesktopPet/releases";
    public bool AutoCheckUpdates { get; set; } = true;
    public string SkippedUpdateVersion { get; set; } = string.Empty;
}
