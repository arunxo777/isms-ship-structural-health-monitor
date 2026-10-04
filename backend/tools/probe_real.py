"""Run the detector over real frames captured from the node.

Every frame in data/frames/ was captured with the camera pointed at a room,
not a hull plate, so none of them may report rust or a break.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import crack_detect  # noqa: E402

frames = sorted(Path("data/frames").glob("*.jpg"))
extra = Path("data/real_frame.jpg")
if extra.exists():
    frames.append(extra)

if not frames:
    print("no real frames — run tools/collect_frames.py first")
    sys.exit(0)

print(f"{'frame':18s} {'status':11s} {'plate':6s} rust  break  note")
print("-" * 104)
bad = 0
for p in frames:
    r = crack_detect.detect_hull(p.read_bytes(), save_frame=False, confirm=False)
    hit = bool(r.get("rust_detected") or r.get("break_detected"))
    if hit:
        bad += 1
    plate = "-" if not r.get("usable") else ("yes" if r.get("plate_like") else "no")
    print(
        f"{p.name:18s} {str(r.get('status')):11s} {plate:6s} "
        f"{r.get('rust_score', 0):.2f}  {r.get('crack_score', 0):.2f}   "
        f"{'FALSE ALARM: ' if hit else ''}{r.get('note', '')[:58]}"
    )
print("-" * 104)
print(f"{len(frames) - bad}/{len(frames)} real frames correctly reported no hull fault")
sys.exit(1 if bad else 0)
