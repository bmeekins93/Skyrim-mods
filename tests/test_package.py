"""Tests for src/07_package.py (SPID ini generator, build-input gate, package validator, zip),
src/build_all.py (outline check wiring) and the .claude/hooks/block_game_data.{sh,py} safety hook.

The validator tests run against dist/CardboardFaceMask when it exists (skipped otherwise); the
negative tests break a temporary copy of it and expect the validator to fail loudly. The
build-input tests use small synthetic work/ folders, so they never depend on (or touch) work/.

Run: .venv/bin/python -m pytest tests/test_package.py
"""
from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from cfm.paths import MOD_DIST, MOD_NAME, load_config  # noqa: E402


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module   # dataclasses look their module up while the class is created
    spec.loader.exec_module(module)
    return module


pkg = _load("package07", ROOT / "src" / "07_package.py")
build_all = _load("build_all", ROOT / "src" / "build_all.py")

HOOK = ROOT / ".claude" / "hooks" / "block_game_data.py"
HOOK_SH = ROOT / ".claude" / "hooks" / "block_game_data.sh"
SETTINGS = ROOT / ".claude" / "settings.json"
BASE_CFG = {"plugin_name": "CardboardFaceMask.esp", "exclude_children": True, "chance_percent": 100}


# --------------------------------------------------------------------------- DISTR ini generator

def test_contract_line_matches_claude_md():
    """CLAUDE.md fixes the primary line; the generator must reproduce it for the default config."""
    line = pkg.distribution_line(BASE_CFG)
    assert line == "Item = 0x800~CardboardFaceMask.esp|ActorTypeNPC|NONE|NONE|-C|1|100"
    assert line in (ROOT / "CLAUDE.md").read_text(encoding="utf-8")


def test_line_follows_config():
    cfg = dict(BASE_CFG, exclude_children=False, chance_percent=12.5, plugin_name="Other.esp")
    assert pkg.distribution_line(cfg) == "Item = 0x800~Other.esp|ActorTypeNPC|NONE|NONE|NONE|1|12.5!"


@pytest.mark.parametrize("value,text", [(100, "100"), (100.0, "100"), (50, "50"), (0.01, "0.01"),
                                        (33.3, "33.3"), ("25", "25"), (0.000123, "0.000123")])
def test_format_chance(value, text):
    assert pkg.format_chance(value) == text


@pytest.mark.parametrize("value", [0, -5, 100.5, "abc", None, float("nan"), 1e-9])
def test_format_chance_rejects(value):
    with pytest.raises(pkg.PackageError):
        pkg.format_chance(value)


@pytest.mark.parametrize("value,field", [(100, "100"), (25, "25!"), (99.9, "99.9!"), (0.5, "0.5!")])
def test_chance_below_100_is_deterministic(value, field):
    """SPID re-rolls a plain chance on every launch; '!' fixes it per NPC and character."""
    assert pkg.chance_field(value) == field
    assert pkg.distribution_line(dict(BASE_CFG, chance_percent=value)).endswith("|" + field)


def test_ini_structure_as_spid_reads_it():
    """One active Item entry, 7 '|' sections, no [sections], ASCII, comments on their own lines."""
    text = pkg.make_distr_ini(BASE_CFG)
    text.encode("ascii")
    entries = pkg.parse_distr_ini(text)
    assert entries == [("Item", "0x800~CardboardFaceMask.esp|ActorTypeNPC|NONE|NONE|-C|1|100")]
    sections = entries[0][1].split("|")
    assert len(sections) == pkg.SPID_SECTIONS
    assert not any(line.lstrip().startswith("[") for line in text.splitlines())
    # SimpleIni has no inline comments: an active line must not carry a ';'
    for line in text.splitlines():
        if line and line[0] not in ";#":
            assert ";" not in line and "#" not in line


def test_ini_examples_are_valid_entries():
    """Each commented example, uncommented, parses as a 7-section Item line for our ARMO."""
    examples = [line.split("Item = ", 1)[1] for line in pkg.make_distr_ini(BASE_CFG).splitlines()
                if line.startswith(";") and "Item = 0x" in line]
    assert len(examples) == 3
    for value in examples:
        sections = value.split("|")
        assert len(sections) == 7
        assert sections[0] == "0x800~CardboardFaceMask.esp"
        assert value == re.sub(r"\s*([|,])\s*", r"\1", value)  # nothing SPID's sanitizer rewrites
    assert examples[-1].endswith("|25!")


