#!/usr/bin/env python3
"""Fetch PyNifly + nifly at pinned commits and build NiflyDLL for Linux/macOS.

PyNifly only ships a Windows NiflyDLL.dll. Its C++ wrapper is portable apart from a
handful of Win32 calls, so this script applies small, asserted source patches and
builds a shared library with CMake. The result is copied to
third_party/PyNifly/io_scene_nifly/NiflyDLL.dll, the name PyNifly's ctypes loader
expects (ctypes loads an ELF/Mach-O fine regardless of the file name).

On Windows, skip this script: drop the official PyNifly release's NiflyDLL.dll into
third_party/PyNifly/io_scene_nifly/ instead (run with --fetch-only to get the sources).

Usage: python tools/nifly/build_niflydll.py [--fetch-only] [--jobs N]
"""
import argparse
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
THIRD = ROOT / "third_party"
PYNIFLY_URL = "https://github.com/BadDogSkyrim/PyNifly.git"
PYNIFLY_SHA = "bcb723aba2ca4a80e976d957efbde69b33f2e56f"  # 2026-09-11
NIFLY_URL = "https://github.com/ousnius/nifly.git"
NIFLY_SHA = "cca0a770094bb962fb28ea1fec5ea903e68fda8e"  # 2026-09-14

# Only what this project needs from the (1 GB) PyNifly repo.
PYNIFLY_SPARSE = [
    "NiflyDLL",
    "io_scene_nifly",
    "LICENSE",
    "tests/tests/SkyrimSE/malehead.nif",
    "tests/tests/SkyrimSE/maleheadargonian.nif",
    "tests/tests/SkyrimSE/helmet-SMP.nif",
    "tests/tests/SkyrimSE/skeleton_vanilla.nif",
    "tests/tests/SkyrimSE/skeletonbeast_female.nif",
    "tests/tests/SkyrimSE/CitrusFemHead.nif",
    "tests/tests/SkyrimSE/maleorchair27.nif",
    "tests/tests/SkyrimSE/meshes/actors/character/character assets/maleheadkhajiit.nif",
    "tests/tests/SkyrimSE/meshes/draugr lich01 hood.nif",
    "tests/tests/Skyrim/skeletonbeast_vanilla.nif",
]


def run(cmd, cwd=None):
    print("+", " ".join(str(c) for c in cmd), flush=True)
    subprocess.run([str(c) for c in cmd], cwd=cwd, check=True)


def fetch(url, sha, dest, sparse=None):
    if (dest / ".git").exists():
        head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=dest, capture_output=True, text=True).stdout.strip()
        if head == sha:
            print(f"{dest.name}: already at {sha[:10]}")
            return
    dest.mkdir(parents=True, exist_ok=True)
    if not (dest / ".git").exists():
        run(["git", "init", "-q"], cwd=dest)
        run(["git", "remote", "add", "origin", url], cwd=dest)
    if sparse:
        run(["git", "config", "core.sparseCheckout", "true"], cwd=dest)
        run(["git", "sparse-checkout", "init", "--no-cone"], cwd=dest)
        (dest / ".git" / "info" / "sparse-checkout").write_text("\n".join("/" + p for p in sparse) + "\n")
    run(["git", "fetch", "-q", "--depth", "1", "--filter=blob:none", "origin", sha], cwd=dest)
    run(["git", "checkout", "-q", "FETCH_HEAD"], cwd=dest)


def patch(path, old, new, count=1):
    text = path.read_text(encoding="utf-8", errors="surrogateescape")
    if new in text and old not in text:
        return  # already applied
    n = text.count(old)
    if n != count:
        sys.exit(f"patch failed for {path.name}: expected {count} occurrence(s), found {n}:\n{old[:200]}")
    path.write_text(text.replace(old, new), encoding="utf-8", errors="surrogateescape")


