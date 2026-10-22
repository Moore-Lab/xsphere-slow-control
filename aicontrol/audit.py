"""
The AI audit trail: every action, refusal, arm change, rails change and note.

Each entry goes three places:

* ``state_dir/audit.jsonl`` — one JSON object per line, appended and flushed
  per entry; at 5 MB the file is renamed to ``audit.jsonl.1`` (the older ones
  move up to ``.2`` … ``.5``, the oldest is dropped) and a new file started;
* a ring of the last 500 in memory — :meth:`Audit.recent` for the read tools
  (re-filled from the file's tail at start, so a restart keeps the history);
* the last 50, newest first, published retained on ``<status_topic>/audit``
  at most once a second — the Control page's "Recent AI activity" reads that
  from webcontrol's MQTT cache, so it needs no cross-origin request.

Entry: ``{"utc", "kind", "detail", ...}`` with optional ``tool``, ``args``,
``reason`` (the AI's own words), ``published``, ``confirmed``, ``by`` and any
other keyword the caller passes; None-valued fields are left out.  Values are
made strict-JSON safe (non-finite floats → null) and any key that looks like a
credential is redacted: nothing secret is ever written or published.
"""

from __future__ import annotations

import collections
import copy
import json
import logging
import math
import os
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Deque, Dict, List, Optional

log = logging.getLogger(__name__)

KINDS = ("action", "refused", "arm", "disarm", "auto_disarm", "rails", "rails_refused",
         "note", "ramp", "auto_close")

PUBLISH_LAST = 50
PUBLISH_MIN_INTERVAL_S = 1.0
#: Rotated files kept beside audit.jsonl (audit.jsonl.1 … .5, 5 MB each).
KEEP_ROTATED = 5
_MAX_STR = 4000
_SECRET_KEY_RE = re.compile(r"token|password|passwd|secret|authorization|api[_-]?key", re.I)


def _clean(value: Any, depth: int = 0) -> Any:
    """Strict-JSON-safe copy: non-finite floats → None, unknown types → str,
    long strings cut, credential-looking keys redacted."""
    if depth > 20:
        return "…"
    if value is None or isinstance(value, bool) or isinstance(value, int):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, str):
        return value if len(value) <= _MAX_STR else value[:_MAX_STR] + "…"
    if isinstance(value, dict):
        return {str(k): ("[redacted]" if _SECRET_KEY_RE.search(str(k)) else _clean(v, depth + 1))
                for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_clean(v, depth + 1) for v in value]
    return _clean(str(value), depth + 1)


def _utc_now_iso(ts: float) -> str:
    return (datetime.fromtimestamp(ts, tz=timezone.utc)
            .isoformat(timespec="milliseconds").replace("+00:00", "Z"))


