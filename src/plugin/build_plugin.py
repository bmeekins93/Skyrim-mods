"""Build CardboardFaceMask.esp.

Default:        Spriggit YAML (src/plugin/yaml) -> spriggit deserialize -> work/plugin/spriggit/<plugin>
                -> generator `canonicalize` (records in FormID order) -> work/plugin/CardboardFaceMask.esp
--regenerate:   config.yaml + work/nif_manifest.json -> Mutagen generator -> work/plugin/generated/<plugin>
                -> spriggit serialize -> src/plugin/yaml (replaced)
                -> the default build above
                -> round-trip gates:
                   (a) generated ESP and rebuilt ESP are byte-identical
                   (b) re-serializing the rebuilt ESP reproduces src/plugin/yaml exactly

Why canonicalize: Spriggit reads the record files of a GRUP in directory-listing order
(Mutagen.Bethesda.Serialization GroupParallelHelper.ReadFilePerRecord; the Skyrim YAML translation
does not number its files), which differs between filesystems and checkouts. The engine ignores
record order, but the shipped bytes must not depend on the machine. Canonicalizing is a Mutagen
re-write; this script checks it changed nothing but the order (records_modulo_order).

Run from the repo root:  .venv/bin/python src/plugin/build_plugin.py [--regenerate]
Needs the .NET SDK (10.x for the generator) and the local Spriggit tool (.config/dotnet-tools.json).
"""
from __future__ import annotations

import argparse
import filecmp
import hashlib
import json
import re
import shutil
import struct
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cfm.paths import ROOT, WORK, load_config  # noqa: E402

PLUGIN_SRC = ROOT / "src" / "plugin"
YAML_DIR = PLUGIN_SRC / "yaml"
GENERATOR_PROJ = PLUGIN_SRC / "Generator" / "Generator.csproj"
GENERATOR_DLL = WORK / "plugin" / "dotnet" / "bin" / "Generator" / "release" / "CardboardFaceMask.Generator.dll"
TOOL_MANIFEST = ROOT / ".config" / "dotnet-tools.json"
VANILLA_JSON = PLUGIN_SRC / "vanilla_formkeys.json"  # vanilla FormKeys the generator used (for tests)
OUT_DIR = WORK / "plugin"
MANIFEST = WORK / "nif_manifest.json"

SPRIGGIT_TRANSLATION = "Spriggit.Yaml.Skyrim"
GAME_RELEASE = "SkyrimSE"

# Anything that looks like a machine-specific path must never reach the committed YAML.
LOCAL_PATH_RE = re.compile(r"(/home/|/root/|/tmp/|/Users/|/mnt/|[A-Za-z]:[\\/]|\\\\[A-Za-z])")


class BuildError(RuntimeError):
    pass


# --------------------------------------------------------------------------- tools

def run(cmd: list[str], *, quiet: bool = False) -> str:
    """Run a command from the repo root; raise BuildError with its output on failure."""
    proc = subprocess.run(cmd, cwd=ROOT, text=True, capture_output=True)
    if proc.returncode != 0:
        raise BuildError(f"command failed ({proc.returncode}): {' '.join(cmd)}\n{proc.stdout}\n{proc.stderr}")
    if not quiet and proc.stdout:
        print(proc.stdout, end="")
    return proc.stdout


def require_dotnet() -> None:
    if shutil.which("dotnet") is None:
        raise BuildError("`dotnet` not found on PATH. Install the .NET SDK (10.x for the generator; "
                         "Spriggit.CLI also runs on .NET 10) from https://dotnet.microsoft.com/download")


def spriggit_version() -> str:
    """Pinned Spriggit version; the YAML translation package uses the same version as the CLI."""
    try:
        tools = json.loads(TOOL_MANIFEST.read_text(encoding="utf-8"))["tools"]
        return tools["spriggit.cli"]["version"]
    except (OSError, KeyError, ValueError) as e:
        raise BuildError(f"cannot read the Spriggit pin from {TOOL_MANIFEST}: {e}") from e


def require_spriggit() -> str:
    require_dotnet()
    version = spriggit_version()
    try:
        run(["dotnet", "tool", "restore"], quiet=True)
        listing = run(["dotnet", "tool", "list", "--local"], quiet=True)
    except BuildError as e:
        raise BuildError("Spriggit CLI is not available. From the repo root run `dotnet tool restore` "
                         f"(needs nuget.org; pin in {TOOL_MANIFEST.relative_to(ROOT)}).\n{e}") from e
    if not re.search(rf"^spriggit\.cli\s+{re.escape(version)}\s", listing, re.MULTILINE | re.IGNORECASE):
        raise BuildError(f"local tool spriggit.cli {version} not installed; `dotnet tool list --local` says:\n{listing}")
    return version


