"""Twine string around the back of the head (path + tube) and the tape strips that hold it.

The look is a cheap party-mask string riding awkwardly over the hair (string_path):
  * each end lies on the back face under a tape strip, runs along it toward the mask edge and lifts
    off; the +X end sits string_asymmetry * ASYM_UNITS above the eye line, the -X end as far below;
  * a straight, taut span runs from the lift point to its tangent point on the clearance surface
    (for humans just over the ear). The +X contact rides string_asymmetry * 2 * ASYM_SIDE_UNITS
    higher than the -X one (but stays CROWN_MARGIN under the crown: no headband); where the kind has
    ears beside the head both are raised until the built tube clears the ear tops by EAR_TOP_GAP;
  * the wrapped part follows the clearance surface round the back of the head and droops
    string_sag * SAG_DROP_UNITS below the mean contact height, its lowest point
    string_asymmetry * SAG_OFFSET_DEG off-centre toward the lower (-X) side. Cosine easing keeps
    it level at the contacts and at the bottom (no kinks in the drop);
  * the whole centre line is smoothed along its arc length (SMOOTH_SIGMA): the bends over the ears
    and at the mask edge are rounded like a real string's.

Clearance surface (ClearanceProfile): at every height, the convex hull of the bald head(s) within
+-SLAB_HALF of that height, grown by the clearance plus the string radius. The clearance is
c = string_clearance_units * CLEARANCE_FACTOR[kind] over the sides and the ears; toward the back
centre it swells smoothly (cos^2 over +-BACK_HALF_WIDTH_DEG) to c * (1 + BACK_EXTRA[kind]),
because hair hangs thickest over the back of the head - that is where the droop has to show over
it. In front of the head centre it tapers (smoothstep in y) to STRING_HEAD_GAP + radius at the outer
orbital rim (temples, where the string leaves the head toward the mask); only the head behind the
orbital rim shapes the hull. The string pokes out of close-fitting helmets and hoods by design
(03_mesh.py reports helmet-SMP.nif and a hood, and gates it against a reference hair mesh).
For the beast mesh the hull is taken over the union of the Argonian and Khajiit heads; the string
must clear the larger of the two everywhere, so the beast clearance factor is smaller (on the
Argonian it rides the Khajiit's extra 1-1.6 units higher at the back)."""
from __future__ import annotations

import numpy as np
from scipy.ndimage import gaussian_filter1d
from shapely.geometry import MultiPoint, Point

from .geom_mesh import unit
from .geom_sheet import Bend

STRING_GAP = 0.004      # string centre sits radius + this behind the back face
STRING_HEAD_GAP = 0.15  # minimum gap between the string surface and the bald head (temples, taper end)
SLAB_HALF = 0.3         # head cross-sections are taken over z +- this (> string radius)
LEVEL_STEP = 0.1        # height spacing of the precomputed cross-sections
THETA_STEP_DEG = 0.25   # polar sampling of each cross-section
TEMPLE_FROM_EYE_PIT = 1.0  # the hull only uses the head this far in front of the eye-socket pit (y),
                           # the outer orbital rim; the clearance has tapered to STRING_HEAD_GAP there
TAPE_GAP = 0.015        # tape surface offset from the back face (z-fighting)
TAPE_END_INSET = 0.06   # the string's cut end hides this far inside the tape's inner edge
SAG_DROP_UNITS = 7.0    # droop of the back of the loop below the mean contact height for string_sag = 1
ASYM_UNITS = 1.0        # half height difference of the two string ends on the mask for string_asymmetry = 1
ASYM_SIDE_UNITS = 3.0   # half height difference of the two sides at the ears for string_asymmetry = 1
SAG_OFFSET_DEG = 30.0   # lowest point moves this far round from straight back (toward -X) per asymmetry
EAR_TOP_GAP = 1.0       # the built tube's surface passes at least this far above the (human) ear top all
                        # along the ear; elf and orc ears are pointed race morphs of it (not available here)
