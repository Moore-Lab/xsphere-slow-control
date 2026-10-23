"""
Offline tests for aicontrol/history.py — the AI's query_history tool.

    python -m pytest tests/test_aicontrol_history.py -q      (from the repo root)

The state → InfluxDB map is built from the real slowcontrol/state.yaml and
telegraf/telegraf.conf.  InfluxDB is a fake ``http_post`` that records the
request and answers with canned annotated CSV — no network.
"""

import io
import json
import logging
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml

from aicontrol import history as hist
from aicontrol.history import (History, HistoryError, build_flux,
                               build_history_map, parse_flux_csv, urllib_post)

REPO = Path(__file__).resolve().parents[1]
STATE = REPO / "slowcontrol" / "state.yaml"
CONF = REPO / "telegraf" / "telegraf.conf"
UTC = timezone.utc
NOW = datetime(2026, 10, 22, 12, 0, 0, tzinfo=UTC)
TOKEN = "tok-SECRET-123"


class FakePost:
    """Stands in for urllib: records each request, answers (status, body)."""

    def __init__(self, status=200, body=b"", exc=None):
        self.status, self.body, self.exc = status, body, exc
        self.calls = []

    def __call__(self, url, headers, body, timeout):
        self.calls.append({"url": url, "headers": dict(headers), "body": body,
                           "timeout": timeout, "json": json.loads(body)})
        if self.exc is not None:
            raise self.exc
        return self.status, self.body

    @property
    def flux(self):
        return self.calls[-1]["json"]["query"]


def make(post=None, token=TOKEN, state=STATE, conf=CONF):
    return History("http://localhost:8087/", "xbox-server", "xsphere", token, state, conf,
                   http_post=post if post is not None else FakePost(),
                   clock=lambda: NOW.timestamp())


def ts(offset_s, base=NOW - timedelta(hours=6)):
    return (base + timedelta(seconds=offset_s)).strftime("%Y-%m-%dT%H:%M:%SZ")


def table(result, dtype, rows, field=None):
    """One annotated-CSV table as InfluxDB writes it (CRLF line ends)."""
    head = "#datatype,string,long,dateTime:RFC3339," + dtype + (",string" if field else "")
    cols = ",result,table,_time,_value" + (",_field" if field else "")
    lines = [head, cols]
    for t, v in rows:
        lines.append(f",{result},0,{t},{v}" + (f",{field}" if field else ""))
    return "\r\n".join(lines) + "\r\n"


def body(*tables):
    return "\r\n".join(tables).encode("utf-8")


@pytest.fixture(scope="module")
def hmap():
    return build_history_map(STATE, CONF)


# ---------------------------------------------------------------------------
# map from the real repo files
# ---------------------------------------------------------------------------

def test_map_real_files_known_entries(hmap):
    entries, unavailable = hmap
    e = entries["pid_top_setpoint"]
    assert (e["measurement"], e["field"], e["type"], e["topic"]) == (
        "pid_state", "setpoint_k", "double", "xsphere/status/pid/top")
    e = entries["valve_cryostat"]
    assert (e["measurement"], e["field"], e["type"]) == ("valve_state", "state", "long")
    assert e["kind"] == "binary"
    e = entries["t_cube_top"]
    assert (e["measurement"], e["field"], e["topic"]) == (
        "temperature", "value_k", "xsphere/sensors/temperature/labjack/rtd/3")
    e = entries["level_cryostat"]
    assert (e["measurement"], e["field"], e["fields"]) == ("level", "filtered", ["filtered", "raw"])
    e = entries["turbo_ghs_state"]
    assert (e["measurement"], e["field"], e["type"]) == ("turbo", "state", "string")
    assert entries["nge_ch1_output"]["measurement"] == "nge_state"
    assert entries["pneumatic_control_enabled"]["measurement"] == "pneumatic_control"
    assert entries["level_cryostat_probe"]["field"] == "voltage_v"
    assert entries["t_top_clamp"]["topic"] == "xsphere/sensors/temperature/plc/rtd/6"


def test_map_real_files_unavailable(hmap):
    entries, unavailable = hmap
    for sid in ("gradient_mode", "gradient_base", "gradient_dv", "gradient_dl"):
        assert sid not in entries
        assert "xsphere/status/gradient is not stored" in unavailable[sid]
    for sid in ("dt_vertical_meas", "dt_longitudinal_meas", "pid_top_error", "nge_power_total"):
        assert sid not in entries
        assert unavailable[sid].startswith("derived state")
    assert "t_cube_bottom, t_cube_top" in unavailable["dt_vertical_meas"]
    assert "not stored" in unavailable["pid_top_mode"]          # key not a telegraf field
    assert "not stored" in unavailable["pid_top_setpoint_expr"]
    assert "presence" in unavailable["service_alive"]
    # the supply-level topic has one segment fewer than xsphere/status/nge/+
    assert "xsphere/status/nge is not stored" in unavailable["nge_connected"]
    assert "pv_interlock" in unavailable["pv_interlock_max_k"]


