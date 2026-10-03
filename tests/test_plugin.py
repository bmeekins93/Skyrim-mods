"""Independent checks of CardboardFaceMask.esp: a pure-Python reader, no Mutagen.

Binary layout (UESP "Skyrim Mod:Mod File Format", .../TES4, .../ARMO, .../ARMA, .../BOD2 Field):
  record  = type[4] dataSize:u32 flags:u32 formID:u32 vcInfo:u32 formVersion:u16 unknown:u16, data
  GRUP    = "GRUP" groupSize:u32 (incl. 24-byte header) label[4] groupType:i32 stamp:u16 vc:u16 unk:u32
  field   = type[4] size:u16 data   (an XXXX field carries the u32 size of the next one)
  record flag 0x00040000 = data is u32 decompressedSize + zlib stream.
ARMA DNAM byte meanings follow xEdit Core/wbDefinitionsTES5.pas (weight slider byte, 0x02 = Enabled).

The vanilla FormIDs the generator took from Mutagen.Bethesda.FormKeys.SkyrimSE are recorded in
src/plugin/vanilla_formkeys.json; SECOND_SOURCE below re-states them from dumps that do not come
from Mutagen, so a wrong FormKey package would be caught.

Run: .venv/bin/python -m pytest tests/test_plugin.py
"""
from __future__ import annotations

import json
import re
import shutil
import struct
import subprocess
import sys
import zlib
from dataclasses import dataclass, field
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from cfm.paths import WORK, load_config  # noqa: E402

PLUGIN_DIR = ROOT / "src" / "plugin"
YAML_DIR = PLUGIN_DIR / "yaml"
VANILLA_JSON = PLUGIN_DIR / "vanilla_formkeys.json"
MANIFEST = WORK / "nif_manifest.json"

# Contract defaults (docs/CONTRACTS.md) when the NIF stage has not written a manifest yet.
DEFAULT_MODELS = {
    "human_m": "CardboardFaceMask\\mask_human_m.nif",
    "human_f": "CardboardFaceMask\\mask_human_f.nif",
    "beast_m": "CardboardFaceMask\\mask_beast_m.nif",
    "beast_f": "CardboardFaceMask\\mask_beast_f.nif",
}

# Local FormIDs are a contract; stored with master index 01 because Skyrim.esm is master 00.
ARMO_ID, AA_HUMAN_ID, AA_BEAST_ID = 0x01000800, 0x01000801, 0x01000802
EDITOR_IDS = {ARMO_ID: "FaceMaskArmor", AA_HUMAN_ID: "FaceMaskAA", AA_BEAST_ID: "FaceMaskAA_Beast"}

# Second, non-Mutagen source for every vanilla FormID used (Skyrim.esm, load-order index 00).
#  [U] UESP wiki "Skyrim:Races" ({{ID|...}} cells), fetched 2026-10-03 via en.uesp.net/w/api.php
#  [F] github.com/F02K/Frostfall-Server @91c1694b, skyrim-esm-references/races.json + keywords.json
#  [X] github.com/super-cooper/scripts @e294d01, skyrim-modding/Output.csv (xEdit ARMO YNAM/ZNAM
#      export: "ITMClothingUpSD [SNDR:0003E879],ITMClothingDownSD [SNDR:0003E909]")
#  [M] github.com/Severause/MagelightUI @538ebec, src/MagelightSoundTable.inc (Skyrim.esm SNDR scan)
SECOND_SOURCE = {
    "ArgonianRace": 0x013740,         # U F
    "BretonRace": 0x013741,           # U F
    "DarkElfRace": 0x013742,          # U F
    "HighElfRace": 0x013743,          # U F
    "ImperialRace": 0x013744,         # U F
    "KhajiitRace": 0x013745,          # U F
    "NordRace": 0x013746,             # U F
    "OrcRace": 0x013747,              # U F
    "RedguardRace": 0x013748,         # U F
    "WoodElfRace": 0x013749,          # U F
    "DefaultRace": 0x000019,          # F (and UESP ARMO page: "0x19 DefaultRace")
    "ArgonianRaceVampire": 0x08883A,  # F
    "BretonRaceVampire": 0x08883C,    # F
    "DarkElfRaceVampire": 0x08883D,   # F
    "HighElfRaceVampire": 0x088840,   # F
    "ImperialRaceVampire": 0x088844,  # F
    "KhajiitRaceVampire": 0x088845,   # F
    "RedguardRaceVampire": 0x088846,  # F
    "NordRaceVampire": 0x088794,      # F
    "WoodElfRaceVampire": 0x088884,   # F
    "OrcRaceVampire": 0x0A82B9,       # F
    "ElderRace": 0x067CD8,            # F
    "ElderRaceVampire": 0x0A82BA,     # F
    "NordRaceAstrid": 0x07EAF3,       # F
    "DA13AfflictedRace": 0x097A3D,    # F
    "DremoraRace": 0x0131F0,          # F
    "ArmorClothing": 0x06BBE8,        # F
    "ITMClothingUpSD": 0x03E879,      # X M
    "ITMClothingDownSD": 0x03E909,    # X M
}

