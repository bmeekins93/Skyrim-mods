"""2D cut-out sheet: photo outline -> wobbly, tile-aligned boundary + interior points ->
conforming Delaunay triangulation, and the cylindrical bend that lifts it into model space.

Sheet coordinates (a, h) are model units in the flat sheet: a along model X (image x -> -X),
h along model Z (image y -> -Z), both relative to the photo's eye midpoint."""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from scipy import ndimage
from scipy.spatial import Delaunay
from shapely.geometry import Point, Polygon
from shapely.prepared import prep


def signed_area(poly) -> float:
    p = np.asarray(poly, float)
    return 0.5 * float(np.sum(p[:, 0] * np.roll(p[:, 1], -1) - np.roll(p[:, 0], -1) * p[:, 1]))


def ccw(poly) -> np.ndarray:
    """Return the polygon counter-clockwise in its own (a, h) axes, whatever it came in as."""
    p = np.asarray(poly, float)
    return p if signed_area(p) > 0 else p[::-1].copy()


def resample_closed(poly, n: int) -> np.ndarray:
    """n points at equal arc length along a closed polygon, starting at its first vertex."""
    p = np.asarray(poly, float)
    seg = np.linalg.norm(np.roll(p, -1, axis=0) - p, axis=1)
    cum = np.concatenate([[0.0], np.cumsum(seg)])
    s = np.linspace(0, cum[-1], n, endpoint=False)
    closed = np.vstack([p, p[:1]])
    return np.stack([np.interp(s, cum, closed[:, 0]), np.interp(s, cum, closed[:, 1])], axis=1)


def perimeter(poly) -> float:
    p = np.asarray(poly, float)
    return float(np.sum(np.linalg.norm(np.roll(p, -1, axis=0) - p, axis=1)))


def outward_normals(poly) -> np.ndarray:
    """Per-vertex outward unit normals of a CCW polygon (average of the two adjacent edges)."""
    p = np.asarray(poly, float)
    e = np.roll(p, -1, axis=0) - p
    en = np.stack([e[:, 1], -e[:, 0]], axis=1)          # right-hand normal = outward for CCW
    en /= np.linalg.norm(en, axis=1, keepdims=True)
    n = en + np.roll(en, 1, axis=0)
    return n / np.linalg.norm(n, axis=1, keepdims=True)


def wobble(poly, amplitude: float, rng: np.random.Generator, corr_samples: float = 2.5) -> np.ndarray:
    """Hand-cut jitter: smooth seeded noise along the outward normal, max |offset| = amplitude."""
    if amplitude <= 0:
        return np.asarray(poly, float).copy()
    noise = ndimage.gaussian_filter1d(rng.standard_normal(len(poly)), corr_samples, mode="wrap")
    noise = noise / np.max(np.abs(noise)) * amplitude
    return np.asarray(poly, float) + outward_normals(poly) * noise[:, None]


@dataclass
class Boundary:
    pts: np.ndarray          # (n, 2) CCW sheet points (after wobble)
    per_tile: int            # samples per edge-texture tile
    n_tiles: int
    tile_length: float       # effective (perimeter / n_tiles) world units

    def tile_frac(self, param) -> np.ndarray:
        """Position inside the current edge tile (0..1) for boundary params in sample units."""
        return (np.asarray(param, float) % self.per_tile) / self.per_tile

    def is_seam(self, param) -> np.ndarray:
        """True where a new edge tile starts (u jumps from u1 back to u0)."""
        return np.isclose(np.asarray(param, float) % self.per_tile, 0.0)


def make_boundary(poly_sheet, spacing: float, tile_length: float, amplitude: float,
                  rng: np.random.Generator) -> Boundary:
    """Resample so that edge-texture tiles hold a whole number of samples, then wobble."""
    p = ccw(poly_sheet)
    length = perimeter(p)
    n_tiles = max(1, int(round(length / tile_length)))
    per_tile = max(2, int(np.ceil(length / n_tiles / spacing)))
    pts = wobble(resample_closed(p, n_tiles * per_tile), amplitude, rng)
    return Boundary(pts, per_tile, n_tiles, perimeter(pts) / n_tiles)


def interior_points(boundary_pts, spacing: float, rng: np.random.Generator) -> np.ndarray:
    """Hexagonal grid inside the outline, at least spacing/2 from it, with tiny seeded jitter."""
    poly = Polygon(boundary_pts)
    inner = poly.buffer(-0.5 * spacing)
    if inner.is_empty:
        return np.zeros((0, 2))
    x0, y0, x1, y1 = inner.bounds
    dy = spacing * np.sqrt(3) / 2
    pts = []
    for j, y in enumerate(np.arange(y0, y1 + dy, dy)):
        off = 0.5 * spacing * (j % 2)
        for x in np.arange(x0 + off, x1 + spacing, spacing):
            pts.append((x, y))
    pts = np.array(pts) + rng.uniform(-0.04, 0.04, (len(pts), 2)) * spacing
    test = prep(inner)
    return np.array([q for q in pts if test.contains(Point(q))]).reshape(-1, 2)