def test_ini_name_has_spid_suffix():
    assert pkg.distr_ini_name("CardboardFaceMask.esp") == "CardboardFaceMask_DISTR.ini"


def test_parse_rejects_sections():
    with pytest.raises(pkg.PackageError):
        pkg.parse_distr_ini("[General]\nItem = 0x800~CardboardFaceMask.esp\n")


def test_game_path_normalisation():
    assert pkg.game_path("textures\\CardboardFaceMask\\mask_d.dds") == "textures/CardboardFaceMask/mask_d.dds"
    assert pkg.game_path("C:\\abs\\x.dds") is None
    assert pkg.game_path("..\\x.dds") is None


def test_readme_config_table_matches_config_yaml(config):
    """Every config.yaml key is documented in README's table with its current default."""
    import yaml
    documented = {}
    for line in (ROOT / "README.md").read_text(encoding="utf-8").splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 3 or not cells[0].startswith("`"):
            continue
        keys = [k.strip().strip("`") for k in cells[0].split(",")]
        values = [v.strip().strip("`") for v in cells[1].split(",")]
        if len(keys) == len(values) and all(k in config for k in keys):
            documented.update({k: yaml.safe_load(v) for k, v in zip(keys, values)})
    assert set(documented) == set(config), set(config) ^ set(documented)
    assert documented == config


# --------------------------------------------------------------------------- build-input gate

GATE_CFG = {"silhouette_mode": "face_hair_glasses", "source_image": "input/photo.png"}


@pytest.fixture()
def fake_build(tmp_path):
    """A minimal consistent work/ folder (+ photo) as stages 1-5 would leave it."""
    from PIL import Image
    root, work = tmp_path, tmp_path / "work"
    (work / "mesh").mkdir(parents=True)
    (root / "input").mkdir()
    Image.new("RGB", (40, 60)).save(root / "input" / "photo.png")
    Image.new("L", (40, 60)).save(work / "matte.png")
    records = {
        "outline.json": {"source": "input/photo.png", "image_size": [40, 60],
                         "selected_mode": "face_hair_glasses", "matte": "work/matte.png"},
        "atlas_layout.json": {"silhouette_mode": "face_hair_glasses"},
        "mesh/mesh_report.json": {"inputs": {"outline_is_fixture": False, "layout_is_fixture": False},
                                  "variants": {v: {"silhouette_mode": "face_hair_glasses",
                                                   "inputs_are_fixtures": False}
                                               for v in ("human_m", "beast_f")}},
        "nif_manifest.json": {"models": {}},
    }
    for t, (rel, data) in enumerate(records.items()):   # written in build order, increasing mtimes
        path = work / rel
        path.write_text(json.dumps(data), encoding="utf-8")
        os.utime(path, (1_000_000 + t, 1_000_000 + t))
    os.utime(work / "matte.png", (1_000_000, 1_000_000))
    return root, work


def _edit(path: Path, **changes):
    stat = path.stat()
    data = json.loads(path.read_text(encoding="utf-8"))
    data.update(changes)
    path.write_text(json.dumps(data), encoding="utf-8")
    os.utime(path, (stat.st_atime, stat.st_mtime))   # content change only, keep the build order


def test_gate_accepts_consistent_build(fake_build):
    root, work = fake_build
    assert pkg.build_problems(GATE_CFG, work, root) == []


def test_gate_stale_outline_mode(fake_build):
    """Reviewer scenario: config changed to face_only, build_all --skip-ml reused the outline."""
    root, work = fake_build
    problems = pkg.build_problems(dict(GATE_CFG, silhouette_mode="face_only"), work, root)
    text = "\n".join(problems)
    assert "outline.json was made for silhouette_mode 'face_hair_glasses'" in text
    assert "textures were cut for silhouette_mode 'face_hair_glasses'" in text
    assert "mesh human_m was cut for silhouette_mode 'face_hair_glasses'" in text


def test_gate_texture_mode_differs_from_mesh(fake_build):
    """Reverse scenario: textures cut for face_only, mesh for face_hair_glasses."""
    root, work = fake_build
    _edit(work / "atlas_layout.json", silhouette_mode="face_only")
    problems = pkg.build_problems(GATE_CFG, work, root)
    assert len(problems) == 1 and "textures were cut for silhouette_mode 'face_only'" in problems[0]