def spriggit_deserialize(yaml_dir: Path, out_esp: Path) -> None:
    # -b 0: no backup copies of the previous ESP in the temp folder.
    run(["dotnet", "tool", "run", "spriggit", "deserialize", "-i", str(yaml_dir), "-o", str(out_esp), "-b", "0"],
        quiet=True)


def spriggit_serialize(esp: Path, out_dir: Path, version: str) -> None:
    if out_dir.exists():
        shutil.rmtree(out_dir)
    run(["dotnet", "tool", "run", "spriggit", "serialize", "-i", str(esp), "-o", str(out_dir),
         "-g", GAME_RELEASE, "-p", SPRIGGIT_TRANSLATION, "-v", version], quiet=True)


_generator_built = False


def build_generator() -> None:
    """Build the Mutagen generator once per run (NuGet restore on first use)."""
    global _generator_built
    if _generator_built:
        return
    require_dotnet()
    run(["dotnet", "build", str(GENERATOR_PROJ), "-c", "Release", "--nologo", "-v", "q"], quiet=True)
    if not GENERATOR_DLL.exists():
        raise BuildError(f"generator build did not produce {GENERATOR_DLL}")
    _generator_built = True


def run_generator(*args: str, quiet: bool = False) -> str:
    return run(["dotnet", str(GENERATOR_DLL), *args], quiet=quiet)


# --------------------------------------------------------------------------- file checks

def tree_files(root: Path) -> dict[str, Path]:
    return {p.relative_to(root).as_posix(): p for p in sorted(root.rglob("*")) if p.is_file()}


def tree_differences(a: Path, b: Path) -> list[str]:
    fa, fb = tree_files(a), tree_files(b)
    diffs = [f"only in {a.name}: {k}" for k in fa.keys() - fb.keys()]
    diffs += [f"only in {b.name}: {k}" for k in fb.keys() - fa.keys()]
    diffs += [f"content differs: {k}" for k in fa.keys() & fb.keys() if not filecmp.cmp(fa[k], fb[k], shallow=False)]
    return sorted(diffs)


def replace_tree(src: Path, dst: Path) -> None:
    """Make dst an exact copy of src (stale files removed)."""
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src, dst)


def check_yaml_portable(yaml_dir: Path) -> None:
    for rel, path in tree_files(yaml_dir).items():
        text = path.read_text(encoding="utf-8")
        hit = LOCAL_PATH_RE.search(text)
        if hit:
            raise BuildError(f"{rel} contains a machine-specific path fragment {hit.group(0)!r}")


def split_records(esp: Path) -> tuple[bytes, dict[bytes, tuple[bytes, list[bytes]]]]:
    """Split a plugin into TES4 bytes and, per top-level GRUP, (group header, record blobs).

    Record/GRUP headers are 24 bytes (UESP "Skyrim Mod:Mod File Format"); a record's data size
    is at offset 4, a GRUP's total size (header included) at offset 4 and its label at offset 8.
    """
    data = esp.read_bytes()
    tes4_size = struct.unpack_from("<I", data, 4)[0]
    tes4, pos = data[:24 + tes4_size], 24 + tes4_size
    groups: dict[bytes, tuple[bytes, list[bytes]]] = {}
    while pos < len(data):
        if data[pos:pos + 4] != b"GRUP":
            raise BuildError(f"{esp.name}: expected GRUP at offset {pos}")
        size, label = struct.unpack_from("<I4s", data, pos + 4)
        records, rpos = [], pos + 24
        while rpos < pos + size:
            if data[rpos:rpos + 4] == b"GRUP":
                raise BuildError(f"{esp.name}: nested GRUPs are not expected in this plugin")
            rsize = struct.unpack_from("<I", data, rpos + 4)[0]
            records.append(data[rpos:rpos + 24 + rsize])
            rpos += 24 + rsize
        groups[label] = (data[pos:pos + 24], records)
        pos += size
    return tes4, groups


def records_modulo_order(a: Path, b: Path) -> bool:
    """True when both plugins hold byte-identical TES4, GRUP headers and records, ignoring only
    the order of records inside each GRUP (Spriggit rebuilds a group in directory-listing order)."""
    tes4_a, groups_a = split_records(a)
    tes4_b, groups_b = split_records(b)
    if tes4_a != tes4_b or list(groups_a) != list(groups_b):
        return False
    return all(groups_a[k][0] == groups_b[k][0] and sorted(groups_a[k][1]) == sorted(groups_b[k][1])
               for k in groups_a)


