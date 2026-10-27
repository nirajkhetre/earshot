@echo off
rem earshot setup: a private Python environment, the packages, and the voice models. Safe to run again.
rem   setup.bat /quiet   no questions, no pauses (for scripts)
setlocal
set "QUIET="
if /i "%~1"=="/quiet" set "QUIET=1"
cd /d "%~dp0"
echo.
echo   earshot setup
echo   =============
echo.

set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY where python >nul 2>nul && set "PY=python"
if not defined PY goto :nopython
%PY% -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)" || goto :oldpython
%PY% -c "import sys; print('  Python', sys.version.split()[0], 'at', sys.executable)"
%PY% -c "import sys; sys.exit(1 if 'WindowsApps' in sys.executable else 0)" || (
  echo.
  echo   Heads up: this is the Microsoft Store Python. Windows gives Store apps a private copy of AppData,
  echo   so start-with-Windows and your settings can end up somewhere unexpected. The python.org installer
  echo   avoids that: https://www.python.org/downloads/windows/
  echo.
)

if not exist ".venv\Scripts\python.exe" (
  echo   Creating the environment in .venv ...
  %PY% -m venv .venv || goto :fail
)
echo   Installing packages (a few minutes the first time) ...
".venv\Scripts\python.exe" -m pip install --upgrade pip --quiet || goto :fail
".venv\Scripts\python.exe" -m pip install -r requirements.txt --quiet || goto :fail

echo   Downloading the voice models (about 900 MB, once) ...
".venv\Scripts\python.exe" scripts\get_models.py || goto :fail

echo.
where ollama >nul 2>nul
if errorlevel 1 (
  echo   Ollama isn't installed. earshot works without it, but odd phrasings and offline answers need it:
  echo   install it from https://ollama.com, then run:  ollama pull gemma4:e2b
) else if defined QUIET (
  echo   Ollama found. For the on-device brain run:  ollama pull gemma4:e2b
) else (
  choice /c YN /m "  Download Gemma 4 E2B (about 4.6 GB) for the on-device brain now"
  if not errorlevel 2 ollama pull gemma4:e2b
)

echo.
echo   All set. Start earshot with start.bat, then say "hey Nova".
echo   (Rename it, pick a colour, add a free Gemini key: click the island, then settings.)
echo.
if not defined QUIET pause
exit /b 0

:nopython
echo   Python isn't installed. Get Python 3.11 or newer from https://www.python.org/downloads/windows/
echo   (tick "Add python.exe to PATH" in the installer), then run setup.bat again.
if not defined QUIET pause
exit /b 1

:oldpython
echo   earshot needs Python 3.11 or newer. Get it from https://www.python.org/downloads/windows/
if not defined QUIET pause
exit /b 1

:fail
echo.
echo   Setup stopped on an error (above). Check your internet connection and run setup.bat again.
if not defined QUIET pause
exit /b 1
