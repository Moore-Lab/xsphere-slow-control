"""
History — the AI's read-only window onto InfluxDB.

``query_history`` lets Claude look back at what a state did ("how fast did the
top clamp cool overnight?", "when did XV3 last open?") without being handed
Flux.  That restriction is the point of this module: the InfluxDB token on the
Pi can write, and even a read-only token can reach Flux's ``http.post`` and
``mqtt.to``, which would publish straight onto ``xsphere/commands/#`` and
bypass every arm switch and rail in this service.  So nothing the AI sends is
ever spliced into a query as text:

* measurement, topic and field come only from a map built at start-up from
  ``slowcontrol/state.yaml`` (which topic + key a state reads) and
  ``telegraf/telegraf.conf`` (which topic patterns Telegraf stores, under which
  measurement, with which json_v2 fields).  Every identifier in that map must
  match ``^[A-Za-z0-9_/+.-]+$`` — no quote, backslash or ``${`` can reach the
  script — and :func:`build_flux` checks them again;
* times are parsed into datetimes and re-serialised as ``%Y-%m-%dT%H:%M:%SZ``;
  durations become a whole number of seconds; the aggregate is picked from a
  fixed set.

Mapping rule (``understand/history.md`` §5): a state with ``source: {topic: T,
key: K}`` is stored iff some ``[[inputs.mqtt_consumer]]`` has a topic pattern
matching T segment-wise (``+`` = one segment, equal length) and a json_v2 field
whose ``path`` is K.  Its series is then ``_measurement == <measurement_name>
and topic == T and _field == K``.  ``keys: [filtered, raw]`` states keep every
stored candidate in registry order and merge them per window, preferring the
first — the same "first present key wins" the StateStore applies per message
(the ESP32 vessels only send ``raw``; the cryostat sends both).  Derived
states, presence states, scalar payloads and topics Telegraf does not ingest
(gradient, PID mode / expressions, interlock, NGE supply level, ...) have no
history and are listed with the reason.

One HTTP round trip per call: one Flux script with one pipeline per id, each
ending ``|> yield(name: "s<i>")``, answered as annotated CSV and demultiplexed
on the ``result`` column.  An error that happens after streaming started
arrives with HTTP 200 as a table with an ``error`` column; that is treated as a
failure, never as partial data.

"changes" (state transitions) is done here in Python, not in Flux: the query
asks for ``last`` per window and this module keeps only the windows whose value
differs from the window before (the first window is always kept: it is the
state at the start).  Its resolution is the window, ``every_s`` (1 s — the
native publish period — when the range is short enough, coarser for long
ranges so the response stays well under the 5 MB read cap); a flip and flip
back inside one window is invisible.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import math
import re
import threading
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

import yaml

log = logging.getLogger(__name__)

TOOL = "query_history"

#: The ``xsphere`` bucket was created 2026-05-11 16:48 UTC; nothing older exists
#: under this schema (the legacy Node-RED buckets are not queried).
HISTORY_START = datetime(2026, 5, 11, tzinfo=timezone.utc)
#: Longest range one call may cover.
MAX_RANGE = timedelta(days=90)
MAX_IDS = 8
DEFAULT_MAX_POINTS = 200
MAX_POINTS_PER_SERIES = 1000
#: Total points one call returns (protocol review §8: keeps the result far
#: below Claude Code's 25k-token tool-output cap).
MAX_TOTAL_POINTS = 2000
#: "changes" fetches one row per window per field before filtering; this caps
#: the rows one call may ask InfluxDB for (≈ 40 B each → ≈ 3 MB < the 5 MB cap).
CHANGES_ROW_BUDGET = 80_000
HTTP_TIMEOUT_S = 20.0
MAX_RESPONSE_BYTES = 5 * 1024 * 1024
MAX_CONCURRENT_QUERIES = 2
MAX_QUERIES_PER_MINUTE = 30

#: Every identifier spliced into Flux must match this (protocol review §11).
IDENT_RE = re.compile(r"[A-Za-z0-9_/+.\-]+")
#: Tag names are used as ``r.<tag>`` — a plain Flux identifier.
_TAG_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

_UNIT_S = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}
_REL_RE = re.compile(r"-([1-9][0-9]*)([smhdw])")
_EVERY_RE = re.compile(r"([1-9][0-9]*)([smhdw])")
_RFC3339_RE = re.compile(r"([0-9]{4})-([0-9]{2})-([0-9]{2})T([0-9]{2}):([0-9]{2}):([0-9]{2})"
                         r"(?:\.([0-9]{1,9}))?(Z|[+-][0-9]{2}:[0-9]{2})")

#: Telegraf json_v2 field type → InfluxDB column type.
_TELEGRAF_TYPES = {"float": "double", "int": "long", "uint": "unsignedLong",
                   "string": "string", "bool": "boolean"}

FNS = ("mean", "min", "max", "last", "count", "changes")
_FLUX_FNS = ("mean", "min", "max", "last", "count")
#: Aggregates allowed per field type: ``mean``/``min``/``max`` fail on strings.
FNS_BY_TYPE = {
    "double": FNS, "long": FNS, "unsignedLong": FNS,
    "string": ("last", "count", "changes"),
    "boolean": ("last", "count", "changes"),
    "unknown": ("last", "count", "changes"),
}

#: Window lengths the automatic ``every`` rounds up to, so the AI sees 2 min
#: rather than 109 s.
_NICE_STEPS_S = (1, 2, 5, 10, 15, 20, 30, 60, 120, 300, 600, 900, 1200, 1800,
                 3600, 7200, 10800, 21600, 43200, 86400)

_ARG_KEYS = ("ids", "start", "stop", "every", "fn", "max_points")

TIMES_NOTE = ("Times are UTC (Lucas works in US Eastern, America/New_York).  "
              "Point i is the window starting at t0 + dt_s[i], "
              "every_s long.  Windows with no data are left out: a gap in dt_s means "
              "nothing was recorded (device unreachable or service down), not zero.")

#: JSON schema for the tool registry (tools.py may use it as is).
QUERY_HISTORY_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "ids": {"type": "array", "items": {"type": "string"}, "minItems": 1,
                "maxItems": MAX_IDS, "uniqueItems": True,
                "description": "State ids (as in list_states) to fetch, at most 8."},
        "start": {"type": "string",
                  "description": 'Relative like "-6h" (s, m, h, d, w) or RFC3339 with an '
                                 'explicit offset, e.g. "2026-10-21T14:00:00-04:00" or '
                                 '"2026-10-21T18:00:00Z".'},
        "stop": {"type": "string",
                 "description": 'Same forms as start, or "now" (default).'},
        "every": {"type": "string",
                  "description": 'Window length like "30s", "5m", "1h"; raised '
                                 'automatically to stay within max_points.'},
        "fn": {"type": "string", "enum": list(FNS),
               "description": "Aggregate per window. Default: mean for numbers, last for "
                              "0/1 states and text. 'changes' returns only the windows "
                              "where the value changed."},
        "max_points": {"type": "integer", "minimum": 1, "maximum": MAX_POINTS_PER_SERIES,
                       "description": "Points per series (default 200; at most 2000 in "
                                      "total per call)."},
    },
    "required": ["ids", "start"],
    "additionalProperties": False,
}

QUERY_HISTORY_DESCRIPTION = (
    "Fetch the recorded history (InfluxDB) of up to 8 states, downsampled into windows. "
    "Read-only, no arm needed. Ids are the state ids from list_states; only states "
    "Telegraf stores have history (sensors, PID setpoint/PV/output, valve and pneumatic "
    "states, NGE readbacks, turbos) - PID mode/expressions, gradient settings, interlock "
    "band and derived states do not; get_guide topic 'history' lists them. History "
    "starts 2026-05-11; one call covers at most 90 days. start/stop: relative like "
    "'-6h' or RFC3339 with an explicit offset; all output times are UTC. fn: mean "
    "(default for numbers; for a 0/1 state it is the fraction of the window it was 1), "
    "min, max, last (default for 0/1 states and text), count (samples per window - "
    "reveals gaps), changes (only the windows where the value changed: valve "
    "open/close times; resolution = every_s). Result per series: t0 plus integer "
    "offsets dt_s and values v. Zone names are software names (see get_overview); "
    "topics pin hardware channels."
)

HISTORY_GUIDE = """\
## History (query_history)
- Source: InfluxDB bucket `xsphere`, written by Telegraf from the MQTT topics.
  Timestamps are Telegraf receive time, UTC. Data starts 2026-05-11.
