#!/usr/bin/env python3
"""
Cryostat LN2 fill panel — temperature-gated manual XV3 open.

Gives the operator one button that sets the XV3 desired state (DS1006) to 1,
but only while a chosen pair of temperature sensors has converged to within a
chosen threshold:

    signed   (default):   Ti - Tj  < X
    absolute:            |Ti - Tj| < X

The gate is the same idea the automatic coast controller uses (see
`slowcontrol/controllers/autovalve.py`), reduced to a single operator action:
"the cold reserve looks spent, fill it now".  Defaults for the sensor pair,
the comparison mode and the threshold are read from the cryostat `coast:`
block in config.yaml, so the button and the automation agree out of the box.

The panel never speaks Modbus.  It publishes the ordinary valve command

    xsphere/commands/valve/cryostat/state   {"state": 1}

that `slowcontrol.drivers.plc` already accepts, so the running service stays
the only writer on the PLC connection.

Below the XV3 controls the panel also carries plain open / close buttons for
the three gas-side solenoid valves (ballast, pump, bottle).  Those are not
gated on anything: each button publishes the same kind of valve command, which
the driver writes to DS151 / DS152 / DS153, and the ladder SETs or RSTs Y105 /
Y106 / Y107 from that register.
"""

from __future__ import annotations

import logging
import os
import threading
import time
import tkinter as tk
from dataclasses import dataclass
from tkinter import messagebox, ttk
from typing import Dict, Optional, Tuple

log = logging.getLogger(__name__)

# A reading older than this is treated as absent rather than current.  The PLC
# driver republishes temperatures every poll, so anything this stale means the
# service or the broker has stopped, and a gate decided on a frozen number is
# worse than no decision at all.
MAX_AGE_S = 15.0

# GUI refresh period.  Fast enough that the readouts feel live, slow enough
# that it costs nothing.
REFRESH_MS = 500

# Human labels for the temperature channels, keyed by the MQTT sub-path under
# xsphere/sensors/temperature/ — i.e. the values of plc.RTD_MQTT_PATH joined
# with "/", plus the Omega logger channels.  Keep in step with
# core.config.COAST_SENSOR_CHANNELS.
SENSOR_LABELS = {
    "plc/rtd/1": "DF1  Xe cube top",
    "plc/rtd/2": "DF2  Xe cube bottom",
    "plc/rtd/3": "DF3  Xe cube nozzle",
    "plc/rtd/4": "DF4  LN2 vessel base",
}
for _n in range(1, 7):
    SENSOR_LABELS[f"omega/ch{_n}"] = f"Omega ch{_n}"

GATE_MODES = {
    "signed    (Ti - Tj) < X": "signed",
    "absolute  |Ti - Tj| < X": "absolute",
}
_MODE_TO_LABEL = {v: k for k, v in GATE_MODES.items()}

# Directly commanded solenoid valves: (MQTT valve name, label, output,
# desired-state register).  Keep in step with plc.REG_SOLENOID_DESIRED and
# plc.REG_VALVE_COIL.
SOLENOID_VALVES = (
    ("gas_ballast", "Ballast valve", "Y105", "DS151"),
    ("gas_pump",    "Pump valve",    "Y106", "DS152"),
    ("gas_bottle",  "Bottle valve",  "Y107", "DS153"),
)


# ---------------------------------------------------------------------------
# Gate evaluation — pure, so it can be reasoned about and tested on its own
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class GateResult:
    passed: bool
    reason: str
    delta: Optional[float] = None


def evaluate_gate(t_warm: Optional[float],
                  t_cold: Optional[float],
                  threshold_k: Optional[float],
                  mode: str,
                  same_sensor: bool = False) -> GateResult:
    """Decide whether the temperature gate permits opening XV3.

    Every branch that cannot produce a number refuses.  This is the opposite
    of the automatic coast gate, which releases its inhibit when it cannot
    evaluate a term — and deliberately so.  Coast fails towards spending LN2
    because the alternative is stranding a vessel empty; this button fails
    towards doing nothing, because the alternative is opening a cryogen valve
    on evidence the operator cannot see.
    """
    if same_sensor:
        return GateResult(False, "Ti and Tj are the same channel — "
                                 "pick two different sensors")
    if threshold_k is None:
        return GateResult(False, "threshold is not a number")
    if t_warm is None and t_cold is None:
        return GateResult(False, "no fresh reading from either sensor")
    if t_warm is None:
        return GateResult(False, "no fresh reading from Ti")
    if t_cold is None:
        return GateResult(False, "no fresh reading from Tj")

    delta = t_warm - t_cold
    if mode == "absolute":
        delta = abs(delta)
    if delta < threshold_k:
        return GateResult(True,
                          f"delta = {delta:.2f} K  <  {threshold_k:.2f} K",
                          delta)
    return GateResult(False,
                      f"delta = {delta:.2f} K  is not below  "
                      f"{threshold_k:.2f} K",
                      delta)


