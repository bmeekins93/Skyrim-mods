"""Small triangle-mesh utilities (numpy only): normals, welding, boundary loops,
point-to-mesh distance, generalized winding number and an OBJ writer."""
from __future__ import annotations

from pathlib import Path

import numpy as np


def face_cross(v, t) -> np.ndarray:
    """cross(v1 - v0, v2 - v0) per triangle (length = 2 x area)."""
    v = np.asarray(v, float)
    t = np.asarray(t, int)
    return np.cross(v[t[:, 1]] - v[t[:, 0]], v[t[:, 2]] - v[t[:, 0]])


def unit(a, axis=-1) -> np.ndarray:
    a = np.asarray(a, float)
    n = np.linalg.norm(a, axis=axis, keepdims=True)
    return a / np.where(n > 1e-20, n, 1.0)


def vertex_normals(v, t) -> np.ndarray:
    """Area-weighted smooth vertex normals following the triangle winding."""
    fc = face_cross(v, t)
    n = np.zeros((len(v), 3))
    for k in range(3):
        np.add.at(n, np.asarray(t)[:, k], fc)
    return unit(n)


def winding_agreement(v, t, normals) -> float:
    """Fraction of triangles whose cross(v1-v0, v2-v0) points the same way as their mean vertex normal."""
    fc = face_cross(v, t)
    nrm = np.asarray(normals, float)[np.asarray(t)].sum(axis=1)
    ok = np.einsum("ij,ij->i", fc, nrm) > 0
    return float(np.mean(ok))


def weld(v, tol=1e-4):
    """Merge coincident vertices. Returns (unique_positions, index_map old->new)."""
    v = np.asarray(v, float)
    key = np.round(v / tol).astype(np.int64)
    _, first, inv = np.unique(key, axis=0, return_index=True, return_inverse=True)
    return v[first], inv.reshape(-1)


def boundary_loops(t) -> list:
    """Closed loops of boundary edges (edges used by exactly one triangle), as vertex lists."""
    t = np.asarray(t, int)
    edges = np.concatenate([t[:, [0, 1]], t[:, [1, 2]], t[:, [2, 0]]])
    key = np.sort(edges, axis=1)
    uniq, counts = np.unique(key, axis=0, return_counts=True)
    single = {tuple(e) for e in uniq[counts == 1]}
    nxt = {}
    for a, b in edges:  # keep the directed version of each boundary edge
        if (min(a, b), max(a, b)) in single:
            nxt.setdefault(int(a), []).append(int(b))
    loops, seen = [], set()
    for start in sorted(nxt):
        if start in seen:
            continue
        loop, cur = [], start
        while cur not in seen and cur in nxt:
            seen.add(cur)
            loop.append(cur)
            cur = nxt[cur][0]
        if len(loop) >= 3:
            loops.append(loop)
    return loops


def closest_points(p, v, t, chunk=256):
    """Unsigned distance from points p to triangle mesh (v, t).

    Returns (distance, closest_point, triangle_index). Exact point-triangle distance
    (Ericson, Real-Time Collision Detection 5.1.5), vectorised over all triangles."""
    p = np.asarray(p, float)
    v = np.asarray(v, float)
    t = np.asarray(t, int)
    a, b, c = v[t[:, 0]], v[t[:, 1]], v[t[:, 2]]
    best_d = np.full(len(p), np.inf)
    best_q = np.zeros_like(p)
    best_i = np.zeros(len(p), int)
    for s in range(0, len(p), chunk):
        q = _closest_on_tris(p[s:s + chunk, None, :], a[None], b[None], c[None])
        d = np.linalg.norm(q - p[s:s + chunk, None, :], axis=2)
        i = np.argmin(d, axis=1)
        r = np.arange(len(i))
        best_d[s:s + chunk] = d[r, i]
        best_q[s:s + chunk] = q[r, i]
        best_i[s:s + chunk] = i
    return best_d, best_q, best_i


def _dot(x, y):
    return np.sum(x * y, axis=-1)


def _closest_on_tris(p, a, b, c):
    ab, ac, ap = b - a, c - a, p - a
    d1, d2 = _dot(ab, ap), _dot(ac, ap)
    bp = p - b
    d3, d4 = _dot(ab, bp), _dot(ac, bp)
    cp = p - c
    d5, d6 = _dot(ab, cp), _dot(ac, cp)
    va = d3 * d6 - d5 * d4
    vb = d5 * d2 - d1 * d6
    vc = d1 * d4 - d3 * d2
    denom = va + vb + vc
    denom = np.where(np.abs(denom) < 1e-30, 1e-30, denom)
    vv, ww = vb / denom, vc / denom
    res = a + ab * vv[..., None] + ac * ww[..., None]  # interior
    # Edge and vertex regions, applied in reverse priority so the first match wins.
    with np.errstate(divide="ignore", invalid="ignore"):
        wbc = (d4 - d3) / ((d4 - d3) + (d5 - d6))
        m = (va <= 0) & (d4 - d3 >= 0) & (d5 - d6 >= 0)
        res = np.where(m[..., None], b + (c - b) * np.nan_to_num(wbc)[..., None], res)
        wac = d2 / (d2 - d6)
        m = (vb <= 0) & (d2 >= 0) & (d6 <= 0)
        res = np.where(m[..., None], a + ac * np.nan_to_num(wac)[..., None], res)
        wab = d1 / (d1 - d3)
        m = (vc <= 0) & (d1 >= 0) & (d3 <= 0)
        res = np.where(m[..., None], a + ab * np.nan_to_num(wab)[..., None], res)
    m = (d6 >= 0) & (d5 <= d6)
    res = np.where(m[..., None], c, res)
    m = (d3 >= 0) & (d4 <= d3)
    res = np.where(m[..., None], b, res)
    m = (d1 <= 0) & (d2 <= 0)
    res = np.where(m[..., None], a, res)
    return res