- A state has history only if Telegraf stores its topic + key; query_history
  refuses the others and says why. Not stored: PID mode / setpoint & PV expressions,
  gradient settings, PV-interlock band and trip, NGE mode/trip/connected/hold,
  turbo link/mode, service heartbeat presence, all derived states (query their
  operands instead, e.g. t_cube_bottom and t_cube_top for dt_vertical_meas).
- pid_* history is stored under the SOFTWARE zone name (xsphere/status/pid/<zone>):
  if the top/bottom naming is ever swapped on a deploy, older pid_* history under a
  name belongs to the other physical clamp. Temperature history follows the
  hardware channel in each series' `topic`, which does not move.
- Windows: every_s is chosen so each series has at most max_points points; a
  missing window means no data (device off-line), not zero.
- 0/1 states: fn=changes gives each transition (to the window), fn=max shows
  whether it was ever 1 in a window, fn=mean the fraction of time it was 1.
"""


class HistoryError(Exception):
    """A query that could not be answered; the message is shown to the AI."""


# ---------------------------------------------------------------------------
# Map: registry id → (measurement, topic, field, type)
# ---------------------------------------------------------------------------

def _topic_matches(pattern: str, topic: str) -> bool:
    """MQTT-style match, segment by segment: ``+`` is one segment, a trailing
    ``#`` the rest; otherwise both must have the same number of segments."""
    p, t = pattern.split("/"), topic.split("/")
    for i, seg in enumerate(p):
        if seg == "#" and i == len(p) - 1:
            return True
        if i >= len(t) or (seg != "+" and seg != t[i]):
            return False
    return len(p) == len(t)


