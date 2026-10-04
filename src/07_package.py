"""Stage 6 - package: work/ -> dist/CardboardFaceMask/ + dist/CardboardFaceMask.zip

Inputs (docs/CONTRACTS.md):
  work/plugin/<plugin_name>   ESL-flagged ESP (src/plugin/build_plugin.py)
  work/nif/*.nif              meshes (src/04_export_nif.py)
  work/dds/*.dds              textures (src/05_dds.py)
  work/outline.json, work/atlas_layout.json, work/mesh/mesh_report.json, work/nif_manifest.json
                              build records, checked for consistency with config.yaml
  config.yaml                 plugin_name, exclude_children, chance_percent, equip_method,
                              silhouette_mode, ...
Outputs:
  dist/CardboardFaceMask/     Data-folder layout: <plugin>, <plugin stem>_DISTR.ini,
                              meshes/CardboardFaceMask/*.nif, textures/CardboardFaceMask/*.dds
  dist/CardboardFaceMask.zip  the same files, zip root = Data folder contents (MO2 / Vortex / manual)
  work/package_report.json    file list with sizes + SHA-256 and the validation results

The DISTR ini (SPID) holds an Item line that puts the mask into NPC inventories and, with
equip_method kef (default), a Keyword line whose KEFEquip_... keyword makes KEF (Keyword Equip
Framework, an SKSE plugin) equip it; SPID itself never equips items. The line formats and every
rule the validator applies to the ini come from SPID's source; see docs/SPID_NOTES.md for
file:line citations (KEF: section 13). Packaging fails loudly (exit code 1) on:
build products that do not match config.yaml or each other (silhouette mode, photo, fixtures,
a stage output older than its input), missing inputs, stray or mis-named files, a texture path in
any of a NIF's nine texture slots or a model path in the ESP that does not resolve (exact case)
inside the package, unreferenced meshes/textures, DISTR lines that do not point at the shipped
plugin's ARMO (or do not select the same NPCs), or a zip whose contents differ from the folder.
The package is assembled in a staging folder and only replaces dist/ once it is valid.

Run from the repo root:
  .venv/bin/python src/07_package.py               # assemble + validate + zip
  .venv/bin/python src/07_package.py --check-only  # validate an existing dist/ (no rebuild)
  .venv/bin/python src/07_package.py --print-ini   # print the generated DISTR ini
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import shutil
import struct
import sys
import zipfile
from pathlib import Path, PurePosixPath, PureWindowsPath

sys.path.insert(0, str(Path(__file__).resolve().parent))
from cfm.esp_read import LOCALIZED_FLAG, PluginFormatError, read_plugin  # noqa: E402
from cfm.paths import DIST, MOD_DIST, MOD_NAME, ROOT, WORK, load_config  # noqa: E402

# Contract values (docs/CONTRACTS.md, CLAUDE.md).
ARMO_LOCAL_ID = 0x800
ARMO_EDITOR_ID = "FaceMaskArmor"
STRING_FILTER = "ActorTypeNPC"
ITEM_COUNT = 1
REQUIRED_MASTERS = ["Skyrim.esm"]
ESL_ID_RANGE = (0x800, 0xFFF)          # object indices an ESL-flagged plugin may define
SPID_SECTIONS = 7                        # SPID Parser.h: at most 7 '|' sections per entry

# How NPCs come to wear the mask (config.yaml equip_method; docs/SPID_NOTES.md sections 9, 13).
EQUIP_METHODS = ("kef", "engine")        # kef = Item + KEFEquip Keyword line; engine = Item line only
KEF_PREFIX = "KEFEquip"                  # KEF keyword: KEFEquip_0x<FORMID>_<MODNAME>_<EXTENSION>
KEF_URL = "https://www.nexusmods.com/skyrimspecialedition/mods/185273"
# Plugin names allowed in a KEF keyword: letters/digits, optionally joined by single '_'. KEF's own
# example KEFEquip_0x803_Kad_AkaviriHats_esp_forceEquip_true (Kad_AkaviriHats.esp) shows inner
# underscores; spaces, dots and doubled/edge '_' are not shown anywhere, so they are refused on
# purpose, and so are '_' parts that look like KEF's own tokens (extension, forceEquip suffix).
KEF_MODNAME_RE = re.compile(r"^[A-Za-z0-9]+(?:_[A-Za-z0-9]+)*$")
KEF_FORCE_SUFFIX = "_forceEquip_true"    # per KEF's page: equip even if the slot is taken (not used)
PLUGIN_EXTENSIONS = ("esp", "esm", "esl")

ZIP_PATH = DIST / f"{MOD_NAME}.zip"
STAGING = DIST / f".{MOD_NAME}.staging"   # assembled + validated here, then moved into place
REPORT_PATH = WORK / "package_report.json"
ZIP_TIMESTAMP = (2026, 1, 1, 0, 0, 0)   # fixed, so the zip is byte-for-byte reproducible
ASSET_NAME_RE = re.compile(r"^[a-z0-9_]+\.(nif|dds)$")

DDS_FOURCC_OK = {b"DXT1", b"DXT5", b"DX10"}
DXGI_BC7_UNORM = 98
TEXTURE_SLOTS = 9                        # BSShaderTextureSet slots in Skyrim SE (nifly index 0-8)

# Stage outputs in build order: each must be at least as new as the ones it was built from.
BUILD_RECORDS = {
    "outline": ("outline.json", "segment"),
    "layout": ("atlas_layout.json", "textures"),
    "mesh": ("mesh/mesh_report.json", "mesh"),
    "manifest": ("nif_manifest.json", "nif"),
}
BUILT_FROM = {"layout": ["outline"], "mesh": ["outline", "layout"], "manifest": ["mesh"]}
EXIF_ORIENTATION = 0x0112


class PackageError(RuntimeError):
    """The package could not be built or failed validation."""


# --------------------------------------------------------------------------- DISTR ini

def distr_ini_name(plugin_name: str) -> str:
    """SPID only reads Data\\*.ini files whose name contains '_DISTR' (case-sensitive)."""
    return f"{Path(plugin_name).stem}_DISTR.ini"


def format_chance(value) -> str:
    """Chance in percent, 0 < chance <= 100, written without a trailing '.0'."""
    try:
        chance = float(value)
    except (TypeError, ValueError):
        raise PackageError(f"chance_percent must be a number, got {value!r}") from None
    if not (0 < chance <= 100) or math.isnan(chance):
        raise PackageError(f"chance_percent must be in (0, 100], got {value!r}")
    text = str(int(chance)) if chance.is_integer() else f"{chance:.6f}".rstrip("0").rstrip(".")
    if float(text) == 0:
        raise PackageError(f"chance_percent {value!r} is below the 0.000001 % resolution")
    return text


def chance_field(value) -> str:
    """SPID chance section. Below 100 % a trailing '!' makes the roll deterministic per NPC and
    save (SPID LookupConfigs.h:579-581, LookupFilters.cpp:199-216); without it SPID re-rolls on
    every game launch, so masks would move between NPCs from one session to the next."""
    text = format_chance(value)
    return text if float(text) >= 100 else text + "!"


def equip_method(cfg: dict) -> str:
    """config.yaml equip_method: 'kef' (default) or 'engine'."""
    method = cfg.get("equip_method", "kef")
    if method not in EQUIP_METHODS:
        raise PackageError(f"equip_method must be one of {', '.join(EQUIP_METHODS)}, got {method!r}")
    return method


def item_form(plugin_name: str) -> str:
    """SPID FormID~plugin of the mask ARMO: the local ID, correct for ESL-flagged plugins."""
    return f"0x{ARMO_LOCAL_ID:X}~{plugin_name}"


def kef_keyword(plugin_name: str) -> str:
    """EditorID of the keyword that tells KEF to equip the mask: KEFEquip_0x<FORMID>_<MODNAME>_<EXT>,
    the format and the 0x800-in-an-.esp example on KEF's Nexus page (docs/SPID_NOTES.md section 13;
    its second example, Kad_AkaviriHats.esp, has an '_' in the plugin name). No plugin defines it: SPID creates a keyword with this EditorID (SPID FormData.h:170-197). It
    contains no '~' and no '.es' and is not pure hex, so SPID reads it as an EditorID rather than a
    FormID or plugin name (CLibUtil distribution.hpp:86-98)."""
    stem, dot, ext = plugin_name.rpartition(".")
    if not dot or ext not in PLUGIN_EXTENSIONS:
        raise PackageError(f"plugin_name {plugin_name!r} must end in .esp, .esm or .esl")
    reserved = {*PLUGIN_EXTENSIONS, "forceequip"}
    if not KEF_MODNAME_RE.match(stem) or reserved & {part.lower() for part in stem.split("_")}:
        raise PackageError(f"equip_method kef: plugin_name {plugin_name!r} must be letters and digits, "
                           f"optionally joined by single '_', before the extension, with no '_' part "
                           f"named {', '.join(sorted(reserved))}: KEF reads the plugin name back out of "
                           f"the keyword {KEF_PREFIX}_0x{ARMO_LOCAL_ID:X}_<name>_{ext}, and only names "
                           "of that shape appear in KEF's examples (restricted on purpose)")
    return f"{KEF_PREFIX}_0x{ARMO_LOCAL_ID:X}_{stem}_{ext}"


def spid_entry(key: str, form: str, strings: str, traits: str, count: str, chance: str) -> str:
    """key = FormOrEditorID|StringFilters|FormFilters|LevelFilters|Traits|Count|Chance"""
    return f"{key} = " + "|".join([form, strings, "NONE", "NONE", traits, count, chance])


def distribution_lines(cfg: dict) -> list[str]:
    """The active DISTR entries, in file order.

    engine: the Item line only (SPID adds the mask to the inventory and never equips it).
    kef:    the Item line and a Keyword line with the same filters and chance; the keyword only has
            an effect when KEF is installed. Below 100 % the two lines would roll independently
            (the deterministic seed hashes the whole entry text, SPID LookupConfigs.cpp:73-74,
            LookupFilters.cpp:201-208), so then only the Keyword line carries the chance and the Item
            line selects the NPCs that got the keyword: SPID distributes keywords before items to
            the same NPC (Distribute.cpp:15-19 vs 94-105) and string filters see keywords it added
            (Distribute.h:192, LookupNPC.cpp:86-91, 117-120)."""
    plugin = cfg["plugin_name"]
    traits = "-C" if cfg.get("exclude_children", True) else "NONE"
    chance = chance_field(cfg.get("chance_percent", 100))
    method = equip_method(cfg)
    item = spid_entry("Item", item_form(plugin), STRING_FILTER, traits, str(ITEM_COUNT), chance)
    if method == "engine":
        return [item]
    keyword = kef_keyword(plugin)
    if chance != "100":
        item = spid_entry("Item", item_form(plugin), keyword, traits, str(ITEM_COUNT), "100")
    return [item, spid_entry("Keyword", keyword, STRING_FILTER, traits, "NONE", chance)]


def make_distr_ini(cfg: dict) -> str:
    """The complete CardboardFaceMask_DISTR.ini text (ASCII, comments on their own lines)."""
    plugin = cfg["plugin_name"]
    form = item_form(plugin)
    keyword = kef_keyword(plugin) if equip_method(cfg) == "kef" else None
    beast = "-0x13740~Skyrim.esm,-0x13745~Skyrim.esm,-0x8883A~Skyrim.esm,-0x88845~Skyrim.esm"
    lines = [
        f"; {distr_ini_name(plugin)} - Spell Perk Item Distributor (SPID) configuration",
        "; Generated by src/07_package.py from config.yaml (details: docs/SPID_NOTES.md).",
        ";",
        "; SPID reads every *_DISTR.ini directly inside Data (not in sub-folders) when the game starts.",
        "; Lines starting with ';' or '#' are comments; keep comments on their own line and do not",
        "; add [section] headers (SPID only reads entries outside sections).",
        ";",
    ]
    if keyword:
        lines += [
            "; Two active lines (config.yaml equip_method: kef):",
            ";   Item     puts one mask into the inventory of every matching NPC. SPID never equips",
            ";            items, so on its own this line only makes NPCs CARRY the mask.",
            f";   Keyword  gives the same NPCs the keyword {keyword}, which SPID",
            ";            creates (no plugin defines it). The keyword does nothing by itself: KEF -",
            ";            Keyword Equip Framework, an SKSE plugin you install separately,",
            f";            {KEF_URL}",
            f";            reads it and makes the NPC WEAR item 0x{ARMO_LOCAL_ID:X} of {plugin}.",
            ";            Without KEF the keyword is harmless and NPCs are expected to only carry",
            ";            the mask.",
            ";   KEF's page (not tested here) says that when the NPC already wears another item in",
            ";   that slot (55) the item is 'NOT added to the NPC's inventory and is not equipped',",
            ";   which implies that KEF normally adds the item itself. So with KEF an NPC will",
            ";   probably carry TWO masks, one worn; the Item line stays as the fallback in case KEF",
            ";   only equips. To force the mask on over another slot-55 item (KEF then unequips",
            f";   that item), KEF's page says to append {KEF_FORCE_SUFFIX} to the keyword name:",
            f";   {keyword}{KEF_FORCE_SUFFIX} (below 100 % in both lines).",
            ";   Both lines select the same NPCs; change both when you edit one. Below 100 % only the",
            ";   Keyword line has the chance and the Item line uses the keyword as its filter (two",
            ";   separate chance rolls would mostly pick different NPCs).",
            ";",
        ]
    else:
        lines += [
            "; One active line (config.yaml equip_method: engine): it puts one mask into the inventory",
            "; of every matching NPC. SPID never equips items, so NPCs may just CARRY the mask; whether",
            "; the game puts it on by itself is not verified. To make NPCs wear it, install KEF -",
            f"; Keyword Equip Framework ({KEF_URL})",
            "; and rebuild with equip_method: kef.",
            ";",
        ]
    lines += [
        "; Item = FormOrEditorID|StringFilters|FormFilters|LevelFilters|Traits|Count|Chance",
        f";   FormOrEditorID  {form}  local FormID of the mask (no load-order prefix) ~ plugin.",
        ";                   Correct for ESL-flagged plugins. The EditorID FaceMaskArmor would only",
        ";                   resolve with powerofthree's Tweaks installed, so the FormID is used.",
        f";   StringFilters   {STRING_FILTER}  NPCs whose own or race keywords include ActorTypeNPC.",
        ";                   ',' = OR, '+' = AND, '-' = NOT (always AND-ed), '*' = partial match;",
        ";                   strings are compared with keyword EditorIDs and NPC names.",
        ";   FormFilters     NONE  e.g. races or factions as 0xID~Plugin.esm; '-' excludes.",
        ";   LevelFilters    NONE  no level or skill requirement.",
        ";   Traits          -C  not a child (C = child, '-' negates, '/' combines; also M F U S L T D)."
        if cfg.get("exclude_children", True) else
        ";   Traits          NONE  children are NOT excluded (config.yaml exclude_children: false).",
        f";   Count           {ITEM_COUNT}  masks per NPC; several matching Item lines add up.",
        ";   Chance          percent; 100 = every matching NPC. Below 100 a trailing '!' fixes each",
        ";                   NPC's roll for this character's saves (same NPCs every launch);",
        ";                   without '!' SPID re-rolls on every game launch.",
    ]
    if keyword:
        lines += [
            "; Keyword = EditorID|StringFilters|FormFilters|LevelFilters|Traits|Count|Chance",
            f";   EditorID        {KEF_PREFIX}_0x<local FormID>_<plugin name>_<extension> (KEF's format).",
            ";   Count           NONE  (SPID ignores counts for keywords); the other fields as above.",
        ]
    lines += [
        ";",
        "; SPID adds these to each matching NPC's base record when the NPC loads. It never gives",
        "; anything to the player.",
        "; Log: Documents\\My Games\\Skyrim Special Edition\\SKSE\\po3_SpellPerkItemDistributor.log",
        "; A line SPID cannot read is skipped and logged there as 'Failed to parse entry', without a",
        "; message in game. 'Registered x/y Items' (and '... Keywords') counts the lines of all ini files.",
        ";",
    ]
    if keyword:
        lines += [
            "; Examples: keep ONE active Item line and ONE active Keyword line (copy both, make the same",
            "; change in both copies, comment out the old pair):",
            ";   no Argonians/Khajiit (incl. vampires):",
            f";     Item = {form}|{STRING_FILTER}|{beast}|NONE|-C|1|100",
            f";     Keyword = {keyword}|{STRING_FILTER}|{beast}|NONE|-C|NONE|100",
            ";   everyone except NPCs named Nazeem:",
            f";     Item = {form}|{STRING_FILTER},-Nazeem|NONE|NONE|-C|1|100",
            f";     Keyword = {keyword}|{STRING_FILTER},-Nazeem|NONE|NONE|-C|NONE|100",
            ";   only one NPC in four, the same ones every launch (remove the '!' to re-roll each launch);",
            ";   the Item line follows the keyword:",
            f";     Item = {form}|{keyword}|NONE|NONE|-C|1|100",
            f";     Keyword = {keyword}|{STRING_FILTER}|NONE|NONE|-C|NONE|25!",
        ]
    else:
        lines += [
            "; Example exclusions: keep ONE active Item line (copy it, edit the copy, comment out the old):",
            ";   no Argonians/Khajiit (incl. vampires):",
            f";     Item = {form}|{STRING_FILTER}|{beast}|NONE|-C|1|100",
            ";   everyone except NPCs named Nazeem:",
            f";     Item = {form}|{STRING_FILTER},-Nazeem|NONE|NONE|-C|1|100",
            ";   only one NPC in four, the same ones every launch (remove the '!' to re-roll each launch):",
            f";     Item = {form}|{STRING_FILTER}|NONE|NONE|-C|1|25!",
        ]
    return "\n".join([*lines, "", *distribution_lines(cfg), ""])


def parse_distr_ini(text: str) -> list[tuple[str, str]]:
    """(key, value) for every entry line, the way SimpleIni sees the global section."""
    entries = []
    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line[0] in ";#":
            continue
        if line.startswith("["):
            raise PackageError(f"DISTR ini line {number}: section header {line!r}; SPID ignores "
                               "entries inside sections")
        if "=" not in line:
            raise PackageError(f"DISTR ini line {number}: not a 'key = value' entry: {line!r}")
        key, value = (part.strip() for part in line.split("=", 1))
        entries.append((key, value))
    return entries


def spid_sanitize(value: str) -> str:
    """What SPID's detail::sanitize makes of an entry value (LookupConfigs.cpp:13-53, SE/AE build:
    the VR-only block is skipped). If it changes anything SPID rewrites the ini file on disk
    (LookupConfigs.cpp:140-158), so shipped lines must come out unchanged."""
    if "~" not in value:
        value = value.replace(" - ", "~", 1)
    value = re.sub(r"\s*\|\s*", "|", value)
    value = re.sub(r"\s*,\s*", ",", value)
    value = re.sub(r"\b00+([0-9a-fA-F]{1,6})\b", r"0x\1", value)
    return re.sub(r"(0x00+)([0-9a-fA-F]+)", r"0x\2", value)


def spid_record_kind(text: str) -> str:
    """How SPID reads a FormOrEditorID section: CLibUtil distribution.hpp:86-98 (get_record_type)
    with string.hpp:65-77 (is_only_hex, '0x' prefix required)."""
    if "~" in text:
        return "formid~plugin"
    if ".es" in text:
        return "plugin"
    if re.fullmatch(r"0[xX][0-9a-fA-F]+", text):
        return "formid"
    return "editorid"


# --------------------------------------------------------------------------- build inputs

def read_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as e:
        raise PackageError(f"{path}: cannot be read: {e}") from e


def photo_size(path: Path) -> tuple[int, int] | None:
    """(width, height) of the photo as stage 1 decodes it (EXIF rotation applied)."""
    try:
        from PIL import Image
        with Image.open(path) as img:
            width, height = img.size
            if img.getexif().get(EXIF_ORIENTATION, 1) in (5, 6, 7, 8):
                width, height = height, width
            return width, height
    except (OSError, ImportError):
        return None


def outline_problems(cfg: dict, work: Path = WORK, root: Path = ROOT) -> list[str]:
    """work/outline.json (stage 1) must describe the photo and silhouette config.yaml asks for.
    Stage 2 cuts and bleeds the print for outline.json's selected_mode, stage 3 cuts the mesh for
    config.yaml's silhouette_mode: a stale outline gives a print that does not fit the mesh
    (e.g. the hair area textured with bled skin colour). Also used by src/build_all.py."""
    outline = read_json(work / "outline.json")
    if outline is None:
        return ["work/outline.json is missing: run the segment stage (src/01_segment.py)"]
    problems = []
    if outline.get("selected_mode") != cfg.get("silhouette_mode"):
        problems.append(f"work/outline.json was made for silhouette_mode {outline.get('selected_mode')!r}, "
                        f"config.yaml asks for {cfg.get('silhouette_mode')!r}")
    if outline.get("source") != cfg.get("source_image"):
        problems.append(f"work/outline.json was made from {outline.get('source')!r}, config.yaml "
                        f"source_image is {cfg.get('source_image')!r}")
    else:
        size = photo_size(root / cfg["source_image"])
        if size is None:
            problems.append(f"{cfg['source_image']} cannot be read")
        elif list(size) != list(outline.get("image_size", [])):
            problems.append(f"{cfg['source_image']} is {size[0]}x{size[1]} px but work/outline.json was "
                            f"made from a {outline.get('image_size')} photo (the photo changed)")
    matte = root / outline.get("matte", "work/matte.png")
    if not matte.is_file():
        problems.append(f"{matte.relative_to(root) if root in matte.parents else matte} is missing "
                        "(written by the segment stage)")
    return problems


def staleness_problems(work: Path = WORK) -> list[str]:
    """A stage output older than an output it was built from means a stage was skipped."""
    times = {key: (work / rel).stat().st_mtime for key, (rel, _) in BUILD_RECORDS.items()
             if (work / rel).exists()}
    problems = []
    for key, sources in BUILT_FROM.items():
        for src in sources:
            if key in times and src in times and times[key] < times[src]:
                rel, stage = BUILD_RECORDS[key]
                problems.append(f"work/{rel} is older than work/{BUILD_RECORDS[src][0]}: re-run the "
                                f"build from the {stage} stage (src/build_all.py --from {stage})")
    return problems


def build_problems(cfg: dict, work: Path = WORK, root: Path = ROOT) -> list[str]:
    """Every stage output in work/ must come from the current config.yaml and from each other."""
    mode = cfg.get("silhouette_mode")
    problems = outline_problems(cfg, work, root)
    layout = read_json(work / "atlas_layout.json")
    if layout is None:
        problems.append("work/atlas_layout.json is missing: run the textures stage")
    elif layout.get("silhouette_mode") != mode:
        problems.append(f"the textures were cut for silhouette_mode {layout.get('silhouette_mode')!r}, "
                        f"config.yaml asks for {mode!r} (re-run from the textures stage)")
    mesh = read_json(work / "mesh" / "mesh_report.json")
    if mesh is None:
        problems.append("work/mesh/mesh_report.json is missing: run the mesh stage")
    else:
        inputs = mesh.get("inputs", {})
        fixtures = [k for k in ("outline_is_fixture", "layout_is_fixture") if inputs.get(k)]
        fixtures += [f"{name}.inputs_are_fixtures" for name, v in sorted(mesh.get("variants", {}).items())
                     if v.get("inputs_are_fixtures")]
        if fixtures:
            problems.append(f"the meshes were built from synthetic test fixtures ({', '.join(fixtures)})")
        for name, variant in sorted(mesh.get("variants", {}).items()):
            if variant.get("silhouette_mode") != mode:
                problems.append(f"mesh {name} was cut for silhouette_mode {variant.get('silhouette_mode')!r}, "
                                f"config.yaml asks for {mode!r} (re-run from the mesh stage)")
    return problems + staleness_problems(work)


# --------------------------------------------------------------------------- assembly

def collect_inputs(cfg: dict) -> dict:
    plugin = WORK / "plugin" / cfg["plugin_name"]
    nifs = sorted((WORK / "nif").glob("*.nif"))
    dds = sorted((WORK / "dds").glob("*.dds"))
    missing = []
    if not plugin.is_file():
        missing.append(f"{plugin.relative_to(ROOT)} (run src/plugin/build_plugin.py)")
    if not nifs:
        missing.append("work/nif/*.nif (run src/03_mesh.py and src/04_export_nif.py)")
    if not dds:
        missing.append("work/dds/*.dds (run src/02_textures.py and src/05_dds.py)")
    if missing:
        raise PackageError("missing inputs:\n  " + "\n  ".join(missing))
    return {"plugin": plugin, "nifs": nifs, "dds": dds}


def remove_in_dist(*paths: Path) -> None:
    """Delete previous package files or folders, but only inside this repo's dist/ folder."""
    for path in paths:
        resolved = path.resolve()
        if DIST.resolve() not in resolved.parents:
            raise PackageError(f"refusing to delete {resolved}: not inside {DIST}")
        if path.is_dir():
            shutil.rmtree(path)
        elif path.exists():
            path.unlink()


