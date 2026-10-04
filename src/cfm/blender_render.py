"""Headless Blender render harness: shows exactly what is inside NIF files, on a vanilla head.

Runs inside Blender (src/render_check.py drives it and builds the contact sheet):

    blender -b --factory-startup -P src/cfm/blender_render.py -- \\
        --nif work/nif/mask_human_m.nif --head human --out-dir /tmp/x \\
        [--pose 35,15,0] [--checker] [--views front,front34,left,back34,back,top,closeup] \\
        [--extra-nif third_party/PyNifly/tests/tests/SkyrimSE/maleorchair27.nif]

Writes <out-dir>/<view>.png for every view and <out-dir>/report.json.

Meshes are rebuilt from the raw NIF buffers read with PyNifly's bpy-free `pyn` layer (not
the PyNifly Blender importer), so every transform is explicit. All matrices are 4x4 and
act on column vectors; space is Skyrim model space in the bind pose (+Z up, +Y forward,
+X actor's right, see docs/CONTRACTS.md), 1 Blender unit = 1 Skyrim unit.

    skinned vertex    v_model = sum_b w_b * W_b * S_b * v_nif
        w_b  the raw stored weights, as linear-blend skinning uses them (NOT renormalised:
             a NIF whose weights do not sum to 1 is drawn where skinning really puts it, and
             the report shows the raw sum)
        W_b  bind transform of bone b from the vanilla skeleton NIF (skeleton*.nif); bones the
             skeleton lacks (e.g. SMP physics bones) use the NIF's own node
        S_b  skin-to-bone transform stored in the NIF for bone b (NiSkinData bone transform)
        cross-check: inverse(global_to_skin) * v_nif (only reported as a deviation)
    unskinned vertex  v_model = T_root * ... * T_parent * T_shape * v_nif
        the local transforms of the shape and of every ancestor node, composed explicitly
        (nifly's "global transform" only resolves NiNode blocks, not shapes)
    pose              v_pose  = sum_b w_b * D_b * W_b * S_b * v_nif
        D_b = B_pose * B_bind^-1 = T(p) * R * T(-p) for bones driven by "NPC Head [Head]"
        (p = the head bone's bind position), identity for every other bone (e.g. Spine2).
        With h = sum of head-driven weights and v_h = their part of v_model this is
        v_pose = v_model - v_h + R * v_h + h * t_D   (D = [R | t_D]).
        R = Rz(yaw) * Rx(pitch) * Ry(roll), right-handed about model axes: +yaw turns the
        face toward the actor's left, +pitch tilts the face up, +roll tips the crown toward
        the actor's right.
    UVs               Blender uv = (u', 1 - v'), (u', v') = (u, v) * UV_Scale + UV_Offset
                      (NIF v points down the image; shader UV transform applied)

Shader state that changes the in-game look is read too: NiAlphaProperty (blend / test and
threshold) with the shader Alpha and vertex alpha, vertex colours (SLSF2 Vertex_Colors),
UV offset/scale and Own_Emit emissive colour * multiplier are reproduced; features the render
cannot show (environment maps, glow maps, parallax, rim/back lighting, ...) are listed in the
report as not emulated.

The pure-NIF part (everything above `# --- Blender scene ---`) needs only numpy, so the
venv can import it for tests; bpy is imported lazily.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path, PureWindowsPath

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
PYN_DIR = ROOT / "third_party" / "PyNifly" / "io_scene_nifly"
SKELETONS = PYN_DIR / "skeletons" / "Skyrim"
VANILLA = ROOT / "third_party" / "PyNifly" / "tests" / "tests" / "SkyrimSE"
if str(PYN_DIR) not in sys.path:
    sys.path.insert(0, str(PYN_DIR))

from pyn.nifconstants import BSLSPShaderType, ShaderFlags1, ShaderFlags2, VertexFlags  # noqa: E402
from pyn.pynifly import NifFile  # noqa: E402

HEAD_BONE = "NPC Head [Head]"
WEIGHT_TOL = 1e-3        # half-float weights of vanilla meshes sum to 1 within ~4e-4

# --head choice -> (head NIF, skeleton NIF, matte skin colour in linear RGB)
HEADS = {
    "human": (VANILLA / "malehead.nif", SKELETONS / "skeleton.nif", (0.62, 0.36, 0.25)),
    "beast": (VANILLA / "maleheadargonian.nif", SKELETONS / "skeletonbeast.nif", (0.28, 0.33, 0.20)),
    # Non-vanilla test head shipped with PyNifly (CitrusFemHead), handy for the _f masks.
    "female": (VANILLA / "CitrusFemHead.nif", SKELETONS / "skeleton_female.nif", (0.62, 0.38, 0.28)),
}

SKELETON_FILES = {
    "human": SKELETONS / "skeleton.nif",
    "female": SKELETONS / "skeleton_female.nif",
    "beast": SKELETONS / "skeletonbeast.nif",
    "beast_female": SKELETONS / "skeletonbeast_female.nif",
}

# view name -> (label, azimuth deg, elevation deg, framing target)
# Azimuth 0 = camera in front of the actor (+Y), 90 = actor's left side (-X), 180 = behind.
VIEWS = {
    "front": ("Front", 0, 0, "all"),
    "front34": ("Front 3/4 (actor's left)", 45, 12, "all"),
    "left": ("Left side", 90, 0, "all"),
    "back34": ("Back 3/4 (actor's right)", 225, 12, "all"),
    "back": ("Back", 180, 0, "all"),
    "top": ("Top (actor faces up)", 0, 90, "all"),
    "closeup": ("Close-up front", 0, 0, "front_of_face"),
    # extras, available through --views
    "right": ("Right side", 270, 0, "all"),
    "front34r": ("Front 3/4 (actor's right)", 315, 12, "all"),
    "back34l": ("Back 3/4 (actor's left)", 135, 12, "all"),
}
DEFAULT_VIEWS = ["front", "front34", "left", "back34", "back", "top", "closeup"]


# --- NIF -> model space (numpy only) -------------------------------------------------------


def xf_matrix(xf) -> np.ndarray:
    """PyNifly TransformBuf -> 4x4 matrix (v' = R*s*v + t, rotation stored row-major)."""
    m = np.eye(4)
    m[:3, :3] = np.array([[xf.rotation[i][j] for j in range(3)] for i in range(3)]) * xf.scale
    m[:3, 3] = [xf.translation[i] for i in range(3)]
    return m


def apply(m: np.ndarray, pts: np.ndarray) -> np.ndarray:
    return pts @ m[:3, :3].T + m[:3, 3]


def unit_rows(a: np.ndarray) -> np.ndarray:
    n = np.linalg.norm(a, axis=1, keepdims=True)
    return a / np.where(n > 1e-12, n, 1.0)


@dataclass
class Skeleton:
    path: Path
    world: dict      # bone name -> 4x4 bind transform in model space
    parent: dict     # bone name -> parent name (None at the root)


def load_skeleton(path: Path) -> Skeleton:
    nif = NifFile(str(path))
    world, parent = {}, {}
    for name, node in nif.nodes.items():
        world[name] = xf_matrix(node.global_transform)
        parent[name] = node.parent.name if node.parent is not None else None
    return Skeleton(Path(path), world, parent)


def skeleton_for(nif_path: Path, choice: str) -> Path:
    """Reference skeleton for a NIF: explicit choice, else guessed from the file name."""
    if choice != "auto":
        return SKELETON_FILES[choice]
    stem = Path(nif_path).stem.lower()
    beast = "beast" in stem or "argonian" in stem or "khajiit" in stem
    female = stem.endswith("_f") or "fem" in stem
    key = ("beast_female" if female else "beast") if beast else ("female" if female else "human")
    return SKELETON_FILES[key]


@dataclass
class MeshData:
    name: str
    source: str
    role: str                    # "nif" (the meshes under test), "head" or "overlay" (--extra-nif)
    verts: np.ndarray            # (N, 3) model space
    normals: np.ndarray          # (N, 3) model space, unit
    tris: np.ndarray             # (M, 3)
    uvs: np.ndarray              # (N, 2) Blender convention (shader UV transform applied, v flipped)
    head_weight: np.ndarray      # (N,) raw summed weight of bones driven by the head bone
    head_part: np.ndarray        # (N, 3) the head-driven part of verts: sum over those bones of w*W*S*v
    head_share: np.ndarray       # (N,) head_weight / raw weight sum (0 for unweighted verts)
    colors: np.ndarray | None    # (N, 4) vertex colours as stored (sRGB, 0..1) when the shader uses them
    textures: dict
    double_sided: bool
    model_space_normals: bool
    shader: dict = field(default_factory=dict)
    report: dict = field(default_factory=dict)


def _ancestors(name, skel: Skeleton, nif_nodes: dict):
    """Ancestor bone names: skeleton hierarchy first, else the NIF's own node hierarchy."""
    out = []
    if name in skel.parent:
        p = skel.parent[name]
        while p is not None:
            out.append(p)
            p = skel.parent.get(p)
        return out
    node = nif_nodes.get(name)
    p = node.parent if node is not None else None
    while p is not None:
        out.append(p.name)
        p = p.parent
    return out


def head_driven(name, skel: Skeleton, nif_nodes: dict) -> bool:
    return name == HEAD_BONE or HEAD_BONE in _ancestors(name, skel, nif_nodes)


def node_chain(node, max_depth: int = 256):
    """Local-to-model matrix of a NIF block: T_root * ... * T_parent * T_node, composed from
    each block's own transform, plus the chain of names (root first)."""
    m, names = np.eye(4), []
    while node is not None and len(names) < max_depth:
        m = xf_matrix(node.transform) @ m
        names.insert(0, node.name)
        node = node.parent
    return m, names


def _weight_matrix(shape, bones, n):
    """(n_verts, n_bones) weight matrix from PyNifly's per-bone weight lists."""
    w = np.zeros((n, len(bones)))
    by_bone = shape.bone_weights
    for j, b in enumerate(bones):
        for vi, wt in by_bone.get(b, []):
            w[vi, j] += wt
    return w


def _range(a) -> list:
    return [float(a.min()), float(a.max())] if len(a) else [0.0, 0.0]


def skin_to_model(shape, nif, skel: Skeleton):
    """Model-space positions/normals of a skinned shape (raw weights, see the module docstring),
    the head-driven weight and position part, and a diagnostic report."""
    stored = np.array(shape.verts, float)
    nstored = np.array(shape.normals or np.zeros_like(stored), float)
    bones = list(dict.fromkeys(shape.bone_names))
    w = _weight_matrix(shape, bones, len(stored))
    total = w.sum(1)
    mats, sources, driven = [], {}, []
    for b in bones:
        s2b = xf_matrix(shape.get_shape_skin_to_bone(b))
        if b in skel.world:
            wb, sources[b] = skel.world[b], "skeleton"
        else:
            wb, sources[b] = xf_matrix(nif.nodes[b].global_transform), "nif node"
        mats.append(wb @ s2b)
        driven.append(head_driven(b, skel, nif.nodes))
    verts, head_part, normals = (np.zeros_like(stored) for _ in range(3))
    for j, m in enumerate(mats):
        part = w[:, [j]] * apply(m, stored)
        verts += part
        if driven[j]:
            head_part += part
        normals += w[:, [j]] * (nstored @ m[:3, :3].T)
    head_w = (w * np.array(driven, float)[None, :]).sum(1)
    share = np.where(total > 1e-9, head_w / np.where(total > 1e-9, total, 1.0), 0.0)
    # Vertices with no weight at all would collapse to the origin: draw them through the
    # inverse global-to-skin transform instead and report them.
    g2s_inv = np.linalg.inv(xf_matrix(shape.global_to_skin))
    via_g2s = apply(g2s_inv, stored)
    unweighted = total <= 1e-9
    verts[unweighted] = via_g2s[unweighted]
    normals[unweighted] = nstored[unweighted] @ g2s_inv[:3, :3].T
    weighted = ~unweighted
    report = {
        "skinned": True,
        "bones": {b: {"verts": int((w[:, j] > 0).sum()), "bind_source": sources[b],
                      "head_driven": driven[j]} for j, b in enumerate(bones)},
        "bone_bind_disagreement": float(max(np.abs(m - mats[0]).max() for m in mats)),
        "inverse_global_to_skin_max_dev": float(np.abs(via_g2s - verts)[weighted].max()) if weighted.any() else 0.0,
        "unweighted_verts": int(unweighted.sum()),
        "weight_sum_range": _range(total),
        "head_weight_range": _range(head_w),
        "head_share_range": _range(share[weighted]),
    }
    return verts, unit_rows(normals), head_w, head_part, share, report


def winding_agreement(verts, tris, normals) -> float:
    """Fraction of triangles whose winding normal points the same way as their stored
    vertex normals (1.0 = consistent; low values mean flipped winding or normals)."""
    if not len(tris):
        return 1.0
    a, b, c = (verts[tris[:, i]] for i in range(3))
    face = np.cross(b - a, c - a)
    vn = normals[tris].sum(1)
    valid = np.linalg.norm(face, axis=1) > 1e-12
    return float(((face * vn).sum(1) > 0)[valid].mean()) if valid.any() else 1.0


def geometric_normals(verts, tris) -> np.ndarray:
    """Area-weighted vertex normals from the triangle winding, averaged over vertices that
    share a position (UV seams duplicate vertices; this keeps the seams invisible)."""
    _, group = np.unique(np.round(verts, 4), axis=0, return_inverse=True)
    group = group.ravel()
    a, b, c = (verts[tris[:, i]] for i in range(3))
    face = np.cross(b - a, c - a)
    acc = np.zeros((group.max() + 1, 3))
    for i in range(3):
        np.add.at(acc, group[tris[:, i]], face)
    return unit_rows(acc[group])


def stored_normals_valid(shape, n_verts) -> bool:
    """Vertex normals present and non-zero (vanilla heads use model-space normal maps and
    store none; pyn then returns zero vectors)."""
    n = np.array(shape.normals or [], float).reshape(-1, 3)
    return len(n) == n_verts and bool((np.linalg.norm(n, axis=1) > 0.5).mean() > 0.99)


# Shader features that change the in-game look but are not reproduced by this render.
NOT_EMULATED_FLAGS1 = {
    ShaderFlags1.ENVIRONMENT_MAPPING: "environment map", ShaderFlags1.PARALLAX: "parallax",
    ShaderFlags1.REFRACTION: "refraction", ShaderFlags1.FIRE_REFRACTION: "fire refraction",
    ShaderFlags1.EYE_ENVIRONMENT_MAPPING: "eye environment map",
    ShaderFlags1.HAIR_SOFT_LIGHTING: "hair soft lighting",
    ShaderFlags1.GREYSCALE_COLOR: "greyscale-to-palette colour",
    ShaderFlags1.GREYSCALE_ALPHA: "greyscale-to-palette alpha",
    ShaderFlags1.FACEGEN_DETAIL_MAP: "facegen detail map", ShaderFlags1.FACEGEN_RGB_TINT: "facegen tint",
    ShaderFlags1.PROJECTED_UV: "projected UV", ShaderFlags1.MULTIPLE_TEXTURES: "multiple textures",
    ShaderFlags1.PARALLAX_OCCLUSION: "parallax occlusion", ShaderFlags1.USE_FALLOFF: "falloff",
    ShaderFlags1.MODEL_SPACE_NORMALS: "model-space normal map",
}
NOT_EMULATED_FLAGS2 = {
    ShaderFlags2.GLOW_MAP: "glow map", ShaderFlags2.MULTI_LAYER_PARALLAX: "multi-layer parallax",
    ShaderFlags2.SOFT_LIGHTING: "soft lighting", ShaderFlags2.RIM_LIGHTING: "rim lighting",
    ShaderFlags2.BACK_LIGHTING: "back lighting", ShaderFlags2.ANISOTROPIC_LIGHTING: "anisotropic lighting",
    ShaderFlags2.TREE_ANIM: "tree animation", ShaderFlags2.PREMULT_ALPHA: "premultiplied alpha",
}
# shader type -> the flag feature above it implies (listed once in the report)
SHADER_TYPE_FEATURE = {"Environment_Map": "environment map", "Glow_Shader": "glow map",
                       "Parallax": "parallax", "Parallax_Occ": "parallax occlusion",
                       "MultiLayer_Parallax": "multi-layer parallax", "Eye_Envmap": "eye environment map",
                       "Face_Tint": "facegen tint"}
ALPHA_BLEND, ALPHA_TEST, ALPHA_TEST_FUNC = 0x0001, 0x0200, 0x1C00   # NiAlphaProperty flag masks
TEST_GREATER, TEST_GREATER_EQUAL = 4, 6                              # NiAlphaProperty test functions
CLAMP_WRAP_S_WRAP_T = 3


def _shader_type_name(value: int) -> str:
    try:
        return BSLSPShaderType(value).name
    except ValueError:
        return f"unknown ({value})"


def shader_state(shape, uv_range) -> dict:
    """Everything in the shader/alpha blocks that changes the look, what the render applies
    (`applied`, non-default only) and what it cannot show (`not_emulated`)."""
    pr = shape.shader.properties
    f1, f2 = int(pr.Shader_Flags_1), int(pr.Shader_Flags_2)
    colors = shape.colors if shape.properties.hasVertexColors else []
    st = {
        "shader_type": _shader_type_name(pr.Shader_Type), "flags1": hex(f1), "flags2": hex(f2),
        "alpha_property": None, "shader_alpha": float(pr.Alpha),
        "vertex_colors_stored": len(colors), "vertex_colors_flag": bool(f2 & ShaderFlags2.VERTEX_COLORS),
        "vertex_alpha_flag": bool(f1 & ShaderFlags1.VERTEX_ALPHA),
        "uv_offset": [float(pr.UV_Offset_U), float(pr.UV_Offset_V)],
        "uv_scale": [float(pr.UV_Scale_U), float(pr.UV_Scale_V)],
        "own_emit": bool(f1 & ShaderFlags1.OWN_EMIT),
        "emissive": [round(float(c), 4) for c in list(pr.Emissive_Color)[:3]],
        "emissive_mult": float(pr.Emissive_Mult),
        "glossiness": float(pr.Glossiness), "spec_strength": float(pr.Spec_Str),
        "spec_color": [round(float(c), 4) for c in pr.Spec_Color],
        "texture_clamp_mode": int(pr.textureClampMode),
        # pyn hands back the BSVertexDesc attribute flags already shifted down (0x5b = VERTEX|UV|...)
        "tangents_stored": bool(int(shape.properties.vertexDesc) & VertexFlags.TANGENT),
        "applied": [], "not_emulated": [],
    }
    applied, missing = st["applied"], st["not_emulated"]
    alpha = shape.alpha_property
    if alpha is not None:
        flags, thr = int(alpha.properties.flags), int(alpha.properties.threshold)
        func = (flags & ALPHA_TEST_FUNC) >> 10
        st["alpha_property"] = {"flags": hex(flags), "blend": bool(flags & ALPHA_BLEND),
                                "test": bool(flags & ALPHA_TEST), "test_func": func, "threshold": thr}
        if flags & ALPHA_BLEND:
            applied.append("alpha blend")
        if flags & ALPHA_TEST:
            if func in (TEST_GREATER, TEST_GREATER_EQUAL):
                applied.append(f"alpha test {'>' if func == TEST_GREATER else '>='} {thr}/255")
            else:
                missing.append(f"alpha test function {func}")
        if st["shader_alpha"] != 1.0:
            applied.append(f"shader alpha {st['shader_alpha']:g}")
        if st["vertex_alpha_flag"] and colors:
            applied.append("vertex alpha")
    if st["vertex_colors_flag"] and colors:
        applied.append(f"vertex colours ({len(colors)})")
    if st["uv_offset"] != [0.0, 0.0] or st["uv_scale"] != [1.0, 1.0]:
        applied.append("UV x({:g}, {:g}) + ({:g}, {:g})".format(*st["uv_scale"], *st["uv_offset"]))
    if st["own_emit"] and max(st["emissive"]) * st["emissive_mult"] > 0:
        applied.append("emissive ({:g}, {:g}, {:g}) x {:g}".format(*st["emissive"], st["emissive_mult"]))
    flagged = [label for flag, label in NOT_EMULATED_FLAGS1.items() if f1 & flag]
    flagged += [label for flag, label in NOT_EMULATED_FLAGS2.items() if f2 & flag]
    if pr.Shader_Type != 0:
        implied = SHADER_TYPE_FEATURE.get(st["shader_type"])
        missing.append(f"{implied} (shader type {st['shader_type']})" if implied in flagged
                       else f"shader type {st['shader_type']}")
        flagged = [f for f in flagged if f != implied]
    missing += flagged
    outside = uv_range is not None and (min(uv_range[0]) < 0 or max(uv_range[1]) > 1)
    if st["texture_clamp_mode"] != CLAMP_WRAP_S_WRAP_T and outside:
        missing.append(f"texture clamp mode {st['texture_clamp_mode']} (UVs leave 0..1)")
    return st


def _blender_uvs(shape, st, n_verts):
    """NIF UVs with the shader UV transform applied, then v flipped for Blender."""
    uv = np.array(shape.uvs, float).reshape(-1, 2)
    if not len(uv):
        return np.zeros((n_verts, 2))
    uv = uv * np.array(st["uv_scale"]) + np.array(st["uv_offset"])
    return np.stack([uv[:, 0], 1.0 - uv[:, 1]], 1)


def shape_to_mesh(shape, nif, skel: Skeleton, role: str, source: str) -> MeshData:
    stored = np.array(shape.verts, float)
    tris = np.array(shape.tris, int).reshape(-1, 3)
    if shape.has_skin_instance:
        verts, normals, head_w, head_part, share, report = skin_to_model(shape, nif, skel)
    else:
        m, chain = node_chain(shape)
        verts = apply(m, stored)
        n = np.array(shape.normals or np.zeros_like(stored), float)
        normals = unit_rows(n @ m[:3, :3].T)
        head_w, head_part, share = np.zeros(len(stored)), np.zeros_like(stored), np.zeros(len(stored))
        report = {"skinned": False, "node_chain": chain, "shape_to_model": m.round(5).tolist()}
    has_normals = stored_normals_valid(shape, len(stored))
    if has_normals:
        report["winding_agreement"] = winding_agreement(verts, tris, normals)
    else:
        normals = geometric_normals(verts, tris)
    raw_uv = np.array(shape.uvs, float).reshape(-1, 2)
    uv_range = [raw_uv.min(0).round(4).tolist(), raw_uv.max(0).round(4).tolist()] if len(raw_uv) else None
    st = shader_state(shape, uv_range)
    shader = shape.shader
    colors = None
    if st["vertex_colors_flag"] and st["vertex_colors_stored"] == len(stored):
        colors = np.array(shape.colors, float).reshape(-1, 4)
    report.update({
        "block": shape.blockname,
        "verts": len(stored), "tris": len(tris),
        "model_bbox": [verts.min(0).round(4).tolist(), verts.max(0).round(4).tolist()],
        "uv_range": uv_range,
        "textures": {k: v for k, v in shape.textures.items() if v},
        "double_sided": bool(shader.flag_double_sided),
        "has_normals": has_normals,
        "shader": st,
    })
    return MeshData(
        name=shape.name, source=source, role=role, verts=verts, normals=normals, tris=tris,
        uvs=_blender_uvs(shape, st, len(stored)), head_weight=head_w, head_part=head_part,
        head_share=share,
        colors=colors, textures=dict(shape.textures),
        double_sided=bool(shader.flag_double_sided),
        model_space_normals=bool(shader.properties.Shader_Flags_1 & ShaderFlags1.MODEL_SPACE_NORMALS),
        shader=st, report=report)


def load_nif_meshes(path: Path, skel: Skeleton, role: str) -> list:
    nif = NifFile(str(path))
    rel = _rel(path)
    return [shape_to_mesh(s, nif, skel, role, rel) for s in nif.shapes]


def _abs(path) -> Path:
    return Path(path) if Path(path).is_absolute() else ROOT / path


def _rel(path) -> str:
    p = Path(path).resolve()
    try:
        return str(p.relative_to(ROOT))
    except ValueError:
        return str(p)


def rotation(yaw, pitch, roll) -> np.ndarray:
    """R = Rz(yaw) Rx(pitch) Ry(roll), degrees, right-handed about model axes."""
    y, p, r = (math.radians(a) for a in (yaw, pitch, roll))
    rz = np.array([[math.cos(y), -math.sin(y), 0], [math.sin(y), math.cos(y), 0], [0, 0, 1]])
    rx = np.array([[1, 0, 0], [0, math.cos(p), -math.sin(p)], [0, math.sin(p), math.cos(p)]])
    ry = np.array([[math.cos(r), 0, math.sin(r)], [0, 1, 0], [-math.sin(r), 0, math.cos(r)]])
    return rz @ rx @ ry


def pose_delta(head_bind: np.ndarray, yaw, pitch, roll) -> np.ndarray:
    """D = B_pose * B_bind^-1 where B_pose rotates the head bone about its bind position."""
    p = head_bind[:3, 3]
    t, r, ti = np.eye(4), np.eye(4), np.eye(4)
    t[:3, 3], ti[:3, 3] = p, -p
    r[:3, :3] = rotation(yaw, pitch, roll)
    b_pose = t @ r @ ti @ head_bind
    return b_pose @ np.linalg.inv(head_bind)


def apply_pose(mesh: MeshData, delta: np.ndarray) -> None:
    """Linear-blend skinning with D for head-driven weights and identity elsewhere:
    v' = v - v_h + R v_h + h t (exact for raw weights whatever their sum)."""
    h = mesh.head_weight[:, None]
    rot, t = delta[:3, :3], delta[:3, 3]
    before = mesh.verts
    posed_head = mesh.head_part @ rot.T + h * t
    mesh.verts = before - mesh.head_part + posed_head
    mesh.head_part = posed_head
    share = mesh.head_share[:, None]
    mesh.normals = unit_rows((1.0 - share) * mesh.normals + share * (mesh.normals @ rot.T))
    moved = np.linalg.norm(mesh.verts - before, axis=1)
    still = mesh.head_weight <= 1e-9
    mesh.report["pose_displacement"] = {
        "max": float(moved.max()),
        "max_where_head_weight_0": float(moved[still].max()) if still.any() else None,
        "verts_head_weight_0": int(still.sum()),
    }


# --- Blender scene -------------------------------------------------------------------------


def _bpy():
    import bpy  # noqa: WPS433 (only available inside Blender)
    return bpy


def reset_scene(args):
    bpy = _bpy()
    bpy.ops.wm.read_factory_settings(use_empty=True)
    scene = bpy.context.scene
    scene.render.engine = "CYCLES"
    cyc = scene.cycles
    cyc.device = "CPU"
    cyc.samples = args.samples
    cyc.use_adaptive_sampling = True
    cyc.seed = 0
    cyc.use_denoising = args.samples >= 16
    if cyc.use_denoising:
        cyc.denoiser = "OPENIMAGEDENOISE"
    cyc.max_bounces = 6
    cyc.transparent_max_bounces = 16
    scene.render.resolution_x = scene.render.resolution_y = args.res
    scene.render.resolution_percentage = 100
    scene.render.film_transparent = False
    scene.render.image_settings.file_format = "PNG"
    scene.render.image_settings.color_mode = "RGB"
    scene.render.image_settings.color_depth = "8"
    scene.view_settings.view_transform = "AgX"
    scene.view_settings.look = "AgX - Medium High Contrast"
    scene.render.threads_mode = "AUTO"
    return scene


def make_world(scene):
    """Neutral grey backdrop for camera rays, a dim neutral ambient for lighting."""
    bpy = _bpy()
    world = bpy.data.worlds.new("studio")
    world.use_nodes = True
    nt = world.node_tree
    nt.nodes.clear()
    out = nt.nodes.new("ShaderNodeOutputWorld")
    path = nt.nodes.new("ShaderNodeLightPath")
    bg_cam = nt.nodes.new("ShaderNodeBackground")
    bg_cam.inputs["Color"].default_value = (0.16, 0.165, 0.17, 1)
    bg_amb = nt.nodes.new("ShaderNodeBackground")
    bg_amb.inputs["Color"].default_value = (0.06, 0.06, 0.065, 1)
    mix = nt.nodes.new("ShaderNodeMixShader")
    nt.links.new(path.outputs["Is Camera Ray"], mix.inputs["Fac"])
    nt.links.new(bg_amb.outputs["Background"], mix.inputs[1])
    nt.links.new(bg_cam.outputs["Background"], mix.inputs[2])
    nt.links.new(mix.outputs["Shader"], out.inputs["Surface"])
    scene.world = world


def find_texture(nif_tex: str, args):
    """Local stand-in for a NIF texture path: work/textures/<stem>.png, else work/dds/<name>
    (DDS first with --prefer-dds)."""
    if not nif_tex:
        return None
    name = PureWindowsPath(nif_tex).name
    stem = name.rsplit(".", 1)[0]
    cands = [Path(args.textures_dir) / f"{stem}.png", Path(args.dds_dir) / name]
    if args.prefer_dds:
        cands.reverse()
    return next((c for c in cands if c.exists()), None)


def _image(path: Path, non_color: bool):
    bpy = _bpy()
    img = bpy.data.images.load(str(path), check_existing=True)
    if non_color:
        img.colorspace_settings.name = "Non-Color"
        img.alpha_mode = "CHANNEL_PACKED"   # alpha is a specular mask, not coverage
    return img


def _checker_image():
    bpy = _bpy()
    img = bpy.data.images.get("uv_checker")
    if img is None:
        img = bpy.data.images.new("uv_checker", 1024, 1024, alpha=False)
        img.generated_type = "COLOR_GRID"
    return img


def _cull_backfaces(nt, surface):
    """Skyrim culls back faces unless the shader is double-sided: show them as holes."""
    geo = nt.nodes.new("ShaderNodeNewGeometry")
    transp = nt.nodes.new("ShaderNodeBsdfTransparent")
    mix = nt.nodes.new("ShaderNodeMixShader")
    nt.links.new(geo.outputs["Backfacing"], mix.inputs["Fac"])
    nt.links.new(surface, mix.inputs[1])
    nt.links.new(transp.outputs["BSDF"], mix.inputs[2])
    return mix.outputs["Shader"]


def _math(nt, op, a, b):
    """Math node `a op b`; a and b are sockets or numbers."""
    node = nt.nodes.new("ShaderNodeMath")
    node.operation = op
    for i, v in enumerate((a, b)):
        if isinstance(v, (int, float)):
            node.inputs[i].default_value = float(v)
        else:
            nt.links.new(v, node.inputs[i])
    return node.outputs["Value"]


def _alpha_cutout(nt, surface, alpha, st):
    """NiAlphaProperty: alpha test discards at or below the threshold, alpha blend fades by
    alpha; both act on texture alpha * shader Alpha * vertex alpha (`alpha` socket)."""
    ap = st["alpha_property"]
    cover = None
    if ap["test"] and ap["test_func"] in (TEST_GREATER, TEST_GREATER_EQUAL):
        thr = ap["threshold"] / 255.0 - (0.5 / 255.0 if ap["test_func"] == TEST_GREATER_EQUAL else 0.0)
        cover = _math(nt, "GREATER_THAN", alpha, thr)
    if ap["blend"]:
        cover = alpha if cover is None else _math(nt, "MULTIPLY", cover, alpha)
    if cover is None:
        return surface
    transp = nt.nodes.new("ShaderNodeBsdfTransparent")
    mix = nt.nodes.new("ShaderNodeMixShader")
    nt.links.new(cover, mix.inputs["Fac"])
    nt.links.new(transp.outputs["BSDF"], mix.inputs[1])
    nt.links.new(surface, mix.inputs[2])
    return mix.outputs["Shader"]


def srgb_to_linear(c):
    c = np.asarray(c, float)
    return np.where(c <= 0.04045, c / 12.92, ((c + 0.055) / 1.055) ** 2.4)


def _normal_map(nt, img, bsdf):
    """DirectX tangent-space normal map (G = +v down) -> Blender (G = +uv.y up): flip G.
    The alpha channel is the Skyrim specular mask; it lowers roughness where shiny."""
    tex = nt.nodes.new("ShaderNodeTexImage")
    tex.image = img
    sep = nt.nodes.new("ShaderNodeSeparateColor")
    inv = nt.nodes.new("ShaderNodeMath")
    inv.operation = "SUBTRACT"
    inv.inputs[0].default_value = 1.0
    comb = nt.nodes.new("ShaderNodeCombineColor")
    nmap = nt.nodes.new("ShaderNodeNormalMap")
    nmap.space = "TANGENT"
    nmap.uv_map = "UVMap"
    nt.links.new(tex.outputs["Color"], sep.inputs["Color"])
    nt.links.new(sep.outputs["Red"], comb.inputs["Red"])
    nt.links.new(sep.outputs["Green"], inv.inputs[1])
    nt.links.new(inv.outputs["Value"], comb.inputs["Green"])
    nt.links.new(sep.outputs["Blue"], comb.inputs["Blue"])
    nt.links.new(comb.outputs["Color"], nmap.inputs["Color"])
    nt.links.new(nmap.outputs["Normal"], bsdf.inputs["Normal"])
    rough = nt.nodes.new("ShaderNodeMapRange")
    rough.inputs["To Min"].default_value = 0.9
    rough.inputs["To Max"].default_value = 0.35
    nt.links.new(tex.outputs["Alpha"], rough.inputs["Value"])
    nt.links.new(rough.outputs["Result"], bsdf.inputs["Roughness"])


def _diffuse(nt, mesh: MeshData, args, desc):
    """(colour socket, alpha socket or None) of the diffuse texture, checker or neutral grey."""
    if args.checker:
        tex = nt.nodes.new("ShaderNodeTexImage")
        tex.image = _checker_image()
        desc["diffuse"] = "UV checker (Blender COLOR_GRID)"
        return tex.outputs["Color"], None
    dpath = find_texture(mesh.textures.get("Diffuse", ""), args)
    if dpath is not None:
        tex = nt.nodes.new("ShaderNodeTexImage")
        tex.image = _image(dpath, non_color=False)
        desc["diffuse"] = _rel(dpath)
        return tex.outputs["Color"], tex.outputs["Alpha"]
    grey = nt.nodes.new("ShaderNodeRGB")
    grey.outputs["Color"].default_value = (0.5, 0.5, 0.5, 1)
    missing = PureWindowsPath(mesh.textures.get("Diffuse") or "none").name
    desc["diffuse"] = f"neutral grey ({missing} not in work/)"
    return grey.outputs["Color"], None


def nif_material(mesh: MeshData, args) -> tuple:
    """Material for a mesh under test; returns (material, description for the report)."""
    bpy = _bpy()
    mat = bpy.data.materials.new(f"M_{mesh.name}")
    mat.use_nodes = True
    nt = mat.node_tree
    bsdf = nt.nodes["Principled BSDF"]
    out = nt.nodes["Material Output"]
    bsdf.inputs["Roughness"].default_value = 0.75
    st, desc = mesh.shader, {}
    colour, alpha = _diffuse(nt, mesh, args, desc)
    vcol = None
    if mesh.colors is not None and not args.checker:     # the checker stays untinted
        vcol = nt.nodes.new("ShaderNodeVertexColor")
        vcol.layer_name = "Col"
        mul = nt.nodes.new("ShaderNodeVectorMath")
        mul.operation = "MULTIPLY"
        nt.links.new(colour, mul.inputs[0])
        nt.links.new(vcol.outputs["Color"], mul.inputs[1])
        colour = mul.outputs["Vector"]
    nt.links.new(colour, bsdf.inputs["Base Color"])
    if args.checker:
        desc["normal"] = "off (--checker)"
    else:
        npath = find_texture(mesh.textures.get("Normal", ""), args)
        if npath is not None and not mesh.model_space_normals:
            _normal_map(nt, _image(npath, non_color=True), bsdf)
            desc["normal"] = _rel(npath)
        else:
            desc["normal"] = "none found" if npath is None else "skipped (model-space normal map)"
    if st["own_emit"] and max(st["emissive"]) * st["emissive_mult"] > 0:
        bsdf.inputs["Emission Color"].default_value = (*srgb_to_linear(st["emissive"]).tolist(), 1)
        bsdf.inputs["Emission Strength"].default_value = st["emissive_mult"]
    surface = bsdf.outputs["BSDF"]
    if st["alpha_property"] is not None:
        a = alpha if alpha is not None else 1.0
        a = _math(nt, "MULTIPLY", a, st["shader_alpha"])
        if vcol is not None and st["vertex_alpha_flag"]:
            a = _math(nt, "MULTIPLY", a, vcol.outputs["Alpha"])
        surface = _alpha_cutout(nt, surface, a, st)
    if not mesh.double_sided and not args.no_cull:
        surface = _cull_backfaces(nt, surface)
        desc["backfaces"] = "culled (not double-sided)"
    else:
        desc["backfaces"] = "rendered (double-sided)" if mesh.double_sided else "rendered (--no-cull)"
    nt.links.new(surface, out.inputs["Surface"])
    return mat, desc


def head_material(color, name="M_head_skin", roughness=0.62):
    bpy = _bpy()
    mat = bpy.data.materials.new(name)
    mat.use_nodes = True
    bsdf = mat.node_tree.nodes["Principled BSDF"]
    bsdf.inputs["Base Color"].default_value = (*color, 1)
    bsdf.inputs["Roughness"].default_value = roughness
    bsdf.inputs["Specular IOR Level"].default_value = 0.35
    return mat


OVERLAY_COLOUR = (0.07, 0.045, 0.03)    # --extra-nif meshes: matte dark-brown "hair" (linear RGB)


def make_object(mesh: MeshData, mat):
    bpy = _bpy()
    me = bpy.data.meshes.new(mesh.name)
    me.from_pydata(mesh.verts.tolist(), [], mesh.tris.tolist())
    me.validate(clean_customdata=False)
    mesh.report["blender_dropped_tris"] = len(mesh.tris) - len(me.polygons)   # degenerate/duplicate
    loop_vi = np.zeros(len(me.loops), dtype=np.int32)
    me.loops.foreach_get("vertex_index", loop_vi)
    uv_layer = me.uv_layers.new(name="UVMap")
    uv_layer.data.foreach_set("uv", mesh.uvs[loop_vi].astype(np.float32).ravel())
    me.polygons.foreach_set("use_smooth", np.ones(len(me.polygons), dtype=bool))
    me.normals_split_custom_set_from_vertices(mesh.normals.tolist())
    if mesh.colors is not None:   # stored sRGB bytes; Blender colour attributes are linear
        lin = np.column_stack([srgb_to_linear(mesh.colors[:, :3]), mesh.colors[:, 3]])
        attr = me.color_attributes.new("Col", "FLOAT_COLOR", "POINT")
        attr.data.foreach_set("color", lin.astype(np.float32).ravel())
    me.materials.append(mat)
    me.update()
    obj = bpy.data.objects.new(mesh.name, me)
    bpy.context.scene.collection.objects.link(obj)
    return obj


def winding_numbers(pts, verts, tris, chunk=128) -> np.ndarray:
    """Generalised winding number of each point w.r.t. a triangle mesh (solid angles summed,
    Van Oosterom & Strackee). About +-1 inside a closed surface, 0 outside; robust where the
    nearest feature is an edge or a vertex, and tolerant of the open neck of a head mesh."""
    a0, b0, c0 = (verts[tris[:, i]] for i in range(3))
    out = np.empty(len(pts))
    for s in range(0, len(pts), chunk):
        p = pts[s:s + chunk, None, :]
        a, b, c = a0[None] - p, b0[None] - p, c0[None] - p
        la, lb, lc = (np.linalg.norm(x, axis=2) for x in (a, b, c))
        det = np.einsum("ijk,ijk->ij", a, np.cross(b, c))
        den = (la * lb * lc + np.einsum("ijk,ijk->ij", a, b) * lc
               + np.einsum("ijk,ijk->ij", b, c) * la + np.einsum("ijk,ijk->ij", c, a) * lb)
        out[s:s + chunk] = 2.0 * np.arctan2(det, den).sum(1) / (4.0 * np.pi)
    return out


def _signed_distances(tree, pts, head_verts, head_tris, inside_sign):
    """Distance to the head surface, negative inside the head (winding number > 0.5)."""
    from mathutils import Vector
    dist = np.array([tree.find_nearest(Vector(p))[3] for p in pts])
    inside = winding_numbers(pts, head_verts, head_tris) * inside_sign > 0.5
    return np.where(inside, -dist, dist)


def _clearance_stats(signed, pts) -> dict:
    inside = signed < -0.02
    stats = {"min": float(signed.min()), "p05": float(np.percentile(signed, 5)),
             "median": float(np.median(signed)),
             "verts_inside_head": int(inside.sum()), "verts_total": int(len(signed))}
    if inside.any():
        stats["inside_bbox"] = [pts[inside].min(0).round(2).tolist(), pts[inside].max(0).round(2).tolist()]
    return stats


def clearance(nif_meshes, head_meshes) -> dict:
    """Signed distance of every mesh-under-test vertex to the head surface (negative = inside),
    overall and per shape."""
    if not head_meshes or not nif_meshes:
        return {}
    from mathutils.bvhtree import BVHTree
    verts = np.concatenate([m.verts for m in head_meshes])
    offs = np.cumsum([0] + [len(m.verts) for m in head_meshes])
    tris = np.concatenate([m.tris + o for m, o in zip(head_meshes, offs)])
    tree = BVHTree.FromPolygons(verts.tolist(), tris.tolist())
    # sign of the winding number deep inside the head (centroid of the cranium)
    probe = 0.5 * (verts.min(0) + verts.max(0))
    inside_sign = np.sign(winding_numbers(probe[None], verts, tris)[0]) or 1.0
    per = {m.name: _signed_distances(tree, m.verts, verts, tris, inside_sign) for m in nif_meshes}
    allpts = np.concatenate([m.verts for m in nif_meshes])
    result = _clearance_stats(np.concatenate(list(per.values())), allpts)
    result["per_shape"] = {m.name: _clearance_stats(per[m.name], m.verts) for m in nif_meshes}
    return result


# --- camera and lights ---------------------------------------------------------------------


def view_basis(azimuth, elevation):
    """(dir from subject to camera, camera right, camera up) in model space."""
    az, el = math.radians(azimuth), math.radians(elevation)
    d = np.array([-math.sin(az) * math.cos(el), math.cos(az) * math.cos(el), math.sin(el)])
    if elevation >= 89.9:
        up = np.array([-math.sin(az), math.cos(az), 0.0])   # top view: actor's forward is up
        d = np.array([0.0, 0.0, 1.0])
    else:
        up = np.array([0.0, 0.0, 1.0])
    right = np.cross(-d, up)
    right /= np.linalg.norm(right)
    up = np.cross(right, -d)
    return d, right, up


def fit_camera(points, d, right, up, tan_half, margin):
    """Distance from the target centre and lens shift so `points` fill the frame (perspective)."""
    centre = 0.5 * (points.min(0) + points.max(0))
    rel = points - centre
    x, y, depth = rel @ right, rel @ up, rel @ d
    limit = tan_half * (1.0 - margin)

    def spans(dist):
        z = dist - depth
        return np.ptp(x / z) / 2, np.ptp(y / z) / 2

    lo, hi = depth.max() + 1e-3, depth.max() + 1.0
    while max(spans(hi)) > limit:
        hi *= 2.0
    for _ in range(60):
        mid = 0.5 * (lo + hi)
        lo, hi = (lo, mid) if max(spans(mid)) <= limit else (mid, hi)
    dist = hi
    z = dist - depth
    shift = ((x / z).min() + (x / z).max()) / 2, ((y / z).min() + (y / z).max()) / 2
    return centre, dist, shift


def place_camera(cam_obj, centre, d, right, up, dist, shift, tan_half):
    from mathutils import Matrix
    loc = centre + d * dist
    rot = np.column_stack([right, up, d])     # Blender camera: +X right, +Y up, looks down -Z
    m = np.eye(4)
    m[:3, :3], m[:3, 3] = rot, loc
    cam_obj.matrix_world = Matrix(m.tolist())
    cam = cam_obj.data
    cam.shift_x = shift[0] / (2 * tan_half)
    cam.shift_y = shift[1] / (2 * tan_half)
    cam.clip_start = max(0.05, dist * 0.05)
    cam.clip_end = dist * 10


def make_rig():
    """Key, fill and rim area lights; positioned per view by `place_rig`."""
    bpy = _bpy()
    rig = {}
    for name, size, colour in (("key", 0.9, (1.0, 0.95, 0.88)), ("fill", 1.4, (0.86, 0.92, 1.0)),
                               ("rim", 0.5, (1.0, 1.0, 1.0))):
        light = bpy.data.lights.new(name, "AREA")
        light.shape = "DISK"
        light.color = colour
        obj = bpy.data.objects.new(name, light)
        bpy.context.scene.collection.objects.link(obj)
        rig[name] = (obj, size)
    return rig


# light -> (offset in (right, up, toward-camera) units, relative power)
RIG_LAYOUT = {
    "key": ((-0.85, 0.65, 0.85), 1.0),
    "fill": ((1.0, 0.05, 0.75), 0.35),
    "rim": ((0.45, 0.9, -1.0), 0.9),
}


KEY_IRRADIANCE = 2.6   # W/m^2 at the subject from the key light (E ~ P / (pi d^2))


def place_rig(rig, centre, d, right, up, radius, exposure):
    from mathutils import Matrix, Vector
    dist = radius * 4.0
    for name, (obj, size) in rig.items():
        (a, b, c), power = RIG_LAYOUT[name]
        off = a * right + b * up + c * d
        pos = centre + off / np.linalg.norm(off) * dist
        aim = Vector((centre - pos).tolist()).to_track_quat("-Z", "Y").to_matrix().to_4x4()
        aim.translation = Vector(pos.tolist())
        obj.matrix_world = Matrix(aim)
        obj.data.size = size * radius * 2
        # inverse-square: the same irradiance at the subject whatever the scene scale
        obj.data.energy = power * exposure * KEY_IRRADIANCE * math.pi * dist ** 2


# --- main ----------------------------------------------------------------------------------


def parse_args(argv):
    ap = argparse.ArgumentParser(prog="blender_render.py", description=__doc__.split("\n")[0])
    ap.add_argument("--nif", action="append", default=[], help="NIF under test (repeatable)")
    ap.add_argument("--extra-nif", action="append", default=[],
                    help="overlay NIF (e.g. a hair or hood), drawn matte dark brown and posed with the "
                         "head; not part of the checks (repeatable)")
    ap.add_argument("--head", choices=["human", "beast", "female", "none"], default="human")
    ap.add_argument("--skeleton", choices=["auto", *SKELETON_FILES], default="auto",
                    help="reference skeleton for the --nif files (auto: from the file name)")
    ap.add_argument("--pose", default="0,0,0", help="head bone yaw,pitch,roll in degrees")
    ap.add_argument("--checker", action="store_true", help="UV checker instead of textures")
    ap.add_argument("--no-cull", action="store_true", help="render back faces of single-sided shaders")
    ap.add_argument("--views", default=",".join(DEFAULT_VIEWS))
    ap.add_argument("--res", type=int, default=640)
    ap.add_argument("--samples", type=int, default=64)
    ap.add_argument("--lens", type=float, default=85.0, help="focal length, 36 mm sensor")
    ap.add_argument("--exposure", type=float, default=1.0, help="light power multiplier")
    ap.add_argument("--prefer-dds", action="store_true", help="use work/dds before work/textures PNGs")
    ap.add_argument("--textures-dir", default=str(ROOT / "work" / "textures"))
    ap.add_argument("--dds-dir", default=str(ROOT / "work" / "dds"))
    ap.add_argument("--out-dir", required=True)
    args = ap.parse_args(argv)
    args.pose = [float(a) for a in args.pose.split(",")]
    if len(args.pose) != 3:
        ap.error("--pose needs yaw,pitch,roll")
    args.views = [v.strip() for v in args.views.split(",") if v.strip()]
    unknown = [v for v in args.views if v not in VIEWS]
    if unknown:
        ap.error(f"unknown views {unknown}; choose from {list(VIEWS)}")
    return args


def load_all(args):
    """Model-space meshes for the NIFs under test and the head overlay (posed if asked)."""
    skel_cache = {}

    def skel(path):
        if path not in skel_cache:
            skel_cache[path] = load_skeleton(path)
        return skel_cache[path]

    nif_meshes = []
    for p in args.nif:
        path = _abs(p)
        nif_meshes += load_nif_meshes(path, skel(skeleton_for(path, args.skeleton)), "nif")
    head_meshes = []
    if args.head != "none":
        head_nif, head_skel, _ = HEADS[args.head]
        head_meshes = load_nif_meshes(head_nif, skel(head_skel), "head")
    for p in args.extra_nif:     # overlays ride with the head: drawn and posed like it, never checked
        path = _abs(p)
        head_meshes += load_nif_meshes(path, skel(skeleton_for(path, args.skeleton)), "overlay")
    if any(args.pose):
        first = HEADS[args.head][1] if args.head != "none" else skeleton_for(args.nif[0], args.skeleton)
        head_bind = skel(first).world[HEAD_BONE]
        delta = pose_delta(head_bind, *args.pose)
        for m in nif_meshes + head_meshes:
            apply_pose(m, delta)
    return nif_meshes, head_meshes, {_rel(k): v.world[HEAD_BONE].round(6).tolist() for k, v in skel_cache.items()}


def frame_points(target, nif_meshes, head_meshes, head_y):
    every = np.concatenate([m.verts for m in nif_meshes + head_meshes])
    if target == "front_of_face" and nif_meshes:
        pts = np.concatenate([m.verts for m in nif_meshes])
        front = pts[pts[:, 1] > head_y + 3.0]
        return front if len(front) >= 8 else pts
    return every


def render_views(args, scene, nif_meshes, head_meshes):
    bpy = _bpy()
    cam_data = bpy.data.cameras.new("cam")
    cam_data.lens = args.lens
    cam_data.sensor_width = 36.0
    cam_data.sensor_fit = "HORIZONTAL"
    cam = bpy.data.objects.new("cam", cam_data)
    scene.collection.objects.link(cam)
    scene.camera = cam
    rig = make_rig()
    tan_half = 18.0 / args.lens
    every = np.concatenate([m.verts for m in nif_meshes + head_meshes])
    radius = float(np.linalg.norm(every.max(0) - every.min(0)) / 2)
    heads_only = [m for m in head_meshes if m.role == "head"]
    head_y = 0.0 if not heads_only else float(np.median(np.concatenate([m.verts for m in heads_only])[:, 1]))
    out_dir = Path(args.out_dir)
    views = []
    for name in args.views:
        label, az, el, target = VIEWS[name]
        d, right, up = view_basis(az, el)
        pts = frame_points(target, nif_meshes, head_meshes, head_y)
        margin = 0.04 if target == "front_of_face" else 0.07
        centre, dist, shift = fit_camera(pts, d, right, up, tan_half, margin)
        place_camera(cam, centre, d, right, up, dist, shift, tan_half)
        place_rig(rig, centre, d, right, up, radius, args.exposure)
        scene.render.filepath = str(out_dir / f"{name}.png")
        t0 = time.time()
        bpy.ops.render.render(write_still=True)
        views.append({"name": name, "label": label, "file": f"{name}.png",
                      "seconds": round(time.time() - t0, 1)})
        print(f"[blender_render] {name}: {views[-1]['seconds']} s", flush=True)
    return views


def main(argv):
    args = parse_args(argv)
    shas = {_rel(_abs(p)): hashlib.sha1(_abs(p).read_bytes()).hexdigest() for p in args.nif + args.extra_nif}
    nif_meshes, head_meshes, binds = load_all(args)       # hashes taken just before loading
    if not nif_meshes and not head_meshes:
        raise SystemExit("nothing to render: give --nif and/or --head")
    out_dir = Path(args.out_dir)                          # created only once the input loaded
    out_dir.mkdir(parents=True, exist_ok=True)
    scene = reset_scene(args)
    make_world(scene)
    materials = {}
    for m in nif_meshes:
        mat, desc = nif_material(m, args)
        materials[m.name] = desc
        make_object(m, mat)
    if head_meshes:
        skin = head_material(HEADS[args.head][2]) if args.head != "none" else None
        overlay = head_material(OVERLAY_COLOUR, "M_overlay", 0.8) if args.extra_nif else None
        for m in head_meshes:
            make_object(m, skin if m.role == "head" else overlay)
            if m.role == "overlay":
                materials[m.name] = "matte dark brown (overlay, textures not shown)"
    views = render_views(args, scene, nif_meshes, head_meshes)
    report = {
        "nifs": [_rel(_abs(p)) for p in args.nif],
        "extra_nifs": [_rel(_abs(p)) for p in args.extra_nif],
        "nif_sha1": shas,
        "head": args.head, "head_nif": _rel(HEADS[args.head][0]) if args.head != "none" else None,
        "pose_deg": dict(zip(("yaw", "pitch", "roll"), args.pose)),
        "checker": args.checker, "samples": args.samples, "res": args.res,
        "head_bone_bind_by_skeleton": binds,
        "meshes": [{"name": m.name, "source": m.source, "role": m.role, **m.report,
                    "material": materials.get(m.name, "matte skin (head overlay)")}
                   for m in nif_meshes + head_meshes],
        "clearance_to_head": clearance(nif_meshes, [m for m in head_meshes if m.role == "head"]),
        "views": views,
        "blender": _bpy().app.version_string,
    }
    (out_dir / "report.json").write_text(json.dumps(report, indent=1), encoding="utf-8")
    print(f"[blender_render] wrote {len(views)} views to {out_dir}", flush=True)


if __name__ == "__main__":
    main(sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else [])
