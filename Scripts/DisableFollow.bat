@echo off
setlocal
set "SCRIPT_DIR=%~dp0"
set "APPDATA_DIR=%APPDATA%\GeminiDesktopPet"
set "STARTUP_VBS=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\GeminiDesktopPet_FollowWatcher.vbs"
set "TOKEN_FILE=%APPDATA_DIR%\watcher_token.txt"
set "PET_PATH_FILE=%APPDATA_DIR%\pet_path.txt"

set "FROM_APP=0"
for %%A in (%*) do (
    if /I "%%~A"=="/from-app" set "FROM_APP=1"
)

if "%FROM_APP%"=="0" (
    set "PET_EXE="
    if exist "%SCRIPT_DIR%..\GeminiDesktopPet.exe" set "PET_EXE=%SCRIPT_DIR%..\GeminiDesktopPet.exe"
    if not defined PET_EXE if exist "%PET_PATH_FILE%" (
        for /f "usebackq delims=" %%P in ("%PET_PATH_FILE%") do (
            if exist "%%~P" set "PET_EXE=%%~P"
        )
    )
    if not defined PET_EXE if exist "%SCRIPT_DIR%..\bin\Release\net10.0-windows\GeminiDesktopPet.exe" set "PET_EXE=%SCRIPT_DIR%..\bin\Release\net10.0-windows\GeminiDesktopPet.exe"
    if not defined PET_EXE if exist "%SCRIPT_DIR%..\bin\Debug\net10.0-windows\GeminiDesktopPet.exe" set "PET_EXE=%SCRIPT_DIR%..\bin\Debug\net10.0-windows\GeminiDesktopPet.exe"
    if defined PET_EXE (
        "%PET_EXE%" --follow-flag:off >nul 2>&1
    )
)

if not exist "%APPDATA_DIR%" mkdir "%APPDATA_DIR%" >nul 2>&1
>"%TOKEN_FILE%" echo STOP

rem Wait briefly for wscript.exe watcher poll loop to observe STOP token and exit cleanly
ping 127.0.0.1 -n 3 >nul

if exist "%STARTUP_VBS%" del /f /q "%STARTUP_VBS%" >nul 2>&1
if exist "%APPDATA_DIR%\FollowAppsWatcher.vbs" del /f /q "%APPDATA_DIR%\FollowAppsWatcher.vbs" >nul 2>&1
if exist "%APPDATA_DIR%\StopFollowWatcher.vbs" del /f /q "%APPDATA_DIR%\StopFollowWatcher.vbs" >nul 2>&1

exit /b 0
