"""Measure per-ridge context on real clutter vs a synthetic crack.

A crack lies on plain metal, so its immediate surroundings are smooth. A
cable or keyboard gap lies against clutter. This prints, for every ridge the
break stage currently accepts, the local background roughness around it.
"""

from __future__ import annotations

import io
import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageFilter

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import crack_detect as cd  # noqa: E402
from probe_vision import jpeg, steel_plate, with_crack, with_rust  # noqa: E402


def ridges(blob: bytes):
    im = Image.open(io.BytesIO(blob)).convert("RGB").resize((cd.WORK_W, cd.WORK_H), Image.BILINEAR)
    gray = np.asarray(im.convert("L"), np.float32)
    q = cd.assess_quality(gray)
    if not q.usable:
        return q.status, []

    img = Image.fromarray(gray.astype(np.uint8), "L")
    sm = img.filter(ImageFilter.GaussianBlur(0.6))
    closed = sm.filter(ImageFilter.MaxFilter(5)).filter(ImageFilter.MinFilter(5))
    g = np.asarray(sm, np.float32)
    bh = np.clip(np.asarray(closed, np.float32) - g, 0, 255)
    lm, _ = cd._box_stats(gray, 4)
    mask = (bh > max(8.0, float(bh.mean() + 3.0 * bh.std()))) & (g < lm - 6.0)
    if not mask.any():
        return q.status, []

    labels, n = cd._label(mask)
    areas = np.bincount(labels.ravel())
    order = np.argsort(areas[1:])[::-1] + 1
    out = []
    for lab in order[:cd.BREAK_MAX_COMPONENTS]:
        area = int(areas[lab])
        if area < cd.BREAK_AREA_MIN:
            break
        ys, xs = np.nonzero(labels == lab)
        major, minor = cd._component_axes(ys, xs)
        elong = major / minor
        width = area / major
        contrast = float(bh[ys, xs].mean())
        if major < cd.BREAK_LEN_MIN or elong < cd.BREAK_ELONG_MIN:
            continue
        if width > cd.BREAK_WIDTH_MAX or contrast < cd.BREAK_CONTRAST_MIN:
            continue

        # roughness of the surface around the ridge, excluding the ridge
        y0, y1 = max(ys.min() - 7, 0), min(ys.max() + 8, cd.WORK_H)
        x0, x1 = max(xs.min() - 7, 0), min(xs.max() + 8, cd.WORK_W)
        box = gray[y0:y1, x0:x1]
        holes = mask[y0:y1, x0:x1]
        bg = box[~holes]
        rough = float(bg.std()) if bg.size > 20 else 99.0
        out.append({"len": major, "elong": elong, "w": width, "ctr": contrast, "rough": rough})
    return q.status, out


cases = [("SCENE", p.name, p.read_bytes()) for p in sorted(Path("data/frames").glob("*.jpg"))]
cases.append(("SCENE", "real_frame.jpg", Path("data/real_frame.jpg").read_bytes()))
plate = steel_plate()
cases.append(("PLATE-clean", "synthetic", jpeg(plate)))
cases.append(("PLATE-crack", "synthetic", jpeg(with_crack(plate))))
cases.append(("PLATE-both", "synthetic", jpeg(with_crack(with_rust(plate)))))

for cls, name, blob in cases:
    status, rs = ridges(blob)
    print(f"\n{cls:12s} {name:18s} status={status} accepted={len(rs)}")
    for r in sorted(rs, key=lambda d: -d["len"])[:6]:
        print(f"    len {r['len']:6.1f}  elong {r['elong']:6.1f}  w {r['w']:4.1f}  "
              f"contrast {r['ctr']:5.1f}  bg_rough {r['rough']:5.1f}")
    if rs:
        print(f"    -> min bg_rough {min(r['rough'] for r in rs):.1f}"
              f"  max len {max(r['len'] for r in rs):.0f}")
