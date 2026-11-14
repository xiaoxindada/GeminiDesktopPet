@echo off
chcp 65001 >nul
setlocal
set "ROOT=%~dp0"
set "STARTUP_VBS=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\GeminiDesktopPet_FollowWatcher.vbs"

echo [GeminiDesktopPet] 正在动态生成 Startup VBS 跟随守护脚本...
call "%ROOT%Scripts\EnableFollow.bat"
if errorlevel 2 (
    echo [GeminiDesktopPet] 写入 shell:startup 失败（Access is denied / 被安全软件或权限拦截）：
    echo   %STARTUP_VBS%
    echo 请在安全软件弹窗中放行本目录或使用管理员/有权限会话重试。
    pause
    exit /b 2
)
if errorlevel 1 (
    echo [GeminiDesktopPet] 生成或启动失败，请确认 Scripts 目录完整。
    pause
    exit /b 1
)

echo [GeminiDesktopPet] 成功！已在 shell:startup 生成：
echo   %STARTUP_VBS%
echo [GeminiDesktopPet] 之后打开 Antigravity / Gemini 桌面 App 将通过 Scripts\StartPet.bat 自动拉起桌宠。
echo.
pause
exit /b 0