def assemble(cfg: dict, pkg_dir: Path) -> None:
    inputs = collect_inputs(cfg)
    mesh_dir = pkg_dir / "meshes" / MOD_NAME
    tex_dir = pkg_dir / "textures" / MOD_NAME
    mesh_dir.mkdir(parents=True)
    tex_dir.mkdir(parents=True)
    shutil.copyfile(inputs["plugin"], pkg_dir / cfg["plugin_name"])
    for src in inputs["nifs"]:
        shutil.copyfile(src, mesh_dir / src.name)
    for src in inputs["dds"]:
        shutil.copyfile(src, tex_dir / src.name)
    ini = make_distr_ini(cfg)
    (pkg_dir / distr_ini_name(cfg["plugin_name"])).write_bytes(ini.encode("ascii"))


# --------------------------------------------------------------------------- validation helpers

def package_files(pkg_dir: Path) -> list[str]:
    """Every file below pkg_dir as a POSIX relative path, sorted."""
    return sorted(p.relative_to(pkg_dir).as_posix() for p in pkg_dir.rglob("*") if not p.is_dir())


def exists_exact(root: Path, rel: str) -> bool:
    """True if root/rel exists with exactly this spelling (also on case-insensitive filesystems)."""
    here = root
    for part in PurePosixPath(rel).parts:
        if not here.is_dir() or part not in {p.name for p in here.iterdir()}:
            return False
        here = here / part
    return here.is_file()


