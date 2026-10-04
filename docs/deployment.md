# Deploying to the Raspberry Pi

The Pi runs everything on the processing side: the FastAPI node as a systemd service, Ollama with
Gemma 4, and a Chromium kiosk that shows the HMI on boot. Provisioning is driven from a laptop
over SSH.

## 1. Prepare the Pi

- Raspberry Pi OS (Bookworm, 64-bit) with the desktop (labwc/Wayland) and Chromium.
- Same Wi-Fi network as the node; note the Pi's IP (`hostname -I`).
- Install Ollama and pull the model:

  ```bash
  curl -fsSL https://ollama.com/install.sh | sh
  ollama pull gemma4:e2b-it-qat
  ```

## 2. Configure the laptop side

```bash
cd backend
cp .env.example .env
```

Set the SSH fields in `backend/.env`:

```ini
PI_SSH_HOST=192.168.1.100
PI_SSH_USER=pi
PI_SSH_PASS=your-password
```

Install the deployment dependency: `pip install paramiko python-dotenv`.

## 3. Deploy

```bash
python deploy/deploy_pi.py          # first time: copy code, install deps, create service and kiosk
python deploy/deploy_pi.py --sync   # later: copy code and restart the service only
```

Provisioning (`deploy/pi_setup.sh`, idempotent) installs `python3-opencv` and `python3-scipy`
from apt, creates a venv in `~/isms`, installs the `isms-api` systemd unit, and adds the kiosk to
labwc autostart. The Pi gets its own `.env` with `OLLAMA_HOST=http://127.0.0.1:11434`.

## 4. Point the node at the Pi

Set `ISMS_PI_HOST` in `firmware/esp-idf/main/isms_secrets.h` to the Pi's IP and flash — see
[firmware/esp-idf](../firmware/esp-idf/README.md).

## Operating the Pi

Over SSH (PuTTY or `ssh user@pi-ip`):

```bash
# start / restart everything
sudo systemctl restart ollama isms-api
bash ~/pi_kiosk_restart.sh

# stop everything
pkill -f isms-kiosk-profile
sudo systemctl stop isms-api ollama

# watch the node posting
journalctl -u isms-api -f
```

Helper scripts in `deploy/` (copy to the Pi and run with `PI_PASS` set where they need sudo):

| Script | Purpose |
| --- | --- |
| `pi_kiosk_restart.sh` | rewrite the kiosk launcher and restart Chromium |
| `pi_stop.sh` | stop kiosk, API and unload Gemma |
| `pi_cpu_cleanup.sh` | cap Ollama to 2 threads, unload idle model, stop background apt |
| `pi_latency_fix.sh` | shorter Ollama keep-alive when the node's posts start timing out |
| `pi_stream_check.sh` | print node online state, buffer and camera status |
| `pi_cpu_snap.sh`, `pi_cpu_detail.sh` | CPU and process diagnostics |
| `pi_grab_frames.sh` | save a few live frames to `/tmp/rust_live` |

`python deploy/pi.py "<command>"` runs a one-off command on the Pi using the credentials in `.env`.
