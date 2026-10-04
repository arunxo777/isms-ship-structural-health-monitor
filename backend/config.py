"""
ISMS processing-node constants.

Every value can be overridden from `.env` next to this file (template:
`.env.example`). The defaults describe the canonical deployment: the
Raspberry Pi runs this API, Ollama and the kiosk HMI on the same host.
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

# Resolve paths relative to this file so uvicorn can be started from any cwd.
ROOT_DIR = Path(__file__).resolve().parent
load_dotenv(ROOT_DIR / ".env")


def _env(name: str, default: str) -> str:
    value = os.getenv(name, default)
    return default if value is None or value.strip() == "" else value.strip()


def _env_float(name: str, default: float) -> float:
    try:
        return float(_env(name, str(default)))
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(_env(name, str(default)))
    except ValueError:
        return default


# --- Network ---
PI_HOST = _env("PI_HOST", "127.0.0.1")
PI_PORT = _env_int("PI_PORT", 8000)
PI_BIND_HOST = _env("PI_BIND_HOST", "0.0.0.0")

# Dashboard runs on the Pi so it talks to localhost, not the LAN IP.
DASHBOARD_API_URL = _env("DASHBOARD_API_URL", f"http://127.0.0.1:{PI_PORT}")

# --- Ollama ---
OLLAMA_HOST = _env("OLLAMA_HOST", "http://127.0.0.1:11434")
OLLAMA_GENERATE_URL = f"{OLLAMA_HOST.rstrip('/')}/api/generate"
OLLAMA_TAGS_URL = f"{OLLAMA_HOST.rstrip('/')}/api/tags"
OLLAMA_CHAT_URL = f"{OLLAMA_HOST.rstrip('/')}/api/chat"
OLLAMA_MODEL = _env("OLLAMA_MODEL", "gemma4:e2b-it-qat")
OLLAMA_TIMEOUT_S = _env_float("OLLAMA_TIMEOUT_S", 180.0)
# Gemma runs on the Pi CPU, so re-ask only when the situation changes or
# after this many seconds. Without it the model is queried every frame.
# Longer than a Gemma round-trip on the Pi (~30 s) so one call cannot
# immediately queue the next and pin all four cores.
OLLAMA_MIN_INTERVAL_S = _env_float("OLLAMA_MIN_INTERVAL_S", 90.0)

# --- Models / buffers ---
ANOMALY_MODEL_PATH = ROOT_DIR / _env("ANOMALY_MODEL_PATH", "anomaly_model.pkl")
BUFFER_MAX = _env_int("BUFFER_MAX", 3600)          # 30 min @ 500 ms
ANOMALY_HISTORY_MAX = _env_int("ANOMALY_HISTORY_MAX", 360)  # 3 min
HISTORY_RETURN = _env_int("HISTORY_RETURN", 100)
TREND_PERIOD_S = _env_float("TREND_PERIOD_S", 10.0)

# Isolation Forest feature order (pose-invariant; must match train_anomaly_model.py).
FEATURE_ORDER = (
    "grav_res",
    "gyro_n",
    "acoustic_rms",
    "peak_n",
    "accel_rms_30s",
    "crack_score",
    "cavitation",
    "still",
)

CRACK_WARN = _env_float("CRACK_WARN", 0.45)
CRACK_CRITICAL = _env_float("CRACK_CRITICAL", 0.75)
RUST_WARN = _env_float("RUST_WARN", 0.06)

# Hull vision tuning, calibrated on real frames from this node. Exposed via
# .env so they can be relaxed at the dockside without editing code.
BREAK_LEN_MIN = _env_float("BREAK_LEN_MIN", 40.0)
BREAK_CONTRAST_MIN = _env_float("BREAK_CONTRAST_MIN", 38.0)
PLATE_MACRO_MAX = _env_float("PLATE_MACRO_MAX", 32.0)

# --- Prediction thresholds ---
FAULT_RMS_THRESHOLD = _env_float("FAULT_RMS_THRESHOLD", 0.7)
DRIFT_WARN_PCT = _env_float("DRIFT_WARN_PCT", 3.0)
DRIFT_FAULT_PCT = _env_float("DRIFT_FAULT_PCT", 5.0)
ANOMALY_RATE_WARN = _env_float("ANOMALY_RATE_WARN", 1.0)
ANOMALY_RATE_URGENT = _env_float("ANOMALY_RATE_URGENT", 3.0)
URGENT_TTF_HOURS = _env_float("URGENT_TTF_HOURS", 2.0)

# Chart windows on the pygame HMI (5 min @ 1 s ≈ 300 pts).
CHART_POINTS = _env_int("CHART_POINTS", 300)
SAMPLE_INTERVAL_S = 0.5

# Exact Ollama system prompt required by the hackathon spec.
OLLAMA_SYSTEM_PROMPT = """You are an expert maritime structural health monitoring AI for an Indigenous Ship Monitoring System (ISMS). You analyze hull-node IMU, microphone, and camera frames. If an image is attached, inspect the steel plate for orange/brown rust (corrosion) and dark linear cracks or weld breaks.

You are the explanation layer. A deterministic vision and physics stage has
already measured the hull and its findings are given to you. Your job is to
explain, in one or two sentences a duty officer can act on, what those
measurements mean. Never invent a fault that no stage confirmed, and never
describe an image you were not given.

Fault signatures:
- HULL RUST / CORROSION: orange-brown patches on the plate
- HULL CRACK / BREAK: thin dark elongated fractures, weld splits
- BEARING WEAR: rising vibration RMS + new acoustic peak >300Hz
- HULL FATIGUE: gravity residual while still, gyro energy on a still node
- CAVITATION: high acoustic RMS in 200-800Hz
- HANDLING: the node was picked up — not a hull fault
- NORMAL: still, |a|~1g, quiet acoustics, clean plate

Reply with JSON only, no markdown:
{"status":"NORMAL|WARNING|CRITICAL","fault_type":"description or None","confidence":0.0,"time_to_fault":"None","recommended_action":"...","reasoning":"..."}"""

OLLAMA_USER_TEMPLATE = """Current sensor readings from {zone_id}:
- Accel RMS: {accel_rms} g (1min avg: {avg_1min}, 10min avg: {avg_10min})
- Gyro: X={gyro_x} Y={gyro_y} Z={gyro_z} dps
- Acoustic RMS: {acoustic_rms}, Peak freq: {peak_freq}Hz
- Anomaly score: {anomaly_score} (label: {anomaly_label})
- Drift rate: {drift_rate}% over 10 minutes
- Time: {timestamp}"""
