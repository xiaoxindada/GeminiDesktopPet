using System.Windows;
using System.Windows.Controls;
using System.Windows.Controls.Primitives;
using System.Windows.Input;
using System.Windows.Media;
using System.Windows.Threading;
using GeminiDesktopPet.Services;
using Color = System.Windows.Media.Color;
using ColorConverter = System.Windows.Media.ColorConverter;
using FormsScreen = System.Windows.Forms.Screen;
using LinearGradientBrush = System.Windows.Media.LinearGradientBrush;
using SolidColorBrush = System.Windows.Media.SolidColorBrush;

namespace GeminiDesktopPet.Views;

public partial class QuotaWindow : Window
{
    private const double ShadowMarginDip = 14.0;

    private readonly AntigravityQuotaService _quotaService;
    private readonly DispatcherTimer _autoCloseTimer;
    private readonly bool _autoCloseEnabled;
    private QuotaSnapshot? _lastSnapshot;
    private CancellationTokenSource? _fetchCts;
    private Window? _anchorPet;
    private bool _isFetching;
    private bool _userDragged;
    private bool _deferReveal;

    public event Action<QuotaSnapshot>? SnapshotLoaded;

    public QuotaWindow(AntigravityQuotaService quotaService, bool autoFetch = true, bool enableAutoClose = true)
    {
        InitializeComponent();
        _quotaService = quotaService;
        _autoCloseEnabled = enableAutoClose;

        _autoCloseTimer = new DispatcherTimer
        {
            Interval = TimeSpan.FromSeconds(8)
        };
        _autoCloseTimer.Tick += (_, _) =>
        {
            _autoCloseTimer.Stop();
            if (IsLoaded)
                Close();
        };

        SizeChanged += (_, _) =>
        {
            if (!_userDragged && _anchorPet != null && IsVisible)
                RepositionNearAnchor();
        };

        ContentRendered += (_, _) =>
        {
            if (!_deferReveal) return;
            _deferReveal = false;
            if (!_userDragged && _anchorPet != null)
                RepositionNearAnchor();

            Dispatcher.BeginInvoke(DispatcherPriority.Render, () =>
            {
                if (!IsLoaded) return;
                BeginAnimation(OpacityProperty, new System.Windows.Media.Animation.DoubleAnimation(0, 1, TimeSpan.FromMilliseconds(90))
                {
                    EasingFunction = new System.Windows.Media.Animation.CubicEase
                    {
                        EasingMode = System.Windows.Media.Animation.EasingMode.EaseOut
                    }
                });
            });
        };

        Closed += (_, _) =>
        {
            _autoCloseTimer.Stop();
            _fetchCts?.Cancel();
            _fetchCts?.Dispose();
            _fetchCts = null;
        };

        if (autoFetch)
        {
            Loaded += async (_, _) => await RefreshQuotaAsync();
        }
        else
        {
            ShowLoadingView();
        }
    }

    public void PrepareForFirstRenderReveal(Window? petWindow)
    {
        _deferReveal = true;
        Opacity = 0;
        ShowLoadingView();
        PositionNearPet(petWindow);
    }

    public void PositionNearPet(Window? petWindow)
    {
        _anchorPet = petWindow;
        _userDragged = false;
        RepositionNearAnchor();
    }

    public void ShowLoadingView(string message = "让我看看额度还有多少。。。。。。")
    {
        _autoCloseTimer.Stop();
        LoadingMessageText.Text = message;
        LoadingPanel.Visibility = Visibility.Visible;
        QuotaPanel.Visibility = Visibility.Collapsed;
        UpdateLayout();
    }

    public async Task RefreshQuotaAsync()
    {
        if (_isFetching) return;
        _isFetching = true;
        _autoCloseTimer.Stop();

        RefreshBtn.IsEnabled = false;
        RefreshBtn.Content = "…";

        if (_lastSnapshot == null)
        {
            ShowLoadingView();
        }
        else
        {
            LoadingPanel.Visibility = Visibility.Collapsed;
            QuotaPanel.Visibility = Visibility.Visible;
            MetaText.Text = "正在刷新最新额度...";
        }

        _fetchCts?.Cancel();
        _fetchCts?.Dispose();
        _fetchCts = new CancellationTokenSource();

        try
        {
            var snap = await _quotaService.FetchQuotaAsync(_fetchCts.Token);
            if (!IsLoaded) return;

            _lastSnapshot = snap;
            RenderSnapshot(snap);
            SnapshotLoaded?.Invoke(snap);
        }
        catch (OperationCanceledException)
        {
        }
        catch (Exception ex)
        {
            if (IsLoaded)
                RenderErrorCard($"读取额度时发生异常：{ex.Message}");
        }
        finally
        {
            _isFetching = false;
            if (IsLoaded)
            {
                RefreshBtn.IsEnabled = true;
                RefreshBtn.Content = "↻";
            }
        }
    }

