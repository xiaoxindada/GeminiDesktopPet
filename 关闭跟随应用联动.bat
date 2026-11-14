@echo off
chcp 65001 >nul
setlocal
set "ROOT=%~dp0"
set "STARTUP_VBS=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\GeminiDesktopPet_FollowWatcher.vbs"

echo [GeminiDesktopPet] 正在停止跟随守护并删除 shell:startup 中的 VBS 脚本...
call "%ROOT%Scripts\DisableFollow.bat"

if not exist "%STARTUP_VBS%" (
    echo [GeminiDesktopPet] 已停止联动守护，并已从 shell:startup 删除 GeminiDesktopPet_FollowWatcher.vbs。
) else (
    echo [GeminiDesktopPet] 守护已停止，但 Startup 文件仍存在（可能被占用），请重试。
)
echo.
pause
exit /b 0
