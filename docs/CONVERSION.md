# Conversion (Phase 2: single project)

This document describes the **single-project** conversion path that was accepted in game with `MM69 Raider Punk Top - CBBE` ("Top V1"). The profile setting `conversion.allowed_projects` gates which projects this path may convert; by default it lists only that project. Package/batch conversion (Phase 3) reuses the same planning, script, validation, evidence and commit code: see `BATCH.md`.

Since 0.3.0 plans are made **per shape**: each outfit shape has its own regions, allowed physics bones, copy mode and confidence, and validation checks each shape against its own plan. Source ShapeData with cloth physics data (BSClothExtraData) is supported: Outfit Studio asks "Choose cloth data" when saving, and you tick only the project's own file. The check `cloth physics data (BSClothExtraData) unchanged` verifies the result.

Since 0.3.1 a plan has a **conversion mode**: `outfit_only` (MODE A, the default and the only mode unless the embedded-body option is on) or `embedded_body_replacement` (MODE B: one positively identified embedded CBBE body gets the CBBE-3BBB reference's weights vertex by vertex, keeping its geometry, sliders and zaps; see `BATCH.md` §8). The automation invariants now also require MaxResults 1 for every CopyBoneWeights step with radius < 0.5, relative `SliderDataFile` paths, and no `ImportSliderData` with FromFolder.

Since 0.3.3 a shape can have its **own search radius**. That happens when some of its vertices have no reference vertex within the configured radius, because Outfit Studio would leave them without any weight. The radius is raised to the smallest 0.5 step that reaches them, only when the emulated copy changes no other vertex and the new radius is at most 2× the configured one; otherwise the project is REVIEW. The "weights unchanged" check of untouched shapes compares against the source *after* the per-vertex normalization that Outfit Studio's save always applies (nifly `SetShapeVertWeights`).

## Two source paths (0.4.0)

* **A. BodySlide-project conversion** (everything below): source OSP + ShapeData → Outfit Studio → generated BodySlide AUTO project → BodySlide build → `verify-build`.
* **B. Direct game-NIF** (analysis 0.4.0, conversion 0.4.1; `BATCH.md` §10–11): plugin + built game mesh → classification and a per-shape plan → explicit selection → Outfit Studio `ImportFile` / `CopyBoneWeights` / `ExportFile` on a workspace copy → structural + weight validation → explicit commit of the game mesh to `<output mod>\<same game path>` → `verify-direct-build`.

### Direct-NIF conversion (0.4.1)

Implemented as designed from the Outfit Studio 5.8.2 source and the sandbox runs (`RESEARCH.md` §10–11), with these decisions:

1. **Import from an absolute path** of the workspace copy (`meshes\<nn>_<id>\source\<name>.nif`); `ImportFile` resolves relative paths against the project, absolute ones are used as they are. The source mod file is never opened by Outfit Studio.
2. **Full copy only.** Each CopyBoneWeights step targets the planned shapes (no BoneList, MaxResults 4, the shape's radius). A mesh whose full copy would add 3BBB bones outside its planned regions is REVIEW (FULL_COPY_OUTSIDE_REGIONS) already in the analysis, because region-restricted copies are not validated in game.
3. **ExportFile** with an absolute workspace path, `WithRef=false`, `UseOriginalPath=false`, no prefix/suffix. `check_direct_script` refuses anything else, and any SaveProject/AddProject step.
4. **No cloth, no embedded body, no body-slot items** in 0.4.1. The body identity-copy step and cloth handling stay future work.
5. **Validation** compares the exported file with the source copy block by block (`nif/structure.py`), independent of block numbering: Outfit Studio adds bone nodes and rewrites skin blocks, but must keep everything else.
6. **Post-export restoration (0.4.3):** interactive Outfit Studio 5.8.2 recomputes seam normals (`Mesh::SmoothNormals`) and `ExportNIF` writes them together with regenerated tangents (`RESEARCH.md` §12). Outfit Studio is treated as the skinning backend only. `conversion/restore.py` keeps the raw export and applies the structural gate and the raw-diff policy (allowed: skinning, and normal/tangent basis on duplicated seam vertices). It then writes FINAL = source immutable vertex bytes + raw-export skin weights/bone indices through a temporary file, proven byte-exact before it is promoted. The strict per-field validation runs on FINAL.
7. **Commit** of the single game mesh file (no BodySlide build stage), with a `direct_conversions` manifest record; `verify-direct-build` replaces verify-build for this path.

## Pipeline

```
Phase 1 analysis (fresh re-analysis of the selected project)
  → Dry Run: plan + Outfit Studio automation script (writes nothing)
  → Convert:
      1. hash + stage source files into  <tool>\work\conversions\<id>\Data\Tools\BodySlide
      2. private Outfit Studio 5.8+ copy (<tool>\runtime\outfitstudio-<ver>) with its own Config.xml
         (TargetGame=3, ProjectPath = the workspace, GameDataPath = empty workspace Data folder)
      3. Outfit Studio runs the script (headless, or interactive for 5.8.0–5.8.2, see below)
      4. validation of the generated project + Phase 1 analyzer must say KEEP
      5. source files re-hashed (must be unchanged)
      6. only then: copy into the output mod + manifest.json
  → BodySlide build (manual, CBBE Curvy + Build Morphs)
  → in-game test by the user
```

Every run keeps its workspace: `plan.json`, the staged copies, `Automations\<script>.xml`, `logs\Log_OS.txt`, `logs\validation.txt` and `result.json`. On failure **nothing** is written to the output mod.

## The automation script

It reproduces the manual workflow that was validated in game: a full *Copy Bone Weights* with radius 5 and 4 targets. Comparing against the user's manual Top V2 test showed the manual step was a copy of all reference bones: the mean weight difference to the emulation is 0.0000, while a physics-bones-only copy would differ by up to 0.2.

| # | Step | Parameters |
| --- | --- | --- |
| 1 | `ClearProject` | |
| 2 | `LoadReference` | `SliderSets/CBBE 3BBB Body.osp`, set `CBBE 3BBB Body`, shape `CBBE-3BBB`, MergeSliders/Zaps = true |
| 3 | `AddProject` | the staged source OSP, set `MM69 Raider Punk Top - CBBE`, AppendNewSliders = true |
| 4 | `DeleteSlider` | the 13 sliders only the reference has (keeps the original 71-slider list) |
| 5 | `CopyBoneWeights` | TargetMeshes = `Model001_25_upper_1_0_0`, ProximityRadius = 5.0, MaxResults = 4 (No Target Limit = false), no BoneList (full copy) |
| 6 | `SaveProject` | DisplayName `MM69 Raider Punk Top - CBBE - 3BBB AUTO`, new SliderSet file/ShapeData folder/file, **OutputDataPath `meshes\MM69 Raider Punk` + OutputFileName `raiderPunk_top` preserved**, GenWeights = false (as the source), **AutoCopyRef = false (explicit)**, CopyRefFromProject = false |

`check_invariants` refuses any script without an explicit `<AutoCopyRef>false</AutoCopyRef>`, with more than one SaveProject, with absolute or `..` paths, or with geometry-changing steps.

## Region-aware weight copy

The plan predicts, per outfit shape, which 3BBB physics bones a full copy would add. For Top V1 that is exactly the 6 breast bones and no butt or thigh bones. Mode `auto` uses the full copy only if every predicted bone belongs to a region the Phase 1 analysis marked as relevant. Otherwise it switches to a `BoneList` copy restricted to those regions, plus the superseded `L/RBreast_skin` / `L/RButtFat_skin` bones. That mode is flagged as not yet validated in game. Validation fails if the output carries any physics bone outside the planned regions. Non-physics weights are preserved the way Outfit Studio does it: a full copy replaces the weights of the bones the reference has and rescales the outfit's other bones.

## Outfit Studio runtime

The user's MO2 BodySlide 5.7.1 is never touched. Select a **separate** Outfit Studio 5.8+ folder (for example the extracted release download). The tool copies its program files, without project folders, into `<tool>\runtime\outfitstudio-<version>`. Detection reports version, path and "headless automation supported: YES/NO". Conversion is blocked when the version is below 5.8.0 or the executable lacks the `-a/--automation` option.

### Known upstream limitation: headless CopyBoneWeights is a no-op in 5.8.0–5.8.2

In `-a` mode Outfit Studio never shows its window, so the OpenGL panel is never initialized (`extInitialized` stays false). Outfit Studio therefore never creates the per-shape meshes, and the weights CopyBoneWeights computes are applied only through those meshes (`OutfitStudioFrame::ActiveShapesUpdated`). The step logs "Copying bone weights…" but changes nothing, and SaveProject writes the original weights.

This was reproduced with Outfit Studio 5.8.2 built from the release source and run headless. The tool's validation caught it (no 3BBB bones, analyzer still CONVERT) and marked the run FAILED without writing to the output mod. The same code path exists in the Windows build and on upstream `main` as of 2026-08-17. See `UPSTREAM_ISSUE.md`.

The tool handles this in two ways:

* For Outfit Studio **5.8.0–5.8.2**, run mode `auto` selects **interactive**. The tool opens the private Outfit Studio normally, pointed at the conversion workspace. You press *File → Open Automation… (Ctrl+Shift+A)*, choose the script, press **Execute**, confirm, close the Automation window and close Outfit Studio (answer *No* if asked to save; the script already saved the new project). The script is Outfit Studio's own automation, running through Outfit Studio's own code, so it writes the weights itself. The tool then validates and commits exactly as in headless mode.
* For later versions it uses headless mode. If a later version still has the bug, validation fails safely.

With the window initialized (a diagnostic sandbox build that shows the frame during `-a`), the identical script produced a result that passed every check (see below).

## Did the script actually run? (execution evidence)

Tool 0.2.0 decided this from the `Automation: Step … SaveProject` lines in `Log_OS.txt`. In an **interactive** run those lines never reach the file: in Outfit Studio 5.8.2, `AutomationDialog::StartProgress` makes a `wxLogTextCtrl` (the Automation window's output pane) the active log target until `EndProgress`. On Windows, the first real run therefore passed every content check but was reported FAILED ("no SaveProject step in its log"). Since 0.2.1, the evidence is the artifacts that only the prepared script creates, checked against the Outfit Studio session the tool started. Each of these is a required check:

| Check | Meaning |
| --- | --- |
| `execution: Log_OS.txt is from an Outfit Studio session on this workspace` | the log contains `Project path in config: <workspace>\Data\Tools\BodySlide` |
| `execution: planned outputs created by this Outfit Studio session` | the planned `.osp` and ShapeData `.nif` did not exist before Outfit Studio started, and both exist afterwards, with modification times inside the session (±2 s). The window comes from the tool's clock around the process, or, when revalidating, from the `Log_OS.txt` timestamps |
| `execution: saved project matches the prepared SaveProject step` | exactly one set, named `<source> - 3BBB AUTO`, with the `ReferenceInfo` (reference project file, set, shape) that Outfit Studio's save writes for the loaded reference |
| `execution: logged automation steps include SaveProject` | only when step lines exist (headless mode) |
| `execution: no automation step errors in Log_OS.txt` | |

Headless runs still need exit code 0. A window closed without pressing *Execute* fails with "closed without executing the conversion script".

## Revalidating an existing workspace (no Outfit Studio rerun)

```bat
run_revalidate.bat --profile NAME --workspace "work\conversions\<id>"            :: checks only, writes nothing to the mod
run_revalidate.bat --profile NAME --workspace "work\conversions\<id>" --commit   :: commit if every check passes
```

This reads `plan.json`, `result.json` and `logs\Log_OS.txt` and runs every validation check plus the execution evidence again. The session window comes from the log, and the log's session length must match the recorded run time within 5 s. The source files are hashed again against the hashes recorded before the original run. The originals (`result.json`, logs, generated files) are never changed. It writes `result.revalidated.json` and `logs\validation.revalidated.txt`. `--commit` uses the same gates as a conversion: milestone allow-list, output mod check, no overwrite. The manifest record carries `accepted_by: revalidate`, the workspace, and the evidence.

## Validation (all required unless noted)

Checks: valid OSP (one set with the new name), output path, output file name and GenWeights preserved, expected outfit shape(s) listed, reference shape not listed, original sliders preserved (names), zaps preserved, slider data kept for every source target, slider data resolvable (Outfit Studio keeps AddProject slider data as a reference to the source's `ShapeData\<folder>\*.osd`; it is read, never copied), valid ShapeData NIF, **no embedded CBBE-3BBB body** (no new shapes, no reference-sized shape), geometry unchanged, planned 3BBB bones present, no physics bones outside planned regions, weights normalized (Fallout 4 keeps at most 4 influences), cross-side/quality anomaly checks, predicted vs actual agreement ≥ 0.90, full-copy emulation match (informational), untouched shapes unchanged, **Phase 1 analyzer classifies the result as CBBE 3BBB / KEEP**, and **source files unchanged (SHA-256)**.

## Output mod

The default is `<MO2 mods>\CBBE 3BBB Auto Conversions`. You can configure it, and it is created only when a conversion succeeds. The tool writes only into a folder that is new, empty, or has its `manifest.json`. It never overwrites existing files.

```
CBBE 3BBB Auto Conversions\
  manifest.json
  Tools\BodySlide\SliderSets\MM69 Raider Punk Top - CBBE - 3BBB AUTO.osp
  Tools\BodySlide\ShapeData\MM69 Raider Punk Top - CBBE - 3BBB AUTO\MM69 Raider Punk Top - CBBE - 3BBB AUTO.nif
  Tools\BodySlide\SliderGroups\CBBE 3BBB Auto Conversions.xml
```

`manifest.json` records per conversion: source mod/project/OSP/ShapeData with SHA-256 of every source and reference file, target project and game output, generated files with SHA-256, Outfit Studio version/path/run mode, settings, reference, slider-data dependencies, validation summary, timestamp, tool version, workspace path.

To undo: disable or delete the output mod (or the files listed in the manifest).

## BodySlide build (manual in this milestone)

1. In MO2 press F5, enable **CBBE 3BBB Auto Conversions** and place it after (below) *Raider Punk CBBE - BT3 BT4 A-Body*.
2. Recommended: add an MO2 executable *BodySlide AUTO build* that points to your BodySlide x64.exe with these arguments:
   `--groupbuild "CBBE 3BBB Auto Conversions" --targetdir "T:\MO_Main _ baseCBBE\mods\CBBE 3BBB Auto Conversions" --preset "CBBE Curvy" --trimorphs`
   The mesh (`meshes\MM69 Raider Punk\raiderPunk_top.nif` + `.tri`) then goes into the output mod, and the Raider Punk mod's own meshes stay untouched.
3. Alternatively, in the BodySlide GUI select *MM69 Raider Punk Top - CBBE - 3BBB AUTO*, preset *CBBE Curvy*, tick *Build Morphs*, and Build. A normal build writes the mesh wherever MO2 already has it, so it overwrites `raiderPunk_top.nif` in the Raider Punk mod.

In the sandbox, BodySlide 5.8.2 group-built the generated project into a target folder with `--trimorphs`. That produced `raiderPunk_top.nif` (61 704 bytes, 1 shape, 6 breast bones) and `raiderPunk_top.tri` (246 029 bytes).

## Existing manual Top V2 TEST (left untouched)

| Artifact | Location |
| --- | --- |
| OSP set | `T:\MO_Main _ baseCBBE\mods\Raider Punk CBBE - BT3 BT4 A-Body\Tools\BodySlide\SliderSets\MM69 Raider Punk Top V2 - CBBE.osp`, 2nd set `MM69 Raider Punk Top V2 - 3BBB TEST` (the file also holds the original `…Top V2 - CBBE` set) |
| ShapeData | `T:\MO_Main _ baseCBBE\overwrite\Tools\BodySlide\ShapeData\MM69 Raider Punk Top V2 - 3BBB TEST\MM69 Raider Punk Top V2 - 3BBB TEST.nif` (1 050 674 bytes) |
| Built mesh | `T:\MO_Main _ baseCBBE\mods\Raider Punk CBBE - BT3 BT4 A-Body\meshes\MM69 Raider Punk\raiderPunk_top_v2.nif` (1 050 766 bytes) + `raiderPunk_top_v2.tri` |
| Embedded reference body | shape `CBBE-3BBB` (22 708 vertices) inside both the TEST ShapeData NIF and the built `raiderPunk_top_v2.nif`, listed as a Shape in the TEST set |

Cleanup instructions follow only after the automated Top V1 result has passed your in-game test.
