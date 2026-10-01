# Fallout 4 Body & Physics Converter

Version **0.6.1** · Windows · for Fallout 4 with Mod Organizer 2, BodySlide and Outfit Studio

---

## 1. What it is

Fallout 4 Body & Physics Converter is a Windows tool for BodySlide outfit packages. It looks at every outfit
project in a package and converts the suitable ones to **CBBE 3BBB physics**: breast, butt and thigh physics bones
for physics mods such as OCBP.

- Your **source mods are never modified**. The tool reads them and works on copies.
- Converted projects go into a **separate MO2 mod**, `CBBE 3BBB Auto Conversions`, as new BodySlide projects named
  `<original> - 3BBB AUTO`. Each package gets its own BodySlide group, `CBBE 3BBB Auto Conversions - <package>`.
- It drives **Outfit Studio** on its own: the window opens and runs by itself, and you don't click anything in it.
- Every converted outfit goes through **strict validation**. A result that cannot be verified is rejected and the
  original stays as it is.
- You build the converted outfits in **BodySlide** yourself. The tool tells you exactly what to build, and afterwards
  it can **check the build**.

## 2. Current support

| Source body → target | Status |
| --- | --- |
| **CBBE → CBBE 3BBB** | **Supported.** This is the main, production route. |
| CBBE 3BBB (already converted) | **Already compatible.** Recognised and left as it is. |
| TWB 3BBB, or 3BBB on a body the tool cannot name | **Already compatible.** Left as it is. |
| TWB (without 3BBB) → CBBE 3BBB | Converted the same way as CBBE outfits, because TWB is CBBE-derived. This route has not been accepted separately in game. |
| **Fusion Girl** | **Detected, but not converted.** Fusion Girl → CBBE conversion is not implemented. These outfits are listed under *Needs attention*. |
| BodyTalk / male bodies | Not supported. |
| Unknown or undetermined body | Never converted automatically. |
| Outfits with their own built-in body | Usually *Needs attention* ("contains its own body"): convert them by hand. |

Not every outfit converts. Accessories, belts, holsters, boots, loose or layered parts and some full-body
outfits are deliberately left alone or end up under *Needs attention*. See §7 and §10.

## 3. Requirements

- **Windows 10/11** and **Fallout 4**.
- **Mod Organizer 2.** Every tested workflow uses MO2: the tool reads your MO2 instance and profile, the output mod is an
  MO2 mod, and BodySlide is started through MO2.
- **CBBE** and **CBBE 3BBB Body** installed in MO2. The tool reads the installed *CBBE 3BBB Body* BodySlide project and
  uses it as the target body.
- **BodySlide and Outfit Studio** installed in MO2 as usual, for building.
- A separate copy of **BodySlide and Outfit Studio 5.8.x**, extracted to its own folder outside MO2 and the game
  (for example `T:\Fallout 4 Tools\BodySlide 5.8.2`). The tool needs Outfit Studio 5.8 or newer; 5.8.2 is the
  tested version. It makes a private copy for itself and never changes your installed BodySlide.
- A **3BBB physics framework** in game, such as OCBP with a 3BBB preset. The converter does not install or
  configure in-game physics.
- **Python 3.10 or newer**, used only to run the tool. `setup_env.bat` creates a private `.venv` inside the tool folder
  with numpy, scipy and PySide6.

## 4. Installation

1. Put the tool folder anywhere **outside** the game folder, the MO2 folder and the MO2 mods folder (the tool refuses
   to run from inside them), for example:
   `T:\Fallout 4 Tools\Fallout 4 Body & Physics Converter`
