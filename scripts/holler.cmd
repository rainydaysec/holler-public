@echo off
REM Dev launcher for holler. Activates the local venv and runs the package.
setlocal
cd /d "%~dp0\.."
if not exist .venv\Scripts\python.exe (
    echo [holler] no venv at .venv\, run: python -m venv .venv ^&^& .\.venv\Scripts\Activate.ps1 ^&^& pip install -r requirements.txt
    exit /b 1
)
.venv\Scripts\python.exe -m holler %*