def game_path(raw: str) -> str | None:
    """Backslash game path -> POSIX relative path; None if absolute or escaping upwards."""
    parts = PureWindowsPath(raw.strip()).parts
    if not parts or PureWindowsPath(raw).anchor or ".." in parts:
        return None
    return "/".join(parts)


def dds_problems(path: Path) -> list[str]:
    """Header sanity only; src/05_dds.py does the deep verification."""
    data = path.read_bytes()[:148]
    if len(data) < 128 or data[:4] != b"DDS " or struct.unpack_from("<I", data, 4)[0] != 124:
        return [f"{path.name}: not a DDS file"]
    height, width = struct.unpack_from("<II", data, 12)
    mips = struct.unpack_from("<I", data, 28)[0]
    fourcc = data[84:88]
    problems = []
    for name, n in (("width", width), ("height", height)):
        if n <= 0 or n & (n - 1):
            problems.append(f"{path.name}: {name} {n} is not a power of two")
    if not problems and mips != int(math.log2(max(width, height))) + 1:
        problems.append(f"{path.name}: {mips} mip levels, expected a full chain")
    if fourcc not in DDS_FOURCC_OK:
        problems.append(f"{path.name}: unexpected FourCC {fourcc!r}")
    elif fourcc == b"DX10" and struct.unpack_from("<I", data, 128)[0] != DXGI_BC7_UNORM:
        problems.append(f"{path.name}: DX10 header but not BC7_UNORM")
    return problems


