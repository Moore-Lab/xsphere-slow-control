#!/usr/bin/env python3
"""
Backfill calibrated LabJack RTD points into InfluxDB.

Reads every historical raw RTD record from the ``temperature`` measurement
(``source=labjack, sensor_type=rtd``) between the given time window,
applies the two-point resistance correction + IEC-60751 CVD inverse from
``slowcontrol/calibration/rtd_calibration.json``, and writes the result
back to the same measurement with tag ``source=labjack_cal`` at the
original timestamps.

The backfilled series shares the tag/field schema of the live stream
produced by ``CalibrationController``, so the Grafana dashboard
(``xsphere-rtd-calibration-dashboard.json``) sees a single continuous
labjack_cal timeline across historical + live points.

Idempotency
───────────
InfluxDB overwrites a point when measurement + tag set + timestamp
match — the tag set here is fixed by (source=labjack_cal, sensor_type=
rtd, channel=<N>) and the timestamps are copied verbatim from the source
points, so re-running this script produces the same series (any updated
coefficients in the JSON simply take effect on the next run).

Requires no extra Python packages — uses ``urllib.request`` for both the
Flux query (CSV response) and the line-protocol write.

Usage
─────
    export INFLUX_URL=http://192.168.8.116:8086
    export INFLUX_TOKEN=...            # bucket read + write
    export INFLUX_ORG=xbox-server
    export INFLUX_BUCKET=xsphere

    # Default: all-time backfill for RTDs 1,2,3
    python -m slowcontrol.tools.backfill_calibrated_rtd

    # Selective / dry-run examples
    python -m slowcontrol.tools.backfill_calibrated_rtd --dry-run
    python -m slowcontrol.tools.backfill_calibrated_rtd --start 2026-05-01T00:00:00Z --stop 2026-06-01T00:00:00Z
    python -m slowcontrol.tools.backfill_calibrated_rtd --channels 1,2

Any option can also come from the CLI (see ``--help``).
"""

from __future__ import annotations

import argparse
import csv
import io
import logging
import math
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Iterator, List, Optional, Tuple

# Reuse the same CVD + coefficient loader as the live controller so the
# backfill and live streams cannot drift.
from slowcontrol.controllers.calibration import (
    RtdCalibration,
    _default_calibration_path,
    _KELVIN,
)

log = logging.getLogger("backfill_calibrated_rtd")


# ── HTTP helpers ─────────────────────────────────────────────────────────────

def _http_post(url: str, *, data: bytes, headers: dict, timeout: float = 60.0) -> Tuple[int, bytes]:
    """POST with urllib; return (status, body).  Raises on network error only."""
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read() if e.fp is not None else b""


# ── InfluxDB Flux query ──────────────────────────────────────────────────────

_QUERY_TEMPLATE = """
from(bucket: "{bucket}")
  |> range(start: {start}, stop: {stop})
  |> filter(fn: (r) => r._measurement == "temperature"
                   and r.source == "labjack"
                   and r.sensor_type == "rtd"
                   and r._field == "resistance_ohm"
                   and ({channel_filter}))
  |> keep(columns: ["_time", "_value", "channel"])
"""


def build_query(bucket: str, start: str, stop: str, channels: List[str]) -> str:
    ch_filter = " or ".join(f'r.channel == "{c}"' for c in channels)
    return _QUERY_TEMPLATE.format(
        bucket=bucket, start=start, stop=stop, channel_filter=ch_filter,
    )


def _stream_query_rows(url: str, org: str, token: str, query: str) -> Iterator[Tuple[str, str, float]]:
    """Yield (time_rfc3339, channel, resistance_ohm) for each result row.

    Reads the annotated-CSV response *line by line* off the socket rather
    than buffering it — a single wide time window can still be tens of MB,
    and the caller windows the range so peak memory stays bounded.

    The Flux CSV format emits one table per series (here one per channel),
    each preceded by ``#datatype/#group/#default`` annotation lines and a
    header row.  We track the header per table and map columns by name so
    the parser is insensitive to column ordering.
    """
    endpoint = f"{url.rstrip('/')}/api/v2/query?" + urllib.parse.urlencode({"org": org})
    req = urllib.request.Request(
        endpoint,
        data=query.encode("utf-8"),
        headers={
            "Authorization": f"Token {token}",
            "Content-Type": "application/vnd.flux",
            "Accept": "application/csv",
        },
        method="POST",
    )
    header: Optional[List[str]] = None
    col: dict = {}
    with urllib.request.urlopen(req, timeout=600.0) as resp:
        if resp.status != 200:
            raise RuntimeError(f"Flux query HTTP {resp.status}: {resp.read()[:500]!r}")
        for raw in io.TextIOWrapper(resp, encoding="utf-8"):
            line = raw.rstrip("\r\n")
            if line == "":
                # Blank line separates tables — the next non-# line is a new header.
                header = None
                col = {}
                continue
            if line.startswith("#"):
                # Annotation row (#datatype / #group / #default) — a new table
                # is starting, so forget the previous header.
                header = None
                col = {}
                continue
            fields = next(csv.reader([line]))
            if header is None:
                header = fields
                col = {name: i for i, name in enumerate(header)}
                continue
            try:
                t = fields[col["_time"]]
                ch = fields[col["channel"]]
                v = fields[col["_value"]]
            except (KeyError, IndexError):
                continue
            if not t or not ch or v == "":
                continue
            try:
                yield t, ch, float(v)
            except ValueError:
                continue


