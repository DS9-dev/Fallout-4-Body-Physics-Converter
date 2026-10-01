# Testing

## Automated tests

```bat
run_tests.bat                     :: pytest via .venv (Windows)
.venv\Scripts\python -m pytest -q
python -m unittest discover -s tests -t .     :: stdlib only (no pytest needed)
```

The tests are plain `unittest`, so pytest collects them too. Fixtures are **generated in code** (`tests/synth.py`): a procedurally built cylinder "body" with made-up 3BBB-style weights, synthetic outfit patches, and a minimal test-only NIF writer. Every classification branch is represented by a small fake MO2 installation (`build_fake_install`). No third-party assets are used.

| File | Covers |
| --- | --- |
| `test_nif.py` | NIF reader round trip, skin space vs node transform, truncated/non-NIF/wrong-version/missing files, non-FO4 header |
| `test_osp.py` | multi-set OSPs, shapes/sliders/zaps/output paths, malformed XML, wrong root, cp1252 fallback, set without source |
| `test_discovery.py` | modlist priority (highest first), Qt value decoding, MO2 instance, VFS priority/case-insensitivity/overwrite, installation detection, disabled-mod ranking, template choice |
| `test_analyzer.py` | end-to-end: CONVERT, KEEP (3BBB + TWB 3BBB), superseded SKIP, same-file manual variant (Raider Punk case), undeclared 3BBB outerwear (CP5 case), suspicious mirrored weights, outerwear, far garment, no-region SKIP, body-only SKIP, BodyTalk UNSUPPORTED, missing ShapeData / malformed OSP ERROR, MO2 override, disabled mods, cache round trip, cancellation, **read-only guarantee** (fixture tree hashed before/after) |
| `test_rules.py` | whole-word name hints, declared-3BBB detection, garment risks |
| `test_reports.py` | latest.json/csv/html, JSON round trip, CSV columns, HTML banner/columns/filters, filter semantics |
| `test_app.py` | profile store (round trip, Phase 1 guard, corrupt file), controller writes only into its own folder, refusal inside MO2, CLI detect/analyze/save, log redaction |
| `test_gui.py` | GUI smoke tests (main window, conversion window, package window; the embedded-body option exists and is OFF by default; 0.5.0 simple window: status rows, analysis counts, live problem names during the run, final summary with names and "left unchanged", Problems dialog with Technical Details, build check), skipped when PySide6 is missing |
| `test_batch.py` | Phase 3 on a synthetic package: package discovery; mixed CBBE + BodyTalk; classification and default selection (CONVERT/KEEP/REVIEW manual variant/REVIEW embedded body/SKIP/UNSUPPORTED/ERROR); **per-shape regions** (BREAST-only top, BUTT+THIGH shorts, multi-shape suit with BREAST / BUTT+THIGH / NONE shapes); preview reports and **no writes**; batch Dry Run and selection rules; preview round trip; batch run with the Windows interactive log → all VALIDATED, nothing in the output mod before commit; explicit commit (manifest, main + package group), **idempotent rerun** (ALREADY_CONVERTED, no second commit); **failure isolation** (embedded reference / not saved → FAILED, others committed); REVIEW after validation not committed; headless step errors attributed to their project's step range; cloth data: session planning, wrong dialog answers fail; interactive closed without Execute; **STALE**, **AUTO_CHANGED**, **UNKNOWN_AUTO**; **manifest merge** keeps an earlier Phase 2 record; commit re-checks hashes; **workspace recovery** (lost results and session log); source changed after preview; batch script invariants; status mapping; **real Top V1 regression fixture** (`tests/data/topv1_accepted_plan.json` rebuilds the accepted script byte for byte); CLI (analyze-package, plan-batch, convert-batch without --yes, batch-status); source tree byte-for-byte unchanged |
| `test_body.py` | Phase 3.1: body identity (full body, partial body with geometry evidence, name alone insufficient, sculpted and remeshed → AMBIGUOUS, custom morphs → AMBIGUOUS, already 3BBB, other family → UNSUPPORTED); synthetic body package with the option **OFF** (REVIEW / EMBEDDED_BODY, as 0.3.0) and **ON** (CONVERT / EMBEDDED_BODY_REPLACEMENT, not selected, accessories untouched, MULTIPLE_BODIES, AMBIGUOUS); MODE B plan and script (identity CopyBoneWeights on the body only, ImportSliderData, AutoCopyRef=false); preview and Dry Run write nothing; invariants; batch convert → validate → commit (manifest `conversion_mode`, `body_replacement`, target hash), idempotent rerun, `record-acceptance`, **TARGET_REFERENCE_CHANGED**; failure isolation (body not replaced, body smeared by a clothing-style copy, reference embedded, slider data lost → FAILED, the other project committed); wrong target rejected; cloth sessions and workspace recovery in MODE B; manifest merge keeps the earlier records; CLI flag; Top V1 script still byte-identical |
| `test_buildcheck.py` | 0.3.2: variant tokens and name mismatch (the real Raider Punk mapping Outfit ↔ Outfit V2, Outfit 2 ↔ Outfit; Top, Shorts V2, Outfit v3 and Bottom not flagged); a synthetic FO4 plugin (ARMO/ARMA, compressed records, localized names, an unrelated group, a broken plugin); the Batch Preview lists the item and warns without changing the classification; `verify-build` NOT_BUILT → BUILD_OK (simulated build) → BUILD_MISMATCH (unconverted mesh) → BUILD_STALE (mesh older than the commit); an overwrite copy wins the MO2 conflict; no writes into any mod; CLI |
| `test_reach.py` | 0.3.3: a pants shape with two vertices 6.8 units from the reference gets its own CopyBoneWeights step with a raised radius and VALIDATES; the same plan with the old radius FAILS with "vertices WITHOUT any weight: 5, 6"; a vertex beyond 2× the radius → REVIEW "no safe radius"; other packages keep the project radius; an untouched shape whose source weights do not sum to 1 passes after normalization, while a real change (bones reassigned) still FAILS; `untouched_weight_change` and `plan_shape_reach` unit cases |
| `test_direct_nif.py` | 0.4.0: a synthetic package with a plugin and game meshes, no BodySlide project:
  - DIRECT_NIF source mode; without `--allow-direct-nif` the old safe error (+ hint), and BodySlide packages are unchanged;
  - female vs male model slots: male-only skipped, a male+female mesh AMBIGUOUS, a male-named body in a female slot MALE_MESH;
  - duplicates deduplicated with every item name; missing mesh → ERROR;
  - embedded partial CBBE body: REVIEW without the opt-in, CONVERT EMBEDDED_BODY_REPLACEMENT with it, and never the full reference;
  - KEEP ALREADY_3BBB; SKIP NO_PHYSICS_REGION; CONVERT for HIGH breast and butt/thigh garments; REVIEW for a rigid accessory;
  - adaptive per-shape radius; UNSAFE_RADIUS; MO2 losing copy → MO2_CONFLICT; a mesh from another mod → NOT_PACKAGE_MESH;
  - cloth → REVIEW; a shape named like a bone → UNSUPPORTED_NIF_STRUCTURE; the layering-risk note;
  - MIXED package with a BodySlide-owned mesh (DUPLICATE_GAME_MESH); NO_CONVERTIBLE_SOURCE; inactive plugin;
  - reports only in the tool folder; source tree byte-identical; CLI |
