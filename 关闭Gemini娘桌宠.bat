@echo off
chcp 65001 >nul
setlocal
echo [GeminiDesktopPet] 正在关闭 Gemini 娘桌宠...
call "%~dp0Scripts\StopPet.bat"
echo [GeminiDesktopPet] 已关闭。
exit /b 0