def test_map_covers_every_registry_id_once_with_safe_identifiers(hmap):
    entries, unavailable = hmap
    reg = yaml.safe_load(STATE.read_text(encoding="utf-8"))
    ids = {sid for g in reg["groups"].values() for sid in g} | set(reg["derived"])
    assert set(entries) | set(unavailable) == ids
    assert not set(entries) & set(unavailable)
    for e in entries.values():
        for ident in [e["measurement"], e["topic"], *e["fields"]]:
            assert hist.IDENT_RE.fullmatch(ident)
        assert e["tag"] == "topic"


def test_map_synthetic_rename_tag_wildcard_and_unsafe(tmp_path):
    state = tmp_path / "state.yaml"
    state.write_text(yaml.safe_dump({"groups": {"g": {
        "a": {"kind": "analog", "unit": "K", "source": {"topic": "x/a/1", "key": "v"}},
        "b": {"kind": "binary", "source": {"topic": "x/b/deep/er", "key": "n"}},
        "evil": {"kind": "analog", "source": {"topic": 'x/a/1" or true or "', "key": "v"}},
        "scalar": {"kind": "analog", "source": {"topic": "x/a/2"}},
        "multi": {"kind": "analog", "source": {"topic": "x/a/3", "keys": ["nope", "v", "w"]}},
    }}, "derived": {"d": {"kind": "analog", "expr": "a * 2"}}}), encoding="utf-8")
    conf = tmp_path / "telegraf.conf"
    conf.write_text("""
[[inputs.mqtt_consumer]]
  topics = ["x/a/+"]
  topic_tag = "mqtt_topic"
  data_format = "json_v2"
  [[inputs.mqtt_consumer.json_v2]]
    measurement_name = "meas_a"
    [[inputs.mqtt_consumer.json_v2.field]]
      path = "v"
      rename = "value"
      type = "float"
    [[inputs.mqtt_consumer.json_v2.field]]
      path = "w"
      type = "int"
[[inputs.mqtt_consumer]]
  topics = ["x/b/#"]
  data_format = "json_v2"
  [[inputs.mqtt_consumer.json_v2]]
    measurement_name = "meas_b"
    [[inputs.mqtt_consumer.json_v2.field]]
      path = "n"
      type = "int"
""", encoding="utf-8")
    entries, unavailable = build_history_map(state, conf)
    assert entries["a"]["field"] == "value" and entries["a"]["tag"] == "mqtt_topic"
    assert entries["b"]["measurement"] == "meas_b" and entries["b"]["type"] == "long"
    assert "evil" not in entries and "not a safe identifier" in unavailable["evil"]
    assert "bare value" in unavailable["scalar"]
    # candidates of another type are not merged into one series
    assert entries["multi"]["field"] == "value" and entries["multi"]["fields"] == ["value"]
    assert unavailable["d"].startswith("derived state (a * 2)")
    assert "a" in unavailable["d"]


# ---------------------------------------------------------------------------
# Flux text
# ---------------------------------------------------------------------------

EXPECTED_2 = '''\
from(bucket: "xsphere")
  |> range(start: 2026-10-22T06:00:00Z, stop: 2026-10-22T12:00:00Z)
  |> filter(fn: (r) => r._measurement == "pid_state" and r.topic == "xsphere/status/pid/top" and r._field == "setpoint_k")
  |> aggregateWindow(every: 120s, fn: mean, createEmpty: false, timeSrc: "_start")
  |> keep(columns: ["_time", "_value"])
  |> yield(name: "s0")

from(bucket: "xsphere")
  |> range(start: 2026-10-22T06:00:00Z, stop: 2026-10-22T12:00:00Z)
  |> filter(fn: (r) => r._measurement == "valve_state" and r.topic == "xsphere/status/valve/cryostat" and r._field == "state")
  |> aggregateWindow(every: 120s, fn: last, createEmpty: false, timeSrc: "_start")
  |> keep(columns: ["_time", "_value"])
  |> yield(name: "s1")
'''


def test_build_flux_exact_text_two_ids(hmap):
    entries, _ = hmap
    flux = build_flux([entries["pid_top_setpoint"], entries["valve_cryostat"]],
                      NOW - timedelta(hours=6), NOW, 120, ["mean", "last"], "xsphere")
    assert flux == EXPECTED_2


