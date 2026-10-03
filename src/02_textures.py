"""Stage 2 - textures: work/outline.json + photo -> work/textures/*.png + work/atlas_layout.json.

Atlas (texture_size^2, u right, v down):
  front  photo print of the head crop (largest region, aspect preserved, no rotation)
  back   kraft liner (same image box as the front, mirrored in u on the mesh)
  edge   corrugated cross-section strip, tiles along u, v0 = front face side
  tape   beige masking tape with torn ends on a kraft surround
Twine is a separate tileable texture.

Normal maps: tangent space, DirectX convention (R = +u, G = +v = down the texture,
B = out of the surface); alpha = specular mask. Every region is padded by PAD texels of
edge-extended (edge strip: wrapped) colour so mips do not bleed between regions.

Run from the repo root:  .venv/bin/python src/02_textures.py
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import cv2
import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent))
from cfm.img_textures import (SPEC, corrugated_edge, encode_normal, estimate_foreground,  # noqa: E402
                              height_to_normal, kraft_back, masking_tape, paper_height, print_look,
                              pull_push, to_srgb_u8, twine)
from cfm.imaging import linear_to_srgb, load_source_linear, save_png, srgb_to_linear  # noqa: E402
from cfm.paths import ROOT, WORK, load_config  # noqa: E402

SEED = 20261003
PAD = 16                      # texels of edge-extended padding around every region
CROP_MARGIN = 0.03            # front crop = union bbox of all modes + 3 % of its larger side
EDGE_TILE_UNITS = 6.0         # perimeter length covered by one repeat of the edge strip
FLUTE_PITCH_UNITS = 0.55      # C-flute pitch (~7.9 mm) in Skyrim units (1 unit = 1.42875 cm)
EDGE_HEIGHT = 80              # texels across the cardboard thickness (at 2048)
TAPE_ASPECT = 2.8             # tape region width / height (a ~7 x 2.5 cm strip incl. torn ends)
TWINE_TILE_UNITS = 0.6        # string length per twine texture repeat (one full twist of the cord)
TWINE_TWISTS = 3              # ply stripes passed per tile; multiple of plies so it tiles (~32 deg helix at r=0.06)
DESPILL_BAND = 24            # source px inside the cut where green spill is clamped
HEAD_HEIGHT_UNITS = 15.0      # approx. chin-to-crown of a vanilla head (malehead.nif z-span ~22.5 incl. neck)
RENDERS = ROOT / "docs" / "renders"
OUT = WORK / "textures"


# --- layout -----------------------------------------------------------------------------


def crop_box(outline: dict) -> tuple[float, float, float, float]:
    pts = np.vstack([np.array(m["polygon"]) for m in outline["modes"].values()])
    x0, y0 = pts.min(axis=0)
    x1, y1 = pts.max(axis=0)
    m = CROP_MARGIN * max(x1 - x0, y1 - y0)
    return x0 - m, y0 - m, x1 + m, y1 + m


def plan_layout(size: int, aspect: float) -> dict:
    """Integer texel rects (x0, y0, x1, y1). Gaps between rects are >= 2*PAD."""
    s = size / 2048.0
    fh = size - 2 * PAD
    fw = int(round(fh * aspect))
    col_x0 = PAD + fw + 2 * PAD
    cw = size - PAD - col_x0
    if cw < 0.2 * size:
        raise SystemExit("front crop is too wide for the atlas layout")
    bh = int(round(cw / aspect))
    eh = int(round(EDGE_HEIGHT * s))
    th = int(round(cw / TAPE_ASPECT))
    rects = {"front": (PAD, PAD, PAD + fw, PAD + fh)}
    y = PAD
    for name, hgt in (("back", bh), ("edge", eh), ("tape", th)):
        rects[name] = (col_x0, y, col_x0 + cw, y + hgt)
        y += hgt + 2 * PAD
    if y - PAD > size:
        raise SystemExit("atlas column overflow")
    return rects


def exact_image_rect(crop, rect) -> tuple[list[float], float]:
    """Image box with exactly the texel rect's aspect (centred on the crop); returns (box, texels/px)."""
    x0, y0, x1, y1 = crop
    rw, rh = rect[2] - rect[0], rect[3] - rect[1]
    scale = rh / (y1 - y0)
    cx = 0.5 * (x0 + x1)
    half_w = 0.5 * rw / scale
    return [cx - half_w, y0, cx + half_w, y1], scale