def _safe(value: object) -> bool:
    return isinstance(value, str) and IDENT_RE.fullmatch(value) is not None


def _telegraf_inputs(conf: Mapping) -> list[dict]:
    """Flatten ``[[inputs.mqtt_consumer]]`` into {patterns, tag, fields}, where
    fields maps a json_v2 ``path`` to (measurement, influx field name, type)."""
    out: list[dict] = []
    for inp in (conf.get("inputs", {}) or {}).get("mqtt_consumer", []) or []:
        if not isinstance(inp, Mapping):
            continue
        patterns = [str(t) for t in inp.get("topics", []) or []]
        default_meas = str(inp.get("name_override") or "mqtt_consumer")
        fields: dict[str, tuple[str, str, str]] = {}
        for block in inp.get("json_v2", []) or []:
            if not isinstance(block, Mapping):
                continue
            meas = str(block.get("measurement_name") or default_meas)
            for f in block.get("field", []) or []:
                if not isinstance(f, Mapping) or "path" not in f:
                    continue
                path = str(f["path"])
                name = str(f.get("rename") or path)
                ftype = _TELEGRAF_TYPES.get(str(f.get("type", "")).lower(), "unknown")
                fields.setdefault(path, (meas, name, ftype))
        out.append({"patterns": patterns, "tag": str(inp.get("topic_tag", "topic")),
                    "fields": fields})
    return out


def _iter_states(registry: Mapping) -> Iterable[tuple[str, str, Mapping]]:
    groups = registry.get("groups", {}) or {}
    for group, entries in groups.items():
        for sid, entry in (entries or {}).items():
            yield str(group), str(sid), entry if isinstance(entry, Mapping) else {}
    for sid, entry in (registry.get("derived", {}) or {}).items():
        yield "derived", str(sid), entry if isinstance(entry, Mapping) else {}


def build_history_map(state_yaml: Path | str,
                      telegraf_conf: Path | str) -> tuple[dict, dict]:
    """Return ``(entries, unavailable)``.

    ``entries[id]`` = {"measurement", "topic", "tag", "field", "fields", "type",
    "kind", "label", "unit"}: ``field`` is the preferred stored key and
    ``fields`` every stored candidate of a ``keys:`` source (same type), in
    registry order.  ``unavailable[id]`` = why that state has no history.
    """
    with open(state_yaml, encoding="utf-8") as fh:
        registry = yaml.load(fh, Loader=getattr(yaml, "CSafeLoader", yaml.SafeLoader)) or {}
    with open(telegraf_conf, encoding="utf-8") as fh:
        conf = tomllib.loads(fh.read())
    inputs = _telegraf_inputs(conf)
    all_ids = {sid for _, sid, _ in _iter_states(registry)}

    entries: dict[str, dict] = {}
    unavailable: dict[str, str] = {}
    for group, sid, st in _iter_states(registry):
        if group == "derived" or "expr" in st:
            expr = str(st.get("expr", ""))
            refs = [w for w in dict.fromkeys(re.findall(r"[A-Za-z_][A-Za-z0-9_]*", expr))
                    if w in all_ids]
            unavailable[sid] = (f"derived state ({expr}), computed live and never stored; "
                                f"query its operands instead: {', '.join(refs) or '-'}")
            continue
        src = st.get("source") or {}
        if not isinstance(src, Mapping) or not src.get("topic"):
            unavailable[sid] = "no MQTT source topic in state.yaml"
            continue
        topic = str(src["topic"])
        if src.get("presence"):
            unavailable[sid] = (f"presence state (true while {topic} keeps arriving); "
                                "no field is stored for it")
            continue
        if "keys" in src:
            keys = [str(k) for k in src.get("keys") or []]
        elif "key" in src:
            keys = [str(src["key"])]
        else:
            unavailable[sid] = (f"the payload of {topic} is a bare value; Telegraf stores "
                                "no field for it")
            continue

        matched = [i for i in inputs if any(_topic_matches(p, topic) for p in i["patterns"])]
        if not matched:
            unavailable[sid] = f"{topic} is not stored in InfluxDB (no Telegraf input for it)"
            continue
        hit = None
        for inp in matched:
            cands = [(k, inp["fields"][k]) for k in keys if k in inp["fields"]]
            if cands:
                hit = (inp, cands)
                break
        if hit is None:
            meas = sorted({m for i in matched for (m, _, _) in i["fields"].values()})
            unavailable[sid] = (f"key {'/'.join(keys)} of {topic} is not stored "
                                f"(Telegraf input {', '.join(meas) or '?'} has no such field)")
            continue
        inp, cands = hit
        meas, field, ftype = cands[0][1]
        fields = [name for _, (m, name, t) in cands if m == meas and t == ftype]
        tag = inp["tag"]
        if not tag:
            unavailable[sid] = (f"the Telegraf input for {topic} sets no topic tag; series "
                                "cannot be pinned")
            continue
        idents = [meas, topic, *fields]
        if not all(_safe(x) for x in idents) or not _TAG_RE.fullmatch(tag):
            log.error("history: state %s has an identifier unsafe for Flux (%r); left out",
                      sid, idents + [tag])
            unavailable[sid] = "its measurement/topic/field name is not a safe identifier"
            continue
        unit = st.get("unit")
        entries[sid] = {
            "measurement": meas, "topic": topic, "tag": tag,
            "field": field, "fields": fields, "type": ftype,
            "kind": str(st.get("kind", "")),
            "label": str(st.get("label") or sid),
            "unit": None if unit is None else str(unit),
        }
    return entries, unavailable


