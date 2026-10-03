"""Rigid transforms and skin math for Skyrim NIFs (numpy side of PyNifly's TransformBuf).

Conventions (nifly include/Object3d.hpp, MatTransform::ApplyTransform):
    p_parent = translation + rotation @ (scale * p_child)
`TransformBuf.rotation[r][c]` is row r / column c of that matrix, so a buffer converts to a
4x4 column-vector matrix M with M[:3, :3] = scale * rotation and M[:3, 3] = translation.

Skinning (Gamebryo / nifly naming):
    global-to-skin  (NiSkinData overall transform): model space -> skin (vertex) space
    skin-to-bone    (NiSkinData per-bone transform): skin space  -> bone space
    bind            (bone world transform in the skeleton's bind pose): bone space -> model space
A vertex v stored in the shape lands in model space at  sum_i w_i * bind_i @ skin_to_bone_i @ v,
which is what the engine evaluates (with the posed bone transforms instead of bind_i).
"""
from __future__ import annotations

import numpy as np

from .nifenv import NifFile, pynifly  # noqa: F401  (pynifly re-exported for callers)
from pyn.structs import TransformBuf  # noqa: E402

HEAD_BONE = "NPC Head [Head]"
__all__ = ["HEAD_BONE", "NifFile", "TransformBuf", "pynifly", "xf_to_mat", "mat_to_xf", "apply", "apply_dir",
           "rigid_inverse", "rotation_about", "skeleton_binds", "shape_skin", "skinned_model_positions",
           "posed_positions", "bone_consistency"]


def xf_to_mat(buf) -> np.ndarray:
    """TransformBuf -> 4x4 matrix (column vectors)."""
    m = np.eye(4)
    rot = np.array([[buf.rotation[r][c] for c in range(3)] for r in range(3)], dtype=float)
    m[:3, :3] = rot * float(buf.scale)
    m[:3, 3] = [buf.translation[i] for i in range(3)]
    return m


def mat_to_xf(m: np.ndarray) -> TransformBuf:
    """4x4 rigid(+uniform scale) matrix -> TransformBuf."""
    lin = np.asarray(m, dtype=float)[:3, :3]
    scale = float(np.cbrt(np.linalg.det(lin)))
    rot = lin / scale
    buf = TransformBuf()
    buf.store(tuple(float(x) for x in m[:3, 3]), [tuple(float(x) for x in row) for row in rot], [scale] * 3)
    return buf


def apply(m: np.ndarray, pts) -> np.ndarray:
    """Apply a 4x4 matrix to an (N, 3) array of points."""
    p = np.asarray(pts, dtype=float)
    return p @ m[:3, :3].T + m[:3, 3]


def apply_dir(m: np.ndarray, dirs) -> np.ndarray:
    """Rotate (N, 3) directions by the linear part of m and renormalise."""
    d = np.asarray(dirs, dtype=float) @ m[:3, :3].T
    n = np.linalg.norm(d, axis=1, keepdims=True)
    return d / np.where(n > 0, n, 1.0)


def rigid_inverse(m: np.ndarray) -> np.ndarray:
    return np.linalg.inv(m)


def rotation_about(axis, degrees: float) -> np.ndarray:
    """3x3 rotation matrix (Rodrigues) about a unit axis."""
    a = np.asarray(axis, dtype=float)
    a = a / np.linalg.norm(a)
    t = np.radians(degrees)
    k = np.array([[0, -a[2], a[1]], [a[2], 0, -a[0]], [-a[1], a[0], 0]])
    return np.eye(3) + np.sin(t) * k + (1 - np.cos(t)) * (k @ k)


def skeleton_binds(skeleton_path, bones) -> dict:
    """World (model-space) bind matrices of the named bones in a skeleton NIF."""
    skel = NifFile(str(skeleton_path))
    out = {}
    for name in bones:
        if name not in skel.nodes:
            raise KeyError(f"{name} not in {skeleton_path}")
        out[name] = xf_to_mat(skel.nodes[name].global_transform)
    return out


def shape_skin(shape) -> dict:
    """Stored skin data of a shape: global-to-skin, skin-to-bone per bone, weights per bone."""
    return {
        "global_to_skin": xf_to_mat(shape.global_to_skin),
        "has_global_to_skin": bool(shape.has_global_to_skin),
        "skin_to_bone": {b: xf_to_mat(shape.get_shape_skin_to_bone(b)) for b in shape.bone_names},
        "weights": shape.bone_weights,
        "bones": list(shape.bone_names),
    }


def skinned_model_positions(verts, skin: dict, binds: dict) -> np.ndarray:
    """Model-space bind-pose positions: sum_i w_i * bind_i @ skin_to_bone_i @ v (weights renormalised)."""
    v = np.asarray(verts, dtype=float)
    acc = np.zeros_like(v)
    wsum = np.zeros(len(v))
    for bone, pairs in skin["weights"].items():
        if not pairs:
            continue
        idx = np.array([p[0] for p in pairs], dtype=int)
        w = np.array([p[1] for p in pairs], dtype=float)
        m = binds[bone] @ skin["skin_to_bone"][bone]
        acc[idx] += w[:, None] * apply(m, v[idx])
        wsum[idx] += w
    if np.any(wsum <= 0):
        raise ValueError(f"{int(np.sum(wsum <= 0))} vertices carry no bone weight")
    return acc / wsum[:, None]


def posed_positions(verts, skin: dict, binds: dict, pose: dict) -> np.ndarray:
    """Positions with some bones moved: pose maps bone -> 4x4 world delta applied to its bind."""
    moved = {b: pose.get(b, np.eye(4)) @ m for b, m in binds.items()}
    return skinned_model_positions(verts, skin, moved)


def bone_consistency(skin: dict, binds: dict) -> dict:
    """bind_i @ skin_to_bone_i should be the same skin->model matrix for every bone.

    Returns the per-bone matrix and its max abs deviation from inverse(global-to-skin)."""
    g2s_inv = np.linalg.inv(skin["global_to_skin"])
    out = {}
    for bone in skin["bones"]:
        m = binds[bone] @ skin["skin_to_bone"][bone]
        out[bone] = {"skin_to_model": m, "dev_from_inv_g2s": float(np.max(np.abs(m - g2s_inv)))}
    return out
