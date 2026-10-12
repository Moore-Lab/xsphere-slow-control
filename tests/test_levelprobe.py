"""
Offline tests for the cryostat level probe reader and its hand-off to the PLC.

    python -m pytest tests/test_levelprobe.py -q          (from the repo root)

The lines fed in are what the real sensor printed on 2026-10-11.
"""

import json

import paho.mqtt.client as paho
import pytest

from slowcontrol.core.config import ServiceConfig, load
from slowcontrol.drivers import levelprobe as lp_mod
from slowcontrol.drivers.levelprobe import LevelProbeDriver, parse_line, pwm_to_volts
from slowcontrol.state import default_schema_path, load_state_schema

REAL_LINE = b"median delta = 742.453125 | pwm = 129\r\n"
SENSOR = "xsphere/sensors/level_probe/cryostat"
STATUS = "xsphere/status/level_probe/cryostat"


class FakeMqtt:
    def __init__(self):
        self.last = {}
        self.count = {}
        self.subs = []

    def publish(self, topic, payload, qos=1, retain=False):
        payload = json.loads(json.dumps(payload))
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


class Clock:
    def __init__(self):
        self.mono = 1000.0

    def monotonic(self):
        return self.mono


class FakeLink:
    """A serial port: read() hands out what was queued, then nothing (timeout)."""

    def __init__(self):
        self.pending = b""
        self.dead = False
        self.written = []

    @property
    def in_waiting(self):
        return len(self.pending)

    def read(self, n):
        if self.dead:
            raise OSError("device reports readiness to read but returned no data")
        out, self.pending = self.pending[:n], self.pending[n:]
        return out

    def write(self, data):
        self.written.append(data)

    def close(self):
        pass


class Rig:
    def __init__(self, clock):
        self.clock = clock
        self.mqtt = FakeMqtt()
        self.link = FakeLink()
        self.present = True
        config = ServiceConfig()
        config.level_probe.enabled = True
        self.driver = LevelProbeDriver(config, self.mqtt, open_link=self._open)

    def _open(self):
        if not self.present:
            raise OSError("could not open port: No such file or directory")
        return self.link


@pytest.fixture
def rig(monkeypatch):
    clock = Clock()
    monkeypatch.setattr(lp_mod, "time", clock)
    r = Rig(clock)
    yield r
    assert r.link.written == []         # the sensor is never written to


def test_parse_line():
    assert parse_line("median delta = 742.453125 | pwm = 129") == (742.453125, 129)
    assert parse_line("MPR121 liquid level calibration test") is None
    assert parse_line("Using hardcoded calibration values.") is None
    assert parse_line("median delta = 742.453125 | pwm = ") is None     # torn line


def test_pwm_to_volts():
    assert pwm_to_volts(0) == 0.0
    assert pwm_to_volts(255) == pytest.approx(3.3)
    assert pwm_to_volts(144) == pytest.approx(1.8635, abs=1e-4)


def test_publishes_voltage_for_each_line(rig):
    rig.driver.connect()
    assert rig.mqtt.last[STATUS]["connected"] is True
    assert rig.mqtt.last[STATUS]["fresh"] is False
    rig.link.pending = REAL_LINE
    rig.driver.poll()
    assert rig.mqtt.last[SENSOR] == {"voltage_v": 1.6694, "pwm": 129, "count": 742.453}
    assert rig.mqtt.last[STATUS]["fresh"] is True


def test_line_split_across_reads(rig):
    rig.driver.connect()
    rig.link.pending = REAL_LINE[:20]
    rig.driver.poll()
    assert SENSOR not in rig.mqtt.last
    rig.link.pending = REAL_LINE[20:] + b"median delta = 743.0 | pwm = 128\r\n"
    rig.driver.poll()
    assert rig.mqtt.count[SENSOR] == 2
    assert rig.mqtt.last[SENSOR]["pwm"] == 128


def test_other_output_is_reported_not_published(rig):
    rig.driver.connect()
    rig.link.pending = b"MPR121 not found\r\nmedian delta = 1.0 | pwm = 999\r\n"
    rig.driver.poll()
    assert SENSOR not in rig.mqtt.last
    assert rig.mqtt.last[STATUS]["note"] == "median delta = 1.0 | pwm = 999"


def test_goes_stale_when_the_sensor_stops(rig):
    rig.driver.connect()
    rig.link.pending = REAL_LINE
    rig.driver.poll()
    rig.clock.mono += 16
    rig.driver.poll()
    assert rig.mqtt.last[STATUS]["fresh"] is False
    assert rig.mqtt.last[STATUS]["connected"] is True


def test_unplug_and_replug(rig):
    rig.present = False
    rig.driver.connect()                # must not raise: the driver stays alive
    assert rig.mqtt.last[STATUS]["connected"] is False
    assert "No such file" in rig.mqtt.last[STATUS]["error"]
    rig.present = True
    rig.driver.poll()                   # too soon to retry
    assert rig.mqtt.last[STATUS]["connected"] is False
    rig.clock.mono += 6
    rig.link.pending = REAL_LINE
    rig.driver.poll()
    assert rig.mqtt.last[SENSOR]["pwm"] == 129

    rig.link.dead = True
    rig.driver.poll()
    assert rig.mqtt.last[STATUS]["connected"] is False
    rig.link.dead = False
    rig.clock.mono += 6
    rig.driver.poll()
    assert rig.mqtt.last[STATUS]["connected"] is True


def test_shipped_config_and_registry():
    cfg = load("slowcontrol/config.yaml")
    assert cfg.level_probe.enabled and cfg.level_probe.vessel == "cryostat"
    assert cfg.level_probe.baud_rate == 9600
    load_state_schema(default_schema_path("slowcontrol/config.yaml"))
    with open("slowcontrol/state.yaml", encoding="utf-8") as fh:
        assert "xsphere/sensors/level_probe/cryostat" in fh.read()


def test_plc_driver_writes_voltage_and_age(monkeypatch):
    plc = pytest.importorskip("slowcontrol.drivers.plc")    # needs pymodbus
    clock = Clock()
    monkeypatch.setattr(plc.time, "monotonic", clock.monotonic)
    driver = plc.PlcDriver(ServiceConfig(), FakeMqtt())
    writes = {}
    monkeypatch.setattr(driver, "_write_float",
                        lambda addr, value: writes.__setitem__(addr, value) or True)
    volts, age = plc.REG_LEVEL_PROBE_WRITE["cryostat"], plc.REG_LEVEL_PROBE_AGE["cryostat"]
    assert (volts, age) == (plc._df(333), plc._df(254))

    driver._write_level_to_plc()        # nothing heard yet
    assert volts not in writes and writes[age] == plc.LEVEL_PROBE_AGE_MAX_S

    driver._on_level_probe_message(SENSOR, {"voltage_v": 1.6694, "pwm": 129})
    clock.mono += 3.5
    driver._write_level_to_plc()
    assert writes[volts] == 1.6694 and writes[age] == 3.5

    writes.clear()
    clock.mono += 20                    # the probe went quiet
    driver._write_level_to_plc()
    assert volts not in writes and writes[age] == 23.5

    driver._on_level_probe_message(SENSOR, {"voltage_v": "nan"})
    driver._on_level_probe_message(SENSOR, "garbage")
    driver._write_level_to_plc()
    assert volts not in writes
