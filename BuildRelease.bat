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
set "BUILD_WORK=%REPO_ROOT%\build\pyinstaller"

echo [1/5] Stopping any running GeminiDesktopPet instances...
"%PYTHON_EXE%" "%REPO_ROOT%\main.py" --close-pet --stop-all >nul 2>&1
taskkill /F /IM GeminiDesktopPet.exe >nul 2>&1
taskkill /F /IM CleanConfig.exe >nul 2>&1

echo [2/5] Preparing staging folder: %STAGE_DIR%
if exist "%STAGE_DIR%" rmdir /s /q "%STAGE_DIR%"
if exist "%BUILD_WORK%" rmdir /s /q "%BUILD_WORK%"
if not exist "%OUTPUT_BASE%" mkdir "%OUTPUT_BASE%"
if not exist "%BUILD_WORK%" mkdir "%BUILD_WORK%"

"%PYTHON_EXE%" -m tools.release_package resolve-version --repo-root "%REPO_ROOT%" --output "%BUILD_WORK%\version.txt"
if errorlevel 1 (
    echo [ERROR] Failed to resolve AppVersion or verify release files!
    set "EXIT_CODE=1"
    goto finish
)
set /p APP_VERSION=<"%BUILD_WORK%\version.txt"
if not defined APP_VERSION (
    echo [ERROR] Resolved APP_VERSION is empty!
    set "EXIT_CODE=1"
    goto finish
)

set "SPEC_PATH=%BUILD_WORK%\GeminiDesktopPet.spec"
"%PYTHON_EXE%" -m tools.release_package generate-spec --repo-root "%REPO_ROOT%" --output "%SPEC_PATH%"
if errorlevel 1 (
    echo [ERROR] Failed to generate PyInstaller spec file!
    set "EXIT_CODE=1"
    goto finish
)

set "ZIP_PATH=%OUTPUT_BASE%\GeminiDesktopPet-v%APP_VERSION%-win-x64.zip"
set "SUMMARY_PATH=%OUTPUT_BASE%\git-commit-summary.md"
if exist "%ZIP_PATH%" del /f /q "%ZIP_PATH%"
if exist "%SUMMARY_PATH%" del /f /q "%SUMMARY_PATH%"

echo [GeminiDesktopPet] Packaging Python standalone build (v%APP_VERSION%)...
echo [3/5] Building GeminiDesktopPet.exe ^& CleanConfig.exe (--onedir) via PyInstaller...
"%PYTHON_EXE%" -m PyInstaller ^
    --noconfirm ^
    --clean ^
    --distpath "%OUTPUT_BASE%" ^
    --workpath "%BUILD_WORK%\main" ^
    "%SPEC_PATH%"
if errorlevel 1 (
    echo [ERROR] PyInstaller build failed!
    set "EXIT_CODE=1"
    goto finish
)

echo [4/5] Copying release docs alongside GeminiDesktopPet.exe ^& CleanConfig.exe...
"%PYTHON_EXE%" -m tools.release_package finalize-stage --repo-root "%REPO_ROOT%" --stage-dir "%STAGE_DIR%"
if errorlevel 1 (
    echo [ERROR] Stage copy or layout check failed!
    set "EXIT_CODE=1"
    goto finish
)

echo [5/5] Generating git commit summary ^& creating release ZIP...
"%PYTHON_EXE%" -m tools.release_package create-archive --repo-root "%REPO_ROOT%" --stage-dir "%STAGE_DIR%" --version "%APP_VERSION%" --summary-path "%SUMMARY_PATH%" --zip-path "%ZIP_PATH%"
if errorlevel 1 (
    echo [ERROR] Release summary or ZIP archive generation failed!
    set "EXIT_CODE=1"
    goto finish
)

echo.
echo === Publish ^& Package Complete ===
echo Version   : v%APP_VERSION%
echo Stage Dir : %STAGE_DIR%
echo Summary   : %SUMMARY_PATH%
echo ZIP File  : %ZIP_PATH%
set "EXIT_CODE=0"

:finish
popd >nul
if "%NO_PAUSE%"=="0" (
    echo.
    pause
)
exit /b %EXIT_CODE%