# ── Timestamp conversion ─────────────────────────────────────────────────────

def rfc3339_to_ns(ts: str) -> int:
    """Parse the RFC3339 timestamps InfluxDB returns to nanoseconds since epoch.

    Handles both the second-precision (``...Z``) and nanosecond-precision
    (``...123456789Z``) forms the API can emit depending on the bucket's
    ``precision`` setting.  Uses a straight string split for the nanosecond
    part because ``datetime`` truncates to microseconds.
    """
    # Strip trailing Z and any timezone offset (InfluxDB always emits UTC).
    if ts.endswith("Z"):
        ts = ts[:-1]
    if "." in ts:
        date_part, frac = ts.split(".", 1)
        ns_str = frac[:9].ljust(9, "0")  # pad or truncate to nanoseconds
    else:
        date_part, ns_str = ts, "000000000"
    # Parse the whole-second part with the stdlib.
    dt = datetime.strptime(date_part, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
    epoch_s = int(dt.timestamp())
    return epoch_s * 1_000_000_000 + int(ns_str)


# ── Window planning ──────────────────────────────────────────────────────────

def _parse_relative(spec: str) -> Optional[timedelta]:
    """Parse a Flux-style negative duration (``-30d``, ``-12h``, ``-45m``, ``-90s``)."""
    m = re.fullmatch(r"-(\d+)([dhms])", spec.strip())
    if not m:
        return None
    n = int(m.group(1))
    unit = m.group(2)
    return {"d": timedelta(days=n), "h": timedelta(hours=n),
            "m": timedelta(minutes=n), "s": timedelta(seconds=n)}[unit]


def _resolve_start(spec: str, url: str, org: str, token: str, bucket: str,
                   channels: List[str], now: datetime) -> datetime:
    """Turn a --start spec into a concrete UTC datetime.

    ``0``            → the timestamp of the earliest raw RTD point (so the
                       backfill spans exactly the recorded history).
    RFC3339          → parsed as-is.
    ``-30d`` etc.    → now minus that duration.
    """
    spec = spec.strip()
    if spec in ("0", ""):
        earliest = _earliest_point(url, org, token, bucket, channels)
        if earliest is None:
            raise RuntimeError("no raw RTD points found — nothing to backfill")
        return earliest
    rel = _parse_relative(spec)
    if rel is not None:
        return now - rel
    return _parse_rfc3339(spec)


def _resolve_stop(spec: str, now: datetime) -> datetime:
    spec = spec.strip()
    if spec in ("now()", "now", ""):
        return now
    rel = _parse_relative(spec)
    if rel is not None:
        return now - rel
    return _parse_rfc3339(spec)


def _parse_rfc3339(ts: str) -> datetime:
    s = ts.strip()
    if s.endswith("Z"):
        s = s[:-1]
    date_part = s.split(".", 1)[0]
    return datetime.strptime(date_part, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)


def _fmt_rfc3339(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _earliest_point(url: str, org: str, token: str, bucket: str,
                    channels: List[str]) -> Optional[datetime]:
    ch_filter = " or ".join(f'r.channel == "{c}"' for c in channels)
    q = (
        f'from(bucket:"{bucket}") |> range(start:0) '
        f'|> filter(fn:(r)=> r._measurement=="temperature" and r.source=="labjack" '
        f'and r.sensor_type=="rtd" and r._field=="resistance_ohm" and ({ch_filter})) '
        f'|> group() |> first() |> keep(columns:["_time"])'
    )
    return _query_single_time(url, org, token, q)


def _query_single_time(url: str, org: str, token: str, query: str) -> Optional[datetime]:
    endpoint = f"{url.rstrip('/')}/api/v2/query?" + urllib.parse.urlencode({"org": org})
    req = urllib.request.Request(
        endpoint, data=query.encode("utf-8"),
        headers={"Authorization": f"Token {token}",
                 "Content-Type": "application/vnd.flux",
                 "Accept": "application/csv"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=120.0) as resp:
        text = resp.read().decode("utf-8")
    header = None
    for line in text.splitlines():
        if line == "" or line.startswith("#"):
            header = None
            continue
        fields = next(csv.reader([line]))
        if header is None:
            header = fields
            continue
        try:
            idx = header.index("_time")
        except ValueError:
            continue
        if idx < len(fields) and fields[idx]:
            return _parse_rfc3339(fields[idx])
    return None


def _iter_windows(start: datetime, stop: datetime,
                  window: timedelta) -> Iterator[Tuple[datetime, datetime]]:
    """Yield [w0, w1) half-open windows covering [start, stop).

    Flux ``range(stop:)`` is exclusive, so adjacent windows neither overlap
    nor gap — the union is exactly [start, stop) and each source point lands
    in exactly one window (idempotent re-runs).
    """
    w0 = start
    while w0 < stop:
        w1 = min(w0 + window, stop)
        yield w0, w1
        w0 = w1


# ── Line-protocol writer ─────────────────────────────────────────────────────

def _line(channel: str, r_raw: float, cal: RtdCalibration, ts_ns: int) -> Optional[str]:
    r_corr = cal.corrected_resistance(channel, r_raw)
    if r_corr is None:
        return None
    t_c = cal.corrected_temperature_k(channel, r_raw)
    if t_c is None or math.isnan(t_c):
        return None
    value_k = t_c
    value_c = t_c - _KELVIN
    # Line protocol: measurement,tag=... field=... timestamp
    #
    # The tag set must EXACTLY match what Telegraf writes for the live cal
    # stream, or the backfilled history and the live points form two separate
    # InfluxDB series (same fields, different tags) and never join into one
    # continuous line. Telegraf tags each point with:
    #   source, sensor_type, channel  (from topic_parsing)
    #   system=xsphere                (global_tags)
    #   topic=<full MQTT topic>       (mqtt_consumer's default topic_tag)
    # so we reproduce all four here. ('/' needs no escaping in a tag value.)
    topic = f"xsphere/sensors/temperature/labjack_cal/rtd/{channel}"
    return (
        f"temperature,source=labjack_cal,sensor_type=rtd,channel={channel},"
        f"system=xsphere,topic={topic} "
        f"value_k={value_k:.6f},value_c={value_c:.6f},"
        f"resistance_ohm={r_corr:.6f},resistance_ohm_raw={r_raw:.6f} "
        f"{ts_ns}"
    )


def _write_batch(url: str, org: str, bucket: str, token: str, lines: List[str]) -> None:
    endpoint = (
        f"{url.rstrip('/')}/api/v2/write?"
        + urllib.parse.urlencode({"org": org, "bucket": bucket, "precision": "ns"})
    )
    body = "\n".join(lines).encode("utf-8")
    status, resp = _http_post(
        endpoint,
        data=body,
        headers={
            "Authorization": f"Token {token}",
            "Content-Type": "text/plain; charset=utf-8",
        },
    )
    if status not in (200, 204):
        raise RuntimeError(
            f"InfluxDB write failed: HTTP {status} {resp[:400]!r} "
            f"(sample line: {lines[0][:200]!r})"
        )


# ── Main ─────────────────────────────────────────────────────────────────────

def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(
        description=(
            "Backfill calibrated LabJack RTD resistance / temperature into "
            "InfluxDB from the historical raw stream."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--url",   default=os.environ.get("INFLUX_URL"),
                   help="InfluxDB URL (env INFLUX_URL)")
    p.add_argument("--token", default=os.environ.get("INFLUX_TOKEN"),
                   help="InfluxDB API token with bucket read+write (env INFLUX_TOKEN)")
    p.add_argument("--org",   default=os.environ.get("INFLUX_ORG"),
                   help="InfluxDB org (env INFLUX_ORG)")
    p.add_argument("--bucket", default=os.environ.get("INFLUX_BUCKET", "xsphere"),
                   help="InfluxDB bucket (env INFLUX_BUCKET, default: xsphere)")
    p.add_argument("--calibration",
                   default=_default_calibration_path(),
                   help="Path to rtd_calibration.json")
    p.add_argument("--start", default="0",
                   help='Range start — RFC3339, "-30d"/"-12h", or 0 for full history (default: 0)')
    p.add_argument("--stop",  default="now()",
                   help='Range stop  — RFC3339, "-1d", or now() (default: now())')
    p.add_argument("--window-hours", type=float, default=24.0,
                   help="Read/compute/write one time window of this size at a "
                        "time so memory stays bounded on a multi-week backfill "
                        "(default: 24)")
    p.add_argument("--channels", default="1,2,3",
                   help="Comma-separated channel numbers to backfill (default: 1,2,3)")
    p.add_argument("--batch-size", type=int, default=5000,
                   help="Line-protocol points per write batch (default: 5000)")
    p.add_argument("--dry-run", action="store_true",
                   help="Query and compute calibrated points, but do not write")
    p.add_argument("--limit", type=int, default=0,
                   help="Stop after this many source rows (0 = no limit) — useful for smoke testing")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )

    missing = [name for name, val in
               (("--url/INFLUX_URL", args.url),
                ("--token/INFLUX_TOKEN", args.token),
                ("--org/INFLUX_ORG", args.org))
               if not val]
    if missing:
        p.error("Missing required option(s): " + ", ".join(missing))

    requested = [c.strip() for c in args.channels.split(",") if c.strip()]
    cal = RtdCalibration(args.calibration)
    known = set(cal.channels())
    channels = []
    for c in requested:
        if c in known:
            channels.append(c)
        else:
            log.warning("channel %s has no calibration coefficients — skipping", c)
    if not channels:
        log.error("no requested channels have calibration coefficients — nothing to do")
        return 2

    # Resolve the range to concrete UTC datetimes and walk it in windows.
    now = datetime.now(timezone.utc)
    start_dt = _resolve_start(args.start, args.url, args.org, args.token,
                              args.bucket, channels, now)
    stop_dt = _resolve_stop(args.stop, now)
    if stop_dt <= start_dt:
        log.error("empty range: start %s >= stop %s",
                  _fmt_rfc3339(start_dt), _fmt_rfc3339(stop_dt))
        return 2
    window = timedelta(hours=args.window_hours)

    span_days = (stop_dt - start_dt).total_seconds() / 86400.0
    n_windows = max(1, math.ceil((stop_dt - start_dt) / window))
    log.info("backfill bucket=%s channels=%s range=[%s .. %s] (%.1f days, %d windows of %.1fh)%s",
             args.bucket, channels, _fmt_rfc3339(start_dt), _fmt_rfc3339(stop_dt),
             span_days, n_windows, args.window_hours,
             "  [DRY RUN]" if args.dry_run else "")

    batch: List[str] = []
    n_read = 0
    n_written = 0
    n_skipped = 0
    stop_all = False

    def flush() -> None:
        nonlocal batch, n_written
        if not batch:
            return
        if not args.dry_run:
            _write_batch(args.url, args.org, args.bucket, args.token, batch)
        n_written += len(batch)
        batch.clear()

    for wi, (w0, w1) in enumerate(_iter_windows(start_dt, stop_dt, window), start=1):
        query = build_query(args.bucket, _fmt_rfc3339(w0), _fmt_rfc3339(w1), channels)
        w_read = 0
        for ts_str, channel, r_raw in _stream_query_rows(
                args.url, args.org, args.token, query):
            n_read += 1
            w_read += 1
            if args.limit and n_read > args.limit:
                n_read -= 1
                w_read -= 1
                stop_all = True
                break
            try:
                ts_ns = rfc3339_to_ns(ts_str)
            except ValueError:
                n_skipped += 1
                continue
            line = _line(channel, r_raw, cal, ts_ns)
            if line is None:
                n_skipped += 1
                continue
            batch.append(line)
            if len(batch) >= args.batch_size:
                flush()
        flush()   # close out each window so a batch never straddles two queries
        log.info("[%d/%d] %s .. %s  read=%d  total %s=%d",
                 wi, n_windows, _fmt_rfc3339(w0), _fmt_rfc3339(w1), w_read,
                 "computed" if args.dry_run else "written", n_written)
        if stop_all:
            break

    log.info("done — read %d source points, %s %d calibrated points (skipped %d)",
             n_read, "would write" if args.dry_run else "wrote", n_written, n_skipped)
    return 0


if __name__ == "__main__":
    sys.exit(main())
