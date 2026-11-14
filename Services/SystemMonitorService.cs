using System.Runtime.InteropServices;
using System.Windows.Forms;

namespace GeminiDesktopPet.Services;

public sealed record SystemHealthSnapshot(
    double CpuPercent,
    bool HasBattery,
    int BatteryPercent,
    bool IsCharging);

/// <summary>
/// Lightweight CPU & battery poller using Win32 GetSystemTimes and PowerStatus.
/// </summary>
public sealed class SystemMonitorService
{
    [StructLayout(LayoutKind.Sequential)]
    private struct FileTime
    {
        public uint LowDateTime;
        public uint HighDateTime;
        public ulong ToUInt64() => ((ulong)HighDateTime << 32) | LowDateTime;
    }

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool GetSystemTimes(out FileTime idleTime, out FileTime kernelTime, out FileTime userTime);

    private ulong _prevIdle;
    private ulong _prevKernel;
    private ulong _prevUser;
    private bool _hasBaseline;

    public SystemHealthSnapshot Sample()
    {
        double cpu = SampleCpu();
        var power = SystemInformation.PowerStatus;
        bool hasBattery = power.BatteryChargeStatus != BatteryChargeStatus.NoSystemBattery
                          && power.BatteryChargeStatus != BatteryChargeStatus.Unknown;
        int pct = hasBattery ? (int)Math.Round(power.BatteryLifePercent * 100.0) : 100;
        bool charging = power.PowerLineStatus == PowerLineStatus.Online;

        return new SystemHealthSnapshot(cpu, hasBattery, Math.Clamp(pct, 0, 100), charging);
    }

    private double SampleCpu()
    {
        if (!GetSystemTimes(out var idleFt, out var kernelFt, out var userFt))
            return 0;

        ulong idle = idleFt.ToUInt64();
        ulong kernel = kernelFt.ToUInt64();
        ulong user = userFt.ToUInt64();

        if (!_hasBaseline)
        {
            _prevIdle = idle;
            _prevKernel = kernel;
            _prevUser = user;
            _hasBaseline = true;
            return 0;
        }

        ulong dIdle = idle - _prevIdle;
        ulong dKernel = kernel - _prevKernel;
        ulong dUser = user - _prevUser;

        _prevIdle = idle;
        _prevKernel = kernel;
        _prevUser = user;

        ulong total = dKernel + dUser;
        if (total == 0) return 0;

        double usage = (total - dIdle) * 100.0 / total;
        return Math.Clamp(usage, 0, 100);
    }
}
