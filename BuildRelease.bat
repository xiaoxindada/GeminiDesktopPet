@echo off
setlocal enabledelayedexpansion

set "SCRIPT_DIR=%~dp0"
set "REPO_ROOT=%SCRIPT_DIR:~0,-1%"
pushd "%REPO_ROOT%" >nul

set "NO_PAUSE=0"

:parse_args
if "%~1"=="" goto args_done
if /i "%~1"=="--no-pause" set "NO_PAUSE=1"
if /i "%~1"=="--nopause" set "NO_PAUSE=1"
shift
goto parse_args
:args_done

set "PYTHON_EXE=python"
if exist "%REPO_ROOT%\.venv\Scripts\python.exe" (
    set "PYTHON_EXE=%REPO_ROOT%\.venv\Scripts\python.exe"
)

"%PYTHON_EXE%" -m PyInstaller --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] PyInstaller not found in %PYTHON_EXE%
    echo         Run: uv sync --extra build   or   pip install ".[build]"
    set "EXIT_CODE=1"
    goto finish
)

set "OUTPUT_BASE=%REPO_ROOT%\out"
set "STAGE_DIR=%OUTPUT_BASE%\GeminiDesktopPet"
set "CLEAN_DIR=%OUTPUT_BASE%\CleanConfig"
set "BUILD_WORK=%REPO_ROOT%\build\pyinstaller"
set "ICON=%REPO_ROOT%\Assets\AppIcon.ico"
set "SEVENZA=%REPO_ROOT%\tools\7za\7za.exe"

echo [1/5] Preparing staging folders...
if exist "%STAGE_DIR%" rmdir /s /q "%STAGE_DIR%"
if exist "%CLEAN_DIR%" rmdir /s /q "%CLEAN_DIR%"
if exist "%BUILD_WORK%" rmdir /s /q "%BUILD_WORK%"
if not exist "%OUTPUT_BASE%" mkdir "%OUTPUT_BASE%"
if not exist "%BUILD_WORK%\spec" mkdir "%BUILD_WORK%\spec"

set "APP_VERSION="
for /f "usebackq tokens=3" %%A in (`findstr /b "APP_VERSION" "%REPO_ROOT%\models\app_version.py"`) do set "APP_VERSION=%%~A"
if not defined APP_VERSION (
    echo [ERROR] Failed to read APP_VERSION from models\app_version.py!
    set "EXIT_CODE=1"
    goto finish
)
set "ZIP_PATH=%OUTPUT_BASE%\GeminiDesktopPet-v%APP_VERSION%-win-x64.zip"
if exist "%ZIP_PATH%" del /f /q "%ZIP_PATH%"

echo [2/5] Building GeminiDesktopPet.exe (--onedir, --windowed)...
"%PYTHON_EXE%" -m PyInstaller ^
    --noconfirm ^
    --clean ^
    --onedir ^
    --distpath "%OUTPUT_BASE%" ^
    --workpath "%BUILD_WORK%\main" ^
    --specpath "%BUILD_WORK%\spec" ^
    --name GeminiDesktopPet ^
    --windowed ^
    --icon "%ICON%" ^
    --add-data "%REPO_ROOT%\Assets;Assets" ^
    --collect-submodules google.genai ^
    --exclude-module tkinter ^
    --exclude-module PySide6.QtWebEngineCore ^
    --exclude-module PySide6.QtWebEngineWidgets ^
    --exclude-module PySide6.QtWebEngineQuick ^
    --exclude-module PySide6.QtPdf ^
    --exclude-module PySide6.QtPdfWidgets ^
    --exclude-module PySide6.QtQml ^
    --exclude-module PySide6.QtQuick ^
    --exclude-module PySide6.Qt3DCore ^
    --exclude-module PySide6.QtMultimedia ^
    --exclude-module PySide6.QtNetwork ^
    "%REPO_ROOT%\main.py"
if errorlevel 1 (
    echo [ERROR] GeminiDesktopPet build failed!
    set "EXIT_CODE=1"
    goto finish
)

echo [3/5] Building CleanConfig.exe (--onedir, console)...
"%PYTHON_EXE%" -m PyInstaller ^
    --noconfirm ^
    --clean ^
    --onedir ^
    --distpath "%OUTPUT_BASE%" ^
    --workpath "%BUILD_WORK%\clean" ^
    --specpath "%BUILD_WORK%\spec" ^
    --name CleanConfig ^
    --icon "%ICON%" ^
    --exclude-module tkinter ^
    --exclude-module PySide6 ^
    --exclude-module shiboken6 ^
    --exclude-module google ^
    --exclude-module PIL ^
    --exclude-module unittest ^
    "%REPO_ROOT%\clean_config.py"
if errorlevel 1 (
    echo [ERROR] CleanConfig build failed!
    set "EXIT_CODE=1"
    goto finish
)

echo [4/5] Assembling release stage...
if not exist "%STAGE_DIR%\CleanConfig.exe" (
    copy /Y "%CLEAN_DIR%\CleanConfig.exe" "%STAGE_DIR%\CleanConfig.exe" >nul
)
if errorlevel 1 (
    echo [ERROR] Failed to place CleanConfig.exe into stage!
    set "EXIT_CODE=1"
    goto finish
)
for %%F in (README.md CREDITS.md LICENSE) do (
    if not exist "%STAGE_DIR%\%%F" copy /Y "%REPO_ROOT%\%%F" "%STAGE_DIR%\%%F" >nul
)

echo [5/5] Creating release ZIP via 7za...
if not exist "%SEVENZA%" (
    echo [ERROR] 7za not found: %SEVENZA%
    set "EXIT_CODE=1"
    goto finish
)
pushd "%STAGE_DIR%" >nul
"%SEVENZA%" a -tzip "%ZIP_PATH%" . -r -mx=9 >nul
popd >nul
if errorlevel 1 (
    echo [ERROR] ZIP archive generation failed!
    set "EXIT_CODE=1"
    goto finish
)

echo.
echo === Package Complete ===
echo Version   : v%APP_VERSION%
echo Stage Dir : %STAGE_DIR%
echo ZIP File  : %ZIP_PATH%
set "EXIT_CODE=0"

:finish
popd >nul
if "%NO_PAUSE%"=="0" (
    echo.
    pause
)
exit /b %EXIT_CODE%