def test_query_sends_exactly_that_flux_and_request_shape():
    post = FakePost(body=b"")
    is_err, res = make(post).query({"ids": ["pid_top_setpoint", "valve_cryostat"],
                                    "start": "-6h"})
    assert not is_err, res
    call = post.calls[0]
    assert call["json"]["query"] == EXPECTED_2      # 6 h / 199 → 109 s → 120 s; defaults mean / last
    assert call["url"] == "http://localhost:8087/api/v2/query?org=xbox-server"
    assert call["headers"] == {"Authorization": f"Token {TOKEN}",
                               "Content-Type": "application/json",
                               "Accept": "application/csv"}
    assert call["json"]["type"] == "flux"
    assert call["json"]["dialect"] == {"annotations": ["datatype"], "header": True}
    assert call["timeout"] == 20.0
    assert res["every_s"] == 120 and res["start"] == "2026-10-22T06:00:00Z"
    assert res["stop"] == "2026-10-22T12:00:00Z"


def test_build_flux_multi_field_keeps_field(hmap):
    entries, _ = hmap
    flux = build_flux([entries["level_cryostat"]], NOW - timedelta(hours=1), NOW, 60, "mean",
                      "xsphere")
    assert ('r.topic == "xsphere/sensors/level/cryostat" and '
            '(r._field == "filtered" or r._field == "raw"))') in flux
    assert 'keep(columns: ["_time", "_value", "_field"])' in flux


def test_build_flux_changes_fetches_last(hmap):
    entries, _ = hmap
    flux = build_flux([entries["valve_cryostat"]], NOW - timedelta(hours=1), NOW, 1, "changes",
                      "xsphere")
    assert "aggregateWindow(every: 1s, fn: last," in flux


@pytest.mark.parametrize("mutate", [
    lambda e: e.update(topic='xsphere/status/pid/top" or true or "'),
    lambda e: e.update(measurement="pid_state\\"),
    lambda e: e.update(field="setpoint_k${x}", fields=["setpoint_k${x}"]),
    lambda e: e.update(fields=["setpoint_k", 'kp") |> drop(']),
    lambda e: e.update(tag="topic or true"),
])
def test_build_flux_rejects_unsafe_identifiers(hmap, mutate):
    e = dict(hmap[0]["pid_top_setpoint"])
    mutate(e)
    with pytest.raises(ValueError):
        build_flux([e], NOW - timedelta(hours=1), NOW, 60, "mean", "xsphere")


@pytest.mark.parametrize("kwargs", [
    {"bucket": 'xsphere") |> drop('},
    {"fn": "sum"},
    {"fn": "mean) |> map(fn: (r) => r"},
    {"every_s": 0},
    {"every_s": True},
    {"every_s": 1.5},
    {"start": datetime(2026, 10, 22)},            # naive
])
def test_build_flux_rejects_bad_parameters(hmap, kwargs):
    args = {"entries": [hmap[0]["pid_top_setpoint"]], "start": NOW - timedelta(hours=1),
            "stop": NOW, "every_s": 60, "fn": "mean", "bucket": "xsphere"}
    args.update(kwargs)
    with pytest.raises(ValueError):
        build_flux(**args)


# ---------------------------------------------------------------------------
# input validation — every refusal happens before any request
# ---------------------------------------------------------------------------

OK = {"ids": ["pid_top_setpoint"], "start": "-6h"}


def _with(**kw):
    a = dict(OK)
    for k, v in kw.items():
        if v is _DROP:
            a.pop(k, None)
        else:
            a[k] = v
    return a


_DROP = object()

