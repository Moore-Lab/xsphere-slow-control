"""
Offline tests for the heater-PID windup fix in the PLC driver.

    python -m pytest tests/test_pid_windup.py -q      (from the repo root)

The PLC is a fake Modbus client that emulates the parts of the CLICK that
matter here (docs/pid-windup.md):

  * the three PID loops as fitted to live data on 2026-10-25 — position form,
    e = 100·(SP − PV)/220 %, Bias += Kp·Ts/Ti·e frozen only while it would push
    the output further past a limit, CO = Bias + Kp·e + Kp·Td/Ts·Δe clamped to
    the CO limits, a 20 % PV filter, real sample times;
  * Manual → Auto with bumpless Mode 2 (Bias = CO), the PID's mode indication
    bits C+25 / C+26 at the real coil base (C1 = 16384), the error code DS+0;
  * the operator's mode rung: DF175/177/179 0 → Manual request, 0 → 1 → Auto
    request (edge), DF176/178/180 a copy of the last request.

Time is a fake clock patched into the driver module (monotonic, time, sleep),
so a toggle's waits run instantly and PID samples land when they would.
pymodbus is stubbed when it is not installed: the driver only needs its client
class and its exception type.
"""

import importlib
import json
import struct
import sys
import types

import paho.mqtt.client as paho
import pytest

try:
    importlib.import_module("pymodbus.client")
    importlib.import_module("pymodbus.exceptions")
except ImportError:             # the DAQ PC has no pymodbus
    _pm = types.ModuleType("pymodbus")
    _pm_client = types.ModuleType("pymodbus.client")
    _pm_exc = types.ModuleType("pymodbus.exceptions")

    class _StubClient:          # replaced by FakeClick in every test
        def __init__(self, *a, **k): ...
        def connect(self): return False
        def close(self): ...

    class _StubException(Exception): ...

    _pm_client.ModbusTcpClient = _StubClient
    _pm_exc.ModbusException = _StubException
    _pm.client, _pm.exceptions = _pm_client, _pm_exc
    sys.modules.update({"pymodbus": _pm, "pymodbus.client": _pm_client,
                        "pymodbus.exceptions": _pm_exc})

from slowcontrol.core.config import ServiceConfig     # noqa: E402
from slowcontrol.drivers import plc as plc_mod        # noqa: E402

C0 = 273.15
SPAN = 220.0
DF_BASE = 28672
C_COIL_BASE = 16384                     # C1, independent of the driver's constant
# zone → (DF block, DS block, C block, mode command DF, LabJack RTD channel)
ZONES = {"bottom": (100, 100, 100, 175, 2),
         "top":    (125, 115, 140, 177, 3),
         "nozzle": (150, 130, 180, 179, 1)}
# Live gains and sample times (Kp, Ti s, Td s, Ts s)
GAINS = {"bottom": (41.0, 158.0, 26.0, 3.694),
         "top":    (35.0, 311.0, 51.0, 7.259),
         "nozzle": (14.0, 565.0, 94.0, 13.202)}
ALERT = "xsphere/alerts/pid_stuck_off/{}"


class ProcessDied(BaseException):
    """The service process dying mid-poll (not an Exception: poll() must not catch it)."""


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class FakeClock:
    """Stands in for the `time` module inside slowcontrol.drivers.plc."""

    def __init__(self):
        self.t = 1000.0
        self.plc = None
        self.on_sleep = None            # optional hook, called before each sleep

    def monotonic(self):
        return self.t

    def time(self):
        return 1.79e9 + self.t

    def sleep(self, dt):
        if self.on_sleep is not None:
            self.on_sleep()
        self.advance(dt)

    def advance(self, dt):
        self.t += dt
        if self.plc is not None:
            self.plc.scan()


class FakeMqtt:
    """Keeps the last payload per topic and delivers every publish to matching
    subscribers, as the broker would."""

    def __init__(self):
        self.last = {}
        self.history = []
        self.subs = []

    def publish(self, topic, payload, qos=1, retain=False):
        payload = json.loads(json.dumps(payload))
        self.last[topic] = payload
        self.history.append((topic, payload))
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


class _Loop:
    def __init__(self, zone):
        self.kp, self.ti, self.td, self.ts = GAINS[zone]
        self.mode = "auto"
        self.eprev = 0.0
        self.next = 0.0
        self.last_cmd = 1.0
        self.refuse_auto = 0            # ignore this many Auto requests
        self.error = 0                  # PID error code (forces Manual)


