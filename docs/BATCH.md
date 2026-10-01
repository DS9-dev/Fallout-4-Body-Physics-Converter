# Package / batch conversion (Phase 3)

Phase 3 converts the safe candidates of **one package** (one MO2 mod) in a controlled batch. It does not copy 3BBB bones onto every outfit: each project, and each shape inside it, is analyzed, planned and validated on its own. Nothing is written to the output mod before an explicit **commit**, and only projects whose validation passed are committed.

```
Analyze Package (Batch Preview, writes only reports\packages)
  → Dry Run (selection + Outfit Studio session scripts, writes only reports\packages)
  → Run Batch (batch workspace; Outfit Studio; independent validation of every project; never commits)
  → Validate (optional: the same checks again from the workspace, without Outfit Studio)
  → Commit Successful (only VALIDATED projects; one transaction per project)
  → BodySlide build of the AUTO projects (manual) → in-game test
```

GUI: **Convert Package…** in the main window. CLI (`run_batch.bat` passes its arguments on):

```bat
run_batch.bat analyze-package --profile NAME --package "Raider Punk CBBE - BT3 BT4 A-Body"
run_batch.bat plan-batch      --profile NAME --package "..." [--select "Project"]... [--deselect "Project"]...
run_batch.bat convert-batch   --profile NAME --package "..." [--select "Project"]... --yes
run_batch.bat validate-batch  --profile NAME --batch <id>
run_batch.bat commit-batch    --profile NAME --batch <id> --yes [--only "Project"]...
run_batch.bat batch-status    --profile NAME [--batch <id>]
run_batch.bat packages        --profile NAME
```

The GUI and the CLI call the same `AppController` methods and the same engine (`conversion/package.py`, `conversion/batch.py`). Both use the Phase 2 `build_plan`, automation script, validation, evidence and commit code.

## 1. Package discovery and classification

Every BodySlide project whose `SliderSets` file the package provides as the MO2 winner is analyzed by the Phase 1 analyzer. Files of the package that another mod overrides are listed as a note. Package-level rules are applied on top of the analyzer. They are all conservative: when in doubt, a project becomes REVIEW.

| Action / status | When |
| --- | --- |
| `CONVERT / NEW` | the analyzer says CONVERT and the plan has no blocker. Selected by default only if every converted shape has HIGH confidence |
| `KEEP / ALREADY_CONVERTED` | the manifest records an AUTO conversion of this source (mod + project), the generated files still have the recorded hashes, and every recorded source file (resolved through MO2 now) has the recorded hash |
| `REVIEW / STALE` | as above, but a source file changed or is now provided by another mod. Nothing is overwritten |
| `REVIEW / AUTO_CHANGED` | the recorded generated files are missing or were edited |
| `REVIEW / UNKNOWN_AUTO` | a project file with the generated name exists without a manifest record |
| `REVIEW / EMBEDDED_BODY` | the outfit builds an embedded non-3BBB body (e.g. `CBBE`) into its mesh. Converting only the clothes would leave that body without physics under physics-weighted clothes, and embedding the CBBE-3BBB body is not done automatically (AutoCopyRef stays false) |
| `REVIEW / MANUAL_3BBB_VARIANT` | a 3BBB project already builds the same game mesh (e.g. `Top V2 - 3BBB TEST`) |
| `REVIEW / BLOCKED` | the plan has a blocker (missing slider data, headless run with cloth data, …) |
| `REVIEW / DUPLICATE_NAME` | several sets in the package share the name |
| `KEEP / ALREADY_3BBB`, `SKIP`, `UNSUPPORTED`, `ERROR`, `REVIEW / ANALYZER_REVIEW` | the Phase 1 analyzer's result |

Only CONVERT projects can be selected. The normal batch cannot force-convert REVIEW, UNSUPPORTED, SKIP, KEEP or ERROR projects.

## 2. Per-shape regions and bones

Regions come from the installed CBBE 3BBB reference: `BREAST`, `BUTT`, `THIGH`. The installed reference has no separate pelvis physics bones; the butt bones cover the pelvis area. Each outfit shape gets its own decision:

* **CONVERT** with its planned regions: every region whose geometry/reference correspondence is at least MEDIUM relevance. The shape may receive only those regions' physics bones (`allowed_bones`). Copy mode is `full` (as Top V1) when a full copy is predicted to add only allowed bones, otherwise `regions` (a BoneList copy, confidence at most MEDIUM, not validated in game yet).
* **NONE**: no physics region under the shape (boots, bracelets, decorative parts). It is not a weight-copy target and its weights must stay unchanged.
* **BODY**: an embedded body shape; kept unchanged.
* **UNKNOWN**: no correspondence could be computed.

For each shape the preview shows distance statistics, the share of vertices within the search radius, the reference-region coverage, predicted vertices, predicted bones, left/right balance and side coverage, predicted cross-side weight, confidence and reasons. Names are never the primary signal.

The script has one `CopyBoneWeights` step per group of shapes with the same mode and bone list. For a single-shape project in full mode (Top V1) it is byte-identical to 0.2.1 (a regression test rebuilds the accepted Top V1 script from its real `plan.json`).

