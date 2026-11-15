using System;
using System.Diagnostics;
using System.IO;
using System.Reflection;
using System.Threading;
using System.Windows.Forms;
using Microsoft.Win32;

[assembly: AssemblyTitle("Gemini 娘桌宠卸载程序")]
[assembly: AssemblyProduct("Gemini 娘 · Windows 桌面萌宠")]
[assembly: AssemblyVersion("0.2.2.0")]
[assembly: AssemblyFileVersion("0.2.2.0")]

namespace GeminiDesktopPet.Uninstaller
{
    internal static class Program
    {
        private const string RunRegistryPath = @"Software\Microsoft\Windows\CurrentVersion\Run";

        [STAThread]
        private static int Main(string[] args)
        {
            Application.EnableVisualStyles();
            Application.SetCompatibleTextRenderingDefault(false);

            bool noCompleteDialog = HasArg(args, "--no-complete-dialog");
            string targetDir = GetArgValue(args, "--target-dir");

            string selfPath = Assembly.GetExecutingAssembly().Location;
            string selfDir = Path.GetDirectoryName(selfPath) ?? AppDomain.CurrentDomain.BaseDirectory;
            selfDir = selfDir.TrimEnd(Path.DirectorySeparatorChar, Path.AltDirectorySeparatorChar);

            // 第一阶段：在原目录启动时，将自身单文件复制到 %TEMP% 再执行清理与自删除
            if (string.IsNullOrWhiteSpace(targetDir))
            {
                try
                {
                    string tempExePath = Path.Combine(
                        Path.GetTempPath(),
                        "GeminiDesktopPet_Uninstaller_" + Guid.NewGuid().ToString("N") + ".exe");
                    File.Copy(selfPath, tempExePath, true);

                    string extraArgs = "--target-dir=\"" + selfDir + "\"";
                    if (noCompleteDialog)
                        extraArgs += " --no-complete-dialog";

                    var psi = new ProcessStartInfo
                    {
                        FileName = tempExePath,
                        Arguments = extraArgs,
                        WorkingDirectory = Path.GetTempPath(),
                        UseShellExecute = false
                    };
                    Process.Start(psi);
                    return 0;
                }
                catch
                {
                    targetDir = selfDir;
                }
            }

            // 第二阶段：从 %TEMP% 中执行彻底清理（此时原目录下的卸载程序进程已退出，无文件占用）
            PerformUninstallCleanup(targetDir.TrimEnd(Path.DirectorySeparatorChar, Path.AltDirectorySeparatorChar));

            if (!noCompleteDialog)
            {
                MessageBox.Show(
                    "卸载完成",
                    "提示",
                    MessageBoxButtons.OK,
                    MessageBoxIcon.Information);
            }

            return 0;
        }

        private static void PerformUninstallCleanup(string targetDir)
        {
            // 1. 结束正在运行的桌宠进程
            foreach (Process proc in Process.GetProcessesByName("GeminiDesktopPet"))
            {
                try
                {
                    proc.Kill();
                    proc.WaitForExit(2000);
                }
                catch
                {
                }
                finally
                {
                    proc.Dispose();
                }
            }

            // 2. 清理 HKCU\Software\Microsoft\Windows\CurrentVersion\Run 注册表项
            string[] registryNames =
            {
                "GeminiDesktopPet",
                "GeminiDesktopPet_FollowWatcher",
                "GeminiDesktopPet_AntigravityFollower",
                "GeminiDesktopPet_AntigravityFollow"
            };
            try
            {
                using (RegistryKey key = Registry.CurrentUser.OpenSubKey(RunRegistryPath, true))
                {
                    if (key != null)
                    {
                        foreach (string name in registryNames)
                        {
                            try { key.DeleteValue(name, false); }
                            catch { }
                        }
                    }
                }
            }
            catch
            {
            }

            // 3. 清理旧版 Startup 启动目录脚本残留
            try
            {
                string startupDir = Environment.GetFolderPath(Environment.SpecialFolder.Startup);
                string[] legacyStartupFiles =
                {
                    "GeminiDesktopPet_Follow.vbs",
                    "GeminiDesktopPet_AntigravityFollow.vbs",
                    "GeminiDesktopPet_Follow.bat"
                };
                foreach (string file in legacyStartupFiles)
                    TryDeleteFile(Path.Combine(startupDir, file));
            }
            catch
            {
            }

            // 4. 删除 %AppData%\GeminiDesktopPet 与 %LocalAppData%\GeminiDesktopPet 配置及缓存目录
            string appDataDir = Path.Combine(
                Environment.GetFolderPath(Environment.SpecialFolder.ApplicationData),
                "GeminiDesktopPet");
            TryDeleteDirectoryWithRetry(appDataDir, 6);

            string localAppDataDir = Path.Combine(
                Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
                "GeminiDesktopPet");
            TryDeleteDirectoryWithRetry(localAppDataDir, 6);

            // 5. 删除目标目录下的配置文件、日志与程序本体
            if (!string.IsNullOrWhiteSpace(targetDir) && Directory.Exists(targetDir))
            {
                TryDeleteFile(Path.Combine(targetDir, "config.ini"));
                TryDeleteDirectoryWithRetry(Path.Combine(targetDir, "Logs"), 5);

                if (!Directory.Exists(Path.Combine(targetDir, ".git"))
                    && !File.Exists(Path.Combine(targetDir, "GeminiDesktopPet.csproj")))
                {
                    TryDeleteDirectoryWithRetry(targetDir, 15);
                }
            }
        }

        private static bool HasArg(string[] args, string flag)
        {
            if (args == null) return false;
            foreach (string a in args)
            {
                if (string.Equals(a, flag, StringComparison.OrdinalIgnoreCase))
                    return true;
            }
            return false;
        }

        private static string GetArgValue(string[] args, string prefix)
        {
            if (args == null) return string.Empty;
            string prefixWithEq = prefix + "=";
            for (int i = 0; i < args.Length; i++)
            {
                string a = args[i] ?? string.Empty;
                if (a.StartsWith(prefixWithEq, StringComparison.OrdinalIgnoreCase))
                    return a.Substring(prefixWithEq.Length).Trim('"');
                if (string.Equals(a, prefix, StringComparison.OrdinalIgnoreCase) && i + 1 < args.Length)
                    return (args[i + 1] ?? string.Empty).Trim('"');
            }
            return string.Empty;
        }

        private static void TryDeleteFile(string path)
        {
            try
            {
                if (File.Exists(path))
                    File.Delete(path);
            }
            catch
            {
            }
        }

        private static void TryDeleteDirectoryWithRetry(string path, int maxAttempts)
        {
            for (int i = 0; i < maxAttempts; i++)
            {
                try
                {
                    if (!Directory.Exists(path))
                        return;
                    Directory.Delete(path, true);
                    return;
                }
                catch
                {
                    Thread.Sleep(250);
                }
            }
        }
    }
}
