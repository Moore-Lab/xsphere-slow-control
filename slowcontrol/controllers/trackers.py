"""
Tracker controller — keep one state's value tied to another (with an offset).

A *tracker* reads a source state and writes a target state on every tick:

    target_value = source_value + offset      # clamped to [min_value, max_value]

The target must be a ``control:``-annotated state in ``state.yaml`` (so we
know which command topic to publish and how to template the payload).  The
source must be an analog state with a current FRESH or STALE value (we skip
the write if the source is INVALID — never propagate stale-derived setpoints).

Trackers persist to ``slowcontrol/trackers.json`` so they survive a service
restart.  Add / remove / enable via MQTT (the control GUI's "Trackers" card
sends these); status is published on ``xsphere/status/trackers`` retained for
the GUI to render.

Typical use::

    target  pid_top_setpoint   (a control:-annotated analog state)
    source  t_cube_bottom      (any analog state)
    offset  10.0               → keeps the top zone's setpoint 10 K above the
                                  bottom-cube RTD reading.

MQTT interface
──────────────
Subscribe (commands):
  xsphere/commands/trackers/set      {"id":..., "target":..., "source":...,
                                      "offset":<f>, "enabled":<bool>,
                                      "min_value":<f>?, "max_value":<f>?}   upsert
  xsphere/commands/trackers/remove   {"id":...}                              delete
  xsphere/commands/trackers/enable   {"id":..., "enabled":<bool>}            toggle

Subscribe (state):
  xsphere/state/snapshot                                                     latest values

Publish (status, retained):
  xsphere/status/trackers   [{"id":..., "target":..., "source":..., "offset":...,
                              "enabled":..., "last_sent":..., "last_error":..., ...}]
"""

from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from slowcontrol.controllers.base import Controller

log = logging.getLogger(__name__)

#: How often the controller re-evaluates and (when relevant) re-publishes setpoints.
TICK_S = 1.0

#: A write is suppressed when the new value differs from the last sent by ≤ this.
#: Prevents hammering the PLC with effectively-identical setpoints every tick.
DEADBAND = 0.005


@dataclass
class Tracker:
    id: str
    target: str
    source: str
    offset: float = 0.0
    enabled: bool = True
    min_value: Optional[float] = None
    max_value: Optional[float] = None
    last_sent: Optional[float] = None
    last_error: Optional[str] = None


def _default_persistence_path() -> str:
    """``slowcontrol/trackers.json`` next to ``state.yaml``."""
    return os.path.normpath(os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "..", "trackers.json"))


