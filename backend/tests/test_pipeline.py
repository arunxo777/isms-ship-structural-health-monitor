"""Camera -> vision -> fusion -> verdict, end to end without the HTTP layer."""

from conftest import healthy_payload

import crack_detect
from anomaly import AnomalyEngine, reading_from_payload
from probe_vision import jpeg, steel_plate, with_crack, with_rust

CLEAN = jpeg(steel_plate())
CRACKED = jpeg(with_crack(steel_plate()))
RUSTY = jpeg(with_rust(steel_plate()))


class Run:
    def __init__(self) -> None:
        self.engine = AnomalyEngine()
        self.i = 0

    def feed(self, frames):
        statuses = []
        for frame in frames:
            self.engine.update_camera(crack_detect.detect_hull(frame, save_frame=False))
            self.engine.ingest(reading_from_payload(healthy_payload(self.i)))
            self.engine.compute_trends()
            statuses.append(self.engine.last_verdict["status"])
            self.i += 1
        return statuses


def test_healthy_hull_stays_normal():
    run = Run()
    statuses = run.feed([CLEAN] * 60)
    assert set(statuses[10:]) == {"NORMAL"}
    assert run.engine.trend.anomaly_rate_per_min == 0.0


def test_stray_frame_does_not_escalate():
    run = Run()
    run.feed([CLEAN] * 30)
    assert run.feed([CRACKED])[-1] == "NORMAL"
    assert run.feed([CLEAN] * 2)[-1] == "NORMAL"


def test_confirmed_faults_escalate_and_clear():
    run = Run()
    run.feed([CLEAN] * 30)

    assert run.feed([CRACKED] * 8)[-1] != "NORMAL"
    assert run.engine.latest_camera["break_detected"]

    run.feed([RUSTY] * 8)
    assert run.engine.latest_camera["rust_detected"]

    assert run.feed([CLEAN] * 8)[-1] == "NORMAL"
    assert not run.engine.latest_camera["break_detected"]
    assert not run.engine.latest_camera["rust_detected"]
