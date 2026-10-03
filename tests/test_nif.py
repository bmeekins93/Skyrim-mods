"""Read back every exported NIF and prove it is engine-correct (as far as offline checks can).

Two independent readers are used: PyNifly's pyn layer (NiflyDLL) and cfm.geom_nifinfo.RawNif, a
small byte-level parser written from niftools nif.xml that shares no code with NiflyDLL.

Prerequisite: .venv/bin/python src/03_mesh.py && .venv/bin/python src/04_export_nif.py
Run:          .venv/bin/python -m pytest tests/test_nif.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from cfm import fit as fitmod  # noqa: E402
from cfm.geom_mesh import closest_points, winding_agreement, winding_number  # noqa: E402
from cfm.geom_nifinfo import RawNif, block_list, vertex_flags  # noqa: E402
from cfm.geom_xform import (HEAD_BONE, NifFile, apply, rotation_about, shape_skin,  # noqa: E402
                            skeleton_binds, skinned_model_positions)
from cfm.geom_twine import UV_WRAP_TILES  # noqa: E402
from cfm.geometry import (MASK_SHAPE, TWINE_SHAPE, VARIANTS, build_variant, load_npz, shell_check)  # noqa: E402
from cfm.paths import PYNIFLY_TESTS, SKELETONS, WORK, load_config  # noqa: E402
from pyn.nifconstants import ShaderFlags1, ShaderFlags2  # noqa: E402

NIF_DIR = WORK / "nif"
MESH_DIR = WORK / "mesh"
FIXTURES = ROOT / "tests" / "fixtures"
NIF_NAMES = {v: f"mask_{v}.nif" for v in VARIANTS}
SHAPES = [MASK_SHAPE, TWINE_SHAPE]
TEX = "textures\\CardboardFaceMask\\"
TEXTURES = {MASK_SHAPE: [TEX + "mask_d.dds", TEX + "mask_n.dds"],
            TWINE_SHAPE: [TEX + "twine_d.dds", TEX + "twine_n.dds"]}
PER_SHAPE_BLOCKS = ["BSTriShape", "BSDismemberSkinInstance", "NiSkinData", "NiSkinPartition",
                    "BSLightingShaderProperty", "BSShaderTextureSet"]
TRI_BUDGET = 4000
HELMET = PYNIFLY_TESTS / "SkyrimSE" / "helmet-SMP.nif"


def _nif_path(variant: str) -> Path:
    p = NIF_DIR / NIF_NAMES[variant]
    if not p.exists():
        pytest.fail(f"{p} missing - run src/03_mesh.py and src/04_export_nif.py first")
    return p


@pytest.fixture(scope="module")
def cfg():
    return load_config()


@pytest.fixture(scope="module")
def fit():
    return fitmod.load_fit()


@pytest.fixture(scope="module")
def heads():
    """Reference heads per mask kind: human -> [malehead], beast -> [Argonian, Khajiit]."""
    return {k: fitmod.load_kind_heads(k) for k in fitmod.KIND_HEADS}


@pytest.fixture(scope="module")
def helmet():
    return fitmod.load_helmet()[:2]


@pytest.fixture(scope="module", params=VARIANTS)
def variant(request):
    return request.param


@pytest.fixture(scope="module")
def nif(variant):
    return NifFile(str(_nif_path(variant)))


@pytest.fixture(scope="module")
def raw(variant):
    return RawNif(_nif_path(variant))


@pytest.fixture(scope="module")
def authored(variant):
    return load_npz(MESH_DIR / f"{variant}.npz")


@pytest.fixture(scope="module")
def bind(variant):
    return skeleton_binds(SKELETONS / fitmod.VARIANT_SKELETON[variant], [HEAD_BONE])


def _shape(nif, name):
    return nif.shape_dict[name]


def _model_positions(shape, bind):
    return skinned_model_positions(np.array(shape.verts), shape_skin(shape), bind)


# --- file / block structure -------------------------------------------------------------------------


def test_game_and_version(nif, raw):
    assert nif.game == "SKYRIMSE"
    assert raw.header_string == "Gamebryo File Format, Version 20.2.0.7"
    assert (raw.version, raw.user_version, raw.bs_version) == (0x14020007, 12, 100)
    assert raw.end_of_blocks + 8 == len(raw.data)   # footer: 1 root + root ref


def test_block_types(nif, raw):
    expected = ["NiNode", "NiNode"] + PER_SHAPE_BLOCKS * 2
    assert raw.blocks() == expected
    assert [b for _, b in block_list(nif)] == expected        # NiflyDLL agrees with the raw reader
    assert "NiAlphaProperty" not in raw.blocks()
    assert set(raw.strings) == {"Scene Root", HEAD_BONE, MASK_SHAPE, TWINE_SHAPE}


def test_shapes_bones_and_weights(nif, raw):
    assert [s.name for s in nif.shapes] == SHAPES
    for s in nif.shapes:
        assert s.blockname == "BSTriShape"
        assert s.skin_instance_name == "BSDismemberSkinInstance"
        assert s.bone_names == [HEAD_BONE]
        w = s.bone_weights[HEAD_BONE]
        assert sorted(i for i, _ in w) == list(range(len(s.verts)))
        assert np.allclose([x for _, x in w], 1.0)
    for i, b in enumerate(raw.blocks()):
        if b == "BSDismemberSkinInstance":
            si = raw.skin_instance(i)
            assert si["skeleton_root"] == 0 and len(si["bones"]) == 1
            assert raw.block_type(si["bones"][0]) == "NiNode"
            sd = raw.skin_data(si["data"])
            assert sd["has_vertex_weights"] == 1 and len(sd["bones"]) == 1
            assert np.allclose([x for _, x in sd["bones"][0]["weights"]], 1.0)


def test_partitions(nif, raw, cfg):
    slot = int(cfg["slot"])
    for s in nif.shapes:
        assert [p.id for p in s.partitions] == [slot]
        assert set(s.partition_tris) == {0}
    for i, b in enumerate(raw.blocks()):
        if b == "BSDismemberSkinInstance":
            # 257 = PF_EDITOR_VISIBLE | PF_START_NET_BONESET, as in vanilla helmet-SMP.nif
            assert raw.skin_instance(i)["partitions"] == [{"flags": 257, "body_part": slot}]


def test_texture_paths(nif, raw):
    sets = [raw.texture_set(i) for i, b in enumerate(raw.blocks()) if b == "BSShaderTextureSet"]
    assert [ts[:2] for ts in sets] == [TEXTURES[MASK_SHAPE], TEXTURES[TWINE_SHAPE]]
    assert all(len(ts) == 9 and not any(ts[2:]) for ts in sets)
    for s in nif.shapes:
        assert s.textures["Diffuse"] == TEXTURES[s.name][0]
        assert s.textures["Normal"] == TEXTURES[s.name][1]


def test_shader(nif):
    helmet = NifFile(str(HELMET)).shapes[0].shader.properties
    for s in nif.shapes:
        p = s.shader.properties
        assert s.shader_block_name == "BSLightingShaderProperty"
        f1, f2 = ShaderFlags1(p.Shader_Flags_1), ShaderFlags2(p.Shader_Flags_2)
        assert ShaderFlags1.SKINNED in f1 and ShaderFlags1.SPECULAR in f1
        assert ShaderFlags1.ENVIRONMENT_MAPPING not in f1
        assert ShaderFlags1.MODEL_SPACE_NORMALS not in f1
        assert ShaderFlags2.VERTEX_COLORS not in f2 and ShaderFlags2.DOUBLE_SIDED not in f2
        # vanilla helmet flags minus its environment map
        assert int(p.Shader_Flags_1) == helmet.Shader_Flags_1 & ~int(ShaderFlags1.ENVIRONMENT_MAPPING)
        assert int(p.Shader_Flags_2) == helmet.Shader_Flags_2
        assert p.Shader_Type == 0 and p.textureClampMode == 3
        assert 0 < p.Spec_Str <= 1.0 and 5 <= p.Glossiness <= 50
        assert p.Alpha == 1.0 and not s.has_alpha_property


def test_vertex_format_has_tangents(nif):
    for s in nif.shapes:
        flags = vertex_flags(int(s.properties.vertexDesc))
        assert {"VERTEX", "UV", "NORMAL", "TANGENT", "SKINNED"} <= set(flags)
        assert "COLORS" not in flags


# --- skin math ----------------------------------------------------------------------------------------


def test_skin_transforms(nif, raw, bind):
    b = bind[HEAD_BONE]
    for s in nif.shapes:
        sk = shape_skin(s)
        assert np.allclose(sk["global_to_skin"], np.eye(4), atol=1e-6)
        assert np.allclose(sk["skin_to_bone"][HEAD_BONE], np.linalg.inv(b), atol=1e-4)
        assert np.allclose(np.array(s.transform.translation[:]), 0) and s.transform.scale == 1.0
    # the bone node in the file sits at the skeleton bind, like helmet-SMP.nif's
    node = nif.nodes[HEAD_BONE]
    from cfm.geom_xform import xf_to_mat
    assert np.allclose(xf_to_mat(node.transform), b, atol=1e-4)
    for i, blk in enumerate(raw.blocks()):        # raw bytes say the same
        if blk == "NiSkinData":
            sd = raw.skin_data(i)
            assert np.allclose(sd["global_to_skin"], np.eye(4), atol=1e-6)
            assert np.allclose(sd["bones"][0]["skin_to_bone"], np.linalg.inv(b), atol=1e-4)


def test_model_positions_equal_authored(nif, authored, bind):
    for name in SHAPES:
        v_auth = authored.shape_arrays(name)[0]
        s = _shape(nif, name)
        assert len(s.verts) == len(v_auth)
        assert np.abs(_model_positions(s, bind) - v_auth).max() < 1e-3
        assert np.abs(np.array(s.verts) - v_auth).max() < 1e-3   # skin space == model space here


def test_bone_bound_spheres_enclose_vertices(nif, raw):
    """NiSkinData per-bone spheres (bone space) are used for culling; they must hold every vertex."""
    shapes = iter(nif.shapes)
    for i, blk in enumerate(raw.blocks()):
        if blk == "NiSkinData":
            s = next(shapes)
            sd = raw.skin_data(i)["bones"][0]
            pts = apply(sd["skin_to_bone"], np.array(s.verts))
            c, r = np.array(sd["bound_sphere"][:3]), sd["bound_sphere"][3]
            assert np.linalg.norm(pts - c, axis=1).max() <= r + 1e-3


@pytest.mark.parametrize("axis", [(1, 0, 0), (0, 0, 1), (0.3, 0.8, 0.5)])
def test_head_rotation_moves_mask_rigidly(nif, bind, axis):
    """Turn the head bone 30 degrees about its own pivot: every vertex must follow rigidly."""
    b = bind[HEAD_BONE]
    pose = np.eye(4)
    pose[:3, :3] = rotation_about(axis, 30.0)
    posed_bone = {HEAD_BONE: b @ pose}                  # rotation in bone space, about its origin
    pivot = b[:3, 3]
    world_rot = b[:3, :3] @ pose[:3, :3] @ np.linalg.inv(b[:3, :3])
    for s in nif.shapes:
        rest = _model_positions(s, bind)
        moved = skinned_model_positions(np.array(s.verts), shape_skin(s), posed_bone)
        expected = (rest - pivot) @ world_rot.T + pivot
        assert np.abs(moved - expected).max() < 1e-4
        assert np.allclose(np.linalg.norm(moved - pivot, axis=1), np.linalg.norm(rest - pivot, axis=1), atol=1e-4)
        assert np.abs(moved - rest).max() > 1.0          # it really moved


# --- geometry quality ---------------------------------------------------------------------------------


def test_triangle_budget(nif):
    assert sum(len(s.tris) for s in nif.shapes) <= TRI_BUDGET


def test_winding_matches_normals(nif):
    vanilla = NifFile(str(HELMET)).shapes[0]
    assert winding_agreement(np.array(vanilla.verts), np.array(vanilla.tris), np.array(vanilla.normals)) > 0.99
    for s in nif.shapes:
        n = np.array(s.normals)
        assert np.abs(np.linalg.norm(n, axis=1) - 1).max() < 0.02    # normals are stored as bytes
        assert winding_agreement(np.array(s.verts), np.array(s.tris), n) >= 0.99


def test_normals_unit_in_authored(authored):
    for p in authored.parts:
        assert np.abs(np.linalg.norm(p.normals, axis=1) - 1).max() < 1e-6


def test_clearance_vs_head(nif, variant, bind, fit, heads):
    """Against EVERY reference head of the kind (the beast mesh serves Argonians and Khajiit)."""
    kind_heads = heads[fit["variant_kind"][variant]]
    assert [h.kind for h in kind_heads] == (["human"] if variant.startswith("human") else ["argonian", "khajiit"])
    for head in kind_heads:
        for s, min_d in ((MASK_SHAPE, 0.5), (TWINE_SHAPE, 0.1)):
            v = _model_positions(_shape(nif, s), bind)
            assert (winding_number(v, head.verts, head.tris) < 0.5).all(), f"{s} vertex inside the {head.kind} head"
            d, _, _ = closest_points(v, head.verts, head.tris)
            assert d.min() >= min_d, f"{s} only {d.min():.3f} from the {head.kind} head"


def test_twine_hugs_back_of_head(nif, variant, bind, fit, heads):
    """No halo: behind the head centre the string rests just off the nearest bald head."""
    kind = fit["variant_kind"][variant]
    v = _model_positions(_shape(nif, TWINE_SHAPE), bind)
    back = v[v[:, 1] < fit["anchors"][kind]["centre"][1]]
    assert len(back) > 100
    d = np.min([closest_points(back, h.verts, h.tris)[0] for h in heads[kind]], axis=0)
    assert np.median(d) <= 0.5 and d.max() <= 1.2, (np.median(d), d.max())


def test_twine_inside_helmet_shell(nif, variant, bind, fit, helmet):
    """Human variants: along rays from the head centre, every twine vertex in a direction the vanilla-
    style helmet-SMP.nif covers lies under its outer shell, so helmets hide the string. (The beast
    heads poke through this human helmet themselves, so it says nothing about them.)"""
    if not variant.startswith("human"):
        pytest.skip("helmet-SMP.nif is a human helmet")
    a = fit["anchors"]["human"]
    v = _model_positions(_shape(nif, TWINE_SHAPE), bind)
    res = shell_check(v, [0.0, a["centre"][1], a["eye_line_z"]], *helmet, tol=0.02)
    assert res["covered"] > 0.4 * len(v)
    assert res["outside"] == 0, res


def _layout(authored):
    rep = authored.report
    path = WORK / "atlas_layout.json"
    if rep.get("inputs_are_fixtures") or not path.exists():
        path = FIXTURES / "atlas_layout_synthetic.json"
    return json.loads(path.read_text(encoding="utf-8"))


def test_uvs_inside_atlas_regions(nif, authored):
    regions = _layout(authored)["regions"]
    eps = 1e-3
    s = _shape(nif, MASK_SHAPE)
    uv, t = np.array(s.uvs), np.array(s.tris)
    assert np.abs(uv - authored.shape_arrays(MASK_SHAPE)[2]).max() < 1e-3      # half-float storage
    tri_uv = uv[t]
    in_any = np.zeros(len(t), bool)
    for r in regions.values():
        u0, v0, u1, v1 = r["uv_rect"]
        ok = ((tri_uv[..., 0] >= u0 - eps) & (tri_uv[..., 0] <= u1 + eps)
              & (tri_uv[..., 1] >= v0 - eps) & (tri_uv[..., 1] <= v1 + eps)).all(axis=1)
        in_any |= ok
    assert in_any.all(), f"{(~in_any).sum()} triangles straddle atlas regions"
    # each authored part stays inside its own region
    for p in authored.parts:
        if p.shape == MASK_SHAPE:
            u0, v0, u1, v1 = regions[p.region]["uv_rect"]
            assert (p.uvs[:, 0] >= u0 - eps).all() and (p.uvs[:, 0] <= u1 + eps).all()
            assert (p.uvs[:, 1] >= v0 - eps).all() and (p.uvs[:, 1] <= v1 + eps).all()
    tw = np.array(_shape(nif, TWINE_SHAPE).uvs)
    assert tw[:, 0].min() >= -1e-3 and tw[:, 1].min() >= -1e-3 and tw[:, 1].max() <= 1 + 1e-3
    assert tw[:, 0].max() < 8 and tw[:, 0].max() < UV_WRAP_TILES + 2   # u restarts: half-float step 2^-8


def test_twine_uvs_survive_half_float_storage(nif, authored):
    """BSTriShape stores UVs as half floats; the twine's restarted u keeps the error tiny."""
    uv = np.array(_shape(nif, TWINE_SHAPE).uvs)
    err = np.abs(uv - authored.shape_arrays(TWINE_SHAPE)[2]).max()
    assert err < 0.005, err