# Races that must never wear the mask (children) or were deliberately left out [F].
FORBIDDEN_RACES = {
    "NordRaceChild": 0x02C65B, "BretonRaceChild": 0x02C65C, "RedguardRaceChild": 0x02C65A,
    "ImperialRaceChild": 0x02C659, "BretonRaceChildVampire": 0x108272,
    "ManakinRace": 0x10760A, "InvisibleRace": 0x071E6A,
}

HUMAN_RACES = {"NordRace", "BretonRace", "ImperialRace", "RedguardRace", "HighElfRace", "DarkElfRace",
               "WoodElfRace", "OrcRace", "ElderRace", "NordRaceAstrid", "DA13AfflictedRace", "DremoraRace"}
HUMAN_RACES |= {f"{r}Vampire" for r in ["NordRace", "BretonRace", "ImperialRace", "RedguardRace",
                                       "HighElfRace", "DarkElfRace", "WoodElfRace", "OrcRace", "ElderRace"]}
BEAST_EXTRA_RACES = {"ArgonianRace", "KhajiitRaceVampire", "ArgonianRaceVampire"}

COMPRESSED = 0x00040000
ESL_FLAG, ESM_FLAG, LOCALIZED_FLAG = 0x200, 0x1, 0x80
ARMO_NON_PLAYABLE = 0x4


# --------------------------------------------------------------------------- reader

@dataclass
class Record:
    type: str
    flags: int
    form_id: int
    form_version: int
    fields: list[tuple[str, bytes]]
    raw: bytes = field(repr=False, default=b"")

    def all(self, sig: str) -> list[bytes]:
        return [data for t, data in self.fields if t == sig]

    def one(self, sig: str) -> bytes:
        found = self.all(sig)
        assert len(found) == 1, f"{self.type} {self.form_id:08X}: expected one {sig}, found {len(found)}"
        return found[0]

    def has(self, sig: str) -> bool:
        return any(t == sig for t, _ in self.fields)

    def zstring(self, sig: str) -> str:
        data = self.one(sig)
        assert data.endswith(b"\0"), f"{sig} is not NUL-terminated"
        return data[:-1].decode("cp1252")

    def u32(self, sig: str) -> int:
        data = self.one(sig)
        assert len(data) == 4, f"{sig} size {len(data)} != 4"
        return struct.unpack("<I", data)[0]

    def formids(self, sig: str) -> list[int]:
        """One FormID per occurrence of a repeated field (MODL)."""
        return [struct.unpack("<I", d)[0] for d in self.all(sig)]

    def u32_array(self, sig: str) -> list[int]:
        """A single field holding a packed uint32 array (KWDA)."""
        data = self.one(sig)
        return list(struct.unpack(f"<{len(data) // 4}I", data))


@dataclass
class Plugin:
    header: Record
    groups: dict[str, list[Record]]
    group_headers: dict[str, bytes]
    group_count: int


def parse_fields(data: bytes) -> list[tuple[str, bytes]]:
    fields, pos, big = [], 0, None
    while pos < len(data):
        sig = data[pos:pos + 4].decode("ascii")
        size = struct.unpack_from("<H", data, pos + 4)[0]
        pos += 6
        if big is not None:
            size, big = big, None
        if sig == "XXXX":
            big = struct.unpack_from("<I", data, pos)[0]
        else:
            fields.append((sig, data[pos:pos + size]))
        pos += size
    assert pos == len(data), "field data overruns the record"
    return fields


