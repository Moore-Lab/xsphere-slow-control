"""
Offline tests for the turbo pump monitor, against simulated pumps.

    python -m pytest tests/test_turbovac.py -q          (from the repo root)

slowcontrol/drivers/turbovac_sim.py answers the way the real TURBOVAC 90 i
did, so these pin the driver to that behaviour with no pump attached.  Every
test also ends with a check that no simulated pump was sent anything but a
read — the driver must not be able to operate a pump.
"""

import json
import os

import paho.mqtt.client as paho
import pytest

from slowcontrol.core.config import ServiceConfig, TurbovacPumpConfig, load
from slowcontrol.drivers import turbovac as tv_mod
from slowcontrol.drivers.turbovac import (ACCESS_NONE, ACCESS_READ, ACCESS_READ_FIELD, Identity,
                                          Reply, Turbovac, TurbovacDriver, TurbovacFrameError,
                                          TurbovacLinkError, parse_reply, read_telegram,
                                          state_of)
from slowcontrol.drivers.turbovac_sim import SimTurbovac
from slowcontrol.state import default_schema_path, load_state_schema
from slowcontrol.state.store import StateStore

GHS = "31003077799"             # the real GHS pump
OUTER = "31003088800"           # stand-ins
OTHER = "31003099900"

# What the real pump was sent and what it answered, 2026-10-11.
REAL_POLL = "02 16 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 14"
REAL_POLL_REPLY = "02 16 00 00 00 00 00 00 00 00 00 0f 04 04 b0 00 21 00 08 00 26 00 18 bc"
REAL_SERIAL_CHAR_REPLY = "02 16 00 41 63 00 00 00 00 00 33 0f 04 04 b0 00 21 00 08 00 26 00 18 ad"


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


class Clock:
    """Stands in for the `time` module inside the driver."""

    def __init__(self):
        self.mono = 1000.0
        self.wall = 1_760_000_000.0

    def monotonic(self):
        return self.mono

    def time(self):
        return self.wall

    def sleep(self, seconds):
        self.advance(seconds)

    def advance(self, seconds):
        self.mono += seconds
        self.wall += seconds


class Rig:
    """A Pi's worth of USB ports with simulated pumps on them, one broker, and
    the driver.  As shipped in config.yaml: `ghs` pinned to the real pump's
    serial number, `outer` not pinned yet."""

    def __init__(self, clock, sims):
        self.clock = clock
        self._sims = sims
        self.mqtt = FakeMqtt()
        self.config = ServiceConfig()
        self.config.turbovac.enabled = True
        self.config.turbovac.pumps = {
            "ghs": TurbovacPumpConfig("GHS turbo", GHS),
            "outer": TurbovacPumpConfig("Outer vacuum turbo", ""),
        }
        self.ports = {}                     # tty → the simulated pump on it
        self.opens = []                     # every tty the driver opened, in order
        self.driver = None

    def pump(self, port, serial=GHS, **kwargs):
        """A new pump, plugged into `port`."""
        sim = SimTurbovac(pump_serial=serial, **kwargs)
        self._sims.append(sim)
        self.ports[port] = sim
        return sim

    def unplug(self, port):
        sim = self.ports.pop(port)
        sim.unplug()
        return sim

    def plug(self, port, sim):
        sim.plug()
        self.ports[port] = sim

    def _open(self, port):
        self.opens.append(port)
        if port not in self.ports:
            raise TurbovacLinkError(f"cannot open {port}: No such file or directory")
        return self.ports[port]

    def start(self):
        self.driver = TurbovacDriver(self.config, self.mqtt,
                                     list_ports=lambda: sorted(self.ports),
                                     open_link=self._open)
        self.driver.connect()
        return self.driver

    def tick(self, n=1, dt=2.0):
        for _ in range(n):
            self.clock.advance(dt)
            self.driver.poll()

    def status(self, name="ghs"):
        return self.mqtt.last.get(f"xsphere/status/turbo/{name}")

    def reading(self, name="ghs"):
        return self.mqtt.last.get(f"xsphere/sensors/turbo/{name}")

    def readings(self, name="ghs"):
        return self.mqtt.count.get(f"xsphere/sensors/turbo/{name}", 0)