| `test_direct_batch.py` | 0.4.1 Direct-NIF production workflow (Phase 4.1 test list 1–42): plan read-only; only CONVERT/HIGH/female CBBE selectable, REVIEW/SKIP/male/unisex/cloth/body-slot/clash refused; selection by ID/path/unique name, ambiguous names refused; FULL_COPY_OUTSIDE_REGIONS; duplicate items → one NIF converted once; source winner change → REVIEW SOURCE_WINNER_CHANGED (never converted, never committed); source changed before conversion → REVIEW; source copied to the workspace before Outfit Studio starts; source tree unchanged; absolute ExportFile path, WithRef=false, no SaveProject (and `check_direct_script` refuses violations); breast / butt / thigh weights validate with material, texture set, alpha, segment and extra data preserved; injected export defects all FAILED (reference leak, rename, missing/added shape, moved vertex, lost material, lost segment, lost extra data, added cloth, forbidden region, zero-weight vertex, untouched shape changed, malformed file, no output); adaptive radius; untouched normalization-only rewrite passes; failure isolation; commit writes only VALIDATED, refuses a manual file at the target and a changed source, preserves BodySlide manifest records, no temp files; KEEP ALREADY_CONVERTED_DIRECT_NIF, SOURCE_CHANGED, TARGET_REFERENCE_CHANGED on later analysis; verify DIRECT_OK / NOT_COMMITTED / OUTPUT_NOT_WINNER / OUTPUT_MISMATCH / SOURCE_CHANGED; a BodySlide batch commit and a Direct-NIF commit coexist; CLI |
| `test_vertex_fields.py` | 0.4.2 per-attribute vertex validation: the real EasyGirl vertex layout; a skin-only change passes; positions, normals, UVs, tangents, colors, topology each fail on their own check; a mesh with UVs/normals/tangents/colors/eye data/extra bytes validates end to end; each single-attribute defect (incl. eye data and bytes outside every known field) FAILS; the Windows seam-normal recomputation FAILS on exactly "normals" + "tangents and bitangents"; scrambled weights fail the weight checks; zero-weight, segment, material, rename and reference-leak protections unchanged |
| `test_bodysuit.py` | 0.5.0 Part A/B: body-folder evidence (real folder names from the library); a ≥ 8000-vertex full-length suit with its source DataFolder is an outfit with BREAST/BUTT/THIGH regions, the same shape in a body folder stays a body, the reference shape is always a body; a batch saved like the real Outfit Studio 5.8.2 (`<Shape DataFolder=…>`, source .osd) VALIDATES with agreement ≥ 0.90 and analyzer KEEP; embedded reference and missing weights still fail; without the fix the test fails with the two real messages |
| `test_renormalize.py` | 0.5.1 skin-weight renormalization: sums 0.91 and 0.95 → 1.0; proportions preserved; bone IDs and every other vertex field, triangles and segments unchanged; already normalized vertices byte-identical; raw file kept; mtime kept; idempotent; only the listed shapes; zero / negative / NaN / Inf / sum above 1 / deficit with fewer than 4 influences / sum below 0.5 → REFUSED with the file byte-identical; batch pipeline: deficits 0.09 and 0.05 (both fail without the repair) → VALIDATED and committed with the manifest record and sums of 1; the repair does not hide an embedded reference body (FAILED), refuses a bad file, and the other projects still convert; normalized output is not touched; source tree byte-identical |
| `test_unattended.py` | 0.5.3 unattended Outfit Studio against a model of the 5.8.2 window flow (`fake_win_os.py`): one-click conversion with cloth (A exact scripts in order, D completed + graceful close, E `session_result.json` fields, L single verified cloth origin kept + manifest `cloth_extra_data`, M generated cloth payloads = source, R source unchanged, T package group); another script preselected → exact selection by text; wrong script never executed, control failure retried once then asked, no attempt used; stale / foreign / mismatching script rejected before launch (B); readiness required (C); crash before Execute + resume (F, I); crash mid-session isolates the running project, saved projects not repeated (G, J); timeout isolates (H); failing step fails only its project (K); ambiguous cloth origins → Cancel, project FAILED "Multiple cloth physics data sources require review.", rest continues (N, O); equivalent origins keep only the own; cloth decision rules D; unknown dialog not answered, captured, project isolated then FAILED (P); manual fallback and `resume-batch --manual` (Q); cancel + resume; Unsaved Changes → No, refused close → forced; thresholds unchanged and automation success ≠ conversion success (S); BUILD_STALE explained, AUTO build → BUILD_OK (U, V); 0.5.2 manifests/batches readable, runtime issues (W, X, Y); retry judged by its own session (Wardrobe 10 7→8); routes; lastScript writer |
| `test_win32_ui.py` | 0.5.3 Win32 driver message protocol against a user32 model: window/child enumeration, WM_GETTEXT, list box / list view reads, menu item by text and the known-layout fallback, BN_CLICKED to the parent, TDM_CLICK_BUTTON, classic message box and wx button IDs, LB_SETCURSEL + keypad "+", exact CB_FINDSTRINGEXACT + CBN_SELCHANGE, no simulated input |
| `test_mass.py` | 0.5.0 Phase 3.2: SAFE = CONVERT + HIGH only (MEDIUM, REVIEW attemptable/blocked, SKIP, UNSUPPORTED, ERROR, KEEP excluded); ALL_POSSIBLE adds MEDIUM and REVIEW_ATTEMPTABLE, never REVIEW_BLOCKED (also not via `--select`); overview counts; buckets; bounded, deterministic sessions with cloth last and alone; one-click pipeline (commit per session, SliderGroup = committed only, problem names + human messages, idempotent second run, source unchanged); a failed All-Possible attempt is not committed and the batch continues; OUTPUT_MOD_DISABLED_IN_MO2; session crash keeps the saved project and retries the rest once; exhausted retries → FAILED; SYSTEM failure (source changed) stops with the finished projects kept, resume stops again; interrupted batch + partial commit + resume runs only unfinished projects; resume after a tool crash validates saved projects without converting them again; outcome/result/percent/name priority/messages (no engineering words); weight-sum REVIEW vs zero-weight FAILED; representative items; an outfit analysed as a body and a reference-only project never validate; package build check (NOT_BUILT → 3 BUILD_OK + 1 NOT_BUILT, groups complete), package report, anonymised diagnostics ZIP; CLI `convert-package` (overview without --yes, SYSTEM_ERROR without Outfit Studio), `package-report`, `export-diagnostics`, `verify-package-build`; 0.5.2 Needs attention:
  - blocked, failed, unsupported and not-tried outfits are listed by name with a short reason and never counted as completed;
  - an in-game FAIL report lists a validated conversion without changing its validation data, also after a later analysis (KEEP), in the report and in the diagnostics; PASS clears it;
  - no report is possible for an outfit that was never converted;
  - an earlier failed attempt stays visible;
  - CLI `report-runtime-issue` |
