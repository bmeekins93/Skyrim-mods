"""Acceptance checks for the image stages (01 silhouette, 02 textures, 05 DDS).

Each group skips cleanly when its work/ outputs have not been built yet.
Run: .venv/bin/python -m pytest tests/test_images.py -q
"""
import json
import math
import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from cfm.paths import WORK, load_config  # noqa: E402

OUTLINE = WORK / "outline.json"
LAYOUT = WORK / "atlas_layout.json"
TEX = WORK / "textures"
DDS = WORK / "dds"
MODES = ("face_only", "face_hair", "face_hair_glasses")


def _load(path):
    if not path.exists():
        pytest.skip(f"{path.relative_to(ROOT)} not built")
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def outline():
    return _load(OUTLINE)


@pytest.fixture(scope="module")
def layout():
    return _load(LAYOUT)


def _signed_area(p):
    x, y = p[:, 0], p[:, 1]
    return 0.5 * float(np.sum(x * np.roll(y, -1) - np.roll(x, -1) * y))


# --- stage 0: decoding ----------------------------------------------------------------


def test_source_decode_matches_littlecms():
    """16-bit Display-P3 decode agrees with LittleCMS (8-bit path) to within 2 LSB."""
    import io

    from PIL import Image, ImageCms

    from cfm.imaging import load_source_srgb

    src = ROOT / load_config()["source_image"]
    if not src.exists():
        pytest.skip("source photo missing")
    ours, info = load_source_srgb(src)
    pil = Image.open(src)
    if "icc_profile" not in pil.info:
        pytest.skip("source has no ICC profile")
    prof = ImageCms.ImageCmsProfile(io.BytesIO(pil.info["icc_profile"]))
    ref = np.asarray(ImageCms.profileToProfile(pil, prof, ImageCms.createProfile("sRGB"), outputMode="RGB"))
    diff = np.abs(ours.astype(int) - ref.astype(int))
    assert ours.shape == ref.shape and ours.dtype == np.uint8
    assert diff.max() <= 2 and diff.mean() < 0.6, (diff.max(), diff.mean())


# --- stage 1: silhouette --------------------------------------------------------------------


def test_outline_schema(outline):
    assert outline["selected_mode"] in MODES
    assert set(MODES) <= set(outline["modes"])
    assert len(outline["image_size"]) == 2
    for k in ("eye_image_left", "eye_image_right", "nose_tip", "mouth", "chin", "crown", "cut_top"):
        assert len(outline["landmarks"][k]) == 2


@pytest.mark.parametrize("mode", MODES)
def test_polygon_valid(outline, mode):
    from shapely.geometry import LineString, Polygon

    p = np.array(outline["modes"][mode]["polygon"], dtype=float)
    w, h = outline["image_size"]
    assert 120 <= len(p) <= 400, len(p)
    assert not np.allclose(p[0], p[-1]), "polygon must not repeat its first point"
    poly = Polygon(p)
    assert poly.is_valid and LineString(np.vstack([p, p[:1]])).is_simple
    assert _signed_area(p) > 0, "orientation: positive shoelace area in image coordinates"
    assert poly.exterior.is_ccw  # shapely uses the same raw-coordinate convention
    assert p[:, 0].min() >= 0 and p[:, 1].min() >= 0 and p[:, 0].max() <= w and p[:, 1].max() <= h
    edges = np.linalg.norm(np.diff(np.vstack([p, p[:1]]), axis=0), axis=1)
    assert edges.min() > 0.5, "duplicate / degenerate vertices"
    # die-cut smoothness: no needle spikes (interior turn angles stay gentle)
    v1, v2 = np.roll(p, 1, 0) - p, np.roll(p, -1, 0) - p
    cos = np.sum(v1 * v2, 1) / (np.linalg.norm(v1, axis=1) * np.linalg.norm(v2, axis=1))
    assert np.degrees(np.arccos(np.clip(cos, -1, 1))).min() > 90