@pytest.fixture(autouse=True)
def sims():
    """Every simulated pump a test makes — and, afterwards, the proof that
    none of them was sent anything but a read."""
    made = []
    yield made
    for sim in made:
        assert sim.violations == [], "the driver sent a pump something other than a read"


@pytest.fixture
def clock(monkeypatch):
    c = Clock()
    monkeypatch.setattr(tv_mod, "time", c)
    return c


@pytest.fixture
def rig(clock, sims):
    """The GHS pump on /dev/ttyACM0, found and read once."""
    r = Rig(clock, sims)
    r.ghs = r.pump("/dev/ttyACM0", GHS)
    r.start()
    r.tick()
    return r


# ---------------------------------------------------------------------------
# The telegram
# ---------------------------------------------------------------------------

def test_status_poll_matches_what_the_real_pump_was_sent_and_answered():
    assert read_telegram().hex(" ") == REAL_POLL
    reply = parse_reply(bytes.fromhex(REAL_POLL_REPLY))
    assert reply.status_word == 0x0F04 and state_of(reply.status_word) == "normal"
    assert (reply.frequency_hz, reply.current_a, reply.voltage_v) == (1200, 0.8, 24)
    assert (reply.converter_temp_c, reply.bearing_temp_c) == (33, 38)
    assert reply.number() is None           # a status poll carries no parameter


def test_parameter_read_matches_the_real_exchange():
    telegram = read_telegram(ACCESS_READ_FIELD, 355, 0)
    assert telegram[3:5] == bytes.fromhex("6163") and telegram[6] == 0
    reply = parse_reply(bytes.fromhex(REAL_SERIAL_CHAR_REPLY))
    assert (reply.code, reply.pnu, reply.index) == (4, 355, 0)
    assert chr(reply.number()) == "3"       # first character of 31003077799


@pytest.mark.parametrize("access", [2, 3, 4, 5, 7, 8, 9, 15, -1])
def test_only_reads_can_be_built(access):
    with pytest.raises(ValueError):
        read_telegram(access, 24, 0)


@pytest.mark.parametrize("access, pnu, index", [
    (ACCESS_NONE, 0, 0), (ACCESS_READ, 24, 0), (ACCESS_READ, 1100, 0),
    (ACCESS_READ_FIELD, 171, 253), (ACCESS_READ_FIELD, 355, 17), (ACCESS_READ, 2047, 0),
])
def test_a_read_carries_no_value_no_control_word_and_no_setpoint(access, pnu, index):
    telegram = read_telegram(access, pnu, index)
    assert len(telegram) == 24 and not any(telegram[7:23])
    assert telegram[2] == 0 and telegram[5] == 0
    # the simulated pump agrees that it is harmless, and answers it
    sim = SimTurbovac()
    sim.exchange(telegram)
    assert sim.violations == []


@pytest.mark.parametrize("pnu, index", [(2048, 0), (-1, 0), (24, 256), (24, -1)])
def test_out_of_range_parameters_are_refused_before_they_reach_the_wire(pnu, index):
    with pytest.raises(ValueError):
        read_telegram(ACCESS_READ, pnu, index)


def test_the_sim_tells_on_anything_that_is_not_a_read():
    # the telegram that would take control and stop the pump (control word 0x0400)
    stop = bytearray(read_telegram())
    stop[11] = 0x04
    stop[23] ^= 0x04
    write = bytearray(read_telegram(ACCESS_READ, 24))
    write[3] = (2 << 4) | (write[3] & 0x0F)           # access code 2: write a 16-bit value
    write[23] = tv_mod._bcc(write)
    for bad in (stop, write):
        sim = SimTurbovac()
        sim.exchange(bytes(bad))
        assert len(sim.violations) == 1


def test_a_damaged_reply_is_rejected():
    good = bytes.fromhex(REAL_POLL_REPLY)
    for bad in (good[:-1] + b"\x00", good[:23], b"", b"\x03" + good[1:], good + b"\x00"):
        with pytest.raises(TurbovacFrameError):
            parse_reply(bad)


