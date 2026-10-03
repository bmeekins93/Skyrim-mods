"""Twine string around the back of the head (path + tube) and the tape strips that hold it.

Path, per side: the string end lies on the back face under a tape strip, runs along the back face
toward the mask edge, lifts off and goes taut to its tangent point on the head's clearance surface,
wraps around the back of the head on that surface and mirrors on the other side. The wrapped part
sags (string_sag) and is lopsided (string_asymmetry: one side rides higher); the corner where the
string leaves the cardboard is rounded with Chaikin subdivision.

Clearance surface (ClearanceProfile): at every height, the convex hull of the bald head(s) within
+-SLAB_HALF of that height, grown by STRING_HEAD_GAP + the string radius. The string therefore rests
just off the scalp/ears like a real string pulled round a head (hair hides or "compresses" it) and
stays under the shell of vanilla-style helmets, whose outer surface sits ~0.35 (over the ears), ~0.5
(temples) to ~1.2 units (back of the head) off the bald scalp (helmet-SMP.nif; 03_mesh.py gates it).
Only the head behind the outer orbital rim shapes the hull, so from there back the string lies along
the temple (as a helmet would press it) instead of running straight from the mask edge to the ear.
For the beast mesh the hull is taken over the union of the Argonian and Khajiit heads."""
from __future__ import annotations

import numpy as np
from shapely.geometry import MultiPoint, Point

from .geom_mesh import unit
from .geom_sheet import Bend

STRING_GAP = 0.004      # string centre sits radius + this behind the back face
STRING_HEAD_GAP = 0.15  # gap between the string surface and the bald head / ears (units)
SLAB_HALF = 0.3         # head cross-sections are taken over z +- this (> string radius)
LEVEL_STEP = 0.1        # height spacing of the precomputed cross-sections
THETA_STEP_DEG = 0.25   # polar sampling of each cross-section
TEMPLE_FROM_EYE_PIT = 1.0  # the string meets the side of the head this far in front of the eye-socket
                           # pit (y), at the outer orbital rim; in front of that it runs free to the mask
TAPE_GAP = 0.015        # tape surface offset from the back face (z-fighting)
TAPE_END_INSET = 0.06   # the string's cut end hides this far inside the tape's inner edge
SAG_DROP_UNITS = 2.5    # drop at the back of the head for string_sag = 1
ASYM_UNITS = 1.0        # half height difference between the sides for string_asymmetry = 1
UV_WRAP_TILES = 4       # twine u restarts every this many texture tiles (keeps half-float UVs exact)


def slab_points(v, t, z_lo: float, z_hi: float, y_max: float = np.inf) -> np.ndarray:
    """XY of the points bounding the mesh surface inside z_lo <= z <= z_hi, y <= y_max: the vertices
    inside that box plus the edge crossings of its planes (their hull is the clipped surface's hull
    up to box-edge corner points, which the clearance gates cover)."""
    v, t = np.asarray(v, float), np.asarray(t, int)
    inside = lambda q: (q[:, 2] >= z_lo - 1e-9) & (q[:, 2] <= z_hi + 1e-9) & (q[:, 1] <= y_max + 1e-9)  # noqa: E731
    pts = [v[inside(v), :2]]
    e = np.unique(np.sort(np.concatenate([t[:, [0, 1]], t[:, [1, 2]], t[:, [2, 0]]]), axis=1), axis=0)
    a, b = v[e[:, 0]], v[e[:, 1]]
    for axis, value in ((2, z_lo), (2, z_hi), (1, y_max)):
        if not np.isfinite(value):
            continue
        cross = (a[:, axis] - value) * (b[:, axis] - value) < 0
        f = (value - a[cross, axis]) / (b[cross, axis] - a[cross, axis])
        q = a[cross] + f[:, None] * (b[cross] - a[cross])
        pts.append(q[inside(q), :2])
    return np.concatenate(pts)


