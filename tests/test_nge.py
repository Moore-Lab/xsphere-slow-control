"""
Offline tests for the NGE heater-supply driver, against the simulated supply.

    python -m pytest tests/test_nge.py -q          (from the repo root)

slowcontrol/drivers/nge_sim.py encodes how the real NGE103 behaved on the
bench, so these pin the driver to that behaviour with no hardware attached.
"""

import json

import paho.mqtt.client as paho
import pytest

from slowcontrol.core.config import ServiceConfig
from slowcontrol.drivers import nge as nge_mod
from slowcontrol.drivers.nge import Nge100, NgeDriver
from slowcontrol.drivers.nge_sim import SimNge
from slowcontrol.state import default_schema_path, load_state_schema
from slowcontrol.state.store import StateStore


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

    def advance(self, seconds):
        self.mono += seconds
        self.wall += seconds


class Rig:
    """One simulated supply with 16 Ω (36 W at 24 V) heaters, one broker, and
    as many driver lifetimes as a test wants."""

    def __init__(self, tmp_path, clock, sim=None):
        self.clock = clock
        self.sim = sim or SimNge(load_ohm=16.0)
        self.mqtt = FakeMqtt()
        self.config = ServiceConfig()
        self.config.nge.enabled = True
        self.state_path = str(tmp_path / "nge_state.json")
        self.driver = None

    def start(self):
        """A (re)start of the service: a new driver on a new broker session."""
        self.mqtt.subs = [s for s in self.mqtt.subs if getattr(s[1], "__self__", None)
                          is not self.driver]
        self.driver = NgeDriver(self.config, self.mqtt,
                                link_factory=lambda: self.sim,
                                state_path=self.state_path)
        self.driver.connect()
        return self.driver

    def tick(self, n=1, dt=1.0):
        for _ in range(n):
            self.clock.advance(dt)
            self.driver.poll()

    def cmd(self, what, payload):
        self.mqtt.publish(f"xsphere/commands/nge/{what}", payload)

    def status(self, ch=None):
        return self.mqtt.last.get("xsphere/status/nge" + (f"/{ch}" if ch else ""))

    def writes(self):
        """Everything sent to the supply that was not a query or a channel select."""
        return [c for c in self.sim.log
                if not c.endswith("?") and "?" not in c and not c.startswith("INST:NSEL")]

    def heat(self, ch=1, volts=24, amps=1.5):
        """Set a channel up as a heater feed and switch it on."""
        self.cmd(f"{ch}/voltage", {"value_v": volts})
        self.cmd(f"{ch}/current", {"value_a": amps})
        self.cmd(f"{ch}/output", {"state": 1})


@pytest.fixture
def clock(monkeypatch):
    c = Clock()
    monkeypatch.setattr(nge_mod, "time", c)
    return c


@pytest.fixture
def rig(tmp_path, clock):
    r = Rig(tmp_path, clock)
    r.start()
    r.tick()
    return r


# ---------------------------------------------------------------------------
# The supply itself
# ---------------------------------------------------------------------------

def test_sim_reproduces_the_bench_numbers():
    nge = Nge100(SimNge())
    assert nge.channels == (1, 2, 3) and nge.model == "NGE103"
    # 33.6 W envelope, exactly as read back from the real unit
    assert nge.set_setpoints(3, 24, 1.5) == (24.0, pytest.approx(1.4))
    assert nge.set_setpoints(3, 32, 3) == (32.0, pytest.approx(1.05))
    assert nge.set_setpoints(3, 11.2, 3) == (11.2, 3.0)
    assert nge.set_setpoints(3, 11.3, 3) == (11.3, pytest.approx(2.973451, abs=1e-6))
    assert nge.set_setpoints(3, 33, 0) == (32.2, 0.003)


