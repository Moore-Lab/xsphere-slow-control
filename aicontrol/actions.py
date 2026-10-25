"""
The AI's write tools: every path from a tool call to a command topic.

Every write goes through the same pipeline, in this order (DESIGN §8):

0. **A write token.**  The caller must have sent a known client's token
   (auth.py); without one every write tool — notes and ramp stops included —
   is refused.
1. **Strict arguments.**  Exact JSON types against the tool's schema: a
   number is an int or float, never a bool, finite and not huge; a bool is a
   bool; enums are checked type-first (``True in (1, 2, 3)`` is true in
   Python); unknown keys are refused; strings are plain text (no control
   characters, unpaired surrogates or text-direction marks; only a note may
   span lines); every write carries a ``reason``.  The
   drivers coerce with ``int()`` / ``bool()`` / ``float()`` — ``"false"`` arms
   an LN2 auto flag, NaN drives a PID output to 100 % — so nothing loose may
   reach them.
2. **One write at a time.**  ``write_lock`` is taken without blocking; a
   second write is refused, never queued.  The heater ramp thread uses the
   same lock.  The lock is never needed by the MQTT thread, so a disarm is
   never stuck behind a write waiting for its confirmation.
3. **Armed** for this client (Lucas's arm of the tool's group is a lease for
   one named client, arms.py).
4. **Rails** for the specific item (an invalid rails file refuses everything).
5. **Preconditions**: broker connected, snapshot ≤ 5 s old, slowcontrol's
   heartbeat fresh, Sequencer not running, no enabled tracker (both would
   overwrite an AI write a second later while the AI is told "confirmed"),
   then the tool's own checks, the pressure guard (no warming, no closing the
   xenon's buffer, no pump opening unless the pressures allow it) and the PV
   source plausibility check (guard.py).
6. **Rate**: ≤ 20 write calls a minute overall, the lease's write budget,
   per-valve minimum intervals, ≤ 6 NGE commands a minute paced on the
   supply's own status; ≤ 6 notes a minute.  Calls that send nothing
   (refusals, no-ops, dry runs) beyond 30 a minute are counted, and summed up
   in one audit entry, instead of each being audited.
7. **Publish** through :meth:`Bus.publish_command` (allowlisted topics only),
   re-checking the arm immediately before every single publish.
8. **Confirm** from the status topics (two consecutive messages where it
   matters); "not confirmed" is reported, and a PV source change that does not
   confirm is reverted.
9. **Audit** (with the client and the values the call changed, ``previous``)
   and count.

``dry_run: true`` on any write tool runs steps 0–6 without using anything up
and returns ``would_publish`` instead of publishing (audited as kind
``dry_run``).

Refusals are results (``ok: false`` + a sentence saying what is missing), not
exceptions: the model has to read why.  Every MQTT-sourced string a refusal or
readback quotes is passed through :func:`untrusted.untrusted` («…»).  The
service's own safety actions (the LN2 auto-close, reverting a PV source, the
pressure guard's trip) are the only publishes made without an arm.
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

from .arms import LOG_TEXT_MAX, PV_INTERLOCK_TOPIC, log_text, utc_iso
from .untrusted import clean_text, deep_untrusted, untrusted
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
#: add_note has its own small budget, so notes cannot push the real actions
#: out of the card's and the status page's short lists (or rotate them off disk).
MAX_NOTES_PER_MIN = 6
#: Calls that sent nothing (refused, invalid, or a no-op such as "already
#: closed") audited one by one per minute; beyond this they are counted and
#: summed up in one audit entry (they move no hardware).
MAX_AUDITED_REFUSALS_PER_MIN = 30
#: How long an AI open of a gas valve that was never confirmed still counts as
#: "maybe on its way" for forbid_open_together (commands queue behind slow
#: NGE work on slowcontrol's single MQTT thread).
PN_PENDING_OPEN_S = 120.0
#: String arguments that may span lines (everything else must be one line).
MULTILINE_ARGS: Dict[str, Tuple[str, ...]] = {"add_note": ("text",)}

# Text-direction controls (bidi overrides / isolates): they make a log or
# audit line read differently from what it contains.
_BIDI_CONTROLS = frozenset(range(0x202A, 0x202F)) | frozenset(range(0x2066, 0x206A))

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

_DRY_RUN = {"type": "boolean",
            "description": "true = rehearse only: run every check (token, arm, rails, pressure "
                           "guard, preconditions) and return would_publish, publishing nothing "
                           "and using none of the arm's write budget. Audited as a dry run."}
for _schema in WRITE_SCHEMAS.values():
    _schema["properties"]["dry_run"] = dict(_DRY_RUN)

NO_TOKEN_TEXT = ("this MCP client has no valid write token (Authorization: Bearer …), so write "
                 "tools are refused; reads work without one")

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


def _text_problem(text: str, multiline: bool) -> Optional[str]:
    """What makes ``text`` unfit for the audit and the service log, or None.

    Refused: unpaired surrogates (``"\\ud800"`` is valid JSON, but cannot be
    written as UTF-8 — the audit file write would fail after the command had
    gone out), control characters (a newline in a reason forges a journal
    line), the Unicode line / paragraph separators and text-direction
    controls.  ``multiline`` text may contain newlines and tabs.
    """
    for i, ch in enumerate(text):
        o = ord(ch)
        if 0xD800 <= o <= 0xDFFF:
            return f"an unpaired surrogate (U+{o:04X}) at position {i}"
        if multiline and ch in "\n\t":
            continue
        if o < 0x20 or 0x7F <= o <= 0x9F:
            return (f"a control character (U+{o:04X}) at position {i}"
                    + ("" if multiline else " — keep it on one line"))
        if o in (0x2028, 0x2029) or o in _BIDI_CONTROLS:
            return f"a line-separator or text-direction character (U+{o:04X}) at position {i}"
    return None


def validate_args(schema: dict, value: Any, path: str = "", *,
                  multiline: Tuple[str, ...] = ()) -> Any:
    """Check ``value`` against the JSON-schema subset used by the tools.

    Returns a deep copy; raises :class:`ArgError` naming the first problem.
    Stricter than JSON schema where the drivers need it: numbers must be
    finite and within ±1e6, a string with ``minLength`` must have that many
    non-blank characters, and strings are plain text (:func:`_text_problem`;
    only the paths in ``multiline`` may hold newlines).
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
        return {k: validate_args(props[k], v, f"{path}.{k}" if path else k, multiline=multiline)
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
        problem = _text_problem(value, path in multiline)
        if problem:
            raise ArgError(f"{where}: contains {problem}; send plain text")
        return value
    if t == "array":
        if not isinstance(value, list):
            raise ArgError(f"{where}: must be a list (got {_show(value)})")
        if len(value) < schema.get("minItems", 0):
            raise ArgError(f"{where}: needs at least {schema['minItems']} item(s)")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            raise ArgError(f"{where}: at most {schema['maxItems']} items (got {len(value)})")
        items = schema.get("items", {})
        out = [validate_args(items, v, f"{where}[{i}]", multiline=multiline)
               for i, v in enumerate(value)]
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

    def __init__(self, tool: str, args: dict, client: Optional[str] = None):
        self.tool = tool
        self.args = args
        self.client = client                  # the AI client (None: no valid token)
        self.group = GROUP_OF.get(tool)
        self.reason = args.get("reason") if isinstance(args, dict) else None
        self.dry_run = isinstance(args, dict) and args.get("dry_run") is True
        self.published: List[dict] = []
        self.would_publish: List[dict] = []   # a dry run's commands
        self.previous: Dict[str, Any] = {}    # what the call changes, as it was before
        self.warnings: List[str] = []
        self.readback: Dict[str, Any] = {}
        self.confirmed: Optional[bool] = None
        self.note: Optional[str] = None
        self.detail = ""
        self.ramp_id: Optional[int] = None    # set for a ramp step
        self.noop = False                     # nothing to do: nothing sent, nothing changed
        self.budget_taken = False

    def args_for_audit(self) -> dict:
        return {k: v for k, v in self.args.items() if k not in ("reason", "dry_run")}

    @property
    def who(self) -> str:
        return self.client or "anonymous"


# ──────────────────────────────────────────────────────────────────────────
# Actions
# ──────────────────────────────────────────────────────────────────────────

class Actions:
    """The write tools.  Each public tool method takes the raw arguments dict
    and the caller's client name (None: no valid write token) and returns
    ``(is_error, result)``."""

    def __init__(self, bus, zones, rails, arms, audit, *, ramps=None, timers=None,
                 guard=None, pvcheck=None,
                 clock: Callable[[], float] = time.time, confirm_timeout_s: float = 5.0,
                 nge_timeout_s: float = 5.0, on_change: Optional[Callable[[], None]] = None):
        self.bus = bus
        self.zones = zones
        self.rails = rails
        self.arms = arms
        self.audit = audit
        self.ramps = ramps
        self.timers = timers
        self.guard = guard            # guard.PressureGuard (None: every guarded write refused)
        self.pvcheck = pvcheck        # guard.PvCheck
        self._clock = clock
        self.confirm_timeout_s = float(confirm_timeout_s)
        self.nge_timeout_s = float(nge_timeout_s)
        self._on_change = on_change
        self.write_lock = threading.Lock()
        self.started_ts = clock()

        self._stats_lock = threading.Lock()
        self.counters = {"actions": 0, "refused": 0, "notes": 0, "dry_runs": 0}
        self.last_action: Optional[dict] = None
        self.last_refused: Optional[dict] = None
        self.last_note: Optional[dict] = None
        self._write_times: Deque[float] = collections.deque()
        self._note_times: Deque[float] = collections.deque()
        self._quiet_times: Deque[float] = collections.deque()      # calls that sent nothing
        self._unaudited: Dict[str, Any] = {"n": 0, "since": None, "tools": collections.Counter()}
        self._nge_times: Deque[float] = collections.deque()
        self._nge_last: Optional[dict] = None
        self._zone_ai: Dict[str, dict] = {}         # zone → {"value", "ts"} of the last AI setpoint
        self._pv_ai: Dict[str, str] = {}            # zone → the PV alias the AI chose
        self._ln2_last: Dict[str, float] = {}
        self._pn_last: Dict[str, float] = {}
        # valve → the AI's last gas-valve command {"want", "ts", "seq", "pending"}
        self._pn_cmd: Dict[str, dict] = {}
        if ramps is not None:
            ramps.set_stepper(self.ramp_step)

    def set_on_change(self, fn: Optional[Callable[[], None]]) -> None:
        self._on_change = fn

    def handlers(self) -> Dict[str, Callable[..., Tuple[bool, dict]]]:
        """``{tool name: method(args, client)}`` for the registry."""
        return {name: getattr(self, name) for name in WRITE_SCHEMAS}

    def zone_ai_setpoints(self) -> Dict[str, dict]:
        with self._stats_lock:
            return copy.deepcopy(self._zone_ai)

    def pv_ai_sources(self) -> Dict[str, str]:
        with self._stats_lock:
            return dict(self._pv_ai)

    def stats(self) -> dict:
        """Counters for the status: ``actions`` counts tool calls that did
        something (notes and dry runs are counted apart, so neither becomes
        the "last action" a person sees)."""
        with self._stats_lock:
            return {"actions": self.counters["actions"], "refused": self.counters["refused"],
                    "notes": self.counters["notes"], "dry_runs": self.counters["dry_runs"],
                    "last_action": copy.deepcopy(self.last_action),
                    "last_refused": copy.deepcopy(self.last_refused),
                    "last_note": copy.deepcopy(self.last_note)}

    # ── pipeline plumbing ─────────────────────────────────────────────────

    def _run(self, tool: str, args: Any, body: Callable[[_Call], None], *,
             lock: bool = True, client: Optional[str] = None) -> Tuple[bool, dict]:
        try:
            a = validate_args(WRITE_SCHEMAS[tool], args, multiline=MULTILINE_ARGS.get(tool, ()))
        except ArgError as e:
            return self._invalid(tool, args, str(e), client)
        call = _Call(tool, a, client)
        if client is None:
            return self._refused(call, NO_TOKEN_TEXT)
        # A dry run publishes nothing and waits for nothing: it never needs
        # (nor blocks) the write lock.
        lock = lock and not call.dry_run
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
            if call.dry_run:
                return self._dry_result(call)
            return self._succeeded(call)
        finally:
            if lock:
                self.write_lock.release()
            if call.budget_taken:
                self.arms.end_if_spent(call.group)

    def _invalid(self, tool: str, args: Any, msg: str,
                 client: Optional[str] = None) -> Tuple[bool, dict]:
        text = f"invalid arguments: {msg}"
        try:
            shown = args if len(json.dumps(args, default=str)) <= 2000 else "(too large)"
        except (TypeError, ValueError, RecursionError):
            shown = "(not JSON)"
        self._record_refused(tool, text, args=shown, reason=None, published=[],
                             client=client or "anonymous")
        return True, {"ok": False, "tool": tool, "error": text, "invalid_arguments": True}

    def _refused(self, call: _Call, text: str, *, failed: bool = False,
                 internal: bool = False) -> Tuple[bool, dict]:
        res: Dict[str, Any] = {"ok": False, "tool": call.tool}
        if call.dry_run:
            res["dry_run"] = True
        res["error" if (failed or internal) else "refused"] = text
        if internal:
            res["internal"] = True
        res.update({"published": call.published,
                    "confirmed": call.confirmed if call.published else None,
                    "readback": call.readback,
                    "warnings": call.warnings})
        if call.previous and call.published:
            res["previous"] = deep_untrusted(call.previous, 200)
        if call.note:
            res["note"] = call.note
        detail = text if not call.published else (
            f"{text} — after publishing {len(call.published)} command(s)")
        if call.dry_run:
            detail = f"dry run refused: {detail}"
        self._record_refused(call.tool, detail, args=call.args_for_audit(), reason=call.reason,
                             published=call.published, client=call.who,
                             previous=call.previous if call.published else None,
                             kind="dry_run" if call.dry_run else "refused")
        return True, res

    def _quiet_slot_locked(self, now: float, tool: str) -> bool:
        """For a call that sent nothing (refused, invalid, a no-op or a dry
        run): True while it may still be audited one by one this minute; else
        it is only counted (a looping caller would otherwise flood the audit,
        the card and the journal with calls that moved nothing).  Hold
        _stats_lock."""
        while self._quiet_times and now - self._quiet_times[0] >= 60.0:
            self._quiet_times.popleft()
        ok = len(self._quiet_times) < MAX_AUDITED_REFUSALS_PER_MIN
        self._quiet_times.append(now)
        if not ok:
            u = self._unaudited
            u["n"] += 1
            u["tools"][tool] += 1
            if u["since"] is None:
                u["since"] = now
        return ok

    def _record_refused(self, tool: str, detail: str, *, args: Any, reason: Optional[str],
                        published: list, client: str = "anonymous",
                        previous: Optional[dict] = None, kind: str = "refused") -> None:
        now = self._clock()
        utc = utc_iso(now)
        with self._stats_lock:
            if kind == "dry_run":
                self.counters["dry_runs"] += 1
            else:
                self.counters["refused"] += 1
                self.last_refused = {"utc": utc, "tool": tool, "detail": detail,
                                     "client": client}
            # One that published something is always audited.
            audit_it = bool(published) or self._quiet_slot_locked(now, tool)
        if audit_it:
            log.warning("AI %s (%s) %s: %s", tool, client,
                        "dry run refused" if kind == "dry_run" else "refused",
                        log_text(detail, 500))
            self.flush_unaudited(force=True)
            self._audit(kind, detail, tool=tool, args=args, reason=reason,
                        published=published or None, client=client, previous=previous or None)
            self._changed()
        else:
            # Not even a status publish: the 2 s status loop shows the count.
            log.debug("AI %s refused (not audited, over %d a minute): %s", tool,
                      MAX_AUDITED_REFUSALS_PER_MIN, log_text(detail))

    def flush_unaudited(self, force: bool = False) -> None:
        """Write one audit entry summing up the calls that were only counted
        (call it periodically; ``force`` writes it whatever its age)."""
        now = self._clock()
        with self._stats_lock:
            u = self._unaudited
            if not u["n"] or (not force and now - u["since"] < 60.0):
                return
            n, since, tools = u["n"], u["since"], dict(u["tools"])
            self._unaudited = {"n": 0, "since": None, "tools": collections.Counter()}
        which = ", ".join(f"{t} ×{c}" for t, c in sorted(tools.items()))
        detail = (f"{n} more call(s) that sent nothing (refused, invalid, no-op or dry run) "
                  f"between {utc_iso(since)} and {utc_iso(now)} were counted but not audited one "
                  f"by one (over {MAX_AUDITED_REFUSALS_PER_MIN} a minute): {which}")
        log.warning("AI: %s", detail)
        self._audit("refused", detail, unaudited=n)

    def _audit(self, kind: str, detail: str, **fields: Any) -> None:
        """Audit without ever raising: by now a command may already be out,
        and the caller must still get its true result."""
        try:
            self.audit.record(kind, detail, **fields)
        except Exception:                            # noqa: BLE001
            log.exception("Could not audit %s: %s", kind, log_text(detail))

    def _succeeded(self, call: _Call) -> Tuple[bool, dict]:
        res: Dict[str, Any] = {"ok": True, "tool": call.tool, "published": call.published,
                               "confirmed": call.confirmed,
                               "readback": call.readback,
                               "warnings": call.warnings}
        if call.previous:
            res["previous"] = deep_untrusted(call.previous, 200)
        if call.note:
            res["note"] = call.note
        detail = call.detail or call.tool
        if call.published and call.confirmed is False:
            detail += " — NOT confirmed"
        kind = "note" if call.tool == "add_note" else "action"
        now = self._clock()
        utc = utc_iso(now)
        with self._stats_lock:
            # A no-op ("already closed — nothing sent") shares the budget of
            # the other calls that sent nothing; past it, it is only counted.
            audit_it = (kind == "note" or not call.noop
                        or self._quiet_slot_locked(now, call.tool))
            if kind == "note":
                self.counters["notes"] += 1
                self.last_note = {"utc": utc, "detail": detail, "client": call.who}
            else:
                self.counters["actions"] += 1
                if not call.noop:       # the card's "last action" is one that did something
                    self.last_action = {"utc": utc, "tool": call.tool, "detail": detail,
                                        "reason": call.reason, "client": call.who}
        if not audit_it:
            log.debug("AI %s (not audited, over %d calls that sent nothing a minute): %s",
                      call.tool, MAX_AUDITED_REFUSALS_PER_MIN, log_text(detail))
            return False, res
        log.info("AI %s (%s): %s", call.tool, call.who,
                 log_text(detail, LOG_TEXT_MAX if kind == "note" else 500))
        if call.noop:
            self.flush_unaudited(force=True)
        self._audit(kind, detail, tool=call.tool,
                    args=call.args_for_audit() if kind == "action" else None,
                    reason=call.reason, published=call.published or None,
                    confirmed=call.confirmed if call.published else None,
                    warnings=call.warnings or None, client=call.who,
                    previous=(call.previous or None) if call.published else None)
        self._changed()
        return False, res

    def _dry_result(self, call: _Call) -> Tuple[bool, dict]:
        """A dry run that passed every check: what it would have published."""
        res: Dict[str, Any] = {"ok": True, "tool": call.tool, "dry_run": True,
                               "would_publish": call.would_publish,
                               "readback": call.readback,
                               "warnings": call.warnings}
        if call.previous:
            res["previous"] = deep_untrusted(call.previous, 200)
        res["note"] = ("dry run: every check passed; nothing was published and nothing changed"
                       + (f". {call.note}" if call.note else ""))
        detail = "dry run: " + (call.detail or call.tool)
        now = self._clock()
        with self._stats_lock:
            self.counters["dry_runs"] += 1
            audit_it = self._quiet_slot_locked(now, call.tool)
        if audit_it:
            log.info("AI %s (%s) %s", call.tool, call.who, log_text(detail, 500))
            self.flush_unaudited(force=True)
            self._audit("dry_run", detail, tool=call.tool, args=call.args_for_audit(),
                        reason=call.reason, would_publish=call.would_publish or None,
                        client=call.who)
            self._changed()
        return False, res

    def _changed(self) -> None:
        if self._on_change is not None:
            try:
                self._on_change()
            except Exception:
                log.exception("Error in actions change callback")

    def _publish(self, call: _Call, topic: str, payload: dict, *, check_arm: bool = True) -> None:
        """One command.  The arm (for this call's client) is re-checked right
        here, before every publish.  A dry run records it instead."""
        if check_arm and call.group is not None \
                and not self.arms.is_armed_for(call.group, call.client):
            if call.published:
                raise Refusal(f"{call.group} were disarmed during the call; "
                              f"{len(call.published)} command(s) had already been sent, the "
                              f"rest were not")
            raise Refusal(self.arms.not_armed_text(call.group, call.client))
        if call.ramp_id is not None and self.ramps is not None \
                and not self.ramps.is_active(call.args["zone"], call.ramp_id):
            raise Refusal("the ramp was stopped")
        if call.dry_run:
            call.would_publish.append({"topic": topic, "payload": copy.deepcopy(payload)})
            return
        if not self.bus.publish_command(topic, payload):
            raise Refusal("the MQTT publish failed (is the broker connection down?)"
                          + (f"; {len(call.published)} command(s) had already been sent"
                             if call.published else ""))
        call.published.append({"topic": topic, "payload": copy.deepcopy(payload)})

    # ── common checks ────────────────────────────────────────────────────

    def _require_armed(self, call: _Call) -> None:
        if call.group is not None and not self.arms.is_armed_for(call.group, call.client):
            raise Refusal(self.arms.not_armed_text(call.group, call.client))

    def _require_rails_ok(self) -> None:
        err = self.rails.error
        if err:
            raise Refusal(f"the rails file is invalid, so nothing is writable: {err}")

    def _require_zone_rails(self, zone: str) -> Tuple[float, float]:
        if zone in self.rails.stale_zones:
            raise Refusal(f"zone {zone}: Lucas's heater rails for it were saved when the "
                          f"software zone names pointed at other sensors (the zone mapping "
                          f"changed since), so they count as unset")
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
                step = untrusted(p.get("current_step"), 60)
                raise Refusal(f"the Sequencer is running (item {step}) — it rewrites setpoints "
                              f"and valves, so AI writes are refused while it runs")
        trk = self.bus.raw(TRACKERS_TOPIC)
        if trk is not None:
            p = trk["payload"]
            if not isinstance(p, list):
                raise Refusal("the trackers status is unreadable, so it cannot be ruled out "
                              "that a tracker is rewriting a setpoint")
            on = [t for t in p if not isinstance(t, dict) or t.get("enabled")]
            if on:
                names = ", ".join(
                    f"{untrusted(t.get('id'), 40)} (→ {untrusted(t.get('target'), 40)})"
                    if isinstance(t, dict) else untrusted(_show(t), 40) for t in on[:5])
                raise Refusal(f"tracker(s) enabled: {names} — a tracker rewrites its target "
                              f"every second, so AI writes are refused while one is enabled")

    def _take_write_slot(self, call: _Call) -> None:
        """The last step before publishing: the per-minute rate and one write
        of the client's lease (a dry run only checks both, using neither)."""
        now = self._clock()
        with self._stats_lock:
            while self._write_times and now - self._write_times[0] >= 60.0:
                self._write_times.popleft()
            if len(self._write_times) >= MAX_WRITES_PER_MIN:
                wait = 60.0 - (now - self._write_times[0])
                raise Refusal(f"rate limit: at most {MAX_WRITES_PER_MIN} AI write calls a "
                              f"minute — wait {wait:.0f} s")
            if not call.dry_run:
                self._write_times.append(now)
        if call.group is None:
            return
        if call.dry_run:
            if not self.arms.is_armed_for(call.group, call.client):
                raise Refusal(self.arms.not_armed_text(call.group, call.client))
            if self.arms.budget_left(call.group) <= 0:
                raise Refusal(f"{call.group}: this arm's write budget is used up")
            return
        problem = self.arms.take_write(call.group, call.client)
        if problem:
            raise Refusal(problem)
        call.budget_taken = True

    def _take_note_slot(self, call: _Call) -> None:
        now = self._clock()
        with self._stats_lock:
            while self._note_times and now - self._note_times[0] >= 60.0:
                self._note_times.popleft()
            if len(self._note_times) >= MAX_NOTES_PER_MIN:
                wait = 60.0 - (now - self._note_times[0])
                raise Refusal(f"rate limit: at most {MAX_NOTES_PER_MIN} notes a minute — wait "
                              f"{wait:.0f} s, or put more into one note")
            if not call.dry_run:
                self._note_times.append(now)

    def _require_guard(self, check: str, *args: Any) -> None:
        """One of the pressure guard's preconditions (guard.py); raises Refusal."""
        if self.guard is None:
            raise Refusal("this service has no pressure guard, so guarded writes are refused")
        problem = getattr(self.guard, check)(*args)
        if problem:
            raise Refusal(problem)

    def _interlock(self, *, require_untripped: bool = True) -> dict:
        r = self.bus.raw(PV_INTERLOCK_TOPIC)
        p = r["payload"] if r else None
        if not isinstance(p, dict) or not _is_num(p.get("min_k")) or not _is_num(p.get("max_k")):
            raise Refusal("the PV interlock status (xsphere/status/pv_interlock) has not been "
                          "seen, so its band is unknown — heater writes are refused")
        if require_untripped and p.get("tripped") is not False:
            reasons = p.get("reasons")
            why = ("; ".join(f"RTD{clean_text(k, 10)}: {untrusted(v, 80)}"
                             for k, v in list(reasons.items())[:6])
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
            raise Refusal(f"zone {zone}: its setpoint is the expression {untrusted(expr, 80)}, "
                          f"not a number; a direct setpoint would replace it, which "
                          f"replace_expression: true does — only when Lucas asked for that")
        ev = status.get("setpoint_expr_value_k")
        if _is_num(ev):
            return float(ev), {"source": "replaced", "expr": expr}
        if _is_num(sp_k):
            return float(sp_k), {"source": "replaced", "expr": expr}
        raise Refusal(f"zone {zone}: the expression {untrusted(expr, 80)} has no current value")

    def _pv_fed_problem(self, status: dict, zone: Optional[str] = None) -> Optional[str]:
        """Why nothing live and plausible feeds the PID's process value —
        None when it is fed: pv_expr set, evaluating, the PV register agreeing
        with it ≤ 5 K, and (with ``zone``) its sensor passing the PV
        plausibility re-check (guard.PvCheck)."""
        pv_expr = status.get("pv_expr")
        pv_val = status.get("pv_expr_value_k")
        pv_k = status.get("pv_k")
        if not isinstance(pv_expr, str) or not pv_expr.strip():
            why = "its pv_expr is empty, so nothing feeds the PID's process value"
            if _is_num(pv_k) and abs(float(pv_k) - 500.0) < 0.5:
                why += " (500 K is the PV interlock's surrogate)"
            return why
        if not _is_num(pv_val):
            return f"its pv_expr {untrusted(pv_expr, 60)} does not evaluate"
        if not _is_num(pv_k) or abs(float(pv_k) - float(pv_val)) > PV_AGREE_K:
            return (f"its PV register reads {_fmt(pv_k, 'K')} but pv_expr {untrusted(pv_expr, 60)} "
                    f"is {_fmt(pv_val, 'K')}")
        if zone is not None and self.pvcheck is not None:
            flag = self.pvcheck.flag(zone)
            if flag and flag.get("problem"):
                return f"its PV source failed the plausibility check: {flag['problem']}"
        return None

    def _require_pv_fed(self, zone: str, status: dict) -> None:
        why = self._pv_fed_problem(status, zone)
        if why is None:
            return
        raise Refusal(f"PID {zone} sees {_fmt(status.get('pv_k'), 'K')} — no live PV source "
                      f"({why}); choose one with set_pv_source first. A setpoint increase on a "
                      f"frozen PV would heat open-loop")

    def _check_band(self, zone: str, v: float, il: dict) -> None:
        lo_b = float(il["min_k"]) + INTERLOCK_MARGIN_K
        hi_b = float(il["max_k"]) - INTERLOCK_MARGIN_K
        if not lo_b <= v <= hi_b:
            raise Refusal(f"zone {zone}: {_fmt(v, 'K')} is outside the PV interlock band less "
                          f"its {INTERLOCK_MARGIN_K:g} K margin ({_fmt(lo_b)}–{_fmt(hi_b)} K); "
                          f"a setpoint there would trip the interlock for all three zones")

    def _anchor(self, e: float, lo: float, hi: float, status: dict,
                zone: Optional[str] = None) -> Tuple[float, str]:
        """Where a setpoint change is measured from, and what that point is.

        The current setpoint E when it is inside the rails.  When it is not —
        3 K after every slowcontrol restart, or one a person left outside —
        E says nothing about what a new setpoint would do; the temperature
        the PID regulates on does, so the zone's PV is used when it is fed
        (:meth:`_pv_fed_problem`).  Otherwise E itself.
        """
        if lo <= e <= hi:
            return e, "setpoint"
        if self._pv_fed_problem(status, zone) is None:
            return float(status["pv_k"]), "pv"
        return e, "setpoint"

    def _check_setpoint(self, zone: str, v: float, replace: bool, status: dict, il: dict,
                        now: float) -> dict:
        """Every value check for one zone's new setpoint; raises Refusal.

        max_step_k and the max_rate_k_per_min allowance apply to |v − A|, A
        being :meth:`_anchor`.  When A itself is outside the rails, a move
        into them is measured from the nearest rail edge instead: it may land
        at most max_step_k inside it (anywhere inside without a max_step_k).
        """
        lo, hi = self._require_zone_rails(zone)
        zr = self.rails.zone(zone)
        if not lo <= v <= hi:
            raise Refusal(f"zone {zone}: {_fmt(v, 'K')} is outside Lucas's rails "
                          f"{_fmt(lo)}–{_fmt(hi)} K")
        self._check_band(zone, v, il)
        e, info = self._effective(zone, status, replace)
        anchor, source = self._anchor(e, lo, hi, status, zone)
        if source == "pv":
            frm = (f"the measured PV {_fmt(anchor)} K (the setpoint {_fmt(e)} K is outside the "
                   f"rails, so changes are measured from the temperature the PID regulates on)")
        else:
            frm = f"{_fmt(e)} K"
        step = zr.get("max_step_k")
        if lo <= anchor <= hi:
            ref = anchor
            delta = abs(v - anchor)
            # 1 mK of slack for the ramp's 0.001 K rounding; physically nothing.
            if step is not None and delta > float(step) + 1e-3:
                raise Refusal(f"zone {zone}: {frm} → {_fmt(v)} K is a change of {_fmt(delta)} K; "
                              f"Lucas's max_step_k is {_fmt(step)} K. Use smaller steps or "
                              f"start_heater_ramp")
            rate = zr.get("max_rate_k_per_min")
            if rate is not None:
                with self._stats_lock:
                    last = self._zone_ai.get(zone)
                since_ts = last["ts"] if last else self.started_ts
                minutes = min(max((now - since_ts) / 60.0, 0.0), RATE_CAP_MIN)
                allowance = float(rate) * minutes
                if step is not None:
                    allowance = min(allowance, float(step))
                if delta > allowance + 1e-3:
                    since = ("since the last AI change" if last else "since the service started")
                    raise Refusal(f"zone {zone}: a change of {_fmt(delta)} K from {frm} exceeds "
                                  f"the rate allowance of {allowance:.2f} K (max_rate_k_per_min "
                                  f"{_fmt(rate)} K/min, {minutes:.1f} min {since}). Wait, or "
                                  f"use start_heater_ramp")
        else:
            # From outside the rails: into them, at most one step past the near edge.
            ref = lo if anchor < lo else hi
            delta = abs(v - ref)
            if step is not None and delta > float(step) + 1e-3:
                near = (f"{_fmt(ref)}–{_fmt(min(hi, ref + float(step)))}" if ref == lo
                        else f"{_fmt(max(lo, ref - float(step)))}–{_fmt(ref)}")
                what = "the measured PV" if source == "pv" else "the setpoint"
                raise Refusal(f"zone {zone}: {what} {_fmt(anchor)} K is outside Lucas's rails "
                              f"{_fmt(lo)}–{_fmt(hi)} K, so a move into them may land at most "
                              f"max_step_k {_fmt(step)} K inside the nearest edge "
                              f"({_fmt(ref)} K): {near} K. {_fmt(v)} K is {_fmt(delta)} K from it")
        if v > e + 1e-9:
            self._require_pv_fed(zone, status)
        warnings = []
        mode = status.get("mode")
        if mode != "auto":
            warnings.append(f"zone {zone} is in mode {untrusted(mode, 20)}: the setpoint has no "
                            f"effect until the zone is in auto (set_heater_auto)")
        if info["source"] == "replaced":
            warnings.append(f"zone {zone}: replaced the setpoint expression "
                            f"{untrusted(info['expr'], 80)}")
        return {"E": e, "expr": info["expr"], "source": info["source"], "anchor": anchor,
                "anchor_source": source, "ref": ref, "warnings": warnings}

    # ── heaters: tools ───────────────────────────────────────────────────

    def set_heater_setpoints(self, args: Any, client: Optional[str] = None) -> Tuple[bool, dict]:
        def body(call: _Call) -> None:
            self._setpoints(call, dict(call.args["setpoints"]),
                            bool(call.args.get("replace_expression", False)))
        return self._run("set_heater_setpoints", args, body, client=client)

    def set_heater_gradient(self, args: Any, client: Optional[str] = None) -> Tuple[bool, dict]:
        def body(call: _Call) -> None:
            a = call.args
            base = float(a["base_k"])
            targets = {"top": round(base, 6), "bottom": round(base + float(a["dv_k"]), 6),
                       "nozzle": round(base + float(a["dl_k"]), 6)}
            self._setpoints(call, targets, bool(a.get("replace_expression", False)))
        return self._run("set_heater_gradient", args, body, client=client)

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
            call.previous[z] = {"setpoint_k": plans[z]["E"], "setpoint_expr": plans[z]["expr"]}
        if any(float(v) > plans[z]["E"] + 1e-9 for z, v in targets.items()):
            self._require_guard("warming_problem")
        self._take_write_slot(call)

        seqs = {}
        for z, v in targets.items():
            if not call.dry_run and self.ramps is not None and self.ramps.is_active(z):
                self.ramps.stop([z], "replaced by a direct AI setpoint")
            seqs[z] = self._seq(self._status_topic(z))
            self._publish(call, f"xsphere/commands/pid/{z}/setpoint", {"value_k": float(v)})
            if not call.dry_run:
                with self._stats_lock:
                    self._zone_ai[z] = {"value": float(v), "ts": self._clock()}
        call.detail = ", ".join(
            f"{z} {_fmt(plans[z]['E'])} → {_fmt(v)} K"
            + (f" (measured from PV {_fmt(plans[z]['anchor'])} K)"
               if plans[z]["anchor_source"] == "pv" else "")
            for z, v in targets.items())
        if call.dry_run:
            for z in targets:
                call.readback[z] = {"anchor_k": plans[z]["anchor"],
                                    "anchor_source": plans[z]["anchor_source"]}
            if any(self.ramps is not None and self.ramps.is_active(z) for z in targets):
                call.note = "a running ramp for these zones would be stopped"
            return

        checks = [(self._status_topic(z), seqs[z],
                   (lambda p, want=repr(float(v)): p.get("setpoint_expr") == want))
                  for z, v in targets.items()]
        call.confirmed = self._wait_consecutive(checks)
        for z, v in targets.items():
            st = self._payload(self._status_topic(z)) or {}
            keys = ("setpoint_expr", "setpoint_k", "mode", "pv_k", "pv_expr", "output_pct")
            call.readback[z] = _status_copy(st, keys)
            call.readback[z]["previous_setpoint_k"] = plans[z]["E"]
            call.readback[z]["previous_expr"] = (untrusted(plans[z]["expr"], 80)
                                                 if plans[z]["expr"] else "")
            # What max_step_k / the rate allowance were measured from.
            call.readback[z]["anchor_k"] = plans[z]["anchor"]
            call.readback[z]["anchor_source"] = plans[z]["anchor_source"]
            if plans[z]["ref"] != plans[z]["anchor"]:
                call.readback[z]["measured_from_rail_edge_k"] = plans[z]["ref"]
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

    def start_heater_ramp(self, args: Any, client: Optional[str] = None) -> Tuple[bool, dict]:
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
                    raise Refusal(f"zone {z}: its setpoint is the expression "
                                  f"{untrusted(expr, 80)}; a ramp only starts from a numeric "
                                  f"setpoint")
                e, _info = self._effective(z, status, False)
                if not lo <= e <= hi:
                    raise Refusal(f"zone {z}: its current setpoint {_fmt(e, 'K')} is outside "
                                  f"the rails {_fmt(lo)}–{_fmt(hi)} K; a ramp steps inside the "
                                  f"rails only — set a setpoint inside them first with "
                                  f"set_heater_setpoints (with the setpoint outside the rails, "
                                  f"a change is measured from the zone's live PV; from outside "
                                  f"the rails it may land at most max_step_k past the nearest "
                                  f"edge)")
                if target > e + 1e-9:
                    self._require_pv_fed(z, status)
                if status.get("mode") != "auto":
                    call.warnings.append(f"zone {z} is in mode {untrusted(status.get('mode'), 20)}"
                                         f": the setpoint has no effect until it is in auto")
                plans[z] = {"E": e, "expr": expr}
                call.previous[z] = {"setpoint_k": e, "setpoint_expr": expr}
            if any(t > plans[z]["E"] + 1e-9 for z, t in targets.items()):
                self._require_guard("warming_problem")
            self._take_write_slot(call)
            started, at_target = [], []
            for z, target in targets.items():
                if abs(target - plans[z]["E"]) < 1e-6:
                    at_target.append(z)
                    continue
                if not call.dry_run:
                    self.ramps.start_ramp(z, target, rate, call.reason or "", plans[z]["expr"],
                                          plans[z]["E"], client=call.client)
                started.append(f"{z} {_fmt(plans[z]['E'])} → {_fmt(target)} K")
            call.noop = not started
            call.detail = (f"ramp at {_fmt(rate)} K/min: " + (", ".join(started) or "nothing")
                           + (f"; already at target: {', '.join(at_target)}" if at_target else ""))
            if call.dry_run:
                call.note = ("the ramp would start now; each 10 s step would be checked again "
                             "then")
                return
            call.readback["ramps"] = self.ramps.status()
            call.note = (f"The service moves each setpoint every {self.ramps.interval_s:g} s "
                         "through the same checks as set_heater_setpoints, and stops the ramp on "
                         "any refusal, a disarm, an outside change of the setpoint or "
                         "stop_heater_ramp. Progress: get_ai_control.")
        return self._run("start_heater_ramp", args, body, client=client)

    def stop_heater_ramp(self, args: Any, client: Optional[str] = None) -> Tuple[bool, dict]:
        def body(call: _Call) -> None:
            zones = call.args.get("zones")
            if self.ramps is None:
                stopped = []
            elif call.dry_run:
                stopped = [e["zone"] for e in self.ramps.status()
                           if zones is None or e["zone"] in zones]
            else:
                before = {e["zone"]: e for e in self.ramps.status()}
                stopped = self.ramps.stop(zones, f"stop_heater_ramp ({call.who}): {call.reason}",
                                          audit=False)
                call.readback["stopped"] = [
                    {"zone": z, "target_k": before[z]["target_k"],
                     "setpoint_k": before[z]["current_k"]} for z in stopped if z in before]
            call.detail = ("stopped ramp(s): " + ", ".join(stopped)) if stopped \
                else "no ramp was running"
            if not stopped:
                call.noop = True
                call.note = "no ramp was running" + (f" for {', '.join(zones)}" if zones else "")
        return self._run("stop_heater_ramp", args, body, lock=False, client=client)

    def ramp_step(self, zone: str, entry: dict) -> Tuple[str, Any]:
        """One ramp step for ``zone`` (called by the ramp thread).

        Same checks as a direct setpoint, for the client that started the
        ramp (its lease must still hold); never waits for a confirmation (the
        next step's "is the setpoint still what the ramp wrote?" check is the
        confirmation), so the write lock is only held for the checks and the
        publish.  Steps do not use the lease's write budget: starting the ramp
        did.
        """
        if not self.write_lock.acquire(blocking=False):
            return "busy", None
        try:
            if self.ramps is None or not self.ramps.is_active(zone, entry["id"]):
                return "busy", None
            call = _Call("start_heater_ramp", {"zone": zone, "reason": entry.get("reason")},
                         entry.get("client"))
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
                                      f"started (was {untrusted(entry.get('start_expr'), 40)}, "
                                      f"now {untrusted(expr, 40)})")
                elif expr != repr(float(expected)):
                    raise Refusal(f"the setpoint is now {untrusted(expr, 40)}, not the "
                                  f"{_fmt(expected)} K the ramp wrote — changed by someone else, "
                                  f"or the command was lost")
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
                if nxt > e + 1e-9:
                    self._require_guard("warming_problem")
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

    def set_heater_auto(self, args: Any, client: Optional[str] = None) -> Tuple[bool, dict]:
        def body(call: _Call) -> None:
            z = call.args["zone"]
            self._require_armed(call)
            self._require_rails_ok()
            lo, hi = self._require_zone_rails(z)
            self._require_global()
            status = self._pid_status(z)
            mode = status.get("mode")
            if mode == "auto":
                call.noop = True
                call.detail = f"zone {z} already in auto — nothing sent"
                call.note = f"zone {z} is already in auto; nothing was sent"
                return
            if mode != "manual":
                raise Refusal(f"zone {z}: its PID mode reads {untrusted(mode, 20)}, not manual — "
                              f"cannot tell what switching would do")
            il = self._interlock()
            expr = status.get("setpoint_expr")
            value = parse_numeric_expr(expr)
            if value is None:
                raise Refusal(f"zone {z}: set a setpoint first — its setpoint expression is "
                              f"{untrusted(expr, 80)}, and auto mode needs a numeric setpoint "
                              f"inside the rails that the PLC re-asserts every poll")
            if not lo <= value <= hi:
                raise Refusal(f"zone {z}: its setpoint {_fmt(value, 'K')} is outside Lucas's "
                              f"rails {_fmt(lo)}–{_fmt(hi)} K; set one inside first")
            self._check_band(z, value, il)
            self._require_pv_fed(z, status)
            # Back to auto can raise the heater output as far as the PID asks.
            self._require_guard("warming_problem")
            call.previous[z] = {"mode": "manual", "output_pct": status.get("output_pct")}
            self._take_write_slot(call)
            topic = self._status_topic(z)
            seq0 = self._seq(topic)
            self._publish(call, f"xsphere/commands/pid/{z}/mode", {"mode": "auto"})
            call.detail = f"zone {z} manual → auto at SP {_fmt(value)} K"
            if call.dry_run:
                return
            call.confirmed = self._wait_consecutive(
                [(topic, seq0, lambda p: p.get("mode") == "auto")])
            st = self._payload(topic) or {}
            keys = ("mode", "mode_cmd", "setpoint_k", "setpoint_expr", "pv_k", "output_pct")
            call.readback[z] = _status_copy(st, keys)
            if not call.confirmed:
                call.warnings.append(f"not confirmed: zone {z} mode did not read auto on two "
                                     f"consecutive messages within {self.confirm_timeout_s:g} s")
        return self._run("set_heater_auto", args, body, client=client)

    def set_pv_source(self, args: Any, client: Optional[str] = None) -> Tuple[bool, dict]:
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
            if self.pvcheck is None:
                raise Refusal("this service has no PV plausibility check, so PV sources are "
                              "not writable")
            problem = self.pvcheck.problem(z, source, value, use_history=True, for_write=True)
            if problem:
                raise Refusal(f"zone {z}: {problem} — not a plausible process value for the PID")
            prev = status.get("pv_expr") if isinstance(status.get("pv_expr"), str) else ""
            prev = prev.strip()
            if prev == alias and _is_num(status.get("pv_expr_value_k")):
                call.noop = True
                call.detail = f"zone {z} PV already {alias} — nothing sent"
                call.note = f"zone {z} already uses {alias} ({source}) as its PV; nothing sent"
                return
            pv_k = status.get("pv_k")
            if prev:
                if not _is_num(pv_k) or abs(float(value) - float(pv_k)) > PV_SWITCH_MAX_JUMP_K:
                    raise Refusal(f"zone {z}: switching from {untrusted(prev, 40)} to {alias} "
                                  f"would move the PID's PV from {_fmt(pv_k, 'K')} to "
                                  f"{_fmt(value, 'K')} (more than {PV_SWITCH_MAX_JUMP_K:g} K) — "
                                  f"the PID would jump")
            # A lower PV makes the PID heat harder (and none → a real one, from
            # the 500 K surrogate, starts it heating at all).
            if not prev or not _is_num(pv_k) or float(value) < float(pv_k) - 0.5:
                self._require_guard("warming_problem")
            call.previous[z] = {"pv_expr": prev}
            self._take_write_slot(call)
            topic = self._status_topic(z)
            seq0 = self._seq(topic)
            self._publish(call, f"xsphere/commands/pid/{z}/pv_expr", {"expr": alias})
            call.detail = (f"zone {z} PV source {clean_text(prev, 40) or '(none)'} → {alias} "
                           f"({source}, {sensor})")
            if call.dry_run:
                return
            with self._stats_lock:
                self._pv_ai[z] = alias
            call.confirmed = self._wait_consecutive(
                [(topic, seq0, lambda p: (p.get("pv_expr") == alias
                                          and _is_num(p.get("pv_expr_value_k"))))])
            st = self._payload(topic) or {}
            keys = ("pv_expr", "pv_expr_value_k", "pv_k", "setpoint_k", "output_pct")
            call.readback[z] = _status_copy(st, keys)
            call.readback[z]["previous_pv_expr"] = untrusted(prev, 40) if prev else ""
            if not call.confirmed:
                msg = (f"not confirmed: zone {z} did not report pv_expr {alias!r} with a value "
                       f"within {self.confirm_timeout_s:g} s")
                cur = st.get("pv_expr")
                cur = cur.strip() if isinstance(cur, str) else None
                if not prev:
                    msg += "; the previous pv_expr was empty, so nothing was reverted"
                elif cur is None or cur in (alias, prev):
                    # The service's own repair: put back what was there.  Also
                    # when the status still shows prev — the AI's command may
                    # yet be applied late, and this one is applied after it.
                    try:
                        self._publish(call, f"xsphere/commands/pid/{z}/pv_expr", {"expr": prev},
                                      check_arm=False)
                        with self._stats_lock:
                            self._pv_ai.pop(z, None)
                        msg += f"; reverted to the previous pv_expr {untrusted(prev, 40)}"
                    except Refusal as r:
                        msg += f"; the revert to {untrusted(prev, 40)} failed too: {r.text}"
                else:
                    # A third expression: someone else chose a PV meanwhile.
                    msg += (f"; pv_expr now reads {untrusted(cur, 40)} (changed by someone else "
                            f"meanwhile) — not reverted")
                raise Failure(msg)
        return self._run("set_pv_source", args, body, client=client)

    # ── the service's own safety actions (guard.py calls these) ───────────

    def pv_source_failed(self, zone: str, source: str, problem: str) -> None:
        """A zone's current PV source failed the plausibility re-check: disarm
        the heaters and, when the AI chose that clamp sensor, put the zone back
        on its cube RTD (if that one is plausible)."""
        alias = self.zones.alias(zone, source)
        reason = (f"zone {zone}: its PV source {alias} ({source}) failed the plausibility "
                  f"check — {problem}")
        disarmed = self.arms.disarm("heaters", reason, auto=True)
        with self._stats_lock:
            ai_chose = self._pv_ai.get(zone) == alias
        published, note = [], ""
        if source == "clamp" and ai_chose:
            cube = self.zones.alias(zone, "cube")
            sid = self.zones.sensor_state(zone, "cube")
            cube_v = self.bus.state_value(sid) if sid else None
            cube_problem = (self.pvcheck.problem(zone, "cube", cube_v) if self.pvcheck
                            else "no PV check") if cube else "no cube sensor"
            if cube and not cube_problem:
                topic = f"xsphere/commands/pid/{zone}/pv_expr"
                if self.bus.publish_command(topic, {"expr": cube}):
                    published.append({"topic": topic, "payload": {"expr": cube}})
                    with self._stats_lock:
                        self._pv_ai.pop(zone, None)
                    note = f"; the service put zone {zone} back on its cube RTD {cube}"
                else:
                    note = f"; putting zone {zone} back on {cube} FAILED (publish refused)"
            else:
                note = f"; not reverted to the cube RTD ({cube_problem})"
        if disarmed or published:
            self._audit("pv_check", reason + note, zone=zone, published=published or None)
            self._changed()

    def guard_trip(self, reason: str) -> None:
        """The pressure guard tripped: stop every ramp, disarm heaters and
        pneumatic, and lower each zone the AI last set to its setpoint_min_k."""
        stopped = self.ramps.stop(None, reason) if self.ramps is not None else []
        disarmed = self.arms.disarm_groups(["heaters", "pneumatic"], reason, auto=True)
        published, lowered, skipped = [], [], []
        for z, last in self.zone_ai_setpoints().items():
            st = self._payload(self._status_topic(z)) or {}
            if st.get("setpoint_expr") != repr(float(last["value"])):
                continue                              # changed since by someone else
            lims = self.rails.zone_limits(z)
            if lims is None:
                skipped.append(f"{z} (no setpoint_min_k in force)")
                continue
            if float(last["value"]) <= lims[0] + 1e-9:
                continue
            topic = f"xsphere/commands/pid/{z}/setpoint"
            payload = {"value_k": float(lims[0])}
            if self.bus.publish_command(topic, payload):
                published.append({"topic": topic, "payload": payload})
                lowered.append(f"{z} {_fmt(last['value'])} → {_fmt(lims[0])} K")
            else:
                skipped.append(f"{z} (publish failed)")
        parts = [reason]
        if stopped:
            parts.append("ramps stopped: " + ", ".join(stopped))
        if disarmed:
            parts.append("disarmed: " + ", ".join(disarmed))
        if lowered:
            parts.append("AI setpoints lowered to setpoint_min_k: " + "; ".join(lowered))
        if skipped:
            parts.append("not lowered: " + "; ".join(skipped))
        self._audit("guard", " — ".join(parts), published=published or None)
        self._changed()

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

    @staticmethod
    def _unpowered_text(ch: int, what: str) -> str:
        return (f"supply ch{ch}: {what} would leave the heater without power, and Lucas's rails "
                f"do not let the AI switch it off (may_switch_off is false) — a heater without "
                f"power lets LN2 cool the cell")

    def _require_zone_ready_for_power(self, ch: int, il: dict) -> None:
        """Before more power reaches the heater a supply channel feeds: its PID
        zone must be in auto, at a numeric setpoint inside the rails and the
        interlock band, regulating on a live PV — what set_heater_auto checks
        before it enables a loop.  Raises Refusal."""
        zone = self.zones.channel_zone(ch)
        if zone not in ZONES:
            raise Refusal(f"supply ch{ch}: the registry label does not say which PID zone it "
                          f"feeds, so the service cannot check that zone before powering it")
        head = f"supply ch{ch} feeds software zone {zone}"
        status = self._pid_status(zone)
        mode = status.get("mode")
        if mode != "auto":
            raise Refusal(f"{head}, whose PID is in mode {untrusted(mode, 20)}: its output is "
                          f"whatever a person set, so the AI may not power it up — return it to "
                          f"auto first (set_heater_auto)")
        try:
            e, _info = self._effective(zone, status, False)
        except Refusal:
            raise Refusal(f"{head}, whose setpoint is the expression "
                          f"{untrusted(status.get('setpoint_expr'), 80)}, not a plain number — "
                          f"the service cannot check where its PID would drive the "
                          f"heater") from None
        lo, hi = self._require_zone_rails(zone)
        if not lo <= e <= hi:
            raise Refusal(f"{head}, whose setpoint {_fmt(e, 'K')} is outside Lucas's rails "
                          f"{_fmt(lo)}–{_fmt(hi)} K — set one inside them first")
        self._check_band(zone, e, il)
        why = self._pv_fed_problem(status, zone)
        if why is not None:
            raise Refusal(f"{head}, whose PID sees {_fmt(status.get('pv_k'), 'K')} — no live PV "
                          f"source ({why}); choose one with set_pv_source first. Power into a "
                          f"PID regulating on a frozen PV heats open-loop")

    @staticmethod
    def _nge_payload(what: str, value: Any) -> Tuple[dict, str, Any]:
        """(command payload, status field, wanted value) for one supply step."""
        if what == "voltage":
            return {"value_v": float(value)}, "voltage_request_v", float(value)
        if what == "current":
            return {"value_a": float(value)}, "current_request_a", float(value)
        return {"state": int(bool(value))}, "desired", int(bool(value))

    @staticmethod
    def _check_held(ch: int, sr: dict, vr: Any, ir: Any) -> None:
        """Before switching a channel on: its held V / I inside the rails."""
        if not (_is_num(vr) and _is_num(ir)
                and float(sr["voltage_min_v"]) - 1e-6 <= float(vr)
                <= float(sr["voltage_max_v"]) + 1e-6
                and float(sr["current_min_a"]) - 1e-6 <= float(ir)
                <= float(sr["current_max_a"]) + 1e-6):
            raise Refusal(f"supply ch{ch}: not switched on — its held targets "
                          f"({_fmt(vr, 'V')}, {_fmt(ir, 'A')}) are not inside "
                          f"Lucas's rails; set voltage_v / current_a inside them in "
                          f"the same call")

    def set_heater_supply(self, args: Any, client: Optional[str] = None) -> Tuple[bool, dict]:
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
                if vq == 0 and not sr["may_switch_off"]:
                    raise Refusal(self._unpowered_text(ch, "0 V"))
            if "current_a" in a:
                if sr["current_min_a"] is None or sr["current_max_a"] is None:
                    raise Refusal(f"supply ch{ch}: Lucas has not set current rails "
                                  f"(current_min_a and current_max_a), so the current limit is "
                                  f"not writable")
                aq = round(float(a["current_a"]), 3)
                if not float(sr["current_min_a"]) <= aq <= float(sr["current_max_a"]):
                    raise Refusal(f"supply ch{ch}: {aq:g} A is outside Lucas's rails "
                                  f"{_fmt(sr['current_min_a'])}–{_fmt(sr['current_max_a'])} A")
                if aq == 0 and not sr["may_switch_off"]:
                    raise Refusal(self._unpowered_text(ch, "a 0 A current limit"))
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
            il = self._interlock()
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
                call.noop = True
                call.detail = f"supply ch{ch}: nothing to change"
                call.note = "everything asked for is already set; nothing was sent"
                call.confirmed = None
                self._nge_readback(call, ch, topic)
                return
            # Power into a heater goes where its PID drives it: switching on,
            # or raising V / I while the output is (or is about to be) on,
            # is held to what set_heater_auto checks before enabling a loop.
            on_now = st.get("output") == 1 or st.get("desired") == 1
            held = {"voltage": st.get("voltage_request_v"), "current": st.get("current_request_a")}
            raises = any(what in held and not (_is_num(held[what])
                                               and value <= float(held[what]) + 1e-6)
                         for what, value in steps)
            if ("output", True) in steps or (raises and (on_now or out is True)):
                self._require_zone_ready_for_power(ch, il)
                self._require_guard("warming_problem")
            call.previous[f"ch{ch}"] = {"voltage_request_v": st.get("voltage_request_v"),
                                        "current_request_a": st.get("current_request_a"),
                                        "output": st.get("desired")}
            now = self._clock()
            with self._stats_lock:
                while self._nge_times and now - self._nge_times[0] >= 60.0:
                    self._nge_times.popleft()
                used = len(self._nge_times)
            if used + len(steps) > NGE_MAX_PER_MIN:
                raise Refusal(f"NGE command budget: at most {NGE_MAX_PER_MIN} supply commands a "
                              f"minute ({used} used, this call needs {len(steps)}) — each one "
                              f"blocks slowcontrol's MQTT thread for seconds")
            self._take_write_slot(call)

            parts = []
            for what, value in steps:
                parts.append(f"{what} {value:g} {'V' if what == 'voltage' else 'A'}"
                             if what != "output" else f"output {'ON' if value else 'OFF'}")
            if call.dry_run:
                held_v = vq if vq is not None else st.get("voltage_request_v")
                held_i = aq if aq is not None else st.get("current_request_a")
                for what, value in steps:
                    if what == "output" and value is True:
                        self._check_held(ch, sr, held_v, held_i)
                    self._publish(call, f"xsphere/commands/nge/{ch}/{what}",
                                  self._nge_payload(what, value)[0])
                call.detail = f"supply ch{ch}: " + ", ".join(parts)
                self._nge_readback(call, ch, topic)
                return

            done = []
            for i, (what, value) in enumerate(steps):
                if what == "output" and value is True:
                    cur = self._payload(topic) or {}
                    self._check_held(ch, sr, cur.get("voltage_request_v"),
                                     cur.get("current_request_a"))
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
            call.detail = f"supply ch{ch}: " + ", ".join(parts)
            self._nge_readback(call, ch, topic)
        return self._run("set_heater_supply", args, body, client=client)

    def _nge_step(self, call: _Call, ch: int, topic: str, what: str, value: Any
                  ) -> Tuple[bool, Optional[str]]:
        self._nge_pace()
        nge = self._payload(NGE_TOPIC) or {}
        ev0 = (nge.get("event"), nge.get("event_utc"))
        seq0 = self._seq(topic)
        payload, field, want = self._nge_payload(what, value)
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
                call.readback["event"] = untrusted(ev, 200)
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
        call.readback[f"ch{ch}"] = _status_copy(st, (
            "output", "desired", "voltage_set_v", "voltage_request_v", "current_limit_a",
            "current_request_a", "voltage_v", "current_a", "power_w", "mode", "trip"))
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

    def set_ln2_valve(self, args: Any, client: Optional[str] = None) -> Tuple[bool, dict]:
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
            call.previous[vessel] = {k: st.get(k) for k in ("state", "desired", "auto_open",
                                                            "auto_close")}
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
                self._take_write_slot(call)
                minutes = float(vr["max_open_minutes"])
                if call.dry_run:
                    self._publish(call, f"{prefix}/state", {"state": 1})
                    call.detail = (f"LN2 {vessel} would open (the service would close it after "
                                   f"{minutes:g} min)")
                    self._valve_readback(call, vessel, topic)
                    return
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
                if seen["open"] and self.timers is not None:
                    # From now on a "closed" status may clear the timer.
                    self.timers.mark_seen_open(vessel)
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
                # An AI open that was never seen open may still be queued in
                # slowcontrol (behind slow NGE work): "reads closed" proves
                # nothing then, so the close is sent anyway — on the same
                # topic, it is applied after that open — and the timer stays.
                timer = self.timers.get(vessel) if self.timers is not None else None
                if timer is not None and (st.get("state") == 1 or st.get("desired") == 1):
                    if not call.dry_run:
                        self.timers.mark_seen_open(vessel)   # the open has landed
                    timer["seen_open"] = True
                unseen = timer is not None and not timer.get("seen_open")
                if (st.get("state") == 0 and st.get("desired") == 0 and st.get("auto_open") == 0
                        and not unseen):
                    if timer is not None and not call.dry_run:
                        self.timers.clear(vessel)        # it was seen open, and is closed now
                    call.noop = True
                    call.detail = f"LN2 {vessel} already closed — nothing sent"
                    call.note = f"LN2 {vessel} is already closed with auto-open off; nothing sent"
                    self._valve_readback(call, vessel, topic)
                    return
                self._require_ln2_interval(vessel)
                self._take_write_slot(call)
                seq0 = self._seq(topic)
                if not call.dry_run:
                    self._ln2_last[vessel] = self._clock()
                if st.get("auto_open") != 0:
                    # auto-open would re-open it on the next scan: off first.
                    self._publish(call, f"{prefix}/auto_open", {"enabled": False})
                self._publish(call, f"{prefix}/state", {"state": 0})
                sent = len(call.would_publish if call.dry_run else call.published)
                call.detail = f"LN2 {vessel} closed" + (" (auto-open switched off first)"
                                                        if sent > 1 else "")
                if unseen:
                    call.detail += (f" — sent although it reads closed: the AI's open of "
                                    f"{timer['opened_utc']} was never seen")
                if call.dry_run:
                    self._valve_readback(call, vessel, topic)
                    return
                call.confirmed = self._wait_consecutive(
                    [(topic, seq0, lambda p: p.get("state") == 0 and p.get("desired") == 0)])
                t_now = self.timers.get(vessel) if self.timers is not None else None
                if call.confirmed and t_now is not None:
                    if t_now.get("seen_open"):
                        self.timers.clear(vessel)
                    else:
                        call.warnings.append(
                            f"the AI's open of LN2 {vessel} ({t_now['opened_utc']}) was never "
                            f"seen; its auto-close timer stays until the valve has been seen "
                            f"open and closed again, or until {t_now['deadline_utc']}")
                if not call.confirmed:
                    call.warnings.append(f"not confirmed: LN2 {vessel} did not read closed on two "
                                         f"consecutive messages within "
                                         f"{self.confirm_timeout_s:g} s")
            self._valve_readback(call, vessel, topic)
        return self._run("set_ln2_valve", args, body, client=client)

    def _valve_readback(self, call: _Call, vessel: str, topic: str) -> None:
        st = self._payload(topic) or {}
        keys = ("state", "desired", "auto_close", "auto_open")
        call.readback[vessel] = _status_copy(st, keys)

    def set_ln2_auto(self, args: Any, client: Optional[str] = None) -> Tuple[bool, dict]:
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
            call.previous[vessel] = {k: st.get(k) for k in ("auto_open", "auto_close")}
            if want.get("auto_open") is True:
                if st.get("auto_close") != 1 and want.get("auto_close") is not True:
                    raise Refusal(f"LN2 {vessel}: auto-close is off — auto-open only works with "
                                  f"auto-close armed (include auto_close: true)")
                self._require_level_fresh(vessel)
            is_closed = st.get("state") == 0 and st.get("desired") == 0
            if want.get("auto_close") is False and not is_closed:
                raise Refusal(f"LN2 {vessel}: auto-close may only be turned off while the valve "
                              f"is closed — the PLC's fill timeout only runs with it")
            if (want.get("auto_close") is False and st.get("auto_open") != 0
                    and want.get("auto_open") is not False):
                raise Refusal(f"LN2 {vessel}: auto-open is on — turn it off first (or in the "
                              f"same call: auto_open false); auto-open only works with "
                              f"auto-close armed")
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
                call.noop = True
                call.detail = f"LN2 {vessel}: flags already as asked — nothing sent"
                call.note = "the flags are already as asked; nothing was sent"
                self._valve_readback(call, vessel, topic)
                return
            self._require_ln2_interval(vessel)
            self._take_write_slot(call)
            prefix = self.zones.vessels[vessel]["command_prefix"]
            if call.dry_run:
                for k, v in steps:
                    self._publish(call, f"{prefix}/{k}", {"enabled": bool(v)})
                call.detail = f"LN2 {vessel}: " + ", ".join(f"{k} {'on' if v else 'off'}"
                                                            for k, v in steps)
                self._valve_readback(call, vessel, topic)
                return
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
        return self._run("set_ln2_auto", args, body, client=client)

    # ── pneumatic ─────────────────────────────────────────────────────────

    def _note_pn_command(self, valve: str, want: int, ts: float, seq: int, *,
                         pending: bool) -> None:
        with self._stats_lock:
            self._pn_cmd[valve] = {"want": int(want), "ts": ts, "seq": seq, "pending": pending}

    def _pending_open_age(self, valve: str) -> Optional[float]:
        """Seconds since the AI's last command to ``valve`` when that was an
        open never confirmed (it may still be queued in slowcontrol and land
        later), else None.  Such an open stops counting once a status after it
        shows the valve open — the live check sees that — or after
        :data:`PN_PENDING_OPEN_S`."""
        with self._stats_lock:
            c = dict(self._pn_cmd[valve]) if valve in self._pn_cmd else None
        if not c or c["want"] != 1 or not c["pending"]:
            return None
        age = self._clock() - c["ts"]
        if age > PN_PENDING_OPEN_S:
            return None
        r = self.bus.raw(self.zones.valves[valve]["status_topic"])
        if r is not None and r["seq"] > c["seq"]:
            # The last one or two statuses after the command (not one from before).
            after = [r["payload"]] + ([r["prev_payload"]] if r["seq"] >= c["seq"] + 2 else [])
            if any(isinstance(p, dict) and (p.get("state") == 1 or p.get("desired") == 1)
                   for p in after):
                return None
        return max(age, 0.0)

    def set_pneumatic_valve(self, args: Any, client: Optional[str] = None) -> Tuple[bool, dict]:
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
                raise Refusal("the PLC's pneumatic valve gate is disabled (or the PLC driver is "
                              "not reporting it); it is a second, separate lock in front of the "
                              "gas valves")
            topic = self.zones.valves[valve]["status_topic"]
            st = self.bus.live_payload(topic, GATE_LIVE_S)
            if not isinstance(st, dict):
                raise Refusal(f"pneumatic {valve}: no live valve status from the PLC")
            # The pressure guard: never close the xenon's buffer with xenon
            # present; open the pump only below its limit.
            if not want_open and valve in ("ballast", "bottle"):
                self._require_guard("close_buffer_problem", valve)
            if want_open and valve == "pump":
                self._require_guard("pump_open_problem")
            if want_open:
                pairs = [(tuple(pr), "Lucas's rails forbid")
                         for pr in self.rails.pneumatic()["forbid_open_together"]]
                builtin = (self.guard.builtin_pairs() if self.guard is not None
                           else [("pump", "ballast"), ("pump", "bottle")])
                pairs += [(pr, "the pressure guard (guard.pump_open_with) forbids")
                          for pr in builtin]
                for pair, who in pairs:
                    if valve not in pair:
                        continue
                    partner = pair[1] if pair[0] == valve else pair[0]
                    pst = self.bus.live_payload(self.zones.valves[partner]["status_topic"],
                                                GATE_LIVE_S)
                    if not isinstance(pst, dict) or "state" not in pst:
                        raise Refusal(f"pneumatic {valve}: {who} {valve} and {partner} open "
                                      f"together, and {partner}'s state is not known (no live "
                                      f"status)")
                    if pst.get("state") == 1 or pst.get("desired") == 1:
                        raise Refusal(f"pneumatic {valve}: {partner} is open, and {who} "
                                      f"{valve} and {partner} open at the same time")
                    age = self._pending_open_age(partner)
                    if age is not None:
                        raise Refusal(f"pneumatic {valve}: an AI open of {partner} was sent "
                                      f"{age:.0f} s ago and is not confirmed — it may still be on "
                                      f"its way, and {who} {valve} and {partner} open together. "
                                      f"Close {partner} first, or wait until "
                                      f"{PN_PENDING_OPEN_S:.0f} s have passed")
            interval = float(self.rails.pneumatic()["min_interval_s"])
            now = self._clock()
            last = self._pn_last.get(valve)
            if last is not None and now - last < interval:
                raise Refusal(f"pneumatic {valve}: commanded {now - last:.0f} s ago; wait "
                              f"{interval - (now - last):.0f} s more (min_interval_s "
                              f"{interval:g})")
            # A close after the AI's own unconfirmed open is sent even though the
            # valve reads closed: that open may still be queued, and this close
            # is applied after it.
            unseen_open = not want_open and self._pending_open_age(valve) is not None
            if st.get("state") == want and st.get("desired") == want and not unseen_open:
                call.noop = True
                call.detail = f"pneumatic {valve} already {'open' if want else 'closed'}"
                call.note = f"{valve} is already {'open' if want else 'closed'}; nothing sent"
                call.readback[valve] = _status_copy(st, ("state", "desired", "relay"))
                return
            call.previous[valve] = {k: st.get(k) for k in ("state", "desired")}
            self._take_write_slot(call)
            lr0 = copy.deepcopy(gate.get("last_refused"))
            seq0 = self._seq(topic)
            t_pub = self._clock()
            if call.dry_run:
                self._publish(call, f"xsphere/commands/pneumatic/{valve}/state", {"state": want})
                call.detail = f"pneumatic {valve} → {'open' if want else 'closed'}"
                call.readback[valve] = _status_copy(st, ("state", "desired", "relay"))
                return
            self._pn_last[valve] = t_pub
            self._publish(call, f"xsphere/commands/pneumatic/{valve}/state", {"state": want})
            # Out now: pending until the status confirms it (or the PLC refuses it).
            self._note_pn_command(valve, want, t_pub, seq0, pending=True)
            word = "open" if want else "closed"
            call.detail = f"pneumatic {valve} → {word}" + (
                " (sent although it reads closed: the AI's open of it was never confirmed)"
                if unseen_open else "")

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
                self._note_pn_command(valve, want, t_pub, seq0, pending=False)
                raise Failure(f"pneumatic {valve}: the PLC refused the command — its pneumatic "
                              f"valve gate was disabled when it arrived")
            call.confirmed = self._wait_consecutive(checks, timeout=0)
            if call.confirmed:
                self._note_pn_command(valve, want, t_pub, seq0, pending=False)
            st2 = self._payload(topic) or {}
            call.readback[valve] = _status_copy(st2, ("state", "desired", "relay"))
            if not call.confirmed:
                call.warnings.append(f"not confirmed: {valve} did not read {word} on two "
                                     f"consecutive messages within {self.confirm_timeout_s:g} s"
                                     + ("" if "state" in st2 else " (the PLC did not report the "
                                        "relay state)"))
        return self._run("set_pneumatic_valve", args, body, client=client)

    # ── notes ─────────────────────────────────────────────────────────────

    def add_note(self, args: Any, client: Optional[str] = None) -> Tuple[bool, dict]:
        def body(call: _Call) -> None:
            self._take_note_slot(call)
            call.detail = call.args["text"].strip()
            call.note = ("would be noted in the AI logbook" if call.dry_run else
                         "noted in the AI logbook (audit), shown on the Control page card as "
                         f"a note by AI client {call.who}")
        return self._run("add_note", args, body, lock=False, client=client)


def _status_copy(status: dict, keys) -> dict:
    """``{key: value}`` from a status payload, its text values quoted «…»
    (they came from MQTT, where anything on the network can write)."""
    return {k: (untrusted(status.get(k), 120) if isinstance(status.get(k), str)
                else status.get(k)) for k in keys}


def _safe(cond: Callable[[dict], bool], payload: dict) -> bool:
    try:
        return bool(cond(payload))
    except Exception:
        return False