EAR_SAMPLE_STEP = 0.02  # the tube's centre-line segments are sampled this densely for the ear gap
CROWN_MARGIN = 3.5      # the higher side's contact stays this far under the crown (else: a headband)
CLEARANCE_FACTOR = {"human": 1.0, "beast": 0.7}   # x string_clearance_units over the sides / ears
BACK_EXTRA = {"human": 1.5, "beast": 0.0}         # the back centre rides (1 + this) x that clearance
BACK_HALF_WIDTH_DEG = 70.0  # the back swell fades out this far round from straight back
SMOOTH_SIGMA = 0.5      # Gaussian smoothing of the centre line along its length (units)
SMOOTH_STEP = 0.05      # arc-length resampling before smoothing
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


def back_weight(theta, half_width_deg: float = BACK_HALF_WIDTH_DEG) -> np.ndarray:
    """Weight of the back swell at polar angle theta (radians, about the head's vertical axis; -pi/2 =
    straight back): 1 there, cos^2 down to 0 at +-half_width_deg (smooth, no kink)."""
    d = np.angle(np.exp(1j * (np.asarray(theta, float) + np.pi / 2)))
    f = np.clip(np.abs(d) / np.radians(half_width_deg), 0.0, 1.0)
    return 0.5 * (1.0 + np.cos(np.pi * f))


