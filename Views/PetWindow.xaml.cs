using System.Runtime.InteropServices;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Input;
using System.Windows.Interop;
using System.Windows.Media;
using System.Windows.Shapes;
using System.Windows.Threading;
using GeminiDesktopPet.Models;
using GeminiDesktopPet.Services;
using Forms = System.Windows.Forms;
using Color = System.Windows.Media.Color;
using ColorConverter = System.Windows.Media.ColorConverter;
using HorizontalAlignment = System.Windows.HorizontalAlignment;
using KeyEventArgs = System.Windows.Input.KeyEventArgs;
using MenuItem = System.Windows.Controls.MenuItem;
using MouseEventArgs = System.Windows.Input.MouseEventArgs;
using Point = System.Windows.Point;

namespace GeminiDesktopPet.Views;

public partial class PetWindow : Window
{
    private const int GwlExStyle = -20;
    private const int WsExTransparent = 0x00000020;
    private const int WsExToolWindow = 0x00000080;

    [DllImport("user32.dll")]
    private static extern int GetWindowLong(IntPtr hWnd, int nIndex);

    [DllImport("user32.dll")]
    private static extern int SetWindowLong(IntPtr hWnd, int nIndex, int dwNewLong);

    private sealed class Particle
    {
        public required UIElement Visual { get; init; }
        public double X { get; set; }
        public double Y { get; set; }
        public double Vx { get; set; }
        public double Vy { get; set; }
        public double Life { get; set; }
        public double MaxLife { get; set; }
        public double Spin { get; set; }
        public double Angle { get; set; }
    }

    private readonly PetSettings _settings;
    private readonly SettingsService _settingsService;
    private readonly SpriteManager _sprites;
    private readonly SoundService _sound;
    private readonly SystemMonitorService _sysMonitor;
    private readonly GeminiChatService _chat;

    private readonly DispatcherTimer _frameTimer;
    private readonly DispatcherTimer _aiTimer;
    private readonly DispatcherTimer _bubbleTimer;
    private readonly DispatcherTimer _monitorTimer;
    private readonly DispatcherTimer _hoverGraceTimer;

    private readonly List<Particle> _particles = [];
    private double _animTime;
    private DateTime _lastTickUtc = DateTime.UtcNow;

    // Sprite & behavioral state
    private PetState _currentState = PetState.Idle;
    private PetState? _overridePose;
    private DateTime _overrideUntilUtc = DateTime.MinValue;
    private bool _isWalking;
    private int _walkDir = 1; // 1 = right, -1 = left
    private bool _isSleeping;
    private bool _isSulk;
    private int _sulkPatsNeeded;

    // Drag, shake, and gravity physics
    private bool _mouseDown;
    private bool _isDragging;
    private Point _dragStartScreenPx;
    private double _winStartLeft;
    private double _winStartTop;
    private Point _lastMoveScreenPx;
    private DateTime _lastMoveUtc;
    private double _prevVx;
    private double _shakeEnergy;
    private bool _isFalling;
    private double _fallVelocityY;
    private double _landingSquash;

    // Headpat tracking
    private Point _lastPatLocalPos;
    private double _patTravel;
    private DateTime _lastPatSoundUtc = DateTime.MinValue;

    // Hover / Chat UI state
    private bool _isHovering;
    private bool _isBubbleHovered;
    private bool _isChatOpen;
    private DateTime _lastMonitorBubbleUtc = DateTime.MinValue;

    // Events handled by App coordinator
    public event Action? OpenProfileRequested;
    public event Action? OpenCharacterEditorRequested;
    public event Action? OpenSettingsRequested;
    public event Action? OpenQuotaRequested;
    public event Action? OpenUpdateRequested;
    public event Action? StandbyRequested;
    public event Action<bool>? FollowAntigravityChanged;
    public event Action<bool>? FollowGeminiAppChanged;
    public event Action? SettingsSynced;
    public event Action? ExitRequested;

    public PetWindow(
        PetSettings settings,
        SettingsService settingsService,
        SpriteManager sprites,
        SoundService sound,
        SystemMonitorService sysMonitor,
        GeminiChatService chat)
    {
        InitializeComponent();
        _settings = settings;
        _settingsService = settingsService;
        _sprites = sprites;
        _sound = sound;
        _sysMonitor = sysMonitor;
        _chat = chat;

        BuildCostumeMenu();
        InitializeWalkRigs();

        _frameTimer = new DispatcherTimer(DispatcherPriority.Render)
        {
            Interval = TimeSpan.FromMilliseconds(16)
        };
        _frameTimer.Tick += FrameTimer_Tick;

        _aiTimer = new DispatcherTimer
        {
            Interval = TimeSpan.FromSeconds(4.5)
        };
        _aiTimer.Tick += AiTimer_Tick;

        _bubbleTimer = new DispatcherTimer
        {
            Interval = TimeSpan.FromSeconds(5.5)
        };
        _bubbleTimer.Tick += (_, _) =>
        {
            if (!_isChatOpen && !_isBubbleHovered)
                SpeechBubble.Visibility = Visibility.Collapsed;
            _bubbleTimer.Stop();
        };

        _monitorTimer = new DispatcherTimer
        {
            Interval = TimeSpan.FromSeconds(30)
        };
        _monitorTimer.Tick += MonitorTimer_Tick;

        _hoverGraceTimer = new DispatcherTimer
        {
            Interval = TimeSpan.FromMilliseconds(380)
        };
        _hoverGraceTimer.Tick += (_, _) =>
        {
            _hoverGraceTimer.Stop();
            if (!_isHovering && !_isChatOpen && !_isBubbleHovered)
                QuickBar.Visibility = Visibility.Collapsed;
        };

        Loaded += PetWindow_Loaded;
        Closing += PetWindow_Closing;
    }

    private void PetWindow_Loaded(object sender, RoutedEventArgs e)
    {
        SyncCharacterProfile(CharacterLore.ActiveProfile);
        ApplySettingsToWindow();
        RestoreOrInitPosition();
        _sysMonitor.Sample(); // prime baseline

        // Show time-of-day or festival greeting on launch
        var greetState = CharacterLore.DetectFestivalState(DateTime.Now)
                         ?? CharacterLore.GetTimeGreetingState(DateTime.Now);
        SetTemporaryPose(greetState, TimeSpan.FromSeconds(5.5), CharacterLore.PickLineForState(greetState));
        _sound.Play(PetSoundEffect.Sparkle);
        SpawnStarBurst(Width * 0.5, Height * 0.65, 8);

        _lastTickUtc = DateTime.UtcNow;
        _frameTimer.Start();
        _aiTimer.Start();
        _monitorTimer.Start();
    }

    protected override void OnSourceInitialized(EventArgs e)
    {
        base.OnSourceInitialized(e);
        ApplyWin32ExtendedStyles();
    }

    private void PetWindow_Closing(object? sender, System.ComponentModel.CancelEventArgs e)
    {
        _settings.LastLeft = Left;
        _settings.LastTop = Top;
        _settingsService.Save(_settings);
    }

    protected override void OnClosed(EventArgs e)
    {
        _frameTimer.Stop();
        _aiTimer.Stop();
        _bubbleTimer.Stop();
        _monitorTimer.Stop();
        _hoverGraceTimer.Stop();
        base.OnClosed(e);
    }

