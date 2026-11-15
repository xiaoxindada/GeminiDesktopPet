using System.Collections.Concurrent;
using System.IO;
using System.Text;
using GeminiDesktopPet.Models;

namespace GeminiDesktopPet.Services;

/// <summary>
/// Built-in regression test suite for follow-app startup, process filtering,
/// state transition detection, and background watcher handoff.
/// </summary>
public static class FollowStartupSelfTest
{
    public static int RunAll(string? reportFilePath = null)
    {
        var log = new StringBuilder();
        int passed = 0;
        int failed = 0;

        void RunCase(string name, Action test)
        {
            try
            {
                test();
                passed++;
                string line = $"[PASS] {name}";
                Console.WriteLine(line);
                log.AppendLine(line);
            }
            catch (Exception ex)
            {
                failed++;
                string line = $"[FAIL] {name} -> {ex.Message}";
                Console.WriteLine(line);
                log.AppendLine(line);
            }
        }

        RunCase("1. Default config.ini generation & INI round-trip persistence", TestConfigIniPersistence);
        RunCase("2. Orphaned Node worker filtering & Electron process classification", TestProcessClassification);
        RunCase("3. Cold-start, two-stage warmup, GPU spike & restart state machine", TestEvaluateTransitionStateMachine);
        RunCase("4. Watcher cold-start wake, resident survival & second cold-start after pet exit", TestWatcherColdStartAndSecondWake);
        RunCase("5. Watcher logon immediate wake when followed app is already running", TestWatcherLogonImmediateWake);
        RunCase("6. Watcher Gemini app cold-start wake & config.ini hot-reload auto-exit", TestWatcherGeminiWakeAndConfigAutoExit);
        RunCase("7. Watcher single-instance mutex deduplication", TestWatcherSingleInstanceDeduplication);

        string summary = failed == 0
            ? $"[FollowStartupSelfTest] ALL PASSED ({passed}/{passed} tests succeeded)."
            : $"[FollowStartupSelfTest] FAILED ({failed} failed, {passed} passed).";
        Console.WriteLine(summary);
        log.AppendLine(summary);

        if (!string.IsNullOrWhiteSpace(reportFilePath))
        {
            try
            {
                string? dir = Path.GetDirectoryName(reportFilePath);
                if (!string.IsNullOrWhiteSpace(dir))
                    Directory.CreateDirectory(dir);
                File.WriteAllText(reportFilePath, log.ToString(), new UTF8Encoding(false));
            }
            catch
            {
            }
        }

        return failed == 0 ? 0 : 1;
    }

    private static void Assert(bool condition, string message)
    {
        if (!condition)
            throw new InvalidOperationException(message);
    }

    private static void TestConfigIniPersistence()
    {
        string tempDir = Path.Combine(Path.GetTempPath(), "GeminiPetTest_Cfg_" + Guid.NewGuid().ToString("N"));
        try
        {
            var svc = new SettingsService(tempDir);
            var initial = svc.Load();

            Assert(File.Exists(svc.SettingsFilePath), "First Load() should auto-generate config.ini.");
            Assert(initial.FollowAntigravity, "FollowAntigravity must default to true.");
            Assert(initial.FollowGeminiApp, "FollowGeminiApp must default to true.");

            initial.FollowAntigravity = false;
            initial.FollowGeminiApp = true;
            initial.SleepWhenAntigravityExits = true;
            svc.Save(initial);

            var reloaded = svc.Load();
            Assert(!reloaded.FollowAntigravity, "Reloaded FollowAntigravity should be false.");
            Assert(reloaded.FollowGeminiApp, "Reloaded FollowGeminiApp should be true.");
            Assert(reloaded.SleepWhenAntigravityExits, "Reloaded SleepWhenAntigravityExits should be true.");
        }
        finally
        {
            TryDeleteTempDir(tempDir);
        }
    }