BAD_ARGS = [
    # ids
    _with(ids="pid_top_setpoint"),
    _with(ids=[]),
    _with(ids=["t_cube_top", "t_cube_bottom", "t_cube_nozzle", "t_top_clamp", "t_bottom_clamp",
               "pid_top_pv", "pid_bottom_pv", "pid_nozzle_pv", "pid_top_output"]),
    _with(ids=["pid_top_setpoint", "pid_top_setpoint"]),
    _with(ids=[1]),
    _with(ids=[None]),
    _with(ids=['pid_top_setpoint" or r._field == "kp']),
    _with(ids=["pid_top_setpoint\") |> yield(name: \"x"]),
    _with(ids=["no_such_state"]),
    _with(ids=["gradient_base"]),
    _with(ids=["dt_vertical_meas"]),
    _with(ids=_DROP),
    # start
    _with(start=_DROP),
    _with(start=None),
    _with(start=6),
    _with(start=["-6h"]),
    _with(start='-6h) |> drop(columns: ["_value"]'),
    _with(start="-6h, stop: now()) |> to(bucket: \"x\""),
    _with(start="-06h"),
    _with(start="-0h"),
    _with(start="-1mo"),
    _with(start="-6H"),
    _with(start="6h"),
    _with(start="-6h\n"),
    _with(start=" -6h"),
    _with(start="-1.5h"),
    _with(start="now()"),
    _with(start="now"),
    _with(start="2026-10-21T00:00:00"),                # no offset: EDT or UTC?
    _with(start="2026-10-21 00:00:00Z"),
    _with(start="2026-10-21T00:00:00z"),
    _with(start="2026-10-21"),
    _with(start="2026-13-01T00:00:00Z"),
    _with(start="2026-10-21T00:00:00+25:00"),
    _with(start="2026-10-21T00:00:00Z) |> yield("),
    # stop
    _with(stop="now()"),
    _with(stop="yesterday"),
    _with(stop='-1h"'),
    _with(stop=5),
    # every
    _with(every="0s"),
    _with(every="05m"),
    _with(every="1mo"),
    _with(every='5m"'),
    _with(every="5m, fn: sum"),
    _with(every=5),
    _with(every="1.5h"),
    _with(every=""),
    _with(every="9999w"),
    # fn
    _with(fn="sum"),
    _with(fn="MEAN"),
    _with(fn="mean) |> map(fn: (r) => r"),
    _with(fn=True),
    _with(fn=["mean"]),
    # max_points
    _with(max_points=0),
    _with(max_points=-1),
    _with(max_points=1001),
    _with(max_points=True),
    _with(max_points=5.0),
    _with(max_points="200"),
    # unknown keys / wrong shape
    _with(query='from(bucket: "xsphere") |> range(start: -1h)'),
    _with(flux="x"),
    [],
    "ids",
    None,
]


@pytest.mark.parametrize("args", BAD_ARGS)
def test_invalid_arguments_refused_without_a_request(args):
    post = FakePost()
    is_err, res = make(post).query(args)
    assert is_err is True
    assert res["ok"] is False and res["tool"] == "query_history"
    assert isinstance(res["error"], str) and res["error"]
    assert post.calls == []


def test_refusal_texts_name_the_problem():
    h = make()
    assert "no history" in h.query(_with(ids=["gradient_base"]))[1]["error"]
    assert "derived" in h.query(_with(ids=["dt_vertical_meas"]))[1]["error"]
    assert "unknown state id" in h.query(_with(ids=["nope"]))[1]["error"]
    assert "at most 8" in h.query(_with(ids=[f"x{i}" for i in range(9)]))[1]["error"]
    assert "explicit offset" in h.query(_with(start="2026-10-21T00:00:00"))[1]["error"]
    assert "unknown argument" in h.query(_with(query="x"))[1]["error"]


@pytest.mark.parametrize("fn", ["mean", "min", "max"])
def test_string_field_refuses_numeric_aggregates(fn):
    post = FakePost()
    is_err, res = make(post).query({"ids": ["turbo_ghs_state"], "start": "-1h", "fn": fn})
    assert is_err and "turbo_ghs_state (string)" in res["error"]
    assert post.calls == []


@pytest.mark.parametrize("fn", ["last", "count", "changes"])
def test_string_field_allows_last_count_changes(fn):
    is_err, res = make().query({"ids": ["turbo_ghs_state"], "start": "-1h", "fn": fn})
    assert not is_err, res


def test_no_token_refuses():
    for token in (None, "", "   "):
        post = FakePost()
        is_err, res = make(post, token=token).query(OK)
        assert is_err and "no InfluxDB token configured" in res["error"]
        assert post.calls == []


def test_map_failure_degrades(tmp_path):
    h = make(state=tmp_path / "missing.yaml")
    assert "could not be built" in h.available()["unavailable"]["*"]
    is_err, res = h.query(OK)
    assert is_err and "could not be built" in res["error"]


# ---------------------------------------------------------------------------
# time range handling
# ---------------------------------------------------------------------------

def test_range_over_90_days_refused():
    post = FakePost()
    is_err, res = make(post).query(_with(start="-100d"))
    assert is_err and "at most 90" in res["error"] and post.calls == []


def test_huge_relative_start_is_clamped_not_crashing():
    is_err, res = make().query(_with(start="-99999999999999w"))
    assert is_err and "at most 90" in res["error"]       # clamped to 2026-05-11 → 164 days


def test_start_before_history_is_clamped_with_note():
    post = FakePost()
    is_err, res = make(post).query(_with(start="2026-01-01T00:00:00Z",
                                         stop="2026-06-01T00:00:00-04:00"))
    assert not is_err, res
    assert res["start"] == "2026-05-11T00:00:00Z" and res["stop"] == "2026-06-01T04:00:00Z"
    assert any("history starts" in n for n in res["notes"])
    assert "range(start: 2026-05-11T00:00:00Z, stop: 2026-06-01T04:00:00Z)" in post.flux


