#!/bin/bash
# Free CPU/RAM that was starving ESP32 HTTP posts.
set -u
SUDO() { echo "$PI_PASS" | sudo -S -p '' "$@"; }

echo "=== unload Gemma ==="
timeout 6 ollama stop gemma4:e2b-it-qat >/dev/null 2>&1 || true
pkill -f /usr/local/lib/ollama/llama-server 2>/dev/null || true
sleep 2

echo "=== keep_alive short ==="
SUDO mkdir -p /etc/systemd/system/ollama.service.d
SUDO tee /etc/systemd/system/ollama.service.d/isms.conf >/dev/null <<'EOF'
[Service]
Environment=OLLAMA_HOST=0.0.0.0:11434
Environment=OLLAMA_NUM_THREAD=2
Environment=OLLAMA_KEEP_ALIVE=30s
Environment=OLLAMA_MAX_LOADED_MODELS=1
EOF
SUDO systemctl daemon-reload
SUDO systemctl restart ollama
sleep 2

echo "=== restart isms-api ==="
SUDO systemctl restart isms-api
sleep 5

echo "=== status ==="
echo -n "isms-api: "; systemctl is-active isms-api
echo -n "ollama: "; systemctl is-active ollama
echo -n "llama: "; pgrep -c llama-server 2>/dev/null || echo 0
free -m | head -2
uptime
curl -s -o /dev/null -w "health %{http_code} in %{time_total}s\n" --max-time 5 http://127.0.0.1:8000/health
curl -s --max-time 5 http://127.0.0.1:8000/latest-verdict | head -c 180; echo
