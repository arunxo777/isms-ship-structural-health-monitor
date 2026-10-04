#!/bin/bash
# Reinstall the kiosk launcher (with the Wayland ozone flag) and restart it.
export XDG_RUNTIME_DIR=/run/user/1000
export WAYLAND_DISPLAY=wayland-0

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
pkill -f "isms-kiosk-profile" 2>/dev/null
sleep 1
# Chromium defaults to the X11 backend on this image, so the platform must
# be named explicitly or it exits with "Missing X server".
exec /usr/lib/chromium/chromium \
  --ozone-platform=wayland \
  --user-data-dir="$HOME/.isms-kiosk-profile" \
  --password-store=basic \
  --disable-extensions \
  --disable-component-extensions-with-background-pages \
  --disable-background-networking \
  --disable-sync \
  --disable-translate \
  --renderer-process-limit=1 \
  --kiosk --noerrdialogs --disable-infobars --no-first-run \
  --disable-session-crashed-bubble --disable-features=TranslateUI \
  --check-for-update-interval=31536000 \
  --force-device-scale-factor=1.0 \
  "$URL"
EOF
chmod +x "$HOME/isms-kiosk.sh"

# Without --password-store=basic, Chromium asks gnome-keyring to unlock on
# every boot and the prompt sits on top of the kiosk.
pkill -f "isms-kiosk-profile" 2>/dev/null
pkill -f gnome-keyring-prompt 2>/dev/null
pkill -f squeekboard 2>/dev/null
sleep 2
nohup "$HOME/isms-kiosk.sh" > /tmp/kiosk.log 2>&1 &
sleep 22
echo "chromium: $(pgrep -c -f isms-kiosk-profile) processes"
tail -3 /tmp/kiosk.log