    public void ApplySnapshot(QuotaSnapshot snapshot)
    {
        _lastSnapshot = snapshot;
        RenderSnapshot(snapshot);
    }

    private void RenderSnapshot(QuotaSnapshot snap)
    {
        LoadingPanel.Visibility = Visibility.Collapsed;
        QuotaPanel.Visibility = Visibility.Visible;

        if (!snap.Success)
        {
            RenderErrorCard(snap.ErrorMessage ?? "未知错误");
            return;
        }

        HeaderTitleText.Text = "Antigravity 剩余额度";
        FooterGrid.Visibility = Visibility.Visible;
        CopyBtn.Visibility = Visibility.Visible;

        string creditsLabel = snap.RemainingCredits.HasValue
            ? $"AI Credits: {snap.RemainingCredits.Value}"
            : "AI Credits: -";
        string modelShort = string.IsNullOrWhiteSpace(snap.ActiveCliModel)
            ? string.Empty
            : $" · {snap.ActiveCliModel}";
        MetaText.Text = $"{creditsLabel} · {snap.FetchedAt.ToLocalTime():HH:mm}{modelShort}";
        MetaText.ToolTip = string.IsNullOrWhiteSpace(snap.AgyPath) ? null : $"数据源：{snap.AgyPath}";

        ContentHost.Children.Clear();

        for (int gi = 0; gi < snap.Groups.Count; gi++)
        {
            var group = snap.Groups[gi];
            if (gi > 0)
            {
                ContentHost.Children.Add(new Border
                {
                    Height = 1,
                    Background = BrushFromHex("#2C2352"),
                    Margin = new Thickness(0, 2, 0, 8)
                });
            }

            if (snap.Groups.Count > 1)
            {
                var groupHeader = new TextBlock
                {
                    Text = FormatGroupTitle(group.Name),
                    FontSize = 11.5,
                    FontWeight = FontWeights.SemiBold,
                    Foreground = BrushFromHex("#C4B5FD"),
                    Margin = new Thickness(0, 0, 0, 6),
                    ToolTip = string.IsNullOrWhiteSpace(group.Description) ? null : group.Description
                };
                ContentHost.Children.Add(groupHeader);
            }

            foreach (var bucket in group.Buckets.OrderBy(b =>
                         b.Window.Equals("5h", StringComparison.OrdinalIgnoreCase) ? 0 :
                         b.Window.Equals("weekly", StringComparison.OrdinalIgnoreCase) ? 1 : 2))
            {
                ContentHost.Children.Add(BuildBucketRow(bucket, group.Description));
            }
        }

        UpdateLayout();
        if (!_userDragged && _anchorPet != null && IsVisible)
            RepositionNearAnchor();
        RestartAutoCloseTimer();
    }

    private void RenderErrorCard(string message)
    {
        LoadingPanel.Visibility = Visibility.Collapsed;
        QuotaPanel.Visibility = Visibility.Visible;
        HeaderTitleText.Text = "Antigravity 额度提示";
        FooterGrid.Visibility = Visibility.Visible;
        CopyBtn.Visibility = Visibility.Collapsed;
        MetaText.Text = $"{DateTime.Now:HH:mm:ss} 查询失败";

        ContentHost.Children.Clear();

        var errStack = new StackPanel { Margin = new Thickness(0, 2, 0, 6) };
        errStack.Children.Add(new TextBlock
        {
            Text = "⚠ 暂时无法读取额度",
            FontSize = 13,
            FontWeight = FontWeights.SemiBold,
            Foreground = BrushFromHex("#FDA4AF"),
            Margin = new Thickness(0, 0, 0, 6)
        });
        errStack.Children.Add(new TextBlock
        {
            Text = message,
            FontSize = 12,
            Foreground = BrushFromHex("#E6E0FF"),
            TextWrapping = TextWrapping.Wrap,
            LineHeight = 18
        });

        var retryBtn = new System.Windows.Controls.Button
        {
            Content = "↻ 重新查询",
            Margin = new Thickness(0, 10, 0, 2),
            HorizontalAlignment = System.Windows.HorizontalAlignment.Left,
            Style = (Style)FindResource("RetryPillBtn")
        };
        retryBtn.Click += async (_, _) => await RefreshQuotaAsync();
        errStack.Children.Add(retryBtn);

        ContentHost.Children.Add(errStack);
        UpdateLayout();
        if (!_userDragged && _anchorPet != null && IsVisible)
            RepositionNearAnchor();
        RestartAutoCloseTimer();
    }

