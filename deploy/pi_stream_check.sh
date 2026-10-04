#!/bin/bash
for i in 1 2 3; do
  curl -s --max-time 5 http://127.0.0.1:8000/latest-verdict > /tmp/v.json
  python3 - <<'PY'
import json
d=json.load(open("/tmp/v.json"))
print(f"online={d.get('node_online')} age={d.get('node_age_s')} buf={d.get('buffer_size')} cam={d.get('camera_status')} mode={d.get('mode')} status={d.get('status')}")
PY
  sleep 3
done
echo "---"
uptime
free -m | head -2
pgrep llama-server >/dev/null && echo llama=YES || echo llama=no
curl -s -o /dev/null -w "health %{time_total}s\n" --max-time 5 http://127.0.0.1:8000/health
