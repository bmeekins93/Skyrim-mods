# NIF notes - Cardboard Face Mask meshes

How `work/nif/mask_{human,beast}_{m,f}.nif` are produced, why they are engine-correct, and a
block-by-block comparison with the vanilla-style skinned helmet
`third_party/PyNifly/tests/tests/SkyrimSE/helmet-SMP.nif`, explaining every difference.

Regenerate the dumps below with
`.venv/bin/python src/04_export_nif.py --dump third_party/PyNifly/tests/tests/SkyrimSE/helmet-SMP.nif work/nif/mask_human_m.nif`
(structure from the independent byte reader `cfm.geom_nifinfo.RawNif`, decoded fields from pyn).

## 1. Export path (PyNifly's own Skyrim SE path, without Blender)

`src/04_export_nif.py` replays what PyNifly's Blender exporter does for a skinned Skyrim SE shape
(`third_party/PyNifly/io_scene_nifly/nif/export_nif.py`):

| PyNifly exporter | our call | what NiflyDLL / nifly does |
|---|---|---|
| `export_nif`: `NifFile.initialize(game, path, "NiNode", "Scene Root")` | same, game `SKYRIMSE` | header 20.2.0.7 / user 12 / BS 100 |
| `export_shape` l.2064: flips Blender UVs to NIF (v down), `createShapeFromData` l.2065 | `createShapeFromData(name, verts, tris, uvs, normals, props=BSTriShape buf)` with NIF-convention UVs | `BSTriShape::Create` (nifly `src/Geometry.cpp` l.1148): stores normals and calls `CalcTangentSpace()` (l.1203) because normals are given -> tangents + bitangents |
| `shaderexp.export(new_shape)` l.2090 -> `save_shader_attributes()` | `set_shader()`: flags, values, `set_texture("Diffuse"/"Normal")`, `save_shader_attributes()` | `addNiShader` / `copyNiShader` (NiflyDLL `NiflyWrapper.cpp`) write every field of the existing BSLightingShaderProperty |
| `export_skin` l.1822: `skin()`, `set_global_to_skin(inverse(skin_xf))`, `add_bone`, `set_skin_to_bone_xform(inverse(inverse(skin_xf) @ bone_xf))`, `setShapeWeights` | identical with `skin_xf = I` (vertices authored in model space, shape at the root): global-to-skin = I, skin-to-bone = inverse(bind) | `NifFile::CreateSkinning` (nifly `src/NifFile.cpp` l.4671): for stream 100 adds NiSkinData + NiSkinPartition + **BSDismemberSkinInstance** and sets the shader's Skinned flag |
| `set_partitions(...)` l.2144 (not demoted because partitions exist, l.2151) | `set_partitions([SkyPartition(55)], [55]*n_tris)` | `setPartitions` -> `SetShapePartitions` + `UpdateSkinPartitions`, whose `UpdatePartitionFlags` (l.4639) sets PF_EDITOR_VISIBLE \| PF_START_NET_BONESET = 257 |
| `nif.save()` | same | `FinalizeData` (l.2079) copies the SSE vertex data into NiSkinPartition; stream 100 always writes full-precision float positions (`Geometry.cpp` l.517 / l.1095) |

The head-bone bind comes from the variant's own skeleton: `skeleton.nif` (human_m),
`skeleton_female.nif` (human_f), `skeletonbeast.nif` (beast_m), `skeletonbeast_female.nif` (beast_f).
All four store the same `NPC Head [Head]` world transform: t = (-0.0003, -1.5475, 120.3436), R = identity
tilted by ~0.6 deg (row 0 = +0.99994 +0.01060 -0.00117).

## 2. Transform conventions and the proof

* nifly `MatTransform::ApplyTransform`: `p_parent = t + R (s p)`; `TransformBuf.rotation[r][c]` is that
  R. The raw reader shows the 9 stored floats of a NiTransform are exactly these rows (helmet
  skin-to-bone row 0 = +0.9999 -0.0106 +0.0012 in both readers).
* Model-space (bind-pose) position of a skinned vertex: `sum_i w_i * bind_i @ skin_to_bone_i @ v`.
  On malehead.nif the two bones (Spine2, Head; very different rotations) agree to 1.8e-5 units, which
  would fail by ~0.3 units if the rotation convention were transposed.
* **global-to-skin is not trustworthy on its own**: maleheadargonian.nif stores an identity
  global-to-skin although its vertices are head-bone relative (its bones put them 120.34 units higher).
  `cfm/fit.py` therefore reconstructs heads through the bones and only cross-checks global-to-skin.
* Proof against vanilla data: helmet-SMP.nif's vertices are already in model space (z 114.4-133.5). The
  reconstructed human head sits inside it: all 138 rays from the head centre through upper-head
  vertices that hit the helmet (the other 136 look out through its face opening) meet the outer
  shell beyond the vertex (min gap 0.04, median 1.14 units), 128 of
  them within 1 unit of the helmet's inner surface, crown 1.61 units under the shell top, centres
  0.01 (x) / 0.52 (y) apart. See `docs/renders/fit_human.png` (and `fit_beast.png`: the Argonian head
  does not fit a human helmet, as expected - informational only).
