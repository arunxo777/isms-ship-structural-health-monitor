"""
ISMS physics-informed hull anomaly engine.

Raw Isolation Forest on accel xyz is a trap: tilting the node looks like a
crack. This detector is pose-invariant and self-calibrating.

Layers (ensemble):
  1. Gravity residual   | |a|/g − 1 |
  2. Gyro energy        handling vs structural
  3. Acoustic envelope  + cavitation band 200–800 Hz
  4. 30 s vibration RMS drift (CUSUM)
  5. Online healthy baseline from the first still, calibrated samples
  6. Isolation Forest on invariant features (optional pickle)

A gyro spike while `still=false` is tagged HANDLING, not a hull fault.
"""

from __future__ import annotations

import logging
import math
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional, Sequence, Tuple

import numpy as np

import config

logger = logging.getLogger("isms.anomaly")

G = 9.80665


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _parse_ts(value: str) -> datetime:
    try:
        ts = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return ts
    except (TypeError, ValueError):
        return _utcnow()


def _finite(value: Any, default: float = 0.0, lo: float = -1e6, hi: float = 1e6) -> float:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(v) or v < lo or v > hi:
        return default
    return v


def _clamp(v: float, lo: float, hi: float) -> float:
    return lo if v < lo else hi if v > hi else v


class _Welford:
    def __init__(self) -> None:
        self.n = 0
        self.mean = 0.0
        self.m2 = 0.0

    def push(self, x: float) -> None:
        self.n += 1
        d = x - self.mean
        self.mean += d / self.n
        self.m2 += d * (x - self.mean)

    @property
    def std(self) -> float:
        if self.n < 2:
            return 0.0
        return math.sqrt(self.m2 / (self.n - 1))

    def z(self, x: float, floor: float = 1e-4) -> float:
        s = max(self.std, floor)
        return (x - self.mean) / s


@dataclass
class SensorReading:
    timestamp: str
    zone_id: str
    lat: float
    lon: float
    gps_valid: bool
    accel_x: float
    accel_y: float
    accel_z: float
    gyro_x: float
    gyro_y: float
    gyro_z: float
    acoustic_rms: float
    acoustic_peak_freq: float
    crack_score: float
    accel_rms_30s: float
    crack_detected: bool = False
    crack_count: int = 0
    rust_score: float = 0.0
    rust_detected: bool = False
    still: bool = False
    imu_calibrated: bool = False
    mic_ok: bool = False
    angle_x: float = 0.0
    angle_y: float = 0.0
    angle_z: float = 0.0
    anomaly_label: int = 1
    anomaly_score: float = 0.0
    health_index: float = 100.0
    sustained: bool = False
    forest_agrees: bool = False
    forest_score: float = 0.0
    mode: str = "CAL"
    reasons: List[str] = field(default_factory=list)
    contributions: Dict[str, float] = field(default_factory=dict)
    received_at: datetime = field(default_factory=_utcnow)

    @property
    def acc_mag(self) -> float:
        return math.sqrt(self.accel_x ** 2 + self.accel_y ** 2 + self.accel_z ** 2)

    @property
    def acc_g(self) -> float:
        return self.acc_mag / G

    @property
    def grav_res(self) -> float:
        return abs(self.acc_g - 1.0)

    @property
    def gyro_mag(self) -> float:
        return math.sqrt(self.gyro_x ** 2 + self.gyro_y ** 2 + self.gyro_z ** 2)

    def invariant_vector(self) -> np.ndarray:
        """Pose-invariant 8-D vector for Isolation Forest."""
        cav = 1.0 if 200.0 <= self.acoustic_peak_freq <= 800.0 else 0.0
        return np.array(
            [[
                self.grav_res,
                min(self.gyro_mag, 80.0) / 80.0,
                min(self.acoustic_rms, 1.5),
                min(self.acoustic_peak_freq, 4000.0) / 4000.0,
                min(self.accel_rms_30s, 2.0),
                self.crack_score,
                cav,
                1.0 if self.still else 0.0,
            ]],
            dtype=np.float64,
        )

    def feature_vector(self) -> np.ndarray:
        return self.invariant_vector()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "timestamp": self.timestamp,
            "zone_id": self.zone_id,
            "gps": {"lat": self.lat, "lon": self.lon, "valid": self.gps_valid},
            "accel": {"x": self.accel_x, "y": self.accel_y, "z": self.accel_z},
            "gyro": {"x": self.gyro_x, "y": self.gyro_y, "z": self.gyro_z},
            "angle": {"x": self.angle_x, "y": self.angle_y, "z": self.angle_z},
            "acoustic_rms": self.acoustic_rms,
            "acoustic_peak_freq": self.acoustic_peak_freq,
            "crack_score": self.crack_score,
            "crack_detected": self.crack_detected,
            "crack_count": self.crack_count,
            "rust_score": self.rust_score,
            "rust_detected": self.rust_detected,
            "accel_rms_30s": self.accel_rms_30s,
            "still": self.still,
            "imu_calibrated": self.imu_calibrated,
            "mic_ok": self.mic_ok,
            "acc_g": round(self.acc_g, 4),
            "grav_res": round(self.grav_res, 4),
            "gyro_mag": round(self.gyro_mag, 3),
            "anomaly_label": self.anomaly_label,
            "anomaly_score": self.anomaly_score,
            "health_index": self.health_index,
            "mode": self.mode,
            "reasons": list(self.reasons),
            "contributions": dict(self.contributions),
        }