    public void ApplySettingsToWindow()
    {
        double oldBottom = IsLoaded ? Top + Height : double.NaN;

        double s = Math.Clamp(_settings.Scale, 0.5, 2.0);
        double baseSpriteH = _settings.StyleMode == PetStyleMode.Standard ? 300.0 : 240.0;
        double spriteH = baseSpriteH * s;
        PetImage.Height = spriteH;

        ChibiWalkRig.Height = 240.0 * s;
        ChibiWalkRig.Width = ChibiWalkRig.Height * (555.0 / 640.0);
        StdWalkRig.Height = 300.0 * s;
        StdWalkRig.Width = StdWalkRig.Height * (337.0 / 977.0);

        Width = Math.Max(360.0, 380.0 * s);
        Height = Math.Max(380.0, spriteH + 200.0);

        if (!double.IsNaN(oldBottom))
        {
            if (_settings.DockedSide == DockSide.None)
            {
                Top = oldBottom - Height;
                ClampInsideNearestWorkArea();
            }
            else
            {
                ReanchorDockedPosition();
            }
        }

        Opacity = Math.Clamp(_settings.Opacity, 0.35, 1.0);
        Topmost = _settings.AlwaysOnTop;
        _sound.Enabled = _settings.SoundEnabled;

        bool pinned = !_settings.AutoWalk;
        if (pinned && _isWalking)
            _isWalking = false;

        MenuStyleChibi.IsChecked = _settings.StyleMode == PetStyleMode.Chibi;
        MenuStyleStandard.IsChecked = _settings.StyleMode == PetStyleMode.Standard;
        MenuAntiGravity.IsChecked = _settings.AntiGravityMode;
        MenuPinOnDesktop.IsChecked = pinned;
        MenuShowSpeechBubble.IsChecked = _settings.ShowSpeechBubble;
        MenuFollowAntigravity.IsChecked = _settings.FollowAntigravity;
        MenuFollowGeminiApp.IsChecked = _settings.FollowGeminiApp;

        QuickStyleBtn.Content = "🎀 切换形象";
        QuickStyleBtn.ToolTip = _settings.StyleMode == PetStyleMode.Standard
            ? "当前：标准立绘版（点击切换至 Q 版）"
            : "当前：Q 版猫娘（点击切换至标准版）";
        QuickFloatBtn.Content = _settings.AntiGravityMode ? "🌟 已悬浮" : "✨ 悬浮";
        QuickPinBtn.Content = pinned ? "📌 已固定" : "📌 固定(不乱跑)";
        QuickPinBtn.ToolTip = pinned
            ? "已固定在桌面（点击恢复漫步）"
            : "固定在桌面不乱跑";

        BondBadgeText.Text = $" · ♥ {_settings.Favorability}";
        if (!_settings.ShowSpeechBubble && !_isChatOpen)
        {
            _bubbleTimer.Stop();
            SpeechBubble.Visibility = Visibility.Collapsed;
        }
        UpdateDockAlignment();
        ApplyWin32ExtendedStyles();
        RefreshSpriteVisual();
    }

    private void ApplyWin32ExtendedStyles()
    {
        var hwnd = new WindowInteropHelper(this).Handle;
        if (hwnd == IntPtr.Zero) return;

        int exStyle = GetWindowLong(hwnd, GwlExStyle);
        exStyle |= WsExToolWindow; // Keep hidden from Alt+Tab clutter
        if (_settings.ClickThrough)
            exStyle |= WsExTransparent;
        else
            exStyle &= ~WsExTransparent;

        SetWindowLong(hwnd, GwlExStyle, exStyle);
    }

    private void BuildCostumeMenu()
    {
        CostumeSubMenu.Items.Clear();
        foreach (var opt in CharacterLore.CostumeOptions)
        {
            var item = new MenuItem
            {
                Header = opt.Label,
                Tag = opt.Id,
                IsCheckable = true,
                IsChecked = string.Equals(_settings.CostumeOverride, opt.Id, StringComparison.OrdinalIgnoreCase)
            };
            item.Click += (_, _) =>
            {
                _settings.CostumeOverride = opt.Id;
                _settings.AutoFestivalCostume = opt.Id == "auto";
                foreach (MenuItem mi in CostumeSubMenu.Items)
                    mi.IsChecked = Equals(mi.Tag, opt.Id);
                _settingsService.Save(_settings);
                SettingsSynced?.Invoke();

                var stateToShow = opt.State ?? CharacterLore.DetectFestivalState(DateTime.Now) ?? PetState.Idle;
                SetTemporaryPose(stateToShow, TimeSpan.FromSeconds(5), CharacterLore.PickLineForState(stateToShow));
                SpawnStarBurst(Width * 0.5, Height - PetImage.Height * 0.55, 7);
            };
            CostumeSubMenu.Items.Add(item);
        }
    }

    private void RestoreOrInitPosition()
    {
        if (_settings.LastLeft.HasValue && _settings.LastTop.HasValue)
        {
            Left = _settings.LastLeft.Value;
            Top = _settings.LastTop.Value;
            if (_settings.DockedSide != DockSide.None)
                ReanchorDockedPosition();
            else
                ClampInsideNearestWorkArea();
        }
        else
        {
            MoveToBottomRightCorner();
        }
    }

    public void MoveToBottomRightCorner()
    {
        _settings.DockedSide = DockSide.None;
        _isFalling = false;
        var area = GetWorkAreaDip(Left + Width / 2, Top + Height / 2);
        Left = area.Right - Width - 28;
        Top = area.Bottom - Height;
        UpdateDockAlignment();
        _settingsService.Save(_settings);
    }

    public void ShowAndGreet(string? customMessage = null)
    {
        if (!IsVisible) Show();
        if (_settings.DockedSide != DockSide.None)
        {
            ReanchorDockedPosition();
            var dockPose = _settings.DockedSide == DockSide.Left ? PetState.EdgeLeftPeek : PetState.EdgeRightPeek;
            SetTemporaryPose(dockPose, TimeSpan.FromSeconds(5.0), customMessage ?? CharacterLore.PickLine("edge"));
        }
        else
        {
            ClampInsideNearestWorkArea();
            var greet = CharacterLore.GetTimeGreetingState(DateTime.Now);
            SetTemporaryPose(greet, TimeSpan.FromSeconds(5.0), customMessage ?? CharacterLore.PickLineForState(greet));
        }
        _sound.Play(PetSoundEffect.Sparkle);
        SpawnStarBurst(Width * 0.5, Height - PetImage.Height * 0.55, 7);
    }

    public void TriggerExternalSpritePreview(string spriteKey)
    {
        if (Enum.TryParse<PetState>(spriteKey.Replace("_", "").Replace("-", ""), true, out var parsed))
        {
            SetTemporaryPose(parsed, TimeSpan.FromSeconds(4.5), CharacterLore.PickLineForState(parsed));
        }
        else
        {
            _overridePose = null;
            PetImage.Source = _sprites.GetSprite(spriteKey);
            ShowBubble($"动作预览：{spriteKey} ✦", TimeSpan.FromSeconds(4));
        }
        _sound.Play(PetSoundEffect.Chime);
        SpawnStarBurst(Width * 0.5, Height - PetImage.Height * 0.5, 5);
    }

