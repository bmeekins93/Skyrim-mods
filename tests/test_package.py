"""Tests for src/07_package.py (SPID ini generator incl. the KEF keyword line, build-input gate,
package validator, zip), src/build_all.py (outline and config checks) and the
.claude/hooks/block_game_data.{sh,py} safety hook.

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

KEYWORD = "KEFEquip_0x800_CardboardFaceMask_esp"
ITEM_LINE = "Item = 0x800~CardboardFaceMask.esp|ActorTypeNPC|NONE|NONE|-C|1|100"
KEYWORD_LINE = f"Keyword = {KEYWORD}|ActorTypeNPC|NONE|NONE|-C|NONE|100"


def test_contract_lines_match_claude_md():
    """CLAUDE.md fixes both lines; the generator must reproduce them for the default config
    (equip_method defaults to kef when the key is missing)."""
    claude_md = (ROOT / "CLAUDE.md").read_text(encoding="utf-8")
    for cfg in (BASE_CFG, dict(BASE_CFG, equip_method="kef")):
        assert pkg.distribution_lines(cfg) == [ITEM_LINE, KEYWORD_LINE]
    assert ITEM_LINE in claude_md and KEYWORD_LINE in claude_md


def test_engine_mode_writes_item_line_only():
    cfg = dict(BASE_CFG, equip_method="engine")
    assert pkg.distribution_lines(cfg) == [ITEM_LINE]
    text = pkg.make_distr_ini(cfg)
    assert pkg.parse_distr_ini(text) == [("Item", ITEM_LINE.split(" = ", 1)[1])]
    assert "SPID never equips items" in text and "rebuild with equip_method: kef" in text
    assert "KEFEquip_0x800" not in text      # no Keyword line, not even in the examples


def test_line_follows_config():
    cfg = dict(BASE_CFG, exclude_children=False, chance_percent=12.5, plugin_name="Other.esp")
    assert pkg.distribution_lines(dict(cfg, equip_method="engine")) == [
        "Item = 0x800~Other.esp|ActorTypeNPC|NONE|NONE|NONE|1|12.5!"]
    assert pkg.distribution_lines(cfg) == [
        "Item = 0x800~Other.esp|KEFEquip_0x800_Other_esp|NONE|NONE|NONE|1|100",
        "Keyword = KEFEquip_0x800_Other_esp|ActorTypeNPC|NONE|NONE|NONE|NONE|12.5!"]


@pytest.mark.parametrize("value", ["KEF", "Kef", "spid", "", None, 1, True])
def test_equip_method_rejects(value):
    with pytest.raises(pkg.PackageError, match="equip_method must be one of kef, engine"):
        pkg.make_distr_ini(dict(BASE_CFG, equip_method=value))


@pytest.mark.parametrize("plugin,keyword", [("CardboardFaceMask.esp", KEYWORD),
                                            ("Mask2.esm", "KEFEquip_0x800_Mask2_esm"),
                                            ("Mask.esl", "KEFEquip_0x800_Mask_esl"),
                                            ("Kad_AkaviriHats.esp", "KEFEquip_0x800_Kad_AkaviriHats_esp"),
                                            ("Cardboard_Face_Mask.esp",
                                             "KEFEquip_0x800_Cardboard_Face_Mask_esp")])
def test_kef_keyword_name(plugin, keyword):
    """KEFEquip_0x[FORMID]_[MODNAME]_[EXTENSION]; KEF's published examples are 0x800 in an .esp and
    0x803 in Kad_AkaviriHats.esp (an '_' inside the plugin name)."""
    assert pkg.kef_keyword(plugin) == keyword
    assert pkg.spid_record_kind(keyword) == "editorid"
    assert pkg.spid_record_kind(keyword + pkg.KEF_FORCE_SUFFIX) == "editorid"


@pytest.mark.parametrize("plugin", ["Cardboard Face Mask.esp", "Cardboard.Face.esp", "_Mask.esp",
                                    "Mask_.esp", "Mask__X.esp", "Mask_esp.esp", "Mask_ESM.esp",
                                    "Mask_forceEquip.esp", "Mask.ESP", "Mask.txt", "Mask"])
def test_kef_keyword_rejects_unverified_plugin_names(plugin):
    with pytest.raises(pkg.PackageError):
        pkg.kef_keyword(plugin)
    with pytest.raises(pkg.PackageError):
        pkg.make_distr_ini(dict(BASE_CFG, plugin_name=plugin))
    if plugin.endswith(".esp"):   # engine mode writes no keyword, so these names work there
        assert len(pkg.distribution_lines(dict(BASE_CFG, plugin_name=plugin, equip_method="engine"))) == 1


def test_kef_keyword_reads_as_editor_id_in_spid():
    """SPID must take the keyword for an EditorID (and create it), never for a FormID or plugin,
    and its sanitizer must leave it alone (it strips '0x00' leading zeros without a word boundary)."""
    assert pkg.spid_record_kind(KEYWORD) == "editorid"
    assert pkg.spid_record_kind("0x800~CardboardFaceMask.esp") == "formid~plugin"
    assert pkg.spid_record_kind("0x800") == "formid"
    assert pkg.spid_record_kind("KEFEquip_0x800_Mask.esp") == "plugin"   # why KEF writes _esp
    assert pkg.spid_sanitize("KEFEquip_0x00800_Mask_esp") == "KEFEquip_0x800_Mask_esp"


@pytest.mark.parametrize("raw,sanitized", [
    ("0x800~X.esp | A , B", "0x800~X.esp|A,B"),
    ("0x00800~X.esp", "0x800~X.esp"),
    ("00012345", "0x12345"),
    ("0x800 - X.esp|A", "0x800~X.esp|A"),
    (ITEM_LINE.split(" = ", 1)[1], ITEM_LINE.split(" = ", 1)[1]),
    (KEYWORD_LINE.split(" = ", 1)[1], KEYWORD_LINE.split(" = ", 1)[1]),
])
def test_spid_sanitize_mirrors_spid(raw, sanitized):
    """LookupConfigs.cpp:13-53; a changed value makes SPID rewrite the ini file."""
    assert pkg.spid_sanitize(raw) == sanitized


@pytest.mark.parametrize("value,text", [(100, "100"), (100.0, "100"), (50, "50"), (0.01, "0.01"),
                                        (33.3, "33.3"), ("25", "25"), (0.000123, "0.000123")])
def test_format_chance(value, text):
    assert pkg.format_chance(value) == text


@pytest.mark.parametrize("value", [0, -5, 100.5, "abc", None, float("nan"), 1e-9])
def test_format_chance_rejects(value):
    with pytest.raises(pkg.PackageError):
        pkg.format_chance(value)


@pytest.mark.parametrize("method", ["kef", "engine"])
@pytest.mark.parametrize("value", [0, -5, 100.5, "abc", None, float("nan"), 1e-9])
def test_bad_chance_rejected_in_both_modes(value, method, monkeypatch):
    """The generator, and build_all's early config check that runs it, refuse a bad chance in
    either equip mode."""
    cfg = dict(BASE_CFG, chance_percent=value, equip_method=method)
    with pytest.raises(pkg.PackageError, match="chance_percent"):
        pkg.make_distr_ini(cfg)
    monkeypatch.setattr(build_all, "package_module", lambda: _StubConfigPackage(cfg))
    with pytest.raises(build_all.BuildError, match="config.yaml: chance_percent"):
        build_all.check_package_config([s for s in build_all.STAGES if s.name == "package"])


@pytest.mark.parametrize("value,field", [(100, "100"), (25, "25!"), (99.9, "99.9!"), (0.5, "0.5!")])
def test_chance_below_100_is_deterministic(value, field):
    """SPID re-rolls a plain chance on every launch; '!' fixes it per NPC and character."""
    assert pkg.chance_field(value) == field
    cfg = dict(BASE_CFG, chance_percent=value)
    assert pkg.distribution_lines(dict(cfg, equip_method="engine"))[0].endswith("|" + field)
    item, keyword = pkg.distribution_lines(cfg)
    assert keyword.endswith("|" + field)
    if field == "100":
        assert item == ITEM_LINE
    else:   # the Item line follows the keyword instead of rolling a second, independent chance
        assert item == f"Item = 0x800~CardboardFaceMask.esp|{KEYWORD}|NONE|NONE|-C|1|100"


@pytest.mark.parametrize("cfg", [BASE_CFG, dict(BASE_CFG, chance_percent=25),
                                 dict(BASE_CFG, exclude_children=False)])
def test_generated_lines_pair_up(cfg):
    """The validator's pairing rule accepts what the generator writes."""
    item, keyword = (line.split(" = ", 1)[1].split("|") for line in pkg.distribution_lines(cfg))
    assert pkg.pairing_problems(item, keyword, KEYWORD) == []


