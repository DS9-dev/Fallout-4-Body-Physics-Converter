# Architecture

```
            ┌────────────── GUI (PySide6) ─────────────┐   ┌──── CLI ────┐
            │ gui/main_window.py  (QThread worker)     │   │ cli.py      │
            └───────────────┬──────────────────────────┘   └─────┬───────┘
                            └──────────── app/controller.py ─────┘
                                   │  profiles, safety check, cache, reports
      ┌────────────────────────────┼────────────────────────────────┐
 profiles/store.py         discovery/                          reports/writer.py
 (config/profiles.json)    install.py ─ mo2.py ─ vfs.py        (json/csv/html)
                                   │
                         analysis/analyzer.py  (thread pool, cache, cross-project)
                ┌──────────────────┼──────────────────────┬─────────────────────┐
         bodyslide/osp.py    nif/reader.py      analysis/reference.py    classification/
         bodyslide/xmlfiles  (read-only NIF)    (knowledge base, OS      family.py
                                                weight-copy emulation)   rules.py
                                                analysis/shape_metrics.py
```

## Layers

- **`discovery`**. `mo2.py` parses a portable `ModOrganizer.ini` and `profiles/<p>/modlist.txt` (the first line has the highest priority). `vfs.py` builds a read-only *virtual file system* for the `Tools/BodySlide` subtree: each `DataProvider` (game Data, enabled mods by priority, overwrite, optional extra roots, optional disabled mods ranked last) is walked once, and every relative path keeps all versions, highest priority first. `install.py` combines game, MO2, BodySlide exe versions, `Config.xml`, `BodySlide.xml`, RefTemplates, presets and groups into an `Installation`, and picks the CBBE 3BBB reference template.
- **`bodyslide`**. Tolerant parsers for OSP/XML project files and BodySlide config files. They never write.
- **`nif`**. A minimal NIF 20.2.0.7 reader: header, NiNode hierarchy, `BSTriShape` family geometry and skin, `BSSkin::Instance`/`BoneData`. Unknown blocks are skipped using the block-size table.
- **`analysis`**
  - `reference.py` builds the `KnowledgeBase` from the *installed* files: vanilla skeleton bones, installed body signatures (for recognising embedded bodies), the CBBE slider vocabulary, and the reference body. Physics bones are the reference bones outside the skeleton that carry weight, grouped into BREAST/BUTT/THIGH regions with a measured side sign. `ReferenceBody.predict_weights` emulates Outfit Studio's Copy Bone Weights.
  - `shape_metrics.py` computes, per shape: body detection, existing physics/legacy/custom bones, distance to the reference, and per-region metrics (relevance, fit, coverage, existing weights, quality signals).
  - `analyzer.py` orchestrates: it enumerates winning project files, parses sets, resolves ShapeData through the VFS, analyzes sets in a thread pool (per-run NIF cache, sqlite cache keyed by a fingerprint), then runs cross-project checks (same output mesh, superseded originals, duplicate set names) and classifies.
  - `models.py` holds dataclasses that serialize to/from JSON (the report and cache format).
- **`classification`**. `family.py` detects the body family (CBBE/TWB/BodyTalk/Fusion Girl/Male) and the physics type (3bbb/legacy/standard/none) from independent signals. `rules.py` turns metrics into `Action` + reasons (see `CLASSIFICATION.md`).
- **`reports`**. JSON (full detail, round-trips into `ScanReport`), CSV (one row per project) and a self-contained HTML page with filters, search, sorting and details.
- **`app`**. `controller.py` holds the Qt-free application logic used by both front ends, and `logsetup.py` sets up rotating `logs/app.log` with redaction of user-profile paths.
- **`gui`**. A thin PySide6 window. Long tasks run in a `QThread` worker, and progress/cancel go through the controller.

## Guarantees

- **Read-only.** No module opens game or mod files for writing. Writes happen only in `config/`, `reports/`, `logs/` and `cache/` of the tool folder, and `AppController.check_output_location` refuses to run when that folder is inside the game, MO2 or mods directory. Tests hash the whole fixture installation before and after analysis.
- **Tolerant.** A malformed OSP, a missing NIF or a crashing shape becomes an `ERROR` row or a warning. The scan continues.
- **Responsive and cancellable.** Progress callbacks and a cancel flag are checked between files and projects, and a cancelled scan returns partial results flagged `cancelled`.
- **Cached.** Repeat scans reuse per-project results when the OSP/NIF size and mtime, the providers, the reference and the settings are unchanged. Classification always re-runs, because it depends on the whole library.
- **No hard-coded paths.** Everything comes from the profile. Environment overrides: `CBBE3BBB_HOME` (tool folder) and `CBBE3BBB_CONFIG_DIR`.

## Extending

