# Cardboard Face Mask (Skyrim Special Edition)

Every adult humanoid NPC in Skyrim wears a cardboard cut-out of my face: a thin slab cut along
the outline of the head in `input/IMG_2117.PNG`, the photo printed on the front, corrugated
cardboard edges, a brown kraft back with masking-tape strips, and a twine string that wraps
awkwardly around the back of the head. Personal-use joke mod; no scripts.

![Human mask on the vanilla male head, seven views](docs/renders/render_human_m.png)

*Offline preview rendered with Blender from a NIF built by this pipeline, on Skyrim's vanilla male
head (`src/render_check.py`; the image header names the NIF's sha1, so you can tell whether it
matches the NIF in `dist/`). This is not an in-game screenshot.*

| Khajiit/Argonian variant | Cut-out modes (`silhouette_mode`) |
|---|---|
| ![Beast mask on the vanilla Argonian head](docs/renders/render_beast_m.png) | ![The three silhouette modes](docs/renders/silhouette_modes.png) |

More previews: `docs/renders/textures_atlas.png` (the texture atlas), `fit_mask_*.png` (fit on each
head), `render_vanilla_helmet.png` (a vanilla helmet rendered the same way, as a reference).

> **Status.** The package is built and checked offline (meshes, textures, plugin, SPID line,
> zip; see "How it is checked" below). It has **not yet been tried in the game**: there is no
> Skyrim install where it was built. Run the checklist in [docs/TESTING.md](docs/TESTING.md) the
> first time you install it.

## How it works

* `CardboardFaceMask.esp` (ESL-flagged, master `Skyrim.esm` only) adds one clothing item,
  "Cardboard Face Mask" (`FaceMaskArmor`, local FormID 0x800, weight 0, value 0, armor 0) that uses
  biped slot 55, so it can be worn together with helmets and hoods. Two armatures pick the human or
  the Khajiit/Argonian mesh, male and female.