def parse_record(buf: bytes, pos: int) -> tuple[Record, int]:
    sig = buf[pos:pos + 4].decode("ascii")
    size, flags, form_id, _vc, form_version, _unk = struct.unpack_from("<IIIIHH", buf, pos + 4)
    data = buf[pos + 24:pos + 24 + size]
    if flags & COMPRESSED:
        data = zlib.decompress(data[4:])
    rec = Record(sig, flags, form_id, form_version, parse_fields(data), buf[pos:pos + 24 + size])
    return rec, pos + 24 + size


def parse_plugin(path: Path) -> Plugin:
    buf = path.read_bytes()
    header, pos = parse_record(buf, 0)
    assert header.type == "TES4", "a plugin must start with the TES4 record"
    groups: dict[str, list[Record]] = {}
    group_headers: dict[str, bytes] = {}
    group_count = 0
    while pos < len(buf):
        assert buf[pos:pos + 4] == b"GRUP", f"top level must hold only GRUPs (offset {pos})"
        size, label, group_type = struct.unpack_from("<I4si", buf, pos + 4)
        assert group_type == 0, "top-level GRUP must have group type 0"
        label = label.decode("ascii")
        assert label not in groups, f"duplicate top-level GRUP {label}"
        group_count += 1
        group_headers[label] = buf[pos:pos + 24]
        records, rpos, end = [], pos + 24, pos + size
        while rpos < end:
            assert buf[rpos:rpos + 4] != b"GRUP", "no nested GRUPs expected for ARMO/ARMA"
            rec, rpos = parse_record(buf, rpos)
            assert rec.type == label, f"{rec.type} record inside GRUP {label}"
            records.append(rec)
        assert rpos == end, f"GRUP {label} size mismatch"
        groups[label] = records
        pos = end
    return Plugin(header, groups, group_headers, group_count)


# --------------------------------------------------------------------------- fixtures

@pytest.fixture(scope="module")
def config() -> dict:
    return load_config()


@pytest.fixture(scope="module")
def manifest() -> dict:
    if MANIFEST.exists():
        data = json.loads(MANIFEST.read_text(encoding="utf-8"))
        return {"models": data["models"], "ground_model": data.get("ground_model")}
    return {"models": DEFAULT_MODELS, "ground_model": None}


@pytest.fixture(scope="module")
def plugin_path(config) -> Path:
    path = WORK / "plugin" / config["plugin_name"]
    if not path.exists():
        if shutil.which("dotnet") is None:
            pytest.skip(f"{path} not built and dotnet is unavailable (run src/plugin/build_plugin.py)")
        subprocess.run([sys.executable, str(PLUGIN_DIR / "build_plugin.py")], cwd=ROOT, check=True)
    return path


@pytest.fixture(scope="module")
def plugin(plugin_path) -> Plugin:
    return parse_plugin(plugin_path)


@pytest.fixture(scope="module")
def vanilla() -> dict:
    return json.loads(VANILLA_JSON.read_text(encoding="utf-8"))


def by_id(plugin: Plugin) -> dict[int, Record]:
    return {r.form_id: r for recs in plugin.groups.values() for r in recs}


def formkey_id(formkey: str) -> int:
    """'013746:Skyrim.esm' -> 0x00013746 (Skyrim.esm is master index 00)."""
    local, mod = formkey.split(":")
    assert mod == "Skyrim.esm", f"only Skyrim.esm may be referenced, got {formkey}"
    return int(local, 16)


# --------------------------------------------------------------------------- header

def test_tes4_flags_version_and_counts(plugin):
    h = plugin.header
    assert h.flags & ESL_FLAG, "ESL (light) flag 0x200 must be set"
    assert not h.flags & ESM_FLAG, "must stay an ESP (no ESM flag)"
    assert not h.flags & LOCALIZED_FLAG, "not localized: FULL/DESC are plain zstrings"
    version, num_records, next_id = struct.unpack("<fII", h.one("HEDR"))
    assert abs(version - 1.7) < 1e-6, f"HEDR version {version}"
    assert num_records == sum(len(r) for r in plugin.groups.values()) + plugin.group_count
    assert next_id > max(r.form_id & 0xFFFFFF for r in by_id(plugin).values())
    assert h.form_version == 44


def test_single_master_skyrim(plugin):
    h = plugin.header
    masters = [d[:-1].decode("ascii") for d in h.all("MAST")]
    assert masters == ["Skyrim.esm"]
    assert h.all("DATA") == [b"\0" * 8], "each MAST is followed by an 8-byte zero DATA"