    private void FrameTimer_Tick(object? sender, EventArgs e)
    {
        var now = DateTime.UtcNow;
        double dt = Math.Clamp((now - _lastTickUtc).TotalSeconds, 0.001, 0.05);
        _lastTickUtc = now;
        _animTime += dt;

        // Decay shake energy gently when not rapidly shaking
        if (_shakeEnergy > 0)
            _shakeEnergy = Math.Max(0, _shakeEnergy - dt * 1.4);

        // Expire temporary override
        if (_overridePose.HasValue && now >= _overrideUntilUtc)
        {
            _overridePose = null;
            if (_isSulk)
                _isSulk = false;
            RefreshSpriteVisual();
        }

        // Gravity fall physics
        if (_isFalling && !_isDragging && !_settings.AntiGravityMode && _settings.DockedSide == DockSide.None)
        {
            var area = GetWorkAreaDip(Left + Width * 0.5, Top + Height * 0.5);
            double floorTop = area.Bottom - Height;

            _fallVelocityY += 2300.0 * dt;
            Top += _fallVelocityY * dt;

            if (Top >= floorTop)
            {
                Top = floorTop;
                _isFalling = false;
                _fallVelocityY = 0;
                _landingSquash = 1.0;
                _sound.Play(PetSoundEffect.Boing);
            }
        }

        // Autonomous horizontal walking along taskbar
        if (_isWalking && !_isDragging && !_isFalling && _settings.DockedSide == DockSide.None)
        {
            var area = GetWorkAreaDip(Left + Width * 0.5, Top + Height * 0.5);
            double spriteHalf = SpriteContainer.ActualWidth > 10 ? SpriteContainer.ActualWidth * 0.5 : 85 * _settings.Scale;
            double minLeft = area.Left - (Width * 0.5 - spriteHalf) + 8;
            double maxLeft = area.Right - (Width * 0.5 + spriteHalf) - 8;

            double speed = 72.0 * _settings.Scale;
            Left += _walkDir * speed * dt;

            if (Left <= minLeft)
            {
                Left = minLeft;
                _walkDir = 1;
            }
            else if (Left >= maxLeft)
            {
                Left = maxLeft;
                _walkDir = -1;
            }
        }

        // Recover landing squash spring
        if (_landingSquash > 0.001)
            _landingSquash = Math.Max(0, _landingSquash - dt * 4.5);

        // Apply smooth sprite transforms
        UpdateSpriteTransforms();

        // Update particles
        UpdateParticles(dt);
    }

    private void UpdateSpriteTransforms()
    {
        double scaleX = 1.0;
        double scaleY = 1.0;
        double angle = 0;
        double transX = 0;
        double transY = 0;

        if (_settings.DockedSide != DockSide.None)
        {
            // Gentle breathing sway glued to monitor edge
            scaleY = 1.0 + 0.011 * Math.Sin(_animTime * 2.5);
            angle = (_settings.DockedSide == DockSide.Left ? 1.0 : -1.0) * 0.8 * Math.Sin(_animTime * 2.1);
        }
        else if (_isDragging)
        {
            angle = Math.Clamp(_prevVx * 0.012, -16, 16);
            scaleY = 1.02;
            if (_shakeEnergy > 1.8)
                transX = Math.Sin(_animTime * 48) * 4.0;
        }
        else if (_isWalking)
        {
            scaleX = _walkDir > 0 ? -1.0 : 1.0;
            double s = Math.Clamp(_settings.Scale, 0.5, 2.0);
            if (_settings.StyleMode == PetStyleMode.Standard && StdBodyImg.Source != null)
            {
                // Articulated human stride kinematics for Standard proportion (feet grounded cleanly)
                double p = _animTime * 6.8;
                transY = 0.0;
                angle = 0.0;
                UpdateStandardWalkRigTransforms(p, s);
            }
            else if (ChibiBodyImg.Source != null)
            {
                // Cheerful directional 3/4 side-walk kinematics for Chibi proportion
                double p = _animTime * 7.6;
                transY = 0.0;
                angle = 0.0;
                UpdateChibiWalkRigTransforms(p, s);
            }
            else
            {
                transY = -Math.Abs(Math.Sin(_animTime * 11.0)) * 7.0;
                angle = Math.Sin(_animTime * 11.0) * 2.8 * _walkDir;
            }
        }
        else if (_settings.AntiGravityMode || _currentState == PetState.Float)
        {
            // Antigravity levitation sine wave!
            transY = -16.0 + 8.5 * Math.Sin(_animTime * 2.1);
            angle = 3.2 * Math.Sin(_animTime * 1.4);
            scaleY = 1.0 + 0.015 * Math.Sin(_animTime * 2.1 + 0.8);

            if (_settings.ParticlesEnabled && Random.Shared.NextDouble() < 0.08)
                SpawnParticle("✦", "#A78BFA", Width * 0.5 + (Random.Shared.NextDouble() - 0.5) * 140, Height - 40 - Random.Shared.NextDouble() * 140, 0, -28, 1.3);
        }
        else if (_isSleeping)
        {
            scaleY = 1.0 + 0.024 * Math.Sin(_animTime * 1.4);
            if (_settings.ParticlesEnabled && Random.Shared.NextDouble() < 0.03)
                SpawnParticle("Zzz", "#93C5FD", Width * 0.58, Height - PetImage.Height * 0.78, 12, -22, 1.8);
        }
        else
        {
            // Soft organic breathing
            scaleY = 1.0 + 0.014 * Math.Sin(_animTime * 2.4);
            scaleX = 1.0 - 0.006 * Math.Sin(_animTime * 2.4);
        }

        if (_landingSquash > 0.001)
        {
            double s = Math.Sin(_landingSquash * Math.PI);
            scaleY *= 1.0 - 0.14 * s;
            scaleX *= 1.0 + 0.12 * s;
        }

        SpriteScale.ScaleX = scaleX;
        SpriteScale.ScaleY = scaleY;
        SpriteRotate.Angle = angle;
        SpriteTranslate.X = transX;
        SpriteTranslate.Y = transY;
    }

    private void AiTimer_Tick(object? sender, EventArgs e)
    {
        if (_mouseDown || _isDragging || _isFalling || _overridePose.HasValue || _isSulk || _isChatOpen)
            return;

        // Docked edge behavior: occasional peek!
        if (_settings.DockedSide != DockSide.None)
        {
            if (!_isHovering && Random.Shared.NextDouble() < 0.35)
            {
                var peek = _settings.DockedSide == DockSide.Left ? PetState.EdgeLeftPeek : PetState.EdgeRightPeek;
                SetTemporaryPose(peek, TimeSpan.FromSeconds(3.2), Random.Shared.NextDouble() < 0.4 ? CharacterLore.PickLine("edge") : null);
            }
            return;
        }

        // Stop walking after one tick burst
        if (_isWalking)
        {
            _isWalking = false;
            _currentState = ResolveRestingState();
            RefreshSpriteVisual();
            return;
        }

        // Late-night sleep chance (00:00 - 05:00)
        if (!_settings.AntiGravityMode && DateTime.Now.Hour < 5 && Random.Shared.NextDouble() < 0.28)
        {
            _isSleeping = true;
            _currentState = PetState.Sleeping;
            RefreshSpriteVisual();
            return;
        }

        _isSleeping = false;
        double roll = Random.Shared.NextDouble();

        if (_settings.AutoWalk && !_settings.AntiGravityMode && roll < 0.36)
        {
            _isWalking = true;
            _walkDir = Random.Shared.Next(2) == 0 ? 1 : -1;
            _currentState = PetState.Running;
            RefreshSpriteVisual();
            if (Random.Shared.NextDouble() < 0.25)
                ShowBubble(CharacterLore.PickLine("running"), TimeSpan.FromSeconds(2.5));
        }
        else if (roll < 0.62)
        {
            PetState[] quirks = [PetState.Idle2, PetState.Idle3, PetState.Hum, PetState.Stretch, PetState.Yawn, PetState.Thinking, PetState.Drawing];
            var pick = quirks[Random.Shared.Next(quirks.Length)];
            SetTemporaryPose(pick, TimeSpan.FromSeconds(4.2), Random.Shared.NextDouble() < 0.3 ? CharacterLore.PickLineForState(pick) : null);
        }
        else if (roll < 0.72 && !_settings.AntiGravityMode)
        {
            SetTemporaryPose(PetState.Float, TimeSpan.FromSeconds(4.5), CharacterLore.PickLine("float"));
        }
        else
        {
            _currentState = ResolveRestingState();
            RefreshSpriteVisual();
            if (Random.Shared.NextDouble() < 0.12)
                ShowBubble(CharacterLore.PickLine("chatter"), TimeSpan.FromSeconds(5));
        }
    }

