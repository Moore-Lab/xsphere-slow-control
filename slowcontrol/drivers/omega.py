"""
Omega RDXL6SD-USB temperature logger driver.

Modbus RTU over USB-serial. Protocol cribbed from a working logger
(github.com/Moore-Lab/RDXL6SD-temperature-logger):

  57600 baud, 8N2, slave address 1 by default.
  Input registers (FC = 4), 12 registers starting at 0x1044, encoded as
  6 × signed Int32 with little-endian word order, big-endian byte order
  within each 16-bit word.  raw / 10  = temperature in °C.

Disconnected channels return implausibly large values; the driver
treats |°C| > 500 as a fault and publishes ``{"fault": true}`` with no
temperature.

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

    def _do_poll(self) -> None:
        # One bulk read of the 6 channels.  Each is a signed Int32 spanning
        # two consecutive Modbus registers, low-word-first, big-endian byte
        # order within each word — exactly the encoding of the working
        # logger at github.com/Moore-Lab/RDXL6SD-temperature-logger.
        #
        # pymodbus' slave-id kwarg has churned across 3.x releases (device_id
        # / slave / unit); try each so we work across versions.
        n_regs = _N_CHANNELS * 2
        with self._lock:
            kw_attempts = [
                {"count": n_regs, "device_id": self._cfg.modbus_address},
                {"count": n_regs, "slave":     self._cfg.modbus_address},
                {"count": n_regs, "unit":      self._cfg.modbus_address},
            ]
            rr = None
            last_exc: Optional[Exception] = None
            for kw in kw_attempts:
                try:
                    rr = self._client.read_input_registers(self._cfg.reg_base, **kw)
                    break
                except TypeError as exc:
                    last_exc = exc
                    continue
            if rr is None:
                raise ModbusException(f"no compatible read_input_registers signature ({last_exc})")
        if rr.isError():
            raise ModbusException(f"read_input_registers error: {rr}")
        regs = list(rr.registers)

        # Decode 12 × Int16 → 6 × Int32 (low-word-first, signed).
        temps_c: List[float] = []
        for i in range(_N_CHANNELS):
            lo, hi = regs[2 * i], regs[2 * i + 1]
            raw32 = (hi << 16) | lo
            if raw32 & 0x80000000:                  # sign-extend
                raw32 -= 0x100000000
            temps_c.append(raw32 / 10.0)

        # Publish per configured channel.  Operator-friendly subpath indexing
        # (tc/1..4, rtd/1..2) is assigned in config-order.
        tc_idx, rtd_idx = 0, 0
        for ch_cfg in self._cfg.channels:
            ch    = ch_cfg["ch"]
            kind  = ch_cfg["kind"].lower()
            label = ch_cfg.get("label", f"ch{ch}")
            if not (1 <= ch <= _N_CHANNELS):
                log.warning("[omega] channel %r out of range 1-%d, skipping",
                            ch, _N_CHANNELS)
                continue
            if kind == "tc":
                tc_idx += 1; sub_idx = tc_idx
            elif kind == "rtd":
                rtd_idx += 1; sub_idx = rtd_idx
            else:
                log.warning("[omega] channel %d kind %r unknown, skipping",
                            ch, kind)
                continue

            t_c = temps_c[ch - 1]
            payload: Dict[str, object] = {"channel": sub_idx, "label": label}
            if abs(t_c) > _FAULT_TEMP_C:
                payload["fault"] = True
            else:
                payload["fault"]   = False
                payload["value_c"] = round(t_c, 1)
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
            },
        )
