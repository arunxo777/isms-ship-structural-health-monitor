import pytest
from conftest import healthy_payload
from fastapi.testclient import TestClient

import server
from probe_vision import jpeg, steel_plate


@pytest.fixture()
def client():
    with TestClient(server.app) as c:
        yield c


def test_health(client):
    body = client.get("/health").json()
    assert body["status"] == "ok"
    assert body["ollama"]["reachable"] is False
    assert "buffer_capacity" in body


def test_hmi_is_served(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]


def test_sensor_data_roundtrip(client):
    before = client.get("/health").json()["buffer_size"]
    for i in range(3):
        r = client.post("/sensor-data", json=healthy_payload(i))
        assert r.status_code == 200
        assert r.json()["status"] == "ok"

    verdict = client.get("/latest-verdict").json()
    assert verdict["node_online"] is True
    assert verdict["zone_id"] == "ENGINE_BAY"
    assert verdict["status"] in {"NORMAL", "WARNING", "CRITICAL"}
    assert client.get("/health").json()["buffer_size"] == before + 3
    assert len(client.get("/history").json()) >= 3


def test_non_finite_values_are_rejected(client):
    body = (
        '{"timestamp":"2026-09-09T12:00:00","zone_id":"Z","gps":{"lat":0,"lon":0,"valid":false},'
        '"accel":{"x":NaN,"y":0,"z":9.8},"gyro":{"x":0,"y":0,"z":0},'
        '"acoustic_rms":0.1,"acoustic_peak_freq":100,"accel_rms_30s":0.1}'
    )
    r = client.post("/sensor-data", content=body, headers={"content-type": "application/json"})
    assert r.status_code == 422


def test_missing_fields_are_rejected(client):
    assert client.post("/sensor-data", json={"zone_id": "Z"}).status_code == 422


def test_camera_frame_is_acknowledged(client):
    r = client.post("/camera-frame", content=jpeg(steel_plate()), headers={"content-type": "image/jpeg"})
    assert r.status_code == 200
    assert r.json()["accepted"] is True
    assert client.get("/camera-frame").status_code == 200


def test_empty_camera_frame(client):
    r = client.post("/camera-frame", content=b"")
    assert r.json()["accepted"] is False
