"""Smoke tests for the render harness (src/render_check.py drives src/cfm/blender_render.py).

The numpy-only tests check the NIF -> model space and pose math in the venv (no Blender):
vanilla helmet on the vanilla head, an unskinned shape under a transformed parent node, raw
(un-normalised) skin weights, and the report's problem list. `test_tiny_render` runs one tiny
Cycles render of the vanilla helmet on the vanilla head; it is skipped when Blender is missing.
Run: .venv/bin/python -m pytest tests/test_render_smoke.py -q
"""
import json
import math
import os
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from cfm import blender_render as br  # noqa: E402  (numpy-only part; bpy is imported lazily)
import render_check as rc  # noqa: E402
from pyn import pynifly  # noqa: E402  (on sys.path through blender_render)
from pyn.structs import TransformBuf  # noqa: E402

HELMET = ROOT / "third_party" / "PyNifly" / "tests" / "tests" / "SkyrimSE" / "helmet-SMP.nif"
QUAD = [(-1.0, 0.0, -1.0), (1.0, 0.0, -1.0), (1.0, 0.0, 1.0), (-1.0, 0.0, 1.0)]   # 2x2 in XZ
QUAD_TRIS = [(0, 1, 2), (0, 2, 3)]


def _blender():
    env = os.environ.get("BLENDER")
    path = Path(env) if env else ROOT / "third_party" / "blender" / "blender"
    return path if path.exists() else None


def _xf(translation, rot_z_deg=0.0, rot_x_deg=0.0, scale=1.0):
    """TransformBuf for T * Rz * Rx * s, plus the same as a 4x4 matrix."""
    z, x = math.radians(rot_z_deg), math.radians(rot_x_deg)
    rz = np.array([[math.cos(z), -math.sin(z), 0], [math.sin(z), math.cos(z), 0], [0, 0, 1]])
    rx = np.array([[1, 0, 0], [0, math.cos(x), -math.sin(x)], [0, math.sin(x), math.cos(x)]])
    rot = rz @ rx
    buf = TransformBuf()
    buf.store(tuple(translation), [tuple(r) for r in rot], [scale] * 3)
    m = np.eye(4)
    m[:3, :3], m[:3, 3] = rot * scale, translation
    return buf, m


def _new_nif(path):
    nif = pynifly.NifFile()
    nif.initialize("SKYRIMSE", str(path), "NiNode", "Scene Root")
    return nif


def _quad(nif, name, parent, verts=QUAD):
    """BSTriShape quad; its winding faces -Y, so the stored normals do too."""
    props = pynifly.NiObject.block_types["BSTriShape"].getbuf()
    return nif.createShapeFromData(name, [tuple(map(float, v)) for v in verts], QUAD_TRIS,
                                   [(0, 1), (1, 1), (1, 0), (0, 0)], [(0.0, -1.0, 0.0)] * 4,
                                   props=props, parent=parent)


@pytest.fixture(scope="module")
def skel():
    return br.load_skeleton(br.SKELETON_FILES["human"])


