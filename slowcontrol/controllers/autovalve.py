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
  7. Optionally apply the coast gate (below) to the OPEN decision.

The PLC ladder logic for autofill continues to run in parallel and acts as
a hardware backup; the Python controller is the primary decision maker.

Coast refill
────────────
Plain autofill refills as soon as the level falls through `level_low`, which
tops the vessel up while there is still cooling authority left in it.  Coast
instead lets the vessel run dry and keeps running on the thermal mass of its
cold block, refilling only once that reserve is spent.  The reserve is judged
from two temperature sensors converging: with liquid in the vessel the cold
sink sits far below the heated load, and as the block dries the two readings
close on each other.

A refill is permitted only when BOTH terms hold:

    level < empty_threshold             the vessel is actually empty
    (T_warm - T_cold) < delta_max_k     the cold reserve is spent

Coast gates `auto_open` ONLY.  Three invariants hold unconditionally:

  * it can never inhibit `auto_close`, the fill timeout, or a manual command;
  * every condition it cannot evaluate — a stale level, a missing RTD, an
    implausible reading, a reversed pair — RELEASES the inhibit rather than
    extending it, so the failure direction is always "spend LN2" and never
    "leave the vessel empty and warming";
  * three backstops (elapsed time, an absolute cube-temperature ceiling, and
    the cold sink itself reading warm) force a refill and latch coast off
    until the vessel is demonstrably wet again.

Coast always comes up DISARMED.  Deliberately running a cryostat dry is not
something a service restart or a config reload should ever re-enter on its
own.

MQTT interface
──────────────
  Subscribe (level readings in):
    xsphere/sensors/level/{vessel}            {"raw": X}

  Subscribe (temperatures in, coast only):
    xsphere/sensors/temperature/plc/rtd/{n}   {"value_k": X}

  Subscribe (commands in):
    xsphere/commands/valve/{vessel}/auto_open  {"enabled": true|false}
    xsphere/commands/valve/{vessel}/auto_close {"enabled": true|false}
    xsphere/commands/valve/{vessel}/state      {"state": 0|1}
    xsphere/commands/valve/{vessel}/coast      {"enabled": true|false}
    xsphere/commands/valve/{vessel}/coast_config
                                               {"delta_max_k": X, ...}

  Subscribe (PLC readback, coast only):
    xsphere/status/valve/{vessel}             {"coast_permit": 0|1, ...}

  Publish (commands out to PLC driver):
    xsphere/commands/valve/{vessel}/state      {"state": 0|1}
    xsphere/commands/valve/{vessel}/coast_permit {"enabled": 0|1}

  Publish (filtered level data):
    xsphere/status/level/{vessel}             {"raw": X, "filtered": Y}

  Publish (coast state):
    xsphere/status/coast/{vessel}             see _publish_coast_status

  Publish (alerts):
    xsphere/alerts/fill_timeout/{vessel}      {"vessel": ..., "msg": ...}
    xsphere/alerts/coast_backstop/{vessel}    a backstop forced a refill
    xsphere/alerts/coast_unverified/{vessel}  gate released on unusable data
    xsphere/alerts/coast_refused/{vessel}     a coast_config was rejected
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, replace
from typing import Dict, Optional, Set, Tuple

from slowcontrol.controllers.base import Controller
from slowcontrol.core.config import CoastConfig, validate_vessel
from slowcontrol.core.mqtt import command_topic, sensor_topic, status_topic

log = logging.getLogger(__name__)

VESSELS = ("cryostat", "primary_xe", "ballast")
ALPHA = 0.01   # exponential filter coefficient (matches PLC ladder α)

# Period of the control loop.  Every coast threshold carries at least 6x
# margin over this, so a temperature-only change reaches the valve well
# inside the shortest dwell the gate can be configured with.
CONTROL_TICK_S = 10.0

# Age past which a level reading will not be used to OPEN a valve. Matches
# interlocks.LEVEL_STALE_S, which is what raises the operator-facing alert.
# Closing is allowed on a stale reading; opening on one is the documented
# fill_while_level_unknown hazard.
LEVEL_TRUST_S = 60.0

# Temperature channels the coast gate subscribes to.  All of them are taken
# whenever any vessel has coast configured, regardless of which pair is
# selected: MqttClient has no unsubscribe, and choosing the pair at decision
# time is what makes a runtime pair change safe.
COAST_TEMP_CHANNELS = tuple(f"plc/rtd/{n}" for n in range(1, 5))

