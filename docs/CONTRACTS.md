# Pipeline contracts

Stages communicate only through the files below. Paths are relative to the repo root.
Every stage reads `config.yaml` through `cfm.paths.load_config()`.

## Coordinate conventions (verified against third_party skeleton.nif)

- Skyrim model space, bind pose, actor standing at the origin.
- **+Z up, +Y forward** (the way the actor faces), **+X = actor's right** (`NPC L Clavicle` is at x = -1.70,
  `NPC R Clavicle` at x = +1.70).
- A viewer standing in front of the actor looks down -Y. The viewer's right is -X, so
  **image x (rightwards) maps to model -X** and **image y (downwards) maps to model -Z**.
- `NPC Head [Head]` bind transform (identical in skeleton.nif, skeleton_female.nif, skeletonbeast*.nif):
  translation (-0.0003, -1.5475, 120.3436), rotation ≈ identity (tiny tilt, use the real matrix).
- NIF UVs: u to the right, **v downwards** (v = 0 is the top row of the texture image).

## Stage 1 — silhouette: `src/01_segment.py` → `work/outline.json`

```json
{
  "source": "input/IMG_2117.PNG",
  "image_size": [1320, 2868],
  "units": "source-image pixels, origin top-left, y down",
  "selected_mode": "face_hair_glasses",
  "modes": {
    "face_only":         {"polygon": [[x, y], ...]},
    "face_hair":         {"polygon": [[x, y], ...]},
    "face_hair_glasses": {"polygon": [[x, y], ...]}
  },
  "landmarks": {
    "eye_image_left":  [x, y],   // eye that appears on the LEFT of the image (subject's right eye)
    "eye_image_right": [x, y],
    "nose_tip": [x, y],
    "mouth":    [x, y],
    "chin":     [x, y],          // lowest point of the jaw on the face centre line
    "crown":    [x, y]           // highest point of the selected silhouette
  }
}
```
Polygons: closed (first point not repeated), counter-clockwise in image coordinates, no
self-intersections, 120–400 points, simplified so it reads as a scissor/die cut.

## Stage 2 — textures: `src/02_textures.py` → `work/textures/*.png`, `work/atlas_layout.json`

- `mask_d.png` (RGB, `texture_size`²) — atlas: front photo print, corrugated edge strip, kraft back, tape.
- `mask_n.png` (RGBA) — tangent-space normal map; **alpha = specular mask** (Skyrim convention).
- `twine_d.png`, `twine_n.png` (`twine_texture_size`², RGBA normal with alpha spec mask).
- `atlas_layout.json`:

```json
{
  "size": 2048,
  "uv_convention": "u right, v down (v=0 top row)",
  "regions": {
    "front": {"uv_rect": [u0, v0, u1, v1], "image_rect": [x0, y0, x1, y1]},
    "back":  {"uv_rect": [u0, v0, u1, v1], "image_rect": [x0, y0, x1, y1], "mirror_u": true},
    "edge":  {"uv_rect": [u0, v0, u1, v1], "tile_length_units": 6.0},
    "tape":  {"uv_rect": [u0, v0, u1, v1]}
  },
  "twine": {"tile_length_units": 2.0}
}
```
`front.image_rect` is the source-image pixel box printed into `front.uv_rect` (linear mapping, no
rotation): a source pixel (x, y) has `u = u0 + (x-x0)/(x1-x0)*(u1-u0)`, `v = v0 + (y-y0)/(y1-y0)*(v1-v0)`.
`back` maps the same image box the same way, mirrored in u when `mirror_u` is true (it is seen from
behind). `edge`: u runs along the perimeter (wrapping every `tile_length_units` world units across
the rect's u span), v runs across the cardboard thickness (v0 = front face, v1 = back face).
`twine_d.png`: u runs along the string (one tile every `tile_length_units`), v around the circumference.

## Stage 3 — DDS: `src/05_dds.py` → `work/dds/*.dds`

Same base names as the PNGs. Full mip chains, power-of-two. Diffuse: BC1 (or BC7 with a DX10
header). Normal maps: BC3 (alpha = specular), or BC7.

## Stage 4 — mesh + NIF: `src/03_mesh.py`, `src/04_export_nif.py` → `work/nif/*.nif`, `work/nif_manifest.json`

Files: `mask_human_m.nif`, `mask_human_f.nif`, `mask_beast_m.nif`, `mask_beast_f.nif`
(optional `mask_go.nif` ground model). Texture paths written into the NIFs:
`textures\CardboardFaceMask\mask_d.dds`, `mask_n.dds`, `twine_d.dds`, `twine_n.dds`.

`work/nif_manifest.json`:
```json
{
  "meshes_subdir": "CardboardFaceMask",
  "models": {"human_m": "CardboardFaceMask\\mask_human_m.nif", "human_f": "...", "beast_m": "...", "beast_f": "..."},
  "ground_model": null,
  "textures": ["textures\\CardboardFaceMask\\mask_d.dds", "..."]
}
```

## Stage 5 — plugin: `src/plugin/` → `work/plugin/CardboardFaceMask.esp`

Local FormIDs (ESL range): ARMO `FaceMaskArmor` = 0x800, ARMA `FaceMaskAA` = 0x801,
ARMA `FaceMaskAA_Beast` = 0x802. Model paths come from `work/nif_manifest.json`
(relative to `meshes\`, backslashes).

## Stage 6 — package: `src/07_package.py` → `dist/CardboardFaceMask/`, `dist/CardboardFaceMask.zip`

```
dist/CardboardFaceMask/
  CardboardFaceMask.esp
  CardboardFaceMask_DISTR.ini
  meshes/CardboardFaceMask/*.nif
  textures/CardboardFaceMask/*.dds
```
