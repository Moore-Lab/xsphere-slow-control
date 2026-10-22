"""
The AI's write tools: every path from a tool call to a command topic.

Every write goes through the same pipeline, in this order (DESIGN §8):

1. **Strict arguments.**  Exact JSON types against the tool's schema: a
   number is an int or float, never a bool, finite and not huge; a bool is a
   bool; enums are checked type-first (``True in (1, 2, 3)`` is true in
   Python); unknown keys are refused; every write carries a ``reason``.  The
   drivers coerce with ``int()`` / ``bool()`` / ``float()`` — ``"false"`` arms
   an LN2 auto flag, NaN drives a PID output to 100 % — so nothing loose may
   reach them.
2. **One write at a time.**  ``write_lock`` is taken without blocking; a
   second write is refused, never queued.  The heater ramp thread uses the
   same lock.  The lock is never needed by the MQTT thread, so a disarm is
   never stuck behind a write waiting for its confirmation.
3. **Armed** (the human's arm for the tool's group).
4. **Rails** for the specific item (an invalid rails file refuses everything).
5. **Preconditions**: broker connected, snapshot ≤ 5 s old, slowcontrol's
   heartbeat fresh, Sequencer not running, no enabled tracker (both would
   overwrite an AI write a second later while the AI is told "confirmed"),
   then the tool's own checks.
6. **Rate**: ≤ 20 write calls a minute overall, per-valve minimum intervals,
   ≤ 6 NGE commands a minute paced on the supply's own status.
7. **Publish** through :meth:`Bus.publish_command` (allowlisted topics only),
   re-checking the arm immediately before every single publish.
8. **Confirm** from the status topics (two consecutive messages where it
   matters); "not confirmed" is reported, and a PV source change that does not
   confirm is reverted.
9. **Audit** and count.

Refusals are results (``ok: false`` + a sentence saying what is missing), not
exceptions: the model has to read why.  The service's own safety actions (the
LN2 auto-close, reverting a PV source) are the only publishes made without an
arm.
"""

from __future__ import annotations

import ast
import collections
import copy
import json
import logging
import math
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable, Deque, Dict, List, Optional, Tuple

from .arms import CONTROL_PAGE, PV_INTERLOCK_TOPIC, utc_iso
from .zones import PNEUMATIC_GATE_TOPIC, VALVES, VESSELS, ZONES

log = logging.getLogger(__name__)

SEQUENCER_TOPIC = "xsphere/status/sequencer"
TRACKERS_TOPIC = "xsphere/status/trackers"
NGE_TOPIC = "xsphere/status/nge"

MAX_WRITES_PER_MIN = 20
NGE_MAX_PER_MIN = 6
LN2_MIN_INTERVAL_S = 10.0
SNAPSHOT_MAX_AGE_S = 5.0
PID_LIVE_S = 3.0             # PLC status is published every ~1 s poll
VALVE_LIVE_S = 3.0
GATE_LIVE_S = 3.0
NGE_LIVE_S = 5.0             # NGE status every 1 s poll; registry period 2 s
INTERLOCK_MARGIN_K = 5.0
PV_AGREE_K = 5.0             # |pv_k − pv_expr_value_k| for "the PV is being fed"
PV_SWITCH_MAX_JUMP_K = 10.0
RATE_CAP_MIN = 10.0          # token bucket holds at most 10 minutes' worth
SUPPLY_WARN_OUTPUT_PCT = 95.0
MAX_ABS_NUMBER = 1e6
MAX_REASON = 200
MAX_NOTE = 1000

# ──────────────────────────────────────────────────────────────────────────
# Argument schemas (also the MCP inputSchema — tools.py publishes these)
# ──────────────────────────────────────────────────────────────────────────

_REASON = {"type": "string", "minLength": 1, "maxLength": MAX_REASON,
           "description": "Why you are doing this, in one short honest sentence (≤ 200 "
                          "characters). It goes into the audit log Lucas reads."}
_K = {"type": "number", "minimum": 1, "maximum": 400}
_ZONE = {"type": "string", "enum": list(ZONES),
         "description": "Software zone name (see get_overview for the physical heater)."}
_REPLACE = {"type": "boolean",
            "description": "Only if Lucas wants it: replace a non-numeric setpoint expression "
                           "(e.g. 'rtd5 + 10') with your number. Default false: such a zone is "
                           "refused and the expression is left alone."}


def _zone_numbers(desc: str) -> dict:
    return {"type": "object", "minProperties": 1, "additionalProperties": False,
            "properties": {z: dict(_K, description=f"{desc} for software zone {z}, in K.")
                           for z in ZONES}}


WRITE_SCHEMAS: Dict[str, dict] = {
    "set_heater_setpoints": {
        "type": "object", "additionalProperties": False,
        "required": ["setpoints", "reason"],
        "properties": {
            "setpoints": dict(_zone_numbers("New PID setpoint"),
                              description="New setpoints in K by software zone name; one, two "
                                          "or all three zones. All are checked before any "
                                          "is sent."),
            "replace_expression": _REPLACE,
            "reason": _REASON}},
    "set_heater_gradient": {
        "type": "object", "additionalProperties": False,
        "required": ["base_k", "dv_k", "dl_k", "reason"],
        "properties": {
            "base_k": dict(_K, description="Setpoint of software zone top, in K."),
            "dv_k": {"type": "number", "minimum": -200, "maximum": 200,
                     "description": "bottom − top, in K (software zone names): bottom = "
                                    "base_k + dv_k."},
            "dl_k": {"type": "number", "minimum": -200, "maximum": 200,
                     "description": "nozzle − top, in K (software zone names): nozzle = "
                                    "base_k + dl_k."},
            "replace_expression": _REPLACE,
            "reason": _REASON}},
    "start_heater_ramp": {
        "type": "object", "additionalProperties": False,
        "required": ["targets", "rate_k_per_min", "reason"],
        "properties": {
            "targets": dict(_zone_numbers("Final setpoint"),
                            description="Final setpoints in K by software zone name."),
            "rate_k_per_min": {"type": "number", "exclusiveMinimum": 0, "maximum": 100,
                               "description": "Ramp speed in K per minute (> 0), the same for "
                                              "every zone; at most each zone's "
                                              "max_rate_k_per_min rail."},
            "reason": _REASON}},
    "stop_heater_ramp": {
        "type": "object", "additionalProperties": False,
        "required": ["reason"],
        "properties": {
            "zones": {"type": "array", "items": _ZONE, "minItems": 1, "maxItems": 3,
                      "uniqueItems": True,
                      "description": "Zones whose ramp to stop; default: every running ramp."},
            "reason": _REASON}},
    "set_heater_auto": {
        "type": "object", "additionalProperties": False,
        "required": ["zone", "reason"],
        "properties": {"zone": _ZONE, "reason": _REASON}},
    "set_pv_source": {
        "type": "object", "additionalProperties": False,
        "required": ["zone", "source", "reason"],
        "properties": {
            "zone": _ZONE,
            "source": {"type": "string", "enum": ["cube", "clamp"],
                       "description": "cube = the zone's LabJack cube RTD, clamp = its PLC "
                                      "clamp RTD (the nozzle has no clamp sensor)."},
            "reason": _REASON}},
    "set_heater_supply": {
        "type": "object", "additionalProperties": False, "minProperties": 3,
        "required": ["channel", "reason"],
        "properties": {
            "channel": {"type": "integer", "enum": [1, 2, 3],
                        "description": "NGE103 channel 1, 2 or 3 (get_overview shows which "
                                       "heater each feeds)."},
            "voltage_v": {"type": "number", "minimum": 0, "maximum": 32,
                          "description": "Voltage setpoint in V (sent rounded to 0.01 V)."},
            "current_a": {"type": "number", "minimum": 0, "maximum": 3,
                          "description": "Current limit in A (sent rounded to 0.001 A)."},
            "output": {"type": "boolean", "description": "true = switch the channel on, "
                                                         "false = off."},
            "reason": _REASON}},
    "set_ln2_valve": {
        "type": "object", "additionalProperties": False,
        "required": ["vessel", "open", "reason"],
        "properties": {
            "vessel": {"type": "string", "enum": list(VESSELS),
                       "description": "cryostat (XV3), primary_xe (XV2) or ballast (XV1)."},
            "open": {"type": "boolean", "description": "true = open the fill valve, "
                                                       "false = close it."},
            "reason": _REASON}},
    "set_ln2_auto": {
        "type": "object", "additionalProperties": False, "minProperties": 3,
        "required": ["vessel", "reason"],
        "properties": {
            "vessel": {"type": "string", "enum": list(VESSELS),
                       "description": "cryostat (XV3), primary_xe (XV2) or ballast (XV1)."},
            "auto_open": {"type": "boolean", "description": "PLC auto-open (fill when low)."},
            "auto_close": {"type": "boolean",
                           "description": "PLC auto-close (close when full / fill timer)."},
            "reason": _REASON}},
    "set_pneumatic_valve": {
        "type": "object", "additionalProperties": False,
        "required": ["valve", "open", "reason"],
        "properties": {
            "valve": {"type": "string", "enum": list(VALVES),
                      "description": "ballast, pump or bottle gas valve."},
            "open": {"type": "boolean", "description": "true = open (valve terms), "
                                                       "false = close."},
            "reason": _REASON}},
    "add_note": {
        "type": "object", "additionalProperties": False,
        "required": ["text"],
        "properties": {
            "text": {"type": "string", "minLength": 1, "maxLength": MAX_NOTE,
                     "description": "The logbook entry (≤ 1000 characters)."}}},
}