| `test_restore.py` | 0.4.3 post-export restoration: OS-changed normals and tangent basis restored to the source; new skin weights and bone indices survive byte for byte; positions, UV, second UV, colors, landscape/eye data and unknown bytes equal the source in the final file; raw changes outside the policy (position, UV, second UV, color, landscape, eye, unknown bytes, a normal on a non-seam vertex) are REFUSED and leave no candidate; vertex count, topology, vertex layout, missing/extra shape, reference body, segments, material and cloth refuse the patch; a verification crash leaves no candidate; the raw export is never modified; pipeline: seam-normal export → RESTORED → VALIDATED → committed with a manifest restoration record; geometry/UV/topology mutations → FAILED, raw kept, nothing committable; reference leak, rename, cloth, segments still fail; cloth/clash still unselectable; a 0.4.1-style workspace revalidates without restoration |
| `test_conversion.py` | Outfit Studio version and headless detection (5.7.1 → NO, 5.8.2 → YES + interactive, missing option, missing exe); private runtime copy and Config.xml; automation script generation and XML format; **AutoCopyRef=false invariant**; forbidden steps and paths; region/bone selection (full vs regions mode); unique, file-safe names; **output path/name preservation**; milestone gate, non-CONVERT, runtime and foreign-output-folder blockers; **dry run performs no writes**; engine with a fake Outfit Studio: success + manifest + slider group, interactive mode, closed-without-running, **embedded reference detection**, extra slider, step error, crash, hang, source changed after plan, **commit rollback**; the source tree hash is unchanged in every run; `.osd` round trip; CLI plan is dry and gated. **Execution evidence** (0.2.1): the exact Windows Outfit Studio 5.8.2 interactive `Log_OS.txt` (no step lines) is parsed, including the midnight rollover; an interactive Windows-format run is **accepted** with all content checks unchanged; it is rejected when closed without Execute, when outputs are stale, pre-existing or written outside the session, when the log belongs to another project path, when ReferenceInfo is missing, or (headless) when the step log lacks SaveProject. **Revalidate**: a reproduced 0.2.0 false-FAILED workspace → VALIDATED without writes, originals preserved byte for byte, `--commit` → SUCCESS + manifest `accepted_by`, no second commit, modified output / foreign log / non-workspace folder rejected, CLI |