def test_landmarks(outline):
    from shapely.geometry import Point, Polygon

    lm = {k: np.array(v) for k, v in outline["landmarks"].items()}
    p = np.array(outline["modes"][outline["selected_mode"]]["polygon"])
    poly = Polygon(p)
    for k in ("eye_image_left", "eye_image_right", "nose_tip", "mouth"):
        assert poly.contains(Point(*lm[k])), k
    for k in ("chin", "cut_top"):  # these lie on the selected outline
        assert poly.exterior.distance(Point(*lm[k])) < 1.0, k
    assert math.isclose(lm["cut_top"][1], p[:, 1].min(), abs_tol=0.01)
    # crown = top of the head incl. hair (face_hair polygon), the same for every mode
    head = np.array(outline["modes"]["face_hair"]["polygon"])
    assert Polygon(head).exterior.distance(Point(*lm["crown"])) < 1.0
    assert math.isclose(lm["crown"][1], head[:, 1].min(), abs_tol=0.01)
    eye_y = 0.5 * (lm["eye_image_left"][1] + lm["eye_image_right"][1])
    assert lm["crown"][1] <= lm["cut_top"][1] < eye_y
    ys = [lm["crown"][1], eye_y, lm["nose_tip"][1], lm["mouth"][1], lm["chin"][1]]
    assert ys == sorted(ys) and len(set(ys)) == 5, ys
    assert lm["eye_image_left"][0] < lm["nose_tip"][0] < lm["eye_image_right"][0]
    assert lm["eye_image_left"][0] < lm["mouth"][0] < lm["eye_image_right"][0]


def test_iris_landmarks_centred(outline):
    """Eye landmarks sit in the middle of the iris: the iris edge is about one fitted radius
    away on the lower-left, lower-right and lower side (the lids hide the top), measured
    directly on the decoded photo."""
    from cfm.img_landmarks import iris_symmetry, luminance
    from cfm.imaging import load_source_srgb

    src = ROOT / outline["source"]
    if not src.exists():
        pytest.skip("source photo missing")
    lum = luminance(load_source_srgb(src)[0])
    for k in ("eye_image_left", "eye_image_right"):
        fit = outline["iris_fit"][k]
        assert np.allclose(fit["centre"], outline["landmarks"][k]), k
        r = fit["radius_px"]
        sym = iris_symmetry(lum, outline["landmarks"][k], r)
        assert abs(sym["asymmetry_over_radius"]) < 0.15, (k, sym)  # centre within ~0.15 r sideways
        for side in ("left_px", "right_px", "down_px"):
            assert abs(sym[side] / r - 1.0) <= 0.2, (k, side, sym)


@pytest.mark.parametrize("mode", ("face_hair", "face_hair_glasses"))
def test_hair_not_cut_off(outline, mode):
    """Above the eyes the matte holds only head and hair, so (almost) all matte foreground
    there must be inside a cut that includes hair. Uses work/matte.png directly, independent
    of the semantic exclusions stage 1 applies."""
    import cv2
    from PIL import Image

    matte_path = ROOT / outline["matte"]
    if not matte_path.exists():
        pytest.skip("matte not built")
    fg = np.asarray(Image.open(matte_path)).astype(float) / 255 > 0.5
    p = np.array(outline["modes"][mode]["polygon"])
    inside = np.zeros(fg.shape, np.uint8)
    cv2.fillPoly(inside, [np.round((p - 0.5) * 16).astype(np.int32)], 1, cv2.LINE_8, 4)
    lm = outline["landmarks"]
    eye_y = min(lm["eye_image_left"][1], lm["eye_image_right"][1])
    above = (np.arange(fg.shape[0]) + 0.5 < eye_y)[:, None]
    lost = fg & above & ~inside.astype(bool)
    assert lost.sum() < 0.001 * inside.sum(), (int(lost.sum()), int(inside.sum()))
    # and no single chunk of hair is sliced off (the old failure: a 4.6k px crown tuft)
    n, _, stats, _ = cv2.connectedComponentsWithStats(lost.astype(np.uint8), connectivity=8)
    assert n == 1 or stats[1:, cv2.CC_STAT_AREA].max() < 400, stats[1:, cv2.CC_STAT_AREA].max()


def test_modes_nested(outline):
    """face_only lies inside face_hair, which lies inside face_hair_glasses (up to smoothing)."""
    from shapely.geometry import Polygon

    a, b, c = (Polygon(outline["modes"][m]["polygon"]) for m in MODES)
    assert a.difference(b).area < 0.02 * a.area
    assert b.difference(c).area < 0.02 * b.area


