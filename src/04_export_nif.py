"""Stage 4b - NIF export: work/mesh/<variant>.npz -> work/nif/mask_{human,beast}_{m,f}.nif
                                                  + work/nif_manifest.json

Replicates PyNifly's own Skyrim SE export path (io_scene_nifly/nif/export_nif.py:
export_shape -> ShaderExporter.export -> export_skin -> set_partitions -> NifFile.save) with plain
pyn calls, no Blender:
  * NifFile.initialize("SKYRIMSE", ...), root NiNode "Scene Root"
  * per shape: createShapeFromData (BSTriShape; NiflyDLL computes tangents because normals are
    given), BSLightingShaderProperty values + textures, save_shader_attributes()
  * skin(): BSDismemberSkinInstance + NiSkinData + NiSkinPartition; transform = identity,
    global-to-skin = identity (vertices are authored in model space, like vanilla armour);
    add_bone("NPC Head [Head]", bind) creates the bone node at its skeleton bind transform;
    skin-to-bone = inverse(bind) (export_skin: inverse(inverse(skin_xf) @ bone_xf), skin_xf = I);
    every vertex weight 1.0; set_partitions([SkyPartition(slot)]) -> body part 55
  * shader flags vanilla-armour style (see docs/NIF_NOTES.md): Specular | Skinned |
    Receive_Shadows | Cast_Shadows | Own_Emit | Remappable_Textures | ZBuffer_Test and
    ZBuffer_Write | EnvMap_Light_Fade; specular mask = normal-map alpha; no env map, no alpha.
The head-bone bind comes from the variant's own skeleton (skeleton.nif, skeleton_female.nif,
skeletonbeast.nif, skeletonbeast_female.nif).

No ground model is written (ground_model null, see docs/NIF_NOTES.md section 7).

Run from the repo root:  .venv/bin/python src/04_export_nif.py [--mesh-dir D] [--out-dir D] [--manifest P]
  (defaults work/mesh, work/nif and <out-dir>/../nif_manifest.json = work/nif_manifest.json)
Block dumps (docs/NIF_NOTES.md):  .venv/bin/python src/04_export_nif.py --dump <nif> [<nif> ...]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from cfm.fit import VARIANT_SKELETON  # noqa: E402
from cfm.geom_xform import HEAD_BONE, NifFile, mat_to_xf, pynifly, skeleton_binds  # noqa: E402
from cfm.geometry import MASK_SHAPE, TWINE_SHAPE, VARIANTS, load_npz  # noqa: E402
from cfm.paths import MOD_NAME, SKELETONS, WORK, load_config  # noqa: E402
from pyn.nifconstants import ShaderFlags1, ShaderFlags2  # noqa: E402

MESH_DIR = WORK / "mesh"
NIF_DIR = WORK / "nif"
TEX_DIR = f"textures\\{MOD_NAME}"
NIF_NAMES = {"human_m": "mask_human_m.nif", "human_f": "mask_human_f.nif",
             "beast_m": "mask_beast_m.nif", "beast_f": "mask_beast_f.nif"}

FLAGS1 = (ShaderFlags1.SPECULAR | ShaderFlags1.SKINNED | ShaderFlags1.RECEIVE_SHADOWS
          | ShaderFlags1.CAST_SHADOWS | ShaderFlags1.OWN_EMIT | ShaderFlags1.REMAPPABLE_TEXTURES
          | ShaderFlags1.ZBUFFER_TEST)
FLAGS2 = ShaderFlags2.ZBUFFER_WRITE | ShaderFlags2.ENVMAP_LIGHT_FADE
NODE_FLAGS = 14               # NiAVObject flags on every node/shape of vanilla malehead.nif and on
                              # helmet-SMP.nif's shapes
SHADER_DEFAULT = 0            # BSLightingShaderType "Default" (helmet-SMP uses 1 = EnvironmentMap)
CLAMP_WRAP_S_WRAP_T = 3

# Matte cardboard / dull twine. Vanilla-like values; the normal-map alpha masks specular per texel.
MATERIALS = {
    MASK_SHAPE: {"diffuse": "mask_d.dds", "normal": "mask_n.dds", "glossiness": 30.0, "spec_strength": 0.6},
    TWINE_SHAPE: {"diffuse": "twine_d.dds", "normal": "twine_n.dds", "glossiness": 12.0, "spec_strength": 0.3},
}


def texture_path(name: str) -> str:
    return f"{TEX_DIR}\\{name}"


def set_shader(shape, mat: dict) -> None:
    """BSLightingShaderProperty values and textures (PyNifly ShaderExporter equivalent)."""
    sh = shape.shader
    p = sh.properties
    p.bufType = pynifly.PynBufferTypes.BSLightingShaderPropertyBufType
    p.Shader_Type = SHADER_DEFAULT
    p.Shader_Flags_1 = int(FLAGS1)
    p.Shader_Flags_2 = int(FLAGS2)
    p.Glossiness = mat["glossiness"]
    p.Spec_Str = mat["spec_strength"]
    for i in range(3):
        p.Spec_Color[i] = 1.0
        p.Emissive_Color[i] = 0.0
    p.Emissive_Color[3] = 0.0
    p.Emissive_Mult = 1.0
    p.Alpha = 1.0
    p.Env_Map_Scale = 1.0
    p.Soft_Lighting = 0.3
    p.Rim_Light_Power = 2.0
    p.UV_Offset_U = p.UV_Offset_V = 0.0
    p.UV_Scale_U = p.UV_Scale_V = 1.0
    p.textureClampMode = CLAMP_WRAP_S_WRAP_T
    sh.set_texture("Diffuse", texture_path(mat["diffuse"]))
    sh.set_texture("Normal", texture_path(mat["normal"]))
    shape.save_shader_attributes()


def skin_to_head(shape, n_verts: int, n_tris: int, bind: np.ndarray, slot: int) -> None:
    """PyNifly export_skin for one bone, vertices in model space (skin frame = model frame)."""
    shape.skin()
    shape.transform = mat_to_xf(np.eye(4))
    shape.set_global_to_skin(mat_to_xf(np.eye(4)))
    shape.add_bone(HEAD_BONE, mat_to_xf(bind))
    shape.set_skin_to_bone_xform(HEAD_BONE, mat_to_xf(np.linalg.inv(bind)))
    shape.setShapeWeights(HEAD_BONE, [(i, 1.0) for i in range(n_verts)])
    part = pynifly.SkyPartition(part_id=slot, namedict=shape.file.dict)
    shape.set_partitions([part], [slot] * n_tris)


def _tuples(a, kind=float) -> list:
    """ctypes array assignment in pyn wants tuples, not lists."""
    return [tuple(kind(x) for x in row) for row in np.asarray(a)]


def export_variant(variant: str, mesh_path: Path, out_path: Path, slot: int) -> dict:
    mesh = load_npz(mesh_path)
    bind = skeleton_binds(SKELETONS / VARIANT_SKELETON[variant], [HEAD_BONE])[HEAD_BONE]
    pynifly.NifFile.clear_log()
    nif = NifFile()
    nif.initialize("SKYRIMSE", str(out_path), "NiNode", "Scene Root")
    info = {}
    for shape_name in (MASK_SHAPE, TWINE_SHAPE):
        v, t, uv, n = mesh.shape_arrays(shape_name)
        if len(v) > 65535:
            raise ValueError(f"{shape_name}: {len(v)} vertices exceed the 16-bit index limit")
        props = pynifly.NiObject.block_types["BSTriShape"].getbuf()
        props.flags = NODE_FLAGS
        shape = nif.createShapeFromData(shape_name, _tuples(v), _tuples(t, int), _tuples(uv), _tuples(n),
                                        props=props, parent=nif.root)
        set_shader(shape, MATERIALS[shape_name])
        skin_to_head(shape, len(v), len(t), bind, slot)
        info[shape_name] = {"vertices": int(len(v)), "triangles": int(len(t))}
    nif.save()
    log = [m for m in NifFile.message_log().split("\n") if m and not m.startswith("Info: Loaded skeleton")]
    if log:
        print(f"{variant}: NiflyDLL log:\n  " + "\n  ".join(log))
    return {"variant": variant, "skeleton": VARIANT_SKELETON[variant], "shapes": info,
            "inputs_are_fixtures": bool(mesh.report.get("inputs_are_fixtures", False)), "log": log}


def write_manifest(path: Path) -> dict:
    manifest = {
        "meshes_subdir": MOD_NAME,
        "models": {v: f"{MOD_NAME}\\{NIF_NAMES[v]}" for v in VARIANTS},
        "ground_model": None,
        "textures": [texture_path(m[k]) for m in MATERIALS.values() for k in ("diffuse", "normal")],
    }
    path.write_text(json.dumps(manifest, indent=1), encoding="utf-8")
    return manifest


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--variants", nargs="*", default=list(VARIANTS))
    ap.add_argument("--mesh-dir", type=Path, default=MESH_DIR, help="03_mesh.py output (default work/mesh)")
    ap.add_argument("--out-dir", type=Path, default=NIF_DIR, help="NIF output (default work/nif)")
    ap.add_argument("--manifest", type=Path, help="manifest path (default: nif_manifest.json next to --out-dir)")
    ap.add_argument("--dump", type=Path, nargs="+", metavar="NIF",
                    help="print a block-by-block markdown dump of these NIFs and exit")
    args = ap.parse_args()
    if args.dump:
        from cfm.geom_nifinfo import markdown_dump
        print("\n\n".join(markdown_dump(p) for p in args.dump))
        return 0
    slot = int(load_config()["slot"])
    manifest_path = args.manifest or args.out_dir.parent / "nif_manifest.json"
    args.out_dir.mkdir(parents=True, exist_ok=True)
    for variant in args.variants:
        mesh_path = args.mesh_dir / f"{variant}.npz"
        if not mesh_path.exists():
            print(f"missing {mesh_path}; run src/03_mesh.py first", file=sys.stderr)
            return 1
        res = export_variant(variant, mesh_path, args.out_dir / NIF_NAMES[variant], slot)
        print(f"{variant}: {res['shapes']} -> {args.out_dir / NIF_NAMES[variant]}")
        if res["inputs_are_fixtures"]:
            print(f"WARNING: {variant} was built from SYNTHETIC fixtures - placeholder mesh", file=sys.stderr)
    manifest = write_manifest(manifest_path)
    print(f"wrote {manifest_path}: {manifest['models']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
