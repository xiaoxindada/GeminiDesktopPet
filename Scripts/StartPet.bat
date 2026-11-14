@echo off
setlocal
set "D=%~dp0"
set "PET_EXE="
if exist "%D%..\GeminiDesktopPet.exe" set "PET_EXE=%D%..\GeminiDesktopPet.exe"
if not defined PET_EXE if exist "%D%..\bin\Release\net10.0-windows\GeminiDesktopPet.exe" set "PET_EXE=%D%..\bin\Release\net10.0-windows\GeminiDesktopPet.exe"
if not defined PET_EXE if exist "%D%..\bin\Debug\net10.0-windows\GeminiDesktopPet.exe" set "PET_EXE=%D%..\bin\Debug\net10.0-windows\GeminiDesktopPet.exe"
if not defined PET_EXE exit /b 1
for %%D in ("%PET_EXE%") do set "PET_DIR=%%~dpD"
start "" /D "%PET_DIR%" "%PET_EXE%" %*
exit /b 0
