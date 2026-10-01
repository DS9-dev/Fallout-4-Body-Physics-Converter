# Development history and engineering notes

This is the developer-oriented README of versions 0.1–0.6.0 (formerly *CBBE3BBBConverter*), kept for reference. The
end-user manual is `README.md`.

### Body routes (0.6.0)

| Source body → Target body | Status |
| --- | --- |
| **CBBE → CBBE 3BBB** | **Production** — the proven backend (Clothes Pack 5, Raider Punk, Vtaw Wardrobe 10; unattended Outfit Studio accepted in 0.5.3) |
| CBBE 3BBB → CBBE 3BBB | already compatible (kept as is) |
| TWB 3BBB → CBBE 3BBB | already compatible (kept as is) |
| TWB → CBBE 3BBB | converted as before 0.6.0 (TWB treated as CBBE-derived geometry); not separately accepted in game |
| **Fusion Girl → CBBE → CBBE 3BBB** | **recognised, not implemented yet** — Fusion Girl outfits are detected and listed under *Needs attention*; nothing is converted |
| BodyTalk / Male → CBBE 3BBB | not supported |
| Unknown → CBBE 3BBB | never converted automatically |

**Source Body** defaults to **Auto Detect** (each outfit's body is detected). Choosing a source explicitly only
*confirms* the detection: an outfit detected as another body is not converted. **Target Body** is CBBE 3BBB.
`run_batch.bat body-routes` lists the routes; `convert-package` / `analyze-package` accept the optional
`--source-body auto|cbbe|fusion_girl|twb` and `--target-body cbbe_3bbb` (defaults reproduce the earlier behaviour,
so existing commands work unchanged).

Compatibility promise: existing CBBE workflows, commands and generated projects remain supported.

### Development history


**Analysis never modifies mods.** Conversions use Outfit Studio 5.8+'s own automation and write only into a generated output mod, and only after validation has passed:

* Phase 2: the accepted single-project path (`docs/CONVERSION.md`).
* Phase 3 (0.3.0): controlled **package/batch** conversion. Batch Preview, Dry Run, Run Batch, independent per-project validation, and an explicit commit of the successful projects only (`docs/BATCH.md`).
* Phase 3.1 (0.3.1): opt-in, default-OFF **embedded CBBE → CBBE-3BBB body replacement** for outfits that build a CBBE body into their mesh (`docs/BATCH.md` §8).
* 0.3.2: read-only **post-build check** `verify-build` and the **in-game item** that uses each mesh (`docs/BATCH.md` §9).
* 0.4.0: **two source paths**.
  * A. BodySlide-project conversion, as above.
  * B. **Direct game-NIF** analysis for packages that ship built meshes and a plugin but no BodySlide projects. It is read-only and opt-in: `analyze-package --allow-direct-nif` (`docs/BATCH.md` §10). Many older Fallout 4 clothing packages are distributed this way.
* 0.4.1: the **controlled Direct-NIF write path** for explicitly selected HIGH-confidence meshes: `plan-direct-batch` → `convert-direct-batch` → `commit-direct-batch` → `verify-direct-build` (`docs/BATCH.md` §11). The converted game mesh goes to the output mod at the same game path, so MO2 overrides the source without any plugin change. No mass conversion, no automatic commit.
* 0.4.2: Direct-NIF validation checks every vertex attribute on its own (positions, UVs, normals, tangents/bitangents, colors, eye/landscape data, unknown bytes, topology), so a failure names exactly what changed. Finding from the first real Windows batch: Outfit Studio 5.8.2's interactive session recomputes seam normals (`docs/RESEARCH.md` §12).
* 0.4.3: **post-export restoration**. In Direct-NIF mode Outfit Studio is only the skinning backend. The converter checks the raw export: only the intended skinning, plus Outfit Studio's normal/tangent regeneration on duplicated seam vertices, may differ. It then restores every other vertex byte from the source and validates the final file strictly (`docs/BATCH.md` §11).
* 0.5.0 (this version): **mass conversion for normal users**. Select a mod, press **Convert** (Safe, or Convert All Possible), wait, build in BodySlide.
  * Every outfit is validated on its own. An outfit that cannot be converted is listed by name, its original stays unchanged, and the batch continues.
  * Crashes and interruptions resume where they stopped.
  * Includes the fix for the CP5 bodysuit regression (`docs/BATCH.md` §12).

A standalone Windows tool for Fallout 4 that scans a BodySlide library, whether it is installed through Mod Organizer 2 or directly in the game's `Data` folder. For every BodySlide project (slider set) it decides whether the outfit needs a **CBBE → CBBE 3BBB** physics weight conversion, is already converted, or should be left alone.

Phase 1 is read-only. It never writes into the game folder, the MO2 instance, any mod, any BodySlide project, any NIF/OSP/TRI or any source asset. The only things it writes are its own files inside the tool folder:

| Folder | Content |
| --- | --- |
| `config/profiles.json` | installation profiles (local JSON, no registry) |
| `reports/latest.json`, `latest.csv`, `latest.html` | the latest analysis; `reports/history/` keeps older JSON runs |
| `logs/app.log` | rotating log, with user-profile paths redacted |
| `cache/analysis.sqlite` | per-project analysis cache, keyed by file size, mtime and settings |

The tool refuses to run if its own folder sits inside the game, MO2 or mods folder.

Conversion (Phase 2 milestone) is described in `docs/CONVERSION.md`: Dry Run, Convert, validation, manifest, and the manual BodySlide build. Outfit Studio 5.8.0–5.8.2 has an upstream bug: headless *CopyBoneWeights* does nothing. For those versions the tool therefore opens Outfit Studio's window, and you press *Execute* once (see `docs/UPSTREAM_ISSUE.md`).

### Quick start (Windows)

```bat
cd "T:\Fallout 4 Tools\CBBE3BBBConverter"
setup_env.bat          :: once: creates .venv and installs numpy, scipy, PySide6, pytest
run_gui.bat            :: start the GUI
run_tests.bat          :: run the test suite
run_analyze.bat --mo2 "T:\MO_Main _ baseCBBE"          :: headless analysis → reports\latest.*
run_convert.bat --profile NAME --project "MM69 Raider Punk Top - CBBE"        :: Dry Run (writes nothing)
run_convert.bat --profile NAME --project "MM69 Raider Punk Top - CBBE" --yes  :: convert
run_revalidate.bat --profile NAME --workspace "work\conversions\<id>" [--commit] :: re-check a finished run (no Outfit Studio rerun)
run_batch.bat analyze-package --profile NAME --package "<MO2 mod>"                :: Phase 3 Batch Preview (writes nothing to mods)
run_batch.bat convert-package --profile NAME --package "<MO2 mod>" [--mode all-possible] --yes :: 0.5.0 one step: convert + validate + install
run_batch.bat plan-direct-batch --profile NAME --package "<MO2 mod>" --allow-direct-nif --select "<mesh>.nif" :: 0.4.1 Direct-NIF PLAN (read-only)
```

`setup_env.bat` looks for Python 3.10+ in this order: `%CBBE3BBB_PYTHON%`, `%USERPROFILE%\MiniConda3`, `%LOCALAPPDATA%\miniconda3`, `%ProgramData%\miniconda3`, `py -3`, then `python`. Everything it installs goes into `.venv` inside the tool folder.

Manual equivalent:

```bat
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements-dev.txt
set PYTHONPATH=%CD%\src
.venv\Scripts\python -m cbbe3bbb gui
.venv\Scripts\python -m cbbe3bbb detect  --mo2 "T:\MO_Main _ baseCBBE"
.venv\Scripts\python -m cbbe3bbb analyze --mo2 "T:\MO_Main _ baseCBBE" --include-disabled
.venv\Scripts\python -m pytest -q
```

### Using the GUI

`run_gui.bat` opens the simple window (0.5.0):

1. The status rows show Fallout 4, Mod Organizer 2, the CBBE 3BBB body, the output mod (enabled in MO2 or not) and Outfit Studio.
2. Choose the outfit mod. *Source body* is detected (Auto-detect → CBBE), *Target body* is CBBE 3BBB; other bodies are listed as "not implemented".
3. Choose **Safe** (only what can be verified with high confidence) or **Convert All Possible** (also tries uncertain outfits; a warning explains what that means).
4. **Analyze** shows how many outfits are ready, would be tried, are already compatible, need nothing, or cannot be processed safely. **Details** opens the engineering table.
5. **Convert** runs everything: *Converting … 87 / 164 · Converted · Needs attention · Remaining*. Outfit Studio may open during conversion. **Do not interact with it unless the converter explicitly asks you to continue manually.** (0.5.3: the converter runs each Outfit Studio session itself — one session per up to 25 outfits — including the "Choose cloth data" question when the answer can be proven.) If the automatic control cannot continue, the converter says so and offers *Retry automatically*, *Continue manually* or *Stop for now*; your progress is kept either way. Outfits that cannot be converted appear by name in *Problems*, and the conversion continues.
6. The summary shows **Completed**, **No conversion needed** and **Needs attention**, with the name and a short reason of every outfit that did not get 3BBB physics. Their originals are unchanged, but that is not counted as a success. From there:
   * **Build Converted Outfits** checks that the output mod is enabled and says exactly what to build: **Build only: CBBE 3BBB Auto Conversions - <mod>**, preset CBBE Curvy, Build Morphs ON. Do not batch-build the original projects of the mod after that group — both build the same game mesh files. *Check the build* then confirms every outfit (BUILD_OK) or explains what is missing.
   * **View Problems** gives each outfit a short reason, with *Technical Details* on request.
   * **Export Diagnostic Report** writes one anonymised ZIP for a bug report.
   * **Resume** continues an interrupted conversion.
   * **Report in-game problem…** records that a converted outfit has no physics (or a problem) in game. The outfit is then listed under *Needs attention*; the automatic check result is kept.

*Settings / Advanced* (or `run_gui.bat --developer`) opens the engineering window described below. It holds the profile fields, the library table, the single-project and package windows, and every report.

Engineering window:

1. Create or select a **profile**, then set the *Fallout 4 directory* and/or the *MO2 directory* (the folder that contains `ModOrganizer.ini`). *Mods directory* and *BodySlide directory* are filled in automatically when left empty. *Output mod* is disabled in Phase 1.
2. **Detect Installation** shows the MO2 version and profile, the mod counts, the BodySlide version and location, and the detected CBBE 3BBB reference.
3. **Analyze Library** scans every winning `SliderSets\*.osp` in the MO2 virtual file system, in the background with a progress bar. **Cancel** stops the scan and keeps partial results.
4. Filter the table (*All, Convert candidates, Already 3BBB, Review, Unsupported, Errors*) or search it. Click a row to see the reasons, warnings, files, family evidence and per-shape region metrics.
5. *Open HTML report* opens `reports\latest.html`, a self-contained page with the same table, filters and details.

"Include mods disabled in the MO2 profile" also scans disabled mods. They rank below every enabled mod, so they never change what enabled mods resolve to, and their projects show provider kind `disabled-mod`.

### Converting (Phase 2 milestone)

1. Download **BodySlide and Outfit Studio 5.8.x** and extract it to its **own** folder outside MO2 and the game, for example `T:\Fallout 4 Tools\BodySlide 5.8.2`. Do not replace the MO2 BodySlide mod.
2. Set *Outfit Studio 5.8+* to that folder and press **Check**. You should see version, path and "headless automation supported: YES".
3. Analyze, select **MM69 Raider Punk Top - CBBE**, then **Convert selected…** → **Dry Run**. Review the preview and the automation script tab, then press **Convert**.
4. With 5.8.0–5.8.2, Outfit Studio opens: *File → Open Automation… (Ctrl+Shift+A)* → choose the `cbbe3bbb_…` script → **Execute** → OK → close the dialog and close Outfit Studio (answer *No* to saving). (This single-project path is the manual one; package conversion — the Convert button, `convert-package` — runs Outfit Studio unattended since 0.5.3.)
5. The tool validates the result. Only on SUCCESS does it write to `CBBE 3BBB Auto Conversions`, together with `manifest.json`.
6. Build with BodySlide (CBBE Curvy + Build Morphs; see `docs/CONVERSION.md`), then test in game.

### Actions

| Action | Meaning |
| --- | --- |
| `CONVERT` | CBBE outfit, tight over breast/butt/thigh, no 3BBB weights: a good automatic conversion candidate |
| `KEEP` | already converted (3BBB physics bones carry plausible weights); do not re-convert |
| `REVIEW` | needs a human decision: outerwear, loose/far garments, rigid accessories, layered outfits, suspicious existing 3BBB weights, undeclared in-place edits, partial conversions |
| `SKIP` | nothing to do: no physics region under the item, a body-only project, or superseded by a 3BBB project that builds the same mesh |
| `UNSUPPORTED` | not a CBBE project (BodyTalk/male, Fusion Girl, non-Fallout-4 NIF) |
| `ERROR` | the project cannot be analyzed (malformed OSP, missing ShapeData NIF, unreadable NIF) |

Every row carries its reasons. Having 3BBB bones in a mesh is never treated as proof of good weights. See `docs/CLASSIFICATION.md`.

### Documentation

- `docs/RESEARCH.md`: findings about this installation, the BodySlide/Outfit Studio versions, automation options, risks and licensing
- `docs/ARCHITECTURE.md`: modules and data flow
- `docs/FILE_FORMATS.md`: OSP, RefTemplates, MO2 and NIF details the analyzer relies on
- `docs/CLASSIFICATION.md`: the decision rules
- `docs/CONVERSION.md`: single-project conversion (Phase 2)
- `docs/BATCH.md`: package/batch conversion (Phase 3) — §13 the unattended Outfit Studio control (0.5.3)
- `docs/RENAME_PLAN.md`: the planned rename to "Fallout 4 Body & Physics Converter"
- `docs/TESTING.md`: tests and manual acceptance
- `CHANGELOG.md`

### Assets and privacy

The repository contains no CBBE, 3BBB, Vtaw, ASA, Raider Punk or other third-party meshes or assets. Tests use a procedurally generated cylinder "body" and synthetic outfits (`tests/synth.py`). The tool has no telemetry, network access or cloud dependency.
