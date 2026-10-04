"""
Hull surface vision: rust (corrosion) and break (crack / weld split).

Design notes, because the previous version was wrong in an instructive way:
an INT8 linear classifier over 8 global texture features saturated to ~1.0
for *every* input, so a blank grey card scored 0.65 "crack". Global texture
statistics cannot localise a crack. This version makes three decisions
explicitly instead:

  1. Frame quality gate. A frame that is dark, blown out, out of focus or
     featureless carries no evidence, so it returns NO evidence rather than
     a number. Most false alarms were really unusable frames.
  2. Rust needs colour AND structure. Orange hue alone matches wood, skin,
     cardboard and warm light. Real corrosion is granular, patchy, and sits
     on visible bare steel, so all four are required.
  3. Break is a shape, not a texture average. Candidate dark ridges are
     segmented, then each connected component is accepted only if PCA says
     it is long, thin and higher-contrast than its surroundings.

Every fault also has to survive temporal confirmation (K of the last N
frames) before it is asserted, which removes single-frame flicker.
"""

from __future__ import annotations

import io
import logging
import math
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional, Tuple

import numpy as np
from PIL import Image, ImageFilter, ImageOps

import config

logger = logging.getLogger("isms.crack")

# Kept for train_crack_model.py, which imports these names.
MODEL_PATH = config.ROOT_DIR / "crack_model_int8.npz"
INPUT_SIZE = 96

FRAME_PATH = config.ROOT_DIR / "data" / "last_frame.jpg"
OVERLAY_PATH = config.ROOT_DIR / "data" / "last_overlay.jpg"

WORK_W, WORK_H = 320, 240

try:
    import cv2  # type: ignore

    _HAS_CV2 = True
except Exception:  # noqa: BLE001
    cv2 = None  # type: ignore
    _HAS_CV2 = False

# --- frame quality limits -------------------------------------------------
MIN_MEAN = 28.0          # below this the sensor is starved of light
MAX_MEAN = 232.0         # above this highlights are clipped
MIN_STD = 7.0            # a featureless wall carries no structural evidence
MIN_FOCUS = 8.0          # Laplacian variance; below this detail is smeared
BLOWN_FRAC = 0.40        # fraction of pixels pinned at white

# --- rust limits (tuned for live ESP32 JPEG + AWB) -------------------------
RUST_HUE_LO, RUST_HUE_HI = 4.0, 50.0     # degrees; deep red-brown through orange
RUST_SAT_LO, RUST_SAT_HI = 40, 230       # ESP32 AWB washes saturation
RUST_VAL_LO, RUST_VAL_HI = 30, 230
RUST_GRAIN_MIN = 2.5     # JPEG compression flattens mottling — keep soft
RUST_GRAIN_STRONG = 5.0
RUST_STEEL_MIN = 0.05
RUST_COVER_MAX = 0.85
# Large colour blobs can pass without strong grain (uniform rust bloom).
RUST_BLOB_NO_GRAIN_MIN = 180  # px after morphology

# --- break limits --------------------------------------------------------
# Calibrated against real frames from this node (tools/calibrate_ridge.py).
# Cables, keyboard gaps and object edges produced ridges up to 67 px long
# with a black-hat contrast of 12-30, while a real fissure in a plate reads
# 56-69. Contrast is therefore the discriminator that matters; the earlier
# threshold of 8 accepted every cable in the room.
BREAK_LEN_MIN = config.BREAK_LEN_MIN            # px on the 320-wide image
BREAK_CONTRAST_MIN = config.BREAK_CONTRAST_MIN  # black-hat depth
BREAK_ELONG_MIN = 4.0    # major/minor axis from PCA
BREAK_WIDTH_MAX = 4.5    # px; a structural crack is thin
BREAK_AREA_MIN = 12
BREAK_MAX_COMPONENTS = 60
# Many simultaneous ridges means a cluttered scene, not one fractured plate.
BREAK_MAX_ACCEPT = 4
# Large-scale luminance spread. A plate is evenly lit (10-13 on this node);
# a room scene reads 23-40. Advisory only, because a real plate under uneven
# lighting can look cluttered by this measure.
PLATE_MACRO_MAX = config.PLATE_MACRO_MAX

