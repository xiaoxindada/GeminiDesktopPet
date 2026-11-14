using System.Collections.ObjectModel;
using System.Diagnostics;
using System.IO;
using System.Windows;
using GeminiDesktopPet.Models;
using GeminiDesktopPet.Services;

namespace GeminiDesktopPet.Views;

public partial class CharacterEditorWindow : Window
{
    public sealed class EditableTrait
    {
        public string Title { get; set; } = string.Empty;
        public string Detail { get; set; } = string.Empty;
    }

    private readonly SpriteManager _spriteManager;
    private readonly CharacterProfileService _characterService;
    private readonly ObservableCollection<EditableTrait> _editableTraits = [];

    private string _customStandeePath = string.Empty;
    private bool _suppressToneSelection;

    public CharacterEditorWindow(SpriteManager spriteManager, CharacterProfileService characterService)
    {
        InitializeComponent();
        _spriteManager = spriteManager;
        _characterService = characterService;

        TraitsEditorList.ItemsSource = _editableTraits;
        BuildPresetButtons();

        var toneChoices = CharacterLore.AiToneOptions
            .Concat([new CharacterLore.AiTonePreset("custom", "自定义细节（使用下方输入框内容）", "")])
            .ToArray();
        AiToneCombo.ItemsSource = toneChoices;

        LoadIntoEditor(_characterService.ReadRawOrDefault());
    }

    public void RefreshFromService()
    {
        LoadIntoEditor(_characterService.ReadRawOrDefault());
    }

    private void BuildPresetButtons()
    {
        PresetButtonsPanel.Children.Clear();
        var chipStyle = (Style)FindResource("PresetChipBtn");
        foreach (var preset in CharacterLore.BuiltinPresets)
        {
            var btn = new System.Windows.Controls.Button
            {
                Content = preset.ButtonLabel,
                Style = chipStyle,
                Tag = preset
            };
            btn.Click += (_, _) =>
            {
                var p = preset.CreateProfile();
                LoadIntoEditor(p);
                StatusText.Text = $"已套用预设「{preset.ButtonLabel}」，点击右下角「保存并立即生效」即可应用";
            };
            PresetButtonsPanel.Children.Add(btn);
        }
    }

    private void LoadIntoEditor(CharacterProfile raw)
    {
        var resolved = CharacterLore.ResolveWithDefaults(raw);

        NameBox.Text = raw.Name;
        AliasBox.Text = raw.Alias;
        BadgeTagBox.Text = raw.BadgeTag;
        SubtitleBox.Text = raw.Subtitle;
        BioBox.Text = raw.Bio;
        AiPromptBox.Text = raw.AiSystemPrompt;

        _customStandeePath = raw.CustomStandeePath?.Trim() ?? string.Empty;
        TurnaroundPathBox.Text = raw.CustomTurnaroundPath?.Trim() ?? string.Empty;
        RefreshStandeePreview();

        _suppressToneSelection = true;
        var matchedTone = CharacterLore.AiToneOptions.FirstOrDefault(
            t => string.Equals(t.SystemPrompt.Trim(), (raw.AiSystemPrompt ?? string.Empty).Trim(), StringComparison.Ordinal));
        AiToneCombo.SelectedValue = matchedTone?.Id ?? "custom";
        _suppressToneSelection = false;

        _editableTraits.Clear();
        var traitsSource = raw.AppearanceTraits is { Count: > 0 }
            ? raw.AppearanceTraits
            : resolved.AppearanceTraits;
        foreach (var t in traitsSource)
        {
            _editableTraits.Add(new EditableTrait { Title = t.Title, Detail = t.Detail });
        }

        GreetingBox.Text = JoinLines(raw.Lines, resolved.Lines, "greeting");
        PatBox.Text = JoinLines(raw.Lines, resolved.Lines, "pat");
        IdleBox.Text = JoinLines(raw.Lines, resolved.Lines, "idle");
        FloatBox.Text = JoinLines(raw.Lines, resolved.Lines, "float");
        ChatterBox.Text = JoinLines(raw.Lines, resolved.Lines, "chatter");
    }

    private void RefreshStandeePreview()
    {
        StandeePreviewImage.Source = _spriteManager.LoadCustomOrProfileImage(_customStandeePath, "gemini_standee.png");
    }

    private static string JoinLines(
        Dictionary<string, string[]>? primary,
        Dictionary<string, string[]> fallback,
        string key)
    {
        if (primary != null && primary.TryGetValue(key, out var arr) && arr.Length > 0)
            return string.Join(Environment.NewLine, arr);
        if (fallback.TryGetValue(key, out var defArr) && defArr.Length > 0)
            return string.Join(Environment.NewLine, defArr);
        return string.Empty;
    }

    private static string[] SplitLines(string text)
        => text.Split(["\r\n", "\n"], StringSplitOptions.RemoveEmptyEntries)
            .Select(s => s.Trim())
            .Where(s => !string.IsNullOrEmpty(s))
            .ToArray();

    private void AiToneCombo_SelectionChanged(object sender, System.Windows.Controls.SelectionChangedEventArgs e)
    {
        if (_suppressToneSelection) return;
        if (AiToneCombo.SelectedItem is CharacterLore.AiTonePreset tone && tone.Id != "custom")
        {
            AiPromptBox.Text = tone.SystemPrompt;
        }
    }

