"""
ISMS fullscreen pygame HMI for the Raspberry Pi 5 HDMI display.

Network I/O lives on background threads so a slow / down FastAPI
process never stalls the 30 FPS render loop.
"""

from __future__ import annotations

import io
import math
import sys
import threading
import time
from collections import deque
from datetime import datetime
from typing import Any, Deque, Dict, List, Optional, Tuple

import pygame
import requests

import config

FPS = 30
TOP_H = 60
BOT_H = 40
LEFT_W = 400
RIGHT_W = 350
NAVY = (10, 22, 40)
NAVY_DEEP = (7, 16, 28)
PANEL = (18, 32, 56)
WHITE = (240, 244, 250)
MUTED = (160, 176, 196)
GREEN = (46, 204, 113)
YELLOW = (241, 196, 15)
RED = (231, 76, 60)
GREY = (130, 140, 155)
GRID = (32, 48, 72)
ACCENT = (56, 189, 248)

STATUS_COLORS = {
    "NORMAL": GREEN,
    "WARNING": YELLOW,
    "CRITICAL": RED,
}


def _lerp(current: float, target: float, alpha: float = 0.22) -> float:
    return current + (target - current) * alpha


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        v = float(value)
        if math.isfinite(v):
            return v
    except (TypeError, ValueError):
        pass
    return default


class SharedState:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.verdict: Dict[str, Any] = {
            "status": "NORMAL",
            "fault_type": "Awaiting first analysis",
            "confidence": 0.0,
            "time_to_fault": "None",
            "recommended_action": "System starting…",
            "reasoning": "",
            "zone_id": "ZONE_UNKNOWN",
            "timestamp": "",
            "accel_rms": 0.0,
            "drift_rate_pct": 0.0,
            "anomaly_rate_per_min": 0.0,
        }
        self.history: List[Dict[str, Any]] = []
        self.connected = False
        self.last_error = ""
        self.last_ollama_time = "—"
        self.buffer_size = 0
        self.anomaly_hour = 0
        self.health: Dict[str, Any] = {}

        self.disp_accel = 0.0
        self.disp_gyro = 0.0
        self.disp_acoust = 0.0
        self.disp_crack = 0.0
        self.camera_jpeg: Optional[bytes] = None

        self.chart_accel: Deque[float] = deque(maxlen=config.CHART_POINTS)
        self.chart_score: Deque[float] = deque(maxlen=config.CHART_POINTS)
        self.chart_freq: Deque[float] = deque(maxlen=config.CHART_POINTS)


STATE = SharedState()


def _latest_sample() -> Dict[str, Any]:
    if STATE.history:
        return STATE.history[-1]
    return {}


def fetch_loop() -> None:
    session = requests.Session()
    verdict_every = 1.0
    history_every = 5.0
    last_v = 0.0
    last_h = 0.0
    while True:
        now = time.time()
        try:
            if now - last_v >= verdict_every:
                last_v = now
                r = session.get(f"{config.DASHBOARD_API_URL}/latest-verdict", timeout=1.5)
                r.raise_for_status()
                data = r.json()
                with STATE.lock:
                    STATE.verdict.update(data if isinstance(data, dict) else {})
                    STATE.connected = True
                    STATE.last_error = ""
                    if data.get("analysed_at"):
                        STATE.last_ollama_time = str(data["analysed_at"])
                    elif data.get("timestamp"):
                        STATE.last_ollama_time = str(data["timestamp"])
                try:
                    img = session.get(f"{config.DASHBOARD_API_URL}/camera-frame", timeout=1.5)
                    if img.ok and img.content:
                        with STATE.lock:
                            STATE.camera_jpeg = img.content
                except Exception:
                    pass

            if now - last_h >= history_every:
                last_h = now
                r = session.get(f"{config.DASHBOARD_API_URL}/history", timeout=2.0)
                r.raise_for_status()
                hist = r.json()
                if not isinstance(hist, list):
                    hist = []
                health = {}
                try:
                    hr = session.get(f"{config.DASHBOARD_API_URL}/health", timeout=1.5)
                    if hr.ok:
                        health = hr.json()
                except Exception:
                    health = {}
                with STATE.lock:
                    STATE.history = hist[-config.CHART_POINTS :]
                    STATE.health = health if isinstance(health, dict) else {}
                    STATE.buffer_size = int(health.get("buffer_size") or len(hist))
                    STATE.anomaly_hour = sum(
                        1 for item in hist if int(item.get("anomaly_label") or 1) == -1
                    )
                    STATE.chart_accel.clear()
                    STATE.chart_score.clear()
                    STATE.chart_freq.clear()
                    for item in STATE.history:
                        STATE.chart_accel.append(_safe_float(item.get("accel_rms_30s")))
                        STATE.chart_score.append(_safe_float(item.get("anomaly_score")))
                        STATE.chart_freq.append(_safe_float(item.get("acoustic_peak_freq")))
                    STATE.connected = True
        except Exception as exc:  # noqa: BLE001
            with STATE.lock:
                STATE.connected = False
                STATE.last_error = str(exc)
        time.sleep(0.2)


