@echo off
chcp 65001 >nul
setlocal EnableExtensions

set "ROOT=%~dp0"
set "CSPROJ=%ROOT%GeminiDesktopPet.csproj"
set "VERSION_FILE=%ROOT%VERSION"
set "RUNTIME=win-x64"
set "SELF_CONTAINED=true"
set "NO_PAUSE=0"

:parse_args
if "%~1"=="" goto :args_done
if /i "%~1"=="--fd" (
    set "SELF_CONTAINED=false"
) else if /i "%~1"=="-FrameworkDependent" (
    set "SELF_CONTAINED=false"
) else if /i "%~1"=="--nopause" (
    set "NO_PAUSE=1"
) else if /i "%~1"=="-NoPause" (
    set "NO_PAUSE=1"
)
shift
goto :parse_args

:args_done
if not exist "%CSPROJ%" (
    echo [ERROR] Project file not found: "%CSPROJ%"
    goto :fail
)

set "APP_VERSION=0.1.0"
if exist "%VERSION_FILE%" (
    for /f "usebackq tokens=* delims= " %%V in ("%VERSION_FILE%") do (
        if not "%%~V"=="" (
            set "APP_VERSION=%%~V"
            goto :version_done
        )
    )
)
:version_done
if /i "%APP_VERSION:~0,1%"=="v" set "APP_VERSION=%APP_VERSION:~1%"

if /i "%SELF_CONTAINED%"=="true" (
    set "MODE_TAG=%RUNTIME%"
) else (
    set "MODE_TAG=%RUNTIME%-fd"
)

set "OUTPUT_BASE=%ROOT%publish"
set "STAGE_DIR=%OUTPUT_BASE%\GeminiDesktopPet"
set "ZIP_PATH=%OUTPUT_BASE%\GeminiDesktopPet-v%APP_VERSION%-%MODE_TAG%.zip"
set "GIT_SUMMARY_PATH=%OUTPUT_BASE%\git-commit-summary.md"

echo [GeminiDesktopPet] 开始编译发布独立应用包 (v%APP_VERSION%) 并生成 Git 提交摘要与 ZIP 压缩包...
echo [1/6] Stopping any running GeminiDesktopPet instances...
taskkill /F /IM GeminiDesktopPet.exe >nul 2>nul
powershell -NoProfile -NonInteractive -Command "Start-Sleep -Seconds 1" >nul 2>nul

echo [2/6] Preparing staging folder: "%STAGE_DIR%"
if exist "%STAGE_DIR%" (
    del /f /s /q "%STAGE_DIR%\*" >nul 2>nul
    rmdir /s /q "%STAGE_DIR%" >nul 2>nul
)
if exist "%ZIP_PATH%" del /f /q "%ZIP_PATH%"
if exist "%GIT_SUMMARY_PATH%" del /f /q "%GIT_SUMMARY_PATH%"
if not exist "%OUTPUT_BASE%" mkdir "%OUTPUT_BASE%"
if not exist "%STAGE_DIR%" mkdir "%STAGE_DIR%"

echo [3/6] Running dotnet publish (Release / %RUNTIME% / SelfContained=%SELF_CONTAINED%)...
dotnet publish "%CSPROJ%" -c Release -r %RUNTIME% --self-contained %SELF_CONTAINED% -p:DebugType=none -p:DebugSymbols=false -o "%STAGE_DIR%"
if errorlevel 1 (
    echo [ERROR] dotnet publish failed.
    goto :fail
)

echo [4/6] Placing user scripts ^& docs together with GeminiDesktopPet.exe...
for %%F in (
    "启动Gemini娘桌宠.bat"
    "关闭Gemini娘桌宠.bat"
    "README.md"
    "VERSION"
) do (
    if not exist "%ROOT%%%~F" (
        echo [ERROR] Required file missing: "%ROOT%%%~F"
        goto :fail
    )
    copy /y "%ROOT%%%~F" "%STAGE_DIR%\%%~F" >nul
    if errorlevel 1 (
        echo [ERROR] Failed to copy "%%~F"
        goto :fail
    )
)