# --------------------------------------------------------------------------- inventory

def test_exactly_one_armo_two_arma_at_contract_formids(plugin):
    assert set(plugin.groups) == {"ARMO", "ARMA"}
    assert [r.form_id for r in plugin.groups["ARMO"]] == [ARMO_ID]
    assert sorted(r.form_id for r in plugin.groups["ARMA"]) == [AA_HUMAN_ID, AA_BEAST_ID]
    for rec in by_id(plugin).values():
        assert rec.form_id >> 24 == 0x01, "own records use master index 01 (one master)"
        assert 0x800 <= rec.form_id & 0xFFFFFF <= 0xFFF, "ESL FormID range"
        assert rec.form_version == 44


def test_editor_ids(plugin):
    assert {fid: rec.zstring("EDID") for fid, rec in by_id(plugin).items()} == EDITOR_IDS


def test_no_scripts_no_compression(plugin):
    for rec in by_id(plugin).values():
        assert not rec.has("VMAD"), "this mod uses no Papyrus"
        assert not rec.flags & COMPRESSED


# --------------------------------------------------------------------------- ARMO

def slot_bits(config) -> int:
    return 1 << (int(config["slot"]) - 30)  # slot 55 -> 0x02000000 (UESP BOD2 / xEdit "55 - Unnamed")


def test_armo_fields(plugin, config, manifest):
    armo = by_id(plugin)[ARMO_ID]
    assert armo.zstring("FULL") == "Cardboard Face Mask"
    flags, armor_type = struct.unpack("<II", armo.one("BOD2"))
    assert flags == slot_bits(config)
    assert armor_type == 2, "armor type Clothing (xEdit wbArmorTypeEnum index 2)"
    assert not armo.has("BODT")
    assert armo.u32("RNAM") == SECOND_SOURCE["DefaultRace"]
    value, weight = struct.unpack("<if", armo.one("DATA"))
    assert (value, weight) == (0, 0.0)
    assert struct.unpack("<i", armo.one("DNAM"))[0] == 0, "armor rating 0"
    keywords = armo.u32_array("KWDA")
    assert armo.u32("KSIZ") == len(keywords)
    assert keywords == [SECOND_SOURCE["ArmorClothing"]]
    assert armo.u32("YNAM") == SECOND_SOURCE["ITMClothingUpSD"]
    assert armo.u32("ZNAM") == SECOND_SOURCE["ITMClothingDownSD"]
    assert bool(armo.flags & ARMO_NON_PLAYABLE) == bool(config["non_playable"])
    for absent in ("EITM", "MOD4", "TNAM", "ETYP", "VMAD", "DEST"):
        assert not armo.has(absent), f"unexpected {absent}"
    ground = manifest["ground_model"]
    if ground is None:
        assert not armo.has("MOD2"), "no world model unless the manifest provides ground_model"
    else:
        assert armo.zstring("MOD2") == ground


def test_armo_armature_order_beast_first(plugin):
    assert by_id(plugin)[ARMO_ID].formids("MODL") == [AA_BEAST_ID, AA_HUMAN_ID]


# --------------------------------------------------------------------------- ARMA

@pytest.mark.parametrize("form_id, race, extra_races, model_key", [
    (AA_HUMAN_ID, "DefaultRace", HUMAN_RACES, "human"),
    (AA_BEAST_ID, "KhajiitRace", BEAST_EXTRA_RACES, "beast"),
], ids=["FaceMaskAA", "FaceMaskAA_Beast"])
def test_arma_fields(plugin, config, manifest, form_id, race, extra_races, model_key):
    arma = by_id(plugin)[form_id]
    flags, armor_type = struct.unpack("<II", arma.one("BOD2"))
    assert (flags, armor_type) == (slot_bits(config), 2)
    assert arma.u32("RNAM") == SECOND_SOURCE[race]

    dnam = arma.one("DNAM")
    assert len(dnam) == 12
    male_prio, female_prio, male_slider, female_slider, _unk, detection, _unk2, _weapon_adjust = \
        struct.unpack("<BBBBHBBf", dnam)
    assert (male_prio, female_prio) == (0, 0)
    assert not male_slider & 0x02 and not female_slider & 0x02, "weight slider must be disabled"
    assert detection == 0

    assert arma.zstring("MOD2") == manifest["models"][f"{model_key}_m"]
    assert arma.zstring("MOD3") == manifest["models"][f"{model_key}_f"]
    for absent in ("MOD4", "MOD5", "NAM0", "NAM1", "NAM2", "NAM3", "SNDD", "ONAM"):
        assert not arma.has(absent), f"unexpected {absent}"

    races = arma.formids("MODL")
    assert races == sorted(races), "xEdit stores Additional Races sorted (wbRArrayS)"
    assert set(races) == {SECOND_SOURCE[r] for r in extra_races}
    assert not set(races) & set(FORBIDDEN_RACES.values()), "child/mannequin/invisible races"


