# Phase 0 research

This file covers what was found on the development installation and upstream, and what that means for the tool. Everything below was read without modifying anything. Paths refer to the development profile only; the tool itself never hard-codes them.

## 1. Development installation (profile "dev")

| Item | Finding |
| --- | --- |
| Game | `T:\Fallout 4` (taken from `ModOrganizer.ini` → `gamePath` when not set explicitly) |
| Mod manager | MO2 **2.5.2**, portable instance `T:\MO_Main _ baseCBBE`, selected profile **"Fix F"** |
| Mods | 171 entries (158 enabled). 32 mods ship `Tools\BodySlide\SliderSets` with **994 project files** between them |
| BodySlide | mod **"BodySlide and Outfit Studio -"** → `Tools\BodySlide\BodySlide x64.exe` **v5.7.1.0** (read from the PE version resource). `Config.xml` has target game 3 (Fallout 4), and `BodySlide.xml` selects preset "CBBE Curvy" |
| Skeleton | `Tools\BodySlide\res\skeleton_fo4.nif`, 129 nodes |
| CBBE | mod "Caliente's Beautiful Bodies Enhancer -CBBE-" (`RefTemplates\CBBE.xml`, CBBE body + vanilla/DLC outfit conversions) |
| CBBE 3BBB reference | mod "Caliente's Beautiful Bodies Enhancer (CBBE) 3BBB Body": `RefTemplates\CBBE 3BBB.xml` → template **"CBBE 3BBB Body"** = `SliderSets\CBBE 3BBB Body.osp`, set "CBBE 3BBB Body", shape **`CBBE-3BBB`** (22 708 vertices, 336 sliders). Also "CBBE Reduced 3BBB Body" (shape `BaseFemaleBody:0`). Note: the shape entry is `<Shape target="CBBE" DataFolder="CBBE">`, where the `DataFolder` attribute only relocates slider data; the geometry comes from `ShapeData\CBBE 3BBB Body\CBBE 3BBB Body.nif` |
| BodySlide folder case | mods use both `Tools\BodySlide` and `Tools\Bodyslide`, so all lookups are case-insensitive |

### 3BBB bone structure (from the installed reference, not assumed)

Physics bones are the bones of the reference that are **not in the vanilla skeleton** and carry weight:

| Region | Bones |
| --- | --- |
| Breast | `LBreast_01_skin`, `LBreast_02_skin`, `LBreast_03_skin`, `RBreast_01_skin`, `RBreast_02_skin`, `RBreast_03_skin` |
| Butt | `LButt_01_skin`, `RButt_01_skin` |
| Thigh | `LLeg_Thigh_01_F_skin`, `LLeg_Thigh_01_R_skin`, `RLeg_Thigh_01_F_skin`, `RLeg_Thigh_01_R_skin` |

Side convention: `L*` bones weight vertices with negative X, `R*` positive X (measured per installation, `side_sign`). The 3BBB body does not use the vanilla `LBreast_skin`/`LButtFat_skin` bones that plain CBBE outfits carry. The older "CBBE Physics" setups use `CLOTH_Bone_Googles_00/01`-style cloth bones; these are detected as `legacy_cbbe_physics`.

### Library composition (sample of 26 mods, 309 projects analyzed in the sandbox)

Projects came from CBBE (vanilla/DLC conversions), CBBE 3BBB conversions (ASA 3BBB, Vtaw BlackMamba 3BBB, Vtaw Wardrobe 8 TWB-3B, TWB Vanilla 3BBB), TWB-based sets, BodyTalk (male) sets and plain CBBE outfit packs (Clothes Pack 4/5, Vtaw Wardrobe 5, Devious Devices, Raider Punk…). See the report summary in the Phase 1 hand-over and `reports/`.

### Findings that need the user's attention (not decided by the tool)

1. **Raider Punk Top V2.** `SliderSets\...Raider Punk...osp` in the source mod now contains **two** sets: the original "MM69 Raider Punk Top V2 - CBBE" and "MM69 Raider Punk Top V2 - 3BBB TEST". Both build `meshes\MM69 Raider Punk\raiderPunk_top_v2`. The TEST set lists the `CBBE-3BBB` shape, and its ShapeData sits in MO2 **overwrite**. The built `raiderPunk_top_v2.nif` (about 1.05 MB, inside the Raider Punk mod) contains the full CBBE-3BBB body, so *"Copy reference shape into output"* was effectively **on**, contrary to the specification. The analyzer reports the original set as `CONVERT` (with a same-file-variant warning) and the TEST set as `KEEP`. Which one should build the mesh is the user's call.
2. **CP5 jacket v3.** The OSP and NIF in "Clothes Pack 5 CBBE BodySlide" were modified in place (3BBB weights plus an embedded body). That mod is **disabled** in profile "Fix F", so BodySlide does not see it unless the mod is enabled. The analyzer reports it as `REVIEW` (undeclared 3BBB edit on outerwear) and needs "Include mods disabled" to see it.
3. **ASA.** "0. ASA 0.2 CBBE PHYSICS" is completely overridden by "1. ASA 0.2 - 3BBB (CBBE) Conversion": every project file of the former loses the conflict, so only the 3BBB versions are analyzed (all `KEEP`).
4. **Tactical Bikini** references a missing `Tactical Bikini.nif` (a real `ERROR`). **HN66 EasyGirl** ships no BodySlide files.

## 2. BodySlide / Outfit Studio behaviour and automation