Settings are the accepted Phase 2 baseline: radius 5, max targets 4, No Target Limit off, AutoCopyRef false. They are recorded per project and per shape, so region-specific settings can be added later.

## 3. Outfit Studio sessions

Outfit Studio 5.8.2 needs interactive execution (headless CopyBoneWeights is a no-op, see `UPSTREAM_ISSUE.md`). Its automation runs any number of steps in order, and `ClearProject` resets the project, so **one script converts several projects in turn**: each project is `ClearProject → LoadReference → AddProject → DeleteSlider → CopyBoneWeights… → SaveProject`. `check_batch_invariants` enforces that every part starts with ClearProject, satisfies the single-project invariants (one SaveProject, AutoCopyRef=false, workspace-relative paths, no geometry-changing steps), and saves to its own file and folder.

**Cloth physics data.** Outfit Studio keeps the `BSClothExtraData` of every file loaded in a session (`OutfitProject::clothData` is not cleared by ClearProject). On every later save it shows a modal "Choose cloth data" dialog listing all of them. A session therefore contains all projects without cloth data first, then at most **one** project with cloth data as its last project. Every further cloth project gets its own session. Only that project's own ShapeData file may be kept (0.5.3: answered automatically when provable, §13). The validation check `cloth physics data (BSClothExtraData) unchanged` compares the output's cloth blocks with the source's, so a wrong answer (none, or another project's data) fails that project. In headless mode the dialog would block, so cloth projects are blocked there.

`batch_session_mode = per_project` in the profile gives every project its own session.

Execution evidence is the 0.2.1 system, per project. The session must be on this batch's project folder. The project's own planned outputs must be new and written inside the session window. The saved project must match its SaveProject step (name and ReferenceInfo). Step lines are checked only when Outfit Studio logs them (headless), and then only the project's own step range; errors are attributed to the step that logged them.

If an "Automation Error … Continue?" dialog appears, the answer is **Yes** (the unattended control does this; manually, press Yes): the next project starts with ClearProject, and the failing project is caught by its own validation.

## 4. Batch workspace

```
work\batch_conversions\<stamp>_<package>\
  batch.json            selection, sessions (script, projects, step ranges, time window), source hashes, state
  preview.json          the Batch Preview the batch was created from
  summary.json          per-project status (also report.txt / report.html)
  cbbe3bbb_batch_<id>[_n].xml   the session scripts
  logs\Log_OS.session<n>.txt, stdout.session<n>.txt
  Data\Tools\BodySlide\ the batch's private Outfit Studio project folder (staged sources + generated projects)
  projects\<nn>_<project>\
      plan.json, <script>.xml (the project's own script), shapes.json
      result.json (run) / result.revalidated.json (validate-batch), commit.json
      logs\Log_OS.txt, logs\validation.txt / validation.revalidated.txt
```

Statuses: `PLANNED → VALIDATED | REVIEW | FAILED → COMMITTED`. REVIEW means the result is structurally sound but a weight-quality check failed (anomalies, low predicted-vs-actual agreement, planned bones missing, analyzer not KEEP). FAILED is everything else: not executed, embedded body, new or missing shapes, changed geometry or sliders, unexpected physics bones, cloth data, source changed, evidence. A failed project never affects another project.

**Recovery.** `validate-batch` rebuilds every result from the workspace. The source hashes are kept in `batch.json`. If the copied session log is missing, the runtime's `Log_OS.txt` is used when its project path belongs to this batch.

## 5. Commit

`commit-batch` (GUI: **Commit Successful**) writes only VALIDATED projects. For each one it first re-checks that:

* the generated files still have the hashes recorded at validation;
* every source file still has its pre-run hash;
* the output mod is tool-owned and the files do not exist yet.

The write itself is the Phase 2 transaction: project file, ShapeData folder, groups and manifest record. On any error that project's files are removed and the groups and manifest are restored. Running commit again never writes a project twice.

Manifest record (merged: earlier records, including the accepted Top V1, are kept unchanged) has the source mod/project/files with SHA-256, the generated project and files, timestamp, tool version, Outfit Studio version and run mode, reference project and shape, settings, regions and physics bones, `regions_by_shape` (regions, allowed bones, copy mode, confidence), shape decisions, confidence, validation result and metrics, execution evidence, `accepted_by: batch <id>` and the batch workspace.

## 6. BodySlide groups and build

* `CBBE 3BBB Auto Conversions`: every generated project.
* `CBBE 3BBB Auto Conversions - <package>`: the package's generated projects, from the whole manifest history, so the package can be Batch Built on its own.

Conversion and BodySlide build are separate stages; nothing builds meshes automatically. After a commit, the report lists the exact projects to tick in BodySlide (preset CBBE Curvy, Build Morphs ON, group of the package). The original project and its AUTO project intentionally build **the same game mesh**, and the last build wins, so build the AUTO projects after the original package.

## 7. Limitations (Outfit Studio 5.8.2)

* Interactive only: one Execute per session. Cloth projects add sessions and a dialog answer each.
* The "Choose cloth data" dialog cannot be answered by a script. The tool orders sessions so the dialog lists only the saving project's own file, and validation checks the result.
* No GUI mouse/keyboard automation is used.

