"""One-command build: photo -> silhouette -> textures -> DDS -> mesh -> NIF -> ESP -> package -> tests.

Stages (run in this order, each a separate script run with this interpreter from the repo root):
  segment   src/01_segment.py                       work/outline.json (ML models, slow on CPU)
  textures  src/02_textures.py                      work/textures/*.png, work/atlas_layout.json
  dds       src/05_dds.py                           work/dds/*.dds
  mesh      src/03_mesh.py                          work/mesh/*.npz
  nif       src/04_export_nif.py                    work/nif/*.nif, work/nif_manifest.json
  plugin    src/plugin/build_plugin.py --regenerate work/plugin/CardboardFaceMask.esp (.NET + Spriggit)
  package   src/07_package.py                       dist/CardboardFaceMask/, dist/CardboardFaceMask.zip
  test      python -m pytest tests                  all acceptance tests

Examples (from the repo root):
  .venv/bin/python src/build_all.py                    # everything
  .venv/bin/python src/build_all.py --skip-ml          # reuse work/outline.json, skip segmentation
  .venv/bin/python src/build_all.py --from mesh        # mesh, nif, plugin, package, test
  .venv/bin/python src/build_all.py --only package,test
  .venv/bin/python src/build_all.py --list
Stage names may also be given by number (1-8) or by script stem (e.g. 04_export_nif).

When the segment stage does not run (--skip-ml, --from textures, ...) but textures or mesh do, the
existing work/outline.json must match config.yaml (silhouette_mode, source_image, photo size) and
work/matte.png must exist; otherwise the build stops before any stage runs. work/ is not in git,
so the first build of a fresh clone needs the segment stage (or both files copied over).
When the package stage is selected, the SPID ini settings in config.yaml (plugin_name,
chance_percent, equip_method, ...) are checked before any stage runs, too.
"""
from __future__ import annotations

import argparse
import importlib.util
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUTLINE = ROOT / "work" / "outline.json"
OUTLINE_READERS = {"textures", "mesh"}   # stages that read work/outline.json directly


@dataclass(frozen=True)
class Stage:
    name: str
    script: str | None          # repo-relative script path; None for pytest
    args: tuple[str, ...] = ()
    produces: str = ""
    aliases: tuple[str, ...] = field(default=())

    def command(self, pytest_args: list[str]) -> list[str]:
        if self.script is None:
            return [sys.executable, "-m", "pytest", "tests", *pytest_args]
        return [sys.executable, self.script, *self.args]


STAGES = [
    Stage("segment", "src/01_segment.py", produces="work/outline.json", aliases=("01_segment",)),
    Stage("textures", "src/02_textures.py", produces="work/textures/", aliases=("02_textures",)),
    Stage("dds", "src/05_dds.py", produces="work/dds/", aliases=("05_dds",)),
    Stage("mesh", "src/03_mesh.py", produces="work/mesh/", aliases=("03_mesh",)),
    Stage("nif", "src/04_export_nif.py", produces="work/nif/", aliases=("04_export_nif",)),
    Stage("plugin", "src/plugin/build_plugin.py", ("--regenerate",), "work/plugin/",
          aliases=("build_plugin",)),
    Stage("package", "src/07_package.py", produces="dist/", aliases=("07_package",)),
    Stage("test", None, produces="test report", aliases=("pytest", "tests")),
]


class BuildError(RuntimeError):
    pass


def resolve(token: str) -> int:
    """Stage index for a name, alias or 1-based number."""
    token = token.strip()
    for i, stage in enumerate(STAGES):
        if token == stage.name or token in stage.aliases or token == str(i + 1):
            return i
    names = ", ".join(s.name for s in STAGES)
    raise BuildError(f"unknown stage {token!r} (choose from: {names})")


def select(args) -> list[Stage]:
    if args.only and args.from_stage:
        raise BuildError("use either --only or --from, not both")
    if args.only:
        wanted = {resolve(t) for t in args.only.split(",") if t.strip()}
        chosen = [s for i, s in enumerate(STAGES) if i in wanted]
    else:
        start = resolve(args.from_stage) if args.from_stage else 0
        chosen = STAGES[start:]
    if args.skip_ml:
        chosen = [s for s in chosen if s.name != "segment"]
        if not OUTLINE.exists():
            raise BuildError("--skip-ml needs work/outline.json and work/matte.png from an earlier "
                             "segment run (work/ is not in git, so a fresh clone has neither): run "
                             "once without --skip-ml (or `--only segment`), or copy both files from a "
                             "machine that ran it")
    if not chosen:
        raise BuildError("no stages selected")
    return chosen


