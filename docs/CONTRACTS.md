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
  "coordinate_convention": "continuous: image covers [0,W]x[0,H]; pixel (i,j) centre is (i+0.5, j+0.5)",
  "orientation": "positive shoelace area on raw (x, y) image coordinates (appears clockwise on screen)",
  "selected_mode": "face_hair_glasses",
  "modes": {
    "face_only":         {"polygon": [[x, y], ...], "bbox": [x0, y0, x1, y1], "crown": [x, y], "stats": {...}},
                         // modes.<m>.crown = highest vertex of THAT mode's polygon (its cut top)
    "face_hair":         {"polygon": [[x, y], ...], ...},
    "face_hair_glasses": {"polygon": [[x, y], ...], ...}
  },
  "landmarks": {
    "eye_image_left":  [x, y],   // iris centre of the eye on the LEFT of the image (subject's right eye)
    "eye_image_right": [x, y],   // iris centre of the eye on the RIGHT of the image
    "nose_tip": [x, y],          // frontal projection of the nose tip (pronasale)
    "mouth":    [x, y],          // centroid of the lips
    "chin":     [x, y],          // where the face centre line (eye midpoint -> mouth, extended) leaves the SELECTED polygon
    "crown":    [x, y],          // top of the head incl. hair: highest vertex of the face_hair polygon,
                                 // the SAME in every mode -> use chin.y - crown.y as the photo head height
    "cut_top":  [x, y]           // highest vertex of the SELECTED polygon (= crown unless face_only)
  },
  "iris_fit": {"eye_image_left": {"centre": [x, y], "radius_px": r, "symmetry": {...}}, "eye_image_right": {...}},
  "segmentation": {"eye_line_y": y, "neck_cloth_relabelled_as_hair_px": n},   // diagnostics
  "matte": "work/matte.png",     // 8-bit foreground alpha, full image size (used by stage 2 only)
  "decode": {...}, "models": {...}   // provenance: ICC/bit depth, pinned Hugging Face revisions
}
```
Polygons: closed (first point not repeated), 120–400 points, no self-intersections, all points
inside [0,W]x[0,H] (where the subject touches the photo border the cut runs along the border).
**Orientation: the signed shoelace area `0.5*sum(x_i*y_{i+1} - x_{i+1}*y_i)` computed directly on the
(x right, y down) pixel coordinates is positive** (shapely `Polygon.exterior.is_ccw` is True); on screen,
where y points down, this traversal looks clockwise. Smoothed so it reads as a scissor/die cut
(no stair-steps, every interior vertex angle > 90°). Modes: `face_only` = skin, nose, eyes, brows,
lips/mouth, ears (+ glasses inside the face's convex hull); `face_hair` = + hair (and hat);
`face_hair_glasses` = + the complete eyeglasses incl. temples. All modes exclude neck and clothing:
the lower boundary follows the jaw/chin line. Neck/clothing labels only count where they can
physically be: connected components that touch the bottom of the photo or lie wholly below the eye
line, and only their part below the eye line; anything else the parser calls neck/cloth (e.g. dark
crown hair tufts) is treated as hair. Parsing decides which parts belong to a mode, the matte
(alpha > 0.5) decides where they end against the background.
Stats per mode: `background_inside_fraction` (matte background inside the cut / cut area),
`raw_region_lost_fraction`, and for the hair modes `head_matte_outside_fraction` (matte foreground
above the eye line outside the cut / cut area; tested < 0.001, independent of the exclusions).
Landmark sanity (tested): crown.y <= cut_top.y < eyes.y < nose.y < mouth.y < chin.y and
eye_image_left.x < nose.x < eye_image_right.x. Eye landmarks are iris circle centres (best
dark-inside/bright-outside circle inside the parsed eye opening); tested on the photo: the iris edge
lies within +-20 % of the radius to the lower-left, lower-right and below, left/right mismatch
< 0.15 radius.

## Stage 2 — textures: `src/02_textures.py` → `work/textures/*.png`, `work/atlas_layout.json`

- `mask_d.png` (RGB, `texture_size`²) — atlas: front photo print, corrugated edge strip, kraft back, tape.
- `mask_n.png` (RGBA) — tangent-space normal map; **alpha = specular mask** (Skyrim convention).
  **Normal convention: DirectX (as Skyrim): R = +u (right), G = +v (down the texture), B = out of the
  surface.** Specular (alpha) levels: front 0.40, back 0.10, edge 0.08, tape 0.25, twine 0.10.
- `twine_d.png` (RGB), `twine_n.png` (RGBA, alpha = specular) — `twine_texture_size`², tile in u and v.
- Every atlas region is padded by `padding_texels` (>= 16) of edge-extended colour/normal
  (the edge strip's padding is wrapped in u); padded rects never overlap. Outside the selected
  silhouette, the front region holds bled subject colours (no background), so cut edges stay clean
  at every mip level.
- `atlas_layout.json`:

```json
{
  "size": 2048,
  "uv_convention": "u right, v down (v=0 top row)",
  "normal_map_convention": "tangent space, DirectX (Skyrim): R=+u, G=+v (down), B=out; alpha = specular mask",
  "padding_texels": 16,
  "silhouette_mode": "face_hair_glasses",        // mode the front print was cut/bled for
  "regions": {
    "front": {"uv_rect": [u0, v0, u1, v1], "image_rect": [x0, y0, x1, y1], "texels_per_image_px": s},
    "back":  {"uv_rect": [u0, v0, u1, v1], "image_rect": [x0, y0, x1, y1], "mirror_u": true},
    "edge":  {"uv_rect": [u0, v0, u1, v1], "tile_length_units": 6.0, "wrap_u": true,
              "layers_v": {"paper": 0.05, "front_liner": 0.17, "back_liner": 0.88}, "flutes_per_tile": 11},
    "tape":  {"uv_rect": [u0, v0, u1, v1], "aspect": 2.8}
  },
  "twine": {"size": 512, "tile_length_units": 0.6, "plies": 3, "twists_per_tile": 3},
  "specular": {...}, "front_cut_edge_greenness": {...}   // informational
}
```
`front.image_rect` is the source-image box printed into `front.uv_rect` (linear mapping, no rotation,
aspect preserved exactly; it may extend past the photo border): a source point (x, y) in the stage 1
continuous coordinates has `u = u0 + (x-x0)/(x1-x0)*(u1-u0)`, `v = v0 + (y-y0)/(y1-y0)*(v1-v0)`.
The box is the union bbox of all three modes plus a 3 % margin, so any mode's polygon maps inside it.
`back` maps the same image box; with `mirror_u` true use `u = u1 - (x-x0)/(x1-x0)*(u1-u0)` (it is seen
from behind; v as for the front). `edge`: u runs along the perimeter, one copy of the rect's u span per
`tile_length_units` world units (split vertices where the pattern wraps; texture coordinates cannot
wrap inside an atlas), v runs across the cardboard thickness (v0 = front face, v1 = back face; the
thin light layer at v0 is the photo paper). `tape`: the tape strip runs along u with torn ends; the
rect includes a kraft-coloured margin (~6 % in u, ~10 % in v) so a quad covering the whole rect,
laid just above the kraft back, reads as tape on cardboard. Twine: u runs along the string (one tile
every `tile_length_units`), v once around the circumference.

## Stage 3 — DDS: `src/05_dds.py` → `work/dds/*.dds`

Same base names as the PNGs. Full mip chains (mip count = log2(size) + 1, down to 1x1),
power-of-two, UNORM formats (no sRGB variants), mips generated by the stage (diffuse: box filter in
linear light; normals: averaged and renormalised, spec alpha averaged).
- `mask_d.dds`, `twine_d.dds`: BC7 with a DX10 header (`DXGI_FORMAT_BC7_UNORM` = 98);
  `--diffuse-format BC1` writes legacy DXT1 instead.
- `mask_n.dds`, `twine_n.dds`: BC3 / FourCC `DXT5` (alpha = specular).
Legacy header fields: flags `0xA1007` (CAPS|HEIGHT|WIDTH|PIXELFORMAT|MIPMAPCOUNT|LINEARSIZE),
`dwPitchOrLinearSize` = byte size of mip 0, caps `0x401008` (COMPLEX|TEXTURE|MIPMAP).
`work/dds/dds_report.json` records the verification (header checks, PSNR per level).

## Stage 4 — mesh + NIF: `src/03_mesh.py`, `src/04_export_nif.py` → `work/nif/*.nif`, `work/nif_manifest.json`

Files: `mask_human_m.nif`, `mask_human_f.nif`, `mask_beast_m.nif`, `mask_beast_f.nif`
(optional `mask_go.nif` ground model). Texture paths written into the NIFs:
`textures\CardboardFaceMask\mask_d.dds`, `mask_n.dds`, `twine_d.dds`, `twine_n.dds`.

Acceptance inside the stage: `04_export_nif.py` reads every written NIF back with pyn
(`verify_nif`: game, shape names and authored vertex/triangle counts, BSTriShape +
BSDismemberSkinInstance, the single bone `NPC Head [Head]` weighting every vertex 1.0,
skin-to-bone = inverse(bind), partitions == [slot], tangents, texture paths) and exits 1 on any
problem or any NiflyDLL log message, before the package stage can copy the files to `dist/`.

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

## Stage 6 — package: `src/07_package.py` → `dist/CardboardFaceMask/`, `dist/CardboardFaceMask.zip`, `work/package_report.json`

```
dist/CardboardFaceMask/
  CardboardFaceMask.esp
  CardboardFaceMask_DISTR.ini
  meshes/CardboardFaceMask/*.nif
  textures/CardboardFaceMask/*.dds
dist/CardboardFaceMask.zip            same files, zip root = Data folder, fixed timestamps (reproducible)
```

Inputs: `work/plugin/<plugin_name>`, `work/nif/*.nif`, `work/dds/*.dds`, the four build records
below and `config.yaml` (`plugin_name`, `exclude_children`, `chance_percent`, `equip_method`,
`silhouette_mode`, `source_image`).

**DISTR ini** (`<plugin stem>_DISTR.ini`, ASCII, generated from config, rules and citations in
docs/SPID_NOTES.md):
- `equip_method: kef` (default): two active lines, `Item = 0x800~<plugin>|ActorTypeNPC|NONE|NONE|<traits>|1|<chance>`
  and `Keyword = KEFEquip_0x800_<plugin stem>_<ext>|ActorTypeNPC|NONE|NONE|<traits>|NONE|<chance>`
  (traits `-C` or `NONE`; chance `100`, or e.g. `25!` below 100). Below 100 % the chance stays on the
  `Keyword` line and the `Item` line uses the keyword as its string filter with chance `100`.
  The plugin stem must be `[A-Za-z0-9]+(_[A-Za-z0-9]+)*` with no `_` part named esp/esm/esl/forceEquip
  (KEF keyword format; KEF's own example plugin `Kad_AkaviriHats.esp` has an `_`; SPID_NOTES section 13).
  The KEF suffix `_forceEquip_true` is not written (documented as a manual ini edit).
- `equip_method: engine`: the `Item` line only. Any other value is an error.

**Build-input gate** (before anything is assembled; also `outline_problems` is used by
`src/build_all.py` when the segment stage is skipped):
- mode/photo consistency: `work/outline.json` `selected_mode` == config `silhouette_mode`, `source`
  == `source_image`, `image_size` == the photo's size (EXIF rotation applied), `work/matte.png`
  exists; `work/atlas_layout.json` `silhouette_mode` and every `work/mesh/mesh_report.json`
  variant's `silhouette_mode` == config; no `*_is_fixture` / `inputs_are_fixtures` flag set.
- mtime order (all in `work/`): `outline.json` <= `atlas_layout.json` <= `mesh/mesh_report.json` <=
  `nif_manifest.json` (`BUILT_FROM`: each pair is compared when both files exist); an output older
  than its input means a stage was skipped, and the error names the `--from` stage to re-run.

**Assembly**: everything is built in `dist/.CardboardFaceMask.staging/` (`CardboardFaceMask/` and the
zip), validated there, and only then moved over the old `dist/CardboardFaceMask/` and zip; on any
problem the old package stays untouched and the staging folder is deleted. `dist/` is the only
folder the stage deletes in (`remove_in_dist`).

**Validation** (also `07_package.py --check-only`, which re-validates the existing `dist/` folder and
zip without rebuilding, without the build-input gate and without writing the report; it still
cross-checks `work/nif_manifest.json` when that exists): only the allowed files (plugin, ini,
`meshes/CardboardFaceMask/*.nif`, `textures/CardboardFaceMask/*.dds`, lower-case `[a-z0-9_]` asset
names, no symlinks); plugin ESL-flagged, not localized, masters `[Skyrim.esm]`, own records in
0x800-0xFFF, ARMO `FaceMaskArmor` at 0x800; every ESP model path and every NIF texture path (all
nine BSShaderTextureSet slots, index 0-8) exists in the package with exact case; no unreferenced
mesh/texture; DDS headers sane and each DDS exactly as long as its header implies (header + every
mip level of 4x4 blocks: a truncated file fails); ESP model paths == `work/nif_manifest.json`; ini entries as above
(exact text == generator output, `Item` targets 0x800 of the shipped plugin, the keyword is the KEF
name for it and reads as an EditorID in SPID, both lines select the same NPCs, nothing SPID's
sanitizer would rewrite); zip entries == folder files byte for byte.

**`work/package_report.json`** (written by a full run only):
```json
{
  "package_dir": "dist/CardboardFaceMask",
  "plugin": {"esl": true, "masters": ["Skyrim.esm"],
             "records": [{"type": "ARMO", "editor_id": "FaceMaskArmor", "local_id": "0x800"}, ...],
             "model_paths": ["CardboardFaceMask\\mask_beast_f.nif", ...]},
  "nif_textures": {"meshes/CardboardFaceMask/mask_human_m.nif": ["textures\\CardboardFaceMask\\mask_d.dds", ...], ...},
  "equip_method": "kef",
  "kef_keyword": "KEFEquip_0x800_CardboardFaceMask_esp",      // null with equip_method engine
  "distr_lines": ["Item = 0x800~CardboardFaceMask.esp|...", "Keyword = KEFEquip_0x800_...|..."],
  "files": [{"path": "CardboardFaceMask.esp", "bytes": 900, "sha256": "..."}, ...],
  "build_inputs": {"silhouette_mode": "face_hair_glasses", "source_image": "input/IMG_2117.PNG", "consistent": true},
  "zip": {"path": "dist/CardboardFaceMask.zip", "bytes": n, "sha256": "...", "entries": 10}
}
```