def test_twine_uv_restart_is_seamless(authored):
    """Where u restarts, the ring is duplicated at the same position with u reduced by whole tiles."""
    tw = next(p for p in authored.parts if p.name == "twine")
    key = np.round(tw.verts * 1e5).astype(np.int64)
    _, inv, counts = np.unique(key, axis=0, return_inverse=True, return_counts=True)
    inv = inv.reshape(-1)
    restarts = 0
    for g in np.nonzero(counts > 1)[0]:
        u = tw.uvs[inv == g, 0]
        du = u.max() - u.min()
        if du > 1e-6:                                   # not the cap / v-seam duplicates
            assert abs(du - round(du)) < 1e-6 and round(du) >= 1
            restarts += 1
    assert restarts > 0


def test_front_print_follows_photo_mapping(authored, fit):
    """Front-cap UVs, inverted to photo pixels, must land where the contract mapping puts them:
    image x -> -X (through the cylindrical bend), image y -> -Z, eye midpoint -> (0, eye line)."""
    rep = authored.report
    m, bend = rep["mapping"], rep["bend"]
    front = next(p for p in authored.parts if p.name == "front_cap")
    reg = _layout(authored)["regions"]["front"]
    (u0, v0, u1, v1), (x0, y0, x1, y1) = reg["uv_rect"], reg["image_rect"]
    px = x0 + (front.uvs[:, 0] - u0) / (u1 - u0) * (x1 - x0)
    py = y0 + (front.uvs[:, 1] - v0) / (v1 - v0) * (y1 - y0)
    s, (ex, ey) = m["units_per_px"], m["eye_px"]
    a, h = -(px - ex) * s, -(py - ey) * s
    rm = bend["radius_back"] + 0.5 * bend["thickness"]
    rho = bend["radius_back"] + bend["thickness"]
    expect_x = rho * np.sin(a / rm)
    assert np.abs(front.verts[:, 0] - expect_x).max() < 1e-4
    assert np.abs(front.verts[:, 2] - (bend["z_eye"] + h)).max() < 1e-4
    anchor = fit["anchors"][fit["variant_kind"][authored.variant]]
    assert bend["z_eye"] == anchor["eye_line_z"]
    assert abs(bend["y_back_at_centre"] - anchor["nose_tip"][1] - load_config()["forward_offset_units"]) < 1e-9