def test_offsets_are_converted_to_utc_and_fraction_dropped():
    post = FakePost()
    is_err, res = make(post).query(_with(start="2026-10-21T14:00:00.123456789-04:00",
                                         stop="2026-10-21T20:30:00+00:00"))
    assert not is_err, res
    assert "range(start: 2026-10-21T18:00:00Z, stop: 2026-10-21T20:30:00Z)" in post.flux


@pytest.mark.parametrize("start,stop,text", [
    ("2026-04-01T00:00:00Z", "2026-05-01T00:00:00Z", "no history before"),
    ("2026-10-23T00:00:00Z", None, "not in the past"),
    ("-1h", "-2h", "must be after start"),
])
def test_bad_ranges_refused(start, stop, text):
    post = FakePost()
    args = {"ids": ["pid_top_setpoint"], "start": start}
    if stop is not None:
        args["stop"] = stop
    is_err, res = make(post).query(args)
    assert is_err and text in res["error"] and post.calls == []


def test_future_stop_is_clamped_to_now():
    is_err, res = make().query(_with(stop="2026-10-23T00:00:00Z"))
    assert not is_err and res["stop"] == "2026-10-22T12:00:00Z"
    assert any("future" in n for n in res["notes"])


# ---------------------------------------------------------------------------
# CSV parsing and error handling
# ---------------------------------------------------------------------------

def test_parse_csv_multiple_tables_types_and_nulls():
    text = (
        "#datatype,string,long,dateTime:RFC3339,double\r\n"
        ",result,table,_time,_value\r\n"
        ",s0,0,2026-10-21T18:00:00Z,77.123456789\r\n"
        ",s0,0,2026-10-21T18:01:00Z,\r\n"
        ",s0,0,2026-10-21T18:02:00Z,NaN\r\n"
        "\r\n"
        "#datatype,string,long,dateTime:RFC3339,long\r\n"
        ",result,table,_time,_value\r\n"
        ",s1,0,2026-10-21T18:00:18.702459815Z,1\r\n"
        ",s1,1,2026-10-21T18:05:00Z,0\r\n"
        "\r\n"
        "#datatype,string,long,dateTime:RFC3339,string\r\n"
        ",result,table,_time,_value\r\n"
        ',s2,0,2026-10-21T18:00:00Z,"stopped, coasting"\r\n'
        "\r\n"
    )
    out = parse_flux_csv(text)
    t0 = datetime(2026, 10, 21, 18, tzinfo=UTC).timestamp()
    assert out["s0"] == [(t0, 77.123456789, None), (t0 + 60, None, None), (t0 + 120, None, None)]
    assert out["s1"][0][0] == pytest.approx(t0 + 18.702459815)
    assert out["s1"][0][1] == 1 and isinstance(out["s1"][0][1], int)
    assert out["s1"][1] == (t0 + 300, 0, None)
    assert out["s2"] == [(t0, "stopped, coasting", None)]


def test_parse_csv_annotations_without_blank_line_and_field_column():
    text = (
        "#datatype,string,long,dateTime:RFC3339,double,string\n"
        "#group,false,false,false,false,true\n"
        "#default,_result,,,,\n"
        ",result,table,_time,_value,_field\n"
        ",s0,0,2026-10-21T18:00:00Z,5.5,filtered\n"
        "#datatype,string,long,dateTime:RFC3339,double,string\n"
        ",result,table,_time,_value,_field\n"
        ",s0,1,2026-10-21T18:00:00Z,6.5,raw\n"
    )
    out = parse_flux_csv(text)
    assert [(v, f) for _, v, f in out["s0"]] == [(5.5, "filtered"), (6.5, "raw")]


def test_parse_csv_error_table_raises():
    text = (
        "#datatype,string,long,dateTime:RFC3339,double\r\n"
        ",result,table,_time,_value\r\n"
        ",s0,0,2026-10-21T18:00:00Z,1.0\r\n"
        "\r\n"
        "#datatype,string,string\r\n"
        "#group,true,true\r\n"
        "#default,,\r\n"
        ",error,reference\r\n"
        ',"panic: runtime error, out of memory",897\r\n'
    )
    with pytest.raises(HistoryError, match="out of memory"):
        parse_flux_csv(text)


def test_parse_csv_empty_body():
    assert parse_flux_csv("") == {}
    assert parse_flux_csv("\r\n") == {}


def test_error_table_in_200_response_is_an_error_not_partial_data():
    data = body(table("s0", "double", [(ts(0), 1.0)]),
                "#datatype,string,string\r\n,error,reference\r\n,engine: unknown field,\r\n")
    is_err, res = make(FakePost(body=data)).query(OK)
    assert is_err and "engine: unknown field" in res["error"]


def test_http_400_json_message():
    err = json.dumps({"code": "invalid", "message": "compilation failed: error at @1:1"}).encode()
    is_err, res = make(FakePost(status=400, body=err)).query(OK)
    assert is_err
    assert res["error"] == "InfluxDB returned HTTP 400: compilation failed: error at @1:1 (invalid)"