def nif_texture_refs(path: Path) -> list[tuple[str, str, str]]:
    """(shape name, 'slot N', raw path) for every non-empty texture slot in the NIF; N is the
    0-based index into BSShaderTextureSet.Textures (0 diffuse ... 8), as nifly and NifSkope count.

    pyn's shape.textures only lists the slots its shader flags enable and never reads index 8
    (pynifly.py:3527-3580), so every index 0-8 is read directly. pyn's _readtexture(layer) reads
    nifly index layer-1 (pynifly.py:3520-3524); nifly returns '' for empty or missing slots and also
    maps BSEffectShaderProperty textures onto slot numbers (NifFile.cpp GetTextureSlot)."""
    try:
        from cfm.nifenv import NifFile  # needs the NiflyDLL (tools/setup.sh builds it)
    except Exception as e:  # noqa: BLE001 - surface any loader problem as a package error
        raise PackageError(f"cannot load PyNifly to read NIF textures: {e}") from e
    nif = NifFile(str(path))
    shapes = list(nif.shapes)
    if not shapes:
        raise PackageError(f"{path.name}: no shapes")
    refs = []
    for shape in shapes:
        shader = shape.shader
        for index in range(TEXTURE_SLOTS):
            tex = shader._readtexture(nif._handle, shape._handle, index + 1)
            if tex:
                refs.append((shape.name, f"slot {index}", tex))
    return refs


