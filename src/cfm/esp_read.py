"""Tiny read-only TES5 (Skyrim SE) plugin reader for the package validator.

It only needs what packaging checks: the header flags and masters, every record's type,
FormID and EditorID, and the model/texture path fields. Nothing here writes plugins.

Binary layout (UESP "Skyrim Mod:Mod File Format"):
  record = type[4] dataSize:u32 flags:u32 formID:u32 vcInfo:u32 formVersion:u16 unknown:u16, data
  GRUP   = "GRUP" groupSize:u32 (includes its 24-byte header) label[4] groupType:i32 stamp:u16
           vc:u16 unknown:u32, then records or nested GRUPs
  field  = type[4] size:u16 data; an XXXX field carries the u32 size of the next field
  record flag 0x00040000: data = decompressedSize:u32 + zlib stream
Path fields (UESP ".../ARMO" and ".../ARMA"; both pages fetched 2026-10-03):
  ARMO: MOD2 male world model, MOD4 female world model (Models\\ relative .nif),
        ICON / ICO2 inventory images; MODL is the armature (ARMA) FormID, not a path.
  ARMA: MOD2..MOD5 biped models (Models\\ relative .nif); MODL is an additional-race FormID.
  Most other record types store their model path in MODL.
"""
from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass, field
from pathlib import Path

COMPRESSED = 0x00040000
ESM_FLAG = 0x00000001
LOCALIZED_FLAG = 0x00000080
ESL_FLAG = 0x00000200

# Fields holding a path relative to Data\meshes, per record type.
MODEL_FIELDS = {
    "ARMO": ("MOD2", "MOD4"),
    "ARMA": ("MOD2", "MOD3", "MOD4", "MOD5"),
}
DEFAULT_MODEL_FIELDS = ("MODL",)
# Fields holding a path relative to Data\textures, per record type.
TEXTURE_FIELDS = {
    "ARMO": ("ICON", "ICO2"),
}


class PluginFormatError(ValueError):
    """The file is not a well-formed plugin (as far as this reader can tell)."""


@dataclass
class Record:
    type: str
    flags: int
    form_id: int
    fields: list[tuple[str, bytes]] = field(repr=False)

    def all(self, sig: str) -> list[bytes]:
        return [data for t, data in self.fields if t == sig]

    def zstring(self, sig: str) -> str | None:
        """First occurrence of a NUL-terminated string field, or None."""
        found = self.all(sig)
        if not found:
            return None
        return found[0].split(b"\0", 1)[0].decode("cp1252")

    @property
    def editor_id(self) -> str | None:
        return self.zstring("EDID")

    @property
    def object_index(self) -> int:
        """FormID without the load-order byte (the 'local' ID used by SPID's 0xID~Plugin.esp)."""
        return self.form_id & 0x00FFFFFF


@dataclass
class Plugin:
    path: Path
    header: Record
    masters: list[str]
    records: list[Record]

    @property
    def is_esl(self) -> bool:
        return bool(self.header.flags & ESL_FLAG)

    @property
    def is_esm(self) -> bool:
        return bool(self.header.flags & ESM_FLAG)

    def own_records(self) -> list[Record]:
        """Records this plugin defines (master byte == number of masters), not overrides."""
        own = len(self.masters)
        return [r for r in self.records if (r.form_id >> 24) == own]

    def model_paths(self) -> list[tuple[Record, str, str]]:
        """(record, field signature, path) for every mesh path, relative to Data\\meshes."""
        return self._paths(lambda t: MODEL_FIELDS.get(t, DEFAULT_MODEL_FIELDS))

    def texture_paths(self) -> list[tuple[Record, str, str]]:
        """(record, field signature, path) for every texture path, relative to Data\\textures."""
        return self._paths(lambda t: TEXTURE_FIELDS.get(t, ()))

    def _paths(self, sigs_for) -> list[tuple[Record, str, str]]:
        out = []
        for rec in self.records:
            for sig in sigs_for(rec.type):
                for data in rec.all(sig):
                    text = data.split(b"\0", 1)[0].decode("cp1252")
                    if text:
                        out.append((rec, sig, text))
        return out


def parse_fields(data: bytes) -> list[tuple[str, bytes]]:
    fields, pos, big = [], 0, None
    while pos < len(data):
        if pos + 6 > len(data):
            raise PluginFormatError("truncated field header")
        sig = data[pos:pos + 4].decode("ascii", errors="replace")
        size = struct.unpack_from("<H", data, pos + 4)[0]
        pos += 6
        if big is not None:
            size, big = big, None
        if sig == "XXXX":
            big = struct.unpack_from("<I", data, pos)[0]
        else:
            fields.append((sig, data[pos:pos + size]))
        pos += size
    if pos != len(data):
        raise PluginFormatError("field data overruns its record")
    return fields


def _parse_record(buf: bytes, pos: int) -> tuple[Record, int]:
    if pos + 24 > len(buf):
        raise PluginFormatError(f"truncated record header at offset {pos}")
    sig = buf[pos:pos + 4].decode("ascii", errors="replace")
    size, flags, form_id = struct.unpack_from("<III", buf, pos + 4)
    end = pos + 24 + size
    if end > len(buf):
        raise PluginFormatError(f"{sig} at offset {pos} runs past the end of the file")
    data = buf[pos + 24:end]
    if flags & COMPRESSED:
        data = zlib.decompress(data[4:])
    return Record(sig, flags, form_id, parse_fields(data)), end


def _parse_group(buf: bytes, pos: int, out: list[Record]) -> int:
    size = struct.unpack_from("<I", buf, pos + 4)[0]
    end = pos + size
    if size < 24 or end > len(buf):
        raise PluginFormatError(f"bad GRUP size at offset {pos}")
    rpos = pos + 24
    while rpos < end:
        if buf[rpos:rpos + 4] == b"GRUP":
            rpos = _parse_group(buf, rpos, out)
        else:
            rec, rpos = _parse_record(buf, rpos)
            out.append(rec)
    if rpos != end:
        raise PluginFormatError(f"GRUP at offset {pos} size mismatch")
    return end


def read_plugin(path: Path | str) -> Plugin:
    path = Path(path)
    buf = path.read_bytes()
    if buf[:4] != b"TES4":
        raise PluginFormatError(f"{path.name}: does not start with a TES4 header record")
    header, pos = _parse_record(buf, 0)
    masters = [d.split(b"\0", 1)[0].decode("cp1252") for t, d in header.fields if t == "MAST"]
    records: list[Record] = []
    while pos < len(buf):
        if buf[pos:pos + 4] != b"GRUP":
            raise PluginFormatError(f"{path.name}: expected a top-level GRUP at offset {pos}")
        pos = _parse_group(buf, pos, records)
    return Plugin(path, header, masters, records)