@dataclass
class TrendSnapshot:
    avg_1min: float = 0.0
    avg_5min: float = 0.0
    avg_10min: float = 0.0
    drift_rate_pct: float = 0.0
    anomaly_rate_per_min: float = 0.0
    acoustic_peak_shift_hz: float = 0.0
    mean_crack_score: float = 0.0
    crack_rate_per_min: float = 0.0
    time_to_fault_hours: Optional[float] = None
    increasing: bool = False
    computed_at: Optional[str] = None
    health_index: float = 100.0
    cusum: float = 0.0


class AnomalyEngine:
    """Self-calibrating physics residual + optional Isolation Forest."""

    BASELINE_N = 40
    CONFIRM_RUN = 3       # consecutive anomalous samples before escalating
    CLEAR_RUN = 3         # consecutive healthy samples before standing down
    MIN_RATE_SAMPLES = 20   # below this the anomaly rate is meaningless
    MIN_RATE_SPAN_S = 30.0  # and so is a rate measured over a few seconds

    def __init__(self, model_path: Path = config.ANOMALY_MODEL_PATH) -> None:
        self.model_path = Path(model_path)
        self.model = None
        self.model_loaded = False
        self.buffer: Deque[SensorReading] = deque(maxlen=config.BUFFER_MAX)
        self.score_history: Deque[Tuple[datetime, int, float]] = deque(
            maxlen=config.ANOMALY_HISTORY_MAX
        )
        self.trend = TrendSnapshot()
        self.latest_camera: Dict[str, Any] = {
            "crack_score": 0.0,
            "crack_detected": False,
            "crack_count": 0,
            "timestamp": None,
        }
        self.baseline_ready = False
        self._base_acoust = _Welford()
        self._base_rms = _Welford()
        self._base_grav = _Welford()
        self._base_gyro = _Welford()
        self._cusum = 0.0
        self._last_acc_g = 1.0
        self._anom_run = 0
        self._calm_run = 0
        self._last_ollama_at = 0.0
        self._last_ollama_sig = ""
        self.last_verdict: Dict[str, Any] = {}
        self._load_model()

    def _load_model(self) -> None:
        try:
            import joblib

            self.model = joblib.load(self.model_path)
            probe = np.zeros((1, 8), dtype=np.float64)
            _ = self.model.predict(probe)
            _ = self.model.decision_function(probe)
            self.model_loaded = True
            logger.info("Loaded Isolation Forest from %s", self.model_path)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Could not load %s (%s). Physics residual detector is primary.",
                self.model_path,
                exc,
            )
            self.model = None
            self.model_loaded = False

    def update_camera(self, result: Dict[str, Any]) -> None:
        self.latest_camera = {
            "crack_score": float(result.get("crack_score") or 0.0),
            "crack_detected": bool(result.get("crack_detected")),
            "crack_count": int(result.get("crack_count") or 0),
            "rust_score": float(result.get("rust_score") or 0.0),
            "rust_detected": bool(result.get("rust_detected")),
            "break_detected": bool(result.get("break_detected")),
            "rust_confidence": float(result.get("rust_confidence") or 0.0),
            "break_confidence": float(result.get("break_confidence") or 0.0),
            "rust_seen": bool(result.get("rust_seen") or result.get("rust_detected")),
            "break_seen": bool(result.get("break_seen") or result.get("break_detected")),
            "status": result.get("status", "UNKNOWN"),
            "usable": bool(result.get("usable")),
            "quality": float(result.get("quality") or 0.0),
            "vision_engine": result.get("vision_engine", "unknown"),
            "frame": result.get("frame") or {},
            "note": result.get("note", ""),
            "timestamp": result.get("timestamp"),
        }

    def ingest(self, reading: SensorReading) -> Tuple[int, float]:
        cam = self.latest_camera or {}
        reading.crack_score = max(reading.crack_score, float(cam.get("crack_score") or 0.0))
        reading.crack_detected = bool(reading.crack_detected or cam.get("crack_detected"))
        reading.crack_count = max(reading.crack_count, int(cam.get("crack_count") or 0))
        reading.rust_score = max(getattr(reading, "rust_score", 0.0), float(cam.get("rust_score") or 0.0))
        reading.rust_detected = bool(getattr(reading, "rust_detected", False) or cam.get("rust_detected"))

        if not reading.imu_calibrated:
            reading.mode = "CAL"
        elif reading.still:
            reading.mode = "STILL"
        else:
            reading.mode = "MOVE"

        self._learn_baseline(reading)
        physics_label, physics_score, contrib, reasons, health = self._physics_detect(reading)

        forest_label, forest_score = 1, 0.35
        if self.model_loaded and self.model is not None:
            try:
                vec = reading.invariant_vector()
                vec = np.nan_to_num(vec, nan=0.0, posinf=0.0, neginf=0.0)
                forest_label = int(self.model.predict(vec)[0])
                forest_score = float(self.model.decision_function(vec)[0])
            except Exception as exc:  # noqa: BLE001
                logger.error("Model inference failed: %s", exc)

        handling = reading.mode == "MOVE" and reading.gyro_mag > 12.0
        if handling:
            reasons = [r for r in reasons if any(k in r.lower() for k in ("handling", "acoustic", "crack", "rust", "break"))]
            if not reasons:
                reasons = ["Node handling — gyro/pose change, not a hull event"]
            if reading.acoustic_rms < 0.20 and not reading.crack_detected and not reading.rust_detected:
                physics_label = 1
                health = max(health, 78.0)

        # The Isolation Forest was fitted on synthetic data, so it only
        # corroborates the physics detector — it never triggers on its own.
        forest_agrees = self.model_loaded and forest_label == -1
        if forest_agrees and physics_label == -1:
            reasons.append("Isolation Forest agrees this sample is out of envelope")

        label = -1 if physics_label == -1 else 1

        # Camera faults are already K-of-N confirmed in crack_detect, so a
        # single odd frame cannot reach this point.
        if reading.crack_detected:
            label = -1
            reasons.append("Camera confirms a linear break in the plate")
        if reading.rust_detected:
            label = -1
            reasons.append("Camera confirms corrosion on the plate")

        score = physics_score if math.isfinite(physics_score) else 0.0

        # Debounce: a hull fault must persist, otherwise one noisy sample
        # would flip the whole CIC to CRITICAL and back.
        if label == -1:
            self._anom_run += 1
            self._calm_run = 0
        else:
            self._calm_run += 1
            if self._calm_run >= self.CLEAR_RUN:
                self._anom_run = 0
        reading.sustained = self._anom_run >= self.CONFIRM_RUN
        reading.forest_agrees = bool(forest_agrees)
        reading.forest_score = float(forest_score) if math.isfinite(forest_score) else 0.0

        reading.anomaly_label = label
        reading.anomaly_score = float(score)
        reading.health_index = float(_clamp(health, 0.0, 100.0))
        reading.reasons = reasons[:6]
        reading.contributions = contrib
        self.buffer.append(reading)
        self.score_history.append((_utcnow(), label, float(score)))
        self.trend.health_index = reading.health_index
        self.last_verdict = self.build_verdict(reading, self.trend)
        return label, float(score)

    def _learn_baseline(self, reading: SensorReading) -> None:
        if self.baseline_ready:
            return
        if not (reading.imu_calibrated and reading.still):
            return
        self._base_acoust.push(reading.acoustic_rms)
        self._base_rms.push(reading.accel_rms_30s)
        self._base_grav.push(reading.grav_res)
        self._base_gyro.push(reading.gyro_mag)
        if self._base_acoust.n >= self.BASELINE_N:
            self.baseline_ready = True
            logger.info(
                "Healthy baseline locked  n=%d  acoust=%.3f±%.3f  grav=%.3f  gyro=%.2f",
                self._base_acoust.n,
                self._base_acoust.mean,
                self._base_acoust.std,
                self._base_grav.mean,
                self._base_gyro.mean,
            )

    def _physics_detect(
        self, reading: SensorReading
    ) -> Tuple[int, float, Dict[str, float], List[str], float]:
        reasons: List[str] = []
        grav = reading.grav_res
        gyro = reading.gyro_mag
        ac = reading.acoustic_rms
        pk = reading.acoustic_peak_freq
        rms = reading.accel_rms_30s
        jerk = abs(reading.acc_g - self._last_acc_g)
        self._last_acc_g = reading.acc_g

        # CUSUM only after a still-hull baseline exists. Raw 30 s RMS on this
        # node sits near 0.1 g even when healthy, so do not trip on that.
        if self.baseline_ready and self._base_rms.n > 8:
            mu = self._base_rms.mean
            k = max(0.04, 2.0 * self._base_rms.std)
            self._cusum = max(0.0, self._cusum + (rms - mu - k))
            if self._cusum > 1.5:
                self._cusum *= 0.8
        else:
            self._cusum = 0.0

        z_ac = self._base_acoust.z(ac, 0.01) if self._base_acoust.n > 8 else ac / 0.05
        z_rms = self._base_rms.z(rms, 0.02) if self._base_rms.n > 8 else rms / 0.08

        cav = 200.0 <= pk <= 800.0 and ac > 0.12
        grav_hit = reading.still and grav > 0.12
        gyro_struct = reading.still and gyro > 8.0
        acoust_hit = ac > 0.18 or z_ac > 4.0
        drift_hit = self._cusum > 0.45 or z_rms > 3.5
        loosening = jerk > 0.25 and ac > 0.10 and reading.still

        contrib = {
            "gravity_residual": _clamp(grav * 6.0, 0.0, 1.0),
            "gyro_energy": _clamp(gyro / 40.0, 0.0, 1.0),
            "acoustic": _clamp(ac / 0.35, 0.0, 1.0),
            "cavitation_band": 1.0 if cav else _clamp(ac * 2.0, 0.0, 0.4),
            "vibration_drift": _clamp(self._cusum / 0.6, 0.0, 1.0),
            "crack": _clamp(reading.crack_score, 0.0, 1.0),
            "rust": _clamp(getattr(reading, "rust_score", 0.0) * 4.0, 0.0, 1.0),
            "handling": 1.0 if (not reading.still and gyro > 12.0) else 0.0,
        }

        if not reading.still and gyro > 12.0:
            reasons.append("Handling transient (pick-up / tilt)")
        if grav_hit:
            reasons.append(f"Gravity residual {grav:.3f} g while still — mount/fatigue shift")
        if gyro_struct:
            reasons.append(f"Gyro {gyro:.1f} dps on a still node — possible loosening")
        if acoust_hit:
            if cav:
                reasons.append(f"Acoustic cavitation band {pk:.0f} Hz  rms={ac:.3f}")
            else:
                reasons.append(f"Acoustic energy rms={ac:.3f} above healthy hull")
        if drift_hit:
            reasons.append(f"Vibration CUSUM {self._cusum:.2f} — rising RMS trend")
        if loosening:
            reasons.append("Intermittent jerk + acoustics — structural loosening signature")
        if reading.crack_score >= config.CRACK_WARN:
            reasons.append(f"Crack/break score {reading.crack_score:.2f}")
        if getattr(reading, "rust_detected", False) or getattr(reading, "rust_score", 0.0) >= config.RUST_WARN:
            reasons.append(f"Rust/corrosion area {reading.rust_score:.2f}")

        structural = sum(
            [
                1.4 * contrib["gravity_residual"] if reading.still else 0.2 * contrib["gravity_residual"],
                1.1 * contrib["acoustic"],
                1.0 * contrib["vibration_drift"],
                1.3 * contrib["crack"],
                1.1 * contrib.get("rust", 0.0),
                0.9 * contrib["cavitation_band"],
                0.8 * contrib["gyro_energy"] if reading.still else 0.15 * contrib["gyro_energy"],
            ]
        )
        health = _clamp(100.0 - structural * 22.0, 0.0, 100.0)

        label = -1 if structural >= 1.35 or health < 62.0 else 1
        # Isolation-Forest-style: higher score = healthier.
        score = (health - 50.0) / 80.0
        self.trend.cusum = self._cusum
        return label, float(score), contrib, reasons, health

    def build_verdict(self, reading: SensorReading, trend: TrendSnapshot) -> Dict[str, Any]:
        health = reading.health_index
        handling = any("Handling" in r for r in reading.reasons)
        if not reading.imu_calibrated:
            status, fault = "NORMAL", "IMU calibrating — locking healthy hull baseline"
            action = "Hold the node still for 3 seconds."
            confidence = 0.4
        elif handling and reading.anomaly_label == 1:
            status, fault = "NORMAL", "Handling — ignored for hull diagnosis"
            action = "Set the node down to resume structural watch."
            confidence = 0.72
        elif reading.sustained and (health < 45.0 or reading.acoustic_rms > 0.28):
            status = "CRITICAL"
            fault = reading.reasons[0] if reading.reasons else "Critical hull energy"
            action = f"Isolate machinery near {reading.zone_id}. Inspect mounts and plate."
            confidence = 0.9 if reading.forest_agrees else 0.84
        elif reading.sustained:
            status = "WARNING"
            fault = reading.reasons[0] if reading.reasons else "Early structural deviation"
            action = f"Inspect {reading.zone_id} within 4 hours. Log RMS and acoustic peak."
            confidence = 0.8 if reading.forest_agrees else 0.74
        elif reading.anomaly_label == -1:
            # Seen once but not yet sustained: report it without escalating.
            status, fault = "NORMAL", "Transient deviation — confirming"
            action = "No action yet. Holding for confirmation."
            confidence = 0.6
        else:
            status, fault = "NORMAL", "None"
            action = "Continue scheduled hull survey. Baseline holding."
            confidence = 0.8 if self.baseline_ready else 0.55

        ttf: Any = "None"
        if trend.time_to_fault_hours is not None and status != "NORMAL":
            ttf = f"{trend.time_to_fault_hours:.1f} hours"

        return {
            "status": status,
            "fault_type": fault,
            "confidence": confidence,
            "time_to_fault": ttf,
            "recommended_action": action,
            "reasoning": "; ".join(reading.reasons) or "Physics residual inside healthy envelope.",
            "zone_id": reading.zone_id,
            "timestamp": reading.timestamp,
            "accel_rms": reading.accel_rms_30s,
            "drift_rate_pct": trend.drift_rate_pct,
            "anomaly_rate_per_min": trend.anomaly_rate_per_min,
            "crack_score": reading.crack_score,
            "crack_detected": reading.crack_detected,
            "crack_count": reading.crack_count,
            "rust_score": reading.rust_score,
            "rust_detected": reading.rust_detected,
            "break_detected": reading.crack_detected,
            "sustained": reading.sustained,
            "forest_agrees": reading.forest_agrees,
            "health_index": health,
            "mode": reading.mode,
            "still": reading.still,
            "imu_calibrated": reading.imu_calibrated,
            "mic_ok": reading.mic_ok,
            "acc_g": reading.acc_g,
            "grav_res": reading.grav_res,
            "gyro_mag": reading.gyro_mag,
            "acoustic_rms": reading.acoustic_rms,
            "acoustic_peak_freq": reading.acoustic_peak_freq,
            "anomaly_score": reading.anomaly_score,
            "anomaly_label": reading.anomaly_label,
            "contributions": reading.contributions,
            "reasons": reading.reasons,
            "baseline_ready": self.baseline_ready,
            "cusum": trend.cusum,
            "source": "physics_ensemble",
        }

    def _window(self, seconds: float) -> List[SensorReading]:
        if not self.buffer:
            return []
        cutoff = _utcnow().timestamp() - seconds
        out: List[SensorReading] = []
        for item in reversed(self.buffer):
            ts = item.received_at.timestamp()
            if ts < cutoff:
                break
            out.append(item)
        out.reverse()
        return out

    def compute_trends(self) -> TrendSnapshot:
        now = _utcnow()
        w1 = self._window(60)
        w5 = self._window(5 * 60)
        w10 = self._window(10 * 60)

        def mean_rms(items: Sequence[SensorReading]) -> float:
            if not items:
                return 0.0
            return float(np.mean([r.accel_rms_30s for r in items]))

        avg_1 = mean_rms(w1)
        avg_5 = mean_rms(w5)
        avg_10 = mean_rms(w10)

        # Both windows need real content, else a stalled node reads as -100%
        # drift simply because the 1-minute window has emptied out.
        if avg_10 > 1e-9 and len(w1) >= 5 and avg_1 > 1e-9:
            drift = (avg_1 - avg_10) / avg_10 * 100.0
        else:
            drift = 0.0
        increasing = drift > 0.0

        anomalies_5 = sum(1 for r in w5 if r.anomaly_label == -1)
        # Divide by real elapsed time, not sample count. With a nearly empty
        # buffer the old form reported >100 anomalies/min on a healthy hull.
        span_s = (
            (w5[-1].received_at - w5[0].received_at).total_seconds() if len(w5) >= 2 else 0.0
        )
        minutes_5 = max(span_s / 60.0, 1e-6)
        if len(w5) >= self.MIN_RATE_SAMPLES and span_s >= self.MIN_RATE_SPAN_S:
            anomaly_rate = anomalies_5 / minutes_5
        else:
            anomaly_rate = 0.0

        if len(w10) >= 4:
            half = max(len(w10) // 4, 1)
            early = float(np.mean([r.acoustic_peak_freq for r in w10[:half]]))
            late = float(np.mean([r.acoustic_peak_freq for r in w10[-half:]]))
            peak_shift = late - early
        else:
            peak_shift = 0.0

        if len(w10) >= 2:
            mean_crack = float(np.mean([r.crack_score for r in w10]))
        else:
            mean_crack = float(w1[-1].crack_score) if w1 else 0.0
        cracks_5 = sum(1 for r in w5 if r.crack_detected)
        crack_rate = cracks_5 / minutes_5 if w5 else 0.0

        ttf: Optional[float] = None
        current = avg_1 if avg_1 > 0 else (w1[-1].accel_rms_30s if w1 else 0.0)
        health = w1[-1].health_index if w1 else 100.0

        if anomaly_rate > config.ANOMALY_RATE_URGENT or mean_crack > config.CRACK_CRITICAL or health < 40:
            ttf = config.URGENT_TTF_HOURS
        elif drift > config.DRIFT_FAULT_PCT and increasing:
            delta_rms = avg_1 - avg_10
            hours = 10.0 / 60.0
            rate_per_hour = delta_rms / hours if hours > 0 else 0.0
            if rate_per_hour > 1e-6:
                remaining = config.FAULT_RMS_THRESHOLD - current
                ttf = max(remaining / rate_per_hour, 0.05)
                if current >= config.FAULT_RMS_THRESHOLD:
                    ttf = 0.1

        self.trend = TrendSnapshot(
            avg_1min=float(avg_1),
            avg_5min=float(avg_5),
            avg_10min=float(avg_10),
            drift_rate_pct=float(drift),
            anomaly_rate_per_min=float(anomaly_rate),
            acoustic_peak_shift_hz=float(peak_shift),
            mean_crack_score=float(mean_crack),
            crack_rate_per_min=float(crack_rate),
            time_to_fault_hours=None if ttf is None else float(ttf),
            increasing=increasing,
            computed_at=now.strftime("%Y-%m-%dT%H:%M:%S"),
            health_index=float(health),
            cusum=self._cusum,
        )
        return self.trend

    def condition_signature(self) -> str:
        """Coarse fingerprint of the current situation.

        Used to avoid re-asking the language model the same question: it is
        only worth a call when the situation is new, or after a cooldown.
        """
        cam = self.latest_camera or {}
        latest = self.buffer[-1] if self.buffer else None
        parts = [
            "rust" if cam.get("rust_detected") else "-",
            "break" if cam.get("break_detected") else "-",
            "sus" if (latest and latest.sustained and latest.mode != "MOVE") else "-",
        ]
        return "|".join(parts)

    def note_ollama_call(self) -> None:
        """Stamp the cooldown clock when a Gemma job is actually launched."""
        self._last_ollama_sig = self.condition_signature()
        self._last_ollama_at = _utcnow().timestamp()

    def should_call_ollama(self, label: int) -> bool:
        """True only for a confirmed fault that is new *and* past the cooldown.

        The cooldown is measured from the last *launch*, not from now-minus-
        interval after a 30 s inference — otherwise Gemma restarts itself
        the instant it finishes and pegs the Pi at 250 % CPU.
        """
        cam = self.latest_camera or {}
        latest = self.buffer[-1] if self.buffer else None
        handling = bool(latest and latest.mode == "MOVE")

        interesting = bool(
            cam.get("rust_detected")
            or cam.get("break_detected")
            or (
                latest is not None
                and latest.sustained
                and not handling
                and (
                    self.trend.drift_rate_pct > config.DRIFT_WARN_PCT
                    or self.trend.anomaly_rate_per_min > config.ANOMALY_RATE_WARN
                )
            )
        )
        if not interesting:
            return False

        now = _utcnow().timestamp()
        if (now - self._last_ollama_at) < config.OLLAMA_MIN_INTERVAL_S:
            return False
        if self.condition_signature() == self._last_ollama_sig:
            return False
        return True

    def recent_history(self, limit: int = config.HISTORY_RETURN) -> List[Dict[str, Any]]:
        items = list(self.buffer)[-limit:]
        return [item.to_dict() for item in items]

    def anomaly_count_last_hour(self) -> int:
        cutoff = _utcnow().timestamp() - 3600
        return sum(1 for r in self.buffer if r.anomaly_label == -1 and r.received_at.timestamp() >= cutoff)

    def last_timestamp(self) -> Optional[str]:
        if not self.buffer:
            return None
        return self.buffer[-1].timestamp


def reading_from_payload(payload: Dict[str, Any]) -> SensorReading:
    gps = payload.get("gps") or {}
    accel = payload.get("accel") or {}
    gyro = payload.get("gyro") or {}
    ang = payload.get("angle") or {}
    return SensorReading(
        timestamp=str(payload.get("timestamp") or _utcnow().strftime("%Y-%m-%dT%H:%M:%S")),
        zone_id=str(payload.get("zone_id") or "ZONE_UNKNOWN")[:16],
        lat=_finite(gps.get("lat"), 0.0, -90.0, 90.0),
        lon=_finite(gps.get("lon"), 0.0, -180.0, 180.0),
        gps_valid=bool(gps.get("valid", False)),
        accel_x=_finite(accel.get("x"), 0.0, -40.0, 40.0),
        accel_y=_finite(accel.get("y"), 0.0, -40.0, 40.0),
        accel_z=_finite(accel.get("z"), 9.81, -40.0, 40.0),
        gyro_x=_finite(gyro.get("x"), 0.0, -512.0, 512.0),
        gyro_y=_finite(gyro.get("y"), 0.0, -512.0, 512.0),
        gyro_z=_finite(gyro.get("z"), 0.0, -512.0, 512.0),
        acoustic_rms=_finite(payload.get("acoustic_rms"), 0.0, 0.0, 5.0),
        acoustic_peak_freq=_finite(payload.get("acoustic_peak_freq"), 0.0, 0.0, 20000.0),
        crack_score=_finite(payload.get("crack_score"), 0.0, 0.0, 1.0),
        crack_detected=bool(payload.get("crack_detected", False)),
        crack_count=int(payload.get("crack_count") or 0),
        accel_rms_30s=_finite(payload.get("accel_rms_30s"), 0.0, 0.0, 20.0),
        still=bool(payload.get("still", False)),
        imu_calibrated=bool(payload.get("imu_calibrated", True)),
        mic_ok=bool(payload.get("mic_ok", False)),
        angle_x=_finite(ang.get("x"), 0.0, -180.0, 180.0),
        angle_y=_finite(ang.get("y"), 0.0, -180.0, 180.0),
        angle_z=_finite(ang.get("z"), 0.0, -180.0, 180.0),
        received_at=_utcnow(),
    )