2. Run `setup_env.bat` once. It creates `.venv` and installs the dependencies. Nothing outside the folder changes.
3. Start the tool with `run_gui.bat`.
4. First launch: click **Settings / Advanced** and set:
   - your **Fallout 4** folder;
   - your **MO2** folder (the one that contains `ModOrganizer.ini`);
   - the **MO2 profile**. If you leave it empty, the profile currently selected in MO2 is used;
   - the **Outfit Studio 5.8+** folder from §3.

   These settings are saved as a **converter profile** (for example `Default`) in `config\profiles.json`. Then click
   **Detect again**: the main window shows Game, Mod Organizer, Reference body (CBBE 3BBB), Output and Outfit Studio
   with ✓ marks.

The tool keeps all its own files inside its folder:

| Folder | Contents |
| --- | --- |
| `config` | profiles |
| `work` | conversion workspaces and checkpoints |
| `runtime` | the private Outfit Studio copy |
| `reports` | analysis, package and build reports |
| `logs` | application log |
| `cache` | analysis cache |

The folder can be renamed or moved: launchers and paths are relative to the folder, and saved paths that pointed
inside the old folder are updated automatically.

## 5. Quick start

1. Close BodySlide and Outfit Studio, then start `run_gui.bat`.
2. **Choose outfit mod**: pick the package, for example *Clothes Pack 4 by DonEb14n*.
3. Leave **Source Body = Auto Detect** and **Target Body = CBBE 3BBB**.
4. Click **Analyze** and review the counts: *Ready to convert*, *Will try in All Possible*, *Already compatible*,
   *No conversion needed*, *Cannot safely process*. The detected bodies and routes are shown too.
5. Click **Convert Safe** or **Convert All Possible** (§6). Outfit Studio opens and works on its own; don't use it
   while it runs. Large packages run in several sessions.
6. Read the result: **Completed**, **No conversion needed**, **Needs attention / original kept**. **View Problems**
   shows the reason for each outfit, including the strict check that failed.
7. Click **Build Converted Outfits** and follow the dialog (§9). In short: in BodySlide, select the exact group
   `CBBE 3BBB Auto Conversions - <package>`, choose your preset, turn **Build Morphs ON**, then **Batch Build** →
   **Build**.
8. Back in the tool, click **Check the build**.
9. Test the outfits in game. If one has no physics in game, report it with **Report in-game problem…**.

## 6. Safe vs. Convert All Possible

- **Safe** converts only the outfits the analysis rates as high-confidence fits.
- **Convert All Possible** also tries lower-confidence candidates that can still be validated.

All Possible does **not** mean "force everything". Both modes use the same strict validation. A result that fails
validation is rejected, nothing is installed for it, and the original is kept.

## 7. What the results mean

| Result | Meaning |
| --- | --- |
| **Completed** | Converted, validated and installed into `CBBE 3BBB Auto Conversions`. |
| **Already compatible** | Already carries 3BBB physics (or is a native 3BBB project). Nothing to do. |
| **No conversion needed** | No breast, butt or thigh physics area under this item (gloves, boots, jewellery…). |
| **Needs attention / original kept** | Not converted. The original is untouched. Common reasons are below. |

Common *Needs attention* reasons:

- **physics conversion unavailable**: the automatic weight transfer would not be safe for part of the outfit.
  Typical causes are parts too far from the body, belts, harnesses, holsters or bags.
- **physics could not be verified**: the converted result could not be proven correct, so it was rejected.
- **expected physics bones missing (rejected)**: part of the outfit did not get all the physics bones the strict
  validation requires.
- **contains its own body (needs manual conversion)**: the outfit includes a body mesh. It is not converted
  automatically.
- **conversion result rejected**: the converted result did not pass a strict structural check, for example a changed
  shape or an unplanned extra part.
- **already has its own physics weights (kept as is)**: existing 3BBB-style weights are never overwritten.
- **Made for Fusion Girl / made for another body**: not a CBBE outfit (§2).
- **does not match the chosen source body**: you chose a Source Body other than *Auto Detect*, and this outfit was
  detected as another body. A chosen source only *confirms* the detection; it never reinterprets an outfit.
- **not tried in Safe mode**: *Convert All Possible* may try it.

Any outfit under *Needs attention* is safe to leave as it is. You can convert it by hand or use an author-made
conversion.

