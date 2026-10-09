"""
Interlock / safety watchdog controller.

Monitors all sensor streams and publishes alerts when conditions fall
outside defined safe bounds. By design this controller is alert-only —
it does NOT automatically actuate valves or heaters. Automated responses
are a future addition requiring deliberate opt-in.

Built-in rules (all configurable in config.yaml — future):
  temperature_sensor_stale   : RTD/TC not updated in > 30 s → alert
  temperature_out_of_range   : any RTD reads outside [50 K, 400 K] → alert
  level_sensor_stale         : level reading not updated in > 60 s → alert
  pid_output_saturated       : heater at 100% for > 300 s → alert
  fill_while_level_unknown   : valve opened when level sensor is stale → alert
  flow_sensor_stale          : MFC flow reading not updated in > 30 s → alert
  flow_while_isolated        : gas flowing with both gas paths shut → alert

MQTT interface
──────────────
  Subscribe:
    xsphere/sensors/temperature/#
    xsphere/sensors/level/#
    xsphere/sensors/flow/#
    xsphere/status/pid/#
    xsphere/status/valve/#
    xsphere/status/gasflow

  Publish:
    xsphere/alerts/{rule}/{channel}   {"rule": ..., "value": ..., "msg": ...}
    xsphere/status/interlocks         {"rules_active": [...], "ok": bool}
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set

from slowcontrol.controllers.base import Controller

log = logging.getLogger(__name__)

# Alert thresholds
TEMP_MIN_K          = 50.0      # below this is a sensor fault
TEMP_MAX_K          = 400.0     # above this is unsafe
TEMP_STALE_S        = 30.0      # seconds before temp reading considered stale
LEVEL_STALE_S       = 60.0      # seconds before level reading considered stale
PID_SAT_THRESHOLD   = 99.0      # output_pct above which we consider saturated
PID_SAT_DURATION_S  = 300.0     # seconds saturated before alert
FLOW_STALE_S        = 30.0      # seconds before flow reading considered stale
FLOW_LEAK_PCT_FS    = 6.0       # % of full scale that counts as "flowing"
                                # when every gas path is supposed to be shut.
                                # Must clear the end-to-end analog error band:
                                # the PLC module alone is ±2% of its 10 V span,
                                # i.e. ±4% of the MFC's 5 V range, so a lower
                                # threshold latches a permanent false alarm on
                                # an uncalibrated system.  Tighten it once
                                # adc_gain/adc_offset_v have been trimmed.
FLOW_LEAK_DWELL     = 2         # consecutive watchdog passes before alerting


@dataclass
class ChannelHealth:
    last_value: float = 0.0
    last_update: float = field(default_factory=time.monotonic)
    alerted: bool = False


@dataclass
class PidHealth:
    output_pct: float = 0.0
    sat_start: Optional[float] = None   # monotonic time saturation began
    last_update: float = field(default_factory=time.monotonic)
    alerted: bool = False


class InterlocksController(Controller):
    NAME = "interlocks"

    def __init__(self, config, mqtt):
        super().__init__(config, mqtt)
        self._lock = threading.Lock()
        self._temps:  Dict[str, ChannelHealth] = {}
        self._levels: Dict[str, ChannelHealth] = {}
        self._pids:   Dict[str, PidHealth]     = {}
        self._flows:  Dict[str, ChannelHealth] = {}
        self._flow_pct: Dict[str, float]       = {}
        self._flow_leak_dwell: Dict[str, int]  = {}
        self._gas_path: Optional[str]          = None
        self._active_alerts: Set[str] = set()

        # Seed the flow channel so a device that never publishes at all is
        # reported as stale.  Without this, a dead MFC is invisible: the
        # stale check only ever looks at channels it has already heard from,
        # so interlocks would happily report ok = true.
        if config.gasflow.enabled and config.gasflow.mks.enabled:
            self._flows["mks"] = ChannelHealth()

        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        self._mqtt.subscribe("xsphere/sensors/temperature/#", self._on_temp)
        self._mqtt.subscribe("xsphere/sensors/level/#",       self._on_level)
        self._mqtt.subscribe("xsphere/sensors/flow/#",        self._on_flow)
        self._mqtt.subscribe("xsphere/status/pid/#",          self._on_pid)
        self._mqtt.subscribe("xsphere/status/gasflow",        self._on_gasflow)

        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._watchdog_loop,
            name="interlocks-watchdog",
            daemon=True,
        )
        self._thread.start()
        log.info("[interlocks] started")

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=10)
        log.info("[interlocks] stopped")

    # ------------------------------------------------------------------
    # MQTT data callbacks
    # ------------------------------------------------------------------

    def _on_temp(self, topic: str, payload: dict) -> None:
        channel = topic.split("/", 2)[-1]   # e.g. "plc/rtd/1"
        value_k = payload.get("value_k")
        if value_k is None:
            return
        with self._lock:
            h = self._temps.setdefault(channel, ChannelHealth())
            h.last_value  = float(value_k)
            h.last_update = time.monotonic()

    def _on_level(self, topic: str, payload: dict) -> None:
        vessel = topic.split("/")[-1]
        value = payload.get("filtered", payload.get("raw"))
        if value is None:
            return
        with self._lock:
            h = self._levels.setdefault(vessel, ChannelHealth())
            h.last_value  = float(value)
            h.last_update = time.monotonic()

    def _on_flow(self, topic: str, payload: dict) -> None:
        device = topic.split("/")[-1]          # e.g. "mks"
        value = payload.get("value_sccm")
        if value is None:
            return
        with self._lock:
            h = self._flows.setdefault(device, ChannelHealth())
            h.last_value  = float(value)
            h.last_update = time.monotonic()

            # An over-range sample is the MFC's documented power-up transient
            # (~7 V for a minute or two while the sensor heaters settle), not
            # a flow.  Counting it would raise "check valve seating" on every
            # power-up.  The reading still counts as fresh, so staleness is
            # tracked, but the leak check has nothing to look at.
            if payload.get("over_range"):
                self._flow_pct.pop(device, None)
                return

            pct = payload.get("percent_fs")
            if pct is not None:
                self._flow_pct[device] = float(pct)

    def _on_gasflow(self, topic: str, payload: dict) -> None:
        if not isinstance(payload, dict):
            return
        with self._lock:
            self._gas_path = payload.get("path")

    def _on_pid(self, topic: str, payload: dict) -> None:
        zone = topic.split("/")[-1]
        out  = payload.get("output_pct")
        if out is None:
            return
        now = time.monotonic()
        with self._lock:
            h = self._pids.setdefault(zone, PidHealth())
            h.output_pct  = float(out)
            h.last_update = now
            if h.output_pct >= PID_SAT_THRESHOLD:
                if h.sat_start is None:
                    h.sat_start = now
            else:
                h.sat_start = None
                if h.alerted:
                    h.alerted = False

    # ------------------------------------------------------------------
    # Watchdog loop
    # ------------------------------------------------------------------

    def _watchdog_loop(self) -> None:
        while not self._stop_event.wait(15):
            now = time.monotonic()
            active: List[str] = []

            with self._lock:
                temps  = dict(self._temps)
                levels = dict(self._levels)
                pids   = dict(self._pids)
                flows  = dict(self._flows)
                flow_pct = dict(self._flow_pct)
                gas_path = self._gas_path

            # ── Temperature checks ─────────────────────────────────────
            for channel, h in temps.items():
                age = now - h.last_update

                # Stale
                if age > TEMP_STALE_S:
                    rule = f"temperature_stale/{channel}"
                    active.append(rule)
                    if rule not in self._active_alerts:
                        self._alert(rule, h.last_value,
                                    f"No update for {age:.0f} s "
                                    f"(channel {channel})")
                        self._active_alerts.add(rule)
                else:
                    self._clear_alert(f"temperature_stale/{channel}")

                # Out-of-range
                if not (TEMP_MIN_K <= h.last_value <= TEMP_MAX_K):
                    rule = f"temperature_range/{channel}"
                    active.append(rule)
                    if rule not in self._active_alerts:
                        self._alert(rule, h.last_value,
                                    f"Temperature {h.last_value:.1f} K out of "
                                    f"range [{TEMP_MIN_K}, {TEMP_MAX_K}] K")
                        self._active_alerts.add(rule)
                else:
                    self._clear_alert(f"temperature_range/{channel}")

            # ── Level checks ───────────────────────────────────────────
            for vessel, h in levels.items():
                age = now - h.last_update
                if age > LEVEL_STALE_S:
                    rule = f"level_stale/{vessel}"
                    active.append(rule)
                    if rule not in self._active_alerts:
                        self._alert(rule, h.last_value,
                                    f"No level update for {age:.0f} s "
                                    f"({vessel})")
                        self._active_alerts.add(rule)
                else:
                    self._clear_alert(f"level_stale/{vessel}")

            # ── Gas flow checks ────────────────────────────────────────
            for device, h in flows.items():
                age = now - h.last_update
                if age > FLOW_STALE_S:
                    rule = f"flow_stale/{device}"
                    active.append(rule)
                    if rule not in self._active_alerts:
                        self._alert(rule, h.last_value,
                                    f"No flow update for {age:.0f} s "
                                    f"({device})")
                        self._active_alerts.add(rule)
                else:
                    self._clear_alert(f"flow_stale/{device}")

                # Gas moving while every path is supposed to be shut means a
                # valve did not seat, an override contact did not release, or
                # the MFC is reading offset — all worth an operator's eyes.
                # `pct` is absent while the MFC is over-range (warming up).
                pct = flow_pct.get(device)
                leaking = (gas_path == "isolated" and pct is not None
                           and pct > FLOW_LEAK_PCT_FS and age <= FLOW_STALE_S)

                # Require the condition to persist so a decaying flow signal
                # during a path change does not trip it.
                dwell = self._flow_leak_dwell.get(device, 0)
                dwell = dwell + 1 if leaking else 0
                self._flow_leak_dwell[device] = dwell

                if dwell >= FLOW_LEAK_DWELL:
                    rule = f"flow_while_isolated/{device}"
                    active.append(rule)
                    if rule not in self._active_alerts:
                        self._alert(rule, h.last_value,
                                    f"{pct:.1f}% FS flowing while gas path is "
                                    f"isolated — check valve seating")
                        self._active_alerts.add(rule)
                else:
                    self._clear_alert(f"flow_while_isolated/{device}")

            # ── PID saturation checks ──────────────────────────────────
            for zone, h in pids.items():
                if (h.sat_start is not None and
                        now - h.sat_start > PID_SAT_DURATION_S and
                        not h.alerted):
                    rule = f"pid_saturated/{zone}"
                    active.append(rule)
                    with self._lock:
                        h.alerted = True
                    self._alert(rule, h.output_pct,
                                f"PID {zone} output at {h.output_pct:.1f}% "
                                f"for {now - h.sat_start:.0f} s")
                    self._active_alerts.add(rule)

            # ── Publish consolidated status ────────────────────────────
            self._mqtt.publish_status(
                "interlocks",
                payload={"rules_active": sorted(active), "ok": len(active) == 0},
                retain=True,
            )

    # ------------------------------------------------------------------
    # Alert helpers
    # ------------------------------------------------------------------

    def _alert(self, rule: str, value: float, msg: str) -> None:
        log.warning("[interlocks] ALERT %s: %s (value=%.3f)", rule, msg, value)
        self._mqtt.publish(
            f"xsphere/alerts/{rule}",
            {"rule": rule, "value": value, "msg": msg,
             "timestamp": time.time()},
            qos=1,
            retain=True,
        )

    def _clear_alert(self, rule: str) -> None:
        if rule in self._active_alerts:
            self._active_alerts.discard(rule)
            # Publish empty retained message to clear the alert
            self._mqtt.publish(
                f"xsphere/alerts/{rule}", "", qos=1, retain=True
            )
            log.info("[interlocks] cleared alert: %s", rule)