# --- temporal confirmation ----------------------------------------------
# Rust needs to feel live: 2 of 3 frames (~4–6 s at 2 s camera rate).
# Break stays a bit stricter — false cracks are more costly.
CONFIRM_WINDOW = 5
CONFIRM_ON = 3
CONFIRM_OFF = 3
RUST_CONFIRM_WINDOW = 3
RUST_CONFIRM_ON = 2
RUST_CONFIRM_OFF = 2
BAD_FRAMES_RESET = 3


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def _clamp(v: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return lo if v < lo else hi if v > hi else v


# --------------------------------------------------------------------------
# small image helpers
# --------------------------------------------------------------------------

def _box_mean(arr: np.ndarray, radius: int) -> np.ndarray:
    """Exact box mean via an integral image.

    Doing this through 8-bit PIL blurs would quantise the squared image and
    destroy the local variance we need, so keep it in float.
    """
    a = arr.astype(np.float64)
    h, w = a.shape
    ii = np.zeros((h + 1, w + 1), dtype=np.float64)
    ii[1:, 1:] = a.cumsum(axis=0).cumsum(axis=1)

    y0 = np.clip(np.arange(h) - radius, 0, h)
    y1 = np.clip(np.arange(h) + radius + 1, 0, h)
    x0 = np.clip(np.arange(w) - radius, 0, w)
    x1 = np.clip(np.arange(w) + radius + 1, 0, w)

    total = (
        ii[np.ix_(y1, x1)] - ii[np.ix_(y0, x1)] - ii[np.ix_(y1, x0)] + ii[np.ix_(y0, x0)]
    )
    count = ((y1 - y0)[:, None] * (x1 - x0)[None, :]).astype(np.float64)
    return total / np.maximum(count, 1.0)


def _box_stats(arr: np.ndarray, radius: int = 3) -> Tuple[np.ndarray, np.ndarray]:
    """Local mean and standard deviation over a (2r+1)² window."""
    mean = _box_mean(arr, radius)
    mean_sq = _box_mean(arr.astype(np.float64) ** 2, radius)
    var = np.clip(mean_sq - mean ** 2, 0.0, None)
    return mean.astype(np.float32), np.sqrt(var).astype(np.float32)


def _laplacian_var(gray: np.ndarray) -> float:
    """Variance of a 4-neighbour Laplacian — a standard focus measure."""
    g = gray.astype(np.float32)
    lap = (
        -4.0 * g[1:-1, 1:-1]
        + g[:-2, 1:-1]
        + g[2:, 1:-1]
        + g[1:-1, :-2]
        + g[1:-1, 2:]
    )
    return float(lap.var())


def _label(mask: np.ndarray) -> Tuple[np.ndarray, int]:
    """8-connected labelling. Uses scipy when present, else union-find."""
    try:
        from scipy import ndimage  # type: ignore

        structure = np.ones((3, 3), dtype=bool)
        lab, n = ndimage.label(mask, structure=structure)
        return lab.astype(np.int32), int(n)
    except Exception:  # noqa: BLE001
        return _label_numpy(mask)


def _label_numpy(mask: np.ndarray) -> Tuple[np.ndarray, int]:
    """Two-pass 8-connected labelling with union-find (scipy fallback)."""
    h, w = mask.shape
    labels = np.zeros((h, w), dtype=np.int32)
    parent: List[int] = [0]

    def find(a: int) -> int:
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)

    nxt = 1
    for y in range(h):
        row = mask[y]
        for x in np.nonzero(row)[0]:
            neigh = []
            if y > 0:
                for dx in (-1, 0, 1):
                    nx = x + dx
                    if 0 <= nx < w and labels[y - 1, nx]:
                        neigh.append(int(labels[y - 1, nx]))
            if x > 0 and labels[y, x - 1]:
                neigh.append(int(labels[y, x - 1]))
            if not neigh:
                labels[y, x] = nxt
                parent.append(nxt)
                nxt += 1
            else:
                m = min(neigh)
                labels[y, x] = m
                for n in neigh:
                    union(m, n)

    remap: Dict[int, int] = {}
    out = np.zeros_like(labels)
    count = 0
    for y in range(h):
        for x in np.nonzero(labels[y])[0]:
            root = find(int(labels[y, x]))
            if root not in remap:
                count += 1
                remap[root] = count
            out[y, x] = remap[root]
    return out, count


