"""Render NIF files on a vanilla head with headless Blender and save a labelled contact sheet.

Examples (run from the repo root):
    .venv/bin/python src/render_check.py --nif work/nif/mask_human_m.nif --head human \\
        --out docs/renders/render_human_m.png [--pose 35,15,0] [--checker] [--views front,left]
    .venv/bin/python src/render_check.py --nif work/nif/mask_human_m.nif --head human \\
        --extra-nif third_party/PyNifly/tests/tests/SkyrimSE/maleorchair27.nif \\
        --out docs/renders/render_human_m_hair.png
    .venv/bin/python src/render_check.py --nif third_party/PyNifly/tests/tests/SkyrimSE/helmet-SMP.nif \\
        --head human --out docs/renders/render_vanilla_helmet.png

Blender is third_party/blender/blender or $BLENDER. The scene itself is built by
src/cfm/blender_render.py (see its docstring for the transform math); this wrapper runs it,
then lays the per-view PNGs out in a grid with a header and a report panel.

The report starts with the list of problems (red: verts inside the head, weights that do not
sum to 1 or are not on the head, inconsistent winding, degenerate triangles, unskinned worn
shapes, shader state the render cannot show); amber lines are non-default shader state the
render reproduces. The panel grows into extra cells rather than dropping lines (an explicit
"+N more lines" note if it still overflows), the full text is printed to stdout with red
lines marked "!!", and --strict exits 2 when there is any problem.
Relative --nif paths are taken from the current directory first, then from the repo root.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "src" / "cfm" / "blender_render.py"
FONT_FILES = ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
              "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf")

BG = (236, 236, 232)
INK = (30, 30, 30)
MUTED = (95, 95, 95)
BAD = (170, 30, 20)
NOTE = (150, 95, 0)
GOOD = (30, 110, 45)
# report line level -> colour; "bad" lines are problems (listed first, fail --strict),
# "note" lines are non-default NIF state that the render reproduces.
LEVEL_COLOUR = {"title": INK, "ok": MUTED, "note": NOTE, "bad": BAD, "pass": GOOD}
WEIGHT_TOL = 1e-3          # same tolerance as cfm/blender_render.py
MAX_PANEL_CELLS = 4        # report cells after the views before lines are cut


def find_blender() -> Path:
    env = os.environ.get("BLENDER")
    for cand in ([Path(env)] if env else []) + [ROOT / "third_party" / "blender" / "blender"]:
        if cand.exists():
            return cand
    found = shutil.which("blender")
    if found:
        return Path(found)
    raise SystemExit("Blender not found: set $BLENDER or run tools/setup.sh")


def font(size: int, bold: bool = False):
    path = FONT_FILES[1 if bold else 0]
    try:
        return ImageFont.truetype(path, size)
    except OSError:
        return ImageFont.load_default(size=size)


def run_blender(blender: Path, args, view_dir: Path) -> dict:
    """Run blender_render.py; return its report.json (raise with the log tail on failure).
    All paths handed to Blender are absolute (it runs with cwd = repo root)."""
    cmd = [str(blender), "-b", "--factory-startup", "-noaudio", "--python-exit-code", "1",
           "-P", str(SCRIPT), "--", "--out-dir", str(view_dir), "--head", args.head,
           f"--pose={args.pose}", "--views", args.views, "--res", str(args.res),
           "--samples", str(args.samples), "--skeleton", args.skeleton,
           "--exposure", str(args.exposure)]
    for nif in args.nif:
        cmd += ["--nif", str(nif)]
    for nif in args.extra_nif:
        cmd += ["--extra-nif", str(nif)]
    if args.checker:
        cmd.append("--checker")
    if args.no_cull:
        cmd.append("--no-cull")
    if args.prefer_dds:
        cmd.append("--prefer-dds")
    proc = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)
    (view_dir / "blender.log").write_text(proc.stdout + proc.stderr, encoding="utf-8")
    report = view_dir / "report.json"
    if proc.returncode != 0 or not report.exists():
        tail = "\n".join((proc.stdout + proc.stderr).splitlines()[-40:])
        raise SystemExit(f"Blender render failed (exit {proc.returncode}):\n{tail}")
    return json.loads(report.read_text(encoding="utf-8"))


def _fmt(x: float) -> str:
    return f"{x:+.2f}" if abs(x) >= 0.005 else "0.00"


def _short(desc: str) -> str:
    """File name for texture paths, the full text for descriptions such as 'neutral grey (...)'."""
    desc = str(desc)
    return Path(desc).name if desc.lower().endswith((".png", ".dds")) else desc


def title_lines(rep: dict) -> list:
    nifs = ", ".join(Path(n).name for n in rep["nifs"])
    if rep.get("extra_nifs"):
        nifs += " + " + ", ".join(Path(n).name for n in rep["extra_nifs"]) + " (overlay)"
    if not nifs:
        head = f"{Path(rep['head_nif']).name} ({rep['head']} head only)"
    else:
        head = f"on {Path(rep['head_nif']).name} ({rep['head']})" if rep["head_nif"] else "(no head)"
    pose = rep["pose_deg"]
    extras = []
    if any(pose.values()):
        extras.append(f"head pose yaw {pose['yaw']:g}° pitch {pose['pitch']:g}° roll {pose['roll']:g}°")
    else:
        extras.append("bind pose")
    if rep["checker"]:
        extras.append("UV checker")
    shas = [f"{Path(n).name} sha1 {h[:10]}" for n, h in rep.get("nif_sha1", {}).items()]
    return [f"{nifs}  {head}".strip(), " · ".join(extras + shas) +
            f" · Cycles {rep['samples']} spp · Blender {rep['blender']}"]


def _near_one(rng) -> bool:
    return abs(rng[0] - 1) <= WEIGHT_TOL and abs(rng[1] - 1) <= WEIGHT_TOL


def _skin_lines(m: dict) -> list:
    """(text, level) lines about the skin of a mesh under test."""
    if not m.get("skinned"):
        chain = " > ".join(m.get("node_chain", [])) or "?"
        ground = Path(m["source"]).stem.lower().endswith("_go")
        return [(f"  not skinned: placed by node transforms {chain}"
                 + (" (ground model)" if ground else "; worn meshes must be skinned to the head"),
                 "ok" if ground else "bad")]
    out = [(f"  bones: {', '.join(m['bones'])}", "ok")]
    ws, share = m["weight_sum_range"], m["head_share_range"]
    ok = _near_one(ws) and _near_one(share)
    out.append((f"  weights (raw, as skinned): sum {ws[0]:.3f}..{ws[1]:.3f}, "
                f"head-driven share {share[0]:.3f}..{share[1]:.3f}", "ok" if ok else "bad"))
    if m.get("unweighted_verts"):
        out.append((f"  {m['unweighted_verts']} verts have no weight (drawn via inverse global-to-skin)", "bad"))
    dev = m["inverse_global_to_skin_max_dev"]
    out.append((f"  inv(global-to-skin) vs bones: {dev:.4f} u", "ok" if dev < 0.05 else "bad"))
    return out


def _shader_lines(m: dict) -> list:
    st = m.get("shader") or {}
    out = []
    if st.get("applied"):
        out.append((f"  shader state shown: {', '.join(st['applied'])}", "note"))
    if st.get("not_emulated"):
        out.append((f"  NOT shown by this render: {', '.join(st['not_emulated'])}", "bad"))
    if st and not st.get("applied") and not st.get("not_emulated"):
        out.append(("  shader: no alpha, vertex colours, UV transform or emission", "ok"))
    if st:
        out.append((f"  gloss {st['glossiness']:g}, spec {st['spec_strength']:.2f} (approximated); "
                    f"{'stored tangents ignored (Blender MikkTSpace)' if st['tangents_stored'] else 'no stored tangents'}",
                    "ok"))
    return out


def mesh_lines(m: dict) -> list:
    """(text, level) lines for one mesh under test."""
    out = [(f"{m['name']}  ({Path(m['source']).name}, {m['block']})", "title")]
    wa, dropped = m.get("winding_agreement"), m.get("blender_dropped_tris", 0)
    wtxt = f"winding vs normals {wa:.1%}" if wa is not None else "no vertex normals stored"
    out.append((f"  {m['verts']} verts, {m['tris']} tris, {wtxt}"
                + (f", {dropped} degenerate tris" if dropped else ""),
                "ok" if (wa is None or wa > 0.99) and not dropped else "bad"))
    out += _skin_lines(m)
    mat = m.get("material", {})
    out.append((f"  diffuse: {_short(mat.get('diffuse', '?'))}", "ok"))
    out.append((f"  normal map: {_short(mat.get('normal', '?'))}", "ok"))
    out.append((f"  back faces: {mat.get('backfaces', '?')}", "ok"))
    return out + _shader_lines(m)


def head_lines(m: dict) -> list:
    """(text, level) lines for the head overlay or an --extra-nif overlay (drawn, never checked)."""
    moving = [b for b, v in m.get("bones", {}).items() if v["head_driven"]]
    still = [b for b, v in m.get("bones", {}).items() if not v["head_driven"]]
    what = "Head overlay" if m["role"] == "head" else "Extra overlay (matte, not checked)"
    out = [(f"{what} {m['name']} ({Path(m['source']).name}), {m['verts']} verts", "title"),
           (f"  follows head pose: {', '.join(moving) or '-'}", "ok"),
           (f"  stays put: {', '.join(still) or '-'}", "ok")]
    pd = m.get("pose_displacement")
    if pd:
        leak = pd["max_where_head_weight_0"] or 0.0
        out.append((f"  pose: max move {pd['max']:.2f} u; verts with head weight 0 "
                    f"({pd['verts_head_weight_0']}) moved {leak:.4f} u", "ok" if leak < 1e-4 else "bad"))
    return out


def clearance_lines(rep: dict) -> list:
    c = rep.get("clearance_to_head") or {}
    if not c:
        return []
    out = [("Clearance to head surface (units)", "title"),
           (f"  min {_fmt(c['min'])}, 5% {_fmt(c['p05'])}, median {_fmt(c['median'])}", "ok")]
    for name, sc in c.get("per_shape", {}).items():
        where = ""
        if sc.get("inside_bbox"):
            lo, hi = sc["inside_bbox"]
            where = f" at z {lo[2]:.1f}..{hi[2]:.1f}, y {lo[1]:.1f}..{hi[1]:.1f}"
        out.append((f"  {name}: min {_fmt(sc['min'])}, verts inside head "
                    f"{sc['verts_inside_head']}/{sc['verts_total']}{where}",
                    "ok" if sc["verts_inside_head"] == 0 else "bad"))
    return out


def problems(rep: dict) -> list:
    """Every red line, prefixed with the mesh it belongs to."""
    out = []
    for m in rep["meshes"]:
        lines = mesh_lines(m) if m["role"] == "nif" else head_lines(m)
        out += [f"{m['name']}: {t.strip()}" for t, lvl in lines if lvl == "bad"]
    out += [f"clearance: {t.strip()}" for t, lvl in clearance_lines(rep) if lvl == "bad"]
    return out


def report_lines(rep: dict) -> list:
    """(text, level) lines for the panel: problems first, then clearance, every mesh under
    test, and the head overlay."""
    bad = problems(rep)
    out = [("Checks: all passed", "pass")] if not bad else \
        [(f"Checks: {len(bad)} problem{'s' if len(bad) > 1 else ''}", "bad")] + [(f"  {b}", "bad") for b in bad]
    out += clearance_lines(rep)
    for m in rep["meshes"]:
        if m["role"] == "nif":
            out += mesh_lines(m)
    for m in rep["meshes"]:
        if m["role"] != "nif":
            out += head_lines(m)
    return out


def wrap(draw, text, fnt, width) -> list:
    """Split text into lines no wider than `width` pixels, keeping its leading indent
    (continuation lines are indented a little further)."""
    body = text.lstrip(" ")
    indent = " " * (len(text) - len(body))
    lines, line = [], indent
    for word in body.split(" "):
        trial = (line + " " + word) if line.strip() else line + word
        if draw.textlength(trial, font=fnt) > width and line.strip():
            lines.append(line)
            line = indent + "    " + word
        else:
            line = trial
    return lines + [line]


def panel_layout(draw, rep, tile, scale, budgets):
    """Largest font whose wrapped report fits the smallest cell budget that can hold it;
    when nothing fits, the smallest font in the largest budget with the overflow cut and
    announced. Returns (font, [(text, colour)], lines per cell, cells used)."""
    lines = report_lines(rep)
    for budget in budgets:
        for size in (15, 13, 11):
            fnt = font(max(9, int(size * scale)))
            rows = [(t, LEVEL_COLOUR[lvl]) for text, lvl in lines for t in wrap(draw, text, fnt, tile - 20)]
            per_cell = max(1, (tile - 16) // (fnt.size + 4))
            cells = -(-len(rows) // per_cell)
            if cells <= budget:
                return fnt, rows, per_cell, cells
    room = budgets[-1] * per_cell
    note = wrap(draw, f"… +{len(rows)} more lines (full report on stdout)", fnt, tile - 20)
    keep = max(0, room - len(note))
    note = wrap(draw, f"… +{len(rows) - keep} more lines (full report on stdout)", fnt, tile - 20)
    rows = rows[:keep] + [(t, BAD) for t in note]
    return fnt, rows, per_cell, budgets[-1]


def _grid_cols(cells: int, columns: int) -> int:
    return cells if cells <= columns + 1 else columns          # one row when it fits


def contact_sheet(rep: dict, view_dir: Path, out: Path, columns: int) -> None:
    views = rep["views"]
    tile = Image.open(view_dir / views[0]["file"]).size[0]
    scale = tile / 640
    cols = _grid_cols(len(views) + 1, columns)
    free = -(-(len(views) + 1) // cols) * cols - len(views)    # panel cells without a new row
    budgets = sorted({free} | {min(free + k * cols, max(free, MAX_PANEL_CELLS)) for k in (1, 2, 3)})
    probe = ImageDraw.Draw(Image.new("RGB", (8, 8)))
    fnt, rows, per_cell, n_panel = panel_layout(probe, rep, tile, scale, budgets)
    cells = len(views) + n_panel
    cols = _grid_cols(cells, columns)
    rows_n = -(-cells // cols)
    pad, label_h = max(6, tile // 64), max(22, tile // 18)
    head_h = int(84 * scale) + 10
    w = cols * tile + (cols + 1) * pad
    h = head_h + rows_n * (tile + label_h) + (rows_n + 1) * pad
    sheet = Image.new("RGB", (w, h), BG)
    draw = ImageDraw.Draw(sheet)
    t1, t2 = title_lines(rep)
    draw.text((pad * 2, int(12 * scale)), t1, font=font(int(30 * scale), True), fill=INK)
    draw.text((pad * 2, int(52 * scale)), t2, font=font(int(19 * scale)), fill=MUTED)
    lab = font(max(12, int(label_h * 0.62)), True)
    for i in range(cells):
        r, c = divmod(i, cols)
        x = pad + c * (tile + pad)
        y = head_h + pad + r * (tile + label_h + pad)
        if i < len(views):
            draw.text((x + 4, y + (label_h - lab.size) // 2), views[i]["label"], font=lab, fill=INK)
            sheet.paste(Image.open(view_dir / views[i]["file"]).convert("RGB"), (x, y + label_h))
        else:
            k = i - len(views)
            title = "Report" if n_panel == 1 else f"Report {k + 1}/{n_panel}"
            draw.text((x + 4, y + (label_h - lab.size) // 2), title, font=lab, fill=INK)
            panel_cell(draw, rows[k * per_cell:(k + 1) * per_cell], (x, y + label_h), tile, fnt)
    out.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(out, optimize=True)


def panel_cell(draw, rows, xy, tile, fnt) -> None:
    x, y = xy
    draw.rectangle([x, y, x + tile - 1, y + tile - 1], fill=(248, 248, 245))
    yy = y + 8
    for text, colour in rows:
        draw.text((x + 10, yy), text, font=fnt, fill=colour)
        yy += fnt.size + 4


def parse_args(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--nif", action="append", default=[], help="NIF to show (repeatable)")
    ap.add_argument("--extra-nif", action="append", default=[],
                    help="overlay NIF such as a hair or hood: drawn matte dark brown and posed with the "
                         "head, but not checked (repeatable)")
    ap.add_argument("--head", choices=["human", "beast", "female", "none"], default="human",
                    help="head overlay: vanilla malehead.nif / maleheadargonian.nif, "
                         "PyNifly's CitrusFemHead.nif test head, or none")
    ap.add_argument("--out", required=True, help="contact sheet PNG")
    ap.add_argument("--pose", default="0,0,0",
                    help="head bone yaw,pitch,roll in degrees (+yaw: face turns to the actor's "
                         "left, +pitch: face tilts up, +roll: crown tips to the actor's right); "
                         "write negative values as --pose=-20,0,0")
    ap.add_argument("--checker", action="store_true", help="UV checker texture on the NIF shapes")
    ap.add_argument("--no-cull", action="store_true", help="also draw back faces of single-sided shaders")
    ap.add_argument("--prefer-dds", action="store_true",
                    help="texture from work/dds (what ships) before work/textures PNGs")
    ap.add_argument("--views", default="front,front34,left,back34,back,top,closeup",
                    help="comma list from: front, front34, left, back34, back, top, closeup, "
                         "right, front34r, back34l")
    ap.add_argument("--res", type=int, default=640, help="pixels per view (square)")
    ap.add_argument("--samples", type=int, default=64, help="Cycles samples per pixel")
    ap.add_argument("--skeleton", default="auto",
                    choices=["auto", "human", "female", "beast", "beast_female"])
    ap.add_argument("--exposure", type=float, default=1.0, help="light power multiplier")
    ap.add_argument("--columns", type=int, default=4)
    ap.add_argument("--keep-views", default=None, help="also keep per-view PNGs + report here")
    ap.add_argument("--strict", action="store_true",
                    help="exit 2 when the report has problems (red lines: verts inside the head, "
                         "weights not summing to 1 or not on the head, inconsistent winding, "
                         "degenerate tris, unskinned worn shapes, shader state the render can't show)")
    return ap.parse_args(argv)


def resolve_nif(path: str) -> Path:
    """Absolute NIF path: relative to the caller's directory first, then to the repo root."""
    p = Path(path).expanduser()
    for cand in (p, ROOT / p) if not p.is_absolute() else (p,):
        if cand.exists():
            return cand.resolve()
    raise SystemExit(f"missing NIF: {path}")


def main(argv=None) -> int:
    args = parse_args(argv)
    if not args.nif and args.head == "none":
        raise SystemExit("nothing to render: give --nif and/or --head")
    args.nif = [str(resolve_nif(n)) for n in args.nif]      # Blender runs with cwd = repo root
    args.extra_nif = [str(resolve_nif(n)) for n in args.extra_nif]
    out = Path(args.out).expanduser().resolve()
    blender = find_blender()
    with tempfile.TemporaryDirectory(prefix="cfm_render_") as tmp:
        view_dir = Path(args.keep_views).expanduser().resolve() if args.keep_views else Path(tmp)
        view_dir.mkdir(parents=True, exist_ok=True)
        rep = run_blender(blender, args, view_dir)
        contact_sheet(rep, view_dir, out, args.columns)
    secs = sum(v["seconds"] for v in rep["views"])
    print(f"wrote {out} ({len(rep['views'])} views, {secs:.0f} s render)")
    for text, level in report_lines(rep):          # the full panel text, never cut
        print(("!! " if level == "bad" else "   ") + text)
    found = problems(rep)
    if args.strict and found:
        print(f"--strict: {len(found)} problem(s)", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
