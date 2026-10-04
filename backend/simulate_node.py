"""
Optional laptop/Pi simulator: posts realistic ISMS frames to FastAPI
so the dashboard and Ollama path can be demoed without the ESP32.
"""

from __future__ import annotations

import argparse
import math
import random
import time
from datetime import datetime, timezone

import requests

import config


def iso_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def frame(t: float, mode: str) -> dict:
    # Slow 0.4 Hz hull vibration plus optional injected faults.
    vib = 0.08 * math.sin(2 * math.pi * 0.4 * t)
    acoustic = 0.28 + 0.04 * math.sin(2 * math.pi * 0.15 * t)
    crack = 0.08 + 0.02 * math.sin(2 * math.pi * 0.01 * t)
    peak = 180.0 + 15.0 * math.sin(2 * math.pi * 0.01 * t)

    if mode == "bearing":
        vib += 0.002 * t
        acoustic += min(0.6, 0.0015 * t)
        peak = 340.0 + 20.0 * math.sin(t / 8.0)
    elif mode == "cavitation":
        acoustic = 0.9 + 0.05 * random.random()
        peak = 420.0
    elif mode == "crack":
        crack = min(0.95, 0.35 + 0.01 * t)
        vib += 0.15

    ax = 0.12 + vib + random.gauss(0, 0.01)
    ay = 0.03 + 0.3 * vib + random.gauss(0, 0.01)
    az = 9.81 + random.gauss(0, 0.02)
    mag_g = math.sqrt(ax * ax + ay * ay + az * az) / 9.80665

    return {
        "timestamp": iso_now(),
        "zone_id": "ZONE_A",
        "gps": {"lat": 9.9252, "lon": 78.1198, "valid": True},
        "accel": {"x": ax, "y": ay, "z": az},
        "gyro": {
            "x": 0.01 + random.gauss(0, 0.02),
            "y": 0.02 + random.gauss(0, 0.02),
            "z": random.gauss(0, 0.01),
        },
        "acoustic_rms": max(0.0, acoustic + random.gauss(0, 0.01)),
        "acoustic_peak_freq": peak,
        "crack_score": max(0.0, min(1.0, crack)),
        "crack_detected": crack >= 0.45,
        "crack_count": 1 if crack >= 0.45 else 0,
        "accel_rms_30s": abs(mag_g - 1.0) + abs(vib),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="ISMS sensor-node simulator")
    parser.add_argument("--url", default=f"{config.DASHBOARD_API_URL}/sensor-data")
    parser.add_argument("--mode", choices=["normal", "bearing", "cavitation", "crack"], default="normal")
    parser.add_argument("--hz", type=float, default=2.0, help="POST rate (spec is 2 Hz)")
    args = parser.parse_args()

    period = 1.0 / max(args.hz, 0.2)
    t0 = time.time()
    print(f"Posting {args.mode} frames to {args.url}")
    while True:
        t = time.time() - t0
        payload = frame(t, args.mode)
        try:
            r = requests.post(args.url, json=payload, timeout=2.0)
            print(f"{payload['timestamp']} {r.status_code} {r.text[:120]}")
        except Exception as exc:  # noqa: BLE001
            print(f"POST failed: {exc}")
        time.sleep(period)


if __name__ == "__main__":
    main()