# --------------------------------------------------------------------------
# frame quality
# --------------------------------------------------------------------------

@dataclass
class FrameQuality:
    status: str
    usable: bool
    score: float
    mean: float
    std: float
    focus: float

    def as_dict(self) -> Dict[str, Any]:
        return {
            "status": self.status,
            "usable": self.usable,
            "score": round(self.score, 3),
            "brightness": round(self.mean, 1),
            "contrast": round(self.std, 1),
            "focus": round(self.focus, 1),
        }


def assess_quality(gray: np.ndarray) -> FrameQuality:
    mean = float(gray.mean())
    std = float(gray.std())
    focus = _laplacian_var(gray)
    blown = float((gray > 250).mean())

    if mean < MIN_MEAN:
        status, usable = "DARK", False
    elif mean > MAX_MEAN or blown > BLOWN_FRAC:
        status, usable = "BLOWN_OUT", False
    elif std < MIN_STD:
        status, usable = "NO_TARGET", False
    elif focus < MIN_FOCUS:
        status, usable = "BLURRED", False
    else:
        status, usable = "OK", True

    lit = 1.0 - abs(mean - 128.0) / 128.0
    score = _clamp(0.4 * lit + 0.3 * _clamp(std / 40.0) + 0.3 * _clamp(focus / 120.0))
    return FrameQuality(status, usable, score, mean, std, focus)


# --------------------------------------------------------------------------
# rust (OpenCV on the Pi; NumPy fallback if cv2 is missing)
# --------------------------------------------------------------------------

# OpenCV HSV: H is degrees/2 (0–179), S/V are 0–255.
_CV_H_LO = int(RUST_HUE_LO / 2.0)
_CV_H_HI = int(RUST_HUE_HI / 2.0)
_RUST_MIN_CONTOUR = 28  # px; drop speckles after morphology


def _sat_grain(sat_u8: np.ndarray, radius: int = 2) -> np.ndarray:
    """Local saturation std — mottling measure for corrosion vs flat paint."""
    if _HAS_CV2:
        k = 2 * radius + 1
        s = sat_u8.astype(np.float32)
        mean = cv2.blur(s, (k, k))
        mean_sq = cv2.blur(s * s, (k, k))
        return np.sqrt(np.clip(mean_sq - mean * mean, 0.0, None))
    _, grain = _box_stats(sat_u8.astype(np.float32), radius=radius)
    return grain


def _rust_decide(
    colour: np.ndarray,
    mask: np.ndarray,
    sat: np.ndarray,
    grain: np.ndarray,
    patches: int,
    engine: str,
) -> Dict[str, Any]:
    colour_frac = float(colour.mean())
    frac = float(mask.mean())
    steel_frac = float((sat < RUST_SAT_LO).mean())
    grain_in = float(grain[mask].mean()) if mask.any() else 0.0

    has_steel = steel_frac >= RUST_STEEL_MIN
    patchy = frac <= RUST_COVER_MAX
    strongly_mottled = grain_in >= RUST_GRAIN_STRONG
    # Score uses the cleaned mask; warn threshold is softer for live demos.
    warn = float(getattr(config, "RUST_WARN", 0.06))
    detected = bool(frac >= warn and patchy and (has_steel or strongly_mottled or patches >= 1))

    if detected:
        confidence = _clamp(
            0.40 * _clamp(frac / 0.30)
            + 0.35 * _clamp(grain_in / 20.0)
            + 0.25 * _clamp(max(steel_frac, 0.15) / 0.45)
        )
        confidence = max(confidence, 0.5)
    else:
        confidence = 0.0

    if not detected and not has_steel and not strongly_mottled and colour_frac > 0.55:
        why = "warm-coloured object fills the view, no bare steel — not corrosion"
    elif not patchy:
        why = "colour covers the whole frame — treated as a coloured surface"
    elif colour_frac > 0.04 and frac < warn:
        why = "orange-brown present but too small for corrosion"
    elif detected:
        why = (
            f"OpenCV: {patches} corrosion patch(es), "
            f"{frac * 100:.0f}% of plate"
            if engine == "opencv"
            else f"granular orange-brown over {frac * 100:.0f}% of the plate"
        )
    else:
        why = "no corrosion colour"

    return {
        "detected": detected,
        "score": round(frac, 4),
        "confidence": round(confidence, 3),
        "colour_frac": round(colour_frac, 4),
        "steel_frac": round(steel_frac, 4),
        "grain": round(grain_in, 2),
        "patches": int(patches),
        "engine": engine,
        "why": why,
        "mask": mask.astype(bool),
    }


