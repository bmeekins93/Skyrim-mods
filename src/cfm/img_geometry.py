"""Mask clean-up, outline extraction and polygon utilities for stage 1.

Coordinate convention (see docs/CONTRACTS.md, stage 1): continuous source-image
pixel coordinates, x right, y down, the image covers [0, W] x [0, H] and pixel
(i, j) has its centre at (i + 0.5, j + 0.5).
"""
from __future__ import annotations

import cv2
import numpy as np
from scipy import ndimage
from shapely.geometry import LineString, Point, Polygon


def disk(radius: int) -> np.ndarray:
    r = int(max(1, radius))
    return cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * r + 1, 2 * r + 1))


def largest_component(mask: np.ndarray) -> np.ndarray:
    n, lab, stats, _ = cv2.connectedComponentsWithStats(mask.astype(np.uint8), connectivity=8)
    if n <= 1:
        return mask.astype(bool)
    best = 1 + int(np.argmax(stats[1:, cv2.CC_STAT_AREA]))
    return lab == best


def fill_holes(mask: np.ndarray) -> np.ndarray:
    return ndimage.binary_fill_holes(mask.astype(bool))


def components_by_area(mask: np.ndarray, count: int, min_area: int = 1):
    """The `count` largest 8-connected components as boolean masks (largest first)."""
    n, lab, stats, _ = cv2.connectedComponentsWithStats(mask.astype(np.uint8), connectivity=8)
    order = np.argsort(-stats[1:, cv2.CC_STAT_AREA])[:count] + 1
    return [lab == i for i in order if stats[i, cv2.CC_STAT_AREA] >= min_area]


def convex_hull_mask(mask: np.ndarray) -> np.ndarray:
    pts = cv2.findNonZero(mask.astype(np.uint8))
    out = np.zeros(mask.shape, np.uint8)
    if pts is not None:
        cv2.fillConvexPoly(out, cv2.convexHull(pts), 1)
    return out.astype(bool)


def smooth_cut_mask(mask: np.ndarray, open_r: int, close_r: int, blur_sigma: float) -> np.ndarray:
    """Turn a raw segmentation into a 'scissor cut' region.

    Opening removes strand-level specks, closing bridges narrow notches a pair of
    scissors would never follow, and a Gaussian blur + 0.5 threshold rounds off the
    pixel stair-steps. Holes are filled and only the largest component is kept.
    """
    m = fill_holes(largest_component(mask)).astype(np.uint8)
    pad = close_r + int(4 * blur_sigma) + 4  # avoid border effects of closing / blur
    m = cv2.copyMakeBorder(m, pad, pad, pad, pad, cv2.BORDER_CONSTANT, value=0)
    if open_r > 0:
        m = cv2.morphologyEx(m, cv2.MORPH_OPEN, disk(open_r))
    if close_r > 0:
        m = cv2.morphologyEx(m, cv2.MORPH_CLOSE, disk(close_r))
    if blur_sigma > 0:
        m = (cv2.GaussianBlur(m.astype(np.float32), (0, 0), blur_sigma) > 0.5).astype(np.uint8)
    m = m[pad:-pad, pad:-pad]
    return fill_holes(largest_component(m))


def signed_area(poly: np.ndarray) -> float:
    """Shoelace area on raw (x, y) coordinates; positive = the contract's orientation."""
    x, y = poly[:, 0], poly[:, 1]
    return 0.5 * float(np.sum(x * np.roll(y, -1) - np.roll(x, -1) * y))