def test_http_500_plain_text():
    is_err, res = make(FakePost(status=500, body=b"upstream exploded")).query(OK)
    assert is_err and "HTTP 500: upstream exploded" in res["error"]


def test_oversized_response_refused():
    is_err, res = make(FakePost(body=b"x" * (hist.MAX_RESPONSE_BYTES + 1))).query(OK)
    assert is_err and "larger than 5 MB" in res["error"]


def test_transport_failure_is_reported_without_the_token(caplog):
    caplog.set_level(logging.DEBUG)
    post = FakePost(exc=urllib.error.URLError(f"refused (Authorization: Token {TOKEN})"))
    is_err, res = make(post).query(OK)
    assert is_err and "InfluxDB not reachable" in res["error"]
    assert TOKEN not in res["error"]
    assert TOKEN not in caplog.text


def test_token_never_logged_on_success(caplog):
    caplog.set_level(logging.DEBUG)
    make(FakePost(body=b"")).query(OK)
    assert TOKEN not in caplog.text


# ---------------------------------------------------------------------------
# output shape, capping, changes
# ---------------------------------------------------------------------------

def test_compact_output_shape():
    data = body(table("s0", "double", [(ts(0), 77.123456789), (ts(120), 77.2), (ts(240), 77.3)]),
                table("s1", "long", [(ts(0), 0), (ts(120), 1)]))
    is_err, res = make(FakePost(body=data)).query(
        {"ids": ["pid_top_setpoint", "valve_cryostat"], "start": "-6h"})
    assert not is_err, res
    assert set(res) == {"ok", "tool", "start", "stop", "every_s", "max_points_per_series",
                        "times", "series", "truncated"}
    assert res["ok"] is True and res["truncated"] is False and res["max_points_per_series"] == 200
    s0, s1 = res["series"]
    assert s0 == {
        "id": "pid_top_setpoint", "label": "Zone 2 (top) setpoint", "unit": "K",
        "measurement": "pid_state", "topic": "xsphere/status/pid/top", "field": "setpoint_k",
        "type": "double", "fn": "mean", "every_s": 120,
        "t0": "2026-10-22T06:00:00Z", "dt_s": [0, 120, 240], "v": [77.12346, 77.2, 77.3],
        "points": 3, "truncated": False,
    }
    assert s1["fn"] == "last" and s1["v"] == [0, 1] and isinstance(s1["v"][0], int)
    assert s1["t0"] == "2026-10-22T06:00:00Z" and s1["dt_s"] == [0, 120]
    json.dumps(res, allow_nan=False)                 # serialisable as is


def test_series_without_data():
    is_err, res = make(FakePost(body=b"")).query(OK)
    s = res["series"][0]
    assert not is_err and s["t0"] is None and s["dt_s"] == [] and s["v"] == []
    assert s["points"] == 0 and "no data" in s["note"]


def test_every_follows_max_points():
    post = FakePost()
    is_err, res = make(post).query(_with(max_points=10))
    assert not is_err and res["every_s"] == 3600     # 21600 / 9 = 2400 → 3600
    assert "every: 3600s" in post.flux


def test_user_every_kept_when_coarse_enough_raised_when_not():
    is_err, res = make().query(_with(every="1h"))
    assert not is_err and res["every_s"] == 3600 and "notes" not in res
    is_err, res = make().query(_with(every="1m"))
    assert not is_err and res["every_s"] == 120
    assert any("raised from 60s to 120s" in n for n in res["notes"])


def test_total_points_capped_across_ids():
    ids = ["t_cube_top", "t_cube_bottom", "t_cube_nozzle", "t_top_clamp", "t_bottom_clamp",
           "pid_top_pv", "pid_bottom_pv", "pid_nozzle_pv"]
    post = FakePost()
    is_err, res = make(post).query({"ids": ids, "start": "-6h", "max_points": 1000})
    assert not is_err
    assert res["max_points_per_series"] == 250       # 2000 / 8
    assert res["every_s"] == 120                     # 21600 / 249 = 87 → 120
    assert post.flux.count("yield(") == 8


def test_more_rows_than_cap_keeps_the_latest():
    rows = [(ts(i * 3600), float(i)) for i in range(8)]
    is_err, res = make(FakePost(body=body(table("s0", "double", rows)))).query(
        _with(max_points=5))
    s = res["series"][0]
    assert not is_err and s["truncated"] is True and res["truncated"] is True
    assert s["v"] == [3.0, 4.0, 5.0, 6.0, 7.0] and s["dt_s"] == [0, 3600, 7200, 10800, 14400]
    assert s["t0"] == ts(3 * 3600)
    assert "earliest were dropped" in s["truncated_note"]