# --- manifest, fit proof, fixture path ------------------------------------------------------------------


def test_manifest():
    man = json.loads((WORK / "nif_manifest.json").read_text(encoding="utf-8"))
    assert man["meshes_subdir"] == "CardboardFaceMask"
    assert man["models"] == {v: f"CardboardFaceMask\\{NIF_NAMES[v]}" for v in VARIANTS}
    assert man["ground_model"] is None
    assert sorted(man["textures"]) == sorted(TEXTURES[MASK_SHAPE] + TEXTURES[TWINE_SHAPE])
    for v in VARIANTS:
        assert (NIF_DIR / NIF_NAMES[v]).exists()


def test_fit_transform_proof(fit):
    assert fitmod.check_fit(fit) == []
    human = fit["heads"]["human"]
    hc = human["helmet_check"]
    assert hc["outside_outer_shell"] == 0 and hc["under_outer_shell"] > 50 and hc["crown_gap_z"] > 0
    assert abs(human["eye_line_z"] - human["eye_line_crosscheck"]["NPCEyeBone_z"]) < 0.25
    assert set(fit["heads"]) == {"human", "argonian", "khajiit"}
    for h in fit["heads"].values():
        assert h["skin"]["bone_disagreement_max"] < 1e-3
        assert h["chin_z"] < h["eye_line_z"] < h["crown_z"]
        assert abs(h["crown_point"][0]) <= fitmod.CROWN_STRIP      # scalp, not the Khajiit ear tips


