"""
Offline tests for the cryostat level probe reader and its hand-off to the PLC.

    python -m pytest tests/test_levelprobe.py -q          (from the repo root)

The lines fed in are what the real boards printed: the level probe on
2026-10-11, the gas handling cart's ESP32 (the other CH340 on the Pi) on
2026-10-12.
"""

import json

import paho.mqtt.client as paho
import pytest

from slowcontrol.core.config import ServiceConfig, load
from slowcontrol.drivers import levelprobe as lp_mod
from slowcontrol.drivers.levelprobe import LevelProbeDriver, parse_line, pwm_to_volts
from slowcontrol.state import default_schema_path, load_state_schema

REAL_LINE = b"median delta = 742.453125 | pwm = 129\r\n"
CART_BLOCK = (b"Temperature: 23.80 \xc2\xb0C, Humidity: 47.00 %\r\nPressure: 1006.77 hPa\r\n"
              b"CH0: 0.01 V  ->  0.75 mbar\r\nCH1: 3.02 V  ->  2081.71 mbar\r\n"
              b"CH2: 3.24 V  ->  1.162e-06 mbar\r\nCH3: 3.45 V  ->  2.687e-06 mbar\r\n")
SENSOR = "xsphere/sensors/level_probe/cryostat"
STATUS = "xsphere/status/level_probe/cryostat"
PROBE, CART, OTHER = "/dev/ttyUSB0", "/dev/ttyUSB1", "/dev/ttyUSB2"


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
    """A Pi's worth of USB-serial adapters, one broker, and the driver.  By
    default as shipped (port: auto) with just the probe plugged in."""

    def __init__(self, clock, port="auto"):
        self.clock = clock
        self.mqtt = FakeMqtt()
        self.link = FakeLink()              # the level probe's board
        self.ports = {PROBE: self.link}     # tty → what is plugged in there
        self.plugged = {PROBE: 1}           # tty → changes when it is re-plugged
        self.busy = set()                   # ttys something else holds the lock on
        self.opens = []
        self.links = [self.link]
        config = ServiceConfig()
        config.level_probe.enabled = True
        config.level_probe.port = port
        self.driver = LevelProbeDriver(config, self.mqtt, open_link=self._open,
                                       list_ports=lambda: sorted(self.ports),
                                       plugged_in_at=lambda tty: self.plugged.get(tty, 0),
                                       describe=lambda tty: tty)

    def plug(self, tty, link=None):
        link = link or FakeLink()
        link.dead = False
        self.ports[tty] = link
        self.plugged[tty] = self.plugged.get(tty, 0) + 1
        if link not in self.links:
            self.links.append(link)
        return link

    def unplug(self, tty):
        link = self.ports.pop(tty)
        link.dead = True
        return link

    def _open(self, tty):
        self.opens.append(tty)
        if tty not in self.ports:
            raise OSError(f"could not open port {tty}: No such file or directory")
        if tty in self.busy:
            raise OSError(f"Could not exclusively lock port {tty}")
        return self.ports[tty]

    def poll(self, n=1, dt=0.2):
        for _ in range(n):
            self.clock.mono += dt
            self.driver.poll()

    @property
    def status(self):
        return self.mqtt.last[STATUS]


def _rig(monkeypatch, **kwargs):
    clock = Clock()
    monkeypatch.setattr(lp_mod, "time", clock)
    return Rig(clock, **kwargs)


@pytest.fixture
def rig(monkeypatch):
    r = _rig(monkeypatch)
    yield r
    assert all(link.written == [] for link in r.links)      # nothing is ever written to a port


@pytest.fixture
def pinned(monkeypatch):
    """The driver told which port to use (level_probe.port: /dev/...)."""
    r = _rig(monkeypatch, port=PROBE)
    yield r
    assert all(link.written == [] for link in r.links)


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
    assert rig.status["connected"] is False             # not yet known to be the probe
    assert "looking for the level probe" in rig.status["error"]
    rig.link.pending = REAL_LINE
    rig.poll()
    assert rig.mqtt.last[SENSOR] == {"voltage_v": 1.6694, "pwm": 129, "count": 742.453}
    assert rig.status == {"connected": True, "fresh": True, "port": PROBE, "error": "", "note": ""}


def test_line_split_across_reads(rig):
    rig.driver.connect()
    rig.link.pending = REAL_LINE[:20]
    rig.poll()
    assert SENSOR not in rig.mqtt.last
    rig.link.pending = REAL_LINE[20:] + b"median delta = 743.0 | pwm = 128\r\n"
    rig.poll()
    assert rig.mqtt.count[SENSOR] == 2
    assert rig.mqtt.last[SENSOR]["pwm"] == 128


def test_a_line_torn_by_opening_mid_way_is_not_held_against_it(rig):
    rig.driver.connect()
    rig.link.pending = b"ta = 742.453125 | pwm = 129\r\n" + REAL_LINE
    rig.poll()
    assert rig.status["connected"] is True and rig.mqtt.count[SENSOR] == 1


def test_goes_stale_when_the_sensor_stops(rig):
    rig.driver.connect()
    rig.link.pending = REAL_LINE
    rig.poll()
    rig.clock.mono += 16
    rig.poll()
    assert rig.status["fresh"] is False
    assert rig.status["connected"] is True              # it stays with a probe it has found


# ── telling the probe from the other CH340 board ─────────────────────────────

