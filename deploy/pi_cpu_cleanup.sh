#!/bin/bash
# Stop the processes that were pinning the Pi at ~250% CPU.
set -u
SUDO() { echo "$PI_PASS" | sudo -S -p '' "$@"; }

echo "=== stop in-flight Gemma ==="
# ollama stop waits on the current chat; kill the runner if it hangs.
timeout 8 ollama stop gemma4:e2b-it-qat >/dev/null 2>&1 || true
pkill -f /usr/local/lib/ollama/llama-server 2>/dev/null || true
sleep 2
echo "  llama-server: $(pgrep -c llama-server || echo 0)"
echo "  ollama ps:"
ollama ps || true

echo "=== cap Ollama to 2 CPU threads ==="
SUDO mkdir -p /etc/systemd/system/ollama.service.d
SUDO tee /etc/systemd/system/ollama.service.d/isms.conf >/dev/null <<'EOF'
[Service]
Environment=OLLAMA_HOST=0.0.0.0:11434
Environment=OLLAMA_NUM_THREAD=2
Environment=OLLAMA_KEEP_ALIVE=2m
Environment=OLLAMA_MAX_LOADED_MODELS=1
EOF
SUDO systemctl daemon-reload
SUDO systemctl restart ollama
sleep 3
echo "  ollama: $(systemctl is-active ollama)"

echo "=== stop PackageKit (background apt) ==="
SUDO systemctl stop packagekit 2>/dev/null || true
SUDO systemctl stop packagekitd 2>/dev/null || true

echo "=== unload unused 1-wire bus (w1_bus_master1) ==="
SUDO rmmod w1_therm 2>/dev/null || true
SUDO rmmod w1_gpio 2>/dev/null || true
SUDO rmmod wire 2>/dev/null || true

echo "=== cpu now ==="
uptime
ps -eo pid,pcpu,pmem,rss,comm --sort=-pcpu | head -10
