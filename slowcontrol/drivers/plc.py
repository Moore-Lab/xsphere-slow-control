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
import logging
import struct
import time
from typing import Dict, Optional, Tuple

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
# Read off the PID Monitor pages for HTR1/2/3.  Each PID has Manual/Auto mode
# bits, an "autotune start" trigger and a PI-vs-PID control type bit.
def _c(n: int) -> int:
    """Coil address for control relay C<n>."""
    return C_BASE + (n - 1)

REG_PID_MODE_MANUAL    = {"top": _c(115), "bottom": _c(155), "nozzle": _c(195)}
REG_PID_MODE_AUTO      = {"top": _c(116), "bottom": _c(156), "nozzle": _c(196)}
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
        # Cache latest level values received from ESP32 MQTT topics
        self._level_raw: Dict[str, float] = {}
        # Cache latest LabJack temperatures received over MQTT (°C), keyed by
        # channel number — RTD absolute, TC gradient (ΔT) — to mirror into the PLC
        self._labjack_rtd_c: Dict[int, float] = {}
        self._labjack_tc_delta_c: Dict[int, float] = {}
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

    def poll(self) -> None:
        if self._client is None:
            return
        try:
            self._publish_rtds()
            self._publish_level()
            self._publish_pid_status()
            self._publish_valve_status()
            self._write_level_to_plc()
            self._write_labjack_to_plc()
            self._write_pid_expressions()
        except ModbusException as exc:
            log.warning("[plc] Modbus error during poll: %s", exc)
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
        rr = self._client.read_holding_registers(address, count=2)
        if rr.isError():
            log.debug("[plc] read error at address %d", address)
            return None
        raw = (rr.registers[1] << 16) | rr.registers[0]
        return struct.unpack(">f", struct.pack(">I", raw))[0]

    def _read_int(self, address: int) -> Optional[int]:
        """Read a single 16-bit integer holding register."""
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
        result = self._client.write_registers(address, [lo, hi])
        return not result.isError()

    def _write_int(self, address: int, value: int) -> bool:
        """Write a single 16-bit integer to a holding register."""
        result = self._client.write_register(address, value)
        return not result.isError()

    def _read_coil(self, address: int) -> Optional[bool]:
        """Read a single coil/discrete control relay."""
        rr = self._client.read_coils(address, count=1)
        if rr.isError():
            return None
        return bool(rr.bits[0])

    def _write_coil(self, address: int, value: bool) -> bool:
        """Set or clear a single coil/control relay."""
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
            # expressions can reference it (no MQTT round-trip).
            self._sensor_c[f"rtd{path[-1]}"] = val_c
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
        """Mirror the latest LabJack temperatures into PLC DF registers (°C):
        RTD absolute → DF210-212, TC gradient (ΔT) → DF213-216."""
        for ch, addr in REG_LABJACK_RTD_WRITE.items():
            val = self._labjack_rtd_c.get(ch)
            if val is not None:
                self._write_float(addr, val)
        for ch, addr in REG_LABJACK_TC_WRITE.items():
            val = self._labjack_tc_delta_c.get(ch)
            if val is not None:
                self._write_float(addr, val)

    # ------------------------------------------------------------------
    # Publish: PID status
    # ------------------------------------------------------------------

    def _publish_pid_status(self) -> None:
        for zone in ("top", "bottom", "nozzle"):
            sp_c  = self._read_float(_pid_reg(zone, "sp"))
            pv_c  = self._read_float(_pid_reg(zone, "pv"))
            out   = self._read_float(_pid_reg(zone, "output"))
            kp    = self._read_float(_pid_reg(zone, "kp"))
            ki    = self._read_float(_pid_reg(zone, "ki"))
            kd    = self._read_float(_pid_reg(zone, "kd"))
            manual_bit = self._read_coil(REG_PID_MODE_MANUAL[zone])
            auto_bit   = self._read_coil(REG_PID_MODE_AUTO[zone])
            ctrl_bit   = self._read_coil(REG_PID_CONTROLLER_PID[zone])
            if sp_c is None or pv_c is None:
                continue
            mode = "manual" if manual_bit else ("auto" if auto_bit else "unknown")
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
                    "mode": mode,
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
        """Cache a LabJack RTD absolute temperature (°C) — for the PLC mirror
        (DF210-212) and as the `rtd<n>` alias usable in setpoint/pv expressions.
        Topic: xsphere/sensors/temperature/labjack/rtd/<n>  payload {"value_c": ...}"""
        if not isinstance(payload, dict):
            return
        val = payload.get("value_c")
        if val is None:
            return
        try:
            ch = int(topic.rsplit("/", 1)[-1])
            val = float(val)
        except (TypeError, ValueError):
            return
        self._labjack_rtd_c[ch] = val
        self._sensor_c[f"rtd{ch}"] = val

    def _on_labjack_tc(self, topic: str, payload: dict) -> None:
        """Cache a LabJack thermocouple absolute temperature (°C) under the
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
            except (TypeError, ValueError):
                pass
        val_c = payload.get("value_c")
        if val_c is not None:
            try:
                self._sensor_c[f"tc{ch}"] = float(val_c)
            except (TypeError, ValueError):
                pass

    def _on_pid_setpoint(self, topic: str, payload: dict) -> None:
        """xsphere/commands/pid/{zone}/setpoint  → {"value_k": X}"""
        parts = topic.split("/")
        zone = parts[-2]
        if zone not in _PID_BLOCKS:
            log.warning("[plc] unknown PID zone: %s", zone)
            return
        value_k = payload.get("value_k")
        if value_k is None:
            return
        value_c = float(value_k) - CELSIUS_TO_KELVIN
        addr = _pid_reg(zone, "sp")
        ok = self._write_float(addr, value_c)
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

    def _on_pid_mode(self, topic: str, payload: dict) -> None:
        """xsphere/commands/pid/{zone}/mode  → {"mode": "manual"|"auto"}.

        Sets the corresponding "request" coil (Cxxx5/6) high; the CLICK ladder
        clears the opposite bit as it changes mode."""
        zone = topic.split("/")[-2]
        if zone not in _PID_BLOCKS:
            return
        mode = str(payload.get("mode", "")).lower()
        if mode == "manual":
            ok = self._write_coil(REG_PID_MODE_MANUAL[zone], True)
        elif mode == "auto":
            ok = self._write_coil(REG_PID_MODE_AUTO[zone], True)
        else:
            log.warning("[plc] PID %s mode bad payload: %r", zone, payload)
            return
        log.info("[plc] PID %s mode → %s: %s", zone, mode, "OK" if ok else "FAIL")

    def _on_pid_output(self, topic: str, payload: dict) -> None:
        """xsphere/commands/pid/{zone}/output  → {"value_pct": X}.

        Writes the manual output register.  Effective in Manual mode; in Auto
        the PID overwrites it next scan."""
        zone = topic.split("/")[-2]
        if zone not in _PID_BLOCKS:
            return
        val = payload.get("value_pct")
        if val is None:
            return
        ok = self._write_float(_pid_reg(zone, "output"), float(val))
        log.info("[plc] PID %s manual output → %.2f %%: %s",
                 zone, float(val), "OK" if ok else "FAIL")

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

        Result is in °C (matching the underlying register convention)."""
        for zone in ("top", "bottom", "nozzle"):
            for kind, slot in (("sp_expr", "sp"), ("pv_expr", "pv_raw")):
                expr = (self._pid_sp_expr if kind == "sp_expr" else self._pid_pv_expr).get(zone, "")
                if not expr:
                    continue
                v = self._eval_expr(expr)
                if v is None:
                    log.debug("[plc] PID %s %s eval(%r) → None (missing alias / read error)",
                              zone, kind, expr)
                    continue
                self._write_float(_pid_reg(zone, slot), float(v))

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
