using System.Diagnostics;
using System.IO;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Media;
using GeminiDesktopPet.Models;
using GeminiDesktopPet.Services;

namespace GeminiDesktopPet.Views;

public partial class SettingsWindow : Window
{
    private readonly PetSettings _settings;
    private readonly SettingsService _settingsService;
    private readonly AppFollowService _followService;
    private bool _initializing = true;
    private bool _allowClose;

    public event Action? SettingsChanged;
    public event Action? FollowSettingsChanged;

    public sealed record OptionItem(string Id, string Label);

    public SettingsWindow(PetSettings settings, SettingsService settingsService, AppFollowService followService)
    {
        InitializeComponent();
        _settings = settings;
        _settingsService = settingsService;
        _followService = followService;

        StyleModeBox.ItemsSource = new[]
        {
            new OptionItem("Chibi", "Q 版猫娘（55 种 Q 版动作表情）"),
            new OptionItem("Standard", "标准立绘版（原案修长比例 + 三视图）")
        };

        CostumeBox.ItemsSource = CharacterLore.CostumeOptions
            .Select(c => new OptionItem(c.Id, c.Label))
            .ToArray();

        RefreshFromSettings();
    }

    public void RefreshFromSettings()
    {
        _initializing = true;

        StyleModeBox.SelectedValue = _settings.StyleMode.ToString();
        ScaleSlider.Value = _settings.Scale;
        ScaleLabel.Text = $"{_settings.Scale:F2}x";
        OpacitySlider.Value = _settings.Opacity;
        OpacityLabel.Text = $"{(int)Math.Round(_settings.Opacity * 100)}%";
        CostumeBox.SelectedValue = _settings.CostumeOverride;

        AlwaysOnTopCheck.IsChecked = _settings.AlwaysOnTop;
        AutoWalkCheck.IsChecked = _settings.AutoWalk;
        AntiGravityCheck.IsChecked = _settings.AntiGravityMode;
        GravityCheck.IsChecked = _settings.GravityEnabled;
        EdgeDockCheck.IsChecked = _settings.EdgeDockEnabled;
        ParticlesCheck.IsChecked = _settings.ParticlesEnabled;
        SoundCheck.IsChecked = _settings.SoundEnabled;
        SysMonCheck.IsChecked = _settings.SystemMonitorEnabled;
        ShowSpeechBubbleCheck.IsChecked = _settings.ShowSpeechBubble;
        AutoStartCheck.IsChecked = _settingsService.IsAutoStartEnabled();

        FollowAntigravityCheck.IsChecked = _settings.FollowAntigravity;
        FollowGeminiAppCheck.IsChecked = _settings.FollowGeminiApp;
        SleepOnAgExitCheck.IsChecked = _settings.SleepWhenAntigravityExits;
        UpdateAntigravityBadge();

        ApiKeyBox.Password = _settings.GeminiApiKey;
        ModelBox.Text = _settings.GeminiModel;

        _initializing = false;
    }

    public void UpdateAntigravityBadge()
    {
        ApplyStatusBadge(AgStatusText, AntigravityWatcherService.CheckProcessRunning(), "● Antigravity 运行中", "○ 未检测到 Antigravity");
        ApplyStatusBadge(GeminiStatusText, AntigravityWatcherService.CheckGeminiAppRunning(), "● Gemini App 运行中", "○ 未检测到 Gemini App");
        bool active = AppFollowService.AnyFollowEnabled(_settings);
        bool logonEnabled = _followService.IsFollowLogonEnabled();
        string followText = (active, logonEnabled) switch
        {
            (true, true) => "● 原生联动守护运行中（已同步开机待机）",
            (true, false) => "◐ 原生联动守护运行中",
            _ => "○ 应用联动已停用"
        };
        ApplyStatusBadge(VbsStatusText, active, followText, followText);
    }

    private static void ApplyStatusBadge(TextBlock label, bool active, string activeText, string inactiveText)
    {
        label.Text = active ? activeText : inactiveText;
        label.Foreground = new SolidColorBrush((System.Windows.Media.Color)System.Windows.Media.ColorConverter.ConvertFromString(active ? "#6EE7B7" : "#B6AEDB"));
    }

