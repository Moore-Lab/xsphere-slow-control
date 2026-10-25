"""
The physical-state guard: gas pressure and the plausibility of PID PV sources.

Rails bound what the AI may command; they say nothing about the xenon.  This
module adds the checks that look at the physics.

**Pressure guard** (:class:`PressureGuard`, rails section ``guard``, deny by
default).  It reads ``p_xe_cube`` (Setra #1) and ``p_primary_bottle`` (Setra
#2) from the slowcontrol snapshot — the GHS ESP32 is their only source, and it
drops out for minutes to hours — and treats a reading as:

* *unknown* when missing, not fresh in slowcontrol, older than
  ``guard.fresh_s``, or above its gauge's range (not physical);
* *at the clip* when at or above 0.97 × the gauge's clip (zones.SETRA_CLIP_MBAR,
  identified from the Setra channel the state reads): the real pressure may be
  anything higher, so it counts as above every limit.

Write preconditions (actions.py asks before publishing):

* a *warming* heater write (setpoint or ramp up, auto mode, PV source, supply
  on or V / I up) needs the guard set up, both gauges known and below their
  ``warn_mbar``, and — with xenon in the system (``p_xe_cube`` ≥
  ``xenon_present_mbar``) — the ballast and bottle valves open;
* closing the ballast or bottle valve (the xenon's walk-away buffer) is refused
  while xenon is present or the cube pressure is unknown;
* opening the pump valve needs both gauges known and ≤ ``pump_open_max_mbar``;
* pump + ballast and pump + bottle open together are forbidden unless
  ``guard.pump_open_with`` names the partner.

Supervision (:meth:`PressureGuard.tick`, every supervisor tick): a gauge at or
above its ``trip_mbar`` (or at its clip) trips the guard once — the service
stops every ramp, disarms heaters and pneumatic, and sets each zone whose
setpoint is still the AI's last one to that zone's ``setpoint_min_k`` (a safety
action like the LN2 auto-close, done armed or not).  The trip re-arms when the
gauge reads below ``warn_mbar`` again.  With ``blind_disarm_s`` set, a gauge
with no fresh reading for that long disarms heaters and pneumatic.

**PV-source plausibility** (:class:`PvCheck`).  A PID regulating on a dead
sensor heats or cools open-loop (clamp RTD5 has read a constant 200.75 K since
2026-10-12).  A sensor is implausible when it reads outside 50–350 K, when a
clamp differs from its zone's cube RTD by more than ``heaters.pv_cross_check_k``
(15 K when unset, for the re-check only), or when it has stayed flat (≤ 0.2 K)
while the zone's other sensor moved ≥ 2 K within 30 min.  ``set_pv_source``
refuses such a source; every 10 s the current source of each zone is checked
again, and when one fails the service disarms the heaters and — if the AI chose
that source — puts the zone back on its cube RTD.
"""

from __future__ import annotations

import collections
import logging
import math
import threading
import time
from typing import Any, Callable, Deque, Dict, List, Optional, Tuple

from .rails import PUMP_PARTNERS
from .zones import GAUGES, ZONES

log = logging.getLogger(__name__)

CLIP_FRACTION = 0.97
#: A reading this far above its gauge's clip is not physical.
OVER_RANGE_FRACTION = 1.02
VALVE_LIVE_S = 3.0

PV_MIN_K = 50.0
PV_MAX_K = 350.0
FLAT_K = 0.2
MOVE_K = 2.0
PV_WINDOW_S = 1800.0
#: In-memory history shorter than this is topped up from InfluxDB (set_pv_source only).
PV_MIN_COVERAGE_S = 1500.0
PV_SAMPLE_S = 10.0
DEFAULT_CROSS_CHECK_K = 15.0


def _is_num(x: Any) -> bool:
    if isinstance(x, bool) or not isinstance(x, (int, float)):
        return False
    try:
        return math.isfinite(float(x))
    except OverflowError:
        return False


def _f(x: Any) -> str:
    return f"{float(x):.6g}" if _is_num(x) else "?"