class ClearanceProfile:
    """Where the string centre may run: polar radius R(theta, z) about a vertical axis through
    `centre` of the convex hull of the heads' slab at height z, grown by `grow` where y <= y_full
    (back and sides), swelling to `grow_back` toward the back centre (back_weight; convex hull of the
    result) and blended (smoothstep in y) down to `grow_front` at y = y_cut. Only the head behind
    y_cut (the sides of the head from the outer orbital rim back) shapes the hull, so the string can
    lie along the temples instead of being held off them by the brow and cheeks."""

    def __init__(self, heads, z_lo: float, z_hi: float, grow: float, centre, y_cut: float = np.inf,
                 grow_front: float | None = None, y_full: float | None = None, hair=(), grow_hair: float = 0.0,
                 grow_back: float | None = None, back_half_width_deg: float = BACK_HALF_WIDTH_DEG,
                 z_need: float | None = None):
        """hair = [(verts, tris)] the string must also ride over (a reference hair mesh): its slab hull,
        grown by grow_hair, joins the head's before the convex hull is taken. Levels above z_need
        whose slab no longer contains the centre (toward the crown) are dropped; queries above the
        top level use it."""
        self.centre = np.asarray(centre, float)
        self.grow = grow
        self.grow_front = grow if grow_front is None else grow_front
        self.grow_back = grow if grow_back is None else grow_back
        self.back_half_width_deg = back_half_width_deg
        self.y_cut = y_cut
        self.y_full = self.centre[1] if y_full is None else y_full
        levels = np.arange(z_lo, z_hi + LEVEL_STEP, LEVEL_STEP)
        self.theta = np.radians(np.arange(-180.0, 180.0, THETA_STEP_DEG))
        grows = list(dict.fromkeys((grow, self.grow_front, self.grow_back)))
        self.rings, rad, kept = [], {g: [] for g in grows}, []
        for z in levels:
            pts = np.concatenate([slab_points(v, t, z - SLAB_HALF, z + SLAB_HALF, y_cut) for v, t in heads])
            hull = MultiPoint([tuple(p) for p in pts]).convex_hull
            if not hull.contains(Point(*self.centre)):
                if z_need is not None and z > z_need and kept:
                    break
                raise ValueError(f"profile centre {self.centre} outside the head section at z={z:.2f}")
            hp = [slab_points(v, t, z - SLAB_HALF, z + SLAB_HALF, y_cut) for v, t in hair]
            hp = np.concatenate(hp) if hp else np.zeros((0, 2))
            over = MultiPoint([tuple(p) for p in hp]).convex_hull.buffer(grow_hair, quad_segs=32) if len(hp) else None
            for g in grows:
                poly = hull.buffer(g, quad_segs=32)
                poly = poly.union(over).convex_hull if over is not None else poly
                ring = np.array(poly.exterior.coords)[:-1]
                if g == grow:
                    self.rings.append(ring)
                rad[g].append(self._polar(ring))
            kept.append(z)
        self.levels = np.array(kept)
        self.radius_grid = np.array(rad[grow])                     # (levels, theta)
        self.radius_front_grid = np.array(rad[self.grow_front])
        if self.grow_back != grow:
            # swell toward the back, then the convex hull again: a taut string bridges the swell's
            # flanks instead of following their slight inflection
            w = back_weight(self.theta, back_half_width_deg)
            blend = self.radius_grid + w * (np.array(rad[self.grow_back]) - self.radius_grid)
            dirs = np.stack([np.cos(self.theta), np.sin(self.theta)], axis=1)
            hulls = [MultiPoint([tuple(p) for p in self.centre + b[:, None] * dirs]).convex_hull for b in blend]
            self.radius_grid = np.array([self._polar(np.array(h.exterior.coords)[:-1]) for h in hulls])

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

    def _bilinear(self, grid, theta, z) -> np.ndarray:
        """grid(theta, z), linear in both (theta wraps; z clamped to the precomputed levels)."""
        fz = np.clip((z - self.levels[0]) / LEVEL_STEP, 0, max(len(self.levels) - 1.000001, 0))
        i0 = np.floor(fz).astype(int)
        wz = fz - i0
        n = len(self.theta)
        ft = np.mod((theta - self.theta[0]) / np.radians(THETA_STEP_DEG), n)
        j0 = np.floor(ft).astype(int) % n
        wt = ft - np.floor(ft)
        j1 = (j0 + 1) % n
        r0 = (1 - wt) * grid[i0, j0] + wt * grid[i0, j1]
        r1 = (1 - wt) * grid[i0 + 1, j0] + wt * grid[i0 + 1, j1]
        return (1 - wz) * r0 + wz * r1

    def taper(self, y) -> np.ndarray:
        """Weight of the full clearance at y: 1 behind y_full, smoothstep to 0 at y_cut."""
        if not np.isfinite(self.y_cut) or self.y_cut <= self.y_full:
            return np.ones_like(np.asarray(y, float))
        f = np.clip((self.y_cut - np.asarray(y, float)) / (self.y_cut - self.y_full), 0.0, 1.0)
        return f * f * (3 - 2 * f)

    def radius(self, theta, z) -> np.ndarray:
        """R(theta, z) with the back swell and the temple taper applied."""
        theta, z = np.broadcast_arrays(np.asarray(theta, float), np.asarray(z, float))
        r_full = self._bilinear(self.radius_grid, theta, z)
        if self.grow_front == self.grow:
            return r_full
        r_front = self._bilinear(self.radius_front_grid, theta, z)
        w = self.taper(self.centre[1] + r_full * np.sin(theta))
        return r_front + w * (r_full - r_front)

    def point(self, theta, z) -> np.ndarray:
        r = self.radius(theta, z)
        return self.centre + r[..., None] * np.stack([np.cos(theta), np.sin(theta)], axis=-1)

    def tangent_angle(self, xy, z: float, side: float) -> float:
        """Polar angle of the tangent point, on the +X (side 1) or -X side, of the line from the
        external point xy to the profile at height z: the point where a taut string from xy that
        wraps round the back of the head on that side first touches the profile."""
        th = self.theta
        q = self.point(th, np.full_like(th, z))
        v, d = self.centre - np.asarray(xy, float), q - np.asarray(xy, float)
        ang = np.arctan2(v[0] * d[:, 1] - v[1] * d[:, 0], d @ v)
        i = int(np.argmax(side * ang))
        if not (side * q[i, 0] > 0):
            raise ValueError(f"tangent point on the wrong side of the head at z={z:.2f}")
        return float(th[i])