def test_beast_anchor_combines_argonian_and_khajiit(fit):
    a, ar, kh = fit["anchors"]["beast"], fit["heads"]["argonian"], fit["heads"]["khajiit"]
    assert a["heads"] == ["argonian", "khajiit"]
    assert a["nose_tip"][1] == max(ar["nose_tip"][1], kh["nose_tip"][1])     # clears both snouts
    for key in ("eye_line_z", "chin_z", "crown_z"):
        assert abs(a[key] - 0.5 * (ar[key] + kh[key])) < 1e-3
    assert fit["anchors"]["human"]["eye_line_z"] == fit["heads"]["human"]["eye_line_z"]


def test_reference_heads_are_pinned():
    """Every reference NIF (third_party or the vendored tests/fixtures/vanilla copy) matches its sha256."""
    import hashlib
    for rel in [r for r, _ in fitmod.HEAD_SOURCES.values()] + [fitmod.HELMET_SOURCE[0]]:
        path = fitmod.reference_path(rel)
        assert hashlib.sha256(path.read_bytes()).hexdigest() == fitmod.REFERENCE_SHA256[rel]
    vendored = FIXTURES / "vanilla" / "maleheadkhajiit.nif"
    assert vendored.exists()
    rel = fitmod.HEAD_SOURCES["khajiit"][0]
    assert hashlib.sha256(vendored.read_bytes()).hexdigest() == fitmod.REFERENCE_SHA256[rel]


