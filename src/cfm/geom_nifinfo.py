"""NIF inspection: block lists and shape summaries through pyn, plus an independent minimal reader.

The independent reader (RawNif) parses only what the tests need straight from the file bytes,
following niftools nif.xml for version 20.2.0.7 / user version 12 / BS version 100 (Skyrim SE):
header (version, block types, block sizes, string table), BSShaderTextureSet texture paths,
NiSkinInstance / BSDismemberSkinInstance references + partitions and NiSkinData transforms.
It gives a second opinion that does not share code with NiflyDLL."""
from __future__ import annotations

import struct
from ctypes import create_string_buffer
from pathlib import Path

import numpy as np

from .geom_xform import NifFile, xf_to_mat
from pyn.niflydll import nifly  # noqa: E402
from pyn.nifconstants import ShaderFlags1, ShaderFlags2  # noqa: E402

VF_FLAGS = {"VERTEX": 1 << 0, "UV": 1 << 1, "UV_2": 1 << 2, "NORMAL": 1 << 3, "TANGENT": 1 << 4,
            "COLORS": 1 << 5, "SKINNED": 1 << 6, "LANDDATA": 1 << 7, "EYEDATA": 1 << 8,
            "FULLPREC": 1 << 10}   # nifly include/VertexData.hpp VertexFlags


def vertex_flags(desc: int) -> list:
    return [k for k, b in VF_FLAGS.items() if desc & b]


def block_list(nif) -> list:
    """[(id, block type)] for every block, via NiflyDLL getBlockname."""
    out, i = [], 0
    buf = create_string_buffer(256)
    while True:
        n = nifly.getBlockname(nif._handle, i, buf, 256)
        if n <= 0:
            return out
        out.append((i, buf.value.decode("utf-8")))
        i += 1


def shader_summary(shape) -> dict:
    p = shape.shader.properties
    return {
        "block": shape.shader_block_name,
        "shader_type": int(p.Shader_Type),
        "flags1": ShaderFlags1(p.Shader_Flags_1).fullname, "flags1_hex": hex(p.Shader_Flags_1),
        "flags2": ShaderFlags2(p.Shader_Flags_2).fullname, "flags2_hex": hex(p.Shader_Flags_2),
        "glossiness": round(float(p.Glossiness), 4), "spec_strength": round(float(p.Spec_Str), 4),
        "spec_color": [round(float(x), 4) for x in p.Spec_Color],
        "emissive_color": [round(float(x), 4) for x in p.Emissive_Color][:3],
        "emissive_mult": round(float(p.Emissive_Mult), 4), "alpha": round(float(p.Alpha), 4),
        "env_map_scale": round(float(p.Env_Map_Scale), 4), "soft_lighting": round(float(p.Soft_Lighting), 4),
        "rim_light_power": round(float(p.Rim_Light_Power), 4),
        "texture_clamp_mode": int(p.textureClampMode),
        "uv_offset": [round(float(p.UV_Offset_U), 4), round(float(p.UV_Offset_V), 4)],
        "uv_scale": [round(float(p.UV_Scale_U), 4), round(float(p.UV_Scale_V), 4)],
        "textures": {k: v for k, v in shape.textures.items()},
        "alpha_property": bool(shape.has_alpha_property),
    }


def shape_summary(shape) -> dict:
    pr = shape.properties
    out = {
        "name": shape.name, "block": shape.blockname,
        "vertices": int(pr.vertexCount), "triangles": int(pr.triangleCount),
        "vertex_flags": vertex_flags(int(pr.vertexDesc)), "node_flags": int(pr.flags),
        "transform": xf_to_mat(shape.transform).round(5).tolist(),
        "bound_sphere": [round(float(x), 4) for x in pr.boundingSphereCenter] + [round(float(pr.boundingSphereRadius), 4)],
        "skin_instance": shape.skin_instance_name,
        "shader": shader_summary(shape),
    }
    if shape.skin_instance_name:
        out["bones"] = list(shape.bone_names)
        out["global_to_skin"] = xf_to_mat(shape.global_to_skin).round(5).tolist()
        out["skin_to_bone"] = {b: xf_to_mat(shape.get_shape_skin_to_bone(b)).round(5).tolist()
                               for b in shape.bone_names}
        out["partitions"] = [{"id": p.id, "flags": int(p.flags), "name": p.name} for p in shape.partitions]
    return out


