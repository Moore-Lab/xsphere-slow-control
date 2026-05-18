"""
StateStore — the centralized proprioception state holder.

Subscribes to every source topic named in ``state.yaml``, keeps the latest
value (and, for analog states, a ring buffer for moving averages), recomputes
freshness and the derived expressions on a ~1 Hz tick, and republishes a
consolidated snapshot (retained) on ``xsphere/state/snapshot``.

It does not poll hardware and does not actuate anything — it only listens to
the broker and publishes its consolidated view.  Controllers may hold a
reference and read ``get(id)`` / ``snapshot()``; the webcontrol GUI consumes the
retained snapshot topic.

See ``slowcontrol/STATE_LAYER_PLAN.md`` for the contract.
"""

from __future__ import annotations

import ast
import logging
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Deque, Dict, List, Optional, Tuple

from slowcontrol.state.schema import Freshness, StateDef, StateSchema

log = logging.getLogger(__name__)

#: MQTT topic the consolidated snapshot is published on (retained).
SNAPSHOT_TOPIC = "xsphere/state/snapshot"

#: Default tick period (seconds) — how often freshness/derived/averages are
#: recomputed and the snapshot is republished.
DEFAULT_TICK_S = 1.0

#: Ordering used to take the "worst" freshness across a derived state's inputs.
_FRESHNESS_RANK = {Freshness.FRESH: 0, Freshness.STALE: 1, Freshness.INVALID: 2}
_RANK_FRESHNESS = {v: k for k, v in _FRESHNESS_RANK.items()}

#: Strings recognised as true/false when coercing a binary state's value.
_TRUE_STRINGS = {"1", "true", "on", "yes", "open", "energized", "energised"}
_FALSE_STRINGS = {"0", "false", "off", "no", "closed", "de-energized", "de-energised", ""}


# ---------------------------------------------------------------------------
# Entry — the raw record kept per state
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Entry:
    """The latest value of a state, plus when/where it came from.

    ``freshness`` is *not* stored here — it depends on "now" — use
    :meth:`StateStore.get` / :meth:`StateStore.snapshot`, which compute it.
    """

    value: Any
    ts_wall: float          # epoch seconds (time.time)
    ts_mono: float          # monotonic seconds (time.monotonic) — used for age
    source: str             # the topic it came from, or "derived"
    seq: int


# ---------------------------------------------------------------------------
# Safe arithmetic evaluator for derived `expr`s
# ---------------------------------------------------------------------------

_ALLOWED_BINOPS = (ast.Add, ast.Sub, ast.Mult, ast.Div)
_ALLOWED_UNARYOPS = (ast.UAdd, ast.USub)


def _compile_expr(expr: str) -> ast.Expression:
    """Parse and validate a derived expression; return the AST.

    Only ``+ - * /``, parentheses, unary ``+``/``-``, numeric literals and bare
    names (state ids) are allowed.  Raises ``ValueError`` on anything else.
    """
    tree = ast.parse(expr, mode="eval")

    def check(node: ast.AST) -> None:
        if isinstance(node, ast.Expression):
            check(node.body)
        elif isinstance(node, ast.BinOp) and isinstance(node.op, _ALLOWED_BINOPS):
            check(node.left)
            check(node.right)
        elif isinstance(node, ast.UnaryOp) and isinstance(node.op, _ALLOWED_UNARYOPS):
            check(node.operand)
        elif isinstance(node, ast.Name):
            return
        elif isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) \
                and not isinstance(node.value, bool):
            return
        else:
            raise ValueError(f"disallowed element in expression: {ast.dump(node)}")

    check(tree)
    return tree


def _eval_expr(tree: ast.Expression, names: Dict[str, float]) -> float:
    def ev(node: ast.AST) -> float:
        if isinstance(node, ast.Expression):
            return ev(node.body)
        if isinstance(node, ast.BinOp):
            a, b = ev(node.left), ev(node.right)
            if isinstance(node.op, ast.Add):
                return a + b
            if isinstance(node.op, ast.Sub):
                return a - b
            if isinstance(node.op, ast.Mult):
                return a * b
            return a / b                                 # Div (only remaining)
        if isinstance(node, ast.UnaryOp):
            v = ev(node.operand)
            return +v if isinstance(node.op, ast.UAdd) else -v
        if isinstance(node, ast.Name):
            return names[node.id]                        # KeyError ⇒ caller handles
        if isinstance(node, ast.Constant):
            return float(node.value)
        raise ValueError("unreachable")                  # pragma: no cover

    return ev(tree)


# ---------------------------------------------------------------------------
# Value coercion
# ---------------------------------------------------------------------------