## 8. Embedded CBBE body replacement (Phase 3.1, opt-in, default OFF)

Some outfits build part of a CBBE body into their mesh. Without the option those projects stay `REVIEW / EMBEDDED_BODY` exactly as in 0.3.0 (MODE A, `outfit_only`).

With **"Allow safe embedded CBBE → CBBE-3BBB body replacement"** (GUI, Convert Package window) or `--allow-embedded-body-replacement` (CLI `analyze-package`, `plan-batch`, `convert-batch`) each body shape is identified with several signals (`conversion/body.py`):

| Signal | Required |
| --- | --- |
| vertices coincide with CBBE-3BBB reference vertices (≤ 0.001) | every vertex |
| welded triangles are a subset of the reference's | ≥ 99.9 % |
| body slider morphs equal the reference's morphs at the same vertices | ≤ 0.05 |
| no 3BBB physics bones already weighted | yes |
| shape named like a body | weak hint only; never enough on its own |

| Class | Result |
| --- | --- |
| `BODY_CBBE_COMPATIBLE` | exactly one such body, no manual 3BBB variant → `CONVERT / EMBEDDED_BODY_REPLACEMENT` (MODE B), never selected by default |
| `BODY_ALREADY_3BBB` | the body already has 3BBB weights → KEEP rules |
| `BODY_AMBIGUOUS` | sculpted, remeshed, custom morphs, partially 3BBB → `REVIEW / EMBEDDED_BODY_AMBIGUOUS` |
| `BODY_UNSUPPORTED` | not the CBBE topology (other body family) → `REVIEW / EMBEDDED_BODY_UNSUPPORTED` |
| more than one body | `REVIEW / MULTIPLE_BODIES` |

**MODE B** keeps the body's geometry, deleted vertices, triangles, sliders and zaps. It copies the CBBE-3BBB reference's weights onto it vertex by vertex (radius 0.01, one target; the body never gets the clothing CopyBoneWeights), imports its slider data into the project, and saves with AutoCopyRef=false (RESEARCH.md §7). Accessories and non-physics shapes stay untouched. The target body is the installed CBBE-3BBB reference; its template, project, set, shape, source file, SHA-256, vertex count and geometry signature are recorded in the plan and the manifest (`body_replacement`).

Additional required checks in MODE B: source body positively identified; target reference unchanged since the plan (SHA-256); exactly one body in the output; body geometry unchanged; every body vertex coincides with the reference; body weights identical to the reference; no legacy physics bones on the body; the reference's 3BBB physics bones present on the body; body slider morphs identical and resolvable; body zaps preserved. All earlier checks (cloth data, sliders, outputs, evidence) still apply.

Rerun: an unchanged MODE B conversion is `KEEP / ALREADY_CONVERTED`. If only the CBBE-3BBB reference changed it becomes `REVIEW / TARGET_REFERENCE_CHANGED` ("regeneration may be required"); nothing is overwritten.

Acceptance after an in-game test is recorded with `record-acceptance --generated "<project> - 3BBB AUTO" --state PASS|FAIL|PARTIAL --note "…"`. It appends to that record's `acceptance.history` and never changes other records.

## 9. After the BodySlide build: verify-build and the in-game item (0.3.2)

The validation checks prove that the *generated project* is right. What the game shows is decided by two further stages:
- the mesh BodySlide built;
- the plugin's armor addon (ARMA) that points at that mesh.

Neither stage is visible to the conversion checks, so after the build run:

```bat
run_batch.bat verify-build --profile <NAME> [--generated "<project> - 3BBB AUTO"]
```

It is read-only and writes only `reports\builds\*.json/.txt`. For every committed conversion it checks the following:

| Check | Meaning |
| --- | --- |
| generated ShapeData unchanged since the commit | SHA-256 against the manifest |
| built game mesh present (MO2 winner) | `meshes\<OutputPath>\<OutputFile>.nif` resolved over overwrite + mods; every overridden copy is listed |
| built after the conversion was committed | the mesh's modification time is not older than the commit |
| `<shape>`: 3BBB physics bones survived the build | the same physics bones are weighted in the built mesh |
| `<shape>`: bone list and weights identical to the generated ShapeData | BodySlide morphs positions, never weights (≤ 1e-4; zapped vertices are matched as a subset) |
| every generated shape is in the built mesh | |
| an in-game item of the package uses this mesh | note: ARMO name, editor ID, form ID and ARMA of the package's own plugins |

Result: `BUILD_OK`, `BUILD_MISMATCH`, `NOT_BUILT` or `BUILD_STALE`.

**The in-game item can have a different name than the project.** In Raider Punk:
- `MM69 Raider Punk Outfit - CBBE` builds `raiderPunk.nif`, which the game shows as **"Raider Punk Outfit V2"**;
- `MM69 Raider Punk Outfit 2 - CBBE` builds `raiderPunk_2.nif`, which the game shows as **"Raider Punk Outfit"**.

The Batch Preview lists the item for every project and warns with `GAME_ITEM_NAME_MISMATCH`. Always test the item that verify-build names. Plugins of other mods can override ARMA/ARMO records; only the package's own plugins are read.