class FakeClick:
    """DF floats, DS ints and coils over fake Modbus, plus the PID loops and
    the mode rung.  `writes` logs every float write as (DF number, value)."""

    def __init__(self, clock):
        self.clock = clock
        clock.plc = self
        self.df = {}
        self.ds = {}
        self.coils = {}
        self.writes = []
        self.coil_writes = []
        self.refuse_bias_writes = False
        self.crash_on = None            # (DF number, value): die on that write
        self.loops = {z: _Loop(z) for z in ZONES}
        for z, (dfb, dsb, _cb, cmd, _ch) in ZONES.items():
            for off in range(25):
                self.df[dfb + off] = 0.0
            self.df[dfb + 9], self.df[dfb + 10] = 0.0, 100.0      # CO limits
            self.ds[dsb + 5] = int(round(GAINS[z][3] * 1000))      # sample ms
            self.df[dfb + 5], self.df[dfb + 6], self.df[dfb + 7] = GAINS[z][:3]
            self.df[cmd] = self.df[cmd + 1] = 1.0
            self.loops[z].next = clock.t + GAINS[z][3] / 2
        for z in ZONES:                 # every loop regulating at 165 K
            self.set_zone(z, 165.0, 165.0, 30.0)

    # -- helpers for tests ---------------------------------------------------
    def set_zone(self, zone, sp_k, pv_k, bias, mode="auto"):
        dfb = ZONES[zone][0]
        lp = self.loops[zone]
        self.df[dfb + 0] = sp_k - C0
        self.df[dfb + 11] = self.df[dfb + 12] = pv_k - C0
        self.df[dfb + 4] = bias
        lp.mode = mode
        lp.eprev = 100.0 * (sp_k - pv_k) / SPAN
        if mode == "auto":
            self.df[dfb + 8] = min(100.0, max(0.0, bias + lp.kp * lp.eprev))
        self.scan()

    def get(self, zone, off):
        return self.df.get(ZONES[zone][0] + off, 0.0)

    def bias(self, zone):
        return self.get(zone, 4)

    def co(self, zone):
        return self.get(zone, 8)

    def mode(self, zone):
        return self.loops[zone].mode

    def df_writes(self, n):
        return [v for (m, v) in self.writes if m == n]

    # -- the PLC scan ----------------------------------------------------------
    def scan(self):
        for z, (dfb, dsb, cb, cmd, _ch) in ZONES.items():
            lp = self.loops[z]
            val = self.df.get(cmd, 0.0)
            if val != lp.last_cmd:                  # the mode rung sees an edge
                lp.last_cmd = val
                if abs(val) < 0.5:
                    self.df[cmd + 1] = 0.0
                    lp.mode = "manual"
                elif abs(val - 1.0) < 0.5:
                    self.df[cmd + 1] = 1.0
                    if lp.mode == "manual":
                        if lp.refuse_auto > 0 or lp.error:
                            lp.refuse_auto = max(0, lp.refuse_auto - 1)
                        else:
                            lp.mode = "auto"
                            self.df[dfb + 4] = self.df.get(dfb + 8, 0.0)   # Mode 2
            if lp.error:
                lp.mode = "manual"
            self.ds[dsb + 0] = lp.error
            self.coils[C_COIL_BASE + cb + 25 - 1] = lp.mode == "manual"
            self.coils[C_COIL_BASE + cb + 26 - 1] = lp.mode == "auto"
            self.coils[C_COIL_BASE + cb + 27 - 1] = False
            self.coils[C_COIL_BASE + cb + 5 - 1] = True              # Freeze Bias
            self.coils[C_COIL_BASE + cb + 8 - 1] = True              # bumpless Mode 2
            while self.clock.t >= lp.next:
                lp.next += lp.ts
                self._sample(z)

    def _sample(self, z):
        dfb = ZONES[z][0]
        lp = self.loops[z]
        d = self.df
        d[dfb + 12] = d[dfb + 12] + 0.2 * (d[dfb + 11] - d[dfb + 12])   # PV filter
        e = 100.0 * (d[dfb + 0] - d[dfb + 12]) / SPAN
        if lp.mode == "auto":
            lo, hi = d[dfb + 9], d[dfb + 10]
            b = d[dfb + 4]
            db = lp.kp * lp.ts / lp.ti * e
            p = lp.kp * e
            dd = lp.kp * lp.td / lp.ts * (e - lp.eprev)
            cot = p + b + db + dd
            if (cot > hi and db > 0) or (cot < lo and db < 0):     # Freeze Bias
                db = 0.0
            d[dfb + 4] = b + db
            d[dfb + 8] = min(hi, max(lo, p + d[dfb + 4] + dd))
        lp.eprev = e

    # -- Modbus client surface -------------------------------------------------
    def connect(self):
        return True

    def close(self):
        pass

    def _word(self, a):
        if a >= DF_BASE:
            n, w = (a - DF_BASE) // 2 + 1, (a - DF_BASE) % 2
            raw = struct.unpack(">I", struct.pack(">f", float(self.df.get(n, 0.0))))[0]
            return (raw & 0xFFFF) if w == 0 else (raw >> 16) & 0xFFFF
        return int(self.ds.get(a + 1, 0))

    def read_holding_registers(self, address, count=1):
        self.scan()
        return _Result(registers=[self._word(address + i) for i in range(count)])

    def write_registers(self, address, values):
        assert address >= DF_BASE and (address - DF_BASE) % 2 == 0 and len(values) == 2
        n = (address - DF_BASE) // 2 + 1
        v = struct.unpack(">f", struct.pack(">I", (values[1] << 16) | values[0]))[0]
        if self.crash_on is not None and n == self.crash_on[0] and v == self.crash_on[1]:
            self.crash_on = None
            raise ProcessDied()
        self.writes.append((n, v))
        if self.refuse_bias_writes and n in [b + 4 for b, *_ in ZONES.values()]:
            return _Result()
        self.df[n] = v
        return _Result()

    def write_register(self, address, value):
        self.ds[address + 1] = int(value)
        return _Result()

    def read_coils(self, address, count=1):
        self.scan()
        return _Result(bits=[bool(self.coils.get(address + i, False)) for i in range(count)])

    def write_coil(self, address, value):
        self.coil_writes.append((address, bool(value)))
        self.coils[address] = bool(value)
        return _Result()