def bar_color(value: float, green_max: float, yellow_max: float) -> Tuple[int, int, int]:
    if value < green_max:
        return GREEN
    if value < yellow_max:
        return YELLOW
    return RED


def draw_round_rect(surf, color, rect, radius=10) -> None:
    pygame.draw.rect(surf, color, rect, border_radius=radius)


def wrap_text(text: str, font: pygame.font.Font, width: int) -> List[str]:
    words = (text or "").split()
    if not words:
        return [""]
    lines: List[str] = []
    current = words[0]
    for word in words[1:]:
        trial = current + " " + word
        if font.size(trial)[0] <= width:
            current = trial
        else:
            lines.append(current)
            current = word
    lines.append(current)
    return lines


def draw_bar(surf, font, x, y, w, h, label, value, vmax, color, unit="") -> None:
    surf.blit(font.render(label, True, MUTED), (x, y))
    track = pygame.Rect(x, y + 22, w, 18)
    draw_round_rect(surf, NAVY_DEEP, track, 6)
    frac = 0.0 if vmax <= 0 else max(0.0, min(1.0, value / vmax))
    fill = pygame.Rect(x, y + 22, int(w * frac), 18)
    draw_round_rect(surf, color, fill, 6)
    txt = f"{value:.3f} {unit}".strip()
    surf.blit(font.render(txt, True, WHITE), (x, y + 44))


def draw_chart(
    surf,
    rect: pygame.Rect,
    title: str,
    series: List[float],
    font,
    threshold: Optional[float],
    color,
) -> None:
    draw_round_rect(surf, PANEL, rect, 10)
    surf.blit(font.render(title, True, MUTED), (rect.x + 10, rect.y + 6))
    plot = pygame.Rect(rect.x + 10, rect.y + 28, rect.w - 20, rect.h - 40)
    pygame.draw.rect(surf, NAVY_DEEP, plot, border_radius=6)
    if len(series) < 2:
        return
    ymin = min(series)
    ymax = max(series)
    if abs(ymax - ymin) < 1e-6:
        ymax = ymin + 1.0
    pad = (ymax - ymin) * 0.12
    ymin -= pad
    ymax += pad

    def y_of(v: float) -> int:
        t = (v - ymin) / (ymax - ymin)
        return int(plot.bottom - t * plot.height)

    pts = []
    n = len(series)
    for i, v in enumerate(series):
        x = plot.x + int(i * (plot.w - 1) / max(n - 1, 1))
        pts.append((x, y_of(v)))
    pygame.draw.lines(surf, color, False, pts, 2)
    if threshold is not None and ymin < threshold < ymax:
        yt = y_of(threshold)
        pygame.draw.line(surf, RED, (plot.x, yt), (plot.right, yt), 1)


def current_live() -> Dict[str, float]:
    sample = _latest_sample()
    accel = sample.get("accel") or {}
    gyro = sample.get("gyro") or {}
    ax = _safe_float(accel.get("x"))
    ay = _safe_float(accel.get("y"))
    az = _safe_float(accel.get("z"))
    gx = _safe_float(gyro.get("x"))
    gy = _safe_float(gyro.get("y"))
    gz = _safe_float(gyro.get("z"))
    mag_g = math.sqrt(ax * ax + ay * ay + az * az) / 9.80665
    gyro_mag = math.sqrt(gx * gx + gy * gy + gz * gz)
    return {
        "accel_rms": _safe_float(sample.get("accel_rms_30s"), mag_g),
        "gyro_mag": gyro_mag,
        "acoustic_rms": _safe_float(sample.get("acoustic_rms")),
        "crack_score": _safe_float(sample.get("crack_score"), _safe_float(STATE.verdict.get("crack_score"))),
        "crack_detected": bool(sample.get("crack_detected") or STATE.verdict.get("crack_detected")),
        "crack_count": int(sample.get("crack_count") or STATE.verdict.get("crack_count") or 0),
        "lat": _safe_float((sample.get("gps") or {}).get("lat")),
        "lon": _safe_float((sample.get("gps") or {}).get("lon")),
        "zone": str(sample.get("zone_id") or STATE.verdict.get("zone_id") or "ZONE_UNKNOWN"),
        "gps_valid": bool((sample.get("gps") or {}).get("valid")),
    }