    private static void TestProcessClassification()
    {
        // Orphaned Node/MCP workers (--import=...) with 10-12 threads and no main window must be ignored.
        var orphanOnly = AntigravityWatcherService.BuildSnapshotFromProbes(
        [
            new ProcessProbeInfo(1001, HasMainWindow: false, ThreadCount: 10),
            new ProcessProbeInfo(1002, HasMainWindow: false, ThreadCount: 11),
            new ProcessProbeInfo(1003, HasMainWindow: false, ThreadCount: 12)
        ]);
        Assert(!orphanOnly.IsRunning, "Orphaned 10-12 thread Node workers must not count as running.");
        Assert(orphanOnly.ActivePids.Count == 0 && orphanOnly.PrimaryPids.Count == 0, "Orphan snapshot PID sets must be empty.");

        // Active Electron child processes (22-37 threads, no window) are active but not primary.
        var warmupOnly = AntigravityWatcherService.BuildSnapshotFromProbes(
        [
            new ProcessProbeInfo(1001, HasMainWindow: false, ThreadCount: 11),
            new ProcessProbeInfo(2001, HasMainWindow: false, ThreadCount: 26),
            new ProcessProbeInfo(2002, HasMainWindow: false, ThreadCount: 33)
        ]);
        Assert(warmupOnly.IsRunning, "26-33 thread Electron processes should be marked active.");
        Assert(warmupOnly.ActivePids.SetEquals([2001, 2002]), "ActivePids should contain 2001 and 2002.");
        Assert(warmupOnly.PrimaryPids.Count == 0, "PrimaryPids should be empty before main window or >=38 threads.");

        // Primary Electron browser process (either MainWindowHandle != 0 or >= 38 threads).
        var fullRunning = AntigravityWatcherService.BuildSnapshotFromProbes(
        [
            new ProcessProbeInfo(1001, HasMainWindow: false, ThreadCount: 11),
            new ProcessProbeInfo(2001, HasMainWindow: false, ThreadCount: 28),
            new ProcessProbeInfo(3001, HasMainWindow: false, ThreadCount: 49),
            new ProcessProbeInfo(3002, HasMainWindow: true, ThreadCount: 19)
        ]);
        Assert(fullRunning.IsRunning, "Full running snapshot must be running.");
        Assert(fullRunning.PrimaryPids.SetEquals([3001, 3002]), "PrimaryPids should include >=38-thread and windowed processes.");
    }

