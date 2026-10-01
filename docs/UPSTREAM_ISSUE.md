# Upstream report draft: headless `CopyBoneWeights` does nothing (Outfit Studio 5.8.0–5.8.2)

*Ready to post at https://github.com/ousnius/BodySlide-and-Outfit-Studio/issues. The patch in `upstream/outfitstudio-headless-copyboneweights.patch` is offered under the project's GPL-3.0.*

**Title:** Automation: CopyBoneWeights has no effect in headless mode (`-a`)

**Version:** 5.8.2 (also current `main`, ee268496).

**Steps**
1. Make an automation script: `ClearProject`, `LoadReference` (for example the CBBE 3BBB Body), `AddProject` (any outfit), `CopyBoneWeights` (TargetMeshes = the outfit shape, radius 5, max results 4), `SaveProject` (`AutoCopyRef=false`).
2. Run it headless: `OutfitStudio -a <script>`. The exit code is 0 and the log shows "Copying bone weights to '…'".
3. The saved ShapeData NIF has the original weights. None of the reference's bones were added.
4. Running the same script from *File → Open Automation → Execute* with the window open does copy the weights.

**Cause**
In headless mode the frame is never shown, so `wxGLPanel::OnShown` never runs and `extInitialized` stays false. `MeshFromProj` therefore creates no `Mesh` objects. `ExecuteStepCopyBoneWeights` computes the weights into the undo state, but `OutfitStudioFrame::ActiveShapesUpdated` applies weight states only for shapes that have a GL mesh (`if (!m) continue;`), so the result is discarded.

**Proposed fix** (tested on a Linux build of 5.8.2: the headless result is bit-identical to the windowed run)
In the `UndoType::Weight` branch of `ActiveShapesUpdated`, fall back to `uss.shapeName` when there is no mesh, and skip only the pose/mesh refresh:

```diff
-				if (!m)
+				// without a GL mesh (headless automation) still apply the weights
+				const std::string shapeName = m ? m->shapeName : uss.shapeName;
+				if (!m && !project->GetWorkNif()->FindBlockByName<NiShape>(shapeName))
 					continue;
 ...
-					project->GetWorkAnim()->AddShapeBone(m->shapeName, bw.boneName);
-					auto weights = project->GetWorkAnim()->GetWeightsPtr(m->shapeName, bw.boneName);
+					project->GetWorkAnim()->AddShapeBone(shapeName, bw.boneName);
+					auto weights = project->GetWorkAnim()->GetWeightsPtr(shapeName, bw.boneName);
 ...
-				if (project->bPose) {
+				if (m && project->bPose) {
```

Other steps that go through `ActiveShapesUpdated` (color, alpha, slider edits) or need GL meshes may be affected in the same way. This was not investigated.


---

# Upstream report draft 2: cloth data from earlier projects is offered at every later save (Outfit Studio 5.8.2)

**Title:** Automation/ClearProject: cloth data (BSClothExtraData) of previously loaded files persists and is offered on every later SaveProject

**Steps**
1. Write an automation script with two parts: `ClearProject, LoadReference, AddProject A (a NIF with BSClothExtraData), SaveProject`, then `ClearProject, LoadReference, AddProject B (no cloth data), SaveProject`.
2. Execute it. At A's save, "Choose cloth data" lists A's file (expected). At B's save it lists A's file again, although A was cleared.

**Cause**
`OutfitProject::clothData` is filled in `LoadReference`/`AddNif` and read in `ChooseClothData`, but `ClearReference`/`ClearOutfit`/`ResetProject` never clear it. Only File > New replaces the whole `OutfitProject`. In headless mode the dialog is modal and blocks the run.

**Proposed fix**
Clear `clothData` in `OutfitProject::ClearOutfit` (or in `ResetAndClearProject`). Add an optional `SaveProject` parameter for the cloth choice (e.g. `ClothData = own|none|all`), so automation never needs the dialog.

---

# Upstream report draft 3: AddProject loses local slider data of shapes with a DataFolder attribute (Outfit Studio 5.8.2)

**Title:** AddProject/SaveProject: local slider data of a shape with a `DataFolder` attribute is saved as a reference to a non-existent folder

**Steps**
1. Take a project whose slider data is local (stored in the set's own `ShapeData\<set folder>\<set>.osd`) and whose shape (for example an embedded `CBBE` body) has `DataFolder="CBBE"` in the OSP.
2. Automation: `ClearProject`, `LoadReference` (any body), `AddProject` (that project), `SaveProject` (AutoCopyRef=false, new name).
3. The saved OSP keeps `DataFolder="CBBE"` for that shape and writes no local data for it. BodySlide then looks in `ShapeData\CBBE\<set>.osd`, which does not exist, and the shape does not morph.

**Cause**
The merged slider data is marked non-local while the shape's DataFolder attribute is kept, so at save time the data is neither written nor resolvable. `ImportSliderData` (→ `SetSliderFromDiff` → `EnsureSliderDataLocal`) before saving makes the data local again and is the workaround the tool uses.

**Proposed fix**
When `AddProject` merges data that was local in its source project, keep it local (or copy it into the new project on save) instead of relying on the shape's DataFolder attribute.
