#!/bin/bash
# Stop the ISMS CIC / API / Gemma runner on the Pi.
set -u
SUDO() { echo "$PI_PASS" | sudo -S -p '' "$@"; }

echo "=== before ==="
echo -n "isms-api: "; systemctl is-active isms-api 2>/dev/null || true
echo -n "kiosk: "; pgrep -c -f isms-kiosk-profile 2>/dev/null || echo 0
echo -n "llama: "; pgrep -c llama-server 2>/dev/null || echo 0

echo "=== stopping kiosk ==="
pkill -f isms-kiosk-profile 2>/dev/null || true
pkill -f isms-kiosk.sh 2>/dev/null || true
sleep 1

echo "=== stopping isms-api ==="
SUDO systemctl stop isms-api

echo "=== unloading Gemma ==="
timeout 5 ollama stop gemma4:e2b-it-qat >/dev/null 2>&1 || true
pkill -f /usr/local/lib/ollama/llama-server 2>/dev/null || true
sleep 1

echo "=== after ==="
echo -n "isms-api: "; systemctl is-active isms-api 2>/dev/null || true
echo -n "kiosk: "; pgrep -c -f isms-kiosk-profile 2>/dev/null || echo 0
echo -n "llama: "; pgrep -c llama-server 2>/dev/null || echo 0
echo -n "ollama: "; systemctl is-active ollama 2>/dev/null || true
uptime
echo "stopped. ESP32 posts will fail until you start again."
