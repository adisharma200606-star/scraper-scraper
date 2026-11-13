@echo off
rem One-time setup for Windows. Double-click this file, or run:  setup.bat
cd /d "%~dp0"

set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY (
  python -c "import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)" >nul 2>nul && set "PY=python"
)
if not defined PY goto nopython
%PY% --version

if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" -c "" >nul 2>nul || rmdir /s /q .venv
)
if not exist ".venv\Scripts\python.exe" (
  %PY% -m venv .venv || goto fail
)

echo Installing (takes a few seconds)...
".venv\Scripts\python.exe" -m pip install --quiet --disable-pip-version-check --upgrade pip || goto fail
".venv\Scripts\python.exe" -m pip install --quiet --disable-pip-version-check -e . || goto fail

echo.
echo Setup done. Try:
echo   .venv\Scripts\rscrape check     (see which sources work)
echo   .venv\Scripts\rscrape ui        (open the web page)
echo.
pause
exit /b 0

:nopython
echo Python 3 isn't installed. Get it from https://www.python.org/downloads/
echo During install, tick "Add python.exe to PATH". Then run this again.
pause
exit /b 1

:fail
echo.
echo Setup failed. Copy the messages above and ask for help.
pause
exit /b 1
