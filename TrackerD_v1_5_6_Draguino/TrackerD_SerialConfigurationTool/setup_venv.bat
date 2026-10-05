@echo off
setlocal
cd /d "%~dp0"

echo [1/3] Creating virtual environment...
py -3 -m venv .venv
if errorlevel 1 (
    echo Could not create the virtual environment.
    echo Make sure Python 3 is installed and the py launcher is available.
    pause
    exit /b 1
)

echo [2/3] Updating pip...
.venv\Scripts\python.exe -m pip install --upgrade pip
if errorlevel 1 goto :error

echo [3/3] Installing dependencies...
.venv\Scripts\python.exe -m pip install -r requirements.txt
if errorlevel 1 goto :error

echo.
echo Installation complete.
echo Run run.bat to start TrackerD GUI.
pause
exit /b 0

:error
echo Installation failed.
pause
exit /b 1
