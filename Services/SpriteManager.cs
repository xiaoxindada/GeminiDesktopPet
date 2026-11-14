using System.IO;
using System.Windows.Media.Imaging;
using GeminiDesktopPet.Models;

namespace GeminiDesktopPet.Services;

/// <summary>
/// Loads, freezes, and caches all Gemini-chan PNG sprites (Chibi + Standard) and reference profile artwork.
/// Resolves Assets directory robustly across Debug/Release/Visual Studio/shortcut launches.
/// </summary>
public sealed class SpriteManager
{
    private readonly string _spritesDir;
    private readonly string _standardDir;
    private readonly string _profileDir;
    private readonly string _walkRigDir;
    private readonly Dictionary<string, BitmapSource> _cache = new(StringComparer.OrdinalIgnoreCase);

    public SpriteManager()
    {
        string assetsRoot = ResolveAssetsRoot();
        _spritesDir = Path.Combine(assetsRoot, "Sprites");
        _standardDir = Path.Combine(assetsRoot, "Standard");
        _profileDir = Path.Combine(assetsRoot, "Profile");
        _walkRigDir = Path.Combine(assetsRoot, "WalkRig");
    }

    private static string ResolveAssetsRoot()
    {
        string[] seeds =
        [
            AppContext.BaseDirectory,
            Environment.CurrentDirectory
        ];

        foreach (string seed in seeds)
        {
            if (string.IsNullOrWhiteSpace(seed)) continue;
            var dir = new DirectoryInfo(seed);
            for (int depth = 0; depth < 5 && dir != null; depth++, dir = dir.Parent)
            {
                string candidate = Path.Combine(dir.FullName, "Assets");
                if (Directory.Exists(Path.Combine(candidate, "Sprites")))
                    return candidate;
            }
        }

        return Path.Combine(AppContext.BaseDirectory, "Assets");
    }

    public BitmapSource GetSprite(PetState state, PetStyleMode style = PetStyleMode.Chibi)
    {
        if (style == PetStyleMode.Standard)
            return GetStandardSpriteForState(state);
        return GetSprite(state.ToSpriteKey());
    }

    public BitmapSource GetSprite(string key)
    {
        string cacheKey = "chibi:" + key;
        if (_cache.TryGetValue(cacheKey, out var cached))
            return cached;

        string path = Path.Combine(_spritesDir, $"{key}.png");
        if (!File.Exists(path))
        {
            if (key != "idle")
                return GetSprite("idle");
            throw new FileNotFoundException($"Missing fallback sprite: {path}");
        }

        var bmp = LoadFrozenBitmap(path);
        _cache[cacheKey] = bmp;
        return bmp;
    }

    private BitmapSource GetStandardSpriteForState(PetState state)
    {
        string file = state switch
        {
            PetState.EdgeLeft => "edge_left",
            PetState.EdgeLeftPeek => "edge_left_peek",
            PetState.EdgeRight => "edge_right",
            PetState.EdgeRightPeek => "edge_right_peek",
            PetState.Running or PetState.Searching => "side",
            PetState.Sulk or PetState.Sleeping or PetState.Bye or PetState.Tired => "back",
            PetState.Idle2 or PetState.Idle3 or PetState.Thinking or PetState.Reading or PetState.Writing
                or PetState.Listening or PetState.Annoyed or PetState.Puffed or PetState.Redhot
                or PetState.Compact => "front",
            _ => "standee"
        };

        string cacheKey = "std:" + file;
        if (_cache.TryGetValue(cacheKey, out var cached))
            return cached;

        string path = Path.Combine(_standardDir, $"{file}.png");
        if (!File.Exists(path))
        {
            string fallback = Path.Combine(_standardDir, "standee.png");
            if (File.Exists(fallback))
                path = fallback;
            else
                return GetSprite(state.ToSpriteKey());
        }

        var bmp = LoadFrozenBitmap(path);
        _cache[cacheKey] = bmp;
        return bmp;
    }

    public BitmapSource? GetWalkRigPart(string fileName)
    {
        string cacheKey = "rig:" + fileName;
        if (_cache.TryGetValue(cacheKey, out var cached))
            return cached;

        string path = Path.Combine(_walkRigDir, fileName);
        if (!File.Exists(path))
            return null;

        var bmp = LoadFrozenBitmap(path);
        _cache[cacheKey] = bmp;
        return bmp;
    }

    public BitmapSource? GetProfileImage(string fileName)
    {
        string path = Path.Combine(_profileDir, fileName);
        if (!File.Exists(path))
            return null;
        return LoadFrozenBitmap(path);
    }

    public BitmapSource? LoadCustomOrProfileImage(string? customPath, string fallbackProfileFileName)
    {
        if (!string.IsNullOrWhiteSpace(customPath))
        {
            try
            {
                string trimmed = customPath.Trim().Trim('"');
                if (File.Exists(trimmed))
                    return LoadFrozenBitmap(trimmed);
            }
            catch
            {
                // Fall back cleanly to built-in image on invalid/corrupted custom file.
            }
        }
        return GetProfileImage(fallbackProfileFileName);
    }

    public IReadOnlyList<string> ListAvailableSpriteKeys()
    {
        if (!Directory.Exists(_spritesDir))
            return [];

        return Directory.EnumerateFiles(_spritesDir, "*.png")
            .Select(Path.GetFileNameWithoutExtension)
            .Where(name => !string.IsNullOrWhiteSpace(name) && !string.Equals(name, "avatar", StringComparison.OrdinalIgnoreCase))
            .OrderBy(name => name, StringComparer.OrdinalIgnoreCase)
            .ToArray()!;
    }

    private static BitmapImage LoadFrozenBitmap(string path)
    {
        // Decode from memory buffer so WPF never keeps file handles or URI cache locks on Assets/*.png.
        byte[] bytes = File.ReadAllBytes(path);
        using var ms = new MemoryStream(bytes);
        var bmp = new BitmapImage();
        bmp.BeginInit();
        bmp.CacheOption = BitmapCacheOption.OnLoad;
        bmp.CreateOptions = BitmapCreateOptions.IgnoreColorProfile;
        bmp.StreamSource = ms;
        bmp.EndInit();
        bmp.Freeze();
        return bmp;
    }
}
