"""
RTD calibration controller.

Subscribes to the raw LabJack RTD temperature stream and republishes a
*calibrated* stream on the ``labjack_cal`` source, with the two-point
correction from ``slowcontrol/calibration/rtd_calibration.json`` applied
in resistance-space and the IEC-60751 PT100 CVD polynomial used to
re-derive temperature.

Topic map
─────────
Input (from ``labjack_t7`` controller):
    xsphere/sensors/temperature/labjack/rtd/<channel>
    payload: {"value_k": f, "value_c": f, "resistance_ohm": f,
              "voltage_v": f, "label": s}

Output (this controller):
    xsphere/sensors/temperature/labjack_cal/rtd/<channel>
    payload: {"value_k": f, "value_c": f, "resistance_ohm": f,
              "resistance_ohm_raw": f, "voltage_v": f, "label": s,
              "gain": f, "offset_ohm": f, "calibrated": true}

The output payload is intentionally a superset of the input schema — the
existing Telegraf temperature route already picks up ``value_k``,
``value_c``, ``resistance_ohm``, and ``voltage_v`` when the topic matches
``xsphere/sensors/temperature/+/+/+`` and tags ``source=labjack_cal``, so
no Telegraf config change is required to persist the calibrated stream.
"""

from __future__ import annotations

import json
import logging
import math
import os
from typing import Any, Dict, Optional

from slowcontrol.controllers.base import Controller

log = logging.getLogger(__name__)

_KELVIN = 273.15


def _default_calibration_path() -> str:
    """``slowcontrol/calibration/rtd_calibration.json`` next to the drivers/ tree."""
    return os.path.normpath(os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "..",
        "calibration", "rtd_calibration.json"))


# ── IEC-60751 CVD polynomial ─────────────────────────────────────────────────
# Constants are also stored inside the JSON file; those are the source of truth
# at runtime, but keeping the same numbers here in code lets a smoke test run
# even if the JSON is missing.
_R0 = 100.0
_A = 3.9083e-3
_B = -5.775e-7
_C = -4.183e-12


def _cvd_r_from_t(t_c: float, r0: float = _R0,
                  a: float = _A, b: float = _B, c: float = _C) -> float:
    """PT100 forward: temperature (°C) → resistance (Ω)."""
    if t_c >= 0.0:
        return r0 * (1.0 + a * t_c + b * t_c * t_c)
    return r0 * (1.0 + a * t_c + b * t_c * t_c
                 + c * (t_c - 100.0) * t_c ** 3)


def _cvd_t_from_r(r_ohm: float, r0: float = _R0,
                  a: float = _A, b: float = _B, c: float = _C) -> float:
    """PT100 inverse: resistance (Ω) → temperature (°C).

    * R ≥ R0 (T ≥ 0 °C): analytic quadratic solution.
    * R < R0 (T < 0 °C): Newton-Raphson from a linear initial guess.

    Converges to < 1 µK within a handful of iterations across the useful
    PT100 range.  See ``extras/rtd_cvd_calibration.py`` for the vectorised
    reference implementation this scalar one mirrors.
    """
    if r_ohm >= r0:
        # B*T^2 + A*T + (1 - R/R0) = 0
        disc = a * a - 4.0 * b * (1.0 - r_ohm / r0)
        if disc < 0.0:
            # Should be unreachable in-band; guard the sqrt anyway.
            return float("nan")
        return (-a + math.sqrt(disc)) / (2.0 * b)

    t_g = (r_ohm / r0 - 1.0) / a       # linear guess
    for _ in range(30):
        f = _cvd_r_from_t(t_g, r0, a, b, c) - r_ohm
        dfdt = r0 * (a + 2.0 * b * t_g
                     + c * (4.0 * t_g ** 3 - 300.0 * t_g ** 2 + 30000.0 * t_g))
        if dfdt == 0.0:
            break
        step = f / dfdt
        t_g -= step
        if abs(step) < 1e-9:
            break
    return t_g


