"""Grab a series of live frames from the node for offline calibration.

    ISMS_API=http://<pi-ip>:8000 python tools/collect_frames.py 12 2.0 real
Saves data/frames/<tag>_NN.jpg, skipping bytewise duplicates.
"""

from __future__ import annotations

import hashlib
import os
import sys
import time
from pathlib import Path

import httpx

BASE = os.getenv("ISMS_API", "http://127.0.0.1:8000").rstrip("/")
count = int(sys.argv[1]) if len(sys.argv) > 1 else 10
gap = float(sys.argv[2]) if len(sys.argv) > 2 else 2.0
tag = sys.argv[3] if len(sys.argv) > 3 else "real"

out = Path("data/frames")
out.mkdir(parents=True, exist_ok=True)
seen, saved = set(), 0

deadline = time.time() + count * gap + 30
with httpx.Client(timeout=10.0) as c:
    while saved < count and time.time() < deadline:
        try:
            r = c.get(f"{BASE}/camera-frame")
            if r.status_code != 200 or not r.content:
                print("  no frame")
                time.sleep(gap)
                continue
            h = hashlib.md5(r.content).hexdigest()
            if h in seen:
                time.sleep(gap)
                continue
            seen.add(h)
            p = out / f"{tag}_{saved:02d}.jpg"
            p.write_bytes(r.content)
            print(f"  saved {p.name} ({len(r.content)} B)")
            saved += 1
        except Exception as exc:
            print("  err", exc)
        time.sleep(gap)

print(f"{saved} frames in {out}")