class Rig:
    """One fake PLC, one broker, a fake clock and as many driver lifetimes as
    a test wants.  `rtd_k` is what the LabJack reports, per channel."""

    def __init__(self, monkeypatch, tmp_path, **plc_cfg):
        self.clock = FakeClock()
        self.plc = FakeClick(self.clock)
        monkeypatch.setattr(plc_mod, "ModbusTcpClient", lambda *a, **k: self.plc)
        monkeypatch.setattr(plc_mod, "time", self.clock)
        self.mqtt = FakeMqtt()
        self.config = ServiceConfig()
        for k, v in plc_cfg.items():
            setattr(self.config.plc, k, v)
        self.tmp_path = tmp_path
        self.rescue_path = tmp_path / "pid_rescue.json"
        self.rtd_k = {1: 165.0, 2: 165.0, 3: 165.0}
        self.lj_alive = True
        self.plant = {}                 # zone → K/min per % of output above 30 %
        self.driver = None
        self.start()

    def start(self):
        """A (re)start of the service: a new driver on a new broker session."""
        self.mqtt.subs = [s for s in self.mqtt.subs
                          if getattr(s[1], "__self__", None) is not self.driver]
        self.driver = plc_mod.PlcDriver(self.config, self.mqtt,
                                        pid_rescue_path=str(self.rescue_path))
        self.driver._interlock_path = str(self.tmp_path / "pv_interlock.json")
        self.driver.connect()
        return self.driver

    def cmd(self, zone, what, payload):
        self.mqtt.publish(f"xsphere/commands/pid/{zone}/{what}", payload)

    def expressions(self):
        """The operator's setup: SP 165 K everywhere, each zone on its cube RTD."""
        for zone, (*_x, ch) in ZONES.items():
            self.cmd(zone, "setpoint_expr", {"expr": "165"})
            self.cmd(zone, "pv_expr", {"expr": f"rtd{ch}"})

    def tick(self, seconds=1):
        """`seconds` polls, one per (fake) second, with the LabJack publishing
        before each one while it is alive."""
        for _ in range(int(seconds)):
            for zone, rate in self.plant.items():
                ch = ZONES[zone][4]
                self.rtd_k[ch] += rate * (self.plc.co(zone) - 30.0) / 30.0 / 60.0
            if self.lj_alive:
                for ch, k in self.rtd_k.items():
                    self.mqtt.publish(f"xsphere/sensors/temperature/labjack/rtd/{ch}",
                                      {"value_c": k - C0, "value_k": k})
            t0 = self.clock.t
            self.driver.poll()
            self.clock.advance(max(0.0, 1.0 - (self.clock.t - t0)))

    def status(self, zone):
        return self.mqtt.last[f"xsphere/status/pid/{zone}"]

    def alert(self, zone):
        return self.mqtt.last.get(ALERT.format(zone))


def stuck_bottom(rig, bias=-150.0, pv_k=160.0, mode="auto"):
    """The bottom zone as on 2026-10-25 02:20: Auto, SP 165 K, PV below it,
    output 0 %, integrator far below the floor."""
    rig.expressions()
    rig.rtd_k[2] = pv_k
    rig.tick(2)                         # expressions written, interlock clear
    rig.plc.set_zone("bottom", 165.0, pv_k, bias, mode=mode)


@pytest.fixture
def rig(monkeypatch, tmp_path):
    return Rig(monkeypatch, tmp_path)


def cmd_writes(rig, zone="bottom"):
    return rig.plc.df_writes(ZONES[zone][3])


# ---------------------------------------------------------------------------
# Addressing and status
# ---------------------------------------------------------------------------

def test_c_relays_are_read_at_the_real_coil_base():
    assert plc_mod._c(1) == 16384
    assert plc_mod._c(126) == 16384 + 125          # HTR1 Auto indication


def test_status_carries_bias_limits_and_the_pids_own_mode(rig):
    rig.expressions()
    rig.tick(2)
    rig.plc.set_zone("bottom", 165.0, 165.0, 30.0)
    rig.tick(1)
    s = rig.status("bottom")
    assert s["mode"] == "auto" and s["mode_source"] == "indication"
    assert s["bias_pct"] == pytest.approx(rig.plc.bias("bottom"), abs=1e-3)
    assert (s["co_lo_pct"], s["co_hi_pct"]) == (0.0, 100.0)
    assert s["error_code"] == 0 and s["sample_s"] == pytest.approx(3.694)
    assert (s["stuck_off"], s["stuck_off_s"], s["rescue_count"], s["rescue_pending"]) == (0, 0.0, 0, 0)
    assert s["rescue_remedy"] == "toggle" and s["guard"] == "off"