    private void MonitorTimer_Tick(object? sender, EventArgs e)
    {
        if (!_settings.SystemMonitorEnabled || _isDragging || _isSulk)
            return;

        var snap = _sysMonitor.Sample();
        var now = DateTime.UtcNow;
        if ((now - _lastMonitorBubbleUtc).TotalMinutes < 3)
            return;

        if (snap.HasBattery && !snap.IsCharging && snap.BatteryPercent <= 20)
        {
            _lastMonitorBubbleUtc = now;
            SetTemporaryPose(PetState.Tired, TimeSpan.FromSeconds(5.5), $"电量只剩 {snap.BatteryPercent}% 了喵…快充电呀！");
        }
        else if (snap.CpuPercent >= 88)
        {
            _lastMonitorBubbleUtc = now;
            var swamped = Random.Shared.Next(2) == 0 ? PetState.SwampedLeft : PetState.SwampedRight;
            SetTemporaryPose(swamped, TimeSpan.FromSeconds(5.0), $"CPU 飙到 {snap.CpuPercent:F0}% 了！电脑要烫爪子了喵！");
        }
    }

    private PetState ResolveRestingState()
    {
        if (_settings.DockedSide == DockSide.Left)
            return _isHovering ? PetState.EdgeLeftPeek : PetState.EdgeLeft;
        if (_settings.DockedSide == DockSide.Right)
            return _isHovering ? PetState.EdgeRightPeek : PetState.EdgeRight;

        if (_settings.AntiGravityMode)
            return PetState.Float;

        if (!string.Equals(_settings.CostumeOverride, "auto", StringComparison.OrdinalIgnoreCase))
        {
            var match = CharacterLore.CostumeOptions.FirstOrDefault(c => c.Id == _settings.CostumeOverride);
            if (match.State.HasValue)
                return match.State.Value;
        }
        else if (_settings.AutoFestivalCostume)
        {
            var fest = CharacterLore.DetectFestivalState(DateTime.Now);
            if (fest.HasValue)
                return fest.Value;
        }

        return PetState.Idle;
    }

    private void SetTemporaryPose(PetState state, TimeSpan duration, string? bubbleMessage = null)
    {
        _isWalking = false;
        _isSleeping = state == PetState.Sleeping;
        _overridePose = state;
        _overrideUntilUtc = DateTime.UtcNow + duration;
        RefreshSpriteVisual();

        if (!string.IsNullOrWhiteSpace(bubbleMessage))
            ShowBubble(bubbleMessage, duration);
    }

    private void InitializeWalkRigs()
    {
        ChibiArmLImg.Source = _sprites.GetWalkRigPart("chibi_arm_l.png");
        ChibiArmRImg.Source = _sprites.GetWalkRigPart("chibi_arm_r.png");
        ChibiLegLImg.Source = _sprites.GetWalkRigPart("chibi_leg_l.png");
        ChibiLegRImg.Source = _sprites.GetWalkRigPart("chibi_leg_r.png");
        ChibiBodyImg.Source = _sprites.GetWalkRigPart("chibi_body.png");

        StdBackThighImg.Source = _sprites.GetWalkRigPart("std_thigh_back.png") ?? _sprites.GetWalkRigPart("std_thigh.png");
        StdBackShinImg.Source = _sprites.GetWalkRigPart("std_shin_back.png") ?? _sprites.GetWalkRigPart("std_shin.png");
        StdFrontThighImg.Source = _sprites.GetWalkRigPart("std_thigh.png");
        StdFrontShinImg.Source = _sprites.GetWalkRigPart("std_shin.png");
        StdBodyImg.Source = _sprites.GetWalkRigPart("std_body.png");
    }

    private void UpdateChibiWalkRigTransforms(double p, double scale)
    {
        double stride = Math.Sin(p);
        double liftL = Math.Max(0.0, Math.Cos(p));
        double liftR = Math.Max(0.0, -Math.Cos(p));

        // Left/front leg is drawn ~+10° forward; right/back leg ~-14° backward.
        // Centering rotation around neutral vertical axis produces symmetric natural stride.
        ChibiLegLRotate.Angle = -6.0 + 16.0 * stride;
        ChibiLegLMove.Y = -liftL * 4.5 * scale;
        ChibiLegLMove.X = 0.0;
        ChibiLegLScale.ScaleY = 1.0;

        ChibiLegRRotate.Angle = 8.0 - 16.0 * stride;
        ChibiLegRMove.Y = -liftR * 4.5 * scale;
        ChibiLegRMove.X = 0.0;
        ChibiLegRScale.ScaleY = 1.0;

        // Contralateral arm swing: left arm counter-balances left leg, right arm counter-balances right leg
        ChibiArmLRotate.Angle = -6.0 - 15.0 * stride;
        ChibiArmRRotate.Angle = 4.0 + 15.0 * stride;

        // Natural double-step weight bounce + subtle forward lean matching walking head direction
        ChibiBodyMove.Y = -3.2 * (1.0 - Math.Cos(2.0 * p)) * 0.5 * scale;
        ChibiBodyRotate.Angle = -1.8 + 1.3 * Math.Sin(2.0 * p);
    }

    private void UpdateStandardWalkRigTransforms(double p, double scale)
    {
        double frontPhase = p;
        double backPhase = p + Math.PI;

        StdFrontHipRotate.Angle = 17.5 * Math.Sin(frontPhase);
        StdFrontKneeRotate.Angle = -32.0 * Math.Max(0.0, Math.Cos(frontPhase - 0.1))
                                 - 5.0 * Math.Max(0.0, Math.Sin(frontPhase));

        StdBackHipRotate.Angle = 17.5 * Math.Sin(backPhase);
        StdBackKneeRotate.Angle = -32.0 * Math.Max(0.0, Math.Cos(backPhase - 0.1))
                                - 5.0 * Math.Max(0.0, Math.Sin(backPhase));

        StdBodyMove.Y = -2.8 * (1.0 - Math.Cos(2.0 * p)) * 0.5 * scale;
        StdBodyRotate.Angle = -2.2 + 1.1 * Math.Sin(2.0 * p);
    }

    private void RefreshSpriteVisual()
    {
        PetState active = _overridePose ?? _currentState;
        if (!_overridePose.HasValue && !_isWalking && !_isSleeping && !_isDragging && !_isSulk)
            active = ResolveRestingState();

        bool useWalkRig = _isWalking
            && !_overridePose.HasValue
            && !_isDragging
            && _settings.DockedSide == DockSide.None;

        if (useWalkRig && _settings.StyleMode == PetStyleMode.Standard && StdBodyImg.Source != null)
        {
            PetImage.Visibility = Visibility.Collapsed;
            ChibiWalkRig.Visibility = Visibility.Collapsed;
            StdWalkRig.Visibility = Visibility.Visible;
            return;
        }

        if (useWalkRig && _settings.StyleMode == PetStyleMode.Chibi && ChibiBodyImg.Source != null)
        {
            PetImage.Visibility = Visibility.Collapsed;
            StdWalkRig.Visibility = Visibility.Collapsed;
            ChibiWalkRig.Visibility = Visibility.Visible;
            return;
        }

        ChibiWalkRig.Visibility = Visibility.Collapsed;
        StdWalkRig.Visibility = Visibility.Collapsed;
        PetImage.Visibility = Visibility.Visible;
        PetImage.Source = _sprites.GetSprite(active, _settings.StyleMode);
    }

    private void UpdateDockAlignment()
    {
        var align = _settings.DockedSide switch
        {
            DockSide.Left => HorizontalAlignment.Left,
            DockSide.Right => HorizontalAlignment.Right,
            _ => HorizontalAlignment.Center
        };

        PetImage.HorizontalAlignment = align;
        SpriteContainer.HorizontalAlignment = align;
        StageTopPanel.HorizontalAlignment = align;
        SpriteContainer.RenderTransformOrigin = _settings.DockedSide switch
        {
            DockSide.Left => new Point(0.0, 1.0),
            DockSide.Right => new Point(1.0, 1.0),
            _ => new Point(0.5, 1.0)
        };
    }

