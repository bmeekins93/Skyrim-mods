"""Fit reference: vanilla head meshes reconstructed in Skyrim model space -> work/fit.json.

Run from the repo root:  .venv/bin/python src/cfm/fit.py   (03_mesh.py also runs it when needed)

Heads (vanilla SSE data from PyNifly's test assets at the commit pinned in tools/nifly/build_niflydll.py):
  human    = malehead.nif           skinned to skeleton.nif
  argonian = maleheadargonian.nif   skinned to skeletonbeast.nif
  khajiit  = maleheadkhajiit.nif    skinned to skeletonbeast.nif  (meshes/actors/character/character
             assets/ in PyNifly)
Every reference file is checked against its pinned sha256 (byte-identical fallback copies of the files
that joined the sparse checkout later live in tests/fixtures/vanilla/, see reference_path).
Headgear references for the twine (src/03_mesh.py; human skeleton, informational except the hair gate):
  helmet-SMP.nif               vanilla-style skinned helmet (also the transform proof below)
  maleorchair27.nif            hair MaleOrcHair27 (BSDynamicTriShape, NPC Head [Head]), a dreadlock
                               mohawk; made for the orc head, so ~17 % of it lies inside the human head
  meshes/draugr lich01 hood.nif  skinned cloth hood (PyNifly test asset, textures\_00\actors\lich\;
                               made for the human skeleton, vertices stored in model space)
One beast mesh serves both Khajiit and Argonians (ARMA FaceMaskAA_Beast), so the beast variants are
fitted to an anchor that combines both heads (see kind_anchor) and are clearance-checked against both.
Vertices are stored in skin space; model space is  sum_i w_i * bind_i @ skin_to_bone_i @ v  with
bind_i the bone's world transform in the skeleton (see cfm.geom_xform). The stored global-to-skin
transform is only cross-checked: maleheadargonian.nif stores an identity global-to-skin although
its vertices are head-bone relative, so it is NOT a reliable source on its own.

Proof of the transform math: helmet-SMP.nif (a vanilla-style skinned helmet whose vertices are
already in model space, z ~ 114-133) must enclose the reconstructed human head: its crown sits under
the helmet shell, rays from the head centre through every upper-head vertex hit the helmet only
beyond that vertex, and the two share a centre line.

Measurements per head (model units, +Z up, +Y forward, +X actor's right):
  nose_tip       vertex with max y (beast: the snout tip)
  chin_z         steepest drop of the centre-line front profile below the mouth: where the face
                 surface turns under the jaw (the lowest point of the chin seen from the front)
  crown_z        max z of the bald scalp within 2 units of the centre line (Khajiit ear tips, which
                 reach z 135.2, are not scalp; hair is added later as a fixed allowance)
  eye_line_z     eye-socket concavity: the front height field minus a 1-unit Gaussian blur of itself
                 has its deepest pit per side in the band above the nose; mean z of both pits.
                 Cross-checked against the skeleton's NPCEyeBone and the half-height proportion.
  head_half_width max |x| above the chin (ears included); back_of_head_y min y above the
                 chin/eye midpoint (cranium, not the beast neck frills)
  ears           outermost lateral point per side (+ centroid of SBP_143_EARS where present)
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np  # noqa: E402
from scipy import ndimage  # noqa: E402

from cfm.geom_mesh import (boundary_loops, face_cross, height_field, ray_hits, weld,  # noqa: E402
                           winding_number)
from cfm.geom_render import Canvas, hstack, vstack  # noqa: E402
from cfm.geom_xform import (HEAD_BONE, NifFile, bone_consistency, shape_skin,  # noqa: E402
                            skeleton_binds, skinned_model_positions)
from cfm.paths import PYNIFLY_TESTS, ROOT, SKELETONS, WORK  # noqa: E402

HEAD_SOURCES = {
    "human": ("SkyrimSE/malehead.nif", "skeleton.nif"),
    "argonian": ("SkyrimSE/maleheadargonian.nif", "skeletonbeast.nif"),
    "khajiit": ("SkyrimSE/meshes/actors/character/character assets/maleheadkhajiit.nif", "skeletonbeast.nif"),
}
HELMET_SOURCE = ("SkyrimSE/helmet-SMP.nif", "Helmet", "skeleton.nif")
# Headgear the twine is measured against (03_mesh.py): (file, shape name or None = first, skeleton)
HEADGEAR_SOURCES = {
    "helmet": HELMET_SOURCE,
    "hair": ("SkyrimSE/maleorchair27.nif", "MaleOrcHair27", "skeleton.nif"),
    "hood": ("SkyrimSE/meshes/draugr lich01 hood.nif", "Hood", "skeleton.nif"),
}
# Not vanilla: PyNifly's custom "cheetah" female head (Khajiit-derived topology, textures
# actors\character\cheetahfemale). Used only as an informational clearance reference for beast_f.
EXTRA_REFERENCES = {"beast_f": [("SkyrimSE/CitrusFemHead.nif", "skeletonbeast_female.nif")]}
# sha256 of every reference NIF (PyNifly bcb723aba2ca4a80e976d957efbde69b33f2e56f, tests/tests/...)
REFERENCE_SHA256 = {
    "SkyrimSE/malehead.nif": "b9ab886ebe506ab744793406ffef3a409a6a7e2299441cc5cb3a1bb63d737760",
    "SkyrimSE/maleheadargonian.nif": "55c220562af4d2096763440e13d424029b9ce4902e2d20a69d10251b7c87bf9b",
    "SkyrimSE/meshes/actors/character/character assets/maleheadkhajiit.nif":
        "de5602f85a1c5be4b312120a5b0ab0fc9add371b47c04e1db35b8f93e62bab6b",
    "SkyrimSE/helmet-SMP.nif": "fb02d4aed6745d60d912c1d7c8c6ee616053e9328b9c6723431c101684adfaa1",
    "SkyrimSE/CitrusFemHead.nif": "a02db3d747653e1459c8418eeccb394a9e23dbad0e259eb511f20d2177447d35",
    "SkyrimSE/maleorchair27.nif": "ef6f02e5bed6aee68c4f73b8667af2f6118158c23bf20212cf83b7dd37e01e60",
    "SkyrimSE/meshes/draugr lich01 hood.nif": "b95d4a3454676a1702c25a2fdc44c2fc998fcef6cfba722362184403047a11e6",
}
VANILLA_FIXTURES = ROOT / "tests" / "fixtures" / "vanilla"   # fallback copies, see reference_path
KIND_HEADS = {"human": ("human",), "beast": ("argonian", "khajiit")}
VARIANT_KIND = {"human_m": "human", "human_f": "human", "beast_m": "beast", "beast_f": "beast"}
VARIANT_SKELETON = {"human_m": "skeleton.nif", "human_f": "skeleton_female.nif",
                    "beast_m": "skeletonbeast.nif", "beast_f": "skeletonbeast_female.nif"}
HAIR_ALLOWANCE_UNITS = 1.5   # crown hair allowance used to size the mask (not the twine clearance)
CROWN_STRIP = 2.0            # scalp crown = highest vertex with |x| <= this (excludes Khajiit ears)
EAR_PARTITION = 143          # SBP_143_EARS
SIDE_EAR_BAND = 2.0          # ears whose centroid is within this of the eye line sit beside the head
                             # (human); Khajiit ears (centroid 6.3 above) sit on top of it
GRID = 0.05                  # height-field resolution (units)
FIT_JSON = WORK / "fit.json"
RENDERS = ROOT / "docs" / "renders"
# Source files whose change makes work/fit.json stale (03_mesh.py checks their mtimes)
FIT_DEPENDENCIES = [Path(__file__), Path(__file__).with_name("geom_mesh.py"),
                    Path(__file__).with_name("geom_xform.py")]


@dataclass
class Head:
    kind: str
    path: Path
    skeleton: Path
    verts: np.ndarray            # model space, bind pose
    tris: np.ndarray
    tri_parts: np.ndarray        # partition id per triangle
    skin_report: dict = field(default_factory=dict)


def _rel(p: Path) -> str:
    return str(Path(p).resolve().relative_to(ROOT))


def _sha256(path: Path) -> str:
    import hashlib
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def reference_path(rel: str) -> Path:
    """Path of a PyNifly reference NIF, checked against its pinned sha256.

    Looks in third_party/PyNifly/tests/tests/<rel> (the sparse checkout listed in
    tools/nifly/build_niflydll.py) first, then for the byte-identical copy in tests/fixtures/vanilla/:
    a checkout made before a file joined PYNIFLY_SPARSE does not get it from build_niflydll.py, which
    leaves checkouts already at the pinned commit alone (tests/fixtures/vanilla/provenance.json)."""
    for cand in (PYNIFLY_TESTS / rel, VANILLA_FIXTURES / Path(rel).name):
        if cand.exists():
            want = REFERENCE_SHA256.get(rel)
            if want and _sha256(cand) != want:
                raise RuntimeError(f"{cand}: sha256 differs from the pinned reference {rel}")
            return cand
    raise FileNotFoundError(f"reference NIF {rel} not found in {PYNIFLY_TESTS} or {VANILLA_FIXTURES}; "
                            "run tools/setup.sh")


def reference_files() -> list:
    """Every reference NIF and skeleton the fit depends on (for staleness checks)."""
    out = [reference_path(rel) for rel, _ in HEAD_SOURCES.values()] + [reference_path(HELMET_SOURCE[0])]
    return out + sorted({SKELETONS / skel for _, skel in HEAD_SOURCES.values()} | {SKELETONS / HELMET_SOURCE[2]})


def load_skinned_shape(nif_path: Path, skeleton: Path, shape_name: str | None = None):
    """(model-space verts, tris, partition ids per tri, skin report) of one skinned shape."""
    nif = NifFile(str(nif_path))
    shape = nif.shapes[0] if shape_name is None else nif.shape_dict[shape_name]
    skin = shape_skin(shape)
    binds = skeleton_binds(skeleton, skin["bones"])
    stored = np.array(shape.verts, float)
    model = skinned_model_positions(stored, skin, binds)
    cons = bone_consistency(skin, binds)
    mats = [c["skin_to_model"] for c in cons.values()]
    g2s_model = (stored @ np.linalg.inv(skin["global_to_skin"])[:3, :3].T
                 + np.linalg.inv(skin["global_to_skin"])[:3, 3])
    parts = shape.partitions
    pt = np.array(shape.partition_tris, int)
    tri_parts = np.array([parts[i].id for i in pt]) if len(parts) else np.zeros(len(shape.tris), int)
    report = {
        "shape": shape.name,
        "block": shape.blockname,
        "bones": skin["bones"],
        "stored_bbox": [stored.min(0).round(4).tolist(), stored.max(0).round(4).tolist()],
        "model_bbox": [model.min(0).round(4).tolist(), model.max(0).round(4).tolist()],
        "bone_disagreement_max": float(max(np.abs(m - mats[0]).max() for m in mats)),
        "global_to_skin_translation": skin["global_to_skin"][:3, 3].round(4).tolist(),
        "inverse_g2s_vs_bones_max_dev": float(np.abs(g2s_model - model).max()),
    }
    return model, np.array(shape.tris, int), tri_parts, report


def load_head(name: str) -> Head:
    """One reference head by name (human / argonian / khajiit), in model space."""
    rel, skel = HEAD_SOURCES[name]
    path, skeleton = reference_path(rel), SKELETONS / skel
    v, t, parts, report = load_skinned_shape(path, skeleton)
    return Head(name, path, skeleton, v, t, parts, report)


def load_kind_heads(kind: str) -> list:
    """All reference heads one mask kind must fit (human: 1, beast: Argonian + Khajiit)."""
    return [load_head(name) for name in KIND_HEADS[kind]]


def load_extra_references(variant: str) -> list:
    """Informational (non-vanilla) clearance references for a variant: [(name, verts, tris)]."""
    out = []
    for rel, skel in EXTRA_REFERENCES.get(variant, []):
        v, t, _, _ = load_skinned_shape(reference_path(rel), SKELETONS / skel)
        out.append((Path(rel).stem, v, t))
    return out


def load_helmet():
    rel, shape, skel = HELMET_SOURCE
    v, t, _, report = load_skinned_shape(reference_path(rel), SKELETONS / skel, shape)
    return v, t, report


def load_headgear() -> dict:
    """{name: (model-space verts, tris, skin report)} for the helmet, hair and hood references."""
    out = {}
    for name, (rel, shape, skel) in HEADGEAR_SOURCES.items():
        v, t, _, report = load_skinned_shape(reference_path(rel), SKELETONS / skel, shape)
        out[name] = (v, t, {"source": rel, **report})
    return out


# --- measurements ---------------------------------------------------------------------------


def _front_field(head: Head):
    v = head.verts
    xs = np.arange(np.floor(v[:, 0].min()) - 1, np.ceil(v[:, 0].max()) + 1 + 1e-9, GRID)
    zs = np.arange(np.floor(v[:, 2].min()) - 1, np.ceil(v[:, 2].max()) + 1 + 1e-9, GRID)
    return xs, zs, height_field(v, head.tris, xs, zs, front=True)


def _centre_profile(xs, zs, hf, half_width=0.5):
    cols = hf[:, np.abs(xs) <= half_width]
    prof = np.full(len(zs), np.nan)
    ok = np.any(np.isfinite(cols), axis=1)
    prof[ok] = np.nanmax(cols[ok], axis=1)
    return prof


def measure_chin(zs, prof, nose_z):
    """z of the steepest centre-profile drop between nose-9 and nose-2 (face turning under the jaw)."""
    p = np.interp(zs, zs[np.isfinite(prof)], prof[np.isfinite(prof)])
    p = ndimage.gaussian_filter1d(p, 0.15 / GRID)
    grad = np.gradient(p, zs)
    win = (zs > nose_z - 9) & (zs < nose_z - 2)
    i = np.nonzero(win)[0][np.argmax(grad[win])]
    return float(zs[i]), float(p[i])


def measure_eyes(xs, zs, hf, nose_z, half_width):
    """Deepest eye-socket pit per side: front height field minus its 1-unit Gaussian blur.

    Searched above the nose and between 15% and 65% of the head half-width from the centre line
    (outside that band the temples and the nose bridge are concave too)."""
    filled = np.where(np.isfinite(hf), hf, np.nanmin(hf))
    resid = ndimage.gaussian_filter(filled - ndimage.gaussian_filter(filled, 1.0 / GRID), 0.15 / GRID)
    band = (zs > nose_z + 0.5) & (zs < nose_z + 6.0)
    lo, hi = 0.15 * half_width, 0.65 * half_width
    out = {}
    for side, sel in (("right", (xs > lo) & (xs < hi)), ("left", (xs < -lo) & (xs > -hi))):
        sub = np.where(np.isfinite(hf[np.ix_(band, sel)]), resid[np.ix_(band, sel)], np.inf)
        iz, ix = np.unravel_index(np.argmin(sub), sub.shape)
        zi, xi = np.nonzero(band)[0][iz], np.nonzero(sel)[0][ix]
        out[side] = {"point": [float(xs[xi]), float(hf[zi, xi]), float(zs[zi])], "depth": float(sub[iz, ix])}
    return out


def measure_ears(head: Head, chin_z: float):
    """Outermost point per side; where the head has an SBP_143_EARS partition also its centroid, top
    (highest vertex) and front-to-back (y) extent."""
    v = head.verts
    above = v[:, 2] >= chin_z
    out = {}
    for side, sign in (("right", 1.0), ("left", -1.0)):
        cand = np.nonzero(above & (sign * v[:, 0] > 0))[0]
        i = cand[np.argmax(sign * v[cand, 0])]
        ear = {"outermost": v[i].round(4).tolist()}
        ear_tris = head.tris[head.tri_parts == EAR_PARTITION]
        if len(ear_tris):
            idx = np.unique(ear_tris)
            idx = idx[sign * v[idx, 0] > 0]
            ear["partition_centroid"] = v[idx].mean(0).round(4).tolist()
            ear["partition_top"] = v[idx[np.argmax(v[idx, 2])]].round(4).tolist()
            ear["partition_y_range"] = [round(float(v[idx, 1].min()), 4), round(float(v[idx, 1].max()), 4)]
        out[side] = ear
    return out


def eye_bone_z(skeleton: Path) -> float:
    from cfm.geom_xform import xf_to_mat
    return float(xf_to_mat(NifFile(str(skeleton)).nodes["NPCEyeBone"].global_transform)[2, 3])


def measure(head: Head) -> dict:
    v = head.verts
    above_neck = v[:, 2] > v[:, 2].min() + 3.0
    i_nose = np.nonzero(above_neck)[0][np.argmax(v[above_neck, 1])]
    nose = v[i_nose]
    xs, zs, hf = _front_field(head)
    prof = _centre_profile(xs, zs, hf)
    chin_z, chin_y = measure_chin(zs, prof, nose[2])
    strip = v[np.abs(v[:, 0]) <= CROWN_STRIP]
    crown = strip[np.argmax(strip[:, 2])]
    upper = v[:, 2] >= chin_z
    half_w = float(np.abs(v[upper, 0]).max())
    eyes = measure_eyes(xs, zs, hf, nose[2], half_w)
    eye_z = 0.5 * (eyes["left"]["point"][2] + eyes["right"]["point"][2])
    cranium = v[v[:, 2] >= 0.5 * (chin_z + eye_z)]  # skip the neck / beast neck frills
    back = cranium[np.argmin(cranium[:, 1])]
    vw, imap = weld(v, 1e-3)
    loops = [{"n": len(lp), "centroid": vw[lp].mean(0).round(3).tolist()} for lp in boundary_loops(imap[head.tris])]
    centre = np.array([0.0, 0.5 * (nose[1] + back[1]), 0.5 * (chin_z + crown[2])])
    return {
        "nose_tip": nose.round(4).tolist(),
        "chin_z": round(chin_z, 4),
        "chin_point": [0.0, round(chin_y, 4), round(chin_z, 4)],
        "crown_z": round(float(crown[2]), 4),
        "crown_point": crown.round(4).tolist(),
        "chin_to_crown": round(float(crown[2] - chin_z), 4),
        "eye_line_z": round(eye_z, 4),
        "eyes": eyes,
        "eye_line_crosscheck": {
            "NPCEyeBone_z": round(eye_bone_z(head.skeleton), 4),
            "half_height_proportion_z": round(0.5 * (chin_z + float(crown[2])), 4),
        },
        "head_half_width": round(half_w, 4),
        "back_of_head_y": round(float(back[1]), 4),
        "back_of_head_point": back.round(4).tolist(),
        "ears": measure_ears(head, chin_z),
        "centre": centre.round(4).tolist(),
        "bbox": [v.min(0).round(4).tolist(), v.max(0).round(4).tolist()],
        "boundary_loops": loops,
        "winding_number_at_centre": round(float(winding_number(centre[None], v, head.tris)[0]), 4),
    }


# --- transform proof against the vanilla-style helmet --------------------------------------------


def helmet_check(head: Head, meas: dict, helmet_v, helmet_t) -> dict:
    """Upper head inside helmet shell: rays from the head centre through each head vertex above the
    eye line must reach the vertex before the first helmet hit (the inner shell)."""
    v = head.verts
    c = np.array([0.0, meas["centre"][1], meas["eye_line_z"]])
    upper = v[v[:, 2] > meas["eye_line_z"]]
    d = upper - c
    r = np.linalg.norm(d, axis=1)
    hits = ray_hits(np.repeat(c[None], len(d), 0), d / r[:, None], helmet_v, helmet_t)
    outer = np.array([h[-1] - ri if len(h) else np.nan for h, ri in zip(hits, r)])
    inner = np.array([h[0] - ri if len(h) else np.nan for h, ri in zip(hits, r)])
    tested = np.isfinite(outer)
    hu = helmet_v[helmet_v[:, 2] > meas["eye_line_z"]]
    head_c = 0.5 * (upper.min(0) + upper.max(0))
    helm_c = 0.5 * (hu.min(0) + hu.max(0))
    return {
        "upper_head_vertices": int(len(upper)),
        "rays_hitting_helmet": int(tested.sum()),
        "under_outer_shell": int((outer[tested] > 0).sum()),
        "outside_outer_shell": int((outer[tested] <= 0).sum()),
        "outer_gap_min": round(float(np.nanmin(outer)), 4),
        "outer_gap_median": round(float(np.nanmedian(outer)), 4),
        "inner_surface_gap_median": round(float(np.nanmedian(inner)), 4),
        "within_1_unit_of_inner_surface": int((np.abs(inner[tested]) < 1.0).sum()),
        "crown_gap_z": round(float(helmet_v[:, 2].max() - v[:, 2].max()), 4),
        "centre_offset_xy": (head_c - helm_c)[:2].round(4).tolist(),
        "helmet_z_range": [round(float(helmet_v[:, 2].min()), 3), round(float(helmet_v[:, 2].max()), 3)],
    }


# --- debug render ----------------------------------------------------------------------------


def render(head: Head, meas: dict, helmet_v, helmet_t, anchor: dict | None = None):
    """Front + side debug panel of one head inside helmet-SMP.nif, with the measured markers.

    anchor (beast heads): the combined kind anchor's eye line, drawn as a second line."""
    allv = np.vstack([head.verts, helmet_v])
    lo, hi = allv.min(0), allv.max(0)
    centre = 0.5 * (lo + hi)
    centre[0] = 0.0
    ppu = min(22.0, 400.0 / (hi[1] - lo[1]), 560.0 / (hi[2] - lo[2]))
    panels = []
    for view in ("front", "side"):
        cv = Canvas(view, centre, ppu, (460, 640))
        cv.scene([(helmet_v, helmet_t, (205, 215, 235)), (head.verts, head.tris, (225, 180, 150))])
        _helmet_outline(cv, helmet_v, helmet_t)
        cv.hline([0, 0, meas["crown_z"]], (200, 0, 0), f"crown z={meas['crown_z']:.2f}")
        cv.hline([0, 0, meas["eye_line_z"]], (0, 120, 0), f"eye line z={meas['eye_line_z']:.2f}")
        if anchor is not None:
            cv.hline([0, 0, anchor["eye_line_z"]], (200, 120, 0),
                     f"beast mask eye anchor z={anchor['eye_line_z']:.2f}", dash=3)
        cv.hline([0, 0, meas["chin_z"]], (0, 0, 200), f"chin z={meas['chin_z']:.2f}")
        eb = meas["eye_line_crosscheck"]["NPCEyeBone_z"]
        cv.marker(meas["nose_tip"], (220, 0, 160), "nose" if head.kind == "human" else "snout")
        cv.marker(meas["crown_point"], (200, 0, 0), "scalp crown")
        cv.marker(meas["back_of_head_point"], (120, 0, 200), "back")
        cv.marker([0, meas["eyes"]["left"]["point"][1], eb], (0, 160, 160), "NPCEyeBone")
        for side in ("left", "right"):
            cv.marker(meas["eyes"][side]["point"], (0, 150, 0))
            cv.marker(meas["ears"][side]["outermost"], (230, 120, 0), "widest" if side == "right" else None)
        if view == "front":
            w = meas["head_half_width"]
            cv.vline([w, 0, 0], (230, 120, 0), f"+/-{w:.2f}")
            cv.vline([-w, 0, 0], (230, 120, 0))
        cv.title(f"{head.kind} ({head.path.name}) - {view}; blue = helmet-SMP.nif")
        panels.append(cv.image())
    return hstack(panels)


