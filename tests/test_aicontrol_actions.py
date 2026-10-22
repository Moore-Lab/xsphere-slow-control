"""
Offline tests for the AI control service's behaviour: the write pipeline
(aicontrol/actions.py), arms / supervisor / LN2 timers / ramps
(aicontrol/arms.py), the read tools (aicontrol/reads.py), the tool registry
(aicontrol/tools.py) and the Flask app (aicontrol/app.py).

    python -m pytest tests/test_aicontrol_actions.py -q      (from the repo root)

No broker and no network: the service runs on LoopbackTransport, and
:class:`FakeSlowcontrol` answers the allowed command topics the way the real
drivers do (PID setpoint → ``setpoint_expr = repr(float(v))``; pv_expr →
``pv_expr_value_k`` from a sensor table; LN2 valve state and auto flags; NGE
request fields + ``desired`` and its event text for rejections; pneumatic
valves honouring the PLC gate and recording ``last_refused``).  Its snapshot
is built from the real ``slowcontrol/state.yaml`` ids with ``generated_at`` =
the fake clock.  Ages are driven by that clock; confirmation waits use real
time, so the service runs with short confirm timeouts here.
"""

import json
import re
from pathlib import Path

import pytest
import yaml

from aicontrol import __version__
from aicontrol.actions import (MAX_WRITES_PER_MIN, WRITE_SCHEMAS, parse_numeric_expr,
                               validate_args, ArgError)
from aicontrol.app import Service, create_app
from aicontrol.arms import Ln2Timers
from aicontrol.bus import LoopbackTransport, SNAPSHOT_TOPIC, is_allowed_command
from aicontrol.config import Settings
from aicontrol.rails import default_rails
from aicontrol.tools import INSTRUCTIONS, TOOL_TEXT, ToolRegistry
from aicontrol.zones import CHANNELS, VALVES, VESSELS, ZONES

REPO = Path(__file__).resolve().parent.parent
STATE_YAML = REPO / "slowcontrol" / "state.yaml"
T0 = 1_792_650_000.0


def _load_registry():
    with open(STATE_YAML, encoding="utf-8") as f:
        doc = yaml.safe_load(f)
    states = {}
    for group, items in doc["groups"].items():
        for sid, sdef in items.items():
            states[sid] = (group, sdef)
    return states, doc.get("derived") or {}


REGISTRY, DERIVED = _load_registry()


class Clock:
    def __init__(self, t=T0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, dt):
        self.t += dt


def iso(ts):
    from datetime import datetime, timezone
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(timespec="seconds")


# ──────────────────────────────────────────────────────────────────────────
# The fake slowcontrol
# ──────────────────────────────────────────────────────────────────────────

class FakeSlowcontrol:
    """Answers the AI's command topics like the drivers and publishes status."""

    def __init__(self, transport, clock):
        self.t = transport
        self.clock = clock
        self.respond = True
        self.polls_per_command = 2
        self.eval_fail = False
        self.reject_nge = False
        self.ladder_close = set()
        self.gate_internal = None          # None = follow the published gate
        self.omit_pneumatic_state = False
        self.on_command_hook = None
        self.commands = []
        self.last = {}                      # topic → last payload (for the snapshot)
        self.sensors = {"rtd1": 160.0, "rtd2": 165.0, "rtd3": 166.0,
                        "rtd4": 80.0, "rtd5": 165.5, "rtd6": 166.5}
        self.pid = {
            "top": self._pid("165.0", "rtd5"),
            "bottom": self._pid("166.0", "rtd6"),
            "nozzle": self._pid("160.0", "rtd1"),
        }
        self.valves = {v: {"state": 0, "desired": 0, "auto_close": 1, "auto_open": 0}
                       for v in VESSELS}
        self.pneumatic = {v: {"desired": 0, "state": 0, "relay": 0} for v in VALVES}
        self.gate = {"enabled": 1, "since_utc": iso(T0), "refused": 0}
        self.nge = {"connected": True, "hold": True, "event": None, "event_utc": None,
                    "max_voltage_v": 32.0, "max_current_a": 3.0}
        self.nge_ch = {ch: {"output": 1, "desired": 1, "armed": 1, "voltage_set_v": 21.5,
                            "voltage_request_v": 21.5, "current_limit_a": 1.5,
                            "current_request_a": 1.5, "voltage_v": 21.49, "current_a": 1.34,
                            "power_w": 28.8, "mode": "cv", "trip": "none", "dropouts": 0}
                       for ch in (1, 2, 3)}
        self.pv_interlock = {"min_k": 4.0, "max_k": 320.0, "tripped": False, "reasons": {}}
        self.interlocks = {"rules_active": [], "ok": True}
        self.sequencer = {"running": False, "current_step": None, "steps": [], "last_message": ""}
        self.trackers = []
        self.uptime = 5000
        self.heartbeat_alive = True
        self.esp32_alive = True
        self.levels = {"cryostat": 3.0, "primary_xe": 5.0, "ballast": 4.0}
        self.probe_v = 1.7
        self.stale_ids = set()
        transport.add_responder("xsphere/commands/#", self._on_command)

    @staticmethod
    def _pid(sp_expr, pv_expr):
        return {"setpoint_c": None, "setpoint_k": float(sp_expr), "pv_k": 0.0, "pv_c": None,
                "output_pct": 40.0, "kp": 1.0, "ki": 0.1, "kd": 0.0, "mode": "auto",
                "mode_cmd": 1.0, "mode_status_raw": 1.0, "controller_type": "pid",
                "setpoint_expr": sp_expr, "pv_expr": pv_expr,
                "setpoint_expr_value_k": float(sp_expr), "pv_expr_value_k": None}

    # publishing ------------------------------------------------------------

    def pub(self, topic, payload, retain=True):
        self.last[topic] = json.loads(json.dumps(payload))
        self.t.publish(topic, json.dumps(payload), qos=1, retain=retain)

    def alias_value(self, expr):
        if self.eval_fail:
            return None
        return self.sensors.get(expr.strip())

    def _sp_value(self, expr):
        num = parse_numeric_expr(expr)
        if num is not None:
            return num
        m = re.fullmatch(r"\s*(rtd\d)\s*\+\s*([0-9.]+)\s*", expr or "")
        if m and m.group(1) in self.sensors:
            return self.sensors[m.group(1)] + float(m.group(2))
        return None

    def publish_pid(self, z):
        p = self.pid[z]
        if p["pv_expr"]:
            val = self.alias_value(p["pv_expr"])
            p["pv_expr_value_k"] = val
            if val is not None:
                p["pv_k"] = val
        else:
            p["pv_expr_value_k"] = None
        if p["setpoint_expr"]:
            val = self._sp_value(p["setpoint_expr"])
            p["setpoint_expr_value_k"] = val
            if val is not None:
                p["setpoint_k"] = val
        else:
            p["setpoint_expr_value_k"] = None
        payload = dict(p)
        if self.pv_interlock["tripped"]:
            payload["setpoint_k"] = 3.0
            payload["pv_k"] = 500.0
            payload["output_pct"] = 0.0
        self.pub(f"xsphere/status/pid/{z}", payload)

    def publish_valve(self, v):
        self.pub(f"xsphere/status/valve/{v}", self.valves[v])

    def publish_pneumatic(self, v):
        payload = dict(self.pneumatic[v])
        if self.omit_pneumatic_state:
            payload.pop("state", None)
            payload.pop("relay", None)
        self.pub(f"xsphere/status/pneumatic/{v}", payload)

    def publish_gate(self):
        self.pub("xsphere/status/pneumatic_control", self.gate)

    def publish_nge(self, channels=(1, 2, 3)):
        self.pub("xsphere/status/nge", self.nge)
        for ch in channels:
            self.pub(f"xsphere/status/nge/{ch}", self.nge_ch[ch])

    def publish_sensors(self):
        for n in (1, 2, 3):
            self.last[f"xsphere/sensors/temperature/labjack/rtd/{n}"] = {
                "value_k": self.sensors[f"rtd{n}"], "resistance_ohm": 100.0}
        for n in (4, 5, 6):
            self.last[f"xsphere/sensors/temperature/plc/rtd/{n}"] = {
                "value_k": self.sensors[f"rtd{n}"]}
        for v, lv in self.levels.items():
            self.last[f"xsphere/sensors/level/{v}"] = {"raw": lv, "filtered": lv}
        self.last["xsphere/sensors/level_probe/cryostat"] = {
            "voltage_v": self.probe_v, "count": 400}

    def set_interlock(self, **kw):
        self.pv_interlock.update(kw)
        self.pub("xsphere/status/pv_interlock", self.pv_interlock)

    def set_sequencer(self, **kw):
        self.sequencer.update(kw)
        self.pub("xsphere/status/sequencer", self.sequencer)

    def set_trackers(self, trackers):
        self.trackers = trackers
        self.pub("xsphere/status/trackers", trackers)

    def heartbeat(self):
        self.pub("xsphere/status/service/heartbeat", {"uptime_s": self.uptime})

    def start(self, interlock=True):
        if interlock:
            self.pub("xsphere/status/pv_interlock", self.pv_interlock)
        self.pub("xsphere/status/sequencer", self.sequencer)
        self.pub("xsphere/status/trackers", self.trackers)
        self.pub("xsphere/status/interlocks", self.interlocks)
        self.poll()

    def poll(self, pid=True, valves=True, pneumatic=True, nge=True, snapshot=True):
        if pid:
            for z in ZONES:
                self.publish_pid(z)
        if valves:
            for v in VESSELS:
                self.publish_valve(v)
        if pneumatic:
            for v in VALVES:
                self.publish_pneumatic(v)
            self.publish_gate()
        if nge:
            self.publish_nge()
        self.heartbeat()
        if snapshot:
            self.publish_snapshot()

    # snapshot ----------------------------------------------------------------

    def _presence(self, topic):
        if topic == "xsphere/status/service/heartbeat":
            return self.heartbeat_alive
        if topic == "xsphere/status/ghs_esp32":
            return self.esp32_alive
        return topic in self.last

    def snapshot(self):
        self.publish_sensors()
        now = self.clock()
        states, values = {}, {}
        for sid, (group, sdef) in REGISTRY.items():
            src = sdef.get("source") or {}
            topic = src.get("topic")
            kind = sdef.get("kind", "analog")
            if src.get("presence"):
                value = bool(self._presence(topic))
                fresh = "fresh" if value else "invalid"
            else:
                payload = self.last.get(topic)
                value = None
                if isinstance(payload, dict):
                    keys = [src["key"]] if "key" in src else list(src.get("keys") or [])
                    for k in keys:
                        if payload.get(k) is not None:
                            value = payload[k]
                            break
                if value is not None and kind == "binary":
                    value = bool(value)
                fresh = "fresh" if value is not None else "invalid"
            if sid in self.stale_ids:
                fresh = "stale"
            values[sid] = value
            states[sid] = {"id": sid, "group": group, "kind": kind,
                           "label": sdef.get("label", sid), "unit": sdef.get("unit"),
                           "value": value, "freshness": fresh, "ts": now, "age_s": 0.1,
                           "source": topic, "seq": 1}
        for sid, d in DERIVED.items():
            names = re.findall(r"[A-Za-z_][A-Za-z0-9_]*", d["expr"])
            if all(isinstance(values.get(n), (int, float)) for n in names):
                value = eval(d["expr"], {"__builtins__": {}}, {n: values[n] for n in names})
                fresh = "fresh"
            else:
                value, fresh = None, "invalid"
            states[sid] = {"id": sid, "group": "derived", "kind": "analog",
                           "label": d.get("label", sid), "unit": d.get("unit"), "value": value,
                           "freshness": fresh, "ts": now, "age_s": 0.0, "source": "derived",
                           "seq": None}
        counts = {"fresh": 0, "stale": 0, "invalid": 0}
        for v in states.values():
            counts[v["freshness"]] += 1
        return {"generated_at": now, "counts": counts, "states": states}

    def publish_snapshot(self):
        self.t.publish(SNAPSHOT_TOPIC, json.dumps(self.snapshot()), qos=0, retain=True)

    # commands ---------------------------------------------------------------

    def _on_command(self, topic, payload, transport):
        self.commands.append((topic, payload))
        if self.on_command_hook is not None:
            self.on_command_hook(topic, payload)
        if not self.respond or not isinstance(payload, dict):
            return
        _x, _c, kind, name, what = topic.split("/")
        if kind == "pid":
            p = self.pid[name]
            if what == "setpoint":
                v = float(payload["value_k"])
                p["setpoint_expr"] = repr(v)
            elif what == "pv_expr":
                p["pv_expr"] = str(payload.get("expr", "")).strip()
            elif what == "mode":
                m = str(payload.get("mode", "")).lower()
                if m in ("auto", "manual"):
                    p["mode"] = m
            for _ in range(self.polls_per_command):
                self.publish_pid(name)
        elif kind == "valve":
            v = self.valves[name]
            if what == "state":
                v["desired"] = int(payload.get("state", 0))
                v["state"] = v["desired"]
                self.publish_valve(name)
                if name in self.ladder_close and v["state"] == 1:
                    v["desired"] = v["state"] = 0
                    self.publish_valve(name)
                    self.publish_valve(name)
                    return
            else:
                v[what] = int(bool(payload.get("enabled", False)))
            for _ in range(self.polls_per_command):
                self.publish_valve(name)
        elif kind == "nge":
            ch = int(name)
            c = self.nge_ch[ch]
            if what in ("voltage", "current"):
                key, unit, ceiling = (("value_v", "V", 32.0) if what == "voltage"
                                      else ("value_a", "A", 3.0))
                raw = payload.get(key)
                if self.reject_nge or isinstance(raw, bool) or not 0 <= float(raw) <= ceiling:
                    self.nge["event"] = f"ch{ch}: {raw!r} {unit} rejected — allowed 0 … " \
                                        f"{ceiling:g} {unit}"
                    self.nge["event_utc"] = iso(self.clock())
                elif what == "voltage":
                    c["voltage_request_v"] = round(float(raw), 3)
                    c["voltage_set_v"] = round(float(raw), 3)
                else:
                    c["current_request_a"] = round(float(raw), 4)
                if c["voltage_request_v"]:
                    c["current_limit_a"] = round(min(c["current_request_a"],
                                                     33.6 / c["voltage_request_v"]), 4)
            elif what == "output":
                raw = payload.get("state")
                if raw in (0, 1):
                    if not self.nge["connected"] and raw:
                        self.nge["event"] = f"ch{ch} ON ignored — the supply is not connected"
                        self.nge["event_utc"] = iso(self.clock())
                    else:
                        c["desired"] = c["output"] = raw
            self.publish_nge(channels=(ch,))
        elif kind == "pneumatic":
            raw = payload.get("state")
            if raw not in (0, 1):
                return
            gate_on = self.gate["enabled"] if self.gate_internal is None else self.gate_internal
            if not gate_on:
                self.gate["refused"] += 1
                self.gate["last_refused"] = {"valve": name, "state": int(raw),
                                             "utc": iso(self.clock())}
                self.publish_gate()
                return
            self.pneumatic[name]["desired"] = raw
            self.pneumatic[name]["state"] = raw
            for _ in range(self.polls_per_command):
                self.publish_pneumatic(name)


