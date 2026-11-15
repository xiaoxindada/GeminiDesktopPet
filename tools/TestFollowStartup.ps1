param(
    [string]$RepoRoot = "",
    [string]$ExePath = ""
)

$ErrorActionPreference = "Stop"

if ([string]::IsNullOrWhiteSpace($RepoRoot)) {
    $RepoRoot = Split-Path -Parent $PSScriptRoot
}
$RepoRoot = (Resolve-Path $RepoRoot).Path

Write-Host "[FollowStartupTest] Repository root: $RepoRoot" -ForegroundColor Cyan

$failures = New-Object System.Collections.Generic.List[string]

function Assert-Condition {
    param(
        [bool]$Condition,
        [string]$Label
    )
    if ($Condition) {
        Write-Host "  [PASS] $Label" -ForegroundColor Green
    } else {
        Write-Host "  [FAIL] $Label" -ForegroundColor Red
        $script:failures.Add($Label)
    }
}

# 1. Static source invariants that prevent past follow-startup regressions from returning
Write-Host "[FollowStartupTest] Phase 1: Verifying static source invariants..." -ForegroundColor Cyan

$petSettingsPath = Join-Path $RepoRoot "Models\PetSettings.cs"
$settingsSvcPath = Join-Path $RepoRoot "Services\SettingsService.cs"
$programPath = Join-Path $RepoRoot "Program.cs"
$appXamlCsPath = Join-Path $RepoRoot "App.xaml.cs"
$followSvcPath = Join-Path $RepoRoot "Services\AppFollowService.cs"
$watcherSvcPath = Join-Path $RepoRoot "Services\AntigravityWatcherService.cs"

$petSettingsText = [System.IO.File]::ReadAllText($petSettingsPath, [System.Text.Encoding]::UTF8)
$settingsSvcText = [System.IO.File]::ReadAllText($settingsSvcPath, [System.Text.Encoding]::UTF8)
$programText = [System.IO.File]::ReadAllText($programPath, [System.Text.Encoding]::UTF8)
$appXamlCsText = [System.IO.File]::ReadAllText($appXamlCsPath, [System.Text.Encoding]::UTF8)
$followSvcText = [System.IO.File]::ReadAllText($followSvcPath, [System.Text.Encoding]::UTF8)
$watcherSvcText = [System.IO.File]::ReadAllText($watcherSvcPath, [System.Text.Encoding]::UTF8)

Assert-Condition ($petSettingsText -match "FollowAntigravity\s*\{\s*get;\s*set;\s*\}\s*=\s*true") `
    "PetSettings.FollowAntigravity defaults to true"
Assert-Condition ($petSettingsText -match "FollowGeminiApp\s*\{\s*get;\s*set;\s*\}\s*=\s*true") `
    "PetSettings.FollowGeminiApp defaults to true"
Assert-Condition ($settingsSvcText -match '--follow-watcher') `
    "SettingsService registers --follow-watcher in HKCU Run"
Assert-Condition ($programText -match '--follow-watcher' -and $programText -match 'RunFollowWatcher') `
    "Program.Main dispatches --follow-watcher before WPF initialization"
Assert-Condition ($programText -match '--close-pet' -and $programText -match 'SpawnFollowWatcher\(fromExit:\s*true\)') `
    "Program.Main --close-pet preserves/spawns background FollowWatcher when enabled"

# Ensure App.OnStartup does NOT kill the background FollowWatcher upon GUI launch
$onStartupBlock = $appXamlCsText
$wireIdx = $appXamlCsText.IndexOf("private void WireEvents()")
if ($wireIdx -gt 0) {
    $onStartupBlock = $appXamlCsText.Substring(0, $wireIdx)
}
Assert-Condition ($onStartupBlock -notmatch 'SignalStopFollowWatcher') `
    "App.OnStartup does not prematurely terminate resident FollowWatcher"