## 10. Direct-NIF packages (0.4.0, read-only analysis)

Many older Fallout 4 clothing packages ship a plugin, materials, textures and **built** `meshes\*.nif`, but no BodySlide projects. The BodySlide pipeline cannot use them. `analyze-package --allow-direct-nif` (GUI: "Also analyze the plugin's game meshes") analyzes such a package from the game side, **without writing anything** except reports:

1. **Plugins:** the package's own `.esp/.esm/.esl` are read: ARMO (item, name, addons, biped slots) and ARMA (MOD2 male / MOD3 female third-person, MOD4/MOD5 first-person). Activation comes from the MO2 profile's `plugins.txt`. Candidates are only meshes an item really uses, never "every NIF in the folder". One mesh maps to all of its items (deduplicated, names reported).
2. **Source mode:** BODYSLIDE_PROJECT, DIRECT_NIF, MIXED (a mesh a BodySlide project of the package builds stays with that project: DUPLICATE_GAME_MESH), or NO_CONVERTIBLE_SOURCE.
3. **MO2 winner:** every provider of the mesh is listed. A losing package copy is never converted (MO2_CONFLICT); a mesh the package does not ship is skipped (NOT_PACKAGE_MESH); a referenced mesh nobody ships is an ERROR (MISSING_GAME_MESH).
4. **Sex / body family:** only female third-person meshes are analyzed.

   | Class | When |
   | --- | --- |
   | MALE | male-only models are not analyzed (counted as skipped); a male-named non-CBBE body inside a female-slot mesh → UNSUPPORTED MALE_MESH |
   | AMBIGUOUS | the same NIF is a male and a female model → REVIEW |
   | FEMALE_CBBE_COMPATIBLE | an embedded body lies on the CBBE-3BBB reference (HIGH), or no body and the garment lies on the CBBE surface (MEDIUM) |
   | FEMALE_ALREADY_3BBB | 3BBB physics bones are weighted |
   | FEMALE_NON_CBBE | a body shape that is not the CBBE topology → UNSUPPORTED |
   | UNKNOWN | nothing close to the CBBE surface → SKIP when no physics region, else REVIEW |

   Geometry alone cannot tell male clothing without a body from female clothing: in EasyGirl, Nate jeans lie within 0.83 units of the female reference. The plugin's female model slot is therefore required.
5. **Shapes:** each shape is analyzed with the Phase 1 metrics (regions, fit, coverage) and classified as OUTFIT, RIGID_ACCESSORY (≤ 4 bones and ≤ 1 500 vertices) or BODY_*. Embedded bodies are identified by geometry (Phase 3.1 `identify_body`; a built NIF has no slider data to compare).
6. **Action:** the Phase 1 rules (`classify_project`: fit, outerwear/loose/footwear/accessory words, layered outfits) decide first, then the direct-NIF gates:

   | Action / reason | When |
   | --- | --- |
   | CONVERT / DIRECT_NIF_HIGH_CONFIDENCE | HIGH fit, female CBBE, per-shape reach safe |
   | CONVERT / EMBEDDED_BODY_REPLACEMENT | with `--allow-embedded-body-replacement`: a positively identified partial CBBE body gets the reference weights by vertex identity (never the full body) |
   | KEEP / ALREADY_3BBB | existing 3BBB weights pass the quality checks |
   | SKIP / NO_PHYSICS_REGION, NOT_PACKAGE_MESH, DUPLICATE_GAME_MESH | |
   | REVIEW / FULL_COPY_OUTSIDE_REGIONS (0.4.1: a full copy would add 3BBB bones outside the planned regions), ANALYZER_REVIEW, RIGID_REGION_OVERLAP, EMBEDDED_BODY, AMBIGUOUS_BODY, MULTIPLE_BODIES, CLOTH_DATA_UNSUPPORTED, UNSUPPORTED_NIF_STRUCTURE, UNSAFE_RADIUS, MO2_CONFLICT, AMBIGUOUS, UNKNOWN | |
   | UNSUPPORTED / NON_CBBE_BODY, MALE_MESH | |
   | ERROR / MISSING_GAME_MESH, UNREADABLE_NIF | |

   The per-shape radius follows 0.3.3: it is raised only for the shape that needs it, only when no other vertex changes, and at most to 2× the configured radius.
7. **Layering risk:** if body-slot (33) items of the package embed a plain CBBE body that is not converted, converted clothing worn with them moves while their body does not. The report says so.

Reports: `reports\packages\<time>_<package>_direct.{json,csv,txt,html}`, one row per unique female mesh (items, sex/body, shapes, regions, confidence, action, reason, 3BBB state, embedded body, MO2 winner, SHA-256, planned bones and radius, warnings). The CLI prints a compact table; `--details` adds per-shape lines.

The analysis itself never converts anything. After a Direct-NIF commit it recognizes the result (KEEP ALREADY_CONVERTED_DIRECT_NIF, see §11).

## 11. Direct-NIF conversion (0.4.1)

Controlled conversion of **explicitly selected** HIGH-confidence Direct-NIF meshes. Every stage is its own command; nothing is selected by default and nothing is committed automatically.