## 8. Output safety

- Source mods are read-only for the tool; it checks this with SHA-256 hashes before and after each conversion.
- Everything generated goes into `CBBE 3BBB Auto Conversions` with a `manifest.json` that lists every file the tool
  owns. Files it did not create are never overwritten, including a generated project with no manifest record.
- Each package gets an exact BodySlide group, `CBBE 3BBB Auto Conversions - <package>`, containing only the
  generated `- 3BBB AUTO` projects.
- Generated and original projects build the **same game mesh files**. Build **only the generated AUTO group**, and do
  not batch-build the original package afterwards: that would overwrite the converted meshes.

## 9. Building in BodySlide

**Build Converted Outfits** shows the exact group, the preset and Build Morphs, and offers:

- **Open BodySlide (via Mod Organizer 2)**. MO2 may first show its usual dialogs. If BodySlide does not come to the
  front, look for it on the taskbar.
- **Copy group name**.
- **Check the build**.

In BodySlide:

1. Select the group `CBBE 3BBB Auto Conversions - <package>` in the **group filter**. Don't rely on the text search
   alone, because it also shows the original projects.
2. Choose the CBBE body preset you play with. *CBBE Curvy* was used during testing; any CBBE preset works.
3. Turn **Build Morphs ON**.
4. Click **Batch Build** → **Build**. If BodySlide asks which project should build a file, choose the **- 3BBB AUTO**
   one.

Then click **Check the build**, or run `verify-package-build`. Each converted outfit is reported as one of:

| Status | Meaning |
| --- | --- |
| built correctly | The game mesh MO2 serves was built after the conversion, and its bones and weights match the generated project. A file simply existing is not enough. |
| not built yet | No built mesh was found. |
| built before the conversion or from the original project | Rebuild the AUTO group. |
| built mesh does not match | Rebuild the AUTO group. |
| could not be checked | A file is missing or unreadable. |

## 10. Limitations

- Not every clothing item needs physics. Accessories, belts, holsters, boots and similar items often (correctly)
  stay unconverted.
- Outfits with a built-in body usually need a manual conversion.
- Strict validation can reject output that looks fine. That is intentional: the original is kept.
- Some package structures cannot be converted safely by automation.
- For some packages a ready-made author conversion is the better choice. Example: *Vtaw Wardrobe 6 - 3BBB (CBBE)
  Conversion* with its own group *Vtaw Wardrobe 6*. Don't build both conversions of the same outfits.
- Fusion Girl conversion is not implemented. Male / BodyTalk conversion is not supported.

## 11. Troubleshooting

- **BodySlide shows both the original and the AUTO projects.** Select the exact group `CBBE 3BBB Auto Conversions -
  <package>` in the group filter instead of searching by outfit name.
- **MO2 shows a dialog when BodySlide starts** (secondary process, Steam). This is normal MO2 behaviour; answer it as
  usual.
- **An outfit was rejected.** Open **View Problems** (tick *Technical Details* for the full list of checks). The
  original is safe; nothing was installed for that outfit.
- **The game still shows the old mesh.**
  - Make sure `CBBE 3BBB Auto Conversions` is enabled in MO2.
  - Check MO2 mod priority: another mod may provide the same mesh.
  - Check that no original project was built afterwards over the same output path.
  - Run **Check the build**: it names the mod whose mesh the game actually loads.
- **NPCs wear unexpected outfits.** Outfit distribution mods (leveled-list patchers and similar) decide what NPCs wear.
  That is outside this tool.
- **Outfit Studio could not continue on its own.** The tool stops safely and offers *Retry automatically*, *Continue
  manually* or *Stop for now*. Interrupted packages can be resumed with **Resume**.

## 12. Real-world validation

The converter has been run against real installed packages through the GUI and the command line. Mixed or unsupported
outfits in them were deliberately preserved:

