"""
Offline tests for the level-stale grace (aicontrol/arms.py, Supervisor): a
not-ok interlock made only of the GHS ESP32's level_stale rules holds the AI's
heater writes, and disarms the heaters only if it outlasts
XSPHERE_AICONTROL_LEVEL_STALE_GRACE_S (default 120 s).  At 0 every not-ok
disarms at once, as before.

    python -m pytest tests/test_aicontrol_grace.py -q      (from the repo root)

Uses the rig of test_aicontrol_actions.py: no broker, a fake clock.  The
timeline tests at the end also run the real watchdog of
slowcontrol/controllers/interlocks.py on that clock.
"""

from types import SimpleNamespace

import pytest

import slowcontrol.controllers.interlocks as interlocks_py
from aicontrol.app import Service
from aicontrol.arms import GRACE_RULES
from aicontrol.audit import KINDS
from aicontrol.bus import LoopbackTransport
from aicontrol.config import (DEFAULT_LEVEL_STALE_GRACE_S, MAX_LEVEL_STALE_GRACE_S,
                              ConfigError, Settings)

from test_aicontrol_actions import Clock, Rig, assert_ok, assert_refused

INTERLOCKS = "xsphere/status/interlocks"
ESP32_RULES = ["level_stale/ballast", "level_stale/primary_xe"]
SETPOINT = {"setpoints": {"bottom": 167}, "reason": "r"}


def not_ok(rig, rules):
    rig.fake.pub(INTERLOCKS, {"rules_active": rules, "ok": False})


def ok(rig):
    rig.fake.pub(INTERLOCKS, {"rules_active": [], "ok": True})


def notes(rig):
    """The audit's hold entries (kind heater_hold)."""
    return [e["detail"] for e in rig.svc.audit.recent(200) if e["kind"] == "heater_hold"]


@pytest.fixture
def rig(tmp_path):
    return Rig(tmp_path)


@pytest.fixture
def grace_rig(tmp_path):
    r = Rig(tmp_path)
    r.svc.supervisor.level_stale_grace_s = 90.0
    return r


@pytest.fixture
def no_grace_rig(tmp_path):
    r = Rig(tmp_path)
    r.svc.supervisor.level_stale_grace_s = 0.0
    return r


# ──────────────────────────────────────────────────────────────────────────
# Settings
# ──────────────────────────────────────────────────────────────────────────

def test_grace_rules_are_the_esp32_levels_only():
    assert GRACE_RULES == {"level_stale/ballast", "level_stale/primary_xe"}


def test_settings_default_and_bounds(tmp_path):
    assert DEFAULT_LEVEL_STALE_GRACE_S == 120.0
    assert Settings(state_dir=tmp_path).level_stale_grace_s == 120.0
    assert Settings(state_dir=tmp_path, level_stale_grace_s=90).level_stale_grace_s == 90.0
    assert Settings(state_dir=tmp_path, level_stale_grace_s=0).level_stale_grace_s == 0.0
    for bad in (-1, MAX_LEVEL_STALE_GRACE_S + 1, float("nan"), float("inf"), True, "abc", None):
        with pytest.raises(ConfigError):
            Settings(state_dir=tmp_path, level_stale_grace_s=bad)


def test_settings_from_env():
    assert Settings.from_env({}).level_stale_grace_s == 120.0
    for unset in ("", "  "):
        env = {"XSPHERE_AICONTROL_LEVEL_STALE_GRACE_S": unset}
        assert Settings.from_env(env).level_stale_grace_s == 120.0
    env = {"XSPHERE_AICONTROL_LEVEL_STALE_GRACE_S": " 90 "}
    assert Settings.from_env(env).level_stale_grace_s == 90.0
    env = {"XSPHERE_AICONTROL_LEVEL_STALE_GRACE_S": "0"}
    assert Settings.from_env(env).level_stale_grace_s == 0.0
    for bad in ("ninety", "-5", "601"):
        with pytest.raises(ConfigError, match="XSPHERE_AICONTROL_LEVEL_STALE_GRACE_S"):
            Settings.from_env({"XSPHERE_AICONTROL_LEVEL_STALE_GRACE_S": bad})