def test_synthetic_fixture_builds_and_ignores_orientation(cfg, fit, heads):
    outline = json.loads((FIXTURES / "outline_synthetic.json").read_text(encoding="utf-8"))
    layout = json.loads((FIXTURES / "atlas_layout_synthetic.json").read_text(encoding="utf-8"))
    hs = [(h.kind, h.verts, h.tris) for h in heads["human"]]
    a = build_variant("human_m", outline, layout, fit, hs, cfg)
    flipped = json.loads(json.dumps(outline))
    mode = cfg.get("silhouette_mode") or outline["selected_mode"]
    flipped["modes"][mode]["polygon"] = flipped["modes"][mode]["polygon"][::-1]
    b = build_variant("human_m", flipped, layout, fit, hs, cfg)
    for mesh in (a, b):
        r = mesh.report
        assert r["triangles_total"] <= TRI_BUDGET
        cl = r["clearance"]["human"]
        assert cl[MASK_SHAPE]["inside_head"] == 0 and cl[TWINE_SHAPE]["inside_head"] == 0
        assert r["twine_vs_slab"]["vertices_inside_cardboard"] == 0
        front = next(p for p in mesh.parts if p.name == "front_cap")
        assert winding_agreement(front.verts, front.tris, front.normals) == 1.0
        assert (front.normals[:, 1] > 0.8).all()     # front faces forward (+Y) whatever the input order
    assert abs(a.report["mask_bbox"][0][0] - b.report["mask_bbox"][0][0]) < 0.05