def test_model_paths_are_game_relative(plugin):
    for rec in plugin.groups["ARMA"]:
        for sig in ("MOD2", "MOD3"):
            path = rec.zstring(sig)
            assert re.fullmatch(r"CardboardFaceMask\\[A-Za-z0-9_]+\.nif", path), path


# --------------------------------------------------------------------------- vanilla FormKeys

def test_esp_uses_exactly_the_recorded_mutagen_formkeys(plugin, vanilla):
    """The ESP references precisely the FormKeys the generator took from the FormKeys package."""
    recs = by_id(plugin)
    armo, human, beast = recs[ARMO_ID], recs[AA_HUMAN_ID], recs[AA_BEAST_ID]
    ids = lambda table: [formkey_id(v) for v in table.values()]  # noqa: E731
    assert [armo.u32("RNAM")] == ids(vanilla["armor"]["race"])
    assert armo.u32_array("KWDA") == ids(vanilla["armor"]["keywords"])
    assert [armo.u32("YNAM")] == ids(vanilla["armor"]["pickup_sound"])
    assert [armo.u32("ZNAM")] == ids(vanilla["armor"]["putdown_sound"])
    assert [human.u32("RNAM")] == ids(vanilla["human_addon"]["race"])
    assert human.formids("MODL") == ids(vanilla["human_addon"]["additional_races"])
    assert [beast.u32("RNAM")] == ids(vanilla["beast_addon"]["race"])
    assert beast.formids("MODL") == ids(vanilla["beast_addon"]["additional_races"])


def test_mutagen_formkeys_match_second_source(vanilla):
    """Every EditorID -> FormID pair from Mutagen.Bethesda.FormKeys matches the independent dumps."""
    used: dict[str, str] = {}
    for section in ("armor", "human_addon", "beast_addon"):
        for table in vanilla[section].values():
            used.update(table)
    assert set(used) <= set(SECOND_SOURCE), f"no second source for {set(used) - set(SECOND_SOURCE)}"
    mismatches = {k: v for k, v in used.items() if formkey_id(v) != SECOND_SOURCE[k]}
    assert not mismatches, f"FormKey package disagrees with second source: {mismatches}"
    assert set(vanilla["human_addon"]["additional_races"]) == HUMAN_RACES
    assert set(vanilla["beast_addon"]["additional_races"]) == BEAST_EXTRA_RACES


# --------------------------------------------------------------------------- Spriggit source

def test_spriggit_yaml_is_portable_and_pinned():
    tools = json.loads((ROOT / ".config" / "dotnet-tools.json").read_text(encoding="utf-8"))["tools"]
    meta = json.loads((YAML_DIR / "spriggit-meta.json").read_text(encoding="utf-8"))
    assert meta["PackageName"] == "Spriggit.Yaml.Skyrim"
    assert meta["Version"] == tools["spriggit.cli"]["version"]
    assert meta["Release"] == "SkyrimSE" and meta["ModKey"] == "CardboardFaceMask.esp"
    local = re.compile(r"(/home/|/root/|/tmp/|/Users/|/mnt/|[A-Za-z]:[\\/])")
    for path in YAML_DIR.rglob("*"):
        if path.is_file():
            hit = local.search(path.read_text(encoding="utf-8"))
            assert not hit, f"{path.relative_to(ROOT)} contains machine-specific {hit.group(0)!r}"


def test_records_are_in_canonical_formid_order(plugin):
    """The shipped plugin is canonicalized: inside every GRUP, records ascend by FormID, so its
    bytes do not depend on the directory-listing order Spriggit read the YAML files in."""
    for label, records in plugin.groups.items():
        ids = [r.form_id for r in records]
        assert ids == sorted(ids), f"GRUP {label} order {[f'{i:08X}' for i in ids]}"


