@echo off
setlocal
set "SCRIPT_DIR=%~dp0"
set "APPDATA_DIR=%APPDATA%\GeminiDesktopPet"
set "PET_PATH_FILE=%APPDATA_DIR%\pet_path.txt"
set "PET_EXE="

if not exist "%APPDATA_DIR%" mkdir "%APPDATA_DIR%" >nul 2>&1

if exist "%SCRIPT_DIR%..\GeminiDesktopPet.exe" set "PET_EXE=%SCRIPT_DIR%..\GeminiDesktopPet.exe"
if not defined PET_EXE if exist "%PET_PATH_FILE%" (
    for /f "usebackq delims=" %%P in ("%PET_PATH_FILE%") do (
        if exist "%%~P" set "PET_EXE=%%~P"
    )
)
if not defined PET_EXE if exist "%SCRIPT_DIR%..\bin\Release\net10.0-windows\GeminiDesktopPet.exe" set "PET_EXE=%SCRIPT_DIR%..\bin\Release\net10.0-windows\GeminiDesktopPet.exe"
if not defined PET_EXE if exist "%SCRIPT_DIR%..\bin\Debug\net10.0-windows\GeminiDesktopPet.exe" set "PET_EXE=%SCRIPT_DIR%..\bin\Debug\net10.0-windows\GeminiDesktopPet.exe"

if not defined PET_EXE exit /b 1

for %%F in ("%PET_EXE%") do set "PET_EXE=%%~fF"
>"%PET_PATH_FILE%" echo %PET_EXE%

for %%D in ("%PET_EXE%") do set "PET_DIR=%%~dpD"
start "" /D "%PET_DIR%" "%PET_EXE%" %*
exit /b 0