# The ceiling backstop protects the xenon, so it watches all three cube RTDs
# rather than the configured pair.  Not operator-configurable — letting a
# dashboard narrow the thing that stops the cube warming is not a feature.
CEILING_SENSORS = ("plc/rtd/1", "plc/rtd/2", "plc/rtd/3")

# A "cold" sink reading this far above the load is a wiring or scaling fault,
# not convergence.  Caught explicitly so that delta_mode: absolute cannot read
# it as a satisfied gate.
SENSOR_REVERSED_MARGIN_K = 2.0

# Coast keys an operator may retune at runtime over MQTT.  The backstops are
# deliberately absent: a dashboard typo must not be able to remove a backstop
# or make the gate unsatisfiable.
COAST_TUNABLE = frozenset({
    "empty_threshold", "sensor_warm", "sensor_cold",
    "delta_max_k", "delta_mode", "delta_hysteresis_k", "confirm_s",
})

# Every alert rule the coast gate owns, so a rule that stops applying gets
# cleared instead of sitting retained on the broker forever.
COAST_ALERT_RULES = ("coast_backstop", "coast_unverified", "coast_refused")


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
    level_time:     Optional[float] = None    # monotonic time of last reading
    # Config thresholds (loaded from config)
    level_high:     float = 2.5
    level_low:      float = 0.5
    fill_timeout_s: int   = 600
    # Coast gate — coast_cfg is None when the vessel has no coast block
    coast_cfg:      Optional[CoastConfig] = None
    coast_armed:    bool = False              # operator arm, RAM only
    coast_state:    str  = "disarmed"
    coast_reason:   str  = "coast disarmed"
    coast_since:    Optional[float] = None    # monotonic, episode start
    coast_latched_off: bool = False
    coast_release_reason: Optional[str] = None
    gate_armed:     bool = False              # hysteresis latch on the ΔT term
    gate_true_since: Optional[float] = None   # dwell accumulator
    coast_open_threshold: float = 0.0
    coast_permit_open: bool = True
    coast_t_warm_k: Optional[float] = None
    coast_t_cold_k: Optional[float] = None
    coast_delta_k:  Optional[float] = None
    coast_ceiling_max_k: Optional[float] = None
    ladder_permit:  Optional[int] = None      # last value we wrote to DS1107
    ladder_permit_readback: Optional[int] = None


# ---------------------------------------------------------------------------
# Coast gate — pure decision function
# ---------------------------------------------------------------------------

@dataclass
class CoastSnapshot:
    """Everything the coast gate is allowed to look at, sampled atomically."""
    now:          float                        # monotonic
    armed:        bool
    auto_open:    bool                         # Python's own auto_open enable
    latched_off:  bool
    level:        Optional[float]              # filtered level
    level_age_s:  Optional[float]
    temps:        Dict[str, Tuple[float, float]]   # channel -> (value_k, age_s)
    coast_since:  Optional[float]
    gate_armed:   bool
    gate_true_since: Optional[float]


@dataclass
class CoastDecision:
    """The gate's verdict, plus the state the caller must carry forward."""
    state:          str        # disarmed|latched_off|released|monitoring|
                               # coasting|converged|backstop:<why>
    open_threshold: float      # level the OPEN branch compares against
    permit_open:    bool       # may the OPEN branch fire at all
    ladder_permit:  int        # 1 = release DS1107, 0 = inhibit the ladder
    reason:         str        # one operator-readable sentence
    alert:          Optional[str] = None
    latch:          bool = False
    coast_since:    Optional[float] = None
    gate_armed:     bool = False
    gate_true_since: Optional[float] = None
    t_warm_k:       Optional[float] = None
    t_cold_k:       Optional[float] = None
    delta_k:        Optional[float] = None
    ceiling_max_k:  Optional[float] = None