# --- front ------------------------------------------------------------------------------


def warp_to_rect(img: np.ndarray, image_rect, scale: float, wh, interp, border) -> np.ndarray:
    """Resample the image box into a (w, h) texel grid; texel (i, j) centre <- image point
    (x0 + (i + .5)/scale, y0 + (j + .5)/scale) in continuous coordinates (pixel centres at +.5)."""
    x0, y0 = image_rect[0], image_rect[1]
    m = np.array([[1 / scale, 0, x0 + 0.5 / scale - 0.5], [0, 1 / scale, y0 + 0.5 / scale - 0.5]])
    return cv2.warpAffine(img.astype(np.float32), m, wh, flags=interp | cv2.WARP_INVERSE_MAP,
                          borderMode=border, borderValue=0)


def polygon_coverage(poly: np.ndarray, image_rect, scale: float, wh, ss: int = 4) -> np.ndarray:
    """Anti-aliased polygon coverage in texel space (supersampled ss x ss)."""
    w, h = wh
    t = (poly - [image_rect[0], image_rect[1]]) * scale * ss - 0.5  # cv2 integer coords = pixel centres
    canvas = np.zeros((h * ss, w * ss), np.uint8)
    cv2.fillPoly(canvas, [np.round(t * 16).astype(np.int32)], 255, cv2.LINE_8, 4)
    return cv2.resize(canvas.astype(np.float32) / 255.0, wh, interpolation=cv2.INTER_AREA)


def build_front(img_lin, matte, poly, image_rect, scale, wh, rng, ridge_px):
    """Photo print with background replaced by bled subject colours outside the cut."""
    lin = np.clip(warp_to_rect(img_lin, image_rect, scale, wh, cv2.INTER_LANCZOS4, cv2.BORDER_REPLICATE), 0, 1)
    alpha = np.clip(warp_to_rect(matte, image_rect, scale, wh, cv2.INTER_LINEAR, cv2.BORDER_CONSTANT), 0, 1)
    cov = polygon_coverage(poly, image_rect, scale, wh)
    srgb = linear_to_srgb(lin).astype(np.float32)
    # 1) decontaminate soft (hair) edges: foreground colour estimate from the matte
    fg = estimate_foreground(srgb, alpha, r1=max(3, int(90 * scale)), r2=max(3, int(6 * scale)))
    # 2) bleed: smooth extension of confidently-subject colours from inside the cut
    inner = cv2.erode((cov > 0.999).astype(np.uint8), cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5)))
    conf = inner.astype(np.float32) * np.clip((alpha - 0.85) / 0.1, 0, 1)
    fill = pull_push(srgb_to_linear(fg).astype(np.float32), conf)
    fill = linear_to_srgb(fill).astype(np.float32)
    a = alpha[..., None]
    inside = a * fg + (1 - a) * fill
    c = cov[..., None]
    colour = despill(c * inside + (1 - c) * fill, cov, band=DESPILL_BAND * scale)
    printed = print_look(colour, rng)
    height = paper_height(wh[::-1], rng, ridge_px)
    normal = height_to_normal(height, 0.06)
    spec = SPEC["front"] + 0.03 * np.clip(rng.standard_normal(wh[::-1]) * 0.3, -1, 1)
    return printed, colour, encode_normal(normal, spec), cov


