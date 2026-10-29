"""
The AI's read tools: overview, states, the AI-control state, history, guide.

Reads need no arm and never take the write lock.  Everything shown comes
from what the service holds right now — the slowcontrol snapshot, the raw
driver status topics, the rails, arms and audit — never from the repo docs,
which drift (the Control page hint still gives the old interlock default; the
architecture notes give the old XV3 threshold).

``get_overview`` is the AI's starting point: one page of plain text that says
what each software zone physically is, what the AI may do *right now* (arms ×
rails × the PLC's own gates), and a computed list of warnings — the facts that
make a write pointless or dangerous (no PV source, interlock tripped, Sequencer
running, a level feed frozen …).  ``format: "json"`` gives the same data as
JSON.
"""

from __future__ import annotations

import logging
import math
import re
import time
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

from .actions import (NGE_TOPIC, SEQUENCER_TOPIC, TRACKERS_TOPIC, ArgError,
                      parse_numeric_expr, validate_args)
from .arms import CONTROL_PAGE, GROUPS, INTERLOCKS_TOPIC, PV_INTERLOCK_TOPIC, utc_iso
from .zones import CHANNELS, PNEUMATIC_GATE_TOPIC, VALVES, VESSELS, ZONES

log = logging.getLogger(__name__)

try:                                               # tz data may be missing on Windows
    from zoneinfo import ZoneInfo
    _LOCAL_TZ = ZoneInfo("America/New_York")
except Exception:                                  # noqa: BLE001
    _LOCAL_TZ = None

GUIDE_TOPICS = ("all", "heaters", "ln2", "pneumatic", "history", "safety")
MAX_STATES = 40

READ_SCHEMAS: Dict[str, dict] = {
    "get_overview": {
        "type": "object", "additionalProperties": False, "properties": {
            "format": {"type": "string", "enum": ["text", "json"],
                       "description": "text (default, one page) or json (same data)."}}},
    "list_states": {
        "type": "object", "additionalProperties": False, "properties": {
            "group": {"type": "string", "maxLength": 40,
                      "description": "Only this registry group, e.g. pid, temperature, level, "
                                     "valve, pneumatic, heater_supply, pressure, vacuum, turbo, "
                                     "service, interlock, derived."},
            "query": {"type": "string", "maxLength": 100,
                      "description": "Only ids or labels containing this text "
                                     "(case-insensitive)."},
            "limit": {"type": "integer", "minimum": 1, "maximum": 200,
                      "description": "At most this many rows (default 200)."}}},
    "get_states": {
        "type": "object", "additionalProperties": False, "required": ["ids"], "properties": {
            "ids": {"type": "array", "minItems": 1, "maxItems": MAX_STATES, "uniqueItems": True,
                    "items": {"type": "string", "maxLength": 80},
                    "description": "State ids from list_states (at most 40)."}}},
    "get_ai_control": {"type": "object", "additionalProperties": False, "properties": {}},
    "get_guide": {
        "type": "object", "additionalProperties": False, "properties": {
            "topic": {"type": "string", "enum": list(GUIDE_TOPICS),
                      "description": "Which part (default all)."}}},
}

#: Fallback when aicontrol.history cannot be imported.
QUERY_HISTORY_FALLBACK_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["ids", "start"],
    "properties": {
        "ids": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 8},
        "start": {"type": "string"}, "stop": {"type": "string"}, "every": {"type": "string"},
        "fn": {"type": "string", "enum": ["mean", "min", "max", "last", "count", "changes"]},
        "max_points": {"type": "integer", "minimum": 1, "maximum": 1000}}}

#: Which AI tool writes a registry state (list_states "ai:" column).
_AI_TOOL_PATTERNS = [
    (re.compile(r"pid_(top|bottom|nozzle)_(setpoint_expr|setpoint)"), "set_heater_setpoints"),
    (re.compile(r"pid_(top|bottom|nozzle)_mode"), "set_heater_auto"),
    (re.compile(r"pid_(top|bottom|nozzle)_pv_expr"), "set_pv_source"),
    (re.compile(r"nge_ch[123]_(output|voltage_set|current_limit)"), "set_heater_supply"),
    (re.compile(r"valve_(cryostat|primary_xe|ballast)"), "set_ln2_valve"),
    (re.compile(r"valve_(cryostat|primary_xe|ballast)_auto_(open|close)"), "set_ln2_auto"),
    (re.compile(r"pneumatic_(ballast|pump|bottle)"), "set_pneumatic_valve"),
]

_RAW_PREFIXES = ("xsphere/status/pid/", "xsphere/status/valve/", "xsphere/status/nge",
                 "xsphere/status/pneumatic")


def ai_tool_for(state_id: str) -> Optional[str]:
    for rx, tool in _AI_TOOL_PATTERNS:
        if rx.fullmatch(state_id):
            return tool
    return None


def _is_num(x: Any) -> bool:
    if isinstance(x, bool) or not isinstance(x, (int, float)):
        return False
    try:
        return math.isfinite(float(x))
    except OverflowError:
        return False


def _f(x: Any, digits: int = 5) -> str:
    """Compact number text (``165``, ``165.25``, ``1.2e-06``), else str."""
    if _is_num(x):
        return f"{float(x):.{digits}g}"
    if x is None:
        return "?"
    if isinstance(x, bool):
        return "true" if x else "false"
    return str(x)


def _ts_of(value: Any) -> Optional[float]:
    if _is_num(value):
        return float(value)
    if isinstance(value, str) and value:
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    return None


def local_time(value: Any, with_date: bool = False) -> str:
    """Epoch or ISO time → US Eastern clock time (UTC if the tz data is missing)."""
    ts = _ts_of(value)
    if ts is None:
        return "?"
    if _LOCAL_TZ is not None:
        dt = datetime.fromtimestamp(ts, tz=_LOCAL_TZ)
    else:
        dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    return dt.strftime(("%Y-%m-%d " if with_date else "") + "%H:%M %Z")


