"""
Sequencer controller — run an ordered list of "items" against the system.

Each *item* in the program is one of:

* **Step** — a one-shot set of actions plus a hold time.  Actions are either
  ``{"type":"set",   "target":"<state-id>", "value":<scalar>}``      — direct
  one-shot write to the target's command topic (target must be a
  ``control:``-annotated state in ``state.yaml``); or
  ``{"type":"track", "target":"<state-id>", "source":"<state-id>",
                     "offset":<f>}``                                  — creates
  or replaces a *sequencer-owned* tracker (id ``seq:<target>``) that keeps
  ``target = source + offset`` while this step is current.  When the next
  step is entered, sequencer-owned trackers not present in that step's
  track actions are automatically removed.

* **Sweep** — a compact representation of "scan one target from start to
  stop in steps of `step`, dwelling `dwell_s` at each value".  Expanded
  inline at run time: for each generated value the controller writes
  ``target = value`` (via the target's control mapping) and holds for
  ``dwell_s``.  Replaces the old `GradientScannerPlugin` with a more general
  "scan any analog control-state".

Operation
─────────
- The program is persisted to ``slowcontrol/sequencer.json`` so it survives
  a service restart.
- Add / clear / run / stop via MQTT (the Sequencer page sends these).
- Status (running flag, current item index, sub-step index for sweeps,
  remaining seconds, the program itself) is published on
  ``xsphere/status/sequencer`` retained.

MQTT interface
──────────────
Subscribe (commands):
  xsphere/commands/sequencer/set     {"steps":[ <item>, ... ]}                replace program
  xsphere/commands/sequencer/append  {"step":   <item> }                      append one item
  xsphere/commands/sequencer/clear   {}                                       clear (idle only)
  xsphere/commands/sequencer/run     {}                                       start from item 0
  xsphere/commands/sequencer/stop    {}                                       stop after current sub-step

Item JSON
─────────
  Step:   {"type":"step",  "label":..., "hold_s":<s>,
           "actions":[ {"type":"set",   "target":..., "value":...},
                       {"type":"track", "target":..., "source":..., "offset":<f>}, ... ]}
  Sweep:  {"type":"sweep", "label":..., "target":<state-id>,
           "start":<f>, "stop":<f>, "step":<f>, "dwell_s":<s>}

Subscribe (state):
  xsphere/state/snapshot   — latest state.yaml view, used to look up each
                             target's ``control`` mapping for set/sweep actions.

Publish (status, retained):
  xsphere/status/sequencer  {"running":<bool>,
                             "current_step":<idx|null>,
                             "sub_step":<idx|null>, "sub_step_total":<int|null>,
                             "step_started_at":<ts|null>, "step_ends_at":<ts|null>,
                             "now":<ts>, "last_message":<str>, "steps":[<item>, ...]}
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set, Union

from slowcontrol.controllers.base import Controller

log = logging.getLogger(__name__)

#: Prefix on every tracker the sequencer creates, so we can find and clean them up.
_TRACK_PREFIX = "seq:"

#: How often we republish status (so the GUI's "remaining time" ticks).
STATUS_TICK_S = 1.0


# ---------------------------------------------------------------------------
# Item types
# ---------------------------------------------------------------------------

@dataclass
class Step:
    label: str
    hold_s: float
    actions: List[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "type": "step",
            "label": self.label,
            "hold_s": self.hold_s,
            "actions": list(self.actions),
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Step":
        try:
            hold = float(d.get("hold_s", 0))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"step.hold_s not numeric: {d.get('hold_s')!r}") from exc
        if hold < 0:
            raise ValueError(f"step.hold_s must be >= 0, got {hold}")
        raw_actions = d.get("actions", [])
        if not isinstance(raw_actions, list):
            raise ValueError("step.actions must be a list")
        actions: List[dict] = []
        for a in raw_actions:
            if not isinstance(a, dict):
                raise ValueError(f"action must be a dict, got {type(a).__name__}")
            t = a.get("type")
            if t == "set":
                if not isinstance(a.get("target"), str) or not a["target"]:
                    raise ValueError("set action: 'target' required")
                if "value" not in a:
                    raise ValueError("set action: 'value' required")
                actions.append({"type": "set", "target": a["target"], "value": a["value"]})
            elif t == "track":
                for f in ("target", "source"):
                    if not isinstance(a.get(f), str) or not a[f]:
                        raise ValueError(f"track action: {f!r} required")
                actions.append({
                    "type": "track",
                    "target": a["target"],
                    "source": a["source"],
                    "offset": float(a.get("offset", 0.0)),
                })
            else:
                raise ValueError(f"unknown action type: {t!r}")
        return cls(label=str(d.get("label", "")), hold_s=hold, actions=actions)


@dataclass
class Sweep:
    label: str
    target: str
    start: float
    stop: float
    step: float
    dwell_s: float

    def to_dict(self) -> dict:
        return {
            "type": "sweep",
            "label": self.label,
            "target": self.target,
            "start": self.start,
            "stop": self.stop,
            "step": self.step,
            "dwell_s": self.dwell_s,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Sweep":
        target = d.get("target")
        if not isinstance(target, str) or not target:
            raise ValueError("sweep: 'target' is required")
        try:
            start = float(d["start"])
            stop = float(d["stop"])
            step = float(d["step"])
            dwell = float(d.get("dwell_s", 0))
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"sweep: start/stop/step/dwell_s must be numeric ({exc})") from exc
        if dwell < 0:
            raise ValueError(f"sweep.dwell_s must be >= 0, got {dwell}")
        if step == 0 and start != stop:
            raise ValueError("sweep.step == 0 with start != stop would never finish")
        return cls(label=str(d.get("label", "")), target=target,
                   start=start, stop=stop, step=step, dwell_s=dwell)

    def values(self) -> List[float]:
        """Generate the ordered list of setpoints (inclusive of start and stop)."""
        if self.step == 0 or self.start == self.stop:
            return [self.start]
        vs: List[float] = []
        sp = self.start
        # Tolerance to include `stop` even when it doesn't divide evenly.
        eps = abs(self.step) * 1e-9 + 1e-9
        while ((self.step > 0 and sp <= self.stop + eps)
               or (self.step < 0 and sp >= self.stop - eps)):
            vs.append(round(sp, 6))
            sp += self.step
        return vs


Item = Union[Step, Sweep]


def _parse_item(d: Any) -> Item:
    if not isinstance(d, dict):
        raise ValueError(f"item must be a dict, got {type(d).__name__}")
    t = d.get("type", "step")
    if t == "step":
        return Step.from_dict(d)
    if t == "sweep":
        return Sweep.from_dict(d)
    raise ValueError(f"unknown item type: {t!r}")


def _default_persistence_path() -> str:
    return os.path.normpath(os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "..", "sequencer.json"))


# ---------------------------------------------------------------------------
# Controller
# ---------------------------------------------------------------------------

class SequencerController(Controller):
    NAME = "sequencer"

    def __init__(self, config, mqtt, *, persistence_path: Optional[str] = None):
        super().__init__(config, mqtt)
        self._path = persistence_path or _default_persistence_path()
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._abort_event = threading.Event()
        self._status_thread: Optional[threading.Thread] = None
        self._runner_thread: Optional[threading.Thread] = None

        self._items: List[Item] = []
        self._snapshot: dict = {}

        # Run state — all only meaningful while running.
        self._running: bool = False
        self._current_step: Optional[int] = None
        self._sub_step: Optional[int] = None
        self._sub_step_total: Optional[int] = None
        self._step_started_at: Optional[float] = None
        self._step_ends_at: Optional[float] = None
        self._last_message: str = "idle"
        # Sequencer-owned trackers active *right now* (only meaningful while running).
        self._run_active_trackers: Set[str] = set()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        self._load()
        self._mqtt.subscribe("xsphere/state/snapshot",            self._on_snapshot)
        self._mqtt.subscribe("xsphere/commands/sequencer/set",    self._on_set)
        self._mqtt.subscribe("xsphere/commands/sequencer/append", self._on_append)
        self._mqtt.subscribe("xsphere/commands/sequencer/clear",  self._on_clear)
        self._mqtt.subscribe("xsphere/commands/sequencer/run",    self._on_run)
        self._mqtt.subscribe("xsphere/commands/sequencer/stop",   self._on_stop)
        self._stop_event.clear()
        self._status_thread = threading.Thread(target=self._status_loop,
                                               name="sequencer-status", daemon=True)
        self._status_thread.start()
        log.info("[sequencer] started (%d items in program, persistence %s)",
                 len(self._items), self._path)
        self._publish_status()

    def stop(self) -> None:
        self._stop_event.set()
        self._abort_event.set()
        if self._runner_thread is not None:
            self._runner_thread.join(timeout=10)
        if self._status_thread is not None:
            self._status_thread.join(timeout=10)
        log.info("[sequencer] stopped")

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _load(self) -> None:
        if not os.path.exists(self._path):
            return
        try:
            with open(self._path) as fh:
                data = json.load(fh)
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("[sequencer] cannot load %s: %s", self._path, exc)
            return
        if not isinstance(data, dict) or not isinstance(data.get("steps"), list):
            log.warning("[sequencer] %s: expected {steps: [...]}", self._path)
            return
        items: List[Item] = []
        for raw in data["steps"]:
            try:
                items.append(_parse_item(raw))
            except (ValueError, TypeError) as exc:
                log.warning("[sequencer] skipping malformed item: %s", exc)
        with self._lock:
            self._items = items

    def _save(self) -> None:
        with self._lock:
            data = {"steps": [it.to_dict() for it in self._items]}
        try:
            tmp = self._path + ".tmp"
            with open(tmp, "w") as fh:
                json.dump(data, fh, indent=2)
            os.replace(tmp, self._path)
        except OSError as exc:
            log.warning("[sequencer] cannot persist to %s: %s", self._path, exc)

    # ------------------------------------------------------------------
    # MQTT inputs
    # ------------------------------------------------------------------

    def _on_snapshot(self, topic: str, payload: Any) -> None:
        if isinstance(payload, dict):
            self._snapshot = payload

    def _on_set(self, topic: str, payload: Any) -> None:
        if not isinstance(payload, dict):
            return
        if self._running:
            log.warning("[sequencer] set rejected: sequence is running")
            return
        raw = payload.get("steps")
        if not isinstance(raw, list):
            log.warning("[sequencer] set: 'steps' must be a list")
            return
        items: List[Item] = []
        for entry in raw:
            try:
                items.append(_parse_item(entry))
            except (ValueError, TypeError) as exc:
                log.warning("[sequencer] set: rejecting malformed item (%s)", exc)
                return
        with self._lock:
            self._items = items
            self._last_message = f"program loaded ({len(items)} items)"
        log.info("[sequencer] program set: %d items", len(items))
        self._save()
        self._publish_status()

    def _on_append(self, topic: str, payload: Any) -> None:
        if not isinstance(payload, dict):
            return
        if self._running:
            log.warning("[sequencer] append rejected: sequence is running")
            return
        try:
            item = _parse_item(payload.get("step", {}))
        except (ValueError, TypeError) as exc:
            log.warning("[sequencer] append: rejecting item (%s)", exc)
            return
        with self._lock:
            self._items.append(item)
            self._last_message = f"appended item ({len(self._items)} total)"
        log.info("[sequencer] item appended (%d total)", len(self._items))
        self._save()
        self._publish_status()

    def _on_clear(self, topic: str, payload: Any) -> None:
        if self._running:
            log.warning("[sequencer] clear rejected: sequence is running")
            return
        with self._lock:
            self._items = []
            self._last_message = "program cleared"
        log.info("[sequencer] program cleared")
        self._save()
        self._publish_status()

    def _on_run(self, topic: str, payload: Any) -> None:
        if self._running:
            log.info("[sequencer] run ignored: already running")
            return
        with self._lock:
            if not self._items:
                self._last_message = "cannot run: program is empty"
                log.warning("[sequencer] %s", self._last_message)
                self._publish_status()
                return
            items = list(self._items)
        self._abort_event.clear()
        self._runner_thread = threading.Thread(
            target=self._run_sequence, args=(items,),
            name="sequencer-runner", daemon=True)
        self._runner_thread.start()

    def _on_stop(self, topic: str, payload: Any) -> None:
        if not self._running:
            return
        log.info("[sequencer] stop requested")
        self._abort_event.set()

    # ------------------------------------------------------------------
    # Runner thread
    # ------------------------------------------------------------------

    def _run_sequence(self, items: List[Item]) -> None:
        with self._lock:
            self._running = True
            self._current_step = None
            self._sub_step = None
            self._sub_step_total = None
            self._step_started_at = None
            self._step_ends_at = None
            self._last_message = "running"
            # A new run starts with no record of seq-owned trackers; any pre-
            # existing seq trackers from a previous run that aren't re-asserted
            # by this run's first track-step will be left alone.
            self._run_active_trackers = set()
        log.info("[sequencer] starting sequence (%d items)", len(items))
        self._publish_status()
        try:
            for idx, item in enumerate(items):
                if self._abort_event.is_set() or self._stop_event.is_set():
                    break
                if isinstance(item, Step):
                    self._run_step(idx, item)
                elif isinstance(item, Sweep):
                    self._run_sweep(idx, item)
                if self._abort_event.is_set() or self._stop_event.is_set():
                    break
        finally:
            stopped_early = self._abort_event.is_set() or self._stop_event.is_set()
            with self._lock:
                self._running = False
                self._current_step = None
                self._sub_step = None
                self._sub_step_total = None
                self._step_started_at = None
                self._step_ends_at = None
                self._last_message = "stopped" if stopped_early else "done"
                self._run_active_trackers.clear()
            log.info("[sequencer] sequence %s", "stopped" if stopped_early else "complete")
            self._publish_status()

    # ---- Step ---------------------------------------------------------

    def _run_step(self, idx: int, step: Step) -> None:
        now = time.time()
        label = step.label or f"step {idx + 1}"
        with self._lock:
            self._current_step = idx
            self._sub_step = None
            self._sub_step_total = None
            self._step_started_at = now
            self._step_ends_at = now + step.hold_s
            self._last_message = f"item {idx + 1}/{len(self._items) or '?'}: {label}"
        log.info("[sequencer] %s", self._last_message)
        self._apply_step_actions(step)
        self._publish_status()
        self._wait_until(self._step_ends_at)

    def _apply_step_actions(self, step: Step) -> None:
        # Reconcile sequencer-owned trackers: this step's track actions become
        # the authoritative set; sequencer-owned trackers active from a prior
        # step (in THIS run) that aren't requested by this step are removed.
        wanted: Dict[str, dict] = {}
        for a in step.actions:
            if a["type"] == "track":
                tid = _TRACK_PREFIX + a["target"]
                wanted[tid] = {
                    "id": tid, "target": a["target"],
                    "source": a["source"], "offset": float(a["offset"]),
                    "enabled": True,
                }
        with self._lock:
            prev = set(self._run_active_trackers)
        for tid in prev - set(wanted):
            self._mqtt.publish("xsphere/commands/trackers/remove",
                               {"id": tid}, qos=1)
        for spec in wanted.values():
            self._mqtt.publish("xsphere/commands/trackers/set", spec, qos=1)
        with self._lock:
            self._run_active_trackers = set(wanted)

        # One-shot constant writes.
        for a in step.actions:
            if a["type"] != "set":
                continue
            self._write_set(a["target"], a["value"])

    # ---- Sweep --------------------------------------------------------

    def _run_sweep(self, idx: int, sweep: Sweep) -> None:
        values = sweep.values()
        label = sweep.label or (
            f"sweep {sweep.target} {sweep.start}→{sweep.stop} step {sweep.step}")
        log.info("[sequencer] item %d/%d: %s (%d points, dwell %.0fs)",
                 idx + 1, len(self._items), label, len(values), sweep.dwell_s)
        for sub_idx, v in enumerate(values):
            if self._abort_event.is_set() or self._stop_event.is_set():
                break
            now = time.time()
            with self._lock:
                self._current_step = idx
                self._sub_step = sub_idx
                self._sub_step_total = len(values)
                self._step_started_at = now
                self._step_ends_at = now + sweep.dwell_s
                self._last_message = (
                    f"item {idx + 1}/{len(self._items) or '?'}: {label} "
                    f"— point {sub_idx + 1}/{len(values)}: {sweep.target} = {v}")
            log.info("[sequencer] %s", self._last_message)
            self._write_set(sweep.target, v)
            self._publish_status()
            self._wait_until(self._step_ends_at)

    # ---- Wait / write helpers ----------------------------------------

    def _wait_until(self, deadline_ts: Optional[float]) -> None:
        """Sleep until ``deadline_ts`` (wall-clock seconds), aborting promptly
        on stop.  ``None`` ⇒ no wait."""
        if deadline_ts is None:
            return
        while True:
            now = time.time()
            remaining = deadline_ts - now
            if remaining <= 0:
                return
            if self._abort_event.wait(min(1.0, remaining + 0.01)):
                return
            if self._stop_event.is_set():
                return

    def _write_set(self, target: str, value: Any) -> None:
        states = (self._snapshot or {}).get("states") or {}
        tgt = states.get(target)
        if tgt is None or "control" not in tgt:
            log.warning("[sequencer] skipping set: target %r has no control mapping",
                        target)
            return
        ctl = tgt["control"]
        try:
            payload = _build_payload(ctl["payload"], value)
        except Exception as exc:                              # noqa: BLE001
            log.warning("[sequencer] skipping set %s: %s", target, exc)
            return
        self._mqtt.publish(ctl["topic"], payload, qos=1)

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------

    def _status_loop(self) -> None:
        while not self._stop_event.wait(STATUS_TICK_S):
            if self._running:
                self._publish_status()

    def _publish_status(self) -> None:
        with self._lock:
            data = {
                "running": self._running,
                "current_step": self._current_step,
                "sub_step": self._sub_step,
                "sub_step_total": self._sub_step_total,
                "step_started_at": self._step_started_at,
                "step_ends_at": self._step_ends_at,
                "now": time.time(),
                "last_message": self._last_message,
                "steps": [it.to_dict() for it in self._items],
            }
        self._mqtt.publish_status("sequencer", payload=data, retain=True)


def _build_payload(template: Dict[str, Any], value: Any) -> Dict[str, Any]:
    """Substitute ``$value`` / ``$value01`` placeholders in a control payload."""
    out: Dict[str, Any] = {}
    for k, v in template.items():
        if v == "$value":
            out[k] = value
        elif v == "$value01":
            out[k] = 1 if value else 0
        else:
            out[k] = v
    return out