* Eye line (human) from the eye-socket pits of the front height field: z = 124.05; the skeleton's
  `NPCEyeBone` is at z = 124.048.
* Reference heads (all vanilla, from PyNifly's test assets at the pinned commit bcb723a, each checked
  against its sha256): malehead.nif (human), maleheadargonian.nif and
  `meshes/actors/character/character assets/maleheadkhajiit.nif` (beast; skinned to skeletonbeast.nif,
  all bones agree to 1.8e-5, and unlike the Argonian file its global-to-skin is correct). Every
  reference file is listed in PyNifly's sparse checkout (`PYNIFLY_SPARSE` in
  `tools/nifly/build_niflydll.py`, which now also lists the Khajiit head, `maleorchair27.nif` and
  `meshes/draugr lich01 hood.nif`). Those three also keep byte-identical, sha-pinned copies in
  `tests/fixtures/vanilla/` (`provenance.json`): `build_niflydll.py` leaves a checkout that is already
  at the pinned commit alone, so a checkout made before they joined the list never gets them, and
  `cfm.fit.reference_path` falls back to the copies. They can go once `fetch()` re-applies the list.
* One beast mesh serves both races (ARMA FaceMaskAA_Beast), so the beast variants use a combined
  anchor (cfm.fit.kind_anchor): the back face clears the most forward snout (Argonian, y 13.84; the
  Khajiit nose is at y 12.08, so for Khajiit the gap is 3.56 instead of forward_offset 1.8); eye
  line, chin and crown are the means (eye 125.53 = Argonian 126.05 / Khajiit 125.00; chin-to-crown
  14.98 = 13.51 / 16.44). The scalp crown is the highest vertex within 2 units of the centre line:
  the Khajiit ear tips reach z 135.19 and are not scalp. See `docs/renders/fit_beast.png` (both heads,
  with the combined eye anchor drawn in orange).

## 3. Winding, normals, tangents

* Vanilla convention measured on helmet-SMP.nif: `cross(v1 - v0, v2 - v0)` points the same way as the
  stored vertex normals for 100 % of triangles in all three shapes (counter-clockwise seen from
  outside = front face). The heads agree too (generalized winding number +0.90 at the head centre).
* Our triangles are oriented against analytic outward normals (cylinder radial on the caps, outline
  normal on the walls, tube radial on the twine, tape surface normal); agreement 100 % before export
  and >= 99 % (100 %) after read-back with byte-quantised normals. Normals are unit length; smooth
  inside each part, hard at the slab edges (walls and caps have their own vertices).
* Vertex attributes after export: VERTEX + UV + NORMAL + TANGENT + SKINNED - identical to the helmet's
  main shapes. Tangents come from nifly's `BSTriShape::CalcTangentSpace` (the Outfit Studio code).

## 4. helmet-SMP.nif (vanilla-style reference)

`helmet-SMP.nif`: Gamebryo File Format, Version 20.2.0.7, user version 12, BS version 100, 26 blocks, process script "Exported using Outfit Studio.", strings ['Scene Root', 'NPC Head [Head]', 'H_00', 'H_01', 'H_02', 'H_03', 'Helmet', 'HelmetTail', 'HelmetVisor', 'HDT Skinned Mesh Physics Object', 'meshes\\gvlt\\armor\\baron\\armor.xml', 'meshes\\gvlt\\armor\\baron\\helmet.xml']