def despill(srgb: np.ndarray, cov: np.ndarray, band: float) -> np.ndarray:
    """Green-spill suppression near the cut (keying 'average' limiter: G <= (R + B) / 2, plus
    desaturation of the pixels that had spill).

    Translucent hair tips can keep a little foliage colour even after foreground
    estimation. Skin (R > G > B) and dark hair (R ~ G ~ B) are left untouched by the
    limiter, so it is applied at full strength outside the cut and within `band` texels
    inside it, fading out over another `band` texels.
    """
    inside_dist = cv2.distanceTransform((cov > 0.5).astype(np.uint8), cv2.DIST_L2, 5)
    weight = np.clip(2.0 - inside_dist / band, 0, 1)[..., None]
    spill = np.clip((srgb[..., 1] - 0.5 * (srgb[..., 0] + srgb[..., 2])) / 0.04, 0, 1)[..., None]
    limited = srgb.copy()
    limited[..., 1] = np.minimum(srgb[..., 1], 0.5 * (srgb[..., 0] + srgb[..., 2]))
    # foliage spill is yellow-green, so clamping G alone leaves an orange cast: also pull
    # strongly spilled pixels towards grey (the hair here is neutral dark brown/grey)
    lum = (limited @ np.array([0.2126, 0.7152, 0.0722], np.float32))[..., None]
    limited = limited + 0.8 * spill * (lum - limited)
    return weight * limited + (1 - weight) * srgb


