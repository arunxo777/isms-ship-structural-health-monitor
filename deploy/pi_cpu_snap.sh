#!/bin/bash
echo "=== load / mem ==="
uptime
free -m | head -2
echo
echo "=== top cpu (htop-style) ==="
ps -eo pid,ppid,pcpu,pmem,rss,comm,args --sort=-pcpu | head -25
echo
echo "=== chromium windows / tabs ==="
pgrep -a chromium | head -40
echo
echo "chromium process count: $(pgrep -c chromium || echo 0)"
echo "kiosk profile count: $(pgrep -c -f isms-kiosk-profile || echo 0)"
echo
echo "=== llama / ollama ==="
pgrep -a llama-server || true
pgrep -a ollama | head -10
echo
echo "=== isms-api ==="
systemctl is-active isms-api
pgrep -a uvicorn || true
echo
echo "=== extra browsers / terminals ==="
pgrep -a firefox || true
pgrep -a lxterminal || true
pgrep -a pcmanfm || true
pgrep -a wf-panel || true
echo
echo "=== autostart ==="
cat "$HOME/.config/labwc/autostart" 2>/dev/null
echo
echo "=== chromium running as non-kiosk ==="
ps -eo pid,args | grep -i chromium | grep -v grep | grep -v isms-kiosk-profile || echo "(none besides kiosk)"