    private static void TestEvaluateTransitionStateMachine()
    {
        bool wasRunning = false;
        HashSet<int> knownPids = [];
        DateTime lastStartUtc = DateTime.MinValue;
        DateTime t0 = new(2026, 11, 15, 4, 0, 0, DateTimeKind.Utc);

        // Step 1: Two-stage startup - Poll 1 sees process 5000 warming up at 25 threads (no window yet).
        var snapWarmup = new AppActivitySnapshot(true, [5000], []);
        AntigravityWatcherService.EvaluateTransition(
            snapWarmup, ref wasRunning, ref knownPids, ref lastStartUtc, out bool started1, out bool exited1, t0);
        Assert(started1 && !exited1, "Step 1: Warmup cold start must trigger started=true.");

        // Step 2: 8 seconds later, process 5000 finishes window/thread initialization (49 threads) + spawns GPU child 5001 (33 threads).
        // Even though >6s elapsed, 5000 was already recorded at Step 1, so it must NOT double-trigger!
        var snapReady = new AppActivitySnapshot(true, [5000, 5001], [5000]);
        AntigravityWatcherService.EvaluateTransition(
            snapReady, ref wasRunning, ref knownPids, ref lastStartUtc, out bool started2, out bool exited2, t0.AddSeconds(8));
        Assert(!started2 && !exited2, "Step 2: Same process 5000 reaching primary threshold must not double-trigger.");

        // Step 3: 20 seconds later, existing GPU child 5001 spikes from 33 threads to 40 threads.
        // Because 5001 was already absorbed as a child of running primary 5000, it must NOT false-trigger!
        var snapGpuSpike = new AppActivitySnapshot(true, [5000, 5001], [5000, 5001]);
        AntigravityWatcherService.EvaluateTransition(
            snapGpuSpike, ref wasRunning, ref knownPids, ref lastStartUtc, out bool started3, out bool exited3, t0.AddSeconds(28));
        Assert(!started3 && !exited3, "Step 3: Existing child GPU process spiking >=38 threads must not false-trigger.");

        // Step 4: App closes (leaving only ignored 11-thread Node workers -> empty snapshot).
        var snapStopped = new AppActivitySnapshot(false, [], []);
        AntigravityWatcherService.EvaluateTransition(
            snapStopped, ref wasRunning, ref knownPids, ref lastStartUtc, out bool started4, out bool exited4, t0.AddSeconds(40));
        Assert(!started4 && exited4 && !wasRunning, "Step 4: Closing app must trigger exited=true.");

        // Step 5: Cold-start again with PID 6000.
        var snapRelaunch = new AppActivitySnapshot(true, [6000], [6000]);
        AntigravityWatcherService.EvaluateTransition(
            snapRelaunch, ref wasRunning, ref knownPids, ref lastStartUtc, out bool started5, out bool exited5, t0.AddSeconds(42));
        Assert(started5 && !exited5, "Step 5: Re-launching app after exit must trigger started=true.");

        // Step 6: Suppose primary 6000 exits, but a stuck 23-thread utility process 6099 lingers (IsRunning=true, PrimaryPids=empty),
        // and then the user cold-starts the app again (new process 7000 starts at 25 threads on poll A, then 48 threads on poll B).
        var snapStuckHelper = new AppActivitySnapshot(true, [6099], []);
        AntigravityWatcherService.EvaluateTransition(
            snapStuckHelper, ref wasRunning, ref knownPids, ref lastStartUtc, out bool started6a, out _, t0.AddSeconds(50));
        Assert(!started6a, "Step 6a: Stuck helper alone must not trigger start.");

        var snapNewWarmupOverHelper = new AppActivitySnapshot(true, [6099, 7000], []);
        AntigravityWatcherService.EvaluateTransition(
            snapNewWarmupOverHelper, ref wasRunning, ref knownPids, ref lastStartUtc, out bool started6b, out _, t0.AddSeconds(52));
        Assert(!started6b, "Step 6b: Warmup over stuck helper waits for primary confirmation.");

        var snapNewPrimaryOverHelper = new AppActivitySnapshot(true, [6099, 7000], [7000]);
        AntigravityWatcherService.EvaluateTransition(
            snapNewPrimaryOverHelper, ref wasRunning, ref knownPids, ref lastStartUtc, out bool started6c, out _, t0.AddSeconds(53));
        Assert(started6c, "Step 6c: New primary PID 7000 over stuck helper must trigger started=true.");
    }

    private static void TestWatcherColdStartAndSecondWake()
    {
        string tempDir = Path.Combine(Path.GetTempPath(), "GeminiPetTest_Watch1_" + Guid.NewGuid().ToString("N"));
        string id = Guid.NewGuid().ToString("N");
        string mainMutexName = $"GeminiDesktopPet_Test_{id}_Main";
        string watcherMutexName = $"GeminiDesktopPet_Test_{id}_Watcher";
        string stopEventName = $"GeminiDesktopPet_Test_{id}_Stop";

        try
        {
            var svc = new SettingsService(tempDir);
            svc.Save(new PetSettings { FollowAntigravity = true, FollowGeminiApp = true });

            int agState = 0; // 0 = stopped, 1 = running PID 8001, 2 = running PID 8002
            var wakes = new ConcurrentQueue<string>();
            using var wakeSignal = new AutoResetEvent(false);

            var options = new FollowWatcherOptions
            {
                FromExit = true,
                CustomSettingsFolder = tempDir,
                MainMutexName = mainMutexName,
                WatcherMutexName = watcherMutexName,
                StopWatcherEventName = stopEventName,
                PollIntervalMs = 25,
                SampleAntigravity = () => agState switch
                {
                    1 => new AppActivitySnapshot(true, [8001], [8001]),
                    2 => new AppActivitySnapshot(true, [8002], [8002]),
                    _ => new AppActivitySnapshot(false, [], [])
                },
                SampleGemini = () => new AppActivitySnapshot(false, [], []),
                CustomWakeLauncher = arg =>
                {
                    wakes.Enqueue(arg);
                    wakeSignal.Set();
                    return true;
                }
            };

            var watcherTask = Task.Run(() => AppFollowService.RunFollowWatcherCore(options));
            Thread.Sleep(80);
            Assert(AppFollowService.IsFollowWatcherRunning(watcherMutexName), "Watcher should hold WatcherMutex while running.");

            // 1st cold start of Antigravity while pet is closed
            Volatile.Write(ref agState, 1);
            Assert(wakeSignal.WaitOne(1500), "Watcher failed to wake pet on 1st Antigravity cold start.");
            Assert(wakes.TryDequeue(out string? firstArg) && firstArg == "--woken-by:antigravity", $"Expected --woken-by:antigravity, got '{firstArg}'.");

            // Simulate main pet GUI taking over and then closing, while watcher stays resident in background
            using (var simMainMutex = new Mutex(true, mainMutexName, out bool ownsMain))
            {
                Assert(ownsMain, "Should acquire simulated main mutex.");
                Thread.Sleep(80);
                Assert(!watcherTask.IsCompleted, "Watcher must remain resident while main pet is running.");
                simMainMutex.ReleaseMutex();
            }

            // Antigravity exits, then cold-starts a 2nd time -> watcher must wake pet again!
            Volatile.Write(ref agState, 0);
            Thread.Sleep(90);
            Volatile.Write(ref agState, 2);
            Assert(wakeSignal.WaitOne(1500), "Watcher failed to wake pet on 2nd Antigravity cold start after pet exit.");
            Assert(wakes.TryDequeue(out string? secondArg) && secondArg == "--woken-by:antigravity", $"Expected 2nd --woken-by:antigravity, got '{secondArg}'.");

            AppFollowService.SignalStopFollowWatcher(stopEventName);
            Assert(watcherTask.Wait(2000) && watcherTask.Result == 0, "Watcher should exit cleanly on StopWatcherEvent.");
        }
        finally
        {
            AppFollowService.SignalStopFollowWatcher(stopEventName);
            TryDeleteTempDir(tempDir);
        }
    }