def test_set_outputs_cannot_energise_a_channel_someone_else_armed():
    sim = SimNge()
    sim.press_channel(2)                    # armed at the front panel, master off
    Nge100(sim).set_outputs({1: True})
    assert sim.output(1)
    assert not sim.output(2) and not sim.ch[2].armed

    # ... which is what a plain OUTP ON does:
    sim = SimNge()
    sim.press_channel(2)
    sim.write("INST:NSEL 1")
    sim.write("OUTP ON")
    assert sim.output(1) and sim.output(2)


def test_a_trip_latches_one_channel_off_and_rearming_clears_it():
    sim = SimNge()
    nge = Nge100(sim)
    nge.set_outputs({1: True, 3: True})
    sim.trip(3, "ovp")
    assert sim.output(1) and not sim.output(3) and sim.master
    assert nge.read(3).trips == ["ovp"]
    nge.set_outputs({1: True, 3: True})
    assert sim.output(3) and nge.read(3).trips == []


# ---------------------------------------------------------------------------
# Start-up
# ---------------------------------------------------------------------------

def test_first_start_adopts_the_supply_and_only_removes_auto_off(rig):
    assert set(rig.writes()) == {"POW:PROT OFF"}
    assert not any(ch.opp for ch in rig.sim.ch.values())
    assert rig.status()["connected"] is True and rig.status()["hold"] is True
    assert rig.status()["model"] == "NGE103"
    for ch in (1, 2, 3):
        s = rig.status(ch)
        assert (s["output"], s["desired"], s["mode"]) == (0, 0, "off")
        assert (s["voltage_set_v"], s["current_limit_a"]) == (5.0, 1.0)
        assert s["opp"] == 0 and s["trip"] == "none" and s["dropouts"] == 0


def test_auto_off_removal_can_be_disabled(tmp_path, clock):
    rig = Rig(tmp_path, clock)
    rig.config.nge.disable_auto_off = False
    rig.start()
    rig.tick()
    assert rig.writes() == []
    assert all(ch.opp for ch in rig.sim.ch.values())


def test_a_lowered_ovp_level_is_raised_back_to_the_maximum(rig):
    rig.sim.ch[2].ovp_level = 25.0
    rig.tick()
    assert rig.sim.ch[2].ovp_level == 32.5 and rig.sim.ch[2].ovp


def test_wrong_serial_number_is_not_driven(tmp_path, clock):
    rig = Rig(tmp_path, clock)
    rig.config.nge.serial = "101129"        # the sim is 000000
    rig.start()
    rig.tick()
    assert rig.status()["connected"] is False
    assert "serial" in rig.status()["error"]
    assert rig.writes() == [] or set(rig.writes()) == {"SYST:LOC"}

    rig = Rig(tmp_path, clock, SimNge(serial="101129"))
    rig.config.nge.serial = "101129"
    rig.start()
    rig.tick()
    assert rig.status()["connected"] is True


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def test_output_command_switches_only_that_channel(rig):
    rig.cmd("2/output", {"state": 1})
    assert [rig.sim.output(c) for c in (1, 2, 3)] == [False, True, False]
    assert rig.status(2)["output"] == 1 and rig.status(2)["desired"] == 1
    rig.cmd("2/output", {"state": 0})
    assert not rig.sim.output(2) and not rig.sim.master
    assert rig.status(2)["desired"] == 0


def test_24v_1a5_is_capped_to_1a4_and_not_rewritten_every_poll(rig):
    rig.heat(1, 24, 1.5)
    s = rig.status(1)
    assert (s["voltage_set_v"], s["current_limit_a"]) == (24.0, 1.4)
    assert (s["voltage_request_v"], s["current_request_a"]) == (24.0, 1.5)
    # a 16 Ω heater wants 1.5 A: the supply limits at 1.4 A and sags to 22.4 V
    assert s["mode"] == "cc"
    assert s["voltage_v"] == pytest.approx(22.4) and s["power_w"] == pytest.approx(31.36)

    rig.sim.log.clear()
    rig.tick(5)
    assert rig.writes() == []               # the capped readback counts as in sync