| # | block | bytes | contents |
|---|---|---|---|
| 0 | NiNode | 100 | "Scene Root" flags 524302; t=(0.0000, 0.0000, 0.0000) R=[+1.0000 +0.0000 +0.0000 +0.0000 +1.0000 +0.0000 +0.0000 +0.0000 +1.0000] |
| 1 | NiNode | 84 | "NPC Head [Head]" flags 524302; t=(-0.0003, -1.5475, 120.3436) R=[+0.9999 +0.0106 -0.0012 -0.0106 +0.9999 +0.0010 +0.0012 -0.0009 +1.0000] |
| 2 | NiStringExtraData | 8 | string extra data on the root (HDT-SMP physics XML reference) |
| 3 | NiNode | 84 | "H_00" flags 524302; t=(0.0000, -7.0000, 3.0000) R=[+1.0000 +0.0000 +0.0000 +0.0000 +1.0000 +0.0000 +0.0000 +0.0000 +1.0000] |
| 4 | NiNode | 84 | "H_01" flags 524302; t=(0.0000, -1.0000, -3.0000) R=[+1.0000 +0.0000 +0.0000 +0.0000 +1.0000 +0.0000 +0.0000 +0.0000 +1.0000] |
| 5 | NiNode | 84 | "H_02" flags 524302; t=(0.0000, -1.0000, -3.0000) R=[+1.0000 +0.0000 +0.0000 +0.0000 +1.0000 +0.0000 +0.0000 +0.0000 +1.0000] |
| 6 | NiNode | 80 | "H_03" flags 524302; t=(0.0000, -1.0000, -3.0000) R=[+1.0000 +0.0000 +0.0000 +0.0000 +1.0000 +0.0000 +0.0000 +0.0000 +1.0000] |
| 7 | BSTriShape | 120 | "Helmet" 4485 verts / 7085 tris, flags 14, vertex attrs VERTEX+UV+NORMAL+TANGENT+SKINNED, bound sphere (0.001, -0.770, 122.880) r 11.569; transform t=(0.0001, 0.0004, -0.0002) R=[+1.0000 +0.0000 -0.0000 -0.0000 +1.0000 -0.0000 +0.0000 +0.0000 +1.0000] |
| 8 | BSDismemberSkinInstance | 28 | data #9, partition #10, root #0, bones [1], partitions [(257, 131)] (flags, body part) |
| 9 | NiSkinData | 27037 | global-to-skin t=(-0.0001, -0.0004, 0.0002) R=[+1.0000 -0.0000 +0.0000 +0.0000 +1.0000 +0.0000 -0.0000 -0.0000 +1.0000]; bone 0: skin-to-bone t=(-0.1580, 1.6609, -120.3422) R=[+0.9999 -0.0106 +0.0012 +0.0106 +0.9999 -0.0009 -0.0012 +0.0010 +1.0000], sphere (-0.004, 0.775, 2.537, 11.569), 4485 weights (min 1.000) |
| 10 | NiSkinPartition | 363136 | 1 partition(s), vertex data copied from the shape (SSE) |
| 11 | BSLightingShaderProperty | 104 | type 1, SF1 0x82400383 (SPECULAR, SKINNED, ENVIRONMENT_MAPPING, RECEIVE_SHADOWS, CAST_SHADOWS, OWN_EMIT, REMAPPABLE_TEXTURES, ZBUFFER_TEST), SF2 0x8001 (ZBUFFER_WRITE, ENVMAP_LIGHT_FADE), gloss 80.0, spec 0.7 [1.0, 1.0, 1.0], emissive [0.0, 0.0, 0.0] x0.0, env scale 0.7, clamp 3, alpha 1.0 |
| 12 | BSShaderTextureSet | 187 | [0] textures\gvlt\armor\baron\helmet_d.dds; [1] textures\gvlt\armor\baron\helmet_n.dds; [4] textures\cubemaps\metalness_e.dds; [5] textures\gvlt\armor\baron\helmet_m.dds |
| 13 | BSTriShape | 120 | "HelmetTail" 120 verts / 208 tris, flags 14, vertex attrs VERTEX+UV+NORMAL+TANGENT+COLORS+SKINNED, bound sphere (0.045, -0.017, 0.106) r 0.041; transform t=(-3.9733, -8.3294, 109.6273) R=[+93.0586 +0.0000 -0.0000 -0.0000 +93.0586 -0.0001 +0.0000 +0.0001 +93.0586] |
| 14 | BSDismemberSkinInstance | 40 | data #15, partition #16, root #0, bones [3, 4, 5, 6], partitions [(257, 131)] (flags, body part) |
| 15 | NiSkinData | 1489 | global-to-skin t=(0.0427, 0.0895, -1.1780) R=[+0.0107 -0.0000 +0.0000 +0.0000 +0.0107 +0.0000 -0.0000 -0.0000 +0.0107]; bone 0: skin-to-bone t=(-3.9135, 0.1865, -13.7181) R=[+93.0533 -0.9867 +0.1098 +0.9868 +93.0534 -0.0875 -0.1089 +0.0886 +93.0585], sphere (0.064, -0.405, -1.897, 1.722), 40 weights (min 0.060); bone 1: skin-to-bone t=(-3.9135, 1.1865, -10.7181) R=[+93.0533 -0.9867 +0.1098 +0.9868 +93.0534 -0.0875 -0.1089 +0.0886 +93.0585], sphere (-0.062, 0.609, 0.443, 1.434), 48 weights (min 0.135); bone 2: skin-to-bone t=(-3.9135, 2.1865, -7.7181) R=[+93.0533 -0.9867 +0.1098 +0.9868 +93.0534 -0.0875 -0.1089 +0.0886 +93.0585], sphere (0.070, 0.916, 2.047, 2.028), 64 weights (min 0.000); bone 3: skin-to-bone t=(-3.9135, 3.1865, -4.7181) R=[+93.0533 -0.9867 +0.1098 +0.9868 +93.0534 -0.0875 -0.1089 +0.0886 +93.0585], sphere (0.077, 1.067, 2.776, 1.761), 40 weights (min 0.292) |
| 16 | NiSkinPartition | 10468 | 1 partition(s), vertex data copied from the shape (SSE) |
| 17 | BSLightingShaderProperty | 104 | type 1, SF1 0x82400383 (SPECULAR, SKINNED, ENVIRONMENT_MAPPING, RECEIVE_SHADOWS, CAST_SHADOWS, OWN_EMIT, REMAPPABLE_TEXTURES, ZBUFFER_TEST), SF2 0x8031 (ZBUFFER_WRITE, DOUBLE_SIDED, VERTEX_COLORS, ENVMAP_LIGHT_FADE), gloss 80.0, spec 0.7 [1.0, 1.0, 1.0], emissive [0.0, 0.0, 0.0] x0.0, env scale 0.7, clamp 3, alpha 1.0 |
| 18 | BSShaderTextureSet | 187 | [0] textures\gvlt\armor\baron\helmet_d.dds; [1] textures\gvlt\armor\baron\helmet_n.dds; [4] textures\cubemaps\metalness_e.dds; [5] textures\gvlt\armor\baron\helmet_m.dds |
| 19 | NiAlphaProperty | 15 | alpha blending/testing property (not used by the mask) |
| 20 | BSTriShape | 120 | "HelmetVisor" 2322 verts / 4032 tris, flags 14, vertex attrs VERTEX+UV+NORMAL+TANGENT+SKINNED, bound sphere (0.000, 4.029, 121.526) r 10.350; transform t=(0.0001, 0.0004, -0.0002) R=[+1.0000 +0.0000 -0.0000 -0.0000 +1.0000 -0.0000 +0.0000 +0.0000 +1.0000] |
| 21 | BSDismemberSkinInstance | 28 | data #22, partition #23, root #0, bones [1], partitions [(257, 131)] (flags, body part) |
| 22 | NiSkinData | 14059 | global-to-skin t=(-0.0001, -0.0004, 0.0002) R=[+1.0000 -0.0000 +0.0000 +0.0000 +1.0000 +0.0000 -0.0000 -0.0000 +1.0000]; bone 0: skin-to-bone t=(-0.1580, 1.6609, -120.3422) R=[+0.9999 -0.0106 +0.0012 +0.0106 +0.9999 -0.0009 -0.0012 +0.0010 +1.0000], sphere (-0.057, 5.575, 1.187, 10.350), 2322 weights (min 1.000) |
| 23 | NiSkinPartition | 192394 | 1 partition(s), vertex data copied from the shape (SSE) |
| 24 | BSLightingShaderProperty | 104 | type 1, SF1 0x82400383 (SPECULAR, SKINNED, ENVIRONMENT_MAPPING, RECEIVE_SHADOWS, CAST_SHADOWS, OWN_EMIT, REMAPPABLE_TEXTURES, ZBUFFER_TEST), SF2 0x8001 (ZBUFFER_WRITE, ENVMAP_LIGHT_FADE), gloss 80.0, spec 0.7 [1.0, 1.0, 1.0], emissive [0.0, 0.0, 0.0] x0.0, env scale 0.7, clamp 3, alpha 1.0 |
| 25 | BSShaderTextureSet | 187 | [0] textures\gvlt\armor\baron\helmet_d.dds; [1] textures\gvlt\armor\baron\helmet_n.dds; [4] textures\cubemaps\metalness_e.dds; [5] textures\gvlt\armor\baron\helmet_m.dds |