def test_signed_and_wide_values():
    def reply(code, raw):
        return Reply(code, 7, 0, raw, 0, 0, 0, 0.0, 0, 0)

    assert reply(1, 0xFFFB).number() == 0xFFFB and reply(1, 0xFFFB).number(signed=True) == -5
    assert reply(4, 56).number(signed=True) == 56
    assert reply(2, 656050).number() == 656050 and reply(5, 0xFFFFFFFF).number(signed=True) == -1
    assert reply(7, 3).number() is None and reply(8, 0).number() is None


@pytest.mark.parametrize("word, state", [
    (0x0F04, "normal"),                     # as read from the pump at full speed
    (0x0B14, "accelerating"),
    (0x0A21, "decelerating"),               # drive off, running down
    (0x0B04, "running"),                    # drive on, below normal speed, holding
    (0x0A00, "coasting"),
    (0x0201, "stopped"),
    (0x0A28, "error"),                      # an error outranks everything else
    (0x0F0C, "error"),
])
def test_state_words(word, state):
    assert state_of(word) == state


# ---------------------------------------------------------------------------
# The simulated pump answers like the real one
# ---------------------------------------------------------------------------

def test_sim_reproduces_what_was_read_from_the_pump():
    tv = Turbovac(SimTurbovac())
    assert tv.identify() == Identity(pump_serial=GHS, product="TMP T 90i",
                                     catalog_no="810041V1000", converter_serial="9801750017",
                                     converter_type=190, firmware="2.10.01",
                                     drive_firmware="3.03.00")
    assert tv.read(184) == 656050 and tv.read(682) == 1119878       # 32-bit hour counters
    assert tv.read(171, 0) == 101 and tv.read(176, 0) == 651144
    assert tv.read(171) == 101              # an indexed parameter, read without an index
    assert tv.read(3, 0) is None            # an unindexed one, read as a field: refused
    assert tv.read(355, 18) is None         # past the end of a string: refused
    assert tv.read(9) is None               # no such parameter: refused
    st = tv.status()
    assert (st.status_word, st.frequency_hz, st.voltage_v) == (0x0F04, 1200, 24)
    assert tv.read(4) == 238                # the same voltage, in 0.1 V


# ---------------------------------------------------------------------------
# Which pump is which
# ---------------------------------------------------------------------------

def test_the_ghs_pump_is_recognised_by_its_serial_number(rig):
    s = rig.status("ghs")
    assert s["connected"] is True and s["pinned"] is True and s["error"] is None
    assert (s["pump_serial"], s["configured_serial"]) == (GHS, GHS)
    assert (s["product"], s["catalog_no"], s["port"]) == ("TMP T 90i", "810041V1000",
                                                          "/dev/ttyACM0")
    assert (s["firmware"], s["drive_firmware"], s["converter_type"]) == ("2.10.01", "3.03.00", 190)
    assert (s["operating_hours"], s["electronics_hours"]) == (6560.5, 11198.78)
    assert (s["last_error_code"], s["last_error_hz"], s["last_error_hours"]) == (101, 1081, 6511.44)
    assert s["last_error"].startswith("overload warning")

    r = rig.reading("ghs")
    assert r["state"] == "normal" and r["frequency_hz"] == 1200 and r["setpoint_hz"] == 1200
    assert (r["current_a"], r["voltage_v"], r["power_w"]) == (0.8, 23.8, 12.9)
    assert (r["motor_temp_c"], r["bearing_temp_c"], r["converter_temp_c"]) == (56, 38, 33)
    assert (r["drive_on"], r["normal"], r["turning"], r["fault"], r["warning"]) == (1, 1, 1, 0, 0)
    assert (r["accelerating"], r["decelerating"], r["remote"]) == (0, 0, 0)
    assert (r["operating_hours"], r["start_count"], r["error_count"]) == (6560.5, 108, 8)

    # the outer vacuum pump is not connected yet, and nothing is made up for it
    o = rig.status("outer")
    assert o["connected"] is False and o["pinned"] is False and o["pump_serial"] is None
    assert "no unassigned TURBOVAC" in o["error"]
    assert rig.reading("outer") is None


