@echo off
setlocal DisableDelayedExpansion

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

if not defined PYTHON_EXE (
    if exist "%REPO_ROOT%\.venv\Scripts\python.exe" (
        set "PYTHON_EXE=%REPO_ROOT%\.venv\Scripts\python.exe"
    ) else (
        for %%I in (python.exe) do set "PYTHON_EXE=%%~$PATH:I"
    )
)
if not defined PYTHON_EXE goto failed
for %%I in ("%PYTHON_EXE%") do set "PYTHON_EXE=%%~fI"

rem Prevent PyInstaller from collecting unrelated DLLs (such as ICU) from PATH.
set "PATH=%SystemRoot%\System32;%SystemRoot%"
set "PYTHONPATH="
set "PYTHONHOME="
set "PYTHONNOUSERSITE=1"
"%PYTHON_EXE%" -c "import sysconfig, PyInstaller; from PySide6 import QtCore, QtGui, QtWidgets; raise SystemExit(sysconfig.get_platform() != 'win-amd64')"
if errorlevel 1 (
    echo [ERROR] Build requires 64-bit Python with working PyInstaller and PySide6.
    echo         Run: uv sync --locked --extra build
    set "EXIT_CODE=1"
    goto finish
)

set "OUTPUT_BASE=%REPO_ROOT%\out"
set "STAGE_DIR=%OUTPUT_BASE%\GeminiDesktopPet"
set "BUILD_WORK=%REPO_ROOT%\build\pyinstaller"
set "PYINSTALLER_CONFIG_DIR=%BUILD_WORK%\cache"
set "WATCHER_DIR=%BUILD_WORK%\watcher"
set "CLEAN_DIR=%BUILD_WORK%\helper"
set "ICON=%REPO_ROOT%\Assets\AppIcon.ico"
set "SEVENZA=%REPO_ROOT%\tools\7za\7za.exe"

echo [1/6] Preparing staging folders...
if not exist "%OUTPUT_BASE%\" mkdir "%OUTPUT_BASE%"
if not exist "%BUILD_WORK%\spec\" mkdir "%BUILD_WORK%\spec"

set "APP_VERSION="
for /f "usebackq tokens=3" %%A in (`findstr /b "APP_VERSION" "%REPO_ROOT%\models\app_version.py"`) do set "APP_VERSION=%%~A"
if not defined APP_VERSION (
    echo [ERROR] Failed to read APP_VERSION from models\app_version.py!
    set "EXIT_CODE=1"
    goto finish
)
set "ZIP_PATH=%OUTPUT_BASE%\GeminiDesktopPet-v%APP_VERSION%-win-x64.zip"
set "TEMP_ZIP=%ZIP_PATH%.tmp"
if exist "%TEMP_ZIP%" del /q "%TEMP_ZIP%"
if exist "%TEMP_ZIP%" goto failed

echo [2/6] Building GeminiDesktopPet.exe (--onedir, --windowed)...
"%PYTHON_EXE%" -m PyInstaller ^
    --noconfirm ^
    --clean ^
    --noupx ^
    --onedir ^
    --distpath "%OUTPUT_BASE%" ^
    --workpath "%BUILD_WORK%\main" ^
    --specpath "%BUILD_WORK%\spec" ^
    --name GeminiDesktopPet ^
    --windowed ^
    --icon "%ICON%" ^
    --add-data "%REPO_ROOT%\Assets:Assets" ^
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
    "%REPO_ROOT%\main.py"
if errorlevel 1 (
    echo [ERROR] GeminiDesktopPet build failed!
    set "EXIT_CODE=1"
    goto finish
)