Source: [ousnius/BodySlide-and-Outfit-Studio](https://github.com/ousnius/BodySlide-and-Outfit-Studio) (tags v5.7.0 to v5.8.2), which uses [nifly](https://github.com/ousnius/nifly) for NIF I/O.

**Weight copy (Outfit Studio "Copy Bone Weights").** For each outfit vertex, Outfit Studio takes reference vertices within the *Search Radius*, sorted by distance and limited to *Max Vertex Targets* (unless *No Target Limit* is set). If the nearest one has no weight for a bone, the result for that bone is 0. Otherwise it is the inverse-distance-weighted mean over the neighbours that carry the bone (a distance of 0 counts as weight 1000). Weights are normalized afterwards. The analyzer re-implements this read-only in numpy/scipy to *predict* what a conversion would produce. On real converted outfits the prediction agrees with Outfit Studio's result at 0.999 (Vtaw Gym Top, CP5 jacket) and 0.978 (ASA).

**Skin space.** Fallout 4 skinned `BSTriShape` vertices are in skin space; skin→global = `world(bone) @ BoneData(bone)`. The NiAVObject transform is ignored for skinned shapes. Getting this wrong puts outfits 100+ units away from the body.

**Command-line options (installed v5.7.1).** BodySlide: `--groupbuild <groups>`, `-b/--build`, `-f` (targets), `--targetdir`, `-p <preset>`, `--trimorphs`. These rebuild projects; they cannot edit weights. Outfit Studio 5.7.1 has no scripting.

**Headless automation (v5.8.0+, not installed).** Outfit Studio 5.8 adds automation scripts and a headless mode: `OutfitStudio x64.exe -a <script name> [files…]` runs a script without showing the window and returns its exit code. Relevant step types include `LoadReference`, `AddProject`, `CopyBoneWeights` (`ProximityRadius`, `MaxResults`, bone list), `SaveProject` (`AutoCopyRef` **defaults to true**, `CopyRefFromProject`, `CopyRefShapeName`, `GenWeights`), `SetReferenceShape`, `DeleteBones`, `MaskWeighted`, `ConformSliders` and `LogMessage`. This is the recommended Phase 2 engine because it is the real Outfit Studio code path, not a re-implementation. It **requires upgrading** BodySlide/Outfit Studio to 5.8.x, and scripts must set `AutoCopyRef=false` explicitly.

**Not used.** GUI mouse automation of Outfit Studio is too fragile and is not the core engine. Writing NIFs from Python (a nifly re-implementation) is possible, but it would duplicate Outfit Studio's tangent, partition and segment handling. That is a `REFERENCE_GAP`: Phase 1 only *reads* NIFs.

### REFERENCE_GAP notes

- `.osd` slider data and `.tri` morph files are not parsed. Phase 1 only needs slider names and targets from the OSP.
- Outfit Studio's weight copy has extra options (per-bone masks, "copy only unweighted") that the prediction does not model. The prediction uses the plain radius/targets algorithm.
- `BSSubIndexTriShape` segment data and `BSClothExtraData` are skipped (block sizes allow safe skipping).
- Non-portable MO2 instances (AppData) are not auto-detected: set the folders manually.

## 3. Risks

| Risk | Mitigation in Phase 1 |
| --- | --- |
| "3BBB bones present" ≠ good weights | quality checks: L/R balance vs prediction, cross-side weights, weights outside the reference region, agreement with a reference transfer, weight islands |
| Faithful weight copy still behaves badly (CP5 jacket) | garment-type rules: outerwear, loose, rigid, accessory, footwear, layered → `REVIEW`; undeclared in-place 3BBB edits → `REVIEW` |
| Copy-reference-shape left on (Raider Punk) | embedded body shapes detected (`includes_body_shape`), same-output and same-file variants reported |
| Disabled/overridden mods confuse the picture | real MO2 priority resolution; disabled mods optional and ranked last; `osp_also_provided_by` lists the losers |
| Writing into user data by accident | no write code paths outside the tool folder; the tool refuses to run from inside game/MO2/mods; tests hash the fixture tree before and after |

## 4. Licensing

- BodySlide and Outfit Studio: **GPL-3.0**. nifly: **GPL-3.0**. niftools nif.xml: **BSD-3-Clause**. No code from these projects is copied. The NIF reader is an independent implementation from the public format description, and the weight-copy prediction re-implements the documented behaviour.
- PySide6 (Qt for Python): **LGPL-3.0**, used as an unmodified dynamic dependency. numpy/scipy: BSD.
- Game and mod assets (CBBE, 3BBB, Vtaw, ASA, Raider Punk, …) belong to their authors. None of them are included in this repository. Phase 2 outputs must stay in the user's own output mod and must not be redistributed.

## 5. Phase 2 findings

- **Manual Top V2 TEST = full Copy Bone Weights.** Its weights match an emulated copy of *all* reference bones (radius 5, 4 targets) with a mean difference of 0.0000. A physics-bones-only copy differs by up to 0.2. On this top the full copy adds only the 6 breast bones.
- **Top V1** is `MM69 Raider Punk Top - CBBE` (`SliderSets\MM69 Raider Punk Top - CBBE.osp`, one shape `Model001_25_upper_1_0_0` with 1 356 vertices, 71 sliders, output `meshes\MM69 Raider Punk\raiderPunk_top`, GenWeights false). The currently built `raiderPunk_top.nif` (60 642 bytes) sits in the Raider Punk mod. Earlier BodySlide builds wrote into that mod's `meshes` folder.
- **Outfit Studio 5.8.2 headless automation**: scripts live in `<ProjectPath>\Automations\<name>.xml`. `ProjectPath` is used only when the program folder has no `SliderSets` folder, and the release zip ships an empty one, which is why the tool runs a private copy without it. Exit codes: 0 ok, 10 a step failed, 1–6 script problems. Error dialogs can still block, so the tool uses a timeout and watches the log for fatal errors.
- **Bug:** headless CopyBoneWeights is a no-op (see `UPSTREAM_ISSUE.md`).
- **Slider data after AddProject**: the saved project references the source's `ShapeData\<folder>\<name>.osd` via the Shape `DataFolder` attribute instead of copying it. BodySlide 5.7.1 supports that attribute and reads the file through MO2 from the unchanged source mod.
- **Interactive automation does not log to `Log_OS.txt`** (5.8.2, Windows run 2026-09-27). `AutomationDialog::StartProgress` makes a `wxLogTextCtrl` on the dialog's output pane the active log target until `EndProgress`. Step messages therefore appear only in the Automation window. `Log_OS.txt` shows only the session: `Project path in config: …`, "frame loaded", "frame closed". The tool now proves execution with artifacts (see `CONVERSION.md`, "execution evidence"). `SaveProject` writes `<ReferenceInfo ProjectFile=… ProjectName=… ShapeName=…>` for the loaded reference.

## 6. Phase 3 findings (Outfit Studio 5.8.2 source + sandbox runs on read-only copies)

- **Multi-project scripts work.** `AutomationDialog::ExecuteSteps` runs any list of steps in order. `ClearProject` calls `ResetAndClearProject` (reference, outfit, anim, work NIF cleared). A 3-project script (Top V1, Shorts, Shorts V2) run by the patched sandbox build produced project files and NIFs byte-identical to one session per project. The Top V1 NIF from the batch has weights and geometry identical to the in-game accepted Windows result (one float in a bone transform differs in the last bit, between Windows and Linux builds).
- **Cloth physics data persists across projects.** `OutfitProject::clothData` (from every loaded NIF with `BSClothExtraData`) is not cleared by ClearProject/ResetProject. Every later SaveProject shows a modal "Choose cloth data" dialog listing all of them: reproduced with Shorts → Shorts V2 → Top V1, where Top V1's save offered both shorts' cloth data. The dialog also blocks headless runs. Raider Punk Shorts, Shorts V2, Bracelets, Heels and two BT3 NIFs contain cloth data; Top V1 does not. Handled by session ordering (cloth project last, one per session), a validation check, and a headless blocker (`BATCH.md` §3).
- **Slider data resolution.** `SliderSet::ResolveSliderDataFile` reads *local* data from the set's DataFolder, and other data from the target shape's `DataFolder` attribute (a `;` list). The planner and the validation follow this (0.2.x used the shape attribute for local data too, which wrongly blocked outfits with an embedded `CBBE` body shape).
- **Raider Punk package** (real files, read-only copies, 2026-09-28): 17 projects. CONVERT 2 (Shorts, Shorts V2: BUTT+THIGH, cloth data). KEEP 2 (Top V1 already converted; Top V2 3BBB TEST). REVIEW 5: Outfit, Outfit 2 and Outfit v3 embed a plain CBBE body; Bottom has the analyzer's rigid-accessory (Belt) review; Top V2 CBBE is a manual 3BBB variant. SKIP 2 (Bracelets, Heels). UNSUPPORTED 6 (BT3). ERROR 0.

## 7. Phase 3.1 findings: embedded CBBE body (Outfit Studio 5.8.2 source + sandbox runs on copies of `MM69 Raider Punk Outfit - CBBE`)

The accepted output mod and the source mods were not touched; every experiment ran on staged copies in the sandbox.

- **E1, the 0.3.0 path (clothes only, AutoCopyRef=false):** the embedded `CBBE` body **loses its slider data**. `AddProject` marks merged slider data non-local but keeps the shape's `DataFolder` attribute (`CBBE`). The source data was *local* (in the set's own DataFolder), so the saved project points at `ShapeData\CBBE\<outfit>.osd`, which does not exist. BodySlide has no fallback (`SliderSet::ResolveSliderDataFile`), so the body would not morph. 0.3.0 never converted such a project (REVIEW / EMBEDDED_BODY), and its "slider data resolvable" check would have failed it.
- **E2, identity weight copy onto the body, no slider import:** weights are correct but the slider-data problem of E1 remains.
- **E3, AutoCopyRef=true + DeleteShape CBBE:** the saved project contains the **full** CBBE-3BBB reference body (no deleted vertices, no outfit zaps) and the reference's slider data. The outfit's intended partial body is lost, which gives clipping.
- **E4, chosen method `reference_weights_by_vertex_identity`:** the clothing CopyBoneWeights as before, then `CopyBoneWeights` targeting **only the body** with radius 0.01 and MaxResults 1, then `ImportSliderData` of the `.osd` files that the body's slider data references (FromFolder=false, MergeSliders=true), then SaveProject with AutoCopyRef=false. Every body vertex coincides with a CBBE-3BBB reference vertex, so each gets that vertex's weights exactly (the reference's 3BBB physics bones included). ImportSliderData → `SetSliderFromDiff` → `EnsureSliderDataLocal` makes all slider data project-local, and the DataFolder list is cleared. Geometry, triangles, deleted vertices, sliders and zaps are unchanged. Result: engine VALIDATED (47 checks). A BodySlide 5.8.2 build (CBBE Curvy, Build Morphs) of the result is identical in geometry to a build of the original (every shape max deviation 0.0000), the TRI file is byte-identical, and the body weights equal the reference.
- **E5, as E4 + ClearReference before saving:** the saved project loses `ReferenceInfo`, so it is not used.
- **Body identity on real files** (read-only): Raider Punk Outfit, Outfit 2, Outfit v3, Top V2 CBBE and Bottom bodies are all BODY_CBBE_COMPATIBLE / HIGH. For `Outfit - CBBE`: 38.5 % of the reference covered (a partial body, the covered parts are deleted), welded-triangle subset 1.000, 84 slider morphs within 0.0155 of the reference's, no 3BBB bones, and 4 328 vertices whose weights change (the author customised chest weights; the change is reported, not treated as a mismatch).
- **Package preview** (sandbox, real files): option OFF: CONVERT 0, KEEP 4, REVIEW 5, SKIP 2, UNSUPPORTED 6 (unchanged from 0.3.0 once Top V1, Shorts and Shorts V2 are converted). Option ON: Outfit, Outfit 2 and Outfit v3 become CONVERT / EMBEDDED_BODY_REPLACEMENT (not selected by default); Bottom stays REVIEW / ANALYZER_REVIEW and Top V2 CBBE stays REVIEW / MANUAL_3BBB_VARIANT.

## 8. First real Phase 3.1 in-game test (2026-09-28): "Outfit has no physics"

Read-only forensics on files staged from the Windows installation:
- **Generated ShapeData** (`Outfit - CBBE - 3BBB AUTO.nif`, 20:01:39): the jewel, shorts, heels, top and CBBE body shapes as planned:
  - top: 6 breast bones;
  - shorts: butt and thigh bones;
  - CBBE body (8 467 vertices): all 12 3BBB bones;
  - heels and jewel: no physics bones.
- **Built mesh** `mods\Raider Punk CBBE - BT3 BT4 A-Body\meshes\MM69 Raider Punk\raiderPunk.nif` (20:04:47, written after the 20:02:47 commit; no copy in overwrite or in the output mod): the same 5 shapes, the same bone lists, **weights identical** (max difference 0 on every shape). Only the positions moved, by the CBBE Curvy preset (≤ 1.9 units). The block structure matches the working controls (BSSubIndexTriShape + BSSkin, no cloth data).
- **Plugin** `MM69 Raider Punk.esp`:

  | In-game item | ARMO | ARMA | Female mesh | Built by project |
  | --- | --- | --- | --- | --- |
  | Raider Punk Outfit | 01000F9B | 00_MM69_AA_Raider_Punk_Outfit | `raiderPunk_2.nif` | Outfit 2 - CBBE (not converted) |
  | Raider Punk Outfit V2 | 01005416 | 00_MM69_AA_Raider_Punk_Outfit_2 | `raiderPunk.nif` | Outfit - CBBE → 3BBB AUTO |
  | Raider Punk Outfit V3 | 0100634B | 00_MM69_AA_Raider_Punk_Outfit_3 | `raiderPunk_outfitF_3.nif` | Outfit v3 - CBBE |
  | Raider Punk Top / Top V2 / Shorts / Shorts V2 | | | `raiderPunk_top`, `_top_v2`, `_shorts`, `_shorts_v2` | names agree |

- `raiderPunk_2.nif` (last written 2026-05-14) has only the legacy `LBreast_skin`/`LButtFat_skin` weights, so the item "Raider Punk Outfit" has no 3BBB physics. The item "Raider Punk Outfit V2" (reported as working) *is* the converted mesh.
- **Conclusion:** the failure was not in the conversion, the BodySlide build, MO2 output resolution or the NIF structure. It was a mapping between BodySlide project names and in-game item names that the validation could not see. Fixed by `verify-build` and the in-game item lookup (`BATCH.md` §9).

## 9. Raider Punk Outfit 2 validation failure (2026-09-28, batch 20260928-202953)

Reproduced in the sandbox with the same script on copies (identical ShapeData NIF size, 481 055 bytes; the same 5 Pants vertices and the same Hip_Case 0.0068). The failed Windows workspace itself was only partly readable from here: its ShapeData sits too deep for the file bridge.

- **Pants, a real defect.** Vertices **466 and 771** (duplicates on a UV seam at the rear waist) are **6.58** units from the nearest CBBE-3BBB vertex. Source weights: Pelvis_Rear 0.1045, Spine1_Rear 0.6362, Spine2_Rear 0.2593, all reference bones. `OutfitProject::CopyBoneWeights` zeroes every copied bone and fills in only from reference vertices within the radius, and in full mode every reference bone is copied. So both vertices came back with **no weight at all** (sum 0.000); in game they would collapse. The other three vertices flagged as >1% off (109, 786, 796: sums 0.987 / 0.988 / 0.984) have 5+ influences after the copy. nifly keeps the 4 largest after dividing by the full sum, which is the same Fallout 4 4-influence truncation as in the accepted Top V1 (upper 0.976). Those three are not the failure: the check allows ±0.05 and failed only because of the zero sums.
- **Fix:** radius 7.0 for Pants only. The emulated copy changes exactly vertices 466 and 771; every other Pants vertex has ≥ 4 reference vertices within 5, so its result is unchanged. Sandbox Outfit Studio 5.8.2 run with the new script: VALIDATED (49 checks). Compared with the failed run, only Pants vertices 466 and 771 differ, and they now carry Pelvis_Rear 0.1044, Spine1_Rear 0.6362, Spine2_Rear 0.2593 (≈ the source weights). Every other shape is identical.
- **Hip_Case, a validator false positive.** 4 source vertices (41, 64, 162, 163) have weight sums of 0.9965 / 0.9916. Outfit Studio's SaveProject rewrites every shape's weights through `NifFile::SetShapeVertWeights`, which divides by the sum. The output shows exactly those vertices scaled to 1.0 (0.8003 → 0.8071), with the same bones and the same bone order, and no geometry change. After per-vertex normalization the difference is 0.0002 (half-float storage). AddProject, LoadReference and ImportSliderData do not change weights. Belt's 0.0001 is the same effect.
- **Outfit (V1) did not show this:** all of its target vertices are within 5 units, and its untouched shapes' source sums are within 0.9997–1.0003.
- **Body:** 6 635 vertices, all on reference vertices, weights identical (max 0.00003), legacy LBreast_skin/LButtFat_skin gone, 8 3BBB bones (breast + butt; the author deleted the thigh part), slider data resolves (77/77).

## 10. Phase 4.0: HN66 EasyGirl (Direct NIF) and Outfit Studio's direct-NIF path (2026-09-28)

**Package** `HN66Fo4_EasyGirl_BS` (read-only copies staged from the installation; MO2 profile "Fix F", where the plugin is active):
- **Contents:** 1 plugin (`HN66Fo4_EasyGirl_BS.esp`, not localized, 210 ARMO / 210 ARMA), 300 NIFs, 302 DDS, 165 materials, **no** `.osp` and no `.tri`.
- **Female items and meshes:** 210 items have a female third-person model; 141 unique female meshes (one mesh is shared by up to 5 items).
- **Male and other models:** 153 items have a male model, and 21 male-only meshes (`*_NateOutfit_*_SHB.nif`) are skipped. 4 models are first-person only.
- **Plugin quirks:**
  - one referenced mesh does not exist in any mod (`40_EasyGirl_JeansPants_Ripped_White_BS.nif` → ERROR);
  - several vanilla first-person paths point into archives;
  - several glasses items use the gauntlet mesh as their female model.
- **Structure:** all 310 shapes are BSSubIndexTriShape + BSSkin. 5 meshes have BSClothExtraData (pleated/plaid skirts, Maxon's coat). No male/female dual-use mesh.
- **Embedded bodies:** every body-slot (33) item (shoes and boots) embeds a CBBE body with legacy weights:
  - BuckleBoots and RadSuit: 100 % on the CBBE-3BBB reference, 58–61 % of it (feet/legs deleted);
  - the other 19: 92–94 % of their vertices coincide, while their feet vertices are posed for heels → AMBIGUOUS.
- **Clothing without a body** lies on the CBBE surface (median distance 0.16–1.7) and carries legacy LBreast_skin / LButtFat_skin weights.
- **Real classification** (`--allow-direct-nif --allow-embedded-body-replacement`):

  | Action | Count | Reasons |
  | --- | --- | --- |
  | CONVERT | 63 | all DIRECT_NIF_HIGH_CONFIDENCE (tops, tank tops, blouses, mini-dresses, corsages, bikini tops, thongs, hot pants, flight suits …) |
  | KEEP | 0 | |
  | SKIP | 17 | NO_PHYSICS_REGION (bracelets, gauntlets, waist belts, collars, socks, navel piercings, glasses, gloves) |
  | REVIEW | 60 | AMBIGUOUS_BODY 19, ANALYZER_REVIEW 20 (thigh-highs as footwear, jackets/coats/vests as outerwear, layered pants, fit), RIGID_REGION_OVERLAP 11 (zips/rivets/gun belts/nipple piercings), CLOTH_DATA_UNSUPPORTED 5, UNSUPPORTED_NIF_STRUCTURE 3 (BuckleBoots ×2: shape 'Chest'; Vest: shape 'Neck'), UNSAFE_RADIUS 2 (long-sleeve knit: 112 sleeve vertices 8.1 from the body) |
  | UNSUPPORTED | 0 | |
  | ERROR | 1 | MISSING_GAME_MESH |

  With the body-replacement option off the counts are the same, except that RadSuit is REVIEW EMBEDDED_BODY instead of ANALYZER_REVIEW (its suit fit is only medium).

  Neither run produces an EMBEDDED_BODY_REPLACEMENT candidate: BuckleBoots are blocked by the shape-name clash, and the other body-slot items are AMBIGUOUS.

  A LAYERING RISK note applies: all 22 body-slot items keep their plain CBBE body.

**Outfit Studio 5.8.2 direct path** (source read, and sandbox runs of `ClearProject → LoadReference → ImportFile → CopyBoneWeights → ExportFile`):
- `ImportFile` loads an arbitrary NIF without a project. `ImportNIF`:
  - renames the root node to "Scene Root" (EasyGirl roots already are);
  - renames duplicate shapes;
  - renames a shape whose name is already taken: `33_EasyGirl_BuckleBoots_White_BS` shape **'Chest' came back 'Chest_1'** because a skeleton bone is named Chest.
- `ExportFile` (`OutfitProject::ExportNIF`):
  - with `WithRef` false it drops the reference;
  - it calls `CleanupBones`: unweighted bones disappear (e.g. Chest_Upper_skin after a full copy);
  - it adds the new bone nodes and rewrites skin partitions;
  - it keeps material paths, texture sets and BSSubIndexTriShape segment data **byte-identical**, and block sizes are unchanged;
  - it drops unused header strings.
  `WithRef` defaults to true, and a relative export path is **not** resolved against the project.
- **Headless:** export works (no GL meshes, so the geometry comes from the work NIF; vertices move ≤ 5e-6). With BSClothExtraData, ExportFile opens "Choose cloth data" and a headless run **hung** (killed after the timeout).
- **Results:**
  - TankTop MasonBlue: the 8 planned breast/butt bones were added, and the weights match the emulated full copy (mean |Δ| 0.0000, p99 0.005);
  - BuckleBoots body: identity copy gives weights equal to the CBBE-3BBB reference (max 3e-5); the other shapes are unchanged (0 weight or geometry change).
- **Conclusion:** ImportFile + ExportFile is a workable Phase 4.1 path for meshes without cloth data and without name clashes. SaveProject is not appropriate, because it would create a BodySlide project the package never had.

## 11. Phase 4.1: first Direct-NIF batch, sandbox acceptance (2026-09-29)

Exact production path (`plan-direct-batch` → `convert-direct-batch` → `commit-direct-batch` → `verify-direct-build` code) on staged **copies**. The sandbox MO2 layout (profile "Fix F") links the staged mods read-only. The output mod `CBBE 3BBB Auto Conversions` is a separate **sandbox copy**, so the staged real output mod was never written. Outfit Studio: the patched 5.8.2 Linux build started with `-a`, the equivalent of the interactive Windows run.

**Plan:** 4 selectors, each resolved to one analyzed game mesh.

| Mesh | ID | Plan |
| --- | --- | --- |
| 38_EasyGirl_TankTop_MasonBlue_BS | dn-2dac7435da | OK: TankTop46-B, 3412 v, breast + butt, 8 bones, radius 5 |
| 40_EasyGirl_HotPants_Jeans_BS | dn-b724e392a6 | OK: RIPPED HOTPANTS, 2110 v, butt + thigh, 6 bones, radius 5 |
| 45_EasyGirl_Thong_BlackLeather_BS | dn-49f91d1053 | OK: thong PREC2, 405 v, butt, 2 bones, radius 5 |
| 38_EasyGirl_MiniDress_BlackLatex_BS | dn-0687b9de10 | **REFUSED: FULL_COPY_OUTSIDE_REGIONS** |

**MiniDress (real reason; validation not weakened):**
- Planned regions: breast + butt (thigh relevance below MEDIUM).
- A full Copy Bone Weights would also give the 4 thigh bones weight on the hem: 186 vertices at z 65.6–67.7, max weight 0.040.
- That is an unplanned 3BBB region. The BodySlide path would switch such a project to a region-restricted copy with MEDIUM confidence, which is not validated in game.
- The 0.4.0 analysis did not run this check. It now does, so 8 former CONVERT meshes become REVIEW FULL_COPY_OUTSIDE_REGIONS:
  - 4 mini-dresses and 1 mini-skirt: thigh weights at the hem, max 0.04;
  - 2 laced corsages and 1 short blouse: butt weights, max 0.08–0.10.
- New real counts: CONVERT 55, REVIEW 68, SKIP 17, ERROR 1.
- Options for the MiniDress (a user decision, not taken here):
  - plan the thigh region as well (the hem gets weak thigh motion);
  - validate a region-restricted copy in game first;
  - leave it unconverted.

**Conversion:** one Outfit Studio session with 3 parts; ImportFile with an absolute path worked. All three meshes are **VALIDATED** with no failed check (29 / 29 / 28 checks).

| | TankTop MasonBlue | HotPants Jeans | Thong BlackLeather |
| --- | --- | --- | --- |
| source SHA-256 | 434c1dcac0120038… | 9d8aa4c6b07ba8e0… | 135d614875c508ff… |
| generated SHA-256 | 2a260db283a5780c… (179 085 B) | 039dd83ebd873b07… (104 701 B) | c0edcfbe07d1a4d4… (22 543 B) |
| shape (same name out) | TankTop46-B | CBBE HN66 body V1 - RIPPED HOTPANTS | thong PREC2 |
| regions / radius | breast + butt / 5 | butt + thigh / 5 | butt / 5 |
| 3BBB bones (vertices) | L/RBreast_01–03 (537/472/367, 566/471/369), L/RButt_01 (82/82) | L/RButt_01 (475/427), thigh F/R (19/35, 18/42) | L/RButt_01 (72/63) |
| geometry, triangles, normals/UV/tangents | byte-identical | byte-identical | byte-identical |
| material / textures | identical (`hn66_gagetanktop_blue.bgsm`) | identical | identical |
| segments (BSSubIndexTriShape, 76 B) | identical | identical | identical |
| weight sums | 0.982–1.000 | 0.976–1.000 | 0.995–1.000 |
| prediction mean / p99 \|Δw\| | 0.00003 / 0.026 | 0.00002 / 0.024 | 0.00002 / 0.021 |
| agreement | breast 0.999, butt 1.000 | thigh 1.000, butt 1.000 | butt 1.000 |
| reference leak / cloth | none / none | none / none | none / none |
| re-parse / analyzer | OK / KEEP ALREADY_3BBB | OK / KEEP | OK / KEEP |

**Commit and verify (sandbox output-mod copy only):**
- Commit: 3 files at `meshes\HN66FO4\EasyGirlOutfit\…` in the copy, read-back SHA-256 identical, 3 `direct_conversions` records; the 4 Raider Punk BodySlide records are unchanged.
- `verify-direct-build`: 3 × DIRECT_OK. The output mod (modlist line 90) wins over EasyGirl (line 104), and every in-game item is listed.
- Re-analysis: the three meshes are KEEP ALREADY_CONVERTED_DIRECT_NIF.

**Unchanged:**
- the staged `HN66Fo4_EasyGirl_BS` (166 files, SHA-256 before/after);
- the staged real output mod (12 files).

**Outfit Studio limitations noted:**
- 5.8.0–5.8.2 still need the interactive run on Windows (headless CopyBoneWeights no-op, `UPSTREAM_ISSUE.md`);
- cloth data blocks ExportFile with a dialog, so cloth meshes stay REVIEW;
- a shape named like a bone is renamed on import, so those meshes stay REVIEW;
- `WithRef` defaults to true and relative export paths are not project-relative; both are handled explicitly.

## 12. Phase 4.1R: the first real Windows Direct-NIF batch (2026-09-29, batch 20260929-201925)

**Result on Windows:** Outfit Studio 5.8.2 ran interactively and exported all three meshes (TankTop MasonBlue, HotPants Jeans, Thong BlackLeather). All three FAILED on one check: "geometry and vertex attributes identical … vertex data DIFFERENT".

**Forensics** (read-only copies of the workspace; Outfit Studio not rerun):
- **What that check covered:** 0.4.1 already compared the vertex buffer *without* the 12 skin bytes (4 half-float weights + 4 bone indices at offset 28). A changed skinning could not trigger it.
- **Field-by-field decode** (desc `0x45b0007065040a`, 40-byte vertices: position 0–11, bitangent X 12–15, UV 16–19, normal 20–22, bitangent Y 23, tangent 24–26, bitangent Z 27, skin 28–39): positions and UVs are identical for every vertex. Only the normal/tangent/bitangent bytes differ:

  | Mesh | Vertices changed | Max normal change | Example (source → Windows) |
  | --- | --- | --- | --- |
  | TankTop46-B | 12 / 3412 | 8.7° | v2847 normal (244,134,77) → (245,152,84) |
  | RIPPED HOTPANTS | 20 / 2110 | 28.4° | v475 normal (123,69,14) → (123,130,0) |
  | thong PREC2 | 6 / 405 | 16.7° | v198 normal (186,188,32) → (175,201,35) |

- **Windows vs sandbox output:** same file sizes. They differ in only 120 / 170 / 59 bytes, all inside those normal/tangent/bitangent fields. Skinning, bones, segments, materials and block structure are bit-identical.
- **Mechanism (proven by emulation):**
  - When the GUI builds a mesh, Outfit Studio copies the file's normals (`GLSurface::AddMeshFromNif`).
  - The interactive session then recomputes them with `Mesh::SmoothNormals`: area-weighted face normals, plus seam welding. Vertices at the same position (`SortingMatcher`) are averaged when their normals are less than 60° apart.
  - `OutfitProject::ExportNIF` writes those mesh normals (`SetNormalsForShape`) and recomputes the tangents (`CalcTangentsForShape`).
  - A numpy re-implementation on the source geometry (nifly quantization `round((x + 1) / 2 · 255)`) gives the Windows normals exactly: TankTop 3412/3412, Thong 405/405, HotPants 2107/2110 (the other 3 differ by one quantization step).
  - For every other vertex the author's normals already equal this recomputation. The changed vertices are all UV-seam vertices where the author kept split (hard-edge) normals and Outfit Studio smooths them.
- **Why the sandbox passed:** the patched headless build creates no GL meshes, so `ExportNIF` has no live mesh to take normals from and keeps the file's normals.
- **Conclusion:** the check was right. The Windows export changes shading data at a few seams; it is not an artefact of comparing skinning. The three meshes remain FAILED and nothing was committed. 0.4.2 reports every attribute separately, so this now shows as "normals identical: 12/3412 vertices differ …; max 8.70°".
- **Possible remedies (not implemented; a decision for the user):**
  - accept Outfit Studio's seam smoothing as an explicit, verified policy: only welded seam vertices, and only exactly the emulated recomputation;
  - restore the source normal/tangent bytes into the exported NIF, then require full equality;
  - leave these meshes unconverted.

  A BodySlide build also recomputes normals with `SmoothNormals`, for the vertices its sliders move.

## 13. Phase 4.1R2: post-export restoration on copies of the real Windows exports (2026-09-29)

The raw Windows exports of batch `20260929-201925` were copied into a recovery workspace; the batch itself was not changed. `restore_immutable_vertices` was run on the copies, followed by the full strict validator; Outfit Studio was not rerun.

| Mesh | Raw differences vs source (vertices) | Restored bytes | Final vs source (immutable) | Final vs raw (skinning) | Checks | Result |
| --- | --- | --- | --- | --- | --- | --- |
| TankTop MasonBlue | normal 12, tangent 12, bitangent X 12 / Y 5 / Z 5; skin weights 3412, indices 3412 | 120 | identical | identical | 39/39 | VALIDATED |
| HotPants Jeans | normal 20, tangent 17, bitangent X 20 / Y 11 / Z 13; skin weights 2050, indices 1109 | 170 | identical | identical | 39/39 | VALIDATED |
| Thong BlackLeather | normal 6, tangent 6, bitangent X 6 / Y 4 / Z 6; skin weights 380, indices 253 | 59 | identical | identical | 38/38 | VALIDATED |

- **Attribution:** every vertex with a normal or tangent-basis difference is a duplicated seam vertex, which is the condition the raw-diff policy requires. No other field differed.
- **Weights unchanged by the restoration:** prediction mean |Δw| 0.00003 / 0.00002 / 0.00002, the same 3BBB bones as the plan, no vertex without weight, ≤ 4 influences, no cross-side anomaly, analyzer KEEP, no reference body.
- **The restored files are byte-identical to the 0.4.1 sandbox exports** (SHA-256 `2a260db2…`, `039dd83e…`, `c0edcfbe…`): Windows skinning plus source vertex attributes is exactly what the headless build produced.

## 14. Phase 3.2: CP5 "bodysuit v1" REVIEW (pilot batch 20260929-222016)

**Symptom:** the real bodysuit v1 came back REVIEW with "agreement n/a" and "analyzer: SKIP / project contains only a body/reference shape (no outfit geometry)". Everything else passed: geometry identical, 12 planned physics bones, full-copy prediction met (mean |Δw| 0.0000).

**Reproduction** on staged copies of the full CP5 (406 files), with the patched Outfit Studio 5.8.2: the same two messages, and a byte-identical generated NIF (1 519 567 bytes).

**Root cause:**
- Outfit Studio saves the generated project with `<Shape target="bodysuit_v1" DataFolder="CP5_DonEb14n bodysuit v1">`, because the slider data stays in the source project's folder.
- `analysis/shape_metrics.py` treated *any* per-shape DataFolder as body-name evidence. It combined that with a size rule (≥ 8000 vertices, ≥ 80 units tall).
- The suit has 34 852 vertices and runs from neck to ankles, so it was analysed as a body. That means no region metrics (agreement n/a), and the project is "only a body".
- With the per-shape DataFolder removed, the same file gives BREAST 0.999, THIGH 1.000, BUTT 1.000. The small bikini pilots never met the size rule.

**Fix:** per-shape DataFolder evidence counts only for body or reference folders. Validation and thresholds are unchanged.
- Library-wide re-analysis (840 projects): 4 classifications changed. Tao's Latex Catsuit and Vtaw Jumpsuit 3BBB (full-body outfits in merged projects) went from SKIP "only a body" to KEEP; the other two only lost a mislabelled body shape.
- CP5 counts are unchanged: CONVERT 99 (+ 2 pilot KEEP ALREADY_CONVERTED), REVIEW 24, SKIP 11.
- Regression test: `tests/test_bodysuit.py`. Without the fix it fails with exactly the two real messages.

## 15. Phase 3.2: real CP5 mass conversion on a disposable copy (2026-09-29)

Setup:
- MO2 copy: every mod links to the staged copies. The output mod is a real copy of the user's (pilot bikinis, EasyGirl Direct-NIF, Raider Punk).
- Outfit Studio: the patched 5.8.2 build, in the pipeline exactly as the GUI runs it.

| Run | Selected | Sessions (projects / seconds) | Result |
| --- | --- | --- | --- |
| SAFE | 89 (CONVERT + HIGH) | 25/27 s, 25/14 s, 25/18 s, 14/9 s | 66 VALIDATED + committed (incl. bodysuit v1, v4, v5); 23 REVIEW |
| ALL_POSSIBLE, same copy afterwards | 45 (23 SAFE retried + 10 MEDIUM + 12 REVIEW_ATTEMPTABLE) | 25/32 s, 20/22 s | 68 recognised as already converted (none repeated); 6 more committed: hoodie v2, jacket v1, panty v4, sweater v5, t-shirt v5, top v18 |

- **The 23 SAFE REVIEWs all fail only `weights normalized`:** minimum vertex weight sums 0.913–0.950, and 0.848–0.95 across all attempts. This is the 4-influence truncation described in `BATCH.md` §12, not a converter or validator error. They stay unconverted. A post-export renormalization of the 4 stored weights, in the style of §13, is the candidate next step and needs a decision.
- **Other outcomes:**
  - 11 projects have no safe weight-transfer radius (BLOCKED_WEIGHT_TRANSFER);
  - jacket v3 has undeclared existing 3BBB weights (EXISTING_3BBB_AMBIGUOUS);
  - 11 need no conversion;
  - 12 REVIEW_ATTEMPTABLE are not tried in Safe mode.
- The SliderGroup of the package lists exactly the 74 committed CP5 AUTO projects (2 pilot + 66 + 6). The source files were SHA-256-identical before and after, and the staged real output mod was unchanged. `verify-package-build`: every project NOT_BUILT (no BodySlide build in the sandbox), both groups present, output mod ENABLED in the copy of "Fix F".

## 16. Phase 3.2 / 0.5.1: skin-weight renormalization, fresh disposable CP5 run (2026-09-29)

Setup: a new MO2 copy with fresh links to the staged sources and a fresh copy of the output mod; nothing was reused from the 0.5.0 run. Patched Outfit Studio 5.8.2.

| Run | Selected | Sessions | Result |
| --- | --- | --- | --- |
| SAFE | 89 | 25 + 25 + 25 + 14, no session failure, 128 s | **89 VALIDATED + committed**, including all 23 that were REVIEW in 0.5.0 (weight sums) |
| ALL_POSSIBLE afterwards | 22 (10 MEDIUM + 12 REVIEW_ATTEMPTABLE) | 22, 62 s | 91 recognised as already converted; **20 more committed**; 2 REVIEW |

**Renormalization over both runs:**
- 103 of 111 generated files repaired, 8 needed nothing, 0 refused.
- 35 341 vertices rescaled. The lowest sum before was 0.848; the largest |sum − 1| after was 0.0004.
- The only failed checks in both runs are "breast 3BBB bones present" (chain v1) and "thigh 3BBB bones present" (leggings v5). They are the same two failures as the 0.5.0 run: no new validation failure.

**Remaining 14:**
- 11 have no safe weight-transfer radius: hoodie v3, hoodie v4, onepiece v01, onepiece v14, skirt v3, skirt v4, skirt v5, sweater v12, sweater v16, sweater v17, top v15.
- jacket v3 has existing, undeclared 3BBB weights.
- chain v1 and leggings v5 were attempted, but the physics bones did not come out as planned.

The sources were SHA-256-identical before and after. The real output mod on the PC was re-read: manifest and files have the same sizes and modification times as before the session. The earlier 0.5.0 baseline hash of its Raider Punk SliderGroup came from a stale sandbox copy (Sep 28, 20:12). The PC file itself dates from Sep 28, 20:50, before this work.

## 17. CP5 final acceptance (2026-09-30)

The user's real mass conversion of "Clothes Pack 5 CBBE BodySlide" with 0.5.1 is **accepted**: the vast majority of the package works in game. CP5 counts as production validation of the CBBE → CBBE 3BBB pipeline.

Accepted limitations (no expected physics in game), with no further conversion work:
- **No safe weight-transfer radius, not converted:** hoodie v3, hoodie v4, onepiece v01, onepiece v14, skirt v3, skirt v4, sweater v12, sweater v16, sweater v17, top v15.
- **Converted and validated automatically, but no physics in game:** leggings v4. This is a runtime-only exception; it is recorded as an in-game verification report (`report-runtime-issue`) and the automated result is not changed.

Requirement taken from this acceptance (0.5.2): every outfit without the requested physics stays visible by name in the final package result ("Needs attention / original kept"). A still-usable original is not reported as a success.


## 18. Phase 0.5.3: driving Outfit Studio 5.8.2 without the user (2026-09-30)

Sources: Outfit Studio v5.8.2 (`ecab889a`, tag v5.8.2), wxWidgets 3.2.4 source for the MSW control behaviour, and the converter's private runtime on the PC (`OutfitStudio.exe` SHA-256 `53cec595…4218c`, built with **wxWidgets 3.3.3** / MSVC; its `Log_OS.txt` and `OutfitStudio.xml`).

**How scripts are found and run (5.8.2):**
- `AutomationDialog::GetAutomationsFolder()` = `<ProjectPath>/Automations`; `PopulateAutomationList` lists `*.xml` there (sub-folders as groups) in the `cmbAutomation` combo box (native CBS_DROPDOWN).
- The constructor restores **`AutomationDialog.lastScript`** from `<exe dir>/OutfitStudio.xml` and calls `LoadAutomation(name)` when the name is in the list; the destructor saves it again. On the PC the file already held the last session's script name. This is the exact-selection mechanism: the converter writes the session's name there before starting its private copy.
- *Execute* (`btnExecuteAll`) → `OnExecuteAll` → `wxMessageBox("Execute %zu active step(s) on the current project?", "Confirm Execution", YES_NO)` → `ExecuteSteps`: one `Automation: Step i/N: <type>` log line per step, `wxYield()` between steps, and at the end `EndProgress("Automation complete.")` + `wxMessageBox("Automation completed: %d step(s) executed.", "Automation")`. A failing step asks "Automation Error … Continue with remaining steps?" (headless: logged and continued).
- `StartProgress` redirects wxLog to the window's output pane (`txtOutput`, a read-only rich edit) while the script runs: that is why `Log_OS.txt` has no step lines in the windowed mode (§8), and why the converter reads the pane (WM_GETTEXT) for progress and step→project attribution.
- The only entry points are the menu item `menuRunAutomation` ("Open Automation...", File menu, 11th entry) and the headless `-a` option. The IPC server (`OutfitStudioIPC`, DDE on Windows) only opens files (`LoadFiles`), no automation. `-a` cannot be combined with a shown window in the release build.
- **Readiness:** `OnInit` logs "Outfit Studio initialized." and the GL panel logs "OpenGL Context Info:" on its first show — the condition the headless mode never reaches. Both are in the real Windows log within 1 s.
- Window titles: main "Outfit Studio" (later "<project>[*] - Outfit Studio"), "Automation Script", task dialogs with the captions above; "Choose cloth data" is a wx `wxMultiChoiceDialog` (native check list box, OK = wxID_OK 5100, Cancel = 5101, nothing ticked by default, item 0 selected). `wxMessageBox` on Windows is a task dialog (`TaskDialogIndirect`), answered with `TDM_CLICK_BUTTON`.

**Why no patch:** a patched 5.8.2 would have to be built for Windows by the converter (MSVC/vcpkg wx 3.3.3 toolchain not available here); it would replace the release binary that produced the accepted CP5/Wardrobe 10 results. The message-level control keeps the accepted binary byte-identical. No simulated input, no coordinates (`tests/test_win32_ui.py` asserts SendInput/SetCursorPos/… are not used).

**Choose cloth data (source):** `AddNif`/`LoadReference` store each loaded file's `BSClothExtraData` in `OutfitProject::clothData[fileName]` (absolute path `<ProjectPath>\ShapeData\<folder>\<file>`) and **delete** the blocks from the work NIF (`DeleteBlockByType("BSClothExtraData")`); only the origins ticked in the dialog are added back at save. Consequences: pressing OK without a tick, or Cancel, silently drops the cloth data — the dialog cannot simply be dismissed; and the map keeps one block per file (a file with several cloth blocks keeps only the last), which the validation (payloads and count equal to the source) catches.

**Real Wardrobe 10 batch (`20260930-214438`, 0.5.2, 11 manual sessions):**
- Cloth projects: Latex Suit Top (session 9, committed), Dress 2 V2 (last of sessions 7/8), Skirt V2 (sessions 10, 11: never saved). The committed Latex Suit Top NIF carries exactly the source's block (105 072 bytes, sha1 `c56592f7…`), i.e. the manual answer "tick the single own origin" is the right one — the rule A the converter now applies.
- **Converter bug:** sessions 7 (closed without Execute) and 8 (the retry) held the same 12 projects. Session 8 saved all of them (22:16:09–22:17:01), but validation took the *first* session containing the project (`_session_of`), so the outputs were "outside the session window" → 12 false FAILED. Fixed in 0.5.3 (latest session; leftovers of an earlier attempt moved aside). 6 of the 12 had no other failed check.
- The first build reported BUILD_STALE for all 138 committed projects until the AUTO group was built last; the check was right, the handoff was not explicit enough (0.5.3 handoff + explanation).

**BodySlide 5.8.2 command line:** `--groupbuild <groups,…>` (`-gbuild`), `-t/--targetdir`, `-p/--preset`, `--tri/--trimorphs`. `GroupBuild` builds only the members of the named groups, applies saved BuildSelection choices for conflicts inside the group, and writes to GameDataPath unless `-t` is given. Building only the package AUTO group therefore never conflicts with the originals; it is not started automatically in 0.5.3 because the output location depends on how BodySlide runs through MO2 (overwrite vs. a configured mod).

**Regression on a fresh disposable CP5 copy (sandbox, 0.5.3 code, the patched Linux 5.8.2 engine as in §16, empty output mod):** Safe 91 converted (the 89 of 0.5.1 plus the 2 that were already committed by the 0.4.3 pilot there), Convert All Possible +20, 14 remain — the same 14 as §16/§17. 110 of the 110 NIFs that 0.5.1 generated are byte-identical (the 111th in that copy came from the 0.4.3 pilot, without renormalization). CP5 sources and the CBBE 3BBB reference are SHA-256-identical before and after. The unattended control itself is Windows-only and is not part of this run.

**Acceptance status:** the controller is exercised against a model of the 5.8.2 window flow (`tests/fake_win_os.py`) and a model of user32 (`tests/test_win32_ui.py`). The real Windows run of the unattended control is the acceptance step on the PC (§19 when done).

## 19. First real Windows acceptance of the unattended control (2026-10-01, batch `20261001-002408`)

- `probe-outfit-studio`: PASS (start, readiness, menu command, Automation window, exact script and step count, close).
- W10 `--no-commit`, 26 planned projects (25 + Dress 2 V2 alone): both sessions stopped at "Confirm Execution" — `unexpected buttons … {0: 'No'}`; then the control-failure stop. Nothing executed (`started_at` null, no step lines, no output), nothing installed.
- Captured hierarchy: `#32770 "Confirm Execution"` with `Button "&Yes"` and `Button "&No"`, **both control ID 0** (task dialog: DirectUIHWND > CtrlNotifySink > Button). The driver keyed buttons by ID → one entry. Fixed (§13 BATCH, CHANGELOG 0.5.3 update): buttons by text, exact title + exact button set, press via `TDM_CLICK_BUTTON` with the ID wxWidgets assigns.
- wxWidgets `msw/msgdlg.cpp`: wxYES_NO → IDYES/IDNO; wxYES_NO|wxCANCEL → + IDCANCEL; OK-only → one custom button **IDCANCEL** labelled "OK". The completion message would otherwise not have been closable with IDOK.
- The doubled "Converting: …" line was two progress announcements of the same first project, not two executions.

## 20. 0.5.3 main acceptance PASSED; targeted cloth-dialog acceptance prepared (2026-10-01)

- Real W10 `--no-commit` unattended run (batch `20261001-195017`): 26 attempted, 7 converted, 0 failed; zero manual Outfit Studio interaction. The user accepted the main unattended requirement.
- The real *Choose cloth data* dialog was already answered automatically twice in that run:
  - Dress 2 V2 (session 1, step 152): 1 origin (the workspace copy `…\ShapeData\VtawWardrobe10\Dress2V2.nif`), policy single_verified_origin_preserved, payload 104 320 B sha256 `82e5f8ae…` source = generated, VALIDATED (41 checks);
  - Skirt V2 (session 2): payload 100 176 B sha256 `5fe9ee2e…` source = generated, VALIDATED (29 checks);
  - sources SHA-256-unchanged, real output mod manifest unchanged.
  - Not recorded then: the dialog's control hierarchy on success; payload SHA-256 by the tool itself.
- Plumbing added (deployed, SHA-256 read-back OK):
  - the cloth dialog hierarchy + `closed_after_confirmation` are always recorded;
  - OK/Cancel are identified by ID + label + enabled;
  - source/generated payload size+SHA-256 + `payload_match` go into result/manifest;
  - new DEV command `acceptance-test-cloth` (one exact project from its original source, disposable output under `work/acceptance`, forced no-commit, unattended only, prod output mod fingerprinted before/after).
- Pending: the user's run of `run_batch.bat acceptance-test-cloth --profile Default --package "Vtaw Wardrobe 10 Bodyslides" --project "Vtaw Wardrobe 10 Latex Suit Top"`. If it passes: 0.5.3 UNATTENDED OUTFIT STUDIO: ACCEPTED.
- First real run of the command (2026-10-01 20:45) refused at planning with UNKNOWN_AUTO: the real output mod holds `Vtaw Wardrobe 10 Latex Suit Top - 3BBB AUTO.osp` without a manifest record. That was an acceptance source-isolation bug, not a production-policy problem. Fixed with a scoped, verified `AcceptanceIsolation` (CHANGELOG "acceptance source isolation"); production UNKNOWN_AUTO is unchanged.

## 21. 0.5.3 UNATTENDED OUTFIT STUDIO: ACCEPTED (real Windows, 2026-10-01)

This area is **proven**; refactors must not treat it as unproven or rewrite it (`os_control.py`, `win32_ui.py`, the
session/cloth/renormalization/validation path in `batch.py`).

- Main acceptance (batch `20261001-195017`, `--no-commit`): 291 projects analysed, 26 attempted, two unattended
  sessions (25 + Skirt V2), 7 converted, 0 failed; no manual Ctrl+Shift+A / script choice / Execute / Confirm.
  Exact script verification by name + SHA-256 + step count; Confirm Execution identified by exact title + exact
  button set (task-dialog buttons all have control ID 0); completion handshake `session_result.json`.
- Targeted cloth acceptance (`acceptance-test-cloth`, Vtaw Wardrobe 10 Latex Suit Top, after the source-isolation
  fix of §20): real *Choose cloth data* = `#32770`, ListBox id 3000, OK 5100, Cancel 5101; exactly one verified origin,
  selected automatically, zero user interaction; source and generated cloth payload 105 072 bytes, SHA-256 identical;
  VALIDATED 41/41; source OSP/NIF/OSD unchanged; the existing generated AUTO OSP/NIF unchanged; production output
  mod unchanged; committed false.
- Also proven earlier in the same run: Dress 2 V2 (104 320 B) and Skirt V2 (100 176 B) cloth payloads byte-identical.

## 22. 0.6.0 Body Architecture Foundation: where CBBE / CBBE 3BBB are assumed (audit before the change)

| Where | Assumption | Kind | 0.6.0 |
| --- | --- | --- | --- |
| `classification/family.py` | CBBE/TWB/BodyTalk/Fusion Girl/Male scores (bones, embedded bodies, sliders, names); physics none/standard/legacy/3bbb | **semantic body identity** (detection) | formalised by `conversion/bodies.py` (`detect_body`: body id + analyzer confidence + evidence + conflicting evidence); semantics unchanged |
| `classification/rules.py` | Fusion Girl / BodyTalk / Male → UNSUPPORTED; UNKNOWN → REVIEW/SKIP; 3BBB physics → KEEP/REVIEW; everything else (CBBE, legacy CBBE physics, **TWB without 3BBB**) judged against the CBBE 3BBB reference → CONVERT/REVIEW | **conversion routing** (implicit) | made explicit by the route registry (`routes.py`); TWB → CBBE 3BBB is LEGACY_SUPPORTED (18 TWB CONVERT projects exist in the user's library report of 2026-09-26: TWB VANILLA 3BBB-UNI.1 15, VtawWardrobe8TWB-3B 3) |
| `analysis/reference.py`, `discovery/install.py` | the target reference is the installed "CBBE 3BBB Body" template; CBBE slider vocabulary from the installed CBBE body | target body identity + detection input | unchanged (the only target) |
| `analysis/shape_metrics.py` | body-shape name patterns (CBBE, CBBE 3BBB, TWB, BaseFemaleBody…) | detection (names) | unchanged |
| `conversion/plan.py` | LoadReference(CBBE 3BBB) → AddProject → CopyBoneWeights → SaveProject; physics bones = reference bones outside the skeleton | **physics stage** of CBBE → CBBE 3BBB | unchanged; it is `PHYSICS_CBBE_3BBB` of the route; plans now carry `route` |
| `conversion/body.py`, `validate.py` (MODE B) | embedded CBBE body = installed CBBE topology; replaced by CBBE 3BBB weights by vertex identity | validation + physics (opt-in) | unchanged |
| `conversion/validate.py` | strict CBBE → CBBE 3BBB validation (geometry, normals, weights, cloth, 3BBB bones, KEEP re-analysis) | **validation** | authoritative and unchanged; the route declares its validator (`cbbe_to_cbbe_3bbb_strict`); any other validator id fails a required check |
| `plan.py` `DEFAULT_OUTPUT_MOD`, `engine.py`/`package.py` group names, `manifest.py` `tool` | "CBBE 3BBB Auto Conversions", "CBBE3BBBConverter" | **names/paths** (serialised identities) | unchanged on purpose |
| `profiles/store.py` | preset "CBBE Curvy", output mod name | names/settings | unchanged |
| GUI (`simple_window.py`, `main_window.py`), README, report headers | "CBBE → 3BBB Converter", "CBBE3BBBConverter" | **UI/reporting only** | product name "Fallout 4 Body & Physics Converter" |
| `direct_nif.py` / `direct_batch.py` (Direct-NIF) | CBBE game meshes | separate path, not routed in 0.6.0 | unchanged (REFERENCE_GAP: not part of the route registry yet) |

## 23. Fusion Girl → CBBE → CBBE 3BBB: research for the next route (not implemented)

Sources: the user's installation (read-only: MO2 profile "Fix F", `ZeX - ZaZ Extended Skeleton`, the BodySlide mod's
`RefTemplates.xml` + `ConversionSets/References.osp`, the converter's Outfit Studio 5.8.2 runtime `res/xrc`), the
library report `reports/latest.csv` (2026-09-26, 1 630 projects), Outfit Studio source tag `v5.8.2`
(`src/program/AutomationDialogExecute.cpp`), and Nexus pages listed in the 0.6.0 report.

**Installed state:** no Fusion Girl body and no Fusion Girl outfit is installed (library families: CBBE 675,
BodyTalk 396, TWB 3BBB 364, TWB 107, CBBE 3BBB 74, unknown 12, CBBE Physics legacy 2; Fusion Girl 0). The installed
BodySlide conversion references are Vanilla↔CBBE, Skyrim↔FO4 CBBE, Jane Bod↔CBBE and CBBE Uniboob — **no Fusion
Girl↔CBBE conversion reference**. The converter's 5.8.2 runtime ships no SliderSets (by design). So every finding
below that needs FG files is a REFERENCE_GAP until a real FG package (and the FG body / a conversion reference) is
installed.

1. **Detection.** Already present (`family.py`): ≥ 3 sliders named `FG …`/`Fusion…` (+4), an embedded body named
   Fusion Girl/FG (+3), names (weak +0.5). New strong evidence available locally: Fusion Girl requires the **ZeX**
   skeleton, whose physics bones (`Breast_CBP_L/R_00..04`, `Butt_CBP_L/R_01`, `Thigh_CBP_L/R_F/B_01..02`,
   `Belly_01/02`, …; 77 bones beyond vanilla's 129) differ from 3BBB's (`LBreast_01_skin`…, `LButt_01_skin`,
   `LLeg_Thigh_01_F_skin`…). Weights on ZeX CBP bones are a strong FG (or ZeX-based) signal and must be added to
   detection evidence in the next milestone. The analyzer currently sees them as custom (non-skeleton) bones → REVIEW.
2. **Skeleton/bones.** In "Fix F", `Skeletal Adjustments for CBBE` (priority 182) wins over `ZeX` (187): the game
   uses a 3BBB-capable CBBE skeleton. FG physics weights (ZeX CBP bones) would do nothing there; they must be removed
   (DeleteBones) and replaced by the 3BBB weights of the existing physics stage. ZeX also adds anatomy bones
   (Anus/Vagina/Penis/Tongue/Tail): weights on those → REVIEW.
3. **Reference bodies.** FG and CBBE are different meshes (different topology and proportions); FG outfits fit the
   FG body. A geometry stage needs an FG-based reference whose slider morphs the FG body into the CBBE body (or the
   inverse CBBE→FG reference, see 7).
4. **Sliders/morphs.** FG outfits carry FG body sliders (`FG …`); CBBE presets do not drive them. After geometry
   conversion the outfit must get the CBBE slider set (ConformSliders to the CBBE/CBBE 3BBB reference) and the FG
   sliders must be deleted; zaps are kept.
5. **Outfit geometry.** Geometry must move by the body difference near the body (conform-based), not by a weight
   copy. Loose parts far from the body follow the nearest body vertices (proximity radius) and may need review.
6. **Outfit Studio workflow.** OS 5.8.2 has the *Convert Body Reference* wizard (GUI only, `ConvertBodyReference.xrc`)
   and, in automation, `LoadReference`, `SetSliderValues` (percent, applied with ApplySliders), `ConformSliders`
   (radius/max results/smooth/no-squeeze/axes/named sliders), `SetBaseShape` (bakes slider values), `ClearReference`,
   `DeleteSlider`, `DeleteBones`, `CopyBoneWeights`, `SaveProject`. (`MakeConversionRef`/`NegateSlider` exist only
   after 5.8.2.)
7. **Automation through the proven unattended control.** Feasible in principle as one script per outfit:
   `LoadReference(FG→CBBE conversion ref) → AddProject(FG outfit) → ConformSliders(conversion slider) →
   SetSliderValues(conversion slider 100 %) → SetBaseShape → ClearReference → LoadReference(CBBE 3BBB, MergeSliders) →
   ConformSliders(CBBE sliders) → DeleteSlider(FG sliders) → DeleteBones(ZeX CBP bones) → CopyBoneWeights →
   SaveProject`. With only a CBBE→FG reference, the inverse needs `SetSliderValues(100 %) → SetBaseShape` on the
   reference first and −100 % later — unverified (REFERENCE_GAP: conform proximity is computed against the base shape).
8. **Information lost.** FG-specific slider morphs (shape variety of FG presets), FG/ZeX physics weights, anatomy-bone
   weights, and any author fine-tuning of fit; the converted outfit fits CBBE presets only.
9. **Safe automatic conversion** (proposal): HIGH FG detection; no embedded body (or an embedded FG body that is
   replaced by the CBBE 3BBB body — separate opt-in); only body-hugging geometry within the conform radius; no weights
   on ZeX anatomy bones; a conversion reference present and verified (SHA-256); the intermediate passes §15 checks.
10. **REVIEW instead:** MEDIUM/LOW detection or conflicting evidence; embedded bodies; loose/far geometry; weights on
    anatomy/custom bones; cloth data from several origins; topology-sensitive features (11–14); any intermediate check
    failing.
11. **Embedded bodies:** an embedded FG body cannot be "converted" by conforming — it must be replaced by the CBBE
    3BBB body (like MODE B, which today requires the CBBE topology); default REVIEW.
12. **TRI/morphs:** BodySlide rebuilds TRI from the new CBBE sliders (Build Morphs); FG morphs must not survive.
13. **Cloth data:** BSClothExtraData is per-file and survives AddProject; the existing cloth policy (single verified
    origin, payload equality) applies unchanged — but cloth simulation data authored for FG proportions may not fit
    CBBE (in-game verification needed).
14. **Topology-sensitive features:** conform moves vertices but never changes topology, partitions/segments, UVs or
    normals' meaning; seams can open if duplicated seam vertices conform differently (check duplicated-vertex
    coincidence after conversion, like §12/§13).
15. **Proof that the intermediate is CBBE-compatible** before the physics stage: every outfit vertex near the body
    lies at the expected distance from the CBBE reference (distance distribution like a native CBBE outfit, no
    penetration beyond tolerance), CBBE sliders present and FG sliders absent, no ZeX bones weighted, topology/UV/
    segment invariants unchanged, and the analyzer re-classifies the intermediate as CBBE with HIGH confidence. Only
    then the existing CBBE → CBBE 3BBB physics stage and its strict validation run.

## 24. 0.6.1: real GUI acceptance findings (2026-10-01/02)

Packages converted through the GUI (Completed / No conversion needed / Needs attention): Clothes Pack 4 41/9/3,
Vtaw Overalls 5/1/2, Vtaw Utility Pack 1 22/4/11, Vtaw Wardrobe 4 23/11/4 (the generated group held exactly the 23
AUTO projects), Vtaw Wardrobe 5 60/16/22, Vtaw Wardrobe 6 73/14/11 (stress test; the author's ready-made 3BBB
conversion is used in game). NPC Underarmor Replacer verified in game; an unrelated ASA RobCo leveled-list patch was
distributing ASA outfits to Raiders (not a converter issue).

**Vtaw Overalls Belts — real defect, fixed.** Source OSP `VtawOverallsBelts.osp` lists `<Shape target="Metal Overalls ">
Metal Overalls </Shape>` (trailing space) and `OverallsBelts.nif` has exactly that shape (3 shapes: Belts Belly 3264 v,
`Metal Overalls ` 1552 v, Metall Belly belts2 6560 v). The OSP parser stripped the name → "missing from the NIF" → the
plan covered only Belts Belly; Outfit Studio still saved all three shapes → failed checks "expected outfit shape(s)
listed", "CBBE-3BBB body NOT embedded (NEW shapes ['Metal Overalls '])" and "analyzer KEEP (missing Metal Overalls
/butt)". BodySlide keeps the exact name; Outfit Studio 5.8.2 trims TargetMeshes entries (`SplitCommaSeparated`,
Automation.h), so such names are targeted with `TargetRegex` and `^…$` patterns (ECMAScript, icase, regex_search).

**Vtaw Wardrobe 5 Bodysuit V1/V2/V3/V6/V7 — correct strict rejection of a marginal part; not changed.** In batch
`20261001-223725` only these five of the attempted bodysuits were REVIEW (V4, V5 and all "No Arms" variants
VALIDATED). Each fails exactly one check: `arm: breast 3BBB bones present` — `LBreast_01_skin(26), RBreast_01_skin(26)`,
`_02`/`_03` 0. The main shape `v1` (8 699 v) passed every check (breast/butt/thigh, prediction agreement). The sleeve
shape `arm` (2 526 v) only touches the breast region at the armpit (relevance MEDIUM, 20 % of the reference region,
42 vertices predicted) and the planner's own full-copy prediction for it is exactly `LBreast_01_skin, RBreast_01_skin`
— Outfit Studio produced what was predicted. The rule "≥ half of a planned region's bones on ≥ 5 vertices" is stricter
than the planner's prediction for such marginal parts. Classification: (c) a planner/validator strictness mismatch,
not an automation/export defect and not a mesh defect. Kept unchanged (the same rule decided accepted CP5 cases such as
chain v1 / leggings v5); the message now names the failing check. A possible future rule ("every bone the prediction
expects for the region is weighted, and ≥ 50 % of the predicted region vertices") is a user decision.

**Product folder rename.** No absolute tool-root path in code, launchers or tests (fixture log text only). The saved
converter profile holds no path inside the tool folder (Outfit Studio source is the separate BodySlide 5.8.2 folder);
`runtime\…\Config.xml` is rewritten per session; `.venv` uses `python -m` (no console-script paths); batches resolve
their workspace relative to the tool root. '&' in the new name: launchers quote `%~dp0`; XML is written by
ElementTree (escaped); subprocesses get argument lists. ModOrganizer.ini has no reference to the tool folder.