def test_changes_emits_transitions_only():
    base = NOW - timedelta(days=1)
    rows = [(ts(i, base), v) for i, v in enumerate([0, 0, 1, 1, 1, 0, 0])]
    post = FakePost(body=body(table("s0", "long", rows)))
    is_err, res = make(post).query({"ids": ["valve_cryostat"], "start": "-1d", "fn": "changes"})
    assert not is_err, res
    s = res["series"][0]
    assert s["fn"] == "changes" and s["v"] == [0, 1, 0] and s["dt_s"] == [0, 2, 5]
    assert res["every_s"] == 2                       # 86400 rows / 80000 budget → 2 s
    assert "fn: last" in post.flux and "every: 2s" in post.flux


def test_changes_short_range_runs_at_one_second():
    post = FakePost()
    is_err, res = make(post).query({"ids": ["valve_cryostat", "valve_ballast"], "start": "-6h",
                                    "fn": "changes"})
    assert not is_err and res["every_s"] == 1


def test_changes_respects_user_every_and_strings():
    rows = [(ts(i * 60), v) for i, v in enumerate(["stopped", "accelerating", "accelerating",
                                                   "normal", "normal"])]
    is_err, res = make(FakePost(body=body(table("s0", "string", rows)))).query(
        {"ids": ["turbo_ghs_state"], "start": "-6h", "fn": "changes", "every": "1m"})
    s = res["series"][0]
    assert not is_err and res["every_s"] == 60
    assert s["v"] == ["stopped", "accelerating", "normal"] and s["dt_s"] == [0, 60, 180]


def test_changes_capped_to_latest():
    rows = [(ts(i), i % 2) for i in range(10)]
    is_err, res = make(FakePost(body=body(table("s0", "long", rows)))).query(
        {"ids": ["valve_cryostat"], "start": "-6h", "fn": "changes", "max_points": 4})
    s = res["series"][0]
    assert s["truncated"] and s["v"] == [0, 1, 0, 1] and s["dt_s"] == [0, 1, 2, 3]
    assert s["t0"] == ts(6)
    # The earliest changes were dropped: v[0] is not the state at the start.
    assert "v[0] is NOT the state at the start" in s["note"]
    assert "the first point is the state at the start" not in s["note"]


def test_changes_not_truncated_first_point_is_the_start_state():
    rows = [(ts(i), i % 2) for i in range(4)]
    is_err, res = make(FakePost(body=body(table("s0", "long", rows)))).query(
        {"ids": ["valve_cryostat"], "start": "-6h", "fn": "changes"})
    s = res["series"][0]
    assert not is_err and not s["truncated"]
    assert "the first point is the state at the start" in s["note"]


def test_times_note_says_first_and_last_windows_are_cut():
    # Flux clips the epoch-aligned windows to the range: the first starts at
    # `start`, the last ends at `stop` (now), both possibly short.
    is_err, res = make(FakePost()).query(OK)
    assert not is_err
    assert "first and the last are cut at start and stop" in res["times"]
    assert "every_s long." not in res["times"]


def test_multi_key_level_prefers_filtered_then_raw():
    data = body(table("s0", "double", [(ts(0), 5.0), (ts(60), 5.5)], field="filtered")
                + table("s0", "double", [(ts(0), 9.0), (ts(60), 9.5), (ts(120), 4.25)],
                        field="raw").split("\r\n", 2)[2])   # same schema: no new header
    is_err, res = make(FakePost(body=data)).query({"ids": ["level_ballast"], "start": "-6h"})
    s = res["series"][0]
    assert not is_err, res
    assert s["v"] == [5.0, 5.5, 4.25] and s["dt_s"] == [0, 60, 120]
    assert s["field"] == "filtered|raw" and "filtered where stored" in s["note"]


def test_binary_mean_is_a_fraction_and_count_is_samples():
    data = body(table("s0", "double", [(ts(0), 0.25)]), table("s1", "long", [(ts(0), 120)]))
    is_err, res = make(FakePost(body=data)).query(
        {"ids": ["valve_cryostat", "valve_ballast"], "start": "-6h", "fn": "mean"})
    s = res["series"][0]
    assert not is_err and s["unit"] == "fraction" and "fraction of the window" in s["note"]
    is_err, res = make(FakePost(body=data)).query(
        {"ids": ["pid_top_pv", "pid_top_output"], "start": "-6h", "fn": "count"})
    assert res["series"][1]["unit"] == "samples" and res["series"][1]["v"] == [120]


# ---------------------------------------------------------------------------
# available(), guards, default transport
# ---------------------------------------------------------------------------

