using System.Diagnostics;
using System.Windows;
using System.Windows.Input;
using System.Windows.Media.Imaging;
using GeminiDesktopPet.Models;
using GeminiDesktopPet.Services;

namespace GeminiDesktopPet.Views;

public partial class ProfileWindow : Window
{
    public sealed record TraitItem(string Title, string Detail);
    public sealed record SpriteCardItem(string Key, BitmapSource Image);

    private readonly SpriteManager _spriteManager;
    private readonly PetSettings _settings;
    private readonly CharacterProfileService _characterService;
    private readonly Action<string>? _previewSpriteCallback;

    private string _activeSourceUrl = CharacterLore.SourcePageUrl;

    public event Action? OpenCharacterEditorRequested;

    public ProfileWindow(
        SpriteManager spriteManager,
        PetSettings settings,
        CharacterProfileService characterService,
        Action<string>? previewSpriteCallback = null)
    {
        InitializeComponent();
        _spriteManager = spriteManager;
        _settings = settings;
        _characterService = characterService;
        _previewSpriteCallback = previewSpriteCallback;

        ApplyProfile(_characterService.Current);

        var cards = _spriteManager.ListAvailableSpriteKeys()
            .Select(key => new SpriteCardItem(key, _spriteManager.GetSprite(key)))
            .ToArray();
        SpritesGallery.ItemsSource = cards;
    }

    public void RefreshBondDisplay()
    {
        BondText.Text = $"好感度 {_settings.Favorability} ✦ 累计摸头 {_settings.TotalPats} 次";
    }

    public void ApplyProfile(CharacterProfile profile)
    {
        Title = $"{profile.Name} · 角色档案与设定图鉴";
        ProfileNameText.Text = $"✦ {profile.Name}";
        ProfileAliasText.Text = profile.Alias;
        ProfileBadgeTagText.Text = profile.BadgeTag;
        ProfileSubtitleText.Text = profile.Subtitle;
        ProfileBioText.Text = profile.Bio;
        StandeeCaptionText.Text = profile.StandeeCaption;
        TurnaroundCaptionText.Text = profile.TurnaroundCaption;
        ProfileFooterText.Text = profile.FooterAttribution;
        _activeSourceUrl = profile.SourcePageUrl;
        SourceLinkText.Text = string.IsNullOrWhiteSpace(profile.SourcePageUrl)
            ? string.Empty
            : $"来源：{profile.SourcePageUrl.Replace("https://", "", StringComparison.OrdinalIgnoreCase)}";

        RefreshBondDisplay();
        TraitsList.ItemsSource = profile.AppearanceTraits
            .Select(t => new TraitItem(t.Title, t.Detail))
            .ToArray();

        StandeeImage.Source = _spriteManager.LoadCustomOrProfileImage(profile.CustomStandeePath, "gemini_standee.png");
        TurnaroundImage.Source = _spriteManager.LoadCustomOrProfileImage(profile.CustomTurnaroundPath, "gemini_turnaround.png");
    }

    private void EditProfileBtn_Click(object sender, RoutedEventArgs e)
        => OpenCharacterEditorRequested?.Invoke();

    private void SpriteCard_Click(object sender, RoutedEventArgs e)
    {
        if (sender is System.Windows.Controls.Button btn && btn.Tag is string key)
            _previewSpriteCallback?.Invoke(key);
    }

    private void SourceLink_Click(object sender, MouseButtonEventArgs e)
    {
        if (string.IsNullOrWhiteSpace(_activeSourceUrl)) return;
        try
        {
            Process.Start(new ProcessStartInfo(_activeSourceUrl) { UseShellExecute = true });
        }
        catch
        {
            // Ignore shell launch error.
        }
    }
}
