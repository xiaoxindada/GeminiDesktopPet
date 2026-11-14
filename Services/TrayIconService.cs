using System.Drawing;
using System.IO;
using System.Windows.Forms;
using GeminiDesktopPet.Models;

namespace GeminiDesktopPet.Services;

/// <summary>
/// Manages the native Windows System Tray icon and quick menu, ensuring full control even in click-through mode.
/// </summary>
public sealed class TrayIconService : IDisposable
{
    private readonly NotifyIcon _notifyIcon;
    private readonly ToolStripMenuItem _styleModeItem;
    private readonly ToolStripMenuItem _antiGravityItem;
    private readonly ToolStripMenuItem _autoWalkItem;
    private readonly ToolStripMenuItem _speechBubbleItem;
    private readonly ToolStripMenuItem _followAgItem;
    private readonly ToolStripMenuItem _followGeminiItem;
    private readonly ToolStripMenuItem _clickThroughItem;
    private readonly ToolStripMenuItem _alwaysOnTopItem;
    private readonly ToolStripMenuItem _soundItem;

    public event Action? ShowAndGreetRequested;
    public event Action? OpenChatRequested;
    public event Action? PatRequested;
    public event Action? ResetPositionRequested;
    public event Action? StandbyForAntigravityRequested;
    public event Action? StyleToggleRequested;
    public event Action<bool>? AntiGravityToggled;
    public event Action<bool>? AutoWalkToggled;
    public event Action<bool>? SpeechBubbleToggled;
    public event Action<bool>? FollowAntigravityToggled;
    public event Action<bool>? FollowGeminiAppToggled;
    public event Action<bool>? ClickThroughToggled;
    public event Action<bool>? AlwaysOnTopToggled;
    public event Action<bool>? SoundToggled;
    public event Action? OpenProfileRequested;
    public event Action? OpenSettingsRequested;
    public event Action? OpenQuotaRequested;
    public event Action? ExitRequested;