class RtdCalibration:
    """In-memory view of ``rtd_calibration.json``.

    Public methods are pure functions of resistance — safe to reuse from
    the backfill script and unit tests without pulling in MQTT.
    """

    def __init__(self, path: str):
        self.path = path
        with open(path, "r", encoding="utf-8") as f:
            self._doc = json.load(f)
        cvd = self._doc.get("cvd_polynomial", {})
        self.r0 = float(cvd.get("r0_ohm", _R0))
        self.a  = float(cvd.get("A_per_C", _A))
        self.b  = float(cvd.get("B_per_C2", _B))
        self.c  = float(cvd.get("C_per_C4", _C))
        # per-channel coefficients, keyed by *string* channel number ("1","2","3")
        self._rtds: Dict[str, Dict[str, float]] = {}
        for ch, entry in (self._doc.get("rtds") or {}).items():
            self._rtds[str(ch)] = {
                "gain":       float(entry["gain"]),
                "offset_ohm": float(entry["offset_ohm"]),
                "label":      entry.get("label", ""),
            }

    def channels(self):
        return list(self._rtds.keys())

    def coeffs(self, channel: str | int) -> Optional[Dict[str, float]]:
        return self._rtds.get(str(channel))

    def corrected_resistance(self, channel: str | int, r_raw_ohm: float) -> Optional[float]:
        c = self.coeffs(channel)
        if c is None:
            return None
        return c["gain"] * r_raw_ohm + c["offset_ohm"]

    def corrected_temperature_k(self, channel: str | int,
                                r_raw_ohm: float) -> Optional[float]:
        r_corr = self.corrected_resistance(channel, r_raw_ohm)
        if r_corr is None:
            return None
        t_c = _cvd_t_from_r(r_corr, self.r0, self.a, self.b, self.c)
        return t_c + _KELVIN


class CalibrationController(Controller):
    """Republish calibrated LabJack RTDs on the ``labjack_cal`` source tag.

    Runs on every input MQTT message (no polling of its own): a raw RTD
    publish from ``labjack_t7`` immediately triggers a matching calibrated
    publish here, keeping the two streams in near-lockstep at InfluxDB
    ingestion time and letting Grafana overlay them with a rolling window.
    """

    NAME = "calibration"

    def __init__(self, config, mqtt, *,
                 calibration_path: Optional[str] = None):
        super().__init__(config, mqtt)
        self._calibration_path = calibration_path or _default_calibration_path()
        self._cal: Optional[RtdCalibration] = None

    # ── Lifecycle ────────────────────────────────────────────────────────
    def start(self) -> None:
        try:
            self._cal = RtdCalibration(self._calibration_path)
        except FileNotFoundError:
            log.warning("[calibration] no calibration file at %s — controller idle",
                        self._calibration_path)
            return
        except (json.JSONDecodeError, KeyError, ValueError) as exc:
            log.error("[calibration] failed to load %s: %s",
                      self._calibration_path, exc)
            return
        log.info("[calibration] loaded coefficients for RTDs %s from %s",
                 self._cal.channels(), self._calibration_path)
        self._mqtt.subscribe(
            "xsphere/sensors/temperature/labjack/rtd/+",
            self._on_raw_rtd,
        )
        # Retained status so the GUI / a future 'about' page can see which
        # coefficients are live without having to guess by topic-sniffing.
        self._mqtt.publish_status(
            "calibration", "rtd",
            payload={
                "path": self._calibration_path,
                "cvd": {"r0": self._cal.r0, "A": self._cal.a,
                        "B": self._cal.b, "C": self._cal.c},
                "rtds": {ch: self._cal.coeffs(ch) for ch in self._cal.channels()},
            },
            retain=True,
        )

    def stop(self) -> None:
        # Subscriptions are cleared when the MQTT client disconnects during
        # shutdown; no per-controller resources to release.
        pass

    # ── MQTT ─────────────────────────────────────────────────────────────
    def _on_raw_rtd(self, topic: str, payload: Any) -> None:
        if self._cal is None or not isinstance(payload, dict):
            return
        # Topic tail is the channel number ("1", "2", "3").
        channel = topic.rsplit("/", 1)[-1]
        r_raw = payload.get("resistance_ohm")
        if r_raw is None:
            # Nothing we can meaningfully calibrate — skip silently. The raw
            # payload always includes resistance_ohm today; guard is future-
            # proofing.
            return
        try:
            r_raw = float(r_raw)
        except (TypeError, ValueError):
            return
        coeffs = self._cal.coeffs(channel)
        if coeffs is None:
            # An RTD we don't have coefficients for — ignore rather than
            # publish an uncalibrated copy under the calibrated source.
            return
        r_corr = self._cal.corrected_resistance(channel, r_raw)
        t_k = self._cal.corrected_temperature_k(channel, r_raw)
        if r_corr is None or t_k is None or math.isnan(t_k):
            return

        out = {
            "value_k":            round(t_k, 4),
            "value_c":            round(t_k - _KELVIN, 4),
            "resistance_ohm":     round(r_corr, 4),
            "resistance_ohm_raw": round(r_raw, 4),
            "gain":               coeffs["gain"],
            "offset_ohm":         coeffs["offset_ohm"],
            "calibrated":         True,
        }
        # Preserve any human-facing fields from the raw payload.
        if "label" in payload:
            out["label"] = payload["label"]
        if "voltage_v" in payload:
            out["voltage_v"] = payload["voltage_v"]

        self._mqtt.publish_sensor(
            "temperature", "labjack_cal", "rtd", str(channel),
            payload=out,
        )
