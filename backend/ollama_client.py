"""
Async Ollama client for ISMS maritime SHM analysis.

Calls POST http://localhost:11434/api/generate with stream=false so a
single JSON document comes back. Parsing is defensive: the model is
instructed to emit JSON only, but we still strip fences and fall back
to a rule-based verdict if Ollama is down.
"""

from __future__ import annotations

import base64
import json
import logging
import re
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import httpx

import config
from anomaly import SensorReading, TrendSnapshot

logger = logging.getLogger("isms.ollama")

_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def default_verdict(
    reading: Optional[SensorReading] = None,
    trend: Optional[TrendSnapshot] = None,
    reason: str = "Baseline: no Ollama analysis yet",
) -> Dict[str, Any]:
    status = "NORMAL"
    fault = "None"
    ttf: Any = "None"
    action = "Continue scheduled hull survey. No immediate intervention."
    confidence = 0.55

    if reading is not None and trend is not None:
        if trend.anomaly_rate_per_min > config.ANOMALY_RATE_URGENT or reading.acoustic_rms > 0.8:
            status = "CRITICAL"
            fault = "High anomaly rate / acoustic energy — possible cavitation or bearing failure"
            ttf = f"{config.URGENT_TTF_HOURS:.1f} hours"
            action = f"Isolate machinery in {reading.zone_id} and inspect mountings immediately."
            confidence = 0.78
        elif reading.crack_detected or reading.crack_score >= config.CRACK_WARN:
            status = "CRITICAL" if reading.crack_score >= config.CRACK_CRITICAL else "WARNING"
            fault = "Hull crack / break signature from camera AI"
            action = f"Inspect plate surface in {reading.zone_id}. Mark crack ends and photograph."
            confidence = min(0.55 + reading.crack_score * 0.4, 0.95)
            if trend.time_to_fault_hours is not None:
                ttf = f"{trend.time_to_fault_hours:.1f} hours"
        elif getattr(reading, "rust_detected", False) or getattr(reading, "rust_score", 0.0) >= config.RUST_WARN:
            status = "WARNING"
            fault = "Hull rust / corrosion patches from camera AI"
            action = f"Photograph and treat corrosion in {reading.zone_id}. Recheck surrounding welds."
            confidence = min(0.55 + float(getattr(reading, "rust_score", 0.0)) * 2.0, 0.9)
        elif trend.drift_rate_pct > config.DRIFT_FAULT_PCT or reading.anomaly_label == -1:
            status = "WARNING"
            fault = "Early vibration drift consistent with bearing wear or loosening"
            if trend.time_to_fault_hours is not None:
                ttf = f"{trend.time_to_fault_hours:.1f} hours"
            action = f"Inspect {reading.zone_id} structure within 4 hours."
            confidence = 0.7

    return {
        "status": status,
        "fault_type": fault,
        "confidence": confidence,
        "time_to_fault": ttf,
        "recommended_action": action,
        "reasoning": reason,
        "zone_id": reading.zone_id if reading else "ZONE_UNKNOWN",
        "timestamp": reading.timestamp if reading else _utcnow_iso(),
        "accel_rms": reading.accel_rms_30s if reading else 0.0,
        "drift_rate_pct": trend.drift_rate_pct if trend else 0.0,
        "anomaly_rate_per_min": trend.anomaly_rate_per_min if trend else 0.0,
        "crack_score": reading.crack_score if reading else 0.0,
        "crack_detected": reading.crack_detected if reading else False,
        "crack_count": reading.crack_count if reading else 0,
        "rust_score": getattr(reading, "rust_score", 0.0) if reading else 0.0,
        "rust_detected": bool(getattr(reading, "rust_detected", False)) if reading else False,
        "break_detected": bool(getattr(reading, "crack_detected", False)) if reading else False,
        "source": "rule_engine",
    }