Assert-Condition ($appXamlCsText -match 'SpawnFollowWatcher\(fromExit:\s*true\)') `
    "App.GracefullyExit hands off to FollowWatcher on exit"

# Ensure FollowWatcher loop stays resident when MainInstance is running instead of breaking out
Assert-Condition ($followSvcText -notmatch 'if\s*\(\s*IsMainInstanceRunning\([^\)]*\)\s*\)\s*\{\s*[^\}]*break;') `
    "AppFollowService watcher loop stays resident while main GUI is running"
Assert-Condition ($watcherSvcText -match 'MinActiveElectronThreadCount\s*=\s*22' -and $watcherSvcText -match 'MinPrimaryBrowserThreadCount\s*=\s*38') `
    "AntigravityWatcherService enforces thread thresholds for orphaned Node filtering"

# 2. Execute built-in binary state-machine & multi-threaded watcher integration tests
Write-Host "[FollowStartupTest] Phase 2: Running compiled FollowStartupSelfTest suite..." -ForegroundColor Cyan

if ([string]::IsNullOrWhiteSpace($ExePath)) {
    $candidates = @(
        (Join-Path $RepoRoot "publish\GeminiDesktopPet\GeminiDesktopPet.exe"),
        (Join-Path $RepoRoot "bin\Release\net10.0-windows\win-x64\GeminiDesktopPet.exe"),
        (Join-Path $RepoRoot "bin\Release\net10.0-windows\GeminiDesktopPet.exe"),
        (Join-Path $RepoRoot "bin\Debug\net10.0-windows\GeminiDesktopPet.exe")
    )
    foreach ($c in $candidates) {
        if (Test-Path $c -PathType Leaf) {
            $ExePath = $c
            break
        }
    }
}

if ([string]::IsNullOrWhiteSpace($ExePath) -or -not (Test-Path $ExePath -PathType Leaf)) {
    Write-Host "[FollowStartupTest] Building Release binary for test execution..." -ForegroundColor Yellow
    & dotnet build (Join-Path $RepoRoot "GeminiDesktopPet.slnx") -c Release --nologo
    if ($LASTEXITCODE -ne 0) {
        throw "dotnet build failed with exit code $LASTEXITCODE."
    }
    $ExePath = Join-Path $RepoRoot "bin\Release\net10.0-windows\GeminiDesktopPet.exe"
}

$ExePath = (Resolve-Path $ExePath).Path
Write-Host "[FollowStartupTest] Target executable: $ExePath" -ForegroundColor Cyan

$reportFile = Join-Path ([System.IO.Path]::GetTempPath()) ("GeminiPet_FollowTestReport_" + [Guid]::NewGuid().ToString("N") + ".log")
try {
    $proc = Start-Process -FilePath $ExePath -ArgumentList @("--test-follow-startup", "`"$reportFile`"") -Wait -PassThru
    if (Test-Path $reportFile) {
        $lines = Get-Content -Path $reportFile -Encoding UTF8
        foreach ($line in $lines) {
            if ($line -match "^\[PASS\]") {
                Write-Host "  $line" -ForegroundColor Green
            } elseif ($line -match "^\[FAIL\]") {
                Write-Host "  $line" -ForegroundColor Red
            } else {
                Write-Host "  $line" -ForegroundColor Cyan
            }
        }
    } else {
        $failures.Add("Self-test report file was not created by $ExePath --test-follow-startup")
    }

    if ($proc.ExitCode -ne 0) {
        $failures.Add("Binary self-test exited with non-zero code: $($proc.ExitCode)")
    }
} finally {
    if (Test-Path $reportFile) {
        Remove-Item -Force $reportFile -ErrorAction SilentlyContinue
    }
}

if ($failures.Count -gt 0) {
    Write-Host "[FollowStartupTest] FAILED with $($failures.Count) error(s):" -ForegroundColor Red
    foreach ($f in $failures) {
        Write-Host "  - $f" -ForegroundColor Red
    }
    exit 1
}

Write-Host "[FollowStartupTest] ALL CHECKS PASSED." -ForegroundColor Green
exit 0
