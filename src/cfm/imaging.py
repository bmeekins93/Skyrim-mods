"""Image I/O and colour helpers shared by the image stages (01, 02, 05).

The source photo is a 16-bit RGB PNG tagged with a Display P3 ICC profile
(matrix/shaper, parametric sRGB-style TRC). Pillow opens such files as 8-bit
"RGB" by dropping the low byte and ignores the profile, which both truncates
and shifts colours. `load_source_srgb` instead reads all 16 bits with OpenCV,
applies the ICC matrix/shaper transform to sRGB in floating point and rounds
once at the end.
"""
from __future__ import annotations

import io
import struct
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageCms

# --- sRGB transfer functions (IEC 61966-2-1) ---------------------------------


def srgb_to_linear(x: np.ndarray) -> np.ndarray:
    x = np.asarray(x, dtype=np.float64)
    return np.where(x <= 0.04045, x / 12.92, ((x + 0.055) / 1.055) ** 2.4)


def linear_to_srgb(x: np.ndarray) -> np.ndarray:
    x = np.clip(np.asarray(x, dtype=np.float64), 0.0, 1.0)
    return np.where(x <= 0.0031308, x * 12.92, 1.055 * np.power(x, 1 / 2.4) - 0.055)


def to_u8(x: np.ndarray) -> np.ndarray:
    """Float image in [0, 1] -> uint8 with rounding (not truncation)."""
    return np.clip(np.rint(np.asarray(x) * 255.0), 0, 255).astype(np.uint8)


# --- Minimal ICC matrix/shaper parsing (ICC.1:2010 sections 10.15, 10.31) ----


def _icc_tags(icc: bytes) -> dict[bytes, bytes]:
    count = struct.unpack(">I", icc[128:132])[0]
    tags = {}
    for i in range(count):
        sig, off, size = struct.unpack(">4sII", icc[132 + 12 * i:144 + 12 * i])
        tags[sig] = icc[off:off + size]
    return tags


def _s15f16(raw: bytes) -> float:
    return struct.unpack(">i", raw)[0] / 65536.0


def _xyz_tag(data: bytes) -> np.ndarray:
    assert data[:4] == b"XYZ ", data[:4]
    return np.array([_s15f16(data[8 + 4 * i:12 + 4 * i]) for i in range(3)])


def _trc_function(data: bytes):
    """Return a numpy callable for a 'para' or 'curv' tone curve (encoded -> linear)."""
    kind = data[:4]
    if kind == b"para":
        ftype = struct.unpack(">H", data[8:10])[0]
        nparams = {0: 1, 1: 3, 2: 4, 3: 5, 4: 7}[ftype]
        p = [_s15f16(data[12 + 4 * i:16 + 4 * i]) for i in range(nparams)]
        g = p[0]
        if ftype == 0:
            return lambda x: np.power(x, g)
        if ftype == 1:
            a, b = p[1], p[2]
            return lambda x: np.where(x >= -b / a, np.power(np.maximum(a * x + b, 0), g), 0.0)
        if ftype == 2:
            a, b, c = p[1], p[2], p[3]
            return lambda x: np.where(x >= -b / a, np.power(np.maximum(a * x + b, 0), g) + c, c)
        if ftype == 3:
            a, b, c, d = p[1], p[2], p[3], p[4]
            return lambda x: np.where(x >= d, np.power(np.maximum(a * x + b, 0), g), c * x)
        a, b, c, d, e, f = p[1:]
        return lambda x: np.where(x >= d, np.power(np.maximum(a * x + b, 0), g) + e, c * x + f)
    if kind == b"curv":
        n = struct.unpack(">I", data[8:12])[0]
        if n == 0:
            return lambda x: x
        if n == 1:
            gamma = struct.unpack(">H", data[12:14])[0] / 256.0
            return lambda x: np.power(x, gamma)
        table = np.frombuffer(data[12:12 + 2 * n], dtype=">u2").astype(np.float64) / 65535.0
        grid = np.linspace(0.0, 1.0, n)
        return lambda x: np.interp(x, grid, table)
    raise ValueError(f"unsupported TRC type {kind!r}")


def _srgb_matrix_d50() -> np.ndarray:
    """Linear sRGB -> PCS XYZ (D50-adapted), from LittleCMS's built-in sRGB profile."""
    prof = ImageCms.createProfile("sRGB")
    cols = [prof.red_colorant[0], prof.green_colorant[0], prof.blue_colorant[0]]
    return np.array(cols, dtype=np.float64).T