echo [Security] Running static ^& staged artifact security check...
powershell -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "%ROOT%tools\SecurityCheck.ps1" -RepoRoot "%ROOT%." -StageDir "%STAGE_DIR%"
if errorlevel 1 (
    echo [ERROR] Security check failed. Aborting package creation.
    goto :fail
)

if /i not "%GITHUB_ACTIONS%"=="true" if /i not "%CI%"=="true" (
    echo [Test] Running local follow-app startup regression tests...
    powershell -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "%ROOT%tools\TestFollowStartup.ps1" -RepoRoot "%ROOT%." -ExePath "%STAGE_DIR%\GeminiDesktopPet.exe"
    if errorlevel 1 (
        echo [ERROR] Follow-app startup regression test failed. Aborting package creation.
        goto :fail
    )
)

echo [5/6] Generating git commit summary: "%GIT_SUMMARY_PATH%"
powershell -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "%ROOT%tools\GenerateGitSummary.ps1" -RepoRoot "%ROOT%." -Version "%APP_VERSION%" -OutputPath "%GIT_SUMMARY_PATH%"
if errorlevel 1 (
    echo [ERROR] Failed to generate git commit summary.
    goto :fail
)

echo [6/6] Creating release ZIP archive: "%ZIP_PATH%"
set "SEVEN_ZIP="
if exist "%ProgramFiles%\7-Zip\7z.exe" set "SEVEN_ZIP=%ProgramFiles%\7-Zip\7z.exe"
if not defined SEVEN_ZIP (
    for /f "delims=" %%Z in ('where 7z.exe 2^>nul') do if not defined SEVEN_ZIP set "SEVEN_ZIP=%%Z"
)
if not defined SEVEN_ZIP goto :zip_try_tar
pushd "%STAGE_DIR%"
"%SEVEN_ZIP%" a -tzip -mcu=on "%ZIP_PATH%" * >nul
set "ZIP_EXIT=%errorlevel%"
popd
if "%ZIP_EXIT%"=="0" if exist "%ZIP_PATH%" goto :zip_done
if exist "%ZIP_PATH%" del /f /q "%ZIP_PATH%" >nul 2>nul

:zip_try_tar
pushd "%STAGE_DIR%"
"%SystemRoot%\System32\tar.exe" -a -cf "%ZIP_PATH%" *
set "ZIP_EXIT=%errorlevel%"
popd
if "%ZIP_EXIT%"=="0" if exist "%ZIP_PATH%" goto :zip_done
if exist "%ZIP_PATH%" del /f /q "%ZIP_PATH%" >nul 2>nul

powershell -NoProfile -NonInteractive -ExecutionPolicy Bypass -Command "Add-Type -AssemblyName System.IO.Compression.FileSystem; [System.IO.Compression.ZipFile]::CreateFromDirectory($env:STAGE_DIR, $env:ZIP_PATH, [System.IO.Compression.CompressionLevel]::Optimal, $false, [System.Text.Encoding]::UTF8)"
if errorlevel 1 (
    echo [ERROR] Release ZIP archive creation failed.
    goto :fail
)
:zip_done

echo.
echo === Publish ^& Package Complete ===
echo Version   : v%APP_VERSION%
echo Stage Dir : %STAGE_DIR%
echo Summary   : %GIT_SUMMARY_PATH%
for %%Z in ("%ZIP_PATH%") do echo ZIP File  : %%~fZ ^(%%~zZ bytes^)
echo Root entries in package:
dir /b "%STAGE_DIR%"
echo.
echo [GeminiDesktopPet] 全部打包完成！产物位于 "%OUTPUT_BASE%\" 目录。
if "%NO_PAUSE%"=="0" pause
exit /b 0

:fail
echo.
echo [GeminiDesktopPet] 发布或打包失败，请检查上方错误信息。
if "%NO_PAUSE%"=="0" pause
exit /b 1
