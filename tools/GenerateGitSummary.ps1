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
    $generatedAt = (Get-Date).ToString('yyyy-MM-dd HH:mm:ss zzz')

    $checkGit = Invoke-Git @('rev-parse', '--is-inside-work-tree')
    if ($checkGit.ExitCode -ne 0 -or ($checkGit.Lines -join '').Trim() -ne 'true') {
        $fallbackMd = @(
            "## Git 提交摘要 ($currentTag)"
            ""
            "- 生成时间：$generatedAt"
            "- 提交记录：当前目录不是 Git 仓库，无法采集提交摘要。"
        ) -join "`n"
        [System.IO.File]::WriteAllText($OutputPath, $fallbackMd + "`n", $utf8NoBom)
        exit 0
    }

    $headRes = Invoke-Git @('rev-parse', '--short', 'HEAD')
    $headShort = if ($headRes.ExitCode -eq 0 -and $headRes.Lines.Count -gt 0) { $headRes.Lines[0].Trim() } else { 'unknown' }

    $tagsRes = Invoke-Git @('tag', '--merged', 'HEAD', '--sort=-v:refname')
    $mergedTags = @($tagsRes.Lines | ForEach-Object { $_.Trim() } | Where-Object { $_ -ne '' })
    $prevTag = $mergedTags | Where-Object { $_ -ne $currentTag } | Select-Object -First 1

    $rangeArgs = @()
    $rangeDesc = ""
    if (-not [string]::IsNullOrWhiteSpace($prevTag)) {
        $rangeArgs = @("$prevTag..HEAD")
        $rangeDesc = "``$prevTag..$headShort``"
    }
    elseif ($mergedTags -contains $currentTag) {
        $sinceCurr = Invoke-Git @('rev-list', '--count', "$currentTag..HEAD")
        $countAfterCurr = 0
        if ($sinceCurr.ExitCode -eq 0 -and $sinceCurr.Lines.Count -gt 0) {
            [int]::TryParse($sinceCurr.Lines[0].Trim(), [ref]$countAfterCurr) | Out-Null
        }
        if ($countAfterCurr -gt 0) {
            $rangeArgs = @("$currentTag..HEAD")
            $rangeDesc = "``$currentTag..$headShort``"
        }
        else {
            $rangeArgs = @('-n', '30', 'HEAD')
            $rangeDesc = "最近 30 条提交（截至 ``$headShort``）"
        }
    }
    else {
        $rangeArgs = @('-n', '30', 'HEAD')
        $rangeDesc = "最近 30 条提交（截至 ``$headShort``）"
    }

    $logArgs = @('log') + $rangeArgs + @('--no-merges', '--pretty=format:%h%x09%an%x09%ad%x09%s', '--date=short')
    $logRes = Invoke-Git $logArgs
    if ($logRes.ExitCode -ne 0 -or $logRes.Lines.Count -eq 0) {
        $logArgs = @('log') + $rangeArgs + @('--pretty=format:%h%x09%an%x09%ad%x09%s', '--date=short')
        $logRes = Invoke-Git $logArgs
    }

    $commitLines = New-Object System.Collections.Generic.List[string]
    foreach ($line in $logRes.Lines) {
        if ([string]::IsNullOrWhiteSpace($line)) { continue }
        $parts = $line -split "`t", 4
        if ($parts.Length -eq 4) {
            $hash = $parts[0].Trim()
            $author = $parts[1].Trim()
            $date = $parts[2].Trim()
            $subject = $parts[3].Trim()
            $commitLines.Add("- ``$hash`` $subject ($author, $date)")
        }
        else {
            $commitLines.Add("- $($line.Trim())")
        }
    }

    $mdLines = New-Object System.Collections.Generic.List[string]
    $mdLines.Add("## Git 提交摘要 ($currentTag)")
    $mdLines.Add("")
    $mdLines.Add("- 版本：``$currentTag``")
    $mdLines.Add("- 提交范围：$rangeDesc")
    $mdLines.Add("- 生成时间：$generatedAt")
    $mdLines.Add("- 提交数量：$($commitLines.Count) 条")
    $mdLines.Add("")
    if ($commitLines.Count -eq 0) {
        $mdLines.Add("- 暂无新增提交记录。")
    }
    else {
        $mdLines.Add("### 提交列表")
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