- A new body family means a signature in `family.py`, plus its reference template, which is discovered automatically from `RefTemplates`.
- The `conversion` package sits behind `AppController`. It consumes `ProjectResult`s with `action == CONVERT` and the stored `ConversionSettings`:
  - `plan.py` builds the per-shape plan (the Dry Run).
  - `automation.py` generates the Outfit Studio scripts and enforces their invariants (single and batch).
  - `runtime.py` handles the private Outfit Studio copy.
  - `engine.py` runs one project (stage → run → validate → commit).
  - `evidence.py` proves execution.
  - `validate.py` runs the checks.
  - `manifest.py` writes the output mod's record.
  - Phase 3 adds `package.py` (package analysis, Batch Preview, reports) and `batch.py` (batch workspace, Outfit Studio sessions, per-project validation, commit, recovery). GUI: `gui/package_dialog.py`. See `CONVERSION.md` and `BATCH.md`.

## 0.5.0 additions (Phase 3.2)

- `conversion/outcomes.py`: selection modes, REVIEW policy (attemptable / blocked), outcomes, report buckets, package result, display names.
- `conversion/batch.py`: bounded sessions, per-project checkpoints, session/system failure handling, `resume`, the session hook the pipeline uses for per-session commits.
- `app/pipeline.py`: the one-click package operation (events with levels NORMAL / DETAILED / DEBUG, `PipelineResult`).
- `conversion/os_control.py` (0.5.3): the unattended Outfit Studio session — script provenance, preselection, readiness, the known dialogs, cloth-data decisions, completion handshake (`session_result.json`), graceful close. Platform access goes through a `UIDriver`; `conversion/win32_ui.py` is the Windows one (Win32 window messages to Outfit Studio's native controls). `BatchEngine._drive` runs it per session and isolates the project a failed session was running.
- `conversion/routes.py` (0.5.3): conversion routes (source/target body family, physics profile, geometry and weight-transfer strategy); only CBBE → CBBE 3BBB is implemented.
- `app/messages.py`: human-readable messages.
- `app/report.py`: the package report.
- `app/diagnostics.py`: the anonymised diagnostic ZIP.
- `gui/simple_window.py`: the Normal-mode window. The engineering window (`gui/main_window.py`) is Settings / Advanced.


## 0.6.0 Body Architecture Foundation

Product name: **Fallout 4 Body & Physics Converter** (`cbbe3bbb.PRODUCT_NAME`); serialised identities unchanged
(`TOOL_ID = "CBBE3BBBConverter"`, output mod, groups, package `cbbe3bbb`).

```
analyzer FamilyResult ──► bodies.detect_body() ──► BodyDetection {body id, confidence, evidence, conflicting}
                                                         │
requested Source Body (auto | cbbe | fusion_girl | twb) ─┤  routes.decide_route()
requested Target Body (cbbe_3bbb)                        ▼
                         RouteDecision {route, status, reason}  ── package.apply_routing(): gate per project
                                │
          ConversionRoute = geometry stage + physics stage + validator + validation stages
          CBBE_TO_CBBE_3BBB: geometry NOT_REQUIRED, physics PRODUCTION (the 0.5.x backend), cbbe_to_cbbe_3bbb_strict
          FUSION_GIRL_TO_CBBE_3BBB: geometry NOT_IMPLEMENTED → never executable
```

- `conversion/bodies.py`: body ids (`unknown`, `cbbe`, `cbbe_3bbb`, `fusion_girl`, `twb`, `twb_3bbb`,
  `bodytalk_male`, `unknown_3bbb`), labels, `normalize_body_id`, `detect_body` (formalises `family.py`).
- `conversion/routes.py`: `Stage`, `ConversionRoute` (route_id, statuses, stages, validator, limitations; 0.5.3
  `id`/`to_dict()` keys kept), registry `ROUTES`, `route_for`, `resolve_request` (GUI/CLI request check),
  `decide_route` (an explicit source only confirms the detection), `resolve_route` (0.5.3 API).
- `package.apply_routing(prev, source, target)`: fills the 0.6.0 preview fields and demotes a CONVERT/attemptable
  project whose route cannot run to REVIEW (`ROUTE_NOT_EXECUTABLE` / `SOURCE_OVERRIDE_CONFLICT` /
  `SOURCE_UNVERIFIED`). With Auto Detect → CBBE 3BBB it changes no 0.5.x classification (tested).
- Plans carry `route`; batch `route` + manifest record `route` keep the 0.5.3 keys and add `route_id`, `source_body`,
  `target_body`, `route_status`, `geometry_stage`, `physics_stage`, `validator`; records add `body_detection`.
  Readers tolerate their absence (old previews infer the body from the family label only).
- Defence in depth: `batch.py` refuses to stage a plan whose route status is not PRODUCTION/LEGACY_SUPPORTED;
  `engine._validate` adds a failing required check for any validator other than `cbbe_to_cbbe_3bbb_strict`.
- Not routed yet: the Direct-NIF path (`direct_nif.py`, `direct_batch.py`).
