using System.IO;
using System.Text;
using System.Text.Encodings.Web;
using System.Text.Json;
using GeminiDesktopPet.Models;

namespace GeminiDesktopPet.Services;

/// <summary>
/// Manages user-configurable character profile persistence (%AppData%\GeminiDesktopPet\character.json).
/// Generates the default configuration only when missing or invalid, and fills blank fields with defaults.
/// </summary>
public sealed class CharacterProfileService
{
    private static readonly JsonSerializerOptions JsonOptions = new()
    {
        WriteIndented = true,
        Encoder = JavaScriptEncoder.UnsafeRelaxedJsonEscaping
    };

    public string CharacterFilePath { get; }
    public CharacterProfile Current { get; private set; }

    public event Action<CharacterProfile>? ProfileChanged;

    public CharacterProfileService(string settingsFolder)
    {
        Directory.CreateDirectory(settingsFolder);
        CharacterFilePath = Path.Combine(settingsFolder, "character.json");
        Current = LoadOrGenerateDefault();
        CharacterLore.SetActiveProfile(Current);
    }

    public CharacterProfile LoadOrGenerateDefault()
    {
        if (File.Exists(CharacterFilePath))
        {
            try
            {
                string json = File.ReadAllText(CharacterFilePath, Encoding.UTF8);
                if (!string.IsNullOrWhiteSpace(json))
                {
                    var loaded = JsonSerializer.Deserialize<CharacterProfile>(json, JsonOptions);
                    if (loaded != null)
                    {
                        Current = CharacterLore.ResolveWithDefaults(loaded);
                        CharacterLore.SetActiveProfile(Current);
                        return Current;
                    }
                }
            }
            catch
            {
                // Regenerate default below when configuration is unreadable.
            }
        }

        var generated = CharacterLore.CreateDefaultProfile();
        WriteFileInternal(generated);
        Current = CharacterLore.ResolveWithDefaults(generated);
        CharacterLore.SetActiveProfile(Current);
        return Current;
    }

    public CharacterProfile EnsureGenerated()
    {
        if (!File.Exists(CharacterFilePath))
            return LoadOrGenerateDefault();
        return Current;
    }

    public CharacterProfile Reload()
    {
        var profile = LoadOrGenerateDefault();
        ProfileChanged?.Invoke(profile);
        return profile;
    }

    public CharacterProfile Save(CharacterProfile userConfig)
    {
        WriteFileInternal(userConfig);
        Current = CharacterLore.ResolveWithDefaults(userConfig);
        CharacterLore.SetActiveProfile(Current);
        ProfileChanged?.Invoke(Current);
        return Current;
    }

    public CharacterProfile ResetToDefault()
    {
        var def = CharacterLore.CreateDefaultProfile();
        WriteFileInternal(def);
        Current = CharacterLore.ResolveWithDefaults(def);
        CharacterLore.SetActiveProfile(Current);
        ProfileChanged?.Invoke(Current);
        return Current;
    }

    public CharacterProfile ReadRawOrDefault()
    {
        if (File.Exists(CharacterFilePath))
        {
            try
            {
                string json = File.ReadAllText(CharacterFilePath, Encoding.UTF8);
                var raw = JsonSerializer.Deserialize<CharacterProfile>(json, JsonOptions);
                if (raw != null)
                    return raw;
            }
            catch
            {
                // Fall back to resolved active profile.
            }
        }
        return Current;
    }

    private void WriteFileInternal(CharacterProfile profile)
    {
        try
        {
            string? dir = Path.GetDirectoryName(CharacterFilePath);
            if (!string.IsNullOrWhiteSpace(dir))
                Directory.CreateDirectory(dir);
            string json = JsonSerializer.Serialize(profile, JsonOptions);
            File.WriteAllText(CharacterFilePath, json, new UTF8Encoding(false));
        }
        catch
        {
            // Ignore transient IO failures.
        }
    }
}