## Real Outfit Studio verification (development sandbox)

Outfit Studio and BodySlide 5.8.2 were built from the v5.8.2 release source (Linux, wxGTK 3.2.4) and run under Xvfb on staged copies of Top V1 and the CBBE 3BBB reference:

| Run | Outfit Studio | Result |
| --- | --- | --- |
| A | 5.8.2 unmodified, headless `-a` | **FAILED** (correctly): CopyBoneWeights was a no-op (0 breast bones, analyzer still CONVERT); nothing written to the output mod |
| B | 5.8.2 with its window initialized (the interactive-mode equivalent) | **SUCCESS**: all 22 checks passed, analyzer KEEP |
| C | 5.8.2 + the proposed upstream fix, headless | **SUCCESS**, bit-identical weights to B |
| BodySlide | 5.8.2 `--groupbuild "CBBE 3BBB Auto Conversions" --targetdir … --preset "CBBE Curvy" --trimorphs` | built `raiderPunk_top.nif` (6 breast bones, no body) + `raiderPunk_top.tri` |

### Phase 3 (sandbox, read-only copies of the user's Raider Punk files)

| Run | Result |
| --- | --- |
| Batch Preview of the real package | 17 projects: CONVERT 2, KEEP 2, REVIEW 5, SKIP 2, UNSUPPORTED 6, ERROR 0 (see `RESEARCH.md` §6) |
| One script, 3 projects (Top V1, Shorts, Shorts V2), patched 5.8.2 build | all VALIDATED; exposed the cloth-data dialog and its carry-over between projects |
| One session per project, same 3 projects | outputs byte-identical to the one-script run |
| Session planning (Top V1 + Shorts, then Shorts V2) | Top V1 no dialog; cloth dialog answered wrong once → that project FAILED on the cloth check, the others VALIDATED and committed to a sandbox output mod |
| Top V1 from the batch vs the in-game accepted Windows result | identical weights and geometry |

