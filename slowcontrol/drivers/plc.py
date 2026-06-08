"""
CLICK Plus PLC driver — Modbus TCP.

Reads all PLC-connected sensors (RTDs, level sensors, PID state, valve state)
and publishes them to MQTT under the xsphere/sensors/... and xsphere/status/...
topic hierarchy.

Also accepts write commands for:
  - PID setpoints (DF100, DF125, DF151)
  - PID gains (DF105-107, DF130-132, DF155-157)
  - Valve desired state (DS1002, DS1004, DS1006)
  - Valve automation enables (DS1101-1106)
  - Level sensor raw values (DF251, DF252) — written from MQTT callbacks
    so the PLC's autofill ladder logic stays current

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
#     Y (output bits)  : address = Y_BASE + Y_number - 1
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
#   C_BASE   = 0        (C001 = coil address 0)
#
# Reference: C2-USERM (CLICK PLC User Manual), Appendix D – Modbus TCP
# ==========================================================================
"""

from __future__ import annotations

import ast
import json
import logging
import os
import struct
import threading
import time
from typing import Dict, List, Optional, Tuple

from pymodbus.client import ModbusTcpClient
from pymodbus.exceptions import ModbusException

from slowcontrol.drivers.base import SensorDriver
from slowcontrol.core.mqtt import sensor_topic, status_topic

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Modbus base addresses (verify against C2-USERM Appendix D)
# ---------------------------------------------------------------------------
DS_BASE: int = 0        # DS1 → holding register 0
DF_BASE: int = 28672    # DF1 → holding registers 28672, 28673
Y_BASE:  int = 8192     # Y001 → coil 8192
X_BASE:  int = 0        # X001 → discrete input 0
C_BASE:  int = 0        # C001 → coil 0

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


# --- RTD inputs (read-only, DF, from the CLICK RTD module) ---
# Three RTDs are wired to the CLICK (DF1/DF2/DF3); DF4 is unused.
# Global RTD numbering: 1-3 are on the LabJack, 4-6 are on the PLC.
#   PLC RTD 4 = LN2-vessel base   (DF1)
#   PLC RTD 5 = top clamp         (DF2)
#   PLC RTD 6 = bottom clamp      (DF3)
REG_RTD = {
    "rtd_base":         _df(1),   # DF1  RTD 4  LN2 vessel base (°C)
    "rtd_top_clamp":    _df(2),   # DF2  RTD 5  top clamp        (°C)
    "rtd_bottom_clamp": _df(3),   # DF3  RTD 6  bottom clamp     (°C)
}

# MQTT sub-topics for RTD channels (matches topic schema: temperature/{src}/{type}/{ch})
RTD_MQTT_PATH = {
    "rtd_base":         ("plc", "rtd", "4"),
    "rtd_top_clamp":    ("plc", "rtd", "5"),
    "rtd_bottom_clamp": ("plc", "rtd", "6"),
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

# --- LabJack T7 temperatures mirrored into PLC DF registers (written by us) ---
# The LabJack publishes its RTD/TC values over MQTT; we forward them into these
# DF registers (°C, 32-bit float) so the CLICK ladder / display can use them.
#   RTD (absolute °C):   DF210 = rtd1 nozzle, DF211 = rtd2 top cube, DF212 = rtd3 bottom cube
#   TC (gradient ΔT °C): DF213 = tc1 cube L/R, DF214 = tc2 LN↔base, DF215 = tc3 nipple, DF216 = tc4 cube F/B
# (mapped by channel number — see the `labjack:` block in config.yaml for the
#  channel→AIN→label/reference assignment.)
REG_LABJACK_RTD_WRITE = {1: _df(210), 2: _df(211), 3: _df(212)}
REG_LABJACK_TC_WRITE  = {1: _df(213), 2: _df(214), 3: _df(215), 4: _df(216)}


# --- PID per-zone control coils (CLICK "C" relays) ---
# The CLICK PID instruction allocates 40 coils per block (C100-C139 for HTR1)
# but the firmware's internal Auto/Manual state and the manual-output gating
# are NOT exposed via Modbus on this PLC — neither writes to these coils
# (C115/C116) nor reads of them affect or reflect the actual mode. Mode +
# manual output must be driven from the CLICK Programming Software's PID
# Monitor. Only the bits that do have a measurable effect are kept here.
def _c(n: int) -> int:
    """Coil address for control relay C<n>."""
    return C_BASE + (n - 1)

REG_PID_AUTOTUNE_START = {"top": _c(117), "bottom": _c(157), "nozzle": _c(197)}
REG_PID_CONTROLLER_PID = {"top": _c(107), "bottom": _c(147), "nozzle": _c(187)}  # 1 = PID, 0 = PI

# --- PID registers (DF, read setpoint/PV/output; write setpoint/gains) ---
#
#  Each PID block occupies 25 float registers starting at its DF_Memory_Start.
#  Offsets within the block (0-indexed from block start):
#    0  : SP_Setpoint          (°C)   r/w
#    5  : P_Gain               (Kp)   r/w
#    6  : I_Reset              (Ki)   r/w
#    7  : D_Rate               (Kd)   r/w
#    8  : OUT_Control          (%)    r
#    11 : PV_ProcessRaw        (°C)   r
#    12 : PV_ProcessVar        (°C)   r   ← filtered PV used by PID
#    4  : Bias                 (%)    r/w
#
#  HTR1 (top, Y004):    DF_Memory_Start = DF100
#  HTR2 (bottom, Y003): DF_Memory_Start = DF125
#  HTR3 (nozzle, Y002): DF_Memory_Start = DF150  (SP confirmed at DF151)

_PID_BLOCKS = {
    "top":    100,   # HTR1 DF_Memory_Start
    "bottom": 125,   # HTR2
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
    "pv_raw": 11,
    "pv":     12,
}

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
    "cryostat":   Y_BASE + 103 - 1,   # Y103
    "primary_xe": Y_BASE + 102 - 1,   # Y102
    "ballast":    Y_BASE + 101 - 1,   # Y101
}