def test_pairing_rejects_independent_rolls():
    item = ITEM_LINE.split(" = ", 1)[1].split("|")
    keyword = KEYWORD_LINE.split(" = ", 1)[1].split("|")
    assert pkg.pairing_problems(item[:6] + ["25!"], keyword[:6] + ["25!"], KEYWORD)
    assert pkg.pairing_problems(item, keyword[:4] + ["NONE"] + keyword[5:], KEYWORD)   # traits
    assert pkg.pairing_problems(item[:1] + ["ActorTypeNPC,-Nazeem"] + item[2:], keyword, KEYWORD)


@pytest.mark.parametrize("method", ["kef", "engine"])
def test_ini_structure_as_spid_reads_it(method):
    """Item (+ Keyword) entries with 7 '|' sections, no [sections], ASCII, comments on their own
    lines, nothing SPID's sanitizer would rewrite."""
    cfg = dict(BASE_CFG, equip_method=method)
    text = pkg.make_distr_ini(cfg)
    text.encode("ascii")
    entries = pkg.parse_distr_ini(text)
    assert [f"{k} = {v}" for k, v in entries] == pkg.distribution_lines(cfg)
    assert [k for k, _ in entries] == (["Item", "Keyword"] if method == "kef" else ["Item"])
    for _, value in entries:
        assert len(value.split("|")) == pkg.SPID_SECTIONS
        assert pkg.spid_sanitize(value) == value
    assert not any(line.lstrip().startswith("[") for line in text.splitlines())
    # SimpleIni has no inline comments: an active line must not carry a ';'
    for line in text.splitlines():
        if line and line[0] not in ";#":
            assert ";" not in line and "#" not in line