class TrackerController(Controller):
    NAME = "trackers"

    def __init__(self, config, mqtt, *, persistence_path: Optional[str] = None):
        super().__init__(config, mqtt)
        self._path = persistence_path or _default_persistence_path()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._trackers: Dict[str, Tracker] = {}
        self._snapshot: dict = {}

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        self._load()
        self._mqtt.subscribe("xsphere/state/snapshot",          self._on_snapshot)
        self._mqtt.subscribe("xsphere/commands/trackers/set",   self._on_set)
        self._mqtt.subscribe("xsphere/commands/trackers/remove", self._on_remove)
        self._mqtt.subscribe("xsphere/commands/trackers/enable", self._on_enable)
        self._stop.clear()
        self._thread = threading.Thread(target=self._tick_loop,
                                        name="trackers", daemon=True)
        self._thread.start()
        log.info("[trackers] started (%d known, persistence %s)",
                 len(self._trackers), self._path)
        self._publish_status()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=10)
        log.info("[trackers] stopped")

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
            log.warning("[trackers] cannot load %s: %s", self._path, exc)
            return
        if not isinstance(data, list):
            log.warning("[trackers] %s: expected a JSON list, got %s",
                        self._path, type(data).__name__)
            return
        with self._lock:
            for d in data:
                try:
                    t = Tracker(
                        id=str(d["id"]),
                        target=str(d["target"]),
                        source=str(d["source"]),
                        offset=float(d.get("offset", 0.0)),
                        enabled=bool(d.get("enabled", True)),
                        min_value=(None if d.get("min_value") is None else float(d["min_value"])),
                        max_value=(None if d.get("max_value") is None else float(d["max_value"])),
                    )
                    self._trackers[t.id] = t
                except (KeyError, ValueError, TypeError) as exc:
                    log.warning("[trackers] skipping malformed entry: %s (%s)", exc, d)

    def _save(self) -> None:
        with self._lock:
            data = [
                {
                    "id": t.id, "target": t.target, "source": t.source,
                    "offset": t.offset, "enabled": t.enabled,
                    "min_value": t.min_value, "max_value": t.max_value,
                }
                for t in self._trackers.values()
            ]
        try:
            tmp = self._path + ".tmp"
            with open(tmp, "w") as fh:
                json.dump(data, fh, indent=2)
            os.replace(tmp, self._path)
        except OSError as exc:
            log.warning("[trackers] cannot persist to %s: %s", self._path, exc)

    # ------------------------------------------------------------------
    # MQTT inputs
    # ------------------------------------------------------------------

    def _on_snapshot(self, topic: str, payload: Any) -> None:
        if isinstance(payload, dict):
            self._snapshot = payload

    def _on_set(self, topic: str, payload: Any) -> None:
        if not isinstance(payload, dict):
            return
        try:
            tid = str(payload["id"]).strip()
            target = str(payload["target"]).strip()
            source = str(payload["source"]).strip()
            offset = float(payload.get("offset", 0.0))
            enabled = bool(payload.get("enabled", True))
            min_value = payload.get("min_value")
            max_value = payload.get("max_value")
            min_value = None if min_value is None else float(min_value)
            max_value = None if max_value is None else float(max_value)
        except (KeyError, ValueError, TypeError) as exc:
            log.warning("[trackers] bad set payload (%s): %s", exc, payload)
            return
        if not tid or not target or not source:
            log.warning("[trackers] set: id/target/source must all be non-empty")
            return
        with self._lock:
            existing = self._trackers.get(tid)
            self._trackers[tid] = Tracker(
                id=tid, target=target, source=source,
                offset=offset, enabled=enabled,
                min_value=min_value, max_value=max_value,
                last_sent=existing.last_sent if existing else None,
            )
        log.info("[trackers] set %s:  %s = %s + %.4f  (enabled=%s)",
                 tid, target, source, offset, enabled)
        self._save()
        self._publish_status()

    def _on_remove(self, topic: str, payload: Any) -> None:
        if not isinstance(payload, dict):
            return
        tid = str(payload.get("id", "")).strip()
        with self._lock:
            removed = self._trackers.pop(tid, None)
        if removed is None:
            return
        log.info("[trackers] removed %s", tid)
        self._save()
        self._publish_status()

    def _on_enable(self, topic: str, payload: Any) -> None:
        if not isinstance(payload, dict):
            return
        tid = str(payload.get("id", "")).strip()
        enabled = bool(payload.get("enabled", False))
        with self._lock:
            t = self._trackers.get(tid)
            if t is None:
                return
            t.enabled = enabled
            t.last_error = None        # give it a fresh chance
        log.info("[trackers] %s enabled → %s", tid, enabled)
        self._save()
        self._publish_status()

    # ------------------------------------------------------------------
    # Tick loop
    # ------------------------------------------------------------------

    def _tick_loop(self) -> None:
        # First tick after a short delay so the state snapshot has a chance to arrive.
        if not self._stop.wait(0.5):
            self._safe_evaluate()
        while not self._stop.wait(TICK_S):
            self._safe_evaluate()

    def _safe_evaluate(self) -> None:
        try:
            self._evaluate()
        except Exception:                                  # noqa: BLE001
            log.exception("[trackers] error during evaluation")

    def _evaluate(self) -> None:
        snap = self._snapshot
        if not snap:
            return
        states = snap.get("states") or {}
        with self._lock:
            trackers = list(self._trackers.values())
        any_change = False
        for t in trackers:
            err: Optional[str] = None
            new_val: Optional[float] = None
            if not t.enabled:
                if t.last_error not in (None, "disabled"):
                    t.last_error = None
                    any_change = True
                continue
            src = states.get(t.source)
            tgt = states.get(t.target)
            if src is None:
                err = f"unknown source state {t.source!r}"
            elif tgt is None:
                err = f"unknown target state {t.target!r}"
            elif src.get("kind") != "analog":
                err = f"source {t.source!r} is not analog"
            elif "control" not in tgt:
                err = f"target {t.target!r} has no control mapping"
            elif src.get("freshness") == "invalid":
                err = f"source {t.source!r} is invalid"
            elif src.get("value") is None:
                err = f"source {t.source!r} has no value yet"
            else:
                try:
                    new_val = float(src["value"]) + t.offset
                except (TypeError, ValueError) as exc:
                    err = f"value arithmetic failed: {exc}"
            if err is None and new_val is not None:
                if t.min_value is not None and new_val < t.min_value:
                    new_val = t.min_value
                if t.max_value is not None and new_val > t.max_value:
                    new_val = t.max_value

            if err is not None:
                if t.last_error != err:
                    log.warning("[trackers] %s skipped: %s", t.id, err)
                    t.last_error = err
                    any_change = True
                continue
            if (t.last_sent is not None
                    and abs(new_val - t.last_sent) <= DEADBAND):
                # value hasn't moved enough; just clear any stale error mark
                if t.last_error is not None:
                    t.last_error = None
                    any_change = True
                continue
            ctl = tgt["control"]
            try:
                out_payload = _build_payload(ctl["payload"], new_val)
                self._mqtt.publish(ctl["topic"], out_payload, qos=1)
            except Exception as exc:                       # noqa: BLE001
                log.warning("[trackers] %s publish failed: %s", t.id, exc)
                t.last_error = f"publish failed: {exc}"
                any_change = True
                continue
            t.last_sent = new_val
            t.last_error = None
            any_change = True
        if any_change:
            self._publish_status()

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------

    def _publish_status(self) -> None:
        with self._lock:
            data: List[dict] = [
                {
                    "id": t.id, "target": t.target, "source": t.source,
                    "offset": t.offset, "enabled": t.enabled,
                    "min_value": t.min_value, "max_value": t.max_value,
                    "last_sent": (None if t.last_sent is None else round(t.last_sent, 4)),
                    "last_error": t.last_error,
                }
                for t in self._trackers.values()
            ]
        self._mqtt.publish_status("trackers", payload=data, retain=True)


def _build_payload(template: Dict[str, Any], value: float) -> Dict[str, Any]:
    """Substitute "$value" / "$value01" in the control payload template."""
    out: Dict[str, Any] = {}
    for k, v in template.items():
        if v == "$value":
            out[k] = value
        elif v == "$value01":
            out[k] = 1 if value else 0
        else:
            out[k] = v
    return out