def _dense_contour(mask: np.ndarray, step: float, smooth_sigma: float) -> np.ndarray:
    """Outer contour, resampled every `step` px and Gaussian-smoothed (periodic)."""
    cnts, _ = cv2.findContours(mask.astype(np.uint8), cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
    c = max(cnts, key=cv2.contourArea)[:, 0, :].astype(np.float64) + 0.5  # pixel centres
    seg = np.linalg.norm(np.diff(np.vstack([c, c[:1]]), axis=0), axis=1)
    s = np.concatenate([[0.0], np.cumsum(seg)])
    n = max(16, int(s[-1] / step))
    t = np.linspace(0.0, s[-1], n, endpoint=False)
    closed = np.vstack([c, c[:1]])
    pts = np.stack([np.interp(t, s, closed[:, 0]), np.interp(t, s, closed[:, 1])], axis=1)
    if smooth_sigma > 0:
        pts = ndimage.gaussian_filter1d(pts, smooth_sigma / step, axis=0, mode="wrap")
    if signed_area(pts) < 0:
        pts = pts[::-1]
    return pts


def _dp_indices(dense: np.ndarray, eps: float) -> np.ndarray:
    approx = cv2.approxPolyDP(dense.astype(np.float32).reshape(-1, 1, 2), eps, True)[:, 0, :]
    # map back to dense indices (approxPolyDP returns a subset of the input points)
    tree = {tuple(np.round(p, 3)): i for i, p in enumerate(dense.astype(np.float32))}
    idx = sorted(tree[tuple(np.round(p, 3))] for p in approx)
    return np.array(idx, dtype=int)


def _limit_segment_length(dense: np.ndarray, idx: np.ndarray, max_len: float) -> np.ndarray:
    """Insert dense-contour points so no polygon edge spans more than max_len px of outline."""
    n = len(dense)
    out = []
    for a, b in zip(idx, np.roll(idx, -1)):
        out.append(a)
        span = (b - a) % n
        if span == 0:
            continue
        seglen = np.linalg.norm(dense[b] - dense[a])
        k = int(np.ceil(seglen / max_len))
        for j in range(1, k):
            out.append((a + int(round(j * span / k))) % n)
    return np.array(out, dtype=int)


def mask_to_polygon(mask: np.ndarray, min_points: int = 120, max_points: int = 400,
                    target_points: int = 260, smooth_sigma: float = 3.0,
                    max_edge_frac: float = 1.0 / 60) -> np.ndarray:
    """Smooth, simplified outline polygon (N x 2, positive shoelace area, not closed)."""
    dense = _dense_contour(mask, step=1.0, smooth_sigma=smooth_sigma)
    perimeter = float(np.sum(np.linalg.norm(np.diff(np.vstack([dense, dense[:1]]), axis=0), axis=1)))
    max_edge = perimeter * max_edge_frac
    lo, hi = 0.05, 20.0
    best = None
    for _ in range(40):  # bisection on the Douglas-Peucker tolerance
        eps = 0.5 * (lo + hi)
        idx = _limit_segment_length(dense, _dp_indices(dense, eps), max_edge)
        if len(idx) > target_points:
            lo = eps
        else:
            hi = eps
            best = idx
        if abs(len(idx) - target_points) <= 4 and min_points <= len(idx) <= max_points:
            best = idx
            break
    if best is None or not (min_points <= len(best) <= max_points):
        raise RuntimeError("could not simplify outline into the requested point range")
    poly = dense[np.unique(best)]
    if signed_area(poly) < 0:
        poly = poly[::-1]
    return poly


def polygon_report(poly: np.ndarray) -> dict:
    p = Polygon(poly)
    edges = np.linalg.norm(np.diff(np.vstack([poly, poly[:1]]), axis=0), axis=1)
    v1 = np.roll(poly, 1, axis=0) - poly
    v2 = np.roll(poly, -1, axis=0) - poly
    cosang = np.sum(v1 * v2, axis=1) / (np.linalg.norm(v1, axis=1) * np.linalg.norm(v2, axis=1) + 1e-12)
    interior = np.degrees(np.arccos(np.clip(cosang, -1, 1)))
    return {
        "points": int(len(poly)),
        "valid": bool(p.is_valid),
        "simple": bool(LineString(np.vstack([poly, poly[:1]])).is_simple),
        "signed_area_px": round(signed_area(poly), 1),
        "perimeter_px": round(float(p.length), 1),
        "bbox": [round(float(v), 2) for v in p.bounds],
        "edge_len_px": [round(float(edges.min()), 2), round(float(edges.max()), 2)],
        "min_vertex_angle_deg": round(float(interior.min()), 1),
    }


def ray_exit_point(poly: np.ndarray, origin, direction) -> np.ndarray:
    """Farthest intersection of the ray origin + t*direction (t > 0) with the polygon boundary."""
    d = np.asarray(direction, float)
    d /= np.linalg.norm(d)
    o = np.asarray(origin, float)
    far = o + d * 1e5
    inter = LineString([tuple(o), tuple(far)]).intersection(Polygon(poly).exterior)
    pts = [np.array(g.coords[0]) for g in getattr(inter, "geoms", [inter]) if not g.is_empty]
    if not pts:
        raise RuntimeError("ray does not leave the polygon")
    return max(pts, key=lambda p: float(np.dot(p - o, d)))


def point_in_polygon(poly: np.ndarray, pt) -> bool:
    return bool(Polygon(poly).contains(Point(float(pt[0]), float(pt[1]))))
