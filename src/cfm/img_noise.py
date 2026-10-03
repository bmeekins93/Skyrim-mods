"""Seeded procedural noise building blocks for stage 2 (all outputs are deterministic).

Spectral (FFT) noise is periodic by construction, so every texture built from it
tiles seamlessly; `fibres` can also wrap so tileable textures stay tileable.
"""
from __future__ import annotations

import cv2
import numpy as np


def normalise(x: np.ndarray) -> np.ndarray:
    """Zero mean, unit standard deviation."""
    x = x - x.mean()
    s = x.std()
    return x / s if s > 0 else x


def spectral_noise(shape, rng: np.random.Generator, beta: float = 2.0, stretch=(1.0, 1.0),
                   angle: float = 0.0, f_low: float = 0.0, f_high: float | None = None) -> np.ndarray:
    """Periodic 1/f^beta noise (zero mean, unit std).

    stretch=(sx, sy) multiplies the frequencies along the (rotated) x / y axes, so a
    large sx makes features long along x. f_low suppresses frequencies below f_low
    (cycles/pixel), f_high applies a Gaussian roll-off above f_high.
    """
    h, w = shape
    spec = np.fft.rfft2(rng.standard_normal((h, w)))
    fy = np.fft.fftfreq(h)[:, None]
    fx = np.fft.rfftfreq(w)[None, :]
    c, s = np.cos(angle), np.sin(angle)
    u = (c * fx + s * fy) * stretch[0]
    v = (-s * fx + c * fy) * stretch[1]
    f = np.sqrt(u * u + v * v)
    f[0, 0] = 1.0
    amp = f ** (-beta / 2.0)
    amp[0, 0] = 0.0
    if f_low > 0:
        amp *= 1.0 - np.exp(-((f / f_low) ** 2))
    if f_high:
        amp *= np.exp(-((f / f_high) ** 2))
    return normalise(np.fft.irfft2(spec * amp, s=(h, w)))


def fold_wrap(canvas: np.ndarray, margin: int) -> np.ndarray:
    """Fold a canvas drawn with `margin` px overhang back onto itself (periodic tiling)."""
    m = margin
    h, w = canvas.shape[0] - 2 * m, canvas.shape[1] - 2 * m
    out = canvas[m:m + h, m:m + w].copy()
    out[:m] += canvas[m + h:, m:m + w]
    out[-m:] += canvas[:m, m:m + w]
    out[:, :m] += canvas[m:m + h, m + w:]
    out[:, -m:] += canvas[m:m + h, :m]
    out[:m, :m] += canvas[m + h:, m + w:]
    out[-m:, -m:] += canvas[:m, :m]
    out[:m, -m:] += canvas[m + h:, :m]
    out[-m:, :m] += canvas[:m, m + w:]
    return out


def fibres(shape, rng: np.random.Generator, count: int, length=(6, 30), angle_mean: float = 0.0,
           angle_std: float = np.pi, curl: float = 0.25, wrap: bool = False, supersample: int = 2) -> np.ndarray:
    """Thin anti-aliased curved strands with random signed intensity in [-1, 1].

    Drawn at `supersample`x resolution and area-downsampled so 1 px strands stay soft.
    With wrap=True the result tiles seamlessly.
    """
    h, w = shape
    ss = supersample
    m = int(length[1]) + 4 if wrap else 0
    canvas = np.zeros(((h + 2 * m) * ss, (w + 2 * m) * ss), np.float32)
    starts = rng.uniform([0, 0], [w, h], size=(count, 2)) + m
    lens = rng.uniform(length[0], length[1], count)
    angs = angle_mean + rng.normal(0.0, 1.0, count) * angle_std
    bends = rng.normal(0.0, curl, count)
    vals = rng.uniform(-1.0, 1.0, count)
    # Draw strands grouped by quantised intensity so cv2 can batch polylines.
    levels = np.round(vals * 8) / 8
    for lv in np.unique(levels):
        pts = []
        for i in np.nonzero(levels == lv)[0]:
            t = np.linspace(-0.5, 0.5, 5)[:, None]
            d = np.array([np.cos(angs[i]), np.sin(angs[i])])
            nrm = np.array([-d[1], d[0]])
            p = starts[i] + t * lens[i] * d + (t ** 2 - 0.25) * bends[i] * lens[i] * nrm
            pts.append(np.round(p * ss * 16).astype(np.int32))
        tmp = np.zeros(canvas.shape, np.uint8)  # LINE_AA needs an 8-bit canvas
        cv2.polylines(tmp, pts, False, 255, max(1, ss // 2), cv2.LINE_AA, 4)
        canvas += float(lv) / 255.0 * tmp
    canvas = cv2.resize(canvas, (w + 2 * m, h + 2 * m), interpolation=cv2.INTER_AREA)
    return fold_wrap(canvas, m) if wrap else canvas


def smoothstep(e0: float, e1: float, x: np.ndarray) -> np.ndarray:
    t = np.clip((x - e0) / (e1 - e0), 0.0, 1.0)
    return t * t * (3 - 2 * t)
