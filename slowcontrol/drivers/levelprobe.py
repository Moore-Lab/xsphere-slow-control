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

Which USB port
──────────────
None is configured (level_probe.port: auto).  The board's CH340 USB chip has
no serial number, so on the USB side it cannot be told from any other CH340
board on the Pi — the gas handling cart's ESP32 is one — except by the socket
it is in, and cables get moved.  The probe is recognised by what it prints
instead: the driver listens to each CH340 adapter in turn and stays with the
one that prints lines like the above.  An adapter that prints other things is
left alone from then on (until it is re-plugged); one that is silent, or in
use by something else, is tried again later.  Moving the probe's cable to
another socket therefore needs nothing: the old port disappears and the probe
is found again on the new one.

Nothing is ever written to any of these ports: the probe's firmware only
reads its serial port during a start-up calibration that the hardcoded
calibration values skip.  Ports are opened with pyserial's defaults on
purpose — DTR and RTS both asserted is the one combination that leaves an
ESP32 dev board's auto-reset circuit alone, so (re)starting the service does
not reboot a sensor — and with an advisory exclusive lock, so that this and
tools/serial_capture.py can never end up sharing one port's bytes.
"""

from __future__ import annotations

import logging
import re
import time
from typing import Callable, Dict, List, Optional, Tuple

from slowcontrol.drivers import usbserial
from slowcontrol.drivers.base import SensorDriver

log = logging.getLogger(__name__)

_LINE_RE = re.compile(r"median delta\s*=\s*(-?\d+(?:\.\d+)?)\s*\|\s*pwm\s*=\s*(\d+)\s*$")
# What else the probe's firmware prints: its boot banner, "MPR121 not found",
# the calibration dialogue.  Enough to know the board even when it has no reading.
_PROBE_TEXT_RE = re.compile(r"MPR121|calibration values|Place sensor in|Sampling (EMPTY|FULL)|_COUNT")

REOPEN_INTERVAL_S = 5.0     # between attempts to open a configured port that is not there
SEARCH_INTERVAL_S = 1.0     # between looks for the probe while it has not been found
RETRY_S = 30.0              # before a silent or busy adapter is listened to again
MAX_STRANGE_LINES = 5       # this many lines that are not the probe's ⇒ another board
STATUS_INTERVAL_S = 10.0    # the retained status is refreshed at least this often
_MAX_LINE = 512             # a "line" longer than this is noise, not the sensor
_FOREVER = float("inf")


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
    return serial.Serial(port, baud, timeout=timeout_s, exclusive=True)


class LevelProbeDriver(SensorDriver):
    NAME = "level_probe"

    def __init__(self, config, mqtt,
                 open_link: Optional[Callable[[str], object]] = None,
                 list_ports: Optional[Callable[[], List[str]]] = None,
                 plugged_in_at: Callable[[str], int] = usbserial.plugged_in_at,
                 describe: Callable[[str], str] = usbserial.describe):
        super().__init__(config, mqtt)
        self._cfg = config.level_probe
        self._auto = self._cfg.port.strip().lower() == "auto"
        self._open_link = open_link or (
            lambda port: _open_serial(port, self._cfg.baud_rate, self._cfg.timeout_s))
        self._list_ports = list_ports or (lambda: usbserial.adapters(self._cfg.usb_id))
        self._plugged_in_at = plugged_in_at
        self._describe = describe
        self._link = None
        self._port = ""                 # the port that is open
        self._identified = False        # … and known to be the probe
        self._opened_ts = 0.0
        self._strange = 0               # lines since opening that are not the probe's
        self._last_good = ""            # where the probe was last found
        # port → (when it was plugged in, do not listen again before): adapters
        # found not to be the probe.  Forgotten when the adapter is re-plugged.
        self._passed_over: Dict[str, Tuple[int, float]] = {}
        self._buf = b""
        self._next_open_ts = 0.0
        self._last_reading_ts: Optional[float] = None
        self._error = ""
        self._logged_error = ""
        self._note = ""                 # last line of the probe's that was not a reading
        self._last_status: Optional[dict] = None
        self._last_status_ts = 0.0

    @property
    def poll_interval(self) -> float:
        # poll() blocks in the serial read for up to timeout_s; this only paces
        # the loop while no port is open.
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
                log.warning("[level_probe] lost %s: %s", self._describe(self._port), exc)
                self._close()
                self._error = self._logged_error = f"read failed: {exc}"
                self._next_open_ts = time.monotonic() + (
                    SEARCH_INTERVAL_S if self._auto else REOPEN_INTERVAL_S)
            else:
                self._feed(chunk)
                if (self._link is not None and not self._identified
                        and time.monotonic() - self._opened_ts > self._cfg.identify_s):
                    self._pass_over("it printed nothing the level probe prints",
                                    time.monotonic() + RETRY_S)
        self._publish_status()

    # ------------------------------------------------------------------
    # Finding and opening the port
    # ------------------------------------------------------------------

    def _try_open(self) -> None:
        now = time.monotonic()
        if not self._auto:
            self._open(self._cfg.port, now, identified=True)
            return

        present = self._list_ports()
        for port in list(self._passed_over):        # re-plugged or gone: forget
            if port not in present or self._plugged_in_at(port) != self._passed_over[port][0]:
                del self._passed_over[port]
        # Where the probe was last time first, so a restart finds it at once.
        todo = sorted((p for p in present
                       if p not in self._passed_over or now >= self._passed_over[p][1]),
                      key=lambda p: p != self._last_good)
        for port in todo:
            if self._open(port, now, identified=False):
                return
        self._next_open_ts = now + SEARCH_INTERVAL_S
        if not present:
            self._set_error(f"no USB-serial adapter ({self._cfg.usb_id}) is plugged in")
        else:
            self._set_error(f"the level probe is not on any of the {len(present)} "
                            f"USB-serial adapter(s) plugged in — still looking")

    def _open(self, port: str, now: float, identified: bool) -> bool:
        try:
            link = self._open_link(port)
        except Exception as exc:
            if self._auto:                  # in use by something else, or just unplugged
                self._passed_over[port] = (self._plugged_in_at(port), now + RETRY_S)
                log.debug("[level_probe] cannot listen to %s: %s", port, exc)
            else:
                self._set_error(str(exc), f"cannot open {port}")
                self._next_open_ts = now + REOPEN_INTERVAL_S
            return False
        self._link, self._port = link, port
        self._buf = b""
        self._strange = 0
        self._opened_ts = now
        self._identified = identified
        if identified:
            self._error = self._logged_error = ""
            log.info("[level_probe] reading %s at %d baud", port, self._cfg.baud_rate)
        else:
            self._error = f"looking for the level probe — listening to {self._describe(port)}"
        return True

    def _pass_over(self, why: str, retry_at: float) -> None:
        """The adapter that is open is not the probe: close it and move on."""
        port = self._port
        level = logging.DEBUG if port in self._passed_over else logging.INFO
        log.log(level, "[level_probe] %s is not the level probe: %s",
                self._describe(port), why)
        self._passed_over[port] = (self._plugged_in_at(port), retry_at)
        self._close()
        self._next_open_ts = 0.0            # straight on to the next candidate

    def _close(self) -> None:
        if self._link is not None:
            try:
                self._link.close()
            except Exception:
                pass
        self._link = None
        self._port = ""
        self._identified = False
        self._buf = b""

    def _set_error(self, text: str, log_as: str = "") -> None:
        if text != self._logged_error:      # once, not on every retry
            log.warning("[level_probe] %s", f"{log_as}: {text}" if log_as else text)
            self._logged_error = text
        self._error = text

    # ------------------------------------------------------------------
    # Lines
    # ------------------------------------------------------------------

    def _feed(self, chunk: bytes) -> None:
        self._buf += chunk
        while b"\n" in self._buf and self._link is not None:
            raw, self._buf = self._buf.split(b"\n", 1)
            self._handle_line(raw.decode("ascii", errors="replace").strip())
        if len(self._buf) > _MAX_LINE:
            self._buf = b""

    def _handle_line(self, line: str) -> None:
        if not line:
            return
        parsed = parse_line(line)
        if parsed is not None and parsed[1] > self._cfg.pwm_max:
            parsed = None

        if not self._identified:
            if parsed is None and not _PROBE_TEXT_RE.search(line):
                # Not (yet) recognisable: a line torn by opening mid-way — or
                # another board altogether, which nothing is reported about.
                self._strange += 1
                if self._strange >= MAX_STRANGE_LINES:
                    self._pass_over(f"it prints other things ({line[:40]!r})", _FOREVER)
                return
            self._identified = True
            self._last_good = self._port
            self._error = self._logged_error = ""
            log.info("[level_probe] found the level probe on %s", self._describe(self._port))

        if parsed is None:
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
        connected = self._link is not None and self._identified
        status = {
            "connected": connected,
            "fresh": fresh,
            "port": self._describe(self._port) if connected else "",
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
