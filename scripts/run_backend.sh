#!/usr/bin/env bash
# Run the ISMS processing node (API + web HMI) from a checkout.
set -euo pipefail
cd "$(dirname "$0")/../backend"

PYTHON="${PYTHON:-python3}"
[ -f anomaly_model.pkl ] || "$PYTHON" train_anomaly_model.py
exec "$PYTHON" -m uvicorn server:app --host "${PI_BIND_HOST:-0.0.0.0}" --port "${PI_PORT:-8000}"
