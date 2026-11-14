using System.Diagnostics;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Input;
using System.Windows.Media.Imaging;
using GeminiDesktopPet.Models;
using GeminiDesktopPet.Services;

namespace GeminiDesktopPet.Views;

public partial class ProfileWindow : Window
{
    public sealed record TraitItem(string Title, string Detail);
    public sealed record SpriteCardItem(string Key, BitmapSource Image);

    private readonly Action<string>? _previewSpriteCallback;

    public ProfileWindow(SpriteManager spriteManager, PetSettings settings, Action<string>? previewSpriteCallback = null)
    {
        InitializeComponent();
        _previewSpriteCallback = previewSpriteCallback;

        BondText.Text = $"好感度 {settings.Favorability} ✦ 累计摸头 {settings.TotalPats} 次";
        TraitsList.ItemsSource = CharacterLore.AppearanceTraits
            .Select(t => new TraitItem(t.Title, t.Detail))
            .ToArray();

        StandeeImage.Source = spriteManager.GetProfileImage("gemini_standee.png");
        TurnaroundImage.Source = spriteManager.GetProfileImage("gemini_turnaround.png");

        var cards = spriteManager.ListAvailableSpriteKeys()
            .Select(key => new SpriteCardItem(key, spriteManager.GetSprite(key)))
            .ToArray();
        SpritesGallery.ItemsSource = cards;
    }

    private void SpriteCard_Click(object sender, RoutedEventArgs e)
    {
        if (sender is System.Windows.Controls.Button btn && btn.Tag is string key)
            _previewSpriteCallback?.Invoke(key);
    }

    private void SourceLink_Click(object sender, MouseButtonEventArgs e)
    {
        try
        {
            Process.Start(new ProcessStartInfo(CharacterLore.SourcePageUrl) { UseShellExecute = true });
        }
        catch
        {
            // Ignore shell launch error.
        }
    }
}
