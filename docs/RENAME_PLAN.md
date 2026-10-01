# Rename plan: CBBE3BBBConverter → "Fallout 4 Body & Physics Converter"

Not done in 0.5.3 (a wholesale rename in the middle of a production phase is not trivial). This is the plan, so no new
"CBBE3BBB only" names are added meanwhile.

## What changes, what stays

| Area | Today | After the rename | Compatibility |
| --- | --- | --- | --- |
| Product / window title | "CBBE → 3BBB Converter" | "Fallout 4 Body & Physics Converter" | — |
| Python package | `cbbe3bbb` | `f4bpc` (new) | keep `cbbe3bbb` as a thin alias package re-exporting `f4bpc` for at least one release; `python -m cbbe3bbb` keeps working |
| CLI | `python -m cbbe3bbb …`, `run_*.bat` | `python -m f4bpc …`, same `run_*.bat` names | the .bat files call the new module; old module name is an alias |
| Tool folder | `T:\Fallout 4 Tools\CBBE3BBBConverter` | unchanged (user-chosen path) | the tool never hard-codes its folder |
| Output mod | `CBBE 3BBB Auto Conversions` | per route: `<Target body> Auto Conversions` (CBBE 3BBB stays `CBBE 3BBB Auto Conversions`) | existing output mods and their names are never renamed automatically |
| Generated project suffix | ` - 3BBB AUTO` | per route (CBBE 3BBB keeps ` - 3BBB AUTO`) | existing generated projects keep their names (they are BodySlide projects the user has built) |
| BodySlide groups | `CBBE 3BBB Auto Conversions`, `… - <package>` | per route, same pattern | existing group files are kept |
| Automation scripts | `cbbe3bbb_batch_<batch>_<n>`, `cbbe3bbb_<…>` | `f4bpc_batch_<batch>_<n>` | the session controller verifies the name the batch recorded (`sess.script`), so old batches resume with their own names |
| Runtime marker | `runtime/…/cbbe3bbb-runtime.json` | read both, write the new name | `detect_runtime` accepts either |
| Temp/commit suffix | `.cbbe3bbb-tmp` | `.f4bpc-tmp` | cleanup recognises both |
| Diagnostics | `cbbe3bbb_diagnostics_<stamp>.zip`, `about.json` `tool: CBBE3BBBConverter` | new names | — |
| Manifest | `manifest.json` records: `tool_version`, `source`, `target`, `validation`, `acceptance`, … | unchanged keys; new optional `route` (0.5.3), `tool` | every reader tolerates missing keys (0.5.3 already writes `route`); records are never rewritten by a rename |
| batch.json | schema 2 (+ 0.5.3 optional `route`, `control`, `result`, `script_sha256`) | unchanged | readers use `.get` for every 0.5.x field |
| Profiles | `config/profiles.json` (`conversion.output_mod_name`, `project_suffix`, …) | route-specific settings nested under the route | missing route settings = today's values |

## Route split (started in 0.5.3)

`conversion/routes.py` names `source_body_family`, `target_body_family`, `physics_profile`,
`geometry_conversion_strategy`, `weight_transfer_strategy`. Only `cbbe-to-cbbe3bbb` is implemented; the next phase adds
`fusiongirl-to-cbbe3bbb` (Fusion Girl → CBBE geometry → CBBE 3BBB physics). Module names that will need neutral names:
`cbbe3bbb.conversion.body` (CBBE body identity), `analysis.reference` (the CBBE 3BBB reference) — both stay, new routes
add their own modules.

## Order

1. Add `f4bpc` as the real package and `cbbe3bbb` as the alias; run the full test suite through both names.
2. Switch window title, diagnostics and new script names; keep verifying old names where a batch recorded them.
3. Route-specific output mod / suffix / group settings (defaults = today's for CBBE → CBBE 3BBB).
4. Only then rename documentation and the repository.

## Status 0.6.0

Done (non-destructive): the user-facing product name **Fallout 4 Body & Physics Converter** (GUI titles, README
heading, report headers, `cbbe3bbb.PRODUCT_NAME`). Unchanged by design: folder, `run_batch.bat`, package `cbbe3bbb`,
`TOOL_ID` "CBBE3BBBConverter" in manifests/settings, output mod and group names, profile names. A migration of
those is only worth doing with a concrete benefit and a tested migration path.

## Status 0.6.1

The application folder is renamed to `T:\Fallout 4 Tools\Fallout 4 Body & Physics Converter` (a plain folder rename;
no junction). Code, launchers and tests are folder-relative; profiles remember `tool_root` and migrate paths that
pointed inside the former folder. Unchanged on purpose: package `cbbe3bbb`, `run_batch.bat`, manifest `tool`
"CBBE3BBBConverter", output mod "CBBE 3BBB Auto Conversions", groups, profile names, `CBBE3BBB_HOME` /
`CBBE3BBB_CONFIG_DIR` environment variables.