def test_gate_photo_changed(fake_build):
    root, work = fake_build
    from PIL import Image
    Image.new("RGB", (60, 40)).save(root / "input" / "photo.png")
    assert any("the photo changed" in p for p in pkg.build_problems(GATE_CFG, work, root))
    other = dict(GATE_CFG, source_image="input/other.png")
    assert any("source_image is 'input/other.png'" in p for p in pkg.outline_problems(other, work, root))


def test_gate_missing_matte_and_fixtures(fake_build):
    root, work = fake_build
    (work / "matte.png").unlink()
    _edit(work / "mesh" / "mesh_report.json", inputs={"outline_is_fixture": True})
    text = "\n".join(pkg.build_problems(GATE_CFG, work, root))
    assert "work/matte.png is missing" in text
    assert "synthetic test fixtures (outline_is_fixture)" in text


def test_gate_detects_skipped_stage(fake_build):
    """outline.json rewritten after the mesh was built: the mesh stage was not re-run."""
    root, work = fake_build
    os.utime(work / "outline.json", (2_000_000, 2_000_000))
    problems = pkg.build_problems(GATE_CFG, work, root)
    assert any("atlas_layout.json is older than work/outline.json" in p for p in problems)
    assert any("mesh_report.json is older than work/outline.json" in p and "--from mesh" in p
               for p in problems)


def test_gate_missing_outline(tmp_path):
    assert pkg.outline_problems(GATE_CFG, tmp_path, tmp_path) == [
        "work/outline.json is missing: run the segment stage (src/01_segment.py)"]


def test_remove_in_dist_refuses_outside_dist(tmp_path):
    with pytest.raises(pkg.PackageError):
        pkg.remove_in_dist(tmp_path / "x")


# --------------------------------------------------------------------------- build_all wiring

class _StubPackage:
    def __init__(self, problems):
        self.problems = problems

    @staticmethod
    def load_config():
        return {}

    def outline_problems(self, cfg):
        return self.problems


@pytest.mark.parametrize("names,checked", [
    (["textures", "dds", "mesh", "nif", "plugin", "package", "test"], True),   # --skip-ml
    (["mesh", "nif"], True),                                                     # --from mesh ... nif
    (["segment", "textures", "mesh"], False),                                    # segment refreshes it
    (["package", "test"], False),                                                # nothing reads it
])
def test_build_all_checks_outline_when_segment_is_skipped(monkeypatch, names, checked):
    stages = [s for s in build_all.STAGES if s.name in names]
    monkeypatch.setattr(build_all, "package_module", lambda: _StubPackage(["mode mismatch"]))
    if checked:
        with pytest.raises(build_all.BuildError, match="mode mismatch"):
            build_all.check_outline(stages)
    else:
        build_all.check_outline(stages)
    monkeypatch.setattr(build_all, "package_module", lambda: _StubPackage([]))
    build_all.check_outline(stages)


def test_build_all_real_outline_check_runs():
    """The real check loads src/07_package.py and returns a list (contents depend on work/)."""
    module = build_all.package_module()
    assert isinstance(module.outline_problems(module.load_config()), list)


# --------------------------------------------------------------------------- package validator

@pytest.fixture(scope="module")
def config():
    return load_config()


@pytest.fixture(scope="module")
def dist_dir():
    if not (MOD_DIST / load_config()["plugin_name"]).exists():
        pytest.skip("dist/ not built (run src/07_package.py)")
    return MOD_DIST


def test_dist_package_is_valid(dist_dir, config):
    report = pkg.validate_package(dist_dir, config)
    paths = [f["path"] for f in report["files"]]
    assert config["plugin_name"] in paths
    assert pkg.distr_ini_name(config["plugin_name"]) in paths
    assert any(p.startswith(f"meshes/{MOD_NAME}/") for p in paths)
    assert any(p.startswith(f"textures/{MOD_NAME}/") for p in paths)
    assert report["distr_lines"] == [pkg.distribution_line(config)]


def test_dist_zip_matches_folder(dist_dir):
    if not pkg.ZIP_PATH.exists():
        pytest.skip("dist zip not built")
    info = pkg.verify_zip(pkg.ZIP_PATH, dist_dir)
    with zipfile.ZipFile(pkg.ZIP_PATH) as zf:
        names = zf.namelist()
    assert info["entries"] == len(names)
    # Data-relative: the plugin sits at the zip root, assets under meshes/ and textures/
    assert all(n.split("/")[0] in ("meshes", "textures") or "/" not in n for n in names)