class ClearanceProfile:
    """Where the string centre may run: polar radius R(theta, z) about a vertical axis through
    `centre` of the convex hull of the heads' slab at height z, grown by `grow`. Only the head
    behind y_cut (the sides of the head from the outer orbital rim back) shapes the hull, so the
    string lies along the temples instead of being held off them by the brow and cheeks."""

    def __init__(self, heads, z_lo: float, z_hi: float, grow: float, centre, y_cut: float = np.inf):
        self.centre = np.asarray(centre, float)
        self.grow = grow
        self.y_cut = y_cut
        self.levels = np.arange(z_lo, z_hi + LEVEL_STEP, LEVEL_STEP)
        self.theta = np.radians(np.arange(-180.0, 180.0, THETA_STEP_DEG))
        self.rings, rad = [], []
        for z in self.levels:
            pts = np.concatenate([slab_points(v, t, z - SLAB_HALF, z + SLAB_HALF, y_cut) for v, t in heads])
            poly = MultiPoint([tuple(p) for p in pts]).convex_hull.buffer(grow, quad_segs=32)
            if not poly.contains(Point(*self.centre)):
                raise ValueError(f"profile centre {self.centre} outside the head section at z={z:.2f}")
            ring = np.array(poly.exterior.coords)[:-1]
            self.rings.append(ring)
            rad.append(self._polar(ring))
        self.radius_grid = np.array(rad)          # (levels, theta)

    def _polar(self, ring) -> np.ndarray:
        """Radius along every self.theta of a closed polygon that is star-shaped about the centre."""
        closed = np.vstack([ring, ring[:1]])
        seg = np.linalg.norm(np.diff(closed, axis=0), axis=1)
        dense = [closed[i] + np.linspace(0, 1, max(2, int(np.ceil(seg[i] / 0.02))), endpoint=False)[:, None]
                 * (closed[i + 1] - closed[i]) for i in range(len(ring))]
        d = np.concatenate(dense) - self.centre
        ang, r = np.arctan2(d[:, 1], d[:, 0]), np.hypot(d[:, 0], d[:, 1])
        order = np.argsort(ang)
        return np.interp(self.theta, ang[order], r[order], period=2 * np.pi)

    def level_index(self, z: float) -> int:
        return int(np.clip(np.round((z - self.levels[0]) / LEVEL_STEP), 0, len(self.levels) - 1))

    def ring(self, z: float) -> np.ndarray:
        return self.rings[self.level_index(z)]

    def radius(self, theta, z) -> np.ndarray:
        """R(theta, z), linear in both (theta wraps)."""
        theta, z = np.broadcast_arrays(np.asarray(theta, float), np.asarray(z, float))
        fz = np.clip((z - self.levels[0]) / LEVEL_STEP, 0, len(self.levels) - 1.000001)
        i0 = np.floor(fz).astype(int)
        w = fz - i0
        out = np.empty(theta.shape)
        for k, (th, i, wk) in enumerate(zip(theta.ravel(), i0.ravel(), w.ravel())):
            r0 = np.interp(th, self.theta, self.radius_grid[i], period=2 * np.pi)
            r1 = np.interp(th, self.theta, self.radius_grid[i + 1], period=2 * np.pi)
            out.flat[k] = (1 - wk) * r0 + wk * r1
        return out

    def point(self, theta, z) -> np.ndarray:
        r = self.radius(theta, z)
        return self.centre + r[..., None] * np.stack([np.cos(theta), np.sin(theta)], axis=-1)

    def temple_angle(self, z: float, side: float) -> float:
        """Polar angle where the profile at height z crosses y = y_cut on the +X (side 1) or -X side."""
        th = np.radians(np.arange(-90.0, 90.0, 0.05))
        if side < 0:
            th = np.pi - th                       # from the back (-90 deg) round the -X side
        y = self.point(th, np.full_like(th, z))[:, 1]
        i = int(np.argmax(y >= self.y_cut))
        if y[i] < self.y_cut:
            raise ValueError(f"profile never reaches y={self.y_cut} at z={z:.2f}")
        return float(np.arctan2(np.sin(th[i]), np.cos(th[i])))


def chaikin(p, iterations: int = 3) -> np.ndarray:
    """Corner-cutting subdivision of an open polyline, keeping both end points."""
    p = np.asarray(p, float)
    for _ in range(iterations):
        q = np.empty((2 * (len(p) - 1), p.shape[1]))
        q[0::2] = 0.75 * p[:-1] + 0.25 * p[1:]
        q[1::2] = 0.25 * p[:-1] + 0.75 * p[1:]
        p = np.vstack([p[:1], q, p[-1:]])
    return p


def adaptive_resample(p, max_len: float, max_turn_deg: float = 12.0) -> np.ndarray:
    """Keep a dense polyline's points every max_len of arc or every max_turn_deg of turning."""
    p = np.asarray(p, float)
    keep, acc_len, acc_turn = [0], 0.0, 0.0
    seg = np.diff(p, axis=0)
    seg_len = np.linalg.norm(seg, axis=1)
    dirs = unit(seg)
    for i in range(1, len(p) - 1):
        acc_len += seg_len[i - 1]
        acc_turn += np.degrees(np.arccos(np.clip(dirs[i - 1] @ dirs[i], -1, 1)))
        if acc_len >= max_len or acc_turn >= max_turn_deg:
            keep.append(i)
            acc_len, acc_turn = 0.0, 0.0
    keep.append(len(p) - 1)
    return p[keep]