def coast_decide(snap: CoastSnapshot, c: Optional[CoastConfig],
                 level_low: float) -> CoastDecision:
    """Decide whether the coast gate permits an autofill open.

    Pure: no locks, no I/O, no clock reads.  Everything it needs is in `snap`,
    which is why the whole gate can be exercised in a unit test without a
    broker or a PLC.

    Guards are evaluated in order and the first match wins.  Every path that
    is not `coasting` (or `monitoring`, which withholds nothing but keeps the
    ladder quiet during the descent) leaves the valve at least as free to open
    as plain autofill would.
    """

    def release(state: str, reason: str, alert: Optional[str] = None,
                **tele) -> CoastDecision:
        """Coast is not gating anything: behave exactly as autofill does today.

        Resets the episode clock and the ΔT dwell by omission — a coast that
        was released is over, and the next one starts its dwell afresh.
        """
        return CoastDecision(state=state, open_threshold=level_low,
                             permit_open=True, ladder_permit=1,
                             reason=reason, alert=alert, **tele)

    # 1. Not configured, not enabled in the file, or not armed by an operator.
    if c is None or not c.enabled or not snap.armed:
        return release("disarmed", "coast disarmed")

    # 1b. Coast may only ever SUBTRACT from what this service would do on its
    #     own. With auto_open disarmed the service will not open the valve at
    #     any level, so inhibiting the ladder as well would leave nothing
    #     willing to fill the vessel — coast would be suppressing the backup
    #     layer to hold off a refill it was never going to perform.
    if not snap.auto_open:
        return release(
            "disarmed",
            "coast armed but auto_open is not — arm auto_open for the coast "
            "gate to take effect")

    # 2. A backstop fired this episode and the vessel is not yet proven wet.
    #    Keeps asserting coast_backstop: _alert is idempotent, so the original
    #    message naming the actual cause stays retained on the broker, and the
    #    alert clears only when the latch does. Without re-asserting it here,
    #    the alert would be cleared on the very next tick — a backstop would
    #    flash for one tick and vanish while the vessel was still latched off.
    if snap.latched_off:
        return release(
            "latched_off",
            "coast latched off until the vessel is proven refilled",
            alert="coast_backstop")

    # 3. The gate's first term is a level, so an unusable level releases it.
    if (snap.level is None or snap.level_age_s is None
            or snap.level_age_s > c.level_stale_s):
        age = ("never" if snap.level_age_s is None
               else f"{snap.level_age_s:.0f} s old")
        return release("released",
                       f"level unusable ({age}) — coast inhibit released",
                       alert="coast_unverified")

    # 4. Both channels of the pair must be present, fresh and plausible.
    pair = []
    for role, ch in (("warm", c.sensor_warm), ("cold", c.sensor_cold)):
        entry = snap.temps.get(ch)
        if entry is None:
            return release(
                "released",
                f"{role} sensor {ch} has never reported — "
                "coast inhibit released",
                alert="coast_unverified")
        value, age = entry
        if age > c.temp_stale_s:
            return release(
                "released",
                f"{role} sensor {ch} stale ({age:.0f} s) — "
                "coast inhibit released",
                alert="coast_unverified")
        if not c.temp_min_k <= value <= c.temp_max_k:
            return release(
                "released",
                f"{role} sensor {ch} implausible ({value:.1f} K) — "
                "coast inhibit released",
                alert="coast_unverified")
        pair.append(value)
    t_warm, t_cold = pair

    delta_signed = t_warm - t_cold
    delta = abs(delta_signed) if c.delta_mode == "absolute" else delta_signed

    # Warmest fresh, plausible cube RTD — the ceiling backstop's input.
    ceiling_max: Optional[float] = None
    for ch in CEILING_SENSORS:
        entry = snap.temps.get(ch)
        if entry is None:
            continue
        value, age = entry
        if age > c.temp_stale_s or not c.temp_min_k <= value <= c.temp_max_k:
            continue
        ceiling_max = value if ceiling_max is None else max(ceiling_max, value)

    tele = {"t_warm_k": t_warm, "t_cold_k": t_cold, "delta_k": delta,
            "ceiling_max_k": ceiling_max}

    # 5. A sink hotter than the load it is supposed to be cooling is a fault.
    if t_cold > t_warm + SENSOR_REVERSED_MARGIN_K:
        return release(
            "released",
            f"cold sensor {c.sensor_cold} at {t_cold:.1f} K is above the load "
            f"at {t_warm:.1f} K — check the sensor assignment; coast inhibit "
            "released",
            alert="coast_unverified", **tele)

    # 6. Level says there is liquid but the sink reads dry: they cannot both
    #    be right, and a stuck-high level channel is the dangerous reading.
    if snap.level >= c.empty_threshold and t_cold > c.sink_warm_k:
        return release(
            "released",
            f"level {snap.level:.3f} says liquid but {c.sensor_cold} reads "
            f"{t_cold:.1f} K — sensors disagree; coast inhibit released",
            alert="coast_unverified", **tele)

    # 7. Episode bookkeeping. The clock starts when the vessel first reads
    #    empty and is dropped the moment it does not.
    empty = snap.level < c.empty_threshold
    coast_since = snap.coast_since if empty else None
    if empty and coast_since is None:
        coast_since = snap.now

    # 8. Backstops. Checked ahead of the empty/not-empty split so they also
    #    release the ladder during the descent, and acted on the first tick
    #    they are true — the confirm_s dwell only ever delays a fill.
    backstop: Optional[Tuple[str, str]] = None
    if coast_since is not None and snap.now - coast_since > c.max_duration_s:
        backstop = ("max_duration",
                    f"coasting for {(snap.now - coast_since) / 60:.0f} min, "
                    f"past the {c.max_duration_s / 60:.0f} min limit")
    elif t_cold > c.sink_warm_k:
        backstop = ("sink_warm",
                    f"{c.sensor_cold} at {t_cold:.1f} K is above "
                    f"{c.sink_warm_k:.0f} K — the cold sink is dry")
    elif ceiling_max is not None and ceiling_max > c.ceiling_k:
        backstop = ("ceiling",
                    f"a cube RTD reached {ceiling_max:.1f} K, above the "
                    f"{c.ceiling_k:.0f} K ceiling")

    if backstop is not None:
        why, msg = backstop
        return CoastDecision(
            state=f"backstop:{why}", open_threshold=level_low,
            permit_open=True, ladder_permit=1,
            reason=f"refill forced: {msg}",
            alert="coast_backstop", latch=True,
            coast_since=coast_since, **tele)

    # 9. Not empty yet. Nothing is withheld — the threshold is already the
    #    coast one — but DS1107 must be 0 here, because the descent through
    #    the ladder's own 0.25 < level < 0.8 auto-open window happens in this
    #    state. Leave the permit granted and the ladder tops the vessel up
    #    before it is ever empty, and coast saves nothing.
    if not empty:
        return CoastDecision(
            state="monitoring", open_threshold=c.empty_threshold,
            permit_open=True, ladder_permit=0,
            reason=(f"level {snap.level:.3f} above empty_threshold "
                    f"{c.empty_threshold:.3f} — waiting for the vessel to "
                    "empty"),
            **tele)

    # 10. The ΔT gate, with hysteresis on the threshold and a dwell on top.
    gate_armed = snap.gate_armed
    if delta < c.delta_max_k:
        gate_armed = True
    elif delta > c.delta_max_k + c.delta_hysteresis_k:
        gate_armed = False

    gate_true_since = snap.gate_true_since if gate_armed else None
    if gate_armed and gate_true_since is None:
        gate_true_since = snap.now
    dwell = 0.0 if gate_true_since is None else snap.now - gate_true_since

    if gate_armed and dwell >= c.confirm_s:
        return CoastDecision(
            state="converged", open_threshold=c.empty_threshold,
            permit_open=True, ladder_permit=1,
            reason=(f"ΔT {delta:.1f} K held below {c.delta_max_k:.1f} K for "
                    f"{dwell:.0f} s — cold reserve spent, refilling"),
            coast_since=coast_since, gate_armed=gate_armed,
            gate_true_since=gate_true_since, **tele)

    if gate_armed:
        hold = (f"ΔT {delta:.1f} K below {c.delta_max_k:.1f} K but only for "
                f"{dwell:.0f} s of the {c.confirm_s:.0f} s required")
    else:
        hold = (f"ΔT {delta:.1f} K still above {c.delta_max_k:.1f} K — "
                "coasting on thermal mass")

    # 11. The only path that withholds a fill.
    return CoastDecision(
        state="coasting", open_threshold=c.empty_threshold,
        permit_open=False, ladder_permit=0, reason=hold,
        coast_since=coast_since, gate_armed=gate_armed,
        gate_true_since=gate_true_since, **tele)