def test_no_staging_leftovers(dist_dir):
    assert not pkg.STAGING.exists()


@pytest.fixture()
def broken_copy(dist_dir, tmp_path):
    copy = tmp_path / MOD_NAME
    shutil.copytree(dist_dir, copy)
    return copy


def _expect_failure(pkg_dir, config, needle):
    with pytest.raises(pkg.PackageError) as err:
        pkg.validate_package(pkg_dir, config)
    assert needle in str(err.value), str(err.value)


def test_detects_stray_file(broken_copy, config):
    (broken_copy / "readme.txt").write_text("x")
    _expect_failure(broken_copy, config, "stray file readme.txt")


def test_detects_missing_texture(broken_copy, config):
    (broken_copy / "textures" / MOD_NAME / "mask_n.dds").unlink()
    _expect_failure(broken_copy, config, "mask_n.dds is not in the package")


def test_detects_wrong_case_texture(broken_copy, config):
    tex = broken_copy / "textures" / MOD_NAME
    (tex / "twine_d.dds").rename(tex / "Twine_D.dds")
    _expect_failure(broken_copy, config, "twine_d.dds is not in the package (exact case)")


def test_detects_texture_in_unflagged_slot(broken_copy, config):
    """A path in slot 3 (glow) without the GLOW_MAP shader flag is invisible to pyn's
    shape.textures; the validator reads all nine slots."""
    from cfm.nifenv import NifFile
    path = sorted((broken_copy / "meshes" / MOD_NAME).glob("*.nif"))[0]
    nif = NifFile(str(path))
    shape = nif.shapes[0]
    shape.set_texture("Glow", "textures\\CardboardFaceMask\\does_not_exist.dds")
    nif.save()
    reread = NifFile(str(path)).shapes[0]
    assert "Glow" not in dict(reread.textures)        # the flag-based view misses it ...
    _expect_failure(broken_copy, config, "slot 3: textures/CardboardFaceMask/does_not_exist.dds "
                                         "is not in the package")   # ... the validator does not


def test_detects_missing_mesh(broken_copy, config):
    nif = sorted((broken_copy / "meshes" / MOD_NAME).glob("*.nif"))[0]
    nif.unlink()
    _expect_failure(broken_copy, config, f"meshes/{MOD_NAME}/{nif.name} is not in the package")


def test_detects_unreferenced_mesh(broken_copy, config):
    nif = sorted((broken_copy / "meshes" / MOD_NAME).glob("*.nif"))[0]
    shutil.copyfile(nif, nif.with_name("extra_mesh.nif"))
    _expect_failure(broken_copy, config, "stray mesh")


def test_detects_ini_for_other_plugin(broken_copy, config):
    ini = broken_copy / pkg.distr_ini_name(config["plugin_name"])
    ini.write_text(ini.read_text().replace("~CardboardFaceMask.esp|", "~Other.esp|"))
    _expect_failure(broken_copy, config, "entry targets 'Other.esp'")


def test_detects_ini_section(broken_copy, config):
    ini = broken_copy / pkg.distr_ini_name(config["plugin_name"])
    ini.write_text("[Main]\n" + ini.read_text())
    _expect_failure(broken_copy, config, "section header")


def test_zip_mismatch_detected(dist_dir, tmp_path):
    zip_path = tmp_path / "pkg.zip"
    pkg.write_zip(dist_dir, zip_path)
    with zipfile.ZipFile(zip_path, "a") as zf:
        zf.writestr("extra.txt", "x")
    with pytest.raises(pkg.PackageError):
        pkg.verify_zip(zip_path, dist_dir)


def test_zip_is_reproducible(dist_dir, tmp_path):
    a, b = tmp_path / "a.zip", tmp_path / "b.zip"
    pkg.write_zip(dist_dir, a)
    pkg.write_zip(dist_dir, b)
    assert a.read_bytes() == b.read_bytes()


# --------------------------------------------------------------------------- safety hook

STEAM = "C:\\Program Files (x86)\\Steam\\steamapps\\common\\Skyrim Special Edition\\Data"
BOTTLE = ("/Users/me/Library/Application Support/CrossOver/Bottles/Steam/drive_c/Program Files (x86)"
          "/Steam/steamapps/common/Skyrim Special Edition/Data")
