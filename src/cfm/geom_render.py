"""Tiny orthographic software renderer (PIL) for debug images of heads and masks.

Painter's algorithm with flat Lambert shading; good enough to eyeball placement, winding
and UV mapping. Views follow docs/CONTRACTS.md: a viewer in front of the actor looks down -Y,
so screen-right is model -X and screen-down is model -Z."""
from __future__ import annotations

import numpy as np
from PIL import Image, ImageDraw, ImageFont

# view name -> (screen-x axis, screen-y axis, towards-viewer axis) as model-space vectors
VIEWS = {
    "front": (np.array([-1.0, 0, 0]), np.array([0, 0, -1.0]), np.array([0, 1.0, 0])),
    "side": (np.array([0, 1.0, 0]), np.array([0, 0, -1.0]), np.array([1.0, 0, 0])),  # actor faces right
    "back": (np.array([1.0, 0, 0]), np.array([0, 0, -1.0]), np.array([0, -1.0, 0])),
    "top": (np.array([-1.0, 0, 0]), np.array([0, -1.0, 0]), np.array([0, 0, 1.0])),
}


class Canvas:
    """One orthographic panel: model units -> pixels around a model-space centre."""

    def __init__(self, view, center, px_per_unit, size, ss=2):
        self.sx, self.sy, self.toward = VIEWS[view]
        self.center = np.asarray(center, float)
        self.ppu = px_per_unit * ss
        self.size = size
        self.ss = ss
        self.img = Image.new("RGB", (size[0] * ss, size[1] * ss), (250, 250, 247))
        self.draw = ImageDraw.Draw(self.img)

    def project(self, p):
        q = np.asarray(p, float) - self.center
        x = q @ self.sx * self.ppu + self.img.width / 2
        y = q @ self.sy * self.ppu + self.img.height / 2
        return np.stack([x, y], axis=-1)

    def depth(self, p):
        return (np.asarray(p, float) - self.center) @ self.toward

    def mesh(self, v, t, colors=(200, 200, 200), cull=True, light=(0.3, 0.5, 0.8), outline=None):
        """Draw one mesh; colors is one RGB tuple or an (n_tris, 3) array."""
        self.scene([(v, t, colors)], cull=cull, light=light, outline=outline)

    def scene(self, meshes, cull=True, light=(0.3, 0.5, 0.8), outline=None):
        """Draw several meshes [(v, t, colors), ...] depth-sorted together."""
        vs, ts, cs, base = [], [], [], 0
        for v, t, colors in meshes:
            v, t = np.asarray(v, float), np.asarray(t, int)
            vs.append(v)
            ts.append(t + base)
            cs.append(np.broadcast_to(np.asarray(colors, float), (len(t), 3)))
            base += len(v)
        v, t = np.concatenate(vs), np.concatenate(ts)
        if len(t) == 0:
            return
        colors = np.concatenate(cs)
        fc = np.cross(v[t[:, 1]] - v[t[:, 0]], v[t[:, 2]] - v[t[:, 0]])
        n = fc / np.maximum(np.linalg.norm(fc, axis=1, keepdims=True), 1e-20)
        facing = n @ self.toward
        ldir = np.asarray(light, float)
        ldir = ldir[0] * -self.sx + ldir[1] * -self.sy + ldir[2] * self.toward
        ldir /= np.linalg.norm(ldir)
        shade = 0.45 + 0.55 * np.clip(n @ ldir, 0, 1)
        cols = np.broadcast_to(np.asarray(colors, float), (len(t), 3))
        order = np.argsort(self.depth(v[t].mean(axis=1)))
        pts = self.project(v)
        for i in order:
            if cull and facing[i] <= 0:
                continue
            c = tuple(int(x) for x in np.clip(cols[i] * shade[i], 0, 255))
            self.draw.polygon([tuple(pts[j]) for j in t[i]], fill=c, outline=outline)

    def polyline(self, p, color, width=1, closed=False):
        q = [tuple(x) for x in self.project(p)]
        if closed:
            q.append(q[0])
        self.draw.line(q, fill=color, width=width * self.ss)

    def marker(self, p, color, label=None, r=4):
        x, y = self.project(np.asarray(p, float)[None])[0]
        r *= self.ss
        self.draw.line([(x - r, y), (x + r, y)], fill=color, width=2 * self.ss)
        self.draw.line([(x, y - r), (x, y + r)], fill=color, width=2 * self.ss)
        if label:
            self.draw.text((x + r + 2, y - 6 * self.ss), label, fill=color, font=_font(11 * self.ss))

    def hline(self, z_point, color, label=None, dash=6):
        """Horizontal screen line through the projection of a model point."""
        _, y = self.project(np.asarray(z_point, float)[None])[0]
        for x0 in range(0, self.img.width, 2 * dash * self.ss):
            self.draw.line([(x0, y), (x0 + dash * self.ss, y)], fill=color, width=self.ss)
        if label:
            self.draw.text((4 * self.ss, y - 13 * self.ss), label, fill=color, font=_font(10 * self.ss))

    def vline(self, point, color, label=None, dash=6):
        x, _ = self.project(np.asarray(point, float)[None])[0]
        for y0 in range(0, self.img.height, 2 * dash * self.ss):
            self.draw.line([(x, y0), (x, y0 + dash * self.ss)], fill=color, width=self.ss)
        if label:
            self.draw.text((x + 3 * self.ss, self.img.height - 16 * self.ss), label, fill=color, font=_font(10 * self.ss))

    def title(self, text):
        self.draw.text((6 * self.ss, 4 * self.ss), text, fill=(20, 20, 20), font=_font(13 * self.ss))

    def image(self):
        return self.img.resize(self.size, Image.LANCZOS)


