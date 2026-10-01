# Classification

Each BodySlide slider set gets exactly one action plus human-readable reasons, and optionally warnings and errors.

## Inputs

**Per shape** (`analysis/shape_metrics.py`, reference = installed CBBE 3BBB body):

- **Body detection.** A shape is a body if its vertex count matches an installed body and it lies on it, if it has a body name, ≥8000 vertices and a tall extent, or if it coincides with the reference surface (n ≥ 2000, median distance < 0.1, ≥ 90 % within 0.3). Body shapes are excluded from outfit decisions but reported (`includes_body_shape`).
- **Existing weights.** Which 3BBB physics bones carry weight (and on how many vertices), plus legacy physics bones and custom non-skeleton bones.
- **Per region (BREAST, BUTT, THIGH)**
  - *predicted*: the Outfit Studio weight copy is emulated (search radius 5, max 4 targets by default, both from the profile), giving the vertices that would receive ≥ 0.05 region weight and the fraction of the reference's region surface covered.
  - **relevance**: HIGH if ≥ 30 predicted vertices and coverage ≥ 50 %. MEDIUM if ≥ 15 vertices and (coverage ≥ 20 % or ≥ 20 % of the shape). LOW if ≥ 5 vertices. Otherwise NONE.
  - **fit** (radius-scaled): HIGH if ≥ 95 % of the region footprint is within the radius, median ≤ 0.5 r and p90 ≤ 0.6 r. MEDIUM if ≥ 80 % and median ≤ 0.6 r. Otherwise LOW.
  - **quality of existing 3BBB weights**: L/R balance vs the prediction, weights on the wrong side, weights where the reference has none, agreement with the predicted transfer, and weight islands vs predicted islands.
  - side coverage (L/R) and the predicted cross-side fraction (bridging geometry).

**Per project.** The body family and physics type (`classification/family.py`) come from independent signals. Weighted bones are strongest, then embedded body shapes, then slider vocabulary (overlap with the installed CBBE body; TWB/BodyTalk/Fusion Girl signature sliders), with names as weak evidence. Cross-project checks add same-output groups, 3BBB projects in *other* files superseding an original, same-file 3BBB variants, and duplicate set names.

## Decision order (`classification/rules.py`)

1. **ERROR**: no readable shapes (malformed OSP, missing/unreadable ShapeData NIF, no `<SourceFile>`).
2. **UNSUPPORTED**: Fusion Girl, BodyTalk/male, non-Fallout-4 NIF.
3. **SKIP**: the project contains only body/reference shapes.
4. **3BBB physics present** (on outfit shapes or on an embedded body)
   - any quality issue → **REVIEW** ("bones present does not mean good weights"), including 3BBB weights on geometry that is not over that region;
   - not declared as a 3BBB conversion (no `3BBB`/`3B` in the project, mod or file names) *and* a garment risk → **REVIEW** (the CP5 jacket case: a faithful copy that still behaves badly);
   - a core region (breast/butt) relevant but unweighted → **REVIEW** (partial conversion); a missing thigh only adds a warning;
   - otherwise → **KEEP** (never re-converted).
5. **Unknown family**: no relevant region → SKIP, otherwise → REVIEW.
6. **No reference available** → REVIEW.
7. **Superseded** by a 3BBB project in another project file that builds the same output → **SKIP**.
8. **No relevant region**
   - but the geometry lies over a region beyond the search radius (≥ 30 footprint vertices, < 50 % within radius) → **REVIEW** (a copy would add nothing: capes, loose coats);
   - otherwise → **SKIP**.
9. **Candidate checks** → **REVIEW** if any apply: loose fit (LOW) over a relevant region, predicted cross-side weights > 5 %, custom bones, legacy CBBE Physics cloth bones (must be replaced, not merged), no sliders, geometry more than 8 units from the body (p90), or a garment risk:
   - outerwear (`jacket, coat, cape, cloak, robe, poncho, hoodie, duster, vest, …`)
   - rigid armor/equipment (`armor, plate, helmet, gauntlet, …`)
   - loose/open clothing (`skirt, dress, apron, …`), only when the fit is not tight
   - rigid accessories over physics regions (`belt, strap, buckle, chain, cuff, plug, harness, clamp, zipper, pocket, …`)
   - footwear reaching a physics region (`boots, heels, shoes, …`)
   - layered outfits (more than two shapes over physics regions)

   Otherwise → **CONVERT** (confidence = the lowest fit over the relevant regions; with no issues this is HIGH).

Name hints are matched on whole word tokens (CamelCase and separators are split), so "Wardrobe" does not match "robe".

## Report columns

| Column | Meaning |
| --- | --- |
| Body | family + physics label, e.g. `CBBE`, `CBBE 3BBB`, `TWB 3BBB`, `CBBE Physics (legacy)`, `BodyTalk` |
| Breast / Butt | highest relevance of that region over the outfit shapes (Thigh is in the details) |
| 3BBB | `YES` all relevant core regions weighted · `PARTIAL` some missing · `NO` none |
| Confidence | geometry confidence of the decision (HIGH/MEDIUM/LOW) |

## Regression expectations (development installation)

| Project | Expected | Why |
| --- | --- | --- |
| MM69 Raider Punk Top V2 - CBBE | CONVERT | tight top over the breasts (median 1.6, 100 % within radius), no 3BBB weights; a same-file 3BBB TEST variant is reported |
| MM69 Raider Punk Top V2 - 3BBB TEST | KEEP | 3BBB weighted; embeds the 3BBB body (copy reference was on) |
| Vtaw BlackMamba 3BBB TWB (4 sets) | KEEP | TWB 3BBB; the original Vtaw BlackMamba sets are SKIP (superseded) |
| Vtaw Wardrobe 8 TWB-3B conversions | KEEP | TWB 3BBB |
| ASA 0.2 - 3BBB (CBBE) Conversion | KEEP | CBBE 3BBB; the ASA Physics mod is fully overridden |
| CP5_DonEb14n jacket v1–v4 | REVIEW | outerwear; v3 carries undeclared in-place 3BBB weights |

`tools/check_regressions.py` checks these against a live installation or a saved report.