def stage_port(build_src):
    """Copy the current (root-level) NiflyDLL sources and make them POSIX-buildable."""
    dll = THIRD / "PyNifly" / "NiflyDLL"
    if build_src.exists():
        shutil.rmtree(build_src)
    (build_src / "src").mkdir(parents=True)
    (build_src / "include").mkdir()
    (build_src / "compat").mkdir()
    for f in ["Anim.cpp", "NiflyWrapper.cpp", "NiflyFunctions.cpp", "Logger.cpp"]:
        shutil.copy2(dll / f, build_src / "src" / f)
    for f in ["Anim.h", "NiflyWrapper.hpp", "NiflyFunctions.hpp", "Logger.hpp", "NiflyDefs.hpp", "pch.h"]:
        shutil.copy2(dll / f, build_src / "include" / f)
    shutil.copy2(Path(__file__).with_name("posix_compat.hpp"), build_src / "compat" / "posix_compat.hpp")
    (build_src / "include" / "framework.h").write_text(
        "#pragma once\n#if defined(_WIN32)\n#define WIN32_LEAN_AND_MEAN\n#include <windows.h>\n#endif\n")

    patch(build_src / "include" / "NiflyWrapper.hpp",
          "#ifdef NIFLYDLL_EXPORTS\n\t#define NIFLY_API __declspec(dllexport)\n#else\n\t#define NIFLY_API __declspec(dllimport)\n#endif",
          "#if defined(_WIN32)\n  #ifdef NIFLYDLL_EXPORTS\n\t#define NIFLY_API __declspec(dllexport)\n  #else\n"
          "\t#define NIFLY_API __declspec(dllimport)\n  #endif\n#else\n"
          "  #define NIFLY_API __attribute__((visibility(\"default\")))\n#endif")

    nf = build_src / "src" / "NiflyFunctions.cpp"
    text = nf.read_text(encoding="utf-8", errors="surrogateescape")
    start = text.index("void FindProjectRoot() {")
    end = text.index("\n}\n", start) + 3
    posix_root = (
        "void FindProjectRoot() {\n"
        "\tif (!projectRoot.empty()) return;\n"
        "\tDl_info info;\n"
        "\tif (dladdr((void*)&FindProjectRoot, &info) == 0 || !info.dli_fname) {\n"
        "\t\tniflydll::LogWrite(\"Failed to get the filename of the shared library\");\n"
        "\t\treturn;\n\t}\n"
        "\tprojectRoot = std::filesystem::path(info.dli_fname).parent_path();\n"
        "}\n")
    text = "#include <dlfcn.h>\n" + text[:start] + posix_root + text[end:]
    nf.write_text(text, encoding="utf-8", errors="surrogateescape")

    # Starfield-only geometry bounds API that public nifly does not have (yet).
    patch(nf,
          "\t\t\tbsGeom->SetGeometryBounds(\n\t\t\t\tBoundingSphere(Vector3(c.x * inv, c.y * inv, c.z * inv), radius * inv), minmax);",
          "#if defined(NIFLY_HAS_SF_BOUNDS)\n\t\t\tbsGeom->SetGeometryBounds(\n"
          "\t\t\t\tBoundingSphere(Vector3(c.x * inv, c.y * inv, c.z * inv), radius * inv), minmax);\n"
          "#else\n\t\t\t(void)minmax;\n#endif")
    nw = build_src / "src" / "NiflyWrapper.cpp"
    patch(nw,
          "    geom->SetGeometryBounds(BoundingSphere(Vector3(0.0f, 0.0f, 0.0f), 0.0f), infMinMax);",
          "#if defined(NIFLY_HAS_SF_BOUNDS)\n"
          "    geom->SetGeometryBounds(BoundingSphere(Vector3(0.0f, 0.0f, 0.0f), 0.0f), infMinMax);\n"
          "#else\n    (void)infMinMax;\n#endif")
    text = nw.read_text(encoding="utf-8", errors="surrogateescape")
    if "FillMemory(" in text:
        import re
        text = re.sub(r"FillMemory\((\w+), (sizeof\(\w+\)), 0\);", r"memset(\1, 0, \2);", text)
        nw.write_text(text, encoding="utf-8", errors="surrogateescape")
    if "FillMemory(" in nw.read_text(encoding="utf-8", errors="surrogateescape"):
        sys.exit("patch failed: FillMemory still present")

    # Headers are included with inconsistent case; give case-sensitive filesystems lowercase aliases.
    nifly_inc = THIRD / "nifly" / "include"
    for inc in list(nifly_inc.glob("*.hpp")) + list((build_src / "include").iterdir()):
        low = inc.name.lower()
        if low != inc.name:
            alias = build_src / "compat" / low
            if not alias.exists():
                alias.symlink_to(inc.resolve())

    (build_src / "CMakeLists.txt").write_text(f"""cmake_minimum_required(VERSION 3.16)
set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED True)
set(CMAKE_POSITION_INDEPENDENT_CODE ON)
project(NiflyDLLPosix LANGUAGES C CXX)
add_subdirectory("{(THIRD / 'nifly').as_posix()}" nifly)
add_library(nifly_dll SHARED src/Anim.cpp src/NiflyWrapper.cpp src/NiflyFunctions.cpp src/Logger.cpp)
set_target_properties(nifly_dll PROPERTIES OUTPUT_NAME NiflyDLL)
target_compile_definitions(nifly_dll PRIVATE NIFLYDLL_EXPORTS)
target_include_directories(nifly_dll PRIVATE include compat)
target_compile_options(nifly_dll PRIVATE -include ${{CMAKE_CURRENT_SOURCE_DIR}}/compat/posix_compat.hpp -w)
target_link_libraries(nifly_dll PRIVATE nifly ${{CMAKE_DL_LIBS}})
""")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--fetch-only", action="store_true")
    ap.add_argument("--jobs", type=int, default=os.cpu_count() or 2)
    args = ap.parse_args()

    fetch(PYNIFLY_URL, PYNIFLY_SHA, THIRD / "PyNifly", sparse=PYNIFLY_SPARSE)
    fetch(NIFLY_URL, NIFLY_SHA, THIRD / "nifly")
    if args.fetch_only:
        return
    if platform.system() == "Windows":
        sys.exit("On Windows use the official PyNifly NiflyDLL.dll (see README).")

    build_src = THIRD / "niflydll-posix"
    stage_port(build_src)
    build_dir = build_src / "build"
    run(["cmake", "-S", build_src, "-B", build_dir, "-DCMAKE_BUILD_TYPE=Release"])
    run(["cmake", "--build", build_dir, "-j", str(args.jobs)])
    lib = next(p for p in build_dir.iterdir() if p.name.startswith("libNiflyDLL.") and p.suffix in (".so", ".dylib"))
    target = THIRD / "PyNifly" / "io_scene_nifly" / "NiflyDLL.dll"
    shutil.copy2(lib, target)
    print(f"installed {lib.name} -> {target.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
