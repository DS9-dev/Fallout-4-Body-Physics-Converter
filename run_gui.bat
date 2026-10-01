@echo off
rem Starts the Phase 1 analyzer GUI. Errors are also written to logs\app.log.
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" call setup_env.bat || exit /b 1
set "PYTHONPATH=%~dp0src"
".venv\Scripts\python.exe" -m cbbe3bbb gui %*
if errorlevel 1 pause
