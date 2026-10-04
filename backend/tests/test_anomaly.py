import math

import numpy as np
from conftest import healthy_payload

import config
from anomaly import AnomalyEngine, reading_from_payload


def test_invariant_vector_matches_feature_order():
    vec = reading_from_payload(healthy_payload()).invariant_vector()
    assert vec.shape == (1, len(config.FEATURE_ORDER))
    assert np.isfinite(vec).all()


def test_tilt_does_not_change_gravity_residual():
    level = reading_from_payload(healthy_payload())
    g = 9.81
    tilted = reading_from_payload(
        healthy_payload(accel={"x": g * math.sin(0.6), "y": 0.0, "z": g * math.cos(0.6)})
    )
    assert abs(level.grav_res - tilted.grav_res) < 1e-3


def test_mode_follows_calibration_and_stillness():
    engine = AnomalyEngine()
    for payload, mode in [
        (healthy_payload(imu_calibrated=False), "CAL"),
        (healthy_payload(still=True), "STILL"),
        (healthy_payload(still=False), "MOVE"),
    ]:
        reading = reading_from_payload(payload)
        engine.ingest(reading)
        assert reading.mode == mode


def test_garbage_values_are_sanitised():
    reading = reading_from_payload(
        healthy_payload(acoustic_rms="nan", gps={"lat": 999, "lon": -999, "valid": True})
    )
    assert math.isfinite(reading.acoustic_rms)


def test_health_index_is_bounded():
    engine = AnomalyEngine()
    for i in range(50):
        engine.ingest(reading_from_payload(healthy_payload(i, acoustic_rms=1.2, accel_rms_30s=1.5)))
    engine.compute_trends()
    assert 0.0 <= engine.trend.health_index <= 100.0