def _rust_evidence_opencv(rgb: np.ndarray) -> Dict[str, Any]:
    """Fast Pi-side rust: CLAHE → dual HSV → light morphology → contours."""
    bgr = cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)
    # Equalise value so AWB / dim ESP32 frames still show brown hue.
    hsv0 = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    h, s, v = cv2.split(hsv0)
    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
    v = clahe.apply(v)
    hsv = cv2.merge([h, s, v])

    # Primary orange-brown + a little deep red (ESP32 often shifts rust red).
    lo = np.array([_CV_H_LO, RUST_SAT_LO, RUST_VAL_LO], dtype=np.uint8)
    hi = np.array([_CV_H_HI, RUST_SAT_HI, RUST_VAL_HI], dtype=np.uint8)
    colour = cv2.inRange(hsv, lo, hi)
    red_lo = np.array([0, RUST_SAT_LO, RUST_VAL_LO], dtype=np.uint8)
    red_hi = np.array([max(_CV_H_LO, 4), RUST_SAT_HI, RUST_VAL_HI], dtype=np.uint8)
    colour = cv2.bitwise_or(colour, cv2.inRange(hsv, red_lo, red_hi))

    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
    cleaned = cv2.morphologyEx(colour, cv2.MORPH_OPEN, kernel, iterations=1)
    cleaned = cv2.morphologyEx(cleaned, cv2.MORPH_CLOSE, kernel, iterations=1)

    sat = hsv[:, :, 1]
    grain = _sat_grain(sat, radius=2)
    mottled = (grain >= RUST_GRAIN_MIN).astype(np.uint8) * 255

    contours, _ = cv2.findContours(cleaned, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    mask = np.zeros(cleaned.shape, dtype=np.uint8)
    patches = 0
    for cnt in contours:
        area = cv2.contourArea(cnt)
        if area < _RUST_MIN_CONTOUR:
            continue
        # Keep large colour blobs even if grain is weak (uniform rust).
        # Small blobs still need mottling so wood/plastic speckles drop out.
        if area < RUST_BLOB_NO_GRAIN_MIN:
            tmp = np.zeros_like(mask)
            cv2.drawContours(tmp, [cnt], -1, 255, thickness=-1)
            if float(grain[tmp > 0].mean()) < RUST_GRAIN_MIN:
                continue
        patches += 1
        cv2.drawContours(mask, [cnt], -1, 255, thickness=-1)

    # Prefer mottled pixels inside kept blobs, but never erase a kept blob.
    preferred = cv2.bitwise_and(mask, mottled)
    if preferred.any():
        mask = cv2.bitwise_or(preferred, mask)

    return _rust_decide(
        colour > 0,
        mask > 0,
        sat.astype(np.float32),
        grain,
        patches,
        "opencv",
    )


def _rust_evidence_numpy(rgb: np.ndarray, gray: np.ndarray) -> Dict[str, Any]:
    """Fallback when OpenCV is not installed."""
    hsv = np.asarray(Image.fromarray(rgb, mode="RGB").convert("HSV"), dtype=np.float32)
    hue = hsv[:, :, 0] * (360.0 / 256.0)
    sat = hsv[:, :, 1]
    val = hsv[:, :, 2]
    colour = (
        (hue >= RUST_HUE_LO)
        & (hue <= RUST_HUE_HI)
        & (sat >= RUST_SAT_LO)
        & (sat <= RUST_SAT_HI)
        & (val >= RUST_VAL_LO)
        & (val <= RUST_VAL_HI)
    )
    grain = _sat_grain(sat.astype(np.uint8), radius=3)
    mask = colour & (grain >= RUST_GRAIN_MIN)
    return _rust_decide(colour, mask, sat, grain, int(mask.any()), "numpy")


def _rust_evidence(rgb: np.ndarray, gray: np.ndarray) -> Dict[str, Any]:
    if _HAS_CV2:
        return _rust_evidence_opencv(rgb)
    return _rust_evidence_numpy(rgb, gray)


# --------------------------------------------------------------------------
# break / crack
# --------------------------------------------------------------------------

def _component_axes(ys: np.ndarray, xs: np.ndarray) -> Tuple[float, float]:
    """Major and minor axis length from the pixel covariance (PCA).

    Bounding boxes are useless here: a diagonal crack has a near-square box.
    """
    if ys.size < 2:
        return 1.0, 1.0
    pts = np.stack([xs.astype(np.float64), ys.astype(np.float64)])
    pts -= pts.mean(axis=1, keepdims=True)
    cov = np.cov(pts)
    if not np.all(np.isfinite(cov)):
        return 1.0, 1.0
    eigs = np.linalg.eigvalsh(cov)
    eigs = np.clip(eigs, 0.0, None)
    major = 4.0 * math.sqrt(float(eigs[-1]))
    minor = 4.0 * math.sqrt(float(eigs[0]))
    return max(major, 1.0), max(minor, 0.5)


def _break_evidence(gray: np.ndarray) -> Dict[str, Any]:
    img = Image.fromarray(gray.astype(np.uint8), mode="L")
    smooth = img.filter(ImageFilter.GaussianBlur(0.6))
    # Morphological closing (dilate then erode) fills thin dark lines, so
    # closing - image isolates them. This is a black-hat transform.
    closed = smooth.filter(ImageFilter.MaxFilter(5)).filter(ImageFilter.MinFilter(5))
    g = np.asarray(smooth, dtype=np.float32)
    blackhat = np.clip(np.asarray(closed, dtype=np.float32) - g, 0.0, 255.0)

    local_mean, _ = _box_stats(gray, radius=4)
    thresh = max(8.0, float(blackhat.mean() + 3.0 * blackhat.std()))
    mask = (blackhat > thresh) & (g < local_mean - 6.0)

    result = {
        "detected": False,
        "score": 0.0,
        "count": 0,
        "confidence": 0.0,
        "length_px": 0.0,
        "why": "no dark linear ridge",
        "mask": np.zeros(gray.shape, dtype=bool),
    }
    if not mask.any():
        return result

    labels, n = _label(mask)
    if n == 0:
        return result

    areas = np.bincount(labels.ravel())
    order = np.argsort(areas[1:])[::-1] + 1
    accepted: List[Dict[str, float]] = []
    accepted_labs: List[int] = []
    best_reject = ""

    for lab in order[:BREAK_MAX_COMPONENTS]:
        area = int(areas[lab])
        if area < BREAK_AREA_MIN:
            break
        ys, xs = np.nonzero(labels == lab)
        major, minor = _component_axes(ys, xs)
        elong = major / minor
        width = area / major
        contrast = float(blackhat[ys, xs].mean())

        if major < BREAK_LEN_MIN:
            best_reject = best_reject or "dark marks too short to be a crack"
            continue
        if elong < BREAK_ELONG_MIN:
            best_reject = best_reject or "dark region is blob-shaped, not linear"
            continue
        if width > BREAK_WIDTH_MAX:
            best_reject = best_reject or "dark band too wide — shadow or edge"
            continue
        if contrast < BREAK_CONTRAST_MIN:
            best_reject = best_reject or "linear mark too faint"
            continue
        accepted.append(
            {"area": area, "length": major, "elong": elong, "width": width, "contrast": contrast}
        )
        accepted_labs.append(int(lab))

    if not accepted:
        result["why"] = best_reject or "no dark linear ridge"
        return result

    if len(accepted) > BREAK_MAX_ACCEPT:
        result["count"] = 0
        result["why"] = (
            f"{len(accepted)} unrelated dark lines — cluttered scene, not a fractured plate"
        )
        return result

    overlay = np.zeros(gray.shape, dtype=bool)
    for lab in accepted_labs:
        overlay |= labels == lab

    best = max(accepted, key=lambda c: c["length"] * c["contrast"])
    score = _clamp(
        0.45 * _clamp(best["length"] / 120.0)
        + 0.35 * _clamp(best["contrast"] / 40.0)
        + 0.20 * _clamp(best["elong"] / 20.0)
    )
    score = max(score, 0.35)
    return {
        "detected": True,
        "score": round(score, 4),
        "count": len(accepted),
        "confidence": round(_clamp(0.5 + 0.5 * score), 3),
        "length_px": round(best["length"], 1),
        "why": (
            f"{len(accepted)} dark ridge(s), longest {best['length']:.0f} px, "
            f"{best['width']:.1f} px wide"
        ),
        "mask": overlay,
    }


# --------------------------------------------------------------------------
# temporal confirmation
# --------------------------------------------------------------------------

class _Confirm:
    """K-of-N confirmation with hysteresis, so one frame cannot flip a fault."""

    def __init__(self, window: int = CONFIRM_WINDOW, on: int = CONFIRM_ON, off: int = CONFIRM_OFF) -> None:
        self.votes: Deque[bool] = deque(maxlen=window)
        self.on = on
        self.off = off
        self.state = False

    def push(self, hit: bool) -> bool:
        self.votes.append(bool(hit))
        hits = sum(self.votes)
        misses = len(self.votes) - hits
        if not self.state and hits >= self.on:
            self.state = True
        elif self.state and misses >= self.off:
            self.state = False
        return self.state

    def reset(self) -> None:
        self.votes.clear()
        self.state = False


@dataclass
class _Tracker:
    rust: _Confirm = field(
        default_factory=lambda: _Confirm(RUST_CONFIRM_WINDOW, RUST_CONFIRM_ON, RUST_CONFIRM_OFF)
    )
    break_: _Confirm = field(default_factory=_Confirm)
    bad_frames: int = 0

    def reset(self) -> None:
        self.rust.reset()
        self.break_.reset()
        self.bad_frames = 0


_TRACKER = _Tracker()


def reset_tracker() -> None:
    """Clear temporal state (used by tests and on stream restart)."""
    _TRACKER.reset()


def render_overlay(
    rgb: np.ndarray,
    rust_mask: Optional[np.ndarray] = None,
    break_mask: Optional[np.ndarray] = None,
    *,
    rust_label: str = "",
    break_label: str = "",
    engine: str = "opencv",
    status: str = "OK",
) -> bytes:
    """Annotate the plate with rust (orange) and break (cyan) masks for the HMI."""
    out = rgb.copy()
    if rust_mask is not None and rust_mask.any():
        tint = out[rust_mask].astype(np.float32)
        tint = tint * 0.45 + np.array([255.0, 110.0, 20.0], dtype=np.float32) * 0.55
        out[rust_mask] = np.clip(tint, 0, 255).astype(np.uint8)
    if break_mask is not None and break_mask.any():
        tint = out[break_mask].astype(np.float32)
        tint = tint * 0.35 + np.array([0.0, 230.0, 255.0], dtype=np.float32) * 0.65
        out[break_mask] = np.clip(tint, 0, 255).astype(np.uint8)

    if _HAS_CV2:
        bgr = cv2.cvtColor(out, cv2.COLOR_RGB2BGR)
        if rust_mask is not None and rust_mask.any():
            cnts, _ = cv2.findContours(
                rust_mask.astype(np.uint8) * 255, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE
            )
            cv2.drawContours(bgr, cnts, -1, (20, 110, 255), 1)
        if break_mask is not None and break_mask.any():
            cnts, _ = cv2.findContours(
                break_mask.astype(np.uint8) * 255, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE
            )
            cv2.drawContours(bgr, cnts, -1, (255, 230, 0), 2)
        banner = f"Pi CV · {engine} · {status}"
        cv2.rectangle(bgr, (0, 0), (WORK_W, 22), (10, 16, 28), -1)
        cv2.putText(bgr, banner, (6, 15), cv2.FONT_HERSHEY_SIMPLEX, 0.42, (200, 220, 240), 1, cv2.LINE_AA)
        y = 38
        if rust_label:
            cv2.putText(bgr, rust_label, (6, y), cv2.FONT_HERSHEY_SIMPLEX, 0.4, (20, 140, 255), 1, cv2.LINE_AA)
            y += 16
        if break_label:
            cv2.putText(bgr, break_label, (6, y), cv2.FONT_HERSHEY_SIMPLEX, 0.4, (255, 230, 0), 1, cv2.LINE_AA)
        ok, enc = cv2.imencode(".jpg", bgr, [int(cv2.IMWRITE_JPEG_QUALITY), 85])
        if ok:
            return bytes(enc)
        out = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)

    buf = io.BytesIO()
    Image.fromarray(out).save(buf, format="JPEG", quality=85)
    return buf.getvalue()


