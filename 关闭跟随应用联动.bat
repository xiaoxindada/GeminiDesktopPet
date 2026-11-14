@echo off
chcp 65001 >nul
setlocal
set "ROOT=%~dp0"
set "PET_EXE="
if exist "%ROOT%GeminiDesktopPet.exe" set "PET_EXE=%ROOT%GeminiDesktopPet.exe"
if not defined PET_EXE if exist "%ROOT%bin\Release\net10.0-windows\GeminiDesktopPet.exe" set "PET_EXE=%ROOT%bin\Release\net10.0-windows\GeminiDesktopPet.exe"
if not defined PET_EXE if exist "%ROOT%bin\Debug\net10.0-windows\GeminiDesktopPet.exe" set "PET_EXE=%ROOT%bin\Debug\net10.0-windows\GeminiDesktopPet.exe"
if not defined PET_EXE (
    echo [GeminiDesktopPet] 未找到 GeminiDesktopPet.exe，请确认目录完整。
    pause
    exit /b 1
)
"%PET_EXE%" --disable-follow
echo [GeminiDesktopPet] 已关闭应用跟随联动并清理旧版 Startup 残留脚本。
pause
exit /b 0
