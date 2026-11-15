param(
    [Parameter(Mandatory = $true)]
    [string]$RepoRoot,

    [Parameter(Mandatory = $true)]
    [string]$Version,

    [Parameter(Mandatory = $true)]
    [string]$OutputPath
)

$ErrorActionPreference = 'Stop'

$prevOutEnc = [Console]::OutputEncoding
$utf8NoBom = New-Object System.Text.UTF8Encoding($false)
[Console]::OutputEncoding = $utf8NoBom

function Invoke-Git {
    param([string[]]$GitArgs)
    $out = & git -C $RepoRoot -c i18n.logOutputEncoding=UTF-8 -c core.quotepath=false @GitArgs 2>$null
    return @{
        ExitCode = $LASTEXITCODE
        Lines    = @($out)
    }
}

try {
    $outDir = Split-Path -Parent $OutputPath
    if (-not [string]::IsNullOrWhiteSpace($outDir) -and -not (Test-Path $outDir)) {
        New-Item -ItemType Directory -Path $outDir -Force | Out-Null
    }

    $normalizedVer = $Version.Trim().TrimStart('v', 'V')
    $currentTag = "v$normalizedVer"

    $checkGit = Invoke-Git @('rev-parse', '--is-inside-work-tree')
    if ($checkGit.ExitCode -ne 0 -or ($checkGit.Lines -join '').Trim() -ne 'true') {
        $fallbackMd = @(
            "## 变更日志 ($currentTag)"
            ""
            "- 版本：``$currentTag``"
            "- 更新内容：当前目录不是 Git 仓库，无法采集变更列表。"
        ) -join "`n"
        [System.IO.File]::WriteAllText($OutputPath, $fallbackMd + "`n", $utf8NoBom)
        exit 0
    }

    $tagsRes = Invoke-Git @('tag', '--merged', 'HEAD', '--sort=-v:refname')
    $mergedTags = @($tagsRes.Lines | ForEach-Object { $_.Trim() } | Where-Object { $_ -ne '' })
    $prevTag = $mergedTags | Where-Object { $_ -ne $currentTag } | Select-Object -First 1

    $rangeArgs = @()
    $rangeDesc = ""
    if (-not [string]::IsNullOrWhiteSpace($prevTag)) {
        $rangeArgs = @("$prevTag..HEAD")
        $rangeDesc = "``$prevTag...$currentTag``"
    }
    elseif ($mergedTags -contains $currentTag) {
        $sinceCurr = Invoke-Git @('rev-list', '--count', "$currentTag..HEAD")
        $countAfterCurr = 0
        if ($sinceCurr.ExitCode -eq 0 -and $sinceCurr.Lines.Count -gt 0) {
            [int]::TryParse($sinceCurr.Lines[0].Trim(), [ref]$countAfterCurr) | Out-Null
        }
        if ($countAfterCurr -gt 0) {
            $rangeArgs = @("$currentTag..HEAD")
            $rangeDesc = "自 ``$currentTag`` 起的新增变更"
        }
        else {
            $rangeArgs = @('-n', '30', 'HEAD')
            $rangeDesc = "最近 30 条更新内容"
        }
    }
    else {
        $rangeArgs = @('-n', '30', 'HEAD')
        $rangeDesc = "最近 30 条更新内容"
    }

    function Test-IsReleaseVersionCommit {
        param([string]$Subject)

        if ([string]::IsNullOrWhiteSpace($Subject)) { return $false }
        $s = $Subject.Trim()

        # Exclude version-bump/release-marker subjects so release notes only list real changes.
        if ($s -match '^(?:发布|发版|版本发布)\s*(?:版本\s*)?v?\d+(?:\.\d+)+(?:[-+][0-9A-Za-z.\-]+)?(?:\s*版本)?$') { return $true }
        if ($s -match '(?i)^(?:(?:chore|build|ci|docs)(?:\([^)]+\))?\s*:\s*)?(?:release|bump(?:\s+version)?|version\s+bump)\s*(?:to\s+)?v?\d+(?:\.\d+)+(?:[-+][0-9A-Za-z.\-]+)?$') { return $true }

        return $false
    }

    # Only collect concise non-release commit subjects without commit IDs or timestamps.
    $logArgs = @('log') + $rangeArgs + @('--no-merges', '--pretty=format:%s')
    $logRes = Invoke-Git $logArgs
    if ($logRes.ExitCode -ne 0 -or $logRes.Lines.Count -eq 0) {
        $logArgs = @('log') + $rangeArgs + @('--pretty=format:%s')
        $logRes = Invoke-Git $logArgs
    }

    $commitLines = New-Object System.Collections.Generic.List[string]
    foreach ($line in $logRes.Lines) {
        $subject = $line.Trim()
        if ([string]::IsNullOrWhiteSpace($subject)) { continue }
        if (Test-IsReleaseVersionCommit -Subject $subject) { continue }
        $commitLines.Add("- $subject")
    }

    $mdLines = New-Object System.Collections.Generic.List[string]
    $mdLines.Add("## 变更日志 ($currentTag)")
    $mdLines.Add("")
    $mdLines.Add("- 版本：``$currentTag``")
    $mdLines.Add("- 对比范围：$rangeDesc")
    $mdLines.Add("- 变更条目：$($commitLines.Count) 条")
    $mdLines.Add("")
    if ($commitLines.Count -eq 0) {
        $mdLines.Add("- 暂无新增变更记录。")
    }
    else {
        $mdLines.Add("### 更新内容")
        foreach ($item in $commitLines) {
            $mdLines.Add($item)
        }
    }

    [System.IO.File]::WriteAllText($OutputPath, ($mdLines -join "`n") + "`n", $utf8NoBom)
    exit 0
}
finally {
    [Console]::OutputEncoding = $prevOutEnc
}