def quick_sanity(esp: Path) -> None:
    """Cheap structural check of a built plugin (the full contract lives in tests/test_plugin.py)."""
    tes4, groups = split_records(esp)
    flags = struct.unpack_from("<I", tes4, 8)[0]
    if tes4[:4] != b"TES4" or not flags & 0x200:
        raise BuildError(f"{esp}: TES4 header missing or ESL flag (0x200) not set")
    counts = {k.decode(): len(v[1]) for k, v in groups.items()}
    if counts != {"ARMO": 1, "ARMA": 2}:
        raise BuildError(f"{esp}: unexpected record groups {counts}")
    for label, (_, records) in groups.items():
        ids = [struct.unpack_from("<I", r, 12)[0] for r in records]  # FormID at record offset 12
        if ids != sorted(ids):
            raise BuildError(f"{esp}: GRUP {label.decode()} records are not in FormID order: "
                             + ", ".join(f"{i:08X}" for i in ids))


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# --------------------------------------------------------------------------- modes

def build_from_yaml(yaml_dir: Path = YAML_DIR, out_dir: Path = OUT_DIR) -> Path:
    """Default mode: Spriggit YAML -> ESP (Spriggit), then records put in FormID order (Mutagen)."""
    require_spriggit()
    build_generator()
    plugin_name = load_config()["plugin_name"]
    if not (yaml_dir / "spriggit-meta.json").exists():
        raise BuildError(f"{yaml_dir} has no Spriggit data; run with --regenerate first")
    raw = out_dir / "spriggit" / plugin_name   # Spriggit's own output, kept for inspection
    out_esp = out_dir / plugin_name
    raw.parent.mkdir(parents=True, exist_ok=True)
    spriggit_deserialize(yaml_dir, raw)
    run_generator("canonicalize", str(raw), str(out_esp), quiet=True)
    if not records_modulo_order(raw, out_esp):
        raise BuildError(f"canonicalizing {raw} changed more than the order of records")
    quick_sanity(out_esp)
    print(f"Built {display(out_esp)} from {display(yaml_dir)} (sha256 {sha256(out_esp)[:16]})")
    return out_esp


def display(path: Path) -> str:
    """Repo-relative path when possible (keeps messages short and machine-independent)."""
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def regenerate() -> None:
    """Generator -> YAML -> ESP, then both round-trip gates."""
    version = require_spriggit()
    build_generator()
    plugin_name = load_config()["plugin_name"]
    generated = OUT_DIR / "generated" / plugin_name
    if generated.exists():
        generated.unlink()
    dump = run_generator("generate", "--config", str(ROOT / "config.yaml"), "--manifest", str(MANIFEST),
                         "--out", str(generated), "--vanilla-json", str(VANILLA_JSON))
    (OUT_DIR / "generated" / "dump.txt").write_text(dump, encoding="utf-8")

    staged = OUT_DIR / "yaml_staged"
    spriggit_serialize(generated, staged, version)
    check_yaml_portable(staged)
    replace_tree(staged, YAML_DIR)
    shutil.rmtree(staged)
    print(f"Serialized {generated.relative_to(ROOT)} -> {YAML_DIR.relative_to(ROOT)}")

    rebuilt = build_from_yaml()
    gate_a(generated, rebuilt)
    gate_b(rebuilt, version)
    print("Round-trip gates: OK")


def gate_a(generated: Path, rebuilt: Path) -> None:
    """Generator output and the (canonicalized) Spriggit rebuild must be byte-identical."""
    if generated.read_bytes() == rebuilt.read_bytes():
        print(f"Gate (a): generated ESP and Spriggit-rebuilt ESP are byte-identical (sha256 {sha256(rebuilt)[:16]})")
        return
    detail = "only the record order differs" if records_modulo_order(generated, rebuilt) else "record bytes differ"
    try:
        verdict = run_generator("compare", str(generated), str(rebuilt), quiet=True).strip().splitlines()[-1]
    except BuildError:
        verdict = "records DIFFER"
    raise BuildError(f"Gate (a) failed: {display(rebuilt)} != {display(generated)} ({detail}; Mutagen: {verdict})")


def gate_b(rebuilt: Path, version: str) -> None:
    again = OUT_DIR / "yaml_roundtrip"
    spriggit_serialize(rebuilt, again, version)
    diffs = tree_differences(YAML_DIR, again)
    if diffs:
        raise BuildError("Gate (b) failed: re-serialized YAML differs:\n  " + "\n  ".join(diffs))
    shutil.rmtree(again)
    print("Gate (b): re-serializing the rebuilt ESP reproduces src/plugin/yaml exactly")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--regenerate", action="store_true",
                        help="rerun the Mutagen generator and rewrite src/plugin/yaml (then round-trip checks)")
    args = parser.parse_args()
    try:
        regenerate() if args.regenerate else build_from_yaml()
    except BuildError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