def conforming_triangulation(boundary: np.ndarray, interior: np.ndarray, max_rounds: int = 8):
    """Delaunay of boundary + interior points keeping only triangles inside the outline.

    Boundary edges missing from the Delaunay are split at their midpoint (conforming Delaunay)
    until every outline edge is present. Returns (points, tris, n_boundary, param): the first
    n_boundary points are the CCW boundary loop and param[k] is each loop point's position along
    the input boundary in sample units (k.5 = midpoint inserted between samples k and k+1)."""
    n0 = len(boundary)
    pos, param = np.asarray(boundary, float).copy(), np.arange(n0, dtype=float)
    for _ in range(max_rounds):
        pts = np.vstack([pos, interior])
        tri = Delaunay(pts).simplices
        tri = tri[points_in_polygon(pts[tri].mean(axis=1), pos)]
        edges = {tuple(sorted(e)) for t in tri for e in ((t[0], t[1]), (t[1], t[2]), (t[2], t[0]))}
        n = len(pos)
        missing = {k for k in range(n) if tuple(sorted((k, (k + 1) % n))) not in edges}
        if not missing:
            return pts, tri, n, param
        new_pos, new_param = [], []
        for k in range(n):
            new_pos.append(pos[k])
            new_param.append(param[k])
            if k in missing:
                k2 = (k + 1) % n
                nxt = param[k2] if k2 else float(n0)
                new_pos.append(0.5 * (pos[k] + pos[k2]))
                new_param.append(0.5 * (param[k] + nxt))
        pos, param = np.array(new_pos), np.array(new_param)
    raise RuntimeError("conforming triangulation did not converge")


def points_in_polygon(q, poly) -> np.ndarray:
    """Even-odd point-in-polygon, vectorised over points."""
    q = np.asarray(q, float)
    p = np.asarray(poly, float)
    x, y = q[:, 0][:, None], q[:, 1][:, None]
    x0, y0 = p[:, 0][None], p[:, 1][None]
    x1, y1 = np.roll(p[:, 0], -1)[None], np.roll(p[:, 1], -1)[None]
    cond = (y0 > y) != (y1 > y)
    with np.errstate(divide="ignore", invalid="ignore"):
        xin = x0 + (y - y0) * (x1 - x0) / (y1 - y0)
    return np.sum(cond & (x < xin), axis=1) % 2 == 1


@dataclass
class Bend:
    """Cylindrical bend about a vertical axis behind the sheet (radius 0 = flat).

    The back face at a = 0 sits at y = y_back0; arc length is measured on the mid surface."""
    radius_back: float
    thickness: float
    y_back0: float
    z_eye: float

    def lift(self, ah, face: str) -> np.ndarray:
        """Sheet (a, h) -> model XYZ on the 'front' or 'back' face (or a float offset from the back
        face toward the front, used for things lying on/above the back face)."""
        ah = np.asarray(ah, float)
        a, h = ah[:, 0], ah[:, 1]
        off = {"front": self.thickness, "back": 0.0}.get(face, face) if isinstance(face, str) else face
        if self.radius_back <= 0:
            return np.stack([a, np.full_like(a, self.y_back0 + off), self.z_eye + h], axis=1)
        rm = self.radius_back + 0.5 * self.thickness
        rho = self.radius_back + off
        th = a / rm
        y_axis = self.y_back0 - self.radius_back
        return np.stack([rho * np.sin(th), y_axis + rho * np.cos(th), self.z_eye + h], axis=1)

    def radial(self, ah) -> np.ndarray:
        """Unit outward (front-facing) normal of the sheet at (a, h)."""
        a = np.asarray(ah, float)[:, 0]
        if self.radius_back <= 0:
            return np.tile([0.0, 1.0, 0.0], (len(a), 1))
        th = a / (self.radius_back + 0.5 * self.thickness)
        return np.stack([np.sin(th), np.cos(th), np.zeros_like(th)], axis=1)

    def tangent_a(self, ah) -> np.ndarray:
        """Unit model-space direction of increasing a."""
        a = np.asarray(ah, float)[:, 0]
        if self.radius_back <= 0:
            return np.tile([1.0, 0.0, 0.0], (len(a), 1))
        th = a / (self.radius_back + 0.5 * self.thickness)
        return np.stack([np.cos(th), -np.sin(th), np.zeros_like(th)], axis=1)

    def to_sheet(self, xyz):
        """Inverse map: model XYZ -> (a, h, offset from the back face along the radius)."""
        p = np.asarray(xyz, float)
        if self.radius_back <= 0:
            return np.stack([p[:, 0], p[:, 2] - self.z_eye, p[:, 1] - self.y_back0], axis=1)
        y_axis = self.y_back0 - self.radius_back
        rho = np.hypot(p[:, 0], p[:, 1] - y_axis)
        th = np.arctan2(p[:, 0], p[:, 1] - y_axis)
        rm = self.radius_back + 0.5 * self.thickness
        return np.stack([th * rm, p[:, 2] - self.z_eye, rho - self.radius_back], axis=1)