def _camera_lines(camera: Optional[Dict[str, Any]]) -> str:
    """Describe what the vision stage actually measured, in plain words."""
    if not camera:
        return "- Camera: no frame yet."
    status = camera.get("status", "UNKNOWN")
    if not camera.get("usable"):
        return f"- Camera frame UNUSABLE ({status}): {camera.get('note', '')}"

    rust = camera.get("rust_detail") or {}
    brk = camera.get("break_detail") or {}
    lines = [
        f"- Camera frame quality: {status}, score {camera.get('quality', 0):.2f}",
        (
            "- Corrosion stage: CONFIRMED over multiple frames, "
            f"{float(camera.get('rust_score') or 0) * 100:.0f}% of the plate area"
            if camera.get("rust_detected")
            else f"- Corrosion stage: none ({rust.get('why', 'no corrosion colour')})"
        ),
        (
            f"- Break stage: CONFIRMED over multiple frames, {brk.get('why', '')}"
            if camera.get("break_detected")
            else f"- Break stage: none ({brk.get('why', 'no dark linear ridge')})"
        ),
    ]
    return "\n".join(lines)


def build_user_prompt(
    reading: SensorReading,
    trend: TrendSnapshot,
    camera: Optional[Dict[str, Any]] = None,
) -> str:
    ttf = "None" if trend.time_to_fault_hours is None else f"{trend.time_to_fault_hours:.2f} hours"
    return config.OLLAMA_USER_TEMPLATE.format(
        zone_id=reading.zone_id,
        accel_rms=f"{reading.accel_rms_30s:.4f}",
        avg_1min=f"{trend.avg_1min:.4f}",
        avg_10min=f"{trend.avg_10min:.4f}",
        gyro_x=f"{reading.gyro_x:.4f}",
        gyro_y=f"{reading.gyro_y:.4f}",
        gyro_z=f"{reading.gyro_z:.4f}",
        acoustic_rms=f"{reading.acoustic_rms:.4f}",
        peak_freq=f"{reading.acoustic_peak_freq:.1f}",
        crack_score=f"{reading.crack_score:.3f}",
        crack_detected=str(reading.crack_detected),
        crack_count=reading.crack_count,
        rust_score=f"{getattr(reading, 'rust_score', 0.0):.3f}",
        rust_detected=str(getattr(reading, 'rust_detected', False)),
        anomaly_score=f"{reading.anomaly_score:.4f}",
        anomaly_label=reading.anomaly_label,
        drift_rate=f"{trend.drift_rate_pct:.2f}",
        anomaly_rate=f"{trend.anomaly_rate_per_min:.2f}",
        lat=f"{reading.lat:.6f}",
        lon=f"{reading.lon:.6f}",
        timestamp=reading.timestamp,
    ) + (
        f"\n{_camera_lines(camera)}"
        f"\n- Predicted time to fault (numeric engine): {ttf}"
        f"\n- Sustained anomaly: {getattr(reading, 'sustained', False)}"
        "\n\nThe vision and physics stages above are measurements, not guesses."
        " Describe only what they show. Do not report a fault that no stage"
        " confirmed. If every stage is clear, answer NORMAL."
    )


def _extract_json(text: str) -> Dict[str, Any]:
    cleaned = text.strip()
    cleaned = re.sub(r"^```(?:json)?", "", cleaned, flags=re.IGNORECASE).strip()
    cleaned = re.sub(r"```$", "", cleaned).strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        match = _JSON_RE.search(cleaned)
        if not match:
            raise
        return json.loads(match.group(0))


def _normalise_status(value: Any) -> str:
    text = str(value or "NORMAL").strip().upper()
    if text in ("CRITICAL", "ALARM", "FAULT"):
        return "CRITICAL"
    if text in ("WARNING", "WARN", "CAUTION"):
        return "WARNING"
    return "NORMAL"