def test_takes_the_adapter_that_prints_the_probes_lines(rig):
    probe = rig.unplug(PROBE)
    cart = rig.plug(CART)                               # the cart board comes first
    rig.plug(OTHER, probe)
    cart.pending = CART_BLOCK
    rig.driver.connect()
    rig.poll()                                          # five lines that are not the probe's
    assert rig.opens == [CART]
    probe.pending = REAL_LINE
    rig.poll(2)
    assert rig.opens == [CART, OTHER]
    assert rig.status["connected"] is True and rig.status["port"] == OTHER
    assert rig.mqtt.last[SENSOR]["pwm"] == 129
    assert rig.status["note"] == ""                     # nothing of the cart's is reported

    cart.pending = CART_BLOCK * 3                       # and it is not listened to again
    rig.poll(50, dt=1.0)
    assert rig.opens == [CART, OTHER]


def test_an_adapter_in_use_by_something_else_is_skipped(rig):
    probe = rig.unplug(PROBE)
    rig.plug(CART)
    rig.plug(OTHER, probe)
    rig.busy.add(CART)                                  # the serial capture holds its lock
    rig.driver.connect()
    probe.pending = REAL_LINE
    rig.poll()
    assert rig.opens == [CART, OTHER] and rig.status["port"] == OTHER


def test_moving_the_cable_to_another_socket_needs_nothing(rig):
    cart = rig.plug(CART)
    rig.driver.connect()
    rig.link.pending = REAL_LINE
    rig.poll()
    assert rig.status["port"] == PROBE

    probe = rig.unplug(PROBE)                           # … out, and into another socket
    rig.poll()
    assert rig.status["connected"] is False
    rig.plug(OTHER, probe)
    cart.pending = CART_BLOCK
    probe.pending = REAL_LINE
    rig.poll(12)
    assert rig.status["connected"] is True and rig.status["port"] == OTHER
    assert rig.mqtt.count[SENSOR] == 2


def test_the_two_boards_swapping_sockets(rig):
    cart = rig.plug(CART)
    rig.driver.connect()
    rig.link.pending = REAL_LINE
    rig.poll()
    assert rig.status["port"] == PROBE

    probe = rig.unplug(PROBE)
    rig.unplug(CART)
    rig.poll()                                          # the open port goes away
    rig.plug(PROBE, cart)                               # each where the other was
    rig.plug(CART, probe)
    cart.pending = CART_BLOCK
    probe.pending = REAL_LINE
    rig.poll(12)
    assert rig.status["connected"] is True and rig.status["port"] == CART
    assert rig.mqtt.count[SENSOR] == 2


def test_a_silent_adapter_is_given_up_on_and_tried_again_later(rig):
    rig.driver.connect()
    rig.poll(60)                                        # 12 s of nothing
    assert rig.status["connected"] is False
    assert "still looking" in rig.status["error"]
    assert rig.opens == [PROBE]
    rig.poll(40, dt=1.0)                                # … and again after RETRY_S
    assert rig.opens.count(PROBE) >= 2
    rig.link.pending = REAL_LINE
    rig.poll(40, dt=1.0)
    assert rig.status["connected"] is True and rig.status["error"] == ""


def test_the_probe_is_known_by_its_other_messages_too(rig):
    rig.driver.connect()
    rig.link.pending = b"MPR121 liquid level calibration test\r\nMPR121 not found\r\n"
    rig.poll()
    assert rig.status["connected"] is True
    assert rig.status["note"] == "MPR121 not found"
    assert SENSOR not in rig.mqtt.last


def test_nothing_plugged_in(rig):
    probe = rig.unplug(PROBE)
    rig.driver.connect()                                # must not raise: the driver stays alive
    assert rig.status["connected"] is False
    assert "no USB-serial adapter" in rig.status["error"]
    rig.plug(PROBE, probe)
    probe.pending = REAL_LINE
    rig.poll(8)
    assert rig.mqtt.last[SENSOR]["pwm"] == 129


# ── a port named in the config is used without looking ──────────────────────

def test_named_port_is_read_without_identification(pinned):
    pinned.driver.connect()
    assert pinned.status["connected"] is True
    pinned.link.pending = b"CH0: 0.01 V  ->  0.75 mbar\r\nmedian delta = 1.0 | pwm = 999\r\n"
    pinned.poll()
    assert SENSOR not in pinned.mqtt.last
    assert pinned.status["note"] == "median delta = 1.0 | pwm = 999"
    pinned.link.pending = REAL_LINE
    pinned.poll()
    assert pinned.mqtt.last[SENSOR]["pwm"] == 129 and pinned.opens == [PROBE]


def test_named_port_unplug_and_replug(pinned):
    probe = pinned.unplug(PROBE)
    pinned.driver.connect()
    assert pinned.status["connected"] is False
    assert "No such file" in pinned.status["error"]
    pinned.plug(PROBE, probe)
    pinned.poll()                                       # too soon to retry
    assert pinned.status["connected"] is False
    pinned.clock.mono += 6
    probe.pending = REAL_LINE
    pinned.poll()
    assert pinned.mqtt.last[SENSOR]["pwm"] == 129

    probe.dead = True
    pinned.poll()
    assert pinned.status["connected"] is False
    probe.dead = False
    pinned.clock.mono += 6
    pinned.poll()
    assert pinned.status["connected"] is True


def test_shipped_config_and_registry():
    cfg = load("slowcontrol/config.yaml")
    assert cfg.level_probe.enabled and cfg.level_probe.vessel == "cryostat"
    assert cfg.level_probe.port == "auto" and cfg.level_probe.usb_id == "1a86:7523"
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