    private void PickStandeeBtn_Click(object sender, RoutedEventArgs e)
    {
        string? picked = PickImageFile("选择自定义立绘预览图片");
        if (picked == null) return;
        _customStandeePath = picked;
        RefreshStandeePreview();
        StatusText.Text = "已预览所选立绘（记得点击保存生效）";
    }

    private void ClearStandeeBtn_Click(object sender, RoutedEventArgs e)
    {
        _customStandeePath = string.Empty;
        RefreshStandeePreview();
        StatusText.Text = "已恢复默认原案立绘预览";
    }

    private void PickTurnaroundBtn_Click(object sender, RoutedEventArgs e)
    {
        string? picked = PickImageFile("选择自定义角色三视图图片");
        if (picked != null)
            TurnaroundPathBox.Text = picked;
    }

    private void ClearTurnaroundBtn_Click(object sender, RoutedEventArgs e)
        => TurnaroundPathBox.Text = string.Empty;

    private string? PickImageFile(string title)
    {
        var dlg = new Microsoft.Win32.OpenFileDialog
        {
            Title = title,
            Filter = "图片文件 (*.png;*.jpg;*.jpeg;*.webp;*.bmp)|*.png;*.jpg;*.jpeg;*.webp;*.bmp|所有文件 (*.*)|*.*"
        };
        return dlg.ShowDialog(this) == true ? dlg.FileName : null;
    }

    private void AddTraitBtn_Click(object sender, RoutedEventArgs e)
        => _editableTraits.Add(new EditableTrait { Title = "新设定词条", Detail = "" });

    private void RemoveTraitBtn_Click(object sender, RoutedEventArgs e)
    {
        if (sender is System.Windows.Controls.Button btn && btn.Tag is EditableTrait item)
            _editableTraits.Remove(item);
    }

    private void SaveBtn_Click(object sender, RoutedEventArgs e)
    {
        var existingRaw = _characterService.ReadRawOrDefault();
        var lines = existingRaw.Lines != null
            ? new Dictionary<string, string[]>(existingRaw.Lines, StringComparer.OrdinalIgnoreCase)
            : new Dictionary<string, string[]>(StringComparer.OrdinalIgnoreCase);

        void UpdateCategory(string key, string rawText)
        {
            var arr = SplitLines(rawText);
            if (arr.Length > 0)
                lines[key] = arr;
            else
                lines.Remove(key);
        }

        UpdateCategory("greeting", GreetingBox.Text);
        UpdateCategory("pat", PatBox.Text);
        UpdateCategory("idle", IdleBox.Text);
        UpdateCategory("float", FloatBox.Text);
        UpdateCategory("chatter", ChatterBox.Text);

        var updatedRaw = new CharacterProfile
        {
            Name = NameBox.Text.Trim(),
            Alias = AliasBox.Text.Trim(),
            BadgeTag = BadgeTagBox.Text.Trim(),
            Subtitle = SubtitleBox.Text.Trim(),
            Bio = BioBox.Text.Trim(),
            AiSystemPrompt = AiPromptBox.Text.Trim(),
            CustomStandeePath = _customStandeePath,
            CustomTurnaroundPath = TurnaroundPathBox.Text.Trim(),
            SourcePageUrl = existingRaw.SourcePageUrl,
            StandeeCaption = existingRaw.StandeeCaption,
            TurnaroundCaption = existingRaw.TurnaroundCaption,
            FooterAttribution = existingRaw.FooterAttribution,
            AppearanceTraits = _editableTraits
                .Where(t => !string.IsNullOrWhiteSpace(t.Title) || !string.IsNullOrWhiteSpace(t.Detail))
                .Select(t => new CharacterTrait(t.Title.Trim(), t.Detail.Trim()))
                .ToList(),
            Lines = lines
        };

        _characterService.Save(updatedRaw);
        StatusText.Text = "✓ 已保存并立即应用（空白项自动沿用默认设定）";
    }

    private void ResetDefaultBtn_Click(object sender, RoutedEventArgs e)
    {
        var reset = _characterService.ResetToDefault();
        LoadIntoEditor(reset);
        StatusText.Text = "✓ 已恢复并重新生成默认 Gemini 娘设定";
    }

    private void OpenJsonBtn_Click(object sender, RoutedEventArgs e)
    {
        try
        {
            _characterService.EnsureGenerated();
            string file = _characterService.CharacterFilePath;
            string dir = Path.GetDirectoryName(file) ?? Environment.GetFolderPath(Environment.SpecialFolder.UserProfile);
            Process.Start(new ProcessStartInfo(file)
            {
                WorkingDirectory = dir,
                UseShellExecute = true
            });
            StatusText.Text = "已打开 character.json，修改保存后点击「从文件重载」生效";
        }
        catch
        {
            // Ignore shell launch error.
        }
    }

    private void ReloadJsonBtn_Click(object sender, RoutedEventArgs e)
    {
        _characterService.Reload();
        LoadIntoEditor(_characterService.ReadRawOrDefault());
        StatusText.Text = "✓ 已从 character.json 重新加载并应用";
    }

    private void CloseBtn_Click(object sender, RoutedEventArgs e) => Close();
}