def test_service_takes_the_grace_from_its_settings(tmp_path):
    svc = Service(Settings(state_dir=tmp_path, level_stale_grace_s=45), LoopbackTransport(),
                  clock=Clock(), history=None)
    assert svc.supervisor.level_stale_grace_s == 45.0
    st = svc.status_payload()
    assert st["level_stale_grace_s"] == 45.0 and st["heater_hold"] is None


def test_the_default_service_holds(rig):
    assert rig.svc.supervisor.level_stale_grace_s == 120.0
    assert rig.svc.status_payload()["level_stale_grace_s"] == 120.0
    rig.arm("heaters")
    not_ok(rig, ESP32_RULES)
    assert rig.svc.arms.is_armed("heaters")
    assert rig.svc.supervisor.heater_hold()["rules"] == ESP32_RULES


# ──────────────────────────────────────────────────────────────────────────
# Grace 0: the behaviour before 2026-10-30
# ──────────────────────────────────────────────────────────────────────────

def test_without_a_grace_level_stale_disarms_heaters_at_once(no_grace_rig):
    rig = no_grace_rig
    rig.arm("heaters", "ln2")
    not_ok(rig, ESP32_RULES)
    assert rig.svc.arms.armed_groups() == ["ln2"]
    assert "level_stale/ballast, level_stale/primary_xe" in \
        rig.svc.arms.last_disarm("heaters")["reason"]
    assert rig.svc.supervisor.heater_hold() is None
    assert notes(rig) == []


# ──────────────────────────────────────────────────────────────────────────
# With a grace
# ──────────────────────────────────────────────────────────────────────────

def test_hold_refuses_heater_writes_and_keeps_the_arm(grace_rig):
    rig = grace_rig
    rig.arm("heaters", "ln2")
    not_ok(rig, ESP32_RULES)
    assert rig.svc.arms.armed_groups() == ["heaters", "ln2"]
    hold = rig.svc.supervisor.heater_hold()
    assert hold["rules"] == ESP32_RULES
    assert any("heaters held, not disarmed" in d for d in notes(rig))
    # The service's own entry, not one of the AI's notes.
    assert "heater_hold" in KINDS
    assert not [e for e in rig.svc.audit.recent(200) if e["kind"] == "note"]
    assert rig.svc.actions.stats()["notes"] == 0
    out = assert_refused(rig.call("set_heater_setpoints", SETPOINT), "heater writes are held",
                         "level_stale/ballast", hold["disarm_at_utc"])
    assert out.get("published", []) == []
    assert rig.cmds() == []
    # Only the heaters are held: an LN2 close still goes out.
    assert_ok(rig.call("set_ln2_valve", {"vessel": "cryostat", "open": False, "reason": "r"}))


def test_interlocks_ok_again_ends_the_hold(grace_rig):
    rig = grace_rig
    rig.arm("heaters")
    not_ok(rig, ESP32_RULES)
    rig.advance(30)
    not_ok(rig, ESP32_RULES)                 # the watchdog repeats itself every 15 s
    assert rig.svc.supervisor.heater_hold() is not None
    ok(rig)
    assert rig.svc.supervisor.heater_hold() is None
    assert rig.svc.arms.is_armed("heaters") and rig.svc.arms.auto_disarms == 0
    assert any("ok again after 30 s" in d for d in notes(rig))
    assert_ok(rig.call("set_heater_setpoints", SETPOINT))