def winding_number(p, v, t, chunk=256) -> np.ndarray:
    """Generalized winding number (Jacobson et al. 2013): ~1 inside, ~0 outside.

    Robust for meshes with holes (eye sockets, open neck) away from the holes. The mesh
    must be wound with outward-facing counter-clockwise triangles."""
    p = np.asarray(p, float)
    v = np.asarray(v, float)
    t = np.asarray(t, int)
    out = np.zeros(len(p))
    for s in range(0, len(p), chunk):
        a = v[t[:, 0]][None] - p[s:s + chunk, None]
        b = v[t[:, 1]][None] - p[s:s + chunk, None]
        c = v[t[:, 2]][None] - p[s:s + chunk, None]
        la, lb, lc = (np.linalg.norm(x, axis=2) for x in (a, b, c))
        num = _dot(a, np.cross(b, c))
        den = la * lb * lc + _dot(a, b) * lc + _dot(b, c) * la + _dot(c, a) * lb
        out[s:s + chunk] = np.sum(2.0 * np.arctan2(num, den), axis=1) / (4 * np.pi)
    return out


def write_obj(path, parts) -> None:
    """Write named parts [(name, verts, tris, uvs|None, normals|None), ...] to an OBJ file.

    UVs are written OBJ-style (v up), i.e. 1 - v_nif."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    lines, base = [], 1
    for name, v, t, uv, n in parts:
        lines.append(f"o {name}")
        lines += [f"v {x:.5f} {y:.5f} {z:.5f}" for x, y, z in np.asarray(v)]
        if uv is not None:
            lines += [f"vt {a:.5f} {1.0 - b:.5f}" for a, b in np.asarray(uv)]
        if n is not None:
            lines += [f"vn {x:.4f} {y:.4f} {z:.4f}" for x, y, z in np.asarray(n)]
        for tri in np.asarray(t) + base:
            if uv is not None and n is not None:
                lines.append("f " + " ".join(f"{i}/{i}/{i}" for i in tri))
            elif n is not None:
                lines.append("f " + " ".join(f"{i}//{i}" for i in tri))
            else:
                lines.append("f " + " ".join(str(i) for i in tri))
        base += len(v)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def height_field(v, t, xs, zs, front=True) -> np.ndarray:
    """Rasterise a mesh along Y: for each (z, x) grid node the largest (front=True) or
    smallest Y of the surface above it; NaN where no triangle covers the node."""
    v = np.asarray(v, float)
    t = np.asarray(t, int)
    xs = np.asarray(xs, float)
    zs = np.asarray(zs, float)
    out = np.full((len(zs), len(xs)), -np.inf if front else np.inf)
    for tri in t:
        p = v[tri]
        ix = np.nonzero((xs >= p[:, 0].min()) & (xs <= p[:, 0].max()))[0]
        iz = np.nonzero((zs >= p[:, 2].min()) & (zs <= p[:, 2].max()))[0]
        if len(ix) == 0 or len(iz) == 0:
            continue
        gx, gz = np.meshgrid(xs[ix], zs[iz])
        (x0, _, z0), (x1, _, z1), (x2, _, z2) = p
        det = (z1 - z2) * (x0 - x2) + (x2 - x1) * (z0 - z2)
        if abs(det) < 1e-12:
            continue
        l0 = ((z1 - z2) * (gx - x2) + (x2 - x1) * (gz - z2)) / det
        l1 = ((z2 - z0) * (gx - x2) + (x0 - x2) * (gz - z2)) / det
        l2 = 1 - l0 - l1
        inside = (l0 >= -1e-9) & (l1 >= -1e-9) & (l2 >= -1e-9)
        y = l0 * p[0, 1] + l1 * p[1, 1] + l2 * p[2, 1]
        sub = out[np.ix_(iz, ix)]
        if front:
            sub = np.where(inside & (y > sub), y, sub)
        else:
            sub = np.where(inside & (y < sub), y, sub)
        out[np.ix_(iz, ix)] = sub
    out[~np.isfinite(out)] = np.nan
    return out


def ray_hits(origins, dirs, v, t, chunk=64) -> list:
    """Möller-Trumbore: for each ray, the sorted distances (> 0) of all triangle hits."""
    o = np.asarray(origins, float)
    d = np.asarray(dirs, float)
    v = np.asarray(v, float)
    t = np.asarray(t, int)
    a, e1, e2 = v[t[:, 0]], v[t[:, 1]] - v[t[:, 0]], v[t[:, 2]] - v[t[:, 0]]
    out = []
    for s in range(0, len(o), chunk):
        oo, dd = o[s:s + chunk, None, :], d[s:s + chunk, None, :]
        pvec = np.cross(dd, e2[None])
        det = _dot(e1[None], pvec)
        ok = np.abs(det) > 1e-12
        inv = np.where(ok, 1.0 / np.where(ok, det, 1.0), 0.0)
        tvec = oo - a[None]
        u = _dot(tvec, pvec) * inv
        qvec = np.cross(tvec, e1[None])
        w = _dot(dd, qvec) * inv
        dist = _dot(e2[None], qvec) * inv
        hit = ok & (u >= 0) & (w >= 0) & (u + w <= 1) & (dist > 1e-9)
        for row_hit, row_dist in zip(hit, dist):
            out.append(np.sort(row_dist[row_hit]))
    return out