# ---------------------------------------------------------------------------
# Flux
# ---------------------------------------------------------------------------

def _fmt_utc(dt: datetime) -> str:
    if not isinstance(dt, datetime) or dt.tzinfo is None:
        raise ValueError("times must be timezone-aware datetimes")
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_flux(entries: Sequence[Mapping], start: datetime, stop: datetime,
               every_s: int, fn: str | Sequence[str], bucket: str) -> str:
    """One Flux script, one pipeline per entry, the i-th ending in
    ``yield(name: "s<i>")``.  ``fn`` is one aggregate for all entries or one per
    entry; "changes" is fetched as ``last`` (the filtering happens in Python).

    Raises ``ValueError`` if anything that would be spliced in is not a
    whitelisted identifier — the map already guarantees that; this is the
    second lock on the same door.
    """
    fns = [fn] * len(entries) if isinstance(fn, str) else list(fn)
    if len(fns) != len(entries):
        raise ValueError("one fn per entry")
    if not _safe(bucket):
        raise ValueError(f"unsafe bucket name {bucket!r}")
    if not isinstance(every_s, int) or isinstance(every_s, bool) or every_s < 1:
        raise ValueError(f"every_s must be a positive int, not {every_s!r}")
    t0, t1 = _fmt_utc(start), _fmt_utc(stop)
    blocks = []
    for i, (e, f) in enumerate(zip(entries, fns)):
        agg = "last" if f == "changes" else f
        if agg not in _FLUX_FNS:
            raise ValueError(f"unknown aggregate {f!r}")
        meas, topic, tag = e["measurement"], e["topic"], e.get("tag", "topic")
        fields = list(e.get("fields") or [e["field"]])
        if not all(_safe(x) for x in [meas, topic, *fields]) or not (
                isinstance(tag, str) and _TAG_RE.fullmatch(tag)):
            raise ValueError(f"unsafe identifier in {dict(e)!r}")
        if len(fields) == 1:
            fcond = f'r._field == "{fields[0]}"'
            keep = '["_time", "_value"]'
        else:
            fcond = "(" + " or ".join(f'r._field == "{x}"' for x in fields) + ")"
            keep = '["_time", "_value", "_field"]'
        blocks.append(
            f'from(bucket: "{bucket}")\n'
            f'  |> range(start: {t0}, stop: {t1})\n'
            f'  |> filter(fn: (r) => r._measurement == "{meas}" and r.{tag} == "{topic}"'
            f' and {fcond})\n'
            f'  |> aggregateWindow(every: {every_s}s, fn: {agg}, createEmpty: false,'
            f' timeSrc: "_start")\n'
            f'  |> keep(columns: {keep})\n'
            f'  |> yield(name: "s{i}")\n')
    return "\n".join(blocks)


# ---------------------------------------------------------------------------
# Annotated CSV
# ---------------------------------------------------------------------------

def _parse_rfc3339(text: str) -> float:
    """RFC3339 (nanoseconds allowed) → epoch seconds."""
    m = _RFC3339_RE.fullmatch(text)
    if not m:
        raise HistoryError(f"unexpected time {text[:40]!r} in the InfluxDB response")
    y, mo, d, h, mi, s, frac, tz = m.groups()
    dt = datetime(int(y), int(mo), int(d), int(h), int(mi), int(s), tzinfo=timezone.utc)
    if tz != "Z":
        sign = 1 if tz[0] == "+" else -1
        dt -= sign * timedelta(hours=int(tz[1:3]), minutes=int(tz[4:6]))
    return dt.timestamp() + (float("0." + frac) if frac else 0.0)


def _convert(text: str, dtype: str | None) -> Any:
    if text == "":
        return None
    if dtype in ("long", "unsignedLong"):
        return int(text)
    if dtype == "double":
        v = float(text)
        return v if math.isfinite(v) else None
    if dtype == "boolean":
        return text == "true"
    if dtype == "string":
        return text
    for conv in (int, float):         # no #datatype row: best effort
        try:
            v = conv(text)
            return v if not isinstance(v, float) or math.isfinite(v) else None
        except ValueError:
            pass
    return text