def test_lowering_the_voltage_lets_the_requested_current_through(rig):
    rig.heat(1, 24, 1.5)
    rig.cmd("1/voltage", {"value_v": 20})
    s = rig.status(1)
    assert (s["voltage_set_v"], s["current_limit_a"]) == (20.0, 1.5)
    assert s["mode"] == "cv"                # 20 V / 16 Ω = 1.25 A, under the limit


@pytest.mark.parametrize("what, payload", [
    ("1/output", {"state": 2}),
    ("1/output", {"state": "1"}),
    ("1/output", {"on": 1}),
    ("1/output", "on"),
    ("1/voltage", {"value_v": "abc"}),
    ("1/voltage", {"value_v": True}),
    ("1/voltage", {"value_v": -1}),
    ("1/voltage", {"value_v": 40}),
    ("1/voltage", {}),
    ("1/current", {"value_a": 3.5}),
    ("1/current", {"value_a": None}),
    ("4/output", {"state": 1}),
    ("x/output", {"state": 1}),
    ("hold", {"enabled": "yes"}),
])
def test_bad_commands_change_nothing(rig, what, payload):
    rig.sim.log.clear()
    rig.cmd(what, payload)
    rig.tick()
    assert rig.writes() == []
    assert not any(rig.sim.output(c) for c in (1, 2, 3))
    assert rig.status()["hold"] is True


def test_two_channel_supply_has_no_channel_three(tmp_path, clock):
    rig = Rig(tmp_path, clock, SimNge(n_channels=2))
    rig.start()
    rig.tick()
    assert rig.status()["model"] == "NGE102"
    assert rig.status(2) is not None and rig.status(3) is None
    rig.cmd("3/output", {"state": 1})
    assert not any(rig.sim.output(c) for c in (1, 2))


# ---------------------------------------------------------------------------
# Hold
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("kind, reason", [("opp", "OPP trip"), ("ovp", "OVP trip"),
                                          ("fuse", "fuse trip")])
def test_hold_switches_a_tripped_channel_back_on(rig, kind, reason):
    rig.heat(1)
    rig.heat(3)
    rig.sim.trip(1, kind)
    assert not rig.sim.output(1)
    rig.tick()
    assert rig.sim.output(1) and rig.sim.output(3)
    s = rig.status(1)
    assert (s["output"], s["trip"], s["dropouts"], s["last_dropout"]) == (1, "none", 1, reason)
    assert rig.status(3)["dropouts"] == 0
    assert "switched back on" in rig.status()["event"]


def test_hold_undoes_front_panel_switching_in_both_directions(rig):
    rig.heat(1)
    rig.sim.press_output()                  # master off at the panel
    rig.tick()
    assert rig.sim.output(1)
    assert rig.status(1)["last_dropout"] == "master output switched off at the supply"

    rig.tick()                              # seen back on: that drop-out is over
    rig.sim.press_channel(1)                # channel key
    rig.tick()
    assert rig.sim.output(1)
    assert rig.status(1)["last_dropout"] == "switched off at the supply"
    assert rig.status(1)["dropouts"] == 2

    rig.sim.press_channel(2)                # someone switches ch2 ON
    assert rig.sim.output(2)
    rig.tick()
    assert not rig.sim.output(2) and rig.sim.output(1)
    assert rig.status(2)["dropouts"] == 0


def test_off_means_disarmed_not_just_unpowered(rig):
    # Found on the real supply: with the master off, an armed channel reads
    # "off" — and comes on with one press of the Output key.
    rig.cmd("hold", {"enabled": False})
    rig.heat(3)
    rig.sim.press_output()                  # master off at the panel; ch3 still armed
    rig.tick()
    assert rig.sim.ch[3].armed and not rig.sim.output(3)
    rig.cmd("3/output", {"state": 0})
    assert not rig.sim.ch[3].armed and not rig.sim.master

    rig.cmd("hold", {"enabled": True})
    rig.sim.press_channel(2)                # armed at the panel, master off: no output yet
    assert not rig.sim.output(2)
    rig.tick()
    assert not rig.sim.ch[2].armed          # hold does not leave it lying in wait
    assert rig.status(2)["armed"] == 0 and rig.status(2)["dropouts"] == 0


