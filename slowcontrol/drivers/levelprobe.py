"""
Cryostat LN level probe — the MPR121 capacitive sensor's ESP32, read over USB.

The ESP32 (firmware: Moore-Lab/liquid-level-sensor, "MPR121 Level Sensor")
turns the probe's capacitance count into an 8-bit PWM duty on its analog-out
pin, which used to be wired to a CLICK analog input.  It also prints every
result on its USB serial port (9600 8N1), one line per ~3.5 s:

    median delta = 742.453125 | pwm = 129

This driver reads those lines on the Pi, converts the PWM duty back into the
voltage the analog input would have seen (pwm / 255 × 3.3 V), and publishes

    xsphere/sensors/level_probe/{vessel}   {"voltage_v", "pwm", "count"}
    xsphere/status/level_probe/{vessel}    {"connected", "fresh", "port", "error", "note"}  (retained)

The PLC driver subscribes to the sensor topic and writes voltage_v into the
PLC (see REG_LEVEL_PROBE_WRITE in drivers/plc.py), so the ladder gets the
same number it got from the analog input.

Nothing is ever written to the ESP32: its firmware only reads the serial port
during a start-up calibration that the hardcoded calibration values skip.
The port is opened with pyserial's defaults on purpose — DTR and RTS both
asserted is the one combination that leaves an ESP32 dev board's auto-reset
circuit alone, so (re)starting the service does not reboot the sensor.
"""

from __future__ import annotations

import logging
import re
import time
from typing import Callable, Optional, Tuple

from slowcontrol.drivers.base import SensorDriver

log = logging.getLogger(__name__)

_LINE_RE = re.compile(r"median delta\s*=\s*(-?\d+(?:\.\d+)?)\s*\|\s*pwm\s*=\s*(\d+)\s*$")

REOPEN_INTERVAL_S = 5.0     # between attempts to open a port that is not there
STATUS_INTERVAL_S = 10.0    # the retained status is refreshed at least this often
_MAX_LINE = 512             # a "line" longer than this is noise, not the sensor


def parse_line(line: str) -> Optional[Tuple[float, int]]:
    """(count, pwm) from one line of the sensor's output, or None if the line
    is something else (boot banner, calibration prompt, a torn line)."""
    m = _LINE_RE.search(line.strip())
    if m is None:
        return None
    return float(m.group(1)), int(m.group(2))


def pwm_to_volts(pwm: int, pwm_max: int = 255, v_ref: float = 3.3) -> float:
    """The mean voltage of the ESP32's PWM output at this duty."""
    return pwm / pwm_max * v_ref


def _open_serial(port: str, baud: int, timeout_s: float):
    import serial       # deferred so the tests need no pyserial
    return serial.Serial(port, baud, timeout=timeout_s)


class LevelProbeDriver(SensorDriver):
    NAME = "level_probe"

    def __init__(self, config, mqtt, open_link: Optional[Callable] = None):
        super().__init__(config, mqtt)
        self._cfg = config.level_probe
        self._open_link = open_link or (
            lambda: _open_serial(self._cfg.port, self._cfg.baud_rate, self._cfg.timeout_s))
        self._link = None
        self._buf = b""
        self._next_open_ts = 0.0
        self._last_reading_ts: Optional[float] = None
        self._error = ""
        self._note = ""             # last line that was not a reading
        self._last_status: Optional[dict] = None
        self._last_status_ts = 0.0

    @property
    def poll_interval(self) -> float:
        # poll() blocks in the serial read for up to timeout_s; this only paces
        # the loop while the port is absent.
        return 0.2

    # ------------------------------------------------------------------
    # SensorDriver interface
    # ------------------------------------------------------------------

    def connect(self) -> None:
        # Never raises: a sensor that is unplugged at start-up is picked up by
        # poll() when it appears, instead of disabling the driver for good.
        self._try_open()
        self._publish_status(force=True)

    def disconnect(self) -> None:
        self._close()
        self._error = "service stopped"
        self._publish_status(force=True)

    def poll(self) -> None:
        if self._link is None and time.monotonic() >= self._next_open_ts:
            self._try_open()
        if self._link is not None:
            try:
                chunk = self._link.read(max(1, getattr(self._link, "in_waiting", 0) or 1))
            except Exception as exc:        # unplugged: SerialException / OSError
                log.warning("[level_probe] lost %s: %s", self._cfg.port, exc)
                self._close()
                self._error = f"read failed: {exc}"
                self._next_open_ts = time.monotonic() + REOPEN_INTERVAL_S
            else:
                self._feed(chunk)
        self._publish_status()

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _try_open(self) -> None:
        try:
            self._link = self._open_link()
        except Exception as exc:
            if str(exc) != self._error:
                log.warning("[level_probe] cannot open %s: %s", self._cfg.port, exc)
            self._error = str(exc)
            self._link = None
            self._next_open_ts = time.monotonic() + REOPEN_INTERVAL_S
            return
        self._buf = b""
        self._error = ""
        log.info("[level_probe] reading %s at %d baud", self._cfg.port, self._cfg.baud_rate)

    def _close(self) -> None:
        if self._link is not None:
            try:
                self._link.close()
            except Exception:
                pass
        self._link = None

    def _feed(self, chunk: bytes) -> None:
        self._buf += chunk
        while b"\n" in self._buf:
            raw, self._buf = self._buf.split(b"\n", 1)
            self._handle_line(raw.decode("ascii", errors="replace").strip())
        if len(self._buf) > _MAX_LINE:
            self._buf = b""

    def _handle_line(self, line: str) -> None:
        if not line:
            return
        parsed = parse_line(line)
        if parsed is None or parsed[1] > self._cfg.pwm_max:
            # Boot banner, "MPR121 not found", a calibration prompt, …
            if line != self._note:
                log.warning("[level_probe] sensor says: %s", line[:120])
            self._note = line[:120]
            return
        count, pwm = parsed
        self._last_reading_ts = time.monotonic()
        self._note = ""
        self._mqtt.publish_sensor("level_probe", self._cfg.vessel, payload={
            "voltage_v": round(pwm_to_volts(pwm, self._cfg.pwm_max, self._cfg.v_ref), 4),
            "pwm": pwm,
            "count": round(count, 3),
        })

    def _publish_status(self, force: bool = False) -> None:
        now = time.monotonic()
        fresh = (self._last_reading_ts is not None
                 and now - self._last_reading_ts <= self._cfg.stale_s)
        status = {
            "connected": self._link is not None,
            "fresh": fresh,
            "port": self._cfg.port,
            "error": self._error,
            "note": self._note,
        }
        if (force or status != self._last_status
                or now - self._last_status_ts >= STATUS_INTERVAL_S):
            if (self._last_status is not None and self._last_status["fresh"]
                    and not fresh):
                log.warning("[level_probe] no reading from the sensor for %.0f s",
                            self._cfg.stale_s)
            self._last_status = status
            self._last_status_ts = now
            self._mqtt.publish_status("level_probe", self._cfg.vessel, payload=status)