    private void RestartAutoCloseTimer()
    {
        _autoCloseTimer.Stop();
        if (_autoCloseEnabled && IsLoaded)
            _autoCloseTimer.Start();
    }

    private static UIElement BuildBucketRow(QuotaBucketInfo bucket, string? groupDescription)
    {
        double pct = Math.Clamp(bucket.RemainingFraction * 100.0, 0.0, 100.0);
        string pctDisplay = pct.ToString("0.#");
        string windowLabel = FormatWindowLabel(bucket);

        var rowPanel = new StackPanel
        {
            Margin = new Thickness(0, 0, 0, 8),
            ToolTip = BuildBucketTooltip(bucket, groupDescription)
        };

        // Top line matching reference: "5小时 · 剩余 100%"
        rowPanel.Children.Add(new TextBlock
        {
            Text = $"{windowLabel} · 剩余 {pctDisplay}%",
            FontSize = 13,
            FontWeight = FontWeights.Medium,
            Foreground = BrushFromHex("#F5F2FF"),
            Margin = new Thickness(0, 0, 0, 5)
        });

        // Thick pill progress bar adapted to Gemini 娘 violet palette
        var track = new Border
        {
            Background = BrushFromHex("#251D4A"),
            BorderBrush = BrushFromHex("#493A85"),
            BorderThickness = new Thickness(1),
            CornerRadius = new CornerRadius(6),
            Height = 12,
            ClipToBounds = true
        };

        double filledRatio = Math.Clamp(bucket.RemainingFraction, 0.0, 1.0);
        if (filledRatio > 0.001)
        {
            var barGrid = new Grid();
            double emptyRatio = Math.Max(0.0, 1.0 - filledRatio);
            barGrid.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(filledRatio, GridUnitType.Star) });
            barGrid.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(emptyRatio, GridUnitType.Star) });

            var fillBrush = pct >= 25.0
                ? CreateHorizontalGradient("#7C5CFF", "#A78BFA", "#C4B5FD")
                : CreateHorizontalGradient("#DB2777", "#F472B6", "#FB7185");

            var fillBar = new Border
            {
                Background = fillBrush,
                CornerRadius = new CornerRadius(5)
            };
            Grid.SetColumn(fillBar, 0);
            barGrid.Children.Add(fillBar);
            track.Child = barGrid;
        }
        rowPanel.Children.Add(track);

        // Reset line matching reference: "重置: 11月15日 04:13"
        string resetLine = FormatShortResetLine(bucket);
        if (!string.IsNullOrWhiteSpace(resetLine))
        {
            rowPanel.Children.Add(new TextBlock
            {
                Text = resetLine,
                FontSize = 11.5,
                Foreground = BrushFromHex("#A59AD1"),
                Margin = new Thickness(0, 4, 0, 0),
                TextWrapping = TextWrapping.Wrap
            });
        }

        return rowPanel;
    }

    private void RepositionNearAnchor()
    {
        UpdateLayout();
        if (ActualWidth <= 1 || ActualHeight <= 1)
            Measure(new System.Windows.Size(double.PositiveInfinity, double.PositiveInfinity));

        double w = ActualWidth > 1 ? ActualWidth : (DesiredSize.Width > 1 ? DesiredSize.Width : 292);
        double h = ActualHeight > 1 ? ActualHeight : (DesiredSize.Height > 1 ? DesiredSize.Height : 88);
        double visW = Math.Max(120.0, w - ShadowMarginDip * 2.0);
        double visH = Math.Max(40.0, h - ShadowMarginDip * 2.0);
        const double gap = 6.0;

        var (sx, sy) = GetDpiScale();

        if (_anchorPet != null && _anchorPet.IsVisible)
        {
            Rect spriteRect = _anchorPet is PetWindow petWin
                ? petWin.GetVisualSpriteBoundsDip()
                : new Rect(
                    _anchorPet.Left + Math.Max(0, _anchorPet.Width - 210) * 0.5,
                    _anchorPet.Top + Math.Max(0, _anchorPet.Height - 240),
                    Math.Min(_anchorPet.Width, 210),
                    Math.Min(_anchorPet.Height, 240));

            double spriteCenterX = spriteRect.Left + spriteRect.Width * 0.5;
            double spriteCenterY = spriteRect.Top + spriteRect.Height * 0.5;
            var area = GetWorkAreaDip(spriteCenterX, spriteCenterY, sx, sy);

            bool preferLeft = spriteCenterX >= area.Left + area.Width * 0.5;
            double leftSide = spriteRect.Left - gap - visW;
            double rightSide = spriteRect.Right + gap;
            bool leftFits = leftSide >= area.Left + 4;
            bool rightFits = rightSide + visW <= area.Right - 4;
            bool aboveFits = spriteRect.Top - gap - visH >= area.Top + 4;

            double cardLeft;
            double cardTop;
            if ((preferLeft && leftFits) || (!preferLeft && rightFits) || leftFits || rightFits)
            {
                cardLeft = preferLeft
                    ? (leftFits ? leftSide : rightSide)
                    : (rightFits ? rightSide : leftSide);
                cardTop = spriteRect.Top + Math.Max(0.0, (spriteRect.Height - visH) * 0.14);
            }
            else if (aboveFits)
            {
                cardLeft = spriteCenterX - visW * 0.5;
                cardTop = spriteRect.Top - gap - visH;
            }
            else
            {
                cardLeft = spriteCenterX - visW * 0.5;
                cardTop = area.Top + 6;
            }

            cardLeft = Math.Clamp(cardLeft, area.Left + 4, Math.Max(area.Left + 4, area.Right - visW - 4));
            cardTop = Math.Clamp(cardTop, area.Top + 4, Math.Max(area.Top + 4, area.Bottom - visH - 4));

            Left = cardLeft - ShadowMarginDip;
            Top = cardTop - ShadowMarginDip;
        }
        else
        {
            var area = GetWorkAreaDip(SystemParameters.PrimaryScreenWidth * 0.85, SystemParameters.PrimaryScreenHeight * 0.85, sx, sy);
            Left = Math.Max(area.Left + 8 - ShadowMarginDip, area.Right - visW - 12 - ShadowMarginDip);
            Top = Math.Max(area.Top + 8 - ShadowMarginDip, area.Bottom - visH - 12 - ShadowMarginDip);
        }
    }

    private (double sx, double sy) GetDpiScale()
    {
        var visual = (_anchorPet != null && PresentationSource.FromVisual(_anchorPet) != null)
            ? (Visual)_anchorPet
            : this;
        var dpi = VisualTreeHelper.GetDpi(visual);
        return (dpi.DpiScaleX > 0 ? dpi.DpiScaleX : 1.0, dpi.DpiScaleY > 0 ? dpi.DpiScaleY : 1.0);
    }

    private static Rect GetWorkAreaDip(double centerDipX, double centerDipY, double sx, double sy)
    {
        var pt = new System.Drawing.Point((int)Math.Round(centerDipX * sx), (int)Math.Round(centerDipY * sy));
        var screen = FormsScreen.FromPoint(pt);
        var wa = screen.WorkingArea;
        return new Rect(wa.Left / sx, wa.Top / sy, wa.Width / sx, wa.Height / sy);
    }

    private static string FormatGroupTitle(string name) => name switch
    {
        "Gemini Models" => "Gemini 模型组",
        "Claude and GPT models" => "Claude / GPT 模型组",
        "Gemini 4 Argon" => "Gemini 4 Argon",
        _ => name
    };

    private static string FormatWindowLabel(QuotaBucketInfo b)
    {
        if (b.Window.Equals("5h", StringComparison.OrdinalIgnoreCase))
            return "5小时";
        if (b.Window.Equals("weekly", StringComparison.OrdinalIgnoreCase) && !b.Name.Contains("Argon", StringComparison.OrdinalIgnoreCase))
            return "一周";
        return b.Name;
    }

    private static string FormatShortResetLine(QuotaBucketInfo b)
    {
        if (!b.ResetTime.HasValue)
            return b.Description ?? string.Empty;

        var localReset = b.ResetTime.Value.ToLocalTime();
        return $"重置: {localReset:M月d日 HH:mm}";
    }

    private static string BuildBucketTooltip(QuotaBucketInfo b, string? groupDescription)
    {
        string countdown = string.Empty;
        if (b.ResetTime.HasValue)
        {
            var localReset = b.ResetTime.Value.ToLocalTime();
            var span = localReset - DateTimeOffset.Now;
            if (span <= TimeSpan.Zero)
                countdown = $"即将刷新 ({localReset:yyyy-MM-dd HH:mm})";
            else if (span.TotalDays >= 1)
                countdown = $"{(int)span.TotalDays} 天 {span.Hours} 小时后重置 ({localReset:yyyy-MM-dd HH:mm})";
            else if (span.TotalHours >= 1)
                countdown = $"{(int)span.TotalHours} 小时 {span.Minutes} 分钟后重置 ({localReset:yyyy-MM-dd HH:mm})";
            else
                countdown = $"{Math.Max(1, (int)Math.Ceiling(span.TotalMinutes))} 分钟后重置 ({localReset:yyyy-MM-dd HH:mm})";
        }

        if (string.IsNullOrWhiteSpace(groupDescription))
            return string.IsNullOrWhiteSpace(countdown) ? b.Name : $"{b.Name}\n{countdown}";

        return string.IsNullOrWhiteSpace(countdown)
            ? $"{b.Name}\n{groupDescription}"
            : $"{b.Name}\n{countdown}\n{groupDescription}";
    }

    private static SolidColorBrush BrushFromHex(string hex)
        => new((Color)ColorConverter.ConvertFromString(hex));

    private static LinearGradientBrush CreateHorizontalGradient(string hex0, string hex1, string hex2)
    {
        var brush = new LinearGradientBrush
        {
            StartPoint = new System.Windows.Point(0, 0.5),
            EndPoint = new System.Windows.Point(1, 0.5)
        };
        brush.GradientStops.Add(new GradientStop((Color)ColorConverter.ConvertFromString(hex0), 0.0));
        brush.GradientStops.Add(new GradientStop((Color)ColorConverter.ConvertFromString(hex1), 0.55));
        brush.GradientStops.Add(new GradientStop((Color)ColorConverter.ConvertFromString(hex2), 1.0));
        return brush;
    }

    private void CardBorder_MouseLeftButtonDown(object sender, MouseButtonEventArgs e)
    {
        if (e.OriginalSource is DependencyObject dep && FindParent<System.Windows.Controls.Primitives.ButtonBase>(dep) != null)
            return;

        if (e.ButtonState == MouseButtonState.Pressed)
        {
            _userDragged = true;
            try { DragMove(); } catch { }
        }
    }

    private static T? FindParent<T>(DependencyObject? current) where T : DependencyObject
    {
        while (current != null)
        {
            if (current is T match)
                return match;
            current = VisualTreeHelper.GetParent(current);
        }
        return null;
    }

    private async void RefreshBtn_Click(object sender, RoutedEventArgs e)
        => await RefreshQuotaAsync();

    private void CopyBtn_Click(object sender, RoutedEventArgs e)
    {
        if (_lastSnapshot == null || !_lastSnapshot.Success)
            return;

        try
        {
            System.Windows.Clipboard.SetText(_lastSnapshot.FormatClipboardText());
            CopyBtn.Content = "✓ 已复制";
            Task.Delay(1500).ContinueWith(_ =>
            {
                try
                {
                    if (!Dispatcher.HasShutdownStarted)
                        Dispatcher.Invoke(() => { if (IsLoaded) CopyBtn.Content = "复制"; });
                }
                catch
                {
                }
            });
        }
        catch
        {
        }
    }

    private void CloseBtn_Click(object sender, RoutedEventArgs e) => Close();
}
