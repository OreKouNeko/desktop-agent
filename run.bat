@echo off
rem First run creates a local venv and installs the package, then starts the UI.
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Creating virtual environment...
  py -3 -m venv .venv || goto :fail
  ".venv\Scripts\python.exe" -m pip install --quiet --upgrade pip
  ".venv\Scripts\python.exe" -m pip install --quiet -e . || goto :fail
)
start "" ".venv\Scripts\pythonw.exe" -m desktop_agent
exit /b 0
:fail
echo.
echo Setup failed. Make sure Python 3.10+ is installed and on PATH.
pause