# --------------------------------------------------------------------------- validation

def check_layout(files: list[str], plugin_name: str, ini_name: str) -> list[str]:
    problems = []
    if not plugin_name.endswith(".esp"):
        problems.append(f"plugin {plugin_name!r} must be an .esp (ESL-flagged ESP, CLAUDE.md)")
    for required in (plugin_name, ini_name):
        if required not in files:
            problems.append(f"missing {required}")
    for rel in files:
        parts = rel.split("/")
        if rel in (plugin_name, ini_name):
            continue
        if len(parts) == 3 and parts[0] in ("meshes", "textures") and parts[1] == MOD_NAME:
            want = ".nif" if parts[0] == "meshes" else ".dds"
            if not parts[2].endswith(want):
                problems.append(f"stray file {rel} (only {want} belongs in {parts[0]}/)")
            elif not ASSET_NAME_RE.match(parts[2]):
                problems.append(f"{rel}: asset file names must be lower-case [a-z0-9_]")
            continue
        problems.append(f"stray file {rel} (allowed: plugin, DISTR ini, meshes/{MOD_NAME}/*.nif, "
                        f"textures/{MOD_NAME}/*.dds)")
    return problems


def check_plugin(pkg_dir: Path, plugin_name: str, files: list[str], report: dict) -> list[str]:
    try:
        plugin = read_plugin(pkg_dir / plugin_name)
    except (OSError, PluginFormatError, ValueError) as e:
        return [f"{plugin_name}: cannot be read: {e}"]
    problems = []
    if not plugin.is_esl:
        problems.append(f"{plugin_name}: ESL flag (0x200) is not set")
    if plugin.header.flags & LOCALIZED_FLAG:
        problems.append(f"{plugin_name}: localized flag set, but no .strings files are shipped")
    if plugin.masters != REQUIRED_MASTERS:
        problems.append(f"{plugin_name}: masters {plugin.masters}, expected {REQUIRED_MASTERS}")
    for rec in plugin.own_records():
        if not ESL_ID_RANGE[0] <= rec.object_index <= ESL_ID_RANGE[1]:
            problems.append(f"{plugin_name}: {rec.type} {rec.editor_id} 0x{rec.object_index:X} "
                            "is outside the ESL range 0x800-0xFFF")
    armo = [r for r in plugin.own_records() if r.object_index == ARMO_LOCAL_ID]
    if len(armo) != 1 or armo[0].type != "ARMO" or armo[0].editor_id != ARMO_EDITOR_ID:
        found = [(r.type, r.editor_id) for r in armo]
        problems.append(f"{plugin_name}: expected ARMO {ARMO_EDITOR_ID} at 0x{ARMO_LOCAL_ID:X}, "
                        f"found {found}")

    referenced = set()
    for rec, sig, raw in plugin.model_paths():
        rel = game_path(raw)
        where = f"{plugin_name} {rec.type} {rec.editor_id} {sig}"
        if rel is None:
            problems.append(f"{where}: bad model path {raw!r}")
        elif rel.lower().startswith("meshes/"):
            problems.append(f"{where}: {raw!r} must be relative to meshes\\ (no 'meshes\\' prefix)")
        elif not exists_exact(pkg_dir / "meshes", rel):
            problems.append(f"{where}: meshes/{rel} is not in the package (exact case)")
        else:
            referenced.add(f"meshes/{rel}")
    for rec, sig, raw in plugin.texture_paths():
        rel = game_path(raw)
        if rel is None or not exists_exact(pkg_dir / "textures", rel):
            problems.append(f"{plugin_name} {rec.type} {sig}: textures/{raw} is not in the package")
    shipped = {f for f in files if f.startswith("meshes/")}
    for rel in sorted(shipped - referenced):
        problems.append(f"stray mesh {rel}: no record in {plugin_name} uses it")
    report["plugin"] = {
        "esl": plugin.is_esl, "masters": plugin.masters,
        "records": [{"type": r.type, "editor_id": r.editor_id, "local_id": f"0x{r.object_index:X}"}
                    for r in plugin.own_records()],
        "model_paths": sorted({raw for _, _, raw in plugin.model_paths()}),
    }
    return problems


