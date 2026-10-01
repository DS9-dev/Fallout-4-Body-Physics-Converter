@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" call setup_env.bat || exit /b 1
set "PYTHONPATH=%~dp0src;%~dp0"
set "QT_QPA_PLATFORM=offscreen"
".venv\Scripts\python.exe" -m pytest -q %*
