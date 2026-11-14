@echo off
setlocal
set "SCRIPT_DIR=%~dp0"
set "APPDATA_DIR=%APPDATA%\GeminiDesktopPet"
set "PET_PATH_FILE=%APPDATA_DIR%\pet_path.txt"
set "PET_EXE="

tasklist /FI "IMAGENAME eq GeminiDesktopPet.exe" 2>nul | find /I "GeminiDesktopPet.exe" >nul
if errorlevel 1 exit /b 0

if exist "%SCRIPT_DIR%..\GeminiDesktopPet.exe" set "PET_EXE=%SCRIPT_DIR%..\GeminiDesktopPet.exe"
if not defined PET_EXE if exist "%PET_PATH_FILE%" (
    for /f "usebackq delims=" %%P in ("%PET_PATH_FILE%") do (
        if exist "%%~P" set "PET_EXE=%%~P"
    )
)
if not defined PET_EXE if exist "%SCRIPT_DIR%..\bin\Release\net10.0-windows\GeminiDesktopPet.exe" set "PET_EXE=%SCRIPT_DIR%..\bin\Release\net10.0-windows\GeminiDesktopPet.exe"
if not defined PET_EXE if exist "%SCRIPT_DIR%..\bin\Debug\net10.0-windows\GeminiDesktopPet.exe" set "PET_EXE=%SCRIPT_DIR%..\bin\Debug\net10.0-windows\GeminiDesktopPet.exe"

if defined PET_EXE (
    "%PET_EXE%" --close-pet >nul 2>&1
    ping 127.0.0.1 -n 2 >nul
)

tasklist /FI "IMAGENAME eq GeminiDesktopPet.exe" 2>nul | find /I "GeminiDesktopPet.exe" >nul
if errorlevel 1 exit /b 0

taskkill /IM GeminiDesktopPet.exe /T >nul 2>&1
ping 127.0.0.1 -n 2 >nul

tasklist /FI "IMAGENAME eq GeminiDesktopPet.exe" 2>nul | find /I "GeminiDesktopPet.exe" >nul
if errorlevel 1 exit /b 0

taskkill /F /IM GeminiDesktopPet.exe /T >nul 2>&1
exit /b 0
