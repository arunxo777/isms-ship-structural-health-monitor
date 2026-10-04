"""
Train Isolation Forest on pose-invariant hull features (not raw accel xyz).

Columns:
    grav_res, gyro_n, acoustic_rms, peak_n, accel_rms_30s,
    crack_score, cavitation, still
"""

from __future__ import annotations

import argparse
from pathlib import Path

import joblib
import numpy as np
from sklearn.ensemble import IsolationForest

import config


def synthesise_training_set(n_normal: int = 9000, n_fault: int = 500, seed: int = 42) -> np.ndarray:
    rng = np.random.default_rng(seed)
    grav = np.abs(rng.normal(0.02, 0.02, n_normal)).clip(0, 0.08)
    gyro_n = rng.beta(1.2, 8.0, n_normal) * 0.15
    acoust = rng.normal(0.04, 0.015, n_normal).clip(0.005, 0.12)
    peak_n = rng.uniform(0.02, 0.12, n_normal)
    rms = rng.normal(0.04, 0.015, n_normal).clip(0.0, 0.12)
    crack = rng.beta(1.2, 10.0, n_normal) * 0.2
    cav = np.zeros(n_normal)
    still = np.ones(n_normal)
    normal = np.column_stack([grav, gyro_n, acoust, peak_n, rms, crack, cav, still])

    f_grav = rng.uniform(0.15, 0.6, n_fault)
    f_gyro = rng.uniform(0.25, 1.0, n_fault)
    f_ac = rng.uniform(0.22, 1.1, n_fault)
    f_pk = rng.uniform(0.05, 0.25, n_fault)
    f_rms = rng.uniform(0.25, 1.4, n_fault)
    f_cr = rng.uniform(0.4, 0.98, n_fault)
    f_cav = rng.integers(0, 2, n_fault).astype(np.float64)
    f_still = rng.integers(0, 2, n_fault).astype(np.float64)
    fault = np.column_stack([f_grav, f_gyro, f_ac, f_pk, f_rms, f_cr, f_cav, f_still])
    return np.vstack([normal, fault]).astype(np.float64)


def train(path: Path) -> None:
    x = synthesise_training_set()
    model = IsolationForest(
        n_estimators=240,
        contamination=0.05,
        max_samples=512,
        random_state=42,
        n_jobs=-1,
    )
    model.fit(x)
    path.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump(model, path)
    probe = np.array([[0.02, 0.02, 0.04, 0.06, 0.04, 0.05, 0.0, 1.0]], dtype=np.float64)
    print(f"Wrote {path}")
    print(f"Sanity probe label={int(model.predict(probe)[0])} score={float(model.decision_function(probe)[0]):.4f}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("-o", "--output", default=str(config.ANOMALY_MODEL_PATH))
    args = parser.parse_args()
    train(Path(args.output))
