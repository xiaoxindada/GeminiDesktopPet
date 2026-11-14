@echo off
chcp 65001 >nul
setlocal
set "ROOT=%~dp0"
set "PET_EXE="
if exist "%ROOT%GeminiDesktopPet.exe" set "PET_EXE=%ROOT%GeminiDesktopPet.exe"
if not defined PET_EXE if exist "%ROOT%bin\Release\net10.0-windows\GeminiDesktopPet.exe" set "PET_EXE=%ROOT%bin\Release\net10.0-windows\GeminiDesktopPet.exe"
if not defined PET_EXE if exist "%ROOT%bin\Debug\net10.0-windows\GeminiDesktopPet.exe" set "PET_EXE=%ROOT%bin\Debug\net10.0-windows\GeminiDesktopPet.exe"
echo [GeminiDesktopPet] 正在关闭 Gemini 娘桌宠...
if defined PET_EXE (
    "%PET_EXE%" --close-pet >nul 2>&1
    timeout /t 1 /nobreak >nul
)
tasklist /FI "IMAGENAME eq GeminiDesktopPet.exe" 2>nul | find /I "GeminiDesktopPet.exe" >nul
if not errorlevel 1 taskkill /F /IM GeminiDesktopPet.exe >nul 2>&1
echo [GeminiDesktopPet] 已关闭。
exit /b 0
