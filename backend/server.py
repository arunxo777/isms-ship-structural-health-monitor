"""
ISMS FastAPI processing node.

Endpoints:
    POST /sensor-data      ingest one hull-node frame, run Isolation Forest
    GET  /latest-verdict   last Ollama / rule-engine analysis
    GET  /history          last 100 samples for HMI charts
    GET  /health           Ollama, model, buffer, last timestamp
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel, Field, field_validator

import config
from anomaly import AnomalyEngine, reading_from_payload
from crack_detect import FRAME_PATH, OVERLAY_PATH, detect_cracks
from ollama_client import OllamaClient, default_verdict

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("isms.server")

engine = AnomalyEngine()
ollama = OllamaClient()
_state_lock = asyncio.Lock()
_ollama_task: Optional[asyncio.Task] = None
_vision_task: Optional[asyncio.Task] = None
_trend_task: Optional[asyncio.Task] = None


def _camera_ack() -> Dict[str, Any]:
    """Instant reply for the ESP32 — last known vision, no waiting on OpenCV."""
    cam = engine.latest_camera or {}
    return {
        "accepted": True,
        "queued": True,
        "crack_detected": bool(cam.get("crack_detected")),
        "crack_score": float(cam.get("crack_score") or 0.0),
        "crack_count": int(cam.get("crack_count") or 0),
        "rust_detected": bool(cam.get("rust_detected")),
        "rust_score": float(cam.get("rust_score") or 0.0),
        "break_detected": bool(cam.get("break_detected")),
        "status": cam.get("status", "PENDING"),
        "vision_engine": cam.get("vision_engine", "unknown"),
    }


class GpsModel(BaseModel):
    lat: float = 0.0
    lon: float = 0.0
    valid: bool = False

    @field_validator("lat")
    @classmethod
    def _lat(cls, value: float) -> float:
        if value != value or value < -90 or value > 90:  # NaN check
            return 0.0
        return value

    @field_validator("lon")
    @classmethod
    def _lon(cls, value: float) -> float:
        if value != value or value < -180 or value > 180:
            return 0.0
        return value


class Vec3(BaseModel):
    x: float
    y: float
    z: float

    @field_validator("x", "y", "z")
    @classmethod
    def _finite(cls, value: float) -> float:
        if value != value or value in (float("inf"), float("-inf")):
            raise ValueError("non-finite sensor value")
        return value


class SensorPayload(BaseModel):
    timestamp: str
    zone_id: str = Field(..., min_length=1, max_length=32)
    gps: GpsModel
    accel: Vec3
    gyro: Vec3
    acoustic_rms: float
    acoustic_peak_freq: float
    accel_rms_30s: float
    crack_score: float = 0.0
    crack_detected: bool = False
    crack_count: int = 0
    still: bool = False
    imu_calibrated: bool = True
    mic_ok: bool = False
    angle: Optional[Vec3] = None

    @field_validator("acoustic_rms", "acoustic_peak_freq", "accel_rms_30s", "crack_score")
    @classmethod
    def _finite_scalar(cls, value: float) -> float:
        if value != value or value in (float("inf"), float("-inf")):
            raise ValueError("non-finite sensor value")
        return value


async def _trend_loop() -> None:
    """Recompute 1/5/10 minute windows every TREND_PERIOD_S seconds."""
    while True:
        try:
            async with _state_lock:
                trend = await asyncio.to_thread(engine.compute_trends)
            logger.debug(
                "trend drift=%.2f%% anom/min=%.2f ttf=%s",
                trend.drift_rate_pct,
                trend.anomaly_rate_per_min,
                trend.time_to_fault_hours,
            )
        except Exception as exc:  # noqa: BLE001
            logger.error("trend loop error: %s", exc)
        await asyncio.sleep(config.TREND_PERIOD_S)


async def _run_ollama(reading_copy, trend_copy, jpeg=None, baseline=None, camera=None) -> None:
    try:
        verdict = await ollama.analyse(
            reading_copy, trend_copy, jpeg=jpeg, baseline=baseline, camera=camera
        )
        logger.info(
            "cached verdict %s (ai said %s)",
            verdict.get("status"),
            verdict.get("ai_status"),
        )
    except Exception as exc:  # noqa: BLE001
        logger.error("background Ollama task failed: %s", exc)


async def _process_camera(body: bytes) -> None:
    """OpenCV + overlay run off the HTTP request path so the ESP32 is not blocked."""
    global _ollama_task
    try:
        result = await asyncio.to_thread(detect_cracks, body, True)
        async with _state_lock:
            engine.update_camera(result)
            verdict = dict(ollama.last_verdict)
            verdict["crack_score"] = result["crack_score"]
            verdict["crack_detected"] = result["crack_detected"]
            verdict["crack_count"] = result["crack_count"]
            verdict["rust_score"] = result["rust_score"]
            verdict["rust_detected"] = result["rust_detected"]
            verdict["camera"] = engine.latest_camera
            ollama.last_verdict = verdict
            should = bool(
                result.get("usable")
                and (result.get("rust_detected") or result.get("break_detected"))
                and engine.should_call_ollama(-1)
            )
            reading_copy = engine.buffer[-1] if engine.buffer else None
            trend_copy = engine.trend
            baseline = dict(engine.last_verdict)
            camera_copy = dict(engine.latest_camera)
            camera_copy["rust_detail"] = result.get("rust_detail")
            camera_copy["break_detail"] = result.get("break_detail")
            busy = _ollama_task is not None and not _ollama_task.done()
            if should and reading_copy is not None and not busy:
                engine.note_ollama_call()
                _ollama_task = asyncio.create_task(
                    _run_ollama(
                        reading_copy, trend_copy, jpeg=body, baseline=baseline, camera=camera_copy
                    )
                )
        logger.info(
            "vision %s rust=%s break=%s",
            result.get("vision_engine"),
            result.get("rust_detected"),
            result.get("break_detected"),
        )
    except Exception as exc:  # noqa: BLE001
        logger.error("background vision failed: %s", exc)


def _save_raw_frame(jpeg: bytes) -> None:
    try:
        FRAME_PATH.parent.mkdir(parents=True, exist_ok=True)
        FRAME_PATH.write_bytes(jpeg)
    except Exception as exc:  # noqa: BLE001
        logger.warning("raw frame save failed: %s", exc)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _trend_task
    await ollama.health()
    engine.compute_trends()
    ollama.last_verdict = default_verdict()
    _trend_task = asyncio.create_task(_trend_loop())
    logger.info(
        "ISMS API listening — model_loaded=%s ollama=%s",
        engine.model_loaded,
        ollama.available,
    )
    try:
        yield
    finally:
        if _trend_task:
            _trend_task.cancel()
        if _ollama_task and not _ollama_task.done():
            _ollama_task.cancel()
        if _vision_task and not _vision_task.done():
            _vision_task.cancel()


app = FastAPI(title="ISMS Processing Node", version="1.0.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(RequestValidationError)
async def validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
    # The default handler echoes the rejected input, and json cannot encode
    # the NaN/Infinity a glitching sensor sends — that turned a 422 into a 500.
    detail = [{k: e[k] for k in ("loc", "msg", "type") if k in e} for e in exc.errors()]
    return JSONResponse(status_code=422, content={"detail": detail})


@app.post("/sensor-data")
async def post_sensor_data(payload: SensorPayload) -> Dict[str, Any]:
    """Validate, buffer, run Isolation Forest, maybe kick Ollama."""
    global _ollama_task
    reading = reading_from_payload(
        payload.model_dump() if hasattr(payload, "model_dump") else payload.dict()
    )

    async with _state_lock:
        label, score = await asyncio.to_thread(engine.ingest, reading)
        trend = engine.trend
        physics = dict(engine.last_verdict)
        should = engine.should_call_ollama(label)
        busy = _ollama_task is not None and not _ollama_task.done()
        if should and not busy:
            engine.note_ollama_call()
            cam = dict(engine.latest_camera)
            _ollama_task = asyncio.create_task(
                _run_ollama(reading, trend, baseline=physics, camera=cam)
            )

    return {
        "status": "ok",
        "anomaly_score": float(score),
        "anomaly_label": int(label),
        "health_index": physics.get("health_index"),
        "mode": physics.get("mode"),
    }


@app.post("/camera-frame")
async def camera_frame(request: Request) -> Dict[str, Any]:
    """Accept JPEG immediately; OpenCV runs in the background.

    The ESP32 used to wait for full vision (and sometimes Gemma) before the
    HTTP response returned, which looked like Wi‑Fi disconnects under load.
    """
    global _vision_task
    body = await request.body()
    if not body:
        return {"accepted": False, "crack_detected": False, "crack_score": 0.0, "crack_count": 0, "error": "empty"}

    # Keep the live preview fresh even if vision is still busy.
    await asyncio.to_thread(_save_raw_frame, body)

    if _vision_task is None or _vision_task.done():
        _vision_task = asyncio.create_task(_process_camera(body))
        queued = True
    else:
        # Drop this frame for vision — sensor stream stays healthy.
        queued = False
        logger.debug("vision busy — keeping preview, skipping OpenCV on this frame")

    ack = _camera_ack()
    ack["queued"] = queued
    return ack


def _jpeg_response(path) -> Response:
    headers = {
        "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
        "Pragma": "no-cache",
    }
    return Response(content=path.read_bytes(), media_type="image/jpeg", headers=headers)


@app.get("/camera-frame")
async def get_camera_frame() -> Response:
    """Latest ESP32 JPEG — written on every POST, before OpenCV finishes."""
    if FRAME_PATH.exists():
        return _jpeg_response(FRAME_PATH)
    return Response(status_code=404)


@app.get("/camera-overlay")
async def get_camera_overlay() -> Response:
    """Annotated OpenCV view: orange = rust, cyan = break."""
    if OVERLAY_PATH.exists():
        return _jpeg_response(OVERLAY_PATH)
    if FRAME_PATH.exists():
        return _jpeg_response(FRAME_PATH)
    return Response(status_code=404)


@app.get("/latest-verdict")
async def latest_verdict() -> Dict[str, Any]:
    async with _state_lock:
        verdict = dict(engine.last_verdict or ollama.last_verdict)
        ai = ollama.last_verdict or {}
        # Status stays with the live physics/vision engine. Gemma only supplies
        # wording, and only while that wording still matches the situation —
        # otherwise a cached note would contradict the badge next to it.
        if ai.get("source") in ("ollama", "gemma4") and ai.get("status") == verdict.get("status"):
            fresh = True
            if ai.get("analysed_at"):
                try:
                    stamp = datetime.fromisoformat(ai["analysed_at"]).replace(tzinfo=timezone.utc)
                    fresh = (datetime.now(timezone.utc) - stamp).total_seconds() < 90.0
                except ValueError:
                    fresh = False
            if fresh:
                verdict.update({k: ai[k] for k in (
                    "fault_type", "recommended_action", "reasoning", "ai_note",
                    "ai_status", "ai_agrees", "source", "analysed_at",
                ) if k in ai})
        if engine.buffer:
            latest = engine.buffer[-1]
            verdict.setdefault("zone_id", latest.zone_id)
            verdict.setdefault("timestamp", latest.timestamp)
            verdict["accel_rms"] = latest.accel_rms_30s
            verdict["health_index"] = latest.health_index
            verdict["mode"] = latest.mode
            verdict["still"] = latest.still
            verdict["acc_g"] = latest.acc_g
            verdict["grav_res"] = latest.grav_res
            verdict["gyro_mag"] = latest.gyro_mag
            verdict["acoustic_rms"] = latest.acoustic_rms
            verdict["acoustic_peak_freq"] = latest.acoustic_peak_freq
            verdict["anomaly_score"] = latest.anomaly_score
            verdict["anomaly_label"] = latest.anomaly_label
            verdict["contributions"] = latest.contributions
            verdict["reasons"] = latest.reasons
            verdict["sustained"] = latest.sustained
            verdict["forest_agrees"] = latest.forest_agrees
            age = (datetime.now(timezone.utc) - latest.received_at).total_seconds()
            verdict["node_age_s"] = round(age, 1)
            # 15 s: sensor posts every 0.5 s, but a single Wi‑Fi blip or a
            # camera upload can stall the radio for a few seconds. 5 s made
            # the CIC flicker "offline" even while the node was still up.
            verdict["node_online"] = age < 15.0
        else:
            verdict["node_online"] = False
            verdict["node_age_s"] = None
        verdict["drift_rate_pct"] = engine.trend.drift_rate_pct
        verdict["anomaly_rate_per_min"] = engine.trend.anomaly_rate_per_min
        verdict["baseline_ready"] = engine.baseline_ready
        verdict["buffer_size"] = len(engine.buffer)
        cam = engine.latest_camera or {}
        verdict["crack_score"] = cam.get("crack_score", verdict.get("crack_score", 0.0))
        verdict["crack_detected"] = cam.get("crack_detected", verdict.get("crack_detected", False))
        verdict["crack_count"] = cam.get("crack_count", verdict.get("crack_count", 0))
        verdict["rust_score"] = cam.get("rust_score", verdict.get("rust_score", 0.0))
        verdict["rust_detected"] = cam.get("rust_detected", verdict.get("rust_detected", False))
        verdict["break_detected"] = cam.get("break_detected", verdict.get("break_detected", False))
        verdict["camera"] = cam
        verdict["camera_status"] = cam.get("status", "NO_FRAME")
        verdict["camera_note"] = cam.get("note", "Waiting for the first camera frame.")
        verdict["vision_engine"] = cam.get("vision_engine", "unknown")
        verdict["ai_source"] = (ollama.last_verdict or {}).get("source", "physics_ensemble")
        verdict["ai_analysed_at"] = (ollama.last_verdict or {}).get("analysed_at")
        if engine.trend.time_to_fault_hours is not None and verdict.get("status") != "NORMAL":
            if not verdict.get("time_to_fault") or verdict.get("time_to_fault") in ("None", "null"):
                verdict["time_to_fault"] = f"{engine.trend.time_to_fault_hours:.1f} hours"
    return verdict


@app.get("/history")
async def history() -> List[Dict[str, Any]]:
    async with _state_lock:
        return engine.recent_history(config.HISTORY_RETURN)


@app.get("/health")
async def health() -> Dict[str, Any]:
    ollama_ok = await ollama.health()
    async with _state_lock:
        return {
            "status": "ok",
            "ollama": {
                "reachable": ollama_ok,
                "model": config.OLLAMA_MODEL,
                "last_call_at": ollama.last_call_at,
            },
            "anomaly_model_loaded": engine.model_loaded,
            "anomaly_model_path": str(engine.model_path),
            "buffer_size": len(engine.buffer),
            "buffer_capacity": config.BUFFER_MAX,
            "last_reading_timestamp": engine.last_timestamp(),
            "trend": {
                "drift_rate_pct": engine.trend.drift_rate_pct,
                "anomaly_rate_per_min": engine.trend.anomaly_rate_per_min,
                "time_to_fault_hours": engine.trend.time_to_fault_hours,
                "computed_at": engine.trend.computed_at,
            },
            "camera": engine.latest_camera,
            "baseline_ready": engine.baseline_ready,
            "health_index": engine.trend.health_index,
        }


@app.get("/")
async def hmi_index() -> FileResponse:
    return FileResponse(config.ROOT_DIR / "hmi" / "index.html")