def _helmet_outline(cv: Canvas, v, t):
    """Silhouette edges of the helmet (edges between a front- and a back-facing triangle)."""
    fc = face_cross(v, t)
    facing = fc @ cv.toward > 0
    edges = {}
    for i, tri in enumerate(t):
        for a, b in ((tri[0], tri[1]), (tri[1], tri[2]), (tri[2], tri[0])):
            edges.setdefault((min(a, b), max(a, b)), []).append(facing[i])
    for (a, b), f in edges.items():
        if len(f) == 1 or f[0] != f[1]:
            cv.polyline(v[[a, b]], (60, 90, 170), 1)


# --- driver ----------------------------------------------------------------------------------


def head_bone_binds() -> dict:
    out = {}
    for variant, skel in VARIANT_SKELETON.items():
        m = skeleton_binds(SKELETONS / skel, [HEAD_BONE])[HEAD_BONE]
        out[variant] = {"skeleton": _rel(SKELETONS / skel), "bind": m.round(8).tolist()}
    return out


def kind_anchor(heads: dict, kind: str) -> dict:
    """Placement anchor of one mask kind from its reference heads' measurements.

    nose_tip: the most forward nose/snout (the mask must clear every head); eye line, chin, crown:
    the mean over the heads (one mesh is a compromise between Argonian and Khajiit); half-width:
    max; back of head: min y."""
    hs = {n: heads[n] for n in KIND_HEADS[kind]}
    nose_from = max(hs, key=lambda n: hs[n]["nose_tip"][1])
    mean = lambda key: round(float(np.mean([h[key] for h in hs.values()])), 4)  # noqa: E731
    return {
        "heads": list(hs),
        "nose_tip": hs[nose_from]["nose_tip"], "nose_from": nose_from,
        "eye_line_z": mean("eye_line_z"), "chin_z": mean("chin_z"), "crown_z": mean("crown_z"),
        "chin_to_crown": mean("chin_to_crown"),
        "centre": np.mean([h["centre"] for h in hs.values()], axis=0).round(4).tolist(),
        "eye_pit_y": round(float(np.mean([[h["eyes"][s]["point"][1] for s in ("left", "right")]
                                          for h in hs.values()])), 4),
        "head_half_width": max(h["head_half_width"] for h in hs.values()),
        "back_of_head_y": min(h["back_of_head_y"] for h in hs.values()),
        "side_ears": side_ears(hs),
        "rule": "nose = most forward head; eye line / chin / crown = mean over the heads",
    }


