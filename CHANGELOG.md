# Changelog

## 0.6.1 (stabilization: product folder rename, real-acceptance fixes, end-user README)

**0.6.1 MANUAL ACCEPTANCE: PASS (real PC, 2026-10-02).** The folder was renamed to `T:\Fallout 4 Tools\Fallout 4 Body &
Physics Converter` (old folder gone, no junction). From the new root: GUI title "Fallout 4 Body & Physics Converter
0.6.1"; Fallout 4, MO2, MO2 profile Fix F, CBBE 3BBB, the enabled output mod and Outfit Studio 5.8.2 detected; Vtaw
Wardrobe 4 analysed (38 projects: 23 existing generated conversions already compatible, 11 not needed, 4 cannot safely
process); `verify-package-build` 23/23 BUILD_OK, both AUTO groups complete; `--help` and `body-routes` correct; README
reviewed. Documentation follow-up: README notes the PowerShell `.\run_batch.bat` form.

Real GUI acceptance on CP4, Vtaw Overalls, Utility Pack 1, Wardrobe 4/5/6 (RESEARCH §24). Conversion semantics and the
strict validation are unchanged except for one proven defect:

- **Fixed (real defect, Vtaw Overalls Belts): exact shape names.** The OSP parser stripped spaces from shape names;
  the project lists `Metal Overalls ` (trailing space) exactly as its NIF does, so the analysis lost that part, Outfit
  Studio saved it anyway and validation rejected the result ("conversion result rejected", NEW shapes
  ['Metal Overalls ']). Names are now exact (only multi-line text is trimmed). Outfit Studio itself trims
  `TargetMeshes` entries, so a name with leading/trailing blanks or a comma is addressed with an exact anchored pattern
  (`TargetRegex`); normal names produce the same scripts as before. Analyzer cache version 1.2 (re-analysis).
- **WD5 Bodysuit V1/V2/V3/V6/V7 investigated, behaviour unchanged** (RESEARCH §24): the generated meshes match the
  planner's own prediction; the strict per-region rule rejects the sleeve part `arm` (breast 2 of 6 bones). Documented,
  wording improved; not changed without a decision.
- Window title: no duplicated product name (Qt application display name removed); `product_title()`.
- One version source: `cbbe3bbb.__version__`; `pyproject.toml` reads it (`dynamic = ["version"]`).
- Build Converted Outfits: the exact group large and selectable, numbered steps (group filter, preset, Build Morphs,
  choose "- 3BBB AUTO" on conflicts), the explicit overwrite warning, a note where BodySlide appears (no focus hacks).
- Check the build: one line per status (built correctly / not built / stale / mismatch / could not be checked); ERROR
  is now explained; BUILD_OK still requires the weight comparison.
- Needs attention: the failing strict check is shown (`failed_check`); new messages UNEXPECTED_SHAPE and a precise
  PHYSICS_MISSING ("expected physics bones missing (rejected)").
- Folder rename to `T:\Fallout 4 Tools\Fallout 4 Body & Physics Converter`: launchers are folder-relative and quote
  `%~dp0` ('&' safe), Outfit Studio's Config.xml is written with XML escaping, profiles store `tool_root` and move
  paths that pointed inside the former folder (`CBBE3BBBConverter`) when the same relative path exists in the new one.
  Internal identities unchanged (package `cbbe3bbb`, manifest `tool`, output mod, groups, profile names).
- CLI: `run_batch.bat --help` shows the product, version and the converter/MO2 profile distinction.
- README rewritten as the end-user manual; the former engineering README is `docs/DEVELOPMENT_HISTORY.md`.
- Tests: `tests/test_shape_names.py`, `tests/test_release.py` (version source, title, launchers, a full run from a
  folder named "Fallout 4 Body & Physics Converter", profile migration, messages).

## 0.6.0 (Body Architecture Foundation; product name "Fallout 4 Body & Physics Converter")

**0.5.3 UNATTENDED OUTFIT STUDIO: ACCEPTED** on real Windows (RESEARCH §21). That backend is unchanged.

- Product name **Fallout 4 Body & Physics Converter** in the GUI titles, README heading and report headers. Internal
  and serialised names unchanged: folder `CBBE3BBBConverter`, `run_batch.bat`, package `cbbe3bbb`, manifest `tool`
  id, output mod "CBBE 3BBB Auto Conversions", groups "CBBE 3BBB Auto Conversions - <package>", profiles.
- **Body identities** (`conversion/bodies.py`): stable ids + labels; `detect_body` turns the analyzer's existing
  family detection into a `BodyDetection` (body, confidence, evidence, conflicting evidence, source).
- **Routes** (`conversion/routes.py`): `ConversionRoute` with separate **geometry** and **physics** stages, validator,
  validation stages, limitations. CBBE → CBBE 3BBB = PRODUCTION (geometry not required, physics = the accepted
  backend); CBBE 3BBB / TWB 3BBB / "3BBB (body unknown)" → ALREADY_COMPATIBLE; TWB → CBBE 3BBB = LEGACY_SUPPORTED
  (0.5.x behaviour preserved); **Fusion Girl → CBBE → CBBE 3BBB = NOT_IMPLEMENTED** (geometry stage missing; never
  executable); BodyTalk/Male = UNSUPPORTED; Unknown = REVIEW.
