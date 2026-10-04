import os

# Point the LLM client at a closed port so tests never reach a real Ollama.
# load_dotenv() does not override variables that are already set.
os.environ["OLLAMA_HOST"] = "http://127.0.0.1:9"
os.environ["OLLAMA_TIMEOUT_S"] = "1"

import pytest  # noqa: E402

import crack_detect  # noqa: E402


@pytest.fixture(autouse=True)
def _fresh_vision_tracker():
    crack_detect.reset_tracker()
    yield
    crack_detect.reset_tracker()


def healthy_payload(i: int = 0, **overrides) -> dict:
    payload = {
        "timestamp": f"2026-09-09T12:{(i // 60) % 60:02d}:{i % 60:02d}",
        "zone_id": "ENGINE_BAY",
        "gps": {"lat": 12.9, "lon": 77.6, "valid": True},
        "accel": {"x": 0.02, "y": -0.01, "z": 9.81},
        "gyro": {"x": 0.1, "y": 0.05, "z": 0.08},
        "acoustic_rms": 0.03,
        "acoustic_peak_freq": 120.0,
        "accel_rms_30s": 0.06,
        "still": True,
        "imu_calibrated": True,
        "mic_ok": True,
    }
    payload.update(overrides)
    return payload
