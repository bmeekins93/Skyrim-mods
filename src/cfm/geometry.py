"""Cardboard mask geometry for one variant: slab (front/back caps + corrugated side wall), tape
strips and the twine string, authored in Skyrim model space (bind pose) with NIF UVs.

Photo -> model mapping (docs/CONTRACTS.md): image x -> -X, image y -> -Z.
  scale s (units / px) = size_factor * (NPC chin->crown + hair allowance) * variant scale
                         / (photo chin->crown in px)
  the photo eye midpoint lands on (x = 0, z = NPC eye line);
  the back face sits forward_offset in front of the nose tip (beast: the most forward snout) and is
  bent around a vertical cylinder of radius bend_radius_units so the edges curve back toward the head.
NPC values come from the kind's anchor in work/fit.json (cfm.fit.kind_anchor): the human head, or
for the beast mesh a compromise between the Argonian and Khajiit heads; clearance is checked
against every head of the kind.
Triangles are wound counter-clockwise seen from outside (the convention measured on vanilla
helmet-SMP.nif, see 03_mesh.py), normals point outward, smooth inside a part, hard at slab edges.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field

import numpy as np

from .geom_mesh import closest_points, face_cross, vertex_normals, winding_number
from .geom_sheet import (Bend, points_in_polygon, conforming_triangulation, interior_points,
                         make_boundary)
from .geom_mesh import ray_hits
from .geom_twine import (ASYM_SIDE_UNITS, ASYM_UNITS, CLEARANCE_FACTOR, CROWN_MARGIN, EAR_TOP_GAP, SAG_DROP_UNITS,
                         SMOOTH_SIGMA, STRING_HEAD_GAP, TEMPLE_FROM_EYE_PIT, ClearanceProfile, adaptive_resample,
                         back_swell, contact_heights, ear_gaps, string_path, tape_strip, tube)

VARIANTS = ("human_m", "human_f", "beast_m", "beast_f")
SEED = 2117                    # IMG_2117: every random choice derives from this
BOUNDARY_SPACING = 0.30        # outline sample spacing (units, human_m; scales with the mask)
INTERIOR_SPACING = 0.75        # cap interior point spacing (units, human_m)
TWINE_SIDES = 6
TWINE_MAX_SEGMENT = 0.6        # twine ring spacing on straight runs (units)
MAX_STRING_CLEARANCE = 2.5     # string_clearance_units above this is refused (see build_twine)
ATTACH_INSET = 1.25            # string end, measured in from the mask edge (units, human_m)
LIFT_INSET = 0.30              # where the string lifts off the back face, from the edge
TAPE_LENGTH = 2.2             # tape strip length (units); width = length / 2.8 (atlas tape aspect)
MASK_SHAPE, TWINE_SHAPE = "CardboardMask", "Twine"


@dataclass
class Part:
    name: str
    shape: str                 # NIF shape this part belongs to
    verts: np.ndarray
    tris: np.ndarray
    uvs: np.ndarray            # NIF convention: u right, v down
    normals: np.ndarray
    region: str                # atlas region (front/back/edge/tape) or 'twine'


@dataclass
class MaskMesh:
    variant: str
    parts: list
    report: dict = field(default_factory=dict)

    def shape_arrays(self, shape: str):
        """Concatenate the parts of one NIF shape -> (verts, tris, uvs, normals)."""
        vs, ts, us, ns, base = [], [], [], [], 0
        for p in self.parts:
            if p.shape != shape:
                continue
            vs.append(p.verts)
            ts.append(p.tris + base)
            us.append(p.uvs)
            ns.append(p.normals)
            base += len(p.verts)
        return np.concatenate(vs), np.concatenate(ts), np.concatenate(us), np.concatenate(ns)


# --- mapping ------------------------------------------------------------------------------------


def variant_scale(cfg: dict, variant: str) -> float:
    """scale_human or scale_beast, times scale_female for the female variants."""
    kind, sex = variant.split("_")
    s = float(cfg["scale_beast"] if kind == "beast" else cfg["scale_human"])
    return s * (float(cfg["scale_female"]) if sex == "f" else 1.0)


def photo_mapping(outline: dict, head: dict, cfg: dict, variant: str, hair: float) -> dict:
    lm = outline["landmarks"]
    eye = 0.5 * (np.array(lm["eye_image_left"], float) + np.array(lm["eye_image_right"], float))
    photo_h = float(lm["chin"][1] - lm["crown"][1])
    npc_h = head["crown_z"] + hair - head["chin_z"]
    s = float(cfg["size_factor"]) * npc_h * variant_scale(cfg, variant) / photo_h
    return {"units_per_px": s, "eye_px": eye.tolist(), "photo_chin_crown_px": photo_h,
            "npc_chin_crown_plus_hair": npc_h, "variant_scale": variant_scale(cfg, variant)}


def image_to_sheet(xy, m: dict) -> np.ndarray:
    e = np.array(m["eye_px"])
    return -(np.asarray(xy, float) - e) * m["units_per_px"]


def sheet_to_image(ah, m: dict) -> np.ndarray:
    return np.array(m["eye_px"]) - np.asarray(ah, float) / m["units_per_px"]


def region_uv(region: dict, img_xy, mirror: bool = False) -> np.ndarray:
    u0, v0, u1, v1 = region["uv_rect"]
    x0, y0, x1, y1 = region["image_rect"]
    p = np.asarray(img_xy, float)
    u = u0 + (p[:, 0] - x0) / (x1 - x0) * (u1 - u0)
    v = v0 + (p[:, 1] - y0) / (y1 - y0) * (v1 - v0)
    if mirror:
        u = u0 + u1 - u
    return np.stack([u, v], axis=1)


def orient(verts, tris, normals) -> np.ndarray:
    """Flip triangles whose cross(v1-v0, v2-v0) disagrees with their vertex normals."""
    t = np.asarray(tris, int).copy()
    bad = np.einsum("ij,ij->i", face_cross(verts, t), np.asarray(normals)[t].sum(axis=1)) < 0
    t[bad] = t[bad][:, [0, 2, 1]]
    return t


# --- slab -----------------------------------------------------------------------------------------


def build_slab(poly_sheet, bend: Bend, layout: dict, m: dict, k: float, wobble: float, rng):
    """Front cap, back cap and side wall parts. k = linear size factor relative to human_m."""
    edge = layout["regions"]["edge"]
    bnd = make_boundary(poly_sheet, BOUNDARY_SPACING * k, float(edge["tile_length_units"]), wobble, rng)
    inner = interior_points(bnd.pts, INTERIOR_SPACING * k, rng)
    pts, tris, nb, param = conforming_triangulation(bnd.pts, inner)
    img = sheet_to_image(pts, m)
    parts = []
    for face, sign, region in (("front", 1.0, "front"), ("back", -1.0, "back")):
        v = bend.lift(pts, face)
        n = sign * bend.radial(pts)
        reg = layout["regions"][region]
        uv = region_uv(reg, img, bool(reg.get("mirror_u", region == "back")))
        parts.append(Part(f"{face}_cap", MASK_SHAPE, v, orient(v, tris, n), uv, n, region))
    parts.append(build_wall(pts[:nb], param, bnd, bend, edge))
    return parts, bnd, pts[:nb]


def build_wall(loop, param, bnd, bend: Bend, edge: dict) -> Part:
    """Side wall quads around the CCW outline; vertices duplicated where an edge tile restarts."""
    frac = bnd.tile_frac(param)
    seam = bnd.is_seam(param)
    n2 = _loop_normals(loop)
    n3 = n2[:, :1] * bend.tangent_a(loop) + n2[:, 1:] * np.array([0.0, 0.0, 1.0])
    n3 /= np.linalg.norm(n3, axis=1, keepdims=True)
    u0, v0, u1, v1 = edge["uv_rect"]
    verts, norms, uvs, tris = [], [], [], []
    n = len(loop)
    for k in range(n):                       # quad k spans loop[k] -> loop[k+1]
        k2 = (k + 1) % n
        f_end = 1.0 if seam[k2] else frac[k2]
        base = len(verts)
        for idx, f in ((k, frac[k]), (k2, f_end)):
            for face, vv in (("front", v0), ("back", v1)):
                verts.append(bend.lift(loop[idx:idx + 1], face)[0])
                norms.append(n3[idx])
                uvs.append((u0 + f * (u1 - u0), vv))
        tris += [(base, base + 1, base + 3), (base, base + 3, base + 2)]
    v, nn = np.array(verts), np.array(norms)
    v, t, uv, nn = _merge_wall(v, np.array(tris), np.array(uvs), nn)
    return Part("edge_wall", MASK_SHAPE, v, orient(v, t, nn), uv, nn, "edge")


def _loop_normals(loop) -> np.ndarray:
    e = np.roll(loop, -1, axis=0) - loop
    en = np.stack([e[:, 1], -e[:, 0]], axis=1)
    en /= np.linalg.norm(en, axis=1, keepdims=True)
    n = en + np.roll(en, 1, axis=0)
    return n / np.linalg.norm(n, axis=1, keepdims=True)


def _merge_wall(v, t, uv, n):
    """Weld wall vertices that share position, UV and normal (adjacent quads)."""
    key = np.round(np.hstack([v * 1e4, uv * 1e6, n * 1e3])).astype(np.int64)
    _, first, inv = np.unique(key, axis=0, return_index=True, return_inverse=True)
    inv = inv.reshape(-1)
    return v[first], inv[t], uv[first], n[first]


# --- twine + tape ---------------------------------------------------------------------------------


def edge_crossings(loop, h: float):
    """Leftmost and rightmost a where the outline crosses the horizontal line at height h."""
    p, q = loop, np.roll(loop, -1, axis=0)
    hit = (p[:, 1] - h) * (q[:, 1] - h) <= 0
    t = (h - p[hit, 1]) / np.where(q[hit, 1] != p[hit, 1], q[hit, 1] - p[hit, 1], 1)
    a = p[hit, 0] + t * (q[hit, 0] - p[hit, 0])
    return float(a.min()), float(a.max())


def build_twine(loop, bend: Bend, layout: dict, cfg: dict, heads, anchor: dict, kind: str, k: float, rng, hair=()):
    """Twine tube + tape strips. heads = [(name, verts, tris)] the string must wrap (union); hair =
    [(name, verts, tris)] reference hair it rides over as well (STRING_HEAD_GAP off its surface).

    The string rides string_clearance_units * CLEARANCE_FACTOR[kind] over the bald head(s) round the
    sides, swelling by back_swell(cfg, kind) of that toward the back centre (config string_back_swell
    for humans), over the ears where the kind has ears beside the head (anchor["side_ears"]); see
    cfm.geom_twine."""
    r = float(cfg["string_radius_units"])
    asym = float(cfg["string_asymmetry"])
    sag = float(cfg["string_sag"])
    clear = float(cfg.get("string_clearance_units", STRING_HEAD_GAP))
    if clear < STRING_HEAD_GAP:
        raise ValueError(f"string_clearance_units {clear} < the minimum head gap {STRING_HEAD_GAP}")
    if not clear <= MAX_STRING_CLEARANCE:
        # the halo gates in 03_mesh.py scale with this value, so they cannot catch a typo
        raise ValueError(f"string_clearance_units {clear} > {MAX_STRING_CLEARANCE} (the twine would "
                         "float as a loose hoop; 1.0 is the default)")
    c_side = max(clear * CLEARANCE_FACTOR[kind], STRING_HEAD_GAP)
    swell = back_swell(cfg, kind)
    c_back = c_side * (1.0 + swell)
    attach, h_side = {}, {}
    for side in (+1, -1):
        h_side[side] = h = side * asym * ASYM_UNITS
        a_min, a_max = edge_crossings(loop, h)
        a_edge = a_max if side > 0 else a_min
        attach[side] = (a_edge - side * ATTACH_INSET * k, a_edge - side * LIFT_INSET * k)
    ears = anchor.get("side_ears")
    z_cap = anchor["crown_z"] - CROWN_MARGIN
    ear_top = ears["top_z"] if ears else None
    side_z = contact_heights(bend.z_eye, asym, r, ear_top, z_cap)
    z_all = list(side_z.values()) + [bend.z_eye + h for h in h_side.values()]
    margin = 1.0 + 3 * SMOOTH_SIGMA
    profile = ClearanceProfile([(v, t) for _, v, t in heads], min(z_all) - sag * SAG_DROP_UNITS - margin,
                               max(z_all) + margin, c_side + r, [0.0, anchor["centre"][1]],
                               y_cut=anchor["eye_pit_y"] + TEMPLE_FROM_EYE_PIT, grow_front=STRING_HEAD_GAP + r,
                               hair=[(v, t) for _, v, t in hair], grow_hair=STRING_HEAD_GAP + r,
                               grow_back=c_back + r, z_need=max(z_all) + EAR_TOP_GAP)
    # smoothing (and the ring spacing) round the string off over the ear: lift until the built tube
    # clears the ear top by EAR_TOP_GAP
    extra = 0.0
    for _ in range(6):
        side_z = contact_heights(bend.z_eye, asym, r, ear_top, z_cap, extra)
        dense, path_info = string_path(bend, attach, profile, r, sag, asym, side_z)
        path = adaptive_resample(dense, TWINE_MAX_SEGMENT)
        if not ears:
            break
        short = EAR_TOP_GAP - min(g for g in ear_gaps(path, ears, r).values() if g is not None)
        if short <= 2e-4:
            break
        extra += short + 0.002
    else:
        raise ValueError(f"twine does not clear the ear tops by {EAR_TOP_GAP} after lifting {extra:.3f}")
    nominal = 2 * asym * ASYM_SIDE_UNITS
    tile = float(layout.get("twine", {}).get("tile_length_units", 2.0))
    v, t, uv, n, length = tube(path, r, TWINE_SIDES, tile)
    twine = Part("twine", TWINE_SHAPE, v, orient(v, t, n), uv, n, "twine")
    parts = [twine]
    if cfg.get("tape_strip", True):
        tape_rect = layout["regions"]["tape"]["uv_rect"]
        tape_aspect = float(layout["regions"]["tape"].get("aspect", 2.8))
        for side in (+1, -1):
            a_end = attach[side][0]
            angle = float(rng.uniform(-12, 12))
            tv, tt, tuv = tape_strip(bend, a_end, side, h_side[side], r, TAPE_LENGTH * k,
                                     TAPE_LENGTH * k / tape_aspect, angle, tape_rect)
            want = -bend.radial(bend.to_sheet(tv)[:, :2])
            tt = orient(tv, tt, want)
            parts.append(Part(f"tape_{'r' if side > 0 else 'l'}", MASK_SHAPE, tv, tt, tuv,
                              vertex_normals(tv, tt), "tape"))
    info = {"attach_sheet": {("+X" if s > 0 else "-X"): [round(x, 4) for x in a] for s, a in attach.items()},
            "length": round(length, 3), "rings": int(len(path)), "radius": r, "clearance_units": clear,
            "back_swell": swell, "clearance_side_back": [round(c_side, 4), round(c_back, 4)],
            "min_head_gap": STRING_HEAD_GAP,
            "side_ears": ears, "ear_lift_extra": round(extra, 4),
            "ear_gaps": ({("+X" if s_ > 0 else "-X"): None if g is None else round(g, 4)
                          for s_, g in ear_gaps(path, ears, r).items()} if ears else None),
            "high_side_cap": round(z_cap, 4),
            "high_side_capped": bool(path_info["side_height_difference"] < nominal - 1e-6),
            "rides_over_hair": [name for name, _, _ in hair],
            "profile_centre": [round(float(x), 4) for x in profile.centre], **path_info,
            "u_max": round(float(uv[:, 0].max()), 4)}
    return parts, path, info


# --- top level ------------------------------------------------------------------------------------


def build_variant(variant: str, outline: dict, layout: dict, fit: dict, heads, cfg: dict, hair_refs=(),
                  head_clearance: bool = True) -> MaskMesh:
    """heads = [(name, verts, tris)]: every reference head of the variant's kind (model space);
    hair_refs = [(name, verts, tris)]: reference hair the twine rides over (03_mesh.TWINE_OVER_HAIR).
    head_clearance=False skips the (slow) mask/twine-to-head distance report."""
    kind = fit["variant_kind"][variant]
    anchor = fit["anchors"][kind]
    hair = float(fit["hair_allowance_units"])
    m = photo_mapping(outline, anchor, cfg, variant, hair)
    k = m["units_per_px"] / photo_mapping(outline, fit["anchors"]["human"], cfg, "human_m", hair)["units_per_px"]
    mode = cfg.get("silhouette_mode") or outline["selected_mode"]
    poly = image_to_sheet(outline["modes"][mode]["polygon"], m)
    bend = Bend(float(cfg["bend_radius_units"]), float(cfg["thickness_units"]),
                anchor["nose_tip"][1] + float(cfg["forward_offset_units"]), anchor["eye_line_z"])
    rng = np.random.default_rng(SEED)
    parts, bnd, loop = build_slab(poly, bend, layout, m, k, float(cfg["edge_wobble_units"]), rng)
    twine_parts, path, twine_info = build_twine(loop, bend, layout, cfg, heads, anchor, kind, k, rng, hair_refs)
    mesh = MaskMesh(variant, parts + twine_parts)
    top, bottom = float(loop[:, 1].max() + bend.z_eye), float(loop[:, 1].min() + bend.z_eye)
    mesh.report = {
        "variant": variant, "kind": kind, "heads": [name for name, _, _ in heads], "silhouette_mode": mode,
        "mapping": m, "linear_scale_vs_human_m": round(k, 6),
        "bend": {"radius_back": bend.radius_back, "thickness": bend.thickness,
                 "y_back_at_centre": bend.y_back0, "z_eye": bend.z_eye},
        "edge_tiles": {"n": bnd.n_tiles, "tile_length": round(bnd.tile_length, 4)},
        "twine": twine_info,
        "triangles": {p.name: int(len(p.tris)) for p in mesh.parts},
        "triangles_total": int(sum(len(p.tris) for p in mesh.parts)),
        "vertices_total": int(sum(len(p.verts) for p in mesh.parts)),
        "mask_bbox": [np.concatenate([p.verts for p in parts]).min(0).round(3).tolist(),
                      np.concatenate([p.verts for p in parts]).max(0).round(3).tolist()],
        "overhang": {name: {"mask_top_minus_crown_plus_hair": round(top - fit["heads"][name]["crown_z"] - hair, 3),
                            "mask_bottom_minus_chin": round(bottom - fit["heads"][name]["chin_z"], 3),
                            "back_face_gap_to_nose": round(bend.y_back0 - fit["heads"][name]["nose_tip"][1], 3)}
                     for name, _, _ in heads},
    }
    if head_clearance:
        mesh.report["clearance"] = {name: clearance(mesh, v, t) for name, v, t in heads}
    mesh.report["twine_vs_slab"] = twine_inside_slab(mesh, bend, loop)
    return mesh


def clearance(mesh: MaskMesh, head_v, head_t) -> dict:
    """Distance from every mask / twine vertex to the head surface + inside test (winding number)."""
    out = {}
    for shape in (MASK_SHAPE, TWINE_SHAPE):
        v = mesh.shape_arrays(shape)[0]
        d, _, _ = closest_points(v, head_v, head_t)
        w = winding_number(v, head_v, head_t)
        out[shape] = {"min_distance": round(float(d.min()), 4),
                      "closest_vertex": v[int(np.argmin(d))].round(3).tolist(),
                      "inside_head": int(np.sum(w > 0.5)), "max_winding": round(float(w.max()), 4)}
    return out


def shell_depth(points, centre, shell_v, shell_t) -> np.ndarray:
    """Per point, along the ray from `centre` through it: how far it lies beyond the shell's
    outermost hit (> 0 outside the shell, < 0 under it); nan where the ray misses the shell."""
    p = np.asarray(points, float)
    c = np.asarray(centre, float)
    d = p - c
    r = np.linalg.norm(d, axis=1)
    hits = ray_hits(np.repeat(c[None], len(p), 0), d / r[:, None], shell_v, shell_t)
    return np.array([r_ - h[-1] if len(h) else np.nan for h, r_ in zip(hits, r)])


def twine_vs_headgear(line, centre, gear_v, gear_t, tol: float, head=None, thin_below: float | None = None,
                      run_y_max: float = np.inf, near: float | None = None) -> dict:
    """Where the twine centre line runs relative to a headgear / hair mesh, by length: rays from
    `centre` through the midpoint of every segment. `visible` = the ray misses the mesh, or the string
    centre lies no more than `tol` under its outermost surface (the string still shows through / on
    it). `run` = the part with y <= run_y_max (the run round the back and sides of the head);
    `covered_run_visible_fraction` = visible share of the run where the mesh is over it at all.
    With head = (verts, tris) and thin_below, also the length under a layer no thicker than
    thin_below over the head along the same ray (such hair the string is meant to ride over).
    With near, the length whose centre lies within +-near of the surface (z-fighting / flicker risk)."""
    q = np.asarray(line, float)
    mid, seg = 0.5 * (q[1:] + q[:-1]), np.linalg.norm(np.diff(q, axis=0), axis=1)
    depth = shell_depth(mid, centre, gear_v, gear_t)
    covered = np.isfinite(depth)
    visible = ~covered | (depth >= -tol)
    run = mid[:, 1] <= run_y_max

    def frac(m, within=None):
        w = np.ones(len(seg), bool) if within is None else within
        return round(float(seg[m & w].sum() / max(float(seg[w].sum()), 1e-12)), 4)

    out = {"length": round(float(seg.sum()), 3), "covered_fraction": frac(covered),
           "visible_fraction": frac(visible), "poke_out_fraction": frac(covered & (depth > tol)),
           "run_length": round(float(seg[run].sum()), 3), "run_visible_fraction": frac(visible, run),
           "covered_run_length": round(float(seg[run & covered].sum()), 3),
           "covered_run_visible_fraction": frac(visible, run & covered) if (run & covered).any() else None,
           "deepest_under": round(float(-np.nanmin(depth)), 4) if covered.any() else None,
           "max_poke": round(float(np.nanmax(depth)), 4) if covered.any() else None}
    if near is not None:
        out["near_surface_fraction"] = frac(covered & (np.abs(depth) <= near))
    if head is not None and thin_below is not None:
        c = np.asarray(centre, float)
        dirs = (mid - c) / np.linalg.norm(mid - c, axis=1)[:, None]
        head_r = np.array([h[-1] if len(h) else np.nan for h in ray_hits(np.repeat(c[None], len(mid), 0), dirs, *head)])
        thick = np.linalg.norm(mid - c, axis=1) - depth - head_r
        thin = covered & (thick <= thin_below)
        out["thin_layer_length"] = round(float(seg[thin].sum()), 3)
        out["buried_under_thin_layer_length"] = round(float(seg[thin & ~visible].sum()), 4)
    return out


def twine_inside_slab(mesh: MaskMesh, bend: Bend, loop) -> dict:
    v = mesh.shape_arrays(TWINE_SHAPE)[0]
    sh = bend.to_sheet(v)
    inside = (sh[:, 2] > 1e-3) & (sh[:, 2] < bend.thickness - 1e-3) & points_in_polygon(sh[:, :2], loop)
    return {"vertices_inside_cardboard": int(inside.sum()),
            "min_offset_behind_back_face": round(float(-sh[:, 2].max()), 4)}


def save_npz(path, mesh: MaskMesh) -> None:
    arrays = {}
    for i, p in enumerate(mesh.parts):
        for key in ("verts", "tris", "uvs", "normals"):
            arrays[f"p{i}_{key}"] = getattr(p, key)
    meta = [{"name": p.name, "shape": p.shape, "region": p.region} for p in mesh.parts]
    np.savez_compressed(path, meta=json.dumps({"variant": mesh.variant, "parts": meta, "report": mesh.report}),
                        **arrays)


def load_npz(path) -> MaskMesh:
    z = np.load(path, allow_pickle=False)
    meta = json.loads(str(z["meta"]))
    parts = [Part(pm["name"], pm["shape"], z[f"p{i}_verts"], z[f"p{i}_tris"], z[f"p{i}_uvs"],
                  z[f"p{i}_normals"], pm["region"]) for i, pm in enumerate(meta["parts"])]
    return MaskMesh(meta["variant"], parts, meta["report"])
