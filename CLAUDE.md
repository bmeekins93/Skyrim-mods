# Project: Cardboard Face Mask (Skyrim SE)

## Goal
Every adult humanoid NPC in Skyrim Special Edition (Steam) wears a comedic cardboard-cutout mask:
a thin 3D slab cut to the outline of the face/head in input/IMG_2117.PNG, photo printed on the front,
visible corrugated edges, plain brown cardboard back (optional tape strip), and a twine string that
wraps awkwardly around the back of the head. Personal use only. No Papyrus scripts.

## Architecture (do not change without asking)
- Plugin: CardboardFaceMask.esp, ESL-flagged, master Skyrim.esm only. Generated with Mutagen,
  stored as Spriggit YAML (src/plugin/yaml), rebuilt with `spriggit deserialize`.
- Records (local FormIDs are a contract, see docs/CONTRACTS.md):
  ARMO FaceMaskArmor 0x800 (Clothing, wt 0, val 0, AR 0, biped slot 55, playable unless config says otherwise),
  ARMA FaceMaskAA 0x801 (DefaultRace + explicit humanoid/vampire/elder races, slot 55, M+F models),
  ARMA FaceMaskAA_Beast 0x802 (Khajiit/Argonian + vampire variants, scaled mesh).
- Distribution: CardboardFaceMask_DISTR.ini (SPID), primary line:
  Item = 0x800~CardboardFaceMask.esp|ActorTypeNPC|NONE|NONE|-C|1|100
- Meshes: meshes/CardboardFaceMask/mask_{human,beast}_{m,f}.nif — BSTriShape, BSLightingShaderProperty,
  no collision, every vertex skinned 1.0 to "NPC Head [Head]", dismember partition 55.
- Textures: textures/CardboardFaceMask/*.dds with mipmaps, power-of-two sizes.

## Rules
1. NEVER write NIF or ESP binaries by hand. NIF via PyNifly's `pyn` layer (src/cfm/nifenv.py);
   ESP via Mutagen/Spriggit only.
2. NEVER guess record schemas, FormIDs or EditorIDs. Use verified sources (Mutagen FormKey packages,
   real vanilla NIFs under third_party/PyNifly) and cite them.
3. NEVER write into a game Data folder. Build into dist/CardboardFaceMask/; the user installs it.
4. Every stage ends with acceptance checks; keep the evidence (renders in docs/renders, test output).
5. Do not invent Papyrus functions; this project uses none.

## Environment
- `tools/setup.sh` creates .venv, builds NiflyDLL for Linux/macOS (tools/nifly/build_niflydll.py) and
  fetches Blender into third_party/blender (Linux). On Windows use PyNifly's official NiflyDLL.dll.
- Python: always run with `.venv/bin/python`. Shared helpers live in src/cfm/.
- Blender (headless renders): third_party/blender/blender or $BLENDER.
- .NET SDK 8+ for the plugin generator and Spriggit.
- Full build: `.venv/bin/python src/build_all.py`; tests: `.venv/bin/python -m pytest tests`.