def test_ini_header_explains_kef():
    """kef mode: the header says what each line does and that KEF is needed for the keyword."""
    text = pkg.make_distr_ini(BASE_CFG)
    header = "\n".join(line for line in text.splitlines() if line.startswith(";"))
    assert pkg.KEF_URL in header and "Keyword Equip Framework" in header
    assert "never equips" in header and "does nothing by itself" in header
    assert "Without KEF the keyword is harmless" in header
    # what KEF's page says about adding the item and occupied slots, and the force suffix
    assert "TWO" in header and "slot (55)" in header
    assert KEYWORD + pkg.KEF_FORCE_SUFFIX in header and "not tested here" in header


@pytest.mark.parametrize("method", ["kef", "engine"])
def test_ini_examples_are_valid_entries(method):
    """Each commented example, uncommented, parses as a 7-section line for our ARMO / keyword;
    in kef mode the examples come in Item + Keyword pairs that select the same NPCs."""
    text = pkg.make_distr_ini(dict(BASE_CFG, equip_method=method))
    examples = [line.lstrip("; ").split(" = ", 1) for line in text.splitlines()
                if line.startswith(";") and re.match(r";\s+(Item|Keyword) = (0x|KEF)", line)]
    items = [v for k, v in examples if k == "Item"]
    keywords = [v for k, v in examples if k == "Keyword"]
    assert len(items) == 3 and len(keywords) == (3 if method == "kef" else 0)
    for value in items + keywords:
        sections = value.split("|")
        assert len(sections) == 7
        assert pkg.spid_sanitize(value) == value
    assert all(v.startswith("0x800~CardboardFaceMask.esp|") for v in items)
    assert all(v.startswith(KEYWORD + "|") for v in keywords)
    for item, keyword in zip(items, keywords):
        assert pkg.pairing_problems(item.split("|"), keyword.split("|"), KEYWORD) == []
    assert (keywords or items)[-1].endswith("|25!")


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
    assert {"equip_method", "string_clearance_units"} <= set(documented)


def test_readme_names_kef_requirement():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert pkg.KEF_URL in readme and "Keyword Equip Framework" in readme
    assert pkg.distribution_lines(load_config())[-1].split(" = ", 1)[1] in readme


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


class _StubConfigPackage:
    """src/07_package.py with a substitute config.yaml."""
    PackageError = pkg.PackageError
    make_distr_ini = staticmethod(pkg.make_distr_ini)

    def __init__(self, cfg):
        self.cfg = cfg

    def load_config(self):
        return self.cfg


@pytest.mark.parametrize("names,cfg,fails", [
    (["mesh", "nif", "plugin", "package", "test"], dict(BASE_CFG, equip_method="KEF"), True),
    (["package"], dict(BASE_CFG, plugin_name="Card Board.esp"), True),
    (["package"], dict(BASE_CFG, equip_method="engine"), False),
    (["mesh", "nif"], dict(BASE_CFG, equip_method="KEF"), False),     # package not selected
])
def test_build_all_checks_package_config_first(monkeypatch, names, cfg, fails):
    monkeypatch.setattr(build_all, "package_module", lambda: _StubConfigPackage(cfg))
    stages = [s for s in build_all.STAGES if s.name in names]
    if fails:
        with pytest.raises(build_all.BuildError, match="config.yaml: "):
            build_all.check_package_config(stages)
    else:
        build_all.check_package_config(stages)


