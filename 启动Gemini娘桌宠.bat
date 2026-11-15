@echo off
setlocal
set "EXE=%~dp0GeminiDesktopPet.exe"
if not exist "%EXE%" set "EXE=%~dp0bin\Release\net10.0-windows\GeminiDesktopPet.exe"
if not exist "%EXE%" set "EXE=%~dp0bin\Debug\net10.0-windows\GeminiDesktopPet.exe"
if exist "%EXE%" start "" "%EXE%"