def test_a_command_with_hold_off_cannot_wake_an_armed_channel(rig):
    rig.cmd("hold", {"enabled": False})
    rig.sim.press_channel(1)                # someone armed ch1 and walked away
    rig.tick()
    rig.cmd("2/output", {"state": 1})       # raising the master for ch2 …
    assert rig.sim.output(2)
    assert not rig.sim.output(1)            # … must not bring ch1 up with it


def test_hold_puts_back_setpoints_changed_at_the_supply(rig):
    rig.heat(1)
    rig.sim.write("INST:NSEL 1")
    rig.sim.write("VOLT 12")                # knob turned at the front panel
    rig.tick()
    assert rig.status(1)["voltage_set_v"] == 24.0 and rig.status(1)["current_limit_a"] == 1.4
    assert "restored" in rig.status()["event"]


def test_power_cycle_of_the_supply_is_fully_restored(rig):
    rig.heat(1)
    rig.heat(2, 12, 2.0)
    rig.sim.power_cycle()                   # outputs off, M1 setpoints, OPP on again
    assert not rig.sim.output(1) and rig.sim.ch[1].volt == 5.0 and rig.sim.ch[1].opp
    rig.tick()
    assert rig.sim.output(1) and rig.sim.output(2) and not rig.sim.output(3)
    assert (rig.sim.ch[1].volt, rig.sim.ch[1].curr) == (24.0, pytest.approx(1.4))
    assert (rig.sim.ch[2].volt, rig.sim.ch[2].curr) == (12.0, 2.0)
    assert not any(ch.opp for ch in rig.sim.ch.values())
    assert rig.status(1)["last_dropout"] == "the supply was power-cycled"


def _arm_attempts(log, channel):
    selected, n = None, 0
    for command in log:
        if command.startswith("INST:NSEL "):
            selected = int(command.split()[1])
        elif command == "OUTP:SEL ON" and selected == channel:
            n += 1
    return n


def test_a_channel_that_will_not_stay_on_is_retried_with_back_off(tmp_path, clock):
    class Stuck(SimNge):
        stuck = True

        def _arm(self, c, on):
            super()._arm(c, on)
            if on and c == 1 and self.stuck:
                self.trip(1, "ovp")

    rig = Rig(tmp_path, clock, Stuck(load_ohm=16.0))
    rig.start()
    rig.tick()
    rig.heat(2)
    rig.cmd("1/output", {"state": 1})
    rig.sim.log.clear()
    rig.tick(120)                           # two minutes of it refusing
    attempts = _arm_attempts(rig.sim.log, 1)
    assert 4 <= attempts <= 8, attempts     # 0, 2, 4, 8, 16, 32, 60 s apart — not 120 tries
    assert rig.status(1)["dropouts"] == 1   # one drop-out, however long it lasts
    assert rig.sim.output(2)                # the healthy channel never blinked
    assert rig.status(2)["dropouts"] == 0

    rig.sim.stuck = False
    rig.tick(61)
    assert rig.sim.output(1)
    rig.tick(61)                            # stable for a minute: back-off forgotten
    rig.sim.trip(1, "opp")
    rig.tick()
    assert rig.sim.output(1) and rig.status(1)["dropouts"] == 2


