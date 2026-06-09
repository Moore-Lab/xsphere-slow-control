"""
Omega RDXL6SD-USB temperature logger driver.

Modbus RTU over USB-serial.  Protocol per the RDXL6SD-USB Modbus
programming manual (Omega document, Sept 2025):

  57600 baud, 8N2, slave address 1, reply delay ≤ 20 ms.
  Supported function codes: 0x03 (holding) and 0x04 (input registers).

Register addresses we use:

  0x1000  Unfiltered temperature   Int32  × 6   raw / 10 → temperature
                                                in the *device's* unit (see 0x106E)
  0x1068  Channel type             UInt16 × 6   0-6  = thermocouple (K..S)
                                                7-14 = PT100/200/500/1000 (2/3 wire)
  0x106E  Units                    UInt16 × 1   0 = °C, 1 = °F

We use the *unfiltered* register (0x1000), not the Average register
(0x1044) — the latter is the meter's running average since reset and
gets pulled by transients (e.g. cold-start spikes), so it doesn't
match what the meter's display shows.  Unfiltered = instantaneous,
matches the display, which is what an operator expects.

We read the Units and Channel-type registers every poll so the driver
auto-detects °C↔°F changes made via the meter's front-panel SETUP menu
and reports the wire type the meter currently has each channel
configured for.  Disconnected channels return implausibly large values
which trip |°C| > 500 ⇒ {"fault": true}.

MQTT topics published, matching the xsphere temperature schema:
  xsphere/sensors/temperature/omega/tc/<n>   payload {value_k, value_c,
                                                       channel, label, fault}
  xsphere/sensors/temperature/omega/rtd/<n>  (same payload shape)
  xsphere/status/omega                       {connected, port, error,
                                              last_read_utc, poll_interval_s}

Config (config.yaml, ``omega:`` block; see OmegaConfig in core/config.py
for the full set of knobs and defaults).
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional

from slowcontrol.drivers.base import SensorDriver

log = logging.getLogger(__name__)

try:
    from pymodbus.client import ModbusSerialClient
    from pymodbus.exceptions import ModbusException
    _PYMODBUS_OK = True
except ImportError:                                                     # pragma: no cover
    _PYMODBUS_OK = False
    log.warning("pymodbus not installed; Omega driver disabled")


CELSIUS_TO_KELVIN = 273.15

# Number of channels the RDXL6SD reports per read (TC + RTD inputs)
_N_CHANNELS = 6

# A disconnected channel returns an implausibly large value; |°C| > this
# is treated as fault. Set generously since the encoding is Int32.
_FAULT_TEMP_C = 500.0

# Register addresses (see RDXL6SD-USB Modbus manual).
_REG_UNFILT_TEMP = 0x1000   # Int32 × 6  (instantaneous, matches the meter's display)
_REG_CHAN_TYPE   = 0x1068   # UInt16 × 6
_REG_UNITS       = 0x106E   # UInt16 × 1 (0 = °C, 1 = °F)

# Channel-type codes from the manual.  We collapse them to "tc" or "rtd"
# (the wire-type detail — K vs T, 2-wire vs 3-wire — is preserved as a
# label so it appears in the snapshot).
_CHAN_TYPE_NAMES = {
    0: ("tc",  "Type K"), 1: ("tc",  "Type J"), 2: ("tc",  "Type T"),
    3: ("tc",  "Type N"), 4: ("tc",  "Type E"), 5: ("tc",  "Type R"),
    6: ("tc",  "Type S"),
    7: ("rtd", "PT100 2-wire"),  8:  ("rtd", "PT200 2-wire"),
    9: ("rtd", "PT500 2-wire"),  10: ("rtd", "PT1000 2-wire"),
    11:("rtd", "PT100 3-wire"),  12: ("rtd", "PT200 3-wire"),
    13:("rtd", "PT500 3-wire"),  14: ("rtd", "PT1000 3-wire"),
}


class OmegaDriver(SensorDriver):
    NAME = "omega"

    def __init__(self, config, mqtt):
        super().__init__(config, mqtt)
        self._cfg = config.omega
        self._client: Optional["ModbusSerialClient"] = None
        # Serial port is single-threaded by nature, but we lock it to be safe
        # if a command handler is ever added that reaches the device too.
        self._lock = threading.Lock()
        self._last_read_utc: Optional[str] = None
        self._last_error: Optional[str] = None
        # Most recent meter configuration as read from 0x106E / 0x1068.
        # Updated every poll, published in xsphere/status/omega.
        self._device_units: Optional[str] = None             # "C" or "F"
        self._device_chan_types: List[str] = []              # per-channel "Type K" / "PT100 3-wire" / "unknown" / "unconfigured"
        self._device_chan_kinds: List[str] = []              # per-channel "tc" / "rtd" / "unknown"

    @property
    def poll_interval(self) -> float:
        return self._cfg.poll_interval_s

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def connect(self) -> None:
        if not _PYMODBUS_OK:
            raise RuntimeError("pymodbus is not installed")
        if not self._cfg.enabled:
            raise RuntimeError("omega.enabled = false in config")
        if not self._cfg.channels:
            raise RuntimeError("omega.channels is empty — no channels to read")
        c = ModbusSerialClient(
            port=self._cfg.port,
            baudrate=self._cfg.baud_rate,
            bytesize=8, parity="N", stopbits=self._cfg.stop_bits,
            timeout=self._cfg.timeout_s,
        )
        if not c.connect():
            raise ConnectionError(f"Cannot open serial port {self._cfg.port}")
        self._client = c
        log.info("[omega] connected on %s @ %d baud, slave=%d, channels=%d",
                 self._cfg.port, self._cfg.baud_rate, self._cfg.modbus_address,
                 len(self._cfg.channels))
        self._publish_status()

    def disconnect(self) -> None:
        with self._lock:
            if self._client is not None:
                try: self._client.close()
                except Exception: pass
                self._client = None
        self._publish_status()

    def _ensure_modbus(self) -> bool:
        if self._client is not None:
            return True
        try:
            self.connect()
            return True
        except Exception as exc:
            self._last_error = str(exc)
            log.warning("[omega] reconnect failed: %s", exc)
            return False

    # ------------------------------------------------------------------
    # Poll
    # ------------------------------------------------------------------

    def poll(self) -> None:
        if not self._ensure_modbus():
            self._publish_status()
            return
        try:
            self._do_poll()
            self._last_read_utc = datetime.now(timezone.utc).isoformat()
            self._last_error = None
        except (ModbusException, OSError, ConnectionError) as exc:
            log.warning("[omega] poll error — will reconnect: %s", exc)
            self._last_error = str(exc)
            with self._lock:
                if self._client is not None:
                    try: self._client.close()
                    except Exception: pass
                    self._client = None
        except Exception:
            log.exception("[omega] unexpected error in poll")
        self._publish_status()

    def _read_input_regs(self, addr: int, count: int) -> List[int]:
        """Read `count` input registers (FC=4); transparent across pymodbus
        3.x kwarg variants (device_id / slave / unit)."""
        with self._lock:
            last_exc: Optional[Exception] = None
            rr = None
            for slave_kw in ("device_id", "slave", "unit"):
                try:
                    rr = self._client.read_input_registers(
                        addr, count=count, **{slave_kw: self._cfg.modbus_address})
                    break
                except TypeError as exc:
                    last_exc = exc
            if rr is None:
                raise ModbusException(f"no compatible read_input_registers ({last_exc})")
        if rr.isError():
            raise ModbusException(f"read_input_registers @ {addr:#06x}: {rr}")
        return list(rr.registers)

    def _do_poll(self) -> None:
        # 1) Temperatures — 6 channels × Int32 spanning 12 registers from
        #    0x1000 (Unfiltered = instantaneous, matches the meter's
        #    display), low-word-first, big-endian within each word.
        #    Raw / 10 is the temperature in *whatever unit the meter is set
        #    to* (see 0x106E read below).
        regs = self._read_input_regs(_REG_UNFILT_TEMP, _N_CHANNELS * 2)
        raw_temps: List[float] = []
        for i in range(_N_CHANNELS):
            lo, hi = regs[2 * i], regs[2 * i + 1]
            raw32 = (hi << 16) | lo
            if raw32 & 0x80000000:                  # sign-extend
                raw32 -= 0x100000000
            raw_temps.append(raw32 / 10.0)

        # 2) Config block — channel types (6 × UInt16 at 0x1068) followed
        #    immediately by the units register (1 × UInt16 at 0x106E). One
        #    read of 7 registers covers both.
        cfg_regs = self._read_input_regs(_REG_CHAN_TYPE, _N_CHANNELS + 1)
        chan_codes = cfg_regs[:_N_CHANNELS]
        units_code = cfg_regs[_N_CHANNELS]
        self._device_units = "F" if units_code == 1 else "C"
        self._device_chan_kinds = []
        self._device_chan_types = []
        for code in chan_codes:
            kind, name = _CHAN_TYPE_NAMES.get(code, ("unknown", f"unknown ({code})"))
            self._device_chan_kinds.append(kind)
            self._device_chan_types.append(name)

        # 3) Convert each raw temperature to °C using whichever unit the
        #    meter is currently configured in.
        if self._device_units == "F":
            temps_c = [(t - 32.0) * 5.0 / 9.0 for t in raw_temps]
        else:
            temps_c = list(raw_temps)

        # 4) Publish per configured channel. The MQTT subpath ("tc" / "rtd")
        #    uses the meter-reported kind, NOT the operator-set config —
        #    matches reality if someone changes channel type on the device.
        tc_idx, rtd_idx = 0, 0
        for ch_cfg in self._cfg.channels:
            ch    = ch_cfg["ch"]
            label = ch_cfg.get("label", f"ch{ch}")
            if not (1 <= ch <= _N_CHANNELS):
                log.warning("[omega] channel %r out of range 1-%d, skipping",
                            ch, _N_CHANNELS)
                continue
            kind = self._device_chan_kinds[ch - 1]
            if kind == "tc":
                tc_idx += 1; sub_idx = tc_idx
            elif kind == "rtd":
                rtd_idx += 1; sub_idx = rtd_idx
            else:
                log.warning("[omega] channel %d device-reported kind %r — skipping",
                            ch, self._device_chan_types[ch - 1])
                continue

            t_c = temps_c[ch - 1]
            payload: Dict[str, object] = {
                "channel": sub_idx,
                "label":   label,
                "device_type": self._device_chan_types[ch - 1],
            }
            if abs(t_c) > _FAULT_TEMP_C:
                payload["fault"] = True
            else:
                payload["fault"]   = False
                payload["value_c"] = round(t_c, 2)
                payload["value_k"] = round(t_c + CELSIUS_TO_KELVIN, 2)
            self._mqtt.publish_sensor(
                "temperature", "omega", kind, str(sub_idx), payload=payload,
            )

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------

    def _publish_status(self) -> None:
        self._mqtt.publish_status(
            "omega",
            payload={
                "connected":       self._client is not None,
                "port":            self._cfg.port,
                "error":           self._last_error,
                "last_read_utc":   self._last_read_utc,
                "poll_interval_s": self._cfg.poll_interval_s,
                "device_units":    self._device_units,
                "channel_types":   list(self._device_chan_types),
            },
        )