def check_textures(pkg_dir: Path, files: list[str], report: dict) -> list[str]:
    problems, referenced, per_nif = [], set(), {}
    for rel in (f for f in files if f.endswith(".nif") and f.startswith("meshes/")):
        try:
            refs = nif_texture_refs(pkg_dir / rel)
        except PackageError as e:
            problems.append(str(e))
            continue
        per_nif[rel] = sorted({raw for _, _, raw in refs})
        for shape, slot, raw in refs:
            path = game_path(raw)
            where = f"{rel} shape {shape!r} {slot}"
            if path is None or not path.startswith("textures/"):
                problems.append(f"{where}: {raw!r} must be a relative path starting 'textures\\'")
            elif not exists_exact(pkg_dir, path):
                problems.append(f"{where}: {path} is not in the package (exact case)")
            else:
                referenced.add(path)
    for rel in sorted(f for f in files if f.startswith("textures/")):
        if rel not in referenced:
            problems.append(f"stray texture {rel}: no NIF uses it")
        problems += dds_problems(pkg_dir / rel)
    report["nif_textures"] = per_nif
    return problems


def check_manifest(report: dict) -> list[str]:
    """Cross-check against work/nif_manifest.json when the build products are present."""
    manifest_path = WORK / "nif_manifest.json"
    if not manifest_path.exists() or "plugin" not in report:
        return []
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    problems = []
    models = set(manifest.get("models", {}).values())
    if manifest.get("ground_model"):
        models.add(manifest["ground_model"])
    if models != set(report["plugin"]["model_paths"]):
        problems.append(f"ESP model paths {sorted(report['plugin']['model_paths'])} differ from "
                        f"work/nif_manifest.json {sorted(models)} (rebuild the plugin)")
    used = {t for refs in report.get("nif_textures", {}).values() for t in refs}
    listed = set(manifest.get("textures", []))
    if used and listed and used != listed:
        problems.append(f"NIF texture paths {sorted(used)} differ from work/nif_manifest.json "
                        f"{sorted(listed)}")
    return problems


