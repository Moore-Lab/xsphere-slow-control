"""
CLICK Plus PLC driver — Modbus TCP.

Reads all PLC-connected sensors (RTDs, level sensors, PID state, valve state)
and publishes them to MQTT under the xsphere/sensors/... and xsphere/status/...
topic hierarchy.

Also accepts write commands for:
  - PID setpoints (DF100, DF125, DF150)
  - PID gains (DF105-107, DF130-132, DF155-157)
  - Valve desired state (DS1002, DS1004, DS1006)
  - Valve automation enables (DS1101-1106)
  - Pneumatic valve desired state (DS151, DS152, DS153)
  - Level sensor raw values (DF251, DF252) — written from MQTT callbacks
  - Cryostat level probe voltage (DF333) and its age (DF254) — likewise
    so the PLC's autofill ladder logic stays current

It also watches the three heater PIDs for integrator windup — a loop in Auto
held at 0 % with PV below SP because its Bias sank below the output floor —
and can undo it; see "PID integrator windup" in PlcDriver and
docs/pid-windup.md.

# ==========================================================================
# CLICK PLC Modbus TCP Register Address Mapping
# ==========================================================================
#
# The CLICK Plus C2-series PLC exposes all memory over Modbus TCP on port 502.
# pymodbus uses 0-based addressing for all register reads/writes.
#
# !! VERIFY THESE ADDRESSES BEFORE FIRST USE !!
# Use a Modbus scanner tool (e.g. Modscan, mbpoll, or pymodbus console) to
# confirm the mapping on your specific PLC firmware version.
# The existing Node-RED CLICK Read/Write nodes use symbolic addresses
# (DF1, DS1001, etc.). The mapping below is derived from the CLICK PLC
# C2-USERM manual, Appendix D.
#
# Register type conventions in pymodbus (0-indexed):
#
#   Holding Registers (FC3 read, FC6/FC16 write):
#     DS (16-bit int)  : address = DS_number - 1
#                        DS1 → 0, DS1001 → 1000, DS1002 → 1001
#     DF (32-bit float): address = DF_BASE + (DF_number - 1) * 2
#                        where DF_BASE must be determined from the manual.
#                        Each DF register occupies 2 consecutive holding
#                        registers (big-endian IEEE 754).
#
#   Coils (FC1 read, FC5 write):
#     Y (output bits)  : address = Y_BASE + 32 * slot + (point - 1), where
#                        Y<slot><point> has a 2-digit point number.  The slot
#                        stride is 32, NOT 100: Y001 → 8192, Y101 → 8224,
#                        Y105 → 8228.
#     C (control relay): address = C_BASE + C_number - 1
#
#   Discrete Inputs (FC2 read):
#     X (input bits)   : address = X_BASE + X_number - 1
#
# Known offsets from CLICK PLC documentation (verify against C2-USERM):
#   DS_BASE  = 0        (DS1 = HR address 0)
#   DF_BASE  = 28672    (DF1 = HR address 28672, 28673)
#   Y_BASE   = 8192     (Y001 = coil address 8192)
#   X_BASE   = 0        (X001 = discrete input address 0)
#   C_BASE   = 16384    (C001 = coil address 16384 — Modbus 016385, help CL299)
#
# Reference: C2-USERM (CLICK PLC User Manual), Appendix D – Modbus TCP
# ==========================================================================
"""

from __future__ import annotations

import ast
import json
import logging
import math
import os
import struct
import threading
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

from pymodbus.client import ModbusTcpClient
from pymodbus.exceptions import ModbusException

from slowcontrol.drivers.base import SensorDriver
from slowcontrol.core.mqtt import sensor_topic

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Modbus base addresses (verify against C2-USERM Appendix D)
# ---------------------------------------------------------------------------
DS_BASE: int = 0        # DS1 → holding register 0
DF_BASE: int = 28672    # DF1 → holding registers 28672, 28673
Y_BASE:  int = 8192     # Y001 → coil 8192
X_BASE:  int = 0        # X001 → discrete input 0
# C001 → coil 16384 (Modbus 016385; CLICK help topic CL299).  This was 0 until
# 2026-10-25, so every C-relay read and write went to the wrong coil: the
# controller-type readback was always "pi" and the autotune / controller-type
# writes never reached the PID.  A read-only probe that day confirmed 16384
# (C105/C145/C185 Freeze Bias, C108/C148/C188 bumpless Mode 2 and
# C126/C166/C206 Auto indication all ON, matching the PID setup).
C_BASE:  int = 16384

# ---------------------------------------------------------------------------
# Register map — all addresses as pymodbus 0-based holding register offsets
# unless noted as coil/discrete.
# ---------------------------------------------------------------------------

# DF register addresses (each DF = 2 holding registers)
def _df(n: int) -> int:
    """Return pymodbus holding register address for DF register n."""
    return DF_BASE + (n - 1) * 2


# DS register addresses (each DS = 1 holding register)
def _ds(n: int) -> int:
    """Return pymodbus holding register address for DS register n."""
    return DS_BASE + (n - 1)


def _y(n: int) -> int:
    """Coil address for output bit Y<n>.

    CLICK numbers outputs as slot digit(s) + a 2-digit point, but the Modbus
    addresses advance 32 per slot, not 100 — so Y101 (slot 1, point 1) is
    Y_BASE + 32, not Y_BASE + 100.
    """
    slot, point = divmod(n, 100)
    return Y_BASE + 32 * slot + (point - 1)