def test_mode_comes_from_the_indication_not_the_ladder_mirror(rig):
    """A PID error drops the loop to Manual by itself; DF180 still says Auto."""
    rig.expressions()
    rig.tick(2)
    rig.plc.loops["nozzle"].error = 5
    rig.tick(1)
    s = rig.status("nozzle")
    assert s["mode_status_raw"] == 1.0
    assert (s["mode"], s["mode_source"], s["error_code"]) == ("manual", "indication", 5)


def test_autotune_and_controller_type_are_refused(rig):
    rig.cmd("bottom", "autotune", {})
    rig.cmd("top", "controller_type", {"mode": "pid"})
    assert rig.plc.coil_writes == []


# ---------------------------------------------------------------------------
# The stuck-off remedy
# ---------------------------------------------------------------------------

def test_a_stuck_zone_is_toggled_and_heats_again(rig):
    stuck_bottom(rig)
    assert rig.plc.co("bottom") == 0.0
    rig.tick(10)
    assert rig.alert("bottom") in (None, "") and cmd_writes(rig) == []
    assert rig.status("bottom")["stuck_off_s"] > 5
    rig.tick(15)                        # > max(20 s, 2 × 3.7 s + 5 s)
    alert = [p for (t, p) in rig.mqtt.history if t == ALERT.format("bottom") and p]
    assert alert and alert[0]["rule"] == "pid_stuck_off" and alert[0]["bias_pct"] < -100.0
    assert cmd_writes(rig) == [0.0, 1.0]            # Manual, then Auto
    assert rig.plc.mode("bottom") == "auto"
    assert not rig.rescue_path.exists()
    rig.tick(5)
    assert rig.plc.bias("bottom") >= 0.0            # bumpless: Bias = CO = 0, then integrates up
    assert rig.plc.co("bottom") > 50.0              # 5 K under SP → heating
    s = rig.status("bottom")
    assert s["rescue_count"] == 1 and s["rescue_last"]["result"] == "ok"
    assert s["stuck_off"] == 0 and rig.alert("bottom") == ""
    assert s["mode"] == "auto" and s["rescue_pending"] == 0
    # the other zones were left alone
    assert cmd_writes(rig, "top") == [] and cmd_writes(rig, "nozzle") == []


def test_alert_only_mode_never_touches_the_mode(monkeypatch, tmp_path):
    rig = Rig(monkeypatch, tmp_path, pid_stuck_off_remedy="alert")
    stuck_bottom(rig)
    rig.tick(40)
    assert rig.alert("bottom")["remedy"] == "alert"
    assert rig.status("bottom")["stuck_off"] == 1
    assert cmd_writes(rig) == [] and rig.plc.mode("bottom") == "auto"


def test_remedy_off_does_not_even_alert(monkeypatch, tmp_path):
    rig = Rig(monkeypatch, tmp_path, pid_stuck_off_remedy="off")
    stuck_bottom(rig)
    rig.tick(40)
    assert rig.alert("bottom") in (None, "") and cmd_writes(rig) == []


def test_no_toggle_without_bumpless_mode_2(rig, monkeypatch):
    """The toggle is only a reset of the Bias because Manual → Auto sets
    Bias = CO (C108 on); with that option off the service only alerts."""
    real_scan = rig.plc.scan

    def scan_with_mode_1():
        real_scan()
        rig.plc.coils[C_COIL_BASE + 100 + 8 - 1] = False
    monkeypatch.setattr(rig.plc, "scan", scan_with_mode_1)
    stuck_bottom(rig)
    rig.tick(40)
    assert rig.alert("bottom")["rule"] == "pid_stuck_off"
    assert cmd_writes(rig) == []


def test_no_rescue_in_manual(rig):
    stuck_bottom(rig, mode="manual")
    rig.tick(60)
    assert cmd_writes(rig) == [] and rig.alert("bottom") in (None, "")
    assert rig.plc.mode("bottom") == "manual"


def test_no_rescue_while_the_pv_interlock_is_tripped(rig):
    stuck_bottom(rig)
    rig.lj_alive = False                # LabJack feed stops → trips after 10 s
    rig.tick(60)
    assert rig.mqtt.last["xsphere/status/pv_interlock"]["tripped"] is True
    assert cmd_writes(rig) == [] and rig.alert("bottom") in (None, "")


def test_no_rescue_when_pv_is_within_delta_of_sp(rig):
    stuck_bottom(rig, pv_k=164.5)       # 0.5 K under SP; δ = 1 K
    rig.tick(60)
    assert rig.plc.co("bottom") == 0.0                  # still held off by the windup …
    assert cmd_writes(rig) == [] and rig.alert("bottom") in (None, "")   # … but not ours to judge


def test_no_rescue_for_a_deliberate_setpoint_below_pv(rig):
    stuck_bottom(rig, pv_k=170.0)       # PV above SP: off is what was asked for
    rig.tick(60)
    assert cmd_writes(rig) == [] and rig.alert("bottom") in (None, "")


def test_no_rescue_while_the_output_is_on(rig):
    stuck_bottom(rig, bias=-10.0)       # small windup: P term still wins
    rig.tick(60)
    assert rig.plc.co("bottom") > 0.5
    assert cmd_writes(rig) == []