# ---------------------------------------------------------------------------
# Panel
# ---------------------------------------------------------------------------

@dataclass
class _Reading:
    value: float
    stamp: float

    def fresh(self, now: Optional[float] = None) -> bool:
        return ((now if now is not None else time.monotonic())
                - self.stamp) <= MAX_AGE_S


class CryostatPanel(ttk.Frame):
    """Live cryostat status, the temperature-gated XV3 open button, and the
    open / close buttons for the gas-side solenoid valves."""

    def __init__(self, parent: tk.Misc, config=None, **kw) -> None:
        super().__init__(parent, padding=12, **kw)
        self._cfg = config
        self._coast = self._coast_defaults(config)

        # Everything the MQTT thread writes and the Tk thread reads lives
        # behind this lock.  Tk is not thread-safe, so the callbacks below
        # touch only these plain dicts and never a widget; the after() loop
        # does all of the widget work.
        self._lock = threading.Lock()
        self._temps: Dict[str, _Reading] = {}
        self._level: Dict[str, object] = {}
        self._valve: Dict[str, object] = {}
        self._level_stamp = 0.0
        self._valve_stamp = 0.0
        # valve name -> (last status payload, arrival time)
        self._solenoids: Dict[str, Tuple[dict, float]] = {}

        self._mqtt = None
        self._mqtt_error: Optional[str] = None
        self._host = "localhost"
        self._port = 1883
        self._after_id: Optional[str] = None

        self._build()
        self._connect()
        self._tick()

    # ------------------------------------------------------------------
    # Config
    # ------------------------------------------------------------------

    @staticmethod
    def _coast_defaults(config):
        """Return the cryostat coast block if there is one, else None."""
        try:
            return config.autovalve.vessels["cryostat"].coast
        except Exception:
            return None

    def _default(self, attr: str, fallback):
        return getattr(self._coast, attr, fallback) if self._coast else fallback

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------

    def _build(self) -> None:
        ttk.Label(self, text="Cryostat LN2 fill (XV3)",
                  font=("TkDefaultFont", 14, "bold")).pack(anchor="w")
        ttk.Label(self,
                  text="Opens XV3 only while the selected sensor pair has "
                       "converged to within the threshold.",
                  foreground="gray").pack(anchor="w", pady=(0, 10))

        self._build_status()
        self._build_gate()
        self._build_actions()
        self._build_solenoids()
        self._build_footer()

    # -- live status ---------------------------------------------------

    def _build_status(self) -> None:
        box = ttk.LabelFrame(self, text="Live status", padding=10)
        box.pack(fill="x", pady=(0, 10))
        box.columnconfigure(1, weight=1)
        box.columnconfigure(3, weight=1)

        self._v_level = tk.StringVar(value="—")
        self._v_fill = tk.StringVar(value="—")
        self._v_xv3 = tk.StringVar(value="—")
        self._v_auto = tk.StringVar(value="—")

        rows = [
            ("LN2 level (DF303)", self._v_level,
             "Fill status (DS3)", self._v_fill),
            ("XV3 state", self._v_xv3,
             "Auto close / open", self._v_auto),
        ]
        for r, (la, va, lb, vb) in enumerate(rows):
            ttk.Label(box, text=f"{la}:", anchor="e", width=18).grid(
                row=r, column=0, sticky="e", padx=(0, 6), pady=3)
            ttk.Label(box, textvariable=va, font=("TkFixedFont", 10)).grid(
                row=r, column=1, sticky="w")
            ttk.Label(box, text=f"{lb}:", anchor="e", width=18).grid(
                row=r, column=2, sticky="e", padx=(12, 6), pady=3)
            ttk.Label(box, textvariable=vb, font=("TkFixedFont", 10)).grid(
                row=r, column=3, sticky="w")

        # Shown only when the ladder would immediately undo a manual open.
        self._v_conflict = tk.StringVar(value="")
        ttk.Label(box, textvariable=self._v_conflict,
                  foreground="#b26a00", wraplength=640,
                  justify="left").grid(row=2, column=0, columnspan=4,
                                       sticky="w", pady=(8, 0))

    # -- gate ----------------------------------------------------------

    def _build_gate(self) -> None:
        box = ttk.LabelFrame(self, text="Temperature gate", padding=10)
        box.pack(fill="x", pady=(0, 10))
        box.columnconfigure(1, weight=1)

        choices = sorted(SENSOR_LABELS)
        fmt = [self._fmt_sensor(c) for c in choices]

        def _row(r, text, var, values):
            ttk.Label(box, text=text, anchor="e", width=18).grid(
                row=r, column=0, sticky="e", padx=(0, 6), pady=3)
            cb = ttk.Combobox(box, textvariable=var, values=values,
                              state="readonly", width=34)
            cb.grid(row=r, column=1, sticky="w")
            cb.bind("<<ComboboxSelected>>", lambda _e: self._refresh_gate())
            return cb

        self._v_warm = tk.StringVar(
            value=self._fmt_sensor(self._default("sensor_warm", "plc/rtd/2")))
        self._v_cold = tk.StringVar(
            value=self._fmt_sensor(self._default("sensor_cold", "plc/rtd/4")))
        _row(0, "Ti  (warm / load)", self._v_warm, fmt)
        _row(1, "Tj  (cold / sink)", self._v_cold, fmt)

        mode = self._default("delta_mode", "signed")
        self._v_mode = tk.StringVar(
            value=_MODE_TO_LABEL.get(mode, next(iter(GATE_MODES))))
        _row(2, "Comparison", self._v_mode, list(GATE_MODES))

        ttk.Label(box, text="Threshold X (K):", anchor="e", width=18).grid(
            row=3, column=0, sticky="e", padx=(0, 6), pady=3)
        self._v_thresh = tk.StringVar(
            value=f"{self._default('delta_max_k', 40.0):g}")
        ttk.Entry(box, textvariable=self._v_thresh, width=12).grid(
            row=3, column=1, sticky="w")
        self._v_thresh.trace_add("write", lambda *_a: self._refresh_gate())

        self._v_ti = tk.StringVar(value="Ti —     Tj —")
        ttk.Label(box, textvariable=self._v_ti,
                  font=("TkFixedFont", 10)).grid(
            row=4, column=0, columnspan=2, sticky="w", pady=(10, 2))

        self._v_verdict = tk.StringVar(value="—")
        self._l_verdict = ttk.Label(box, textvariable=self._v_verdict,
                                    font=("TkDefaultFont", 11, "bold"),
                                    wraplength=640, justify="left")
        self._l_verdict.grid(row=5, column=0, columnspan=2, sticky="w")

    # -- buttons -------------------------------------------------------

    def _build_actions(self) -> None:
        bar = ttk.Frame(self)
        bar.pack(fill="x", pady=(0, 6))
        self._b_open = ttk.Button(bar, text="Open XV3  (desired = 1)",
                                  command=self._on_open, state="disabled")
        self._b_open.pack(side="left")
        # Close is deliberately never gated.  A panel that can open a cryogen
        # fill valve but not shut it again is the wrong shape, even though the
        # ladder does auto-close on DS3 = 1 or the T3 timeout.
        ttk.Button(bar, text="Close XV3  (desired = 0)",
                   command=self._on_close_valve).pack(side="left", padx=(8, 0))

    # -- solenoid valves -----------------------------------------------

    def _build_solenoids(self) -> None:
        box = ttk.LabelFrame(self, text="Gas-side solenoid valves", padding=10)
        box.pack(fill="x", pady=(4, 10))
        box.columnconfigure(2, weight=1)

        self._v_solenoid: Dict[str, tk.StringVar] = {}
        for r, valve in enumerate(SOLENOID_VALVES):
            name, label, output, register = valve
            ttk.Label(box, text=f"{label}:", anchor="e", width=18).grid(
                row=r, column=0, sticky="e", padx=(0, 6), pady=3)
            ttk.Label(box, text=f"{output} / {register}",
                      foreground="gray").grid(
                row=r, column=1, sticky="w", padx=(0, 12))
            var = tk.StringVar(value="—")
            self._v_solenoid[name] = var
            ttk.Label(box, textvariable=var, font=("TkFixedFont", 10)).grid(
                row=r, column=2, sticky="w")
            # Neither direction is gated, and neither is the "safe" one in
            # general — closing the ballast or bottle valve removes expansion
            # volume — so both simply confirm, like the XV3 buttons above.
            ttk.Button(box, text=f"Open  ({register} = 1)",
                       command=lambda v=valve: self._on_solenoid(v, 1)).grid(
                row=r, column=3, padx=(8, 0))
            ttk.Button(box, text=f"Close  ({register} = 0)",
                       command=lambda v=valve: self._on_solenoid(v, 0)).grid(
                row=r, column=4, padx=(8, 0))

    def _build_footer(self) -> None:
        bar = ttk.Frame(self)
        bar.pack(fill="x", pady=(6, 0))
        self._v_conn = tk.StringVar(value="MQTT: connecting...")
        ttk.Label(bar, textvariable=self._v_conn,
                  foreground="gray").pack(side="left")
        ttk.Button(bar, text="Reconnect",
                   command=self._reconnect).pack(side="right")

    # ------------------------------------------------------------------
    # Sensor id <-> combobox label
    # ------------------------------------------------------------------

    @staticmethod
    def _fmt_sensor(sid: str) -> str:
        return f"{sid}    {SENSOR_LABELS.get(sid, '')}".rstrip()

    @staticmethod
    def _parse_sensor(text: str) -> str:
        return text.split()[0] if text and text.split() else ""

    # ------------------------------------------------------------------
    # MQTT
    # ------------------------------------------------------------------

    def _connect(self) -> None:
        try:
            from slowcontrol.core.mqtt import MqttClient
        except Exception as exc:      # paho missing, or run outside package
            self._mqtt_error = f"unavailable ({exc})"
            log.exception("Cannot import MQTT client")
            return

        mqtt_cfg = getattr(self._cfg, "mqtt", None)
        self._host = getattr(mqtt_cfg, "host", "localhost")
        self._port = getattr(mqtt_cfg, "port", 1883)

        # A distinct client id per process.  An MQTT broker evicts the existing
        # session when a second connection claims the same client id, so
        # reusing the service default here would knock the slow control
        # service off the broker every time this GUI started.
        client_id = f"xsphere-gui-{os.getpid()}"
        try:
            self._mqtt = MqttClient(host=self._host, port=self._port,
                                    client_id=client_id)
            self._mqtt.subscribe("xsphere/sensors/temperature/#",
                                 self._on_temp)
            self._mqtt.subscribe("xsphere/sensors/level/cryostat",
                                 self._on_level)
            self._mqtt.subscribe("xsphere/status/valve/cryostat",
                                 self._on_valve)
            for name, *_ in SOLENOID_VALVES:
                self._mqtt.subscribe(f"xsphere/status/valve/{name}",
                                     self._on_solenoid_status)
            self._mqtt.connect()
            self._mqtt_error = None
        except Exception as exc:
            self._mqtt = None
            self._mqtt_error = str(exc)
            log.warning("MQTT connect failed: %s", exc)

    def _reconnect(self) -> None:
        self._disconnect()
        self._connect()

    def _disconnect(self) -> None:
        if self._mqtt is not None:
            try:
                self._mqtt.disconnect()
            except Exception:
                log.debug("MQTT disconnect failed", exc_info=True)
            self._mqtt = None

    # -- subscription callbacks (MQTT thread — no widget access) -------

    def _on_temp(self, topic: str, payload) -> None:
        if not isinstance(payload, dict):
            return
        sid = topic.split("/sensors/temperature/", 1)[-1]
        val = payload.get("value_k")
        if val is None:
            # Fall back to Celsius if a publisher omits Kelvin.  The gate is a
            # difference, so the offset cancels — but never mix the two units
            # within one comparison, hence the conversion here rather than
            # reading value_c for one sensor and value_k for the other.
            c = payload.get("value_c")
            val = None if c is None else c + 273.15
        try:
            val = float(val)
        except (TypeError, ValueError):
            return
        with self._lock:
            self._temps[sid] = _Reading(val, time.monotonic())

    def _on_level(self, _topic: str, payload) -> None:
        if not isinstance(payload, dict):
            return
        with self._lock:
            self._level = dict(payload)
            self._level_stamp = time.monotonic()

    def _on_valve(self, _topic: str, payload) -> None:
        if not isinstance(payload, dict):
            return
        with self._lock:
            self._valve = dict(payload)
            self._valve_stamp = time.monotonic()

    def _on_solenoid_status(self, topic: str, payload) -> None:
        if not isinstance(payload, dict):
            return
        name = topic.rsplit("/", 1)[-1]
        with self._lock:
            self._solenoids[name] = (dict(payload), time.monotonic())

    # ------------------------------------------------------------------
    # Refresh loop (Tk thread)
    # ------------------------------------------------------------------

    def _tick(self) -> None:
        try:
            self._refresh_status()
            self._refresh_solenoids()
            self._refresh_gate()
        except Exception:
            log.exception("Cryostat panel refresh failed")
        self._after_id = self.after(REFRESH_MS, self._tick)

    def _snapshot(self):
        with self._lock:
            return (dict(self._temps), dict(self._level), dict(self._valve),
                    self._level_stamp, self._valve_stamp)

    def _refresh_status(self) -> None:
        _, level, valve, lstamp, vstamp = self._snapshot()
        now = time.monotonic()

        fill_status = None
        if level and (now - lstamp) <= MAX_AGE_S:
            filt = level.get("filtered")
            self._v_level.set(f"{filt:.4f}"
                              if isinstance(filt, (int, float)) else "—")
            fill_status = level.get("fill_status")
            self._v_fill.set({0: "0   EMPTY (wants fill)",
                              1: "1   FULL"}.get(fill_status,
                                                 "— (not reported)"))
        else:
            self._v_level.set("— stale")
            self._v_fill.set("— stale")

        if valve and (now - vstamp) <= MAX_AGE_S:
            self._v_xv3.set(self._fmt_valve(valve))
            self._v_auto.set(f"{self._onoff(valve.get('auto_close'))} / "
                             f"{self._onoff(valve.get('auto_open'))}")
        else:
            self._v_xv3.set("— stale")
            self._v_auto.set("— stale")
            valve = {}

        self._v_conflict.set(self._conflict_hint(fill_status, valve))
        self._v_conn.set(self._conn_text())

    def _refresh_solenoids(self) -> None:
        with self._lock:
            seen = dict(self._solenoids)
        now = time.monotonic()
        for name, var in self._v_solenoid.items():
            payload, stamp = seen.get(name, ({}, 0.0))
            if payload and (now - stamp) <= MAX_AGE_S:
                var.set(self._fmt_valve(payload))
            else:
                var.set("— stale")

    @staticmethod
    def _fmt_valve(status: dict) -> str:
        st = status.get("state")
        de = status.get("desired")
        shown = "OPEN" if st == 1 else "closed" if st == 0 else "?"
        return f"{shown}   (desired {de if de is not None else '?'})"

    @staticmethod
    def _onoff(v) -> str:
        return "on" if v == 1 else "off" if v == 0 else "?"

    @staticmethod
    def _conflict_hint(fill_status, valve: dict) -> str:
        """Warn when the ladder will immediately undo a manual open.

        Rung 28 closes XV3 whenever auto-close is enabled, XV3 is energised
        and DS3 = 1.  Opening the valve into that condition writes DS1006 = 1
        and then watches the PLC put it straight back to 0, which looks like a
        broken button unless the operator is told why.
        """
        if fill_status == 1 and valve.get("auto_close") == 1:
            return ("Note: DS3 reads FULL and XV3 auto-close is on, so the "
                    "ladder (rung 28) will close XV3 again almost immediately "
                    "after a manual open.")
        return ""

    def _connected(self) -> bool:
        """True only while a publish would actually reach the broker."""
        if self._mqtt is None:
            return False
        try:
            return self._mqtt.is_connected()
        except Exception:
            return False

    def _conn_text(self) -> str:
        if self._mqtt_error:
            return f"MQTT: {self._mqtt_error}"
        if self._mqtt is None:
            return "MQTT: not connected"
        if not self._connected():
            return (f"MQTT: {self._host}:{self._port} — link down, "
                    "reconnecting")
        return f"MQTT: connected to {self._host}:{self._port}"

    # -- gate ----------------------------------------------------------

    def _read_gate_inputs(self):
        """Current sensor ids, mode and threshold, as set in the widgets."""
        warm = self._parse_sensor(self._v_warm.get())
        cold = self._parse_sensor(self._v_cold.get())
        mode = GATE_MODES.get(self._v_mode.get(), "signed")
        try:
            thresh = float(self._v_thresh.get())
        except (TypeError, ValueError):
            thresh = None
        return warm, cold, mode, thresh

    def _evaluate(self):
        warm, cold, mode, thresh = self._read_gate_inputs()
        temps, _, _, _, _ = self._snapshot()
        now = time.monotonic()

        def fresh(sid):
            r = temps.get(sid)
            return r.value if r is not None and r.fresh(now) else None

        t_warm, t_cold = fresh(warm), fresh(cold)
        res = evaluate_gate(t_warm, t_cold, thresh, mode,
                            same_sensor=bool(warm) and warm == cold)
        return res, t_warm, t_cold

    def _refresh_gate(self) -> None:
        res, t_warm, t_cold = self._evaluate()
        self._v_ti.set(f"Ti {self._fmt_k(t_warm)}     "
                       f"Tj {self._fmt_k(t_cold)}")
        self._v_verdict.set(("PERMITTED — " if res.passed else "BLOCKED — ")
                            + res.reason)
        self._l_verdict.configure(
            foreground="#1a7f37" if res.passed else "#b3261e")
        self._b_open.configure(
            state=("normal" if res.passed and self._connected()
                   else "disabled"))

    @staticmethod
    def _fmt_k(v: Optional[float]) -> str:
        return f"= {v:8.3f} K" if v is not None else "=        —  "

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def _on_open(self) -> None:
        # Re-evaluate at click time.  The displayed verdict can be up to
        # REFRESH_MS old and the readings behind it up to MAX_AGE_S, and
        # neither is a basis for opening a cryogen valve.
        res, t_warm, t_cold = self._evaluate()
        if not res.passed:
            messagebox.showwarning(
                "XV3 open refused",
                "The temperature gate does not permit an open:\n\n"
                f"{res.reason}",
                parent=self)
            self._refresh_gate()
            return

        warm, cold, mode, thresh = self._read_gate_inputs()
        symbol = "|Ti - Tj|" if mode == "absolute" else "Ti - Tj"
        if not messagebox.askokcancel(
                "Open XV3?",
                "Set the XV3 desired state (DS1006) to 1 and start an LN2 "
                "fill of the cryostat?\n\n"
                f"Ti   {warm}   = {t_warm:.3f} K\n"
                f"Tj   {cold}   = {t_cold:.3f} K\n"
                f"{symbol} = {res.delta:.3f} K   <   {thresh:.3f} K\n\n"
                "The ladder still owns auto-close and the fill timeout.",
                parent=self):
            return
        self._send_state(1)

    def _on_close_valve(self) -> None:
        if messagebox.askokcancel(
                "Close XV3?",
                "Set the XV3 desired state (DS1006) to 0?",
                parent=self):
            self._send_state(0)

    def _on_solenoid(self, valve: Tuple[str, str, str, str],
                     state: int) -> None:
        name, label, output, register = valve
        verb = "Open" if state else "Close"
        if messagebox.askokcancel(
                f"{verb} {label.lower()}?",
                f"Set {register} to {state} and {verb.lower()} the "
                f"{label.lower()} ({output})?",
                parent=self):
            self._send_state(state, name)

    def _send_state(self, state: int, valve: str = "cryostat") -> None:
        if not self._connected():
            messagebox.showerror("Not connected",
                                 "No MQTT connection — command not sent.",
                                 parent=self)
            return
        try:
            from slowcontrol.core.mqtt import command_topic
            topic = command_topic("valve", valve, "state")
            self._mqtt.publish(topic, {"state": int(state)})
            log.info("[gui] published %s state=%d", topic, state)
        except Exception as exc:
            log.exception("Failed to publish valve command")
            messagebox.showerror("Command failed",
                                 f"Could not publish the command:\n\n{exc}",
                                 parent=self)

    # ------------------------------------------------------------------

    def destroy(self) -> None:
        if self._after_id is not None:
            try:
                self.after_cancel(self._after_id)
            except Exception:
                log.debug("after_cancel failed", exc_info=True)
            self._after_id = None
        self._disconnect()
        super().destroy()