def icc_to_srgb_linear(rgb01: np.ndarray, icc: bytes) -> np.ndarray:
    """Encoded RGB in [0,1] (source profile) -> linear sRGB (unclipped)."""
    tags = _icc_tags(icc)
    if not all(t in tags for t in (b"rXYZ", b"gXYZ", b"bXYZ", b"rTRC", b"gTRC", b"bTRC")):
        raise ValueError("ICC profile is not a matrix/shaper RGB profile")
    src = np.stack([_xyz_tag(tags[t]) for t in (b"rXYZ", b"gXYZ", b"bXYZ")], axis=1)
    lin = np.empty_like(rgb01, dtype=np.float64)
    for ch, t in enumerate((b"rTRC", b"gTRC", b"bTRC")):
        lin[..., ch] = _trc_function(tags[t])(rgb01[..., ch].astype(np.float64))
    m = np.linalg.solve(_srgb_matrix_d50(), src)  # source linear -> sRGB linear
    return lin @ m.T


def _exif_orientation(path: Path) -> int:
    try:
        return int(Image.open(path).getexif().get(274, 1))
    except Exception:
        return 1


def _apply_orientation(img: np.ndarray, orientation: int) -> np.ndarray:
    ops = {
        2: lambda a: a[:, ::-1], 3: lambda a: a[::-1, ::-1], 4: lambda a: a[::-1],
        5: lambda a: np.swapaxes(a, 0, 1), 6: lambda a: np.swapaxes(a, 0, 1)[:, ::-1],
        7: lambda a: np.swapaxes(a, 0, 1)[::-1, ::-1], 8: lambda a: np.swapaxes(a, 0, 1)[::-1],
    }
    return np.ascontiguousarray(ops[orientation](img)) if orientation in ops else img


def load_source_linear(path: str | Path) -> tuple[np.ndarray, dict]:
    """Decode the photo at full bit depth -> linear-light sRGB float64 (H, W, 3), clipped to [0, 1].

    Returns (image, info) where info records bit depth, ICC description, orientation
    and the fraction of pixels that were outside the sRGB gamut (clipped).
    """
    path = Path(path)
    raw = cv2.imread(str(path), cv2.IMREAD_UNCHANGED)
    if raw is None:
        raise FileNotFoundError(path)
    if raw.ndim == 2:
        raw = np.repeat(raw[..., None], 3, axis=2)
    raw = raw[..., :3][..., ::-1]  # OpenCV is BGR(A)
    maxval = {np.dtype(np.uint8): 255.0, np.dtype(np.uint16): 65535.0}[raw.dtype]
    rgb01 = raw.astype(np.float64) / maxval

    pil = Image.open(path)
    icc = pil.info.get("icc_profile")
    info = {"bit_depth": 16 if raw.dtype == np.uint16 else 8, "icc": None, "orientation": 1}
    if icc:
        desc = ImageCms.getProfileDescription(ImageCms.ImageCmsProfile(io.BytesIO(icc))).strip()
        info["icc"] = desc
        lin = icc_to_srgb_linear(rgb01, icc)
    else:
        lin = srgb_to_linear(rgb01)  # untagged: assume sRGB
    info["out_of_gamut_fraction"] = float(np.mean(np.any((lin < -1e-4) | (lin > 1 + 1e-4), axis=-1)))
    lin = np.clip(lin, 0.0, 1.0)
    info["orientation"] = _exif_orientation(path)
    lin = _apply_orientation(lin, info["orientation"])
    return lin, info


def load_source_srgb(path: str | Path) -> tuple[np.ndarray, dict]:
    """Decode the photo -> 8-bit sRGB uint8 (H, W, 3) plus decode info (see load_source_linear)."""
    lin, info = load_source_linear(path)
    return to_u8(linear_to_srgb(lin)), info


# --- small generic helpers ---------------------------------------------------


def save_png(path: str | Path, img: np.ndarray) -> None:
    """Write an RGB/RGBA/L uint8 array as PNG (deterministic: no timestamps)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(img).save(path, optimize=False, compress_level=6)


def resize_linear(img: np.ndarray, size: tuple[int, int], interp=cv2.INTER_AREA) -> np.ndarray:
    """Resize an sRGB uint8 image in linear light. size = (width, height)."""
    lin = srgb_to_linear(img.astype(np.float64) / 255.0).astype(np.float32)
    out = cv2.resize(lin, size, interpolation=interp)
    return to_u8(linear_to_srgb(out))