@pytest.mark.parametrize("change", [
    {"mode_ind": "manual"}, {"mode_ind": "autotune"}, {"mode_ind": None},
    {"error_code": 3}, {"error_code": None},
    {"pv_c": 165.0 - 0.9 - C0}, {"output": 0.6},
    {"bias": 0.0}, {"bias": 5.0},       # Bias inside the limits: D-term braking, not windup
    {"co_hi": 0.0},                     # output range allows no heat
])
def test_the_stuck_condition_needs_every_part(rig, change):
    base = {"sp_c": 165.0 - C0, "pv_c": 160.0 - C0, "output": 0.0, "bias": -150.0,
            "co_lo": 0.0, "co_hi": 100.0, "mode_ind": "auto", "error_code": 0,
            "sample_s": 3.694}
    t = rig.clock.t
    r = dict(base)
    rig.driver._update_stuck_off("bottom", r, False, t)
    rig.driver._update_stuck_off("bottom", r, False, t + 30)
    assert r["stuck"] is True                       # the full condition does confirm
    rig.driver._update_stuck_off("bottom", dict(base), True, t + 31)     # tripped clears it
    r = dict(base, **change)
    rig.driver._update_stuck_off("bottom", r, False, t + 32)
    rig.driver._update_stuck_off("bottom", r, False, t + 100)
    assert r["stuck"] is False and r["stuck_s"] == 0.0


def test_no_second_toggle_within_the_holdoff(monkeypatch, tmp_path):
    rig = Rig(monkeypatch, tmp_path, pid_rescue_holdoff_s=60.0)
    stuck_bottom(rig)
    rig.tick(30)                                        # toggled at ~22 s
    assert cmd_writes(rig) == [0.0, 1.0]
    # wound up again (another big error step), deep enough to outlast the hold-off …
    rig.plc.set_zone("bottom", 165.0, 160.0, -300.0)
    rig.tick(40)
    assert rig.alert("bottom")["bias_pct"] < -100.0     # … alerted again …
    assert cmd_writes(rig) == [0.0, 1.0]                # … but not toggled again
    rig.tick(60)                                        # hold-off over
    assert cmd_writes(rig) == [0.0, 1.0, 0.0, 1.0]
    assert rig.status("bottom")["rescue_count"] == 2


# ---------------------------------------------------------------------------
# Mode verification, retries, crashes, operators
# ---------------------------------------------------------------------------

def test_auto_is_verified_and_re_requested_until_it_takes(rig):
    stuck_bottom(rig)
    rig.plc.loops["bottom"].refuse_auto = 2         # the PLC ignores two Auto requests
    rig.tick(25)
    assert cmd_writes(rig) == [0.0, 1.0] and rig.plc.mode("bottom") == "manual"
    rig.tick(1)                                     # status is published before the toggle
    s = rig.status("bottom")
    assert s["rescue_pending"] == 1 and s["rescue_last"]["result"] == "not_auto"
    assert s["mode"] == "manual"
    saved = json.loads(rig.rescue_path.read_text())
    assert "bottom" in saved["pending"]
    rig.tick(12)                                    # one retry: refused again
    assert cmd_writes(rig) == [0.0, 1.0, 0.0, 1.0] and rig.plc.mode("bottom") == "manual"
    assert json.loads(rig.rescue_path.read_text())["pending"]["bottom"]["attempts"] == 2
    rig.tick(12)                                    # next retry takes
    assert cmd_writes(rig) == [0.0, 1.0] * 3 and rig.plc.mode("bottom") == "auto"
    assert not rig.rescue_path.exists()
    rig.tick(30)
    assert cmd_writes(rig) == [0.0, 1.0] * 3        # and then it stops
    assert rig.status("bottom")["rescue_pending"] == 0


def test_a_crash_between_the_two_writes_ends_in_auto_after_restart(rig):
    stuck_bottom(rig)
    rig.plc.crash_on = (175, 1.0)                   # the process dies instead of writing Auto
    with pytest.raises(ProcessDied):
        rig.tick(30)
    assert rig.plc.mode("bottom") == "manual"       # left in Manual …
    assert "bottom" in json.loads(rig.rescue_path.read_text())["pending"]   # … but on record
    rig.clock.advance(60)                           # systemd's RestartSec
    rig.start()
    rig.tick(2)
    assert rig.plc.mode("bottom") == "auto"
    assert cmd_writes(rig)[-1] == 1.0
    assert not rig.rescue_path.exists()


def test_a_graceful_stop_mid_rescue_requests_auto(rig):
    stuck_bottom(rig)
    rig.plc.loops["bottom"].refuse_auto = 1
    rig.tick(26)
    assert rig.plc.mode("bottom") == "manual" and rig.rescue_path.exists()
    rig.driver.disconnect()
    assert cmd_writes(rig)[-1] == 1.0               # one last Auto request on the way out
    assert rig.rescue_path.exists()                 # still pending until seen in Auto


def test_an_operator_mode_command_mid_toggle_wins(rig):
    stuck_bottom(rig)
    fired = []

    def operator_presses_manual():
        if not fired:
            fired.append(True)
            rig.cmd("bottom", "mode", {"mode": "manual"})
    rig.clock.on_sleep = operator_presses_manual
    rig.tick(25)
    rig.clock.on_sleep = None
    assert fired
    assert rig.plc.mode("bottom") == "manual"
    assert cmd_writes(rig)[-1] == 0.0               # the operator's value stands
    assert not rig.rescue_path.exists()
    n = len(cmd_writes(rig))
    rig.tick(60)
    assert len(cmd_writes(rig)) == n                # nothing more from the service
    assert rig.status("bottom")["rescue_last"]["result"] == "operator"


