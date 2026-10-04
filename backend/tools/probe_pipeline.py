"""End-to-end check of camera -> vision -> fusion -> verdict.

Feeds the engine a healthy hull, then one stray cracked frame, then a
sustained crack, then recovery. Verifies the CIC does not escalate on a
single frame and does escalate on a confirmed fault.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import crack_detect  # noqa: E402
from anomaly import AnomalyEngine, reading_from_payload  # noqa: E402
from probe_vision import jpeg, steel_plate, with_crack, with_rust  # noqa: E402

CLEAN = jpeg(steel_plate())
CRACKED = jpeg(with_crack(steel_plate()))
RUSTY = jpeg(with_rust(steel_plate()))

engine = AnomalyEngine()


def healthy_payload(i: int) -> dict:
    return {
        "timestamp": f"2026-09-09T12:00:{i % 60:02d}",
        "zone_id": "ENGINE_BAY",
        "gps": {"lat": 12.9, "lon": 77.6, "valid": True},
        "accel": {"x": 0.02, "y": -0.01, "z": 9.81},
        "gyro": {"x": 0.1, "y": 0.05, "z": 0.08},
        "acoustic_rms": 0.03,
        "acoustic_peak_freq": 120.0,
        "accel_rms_30s": 0.06,
        "still": True,
        "imu_calibrated": True,
        "mic_ok": True,
    }


def step(i: int, frame: bytes | None) -> dict:
    if frame is not None:
        engine.update_camera(crack_detect.detect_hull(frame, save_frame=False))
    engine.ingest(reading_from_payload(healthy_payload(i)))
    engine.compute_trends()
    return engine.last_verdict


def phase(name: str, frames: list, start: int) -> tuple[int, list]:
    statuses = []
    i = start
    for f in frames:
        v = step(i, f)
        statuses.append(v["status"])
        i += 1
    cam = engine.latest_camera
    print(
        f"{name:28s} status={statuses[-1]:8s} rust={cam['rust_detected']!s:5s} "
        f"break={cam['break_detected']!s:5s} rate={engine.trend.anomaly_rate_per_min:.2f}/min"
    )
    return i, statuses


fails = []
i = 0

i, s = phase("60x healthy + clean plate", [CLEAN] * 60, i)
if set(s[10:]) != {"NORMAL"}:
    fails.append(f"healthy hull did not stay NORMAL: {sorted(set(s))}")
if engine.trend.anomaly_rate_per_min != 0.0:
    fails.append(f"healthy anomaly rate should be 0, got {engine.trend.anomaly_rate_per_min}")

i, s = phase("1x stray cracked frame", [CRACKED], i)
if s[-1] != "NORMAL":
    fails.append(f"single frame escalated to {s[-1]} — confirmation not working")

i, s = phase("2 more clean frames", [CLEAN] * 2, i)
if s[-1] != "NORMAL":
    fails.append("recovered frames should stay NORMAL")

i, s = phase("sustained crack (8 frames)", [CRACKED] * 8, i)
if s[-1] == "NORMAL":
    fails.append("confirmed crack did not escalate")
if not engine.latest_camera["break_detected"]:
    fails.append("confirmed crack not reported by camera")

i, s = phase("sustained rust (8 frames)", [RUSTY] * 8, i)
if not engine.latest_camera["rust_detected"]:
    fails.append("confirmed rust not reported by camera")

i, s = phase("recovery (8 clean frames)", [CLEAN] * 8, i)
if engine.latest_camera["break_detected"] or engine.latest_camera["rust_detected"]:
    fails.append("faults did not clear after recovery")
if s[-1] != "NORMAL":
    fails.append(f"did not stand down after recovery: {s[-1]}")

print("-" * 78)
if fails:
    for f in fails:
        print("FAIL:", f)
    sys.exit(1)
print("pipeline ok — no false escalation, confirmed faults escalate, faults clear")
