using System.Diagnostics;
using System.Windows;
using System.Windows.Input;
using System.Windows.Media;
using GeminiDesktopPet.Models;
using GeminiDesktopPet.Services;

namespace GeminiDesktopPet.Views;

public partial class UpdateWindow : Window
{
    private readonly UpdateService _updateService;
    private readonly PetSettings _settings;
    private readonly SettingsService _settingsService;
    private readonly Action _triggerGracefulExit;
    private readonly Action? _beforeExitCleanup;

    private CancellationTokenSource? _cts;
    private UpdateCheckResult? _latestResult;
    private bool _isBusy;

    public event Action<UpdateCheckResult>? CheckCompleted;

    public UpdateWindow(
        UpdateService updateService,
        PetSettings settings,
        SettingsService settingsService,
        Action triggerGracefulExit,
        UpdateCheckResult? initialResult = null,
        Action? beforeExitCleanup = null)
    {
        InitializeComponent();
        _updateService = updateService;
        _settings = settings;
        _settingsService = settingsService;
        _triggerGracefulExit = triggerGracefulExit;
        _beforeExitCleanup = beforeExitCleanup;

        CurrentVersionBadge.Text = $"当前 {AppVersion.CurrentDisplay}";
        UpstreamLinkText.Text = $"上游追踪源：{UpdateService.NormalizeReleasesUrl(_settings.UpdateReleasesUrl)}";

        Closed += (_, _) =>
        {
            _cts?.Cancel();
            _cts?.Dispose();
            _cts = null;
        };

        if (initialResult != null)
        {
            ApplyCheckResult(initialResult);
        }
        else
        {
            Loaded += async (_, _) => await RunCheckAsync();
        }
    }

    public async Task RunCheckAsync()
    {
        if (_isBusy) return;
        _isBusy = true;
        RefreshBtn.IsEnabled = false;
        InstallUpdateBtn.IsEnabled = false;
        SkipVersionBtn.Visibility = Visibility.Collapsed;
        StatusText.Text = "正在连接 GitHub Releases 检查最新版本...";
        ReleaseMetaText.Text = $"追踪地址：{UpdateService.GetLatestTrackingUrl(_settings.UpdateReleasesUrl)}";

        _cts?.Cancel();
        _cts?.Dispose();
        _cts = new CancellationTokenSource();

        try
        {
            var result = await _updateService.CheckForUpdateAsync(_settings.UpdateReleasesUrl, _cts.Token);
            ApplyCheckResult(result);
            CheckCompleted?.Invoke(result);
        }
        catch (OperationCanceledException)
        {
            StatusText.Text = "已取消检查更新。";
        }
        finally
        {
            _isBusy = false;
            RefreshBtn.IsEnabled = true;
        }
    }

    public void ApplyCheckResult(UpdateCheckResult result)
    {
        _latestResult = result;
        CurrentVersionBadge.Text = $"当前 {AppVersion.FormatTag(result.CurrentVersion)}";
        UpstreamLinkText.Text = $"上游追踪源：{UpdateService.NormalizeReleasesUrl(_settings.UpdateReleasesUrl)}";
        StatusText.Text = result.StatusMessage;

        if (!result.Succeeded)
        {
            LatestVersionBadge.Text = "检查失败";
            ApplyBadgeColor(LatestBadgeBorder, LatestVersionBadge, "#451E2E", "#FCA5A5");
            ReleaseMetaText.Text = "可检查网络连接或代理设置，也可直接点击下方按钮在浏览器查看 Releases。";
            ChangelogBox.Text = result.StatusMessage;
            InstallUpdateBtn.IsEnabled = false;
            return;
        }

        if (result.NoPublishedReleases || string.IsNullOrWhiteSpace(result.LatestTag))
        {
            LatestVersionBadge.Text = "暂无发布";
            ApplyBadgeColor(LatestBadgeBorder, LatestVersionBadge, "#252048", "#B9B2DE");
            ReleaseMetaText.Text = "仓库上游尚未创建 Release，创建新 Release 后将自动识别。";
            ChangelogBox.Text = "上游 Releases 暂无发布记录。\n待创建版本标签并通过发布工作流生成 Changelog 后，这里会自动显示 Git 提交摘要与更新说明。";
            InstallUpdateBtn.IsEnabled = false;
            return;
        }

        LatestVersionBadge.Text = $"最新 {AppVersion.FormatTag(result.LatestVersion)}";
        if (result.UpdateAvailable)
        {
            ApplyBadgeColor(LatestBadgeBorder, LatestVersionBadge, "#184639", "#6EE7B7");
            SkipVersionBtn.Visibility = Visibility.Visible;
        }
        else
        {
            ApplyBadgeColor(LatestBadgeBorder, LatestVersionBadge, "#263B61", "#93C5FD");
            SkipVersionBtn.Visibility = Visibility.Collapsed;
        }

        string dateText = result.PublishedAt.HasValue
            ? $"发布时间：{result.PublishedAt.Value:yyyy-MM-dd HH:mm}"
            : "发布时间：未知";
        string assetText = result.PackageAsset != null
            ? result.PackageAsset.SizeBytes > 0
                ? $"安装包：{result.PackageAsset.Name}（{result.PackageAsset.SizeBytes / 1048576.0:F1} MB）"
                : $"安装包：{result.PackageAsset.Name}"
            : "安装包：未附带 ZIP 压缩包（请前往浏览器查看）";
        ReleaseMetaText.Text = $"{result.ReleaseTitle}  ·  {dateText}  ·  {assetText}";

        ChangelogBox.Text = result.ChangelogMarkdown;
        InstallUpdateBtn.IsEnabled = result.UpdateAvailable && result.PackageAsset != null;
    }