def test_an_operator_command_ends_the_re_requests(rig):
    stuck_bottom(rig)
    rig.plc.loops["bottom"].refuse_auto = 99
    rig.tick(26)
    assert rig.rescue_path.exists()
    rig.cmd("bottom", "mode", {"mode": "manual"})
    assert not rig.rescue_path.exists()
    n = len(cmd_writes(rig))
    rig.tick(60)
    assert len(cmd_writes(rig)) == n


# ---------------------------------------------------------------------------
# The PV interlock no longer writes the PIDs' PV
# ---------------------------------------------------------------------------

def trip(rig, seconds=30):
    rig.lj_alive = False
    rig.tick(seconds)
    assert rig.mqtt.last["xsphere/status/pv_interlock"]["tripped"] is True


def test_a_trip_holds_sp_and_output_but_leaves_the_pv_alone(rig):
    rig.expressions()
    rig.tick(3)
    trip(rig, 11)                       # stale after 10 s
    pv_before = rig.plc.get("bottom", 11)
    n = len(rig.plc.writes)
    rig.tick(20)
    during = rig.plc.writes[n:]
    for zone, (dfb, *_x) in ZONES.items():
        assert dfb + 11 not in [m for m, _v in during]                  # PV untouched
        assert rig.plc.get(zone, 0) == pytest.approx(3.0 - C0)          # SP 3 K
        assert rig.plc.get(zone, 8) == 0.0                              # heater off
        assert (dfb + 8, 0.0) in during                                 # OUT ← 0 written
    assert rig.plc.get("bottom", 11) == pv_before
    assert rig.plc.df.get(211) == pytest.approx(500.0 - C0)             # mirror surrogate


def test_the_old_pv_leg_can_be_brought_back(monkeypatch, tmp_path):
    rig = Rig(monkeypatch, tmp_path, pv_interlock_writes_pv=True)
    rig.expressions()
    rig.tick(3)
    trip(rig)
    assert rig.plc.get("bottom", 11) == pytest.approx(500.0 - C0)


def test_setpoints_sent_during_a_trip_wait_for_it_to_clear(rig):
    rig.expressions()
    rig.tick(3)
    trip(rig)
    rig.cmd("bottom", "setpoint", {"value_k": 170.0})     # gradient controller / page
    rig.tick(2)
    assert rig.plc.get("bottom", 0) == pytest.approx(3.0 - C0)
    assert (100, pytest.approx(170.0 - C0)) not in rig.plc.writes
    rig.lj_alive = True
    rig.tick(2)
    assert rig.plc.get("bottom", 0) == pytest.approx(170.0 - C0)


def test_a_pv_write_command_during_a_trip_is_dropped(rig):
    rig.expressions()
    rig.tick(3)
    trip(rig)
    before = rig.plc.get("bottom", 11)
    rig.cmd("bottom", "pv", {"value_k": 120.0})
    assert rig.plc.get("bottom", 11) == before


def test_an_absurd_rtd_reading_never_reaches_the_pid(rig):
    rig.expressions()
    rig.tick(3)
    rig.rtd_k[2] = -100000.0            # broken RTD
    rig.tick(5)
    assert rig.mqtt.last["xsphere/status/pv_interlock"]["tripped"] is True
    assert min(v for m, v in rig.plc.writes if m == 111) > -200.0


def _trip_and_release(rig, seconds):
    """Bottom zone regulating-ish (Bias 3 %, PV 170 K above SP 165 K, output
    0 %), a LabJack gap of `seconds`, then the feed comes back."""
    rig.expressions()
    rig.rtd_k[2] = 170.0
    rig.tick(3)
    rig.plc.set_zone("bottom", 165.0, 170.0, 3.0)
    rig.tick(2)
    trip(rig, seconds)
    rig.lj_alive = True
    rig.tick(60)


@pytest.mark.parametrize("seconds", [15, 120])
def test_a_trip_no_longer_winds_the_integrator(rig, seconds):
    _trip_and_release(rig, seconds)
    assert rig.plc.bias("bottom") > -5.0


def test_the_old_pv_leg_is_what_wound_it(monkeypatch, tmp_path):
    """The mechanism, reproduced in the emulator: releasing the 500 K
    surrogate drives the Bias far below 0 % (tonight's −183 % on the bottom)."""
    rig = Rig(monkeypatch, tmp_path, pv_interlock_writes_pv=True,
              pid_stuck_off_remedy="off")
    _trip_and_release(rig, 120)
    assert rig.plc.bias("bottom") < -100.0
    assert rig.plc.co("bottom") == 0.0