def string_path(bend: Bend, attach: dict, profile: ClearanceProfile, r: float, sag: float,
                asym: float) -> tuple:
    """3D centre line of the twine (dense) and a summary of its construction.

    attach = {side(+1/-1): (a_end, a_lift)} in sheet units; side +1 rides asym * ASYM_UNITS above
    the eye line, side -1 as far below. Each side: run on the back face, lift off, straight span at
    constant height to the temple point (where the clearance profile crosses y_cut); between the two
    temple points the string follows the profile round the back of the head (over the ears) and
    sags by sag * SAG_DROP_UNITS."""
    h = {+1: asym * ASYM_UNITS, -1: -asym * ASYM_UNITS}
    drop = sag * SAG_DROP_UNITS
    on_back = -(r + STRING_GAP)
    face_runs, lifts, temple = {}, {}, {}
    for side, (a_end, a_lift) in attach.items():
        ah = np.stack([np.linspace(a_end, a_lift, 8), np.full(8, h[side])], axis=1)
        face_runs[side] = bend.lift(ah, on_back)
        lifts[side] = face_runs[side][-1]
        temple[side] = profile.temple_angle(lifts[side][2], side)
    # clockwise seen from above (decreasing theta) from the +X temple through the back (-Y) to -X
    th0 = temple[+1]
    th1 = th0 - (th0 - temple[-1]) % (2 * np.pi)
    if not th1 < -np.pi / 2 < th0:
        raise ValueError(f"string temple points {np.degrees([th0, th1])} do not straddle the back of the head")
    theta = np.linspace(th0, th1, max(64, int(np.degrees(th0 - th1) / 1.0)))
    z0, z1 = lifts[+1][2], lifts[-1][2]
    phi = (th0 - theta) / (th0 - th1)
    for _ in range(3):                         # z depends on arc length, arc length (mildly) on z
        z = z0 + (z1 - z0) * phi - drop * np.sin(np.pi * phi) ** 2
        xy = profile.point(theta, z)
        arc = np.concatenate([[0.0], np.cumsum(np.linalg.norm(np.diff(xy, axis=0), axis=1))])
        phi = arc / arc[-1]
    z = z0 + (z1 - z0) * phi - drop * np.sin(np.pi * phi) ** 2
    xy = profile.point(theta, z)
    wrap = np.column_stack([xy, z])
    spans = {s_: _segment(lifts[s_], end, 0.25) for s_, end in ((+1, wrap[0]), (-1, wrap[-1]))}
    path = np.vstack([face_runs[+1][:-1], spans[+1][:-1], wrap, spans[-1][::-1][1:], face_runs[-1][::-1][1:]])
    info = {"temple_theta_deg": [round(float(np.degrees(th0)), 2), round(float(np.degrees(th1)), 2)],
            "temple_points": [wrap[0].round(3).tolist(), wrap[-1].round(3).tolist()],
            "y_cut": round(float(profile.y_cut), 4), "wrap_length": round(float(arc[-1]), 3),
            "back_drop": round(float(0.5 * (z0 + z1) - z.min()), 3),
            "side_heights": [round(float(z0), 3), round(float(z1), 3)]}
    return chaikin(path, 3), info


def _segment(a, b, step: float) -> np.ndarray:
    """Points from a to b (inclusive) at most `step` apart."""
    n = max(2, int(np.ceil(np.linalg.norm(np.asarray(b) - np.asarray(a)) / step)) + 1)
    return np.asarray(a) + np.linspace(0, 1, n)[:, None] * (np.asarray(b) - np.asarray(a))


def _frames(p):
    """Parallel-transport frames (n1, n2) along a polyline, plus unit tangents."""
    t = unit(np.gradient(p, axis=0))
    n1 = unit(np.cross(t[0], [0.0, 0.0, 1.0] if abs(t[0][2]) < 0.9 else [1.0, 0.0, 0.0]))
    frames = []
    for k in range(len(p)):
        n1 = unit(n1 - (n1 @ t[k]) * t[k])
        frames.append((n1, np.cross(t[k], n1)))
    return frames, t


