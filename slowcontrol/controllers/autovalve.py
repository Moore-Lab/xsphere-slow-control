"""
Autovalve controller.

Manages the three LN2 fill solenoid valves (XV1 ballast, XV2 primary_xe,
XV3 cryostat) using level sensor readings received over MQTT.

Responsibilities
────────────────
  1. Receive raw level readings from ESP32 level sensor boards.
  2. Apply an exponential low-pass filter (replaces PLC ladder filtering).
  3. Write filtered values back into the PLC so the PLC's own ladder logic
     can also see them (DF251 for ballast, DF252 for primary_xe; the PLC
     reads cryostat level directly from its ADC).
  4. Independently run autofill state machines for all three vessels.
  5. Expose enable/disable commands via MQTT so the dashboard can arm/disarm
     the autofill without touching the PLC directly.
  6. Enforce a fill timeout safety: if a valve has been open for longer than
     fill_timeout_s without reaching the high threshold, force it closed and
     raise an alert.

The PLC ladder logic for autofill continues to run in parallel and acts as
a hardware backup; the Python controller is the primary decision maker.

MQTT interface
──────────────
  Subscribe (level readings in):
    xsphere/sensors/level/{vessel}            {"raw": X}

  Subscribe (commands in):
    xsphere/commands/valve/{vessel}/auto_open  {"enabled": true|false}
    xsphere/commands/valve/{vessel}/auto_close {"enabled": true|false}
    xsphere/commands/valve/{vessel}/state      {"state": 0|1}

  Publish (commands out to PLC driver):
    xsphere/commands/valve/{vessel}/state      {"state": 0|1}
    xsphere/commands/valve/{vessel}/auto_open  {"enabled": true|false}
    xsphere/commands/valve/{vessel}/auto_close {"enabled": true|false}

  Publish (filtered level data):
    xsphere/sensors/level/{vessel}            {"raw": X, "filtered": Y}

  Publish (alerts):
    xsphere/alerts/fill_timeout/{vessel}      {"vessel": ..., "msg": ...}
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Dict, Optional

from slowcontrol.controllers.base import Controller
from slowcontrol.core.mqtt import command_topic, sensor_topic, status_topic

log = logging.getLogger(__name__)

VESSELS = ("cryostat", "primary_xe", "ballast")
ALPHA = 0.01   # exponential filter coefficient (matches PLC ladder α)


@dataclass
class VesselState:
    """Runtime state for one vessel."""
    level_raw:      float = 0.0
    level_filtered: float = 0.0
    filter_init:    bool  = False    # False until first reading received
    valve_open:     bool  = False
    auto_open_en:   bool  = False
    auto_close_en:  bool  = False
    fill_start_time: Optional[float] = None   # monotonic time fill began
    # Config thresholds (loaded from config)
    level_high:     float = 2.5
    level_low:      float = 0.5
    fill_timeout_s: int   = 600


class AutovalveController(Controller):
    NAME = "autovalve"

    def __init__(self, config, mqtt):
        super().__init__(config, mqtt)
        self._lock = threading.Lock()
        self._states: Dict[str, VesselState] = {}
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

        # Build state objects from config
        av_cfg = config.autovalve
        for vessel in VESSELS:
            vc = av_cfg.vessels.get(vessel)
            s = VesselState()
            if vc:
                s.level_high     = vc.level_high
                s.level_low      = vc.level_low
                s.fill_timeout_s = vc.fill_timeout_s
            self._states[vessel] = s

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        if not self._config.autovalve.enabled:
            log.info("[autovalve] disabled in config")
            return

        for vessel in VESSELS:
            self._mqtt.subscribe(
                f"xsphere/sensors/level/{vessel}",
                self._on_level,
            )
            self._mqtt.subscribe(
                command_topic("valve", vessel, "auto_open"),
                self._on_auto_cmd,
            )
            self._mqtt.subscribe(
                command_topic("valve", vessel, "auto_close"),
                self._on_auto_cmd,
            )
            self._mqtt.subscribe(
                command_topic("valve", vessel, "state"),
                self._on_manual_state,
            )

        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._watchdog_loop,
            name="autovalve-watchdog",
            daemon=True,
        )
        self._thread.start()
        log.info("[autovalve] started")

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=10)
        log.info("[autovalve] stopped")

    # ------------------------------------------------------------------
    # Level reading callback
    # ------------------------------------------------------------------

    def _on_level(self, topic: str, payload: dict) -> None:
        vessel = topic.split("/")[-1]
        raw = payload.get("raw")
        if raw is None or vessel not in self._states:
            return
        raw = float(raw)

        with self._lock:
            s = self._states[vessel]
            if not s.filter_init:
                s.level_filtered = raw
                s.filter_init = True
            else:
                s.level_filtered = ALPHA * raw + (1 - ALPHA) * s.level_filtered
            s.level_raw = raw

        # Republish with both raw and filtered values (Telegraf picks this up)
        self._mqtt.publish_sensor(
            "level", vessel,
            payload={"raw": round(raw, 4),
                     "filtered": round(s.level_filtered, 4)},
        )

        self._evaluate(vessel)

    # ------------------------------------------------------------------
    # Command callbacks
    # ------------------------------------------------------------------

    def _on_auto_cmd(self, topic: str, payload: dict) -> None:
        # topic: xsphere/commands/valve/{vessel}/auto_open|auto_close
        parts = topic.split("/")
        vessel = parts[-2]
        mode   = parts[-1]   # "auto_open" or "auto_close"
        if vessel not in self._states:
            return
        enabled = bool(payload.get("enabled", False))
        with self._lock:
            s = self._states[vessel]
            if mode == "auto_open":
                s.auto_open_en = enabled
            elif mode == "auto_close":
                s.auto_close_en = enabled
        log.info("[autovalve] %s %s → %s", vessel, mode, enabled)
        self._evaluate(vessel)

    def _on_manual_state(self, topic: str, payload: dict) -> None:
        """Track valve state when manually commanded (not from autofill)."""
        parts = topic.split("/")
        vessel = parts[-2]
        if vessel not in self._states:
            return
        state = bool(payload.get("state", 0))
        with self._lock:
            s = self._states[vessel]
            s.valve_open = state
            if state:
                s.fill_start_time = time.monotonic()
            else:
                s.fill_start_time = None

    # ------------------------------------------------------------------
    # Autofill logic
    # ------------------------------------------------------------------

    def _evaluate(self, vessel: str) -> None:
        """Re-evaluate whether to open or close the valve for this vessel."""
        with self._lock:
            s = self._states[vessel]
            filtered   = s.level_filtered
            valve_open = s.valve_open
            auto_open  = s.auto_open_en
            auto_close = s.auto_close_en
            level_high = s.level_high
            level_low  = s.level_low

        if auto_close and valve_open and filtered >= level_high:
            log.info("[autovalve] %s full (%.3f >= %.3f) → close",
                     vessel, filtered, level_high)
            self._set_valve(vessel, False)

        elif auto_open and not valve_open and filtered < level_low:
            log.info("[autovalve] %s low (%.3f < %.3f) → open",
                     vessel, filtered, level_low)
            self._set_valve(vessel, True)

    def _set_valve(self, vessel: str, open_: bool) -> None:
        with self._lock:
            s = self._states[vessel]
            s.valve_open = open_
            if open_:
                s.fill_start_time = time.monotonic()
            else:
                s.fill_start_time = None

        self._mqtt.publish(
            command_topic("valve", vessel, "state"),
            {"state": int(open_)},
            qos=1,
        )

    # ------------------------------------------------------------------
    # Watchdog loop — fill timeout safety
    # ------------------------------------------------------------------

    def _watchdog_loop(self) -> None:
        """Check for fill timeouts every 10 seconds."""
        while not self._stop_event.wait(10):
            now = time.monotonic()
            for vessel in VESSELS:
                with self._lock:
                    s = self._states[vessel]
                    if not s.valve_open or s.fill_start_time is None:
                        continue
                    elapsed = now - s.fill_start_time
                    timeout = s.fill_timeout_s

                if elapsed > timeout:
                    log.warning(
                        "[autovalve] %s fill timeout (%.0f s) — forcing close",
                        vessel, elapsed,
                    )
                    self._set_valve(vessel, False)
                    self._mqtt.publish(
                        f"xsphere/alerts/fill_timeout/{vessel}",
                        {"vessel": vessel,
                         "msg": f"Fill timeout after {elapsed:.0f} s — "
                                f"valve forced closed",
                         "elapsed_s": round(elapsed)},
                        qos=1,
                        retain=True,
                    )
