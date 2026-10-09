"""
CLICK Plus PLC driver — Modbus TCP.

Reads all PLC-connected sensors (RTDs, level sensors, PID state, valve state)
and publishes them to MQTT under the xsphere/sensors/... and xsphere/status/...
topic hierarchy.

Also accepts write commands for:
  - PID setpoints (DF100, DF125, DF151)
  - PID gains (DF105-107, DF130-132, DF155-157)
  - Valve desired state (DS1002, DS1004, DS1006, DS1008)
  - Gas-side solenoid valve desired state (DS151, DS152, DS153)
  - Valve automation enables (DS1101-1106)
  - MKS mass flow controller setpoint (DF205) and valve mode (DS1010)
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
#                        Each DF occupies 2 consecutive holding registers.
#                        DF1 → 28672/28673, DF201 → 29072, DF205 → 29080.
#
#   Coils (FC1 read, FC5 write):
#     Y (output bits)  : address = Y_BASE + 32 * slot + (point - 1)
#                        !! The slot stride is 32, NOT the point count. !!
#                        Y001 → 8192, Y101 → 8224, Y103 → 8226.
#     C (control relay): address = 16384 + (C_number - 1)
#
#   Discrete Inputs (FC2 read):
#     X (input bits)   : address = 32 * slot + (point - 1)
#                        X001 → 0, X101 → 32
#
# Base addresses:
#   DS_BASE  = 0        (DS1 = HR address 0)
#   DF_BASE  = 28672    (DF1 = HR address 28672, 28673)
#   Y_BASE   = 8192     (Y001 = coil address 8192)
#   X_BASE   = 0        (X001 = discrete input address 0)
#   C_BASE   = 16384    (C1  = coil address 16384)
#
# FLOAT WORD ORDER — verify this on the bench before trusting any DF value.
#   CLICK stores 32-bit floats LOW WORD FIRST (little-endian word order,
#   big-endian bytes within each word).  AutomationDirect does not publish
#   this; it is the agreed behaviour of the MathWorks CLICK docs and the
#   numat/clickplc and mattj23/ClickPLC driver libraries, and CLICK's own
#   Send/Receive dialogs expose a "swap the order of the data" option — so a
#   mismatched project setting is possible.
#
#   One-time check: write 1.0 into DF1 from the CLICK software and read
#   registers 28672/28673.
#       low-word-first  → [0x0000, 0x3F80]   ← config float_word_order: low_first
#       high-word-first → [0x3F80, 0x0000]   ← config float_word_order: high_first
#
# AutomationDirect does not print the numeric Modbus table in C2-USERM at all;
# it directs you to the CLICK programming software → Program tab → Address
# Picker → tick "Display MODBUS Address".  Confirm DS1, DF1, DF205, Y101 and
# X101 there before writing to live hardware — a wrong base address silently
# reads a different memory block instead of raising an error.
#
# Operational limit: CLICK PLUS Com Port 1 serves at most 3 simultaneous
# Modbus TCP clients; the 4th connection is refused (C2-USERM p.4-19).  Keep
# this service to a single connection and close debug sessions.
#
# Reference: C2-USERM (CLICK PLUS Hardware User Manual), Ch.4 Modbus Addressing
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
C_BASE:  int = 16384    # C1   → coil 16384
SLOT_STRIDE: int = 32   # X/Y bit addresses advance 32 per I/O slot

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
    """Return the coil address for CLICK output bit Y{n}.

    CLICK numbers outputs as SSPP — slot digit(s) then a 2-digit point — but
    the Modbus bit addresses advance in blocks of 32 per slot, not by point
    count.  So Y101 (slot 1, point 1) is 8192 + 32 = 8224, not 8192 + 100.
    """
    slot, point = divmod(n, 100)
    return Y_BASE + SLOT_STRIDE * slot + (point - 1)


def _x(n: int) -> int:
    """Return the discrete-input address for CLICK input bit X{n}."""
    slot, point = divmod(n, 100)
    return X_BASE + SLOT_STRIDE * slot + (point - 1)


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

# Ladder-computed fill status, one integer per vessel that has one.
#
# The CLICK program (main program rungs 26/27) drives DS3 from the filtered
# cryostat level DF303 as a latching Schmitt trigger:
#
#     DF303 < 0.8  →  DS3 = 0   ("empty" — this vessel wants filling)
#     DF303 > 2.5  →  DS3 = 1   ("full")
#
# Between 0.8 and 2.5 neither rung fires and DS3 holds its previous value.
# That hysteresis is the point: DS3 is the ladder's own debounced answer to
# "does the cryostat want filling?", so it does not chatter the way a bare
# comparison against the noisy level does, and it is the exact term rung 28
# now uses to gate the XV3 auto-open and auto-close decisions.  Reading it
# back here means the GUI and the dashboard judge fill state from the same
# latch the valve logic uses, rather than re-deriving it from DF303 and
# disagreeing with the PLC at the edges.
REG_LEVEL_FILL_STATUS = {
    "cryostat":   _ds(3),    # DS3 cryostat fill status (0 = empty, 1 = full)
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
    # Coast permit: 0 = the Python service is deliberately withholding a
    # refill, anything else = permit granted.  Retentive with an initial value
    # of 1 in the ladder, so a PLC that has never been written to, or one
    # whose project predates the rung, behaves exactly as it does today.
    # See "Required CLICK ladder additions" item 6 in SYSTEM_ARCHITECTURE.md.
    "cryostat_coast_permit": _ds(1107),  # DS1107
    "primary_xe_auto_close": _ds(1103),  # DS1103
    "primary_xe_auto_open":  _ds(1104),  # DS1104
    "ballast_auto_close":    _ds(1101),  # DS1101
    "ballast_auto_open":     _ds(1102),  # DS1102
}

# Coil addresses for actual output state.
#
# The C0-08TR splits its 8 relay points into TWO isolated commons of four:
#   C1 → Y101–Y104   C2 → Y105–Y108
# Each common therefore has to serve exactly one voltage domain:
#   C1 → LN2 fill valves + the MKS bypass pilot
#   C2 → the three gas-side solenoid valves (ballast / pump / bottle)
#
# Y105 and Y106 were originally earmarked for the two MKS valve-override
# contacts, with C2 tied to the MFC signal common.  Those rungs were never
# entered and the points now switch solenoids, so the override contacts have
# no relay point assigned — and whatever they end up on, it cannot be C2.
REG_VALVE_COIL = {
    "cryostat":    _y(103),  # Y103  LN2 cryostat fill      (common C1)
    "primary_xe":  _y(102),  # Y102  LN2 primary Xe fill    (common C1)
    "ballast":     _y(101),  # Y101  LN2 ballast fill       (common C1)
    "bypass":      _y(104),  # Y104  MKS bypass pilot       (common C1)
    "gas_ballast": _y(105),  # Y105  valve on the ballast   (common C2)
    "gas_pump":    _y(106),  # Y106  valve on the pump      (common C2)
    "gas_bottle":  _y(107),  # Y107  valve on the bottle    (common C2)
}

# --- Gas-side solenoid valves (direct relay command) ----------------------
#
#  Each valve is one pair of ladder rungs:
#
#      DS15x == 1  →  SET Y10x      (open)
#      DS15x != 1  →  RST Y10x      (closed)
#
#  so the DS register is the whole interface.  Unlike XV1–XV3 there is no
#  present-state register, no X-input feedback, no auto-open / auto-close and
#  no timer behind these — the relay follows the register and nothing else.
#  The readback published as `state` is therefore the Y output coil itself,
#  from REG_VALVE_COIL.
#
#  The MQTT names carry a gas_ prefix because `ballast` already means XV1,
#  the LN2 fill valve for the ballast bottle's cryoflask.
REG_SOLENOID_DESIRED = {
    "gas_ballast": _ds(151),   # DS151 → Y105
    "gas_pump":    _ds(152),   # DS152 → Y106
    "gas_bottle":  _ds(153),   # DS153 → Y107
}

# Registering them as `{name}_desired` is what lets the ordinary
# xsphere/commands/valve/{name}/state handler command them.
REG_VALVE.update({f"{name}_desired": addr
                  for name, addr in REG_SOLENOID_DESIRED.items()})

# --- Gas flow: MKS M330B mass flow controller + pneumatic bypass ---------
#
#  Analog (Slot0, C2-08D2-6V):
#    DF201  AI ch1  ← MKS flow signal output, volts as read by the ADC
#    DF205  AO ch1  → MKS setpoint input, volts commanded to the DAC
#
#  Discrete (DS integers, decoded to relays by the ladder):
#    DS1007 bypass valve present state   (read)   0=closed 1=open
#    DS1008 bypass valve desired state   (write)  0=closed 1=open
#    DS1009 MKS valve present mode       (read)   0=closed 1=normal 2=open
#    DS1010 MKS valve desired mode       (write)  0=closed 1=normal 2=open
#
#  The MKS valve override is commanded as ONE integer rather than two
#  independent open/close bits.  The ladder decodes it into a valve-open and
#  a valve-close contact with a mutual-exclusion rung.  (Not entered yet, and
#  the outputs are unassigned: Y105/Y106 went to the gas-side solenoids.)
#
#  On the 15-pin M330B the override is active-LOW: a dry contact shorting
#  pin 4 to signal common forces the valve open, pin 3 to signal common
#  forces it closed, and BOTH pins floating is the normal setpoint-following
#  mode.  Asserting both is documented and non-destructive — but the MFC
#  resolves it as VALVE OPEN, which is the wrong way to fail.  Writing one
#  integer means "normal" is unambiguously both-relays-open, and no software
#  fault can produce a surprise purge.
#
#    DS1099 watchdog counter (write) — incremented by this driver on every
#           poll.  The ladder must force DS1010 = 0 (valve closed) and
#           DF205 = 0 V if this value stops changing; otherwise a crashed
#           Python service would leave the MFC flowing indefinitely.

REG_MKS_FLOW_V     = _df(201)   # DF201  flow signal input (volts)
REG_MKS_SETPOINT_V = _df(205)   # DF205  setpoint output  (volts)
REG_WATCHDOG       = _ds(1099)  # DS1099 service liveness counter

REG_VALVE.update({
    "bypass_state":   _ds(1007),
    "bypass_desired": _ds(1008),
    "mks_state":      _ds(1009),
    "mks_desired":    _ds(1010),
})

# MKS valve override modes (must match the ladder's decode rungs).
# See the 15-pin wiring table in SYSTEM_ARCHITECTURE.md §3.1c.
MKS_MODE_CLOSED = 0   # pin 3 shorted to signal common: valve driven shut
MKS_MODE_NORMAL = 1   # pins 3 and 4 both floating: MFC follows the setpoint
MKS_MODE_OPEN   = 2   # pin 4 shorted to signal common: valve forced open

MKS_MODE_NAMES = {
    MKS_MODE_CLOSED: "closed",
    MKS_MODE_NORMAL: "normal",
    MKS_MODE_OPEN:   "open",
}
MKS_MODE_VALUES = {v: k for k, v in MKS_MODE_NAMES.items()}

# PWM output coil addresses (for reading heater duty cycle state)
REG_HTR_COIL = {
    "top":    _y(4),   # Y004
    "bottom": _y(3),   # Y003
    "nozzle": _y(2),   # Y002
}

CELSIUS_TO_KELVIN = 273.15

# Re-warn about unreadable gas flow registers at most this often (seconds).
GASFLOW_WARN_INTERVAL_S = 60.0


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
        # Rolling counter written to the PLC so the ladder can detect that
        # this service has stopped and fail the MFC closed.
        self._wd_count: int = 0
        # Gas flow read health, for throttling the unreadable-registers warning.
        self._gf_healthy: Optional[bool] = None
        self._gf_warned_at: float = 0.0

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
        # Same handler: it keys off f"{vessel}_{mode}" in REG_VALVE, so
        # coast_permit resolves to DS1107 for the cryostat and is ignored for
        # vessels that have no such register.
        self._mqtt.subscribe(
            command_topic("valve", "+", "coast_permit"),
            self._on_valve_auto,
        )
        self._mqtt.subscribe(
            command_topic("valve", "mks", "mode"),
            self._on_mks_mode,
        )
        self._mqtt.subscribe(
            command_topic("flow", "mks", "setpoint_v"),
            self._on_mks_setpoint_v,
        )

    def disconnect(self) -> None:
        if self._client:
            self._release_coast_permits()
            self._client.close()
            self._client = None

    def _release_coast_permits(self) -> None:
        """Hand the coast permit back to the ladder on a clean shutdown.

        DS1107 is retentive, so a 0 left behind by a coasting service would
        keep the ladder's backup fill inhibited until something wrote to it
        again.  Done here as a direct register write rather than from the
        autovalve controller, because a controller publishing an MQTT command
        during shutdown is racing its own broker round-trip against
        `mqtt.disconnect()`.

        An unclean death is covered by the ladder itself: the watchdog-stale
        branch of ladder addition 6 restores autonomous filling within ~10 s.
        """
        for key, addr in REG_VALVE.items():
            if not key.endswith("_coast_permit"):
                continue
            if not self._write_int(addr, 1):
                log.error("[plc] failed to release coast permit %s — the "
                          "ladder may stay inhibited until the watchdog-stale "
                          "branch takes over", key)

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

            gf = self._config.gasflow
            if gf.enabled:
                # The bypass is a plain pneumatic valve with no MKS hardware
                # involved, so its readback must survive the MFC being
                # disabled — otherwise the dashboard keeps a stale retained
                # position for a valve the operator can still actuate.
                self._publish_bypass()

                # The watchdog exists solely so the ladder can fail the MFC
                # closed.  When the MFC is disabled — not wired yet, or out
                # for service — the counter must be allowed to stall, or the
                # service is telling the ladder it is alive and in control of
                # hardware it is not even reading.
                if gf.mks.enabled:
                    self._report_gasflow_health(self._publish_mks())

            # Last on purpose: these coil reads are the only FC1 traffic in
            # the driver, and a problem with them must not be able to starve
            # the watchdog kick above.
            self._publish_solenoid_valves()
        except ModbusException as exc:
            log.warning("[plc] Modbus error during poll: %s", exc)
        except Exception:
            log.exception("[plc] unexpected error during poll")

    # ------------------------------------------------------------------
    # Read helpers
    # ------------------------------------------------------------------

    def _low_word_first(self) -> bool:
        return self._config.plc.float_word_order.lower() != "high_first"

    def _read_float(self, address: int) -> Optional[float]:
        """Read a 32-bit IEEE 754 float from two consecutive holding registers.

        CLICK stores the two words low-word-first by default; see the word
        order note in the module docstring for the one-time bench check.
        """
        rr = self._client.read_holding_registers(address, count=2)
        if rr.isError():
            log.debug("[plc] read error at address %d", address)
            return None
        lo, hi = ((rr.registers[0], rr.registers[1])
                  if self._low_word_first()
                  else (rr.registers[1], rr.registers[0]))
        raw = (hi << 16) | lo
        return struct.unpack(">f", struct.pack(">I", raw))[0]

    def _read_int(self, address: int) -> Optional[int]:
        """Read a single 16-bit integer holding register."""
        rr = self._client.read_holding_registers(address, count=1)
        if rr.isError():
            return None
        return rr.registers[0]

    def _read_coil(self, address: int) -> Optional[int]:
        """Read a single coil (a Y output or C relay) as 0/1."""
        rr = self._client.read_coils(address, count=1)
        if rr.isError():
            log.debug("[plc] coil read error at address %d", address)
            return None
        return int(rr.bits[0])

    def _write_float(self, address: int, value: float) -> bool:
        """Write a 32-bit float to two consecutive holding registers."""
        raw = struct.unpack(">I", struct.pack(">f", value))[0]
        hi = (raw >> 16) & 0xFFFF
        lo = raw & 0xFFFF
        words = [lo, hi] if self._low_word_first() else [hi, lo]
        result = self._client.write_registers(address, words)
        return not result.isError()

    def _write_int(self, address: int, value: int) -> bool:
        """Write a single 16-bit integer to a holding register.

        Clamped to the signed 16-bit range a CLICK DS register holds — an
        out-of-range value would otherwise be rejected by pymodbus (or wrap)
        depending on version, and some of these writes originate from MQTT
        payloads we do not control.
        """
        value = max(-32768, min(int(value), 32767))
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
            payload = {
                "raw": round(raw, 4),
                # `if filtered is not None`, not a truth test: a filtered
                # value of exactly 0.0 is the empty vessel, which is the one
                # reading the coast gate cares most about, and falling back to
                # the unfiltered value there would feed it sensor noise.
                "filtered": (round(filtered, 4)
                             if filtered is not None else raw),
            }
            # Ladder fill-status latch, where the ladder publishes one.  Left
            # absent rather than defaulted when the read fails, so a consumer
            # can tell "PLC says full" from "we could not ask the PLC".
            status_addr = REG_LEVEL_FILL_STATUS.get(vessel)
            if status_addr is not None:
                fill_status = self._read_int(status_addr)
                if fill_status is not None:
                    payload["fill_status"] = fill_status
            self._mqtt.publish_sensor("level", vessel, payload=payload)
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
            payload = {
                "state":      state,
                "desired":    desired,
                "auto_close": ac,
                "auto_open":  ao,
            }
            # Read the coast permit back so the operator can tell a permit
            # that was written from one that actually landed. Absent for
            # vessels with no coast register.
            permit_addr = REG_VALVE.get(f"{vessel}_coast_permit")
            if permit_addr is not None:
                payload["coast_permit"] = self._read_int(permit_addr)
            self._mqtt.publish_status("valve", vessel, payload=payload)

    def _publish_solenoid_valves(self) -> None:
        """Publish the directly commanded gas-side solenoid valves.

        `desired` is the DS register the operator writes; `state` is the Y
        output the ladder SET/RSTs from it.  `state` is left absent rather
        than inferred from `desired` when the coil cannot be read, so a
        consumer never mistakes the command for the relay's answer.
        """
        for name, desired_addr in REG_SOLENOID_DESIRED.items():
            desired = self._read_int(desired_addr)
            state = self._read_coil(REG_VALVE_COIL[name])
            if desired is None and state is None:
                continue
            payload = {"desired": desired}
            if state is not None:
                payload["state"] = state
            self._mqtt.publish_status("valve", name, payload=payload)

    # ------------------------------------------------------------------
    # Publish: MKS mass flow controller + bypass valve
    # ------------------------------------------------------------------

    def _publish_bypass(self) -> None:
        """Publish the pneumatic bypass valve state.

        Separate from the MFC read because the bypass has nothing to do with
        the MKS hardware and must stay observable when the MFC is disabled.
        """
        state   = self._read_int(REG_VALVE["bypass_state"])
        desired = self._read_int(REG_VALVE["bypass_desired"])
        if state is not None:
            self._mqtt.publish_status(
                "valve", "bypass",
                payload={"state": state, "desired": desired},
            )

    def _report_gasflow_health(self, healthy: bool) -> None:
        """Warn when the MFC registers cannot be read, without spamming.

        The condition is usually not transient — an unwired analog channel
        stays unwired — so an unthrottled warning at the poll rate would bury
        every other message in the journal.
        """
        if healthy:
            if self._gf_healthy is False:
                log.info("[plc] gas flow registers readable again")
            self._gf_healthy = True
            return

        now = time.monotonic()
        first = self._gf_healthy is not False
        if first or now - self._gf_warned_at >= GASFLOW_WARN_INTERVAL_S:
            log.warning("[plc] gas flow registers unreadable — withholding "
                        "watchdog kick so the ladder fails the MFC closed")
            self._gf_warned_at = now
        self._gf_healthy = False

    def _publish_mks(self) -> bool:
        """Read the MFC flow signal and valve mode, convert the raw
        analog voltage into engineering units, and publish.

        The MKS analog interface is ratiometric — 0 V is zero flow and
        `signal_span_v` is 100% of the device's full scale in its calibration
        gas.  Xenon flow is the calibration-gas flow scaled by the gas
        correction factor.

        Returns True when both the flow signal and the MKS valve mode were
        read successfully, and pets the PLC watchdog in that case — a driver
        that has gone blind must stop asserting liveness.
        """
        mks_cfg = self._config.gasflow.mks

        raw_v = self._read_float(REG_MKS_FLOW_V)
        if raw_v is not None:
            span = mks_cfg.signal_span_v or 5.0
            volts = raw_v * mks_cfg.adc_gain + mks_cfg.adc_offset_v
            pct = (volts / span) * 100.0
            sccm_cal = (volts / span) * mks_cfg.full_scale_sccm
            sccm_act = sccm_cal * mks_cfg.gas_correction_factor

            # MKS analog Mass-Flo units park their flow output near +7 V for
            # a minute or two after power-up while the sensor heaters settle.
            # Flag it rather than reporting a wild flow number as if it were
            # real — the reading is meaningless until the MFC has warmed up.
            warming = volts > span * 1.1

            self._mqtt.publish_sensor(
                "flow", "mks",
                payload={
                    "raw_v":          round(raw_v, 5),
                    "value_v":        round(volts, 5),
                    "percent_fs":     round(pct, 3),
                    "value_sccm":     round(sccm_act, 3),
                    "value_sccm_cal": round(sccm_cal, 3),
                    "cal_gas":        mks_cfg.gas_name,
                    "gcf":            mks_cfg.gas_correction_factor,
                    "full_scale_sccm": mks_cfg.full_scale_sccm,
                    "over_range":     warming,
                },
            )
            if warming:
                log.warning("[plc] MKS flow signal at %.2f V (> %.2f V full "
                            "scale) — MFC still warming up or miswired",
                            volts, span)

        # Commanded setpoint, read back from the analog output register so the
        # dashboard shows what the PLC is actually driving, not what we last
        # asked for.
        sp_v = self._read_float(REG_MKS_SETPOINT_V)

        mode    = self._read_int(REG_VALVE["mks_state"])
        desired = self._read_int(REG_VALVE["mks_desired"])
        if mode is not None:
            payload = {
                "mode":         MKS_MODE_NAMES.get(mode, f"unknown({mode})"),
                "mode_int":     mode,
                "desired":      MKS_MODE_NAMES.get(desired, None) if desired is not None else None,
                "desired_int":  desired,
            }
            if sp_v is not None:
                span = mks_cfg.signal_span_v or 5.0
                # Undo the DAC calibration so the reported setpoint is the
                # voltage actually presented to the MFC, not the register value.
                gain = mks_cfg.dac_gain or 1.0
                applied_v = (sp_v - mks_cfg.dac_offset_v) / gain
                payload["setpoint_register_v"] = round(sp_v, 5)
                payload["setpoint_v"] = round(applied_v, 5)
                payload["setpoint_percent"] = round((applied_v / span) * 100.0, 3)
                payload["setpoint_sccm"] = round(
                    (applied_v / span) * mks_cfg.full_scale_sccm
                    * mks_cfg.gas_correction_factor, 3)
            self._mqtt.publish_status("valve", "mks", payload=payload)

        healthy = raw_v is not None and mode is not None
        if healthy:
            self._kick_watchdog()
        return healthy

    def _kick_watchdog(self) -> None:
        """Increment the PLC-side liveness counter.

        The ladder watches this register; if it stops advancing the PLC drives
        the MFC setpoint to 0 V and forces the valve override to CLOSED.  That
        way a crashed or disconnected slow-control service cannot leave xenon
        flowing.

        Modulo 30000 keeps the value inside the signed 16-bit range a CLICK DS
        register holds, so the ladder's change-detection never sees a wrap into
        negative territory.
        """
        self._wd_count = (self._wd_count + 1) % 30000
        if not self._write_int(REG_WATCHDOG, self._wd_count):
            # Worth an error rather than a debug line: a silently failing
            # watchdog write is indistinguishable from a working one until the
            # ladder shuts the MFC unexpectedly.
            log.error("[plc] watchdog write to DS1099 failed — the ladder "
                      "will fail the MFC closed if this persists")

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
        if vessel == "mks":
            # The MKS valve is tri-state (closed / normal / open), so it is
            # commanded through .../valve/mks/mode instead. Refuse a boolean
            # here rather than silently writing 0/1 into the mode register.
            log.warning("[plc] use commands/valve/mks/mode for the MKS valve, "
                        "not .../state")
            return
        key = f"{vessel}_desired"
        if key not in REG_VALVE:
            log.warning("[plc] unknown vessel: %s", vessel)
            return
        # Coerce explicitly rather than int()-ing whatever arrived: this
        # payload comes off the MQTT bus and ends up commanding a valve.
        raw = payload.get("state", 0)
        if raw in (1, True, "1", "on", "open", "true"):
            state = 1
        elif raw in (0, False, "0", "off", "closed", "close", "false"):
            state = 0
        else:
            log.warning("[plc] valve %s: uninterpretable state %r — ignored",
                        vessel, raw)
            return
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

    def _on_mks_mode(self, topic: str, payload: dict) -> None:
        """xsphere/commands/valve/mks/mode → {"mode": "closed"|"normal"|"open"}

        Writing a single integer keeps the two override contacts — pin 4
        (open/purge) and pin 3 (close), each active-low to signal common —
        mutually exclusive by construction.  The ladder decodes this value and
        can only ever assert one of them, so an accidental both-asserted state
        (which the MFC resolves as valve OPEN) is unreachable.

        "purge" is accepted as a synonym for "open" because that is what the
        dashboard button is labelled and what the mode actually does.
        """
        mode = payload.get("mode")
        if isinstance(mode, str):
            name = mode.strip().lower()
            if name == "purge":
                name = "open"
            value = MKS_MODE_VALUES.get(name)
        elif isinstance(mode, (int, float)) and not isinstance(mode, bool):
            # bool is a subclass of int, so True would otherwise sail through
            # as mode 1 (normal) and quietly release the valve override.
            value = int(mode) if int(mode) in MKS_MODE_NAMES else None
        else:
            value = None

        if value is None:
            log.warning("[plc] invalid MKS valve mode: %r (expected one of %s)",
                        mode, sorted(MKS_MODE_VALUES))
            return

        ok = self._write_int(REG_VALVE["mks_desired"], value)
        log.info("[plc] MKS valve mode → %s (%d): %s",
                 MKS_MODE_NAMES[value], value, "OK" if ok else "FAIL")

    def _on_mks_setpoint_v(self, topic: str, payload: dict) -> None:
        """xsphere/commands/flow/mks/setpoint_v → {"volts": X}

        Raw analog setpoint in volts.  Engineering-unit setpoints (sccm or
        % of full scale) are converted and range-checked by the gas flow
        controller, which then publishes here — this handler deliberately does
        no unit maths so there is exactly one place that owns the conversion.
        """
        volts = payload.get("volts")
        if volts is None:
            return
        try:
            volts = float(volts)
        except (TypeError, ValueError):
            log.warning("[plc] non-numeric MKS setpoint: %r", volts)
            return

        mks_cfg = self._config.gasflow.mks
        span = mks_cfg.signal_span_v or 5.0
        if not (0.0 <= volts <= span):
            log.warning("[plc] MKS setpoint %.4f V outside 0–%.2f V — rejected",
                        volts, span)
            return

        # Pre-distort by the DAC calibration so the MFC sees the voltage asked
        # for, then clamp to the module's own 0–10 V output range.
        register_v = volts * mks_cfg.dac_gain + mks_cfg.dac_offset_v
        register_v = max(0.0, min(register_v, 10.0))

        ok = self._write_float(REG_MKS_SETPOINT_V, register_v)
        log.info("[plc] MKS setpoint → %.4f V (register %.4f V): %s",
                 volts, register_v, "OK" if ok else "FAIL")