    private void ReanchorDockedPosition()
    {
        if (_settings.DockedSide == DockSide.None) return;

        var area = GetWorkAreaDip(Left + Width * 0.5, Top + Height * 0.5);
        if (_settings.DockedSide == DockSide.Left)
            Left = area.Left;
        else if (_settings.DockedSide == DockSide.Right)
            Left = area.Right - Width;

        Top = Math.Clamp(Top, area.Top, Math.Max(area.Top, area.Bottom - Height));
    }

    public void ConfigureSmokePreview(PetStyleMode styleMode, bool walking, double animTimeSec, DockSide dockSide, bool showQuickBar = false)
    {
        _overridePose = null;
        _isDragging = false;
        _isFalling = false;
        _isSleeping = false;
        _isSulk = false;
        _landingSquash = 0;

        _settings.StyleMode = styleMode;
        _settings.DockedSide = dockSide;
        if (dockSide != DockSide.None)
            _settings.AntiGravityMode = false;

        _isWalking = walking && dockSide == DockSide.None;
        _walkDir = -1;
        _animTime = animTimeSec;
        _currentState = dockSide switch
        {
            DockSide.Left => PetState.EdgeLeftPeek,
            DockSide.Right => PetState.EdgeRightPeek,
            _ when _isWalking => PetState.Running,
            _ => PetState.Idle
        };

        ApplySettingsToWindow();
        if (dockSide != DockSide.None)
            ReanchorDockedPosition();

        QuickBar.Visibility = showQuickBar ? Visibility.Visible : Visibility.Collapsed;
        UpdateSpriteTransforms();
        UpdateLayout();
    }

    // ==================== Mouse, Drag, Shake & Headpat ====================

    private void PetImage_MouseLeftButtonDown(object sender, MouseButtonEventArgs e)
    {
        if (e.ClickCount >= 2)
        {
            _mouseDown = false;
            _isDragging = false;
            _isWalking = false;
            OpenQuotaRequested?.Invoke();
            e.Handled = true;
            return;
        }

        bool wasWalking = _isWalking;
        _mouseDown = true;
        _isDragging = false;
        _isFalling = false;
        _isWalking = false;
        _isSleeping = false;
        _shakeEnergy = 0;
        _prevVx = 0;
        if (wasWalking)
            RefreshSpriteVisual();

        _dragStartScreenPx = PointToScreen(e.GetPosition(this));
        _lastMoveScreenPx = _dragStartScreenPx;
        _lastMoveUtc = DateTime.UtcNow;
        _winStartLeft = Left;
        _winStartTop = Top;

        SpriteContainer.CaptureMouse();
        e.Handled = true;
    }

    private void PetImage_MouseMove(object sender, MouseEventArgs e)
    {
        var localPos = e.GetPosition(SpriteContainer);

        if (_mouseDown)
        {
            Point curScreenPx = PointToScreen(e.GetPosition(this));
            double dxPx = curScreenPx.X - _dragStartScreenPx.X;
            double dyPx = curScreenPx.Y - _dragStartScreenPx.Y;

            if (!_isDragging && MathCompat.Hypot(dxPx, dyPx) > 5.0)
            {
                _isDragging = true;
                if (_settings.DockedSide != DockSide.None)
                {
                    _settings.DockedSide = DockSide.None;
                    UpdateDockAlignment();
                }
                _overridePose = PetState.Float;
                RefreshSpriteVisual();
            }

            if (_isDragging)
            {
                var scaleFactor = GetDpiScale();
                Left = _winStartLeft + dxPx / scaleFactor.x;
                Top = _winStartTop + dyPx / scaleFactor.y;

                // Shake detection
                var now = DateTime.UtcNow;
                double dt = Math.Max(0.005, (now - _lastMoveUtc).TotalSeconds);
                double vx = (curScreenPx.X - _lastMoveScreenPx.X) / scaleFactor.x / dt;
                double vy = (curScreenPx.Y - _lastMoveScreenPx.Y) / scaleFactor.y / dt;
                double speed = MathCompat.Hypot(vx, vy);

                if (speed > 460 && (_prevVx * vx < -12000))
                {
                    _shakeEnergy += 0.65;
                    ReactToShake();
                }

                _prevVx = vx;
                _lastMoveScreenPx = curScreenPx;
                _lastMoveUtc = now;
            }
            return;
        }

        // Hover Headpat detection on upper 48% of sprite (ears, ahoge, hair)
        double hitH = SpriteContainer.ActualHeight > 20 ? SpriteContainer.ActualHeight : PetImage.Height;
        if (_settings.DockedSide == DockSide.None && hitH > 20)
        {
            double relY = localPos.Y / hitH;
            if (relY <= 0.48)
            {
                double dist = MathCompat.Hypot(localPos.X - _lastPatLocalPos.X, localPos.Y - _lastPatLocalPos.Y);
                if (dist < 80)
                    _patTravel += dist;
                _lastPatLocalPos = localPos;

                if (_patTravel >= 60.0)
                {
                    _patTravel = 0;
                    TriggerPat();
                }
            }
            else
            {
                _patTravel = 0;
            }
        }
    }

    private void PetImage_MouseLeftButtonUp(object sender, MouseButtonEventArgs e)
    {
        if (!_mouseDown) return;
        _mouseDown = false;
        SpriteContainer.ReleaseMouseCapture();

        if (!_isDragging)
        {
            // Click interaction!
            var localPos = e.GetPosition(SpriteContainer);
            double relY = SpriteContainer.ActualHeight > 0 ? localPos.Y / SpriteContainer.ActualHeight : 0.5;
            if (relY <= 0.45 && _settings.DockedSide == DockSide.None)
            {
                TriggerPat();
            }
            else if (_settings.DockedSide != DockSide.None)
            {
                var peek = _settings.DockedSide == DockSide.Left ? PetState.EdgeLeftPeek : PetState.EdgeRightPeek;
                SetTemporaryPose(peek, TimeSpan.FromSeconds(3.5), CharacterLore.PickLine("edge"));
                _sound.Play(PetSoundEffect.Chime);
            }
            else if (_isSulk)
            {
                ShowBubble("……哼！（摸摸头才原谅你喵）", TimeSpan.FromSeconds(3.5));
            }
            else
            {
                var pose = Random.Shared.Next(3) switch
                {
                    0 => PetState.Happy,
                    1 => PetState.Asking,
                    _ => PetState.Surprised
                };
                ShowBubble(CharacterLore.PickLine("chatter"), TimeSpan.FromSeconds(4.5));
                SetTemporaryPose(pose, TimeSpan.FromSeconds(2.4));
                _sound.Play(PetSoundEffect.Chime);
            }
            return;
        }

        _isDragging = false;

        // Heavy shake reaction -> Dizzy -> Sulk!
        if (_shakeEnergy >= 2.2)
        {
            _isSulk = true;
            _sulkPatsNeeded = 2;
            SetTemporaryPose(PetState.Dizzy, TimeSpan.FromSeconds(2.6), CharacterLore.PickLine("dizzy"));
            Task.Delay(2600).ContinueWith(_ => Dispatcher.Invoke(() =>
            {
                if (_isSulk && !_isDragging)
                    SetTemporaryPose(PetState.Sulk, TimeSpan.FromSeconds(14), CharacterLore.PickLine("sulk"));
            }));
        }
        else
        {
            _overridePose = null;
            TryEdgeDockOrGravityDrop();
        }

        _settings.LastLeft = Left;
        _settings.LastTop = Top;
        _settingsService.Save(_settings);
    }

