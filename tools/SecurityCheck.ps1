param(
    [string]$RepoRoot = "",
    [string]$StageDir = ""
)

$ErrorActionPreference = "Stop"

if ([string]::IsNullOrWhiteSpace($RepoRoot)) {
    $RepoRoot = Split-Path -Parent $PSScriptRoot
}
$RepoRoot = (Resolve-Path $RepoRoot).Path

Write-Host "[SecurityCheck] Scanning repository: $RepoRoot"

function Get-ScanFiles {
    param([string]$Root)

    $gitDir = Join-Path $Root ".git"
    $extensions = @(".cs", ".bat", ".cmd", ".ps1", ".xaml", ".yml", ".yaml", ".vbs", ".vbe", ".jse", ".wsf", ".wsh", ".hta")
    $results = New-Object System.Collections.Generic.List[string]

    if ((Test-Path $gitDir) -and (Get-Command git -ErrorAction SilentlyContinue)) {
        $tracked = & git -C $Root -c core.quotepath=false ls-files --cached --others --exclude-standard
        if ($LASTEXITCODE -eq 0) {
            foreach ($rel in $tracked) {
                if ([string]::IsNullOrWhiteSpace($rel)) { continue }
                $full = Join-Path $Root ($rel -replace "/", "\")
                if (-not (Test-Path $full -PathType Leaf)) { continue }
                $ext = [System.IO.Path]::GetExtension($full).ToLowerInvariant()
                if ($extensions -contains $ext) {
                    $results.Add($full)
                }
            }
            return $results
        }
    }

    $skipPattern = "\\(\.git|bin|obj|publish)\\"
    foreach ($item in (Get-ChildItem -Path $Root -Recurse -File)) {
        if ($item.FullName -match $skipPattern) { continue }
        if ($extensions -contains $item.Extension.ToLowerInvariant()) {
            $results.Add($item.FullName)
        }
    }
    return $results
}

$scanFiles = Get-ScanFiles -Root $RepoRoot
$violations = New-Object System.Collections.Generic.List[string]

# 1. Forbidden script extensions anywhere in tracked source tree.
$forbiddenExts = @(".vbs", ".vbe", ".jse", ".wsf", ".wsh", ".hta")
foreach ($file in $scanFiles) {
    $ext = [System.IO.Path]::GetExtension($file).ToLowerInvariant()
    if ($forbiddenExts -contains $ext) {
        $rel = $file.Substring($RepoRoot.Length).TrimStart('\', '/')
        $violations.Add("Forbidden script extension tracked: $rel")
    }
}

# 2. High-risk API, script dropper, AV-trigger, and secret regex checks.
$rules = @(
    @{ Name = "Process memory / PEB inspection API"; Pattern = "NtQueryInformationProcess|NtReadVirtualMemory|ReadProcessMemory|WriteProcessMemory|VirtualAllocEx|CreateRemoteThread" },
    @{ Name = "Detached / breakaway native process API"; Pattern = "CreateProcessW|CreateProcessA|CREATE_BREAKAWAY_FROM_JOB|DETACHED_PROCESS" },
    @{ Name = "Legacy WScript / WMI scripting host"; Pattern = "\b(?:wscript|cscript)\b|winmgmts:|WScript\.Shell|Shell\.Application" },
    @{ Name = "Forbidden legacy VBS / batch updater symbol"; Pattern = "apply_update\.bat|FollowAppsWatcher\.template\.vbs|FollowWatcherScriptBuilder|VbsFollowService" },
    @{ Name = "Dynamic VBScript or batch file generation"; Pattern = "(?:WriteAllText|WriteAllLines|CreateText|StreamWriter)[^\r\n]*(?:\.vbs|\.bat|\.cmd)" },
    @{ Name = "Hidden WindowStyle process launch"; Pattern = "ProcessWindowStyle\.Hidden|-WindowStyle\s+Hidden" },
    @{ Name = "Raw CREATE_NO_WINDOW or cmd /c launcher"; Pattern = "CREATE_NO_WINDOW|cmd\.exe\s*/c" },
    @{ Name = "PowerShell obfuscation / remote exec"; Pattern = "Invoke-Expression|FromBase64String[^\r\n]*Invoke|DownloadString\s*\(" },
    @{ Name = "AV-flagged ping localhost sleep hack"; Pattern = "ping\s+(?:-n\s+\d+\s+)?127\.0\.0\.1|ping\s+127\.0\.0\.1" },
    @{ Name = "Hardcoded credential / API key / private key"; Pattern = "AIza[0-9A-Za-z_-]{35}|ghp_[0-9A-Za-z]{36}|github_pat_[0-9A-Za-z_]{20,}|-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----" }
)

# Strictly audited P/Invoke declarations allowed in source code.
$allowedDllImports = @{
    "kernel32.dll" = @("GetSystemTimes")
    "user32.dll"   = @("GetWindowLong", "SetWindowLong")
}

$selfFull = (Resolve-Path $PSCommandPath).Path

foreach ($file in $scanFiles) {
    if ([string]::Equals($file, $selfFull, [System.StringComparison]::OrdinalIgnoreCase)) {
        continue
    }

    $rel = $file.Substring($RepoRoot.Length).TrimStart('\', '/')
    $content = [System.IO.File]::ReadAllText($file, [System.Text.Encoding]::UTF8)

    foreach ($rule in $rules) {
        $matches = [regex]::Matches($content, $rule.Pattern, [System.Text.RegularExpressions.RegexOptions]::IgnoreCase)
        foreach ($m in $matches) {
            $lineNo = ($content.Substring(0, $m.Index) -split "`n").Count
            $violations.Add("[$($rule.Name)] ${rel}:${lineNo} -> '$($m.Value)'")
        }
    }

    if ($file.EndsWith(".cs", [System.StringComparison]::OrdinalIgnoreCase)) {
        $pinvokeMatches = [regex]::Matches(
            $content,
            '\[(?:DllImport|LibraryImport)\(\s*"([^"]+)"[^\]]*\)\]\s*(?:\[[^\]]+\]\s*)*(?:public|private|internal|protected|static|extern|partial|\s)+[A-Za-z0-9_<>\[\]\.\?]+\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(',
            [System.Text.RegularExpressions.RegexOptions]::IgnoreCase)
        foreach ($pm in $pinvokeMatches) {
            $dll = $pm.Groups[1].Value.ToLowerInvariant()
            $fn = $pm.Groups[2].Value
            $lineNo = ($content.Substring(0, $pm.Index) -split "`n").Count
            if (-not $allowedDllImports.ContainsKey($dll) -or ($allowedDllImports[$dll] -notcontains $fn)) {
                $violations.Add("[Unaudited P/Invoke] ${rel}:${lineNo} -> $dll!$fn")
            }
        }
    }
}

# 3. Optional published staging folder verification + Windows Defender scan when active.
if (-not [string]::IsNullOrWhiteSpace($StageDir)) {
    if (-not (Test-Path $StageDir -PathType Container)) {
        $violations.Add("Staging directory does not exist: $StageDir")
    } else {
        Write-Host "[SecurityCheck] Verifying staging output: $StageDir"
        foreach ($item in (Get-ChildItem -Path $StageDir -Recurse -File)) {
            $ext = $item.Extension.ToLowerInvariant()
            if ($forbiddenExts -contains $ext) {
                $violations.Add("Forbidden script packaged in staging output: $($item.FullName)")
            }
        }

        $defenderSvc = Get-Service -Name "WinDefend" -ErrorAction SilentlyContinue
        $mpCmd = Join-Path $env:ProgramFiles "Windows Defender\MpCmdRun.exe"
        if ($defenderSvc -and $defenderSvc.Status -eq "Running" -and (Test-Path $mpCmd)) {
            Write-Host "[SecurityCheck] Running Microsoft Defender scan on staging output..."
            & $mpCmd -Scan -ScanType 3 -File $StageDir
            if ($LASTEXITCODE -eq 2) {
                $violations.Add("Microsoft Defender reported threat(s) in staging output: $StageDir")
            } elseif ($LASTEXITCODE -ne 0) {
                Write-Warning "[SecurityCheck] MpCmdRun returned exit code $LASTEXITCODE (non-threat status); skipping fatal failure."
            } else {
                Write-Host "[SecurityCheck] Microsoft Defender scan passed with 0 threats."
            }
        } else {
            Write-Host "[SecurityCheck] WinDefend service not active; static + artifact checks completed."
        }
    }
}

if ($violations.Count -gt 0) {
    Write-Host "[SecurityCheck] FAILED with $($violations.Count) issue(s):" -ForegroundColor Red
    foreach ($v in $violations) {
        Write-Host "  - $v" -ForegroundColor Red
    }
    exit 1
}

Write-Host "[SecurityCheck] PASSED ($($scanFiles.Count) files verified, 0 violations)."
exit 0
