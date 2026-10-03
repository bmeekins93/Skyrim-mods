"""Texture builders for stage 2: photo print, kraft back, corrugated edge, masking tape, twine.

Conventions
- Colours are float sRGB in [0, 1] unless a name says `lin` (linear light).
- Height fields are floats in texel units of "bumpiness"; `height_to_normal` turns them into
  tangent-space normals in the DirectX convention Skyrim uses:
  R = +u (texture right), G = +v (texture DOWN, i.e. increasing image row), B = out of the surface.
- Every generator takes a seeded numpy Generator, so outputs are deterministic.
"""
from __future__ import annotations

import cv2
import numpy as np

from .img_noise import fibres, normalise, smoothstep, spectral_noise
from .imaging import linear_to_srgb, srgb_to_linear

KRAFT_SRGB = np.array([176, 134, 90]) / 255.0       # brown corrugated board liner
KRAFT_DARK_SRGB = np.array([92, 64, 38]) / 255.0    # inside the flutes
PAPER_SRGB = np.array([238, 233, 222]) / 255.0      # photo paper core seen on the cut edge
TAPE_SRGB = np.array([228, 213, 172]) / 255.0       # beige masking tape
JUTE_SRGB = np.array([186, 152, 102]) / 255.0       # natural jute/hemp twine

SPEC = {"front": 0.40, "back": 0.10, "edge": 0.08, "tape": 0.25, "twine": 0.10}


# --- normal maps -------------------------------------------------------------------


def height_to_normal(height: np.ndarray, strength: float, wrap=(False, False)) -> np.ndarray:
    """Tangent-space normals (H, W, 3) in [-1, 1], DirectX convention (G = +v, down)."""
    def diff(axis, periodic):
        if periodic:
            return 0.5 * (np.roll(height, -1, axis) - np.roll(height, 1, axis))
        return np.gradient(height, axis=axis)
    dx = diff(1, wrap[0])
    dy = diff(0, wrap[1])
    n = np.dstack([-strength * dx, -strength * dy, np.ones_like(height)])
    return n / np.linalg.norm(n, axis=2, keepdims=True)


def encode_normal(n: np.ndarray, spec: np.ndarray | float) -> np.ndarray:
    """(H, W, 3) unit normals + specular mask -> RGBA uint8."""
    rgb = np.clip(np.rint((n * 0.5 + 0.5) * 255), 0, 255)
    a = np.broadcast_to(np.asarray(spec, np.float64), n.shape[:2])
    alpha = np.clip(np.rint(a * 255), 0, 255)
    return np.dstack([rgb, alpha]).astype(np.uint8)


# --- photo print -----------------------------------------------------------------------