def test_tonight_replayed_a_restart_then_setpoints_re_sent(rig):
    """02:16 restart → trip → clear with no expressions (SP held at 3 K, the
    heater off, PV sinking 1 K/min) → setpoints and PV sources re-sent 90 s
    later with the PV already under SP: the heater comes back within two
    samples, with the integrator where it was."""
    rig.expressions()
    rig.rtd_k[2] = 165.5
    rig.tick(3)
    rig.plc.set_zone("bottom", 165.0, 165.5, 30.0)
    rig.plant = {"bottom": 1.0}
    rig.tick(30)
    rig.lj_alive = False                            # MQTT down for the backup
    rig.tick(20)
    rig.start()                                     # slowcontrol restarts: expressions gone
    rig.lj_alive = True
    rig.tick(90)
    assert rig.plc.get("bottom", 0) == pytest.approx(3.0 - C0)
    assert rig.rtd_k[2] < 164.0                     # cooling with the heater off
    rig.expressions()
    rig.tick(int(2 * GAINS["bottom"][3]) + 2)
    assert rig.plc.co("bottom") > 0.5
    assert rig.plc.bias("bottom") > 0.0
    assert cmd_writes(rig) == []                    # no rescue was needed


# ---------------------------------------------------------------------------
# The integrator guard (off by default)
# ---------------------------------------------------------------------------

def test_the_guard_clamps_the_bias_within_a_poll(monkeypatch, tmp_path):
    rig = Rig(monkeypatch, tmp_path, pid_integrator_guard=True)
    stuck_bottom(rig)
    rig.tick(2)
    assert rig.plc.bias("bottom") >= 0.0
    rig.tick(5)
    assert rig.plc.co("bottom") > 50.0
    assert cmd_writes(rig) == []                    # no toggle needed
    assert rig.status("bottom")["guard"] == "on" and rig.status("bottom")["guard_writes"] >= 1


def test_a_refused_guard_turns_itself_off_and_the_toggle_covers_it(monkeypatch, tmp_path):
    rig = Rig(monkeypatch, tmp_path, pid_integrator_guard=True)
    rig.plc.refuse_bias_writes = True
    stuck_bottom(rig)
    rig.tick(15)
    assert rig.status("bottom")["guard"] == "refused"
    n = len(rig.plc.df_writes(104))
    rig.tick(15)
    assert len(rig.plc.df_writes(104)) == n         # stopped writing
    assert cmd_writes(rig) == [0.0, 1.0] and rig.plc.mode("bottom") == "auto"
    rig.tick(5)
    assert rig.plc.co("bottom") > 50.0


def test_the_guard_leaves_manual_loops_alone(monkeypatch, tmp_path):
    rig = Rig(monkeypatch, tmp_path, pid_integrator_guard=True)
    stuck_bottom(rig, mode="manual")
    rig.tick(10)
    assert rig.plc.df_writes(104) == []


# ---------------------------------------------------------------------------
# Where Lucas sees it: the registry and Telegraf
# ---------------------------------------------------------------------------

def test_the_registry_and_telegraf_carry_the_bias_and_the_stuck_flag(rig):
    import os
    from slowcontrol.state import default_schema_path, load_state_schema
    from slowcontrol.state.store import StateStore
    store = StateStore(rig.config, rig.mqtt, load_state_schema(default_schema_path()))
    for topic in sorted(store.schema.subscribe_topics()):
        rig.mqtt.subscribe(topic, store._on_message)
    stuck_bottom(rig)
    rig.tick(21)                        # confirmed stuck, just before the toggle's poll
    states = store.snapshot()["states"]
    assert states["pid_bottom_bias"]["value"] < -100.0
    assert states["pid_bottom_stuck_off"]["value"] is True
    assert states["pid_top_stuck_off"]["value"] is False
    repo = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
    with open(os.path.join(repo, "telegraf", "telegraf.conf"), encoding="utf-8") as fh:
        conf = fh.read()
    pid_block = conf.split('measurement_name = "pid_state"')[1].split("[[inputs.mqtt_consumer]]")[0]
    for field in ("bias_pct", "stuck_off", "rescue_count"):
        assert f'"{field}"' in pid_block


# ---------------------------------------------------------------------------
# Review fixes (2026-10-25): races and values the interlock let through
# ---------------------------------------------------------------------------

def test_an_absurd_reading_arriving_mid_poll_never_reaches_the_pid(rig, monkeypatch):
    """The LabJack callback runs on the MQTT thread.  A broken RTD's first
    reading can land after poll() has decided the interlock (clear) and
    before the expressions are written.  The trip no longer overwrites the
    PV, so a value that got in would sit in DF111 for the whole trip — and an
    absurdly low PV under SP = 3 K is a full-on heater."""
    rig.expressions()
    rig.tick(3)
    real = rig.driver._write_level_to_plc

    def labjack_publishes_mid_poll():
        rig.rtd_k[2] = -100000.0
        rig.mqtt.publish("xsphere/sensors/temperature/labjack/rtd/2",
                         {"value_c": -100000.0 - C0, "value_k": -100000.0})
        real()
    monkeypatch.setattr(rig.driver, "_write_level_to_plc", labjack_publishes_mid_poll)
    rig.tick(1)
    monkeypatch.setattr(rig.driver, "_write_level_to_plc", real)
    co_during_trip = []
    for _ in range(40):
        rig.tick(1)
        if rig.mqtt.last["xsphere/status/pv_interlock"]["tripped"]:
            co_during_trip.append(rig.plc.co("bottom"))
    assert co_during_trip and max(co_during_trip) == 0.0
    assert min(v for m, v in rig.plc.writes if m == 111) > -200.0
    assert rig.plc.get("bottom", 11) > -200.0 and rig.plc.get("bottom", 12) > -200.0


