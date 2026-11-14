@echo off
chcp 65001 >nul
setlocal
set "ROOT=%~dp0"
set "PET_EXE="
if exist "%ROOT%GeminiDesktopPet.exe" set "PET_EXE=%ROOT%GeminiDesktopPet.exe"
if not defined PET_EXE if exist "%ROOT%bin\Release\net10.0-windows\GeminiDesktopPet.exe" set "PET_EXE=%ROOT%bin\Release\net10.0-windows\GeminiDesktopPet.exe"
if not defined PET_EXE if exist "%ROOT%bin\Debug\net10.0-windows\GeminiDesktopPet.exe" set "PET_EXE=%ROOT%bin\Debug\net10.0-windows\GeminiDesktopPet.exe"
if not defined PET_EXE if exist "%ROOT%GeminiDesktopPet.slnx" (
    dotnet build "%ROOT%GeminiDesktopPet.slnx" -c Release
    if exist "%ROOT%bin\Release\net10.0-windows\GeminiDesktopPet.exe" set "PET_EXE=%ROOT%bin\Release\net10.0-windows\GeminiDesktopPet.exe"
)
if not defined PET_EXE (
    echo [GeminiDesktopPet] 未找到 GeminiDesktopPet.exe，请先构建项目或确认解压目录完整。
    pause
    exit /b 1
)
for %%D in ("%PET_EXE%") do set "PET_DIR=%%~dpD"
start "" /D "%PET_DIR%" "%PET_EXE%" %*
exit /b 0