# --- stage 2: textures ----------------------------------------------------------------------


def _px_rect(r, size):
    return [int(round(v * size)) for v in r]


def test_atlas_layout(layout):
    cfg = load_config()
    size = layout["size"]
    assert size == cfg["texture_size"]
    pad = layout["padding_texels"]
    assert pad >= 16
    rects = {}
    for name in ("front", "back", "edge", "tape"):
        r = layout["regions"][name]["uv_rect"]
        assert all(0.0 <= v <= 1.0 for v in r) and r[0] < r[2] and r[1] < r[3], name
        x0, y0, x1, y1 = _px_rect(r, size)
        assert x0 >= pad and y0 >= pad and x1 <= size - pad and y1 <= size - pad, f"{name} padding at atlas border"
        rects[name] = (x0 - pad, y0 - pad, x1 + pad, y1 + pad)
    names = list(rects)
    for i, a in enumerate(names):  # padded rects must not overlap
        for b in names[i + 1:]:
            ra, rb = rects[a], rects[b]
            overlap = ra[0] < rb[2] and rb[0] < ra[2] and ra[1] < rb[3] and rb[1] < ra[3]
            assert not overlap, (a, b)
    # front: aspect preserved, biggest texel area
    f = layout["regions"]["front"]
    fx0, fy0, fx1, fy1 = _px_rect(f["uv_rect"], size)
    ix0, iy0, ix1, iy1 = f["image_rect"]
    assert abs((fx1 - fx0) / (fy1 - fy0) - (ix1 - ix0) / (iy1 - iy0)) < 1e-3
    areas = {n: (rects[n][2] - rects[n][0]) * (rects[n][3] - rects[n][1]) for n in rects}
    assert max(areas, key=areas.get) == "front"
    assert layout["regions"]["back"]["image_rect"] == f["image_rect"] and layout["regions"]["back"]["mirror_u"]
    assert layout["regions"]["edge"]["tile_length_units"] > 0 and layout["twine"]["tile_length_units"] > 0


def test_front_covers_all_modes(outline, layout):
    ix0, iy0, ix1, iy1 = layout["regions"]["front"]["image_rect"]
    for m in MODES:
        p = np.array(outline["modes"][m]["polygon"])
        assert p[:, 0].min() > ix0 and p[:, 0].max() < ix1 and p[:, 1].min() > iy0 and p[:, 1].max() < iy1, m


def _img(name):
    from PIL import Image

    path = TEX / name
    if not path.exists():
        pytest.skip(f"{name} not built")
    return np.asarray(Image.open(path))


def test_texture_files(layout):
    cfg = load_config()
    s, t = cfg["texture_size"], cfg["twine_texture_size"]
    assert _img("mask_d.png").shape == (s, s, 3)
    assert _img("mask_n.png").shape == (s, s, 4)
    assert _img("twine_d.png").shape == (t, t, 3)
    assert _img("twine_n.png").shape == (t, t, 4)
    for name in ("mask_n.png", "twine_n.png"):
        n = _img(name)[..., :3].astype(float) / 127.5 - 1
        length = np.linalg.norm(n, axis=2)
        assert np.abs(length - 1).max() < 0.03, name
        assert (n[..., 2] > 0).all(), f"{name}: normals must face out of the surface"


def test_specular_alpha(layout):
    a = _img("mask_n.png")[..., 3].astype(float) / 255
    size = layout["size"]
    expect = {"front": (0.35, 0.45), "back": (0.08, 0.12), "edge": (0.06, 0.10)}
    for name, (lo, hi) in expect.items():
        x0, y0, x1, y1 = _px_rect(layout["regions"][name]["uv_rect"], size)
        m = a[y0:y1, x0:x1].mean()
        assert lo <= m <= hi, (name, m)
    x0, y0, x1, y1 = _px_rect(layout["regions"]["tape"]["uv_rect"], size)
    cy, cx = (y0 + y1) // 2, (x0 + x1) // 2
    assert 0.22 <= a[cy - 5:cy + 5, cx - 20:cx + 20].mean() <= 0.28
    tw = _img("twine_n.png")[..., 3].astype(float).mean() / 255
    assert 0.08 <= tw <= 0.12