def decorate_verdict(
    parsed: Dict[str, Any],
    reading: SensorReading,
    trend: TrendSnapshot,
    source: str,
    baseline: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    ai_status = _normalise_status(parsed.get("status"))

    # Gemma 4 e2b is a 2B quantised model. It is good at describing what the
    # instruments show and unreliable at deciding severity, so the measured
    # physics/vision verdict owns `status` and the model owns the narrative.
    if baseline and baseline.get("status"):
        status = _normalise_status(baseline.get("status"))
    else:
        status = ai_status

    fault = parsed.get("fault_type")
    if fault in (None, "None", "null", ""):
        fault = "None" if status == "NORMAL" else "Unclassified structural anomaly"
    if status == "NORMAL" and ai_status != "NORMAL":
        # Model cried wolf: keep its text out of the fault line.
        fault = baseline.get("fault_type", "None") if baseline else "None"

    confidence = parsed.get("confidence", 0.5)
    try:
        confidence = float(confidence)
    except (TypeError, ValueError):
        confidence = 0.5
    confidence = max(0.0, min(1.0, confidence))

    ttf = parsed.get("time_to_fault")
    if ttf in (None, "None", "null", ""):
        if trend.time_to_fault_hours is not None and status != "NORMAL":
            ttf = f"{trend.time_to_fault_hours:.1f} hours"
        else:
            ttf = "None"
    elif isinstance(ttf, (int, float)):
        ttf = f"{float(ttf):.1f} hours"

    action = str(parsed.get("recommended_action") or "Continue monitoring.")
    reasoning = str(parsed.get("reasoning") or "")

    return {
        "status": status,
        "ai_status": ai_status,
        "ai_agrees": ai_status == status,
        "fault_type": str(fault),
        "confidence": confidence,
        "time_to_fault": str(ttf),
        "recommended_action": action,
        "reasoning": reasoning,
        "ai_note": reasoning or action,
        "zone_id": reading.zone_id,
        "timestamp": reading.timestamp,
        "accel_rms": reading.accel_rms_30s,
        "drift_rate_pct": trend.drift_rate_pct,
        "anomaly_rate_per_min": trend.anomaly_rate_per_min,
        "crack_score": reading.crack_score,
        "crack_detected": reading.crack_detected,
        "crack_count": reading.crack_count,
        "rust_score": getattr(reading, "rust_score", 0.0),
        "rust_detected": bool(getattr(reading, "rust_detected", False)),
        "break_detected": bool(reading.crack_detected),
        "source": source,
        "analysed_at": _utcnow_iso(),
    }


class OllamaClient:
    def __init__(self) -> None:
        self.last_verdict: Dict[str, Any] = default_verdict()
        self.last_call_at: Optional[str] = None
        self.available: Optional[bool] = None
        self._lock_busy = False

    async def health(self) -> bool:
        try:
            async with httpx.AsyncClient(timeout=2.0) as client:
                response = await client.get(config.OLLAMA_TAGS_URL)
            self.available = response.status_code == 200
        except Exception:  # noqa: BLE001
            self.available = False
        return bool(self.available)

    async def analyse(
        self,
        reading: SensorReading,
        trend: TrendSnapshot,
        jpeg: Optional[bytes] = None,
        baseline: Optional[Dict[str, Any]] = None,
        camera: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Chat call to Gemma 4 on the Pi. Optional hull JPEG for vision."""
        prompt = build_user_prompt(reading, trend, camera)
        user: Dict[str, Any] = {"role": "user", "content": prompt}
        if jpeg:
            user["images"] = [base64.b64encode(jpeg).decode("ascii")]
        payload = {
            "model": config.OLLAMA_MODEL,
            "stream": False,
            "think": False,
            "messages": [
                {"role": "system", "content": config.OLLAMA_SYSTEM_PROMPT},
                user,
            ],
            "options": {"temperature": 0.1, "num_predict": 80, "num_thread": 2},
            "keep_alive": "2m",
        }
        try:
            async with httpx.AsyncClient(timeout=config.OLLAMA_TIMEOUT_S) as client:
                response = await client.post(config.OLLAMA_CHAT_URL, json=payload)
            response.raise_for_status()
            body = response.json()
            text = (
                (body.get("message") or {}).get("content")
                or body.get("response")
                or ""
            )
            parsed = _extract_json(text)
            verdict = decorate_verdict(parsed, reading, trend, "gemma4", baseline)
            self.last_verdict = verdict
            self.last_call_at = _utcnow_iso()
            self.available = True
            logger.info("Ollama verdict %s (%s)", verdict["status"], verdict["fault_type"])
            return verdict
        except Exception as exc:  # noqa: BLE001
            logger.warning("Ollama call failed: %s — using rule engine", exc)
            self.available = False
            verdict = default_verdict(reading, trend, reason=f"Ollama unavailable: {exc}")
            verdict["analysed_at"] = _utcnow_iso()
            self.last_verdict = verdict
            self.last_call_at = _utcnow_iso()
            return verdict