def parse_flux_csv(text: str) -> dict[str, list[tuple[float, Any, str | None]]]:
    """Demultiplex an annotated-CSV Flux response into
    ``{result name: [(epoch_s, value, field or None), ...]}``.

    Tables are separated by blank lines and/or annotation rows (``#datatype``
    gives the column types); the header row follows.  A table whose header has
    an ``error`` column is an error that happened mid-stream (HTTP was already
    200) and raises :class:`HistoryError` — partial data is never returned.
    """
    out: dict[str, list[tuple[float, Any, str | None]]] = {}
    header: list[str] | None = None
    types: list[str] | None = None
    col: dict[str, int] = {}
    in_error = False
    bad_rows = 0
    for row in csv.reader(io.StringIO(text)):
        if not row or all(c.strip() == "" for c in row):
            header, types = None, None
            continue
        if row[0].startswith("#"):
            if row[0] == "#datatype":
                types = row
            header = None
            continue
        if header is None:
            header = row
            col = {name: i for i, name in enumerate(header)}
            in_error = "error" in col
            if not in_error and ("_time" not in col or "_value" not in col):
                raise HistoryError(f"unexpected InfluxDB response columns: {header[:8]}")
            continue
        if in_error:
            i = col["error"]
            msg = row[i] if i < len(row) else ""
            raise HistoryError(f"InfluxDB query failed: {msg.strip()[:500] or 'unknown error'}")
        if len(row) != len(header):
            bad_rows += 1
            continue
        result = row[col["result"]] if "result" in col else "_result"
        vi = col["_value"]
        dtype = types[vi] if types is not None and vi < len(types) else None
        try:
            t = _parse_rfc3339(row[col["_time"]])
            v = _convert(row[vi], dtype)
        except ValueError:
            bad_rows += 1
            continue
        field = row[col["_field"]] if "_field" in col else None
        out.setdefault(result, []).append((t, v, field))
    if in_error:
        raise HistoryError("InfluxDB query failed (error table without a message)")
    if bad_rows:
        log.warning("history: skipped %d malformed CSV rows", bad_rows)
    return out


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A redirect would re-send the Authorization header elsewhere: refuse."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401
        return None


_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())


def urllib_post(url: str, headers: dict, body: bytes, timeout: float,
                opener: Any = None) -> tuple[int, bytes]:
    """POST with stdlib urllib; no proxies, no redirects.  Reads at most
    MAX_RESPONSE_BYTES + 1 (the caller refuses anything longer) and gives up
    when the whole exchange takes longer than ``timeout`` — urllib's own
    timeout is per socket operation, so a slow drip would never trip it."""
    deadline = time.monotonic() + timeout
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        resp = (opener or _OPENER).open(req, timeout=timeout)
    except urllib.error.HTTPError as exc:
        try:
            data = exc.read(64 * 1024) or b""
        except Exception:                       # noqa: BLE001 — body is best effort
            data = b""
        return exc.code, data
    with resp:
        chunks, size = [], 0
        while size <= MAX_RESPONSE_BYTES:
            if time.monotonic() > deadline:
                raise TimeoutError(f"InfluxDB took longer than {timeout:.0f} s")
            chunk = resp.read(min(65536, MAX_RESPONSE_BYTES + 1 - size))
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
        return getattr(resp, "status", 200), b"".join(chunks)


# ---------------------------------------------------------------------------
# Argument validation helpers
# ---------------------------------------------------------------------------