def test_fuse_is_switched_off_but_never_while_the_channel_is_on(rig):
    rig.sim.ch[2].fuse = True               # enabled at the panel, channel off
    rig.tick()
    assert not rig.sim.ch[2].fuse

    rig.heat(1, 12, 2.0)                    # 0.75 A into 16 Ω: below the limit
    rig.sim.ch[1].fuse = True               # the supply refuses a change while on
    rig.sim.log.clear()
    rig.tick(3)
    assert "FUSE OFF" not in rig.sim.log and rig.sim.ch[1].fuse
    assert rig.status(1)["fuse"] == 1 and rig.sim.output(1)

    rig.cmd("1/voltage", {"value_v": 24})   # now the heater wants more than the
    rig.cmd("1/current", {"value_a": 1.4})  # limit, and a fuse trips on that
    assert not rig.sim.output(1) and rig.sim.ch[1].fuse_trip
    rig.tick()                              # off, so the fuse can be removed
    assert not rig.sim.ch[1].fuse and rig.sim.output(1)
    rig.tick()
    assert rig.status(1)["fuse"] == 0 and rig.status(1)["mode"] == "cc"
    assert rig.status(1)["last_dropout"] == "fuse trip"


def test_hold_off_only_watches(rig):
    rig.heat(1)
    rig.cmd("hold", {"enabled": False})
    assert rig.status()["hold"] is False

    rig.sim.trip(1, "opp")
    rig.sim.press_channel(2)
    rig.sim.write("INST:NSEL 3")
    rig.sim.write("VOLT 9")
    rig.sim.log.clear()
    rig.tick(3)
    assert rig.writes() == []               # nothing put back
    assert not rig.sim.output(1) and rig.sim.output(2)
    assert rig.status(1)["desired"] == 0 and rig.status(2)["desired"] == 1
    assert rig.status(1)["trip"] == "opp"

    rig.cmd("3/output", {"state": 1})       # a command still moves just its target
    assert [rig.sim.output(c) for c in (1, 2, 3)] == [False, True, True]
    assert rig.sim.ch[3].volt == 9.0


def test_switching_hold_on_holds_the_supply_as_it_is(rig):
    rig.cmd("hold", {"enabled": False})
    rig.sim.press_channel(2)
    rig.sim.press_output()                  # ch2 on by hand
    rig.tick()
    rig.cmd("hold", {"enabled": True})
    assert rig.status(2)["desired"] == 1 and rig.status(1)["desired"] == 0
    rig.sim.trip(2, "opp")
    rig.tick()
    assert rig.sim.output(2) and not rig.sim.output(1)


# ---------------------------------------------------------------------------
# Losing and finding the supply, restarts
# ---------------------------------------------------------------------------

def test_unplugged_supply_is_reported_and_picked_up_again(rig):
    rig.heat(1)
    rig.sim.unplug()
    rig.tick()
    assert rig.status()["connected"] is False and rig.status()["error"]
    published = rig.mqtt.count["xsphere/status/nge/1"]
    rig.tick(3)
    assert rig.mqtt.count["xsphere/status/nge/1"] == published   # no stale numbers

    rig.cmd("2/output", {"state": 1})       # ON while away: refused, not queued
    rig.cmd("1/output", {"state": 0})       # OFF while away: remembered
    rig.sim.plug()
    rig.sim.power_cycle()
    rig.tick(8)
    assert rig.status()["connected"] is True
    assert not rig.sim.output(1) and not rig.sim.output(2)
    assert rig.sim.ch[1].volt == 24.0       # the held setpoints did come back


def test_service_restart_carries_on_holding(rig):
    rig.heat(1)
    rig.driver.disconnect()
    assert rig.sim.output(1)                # stopping the service touches nothing
    assert rig.status()["connected"] is False

    rig.clock.advance(300)
    rig.sim.power_cycle()                   # power cut while the service was down
    rig.start()
    rig.tick()
    assert rig.status()["hold"] is True
    assert rig.sim.output(1) and rig.sim.ch[1].volt == 24.0
    assert rig.status(1)["current_request_a"] == 1.5
    assert "power-cycled" in rig.status(1)["last_dropout"]