# ──────────────────────────────────────────────────────────────────────────
# Pressure guard
# ──────────────────────────────────────────────────────────────────────────

class PressureGuard:
    """Pressure preconditions for writes, and the trip / blind supervision."""

    def __init__(self, bus, zones, rails, *, clock: Callable[[], float] = time.time,
                 mono: Optional[Callable[[], float]] = None,
                 on_trip: Optional[Callable[[str], None]] = None,
                 on_blind: Optional[Callable[[str], None]] = None):
        self._bus = bus
        self._zones = zones
        self._rails = rails
        self._clock = clock
        self._mono = mono if mono is not None else (time.monotonic if clock is time.time
                                                    else clock)
        self._on_trip = on_trip
        self._on_blind = on_blind
        self._lock = threading.Lock()
        self._latched: Dict[str, bool] = {g: False for g in GAUGES}
        self._last_known: Dict[str, float] = {g: self._mono() for g in GAUGES}
        self.last_trip: Optional[dict] = None
        self.trips = 0

    def set_callbacks(self, on_trip=None, on_blind=None) -> None:
        self._on_trip = on_trip
        self._on_blind = on_blind

    # readings -------------------------------------------------------------

    def reading(self, gauge: str, fresh_s: Optional[float]) -> dict:
        """``{id, label, value, age_s, clip_mbar, status, detail}``; status is
        ``ok`` | ``clipped`` | ``stale`` | ``missing`` | ``invalid``."""
        g = self._zones.gauges.get(gauge, {}) if self._zones is not None else {}
        clip = g.get("clip_mbar")
        out = {"id": gauge, "label": g.get("label") or gauge, "value": None, "age_s": None,
               "clip_mbar": clip, "status": "missing", "detail": "no reading"}
        view = self._bus.state(gauge)
        if not isinstance(view, dict) or not _is_num(view.get("value")):
            return out
        value = float(view["value"])
        out["value"] = value
        ts = view.get("ts")
        if _is_num(ts):
            out["age_s"] = max(0.0, self._clock() - float(ts))
        else:
            snap_age = self._bus.snapshot_age()
            if _is_num(view.get("age_s")) and snap_age is not None:
                out["age_s"] = float(view["age_s"]) + snap_age
        if view.get("freshness") != "fresh" or out["age_s"] is None or \
                (self._bus.snapshot_age() or 0) > 30:
            out["status"], out["detail"] = "stale", "not fresh in slowcontrol"
            return out
        if fresh_s is not None and out["age_s"] > float(fresh_s):
            out["status"] = "stale"
            out["detail"] = f"{out['age_s']:.0f} s old (guard fresh_s {_f(fresh_s)} s)"
            return out
        if clip is not None and value > clip * OVER_RANGE_FRACTION:
            out["status"] = "invalid"
            out["detail"] = (f"{_f(value)} mbar is above the gauge's {_f(clip)} mbar range — "
                             f"not physical")
            return out
        if clip is not None and value >= CLIP_FRACTION * clip:
            out["status"] = "clipped"
            out["detail"] = (f"{_f(value)} mbar is at the gauge's {_f(clip)} mbar limit — the "
                             f"real pressure may be higher")
            return out
        out["status"], out["detail"] = "ok", ""
        return out

    @staticmethod
    def known(r: dict) -> bool:
        return r["status"] in ("ok", "clipped")

    @staticmethod
    def at_least(r: dict, limit: float) -> bool:
        """The reading is at or above ``limit`` (a clipped one always is)."""
        return r["status"] == "clipped" or (r["status"] == "ok" and r["value"] >= limit)

    @staticmethod
    def _shown(r: dict) -> str:
        if r["status"] == "ok":
            return f"{r['id']} {_f(r['value'])} mbar"
        return f"{r['id']} ({r['detail']})"

    def _setup_missing(self, g: dict, *, gauges=True, xenon=True) -> List[str]:
        missing = []
        if gauges:
            missing += [f"{x}.warn_mbar/trip_mbar" for x in GAUGES
                        if g[x]["warn_mbar"] is None or g[x]["trip_mbar"] is None]
        if g["fresh_s"] is None:
            missing.append("fresh_s")
        if xenon and g["xenon_present_mbar"] is None:
            missing.append("xenon_present_mbar")
        return missing

    def xenon_present(self, g: Optional[dict] = None) -> Optional[bool]:
        """True / False from p_xe_cube against ``xenon_present_mbar``; None
        when the threshold is unset or the cube pressure unknown."""
        g = g if g is not None else self._rails.guard()
        thr = g["xenon_present_mbar"]
        if thr is None:
            return None
        r = self.reading("p_xe_cube", g["fresh_s"])
        if not self.known(r):
            return None
        return self.at_least(r, float(thr))

    def _valve_open(self, valve: str) -> Optional[bool]:
        vm = self._zones.valves.get(valve) if self._zones is not None else None
        topic = vm["status_topic"] if vm else f"xsphere/status/pneumatic/{valve}"
        st = self._bus.live_payload(topic, VALVE_LIVE_S)
        if not isinstance(st, dict) or "state" not in st:
            return None
        if st.get("state") == 1 and st.get("desired") in (1, None):
            return True
        if st.get("state") == 0:
            return False
        return None

    # write preconditions (None = allowed; else the refusal text) -------------

    def warming_problem(self) -> Optional[str]:
        g = self._rails.guard()
        missing = self._setup_missing(g)
        if missing:
            return ("the pressure guard is not set up in Lucas's rails (guard: "
                    + ", ".join(missing) + " unset), so warming heater writes are refused — "
                    "the service cannot check the xenon pressure")
        for gauge in GAUGES:
            r = self.reading(gauge, g["fresh_s"])
            warn = float(g[gauge]["warn_mbar"])
            if not self.known(r):
                return (f"pressure {self._shown(r)} is unknown, so warming heater writes are "
                        f"refused until it reads again")
            if r["status"] == "clipped":
                return (f"pressure {r['id']}: {r['detail']}, so warming heater writes are "
                        f"refused")
            if r["value"] >= warn:
                return (f"pressure {r['id']} reads {_f(r['value'])} mbar, at or above the "
                        f"guard's warn level {_f(warn)} mbar — warming heater writes are "
                        f"refused")
        if self.xenon_present(g):
            closed = []
            for v in ("ballast", "bottle"):
                state = self._valve_open(v)
                if state is not True:
                    closed.append(f"{v} {'closed' if state is False else 'state unknown'}")
            if closed:
                return (f"xenon is in the system (p_xe_cube ≥ {_f(g['xenon_present_mbar'])} "
                        f"mbar) and the buffer is not open ({', '.join(closed)}) — warming heater "
                        f"writes are refused while the ballast and bottle valves are not both "
                        f"open")
        return None

    def close_buffer_problem(self, valve: str) -> Optional[str]:
        """Closing the ballast or bottle valve (the walk-away buffer)."""
        g = self._rails.guard()
        missing = self._setup_missing(g, gauges=False)
        if missing:
            return (f"the pressure guard is not set up in Lucas's rails (guard: "
                    f"{', '.join(missing)} unset), so closing the {valve} valve — the xenon's "
                    f"buffer — is refused")
        r = self.reading("p_xe_cube", g["fresh_s"])
        if not self.known(r):
            return (f"pressure {self._shown(r)} is unknown, so xenon in the system cannot be "
                    f"ruled out — closing the {valve} valve is refused")
        if self.at_least(r, float(g["xenon_present_mbar"])):
            shown = (r["detail"] if r["status"] == "clipped"
                     else f"{_f(r['value'])} mbar ≥ {_f(g['xenon_present_mbar'])} mbar")
            return (f"xenon is in the system (p_xe_cube {shown}) — the guard never closes the "
                    f"{valve} valve, the xenon's walk-away buffer, while xenon is present")
        return None

    def pump_open_problem(self) -> Optional[str]:
        g = self._rails.guard()
        limit = g["pump_open_max_mbar"]
        if limit is None or g["fresh_s"] is None:
            return ("the pressure guard's pump_open_max_mbar / fresh_s are not set in Lucas's "
                    "rails, so opening the pump valve is refused")
        for gauge in GAUGES:
            r = self.reading(gauge, g["fresh_s"])
            if not self.known(r):
                return (f"pressure {self._shown(r)} is unknown, so opening the pump valve is "
                        f"refused")
            if r["status"] == "clipped" or r["value"] > float(limit):
                return (f"pressure {r['id']} reads "
                        + (r["detail"] if r["status"] == "clipped" else f"{_f(r['value'])} mbar")
                        + f", above the guard's pump_open_max_mbar {_f(limit)} mbar — opening "
                          f"the pump valve is refused")
        return None

    def builtin_pairs(self) -> List[Tuple[str, str]]:
        """Pairs the guard forbids open together: pump + ballast and pump +
        bottle, unless ``guard.pump_open_with`` names the partner."""
        allowed = set(self._rails.guard()["pump_open_with"])
        return [("pump", p) for p in PUMP_PARTNERS if p not in allowed]

    # supervision ------------------------------------------------------------

    def tick(self, armed_groups: List[str]) -> None:
        g = self._rails.guard()
        now = self._mono()
        for gauge in GAUGES:
            r = self.reading(gauge, g["fresh_s"])
            if self.known(r):
                self._last_known[gauge] = now
            warn, trip = g[gauge]["warn_mbar"], g[gauge]["trip_mbar"]
            with self._lock:
                latched = self._latched[gauge]
            if trip is None:
                with self._lock:
                    self._latched[gauge] = False
                continue
            if not latched and self.at_least(r, float(trip)):
                with self._lock:
                    self._latched[gauge] = True
                shown = (r["detail"] if r["status"] == "clipped"
                         else f"{_f(r['value'])} mbar ≥ trip {_f(trip)} mbar")
                self._trip(f"pressure guard trip: {gauge} {shown}")
            elif latched and r["status"] == "ok" and r["value"] < float(warn):
                with self._lock:
                    self._latched[gauge] = False
                log.warning("Pressure guard: %s back below warn (%s mbar < %s mbar) — the trip "
                            "re-arms", gauge, _f(r["value"]), _f(warn))
        blind_s = g["blind_disarm_s"]
        if blind_s is None:
            return
        worst = max(GAUGES, key=lambda x: now - self._last_known[x])
        gap = now - self._last_known[worst]
        # Every tick while blind: an arm made meanwhile is dropped again.
        if gap < float(blind_s) or not any(x in armed_groups for x in ("heaters", "pneumatic")):
            return
        reason = (f"pressure guard: no fresh {worst} reading for {gap:.0f} s (guard "
                  f"blind_disarm_s {_f(blind_s)} s)")
        log.warning("%s", reason)
        if self._on_blind is not None:
            self._on_blind(reason)

    def _trip(self, reason: str) -> None:
        self.trips += 1
        self.last_trip = {"utc": _utc(self._clock()), "reason": reason}
        log.error("%s", reason)
        if self._on_trip is not None:
            try:
                self._on_trip(reason)
            except Exception:
                log.exception("Pressure guard trip action failed")

    def latched(self) -> List[str]:
        with self._lock:
            return [g for g, v in self._latched.items() if v]

    # status ----------------------------------------------------------------

    def summary(self) -> dict:
        """What the overview, the status and the card show."""
        g = self._rails.guard()
        gauges = {}
        for gauge in GAUGES:
            r = self.reading(gauge, g["fresh_s"])
            warn, trip = g[gauge]["warn_mbar"], g[gauge]["trip_mbar"]
            if not self.known(r):
                level = "unknown"
            elif trip is None:
                level = "no limits set"
            elif self.at_least(r, float(trip)):
                level = "TRIP"
            elif self.at_least(r, float(warn)):
                level = "warn"
            else:
                level = "ok"
            gauges[gauge] = {"value_mbar": r["value"], "age_s": _round(r["age_s"]),
                             "status": r["status"], "detail": r["detail"], "level": level,
                             "warn_mbar": warn, "trip_mbar": trip, "clip_mbar": r["clip_mbar"]}
        return {"configured": not self._setup_missing(g),
                "gauges": gauges,
                "xenon_present": self.xenon_present(g),
                "xenon_present_mbar": g["xenon_present_mbar"],
                "tripped": self.latched(),
                "last_trip": dict(self.last_trip) if self.last_trip else None,
                "warming_refused": self.warming_problem(),
                "forbidden_pairs": ["+".join(p) for p in self.builtin_pairs()]}


