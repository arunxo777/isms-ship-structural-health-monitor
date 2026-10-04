"""Explain the detector's decision on a real camera frame.

    py -3 tools/diag_frame.py data/real_frame.jpg

Writes <name>_diag.png showing the rust mask and the accepted break ridges
next to the original, so a decision can be checked by eye.
"""

from __future__ import annotations

import io
import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import crack_detect as cd  # noqa: E402

path = Path(sys.argv[1] if len(sys.argv) > 1 else "data/real_frame.jpg")
jpeg = path.read_bytes()

im = Image.open(io.BytesIO(jpeg)).convert("RGB").resize((cd.WORK_W, cd.WORK_H), Image.BILINEAR)
rgb = np.asarray(im, np.uint8)
gray = np.asarray(im.convert("L"), np.float32)

q = cd.assess_quality(gray)
print("quality:", q.as_dict())

hsv = np.asarray(Image.fromarray(rgb, "RGB").convert("HSV"), np.float32)
hue = hsv[:, :, 0] * (360.0 / 256.0)
sat = hsv[:, :, 1]
val = hsv[:, :, 2]
_, grain = cd._box_stats(sat, 3)

colour = (
    (hue >= cd.RUST_HUE_LO) & (hue <= cd.RUST_HUE_HI)
    & (sat >= cd.RUST_SAT_LO) & (sat <= cd.RUST_SAT_HI)
    & (val >= cd.RUST_VAL_LO) & (val <= cd.RUST_VAL_HI)
)
mask = colour & (grain >= cd.RUST_GRAIN_MIN)
steel = float((sat < cd.RUST_SAT_LO).mean())

print("\nrust gates")
print(f"  hue in band      {((hue >= cd.RUST_HUE_LO) & (hue <= cd.RUST_HUE_HI)).mean() * 100:6.2f}%")
print(f"  sat in band      {((sat >= cd.RUST_SAT_LO) & (sat <= cd.RUST_SAT_HI)).mean() * 100:6.2f}%")
print(f"  val in band      {((val >= cd.RUST_VAL_LO) & (val <= cd.RUST_VAL_HI)).mean() * 100:6.2f}%")
print(f"  colour combined  {colour.mean() * 100:6.2f}%")
print(f"  + grain>={cd.RUST_GRAIN_MIN}     {mask.mean() * 100:6.2f}%   (needs >= {cd.config.RUST_WARN * 100:.0f}%)")
print(f"  grain in mask    {float(grain[mask].mean()) if mask.any() else 0:6.2f}")
print(f"  steel_frac       {steel * 100:6.2f}%   (needs >= {cd.RUST_STEEL_MIN * 100:.0f}%)")
print("  sat percentiles  " + " ".join(f"p{p}={np.percentile(sat, p):.0f}" for p in (5, 25, 50, 75, 95)))
print("  hue percentiles  " + " ".join(f"p{p}={np.percentile(hue, p):.0f}" for p in (5, 25, 50, 75, 95)))

r = cd.analyse_frame(jpeg)
print("\nverdict:", {k: r[k] for k in ("status", "rust_score", "rust_detected", "crack_score", "crack_detected", "crack_count")})
print("  rust why :", (r.get("rust_detail") or {}).get("why"))
print("  break why:", (r.get("break_detail") or {}).get("why"))

# ---- visual: original | rust mask | break ridges ----
brk = cd._break_evidence(gray)
img = Image.fromarray(gray.astype(np.uint8), "L")
smooth = img.filter(cd.ImageFilter.GaussianBlur(0.6))
closed = smooth.filter(cd.ImageFilter.MaxFilter(5)).filter(cd.ImageFilter.MinFilter(5))
g = np.asarray(smooth, np.float32)
bh = np.clip(np.asarray(closed, np.float32) - g, 0, 255)
lm, _ = cd._box_stats(gray, 4)
bmask = (bh > max(8.0, float(bh.mean() + 3.0 * bh.std()))) & (g < lm - 6.0)

panel = Image.new("RGB", (cd.WORK_W * 3 + 20, cd.WORK_H + 24), (10, 20, 34))
panel.paste(im, (0, 24))
ov = np.asarray(im, np.uint8).copy()
ov[mask] = [255, 90, 0]
panel.paste(Image.fromarray(ov), (cd.WORK_W + 10, 24))
ov2 = np.asarray(im, np.uint8).copy()
ov2[bmask] = [0, 255, 255]
panel.paste(Image.fromarray(ov2), (cd.WORK_W * 2 + 20, 24))
d = ImageDraw.Draw(panel)
d.text((4, 6), "original", fill=(200, 220, 240))
d.text((cd.WORK_W + 14, 6), f"rust mask {mask.mean() * 100:.1f}%", fill=(255, 140, 60))
d.text((cd.WORK_W * 2 + 24, 6), f"break ridges ({brk['count']} accepted)", fill=(80, 230, 240))
out = path.with_name(path.stem + "_diag.png")
panel.save(out)
print("\nwrote", out)