def test_generated_and_rebuilt_are_byte_identical(plugin_path, config):
    """Gate (a) re-checked independently: the Mutagen generator output and the canonicalized
    Spriggit rebuild are the same file; the raw Spriggit output holds the same records."""
    generated = WORK / "plugin" / "generated" / config["plugin_name"]
    if not generated.exists():
        pytest.skip("run src/plugin/build_plugin.py --regenerate to produce the generator output")
    assert generated.read_bytes() == plugin_path.read_bytes()
    raw = WORK / "plugin" / "spriggit" / config["plugin_name"]
    if raw.exists():  # Spriggit's own output differs at most in record order
        a, b = parse_plugin(generated), parse_plugin(raw)
        assert a.header.raw == b.header.raw and a.group_headers == b.group_headers
        for label in a.groups:
            assert sorted(r.raw for r in a.groups[label]) == sorted(r.raw for r in b.groups[label])


# --------------------------------------------------------------------------- generator inputs

GENERATOR_DLL = WORK / "plugin" / "dotnet" / "bin" / "Generator" / "release" / "CardboardFaceMask.Generator.dll"
GOOD_MANIFEST = {"meshes_subdir": "CardboardFaceMask", "models": dict(DEFAULT_MODELS), "ground_model": None}


def _manifest(**changes) -> dict:
    data = json.loads(json.dumps(GOOD_MANIFEST))
    for key, value in changes.items():
        if key.startswith("models_"):
            name = key[len("models_"):]
            data["models"].pop(name) if value is None else data["models"].__setitem__(name, value)
        else:
            data[key] = value
    return data


@pytest.mark.parametrize("manifest_text, config_edit, expected", [
    (json.dumps(_manifest()), None, None),
    (json.dumps(_manifest(models_beast_f=None)), None, "models.beast_f"),
    (json.dumps(_manifest(models_human_m="CardboardFaceMask\\..\\..\\x.nif")), None, "models.human_m must be"),
    (json.dumps(_manifest(models_beast_f="x.nif")), None, "models.beast_f must be"),
    (json.dumps(_manifest(models_human_f="CardboardFaceMask/mask_human_f.nif")), None, "models.human_f must be"),
    (json.dumps(_manifest(ground_model=7)), None, "ground_model must be a string or null"),
    (json.dumps(_manifest(meshes_subdir="..")), None, "meshes_subdir must be"),
    ('{"models": {', None, "not valid JSON"),
    (json.dumps(_manifest()), ("slot: 55", "slot: abc"), "slot must be a biped slot"),
], ids=["ok", "missing-key", "dotdot", "outside-subdir", "forward-slash", "ground-type", "bad-subdir",
        "json-syntax", "config-slot"])
def test_generator_rejects_bad_inputs_cleanly(tmp_path, manifest_text, config_edit, expected):
    """The generator turns schema and path errors in its inputs into a one-line ERROR (exit 1),
    never a stack trace, and keeps model paths inside meshes\\<meshes_subdir>\\."""
    if shutil.which("dotnet") is None or not GENERATOR_DLL.exists():
        pytest.skip("generator not built (run src/plugin/build_plugin.py)")
    config_text = (ROOT / "config.yaml").read_text(encoding="utf-8")
    if config_edit:
        assert config_edit[0] in config_text
        config_text = config_text.replace(config_edit[0], config_edit[1], 1)
    (tmp_path / "config.yaml").write_text(config_text, encoding="utf-8")
    (tmp_path / "manifest.json").write_text(manifest_text, encoding="utf-8")
    out = tmp_path / "out" / load_config()["plugin_name"]
    proc = subprocess.run(["dotnet", str(GENERATOR_DLL), "generate", "--config", str(tmp_path / "config.yaml"),
                           "--manifest", str(tmp_path / "manifest.json"), "--out", str(out)],
                          capture_output=True, text=True)
    if expected is None:
        assert proc.returncode == 0, proc.stderr
        assert "Contract checks: OK" in proc.stdout
        return
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "Unhandled exception" not in proc.stderr
    assert proc.stderr.startswith("ERROR: ") and expected in proc.stderr, proc.stderr
    assert not out.exists(), "no plugin may be written from bad inputs"