    private static void TestWatcherLogonImmediateWake()
    {
        string tempDir = Path.Combine(Path.GetTempPath(), "GeminiPetTest_Watch2_" + Guid.NewGuid().ToString("N"));
        string id = Guid.NewGuid().ToString("N");
        string mainMutexName = $"GeminiDesktopPet_Test_{id}_Main";
        string watcherMutexName = $"GeminiDesktopPet_Test_{id}_Watcher";
        string stopEventName = $"GeminiDesktopPet_Test_{id}_Stop";

        try
        {
            var svc = new SettingsService(tempDir);
            svc.Save(new PetSettings { FollowAntigravity = true, FollowGeminiApp = true });

            string? wokenArg = null;
            using var wakeSignal = new ManualResetEventSlim(false);

            var options = new FollowWatcherOptions
            {
                FromExit = false, // Logon startup mode
                CustomSettingsFolder = tempDir,
                MainMutexName = mainMutexName,
                WatcherMutexName = watcherMutexName,
                StopWatcherEventName = stopEventName,
                PollIntervalMs = 25,
                SampleAntigravity = () => new AppActivitySnapshot(true, [9001], [9001]),
                SampleGemini = () => new AppActivitySnapshot(false, [], []),
                CustomWakeLauncher = arg =>
                {
                    wokenArg = arg;
                    wakeSignal.Set();
                    return true;
                }
            };

            var watcherTask = Task.Run(() => AppFollowService.RunFollowWatcherCore(options));
            Assert(wakeSignal.Wait(1500), "Logon watcher (fromExit=false) must immediately wake pet when target is already running.");
            Assert(wokenArg == "--woken-by:antigravity", $"Expected --woken-by:antigravity on logon wake, got '{wokenArg}'.");

            AppFollowService.SignalStopFollowWatcher(stopEventName);
            Assert(watcherTask.Wait(2000), "Logon watcher should stop cleanly.");
        }
        finally
        {
            AppFollowService.SignalStopFollowWatcher(stopEventName);
            TryDeleteTempDir(tempDir);
        }
    }