```bat
:: 1. PLAN (read-only; writes only reports\direct_batches\<time>_<package>_direct_plan.{json,txt})
run_batch.bat plan-direct-batch --profile Default --package "HN66Fo4_EasyGirl_BS" --allow-direct-nif ^
  --select "38_EasyGirl_TankTop_MasonBlue_BS.nif" --select "40_EasyGirl_HotPants_Jeans_BS.nif" --details
:: 2. CONVERT TO WORKSPACE + VALIDATE (never commits). --plan refuses if anything changed since the inspected plan
run_batch.bat convert-direct-batch --profile Default --package "HN66Fo4_EasyGirl_BS" --allow-direct-nif ^
  --select ... --plan "reports\direct_batches\<plan>.json" --yes
:: 3. inspect work\direct_nif_batches\<id>\report.txt and meshes\*\logs\validation.txt
:: 4. COMMIT the VALIDATED meshes only
run_batch.bat commit-direct-batch --profile Default --batch <id> --yes
:: 5. VERIFY what MO2 serves (read-only) BEFORE starting the game
run_batch.bat verify-direct-build --profile Default
```

**Selection.** `--select` accepts a Direct-NIF ID (`dn-…`, shown in the plan and the `--details` report), a game-relative path (`meshes\HN66FO4\EasyGirlOutfit\X.nif`) or a file name that only one analyzed female mesh of the package has. An ambiguous or unknown selector is an error; two items that share one NIF convert it once. The file name only finds the record; sex and items come from the plugin analysis.

**Eligibility** (there is no override): CONVERT, HIGH, FEMALE_CBBE_COMPATIBLE, reason DIRECT_NIF_HIGH_CONFIDENCE; the package is the MO2 winner; no cloth data, no embedded body, not a body-slot (33) item, no shape-name clash; and a full Copy Bone Weights must add only the planned 3BBB bones. Anything else is REFUSED in the plan with its reason, and the plan cannot be converted until the selection is fixed.

**The plan** lists per mesh: ID, game path, in-game items, plugin, source provider, MO2 winner and competitors, SHA-256 and size, sex/body, action/confidence, every shape with vertices, regions, existing bones, planned 3BBB bones, the Copy Bone Weights radius, the full-copy prediction, untouched shapes, embedded body, cloth data, name collisions, the output path (and who owns an existing file there), warnings, and the body-slot layering note.