    private void PetImage_MouseRightButtonUp(object sender, MouseButtonEventArgs e)
    {
        // Let ContextMenu open naturally
    }

    private void ReactToShake()
    {
        if (_shakeEnergy >= 3.4)
        {
            SetTemporaryPose(PetState.Redhot, TimeSpan.FromSeconds(2.2), CharacterLore.PickLine("redhot"));
            _sound.Play(PetSoundEffect.Angry);
            SpawnParticle("💢", "#F87171", Width * 0.65, Height - PetImage.Height * 0.85, 18, -32, 0.8);
        }
        else if (_shakeEnergy >= 2.1)
        {
            SetTemporaryPose(PetState.Puffed, TimeSpan.FromSeconds(2.0), CharacterLore.PickLine("puffed"));
        }
        else if (_shakeEnergy >= 1.0)
        {
            SetTemporaryPose(PetState.Annoyed, TimeSpan.FromSeconds(1.8), CharacterLore.PickLine("annoyed"));
        }
    }

    public void TriggerPat()
    {
        _settings.TotalPats++;
        _settings.Favorability = Math.Min(9999, _settings.Favorability + 1);
        BondBadgeText.Text = $" · ♥ {_settings.Favorability}";

        if (_isSulk)
        {
            _sulkPatsNeeded--;
            if (_sulkPatsNeeded > 0)
            {
                SetTemporaryPose(PetState.Coaxed, TimeSpan.FromSeconds(4.0), CharacterLore.PickLine("coaxed"));
            }
            else
            {
                _isSulk = false;
                SetTemporaryPose(PetState.Forgiven, TimeSpan.FromSeconds(3.5), CharacterLore.PickLine("forgiven"));
            }
        }
        else
        {
            SetTemporaryPose(PetState.Pat, TimeSpan.FromSeconds(2.8), CharacterLore.PickLine("pat"));
        }

        var now = DateTime.UtcNow;
        if ((now - _lastPatSoundUtc).TotalMilliseconds > 450)
        {
            _lastPatSoundUtc = now;
            _sound.Play(PetSoundEffect.Purr);
            _settingsService.Save(_settings);
        }

        if (_settings.ParticlesEnabled)
        {
            SpawnParticle("♥", "#F472B6", Width * 0.5 + (Random.Shared.NextDouble() - 0.5) * 80, Height - PetImage.Height * 0.88, (Random.Shared.NextDouble() - 0.5) * 30, -45, 1.1);
            SpawnParticle("✦", "#C4B5FD", Width * 0.5 + (Random.Shared.NextDouble() - 0.5) * 90, Height - PetImage.Height * 0.82, (Random.Shared.NextDouble() - 0.5) * 36, -52, 1.0);
        }
    }

    private void TryEdgeDockOrGravityDrop()
    {
        var area = GetWorkAreaDip(Left + Width * 0.5, Top + Height * 0.5);
        double spriteW = PetImage.ActualWidth > 10 ? PetImage.ActualWidth : 180 * _settings.Scale;
        double spriteLeft = Left + (Width - spriteW) * 0.5;
        double spriteRight = spriteLeft + spriteW;
        double snapMargin = 38.0 * Math.Clamp(_settings.Scale, 0.5, 2.0);

        if (_settings.EdgeDockEnabled && (Left <= area.Left + 10 || spriteLeft <= area.Left + snapMargin))
        {
            _settings.DockedSide = DockSide.Left;
            Left = area.Left;
            Top = Math.Clamp(Top, area.Top, area.Bottom - Height);
            UpdateDockAlignment();
            _currentState = PetState.EdgeLeftPeek;
            RefreshSpriteVisual();
            ShowBubble(CharacterLore.PickLine("edge"), TimeSpan.FromSeconds(3.5));
            _sound.Play(PetSoundEffect.Chime);
            return;
        }

        if (_settings.EdgeDockEnabled && (Left + Width >= area.Right - 10 || spriteRight >= area.Right - snapMargin))
        {
            _settings.DockedSide = DockSide.Right;
            Left = area.Right - Width;
            Top = Math.Clamp(Top, area.Top, area.Bottom - Height);
            UpdateDockAlignment();
            _currentState = PetState.EdgeRightPeek;
            RefreshSpriteVisual();
            ShowBubble(CharacterLore.PickLine("edge"), TimeSpan.FromSeconds(3.5));
            _sound.Play(PetSoundEffect.Chime);
            return;
        }

        if (!_settings.AntiGravityMode && _settings.GravityEnabled && Top + Height < area.Bottom - 2)
        {
            _fallVelocityY = 0;
            _isFalling = true;
        }
        else
        {
            ClampInsideNearestWorkArea();
            RefreshSpriteVisual();
        }
    }

    // ==================== Speech Bubble & Chat ====================

    public void ShowBubble(string message, TimeSpan duration, bool force = false)
    {
        if (!force && !_settings.ShowSpeechBubble && !_isChatOpen)
            return;

        BubbleText.Text = message;
        SpeechBubble.Visibility = Visibility.Visible;
        _bubbleTimer.Stop();
        _bubbleTimer.Interval = duration;
        _bubbleTimer.Start();
    }

    public void OpenChatDialog()
    {
        _isChatOpen = true;
        if (SpeechBubble.Visibility != Visibility.Visible)
            BubbleText.Text = "想聊什么呀喵？输入文字或指令试试✦";
        SpeechBubble.Visibility = Visibility.Visible;
        ChatInputPanel.Visibility = Visibility.Visible;
        _bubbleTimer.Stop();
        Activate();
        ChatTextBox.Focus();
        SetTemporaryPose(PetState.Listening, TimeSpan.FromSeconds(10));
    }

    public void SetSpeechBubbleEnabled(bool enabled)
    {
        _settings.ShowSpeechBubble = enabled;
        ApplySettingsToWindow();
        if (enabled)
        {
            ShowBubble("已开启宠物对话气泡，会自动陪你聊天喵✦", TimeSpan.FromSeconds(4), force: true);
        }
        else if (!_isChatOpen)
        {
            ChatInputPanel.Visibility = Visibility.Collapsed;
            SpeechBubble.Visibility = Visibility.Collapsed;
            _bubbleTimer.Stop();
        }
    }

    private void MenuShowSpeechBubble_Click(object sender, RoutedEventArgs e)
    {
        SetSpeechBubbleEnabled(MenuShowSpeechBubble.IsChecked);
        _settingsService.Save(_settings);
        SettingsSynced?.Invoke();
    }

    private void ToggleChat_Click(object sender, RoutedEventArgs e)
    {
        if (!_isChatOpen)
        {
            OpenChatDialog();
        }
        else
        {
            _isChatOpen = false;
            ChatInputPanel.Visibility = Visibility.Collapsed;
            if (_settings.ShowSpeechBubble)
            {
                _bubbleTimer.Interval = TimeSpan.FromSeconds(3);
                _bubbleTimer.Start();
            }
            else
            {
                _bubbleTimer.Stop();
                SpeechBubble.Visibility = Visibility.Collapsed;
            }
        }
    }

    private async void SendChat_Click(object sender, RoutedEventArgs e) => await SubmitChatAsync();

    private async void ChatTextBox_KeyDown(object sender, KeyEventArgs e)
    {
        if (e.Key == Key.Enter)
        {
            e.Handled = true;
            await SubmitChatAsync();
        }
        else if (e.Key == Key.Escape)
        {
            _isChatOpen = false;
            ChatInputPanel.Visibility = Visibility.Collapsed;
            SpeechBubble.Visibility = Visibility.Collapsed;
        }
    }

