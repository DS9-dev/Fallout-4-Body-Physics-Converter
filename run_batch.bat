@echo off
rem Phase 3 package / batch commands, e.g.
rem   run_batch.bat analyze-package --profile NAME --package "Raider Punk CBBE - BT3 BT4 A-Body"
rem   run_batch.bat analyze-package --profile NAME --package "HN66Fo4_EasyGirl_BS" --allow-direct-nif   (Direct NIF, read-only)
rem   run_batch.bat plan-batch      --profile NAME --package "..." --select "Project A"
rem   run_batch.bat convert-batch   --profile NAME --package "..." --select "Project A" --yes
rem   run_batch.bat validate-batch  --profile NAME --batch ID
rem   run_batch.bat commit-batch    --profile NAME --batch ID --yes
rem   run_batch.bat verify-build    --profile NAME [--generated "X - 3BBB AUTO"]   (after the BodySlide build, read-only)
rem 0.5.0 mass conversion (what the GUI's Convert button does), resumable, with a package report:
rem   run_batch.bat convert-package      --profile NAME --package "Clothes Pack 5 CBBE BodySlide" [--mode all-possible] [--max-per-session 25] --yes
rem   run_batch.bat resume-batch         --profile NAME --batch ID --commit --yes [--manual]   (0.5.3: --manual = you press Execute)
rem   run_batch.bat package-report       --profile NAME --batch ID [--verify]
rem   run_batch.bat verify-package-build --profile NAME --package "..."        (after the BodySlide build, read-only)
rem   run_batch.bat export-diagnostics   --profile NAME [--batch ID]
rem   run_batch.bat probe-outfit-studio  --profile NAME     (0.5.3: can the converter drive Outfit Studio? runs nothing)
rem   run_batch.bat body-routes                            (0.6.0: body conversion routes; convert-package/analyze-package accept --source-body auto^|cbbe ... --target-body cbbe_3bbb)
rem   run_batch.bat acceptance-test-cloth --profile NAME --package "..." --project "..." [--output DIR]   (0.5.3 DEV/ACCEPTANCE: one cloth project, disposable, never installed)
rem   run_batch.bat report-runtime-issue --profile NAME --project "CP5_DonEb14n leggings v4" --state FAIL --note "no physics in game"
rem 0.4.1 Direct-NIF (game meshes without BodySlide projects), each stage explicit:
rem   run_batch.bat plan-direct-batch    --profile NAME --package "HN66Fo4_EasyGirl_BS" --allow-direct-nif --select "X.nif" [--select ...] --details
rem   run_batch.bat convert-direct-batch --profile NAME --package "HN66Fo4_EasyGirl_BS" --allow-direct-nif --select "X.nif" ... --plan PLANJSON --yes
rem   run_batch.bat commit-direct-batch  --profile NAME --batch ID --yes
rem   run_batch.bat verify-direct-build  --profile NAME
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" call setup_env.bat || exit /b 1
set "PYTHONPATH=%~dp0src"
".venv\Scripts\python.exe" -m cbbe3bbb %*
