@echo off
chcp 65001 >nul
setlocal
set "ROOT=%~dp0"
set "LOCAL_EXE=%ROOT%GeminiDesktopPet.exe"
set "REL_EXE=%ROOT%bin\Release\net10.0-windows\GeminiDesktopPet.exe"
set "DBG_EXE=%ROOT%bin\Debug\net10.0-windows\GeminiDesktopPet.exe"

if not exist "%APPDATA%\GeminiDesktopPet" mkdir "%APPDATA%\GeminiDesktopPet" >nul 2>&1

if exist "%LOCAL_EXE%" (
    >"%APPDATA%\GeminiDesktopPet\pet_path.txt" echo %LOCAL_EXE%
    call "%ROOT%Scripts\StartPet.bat" %*
    exit /b %errorlevel%
)
if exist "%REL_EXE%" (
    >"%APPDATA%\GeminiDesktopPet\pet_path.txt" echo %REL_EXE%
    call "%ROOT%Scripts\StartPet.bat" %*
    exit /b %errorlevel%
)
if exist "%DBG_EXE%" (
    >"%APPDATA%\GeminiDesktopPet\pet_path.txt" echo %DBG_EXE%
    call "%ROOT%Scripts\StartPet.bat" %*
    exit /b %errorlevel%
)

if exist "%ROOT%GeminiDesktopPet.slnx" (
    echo [GeminiDesktopPet] 未检测到编译产物，正在尝试构建 Release...
    dotnet build "%ROOT%GeminiDesktopPet.slnx" -c Release
    if exist "%REL_EXE%" (
        call "%ROOT%Scripts\StartPet.bat" %*
        exit /b %errorlevel%
    )
)

echo [GeminiDesktopPet] 未找到 GeminiDesktopPet.exe，请先构建项目或确认解压目录完整。
pause
exit /b 1