    private async Task SubmitChatAsync()
    {
        string input = ChatTextBox.Text.Trim();
        if (string.IsNullOrEmpty(input)) return;

        ChatTextBox.Text = string.Empty;
        SetTemporaryPose(PetState.Thinking, TimeSpan.FromSeconds(15), "让我想想喵…✦");

        var health = _sysMonitor.Sample();
        var reply = await _chat.AskAsync(input, _settings, health);

        var targetPose = reply.SuggestedState ?? PetState.Happy;
        if (targetPose == PetState.Float && !_settings.AntiGravityMode)
        {
            _settings.AntiGravityMode = true;
            ApplySettingsToWindow();
            SettingsSynced?.Invoke();
        }
        else if (targetPose == PetState.Pat)
        {
            TriggerPat();
        }

        SetTemporaryPose(targetPose, TimeSpan.FromSeconds(7), reply.Text);
        _sound.Play(PetSoundEffect.Sparkle);
    }

    private void CloseBubble_Click(object sender, RoutedEventArgs e)
    {
        _isChatOpen = false;
        ChatInputPanel.Visibility = Visibility.Collapsed;
        SpeechBubble.Visibility = Visibility.Collapsed;
        _bubbleTimer.Stop();
    }

    private void Window_MouseEnter(object sender, MouseEventArgs e)
    {
        _hoverGraceTimer.Stop();
        _isHovering = true;
        QuickBar.Visibility = Visibility.Visible;
        if (_settings.DockedSide != DockSide.None && !_overridePose.HasValue)
            RefreshSpriteVisual();
    }

    private void Window_MouseLeave(object sender, MouseEventArgs e)
    {
        _isHovering = false;
        _hoverGraceTimer.Stop();
        _hoverGraceTimer.Start();
        if (_settings.DockedSide != DockSide.None && !_overridePose.HasValue)
            RefreshSpriteVisual();
    }

    private void Bubble_MouseEnter(object sender, MouseEventArgs e) => _isBubbleHovered = true;

    private void Bubble_MouseLeave(object sender, MouseEventArgs e)
    {
        _isBubbleHovered = false;
        if (!_isChatOpen && SpeechBubble.Visibility == Visibility.Visible)
        {
            _bubbleTimer.Stop();
            _bubbleTimer.Interval = TimeSpan.FromSeconds(2.5);
            _bubbleTimer.Start();
        }
    }

    // ==================== Particles ====================

    private void SpawnStarBurst(double centerX, double centerY, int count)
    {
        if (!_settings.ParticlesEnabled) return;
        string[] palette = ["#A78BFA", "#38BDF8", "#F472B6", "#FBBF24"];
        for (int i = 0; i < count; i++)
        {
            double ang = (2 * Math.PI * i) / count + Random.Shared.NextDouble() * 0.4;
            double spd = 35 + Random.Shared.NextDouble() * 55;
            SpawnParticle("✦", palette[i % palette.Length], centerX, centerY, Math.Cos(ang) * spd, Math.Sin(ang) * spd - 20, 1.15);
        }
    }

    private void SpawnParticle(string glyph, string colorHex, double x, double y, double vx, double vy, double maxLife)
    {
        if (_particles.Count > 36) return;

        var tb = new TextBlock
        {
            Text = glyph,
            FontSize = glyph == "Zzz" ? 13 : 16,
            FontWeight = FontWeights.Bold,
            Foreground = new SolidColorBrush((Color)ColorConverter.ConvertFromString(colorHex)),
            IsHitTestVisible = false
        };
        Canvas.SetLeft(tb, x);
        Canvas.SetTop(tb, y);
        ParticleCanvas.Children.Add(tb);

        _particles.Add(new Particle
        {
            Visual = tb,
            X = x,
            Y = y,
            Vx = vx,
            Vy = vy,
            Life = 0,
            MaxLife = maxLife,
            Spin = (Random.Shared.NextDouble() - 0.5) * 90
        });
    }

    private void UpdateParticles(double dt)
    {
        for (int i = _particles.Count - 1; i >= 0; i--)
        {
            var p = _particles[i];
            p.Life += dt;
            if (p.Life >= p.MaxLife)
            {
                ParticleCanvas.Children.Remove(p.Visual);
                _particles.RemoveAt(i);
                continue;
            }

            p.X += p.Vx * dt;
            p.Y += p.Vy * dt;
            p.Angle += p.Spin * dt;
            double alpha = 1.0 - (p.Life / p.MaxLife);

            Canvas.SetLeft(p.Visual, p.X);
            Canvas.SetTop(p.Visual, p.Y);
            p.Visual.Opacity = Math.Clamp(alpha, 0, 1);
            p.Visual.RenderTransform = new RotateTransform(p.Angle, 8, 8);
        }
    }

    // ==================== Monitor & DPI Helpers ====================

    public Rect GetVisualSpriteBoundsDip()
    {
        FrameworkElement active = ChibiWalkRig.Visibility == Visibility.Visible
            ? ChibiWalkRig
            : StdWalkRig.Visibility == Visibility.Visible
                ? StdWalkRig
                : PetImage;

        var (sx, sy) = GetDpiScale();
        if (PresentationSource.FromVisual(active) != null && active.ActualWidth > 1 && active.ActualHeight > 1)
        {
            Point p0 = active.PointToScreen(new Point(0, 0));
            Point p1 = active.PointToScreen(new Point(active.ActualWidth, active.ActualHeight));
            double x1 = p0.X / sx;
            double y1 = p0.Y / sy;
            double x2 = p1.X / sx;
            double y2 = p1.Y / sy;
            return new Rect(
                Math.Min(x1, x2),
                Math.Min(y1, y2),
                Math.Max(1.0, Math.Abs(x2 - x1)),
                Math.Max(1.0, Math.Abs(y2 - y1)));
        }

        double estH = (_settings.StyleMode == PetStyleMode.Standard ? 300.0 : 240.0) * Math.Clamp(_settings.Scale, 0.5, 2.0);
        double estW = Math.Min(Width, estH * 0.85);
        double estLeft = _settings.DockedSide switch
        {
            DockSide.Left => Left,
            DockSide.Right => Left + Width - estW,
            _ => Left + (Width - estW) * 0.5
        };
        return new Rect(estLeft, Top + Height - estH, estW, estH);
    }

    private (double x, double y) GetDpiScale()
    {
        var source = PresentationSource.FromVisual(this);
        if (source?.CompositionTarget != null)
        {
            var m = source.CompositionTarget.TransformToDevice;
            return (m.M11, m.M22);
        }
        return (1.0, 1.0);
    }

    private Rect GetWorkAreaDip(double centerDipX, double centerDipY)
    {
        var (sx, sy) = GetDpiScale();
        var pt = new System.Drawing.Point((int)Math.Round(centerDipX * sx), (int)Math.Round(centerDipY * sy));
        var screen = Forms.Screen.FromPoint(pt);
        var wa = screen.WorkingArea;
        return new Rect(wa.Left / sx, wa.Top / sy, wa.Width / sx, wa.Height / sy);
    }

    private void ClampInsideNearestWorkArea()
    {
        var area = GetWorkAreaDip(Left + Width * 0.5, Top + Height * 0.5);
        Left = Math.Clamp(Left, area.Left, Math.Max(area.Left, area.Right - Width));
        Top = Math.Clamp(Top, area.Top, Math.Max(area.Top, area.Bottom - Height));
    }

    // ==================== Menu & Quick Action Handlers ====================

    private void MenuPat_Click(object sender, RoutedEventArgs e) => TriggerPat();