def test_port_names_and_plug_in_order_do_not_decide_which_pump_is_which(clock, sims):
    rig = Rig(clock, sims)
    rig.config.turbovac.pumps["outer"].serial = OUTER
    outer = rig.pump("/dev/ttyACM0", OUTER)             # plugged in first this time
    outer.standstill()
    rig.pump("/dev/ttyACM1", GHS)
    rig.start()
    rig.tick()
    assert (rig.status("ghs")["pump_serial"], rig.status("ghs")["port"]) == (GHS, "/dev/ttyACM1")
    assert (rig.status("outer")["pump_serial"], rig.status("outer")["port"]) == (OUTER,
                                                                                "/dev/ttyACM0")
    assert rig.reading("ghs")["frequency_hz"] == 1200 and rig.reading("ghs")["state"] == "normal"
    assert rig.reading("outer")["frequency_hz"] == 0 and rig.reading("outer")["state"] == "stopped"


def test_swapping_the_usb_cables_keeps_the_names(clock, sims):
    rig = Rig(clock, sims)
    rig.config.turbovac.pumps["outer"].serial = OUTER
    ghs = rig.pump("/dev/ttyACM0", GHS)
    outer = rig.pump("/dev/ttyACM1", OUTER)
    outer.standstill()
    rig.start()
    rig.tick()
    assert rig.status("ghs")["port"] == "/dev/ttyACM0"

    rig.unplug("/dev/ttyACM0")
    rig.unplug("/dev/ttyACM1")
    rig.tick()
    assert not rig.status("ghs")["connected"] and not rig.status("outer")["connected"]
    rig.plug("/dev/ttyACM0", outer)                     # each cable into the other pump
    rig.plug("/dev/ttyACM1", ghs)
    rig.tick(4)
    assert (rig.status("ghs")["pump_serial"], rig.status("ghs")["port"]) == (GHS, "/dev/ttyACM1")
    assert (rig.status("outer")["pump_serial"], rig.status("outer")["port"]) == (OUTER,
                                                                                "/dev/ttyACM0")
    assert rig.reading("ghs")["frequency_hz"] == 1200
    assert rig.reading("outer")["frequency_hz"] == 0


def test_another_pump_is_never_reported_as_the_ghs_pump(clock, sims):
    rig = Rig(clock, sims)
    del rig.config.turbovac.pumps["outer"]
    rig.pump("/dev/ttyACM0", OTHER)
    rig.start()
    rig.tick(5)
    s = rig.status("ghs")
    assert s["connected"] is False and s["pump_serial"] is None
    assert f"pump serial {GHS} is not on USB" in s["error"]
    assert f"serial {OTHER} on /dev/ttyACM0" in s["error"]      # … and says what is there
    assert rig.reading("ghs") is None


def test_the_unpinned_outer_entry_takes_the_one_unassigned_pump(rig):
    rig.pump("/dev/ttyACM1", OUTER).standstill()
    rig.tick(4)
    o = rig.status("outer")
    assert o["connected"] is True and o["pinned"] is False
    assert o["pump_serial"] == OUTER and o["configured_serial"] is None
    assert "not pinned" in o["event"] and "turbovac.pumps.outer.serial" in o["event"]
    assert rig.reading("outer")["state"] == "stopped"
    assert rig.status("ghs")["pump_serial"] == GHS      # and the GHS pump is still itself


def test_the_unpinned_entry_cannot_take_the_ghs_pump(clock, sims):
    # Only the GHS pump is on USB, but it does not answer at first — so `ghs`
    # cannot claim it.  `outer` must not pick it up in the meantime.
    rig = Rig(clock, sims)
    ghs = rig.pump("/dev/ttyACM0", GHS)
    ghs.mute()
    rig.start()
    rig.tick(3)
    assert not rig.status("ghs")["connected"] and not rig.status("outer")["connected"]
    ghs.mute(False)
    rig.tick(4)
    assert rig.status("ghs")["pump_serial"] == GHS
    assert rig.status("outer")["connected"] is False