## Regression check on a real installation

```bat
.venv\Scripts\python tools\check_regressions.py --mo2 "T:\MO_Main _ baseCBBE"
.venv\Scripts\python tools\check_regressions.py --report reports\latest.json
```

This checks Raider Punk Top V2 → CONVERT, Vtaw BlackMamba 3BBB / Wardrobe 8 3BBB / ASA 3BBB → KEEP, and CP5 jacket → REVIEW. Disabled mods are included by default because the CP5 mod is disabled in profile "Fix F".

## Manual acceptance

See the numbered steps in the Phase 1 hand-over (also summarised in `README.md`). In short: set up, detect, analyze, check the regression cases in the GUI and HTML report, confirm that nothing in the game/MO2 folders changed, and try cancelling and re-running (cache).

## Real Direct-NIF acceptance (local integration check, not part of the portable suite)

```bat
run_batch.bat analyze-package --profile Default --package "HN66Fo4_EasyGirl_BS" --allow-direct-nif --allow-embedded-body-replacement
```
Expected (installation of 2026-09-28): source mode DIRECT_NIF, 0 BodySlide projects, `HN66Fo4_EasyGirl_BS.esp` active, 210 female items / 141 unique female meshes, 21 male-only meshes skipped. Counts are in `RESEARCH.md` §10 (0.4.0) and §11 (0.4.1: CONVERT 55, REVIEW 68). Nothing may be written outside `reports\packages`.

The 0.4.1 sandbox acceptance of the first Direct-NIF batch (copies only, output mod = a sandbox copy) is in `RESEARCH.md` §11.

## 0.5.3 targeted cloth-data acceptance (real Windows)

`run_batch.bat acceptance-test-cloth --profile Default --package "Vtaw Wardrobe 10 Bodyslides" --project "Vtaw Wardrobe 10 Latex Suit Top"`
converts that single project again from its source into a disposable batch (never installed) and prints the report
(exit code 0 = `0.5.3 UNATTENDED OUTFIT STUDIO: ACCEPTED`). Model tests: `tests/test_acceptance_cloth.py`.

## 0.6.0 Body Architecture Foundation

`tests/test_bodies.py`: body ids/labels/normalization, detection from the analyzer's FamilyResult, route registry
(statuses, stages, executability, 0.5.3 keys), source/target requests, override safety (conflict / unverified),
package routing on the fixtures (CBBE → proven route, native 3BBB KEEP, BodyTalk unsupported, a synthetic Fusion Girl
package recognised and never converted), "Auto Detect changes no classification" (with and without the gate, three
fixtures), legacy preview/manifest without body fields, manifest round trip of the new fields, UNKNOWN_AUTO unchanged,
CLI body arguments, product name vs serialised identities.

## 0.6.1 stabilization

`tests/test_shape_names.py` (exact shape names incl. trailing spaces; TargetRegex patterns; a trailing-space shape is
planned and converted — fails with the old stripping parser) and `tests/test_release.py` (one version source, clean
title, '&'-safe folder-relative launchers, a full one-click run from a folder named "Fallout 4 Body & Physics
Converter", profile tool-root migration, user messages).