    private void ToggleAntiGravity_Click(object sender, RoutedEventArgs e)
    {
        _settings.AntiGravityMode = !_settings.AntiGravityMode;
        if (_settings.AntiGravityMode)
            _settings.DockedSide = DockSide.None;
        else
            _isFalling = true;

        _settingsService.Save(_settings);
        ApplySettingsToWindow();
        SettingsSynced?.Invoke();

        if (_settings.AntiGravityMode)
        {
            SetTemporaryPose(PetState.Float, TimeSpan.FromSeconds(4.0), CharacterLore.PickLine("float"));
            _sound.Play(PetSoundEffect.Sparkle);
            SpawnStarBurst(Width * 0.5, Height - PetImage.Height * 0.5, 9);
        }
        else
        {
            ShowBubble("反重力模块关闭，安全降落喵～", TimeSpan.FromSeconds(3));
        }
    }

    private void MenuAntiGravity_Click(object sender, RoutedEventArgs e) => ToggleAntiGravity_Click(sender, e);

    private void SetPinOnDesktopEnabled(bool pinned)
    {
        _settings.AutoWalk = !pinned;
        if (pinned && _isWalking)
            _isWalking = false;

        _settingsService.Save(_settings);
        ApplySettingsToWindow();
        SettingsSynced?.Invoke();
    }

    private void TogglePinOnDesktop_Click(object sender, RoutedEventArgs e)
    {
        bool nextPinned = _settings.AutoWalk;
        SetPinOnDesktopEnabled(nextPinned);
        ShowBubble(
            nextPinned
                ? "已开启「固定在桌面」～乖乖待在原地陪你喵📌"
                : "已关闭「固定在桌面」，恢复桌面自由漫步喵～✦",
            TimeSpan.FromSeconds(3.0));
    }

    private void MenuPinOnDesktop_Click(object sender, RoutedEventArgs e)
        => SetPinOnDesktopEnabled(MenuPinOnDesktop.IsChecked);

    private void MenuPose_Click(object sender, RoutedEventArgs e)
    {
        if (sender is MenuItem mi && mi.Tag is string key)
            TriggerExternalSpritePreview(key);
    }

    private void MenuScale_Click(object sender, RoutedEventArgs e)
    {
        if (sender is MenuItem mi && double.TryParse(mi.Tag?.ToString(), System.Globalization.CultureInfo.InvariantCulture, out double scale))
        {
            double oldBottom = Top + Height;
            _settings.Scale = scale;
            ApplySettingsToWindow();
            if (_settings.DockedSide != DockSide.None)
            {
                ReanchorDockedPosition();
            }
            else
            {
                Top = oldBottom - Height;
                ClampInsideNearestWorkArea();
            }
            _settingsService.Save(_settings);
            SettingsSynced?.Invoke();
        }
    }

    private void MenuDock_Click(object sender, RoutedEventArgs e)
    {
        if (sender is not MenuItem mi || mi.Tag is not string tag) return;
        var area = GetWorkAreaDip(Left + Width * 0.5, Top + Height * 0.5);

        switch (tag)
        {
            case "left":
                _settings.DockedSide = DockSide.Left;
                _settings.AntiGravityMode = false;
                Left = area.Left;
                Top = area.Top + (area.Height - Height) * 0.55;
                UpdateDockAlignment();
                SetTemporaryPose(PetState.EdgeLeftPeek, TimeSpan.FromSeconds(4), CharacterLore.PickLine("edge"));
                break;
            case "right":
                _settings.DockedSide = DockSide.Right;
                _settings.AntiGravityMode = false;
                Left = area.Right - Width;
                Top = area.Top + (area.Height - Height) * 0.55;
                UpdateDockAlignment();
                SetTemporaryPose(PetState.EdgeRightPeek, TimeSpan.FromSeconds(4), CharacterLore.PickLine("edge"));
                break;
            case "reset":
                MoveToBottomRightCorner();
                ApplySettingsToWindow();
                ShowAndGreet();
                break;
        }
        _settingsService.Save(_settings);
        SettingsSynced?.Invoke();
    }

    private void MenuClickThrough_Click(object sender, RoutedEventArgs e)
    {
        _settings.ClickThrough = true;
        _settingsService.Save(_settings);
        ApplySettingsToWindow();
        SettingsSynced?.Invoke();
        ShowBubble("已开启鼠标穿透！可在右下角托盘图标右键取消喵～", TimeSpan.FromSeconds(4.5));
    }

    private void OpenProfile_Click(object sender, RoutedEventArgs e) => OpenProfileRequested?.Invoke();
    private void OpenCharacterEditor_Click(object sender, RoutedEventArgs e) => OpenCharacterEditorRequested?.Invoke();
    private void OpenSettings_Click(object sender, RoutedEventArgs e) => OpenSettingsRequested?.Invoke();
    private void OpenQuota_Click(object sender, RoutedEventArgs e) => OpenQuotaRequested?.Invoke();
    private void OpenUpdate_Click(object sender, RoutedEventArgs e) => OpenUpdateRequested?.Invoke();
    private void MenuStandby_Click(object sender, RoutedEventArgs e) => StandbyRequested?.Invoke();
    private void MenuExit_Click(object sender, RoutedEventArgs e) => ExitRequested?.Invoke();

    public void SyncCharacterProfile(CharacterProfile profile)
    {
        Title = profile.Name;
        BubbleCharacterNameText.Text = profile.Name;
    }

    public void ToggleStyleMode()
    {
        SetStyleMode(_settings.StyleMode == PetStyleMode.Chibi ? PetStyleMode.Standard : PetStyleMode.Chibi);
    }

    private void SetStyleMode(PetStyleMode mode)
    {
        _settings.StyleMode = mode;
        _settingsService.Save(_settings);
        ApplySettingsToWindow();
        SettingsSynced?.Invoke();

        string msg = mode == PetStyleMode.Standard
            ? "已切换为「标准立绘版」形象喵～修长比例登场✦"
            : "已切换为「Q 版猫娘」形象喵～软萌模式✦";

        if (_settings.DockedSide != DockSide.None)
        {
            ReanchorDockedPosition();
            var dockPose = _settings.DockedSide == DockSide.Left ? PetState.EdgeLeftPeek : PetState.EdgeRightPeek;
            SetTemporaryPose(dockPose, TimeSpan.FromSeconds(4.2), msg);
        }
        else
        {
            SetTemporaryPose(PetState.Happy, TimeSpan.FromSeconds(4.2), msg);
        }
        _sound.Play(PetSoundEffect.Sparkle);
        SpawnStarBurst(Width * 0.5, Height - PetImage.Height * 0.55, 8);
    }

    private void ToggleStyleMode_Click(object sender, RoutedEventArgs e) => ToggleStyleMode();
    private void MenuStyleChibi_Click(object sender, RoutedEventArgs e) => SetStyleMode(PetStyleMode.Chibi);
    private void MenuStyleStandard_Click(object sender, RoutedEventArgs e) => SetStyleMode(PetStyleMode.Standard);

    private void MenuFollowAntigravity_Click(object sender, RoutedEventArgs e)
    {
        bool enabled = MenuFollowAntigravity.IsChecked;
        FollowAntigravityChanged?.Invoke(enabled);
        ShowBubble(
            enabled
                ? "已开启「跟随 Antigravity 启动而启动」(原生守护已上线) 喵✦"
                : "已关闭跟随 Antigravity 启动喵～",
            TimeSpan.FromSeconds(4));
    }

    private void MenuFollowGeminiApp_Click(object sender, RoutedEventArgs e)
    {
        bool enabled = MenuFollowGeminiApp.IsChecked;
        FollowGeminiAppChanged?.Invoke(enabled);
        ShowBubble(
            enabled
                ? "已开启「跟随 Gemini 桌面 App 启动而启动」(原生守护已上线) 喵✦"
                : "已关闭跟随 Gemini 桌面 App 启动喵～",
            TimeSpan.FromSeconds(4));
    }
}

internal static class MathCompat
{
    public static double Hypot(double a, double b) => Math.Sqrt(a * a + b * b);
}