#: Which arm each write tool needs (None: no arm, e.g. stopping or a note).
GROUP_OF: Dict[str, Optional[str]] = {
    "set_heater_setpoints": "heaters", "set_heater_gradient": "heaters",
    "start_heater_ramp": "heaters", "stop_heater_ramp": None, "set_heater_auto": "heaters",
    "set_pv_source": "heaters", "set_heater_supply": "heaters", "set_ln2_valve": "ln2",
    "set_ln2_auto": "ln2", "set_pneumatic_valve": "pneumatic", "add_note": None,
}


class ArgError(ValueError):
    """Arguments that do not match the tool's schema."""


def _show(value: Any) -> str:
    try:
        text = json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        text = repr(value)
    return text if len(text) <= 40 else text[:37] + "..."


def _is_num(x: Any) -> bool:
    """A finite int/float that is not a bool."""
    if isinstance(x, bool) or not isinstance(x, (int, float)):
        return False
    try:
        return math.isfinite(float(x))
    except OverflowError:
        return False


def validate_args(schema: dict, value: Any, path: str = "") -> Any:
    """Check ``value`` against the JSON-schema subset used by the tools.

    Returns a deep copy; raises :class:`ArgError` naming the first problem.
    Stricter than JSON schema where the drivers need it: numbers must be
    finite and within ±1e6, and a string with ``minLength`` must have that
    many non-blank characters.
    """
    where = path or "arguments"
    t = schema.get("type")
    if t == "object":
        if not isinstance(value, dict):
            raise ArgError(f"{where}: must be an object (got {_show(value)})")
        props = schema.get("properties", {})
        for k in value:
            if k not in props:
                allowed = ", ".join(props) or "none"
                raise ArgError(f"{where}: unknown key {_show(k)} (allowed: {allowed})")
        for k in schema.get("required", ()):
            if k not in value:
                raise ArgError(f"{where}: missing required key {_show(k)}")
        if len(value) < schema.get("minProperties", 0):
            raise ArgError(f"{where}: needs at least {schema['minProperties']} keys")
        return {k: validate_args(props[k], v, f"{path}.{k}" if path else k)
                for k, v in value.items()}
    if t in ("number", "integer"):
        if t == "integer":
            ok = isinstance(value, int) and not isinstance(value, bool)
            kind = "an integer"
        else:
            ok = _is_num(value)
            kind = "a finite number"
        if not ok:
            raise ArgError(f"{where}: must be {kind} (got {_show(value)})")
        try:
            too_big = abs(float(value)) > MAX_ABS_NUMBER
        except OverflowError:
            too_big = True
        if too_big:
            raise ArgError(f"{where}: {_show(value)} is out of range")
        if "enum" in schema and value not in schema["enum"]:
            raise ArgError(f"{where}: must be one of {', '.join(map(str, schema['enum']))} "
                           f"(got {_show(value)})")
        if "minimum" in schema and value < schema["minimum"]:
            raise ArgError(f"{where}: must be ≥ {schema['minimum']} (got {_show(value)})")
        if "maximum" in schema and value > schema["maximum"]:
            raise ArgError(f"{where}: must be ≤ {schema['maximum']} (got {_show(value)})")
        if "exclusiveMinimum" in schema and not value > schema["exclusiveMinimum"]:
            raise ArgError(f"{where}: must be > {schema['exclusiveMinimum']} "
                           f"(got {_show(value)})")
        return value
    if t == "boolean":
        if not isinstance(value, bool):
            raise ArgError(f"{where}: must be true or false (got {_show(value)})")
        return value
    if t == "string":
        if not isinstance(value, str):
            raise ArgError(f"{where}: must be a string (got {_show(value)})")
        if "enum" in schema and value not in schema["enum"]:
            raise ArgError(f"{where}: must be one of {', '.join(schema['enum'])} "
                           f"(got {_show(value)})")
        if len(value.strip()) < schema.get("minLength", 0):
            raise ArgError(f"{where}: must not be empty")
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            raise ArgError(f"{where}: at most {schema['maxLength']} characters "
                           f"(got {len(value)})")
        return value
    if t == "array":
        if not isinstance(value, list):
            raise ArgError(f"{where}: must be a list (got {_show(value)})")
        if len(value) < schema.get("minItems", 0):
            raise ArgError(f"{where}: needs at least {schema['minItems']} item(s)")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            raise ArgError(f"{where}: at most {schema['maxItems']} items (got {len(value)})")
        items = schema.get("items", {})
        out = [validate_args(items, v, f"{where}[{i}]") for i, v in enumerate(value)]
        if schema.get("uniqueItems"):
            seen = []
            for v in out:
                if v in seen:
                    raise ArgError(f"{where}: {_show(v)} listed twice")
                seen.append(v)
        return out
    raise ArgError(f"{where}: unsupported schema type {t!r}")


# ──────────────────────────────────────────────────────────────────────────
# Setpoint expressions
# ──────────────────────────────────────────────────────────────────────────

def parse_numeric_expr(expr: Any) -> Optional[float]:
    """The value of a setpoint expression that is a plain number, else None.

    The PLC driver evaluates expressions with ``ast``; a constant setpoint is
    stored as ``repr(float(K))``.  Accepted here: a numeric literal (int or
    float, not bool) or one unary +/- applied to one, and finite.  ``"nan"``,
    ``"inf"`` and ``"1e999"`` (which ``float()`` would happily accept) are not
    numbers here, and anything with a name or an operator is an expression.
    """
    if not isinstance(expr, str):
        return None
    text = expr.strip()
    if not text or len(text) > 64:
        return None
    try:
        node = ast.parse(text, mode="eval").body
    except (SyntaxError, ValueError, MemoryError, RecursionError):
        return None
    sign = 1.0
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
        sign = -1.0 if isinstance(node.op, ast.USub) else 1.0
        node = node.operand
    if (not isinstance(node, ast.Constant) or isinstance(node.value, bool)
            or not isinstance(node.value, (int, float))):
        return None
    try:
        value = sign * float(node.value)
    except OverflowError:
        return None
    return value if math.isfinite(value) else None


def _fmt(x: Any, unit: str = "") -> str:
    if _is_num(x):
        s = f"{float(x):.6g}"
    elif x is None:
        s = "?"
    else:
        s = str(x)
    return f"{s} {unit}".rstrip() if unit else s


def _parse_utc(text: Any) -> Optional[float]:
    if not isinstance(text, str) or not text:
        return None
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


class Refusal(Exception):
    """A write refused (or stopped) for a reason the AI must read."""

    def __init__(self, text: str):
        super().__init__(text)
        self.text = text


class Failure(Refusal):
    """The write went out but did not take effect (reported as ``error``)."""


class _Call:
    """Book-keeping for one write call."""

    def __init__(self, tool: str, args: dict):
        self.tool = tool
        self.args = args
        self.group = GROUP_OF.get(tool)
        self.reason = args.get("reason") if isinstance(args, dict) else None
        self.published: List[dict] = []
        self.warnings: List[str] = []
        self.readback: Dict[str, Any] = {}
        self.confirmed: Optional[bool] = None
        self.note: Optional[str] = None
        self.detail = ""
        self.ramp_id: Optional[int] = None    # set for a ramp step

    def args_for_audit(self) -> dict:
        return {k: v for k, v in self.args.items() if k != "reason"}


# ──────────────────────────────────────────────────────────────────────────
# Actions
# ──────────────────────────────────────────────────────────────────────────

