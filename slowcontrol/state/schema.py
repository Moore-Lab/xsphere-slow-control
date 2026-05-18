"""
State registry — the schema half of the proprioception layer.

`state.yaml` declares every state the system tracks (see ``STATE_LAYER_PLAN.md``
for the field reference).  This module parses that file into typed ``StateDef``
objects collected in a ``StateSchema``.  The ``StateStore`` (added in build
step 2) consumes the result; nothing here touches MQTT or hardware.

Run as a script to validate / dump the registry::

    python -m slowcontrol.state.schema [path/to/state.yaml]
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from enum import Enum
from typing import Dict, List, Mapping, Optional, Sequence, Set, Tuple

import yaml


#: Permitted ``kind`` values for a state.
KINDS: Tuple[str, ...] = ("analog", "binary", "enum", "text")

#: Identifier pattern used both for state ids and for references inside a
#: derived state's ``expr``.
_ID_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

#: Hard-coded fallbacks, layered *under* the YAML ``defaults:`` block, which is
#: layered under each entry's own fields.
_TOP_DEFAULTS: Mapping[str, float] = {"stale_factor": 1.5, "invalid_factor": 5.0}
_KIND_DEFAULTS: Mapping[str, Mapping[str, object]] = {
    "analog": {"period_s": 1.0, "avg_windows_s": (60.0,)},
    "binary": {"period_s": 5.0, "avg_windows_s": ()},
    "enum":   {"period_s": 5.0, "avg_windows_s": ()},
    "text":   {"period_s": 5.0, "avg_windows_s": ()},
}


class Freshness(str, Enum):
    """How much to trust a state's current value.

    ``FRESH``   — updated within ``stale_factor * period_s``.
    ``STALE``   — older than that, but within ``invalid_factor * period_s``.
    ``INVALID`` — older still, or never seen, or (for a derived state) any input
    is itself invalid.
    """

    FRESH = "fresh"
    STALE = "stale"
    INVALID = "invalid"


class SchemaError(ValueError):
    """``state.yaml`` is missing or malformed."""


@dataclass(frozen=True)
class SourceSpec:
    """Where a state's value comes from.

    Exactly one shape is used:

    * MQTT-sourced — ``topic`` set, plus either ``keys`` (ordered fallbacks
      into the JSON payload; the first present key wins; empty ⇒ the payload
      itself is the scalar) or ``presence=True`` (a *binary* state that is
      ``True`` iff a message arrived on ``topic`` within ``period_s``).
    * Derived — ``expr`` set: restricted arithmetic over other state ids.
    """

    topic: Optional[str] = None
    keys: Tuple[str, ...] = ()
    presence: bool = False
    expr: Optional[str] = None

    @property
    def is_derived(self) -> bool:
        return self.expr is not None

    def expr_refs(self) -> Tuple[str, ...]:
        """State ids referenced by ``expr`` (empty if not derived)."""
        if self.expr is None:
            return ()
        return tuple(dict.fromkeys(_ID_RE.findall(self.expr)))


@dataclass(frozen=True)
class ControlSpec:
    """How to actuate a state.

    Publishing the command means sending ``payload`` to ``topic`` with the
    template strings substituted: ``"$value"`` → the operator's input as-is
    (bool/number/str), ``"$value01"`` → ``int(bool(input))``.
    """

    topic: str
    payload: Mapping[str, object]


@dataclass(frozen=True)
class StateDef:
    """One tracked state, as declared in ``state.yaml``."""

    id: str
    kind: str
    group: str
    label: str
    source: SourceSpec
    unit: Optional[str] = None
    control: Optional[ControlSpec] = None
    period_s: Optional[float] = None      # None ⇒ never self-stales
    stale_factor: float = 1.5
    invalid_factor: float = 5.0
    avg_windows_s: Tuple[float, ...] = ()
    values: Tuple[str, ...] = ()          # allowed values; ``kind == "enum"`` only

    @property
    def controllable(self) -> bool:
        return self.control is not None

    @property
    def derived(self) -> bool:
        return self.source.is_derived


@dataclass(frozen=True)
class StateSchema:
    """The whole registry — an ordered tuple of ``StateDef`` plus lookups."""

    states: Tuple[StateDef, ...]

    def by_id(self) -> Dict[str, StateDef]:
        return {s.id: s for s in self.states}

    def by_group(self) -> "Dict[str, List[StateDef]]":
        out: Dict[str, List[StateDef]] = {}
        for s in self.states:
            out.setdefault(s.group, []).append(s)
        return out

    def subscribe_topics(self) -> Set[str]:
        """Distinct MQTT topics the store must subscribe to."""
        return {s.source.topic for s in self.states if s.source.topic is not None}

    def derived_states(self) -> List[StateDef]:
        return [s for s in self.states if s.source.is_derived]

    def controllable_states(self) -> List[StateDef]:
        return [s for s in self.states if s.controllable]


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------

def _as_float_tuple(v: object) -> Tuple[float, ...]:
    if v is None:
        return ()
    if isinstance(v, (int, float)):
        return (float(v),)
    if isinstance(v, (list, tuple)):
        return tuple(float(x) for x in v)
    raise SchemaError(f"expected a number or list of numbers, got {v!r}")


def _pick(name: str, entry: Mapping, kind_defaults: Mapping, hard_fallback: object) -> object:
    if name in entry:
        return entry[name]
    if name in kind_defaults:
        return kind_defaults[name]
    return hard_fallback


def _parse_source(sid: str, entry: Mapping, *, is_derived: bool) -> SourceSpec:
    if is_derived:
        expr = entry.get("expr")
        if not isinstance(expr, str) or not expr.strip():
            raise SchemaError(f"derived state {sid!r}: needs a non-empty `expr`")
        if "source" in entry:
            raise SchemaError(f"derived state {sid!r}: use `expr`, not `source`")
        return SourceSpec(expr=expr.strip())

    src = entry.get("source")
    if not isinstance(src, Mapping):
        raise SchemaError(f"state {sid!r}: needs a `source` mapping")
    topic = src.get("topic")
    if not isinstance(topic, str) or not topic:
        raise SchemaError(f"state {sid!r}: `source.topic` is required")
    presence = bool(src.get("presence", False))
    keys: Tuple[str, ...] = ()
    if "keys" in src and "key" in src:
        raise SchemaError(f"state {sid!r}: give `key` or `keys`, not both")
    if "keys" in src:
        kk = src["keys"]
        if not isinstance(kk, (list, tuple)) or not kk:
            raise SchemaError(f"state {sid!r}: `source.keys` must be a non-empty list")
        keys = tuple(str(k) for k in kk)
    elif "key" in src:
        keys = (str(src["key"]),)
    if presence and keys:
        raise SchemaError(f"state {sid!r}: `presence` and `key`/`keys` are mutually exclusive")
    return SourceSpec(topic=topic, keys=keys, presence=presence)


def _parse_control(sid: str, entry: Mapping) -> Optional[ControlSpec]:
    craw = entry.get("control")
    if craw is None:
        return None
    if not isinstance(craw, Mapping):
        raise SchemaError(f"state {sid!r}: `control` must be a mapping")
    topic = craw.get("topic")
    if not isinstance(topic, str) or not topic:
        raise SchemaError(f"state {sid!r}: `control.topic` is required")
    payload = craw.get("payload", {})
    if not isinstance(payload, Mapping):
        raise SchemaError(f"state {sid!r}: `control.payload` must be a mapping")
    return ControlSpec(topic=topic, payload=dict(payload))


def _build_state(*, group: str, sid: str, entry: object,
                 yaml_defaults: Mapping, is_derived: bool) -> StateDef:
    if not isinstance(entry, Mapping):
        raise SchemaError(f"state {sid!r}: entry must be a mapping")

    kind = entry.get("kind", "analog")
    if kind not in KINDS:
        raise SchemaError(f"state {sid!r}: `kind` must be one of {KINDS}, got {kind!r}")
    kind_defaults = dict(_KIND_DEFAULTS[kind])
    kind_defaults.update(yaml_defaults.get(kind, {}) or {})

    source = _parse_source(sid, entry, is_derived=is_derived)
    control = _parse_control(sid, entry)
    if control is not None and is_derived:
        raise SchemaError(f"derived state {sid!r}: cannot be `control`-able")

    values: Tuple[str, ...] = ()
    if kind == "enum":
        vs = entry.get("values")
        if not isinstance(vs, (list, tuple)) or not vs:
            raise SchemaError(f"enum state {sid!r}: needs a non-empty `values` list")
        values = tuple(str(v) for v in vs)

    period_raw = _pick("period_s", entry, kind_defaults, None)
    period_s = None if period_raw is None else float(period_raw)
    stale_factor = float(_pick("stale_factor", entry, kind_defaults, _TOP_DEFAULTS["stale_factor"]))
    invalid_factor = float(_pick("invalid_factor", entry, kind_defaults, _TOP_DEFAULTS["invalid_factor"]))
    if invalid_factor < stale_factor:
        raise SchemaError(f"state {sid!r}: invalid_factor < stale_factor")
    avg_windows_s = _as_float_tuple(_pick("avg_windows_s", entry, kind_defaults, ()))
    if kind != "analog" and avg_windows_s:
        raise SchemaError(f"state {sid!r}: `avg_windows_s` is only valid for analog states")

    return StateDef(
        id=sid,
        kind=kind,
        group=group,
        label=str(entry.get("label", sid)),
        source=source,
        unit=(None if entry.get("unit") is None else str(entry["unit"])),
        control=control,
        period_s=period_s,
        stale_factor=stale_factor,
        invalid_factor=invalid_factor,
        avg_windows_s=avg_windows_s,
        values=values,
    )


def load_state_schema(path: str) -> StateSchema:
    """Parse ``state.yaml`` at *path*; raise ``SchemaError`` if it is malformed."""
    if not os.path.exists(path):
        raise SchemaError(f"state schema file not found: {path}")
    with open(path) as fh:
        raw = yaml.safe_load(fh) or {}
    if not isinstance(raw, Mapping):
        raise SchemaError(f"{path}: top level must be a mapping")

    yaml_defaults = raw.get("defaults", {}) or {}
    if not isinstance(yaml_defaults, Mapping):
        raise SchemaError(f"{path}: `defaults` must be a mapping")
    groups = raw.get("groups", {}) or {}
    derived = raw.get("derived", {}) or {}
    if not isinstance(groups, Mapping) or not isinstance(derived, Mapping):
        raise SchemaError(f"{path}: `groups` and `derived` must be mappings")

    states: List[StateDef] = []
    seen: Set[str] = set()

    def _add(group: str, sid: str, entry: object, is_derived: bool) -> None:
        if not _ID_RE.fullmatch(sid):
            raise SchemaError(f"state id {sid!r}: must match {_ID_RE.pattern}")
        if sid in seen:
            raise SchemaError(f"duplicate state id {sid!r}")
        states.append(_build_state(group=group, sid=sid, entry=entry,
                                   yaml_defaults=yaml_defaults, is_derived=is_derived))
        seen.add(sid)

    for group, entries in groups.items():
        if not isinstance(entries, Mapping):
            raise SchemaError(f"group {group!r}: must be a mapping of state entries")
        for sid, entry in entries.items():
            _add(str(group), str(sid), entry, is_derived=False)

    for sid, entry in derived.items():
        _add("derived", str(sid), entry, is_derived=True)

    if not states:
        raise SchemaError(f"{path}: no states declared")

    # Validate that derived `expr`s only reference known state ids.
    known = {s.id for s in states}
    for s in states:
        if s.source.is_derived:
            unknown = [r for r in s.source.expr_refs() if r not in known]
            if unknown:
                raise SchemaError(
                    f"derived state {s.id!r}: `expr` references unknown id(s): {unknown}")

    # Validate enum source values fall inside the declared set (best-effort: we
    # can't check live data, but we can check it's a sane non-empty enum above).
    return StateSchema(states=tuple(states))


def default_schema_path(config_path: Optional[str] = None) -> str:
    """Best guess at where ``state.yaml`` lives.

    Prefer a sibling of *config_path* (the service is launched with
    ``-c slowcontrol/config.yaml``); otherwise fall back to the package's own
    ``slowcontrol/state.yaml``.
    """
    if config_path:
        cand = os.path.join(os.path.dirname(os.path.abspath(config_path)), "state.yaml")
        if os.path.exists(cand):
            return cand
    return os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "state.yaml"))


# ---------------------------------------------------------------------------
# CLI: validate / dump
# ---------------------------------------------------------------------------

def _main(argv: Sequence[str]) -> int:
    path = argv[1] if len(argv) > 1 else default_schema_path()
    try:
        schema = load_state_schema(path)
    except SchemaError as exc:
        print(f"INVALID  {path}\n  {exc}")
        return 1

    print(f"OK  {path}  —  {len(schema.states)} states "
          f"({len(schema.controllable_states())} controllable, "
          f"{len(schema.derived_states())} derived), "
          f"{len(schema.subscribe_topics())} source topics\n")
    for group, defs in schema.by_group().items():
        print(f"[{group}]")
        for d in defs:
            src = f"={d.source.expr}" if d.source.is_derived else (
                "(presence)" if d.source.presence else f"{d.source.topic}"
                + (f" .{'/'.join(d.source.keys)}" if d.source.keys else ""))
            per = "period=∞" if d.period_s is None else f"period={d.period_s:g}s"
            avg = f" avg={[int(w) for w in d.avg_windows_s]}s" if d.avg_windows_s else ""
            ctl = "  ✎" if d.controllable else ""
            unit = f"[{d.unit}]" if d.unit else ""
            print(f"  {d.id:26s} {d.kind:7s} {unit:8s} {per}{avg}{ctl}\n"
                  f"  {'':26s} {'':7s} {'':8s} ← {src}")
        print()
    return 0


if __name__ == "__main__":
    import sys

    raise SystemExit(_main(sys.argv))
