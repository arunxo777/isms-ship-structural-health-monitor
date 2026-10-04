#!/bin/bash
# Provision the ISMS processing node on the Raspberry Pi.
# Idempotent: safe to re-run. Needs PI_PASS in the environment for sudo.
set -u
APP=$HOME/isms
VENV=$APP/venv

# sudo without touching the Pi's sudoers policy. Files destined for /etc are
# staged in /tmp first, because a heredoc would collide with sudo's stdin.
SUDO() { echo "$PI_PASS" | sudo -S -p '' "$@"; }

echo "=== apt update"
SUDO apt-get update -q 2>&1 | tail -2

echo "=== apt packages"
# numpy and Pillow are already system packages. scipy gives crack_detect its
# fast connected-component path. python3-opencv is the Pi-side rust CV engine.
SUDO apt-get install -y -q python3-venv python3-scipy python3-opencv 2>&1 | tail -5

echo "=== venv"
if [ ! -x "$VENV/bin/python" ]; then
  # --system-site-packages reuses the apt numpy/Pillow/scipy instead of
  # compiling wheels on the Pi.
  python3 -m venv --system-site-packages "$VENV" || exit 1
fi
"$VENV/bin/pip" install -q --upgrade pip 2>&1 | tail -2
"$VENV/bin/pip" install -q fastapi "uvicorn[standard]" httpx python-dotenv 2>&1 | tail -5

echo "=== runtime check"
"$VENV/bin/python" - <<'PY'
for m in ["numpy", "PIL", "scipy", "cv2", "fastapi", "uvicorn", "httpx", "dotenv", "pydantic"]:
    try:
        mod = __import__(m)
        print(f"  {m:10s} {getattr(mod, '__version__', 'ok')}")
    except Exception as exc:
        print(f"  {m:10s} MISSING ({exc})")
PY

echo "=== app import + self-test"
cd "$APP" && "$VENV/bin/python" -c "import crack_detect as cd; import server; print('  vision', 'opencv' if cd._HAS_CV2 else 'numpy'); print('  server imports ok')" 2>&1 | tail -6

echo "=== systemd service"
cat > /tmp/isms-api.service <<EOF
[Unit]
Description=ISMS hull processing node (FastAPI)
After=network-online.target ollama.service
Wants=network-online.target

[Service]
Type=simple
User=$(id -un)
WorkingDirectory=$APP
ExecStart=$VENV/bin/uvicorn server:app --host 0.0.0.0 --port 8000 --log-level info
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF
SUDO cp /tmp/isms-api.service /etc/systemd/system/isms-api.service
SUDO chown root:root /etc/systemd/system/isms-api.service
SUDO systemctl daemon-reload
SUDO systemctl enable isms-api.service 2>&1 | tail -1
SUDO systemctl restart isms-api.service
sleep 7
echo "  status: $(systemctl is-active isms-api.service)"
systemctl is-active isms-api.service >/dev/null || \
  SUDO journalctl -u isms-api -n 25 --no-pager | tail -25

echo "=== api probe"
curl -s --max-time 10 http://127.0.0.1:8000/health | head -c 420; echo

echo "=== kiosk launcher"
cat > "$HOME/isms-kiosk.sh" <<'EOF'
#!/bin/bash
# Full-screen the CIC on the Pi's DSI panel (1280x720 after its 270 transform).
export XDG_RUNTIME_DIR=/run/user/1000
export WAYLAND_DISPLAY=wayland-0
URL=http://127.0.0.1:8000/
# Wait for the API so the kiosk never shows a browser error page.
for i in $(seq 1 60); do
  curl -s -o /dev/null --max-time 2 "$URL" && break
  sleep 2
done
pkill -f "chromium.*isms-kiosk-profile" 2>/dev/null
sleep 1
exec /usr/lib/chromium/chromium \
  --ozone-platform=wayland \
  --user-data-dir="$HOME/.isms-kiosk-profile" \
  --password-store=basic \
  --disable-extensions \
  --disable-component-extensions-with-background-pages \
  --disable-background-networking \
  --disable-sync \
  --renderer-process-limit=1 \
  --kiosk --noerrdialogs --disable-infobars --no-first-run \
  --disable-session-crashed-bubble --disable-features=TranslateUI \
  --check-for-update-interval=31536000 \
  --force-device-scale-factor=1.0 \
  "$URL"
EOF
chmod +x "$HOME/isms-kiosk.sh"

# labwc runs the desktop session, so its autostart brings the kiosk up on boot.
mkdir -p "$HOME/.config/labwc"
touch "$HOME/.config/labwc/autostart"
if ! grep -q "isms-kiosk" "$HOME/.config/labwc/autostart"; then
  echo "$HOME/isms-kiosk.sh &" >> "$HOME/.config/labwc/autostart"
fi
echo "  autostart:"; sed 's/^/    /' "$HOME/.config/labwc/autostart"

echo "=== done"