    private static void TestWatcherGeminiWakeAndConfigAutoExit()
    {
        string tempDir = Path.Combine(Path.GetTempPath(), "GeminiPetTest_Watch3_" + Guid.NewGuid().ToString("N"));
        string id = Guid.NewGuid().ToString("N");
        string mainMutexName = $"GeminiDesktopPet_Test_{id}_Main";
        string watcherMutexName = $"GeminiDesktopPet_Test_{id}_Watcher";
        string stopEventName = $"GeminiDesktopPet_Test_{id}_Stop";

        try
        {
            var svc = new SettingsService(tempDir);
            svc.Save(new PetSettings { FollowAntigravity = true, FollowGeminiApp = true });

            int geminiRunning = 0;
            string? wokenArg = null;
            using var wakeSignal = new ManualResetEventSlim(false);

            var options = new FollowWatcherOptions
            {
                FromExit = true,
                CustomSettingsFolder = tempDir,
                MainMutexName = mainMutexName,
                WatcherMutexName = watcherMutexName,
                StopWatcherEventName = stopEventName,
                PollIntervalMs = 25,
                SampleAntigravity = () => new AppActivitySnapshot(false, [], []),
                SampleGemini = () => Volatile.Read(ref geminiRunning) == 1
                    ? new AppActivitySnapshot(true, [9500], [9500])
                    : new AppActivitySnapshot(false, [], []),
                CustomWakeLauncher = arg =>
                {
                    wokenArg = arg;
                    wakeSignal.Set();
                    return true;
                }
            };

            var watcherTask = Task.Run(() => AppFollowService.RunFollowWatcherCore(options));
            Thread.Sleep(70);

            Volatile.Write(ref geminiRunning, 1);
            Assert(wakeSignal.Wait(1500), "Watcher must wake pet when Gemini Desktop App cold-starts.");
            Assert(wokenArg == "--woken-by:gemini", $"Expected --woken-by:gemini, got '{wokenArg}'.");

            // Now disable both follow switches in config.ini and ensure watcher auto-exits
            Thread.Sleep(30);
            svc.Save(new PetSettings { FollowAntigravity = false, FollowGeminiApp = false });
            File.SetLastWriteTimeUtc(svc.SettingsFilePath, DateTime.UtcNow.AddSeconds(2));

            Assert(watcherTask.Wait(2000) && watcherTask.Result == 0, "Watcher must auto-exit when all follow switches are disabled in config.ini.");
        }
        finally
        {
            AppFollowService.SignalStopFollowWatcher(stopEventName);
            TryDeleteTempDir(tempDir);
        }
    }

    private static void TestWatcherSingleInstanceDeduplication()
    {
        string tempDir = Path.Combine(Path.GetTempPath(), "GeminiPetTest_Watch4_" + Guid.NewGuid().ToString("N"));
        string id = Guid.NewGuid().ToString("N");
        string watcherMutexName = $"GeminiDesktopPet_Test_{id}_Watcher";
        string stopEventName = $"GeminiDesktopPet_Test_{id}_Stop";

        try
        {
            var svc = new SettingsService(tempDir);
            svc.Save(new PetSettings { FollowAntigravity = true, FollowGeminiApp = true });

            var options = new FollowWatcherOptions
            {
                FromExit = true,
                CustomSettingsFolder = tempDir,
                MainMutexName = $"GeminiDesktopPet_Test_{id}_Main",
                WatcherMutexName = watcherMutexName,
                StopWatcherEventName = stopEventName,
                PollIntervalMs = 40,
                SampleAntigravity = () => new AppActivitySnapshot(false, [], []),
                SampleGemini = () => new AppActivitySnapshot(false, [], [])
            };

            var firstTask = Task.Run(() => AppFollowService.RunFollowWatcherCore(options));
            Thread.Sleep(80);

            // Second instance with same WatcherMutexName must return 0 immediately
            var secondTask = Task.Run(() => AppFollowService.RunFollowWatcherCore(options));
            Assert(secondTask.Wait(1000) && secondTask.Result == 0, "Duplicate watcher instance must exit immediately.");
            Assert(!firstTask.IsCompleted, "Primary watcher instance must remain active.");

            AppFollowService.SignalStopFollowWatcher(stopEventName);
            Assert(firstTask.Wait(2000), "Primary watcher instance should stop cleanly.");
        }
        finally
        {
            AppFollowService.SignalStopFollowWatcher(stopEventName);
            TryDeleteTempDir(tempDir);
        }
    }

    private static void TryDeleteTempDir(string path)
    {
        try
        {
            if (Directory.Exists(path))
                Directory.Delete(path, true);
        }
        catch
        {
        }
    }
}
