"""
Arms, the auto-disarm supervisor, LN2 open timers and heater ramps.

**Arms.**  Three groups — heaters, ln2, pneumatic — each armed by a human on
the Control page (MQTT ``<cmd_prefix>/arm``).  Every group starts disarmed at
every service start and nothing about an arm is ever written to disk, the
same as the PLC's pneumatic and turbo gates.  :meth:`ArmState.is_armed` is a
plain read of two values (the flag and the expiry time) with no lock, so the
action pipeline can re-check it immediately before every single publish and a
disarm arriving on the MQTT thread is never queued behind a write that is
waiting for a confirmation.

**Supervisor.**  Disarms by itself — it never arms — whenever the conditions
the human armed under no longer hold:

* all groups: the arm expires (``general.arm_hours``); this service
  reconnects to the broker (a disarm sent while we were away would otherwise
  be lost: there is no persistent session); slowcontrol restarts (heartbeat
  ``uptime_s`` went down, or ``service_alive`` came back) — a restart clears
  the PID expressions and the PV interlock drops every setpoint to 3 K, and an
  armed AI should not "restore" setpoints into that; the snapshot is older
  than 30 s;
* heaters: the PV interlock trips (``tripped`` false → true on a live
  message), or ``xsphere/status/interlocks`` turns not-ok.

Each automatic disarm is audited (kind ``auto_disarm``), counted and shown as
the group's ``last_disarm``.

**LN2 open timers.**  When the AI opens a fill valve, the open time and a
deadline ``max_open_minutes`` later are persisted in ``runtime.json`` (before
the open is published).  At the deadline, if the valve is still open, the
supervisor turns ``auto_open`` off and closes the valve — the service's own
safety action, done even while ln2 is disarmed — audits it and disarms ln2.
The PLC ladder's fill timer only runs with auto-close armed and the Python
autovalve is disabled, so this is the backstop for a valve the AI opened.
Timers are reloaded at start, so a restarted service still closes.

**Heater ramps.**  :class:`Ramps` holds in-memory ramp entries and runs a
thread that every 10 s asks the action layer (``Actions.ramp_step``) to move
each zone one step.  The step goes through the same validated setpoint path as
``set_heater_setpoints``; any refusal stops the ramp, and so do a heaters
disarm, an external change of the setpoint, reaching the target, a
``stop_heater_ramp`` call or a direct AI setpoint for that zone.
"""

from __future__ import annotations

import copy
import json
import logging
import math
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from .zones import VESSELS

log = logging.getLogger(__name__)

GROUPS = ("heaters", "ln2", "pneumatic")
#: Sentence subjects for refusals ("Heaters are not armed …").
GROUP_NAMES = {"heaters": "Heaters", "ln2": "LN2 valves", "pneumatic": "Pneumatic valves"}
CONTROL_PAGE = "http://xbox-pi:8088/control"

HEARTBEAT_TOPIC = "xsphere/status/service/heartbeat"
PV_INTERLOCK_TOPIC = "xsphere/status/pv_interlock"
INTERLOCKS_TOPIC = "xsphere/status/interlocks"

#: Snapshot older than this disarms everything.
SNAPSHOT_DISARM_AGE_S = 30.0
#: A valve status is "live" when delivered (not as a retained replay) this recently.
LIVE_STATUS_S = 3.0
#: After an open, the timer is not cleared by a "closed" status for this long
#: (the PLC may not have acted on the open yet).
TIMER_CLEAR_GRACE_S = 15.0
#: Re-send the auto-close while a valve stays open past its deadline.
AUTO_CLOSE_RETRY_S = 10.0


def utc_iso(ts: Optional[float]) -> Optional[str]:
    """Epoch seconds → ``2026-10-22T05:00:00Z`` (None stays None)."""
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _is_number(x: Any) -> bool:
    if isinstance(x, bool) or not isinstance(x, (int, float)):
        return False
    try:
        return math.isfinite(float(x))
    except OverflowError:
        return False


# ──────────────────────────────────────────────────────────────────────────
# ArmState
# ──────────────────────────────────────────────────────────────────────────