## 5. mask_human_m.nif (ours)

`mask_human_m.nif`: Gamebryo File Format, Version 20.2.0.7, user version 12, BS version 100, 14 blocks, process script "Created with pyNifly", strings ['Scene Root', 'NPC Head [Head]', 'CardboardMask', 'Twine']

| # | block | bytes | contents |
|---|---|---|---|
| 0 | NiNode | 92 | "Scene Root" flags 524302; t=(0.0000, 0.0000, 0.0000) R=[+1.0000 +0.0000 +0.0000 +0.0000 +1.0000 +0.0000 +0.0000 +0.0000 +1.0000] |
| 1 | NiNode | 80 | "NPC Head [Head]" flags 14; t=(-0.0003, -1.5475, 120.3436) R=[+0.9999 +0.0106 -0.0012 -0.0106 +0.9999 +0.0010 +0.0012 -0.0009 +1.0000] |
| 2 | BSTriShape | 120 | "CardboardMask" 1596 verts / 2364 tris, flags 14, vertex attrs VERTEX+UV+NORMAL+TANGENT+SKINNED, bound sphere (-0.572, 12.004, 125.567) r 9.837; transform t=(0.0000, 0.0000, 0.0000) R=[+1.0000 +0.0000 +0.0000 +0.0000 +1.0000 +0.0000 +0.0000 +0.0000 +1.0000] |
| 3 | BSDismemberSkinInstance | 28 | data #4, partition #5, root #0, bones [1], partitions [(257, 55)] (flags, body part) |
| 4 | NiSkinData | 9703 | global-to-skin t=(0.0000, 0.0000, 0.0000) R=[+1.0000 +0.0000 +0.0000 +0.0000 +1.0000 +0.0000 +0.0000 +0.0000 +1.0000]; bone 0: skin-to-bone t=(-0.1582, 1.6605, -120.3420) R=[+0.9999 -0.0106 +0.0012 +0.0106 +0.9999 -0.0009 -0.0012 +0.0010 +1.0000], sphere (-0.709, 13.540, 5.237, 9.837), 1596 weights (min 1.000) |
| 5 | NiSkinPartition | 127366 | 1 partition(s), vertex data copied from the shape (SSE) |
| 6 | BSLightingShaderProperty | 100 | type 0, SF1 0x82400303 (SPECULAR, SKINNED, RECEIVE_SHADOWS, CAST_SHADOWS, OWN_EMIT, REMAPPABLE_TEXTURES, ZBUFFER_TEST), SF2 0x8001 (ZBUFFER_WRITE, ENVMAP_LIGHT_FADE), gloss 30.0, spec 0.6 [1.0, 1.0, 1.0], emissive [0.0, 0.0, 0.0] x1.0, env scale 1.0, clamp 3, alpha 1.0 |
| 7 | BSShaderTextureSet | 114 | [0] textures\CardboardFaceMask\mask_d.dds; [1] textures\CardboardFaceMask\mask_n.dds |
| 8 | BSTriShape | 120 | "Twine" 784 verts / 1068 tris, flags 14, vertex attrs VERTEX+UV+NORMAL+TANGENT+SKINNED, bound sphere (-0.193, 1.237, 123.578) r 11.189; transform t=(0.0000, 0.0000, 0.0000) R=[+1.0000 +0.0000 +0.0000 +0.0000 +1.0000 +0.0000 +0.0000 +0.0000 +1.0000] |
| 9 | BSDismemberSkinInstance | 28 | data #10, partition #11, root #0, bones [1], partitions [(257, 55)] (flags, body part) |
| 10 | NiSkinData | 4831 | global-to-skin t=(0.0000, 0.0000, 0.0000) R=[+1.0000 +0.0000 +0.0000 +0.0000 +1.0000 +0.0000 +0.0000 +0.0000 +1.0000]; bone 0: skin-to-bone t=(-0.1582, 1.6605, -120.3420) R=[+0.9999 -0.0106 +0.0012 +0.0106 +0.9999 -0.0009 -0.0012 +0.0010 +1.0000], sphere (-0.219, 2.779, 3.238, 11.189), 784 weights (min 1.000) |
| 11 | NiSkinPartition | 61470 | 1 partition(s), vertex data copied from the shape (SSE) |
| 12 | BSLightingShaderProperty | 100 | type 0, SF1 0x82400303 (SPECULAR, SKINNED, RECEIVE_SHADOWS, CAST_SHADOWS, OWN_EMIT, REMAPPABLE_TEXTURES, ZBUFFER_TEST), SF2 0x8001 (ZBUFFER_WRITE, ENVMAP_LIGHT_FADE), gloss 12.0, spec 0.3 [1.0, 1.0, 1.0], emissive [0.0, 0.0, 0.0] x1.0, env scale 1.0, clamp 3, alpha 1.0 |
| 13 | BSShaderTextureSet | 116 | [0] textures\CardboardFaceMask\twine_d.dds; [1] textures\CardboardFaceMask\twine_n.dds |