**Conversion** (`work\direct_nif_batches\<stamp>_<package>\`): the reference files are hash-checked and staged; for each mesh the current MO2 winner is resolved and hashed again (another winner → REVIEW `SOURCE_WINNER_CHANGED`; changed content → REVIEW `SOURCE_CHANGED`) and copied to `meshes\<nn>_<id>\source\`. One Outfit Studio session (interactive for 5.8.0–5.8.2: File > Open Automation, choose the script, Execute once, close) runs per mesh: ClearProject → LoadReference → ImportFile (the copy, absolute path) → CopyBoneWeights on the planned shapes → ExportFile to `meshes\<nn>_<id>\generated\<name>.nif` (absolute path, `WithRef=false`). There is no SaveProject.

**Post-export restoration (0.4.3).** Outfit Studio exports to `meshes\<nn>_<id>\generated_raw\<name>.nif`, which is kept as evidence. Interactive Outfit Studio 5.8.2 may smooth normals at duplicated seam vertices during the export; CBBE3BBBConverter treats this as an unwanted side effect. Steps:
- **Structural gate:** vertex N of the source must provably be vertex N of the export. The same shapes, counts, vertex layout, topology, segments, material, transforms, extra data and block types are required, and cloth is not allowed. Otherwise `POST_EXPORT_RESTORE_UNSAFE`.
- **Raw-diff policy:** only the skinning, and normal/tangent-basis changes on duplicated seam vertices, may differ from the source. Any other difference is `RAW_EXPORT_UNEXPECTED_CHANGE`, and nothing is restored.
- **Restore:** a temporary copy of the raw export gets every immutable vertex byte from the source. It is re-read and proven: immutable == source, skinning == raw export, nothing else changed. Then it becomes the final candidate `generated\<name>.nif`.

Only that final file is validated and committed; a refused or failed restoration leaves no candidate. The stage is shown per mesh: RAW_EXPORTED, RESTORED, or LEGACY_NO_RESTORATION for 0.4.1/0.4.2 workspaces.

**Validation** of each final generated mesh, independently of the others:

| Group | Checks |
| --- | --- |
| execution | log from this workspace's session; session finished normally; the generated NIF was written during the session; no step errors |
| round trip | the file exists at the planned absolute path; independent re-parse (reader + structure parser, no warnings) |
| source | source game mesh and workspace copy unchanged (SHA-256) |
| structure | same shapes in the same order (no reference leak, rename, missing or added shape); root name; vertex count and format; triangles; and, each on its own (0.4.2), vertex positions, UVs, second UVs, normals, tangents and bitangents, vertex colors, landscape/eye data and any byte no known field covers — only skin weights and bone indices may differ; transforms; BSSubIndexTriShape segment data; material, texture set, alpha; extra data; block types |
| cloth | no cloth data in source or output |
| converted shapes | no 3BBB bone outside the planned regions; region bones present; every predicted bone weighted; no vertex without weight; normalized; ≤ 4 influences; cross-side/quality anomalies; agreement ≥ 0.90; Copy Bone Weights prediction mean \|Δw\| < 0.005 |
| untouched shapes | weights unchanged after Outfit Studio's per-vertex normalization (0.3.3) |
| analyzer | the Direct-NIF analyzer classifies the output KEEP ALREADY_3BBB |
| restoration (0.4.3) | structural gate proven; raw export differs only by skinning and OS-regenerated normals/tangent basis on seam vertices; final immutable bytes == source and skinning == raw export (byte-exact) |

Only quality checks (quality/cross-side, agreement, prediction, region bones present, expected bones weighted, analyzer) can make a mesh REVIEW; every other failure is FAILED. `validate-direct-batch` repeats the checks from the workspace without Outfit Studio.

**Commit** writes VALIDATED meshes only, one transaction each: the generated file must still have its validated hash, the source winner must be unchanged, the target must be `<output mod>\<same game-relative path>`. The file is copied to a temporary name, verified, renamed, and read back (SHA-256). An existing file at the target is **never** overwritten; the message says whether it is the same accepted conversion, a stale/modified conversion, another generated conversion or a manual file. The manifest gets a `direct_conversions` record (source and generated SHA-256, reference files, items, shapes with planned/expected/actual bones and radius, settings, batch, acceptance state); the BodySlide `conversions` records are kept as they are.

**In-game test note (printed after the commit):** do not test converted clothes while wearing an unconverted body-slot shoe/boot of the package that brings its own legacy CBBE body (all 22 EasyGirl body-slot items); use the normal CBBE-3BBB body.

**verify-direct-build** (read-only) reports per committed mesh the game path, the output file, the winning provider, the winner/generated/source SHA-256, the in-game items, and expected vs actual 3BBB bones, and a status:

| Status | Meaning |
| --- | --- |
| DIRECT_OK | the output mod wins and serves exactly the committed, valid file |
| NOT_COMMITTED | no record / the file is not in the output mod |
| OUTPUT_NOT_WINNER | another provider wins the game path (move the output mod above the package) |
| OUTPUT_MISMATCH | the winning file is not the committed file |
| SOURCE_CHANGED | the package's source mesh changed since the conversion |
| GENERATED_INVALID | shapes or 3BBB weights are not as committed |

**Later analysis** of the package: KEEP ALREADY_CONVERTED_DIRECT_NIF when source, generated file and target reference are unchanged; otherwise REVIEW SOURCE_CHANGED, TARGET_REFERENCE_CHANGED, OUTPUT_CHANGED or OUTPUT_NOT_WINNER. Nothing is ever reconverted or overwritten automatically.

## 12. Mass conversion (0.5.0, Phase 3.2)

One package, converted in one operation. In the GUI this is the **Convert** button. On the CLI:

```bat
run_batch.bat convert-package --profile NAME --package "Clothes Pack 5 CBBE BodySlide" [--mode all-possible] [--max-per-session 25] --yes
run_batch.bat resume-batch    --profile NAME --batch <id> --commit --yes      :: after an interruption
run_batch.bat package-report  --profile NAME --batch <id> [--verify]
run_batch.bat verify-package-build --profile NAME --package "..."             :: after the BodySlide Batch Build
run_batch.bat export-diagnostics   --profile NAME [--batch <id>]
```

Internally the stages are the same as above: Analyze → plan → sessions of Outfit Studio → validation of every project → commit of validated projects, session by session (`app/pipeline.py`). The explicit stage commands (`plan-batch`, `convert-batch`, `validate-batch`, `commit-batch`) remain for development and automation, and they accept `--mode` and `--max-per-session`.

**Modes** (`conversion/outcomes.py`):

| Mode | Converts | Never |
| --- | --- | --- |
| SAFE (default) | CONVERT + NEW + HIGH confidence in every converted shape | MEDIUM/LOW, REVIEW, BLOCKED, SKIP, UNSUPPORTED, ERROR, already converted, embedded-body replacement |
| ALL_POSSIBLE | SAFE, plus CONVERT below HIGH, plus **REVIEW_ATTEMPTABLE** | **REVIEW_BLOCKED** |

- **REVIEW_ATTEMPTABLE:** an analyzer REVIEW at the conversion-candidate stage (garment-type risk, loose or uncertain fit, bridging) whose plan has no blocker.
- **REVIEW_BLOCKED:** no safe weight-transfer radius, an embedded body, existing/undeclared/partial 3BBB weights, a manual 3BBB variant of the same mesh, duplicate names, STALE / AUTO_CHANGED / UNKNOWN_AUTO earlier conversions, legacy CBBE physics weights, an unknown body family, or a missing reference.
- Validation is identical in both modes, and only VALIDATED results are committed.

**Sessions:**
- At most `max_projects_per_session` projects per session (default 25; profile `conversion.max_projects_per_session`), in the preview's order.
- Cloth-data projects go last, one per session.
- Each session is validated as soon as it ends, and its validated projects are installed before the next session starts.

**Checkpoints:** `batch.json` (schema 2) is saved after every change. Each project goes PENDING → ATTEMPTING → GENERATED → VALIDATED / REVIEW / FAILED → COMMITTED (`attempts`, `origin` SAFE / MEDIUM / REVIEW). Batches from 0.3/0.4 (`PLANNED`) are read as PENDING.

**Failure classes:**

| Class | Examples | Behaviour |
| --- | --- | --- |
| PROJECT_FAILURE | a validation failure, REVIEW, no safe strategy | only that project's status changes; the batch continues |
| SESSION_FAILURE | Outfit Studio crashed or timed out, window closed without Execute, automation exit code | every project saved before it is validated on its own and kept; the session's unfinished projects go back to PENDING and run once more in a fresh session (`session_retries`, default 1) |
| SYSTEM_FAILURE | a source or reference file changed, modlist.txt changed, the output location or workspace became unusable, Outfit Studio not usable | the batch stops in `SYSTEM_ERROR`; finished results are kept; `resume-batch` checks again before continuing |

**Resume:** `resume-batch` (GUI: *Resume*) does the following:
1. Validates projects that were saved before the tool stopped (GENERATED).
2. Returns unfinished ones to PENDING.
3. Continues. VALIDATED, REVIEW, FAILED and COMMITTED projects are never run again. A later run of the package sees committed projects as KEEP / ALREADY_CONVERTED.

**Weight sums.** Outfit Studio (nifly `SetShapeVertWeights`) normalizes a vertex's weights over all its bones and then stores the 4 largest. A vertex that received more than 4 bones is therefore saved with a sum below 1, and BodySlide builds keep that sum. The `weights normalized` check (tolerance 0.05, unchanged) now makes such a project REVIEW / "could not be stored completely"; vertices without any weight stay FAILED.

**0.5.1: skin-weight renormalization.** The weight-sum deficit above is repaired after each session and before validation, strictly:
- Rule: `w_i / sum` of the stored 1–4 weights.
- Scope: only vertices with exactly 4 non-zero weights and a sum in [0.5, 1 − 0.002), only in converted shapes.
- Unchanged: bone indices and every other byte (proven on the re-read file).
- Refused: every other anomaly — zero / negative / NaN / Inf, a sum above 1, a deficit with fewer than 4 influences, a sum below 0.5.
- Records: the raw Outfit Studio file is kept (`generated_raw/`), and the repair appears in `renormalization.json`, the result and the manifest (`skin_weight_renormalization`).
- The full validation then runs unchanged on the repaired file.

**MO2:** the preview reports `output_mod_state` (ENABLED / DISABLED_IN_MO2 / NOT_IN_PROFILE / NOT_CREATED) and the codes `OUTPUT_MOD_DISABLED_IN_MO2` / `OUTPUT_MOD_NOT_IN_PROFILE`. The profile is never edited.

**Results** (`app/pipeline.py`, `app/messages.py`):
- Each project has an outcome: CONVERTED, ALREADY_COMPATIBLE, NO_CONVERSION_NEEDED, COULD_NOT_CONVERT_SAFELY, FAILED, SKIPPED (not tried in this mode) or UNFINISHED.
- The package result is COMPLETE, COMPLETE_WITH_WARNINGS, INTERRUPTED or SYSTEM_ERROR.
- Every problem carries a display name (in-game item name → project → mesh), a short human message and the technical reasons. When the package has no plugin, item names come from the plugins of enabled mods.

**Needs attention (0.5.2):**
- The result's `completed` list holds CONVERTED or ALREADY_COMPATIBLE outfits with no in-game problem report.
- `needs_attention` holds every other outfit that should have physics but does not: COULD_NOT_CONVERT_SAFELY, FAILED, SKIPPED, UNFINISHED, or a reported in-game problem. Each has a short reason (`app/messages.py` `SHORT`).
- An earlier batch's REVIEW / FAILED attempt of the same project stays visible in later views.
- In-game reports (`report-runtime-issue`, stored as manifest `acceptance` FAIL / PARTIAL) never change the automated validation result.

**Package report** (`reports\packages\*_package_report.{txt,json}`) contains:
- the buckets SAFE_AUTO_DONE, MEDIUM_NOT_AUTO, REVIEW_COMPLEX_GEOMETRY, BLOCKED_WEIGHT_TRANSFER, EXISTING_3BBB_AMBIGUOUS, NO_PHYSICS_NEEDED and FAILED_TECHNICAL (plus ALREADY_COMPATIBLE and UNSUPPORTED_BODY);
- the BodySlide build plan: output mod state, **Build only** the package group, preset CBBE Curvy, Build Morphs ON, and the warning not to batch-build the original projects after the AUTO group (0.5.3);
- 5–8 representative items to test in game;
- optionally the `verify-package-build` aggregate: build status per committed project and whether both SliderGroups exist and list every committed project.

**Diagnostic report:** one ZIP in `reports\diagnostics`. It holds version, settings, classification, batch state and sessions, and for every problem project its result, validation and Outfit Studio logs, plus the end of the tool log. Paths are anonymised; no mod files are included.


## 13. Unattended Outfit Studio (0.5.3)

The user presses **Convert** once; Outfit Studio may open and visibly work, but nobody has to touch it (`conversion/os_control.py`, Windows side `conversion/win32_ui.py`). Profile `conversion.os_control`: `auto` (unattended where the converter can drive Outfit Studio's window, i.e. Windows; otherwise manual), `unattended`, or `manual`.

**Why the GUI stays:** Outfit Studio 5.8.0–5.8.2 headless (`-a`) never initializes the 3D view, so CopyBoneWeights silently does nothing (§7, `UPSTREAM_ISSUE.md`). The accepted CP5 / Wardrobe 10 results come from the windowed Automation dialog; 0.5.3 keeps exactly that path and only takes over the clicks.

**One session, step by step** (all state-driven; timeouts only as guards):

| Step | What is checked / done |
| --- | --- |
| 1. Script provenance | exact name `cbbe3bbb_batch_<batch>_<session>`; SHA-256 equals the hash recorded when the batch wrote it (`sess.script_sha256`); batch invariants; one part per planned project, in order, each saving that project's generated project; the planned step count. Otherwise nothing runs (`AUTOMATION_FAILED`, not started). |
| 2. Preselection | `AutomationDialog.lastScript` in the private runtime's `OutfitStudio.xml` (Outfit Studio restores it when the Automation window opens). |
| 3. Launch + readiness | `Log_OS.txt` (deleted before the start) shows "Outfit Studio initialized." and "OpenGL Context Info:", the main window exists, the process is idle, no startup message. |
| 4. Open + verify | menu command "Open Automation…" (native menu ID read from the menu); the Automation window must show the exact script name and the planned number of steps, one enabled Execute; hash checked again. If the box shows another script, the exact name is selected by text and verified again. |
| 5. Execute | Execute → *Confirm Execution* → Yes. |
| 6. Monitor | the output pane's `Automation: Step i/N` lines (current step → current project), saved outputs, dialogs. |
| 7. Completion | Outfit Studio's completion message + status bar "Automation complete." + last step N/N. |
| 8. Close | Automation window, main window, *Unsaved Changes* → No; forced only if it does not exit. |
| 9. Validate | every project on its own, unchanged rules; only VALIDATED results are committed. |

**Native message boxes** (wxMessageBox = Windows task dialogs) are identified by exact title + exact set of button texts (their button windows all have control ID 0) and answered with the task-dialog ID wxWidgets assigns (Yes = IDYES, No = IDNO, an OK-only box = IDCANCEL); a box that does not close afterwards stops the session (`os_control.KNOWN_BOXES`).

**Dialogs:** only known ones are answered (Confirm Execution, Automation Error → Yes/continue, Reference/NIF Error and Deleted Shapes → OK and recorded, the completion message, Unsaved Changes → No, Choose cloth data → see below). Anything else is captured into `session_result.json` (title, controls, list items; a screenshot in `logs/os_control/` when Pillow is installed) and the session stops (`USER_INTERACTION_REQUIRED`).

**Choose cloth data:** A — one origin, the saving project's own staged ShapeData NIF, SHA-256 equal to the source, the planned block count → kept; B — several origins with byte-identical payloads → only the project's own; C — different origins → Cancel (nothing written), the project fails with *Multiple cloth physics data sources require review.*, the session goes on; D — anything unexpected → stop and isolate. Every save is re-read and must carry exactly the source's cloth payloads; the manifest records `cloth_extra_data`.

**Isolation and resume:**
- the project that was running when a session crashed, timed out or showed an unknown dialog runs **alone** next (`isolate`), then fails on its own after `session_retries`;
- projects whose first step never ran go back to PENDING without using an attempt;
- a failure without an identifiable project halves the next sessions (`split_limit`);
- a control failure before anything ran (window/script could not be verified) is retried once in a fresh Outfit Studio; the second in a row asks *Retry automatically / Continue manually / Stop for now* (CLI: stop and print `resume-batch … --manual` / plain `resume-batch`);
- **Continue manually** runs the next session the old way (the user presses Execute), then unattended again.

**Probe:** `run_batch.bat probe-outfit-studio --profile NAME` starts Outfit Studio, opens the Automation window with a one-step probe script, verifies it and closes it without executing anything (`reports/os_probe_*.json`).

**Diagnostics:** `logs/session_result.session<N>.json`, `logs/automation_output.session<N>.txt`, `logs/Log_OS.session<N>.txt`; `export-diagnostics` includes them (screenshots stay local).

**BodySlide:** build only the package group `CBBE 3BBB Auto Conversions - <package>`. The generated AUTO projects intentionally build the same game mesh files as the originals; if BodySlide sees both in one build it asks "Choose output set", and building the originals afterwards replaces the 3BBB meshes (`verify-package-build` → BUILD_STALE, explained in plain words). BodySlide 5.8.2 can group-build from the command line (`--groupbuild "<group>" --preset "CBBE Curvy" --trimorphs`, only the group's members, conflicts only inside the group), but where it writes depends on how BodySlide is run through MO2, so 0.5.3 does not start it; the handoff screen says exactly what to build.