def test_available_shape():
    av = make().available()
    assert av["pid_top_setpoint"] == {"measurement": "pid_state", "field": "setpoint_k",
                                      "type": "double", "topic": "xsphere/status/pid/top",
                                      "label": "Zone 2 (top) setpoint", "unit": "K"}
    assert av["level_cryostat"]["fields"] == ["filtered", "raw"]
    assert "gradient_mode" in av["unavailable"] and "gradient_mode" not in av
    assert "*" not in av["unavailable"]


def test_concurrency_and_rate_guards():
    h = make()
    for _ in range(hist.MAX_CONCURRENT_QUERIES):
        h._sem.acquire()
    try:
        is_err, res = h.query(OK)
        assert is_err and "still running" in res["error"]
    finally:
        for _ in range(hist.MAX_CONCURRENT_QUERIES):
            h._sem.release()
    h = make()
    for _ in range(hist.MAX_QUERIES_PER_MINUTE):
        assert not h.query(OK)[0]
    is_err, res = h.query(OK)
    assert is_err and "last minute" in res["error"]


class _FakeResp:
    def __init__(self, data, status=200):
        self._buf, self.status = io.BytesIO(data), status

    def read(self, n=-1):
        return self._buf.read(n)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeOpener:
    def __init__(self, resp=None, exc=None):
        self.resp, self.exc, self.requests = resp, exc, []

    def open(self, req, timeout=None):
        self.requests.append((req, timeout))
        if self.exc:
            raise self.exc
        return self.resp


def test_urllib_post_reads_and_caps():
    op = _FakeOpener(_FakeResp(b"a,b\r\n"))
    status, data = urllib_post("http://localhost:8087/api/v2/query?org=o",
                               {"Authorization": "Token t"}, b"{}", 20.0, opener=op)
    assert (status, data) == (200, b"a,b\r\n")
    req, timeout = op.requests[0]
    assert req.get_method() == "POST" and timeout == 20.0
    assert req.get_header("Authorization") == "Token t"

    big = b"x" * (hist.MAX_RESPONSE_BYTES + 1000)
    status, data = urllib_post("http://h/", {}, b"{}", 20.0, opener=_FakeOpener(_FakeResp(big)))
    assert len(data) == hist.MAX_RESPONSE_BYTES + 1


@pytest.fixture
def drip_server():
    """A local HTTP server answering POST with a chunked body: /drip sends 50 B
    every 0.3 s for ~3 s, /stall one chunk then nothing for 3 s, /fast all at once."""
    import http.server
    import threading
    import time as _time

    class Drip(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length") or 0))
            self.send_response(200)
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            try:
                if self.path == "/fast":
                    plan = [(b"a,b\r\n", 0.0)] * 3
                elif self.path == "/stall":
                    plan = [(b"x" * 50, 3.0)]
                else:
                    plan = [(b"x" * 50, 0.3)] * 10
                for chunk, gap in plan:
                    self.wfile.write(b"%x\r\n%s\r\n" % (len(chunk), chunk))
                    self.wfile.flush()
                    _time.sleep(gap)
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()
            except OSError:
                pass                                 # the client gave up: expected

        def log_message(self, *a):
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Drip)
    srv.daemon_threads = True
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        yield f"http://127.0.0.1:{srv.server_address[1]}"
    finally:
        srv.shutdown()
        srv.server_close()


@pytest.mark.parametrize("path", ["/drip", "/stall"])
def test_urllib_post_deadline_holds_for_a_slow_chunked_body(drip_server, path):
    """read(n) on a chunked body would keep reading until it had n bytes, each
    recv with its own socket timeout: a drip ran far past the deadline."""
    import time as _time
    t0 = _time.monotonic()
    with pytest.raises(TimeoutError, match="took longer than 1 s"):
        urllib_post(drip_server + path, {}, b"{}", 1.0)
    assert _time.monotonic() - t0 < 2.0


def test_urllib_post_reads_a_fast_chunked_body(drip_server):
    status, data = urllib_post(drip_server + "/fast", {}, b"{}", 5.0)
    assert (status, data) == (200, b"a,b\r\n" * 3)


def test_urllib_post_returns_http_error_body():
    exc = urllib.error.HTTPError("http://h/", 401, "Unauthorized", {},
                                 io.BytesIO(b'{"code":"unauthorized","message":"no"}'))
    status, data = urllib_post("http://h/", {}, b"{}", 20.0, opener=_FakeOpener(exc=exc))
    assert status == 401 and b"unauthorized" in data


def test_schema_constant_matches_validation():
    s = hist.QUERY_HISTORY_SCHEMA
    assert set(s["properties"]) == set(hist._ARG_KEYS)
    assert s["required"] == ["ids", "start"] and s["additionalProperties"] is False
    assert len(hist.QUERY_HISTORY_DESCRIPTION) <= 1500