def test_grace_running_out_disarms_the_heaters(grace_rig):
    rig = grace_rig
    rig.arm("heaters", "pneumatic")
    not_ok(rig, ESP32_RULES)
    rig.advance(89)
    rig.svc.supervisor.tick()
    assert rig.svc.arms.is_armed("heaters")
    rig.advance(2)
    rig.svc.supervisor.tick()
    assert rig.svc.arms.armed_groups() == ["pneumatic"]
    assert "stayed not-ok (level_stale/ballast, level_stale/primary_xe) past the 90 s " \
           "level-stale grace" in rig.svc.arms.last_disarm("heaters")["reason"]
    assert rig.svc.supervisor.heater_hold() is None and rig.svc.arms.auto_disarms == 1
    # The same rules on the next watchdog message: no second hold, and a
    # re-arm by Lucas sticks (as it does today while an interlock stays not-ok).
    rig.arm("heaters")
    not_ok(rig, ESP32_RULES)
    assert rig.svc.arms.is_armed("heaters") and rig.svc.supervisor.heater_hold() is None
    assert rig.svc.arms.auto_disarms == 1


def test_any_other_rule_ends_the_hold_and_disarms_at_once(grace_rig):
    rig = grace_rig
    rig.arm("heaters")
    not_ok(rig, ESP32_RULES)
    assert rig.svc.arms.is_armed("heaters")
    not_ok(rig, ESP32_RULES + ["temperature_stale/plc/rtd/5"])
    assert not rig.svc.arms.is_armed("heaters")
    assert "temperature_stale/plc/rtd/5" in rig.svc.arms.last_disarm("heaters")["reason"]
    assert rig.svc.supervisor.heater_hold() is None


@pytest.mark.parametrize("rules", [
    ["pid_saturated/top"],
    ["level_stale/cryostat"],                       # the PLC driver's, not the ESP32's
    ["level_stale/ballast", "temperature_range/labjack/rtd/2"],
    [],                                             # not ok, no rule named
])
def test_the_grace_covers_only_the_esp32_level_rules(grace_rig, rules):
    rig = grace_rig
    rig.arm("heaters")
    not_ok(rig, rules)
    assert not rig.svc.arms.is_armed("heaters")
    assert rig.svc.supervisor.heater_hold() is None


def test_a_retained_not_ok_starts_no_hold(grace_rig):
    rig = grace_rig
    rig.arm("heaters")
    rig.transport.inject(INTERLOCKS, {"rules_active": ESP32_RULES, "ok": False}, retain=True)
    assert rig.svc.supervisor.heater_hold() is None and rig.svc.arms.is_armed("heaters")


def test_reconnect_ends_the_hold(grace_rig):
    rig = grace_rig
    rig.arm("heaters")
    not_ok(rig, ESP32_RULES)
    rig.transport.set_connected(False)
    rig.transport.set_connected(True)
    assert rig.svc.supervisor.heater_hold() is None
    assert rig.svc.arms.armed_groups() == []


def test_hold_stops_a_running_ramp(grace_rig):
    rig = grace_rig
    rig.arm("heaters")
    assert_ok(rig.call("start_heater_ramp",
                       {"targets": {"top": 175}, "rate_k_per_min": 1, "reason": "r"}))
    rig.transport.clear()
    not_ok(rig, ESP32_RULES)
    rig.advance(60)
    rig.svc.ramps.tick()
    assert rig.svc.ramps.status() == []
    assert rig.cmds() == []
    assert rig.svc.arms.is_armed("heaters")
    assert any("heater writes are held" in e["detail"] for e in rig.svc.audit.recent(10))


def test_hold_mid_call_stops_the_remaining_publishes(grace_rig):
    rig = grace_rig
    rig.arm("heaters")

    def hook(topic, payload):
        if topic.endswith("/top/setpoint"):
            not_ok(rig, ESP32_RULES)
    rig.fake.on_command_hook = hook
    out = assert_refused(rig.call("set_heater_setpoints",
                                  {"setpoints": {"top": 170, "bottom": 170}, "reason": "r"}),
                         "held during the call", "1 command(s) had already been sent")
    assert [t for t, _p in rig.cmds()] == ["xsphere/commands/pid/top/setpoint"]
    assert len(out["published"]) == 1


def test_a_hold_without_an_arm_is_quiet_but_still_holds(grace_rig):
    rig = grace_rig
    not_ok(rig, ESP32_RULES)
    assert rig.svc.supervisor.heater_hold() is not None
    assert notes(rig) == []
    # Armed during the hold: writes stay held, and the grace still runs out.
    rig.arm("heaters")
    rig.advance(1)
    assert_refused(rig.call("set_heater_setpoints", SETPOINT), "heater writes are held")
    rig.advance(90)
    rig.svc.supervisor.tick()
    assert not rig.svc.arms.is_armed("heaters")