def test_a_nan_rtd_reading_trips_the_interlock(rig):
    """NaN compares False against both band edges; it must trip, not pass."""
    rig.expressions()
    rig.tick(3)
    rig.rtd_k[3] = float("nan")
    rig.tick(2)
    il = rig.mqtt.last["xsphere/status/pv_interlock"]
    assert il["tripped"] is True and "3" in il["reasons"]
    assert all(v == v for m, v in rig.plc.writes if m == 136)          # no NaN PV written
    assert rig.plc.get("top", 0) == pytest.approx(3.0 - C0)


def test_a_pv_below_the_band_from_a_plc_rtd_is_not_written(rig):
    """A PV source the interlock does not watch (a PLC RTD) reading an open
    circuit: the PID keeps its last good PV instead of heating flat out."""
    rig.expressions()
    rig.tick(3)
    rig.cmd("bottom", "pv_expr", {"expr": "rtd5"})
    rig.plc.df[2] = 165.0 - C0                   # DF2 = PLC RTD 5
    rig.tick(2)
    assert rig.plc.get("bottom", 11) == pytest.approx(165.0 - C0, abs=1e-3)
    rig.plc.df[2] = -250.0                       # 23 K: open RTD
    n = len(rig.plc.writes)
    rig.tick(5)
    assert (111, pytest.approx(-250.0)) not in rig.plc.writes[n:]
    assert rig.plc.get("bottom", 11) == pytest.approx(165.0 - C0, abs=1e-3)
    rig.plc.df[2] = 166.0 - C0                   # repaired: written again
    rig.tick(2)
    assert rig.plc.get("bottom", 11) == pytest.approx(166.0 - C0, abs=1e-3)


def test_an_operator_command_racing_the_decision_is_not_overridden(rig, monkeypatch):
    """Lucas presses Manual just as the service decides to toggle: his
    command is counted after the re-read that saw Auto, but before the
    toggle's first write.  The service must not then put the zone in Auto."""
    stuck_bottom(rig)
    real = rig.driver._read_pid_zone
    fired = []

    def read_then_operator(zone):
        r = real(zone)
        if (zone == "bottom" and not fired
                and sys._getframe(1).f_code.co_name == "_pid_windup_actions"):
            fired.append(True)
            rig.cmd("bottom", "mode", {"mode": "manual"})
        return r                                  # the reading from before his press
    monkeypatch.setattr(rig.driver, "_read_pid_zone", read_then_operator)
    rig.tick(30)
    assert fired
    assert rig.plc.mode("bottom") == "manual"
    assert cmd_writes(rig) == [0.0]                # his write, nothing from the service
    assert not rig.rescue_path.exists()
    assert rig.status("bottom")["rescue_count"] == 0


def test_rescue_last_records_the_bias_after_the_toggle(rig):
    stuck_bottom(rig)
    rig.tick(26)
    last = rig.status("bottom")["rescue_last"]
    assert last["result"] == "ok" and last["bias_pct"] < -100.0
    assert last["bias_after_pct"] == pytest.approx(0.0, abs=0.6)       # Bias = CO


def test_no_toggle_on_a_pv_that_is_not_fed(rig):
    """No PV expression (lost in a restart, or cleared): the PID's PV is a
    frozen number, so 'below SP' may be stale.  Alert, but do not heat
    sooner on it."""
    stuck_bottom(rig)
    rig.cmd("bottom", "pv_expr", {"expr": ""})
    rig.tick(40)
    assert rig.alert("bottom")["rule"] == "pid_stuck_off"
    assert cmd_writes(rig) == [] and rig.plc.mode("bottom") == "auto"
    rig.cmd("bottom", "pv_expr", {"expr": "rtd2"})     # fed again → the remedy acts
    rig.tick(3)
    assert cmd_writes(rig) == [0.0, 1.0]


@pytest.mark.parametrize("zone", ["bottom", "top", "nozzle"])
def test_a_healthy_loop_with_sensor_noise_is_never_toggled(rig, zone):
    """30 min of a zone regulating at SP with ±0.6 K of RTD noise and a
    plant that cools 1 K/min at 0 % (heats at 2 K/min per 100 % above 30 %):
    no alert, no toggle, and the Bias never leaves the output range."""
    import random
    rnd = random.Random(1234)
    ch = ZONES[zone][4]
    rig.expressions()
    rig.tick(3)
    true_k = 165.0
    rig.plc.set_zone(zone, 165.0, true_k, 30.0)
    biases = []
    for _ in range(1800):
        true_k += (rig.plc.co(zone) - 30.0) / 30.0 / 60.0
        rig.rtd_k[ch] = true_k + rnd.uniform(-0.6, 0.6)
        rig.tick(1)
        biases.append(rig.plc.bias(zone))
    assert abs(true_k - 165.0) < 2.0
    assert rig.alert(zone) in (None, "") and cmd_writes(rig, zone) == []
    assert rig.status(zone)["rescue_count"] == 0
    assert min(biases) > -1.0
