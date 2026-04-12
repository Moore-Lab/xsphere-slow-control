"""
Gradient scanner plugin.

Steps the gradient base temperature setpoint through a user-defined
range (start_k → end_k in increments of step_k), dwelling at each
setpoint for dwell_s seconds before moving to the next.

An optional stabilization check waits until the mean temperature of
all active PLC RTD channels is within stable_band_k of the setpoint
(or until stable_timeout_s elapses) before the dwell timer starts.

MQTT interface
──────────────
  Subscribe (commands):
    xsphere/commands/gradient_scanner/start
        {
          "start_k":          float,   # first setpoint (K)
          "end_k":            float,   # last  setpoint (K)
          "step_k":           float,   # increment (signed OK, e.g. -5)
          "dwell_s":          int,     # seconds to dwell at each step
          "stable_band_k":    float,   # optional; stability window (K)
          "stable_timeout_s": int      # optional; max wait for stability
        }
    xsphere/commands/gradient_scanner/stop   {}

  Subscribe (data):
    xsphere/sensors/temperature/#     (watches RTD/TC channels for stability check)

  Publish (status):
    xsphere/status/gradient_scanner
        {
          "state":      "idle" | "running" | "stabilizing" | "dwelling",
          "step":       int,       # current step index (0-based)
          "total_steps": int,
          "setpoint_k": float,
          "elapsed_s":  float,     # seconds since scan started
          "ok":         bool
        }
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from slowcontrol.controllers.base import Controller
from slowcontrol.core.mqtt import command_topic, status_topic

log = logging.getLogger(__name__)


@dataclass
class ScanParams:
    start_k: float
    end_k: float
    step_k: float
    dwell_s: float
    stable_band_k: float = 1.0
    stable_timeout_s: float = 300.0

    def steps(self) -> List[float]:
        """Return ordered list of setpoints to visit."""
        setpoints: List[float] = []
        sp = self.start_k
        if self.step_k == 0:
            return [sp]
        while (self.step_k > 0 and sp <= self.end_k + 1e-9) or \
              (self.step_k < 0 and sp >= self.end_k - 1e-9):
            setpoints.append(round(sp, 4))
            sp += self.step_k
        return setpoints


class GradientScannerPlugin(Controller):
    """Automated temperature gradient scan."""

    NAME = "gradient_scanner"

    def __init__(self, config, mqtt):
        super().__init__(config, mqtt)
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._scan_event = threading.Event()   # set when a scan is requested
        self._thread: Optional[threading.Thread] = None

        # Current scan state
        self._params: Optional[ScanParams] = None
        self._state: str = "idle"
        self._step_idx: int = 0
        self._total_steps: int = 0
        self._current_sp: float = 0.0
        self._scan_start: float = 0.0

        # Temperature readings for stability check (channel → latest K)
        self._temp_readings: Dict[str, float] = {}

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        self._mqtt.subscribe(
            command_topic("gradient_scanner", "start"),
            self._on_start,
        )
        self._mqtt.subscribe(
            command_topic("gradient_scanner", "stop"),
            self._on_stop,
        )
        self._mqtt.subscribe(
            "xsphere/sensors/temperature/#",
            self._on_temp,
        )

        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._scan_loop,
            name="gradient-scanner",
            daemon=True,
        )
        self._thread.start()
        log.info("[gradient_scanner] started")

    def stop(self) -> None:
        self._stop_event.set()
        self._scan_event.set()   # unblock any wait
        if self._thread:
            self._thread.join(timeout=15)
        log.info("[gradient_scanner] stopped")

    # ------------------------------------------------------------------
    # MQTT handlers
    # ------------------------------------------------------------------

    def _on_start(self, topic: str, payload: dict) -> None:
        try:
            params = ScanParams(
                start_k=float(payload["start_k"]),
                end_k=float(payload["end_k"]),
                step_k=float(payload["step_k"]),
                dwell_s=float(payload["dwell_s"]),
                stable_band_k=float(payload.get("stable_band_k", 1.0)),
                stable_timeout_s=float(payload.get("stable_timeout_s", 300.0)),
            )
        except (KeyError, ValueError) as exc:
            log.warning("[gradient_scanner] bad start payload: %s", exc)
            return

        steps = params.steps()
        if not steps:
            log.warning("[gradient_scanner] no steps generated from params")
            return

        with self._lock:
            if self._state != "idle":
                log.warning("[gradient_scanner] already running — stop first")
                return
            self._params = params
            self._total_steps = len(steps)
            self._step_idx = 0
            self._scan_start = time.monotonic()

        log.info(
            "[gradient_scanner] scan request: %.1f→%.1f K step=%.1f "
            "dwell=%.0f s (%d steps)",
            params.start_k, params.end_k, params.step_k,
            params.dwell_s, len(steps),
        )
        self._scan_event.set()

    def _on_stop(self, topic: str, payload: dict) -> None:
        log.info("[gradient_scanner] stop requested")
        with self._lock:
            self._params = None
            self._state = "idle"
        self._scan_event.set()   # wake up the loop so it can exit cleanly
        self._publish_status()

    def _on_temp(self, topic: str, payload: dict) -> None:
        channel = topic.split("/", 2)[-1]
        value_k = payload.get("value_k")
        if value_k is not None:
            with self._lock:
                self._temp_readings[channel] = float(value_k)

    # ------------------------------------------------------------------
    # Scan loop
    # ------------------------------------------------------------------

    def _scan_loop(self) -> None:
        while not self._stop_event.is_set():
            # Wait for a scan to be requested
            self._scan_event.wait(timeout=60)
            self._scan_event.clear()

            if self._stop_event.is_set():
                break

            with self._lock:
                params = self._params
                if params is None:
                    continue
                self._state = "running"

            steps = params.steps()
            log.info("[gradient_scanner] starting scan (%d steps)", len(steps))

            for idx, sp_k in enumerate(steps):
                if self._stop_event.is_set():
                    break
                with self._lock:
                    if self._params is None:   # stop was called
                        break
                    self._step_idx = idx
                    self._current_sp = sp_k

                # Command the gradient controller to move to this setpoint
                self._mqtt.publish(
                    command_topic("gradient", "base"),
                    {"value_k": sp_k},
                    qos=1,
                )
                log.info(
                    "[gradient_scanner] step %d/%d → %.2f K",
                    idx + 1, len(steps), sp_k,
                )

                # Stability wait
                if params.stable_band_k > 0:
                    with self._lock:
                        self._state = "stabilizing"
                    self._publish_status()
                    self._wait_stable(sp_k, params.stable_band_k,
                                      params.stable_timeout_s)

                # Dwell
                with self._lock:
                    if self._params is None:
                        break
                    self._state = "dwelling"
                self._publish_status()

                deadline = time.monotonic() + params.dwell_s
                while time.monotonic() < deadline:
                    if self._stop_event.is_set():
                        break
                    with self._lock:
                        if self._params is None:
                            break
                    remaining = deadline - time.monotonic()
                    self._publish_status()
                    time.sleep(min(5.0, max(0.1, remaining)))

            # Scan complete
            with self._lock:
                self._params = None
                self._state = "idle"
            log.info("[gradient_scanner] scan complete")
            self._publish_status()

    def _wait_stable(self, target_k: float, band_k: float,
                     timeout_s: float) -> None:
        """Block until all temperature channels are within band of target."""
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if self._stop_event.is_set():
                return
            with self._lock:
                if self._params is None:
                    return
                readings = dict(self._temp_readings)

            if readings:
                temps = list(readings.values())
                mean_k = sum(temps) / len(temps)
                if abs(mean_k - target_k) <= band_k:
                    log.info(
                        "[gradient_scanner] stable at %.2f K (mean=%.2f K)",
                        target_k, mean_k,
                    )
                    return
            time.sleep(5.0)

        log.warning(
            "[gradient_scanner] stability timeout for %.2f K", target_k
        )

    # ------------------------------------------------------------------
    # Status publish
    # ------------------------------------------------------------------

    def _publish_status(self) -> None:
        with self._lock:
            state     = self._state
            step      = self._step_idx
            total     = self._total_steps
            sp        = self._current_sp
            elapsed   = time.monotonic() - self._scan_start if self._scan_start else 0.0

        self._mqtt.publish_status(
            "gradient_scanner",
            payload={
                "state":        state,
                "step":         step,
                "total_steps":  total,
                "setpoint_k":   sp,
                "elapsed_s":    round(elapsed, 1),
                "ok":           state != "idle" or total == 0,
            },
            retain=True,
        )