# ──────────────────────────────────────────────────────────────────────────
# Rig
# ──────────────────────────────────────────────────────────────────────────

def full_rails():
    doc = default_rails()
    for z in ZONES:
        doc["heaters"]["zones"][z] = {
            "setpoint_min_k": 150, "setpoint_max_k": 200, "max_step_k": 10,
            "max_rate_k_per_min": 5, "pv_sources": ["cube", "clamp"] if z != "nozzle"
            else ["cube"]}
    for ch in CHANNELS:
        doc["heaters"]["supply"][ch] = {
            "voltage_min_v": 10, "voltage_max_v": 24, "current_min_a": 0.5,
            "current_max_a": 2.0, "may_switch_on": True, "may_switch_off": True}
    for v in VESSELS:
        doc["ln2"]["vessels"][v] = {"may_operate": True, "max_open_minutes": 1,
                                    "may_set_auto_open": True, "may_disarm_auto_close": True}
    for v in VALVES:
        doc["pneumatic"]["valves"][v] = {"may_open": True, "may_close": True}
    doc["pneumatic"]["forbid_open_together"] = [["pump", "bottle"]]
    doc["pneumatic"]["min_interval_s"] = 10
    return doc


class Rig:
    def __init__(self, tmp_path, *, rails=True, interlock=True, history=None):
        self.clock = Clock()
        self.transport = LoopbackTransport()
        self.settings = Settings(state_dir=tmp_path)
        self.svc = Service(self.settings, self.transport, clock=self.clock, history=history,
                           confirm_timeout_s=0.3, nge_timeout_s=0.3)
        self.fake = FakeSlowcontrol(self.transport, self.clock)
        self.svc.start(threads=False)
        # A full token bucket: the service has been up for 10 minutes.
        self.clock.advance(600)
        self.fake.start(interlock=interlock)
        if rails:
            self.set_rails(full_rails())
        self.transport.clear()

    # helpers
    @property
    def actions(self):
        return self.svc.actions

    def set_rails(self, doc):
        ok, err, _ = self.svc.rails.replace(doc, self.svc.rails.revision, "test")
        assert ok, err

    def arm(self, *groups, armed=True):
        for g in groups:
            self.transport.inject(f"{self.settings.cmd_prefix}/arm",
                                  {"group": g, "armed": armed, "by": "pytest"})

    def call(self, tool, args):
        return self.svc.registry.call(tool, args)

    def cmds(self):
        return [(t, p) for t, p, _q, _r in self.transport.published_to("xsphere/commands/#")]

    def advance(self, dt, poll=True):
        self.clock.advance(dt)
        if poll:
            self.fake.poll()

    def audit_kinds(self):
        return [e["kind"] for e in self.svc.audit.recent(500)][::-1]


@pytest.fixture
def rig(tmp_path):
    return Rig(tmp_path)


def assert_refused(res, *needles):
    is_error, out = res
    assert is_error is True, out
    assert out["ok"] is False
    text = out.get("refused") or out.get("error") or ""
    for n in needles:
        assert n in text, f"{n!r} not in {text!r}"
    return out


def assert_ok(res):
    is_error, out = res
    assert is_error is False, out
    assert out["ok"] is True
    return out


# ──────────────────────────────────────────────────────────────────────────
# Argument validation
# ──────────────────────────────────────────────────────────────────────────

GOOD = {
    "set_heater_setpoints": {"setpoints": {"top": 170.0}, "reason": "r"},
    "set_heater_gradient": {"base_k": 165, "dv_k": 1, "dl_k": -5, "reason": "r"},
    "start_heater_ramp": {"targets": {"top": 170}, "rate_k_per_min": 1, "reason": "r"},
    "stop_heater_ramp": {"reason": "r"},
    "set_heater_auto": {"zone": "top", "reason": "r"},
    "set_pv_source": {"zone": "top", "source": "cube", "reason": "r"},
    "set_heater_supply": {"channel": 1, "voltage_v": 20, "reason": "r"},
    "set_ln2_valve": {"vessel": "cryostat", "open": False, "reason": "r"},
    "set_ln2_auto": {"vessel": "cryostat", "auto_close": True, "reason": "r"},
    "set_pneumatic_valve": {"valve": "pump", "open": True, "reason": "r"},
    "add_note": {"text": "hello"},
}

BAD = [
    ("set_heater_setpoints", {"setpoints": {"top": True}, "reason": "r"}, "top: must be a finite"),
    ("set_heater_setpoints", {"setpoints": {"top": "170"}, "reason": "r"}, "must be a finite"),
    ("set_heater_setpoints", {"setpoints": {"top": float("nan")}, "reason": "r"}, "finite"),
    ("set_heater_setpoints", {"setpoints": {"top": float("inf")}, "reason": "r"}, "finite"),
    ("set_heater_setpoints", {"setpoints": {"top": 10 ** 400}, "reason": "r"}, "finite"),
    ("set_heater_setpoints", {"setpoints": {"top": 5e6}, "reason": "r"}, "out of range"),
    ("set_heater_setpoints", {"setpoints": {"top": 500}, "reason": "r"}, "≤ 400"),
    ("set_heater_setpoints", {"setpoints": {}, "reason": "r"}, "at least 1"),
    ("set_heater_setpoints", {"setpoints": {"middle": 170}, "reason": "r"}, "unknown key"),
    ("set_heater_setpoints", {"setpoints": {"top": 170}}, "missing required key \"reason\""),
    ("set_heater_setpoints", {"setpoints": {"top": 170}, "reason": "   "}, "must not be empty"),
    ("set_heater_setpoints", {"setpoints": {"top": 170}, "reason": "x" * 201}, "at most 200"),
    ("set_heater_setpoints", {"setpoints": {"top": 170}, "reason": 5}, "must be a string"),
    ("set_heater_setpoints", {"setpoints": {"top": 170}, "reason": "r", "force": True},
     "unknown key \"force\""),
    ("set_heater_setpoints", {"setpoints": {"top": 170}, "reason": "r",
                              "replace_expression": 1}, "true or false"),
    ("set_heater_supply", {"channel": 1.0, "voltage_v": 20, "reason": "r"}, "an integer"),
    ("set_heater_supply", {"channel": True, "voltage_v": 20, "reason": "r"}, "an integer"),
    ("set_heater_supply", {"channel": 4, "voltage_v": 20, "reason": "r"}, "one of 1, 2, 3"),
    ("set_heater_supply", {"channel": 1, "reason": "r"}, "at least 3"),
    ("set_heater_supply", {"channel": 1, "output": 1, "reason": "r"}, "true or false"),
    ("set_ln2_valve", {"vessel": "Cryostat", "open": True, "reason": "r"}, "one of"),
    ("set_ln2_valve", {"vessel": "cryostat", "open": "true", "reason": "r"}, "true or false"),
    ("set_ln2_auto", {"vessel": "cryostat", "reason": "r"}, "at least 3"),
    ("start_heater_ramp", {"targets": {"top": 170}, "rate_k_per_min": 0, "reason": "r"}, "> 0"),
    ("stop_heater_ramp", {"zones": ["top", "top"], "reason": "r"}, "listed twice"),
    ("set_pneumatic_valve", {"valve": "pump", "open": None, "reason": "r"}, "true or false"),
    ("add_note", {"text": "x" * 1001}, "at most 1000"),
    ("set_heater_auto", [], "must be an object"),
]


@pytest.mark.parametrize("tool,args,needle", BAD)
def test_invalid_arguments_are_refused_and_audited(rig, tool, args, needle):
    rig.arm("heaters", "ln2", "pneumatic")
    is_error, out = rig.call(tool, args)
    assert is_error is True and out["ok"] is False and out["invalid_arguments"] is True
    assert needle in out["error"], out["error"]
    assert rig.cmds() == []
    last = rig.svc.audit.recent(1)[0]
    assert last["kind"] == "refused" and last["tool"] == tool
    assert rig.svc.actions.stats()["refused"] >= 1


@pytest.mark.parametrize("tool", sorted(GOOD))
def test_good_arguments_pass_validation(tool):
    validate_args(WRITE_SCHEMAS[tool], GOOD[tool])


@pytest.mark.parametrize("expr,value", [
    ("165.0", 165.0), ("165", 165.0), ("-3", -3.0), ("+5.5", 5.5), (" 170.25 ", 170.25),
    ("1_000", 1000.0), ("nan", None), ("inf", None), ("1e999", None), ("-1e999", None),
    ("True", None), ("rtd5", None), ("rtd5 + 10", None), ("--5", None), ("1j", None),
    ("165.0)", None), ("", None), ("   ", None), (None, None), (165.0, None),
    ("(165)", 165.0), ("x" * 100, None),
])
def test_parse_numeric_expr_grammar(expr, value):
    assert parse_numeric_expr(expr) == value


def test_validate_args_returns_a_copy():
    args = {"setpoints": {"top": 170}, "reason": "r"}
    out = validate_args(WRITE_SCHEMAS["set_heater_setpoints"], args)
    out["setpoints"]["top"] = 1
    assert args["setpoints"]["top"] == 170
    with pytest.raises(ArgError):
        validate_args(WRITE_SCHEMAS["set_heater_setpoints"], {"setpoints": {"top": 170},
                                                              "reason": None})


