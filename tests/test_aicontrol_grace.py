"""
Offline tests for the level-stale grace (aicontrol/arms.py, Supervisor): a
not-ok interlock made only of the GHS ESP32's level_stale rules holds the AI's
heater writes, and disarms the heaters only if it outlasts
XSPHERE_AICONTROL_LEVEL_STALE_GRACE_S.  At the default of 0 every not-ok
disarms at once, as before.

    python -m pytest tests/test_aicontrol_grace.py -q      (from the repo root)

Uses the rig of test_aicontrol_actions.py: no broker, a fake clock.
"""

import pytest

from aicontrol.app import Service
from aicontrol.arms import GRACE_RULES
from aicontrol.bus import LoopbackTransport
from aicontrol.config import MAX_LEVEL_STALE_GRACE_S, ConfigError, Settings

from test_aicontrol_actions import Clock, Rig, assert_ok, assert_refused

INTERLOCKS = "xsphere/status/interlocks"
ESP32_RULES = ["level_stale/ballast", "level_stale/primary_xe"]
SETPOINT = {"setpoints": {"bottom": 167}, "reason": "r"}


def not_ok(rig, rules):
    rig.fake.pub(INTERLOCKS, {"rules_active": rules, "ok": False})


def ok(rig):
    rig.fake.pub(INTERLOCKS, {"rules_active": [], "ok": True})


def notes(rig):
    return [e["detail"] for e in rig.svc.audit.recent(200) if e["kind"] == "note"]


@pytest.fixture
def rig(tmp_path):
    return Rig(tmp_path)


@pytest.fixture
def grace_rig(tmp_path):
    r = Rig(tmp_path)
    r.svc.supervisor.level_stale_grace_s = 90.0
    return r


# ──────────────────────────────────────────────────────────────────────────
# Settings
# ──────────────────────────────────────────────────────────────────────────

def test_grace_rules_are_the_esp32_levels_only():
    assert GRACE_RULES == {"level_stale/ballast", "level_stale/primary_xe"}


def test_settings_default_and_bounds(tmp_path):
    assert Settings(state_dir=tmp_path).level_stale_grace_s == 0.0
    assert Settings(state_dir=tmp_path, level_stale_grace_s=90).level_stale_grace_s == 90.0
    for bad in (-1, MAX_LEVEL_STALE_GRACE_S + 1, float("nan"), float("inf"), True, "abc", None):
        with pytest.raises(ConfigError):
            Settings(state_dir=tmp_path, level_stale_grace_s=bad)


def test_settings_from_env():
    assert Settings.from_env({}).level_stale_grace_s == 0.0
    env = {"XSPHERE_AICONTROL_LEVEL_STALE_GRACE_S": " 90 "}
    assert Settings.from_env(env).level_stale_grace_s == 90.0
    for bad in ("ninety", "-5", "601"):
        with pytest.raises(ConfigError, match="XSPHERE_AICONTROL_LEVEL_STALE_GRACE_S"):
            Settings.from_env({"XSPHERE_AICONTROL_LEVEL_STALE_GRACE_S": bad})


def test_service_takes_the_grace_from_its_settings(tmp_path):
    svc = Service(Settings(state_dir=tmp_path, level_stale_grace_s=45), LoopbackTransport(),
                  clock=Clock(), history=None)
    assert svc.supervisor.level_stale_grace_s == 45.0
    st = svc.status_payload()
    assert st["level_stale_grace_s"] == 45.0 and st["heater_hold"] is None


# ──────────────────────────────────────────────────────────────────────────
# Default: no grace
# ──────────────────────────────────────────────────────────────────────────

def test_without_a_grace_level_stale_disarms_heaters_at_once(rig):
    assert rig.svc.supervisor.level_stale_grace_s == 0.0
    rig.arm("heaters", "ln2")
    not_ok(rig, ESP32_RULES)
    assert rig.svc.arms.armed_groups() == ["ln2"]
    assert "level_stale/ballast, level_stale/primary_xe" in \
        rig.svc.arms.last_disarm("heaters")["reason"]
    assert rig.svc.supervisor.heater_hold() is None


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