class Actions:
    """The write tools.  Each public tool method takes the raw arguments dict
    and returns ``(is_error, result)``."""

    def __init__(self, bus, zones, rails, arms, audit, *, ramps=None, timers=None,
                 clock: Callable[[], float] = time.time, confirm_timeout_s: float = 5.0,
                 nge_timeout_s: float = 5.0, on_change: Optional[Callable[[], None]] = None):
        self.bus = bus
        self.zones = zones
        self.rails = rails
        self.arms = arms
        self.audit = audit
        self.ramps = ramps
        self.timers = timers
        self._clock = clock
        self.confirm_timeout_s = float(confirm_timeout_s)
        self.nge_timeout_s = float(nge_timeout_s)
        self._on_change = on_change
        self.write_lock = threading.Lock()
        self.started_ts = clock()

        self._stats_lock = threading.Lock()
        self.counters = {"actions": 0, "refused": 0}
        self.last_action: Optional[dict] = None
        self.last_refused: Optional[dict] = None
        self._write_times: Deque[float] = collections.deque()
        self._nge_times: Deque[float] = collections.deque()
        self._nge_last: Optional[dict] = None
        self._zone_ai: Dict[str, dict] = {}         # zone → {"value", "ts"} of the last AI setpoint
        self._ln2_last: Dict[str, float] = {}
        self._pn_last: Dict[str, float] = {}
        if ramps is not None:
            ramps.set_stepper(self.ramp_step)

    def set_on_change(self, fn: Optional[Callable[[], None]]) -> None:
        self._on_change = fn

    def handlers(self) -> Dict[str, Callable[[dict], Tuple[bool, dict]]]:
        """``{tool name: method}`` for the registry."""
        return {name: getattr(self, name) for name in WRITE_SCHEMAS}

    def zone_ai_setpoints(self) -> Dict[str, dict]:
        with self._stats_lock:
            return copy.deepcopy(self._zone_ai)

    def stats(self) -> dict:
        with self._stats_lock:
            return {"actions": self.counters["actions"], "refused": self.counters["refused"],
                    "last_action": copy.deepcopy(self.last_action),
                    "last_refused": copy.deepcopy(self.last_refused)}

    # ── pipeline plumbing ─────────────────────────────────────────────────

    def _run(self, tool: str, args: Any, body: Callable[[_Call], None], *,
             lock: bool = True) -> Tuple[bool, dict]:
        try:
            a = validate_args(WRITE_SCHEMAS[tool], args)
        except ArgError as e:
            return self._invalid(tool, args, str(e))
        call = _Call(tool, a)
        if lock and not self.write_lock.acquire(blocking=False):
            return self._refused(call, "another AI write is in progress — try again in a few "
                                       "seconds (writes are never queued)")
        try:
            try:
                body(call)
            except Refusal as r:
                return self._refused(call, r.text, failed=isinstance(r, Failure))
            except Exception as e:                   # a bug: never leave it unaudited
                log.exception("Write tool %s failed", tool)
                return self._refused(call, f"internal error: {type(e).__name__}: {e}",
                                     internal=True)
            return self._succeeded(call)
        finally:
            if lock:
                self.write_lock.release()

    def _invalid(self, tool: str, args: Any, msg: str) -> Tuple[bool, dict]:
        text = f"invalid arguments: {msg}"
        try:
            shown = args if len(json.dumps(args, default=str)) <= 2000 else "(too large)"
        except (TypeError, ValueError):
            shown = "(not JSON)"
        self._record_refused(tool, text, args=shown, reason=None, published=[])
        return True, {"ok": False, "tool": tool, "error": text, "invalid_arguments": True}

    def _refused(self, call: _Call, text: str, *, failed: bool = False,
                 internal: bool = False) -> Tuple[bool, dict]:
        res: Dict[str, Any] = {"ok": False, "tool": call.tool}
        res["error" if (failed or internal) else "refused"] = text
        if internal:
            res["internal"] = True
        res.update({"published": call.published,
                    "confirmed": call.confirmed if call.published else None,
                    "readback": call.readback, "warnings": call.warnings})
        if call.note:
            res["note"] = call.note
        detail = text if not call.published else (
            f"{text} — after publishing {len(call.published)} command(s)")
        self._record_refused(call.tool, detail, args=call.args_for_audit(), reason=call.reason,
                             published=call.published)
        return True, res

    def _record_refused(self, tool: str, detail: str, *, args: Any, reason: Optional[str],
                        published: list) -> None:
        log.warning("AI %s refused: %s", tool, detail)
        utc = utc_iso(self._clock())
        with self._stats_lock:
            self.counters["refused"] += 1
            self.last_refused = {"utc": utc, "tool": tool, "detail": detail}
        self.audit.record("refused", detail, tool=tool, args=args, reason=reason,
                          published=published or None)
        self._changed()

    def _succeeded(self, call: _Call) -> Tuple[bool, dict]:
        res: Dict[str, Any] = {"ok": True, "tool": call.tool, "published": call.published,
                               "confirmed": call.confirmed, "readback": call.readback,
                               "warnings": call.warnings}
        if call.note:
            res["note"] = call.note
        detail = call.detail or call.tool
        if call.published and call.confirmed is False:
            detail += " — NOT confirmed"
        log.info("AI %s: %s", call.tool, detail)
        utc = utc_iso(self._clock())
        with self._stats_lock:
            self.counters["actions"] += 1
            self.last_action = {"utc": utc, "tool": call.tool, "detail": detail,
                                "reason": call.reason}
        kind = "note" if call.tool == "add_note" else "action"
        self.audit.record(kind, detail, tool=call.tool,
                          args=call.args_for_audit() if kind == "action" else None,
                          reason=call.reason, published=call.published or None,
                          confirmed=call.confirmed if call.published else None,
                          warnings=call.warnings or None)
        self._changed()
        return False, res

    def _changed(self) -> None:
        if self._on_change is not None:
            try:
                self._on_change()
            except Exception:
                log.exception("Error in actions change callback")

    def _publish(self, call: _Call, topic: str, payload: dict, *, check_arm: bool = True) -> None:
        """One command.  The arm is re-checked right here, before every publish."""
        if check_arm and call.group is not None and not self.arms.is_armed(call.group):
            if call.published:
                raise Refusal(f"{call.group} were disarmed during the call; "
                              f"{len(call.published)} command(s) had already been sent, the "
                              f"rest were not")
            raise Refusal(self.arms.not_armed_text(call.group))
        if call.ramp_id is not None and self.ramps is not None \
                and not self.ramps.is_active(call.args["zone"], call.ramp_id):
            raise Refusal("the ramp was stopped")
        if not self.bus.publish_command(topic, payload):
            raise Refusal("the MQTT publish failed (is the broker connection down?)"
                          + (f"; {len(call.published)} command(s) had already been sent"
                             if call.published else ""))
        call.published.append({"topic": topic, "payload": copy.deepcopy(payload)})

    # ── common checks ────────────────────────────────────────────────────

    def _require_armed(self, call: _Call) -> None:
        if call.group is not None and not self.arms.is_armed(call.group):
            raise Refusal(self.arms.not_armed_text(call.group))

    def _require_rails_ok(self) -> None:
        err = self.rails.error
        if err:
            raise Refusal(f"Lucas's rails file is invalid, so nothing is writable until he "
                          f"saves the rails again on the Control page ({CONTROL_PAGE}): {err}")

    def _require_zone_rails(self, zone: str) -> Tuple[float, float]:
        if zone in self.rails.stale_zones:
            raise Refusal(f"zone {zone}: Lucas's heater rails for it were saved when the "
                          f"software zone names pointed at other sensors (the zone mapping "
                          f"changed since), so they count as unset until he saves the rails "
                          f"again on the Control page")
        lims = self.rails.zone_limits(zone)
        if lims is None:
            raise Refusal(f"zone {zone}: Lucas has not set setpoint rails for it "
                          f"(setpoint_min_k and setpoint_max_k must both be set), so its "
                          f"setpoint is not writable")
        return lims

    def _require_global(self) -> None:
        if not self.bus.connected():
            raise Refusal("the AI service is not connected to the MQTT broker")
        age = self.bus.snapshot_age()
        if age is None:
            raise Refusal("no slowcontrol snapshot has been received yet")
        if age > SNAPSHOT_MAX_AGE_S:
            raise Refusal(f"the slowcontrol snapshot is {age:.0f} s old (needs ≤ "
                          f"{SNAPSHOT_MAX_AGE_S:.0f} s) — is xsphere-slowcontrol running?")
        if self.bus.state_value("service_alive") is not True:
            raise Refusal("slowcontrol's heartbeat (service_alive) is not fresh")
        seq = self.bus.raw(SEQUENCER_TOPIC)
        if seq is not None:
            p = seq["payload"]
            if not isinstance(p, dict):
                raise Refusal("the Sequencer status is unreadable, so it cannot be ruled out "
                              "that a sequence is running")
            if p.get("running"):
                step = p.get("current_step")
                raise Refusal(f"the Sequencer is running (item {step}) — it rewrites setpoints "
                              f"and valves, so AI writes are refused until Lucas stops it")
        trk = self.bus.raw(TRACKERS_TOPIC)
        if trk is not None:
            p = trk["payload"]
            if not isinstance(p, list):
                raise Refusal("the trackers status is unreadable, so it cannot be ruled out "
                              "that a tracker is rewriting a setpoint")
            on = [t for t in p if not isinstance(t, dict) or t.get("enabled")]
            if on:
                names = ", ".join(f"{t.get('id')} (→ {t.get('target')})" if isinstance(t, dict)
                                  else _show(t) for t in on[:5])
                raise Refusal(f"tracker(s) enabled: {names} — a tracker rewrites its target "
                              f"every second, so AI writes are refused until Lucas disables "
                              f"them")

    def _take_write_slot(self) -> None:
        now = self._clock()
        with self._stats_lock:
            while self._write_times and now - self._write_times[0] >= 60.0:
                self._write_times.popleft()
            if len(self._write_times) >= MAX_WRITES_PER_MIN:
                wait = 60.0 - (now - self._write_times[0])
                raise Refusal(f"rate limit: at most {MAX_WRITES_PER_MIN} AI write calls a "
                              f"minute — wait {wait:.0f} s")
            self._write_times.append(now)

    def _interlock(self, *, require_untripped: bool = True) -> dict:
        r = self.bus.raw(PV_INTERLOCK_TOPIC)
        p = r["payload"] if r else None
        if not isinstance(p, dict) or not _is_num(p.get("min_k")) or not _is_num(p.get("max_k")):
            raise Refusal("the PV interlock status (xsphere/status/pv_interlock) has not been "
                          "seen, so its band is unknown — heater writes are refused")
        if require_untripped and p.get("tripped") is not False:
            reasons = p.get("reasons")
            why = ("; ".join(f"RTD{k}: {v}" for k, v in reasons.items())
                   if isinstance(reasons, dict) and reasons else "no reason given")
            raise Refusal(f"the PV interlock is tripped ({why}); it holds every zone at SP 3 K "
                          f"/ output 0, and heater writes are refused until it clears")
        return p

    def _status_topic(self, zone: str) -> str:
        return self.zones.zones[zone]["status_topic"]

    def _pid_status(self, zone: str) -> dict:
        topic = self._status_topic(zone)
        age = self.bus.live_age(topic)
        if age is None or age > PID_LIVE_S:
            raise Refusal(f"zone {zone}: no live PID status from the PLC "
                          + ("since the service started" if age is None else f"for {age:.0f} s")
                          + f" (needs ≤ {PID_LIVE_S:.0f} s) — the PLC driver may be down")
        r = self.bus.raw(topic)
        p = r["payload"] if r else None
        if not isinstance(p, dict):
            raise Refusal(f"zone {zone}: the PID status is unreadable")
        return p

    def _seq(self, topic: str) -> int:
        r = self.bus.raw(topic)
        return int(r["seq"]) if r else 0

    def _wait_consecutive(self, checks: List[Tuple[str, int, Callable[[dict], bool]]],
                          n: int = 2, timeout: Optional[float] = None) -> bool:
        """True once, for every (topic, seq_before, cond), the last ``n``
        messages on the topic all arrived after the publish and satisfy cond."""
        def ok_one(topic, seq0, cond):
            r = self.bus.raw(topic)
            if r is None or r["seq"] < seq0 + n:
                return False
            payloads = [r["payload"]] + ([r["prev_payload"]] if n >= 2 else [])
            return all(isinstance(p, dict) and _safe(cond, p) for p in payloads)

        return self.bus.wait_for(lambda: all(ok_one(*c) for c in checks),
                                 self.confirm_timeout_s if timeout is None else timeout)

    def _payload(self, topic: str) -> Optional[dict]:
        r = self.bus.raw(topic)
        p = r["payload"] if r else None
        return p if isinstance(p, dict) else None

    # ── heaters: setpoint checks ─────────────────────────────────────────

    def _effective(self, zone: str, status: dict, replace: bool) -> Tuple[float, dict]:
        """The zone's current setpoint as the rails see it, and where it came from."""
        expr = status.get("setpoint_expr", "")
        if expr is None:
            expr = ""
        if not isinstance(expr, str):
            raise Refusal(f"zone {zone}: the PID status setpoint_expr is not text")
        sp_k = status.get("setpoint_k")
        if not expr.strip():
            if not _is_num(sp_k):
                raise Refusal(f"zone {zone}: the PID status has no setpoint_k")
            return float(sp_k), {"source": "register", "expr": ""}
        value = parse_numeric_expr(expr)
        if value is not None:
            return value, {"source": "expr", "expr": expr}
        if not replace:
            raise Refusal(f"zone {zone}: its setpoint is the expression {expr!r}, not a number; "
                          f"a direct setpoint would replace it. Repeat with "
                          f"replace_expression: true only if Lucas wants that expression "
                          f"replaced")
        ev = status.get("setpoint_expr_value_k")
        if _is_num(ev):
            return float(ev), {"source": "replaced", "expr": expr}
        if _is_num(sp_k):
            return float(sp_k), {"source": "replaced", "expr": expr}
        raise Refusal(f"zone {zone}: the expression {expr!r} has no current value")

    def _require_pv_fed(self, zone: str, status: dict) -> None:
        pv_expr = status.get("pv_expr")
        pv_val = status.get("pv_expr_value_k")
        pv_k = status.get("pv_k")
        if not isinstance(pv_expr, str) or not pv_expr.strip():
            why = "its pv_expr is empty, so nothing feeds the PID's process value"
            if _is_num(pv_k) and abs(float(pv_k) - 500.0) < 0.5:
                why += " (500 K is the PV interlock's surrogate)"
        elif not _is_num(pv_val):
            why = f"its pv_expr {pv_expr!r} does not evaluate"
        elif not _is_num(pv_k) or abs(float(pv_k) - float(pv_val)) > PV_AGREE_K:
            why = (f"its PV register reads {_fmt(pv_k, 'K')} but pv_expr {pv_expr!r} is "
                   f"{_fmt(pv_val, 'K')}")
        else:
            return
        raise Refusal(f"PID {zone} sees {_fmt(pv_k, 'K')} — no live PV source ({why}); choose "
                      f"one with set_pv_source first. A setpoint increase on a frozen PV would "
                      f"heat open-loop")

    def _check_band(self, zone: str, v: float, il: dict) -> None:
        lo_b = float(il["min_k"]) + INTERLOCK_MARGIN_K
        hi_b = float(il["max_k"]) - INTERLOCK_MARGIN_K
        if not lo_b <= v <= hi_b:
            raise Refusal(f"zone {zone}: {_fmt(v, 'K')} is outside the PV interlock band less "
                          f"its {INTERLOCK_MARGIN_K:g} K margin ({_fmt(lo_b)}–{_fmt(hi_b)} K); "
                          f"a setpoint there would trip the interlock for all three zones")

    def _check_setpoint(self, zone: str, v: float, replace: bool, status: dict, il: dict,
                        now: float) -> dict:
        """Every value check for one zone's new setpoint; raises Refusal."""
        lo, hi = self._require_zone_rails(zone)
        zr = self.rails.zone(zone)
        if not lo <= v <= hi:
            raise Refusal(f"zone {zone}: {_fmt(v, 'K')} is outside Lucas's rails "
                          f"{_fmt(lo)}–{_fmt(hi)} K")
        self._check_band(zone, v, il)
        e, info = self._effective(zone, status, replace)
        delta = abs(v - e)
        toward = (e < lo and v > e) or (e > hi and v < e)
        if not toward:
            step = zr.get("max_step_k")
            # 1 mK of slack for the ramp's 0.001 K rounding; physically nothing.
            if step is not None and delta > float(step) + 1e-3:
                raise Refusal(f"zone {zone}: {_fmt(e)} → {_fmt(v)} K is a change of "
                              f"{_fmt(delta)} K; Lucas's max_step_k is {_fmt(step)} K. Use "
                              f"smaller steps or start_heater_ramp")
            rate = zr.get("max_rate_k_per_min")
            if rate is not None:
                with self._stats_lock:
                    last = self._zone_ai.get(zone)
                anchor = last["ts"] if last else self.started_ts
                minutes = min(max((now - anchor) / 60.0, 0.0), RATE_CAP_MIN)
                allowance = float(rate) * minutes
                if step is not None:
                    allowance = min(allowance, float(step))
                if delta > allowance + 1e-3:
                    since = ("since the last AI change" if last else "since the service started")
                    raise Refusal(f"zone {zone}: a change of {_fmt(delta)} K exceeds the rate "
                                  f"allowance of {allowance:.2f} K (max_rate_k_per_min "
                                  f"{_fmt(rate)} K/min, {minutes:.1f} min {since}). Wait, or "
                                  f"use start_heater_ramp")
        if v > e + 1e-9:
            self._require_pv_fed(zone, status)
        warnings = []
        mode = status.get("mode")
        if mode != "auto":
            warnings.append(f"zone {zone} is in mode {mode!r}: the setpoint has no effect until "
                            f"the zone is in auto (set_heater_auto)")
        if info["source"] == "replaced":
            warnings.append(f"zone {zone}: replaced the setpoint expression {info['expr']!r}")
        return {"E": e, "expr": info["expr"], "source": info["source"], "warnings": warnings}

    # ── heaters: tools ───────────────────────────────────────────────────

    def set_heater_setpoints(self, args: Any) -> Tuple[bool, dict]:
        def body(call: _Call) -> None:
            self._setpoints(call, dict(call.args["setpoints"]),
                            bool(call.args.get("replace_expression", False)))
        return self._run("set_heater_setpoints", args, body)

    def set_heater_gradient(self, args: Any) -> Tuple[bool, dict]:
        def body(call: _Call) -> None:
            a = call.args
            base = float(a["base_k"])
            targets = {"top": round(base, 6), "bottom": round(base + float(a["dv_k"]), 6),
                       "nozzle": round(base + float(a["dl_k"]), 6)}
            self._setpoints(call, targets, bool(a.get("replace_expression", False)))
        return self._run("set_heater_gradient", args, body)

    def _setpoints(self, call: _Call, targets: Dict[str, float], replace: bool) -> None:
        self._require_armed(call)
        self._require_rails_ok()
        for z in targets:
            self._require_zone_rails(z)
        self._require_global()
        il = self._interlock()
        now = self._clock()
        plans = {}
        for z, v in targets.items():
            plans[z] = self._check_setpoint(z, float(v), replace, self._pid_status(z), il, now)
            call.warnings.extend(plans[z]["warnings"])
        self._take_write_slot()

        seqs = {}
        for z, v in targets.items():
            if self.ramps is not None and self.ramps.is_active(z):
                self.ramps.stop([z], "replaced by a direct AI setpoint")
            seqs[z] = self._seq(self._status_topic(z))
            self._publish(call, f"xsphere/commands/pid/{z}/setpoint", {"value_k": float(v)})
            with self._stats_lock:
                self._zone_ai[z] = {"value": float(v), "ts": self._clock()}
        call.detail = ", ".join(f"{z} {_fmt(plans[z]['E'])} → {_fmt(v)} K"
                                for z, v in targets.items())

        checks = [(self._status_topic(z), seqs[z],
                   (lambda p, want=repr(float(v)): p.get("setpoint_expr") == want))
                  for z, v in targets.items()]
        call.confirmed = self._wait_consecutive(checks)
        for z, v in targets.items():
            st = self._payload(self._status_topic(z)) or {}
            keys = ("setpoint_expr", "setpoint_k", "mode", "pv_k", "pv_expr", "output_pct")
            call.readback[z] = {k: st.get(k) for k in keys}
            call.readback[z]["previous_setpoint_k"] = plans[z]["E"]
            call.readback[z]["previous_expr"] = plans[z]["expr"]
            sp = st.get("setpoint_k")
            if call.confirmed and _is_num(sp) and abs(float(sp) - float(v)) > 0.01:
                call.warnings.append(f"zone {z}: setpoint_k reads {_fmt(sp, 'K')} although the "
                                     f"expression is set (the PV interlock forces 3 K while "
                                     f"tripped)")
        if not call.confirmed:
            call.warnings.append("not confirmed: the PID status did not show the new setpoint "
                                 "expression on two consecutive messages within "
                                 f"{self.confirm_timeout_s:g} s — read get_overview before "
                                 "doing anything else")

    def start_heater_ramp(self, args: Any) -> Tuple[bool, dict]:
        def body(call: _Call) -> None:
            if self.ramps is None:
                raise Refusal("heater ramps are not available in this service")
            a = call.args
            targets = {z: float(v) for z, v in a["targets"].items()}
            rate = float(a["rate_k_per_min"])
            self._require_armed(call)
            self._require_rails_ok()
            for z in targets:
                self._require_zone_rails(z)
            self._require_global()
            il = self._interlock()
            plans = {}
            for z, target in targets.items():
                lo, hi = self._require_zone_rails(z)
                if not lo <= target <= hi:
                    raise Refusal(f"zone {z}: the target {_fmt(target, 'K')} is outside Lucas's "
                                  f"rails {_fmt(lo)}–{_fmt(hi)} K")
                self._check_band(z, target, il)
                max_rate = self.rails.zone(z).get("max_rate_k_per_min")
                if max_rate is not None and rate > float(max_rate) + 1e-9:
                    raise Refusal(f"zone {z}: {_fmt(rate)} K/min is faster than Lucas's "
                                  f"max_rate_k_per_min {_fmt(max_rate)} K/min")
                status = self._pid_status(z)
                expr = status.get("setpoint_expr") or ""
                if isinstance(expr, str) and expr.strip() and parse_numeric_expr(expr) is None:
                    raise Refusal(f"zone {z}: its setpoint is the expression {expr!r}; a ramp "
                                  f"only starts from a numeric setpoint (set one first with "
                                  f"set_heater_setpoints, replace_expression true, if Lucas "
                                  f"agrees)")
                e, _info = self._effective(z, status, False)
                if not lo <= e <= hi:
                    raise Refusal(f"zone {z}: its current setpoint {_fmt(e, 'K')} is outside "
                                  f"the rails {_fmt(lo)}–{_fmt(hi)} K; a ramp steps inside the "
                                  f"rails only — set a setpoint inside the range first "
                                  f"(set_heater_setpoints allows a move into the range)")
                if target > e + 1e-9:
                    self._require_pv_fed(z, status)
                if status.get("mode") != "auto":
                    call.warnings.append(f"zone {z} is in mode {status.get('mode')!r}: the "
                                         f"setpoint has no effect until it is in auto")
                plans[z] = {"E": e, "expr": expr}
            self._take_write_slot()
            started, at_target = [], []
            for z, target in targets.items():
                if abs(target - plans[z]["E"]) < 1e-6:
                    at_target.append(z)
                    continue
                self.ramps.start_ramp(z, target, rate, call.reason or "", plans[z]["expr"],
                                      plans[z]["E"])
                started.append(f"{z} {_fmt(plans[z]['E'])} → {_fmt(target)} K")
            call.detail = (f"ramp at {_fmt(rate)} K/min: " + (", ".join(started) or "nothing")
                           + (f"; already at target: {', '.join(at_target)}" if at_target else ""))
            call.readback["ramps"] = self.ramps.status()
            call.note = (f"The service moves each setpoint every {self.ramps.interval_s:g} s "
                         "through the same checks as set_heater_setpoints, and stops the ramp on "
                         "any refusal, a disarm, an outside change of the setpoint or "
                         "stop_heater_ramp. Progress: get_ai_control.")
        return self._run("start_heater_ramp", args, body)

    def stop_heater_ramp(self, args: Any) -> Tuple[bool, dict]:
        def body(call: _Call) -> None:
            zones = call.args.get("zones")
            if self.ramps is None:
                stopped = []
            else:
                before = {e["zone"]: e for e in self.ramps.status()}
                stopped = self.ramps.stop(zones, f"stop_heater_ramp: {call.reason}", audit=False)
                call.readback["stopped"] = [
                    {"zone": z, "target_k": before[z]["target_k"],
                     "setpoint_k": before[z]["current_k"]} for z in stopped if z in before]
            call.detail = ("stopped ramp(s): " + ", ".join(stopped)) if stopped \
                else "no ramp was running"
            if not stopped:
                call.note = "no ramp was running" + (f" for {', '.join(zones)}" if zones else "")
        return self._run("stop_heater_ramp", args, body, lock=False)

    def ramp_step(self, zone: str, entry: dict) -> Tuple[str, Any]:
        """One ramp step for ``zone`` (called by the ramp thread).

        Same checks as a direct setpoint; never waits for a confirmation (the
        next step's "is the setpoint still what the ramp wrote?" check is the
        confirmation), so the write lock is only held for the checks and the
        publish.
        """
        if not self.write_lock.acquire(blocking=False):
            return "busy", None
        try:
            if self.ramps is None or not self.ramps.is_active(zone, entry["id"]):
                return "busy", None
            call = _Call("start_heater_ramp", {"zone": zone, "reason": entry.get("reason")})
            call.ramp_id = entry["id"]
            try:
                self._require_armed(call)
                self._require_rails_ok()
                self._require_zone_rails(zone)
                self._require_global()
                il = self._interlock()
                status = self._pid_status(zone)
                expr = status.get("setpoint_expr")
                expected = entry.get("last_written_k")
                if expected is None:
                    if expr != entry.get("start_expr"):
                        raise Refusal(f"the setpoint was changed by someone else since the ramp "
                                      f"started (was {entry.get('start_expr')!r}, now {expr!r})")
                elif expr != repr(float(expected)):
                    raise Refusal(f"the setpoint is now {expr!r}, not the {_fmt(expected)} K the "
                                  f"ramp wrote — changed by someone else, or the command was "
                                  f"lost")
                e, _info = self._effective(zone, status, False)
                target = float(entry["target_k"])
                if abs(target - e) < 1e-6:
                    return "done", None
                now = self._clock()
                with self._stats_lock:
                    last = self._zone_ai.get(zone)
                anchor = max(last["ts"] if last else entry["started_ts"], entry["started_ts"])
                minutes = min(max((now - anchor) / 60.0, 0.0), RATE_CAP_MIN)
                step = float(entry["rate_k_per_min"]) * minutes
                zr = self.rails.zone(zone)
                if zr.get("max_rate_k_per_min") is not None:
                    step = min(step, float(zr["max_rate_k_per_min"]) * minutes)
                if zr.get("max_step_k") is not None:
                    step = min(step, float(zr["max_step_k"]))
                step = math.floor(step * 1000.0) / 1000.0
                if step <= 0:
                    return "busy", None
                if abs(target - e) <= step:
                    nxt = target
                else:
                    nxt = round(e + math.copysign(step, target - e), 3)
                self._check_setpoint(zone, nxt, False, status, il, now)
                self._publish(call, f"xsphere/commands/pid/{zone}/setpoint",
                              {"value_k": float(nxt)})
                with self._stats_lock:
                    self._zone_ai[zone] = {"value": float(nxt), "ts": self._clock()}
                self.ramps.update(zone, entry["id"], last_written_k=float(nxt),
                                  current_k=float(nxt), last_step_ts=now,
                                  steps=int(entry.get("steps", 0)) + 1)
                log.info("Ramp %s: %s → %s K (target %s K)", zone, _fmt(e), _fmt(nxt),
                         _fmt(target))
                return "stepped", nxt
            except Refusal as r:
                return "stop", r.text
        finally:
            self.write_lock.release()

    def set_heater_auto(self, args: Any) -> Tuple[bool, dict]:
        def body(call: _Call) -> None:
            z = call.args["zone"]
            self._require_armed(call)
            self._require_rails_ok()
            lo, hi = self._require_zone_rails(z)
            self._require_global()
            status = self._pid_status(z)
            mode = status.get("mode")
            if mode == "auto":
                call.detail = f"zone {z} already in auto — nothing sent"
                call.note = f"zone {z} is already in auto; nothing was sent"
                return
            if mode != "manual":
                raise Refusal(f"zone {z}: its PID mode reads {mode!r}, not manual — cannot tell "
                              f"what switching would do")
            il = self._interlock()
            expr = status.get("setpoint_expr")
            value = parse_numeric_expr(expr)
            if value is None:
                raise Refusal(f"zone {z}: set a setpoint first — its setpoint expression is "
                              f"{expr!r}, and auto mode needs a numeric setpoint inside the "
                              f"rails that the PLC re-asserts every poll")
            if not lo <= value <= hi:
                raise Refusal(f"zone {z}: its setpoint {_fmt(value, 'K')} is outside Lucas's "
                              f"rails {_fmt(lo)}–{_fmt(hi)} K; set one inside first")
            self._check_band(z, value, il)
            self._require_pv_fed(z, status)
            self._take_write_slot()
            topic = self._status_topic(z)
            seq0 = self._seq(topic)
            self._publish(call, f"xsphere/commands/pid/{z}/mode", {"mode": "auto"})
            call.detail = f"zone {z} manual → auto at SP {_fmt(value)} K"
            call.confirmed = self._wait_consecutive(
                [(topic, seq0, lambda p: p.get("mode") == "auto")])
            st = self._payload(topic) or {}
            keys = ("mode", "mode_cmd", "setpoint_k", "setpoint_expr", "pv_k", "output_pct")
            call.readback[z] = {k: st.get(k) for k in keys}
            if not call.confirmed:
                call.warnings.append(f"not confirmed: zone {z} mode did not read auto on two "
                                     f"consecutive messages within {self.confirm_timeout_s:g} s")
        return self._run("set_heater_auto", args, body)

    def set_pv_source(self, args: Any) -> Tuple[bool, dict]:
        def body(call: _Call) -> None:
            z, source = call.args["zone"], call.args["source"]
            self._require_armed(call)
            self._require_rails_ok()
            if z in self.rails.stale_zones:
                self._require_zone_rails(z)              # raises the stale-zone text
            allowed = self.rails.zone(z).get("pv_sources") or []
            if source not in allowed:
                raise Refusal(f"zone {z}: Lucas's rails do not let the AI make the {source} "
                              f"sensor its PV (pv_sources: {allowed or 'none'})")
            alias = self.zones.alias(z, source)
            sensor = self.zones.sensor_state(z, source)
            if alias is None or sensor is None:
                raise Refusal(f"zone {z} has no recognised {source} sensor"
                              + (" (the nozzle has no clamp)" if z == "nozzle" else ""))
            self._require_global()
            self._interlock()
            status = self._pid_status(z)
            value = self.bus.state_value(sensor)
            if not _is_num(value):
                raise Refusal(f"zone {z}: the {source} sensor ({sensor}, {alias}) has no fresh "
                              f"reading, so it cannot be the PID's process value")
            prev = status.get("pv_expr") if isinstance(status.get("pv_expr"), str) else ""
            prev = prev.strip()
            if prev == alias and _is_num(status.get("pv_expr_value_k")):
                call.detail = f"zone {z} PV already {alias} — nothing sent"
                call.note = f"zone {z} already uses {alias} ({source}) as its PV; nothing sent"
                return
            pv_k = status.get("pv_k")
            if prev:
                if not _is_num(pv_k) or abs(float(value) - float(pv_k)) > PV_SWITCH_MAX_JUMP_K:
                    raise Refusal(f"zone {z}: switching from {prev!r} to {alias} would move the "
                                  f"PID's PV from {_fmt(pv_k, 'K')} to {_fmt(value, 'K')} (more "
                                  f"than {PV_SWITCH_MAX_JUMP_K:g} K) — the PID would jump; ask "
                                  f"Lucas")
            self._take_write_slot()
            topic = self._status_topic(z)
            seq0 = self._seq(topic)
            self._publish(call, f"xsphere/commands/pid/{z}/pv_expr", {"expr": alias})
            call.detail = f"zone {z} PV source {prev or '(none)'} → {alias} ({source}, {sensor})"
            call.confirmed = self._wait_consecutive(
                [(topic, seq0, lambda p: (p.get("pv_expr") == alias
                                          and _is_num(p.get("pv_expr_value_k"))))])
            st = self._payload(topic) or {}
            keys = ("pv_expr", "pv_expr_value_k", "pv_k", "setpoint_k", "output_pct")
            call.readback[z] = {k: st.get(k) for k in keys}
            call.readback[z]["previous_pv_expr"] = prev
            if not call.confirmed:
                msg = (f"not confirmed: zone {z} did not report pv_expr {alias!r} with a value "
                       f"within {self.confirm_timeout_s:g} s")
                if prev:
                    # The service's own repair: put back what was there.
                    try:
                        self._publish(call, f"xsphere/commands/pid/{z}/pv_expr", {"expr": prev},
                                      check_arm=False)
                        msg += f"; reverted to the previous pv_expr {prev!r}"
                    except Refusal as r:
                        msg += f"; the revert to {prev!r} failed too: {r.text}"
                else:
                    msg += "; the previous pv_expr was empty, so nothing was reverted"
                raise Failure(msg)
        return self._run("set_pv_source", args, body)

    # ── heaters: NGE supply ──────────────────────────────────────────────

    def _nge_pace(self) -> None:
        """Wait (≤ 5 s) for the status publish that followed the previous NGE
        command: each command runs ~80 SCPI queries on slowcontrol's MQTT thread."""
        last = self._nge_last
        if not last:
            return
        remaining = self.nge_timeout_s - (time.monotonic() - last["mono"])
        if remaining > 0:
            self.bus.wait_for(lambda: self._seq(last["topic"]) > last["seq"], remaining)

    def set_heater_supply(self, args: Any) -> Tuple[bool, dict]:
        def body(call: _Call) -> None:
            a = call.args
            ch = a["channel"]
            key = str(ch)
            if not any(k in a for k in ("voltage_v", "current_a", "output")):
                raise Refusal("give at least one of voltage_v, current_a, output")
            self._require_armed(call)
            self._require_rails_ok()
            sr = self.rails.channel(key)
            vq = aq = None
            if "voltage_v" in a:
                if sr["voltage_min_v"] is None or sr["voltage_max_v"] is None:
                    raise Refusal(f"supply ch{ch}: Lucas has not set voltage rails "
                                  f"(voltage_min_v and voltage_max_v), so the voltage is not "
                                  f"writable")
                vq = round(float(a["voltage_v"]), 2)
                if not float(sr["voltage_min_v"]) <= vq <= float(sr["voltage_max_v"]):
                    raise Refusal(f"supply ch{ch}: {vq:g} V is outside Lucas's rails "
                                  f"{_fmt(sr['voltage_min_v'])}–{_fmt(sr['voltage_max_v'])} V")
            if "current_a" in a:
                if sr["current_min_a"] is None or sr["current_max_a"] is None:
                    raise Refusal(f"supply ch{ch}: Lucas has not set current rails "
                                  f"(current_min_a and current_max_a), so the current limit is "
                                  f"not writable")
                aq = round(float(a["current_a"]), 3)
                if not float(sr["current_min_a"]) <= aq <= float(sr["current_max_a"]):
                    raise Refusal(f"supply ch{ch}: {aq:g} A is outside Lucas's rails "
                                  f"{_fmt(sr['current_min_a'])}–{_fmt(sr['current_max_a'])} A")
            out = a.get("output")
            if out is True:
                if not sr["may_switch_on"]:
                    raise Refusal(f"supply ch{ch}: Lucas's rails do not let the AI switch it on "
                                  f"(may_switch_on is false)")
                if None in (sr["voltage_min_v"], sr["voltage_max_v"], sr["current_min_a"],
                            sr["current_max_a"]):
                    raise Refusal(f"supply ch{ch}: switching on needs voltage and current rails "
                                  f"set, to check what the channel would deliver")
            elif out is False and not sr["may_switch_off"]:
                raise Refusal(f"supply ch{ch}: Lucas's rails do not let the AI switch it off "
                              f"(may_switch_off is false) — a heater without power lets LN2 "
                              f"cool the cell")
            self._require_global()
            self._interlock()
            if self.bus.state_value("nge_connected") is not True:
                raise Refusal("the NGE103 supply is not connected (nge_connected is not fresh "
                              "and true)")
            topic = self.zones.channels[key]["status_topic"]
            age = self.bus.live_age(topic)
            st = self._payload(topic)
            if age is None or age > NGE_LIVE_S or st is None:
                raise Refusal(f"supply ch{ch}: no live status from the NGE driver (needs ≤ "
                              f"{NGE_LIVE_S:g} s)")
            steps = []
            if vq is not None:
                if _is_num(st.get("voltage_request_v")) and \
                        abs(float(st["voltage_request_v"]) - vq) < 5e-4:
                    call.warnings.append(f"ch{ch} voltage already {vq:g} V — not re-sent")
                else:
                    steps.append(("voltage", vq))
            if aq is not None:
                if _is_num(st.get("current_request_a")) and \
                        abs(float(st["current_request_a"]) - aq) < 5e-5:
                    call.warnings.append(f"ch{ch} current limit already {aq:g} A — not re-sent")
                else:
                    steps.append(("current", aq))
            if out is not None:
                if st.get("desired") == int(out) and st.get("output") == int(out):
                    call.warnings.append(f"ch{ch} output already {'ON' if out else 'OFF'} — "
                                         f"not re-sent")
                else:
                    steps.append(("output", out))
            if not steps:
                call.detail = f"supply ch{ch}: nothing to change"
                call.note = "everything asked for is already set; nothing was sent"
                call.confirmed = None
                self._nge_readback(call, ch, topic)
                return
            now = self._clock()
            with self._stats_lock:
                while self._nge_times and now - self._nge_times[0] >= 60.0:
                    self._nge_times.popleft()
                used = len(self._nge_times)
            if used + len(steps) > NGE_MAX_PER_MIN:
                raise Refusal(f"NGE command budget: at most {NGE_MAX_PER_MIN} supply commands a "
                              f"minute ({used} used, this call needs {len(steps)}) — each one "
                              f"blocks slowcontrol's MQTT thread for seconds")
            self._take_write_slot()

            done = []
            for i, (what, value) in enumerate(steps):
                if what == "output" and value is True:
                    cur = self._payload(topic) or {}
                    vr, ir = cur.get("voltage_request_v"), cur.get("current_request_a")
                    if not (_is_num(vr) and _is_num(ir)
                            and float(sr["voltage_min_v"]) - 1e-6 <= float(vr)
                            <= float(sr["voltage_max_v"]) + 1e-6
                            and float(sr["current_min_a"]) - 1e-6 <= float(ir)
                            <= float(sr["current_max_a"]) + 1e-6):
                        raise Refusal(f"supply ch{ch}: not switched on — its held targets "
                                      f"({_fmt(vr, 'V')}, {_fmt(ir, 'A')}) are not inside "
                                      f"Lucas's rails; set voltage_v / current_a inside them in "
                                      f"the same call")
                    zone = self.zones.channel_zone(ch)
                    pid = self._payload(self._status_topic(zone)) if zone in ZONES else None
                    if pid and _is_num(pid.get("output_pct")) and \
                            float(pid["output_pct"]) >= SUPPLY_WARN_OUTPUT_PCT:
                        call.warnings.append(f"PID {zone} output is {_fmt(pid['output_pct'])} % — "
                                             f"switching ch{ch} on gives full power until its PV "
                                             f"reaches the setpoint")
                ok, why = self._nge_step(call, ch, topic, what, value)
                if ok:
                    done.append(what)
                    continue
                rest = [w for w, _v in steps[i + 1:]]
                if why == "rejected":
                    raise Failure(f"supply ch{ch}: the NGE driver rejected the {what} command "
                                  f"({call.readback.get('event')})"
                                  + (f"; not sent: {', '.join(rest)}" if rest else ""))
                if rest:
                    raise Failure(f"supply ch{ch}: the {what} change was not confirmed within "
                                  f"{self.nge_timeout_s:g} s, so {', '.join(rest)} was not sent")
                call.confirmed = False
                call.warnings.append(f"not confirmed: ch{ch} {what} did not read back within "
                                     f"{self.nge_timeout_s:g} s")
                break
            else:
                call.confirmed = True
            parts = []
            for what, value in steps:
                parts.append(f"{what} {value:g} {'V' if what == 'voltage' else 'A'}"
                             if what != "output" else f"output {'ON' if value else 'OFF'}")
            call.detail = f"supply ch{ch}: " + ", ".join(parts)
            self._nge_readback(call, ch, topic)
        return self._run("set_heater_supply", args, body)

    def _nge_step(self, call: _Call, ch: int, topic: str, what: str, value: Any
                  ) -> Tuple[bool, Optional[str]]:
        self._nge_pace()
        nge = self._payload(NGE_TOPIC) or {}
        ev0 = (nge.get("event"), nge.get("event_utc"))
        seq0 = self._seq(topic)
        if what == "voltage":
            payload, field, want = {"value_v": float(value)}, "voltage_request_v", float(value)
        elif what == "current":
            payload, field, want = {"value_a": float(value)}, "current_request_a", float(value)
        else:
            payload, field, want = {"state": int(bool(value))}, "desired", int(bool(value))
        self._publish(call, f"xsphere/commands/nge/{ch}/{what}", payload)
        with self._stats_lock:
            self._nge_times.append(self._clock())
        self._nge_last = {"topic": topic, "seq": seq0, "mono": time.monotonic()}
        state = {"rejected": False}

        def rejected() -> bool:
            p = self._payload(NGE_TOPIC) or {}
            ev = p.get("event")
            if (ev, p.get("event_utc")) != ev0 and isinstance(ev, str) and f"ch{ch}" in ev \
                    and ("rejected" in ev or "ignored" in ev):
                call.readback["event"] = ev
                return True
            return False

        def accepted() -> bool:
            r = self.bus.raw(topic)
            if r is None or r["seq"] <= seq0 or not isinstance(r["payload"], dict):
                return False
            got = r["payload"].get(field)
            if what == "output":
                return got == want
            return _is_num(got) and abs(float(got) - want) < (5e-4 if what == "voltage" else 5e-5)

        def done() -> bool:
            if rejected():
                state["rejected"] = True
                return True
            return accepted()

        self.bus.wait_for(done, self.nge_timeout_s)
        if state["rejected"]:
            return False, "rejected"
        return (True, None) if accepted() else (False, "timeout")

    def _nge_readback(self, call: _Call, ch: int, topic: str) -> None:
        st = self._payload(topic) or {}
        call.readback[f"ch{ch}"] = {k: st.get(k) for k in (
            "output", "desired", "voltage_set_v", "voltage_request_v", "current_limit_a",
            "current_request_a", "voltage_v", "current_a", "power_w", "mode", "trip")}
        lim, req = st.get("current_limit_a"), st.get("current_request_a")
        if _is_num(lim) and _is_num(req) and float(lim) < float(req) - 1e-3:
            call.warnings.append(f"ch{ch} current limit reads {_fmt(lim, 'A')} for a request of "
                                 f"{_fmt(req, 'A')}: the supply caps it at 33.6 W / V — normal, "
                                 f"not a failure")

    # ── LN2 ───────────────────────────────────────────────────────────────

    def _valve_status(self, vessel: str) -> Tuple[str, dict]:
        topic = self.zones.vessels[vessel]["status_topic"]
        st = self.bus.live_payload(topic, VALVE_LIVE_S)
        if not isinstance(st, dict) or not _is_num(st.get("state")):
            raise Refusal(f"LN2 {vessel}: no live valve status from the PLC (needs ≤ "
                          f"{VALVE_LIVE_S:g} s)")
        return topic, st

    def _require_level_fresh(self, vessel: str) -> None:
        v = self.zones.vessels[vessel]
        level = v.get("level_state")
        if level is None or not _is_num(self.bus.state_value(level)):
            raise Refusal(f"LN2 {vessel}: its level reading ({level}) is not fresh, so the PLC "
                          f"could not tell when it is full")
        esp = v.get("esp32_state")
        if esp is not None and self.bus.state_value(esp) is not True:
            raise Refusal(f"LN2 {vessel}: the GHS ESP32 heartbeat ({esp}) is not fresh — the PLC "
                          f"keeps using its last level value when that feed stops")

    def _require_ln2_interval(self, vessel: str) -> None:
        now = self._clock()
        last = self._ln2_last.get(vessel)
        if last is not None and now - last < LN2_MIN_INTERVAL_S:
            raise Refusal(f"LN2 {vessel}: the valve was commanded {now - last:.0f} s ago; wait "
                          f"{LN2_MIN_INTERVAL_S - (now - last):.0f} s more (at most one command "
                          f"per {LN2_MIN_INTERVAL_S:g} s)")

    def set_ln2_valve(self, args: Any) -> Tuple[bool, dict]:
        def body(call: _Call) -> None:
            vessel, want_open = call.args["vessel"], call.args["open"]
            self._require_armed(call)
            self._require_rails_ok()
            vr = self.rails.vessel(vessel)
            if not vr["may_operate"]:
                raise Refusal(f"LN2 {vessel}: Lucas's rails do not let the AI operate this valve "
                              f"(may_operate is false)")
            if want_open and vr["max_open_minutes"] is None:
                raise Refusal(f"LN2 {vessel}: Lucas has not set max_open_minutes for it, so the "
                              f"AI may not open it")
            self._require_global()
            topic, st = self._valve_status(vessel)
            prefix = self.zones.vessels[vessel]["command_prefix"]
            if want_open:
                if st.get("auto_close") != 1 or isinstance(st.get("auto_close"), bool):
                    raise Refusal(f"LN2 {vessel}: auto-close is off — the PLC's fill timeout "
                                  f"only runs with auto-close armed; turn it on first "
                                  f"(set_ln2_auto auto_close true)")
                self._require_level_fresh(vessel)
                if st.get("state") != 0 or st.get("desired") != 0:
                    raise Refusal(f"LN2 {vessel}: the valve is not closed (state "
                                  f"{st.get('state')}, desired {st.get('desired')})")
                self._require_ln2_interval(vessel)
                self._take_write_slot()
                minutes = float(vr["max_open_minutes"])
                timer = self.timers.set(vessel, minutes) if self.timers is not None else None
                seq0 = self._seq(topic)
                try:
                    self._ln2_last[vessel] = self._clock()
                    self._publish(call, f"{prefix}/state", {"state": 1})
                except Refusal:
                    if self.timers is not None:
                        self.timers.clear(vessel)
                    raise
                call.detail = f"LN2 {vessel} opened (the service closes it after {minutes:g} min)"
                seen = {"open": False}

                def opened(p: dict) -> bool:
                    if p.get("state") == 1 or p.get("desired") == 1:
                        seen["open"] = True
                    return p.get("state") == 1

                def closed_by_ladder() -> bool:
                    r = self.bus.raw(topic)
                    if r is None or r["seq"] < seq0 + 2 or not seen["open"]:
                        return False
                    p = r["payload"]
                    return isinstance(p, dict) and p.get("desired") == 0 and p.get("state") == 0

                checks = [(topic, seq0, opened)]
                ok = self.bus.wait_for(lambda: self._wait_consecutive(checks, timeout=0)
                                       or closed_by_ladder(), self.confirm_timeout_s)
                call.confirmed = bool(ok) and not closed_by_ladder()
                if timer:
                    call.readback["auto_close_deadline_utc"] = timer["deadline_utc"]
                if not call.confirmed:
                    last = self._payload(topic) or {}
                    if last.get("desired") == 0:
                        call.warnings.append(f"not confirmed: LN2 {vessel} reads desired 0 — "
                                             f"closed by the PLC ladder (level/timer)?")
                    else:
                        call.warnings.append(f"not confirmed: LN2 {vessel} did not read open on "
                                             f"two consecutive messages within "
                                             f"{self.confirm_timeout_s:g} s")
            else:
                if st.get("state") == 0 and st.get("desired") == 0 and st.get("auto_open") == 0:
                    if self.timers is not None:
                        self.timers.clear(vessel)
                    call.detail = f"LN2 {vessel} already closed — nothing sent"
                    call.note = f"LN2 {vessel} is already closed with auto-open off; nothing sent"
                    self._valve_readback(call, vessel, topic)
                    return
                self._require_ln2_interval(vessel)
                self._take_write_slot()
                seq0 = self._seq(topic)
                self._ln2_last[vessel] = self._clock()
                if st.get("auto_open") != 0:
                    # auto-open would re-open it on the next scan: off first.
                    self._publish(call, f"{prefix}/auto_open", {"enabled": False})
                self._publish(call, f"{prefix}/state", {"state": 0})
                call.detail = f"LN2 {vessel} closed" + (" (auto-open switched off first)"
                                                        if len(call.published) > 1 else "")
                call.confirmed = self._wait_consecutive(
                    [(topic, seq0, lambda p: p.get("state") == 0 and p.get("desired") == 0)])
                if call.confirmed and self.timers is not None:
                    self.timers.clear(vessel)
                if not call.confirmed:
                    call.warnings.append(f"not confirmed: LN2 {vessel} did not read closed on two "
                                         f"consecutive messages within "
                                         f"{self.confirm_timeout_s:g} s")
            self._valve_readback(call, vessel, topic)
        return self._run("set_ln2_valve", args, body)

    def _valve_readback(self, call: _Call, vessel: str, topic: str) -> None:
        st = self._payload(topic) or {}
        keys = ("state", "desired", "auto_close", "auto_open")
        call.readback[vessel] = {k: st.get(k) for k in keys}

    def set_ln2_auto(self, args: Any) -> Tuple[bool, dict]:
        def body(call: _Call) -> None:
            a = call.args
            vessel = a["vessel"]
            want = {k: a[k] for k in ("auto_open", "auto_close") if k in a}
            if not want:
                raise Refusal("give auto_open and/or auto_close")
            self._require_armed(call)
            self._require_rails_ok()
            vr = self.rails.vessel(vessel)
            if not vr["may_operate"]:
                raise Refusal(f"LN2 {vessel}: Lucas's rails do not let the AI operate this valve "
                              f"(may_operate is false)")
            if want.get("auto_open") is True and not vr["may_set_auto_open"]:
                raise Refusal(f"LN2 {vessel}: Lucas's rails do not let the AI turn auto-open on "
                              f"(may_set_auto_open is false)")
            if want.get("auto_close") is False and not vr["may_disarm_auto_close"]:
                raise Refusal(f"LN2 {vessel}: Lucas's rails do not let the AI turn auto-close "
                              f"off (may_disarm_auto_close is false)")
            if want.get("auto_open") is True and want.get("auto_close") is False:
                raise Refusal("auto-open only works with auto-close armed; this asks for "
                              "auto-open on and auto-close off")
            self._require_global()
            topic, st = self._valve_status(vessel)
            if want.get("auto_open") is True:
                if st.get("auto_close") != 1 and want.get("auto_close") is not True:
                    raise Refusal(f"LN2 {vessel}: auto-close is off — auto-open only works with "
                                  f"auto-close armed (include auto_close: true)")
                self._require_level_fresh(vessel)
            is_closed = st.get("state") == 0 and st.get("desired") == 0
            if want.get("auto_close") is False and not is_closed:
                raise Refusal(f"LN2 {vessel}: auto-close may only be turned off while the valve "
                              f"is closed — the PLC's fill timeout only runs with it")
            # Safe order: arm auto-close, drop auto-open, then raise auto-open,
            # and only then drop auto-close.
            order = []
            if want.get("auto_close") is True:
                order.append(("auto_close", True))
            if want.get("auto_open") is False:
                order.append(("auto_open", False))
            if want.get("auto_open") is True:
                order.append(("auto_open", True))
            if want.get("auto_close") is False:
                order.append(("auto_close", False))
            steps = [(k, v) for k, v in order if st.get(k) != int(v)]
            for k, v in order:
                if (k, v) not in steps:
                    call.warnings.append(f"{k} already {'on' if v else 'off'} — not re-sent")
            if not steps:
                call.detail = f"LN2 {vessel}: flags already as asked — nothing sent"
                call.note = "the flags are already as asked; nothing was sent"
                self._valve_readback(call, vessel, topic)
                return
            self._require_ln2_interval(vessel)
            self._take_write_slot()
            prefix = self.zones.vessels[vessel]["command_prefix"]
            self._ln2_last[vessel] = self._clock()
            for i, (k, v) in enumerate(steps):
                seq0 = self._seq(topic)
                self._publish(call, f"{prefix}/{k}", {"enabled": bool(v)})
                ok = self._wait_consecutive([(topic, seq0,
                                              lambda p, k=k, v=v: p.get(k) == int(v))])
                if not ok:
                    rest = [f"{kk} {'on' if vv else 'off'}" for kk, vv in steps[i + 1:]]
                    if rest:
                        raise Failure(f"LN2 {vessel}: {k} {'on' if v else 'off'} was not "
                                      f"confirmed within {self.confirm_timeout_s:g} s, so "
                                      f"{', '.join(rest)} was not sent")
                    call.confirmed = False
                    call.warnings.append(f"not confirmed: LN2 {vessel} {k} did not read "
                                         f"{int(v)} on two consecutive messages")
                    break
            else:
                call.confirmed = True
            done = [f"{k} {'on' if v else 'off'}" for k, v in steps]
            call.detail = f"LN2 {vessel}: " + ", ".join(done)
            self._valve_readback(call, vessel, topic)
        return self._run("set_ln2_auto", args, body)

    # ── pneumatic ─────────────────────────────────────────────────────────

    def set_pneumatic_valve(self, args: Any) -> Tuple[bool, dict]:
        def body(call: _Call) -> None:
            valve, want_open = call.args["valve"], call.args["open"]
            want = int(want_open)
            self._require_armed(call)
            self._require_rails_ok()
            vr = self.rails.valve(valve)
            if want_open and not vr["may_open"]:
                raise Refusal(f"pneumatic {valve}: Lucas's rails do not let the AI open it "
                              f"(may_open is false)")
            if not want_open and not vr["may_close"]:
                raise Refusal(f"pneumatic {valve}: Lucas's rails do not let the AI close it "
                              f"(may_close is false)")
            self._require_global()
            gate = self.bus.live_payload(PNEUMATIC_GATE_TOPIC, GATE_LIVE_S)
            if not isinstance(gate, dict) or gate.get("enabled") != 1 \
                    or isinstance(gate.get("enabled"), bool):
                raise Refusal("the pneumatic valve gate on the Control page is disabled (or the "
                              "PLC driver is not reporting it) — Lucas must enable it there as "
                              "well; it is a second, separate lock")
            topic = self.zones.valves[valve]["status_topic"]
            st = self.bus.live_payload(topic, GATE_LIVE_S)
            if not isinstance(st, dict):
                raise Refusal(f"pneumatic {valve}: no live valve status from the PLC")
            if want_open:
                for pair in self.rails.pneumatic()["forbid_open_together"]:
                    if valve not in pair:
                        continue
                    partner = pair[1] if pair[0] == valve else pair[0]
                    pst = self.bus.live_payload(self.zones.valves[partner]["status_topic"],
                                                GATE_LIVE_S)
                    if not isinstance(pst, dict) or "state" not in pst:
                        raise Refusal(f"pneumatic {valve}: Lucas's rails forbid {valve} and "
                                      f"{partner} open together, and {partner}'s state is not "
                                      f"known (no live status)")
                    if pst.get("state") == 1 or pst.get("desired") == 1:
                        raise Refusal(f"pneumatic {valve}: {partner} is open, and Lucas's rails "
                                      f"forbid {valve} and {partner} open at the same time")
            interval = float(self.rails.pneumatic()["min_interval_s"])
            now = self._clock()
            last = self._pn_last.get(valve)
            if last is not None and now - last < interval:
                raise Refusal(f"pneumatic {valve}: commanded {now - last:.0f} s ago; wait "
                              f"{interval - (now - last):.0f} s more (min_interval_s "
                              f"{interval:g})")
            if st.get("state") == want and st.get("desired") == want:
                call.detail = f"pneumatic {valve} already {'open' if want else 'closed'}"
                call.note = f"{valve} is already {'open' if want else 'closed'}; nothing sent"
                call.readback[valve] = {k: st.get(k) for k in ("state", "desired", "relay")}
                return
            self._take_write_slot()
            lr0 = copy.deepcopy(gate.get("last_refused"))
            seq0 = self._seq(topic)
            t_pub = self._clock()
            self._pn_last[valve] = t_pub
            self._publish(call, f"xsphere/commands/pneumatic/{valve}/state", {"state": want})
            word = "open" if want else "closed"
            call.detail = f"pneumatic {valve} → {word}"

            def plc_refused() -> bool:
                g = self._payload(PNEUMATIC_GATE_TOPIC) or {}
                lr = g.get("last_refused")
                if not isinstance(lr, dict) or lr == lr0:
                    return False
                if lr.get("valve") != valve or lr.get("state") != want:
                    return False
                ts = _parse_utc(lr.get("utc"))
                return ts is None or ts >= math.floor(t_pub) - 1

            checks = [(topic, seq0, lambda p: "state" in p and p.get("state") == want)]
            self.bus.wait_for(lambda: plc_refused() or self._wait_consecutive(checks, timeout=0),
                              self.confirm_timeout_s)
            if plc_refused():
                call.confirmed = False
                raise Failure(f"pneumatic {valve}: the PLC refused the command — its pneumatic "
                              f"valve gate was disabled when it arrived")
            call.confirmed = self._wait_consecutive(checks, timeout=0)
            st2 = self._payload(topic) or {}
            call.readback[valve] = {k: st2.get(k) for k in ("state", "desired", "relay")}
            if not call.confirmed:
                call.warnings.append(f"not confirmed: {valve} did not read {word} on two "
                                     f"consecutive messages within {self.confirm_timeout_s:g} s"
                                     + ("" if "state" in st2 else " (the PLC did not report the "
                                        "relay state)"))
        return self._run("set_pneumatic_valve", args, body)

    # ── notes ─────────────────────────────────────────────────────────────

    def add_note(self, args: Any) -> Tuple[bool, dict]:
        def body(call: _Call) -> None:
            call.detail = call.args["text"].strip()
            call.note = "noted in the AI logbook (audit), shown on the Control page card"
        return self._run("add_note", args, body, lock=False)


def _safe(cond: Callable[[dict], bool], payload: dict) -> bool:
    try:
        return bool(cond(payload))
    except Exception:
        return False