# ──────────────────────────────────────────────────────────────────────────
# Generic refusals
# ──────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("tool,group", [
    ("set_heater_setpoints", "Heaters"), ("set_heater_gradient", "Heaters"),
    ("start_heater_ramp", "Heaters"), ("set_heater_auto", "Heaters"),
    ("set_pv_source", "Heaters"), ("set_heater_supply", "Heaters"),
    ("set_ln2_valve", "LN2 valves"), ("set_ln2_auto", "LN2 valves"),
    ("set_pneumatic_valve", "Pneumatic valves")])
def test_disarmed_refuses_every_write(rig, tool, group):
    out = assert_refused(rig.call(tool, GOOD[tool]), f"{group} are not armed",
                         "http://xbox-pi:8088/control")
    assert out["published"] == [] and rig.cmds() == []
    assert rig.svc.audit.recent(1)[0]["kind"] == "refused"


def test_refusal_names_the_last_disarm(rig):
    rig.arm("heaters")
    rig.arm("heaters", armed=False)
    assert_refused(rig.call("set_heater_setpoints", GOOD["set_heater_setpoints"]),
                   "Last disarmed", "disarmed by pytest")


def test_rails_unset_refuses(tmp_path):
    rig = Rig(tmp_path, rails=False)
    rig.arm("heaters", "ln2", "pneumatic")
    assert_refused(rig.call("set_heater_setpoints", GOOD["set_heater_setpoints"]),
                   "has not set setpoint rails")
    assert_refused(rig.call("set_heater_supply", GOOD["set_heater_supply"]),
                   "has not set voltage rails")
    assert_refused(rig.call("set_ln2_valve", GOOD["set_ln2_valve"]), "may_operate is false")
    assert_refused(rig.call("set_pneumatic_valve", GOOD["set_pneumatic_valve"]),
                   "may_open is false")
    assert_refused(rig.call("set_pv_source", GOOD["set_pv_source"]), "pv_sources")
    assert rig.cmds() == []


def test_rails_invalid_refuses_everything(rig):
    rig.svc.rails.path.write_text("{not json", encoding="utf-8")
    rig.svc.rails.load()
    rig.arm("heaters", "ln2", "pneumatic")
    for tool in ("set_heater_setpoints", "set_heater_supply", "set_ln2_valve",
                 "set_pneumatic_valve", "set_pv_source", "set_heater_auto"):
        assert_refused(rig.call(tool, GOOD[tool]), "rails file is invalid")
    assert rig.cmds() == []
    _e, ov = rig.call("get_overview", {})
    assert "rails file is invalid" in ov


def test_stale_fingerprint_refuses_that_zone_only(rig):
    doc = json.loads(rig.svc.rails.path.read_text(encoding="utf-8"))
    doc["zone_fingerprints"]["top"] = "cube=xsphere/sensors/temperature/labjack/rtd/3|clamp=-"
    rig.svc.rails.path.write_text(json.dumps(doc), encoding="utf-8")
    rig.svc.rails.load()
    assert rig.svc.rails.stale_zones == ["top"]
    rig.arm("heaters")
    assert_refused(rig.call("set_heater_setpoints", {"setpoints": {"top": 166}, "reason": "r"}),
                   "zone top", "zone mapping changed")
    assert_refused(rig.call("set_pv_source", GOOD["set_pv_source"]), "zone mapping changed")
    assert rig.cmds() == []
    assert_ok(rig.call("set_heater_setpoints", {"setpoints": {"bottom": 167}, "reason": "r"}))


def test_second_write_while_one_is_in_progress_is_refused(rig):
    rig.arm("heaters")
    assert rig.actions.write_lock.acquire(blocking=False)
    try:
        assert_refused(rig.call("set_heater_setpoints", GOOD["set_heater_setpoints"]),
                       "another AI write is in progress")
    finally:
        rig.actions.write_lock.release()
    assert rig.cmds() == []


def test_disconnected_refuses(rig):
    rig.arm("heaters")
    rig.transport.set_connected(False)
    assert_refused(rig.call("set_heater_setpoints", GOOD["set_heater_setpoints"]),
                   "not connected to the MQTT broker")


def test_stale_snapshot_refuses(rig):
    rig.arm("heaters")
    rig.clock.advance(10)
    rig.fake.poll(snapshot=False)
    assert_refused(rig.call("set_heater_setpoints", GOOD["set_heater_setpoints"]),
                   "snapshot is 10 s old")


def test_no_heartbeat_refuses(rig):
    rig.arm("heaters")
    rig.fake.heartbeat_alive = False
    rig.fake.poll()
    assert_refused(rig.call("set_heater_setpoints", GOOD["set_heater_setpoints"]),
                   "service_alive")


def test_sequencer_running_refuses(rig):
    rig.arm("heaters", "ln2", "pneumatic")
    rig.fake.set_sequencer(running=True, current_step=2)
    for tool in ("set_heater_setpoints", "set_ln2_valve", "set_pneumatic_valve"):
        assert_refused(rig.call(tool, GOOD[tool]), "Sequencer is running")
    rig.fake.set_sequencer(running=False)
    assert_ok(rig.call("set_heater_setpoints", GOOD["set_heater_setpoints"]))


def test_enabled_tracker_refuses_and_names_it(rig):
    rig.arm("heaters")
    rig.fake.set_trackers([{"id": "seq:pid_top_setpoint_expr", "target": "pid_top_setpoint_expr",
                            "source": "t_cube_top", "enabled": True}])
    assert_refused(rig.call("set_heater_setpoints", GOOD["set_heater_setpoints"]),
                   "seq:pid_top_setpoint_expr", "tracker")
    rig.fake.set_trackers([{"id": "t1", "target": "x", "source": "y", "enabled": False}])
    assert_ok(rig.call("set_heater_setpoints", GOOD["set_heater_setpoints"]))


def test_unreadable_tracker_status_refuses(rig):
    rig.arm("heaters")
    rig.fake.pub("xsphere/status/trackers", "garbage")
    assert_refused(rig.call("set_heater_setpoints", GOOD["set_heater_setpoints"]),
                   "trackers status is unreadable")


def test_global_write_rate_limit(rig):
    rig.arm("heaters")
    rig.actions._write_times.extend([rig.clock()] * MAX_WRITES_PER_MIN)
    assert_refused(rig.call("set_heater_setpoints", GOOD["set_heater_setpoints"]),
                   "at most 20 AI write calls a minute")
    rig.clock.advance(61)
    rig.fake.poll()
    assert_ok(rig.call("set_heater_setpoints", GOOD["set_heater_setpoints"]))


# ──────────────────────────────────────────────────────────────────────────
# Heater setpoints
# ──────────────────────────────────────────────────────────────────────────

def test_setpoint_happy_path_exact_publish_and_confirm(rig):
    rig.arm("heaters")
    out = assert_ok(rig.call("set_heater_setpoints",
                             {"setpoints": {"top": 170}, "reason": "warm up a little"}))
    assert rig.transport.published_to("xsphere/commands/#") == [
        ("xsphere/commands/pid/top/setpoint", {"value_k": 170.0}, 0, False)]
    assert out["published"] == [{"topic": "xsphere/commands/pid/top/setpoint",
                                 "payload": {"value_k": 170.0}}]
    assert out["confirmed"] is True
    assert out["readback"]["top"]["setpoint_expr"] == "170.0"
    assert out["readback"]["top"]["setpoint_k"] == 170.0
    assert out["readback"]["top"]["previous_setpoint_k"] == 165.0
    assert out["warnings"] == []
    e = rig.svc.audit.recent(1)[0]
    assert e["kind"] == "action" and e["tool"] == "set_heater_setpoints"
    assert e["reason"] == "warm up a little" and e["confirmed"] is True
    assert "top 165 → 170 K" in e["detail"]
    st = rig.svc.actions.stats()
    assert st["actions"] == 1 and st["last_action"]["tool"] == "set_heater_setpoints"


def test_all_zones_validated_before_any_is_published(rig):
    rig.arm("heaters")
    assert_refused(rig.call("set_heater_setpoints",
                            {"setpoints": {"top": 170, "bottom": 230}, "reason": "r"}),
                   "zone bottom", "outside Lucas's rails 150–200 K")
    assert rig.cmds() == []


def test_setpoint_outside_rails_refused(rig):
    rig.arm("heaters")
    assert_refused(rig.call("set_heater_setpoints", {"setpoints": {"top": 149}, "reason": "r"}),
                   "outside Lucas's rails")


def test_max_step(rig):
    rig.arm("heaters")
    assert_refused(rig.call("set_heater_setpoints", {"setpoints": {"top": 180}, "reason": "r"}),
                   "max_step_k is 10 K")
    assert_ok(rig.call("set_heater_setpoints", {"setpoints": {"top": 175}, "reason": "r"}))


def test_rate_token_bucket(rig):
    rig.arm("heaters")
    assert_ok(rig.call("set_heater_setpoints", {"setpoints": {"top": 170}, "reason": "r"}))
    rig.advance(30)                       # 0.5 min × 5 K/min = 2.5 K allowed
    assert_refused(rig.call("set_heater_setpoints", {"setpoints": {"top": 173}, "reason": "r"}),
                   "exceeds the rate allowance of 2.50 K")
    assert_ok(rig.call("set_heater_setpoints", {"setpoints": {"top": 172.5}, "reason": "r"}))
    rig.advance(600)                      # bucket capped at max_step 10 K
    assert_refused(rig.call("set_heater_setpoints", {"setpoints": {"top": 183}, "reason": "r"}),
                   "max_step_k")
    assert_ok(rig.call("set_heater_setpoints", {"setpoints": {"top": 182.5}, "reason": "r"}))


def test_first_change_bucket_counts_from_service_start(tmp_path):
    rig = Rig(tmp_path)
    rig.svc.actions.started_ts = rig.clock()            # service "just started"
    rig.arm("heaters")
    rig.advance(12)                                     # 0.2 min × 5 = 1 K
    assert_refused(rig.call("set_heater_setpoints", {"setpoints": {"top": 167}, "reason": "r"}),
                   "since the service started")
    assert_ok(rig.call("set_heater_setpoints", {"setpoints": {"top": 166}, "reason": "r"}))


def test_move_into_rails_from_outside_ignores_step_and_rate(rig):
    rig.arm("heaters")
    p = rig.fake.pid["top"]
    p["setpoint_expr"], p["setpoint_k"] = "", 3.0        # after a slowcontrol restart
    rig.fake.poll()
    rig.svc.actions.started_ts = rig.clock()             # empty bucket
    out = assert_ok(rig.call("set_heater_setpoints", {"setpoints": {"top": 165}, "reason": "r"}))
    assert out["readback"]["top"]["previous_setpoint_k"] == 3.0
    assert rig.cmds() == [("xsphere/commands/pid/top/setpoint", {"value_k": 165.0})]


def test_interlock_tripped_refuses(rig):
    rig.fake.set_interlock(tripped=True, reasons={"2": "stale 12 s"})
    rig.fake.poll()
    rig.arm("heaters")                      # re-armed by a human while still tripped
    assert_refused(rig.call("set_heater_setpoints", GOOD["set_heater_setpoints"]),
                   "PV interlock is tripped", "RTD2: stale 12 s")
    assert rig.cmds() == []


def test_interlock_band_margin(rig):
    rig.arm("heaters")
    rig.fake.set_interlock(max_k=178.0)
    assert_refused(rig.call("set_heater_setpoints", {"setpoints": {"top": 174}, "reason": "r"}),
                   "PV interlock band", "173")
    assert_ok(rig.call("set_heater_setpoints", {"setpoints": {"top": 173}, "reason": "r"}))


def test_interlock_never_seen_refuses(tmp_path):
    rig = Rig(tmp_path, interlock=False)
    rig.arm("heaters")
    assert_refused(rig.call("set_heater_setpoints", GOOD["set_heater_setpoints"]),
                   "PV interlock status")


def test_increase_needs_a_fed_pv_but_decrease_does_not(rig):
    rig.arm("heaters")
    p = rig.fake.pid["top"]
    p["pv_expr"], p["pv_k"] = "", 500.0
    rig.fake.poll()
    assert_refused(rig.call("set_heater_setpoints", {"setpoints": {"top": 170}, "reason": "r"}),
                   "PID top sees 500 K — no live PV source", "set_pv_source")
    assert_ok(rig.call("set_heater_setpoints", {"setpoints": {"top": 160}, "reason": "r"}))


def test_increase_refused_when_pv_expr_does_not_evaluate(rig):
    rig.arm("heaters")
    rig.fake.eval_fail = True
    rig.fake.poll()
    assert_refused(rig.call("set_heater_setpoints", {"setpoints": {"top": 170}, "reason": "r"}),
                   "does not evaluate")