def _nice_every(min_s: int) -> int:
    for s in _NICE_STEPS_S:
        if s >= min_s:
            return s
    return -(-min_s // 86400) * 86400


def _parse_time(name: str, value: object, now: datetime) -> datetime:
    fmt_help = (f'{name} must be a relative time like "-6h" (units s, m, h, d, w) or an '
                f'RFC3339 time with an explicit offset, e.g. "2026-10-21T14:00:00-04:00" '
                f'or "2026-10-21T18:00:00Z"')
    if name == "stop":
        fmt_help += ', or "now"'
    if not isinstance(value, str) or len(value) > 40:
        raise HistoryError(fmt_help)
    if name == "stop" and value == "now":
        return now
    m = _REL_RE.fullmatch(value)
    if m:
        secs = int(m.group(1)) * _UNIT_S[m.group(2)]
        return now - timedelta(seconds=min(secs, 3650 * 86400))
    m = _RFC3339_RE.fullmatch(value)
    if not m:
        raise HistoryError(fmt_help)
    y, mo, d, h, mi, s, _frac, tz = m.groups()     # sub-second part dropped
    try:
        if tz == "Z":
            tzinfo = timezone.utc
        else:
            oh, om = int(tz[1:3]), int(tz[4:6])
            if oh > 23 or om > 59:
                raise ValueError("offset")
            off = timedelta(hours=oh, minutes=om)
            tzinfo = timezone(off if tz[0] == "+" else -off)
        dt = datetime(int(y), int(mo), int(d), int(h), int(mi), int(s), tzinfo=tzinfo)
        return dt.astimezone(timezone.utc)
    except (ValueError, OverflowError):
        raise HistoryError(f"{name} {value!r} is not a valid date/time") from None


def _short(value: object) -> str:
    text = repr(value)
    return text if len(text) <= 60 else text[:57] + "..."


def _compact(v: Any) -> Any:
    """Doubles to 7 significant digits (mK at room temperature) — the series
    are downsampled anyway and full repr doubles the payload."""
    if isinstance(v, float):
        return float(f"{v:.7g}")
    return v


# ---------------------------------------------------------------------------
# History
# ---------------------------------------------------------------------------

class History:
    """Answers ``query_history`` against InfluxDB with whitelisted Flux only.

    ``http_post(url, headers, body, timeout) -> (status, body)`` is injectable
    for tests; ``clock`` (epoch seconds) too.  The map is built once here; a
    broken state.yaml / telegraf.conf leaves the service up with history
    disabled rather than taking the write tools down with it.
    """

    def __init__(self, url: str, org: str, bucket: str, token: str | None,
                 state_yaml: Path, telegraf_conf: Path,
                 http_post: Callable[[str, dict, bytes, float], tuple[int, bytes]] | None = None,
                 clock: Callable[[], float] | None = None) -> None:
        self._url = (url or "").strip().rstrip("/")
        self._org = (org or "").strip()
        self._bucket = (bucket or "").strip()
        self._token = (token or "").strip() or None
        self._post = http_post or urllib_post
        self._clock = clock or time.time
        self._sem = threading.BoundedSemaphore(MAX_CONCURRENT_QUERIES)
        self._rate_lock = threading.Lock()
        self._recent: deque[float] = deque()
        self._map_error: str | None = None
        try:
            self._entries, self._unavailable = build_history_map(state_yaml, telegraf_conf)
        except Exception as exc:                # noqa: BLE001 — degrade, do not crash
            log.exception("history: could not build the state → InfluxDB map")
            self._entries, self._unavailable = {}, {}
            self._map_error = f"history map could not be built: {type(exc).__name__}: {exc}"
        log.info("history: %d states with history, %d without; InfluxDB %s org=%s bucket=%s, "
                 "token %s", len(self._entries), len(self._unavailable), self._url or "-",
                 self._org or "-", self._bucket or "-",
                 "configured" if self._token else "MISSING (query_history disabled)")

    # -- introspection ------------------------------------------------------

    def available(self) -> dict:
        """{id: {measurement, field, type, topic, label, unit}} for every state
        with history (+ "fields" for multi-key states), and "unavailable":
        {id: reason}."""
        out: dict[str, Any] = {}
        for sid, e in self._entries.items():
            row = {k: e[k] for k in ("measurement", "field", "type", "topic", "label", "unit")}
            if len(e["fields"]) > 1:
                row["fields"] = list(e["fields"])
            out[sid] = row
        unavailable = dict(self._unavailable)
        if self._map_error:
            unavailable["*"] = self._map_error
        out["unavailable"] = unavailable
        return out

    def entry(self, sid: str) -> dict | None:
        e = self._entries.get(sid)
        return dict(e) if e else None

    # -- the tool -----------------------------------------------------------

    def query(self, args: dict) -> tuple[bool, dict]:
        """``query_history`` → (is_error, result)."""
        try:
            return False, self._query(args)
        except HistoryError as exc:
            return True, {"ok": False, "tool": TOOL, "error": self._scrub(str(exc))}

    def _scrub(self, text: str) -> str:
        if self._token:
            text = text.replace(self._token, "***")
        return text

    def _config_problem(self) -> str | None:
        if self._map_error:
            return self._map_error
        if not self._token:
            return "no InfluxDB token configured (INFLUX_TOKEN in the service's env file)"
        if not self._url.startswith(("http://", "https://")):
            return f"InfluxDB URL {self._url!r} is not an http(s) URL"
        if not self._org:
            return "no InfluxDB org configured"
        if not _safe(self._bucket):
            return f"InfluxDB bucket {self._bucket!r} is missing or not a plain name"
        return None

    def _validate(self, args: object) -> dict:
        if not isinstance(args, Mapping):
            raise HistoryError("arguments must be an object")
        unknown = [k for k in args if k not in _ARG_KEYS]
        if unknown:
            raise HistoryError(f"unknown argument(s): {', '.join(_short(k) for k in unknown)}; "
                               f"allowed: {', '.join(_ARG_KEYS)}")

        ids = args.get("ids")
        if not isinstance(ids, list) or not ids:
            raise HistoryError("ids must be a non-empty list of state ids")
        if len(ids) > MAX_IDS:
            raise HistoryError(f"at most {MAX_IDS} ids per call (got {len(ids)})")
        if not all(isinstance(i, str) for i in ids):
            raise HistoryError("every id must be a string")
        if len(set(ids)) != len(ids):
            raise HistoryError("ids contains duplicates")
        problems = []
        for sid in ids:
            if sid in self._entries:
                continue
            if sid in self._unavailable:
                problems.append(f"{sid}: no history — {self._unavailable[sid]}")
            else:
                problems.append(f"{_short(sid)}: unknown state id (list_states shows the ids)")
        if problems:
            raise HistoryError("; ".join(problems))
        entries = [self._entries[sid] for sid in ids]

        fn = args.get("fn")
        if fn is not None and (not isinstance(fn, str) or fn not in FNS):
            raise HistoryError(f"fn must be one of {', '.join(FNS)}")
        if fn is not None:
            bad = [f"{sid} ({e['type']})" for sid, e in zip(ids, entries)
                   if fn not in FNS_BY_TYPE.get(e["type"], ())]
            if bad:
                raise HistoryError(f"fn {fn} does not apply to {', '.join(bad)}: text and "
                                   "untyped fields allow last, count or changes only")
            fns = [fn] * len(ids)
        else:
            fns = ["mean" if e["type"] == "double" else "last" for e in entries]

        mp = args.get("max_points")
        if mp is None:
            mp = DEFAULT_MAX_POINTS
        if (not isinstance(mp, int) or isinstance(mp, bool)
                or not 1 <= mp <= MAX_POINTS_PER_SERIES):
            raise HistoryError(f"max_points must be an integer from 1 to {MAX_POINTS_PER_SERIES}")

        every_user = None
        if args.get("every") is not None:
            ev = args["every"]
            m = _EVERY_RE.fullmatch(ev) if isinstance(ev, str) and len(ev) <= 12 else None
            if not m:
                raise HistoryError('every must be a duration like "30s", "5m", "1h" or "1d" '
                                   "(units s, m, h, d, w; no leading zeros)")
            every_user = int(m.group(1)) * _UNIT_S[m.group(2)]
            if every_user > MAX_RANGE.total_seconds():
                raise HistoryError("every must be at most 90 days")

        now = datetime.fromtimestamp(int(self._clock()), timezone.utc)
        if "start" not in args:
            raise HistoryError('start is required (e.g. "-6h")')
        start = _parse_time("start", args["start"], now)
        stop = now if args.get("stop") is None else _parse_time("stop", args["stop"], now)
        notes: list[str] = []
        if start >= now:
            raise HistoryError(f"start {_fmt_utc(start)} is not in the past (now {_fmt_utc(now)})")
        if stop > now:
            stop = now
            notes.append(f"stop was in the future; used now ({_fmt_utc(now)})")
        if stop <= HISTORY_START:
            raise HistoryError(f"no history before {_fmt_utc(HISTORY_START)} (the bucket was "
                               "created then)")
        if start < HISTORY_START:
            start = HISTORY_START
            notes.append(f"history starts {_fmt_utc(HISTORY_START)}; start moved there")
        if stop <= start:
            raise HistoryError(f"stop ({_fmt_utc(stop)}) must be after start ({_fmt_utc(start)})")
        if stop - start > MAX_RANGE:
            days = (stop - start).total_seconds() / 86400
            raise HistoryError(f"range is {days:.1f} days; at most 90 per call — split it")
        return {"ids": ids, "entries": entries, "fn": fn, "fns": fns, "max_points": mp,
                "every_user": every_user, "start": start, "stop": stop, "notes": notes}

    def _rate_ok(self) -> bool:
        now = time.monotonic()
        with self._rate_lock:
            while self._recent and now - self._recent[0] > 60.0:
                self._recent.popleft()
            if len(self._recent) >= MAX_QUERIES_PER_MINUTE:
                return False
            self._recent.append(now)
            return True

    def _query(self, args: dict) -> dict:
        problem = self._config_problem()
        if problem:
            raise HistoryError(problem)
        req = self._validate(args)
        entries, ids, fns = req["entries"], req["ids"], req["fns"]
        start, stop = req["start"], req["stop"]
        notes = req["notes"]
        range_s = int((stop - start).total_seconds())
        cap = min(req["max_points"], MAX_TOTAL_POINTS // len(ids))

        if req["fn"] == "changes":
            rows_per_window = sum(len(e["fields"]) for e in entries)
            floor_s = _nice_every(max(1, math.ceil(range_s * rows_per_window / CHANGES_ROW_BUDGET)))
        else:
            # Windows are aligned to the epoch, so a range can touch one more
            # window than range/every: size for cap - 1 to land on ≤ cap.
            floor_s = _nice_every(max(1, math.ceil(range_s / max(cap - 1, 1))))
        every_s = max(req["every_user"] or 1, floor_s)
        if req["every_user"] is not None and req["every_user"] < every_s:
            notes.append(f"every raised from {req['every_user']}s to {every_s}s to stay within "
                         + ("the response budget" if req["fn"] == "changes"
                            else f"{cap} points per series"))

        flux = build_flux(entries, start, stop, every_s, fns, self._bucket)
        if not self._sem.acquire(blocking=False):
            raise HistoryError("another history query is still running; try again in a moment")
        try:
            if not self._rate_ok():
                raise HistoryError(f"more than {MAX_QUERIES_PER_MINUTE} history queries in the "
                                   "last minute; wait a little")
            t_req = time.monotonic()
            results = self._run(flux)
            elapsed = time.monotonic() - t_req
        finally:
            self._sem.release()

        series = [self._series(sid, e, f, results.get(f"s{i}", []), every_s, cap)
                  for i, (sid, e, f) in enumerate(zip(ids, entries, fns))]
        log.info("history: ids=%s fn=%s %s..%s every=%ss -> %d points in %.2f s",
                 ",".join(ids), req["fn"] or "default", _fmt_utc(start), _fmt_utc(stop),
                 every_s, sum(s["points"] for s in series), elapsed)
        result = {
            "ok": True, "tool": TOOL,
            "start": _fmt_utc(start), "stop": _fmt_utc(stop), "every_s": every_s,
            "max_points_per_series": cap,
            "times": TIMES_NOTE,
            "series": series,
            "truncated": any(s["truncated"] for s in series),
        }
        if notes:
            result["notes"] = notes
        return result

    def _run(self, flux: str) -> dict[str, list[tuple[float, Any, str | None]]]:
        url = f"{self._url}/api/v2/query?" + urllib.parse.urlencode({"org": self._org})
        headers = {"Authorization": f"Token {self._token}",
                   "Content-Type": "application/json",
                   "Accept": "application/csv"}
        body = json.dumps({"query": flux, "type": "flux",
                           "dialect": {"annotations": ["datatype"], "header": True}}
                          ).encode("utf-8")
        try:
            status, data = self._post(url, headers, body, HTTP_TIMEOUT_S)
        except Exception as exc:                # noqa: BLE001 — any transport failure
            log.warning("history: InfluxDB request failed: %s", self._scrub(repr(exc)))
            raise HistoryError(f"InfluxDB not reachable: {type(exc).__name__}: {exc}") from None
        if status != 200:
            raise HistoryError(f"InfluxDB returned HTTP {status}: {_http_error_text(data)}")
        if len(data) > MAX_RESPONSE_BYTES:
            raise HistoryError("InfluxDB response is larger than 5 MB; use fewer ids, a shorter "
                               "range or a longer every")
        return parse_flux_csv(data.decode("utf-8", errors="replace"))

    def _series(self, sid: str, e: Mapping, fn: str,
                rows: list[tuple[float, Any, str | None]], every_s: int, cap: int) -> dict:
        fields = list(e["fields"])
        if len(fields) > 1:
            # keys: [filtered, raw] — per window, the first candidate present wins
            rank = {f: i for i, f in enumerate(fields)}
            best: dict[float, tuple[int, Any, str]] = {}
            for t, v, f in rows:
                r = rank.get(f, len(fields))
                if t not in best or r < best[t][0]:
                    best[t] = (r, v, f)
            points = [(t, best[t][1], best[t][2]) for t in sorted(best)]
        else:
            seen: set[float] = set()
            points = []
            for t, v, f in sorted(rows, key=lambda r: r[0]):
                if t not in seen:             # one series per topic+field; be safe anyway
                    seen.add(t)
                    points.append((t, v, f))

        if fn == "changes":
            kept, prev, first = [], None, True
            for p in points:
                if first or p[1] != prev:
                    kept.append(p)
                prev, first = p[1], False
            points = kept

        truncated = len(points) > cap
        if truncated:
            points = points[-cap:]            # keep the most recent

        used = [f for f in fields if any(p[2] == f for p in points)] if len(fields) > 1 else []
        unit = e["unit"]
        out: dict[str, Any] = {
            "id": sid, "label": e["label"], "unit": unit,
            "measurement": e["measurement"], "topic": e["topic"],
            "field": "|".join(used) if used else e["field"],
            "type": e["type"], "fn": fn, "every_s": every_s,
        }
        notes = []
        if fn == "count":
            out["unit"] = "samples"
        elif fn == "mean" and e["kind"] == "binary":
            out["unit"] = "fraction"
            notes.append("mean of a 0/1 state = fraction of the window it was 1")
        elif fn == "changes":
            notes.append("only windows whose last value differs from the previous window's; "
                         "the first point is the state at the start")
        if len(fields) > 1:
            notes.append(f"per window, {fields[0]} where stored, else {', '.join(fields[1:])}")
        if not points:
            notes.append("no data recorded in this range")
        if notes:
            out["note"] = "; ".join(notes)
        if points:
            t0 = math.floor(points[0][0])
            out["t0"] = datetime.fromtimestamp(t0, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            out["dt_s"] = [int(round(p[0] - t0)) for p in points]
            out["v"] = [_compact(p[1]) for p in points]
        else:
            out["t0"], out["dt_s"], out["v"] = None, [], []
        out["points"] = len(points)
        out["truncated"] = truncated
        if truncated:
            out["truncated_note"] = (f"more than {cap} points; the earliest were dropped — "
                                     "narrow the range or raise every")
        return out


def _http_error_text(data: bytes) -> str:
    """InfluxDB's pre-stream errors are JSON {"code", "message"}."""
    text = (data or b"").decode("utf-8", errors="replace").strip()
    try:
        obj = json.loads(text)
        if isinstance(obj, dict) and obj.get("message"):
            code = obj.get("code")
            return f"{obj['message']}" + (f" ({code})" if code else "")
    except ValueError:
        pass
    return text[:300] or "(empty body)"
