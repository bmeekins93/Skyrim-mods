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
  all bones agree to 1.8e-5, and unlike the Argonian file its global-to-skin is correct). The Khajiit
  head is not yet in tools/setup.sh's sparse checkout, so a byte-identical copy is vendored in
  `tests/fixtures/vanilla/` (cfm.fit.reference_path prefers third_party when present).
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
| 8 | BSTriShape | 120 | "Twine" 819 verts / 1152 tris, flags 14, vertex attrs VERTEX+UV+NORMAL+TANGENT+SKINNED, bound sphere (-0.209, 2.586, 123.353) r 10.180; transform t=(0.0000, 0.0000, 0.0000) R=[+1.0000 +0.0000 +0.0000 +0.0000 +1.0000 +0.0000 +0.0000 +0.0000 +1.0000] |
| 9 | BSDismemberSkinInstance | 28 | data #10, partition #11, root #0, bones [1], partitions [(257, 55)] (flags, body part) |
| 10 | NiSkinData | 5041 | global-to-skin t=(0.0000, 0.0000, 0.0000) R=[+1.0000 +0.0000 +0.0000 +0.0000 +1.0000 +0.0000 +0.0000 +0.0000 +1.0000]; bone 0: skin-to-bone t=(-0.1582, 1.6605, -120.3420) R=[+0.9999 -0.0106 +0.0012 +0.0106 +0.9999 -0.0009 -0.0012 +0.0010 +1.0000], sphere (-0.249, 4.128, 3.013, 10.180), 819 weights (min 1.000) |
| 11 | NiSkinPartition | 64648 | 1 partition(s), vertex data copied from the shape (SSE) |
| 12 | BSLightingShaderProperty | 100 | type 0, SF1 0x82400303 (SPECULAR, SKINNED, RECEIVE_SHADOWS, CAST_SHADOWS, OWN_EMIT, REMAPPABLE_TEXTURES, ZBUFFER_TEST), SF2 0x8001 (ZBUFFER_WRITE, ENVMAP_LIGHT_FADE), gloss 12.0, spec 0.3 [1.0, 1.0, 1.0], emissive [0.0, 0.0, 0.0] x1.0, env scale 1.0, clamp 3, alpha 1.0 |
| 13 | BSShaderTextureSet | 116 | [0] textures\CardboardFaceMask\twine_d.dds; [1] textures\CardboardFaceMask\twine_n.dds |

The other three files have the identical 14-block layout; only vertex positions / counts differ
(`CardboardMask` 1596 verts / 2364 tris for human, 1600 / 2368 for beast; `Twine` 819-980 verts /
1152-1392 tris) and the bind is read from their own skeleton (same values).

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

## 7. Twine and headgear

The mask is slot 55 so it can be worn together with helmets and hoods, so the string must not show
through them. Measured on helmet-SMP.nif (rays from the head centre, eye height): its outer shell is
~0.35-0.5 units outside the ears, ~0.55-0.85 outside the temples and 0.9-1.6 behind the head; the
face-opening rim is at y ~ 5. A string held 1.5 units off the scalp therefore pokes out all round
the back (538 of 539 covered twine vertices outside in the previous build).

`cfm/geom_twine.py` now rests the string on the head: at every height, the convex hull of the bald
head(s) within +-0.3 units of that height, behind y = eye-pit y + 1 (the outer orbital rim), grown by
0.15 (gap) + the string radius. In front of the rim the string runs straight to the mask; behind it,
it lies along the temple, over the ear and round the back (sagging 1.5 and lopsided +-0.35 as
configured). For the beast mesh the hull is the union of the Argonian and Khajiit heads.

Gates in 03_mesh.py, re-checked on the exported files by tests/test_nif.py:
* no twine vertex inside or closer than 0.1 to any reference head of the kind (human: 0.156; beast:
  Argonian 0.199, Khajiit 0.143);
* no halo: behind the head centre, the median distance to the nearest head is 0.27 and the max
  <= 0.81 (bridges behind the ears) for every variant (limits 0.5 / 1.2; the same path with the old
  1.5-unit clearance gives 1.59 / 2.00 and fails);
* human variants: 0 of ~490 twine vertices in directions helmet-SMP covers lie outside its outer
  shell (closest 0.064 inside, at the ear). See `docs/renders/fit_twine_helmet.png`: with the helmet
  on, no string is visible from the back or side. The beast heads poke through this human helmet
  themselves, so for them the check is reported, not gated. Beast-specific helmets are not available.
* Hair is not modelled: on NPCs with hair the string disappears into the hair at the back, as a real
  string pressed into hair would.

`CitrusFemHead.nif` (PyNifly test asset) is not a human female head: its textures are
`actors\character\cheetahfemale\...` and it has the Khajiit muzzle topology (half-width 8.472 like
maleheadkhajiit.nif). It is only an informational beast_f reference: the mask clears it by 3.0 and
the twine by 0.03 (temple), with no vertex inside.

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
no vertex inside; mask >= 0.5, twine >= 0.1 units away), the twine hugging the back of the head, the
twine under helmet-SMP's shell (human), UVs inside their atlas regions, twine UVs read back within
0.005 (u restarts every 4 tiles, so half floats stay exact) with seamless restarts, the
photo-to-model mapping of the front print against the kind's anchor, the combined beast anchor, the
pinned sha256 of every reference NIF, the manifest, the fit proof, the synthetic-fixture build with
reversed polygon order, that `03 --out-dir` / `04 --mesh-dir --out-dir` builds leave work/ and
docs/ untouched, and that PyNifly's own Blender importer reads the file back (meshes, "NPC Head"
vertex group, bone at the bind).