class ArmState:
    """Who may write what right now.  Never persisted; all off at start."""

    def __init__(self, rails, audit=None, *, clock: Callable[[], float] = time.time):
        self._rails = rails
        self._audit = audit
        self._clock = clock
        self._lock = threading.Lock()            # bookkeeping only, never held while waiting
        # Read without the lock by is_armed(): single dict lookups are atomic.
        self._flags: Dict[str, bool] = {g: False for g in GROUPS}
        self._expires: Dict[str, Optional[float]] = {g: None for g in GROUPS}
        self._info: Dict[str, dict] = {g: {"since_utc": None, "expires_utc": None, "by": None,
                                           "last_disarm": None} for g in GROUPS}
        self._listeners: List[Callable[[str, bool, Optional[str]], None]] = []
        self.auto_disarms = 0

    # reading -------------------------------------------------------------

    def is_armed(self, group: str) -> bool:
        """True while ``group`` is armed and its arm has not expired.  No locks."""
        if not self._flags.get(group, False):
            return False
        exp = self._expires.get(group)
        return exp is None or self._clock() < exp

    def armed_groups(self) -> List[str]:
        return [g for g in GROUPS if self.is_armed(g)]

    @property
    def arm_hours(self) -> Optional[float]:
        """How long a new arm lasts (``general.arm_hours`` of the rails in force)."""
        return self._rails.arm_hours

    def expired_groups(self) -> List[str]:
        """Groups still flagged armed whose expiry time has passed."""
        now = self._clock()
        return [g for g in GROUPS
                if self._flags[g] and self._expires[g] is not None and now >= self._expires[g]]

    def last_disarm(self, group: str) -> Optional[dict]:
        with self._lock:
            ld = self._info[group]["last_disarm"]
            return dict(ld) if ld else None

    def snapshot(self) -> Dict[str, dict]:
        """``{group: {armed, since_utc, expires_utc, by, last_disarm}}`` for the status."""
        out = {}
        with self._lock:
            info = copy.deepcopy(self._info)
        for g in GROUPS:
            armed = self.is_armed(g)
            out[g] = {"armed": armed,
                      "since_utc": info[g]["since_utc"] if armed else None,
                      "expires_utc": info[g]["expires_utc"] if armed else None,
                      "by": info[g]["by"] if armed else None,
                      "last_disarm": info[g]["last_disarm"]}
        return out

    def not_armed_text(self, group: str) -> str:
        """The refusal sentence for a write to a disarmed group."""
        text = (f"{GROUP_NAMES.get(group, group)} are not armed for AI control. Ask Lucas to "
                f"arm them on the Control page ({CONTROL_PAGE}).")
        ld = self.last_disarm(group)
        if ld and ld.get("reason"):
            text += f" Last disarmed {ld.get('utc')}: {ld['reason']}."
        return text

    # listeners -----------------------------------------------------------

    def add_listener(self, fn: Callable[[str, bool, Optional[str]], None]) -> None:
        """``fn(group, armed, reason)`` after every arm / disarm (any thread)."""
        self._listeners.append(fn)

    def _notify(self, group: str, armed: bool, reason: Optional[str]) -> None:
        for fn in list(self._listeners):
            try:
                fn(group, armed, reason)
            except Exception:
                log.exception("Error in arm listener")

    # changing ------------------------------------------------------------

    def arm(self, group: str, by: str = "unknown") -> None:
        if group not in GROUPS:
            raise ValueError(f"unknown group {group!r}")
        hours = self._rails.arm_hours
        now = self._clock()
        expires = now + float(hours) * 3600.0 if hours is not None else None
        with self._lock:
            again = self._flags[group] and self.is_armed(group)
            self._expires[group] = expires          # expiry before the flag
            self._info[group].update(since_utc=utc_iso(now), expires_utc=utc_iso(expires), by=by)
            self._flags[group] = True
        until = f"expires {utc_iso(expires)} ({hours:g} h)" if expires else "no time limit"
        detail = f"{group} {'re-armed' if again else 'armed'} by {by} — {until}"
        log.warning("AI control: %s", detail)
        if self._audit is not None:
            self._audit.record("arm", detail, by=by)
        self._notify(group, True, None)

    def disarm(self, group: str, reason: str, *, by: Optional[str] = None,
               auto: bool = False) -> bool:
        """Disarm one group; True when it was armed (flagged) before."""
        return bool(self._disarm([group], reason, by=by, auto=auto))

    def disarm_all(self, reason: str, *, by: Optional[str] = None, auto: bool = False) -> List[str]:
        """Disarm every group; returns the groups that were armed."""
        return self._disarm(list(GROUPS), reason, by=by, auto=auto)

    def _disarm(self, groups: List[str], reason: str, *, by: Optional[str],
                auto: bool) -> List[str]:
        now_utc = utc_iso(self._clock())
        changed = []
        with self._lock:
            for g in groups:
                if g not in GROUPS:
                    continue
                was = self._flags[g]
                self._flags[g] = False              # flag first, then the rest
                self._expires[g] = None
                if was:
                    changed.append(g)
                    self._info[g]["last_disarm"] = {"reason": reason, "utc": now_utc}
            if changed and auto:
                self.auto_disarms += 1
        if changed:
            detail = f"{', '.join(changed)} disarmed — {reason}"
            log.warning("AI control: %s", detail)
            if self._audit is not None:
                self._audit.record("auto_disarm" if auto else "disarm", detail, by=by)
            for g in changed:
                self._notify(g, False, reason)
        else:
            log.info("AI control: disarm (%s) of %s — nothing was armed", reason, ", ".join(groups))
        return changed

    def handle_command(self, payload: Any) -> None:
        """The human's MQTT ``<cmd_prefix>/arm`` message:
        ``{"group": "heaters"|"ln2"|"pneumatic"|"all", "armed": true|false, "by"?: str}``.

        ``armed`` must be a JSON bool (0 / 1 / "true" are refused, as the
        drivers' own gates do); ``"all"`` can only be disarmed.
        """
        if not isinstance(payload, dict):
            log.warning("Ignoring arm command: not a JSON object")
            return
        raw_by = payload.get("by")
        by = raw_by.strip()[:80] if isinstance(raw_by, str) and raw_by.strip() else "unknown"
        group, armed = payload.get("group"), payload.get("armed")
        unknown = sorted(str(k) for k in payload if k not in ("group", "armed", "by"))
        problem = None
        if unknown:
            problem = f"unknown key {unknown[0]!r}"
        elif not isinstance(group, str) or group not in GROUPS + ("all",):
            problem = "group must be heaters, ln2, pneumatic or all"
        elif not isinstance(armed, bool):
            problem = "armed must be true or false (a JSON bool)"
        elif group == "all" and armed:
            problem = '"all" can only be disarmed'
        if problem:
            log.warning("Ignoring arm command %r from %s: %s", payload, by, problem)
            if self._audit is not None:
                self._audit.record("arm", f"arm command ignored: {problem}", by=by,
                                   args=payload)
            return
        if group == "all":
            self.disarm_all(f"disarmed by {by}", by=by)
        elif armed:
            self.arm(group, by)
        else:
            self.disarm(group, f"disarmed by {by}", by=by)


