@echo off
rem Starts earshot without a console window. Run setup.bat once first.
cd /d "%~dp0"
if exist ".venv\Scripts\pythonw.exe" (
  start "" ".venv\Scripts\pythonw.exe" "%~dp0run.pyw"
) else (
  echo earshot isn't set up yet: run setup.bat first.
  pause
)
