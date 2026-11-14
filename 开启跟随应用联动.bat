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
"%PET_EXE%" --enable-follow
if errorlevel 1 (
    echo [GeminiDesktopPet] 启用跟随联动失败，请检查权限或设置文件是否可写。
    pause
    exit /b %errorlevel%
)
echo [GeminiDesktopPet] 已启用原生应用跟随联动并同步开机静默待机入口。
pause
exit /b 0
