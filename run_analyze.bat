@echo off
rem Usage: run_analyze.bat --mo2 "T:\MO_Main _ baseCBBE" [--include-disabled]   or   run_analyze.bat --profile NAME
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" call setup_env.bat || exit /b 1
set "PYTHONPATH=%~dp0src"
".venv\Scripts\python.exe" -m cbbe3bbb analyze %*