def side_ears(heads: dict) -> dict | None:
    """Ears beside the head (partition centroid within SIDE_EAR_BAND of the eye line) that the twine
    must pass over: highest top over the heads and both sides, their front-to-back (y) extent and the
    mean centroid of the +X (right) ear, or None
    (Argonian: no ear partition; Khajiit: ears on top of the head, which the hull clears instead)."""
    tops, cents, ys = [], [], []
    for h in heads.values():
        for ear in h["ears"].values():
            if "partition_centroid" in ear and abs(ear["partition_centroid"][2] - h["eye_line_z"]) <= SIDE_EAR_BAND:
                tops.append(ear["partition_top"][2])
                ys += ear["partition_y_range"]
                if ear["partition_centroid"][0] > 0:
                    cents.append(ear["partition_centroid"])
    if not tops:
        return None
    return {"top_z": round(float(max(tops)), 4), "centroid_right": np.mean(cents, axis=0).round(4).tolist(),
            "y_range": [round(float(min(ys)), 4), round(float(max(ys)), 4)]}


def compute_fit(render_images: bool = True) -> dict:
    helmet_v, helmet_t, helmet_report = load_helmet()
    fit = {
        "units": "Skyrim model space, bind pose: +Z up, +Y forward, +X actor's right",
        "hair_allowance_units": HAIR_ALLOWANCE_UNITS,
        "variant_kind": VARIANT_KIND,
        "kind_heads": {k: list(v) for k, v in KIND_HEADS.items()},
        "head_bone_bind": head_bone_binds(),
        "helmet": {"source": _rel(reference_path(HELMET_SOURCE[0])), **helmet_report},
        "heads": {},
        "anchors": {},
    }
    loaded = {}
    for name in HEAD_SOURCES:
        head = load_head(name)
        meas = measure(head)
        loaded[name] = (head, meas)
        fit["heads"][name] = {"source": _rel(head.path), "skeleton": _rel(head.skeleton),
                              "sha256": _sha256(head.path), "skin": head.skin_report, **meas,
                              "helmet_check": helmet_check(head, meas, helmet_v, helmet_t)}
    for kind in KIND_HEADS:
        fit["anchors"][kind] = kind_anchor(fit["heads"], kind)
    if render_images:
        RENDERS.mkdir(parents=True, exist_ok=True)
        for kind, names in KIND_HEADS.items():
            anchor = fit["anchors"][kind] if len(names) > 1 else None
            imgs = [render(*loaded[n], helmet_v, helmet_t, anchor) for n in names]
            vstack(imgs).save(RENDERS / f"fit_{kind}.png")
    return fit


