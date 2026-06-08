"""
Omega RDXL6SD-USB temperature logger driver.

Modbus RTU over USB-serial. The device exposes 6 channels (4 type-K
thermocouples + 2 PT100 RTDs in the lab's wiring) at 9600 baud, default
slave address 1. Each channel temperature lives in a signed 16-bit
holding register, value = 0.1 × °C.

Disconnected channels return a sentinel near ±32767; the driver treats
any raw |reading| > 5000 (i.e. |°C| > 500) as a fault and publishes
``{"fault": true}`` with no temperature.

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

# RDXL6SD sentinel: raw 16-bit values whose absolute magnitude exceeds this
# (in 0.1 °C units) indicate a disconnected sensor or out-of-range fault.
_RAW_FAULT_THRESHOLD = 5000        # |°C| > 500


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
            bytesize=8, parity="N", stopbits=1,
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
        # One bulk read of all 6 registers; channels we don't publish are
        # just ignored.  Slot-by-slot reads would be more polite to a busy
        # bus but this device has nothing else on it.
        #
        # pymodbus' slave-id kwarg name has churned across 3.x releases:
        #   3.7+ → device_id=, earlier 3.x → slave= or unit=
        # Try each in order so this works regardless of installed version.
        with self._lock:
            kw_attempts = [
                {"count": 6, "device_id": self._cfg.modbus_address},
                {"count": 6, "slave":     self._cfg.modbus_address},
                {"count": 6, "unit":      self._cfg.modbus_address},
            ]
            rr = None
            last_exc: Optional[Exception] = None
            for kw in kw_attempts:
                try:
                    rr = self._client.read_holding_registers(self._cfg.reg_base, **kw)
                    break
                except TypeError as exc:
                    last_exc = exc
                    continue
            if rr is None:
                raise ModbusException(f"no compatible read_holding_registers signature ({last_exc})")
        if rr.isError():
            raise ModbusException(f"read_holding_registers error: {rr}")
        regs = list(rr.registers)

        # Each register is a signed 16-bit integer in 0.1 °C.
        def _signed(u: int) -> int:
            return u - 0x10000 if u >= 0x8000 else u

        tc_idx  = 0
        rtd_idx = 0
        for ch_cfg in self._cfg.channels:
            ch     = ch_cfg["ch"]
            kind   = ch_cfg["kind"].lower()        # "tc" | "rtd"
            label  = ch_cfg.get("label", f"ch{ch}")
            if not (1 <= ch <= 6):
                log.warning("[omega] channel %r out of range 1-6, skipping", ch)
                continue
            raw = _signed(regs[ch - 1])
            # Per-kind running index for the MQTT subpath (tc/1..4, rtd/1..2),
            # matched against the order in the config so the operator's
            # numbering is stable as channels are added.
            if kind == "tc":
                tc_idx += 1
                sub_idx = tc_idx
            elif kind == "rtd":
                rtd_idx += 1
                sub_idx = rtd_idx
            else:
                log.warning("[omega] channel %d kind %r unknown, skipping",
                            ch, kind)
                continue

            payload: Dict[str, object] = {
                "channel": sub_idx,
                "label":   label,
            }
            if abs(raw) > _RAW_FAULT_THRESHOLD:
                payload["fault"] = True
            else:
                t_c = raw * 0.1
                payload["fault"]   = False
                payload["value_c"] = round(t_c, 1)
                payload["value_k"] = round(t_c + CELSIUS_TO_KELVIN, 2)
            self._mqtt.publish_sensor(
                "temperature", "omega", kind, str(sub_idx),
                payload=payload,
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
