@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo First run the installation commands in README.md to create .venv.
    pause
    exit /b 1
)
".venv\Scripts\python.exe" -m splash_patcher
if errorlevel 1 pause