def pull_push(col: np.ndarray, w: np.ndarray, boost: float = 2.0) -> np.ndarray:
    """Fill colours where w == 0 from surrounding weighted colours (pull-push / Gortler 1996).

    col: (H, W, C) valid where w > 0; w: (H, W) weights in [0, 1]. Known pixels keep
    their colour (blended by w); holes get a smooth extension of nearby edge colours.
    """
    h, wd = w.shape
    w3 = w[..., None]
    if min(h, wd) <= 4:
        total = max(float(w.sum()), 1e-8)
        mean = (col * w3).reshape(-1, col.shape[2]).sum(0) / total
        return col * w3 + mean * (1 - w3)
    half = (max(1, (wd + 1) // 2), max(1, (h + 1) // 2))
    num = cv2.resize((col * w3).astype(np.float32), half, interpolation=cv2.INTER_AREA)
    den = cv2.resize(w.astype(np.float32), half, interpolation=cv2.INTER_AREA)
    if num.ndim == 2:
        num = num[..., None]
    coarse = num / np.maximum(den, 1e-8)[..., None]
    filled = pull_push(coarse, np.clip(den * boost, 0, 1), boost)
    up = cv2.resize(filled.astype(np.float32), (wd, h), interpolation=cv2.INTER_LINEAR)
    if up.ndim == 2:
        up = up[..., None]
    return col * w3 + up * (1 - w3)


def estimate_foreground(img: np.ndarray, alpha: np.ndarray, r1: int = 90, r2: int = 6) -> np.ndarray:
    """Foreground colour estimate ("blur fusion", Forte & Pitie, ICIP 2021 / Photoroom
    fast-foreground-estimation, MIT) - removes background colour mixed into soft edges."""
    a = alpha[..., None].astype(np.float32)
    img = img.astype(np.float32)

    def fb(image, f, b, r):
        ba = cv2.blur(a, (r, r))[..., None]
        bf = cv2.blur(f * a, (r, r)) / (ba + 1e-5)
        bb = cv2.blur(b * (1 - a), (r, r)) / ((1 - ba) + 1e-5)
        f_new = bf + a * (image - a * bf - (1 - a) * bb)
        return np.clip(f_new, 0, 1), bb

    f, blur_b = fb(img, img, img, r1)
    return fb(img, f, blur_b, r2)[0]


def print_look(srgb: np.ndarray, rng: np.random.Generator) -> np.ndarray:
    """Inkjet-on-cardstock look: a little desaturated, gentle S-curve, warm paper, lifted
    blacks and faint grain. Input/output float sRGB."""
    lum = srgb @ np.array([0.2126, 0.7152, 0.0722])
    x = lum[..., None] + 0.88 * (srgb - lum[..., None])                # -12 % saturation
    x = np.clip(x, 0, 1)
    x = 0.8 * x + 0.2 * smoothstep(0.0, 1.0, x)                           # gentle S-curve
    x = 0.5 + 1.05 * (x - 0.5)                                            # +5 % contrast
    x = 0.035 + 0.955 * np.clip(x, 0, 1)                                  # ink black / paper white
    x = x * np.array([1.0, 0.978, 0.94])                                  # warm paper tint
    h, w = srgb.shape[:2]
    grain = cv2.GaussianBlur(rng.standard_normal((h, w)).astype(np.float32), (0, 0), 0.7)
    chroma = cv2.GaussianBlur(rng.standard_normal((h, w, 3)).astype(np.float32), (0, 0), 1.2)
    x = x + 0.016 * normalise(grain)[..., None] + 0.004 * chroma
    return np.clip(x, 0, 1)


def paper_height(shape, rng: np.random.Generator, ridge_period: float) -> np.ndarray:
    """Cardstock tooth plus very faint washboarding from the flutes under the liner."""
    tooth = spectral_noise(shape, rng, beta=1.2, f_high=0.35)
    soft = spectral_noise(shape, rng, beta=3.0, f_high=0.02)
    xs = np.arange(shape[1])[None, :]
    ridges = np.cos(2 * np.pi * xs / ridge_period)
    return 0.25 * tooth + 0.8 * soft + 1.2 * ridges


# --- kraft back ------------------------------------------------------------------------


def _stain_mask(shape, rng, centre, radius, ring: bool) -> np.ndarray:
    """Soft irregular stain; ring=True gives a coffee-ring (darker rim)."""
    h, w = shape
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float64)
    ang = np.arctan2(yy - centre[1], xx - centre[0])
    k = np.arange(1, 6)
    phase = rng.uniform(0, 2 * np.pi, 5)
    amp = rng.uniform(0.02, 0.08, 5) / k
    wobble = 1 + np.sum(amp[:, None, None] * np.sin(k[:, None, None] * ang + phase[:, None, None]), axis=0)
    r = np.hypot(xx - centre[0], yy - centre[1]) / (radius * wobble)
    if ring:
        return 0.3 * smoothstep(1.02, 0.5, r) + 0.8 * np.exp(-((r - 0.96) / 0.07) ** 2)
    return smoothstep(1.05, 0.55, r) * (0.6 + 0.4 * np.exp(-((r - 0.95) / 0.08) ** 2))


def kraft_back(shape, rng: np.random.Generator, ridge_period: float):
    """Plain kraft liner: fibres, mottling, recycled specks, washboard ridges, two faint stains.
    Returns (colour sRGB float (H, W, 3), height (H, W))."""
    h, w = shape
    mott = spectral_noise(shape, rng, beta=3.2, f_high=0.03)
    hue = spectral_noise(shape, rng, beta=3.5, f_high=0.02)
    fine = spectral_noise(shape, rng, beta=1.0, f_high=0.4)
    fib = fibres(shape, rng, count=h * w // 30, length=(4, 22), angle_std=np.pi)
    long_fib = fibres(shape, rng, count=h * w // 900, length=(15, 45), angle_std=np.pi, curl=0.4)
    specks = np.zeros(shape, np.uint8)  # dark recycled-fibre specks (LINE_AA needs 8-bit)
    k = max(1, h * w // 4000)
    for x, y, r in zip(rng.uniform(0, w, k), rng.uniform(0, h, k), rng.uniform(0.6, 1.6, k)):
        cv2.circle(specks, (int(x * 16), int(y * 16)), int(r * 16), 255, -1, cv2.LINE_AA, 4)
    specks = specks.astype(np.float32) / 255.0
    xs = np.arange(w)[None, :]
    ridges = np.cos(2 * np.pi * xs / ridge_period) * np.ones((h, 1))

    shade = 1 + 0.045 * mott + 0.05 * fib + 0.025 * fine - 0.07 * np.clip(-long_fib, 0, 1) \
        + 0.04 * np.clip(long_fib, 0, 1) - 0.35 * specks + 0.012 * ridges
    col = KRAFT_SRGB[None, None, :] * shade[..., None]
    col = col * (1 + 0.025 * hue[..., None] * np.array([1.0, 0.2, -1.0]))
    stains = (_stain_mask(shape, rng, (0.68 * w, 0.30 * h), 0.13 * w, ring=True),
              _stain_mask(shape, rng, (0.30 * w, 0.72 * h), 0.18 * w, ring=False))
    for s, depth in zip(stains, (0.08, 0.06)):
        col = col * (1 - depth * s[..., None] * np.array([0.9, 1.0, 1.15]))
    height = 0.6 * fib + 0.3 * fine + 0.5 * long_fib + 0.5 * ridges + 0.4 * mott - 0.5 * specks
    return np.clip(col, 0, 1), height


# --- corrugated edge ---------------------------------------------------------------------


def corrugated_edge(shape, rng: np.random.Generator, flutes: int):
    """Cross-section of single-wall board, tileable along u (x). Rows run front (v=0) -> back.

    Layers (fractions of the thickness): photo paper 0.05 | front liner 0.12 |
    flute zone 0.71 (sinusoidal medium with dark cavities) | back liner 0.12.
    Returns (colour sRGB float, height, fraction-of-thickness boundaries).
    """
    h, w = shape
    v = (np.arange(h)[:, None] + 0.5) / h
    u = (np.arange(w)[None, :] + 0.5) / w
    paper_end, liner1_end, liner2_start = 0.05, 0.17, 0.88
    zone_mid = 0.5 * (liner1_end + liner2_start)
    medium_t = 0.07
    amp = 0.5 * (liner2_start - liner1_end) - 0.5 * medium_t
    centre = zone_mid + amp * np.sin(2 * np.pi * flutes * u + 0.6 * np.sin(2 * np.pi * u))
    px = 1.0 / h  # one texel in v
    d_med = np.abs(v - centre) - 0.5 * medium_t
    in_medium = smoothstep(px, -px, d_med)
    in_zone = smoothstep(liner1_end - px, liner1_end + px, v) * smoothstep(liner2_start + px, liner2_start - px, v)
    cavity = in_zone * (1 - in_medium)
    # depth inside a flute tube: distance to the nearest wall, normalised
    wall = np.minimum(np.minimum(v - liner1_end, liner2_start - v), np.maximum(d_med, 0))
    depth = np.clip(wall / (0.5 * amp), 0, 1) * cavity

    streak = spectral_noise(shape, rng, beta=2.0, stretch=(6.0, 1.0))   # knife streaks along u
    fib = fibres(shape, rng, count=h * w // 12, length=(3, 12), angle_mean=0.0, angle_std=0.5, wrap=True)
    fine = spectral_noise(shape, rng, beta=1.0, f_high=0.4)

    board = KRAFT_SRGB[None, None] * (1 + 0.06 * fib[..., None] + 0.04 * streak[..., None])
    board = board * (1 - 0.10 * in_medium[..., None])  # medium paper is a touch darker
    paper = PAPER_SRGB[None, None] * (1 + 0.02 * fine[..., None])
    is_paper = smoothstep(paper_end + px, paper_end - px, v)
    col = board * (1 - is_paper[..., None]) + paper * is_paper[..., None]
    hole = KRAFT_DARK_SRGB[None, None] * (1 - 0.75 * depth[..., None] ** 0.7) * (1 + 0.05 * fine[..., None])
    col = col * (1 - cavity[..., None]) + hole * cavity[..., None]
    # thin glue/shadow lines where the medium meets the liners
    glue = np.exp(-(((v - liner1_end) / (1.2 * px)) ** 2)) + np.exp(-(((v - liner2_start) / (1.2 * px)) ** 2))
    col = col * (1 - 0.15 * glue[..., None])
    height = (1 - cavity) * 1.0 + cavity * (0.6 - 0.6 * depth) + 0.08 * fib + 0.05 * streak
    return np.clip(col, 0, 1), height, {"paper": paper_end, "front_liner": liner1_end, "back_liner": liner2_start}


# --- masking tape --------------------------------------------------------------------------


def _torn_profile(n: int, rng: np.random.Generator, depth: float) -> np.ndarray:
    """1-D jagged torn-edge offsets (px) of length n: zig-zag fibre tears on a slow wobble."""
    slow = np.cumsum(rng.normal(0, 1, n))
    slow = cv2.GaussianBlur((slow - np.linspace(slow[0], slow[-1], n)).astype(np.float32)[None], (0, 0), 6)[0]
    teeth = np.abs(rng.normal(0, 1, n)).astype(np.float32)
    teeth = cv2.GaussianBlur(teeth[None], (0, 0), 1.2)[0]
    p = 0.6 * normalise(slow) + 0.5 * normalise(teeth)
    return depth * (p - p.min()) / (np.ptp(p) + 1e-8)


def masking_tape(shape, rng: np.random.Generator):
    """Beige crinkled masking tape lying on kraft, torn at both short ends.
    The tape runs along x and fills the rect apart from a kraft margin of ~6 % around it.
    Returns (colour sRGB float, height, spec mask, tape coverage)."""
    h, w = shape
    back_col, back_h = kraft_back(shape, rng, ridge_period=max(8.0, h / 6))
    my, mx = int(round(0.10 * h)), int(round(0.06 * w))
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float64)
    left = mx + _torn_profile(h, rng, 0.05 * w)[:, None]
    right = w - mx - _torn_profile(h, rng, 0.05 * w)[:, None]
    wav = 1.5 * spectral_noise((1, w), rng, beta=3.0)[0][None, :]
    top, bottom = my + wav, h - my + wav[:, ::-1]
    cov = (smoothstep(left - 0.7, left + 0.7, xx) * smoothstep(right + 0.7, right - 0.7, xx)
           * smoothstep(top - 0.7, top + 0.7, yy) * smoothstep(bottom + 0.7, bottom - 0.7, yy))
    near_end = np.exp(-np.minimum(np.abs(xx - left), np.abs(xx - right)) / 4.0)

    crinkle = spectral_noise(shape, rng, beta=2.6, stretch=(1.0, 0.18))          # long across the width
    fine = spectral_noise(shape, rng, beta=1.2, f_high=0.35)
    creases = np.zeros(shape, np.uint8)  # sharp folds across the tape (LINE_AA needs 8-bit)
    for _ in range(7):
        x0 = rng.uniform(0.1 * w, 0.9 * w)
        dx = rng.normal(0, 0.12 * h)
        pts = np.array([[x0 - dx, top.min() - 2], [x0 + dx, bottom.max() + 2]]) * 16
        cv2.line(creases, tuple(pts[0].astype(int)), tuple(pts[1].astype(int)), int(rng.uniform(100, 255)), 1, cv2.LINE_AA, 4)
    creases = cv2.GaussianBlur(creases.astype(np.float32) / 255.0, (0, 0), 1.0)
    tint = 1 + 0.03 * spectral_noise(shape, rng, beta=3.0, f_high=0.03) + 0.015 * fine
    tape = TAPE_SRGB[None, None] * tint[..., None]
    tape = tape * (1 + 0.05 * crinkle[..., None] - 0.18 * creases[..., None])
    tape = 0.88 * tape + 0.12 * back_col * TAPE_SRGB / KRAFT_SRGB.mean()          # slightly translucent
    tape = tape * (1 + 0.06 * near_end[..., None])                                # paper fibres at tears
    shadow = cv2.GaussianBlur(cov.astype(np.float32), (0, 0), 2.5)
    under = back_col * (1 - 0.25 * np.clip(np.roll(np.roll(shadow, 2, 0), 2, 1) - cov, 0, 1)[..., None])
    col = under * (1 - cov[..., None]) + tape * cov[..., None]
    tape_h = 1.5 + 0.8 * crinkle - 1.2 * creases + 0.15 * fine + 0.6 * near_end
    height = back_h * 0.6 * (1 - cov) + tape_h * cov
    spec = SPEC["back"] * (1 - cov) + SPEC["tape"] * cov
    return np.clip(col, 0, 1), height, spec, cov


# --- twine ------------------------------------------------------------------------------


def twine(size: int, rng: np.random.Generator, plies: int = 3, twists: int = 4):
    """Tileable twisted jute string. u (x) runs along the string, v (y) around it.
    `twists` = ply stripes passed per tile; it must be a multiple of `plies` so each ply
    returns to the same place after one tile. Returns (colour sRGB float, height)."""
    if twists % plies:
        raise ValueError("twists must be a multiple of plies for a tileable twine texture")
    n = size
    u = (np.arange(n)[None, :] + 0.5) / n
    v = (np.arange(n)[:, None] + 0.5) / n
    phase = plies * v + twists * u
    t = phase - np.floor(phase)
    ply_id = np.mod(np.floor(phase), plies)
    bump = np.sin(np.pi * t) ** 0.55                               # rounded ply cross-section
    stripe_angle = np.arctan2(-twists, plies)                      # direction of constant phase (texels)
    along = spectral_noise((n, n), rng, beta=2.0, stretch=(0.15, 1.0), angle=stripe_angle + np.pi / 2)
    # yarn fibres run along each ply but twisted against the ply helix (as in real cord)
    fib = fibres((n, n), rng, count=n * n // 8, length=(8, 30), angle_mean=stripe_angle - 0.45,
                 angle_std=0.15, wrap=True)
    fuzz = fibres((n, n), rng, count=n * n // 220, length=(10, 40), angle_std=np.pi, curl=0.6, wrap=True)
    fine = spectral_noise((n, n), rng, beta=1.0, f_high=0.4)
    shade = (0.55 + 0.45 * bump) * (1 + 0.05 * (ply_id - 1)) * (1 + 0.07 * along + 0.16 * fib + 0.03 * fine)
    col = JUTE_SRGB[None, None] * shade[..., None]
    col = col + 0.10 * np.clip(fuzz, 0, 1)[..., None] * np.array([1.0, 0.95, 0.85])
    col = col * (1 + 0.04 * spectral_noise((n, n), rng, beta=3.0, f_high=0.02)[..., None] * np.array([1.0, 0.9, 0.7]))
    height = 3.0 * bump + 0.5 * fib + 0.2 * along + 0.25 * np.clip(fuzz, 0, 1)
    return np.clip(col, 0, 1), height


def to_srgb_u8(srgb: np.ndarray) -> np.ndarray:
    return np.clip(np.rint(srgb * 255), 0, 255).astype(np.uint8)


__all__ = [
    "KRAFT_SRGB", "SPEC", "height_to_normal", "encode_normal", "pull_push", "estimate_foreground",
    "print_look", "paper_height", "kraft_back", "corrugated_edge", "masking_tape", "twine", "to_srgb_u8",
    "linear_to_srgb", "srgb_to_linear",
]
