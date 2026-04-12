"""
Gradient controller.

Abstracts the three PID zones into two high-level parameters:
  - base_k       : base temperature setpoint for the top zone (Kelvin)
  - delta_v_k    : vertical gradient  ΔT = T_bottom − T_top (Kelvin)
  - delta_l_k    : longitudinal gradient ΔT = T_nozzle − T_top (Kelvin)

In gradient mode the controller continuously recomputes each zone's
absolute setpoint and publishes it as an MQTT command, which the PLC
driver then writes via Modbus TCP.

Modes
─────
  absolute  : each zone has an independent absolute setpoint (K); no
              coupling between zones.  Dashboard exposes three sliders.
  gradient  : top zone has a base setpoint; bottom and nozzle zones are
              offset from top by delta_v_k and delta_l_k respectively.
              Dashboard exposes one base slider + two ΔT sliders.

MQTT interface
──────────────
  Subscribe (commands in):
    xsphere/commands/gradient/mode          {"mode": "absolute"|"gradient"}
    xsphere/commands/gradient/base          {"value_k": X}
    xsphere/commands/gradient/vertical      {"delta_k": X}   # bottom − top
    xsphere/commands/gradient/longitudinal  {"delta_k": X}   # nozzle − top
    xsphere/commands/pid/{zone}/setpoint    {"value_k": X}   # absolute mode

  Publish (commands out to PLC driver):
    xsphere/commands/pid/top/setpoint       {"value_k": X}
    xsphere/commands/pid/bottom/setpoint    {"value_k": X}
    xsphere/commands/pid/nozzle/setpoint    {"value_k": X}

  Publish (status):
    xsphere/status/gradient                 {"mode": ..., "base_k": ...,
                                             "delta_v_k": ..., "delta_l_k": ...,
                                             "setpoints_k": {...}}
"""

from __future__ import annotations

import logging
import threading
from typing import Dict, Optional

from slowcontrol.controllers.base import Controller
from slowcontrol.core.mqtt import command_topic, status_topic

log = logging.getLogger(__name__)

ZONES = ("top", "bottom", "nozzle")
DEFAULT_BASE_K    = 165.0
DEFAULT_DELTA_V_K = 0.0
DEFAULT_DELTA_L_K = 0.0


class GradientController(Controller):
    """High-level temperature gradient abstraction."""

    NAME = "gradient"

    def __init__(self, config, mqtt):
        super().__init__(config, mqtt)
        self._lock = threading.Lock()

        # Current mode and gradient parameters
        self._mode: str = "gradient"
        self._base_k: float = DEFAULT_BASE_K
        self._delta_v_k: float = DEFAULT_DELTA_V_K   # bottom − top
        self._delta_l_k: float = DEFAULT_DELTA_L_K   # nozzle − top

        # Absolute setpoints (used in 'absolute' mode)
        self._abs_setpoints: Dict[str, float] = {
            z: DEFAULT_BASE_K for z in ZONES
        }

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        self._mqtt.subscribe(
            command_topic("gradient", "mode"),        self._on_mode)
        self._mqtt.subscribe(
            command_topic("gradient", "base"),        self._on_base)
        self._mqtt.subscribe(
            command_topic("gradient", "vertical"),    self._on_vertical)
        self._mqtt.subscribe(
            command_topic("gradient", "longitudinal"), self._on_longitudinal)
        self._mqtt.subscribe(
            command_topic("pid", "+", "setpoint"),   self._on_abs_setpoint)
        log.info("[gradient] started (mode=%s)", self._mode)
        self._apply()

    def stop(self) -> None:
        log.info("[gradient] stopped")

    # ------------------------------------------------------------------
    # MQTT command handlers
    # ------------------------------------------------------------------

    def _on_mode(self, topic: str, payload: dict) -> None:
        mode = payload.get("mode", "gradient")
        if mode not in ("absolute", "gradient"):
            log.warning("[gradient] unknown mode: %s", mode)
            return
        with self._lock:
            self._mode = mode
        log.info("[gradient] mode → %s", mode)
        self._apply()

    def _on_base(self, topic: str, payload: dict) -> None:
        val = payload.get("value_k")
        if val is None:
            return
        with self._lock:
            self._base_k = float(val)
        log.info("[gradient] base → %.2f K", self._base_k)
        self._apply()

    def _on_vertical(self, topic: str, payload: dict) -> None:
        val = payload.get("delta_k")
        if val is None:
            return
        with self._lock:
            self._delta_v_k = float(val)
        log.info("[gradient] vertical ΔT → %.2f K", self._delta_v_k)
        self._apply()

    def _on_longitudinal(self, topic: str, payload: dict) -> None:
        val = payload.get("delta_k")
        if val is None:
            return
        with self._lock:
            self._delta_l_k = float(val)
        log.info("[gradient] longitudinal ΔT → %.2f K", self._delta_l_k)
        self._apply()

    def _on_abs_setpoint(self, topic: str, payload: dict) -> None:
        """Handle absolute per-zone setpoint in 'absolute' mode only."""
        with self._lock:
            if self._mode != "absolute":
                return
        # topic: xsphere/commands/pid/{zone}/setpoint
        parts = topic.split("/")
        zone = parts[-2]
        if zone not in ZONES:
            return
        val = payload.get("value_k")
        if val is None:
            return
        with self._lock:
            self._abs_setpoints[zone] = float(val)
        log.info("[gradient] abs setpoint %s → %.2f K", zone, float(val))
        # In absolute mode we just pass the command through to PLC driver
        # (already subscribed there); publish status only.
        self._publish_status()

    # ------------------------------------------------------------------
    # Apply computed setpoints
    # ------------------------------------------------------------------

    def _apply(self) -> None:
        """Compute and publish setpoints based on current mode/params."""
        with self._lock:
            mode       = self._mode
            base_k     = self._base_k
            delta_v_k  = self._delta_v_k
            delta_l_k  = self._delta_l_k
            abs_sp     = dict(self._abs_setpoints)

        if mode == "gradient":
            setpoints = {
                "top":    base_k,
                "bottom": base_k + delta_v_k,
                "nozzle": base_k + delta_l_k,
            }
        else:
            setpoints = abs_sp

        for zone, sp_k in setpoints.items():
            self._mqtt.publish(
                command_topic("pid", zone, "setpoint"),
                {"value_k": round(sp_k, 3)},
                qos=1,
            )

        self._publish_status(setpoints)

    def _publish_status(self, setpoints: Optional[Dict] = None) -> None:
        with self._lock:
            mode      = self._mode
            base_k    = self._base_k
            delta_v_k = self._delta_v_k
            delta_l_k = self._delta_l_k
        self._mqtt.publish_status(
            "gradient",
            payload={
                "mode":       mode,
                "base_k":     base_k,
                "delta_v_k":  delta_v_k,
                "delta_l_k":  delta_l_k,
                "setpoints_k": setpoints or {},
            },
        )