def run() -> None:
    pygame.init()
    pygame.display.set_caption("ISMS — Indigenous Ship Monitoring System")
    try:
        screen = pygame.display.set_mode((0, 0), pygame.FULLSCREEN)
    except pygame.error:
        screen = pygame.display.set_mode((1280, 720))
    width, height = screen.get_size()
    clock = pygame.time.Clock()

    font_title = pygame.font.SysFont("arial", 22, bold=True)
    font_lg = pygame.font.SysFont("arial", 48, bold=True)
    font_md = pygame.font.SysFont("arial", 26, bold=True)
    font_body = pygame.font.SysFont("arial", 18)
    font_sm = pygame.font.SysFont("arial", 14)
    font_tiny = pygame.font.SysFont("arial", 12)

    worker = threading.Thread(target=fetch_loop, name="isms-fetch", daemon=True)
    worker.start()

    running = True
    while running:
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                running = False
            elif event.type == pygame.KEYDOWN and event.key in (pygame.K_ESCAPE, pygame.K_q):
                running = False

        with STATE.lock:
            verdict = dict(STATE.verdict)
            connected = STATE.connected
            live = current_live()
            accel_series = list(STATE.chart_accel)
            score_series = list(STATE.chart_score)
            freq_series = list(STATE.chart_freq)
            buffer_size = STATE.buffer_size
            last_ollama = STATE.last_ollama_time
            anom_hour = STATE.anomaly_hour
            cam_jpeg = STATE.camera_jpeg

        STATE.disp_accel = _lerp(STATE.disp_accel, live["accel_rms"])
        STATE.disp_gyro = _lerp(STATE.disp_gyro, live["gyro_mag"])
        STATE.disp_acoust = _lerp(STATE.disp_acoust, live["acoustic_rms"])
        STATE.disp_crack = _lerp(STATE.disp_crack, live["crack_score"])

        screen.fill(NAVY)

        # ---- Top bar ----
        pygame.draw.rect(screen, NAVY_DEEP, (0, 0, width, TOP_H))
        screen.blit(
            font_title.render("ISMS — Indigenous Ship Monitoring System", True, WHITE),
            (18, 18),
        )
        now_txt = datetime.now().strftime("%Y-%m-%d  %H:%M:%S")
        online = "PI ONLINE" if connected else "RECONNECTING..."
        online_color = GREEN if connected else YELLOW
        info = font_body.render(f"{now_txt}    {online}", True, WHITE)
        screen.blit(info, (width - info.get_width() - 36, 20))
        pygame.draw.circle(screen, online_color, (width - 18, 30), 8)

        # ---- Left panel ----
        left = pygame.Rect(12, TOP_H + 12, LEFT_W - 16, height - TOP_H - BOT_H - 24)
        draw_round_rect(screen, PANEL, left, 12)
        screen.blit(font_md.render("LIVE SENSOR READINGS", True, WHITE), (left.x + 16, left.y + 14))

        draw_bar(
            screen, font_body, left.x + 16, left.y + 60, left.w - 32, 70,
            "Accel RMS (0–2 g)", STATE.disp_accel, 2.0,
            bar_color(STATE.disp_accel, 0.3, 0.7), "g",
        )
        draw_bar(
            screen, font_body, left.x + 16, left.y + 150, left.w - 32, 70,
            "Gyro magnitude (dps)", STATE.disp_gyro, 50.0,
            bar_color(STATE.disp_gyro, 8.0, 20.0), "dps",
        )
        draw_bar(
            screen, font_body, left.x + 16, left.y + 240, left.w - 32, 70,
            "Acoustic RMS", STATE.disp_acoust, 1.0,
            bar_color(STATE.disp_acoust, 0.4, 0.8),
        )
        draw_bar(
            screen, font_body, left.x + 16, left.y + 330, left.w - 32, 70,
            "Camera crack AI (INT8)", STATE.disp_crack, 1.0,
            bar_color(STATE.disp_crack, 0.35, 0.7),
        )
        crack_txt = "CRACK DETECTED" if live["crack_detected"] else "No crack"
        screen.blit(
            font_body.render(f"{crack_txt}   n={live['crack_count']}", True, RED if live["crack_detected"] else MUTED),
            (left.x + 16, left.y + 410),
        )

        if cam_jpeg:
            try:
                thumb = pygame.image.load(io.BytesIO(cam_jpeg)).convert()
                tw = 150
                th = int(thumb.get_height() * tw / max(thumb.get_width(), 1))
                thumb = pygame.transform.smoothscale(thumb, (tw, min(th, 110)))
                screen.blit(thumb, (left.right - tw - 16, left.y + 430))
            except Exception:
                pass

        zone = live["zone"]
        gps_line = (
            f"{live['lat']:.4f}, {live['lon']:.4f}" if live["gps_valid"] else "NO GPS FIX"
        )
        screen.blit(font_body.render(f"Zone  {zone}", True, ACCENT), (left.x + 16, left.bottom - 56))
        screen.blit(font_body.render(f"GPS   {gps_line}", True, WHITE), (left.x + 16, left.bottom - 32))

        # ---- Right charts ----
        right_x = width - RIGHT_W
        chart_h = (height - TOP_H - BOT_H - 36) // 3
        draw_chart(
            screen,
            pygame.Rect(right_x + 4, TOP_H + 12, RIGHT_W - 16, chart_h - 8),
            "Accel RMS (5 min)",
            accel_series,
            font_sm,
            config.FAULT_RMS_THRESHOLD,
            ACCENT,
        )
        draw_chart(
            screen,
            pygame.Rect(right_x + 4, TOP_H + 16 + chart_h, RIGHT_W - 16, chart_h - 8),
            "Anomaly score",
            score_series,
            font_sm,
            0.0,
            YELLOW,
        )
        draw_chart(
            screen,
            pygame.Rect(right_x + 4, TOP_H + 20 + 2 * chart_h, RIGHT_W - 16, chart_h - 8),
            "Acoustic peak freq (Hz)",
            freq_series,
            font_sm,
            300.0,
            GREEN,
        )

        # ---- Centre status ----
        cx = LEFT_W + 8
        cw = width - LEFT_W - RIGHT_W - 16
        centre = pygame.Rect(cx, TOP_H + 12, cw, height - TOP_H - BOT_H - 24)
        draw_round_rect(screen, PANEL, centre, 12)

        status = str(verdict.get("status") or "NORMAL").upper()
        if status not in STATUS_COLORS:
            status = "NORMAL"
        badge_c = STATUS_COLORS[status]
        badge = pygame.Rect(centre.x + 24, centre.y + 28, centre.w - 48, 84)
        draw_round_rect(screen, badge_c, badge, 16)
        label = font_lg.render(status, True, NAVY_DEEP if status == "WARNING" else WHITE)
        screen.blit(label, label.get_rect(center=badge.center))

        fault = str(verdict.get("fault_type") or "None")
        screen.blit(font_md.render(fault, True, WHITE), (centre.x + 28, centre.y + 130))

        conf = _safe_float(verdict.get("confidence")) * 100.0
        screen.blit(
            font_body.render(f"Confidence  {conf:.0f}%", True, MUTED),
            (centre.x + 28, centre.y + 172),
        )

        ttf = str(verdict.get("time_to_fault") or "None")
        if ttf in ("None", "null", ""):
            ttf_txt = "No fault predicted"
        else:
            ttf_txt = f"Est. time to fault: {ttf}"
        screen.blit(font_md.render(ttf_txt, True, ACCENT), (centre.x + 28, centre.y + 210))

        action = str(verdict.get("recommended_action") or "")
        y = centre.y + 260
        screen.blit(font_body.render("Recommended action", True, MUTED), (centre.x + 28, y))
        y += 26
        for line in wrap_text(action, font_body, centre.w - 56):
            screen.blit(font_body.render(line, True, WHITE), (centre.x + 28, y))
            y += 22

        reason = str(verdict.get("reasoning") or "")
        y += 12
        for line in wrap_text(reason, font_sm, centre.w - 56):
            screen.blit(font_sm.render(line, True, GREY), (centre.x + 28, y))
            y += 18

        drift = _safe_float(verdict.get("drift_rate_pct"))
        arate = _safe_float(verdict.get("anomaly_rate_per_min"))
        meta = font_sm.render(
            f"Drift {drift:.2f}%    Anomaly rate {arate:.2f}/min    Zone {verdict.get('zone_id', '')}",
            True,
            MUTED,
        )
        screen.blit(meta, (centre.x + 28, centre.bottom - 36))

        # ---- Bottom bar ----
        pygame.draw.rect(screen, NAVY_DEEP, (0, height - BOT_H, width, BOT_H))
        bot = (
            f"Anomalies (buffer) {anom_hour}    "
            f"Last Ollama {last_ollama}    "
            f"Buffer {buffer_size}/{config.BUFFER_MAX}"
        )
        screen.blit(font_tiny.render(bot, True, MUTED), (16, height - 26))
        tag = font_tiny.render("EDGE AI POWERED — NO CLOUD REQUIRED", True, GREY)
        screen.blit(tag, (width - tag.get_width() - 16, height - 26))

        if not connected:
            banner = font_md.render("RECONNECTING...", True, YELLOW)
            screen.blit(banner, banner.get_rect(center=(width // 2, TOP_H + 10)))

        pygame.display.flip()
        clock.tick(FPS)

    pygame.quit()


if __name__ == "__main__":
    try:
        run()
    except Exception as exc:  # noqa: BLE001
        print(f"dashboard failed: {exc}", file=sys.stderr)
        sys.exit(1)
