@echo off
rem Creates .venv next to this file and installs the dependencies (numpy, scipy, PySide6, pytest).
rem Nothing outside this folder is changed. Set CBBE3BBB_PYTHON to force a specific python.exe.
setlocal
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" goto install
set "PY="
if defined CBBE3BBB_PYTHON set "PY="%CBBE3BBB_PYTHON%""
if defined PY goto make
call :try "%USERPROFILE%\MiniConda3\python.exe"
if defined PY goto make
call :try "%LOCALAPPDATA%\miniconda3\python.exe"
if defined PY goto make
call :try "%ProgramData%\miniconda3\python.exe"
if defined PY goto make
py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)" >nul 2>nul
if not errorlevel 1 set "PY=py -3"
if defined PY goto make
python -c "import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)" >nul 2>nul
if not errorlevel 1 set "PY=python"
if defined PY goto make
echo No Python 3.10+ found. Install Python or Miniconda, or set CBBE3BBB_PYTHON=C:\path\to\python.exe
exit /b 1
:make
echo Using %PY%
%PY% -m venv .venv
if errorlevel 1 exit /b 1
:install
".venv\Scripts\python.exe" -m pip install --upgrade pip
".venv\Scripts\python.exe" -m pip install -r requirements-dev.txt
if errorlevel 1 exit /b 1
echo.
echo Setup complete. Start the GUI with run_gui.bat
exit /b 0
:try
if not exist "%~1" exit /b 0
"%~1" -c "import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)" >nul 2>nul
if not errorlevel 1 set "PY="%~1""
exit /b 0