def _save_bytes(path: Path, data: bytes) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    except Exception as exc:  # noqa: BLE001
        logger.warning("could not save %s: %s", path, exc)


# --------------------------------------------------------------------------
# public API
# --------------------------------------------------------------------------

def _blank(status: str, quality: Optional[FrameQuality] = None, note: str = "") -> Dict[str, Any]:
    return {
        "status": status,
        "usable": False,
        "quality": round(quality.score, 3) if quality else 0.0,
        "frame": quality.as_dict() if quality else {},
        "rust_score": 0.0,
        "rust_detected": False,
        "rust_confidence": 0.0,
        "crack_score": 0.0,
        "crack_detected": False,
        "crack_count": 0,
        "break_detected": False,
        "break_confidence": 0.0,
        "vision_engine": "opencv" if _HAS_CV2 else "numpy",
        "note": note or _QUALITY_NOTES.get(status, status),
        "timestamp": _utcnow_iso(),
        "overlay_jpeg": None,
    }


_QUALITY_NOTES = {
    "DARK": "Camera sees no light — uncover the lens or add lighting.",
    "BLOWN_OUT": "Frame overexposed — reduce glare or move the lamp.",
    "NO_TARGET": "No surface detail in view — aim at a plate.",
    "BLURRED": "Image out of focus — hold the node steady.",
    "DECODE_ERROR": "Camera frame could not be decoded.",
    "EMPTY": "No camera frame received.",
}


