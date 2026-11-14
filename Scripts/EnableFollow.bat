@echo off
setlocal
set "D=%~dp0"
set "PET_EXE="
if exist "%D%..\GeminiDesktopPet.exe" set "PET_EXE=%D%..\GeminiDesktopPet.exe"
if not defined PET_EXE if exist "%D%..\bin\Release\net10.0-windows\GeminiDesktopPet.exe" set "PET_EXE=%D%..\bin\Release\net10.0-windows\GeminiDesktopPet.exe"
if not defined PET_EXE if exist "%D%..\bin\Debug\net10.0-windows\GeminiDesktopPet.exe" set "PET_EXE=%D%..\bin\Debug\net10.0-windows\GeminiDesktopPet.exe"
if not defined PET_EXE exit /b 1
"%PET_EXE%" --enable-follow >nul 2>&1
exit /b %errorlevel%