# ──────────────────────────────────────────────────────────────────────────
# LN2 open timers (runtime.json)
# ──────────────────────────────────────────────────────────────────────────

class Ln2Timers:
    """``{vessel: {opened_ts, deadline_ts, opened_utc, deadline_utc, max_open_minutes}}``
    persisted atomically in ``runtime.json`` so a restarted service still closes."""

    def __init__(self, path, *, clock: Callable[[], float] = time.time,
                 on_change: Optional[Callable[[], None]] = None):
        self.path = Path(path)
        self._clock = clock
        self._lock = threading.Lock()
        self._timers: Dict[str, dict] = {}
        self._on_change = on_change
        self.error: Optional[str] = None

    def set_on_change(self, fn: Optional[Callable[[], None]]) -> None:
        self._on_change = fn

    def load(self) -> None:
        """Read runtime.json.  A broken file is logged and reported, never fatal."""
        try:
            text = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return
        except OSError as e:
            self.error = f"runtime.json unreadable: {e}"
            log.error("%s — LN2 auto-close timers from before the restart are lost", self.error)
            return
        try:
            doc = json.loads(text)
            timers = doc.get("ln2_timers", {}) if isinstance(doc, dict) else None
            if not isinstance(timers, dict):
                raise ValueError("ln2_timers is not an object")
        except ValueError as e:
            self.error = f"runtime.json invalid: {e}"
            log.error("%s — LN2 auto-close timers from before the restart are lost", self.error)
            return
        loaded = {}
        for vessel, t in timers.items():
            if vessel not in VESSELS:
                log.error("runtime.json: ignoring an LN2 timer for unknown vessel %r", vessel)
                continue
            if (isinstance(t, dict) and _is_number(t.get("opened_ts"))
                    and _is_number(t.get("deadline_ts"))):
                loaded[str(vessel)] = {
                    "opened_ts": float(t["opened_ts"]), "deadline_ts": float(t["deadline_ts"]),
                    "opened_utc": utc_iso(float(t["opened_ts"])),
                    "deadline_utc": utc_iso(float(t["deadline_ts"])),
                    "max_open_minutes": t.get("max_open_minutes"),
                    "close_attempts": 0, "last_attempt_ts": None}
            else:
                log.error("runtime.json: ignoring malformed LN2 timer for %r: %r", vessel, t)
        with self._lock:
            self._timers = loaded
        if loaded:
            log.warning("LN2 auto-close timers reloaded: %s",
                        ", ".join(f"{v} (deadline {t['deadline_utc']})" for v, t in loaded.items()))

    def set(self, vessel: str, minutes: float, opened_ts: Optional[float] = None) -> dict:
        now = self._clock() if opened_ts is None else opened_ts
        deadline = now + float(minutes) * 60.0
        entry = {"opened_ts": now, "deadline_ts": deadline, "opened_utc": utc_iso(now),
                 "deadline_utc": utc_iso(deadline), "max_open_minutes": minutes,
                 "close_attempts": 0, "last_attempt_ts": None}
        with self._lock:
            self._timers[vessel] = entry
            self._save_locked()
        self._changed()
        return dict(entry)

    def clear(self, vessel: str) -> bool:
        with self._lock:
            had = self._timers.pop(vessel, None) is not None
            if had:
                self._save_locked()
        if had:
            self._changed()
        return had

    def note_attempt(self, vessel: str, ts: float) -> int:
        with self._lock:
            t = self._timers.get(vessel)
            if t is None:
                return 0
            t["close_attempts"] += 1
            t["last_attempt_ts"] = ts
            return t["close_attempts"]

    def get(self, vessel: str) -> Optional[dict]:
        with self._lock:
            t = self._timers.get(vessel)
            return dict(t) if t else None

    def all(self) -> Dict[str, dict]:
        with self._lock:
            return {v: dict(t) for v, t in self._timers.items()}

    def status(self) -> Dict[str, dict]:
        """``{vessel: {opened_utc, deadline_utc}}`` for the status payload."""
        with self._lock:
            return {v: {"opened_utc": t["opened_utc"], "deadline_utc": t["deadline_utc"]}
                    for v, t in self._timers.items()}

    def _changed(self) -> None:
        if self._on_change is not None:
            try:
                self._on_change()
            except Exception:
                log.exception("Error in LN2 timer change callback")

    def _save_locked(self) -> None:
        keys = ("opened_ts", "deadline_ts", "opened_utc", "deadline_utc", "max_open_minutes")
        doc = {"ln2_timers": {v: {k: t[k] for k in keys} for v, t in self._timers.items()}}
        tmp = self.path.with_name(self.path.name + ".tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(json.dumps(doc, indent=2, allow_nan=False) + "\n")
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.path)
        except OSError as e:
            # The timer still runs in memory; only a restart would lose it.
            log.error("Could not save LN2 timers to %s: %s", self.path, e)