# PWM output coil addresses (for reading heater duty cycle state)
REG_HTR_COIL = {
    "top":    Y_BASE + 4 - 1,   # Y004
    "bottom": Y_BASE + 3 - 1,   # Y003
    "nozzle": Y_BASE + 2 - 1,   # Y002
}

CELSIUS_TO_KELVIN = 273.15


# ---------------------------------------------------------------------------
# Driver class
# ---------------------------------------------------------------------------

class PlcDriver(SensorDriver):
    NAME = "plc"

    def __init__(self, config, mqtt):
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
        # Most-recent value_c per sensor alias (rtd1..rtd6, tc1..tc4) for use
        # as identifiers in those expressions.
        self._sensor_c: Dict[str, float] = {}

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
        self._mqtt.subscribe(command_topic("pid", "+", "autotune"),        self._on_pid_autotune)
        self._mqtt.subscribe(command_topic("pid", "+", "controller_type"), self._on_pid_controller_type)
        self._mqtt.subscribe(command_topic("pid", "+", "pv"),              self._on_pid_pv_write)
        self._mqtt.subscribe(command_topic("pid", "+", "setpoint_expr"),   self._on_pid_setpoint_expr)
        self._mqtt.subscribe(command_topic("pid", "+", "pv_expr"),         self._on_pid_pv_expr_set)
        self._mqtt.subscribe(command_topic("valve", "+", "state"),         self._on_valve_state)
        self._mqtt.subscribe(command_topic("valve", "+", "auto_close"),    self._on_valve_auto)
        self._mqtt.subscribe(command_topic("valve", "+", "auto_open"),     self._on_valve_auto)
        self._mqtt.subscribe(command_topic("pv_interlock", "limits"),      self._on_pv_interlock_limits)
        # Publish the (just-loaded) limits + initial trip state so the
        # snapshot has a value before the first poll.
        self._publish_pv_interlock_status(force=True)
        # (Sensor aliases for setpoint/pv expressions — rtd1..6, tc1..4 — are
        # populated directly when the PlcDriver reads the PLC RTDs and when
        # the LabJack callbacks fire, avoiding any MQTT round-trip race.)

    def disconnect(self) -> None:
        if self._client:
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
            self._publish_rtds()
            self._publish_level()
            self._publish_pid_status()
            self._publish_valve_status()
            self._write_level_to_plc()
            self._write_pid_expressions()
            # MUST run after expressions: when the PV interlock trips, it
            # writes the safe-surrogate to the PID's pv_raw register, which
            # must override anything an active pv_expr would have written.
            self._write_labjack_to_plc()
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

    def _write_labjack_to_plc(self) -> None:
        """Mirror the latest LabJack temperatures into PLC DF registers (°C),
        AND enforce the PV safety interlocks that neutralize the heater PIDs
        when input data is bad.

        Normal path:
          RTD absolute → DF210-212 (the PLC's "LJ mirror" used by display
                                    and possibly by a ladder rung that
                                    forwards them to the PID block's pv_raw)
          TC gradient → DF213-216

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

        On trip, three redundant writes drive every PID output to 0%:
          (1) pv_raw (DF111/136/161) ← pv_safe_surrogate_k (500 K → 226.85 °C).
              If the PID's PV input is in-block pv_raw, the PID sees PV ≫ SP
              and computes output → 0.
          (2) sp     (DF100/125/151) ← sp_safe_surrogate_k (3 K → -270.15 °C).
              Independent of where the PID actually sources its PV, the error
              SP − PV stays large negative ⇒ output → 0.
          (3) output (DF108/133/158) ← 0. The third leg: when the CLICK PID
              instruction is in a frozen/non-computing state (e.g. autotune
              hung when PV stopped changing), it stops updating OUT_Control
              and our external write persists. This is the leg that catches
              the failure mode where (1) and (2) wouldn't help because the
              PID isn't recomputing at all.

        TC mirrors (DF213-216) get the stale check only — they're not used
        as a PID PV on this PLC. Write 0 on stale.

        Ordering note: this runs AFTER `_write_pid_expressions` in poll(), so
        the surrogate writes override any active pv_expr / sp_expr on trip.
        """
        plc_cfg = self._config.plc
        stale_s    = plc_cfg.pv_stale_s
        safe_k     = plc_cfg.pv_safe_surrogate_k
        sp_safe_k  = plc_cfg.sp_safe_surrogate_k
        safe_c     = safe_k - CELSIUS_TO_KELVIN
        sp_safe_c  = sp_safe_k - CELSIUS_TO_KELVIN
        with self._interlock_lock:
            pv_min_k = self._pv_min_k
            pv_max_k = self._pv_max_k
        pv_min_c   = pv_min_k - CELSIUS_TO_KELVIN
        pv_max_c   = pv_max_k - CELSIUS_TO_KELVIN
        now = time.monotonic()

        # ── Detection: per-channel trip reasons ─────────────────────────
        rtd_trip_reason: Dict[int, str] = {}
        for ch in REG_LABJACK_RTD_WRITE:
            val_c = self._labjack_rtd_c.get(ch)
            ts    = self._labjack_rtd_ts.get(ch)
            if ts is None or (now - ts) > stale_s:
                rtd_trip_reason[ch] = "stale"
            elif val_c is None:
                rtd_trip_reason[ch] = "no-value"
            elif val_c < pv_min_c:
                rtd_trip_reason[ch] = f"below band ({val_c:.1f} °C < {pv_min_c:.1f} °C)"
            elif val_c > pv_max_c:
                rtd_trip_reason[ch] = f"above band ({val_c:.1f} °C > {pv_max_c:.1f} °C)"

        # ── Edge-triggered per-channel logging + retained alerts ────────
        for ch in REG_LABJACK_RTD_WRITE:
            reason       = rtd_trip_reason.get(ch)
            was_tripped  = self._labjack_rtd_tripped.get(ch, False)
            if reason and not was_tripped:
                log.warning("[plc] LJ RTD%d interlock TRIPPED (%s): "
                            "forcing safe surrogates (PV→%.1f °C, SP→%.1f °C, "
                            "OUT→0%%) on all three zones",
                            ch, reason, safe_c, sp_safe_c)
                self._publish_pv_alert(ch, "rtd", reason,
                                       self._labjack_rtd_c.get(ch), safe_k)
                self._labjack_rtd_tripped[ch] = True
            elif (not reason) and was_tripped:
                log.info("[plc] LJ RTD%d interlock CLEARED — "
                         "value %.1f °C is fresh and within band",
                         ch, self._labjack_rtd_c.get(ch, float("nan")))
                self._clear_pv_alert(ch, "rtd")
                self._labjack_rtd_tripped[ch] = False

        # ── Action: write registers ─────────────────────────────────────
        tripped_now = bool(rtd_trip_reason)
        if tripped_now:
            for addr in REG_LABJACK_RTD_WRITE.values():
                self._write_float(addr, safe_c)
            for zone in ("top", "bottom", "nozzle"):
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

        # Publish the consolidated interlock status whenever the aggregate
        # trip state changes or the band limits changed (handler does that
        # explicitly; here we only publish on trip-edge to keep traffic low).
        if tripped_now != self._interlock_tripped_was:
            self._interlock_tripped_was = tripped_now
            self._publish_pv_interlock_status(tripped=tripped_now,
                                              reasons=rtd_trip_reason)

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

    def _publish_pid_status(self) -> None:
        # Note: the CLICK PID's internal Auto/Manual state is not visible over
        # Modbus on this PLC (see the comment near REG_PID_AUTOTUNE_START), so
        # no "mode" field is published. Operators drive Auto/Manual + manual
        # output via the CLICK Programming Software's PID Monitor.
        for zone in ("top", "bottom", "nozzle"):
            sp_c  = self._read_float(_pid_reg(zone, "sp"))
            pv_c  = self._read_float(_pid_reg(zone, "pv"))
            out   = self._read_float(_pid_reg(zone, "output"))
            kp    = self._read_float(_pid_reg(zone, "kp"))
            ki    = self._read_float(_pid_reg(zone, "ki"))
            kd    = self._read_float(_pid_reg(zone, "kd"))
            ctrl_bit = self._read_coil(REG_PID_CONTROLLER_PID[zone])
            if sp_c is None or pv_c is None:
                continue
            controller = "pid" if ctrl_bit else ("pi" if ctrl_bit is False else "unknown")
            self._mqtt.publish_status(
                "pid", zone,
                payload={
                    "setpoint_c":  round(sp_c, 3),
                    "setpoint_k":  round(sp_c + CELSIUS_TO_KELVIN, 3),
                    "pv_c":        round(pv_c, 3),
                    "pv_k":        round(pv_c + CELSIUS_TO_KELVIN, 3),
                    "output_pct":  round(out, 2) if out is not None else None,
                    "kp": kp, "ki": ki, "kd": kd,
                    "controller_type": controller,
                    "setpoint_expr": self._pid_sp_expr.get(zone, ""),
                    "pv_expr":       self._pid_pv_expr.get(zone, ""),
                },
            )

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
        same topic, so it benefits too)."""
        parts = topic.split("/")
        zone = parts[-2]
        if zone not in _PID_BLOCKS:
            log.warning("[plc] unknown PID zone: %s", zone)
            return
        value_k = payload.get("value_k")
        if value_k is None:
            return
        value_k = float(value_k)
        value_c = value_k - CELSIUS_TO_KELVIN
        self._pid_sp_expr[zone] = repr(value_k)
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
        """xsphere/commands/pid/{zone}/autotune  → trigger autotune (set coil 1)."""
        zone = topic.split("/")[-2]
        if zone not in _PID_BLOCKS:
            return
        ok = self._write_coil(REG_PID_AUTOTUNE_START[zone], True)
        log.info("[plc] PID %s autotune start: %s", zone, "OK" if ok else "FAIL")

    def _on_pid_controller_type(self, topic: str, payload: dict) -> None:
        """xsphere/commands/pid/{zone}/controller_type  → {"mode": "pi"|"pid"}."""
        zone = topic.split("/")[-2]
        if zone not in _PID_BLOCKS:
            return
        mode = str(payload.get("mode", "")).lower()
        if mode not in ("pi", "pid"):
            return
        ok = self._write_coil(REG_PID_CONTROLLER_PID[zone], mode == "pid")
        log.info("[plc] PID %s controller_type → %s: %s",
                 zone, mode, "OK" if ok else "FAIL")

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
        we subtract 273.15 before writing the DF, which is stored in °C."""
        for zone in ("top", "bottom", "nozzle"):
            for kind, slot in (("sp_expr", "sp"), ("pv_expr", "pv_raw")):
                expr = (self._pid_sp_expr if kind == "sp_expr" else self._pid_pv_expr).get(zone, "")
                if not expr:
                    continue
                v_k = self._eval_expr(expr)
                if v_k is None:
                    log.debug("[plc] PID %s %s eval(%r) → None (missing alias / read error)",
                              zone, kind, expr)
                    continue
                self._write_float(_pid_reg(zone, slot), float(v_k) - CELSIUS_TO_KELVIN)

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