    public TrayIconService()
    {
        Icon icon = LoadAppIcon();
        var menu = new ContextMenuStrip();

        var greetItem = new ToolStripMenuItem("✦ 呼唤 Gemini 娘", null, (_, _) => ShowAndGreetRequested?.Invoke());
        greetItem.Font = new Font(menu.Font, FontStyle.Bold);
        menu.Items.Add(greetItem);

        menu.Items.Add(new ToolStripMenuItem("📊 查看反重力额度 (Antigravity)", null, (_, _) => OpenQuotaRequested?.Invoke()));
        menu.Items.Add(new ToolStripMenuItem("💬 打开宠物对话框 / 问问题", null, (_, _) => OpenChatRequested?.Invoke()));
        menu.Items.Add(new ToolStripMenuItem("♥ 摸摸猫耳", null, (_, _) => PatRequested?.Invoke()));
        _styleModeItem = new ToolStripMenuItem("🎀 切换形象：当前 Q 版 → 切至标准版", null, (_, _) => StyleToggleRequested?.Invoke());
        menu.Items.Add(_styleModeItem);
        menu.Items.Add(new ToolStripMenuItem("📍 回到屏幕右下角", null, (_, _) => ResetPositionRequested?.Invoke()));
        menu.Items.Add(new ToolStripMenuItem("💤 隐藏待机（等待联动软件启动唤醒）", null, (_, _) => StandbyForAntigravityRequested?.Invoke()));
        menu.Items.Add(new ToolStripSeparator());

        _antiGravityItem = new ToolStripMenuItem("✨ 反重力悬浮模式") { CheckOnClick = true };
        _antiGravityItem.Click += (_, _) => AntiGravityToggled?.Invoke(_antiGravityItem.Checked);
        menu.Items.Add(_antiGravityItem);

        _autoWalkItem = new ToolStripMenuItem("🐾 桌面自由漫步") { CheckOnClick = true };
        _autoWalkItem.Click += (_, _) => AutoWalkToggled?.Invoke(_autoWalkItem.Checked);
        menu.Items.Add(_autoWalkItem);

        _speechBubbleItem = new ToolStripMenuItem("💭 自动弹出对话气泡") { CheckOnClick = true };
        _speechBubbleItem.Click += (_, _) => SpeechBubbleToggled?.Invoke(_speechBubbleItem.Checked);
        menu.Items.Add(_speechBubbleItem);

        _followAgItem = new ToolStripMenuItem("🚀 跟随 Antigravity 启动而启动") { CheckOnClick = true };
        _followAgItem.Click += (_, _) => FollowAntigravityToggled?.Invoke(_followAgItem.Checked);
        menu.Items.Add(_followAgItem);

        _followGeminiItem = new ToolStripMenuItem("🌟 跟随 Gemini 桌面 App 启动而启动") { CheckOnClick = true };
        _followGeminiItem.Click += (_, _) => FollowGeminiAppToggled?.Invoke(_followGeminiItem.Checked);
        menu.Items.Add(_followGeminiItem);

        _alwaysOnTopItem = new ToolStripMenuItem("📌 窗口置顶") { CheckOnClick = true };
        _alwaysOnTopItem.Click += (_, _) => AlwaysOnTopToggled?.Invoke(_alwaysOnTopItem.Checked);
        menu.Items.Add(_alwaysOnTopItem);

        _clickThroughItem = new ToolStripMenuItem("👻 鼠标穿透模式") { CheckOnClick = true };
        _clickThroughItem.Click += (_, _) => ClickThroughToggled?.Invoke(_clickThroughItem.Checked);
        menu.Items.Add(_clickThroughItem);

        _soundItem = new ToolStripMenuItem("🔔 互动音效") { CheckOnClick = true };
        _soundItem.Click += (_, _) => SoundToggled?.Invoke(_soundItem.Checked);
        menu.Items.Add(_soundItem);

        menu.Items.Add(new ToolStripSeparator());
        menu.Items.Add(new ToolStripMenuItem("📖 角色档案与设定图鉴", null, (_, _) => OpenProfileRequested?.Invoke()));
        menu.Items.Add(new ToolStripMenuItem("⚙️ 偏好设置", null, (_, _) => OpenSettingsRequested?.Invoke()));

        menu.Items.Add(new ToolStripSeparator());
        menu.Items.Add(new ToolStripMenuItem("🌙 让 Gemini 娘休息（退出）", null, (_, _) => ExitRequested?.Invoke()));

        _notifyIcon = new NotifyIcon
        {
            Icon = icon,
            Text = "Gemini 娘 · 桌面萌宠 (双击/右键看额度)",
            ContextMenuStrip = menu,
            Visible = true
        };
        _notifyIcon.DoubleClick += (_, _) => ShowAndGreetRequested?.Invoke();
    }

    public void SyncState(PetSettings settings)
    {
        _styleModeItem.Text = settings.StyleMode == PetStyleMode.Standard
            ? "🎀 切换形象：当前标准版 → 切至 Q 版"
            : "🎀 切换形象：当前 Q 版 → 切至标准版";
        _antiGravityItem.Checked = settings.AntiGravityMode;
        _autoWalkItem.Checked = settings.AutoWalk;
        _speechBubbleItem.Checked = settings.ShowSpeechBubble;
        _followAgItem.Checked = settings.FollowAntigravity;
        _followGeminiItem.Checked = settings.FollowGeminiApp;
        _alwaysOnTopItem.Checked = settings.AlwaysOnTop;
        _clickThroughItem.Checked = settings.ClickThrough;
        _soundItem.Checked = settings.SoundEnabled;
    }

    public void ShowBalloon(string title, string text)
    {
        try
        {
            _notifyIcon.ShowBalloonTip(2500, title, text, ToolTipIcon.Info);
        }
        catch
        {
            // Ignore balloon failures on restricted systems.
        }
    }

    private static Icon LoadAppIcon()
    {
        try
        {
            var res = System.Windows.Application.GetResourceStream(new Uri("pack://application:,,,/Assets/AppIcon.ico"));
            if (res?.Stream != null)
            {
                using (res.Stream)
                    return new Icon(res.Stream);
            }
        }
        catch
        {
            // Fallback to file lookup.
        }

        string path = Path.Combine(AppContext.BaseDirectory, "Assets", "AppIcon.ico");
        if (File.Exists(path))
            return new Icon(path);
        return SystemIcons.Application;
    }

    public void Dispose()
    {
        _notifyIcon.Visible = false;
        _notifyIcon.Dispose();
    }
}
