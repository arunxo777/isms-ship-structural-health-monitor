"""
Train an INT8 linear crack classifier on synthetic hull-plate images.

Healthy plates: grain + rivet dots.
Cracked plates: dark elongated polylines (fatigue cracks).

Weights are stored as int8 so Pi inference is quantized (no cloud, no GPU).
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
from sklearn.linear_model import LogisticRegression

from crack_detect import INPUT_SIZE, MODEL_PATH, _features


def _plate(rng: np.random.Generator, cracked: bool) -> np.ndarray:
    img = rng.normal(150, 18, (INPUT_SIZE, INPUT_SIZE)).clip(40, 220)
    # Rivets / pitting (not cracks)
    for _ in range(int(rng.integers(4, 12))):
        cy, cx = rng.integers(4, INPUT_SIZE - 4, size=2)
        rr = rng.integers(1, 3)
        y, x = np.ogrid[-cy : INPUT_SIZE - cy, -cx : INPUT_SIZE - cx]
        img[y * y + x * x <= rr * rr] *= 0.55
    if cracked:
        n_strokes = int(rng.integers(1, 3))
        for _ in range(n_strokes):
            y0 = int(rng.integers(5, INPUT_SIZE - 5))
            x0 = int(rng.integers(5, INPUT_SIZE - 5))
            length = int(rng.integers(28, 70))
            ang = float(rng.uniform(-0.6, 0.6) + rng.choice([0.0, np.pi / 2]))
            dy, dx = np.sin(ang), np.cos(ang)
            for t in range(length):
                yy = int(y0 + dy * t + rng.normal(0, 0.4))
                xx = int(x0 + dx * t + rng.normal(0, 0.4))
                if 1 <= yy < INPUT_SIZE - 1 and 1 <= xx < INPUT_SIZE - 1:
                    img[yy - 1 : yy + 2, xx - 1 : xx + 2] *= rng.uniform(0.12, 0.28)
    return img.astype(np.float32)


def train(path: Path = MODEL_PATH, n: int = 500, seed: int = 7) -> None:
    rng = np.random.default_rng(seed)
    xs = []
    ys = []
    for i in range(n):
        cracked = i % 2 == 1
        feat = _features(_plate(rng, cracked))
        xs.append(feat)
        ys.append(1 if cracked else 0)
    x = np.vstack(xs)
    y = np.array(ys)
    clf = LogisticRegression(max_iter=400, class_weight="balanced")
    clf.fit(x, y)

    # Quantize weights: logit ≈ (w_int8 · x_u8) * scale + bias
    # Training features are 0–1; inference uses x_u8 = round(feat*255)
    w = clf.coef_[0].astype(np.float64) / 255.0
    b = float(clf.intercept_[0])
    max_abs = max(np.max(np.abs(w)), 1e-9)
    scale = max_abs / 127.0
    w_q = np.clip(np.round(w / scale), -127, 127).astype(np.int8)

    path.parent.mkdir(parents=True, exist_ok=True)
    np.savez(path, weights=w_q, bias=np.array(b), scale=np.array(scale), threshold=np.array(0.45))

    acc = clf.score(x, y)
    print(f"Wrote {path}  sklearn acc={acc:.3f}  int8 weights={w_q.tolist()}")


if __name__ == "__main__":
    train(Path(MODEL_PATH))