def chance_percent_of(section: str) -> float:
    """A chance section as SPID reads it: empty or NONE = 100 (LookupConfigs.h:575-591)."""
    text = section.rstrip("!")
    if text.upper() in ("", "NONE"):
        return 100.0
    try:
        return float(text)
    except ValueError:
        return math.nan


def pairing_problems(item: list[str], keyword: list[str], name: str) -> list[str]:
    """The Item and the KEFEquip Keyword line must reach the same NPCs (see distribution_lines):
    same filters and chance, or, below 100 %, the Item line filtered on the keyword at 100 %."""
    item = (item + [""] * SPID_SECTIONS)[:SPID_SECTIONS]
    keyword = (keyword + [""] * SPID_SECTIONS)[:SPID_SECTIONS]
    if item[2:5] != keyword[2:5]:
        return [f"Item and Keyword lines have different form/level/trait filters {item[2:5]} vs "
                f"{keyword[2:5]}: NPCs would get a mask KEF does not equip, or a keyword without mask"]
    if item[1] == name and chance_percent_of(item[6]) >= 100:
        return []   # the Item line follows the keyword
    if chance_percent_of(keyword[6]) < 100:
        return [f"the Keyword line has chance {keyword[6]!r}: the Item line must then use {name} as "
                "its string filter with chance 100 (two chance rolls are independent, SPID "
                "LookupConfigs.cpp:73-74, LookupFilters.cpp:201-208)"]
    if item[1] != keyword[1] or chance_percent_of(item[6]) < 100:
        return [f"Item and Keyword lines select different NPCs (string filters {item[1]!r} vs "
                f"{keyword[1]!r}, chance {item[6]!r} vs {keyword[6]!r})"]
    return []


def check_ini(pkg_dir: Path, cfg: dict, plugin_name: str, ini_name: str, report: dict) -> list[str]:
    path = pkg_dir / ini_name
    if not path.is_file():
        return []  # already reported by check_layout
    raw = path.read_bytes()
    try:
        text = raw.decode("ascii")
    except UnicodeDecodeError:
        return [f"{ini_name}: must be plain ASCII"]
    problems = []
    if "_DISTR" not in ini_name or not ini_name.endswith(".ini"):
        problems.append(f"{ini_name}: SPID needs '_DISTR' in the name and a lower-case .ini")
    try:
        entries = parse_distr_ini(text)
    except PackageError as e:
        return [f"{ini_name}: {e}"]
    method = equip_method(cfg)
    allowed = ("Item", "Keyword") if method == "kef" else ("Item",)
    items = [v.split("|") for k, v in entries if k == "Item"]
    keywords = [v.split("|") for k, v in entries if k == "Keyword"]
    others = [(k, v) for k, v in entries if k not in allowed]
    if others:
        problems.append(f"{ini_name}: unexpected entries {others} (equip_method {method})")
    if not items:
        problems.append(f"{ini_name}: no active 'Item = ...' line")
    elif len(items) > 1:
        problems.append(f"{ini_name}: {len(items)} active Item lines; matching Item lines add up "
                        "to several masks per NPC")
    for key, value in entries:
        sections = value.split("|")
        if len(sections) > SPID_SECTIONS:
            problems.append(f"{ini_name}: {key} line has {len(sections)} sections, SPID accepts at "
                            f"most {SPID_SECTIONS}")
        if spid_sanitize(value) != value:
            problems.append(f"{ini_name}: SPID would rewrite {key} = {value!r} as "
                            f"{spid_sanitize(value)!r} (and save the file)")
    for sections in items:
        match = re.fullmatch(r"0x([0-9A-Fa-f]+)~(.+)", sections[0])
        if not match:
            problems.append(f"{ini_name}: {sections[0]!r} is not '0xID~Plugin.esp'")
            continue
        if match.group(2) != plugin_name:
            problems.append(f"{ini_name}: entry targets {match.group(2)!r}, shipped plugin is {plugin_name!r}")
        if int(match.group(1), 16) != ARMO_LOCAL_ID:
            problems.append(f"{ini_name}: FormID 0x{match.group(1)} is not the mask ARMO 0x{ARMO_LOCAL_ID:X}")
    name = None
    if method == "kef":
        name = kef_keyword(plugin_name)
        if len(keywords) != 1:
            problems.append(f"{ini_name}: {len(keywords)} active 'Keyword = {name}|...' lines, "
                            "expected exactly one (equip_method kef)")
        for sections in keywords:
            if sections[0] != name:
                problems.append(f"{ini_name}: keyword {sections[0]!r} is not {name!r} (KEF would equip "
                                "another item, or nothing)")
            elif spid_record_kind(sections[0]) != "editorid":
                problems.append(f"{ini_name}: SPID would read {sections[0]!r} as a "
                                f"{spid_record_kind(sections[0])}, not as a keyword EditorID")
            if len(sections) > 5 and sections[5].upper() not in ("", "NONE"):
                problems.append(f"{ini_name}: Keyword count {sections[5]!r} must be NONE (SPID ignores "
                                "counts for keywords)")
        if len(items) == 1 and len(keywords) == 1:
            problems += [f"{ini_name}: {p}" for p in pairing_problems(items[0], keywords[0], name)]
    expected = make_distr_ini(cfg)
    if text != expected:
        problems.append(f"{ini_name}: differs from what config.yaml generates (rebuild the package)")
    report["equip_method"] = method
    report["kef_keyword"] = name
    report["distr_lines"] = [f"{k} = {v}" for k, v in entries]
    return problems