def test_model_space_math(skel):
    helmet = br.load_nif_meshes(HELMET, skel, "nif")
    head = br.load_nif_meshes(br.HEADS["human"][0], skel, "head")[0]
    for m in helmet:
        assert m.report["bone_bind_disagreement"] < 1e-3
        assert m.report["head_share_range"] == pytest.approx([1.0, 1.0])
        assert m.report["weight_sum_range"] == pytest.approx([1.0, 1.0], abs=br.WEIGHT_TOL)
    # The helmet shell encloses the vanilla head sideways and above the crown.
    shell = next(m for m in helmet if m.name == "Helmet").verts
    assert shell[:, 0].min() < head.verts[:, 0].min() and shell[:, 0].max() > head.verts[:, 0].max()
    assert shell[:, 2].max() > head.verts[:, 2].max()
    # Shader state the render reproduces / cannot show (HelmetTail: alpha test + vertex colours).
    tail = next(m for m in helmet if m.name == "HelmetTail")
    assert "alpha test > 200/255" in tail.shader["applied"] and tail.colors is not None
    assert any("environment map" in f for f in tail.shader["not_emulated"])

    # Winding number (sign of the clearance check): ~1 inside the open-necked head, ~0 far away.
    centre = 0.5 * (head.verts.min(0) + head.verts.max(0))
    w = br.winding_numbers(np.array([centre, centre + [0, 0, 50]]), head.verts, head.tris)
    assert abs(w[0]) > 0.8 and abs(w[1]) < 0.05

    bind = skel.world[br.HEAD_BONE]
    assert np.allclose(br.pose_delta(bind, 0, 0, 0), np.eye(4))
    turn = br.pose_delta(bind, 90, 0, 0)
    assert np.allclose(br.apply(turn, bind[None, :3, 3]), bind[None, :3, 3])   # pivot fixed
    nose = br.apply(turn, (bind[:3, 3] + [0, 10, 0])[None])[0]
    assert nose[0] == pytest.approx(bind[0, 3] - 10, abs=1e-6)                 # +yaw -> actor's left

    before = head.verts.copy()
    br.apply_pose(head, br.pose_delta(bind, 35, 15, 0))
    still = head.head_weight <= 1e-9
    assert still.any() and np.allclose(head.verts[still], before[still])      # Spine2-only verts
    assert np.abs(head.verts[~still] - before[~still]).max() > 1.0


def test_unskinned_shape_uses_node_chain(tmp_path, skel):
    """Unskinned shapes: v_model = T_root * T_parent * T_shape * v (nifly's global transform
    returns identity for shapes, so the chain must be composed explicitly)."""
    path = tmp_path / "unskinned.nif"
    nif = _new_nif(path)
    holder_xf, holder_m = _xf((1.0, 2.0, 100.0), rot_x_deg=30.0, scale=1.5)
    holder = nif.add_node("Holder", holder_xf, parent=nif.root)
    shape_xf, shape_m = _xf((0.0, 5.0, 10.0), rot_z_deg=90.0, scale=2.0)
    shape = _quad(nif, "Quad", holder)
    shape.transform = shape_xf
    nif.save()

    mesh = br.load_nif_meshes(path, skel, "nif")[0]
    expected = br.apply(holder_m @ shape_m, np.array(QUAD))
    assert mesh.report["node_chain"] == ["Scene Root", "Holder", "Quad"]
    assert np.allclose(mesh.verts, expected, atol=1e-4)
    assert np.ptp(mesh.verts[:, 2]) > 3.0            # scaled 2 x 1.5 = 6 units tall, not 2


def test_raw_weights_are_not_normalised(tmp_path, skel):
    """A mesh weighted 0.5 to the head is drawn where linear-blend skinning puts it (half way
    to the origin), not renormalised onto the face, and the report shows the raw sum."""
    path = tmp_path / "halfweight.nif"
    nif = _new_nif(path)
    bind = skel.world[br.HEAD_BONE]
    stored = np.array(QUAD) * 3 + [0.0, 12.0, 123.0]      # skin space = model space here
    shape = _quad(nif, "Half", nif.root, stored)
    shape.skin()
    shape.set_global_to_skin(_matrix_buf(np.eye(4)))
    shape.add_bone(br.HEAD_BONE, _matrix_buf(bind))
    shape.set_skin_to_bone_xform(br.HEAD_BONE, _matrix_buf(np.linalg.inv(bind)))
    shape.setShapeWeights(br.HEAD_BONE, [(i, 0.5) for i in range(len(stored))])
    nif.save()

    mesh = br.load_nif_meshes(path, skel, "nif")[0]
    assert mesh.report["weight_sum_range"] == pytest.approx([0.5, 0.5], abs=1e-3)
    assert mesh.report["head_share_range"] == pytest.approx([1.0, 1.0])
    assert np.allclose(mesh.verts, 0.5 * stored, atol=1e-2)       # W*S = identity, w = 0.5
    # The pose moves exactly the weighted part: v' = v - v_h + R v_h + h t = 0.5 * D v.
    delta = br.pose_delta(bind, 30, 0, 0)
    br.apply_pose(mesh, delta)
    assert np.allclose(mesh.verts, 0.5 * br.apply(delta, stored), atol=1e-2)