- **Source / Target body**: Auto Detect (default) or an explicit source that only confirms the detection (a conflict
  → REVIEW `SOURCE_OVERRIDE_CONFLICT`, an undetermined body → `SOURCE_UNVERIFIED`; no expert override). Target: CBBE
  3BBB. Fusion Girl as source is refused before anything starts ("Fusion Girl geometry conversion is not implemented
  yet").
- Planner gate `package.apply_routing`; preview/plan/batch/manifest/result fields are additive (old records load;
  unknown preview keys ignored). Batch staging refuses a non-runnable route; validation records the route validator
  and fails for any validator that does not exist.
- GUI: Source Body (Auto Detect / CBBE / Fusion Girl / TWB) and Target Body (CBBE 3BBB); detected bodies and the
  route lines after Analyze; an unavailable route shows a warning and never starts.
- CLI: optional `--source-body` / `--target-body` on `convert-package`, `analyze-package`, `plan-batch`,
  `convert-batch` (defaults = 0.5.x); new `body-routes`. `--profile` stays the converter profile, `--mo2-profile`
  the MO2 profile.
- Reports: "Source body / Target body" and one line per route (status, geometry stage, physics stage) in the preview
  text/HTML, per-project body + route in the details, `route_*`/`source_body`/`target_body` in result items; user
  messages ROUTE_NOT_IMPLEMENTED ("Made for Fusion Girl…") and SOURCE_BODY_MISMATCH.
- Research: CBBE/3BBB hard-coding audit (RESEARCH §22) and the Fusion Girl route plan (RESEARCH §23).
- README is an interim document; the final end-user manual follows the real Fusion Girl and CBBE GUI acceptance.
- Tests: `tests/test_bodies.py` (tests 1–15 of the milestone, incl. "Auto Detect changes no 0.5.x classification").

## 0.5.3 (unattended Outfit Studio; BodySlide handoff; retry-window fix)

### 0.5.3 update (acceptance source isolation, 2026-10-01)

- Real run of `acceptance-test-cloth` stopped at planning: `UNKNOWN_AUTO — SliderSets/Vtaw Wardrobe 10 Latex Suit Top -
  3BBB AUTO.osp already exists (CBBE 3BBB Auto Conversions) without a manifest record`. The acceptance planner went
  through the production collision rules. Fixed ONLY for the acceptance path:
  - `plan.AcceptanceIsolation` (frozen): command `acceptance-test-cloth`, the exact source project + source mod, the
    disposable output, `work/acceptance`, the production output mod, the MO2 mods folder, no-commit. `verify()` asserts
    the command, output inside the converter-owned `work\acceptance`, not the production output mod, not under MO2
    mods, commit impossible (`BatchEngine.REFUSES_ACCEPTANCE_COMMIT`), exact source project; raises otherwise.
  - Only for that one project, `build_package_preview(acceptance=…)` skips the existing-conversion lookup
    (ALREADY_CONVERTED / UNKNOWN_AUTO) and `build_plan(acceptance=…)` does not count a generated project of the same
    name in ANOTHER mod as a collision (recorded as an ACCEPTANCE warning). Production commands never build the object:
    UNKNOWN_AUTO and every collision rule are unchanged.
  - `--output` must be inside `work\acceptance`; the batch is marked `acceptance` at creation.
  - The existing generated AUTO files (as MO2 serves them and in the output mod) are SHA-256-hashed before/after and
    must be byte-identical for the acceptance to pass.
- Regression: `UnknownAutoRealCaseTests` reproduces the real message (fails without the fix): production stays
  UNKNOWN_AUTO before and after; the acceptance uses the original source and passes; the AUTO OSP is byte-identical.

### 0.5.3 update (targeted cloth-data acceptance, 2026-10-01)

- New DEVELOPMENT/ACCEPTANCE command `acceptance-test-cloth --package P --project "<exact>" [--output DIR]`
  (`app/acceptance.py`): converts ONE cloth-data project from its ORIGINAL source project, unattended, in a disposable
  batch. Forced no-commit (the batch is marked `acceptance`; `BatchEngine.commit` refuses it), forced disposable output
  (a new empty folder under `work/acceptance`, never the configured output mod or anything inside MO2's mods),
  no manual fallback. The normal "already compatible" detection is unchanged (the acceptance plans against an empty
  disposable output, analyzing only the package's own projects); the existing 3BBB project is neither touched nor hidden.
  Writes `acceptance_cloth.json/.txt` with the report items (dialog, hierarchy, origins, payloads, validation, source
  SHA-256 before/after, a size+mtime fingerprint of the real output mod before/after).
- The *Choose cloth data* dialog's real control hierarchy (classes, IDs, parents, texts, state, list items, buttons) and
  `closed_after_confirmation` are now recorded on success too (`session_result.json` → `cloth[].dialog`).
- OK/Cancel of that dialog are identified by control ID **and** label **and** enabled state (5100 "OK", 5101 "Cancel"),
  exactly one list box required; anything else stops safely with the hierarchy recorded.
- Post-save cloth verification now records source/generated payload size + SHA-256 and `payload_match`
  (validation metrics → `result.json` run.cloth_extra_data → manifest); `NifFile.cloth_sha256`.
- Tests: `tests/test_acceptance_cloth.py` (committed project re-run into disposable output; altered payload never
  passes; relabelled OK button fails safely; refusals). 335 tests.

### 0.5.3 update (first real Windows acceptance, 2026-10-01: stopped safely before Execute was confirmed)

- **Cause** (`logs/session_result.session1/2.json` of batch `20261001-002408_Vtaw_Wardrobe_10_Bodyslides`): Outfit Studio's "Confirm Execution" is a Windows task dialog whose buttons are real `Button` windows under `DirectUIHWND > CtrlNotifySink`, but **all with control ID 0** ("&Yes", "&No"). The driver returned the buttons as a dict keyed by ID, so both collapsed into `{0: 'No'}` and the controller (correctly) refused to answer. `probe-outfit-studio` had passed because it stops before Execute.
- **Second latent bug found while fixing it:** wxWidgets builds an OK-only message box (the completion message "Automation", Reference/NIF Error, Deleted Shapes) as a single **IDCANCEL** button labelled "OK" (`wxMSWTaskDialogConfig`, so Escape works). Pressing IDOK there would have done nothing, and the completion would never have been confirmed.
- **Fix:** `dialog_buttons` now returns every button (hwnd, ID, text, enabled) — never keyed by ID. A native box is answered only when its exact title AND its exact set of button texts match a known Outfit Studio box (`os_control.KNOWN_BOXES`), the button to press exists exactly once and is enabled, and the box then closes; the press is `TDM_CLICK_BUTTON` with the ID wxWidgets gave that button (Yes = IDYES, No = IDNO, OK-only = IDCANCEL), or the button's own ID in a classic message box. Never by position. Confirm Execution is still answered only after the script name, SHA-256 (twice), step count and part order were verified.
- **"Converting: Vtaw WD10 Angel Jacket" twice per attempt** was progress reporting only (the session start and the output monitor both announced the first project; neither session executed a single step: `started_at` null, 0 step lines, no file written). Reported once now.
- Tests reproduce the captured hierarchy (`test_win32_ui`, `fake_win_os`: task dialogs with ID-0 buttons, wx OK-only = IDCANCEL), plus: a Confirm Execution with an unexpected button set is never pressed; a box that ignores the pressed ID stops the session instead of being assumed answered. 331 tests.

- **Convert is one click.** Outfit Studio still opens and runs its normal window (headless 5.8.x cannot copy bone weights), but the converter drives it: nobody presses Ctrl+Shift+A, chooses a script, clicks Execute, answers the cloth dialog, closes Outfit Studio or starts the next session (`conversion/os_control.py`, `conversion/win32_ui.py`).
  - **Mechanism:** Outfit Studio 5.8.2 restores `AutomationDialog.lastScript` from its own `OutfitStudio.xml` when the Automation window opens. The converter writes the exact session script there (private runtime only), opens the window through the menu command whose native ID it reads from the menu, and acts through Win32 window messages to Outfit Studio's own native controls (`WM_COMMAND`/`BN_CLICKED`, `TDM_CLICK_BUTTON`, list-box messages). No simulated keyboard or mouse input, no screen coordinates, no focus needed. Outfit Studio itself is not patched (a self-built binary would replace the accepted engine).
  - **Exact script:** before anything starts, the script's name (`cbbe3bbb_batch_<batch>_<session>`), SHA-256 (recorded when written), batch invariants, part-per-planned-project order and step count are checked; the Automation window must then show exactly that name and step count, and the hash is checked again before Execute. Otherwise nothing runs.
  - **Readiness and completion** from observable state: `Log_OS.txt` "Outfit Studio initialized." + "OpenGL Context Info:" (the 3D view is up), the main window, process idle; then the output pane's `Automation: Step i/N` lines, the status bar and Outfit Studio's own completion message. Timeouts are only guards (startup, total, no-progress). Exiting is never taken as success.
  - **`session_result.json`** per session (`logs/session_result.session<N>.json`): batch/session/script (+hash), process, launch/ready/start/finish/end times, planned/started/processed projects, save attempts, automation errors (step → project), every dialog and its answer, cloth decisions, graceful/forced close, status COMPLETED / COMPLETED_WITH_PROJECT_ERRORS / AUTOMATION_FAILED / USER_INTERACTION_REQUIRED / TIMEOUT / OS_CRASHED / CANCELLED. The output pane text is kept in `logs/automation_output.session<N>.txt`.
  - A project for which Outfit Studio reported an error (Automation Error, Reference/NIF Error, Deleted Shapes) is never accepted, even when Outfit Studio still saved it (`OS_STEP_ERROR`). A failure before anything was executed is always counted as a control failure (retry once, then ask), so a session can never be relaunched endlessly, and Outfit Studio is always ended if the control itself fails.
  - **Known dialogs only** (by title and buttons): Confirm Execution (Yes), Automation Error (Yes = continue, recorded for that project), Reference/NIF Error and Deleted Shapes (OK, recorded), the completion message (OK), Unsaved Changes (No). Anything else is captured (title, controls, list items, screenshot if Pillow is present) and the session stops; the project that was running is isolated.
- **Choose cloth data (BSClothExtraData)**, the Wardrobe 10 case (Latex Suit Top):
  - A: exactly one origin, it is the saving project's own staged ShapeData NIF, its SHA-256 equals the source and it has the planned number of cloth blocks → kept automatically (`single_verified_origin_preserved`);
  - B: several origins whose cloth payloads are byte-identical to the project's own → only the project's own is kept (`equivalent_origins_own_selected`);
  - C: several different origins → not guessed: Cancel (no cloth data written), the project fails with "Multiple cloth physics data sources require review.", the session continues;
  - D: anything unexpected (no origin, outside the workspace, the project has no cloth data of its own, the file does not verify) → captured, the session stops, the project is isolated.
  - Every save is verified afterwards (cloth payloads must equal the source's), and the manifest records `cloth_extra_data` (encountered, policy, source, verification, origins, validation).
- **Fault isolation:** the project that was running when a session crashed, timed out or showed an unknown dialog runs alone in the next session and then fails on its own; projects that never started do not use up an attempt; without an identifiable project the next sessions are halved. A control failure before anything ran is retried once in a fresh Outfit Studio; a second one asks **Retry automatically / Continue manually / Stop for now** (GUI) or prints the manual continuation (CLI, `resume-batch --manual` / `--unattended`). Validated work is never discarded.
- **Fix (found in the real Wardrobe 10 batch):** a project retried in a later session was validated against the *first* session's time window, so every saved retry was FAILED (sessions 7→8: ZhuYuan ×10, Waitress Top V3, Dress 2 V2). The latest session is used now, and an earlier attempt's leftovers are moved to `projects/<id>/previous_attempt_<n>/` before a retry. In that batch, 6 of the 12 had no other failed check (ZhuYuan Jacket Badge, Pants, Pants V2, Shirt, Tie; Dress 2 V2); the other 6 also failed their own weight checks (REVIEW). `validate-batch` re-judges them with 0.5.3.
- **BodySlide handoff:** *Build Converted Outfits* and all reports say "Build only: CBBE 3BBB Auto Conversions - <package>", preset, Build Morphs ON, and warn not to batch-build the original projects after the AUTO group (they target the same game mesh files). `verify-package-build` keeps BUILD_STALE and explains it: "N converted outfits have not been built from the generated 3BBB projects yet, or the original BodySlide projects were built afterward." + what to do. BodySlide itself is not started automatically (its output location depends on the user's MO2 setup); its 5.8.2 command line is documented.
- **Body routes (preparation):** Source body (Auto-detect / CBBE; Fusion Girl shown as not implemented) and Target body (CBBE 3BBB) in the window; `conversion/routes.py` separates source/target body family, physics profile, geometry and weight-transfer strategy; batches and manifest records carry the route. Only CBBE → CBBE 3BBB is implemented. Rename plan: `docs/RENAME_PLAN.md`.
- The private Outfit Studio runtime runs in English with the light theme (`Language -1`, `AppearanceMode Light`) so window titles are predictable.
- Profile settings: `os_control` (auto | unattended | manual), `unattended_stall_timeout_s`, `unattended_ready_timeout_s`, `unattended_close_timeout_s`.
- **`probe-outfit-studio`** (CLI): starts the private Outfit Studio, opens its Automation window with a one-step probe script preselected, verifies it and closes it — nothing is executed; the result goes to `reports/os_probe_*.json`. A one-minute check of the unattended control on a real PC.
- Fresh disposable CP5 regression (sandbox engine): Safe 91, All Possible +20, the same 14 remaining; every NIF 0.5.1 generated is byte-identical; sources unchanged (`RESEARCH.md` §18).
- 32 new tests (328 in total): the 5.8.2 window flow is modelled in `tests/fake_win_os.py`; the Win32 message protocol is checked against a user32 model in `tests/test_win32_ui.py`.

## 0.5.2 (CP5 accepted; "Needs attention" in the final result; in-game verification reports)

- **CP5 is accepted** as production validation of the CBBE → CBBE 3BBB pipeline (`docs/RESEARCH.md` §17). The remaining items are accepted limitations; no further CP5 conversion work is planned.
- **Final result lists names, not only counters.** The normal window, the CLI summary and the package report show three counts — **Completed** (converted or already compatible, with no in-game problem reported), **No conversion needed** and **Needs attention** — and the full list "Needs attention / original kept:". Each entry is the outfit name with a short reason, for example "physics conversion unavailable", "physics could not be verified", "already has its own physics weights (kept as is)" or "not tried in Safe mode".
  - Every outfit that did not get the requested physics stays on this list: not converted, blocked, failed validation, not tried in this mode, or unfinished. A usable original is **not** counted as completed.
  - Technical reasons stay under View Problems → Technical Details, in the package report and in the diagnostics.
  - The per-outcome breakdown (automated validation) is still available: *Show breakdown* in the GUI, and a section of the CLI summary and the report.
- **Earlier failed attempts stay visible.** A project that a previous batch of the same package attempted without success (REVIEW / FAILED) keeps that result in later views instead of reappearing as "not tried".
- **In-game verification reports** for runtime-only exceptions such as leggings v4:
  - Where: GUI *Report in-game problem…*, the CLI `report-runtime-issue --project NAME --state FAIL|PARTIAL|PASS --note …`, or `AppController.record_runtime_issue`.
  - What is recorded: the manifest `acceptance` history of the conversion (history kept; PASS clears a report).
  - Effect: such an outfit is listed as "converted, but no physics in game (reported)". It is not counted as completed, and it appears in the package report ("In-game verification reports") and in the diagnostics (`runtime_issues.json`).
  - The automated validation result and the manifest validation data are never changed retroactively.
- 2 new tests (296 in total).

## 0.5.1 (Phase 3.2: post-export proportional skin-weight renormalization)

- **Why:** Outfit Studio 5.8.2 / nifly `SetShapeVertWeights` normalizes a vertex over all its bones and then stores only the 4 largest weights. After CopyBoneWeights gave a vertex more than 4 bones, the stored weights sum to less than 1 (real CP5: 0.848–0.95), and BodySlide builds keep that. In 0.5.0 this was why 23 of the 89 safe CP5 projects were refused.
- **Repair** (`conversion/renormalize.py`, BodySlide-project batches): after each Outfit Studio session and before validation, the stored 1–4 weights of such a vertex are rescaled: `new_weight_i = old_weight_i / sum(old_weights)`.
  - Scope: only the converted (planned) shapes, and only vertices with the truncation signature (exactly 4 non-zero weights, sum in [0.5, 1 − 0.002)). Already normalized vertices stay byte-identical.
  - Nothing else changes: bone indices, positions, UVs, normals/tangents, colors, triangles, segments/partitions, materials, extra data and the project file (sliders/zaps) are not touched. This is proven on the re-read file: only the rescaled weight bytes differ, the zero pattern is unchanged, proportions are kept, and sums equal 1.
- **Refused, file left as it is** (`RENORMALIZE_REFUSED`): zero sum, negative / NaN / Inf weights, a sum above 1, a deficit on a vertex with fewer than 4 influences (not the truncation), a sum below 0.5, an unreadable file. The strict validation then reports the original problem.
- **Validation:** the full existing validation runs afterwards on the repaired file read from disk. Thresholds are unchanged, and the repair cannot hide another failure: it changes nothing else any check reads.
- **Records:**
  - the untouched Outfit Studio file is kept in `projects/<id>/generated_raw/`;
  - `renormalization.json` per project;
  - `result.json` `run.renormalization`;
  - manifest record `skin_weight_renormalization` (status, vertices, per-shape min/max/mean sum before, max error after, raw and final SHA-256, rule).
  The file's modification time is kept, so the execution evidence still attributes it to the Outfit Studio session.
- **Real CP5 on a fresh disposable copy:**
  - Safe: 89 / 89 converted, including all 23 that were REVIEW in 0.5.0;
  - Convert All Possible: 20 more;
  - 14 remain (see `RESEARCH.md` §16).
- 6 new tests (294 in total). The fake Outfit Studio gains `weight_deficit` (nifly-like saving) and the `bad_weights` injection.

## 0.5.0 (Phase 3.2: CP5 mass conversion, bodysuit regression fix, end-user GUI)

- **Bodysuit v1 root cause (Part A, outcome A: a converter bug, fixed).** Outfit Studio 5.8.2 saves a generated project with `<Shape DataFolder="<source project>">`, because the outfit keeps reading its slider data from the source folder. The analyzer took *any* per-shape DataFolder as body evidence. A full-length suit is ≥ 8000 vertices and ≥ 80 units tall, so it then met the name/size body rule and was analysed as a body. Result: no region metrics, "agreement n/a", and "project contains only a body/reference shape". The bikinis were below the size limit and passed.
  - Now only a DataFolder that names a body or reference folder counts as body evidence (CBBE, CBBE Vanilla/Reduced, TWB Vanilla, BodyTalk, "… Body References" …). Analyzer 1.1.
  - Validation is unchanged (≥ 0.90 agreement kept). The reference shape is still always a body.
  - Library impact (840 projects): 4 changes, all the same bug. Tao's Latex Catsuit and Vtaw Jumpsuit 3BBB were SKIP "only a body"; they are now KEEP.
  - Real CP5 bodysuit v1 on the same files: VALIDATED, agreement breast 0.999 / thigh 1.000 / butt 1.000, analyzer KEEP.
- **Safe mass selection (C).** `select_mode("SAFE")` picks CONVERT + NEW + HIGH only. It never picks MEDIUM, REVIEW, BLOCKED, SKIP, UNSUPPORTED, ERROR, already-converted or embedded-body projects. Real CP5: 89 of 136; the pilot bikinis are KEEP ALREADY_CONVERTED.
- **Convert All Possible (P).** Also attempts CONVERT projects below HIGH and **REVIEW_ATTEMPTABLE** projects (garment risk, loose or uncertain fit). REVIEW_BLOCKED projects are never forced: no safe radius, embedded body, existing or ambiguous 3BBB weights, name collisions, changed or unknown earlier conversions. Validation is identical in both modes, and only validated output is installed.
- **Bounded sessions (D).** `max_projects_per_session` (default 25) in a deterministic order. Cloth projects still go last, one per session. Each session is validated when it ends.
- **Fault isolation, checkpoints and resume (G, Q).**
  - Per-project states: PENDING → ATTEMPTING → GENERATED → VALIDATED / REVIEW / FAILED → COMMITTED, saved after every change.
  - SESSION failure (crash, timeout, closed without Execute): independently validated results are kept, and unfinished projects go to a fresh session (`session_retries`, default 1).
  - SYSTEM failure (source or reference changed, MO2 profile changed, output or workspace unusable): stops in SYSTEM_ERROR.
  - `resume-batch` continues without repeating finished projects. Partial commit happens session by session.
- **Weight sums after the 4-influence limit.** A vertex that received more than 4 bones is saved by Outfit Studio with a weight sum below 1 (nifly normalizes over all bones, then keeps 4). Beyond the unchanged 0.05 tolerance this is now REVIEW ("could not be stored completely"), not FAILED. Vertices without any weight stay FAILED.
- **MO2 (H).** `OUTPUT_MOD_DISABLED_IN_MO2` / `OUTPUT_MOD_NOT_IN_PROFILE` warnings (read-only; the profile is never edited). The package SliderGroup lists exactly the committed AUTO projects.
- **Build plan, package build check, test list, report (I–L).** CBBE Curvy, Build Morphs, AUTO after the original. `verify-package-build` aggregates every committed project and both groups. 5–8 representative in-game items. The package report has buckets SAFE_AUTO_DONE, MEDIUM_NOT_AUTO, REVIEW_COMPLEX_GEOMETRY, BLOCKED_WEIGHT_TRANSFER, EXISTING_3BBB_AMBIGUOUS, NO_PHYSICS_NEEDED, FAILED_TECHNICAL.
- **In-game names.** When a package ships no plugin, the item names come from the plugins of the enabled mods (CP5 BodySlide → "Clothes Pack 5 by DonEb14n.esp").
- **CLI (M).**
  - `convert-package` (one step: analyze → convert → validate → install; `--mode safe|all-possible`, `--no-commit`, `--max-per-session`, `--level`);
  - `resume-batch`, `package-report`, `verify-package-build`, `export-diagnostics`;
  - `plan-batch` / `convert-batch` accept `--mode` and `--max-per-session`.
  - Without a usable Outfit Studio, `convert-package` ends in SYSTEM_ERROR instead of failing 100 outfits.
- **GUI (P, Q, R, R.1).** A simplified Normal window is the default; the engineering window becomes Settings / Advanced (`gui --developer`).
  - Status rows, mod selection, Safe / Convert All Possible (with the warning dialog), Analyze, and a one-click Convert.
  - Progress: bar, counts, current item, and a Problems list by **name** ("+ N more…", View All Problems). No modal dialog per failed outfit.
  - Final summary names the outfits that could not be converted ("Original versions … left unchanged"), with Build Converted Outfits, View Problems (Technical Details on request), View Details, Export Diagnostic Report, and Resume.
  - Log levels NORMAL / DETAILED / DEBUG. Human-readable message map (`app/messages.py`). Anonymised diagnostic ZIP.
- Real disposable CP5 proof and the full list of changes: `docs/RESEARCH.md` §14–15, `docs/BATCH.md` §12.
- 32 new tests (288 in total).

## 0.4.3 (Phase 4.1R2: Direct-NIF post-export restoration of immutable vertex attributes)

- **Policy:** interactive Outfit Studio 5.8.2 may smooth normals at duplicated seam vertices during a Direct-NIF export. CBBE3BBBConverter treats this as an unwanted exporter side effect. Outfit Studio is the skinning backend only.
- **Pipeline:** Outfit Studio now exports to `meshes\<nn>_<id>\generated_raw\`, which keeps the raw evidence. `conversion/restore.py` then builds the final candidate in `generated\`: every vertex byte from the source except the skin weights and bone indices, which come from the raw export. Only that final file is validated and committable.
- **Safety, in order:**
  1. **Structural gate** (`POST_EXPORT_RESTORE_UNSAFE`). Required: same shapes and order, nothing added (no reference body), same block type, vertex count, vertex descriptor/stride, triangles, segment data, material, alpha and transform; same extra data and block types; no cloth. Vertices are mapped by index only, never by position.
  2. **Raw-diff policy** (`RAW_EXPORT_UNEXPECTED_CHANGE`). Allowed: the skinning, and normal + tangent-basis changes on duplicated seam vertices (`OS_REGENERATED_NORMALS`, `OS_REGENERATED_TANGENT_BASIS`). Anything else aborts the restoration: position, UV, second UV, color, landscape/eye data, unknown bytes, or a normal change on an ordinary vertex. Corruption is never overwritten.
  3. **Patch** into a temporary file. Only the immutable vertex bytes are rewritten.
  4. **Proof** on the re-parsed temporary file: immutable fields == source and skinning == raw export (byte-exact), and no other byte differs from the raw export. Only then is it renamed to the final name. A refusal or failure leaves no candidate, so the mesh is FAILED and cannot be committed.
- **Validator unchanged:** the strict 0.4.2 per-field checks run on the final file. There is no exception for smoothing and no angular tolerance. Three restoration checks are added. If restoration is refused, the raw export is validated so the report names every defect.
- **Records:**
  - `restoration.json` per mesh;
  - `result.json` stage RAW_EXPORTED / RESTORED;
  - `generated_raw_sha256`;
  - a `restoration` block in the manifest record (raw and final SHA-256, restored bytes, per-shape OS_REGENERATED counts, other raw differences).
  Commit also re-checks the raw export's hash. 0.4.1/0.4.2 workspaces are revalidated without restoration (stage LEGACY_NO_RESTORATION); they are never rewritten.
- **Real forensics** on copies of batch `20260929-201925`: TankTop, HotPants and Thong are all VALIDATED after restoration (39/39, 39/39, 38/38 checks). 120 / 170 / 59 bytes were restored, and the restored files are byte-identical to the sandbox 0.4.1 exports.
- 17 new tests (256 in total). The synthetic writer adds second UV and landscape data plus seam-split patches; the fake Outfit Studio adds `offseam_normals`.

## 0.4.2 (Phase 4.1R: real Windows Direct-NIF vertex-data forensics)

- **Finding** (`RESEARCH.md` §12): the first real Windows batch `20260929-201925` FAILED on "vertex data DIFFERENT" for TankTop, HotPants and Thong. Skinning was *not* the cause: skin weights and bone indices were already excluded from that check. The differing bytes are the **normals, tangents and bitangents** of 12 / 20 / 6 vertices.
  - Every one of those vertices sits on a UV seam (a group of vertices at the same position).
  - Outfit Studio 5.8.2's interactive session recomputes normals with `Mesh::SmoothNormals`, which averages normals across a seam when they are less than 60° apart. `ExportNIF` then writes those normals and recomputes the tangents.
  - An emulation of that algorithm reproduces the Windows normals exactly: TankTop 3412/3412, Thong 405/405, HotPants 2107/2110, with the other 3 off by one quantization step.
  - Changes: up to 8.7° (TankTop), 28.4° (HotPants), 16.7° (Thong). Positions, UVs, triangles, segments, materials and the skinning are identical to the sandbox result.
  - The sandbox did not show it: its headless run has no GL meshes, so `ExportNIF` keeps the file's normals.
- **The three meshes stay FAILED:** this is a real, unintended change of shading data, not a validator artefact. Nothing was committed.
- **Validator:** the combined "geometry and vertex attributes identical" check is replaced by per-attribute checks decoded from the BSVertexDesc:
  - vertex/triangle counts and format;
  - triangles/topology;
  - positions, UVs, second UVs, normals, tangents and bitangents, vertex colors, landscape and eye data;
  - every byte no known field covers.
  Only skin weights and bone indices may differ, and they are validated by the weight checks. Each failure reports the vertex count, the vertex indices and, for directions, the angle. No check was relaxed.
- `synth.write_nif` writes UVs, normals, tangents/bitangents, colors, eye data and padding bytes. The fake Outfit Studio injects single-attribute defects, a Windows-like seam-normal change and scrambled weights.
- 10 new tests (239 in total).

## 0.4.1 (Phase 4.1: controlled Direct-NIF conversion)

- **First production write path for DIRECT_NIF** (`conversion/direct_batch.py`), with the same stage separation as the BodySlide batch: PLAN (read-only) → CONVERT TO WORKSPACE → VALIDATE → COMMIT (VALIDATED only, explicit) → VERIFY (read-only). There is no one-command convert+commit and no default selection.
- **CLI:** `plan-direct-batch`, `convert-direct-batch` (`--yes`, optional `--plan <inspected plan json>` that refuses when anything changed), `validate-direct-batch`, `commit-direct-batch` (`--yes`, `--only`), `direct-batch-status`, `verify-direct-build`. All need `--allow-direct-nif` and an explicit `--select` per mesh.
- **Selection** resolves to exactly one analyzed game-mesh record: a stable Direct-NIF ID (`dn-` + 10 hex digits of the normalized game path), a game-relative path, or a file name that is unique in the package. An ambiguous name, a male-only or unknown mesh is refused. Two items sharing one NIF convert it once.
- **Eligibility (no override):** CONVERT + HIGH + FEMALE_CBBE_COMPATIBLE + DIRECT_NIF_HIGH_CONFIDENCE, winner = the package, no cloth data, no embedded body, no body-slot (33) item, no structural issue.
- **Outfit Studio script** per mesh: ClearProject → LoadReference (CBBE-3BBB) → ImportFile (absolute path of the workspace copy) → CopyBoneWeights on the planned shapes only (full copy, radius 5 or the 0.3.3 per-shape radius, 4 targets) → ExportFile to an **absolute workspace path with WithRef=false**. No SaveProject and no fake BodySlide project. `check_direct_script` enforces it.
- **Workspace** `work\direct_nif_batches\<stamp>_<package>\`: batch.json, plan.json, per mesh `source\` (hash-checked copy), `generated\`, plan/result/validation; staged reference; scripts; session logs. States PLANNED / CONVERTING / VALIDATED / REVIEW / FAILED / COMMITTED per mesh. The MO2 winner is resolved again before the copy: another winner → REVIEW SOURCE_WINNER_CHANGED, a changed file → REVIEW SOURCE_CHANGED.
- **Validation** (`nif/structure.py`, new): independent re-parse; source and copy SHA-256; exact shape set/order/names (no reference leak, rename, missing or added shape); geometry and every non-skin vertex attribute byte-identical, triangles, transforms; BSSubIndexTriShape segment data; material, texture set, alpha; extra data; block types; no cloth data. Weights: only planned 3BBB bones (forbidden region → FAILED), expected bones weighted, no zero-weight vertex, normalized, ≤ 4 influences, cross-side/quality checks, agreement ≥ 0.90, Copy Bone Weights prediction (mean |Δw| < 0.005), untouched shapes normalization-aware (0.3.3), the Direct-NIF analyzer must classify the output KEEP ALREADY_3BBB. Quality-only failures → REVIEW, everything else → FAILED.
- **Commit** re-hashes the generated file and the source winner, writes atomically to `<output mod>\<same game path>`, reads the file back (SHA-256), never overwrites an existing file (it tells whether it is the same accepted conversion, another generated conversion, a stale conversion or a manual file) and adds a `direct_conversions` manifest record. BodySlide records are untouched. After a commit the CLI prints the body-slot **layering test note**.
- **Idempotency:** the Direct-NIF analysis reads the manifest: KEEP ALREADY_CONVERTED_DIRECT_NIF; REVIEW SOURCE_CHANGED, TARGET_REFERENCE_CHANGED, OUTPUT_CHANGED or OUTPUT_NOT_WINNER.
- **verify-direct-build:** DIRECT_OK / NOT_COMMITTED / OUTPUT_NOT_WINNER / OUTPUT_MISMATCH / SOURCE_CHANGED / GENERATED_INVALID, with the winner, all SHA-256s, in-game items, expected vs actual 3BBB bones. Reports in `reports\builds`.
- **Analyzer change (real finding):** a Direct-NIF mesh whose full Copy Bone Weights would add 3BBB bones outside its planned regions is now REVIEW FULL_COPY_OUTSIDE_REGIONS instead of CONVERT (the BodySlide path already downgrades such projects). Real EasyGirl: 8 of the 63 CONVERT meshes (mini-dresses/mini-skirt: weak thigh weights at the hem, max 0.04; corsages/short blouse: weak butt weights, max 0.10). CONVERT 63 → 55, REVIEW 60 → 68.
- `synth.write_nif` writes shaders, texture sets, alpha, segment data and NiStringExtraData; the fake Outfit Studio handles ImportFile/ExportFile with 14 injectable export defects.
- 42 new tests (229 in total).

## 0.4.0 (Phase 4.0: Direct-NIF package analysis, read-only)

- **Two source paths.** The existing BODYSLIDE_PROJECT pipeline is unchanged. The new DIRECT_NIF path is for packages that ship a plugin and built game meshes but no BodySlide projects (e.g. HN66 EasyGirl). `conversion/direct_nif.py` analyzes them: plugin ARMO/ARMA → female third-person meshes the game uses (one mesh → every item, deduplicated) → MO2 winner and competitors → structural NIF analysis → sex/body family → embedded body identity (Phase 3.1, geometry only) → per-shape physics regions with the Phase 1 rules → per-shape adaptive radius (0.3.3) → KEEP / CONVERT / SKIP / REVIEW / UNSUPPORTED / ERROR with a reason code.
- **Source mode** per package: BODYSLIDE_PROJECT, DIRECT_NIF, MIXED (BodySlide projects keep the meshes they build; DUPLICATE_GAME_MESH), NO_CONVERTIBLE_SOURCE.
- **Opt-in only.** `analyze-package --allow-direct-nif` (CLI), or "Also analyze the plugin's game meshes (Direct NIF, read-only)" in the Convert Package window (default off). Without it, a package without BodySlide projects gives the old error plus a hint. Body-replacement planning stays behind `--allow-embedded-body-replacement`. There is **no** direct conversion or commit command.
- **Conservative rules:**
  - male-slot-only meshes are never analyzed as CBBE;
  - a mesh used as both a male and a female model → REVIEW;
  - a male-named non-CBBE body in a female slot → UNSUPPORTED (MALE_MESH);
  - a non-CBBE body → UNSUPPORTED;
  - AMBIGUOUS / UNKNOWN → REVIEW (no physics region → SKIP);
  - cloth data → REVIEW;
  - Outfit Studio import renames (duplicate names, a shape named like a bone) → REVIEW;
  - few-bone small shapes over a physics region → REVIEW;
  - an unreachable required radius → REVIEW;
  - a losing MO2 copy → REVIEW (MO2_CONFLICT);
  - a mesh not shipped by the package → SKIP.
  CONVERT only at HIGH confidence.
- A package-level **LAYERING RISK** note when body-slot (33) items embed a plain CBBE body while clothing would be converted.
- Reports: `reports/packages/*_direct.{json,csv,txt,html}`. The CLI stays compact; use `--details` for per-shape lines.
- `gamedata`: BOD2 biped slots of ARMA/ARMO, `slot_list`.
- Outfit Studio 5.8.2 direct-NIF research (sandbox, copies): `RESEARCH.md` §10; Phase 4.1 design: `CONVERSION.md`.
- 20 new tests (187 in total), plus a synthetic Direct-NIF package and a test plugin writer (`tests/synth.py`).

## 0.3.3 (Outfit 2 validation failure: unreachable vertices, save-time renormalization)

- **Real defect fixed: vertices that Copy Bone Weights cannot reach.** Outfit Studio zeroes every copied bone and takes new values only from reference vertices within the radius. In full mode, a vertex with no reference vertex within the radius is therefore left with no weight at all. Raider Punk Outfit 2 `Pants` vertices 466 and 771 are 6.58 units from the reference. The planner now finds such vertices per shape (`plan_shape_reach`) and raises that shape's radius to the smallest 0.5 step that reaches them (here 7.0). This happens only when the emulated copy proves that no other vertex changes and the new radius is at most 2× the configured one; otherwise the project is REVIEW / BLOCKED ("no safe radius"). The shape gets its own CopyBoneWeights step.
- **Validator false positive fixed: untouched shapes.** Outfit Studio rewrites every shape's weights on save: nifly `SetShapeVertWeights` divides each vertex by its weight sum and keeps the 4 largest. Hip_Case's source sums of 0.9916 therefore came back as 1.0 (raw change 0.0068). "weights unchanged" now compares against the per-vertex-normalized source (still ≤ 1e-3). It also requires the same weighted bones and the same influencing bones per vertex, and reports the raw change.
- "weights normalized" now lists vertices that have no weight at all.
- 6 new tests (167 in total). Projects without unreachable vertices (Top V1, Shorts, Shorts V2, Outfit, Outfit v3) keep radius 5 and byte-identical scripts.

## 0.3.2 (Phase 3.1 in-game failure investigation: post-build check, in-game item lookup)

- Forensics of the first real Phase 3.1 test (`RESEARCH.md` §8). The conversion and the BodySlide build were correct: the built `raiderPunk.nif` has the generated weights exactly (5 shapes, max difference 0). `MM69 Raider Punk.esp` shows `raiderPunk.nif` as the item **"Raider Punk Outfit V2"**. The item **"Raider Punk Outfit"** uses `raiderPunk_2.nif`, which the project "Outfit 2 - CBBE" builds and which was not converted (only legacy LBreast_skin/LButtFat_skin weights).
- New `verify-build` (CLI; `conversion/buildcheck.py`), read-only. After a BodySlide build it resolves the game mesh through MO2 (the winner and every competitor, with SHA-256 and times) and checks:
  - the mesh is newer than the commit;
  - every generated shape is present;
  - the 3BBB physics bones survived;
  - bone lists and weights are identical to the generated ShapeData.
  It also names the in-game items that use the mesh. Status: BUILD_OK / BUILD_MISMATCH / NOT_BUILT / BUILD_STALE. Reports go to `reports/builds`.
- In-game item lookup (`conversion/gamedata.py`): reads the ARMO/ARMA records of the package's own plugins, read-only. The Batch Preview lists "in-game item(s) using this mesh" per project and warns with **GAME_ITEM_NAME_MISMATCH** when project and item names disagree (Outfit ↔ Outfit V2, Outfit 2 ↔ Outfit). The warning does not change the classification or the selection.
- The commit report's BodySlide instructions now point to `verify-build`.
- 5 new tests (161 in total).

## 0.3.1 (Phase 3.1: safe embedded CBBE body → CBBE-3BBB body replacement, opt-in)

- New option, **default OFF**: "Allow safe embedded CBBE → CBBE-3BBB body replacement" (Convert Package window) / `--allow-embedded-body-replacement` (CLI `analyze-package`, `plan-batch`, `convert-batch`). With it OFF, classification, plans and scripts are exactly as in 0.3.0.
- Positive body identification (`conversion/body.py`): vertex coincidence with the CBBE-3BBB reference, reference coverage, welded-triangle subset, slider morphs vs the reference, existing 3BBB bones and a weak name hint. Classes BODY_CBBE_COMPATIBLE / BODY_ALREADY_3BBB / BODY_AMBIGUOUS / BODY_UNSUPPORTED / NOT_BODY.
- Conversion modes: `outfit_only` (MODE A) and `embedded_body_replacement` (MODE B). MODE B copies the reference's weights onto the body vertex by vertex (radius 0.01, one target, body only), imports the body's slider data into the project and saves with AutoCopyRef=false. Geometry, deleted vertices, sliders and zaps are kept; the clothes are converted per shape as before; accessories are untouched.
- The target body's identity (template, project, set, shape, source, SHA-256, vertex count, geometry signature) is recorded in the plan, batch and manifest. Ten new required validation checks for the body. A changed target reference gives `REVIEW / TARGET_REFERENCE_CHANGED`.
- New REVIEW statuses: EMBEDDED_BODY_AMBIGUOUS, EMBEDDED_BODY_UNSUPPORTED, MULTIPLE_BODIES. MODE B projects are never selected by default.
- Manifest: `conversion_mode`, `body_replacement`, `acceptance` {state, history}; new CLI `record-acceptance`. Earlier records are kept unchanged.
- Automation invariants: MaxResults 1 for radius < 0.5, relative `SliderDataFile`, no ImportSliderData FromFolder.
- Research (sandbox, copies only): RESEARCH.md §7. Also found: converting such an outfit clothes-only would lose the body's slider data (Outfit Studio keeps a stale DataFolder), see upstream draft 3.
- 20 new tests plus a GUI check (156 in total).

## 0.3.0 (Phase 3: controlled package/batch conversion)

- Package analysis and **Batch Preview** (`conversion/package.py`). It covers every project of one MO2 mod, the package rules (already converted / stale / changed / unknown AUTO, embedded body, manual 3BBB variant) and per-shape region decisions with diagnostics, allowed bones, copy mode and confidence. Default selection is CONVERT + HIGH only. Reports are JSON/TXT/HTML in `reports/packages`.
- **Per-shape planning**: every outfit shape has its own regions, allowed physics bones, copy mode and confidence. There is one CopyBoneWeights step per shape group, and validation checks each shape against its own plan. The Top V1 script is byte-identical (regression test with the real accepted plan).
- **Batch engine** (`conversion/batch.py`): a batch workspace, and one Outfit Studio script per session that converts several projects (one Execute). Every project is validated independently (VALIDATED / REVIEW / FAILED); `validate-batch` rebuilds results for recovery; `commit-batch` commits only VALIDATED projects, one transaction per project, after re-checking hashes. The manifest is merged and a package BodySlide group is added. Batch reports are JSON/TXT/HTML.
- **Cloth physics data** (BSClothExtraData): detected in NIFs; sessions are ordered around Outfit Studio's cross-project cloth carry-over; new validation check `cloth physics data (BSClothExtraData) unchanged`; headless runs with cloth data are blocked.
- Slider data resolution follows BodySlide (`local` data in the set's DataFolder).
- GUI: **Convert Package…** window (Analyze Package, Dry Run, Run Batch, Validate, Commit Successful, progress, cancel). CLI: `packages`, `analyze-package`, `plan-batch`, `convert-batch`, `validate-batch`, `commit-batch`, `batch-status`; `run_batch.bat`.
- 24 new tests (136 in total), plus a synthetic package fixture and a fake Outfit Studio that runs multi-project scripts, per-project failures and the cloth dialog.
- Second upstream report draft (cloth data persists across ClearProject).

## 0.2.1 (Phase 2 fix: interactive run detection)

- Fixed a false FAILED for interactive Outfit Studio 5.8.2 runs. Outfit Studio sends automation messages to the Automation window instead of `Log_OS.txt` while a script runs, so the old "SaveProject step in the log" test could never pass on Windows. Execution is now proven by artifacts (`conversion/evidence.py`): the session is on this workspace, the planned outputs are new and written inside the session window, and the saved project has the planned name and the reference's `ReferenceInfo`. Step lines are checked when present, and headless runs still need exit code 0. The content checks are unchanged; the evidence checks are added as required checks.
- `revalidate` (CLI, `run_revalidate.bat`): re-checks an existing workspace without rerunning Outfit Studio and optionally commits it (`--commit`). The original result and files are never changed.
- `ConversionPlan.from_dict`; the run's start/end times are recorded in `result.json`; the fake Outfit Studio writes Windows-format logs; 10 new tests (112 in total).

## 0.2.0 (Phase 2: first controlled conversion)

- Outfit Studio runtime detection: version, path, headless YES/NO, the automation option in the binary, and the known-broken headless weight copy for 5.8.0–5.8.2. A private runtime copy (program files only) is made in `runtime/`, and `Config.xml` is written per run. The MO2 BodySlide 5.7.1 is never touched.
- Dry Run planner: preview of source, family, reference, shapes, regions, bones, radius/targets, output names/paths, output mod, run mode and warnings, plus the exact automation script. Writes nothing.
- Automation script generator (Outfit Studio 5.8.2 XML format) with enforced invariants (explicit `AutoCopyRef=false`, one SaveProject, no absolute paths, no geometry-changing steps).
- Region-aware weight copy: full reference copy (as validated manually) when it adds only planned-region bones, otherwise a BoneList copy.
- Conversion engine: hash → stage → private Outfit Studio run (headless or interactive) → 20+ validation checks → analyzer must classify KEEP → source re-hash → commit to the output mod with `manifest.json` and a BodySlide slider group. Rollback on commit errors. Failed runs keep their workspace and logs and write nothing to the mod.
- GUI: Outfit Studio folder + Check, output mod path, "Convert selected…" window with Dry Run / Convert / Cancel and preview, script and result tabs. CLI: `runtime`, `plan`, `convert --yes`; `run_convert.bat`.
- Milestone gate: `conversion.allowed_projects` (default: `MM69 Raider Punk Top - CBBE`).
- `.osd` reader; fake Outfit Studio test harness; 37 new tests (102 in total).
- Found and documented an upstream Outfit Studio 5.8.x bug (headless CopyBoneWeights no-op), with a tested patch in `docs/upstream/`.

## 0.1.0 (Phase 1: analyzer only)

- Phase 0 research: installation, BodySlide 5.7.1 / Outfit Studio 5.8 automation, 3BBB bone structure, risks, licensing (`docs/RESEARCH.md`).
- Read-only discovery of game + portable MO2 (profiles, modlist priorities, overwrite, optional disabled mods) with a case-insensitive virtual file system for `Tools/BodySlide`.
- Parsers for OSP projects, RefTemplates, SliderGroups/Presets, BodySlide `Config.xml`/`BodySlide.xml`, and executable versions.
- Read-only Fallout 4 NIF reader (BSTriShape family, BSSkin, node hierarchy, skin space).
- Knowledge base from the installed CBBE 3BBB reference; emulation of Outfit Studio's Copy Bone Weights to predict conversions.
- Per-region metrics (relevance, fit, coverage, existing-weight quality), body-family detection, and the KEEP/CONVERT/SKIP/REVIEW/UNSUPPORTED/ERROR rules with reasons.
- Cross-project checks: same output mesh, superseded originals, same-file manual variants, duplicate set names.
- Parallel, cancellable, cached scanning; reports `latest.json/csv/html` (+ history); `logs/app.log` with path redaction.
- PySide6 GUI (profiles, detect, analyze, progress/cancel, filters, search, details, disabled "Convert (Phase 2)") and CLI (`gui`, `detect`, `analyze`, `profiles`).
- Synthetic test fixtures and 65 tests; Windows setup/run scripts; regression-check tool.