def test_scratch_out_dirs_leave_work_and_docs_alone(tmp_path):
    """03 --out-dir and 04 --mesh-dir/--out-dir write only where they are told (renders, manifest)."""
    import subprocess
    renders = ROOT / "docs" / "renders"
    watched = [WORK / "nif_manifest.json", *renders.glob("fit_mask_*.png"), renders / "fit_twine_helmet.png",
               *NIF_DIR.glob("*"), *MESH_DIR.glob("*")]
    before = {p: p.stat().st_mtime_ns for p in watched if p.exists()}
    run = dict(cwd=ROOT, check=True, capture_output=True, timeout=900)
    subprocess.run([sys.executable, "src/03_mesh.py", "--variants", "human_m", "--out-dir", str(tmp_path / "mesh")], **run)
    subprocess.run([sys.executable, "src/04_export_nif.py", "--variants", "human_m", "--mesh-dir", str(tmp_path / "mesh"),
                    "--out-dir", str(tmp_path / "out" / "nif")], **run)
    assert (tmp_path / "mesh" / "fit_mask_human_m.png").exists() and (tmp_path / "mesh" / "fit_twine_helmet.png").exists()
    assert (tmp_path / "out" / "nif" / "mask_human_m.nif").exists() and (tmp_path / "out" / "nif_manifest.json").exists()
    assert {p: p.stat().st_mtime_ns for p in watched if p.exists()} == before


