import numpy as np
import pytest

import crack_detect
from probe_vision import CASES, H, W, jpeg, steel_plate, with_crack


@pytest.fixture(params=["opencv", "numpy"])
def vision_engine(request, monkeypatch):
    if request.param == "opencv" and not crack_detect._HAS_CV2:
        pytest.skip("OpenCV not installed")
    if request.param == "numpy":
        monkeypatch.setattr(crack_detect, "_HAS_CV2", False)
    return request.param


@pytest.mark.parametrize("expect,name,arr", CASES, ids=[c[1] for c in CASES])
def test_control_images(vision_engine, expect, name, arr):
    r = crack_detect.detect_hull(jpeg(arr), save_frame=False, confirm=False)
    rust, brk = bool(r["rust_detected"]), bool(r["break_detected"])
    expected = {
        "CLEAN": (False, False),
        "RUST": (True, False),
        "CRACK": (False, True),
        "BOTH": (True, True),
    }[expect]
    assert (rust, brk) == expected, f"{name}: rust={rust} break={brk}"


@pytest.mark.parametrize(
    "arr,status",
    [
        (np.full((H, W, 3), 6, np.uint8), "DARK"),
        (np.full((H, W, 3), 250, np.uint8), "BLOWN_OUT"),
        (np.full((H, W, 3), 128, np.uint8), "NO_TARGET"),
    ],
)
def test_unusable_frames_carry_no_evidence(arr, status):
    r = crack_detect.detect_hull(jpeg(arr), save_frame=False, confirm=False)
    assert r["status"] == status
    assert r["usable"] is False
    assert r["rust_score"] == 0.0 and r["crack_score"] == 0.0


@pytest.mark.parametrize("payload,status", [(b"", "EMPTY"), (b"not a jpeg", "DECODE_ERROR")])
def test_bad_input(payload, status):
    assert crack_detect.analyse_frame(payload)["status"] == status


def test_single_frame_is_not_confirmed():
    cracked = jpeg(with_crack(steel_plate()))
    first = crack_detect.detect_hull(cracked, save_frame=False)
    assert first["break_seen"] is True
    assert first["break_detected"] is False


def test_sustained_break_is_confirmed():
    cracked = jpeg(with_crack(steel_plate()))
    results = [crack_detect.detect_hull(cracked, save_frame=False) for _ in range(5)]
    assert results[-1]["break_detected"] is True


def test_result_is_json_safe():
    r = crack_detect.analyse_frame(jpeg(with_crack(steel_plate())))
    r.pop("overlay_jpeg")
    for detail in ("rust_detail", "break_detail"):
        assert "mask" not in r[detail]