def test_build_all_real_outline_check_runs():
    """The real check loads src/07_package.py and returns a list (contents depend on work/)."""
    module = build_all.package_module()
    assert isinstance(module.outline_problems(module.load_config()), list)
    build_all.check_package_config(build_all.STAGES)    # the real config.yaml is accepted


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
    assert report["distr_lines"] == pkg.distribution_lines(config)
    assert report["equip_method"] == config.get("equip_method", "kef")


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


@pytest.mark.parametrize("name,index", [("Diffuse", 0), ("Glow", 2), ("Specular", 7), ("Wrinkles", 8)])
def test_detects_texture_in_any_slot(broken_copy, config, name, index):
    """BSShaderTextureSet has nine slots (0-8). pyn's shape.textures only shows the slots its shader
    flags enable and never reads index 8; the validator reads every index directly, first to last."""
    from cfm.nifenv import NifFile
    path = sorted((broken_copy / "meshes" / MOD_NAME).glob("*.nif"))[0]
    nif = NifFile(str(path))
    shape = nif.shapes[0]
    shape.set_texture(name, "textures\\CardboardFaceMask\\does_not_exist.dds")   # pynifly.py:3585-3615
    nif.save()
    refs = pkg.nif_texture_refs(path)
    assert (shape.name, f"slot {index}", "textures\\CardboardFaceMask\\does_not_exist.dds") in refs
    if name in ("Glow", "Wrinkles"):
        assert "does_not_exist" not in str(dict(NifFile(str(path)).shapes[0].textures))   # pyn misses it
    _expect_failure(broken_copy, config, f"slot {index}: textures/CardboardFaceMask/does_not_exist.dds "
                                         "is not in the package")


def test_texture_refs_cover_every_slot_index(dist_dir):
    """The shipped meshes: every shape is read at indices 0..8 and uses only slots 0 and 1 (and
    slot 7, specular, is empty: the specular mask lives in the normal map's alpha)."""
    for nif in sorted((dist_dir / "meshes" / MOD_NAME).glob("*.nif")):
        slots = {slot for _, slot, _ in pkg.nif_texture_refs(nif)}
        assert slots and slots <= {"slot 0", "slot 1"}, (nif.name, slots)


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


def _rewrite_ini(pkg_dir, config, old, new):
    ini = pkg_dir / pkg.distr_ini_name(config["plugin_name"])
    text = ini.read_text()
    assert old in text
    ini.write_text(text.replace(old, new))


def test_detects_keyword_for_other_item(broken_copy, config):
    if pkg.equip_method(config) != "kef":
        pytest.skip("equip_method is not kef")
    _rewrite_ini(broken_copy, config, "\nKeyword = KEFEquip_0x800_", "\nKeyword = KEFEquip_0x801_")
    _expect_failure(broken_copy, config, "keyword 'KEFEquip_0x801_CardboardFaceMask_esp' is not")


def test_detects_missing_keyword_line(broken_copy, config):
    if pkg.equip_method(config) != "kef":
        pytest.skip("equip_method is not kef")
    _rewrite_ini(broken_copy, config, "\nKeyword = KEFEquip_", "\n; Keyword = KEFEquip_")
    _expect_failure(broken_copy, config, "0 active 'Keyword = KEFEquip_0x800_CardboardFaceMask_esp|...' lines")


def test_detects_unpaired_chance(broken_copy, config):
    """Item at 100 % but Keyword at 25 %: masks KEF never equips on three NPCs in four."""
    if pkg.equip_method(config) != "kef" or pkg.chance_field(config.get("chance_percent", 100)) != "100":
        pytest.skip("needs equip_method kef at 100 %")
    _rewrite_ini(broken_copy, config, "|-C|NONE|100\n", "|-C|NONE|25!\n")
    _expect_failure(broken_copy, config, "the Item line must then use KEFEquip_0x800_CardboardFaceMask_esp")


def test_detects_keyword_line_in_engine_mode(broken_copy, config):
    if pkg.equip_method(config) != "kef":
        pytest.skip("equip_method is not kef")
    _expect_failure(broken_copy, dict(config, equip_method="engine"), "unexpected entries [('Keyword'")


def test_detects_line_spid_would_rewrite(broken_copy, config):
    _rewrite_ini(broken_copy, config, "\nItem = 0x800~", "\nItem = 0x00800~")
    _expect_failure(broken_copy, config, "SPID would rewrite Item")


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
