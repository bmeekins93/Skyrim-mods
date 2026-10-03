"""Project paths and config loading."""
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
INPUT = ROOT / "input"
WORK = ROOT / "work"
DIST = ROOT / "dist"
THIRD = ROOT / "third_party"
PYNIFLY = THIRD / "PyNifly"
PYNIFLY_TESTS = PYNIFLY / "tests" / "tests"
SKELETONS = PYNIFLY / "io_scene_nifly" / "skeletons" / "Skyrim"
MOD_NAME = "CardboardFaceMask"
MOD_DIST = DIST / MOD_NAME


def load_config():
    with open(ROOT / "config.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f)