GAME = BOTTLE.rsplit("/", 1)[0]
Q = f'"{BOTTLE}"'


def run_hook(payload, launcher: bool = False, env=None) -> subprocess.CompletedProcess:
    text = payload if isinstance(payload, str) else json.dumps(payload)
    cmd = ["bash", str(HOOK_SH)] if launcher else [sys.executable, str(HOOK)]
    return subprocess.run(cmd, input=text, capture_output=True, text=True, timeout=30, env=env)


@pytest.mark.parametrize("tool,key,path,blocked", [
    ("Write", "file_path", f"{BOTTLE}/CardboardFaceMask.esp", True),
    ("Edit", "file_path", f"{STEAM}\\CardboardFaceMask_DISTR.ini", True),
    ("MultiEdit", "file_path", f"{BOTTLE.lower()}/x.ini", True),
    ("NotebookEdit", "notebook_path", f"{BOTTLE}/n.ipynb", True),
    ("Write", "file_path", str(ROOT / "dist" / MOD_NAME / "x.txt"), False),
    ("Write", "file_path", "/tmp/Skyrim Special Edition/Data backup notes.txt", False),
    ("Write", "file_path", "/tmp/Skyrim Special Edition/DataFiles/x", False),
])
def test_hook_file_tools(tool, key, path, blocked):
    proc = run_hook({"tool_name": tool, "tool_input": {key: path, "content": "x"}, "cwd": str(ROOT)})
    assert proc.returncode == (2 if blocked else 0), proc.stderr
    if blocked:
        assert "Skyrim Data folder" in proc.stderr


def test_hook_follows_symlinks(tmp_path):
    data = tmp_path / "Skyrim Special Edition" / "Data"
    data.mkdir(parents=True)
    link = tmp_path / "innocent"
    link.symlink_to(data)
    proc = run_hook({"tool_name": "Write", "tool_input": {"file_path": str(link / "x.esp")}})
    assert proc.returncode == 2