def test_edge_strip_tiles_in_u(layout):
    d = _img("mask_d.png").astype(float)
    x0, y0, x1, y1 = _px_rect(layout["regions"]["edge"]["uv_rect"], layout["size"])
    strip = d[y0:y1, x0:x1]
    seam = np.abs(strip[:, 0] - strip[:, -1]).mean()
    inner = np.mean([np.abs(strip[:, i] - strip[:, i + 1]).mean() for i in range(10, strip.shape[1] - 10, 37)])
    assert seam < 2.0 * inner + 2.0, (seam, inner)
    # padding is wrapped, so sampling across the rect border continues the pattern
    assert np.array_equal(d[y0:y1, x0 - 1], d[y0:y1, x1 - 1])


def test_twine_tiles():
    for name in ("twine_d.png", "twine_n.png"):
        t = _img(name).astype(float)
        inner_u = np.abs(t[:, 200] - t[:, 201]).mean()
        inner_v = np.abs(t[200] - t[201]).mean()
        assert np.abs(t[:, 0] - t[:, -1]).mean() < 1.5 * inner_u + 1, name
        assert np.abs(t[0] - t[-1]).mean() < 1.5 * inner_v + 1, name


def test_no_foliage_at_cut_edge(outline, layout):
    """Front print: no green spill near the cut line, at mip 0 and in the recorded mip report."""
    import cv2

    d = _img("mask_d.png").astype(float) / 255
    size = layout["size"]
    f = layout["regions"]["front"]
    x0, y0, x1, y1 = _px_rect(f["uv_rect"], size)
    ix0, iy0, ix1, iy1 = f["image_rect"]
    s = (x1 - x0) / (ix1 - ix0)
    p = np.array(outline["modes"][outline["selected_mode"]]["polygon"])
    t = (p - [ix0, iy0]) * s + [x0, y0] - 0.5
    mask = np.zeros(d.shape[:2], np.uint8)
    cv2.fillPoly(mask, [np.round(t * 16).astype(np.int32)], 1, cv2.LINE_8, 4)
    ring = cv2.dilate(mask, np.ones((9, 9), np.uint8)) - cv2.erode(mask, np.ones((25, 25), np.uint8))
    spill = d[..., 1] - 0.5 * (d[..., 0] + d[..., 2])
    assert spill[ring.astype(bool)].max() < 0.04
    for level, stats in layout["front_cut_edge_greenness"].items():
        assert stats["max_spill"] < 0.02, level


# --- stage 3: DDS --------------------------------------------------------------------------


@pytest.mark.parametrize("name,kinds", [("mask_d", ("BC7", "BC1")), ("twine_d", ("BC7", "BC1")),
                                        ("mask_n", ("BC3", "BC7")), ("twine_n", ("BC3", "BC7"))])
def test_dds(name, kinds):
    from PIL import Image

    from cfm.img_dds import (DDSD_LINEARSIZE, DDSD_MIPMAPCOUNT, FORMATS, decode_level, parse_dds, psnr,
                             read_levels)

    path = DDS / f"{name}.dds"
    if not path.exists():
        pytest.skip(f"{name}.dds not built")
    data = path.read_bytes()
    assert data[:4] == b"DDS "
    info = parse_dds(data)
    w, h = info.width, info.height
    assert w == h and (w & (w - 1)) == 0
    assert info.flags & DDSD_MIPMAPCOUNT and info.flags & DDSD_LINEARSIZE
    assert info.mip_count == int(math.log2(w)) + 1
    assert info.pitch_or_linear_size == info.level_sizes[0]
    assert info.fmt in kinds
    if info.fmt == "BC7":
        assert info.fourcc == b"DX10" and info.dxgi_format == FORMATS["BC7"][1]  # BC7_UNORM, not _SRGB
    levels = read_levels(data, info)  # raises on a payload size mismatch
    src = np.asarray(Image.open(TEX / f"{name}.png").convert("RGBA"))
    chans = slice(0, 3) if name.endswith("_d") else slice(0, 4)
    with Image.open(path) as im:
        pil = np.asarray(im.convert("RGBA"))
    assert psnr(pil[..., chans], src[..., chans]) > 30.0
    last = decode_level(levels[-1], 1, 1, info.fmt)  # 1x1 mip is decodable
    assert last.shape == (1, 1, 4)
