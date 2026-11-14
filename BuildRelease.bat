@echo off
chcp 65001 >nul
setlocal EnableExtensions

set "ROOT=%~dp0"
set "CSPROJ=%ROOT%GeminiDesktopPet.csproj"
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
for /f "tokens=1,2 delims=<> " %%A in ('findstr /i "<Version>" "%CSPROJ%"') do (
    if /i "%%A"=="Version" (
        set "APP_VERSION=%%B"
        goto :version_done
    )
)
:version_done

if /i "%SELF_CONTAINED%"=="true" (
    set "MODE_TAG=%RUNTIME%"
) else (
    set "MODE_TAG=%RUNTIME%-fd"
)

set "OUTPUT_BASE=%ROOT%publish"
set "STAGE_DIR=%OUTPUT_BASE%\GeminiDesktopPet"
set "ZIP_PATH=%OUTPUT_BASE%\GeminiDesktopPet-v%APP_VERSION%-%MODE_TAG%.zip"

echo [GeminiDesktopPet] 开始编译发布独立应用包并生成 ZIP 压缩包...
echo [1/5] Stopping any running GeminiDesktopPet instances...
taskkill /F /IM GeminiDesktopPet.exe >nul 2>nul
ping -n 2 127.0.0.1 >nul

echo [2/5] Preparing staging folder: "%STAGE_DIR%"
if exist "%STAGE_DIR%" (
    del /f /s /q "%STAGE_DIR%\*" >nul 2>nul
    rmdir /s /q "%STAGE_DIR%" >nul 2>nul
)
if exist "%ZIP_PATH%" del /f /q "%ZIP_PATH%"
if not exist "%OUTPUT_BASE%" mkdir "%OUTPUT_BASE%"
if not exist "%STAGE_DIR%" mkdir "%STAGE_DIR%"

echo [3/5] Running dotnet publish (Release / %RUNTIME% / SelfContained=%SELF_CONTAINED%)...
dotnet publish "%CSPROJ%" -c Release -r %RUNTIME% --self-contained %SELF_CONTAINED% -p:DebugType=none -p:DebugSymbols=false -o "%STAGE_DIR%"
if errorlevel 1 (
    echo [ERROR] dotnet publish failed.
    goto :fail
)

echo [4/5] Placing user scripts ^& docs together with GeminiDesktopPet.exe...
for %%F in (
    "启动Gemini娘桌宠.bat"
    "关闭Gemini娘桌宠.bat"
    "开启跟随应用联动.bat"
    "关闭跟随应用联动.bat"
    "README.md"
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

echo [5/5] Creating release ZIP archive: "%ZIP_PATH%"
pushd "%STAGE_DIR%"
tar -a -cf "%ZIP_PATH%" *
set "TAR_EXIT=%errorlevel%"
popd
if not "%TAR_EXIT%"=="0" (
    echo [ERROR] tar archive creation failed with exit code %TAR_EXIT%.
    goto :fail
)

echo.
echo === Publish ^& Package Complete ===
echo Stage Dir : %STAGE_DIR%
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