def test_a_stale_held_state_is_not_reimposed(rig):
    rig.heat(1)
    rig.driver.disconnect()
    rig.clock.advance(3 * 86400)            # three days with nobody watching
    rig.sim.power_cycle()
    rig.start()
    rig.tick()
    assert not rig.sim.output(1)
    assert rig.status(1)["desired"] == 0 and rig.status(1)["voltage_set_v"] == 5.0
    assert "adopted" in rig.status()["event"] and rig.status()["hold"] is True


def test_hold_choice_survives_a_restart(rig):
    rig.heat(1)
    rig.cmd("hold", {"enabled": False})
    rig.driver.disconnect()
    rig.sim.power_cycle()
    rig.start()
    rig.tick()
    assert rig.status()["hold"] is False and not rig.sim.output(1)


def test_a_driver_fault_never_reaches_the_service_watchdog(rig, monkeypatch):
    # poll() returning normally is what keeps SensorDriver's poll-liveness
    # stamp fresh; an exception every poll would get the whole service
    # restarted by its watchdog.
    def boom(*args, **kwargs):
        raise RuntimeError("bug")

    monkeypatch.setattr(rig.driver, "_enforce", boom)
    rig.tick()                              # must not raise
    assert rig.status()["connected"] is False
    assert "internal error" in rig.status()["error"]
    monkeypatch.undo()
    monkeypatch.setattr(nge_mod, "time", rig.clock)
    rig.tick(61)                            # retried a minute later, and fine again
    assert rig.status()["connected"] is True


def test_unreadable_state_file_falls_back_to_adopting(tmp_path, clock):
    rig = Rig(tmp_path, clock)
    with open(rig.state_path, "w") as fh:
        fh.write("{not json")
    rig.start()
    rig.tick()
    assert rig.status()["connected"] is True and rig.status(1)["desired"] == 0


# ---------------------------------------------------------------------------
# The registry describes what the driver publishes
# ---------------------------------------------------------------------------

def test_state_yaml_matches_the_driver(rig):
    schema = load_state_schema(default_schema_path())
    store = StateStore(rig.config, rig.mqtt, schema)
    for topic in sorted(schema.subscribe_topics()):
        rig.mqtt.subscribe(topic, store._on_message)
    rig.heat(1)
    rig.tick()

    states = store.snapshot()["states"]
    ours = {sid: st for sid, st in states.items() if sid.startswith("nge_")}
    assert len(ours) == 33
    for sid, st in ours.items():
        assert st["value"] is not None and st["freshness"] == "fresh", sid
    assert ours["nge_connected"]["value"] is True and ours["nge_hold"]["value"] is True
    assert ours["nge_ch1_output"]["value"] is True and ours["nge_ch2_output"]["value"] is False
    assert ours["nge_ch1_mode"]["value"] == "cc" and ours["nge_ch2_mode"]["value"] == "off"
    assert ours["nge_ch1_mode"]["values"] == ["off", "cv", "cc"]
    assert ours["nge_ch1_trip"]["value"] == "none"
    assert ours["nge_ch1_current_limit"]["value"] == 1.4
    assert ours["nge_power_total"]["value"] == pytest.approx(31.36)

    # every control in the registry does what its label says
    def actuate(sid, value):
        ctl = states[sid]["control"]
        payload = {k: (value if v == "$value" else int(bool(value)) if v == "$value01" else v)
                   for k, v in ctl["payload"].items()}
        rig.mqtt.publish(ctl["topic"], payload)

    actuate("nge_ch2_output", True)
    actuate("nge_ch3_voltage_set", 12.5)
    actuate("nge_ch3_current_limit", 0.75)
    assert rig.sim.output(2)
    assert (rig.sim.ch[3].volt, rig.sim.ch[3].curr) == (12.5, 0.75)
    actuate("nge_ch2_output", False)
    actuate("nge_hold", False)
    assert not rig.sim.output(2) and rig.status()["hold"] is False
