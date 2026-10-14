"""
Offline tests for the pneumatic valve control gate in the PLC driver.

    python -m pytest tests/test_pneumatic_enable.py -q      (from the repo root)

The PLC is a fake Modbus client that runs the two ladder rungs behind each
gas valve (Y10x follows DS15x == 1).  pymodbus is stubbed when it is not
installed: the driver only needs its client class and its exception type.
"""

import json
import os
import sys
import types

import paho.mqtt.client as paho
import pytest

try:
    import pymodbus.client      # noqa: F401
    import pymodbus.exceptions  # noqa: F401
except ImportError:             # the DAQ PC has no pymodbus
    _pm = types.ModuleType("pymodbus")
    _pm_client = types.ModuleType("pymodbus.client")
    _pm_exc = types.ModuleType("pymodbus.exceptions")

    class _StubClient:          # replaced by FakePlc in every test
        def __init__(self, *a, **k): ...
        def connect(self): return False
        def close(self): ...

    class _StubException(Exception): ...

    _pm_client.ModbusTcpClient = _StubClient
    _pm_exc.ModbusException = _StubException
    _pm.client, _pm.exceptions = _pm_client, _pm_exc
    sys.modules.update({"pymodbus": _pm, "pymodbus.client": _pm_client,
                        "pymodbus.exceptions": _pm_exc})

from slowcontrol.core.config import ServiceConfig                        # noqa: E402
from slowcontrol.drivers import plc as plc_mod                           # noqa: E402
from slowcontrol.drivers.plc import PNEUMATIC_VALVES, PlcDriver          # noqa: E402
from slowcontrol.state import default_schema_path, load_state_schema     # noqa: E402
from slowcontrol.state.store import StateStore                           # noqa: E402

STATUS = "xsphere/status/pneumatic_control"
ENABLE = "xsphere/commands/pneumatic_control/enable"
REPO = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))


class FakeMqtt:
    """Stands in for core.mqtt.MqttClient: keeps the last payload per topic
    and delivers every publish to matching subscribers, as the broker would."""

    def __init__(self):
        self.last = {}
        self.count = {}
        self.subs = []

    def publish(self, topic, payload, qos=1, retain=False):
        payload = json.loads(json.dumps(payload))       # as decoded off the wire
        self.last[topic] = payload
        self.count[topic] = self.count.get(topic, 0) + 1
        for pattern, callback in list(self.subs):
            if paho.topic_matches_sub(pattern, topic):
                callback(topic, payload)

    def publish_status(self, *parts, payload, retain=True):
        self.publish("xsphere/status/" + "/".join(parts), payload)

    def publish_sensor(self, *parts, payload, retain=False):
        self.publish("xsphere/sensors/" + "/".join(parts), payload)

    def subscribe(self, topic, callback, qos=1):
        self.subs.append((topic, callback))


class _Result:
    def __init__(self, registers=None, bits=None):
        self.registers = registers or []
        self.bits = bits or []

    def isError(self):
        return False


class FakePlc:
    """Holding registers and coils, plus the pneumatic ladder rungs: each
    Y10x coil is SET while its DS15x register == 1 and RST otherwise."""

    def __init__(self):
        self.regs = {}
        self.coils = {}
        self.writes = []            # (address, value) of every register write

    def connect(self):
        return True

    def close(self):
        pass

    def _ladder(self):
        for ds, y, _normally_open in PNEUMATIC_VALVES.values():
            self.coils[y] = (self.regs.get(ds, 0) == 1)

    def read_holding_registers(self, address, count=1):
        return _Result(registers=[self.regs.get(address + i, 0) for i in range(count)])

    def write_register(self, address, value):
        self.regs[address] = int(value)
        self.writes.append((address, int(value)))
        self._ladder()
        return _Result()

    def write_registers(self, address, values):
        for i, v in enumerate(values):
            self.regs[address + i] = int(v)
        self._ladder()
        return _Result()

    def read_coils(self, address, count=1):
        self._ladder()
        return _Result(bits=[bool(self.coils.get(address + i, False)) for i in range(count)])

    def write_coil(self, address, value):
        self.coils[address] = bool(value)
        return _Result()


class Rig:
    """One fake PLC, one broker, a registry listening to it, and as many
    driver lifetimes as a test wants."""

    def __init__(self, monkeypatch, tmp_path):
        self.plc = FakePlc()
        monkeypatch.setattr(plc_mod, "ModbusTcpClient", lambda *a, **k: self.plc)
        self.mqtt = FakeMqtt()
        self.config = ServiceConfig()
        self.tmp_path = tmp_path
        self.driver = None
        # The registry, fed straight from the broker (no tick thread).
        self.store = StateStore(self.config, self.mqtt,
                                load_state_schema(default_schema_path()))
        for topic in sorted(self.store.schema.subscribe_topics()):
            self.mqtt.subscribe(topic, self.store._on_message)
        self.start()

    def start(self):
        """A (re)start of the service: a new driver on a new broker session."""
        self.mqtt.subs = [s for s in self.mqtt.subs if getattr(s[1], "__self__", None)
                          is not self.driver]
        self.driver = PlcDriver(self.config, self.mqtt)
        self.driver._interlock_path = str(self.tmp_path / "pv_interlock.json")
        self.driver.connect()
        return self.driver

    def status(self):
        return self.mqtt.last[STATUS]

    def state(self, sid="pneumatic_control_enabled"):
        return self.store.snapshot()["states"][sid]

    def enable(self, value):
        self.mqtt.publish(ENABLE, {"enabled": value})

    def valve(self, name, state):
        self.mqtt.publish(f"xsphere/commands/pneumatic/{name}/state", {"state": state})

    def ds(self, name):
        """The relay command register behind a valve, as the PLC holds it."""
        return self.plc.regs.get(PNEUMATIC_VALVES[name][0], 0)