def _dur(seconds: Any) -> str:
    if not _is_num(seconds):
        return "?"
    s = float(seconds)
    if s < 120:
        return f"{s:.0f} s"
    if s < 7200:
        return f"{s / 60:.0f} min"
    if s < 172800:
        return f"{int(s // 3600)} h {int((s % 3600) // 60)} min"
    return f"{s / 86400:.1f} d"


# ──────────────────────────────────────────────────────────────────────────
# "AI may now" sentences (status payload and overview)
# ──────────────────────────────────────────────────────────────────────────

def _heater_allowance(rails) -> str:
    parts = []
    stale = set(rails.stale_zones)
    for z in ZONES:
        if z in stale:
            parts.append(f"{z}: rails stale (zone mapping changed — re-save)")
            continue
        zr = rails.zone(z)
        lims = rails.zone_limits(z)
        bits = []
        if lims:
            extra = []
            if zr.get("max_step_k") is not None:
                extra.append(f"step ≤ {_f(zr['max_step_k'])} K")
            if zr.get("max_rate_k_per_min") is not None:
                extra.append(f"≤ {_f(zr['max_rate_k_per_min'])} K/min")
            bits.append(f"setpoint {_f(lims[0])}–{_f(lims[1])} K"
                        + (f" ({', '.join(extra)})" if extra else ""))
        if zr.get("pv_sources"):
            bits.append("PV source " + "/".join(zr["pv_sources"]))
        if bits:
            parts.append(f"{z} " + ", ".join(bits))
    for ch in CHANNELS:
        sr = rails.channel(ch)
        bits = []
        if sr["voltage_min_v"] is not None and sr["voltage_max_v"] is not None:
            bits.append(f"V {_f(sr['voltage_min_v'])}–{_f(sr['voltage_max_v'])} V")
        if sr["current_min_a"] is not None and sr["current_max_a"] is not None:
            bits.append(f"I {_f(sr['current_min_a'])}–{_f(sr['current_max_a'])} A")
        sw = [w for w, k in (("on", "may_switch_on"), ("off", "may_switch_off")) if sr[k]]
        if sw:
            bits.append("switch " + "/".join(sw))
        if bits:
            parts.append(f"supply ch{ch} " + ", ".join(bits))
    return "; ".join(parts)


def _ln2_allowance(rails) -> str:
    parts = []
    for v in VESSELS:
        vr = rails.vessel(v)
        if not vr["may_operate"]:
            continue
        bits = []
        if vr["max_open_minutes"] is not None:
            bits.append(f"open ≤ {_f(vr['max_open_minutes'])} min")
        bits.append("close")
        if vr["may_set_auto_open"]:
            bits.append("auto-open on/off")
        else:
            bits.append("auto-open off")
        bits.append("auto-close on" + ("/off" if vr["may_disarm_auto_close"] else ""))
        parts.append(f"{v}: " + ", ".join(bits))
    return "; ".join(parts)


def _pneumatic_allowance(rails, gate_on: Optional[bool]) -> str:
    parts = []
    for v in VALVES:
        vr = rails.valve(v)
        what = [w for w, k in (("open", "may_open"), ("close", "may_close")) if vr[k]]
        if what:
            parts.append(f"{v} {'/'.join(what)}")
    if not parts:
        return ""
    pn = rails.pneumatic()
    text = "; ".join(parts)
    if pn["forbid_open_together"]:
        pairs = ["+".join(p) for p in pn["forbid_open_together"]]
        text += "; never open together: " + ", ".join(pairs)
    text += f"; ≥ {_f(pn['min_interval_s'])} s between commands to one valve"
    if gate_on is not True:
        text = ("nothing while the PLC's pneumatic valve gate is disabled (Control page); "
                "with it enabled: " + text)
    return text


def may_now(arms, rails, bus, heater_hold: Optional[dict] = None) -> Dict[str, str]:
    """One sentence per group: what the AI could do right now (arms × rails × gates).
    ``heater_hold`` is the supervisor's level-stale hold, if one is running."""
    il = None
    r = bus.raw(PV_INTERLOCK_TOPIC)
    if r and isinstance(r["payload"], dict):
        il = r["payload"]
    gate = bus.live_payload(PNEUMATIC_GATE_TOPIC, 3.0)
    gate_on = isinstance(gate, dict) and gate.get("enabled") == 1
    out = {}
    for g in GROUPS:
        if rails.error:
            allowed = ""
            why = "the rails file is invalid"
        else:
            allowed = (_heater_allowance(rails) if g == "heaters" else
                       _ln2_allowance(rails) if g == "ln2" else
                       _pneumatic_allowance(rails, gate_on))
            why = "no rails allow anything in this group"
        if g == "heaters" and allowed and il and il.get("tripped") is True:
            allowed = "nothing while the PV interlock is tripped; after it clears: " + allowed
        if g == "heaters" and allowed and heater_hold:
            allowed = (f"nothing while held by the level-stale grace "
                       f"({', '.join(heater_hold['rules'])}; the heaters disarm at "
                       f"{heater_hold['disarm_at_utc']} unless it clears); after it clears: "
                       + allowed)
        if arms.is_armed(g):
            out[g] = allowed if allowed else f"nothing — {why}"
        else:
            out[g] = ("nothing (not armed)" + (f"; if armed: {allowed}" if allowed else
                                               f"; {why}"))
    return out


# ──────────────────────────────────────────────────────────────────────────
# Reads
# ──────────────────────────────────────────────────────────────────────────

