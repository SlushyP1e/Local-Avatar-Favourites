@echo off
REM Build a standalone Windows exe with PyInstaller.
setlocal EnableExtensions

REM ---- locate Python (don't rely on PATH) ----
set "PYTHON="

where python >nul 2>nul
if not errorlevel 1 (
    python --version >nul 2>nul
    if not errorlevel 1 set "PYTHON=python"
)

if "%PYTHON%"=="" (
    where py >nul 2>nul
    if not errorlevel 1 (
        for /f "delims=" %%i in ('py -3 -c "import sys;print(sys.executable)" 2^>nul') do set "PYTHON=%%i"
    )
)

if "%PYTHON%"=="" (
    for %%p in (
        "%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
        "%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
        "%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
        "%ProgramFiles%\Python311\python.exe"
        "%ProgramFiles%\Python312\python.exe"
        "%ProgramFiles%\Python313\python.exe"
        "C:\Python311\python.exe"
        "C:\Python312\python.exe"
        "C:\Python313\python.exe"
    ) do (
        if exist %%p (
            set "PYTHON=%%p"
            goto :python_found
        )
    )
)

:python_found
if "%PYTHON%"=="" goto :nopython

echo Using Python: %PYTHON%
"%PYTHON%" --version
echo.

echo Installing dependencies...
"%PYTHON%" -m pip install -r requirements.txt
if errorlevel 1 goto :err
"%PYTHON%" -m pip install pyinstaller
if errorlevel 1 goto :err

tasklist /FI "IMAGENAME eq LocalAvatarFavourites.exe" 2>nul | find /I "LocalAvatarFavourites.exe" >nul
if not errorlevel 1 (
    echo.
    echo LocalAvatarFavourites.exe is currently running.
    echo Close it first, then re-run this script.
    echo.
    pause
    exit /b 1
)

echo.
echo Building executable...
"%PYTHON%" -m PyInstaller --noconfirm --onefile --windowed ^
  --name "LocalAvatarFavourites" ^
  --icon "assets\icon.ico" ^
  --add-data "app\web;web" ^
  --collect-all webview ^
  app\main.py
if errorlevel 1 goto :err

echo.
echo Done. Executable: dist\LocalAvatarFavourites.exe
pause
exit /b 0

:nopython
echo.
echo Python was not found.
echo.
echo Install Python 3.11+ from https://www.python.org/downloads/
echo and during setup tick "Add Python to PATH".
echo.
pause
exit /b 1

:err
echo.
echo Build failed.
pause
exit /b 1