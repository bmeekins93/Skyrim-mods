"""Neural segmentation helpers for stage 1 (face parsing + foreground matte).

Models (downloaded on first use into the Hugging Face cache, ~/.cache/huggingface; CPU only):
- Face parsing: "jonathandinu/face-parsing" (SegFormer trained on CelebAMask-HQ, 19 classes).
  Label ids are read from the model's own config.id2label; nothing is hard-coded by id.
- Foreground matte: "ZhengPeng7/BiRefNet-matting" (BiRefNet, general/portrait matting),
  loaded through transformers AutoModelForImageSegmentation(trust_remote_code=True).
Both are pinned to exact Hub revisions so the build is reproducible.
"""
from __future__ import annotations

import os

import cv2
import numpy as np

FACE_PARSING_REPO = "jonathandinu/face-parsing"
FACE_PARSING_REV = "758b82e15a0178c9db39c1ff666a8b56e3a550c8"
MATTING_REPO = "ZhengPeng7/BiRefNet-matting"
MATTING_REV = "eccde0a8cbdce7ac5fecfeb06340fe7b949e85d9"

IMAGENET_MEAN = np.array([0.485, 0.456, 0.406])
IMAGENET_STD = np.array([0.229, 0.224, 0.225])

# Semantic groups built from the model's label *names* (CelebAMask-HQ naming).
LABEL_GROUPS = {
    "face": ["skin", "nose", "l_eye", "r_eye", "l_brow", "r_brow", "l_ear", "r_ear",
             "mouth", "u_lip", "l_lip", "ear_r"],
    "glasses": ["eye_g"],
    "hair": ["hair", "hat"],
    "neck": ["neck", "neck_l"],
    "cloth": ["cloth"],
    "background": ["background"],
}
# Finer parts used for landmarks.
PART_GROUPS = {
    "eyes": ["l_eye", "r_eye"],
    "nose": ["nose"],
    "lips": ["mouth", "u_lip", "l_lip"],
    "brows": ["l_brow", "r_brow"],
    "ears": ["l_ear", "r_ear"],
}


def _torch():
    import torch

    torch.manual_seed(0)
    torch.set_num_threads(max(1, min(4, os.cpu_count() or 1)))
    return torch


def _to_tensor(img_u8: np.ndarray, size_wh: tuple[int, int]):
    torch = _torch()
    interp = cv2.INTER_AREA if size_wh[0] < img_u8.shape[1] else cv2.INTER_CUBIC
    x = cv2.resize(img_u8, size_wh, interpolation=interp).astype(np.float64) / 255.0
    x = (x - IMAGENET_MEAN) / IMAGENET_STD
    return torch.from_numpy(x.transpose(2, 0, 1)[None].astype(np.float32))


def _group_indices(id2label: dict, groups: dict) -> dict[str, list[int]]:
    name_to_id = {str(v): int(k) for k, v in id2label.items()}
    out = {}
    for group, names in groups.items():
        out[group] = [name_to_id[n] for n in names if n in name_to_id]
    return out


def face_parsing_probs(img_u8: np.ndarray, width: int = 512) -> dict:
    """Run face parsing on the whole photo (aspect preserved, horizontal-flip TTA).

    Returns {"groups": {name: HxW float32 prob}, "parts": {name: HxW float32 prob},
    "id2label": {...}}. Probabilities are softmax outputs summed per group, averaged
    over the original and the mirrored pass, and resized bilinearly to full resolution.
    """
    torch = _torch()
    from transformers import SegformerForSemanticSegmentation

    model = SegformerForSemanticSegmentation.from_pretrained(
        FACE_PARSING_REPO, revision=FACE_PARSING_REV).eval()
    id2label = {int(k): v for k, v in model.config.id2label.items()}
    h, w = img_u8.shape[:2]
    height = int(round(width * h / w / 32.0)) * 32
    probs = []
    with torch.no_grad():
        for flip in (False, True):
            src = img_u8[:, ::-1] if flip else img_u8
            logits = model(pixel_values=_to_tensor(np.ascontiguousarray(src), (width, height))).logits
            p = torch.softmax(logits, dim=1)[0].numpy()
            probs.append(p[:, :, ::-1] if flip else p)
    p = 0.5 * (probs[0] + probs[1])  # (C, h', w'); left/right classes are merged below

    def resize_sum(ids):
        m = p[ids].sum(axis=0).astype(np.float32)
        return cv2.resize(m, (w, h), interpolation=cv2.INTER_LINEAR)

    groups = {g: resize_sum(ids) for g, ids in _group_indices(id2label, LABEL_GROUPS).items() if ids}
    parts = {g: resize_sum(ids) for g, ids in _group_indices(id2label, PART_GROUPS).items() if ids}
    return {"groups": groups, "parts": parts, "id2label": id2label}


def matting_alpha(img_u8: np.ndarray, box: tuple[int, int, int, int], size: int = 1024) -> np.ndarray:
    """Foreground alpha (HxW float32 in [0,1]) from BiRefNet-matting on the crop `box`.

    The crop (x0, y0, x1, y1) is resized to size x size exactly like the model's
    reference handler does (no letterboxing), with horizontal-flip TTA. Pixels
    outside the crop get alpha 0.
    """
    torch = _torch()
    from transformers import AutoModelForImageSegmentation

    model = AutoModelForImageSegmentation.from_pretrained(
        MATTING_REPO, revision=MATTING_REV, trust_remote_code=True).float().eval()
    x0, y0, x1, y1 = box
    crop = np.ascontiguousarray(img_u8[y0:y1, x0:x1])
    preds = []
    with torch.no_grad():
        for flip in (False, True):
            src = np.ascontiguousarray(crop[:, ::-1]) if flip else crop
            pred = model(_to_tensor(src, (size, size)))[-1].sigmoid()[0, 0].numpy()
            preds.append(pred[:, ::-1] if flip else pred)
    pred = 0.5 * (preds[0] + preds[1])
    alpha = np.zeros(img_u8.shape[:2], np.float32)
    alpha[y0:y1, x0:x1] = cv2.resize(pred.astype(np.float32), (x1 - x0, y1 - y0),
                                     interpolation=cv2.INTER_LINEAR)
    return np.clip(alpha, 0.0, 1.0)