def check_fit(fit: dict) -> list:
    """Acceptance gates; returns a list of failure strings (empty = pass)."""
    bad = []
    for kind, h in fit["heads"].items():
        if h["sha256"] != REFERENCE_SHA256[HEAD_SOURCES[kind][0]]:
            bad.append(f"{kind}: reference head changed (sha256 {h['sha256']})")
        if h["skin"]["bone_disagreement_max"] > 1e-3:
            bad.append(f"{kind}: bones disagree on skin->model ({h['skin']['bone_disagreement_max']})")
        if not (h["chin_z"] < h["eye_line_z"] < h["crown_z"]):
            bad.append(f"{kind}: chin/eye/crown out of order")
        if abs(h["eye_line_z"] - h["eye_line_crosscheck"]["NPCEyeBone_z"]) > 2.5:
            bad.append(f"{kind}: eye line {h['eye_line_z']} far from NPCEyeBone")
        if h["winding_number_at_centre"] < 0.5:
            bad.append(f"{kind}: head winding is not outward (winding number {h['winding_number_at_centre']})")
    hc = fit["heads"]["human"]["helmet_check"]
    if (hc["outside_outer_shell"] > 0 or hc["crown_gap_z"] <= 0 or hc["outer_gap_median"] > 2.5
            or max(abs(x) for x in hc["centre_offset_xy"]) > 1.0):
        bad.append(f"human head is not inside helmet-SMP.nif: {hc}")
    for kind, names in KIND_HEADS.items():
        if set(fit["anchors"].get(kind, {}).get("heads", [])) != set(names):
            bad.append(f"{kind}: anchor does not combine {names}")
    return bad


