namespace GeminiDesktopPet.Models;

public sealed class CharacterTrait
{
    public string Title { get; set; } = string.Empty;
    public string Detail { get; set; } = string.Empty;

    public CharacterTrait()
    {
    }

    public CharacterTrait(string title, string detail)
    {
        Title = title;
        Detail = detail;
    }
}

/// <summary>
/// User-configurable character profile & persona lines persisted to %AppData%\GeminiDesktopPet\character.json.
/// Blank or omitted fields automatically fall back to built-in Gemini-chan defaults.
/// </summary>
public sealed class CharacterProfile
{
    public string Name { get; set; } = string.Empty;
    public string Alias { get; set; } = string.Empty;
    public string BadgeTag { get; set; } = string.Empty;
    public string Subtitle { get; set; } = string.Empty;
    public string Bio { get; set; } = string.Empty;
    public string SourcePageUrl { get; set; } = string.Empty;
    public string StandeeCaption { get; set; } = string.Empty;
    public string TurnaroundCaption { get; set; } = string.Empty;
    public string FooterAttribution { get; set; } = string.Empty;
    public string CustomStandeePath { get; set; } = string.Empty;
    public string CustomTurnaroundPath { get; set; } = string.Empty;
    public string AiSystemPrompt { get; set; } = string.Empty;
    public List<CharacterTrait> AppearanceTraits { get; set; } = [];
    public Dictionary<string, string[]> Lines { get; set; } = [];
}
