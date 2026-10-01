@echo off
rem Dry run:  run_convert.bat --profile NAME --project "MM69 Raider Punk Top - CBBE"
rem Convert:  add --yes   (optional: --os-dir "T:\Fallout 4 Tools\BodySlide 5.8.2" --save)
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" call setup_env.bat || exit /b 1
set "PYTHONPATH=%~dp0src"
set "CMD=plan"
for %%A in (%*) do if /i "%%~A"=="--yes" set "CMD=convert"
".venv\Scripts\python.exe" -m cbbe3bbb %CMD% %*