def test_pid_status_must_be_live(rig):
    rig.arm("heaters")
    rig.clock.advance(4)
    rig.fake.poll(pid=False)
    assert_refused(rig.call("set_heater_setpoints", GOOD["set_heater_setpoints"]),
                   "no live PID status", "for 4 s")


def test_retained_pid_status_is_not_live(rig):
    rig.arm("heaters")
    rig.clock.advance(4)
    rig.transport.inject("xsphere/status/pid/top", rig.fake.pid["top"], retain=True)
    rig.fake.poll(pid=False)
    assert_refused(rig.call("set_heater_setpoints", GOOD["set_heater_setpoints"]),
                   "no live PID status")


def test_non_numeric_expression_needs_replace_expression(rig):
    rig.arm("heaters")
    rig.fake.pid["top"]["setpoint_expr"] = "rtd5 + 2"        # = 167.5 K
    rig.fake.poll()
    assert_refused(rig.call("set_heater_setpoints", {"setpoints": {"top": 170}, "reason": "r"}),
                   "expression 'rtd5 + 2'", "replace_expression")
    assert rig.cmds() == []
    out = assert_ok(rig.call("set_heater_setpoints", {"setpoints": {"top": 170}, "reason": "r",
                                                      "replace_expression": True}))
    assert any("replaced the setpoint expression 'rtd5 + 2'" in w for w in out["warnings"])
    assert out["readback"]["top"]["previous_expr"] == "rtd5 + 2"
    assert out["readback"]["top"]["previous_setpoint_k"] == 167.5


@pytest.mark.parametrize("expr", ["nan", "inf", "1e999"])
def test_float_lookalike_expressions_are_not_numbers(rig, expr):
    rig.arm("heaters")
    rig.fake.pid["top"]["setpoint_expr"] = expr
    rig.fake.poll()
    assert_refused(rig.call("set_heater_setpoints", {"setpoints": {"top": 170}, "reason": "r"}),
                   "not a number")


def test_not_confirmed_is_reported(rig):
    rig.arm("heaters")
    rig.fake.respond = False
    out = assert_ok(rig.call("set_heater_setpoints", GOOD["set_heater_setpoints"]))
    assert out["confirmed"] is False
    assert any("not confirmed" in w for w in out["warnings"])
    assert rig.cmds() == [("xsphere/commands/pid/top/setpoint", {"value_k": 170.0})]
    assert "NOT confirmed" in rig.svc.audit.recent(1)[0]["detail"]


def test_confirmation_needs_two_consecutive_messages(rig):
    rig.arm("heaters")
    rig.fake.polls_per_command = 1
    out = assert_ok(rig.call("set_heater_setpoints", GOOD["set_heater_setpoints"]))
    assert out["confirmed"] is False
    rig.fake.polls_per_command = 2
    rig.advance(30)
    out = assert_ok(rig.call("set_heater_setpoints", {"setpoints": {"top": 171}, "reason": "r"}))
    assert out["confirmed"] is True


def test_manual_mode_gives_a_warning(rig):
    rig.arm("heaters")
    rig.fake.pid["top"]["mode"] = "manual"
    rig.fake.poll()
    out = assert_ok(rig.call("set_heater_setpoints", GOOD["set_heater_setpoints"]))
    assert any("has no effect until the zone is in auto" in w for w in out["warnings"])


def test_disarm_mid_call_stops_the_remaining_publishes(rig):
    rig.arm("heaters")

    def hook(topic, payload):
        if topic.endswith("/top/setpoint"):
            rig.svc.arms.disarm("heaters", "test pulled the arm")
    rig.fake.on_command_hook = hook
    out = assert_refused(rig.call("set_heater_setpoints",
                                  {"setpoints": {"top": 170, "bottom": 170}, "reason": "r"}),
                         "disarmed during the call", "1 command(s) had already been sent")
    assert [t for t, _p in rig.cmds()] == ["xsphere/commands/pid/top/setpoint"]
    assert len(out["published"]) == 1


def test_gradient_publishes_three_setpoints(rig):
    rig.arm("heaters")
    out = assert_ok(rig.call("set_heater_gradient",
                             {"base_k": 166, "dv_k": 1.5, "dl_k": -4, "reason": "r"}))
    assert rig.cmds() == [("xsphere/commands/pid/top/setpoint", {"value_k": 166.0}),
                          ("xsphere/commands/pid/bottom/setpoint", {"value_k": 167.5}),
                          ("xsphere/commands/pid/nozzle/setpoint", {"value_k": 162.0})]
    assert out["confirmed"] is True


def test_gradient_refused_as_a_whole(rig):
    rig.arm("heaters")
    assert_refused(rig.call("set_heater_gradient",
                            {"base_k": 166, "dv_k": 1, "dl_k": -40, "reason": "r"}),
                   "zone nozzle")
    assert rig.cmds() == []


# ──────────────────────────────────────────────────────────────────────────
# Ramps
# ──────────────────────────────────────────────────────────────────────────

def test_ramp_steps_to_target_through_the_validated_path(rig):
    rig.arm("heaters")
    out = assert_ok(rig.call("start_heater_ramp",
                             {"targets": {"top": 167}, "rate_k_per_min": 1, "reason": "slow"}))
    assert out["published"] == [] and rig.cmds() == []
    assert [r["zone"] for r in rig.svc.ramps.status()] == ["top"]
    rig.advance(60)
    rig.svc.ramps.tick()
    assert rig.cmds() == [("xsphere/commands/pid/top/setpoint", {"value_k": 166.0})]
    rig.advance(30)
    rig.svc.ramps.tick()
    assert rig.cmds()[-1] == ("xsphere/commands/pid/top/setpoint", {"value_k": 166.5})
    rig.advance(60)
    rig.svc.ramps.tick()
    assert rig.cmds()[-1] == ("xsphere/commands/pid/top/setpoint", {"value_k": 167.0})
    rig.advance(10)
    rig.svc.ramps.tick()
    assert rig.svc.ramps.status() == []
    assert "reached 167 K" in rig.svc.audit.recent(1)[0]["detail"]
    assert all(is_allowed_command(t) for t, _p in rig.cmds())


def test_ramp_rate_above_zone_max_rate_refused(rig):
    rig.arm("heaters")
    assert_refused(rig.call("start_heater_ramp",
                            {"targets": {"top": 180}, "rate_k_per_min": 6, "reason": "r"}),
                   "faster than Lucas's max_rate_k_per_min 5 K/min")


def test_ramp_target_outside_rails_or_band_refused(rig):
    rig.arm("heaters")
    assert_refused(rig.call("start_heater_ramp",
                            {"targets": {"top": 210}, "rate_k_per_min": 1, "reason": "r"}),
                   "outside Lucas's rails")
    rig.fake.set_interlock(max_k=180.0)
    assert_refused(rig.call("start_heater_ramp",
                            {"targets": {"top": 178}, "rate_k_per_min": 1, "reason": "r"}),
                   "PV interlock band")


def test_ramp_from_outside_the_rails_refused(rig):
    rig.arm("heaters")
    rig.fake.pid["top"]["setpoint_expr"], rig.fake.pid["top"]["setpoint_k"] = "", 3.0
    rig.fake.poll()
    assert_refused(rig.call("start_heater_ramp",
                            {"targets": {"top": 170}, "rate_k_per_min": 1, "reason": "r"}),
                   "outside the rails")


def test_ramp_stops_on_external_change(rig):
    rig.arm("heaters")
    assert_ok(rig.call("start_heater_ramp",
                       {"targets": {"top": 175}, "rate_k_per_min": 1, "reason": "r"}))
    rig.advance(60)
    rig.svc.ramps.tick()
    assert len(rig.cmds()) == 1
    rig.fake.pid["top"]["setpoint_expr"] = "180.0"        # a human on the Control page
    rig.advance(10)
    rig.svc.ramps.tick()
    assert rig.svc.ramps.status() == []
    assert len(rig.cmds()) == 1
    e = rig.svc.audit.recent(1)[0]
    assert e["kind"] == "ramp" and "changed by someone else" in e["detail"]


def test_ramp_stops_when_change_before_first_step(rig):
    rig.arm("heaters")
    assert_ok(rig.call("start_heater_ramp",
                       {"targets": {"top": 175}, "rate_k_per_min": 1, "reason": "r"}))
    rig.fake.pid["top"]["setpoint_expr"] = "170.0"
    rig.advance(60)
    rig.svc.ramps.tick()
    assert rig.svc.ramps.status() == [] and rig.cmds() == []


def test_ramp_stops_on_disarm(rig):
    rig.arm("heaters")
    assert_ok(rig.call("start_heater_ramp",
                       {"targets": {"top": 175}, "rate_k_per_min": 1, "reason": "r"}))
    rig.arm("heaters", armed=False)
    assert rig.svc.ramps.status() == []
    rig.advance(60)
    rig.svc.ramps.tick()
    assert rig.cmds() == []
    assert any("heaters disarmed" in e["detail"] for e in rig.svc.audit.recent(5))


def test_ramp_stops_on_interlock_trip(rig):
    rig.arm("heaters")
    assert_ok(rig.call("start_heater_ramp",
                       {"targets": {"top": 175}, "rate_k_per_min": 1, "reason": "r"}))
    rig.fake.set_interlock(tripped=True, reasons={"1": "above max"})
    assert rig.svc.ramps.status() == []      # the trip disarmed heaters, which stops ramps
    rig.advance(60)
    rig.svc.ramps.tick()
    assert rig.cmds() == []


def test_ramp_step_refused_stops_the_ramp(rig):
    rig.arm("heaters")
    assert_ok(rig.call("start_heater_ramp",
                       {"targets": {"top": 175}, "rate_k_per_min": 1, "reason": "r"}))
    rig.fake.pid["top"].update(pv_expr="", pv_k=500.0)      # PV source lost meanwhile
    rig.advance(60)
    rig.svc.ramps.tick()
    assert rig.svc.ramps.status() == [] and rig.cmds() == []
    assert "no live PV source" in rig.svc.audit.recent(1)[0]["detail"]


def test_ramp_waits_while_a_write_holds_the_lock(rig):
    rig.arm("heaters")
    assert_ok(rig.call("start_heater_ramp",
                       {"targets": {"top": 175}, "rate_k_per_min": 1, "reason": "r"}))
    rig.advance(60)
    with rig.actions.write_lock:
        rig.svc.ramps.tick()
    assert rig.cmds() == [] and len(rig.svc.ramps.status()) == 1
    rig.svc.ramps.tick()
    assert len(rig.cmds()) == 1


def test_direct_setpoint_cancels_the_ramp(rig):
    rig.arm("heaters")
    assert_ok(rig.call("start_heater_ramp",
                       {"targets": {"top": 175, "bottom": 170}, "rate_k_per_min": 1,
                        "reason": "r"}))
    assert_ok(rig.call("set_heater_setpoints", {"setpoints": {"top": 166}, "reason": "r"}))
    assert [r["zone"] for r in rig.svc.ramps.status()] == ["bottom"]


def test_stop_heater_ramp_needs_no_arm(rig):
    rig.arm("heaters")
    assert_ok(rig.call("start_heater_ramp",
                       {"targets": {"top": 175, "bottom": 170}, "rate_k_per_min": 1,
                        "reason": "r"}))
    rig.svc.arms._flags["heaters"] = False      # disarmed without the listener (worst case)
    out = assert_ok(rig.call("stop_heater_ramp", {"zones": ["top"], "reason": "enough"}))
    assert out["readback"]["stopped"][0]["zone"] == "top"
    assert [r["zone"] for r in rig.svc.ramps.status()] == ["bottom"]
    out = assert_ok(rig.call("stop_heater_ramp", {"reason": "all"}))
    assert rig.svc.ramps.status() == []
    out = assert_ok(rig.call("stop_heater_ramp", {"reason": "again"}))
    assert out["note"].startswith("no ramp was running")
    assert rig.svc.audit.recent(1)[0]["kind"] == "action"


# ──────────────────────────────────────────────────────────────────────────
# set_heater_auto / set_pv_source
# ──────────────────────────────────────────────────────────────────────────