def _utc_now() -> str:
    """Now, as the ISO-8601 UTC string the status payloads carry."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --- RTD inputs (read-only, DF, from the CLICK RTD module) ---
# Three RTDs are wired to the CLICK (DF1/DF2/DF3); DF4 is unused.
# Global RTD numbering: 1-3 are on the LabJack, 4-6 are on the PLC.
#   PLC RTD 4 = LN2-vessel base   (DF1)
#   PLC RTD 5 = bottom clamp      (DF2)
#   PLC RTD 6 = top clamp         (DF3)
# (Until 2026-10-15 these two — and every other top/bottom name here — were
#  the wrong way round: RTD5, LJ RTD2, PID 1 / HTR1 / Y004 and NGE ch 1 are
#  on the physically *bottom* clamp.  Older Influx data is under the old names.)
REG_RTD = {
    "rtd_base":         _df(1),   # DF1  RTD 4  LN2 vessel base (°C)
    "rtd_bottom_clamp": _df(2),   # DF2  RTD 5  bottom clamp     (°C)
    "rtd_top_clamp":    _df(3),   # DF3  RTD 6  top clamp        (°C)
}

# MQTT sub-topics for RTD channels (matches topic schema: temperature/{src}/{type}/{ch})
RTD_MQTT_PATH = {
    "rtd_base":         ("plc", "rtd", "4"),
    "rtd_bottom_clamp": ("plc", "rtd", "5"),
    "rtd_top_clamp":    ("plc", "rtd", "6"),
}

# --- Level sensor inputs (read-only, DF, from C2-08D2-6V analog module) ---
REG_LEVEL_RAW = {
    "cryostat":   _df(203),  # DF203 cryostat LN level (0-10V, 0-10 scaled)
}

# Filtered level values (computed by PLC exponential filter in ladder)
REG_LEVEL_FILTERED = {
    "cryostat":   _df(303),  # DF303 filtered cryostat level
    "ballast":    _df(351),  # DF351 filtered ballast level
    "primary_xe": _df(352),  # DF352 filtered primary bottle level
}

# Level raw registers written BY this driver into the PLC so the ladder's
# autofill logic stays current for XV1 (ballast) and XV2 (primary_xe).
REG_LEVEL_WRITE = {
    "ballast":    _df(251),  # DF251 ballast level raw (written by us)
    "primary_xe": _df(252),  # DF252 primary bottle level raw (written by us)
}

# MQTT topics that the GHS/level-sensor ESP32s publish to (we subscribe and
# forward to PLC).
LEVEL_SOURCE_TOPICS = {
    "ballast":    "xsphere/sensors/level/ballast",
    "primary_xe": "xsphere/sensors/level/primary_xe",
}

# The cryostat's internal LN level probe is read over USB by LevelProbeDriver
# (drivers/levelprobe.py), which publishes the voltage the probe's analog
# output stands for on xsphere/sensors/level_probe/{vessel}.  We write that
# voltage to DF333 — the "digital" copy of the cryostat level.  The ladder's
# XV3 fill-status rungs compare it with the same 1.0 / 2.3 limits as DF303,
# the filtered analog input, which stays as the "analog" copy.  It is not
# filtered in the ladder; the probe's firmware already takes a median.
#
# Separately we write the age of that reading in seconds — 0-4 while the probe
# is talking, climbing (to LEVEL_PROBE_AGE_MAX_S) once it stops — for a ladder
# rung that wants to know the voltage has gone stale.  The voltage register is
# left at its last value when that happens.
REG_LEVEL_PROBE_WRITE = {
    "cryostat":   _df(333),  # DF333 cryostat level, digital copy (V, written by us)
}
REG_LEVEL_PROBE_AGE = {
    "cryostat":   _df(254),  # DF254 age of DF333's reading (s, written by us)
}
LEVEL_PROBE_AGE_MAX_S = 9999.0

# --- LabJack T7 temperatures mirrored into PLC DF registers (written by us) ---
# The LabJack publishes its RTD/TC values over MQTT; we forward them into these
# DF registers (°C, 32-bit float) so the CLICK ladder / display can use them.
#   RTD (absolute °C):   DF210 = rtd1 nozzle, DF211 = rtd2 bottom cube, DF212 = rtd3 top cube
#   TC (gradient ΔT °C): DF213 = tc1 cube L/R, DF214 = tc2 LN↔base, DF215 = tc3 nipple, DF216 = tc4 cube F/B
# (mapped by channel number — see the `labjack:` block in config.yaml for the
#  channel→AIN→label/reference assignment.)
REG_LABJACK_RTD_WRITE = {1: _df(210), 2: _df(211), 3: _df(212)}
REG_LABJACK_TC_WRITE  = {1: _df(213), 2: _df(214), 3: _df(215), 4: _df(216)}


# --- PID per-zone control coils (CLICK "C" relays) ---
# The CLICK PID instruction allocates 40 coils per block (C100-C139 for HTR1,
# C140-C179 for HTR2, C180-C219 for HTR3).  Per the PID Monitor UI in the
# CLICK Programming Software (verified 2026-07-04): C<block+15>/C<block+16>
# are the Manual/Auto mode selector, C<block+17> is Autotune-start, and
# C<block+7> is PI-vs-PID (1 = PID, 0 = PI).  Writes must be *level*:
# set the target coil True AND the opposite False in the same operation
# so the ladder is never in a state where both are asserted.
def _c(n: int) -> int:
    """Coil address for control relay C<n>."""
    return C_BASE + (n - 1)

REG_PID_MODE_MANUAL    = {"bottom": _c(115), "top": _c(155), "nozzle": _c(195)}
REG_PID_MODE_AUTO      = {"bottom": _c(116), "top": _c(156), "nozzle": _c(196)}
REG_PID_AUTOTUNE_START = {"bottom": _c(117), "top": _c(157), "nozzle": _c(197)}
# C<block+7> only selects the algorithm Autotune uses (help CL254) — the loop
# itself always runs the full P + I + D with whatever D_Rate holds.
REG_PID_CONTROLLER_PID = {"bottom": _c(107), "top": _c(147), "nozzle": _c(187)}  # 1 = PID, 0 = PI

# First C relay / DS register of each PID's memory (PID setup "CStart" /
# "DSStart"; the DF block is _PID_BLOCKS below).  Offsets used here (CL254):
#   C+05 Freeze Bias      C+07 autotune PI/PID     C+08 bumpless Mode 2
#   C+25 Manual mode indication   C+26 Auto mode indication
#   C+27 Autotune mode indication  (read only — what the PID is really doing;
#        C+15 / C+16 are only the requests)
#   DS+00 PID error code (0 = none; any error drops the loop to Manual)
#   DS+05 sample rate (ms)
_PID_C_BLOCKS  = {"bottom": 100, "top": 140, "nozzle": 180}
_PID_DS_BLOCKS = {"bottom": 100, "top": 115, "nozzle": 130}
_PID_C_READ = 28        # C+00 .. C+27 in one read

# Direct writes to the mode coils above turned out NOT to actually flip the
# PID's internal Auto/Manual state on this PLC — the ladder needs to
# consume a command rung, not raw-coil writes.  The operator added two
# ladder rungs for the mode plumbing:
#
#   DF175 — mode-*command* register the ladder reads and interprets:
#             value 0 → Manual, 1 → Auto.  We write it as int16 at the
#             DF's low word (rung compares against integer 0 / 1).
#   DF176 — mode-*status* mirror the ladder writes with the same
#             convention: 0 = Manual coil asserted, 1 = Auto coil.
#             It copies the *request* coils C115/C116, not the PID's
#             mode indication, so after a PID error (which drops the loop
#             to Manual by itself) it still says Auto.  The coil reads that
#             "proved unreliable" were at the wrong base (C_BASE was 0);
#             the published mode now comes from the indication bits
#             C+25/C+26/C+27, with DF176 only as the fallback.
#
# All three PIDs are wired the same way (added 2026-07-04):
#   PID 1 (bottom) — cmd DF175, status DF176
#   PID 2 (top)    — cmd DF177, status DF178
#   PID 3 (nozzle) — cmd DF179, status DF180
_PID_MODE_CMD_DF_NUM    = {"bottom": 175, "top": 177, "nozzle": 179}
_PID_MODE_STATUS_DF_NUM = {"bottom": 176, "top": 178, "nozzle": 180}
REG_PID_MODE_CMD    = {zone: _df(n) for zone, n in _PID_MODE_CMD_DF_NUM.items()}
REG_PID_MODE_STATUS = {zone: _df(n) for zone, n in _PID_MODE_STATUS_DF_NUM.items()}

# --- PID registers (DF, read setpoint/PV/output; write setpoint/gains) ---
#
#  Each PID block occupies 25 float registers starting at its DF_Memory_Start.
#  Offsets within the block (0-indexed from block start):
#    0  : SP_Setpoint          (°C)   r/w
#    4  : Bias                 (%)    r/w  ← the integrator (help CL254)
#    5  : P_Gain               (Kp)   r/w
#    6  : I_Reset              (s)    r/w  ← reset TIME Ti, published as "ki"
#    7  : D_Rate               (s)    r/w  ← derivative TIME Td, as "kd"
#    8  : OUT_Control          (%)    r    (written by the loop each sample in Auto)
#    9  : CO lower limit       (%)    r/w
#    10 : CO upper limit       (%)    r/w
#    11 : PV_ProcessRaw        (°C)   r/w  ← the PID's PV input (RawPVAddr)
#    12 : PV_ProcessVar        (°C)   r    ← filtered PV used by PID
#
#  HTR1 (bottom, Y004): DF_Memory_Start = DF100
#  HTR2 (top, Y003):    DF_Memory_Start = DF125
#  HTR3 (nozzle, Y002): DF_Memory_Start = DF150  (SP is DF150; the project
#                       file's PID setup says DFStart=DF150, RawPVAddr=DF161)
#
#  The loop, as fitted to live data on 2026-10-25 (docs/pid-windup.md), with
#  e = 100·(SP − PV)/220 % (220 = PV range −200…20 °C), every sample Ts:
#      Bias += Kp·(Ts/Ti)·e          (frozen while it would push CO further
#                                      past a limit — Freeze Bias is on)
#      CO    = Bias + Kp·e + Kp·(Td/Ts)·Δe,  clamped to the CO limits
#  Manual → Auto sets Bias = CO (bumpless Mode 2).

_PID_BLOCKS = {
    "bottom": 100,   # HTR1 DF_Memory_Start
    "top":    125,   # HTR2
    "nozzle": 150,   # HTR3
}

# Offsets within each PID float block
_PID_OFF = {
    "sp":     0,
    "bias":   4,
    "kp":     5,
    "ki":     6,
    "kd":     7,
    "output": 8,
    "co_lo":  9,
    "co_hi":  10,
    "pv_raw": 11,
    "pv":     12,
}
_PID_DF_READ = 13       # DF+00 .. DF+12 in one read

# PID integrator windup — see "PID integrator windup" in PlcDriver.
PID_RESCUE_STEP_S  = 0.2    # how often the mode indication is re-read in a toggle
PID_RESCUE_WAIT_S  = 3.0    # how long a toggle waits for Manual, then for Auto
PID_RESCUE_RETRY_S = 10.0   # how often a loop left in Manual is re-requested to Auto
PID_GUARD_TOL_PCT  = 0.01   # Bias this far outside the output limits gets clamped

def _pid_reg(zone: str, field: str) -> int:
    """Return holding register address for a PID field in a given zone."""
    base_df = _PID_BLOCKS[zone]
    off = _PID_OFF[field]
    return _df(base_df + off)


# --- Valve control registers (DS, integer) ---
REG_VALVE = {
    # Current energised state (read from ladder result)
    "cryostat_state":    _ds(1005),  # DS1005 XV3 present state (0/1)
    "primary_xe_state":  _ds(1003),  # DS1003 XV2 present state
    "ballast_state":     _ds(1001),  # DS1001 XV1 present state
    # Desired state (write to command valve)
    "cryostat_desired":  _ds(1006),  # DS1006 XV3 desired state (0/1)
    "primary_xe_desired":_ds(1004),  # DS1004 XV2 desired state
    "ballast_desired":   _ds(1002),  # DS1002 XV1 desired state
    # Automation enables
    "cryostat_auto_close":   _ds(1105),  # DS1105
    "cryostat_auto_open":    _ds(1106),  # DS1106
    "primary_xe_auto_close": _ds(1103),  # DS1103
    "primary_xe_auto_open":  _ds(1104),  # DS1104
    "ballast_auto_close":    _ds(1101),  # DS1101
    "ballast_auto_open":     _ds(1102),  # DS1102
}

# Coil addresses for actual output state
REG_VALVE_COIL = {
    "cryostat":   _y(103),
    "primary_xe": _y(102),
    "ballast":    _y(101),
}

# --- Pneumatic valves — gas handling (relay outputs Y105-Y107) ---
# Each valve is one pair of ladder rungs:
#
#     DS15x == 1  →  SET Y10x      (open)
#     DS15x != 1  →  RST Y10x      (closed)
#
# so the DS register is the whole command interface.  Unlike XV1-XV3 there is
# no present-state register, no X-input feedback, no auto-open / auto-close and
# no timer — the relay follows the register and nothing else.  The readback is
# therefore the Y output coil itself.
#
# RELAY SENSE.  The register and the coil describe the RELAY, not the valve.
# The ballast and bottle valves are plumbed normally open — at rest, relay
# de-energised, they are OPEN, and energising the relay CLOSES them.  The pump
# valve is normally closed, so energising OPENS it.  Everything on MQTT speaks
# in valve terms (1 = open); the sense is applied in this driver and nowhere
# else, so no consumer has to know which way round a given valve is.
#
# Consequence worth knowing: with the PLC outputs off — power loss, PLC in
# STOP, a register never written — ballast and bottle sit OPEN and pump shut.
#
# They get their own topic family (.../pneumatic/{name}) rather than joining
# .../valve/{vessel}: these are not LN2 fill valves, `ballast` already names
# XV1 there, and the Telegraf valve_state input requires the autofill fields.
#
#   name → (desired register, output coil, normally open?)
PNEUMATIC_VALVES = {
    "ballast": (_ds(151), _y(105), True),    # DS151 → Y105  on the ballast
    "pump":    (_ds(152), _y(106), False),   # DS152 → Y106  on the pump
    "bottle":  (_ds(153), _y(107), True),    # DS153 → Y107  on the bottle
}

# PWM output coil addresses (for reading heater duty cycle state)
REG_HTR_COIL = {
    "bottom": _y(4),
    "top":    _y(3),
    "nozzle": _y(2),
}

CELSIUS_TO_KELVIN = 273.15


# ---------------------------------------------------------------------------
# Driver class
# ---------------------------------------------------------------------------

class PlcDriver(SensorDriver):
    NAME = "plc"

    def __init__(self, config, mqtt, pid_rescue_path: Optional[str] = None):
        super().__init__(config, mqtt)
        self._client: Optional[ModbusTcpClient] = None
        # pymodbus 3.x sync TCP client is NOT thread-safe — concurrent calls
        # over the single socket can scramble PDU framing (a read's response
        # gets matched to a write request and vice versa, returning
        # silently-wrong values or successful-looking ACKs for ops that
        # didn't land). The poll thread and the paho-mqtt callback thread
        # both reach this driver, so every Modbus op is serialized here.
        self._modbus_lock = threading.Lock()
        # Cache latest level values received from ESP32 MQTT topics
        self._level_raw: Dict[str, float] = {}
        # Latest level-probe voltage per vessel, with the monotonic time it
        # arrived: (volts, ts).  See REG_LEVEL_PROBE_WRITE.
        self._level_probe: Dict[str, Tuple[float, float]] = {}
        self._level_probe_stale: Dict[str, bool] = {}
        # Cache latest LabJack temperatures received over MQTT (°C), keyed by
        # channel number — RTD absolute, TC gradient (ΔT) — to mirror into the PLC
        self._labjack_rtd_c: Dict[int, float] = {}
        self._labjack_tc_delta_c: Dict[int, float] = {}
        # Monotonic timestamp of the last MQTT update per channel — used by
        # _write_labjack_to_plc to refuse to mirror stale data into the PLC's
        # PV registers (Layer 2 safety, see _write_labjack_to_plc for detail).
        self._labjack_rtd_ts: Dict[int, float] = {}
        self._labjack_tc_ts:  Dict[int, float] = {}
        # Per-channel interlock-tripped state (mirror is writing the safe
        # surrogate). Tracked so we only alert on the rising edge.
        self._labjack_rtd_tripped: Dict[int, bool] = {}
        # Runtime PV-interlock band limits. Initialized from config.yaml and
        # overridden from disk persistence (slowcontrol/pv_interlock.json) if
        # present. Runtime changes via xsphere/commands/pv_interlock/limits
        # update these in-memory and re-save the JSON. Worth a lock since
        # the MQTT-callback thread writes them and the poll thread reads.
        self._interlock_lock = threading.Lock()
        self._pv_min_k: float = config.plc.pv_min_k
        self._pv_max_k: float = config.plc.pv_max_k
        self._interlock_path = os.path.normpath(os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "..", "pv_interlock.json"))
        self._load_pv_interlock_limits()
        # Aggregate trip state from last poll; published on edge so the
        # snapshot's `pv_interlock_tripped` reflects current reality.
        self._interlock_tripped_was: bool = False
        # Per-PID-zone expressions (free-form, evaluated each poll, result in °C).
        # "setpoint" expression writes the PID's SP register (DF100/125/150);
        # "pv" expression writes the RawPV register (DF111/136/161 — the PID's
        # Process Variable parameter is configured to point at the in-block
        # RawPV slot, so writing it is what sets the PV).
        # Empty string = no active expression for that zone.
        self._pid_sp_expr: Dict[str, str] = {"top": "", "bottom": "", "nozzle": ""}
        self._pid_pv_expr: Dict[str, str] = {"top": "", "bottom": "", "nozzle": ""}
        # Last successful evaluation of each expression (in K), published in
        # the PID status so the GUI can show "rtd1 → 313.5 K". None means
        # either no expression is set or the most recent eval failed
        # (e.g. an alias wasn't populated yet).
        self._pid_sp_expr_value_k: Dict[str, Optional[float]] = {"top": None, "bottom": None, "nozzle": None}
        self._pid_pv_expr_value_k: Dict[str, Optional[float]] = {"top": None, "bottom": None, "nozzle": None}
        # "<zone>/<sp_expr|pv_expr>" whose value is currently refused (NaN, or
        # a PV below the interlock band) — so the warning is logged once.
        self._pid_expr_refused: set = set()
        # Most-recent value_c per sensor alias (rtd1..rtd6, tc1..tc4) for use
        # as identifiers in those expressions.
        self._sensor_c: Dict[str, float] = {}
        # Pneumatic valve control gate.  Open / close commands for the gas
        # valves (xsphere/commands/pneumatic/{name}/state) are only acted on
        # while this is True.  It starts False on every service start and is
        # deliberately not persisted: a stray click can only move a valve
        # after someone has enabled control on the Control page within this
        # service's lifetime.  Shutting the gate moves nothing.
        # Both callbacks that write these run on the MQTT thread; the poll
        # thread only reads them to publish the status.
        self._pneumatic_enabled: bool = False
        self._pneumatic_enabled_utc: Optional[str] = None     # when it last changed
        self._pneumatic_refused: int = 0                      # commands refused since start
        self._pneumatic_last_refused: Optional[dict] = None   # {"valve", "state", "utc"}
        # PID integrator windup — see "PID integrator windup" below.  The
        # poll thread owns all of it except `_pid_mode_cmds` and the pending
        # set, which an operator mode command (MQTT thread) also touches.
        zones = tuple(_PID_BLOCKS)
        self._pid_stuck_since: Dict[str, Optional[float]] = {z: None for z in zones}  # monotonic
        self._pid_stuck_alerted: Dict[str, bool] = {z: False for z in zones}
        self._pid_mode_cmds: Dict[str, int] = {z: 0 for z in zones}   # operator mode commands seen
        self._pid_mode_last: Dict[str, int] = {}      # zone → last operator value (0 / 1)
        self._pid_rescue_lock = threading.Lock()
        self._pid_rescue_path = pid_rescue_path or os.path.normpath(os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "..", "pid_rescue.json"))
        # zone → {"since_utc", "attempts"}: loops this service put in Manual
        # and has not yet seen back in Auto.  Kept on disk, so a crash or a
        # restart between the two halves of a toggle still ends in Auto.
        self._pid_rescue_pending: Dict[str, dict] = {}
        self._pid_rescue_retry_at: Dict[str, float] = {z: float("-inf") for z in zones}
        self._pid_rescue_last_at: Dict[str, float] = {z: float("-inf") for z in zones}
        self._pid_rescue_count: Dict[str, int] = {z: 0 for z in zones}
        self._pid_rescue_last: Dict[str, Optional[dict]] = {z: None for z in zones}
        self._pid_guard_writes: Dict[str, int] = {z: 0 for z in zones}
        self._pid_guard_since: Dict[str, Optional[float]] = {z: None for z in zones}  # monotonic
        self._pid_guard_refused: Dict[str, bool] = {z: False for z in zones}
        self._pid_rescue_unfed: set = set()   # stuck zones not toggled for want of a fed PV (logged)
        if config.plc.pid_stuck_off_remedy not in ("off", "alert", "toggle"):
            log.warning("[plc] plc.pid_stuck_off_remedy %r is not off / alert / toggle — "
                        "treated as off", config.plc.pid_stuck_off_remedy)
        self._load_pid_rescue()

    @property
    def poll_interval(self) -> float:
        return self._config.plc.poll_interval

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def connect(self) -> None:
        cfg = self._config.plc
        self._client = ModbusTcpClient(
            host=cfg.host,
            port=cfg.port,
            timeout=cfg.timeout,
        )
        if not self._client.connect():
            raise ConnectionError(
                f"Could not connect to PLC at {cfg.host}:{cfg.port}"
            )

        # Subscribe to level sensor topics so we can forward to PLC
        for vessel, topic in LEVEL_SOURCE_TOPICS.items():
            self._mqtt.subscribe(topic, self._on_level_message)
        self._mqtt.subscribe(sensor_topic("level_probe", "+"),
                             self._on_level_probe_message)

        # Subscribe to LabJack temperature topics so we can mirror them into
        # PLC DF registers (RTD absolute °C → DF210-212; TC gradient °C → DF213-216).
        self._mqtt.subscribe("xsphere/sensors/temperature/labjack/rtd/+",
                             self._on_labjack_rtd)
        self._mqtt.subscribe("xsphere/sensors/temperature/labjack/tc/+",
                             self._on_labjack_tc)

        # Subscribe to command topics
        from slowcontrol.core.mqtt import command_topic
        self._mqtt.subscribe(command_topic("pid", "+", "setpoint"),        self._on_pid_setpoint)
        self._mqtt.subscribe(command_topic("pid", "+", "gains"),           self._on_pid_gains)
        self._mqtt.subscribe(command_topic("pid", "+", "mode"),            self._on_pid_mode)
        self._mqtt.subscribe(command_topic("pid", "+", "output"),          self._on_pid_output)
        self._mqtt.subscribe(command_topic("pid", "+", "autotune"),        self._on_pid_autotune)
        self._mqtt.subscribe(command_topic("pid", "+", "controller_type"), self._on_pid_controller_type)
        self._mqtt.subscribe(command_topic("pid", "+", "pv"),              self._on_pid_pv_write)
        self._mqtt.subscribe(command_topic("pid", "+", "setpoint_expr"),   self._on_pid_setpoint_expr)
        self._mqtt.subscribe(command_topic("pid", "+", "pv_expr"),         self._on_pid_pv_expr_set)
        self._mqtt.subscribe(command_topic("valve", "+", "state"),         self._on_valve_state)
        self._mqtt.subscribe(command_topic("valve", "+", "auto_close"),    self._on_valve_auto)
        self._mqtt.subscribe(command_topic("valve", "+", "auto_open"),     self._on_valve_auto)
        self._mqtt.subscribe(command_topic("pneumatic", "+", "state"),     self._on_pneumatic_state)
        self._mqtt.subscribe(command_topic("pneumatic_control", "enable"), self._on_pneumatic_control_enable)
        self._mqtt.subscribe(command_topic("pv_interlock", "limits"),      self._on_pv_interlock_limits)
        # Publish the (just-loaded) limits + initial trip state so the
        # snapshot has a value before the first poll.
        self._publish_pv_interlock_status(force=True)
        # Likewise the pneumatic gate — retained, so the Control page shows
        # "disabled" and locks the valve buttons from the moment we are up.
        self._publish_pneumatic_control_status()
        # Start with no stuck-off alerts on the broker: a retained one from a
        # previous run would otherwise never be cleared.  A zone still stuck
        # is re-alerted once it has been for pid_stuck_off_s.
        for zone in _PID_BLOCKS:
            self._mqtt.publish(f"xsphere/alerts/pid_stuck_off/{zone}", "",
                               qos=1, retain=True)
        # (Sensor aliases for setpoint/pv expressions — rtd1..6, tc1..4 — are
        # populated directly when the PlcDriver reads the PLC RTDs and when
        # the LabJack callbacks fire, avoiding any MQTT round-trip race.)

    def disconnect(self) -> None:
        if self._client:
            # A loop this service put in Manual and has not yet seen back in
            # Auto gets one last Auto request on the way out (best effort —
            # pid_rescue.json keeps it pending for the next start anyway).
            for zone in list(self._pid_rescue_pending):
                try:
                    self._write_float(REG_PID_MODE_CMD[zone], 1.0)
                    log.warning("[plc] PID %s: Auto requested on shutdown — an "
                                "automatic rescue had left it in Manual", zone)
                except Exception as exc:
                    log.warning("[plc] PID %s: Auto request on shutdown failed: %s",
                                zone, exc)
            self._client.close()
            self._client = None

    # ------------------------------------------------------------------
    # Poll loop
    # ------------------------------------------------------------------

    def _ensure_modbus(self) -> bool:
        """Make sure the Modbus TCP socket is alive; reconnect if it dropped."""
        c = self._client
        if c is not None and getattr(c, "connected", False):
            return True
        if c is not None:
            try: c.close()
            except Exception: pass
        cfg = self._config.plc
        self._client = ModbusTcpClient(host=cfg.host, port=cfg.port, timeout=cfg.timeout)
        try:
            ok = self._client.connect()
        except Exception as exc:
            log.warning("[plc] Modbus reconnect failed: %s", exc)
            self._client = None
            return False
        if not ok:
            self._client = None
            return False
        log.info("[plc] Modbus reconnected to %s:%d", cfg.host, cfg.port)
        return True

    def poll(self) -> None:
        if not self._ensure_modbus():
            return
        try:
            # The PV interlock is decided first, from the LabJack data already
            # cached, so that nothing below writes a PID setpoint or PV while
            # it is tripped: the trip is the only writer of SP then.
            tripped = self._check_pv_interlock()
            self._publish_rtds()
            self._publish_level()
            pid = self._publish_pid_status(tripped)
            self._publish_valve_status()
            self._write_level_to_plc()
            if not tripped:
                self._write_pid_expressions()
            self._write_labjack_to_plc(tripped)
            # Windup guard / stuck-off remedy last: a toggle waits a second or
            # two for the PLC, and everything above has been written by then.
            self._pid_windup_actions(pid, tripped)
            # Last, and fenced off: a fault in this readback must not send the
            # PID-expression and PV-interlock writes above down the reconnect
            # path on every poll.
            try:
                self._publish_pneumatic_status()
                # Republished every poll, so the registry can tell a live
                # "disabled" from a dead driver and Telegraf logs it next to
                # the valves' desired state.
                self._publish_pneumatic_control_status()
            except Exception as exc:
                log.warning("[plc] pneumatic valve readback failed: %s", exc)
        except (BrokenPipeError, ConnectionResetError, ConnectionError, OSError, ModbusException) as exc:
            log.warning("[plc] Modbus poll error — will reconnect: %s", exc)
            try:
                if self._client is not None:
                    self._client.close()
            except Exception: pass
            self._client = None
        except Exception:
            log.exception("[plc] unexpected error during poll")

    # ------------------------------------------------------------------
    # Read helpers
    # ------------------------------------------------------------------

    def _read_float(self, address: int) -> Optional[float]:
        """Read a 32-bit IEEE 754 float from two consecutive holding registers.

        The CLICK PLC stores 32-bit floats low-word-first (little-endian word
        order), big-endian byte order within each 16-bit word — i.e. the float
        occupies [low_word @ address, high_word @ address+1].
        """
        with self._modbus_lock:
            rr = self._client.read_holding_registers(address, count=2)
        if rr.isError():
            log.debug("[plc] read error at address %d", address)
            return None
        raw = (rr.registers[1] << 16) | rr.registers[0]
        return struct.unpack(">f", struct.pack(">I", raw))[0]

    def _read_int(self, address: int) -> Optional[int]:
        """Read a single 16-bit integer holding register."""
        with self._modbus_lock:
            rr = self._client.read_holding_registers(address, count=1)
        if rr.isError():
            return None
        return rr.registers[0]

    def _read_floats(self, address: int, n: int) -> Optional[List[float]]:
        """Read n consecutive DF floats in one request (word order as _read_float)."""
        with self._modbus_lock:
            rr = self._client.read_holding_registers(address, count=2 * n)
        if rr.isError() or len(rr.registers) < 2 * n:
            return None
        r = rr.registers
        return [struct.unpack(">f", struct.pack(">I", (r[2 * i + 1] << 16) | r[2 * i]))[0]
                for i in range(n)]

    def _read_ints(self, address: int, n: int) -> Optional[List[int]]:
        """Read n consecutive 16-bit holding registers (DS) in one request."""
        with self._modbus_lock:
            rr = self._client.read_holding_registers(address, count=n)
        if rr.isError() or len(rr.registers) < n:
            return None
        return list(rr.registers[:n])

    def _read_coils(self, address: int, n: int) -> Optional[List[bool]]:
        """Read n consecutive coils (C relays) in one request."""
        with self._modbus_lock:
            rr = self._client.read_coils(address, count=n)
        if rr.isError() or len(rr.bits) < n:
            return None
        return [bool(b) for b in rr.bits[:n]]

    def _write_float(self, address: int, value: float) -> bool:
        """Write a 32-bit float to two consecutive holding registers.

        Low word first, to match _read_float (CLICK little-endian word order).
        """
        packed = struct.pack(">f", value)
        raw = struct.unpack(">I", packed)[0]
        hi = (raw >> 16) & 0xFFFF
        lo = raw & 0xFFFF
        with self._modbus_lock:
            result = self._client.write_registers(address, [lo, hi])
        return not result.isError()

    def _write_int(self, address: int, value: int) -> bool:
        """Write a single 16-bit integer to a holding register."""
        with self._modbus_lock:
            result = self._client.write_register(address, value)
        return not result.isError()

    def _read_coil(self, address: int) -> Optional[bool]:
        """Read a single coil/discrete control relay."""
        with self._modbus_lock:
            rr = self._client.read_coils(address, count=1)
        if rr.isError():
            return None
        return bool(rr.bits[0])

    def _write_coil(self, address: int, value: bool) -> bool:
        """Set or clear a single coil/control relay."""
        with self._modbus_lock:
            result = self._client.write_coil(address, bool(value))
        return not result.isError()

    # ------------------------------------------------------------------
    # Publish: RTDs
    # ------------------------------------------------------------------

    def _publish_rtds(self) -> None:
        for name, addr in REG_RTD.items():
            val_c = self._read_float(addr)
            if val_c is None:
                continue
            val_k = val_c + CELSIUS_TO_KELVIN
            path = RTD_MQTT_PATH[name]
            # Cache the value under the alias "rtd<channel>" so setpoint/pv
            # expressions can reference it (no MQTT round-trip). Kelvin.
            self._sensor_c[f"rtd{path[-1]}"] = val_k
            self._mqtt.publish_sensor(
                "temperature", *path,
                payload={"value_c": round(val_c, 3),
                         "value_k": round(val_k, 3)},
            )

    # ------------------------------------------------------------------
    # Publish: Level sensors
    # ------------------------------------------------------------------

    def _publish_level(self) -> None:
        # Only the cryostat LN level is sampled by the CLICK itself (DF203 raw,
        # DF303 filtered).  The ballast / primary_xe levels are published by the
        # GHS ESP32 (legacy analog channels) — see PUBLISH_LEGACY_ANALOG_LEVEL
        # in the gas-handling-system firmware — and the slow-control service
        # forwards those back into the PLC via _write_level_to_plc.
        raw = self._read_float(REG_LEVEL_RAW["cryostat"])
        filtered = self._read_float(REG_LEVEL_FILTERED["cryostat"])
        if raw is None and filtered is None:
            return
        payload = {}
        if raw is not None:
            payload["raw"] = round(raw, 4)
        payload["filtered"] = round(filtered, 4) if filtered is not None else payload.get("raw")
        self._mqtt.publish_sensor("level", "cryostat", payload=payload)

    def _write_level_to_plc(self) -> None:
        """Forward latest ESP32 level readings into PLC DF251/DF252 so the
        PLC ladder's autofill decisions for XV1/XV2 remain current."""
        for vessel, addr in REG_LEVEL_WRITE.items():
            val = self._level_raw.get(vessel)
            if val is not None:
                self._write_float(addr, val)

        # Level probe read over USB: voltage → DF333 while it is fresh, and
        # the age of that reading → DF254 always.
        now = time.monotonic()
        stale_s = self._config.level_probe.stale_s
        for vessel, addr in REG_LEVEL_PROBE_WRITE.items():
            got = self._level_probe.get(vessel)
            age = LEVEL_PROBE_AGE_MAX_S if got is None else min(
                now - got[1], LEVEL_PROBE_AGE_MAX_S)
            stale = age > stale_s
            if not stale:
                self._write_float(addr, got[0])
            self._write_float(REG_LEVEL_PROBE_AGE[vessel], round(age, 1))
            if got is not None and stale != self._level_probe_stale.get(vessel, False):
                self._level_probe_stale[vessel] = stale
                if stale:
                    log.warning("[plc] %s level probe: no reading for %.0f s — "
                                "PLC register holds the last voltage (%.3f V)",
                                vessel, age, got[0])
                else:
                    log.info("[plc] %s level probe: readings are back", vessel)

    def _check_pv_interlock(self) -> bool:
        """Decide the PV safety interlock from the cached LabJack RTD data and
        return True while it is tripped.  Pure bookkeeping — no Modbus: it
        runs first in poll(), so the expression writes can be skipped and
        _write_labjack_to_plc told what to do.

        Trip rules (per-channel; system-wide response):
          • stale          : no MQTT update for this channel in `pv_stale_s`
                             (default 10 s). Guards against the LabJack feed
                             freezing — the 2026-06-07 incident's mechanism.
          • outside band   : the measurement is NOT in [pv_min_k, pv_max_k]
                             (defaults 77 K and 310 K). Catches both runaway
                             over-temp AND sensor-fault values like a broken
                             RTD reading -100000 K. The band is runtime-
                             adjustable from the Control page and persisted
                             to slowcontrol/pv_interlock.json so a bakeout
                             ceiling survives a service restart.

        Logs, alerts (retained, per channel) and the aggregate
        xsphere/status/pv_interlock are published on the edges.
        `_interlock_tripped_was` is updated here, so the MQTT-thread command
        handlers see this poll's verdict.
        """
        plc_cfg = self._config.plc
        safe_k     = plc_cfg.pv_safe_surrogate_k
        sp_safe_c  = plc_cfg.sp_safe_surrogate_k - CELSIUS_TO_KELVIN

        # ── Detection: per-channel trip reasons ─────────────────────────
        rtd_trip_reason = self._pv_trip_reasons()

        # ── Edge-triggered per-channel logging + retained alerts ────────
        for ch in REG_LABJACK_RTD_WRITE:
            reason       = rtd_trip_reason.get(ch)
            was_tripped  = self._labjack_rtd_tripped.get(ch, False)
            if reason and not was_tripped:
                log.warning("[plc] LJ RTD%d interlock TRIPPED (%s): "
                            "forcing SP→%.1f °C and OUT→0%% on all three zones%s",
                            ch, reason, sp_safe_c,
                            " (and PV→surrogate)" if plc_cfg.pv_interlock_writes_pv else "")
                self._publish_pv_alert(ch, "rtd", reason,
                                       self._labjack_rtd_c.get(ch), safe_k)
                self._labjack_rtd_tripped[ch] = True
            elif (not reason) and was_tripped:
                log.info("[plc] LJ RTD%d interlock CLEARED — "
                         "value %.1f °C is fresh and within band",
                         ch, self._labjack_rtd_c.get(ch, float("nan")))
                self._clear_pv_alert(ch, "rtd")
                self._labjack_rtd_tripped[ch] = False

        # Publish the consolidated interlock status whenever the aggregate
        # trip state changes (the limits handler publishes on its own).
        tripped_now = bool(rtd_trip_reason)
        if tripped_now != self._interlock_tripped_was:
            self._interlock_tripped_was = tripped_now
            self._publish_pv_interlock_status(tripped=tripped_now,
                                              reasons=rtd_trip_reason)
        return tripped_now

    def _pv_trip_reasons(self) -> Dict[int, str]:
        """The PV interlock's verdict on the cached LabJack RTDs right now:
        {channel: reason} for every channel that trips it, {} if none.  No
        side effects — _check_pv_interlock acts on it once per poll, and
        _write_pid_expressions asks again just before it writes (a reading
        that arrives between the two must not reach a PID)."""
        plc_cfg = self._config.plc
        stale_s = plc_cfg.pv_stale_s
        with self._interlock_lock:
            pv_min_c = self._pv_min_k - CELSIUS_TO_KELVIN
            pv_max_c = self._pv_max_k - CELSIUS_TO_KELVIN
        now = time.monotonic()
        reasons: Dict[int, str] = {}
        for ch in REG_LABJACK_RTD_WRITE:
            val_c = self._labjack_rtd_c.get(ch)
            ts    = self._labjack_rtd_ts.get(ch)
            if ts is None or (now - ts) > stale_s:
                reasons[ch] = "stale"
            elif val_c is None:
                reasons[ch] = "no-value"
            elif not math.isfinite(val_c):
                # NaN compares False both ways, so it would slip past the band.
                reasons[ch] = f"not a number ({val_c})"
            elif val_c < pv_min_c:
                reasons[ch] = f"below band ({val_c:.1f} °C < {pv_min_c:.1f} °C)"
            elif val_c > pv_max_c:
                reasons[ch] = f"above band ({val_c:.1f} °C > {pv_max_c:.1f} °C)"
        return reasons

    def _write_labjack_to_plc(self, tripped: bool) -> None:
        """Mirror the latest LabJack temperatures into PLC DF registers (°C),
        AND, while the PV interlock is tripped (see _check_pv_interlock),
        hold the heater PIDs off.

        Normal path:
          RTD absolute → DF210-212 (the PLC's "LJ mirror", for display — no
                                    ladder rung reads them; the PIDs' PVs
                                    are written by the pv expressions)
          TC gradient → DF213-216

        While tripped, every poll, on all three zones:
          (1) sp     (DF100/125/150) ← sp_safe_surrogate_k (3 K → -270.15 °C).
              With a real PV of 77 K or more the error is below −34 % of
              the 220 K span, so any Kp above 3 (today's are 14–41) puts
              the output at 0 % whatever the Bias, and the Freeze-Bias rule
              stops the integrator moving while it sits there.
          (2) output (DF108/133/158) ← 0. The leg for a PID that is not
              recomputing at all (e.g. autotune hung when the PV stopped
              changing): it stops updating OUT_Control and our write sticks.
          The PID's own PV (pv_raw, DF111/136/161) is left alone — no
          expression writes it while tripped (poll() skips them), so it holds
          the last good reading.  Until 2026-10-25 a third leg wrote 500 K
          there; dropping that surrogate back to a real PV of ~165 K is a
          ~−430 K error step that the D term turns into one 100 % sample
          during which the integrator jumps to about −190 %, and Freeze Bias
          then holds it there: the heater stays off with PV below SP for
          many minutes (docs/pid-windup.md).  `pv_interlock_writes_pv: true`
          brings that leg back.
          The mirrors DF210-212 get the 500 K surrogate, DF213-216 get 0.

        TC mirrors (DF213-216) get the stale check only — they're not used
        as a PID PV on this PLC. Write 0 on stale.
        """
        plc_cfg = self._config.plc
        stale_s    = plc_cfg.pv_stale_s
        safe_c     = plc_cfg.pv_safe_surrogate_k - CELSIUS_TO_KELVIN
        sp_safe_c  = plc_cfg.sp_safe_surrogate_k - CELSIUS_TO_KELVIN
        now = time.monotonic()

        if tripped:
            for addr in REG_LABJACK_RTD_WRITE.values():
                self._write_float(addr, safe_c)
            for zone in ("top", "bottom", "nozzle"):
                if plc_cfg.pv_interlock_writes_pv:
                    self._write_float(_pid_reg(zone, "pv_raw"), safe_c)
                self._write_float(_pid_reg(zone, "sp"),     sp_safe_c)
                self._write_float(_pid_reg(zone, "output"), 0.0)
            # TC gradients are meaningless when the source is suspect.
            for addr in REG_LABJACK_TC_WRITE.values():
                self._write_float(addr, 0.0)
        else:
            for ch, addr in REG_LABJACK_RTD_WRITE.items():
                val_c = self._labjack_rtd_c.get(ch)
                if val_c is not None:
                    self._write_float(addr, val_c)
            for ch, addr in REG_LABJACK_TC_WRITE.items():
                val = self._labjack_tc_delta_c.get(ch)
                ts  = self._labjack_tc_ts.get(ch)
                if ts is None or (now - ts) > stale_s:
                    self._write_float(addr, 0.0)
                elif val is not None:
                    self._write_float(addr, val)

    # ------------------------------------------------------------------
    # PV interlock helpers — persistence, command handler, status publish
    # ------------------------------------------------------------------

    def _load_pv_interlock_limits(self) -> None:
        """Read user-persisted band limits from slowcontrol/pv_interlock.json.

        Missing or malformed file is fine — we keep the config defaults that
        __init__ already loaded. The file only stores user adjustments, so
        a fresh install gets the config values without needing the JSON to
        exist."""
        try:
            with open(self._interlock_path) as fh:
                d = json.load(fh)
        except FileNotFoundError:
            return
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("[plc] cannot read %s: %s — keeping config defaults",
                        self._interlock_path, exc)
            return
        try:
            self._pv_min_k = float(d["pv_min_k"])
            self._pv_max_k = float(d["pv_max_k"])
            log.info("[plc] loaded persisted PV interlock band: "
                     "[%.2f, %.2f] K from %s",
                     self._pv_min_k, self._pv_max_k, self._interlock_path)
        except (KeyError, TypeError, ValueError) as exc:
            log.warning("[plc] %s malformed (%s) — keeping config defaults",
                        self._interlock_path, exc)

    def _save_pv_interlock_limits(self) -> None:
        """Atomically persist current band limits to disk."""
        tmp = self._interlock_path + ".tmp"
        try:
            with open(tmp, "w") as fh:
                json.dump({"pv_min_k": self._pv_min_k,
                           "pv_max_k": self._pv_max_k}, fh, indent=2)
                fh.write("\n")
            os.replace(tmp, self._interlock_path)
        except OSError as exc:
            log.warning("[plc] cannot persist PV interlock limits to %s: %s",
                        self._interlock_path, exc)

    def _on_pv_interlock_limits(self, topic: str, payload: dict) -> None:
        """xsphere/commands/pv_interlock/limits → {"min_k": X, "max_k": Y}
        Both keys are optional — update only what's provided. Sanity check
        that min < max and both are physically reasonable (positive K)."""
        if not isinstance(payload, dict):
            return
        with self._interlock_lock:
            new_min = self._pv_min_k
            new_max = self._pv_max_k
            if "min_k" in payload:
                try: new_min = float(payload["min_k"])
                except (TypeError, ValueError):
                    log.warning("[plc] pv_interlock min_k bad value: %r",
                                payload["min_k"])
                    return
            if "max_k" in payload:
                try: new_max = float(payload["max_k"])
                except (TypeError, ValueError):
                    log.warning("[plc] pv_interlock max_k bad value: %r",
                                payload["max_k"])
                    return
            if not (0.0 < new_min < new_max):
                log.warning("[plc] pv_interlock limits rejected: "
                            "need 0 < min < max, got [%.2f, %.2f]",
                            new_min, new_max)
                return
            self._pv_min_k = new_min
            self._pv_max_k = new_max
            self._save_pv_interlock_limits()
        log.info("[plc] PV interlock band updated → [%.2f, %.2f] K",
                 new_min, new_max)
        self._publish_pv_interlock_status(force=True)

    def _publish_pv_interlock_status(self, tripped: Optional[bool] = None,
                                     reasons: Optional[Dict[int, str]] = None,
                                     force: bool = False) -> None:
        """Publish retained xsphere/status/pv_interlock with limits and trip
        state. `force=True` skips the trip-edge guard (used on startup and
        on limits-change so the snapshot updates immediately)."""
        with self._interlock_lock:
            payload = {
                "min_k": self._pv_min_k,
                "max_k": self._pv_max_k,
            }
        payload["tripped"] = bool(self._interlock_tripped_was) if tripped is None else bool(tripped)
        if reasons:
            payload["reasons"] = {str(ch): r for ch, r in reasons.items()}
        else:
            payload["reasons"] = {}
        self._mqtt.publish("xsphere/status/pv_interlock", payload,
                           qos=1, retain=True)

    def _publish_pv_alert(self, ch: int, kind: str, reason: str,
                          last_val_c: Optional[float], safe_k: float) -> None:
        if last_val_c is not None and not math.isfinite(last_val_c):
            last_val_c = None                       # NaN is not JSON
        self._mqtt.publish(
            f"xsphere/alerts/pv_interlock/labjack/{kind}/{ch}",
            {"rule": "pv_interlock", "channel": f"labjack/{kind}/{ch}",
             "reason": reason, "last_value_c": last_val_c,
             "surrogate_k": safe_k, "timestamp": time.time()},
            qos=1, retain=True,
        )

    def _clear_pv_alert(self, ch: int, kind: str) -> None:
        # Empty retained payload clears the alert.
        self._mqtt.publish(
            f"xsphere/alerts/pv_interlock/labjack/{kind}/{ch}",
            "", qos=1, retain=True,
        )

    # ------------------------------------------------------------------
    # Publish: PID status
    # ------------------------------------------------------------------

    def _read_pid_zone(self, zone: str) -> Optional[dict]:
        """One PID's state in four Modbus reads: DF+00..+12 (the floats),
        DS+00..+05 (error code, sample rate), C+00..+27 (options, mode
        indication) and the ladder's mode command / mirror pair.  None if the
        float block cannot be read; the other parts degrade to None."""
        df = self._read_floats(_df(_PID_BLOCKS[zone]), _PID_DF_READ)
        if df is None:
            return None
        ds = self._read_ints(_ds(_PID_DS_BLOCKS[zone]), 6)
        c  = self._read_coils(_c(_PID_C_BLOCKS[zone]), _PID_C_READ)
        # DF175/176, 177/178 and 179/180 are adjacent: command, then mirror.
        mc = self._read_floats(REG_PID_MODE_CMD[zone], 2)
        ind = None                  # what the PID itself says it is doing
        if c is not None:
            man, auto, tune = c[25], c[26], c[27]
            if tune:
                ind = "autotune"
            elif auto and not man:
                ind = "auto"
            elif man and not auto:
                ind = "manual"
        return {
            "sp_c": df[_PID_OFF["sp"]], "bias": df[_PID_OFF["bias"]],
            "kp": df[_PID_OFF["kp"]], "ki": df[_PID_OFF["ki"]], "kd": df[_PID_OFF["kd"]],
            "output": df[_PID_OFF["output"]],
            "co_lo": df[_PID_OFF["co_lo"]], "co_hi": df[_PID_OFF["co_hi"]],
            "pv_raw_c": df[_PID_OFF["pv_raw"]], "pv_c": df[_PID_OFF["pv"]],
            "error_code": ds[0] if ds is not None else None,
            "sample_s": ds[5] / 1000.0 if ds is not None and ds[5] > 0 else None,
            "mode_ind": ind,
            "controller_bit": c[7] if c is not None else None,
            "bumpless2": c[8] if c is not None else None,   # Manual → Auto sets Bias = CO
            "mode_cmd": mc[0] if mc is not None else None,
            "mode_status": mc[1] if mc is not None else None,
        }

    def _publish_pid_status(self, tripped: bool = False) -> Dict[str, dict]:
        """xsphere/status/pid/{zone}, and the stuck-off bookkeeping (see
        "PID integrator windup").  Returns {zone: _read_pid_zone(...)} for
        the zones that could be read, for _pid_windup_actions.

        `mode` is the PID's own mode indication (C+25 / C+26 / C+27:
        "manual" / "auto" / "autotune"); only if those bits cannot be read
        does it fall back to the ladder's mirror DF176/178/180 (which copies
        the mode *request*, so it can say "auto" for a loop a PID error has
        dropped to Manual).  `mode_source` says which one it was.
        """
        out: Dict[str, dict] = {}
        now = time.monotonic()
        remedy = self._config.plc.pid_stuck_off_remedy
        for zone in ("top", "bottom", "nozzle"):
            r = self._read_pid_zone(zone)
            if r is None:
                self._pid_stuck_since[zone] = None      # a gap is not "still stuck"
                continue
            out[zone] = r
            self._update_stuck_off(zone, r, tripped, now)
            mode_status = r["mode_status"]
            if r["mode_ind"] is not None:
                mode, mode_source = r["mode_ind"], "indication"
            elif mode_status is None:
                mode, mode_source = "unknown", None     # nothing readable
            elif abs(mode_status - 0.0) < 0.5:
                mode, mode_source = "manual", "ladder"
            elif abs(mode_status - 1.0) < 0.5:
                mode, mode_source = "auto", "ladder"
            else:
                mode, mode_source = "unknown", "ladder"  # unexpected mirror value
            bit = r["controller_bit"]
            # C+07 only picks the autotune algorithm; the loop runs P+I+D.
            controller = "pid" if bit else ("pi" if bit is False else "unknown")
            sp_c, pv_c, out_pct, mode_cmd = r["sp_c"], r["pv_c"], r["output"], r["mode_cmd"]
            sp_val = self._pid_sp_expr_value_k.get(zone)
            pv_val = self._pid_pv_expr_value_k.get(zone)
            with self._pid_rescue_lock:
                pending = zone in self._pid_rescue_pending
            last = self._pid_rescue_last[zone]
            self._mqtt.publish_status(
                "pid", zone,
                payload={
                    "setpoint_c":  round(sp_c, 3),
                    "setpoint_k":  round(sp_c + CELSIUS_TO_KELVIN, 3),
                    "pv_c":        round(pv_c, 3),
                    "pv_k":        round(pv_c + CELSIUS_TO_KELVIN, 3),
                    "pv_raw_k":    round(r["pv_raw_c"] + CELSIUS_TO_KELVIN, 3),
                    "output_pct":  round(out_pct, 2),
                    "kp": r["kp"], "ki": r["ki"], "kd": r["kd"],
                    # The integrator and the limits it should stay inside.
                    "bias_pct":    round(r["bias"], 3),
                    "co_lo_pct":   round(r["co_lo"], 3),
                    "co_hi_pct":   round(r["co_hi"], 3),
                    "mode":            mode,
                    "mode_source":     mode_source,
                    "mode_cmd":        round(mode_cmd, 4)    if mode_cmd    is not None else None,
                    "mode_status_raw": round(mode_status, 4) if mode_status is not None else None,
                    "error_code":      r["error_code"],
                    "sample_s":        r["sample_s"],
                    "controller_type": controller,
                    "setpoint_expr":         self._pid_sp_expr.get(zone, ""),
                    "pv_expr":               self._pid_pv_expr.get(zone, ""),
                    "setpoint_expr_value_k": round(sp_val, 3) if sp_val is not None else None,
                    "pv_expr_value_k":       round(pv_val, 3) if pv_val is not None else None,
                    # Windup watch: stuck_off = confirmed (alert raised);
                    # stuck_off_s = how long the condition has held so far.
                    "stuck_off":      int(self._pid_stuck_alerted[zone]),
                    "stuck_off_s":    r["stuck_s"],
                    "rescue_remedy":  remedy,
                    "rescue_count":   self._pid_rescue_count[zone],
                    "rescue_pending": int(pending),
                    "rescue_last":    dict(last) if last else None,
                    "guard":          ("refused" if self._pid_guard_refused[zone] else
                                       "on" if self._config.plc.pid_integrator_guard else "off"),
                    "guard_writes":   self._pid_guard_writes[zone],
                },
            )
        return out

    # ------------------------------------------------------------------
    # Publish: Valve status
    # ------------------------------------------------------------------

    def _publish_valve_status(self) -> None:
        vessels = {
            "cryostat":   ("cryostat_state",   "cryostat_desired",
                           "cryostat_auto_close",   "cryostat_auto_open"),
            "primary_xe": ("primary_xe_state",  "primary_xe_desired",
                           "primary_xe_auto_close", "primary_xe_auto_open"),
            "ballast":    ("ballast_state",     "ballast_desired",
                           "ballast_auto_close",    "ballast_auto_open"),
        }
        for vessel, (sk, dk, ack, aok) in vessels.items():
            state   = self._read_int(REG_VALVE[sk])
            desired = self._read_int(REG_VALVE[dk])
            ac      = self._read_int(REG_VALVE[ack])
            ao      = self._read_int(REG_VALVE[aok])
            if state is None:
                continue
            self._mqtt.publish_status(
                "valve", vessel,
                payload={
                    "state":      state,
                    "desired":    desired,
                    "auto_close": ac,
                    "auto_open":  ao,
                },
            )

    def _publish_pneumatic_status(self) -> None:
        """xsphere/status/pneumatic/{name}
               → {"state": 0|1, "desired": 0|1, "relay": 0|1}

        `state` and `desired` are in VALVE terms (1 = open): `desired` from
        the DS register the operator writes, `state` from the Y output the
        ladder SET/RSTs from it, each with the valve's relay sense applied.
        `relay` is that Y output as it is (1 = energised).

        `state` and `relay` are left out, not inferred from `desired`, when
        the coil cannot be read — so a consumer never mistakes the command for
        the relay's answer.
        """
        for name, (desired_addr, coil_addr, normally_open) in PNEUMATIC_VALVES.items():
            register = self._read_int(desired_addr)
            relay = self._read_coil(coil_addr)
            if register is None and relay is None:
                continue
            payload = {}
            if register is not None:
                # The ladder energises on == 1 exactly; anything else is off.
                payload["desired"] = int((register == 1) != normally_open)
            if relay is not None:
                payload["state"] = int(relay != normally_open)
                payload["relay"] = int(relay)
            self._mqtt.publish_status("pneumatic", name, payload=payload)

    def _publish_pneumatic_control_status(self) -> None:
        """xsphere/status/pneumatic_control
               → {"enabled": 0|1, "since_utc": str|None, "refused": int,
                  "last_refused": {"valve", "state", "utc"}  (once one has been)}

        `enabled` is the gate in _on_pneumatic_state.  `since_utc` is when it
        was last switched (None = untouched since the service started, so
        shut).  `refused` counts open / close commands dropped because the
        gate was shut, and `last_refused` is the latest of them — so a
        Sequencer step that did nothing can be explained from the page.
        """
        payload = {
            "enabled":   int(self._pneumatic_enabled),
            "since_utc": self._pneumatic_enabled_utc,
            "refused":   self._pneumatic_refused,
        }
        if self._pneumatic_last_refused is not None:
            payload["last_refused"] = dict(self._pneumatic_last_refused)
        self._mqtt.publish_status("pneumatic_control", payload=payload)

    # ------------------------------------------------------------------
    # MQTT command callbacks
    # ------------------------------------------------------------------

    def _on_level_message(self, topic: str, payload: dict) -> None:
        """Cache raw level value received from ESP32 MQTT publish."""
        # topic: xsphere/sensors/level/{vessel}
        vessel = topic.split("/")[-1]
        raw = payload.get("raw") if isinstance(payload, dict) else payload
        if raw is not None:
            try:
                self._level_raw[vessel] = float(raw)
            except (TypeError, ValueError):
                pass

    def _on_level_probe_message(self, topic: str, payload: dict) -> None:
        """Cache the level-probe voltage published by LevelProbeDriver."""
        # topic: xsphere/sensors/level_probe/{vessel}
        vessel = topic.split("/")[-1]
        volts = payload.get("voltage_v") if isinstance(payload, dict) else None
        try:
            volts = float(volts)
        except (TypeError, ValueError):
            return
        if volts == volts and vessel in REG_LEVEL_PROBE_WRITE:      # not NaN
            self._level_probe[vessel] = (volts, time.monotonic())

    def _on_labjack_rtd(self, topic: str, payload: dict) -> None:
        """Cache a LabJack RTD — value_c (°C) for the PLC mirror DF210-212 and
        value_k (K) as the `rtd<n>` alias usable in setpoint/pv expressions.
        Topic: xsphere/sensors/temperature/labjack/rtd/<n>"""
        log.debug("[plc] labjack rtd callback: %s", topic)
        if not isinstance(payload, dict):
            return
        try:
            ch = int(topic.rsplit("/", 1)[-1])
        except (TypeError, ValueError):
            return
        val_c = payload.get("value_c")
        val_k = payload.get("value_k")
        if val_k is None and val_c is not None:
            try: val_k = float(val_c) + CELSIUS_TO_KELVIN
            except (TypeError, ValueError): pass
        if val_c is not None:
            try:
                self._labjack_rtd_c[ch] = float(val_c)
                self._labjack_rtd_ts[ch] = time.monotonic()
            except (TypeError, ValueError): pass
        if val_k is not None:
            try: self._sensor_c[f"rtd{ch}"] = float(val_k)
            except (TypeError, ValueError): pass

    def _on_labjack_tc(self, topic: str, payload: dict) -> None:
        """Cache a LabJack thermocouple absolute temperature (K) under the
        `tc<n>` alias for expressions, and its gradient (ΔT, °C) to mirror into
        the PLC (DF213-216).
        Topic: xsphere/sensors/temperature/labjack/tc/<n>"""
        if not isinstance(payload, dict):
            return
        try:
            ch = int(topic.rsplit("/", 1)[-1])
        except (TypeError, ValueError):
            return
        delta_c = payload.get("delta_c")
        if delta_c is not None:
            try:
                self._labjack_tc_delta_c[ch] = float(delta_c)
                self._labjack_tc_ts[ch] = time.monotonic()
            except (TypeError, ValueError): pass
        val_k = payload.get("value_k")
        if val_k is None and payload.get("value_c") is not None:
            try: val_k = float(payload["value_c"]) + CELSIUS_TO_KELVIN
            except (TypeError, ValueError): pass
        if val_k is not None:
            try: self._sensor_c[f"tc{ch}"] = float(val_k)
            except (TypeError, ValueError): pass

    def _on_pid_setpoint(self, topic: str, payload: dict) -> None:
        """xsphere/commands/pid/{zone}/setpoint  → {"value_k": X}.

        A plain numeric setpoint is just a constant setpoint expression, so
        this redirects into the expression mechanism: it stores the value as
        the zone's setpoint expression and writes the SP register once for
        instant feedback. `_write_pid_expressions` then re-asserts it every
        poll. The point is a single per-poll writer of the SP register — a
        one-shot write here would otherwise be overwritten next poll by any
        non-empty expression (and the GradientController publishes on this
        same topic, so it benefits too).

        While the PV interlock is tripped the trip is that single writer
        (SP = 3 K): the new setpoint is stored and only reaches the PLC
        with the first poll after the trip clears."""
        parts = topic.split("/")
        zone = parts[-2]
        if zone not in _PID_BLOCKS:
            log.warning("[plc] unknown PID zone: %s", zone)
            return
        value_k = payload.get("value_k")
        if value_k is None:
            return
        value_k = float(value_k)
        if not math.isfinite(value_k):
            log.warning("[plc] PID %s setpoint %r refused: not a number", zone, value_k)
            return
        value_c = value_k - CELSIUS_TO_KELVIN
        self._pid_sp_expr[zone] = repr(value_k)
        if self._interlock_tripped_was:
            log.info("[plc] PID %s setpoint → %.2f K held: the PV interlock is "
                     "tripped — written when it clears", zone, value_k)
            return
        ok = self._write_float(_pid_reg(zone, "sp"), value_c)
        log.info("[plc] PID %s setpoint → %.2f K (%.2f °C): %s",
                 zone, value_k, value_c, "OK" if ok else "FAIL")

    def _on_pid_gains(self, topic: str, payload: dict) -> None:
        """xsphere/commands/pid/{zone}/gains  → {"kp": X, "ki": X, "kd": X}"""
        parts = topic.split("/")
        zone = parts[-2]
        if zone not in _PID_BLOCKS:
            return
        for field_name, key in [("kp", "kp"), ("ki", "ki"), ("kd", "kd")]:
            val = payload.get(key)
            if val is not None:
                self._write_float(_pid_reg(zone, field_name), float(val))
        log.info("[plc] PID %s gains updated: %s", zone, payload)

    def _on_pid_autotune(self, topic: str, payload: dict) -> None:
        """xsphere/commands/pid/{zone}/autotune — REFUSED.

        Until 2026-10-25 this wrote C117/C157/C197 at the wrong coil base
        (C_BASE 0), so the "trigger autotune" button never reached the PLC.
        With the base fixed it would start a real closed-loop autotune — a
        heater limit cycle — so it stays off until it has an arm gate like
        the pneumatic valves'."""
        zone = topic.split("/")[-2]
        if zone not in _PID_BLOCKS:
            return
        log.warning("[plc] PID %s autotune REFUSED: disabled until it has an "
                    "enable gate (it never reached the PLC before 2026-10-25)", zone)

    def _on_pid_mode(self, topic: str, payload: dict) -> None:
        """xsphere/commands/pid/{zone}/mode  → {"mode": "auto"|"manual"}.

        Preferred path: write the ladder's mode-command DF register (0 =
        Manual, 1 = Auto).  The rung reads that DF and set/resets the two
        mode coils on the next PLC scan.  Direct coil writes were tried
        first but the CLICK PID instruction doesn't respect them without
        the intervening rung.

        Fallback (for zones whose command register isn't wired yet):
        write the mode coils directly.  This won't actually flip the
        PID's internal mode but at least the coil readback follows what
        the operator asked for, which is enough for the GUI to look
        consistent while the operator's still wiring up the ladder for
        the other zones.
        """
        zone = topic.split("/")[-2]
        if zone not in _PID_BLOCKS:
            return
        mode = str(payload.get("mode", "")).lower()
        if mode == "manual":
            cmd_value = 0
        elif mode == "auto":
            cmd_value = 1
        else:
            log.warning("[plc] PID %s mode: bad payload %r", zone, payload)
            return
        # An operator (page, AI, sequencer) has spoken for this zone's mode:
        # an automatic toggle under way stops at its next check, and a loop
        # the service was trying to get back to Auto is left to them.
        with self._pid_rescue_lock:
            self._pid_mode_cmds[zone] += 1
            self._pid_mode_last[zone] = cmd_value
            dropped = self._pid_rescue_pending.pop(zone, None)
            if dropped is not None:
                self._save_pid_rescue_locked()
        if dropped is not None:
            log.info("[plc] PID %s: operator mode command %r — the service no "
                     "longer re-requests Auto after its rescue", zone, mode)

        cmd_addr = REG_PID_MODE_CMD.get(zone)
        if cmd_addr is not None:
            df_num = _PID_MODE_CMD_DF_NUM[zone]
            # Write a full 32-bit float — the CLICK ladder reads DF175 as
            # a float, and writing just the low 16 bits leaves the high
            # word stale (previous float bit pattern) so the composite
            # float doesn't compare equal to 0.0 or 1.0.
            ok = self._write_float(cmd_addr, float(cmd_value))
            log.info("[plc] PID %s mode → %s via DF%d = %.1f: %s",
                     zone, mode, df_num, float(cmd_value), "OK" if ok else "FAIL")
        else:
            log.warning("[plc] PID %s mode: no mode-command DF wired for "
                        "this zone; falling back to direct coil write (may "
                        "not actually flip the PID's internal mode).", zone)
            new_manual = (mode == "manual")
            if new_manual:
                self._write_coil(REG_PID_MODE_AUTO[zone], False)
                ok = self._write_coil(REG_PID_MODE_MANUAL[zone], True)
            else:
                self._write_coil(REG_PID_MODE_MANUAL[zone], False)
                ok = self._write_coil(REG_PID_MODE_AUTO[zone], True)
            log.info("[plc] PID %s mode → %s (direct coil): %s",
                     zone, mode, "OK" if ok else "FAIL")

    def _on_pid_output(self, topic: str, payload: dict) -> None:
        """xsphere/commands/pid/{zone}/output  → {"value_pct": X}.

        Writes the output register (DF108/DF133/DF158). Effective only when
        the PID is in Manual mode — in Auto the PID overwrites this each
        scan.  For a heater at 0-100 %; range-clamped for safety."""
        zone = topic.split("/")[-2]
        if zone not in _PID_BLOCKS:
            return
        try:
            value_pct = float(payload.get("value_pct"))
        except (TypeError, ValueError):
            log.warning("[plc] PID %s output: bad payload %r", zone, payload)
            return
        # Clamp to [0, 100] as a safety measure — the PLC's own output range
        # is set in the PID Monitor's "Output Range" block but we don't want
        # to write a negative or wildly-large number even if a client sends
        # one by mistake.
        value_pct = max(0.0, min(100.0, value_pct))
        ok = self._write_float(_pid_reg(zone, "output"), value_pct)
        log.info("[plc] PID %s manual output → %.2f %%: %s",
                 zone, value_pct, "OK" if ok else "FAIL")

    def _on_pid_controller_type(self, topic: str, payload: dict) -> None:
        """xsphere/commands/pid/{zone}/controller_type — REFUSED.

        C107/C147/C187 only choose the algorithm Autotune uses (the loop
        always runs P + I + D; D_Rate = 0 makes it PI).  The write never
        reached the PLC before the coil base was fixed on 2026-10-25, and it
        is meaningless while autotune is refused, so it stays off with it."""
        zone = topic.split("/")[-2]
        if zone not in _PID_BLOCKS:
            return
        log.warning("[plc] PID %s controller_type %r REFUSED: it only selects the "
                    "autotune algorithm, and autotune is disabled", zone, payload)

    def _on_pid_pv_write(self, topic: str, payload: dict) -> None:
        """xsphere/commands/pid/{zone}/pv  → {"value_c": X} or {"value_k": X}.

        Writes the PID's RawPV register (DF111/136/161 — assumed to be the
        register the PID instruction's Process Variable parameter points at)."""
        zone = topic.split("/")[-2]
        if zone not in _PID_BLOCKS:
            return
        if "value_c" in payload:
            value_c = float(payload["value_c"])
        elif "value_k" in payload:
            value_c = float(payload["value_k"]) - CELSIUS_TO_KELVIN
        else:
            return
        if self._interlock_tripped_was:
            # Nothing writes a PID's PV while the interlock holds it off; a
            # one-shot value would be stale by the time it clears, so drop it.
            log.warning("[plc] PID %s PV ← %.3f °C dropped: the PV interlock is "
                        "tripped", zone, value_c)
            return
        ok = self._write_float(_pid_reg(zone, "pv_raw"), value_c)
        log.info("[plc] PID %s PV ← %.3f °C: %s",
                 zone, value_c, "OK" if ok else "FAIL")

    # ------------------------------------------------------------------
    # PID setpoint / PV expressions  (e.g.  "rtd1 + DF3 - 4")
    #   - identifiers: rtd1..rtd6, tc1..tc4   (most recent value_c, °C)
    #   - register reads: DF<n>, DS<n>, C<n>  (float / int / 0|1)
    #   - arithmetic: + - * / parens, numeric literals
    # The expression result is the °C value to write each poll.
    # ------------------------------------------------------------------

    def _expr_lookup(self, name: str) -> Optional[float]:
        """Resolve an identifier inside a setpoint/pv expression."""
        if name in self._sensor_c:
            return self._sensor_c[name]
        # Register references: DF<n>, DS<n>, C<n>
        if len(name) >= 2 and name[0] in ("D", "C"):
            try:
                if name.startswith("DF"):
                    return self._read_float(_df(int(name[2:])))
                if name.startswith("DS"):
                    v = self._read_int(_ds(int(name[1:])))
                    return float(v) if v is not None else None
                if name.startswith("C"):
                    b = self._read_coil(_c(int(name[1:])))
                    return 1.0 if b else (0.0 if b is False else None)
            except (ValueError, ModbusException):
                return None
        return None

    def _eval_expr(self, expr: str) -> Optional[float]:
        """Safely evaluate a setpoint/pv expression. Returns None on any error."""
        try:
            tree = ast.parse(expr, mode="eval")
        except SyntaxError:
            return None
        try:
            return self._eval_node(tree.body)
        except (ValueError, ZeroDivisionError, TypeError):
            return None

    def _eval_node(self, node) -> float:
        if isinstance(node, ast.Constant):
            if isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
                return float(node.value)
            raise ValueError("non-numeric literal")
        if isinstance(node, ast.Name):
            v = self._expr_lookup(node.id)
            if v is None:
                raise ValueError(f"unknown identifier or missing value: {node.id}")
            return float(v)
        if isinstance(node, ast.BinOp):
            l = self._eval_node(node.left); r = self._eval_node(node.right)
            if isinstance(node.op, ast.Add):  return l + r
            if isinstance(node.op, ast.Sub):  return l - r
            if isinstance(node.op, ast.Mult): return l * r
            if isinstance(node.op, ast.Div):  return l / r
            raise ValueError("unsupported operator")
        if isinstance(node, ast.UnaryOp):
            v = self._eval_node(node.operand)
            if isinstance(node.op, ast.USub): return -v
            if isinstance(node.op, ast.UAdd): return +v
            raise ValueError("unsupported unary op")
        raise ValueError(f"disallowed node: {type(node).__name__}")

    def _on_pid_setpoint_expr(self, topic: str, payload: dict) -> None:
        """xsphere/commands/pid/{zone}/setpoint_expr → {"expr": "..."}; empty clears."""
        zone = topic.split("/")[-2]
        if zone not in _PID_BLOCKS:
            return
        expr = str(payload.get("expr", "")).strip()
        self._pid_sp_expr[zone] = expr
        log.info("[plc] PID %s setpoint expr ← %r", zone, expr or "(none)")

    def _on_pid_pv_expr_set(self, topic: str, payload: dict) -> None:
        """xsphere/commands/pid/{zone}/pv_expr → {"expr": "..."}; empty clears."""
        zone = topic.split("/")[-2]
        if zone not in _PID_BLOCKS:
            return
        expr = str(payload.get("expr", "")).strip()
        self._pid_pv_expr[zone] = expr
        log.info("[plc] PID %s pv expr ← %r", zone, expr or "(none)")

    def _write_pid_expressions(self) -> None:
        """Evaluate any active setpoint/pv expressions and write the PID block.

        Expressions are in Kelvin (matching the sensor aliases rtd<n>/tc<n>);
        we subtract 273.15 before writing the DF, which is stored in °C.
        The resolved value is cached on `self._pid_{sp,pv}_expr_value_k` so
        `_publish_pid_status` can surface it to the GUI as a diagnostic
        ("your expression rtd1 currently evaluates to 313.5 K").

        poll() does not call this while the PV interlock is tripped, so the
        trip is then the only writer of SP and nothing writes the PV.

        Every expression is evaluated first and the interlock asked again
        afterwards, before anything is written.  A LabJack reading that
        arrived since poll() decided the interlock — a broken RTD's first
        absurd value — is then caught here instead of being written: the
        trip no longer overwrites the PV, so a value that got in would sit
        in the PID for the whole trip (an absurdly low PV under SP = 3 K is
        a full-on heater).  For the same reason a PV that is not a number or
        is below the interlock band is never written, whatever its source:
        the PID keeps its last good PV."""
        writes: List[Tuple[int, float]] = []
        with self._interlock_lock:
            pv_min_k = self._pv_min_k
        for zone in ("top", "bottom", "nozzle"):
            for kind, slot, cache in (
                    ("sp_expr", "sp",     self._pid_sp_expr_value_k),
                    ("pv_expr", "pv_raw", self._pid_pv_expr_value_k)):
                expr = (self._pid_sp_expr if kind == "sp_expr" else self._pid_pv_expr).get(zone, "")
                if not expr:
                    cache[zone] = None
                    continue
                v_k = self._eval_expr(expr)
                if v_k is None:
                    cache[zone] = None
                    log.debug("[plc] PID %s %s eval(%r) → None (missing alias / read error)",
                              zone, kind, expr)
                    continue
                v_k = float(v_k)
                cache[zone] = v_k if math.isfinite(v_k) else None
                bad = not math.isfinite(v_k) or (kind == "pv_expr" and v_k < pv_min_k)
                key = f"{zone}/{kind}"
                if bad:
                    if key not in self._pid_expr_refused:
                        self._pid_expr_refused.add(key)
                        log.warning("[plc] PID %s %s %r = %s K NOT written (%s) — the PID "
                                    "keeps its last value", zone, kind, expr, v_k,
                                    "not a number" if not math.isfinite(v_k)
                                    else f"below the interlock band, {pv_min_k:.1f} K")
                    continue
                if key in self._pid_expr_refused:
                    self._pid_expr_refused.discard(key)
                    log.info("[plc] PID %s %s %r = %.2f K is written again", zone, kind, expr, v_k)
                writes.append((_pid_reg(zone, slot), v_k - CELSIUS_TO_KELVIN))
        if writes and self._pv_trip_reasons():
            # The next poll's _check_pv_interlock trips it properly.
            log.info("[plc] PV interlock tripped during the poll — PID expression "
                     "writes skipped")
            return
        for addr, value_c in writes:
            self._write_float(addr, value_c)

    # ------------------------------------------------------------------
    # PID integrator windup
    #
    # The CLICK PIDs run with Freeze Bias on: the integrator (Bias, DF+04)
    # does not move while the output sits at a limit and moving it would push
    # further past it.  That stops it creeping, not jumping: a big error step
    # — the PV interlock's old 500 K surrogate dropping back to a real PV, or
    # a setpoint raised while the PV is far above it — gives one sample in
    # which the D term pins the output at 100 % while the error is hugely
    # negative, and that sample still integrates.  The Bias lands far below
    # 0 % in one go, and Freeze Bias then holds it there while the output
    # reads 0 %.  The heater stays off with PV under SP until the error has
    # unwound it — minutes on the bottom zone, up to ~80 on the nozzle.  A
    # Manual → Auto toggle fixes it at once, because the bumpless transfer
    # (Mode 2) sets Bias = output.  docs/pid-windup.md has the evidence.
    #
    #   stuck off   Auto (the PID's own indication bit), no PID error, PV more
    #               than pid_stuck_off_delta_k below SP, output at or below
    #               pid_stuck_off_output_pct, an output range that allows
    #               heat, and the Bias below the output floor — continuously
    #               for pid_stuck_off_s and at least 2 samples + 5 s, never
    #               while the PV interlock is tripped.  The Bias test is what
    #               makes it windup rather than the D term braking a fast
    #               rise, and it is why resetting Bias to the output (0 %) can
    #               only add heat.  → journal + retained alert
    #               xsphere/alerts/pid_stuck_off/<zone>; `stuck_off` in status.
    #   toggle      (pid_stuck_off_remedy: toggle) one Manual → Auto toggle
    #               through the zone's mode command DF175/177/179 — the same
    #               register the Control page's buttons write — only if the
    #               command, the ladder's mirror and the indication all say
    #               Auto and bumpless Mode 2 is on (C+08: that is what makes
    #               the toggle a Bias reset), only on a PV fed by a live PV
    #               expression, on a fresh re-read, at most once
    #               per pid_rescue_holdoff_s.  The zone is
    #               written to slowcontrol/pid_rescue.json before the first
    #               write; Auto is confirmed on the indication bit, and a loop
    #               not seen back in Auto is asked again every
    #               PID_RESCUE_RETRY_S — after a crash or a restart too —
    #               until it is, or until an operator sends a mode command for
    #               that zone, which always wins.
    #   guard       (pid_integrator_guard) clamp Bias into the output limits
    #               every poll in Auto, so windup never outlasts a poll.  Off
    #               until the PLC is shown to keep a Bias written in Auto; if
    #               it does not, the guard turns itself off for that zone.
    # ------------------------------------------------------------------

    def _stuck_condition(self, r: dict, tripped: bool) -> bool:
        """The instantaneous stuck-off test on one _read_pid_zone reading."""
        cfg = self._config.plc
        return (cfg.pid_stuck_off_remedy in ("alert", "toggle")
                and not tripped
                and r["mode_ind"] == "auto"
                and r["error_code"] == 0
                and r["pv_c"] < r["sp_c"] - cfg.pid_stuck_off_delta_k
                and r["output"] <= cfg.pid_stuck_off_output_pct
                and r["co_hi"] > r["co_lo"] + cfg.pid_stuck_off_output_pct
                and r["bias"] < r["co_lo"])

    def _update_stuck_off(self, zone: str, r: dict, tripped: bool, now: float) -> None:
        """Track one zone's stuck-off condition and raise / clear its alert.
        Adds "stuck_s" (how long the condition has held, 0 if not) and
        "stuck" (held long enough: confirmed) to the reading `r`."""
        cfg = self._config.plc
        sp_k = r["sp_c"] + CELSIUS_TO_KELVIN
        pv_k = r["pv_c"] + CELSIUS_TO_KELVIN
        topic = f"xsphere/alerts/pid_stuck_off/{zone}"
        if not self._stuck_condition(r, tripped):
            self._pid_stuck_since[zone] = None
            self._pid_rescue_unfed.discard(zone)
            r["stuck_s"], r["stuck"] = 0.0, False
            if self._pid_stuck_alerted[zone]:
                self._pid_stuck_alerted[zone] = False
                log.info("[plc] PID %s no longer stuck off (PV %.2f K, SP %.2f K, "
                         "output %.1f %%, bias %.1f %%)",
                         zone, pv_k, sp_k, r["output"], r["bias"])
                self._mqtt.publish(topic, "", qos=1, retain=True)
            return
        since = self._pid_stuck_since[zone]
        if since is None:
            since = self._pid_stuck_since[zone] = now
        held = now - since
        need = max(cfg.pid_stuck_off_s, 2.0 * (r["sample_s"] or 15.0) + 5.0)
        r["stuck_s"], r["stuck"] = round(held, 1), held >= need
        if r["stuck"] and not self._pid_stuck_alerted[zone]:
            self._pid_stuck_alerted[zone] = True
            log.warning("[plc] PID %s STUCK OFF (integrator windup): Auto, PV %.2f K is "
                        "%.2f K below SP %.2f K, output %.1f %%, bias %.1f %% below the "
                        "%.1f %% output floor, for %.0f s — remedy: %s",
                        zone, pv_k, sp_k - pv_k, sp_k, r["output"], r["bias"],
                        r["co_lo"], held, cfg.pid_stuck_off_remedy)
            self._mqtt.publish(topic, {
                "rule": "pid_stuck_off", "zone": zone,
                "pv_k": round(pv_k, 3), "setpoint_k": round(sp_k, 3),
                "output_pct": round(r["output"], 2), "bias_pct": round(r["bias"], 3),
                "held_s": round(held, 1), "remedy": cfg.pid_stuck_off_remedy,
                "timestamp": time.time(),
            }, qos=1, retain=True)

    def _pid_windup_actions(self, pid: Dict[str, dict], tripped: bool) -> None:
        """Once per poll, after everything else has been written: bring back
        loops a rescue left in Manual, run the integrator guard, and toggle
        zones confirmed stuck off.  `pid` is _publish_pid_status's readings."""
        cfg = self._config.plc
        now = time.monotonic()
        # 1. Loops this service put in Manual and has not seen back in Auto
        #    (this run, or before a crash / restart).  Done even while the
        #    interlock is tripped: Auto then just regulates to SP = 3 K.
        with self._pid_rescue_lock:
            pending = list(self._pid_rescue_pending)
        for zone in pending:
            r = pid.get(zone)
            if r is None or r["mode_ind"] is None:
                continue                            # cannot tell — wait
            if r["mode_ind"] == "auto":
                self._pid_rescue_clear(zone)
                log.info("[plc] PID %s is back in Auto after an automatic rescue", zone)
                continue
            if r["mode_ind"] != "manual" or now < self._pid_rescue_retry_at[zone]:
                continue                            # autotune is not ours to touch
            self._pid_rescue_retry_at[zone] = now + PID_RESCUE_RETRY_S
            with self._pid_rescue_lock:
                info = self._pid_rescue_pending.get(zone)
                if info is None:
                    continue                        # an operator just took it
                seq0 = self._pid_mode_cmds[zone]    # any operator command after this wins
                info["attempts"] = int(info.get("attempts", 0)) + 1
                attempt = info["attempts"]
                self._save_pid_rescue_locked()
            if attempt <= 3 or attempt % 6 == 0:
                log.warning("[plc] PID %s is still in MANUAL after an automatic rescue "
                            "(since %s) — requesting Auto again, attempt %d",
                            zone, info.get("since_utc", "?"), attempt)
            result = self._pulse_auto(zone, seq0)
            if result in ("ok", "no_manual"):
                self._pid_rescue_clear(zone)
                log.info("[plc] PID %s is back in Auto after an automatic rescue", zone)
        # 2. Integrator guard.
        if cfg.pid_integrator_guard and not tripped:
            for zone, r in pid.items():
                self._pid_guard_bias(zone, r, now)
        # 3. Stuck-off remedy.
        if cfg.pid_stuck_off_remedy != "toggle" or tripped:
            return
        for zone, r in pid.items():
            if not r.get("stuck"):
                continue
            with self._pid_rescue_lock:
                if zone in self._pid_rescue_pending:
                    continue
                # Taken before the re-read that decides: an operator mode
                # command from here on — even one whose DF175 write has not
                # landed yet — stops the toggle before its first write.
                seq0 = self._pid_mode_cmds[zone]
            if now - self._pid_rescue_last_at[zone] < cfg.pid_rescue_holdoff_s:
                continue
            # Re-read: a toggle of another zone may have taken a few seconds.
            fresh = self._read_pid_zone(zone)
            if fresh is None or not self._stuck_condition(fresh, self._interlock_tripped_was):
                continue
            cmd, mirror = fresh["mode_cmd"], fresh["mode_status"]
            if cmd is None or mirror is None or abs(cmd - 1.0) > 0.01 or abs(mirror - 1.0) > 0.01:
                continue        # the ladder's command / mirror do not both say Auto
            if not fresh["bumpless2"]:
                continue        # the toggle only resets the Bias with bumpless Mode 2
            # Only on a PV that is being fed: a PV expression that evaluated
            # this poll and that the PID's PV register agrees with.  On a
            # frozen PV the "PV below SP" the toggle acts on may be stale,
            # and heating sooner would be heating blind.
            fed_k = self._pid_pv_expr_value_k.get(zone)
            pv_raw_k = fresh["pv_raw_c"] + CELSIUS_TO_KELVIN
            if fed_k is None or abs(pv_raw_k - fed_k) > 5.0:
                if zone not in self._pid_rescue_unfed:
                    self._pid_rescue_unfed.add(zone)
                    log.warning("[plc] PID %s stuck off, but its PV is not fed by a live PV "
                                "expression (pv_expr %r, PV register %.2f K) — not toggling; "
                                "switch Manual → Auto by hand if the PV is right",
                                zone, self._pid_pv_expr.get(zone, ""), pv_raw_k)
                continue
            self._pid_rescue(zone, fresh, now, seq0)

    def _pid_rescue(self, zone: str, r: dict, now: float, seq0: int) -> None:
        """One Manual → Auto toggle of a zone confirmed stuck off.  `seq0` is
        the operator mode-command count the decision was taken under."""
        sp_k = r["sp_c"] + CELSIUS_TO_KELVIN
        pv_k = r["pv_c"] + CELSIUS_TO_KELVIN
        self._pid_rescue_last_at[zone] = now        # the hold-off counts from here
        with self._pid_rescue_lock:
            if self._pid_mode_cmds[zone] != seq0:
                return                              # an operator spoke meanwhile
            self._pid_rescue_pending[zone] = {"since_utc": _utc_now(), "attempts": 1}
            saved = self._save_pid_rescue_locked()
            if not saved:
                del self._pid_rescue_pending[zone]
        if not saved:
            log.error("[plc] PID %s stuck off, but %s cannot be written — not toggling "
                      "(a crash mid-toggle could leave it in Manual)",
                      zone, self._pid_rescue_path)
            return
        self._pid_rescue_count[zone] += 1
        log.warning("[plc] PID %s rescue #%d: toggling Manual → Auto so the PLC sets "
                    "Bias = output (bias %.1f %%, PV %.2f K, SP %.2f K)",
                    zone, self._pid_rescue_count[zone], r["bias"], pv_k, sp_k)
        result = self._pulse_auto(zone, seq0)
        # The Bias right after: the PLC should have set it to the output.
        bias_after = self._read_float(_pid_reg(zone, "bias")) if result == "ok" else None
        self._pid_rescue_last[zone] = {
            "utc": _utc_now(), "result": result, "pv_k": round(pv_k, 2),
            "setpoint_k": round(sp_k, 2), "bias_pct": round(r["bias"], 2),
            "bias_after_pct": round(bias_after, 2) if bias_after is not None else None}
        if result in ("ok", "no_manual", "operator"):
            self._pid_rescue_clear(zone)
        if result == "ok" and bias_after is not None and bias_after < r["co_lo"] - 1.0:
            log.error("[plc] PID %s rescue #%d: back in Auto but the Bias is still %.1f %% "
                      "(was %.1f %%) — the toggle did not reset it; check bumpless Mode 2 "
                      "(C%d)", zone, self._pid_rescue_count[zone], bias_after, r["bias"],
                      _PID_C_BLOCKS[zone] + 8)
        elif result == "ok":
            log.warning("[plc] PID %s rescue #%d done: back in Auto, Bias %.1f %% → %s %%",
                        zone, self._pid_rescue_count[zone], r["bias"],
                        "?" if bias_after is None else f"{bias_after:.1f}")
        elif result == "no_manual":
            log.error("[plc] PID %s rescue: the loop never showed Manual, so nothing "
                      "changed — check the DF%d mode rung", zone, _PID_MODE_CMD_DF_NUM[zone])
        elif result == "operator":
            log.warning("[plc] PID %s rescue: an operator mode command arrived "
                        "mid-toggle — the mode is theirs", zone)
        else:
            log.error("[plc] PID %s rescue: NOT back in Auto (%s) — requesting Auto "
                      "every %.0f s until it is (an operator mode command stops that)",
                      zone, result, PID_RESCUE_RETRY_S)
            self._pid_rescue_retry_at[zone] = time.monotonic() + PID_RESCUE_RETRY_S

    def _pulse_auto(self, zone: str, seq0: int) -> str:
        """Write the zone's mode command 0 (Manual), wait for the PID to show
        Manual, write 1 (Auto), wait for it to show Auto.  Returns
          "ok"           went Manual, then back to Auto
          "no_manual"    never showed Manual, and is in Auto
          "not_auto"     not in Auto within PID_RESCUE_WAIT_S of the request
          "write_failed" the PLC refused a write
          "operator"     an operator mode command arrived since `seq0` (the
                         count the caller decided under); nothing is written
                         if it came before the first write, and their value
                         is written again otherwise, so it is what stands
        Takes up to 2 × PID_RESCUE_WAIT_S of the poll thread."""
        with self._pid_rescue_lock:
            if self._pid_mode_cmds[zone] != seq0:
                return "operator"
        cmd = REG_PID_MODE_CMD[zone]
        if not self._write_float(cmd, 0.0):
            return "write_failed"
        saw_manual = self._wait_pid_mode(zone, "manual", seq0)
        if saw_manual is not None:
            if not self._write_float(cmd, 1.0):
                return "write_failed"
            saw_auto = self._wait_pid_mode(zone, "auto", seq0)
            if saw_auto is not None:
                if not saw_auto:
                    return "not_auto"
                return "ok" if saw_manual else "no_manual"
        # An operator command came in.  Our write may have landed after
        # theirs, so put their value back.
        with self._pid_rescue_lock:
            value = self._pid_mode_last.get(zone)
        if value is not None:
            self._write_float(cmd, float(value))
        return "operator"

    def _wait_pid_mode(self, zone: str, want: str, seq0: int) -> Optional[bool]:
        """Poll the PID's mode indication until it reads `want` (True), the
        wait runs out (False) or an operator mode command arrives (None)."""
        deadline = time.monotonic() + PID_RESCUE_WAIT_S
        addr = _c(_PID_C_BLOCKS[zone] + 25)         # C+25 Manual, C+26 Auto
        while True:
            time.sleep(PID_RESCUE_STEP_S)
            with self._pid_rescue_lock:
                if self._pid_mode_cmds[zone] != seq0:
                    return None
            bits = self._read_coils(addr, 2)
            if bits is not None:
                mode = ("manual" if bits[0] and not bits[1] else
                        "auto" if bits[1] and not bits[0] else None)
                if mode == want:
                    return True
            if time.monotonic() >= deadline:
                return False

    def _pid_guard_bias(self, zone: str, r: dict, now: float) -> None:
        """Clamp a zone's Bias into its output limits (pid_integrator_guard)."""
        if self._pid_guard_refused[zone]:
            return
        with self._pid_rescue_lock:
            pending = zone in self._pid_rescue_pending
        lo, hi, b = r["co_lo"], r["co_hi"], r["bias"]
        target = None
        if r["mode_ind"] == "auto" and r["error_code"] == 0 and not pending and lo < hi:
            if b < lo - PID_GUARD_TOL_PCT:
                target = lo
            elif b > hi + PID_GUARD_TOL_PCT:
                target = hi
        since = self._pid_guard_since[zone]
        if target is None:
            if since is not None:
                self._pid_guard_since[zone] = None
                log.info("[plc] PID %s integrator guard: bias %.2f %% is inside the "
                         "output limits again", zone, b)
            return
        if since is None:
            self._pid_guard_since[zone] = now
            log.warning("[plc] PID %s integrator guard: bias %.2f %% is outside "
                        "[%.1f, %.1f] %% — clamping it to %.1f %%", zone, b, lo, hi, target)
        elif now - since > max(2.0 * (r["sample_s"] or 15.0), 10.0):
            # Clamped every poll for two PID samples and still outside: the
            # PLC does not keep a Bias written in Auto.  Stop writing.
            self._pid_guard_refused[zone] = True
            log.error("[plc] PID %s integrator guard: bias still %.2f %% after %.0f s of "
                      "clamping — the PLC does not keep a Bias written in Auto; guard off "
                      "for this zone (pid_stuck_off_remedy: %s)",
                      zone, b, now - since, self._config.plc.pid_stuck_off_remedy)
            return
        if self._write_float(_pid_reg(zone, "bias"), target):
            self._pid_guard_writes[zone] += 1

    def _pid_rescue_clear(self, zone: str) -> None:
        with self._pid_rescue_lock:
            if self._pid_rescue_pending.pop(zone, None) is not None:
                self._save_pid_rescue_locked()

    def _load_pid_rescue(self) -> None:
        """Pick up loops a previous run put in Manual and never saw back in
        Auto (slowcontrol/pid_rescue.json); the first polls re-request Auto."""
        try:
            with open(self._pid_rescue_path) as fh:
                d = json.load(fh)
        except FileNotFoundError:
            return
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("[plc] cannot read %s: %s", self._pid_rescue_path, exc)
            return
        pending = d.get("pending") if isinstance(d, dict) else None
        if isinstance(pending, dict):
            for zone, info in pending.items():
                if zone in _PID_BLOCKS:
                    self._pid_rescue_pending[zone] = dict(info) if isinstance(info, dict) else {}
        if self._pid_rescue_pending:
            log.warning("[plc] %s: an automatic rescue left %s in Manual and was not "
                        "seen back in Auto — requesting Auto until it is",
                        self._pid_rescue_path, ", ".join(sorted(self._pid_rescue_pending)))

    def _save_pid_rescue_locked(self) -> bool:
        """Write pid_rescue.json atomically and durably (caller holds
        _pid_rescue_lock); the file is removed when nothing is pending."""
        path = self._pid_rescue_path
        try:
            if not self._pid_rescue_pending:
                try:
                    os.remove(path)
                except FileNotFoundError:
                    pass
                return True
            tmp = path + ".tmp"
            with open(tmp, "w") as fh:
                json.dump({"pending": self._pid_rescue_pending}, fh, indent=2)
                fh.write("\n")
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, path)
            return True
        except OSError as exc:
            log.warning("[plc] cannot write %s: %s", path, exc)
            return False

    def _on_valve_state(self, topic: str, payload: dict) -> None:
        """xsphere/commands/valve/{vessel}/state  → {"state": 0|1}"""
        parts = topic.split("/")
        vessel = parts[-2]
        key = f"{vessel}_desired"
        if key not in REG_VALVE:
            log.warning("[plc] unknown vessel: %s", vessel)
            return
        state = int(payload.get("state", 0))
        ok = self._write_int(REG_VALVE[key], state)
        log.info("[plc] valve %s desired → %d: %s",
                 vessel, state, "OK" if ok else "FAIL")

    def _on_valve_auto(self, topic: str, payload: dict) -> None:
        """xsphere/commands/valve/{vessel}/auto_close|auto_open → {"enabled": bool}"""
        parts = topic.split("/")
        vessel = parts[-2]
        mode   = parts[-1]   # "auto_close" or "auto_open"
        key = f"{vessel}_{mode}"
        if key not in REG_VALVE:
            return
        enabled = int(bool(payload.get("enabled", False)))
        self._write_int(REG_VALVE[key], enabled)
        log.info("[plc] valve %s %s → %d", vessel, mode, enabled)

    def _on_pneumatic_state(self, topic: str, payload: dict) -> None:
        """xsphere/commands/pneumatic/{name}/state  → {"state": 0|1}

        `state` is the VALVE position wanted (1 = open).  For a normally-open
        valve that means the opposite relay state, so opening the ballast or
        bottle valve writes 0 to its register and closing it writes 1.
        """
        name = topic.split("/")[-2]
        valve = PNEUMATIC_VALVES.get(name)
        if valve is None:
            log.warning("[plc] unknown pneumatic valve: %s", name)
            return
        desired_addr, _coil, normally_open = valve
        # Only a literal 0/1 opens or closes a gas valve — anything else off
        # the bus is dropped rather than coerced.
        raw = payload.get("state") if isinstance(payload, dict) else None
        if raw not in (0, 1):
            log.warning("[plc] pneumatic valve %s: ignoring state %r", name, raw)
            return
        want_open = bool(raw)
        # The gate.  Checked after the name and the value, so the log says
        # exactly what was asked for and dropped; nothing is queued for later.
        if not self._pneumatic_enabled:
            self._pneumatic_refused += 1
            self._pneumatic_last_refused = {
                "valve": name, "state": int(want_open), "utc": _utc_now()}
            log.warning("[plc] pneumatic valve %s → %s REFUSED: pneumatic valve "
                        "control is disabled — enable it on the Control page first",
                        name, "OPEN" if want_open else "CLOSED")
            self._publish_pneumatic_control_status()
            return
        relay_on = want_open != normally_open
        ok = self._write_int(desired_addr, int(relay_on))
        log.info("[plc] pneumatic valve %s → %s (relay %s): %s",
                 name, "OPEN" if want_open else "CLOSED",
                 "energised" if relay_on else "off", "OK" if ok else "FAIL")

    def _on_pneumatic_control_enable(self, topic: str, payload: dict) -> None:
        """xsphere/commands/pneumatic_control/enable  → {"enabled": true|false}

        Opens or shuts the gate on the pneumatic valve commands.  `enabled`
        must be a bool or a literal 0 / 1; anything else is dropped.  Shutting
        the gate moves no valve — it only stops further open / close commands
        being acted on — and the gate is not remembered across a restart of
        the service: it comes up shut.
        """
        raw = payload.get("enabled") if isinstance(payload, dict) else None
        if isinstance(raw, bool):
            want = raw
        elif isinstance(raw, int) and raw in (0, 1):
            want = bool(raw)
        else:
            log.warning("[plc] pneumatic valve control: ignoring enabled=%r", raw)
            return
        if want != self._pneumatic_enabled:
            self._pneumatic_enabled = want
            self._pneumatic_enabled_utc = _utc_now()
            log.info("[plc] pneumatic valve control %s",
                     "ENABLED — open / close commands are now acted on" if want
                     else "DISABLED — open / close commands are now refused")
        self._publish_pneumatic_control_status()