def test_hold_shows_in_the_status_and_the_overview(grace_rig):
    rig = grace_rig
    rig.arm("heaters")
    not_ok(rig, ESP32_RULES)
    rig.advance(1)
    st = rig.svc.status_payload()
    assert st["level_stale_grace_s"] == 90.0
    assert st["heater_hold"]["rules"] == ESP32_RULES
    assert st["may_now"]["heaters"].startswith("nothing while held by the level-stale grace")
    _e, text = rig.call("get_overview", {})
    assert "AI heater writes are HELD by the level-stale grace" in text
    _e, out = rig.call("get_overview", {"format": "json"})
    assert out["heater_hold"]["disarm_at_utc"] == st["heater_hold"]["disarm_at_utc"]
    ok(rig)
    assert rig.svc.status_payload()["heater_hold"] is None


# ──────────────────────────────────────────────────────────────────────────
# Timelines: the real interlocks.py watchdog feeding the supervisor
# ──────────────────────────────────────────────────────────────────────────

class EspTimeline:
    """slowcontrol/controllers/interlocks.py and the AI service on one fake
    clock, a second at a time.

    Every second the PLC driver sends the cryostat level and, every 2 s unless
    it is quiet, the GHS ESP32 sends the ballast and primary_xe levels; the
    rig's fake slowcontrol publishes a fresh snapshot and the supervisor ticks.
    interlocks.py's watchdog loop runs unchanged: its 15 s wait is made of these
    seconds, and its status reaches the service live, as on the Pi."""

    def __init__(self, rig, monkeypatch, quiet, end, phase=0):
        self.rig = rig
        self.quiet = quiet                  # [start, stop) s: no ESP32 level
        self.end = end
        self.phase = phase                  # s before the watchdog loop starts
        self.t = 0
        self.mono = 1000.0
        monkeypatch.setattr(interlocks_py, "time",
                            SimpleNamespace(monotonic=lambda: self.mono, time=rig.clock))
        self.ctl = interlocks_py.InterlocksController(None, self)
        self.ctl._stop_event = self
        self.not_ok_at = []                 # s of each not-ok status
        self.disarmed_at = None             # s the heaters arm dropped
        self.held = 0                       # s with a heater hold
        self.on_second = None

    # what interlocks.py calls on its MqttClient ----------------------------

    def subscribe(self, topic, callback):
        pass

    def publish(self, topic, payload, qos=0, retain=False):
        pass

    def publish_status(self, name, payload, retain=False):
        if not payload["ok"]:
            self.not_ok_at.append(self.t)
        self.rig.fake.pub(f"xsphere/status/{name}", payload)
        self._note_disarm()

    def _note_disarm(self):
        if self.disarmed_at is None and not self.rig.svc.arms.is_armed("heaters"):
            self.disarmed_at = self.t

    # interlocks.py's _stop_event --------------------------------------------

    def wait(self, timeout):
        for _ in range(int(timeout)):
            if self.t >= self.end:
                return True
            self.second()
        return self.t >= self.end

    def second(self):
        self.t += 1
        self.mono += 1.0
        self.rig.advance(1)
        self.ctl._on_level("xsphere/sensors/level/cryostat", {"raw": 3.0, "filtered": 3.0})
        start, stop = self.quiet
        if self.t % 2 == 0 and not start <= self.t < stop:
            for vessel in ("ballast", "primary_xe"):
                self.ctl._on_level(f"xsphere/sensors/level/{vessel}",
                                   {"raw": 4.0, "filtered": 4.0})
        self.rig.svc.supervisor.tick()
        if self.rig.svc.supervisor.heater_hold() is not None:
            self.held += 1
        self._note_disarm()
        if self.on_second is not None:
            self.on_second(self)

    def run(self):
        for _ in range(self.phase):
            self.second()
        self.ctl._watchdog_loop()
        return self