The other three files have the identical 14-block layout; only vertex positions / counts differ
(`CardboardMask` 1596 verts / 2364 tris for human, 1600 / 2368 for beast; `Twine` 784 verts /
1068 tris for human, 819 / 1116 for beast) and the bind is read from their own skeleton (same values).

## 6. Differences, block by block

| block | helmet-SMP.nif | ours | why |
|---|---|---|---|
| header | process script "Exported using Outfit Studio." | "Created with pyNifly" | export-tool metadata string; not read by the engine |
| block list | 26 blocks, 3 shapes, extra bones `H_00..H_03`, `NiStringExtraData`, `NiAlphaProperty` | 14 blocks, 2 shapes | the helmet carries an HDT-SMP physics tail (string extra data naming the physics XML, 4 tail bones) and an alpha-blended double-sided tail shape. The mask needs no physics and is opaque: the cut-out outline is real geometry, not alpha testing, so no NiAlphaProperty |
| 0 NiNode "Scene Root" | 100 bytes, flags 524302 | 92 bytes, flags 524302 | size: the helmet root has one more child and one extra-data ref |
| 1 NiNode "NPC Head [Head]" | flags 524302, transform = bind, 84 bytes | flags 14, transform = bind, 80 bytes | Outfit Studio copies skeleton.nif's node flags (524302); NiflyDLL `addBoneToNifShape` creates the node with 14, which is also what vanilla malehead.nif uses on its bone nodes. The engine binds skin bones to the actor skeleton by name; the node's own flags are irrelevant to skinning. 4 fewer bytes = no child (the helmet's head node parents H_00) |
| BSTriShape | transform t = (0.0001, 0.0004, -0.0002) | exact identity | Outfit Studio rounding: its global-to-skin is the exact inverse of that offset, so both describe "vertices are model space". Ours is exactly what PyNifly's `export_skin` writes for an object at the armature origin. Bound spheres are model-space in both |
| BSTriShape | node flags 14, VERTEX+UV+NORMAL+TANGENT+SKINNED | same | identical (flags set explicitly to the vanilla 14) |
| BSDismemberSkinInstance | bones [head node], partitions [(257, 131)] | bones [head node], partitions [(257, 55)] | same layout and flags; the body part is 131 (SBP_131_HAIR, the helmet hides hair) vs 55 = the ARMO/ARMA biped slot of the mask (config `slot`) |
| NiSkinData | global-to-skin = translation (-0.0001, -0.0004, 0.0002); skin-to-bone t = (-0.1580, 1.6609, -120.3422) | global-to-skin = identity; skin-to-bone t = (-0.1582, 1.6605, -120.3420) | skin-to-bone = inverse(head bind) in both; the 4th-decimal difference is the helmet's tiny global-to-skin offset folded in. Both store per-vertex weights (all 1.0, one bone). Per-bone bound spheres are in bone space in both (ours enclose every vertex - tested) |
| NiSkinPartition | 1 partition, SSE vertex data | same | identical structure (nifly `FinalizeData`) |
| BSLightingShaderProperty | type 1 (EnvironmentMap), 104 bytes | type 0 (Default), 100 bytes | cardboard has no cube-map reflection; the env-map scale float is only serialised for type 1, hence 4 bytes less |
| shader flags 1 | 0x82400383 SPECULAR, SKINNED, ENVIRONMENT_MAPPING, RECEIVE_SHADOWS, CAST_SHADOWS, OWN_EMIT, REMAPPABLE_TEXTURES, ZBUFFER_TEST | 0x82400303 = the same minus ENVIRONMENT_MAPPING | Skinned is required for a skinned mesh; specular stays on with the specular mask in the normal map's alpha (Skyrim convention, `work/atlas_layout.json` "specular" values 0.08-0.4) |
| shader flags 2 | 0x8001 ZBUFFER_WRITE, ENVMAP_LIGHT_FADE | 0x8001 | identical (vanilla armour default) |
| shader values | glossiness 80, spec 0.7, env scale 0.7, emissive black x0.0 | mask: glossiness 30, spec 0.6; twine: glossiness 12, spec 0.3; env scale 1.0; emissive black x1.0 | matte paper/string instead of polished metal. Emissive colour is black, so the multiple has no effect (1.0 is what vanilla malehead.nif stores); env scale is unused without environment mapping. Clamp mode 3 (wrap S/T) and alpha 1.0 as in the helmet |
| BSShaderTextureSet | [0] diffuse, [1] normal, [4] cube map, [5] env mask | [0] `textures\CardboardFaceMask\mask_d.dds` / `twine_d.dds`, [1] `..._n.dds` | no environment map, so slots 4/5 stay empty; 9 slots in both |
| NiAlphaProperty | on the tail | none | opaque mask (see block list) |

## 7. Twine: a cheap party-mask string over the hair

The brief: a thin tube from each cheek edge, round the back of the skull on a sagging, slightly
off-centre path, one side riding higher than the other, crossing *over* the hair. Round 1 hugged
the bald scalp at 0.15 units so helmets hid it; on real NPCs, whose hair meshes stand off the scalp,
that string was buried and the gag lost. Decision for round 2: visibility over hair wins; poking out
of close-fitting helmets and hoods is accepted.

**Path** (`cfm/geom_twine.py`, `string_path`; numbers for human_m with the default config):
* Ends on the kraft back under the tape strips, at +-`string_asymmetry` x 1.0 about the eye line
  (+X, the actor's right, higher: z 124.40 / 123.70), run toward the mask edge and lift off.
* Straight taut span from the lift point to its tangent point on the clearance surface. The side
  heights are +-`string_asymmetry` x 3.0 about the eye line. For humans the contacts sit over the
  ears, and both are raised until the built tube (its centre-line segments sampled every 0.02, after
  smoothing and resampling) clears the ear top (SBP_143_EARS partition, z 124.64) by
  `EAR_TOP_GAP` = 1.0 along the ears' whole front-to-back extent (y -0.25..1.95): contacts at
  z **128.01 (+X) and 125.91 (-X)**, a 2.1-unit (3 cm) lopsided difference; measured gaps 1.00 (-X)
  and 2.65 (+X). 1.0 rather than a hair's breadth because elf and orc ears are pointed race morphs of
  this head (not available here). The higher side never rides above crown - 3.5 (`CROWN_MARGIN`,
  human z 128.35): a larger `string_asymmetry` saturates at a 2.4-unit difference instead of turning
  the string into a headband (`high_side_capped` in the report). The beast anchor has no ears beside
  the head (Argonian: no ear partition; Khajiit ears sit on top, z 126.7-135.2, and the string passes
  under them), so its contacts are simply eye line +-1.05 (126.58 / 124.48).
* Wrap round the back on the clearance surface, drooping `string_sag` x 7.0 = 4.2 below the mean
  contact height with a cosine ease (level at the contacts and at the bottom). The lowest point is
  `string_asymmetry` x 30 deg = 10.5 deg off straight back toward the lower side: human at
  (-1.98, -9.41, **122.77**), under the occipital bump; beast at (-1.83, -6.84, 121.34). With a
  small sag (sag x 7 below half the side difference) the wrap simply runs downhill to the lower
  contact; `back_drop` and `low_point` are measured on the wrapped part only.
* The whole centre line is smoothed with a 0.5-unit Gaussian along its length (rounds the bend over
  the ear and the corner at the mask edge), then resampled adaptively for the tube.

**Clearance surface** (`ClearanceProfile`): at every height, the convex hull of the bald head(s)
within +-0.3 units, behind the outer orbital rim, grown by the clearance + the string radius:
* sides and ears: c_side = `string_clearance_units` (default 1.0) x `CLEARANCE_FACTOR[kind]`
  (human 1.0, beast 0.7);
* back: swelling smoothly (cos^2 over +-70 deg of straight back, then the convex hull again, so it
  stays a taut convex loop) to c_back = c_side x (1 + `BACK_EXTRA[kind]`) = **2.5** for humans
  (beast: no swell). Hair hangs thickest over the back of the head, which is exactly where the droop
  has to show over it;
* in front of the head centre (y > 1.26 human) a smoothstep down to the 0.15-unit minimum gap +
  radius at the orbital rim (y 6.11). The contacts (angles -12.3 deg / -177.8 deg) lie behind the
  centre, so the taper only shapes where a span may touch the temples.

On a bald head the string therefore stands up to 2.6 units (3.7 cm) off the back of the skull: a
stiff, egg-shaped loop rather than an elastic hugging the scalp. That is the price of one static
mesh serving every hair style (render_human_m.png, side and top views).

Beast: the mesh must clear both heads, and the Khajiit head is 0.8-1.6 units bigger than the
Argonian across the back at the string's height, so over the union the string already floats that
much higher on Argonians. The beast clearance is 0.7 x c over the union (Khajiit at the back) so
that on each head it stays within the human limits.

Measured on the exported files (behind the head centre; distance of twine vertices to the head,
min/p10/median/p90/max; d / design = distance over the design clearance at that angle; excess =
distance minus the human-scale clearance c (1 + BACK_EXTRA x back weight)):

| variant / head | distance | d / design | excess over c |
|---|---|---|---|
| human_m / human | 0.98 / 1.06 / 1.73 / 2.50 / 2.61 | 0.98 / 1.00 / 1.03 / 1.10 / 1.17 | -0.03 .. median 0.04 .. max 0.17 |
| beast_m / Argonian | 0.63 / 0.76 / 1.23 / 2.20 / 2.42 | (nearest head) 0.89 / 1.02 / 1.20 / 1.70 / 2.93 | -0.37 .. median 0.23 .. max 1.42 |
| beast_m / Khajiit | 0.67 / 0.74 / 0.98 / 1.85 / 2.20 | | -0.33 .. median -0.02 .. max 1.20 |

**Gates** (03_mesh.py, re-checked on the NIFs by tests/test_nif.py): no twine vertex inside or
within 0.1 of a reference head (human 0.95; Argonian 0.63, Khajiit 0.67); d / design p10 >= 0.7 and
median >= 0.85 on the nearest head; on EACH head excess median <= 0.5 and max <= 1.5 (no halo, no
lasso; a real NPC wears one head); a typical-hair shell: along the whole run round the back and sides
the string surface stays 0.8 x c_side over the nearest bald head (human min 0.98 over a 0.8 shell;
beast 0.60 over 0.56); human: the string surface passes >= 0.9 above the ear tops (design 1.0;
measured 1.00 / 2.65, closest to an ear vertex 1.34); side heights and droop as configured, lowest
point at the back and off-centre; the hair gate below. `test_twine_builds_at_config_extremes`
builds human_m and beast_m at `string_sag` and `string_asymmetry` in {0, 1} (no head or cardboard
intersection, ear gap, crown cap, droop).

**Headgear and hair** (rays from the head centre (0, 1.26, 124.05) through every segment midpoint
of the twine centre line, by length; `mesh_report.json` "headgear", `docs/renders/fit_twine_headgear.png`
draws the length under each surface in red; human_m):

| reference (human skeleton) | covered | visible | where it covers the back/side run: visible | pokes out | notes |
|---|---|---|---|---|---|
| `maleorchair27.nif` MaleOrcHair27 (PyNifly test asset: hair BSDynamicTriShape skinned to NPC Head [Head], `textures\actors\character\hair\HairDreads2.dds`) - **gated** | 16 % (8.4 units of the run) | 96 % | **77 %** | 12 % (max 1.34) | a dreadlock mohawk: no hair at the sides, dreads hanging 2-2.5 units off the back of the scalp at the string's height (3.8-4.1 above z 126; made for the orc head, 17 % of it lies inside the human head). The string crosses over the dread strip; single dreads that stick out 3-3.5 units cross over it in turn (deepest 1.0 under the outer surface). Round 1 of this design (uniform 1.0 clearance) showed 27 % there. Gate: >= 70 % of that covered length visible and no length under hair thinner than c_side - radius; gated while `string_clearance_units` >= 1.0 (smaller values deliberately tuck the string under hair) |
| `helmet-SMP.nif` (vanilla-style helmet) - informational | 77 % | 88 % | 85 % | 60 % (max 1.25) | 20 % of the length runs within 2 radii of the helmet surface |
| `meshes/draugr lich01 hood.nif` (cloth hood, `textures\_00\...`, PyNifly test asset) - informational | 99 % | 90 % | 89 % | 87 % (max 2.58) | 15 % within 2 radii of the surface |

`visible` = outside the mesh or within one string radius (hair) / 0.02 (helmet, hood) of its outer
surface. The beast variants are reported against the same human references (informational; the
beast heads poke through them themselves).

**Expected look with headgear** (accepted; in-game checks in docs/TESTING.md): the string is not
cleanly on top of a helmet or hood - it weaves in and out of the surface, showing over most of the
back and sides and dipping under it in places (red dashes in fit_twine_headgear.png). Where it runs
within a couple of radii of the surface (helmet 20 %, hood 15 % of its length) it may flicker
(z-fighting). On most guards and hooded NPCs the twine will be seen piercing the headgear.

Riding over every dread (`03_mesh.TWINE_OVER_HAIR = ("hair",)`, off by default, tested by
`test_twine_over_hair_alternative`) adds the hair to the clearance hull: 100 % visible over
MaleOrcHair27, but it builds a tent over the mohawk that stands up to 3.8 units off a bald head and
2.3 out of helmet-SMP.nif. The back swell is the compromise: it clears the dread strip where the
string crosses it (2-2.5 units) and stays a smooth convex loop on every other head.

`CitrusFemHead.nif` (PyNifly test asset) is not a human female head: its textures are
`actors\character\cheetahfemale\...` and it has the Khajiit muzzle topology (half-width 8.472 like
maleheadkhajiit.nif). It is only an informational beast_f reference (`mesh_report.json`
"extra_reference_clearance"), with no vertex inside.

Renders: `docs/renders/render_human_m.png` (bald vanilla head), `render_human_m_hair.png` (the same
with MaleOrcHair27 drawn as a matte overlay, `src/render_check.py --extra-nif`), `render_beast_m.png`,
`render_human_m_pose.png` (`--pose 35,15,0`), `fit_mask_*.png`, `fit_twine_headgear.png`.

## 8. Ground model

`mask_go.nif` is not produced (`ground_model: null` in `work/nif_manifest.json`) - by choice, not for
lack of a reference. PyNifly's repository does contain vanilla SSE world models with havok collision
(`tests/tests/SkyrimSE/steeldagger.nif`: BSFadeNode + BSXFlags + bhkCollisionObject -> bhkRigidBody
(layer 5, mass 9, motion system 3, quality type 4) -> bhkListShape of bhkConvexTransformShape/
bhkBoxShape; also noblecrate01.nif, spitpotopen01.nif), and pyn can write those blocks. But a rigid
body for a new object needs values that cannot be copied (mass, inertia tensor and centre for the
mask's shape; a havok material - nif.xml's SkyrimHavokMaterial list (pyn/nifconstants.py) has no
cardboard, so MATERIAL_BOOK or MATERIAL_CLOTH would be a judgment call; unknown fields such as
steeldagger's unknownInt1), and a wrong rigid body can make a dropped item fly off or crash the game
- which cannot be tested here. A world model without collision would float where dropped. The
plugin stage already supports a ground model (ARMO MOD2 from `ground_model`); building one is a
follow-up that should be tried in game. Until then the ARMO has no world model, so a mask the player
drops has no 3D in the world.

## 9. Verification (offline)

`.venv/bin/python -m pytest tests/test_nif.py` reads back every NIF and checks: game / header version,
exact block list (pyn and the raw reader agree), shape and bone names, all weights 1.0, partition ids
[55] with flags 257, texture paths (raw texture sets), shader flags (= helmet minus env map) and
values, vertex attributes incl. tangents, global-to-skin = I and skin-to-bone = inverse(bind) in both
readers, model-space positions = authored within 1e-3, per-bone bound spheres, rigid motion under a
30 deg head-bone rotation about three axes, triangle budget (<= 4000), winding vs normals, unit
normals, clearance against every reference head of the kind (human; Argonian and Khajiit for beast:
no vertex inside; mask >= 0.5, twine >= 0.1 units away), the twine riding `string_clearance_units`
over the back of the head (back swell for humans) without a halo or lasso on any head of the kind,
the typical-hair shell, its side heights / crown cap / droop / off-centre low point, the string
passing EAR_TOP_GAP above the human ear tops (densely sampled), its visibility where MaleOrcHair27
covers it (human), the informational helmet / hood report, the over-hair alternative, the twine at
the string_sag / string_asymmetry extremes, UVs inside their atlas regions, twine UVs read back within
0.005 (u restarts every 4 tiles, so half floats stay exact) with seamless restarts, the
photo-to-model mapping of the front print against the kind's anchor, the combined beast anchor, the
pinned sha256 of every reference NIF (and the fixture fallback for an older checkout), the manifest, the fit proof, the synthetic-fixture build with
reversed polygon order, that `03 --out-dir` / `04 --mesh-dir --out-dir` builds leave work/ and
docs/ untouched, and that PyNifly's own Blender importer reads the file back (meshes, "NPC Head"
vertex group, bone at the bind).