def _coerce(kind: str, raw: Any) -> Any:
    """Coerce a raw payload value to the canonical type for ``kind``.

    Raises ``ValueError``/``TypeError`` if it can't — the caller skips the
    update and the state ages toward STALE.
    """
    if kind == "binary":
        if isinstance(raw, bool):
            return raw
        if isinstance(raw, (int, float)):
            return raw != 0
        s = str(raw).strip().lower()
        if s in _TRUE_STRINGS:
            return True
        if s in _FALSE_STRINGS:
            return False
        raise ValueError(f"not a recognised boolean: {raw!r}")
    if kind == "analog":
        if isinstance(raw, bool):
            raise ValueError("bool is not an analog value")
        return float(raw)
    return str(raw)                                      # enum / text


def _payload_value(payload: Any, keys: Tuple[str, ...]) -> Any:
    """Pull the value out of a decoded MQTT payload per a state's ``keys``.

    ``keys`` empty ⇒ the payload itself is the scalar.  Otherwise the first key
    present (with a non-None value) wins.  Raises ``KeyError`` if none match.
    """
    if not keys:
        return payload
    if isinstance(payload, dict):
        for k in keys:
            if k in payload and payload[k] is not None:
                return payload[k]
    raise KeyError(keys)


def _window_key(w: float) -> str:
    """Snapshot key for a moving-average window, e.g. 60.0 -> "avg_60s"."""
    iw = int(w)
    return f"avg_{iw if iw == w else w}s"


# ---------------------------------------------------------------------------
# StateStore
# ---------------------------------------------------------------------------