def analyse_frame(jpeg: bytes) -> Dict[str, Any]:
    """Stateless single-frame analysis. No temporal confirmation."""
    if not jpeg:
        return _blank("EMPTY")
    try:
        im = ImageOps.exif_transpose(Image.open(io.BytesIO(jpeg)).convert("RGB"))
    except Exception as exc:  # noqa: BLE001
        logger.warning("JPEG decode failed: %s", exc)
        return _blank("DECODE_ERROR")

    im = im.resize((WORK_W, WORK_H), Image.BILINEAR)
    rgb = np.asarray(im, dtype=np.uint8)
    gray = np.asarray(im.convert("L"), dtype=np.float32)

    quality = assess_quality(gray)
    if not quality.usable:
        blank = _blank(quality.status, quality)
        blank["overlay_jpeg"] = render_overlay(
            rgb, status=quality.status, engine="opencv" if _HAS_CV2 else "numpy"
        )
        return blank

    rust = _rust_evidence(rgb, gray)
    brk = _break_evidence(gray)

    # Advisory, not a gate: tells the operator the camera is looking at a
    # scene rather than a plate, without refusing to inspect a real plate
    # that happens to be unevenly lit.
    macro = float(_box_mean(gray, 14).std())
    plate_like = macro <= PLATE_MACRO_MAX

    notes: List[str] = []
    if rust["detected"]:
        notes.append(f"Rust: {rust['why']}")
    if brk["detected"]:
        notes.append(f"Break: {brk['why']}")
    if not notes:
        notes.append(
            "Plate surface looks intact." if plate_like
            else "No hull fault. Scene looks cluttered — aim the node at a plate."
        )

    rust_mask = rust.get("mask")
    break_mask = brk.get("mask")
    overlay = render_overlay(
        rgb,
        rust_mask if rust["detected"] else None,
        break_mask if brk["detected"] else None,
        rust_label=(f"RUST {rust['score'] * 100:.0f}%" if rust["detected"] else ""),
        break_label=(f"BREAK ×{brk['count']}" if brk["detected"] else ""),
        engine=str(rust.get("engine") or ("opencv" if _HAS_CV2 else "numpy")),
        status="OK",
    )

    # Strip ndarray masks before the result leaves this module — they are only
    # for the overlay JPEG and would break JSON responses.
    rust_public = {k: v for k, v in rust.items() if k != "mask"}
    brk_public = {k: v for k, v in brk.items() if k != "mask"}

    return {
        "status": "OK",
        "usable": True,
        "quality": round(quality.score, 3),
        "frame": quality.as_dict(),
        "plate_like": plate_like,
        "macro_spread": round(macro, 1),
        "rust_score": rust["score"],
        "rust_detected": rust["detected"],
        "rust_confidence": rust["confidence"],
        "rust_detail": rust_public,
        "crack_score": brk["score"],
        "crack_detected": brk["detected"],
        "crack_count": brk["count"],
        "break_detected": brk["detected"],
        "break_confidence": brk["confidence"],
        "break_detail": brk_public,
        "vision_engine": rust.get("engine") or ("opencv" if _HAS_CV2 else "numpy"),
        "note": "  ".join(notes),
        "timestamp": _utcnow_iso(),
        "overlay_jpeg": overlay,
    }