@pytest.mark.parametrize("command,blocked", [
    # writes into Data
    (f"cp dist/CardboardFaceMask/CardboardFaceMask.esp {Q}/", True),
    (f"mv x.nif {Q}/meshes/", True),
    (f"rsync -a dist/CardboardFaceMask/ {Q}/", True),
    (f"cp -t {Q} a.esp b.ini", True),
    (f"cp -r dist/CardboardFaceMask/* ~/Games/Skyrim\\ Special\\ Edition/Data/", True),
    (f"echo hi > {Q}/x.ini", True),
    (f"cat a.ini >> '{STEAM}\\a.ini'", True),
    (f"echo hi | tee {Q}/x.ini", True),
    (f"unzip dist/CardboardFaceMask.zip -d {Q}", True),
    (f"mkdir -p {Q}/meshes", True),
    (f"rm {Q}/CardboardFaceMask.esp", True),
    (f"cd {Q} && cp /tmp/x.esp .", True),
    (f"cd {Q}; unzip /tmp/x.zip", True),
    (f"bash -c 'cp x.esp {BOTTLE}/'", True),
    (f"python3 -c \"open('{BOTTLE}/x','w')\"", True),
    (f"sudo cp x.esp {Q}", True),
    (f"find {Q} -name '*.esp' -delete", True),
    (f"cd /tmp\ncp x.esp {Q}/", True),
    (f"cd {Q}\nunzip /tmp/x.zip", True),
    # review findings: moves out of Data, substitutions, write-capable "readers", cd to the game folder
    (f"mv {Q}/Skyrim.esm /tmp/", True),
    (f"rsync -a --remove-source-files {Q}/ /tmp/bak/", True),
    (f"echo `cp x.esp {Q}/`", True),
    (f"echo \"$(cp x.esp '{BOTTLE}/')\"", True),
    (f"xxd -r hex.txt {Q}/x.esp", True),
    (f"tree -o {Q}/out.txt .", True),
    (f'cd "{GAME}" && cp x.esp Data/', True),
    (f'cd "{GAME}"; tar -xf a.tar -C ./Data', True),
    (f'mv "{GAME}" /tmp/old', True),
    # text piped or fed into something that runs it
    (f"echo \"cp x.esp '{BOTTLE}/'\" | bash", True),
    (f"find {Q} -name '*.bak' | xargs rm", True),
    (f"cat <<'EOF' | sh\ncp x {Q}/\nEOF", True),
    (f"python3 - <<'EOF'\nopen('{BOTTLE}/x', 'w')\nEOF", True),
    (f"bash <<< \"cp x '{BOTTLE}/'\"", True),
    (f"tee >(cat > {Q}/x.ini) < a.ini", True),
    (f"cp x \"$(echo {Q})/\"", True),
    (f"cp x $(echo {Q})", True),
    (f"cp x `echo {Q}`/sub", True),
    (f"echo \"$(cp x.esp '{BOTTLE}/'\"", True),   # unbalanced: cannot parse -> blocked
    # reads and text that only mentions the folder
    (f"cat > /tmp/notes.txt <<'EOF'\ninstall into {BOTTLE}\nEOF", False),
    (f"echo 'line one\ncp x {BOTTLE}' > /tmp/x.txt", False),
    (f"ls -la {Q}", False),
    (f"cat {Q}/CardboardFaceMask_DISTR.ini", False),
    (f"cat {Q}/x.ini | tee /tmp/a", False),
    (f"find {Q} -iname '*_DISTR.ini'", False),
    (f"cp {Q}/Skyrim.esm /tmp/backup/", False),
    (f"ls {Q} 2>/dev/null | head", False),
    (f"echo \"$(ls '{BOTTLE}')\"", False),
    (f"diff <(ls {Q}) <(ls dist/CardboardFaceMask)", False),
    (f"git commit -m \"docs: install into {BOTTLE}\"", False),
    (f"git commit -m \"$(cat <<'EOF'\ndocs: install into {BOTTLE}\n\nDon't forget the ini.\nEOF\n)\"", False),
    (f".venv/bin/python -m pytest tests -k \"{BOTTLE}\"", False),
    (f"python3 - <<'EOF'\nprint('Documents/My Games/Skyrim Special Edition/SKSE')\nEOF", False),
    (f"cd {Q} && cd /tmp && touch x", False),
    ("cp dist/CardboardFaceMask.zip /tmp/", False),
    ("echo hello > /tmp/x.txt", False),
    ("echo $((1 << 4)) && touch /tmp/x", False),
])
def test_hook_bash(command, blocked):
    proc = run_hook({"tool_name": "Bash", "tool_input": {"command": command}, "cwd": str(ROOT)})
    assert proc.returncode == (2 if blocked else 0), (command, proc.stderr)


@pytest.mark.parametrize("command,blocked", [("touch x.esp", True), ("cp /tmp/x.esp .", True),
                                             ("ls -la", False), ("cat x.ini", False)])
def test_hook_bash_cwd_inside_data(command, blocked):
    """The hook input's cwd may already be inside Data after an earlier `cd`."""
    proc = run_hook({"tool_name": "Bash", "tool_input": {"command": command}, "cwd": BOTTLE})
    assert proc.returncode == (2 if blocked else 0), (command, proc.stderr)


def test_hook_unparseable_input():
    assert run_hook(f"not json {BOTTLE}").returncode == 2
    assert run_hook("not json at all").returncode == 0


def test_launcher_runs_the_hook():
    payload = {"tool_name": "Bash", "tool_input": {"command": f"cp x {Q}/"}}
    assert run_hook(payload, launcher=True).returncode == 2
    assert run_hook({"tool_name": "Bash", "tool_input": {"command": "ls"}}, launcher=True).returncode == 0


def test_launcher_fails_closed_without_python():
    """Claude Code only blocks on exit code 2; without Python the launcher must not fail open."""
    bash = shutil.which("bash")
    proc = subprocess.run([bash, str(HOOK_SH)], input=json.dumps({"tool_name": "Bash", "tool_input":
                          {"command": "ls"}}), capture_output=True, text=True, timeout=30,
                          env={"PATH": "/nonexistent"})
    assert proc.returncode == 2 and "no Python 3 found" in proc.stderr


def test_settings_register_hook_for_all_writing_tools():
    settings = json.loads(SETTINGS.read_text(encoding="utf-8"))
    entries = settings["hooks"]["PreToolUse"]
    matchers = {m for e in entries for m in e["matcher"].split("|")
                if any(h["type"] == "command" and "block_game_data.sh" in h["command"] for h in e["hooks"])}
    assert {"Write", "Edit", "MultiEdit", "NotebookEdit", "Bash"} <= matchers
