$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$pythonExe = "C:\Users\luis.barahona\AppData\Roaming\uv\python\cpython-3.14.8-windows-x86_64-none\python.exe"

Write-Host "[1/3] Creating virtual environment..."
& $pythonExe -m venv .venv

Write-Host "[2/3] Updating pip..."
.\.venv\Scripts\python.exe -m pip install --upgrade pip

Write-Host "[3/3] Installing dependencies..."
.\.venv\Scripts\python.exe -m pip install -r requirements.txt

Write-Host ""
Write-Host "Installation complete."
Write-Host "Run .\run.bat to start TrackerD GUI."