def _matrix_buf(m):
    """4x4 rigid matrix -> TransformBuf."""
    buf = TransformBuf()
    buf.store(tuple(float(x) for x in m[:3, 3]), [tuple(float(x) for x in r) for r in m[:3, :3]], [1.0] * 3)
    return buf


def test_problem_list_and_strict_levels():
    """Red report lines become problems (what --strict fails on); notes do not."""
    mesh = {"name": "M", "role": "nif", "source": "work/nif/x.nif", "block": "BSTriShape",
            "verts": 4, "tris": 2, "winding_agreement": 1.0, "skinned": True,
            "bones": {br.HEAD_BONE: {"head_driven": True}}, "weight_sum_range": [1.0, 1.0],
            "head_share_range": [1.0, 1.0], "unweighted_verts": 0,
            "inverse_global_to_skin_max_dev": 0.0, "material": {},
            "shader": {"applied": ["vertex colours (4)"], "not_emulated": [], "glossiness": 30,
                       "spec_strength": 0.6, "tangents_stored": True}}
    rep = {"meshes": [mesh], "clearance_to_head": {}}
    assert rc.problems(rep) == []
    assert rc.report_lines(rep)[0] == ("Checks: all passed", "pass")
    assert any(lvl == "note" for _, lvl in rc.report_lines(rep))
    bad = dict(mesh, weight_sum_range=[0.5, 0.5], shader=dict(mesh["shader"], not_emulated=["glow map"]))
    found = rc.problems({"meshes": [bad], "clearance_to_head": {}})
    assert len(found) == 2 and any("sum 0.500" in f for f in found)
    unskinned = {k: v for k, v in mesh.items() if k not in ("bones", "weight_sum_range")}
    unskinned.update(skinned=False, node_chain=["Scene Root", "Q"])
    assert rc.problems({"meshes": [unskinned], "clearance_to_head": {}})
    assert not rc.problems({"meshes": [dict(unskinned, source="work/nif/mask_go.nif")],
                            "clearance_to_head": {}})


@pytest.mark.skipif(_blender() is None, reason="Blender not installed (tools/setup.sh)")
def test_tiny_render(tmp_path):
    out, views = tmp_path / "sheet.png", tmp_path / "views"
    cmd = [sys.executable, "src/render_check.py", "--nif", str(HELMET), "--head", "human",
           "--views", "front", "--res", "96", "--samples", "4", "--out", str(out),
           "--keep-views", str(views)]
    proc = subprocess.run(cmd, cwd=ROOT, check=True, timeout=600, capture_output=True, text=True)
    assert out.stat().st_size > 1000
    sheet = Image.open(out)
    assert sheet.width > 96 and sheet.height > 96
    front = np.asarray(Image.open(views / "front.png").convert("RGB"), float)
    assert front.shape == (96, 96, 3)
    assert front.std() > 5                                     # not a blank frame
    background = front[:6, :6].reshape(-1, 3).mean(0)
    covered = (np.abs(front - background).max(2) > 12).mean()
    assert 0.2 < covered < 0.95                                # helmet + head fill the frame
    rep = json.loads((views / "report.json").read_text(encoding="utf-8"))
    assert [v["name"] for v in rep["views"]] == ["front"]
    assert {m["name"] for m in rep["meshes"]} >= {"Helmet", "HelmetVisor", "MaleHeadIMF"}
    # The full report reaches stdout, red lines marked, even when the tiny panel cuts lines.
    assert "!!   clearance: Helmet: min" in proc.stdout
