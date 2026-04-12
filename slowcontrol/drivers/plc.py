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


# --- RTD inputs (read-only, DF, from C0-04RTD module) ---
REG_RTD = {
    "rtd_cube_top":    _df(1),   # DF1  Xe cube top RTD (Pt100, °C)
    "rtd_cube_bottom": _df(2),   # DF2  Xe cube bottom RTD (Pt100, °C)
    "rtd_cube_nozzle": _df(3),   # DF3  Xe cube nozzle RTD (Pt100, °C)
    "rtd_ln_base":     _df(4),   # DF4  LN2 vessel base RTD (Pt1000, °C)
}

# MQTT sub-topics for RTD channels (matches topic schema)
RTD_MQTT_PATH = {
    "rtd_cube_top":    ("plc", "rtd", "1"),
    "rtd_cube_bottom": ("plc", "rtd", "2"),
    "rtd_cube_nozzle": ("plc", "rtd", "3"),
    "rtd_ln_base":     ("plc", "rtd", "4"),
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

        # Subscribe to command topics
        from slowcontrol.core.mqtt import command_topic
        self._mqtt.subscribe(
            command_topic("pid", "+", "setpoint"),
            self._on_pid_setpoint,
        )
        self._mqtt.subscribe(
            command_topic("pid", "+", "gains"),
            self._on_pid_gains,
        )
        self._mqtt.subscribe(
            command_topic("valve", "+", "state"),
            self._on_valve_state,
        )
        self._mqtt.subscribe(
            command_topic("valve", "+", "auto_close"),
            self._on_valve_auto,
        )
        self._mqtt.subscribe(
            command_topic("valve", "+", "auto_open"),
            self._on_valve_auto,
        )

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
        except ModbusException as exc:
            log.warning("[plc] Modbus error during poll: %s", exc)
        except Exception:
            log.exception("[plc] unexpected error during poll")

    # ------------------------------------------------------------------
    # Read helpers
    # ------------------------------------------------------------------

    def _read_float(self, address: int) -> Optional[float]:
        """Read a 32-bit IEEE 754 float from two consecutive holding registers."""
        rr = self._client.read_holding_registers(address, count=2)
        if rr.isError():
            log.debug("[plc] read error at address %d", address)
            return None
        raw = (rr.registers[0] << 16) | rr.registers[1]
        return struct.unpack(">f", struct.pack(">I", raw))[0]

    def _read_int(self, address: int) -> Optional[int]:
        """Read a single 16-bit integer holding register."""
        rr = self._client.read_holding_registers(address, count=1)
        if rr.isError():
            return None
        return rr.registers[0]

    def _write_float(self, address: int, value: float) -> bool:
        """Write a 32-bit float to two consecutive holding registers."""
        packed = struct.pack(">f", value)
        raw = struct.unpack(">I", packed)[0]
        hi = (raw >> 16) & 0xFFFF
        lo = raw & 0xFFFF
        result = self._client.write_registers(address, [hi, lo])
        return not result.isError()

    def _write_int(self, address: int, value: int) -> bool:
        """Write a single 16-bit integer to a holding register."""
        result = self._client.write_register(address, value)
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
            self._mqtt.publish_sensor(
                "temperature", *path,
                payload={"value_c": round(val_c, 3),
                         "value_k": round(val_k, 3)},
            )

    # ------------------------------------------------------------------
    # Publish: Level sensors
    # ------------------------------------------------------------------

    def _publish_level(self) -> None:
        for vessel, addr_raw in {
            "cryostat": REG_LEVEL_RAW["cryostat"],
        }.items():
            raw = self._read_float(addr_raw)
            filtered = self._read_float(REG_LEVEL_FILTERED[vessel])
            if raw is None:
                continue
            self._mqtt.publish_sensor(
                "level", vessel,
                payload={"raw": round(raw, 4),
                         "filtered": round(filtered, 4) if filtered else raw},
            )
        # ballast and primary_xe levels come from ESP32 via MQTT (_level_raw)
        # and are re-published by _write_level_to_plc after filtering.

    def _write_level_to_plc(self) -> None:
        """Forward latest ESP32 level readings into PLC DF251/DF252 so the
        PLC ladder's autofill decisions for XV1/XV2 remain current."""
        for vessel, addr in REG_LEVEL_WRITE.items():
            val = self._level_raw.get(vessel)
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
            if sp_c is None or pv_c is None:
                continue
            self._mqtt.publish_status(
                "pid", zone,
                payload={
                    "setpoint_c":  round(sp_c, 3),
                    "setpoint_k":  round(sp_c + CELSIUS_TO_KELVIN, 3),
                    "pv_c":        round(pv_c, 3),
                    "pv_k":        round(pv_c + CELSIUS_TO_KELVIN, 3),
                    "output_pct":  round(out, 2) if out is not None else None,
                    "kp": kp, "ki": ki, "kd": kd,
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