def load_fit(path: Path = FIT_JSON, recompute: bool = False) -> dict:
    if recompute or not Path(path).exists():
        fit = compute_fit()
        write_fit(fit, path)
        return fit
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_fit(fit: dict, path: Path = FIT_JSON) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(fit, indent=1), encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", type=Path, default=FIT_JSON)
    ap.add_argument("--no-render", action="store_true")
    args = ap.parse_args()
    fit = compute_fit(render_images=not args.no_render)
    write_fit(fit, args.out)
    for kind, a in fit["anchors"].items():
        print(f"anchor {kind} ({'+'.join(a['heads'])}): nose y {a['nose_tip'][1]} ({a['nose_from']}) "
              f"eye {a['eye_line_z']} chin {a['chin_z']} crown {a['crown_z']}")
    for kind, h in fit["heads"].items():
        print(f"{kind}: nose {h['nose_tip']} chin {h['chin_z']} eye {h['eye_line_z']} "
              f"(NPCEyeBone {h['eye_line_crosscheck']['NPCEyeBone_z']}) crown {h['crown_z']} "
              f"half-width {h['head_half_width']} back {h['back_of_head_y']}")
        print(f"   skin: {h['skin']['bone_disagreement_max']:.2e} bone disagreement, inverse-g2s dev "
              f"{h['skin']['inverse_g2s_vs_bones_max_dev']:.3f}; helmet: {h['helmet_check']}")
    bad = check_fit(fit)
    for b in bad:
        print("FAIL:", b)
    print(f"wrote {args.out}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