# --- third reader: PyNifly's own Blender importer -----------------------------------------------------

BLENDER = ROOT / "third_party" / "blender" / "blender"
BLENDER_SCRIPT = r'''
import sys, types, ctypes, json, bpy, addon_utils
# PyNifly's addon touches Win32 APIs at import time (blender_defs.GetShortPathNameW, gamefinder's
# registry lookup); stub them so the importer itself can run on Linux.
class _F:
    argtypes = None
    restype = None
    def __call__(self, *a):
        return 0
ctypes.windll = types.SimpleNamespace(kernel32=types.SimpleNamespace(GetShortPathNameW=_F()))
wr = types.ModuleType("winreg")
def _nokey(*a, **k):
    raise FileNotFoundError("no registry")
wr.OpenKey = wr.QueryValueEx = _nokey
wr.HKEY_CURRENT_USER = wr.HKEY_LOCAL_MACHINE = 0
sys.modules["winreg"] = wr
addon_utils.enable("io_scene_nifly", default_set=True)
for o in list(bpy.data.objects):
    bpy.data.objects.remove(o)
args = sys.argv[sys.argv.index("--") + 1:]
bpy.ops.import_scene.pynifly(filepath=args[0])
out = {"meshes": {}, "bones": {}}
for o in bpy.data.objects:
    if o.type == "MESH":
        vs = [o.matrix_world @ v.co for v in o.data.vertices]
        out["meshes"][o.name] = {"n": len(vs), "groups": [g.name for g in o.vertex_groups],
                                 "min": [min(v[i] for v in vs) for i in range(3)],
                                 "max": [max(v[i] for v in vs) for i in range(3)]}
    elif o.type == "ARMATURE":
        for b in o.data.bones:
            out["bones"][b.name] = list((o.matrix_world @ b.matrix_local).translation)
open(args[1], "w").write(json.dumps(out))
'''


@pytest.mark.skipif(not BLENDER.exists(), reason="Blender not installed in third_party/blender")
def test_pynifly_blender_importer_reads_mask(tmp_path):
    import os
    import subprocess
    addons = tmp_path / "scripts" / "addons"
    addons.mkdir(parents=True)
    (addons / "io_scene_nifly").symlink_to(ROOT / "third_party" / "PyNifly" / "io_scene_nifly")
    script, out = tmp_path / "imp.py", tmp_path / "out.json"
    script.write_text(BLENDER_SCRIPT)
    nif_path = _nif_path("human_m")
    env = dict(os.environ, BLENDER_USER_SCRIPTS=str(tmp_path / "scripts"))
    subprocess.run([str(BLENDER), "-b", "--factory-startup", "-P", str(script), "--", str(nif_path), str(out)],
                   env=env, check=True, capture_output=True, timeout=300)
    got = json.loads(out.read_text())
    authored = load_npz(MESH_DIR / "human_m.npz")
    assert set(got["meshes"]) == set(SHAPES)
    for name, m in got["meshes"].items():
        v = authored.shape_arrays(name)[0]
        assert m["n"] == len(v)
        assert "NPC Head" in m["groups"]                          # Blender name of NPC Head [Head]
        assert np.allclose(m["min"], v.min(0), atol=1e-3) and np.allclose(m["max"], v.max(0), atol=1e-3)
    bind = skeleton_binds(SKELETONS / "skeleton.nif", [HEAD_BONE])[HEAD_BONE]
    assert np.allclose(got["bones"]["NPC Head"], bind[:3, 3], atol=1e-3)
