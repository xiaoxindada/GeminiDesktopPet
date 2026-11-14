@echo off
setlocal
set "D=%~dp0"
set "PET_EXE="
if exist "%D%..\GeminiDesktopPet.exe" set "PET_EXE=%D%..\GeminiDesktopPet.exe"
if not defined PET_EXE if exist "%D%..\bin\Release\net10.0-windows\GeminiDesktopPet.exe" set "PET_EXE=%D%..\bin\Release\net10.0-windows\GeminiDesktopPet.exe"
if not defined PET_EXE if exist "%D%..\bin\Debug\net10.0-windows\GeminiDesktopPet.exe" set "PET_EXE=%D%..\bin\Debug\net10.0-windows\GeminiDesktopPet.exe"
if defined PET_EXE (
    "%PET_EXE%" --close-pet >nul 2>&1
    timeout /t 1 /nobreak >nul
)
tasklist /FI "IMAGENAME eq GeminiDesktopPet.exe" 2>nul | find /I "GeminiDesktopPet.exe" >nul
if not errorlevel 1 taskkill /F /IM GeminiDesktopPet.exe >nul 2>&1
exit /b 0
