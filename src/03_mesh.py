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
  * the twine hugs the back of the head (no halo): behind the head centre its median distance to
    the nearest head is <= 0.5 and its maximum (bridges behind the ears) <= 1.2
  * human variants: no twine vertex outside helmet-SMP.nif's outer shell along rays from the head
    centre (where the helmet covers that direction), so helmets hide the string
  * twine u < 8 (restarted every few tiles) so BSTriShape's half-float UVs keep it exact
Renders: docs/renders/fit_mask_<variant>.png (each head of the kind + mask, textured with
work/textures/*_d.png when present) and fit_twine_helmet.png (human_m inside helmet-SMP.nif).
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
from cfm.geometry import (MASK_SHAPE, TWINE_SHAPE, VARIANTS, build_variant, clearance, save_npz,  # noqa: E402
                          shell_check)
from cfm.paths import PYNIFLY_TESTS, ROOT, WORK, load_config  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures"
MESH_DIR = WORK / "mesh"
RENDERS = ROOT / "docs" / "renders"
TRI_BUDGET = 4000
MIN_MASK_CLEARANCE = 0.5      # units between any mask vertex and the head surface
MIN_TWINE_CLEARANCE = 0.1     # twine centre to bald head (its surface rests STRING_HEAD_GAP = 0.15 off)
MAX_BACK_GAP_MEDIAN = 0.5     # twine behind the head centre: median distance to the nearest head
MAX_BACK_GAP = 1.2            # ... and its maximum (taut bridges behind the ears)
HELMET_TOL = 0.02             # allowed poke through helmet-SMP's shell (the face-opening rim)


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


def twine_hug(mesh, heads, centre_y: float) -> dict:
    """How closely the twine follows the back of the head: distance of every twine vertex behind
    the head centre (y < centre_y) to the nearest reference head."""
    v = mesh.shape_arrays(TWINE_SHAPE)[0]
    back = v[:, 1] < centre_y
    per_head = {name: closest_points(v[back], hv, ht)[0] for name, hv, ht in heads}
    d = np.min(np.stack(list(per_head.values())), axis=0)
    pct = lambda a: [round(float(x), 4) for x in np.percentile(a, [0, 50, 90, 100])]  # noqa: E731
    return {"vertices_behind_centre": int(back.sum()), "nearest_head_min_median_p90_max": pct(d),
            "per_head_min_median_p90_max": {k: pct(x) for k, x in per_head.items()}}


def helmet_report(mesh, fit, kind: str, helmet) -> dict:
    a = fit["anchors"][kind]
    centre = [0.0, a["centre"][1], a["eye_line_z"]]
    rep = shell_check(mesh.shape_arrays(TWINE_SHAPE)[0], centre, *helmet, tol=HELMET_TOL)
    rep["ray_centre"] = centre
    rep["gated"] = kind == "human"
    if kind != "human":
        rep["note"] = "informational: helmet-SMP.nif is a human helmet; the beast heads poke through it themselves"
    return rep


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
    hug = r["twine_hug"]["nearest_head_min_median_p90_max"]
    if hug[1] > MAX_BACK_GAP_MEDIAN or hug[3] > MAX_BACK_GAP:
        bad.append(f"twine floats off the back of the head (min/median/p90/max {hug})")
    hel = r["helmet_twine"]
    if hel["gated"] and hel["outside"]:
        bad.append(f"twine pokes through helmet-SMP.nif: {hel}")
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


def render_helmet(mesh, head, helmet, textures: dict, path: Path) -> None:
    """The twine inside helmet-SMP.nif: top view with the helmet's cross-sections at the string's
    heights (blue) over head + string; back and side views with and without the helmet."""
    name, hv, ht = head
    gv, gt = helmet
    parts = _mask_items(mesh, textures)
    tw = mesh.shape_arrays(TWINE_SHAPE)[0]
    centre = np.array([0.0, 0.5 * (gv[:, 1].min() + gv[:, 1].max()), tw[:, 2].mean()])
    size, ppu = (480, 480), 480 / 22.0
    head_i = [(hv, ht, None, None, (225, 185, 160))]
    helm_i = [(gv, gt, None, None, (175, 190, 215))]
    top = raster("top", head_i + parts, centre, ppu, size)
    draw = ImageDraw.Draw(top)
    for z, col in ((float(tw[:, 2].max()), (40, 70, 200)), (float(tw[:, 2].min()), (90, 160, 230))):
        _section(draw, gv, gt, z, centre, ppu, size, col)
    draw.text((8, 6), f"top: helmet sections at z={tw[:, 2].min():.1f}, {tw[:, 2].max():.1f} (blue)",
              fill=(20, 20, 20))
    imgs = [top]
    for view, items, label in (("back", head_i + parts, "back, no helmet"), ("back", head_i + helm_i + parts,
                               "back, helmet on"), ("side", head_i + parts, "side, no helmet"),
                               ("side", head_i + helm_i + parts, "side, helmet on")):
        im = raster(view, items, centre, ppu, size)
        ImageDraw.Draw(im).text((8, 6), f"{mesh.variant} on {name}: {label}", fill=(20, 20, 20))
        imgs.append(im)
    path.parent.mkdir(parents=True, exist_ok=True)
    hstack(imgs).save(path)


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
    heads = {kind: [(h.kind, h.verts, h.tris) for h in fitmod.load_kind_heads(kind)] for kind in fitmod.KIND_HEADS}
    helmet = fitmod.load_helmet()[:2]
    report = {"inputs": {"outline": show(outline_path), "outline_is_fixture": outline_fx,
                         "layout": show(layout_path), "layout_is_fixture": layout_fx},
              "vanilla_winding_ccw_agreement": winding, "variants": {}}
    for variant in args.variants:
        kind = fit["variant_kind"][variant]
        mesh = build_variant(variant, outline, layout, fit, heads[kind], cfg)
        checks = part_checks(mesh)
        mesh.report["part_checks"] = checks
        mesh.report["inputs_are_fixtures"] = outline_fx or layout_fx
        mesh.report["twine_hug"] = twine_hug(mesh, heads[kind], fit["anchors"][kind]["centre"][1])
        mesh.report["helmet_twine"] = helmet_report(mesh, fit, kind, helmet)
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
        hel = mesh.report["helmet_twine"]
        print(f"   twine behind head centre: nearest-head distance min/median/p90/max "
              f"{mesh.report['twine_hug']['nearest_head_min_median_p90_max']}; helmet-SMP: {hel['outside']} of "
              f"{hel['covered']} covered vertices outside (min margin {hel['min_margin']}"
              f"{', informational' if not hel['gated'] else ''})")
        if not args.no_render:
            render(mesh, heads[kind], textures, render_dir / f"fit_mask_{variant}.png")
            if variant == "human_m":
                render_helmet(mesh, heads[kind][0], helmet, textures, render_dir / "fit_twine_helmet.png")
    report["failures"] = fail
    (args.out_dir / "mesh_report.json").write_text(json.dumps(report, indent=1), encoding="utf-8")
    for f in fail:
        print("FAIL:", f)
    print(f"wrote {show(args.out_dir)}/*.npz, *.obj, mesh_report.json" + ("" if args.no_render else
          f"; renders in {show(render_dir)}"))
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(main())
