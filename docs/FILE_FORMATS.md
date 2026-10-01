# File formats used by the analyzer

All files are read-only.

## MO2 (portable instance)

`ModOrganizer.ini` (QSettings INI) keys used:

| Section/key | Use |
| --- | --- |
| `[General] gameName`, `gamePath` | game name, game folder (values may be `@ByteArray(...)` and use `\\` escapes) |
| `[General] selected_profile` | default MO2 profile |
| `[General] version` | MO2 version (informational) |
| `[Settings] base_directory`, `mod_directory`, `profiles_directory`, `overwrite_directory` | folder overrides (`%BASE_DIR%` supported); defaults are `mods`, `profiles`, `overwrite` |
| `[customExecutables] N\title`, `N\binary` | informational (BodySlide/Outfit Studio entries) |

`profiles/<name>/modlist.txt` lists one entry per line: `+Name` enabled, `-Name` disabled, `*Name` unmanaged (DLC/CC). **The first line has the highest priority.** `*_separator` entries are separators. Virtual file resolution: `overwrite` > mods by priority > game `Data`. Disabled mods are optional and ranked below all of these.

## BodySlide folder (`Data/Tools/BodySlide`)

| Path | Content |
| --- | --- |
| `BodySlide x64.exe`, `OutfitStudio x64.exe` | version from the PE `VS_FIXEDFILEINFO` |
| `Config.xml` | `<TargetGame>` (3 = Fallout 4), `<Anim><DefaultSkeletonReference>` |
| `BodySlide.xml` | UI state, e.g. `<SelectedPreset>` |
| `RefTemplates.xml`, `RefTemplates/*.xml` | `<Template sourcefile="SliderSets\X.osp" set="Set" shape="Shape">Display name</Template>` |
| `SliderSets/**/*.osp` (and `.xml`) | project files (see below), loaded recursively |
| `ShapeData/<DataFolder>/<SourceFile>.nif` | project geometry; `.osd` slider data next to it (not parsed) |
| `SliderGroups/*.xml`, `SliderPresets/*.xml` | groups/presets (informational) |
| `res/skeleton_fo4.nif` | vanilla skeleton, used to separate physics bones from vanilla bones |

## OSP project file

```xml
<SliderSetInfo version="1">
  <SliderSet name="MM69 Raider Punk Top V2 - CBBE">
    <DataFolder>MM69 Raider Punk</DataFolder>          <!-- ShapeData subfolder -->
    <SourceFile>raiderPunk_top_v2.nif</SourceFile>     <!-- geometry NIF -->
    <OutputPath>meshes\MM69 Raider Punk</OutputPath>    <!-- relative to Data -->
    <OutputFile GenWeights="true">raiderPunk_top_v2</OutputFile>  <!-- _0/_1 when GenWeights -->
    <Shape target="TopShape">TopShape</Shape>           <!-- NIF shape name; target = slider data key -->
    <Shape target="CBBE" DataFolder="CBBE">CBBE</Shape> <!-- DataFolder attr relocates only .osd data -->
    <Slider name="Breasts" invert="false" small="0" big="100">
      <Data name="TopShapeBreasts" target="TopShape">raiderPunk.osd\TopShapeBreasts</Data>
    </Slider>
    <Slider name="Zap Sleeves" zap="true" .../>          <!-- zaps -->
  </SliderSet>
</SliderSetInfo>
```

One file can hold several sets, and different sets or files can build the same output mesh, which is reported. Files that aren't valid UTF-8 are re-read as cp1252.

## NIF (Fallout 4)

Header `Gamebryo File Format, Version 20.2.0.7`, user version 12, **BS version 130**. Parsed blocks:

- `NiNode` family: name, transform, children (for the parent map and world transforms).
- `BSTriShape`, `BSSubIndexTriShape`, `BSMeshLODTriShape`, `BSDynamicTriShape`: the transform, skin ref, and `BSVertexDesc` (size = `(desc & 0xF)*4`, flags = `desc >> 44`, skin offset = `((desc >> 28) & 0xF)*4`). Also vertex data (half or full precision positions; skinned vertices carry 4 half-float weights + 4 byte bone indices) and triangles (u16).
- `BSSkin::Instance`: skeleton root, bone-data ref, bone refs.
- `BSSkin::BoneData`: per bone, bounding sphere, rotation, translation, scale.

Skinned vertices are in skin space: skin→global = `world(bone) · BoneData(bone)`. The median over bones is used, and a spread above 5 units is reported. Legacy `NiTriShape` blocks are listed as unsupported, and other blocks are skipped by size. Files that are not Fallout 4 (for example Skyrim SE, BS 100) are marked `UNSUPPORTED`.

## Tool files

- `config/profiles.json`: `{"schema": 1, "last_profile": "...", "profiles": [Profile...]}`, see `profiles/store.py`. `output_mod_enabled` is always forced to `false` in Phase 1.
- `reports/latest.json`: `ScanReport` (tool/analyzer version, installation summary, reference, settings, stats, notes, projects[] with shapes and region metrics).
- `reports/latest.csv`: one row per project, `utf-8-sig` for Excel.
- `cache/analysis.sqlite`: table `results(key, fingerprint, payload)`.