def test_a_port_is_checked_again_when_it_is_bound_whatever_was_seen_before(clock, sims):
    # Two unknown pumps: `outer` takes neither, and their serial numbers are
    # remembered for a minute.  Both are then unplugged and the GHS pump goes
    # into one of those ports.  The remembered serial must not be believed.
    rig = Rig(clock, sims)
    rig.pump("/dev/ttyACM0", OUTER)
    rig.pump("/dev/ttyACM1", OTHER)
    rig.start()
    rig.tick()
    assert "2 unassigned TURBOVACs" in rig.status("outer")["error"]
    rig.unplug("/dev/ttyACM0")
    rig.unplug("/dev/ttyACM1")
    rig.pump("/dev/ttyACM0", GHS)
    rig.tick(3)                                         # the first scan after the swap
    assert rig.status("outer")["connected"] is False
    assert "pinned elsewhere" in rig.status("outer")["error"]
    rig.tick(3)
    assert rig.status("ghs")["pump_serial"] == GHS and rig.status("ghs")["connected"] is True
    assert rig.status("outer")["connected"] is False


def test_two_unassigned_pumps_are_not_guessed_at(rig):
    rig.pump("/dev/ttyACM1", OUTER)
    rig.pump("/dev/ttyACM2", OTHER)
    rig.tick(10)
    o = rig.status("outer")
    assert o["connected"] is False and rig.reading("outer") is None
    assert "2 unassigned TURBOVACs" in o["error"] and "turbovac.pumps.outer.serial" in o["error"]
    assert OUTER in o["error"] and OTHER in o["error"]

    rig.unplug("/dev/ttyACM2")                          # one left: no longer a guess
    rig.tick(4)
    assert rig.status("outer")["pump_serial"] == OUTER


def test_two_unpinned_entries_take_nothing(clock, sims):
    rig = Rig(clock, sims)
    rig.config.turbovac.pumps["ghs"].serial = ""
    rig.pump("/dev/ttyACM0", GHS)
    rig.start()
    rig.tick(3)
    for name in ("ghs", "outer"):
        assert rig.status(name)["connected"] is False
        assert "only one may go unpinned" in rig.status(name)["error"]


@pytest.mark.parametrize("pumps, complaint", [
    ({}, "empty"),
    ({"ghs": GHS, "outer": GHS}, "both pinned"),
    ({"GHS": GHS}, "not a usable name"),
    ({"outer vacuum": ""}, "not a usable name"),
])
def test_a_config_that_cannot_work_stops_the_driver_rather_than_guessing(clock, sims, pumps,
                                                                         complaint):
    rig = Rig(clock, sims)
    rig.config.turbovac.pumps = {name: TurbovacPumpConfig(name, serial)
                                 for name, serial in pumps.items()}
    with pytest.raises(RuntimeError, match=complaint):
        rig.start()


def test_an_unassigned_pump_is_not_asked_who_it_is_on_every_scan(rig):
    rig.config.turbovac.pumps["outer"].serial = OUTER   # a pump that is not there
    rig.start()
    rig.pump("/dev/ttyACM1", OTHER)
    rig.opens.clear()
    rig.tick(60)                                        # two minutes, a scan every 5 s
    assert 2 <= rig.opens.count("/dev/ttyACM1") <= 4


# ---------------------------------------------------------------------------
# Losing and finding a pump
# ---------------------------------------------------------------------------

def test_a_pump_switched_off_is_reported_and_picked_up_again(rig):
    sim = rig.unplug("/dev/ttyACM0")                    # its 24 V supply goes off
    rig.tick()
    assert rig.status()["connected"] is False and rig.status()["error"]
    assert rig.status()["port"] is None and rig.status()["operating_hours"] is None
    published = rig.readings()
    rig.tick(5)
    assert rig.readings() == published                  # no stale numbers
    assert f"pump serial {GHS} is not on USB" in rig.status()["error"]

    rig.plug("/dev/ttyACM3", sim)                       # back, on whatever tty it gets
    rig.tick(4)
    assert rig.status()["connected"] is True and rig.status()["port"] == "/dev/ttyACM3"
    assert rig.readings() > published