def nif_summary(path) -> dict:
    nif = NifFile(str(path))
    nodes = {}
    for name, node in nif.nodes.items():
        if node.blockname in ("NiNode", "BSFadeNode") and not hasattr(node, "verts"):
            nodes[name] = {"block": node.blockname, "flags": int(node.flags),
                           "transform": xf_to_mat(node.transform).round(5).tolist(),
                           "parent": node.parent.name if node.parent else None}
    return {"game": nif.game, "root": [nif.root.blockname, nif.rootName],
            "blocks": block_list(nif), "nodes": nodes,
            "shapes": [shape_summary(s) for s in nif.shapes]}


# --- independent byte-level reader ------------------------------------------------------------------


class _Reader:
    def __init__(self, data: bytes, pos: int = 0):
        self.d, self.p = data, pos

    def take(self, fmt: str):
        size = struct.calcsize("<" + fmt)
        vals = struct.unpack_from("<" + fmt, self.d, self.p)
        self.p += size
        return vals if len(vals) > 1 else vals[0]

    def sized_string(self) -> str:
        n = self.take("I")
        s = self.d[self.p:self.p + n].decode("latin-1")
        self.p += n
        return s

    def export_string(self) -> str:
        n = self.take("B")
        s = self.d[self.p:self.p + n].rstrip(b"\0").decode("latin-1")
        self.p += n
        return s

    def transform(self) -> np.ndarray:
        """NiTransform: Matrix33 (9 floats as stored), Vector3, float -> 4x4 in nifly's convention."""
        rot = np.array(self.take("9f")).reshape(3, 3)
        t = np.array(self.take("3f"))
        s = self.take("f")
        m = np.eye(4)
        m[:3, :3] = rot * s
        m[:3, 3] = t
        return m


class RawNif:
    """Minimal Skyrim SE NIF reader (header + a few block types), independent of NiflyDLL."""

    def __init__(self, path):
        self.data = Path(path).read_bytes()
        r = _Reader(self.data)
        end = self.data.index(b"\n")
        self.header_string = self.data[:end].decode("latin-1")
        r.p = end + 1
        self.version = r.take("I")
        self.endian = r.take("B")
        self.user_version = r.take("I")
        self.num_blocks = r.take("I")
        self.bs_version = r.take("I")
        self.author, self.process_script, self.export_script = (r.export_string() for _ in range(3))
        n_types = r.take("H")
        self.block_types = [r.sized_string() for _ in range(n_types)]
        self.type_index = [r.take("H") for _ in range(self.num_blocks)]
        self.block_sizes = [r.take("I") for _ in range(self.num_blocks)]
        n_strings = r.take("I")
        r.take("I")  # max string length
        self.strings = [r.sized_string() for _ in range(n_strings)]
        n_groups = r.take("I")
        r.take(f"{n_groups}I") if n_groups else None
        self.offsets = list(np.cumsum([r.p] + self.block_sizes[:-1]))
        self.end_of_blocks = int(r.p + sum(self.block_sizes))

    def block_type(self, i: int) -> str:
        return self.block_types[self.type_index[i]]

    def blocks(self) -> list:
        return [self.block_type(i) for i in range(self.num_blocks)]

    def _at(self, i: int) -> _Reader:
        return _Reader(self.data, int(self.offsets[i]))

    def texture_set(self, i: int) -> list:
        r = self._at(i)
        return [r.sized_string() for _ in range(r.take("i"))]

    def skin_instance(self, i: int) -> dict:
        r = self._at(i)
        data, part, root, nb = r.take("iiiI")
        out = {"data": data, "skin_partition": part, "skeleton_root": root,
               "bones": [r.take("i") for _ in range(nb)]}
        if self.block_type(i) == "BSDismemberSkinInstance":
            n = r.take("I")
            out["partitions"] = [dict(zip(("flags", "body_part"), r.take("HH"))) for _ in range(n)]
        return out

    def skin_data(self, i: int) -> dict:
        r = self._at(i)
        g2s = r.transform()
        nb = r.take("I")
        has_w = r.take("B")
        bones = []
        for _ in range(nb):
            s2b = r.transform()
            sphere = r.take("4f")
            nv = r.take("H")
            weights = [r.take("Hf") for _ in range(nv)] if has_w else []
            bones.append({"skin_to_bone": s2b, "bound_sphere": sphere, "weights": weights})
        return {"global_to_skin": g2s, "has_vertex_weights": has_w, "bones": bones}