def test_heater_auto_from_manual(rig):
    rig.arm("heaters")
    rig.fake.pid["top"]["mode"] = "manual"
    rig.fake.poll()
    out = assert_ok(rig.call("set_heater_auto", {"zone": "top", "reason": "r"}))
    assert rig.cmds() == [("xsphere/commands/pid/top/mode", {"mode": "auto"})]
    assert out["confirmed"] is True and out["readback"]["top"]["mode"] == "auto"


def test_heater_auto_already_auto_sends_nothing(rig):
    rig.arm("heaters")
    out = assert_ok(rig.call("set_heater_auto", {"zone": "top", "reason": "r"}))
    assert rig.cmds() == [] and "already in auto" in out["note"]


@pytest.mark.parametrize("expr", ["", "rtd5 + 2", "nan"])
def test_heater_auto_needs_a_numeric_setpoint(rig, expr):
    rig.arm("heaters")
    rig.fake.pid["top"].update(mode="manual", setpoint_expr=expr)
    rig.fake.poll()
    assert_refused(rig.call("set_heater_auto", {"zone": "top", "reason": "r"}),
                   "set a setpoint first")


def test_heater_auto_setpoint_outside_rails_refused(rig):
    rig.arm("heaters")
    rig.fake.pid["top"].update(mode="manual", setpoint_expr="250.0")
    rig.fake.poll()
    assert_refused(rig.call("set_heater_auto", {"zone": "top", "reason": "r"}),
                   "outside Lucas's rails")


def test_heater_auto_needs_pv(rig):
    rig.arm("heaters")
    rig.fake.pid["top"].update(mode="manual", pv_expr="", pv_k=500.0)
    rig.fake.poll()
    assert_refused(rig.call("set_heater_auto", {"zone": "top", "reason": "r"}),
                   "no live PV source")


def test_heater_auto_unknown_mode_refused(rig):
    rig.arm("heaters")
    rig.fake.pid["top"]["mode"] = "unknown"
    rig.fake.poll()
    assert_refused(rig.call("set_heater_auto", {"zone": "top", "reason": "r"}), "not manual")


def test_pv_source_happy_path(rig):
    rig.arm("heaters")
    out = assert_ok(rig.call("set_pv_source", {"zone": "top", "source": "cube", "reason": "r"}))
    # On main, software top's cube sensor is LabJack RTD2.
    assert rig.cmds() == [("xsphere/commands/pid/top/pv_expr", {"expr": "rtd2"})]
    assert out["confirmed"] is True
    assert out["readback"]["top"]["pv_expr"] == "rtd2"
    assert out["readback"]["top"]["previous_pv_expr"] == "rtd5"


def test_pv_source_from_none(rig):
    rig.arm("heaters")
    rig.fake.pid["top"].update(pv_expr="", pv_k=500.0)
    rig.fake.poll()
    assert_ok(rig.call("set_pv_source", {"zone": "top", "source": "clamp", "reason": "r"}))
    assert rig.cmds() == [("xsphere/commands/pid/top/pv_expr", {"expr": "rtd5"})]


def test_pv_source_must_be_in_rails(rig):
    rig.arm("heaters")
    doc = full_rails()
    doc["heaters"]["zones"]["top"]["pv_sources"] = ["clamp"]
    rig.set_rails(doc)
    assert_refused(rig.call("set_pv_source", {"zone": "top", "source": "cube", "reason": "r"}),
                   "pv_sources")
    assert_refused(rig.call("set_pv_source", {"zone": "nozzle", "source": "clamp",
                                              "reason": "r"}), "pv_sources")


def test_pv_source_sensor_must_be_fresh(rig):
    rig.arm("heaters")
    rig.fake.stale_ids.add("t_cube_top")
    rig.fake.poll()
    assert_refused(rig.call("set_pv_source", {"zone": "top", "source": "cube", "reason": "r"}),
                   "no fresh reading")


def test_pv_source_jump_limit(rig):
    rig.arm("heaters")
    rig.fake.sensors["rtd2"] = 180.0          # 14.5 K above the current PV (rtd5)
    rig.fake.poll()
    assert_refused(rig.call("set_pv_source", {"zone": "top", "source": "cube", "reason": "r"}),
                   "more than 10 K")


def test_pv_source_reverted_when_not_confirmed(rig):
    rig.arm("heaters")
    rig.fake.eval_fail = True                 # the new alias never evaluates
    rig.fake.pid["top"]["pv_expr_value_k"] = 165.5
    out = assert_refused(rig.call("set_pv_source", {"zone": "top", "source": "cube",
                                                    "reason": "r"}),
                         "not confirmed", "reverted to the previous pv_expr 'rtd5'")
    assert rig.cmds() == [("xsphere/commands/pid/top/pv_expr", {"expr": "rtd2"}),
                          ("xsphere/commands/pid/top/pv_expr", {"expr": "rtd5"})]
    assert out["confirmed"] is False


# ──────────────────────────────────────────────────────────────────────────
# Heater supply
# ──────────────────────────────────────────────────────────────────────────

def test_supply_voltage_current_output_in_order(rig):
    rig.arm("heaters")
    rig.fake.nge_ch[1].update(output=0, desired=0)
    rig.fake.poll()
    out = assert_ok(rig.call("set_heater_supply",
                             {"channel": 1, "voltage_v": 20.004, "current_a": 1.2346,
                              "output": True, "reason": "r"}))
    assert rig.cmds() == [("xsphere/commands/nge/1/voltage", {"value_v": 20.0}),
                          ("xsphere/commands/nge/1/current", {"value_a": 1.235}),
                          ("xsphere/commands/nge/1/output", {"state": 1})]
    assert out["confirmed"] is True
    assert out["readback"]["ch1"]["voltage_request_v"] == 20.0
    assert out["readback"]["ch1"]["desired"] == 1


def test_supply_voltage_rails(rig):
    rig.arm("heaters")
    assert_refused(rig.call("set_heater_supply", {"channel": 2, "voltage_v": 25, "reason": "r"}),
                   "outside Lucas's rails 10–24 V")
    doc = full_rails()
    doc["heaters"]["supply"]["2"].update(voltage_min_v=None, voltage_max_v=None)
    rig.set_rails(doc)
    assert_refused(rig.call("set_heater_supply", {"channel": 2, "voltage_v": 20, "reason": "r"}),
                   "has not set voltage rails")
    assert_refused(rig.call("set_heater_supply", {"channel": 2, "output": True, "reason": "r"}),
                   "needs voltage and current rails")
    assert rig.cmds() == []


def test_supply_zero_volts_needs_the_rail(rig):
    rig.arm("heaters")
    assert_refused(rig.call("set_heater_supply", {"channel": 1, "voltage_v": 0, "reason": "r"}),
                   "outside Lucas's rails")
    assert_refused(rig.call("set_heater_supply", {"channel": 1, "current_a": 0, "reason": "r"}),
                   "outside Lucas's rails")


def test_supply_switch_permissions(rig):
    rig.arm("heaters")
    doc = full_rails()
    doc["heaters"]["supply"]["1"].update(may_switch_off=False, may_switch_on=False)
    rig.set_rails(doc)
    assert_refused(rig.call("set_heater_supply", {"channel": 1, "output": False, "reason": "r"}),
                   "may_switch_off is false")
    assert_refused(rig.call("set_heater_supply", {"channel": 1, "output": True, "reason": "r"}),
                   "may_switch_on is false")


def test_supply_switch_on_checks_held_targets(rig):
    rig.arm("heaters")
    rig.fake.nge_ch[1].update(output=0, desired=0, voltage_request_v=30.0)
    rig.fake.poll()
    assert_refused(rig.call("set_heater_supply", {"channel": 1, "output": True, "reason": "r"}),
                   "held targets")
    assert rig.cmds() == []


def test_supply_command_budget(rig):
    rig.arm("heaters")
    rig.actions._nge_times.extend([rig.clock()] * 5)
    assert_refused(rig.call("set_heater_supply",
                            {"channel": 1, "voltage_v": 20, "current_a": 1.0, "reason": "r"}),
                   "at most 6 supply commands a minute")
    assert rig.cmds() == []


def test_supply_rejection_stops_later_steps(rig):
    rig.arm("heaters")
    rig.fake.reject_nge = True
    out = assert_refused(rig.call("set_heater_supply",
                                  {"channel": 1, "voltage_v": 20, "current_a": 1.0,
                                   "reason": "r"}),
                         "rejected the voltage command", "not sent: current")
    assert rig.cmds() == [("xsphere/commands/nge/1/voltage", {"value_v": 20.0})]
    assert "rejected" in out["readback"]["event"]


def test_supply_not_confirmed_stops_later_steps(rig):
    rig.arm("heaters")
    rig.fake.respond = False
    assert_refused(rig.call("set_heater_supply",
                            {"channel": 3, "voltage_v": 20, "output": False, "reason": "r"}),
                   "not confirmed", "output was not sent")
    assert rig.cmds() == [("xsphere/commands/nge/3/voltage", {"value_v": 20.0})]


def test_supply_not_connected(rig):
    rig.arm("heaters")
    rig.fake.nge["connected"] = False
    rig.fake.poll()
    assert_refused(rig.call("set_heater_supply", GOOD["set_heater_supply"]),
                   "not connected")


def test_supply_current_cap_and_saturation_warnings(rig):
    rig.arm("heaters")
    rig.fake.nge_ch[1].update(output=0, desired=0)
    rig.fake.pid["top"]["output_pct"] = 100.0
    rig.fake.poll()
    out = assert_ok(rig.call("set_heater_supply",
                             {"channel": 1, "voltage_v": 24, "current_a": 2.0, "output": True,
                              "reason": "r"}))
    w = " | ".join(out["warnings"])
    assert "33.6 W / V" in w and "PID top output is 100 %" in w


def test_supply_nothing_to_change(rig):
    rig.arm("heaters")
    out = assert_ok(rig.call("set_heater_supply",
                             {"channel": 1, "voltage_v": 21.5, "output": True, "reason": "r"}))
    assert rig.cmds() == [] and out["confirmed"] is None


def test_supply_refused_while_tripped(rig):
    rig.fake.set_interlock(tripped=True)
    rig.arm("heaters")
    assert_refused(rig.call("set_heater_supply", GOOD["set_heater_supply"]), "tripped")


# ──────────────────────────────────────────────────────────────────────────
# LN2
# ──────────────────────────────────────────────────────────────────────────

def test_ln2_open_happy_path_and_timer(rig):
    rig.arm("ln2")
    out = assert_ok(rig.call("set_ln2_valve", {"vessel": "ballast", "open": True,
                                               "reason": "top up"}))
    assert rig.transport.published_to("xsphere/commands/#") == [
        ("xsphere/commands/valve/ballast/state", {"state": 1}, 0, False)]
    assert out["confirmed"] is True
    saved = json.loads(rig.svc.settings.runtime_path.read_text(encoding="utf-8"))
    t = saved["ln2_timers"]["ballast"]
    assert t["deadline_ts"] - t["opened_ts"] == 60
    assert out["readback"]["auto_close_deadline_utc"] == t["deadline_utc"]
    assert rig.svc.status_payload()["ln2_timers"]["ballast"]["deadline_utc"] == t["deadline_utc"]


@pytest.mark.parametrize("setup,needle", [
    (lambda f: f.valves["cryostat"].update(auto_close=0), "auto-close is off"),
    (lambda f: f.valves["cryostat"].update(auto_close=True), "auto-close is off"),
    (lambda f: f.stale_ids.add("level_cryostat_probe"), "level_cryostat_probe"),
    (lambda f: f.valves["cryostat"].update(desired=1), "not closed"),
])
def test_ln2_open_preconditions(rig, setup, needle):
    rig.arm("ln2")
    setup(rig.fake)
    rig.fake.poll()
    assert_refused(rig.call("set_ln2_valve", {"vessel": "cryostat", "open": True,
                                              "reason": "r"}), needle)
    assert rig.cmds() == []


def test_ln2_open_needs_the_esp32_for_ballast(rig):
    rig.arm("ln2")
    rig.fake.esp32_alive = False
    rig.fake.poll()
    assert_refused(rig.call("set_ln2_valve", {"vessel": "ballast", "open": True, "reason": "r"}),
                   "ghs_esp32_alive")


