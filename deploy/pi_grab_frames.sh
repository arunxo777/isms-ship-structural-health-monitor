#!/bin/bash
mkdir -p /tmp/rust_live
rm -f /tmp/rust_live/*
for i in 0 1 2 3 4 5; do
  curl -s --max-time 4 "http://127.0.0.1:8000/camera-frame" -o "/tmp/rust_live/f$i.jpg"
  sleep 2.2
done
ls -la /tmp/rust_live
python3 - <<'PY'
import json, urllib.request
print(json.dumps(json.load(urllib.request.urlopen("http://127.0.0.1:8000/latest-verdict", timeout=5)), indent=0)[:500])
PY