# ──────────────────────────────────────────────────────────────────────────
# Supervisor
# ──────────────────────────────────────────────────────────────────────────

class Supervisor:
    """Auto-disarm triggers and the LN2 auto-close, on a 1 s tick + bus observers."""

    def __init__(self, bus, arms: ArmState, audit, zones, timers: Ln2Timers, *,
                 clock: Callable[[], float] = time.time, tick_s: float = 1.0):
        self._bus = bus
        self._arms = arms
        self._audit = audit
        self._zones = zones
        self._timers = timers
        self._clock = clock
        self._tick_s = tick_s
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._last_uptime: Optional[float] = None
        self._pv_tripped: Optional[bool] = None
        self._interlocks_ok: Optional[bool] = None
        self._alive_prev: Optional[bool] = None
        bus.on_connect_hook(self._on_connect)
        bus.on_status(HEARTBEAT_TOPIC, self._on_heartbeat)
        bus.on_status(PV_INTERLOCK_TOPIC, self._on_pv_interlock)
        bus.on_status(INTERLOCKS_TOPIC, self._on_interlocks)

    # thread --------------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="aicontrol-supervisor",
                                        daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.wait(self._tick_s):
            try:
                self.tick()
            except Exception:
                log.exception("Supervisor tick failed")

    # bus observers (paho thread: no waiting) --------------------------------

    def _live(self, topic: str) -> bool:
        r = self._bus.raw(topic)
        return bool(r) and not r["retained"]

    def _on_connect(self, is_reconnect: bool) -> None:
        if is_reconnect:
            self._arms.disarm_all("the AI service lost its MQTT broker connection "
                                  "(a disarm sent meanwhile could have been missed)", auto=True)
        # Edge baselines restart from what the broker replays.
        self._last_uptime = None

    def _on_heartbeat(self, topic: str, payload: Any) -> None:
        if not self._live(topic):
            return                      # a retained replay says nothing about now
        up = payload.get("uptime_s") if isinstance(payload, dict) else None
        if not _is_number(up):
            return
        prev, self._last_uptime = self._last_uptime, float(up)
        if prev is not None and float(up) < prev:
            self._arms.disarm_all(f"slowcontrol restarted (heartbeat uptime {prev:.0f} s → "
                                  f"{float(up):.0f} s)", auto=True)

    def _on_pv_interlock(self, topic: str, payload: Any) -> None:
        if not isinstance(payload, dict):
            return
        tripped = payload.get("tripped") is True
        prev, self._pv_tripped = self._pv_tripped, tripped
        if tripped and prev is not True and self._live(topic):
            reasons = payload.get("reasons")
            why = "; ".join(f"RTD{k}: {v}" for k, v in reasons.items()) \
                if isinstance(reasons, dict) and reasons else "no reason given"
            self._arms.disarm("heaters", f"the PV interlock tripped ({why})", auto=True)

    def _on_interlocks(self, topic: str, payload: Any) -> None:
        if not isinstance(payload, dict) or not isinstance(payload.get("ok"), bool):
            return
        ok = payload["ok"]
        prev, self._interlocks_ok = self._interlocks_ok, ok
        if ok is False and prev is not False and self._live(topic):
            rules = payload.get("rules_active")
            names = ", ".join(map(str, rules)) if isinstance(rules, list) and rules else "?"
            self._arms.disarm("heaters", f"slowcontrol interlocks turned not-ok ({names})",
                              auto=True)

    # tick ----------------------------------------------------------------

    def tick(self) -> None:
        now = self._clock()
        for g in self._arms.expired_groups():
            self._arms.disarm(g, "the arm expired", auto=True)
        if self._arms.armed_groups():
            age = self._bus.snapshot_age()
            if age is None:
                self._arms.disarm_all("no slowcontrol snapshot has been received", auto=True)
            elif age > SNAPSHOT_DISARM_AGE_S:
                self._arms.disarm_all(f"the slowcontrol snapshot is {age:.0f} s old "
                                      f"(> {SNAPSHOT_DISARM_AGE_S:.0f} s)", auto=True)
        self._check_service_alive()
        self._check_timers(now)

    def _check_service_alive(self) -> None:
        snap_age = self._bus.snapshot_age()
        if snap_age is None or snap_age > 5.0:
            return
        view = self._bus.state("service_alive")
        if view is None:
            return
        # Re-reading the same snapshot gives the same value, so no false edge.
        alive = view.get("value") is True and view.get("freshness") == "fresh"
        prev, self._alive_prev = self._alive_prev, alive
        if prev is False and alive:
            self._arms.disarm_all("slowcontrol restarted (its heartbeat came back)", auto=True)

    # LN2 auto-close -------------------------------------------------------

    def _check_timers(self, now: float) -> None:
        for vessel, t in self._timers.all().items():
            try:
                self._check_timer(vessel, t, now)
            except Exception:                        # one bad timer must not stop the others
                log.exception("LN2 auto-close check for %s failed", vessel)

    def _check_timer(self, vessel: str, t: dict, now: float) -> None:
        v = self._zones.vessels.get(vessel) if self._zones is not None else None
        topic = v["status_topic"] if v else f"xsphere/status/valve/{vessel}"
        prefix = v["command_prefix"] if v else f"xsphere/commands/valve/{vessel}"
        st = self._bus.live_payload(topic, LIVE_STATUS_S)
        closed = (isinstance(st, dict) and st.get("state") == 0 and st.get("desired") == 0)
        if closed and now - t["opened_ts"] >= TIMER_CLEAR_GRACE_S:
            self._timers.clear(vessel)
            if t["close_attempts"]:
                log.warning("LN2 %s: closed after the service's auto-close", vessel)
            else:
                log.info("LN2 %s: valve shows closed — AI open timer cleared", vessel)
            return
        if now < t["deadline_ts"]:
            return
        last = t.get("last_attempt_ts")
        if last is not None and now - last < AUTO_CLOSE_RETRY_S:
            return
        self._auto_close(vessel, t, st, prefix, now)

    def _auto_close(self, vessel: str, t: dict, st: Any, prefix: str, now: float) -> None:
        attempt = self._timers.note_attempt(vessel, now)
        published = []
        # auto_open off first (if on, or unknown), then the valve: both are
        # the safe direction, so this runs whether ln2 is armed or not.
        if not (isinstance(st, dict) and st.get("auto_open") == 0):
            payload = {"enabled": False}
            if self._bus.publish_command(f"{prefix}/auto_open", payload):
                published.append({"topic": f"{prefix}/auto_open", "payload": payload})
        payload = {"state": 0}
        ok = self._bus.publish_command(f"{prefix}/state", payload)
        if ok:
            published.append({"topic": f"{prefix}/state", "payload": payload})
        minutes = t.get("max_open_minutes")
        open_min = (now - t["opened_ts"]) / 60.0
        if attempt == 1:
            detail = (f"LN2 {vessel}: auto-closed after {open_min:.1f} min (the AI opened it "
                      f"{t['opened_utc']}; max_open_minutes {minutes})"
                      + ("" if ok else " — PUBLISH FAILED, retrying"))
            log.warning(detail)
            if self._audit is not None:
                self._audit.record("auto_close", detail, published=published,
                                   vessel=vessel)
            self._arms.disarm("ln2", f"LN2 {vessel} was still open at its "
                                     f"{minutes} min limit and was closed by the service",
                              auto=True)
        else:
            log.warning("LN2 %s: still open %.1f min after the AI opened it — close re-sent "
                        "(attempt %d)%s", vessel, open_min, attempt,
                        "" if ok else ", publish FAILED")
            if attempt in (3, 10) and self._audit is not None:
                self._audit.record("auto_close", f"LN2 {vessel}: still open after "
                                   f"{attempt} close attempts — check the valve",
                                   published=published, vessel=vessel)