class StateStore:
    """Holds and republishes the system's consolidated state."""

    NAME = "state_store"

    def __init__(self, config, mqtt, schema: StateSchema, *,
                 tick_s: float = DEFAULT_TICK_S,
                 snapshot_topic: str = SNAPSHOT_TOPIC) -> None:
        self._config = config
        self._mqtt = mqtt
        self._schema = schema
        self._tick_s = float(tick_s)
        self._snapshot_topic = snapshot_topic

        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._seq = 0

        self._entries: Dict[str, Entry] = {}                 # state id -> latest Entry
        self._topic_seen: Dict[str, float] = {}              # topic -> last receipt (monotonic)
        self._buffers: Dict[str, Deque[Tuple[float, float]]] = {}  # state id -> (t_mono, value)

        # Precomputed lookups
        self._by_id: Dict[str, StateDef] = schema.by_id()
        self._topic_states: Dict[str, List[StateDef]] = {}
        self._presence_topics: set = set()
        self._derived_ast: Dict[str, ast.Expression] = {}
        self._max_window: Dict[str, float] = {}

        for s in schema.states:
            if s.avg_windows_s:
                self._max_window[s.id] = max(s.avg_windows_s)
                self._buffers[s.id] = deque()
            if s.source.is_derived:
                self._derived_ast[s.id] = _compile_expr(s.source.expr)
            elif s.source.presence:
                self._presence_topics.add(s.source.topic)
            else:
                self._topic_states.setdefault(s.source.topic, []).append(s)

        self._derived_order: List[StateDef] = self._sort_derived(schema.derived_states())

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        topics = sorted(set(self._topic_states) | self._presence_topics)
        for topic in topics:
            self._mqtt.subscribe(topic, self._on_message)
        self._stop.clear()
        self._thread = threading.Thread(target=self._tick_loop,
                                        name="state-store", daemon=True)
        self._thread.start()
        log.info("[state_store] started — %d states, %d source topics, tick %.1fs",
                 len(self._schema.states), len(topics), self._tick_s)

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=10)
        log.info("[state_store] stopped")

    @property
    def schema(self) -> StateSchema:
        return self._schema

    # ------------------------------------------------------------------
    # MQTT ingestion
    # ------------------------------------------------------------------

    def _on_message(self, topic: str, payload: Any) -> None:
        now_mono = time.monotonic()
        now_wall = time.time()
        if topic in self._presence_topics:
            with self._lock:
                self._topic_seen[topic] = now_mono

        for sdef in self._topic_states.get(topic, ()):
            try:
                raw = _payload_value(payload, sdef.source.keys)
                value = _coerce(sdef.kind, raw)
            except (KeyError, ValueError, TypeError) as exc:
                log.debug("[state_store] %s: ignoring payload on %s (%s)",
                          sdef.id, topic, exc)
                continue
            if sdef.kind == "enum" and sdef.values and value not in sdef.values:
                log.debug("[state_store] %s: enum value %r not in %s",
                          sdef.id, value, list(sdef.values))
            with self._lock:
                self._seq += 1
                self._entries[sdef.id] = Entry(value=value, ts_wall=now_wall,
                                               ts_mono=now_mono, source=topic,
                                               seq=self._seq)
                buf = self._buffers.get(sdef.id)
                if buf is not None:
                    buf.append((now_mono, float(value)))
                    self._prune_locked(sdef.id, buf, now_mono)

    def _prune_locked(self, sid: str, buf: Deque[Tuple[float, float]],
                      now_mono: float) -> None:
        horizon = now_mono - self._max_window.get(sid, 0.0) - 2.0 * self._tick_s
        while buf and buf[0][0] < horizon:
            buf.popleft()

    # ------------------------------------------------------------------
    # Tick: recompute + publish
    # ------------------------------------------------------------------

    def _tick_loop(self) -> None:
        # Publish an initial (mostly-INVALID) snapshot promptly so the GUI has
        # the registry to render from before any data arrives.
        try:
            self._publish_snapshot()
        except Exception:                                # noqa: BLE001
            log.exception("[state_store] error publishing initial snapshot")
        while not self._stop.wait(self._tick_s):
            try:
                self._publish_snapshot()
            except Exception:                            # noqa: BLE001
                log.exception("[state_store] error during tick")

    def _publish_snapshot(self) -> None:
        snap = self._build_snapshot(time.monotonic(), time.time(), feed_derived=True)
        self._mqtt.publish(self._snapshot_topic, snap, qos=1, retain=True)

    def _freshness(self, sdef: StateDef, age_s: Optional[float]) -> Freshness:
        if age_s is None:
            return Freshness.INVALID
        if sdef.period_s is None:
            return Freshness.FRESH
        if age_s <= sdef.stale_factor * sdef.period_s:
            return Freshness.FRESH
        if age_s <= sdef.invalid_factor * sdef.period_s:
            return Freshness.STALE
        return Freshness.INVALID

    def _build_snapshot(self, now_mono: float, now_wall: float, *,
                        feed_derived: bool) -> dict:
        with self._lock:
            entries = dict(self._entries)
            topic_seen = dict(self._topic_seen)
            buffers: Dict[str, List[Tuple[float, float]]] = {
                sid: list(buf) for sid, buf in self._buffers.items()
            }

        def avg_for(sid: str, windows: Tuple[float, ...]) -> Dict[str, Optional[float]]:
            out: Dict[str, Optional[float]] = {}
            buf = buffers.get(sid, [])
            for w in windows:
                cutoff = now_mono - w
                vals = [v for (t, v) in buf if t >= cutoff]
                out[_window_key(w)] = (sum(vals) / len(vals)) if vals else None
            return out

        views: Dict[str, dict] = {}
        cur_num: Dict[str, float] = {}                   # for the derived evaluator
        cur_fresh: Dict[str, Freshness] = {}

        # 1. Direct (MQTT-sourced) states.
        for sdef in self._schema.states:
            if sdef.source.is_derived:
                continue
            if sdef.source.presence:
                seen = topic_seen.get(sdef.source.topic)
                age = (now_mono - seen) if seen is not None else None
                fresh = self._freshness(sdef, age)
                views[sdef.id] = self._view_dict(
                    sdef, value=(fresh == Freshness.FRESH), freshness=fresh,
                    ts_wall=(now_wall - age) if age is not None else None,
                    age_s=age, source=sdef.source.topic, seq=None)
                cur_fresh[sdef.id] = fresh
                continue
            ent = entries.get(sdef.id)
            if ent is None:
                views[sdef.id] = self._view_dict(sdef, value=None,
                                                 freshness=Freshness.INVALID,
                                                 ts_wall=None, age_s=None,
                                                 source=None, seq=None)
                cur_fresh[sdef.id] = Freshness.INVALID
                continue
            age = now_mono - ent.ts_mono
            fresh = self._freshness(sdef, age)
            v = self._view_dict(sdef, value=ent.value, freshness=fresh,
                                ts_wall=ent.ts_wall, age_s=age,
                                source=ent.source, seq=ent.seq)
            if sdef.avg_windows_s:
                v["avg"] = avg_for(sdef.id, sdef.avg_windows_s)
            views[sdef.id] = v
            cur_fresh[sdef.id] = fresh
            if sdef.kind == "analog" and isinstance(ent.value, (int, float)) \
                    and not isinstance(ent.value, bool):
                cur_num[sdef.id] = float(ent.value)

        # 2. Derived states, in dependency order.
        for sdef in self._derived_order:
            refs = sdef.source.expr_refs()
            try:
                value = _eval_expr(self._derived_ast[sdef.id],
                                   {r: cur_num[r] for r in refs})
            except (KeyError, ZeroDivisionError, ValueError, ArithmeticError) as exc:
                if not isinstance(exc, KeyError):
                    log.debug("[state_store] derived %s: eval failed (%s)", sdef.id, exc)
                views[sdef.id] = self._view_dict(sdef, value=None,
                                                 freshness=Freshness.INVALID,
                                                 ts_wall=None, age_s=None,
                                                 source="derived", seq=None)
                cur_fresh[sdef.id] = Freshness.INVALID
                continue
            worst_rank = max((_FRESHNESS_RANK[cur_fresh.get(r, Freshness.INVALID)]
                              for r in refs), default=_FRESHNESS_RANK[Freshness.FRESH])
            fresh = _RANK_FRESHNESS[worst_rank]
            buf = self._buffers.get(sdef.id)
            if buf is not None:
                if feed_derived:
                    with self._lock:
                        buf.append((now_mono, value))
                        self._prune_locked(sdef.id, buf, now_mono)
                buffers.setdefault(sdef.id, []).append((now_mono, value))
            v = self._view_dict(sdef, value=value, freshness=fresh,
                                ts_wall=now_wall, age_s=0.0, source="derived", seq=None)
            if sdef.avg_windows_s:
                v["avg"] = avg_for(sdef.id, sdef.avg_windows_s)
            views[sdef.id] = v
            cur_fresh[sdef.id] = fresh
            cur_num[sdef.id] = value

        counts = {"fresh": 0, "stale": 0, "invalid": 0}
        for f in cur_fresh.values():
            counts[f.value] += 1

        return {
            "generated_at": round(now_wall, 3),
            "counts": counts,
            "states": {s.id: views[s.id] for s in self._schema.states},
        }

    # ------------------------------------------------------------------
    # View construction (one entry of the snapshot / get())
    # ------------------------------------------------------------------

    @staticmethod
    def _view_dict(sdef: StateDef, *, value: Any, freshness: Freshness,
                   ts_wall: Optional[float], age_s: Optional[float],
                   source: Optional[str], seq: Optional[int]) -> dict:
        d: dict = {
            "id": sdef.id,
            "group": sdef.group,
            "kind": sdef.kind,
            "label": sdef.label,
            "unit": sdef.unit,
            "value": value,
            "freshness": freshness.value,
            "ts": (round(ts_wall, 3) if ts_wall is not None else None),
            "age_s": (round(age_s, 3) if age_s is not None else None),
            "source": source,
            "seq": seq,
        }
        if sdef.kind == "enum":
            d["values"] = list(sdef.values)
        if sdef.control is not None:
            d["control"] = {"topic": sdef.control.topic,
                            "payload": dict(sdef.control.payload)}
        return d

    # ------------------------------------------------------------------
    # Read API (for in-process consumers)
    # ------------------------------------------------------------------

    def get(self, state_id: str) -> Optional[dict]:
        """Return the current view dict for one state, or ``None`` if unknown."""
        if state_id not in self._by_id:
            return None
        return self.snapshot()["states"].get(state_id)

    def get_entry(self, state_id: str) -> Optional[Entry]:
        """Return the raw latest ``Entry`` for one state (no freshness)."""
        with self._lock:
            return self._entries.get(state_id)

    def snapshot(self) -> dict:
        """Build a fresh snapshot now (same shape as the published one).

        Read-only: it does not feed derived values into the averaging buffers
        (only the periodic tick does that), so calling it often is harmless.
        """
        return self._build_snapshot(time.monotonic(), time.time(), feed_derived=False)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _sort_derived(self, derived: List[StateDef]) -> List[StateDef]:
        """Order derived states so each comes after the derived states it
        references (non-derived refs are always available).  Cycles ⇒ the
        offending states are left in declaration order (they'll read INVALID).
        """
        by_id = {s.id: s for s in derived}
        ordered: List[StateDef] = []
        placed: set = set()

        def visit(s: StateDef, stack: set) -> None:
            if s.id in placed:
                return
            if s.id in stack:
                log.warning("[state_store] derived-state cycle through %s — "
                            "leaving in declaration order", s.id)
                return
            stack.add(s.id)
            for r in s.source.expr_refs():
                if r in by_id:
                    visit(by_id[r], stack)
            stack.discard(s.id)
            if s.id not in placed:
                ordered.append(s)
                placed.add(s.id)

        for s in derived:
            visit(s, set())
        for s in derived:                               # stragglers from cycles
            if s.id not in placed:
                ordered.append(s)
                placed.add(s.id)
        return ordered