def greenness_report(srgb: np.ndarray, cov: np.ndarray, band: float) -> dict:
    """Foliage check on the (pre-print) front colours: spill = G - (R + B) / 2 in a band from
    `band` texels inside the cut line to 4 texels outside it, at mip levels 0..5
    (box-filtered like a GPU mip chain; the band shrinks with the mip)."""
    out = {}
    img, cv = srgb.astype(np.float32), cov.astype(np.float32)
    for level in range(6):
        b = max(1.0, band / 2 ** level)
        d_in = cv2.distanceTransform((cv > 0.5).astype(np.uint8), cv2.DIST_L2, 5)
        d_out = cv2.distanceTransform((cv <= 0.5).astype(np.uint8), cv2.DIST_L2, 5)
        ring = ((cv > 0.5) & (d_in <= b)) | ((cv <= 0.5) & (d_out <= max(1.0, 4 / 2 ** level)))
        g = img[..., 1] - 0.5 * (img[..., 0] + img[..., 2])
        out[f"mip{level}"] = {"max_spill": round(float(g[ring].max()), 4),
                              "frac_spill_gt_0.02": round(float(np.mean(g[ring] > 0.02)), 5)}
        img = cv2.resize(img, (max(1, img.shape[1] // 2), max(1, img.shape[0] // 2)), interpolation=cv2.INTER_AREA)
        cv = cv2.resize(cv, (max(1, cv.shape[1] // 2), max(1, cv.shape[0] // 2)), interpolation=cv2.INTER_AREA)
    return out


# --- atlas assembly -----------------------------------------------------------------------


def place(atlas: np.ndarray, tile: np.ndarray, rect, wrap_u: bool = False) -> None:
    """Paste a region tile with PAD texels of edge-extended (or u-wrapped) padding."""
    x0, y0, x1, y1 = rect
    assert tile.shape[:2] == (y1 - y0, x1 - x0), (tile.shape, rect)
    pad_spec = ((PAD, PAD), (0, 0)) + ((0, 0),) * (tile.ndim - 2)
    padded = np.pad(tile, pad_spec, mode="edge")
    pad_spec = ((0, 0), (PAD, PAD)) + ((0, 0),) * (tile.ndim - 2)
    padded = np.pad(padded, pad_spec, mode="wrap" if wrap_u else "edge")
    atlas[y0 - PAD:y1 + PAD, x0 - PAD:x1 + PAD] = padded


def lit_preview(diffuse_u8: np.ndarray, normal_u8: np.ndarray) -> np.ndarray:
    """Shade a diffuse tile with its normal map (light from upper left, DirectX G = down)."""
    n = normal_u8[..., :3].astype(np.float32) / 127.5 - 1.0
    light = np.array([-0.5, -0.5, 0.71], np.float32)  # from the upper left (y down)
    light /= np.linalg.norm(light)
    lam = np.clip((n @ light) / light[2], 0, 2)  # 1.0 for a flat surface
    out = diffuse_u8.astype(np.float32) * (0.25 + 0.75 * lam[..., None])
    return np.clip(out, 0, 255).astype(np.uint8)


def _label(img, rect, text, scale_px):
    x0, y0 = int(rect[0] * scale_px), int(rect[1] * scale_px)
    x1, y1 = int(rect[2] * scale_px), int(rect[3] * scale_px)
    cv2.rectangle(img, (x0, y0), (x1 - 1, y1 - 1), (255, 0, 255), 2)
    cv2.putText(img, text, (x0 + 6, y0 + 22), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 0, 0), 4, cv2.LINE_AA)
    cv2.putText(img, text, (x0 + 6, y0 + 22), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 1, cv2.LINE_AA)


def write_previews(atlas_d, atlas_n, rects, twine_d, twine_n):
    half = atlas_d.shape[0] * 3 // 8  # preview at 768 px per map for a 2048 atlas
    sc = half / atlas_d.shape[0]
    d = cv2.resize(atlas_d, (half, half), interpolation=cv2.INTER_AREA)
    n = cv2.resize(atlas_n[..., :3], (half, half), interpolation=cv2.INTER_AREA)
    a = cv2.resize(atlas_n[..., 3], (half, half), interpolation=cv2.INTER_AREA)
    for img in (d, n):
        for name, r in rects.items():
            _label(img, r, name, sc)
    a_rgb = np.repeat(a[..., None], 3, axis=2)
    cv2.putText(a_rgb, "normal alpha = specular mask", (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (255, 255, 255), 2, cv2.LINE_AA)
    sheet = np.concatenate([d, np.full((half, 8, 3), 255, np.uint8), n], axis=1)
    save_png(RENDERS / "textures_atlas.png", sheet)

    # 1:1 close-ups of each material, flat and lit by its normal map
    lit = lit_preview(atlas_d, atlas_n)
    crops = []
    fx0, fy0, fx1, fy1 = rects["front"]
    crops.append((lit, (fx1 - 400, fy0 + 500, fx1, fy0 + 900), "front (cut edge, hair)"))
    bx0, by0, bx1, by1 = rects["back"]
    crops.append((lit, (bx0, by0, bx0 + 400, by0 + 400), "back (lit)"))
    tiles = []
    for img, (x0, y0, x1, y1), text in crops:
        t = np.ascontiguousarray(img[y0:y1, x0:x1])
        cv2.putText(t, text, (8, 22), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 2, cv2.LINE_AA)
        tiles.append(t)
    ex0, ey0, ex1, ey1 = rects["edge"]
    edge_d = atlas_d[ey0:ey1, ex0:ex1]
    edge_l = lit[ey0:ey1, ex0:ex1]
    tx0, ty0, tx1, ty1 = rects["tape"]
    tape_l = lit[ty0:ty1, tx0:tx1]
    tw_l = lit_preview(twine_d, twine_n)
    tw = np.concatenate([twine_d, tw_l], axis=1)
    # stack: [front | back] / edge x2 (diffuse, lit) at 2x zoom / tape lit / twine
    row1 = np.concatenate(tiles, axis=1)
    width = row1.shape[1]

    def fit(img):
        s = width / img.shape[1]
        return cv2.resize(img, (width, int(round(img.shape[0] * s))), interpolation=cv2.INTER_NEAREST if s > 1 else cv2.INTER_AREA)

    edge2 = fit(np.concatenate([edge_d[:, : edge_d.shape[1] // 2], edge_l[:, : edge_l.shape[1] // 2]], axis=0))
    sep = np.full((8, width, 3), 255, np.uint8)
    sheet = np.concatenate([row1, sep, edge2, sep, fit(tape_l), sep, fit(tw)], axis=0)
    save_png(RENDERS / "textures_closeups.png", sheet)


def write_cutout_preview(atlas_d, rect, cov):
    """The printed front as it will look once cut out: final atlas texels inside the cut,
    a 3-texel cardboard rim and a soft shadow on a neutral table colour (half size)."""
    x0, y0, x1, y1 = rect
    front = atlas_d[y0:y1, x0:x1].astype(np.float32)
    c = cov[..., None]
    rim = cv2.dilate(cov, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (7, 7)))[..., None]
    shadow = cv2.GaussianBlur(np.roll(np.roll(rim[..., 0], 14, 0), 10, 1), (0, 0), 9)[..., None]
    table = np.full_like(front, 224.0) * (1 - 0.4 * shadow)
    out = table * (1 - rim) + rim * np.array([170, 128, 84], np.float32)
    out = out * (1 - c) + front * c
    out = cv2.resize(np.clip(out, 0, 255).astype(np.uint8), ((x1 - x0) // 2, (y1 - y0) // 2),
                     interpolation=cv2.INTER_AREA)
    save_png(RENDERS / "textures_front_cutout.png", out)


# --- main -----------------------------------------------------------------------------------


def main() -> int:
    t0 = time.time()
    cfg = load_config()
    size = int(cfg["texture_size"])
    tsize = int(cfg["twine_texture_size"])
    outline = json.loads((WORK / "outline.json").read_text(encoding="utf-8"))
    mode = outline["selected_mode"]
    poly = np.array(outline["modes"][mode]["polygon"], dtype=np.float64)
    img_lin, _ = load_source_linear(ROOT / outline["source"])
    matte = np.asarray(Image.open(ROOT / outline.get("matte", "work/matte.png")), dtype=np.float32) / 255.0
    rng = np.random.default_rng(SEED)

    crop = crop_box(outline)
    aspect = (crop[2] - crop[0]) / (crop[3] - crop[1])
    rects = plan_layout(size, aspect)
    image_rect, scale = exact_image_rect(crop, rects["front"])
    lm = outline["landmarks"]
    px_per_unit = abs(lm["chin"][1] - lm["crown"][1]) / (HEAD_HEIGHT_UNITS * float(cfg.get("size_factor", 1.0)))
    ridge_front = FLUTE_PITCH_UNITS * px_per_unit * scale                      # texels per flute (front)

    fw, fh = rects["front"][2] - rects["front"][0], rects["front"][3] - rects["front"][1]
    front_rgb, front_pre, front_n, cov = build_front(img_lin, matte, poly, image_rect, scale, (fw, fh), rng,
                                                     ridge_front)
    green = greenness_report(front_pre, cov, band=12 * scale)
    print(f"front {fw}x{fh} texels from image box {[round(v, 2) for v in image_rect]} "
          f"({scale:.4f} texel/px); cut-edge greenness {green['mip0']} .. {green['mip5']}")

    bw, bh = rects["back"][2] - rects["back"][0], rects["back"][3] - rects["back"][1]
    back_scale = bw / (image_rect[2] - image_rect[0])
    back_rgb, back_h = kraft_back((bh, bw), rng, FLUTE_PITCH_UNITS * px_per_unit * back_scale)
    back_n = encode_normal(height_to_normal(back_h, 0.35), SPEC["back"])

    ew, eh = rects["edge"][2] - rects["edge"][0], rects["edge"][3] - rects["edge"][1]
    flutes = max(1, int(round(EDGE_TILE_UNITS / FLUTE_PITCH_UNITS)))
    edge_rgb, edge_h, layers = corrugated_edge((eh, ew), rng, flutes)
    edge_n = encode_normal(height_to_normal(edge_h, 2.5, wrap=(True, False)), SPEC["edge"])

    tw_, th_ = rects["tape"][2] - rects["tape"][0], rects["tape"][3] - rects["tape"][1]
    tape_rgb, tape_h, tape_spec, _ = masking_tape((th_, tw_), rng)
    tape_n = encode_normal(height_to_normal(tape_h, 0.6), tape_spec)

    twine_rgb, twine_h = twine(tsize, rng, plies=3, twists=TWINE_TWISTS)
    twine_n = encode_normal(height_to_normal(twine_h, 0.12 * tsize / 512 * 4, wrap=(True, True)), SPEC["twine"])

    # assemble the atlas: neutral kraft background, flat normals, kraft spec
    atlas_d = np.empty((size, size, 3), np.uint8)
    atlas_d[:] = to_srgb_u8(back_rgb.reshape(-1, 3).mean(axis=0))
    atlas_n = np.empty((size, size, 4), np.uint8)
    atlas_n[:] = encode_normal(np.array([[[0.0, 0.0, 1.0]]]), SPEC["back"])[0, 0]
    for name, rgb, nrm in (("front", front_rgb, front_n), ("back", back_rgb, back_n),
                           ("edge", edge_rgb, edge_n), ("tape", tape_rgb, tape_n)):
        place(atlas_d, to_srgb_u8(rgb), rects[name], wrap_u=(name == "edge"))
        place(atlas_n, nrm, rects[name], wrap_u=(name == "edge"))

    OUT.mkdir(parents=True, exist_ok=True)
    save_png(OUT / "mask_d.png", atlas_d)
    save_png(OUT / "mask_n.png", atlas_n)
    save_png(OUT / "twine_d.png", to_srgb_u8(twine_rgb))
    save_png(OUT / "twine_n.png", twine_n)

    def uv(r):
        return [r[0] / size, r[1] / size, r[2] / size, r[3] / size]

    layout = {
        "size": size,
        "uv_convention": "u right, v down (v=0 top row)",
        "normal_map_convention": "tangent space, DirectX (Skyrim): R=+u, G=+v (down), B=out; alpha = specular mask",
        "padding_texels": PAD,
        "silhouette_mode": mode,
        "regions": {
            "front": {"uv_rect": uv(rects["front"]), "image_rect": [round(v, 4) for v in image_rect],
                      "texels_per_image_px": round(scale, 6)},
            "back": {"uv_rect": uv(rects["back"]), "image_rect": [round(v, 4) for v in image_rect], "mirror_u": True},
            "edge": {"uv_rect": uv(rects["edge"]), "tile_length_units": EDGE_TILE_UNITS, "wrap_u": True,
                     "layers_v": layers, "flutes_per_tile": flutes},
            "tape": {"uv_rect": uv(rects["tape"]), "aspect": round(tw_ / th_, 4),
                     "note": "tape body fills the rect apart from a ~6% (u) / 10% (v) kraft-coloured margin with torn ends"},
        },
        "twine": {"size": tsize, "tile_length_units": TWINE_TILE_UNITS, "plies": 3, "twists_per_tile": TWINE_TWISTS,
                  "uv": "u along the string (wraps every tile_length_units), v around the circumference (0..1)"},
        "specular": SPEC,
        "front_cut_edge_greenness": green,
    }
    (WORK / "atlas_layout.json").write_text(json.dumps(layout, indent=1), encoding="utf-8")
    write_previews(atlas_d, atlas_n, rects, to_srgb_u8(twine_rgb), twine_n)
    write_cutout_preview(atlas_d, rects["front"], cov)
    print(f"rects {rects}")
    print(f"wrote work/textures/*.png, work/atlas_layout.json, docs/renders/textures_*.png ({time.time() - t0:.1f}s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
