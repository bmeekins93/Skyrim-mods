"""Stage 4a - mask geometry: outline + atlas layout + head fit -> work/mesh/<variant>.npz (+ .obj).

Variants human_m, human_f, beast_m, beast_f (see cfm.geometry for the construction). Inputs default
to work/outline.json and work/atlas_layout.json; if those do not exist yet the synthetic fixtures in
tests/fixtures/ are used WITH A LOUD WARNING (the meshes are then placeholders).

Acceptance gates (exit code 1 on failure), evidence in <out-dir>/mesh_report.json:
  * triangle winding: vanilla helmet-SMP.nif's cross(v1-v0, v2-v0) agrees with its stored normals
    (counter-clockwise = front face); every part of ours agrees with its normals >= 99 %
  * unit normals; triangle budget <= 4000 per NIF
  * clearance vs every reference head of the variant's kind (human: malehead.nif; beast: Argonian
    and Khajiit): no mask/twine vertex inside a head, mask >= 0.5 and twine >= 0.1 units away;
    no twine vertex inside the cardboard
  * the twine rides its design clearance g(theta) over the back and sides of the head, no more (no
    halo, no lasso): g = c_side over the sides, swelling to c_back at the back centre
    (cfm.geom_twine: c_side = string_clearance_units * CLEARANCE_FACTOR[kind], c_back = c_side *
    (1 + BACK_EXTRA[kind])). Behind the head centre, the distance d of its vertices to the nearest
    head has d / g p10 >= 0.7 and median >= 0.85; on EACH head of the kind (a real NPC wears one),
    d minus the human-scale clearance (string_clearance_units * (1 + BACK_EXTRA[kind] * back
    weight)) has median <= 0.5 and max <= 1.5 (taut bridges behind the ears and frills)
  * typical-hair shell: along the whole run round the back and sides, the string surface stays
    TYPICAL_HAIR_FACTOR * c_side over the nearest bald head (0.8 units for humans by default)
  * human variants: the string passes above both ear tops (its surface >= EAR_TOP_MIN over the ear top
    all along the ear's front-to-back extent, segments sampled every 0.02) and, against the hair
    MaleOrcHair27 (PyNifly test asset maleorchair27.nif, a dreadlock mohawk standing 2-4 units off
    the scalp), no length of it lies under hair thinner than c_side - radius and >= 70 % of the run
    where the hair covers it at all is visible (outside the hair or within a radius of its surface);
    gated while string_clearance_units >= 1.0 (the default), informational below
  * twine u < 8 (restarted every few tiles) so BSTriShape's half-float UVs keep it exact
Informational (mesh_report.json "headgear"): the same measurements against helmet-SMP.nif and the
lich hood (the string is meant to ride over hair, so it weaves in and out of close-fitting headgear;
near_surface_fraction = length within 2 radii of the surface, which may flicker in game).
Renders: docs/renders/fit_mask_<variant>.png (each head of the kind + mask, textured with
work/textures/*_d.png when present) and fit_twine_headgear.png (human_m under the helmet, the hood
and the hair; string length under the headgear surface drawn red).
Fixture builds and builds with a non-default --out-dir render into --out-dir instead of docs/.

Run from the repo root:  .venv/bin/python src/03_mesh.py [--outline P] [--layout P] [--out-dir D]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parent))
from cfm import fit as fitmod  # noqa: E402
from cfm.geom_mesh import closest_points, vertex_normals, winding_agreement, write_obj  # noqa: E402
from cfm.geom_render import hstack, project, raster, vstack  # noqa: E402
from cfm.geom_twine import BACK_EXTRA, back_weight, densify, tube_centre_line  # noqa: E402
from cfm.geometry import (MASK_SHAPE, TWINE_SHAPE, TWINE_SIDES, VARIANTS, build_variant, clearance,  # noqa: E402
                          save_npz, shell_depth, twine_vs_headgear)
from cfm.paths import PYNIFLY_TESTS, ROOT, WORK, load_config  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures"
MESH_DIR = WORK / "mesh"
RENDERS = ROOT / "docs" / "renders"
TRI_BUDGET = 4000
MIN_MASK_CLEARANCE = 0.5      # units between any mask vertex and the head surface
MIN_TWINE_CLEARANCE = 0.1     # twine vertex to bald head (temples: its surface rests >= STRING_HEAD_GAP off)
BACK_GAP_MIN_FACTOR = 0.7     # behind the head centre, twine-to-nearest-head distance d / design g: p10 >= this,
BACK_GAP_MEDIAN_FACTOR = 0.85  # median >= this; per head, d - human-scale clearance:
BACK_GAP_MEDIAN_OVER = 0.5    # median <= this,
BACK_GAP_MAX_OVER = 1.5       # max <= this (taut bridges behind the ears and the beast frills)
TYPICAL_HAIR_FACTOR = 0.8     # the run's string surface stays this x c_side over the nearest bald head
EAR_TOP_MIN = 0.9             # string surface above the ear top wherever it runs over an ear (human; the
                              # design value is cfm.geom_twine.EAR_TOP_GAP = 1.0)
HAIR_MIN_COVERED_VISIBLE = 0.7  # share of the run under maleorchair27.nif's hair that is visible (human),
HAIR_GATE_MIN_CLEARANCE = 1.0  # gated when string_clearance_units >= this (the default; smaller values
                               # deliberately tuck the string under hair, config.yaml)
GEAR_TOL = 0.02               # helmet / hood: a string centre this far outside the shell "pokes out"
# Headgear references (cfm.fit.HEADGEAR_SOURCES) whose surface the human string's clearance hull also
# includes. Empty by default: ("hair",) makes the string ride over every MaleOrcHair27 dread too (100 %
# visible instead of ~77 % where the hair covers it), at the price of a tent over the mohawk that
# stands up to 3.8 units off the back of a bald head and 2.3 out of helmet-SMP.nif (measured;
# docs/NIF_NOTES.md section 7).
TWINE_OVER_HAIR: tuple = ()


def resolve_input(given: Path | None, default: Path, fixture: Path, what: str) -> tuple:
    """Return (path, is_fixture). Falls back to the synthetic fixture with a loud warning."""
    if given is not None:
        given = given.resolve()
        return given, given.is_relative_to(FIXTURES.resolve())
    if default.exists():
        return default, False
    bar = "!" * 78
    print(f"{bar}\nWARNING: {default.relative_to(ROOT)} not found - using SYNTHETIC {what} fixture "
          f"{fixture.relative_to(ROOT)}.\nThe meshes built now are placeholders; rerun after the "
          f"upstream stage has produced {default.name}.\n{bar}", file=sys.stderr)
    return fixture, True


def show(p: Path) -> str:
    """Path relative to the repo root when possible (for logs and reports)."""
    p = Path(p).resolve()
    return str(p.relative_to(ROOT)) if p.is_relative_to(ROOT) else str(p)


def vanilla_winding() -> dict:
    """Measure the triangle winding convention on a vanilla mesh that stores vertex normals."""
    from cfm.geom_xform import NifFile
    nif = NifFile(str(PYNIFLY_TESTS / "SkyrimSE" / "helmet-SMP.nif"))
    out = {}
    for s in nif.shapes:
        out[s.name] = round(winding_agreement(np.array(s.verts), np.array(s.tris), np.array(s.normals)), 5)
    return out


def part_checks(mesh) -> dict:
    out = {}
    for p in mesh.parts:
        nlen = np.linalg.norm(p.normals, axis=1)
        out[p.name] = {
            "winding_vs_normals": round(winding_agreement(p.verts, p.tris, p.normals), 5),
            "winding_vs_smooth": round(float(np.mean(np.einsum(
                "ij,ij->i", vertex_normals(p.verts, p.tris), p.normals) > 0)), 5),
            "normal_len_range": [round(float(nlen.min()), 6), round(float(nlen.max()), 6)],
        }
    return out


def design_clearance(points, centre_y: float, side: float, back: float) -> np.ndarray:
    """The twine's design clearance at the polar angle (about the head's vertical axis through y =
    centre_y) of each point: `side`, swelling to `back` at the back centre (cfm.geom_twine.back_weight)."""
    p = np.asarray(points, float)
    return side + (back - side) * back_weight(np.arctan2(p[:, 1] - centre_y, p[:, 0]))


def twine_standoff(mesh, heads, kind: str, centre_y: float) -> dict:
    """How far the twine rides off the back of the head: distance d of every twine vertex behind the
    head centre (y < centre_y) to the nearest reference head and to each one, against its design
    clearance g (c_side .. c_back) and the human-scale one (string_clearance_units, swelling by
    BACK_EXTRA[kind] at the back)."""
    tw = mesh.report["twine"]
    v = mesh.shape_arrays(TWINE_SHAPE)[0]
    back = v[:, 1] < centre_y
    per_head = {name: closest_points(v[back], hv, ht)[0] for name, hv, ht in heads}
    d = np.min(np.stack(list(per_head.values())), axis=0)
    g = design_clearance(v[back], centre_y, *tw["clearance_side_back"])
    c = tw["clearance_units"]
    g_ref = design_clearance(v[back], centre_y, c, c * (1 + BACK_EXTRA[kind]))
    pct = lambda a: [round(float(x), 4) for x in np.percentile(a, [0, 10, 50, 90, 100])]  # noqa: E731
    return {"vertices_behind_centre": int(back.sum()), "design_clearance_side_back": tw["clearance_side_back"],
            "nearest_head_min_p10_median_p90_max": pct(d), "nearest_over_design_min_p10_median_p90_max": pct(d / g),
            "per_head_min_p10_median_p90_max": {k: pct(x) for k, x in per_head.items()},
            "per_head_excess_over_reference_min_p10_median_p90_max": {k: pct(x - g_ref) for k, x in per_head.items()}}


def typical_hair_shell(mesh, heads, clearance_side: float, run_y: float) -> dict:
    """Along the run round the back and sides (centre line, sampled every 0.02, y <= run_y): the
    string surface's distance to the nearest bald head, against a 'typical hair' shell of
    TYPICAL_HAIR_FACTOR * clearance_side (and an absolute 0.8-unit one)."""
    line = densify(tube_centre_line(mesh.shape_arrays(TWINE_SHAPE)[0], TWINE_SIDES))
    q = line[line[:, 1] <= run_y]
    r = mesh.report["twine"]["radius"]
    gap = np.min([closest_points(q, hv, ht)[0] for _, hv, ht in heads], axis=0) - r
    shell = TYPICAL_HAIR_FACTOR * clearance_side
    return {"shell_units": round(shell, 4), "run_points": int(len(q)), "min_surface_gap": round(float(gap.min()), 4),
            "closest_point": q[int(np.argmin(gap))].round(3).tolist(),
            "outside_fraction": round(float(np.mean(gap >= shell - 1e-4)), 4),
            "outside_0_8_fraction": round(float(np.mean(gap >= 0.8 - 1e-4)), 4)}


def ear_pass(line, head, radius: float) -> dict:
    """Per ear (SBP_143_EARS partition, each side): over the stretch where the string centre line runs
    alongside the ear (same side, y within the ear's front-to-back extent; segments sampled every 0.02),
    its lowest surface point (z - radius) minus the ear top, and the closest the string surface comes to
    an ear vertex."""
    ear_idx = np.unique(head.tris[head.tri_parts == fitmod.EAR_PARTITION])
    q = densify(line)
    out = {}
    for side, sign in (("right", 1.0), ("left", -1.0)):
        ev = head.verts[ear_idx[sign * head.verts[ear_idx, 0] > 0]]
        if not len(ev):
            continue
        along = (sign * q[:, 0] > 0) & (q[:, 1] >= ev[:, 1].min()) & (q[:, 1] <= ev[:, 1].max())
        gap = float(np.min(q[along, 2] - radius) - ev[:, 2].max()) if along.any() else None
        near = float(np.min(np.linalg.norm(q[:, None, :] - ev[None], axis=2))) - radius
        out[side] = {"ear_top_z": round(float(ev[:, 2].max()), 4), "points_alongside": int(along.sum()),
                     "string_bottom_minus_ear_top": None if gap is None else round(gap, 4),
                     "closest_to_ear_vertex": round(near, 4)}
    return out


def headgear_report(mesh, fit, kind: str, gear: dict, heads, clear: float, radius: float) -> dict:
    """Twine vs the headgear references (cfm.fit.HEADGEAR_SOURCES), by length along its centre line.
    hair: `visible` within one string radius of the hair surface, thin layer = hair thinner than
    clear - radius over the head (clear = the side clearance); helmet / hood: informational, with
    the length within 2 radii of their surface (may flicker)."""
    a = fit["anchors"][kind]
    centre = [0.0, a["centre"][1], a["eye_line_z"]]
    line = tube_centre_line(mesh.shape_arrays(TWINE_SHAPE)[0], TWINE_SIDES)
    run_y = max(p[1] for p in mesh.report["twine"]["contact_points"])
    head = (heads[0][1], heads[0][2])
    out = {"ray_centre": centre, "run_y_max": round(float(run_y), 4)}
    for name, (gv, gt, src) in gear.items():
        rep = twine_vs_headgear(line, centre, gv, gt, tol=radius if name == "hair" else GEAR_TOL, head=head,
                                thin_below=clear - radius if name == "hair" else None, run_y_max=run_y,
                                near=None if name == "hair" else 2 * radius)
        rep["source"] = src["source"]
        rep["gated"] = (name == "hair" and kind == "human"
                        and mesh.report["twine"]["clearance_units"] >= HAIR_GATE_MIN_CLEARANCE)
        if kind != "human":
            rep["note"] = "informational: human headgear; the beast heads poke through it themselves"
        out[name] = rep
    return out


def gates(mesh, checks) -> list:
    r, bad = mesh.report, []
    for head, cl in r["clearance"].items():
        for shape in (MASK_SHAPE, TWINE_SHAPE):
            if cl[shape]["inside_head"]:
                bad.append(f"{shape}: {cl[shape]['inside_head']} vertices inside the {head} head")
        if cl[MASK_SHAPE]["min_distance"] < MIN_MASK_CLEARANCE:
            bad.append(f"mask too close to the {head} head: {cl[MASK_SHAPE]['min_distance']}")
        if cl[TWINE_SHAPE]["min_distance"] < MIN_TWINE_CLEARANCE:
            bad.append(f"twine too close to the {head} head: {cl[TWINE_SHAPE]['min_distance']}")
    st = r["twine_standoff"]
    ratio = st["nearest_over_design_min_p10_median_p90_max"]
    if ratio[1] < BACK_GAP_MIN_FACTOR or ratio[2] < BACK_GAP_MEDIAN_FACTOR:
        bad.append(f"twine does not ride its clearance {st['design_clearance_side_back']} over the back of the "
                   f"head (distance / design min/p10/median/p90/max {ratio})")
    for head, ex in st["per_head_excess_over_reference_min_p10_median_p90_max"].items():
        if ex[2] > BACK_GAP_MEDIAN_OVER or ex[4] > BACK_GAP_MAX_OVER:
            bad.append(f"twine floats off the back of the {head} head (excess over the clearance "
                       f"min/p10/median/p90/max {ex})")
    shell = r["typical_hair_shell"]
    if shell["outside_fraction"] < 1.0:
        bad.append(f"twine dips into a {shell['shell_units']}-unit typical-hair shell: {shell}")
    for head, ears in r.get("ear_pass", {}).items():
        for side, e in ears.items():
            if e["string_bottom_minus_ear_top"] is None or e["string_bottom_minus_ear_top"] < EAR_TOP_MIN:
                bad.append(f"twine does not pass above the {side} ear top of the {head} head: {e}")
    hair = r["headgear"].get("hair", {})
    if hair.get("gated"):
        if hair["buried_under_thin_layer_length"] > 0:
            bad.append(f"twine buried under hair thinner than its clearance: {hair}")
        if (hair["covered_run_visible_fraction"] or 0.0) < HAIR_MIN_COVERED_VISIBLE:
            bad.append(f"twine hidden in maleorchair27.nif's hair: {hair}")
    if r["twine"]["u_max"] >= 8:
        bad.append(f"twine u reaches {r['twine']['u_max']}: half-float UVs lose precision (step >= 2^-7)")
    if r["twine_vs_slab"]["vertices_inside_cardboard"]:
        bad.append(f"twine passes through the cardboard: {r['twine_vs_slab']}")
    if r["triangles_total"] > TRI_BUDGET:
        bad.append(f"{r['triangles_total']} triangles > budget {TRI_BUDGET}")
    for name, c in checks.items():
        if c["winding_vs_normals"] < 0.99:
            bad.append(f"{name}: winding agrees with normals on only {c['winding_vs_normals']:.3f}")
        if abs(c["normal_len_range"][0] - 1) > 1e-3 or abs(c["normal_len_range"][1] - 1) > 1e-3:
            bad.append(f"{name}: non-unit normals {c['normal_len_range']}")
    return [f"{mesh.variant}: {b}" for b in bad]


FLAT = {"front": (240, 230, 200), "back": (190, 150, 100), "edge": (150, 115, 70),
        "tape": (235, 225, 180), "twine": (150, 115, 70)}


def _mask_items(mesh, textures: dict) -> list:
    items = []
    for p in mesh.parts:
        tex = textures.get("twine" if p.region == "twine" else "mask")
        items.append((p.verts, p.tris, p.uvs, tex, FLAT[p.region]))
    return items


def render(mesh, heads, textures: dict, path: Path) -> None:
    """Evidence panel, one row per reference head: front, back of the mask (head hidden), side, top,
    back of the head. Textured from work/textures when available, flat colours otherwise."""
    parts = _mask_items(mesh, textures)
    mv = np.concatenate([p.verts for p in mesh.parts])
    rows = []
    for name, hv, ht in heads:
        head = [(hv, ht, None, None, (225, 185, 160))]
        allv = np.concatenate([mv, hv[hv[:, 2] > mv[:, 2].min() - 2]])
        c_mask, c_all = 0.5 * (mv.min(0) + mv.max(0)), 0.5 * (allv.min(0) + allv.max(0))
        ppu = 400 / float(np.max(allv.max(0) - allv.min(0)))
        panels = [("front", head + parts, c_mask, "front"), ("back", parts, c_mask, "back of mask (head hidden)"),
                  ("side", head + parts, c_all, "side"), ("top", head + parts, c_all, "top"),
                  ("back", head + parts, c_all, "back of head")]
        imgs = []
        for view, items, centre, label in panels:
            im = raster(view, items, centre, ppu, (480, 480))
            ImageDraw.Draw(im).text((8, 6), f"{mesh.variant} on {name} - {label}", fill=(20, 20, 20))
            imgs.append(im)
        rows.append(hstack(imgs))
    path.parent.mkdir(parents=True, exist_ok=True)
    vstack(rows).save(path)


GEAR_COLOUR = {"helmet": (175, 190, 215), "hood": (150, 165, 120), "hair": (95, 70, 50)}


def render_headgear(mesh, head, gear: dict, report: dict, textures: dict, path: Path) -> None:
    """The twine with each headgear reference (one row each): top view with the gear's
    cross-sections at the string's lowest and highest points (blue) over head + string, and back
    and side views with the gear on. String length under the gear surface
    (cfm.geometry.twine_vs_headgear) is drawn over the image in red, so it shows through."""
    name, hv, ht = head
    parts = _mask_items(mesh, textures)
    tw = mesh.shape_arrays(TWINE_SHAPE)[0]
    line = tube_centre_line(tw, TWINE_SIDES)
    size, ppu = (480, 480), 480 / 26.0
    head_i = [(hv, ht, None, None, (225, 185, 160))]
    rows = []
    for gname, (gv, gt, _) in gear.items():
        rep = report[gname]
        centre = np.array([0.0, 1.0, tw[:, 2].mean() + 1.0])
        top = raster("top", head_i + parts, centre, ppu, size)
        draw = ImageDraw.Draw(top)
        for z, col in ((float(tw[:, 2].max()), (40, 70, 200)), (float(tw[:, 2].min()), (90, 160, 230))):
            _section(draw, gv, gt, z, centre, ppu, size, col)
        draw.text((8, 6), f"top: {gname} sections at z={tw[:, 2].min():.1f}, {tw[:, 2].max():.1f} (blue)",
                  fill=(20, 20, 20))
        imgs = [top]
        depth = shell_depth(0.5 * (line[1:] + line[:-1]), report["ray_centre"], gv, gt)
        under = np.isfinite(depth) & (depth < -(mesh.report["twine"]["radius"] if gname == "hair" else GEAR_TOL))
        for view in ("back", "side"):
            items = head_i + [(gv, gt, None, None, GEAR_COLOUR.get(gname, (160, 160, 160)))] + parts
            im = raster(view, items, centre, ppu, size)
            d = ImageDraw.Draw(im)
            q = project(view, line, centre, ppu, size)
            for i in np.nonzero(under)[0]:
                d.line([tuple(q[i]), tuple(q[i + 1])], fill=(220, 30, 20), width=2)
            d.text((8, 6), f"{mesh.variant} on {name} + {gname}: {view}", fill=(20, 20, 20))
            cov = rep["covered_run_visible_fraction"]
            d.text((8, 22), f"visible {rep['visible_fraction']:.0%} of the string, back/side run "
                            f"{rep['run_visible_fraction']:.0%} (where the {gname} covers it "
                            f"{'-' if cov is None else f'{cov:.0%}'}); red = under the {gname}", fill=(20, 20, 20))
            imgs.append(im)
        rows.append(hstack(imgs))
    path.parent.mkdir(parents=True, exist_ok=True)
    vstack(rows).save(path)


def _section(draw, v, t, z, centre, ppu, size, color) -> None:
    """Draw the cross-section of mesh (v, t) with the plane at height z into a top-view image."""
    tri = v[t]
    above = tri[:, :, 2] > z
    for k in np.nonzero(above.any(1) & ~above.all(1))[0]:
        pts = []
        for i, j in ((0, 1), (1, 2), (2, 0)):
            a, b = tri[k, i], tri[k, j]
            if (a[2] > z) != (b[2] > z):
                pts.append(a + (z - a[2]) / (b[2] - a[2]) * (b - a))
        if len(pts) == 2:
            q = project("top", np.array(pts), centre, ppu, size)
            draw.line([tuple(q[0]), tuple(q[1])], fill=color, width=1)


def anchor_has_side_ears(fit, kind: str) -> bool:
    return bool(fit["anchors"][kind].get("side_ears"))


def fit_is_stale() -> bool:
    """work/fit.json is older than the fit code or any reference NIF it was computed from."""
    if not fitmod.FIT_JSON.exists():
        return True
    deps = list(fitmod.FIT_DEPENDENCIES) + fitmod.reference_files()
    return fitmod.FIT_JSON.stat().st_mtime < max(Path(p).stat().st_mtime for p in deps)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--outline", type=Path)
    ap.add_argument("--layout", type=Path)
    ap.add_argument("--variants", nargs="*", default=list(VARIANTS))
    ap.add_argument("--out-dir", type=Path, default=MESH_DIR)
    ap.add_argument("--refit", action="store_true", help="recompute work/fit.json")
    ap.add_argument("--no-render", action="store_true")
    args = ap.parse_args()

    outline_path, outline_fx = resolve_input(args.outline, WORK / "outline.json",
                                             FIXTURES / "outline_synthetic.json", "outline")
    layout_path, layout_fx = resolve_input(args.layout, WORK / "atlas_layout.json",
                                           FIXTURES / "atlas_layout_synthetic.json", "atlas layout")
    outline = json.loads(outline_path.read_text(encoding="utf-8"))
    layout = json.loads(layout_path.read_text(encoding="utf-8"))
    cfg = load_config()
    fit = fitmod.load_fit(recompute=args.refit or fit_is_stale())
    fail = [f"fit: {b}" for b in fitmod.check_fit(fit)]

    winding = vanilla_winding()
    if min(winding.values()) < 0.99:
        fail.append(f"vanilla winding convention unclear: {winding}")
    textures = {}
    for key in ("mask", "twine"):
        tex_path = WORK / "textures" / f"{key}_d.png"
        if tex_path.exists() and not layout_fx:
            textures[key] = Image.open(tex_path).convert("RGB")

    args.out_dir.mkdir(parents=True, exist_ok=True)
    # placeholder (fixture) builds and scratch --out-dir builds never overwrite the docs renders
    scratch = outline_fx or layout_fx or args.out_dir.resolve() != MESH_DIR.resolve()
    render_dir = args.out_dir if scratch else RENDERS
    head_objs = {kind: fitmod.load_kind_heads(kind) for kind in fitmod.KIND_HEADS}
    heads = {kind: [(h.kind, h.verts, h.tris) for h in hs] for kind, hs in head_objs.items()}
    gear = fitmod.load_headgear()
    report = {"inputs": {"outline": show(outline_path), "outline_is_fixture": outline_fx,
                         "layout": show(layout_path), "layout_is_fixture": layout_fx},
              "vanilla_winding_ccw_agreement": winding, "variants": {}}
    for variant in args.variants:
        kind = fit["variant_kind"][variant]
        over = [(g, *gear[g][:2]) for g in TWINE_OVER_HAIR] if kind == "human" else []
        mesh = build_variant(variant, outline, layout, fit, heads[kind], cfg, over)
        checks = part_checks(mesh)
        mesh.report["part_checks"] = checks
        mesh.report["inputs_are_fixtures"] = outline_fx or layout_fx
        mesh.report["twine_standoff"] = twine_standoff(mesh, heads[kind], kind, fit["anchors"][kind]["centre"][1])
        mesh.report["typical_hair_shell"] = typical_hair_shell(
            mesh, heads[kind], mesh.report["twine"]["clearance_side_back"][0],
            max(p[1] for p in mesh.report["twine"]["contact_points"]))
        line = tube_centre_line(mesh.shape_arrays(TWINE_SHAPE)[0], TWINE_SIDES)
        mesh.report["ear_pass"] = {h.kind: ear_pass(line, h, mesh.report["twine"]["radius"])
                                   for h in head_objs[kind] if anchor_has_side_ears(fit, kind)}
        mesh.report["headgear"] = headgear_report(mesh, fit, kind, gear, heads[kind],
                                                  mesh.report["twine"]["clearance_side_back"][0],
                                                  mesh.report["twine"]["radius"])
        mesh.report["extra_reference_clearance"] = {
            name: {"note": "informational, not vanilla", **clearance(mesh, v, t)}
            for name, v, t in fitmod.load_extra_references(variant)}
        fail += gates(mesh, checks)
        save_npz(args.out_dir / f"{variant}.npz", mesh)
        write_obj(args.out_dir / f"{variant}.obj",
                  [(p.name, p.verts, p.tris, p.uvs, p.normals) for p in mesh.parts])
        report["variants"][variant] = mesh.report
        cl = mesh.report["clearance"]
        print(f"{variant}: {mesh.report['triangles_total']} tris ({mesh.report['triangles']})")
        for head, c in cl.items():
            print(f"   vs {head}: mask>={c[MASK_SHAPE]['min_distance']}, twine>={c[TWINE_SHAPE]['min_distance']}, "
                  f"inside {c[MASK_SHAPE]['inside_head']}/{c[TWINE_SHAPE]['inside_head']}")
        tw = mesh.report["twine"]
        st = mesh.report["twine_standoff"]
        print(f"   twine: contacts at z {tw['side_heights']} (difference {tw['side_height_difference']}), back "
              f"drop {tw['back_drop']}, lowest point {tw['low_point']}; clearance side/back "
              f"{tw['clearance_side_back']}; behind head centre nearest-head distance min/p10/median/p90/max "
              f"{st['nearest_head_min_p10_median_p90_max']} "
              f"(/ design {st['nearest_over_design_min_p10_median_p90_max']})")
        for head, ex in st["per_head_excess_over_reference_min_p10_median_p90_max"].items():
            print(f"   on the {head} head: distance {st['per_head_min_p10_median_p90_max'][head]}, excess over "
                  f"the clearance {ex}")
        sh = mesh.report["typical_hair_shell"]
        print(f"   typical-hair shell {sh['shell_units']}: run outside {sh['outside_fraction']:.1%} (min surface gap "
              f"{sh['min_surface_gap']})")
        for head, ears in mesh.report["ear_pass"].items():
            print(f"   over the {head} ears: string bottom - ear top "
                  f"{ {s_: e['string_bottom_minus_ear_top'] for s_, e in ears.items()} }")
        for gname, g in mesh.report["headgear"].items():
            if gname == "ray_centre" or gname == "run_y_max":
                continue
            cov = g["covered_run_visible_fraction"]
            print(f"   {gname}{' (gated)' if g['gated'] else ''}: visible {g['visible_fraction']:.1%} of the string, "
                  f"back/side run {g['run_visible_fraction']:.1%}, where it covers the run "
                  f"{'-' if cov is None else f'{cov:.1%}'} of {g['covered_run_length']}; pokes out "
                  f"{g['poke_out_fraction']:.1%} (max {g['max_poke']}), deepest under {g['deepest_under']}"
                  + (f", within 2 radii of its surface {g['near_surface_fraction']:.1%}"
                     if "near_surface_fraction" in g else ""))
        if not args.no_render:
            render(mesh, heads[kind], textures, render_dir / f"fit_mask_{variant}.png")
            if variant == "human_m":
                render_headgear(mesh, heads[kind][0], gear, mesh.report["headgear"], textures,
                                render_dir / "fit_twine_headgear.png")
    report["failures"] = fail
    (args.out_dir / "mesh_report.json").write_text(json.dumps(report, indent=1), encoding="utf-8")
    for f in fail:
        print("FAIL:", f)
    print(f"wrote {show(args.out_dir)}/*.npz, *.obj, mesh_report.json" + ("" if args.no_render else
          f"; renders in {show(render_dir)}"))
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(main())
