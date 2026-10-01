@echo off
rem Re-check an existing conversion workspace WITHOUT rerunning Outfit Studio:
rem   run_revalidate.bat --profile NAME --workspace "work\conversions\<id>"
rem Add --commit to write it to the output mod when every check passes.
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" call setup_env.bat || exit /b 1
set "PYTHONPATH=%~dp0src"
".venv\Scripts\python.exe" -m cbbe3bbb revalidate %*