def test_a_pump_that_stops_answering_is_dropped_and_tried_again(rig):
    rig.ghs.mute()
    rig.tick()
    assert rig.status()["connected"] is False and "no reply" in rig.status()["error"]
    rig.tick(3)
    assert rig.status()["connected"] is False
    rig.ghs.mute(False)
    rig.tick(4)
    assert rig.status()["connected"] is True


def test_one_damaged_reply_is_ridden_through_and_two_are_not(rig):
    published = rig.readings()
    rig.ghs.garble(1)
    rig.tick()
    assert rig.status()["connected"] is True and rig.readings() == published + 1

    rig.ghs.garble(2)                                   # the retry is damaged too
    rig.tick()
    assert rig.status()["connected"] is False and "damaged reply" in rig.status()["error"]
    rig.tick(4)
    assert rig.status()["connected"] is True


def test_one_pump_failing_does_not_stop_the_other_being_read(rig):
    outer = rig.pump("/dev/ttyACM1", OUTER)
    rig.tick(4)
    assert rig.status("outer")["connected"] is True
    outer.mute()
    before = rig.readings("ghs")
    rig.tick(10)
    assert rig.status("outer")["connected"] is False
    assert rig.readings("ghs") == before + 10


def test_a_driver_fault_never_reaches_the_service_watchdog(rig, monkeypatch):
    # poll() returning normally is what keeps SensorDriver's poll-liveness
    # stamp fresh; an exception every poll would get the whole service
    # restarted by its watchdog.
    def boom(*args, **kwargs):
        raise RuntimeError("bug")

    monkeypatch.setattr(rig.driver, "_measure", boom)
    rig.tick()                                          # must not raise
    assert rig.status()["connected"] is False
    assert "internal error" in rig.status()["error"]
    monkeypatch.undo()
    monkeypatch.setattr(tv_mod, "time", rig.clock)
    rig.tick(31)                                        # tried again a minute later
    assert rig.status()["connected"] is True


def test_stopping_the_service_says_so_and_leaves_the_pump_alone(rig):
    rig.driver.disconnect()
    assert rig.status()["connected"] is False and rig.status()["error"] == "service stopped"
    assert rig.ghs.drive_on and rig.ghs.frequency == 1200


# ---------------------------------------------------------------------------
# What the pump is doing
# ---------------------------------------------------------------------------

def test_running_down_and_up_again_is_named_step_by_step(rig):
    rig.ghs.stop(900)
    rig.tick()
    r = rig.reading()
    assert (r["state"], r["frequency_hz"]) == ("decelerating", 900)
    assert (r["drive_on"], r["normal"], r["turning"], r["decelerating"]) == (0, 0, 1, 1)
    assert rig.status()["event"] == "normal → decelerating at 900 Hz"

    rig.ghs.standstill()
    rig.tick()
    assert rig.reading()["state"] == "stopped" and rig.reading()["turning"] == 0

    rig.ghs.start(300)
    rig.tick()
    assert rig.reading()["state"] == "accelerating" and rig.reading()["normal"] == 0
    rig.ghs.frequency, rig.ghs.trend = 1000, 0          # held below 90 % by the gas load
    rig.tick()
    assert rig.reading()["state"] == "running"
    rig.ghs.at_speed()
    rig.tick()
    assert rig.reading()["state"] == "normal"
    assert rig.status()["event"] == "running → normal at 1200 Hz"
    rig.tick(31)                                        # the counters are re-read each minute
    assert rig.reading()["start_count"] == 109


def test_a_trip_is_reported_with_the_pumps_own_error_entry(rig):
    rig.ghs.frequency = 731
    rig.ghs.trip(106)
    rig.tick()                                          # seen on the very next poll
    r, s = rig.reading(), rig.status()
    assert (r["state"], r["fault"], r["drive_on"]) == ("error", 1, 0)
    assert (r["error_count"], r["last_error_code"]) == (9, 106)
    assert s["last_error"].startswith("overload failure") and s["last_error_hz"] == 731
    assert "logged 1 new error" in s["event"] and "106 (overload failure" in s["event"]


