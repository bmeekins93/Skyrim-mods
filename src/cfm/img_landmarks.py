"""Eye/iris localisation for the stage 1 landmarks.

The iris is found as the circle with the strongest dark-inside / bright-outside step
along its visible arcs (a simplified Daugman integro-differential operator). Only
samples inside the parsed eye region count, so the upper lid, lashes and the dark
glasses frame (all outside the visible eye opening) cannot pull the centre around.

Coordinates follow docs/CONTRACTS.md: continuous pixels, x right, y down, pixel (i, j)
centred at (i + 0.5, j + 0.5).
"""
from __future__ import annotations

import cv2
import numpy as np

N_ANGLES = 96
MIN_VISIBLE = 0.35          # fraction of the circle that must lie inside the eye opening
RADIUS_RANGE = (0.15, 0.32)  # iris radius / eye-opening width (anatomically ~0.2-0.25)


def luminance(img: np.ndarray) -> np.ndarray:
    """Rec. 709 luma of an sRGB uint8 image (float64, 0..255)."""
    return img.astype(np.float64) @ np.array([0.2126, 0.7152, 0.0722])


def _sample(img: np.ndarray, cx: np.ndarray, cy: np.ndarray, r: float, interp: int) -> np.ndarray:
    """Values on circles of radius r around every centre (cx, cy): shape (len(cx), N_ANGLES)."""
    ang = np.linspace(0.0, 2.0 * np.pi, N_ANGLES, endpoint=False)
    # pixel (i, j) has its centre at (i + 0.5, j + 0.5); remap indexes pixel centres at integers
    mx = (cx[:, None] + r * np.cos(ang) - 0.5).astype(np.float32)
    my = (cy[:, None] + r * np.sin(ang) - 0.5).astype(np.float32)
    return cv2.remap(img, mx, my, interp, borderMode=cv2.BORDER_CONSTANT, borderValue=0)


def fit_iris_circle(lum: np.ndarray, eye: np.ndarray) -> tuple[float, float, float]:
    """Iris circle (cx, cy, r) inside the eye-opening mask `eye` (bool, full image size).

    Exhaustive search over integer-pixel centres inside the eye's bounding box and integer
    radii; the score is the mean luminance step across the circle (outside minus inside),
    measured only where both samples lie in the eye opening.
    """
    ys, xs = np.nonzero(eye)
    width = xs.max() - xs.min() + 1
    blur = cv2.GaussianBlur(lum.astype(np.float32), (0, 0), max(1.0, 0.01 * width))
    visible = cv2.dilate(eye.astype(np.uint8), np.ones((5, 5), np.uint8)).astype(np.float32)
    gx, gy = np.meshgrid(np.arange(xs.min(), xs.max() + 1), np.arange(ys.min(), ys.max() + 1))
    cx, cy = gx.ravel() + 0.5, gy.ravel() + 0.5
    best = (-np.inf, 0.0, 0.0, 0.0)
    for r in range(max(6, int(RADIUS_RANGE[0] * width)), int(RADIUS_RANGE[1] * width) + 1):
        d = max(1.5, 0.05 * r)
        outer = _sample(blur, cx, cy, r + d, cv2.INTER_LINEAR)
        inner = _sample(blur, cx, cy, r - d, cv2.INTER_LINEAR)
        ok = (_sample(visible, cx, cy, r + d, cv2.INTER_NEAREST) > 0.5) & \
             (_sample(visible, cx, cy, r - d, cv2.INTER_NEAREST) > 0.5)
        cnt = ok.sum(axis=1)
        score = np.where(cnt >= MIN_VISIBLE * N_ANGLES,
                         np.sum((outer - inner) * ok, axis=1) / np.maximum(cnt, 1), -np.inf)
        k = int(np.argmax(score))
        if score[k] > best[0]:
            best = (float(score[k]), float(cx[k]), float(cy[k]), float(r))
    if not np.isfinite(best[0]):
        raise RuntimeError("no iris circle fits inside the eye region")
    return best[1], best[2], best[3]


def radial_edge_distances(lum: np.ndarray, centre, radius: float, angles_deg) -> np.ndarray:
    """Distance from `centre` to the strongest dark->bright step along each ray.

    Rays are searched between 0.5 and 1.6 radii; angles in degrees, 0 = +x, 90 = +y (down).
    Used to check that a landmark sits in the middle of the iris (left/right symmetric).
    """
    blur = cv2.GaussianBlur(lum.astype(np.float32), (0, 0), 1.5)
    t = np.arange(0.5 * radius, 1.6 * radius, 0.5)
    out = []
    for a in np.radians(np.asarray(angles_deg, float)):
        px = (centre[0] + t * np.cos(a) - 0.5).astype(np.float32)[None]
        py = (centre[1] + t * np.sin(a) - 0.5).astype(np.float32)[None]
        prof = cv2.remap(blur, px, py, cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)[0]
        out.append(t[1:][int(np.argmax(np.diff(prof)))])
    return np.array(out)


def iris_symmetry(lum: np.ndarray, centre, radius: float) -> dict:
    """Edge distances on the lower-left vs lower-right diagonals (the lids hide the top).

    A correct iris centre has matching distances on both sides (|asymmetry| small compared
    with the radius); a centre pulled sideways by the glasses frame does not.
    """
    right = radial_edge_distances(lum, centre, radius, [10, 20, 30, 40, 50])
    left = radial_edge_distances(lum, centre, radius, [170, 160, 150, 140, 130])
    down = radial_edge_distances(lum, centre, radius, [70, 80, 90, 100, 110])
    return {"left_px": round(float(np.median(left)), 2), "right_px": round(float(np.median(right)), 2),
            "down_px": round(float(np.median(down)), 2),
            "asymmetry_over_radius": round(float((np.median(right) - np.median(left)) / (2 * radius)), 3)}
