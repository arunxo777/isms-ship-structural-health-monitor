"""Drive a running ISMS server with synthetic node traffic.

Usage: py -3 tools/feed_local.py [base_url]
Posts a healthy hull, then a rusty + cracked plate, and prints the verdict
after each phase so the fusion and the Gemma note can be inspected.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))
from probe_vision import jpeg, steel_plate, with_crack, with_rust  # noqa: E402

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8011"
CLEAN = jpeg(steel_plate())
FAULTY = jpeg(with_crack(with_rust(steel_plate())))


def payload(i: int) -> dict:
    return {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "zone_id": "ENGINE_BAY",
        "gps": {"lat": 12.9716, "lon": 77.5946, "valid": True},
        "accel": {"x": 0.02, "y": -0.01, "z": 9.81},
        "gyro": {"x": 0.12, "y": 0.05, "z": 0.09},
        "acoustic_rms": 0.03,
        "acoustic_peak_freq": 120.0,
        "accel_rms_30s": 0.06,
        "still": True,
        "imu_calibrated": True,
        "mic_ok": True,
    }


def show(tag: str, c: httpx.Client) -> dict:
    v = c.get(f"{BASE}/latest-verdict").json()
    print(f"\n=== {tag}")
    for k in (
        "status", "fault_type", "camera_status", "rust_detected", "rust_score",
        "break_detected", "crack_score", "health_index", "sustained",
        "anomaly_rate_per_min", "source", "ai_status", "ai_agrees",
    ):
        print(f"  {k:22s} {v.get(k)}")
    print(f"  {'ai_note':22s} {v.get('ai_note') or v.get('reasoning')}")
    return v


with httpx.Client(timeout=30.0) as c:
    print("phase 1: healthy hull, clean plate")
    for i in range(30):
        c.post(f"{BASE}/sensor-data", json=payload(i))
        if i % 5 == 0:
            c.post(f"{BASE}/camera-frame", content=CLEAN,
                   headers={"Content-Type": "image/jpeg"})
        time.sleep(0.05)
    show("healthy", c)

    print("\nphase 2: rusty + cracked plate")
    for i in range(12):
        c.post(f"{BASE}/camera-frame", content=FAULTY,
               headers={"Content-Type": "image/jpeg"})
        c.post(f"{BASE}/sensor-data", json=payload(i))
        time.sleep(0.1)
    show("fault detected (pre-Gemma)", c)

    print("\nwaiting up to 180 s for the Gemma note ...")
    deadline = time.time() + 180
    while time.time() < deadline:
        v = c.get(f"{BASE}/latest-verdict").json()
        if v.get("source") == "gemma4":
            break
        c.post(f"{BASE}/sensor-data", json=payload(0))
        time.sleep(2.0)
    show("after Gemma", c)