- Clothes Pack 4 by DonEb14n: 41 completed, 9 not needed, 3 need attention
- Clothes Pack 5 CBBE BodySlide: the vast majority works in game
- Vtaw Overalls: 5 / 1 / 2
- Vtaw Utility Pack 1: 22 / 4 / 11
- Vtaw Wardrobe 4: 23 / 11 / 4. The generated group contained exactly the 23 AUTO projects.
- Vtaw Wardrobe 5: 60 / 16 / 22
- Vtaw Wardrobe 6: 73 / 14 / 11, used as a stress test
- Vtaw Wardrobe 10: fully unattended Outfit Studio, including cloth physics data
- Raider Punk: the first in-game accepted conversions

This shows how the tool behaves on real packages. It is not a promise that every project in every package converts.

## 13. Command line

`run_batch.bat` runs the same engine as the GUI. Two profile options mean different things:

- `--profile NAME` is the **converter profile** (for example `Default`, saved by the GUI).
- `--mo2-profile NAME` is the **Mod Organizer 2 profile** (for example `Fix F`).

```bat
run_batch.bat --help
run_batch.bat --version
run_batch.bat body-routes
run_batch.bat analyze-package      --profile Default --package "Clothes Pack 4 by DonEb14n" [--details]
                                   [--source-body auto|cbbe|fusion_girl|twb] [--target-body cbbe_3bbb]
run_batch.bat convert-package      --profile Default --package "Clothes Pack 4 by DonEb14n" --yes
                                   [--mode safe|all-possible] [--no-commit] [--max-per-session N]
                                   [--source-body auto|cbbe|fusion_girl|twb] [--target-body cbbe_3bbb]
                                   [--level normal|detailed|debug] [--mo2-profile "Fix F"]
run_batch.bat resume-batch         --profile Default --batch <batch id> --yes [--commit] [--manual | --unattended]
run_batch.bat package-report       --profile Default --batch <batch id> [--verify]
run_batch.bat verify-package-build --profile Default --package "Clothes Pack 4 by DonEb14n"
run_batch.bat report-runtime-issue --profile Default --project "<project or generated project>" --state FAIL|PARTIAL|PASS [--note "..."]
run_batch.bat export-diagnostics   --profile Default [--batch <batch id>] [--out <zip>]
```

`--source-body` defaults to `auto` and `--target-body` to `cbbe_3bbb`. Choosing `fusion_girl` is refused before
anything starts, because that route is not implemented. `convert-package` needs `--yes`; with `--no-commit` it
converts and validates without installing. Run `run_batch.bat <command> --help` to see every option.

## 14. Safety and recovery

- **Originals are never modified.** The generated output lives only in `CBBE 3BBB Auto Conversions`.
- **To stop using the conversions:**
  1. Disable `CBBE 3BBB Auto Conversions` in MO2.
  2. Remove the meshes BodySlide built from the AUTO group. They are wherever your BodySlide build writes, usually
     MO2's *Overwrite*. Alternatively, rebuild the original projects.
  3. Do not delete individual files inside the output mod by hand; its `manifest.json` describes what it contains.
- **Interruptions.** Conversions are checkpointed per project. **Resume** in the GUI (or `resume-batch`) continues
  with the unfinished outfits.
- **Logs and diagnostics:**
  - `logs\app.log`;
  - package reports in `reports\packages`;
  - build checks in `reports\builds`;
  - **Export Diagnostic Report** (or `export-diagnostics`) writes one ZIP with user paths anonymised.
- **Back up** your MO2 profile and `CBBE 3BBB Auto Conversions` before large conversions. When updating the tool,
  replace the program files and keep `config`, `work` and `reports`.

## 15. Version and status

**0.6.1.** CBBE → CBBE 3BBB is the production route; the other routes are as listed in §2. Fusion Girl is detected
but not converted.

Developer documentation is in `docs/`: architecture, research, testing, the batch engine, and `DEVELOPMENT_HISTORY.md`
(the former engineering README). The changelog is in `CHANGELOG.md`.
