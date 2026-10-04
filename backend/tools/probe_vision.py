"""Control-image probe for the hull vision detector.

Run: py -3 tools/probe_vision.py
Every case prints what the detector claims. Cases marked CLEAN must not
report rust or a break; cases marked RUST/CRACK must.
"""

from __future__ import annotations

import io
import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import crack_detect  # noqa: E402

H, W = 240, 320
rng = np.random.default_rng(7)


def jpeg(arr: np.ndarray) -> bytes:
    buf = io.BytesIO()
    Image.fromarray(arr).save(buf, format="JPEG", quality=88)
    return buf.getvalue()


def steel_plate() -> np.ndarray:
    """Grey steel with mild rolling texture and a lighting gradient."""
    base = 128 + rng.normal(0, 7, (H, W))
    grad = np.linspace(-18, 18, W)[None, :]
    plate = np.clip(base + grad, 0, 255)
    return np.dstack([plate] * 3).astype(np.uint8)


def with_crack(plate: np.ndarray) -> np.ndarray:
    im = Image.fromarray(plate)
    d = ImageDraw.Draw(im)
    pts = [(40, 60), (110, 96), (180, 120), (250, 175)]
    d.line(pts, fill=(28, 28, 30), width=2, joint="curve")
    return np.asarray(im, dtype=np.uint8)


def with_rust(plate: np.ndarray) -> np.ndarray:
    """Mottled orange-brown patches, like real corrosion blooms."""
    out = plate.astype(np.float32)
    yy, xx = np.mgrid[0:H, 0:W]
    mask = np.zeros((H, W), np.float32)
    for cx, cy, r in ((90, 80, 46), (200, 150, 54), (140, 190, 30)):
        d = np.sqrt((xx - cx) ** 2 + (yy - cy) ** 2) / r
        mask = np.maximum(mask, np.clip(1.3 - d, 0, 1))
    mask *= 0.55 + 0.45 * rng.random((H, W))  # granular, not flat
    rust_rgb = np.dstack([
        np.full((H, W), 150.0),
        np.full((H, W), 74.0),
        np.full((H, W), 32.0),
    ])
    m3 = np.dstack([mask] * 3)
    return np.clip(out * (1 - m3) + rust_rgb * m3, 0, 255).astype(np.uint8)


CASES = [
    ("CLEAN", "flat grey card", np.full((H, W, 3), 128, np.uint8)),
    ("CLEAN", "clean steel plate", steel_plate()),
    ("CLEAN", "fine sensor noise", rng.integers(96, 148, (H, W, 3)).astype(np.uint8)),
    ("CLEAN", "black / lens covered", np.full((H, W, 3), 6, np.uint8)),
    ("CLEAN", "blown out white", np.full((H, W, 3), 250, np.uint8)),
    ("CLEAN", "flat orange plastic", np.dstack([
        np.full((H, W), 235, np.uint8),
        np.full((H, W), 120, np.uint8),
        np.full((H, W), 20, np.uint8),
    ])),
    ("CLEAN", "wooden desk", np.dstack([
        np.clip(168 + rng.normal(0, 6, (H, W)), 0, 255).astype(np.uint8),
        np.clip(120 + rng.normal(0, 5, (H, W)), 0, 255).astype(np.uint8),
        np.clip(78 + rng.normal(0, 4, (H, W)), 0, 255).astype(np.uint8),
    ])),
    ("RUST", "corroded plate", with_rust(steel_plate())),
    ("CRACK", "cracked plate", with_crack(steel_plate())),
    ("BOTH", "rusty + cracked", with_crack(with_rust(steel_plate()))),
]


def main() -> int:
    print(f"{'expect':7s} {'case':22s} {'status':11s} eng    rust  break  q")
    print("-" * 78)
    bad = 0
    for expect, name, arr in CASES:
        r = crack_detect.detect_hull(jpeg(arr), save_frame=False, confirm=False)
        rust = bool(r.get("rust_detected"))
        brk = bool(r.get("break_detected"))
        got = ("RUST" if rust else "") + ("CRACK" if brk else "")
        got = got or "CLEAN"
        if expect == "BOTH":
            ok = rust and brk
        elif expect == "RUST":
            ok = rust and not brk
        elif expect == "CRACK":
            ok = brk and not rust
        else:
            ok = not rust and not brk
        if not ok:
            bad += 1
        print(
            f"{expect:7s} {name:22s} {str(r.get('status')):11s} "
            f"{str(r.get('vision_engine', '?')):6s} "
            f"{r.get('rust_score', 0):.2f}  {r.get('crack_score', 0):.2f}   "
            f"{r.get('quality', 0):.2f}  {'ok' if ok else 'FAIL -> ' + got}"
        )
    print("-" * 78)
    print(f"{len(CASES) - bad}/{len(CASES)} correct  (cv2={crack_detect._HAS_CV2})")
    # Write one annotated sample so judges can see the overlay.
    sample = jpeg(with_crack(with_rust(steel_plate())))
    out = Path(__file__).resolve().parent.parent / "data" / "overlay_demo.jpg"
    r = crack_detect.analyse_frame(sample)
    if r.get("overlay_jpeg"):
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(r["overlay_jpeg"])
        print(f"wrote overlay demo -> {out}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