* [Spell Perk Item Distributor](https://www.nexusmods.com/skyrimspecialedition/mods/36869) (SPID)
  reads `CardboardFaceMask_DISTR.ini` and puts one mask into the inventory of every NPC that has the
  `ActorTypeNPC` keyword and is not a child:
  `Item = 0x800~CardboardFaceMask.esp|ActorTypeNPC|NONE|NONE|-C|1|100`.
  NPCs are then expected to wear it in the otherwise unused slot 55. That last step is the game's
  own behaviour, not SPID's (SPID only adds the item to the inventory), and it has **not been
  verified in game yet**: it is step 3 of [docs/TESTING.md](docs/TESTING.md); background in
  [docs/SPID_NOTES.md](docs/SPID_NOTES.md) section 9.
* The player never gets one automatically (SPID skips the player); try it on with the console.
* The meshes are rigidly skinned to the head bone (`NPC Head [Head]`), so the mask follows the head.

## Requirements

| What | Version | Notes |
|---|---|---|
| Skyrim Special Edition (Steam) | runtime 1.7.104 | the version these notes were written for |
| [SKSE64](https://skse.silverlock.org/) | 2.3.1 (the "Anniversary Edition" build for 1.7.104) | download link on skse.silverlock.org points to [Nexus](https://www.nexusmods.com/skyrimspecialedition/mods/30379?tab=files); start the game with `skse64_loader.exe` |
| [Address Library for SKSE Plugins](https://www.nexusmods.com/skyrimspecialedition/mods/32444) | the release for 1.7.104 (you use v13) | SKSE needs `Data\SKSE\Plugins\versionlib-1-7-104-0.bin` |
| [Spell Perk Item Distributor](https://www.nexusmods.com/skyrimspecialedition/mods/36869) | 7.3.3 is the newest source release | SE/AE build (not the VR one) |

powerofthree's Tweaks is **not** needed (the ini uses a FormID, not an EditorID). Why SPID 7.3.3
loads on 1.7.104, with source citations: [docs/SPID_NOTES.md](docs/SPID_NOTES.md) section 11.

## Install

The build output is `dist/CardboardFaceMask.zip` (zip root = contents of the `Data` folder) and the
same files unpacked in `dist/CardboardFaceMask/`:

```
CardboardFaceMask.esp
CardboardFaceMask_DISTR.ini
meshes/CardboardFaceMask/mask_human_m.nif, mask_human_f.nif, mask_beast_m.nif, mask_beast_f.nif
textures/CardboardFaceMask/mask_d.dds, mask_n.dds, twine_d.dds, twine_n.dds
```

Install SKSE, Address Library and SPID first, then one of:

* **Mod Organizer 2**: *Install a new mod from an archive* -> pick `CardboardFaceMask.zip` -> OK.
  Tick the mod in the left pane and make sure `CardboardFaceMask.esp` is ticked in the right pane
  (*Plugins*). Launch *SKSE* from MO2.
* **Vortex**: drag `CardboardFaceMask.zip` onto the *Mods* page (or *Install From File*), enable it,
  let Vortex deploy, and check that `CardboardFaceMask.esp` is enabled on the *Plugins* page.
* **Manual**: copy everything inside `dist/CardboardFaceMask/` (or the zip's contents) into the game's
  `Data` folder, keeping the `meshes/` and `textures/` sub-folders. The `_DISTR.ini` must sit directly
  in `Data`, not in a sub-folder. Then activate the plugin: add the line `*CardboardFaceMask.esp`
  to `Plugins.txt` (Windows: `%LOCALAPPDATA%\Skyrim Special Edition\Plugins.txt`; in a CrossOver
  bottle the same file is under `drive_c/users/<user>/AppData/Local/Skyrim Special Edition/`), or use
  a mod manager.
  * Windows PC (default Steam library):
    `C:\Program Files (x86)\Steam\steamapps\common\Skyrim Special Edition\Data`
  * Mac with CrossOver:
    `~/Library/Application Support/CrossOver/Bottles/<Bottle>/drive_c/Program Files (x86)/Steam/steamapps/common/Skyrim Special Edition/Data`
    (in Finder: *Go -> Go to Folder...*, paste the path with your bottle name).

The plugin is ESL-flagged, so it does not use one of the 254 regular plugin slots.

## Configuration

Edit `config.yaml` and rebuild. Most options only need `.venv/bin/python src/build_all.py --skip-ml`,
which reuses the cut-out outline from the last segmentation run. **`silhouette_mode` and
`source_image` change the outline itself and need the segment stage**: run
`.venv/bin/python src/build_all.py` without `--skip-ml` (`build_all.py` stops with an error if the
outline does not match `config.yaml`, and so does the package stage). Options:

| Key | Default | Effect |
|---|---|---|
| `source_image` | `input/IMG_2117.PNG` | the photo the mask is cut from (needs the segment stage) |
| `silhouette_mode` | `face_hair_glasses` | outline of the cut-out: `face_only`, `face_hair`, `face_hair_glasses` (picture above) |
| `thickness_units` | 0.3 | cardboard thickness (1 unit = 1.43 cm) |
| `forward_offset_units` | 1.8 | gap between the nose tip and the back of the mask |
| `bend_radius_units` | 24 | how much the sheet curves around the face; 0 = flat |
| `size_factor` | 1.08 | photo chin-to-crown height relative to the NPC's |
| `scale_human`, `scale_female`, `scale_beast` | 1.0, 0.96, 1.25 | per-mesh scale (female is applied on top of human) |
| `edge_wobble_units` | 0.04 | hand-cut jitter of the outline; 0 = clean die-cut |
| `string_radius_units`, `string_sag`, `string_asymmetry` | 0.06, 0.6, 0.35 | twine thickness, droop at the back, lopsidedness |
| `tape_strip` | true | masking-tape strips on the back |
| `texture_size`, `twine_texture_size` | 2048, 512 | texture resolution (powers of two) |
| `plugin_name` | `CardboardFaceMask.esp` | plugin file name (the ini follows it) |
| `slot` | 55 | biped slot |
| `non_playable` | false | true hides the mask from inventories and loot; test that NPCs still wear it before relying on it |
| `exclude_children` | true | adds the SPID trait `-C` |
| `chance_percent` | 100 | share of matching NPCs that get a mask (0 < chance <= 100). Below 100 the ini gets SPID's `!` suffix (e.g. `25!`), so the same NPCs keep their masks every time you load that character; without `!` SPID re-rolls on every game launch |
| `distribute_to_player` | false | has no effect: SPID never gives items to the player |

Distribution-only changes (chance, excluding races or named NPCs) can also be made by editing the
installed `CardboardFaceMask_DISTR.ini`; its comment header explains every field and contains
ready-made example lines. Keep exactly one active `Item =` line (several matching lines add up to
several masks).

## In-game test checklist (short version)

Full plan with expected results: [docs/TESTING.md](docs/TESTING.md). Open the console with the `~` key.

1. SPID log (`Documents\My Games\Skyrim Special Edition\SKSE\po3_SpellPerkItemDistributor.log`,
   inside the bottle's `drive_c/users/<user>/Documents/` under CrossOver) lists
   `CardboardFaceMask_DISTR.ini`, has no `Failed to parse entry` or `FAIL` line mentioning
   `CardboardFaceMask`, and says `Registered x/y Items` with x = y (1/1 if no other mod
   distributes items; SPID counts the Item lines of all ini files together).
2. `help "Cardboard Face Mask" 4` shows the item's full FormID. For an ESL plugin it looks like
   `FExxx800`: `FE`, a three-digit light-plugin index that depends on your load order, then `800`.
3. Try it on: `player.additem FExxx800 1`, then `player.equipitem FExxx800` (use your ID). Check it in
   third person: photo the right way round, cardboard back, twine, follows the head.
4. Walk into a town: adult NPCs wear masks, children do not. Click an NPC in the console and type
   `inv` to see whether the mask is in its inventory.
5. Spawn test NPCs: find a base ID with `help "<name>" 4` (look for an `NPC_` line), then
   `player.placeatme <baseID> 1`. Cover male, female, Khajiit and Argonian NPCs.

## Troubleshooting

| Symptom | Likely cause | What to do |
|---|---|---|
| Nobody wears a mask; the SPID log does not exist | SPID not loaded | Start the game with `skse64_loader.exe`. Check `skse64.log` (same SKSE log folder) for SPID; "address library needs to be updated" means the Address Library for 1.7.104 is missing. |
| SPID log does not list `CardboardFaceMask_DISTR.ini` | ini in the wrong place or renamed | It must be directly in `Data`, named `*_DISTR.ini`. |
| Log has `FAIL - formID doesn't exist` for `CardboardFaceMask.esp` (and `Registered x/y Items` with x < y) | plugin not active, or renamed | Enable `CardboardFaceMask.esp`; the name in the ini must match the plugin file. |
| Log has `Failed to parse entry [Item = ...]`; no `Registered ... Items` line | the ini line was edited into a form SPID cannot read (e.g. an extra `\|` section, a bad count or chance). SPID usually shows no message in game for this | Compare the line with the commented examples in the ini, or reinstall the ini from the zip. |
| Different NPCs have masks after each restart | `chance_percent` below 100 without the `!` suffix | Use e.g. `25!` (the generated ini already does). |
| `inv` shows the mask but the NPC does not wear it | the game chose not to equip it, or another mod's item uses slot 55 | Select the NPC and `equipitem FExxx800` to confirm the mesh works; look for other slot-55 items; keep `non_playable: false`. Report what you see. |
| Mask worn (in `inv`/equipped) but invisible on one race | race not covered by the armatures (often a modded race) | Note the NPC's race; it has to be added to the plugin's race list and the plugin rebuilt. |
| Purple mask | textures not found | Check `textures/CardboardFaceMask/*.dds` were installed next to the meshes (MO2: no conflicting mod hides them). |
| Mask floats too far forward or cuts into the face | head shape differs from vanilla (head-mesh or RaceMenu sliders), or beast scale | Adjust `forward_offset_units`, `size_factor`, `scale_*` and rebuild. |
| Mask does not turn with the head | skinning problem | Please report; every vertex is meant to follow `NPC Head [Head]`. |
| Children wear masks | `exclude_children: false`, or the child is not flagged as a child | Check the ini traits section is `-C`. |
| Crash (CTD) when NPCs load | unknown | Disable `CardboardFaceMask.esp`; if the crash stops, keep the crash log (e.g. from a crash-logger SKSE plugin) and report it. Also confirm SKSE, Address Library and SPID match 1.7.104. |
| The player has no mask | by design | Use the console (step 3 above). |

## Uninstall

Disable or remove the mod in MO2/Vortex, or delete `CardboardFaceMask.esp`,
`CardboardFaceMask_DISTR.ini`, `meshes/CardboardFaceMask/` and `textures/CardboardFaceMask/` from
`Data` and remove the plugin from `Plugins.txt`. SPID re-distributes on every game start and does
not write its distribution into saves, but masks that NPCs had in their inventory refer to the
plugin; when it is gone the game drops them and may warn that the save relies on content that is
no longer present. Keep a save from before installing if you want a clean way back.

## Rebuild from source

Everything is generated by one command; the outputs land in `work/` (intermediate) and `dist/`.

**First build on a new machine.** `work/` is not in git, so a fresh clone has no
`work/outline.json` or `work/matte.png`. The first build must therefore run the segment stage
(plain `build_all.py`, which needs the ML packages), unless you copy those two files from a machine
that ran it; after that, `build_all.py --skip-ml` works. PyTorch 2.14.1 has macOS wheels only for
Apple Silicon on macOS 14 or later: on an Intel Mac or an older macOS, `tools/setup.sh` skips the
ML packages with a warning, so run the segment stage on Linux or WSL2 and copy the two files over.

### Linux

```bash
tools/setup.sh                       # .venv, Python packages, NiflyDLL, Blender 4.2.14, checks .NET
.venv/bin/python src/build_all.py    # all stages + tests
```

Needs git, cmake, a C++20 compiler, curl, Python 3.10-3.13 (3.11 recommended) and the
[.NET 10 SDK](https://dotnet.microsoft.com/download) (plugin generator and Spriggit).
`tools/setup.sh --no-ml` skips PyTorch when you already have `work/outline.json` and
`work/matte.png` (then build with `build_all.py --skip-ml`).

### macOS

```bash
xcode-select --install              # C++ compiler
brew install python@3.11 cmake      # or install them another way
# .NET 10 SDK: https://dotnet.microsoft.com/download
tools/setup.sh                      # builds NiflyDLL as a .dylib; prints how to install Blender
export BLENDER=/Applications/Blender.app/Contents/MacOS/Blender   # only for preview renders
.venv/bin/python src/build_all.py   # Intel Mac / macOS < 14: --skip-ml with copied work files
```

`tools/setup.sh` avoids bash 4 features (macOS ships bash 3.2) but has only been run on Linux, with
the macOS branches simulated.

`tools/setup.sh` does not download Blender on macOS; it prints the 4.2.14 `.dmg` link for your
CPU. Blender is only used for the preview renders and their test.

### Windows

The scripts are written for Linux/macOS. The simplest route on Windows is WSL2 (Ubuntu): clone
the repo there and follow the Linux steps. A native build should also work but is untested:
install Python 3.11, the .NET 10 SDK, git and Blender 4.2 LTS, then

```bat
py -3.11 -m venv .venv
.venv\Scripts\pip install -r requirements.txt -r requirements-ml.txt
.venv\Scripts\python tools\nifly\build_niflydll.py --fetch-only
rem put PyNifly's official NiflyDLL.dll (from the PyNifly release matching PYNIFLY_SHA in
rem tools\nifly\build_niflydll.py) into third_party\PyNifly\io_scene_nifly\
rem only for preview renders; adjust to where Blender is installed:
set BLENDER=C:\Program Files\Blender Foundation\Blender 4.2\blender.exe
.venv\Scripts\python src\build_all.py
```

### Pipeline

| Stage | Script | Output |
|---|---|---|
| silhouette | `src/01_segment.py` (face parsing + matting, CPU) | `work/outline.json` |
| textures | `src/02_textures.py` | `work/textures/*.png`, `work/atlas_layout.json` |
| DDS | `src/05_dds.py` (BC7 diffuse, BC3 normal+specular, full mips) | `work/dds/*.dds` |
| mesh | `src/03_mesh.py` | `work/mesh/*.npz` |
| NIF | `src/04_export_nif.py` (PyNifly) | `work/nif/*.nif` |
| plugin | `src/plugin/build_plugin.py --regenerate` (Mutagen + Spriggit) | `work/plugin/CardboardFaceMask.esp`, `src/plugin/yaml/` |
| package | `src/07_package.py` | `dist/CardboardFaceMask/`, `dist/CardboardFaceMask.zip` |
| tests | `python -m pytest tests` | |

`src/build_all.py --list` shows the stages; `--from mesh`, `--only package,test` and `--skip-ml`
select parts. File formats between stages: [docs/CONTRACTS.md](docs/CONTRACTS.md).

### Safety hook for Claude Code

`.claude/settings.json` registers a PreToolUse hook (`.claude/hooks/block_game_data.sh`, which runs
`block_game_data.py`) that stops Claude Code from writing into, or deleting from, a Skyrim `Data`
folder. It needs `bash` and Python 3 (`python3`, `python` or `py -3`) on PATH. Without Python it
deliberately blocks every tool call, with a message saying why, instead of silently letting writes
through. It cannot see paths hidden in shell variables.

### How it is checked

There is no game here, so every stage has offline acceptance checks: silhouette and landmark
sanity, DDS headers and decode quality, mesh winding/normals and clearance against the vanilla head,
NIF skinning (every vertex 1.0 on `NPC Head [Head]`), an independent ESP reader that checks every
record and the vanilla FormIDs against a second source, Blender renders on vanilla heads, and the
package stage (`src/07_package.py`): the build products must match `config.yaml` (silhouette mode,
photo) and each other (no stage output older than its input, no test fixtures); every texture path
in any of the nine texture slots of every NIF and every model path in the ESP must exist in the
package with exact case; no stray or unreferenced files; the SPID line must point at the shipped
plugin's ARMO; and the zip must match the folder byte for byte. The package is assembled in a
staging folder and only replaces `dist/` when all of this passes.
The SPID line itself was derived from SPID's source code: [docs/SPID_NOTES.md](docs/SPID_NOTES.md).

## Credits and licenses

| Component | Used for | License |
|---|---|---|
| [PyNifly](https://github.com/BadDogSkyrim/PyNifly) by BadDogSkyrim | NIF writing (`pyn` layer), reference NIFs and skeletons | GPL-3.0 (`third_party/PyNifly/LICENSE`) |
| [nifly](https://github.com/ousnius/nifly) by ousnius | NIF library behind PyNifly | GPL-3.0 (`third_party/nifly/LICENSE`) |
| [Mutagen](https://github.com/Mutagen-Modding/Mutagen) (Mutagen.Bethesda.Skyrim 0.54.4) | generating the ESP | GPL-3.0-only (NuGet metadata) |
| [Mutagen.Bethesda.FormKeys](https://github.com/Noggog/Mutagen.Bethesda.FormKeys) (SkyrimSE 3.4.0) | vanilla FormIDs | GPL-3.0-only (NuGet metadata) |
| [Spriggit](https://github.com/Mutagen-Modding/Spriggit) CLI 0.41.0 | ESP <-> YAML source (`src/plugin/yaml`) | GPL-3.0-only (NuGet metadata) |
| YamlDotNet 18.1.0 | plugin generator | MIT (NuGet metadata) |
| [jonathandinu/face-parsing](https://huggingface.co/jonathandinu/face-parsing) @ `758b82e` (SegFormer, fine-tuned from nvidia/mit-b5 on CelebAMask-HQ) | face/hair/glasses segmentation | model card: "non-commercial research and educational purposes"; base model nvidia/mit-b5: license "other" |
| [ZhengPeng7/BiRefNet-matting](https://huggingface.co/ZhengPeng7/BiRefNet-matting) @ `eccde0a` | foreground matte | MIT (Hugging Face model metadata) |
| [Blender](https://www.blender.org/) 4.2.14 LTS | offline preview renders only | GPL-2.0-or-later |
| [SPID](https://github.com/powerof3/Spell-Perk-Item-Distributor) by powerofthree | runtime distribution (installed separately, not included) | MIT |

The build tools above are not shipped in the mod package; the package contains only files this
project generated. Python packages are listed with versions in `requirements*.txt` and keep their
own licenses. The face-parsing model's terms restrict it to non-commercial use, which fits this
personal-use mod; do not sell anything made with it.

**About the photo.** `input/IMG_2117.PNG` is my own photo of myself, taken for this personal mod. It is committed to this repository, which is **public**, and the textures in the
package are made from it, so anyone who sees the repo or the zip sees the face.
