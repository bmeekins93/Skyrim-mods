"""DDS container writer/reader, mip-chain generation and BCn encoding for stage 3.

Container layout follows Microsoft's documentation (Win32 "Programming Guide for DDS"):
  DDS_HEADER         https://learn.microsoft.com/windows/win32/direct3ddds/dds-header
  DDS_PIXELFORMAT    https://learn.microsoft.com/windows/win32/direct3ddds/dds-pixelformat
  DDS_HEADER_DXT10   https://learn.microsoft.com/windows/win32/direct3ddds/dds-header-dxt10
  DXGI_FORMAT        https://learn.microsoft.com/windows/win32/api/dxgiformat/ne-dxgiformat-dxgi_format
The legacy fields written for BC1/BC3 (flags 0xA1007, caps 0x401008, linear size of mip 0)
match what ImageMagick 6.9.12's DDS coder writes for the same content.

Encoders (pip packages, MIT-licensed):
  BC7      etcpak 0.9.15 `compress_bc7` (Rich Geldreich's bc7enc)
  BC1/BC3  quicktex 0.3.1 (rgbcx-based BC1 colour + BC4 alpha encoder)
Decoding for verification: texture2ddecoder 1.0.6 (independent of both encoders) and Pillow.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass, field

import cv2
import numpy as np

from .imaging import linear_to_srgb, srgb_to_linear

DDS_MAGIC = b"DDS "
DDSD_CAPS, DDSD_HEIGHT, DDSD_WIDTH, DDSD_PITCH = 0x1, 0x2, 0x4, 0x8
DDSD_PIXELFORMAT, DDSD_MIPMAPCOUNT, DDSD_LINEARSIZE = 0x1000, 0x20000, 0x80000
DDPF_FOURCC = 0x4
DDSCAPS_COMPLEX, DDSCAPS_TEXTURE, DDSCAPS_MIPMAP = 0x8, 0x1000, 0x400000
D3D10_RESOURCE_DIMENSION_TEXTURE2D = 3

# name -> (fourcc, dxgi format, bytes per 4x4 block)
FORMATS = {
    "BC1": (b"DXT1", 71, 8),     # DXGI_FORMAT_BC1_UNORM
    "BC3": (b"DXT5", 77, 16),    # DXGI_FORMAT_BC3_UNORM
    "BC7": (b"DX10", 98, 16),    # DXGI_FORMAT_BC7_UNORM (DX10 extended header)
}
DXGI_TO_NAME = {v[1]: k for k, v in FORMATS.items()}
FOURCC_TO_NAME = {v[0]: k for k, v in FORMATS.items() if v[0] != b"DX10"}


def mip_count(width: int, height: int) -> int:
    return int(np.floor(np.log2(max(width, height)))) + 1


def level_bytes(width: int, height: int, block_bytes: int) -> int:
    return max(1, (width + 3) // 4) * max(1, (height + 3) // 4) * block_bytes


# --- mip generation --------------------------------------------------------------------


def _half(img: np.ndarray) -> np.ndarray:
    """2x2 box average (sizes are powers of two; a 1-px dimension stays 1)."""
    h, w = img.shape[:2]
    h2, w2 = max(1, h // 2), max(1, w // 2)
    return cv2.resize(img, (w2, h2), interpolation=cv2.INTER_AREA)


def mips_colour(rgba: np.ndarray) -> list[np.ndarray]:
    """Full mip chain for an sRGB(A) uint8 image: RGB averaged in linear light, alpha linearly."""
    lin = srgb_to_linear(rgba[..., :3] / 255.0).astype(np.float32)
    alpha = rgba[..., 3:].astype(np.float32) / 255.0 if rgba.shape[2] == 4 else None
    out = [rgba]
    while lin.shape[0] > 1 or lin.shape[1] > 1:
        lin = _half(lin)
        rgb = np.clip(np.rint(linear_to_srgb(lin) * 255), 0, 255).astype(np.uint8)
        if alpha is not None:
            alpha = _half(alpha).reshape(lin.shape[0], lin.shape[1], 1)
            a8 = np.clip(np.rint(alpha * 255), 0, 255).astype(np.uint8)
            out.append(np.dstack([rgb, a8]))
        else:
            out.append(rgb)
    return out


def mips_normal(rgba: np.ndarray) -> list[np.ndarray]:
    """Full mip chain for a tangent-space normal map: vectors averaged and renormalised,
    alpha (specular mask) averaged."""
    n = rgba[..., :3].astype(np.float32) / 127.5 - 1.0
    a = rgba[..., 3].astype(np.float32) / 255.0
    out = [rgba]
    while n.shape[0] > 1 or n.shape[1] > 1:
        n = _half(n).reshape(max(1, n.shape[0] // 2), max(1, n.shape[1] // 2), 3)
        a = _half(a).reshape(n.shape[:2])
        length = np.linalg.norm(n, axis=2, keepdims=True)
        unit = np.where(length > 1e-6, n / np.maximum(length, 1e-6), np.array([0, 0, 1.0], np.float32))
        rgb = np.clip(np.rint((unit + 1.0) * 127.5), 0, 255).astype(np.uint8)
        out.append(np.dstack([rgb, np.clip(np.rint(a * 255), 0, 255).astype(np.uint8)]))
    return out


# --- encoding ---------------------------------------------------------------------------


def _pad_to_blocks(rgba: np.ndarray) -> np.ndarray:
    h, w = rgba.shape[:2]
    ph, pw = (-h) % 4, (-w) % 4
    if ph or pw:
        rgba = np.pad(rgba, ((0, ph), (0, pw), (0, 0)), mode="edge")
    return np.ascontiguousarray(rgba)


def encode_level(rgba: np.ndarray, fmt: str) -> bytes:
    """Encode one RGBA uint8 level (any size; padded to whole 4x4 blocks)."""
    if rgba.shape[2] == 3:
        rgba = np.dstack([rgba, np.full(rgba.shape[:2], 255, np.uint8)])
    img = _pad_to_blocks(rgba)
    h, w = img.shape[:2]
    if fmt == "BC7":
        import etcpak

        params = etcpak.BC7CompressBlockParams()
        params.m_uber_level = 1
        return etcpak.compress_bc7(img.tobytes(), w, h, params)
    from quicktex import RawTexture
    from quicktex.s3tc.bc1 import BC1Encoder
    from quicktex.s3tc.bc3 import BC3Encoder

    enc = {"BC1": BC1Encoder, "BC3": BC3Encoder}[fmt](14)
    return bytes(enc.encode(RawTexture.frombytes(img.tobytes(), w, h)))


def decode_level(data: bytes, width: int, height: int, fmt: str) -> np.ndarray:
    """Decode one level with texture2ddecoder -> RGBA uint8 (height, width, 4)."""
    import texture2ddecoder as t2d

    pw, ph = max(4, (width + 3) // 4 * 4), max(4, (height + 3) // 4 * 4)
    fn = {"BC1": t2d.decode_bc1, "BC3": t2d.decode_bc3, "BC7": t2d.decode_bc7}[fmt]
    bgra = np.frombuffer(fn(data, pw, ph), np.uint8).reshape(ph, pw, 4)
    return bgra[:height, :width, [2, 1, 0, 3]]


# --- container -----------------------------------------------------------------------------


def build_header(width: int, height: int, mips: int, fmt: str) -> bytes:
    fourcc, dxgi, block = FORMATS[fmt]
    flags = DDSD_CAPS | DDSD_HEIGHT | DDSD_WIDTH | DDSD_PIXELFORMAT | DDSD_MIPMAPCOUNT | DDSD_LINEARSIZE
    caps = DDSCAPS_TEXTURE | (DDSCAPS_COMPLEX | DDSCAPS_MIPMAP if mips > 1 else 0)
    pixfmt = struct.pack("<II4s5I", 32, DDPF_FOURCC, fourcc, 0, 0, 0, 0, 0)
    header = struct.pack("<7I", 124, flags, height, width, level_bytes(width, height, block), 0, mips)
    header += b"\0" * 44 + pixfmt + struct.pack("<5I", caps, 0, 0, 0, 0)
    assert len(header) == 124
    out = DDS_MAGIC + header
    if fourcc == b"DX10":
        out += struct.pack("<5I", dxgi, D3D10_RESOURCE_DIMENSION_TEXTURE2D, 0, 1, 0)
    return out


def write_dds(path, levels: list[np.ndarray], fmt: str) -> list[bytes]:
    """Encode every level and write the file; returns the encoded level payloads."""
    h, w = levels[0].shape[:2]
    payloads = [encode_level(lv, fmt) for lv in levels]
    for lv, p in zip(levels, payloads):
        assert len(p) == level_bytes(lv.shape[1], lv.shape[0], FORMATS[fmt][2]), (lv.shape, len(p))
    with open(path, "wb") as f:
        f.write(build_header(w, h, len(levels), fmt))
        for p in payloads:
            f.write(p)
    return payloads


@dataclass
class DDSInfo:
    width: int
    height: int
    mip_count: int
    fmt: str
    flags: int
    caps: int
    pitch_or_linear_size: int
    fourcc: bytes
    dxgi_format: int | None
    data_offset: int
    level_sizes: list = field(default_factory=list)
    file_size: int = 0


def parse_dds(data: bytes) -> DDSInfo:
    """Parse and sanity-check a DDS header (BC1/BC3 FourCC or DX10 BC1/BC3/BC7)."""
    if data[:4] != DDS_MAGIC:
        raise ValueError("bad magic")
    size, flags, height, width, pitch, depth, mips = struct.unpack("<7I", data[4:32])
    if size != 124:
        raise ValueError(f"dwSize {size} != 124")
    pf_size, pf_flags, fourcc = struct.unpack("<II4s", data[76:88])
    if pf_size != 32 or not pf_flags & DDPF_FOURCC:
        raise ValueError("pixel format is not FourCC-compressed")
    caps = struct.unpack("<I", data[108:112])[0]
    offset = 128
    dxgi = None
    if fourcc == b"DX10":
        dxgi, dim, _misc, array_size, _misc2 = struct.unpack("<5I", data[128:148])
        if dim != D3D10_RESOURCE_DIMENSION_TEXTURE2D or array_size != 1:
            raise ValueError("DX10 header is not a single 2D texture")
        fmt = DXGI_TO_NAME[dxgi]
        offset = 148
    else:
        fmt = FOURCC_TO_NAME[fourcc]
    if not flags & DDSD_MIPMAPCOUNT:
        mips = 1
    block = FORMATS[fmt][2]
    sizes, w, h = [], width, height
    for _ in range(mips):
        sizes.append(level_bytes(w, h, block))
        w, h = max(1, w // 2), max(1, h // 2)
    return DDSInfo(width, height, mips, fmt, flags, caps, pitch, fourcc, dxgi, offset, sizes, len(data))


def read_levels(data: bytes, info: DDSInfo) -> list[bytes]:
    out, pos = [], info.data_offset
    for s in info.level_sizes:
        out.append(data[pos:pos + s])
        pos += s
    if pos != len(data):
        raise ValueError(f"payload size mismatch: expected {pos} bytes, file has {len(data)}")
    return out


def psnr(a: np.ndarray, b: np.ndarray) -> float:
    mse = float(np.mean((a.astype(np.float64) - b.astype(np.float64)) ** 2))
    return 100.0 if mse == 0 else min(100.0, 10 * np.log10(255.0 ** 2 / mse))  # 100 = identical