    private static void ApplyBadgeColor(System.Windows.Controls.Border border, System.Windows.Controls.TextBlock text, string bgHex, string fgHex)
    {
        border.Background = new SolidColorBrush((System.Windows.Media.Color)System.Windows.Media.ColorConverter.ConvertFromString(bgHex));
        text.Foreground = new SolidColorBrush((System.Windows.Media.Color)System.Windows.Media.ColorConverter.ConvertFromString(fgHex));
    }

    private async void RefreshBtn_Click(object sender, RoutedEventArgs e)
    {
        await RunCheckAsync();
    }

    private async void InstallUpdateBtn_Click(object sender, RoutedEventArgs e)
    {
        if (_isBusy || _latestResult?.PackageAsset == null)
            return;

        if (!UpdateService.CanApplyInPlaceUpdate(out string blockReason))
        {
            System.Windows.MessageBox.Show(this, blockReason, "提示", MessageBoxButton.OK, MessageBoxImage.Information);
            OpenReleasePage();
            return;
        }

        _isBusy = true;
        RefreshBtn.IsEnabled = false;
        InstallUpdateBtn.IsEnabled = false;
        ProgressPanel.Visibility = Visibility.Visible;
        DownloadProgressBar.IsIndeterminate = false;
        DownloadProgressBar.Value = 0;

        _cts?.Cancel();
        _cts?.Dispose();
        _cts = new CancellationTokenSource();

        try
        {
            var asset = _latestResult.PackageAsset;
            var progress = new Progress<(long DownloadedBytes, long? TotalBytes)>(p =>
            {
                if (p.TotalBytes is > 0)
                {
                    double pct = Math.Clamp(p.DownloadedBytes * 100.0 / p.TotalBytes.Value, 0, 100);
                    DownloadProgressBar.IsIndeterminate = false;
                    DownloadProgressBar.Value = pct;
                    DownloadPercentText.Text = $"{pct:F0}%";
                    DownloadStatusText.Text = $"正在下载 {asset.Name} ({p.DownloadedBytes / 1048576.0:F1} / {p.TotalBytes.Value / 1048576.0:F1} MB)...";
                }
                else
                {
                    DownloadProgressBar.IsIndeterminate = true;
                    DownloadPercentText.Text = $"{p.DownloadedBytes / 1048576.0:F1} MB";
                    DownloadStatusText.Text = $"正在下载 {asset.Name}...";
                }
            });

            string zipPath = await _updateService.DownloadAssetAsync(asset, _latestResult.LatestTag, progress, _cts.Token);

            DownloadStatusText.Text = "正在解压并准备重启更新...";
            DownloadPercentText.Text = "100%";
            DownloadProgressBar.IsIndeterminate = true;

            // Clear skipped version on successful update installation
            _settings.SkippedUpdateVersion = string.Empty;
            _settingsService.Save(_settings);

            UpdateService.ExtractAndLaunchUpdater(
                zipPath,
                _latestResult.LatestTag,
                _triggerGracefulExit,
                _beforeExitCleanup);
        }
        catch (OperationCanceledException)
        {
            DownloadStatusText.Text = "下载已取消。";
            InstallUpdateBtn.IsEnabled = true;
        }
        catch (Exception ex)
        {
            DownloadStatusText.Text = $"更新失败：{ex.Message}";
            System.Windows.MessageBox.Show(this, $"下载或安装更新失败：{ex.Message}", "更新失败", MessageBoxButton.OK, MessageBoxImage.Warning);
            InstallUpdateBtn.IsEnabled = true;
        }
        finally
        {
            _isBusy = false;
            RefreshBtn.IsEnabled = true;
        }
    }

    private void SkipVersionBtn_Click(object sender, RoutedEventArgs e)
    {
        if (_latestResult == null || string.IsNullOrWhiteSpace(_latestResult.LatestTag))
            return;

        _settings.SkippedUpdateVersion = _latestResult.LatestTag;
        _settingsService.Save(_settings);
        StatusText.Text = $"已跳过版本 {_latestResult.LatestTag} 的自动提醒（仍可随时手动点击检查更新）。";
        SkipVersionBtn.Visibility = Visibility.Collapsed;
    }

    private void OpenBrowserBtn_Click(object sender, RoutedEventArgs e) => OpenReleasePage();

    private void UpstreamLink_Click(object sender, MouseButtonEventArgs e) => OpenReleasePage();

    private void OpenReleasePage()
    {
        string target = !string.IsNullOrWhiteSpace(_latestResult?.ReleaseHtmlUrl)
            ? _latestResult.ReleaseHtmlUrl
            : UpdateService.GetLatestTrackingUrl(_settings.UpdateReleasesUrl);
        try
        {
            Process.Start(new ProcessStartInfo(target) { UseShellExecute = true });
        }
        catch
        {
            // Ignore shell launch error.
        }
    }

    private void CloseBtn_Click(object sender, RoutedEventArgs e) => Close();
}