class Audit:
    """Append-only audit log with an in-memory ring and a throttled retained publish."""

    def __init__(self, path, publish: Optional[Callable[[list], bool]] = None,
                 ring: int = 500, rotate_bytes: int = 5_000_000, *,
                 clock: Callable[[], float] = time.time, load_existing: bool = True):
        self.path = Path(path)
        self._publish = publish
        self._ring: Deque[dict] = collections.deque(maxlen=max(1, int(ring)))
        self._rotate_bytes = int(rotate_bytes)
        self._clock = clock
        self._lock = threading.Lock()          # ring + file
        self._pub_lock = threading.Lock()      # throttle state
        self._last_publish = float("-inf")     # time.monotonic() of the last publish attempt
        self._timer: Optional[threading.Timer] = None
        #: True while entries exist that have not been published successfully.
        self.pending = False
        if load_existing:
            self._load_tail()

    # writing -------------------------------------------------------------

    def record(self, kind: str, detail: str, **fields: Any) -> dict:
        """Append one entry; returns (a copy of) it.  Never raises on I/O trouble."""
        if kind not in KINDS:
            log.warning("Audit entry with unexpected kind %r", kind)
        entry: Dict[str, Any] = {"utc": _utc_now_iso(self._clock()), "kind": str(kind),
                                 "detail": _clean(str(detail))}
        for key, value in fields.items():
            if value is None or key in entry:
                continue
            entry[key] = "[redacted]" if _SECRET_KEY_RE.search(key) else _clean(value)
        line = json.dumps(entry, ensure_ascii=False, allow_nan=False)
        with self._lock:
            self._ring.append(entry)
            self._append(line)
        self.pending = True
        self.flush_publish()
        return copy.deepcopy(entry)

    def _append(self, line: str) -> None:
        # Never raises: an entry is written after its command went out, and
        # the caller must still get the call's true result.  Text that is not
        # valid UTF-8 (a lone surrogate) is written escaped, not dropped.
        data = line + "\n"
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            try:
                size = self.path.stat().st_size
            except FileNotFoundError:
                size = 0
            if size and size + len(data.encode("utf-8", "backslashreplace")) > self._rotate_bytes:
                self._rotate()
            with open(self.path, "a", encoding="utf-8", errors="backslashreplace") as f:
                f.write(data)
                f.flush()
        except Exception as e:                       # noqa: BLE001
            log.warning("Could not append to the audit file %s: %s", self.path, e)

    def _rotate(self) -> None:
        """audit.jsonl → .1 → .2 … → .KEEP_ROTATED (the oldest dropped), so a
        burst of entries cannot push the history off disk at once."""
        for i in range(KEEP_ROTATED - 1, 0, -1):
            older = self.path.with_name(f"{self.path.name}.{i}")
            if older.exists():
                os.replace(older, self.path.with_name(f"{self.path.name}.{i + 1}"))
        os.replace(self.path, self.path.with_name(self.path.name + ".1"))

    def _load_tail(self) -> None:
        try:
            with open(self.path, encoding="utf-8", errors="replace") as f:
                # Twice the ring, so a few torn / foreign lines do not cost entries.
                lines = collections.deque(f, maxlen=2 * self._ring.maxlen)
        except FileNotFoundError:
            return
        except OSError as e:
            log.warning("Could not read the audit file %s: %s", self.path, e)
            return
        for line in lines:
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if isinstance(entry, dict):
                self._ring.append(entry)
        if self._ring:
            log.info("Audit: %d earlier entries loaded from %s", len(self._ring), self.path)

    # reading -------------------------------------------------------------

    def recent(self, n: int = 20) -> List[dict]:
        """The last ``n`` entries, newest first (deep copies)."""
        if n <= 0:
            return []
        with self._lock:
            items = list(self._ring)[-n:]
        return copy.deepcopy(items[::-1])

    # publishing ----------------------------------------------------------

    def flush_publish(self, force: bool = False) -> None:
        """Publish the last 50 entries (retained), at most once per second.

        Inside the 1 s window a timer publishes once the window ends, so the
        newest entry always goes out.  ``force`` skips the throttle (use it on
        a reconnect).  A failed publish leaves :attr:`pending` set; call again
        (the status loop / connect hook) to retry.
        """
        if self._publish is None:
            return
        with self._pub_lock:
            now = time.monotonic()
            wait = self._last_publish + PUBLISH_MIN_INTERVAL_S - now
            if wait > 0 and not force:
                if self._timer is None:
                    self._timer = threading.Timer(wait, self._timer_fired)
                    self._timer.daemon = True
                    self._timer.start()
                return
            self._last_publish = now
        entries = self.recent(PUBLISH_LAST)
        try:
            ok = bool(self._publish(entries))
        except Exception:
            log.exception("Audit publish failed")
            ok = False
        if ok:
            self.pending = False

    def _timer_fired(self) -> None:
        with self._pub_lock:
            self._timer = None
        self.flush_publish()

    def close(self) -> None:
        with self._pub_lock:
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None