def _font(size):
    for name in ("DejaVuSans.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def hstack(images, gap=8, bg=(255, 255, 255)):
    w = sum(i.width for i in images) + gap * (len(images) - 1)
    h = max(i.height for i in images)
    out = Image.new("RGB", (w, h), bg)
    x = 0
    for im in images:
        out.paste(im, (x, 0))
        x += im.width + gap
    return out


def vstack(images, gap=8, bg=(255, 255, 255)):
    w = max(i.width for i in images)
    h = sum(i.height for i in images) + gap * (len(images) - 1)
    out = Image.new("RGB", (w, h), bg)
    y = 0
    for im in images:
        out.paste(im, (0, y))
        y += im.height + gap
    return out


def sample_texture(img: Image.Image, uv) -> np.ndarray:
    """Nearest texel colours (RGB float) at NIF UVs (u right, v down)."""
    a = np.asarray(img.convert("RGB"), float)
    uv = np.asarray(uv, float)
    x = np.clip((uv[:, 0] * a.shape[1]).astype(int), 0, a.shape[1] - 1)
    y = np.clip((uv[:, 1] * a.shape[0]).astype(int), 0, a.shape[0] - 1)
    return a[y, x]


def project(view, points, center, px_per_unit, size) -> np.ndarray:
    """Pixel coordinates of model points in a raster() image with the same view parameters."""
    sx, sy, _ = VIEWS[view]
    q = np.asarray(points, float) - np.asarray(center, float)
    return np.stack([q @ sx * px_per_unit + size[0] / 2, q @ sy * px_per_unit + size[1] / 2], axis=-1)


def raster(view, meshes, center, px_per_unit, size, light=(0.3, 0.5, 0.8), bg=(250, 250, 247)):
    """Z-buffered orthographic raster with per-pixel UV texturing (wrap addressing) and flat shading.

    meshes: [(verts, tris, uvs | None, texture Image | None, rgb fallback)]. Back faces culled."""
    sx, sy, toward = VIEWS[view]
    w, h = size
    img = np.empty((h, w, 3))
    img[:] = bg
    zbuf = np.full((h, w), -np.inf)
    ldir = np.asarray(light, float)
    ldir = ldir[0] * -sx + ldir[1] * -sy + ldir[2] * toward
    ldir /= np.linalg.norm(ldir)
    c = np.asarray(center, float)
    for v, t, uv, tex, rgb in meshes:
        v, t = np.asarray(v, float), np.asarray(t, int)
        q = v - c
        px = q @ sx * px_per_unit + w / 2
        py = q @ sy * px_per_unit + h / 2
        pz = q @ toward
        fc = np.cross(v[t[:, 1]] - v[t[:, 0]], v[t[:, 2]] - v[t[:, 0]])
        n = fc / np.maximum(np.linalg.norm(fc, axis=1, keepdims=True), 1e-20)
        texa = np.asarray(tex.convert("RGB"), float) if tex is not None else None
        for i, tri in enumerate(t):
            if n[i] @ toward <= 0:
                continue
            x, y, z = px[tri], py[tri], pz[tri]
            x0, x1 = max(int(np.floor(x.min())), 0), min(int(np.ceil(x.max())), w - 1)
            y0, y1 = max(int(np.floor(y.min())), 0), min(int(np.ceil(y.max())), h - 1)
            if x0 > x1 or y0 > y1:
                continue
            gx, gy = np.meshgrid(np.arange(x0, x1 + 1) + 0.5, np.arange(y0, y1 + 1) + 0.5)
            det = (y[1] - y[2]) * (x[0] - x[2]) + (x[2] - x[1]) * (y[0] - y[2])
            if abs(det) < 1e-12:
                continue
            l0 = ((y[1] - y[2]) * (gx - x[2]) + (x[2] - x[1]) * (gy - y[2])) / det
            l1 = ((y[2] - y[0]) * (gx - x[2]) + (x[0] - x[2]) * (gy - y[2])) / det
            l2 = 1 - l0 - l1
            inside = (l0 >= 0) & (l1 >= 0) & (l2 >= 0)
            zz = l0 * z[0] + l1 * z[1] + l2 * z[2]
            sub = zbuf[y0:y1 + 1, x0:x1 + 1]
            m = inside & (zz > sub)
            if not m.any():
                continue
            sub[m] = zz[m]
            shade = 0.5 + 0.5 * max(float(n[i] @ ldir), 0.0)
            if texa is not None and uv is not None:
                uu = (l0 * uv[tri[0], 0] + l1 * uv[tri[1], 0] + l2 * uv[tri[2], 0])[m]
                vv = (l0 * uv[tri[0], 1] + l1 * uv[tri[1], 1] + l2 * uv[tri[2], 1])[m]
                tx = np.floor(uu * texa.shape[1]).astype(int) % texa.shape[1]   # wrap addressing
                ty = np.floor(vv * texa.shape[0]).astype(int) % texa.shape[0]
                col = texa[ty, tx]
            else:
                col = np.asarray(rgb, float)
            img[y0:y1 + 1, x0:x1 + 1][m] = np.clip(col * shade * 1.15, 0, 255)
    return Image.fromarray(img.astype(np.uint8))