rem Lightweight standalone follow watcher copied to user config dir at runtime so the app folder is never locked.
echo [3/6] Building FollowWatcher.exe (--onefile, --windowed)...
"%PYTHON_EXE%" -m PyInstaller ^
    --noconfirm ^
    --clean ^
    --noupx ^
    --onefile ^
    --windowed ^
    --distpath "%WATCHER_DIR%" ^
    --workpath "%BUILD_WORK%\watcher_work" ^
    --specpath "%BUILD_WORK%\spec" ^
    --name FollowWatcher ^
    --icon "%ICON%" ^
    --exclude-module tkinter ^
    --exclude-module PySide6 ^
    --exclude-module shiboken6 ^
    --exclude-module google ^
    --exclude-module PIL ^
    --exclude-module requests ^
    --exclude-module urllib3 ^
    --exclude-module certifi ^
    --exclude-module charset_normalizer ^
    --exclude-module idna ^
    --exclude-module unittest ^
    "%REPO_ROOT%\follow_watcher.py"
if errorlevel 1 (
    echo [ERROR] FollowWatcher build failed!
    set "EXIT_CODE=1"
    goto finish
)

rem The helper is copied as a single executable, so it must carry its own runtime.
echo [4/6] Building CleanConfig.exe (--onefile, console)...
"%PYTHON_EXE%" -m PyInstaller ^
    --noconfirm ^
    --clean ^
    --noupx ^
    --onefile ^
    --distpath "%CLEAN_DIR%" ^
    --workpath "%BUILD_WORK%\clean" ^
    --specpath "%BUILD_WORK%\spec" ^
    --name CleanConfig ^
    --icon "%ICON%" ^
    --exclude-module tkinter ^
    --exclude-module PySide6 ^
    --exclude-module shiboken6 ^
    --exclude-module google ^
    --exclude-module PIL ^
    --exclude-module requests ^
    --exclude-module urllib3 ^
    --exclude-module certifi ^
    --exclude-module charset_normalizer ^
    --exclude-module idna ^
    --exclude-module unittest ^
    "%REPO_ROOT%\clean_config.py"
if errorlevel 1 (
    echo [ERROR] CleanConfig build failed!
    set "EXIT_CODE=1"
    goto finish
)

echo [5/6] Assembling release stage...
copy /Y "%WATCHER_DIR%\FollowWatcher.exe" "%STAGE_DIR%\_internal\FollowWatcher.exe" >nul
if errorlevel 1 (
    echo [ERROR] Failed to place FollowWatcher.exe into stage!
    set "EXIT_CODE=1"
    goto finish
)
copy /Y "%CLEAN_DIR%\CleanConfig.exe" "%STAGE_DIR%\CleanConfig.exe" >nul
if errorlevel 1 (
    echo [ERROR] Failed to place CleanConfig.exe into stage!
    set "EXIT_CODE=1"
    goto finish
)
for %%F in (README.md CREDITS.md LICENSE) do (
    copy /Y "%REPO_ROOT%\%%F" "%STAGE_DIR%\%%F" >nul
    if errorlevel 1 goto failed
)

echo [6/6] Creating release ZIP via 7za...
if not exist "%SEVENZA%" (
    echo [ERROR] 7za not found: %SEVENZA%
    set "EXIT_CODE=1"
    goto finish
)
pushd "%STAGE_DIR%" >nul
if errorlevel 1 goto failed
"%SEVENZA%" a -tzip "%TEMP_ZIP%" . -r -mx=9 >nul
set "ZIP_EXIT=%ERRORLEVEL%"
popd >nul
if not "%ZIP_EXIT%"=="0" goto failed
"%SEVENZA%" t -tzip "%TEMP_ZIP%" >nul
if errorlevel 1 goto failed
move /Y "%TEMP_ZIP%" "%ZIP_PATH%" >nul
if errorlevel 1 goto failed

echo.
echo === Package Complete ===
echo Version   : v%APP_VERSION%
echo Stage Dir : %STAGE_DIR%
echo ZIP File  : %ZIP_PATH%
set "EXIT_CODE=0"
goto finish

:failed
echo [ERROR] Release preparation, staging or archive verification failed.
set "EXIT_CODE=1"

:finish
popd >nul
if "%NO_PAUSE%"=="0" (
    echo.
    pause
)
exit /b %EXIT_CODE%