def smooth_polyline(p, sigma: float, step: float = SMOOTH_STEP) -> np.ndarray:
    """Resample a polyline every `step` of arc length and smooth it with a Gaussian of `sigma` arc
    length; the end points stay where they are."""
    p = np.asarray(p, float)
    s = np.concatenate([[0.0], np.cumsum(np.linalg.norm(np.diff(p, axis=0), axis=1))])
    su = np.linspace(0.0, s[-1], max(2, int(np.ceil(s[-1] / step)) + 1))
    q = np.column_stack([np.interp(su, s, p[:, k]) for k in range(p.shape[1])])
    out = gaussian_filter1d(q, sigma / (su[1] - su[0]), axis=0, mode="nearest")
    out[0], out[-1] = q[0], q[-1]
    return out


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


def contact_heights(z_eye: float, asym: float, r: float, ear_top: float | None, z_cap: float,
                    extra_lift: float = 0.0) -> dict:
    """z of the string centre where it meets the head: +X side asym * ASYM_SIDE_UNITS above the eye
    line, -X as far below; both raised (when the kind has ears beside the head) until the lower side's
    surface clears the ear top by EAR_TOP_GAP + extra_lift (build_twine adds what smoothing and the
    ring spacing take off); the higher side capped at z_cap (crown - CROWN_MARGIN)."""
    z = {+1: z_eye + asym * ASYM_SIDE_UNITS, -1: z_eye - asym * ASYM_SIDE_UNITS}
    if ear_top is not None:
        lift = max(0.0, ear_top + EAR_TOP_GAP + extra_lift + r - min(z.values()))
        z = {k: v + lift for k, v in z.items()}
    z[+1] = max(min(z[+1], z_cap), z[-1])
    return z


def densify(line, step: float = EAR_SAMPLE_STEP) -> np.ndarray:
    """Points along a polyline's segments at most `step` apart (its vertices included)."""
    q = np.asarray(line, float)
    n = np.maximum(1, np.ceil(np.linalg.norm(np.diff(q, axis=0), axis=1) / step).astype(int))
    pts = [q[i] + np.arange(n[i])[:, None] / n[i] * (q[i + 1] - q[i]) for i in range(len(q) - 1)]
    return np.vstack(pts + [q[-1:]])


def ear_gaps(line, ears: dict, r: float) -> dict:
    """Per side (+1 = +X, -1 = -X): lowest surface point (z - r) of the tube's centre-line segments
    where they run alongside the ear (same side, y within the ears' front-to-back extent, sampled
    every EAR_SAMPLE_STEP) minus the ear top; None where the line never runs alongside."""
    q = densify(line)
    y0, y1 = ears["y_range"]
    out = {}
    for side in (+1, -1):
        along = (side * q[:, 0] > 0) & (q[:, 1] >= y0) & (q[:, 1] <= y1)
        out[side] = float(np.min(q[along, 2]) - r - ears["top_z"]) if along.any() else None
    return out


def _droop(phi, phi_low: float, z_start: float, z_low: float, z_end: float) -> np.ndarray:
    """Height along the wrap (phi = arc-length fraction): cosine ease from z_start down to z_low at
    phi_low and up to z_end; level at both ends and at the bottom."""
    a = np.clip(phi / phi_low, 0, 1)
    b = np.clip((1 - phi) / (1 - phi_low), 0, 1)
    down = z_low + (z_start - z_low) * 0.5 * (1 + np.cos(np.pi * a))
    up = z_low + (z_end - z_low) * 0.5 * (1 + np.cos(np.pi * b))
    return np.where(phi <= phi_low, down, up)