def test_ln2_rails(rig):
    rig.arm("ln2")
    doc = full_rails()
    doc["ln2"]["vessels"]["cryostat"]["max_open_minutes"] = None
    doc["ln2"]["vessels"]["primary_xe"]["may_operate"] = False
    rig.set_rails(doc)
    assert_refused(rig.call("set_ln2_valve", {"vessel": "cryostat", "open": True,
                                              "reason": "r"}), "max_open_minutes")
    assert_refused(rig.call("set_ln2_valve", {"vessel": "primary_xe", "open": False,
                                              "reason": "r"}), "may_operate is false")


def test_ln2_valve_status_must_be_live(rig):
    rig.arm("ln2")
    rig.clock.advance(4)
    rig.fake.poll(valves=False)
    assert_refused(rig.call("set_ln2_valve", {"vessel": "cryostat", "open": True,
                                              "reason": "r"}), "no live valve status")


def test_ln2_close_turns_auto_open_off_first(rig):
    rig.arm("ln2")
    rig.fake.valves["cryostat"].update(state=1, desired=1, auto_open=1)
    rig.fake.poll()
    rig.svc.timers.set("cryostat", 5)
    out = assert_ok(rig.call("set_ln2_valve", {"vessel": "cryostat", "open": False,
                                               "reason": "r"}))
    assert rig.cmds() == [("xsphere/commands/valve/cryostat/auto_open", {"enabled": False}),
                          ("xsphere/commands/valve/cryostat/state", {"state": 0})]
    assert out["confirmed"] is True
    assert rig.svc.timers.get("cryostat") is None


def test_ln2_close_already_closed_sends_nothing(rig):
    rig.arm("ln2")
    out = assert_ok(rig.call("set_ln2_valve", {"vessel": "cryostat", "open": False,
                                               "reason": "r"}))
    assert rig.cmds() == [] and "already closed" in out["note"]


def test_ln2_min_interval(rig):
    rig.arm("ln2")
    assert_ok(rig.call("set_ln2_valve", {"vessel": "ballast", "open": True, "reason": "r"}))
    rig.advance(5)
    assert_refused(rig.call("set_ln2_valve", {"vessel": "ballast", "open": False, "reason": "r"}),
                   "wait 5 s more")
    rig.advance(5)
    assert_ok(rig.call("set_ln2_valve", {"vessel": "ballast", "open": False, "reason": "r"}))


def test_ln2_closed_by_the_ladder_is_reported(rig):
    rig.arm("ln2")
    rig.fake.ladder_close.add("cryostat")
    out = assert_ok(rig.call("set_ln2_valve", {"vessel": "cryostat", "open": True,
                                               "reason": "r"}))
    assert out["confirmed"] is False
    assert any("closed by the PLC ladder" in w for w in out["warnings"])


def test_ln2_timer_auto_close(rig):
    rig.arm("ln2")
    rig.fake.valves["ballast"]["auto_open"] = 1
    rig.fake.poll()
    assert_ok(rig.call("set_ln2_valve", {"vessel": "ballast", "open": True, "reason": "r"}))
    rig.transport.clear()
    rig.advance(30)
    rig.svc.supervisor.tick()
    assert rig.cmds() == []                                  # not yet
    rig.advance(31)
    rig.svc.supervisor.tick()
    assert rig.cmds() == [("xsphere/commands/valve/ballast/auto_open", {"enabled": False}),
                          ("xsphere/commands/valve/ballast/state", {"state": 0})]
    assert not rig.svc.arms.is_armed("ln2")
    kinds = rig.audit_kinds()
    assert "auto_close" in kinds and kinds[-1] == "auto_disarm"
    ld = rig.svc.arms.last_disarm("ln2")
    assert "closed by the service" in ld["reason"]
    rig.advance(1)
    rig.svc.supervisor.tick()                                # status shows closed → cleared
    assert rig.svc.timers.get("ballast") is None
    assert "ballast" not in json.loads(
        rig.svc.settings.runtime_path.read_text(encoding="utf-8"))["ln2_timers"]


def test_ln2_timer_retries_while_still_open(rig):
    rig.arm("ln2")
    assert_ok(rig.call("set_ln2_valve", {"vessel": "ballast", "open": True, "reason": "r"}))
    rig.fake.respond = False                                  # the PLC ignores the close
    rig.transport.clear()
    rig.advance(61)
    rig.svc.supervisor.tick()
    assert len(rig.cmds()) == 1
    rig.advance(5)
    rig.svc.supervisor.tick()
    assert len(rig.cmds()) == 1                               # retry only every 10 s
    rig.advance(6)
    rig.svc.supervisor.tick()
    assert rig.cmds()[-1] == ("xsphere/commands/valve/ballast/state", {"state": 0})
    assert len(rig.cmds()) == 2


def test_ln2_timer_survives_a_restart(tmp_path):
    rig = Rig(tmp_path)
    rig.arm("ln2")
    assert_ok(rig.call("set_ln2_valve", {"vessel": "cryostat", "open": True, "reason": "r"}))
    # A new service on the same state dir (the old one died without a goodbye).
    clock = rig.clock
    transport = LoopbackTransport()
    svc2 = Service(Settings(state_dir=tmp_path), transport, clock=clock, history=None,
                   confirm_timeout_s=0.3)
    fake2 = FakeSlowcontrol(transport, clock)
    fake2.valves["cryostat"].update(state=1, desired=1)
    svc2.start(threads=False)
    assert svc2.timers.get("cryostat") is not None
    assert not svc2.arms.is_armed("ln2")
    clock.advance(61)
    fake2.start()
    svc2.supervisor.tick()
    sent = [(t, p) for t, p, _q, _r in transport.published_to("xsphere/commands/#")]
    assert ("xsphere/commands/valve/cryostat/state", {"state": 0}) in sent


def test_ln2_timers_file_problems_are_reported(tmp_path):
    (tmp_path / "runtime.json").write_text("{broken", encoding="utf-8")
    t = Ln2Timers(tmp_path / "runtime.json")
    t.load()
    assert t.error and t.all() == {}


def test_ln2_auto_order_close_then_open(rig):
    rig.arm("ln2")
    rig.fake.valves["primary_xe"]["auto_close"] = 0
    rig.fake.poll()
    out = assert_ok(rig.call("set_ln2_auto", {"vessel": "primary_xe", "auto_open": True,
                                              "auto_close": True, "reason": "r"}))
    assert rig.cmds() == [("xsphere/commands/valve/primary_xe/auto_close", {"enabled": True}),
                          ("xsphere/commands/valve/primary_xe/auto_open", {"enabled": True})]
    assert out["confirmed"] is True
    assert out["readback"]["primary_xe"] == {"state": 0, "desired": 0, "auto_close": 1,
                                             "auto_open": 1}


def test_ln2_auto_order_open_off_then_close_off(rig):
    rig.arm("ln2")
    rig.fake.valves["cryostat"]["auto_open"] = 1
    rig.fake.poll()
    assert_ok(rig.call("set_ln2_auto", {"vessel": "cryostat", "auto_open": False,
                                        "auto_close": False, "reason": "r"}))
    assert rig.cmds() == [("xsphere/commands/valve/cryostat/auto_open", {"enabled": False}),
                          ("xsphere/commands/valve/cryostat/auto_close", {"enabled": False})]


def test_ln2_auto_refusals(rig):
    rig.arm("ln2")
    rig.fake.valves["cryostat"]["auto_close"] = 0
    rig.fake.poll()
    assert_refused(rig.call("set_ln2_auto", {"vessel": "cryostat", "auto_open": True,
                                             "reason": "r"}), "auto-close is off")
    assert_refused(rig.call("set_ln2_auto", {"vessel": "cryostat", "auto_open": True,
                                             "auto_close": False, "reason": "r"}),
                   "auto-open on and auto-close off")
    rig.fake.valves["ballast"].update(state=1, desired=1)
    rig.fake.poll()
    assert_refused(rig.call("set_ln2_auto", {"vessel": "ballast", "auto_close": False,
                                             "reason": "r"}), "only be turned off while")
    doc = full_rails()
    doc["ln2"]["vessels"]["primary_xe"].update(may_set_auto_open=False,
                                               may_disarm_auto_close=False)
    rig.set_rails(doc)
    assert_refused(rig.call("set_ln2_auto", {"vessel": "primary_xe", "auto_open": True,
                                             "reason": "r"}), "may_set_auto_open is false")
    assert_refused(rig.call("set_ln2_auto", {"vessel": "primary_xe", "auto_close": False,
                                             "reason": "r"}), "may_disarm_auto_close")
    assert rig.cmds() == []


def test_ln2_auto_open_needs_fresh_level(rig):
    rig.arm("ln2")
    rig.fake.stale_ids.add("level_primary_xe")
    rig.fake.poll()
    assert_refused(rig.call("set_ln2_auto", {"vessel": "primary_xe", "auto_open": True,
                                             "reason": "r"}), "level_primary_xe")


# ──────────────────────────────────────────────────────────────────────────
# Pneumatic
# ──────────────────────────────────────────────────────────────────────────

def test_pneumatic_open_happy_path(rig):
    rig.arm("pneumatic")
    out = assert_ok(rig.call("set_pneumatic_valve", {"valve": "pump", "open": True,
                                                     "reason": "pump down"}))
    assert rig.transport.published_to("xsphere/commands/#") == [
        ("xsphere/commands/pneumatic/pump/state", {"state": 1}, 0, False)]
    assert out["confirmed"] is True and out["readback"]["pump"]["state"] == 1


def test_pneumatic_gate_disabled_refused(rig):
    rig.arm("pneumatic")
    rig.fake.gate["enabled"] = 0
    rig.fake.poll()
    assert_refused(rig.call("set_pneumatic_valve", GOOD["set_pneumatic_valve"]),
                   "pneumatic valve gate on the Control page is disabled")


def test_pneumatic_gate_status_must_be_live(rig):
    rig.arm("pneumatic")
    rig.clock.advance(4)
    rig.fake.poll(pneumatic=False)
    assert_refused(rig.call("set_pneumatic_valve", GOOD["set_pneumatic_valve"]),
                   "pneumatic valve gate")


def test_pneumatic_forbid_open_together(rig):
    rig.arm("pneumatic")
    rig.fake.pneumatic["bottle"].update(state=1, desired=1)
    rig.fake.poll()
    assert_refused(rig.call("set_pneumatic_valve", {"valve": "pump", "open": True,
                                                    "reason": "r"}),
                   "bottle is open", "forbid pump and bottle")
    rig.fake.omit_pneumatic_state = True
    rig.fake.pneumatic["bottle"].update(state=0, desired=0)
    rig.fake.poll()
    assert_refused(rig.call("set_pneumatic_valve", {"valve": "pump", "open": True,
                                                    "reason": "r"}), "state is not known")
    assert rig.cmds() == []


def test_pneumatic_close_ignores_forbid_pairs(rig):
    rig.arm("pneumatic")
    rig.fake.pneumatic["bottle"].update(state=1, desired=1)
    rig.fake.pneumatic["pump"].update(state=1, desired=1)
    rig.fake.poll()
    assert_ok(rig.call("set_pneumatic_valve", {"valve": "pump", "open": False, "reason": "r"}))


def test_pneumatic_min_interval(rig):
    rig.arm("pneumatic")
    assert_ok(rig.call("set_pneumatic_valve", {"valve": "pump", "open": True, "reason": "r"}))
    rig.advance(3)
    assert_refused(rig.call("set_pneumatic_valve", {"valve": "pump", "open": False,
                                                    "reason": "r"}), "min_interval_s 10")
    rig.advance(7)
    assert_ok(rig.call("set_pneumatic_valve", {"valve": "pump", "open": False, "reason": "r"}))


def test_pneumatic_plc_refusal_is_detected(rig):
    rig.arm("pneumatic")
    rig.fake.gate_internal = 0             # the gate was shut between status and command
    out = assert_refused(rig.call("set_pneumatic_valve", GOOD["set_pneumatic_valve"]),
                         "the PLC refused the command")
    assert out["confirmed"] is False
    assert rig.cmds() == [("xsphere/commands/pneumatic/pump/state", {"state": 1})]


def test_pneumatic_missing_state_key_is_unconfirmed(rig):
    rig.arm("pneumatic")
    rig.fake.omit_pneumatic_state = True
    rig.fake.poll()
    out = assert_ok(rig.call("set_pneumatic_valve", {"valve": "ballast", "open": True,
                                                     "reason": "r"}))
    assert out["confirmed"] is False
    assert any("did not report the relay state" in w for w in out["warnings"])