# ──────────────────────────────────────────────────────────────────────────
# PV-source plausibility
# ──────────────────────────────────────────────────────────────────────────

class PvCheck:
    """Samples each zone's cube and clamp sensors and judges PV sources."""

    def __init__(self, bus, zones, rails, *, history=None,
                 clock: Callable[[], float] = time.time,
                 on_fail: Optional[Callable[[str, str, str], None]] = None):
        self._bus = bus
        self._zones = zones
        self._rails = rails
        self._history = history
        self._clock = clock
        self._on_fail = on_fail
        self._lock = threading.Lock()
        self._samples: Dict[str, Deque[Tuple[float, float]]] = collections.defaultdict(
            collections.deque)
        self._last_sample: Optional[float] = None
        self._flags: Dict[str, Optional[dict]] = {z: None for z in ZONES}

    def set_on_fail(self, fn: Optional[Callable[[str, str, str], None]]) -> None:
        self._on_fail = fn

    # samples ----------------------------------------------------------------

    def _sensors(self):
        for z in ZONES:
            for src in ("cube", "clamp"):
                sid = self._zones.sensor_state(z, src)
                if sid:
                    yield z, src, sid

    def sample(self, now: Optional[float] = None) -> None:
        now = self._clock() if now is None else now
        with self._lock:
            for _z, _src, sid in self._sensors():
                v = self._bus.state_value(sid)
                dq = self._samples[sid]
                if _is_num(v):
                    dq.append((now, float(v)))
                while dq and now - dq[0][0] > PV_WINDOW_S:
                    dq.popleft()
            self._last_sample = now

    def _span(self, sid: str, now: float) -> Tuple[Optional[float], float, Optional[float]]:
        """(span K, coverage s, last value) of the in-memory samples."""
        with self._lock:
            pts = [(t, v) for t, v in self._samples.get(sid, ()) if now - t <= PV_WINDOW_S]
        if len(pts) < 2:
            return None, 0.0, (pts[-1][1] if pts else None)
        vals = [v for _t, v in pts]
        return max(vals) - min(vals), pts[-1][0] - pts[0][0], pts[-1][1]

    def _history_spans(self, sid: str, ref: str) -> Optional[Tuple[float, float, float]]:
        """(source span, reference span, coverage s) from InfluxDB 1-min means
        of the last 30 min, or None when history is unavailable."""
        if self._history is None:
            return None
        try:
            is_error, res = self._history.query({"ids": [sid, ref], "start": "-30m",
                                                 "every": "1m", "fn": "mean"})
        except Exception:                            # noqa: BLE001 — history is a bonus
            log.exception("PV check: history query failed")
            return None
        if is_error or not isinstance(res, dict):
            return None
        series = {s.get("id"): s for s in res.get("series", []) if isinstance(s, dict)}
        spans = []
        cover = None
        for x in (sid, ref):
            s = series.get(x) or {}
            vals = [float(v) for v in s.get("v", []) if _is_num(v)]
            dts = s.get("dt_s") or []
            if len(vals) < 2:
                return None
            spans.append(max(vals) - min(vals))
            c = float(dts[-1]) if dts and _is_num(dts[-1]) else 0.0
            cover = c if cover is None else min(cover, c)
        return spans[0], spans[1], cover or 0.0

    # judging ----------------------------------------------------------------

    def source_of(self, zone: str, pv_expr: Any) -> Optional[str]:
        if not isinstance(pv_expr, str) or not pv_expr.strip():
            return None
        e = pv_expr.strip()
        for src in ("cube", "clamp"):
            if e == self._zones.alias(zone, src):
                return src
        return None

    def problem(self, zone: str, source: str, value: Any, *, use_history: bool = False,
                for_write: bool = False) -> Optional[str]:
        """Why ``source`` of ``zone`` is not a plausible PV, or None."""
        sid = self._zones.sensor_state(zone, source)
        alias = self._zones.alias(zone, source)
        name = f"the {source} sensor {sid} ({alias})"
        if not _is_num(value):
            return f"{name} has no fresh reading"
        v = float(value)
        if not PV_MIN_K <= v <= PV_MAX_K:
            return (f"{name} reads {_f(v)} K, outside the plausible {_f(PV_MIN_K)}–"
                    f"{_f(PV_MAX_K)} K")
        other = "cube" if source == "clamp" else "clamp"
        ref = self._zones.sensor_state(zone, other)
        if source == "clamp":
            limit = self._rails.pv_cross_check_k
            if limit is None:
                if for_write:
                    return ("Lucas's rails do not set heaters.pv_cross_check_k, so a clamp "
                            "sensor cannot be checked against the cube RTD and is refused")
                limit = DEFAULT_CROSS_CHECK_K
            cube = self._bus.state_value(ref) if ref else None
            if not _is_num(cube):
                return (f"{name} cannot be checked: the zone's cube RTD ({ref}) has no fresh "
                        f"reading")
            if abs(v - float(cube)) > limit:
                return (f"{name} reads {_f(v)} K but the zone's cube RTD reads {_f(cube)} K — "
                        f"more than {_f(limit)} K apart")
        if not ref:
            return None
        now = self._clock()
        s_span, s_cov, _ = self._span(sid, now)
        r_span, r_cov, _ = self._span(ref, now)
        cover = min(s_cov, r_cov)
        if (s_span is None or r_span is None or cover < PV_MIN_COVERAGE_S) and use_history:
            h = self._history_spans(sid, ref)
            if h is not None and (s_span is None or r_span is None or h[2] > cover):
                s_span, r_span, cover = h
        if s_span is None or r_span is None:
            return None
        if r_span >= MOVE_K and s_span <= FLAT_K:
            return (f"{name} has stayed at {_f(v)} K (span {s_span:.2f} K) over the last "
                    f"{cover / 60:.0f} min while the zone's {other} sensor moved "
                    f"{r_span:.1f} K — it looks dead or frozen")
        return None

    def flag(self, zone: str) -> Optional[dict]:
        """The last re-check's failure for the zone's current source, or None."""
        with self._lock:
            f = self._flags.get(zone)
            return dict(f) if f else None

    def flags(self) -> Dict[str, Optional[dict]]:
        with self._lock:
            return {z: (dict(f) if f else None) for z, f in self._flags.items()}

    def tick(self) -> None:
        """Sample (every 10 s) and re-check every zone's current PV source."""
        now = self._clock()
        if self._last_sample is not None and now - self._last_sample < PV_SAMPLE_S:
            return
        self.sample(now)
        for z in ZONES:
            topic = self._zones.zones[z]["status_topic"]
            r = self._bus.raw(topic)
            st = r["payload"] if r and isinstance(r["payload"], dict) else {}
            src = self.source_of(z, st.get("pv_expr"))
            problem = None
            if src is not None:
                sid = self._zones.sensor_state(z, src)
                problem = self.problem(z, src, self._bus.state_value(sid) if sid else None)
            with self._lock:
                prev = self._flags.get(z)
                self._flags[z] = ({"source": src, "alias": self._zones.alias(z, src),
                                   "problem": problem, "since_utc": (prev or {}).get("since_utc")
                                   or _utc(now)} if problem else None)
            if problem and not prev:
                log.warning("PV check: zone %s: %s", z, problem)
                if self._on_fail is not None:
                    try:
                        self._on_fail(z, src, problem)
                    except Exception:
                        log.exception("PV check failure action failed")
            elif prev and not problem:
                log.info("PV check: zone %s passes again", z)


def _round(x: Optional[float]) -> Optional[float]:
    return round(x, 1) if x is not None else None


def _utc(ts: float) -> str:
    from datetime import datetime, timezone
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