def package_module():
    """src/07_package.py holds the shared build-consistency checks (loaded by path: the file name
    starts with a digit)."""
    spec = importlib.util.spec_from_file_location("package07", ROOT / "src" / "07_package.py")
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception as e:  # noqa: BLE001 - report any import problem as a build error
        raise BuildError(f"cannot load src/07_package.py for the outline check: {e}") from e
    return module


def check_outline(stages: list[Stage]) -> None:
    """Without the segment stage, textures/mesh reuse work/outline.json: it must match config.yaml,
    or the print (cut for the outline's mode) and the mesh (cut for config's mode) disagree."""
    names = {s.name for s in stages}
    if "segment" in names or not names & OUTLINE_READERS:
        return
    package = package_module()
    problems = package.outline_problems(package.load_config())
    if problems:
        raise BuildError("work/outline.json does not match config.yaml:\n  " + "\n  ".join(problems)
                         + "\nRun the segment stage first: build without --skip-ml, or `--only segment`.")


def check_package_config(stages: list[Stage]) -> None:
    """A config.yaml value the package stage rejects (e.g. equip_method: KEF) should stop the build
    before the long stages run, not after them."""
    if "package" not in {s.name for s in stages}:
        return
    package = package_module()
    try:
        package.make_distr_ini(package.load_config())
    except package.PackageError as e:
        raise BuildError(f"config.yaml: {e}") from e


def check_scripts(stages: list[Stage]) -> None:
    missing = [s.script for s in stages if s.script and not (ROOT / s.script).is_file()]
    if missing:
        raise BuildError("stage script(s) not found: " + ", ".join(missing)
                         + " (is the checkout complete?)")


def run_stage(index: int, total: int, stage: Stage, pytest_args: list[str]) -> float:
    cmd = stage.command(pytest_args)
    shown = " ".join(shlex.quote(c) for c in [Path(cmd[0]).name, *cmd[1:]])
    print(f"\n{'=' * 78}\n==> [{index}/{total}] {stage.name}: {shown}\n{'=' * 78}", flush=True)
    start = time.monotonic()
    proc = subprocess.run(cmd, cwd=ROOT)
    elapsed = time.monotonic() - start
    if proc.returncode != 0:
        raise BuildError(f"stage '{stage.name}' failed (exit code {proc.returncode}) after "
                         f"{elapsed:.1f} s: {shown}")
    print(f"<== {stage.name} OK in {elapsed:.1f} s", flush=True)
    return elapsed


def print_summary(results: list[tuple[str, float | None, str]]) -> None:
    if not results:   # failed before any stage ran
        return
    print(f"\n{'stage':<10} {'status':<8} {'time':>9}")
    for name, seconds, status in results:
        t = f"{seconds:8.1f}s" if seconds is not None else "        -"
        print(f"{name:<10} {status:<8} {t}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter,
                                 epilog="\n".join(__doc__.split("\n")[2:]))
    ap.add_argument("--from", dest="from_stage", metavar="STAGE", help="start at this stage")
    ap.add_argument("--only", metavar="STAGES", help="comma-separated stages to run")
    ap.add_argument("--skip-ml", action="store_true",
                    help="skip segmentation and reuse the existing work/outline.json")
    ap.add_argument("--pytest-args", default="-q", help="arguments for the test stage (default: -q)")
    ap.add_argument("--list", action="store_true", help="list the stages and exit")
    args = ap.parse_args(argv)

    if args.list:
        for i, s in enumerate(STAGES, 1):
            cmd = " ".join([s.script or "-m pytest tests", *s.args])
            print(f"{i}. {s.name:<9} {cmd:<42} -> {s.produces}")
        return 0
    if ".venv" not in Path(sys.executable).parts:
        print(f"warning: running with {sys.executable}; the project expects .venv/bin/python "
              "(see tools/setup.sh)", file=sys.stderr)

    results: list[tuple[str, float | None, str]] = []
    total_start = time.monotonic()
    try:
        stages = select(args)
        check_scripts(stages)
        check_outline(stages)
        check_package_config(stages)
        for i, stage in enumerate(stages, 1):
            try:
                results.append((stage.name, run_stage(i, len(stages), stage, shlex.split(args.pytest_args)), "ok"))
            except BuildError:
                results.append((stage.name, None, "FAILED"))
                raise
    except BuildError as e:
        print_summary(results)
        print(f"\nBUILD FAILED: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print_summary(results)
        print("\nBUILD INTERRUPTED", file=sys.stderr)
        return 130
    print_summary(results)
    print(f"\nbuild OK in {time.monotonic() - total_start:.1f} s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