def detect_hull(jpeg: bytes, save_frame: bool = True, confirm: bool = True) -> Dict[str, Any]:
    """Analyse one camera frame and optionally apply temporal confirmation."""
    result = analyse_frame(jpeg)
    overlay = result.pop("overlay_jpeg", None)

    if save_frame and jpeg:
        _save_bytes(FRAME_PATH, jpeg)
        if overlay:
            _save_bytes(OVERLAY_PATH, overlay)

    if not confirm:
        return result

    if not result["usable"]:
        _TRACKER.bad_frames += 1
        if _TRACKER.bad_frames >= BAD_FRAMES_RESET:
            _TRACKER.rust.reset()
            _TRACKER.break_.reset()
        result["rust_confirmed"] = _TRACKER.rust.state
        result["break_confirmed"] = _TRACKER.break_.state
        return result

    _TRACKER.bad_frames = 0
    rust_stable = _TRACKER.rust.push(result["rust_detected"])
    break_stable = _TRACKER.break_.push(result["break_detected"])

    # Only confirmed faults propagate downstream, so a single odd frame
    # cannot drive the verdict or wake the language model.
    result["rust_seen"] = result["rust_detected"]
    result["break_seen"] = result["break_detected"]
    result["rust_confirmed"] = rust_stable
    result["break_confirmed"] = break_stable
    # Keep the live score even before confirmation so the HMI bar moves fast.
    live_rust_score = float(result.get("rust_score") or 0.0)
    live_break_score = float(result.get("crack_score") or 0.0)
    result["rust_detected"] = rust_stable
    result["crack_detected"] = break_stable
    result["break_detected"] = break_stable
    result["rust_score"] = live_rust_score if (rust_stable or result["rust_seen"]) else 0.0
    result["crack_score"] = live_break_score if (break_stable or result["break_seen"]) else 0.0
    if not break_stable and not result["break_seen"]:
        result["crack_count"] = 0
    return result


def detect_cracks(jpeg: bytes, save_frame: bool = True) -> Dict[str, Any]:
    """Back-compatible entry point used by server.py."""
    return detect_hull(jpeg, save_frame)


# Retained so train_crack_model.py keeps importing cleanly. The INT8 model is
# deliberately no longer part of any decision.
def _features(gray: np.ndarray) -> np.ndarray:
    mean = float(gray.mean())
    std = float(gray.std())
    _, grain = _box_stats(gray, radius=3)
    lap = _laplacian_var(gray)
    dark = float((gray < mean - 0.45 * max(std, 1.0)).mean())
    return np.clip(
        np.array(
            [
                mean / 255.0,
                min(std / 64.0, 1.0),
                min(dark, 1.0),
                min(lap / 200.0, 1.0),
                min(float(grain.mean()) / 40.0, 1.0),
                0.0,
                0.0,
                0.0,
            ],
            dtype=np.float32,
        ),
        0.0,
        1.0,
    )
