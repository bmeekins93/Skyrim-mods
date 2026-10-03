"""Stage 1 - silhouette extraction: input photo -> work/outline.json (+ work/matte.png, previews).

Pipeline
1. Decode the 16-bit Display-P3 PNG to 8-bit sRGB (cfm.imaging.load_source_srgb).
2. Semantic face parsing (SegFormer "jonathandinu/face-parsing", flip TTA) -> per-group
   probabilities (face parts, glasses, hair, neck, cloth, background).
3. Foreground matte (BiRefNet-matting, flip TTA) on a crop around the head.
4. Per mode: semantic region (which parts belong to the cut-out) x matte (where the
   subject ends against the background); neck/clothing/other-mode parts are excluded
   so the lower boundary follows the jaw line. Neck/clothing only counts where it can
   physically be (connected to the bottom of the photo or below the eye line); the
   parser's "cloth" blobs on the crown are hair.
5. Scissor-cut clean-up (holes, islands, open/close, blur) and simplification to a
   smooth 120-400 point polygon with positive shoelace area in image coordinates.
6. Landmarks (iris circle fits, nose tip, mouth, chin, crown, cut top) + sanity checks.
7. Preview images in docs/renders/ (silhouette_<mode>.png, silhouette_modes.png).

Run from the repo root:  .venv/bin/python src/01_segment.py
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import cv2
import numpy as np
from scipy import ndimage

sys.path.insert(0, str(Path(__file__).resolve().parent))
from cfm.img_geometry import (components_by_area, convex_hull_mask, disk, mask_to_polygon,  # noqa: E402
                              point_in_polygon, polygon_report, ray_exit_point, smooth_cut_mask)
from cfm.img_landmarks import fit_iris_circle, iris_symmetry, luminance  # noqa: E402
from cfm.img_segment import (FACE_PARSING_REPO, FACE_PARSING_REV, MATTING_REPO,  # noqa: E402
                             MATTING_REV, face_parsing_probs, matting_alpha)
from cfm.imaging import load_source_srgb, save_png  # noqa: E402
from cfm.paths import ROOT, WORK, load_config  # noqa: E402

MODES = ("face_only", "face_hair", "face_hair_glasses")
RENDERS = ROOT / "docs" / "renders"

# Tuning (pixels refer to the 1320 px wide source; scaled by image width below).
REF_WIDTH = 1320.0
SNAP_RADIUS = 30      # how far the matte may extend a parsing region (parsing is ~10 px/cell)
PROB_SIGMA = 6.0      # blur of the parsing probabilities before the argmax
# Scissor-cut clean-up per mode: (open radius, close radius, blur sigma). Opening removes
# single strands, closing bridges notches a pair of scissors would not follow, the blur
# rounds off stair-steps. face_only cuts across the fringe, so it is smoothed harder.
CUT_SMOOTHING = {"face_only": (12, 45, 10.0), "face_hair": (4, 14, 5.0), "face_hair_glasses": (4, 14, 5.0)}
MATTE_THRESHOLD = 0.5
TARGET_POINTS = 260
IRIS_MAX_ASYMMETRY = 0.15   # |left/right iris-edge distance mismatch| / (2 * radius)
IRIS_EDGE_TOLERANCE = 0.2   # iris edge distances (left, right, down) within +-20 % of the radius


def _scale(img_w: int, px: float) -> int:
    return max(1, int(round(px * img_w / REF_WIDTH)))


# --- semantic masks -----------------------------------------------------------


def argmax_groups(groups: dict, sigma: float) -> dict:
    """Per-pixel winning group after a light blur (smooths the ~10 px parsing cells)."""
    names = list(groups)
    lab = np.argmax(np.stack([cv2.GaussianBlur(groups[n], (0, 0), sigma) for n in names]), axis=0)
    return {n: lab == i for i, n in enumerate(names)}


def drop_specks(mask: np.ndarray, rel: float = 0.01) -> np.ndarray:
    """Remove components smaller than rel * largest component."""
    comps = components_by_area(mask, 64)
    if not comps:
        return mask
    big = comps[0].sum()
    out = np.zeros_like(mask, dtype=bool)
    for c in comps:
        if c.sum() >= rel * big:
            out |= c
    return out


def eye_line_y(parts: dict) -> float:
    """Mean row of the two parsed eyes (pixel-centre coordinates)."""
    eyes = components_by_area(parts["eyes"] > 0.5, 2, min_area=20)
    if not eyes:
        raise RuntimeError("face parsing found no eyes")
    return float(np.mean([np.nonzero(e)[0].mean() for e in eyes])) + 0.5


def split_body(is_: dict, eye_y: float) -> tuple[np.ndarray, np.ndarray]:
    """Parsed neck/clothing -> (body, misplaced).

    Neck and clothes hang from the jaw down to the bottom of the photo. A neck/cloth
    component counts as body only if it touches the bottom border or lies wholly below the
    eye line, and even then only its part below the eye line. Everything else (the parser
    calls some dark crown hair tufts "cloth") is returned as `misplaced`; the caller treats
    it as hair, so it is still limited by the matte.
    """
    union = is_["neck"] | is_["cloth"]
    h = union.shape[0]
    n, lab, stats, _ = cv2.connectedComponentsWithStats(union.astype(np.uint8), connectivity=8)
    body = np.zeros(union.shape, bool)
    for i in range(1, n):
        top = stats[i, cv2.CC_STAT_TOP]
        if top + stats[i, cv2.CC_STAT_HEIGHT] >= h or top + 0.5 > eye_y:
            body |= lab == i
    body &= (np.arange(h) + 0.5 > eye_y)[:, None]
    return body, union & ~body


def semantic_mode_masks(is_: dict, eye_y: float) -> tuple[dict, dict, dict]:
    """Raw per-mode part masks, per mode the parts that must stay outside, and diagnostics."""
    face = drop_specks(is_["face"])
    hull = convex_hull_mask(face)
    glasses = drop_specks(is_["glasses"], 0.05)
    body, misplaced = split_body(is_, eye_y)
    hair_all = is_["hair"] | misplaced
    hair = drop_specks(hair_all, 0.02)
    face_only = face | (glasses & hull)
    inside = {
        "face_only": face_only,
        "face_hair": face_only | hair,
        "face_hair_glasses": face_only | hair | glasses,
    }
    outside = {
        "face_only": body | hair_all | (is_["glasses"] & ~hull),
        "face_hair": body | (is_["glasses"] & ~hull),
        "face_hair_glasses": body,
    }
    info = {"eye_line_y": round(eye_y, 2), "neck_cloth_relabelled_as_hair_px": int(misplaced.sum())}
    return inside, outside, info


def refine_with_matte(inside: np.ndarray, outside: np.ndarray, fg: np.ndarray, snap: int) -> np.ndarray:
    """Parsing decides *which* parts; the matte decides where they end against the background.

    The parsing region may grow by up to `snap` px into matte foreground that parsing
    called background (fine hair strands, ear rims), but only where that pixel is closer
    to an included part than to an excluded one (so face_only does not creep up the
    outside of the hair).
    """
    d_in = ndimage.distance_transform_edt(~inside)
    d_out = ndimage.distance_transform_edt(~outside) if outside.any() else np.full(inside.shape, np.inf)
    grown = inside | ((d_in <= snap) & (d_in < d_out))
    return grown & ~outside & fg


def cut_quality(poly: np.ndarray, fg: np.ndarray, raw: np.ndarray, eye_y: float, with_hair: bool) -> dict:
    """Evidence for the outline.

    background_inside_fraction: matte background inside the cut / cut area.
    raw_region_lost_fraction: part of the raw semantic+matte region the smoothed cut dropped.
    head_matte_outside_fraction (modes with hair): matte foreground above the eye line that
    lies outside the cut / cut area. Above the eyes the matte is only head and hair, so this
    does not depend on the semantic exclusions and catches hair the cut slices off.
    """
    filled = np.zeros(fg.shape, np.uint8)
    cv2.fillPoly(filled, [np.round((poly - 0.5) * 16).astype(np.int32)], 1, cv2.LINE_8, 4)
    inside = filled.astype(bool)
    q = {"background_inside_fraction": round(float(np.mean(~fg[inside])), 5),
         "raw_region_lost_fraction": round(float(np.sum(raw & ~inside) / max(1, raw.sum())), 5)}
    if with_hair:
        above = (np.arange(fg.shape[0]) + 0.5 < eye_y)[:, None]
        q["head_matte_outside_fraction"] = round(float(np.sum(fg & above & ~inside) / inside.sum()), 5)
    return q


# --- landmarks ------------------------------------------------------------------


def fit_irises(img: np.ndarray, parts: dict) -> list[dict]:
    """Iris circles of the parsed eyes, sorted left to right in the image."""
    lum = luminance(img)
    fits = []
    for eye in components_by_area(parts["eyes"] > 0.5, 2, min_area=20):
        cx, cy, r = fit_iris_circle(lum, eye)
        fits.append({"centre": np.array([cx, cy]), "radius_px": r,
                     "symmetry": iris_symmetry(lum, (cx, cy), r)})
    return sorted(fits, key=lambda f: f["centre"][0])


def compute_landmarks(irises: list[dict], parts: dict, poly: np.ndarray, head_poly: np.ndarray) -> dict:
    """Landmarks for the selected polygon `poly`; `head_poly` (face_hair) gives the crown."""
    nose = components_by_area(parts["nose"] > 0.5, 1)[0]
    lips = parts["lips"] > 0.5
    ys, xs = np.nonzero(nose)
    y_top, y_bot = ys.min(), ys.max()
    # Pronasale (nose tip) projects ~80% of the way down the frontal nose region,
    # just above the columella/nostrils.
    tip_y = y_top + 0.8 * (y_bot - y_top)
    band = np.abs(ys - tip_y) <= 6
    nose_tip = np.array([xs[band].mean() + 0.5, tip_y + 0.5])
    centres = [f["centre"] for f in irises]
    if len(centres) == 1:  # mirror the visible eye about the nose (fallback)
        e = centres[0]
        centres = sorted([e, np.array([2 * nose_tip[0] - e[0], e[1]])], key=lambda p: p[0])
    if len(centres) != 2:
        raise RuntimeError("face parsing found no eyes")
    ly, lx = np.nonzero(lips)
    mouth = np.array([lx.mean() + 0.5, ly.mean() + 0.5])
    eye_mid = 0.5 * (centres[0] + centres[1])
    chin = ray_exit_point(poly, mouth, mouth - eye_mid)  # face centre line, downwards
    return {
        "eye_image_left": centres[0], "eye_image_right": centres[1], "nose_tip": nose_tip,
        "mouth": mouth, "chin": chin,
        "crown": head_poly[int(np.argmin(head_poly[:, 1]))],  # top of the head incl. hair, any mode
        "cut_top": poly[int(np.argmin(poly[:, 1]))],          # top of the selected cut
    }


def check_landmarks(lm: dict, poly: np.ndarray, irises: list[dict]) -> list[str]:
    problems = []
    for k in ("eye_image_left", "eye_image_right", "nose_tip", "mouth"):
        if not point_in_polygon(poly, lm[k]):
            problems.append(f"{k} outside the selected polygon")
    eye_y = 0.5 * (lm["eye_image_left"][1] + lm["eye_image_right"][1])
    order = [lm["crown"][1], eye_y, lm["nose_tip"][1], lm["mouth"][1], lm["chin"][1]]
    if not all(a < b for a, b in zip(order, order[1:])):
        problems.append(f"vertical order crown<eyes<nose<mouth<chin violated: {order}")
    if not lm["crown"][1] <= lm["cut_top"][1] + 0.01 or not lm["cut_top"][1] < eye_y:
        problems.append("cut_top must lie between the crown and the eyes")
    if not lm["eye_image_left"][0] < lm["nose_tip"][0] < lm["eye_image_right"][0]:
        problems.append("nose tip is not between the eyes horizontally")
    for f in irises:  # the iris edge must be about one radius away on both sides and below
        sym, r = f["symmetry"], f["radius_px"]
        if abs(sym["asymmetry_over_radius"]) > IRIS_MAX_ASYMMETRY or not all(
                abs(sym[k] / r - 1.0) <= IRIS_EDGE_TOLERANCE for k in ("left_px", "right_px", "down_px")):
            problems.append(f"iris centre at {f['centre']} is not centred in the iris: {sym}")
    return problems


# --- previews --------------------------------------------------------------------

LM_COLOURS = {"eye_image_left": (0, 200, 255), "eye_image_right": (0, 200, 255), "nose_tip": (255, 160, 0),
              "mouth": (255, 60, 60), "chin": (60, 255, 60), "crown": (255, 255, 0), "cut_top": (255, 255, 255)}


def _view_box(polys: dict, w: int, h: int, margin: float = 0.06):
    allp = np.vstack(list(polys.values()))
    x0, y0 = allp.min(axis=0)
    x1, y1 = allp.max(axis=0)
    m = margin * max(x1 - x0, y1 - y0)
    return (int(max(0, x0 - m)), int(max(0, y0 - m)), int(min(w, x1 + m)), int(min(h, y1 + m)))


def draw_outline_preview(img, poly, lm, box, title, path, out_h=960):
    x0, y0, x1, y1 = box
    s = out_h / (y1 - y0)
    view = cv2.resize(img[y0:y1, x0:x1], (int(round((x1 - x0) * s)), out_h), interpolation=cv2.INTER_AREA)
    view = (view * 0.85).astype(np.uint8)  # darken slightly so the outline stands out
    shift = 4
    pts = np.round((poly - [x0, y0]) * s * (1 << shift)).astype(np.int32)
    cv2.polylines(view, [pts], True, (0, 0, 0), 5, cv2.LINE_AA, shift)
    cv2.polylines(view, [pts], True, (255, 0, 255), 2, cv2.LINE_AA, shift)
    for p in pts:
        cv2.circle(view, tuple(int(v) for v in p), 2 << shift, (255, 255, 255), -1, cv2.LINE_AA, shift)
    for k, p in lm.items():
        q = tuple(int(v) for v in np.round((np.asarray(p) - [x0, y0]) * s))
        col = LM_COLOURS[k]
        cv2.drawMarker(view, q, (0, 0, 0), cv2.MARKER_CROSS, 26, 5, cv2.LINE_AA)
        cv2.drawMarker(view, q, col, cv2.MARKER_CROSS, 24, 2, cv2.LINE_AA)
        cv2.putText(view, k, (q[0] + 10, q[1] - 8), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 0, 0), 3, cv2.LINE_AA)
        cv2.putText(view, k, (q[0] + 10, q[1] - 8), cv2.FONT_HERSHEY_SIMPLEX, 0.5, col, 1, cv2.LINE_AA)
    cv2.putText(view, title, (12, 32), cv2.FONT_HERSHEY_SIMPLEX, 0.9, (0, 0, 0), 4, cv2.LINE_AA)
    cv2.putText(view, title, (12, 32), cv2.FONT_HERSHEY_SIMPLEX, 0.9, (255, 255, 255), 2, cv2.LINE_AA)
    save_png(path, view)


def cutout_tile(img, poly, box, out_h, label):
    """Cut-out on a neutral table colour with a cardboard rim and soft drop shadow."""
    x0, y0, x1, y1 = box
    s = out_h / (y1 - y0)
    w = int(round((x1 - x0) * s))
    photo = cv2.resize(img[y0:y1, x0:x1], (w, out_h), interpolation=cv2.INTER_AREA).astype(np.float32)
    ss = 4  # supersampled coverage for anti-aliased edges
    cov = np.zeros((out_h * ss, w * ss), np.uint8)
    cv2.fillPoly(cov, [np.round((poly - [x0, y0]) * s * ss).astype(np.int32)], 255, cv2.LINE_AA)
    cov = cv2.resize(cov, (w, out_h), interpolation=cv2.INTER_AREA).astype(np.float32)[..., None] / 255
    rim = cv2.dilate(cov, disk(3))[..., None]
    shadow = cv2.GaussianBlur(np.roll(np.roll(rim, 10, 0), 8, 1), (0, 0), 7)[..., None]
    tile = np.ones((out_h, w, 3), np.float32) * np.array([226, 222, 214], np.float32)
    tile *= 1 - 0.45 * shadow
    tile = tile * (1 - rim) + rim * np.array([168, 128, 82], np.float32)  # cut cardboard edge
    tile = tile * (1 - cov) + cov * photo
    tile = tile.astype(np.uint8)
    cv2.putText(tile, label, (10, out_h - 14), cv2.FONT_HERSHEY_SIMPLEX, 0.75, (40, 40, 40), 2, cv2.LINE_AA)
    return tile


# --- main ---------------------------------------------------------------------------


def head_box(is_: dict, w: int, h: int, margin: float = 0.15):
    head = is_["face"] | is_["hair"] | is_["glasses"]
    ys, xs = np.nonzero(head)
    mx = margin * (xs.max() - xs.min())
    my = margin * (ys.max() - ys.min())
    return (int(max(0, xs.min() - mx)), int(max(0, ys.min() - my)),
            int(min(w, xs.max() + mx + 1)), int(min(h, ys.max() + my + 1)))


def _r2(p) -> list:
    return [round(float(p[0]), 2), round(float(p[1]), 2)]


def run_models(img: np.ndarray, cache: Path | None):
    """Face parsing + matting. `cache` (developer option) stores/reuses the raw model outputs."""
    if cache and cache.exists():
        z = np.load(cache, allow_pickle=True)
        print(f"using cached model outputs from {cache}")
        return z["parsing"].item(), z["alpha"]
    t0 = time.time()
    h, w = img.shape[:2]
    parsing = face_parsing_probs(img)
    box = head_box(argmax_groups(parsing["groups"], PROB_SIGMA * w / REF_WIDTH), w, h)
    print(f"face parsing done ({time.time() - t0:.1f}s); matting crop {box}")
    alpha = matting_alpha(img, box)
    print(f"matting done ({time.time() - t0:.1f}s)")
    if cache:
        cache.parent.mkdir(parents=True, exist_ok=True)
        np.savez(cache, parsing=np.array(parsing, dtype=object), alpha=alpha)
    return parsing, alpha


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--cache", type=Path, default=None,
                    help="developer option: .npz file to store/reuse model outputs (not used by the build)")
    cache = ap.parse_args().cache
    t0 = time.time()
    cfg = load_config()
    src_rel = cfg["source_image"]
    selected = cfg.get("silhouette_mode", "face_hair_glasses")
    if selected not in MODES:
        raise SystemExit(f"config silhouette_mode must be one of {MODES}")
    img, decode = load_source_srgb(ROOT / src_rel)
    h, w = img.shape[:2]
    print(f"decoded {src_rel}: {w}x{h}, {decode}")

    parsing, alpha = run_models(img, cache)
    is_ = argmax_groups(parsing["groups"], PROB_SIGMA * w / REF_WIDTH)
    fg = alpha > MATTE_THRESHOLD

    eye_y = eye_line_y(parsing["parts"])
    inside, outside, seg_info = semantic_mode_masks(is_, eye_y)
    print(f"  eye line y={eye_y:.1f}; neck/cloth pixels above it treated as hair: "
          f"{seg_info['neck_cloth_relabelled_as_hair_px']}")
    polys, reports = {}, {}
    for mode in MODES:
        raw = refine_with_matte(inside[mode], outside[mode], fg, _scale(w, SNAP_RADIUS))
        open_r, close_r, sigma = CUT_SMOOTHING[mode]
        cut = smooth_cut_mask(raw, _scale(w, open_r), _scale(w, close_r), sigma * w / REF_WIDTH)
        poly = mask_to_polygon(cut, target_points=TARGET_POINTS)
        poly[:, 0] = np.clip(poly[:, 0], 0.0, float(w))
        poly[:, 1] = np.clip(poly[:, 1], 0.0, float(h))
        polys[mode], reports[mode] = poly, polygon_report(poly)
        reports[mode].update(cut_quality(poly, fg, raw, eye_y, with_hair=(mode != "face_only")))
        print(f"  {mode}: {reports[mode]}")
        if not (reports[mode]["valid"] and reports[mode]["simple"] and reports[mode]["signed_area_px"] > 0):
            raise SystemExit(f"invalid polygon for {mode}")

    irises = fit_irises(img, parsing["parts"])
    lm = compute_landmarks(irises, parsing["parts"], polys[selected], polys["face_hair"])
    problems = check_landmarks(lm, polys[selected], irises)
    for p in problems:
        print("LANDMARK PROBLEM:", p)
    if problems:
        return 1

    WORK.mkdir(parents=True, exist_ok=True)
    save_png(WORK / "matte.png", np.clip(np.rint(alpha * 255), 0, 255).astype(np.uint8))
    out = {
        "source": src_rel,
        "image_size": [w, h],
        "units": "source-image pixels, origin top-left, y down",
        "coordinate_convention": "continuous: image covers [0,W]x[0,H]; pixel (i,j) centre is (i+0.5, j+0.5)",
        "orientation": "positive shoelace area on raw (x, y) image coordinates (appears clockwise on screen)",
        "selected_mode": selected,
        "modes": {m: {"polygon": [_r2(p) for p in polys[m]],
                      "bbox": reports[m]["bbox"],
                      "crown": _r2(polys[m][int(np.argmin(polys[m][:, 1]))]),
                      "stats": reports[m]} for m in MODES},
        "landmarks": {k: _r2(v) for k, v in lm.items()},
        "iris_fit": {k: {"centre": _r2(f["centre"]), "radius_px": f["radius_px"], "symmetry": f["symmetry"]}
                     for k, f in zip(("eye_image_left", "eye_image_right"), irises)},
        "segmentation": seg_info,
        "matte": "work/matte.png",
        "decode": decode,
        "models": {"face_parsing": f"{FACE_PARSING_REPO}@{FACE_PARSING_REV}",
                   "matting": f"{MATTING_REPO}@{MATTING_REV}"},
    }
    with open(WORK / "outline.json", "w", encoding="utf-8") as f:
        json.dump(out, f, indent=1)

    vbox = _view_box(polys, w, h)
    for m in MODES:
        mlm = dict(lm)
        mlm["cut_top"] = polys[m][int(np.argmin(polys[m][:, 1]))]
        if np.linalg.norm(mlm["cut_top"] - lm["crown"]) < 3.0:  # same point: one label is enough
            del mlm["cut_top"]
        title = f"{m} ({len(polys[m])} pts){'  [selected]' if m == selected else ''}"
        draw_outline_preview(img, polys[m], mlm, vbox, title, RENDERS / f"silhouette_{m}.png")
    tiles = [cutout_tile(img, polys[m], vbox, 760, m + (" *" if m == selected else "")) for m in MODES]
    gap = np.full((760, 16, 3), 226, np.uint8)
    save_png(RENDERS / "silhouette_modes.png", np.concatenate(sum([[t, gap] for t in tiles], [])[:-1], axis=1))
    print(f"wrote work/outline.json, work/matte.png, docs/renders/silhouette_*.png ({time.time() - t0:.1f}s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