def test_pneumatic_permissions(rig):
    rig.arm("pneumatic")
    doc = full_rails()
    doc["pneumatic"]["valves"]["ballast"] = {"may_open": True, "may_close": False}
    rig.set_rails(doc)
    assert_refused(rig.call("set_pneumatic_valve", {"valve": "ballast", "open": False,
                                                    "reason": "r"}), "may_close is false")


def test_pneumatic_already_there_sends_nothing(rig):
    rig.arm("pneumatic")
    out = assert_ok(rig.call("set_pneumatic_valve", {"valve": "pump", "open": False,
                                                     "reason": "r"}))
    assert rig.cmds() == [] and "already closed" in out["note"]


# ──────────────────────────────────────────────────────────────────────────
# Notes
# ──────────────────────────────────────────────────────────────────────────

def test_add_note_needs_no_arm(rig):
    out = assert_ok(rig.call("add_note", {"text": "  Lucas asked me to watch the fill.  "}))
    e = rig.svc.audit.recent(1)[0]
    assert e["kind"] == "note" and e["detail"] == "Lucas asked me to watch the fill."
    assert out["published"] == []


# ──────────────────────────────────────────────────────────────────────────
# Arms and the supervisor
# ──────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("payload", [
    {"group": "heaters", "armed": 1}, {"group": "heaters", "armed": "true"},
    {"group": "all", "armed": True}, {"group": "everything", "armed": True},
    {"group": "heaters", "armed": True, "extra": 1}, {"armed": True},
])
def test_arm_command_strictness(rig, payload):
    rig.transport.inject(f"{rig.settings.cmd_prefix}/arm", payload)
    assert rig.svc.arms.armed_groups() == []
    assert "ignored" in rig.svc.audit.recent(1)[0]["detail"]


def test_arm_sets_expiry_and_publishes_status(rig):
    rig.arm("heaters")
    a = rig.svc.arms.snapshot()["heaters"]
    assert a["armed"] is True and a["by"] == "pytest"
    assert a["expires_utc"] is not None
    st = rig.transport.retained_payload(rig.settings.status_topic)
    assert st["armed_heaters"] == 1 and st["arms"]["heaters"]["armed"] is True
    assert st["online"] == 1 and st["instance"] == "test"
    rig.arm("all", armed=False)
    assert rig.svc.arms.armed_groups() == []
    assert rig.transport.retained_payload(rig.settings.status_topic)["armed_heaters"] == 0


def test_retained_arm_command_is_ignored(rig):
    rig.transport.inject(f"{rig.settings.cmd_prefix}/arm",
                         {"group": "heaters", "armed": True}, retain=True)
    assert rig.svc.arms.armed_groups() == []


def test_arm_expires(rig):
    rig.arm("ln2")
    rig.advance(8 * 3600 + 1)
    assert not rig.svc.arms.is_armed("ln2")        # immediately, before any tick
    rig.svc.supervisor.tick()
    assert rig.svc.arms.last_disarm("ln2")["reason"] == "the arm expired"
    assert rig.svc.arms.auto_disarms == 1
    assert rig.audit_kinds()[-1] == "auto_disarm"


def test_arm_without_time_limit(rig):
    doc = full_rails()
    doc["general"]["arm_hours"] = None
    rig.set_rails(doc)
    rig.arm("ln2")
    rig.advance(100 * 3600)
    rig.svc.supervisor.tick()
    assert rig.svc.arms.is_armed("ln2")


def test_reconnect_disarms_everything(rig):
    rig.arm("heaters", "ln2", "pneumatic")
    rig.transport.set_connected(False)
    assert len(rig.svc.arms.armed_groups()) == 3
    rig.transport.set_connected(True)
    assert rig.svc.arms.armed_groups() == []
    assert "broker" in rig.svc.arms.last_disarm("pneumatic")["reason"]
    st = rig.transport.retained_payload(rig.settings.status_topic)
    assert st["armed_heaters"] == 0 and st["auto_disarms"] == 1


def test_slowcontrol_restart_disarms(rig):
    rig.arm("heaters", "ln2")
    rig.fake.uptime = 5010
    rig.fake.heartbeat()
    assert len(rig.svc.arms.armed_groups()) == 2
    # A retained replay of an older heartbeat says nothing about a restart.
    rig.transport.inject("xsphere/status/service/heartbeat", {"uptime_s": 3}, retain=True)
    assert len(rig.svc.arms.armed_groups()) == 2
    rig.fake.uptime = 4
    rig.fake.heartbeat()
    assert rig.svc.arms.armed_groups() == []
    assert "slowcontrol restarted" in rig.svc.arms.last_disarm("heaters")["reason"]


def test_service_alive_coming_back_disarms(rig):
    rig.fake.heartbeat_alive = False
    rig.advance(1)
    rig.svc.supervisor.tick()
    rig.arm("pneumatic")
    rig.fake.heartbeat_alive = True
    rig.advance(1)
    rig.svc.supervisor.tick()
    assert rig.svc.arms.armed_groups() == []
    assert "heartbeat came back" in rig.svc.arms.last_disarm("pneumatic")["reason"]


def test_pv_interlock_trip_edge_disarms_heaters_only(rig):
    rig.arm("heaters", "ln2")
    rig.fake.set_interlock(tripped=True, reasons={"3": "above 320 K"})
    assert rig.svc.arms.armed_groups() == ["ln2"]
    assert "PV interlock tripped (RTD3: above 320 K)" in \
        rig.svc.arms.last_disarm("heaters")["reason"]


def test_pv_interlock_retained_trip_does_not_count_as_an_edge(rig):
    rig.arm("heaters")
    rig.transport.inject("xsphere/status/pv_interlock",
                         dict(rig.fake.pv_interlock, tripped=True), retain=True)
    assert rig.svc.arms.is_armed("heaters")


def test_interlocks_not_ok_disarms_heaters(rig):
    rig.arm("heaters", "pneumatic")
    rig.fake.pub("xsphere/status/interlocks", {"rules_active": ["pid_saturated/top"],
                                               "ok": False})
    assert rig.svc.arms.armed_groups() == ["pneumatic"]
    assert "pid_saturated/top" in rig.svc.arms.last_disarm("heaters")["reason"]


def test_stale_snapshot_disarms_everything(rig):
    rig.arm("heaters", "ln2")
    rig.clock.advance(31)
    rig.svc.supervisor.tick()
    assert rig.svc.arms.armed_groups() == []
    assert "31 s old" in rig.svc.arms.last_disarm("ln2")["reason"]


def test_supervisor_does_nothing_when_all_is_well(rig):
    rig.arm("heaters", "ln2", "pneumatic")
    for _ in range(5):
        rig.advance(1)
        rig.svc.supervisor.tick()
    assert len(rig.svc.arms.armed_groups()) == 3 and rig.svc.arms.auto_disarms == 0


# ──────────────────────────────────────────────────────────────────────────
# Rails command (human, MQTT)
# ──────────────────────────────────────────────────────────────────────────

def test_rails_command_with_base_revision(rig):
    rev = rig.svc.rails.revision
    doc = full_rails()
    doc["heaters"]["zones"]["top"]["setpoint_max_k"] = 190
    rig.transport.inject(f"{rig.settings.cmd_prefix}/rails",
                         {"rails": doc, "base_revision": rev, "by": "control page"})
    assert rig.svc.rails.revision == rev + 1
    assert rig.svc.last_rails_result["ok"] is True
    e = rig.svc.audit.recent(1)[0]
    assert e["kind"] == "rails" and "setpoint_max_k: 200 → 190" in e["detail"]
    assert e["by"] == "control page"
    # A save made from an old editor is refused.
    rig.transport.inject(f"{rig.settings.cmd_prefix}/rails",
                         {"rails": full_rails(), "base_revision": rev, "by": "other tab"})
    assert rig.svc.rails.revision == rev + 1
    assert rig.svc.last_rails_result["ok"] is False
    assert "rails changed since you opened the editor" in rig.svc.last_rails_result["error"]
    assert rig.svc.audit.recent(1)[0]["kind"] == "rails_refused"
    st = rig.transport.retained_payload(rig.settings.status_topic)
    assert st["last_rails_result"]["ok"] is False and st["rails"]["revision"] == rev + 1


def test_no_tool_can_change_rails_or_arms(rig):
    names = rig.svc.registry.names()
    assert not any("rail" in n or n.startswith("arm") or "disarm" in n for n in names)
    for schema in WRITE_SCHEMAS.values():
        assert "rails" not in schema["properties"]


# ──────────────────────────────────────────────────────────────────────────
# Reads
# ──────────────────────────────────────────────────────────────────────────

def test_overview_text(rig):
    rig.arm("heaters")
    rig.fake.pid["bottom"].update(pv_expr="", pv_k=500.0)
    rig.fake.levels["cryostat"] = 0.0
    rig.fake.poll()
    is_error, text = rig.call("get_overview", {})
    assert is_error is False and isinstance(text, str)
    lines = text.splitlines()
    assert len(lines) <= 60
    assert "physically BOTTOM" in text and "physically TOP" in text
    assert "heaters ARMED" in text
    assert "band 4–320 K, not tripped" in text
    assert "zone bottom: NO PV SOURCE" in text
    assert "cryostat analog level reads 0" in text
    assert "AI may now:" in text and "top setpoint 150–200 K" in text


def test_overview_json(rig):
    is_error, out = rig.call("get_overview", {"format": "json"})
    assert is_error is False and out["ok"] is True
    assert out["zones"]["top"]["pv_status"] == "ok"
    assert out["zones"]["top"]["effective_setpoint_k"] == 165.0
    assert out["interlock"]["max_k"] == 320.0
    # Each supply channel names the physical position of the zone it feeds
    # (the registry label only names the software zone).
    for ch, c in out["supply"]["channels"].items():
        assert c["software_zone"] in ZONES
        assert c["physical"] == out["zones"][c["software_zone"]]["physical"]
    _, text = rig.call("get_overview", {})
    c1 = out["supply"]["channels"]["1"]
    assert (f"→ software zone {c1['software_zone']}, physically {c1['physical'].upper()}"
            in text)
    json.dumps(out, allow_nan=False)
    is_error, out = rig.call("get_overview", {"format": "xml"})
    assert is_error is True and out["invalid_arguments"] is True


def test_overview_warns_about_sequencer_and_trackers(rig):
    rig.fake.set_sequencer(running=True)
    rig.fake.set_trackers([{"id": "t9", "target": "a", "source": "b", "enabled": True}])
    _e, text = rig.call("get_overview", {})
    assert "Sequencer is RUNNING" in text and "tracker(s) enabled: t9" in text


def test_list_states(rig):
    _e, out = rig.call("list_states", {"group": "pid", "query": "top"})
    assert out["ok"] and out["count"] == out["matched"] > 0
    row = next(r for r in out["rows"] if r.startswith("pid_top_setpoint_expr |"))
    assert row.endswith("ai: set_heater_setpoints") and "'165.0'" in row
    assert any(r.startswith("pid_top_kp |") and r.endswith("ai: -") for r in out["rows"])
    _e, out = rig.call("list_states", {"limit": 3})
    assert out["count"] == 3 and out["matched"] > 3
    assert rig.call("list_states", {"limit": 0})[0] is True


def test_get_states_with_raw_status(rig):
    _e, out = rig.call("get_states", {"ids": ["pid_top_setpoint", "valve_cryostat",
                                              "t_cube_top", "nope"]})
    assert out["states"]["pid_top_setpoint"]["value"] == 165.0
    assert "control" not in out["states"]["pid_top_setpoint"]
    assert out["raw_status"]["xsphere/status/pid/top"]["payload"]["setpoint_expr"] == "165.0"
    assert "xsphere/status/valve/cryostat" in out["raw_status"]
    assert out["unknown_ids"] == ["nope"]
    is_error, out = rig.call("get_states", {"ids": [f"x{i}" for i in range(41)]})
    assert is_error is True


def test_get_ai_control(rig):
    rig.arm("heaters")
    assert_ok(rig.call("set_heater_setpoints", GOOD["set_heater_setpoints"]))
    _e, out = rig.call("get_ai_control", {})
    assert out["ok"] and out["arms"]["heaters"]["armed"] is True
    assert out["rails"]["revision"] == rig.svc.rails.revision
    assert out["rails_in_force"]["heaters"]["zones"]["top"]["setpoint_max_k"] == 200
    assert out["audit"][0]["tool"] == "set_heater_setpoints"
    assert out["last_ai_setpoints"]["top"]["value"] == 170.0
    assert out["actions"] == 1
    assert rig.call("get_ai_control", {"x": 1})[0] is True