class Reads:
    """The read tools.  Each public method takes the raw arguments dict and
    returns ``(is_error, result)``."""

    def __init__(self, bus, zones, rails, arms, audit, *, actions=None, ramps=None,
                 timers=None, history=None, status_fn: Optional[Callable[[], dict]] = None,
                 clock: Callable[[], float] = time.time,
                 heater_hold: Optional[Callable[[], Optional[dict]]] = None):
        self.bus = bus
        self.zones = zones
        self.rails = rails
        self.arms = arms
        self.audit = audit
        self.actions = actions
        self.ramps = ramps
        self.timers = timers
        self.history = history
        self.status_fn = status_fn
        self._clock = clock
        self.heater_hold = heater_hold          # Supervisor.heater_hold

    def handlers(self) -> Dict[str, Callable[[dict], Tuple[bool, Any]]]:
        return {"get_overview": self.get_overview, "list_states": self.list_states,
                "get_states": self.get_states, "get_ai_control": self.get_ai_control,
                "query_history": self.query_history, "get_guide": self.get_guide}

    @staticmethod
    def _invalid(tool: str, msg: str) -> Tuple[bool, dict]:
        return True, {"ok": False, "tool": tool, "error": f"invalid arguments: {msg}",
                      "invalid_arguments": True}

    def _validate(self, tool: str, args: Any) -> dict:
        return validate_args(READ_SCHEMAS[tool], {} if args is None else args)

    # helpers ---------------------------------------------------------------

    def _raw(self, topic: str) -> Optional[dict]:
        r = self.bus.raw(topic)
        return r["payload"] if r and isinstance(r["payload"], dict) else None

    def _live(self, topic: str, max_age: float) -> Optional[dict]:
        p = self.bus.live_payload(topic, max_age)
        return p if isinstance(p, dict) else None

    # ── get_overview ──────────────────────────────────────────────────────

    def get_overview(self, args: Any = None) -> Tuple[bool, Any]:
        try:
            a = self._validate("get_overview", args)
        except ArgError as e:
            return self._invalid("get_overview", str(e))
        data = self.collect()
        if a.get("format") == "json":
            return False, dict({"ok": True, "tool": "get_overview"}, **data)
        return False, self.render_text(data)

    def collect(self) -> dict:
        """Everything the overview shows, as one JSON-able dict."""
        now = self._clock()
        snap = self.bus.snapshot()
        states = snap.get("states", {}) if isinstance(snap, dict) else {}
        age = self.bus.snapshot_age()

        def view(sid: str) -> dict:
            v = states.get(sid)
            return v if isinstance(v, dict) else {}

        def val(sid: str) -> Any:
            return view(sid).get("value")

        def reading(sid: str) -> dict:
            v = view(sid)
            return {"value": v.get("value"), "unit": v.get("unit"), "freshness": v.get("freshness")}

        hb = self._raw("xsphere/status/service/heartbeat") or {}
        counts = snap.get("counts", {}) if isinstance(snap, dict) else {}
        out: Dict[str, Any] = {
            "time": {"utc": utc_iso(now), "local": local_time(now, with_date=True)},
            "slowcontrol": {"uptime_s": hb.get("uptime_s"), "snapshot_age_s":
                            round(age, 1) if age is not None else None,
                            "service_alive": val("service_alive"), "counts": counts,
                            "mqtt_connected": self.bus.connected()},
            "arms": self.arms.snapshot(),
        }
        hold = self.heater_hold() if self.heater_hold is not None else None
        out["may_now"] = may_now(self.arms, self.rails, self.bus, hold)
        out["heater_hold"] = hold
        il = self._raw(PV_INTERLOCK_TOPIC)
        out["interlock"] = ({"min_k": il.get("min_k"), "max_k": il.get("max_k"),
                             "tripped": il.get("tripped"), "reasons": il.get("reasons")}
                            if il else None)
        ilk = self._raw(INTERLOCKS_TOPIC)
        out["interlocks"] = ({"ok": ilk.get("ok"), "rules_active": ilk.get("rules_active")}
                             if ilk else None)

        zones = {}
        ai_sp = self.actions.zone_ai_setpoints() if self.actions is not None else {}
        for z in ZONES:
            zm = self.zones.zones[z]
            topic = zm["status_topic"]
            st = self._raw(topic) or {}
            live_age = self.bus.live_age(topic)
            expr = st.get("setpoint_expr")
            num = parse_numeric_expr(expr)
            pv_expr = st.get("pv_expr")
            pv_val = st.get("pv_expr_value_k")
            pv_k = st.get("pv_k")
            if not isinstance(pv_expr, str) or not pv_expr.strip():
                pv_flag = "NO PV SOURCE (pv_expr empty)" + (
                    " — 500 K is the interlock surrogate" if _is_num(pv_k)
                    and abs(float(pv_k) - 500.0) < 0.5 else "")
            elif not _is_num(pv_val):
                pv_flag = f"pv_expr {pv_expr!r} does NOT evaluate — PV frozen"
            elif not _is_num(pv_k) or abs(float(pv_k) - float(pv_val)) > 5.0:
                pv_flag = "PV register disagrees with pv_expr"
            else:
                pv_flag = "ok"
            lims = self.rails.zone_limits(z)
            if isinstance(expr, str) and expr.strip():
                effective = num if num is not None else st.get("setpoint_expr_value_k")
            else:
                effective = st.get("setpoint_k")
            zones[z] = {
                "describe": self.zones.describe(z), "physical": zm["physical"],
                "pid_live_age_s": round(live_age, 1) if live_age is not None else None,
                "mode": st.get("mode"), "setpoint_k": st.get("setpoint_k"),
                "setpoint_expr": expr, "setpoint_expr_value_k": st.get("setpoint_expr_value_k"),
                "setpoint_is_number": num is not None, "effective_setpoint_k": effective,
                "pv_k": pv_k, "pv_expr": pv_expr, "pv_expr_value_k": pv_val, "pv_status": pv_flag,
                "output_pct": st.get("output_pct"),
                "clamp": reading(zm["clamp_state"]) if zm.get("clamp_state") else None,
                "cube": reading(zm["cube_state"]) if zm.get("cube_state") else None,
                "rails_k": list(lims) if lims else None,
                "rails_stale": z in self.rails.stale_zones,
                "last_ai_setpoint": ai_sp.get(z),
            }
        out["zones"] = zones
        out["measured"] = {"dt_vertical_meas": reading("dt_vertical_meas"),
                           "dt_longitudinal_meas": reading("dt_longitudinal_meas")}
        grad = self._raw("xsphere/status/gradient")
        out["gradient"] = ({k: grad.get(k) for k in ("mode", "base_k", "delta_v_k", "delta_l_k")}
                           if grad else None)
        seq = self._raw(SEQUENCER_TOPIC)
        out["sequencer"] = ({"running": bool(seq.get("running")),
                             "current_step": seq.get("current_step"),
                             "items_loaded": len(seq.get("steps") or [])
                             if isinstance(seq.get("steps"), list) else None,
                             "last_message": seq.get("last_message")} if seq else None)
        trk = self.bus.raw(TRACKERS_TOPIC)
        tp = trk["payload"] if trk else None
        out["trackers"] = ([{"id": t.get("id"), "target": t.get("target"),
                             "source": t.get("source"), "enabled": t.get("enabled")}
                            for t in tp if isinstance(t, dict)] if isinstance(tp, list) else None)

        nge = self._raw(NGE_TOPIC) or {}
        supply = {"connected": val("nge_connected"), "hold": nge.get("hold"),
                  "event": nge.get("event"), "event_utc": nge.get("event_utc"), "channels": {}}
        for ch in CHANNELS:
            c = self.zones.channels[ch]
            st = self._raw(c["status_topic"]) or {}
            supply["channels"][ch] = dict(
                {k: st.get(k) for k in ("output", "desired", "voltage_set_v", "voltage_request_v",
                                        "current_limit_a", "current_request_a", "voltage_v",
                                        "current_a", "power_w", "mode", "trip")},
                label=c["label"], software_zone=c["zone"],
                physical=(self.zones.zones[c["zone"]]["physical"]
                          if c.get("zone") in self.zones.zones else None),
                live_age_s=_round(self.bus.live_age(c["status_topic"])))
        out["supply"] = supply

        ln2 = {}
        timers = self.timers.status() if self.timers is not None else {}
        for v in VESSELS:
            vm = self.zones.vessels[v]
            st = self._raw(vm["status_topic"]) or {}
            ln2[v] = {"xv": vm["xv"],
                      "levels": {sid: reading(sid) for sid in vm["level_states"]},
                      "esp32_alive": val(vm["esp32_state"]) if vm["esp32_state"] else None,
                      "state": st.get("state"), "desired": st.get("desired"),
                      "auto_close": st.get("auto_close"), "auto_open": st.get("auto_open"),
                      "live_age_s": _round(self.bus.live_age(vm["status_topic"])),
                      "ai_timer": timers.get(v)}
        out["ln2"] = ln2

        gate = self._raw(PNEUMATIC_GATE_TOPIC) or {}
        pn = {"gate_enabled": gate.get("enabled"), "gate_since_utc": gate.get("since_utc"),
              "gate_live_age_s": _round(self.bus.live_age(PNEUMATIC_GATE_TOPIC)),
              "last_refused": gate.get("last_refused"), "valves": {}}
        for v in VALVES:
            vm = self.zones.valves[v]
            st = self._raw(vm["status_topic"]) or {}
            pn["valves"][v] = {"state": st.get("state"), "desired": st.get("desired"),
                               "normally": vm["normally"]}
        out["pneumatic"] = pn
        pressures = ("p_xe_cube", "p_primary_bottle", "p_backup_bottle")
        out["pressures"] = {sid: reading(sid) for sid in pressures if sid in states}
        out["vacuum"] = {sid: reading(sid) for sid in ("vac_ghs", "vac_outer") if sid in states}
        out["turbos"] = {p: {"state": val(f"turbo_{p}_state"),
                             "frequency_hz": val(f"turbo_{p}_frequency"),
                             "mode": val(f"turbo_{p}_mode"),
                             "connected": val(f"turbo_{p}_connected")}
                         for p in ("ghs", "outer")}
        out["ramps"] = self.ramps.status() if self.ramps is not None else []
        out["warnings"] = self._warnings(out, states, counts, age, val)
        recent = [e for e in self.audit.recent(60)
                  if e.get("kind") in ("action", "refused", "note", "auto_close")][:3]
        keys = ("utc", "kind", "tool", "detail", "reason")
        out["last_ai_actions"] = [{k: e.get(k) for k in keys} for e in recent]
        return out

    def _warnings(self, out: dict, states: dict, counts: dict, age: Optional[float],
                  val: Callable[[str], Any]) -> List[str]:
        w: List[str] = []
        if not self.bus.connected():
            w.append("the AI service is not connected to the MQTT broker — every write is "
                     "refused")
        if age is None:
            w.append("no slowcontrol snapshot received — every write is refused")
        elif age > 5:
            w.append(f"the slowcontrol snapshot is {age:.0f} s old — every write is refused "
                     f"(is xsphere-slowcontrol running?)")
        if val("service_alive") is not True:
            w.append("slowcontrol's heartbeat is not fresh")
        if self.rails.error:
            w.append("the rails file is invalid — nothing is writable until Lucas saves the "
                     "rails again")
        for z in self.rails.stale_zones:
            w.append(f"zone {z}: its heater rails were saved for another zone mapping — they "
                     f"count as unset until Lucas re-saves them")
        il = out["interlock"]
        if il is None:
            w.append("the PV interlock status has not been seen — heater writes are refused")
        elif il.get("tripped") is True:
            w.append("the PV INTERLOCK IS TRIPPED: every zone is held at SP 3 K / output 0")
        ilk = out["interlocks"]
        if ilk and ilk.get("ok") is False:
            w.append(f"slowcontrol interlocks not ok: {ilk.get('rules_active')}")
        hold = out.get("heater_hold")
        if hold:
            w.append(f"AI heater writes are HELD by the level-stale grace "
                     f"({', '.join(hold['rules'])}): the heaters disarm at "
                     f"{hold['disarm_at_utc']} unless the interlocks clear first")
        for z, d in out["zones"].items():
            if d["pid_live_age_s"] is None or d["pid_live_age_s"] > 3:
                w.append(f"zone {z}: no live PID status from the PLC")
                continue
            if d["pv_status"] != "ok":
                w.append(f"zone {z}: {d['pv_status']} — setpoint changes will not regulate on a "
                         f"real temperature (set_pv_source)")
            if d["mode"] != "auto":
                w.append(f"zone {z} is in PID mode {d['mode']!r} — a setpoint has no effect")
            if isinstance(d["setpoint_expr"], str) and d["setpoint_expr"].strip() \
                    and not d["setpoint_is_number"]:
                w.append(f"zone {z}: its setpoint is the expression {d['setpoint_expr']!r} "
                         f"(set by a human; replacing it needs replace_expression)")
            lims, e = d["rails_k"], d["effective_setpoint_k"]
            if lims and _is_num(e) and not lims[0] <= float(e) <= lims[1]:
                w.append(f"zone {z}: setpoint {_f(e)} K is outside the AI rails "
                         f"{_f(lims[0])}–{_f(lims[1])} K")
        seq = out["sequencer"]
        if seq and seq["running"]:
            w.append("the Sequencer is RUNNING — every AI write is refused until it stops")
        trk = out["trackers"]
        if trk is None and self.bus.raw(TRACKERS_TOPIC) is not None:
            w.append("the trackers status is unreadable — AI writes are refused")
        elif trk and any(t.get("enabled") for t in trk):
            on = [str(t.get("id")) for t in trk if t.get("enabled")]
            w.append("tracker(s) enabled: " + ", ".join(on) + " — every AI write is refused")
        lc = states.get("level_cryostat") or {}
        if lc.get("freshness") == "fresh" and _is_num(lc.get("value")) and \
                float(lc["value"]) == 0.0:
            w.append("the cryostat analog level reads 0 — the ladder's auto-close relies on the "
                     "USB probe copy (level_cryostat_probe)")
        if "ghs_esp32_alive" in states and val("ghs_esp32_alive") is not True:
            w.append("the GHS ESP32 heartbeat is not fresh — ballast / primary_xe levels may be "
                     "frozen in the PLC")
        sup = out["supply"]
        if sup["connected"] is not True:
            w.append("the NGE103 heater supply is not connected")
        for ch, c in sup["channels"].items():
            if isinstance(c.get("trip"), str) and c["trip"] not in ("none", ""):
                w.append(f"NGE ch{ch} protection trip: {c['trip']}")
        for v, t in out["ln2"].items():
            if t["ai_timer"]:
                w.append(f"LN2 {v} was opened by the AI; the service closes it at "
                         f"{t['ai_timer']['deadline_utc']} if still open")
        gate_on = out["pneumatic"]["gate_enabled"] == 1
        if self.arms.is_armed("pneumatic") and not gate_on:
            w.append("pneumatic is armed for the AI but the PLC's pneumatic valve gate is "
                     "disabled — no gas valve can move")
        stale = [sid for sid, v in states.items()
                 if isinstance(v, dict) and v.get("freshness") in ("stale", "invalid")]
        if stale:
            w.append(f"{counts.get('stale', 0)} stale and {counts.get('invalid', 0)} invalid "
                     f"states, e.g. {', '.join(stale[:6])}")
        return w

    def render_text(self, d: dict) -> str:
        L: List[str] = []
        t = d["time"]
        L.append(f"xsphere overview — {t['local']} (UTC {t['utc']}); Lucas works in US Eastern "
                 f"time, history is UTC")
        sc = d["slowcontrol"]
        cnt = sc.get("counts") or {}
        L.append(f"slowcontrol: up {_dur(sc['uptime_s'])} · snapshot "
                 f"{_f(sc['snapshot_age_s'])} s old · states {cnt.get('fresh', '?')} fresh / "
                 f"{cnt.get('stale', '?')} stale / {cnt.get('invalid', '?')} invalid · MQTT "
                 f"{'connected' if sc['mqtt_connected'] else 'DISCONNECTED'}")
        arms = []
        for g, a in d["arms"].items():
            if a["armed"]:
                arms.append(f"{g} ARMED (" + (f"until {local_time(a['expires_utc'])}"
                                              if a["expires_utc"] else "no time limit")
                            + (f", by {a['by']}" if a.get("by") else "") + ")")
            else:
                ld = a.get("last_disarm")
                arms.append(f"{g} off" + (f" ({local_time(ld['utc'])}: {ld['reason']})"
                                          if ld else ""))
        L.append("AI arms: " + " · ".join(arms) + f" — Lucas arms on {CONTROL_PAGE}")
        L.append("AI may now:")
        for g, s in d["may_now"].items():
            L.append(f"  {g}: {s}")
        il = d["interlock"]
        if il:
            reasons = il.get("reasons") or {}
            L.append(f"PV interlock: band {_f(il['min_k'])}–{_f(il['max_k'])} K, "
                     + ("TRIPPED (" + "; ".join(f"RTD{k}: {v}" for k, v in reasons.items()) + ")"
                        if il.get("tripped") else "not tripped")
                     + " — a trip forces SP 3 K / output 0 on every zone (the PV is left as it was)")
        else:
            L.append("PV interlock: status not seen")
        if d["interlocks"]:
            ok = d["interlocks"].get("ok")
            L.append("slowcontrol interlocks: " + ("ok" if ok else
                                                   f"NOT ok {d['interlocks'].get('rules_active')}"))
        L.append("Zones (software names; physical position as stated):")
        for z, zd in d["zones"].items():
            L.append(f"  {zd['describe']}")
            expr = zd["setpoint_expr"]
            if isinstance(expr, str) and expr.strip():
                sp = (f"SP {_f(zd['setpoint_k'])} K (expr {expr!r}"
                      + ("" if zd["setpoint_is_number"]
                         else f" = {_f(zd['setpoint_expr_value_k'])} K") + ")")
            else:
                sp = f"SP {_f(zd['setpoint_k'])} K (register; no expression)"
            pv = f"PV {_f(zd['pv_k'])} K"
            if isinstance(zd["pv_expr"], str) and zd["pv_expr"].strip():
                pv += f" via {zd['pv_expr']!r} (= {_f(zd['pv_expr_value_k'])} K)"
            pv += f" [{zd['pv_status']}]"
            temps = []
            for which in ("clamp", "cube"):
                r = zd[which]
                if r:
                    temps.append(f"{which} {_f(r['value'])} K"
                                 + ("" if r["freshness"] == "fresh" else f" ({r['freshness']})"))
            rails = (f"AI rails {_f(zd['rails_k'][0])}–{_f(zd['rails_k'][1])} K"
                     if zd["rails_k"] else ("AI rails STALE" if zd["rails_stale"]
                                            else "no AI rails"))
            L.append(f"    mode {zd['mode']} · {sp} · {pv} · output {_f(zd['output_pct'])} % · "
                     + " · ".join(temps) + f" · {rails}")
        m = d["measured"]
        L.append(f"Measured ΔT: vertical (cube bottom − top) {_f(m['dt_vertical_meas']['value'])} K"
                 f" · longitudinal (cube nozzle − top) {_f(m['dt_longitudinal_meas']['value'])} K")
        g = d["gradient"]
        if g:
            L.append(f"Gradient controller: mode {g.get('mode')}, base {_f(g.get('base_k'))} K, "
                     f"ΔV {_f(g.get('delta_v_k'))} K, ΔL {_f(g.get('delta_l_k'))} K (it adopts "
                     f"the PID setpoints; not an AI tool)")
        seq, trk = d["sequencer"], d["trackers"]
        s = ("Sequencer: " + ("RUNNING" if seq and seq["running"] else "idle")
             + (f", {seq['items_loaded']} items loaded" if seq and seq["items_loaded"] else "")
             if seq else "Sequencer: no status")
        s += " · Trackers: " + ("none" if not trk else ", ".join(
            f"{t['id']} → {t['target']} ({'ENABLED' if t['enabled'] else 'disabled'})"
            for t in trk))
        L.append(s)
        sup = d["supply"]
        L.append(f"Heater supply NGE103: {'connected' if sup['connected'] else 'NOT connected'}"
                 f", hold {'on' if sup['hold'] else 'off'}"
                 + (f" · last event {sup['event_utc']}: {sup['event']}" if sup["event"] else ""))
        for ch, c in sup["channels"].items():
            onoff = {1: "ON", 0: "OFF"}.get(c.get("output"), "?")
            # The registry label names the SOFTWARE zone; say where it is.
            where = (f" → software zone {c['software_zone']}, physically "
                     f"{str(c['physical']).upper()}" if c.get("software_zone") else "")
            L.append(f"  {c['label']}{where}: {onoff} (desired {_f(c.get('desired'))}) · V set "
                     f"{_f(c.get('voltage_set_v'))} (request {_f(c.get('voltage_request_v'))}) · "
                     f"I limit {_f(c.get('current_limit_a'))} (request "
                     f"{_f(c.get('current_request_a'))}) · reads {_f(c.get('voltage_v'))} V "
                     f"{_f(c.get('current_a'))} A {_f(c.get('power_w'))} W · {c.get('mode')} · "
                     f"trip {c.get('trip')}")
        L.append("LN2 fill valves (levels 0–10 unless shown):")
        onoff = {1: "on", 0: "off"}
        for v, x in d["ln2"].items():
            lv = " · ".join(_level_text(sid, r) for sid, r in x["levels"].items())
            word = {1: "OPEN", 0: "closed"}.get(x["state"], "?")
            flags = (f"auto-close {onoff.get(x['auto_close'], '?')}, auto-open "
                     f"{onoff.get(x['auto_open'], '?')}")
            extra = ""
            if x["esp32_alive"] is not None:
                extra = " · GHS ESP32 " + ("alive" if x["esp32_alive"] else "NOT alive")
            if x["ai_timer"]:
                extra += f" · AI timer: closes at {local_time(x['ai_timer']['deadline_utc'])}"
            L.append(f"  {v} ({x['xv']}): {lv} · valve {word} (desired {_f(x['desired'])}) · "
                     f"{flags}{extra}")
        pn = d["pneumatic"]
        gate = {1: "ENABLED", 0: "disabled"}.get(pn["gate_enabled"], "?")
        L.append(f"Pneumatic (valve terms): PLC gate {gate}"
                 + (f" since {pn['gate_since_utc']}" if pn["gate_since_utc"] else "") + " · "
                 + " · ".join(f"{v} {({1: 'OPEN', 0: 'closed'}).get(x['state'], '?')}"
                              f" (normally {x['normally']})" for v, x in pn["valves"].items()))
        if d["pressures"]:
            parts = [f"{sid} {_f(r['value'])} {r['unit'] or ''}".rstrip()
                     for sid, r in d["pressures"].items()]
            L.append("Pressures: " + " · ".join(parts))
        if d["vacuum"]:
            parts = [f"{sid} {_f(r['value'], 3)} {r['unit'] or ''}".rstrip()
                     for sid, r in d["vacuum"].items()]
            L.append("Vacuum: " + " · ".join(parts))
        L.append("Turbos (read-only): " + " · ".join(
            f"{p} {_f(x['state'])} {_f(x['frequency_hz'])} Hz (mode {_f(x['mode'])})"
            for p, x in d["turbos"].items()))
        if d["ramps"]:
            L.append("AI ramps: " + "; ".join(
                f"{r['zone']} → {_f(r['target_k'])} K at {_f(r['rate_k_per_min'])} K/min, now "
                f"{_f(r['current_k'])} K" for r in d["ramps"]))
        if d["warnings"]:
            L.append("WARNINGS:")
            L.extend(f"  - {x}" for x in d["warnings"])
        else:
            L.append("WARNINGS: none")
        if d["last_ai_actions"]:
            L.append("Last AI actions:")
            for e in d["last_ai_actions"]:
                L.append(f"  - {e.get('utc')} {e.get('kind')} {e.get('tool') or ''}: "
                         f"{e.get('detail')}" + (f" — “{e['reason']}”" if e.get("reason") else ""))
        return "\n".join(L)

    # ── list_states / get_states ─────────────────────────────────────────

    def list_states(self, args: Any = None) -> Tuple[bool, Any]:
        try:
            a = self._validate("list_states", args)
        except ArgError as e:
            return self._invalid("list_states", str(e))
        snap = self.bus.snapshot()
        states = snap.get("states", {}) if isinstance(snap, dict) else {}
        group, query = a.get("group"), (a.get("query") or "").lower()
        limit = a.get("limit", 200)
        rows, total = [], 0
        for sid, v in states.items():
            if not isinstance(v, dict):
                continue
            if group and v.get("group") != group:
                continue
            label = v.get("label") or ""
            if query and query not in sid.lower() and query not in str(label).lower():
                continue
            total += 1
            if len(rows) >= limit:
                continue
            value = v.get("value")
            vt = (repr(value) if isinstance(value, str) else _f(value))
            if len(vt) > 40:
                vt = vt[:37] + "..."
            unit = v.get("unit")
            rows.append(f"{sid} | {label} | {vt}{' ' + unit if unit else ''} | "
                        f"{v.get('freshness')} | ai: {ai_tool_for(sid) or '-'}")
        groups = sorted({v.get("group") for v in states.values() if isinstance(v, dict)
                         and v.get("group")})
        age = self.bus.snapshot_age()
        return False, {"ok": True, "tool": "list_states",
                       "snapshot_age_s": round(age, 1) if age is not None else None,
                       "columns": "id | label | value unit | freshness | ai: tool that writes it",
                       "count": len(rows), "matched": total, "rows": rows, "groups": groups}

    def get_states(self, args: Any = None) -> Tuple[bool, Any]:
        try:
            a = self._validate("get_states", args)
        except ArgError as e:
            return self._invalid("get_states", str(e))
        snap = self.bus.snapshot()
        states = snap.get("states", {}) if isinstance(snap, dict) else {}
        found, unknown, raw = {}, [], {}
        for sid in a["ids"]:
            v = states.get(sid)
            if not isinstance(v, dict):
                unknown.append(sid)
                continue
            found[sid] = {k: v[k] for k in ("id", "group", "kind", "label", "unit", "value",
                                            "freshness", "age_s", "source", "avg", "values")
                          if k in v}
            tool = ai_tool_for(sid)
            if tool:
                found[sid]["ai_tool"] = tool
            src = v.get("source")
            if isinstance(src, str) and src.startswith(_RAW_PREFIXES) and src not in raw:
                r = self.bus.raw(src)
                if r is not None:
                    live = self.bus.live_age(src)
                    raw[src] = {"payload": r["payload"],
                                "live_age_s": round(live, 1) if live is not None else None}
        age = self.bus.snapshot_age()
        out = {"ok": True, "tool": "get_states",
               "snapshot_age_s": round(age, 1) if age is not None else None,
               "states": found, "raw_status": raw}
        if unknown:
            out["unknown_ids"] = unknown
        return False, out

    # ── get_ai_control ────────────────────────────────────────────────────

    def get_ai_control(self, args: Any = None) -> Tuple[bool, Any]:
        try:
            self._validate("get_ai_control", args)
        except ArgError as e:
            return self._invalid("get_ai_control", str(e))
        status = self.status_fn() if self.status_fn is not None else {
            "arms": self.arms.snapshot(), "rails": self.rails.stored(),
            "rails_error": self.rails.error, "rails_stale_zones": self.rails.stale_zones,
            "may_now": may_now(self.arms, self.rails, self.bus)}
        out = {"ok": True, "tool": "get_ai_control"}
        out.update(status)
        out["rails_in_force"] = self.rails.get()
        out["rails_note"] = ("rails = the document as Lucas saved it; rails_in_force = what writes "
                             "are checked against (stale zones count as unset). Only Lucas can "
                             "change them, on the Control page.")
        if self.actions is not None:
            out["last_ai_setpoints"] = self.actions.zone_ai_setpoints()
        out["audit"] = self.audit.recent(20)
        return False, out

    # ── query_history ─────────────────────────────────────────────────────

    def query_history(self, args: Any = None) -> Tuple[bool, Any]:
        if self.history is None:
            return True, {"ok": False, "tool": "query_history",
                          "error": "history unavailable (the InfluxDB history module is not "
                                   "loaded on this service)"}
        return self.history.query({} if args is None else args)

    # ── get_guide ─────────────────────────────────────────────────────────

    def get_guide(self, args: Any = None) -> Tuple[bool, Any]:
        try:
            a = self._validate("get_guide", args)
        except ArgError as e:
            return self._invalid("get_guide", str(e))
        topic = a.get("topic", "all")
        parts = {"safety": self._guide_safety, "heaters": self._guide_heaters,
                 "ln2": self._guide_ln2, "pneumatic": self._guide_pneumatic,
                 "history": self._guide_history}
        if topic == "all":
            return False, "\n\n".join(fn() for fn in parts.values())
        return False, parts[topic]()

    def _guide_safety(self) -> str:
        return f"""\
## Safety model (read first)
- You act on the xsphere cryostat ONLY through these tools. Reads are always allowed.
- Writes need Lucas to ARM the group (heaters | ln2 | pneumatic) on the Control page
  ({CONTROL_PAGE}) and stay inside his RAILS (get_ai_control shows both). You can never
  change arms or rails, and nothing you can call does.
- Arms lapse after general.arm_hours and are dropped automatically when this service
  reconnects to the broker, slowcontrol restarts, or the snapshot is > 30 s old; heaters
  also on a PV-interlock trip or interlocks not-ok. Disarming moves no hardware.
- Every write is refused while the Sequencer runs or a tracker is enabled (they would
  overwrite you a second later), when the snapshot is > 5 s old, or slowcontrol's heartbeat
  is stale. One write at a time; ≤ 20 write calls a minute.
- A refusal is final: do not retry with variations or look for another route. Tell Lucas
  what was refused, quote the reason, say what he would have to change.
- `confirmed: false` means the status never showed the change: read get_overview and report
  before anything else.
- Give every write a short honest `reason` (plain text, one line); it is in the audit Lucas
  reads. add_note leaves him a logbook entry (≤ 6 a minute).
- Times: history and audit are UTC; Lucas works in US Eastern (America/New_York)."""

    def _guide_heaters(self) -> str:
        zl = "\n".join(f"  - {self.zones.describe(z)}" for z in ZONES)
        ch = "\n".join(f"  - NGE ch{c}: {self.zones.channels[c]['label']}" for c in CHANNELS)
        return f"""\
## Heaters (arm group: heaters)
- Zone names are SOFTWARE names; what each one physically is:
{zl}
{ch}
- Each zone is a PLC PID loop. Its setpoint is an expression the PLC re-asserts every
  poll; set_heater_setpoints stores a plain number (shown as e.g. "170.0") and replaces
  whatever was there — a non-numeric expression (e.g. "rtd5 + 10", set by a human) is only
  replaced with replace_expression: true.
- A PID regulates on its PV, chosen by pv_expr (set_pv_source: cube = LabJack cube RTD,
  clamp = PLC clamp RTD; nozzle: cube only). With no PV source the PV is frozen (500 K =
  left by the old interlock surrogate) and a setpoint does nothing — or, frozen low, heats
  open-loop.
  So raising a setpoint is refused unless pv_expr is set, evaluates, and the PV register
  agrees with it within 5 K. Lowering is always possible.
- PV interlock: LabJack RTD1–3 against the band [min_k, max_k] (get_overview). Any channel
  out of band or stale trips ALL zones: SP 3 K, output 0 every poll (PV untouched), no
  hysteresis; setpoints come back when it clears. AI setpoints must be ≥ 5 K inside the
  band. Heater writes are refused while tripped.
- Rails per zone: setpoint_min_k / setpoint_max_k (both needed), max_step_k (largest single
  change), max_rate_k_per_min (allowance = rate × minutes since that zone's last AI change,
  at most 10 minutes' worth). Both are measured from the current setpoint — or, while that
  is outside the rails (3 K after every slowcontrol restart), from the zone's measured PV
  when it has a live PV source. When even that point is outside the rails, the new value
  may land at most max_step_k inside the nearest rail edge (e.g. PV 295 K, rails
  150–200 K, step 10 K → 190–200 K). The readback's anchor_k / anchor_source say which.
- start_heater_ramp moves setpoints at a rate (≤ max_rate_k_per_min) every 10 s through
  the same checks; it stops on any refusal, a disarm, an outside change of the setpoint, a
  direct setpoint for that zone, or stop_heater_ramp.
- set_heater_gradient: top = base_k, bottom = base_k + dv_k, nozzle = base_k + dl_k
  (software names; dv = bottom − top, dl = nozzle − top). The gradient controller only
  adopts the PID setpoints; its gradient/* commands are not AI tools.
- The AI cannot put a zone into manual or set an output; set_heater_auto returns a manual
  zone to auto when its setpoint is a number inside the rails and the PV is live.
- NGE103 supply (set_heater_supply): voltage (0.01 V steps), current limit (0.001 A),
  output, sent in that order, each confirmed from the supply's status, ≤ 6 commands a
  minute. The supply caps the current limit at 33.6 W / V, so current_limit_a can read
  below the request — normal. Switching on, or raising V / I while on, needs the PID zone
  the channel feeds in auto at a numeric setpoint inside its rails and band, with a live
  PV source — the supply turns that PID's output into heat. Switching a channel off — or
  0 V / a 0 A limit — lets LN2 cool the cell, and needs may_switch_off."""

    def _guide_ln2(self) -> str:
        lines = "\n".join(f"  - {v}: {self.zones.vessels[v]['label']}; level "
                          f"{', '.join(self.zones.vessels[v]['level_states'])}"
                          + (f" + {self.zones.vessels[v]['esp32_state']}"
                             if self.zones.vessels[v]["esp32_state"] else "")
                          for v in VESSELS)
        return f"""\
## LN2 fill valves (arm group: ln2)
{lines}
- The PLC ladder does the filling logic: auto-close shuts the valve when the vessel is full
  or the fill timer runs out — and that timer only runs with auto-close armed; auto-open
  starts a fill when the level is low and only works with auto-close armed. The XV3
  (cryostat) ladder can clear auto-open by itself after a timed-out fill.
- set_ln2_valve open needs: may_operate and max_open_minutes in the rails, auto-close on
  (live status), a fresh level (cryostat: the USB probe level_cryostat_probe;
  ballast / primary_xe: the level plus the GHS ESP32 heartbeat — the PLC keeps the last
  value when that feed stops), the valve closed, and ≥ 10 s since the last command to it.
  The service closes a valve the AI opened after max_open_minutes (and disarms ln2) if a
  live status shows it still open.
- Closing turns auto-open off first — otherwise the ladder reopens a low vessel at once.
  If your open was never seen (not confirmed), a close is sent even though the valve
  reads closed — the open may still be queued — and the auto-close timer stays until the
  valve has been seen open and closed again, or until its deadline.
- set_ln2_auto runs in the safe order (auto-close on → auto-open off → auto-open on →
  auto-close off). auto-open on needs may_set_auto_open; auto-close off needs
  may_disarm_auto_close, a closed valve and auto-open off (already, or in the same call).
- Levels are 0–10 (probe in V). If the ladder closes a valve right after an open, the
  level was above its close threshold or the fill timer ran out."""

    def _guide_pneumatic(self) -> str:
        lines = "\n".join(f"  - {v}: {self.zones.valves[v]['label']}" for v in VALVES)
        return f"""\
## Pneumatic gas valves (arm group: pneumatic)
{lines}
- open / close are in VALVE terms. Ballast and bottle are normally OPEN (their relay
  closes them); the pump valve is normally CLOSED.
- Two locks: the AI arm AND the PLC's own pneumatic valve gate on the Control page. Both
  must be on; the PLC refuses a command while its gate is off and the tool reports that.
- Rails: may_open / may_close per valve, forbid_open_together pairs (checked against live
  status, and against an AI open of the partner that was not confirmed in the last 2 min —
  it may still be on its way), min_interval_s between commands to one valve.
- Ballast + bottle open is the walk-away-safe buffer for the xenon if cooling is lost;
  do not change them unless Lucas asked for exactly that."""

    def _guide_history(self) -> str:
        try:
            from .history import HISTORY_GUIDE
            return HISTORY_GUIDE.rstrip()
        except Exception:                          # noqa: BLE001
            return ("## History (query_history)\n- InfluxDB history of registry states; times "
                    "are UTC. (The history module is not loaded on this service.)")


def _round(x: Optional[float]) -> Optional[float]:
    return round(x, 1) if x is not None else None


def _level_text(sid: str, r: dict) -> str:
    unit = r.get("unit")
    shown = f" {unit}" if unit and unit != "0–10" else ""
    return f"{sid} {_f(r.get('value'))}{shown} ({r.get('freshness')})"