class AutovalveController(Controller):
    NAME = "autovalve"

    def __init__(self, config, mqtt):
        super().__init__(config, mqtt)
        # Re-entrant: _evaluate decides and actuates under a single hold, and
        # _set_valve takes the same lock.
        self._lock = threading.RLock()
        self._states: Dict[str, VesselState] = {}
        self._temps: Dict[str, Tuple[float, float]] = {}   # ch -> (K, mono)
        self._active_alerts: Set[str] = set()
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
                s.coast_cfg      = vc.coast
            s.coast_open_threshold = s.level_low
            self._states[vessel] = s

        # Vessels with a coast block; empty tuple means none of the coast
        # subscriptions or publishes happen at all.
        self._coast_vessels: Tuple[str, ...] = tuple(
            v for v in VESSELS if self._states[v].coast_cfg is not None
        )

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

        if self._coast_vessels:
            # Exact channel strings, so this does not collide with the
            # interlocks controller's xsphere/sensors/temperature/# — the
            # subscription registry is keyed by topic string, and a duplicate
            # string would silently replace the other subscriber.
            for ch in COAST_TEMP_CHANNELS:
                self._mqtt.subscribe(
                    sensor_topic("temperature", ch),
                    self._on_coast_temp,
                )
            for vessel in self._coast_vessels:
                self._mqtt.subscribe(
                    command_topic("valve", vessel, "coast"),
                    self._on_coast_cmd,
                )
                self._mqtt.subscribe(
                    command_topic("valve", vessel, "coast_config"),
                    self._on_coast_config,
                )
                self._mqtt.subscribe(
                    status_topic("valve", vessel),
                    self._on_valve_status,
                )
            log.info("[autovalve] coast gate available for %s "
                     "(disarmed at startup)", ", ".join(self._coast_vessels))

        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._control_loop,
            name="autovalve-control",
            daemon=True,
        )
        self._thread.start()
        log.info("[autovalve] started")

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=10)
        # The coast permit is NOT released from here. Publishing an MQTT
        # command during shutdown races the broker round-trip against
        # mqtt.disconnect(), so the release is a direct register write in
        # PlcDriver._release_coast_permits(), which runs while the Modbus
        # connection is still open.
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
            s.level_time = time.monotonic()
            filtered = s.level_filtered

        # Republished under status/, NOT back onto xsphere/sensors/level/
        # {vessel}: that is the topic this callback subscribes to, so
        # publishing there fed our own filter its own output on every pass.
        self._mqtt.publish_status(
            "level", vessel,
            payload={"raw": round(raw, 4), "filtered": round(filtered, 4)},
            retain=False,
        )

        self._evaluate(vessel)

    def _on_coast_temp(self, topic: str, payload: dict) -> None:
        """Cache a temperature reading. Deliberately does not re-evaluate.

        Four RTDs at the PLC poll rate would otherwise drive four decisions a
        second, each from a different partial view of the cache.  The control
        loop re-evaluates on its own tick instead.
        """
        # xsphere/sensors/temperature/plc/rtd/2 → plc/rtd/2
        channel = "/".join(topic.split("/")[3:])
        value_k = payload.get("value_k")
        if value_k is None:
            return
        try:
            value_k = float(value_k)
        except (TypeError, ValueError):
            return
        with self._lock:
            self._temps[channel] = (value_k, time.monotonic())

    def _on_valve_status(self, topic: str, payload: dict) -> None:
        """Cache the PLC's DS1107 readback so the operator can see whether the
        coast permit write is actually landing."""
        if not isinstance(payload, dict):
            return
        vessel = topic.split("/")[-1]
        if vessel not in self._states:
            return
        permit = payload.get("coast_permit")
        with self._lock:
            self._states[vessel].ladder_permit_readback = (
                None if permit is None else int(permit)
            )

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
        """Track valve state when manually commanded (not from autofill).

        A manual command is honoured whatever coast thinks; the fill timeout
        still bounds it.
        """
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
        self._evaluate(vessel)

    def _on_coast_cmd(self, topic: str, payload: dict) -> None:
        """xsphere/commands/valve/{vessel}/coast → {"enabled": bool}

        Never publish this topic retained: coast coming up disarmed after a
        restart is a safety property, and a retained arm would defeat it.
        """
        vessel = topic.split("/")[-2]
        s = self._states.get(vessel)
        if s is None or s.coast_cfg is None:
            return
        enabled = bool(payload.get("enabled", False))
        if enabled and not s.coast_cfg.enabled:
            msg = ("coast is disabled in config.yaml for this vessel — "
                   "arm request ignored")
            log.warning("[autovalve] %s: %s", vessel, msg)
            self._alert("coast_refused", vessel, msg)
            return
        if enabled and not s.coast_cfg.ladder_rung_entered:
            # Not fatal: the failure mode is the ladder topping the vessel up
            # during the descent, so coast quietly never engages. That wastes
            # LN2 rather than risking anything, but it looks exactly like a
            # broken feature, so say so loudly.
            log.warning(
                "[autovalve] %s: coast armed with ladder_rung_entered=false — "
                "the PLC ladder's own auto-open will refill through the "
                "0.25 < level < 0.8 window and coast will never see an empty "
                "vessel. Enter CLICK ladder addition 6 (see "
                "SYSTEM_ARCHITECTURE.md) or disarm the ladder's auto_open.",
                vessel)
        with self._lock:
            s.coast_armed = enabled
            if not enabled:
                s.coast_since = None
                s.gate_armed = False
                s.gate_true_since = None
                s.coast_latched_off = False
                s.coast_release_reason = None
        log.info("[autovalve] %s coast → %s", vessel, enabled)
        self._clear_alert("coast_refused", vessel)
        self._evaluate(vessel)
        self._publish_coast_status(vessel)

    def _on_coast_config(self, topic: str, payload: dict) -> None:
        """xsphere/commands/valve/{vessel}/coast_config → {"delta_max_k": X}

        Applies operator-facing gate keys only, all-or-nothing, validated by
        the same predicates the file loader uses.  Nothing is persisted: after
        a restart config.yaml wins.
        """
        vessel = topic.split("/")[-2]
        s = self._states.get(vessel)
        if s is None or s.coast_cfg is None or not isinstance(payload, dict):
            return

        rejected = sorted(set(payload) - COAST_TUNABLE)
        if rejected:
            msg = (f"coast_config keys {rejected} are not runtime-tunable; "
                   f"tunable keys are {sorted(COAST_TUNABLE)}")
            log.warning("[autovalve] %s: %s", vessel, msg)
            self._alert("coast_refused", vessel, msg)
            return

        updates = {}
        for key, value in payload.items():
            current = getattr(s.coast_cfg, key)
            try:
                updates[key] = value if isinstance(current, str) else float(value)
            except (TypeError, ValueError):
                msg = f"coast_config {key}={value!r} is not numeric"
                log.warning("[autovalve] %s: %s", vessel, msg)
                self._alert("coast_refused", vessel, msg)
                return

        trial = replace(s.coast_cfg, **updates)
        vessel_cfg = self._config.autovalve.vessels.get(vessel)
        if vessel_cfg is None:
            return
        try:
            validate_vessel(vessel, replace(vessel_cfg, coast=trial))
        except ValueError as exc:
            log.warning("[autovalve] %s: coast_config rejected — %s",
                        vessel, exc)
            self._alert("coast_refused", vessel, str(exc))
            return

        with self._lock:
            s.coast_cfg = trial
            # A changed threshold invalidates a dwell measured against the old
            # one, so the gate re-earns its confirm_s.
            s.gate_armed = False
            s.gate_true_since = None
        log.info("[autovalve] %s coast_config applied: %s", vessel, updates)
        self._clear_alert("coast_refused", vessel)
        self._evaluate(vessel)
        self._publish_coast_status(vessel)

    # ------------------------------------------------------------------
    # Autofill logic
    # ------------------------------------------------------------------

    def _snapshot(self, vessel: str, now: float) -> CoastSnapshot:
        """Sample everything the gate may look at. Caller holds the lock."""
        s = self._states[vessel]
        return CoastSnapshot(
            now=now,
            armed=s.coast_armed,
            auto_open=s.auto_open_en,
            latched_off=s.coast_latched_off,
            level=s.level_filtered if s.filter_init else None,
            level_age_s=None if s.level_time is None else now - s.level_time,
            temps={ch: (value, now - stamp)
                   for ch, (value, stamp) in self._temps.items()},
            coast_since=s.coast_since,
            gate_armed=s.gate_armed,
            gate_true_since=s.gate_true_since,
        )

    def _refill_proven(self, s: VesselState, snap: CoastSnapshot) -> bool:
        """True when the vessel is demonstrably wet again.

        Requires two independent sensors to agree — the level channel at its
        high threshold and the cold sink back down on the LN2 plateau — so a
        single stuck channel cannot re-arm a coast that a backstop stopped.
        """
        c = s.coast_cfg
        if c is None or not s.filter_init or s.level_filtered < s.level_high:
            return False
        entry = snap.temps.get(c.sensor_cold)
        if entry is None:
            return False
        value, age = entry
        return age <= c.temp_stale_s and value < c.sink_cold_k

    def _evaluate(self, vessel: str) -> None:
        """Re-evaluate whether to open or close the valve for this vessel.

        Decision and actuation happen under one hold of the re-entrant lock,
        so no concurrent level message, command, or control tick can land
        between the check and the write.
        """
        now = time.monotonic()
        with self._lock:
            s = self._states[vessel]
            snap = self._snapshot(vessel, now)
            d = coast_decide(snap, s.coast_cfg, s.level_low)

            s.coast_state = d.state
            s.coast_reason = d.reason
            s.coast_since = d.coast_since
            s.gate_armed = d.gate_armed
            s.gate_true_since = d.gate_true_since
            s.coast_open_threshold = d.open_threshold
            s.coast_permit_open = d.permit_open
            s.coast_t_warm_k = d.t_warm_k
            s.coast_t_cold_k = d.t_cold_k
            s.coast_delta_k = d.delta_k
            s.coast_ceiling_max_k = d.ceiling_max_k

            if d.latch and not s.coast_latched_off:
                s.coast_latched_off = True
                s.coast_release_reason = d.reason
                log.warning("[autovalve] %s coast backstop — %s",
                            vessel, d.reason)
            elif s.coast_latched_off and self._refill_proven(s, snap):
                log.info("[autovalve] %s refilled and cold — coast re-armable",
                         vessel)
                s.coast_latched_off = False
                s.coast_release_reason = None
                s.coast_since = None
                s.gate_armed = False
                s.gate_true_since = None

            # Never act on a level that has not been measured. _evaluate now
            # also runs on a timer, so without this an armed auto_open would
            # open the valve against the 0.0 the filter starts at.
            level_known = s.filter_init
            level_fresh = (level_known and s.level_time is not None
                           and now - s.level_time <= LEVEL_TRUST_S)

            # CLOSE has absolute priority and coast is never consulted for it.
            # A stale-but-known level is enough to close on: shutting a valve
            # is safe in every state.
            if (s.auto_close_en and s.valve_open and level_known
                    and s.level_filtered >= s.level_high):
                log.info("[autovalve] %s full (%.3f >= %.3f) → close",
                         vessel, s.level_filtered, s.level_high)
                self._set_valve(vessel, False)
            elif (s.auto_open_en and not s.valve_open and d.permit_open
                    and level_fresh
                    and s.level_filtered < d.open_threshold):
                log.info("[autovalve] %s low (%.3f < %.3f) → open (%s)",
                         vessel, s.level_filtered, d.open_threshold, d.reason)
                self._set_valve(vessel, True)

            permit = d.ladder_permit
            alert = d.alert
            reason = d.reason

        if s.coast_cfg is not None:
            self._write_ladder_permit(vessel, permit)
            for rule in COAST_ALERT_RULES:
                if rule == alert:
                    self._alert(rule, vessel, reason)
                elif rule != "coast_refused":
                    # coast_refused is owned by the command handlers, not by
                    # the gate, so the gate must not clear it.
                    self._clear_alert(rule, vessel)

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

    def _write_ladder_permit(self, vessel: str, permit: int,
                             force: bool = False) -> None:
        """Command DS1107, the ladder's coast permit, via the PLC driver.

        Idempotent — written on change, and re-asserted once per control tick
        so a dropped message self-heals within CONTROL_TICK_S.
        """
        with self._lock:
            s = self._states[vessel]
            if not force and s.ladder_permit == permit:
                return
            changed = s.ladder_permit != permit
            s.ladder_permit = permit
        if changed:
            log.info("[autovalve] %s ladder coast permit → %d", vessel, permit)
        self._mqtt.publish(
            command_topic("valve", vessel, "coast_permit"),
            {"enabled": int(permit)},
            qos=1,
        )

    # ------------------------------------------------------------------
    # Control loop — fill timeout safety + coast re-evaluation
    # ------------------------------------------------------------------

    def _control_loop(self) -> None:
        """Check fill timeouts and re-evaluate every vessel on a fixed tick.

        The periodic re-evaluation is what lets a temperature-only change move
        the valve: _evaluate is otherwise only reached from a level message or
        an operator command, and the coast gate's second term does not live on
        either of those.
        """
        while not self._stop_event.wait(CONTROL_TICK_S):
            now = time.monotonic()
            for vessel in VESSELS:
                with self._lock:
                    s = self._states[vessel]
                    if not s.valve_open or s.fill_start_time is None:
                        elapsed = None
                    else:
                        elapsed = now - s.fill_start_time
                    timeout = s.fill_timeout_s

                if elapsed is not None and elapsed > timeout:
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

                self._evaluate(vessel)

            for vessel in self._coast_vessels:
                # Re-assert the permit even when unchanged, so a lost message
                # cannot leave the ladder inhibited for longer than one tick.
                with self._lock:
                    permit = self._states[vessel].ladder_permit
                if permit is not None:
                    self._write_ladder_permit(vessel, permit, force=True)
                self._publish_coast_status(vessel)

    # ------------------------------------------------------------------
    # Coast status + alerts
    # ------------------------------------------------------------------

    def _publish_coast_status(self, vessel: str) -> None:
        """Publish the retained coast document for the dashboard.

        Retained, so the answer to "why has this not refilled yet" is on
        screen the instant the dashboard loads rather than one tick later.
        """
        now = time.monotonic()
        with self._lock:
            s = self._states[vessel]
            c = s.coast_cfg
            if c is None:
                return
            elapsed = (None if s.coast_since is None
                       else round(now - s.coast_since, 1))
            payload = {
                "vessel":           vessel,
                "enabled":          c.enabled,
                "armed":            s.coast_armed,
                "state":            s.coast_state,
                "hold_reason":      s.coast_reason,
                "permit_open":      s.coast_permit_open,
                "open_threshold":   round(s.coast_open_threshold, 4),
                "level":            (round(s.level_filtered, 4)
                                     if s.filter_init else None),
                "empty_threshold":  c.empty_threshold,
                "sensor_warm":      c.sensor_warm,
                "sensor_cold":      c.sensor_cold,
                "t_warm_k":         _round(s.coast_t_warm_k, 2),
                "t_cold_k":         _round(s.coast_t_cold_k, 2),
                "delta_k":          _round(s.coast_delta_k, 2),
                "delta_max_k":      c.delta_max_k,
                "delta_mode":       c.delta_mode,
                "confirm_s":        c.confirm_s,
                "dwell_s":          (None if s.gate_true_since is None
                                     else round(now - s.gate_true_since, 1)),
                "elapsed_s":        elapsed,
                "max_duration_s":   c.max_duration_s,
                "ceiling_k":        c.ceiling_k,
                "ceiling_max_k":    _round(s.coast_ceiling_max_k, 2),
                "sink_warm_k":      c.sink_warm_k,
                "latched_off":      s.coast_latched_off,
                "release_reason":   s.coast_release_reason,
                "ladder_rung_entered":    c.ladder_rung_entered,
                "ladder_permit":          s.ladder_permit,
                "ladder_permit_readback": s.ladder_permit_readback,
            }
        self._mqtt.publish_status("coast", vessel, payload=payload)

    def _alert(self, rule: str, vessel: str, msg: str) -> None:
        key = f"{rule}/{vessel}"
        if key in self._active_alerts:
            return
        self._active_alerts.add(key)
        self._mqtt.publish(
            f"xsphere/alerts/{key}",
            {"rule": rule, "vessel": vessel, "msg": msg,
             "timestamp": time.time()},
            qos=1,
            retain=True,
        )

    def _clear_alert(self, rule: str, vessel: str) -> None:
        key = f"{rule}/{vessel}"
        if key not in self._active_alerts:
            return
        self._active_alerts.discard(key)
        self._mqtt.publish(f"xsphere/alerts/{key}", "", qos=1, retain=True)


def _round(value: Optional[float], digits: int) -> Optional[float]:
    return None if value is None else round(value, digits)