def test_an_error_logged_while_the_link_was_down_is_still_noticed(rig):
    sim = rig.unplug("/dev/ttyACM0")
    rig.tick()
    sim.log_error(101)
    sim.log_error(61)
    rig.plug("/dev/ttyACM0", sim)
    rig.tick(4)
    assert rig.status()["connected"] is True
    assert "logged 2 new errors" in rig.status()["event"]
    assert "61 (bearing temperature warning)" in rig.status()["event"]


def test_a_different_pump_on_an_unpinned_entry_starts_its_error_count_afresh(rig):
    first = rig.pump("/dev/ttyACM1", OUTER)
    rig.tick(4)
    assert rig.status("outer")["pump_serial"] == OUTER
    rig.unplug("/dev/ttyACM1")
    rig.tick()
    second = rig.pump("/dev/ttyACM1", OTHER)            # another pump, with a longer history
    second.params[40] = first.params[40] + 5
    rig.tick(4)
    assert rig.status("outer")["pump_serial"] == OTHER
    assert "new error" not in rig.status("outer")["event"]


def test_warnings_are_named(rig):
    rig.ghs.warning_bits = (1 << 11) | (1 << 0)
    rig.ghs.remote = True
    rig.tick()
    r = rig.reading()
    assert (r["warning"], r["warning_bits"], r["remote"], r["fault"]) == (1, 2049, 1, 0)
    assert rig.status()["warnings"] == ["pump temperature 1 high", "overload"]
    rig.ghs.warning_bits = 0
    rig.tick()
    assert rig.reading()["warning"] == 0 and rig.status()["warnings"] == []


def test_an_empty_error_memory_reads_none_not_unknown(clock, sims):
    rig = Rig(clock, sims)
    sim = rig.pump("/dev/ttyACM0", GHS)
    sim.params[171] = [0] * 254
    sim.params[40] = 0
    rig.start()
    rig.tick()
    s = rig.status()
    assert (s["last_error_code"], s["last_error"], s["last_error_hz"]) == (0, "none", None)


def test_values_a_pump_will_not_give_are_left_out_not_invented(clock, sims):
    rig = Rig(clock, sims)
    sim = rig.pump("/dev/ttyACM0", GHS)
    sim.missing = {4, 6, 7, 227, 184, 682, 1100}
    rig.start()
    rig.tick()
    r, s = rig.reading(), rig.status()
    assert s["connected"] is True
    assert "power_w" not in r and "motor_temp_c" not in r and "warning_bits" not in r
    assert "operating_hours" not in r and s["operating_hours"] is None
    assert r["voltage_v"] == 24                         # whole volts, from the process data
    assert s["drive_firmware"] == ""
    assert r["frequency_hz"] == 1200 and r["state"] == "normal"


# ---------------------------------------------------------------------------
# Read-only, as seen from the bus
# ---------------------------------------------------------------------------

def test_the_driver_listens_to_no_commands(rig):
    assert rig.mqtt.subs == []
    sent = len(rig.ghs.log)
    for topic in ("xsphere/commands/turbo/ghs/start", "xsphere/commands/turbo/ghs/stop",
                  "xsphere/commands/turbovac/ghs/setpoint", "xsphere/commands/turbo/ghs/state"):
        rig.mqtt.publish(topic, {"state": 0, "value": 0})
    assert len(rig.ghs.log) == sent
    assert rig.ghs.drive_on and rig.ghs.frequency == 1200


def test_everything_sent_in_a_long_eventful_session_was_a_read(rig):
    outer = rig.pump("/dev/ttyACM1", OUTER)
    rig.tick(40)
    rig.ghs.trip(106)
    rig.tick(5)
    rig.ghs.garble(1)
    outer.stop()
    rig.tick(40)
    rig.driver.disconnect()
    assert len(rig.ghs.log) > 300 and len(outer.log) > 300
    for telegram in rig.ghs.log + outer.log:
        assert (telegram[3] >> 4) in (0, 1, 6) and not any(telegram[7:23])


# ---------------------------------------------------------------------------
# The registry and the shipped config describe what the driver publishes
# ---------------------------------------------------------------------------