# ──────────────────────────────────────────────────────────────────────────
# Heater ramps
# ──────────────────────────────────────────────────────────────────────────

class Ramps:
    """In-memory heater ramps, stepped by a thread through ``stepper``.

    ``stepper(zone, entry) -> (outcome, detail)`` is ``Actions.ramp_step``;
    outcome is ``"stepped"`` (it called :meth:`update`), ``"busy"`` (try again
    next tick), ``"done"`` (target reached) or ``"stop"`` (detail = why).
    """

    def __init__(self, arms: ArmState, audit=None, *, clock: Callable[[], float] = time.time,
                 interval_s: float = 10.0, on_change: Optional[Callable[[], None]] = None):
        self._arms = arms
        self._audit = audit
        self._clock = clock
        self.interval_s = float(interval_s)
        self._lock = threading.Lock()
        self._entries: Dict[str, dict] = {}
        self._next_id = 1
        self._stepper: Optional[Callable[[str, dict], Tuple[str, Any]]] = None
        self._on_change = on_change
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        arms.add_listener(self._on_arm_change)

    def set_stepper(self, fn: Callable[[str, dict], Tuple[str, Any]]) -> None:
        self._stepper = fn

    def set_on_change(self, fn: Optional[Callable[[], None]]) -> None:
        self._on_change = fn

    # thread --------------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="aicontrol-ramps", daemon=True)
        self._thread.start()

    def stop_thread(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.wait(self.interval_s):
            try:
                self.tick()
            except Exception:
                log.exception("Ramp tick failed")

    # entries -------------------------------------------------------------

    def start_ramp(self, zone: str, target_k: float, rate_k_per_min: float, reason: str,
                   start_expr: str, current_k: float) -> dict:
        now = self._clock()
        with self._lock:
            entry = {"id": self._next_id, "zone": zone, "target_k": float(target_k),
                     "rate_k_per_min": float(rate_k_per_min), "reason": reason,
                     "started_ts": now, "started_utc": utc_iso(now), "start_expr": start_expr,
                     "start_k": float(current_k), "current_k": float(current_k),
                     "last_written_k": None, "last_step_ts": None, "steps": 0}
            self._next_id += 1
            replaced = self._entries.get(zone)
            self._entries[zone] = entry
        if replaced:
            log.info("Ramp %s replaced by a new one", zone)
        self._changed()
        return dict(entry)

    def is_active(self, zone: str, ramp_id: Optional[int] = None) -> bool:
        with self._lock:
            e = self._entries.get(zone)
            return e is not None and (ramp_id is None or e["id"] == ramp_id)

    def get(self, zone: str) -> Optional[dict]:
        with self._lock:
            e = self._entries.get(zone)
            return dict(e) if e else None

    def update(self, zone: str, ramp_id: int, **fields: Any) -> None:
        with self._lock:
            e = self._entries.get(zone)
            if e is not None and e["id"] == ramp_id:
                e.update(fields)
        self._changed()

    def active_zones(self) -> List[str]:
        with self._lock:
            return list(self._entries)

    def status(self) -> List[dict]:
        """``[{zone, target_k, rate_k_per_min, current_k, started_utc, steps, reason}]``."""
        with self._lock:
            return [{"zone": e["zone"], "target_k": e["target_k"],
                     "rate_k_per_min": e["rate_k_per_min"], "current_k": e["current_k"],
                     "started_utc": e["started_utc"], "steps": e["steps"], "reason": e["reason"]}
                    for e in self._entries.values()]

    def stop(self, zones: Optional[List[str]], reason: str, *, audit: bool = True,
             kind: str = "ramp") -> List[str]:
        """Stop the ramps of ``zones`` (None = all); returns the zones stopped."""
        with self._lock:
            wanted = list(self._entries) if zones is None else zones
            names = [z for z in wanted if z in self._entries]
            stopped = {z: self._entries.pop(z) for z in names}
        for z, e in stopped.items():
            detail = (f"ramp {z} → {e['target_k']:g} K stopped at {e['current_k']:g} K after "
                      f"{e['steps']} step(s): {reason}")
            log.warning(detail)
            if audit and self._audit is not None:
                self._audit.record(kind, detail, zone=z)
        if stopped:
            self._changed()
        return list(stopped)

    def _finish(self, zone: str, ramp_id: int, detail: str) -> None:
        with self._lock:
            e = self._entries.get(zone)
            if e is None or e["id"] != ramp_id:
                return
            self._entries.pop(zone)
        text = f"ramp {zone} reached {e['target_k']:g} K after {e['steps']} step(s)"
        log.info(text)
        if self._audit is not None:
            self._audit.record("ramp", text + (f" ({detail})" if detail else ""), zone=zone)
        self._changed()

    def _on_arm_change(self, group: str, armed: bool, reason: Optional[str]) -> None:
        if group == "heaters" and not armed:
            self.stop(None, f"heaters disarmed ({reason})")

    def _changed(self) -> None:
        if self._on_change is not None:
            try:
                self._on_change()
            except Exception:
                log.exception("Error in ramp change callback")

    # stepping ------------------------------------------------------------

    def tick(self) -> None:
        stepper = self._stepper
        if stepper is None:
            return
        for zone in self.active_zones():
            entry = self.get(zone)
            if entry is None:
                continue
            try:
                outcome, detail = stepper(zone, entry)
            except Exception as e:                   # never leave a ramp running blind
                log.exception("Ramp step for %s failed", zone)
                outcome, detail = "stop", f"internal error: {e}"
            if outcome == "stop":
                with self._lock:
                    still = self._entries.get(zone, {}).get("id") == entry["id"]
                if still:
                    self.stop([zone], str(detail))
            elif outcome == "done":
                self._finish(zone, entry["id"], str(detail or ""))