def validate_package(pkg_dir: Path, cfg: dict) -> dict:
    """Run every check; raise PackageError listing all problems, else return the report."""
    if not pkg_dir.is_dir():
        raise PackageError(f"{pkg_dir} does not exist (run src/07_package.py)")
    plugin_name = cfg["plugin_name"]
    ini_name = distr_ini_name(plugin_name)
    files = package_files(pkg_dir)
    report: dict = {"package_dir": pkg_dir.relative_to(ROOT).as_posix() if ROOT in pkg_dir.parents
                    else str(pkg_dir)}
    problems = check_layout(files, plugin_name, ini_name)
    symlinks = [p for p in pkg_dir.rglob("*") if p.is_symlink()]
    problems += [f"symlink {p.relative_to(pkg_dir)} (ship real files)" for p in symlinks]
    if plugin_name in files:
        problems += check_plugin(pkg_dir, plugin_name, files, report)
    problems += check_textures(pkg_dir, files, report)
    problems += check_manifest(report)
    problems += check_ini(pkg_dir, cfg, plugin_name, ini_name, report)
    if problems:
        raise PackageError("package validation failed:\n  " + "\n  ".join(problems))
    report["files"] = [{"path": rel, "bytes": (pkg_dir / rel).stat().st_size,
                        "sha256": hashlib.sha256((pkg_dir / rel).read_bytes()).hexdigest()}
                       for rel in files]
    return report


# --------------------------------------------------------------------------- zip

def write_zip(pkg_dir: Path, zip_path: Path) -> None:
    """Deterministic zip: sorted entries, fixed timestamps and permissions, Data-relative names."""
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for rel in package_files(pkg_dir):
            info = zipfile.ZipInfo(rel, date_time=ZIP_TIMESTAMP)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            info.create_system = 3  # unix, so the permission bits above are honoured
            zf.writestr(info, (pkg_dir / rel).read_bytes())


def verify_zip(zip_path: Path, pkg_dir: Path) -> dict:
    if not zip_path.is_file():
        raise PackageError(f"{zip_path} does not exist")
    files = package_files(pkg_dir)
    with zipfile.ZipFile(zip_path) as zf:
        bad = zf.testzip()
        if bad:
            raise PackageError(f"{zip_path.name}: CRC error in {bad}")
        names = zf.namelist()
        unsafe = [n for n in names if n.startswith("/") or "\\" in n or ".." in n.split("/")]
        if unsafe:
            raise PackageError(f"{zip_path.name}: unsafe entry names {unsafe}")
        if sorted(names) != files:
            raise PackageError(f"{zip_path.name}: entries {sorted(names)} != package files {files}")
        for name in names:
            if zf.read(name) != (pkg_dir / name).read_bytes():
                raise PackageError(f"{zip_path.name}: {name} differs from {pkg_dir.name}/{name}")
    return {"path": zip_path.relative_to(ROOT).as_posix() if ROOT in zip_path.parents else str(zip_path),
            "bytes": zip_path.stat().st_size,
            "sha256": hashlib.sha256(zip_path.read_bytes()).hexdigest(), "entries": len(names)}


# --------------------------------------------------------------------------- main

def print_summary(report: dict) -> None:
    print(f"package: {report['package_dir']}")
    for f in report["files"]:
        print(f"  {f['bytes']:>10,d}  {f['path']}")
    for line in report.get("distr_lines", []):
        print(f"SPID: {line}")
    if "zip" in report:
        z = report["zip"]
        print(f"zip:  {z['path']} ({z['bytes']:,d} bytes, {z['entries']} entries, sha256 {z['sha256'][:16]}...)")


def build_package(cfg: dict, with_zip: bool = True) -> dict:
    """Check work/, assemble + validate (+ zip) in dist/.staging, then replace the old package.
    On any problem the previous package is left untouched."""
    problems = build_problems(cfg)
    if problems:
        raise PackageError("the build products in work/ do not match config.yaml or each other:\n  "
                           + "\n  ".join(problems))
    staged_dir, staged_zip = STAGING / MOD_NAME, STAGING / ZIP_PATH.name
    remove_in_dist(STAGING)
    try:
        assemble(cfg, staged_dir)
        report = validate_package(staged_dir, cfg)
        if with_zip:
            write_zip(staged_dir, staged_zip)
            verify_zip(staged_zip, staged_dir)
        remove_in_dist(MOD_DIST, ZIP_PATH)
        staged_dir.rename(MOD_DIST)
        if with_zip:
            staged_zip.rename(ZIP_PATH)
    finally:
        remove_in_dist(STAGING)
    report["package_dir"] = MOD_DIST.relative_to(ROOT).as_posix()
    report["build_inputs"] = {"silhouette_mode": cfg.get("silhouette_mode"),
                              "source_image": cfg.get("source_image"), "consistent": True}
    if with_zip:
        report["zip"] = verify_zip(ZIP_PATH, MOD_DIST)
    return report


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--check-only", action="store_true",
                    help="validate the existing dist/ package and zip only (no rebuild, no build-input "
                         "gate, no report written)")
    ap.add_argument("--print-ini", action="store_true", help="print the DISTR ini and exit")
    ap.add_argument("--no-zip", action="store_true", help="do not write the zip (an old zip is removed)")
    args = ap.parse_args(argv)
    cfg = load_config()
    if cfg.get("distribute_to_player"):
        print("note: distribute_to_player is ignored - SPID never distributes to the player "
              "(see docs/SPID_NOTES.md); use the console to try the mask on yourself.")
    try:
        if args.print_ini:
            print(make_distr_ini(cfg), end="")
            return 0
        if equip_method(cfg) == "kef":
            print(f"note: equip_method kef - NPCs only wear the mask in game when KEF (Keyword Equip "
                  f"Framework, {KEF_URL}) is installed; without it they are expected to just carry it.")
        if args.check_only:
            report = validate_package(MOD_DIST, cfg)
            if not args.no_zip:
                report["zip"] = verify_zip(ZIP_PATH, MOD_DIST)
        else:
            report = build_package(cfg, with_zip=not args.no_zip)
    except PackageError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    if not args.check_only:
        REPORT_PATH.write_text(json.dumps(report, indent=1) + "\n", encoding="utf-8")
    print_summary(report)
    print("package OK" + ("" if args.check_only else f"; report: {REPORT_PATH.relative_to(ROOT)}"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