    private void AnyOption_Changed(object sender, RoutedEventArgs e)
    {
        if (_initializing) return;

        if (StyleModeBox.SelectedValue is string styleId && Enum.TryParse<PetStyleMode>(styleId, out var mode))
            _settings.StyleMode = mode;

        _settings.Scale = Math.Round(ScaleSlider.Value, 2);
        ScaleLabel.Text = $"{_settings.Scale:F2}x";

        _settings.Opacity = Math.Round(OpacitySlider.Value, 2);
        OpacityLabel.Text = $"{(int)Math.Round(_settings.Opacity * 100)}%";

        if (CostumeBox.SelectedValue is string costumeId)
        {
            _settings.CostumeOverride = costumeId;
            _settings.AutoFestivalCostume = costumeId == "auto";
        }

        _settings.AlwaysOnTop = AlwaysOnTopCheck.IsChecked == true;
        _settings.AutoWalk = AutoWalkCheck.IsChecked == true;
        _settings.AntiGravityMode = AntiGravityCheck.IsChecked == true;
        _settings.GravityEnabled = GravityCheck.IsChecked == true;
        _settings.EdgeDockEnabled = EdgeDockCheck.IsChecked == true;
        _settings.ParticlesEnabled = ParticlesCheck.IsChecked == true;
        _settings.SoundEnabled = SoundCheck.IsChecked == true;
        _settings.SystemMonitorEnabled = SysMonCheck.IsChecked == true;
        _settings.ShowSpeechBubble = ShowSpeechBubbleCheck.IsChecked == true;
        _settings.SleepWhenAntigravityExits = SleepOnAgExitCheck.IsChecked == true;

        _settingsService.Save(_settings);
        SettingsChanged?.Invoke();
    }

    private void AutoStart_Changed(object sender, RoutedEventArgs e)
    {
        if (_initializing) return;
        _settingsService.SetAutoStart(AutoStartCheck.IsChecked == true);
    }

    private void FollowOptions_Changed(object sender, RoutedEventArgs e)
    {
        if (_initializing) return;
        _settings.FollowAntigravity = FollowAntigravityCheck.IsChecked == true;
        _settings.FollowGeminiApp = FollowGeminiAppCheck.IsChecked == true;
        _settingsService.Save(_settings);
        _followService.SyncAndApply(_settings);
        UpdateAntigravityBadge();
        FollowSettingsChanged?.Invoke();
        SettingsChanged?.Invoke();
    }

    private void RestartVbsBtn_Click(object sender, RoutedEventArgs e)
    {
        if (!_settings.FollowAntigravity && !_settings.FollowGeminiApp)
        {
            _settings.FollowAntigravity = true;
            _settings.FollowGeminiApp = true;
            _settingsService.Save(_settings);
            RefreshFromSettings();
            FollowSettingsChanged?.Invoke();
            SettingsChanged?.Invoke();
        }

        _followService.SyncAndApply(_settings);
        UpdateAntigravityBadge();
    }

    private void StopDeleteVbsBtn_Click(object sender, RoutedEventArgs e)
    {
        _settings.FollowAntigravity = false;
        _settings.FollowGeminiApp = false;
        _settingsService.Save(_settings);
        RefreshFromSettings();
        FollowSettingsChanged?.Invoke();
        SettingsChanged?.Invoke();

        _followService.DisableFollow();
        UpdateAntigravityBadge();
    }

    private void OpenVbsFolderBtn_Click(object sender, RoutedEventArgs e)
    {
        try
        {
            _followService.CleanupLegacyFiles();
            string folder = _settingsService.SettingsFolder;
            Directory.CreateDirectory(folder);
            Process.Start(new ProcessStartInfo("explorer.exe", $"\"{folder}\"")
            {
                WorkingDirectory = folder,
                UseShellExecute = true
            });
        }
        catch
        {
            // Ignore explorer launch errors.
        }
    }

    private void ApiField_Changed(object sender, RoutedEventArgs e)
    {
        if (_initializing) return;
        _settings.GeminiApiKey = ApiKeyBox.Password;
        _settings.GeminiModel = string.IsNullOrWhiteSpace(ModelBox.Text) ? "gemini-2.5-flash" : ModelBox.Text.Trim();
        _settingsService.Save(_settings);
    }

    /// <summary>
    /// Allows App shutdown to close the hidden settings window instead of cancelling Shutdown().
    /// </summary>
    public void ForceClose()
    {
        _allowClose = true;
        Close();
    }

    protected override void OnClosing(System.ComponentModel.CancelEventArgs e)
    {
        if (!_allowClose)
        {
            e.Cancel = true;
            Hide();
            return;
        }
        base.OnClosing(e);
    }
}