def test_state_yaml_matches_the_driver(rig):
    schema = load_state_schema(default_schema_path())
    store = StateStore(rig.config, rig.mqtt, schema)
    for topic in sorted(schema.subscribe_topics()):
        rig.mqtt.subscribe(topic, store._on_message)
    rig.tick()

    states = store.snapshot()["states"]
    turbo = {sid: st for sid, st in states.items() if st["group"] == "turbo"}
    ghs = {sid: st for sid, st in turbo.items() if sid.startswith("turbo_ghs_")}
    outer = {sid: st for sid, st in turbo.items() if sid.startswith("turbo_outer_")}
    assert len(ghs) == 15 and len(outer) == 15 and len(turbo) == 30
    assert {sid[len("turbo_ghs_"):] for sid in ghs} == {sid[len("turbo_outer_"):] for sid in outer}
    assert not any("control" in st for st in turbo.values())        # read-only

    for sid, st in ghs.items():
        assert st["value"] is not None and st["freshness"] == "fresh", sid
    assert ghs["turbo_ghs_connected"]["value"] is True
    assert ghs["turbo_ghs_state"]["value"] == "normal"
    assert ghs["turbo_ghs_state"]["value"] in ghs["turbo_ghs_state"]["values"]
    assert ghs["turbo_ghs_frequency"]["value"] == 1200 and ghs["turbo_ghs_setpoint"]["value"] == 1200
    assert ghs["turbo_ghs_current"]["value"] == 0.8 and ghs["turbo_ghs_voltage"]["value"] == 23.8
    assert ghs["turbo_ghs_power"]["value"] == 12.9
    assert ghs["turbo_ghs_motor_temp"]["value"] == 56 and ghs["turbo_ghs_bearing_temp"]["value"] == 38
    assert ghs["turbo_ghs_converter_temp"]["value"] == 33
    assert ghs["turbo_ghs_normal"]["value"] is True and ghs["turbo_ghs_fault"]["value"] is False
    assert ghs["turbo_ghs_warning"]["value"] is False
    assert ghs["turbo_ghs_operating_hours"]["value"] == 6560.5
    assert ghs["turbo_ghs_last_error"]["value"].startswith("overload warning")
    assert "science chamber" in ghs["turbo_ghs_connected"]["label"]

    # the pump that is not there: "not connected" is known, the rest is not
    assert outer["turbo_outer_connected"]["value"] is False
    for sid, st in outer.items():
        if sid != "turbo_outer_connected":
            assert st["value"] is None and st["freshness"] == "invalid", sid

    # every state the driver can be in is one the registry allows
    allowed = set(ghs["turbo_ghs_state"]["values"])
    assert {state_of(word) for word in range(0, 0x10000, 4)} <= allowed
    for word in (0x0201, 0x0A00, 0x0A21, 0x0B04, 0x0B14, 0x0F04, 0x0A28):
        assert state_of(word) in allowed


def test_config_yaml_names_this_pump_as_the_ghs_pump():
    repo = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
    cfg = load(os.path.join(repo, "slowcontrol", "config.yaml")).turbovac
    assert cfg.enabled is True
    assert list(cfg.pumps) == ["ghs", "outer"]
    assert cfg.pumps["ghs"].serial == GHS               # the pump read on 2026-10-11
    assert "gas handling" in cfg.pumps["ghs"].label and "science chamber" in cfg.pumps["ghs"].label
    assert "outer" in cfg.pumps["outer"].label.lower()
    # each pump in the config has its rows in the registry
    ids = {s.id for s in load_state_schema(default_schema_path()).states}
    for name in cfg.pumps:
        assert f"turbo_{name}_connected" in ids and f"turbo_{name}_frequency" in ids


def test_a_serial_number_written_without_quotes_is_still_text(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("turbovac:\n  enabled: true\n  poll_interval_s: 5\n  pumps:\n"
                    "    ghs: {label: GHS, serial: 31003077799}\n    outer:\n")
    cfg = load(str(path)).turbovac
    assert cfg.enabled is True and cfg.poll_interval_s == 5.0
    assert cfg.pumps["ghs"].serial == GHS and cfg.pumps["ghs"].label == "GHS"
    assert cfg.pumps["outer"].serial == "" and cfg.pumps["outer"].label == ""
    assert ServiceConfig().turbovac.enabled is False    # off unless config.yaml says so