#: The ESP32's last level before the dropout (s into the timeline).
LAST = 100


def timeline(rig, monkeypatch, gap_s, *, run_s=None, phase=0, on_second=None):
    """Heaters and LN2 armed, then a GHS ESP32 dropout: its level messages
    (every 2 s) stop after LAST and the next comes ``gap_s`` later (even).
    Runs ``run_s`` s past LAST (default: the gap and 2 min more)."""
    rig.arm("heaters", "ln2")
    run_s = gap_s + 120 if run_s is None else run_s
    tl = EspTimeline(rig, monkeypatch, quiet=(LAST + 1, LAST + gap_s), end=LAST + run_s,
                     phase=phase)
    tl.on_second = on_second
    return tl.run()


@pytest.mark.parametrize("phase", [0, 7, 14])
def test_the_10_30_dropout_keeps_the_heaters_armed(rig, monkeypatch, phase):
    # 2026-10-30, 02:47:10 EDT: Setra #4 silent 68-119 s, level_stale at
    # 02:48:18, heaters disarmed 06:48:18Z.  Here the longest it can have been.
    tl = timeline(rig, monkeypatch, 120, phase=phase)
    assert tl.not_ok_at, "a 120 s gap turns the interlocks not-ok"
    assert tl.disarmed_at is None and rig.svc.arms.auto_disarms == 0
    assert 0 < tl.held <= 75
    assert rig.svc.supervisor.heater_hold() is None
    assert len(notes(rig)) == 2                 # held, then ok again


def test_the_10_30_dropout_disarmed_the_heaters_without_a_grace(no_grace_rig, monkeypatch):
    rig = no_grace_rig
    tl = timeline(rig, monkeypatch, 120)
    assert tl.disarmed_at == tl.not_ok_at[0]
    assert 60 < tl.disarmed_at - LAST <= 75
    assert rig.svc.arms.last_disarm("heaters")["reason"] == \
        "slowcontrol interlocks turned not-ok (level_stale/ballast, level_stale/primary_xe)"
    assert rig.svc.arms.armed_groups() == ["ln2"]


@pytest.mark.parametrize("phase", [0, 5, 10, 14])
def test_a_dead_esp32_still_disarms_the_heaters(rig, monkeypatch, phase):
    tl = timeline(rig, monkeypatch, 10 ** 6, run_s=300, phase=phase)
    assert 60 + 120 < tl.disarmed_at - LAST <= 75 + 120
    reason = rig.svc.arms.last_disarm("heaters")["reason"]
    # "interlocks" in the reason: the DAQ's thermo_keeper.py keys on it.
    assert reason == ("slowcontrol interlocks stayed not-ok (level_stale/ballast, "
                      "level_stale/primary_xe) past the 120 s level-stale grace")
    assert rig.svc.arms.armed_groups() == ["ln2"]
    assert rig.svc.arms.auto_disarms == 1


@pytest.mark.parametrize("phase", [0, 14])
def test_a_gap_under_60_s_never_reaches_the_heaters(rig, monkeypatch, phase):
    # 48 of the 49 Setra #4 gaps over 10 s on 10-30 00:01-02:59 EDT.
    tl = timeline(rig, monkeypatch, 58, phase=phase)
    assert tl.not_ok_at == [] and tl.held == 0 and tl.disarmed_at is None


def test_heater_writes_wait_out_the_dropout(rig, monkeypatch):
    results = {}

    def write_once(tl):
        held = tl.rig.svc.supervisor.heater_hold() is not None
        if held and "held" not in results:
            results["held"] = tl.rig.call("set_heater_setpoints", SETPOINT)
        elif not held and "held" in results and "after" not in results:
            results["after"] = tl.rig.call("set_heater_setpoints", SETPOINT)

    timeline(rig, monkeypatch, 120, on_second=write_once)
    assert_refused(results["held"], "heater writes are held")
    assert_ok(results["after"])
    assert rig.svc.arms.is_armed("heaters")