def _fmt_mat(m) -> str:
    m = np.asarray(m)
    rot = " ".join(f"{x:+.4f}" for x in m[:3, :3].ravel())
    return f"t=({m[0, 3]:.4f}, {m[1, 3]:.4f}, {m[2, 3]:.4f}) R=[{rot}]"


def markdown_dump(path) -> str:
    """Block-by-block dump (raw reader for structure, pyn for decoded fields) as markdown."""
    raw = RawNif(path)
    nif = NifFile(str(path))
    lines = [f"`{Path(path).name}`: {raw.header_string}, user version {raw.user_version}, "
             f"BS version {raw.bs_version}, {raw.num_blocks} blocks, process script "
             f"\"{raw.process_script}\", strings {raw.strings}", "",
             "| # | block | bytes | contents |", "|---|---|---|---|"]
    shape_iter = iter(nif.shapes)
    shape = None
    for i, b in enumerate(raw.blocks()):
        info = ""
        if b in ("NiNode", "BSFadeNode"):
            name = next((n for n, node in nif.nodes.items() if node.id == i), "?")
            node = nif.nodes.get(name)
            info = f"\"{name}\" flags {node.flags if node else '?'}; " + (_fmt_mat(xf_to_mat(node.transform)) if node else "")
        elif b == "BSTriShape":
            shape = next(shape_iter)
            pr = shape.properties
            info = (f"\"{shape.name}\" {pr.vertexCount} verts / {pr.triangleCount} tris, flags {pr.flags}, "
                    f"vertex attrs {'+'.join(vertex_flags(int(pr.vertexDesc)))}, bound sphere "
                    f"({pr.boundingSphereCenter[0]:.3f}, {pr.boundingSphereCenter[1]:.3f}, "
                    f"{pr.boundingSphereCenter[2]:.3f}) r {pr.boundingSphereRadius:.3f}; transform "
                    + _fmt_mat(xf_to_mat(shape.transform)))
        elif b in ("BSDismemberSkinInstance", "NiSkinInstance"):
            si = raw.skin_instance(i)
            info = f"data #{si['data']}, partition #{si['skin_partition']}, root #{si['skeleton_root']}, bones {si['bones']}"
            if "partitions" in si:
                info += f", partitions {[(p['flags'], p['body_part']) for p in si['partitions']]} (flags, body part)"
        elif b == "NiSkinData":
            sd = raw.skin_data(i)
            info = f"global-to-skin {_fmt_mat(sd['global_to_skin'])}; " + "; ".join(
                f"bone {k}: skin-to-bone {_fmt_mat(bd['skin_to_bone'])}, sphere "
                f"({', '.join(f'{x:.3f}' for x in bd['bound_sphere'])}), {len(bd['weights'])} weights "
                f"(min {min(w for _, w in bd['weights']):.3f})" for k, bd in enumerate(sd["bones"]))
        elif b == "NiSkinPartition":
            info = f"{len(shape.partitions)} partition(s), vertex data copied from the shape (SSE)"
        elif b == "BSLightingShaderProperty":
            sm = shader_summary(shape)
            info = (f"type {sm['shader_type']}, SF1 {sm['flags1_hex']} ({sm['flags1']}), SF2 {sm['flags2_hex']} "
                    f"({sm['flags2']}), gloss {sm['glossiness']}, spec {sm['spec_strength']} {sm['spec_color']}, "
                    f"emissive {sm['emissive_color']} x{sm['emissive_mult']}, env scale {sm['env_map_scale']}, "
                    f"clamp {sm['texture_clamp_mode']}, alpha {sm['alpha']}")
        elif b == "BSShaderTextureSet":
            info = "; ".join(f"[{k}] {t}" for k, t in enumerate(raw.texture_set(i)) if t)
        elif b == "NiAlphaProperty":
            info = "alpha blending/testing property (not used by the mask)"
        elif b == "NiStringExtraData":
            info = "string extra data on the root (HDT-SMP physics XML reference)"
        lines.append(f"| {i} | {b} | {raw.block_sizes[i]} | {info.replace(' | ', ', ')} |")
    return "\n".join(lines)
