@echo off
REM Build a standalone Windows exe with PyInstaller.
setlocal EnableExtensions
cd /d "%~dp0"

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
REM --clean matters: PyInstaller reuses build\, and a stale bundle looks
REM exactly like a broken feature because the UI silently loses behaviour.
"%PYTHON%" -m PyInstaller --noconfirm --clean LocalAvatarFavourites.spec
if errorlevel 1 goto :err

echo.
echo Smoke-testing the executable...
REM The built exe, not the source. This is a GUI-subsystem binary (console=False),
REM so batch would not wait on it and errorlevel would belong to the launch, not
REM to the self-test. Go through Python's subprocess, which waits and captures
REM output, so a stale bundle fails the build instead of shipping.
"%PYTHON%" -c "import pathlib,subprocess,sys; p=subprocess.run([str(pathlib.Path('dist')/'LocalAvatarFavourites.exe'),'--selftest'],capture_output=True,text=True); sys.stdout.write(p.stdout or ''); sys.stderr.write(p.stderr or ''); sys.exit(p.returncode)"
if errorlevel 1 goto :stale

echo.
echo Done. Executable: dist\LocalAvatarFavourites.exe
pause
exit /b 0

:stale
echo.
echo The executable failed its self-test.
echo If it reports a stale stylesheet, delete the build\ directory and run again.
echo If it reports a missing module, the spec's datas or hiddenimports are out
echo of date relative to the code.
pause
exit /b 1

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