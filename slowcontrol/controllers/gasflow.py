"""
Gas flow path controller.

Owns the parallel pair of flow paths between the two gas-handling tees:

        ┌──────────  MKS M330B mass flow controller  ──────────┐
    ────┤                                                       ├────
        └──────────  Pneumatic bypass valve (XV4)   ──────────┘

Forward flow runs through the MFC with the bypass shut, so the flow rate is
metered and logged.  Return/recovery flow runs through the bypass with the MFC
shut, because the MFC is a metering restriction and a one-way device.

Responsibilities
────────────────
  1. Own the engineering-unit ↔ volts conversion for the MFC setpoint.  The
     PLC driver deliberately only accepts raw volts, so unit maths lives in
     exactly one place.
  2. Enforce path exclusivity — refuse to have the MFC and the bypass open at
     the same time (configurable).
  3. Provide a named path abstraction (`mks` / `bypass` / `isolated`) so an
     operator, or later a sequencer, can select a flow configuration without
     knowing which valve is which.
  4. Publish consolidated gas-flow status for the dashboard.

Deliberately NOT done here
──────────────────────────
  - Nothing actuates on service start unless `apply_default_on_start` is set.
    A slow-control restart must never move a gas valve on its own.
  - The MKS valve override contacts are commanded through a single mode
    integer written by the PLC driver.  The ladder decodes it, so this
    controller cannot assert both the pin-4 (open/purge) and pin-3 (close)
    contacts at once — which the MFC would otherwise resolve as valve OPEN.

MQTT interface
──────────────
  Subscribe (commands in):
    xsphere/commands/flow/mks/setpoint    {"value_sccm": X} | {"percent": X}
    xsphere/commands/gasflow/path         {"path": "mks"|"bypass"|"isolated"}

  NOT subscribed: .../valve/mks/mode and .../valve/bypass/state go straight to
  the PLC driver.  A direct valve command is therefore not vetted by the
  exclusivity interlock before it reaches the hardware — it is only noticed,
  and corrected, once the resulting state comes back on the status topics.

  Subscribe (state feedback in):
    xsphere/status/valve/mks              from the PLC driver
    xsphere/status/valve/bypass           from the PLC driver

  Publish (commands out to the PLC driver):
    xsphere/commands/flow/mks/setpoint_v  {"volts": X}
    xsphere/commands/valve/mks/mode       {"mode": ...}
    xsphere/commands/valve/bypass/state   {"state": 0|1}

  Publish (status):
    xsphere/status/gasflow                {"path": ..., "setpoint_sccm": ...}

  Publish (alerts):
    xsphere/alerts/gasflow_path_conflict  both legs open at once
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import Optional

from slowcontrol.controllers.base import Controller
from slowcontrol.core.mqtt import command_topic

log = logging.getLogger(__name__)

# Named flow-path configurations: (mks valve mode, bypass open?)
PATHS = {
    "mks":      ("normal", False),   # metered forward flow through the MFC
    "bypass":   ("closed", True),    # unmetered return flow around the MFC
    "isolated": ("closed", False),   # both legs shut
}

VALID_MKS_MODES = ("closed", "normal", "open")

# Both counters below are in RECONCILE PASSES, not PLC polls.  The MKS and
# bypass status topics arrive as two separate messages per PLC poll, so a pass
# happens roughly twice per poll — at the default 1 s poll interval, 4 passes
# is about 2 seconds.
#
# Debounce: the two topics arrive separately, so just after a path change we
# briefly see the new MKS mode next to the previous bypass state.  Require the
# conflict to persist rather than alerting on that transient.
CONFLICT_DEBOUNCE_PASSES = 4

# While a conflict persists, re-issue the corrective close this often rather
# than only once on the rising edge — a command that was dropped or ignored
# must not leave both legs open forever.
CONFLICT_RETRY_EVERY = 10


@dataclass
class GasFlowState:
    """Last known state, as reported back by the PLC driver."""
    mks_mode:      Optional[str]  = None
    bypass_open:   Optional[bool] = None
    setpoint_sccm: float          = 0.0
    path:          str            = "unknown"


class GasFlowController(Controller):
    NAME = "gasflow"

    def __init__(self, config, mqtt):
        super().__init__(config, mqtt)
        self._lock = threading.Lock()
        self._state = GasFlowState()
        # Valve status arrives on every PLC poll (~1 Hz). Track what we last
        # published so feedback does not turn into a steady stream of
        # identical retained messages.
        #   None = we have not decided yet, so the first reconcile publishes
        #   either way. That clears a retained conflict alert left behind by a
        #   previous run of the service.
        self._conflict_active: Optional[bool] = None
        self._conflict_passes: int = 0    # consecutive passes seeing a conflict
        self._last_status: Optional[dict] = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        cfg = self._config.gasflow
        if not cfg.enabled:
            log.info("[gasflow] disabled in config")
            return

        self._mqtt.subscribe(command_topic("flow", "mks", "setpoint"),
                             self._on_setpoint)
        self._mqtt.subscribe(command_topic("gasflow", "path"),
                             self._on_path)
        self._mqtt.subscribe("xsphere/status/valve/mks",    self._on_mks_status)
        self._mqtt.subscribe("xsphere/status/valve/bypass", self._on_bypass_status)

        if cfg.apply_default_on_start:
            path = str(cfg.default_path).strip().lower()
            if path not in PATHS:
                # Never let a config typo abort startup: this controller is
                # constructed before the interlock watchdog, so raising here
                # would take the safety watchdog down with it.
                log.error("[gasflow] default_path %r is not one of %s — "
                          "leaving valves untouched",
                          cfg.default_path, sorted(PATHS))
            else:
                log.warning("[gasflow] apply_default_on_start is set — "
                            "driving path to %r on startup", path)
                self._apply_path(path)
        else:
            log.info("[gasflow] started; valves left as-is "
                     "(apply_default_on_start = false)")

        self._publish_status()

    def stop(self) -> None:
        # Valves are intentionally left in their current position: an
        # operator-initiated service restart must not disturb a running fill.
        log.info("[gasflow] stopped (valve positions unchanged)")

    # ------------------------------------------------------------------
    # Unit conversion
    # ------------------------------------------------------------------

    def _sccm_to_percent(self, sccm: float) -> float:
        """Convert an actual-gas flow in sccm to % of the MFC's full scale."""
        mks = self._config.gasflow.mks
        gcf = mks.gas_correction_factor or 1.0
        full_scale_actual = mks.full_scale_sccm * gcf
        if full_scale_actual <= 0:
            return 0.0
        return (sccm / full_scale_actual) * 100.0

    def _percent_to_sccm(self, percent: float) -> float:
        mks = self._config.gasflow.mks
        gcf = mks.gas_correction_factor or 1.0
        return (percent / 100.0) * mks.full_scale_sccm * gcf

    # ------------------------------------------------------------------
    # Command callbacks
    # ------------------------------------------------------------------

    def _on_setpoint(self, topic: str, payload: dict) -> None:
        """Accept a setpoint in sccm or in % of full scale, clamp it, and
        forward the equivalent voltage to the PLC driver."""
        if not isinstance(payload, dict):
            return

        if "value_sccm" in payload:
            try:
                percent = self._sccm_to_percent(float(payload["value_sccm"]))
            except (TypeError, ValueError):
                log.warning("[gasflow] non-numeric value_sccm: %r",
                            payload.get("value_sccm"))
                return
        elif "percent" in payload:
            try:
                percent = float(payload["percent"])
            except (TypeError, ValueError):
                log.warning("[gasflow] non-numeric percent: %r",
                            payload.get("percent"))
                return
        else:
            log.warning("[gasflow] setpoint needs value_sccm or percent: %r",
                        payload)
            return

        mks = self._config.gasflow.mks
        limit = mks.setpoint_max_pct
        clamped = max(0.0, min(percent, limit))
        if clamped != percent:
            log.warning("[gasflow] setpoint %.2f%% FS clamped to %.2f%% "
                        "(setpoint_max_pct = %.1f)", percent, clamped, limit)

        volts = (clamped / 100.0) * mks.signal_span_v

        with self._lock:
            self._state.setpoint_sccm = self._percent_to_sccm(clamped)

        self._mqtt.publish(
            command_topic("flow", "mks", "setpoint_v"),
            {"volts": round(volts, 5)},
            qos=1,
        )
        log.info("[gasflow] MKS setpoint → %.2f%% FS (%.2f sccm, %.4f V)",
                 clamped, self._percent_to_sccm(clamped), volts)
        self._publish_status()

    def _on_path(self, topic: str, payload: dict) -> None:
        """xsphere/commands/gasflow/path → {"path": "mks"|"bypass"|"isolated"}"""
        path = payload.get("path") if isinstance(payload, dict) else payload
        if not isinstance(path, str) or path.strip().lower() not in PATHS:
            log.warning("[gasflow] unknown path %r (expected one of %s)",
                        path, sorted(PATHS))
            return
        self._apply_path(path.strip().lower())

    def _apply_path(self, path: str) -> None:
        """Drive both valves to the named configuration.

        The closing move is always issued before the opening move so the two
        legs are never both open in transit, even briefly.
        """
        target = PATHS.get(path)
        if target is None:
            log.error("[gasflow] _apply_path called with unknown path %r", path)
            return
        mks_mode, bypass_open = target

        if bypass_open:
            # Shut the MFC first, then open the bypass.
            self._set_mks_mode(mks_mode)
            self._set_bypass(True)
        else:
            # Shut the bypass first, then hand the MFC its mode.
            self._set_bypass(False)
            self._set_mks_mode(mks_mode)

        with self._lock:
            self._state.path = path
        log.info("[gasflow] path → %s (MKS %s, bypass %s)",
                 path, mks_mode, "open" if bypass_open else "closed")
        self._publish_status()

    def _set_mks_mode(self, mode: str) -> None:
        if mode not in VALID_MKS_MODES:
            log.warning("[gasflow] refusing invalid MKS mode %r", mode)
            return
        self._mqtt.publish(command_topic("valve", "mks", "mode"),
                           {"mode": mode}, qos=1)

    def _set_bypass(self, open_: bool) -> None:
        self._mqtt.publish(command_topic("valve", "bypass", "state"),
                           {"state": int(open_)}, qos=1)

    # ------------------------------------------------------------------
    # State feedback from the PLC driver
    # ------------------------------------------------------------------

    def _on_mks_status(self, topic: str, payload: dict) -> None:
        if not isinstance(payload, dict):
            return
        with self._lock:
            self._state.mks_mode = payload.get("mode")
        self._reconcile()

    def _on_bypass_status(self, topic: str, payload: dict) -> None:
        if not isinstance(payload, dict):
            return
        state = payload.get("state")
        if state is None:
            return
        with self._lock:
            self._state.bypass_open = bool(state)
        self._reconcile()

    # ------------------------------------------------------------------
    # Interlock
    # ------------------------------------------------------------------

    def _mks_is_flowing(self, mode: Optional[str]) -> bool:
        """True when the MFC valve is not held shut, i.e. gas can pass."""
        return mode in ("normal", "open")

    def _reconcile(self) -> None:
        """Fold new valve feedback into the observed path, enforce the
        exclusivity interlock, and publish.

        Called on every valve status message — the PLC driver emits two per
        poll — so the common case must be a no-op.  Alerts and status
        publishes are edge-triggered; the corrective bypass close is retried
        periodically for as long as the conflict is still real.
        """
        self._update_observed_path()

        with self._lock:
            mks_mode    = self._state.mks_mode
            bypass_open = self._state.bypass_open

        if mks_mode is not None and bypass_open is not None:
            conflicting = (self._config.gasflow.exclusive_paths
                           and self._mks_is_flowing(mks_mode)
                           and bypass_open)

            if conflicting:
                self._conflict_passes += 1
            else:
                self._conflict_passes = 0

            # Debounced so a path change in flight — new MKS mode seen next to
            # a stale bypass reading — does not raise a spurious alert.
            conflict = self._conflict_passes >= CONFLICT_DEBOUNCE_PASSES

            if conflict:
                if self._conflict_active is not True:
                    self._conflict_active = True
                    log.warning("[gasflow] PATH CONFLICT: MKS valve is %r and "
                                "the bypass is open — closing the bypass",
                                mks_mode)
                # Keep re-issuing the close for as long as the conflict is
                # real.  Acting only on the rising edge would leave both legs
                # open forever if the first command were dropped.
                since = self._conflict_passes - CONFLICT_DEBOUNCE_PASSES
                if since % CONFLICT_RETRY_EVERY == 0:
                    if since:
                        log.warning("[gasflow] bypass still open %d passes "
                                    "after the close command — retrying",
                                    since)
                    # Close the unmetered leg, not the metered one: the
                    # operator can always see and reason about flow through
                    # the MFC.
                    self._set_bypass(False)
                    self._mqtt.publish(
                        "xsphere/alerts/gasflow_path_conflict",
                        {"msg": (f"MKS valve {mks_mode} while bypass open; "
                                 f"close command issued")
                                + (f" (still open after {since} passes)"
                                   if since else ""),
                         "mks_mode": mks_mode,
                         "bypass_open": bypass_open,
                         "passes_unresolved": since},
                        qos=1, retain=True,
                    )
            elif self._conflict_active is not False:
                # Also runs on the very first clean reconcile, which clears a
                # retained alert left behind by a previous run.
                if self._conflict_active:
                    log.info("[gasflow] path conflict cleared")
                self._conflict_active = False
                self._mqtt.publish("xsphere/alerts/gasflow_path_conflict", "",
                                   qos=1, retain=True)

        self._publish_status()

    def _update_observed_path(self) -> None:
        """Derive the path name from the valve states actually reported."""
        with self._lock:
            mks_mode    = self._state.mks_mode
            bypass_open = self._state.bypass_open

            if mks_mode is None or bypass_open is None:
                self._state.path = "unknown"
            elif self._mks_is_flowing(mks_mode) and bypass_open:
                self._state.path = "conflict"
            elif self._mks_is_flowing(mks_mode):
                self._state.path = "mks"
            elif bypass_open:
                self._state.path = "bypass"
            else:
                self._state.path = "isolated"

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------

    def _publish_status(self) -> None:
        """Publish consolidated status, but only when something changed.

        This runs on every valve status message, so an unconditional publish
        would put a retained message on the broker twice a second forever.
        Live flow is deliberately not part of this payload — it moves on every
        reading and has its own sensor topic.
        """
        with self._lock:
            s = self._state
            payload = {
                "path":            s.path,
                "mks_mode":        s.mks_mode,
                "bypass_open":     s.bypass_open,
                "setpoint_sccm":   round(s.setpoint_sccm, 3),
                "setpoint_percent": round(self._sccm_to_percent(s.setpoint_sccm), 3),
                "exclusive_paths": self._config.gasflow.exclusive_paths,
            }

        if payload == self._last_status:
            return
        self._last_status = payload
        self._mqtt.publish_status("gasflow", payload=payload, retain=True)