def tube(path, radius: float, sides: int, tile_length: float):
    """Closed-ended tube along a polyline: parallel-transport frames, seam column duplicated.

    UVs: u = arc length / tile_length, v = around the circumference 0..1. Once u passes
    UV_WRAP_TILES it restarts: that ring is duplicated, the copy's u reduced by a whole number of
    tiles (seamless with wrap addressing). u then stays below UV_WRAP_TILES + one ring spacing (< 8
    for the 0.6-unit rings and 0.6-unit tiles), where BSTriShape's half-float UVs step by 2^-8, so
    the stored u is within 0.002 of the authored one."""
    p = np.asarray(path, float)
    s = np.concatenate([[0.0], np.cumsum(np.linalg.norm(np.diff(p, axis=0), axis=1))])
    frames, t = _frames(p)
    phi = np.linspace(0, 2 * np.pi, sides + 1)
    ring = sides + 1
    # ring records (path index, u); a wrap ring appears twice: end of one run, start of the next
    records, u_base = [], 0.0
    for k in range(len(p)):
        u = s[k] / tile_length - u_base
        records.append((k, u))
        if u >= UV_WRAP_TILES and k < len(p) - 1:
            u_base += np.floor(u)
            records.append((k, s[k] / tile_length - u_base))
    verts, norms, uvs, tris = [], [], [], []
    for j_rec, (k, u) in enumerate(records):
        a, b = frames[k]
        for j, f in enumerate(phi):
            d = np.cos(f) * a + np.sin(f) * b
            verts.append(p[k] + radius * d)
            norms.append(d)
            uvs.append((u, j / sides))
        if j_rec and records[j_rec - 1][0] != k:      # connect to the previous ring (not its twin)
            for j in range(sides):
                a0, a1 = (j_rec - 1) * ring + j, (j_rec - 1) * ring + j + 1
                b0, b1 = a0 + ring, a1 + ring
                tris += [(a0, b0, b1), (a0, b1, a1)]
    last = len(records) - 1
    for rec, k, sgn in ((0, 0, -1.0), (last, len(p) - 1, 1.0)):    # end caps (hard edge, own vertices)
        c = len(verts)
        u = records[rec][1]
        verts.append(p[k])
        norms.append(sgn * t[k])
        uvs.append((u, 0.5))
        for j in range(sides):
            verts.append(verts[rec * ring + j])
            norms.append(sgn * t[k])
            uvs.append((u, 0.5 + 0.5 * np.cos(phi[j])))
        for j in range(sides):
            tris.append((c, c + 1 + j, c + 1 + (j + 1) % sides))
    return np.array(verts), np.array(tris), np.array(uvs), np.array(norms), float(s[-1])


def tape_strip(bend: Bend, a_end: float, a_dir: float, h_s: float, r: float, length: float,
               width: float, angle_deg: float, uv_rect):
    """A strip of tape across the string end on the back face, draped over the string.

    The long axis runs roughly along h (across the string, tilted by angle_deg); the surface sits
    TAPE_GAP behind the back face and bulges by the string's diameter along the string, which runs
    under the whole strip (its cut end sits just inside the strip's inner edge)."""
    ang = np.radians(angle_deg)
    long_ax = np.array([np.sin(ang), np.cos(ang)])
    wide_ax = np.array([np.cos(ang), -np.sin(ang)])
    centre = np.array([a_end + a_dir * (0.5 * width - TAPE_END_INSET), h_s])
    rows = np.array(sorted({-0.5, -0.3, 0.3, 0.5} | {s * f * r / length for s in (-1, 1)
                                                      for f in (0.0, 0.5, 1.0, 2.0, 3.5)}))
    cols = np.linspace(-0.5, 0.5, 5)
    ah, uv = [], []
    for tl in rows:
        for tw in cols:
            ah.append(centre + tl * length * long_ax + tw * width * wide_ax)
            u0, v0, u1, v1 = uv_rect
            uv.append((u0 + (tl + 0.5) * (u1 - u0), v0 + (tw + 0.5) * (v1 - v0)))
    ah = np.array(ah)
    d = np.abs(ah[:, 1] - h_s)
    hump = np.where(d <= 0.8 * r, 1.0, np.where(d >= 3.5 * r, 0.0,
                                                0.5 + 0.5 * np.cos(np.pi * (d - 0.8 * r) / (2.7 * r))))
    off = -(TAPE_GAP + hump * (2 * r + STRING_GAP))
    verts = np.vstack([bend.lift(ah[i:i + 1], float(off[i])) for i in range(len(ah))])
    nc = len(cols)
    tris = []
    for i in range(len(rows) - 1):
        for j in range(nc - 1):
            a0, a1 = i * nc + j, i * nc + j + 1
            b0, b1 = a0 + nc, a1 + nc
            tris += [(a0, b0, b1), (a0, b1, a1)]
    return verts, np.array(tris), np.array(uv)