def string_path(bend: Bend, attach: dict, profile: ClearanceProfile, r: float, sag: float,
                asym: float, side_z: dict) -> tuple:
    """3D centre line of the twine (dense, smoothed) and a summary of its construction.

    attach = {side(+1/-1): (a_end, a_lift)} in sheet units, at heights +-asym * ASYM_UNITS about
    the eye line; side_z = {side: z} the string's height where it meets the head (contact_heights).
    Each side: run on the back face, lift off, straight taut span to the tangent point on the
    clearance profile; between the two tangent points the string follows the profile round the back
    of the head, drooping by sag * SAG_DROP_UNITS (lowest point asym * SAG_OFFSET_DEG off-centre
    toward -X)."""
    h = {+1: asym * ASYM_UNITS, -1: -asym * ASYM_UNITS}
    drop = sag * SAG_DROP_UNITS
    on_back = -(r + STRING_GAP)
    face_runs, lifts, contact = {}, {}, {}
    for side, (a_end, a_lift) in attach.items():
        ah = np.stack([np.linspace(a_end, a_lift, 8), np.full(8, h[side])], axis=1)
        face_runs[side] = bend.lift(ah, on_back)
        lifts[side] = face_runs[side][-1]
        contact[side] = profile.tangent_angle(lifts[side][:2], side_z[side], side)
    # clockwise seen from above (decreasing theta) from the +X contact through the back (-Y) to -X
    th0 = contact[+1]
    th1 = th0 - (th0 - contact[-1]) % (2 * np.pi)
    th_low = -np.pi / 2 - np.radians(SAG_OFFSET_DEG * asym)
    if not th1 < th_low < th0:
        raise ValueError(f"string contacts {np.degrees([th0, th1])} do not straddle the back of the head")
    theta = np.linspace(th0, th1, max(64, int(np.degrees(th0 - th1) / 0.5)))
    z_low = 0.5 * (side_z[+1] + side_z[-1]) - drop
    phi = (th0 - theta) / (th0 - th1)
    for _ in range(4):                         # z depends on arc length, arc length (mildly) on z
        phi_low = float(np.interp(th_low, theta[::-1], phi[::-1]))
        z = _droop(phi, phi_low, side_z[+1], z_low, side_z[-1])
        xy = profile.point(theta, z)
        arc = np.concatenate([[0.0], np.cumsum(np.linalg.norm(np.diff(xy, axis=0), axis=1))])
        phi = arc / arc[-1]
    phi_low = float(np.interp(th_low, theta[::-1], phi[::-1]))
    z = _droop(phi, phi_low, side_z[+1], z_low, side_z[-1])
    wrap = np.column_stack([profile.point(theta, z), z])
    spans = {s_: _segment(lifts[s_], end, 0.25) for s_, end in ((+1, wrap[0]), (-1, wrap[-1]))}
    path = np.vstack([face_runs[+1][:-1], spans[+1][:-1], wrap, spans[-1][::-1][1:], face_runs[-1][::-1][1:]])
    dense = smooth_polyline(path, SMOOTH_SIGMA)
    behind = dense[dense[:, 1] <= min(wrap[0][1], wrap[-1][1])]          # the wrapped part only
    low = behind[np.argmin(behind[:, 2])]
    info = {"contact_theta_deg": [round(float(np.degrees(th0)), 2), round(float(np.degrees(th1)), 2)],
            "contact_points": [wrap[0].round(3).tolist(), wrap[-1].round(3).tolist()],
            "attach_heights": [round(float(lifts[+1][2]), 3), round(float(lifts[-1][2]), 3)],
            "side_heights": [round(float(side_z[+1]), 3), round(float(side_z[-1]), 3)],
            "side_height_difference": round(float(side_z[+1] - side_z[-1]), 3),
            "low_theta_deg": round(float(np.degrees(th_low)), 2), "low_point": low.round(3).tolist(),
            "back_drop": round(float(0.5 * (side_z[+1] + side_z[-1]) - low[2]), 3),   # wrapped part's lowest point
            "y_cut": round(float(profile.y_cut), 4), "y_full": round(float(profile.y_full), 4),
            "clearance_grow": {"temples": round(profile.grow_front, 4), "sides": round(profile.grow, 4),
                               "back": round(profile.grow_back, 4)},
            "back_half_width_deg": profile.back_half_width_deg,
            "wrap_length": round(float(arc[-1]), 3)}
    return dense, info


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


def tube_centre_line(verts, sides: int) -> np.ndarray:
    """Centre line of a tube built by `tube` (also from its NIF read-back, which keeps the vertex
    order): the mean of each ring's `sides` distinct vertices; the two end caps are dropped."""
    v = np.asarray(verts, float)
    ring = sides + 1
    n = (len(v) - 2 * (1 + sides)) // ring
    return v[:n * ring].reshape(n, ring, 3)[:, :sides].mean(axis=1)


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