def test_get_guide(rig):
    _e, text = rig.call("get_guide", {"topic": "heaters"})
    assert "software zone top" in text and "PV interlock" in text
    _e, text = rig.call("get_guide", {})
    for head in ("Safety model", "Heaters", "LN2", "Pneumatic", "History"):
        assert head in text
    assert rig.call("get_guide", {"topic": "turbos"})[0] is True


def test_query_history_unavailable_without_module(rig):
    is_error, out = rig.call("query_history", {"ids": ["t_cube_top"], "start": "-1h"})
    assert is_error is True and "history unavailable" in out["error"]


def test_query_history_delegates(tmp_path):
    class FakeHistory:
        def __init__(self):
            self.args = None

        def query(self, args):
            self.args = args
            return False, {"ok": True, "tool": "query_history", "series": []}

    h = FakeHistory()
    rig = Rig(tmp_path, history=h)
    is_error, out = rig.call("query_history", {"ids": ["t_cube_top"], "start": "-1h"})
    assert is_error is False and h.args == {"ids": ["t_cube_top"], "start": "-1h"}


# ──────────────────────────────────────────────────────────────────────────
# Tool registry
# ──────────────────────────────────────────────────────────────────────────

def _walk_objects(schema):
    yield schema
    for v in (schema.get("properties") or {}).values():
        if v.get("type") == "object":
            yield from _walk_objects(v)
        if v.get("type") == "array" and isinstance(v.get("items"), dict) \
                and v["items"].get("type") == "object":
            yield from _walk_objects(v["items"])


def test_tool_definitions(rig):
    tools = rig.svc.registry.mcp_tools()
    names = [t["name"] for t in tools]
    assert len(names) == len(set(names)) == len(TOOL_TEXT)
    for t in tools:
        assert re.fullmatch(r"[a-z][a-z0-9_]*", t["name"])
        assert t["title"] and 50 < len(t["description"]) <= 1500, t["name"]
        s = t["inputSchema"]
        assert s["type"] == "object" and s["additionalProperties"] is False
        assert not {"anyOf", "oneOf", "allOf", "not"} & set(s)
        for obj in _walk_objects(s):
            assert obj.get("additionalProperties") is False, t["name"]
        assert "outputSchema" not in t
        ann = t["annotations"]
        assert ann["openWorldHint"] is False
        if t["name"] in WRITE_SCHEMAS:
            assert ann["readOnlyHint"] is False
            if t["name"] != "add_note":
                assert "reason" in s["required"]
        else:
            assert ann["readOnlyHint"] is True
    meta = {t["name"] for t in tools if t.get("_meta")}
    assert meta == {"get_overview", "get_states", "query_history"}
    for t in tools:
        if t.get("_meta"):
            assert t["_meta"] == {"anthropic/maxResultSizeChars": 150000}
    json.dumps(tools, allow_nan=False)


def test_instructions_fit():
    assert 500 < len(INSTRUCTIONS) <= 4000
    for needle in ("get_overview", "reason", "http://xbox-pi:8088/control", "SOFTWARE",
                   "UTC", "America/New_York", "never" if False else "refusal"):
        assert needle in INSTRUCTIONS


def test_registry_call_is_safe(rig):
    assert rig.svc.registry.has("get_overview") and not rig.svc.registry.has("publish")
    is_error, out = rig.svc.registry.call("publish", {})
    assert is_error is True and "unknown tool" in out["error"]

    class Boom:
        def handlers(self):
            return {n: self.fail for n in WRITE_SCHEMAS}

        def fail(self, args):
            raise RuntimeError("kaboom")

    reg = ToolRegistry(Boom(), rig.svc.reads)
    is_error, out = reg.call("set_ln2_valve", GOOD["set_ln2_valve"])
    assert is_error is True and out["internal"] is True and "kaboom" in out["error"]


def test_every_published_command_is_allowlisted(rig):
    rig.arm("heaters", "ln2", "pneumatic")
    assert_ok(rig.call("set_heater_gradient", {"base_k": 166, "dv_k": 0, "dl_k": 0,
                                               "reason": "r"}))
    assert_ok(rig.call("set_pv_source", {"zone": "bottom", "source": "cube", "reason": "r"}))
    assert_ok(rig.call("set_heater_supply", {"channel": 2, "voltage_v": 20, "reason": "r"}))
    assert_ok(rig.call("set_ln2_valve", {"vessel": "ballast", "open": True, "reason": "r"}))
    assert_ok(rig.call("set_pneumatic_valve", {"valve": "pump", "open": True, "reason": "r"}))
    sent = rig.transport.published_to("xsphere/commands/#")
    assert len(sent) == 7                 # gradient 3 + PV 1 + supply 1 + LN2 1 + gas valve 1
    for topic, _p, qos, retain in sent:
        assert is_allowed_command(topic) and qos == 0 and retain is False


# ──────────────────────────────────────────────────────────────────────────
# Flask app
# ──────────────────────────────────────────────────────────────────────────

@pytest.fixture
def client(rig):
    return create_app(rig.svc).test_client()


def test_host_guard(client):
    r = client.get("/healthz", headers={"Host": "evil.example:8089"})
    assert r.status_code == 421
    r = client.post("/mcp", data="{}", content_type="application/json",
                    headers={"Host": "attacker.test"})
    assert r.status_code == 421
    assert client.get("/healthz", headers={"Host": "xbox-pi:8089"}).status_code == 200
    assert client.get("/healthz", headers={"Host": "192.168.8.116"}).status_code == 200


def test_healthz(client):
    r = client.get("/healthz")
    assert r.status_code == 200
    j = r.get_json()
    assert j["ok"] is True and j["version"] == __version__ and j["instance"] == "test"
    assert j["mqtt_connected"] is True


@pytest.mark.parametrize("method,path", [
    ("post", "/mcp"), ("get", "/api/v1/overview"), ("post", "/api/v1/actions/get_overview"),
    ("get", "/api/v1/ai_control")])
def test_origin_header_is_refused(client, method, path):
    r = getattr(client, method)(path, data="{}", content_type="application/json",
                                headers={"Origin": "http://xbox-pi:8089"})
    assert r.status_code == 403


def test_status_page_has_no_origin_rule(client):
    r = client.get("/", headers={"Origin": "http://xbox-pi:8089"})
    assert r.status_code in (200, 404)


@pytest.mark.parametrize("path", ["/mcp", "/api/v1/actions/add_note"])
def test_post_needs_json_content_type(client, path):
    r = client.post(path, data='{"text": "x"}', content_type="text/plain")
    assert r.status_code == 415
    r = client.post(path, data='{"text": "x"}')
    assert r.status_code == 415


@pytest.mark.parametrize("method", ["get", "delete", "options", "put", "head"])
def test_mcp_get_and_delete_are_405(client, method):
    pytest.importorskip("aicontrol.mcp")
    r = getattr(client, method)("/mcp")
    assert r.status_code == 405
    assert r.headers["Allow"] == "POST"


def test_mcp_tools_list_through_flask(client):
    pytest.importorskip("aicontrol.mcp")
    r = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                  "params": {"protocolVersion": "2025-11-25",
                                             "capabilities": {},
                                             "clientInfo": {"name": "t", "version": "1"}}})
    assert r.status_code == 200
    assert r.get_json()["result"]["serverInfo"]["version"] == __version__
    r = client.post("/mcp", json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                    headers={"MCP-Protocol-Version": "2025-11-25"})
    assert r.status_code == 200
    assert len(r.get_json()["result"]["tools"]) == len(TOOL_TEXT)
    r = client.post("/mcp", json={"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                                  "params": {"name": "set_ln2_valve",
                                             "arguments": GOOD["set_ln2_valve"]}},
                    headers={"MCP-Protocol-Version": "2025-11-25"})
    res = r.get_json()["result"]
    assert res["isError"] is True
    assert "not armed" in json.loads(res["content"][0]["text"])["refused"]


def test_rest_action_refusal_is_409(client, rig):
    r = client.post("/api/v1/actions/set_ln2_valve", json=GOOD["set_ln2_valve"])
    assert r.status_code == 409
    assert "not armed" in r.get_json()["refused"]
    assert rig.cmds() == []


def test_rest_action_success_and_errors(client, rig):
    rig.arm("heaters")
    r = client.post("/api/v1/actions/set_heater_setpoints",
                    json={"setpoints": {"top": 170}, "reason": "rest"})
    assert r.status_code == 200 and r.get_json()["confirmed"] is True
    r = client.post("/api/v1/actions/set_heater_setpoints",
                    json={"setpoints": {"top": True}, "reason": "rest"})
    assert r.status_code == 400 and r.get_json()["invalid_arguments"] is True
    r = client.post("/api/v1/actions/set_heater_setpoints", data='{"setpoints": {"top": NaN}}',
                    content_type="application/json")
    assert r.status_code == 400
    r = client.post("/api/v1/actions/set_heater_setpoints", data="[1]",
                    content_type="application/json")
    assert r.status_code == 400
    r = client.post("/api/v1/actions/publish_anything", json={})
    assert r.status_code == 404
    rig.fake.poll()
    r = client.post("/api/v1/actions/get_states", json={"ids": ["pid_top_setpoint"]})
    assert r.status_code == 200 and r.get_json()["states"]["pid_top_setpoint"]["value"] == 170.0


def test_rest_reads(client):
    r = client.get("/api/v1/overview")
    assert r.status_code == 200 and r.mimetype == "text/plain" and "xsphere overview" in r.text
    r = client.get("/api/v1/overview?format=json")
    assert r.status_code == 200 and r.get_json()["ok"] is True
    r = client.get("/api/v1/overview?colour=red")
    assert r.status_code == 400
    r = client.get("/api/v1/states?group=pid&limit=5")
    assert r.status_code == 200 and r.get_json()["count"] == 5
    assert client.get("/api/v1/states?limit=abc").status_code == 400
    r = client.get("/api/v1/states/pid_top_setpoint")
    assert r.status_code == 200 and "pid_top_setpoint" in r.get_json()["states"]
    r = client.get("/api/v1/ai_control")
    j = r.get_json()
    assert r.status_code == 200 and j["online"] == 1 and isinstance(j["audit"], list)
    r = client.get("/api/v1/guide?topic=ln2")
    assert r.status_code == 200 and "LN2" in r.text
    r = client.get("/api/v1/history?ids=t_cube_top&start=-1h")
    assert r.status_code == 400 and "history unavailable" in r.get_json()["error"]
    assert client.get("/api/v1/nothing").status_code == 404


def test_index_page_is_served(client):
    r = client.get("/")
    assert r.status_code in (200, 404)
    if r.status_code == 200:
        assert r.mimetype == "text/html"


def test_shutdown_publishes_offline_status(rig):
    rig.arm("heaters")
    rig.svc.shutdown()
    st = rig.transport.retained_payload(rig.settings.status_topic)
    assert st["online"] == 0 and st["armed_heaters"] == 0 and st["instance"] == "test"
    rig.svc.shutdown()                                   # idempotent


def test_oversized_body_is_413(client):
    big = '{"text": "' + "x" * (1024 * 1024 + 10) + '"}'
    r = client.post("/api/v1/actions/add_note", data=big, content_type="application/json")
    assert r.status_code == 413


def test_runtime_json_unknown_vessel_is_ignored(tmp_path):
    (tmp_path / "runtime.json").write_text(json.dumps({"ln2_timers": {
        "kitchen_sink": {"opened_ts": T0, "deadline_ts": T0 + 60},
        "cryostat": {"opened_ts": T0, "deadline_ts": T0 + 60}}}), encoding="utf-8")
    t = Ln2Timers(tmp_path / "runtime.json")
    t.load()
    assert list(t.all()) == ["cryostat"]


def test_main_refuses_a_test_instance_on_a_remote_broker(monkeypatch, tmp_path):
    from aicontrol import app as app_module
    monkeypatch.setenv("XSPHERE_AICONTROL_INSTANCE", "test")
    monkeypatch.setenv("XSPHERE_MQTT_HOST", "192.168.8.116")
    monkeypatch.setenv("XSPHERE_AICONTROL_STATE_DIR", str(tmp_path))
    assert app_module.main() == 2
