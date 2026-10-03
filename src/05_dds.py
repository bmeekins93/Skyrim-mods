"""Stage 3 - DDS: work/textures/*.png -> work/dds/*.dds (full mip chains) + work/dds/dds_report.json.

Formats (UNORM, like vanilla Skyrim SE):
  mask_d, twine_d   BC7 (DX10 header, DXGI_FORMAT_BC7_UNORM)   [--diffuse-format BC1 for DXT1]
  mask_n, twine_n   BC3 / DXT5 (alpha = specular mask)
Mips are generated here (diffuse: 2x2 box in linear light; normals: averaged + renormalised),
then each level is block-compressed. Every file is re-read and verified: header fields, mip
count == log2(size) + 1, payload size, per-level decode PSNR (texture2ddecoder) and an
independent decode of mip 0 with Pillow compared against the source PNG.

Run from the repo root:  .venv/bin/python src/05_dds.py
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent))
from cfm.img_dds import (DDSCAPS_TEXTURE, DDSD_CAPS, DDSD_HEIGHT, DDSD_LINEARSIZE,  # noqa: E402
                         DDSD_MIPMAPCOUNT, DDSD_PIXELFORMAT, DDSD_WIDTH, FORMATS, decode_level, mip_count,
                         mips_colour, mips_normal, parse_dds, psnr, read_levels, write_dds)
from cfm.paths import WORK  # noqa: E402

SRC = WORK / "textures"
OUT = WORK / "dds"
MIN_PSNR_DIFFUSE = 30.0
MIN_PSNR_NORMAL = 30.0


def jobs(diffuse_fmt: str):
    return [("mask_d", "colour", diffuse_fmt), ("twine_d", "colour", diffuse_fmt),
            ("mask_n", "normal", "BC3"), ("twine_n", "normal", "BC3")]


def verify(path: Path, levels: list[np.ndarray], fmt: str, kind: str) -> dict:
    data = path.read_bytes()
    info = parse_dds(data)
    h, w = levels[0].shape[:2]
    required = DDSD_CAPS | DDSD_HEIGHT | DDSD_WIDTH | DDSD_PIXELFORMAT | DDSD_MIPMAPCOUNT | DDSD_LINEARSIZE
    checks = {
        "magic": data[:4] == b"DDS ",
        "flags": (info.flags & required) == required,
        "caps_texture": bool(info.caps & DDSCAPS_TEXTURE),
        "size": (info.width, info.height) == (w, h),
        "power_of_two": (w & (w - 1)) == 0 and (h & (h - 1)) == 0,
        "linear_size": info.pitch_or_linear_size == info.level_sizes[0],
        "mip_count": info.mip_count == mip_count(w, h) == len(levels),
        "format": info.fmt == fmt and (info.fourcc == FORMATS[fmt][0]),
        "dxgi": info.dxgi_format == (FORMATS[fmt][1] if FORMATS[fmt][0] == b"DX10" else None),
    }
    payloads = read_levels(data, info)  # raises if the payload size is wrong
    checks["payload_size"] = True
    chans = slice(0, 3) if kind == "colour" else slice(0, 4)
    level_psnr = []
    for lv, p in zip(levels, payloads):
        dec = decode_level(p, lv.shape[1], lv.shape[0], fmt)
        src = lv if lv.shape[2] == 4 else np.dstack([lv, np.full(lv.shape[:2], 255, np.uint8)])
        level_psnr.append(round(float(psnr(dec[..., chans], src[..., chans])), 2))
    # independent reader: Pillow decodes mip 0 straight from the file
    with Image.open(path) as im:
        pil = np.asarray(im.convert("RGBA"))
    src0 = levels[0] if levels[0].shape[2] == 4 else np.dstack([levels[0], np.full((h, w), 255, np.uint8)])
    pil_psnr = round(float(psnr(pil[..., chans], src0[..., chans])), 2)
    t2d_vs_pil = round(float(psnr(pil[..., chans], decode_level(payloads[0], w, h, fmt)[..., chans])), 2)
    limit = MIN_PSNR_DIFFUSE if kind == "colour" else MIN_PSNR_NORMAL
    checks["psnr_mip0"] = pil_psnr > limit
    checks = {k: bool(v) for k, v in checks.items()}
    return {"file": str(path.relative_to(WORK.parent)), "format": fmt,
            "dxgi_format": info.dxgi_format, "fourcc": info.fourcc.decode(), "width": w, "height": h,
            "mip_count": info.mip_count, "bytes": len(data), "checks": checks, "all_levels_min_psnr": min(level_psnr),
            "psnr_mip0_pillow_vs_png": pil_psnr, "psnr_texture2ddecoder_vs_pillow": t2d_vs_pil,
            "psnr_per_level": level_psnr, "ok": all(checks.values())}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--diffuse-format", choices=["BC7", "BC1"], default="BC7")
    args = ap.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    report = {}
    for name, kind, fmt in jobs(args.diffuse_format):
        t0 = time.time()
        src = np.asarray(Image.open(SRC / f"{name}.png"))
        h, w = src.shape[:2]
        if (w & (w - 1)) or (h & (h - 1)):
            raise SystemExit(f"{name}.png is {w}x{h}, not a power of two")
        if kind == "normal" and src.shape[2] != 4:
            raise SystemExit(f"{name}.png must be RGBA (alpha = specular)")
        levels = mips_colour(src) if kind == "colour" else mips_normal(src)
        path = OUT / f"{name}.dds"
        write_dds(path, levels, fmt)
        report[name] = verify(path, levels, fmt, kind)
        r = report[name]
        print(f"{name}.dds {fmt} {w}x{h} mips={r['mip_count']} {r['bytes']} B  PSNR mip0 {r['psnr_mip0_pillow_vs_png']} dB "
              f"(levels {r['psnr_per_level'][:4]}...)  ok={r['ok']}  ({time.time() - t0:.1f}s)")
        if not r["ok"]:
            print("  failed checks:", [k for k, v in r["checks"].items() if not v])
    (OUT / "dds_report.json").write_text(json.dumps(report, indent=1), encoding="utf-8")
    return 0 if all(r["ok"] for r in report.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