@pytest.fixture
def rig(monkeypatch, tmp_path):
    return Rig(monkeypatch, tmp_path)


# ---------------------------------------------------------------------------

def test_control_comes_up_disabled_and_the_registry_says_so(rig):
    s = rig.status()
    assert (s["enabled"], s["refused"], s["since_utc"]) == (0, 0, None)
    assert "last_refused" not in s
    v = rig.state()
    assert v["value"] is False and v["kind"] == "binary"
    # No control mapping on purpose: nothing but the Control page's buttons
    # (which publish the command topic directly) can switch the gate.
    assert "control" not in v
    for name in PNEUMATIC_VALVES:
        assert "control" in rig.state(f"pneumatic_{name}")


def test_commands_are_refused_while_disabled(rig):
    rig.valve("pump", 1)
    assert rig.plc.writes == [] and rig.ds("pump") == 0
    s = rig.status()
    assert s["enabled"] == 0 and s["refused"] == 1
    assert s["last_refused"]["valve"] == "pump" and s["last_refused"]["state"] == 1
    assert s["last_refused"]["utc"]
    rig.driver.poll()
    assert rig.mqtt.last["xsphere/status/pneumatic/pump"] == {"desired": 0, "state": 0, "relay": 0}
    rig.valve("ballast", 0)
    assert rig.plc.writes == [] and rig.status()["refused"] == 2
    assert rig.status()["last_refused"]["valve"] == "ballast"


def test_enabling_lets_commands_through_and_the_relay_follows(rig):
    rig.enable(True)
    s = rig.status()
    assert s["enabled"] == 1 and s["since_utc"] and s["refused"] == 0
    assert rig.state()["value"] is True
    rig.valve("pump", 1)            # normally closed: open = energise
    assert rig.ds("pump") == 1
    rig.valve("ballast", 0)         # normally open: close = energise
    assert rig.ds("ballast") == 1
    rig.driver.poll()
    assert rig.mqtt.last["xsphere/status/pneumatic/pump"] == {"desired": 1, "state": 1, "relay": 1}
    assert rig.mqtt.last["xsphere/status/pneumatic/ballast"] == {"desired": 0, "state": 0, "relay": 1}
    assert rig.mqtt.last["xsphere/status/pneumatic/bottle"] == {"desired": 1, "state": 1, "relay": 0}
    assert rig.status()["refused"] == 0


def test_disabling_locks_again_and_moves_nothing(rig):
    rig.enable(1)
    rig.valve("pump", 1)
    rig.enable(0)
    assert rig.status()["enabled"] == 0 and rig.state()["value"] is False
    assert rig.ds("pump") == 1                      # still open: nothing moved
    before = list(rig.plc.writes)
    rig.valve("pump", 0)
    assert rig.plc.writes == before and rig.ds("pump") == 1
    assert rig.status()["refused"] == 1


def test_switching_to_the_same_state_is_harmless(rig):
    rig.enable(False)
    assert rig.status()["enabled"] == 0 and rig.status()["since_utc"] is None
    rig.enable(True)
    since = rig.status()["since_utc"]
    rig.enable(True)
    assert rig.status()["enabled"] == 1 and rig.status()["since_utc"] == since


@pytest.mark.parametrize("bad", [{"enabled": "yes"}, {"enabled": "true"}, {"enabled": 2},
                                 {"enabled": 1.0}, {"enabled": None}, {}, "on", 1, [True]])
def test_only_a_bool_or_a_literal_0_1_switches_the_gate(rig, bad):
    rig.mqtt.publish(ENABLE, bad)
    assert rig.status()["enabled"] == 0
    rig.valve("pump", 1)
    assert rig.ds("pump") == 0


def test_a_bad_valve_command_is_dropped_before_the_gate_counts_it(rig):
    rig.valve("pump", "1")
    rig.valve("nosuchvalve", 1)
    assert rig.status()["refused"] == 0 and rig.plc.writes == []


def test_the_gate_is_republished_with_every_poll(rig):
    n = rig.mqtt.count[STATUS]
    rig.driver.poll()
    rig.driver.poll()
    assert rig.mqtt.count[STATUS] == n + 2
    assert rig.state()["freshness"] == "fresh"


def test_a_service_restart_comes_up_disabled_again(rig):
    rig.enable(True)
    rig.valve("pump", 1)
    rig.start()
    assert rig.status()["enabled"] == 0 and rig.status()["since_utc"] is None
    assert rig.state()["value"] is False
    assert rig.ds("pump") == 1                      # the valve is where it was left
    rig.valve("pump", 0)
    assert rig.ds("pump") == 1 and rig.status()["refused"] == 1


def test_the_registry_and_telegraf_watch_the_topic_the_driver_publishes():
    sdef = load_state_schema(default_schema_path()).by_id()["pneumatic_control_enabled"]
    assert sdef.source.topic == STATUS and sdef.source.keys == ("enabled",)
    assert sdef.control is None
    with open(os.path.join(REPO, "telegraf", "telegraf.conf")) as fh:
        conf = fh.read()
    assert f'topics      = ["{STATUS}"]' in conf
    assert 'measurement_name = "pneumatic_control"' in conf
