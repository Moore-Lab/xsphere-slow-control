"""
Leybold TURBOVAC i / iX turbo pump monitor — read-only, over USB.

Two turbo pumps serve the experiment, and the frequency converter on each has
a USB port that goes to the Pi:

    ghs    the gas handling system, science chamber included
    outer  the outer (insulation) vacuum

This driver reads them — rotor speed, motor current, power, temperatures,
operating state, warnings, the pump's own error memory — and publishes that.
It does nothing else.

It cannot operate a pump
────────────────────────
The converter speaks USS (VDI/VDE 3689): fixed 24-byte telegrams, one request
→ one reply.  It acts on a telegram's control word only if bit 10 of that
word is set, and changes a setting only for one of the write access codes.
`read_telegram()` is the one place a telegram is built, and all it can build
is a parameter read with every other field zero.  So there is no way from
here to start or stop a pump, change its speed or alter a parameter — and the
driver subscribes to no command topic.  The pumps stay under whatever
operates them now: the pumping stand.  (No bus master holds control of the
GHS pump — bit 15 of its status word is clear — so its start comes in at the
converter's own X1 connector.)

That is more than tidiness.  By the manual, a telegram with bit 10 set and the
start bit clear takes control away from X1 and spins the pump down, and a
master that has taken control and then goes quiet hands it back after 10 s
(parameter 182).  None of that has been tried, and none of it can be sent.

Which pump is which
───────────────────
Both converters present the same USB identity, serial number string included
("0001A0000000" is a constant in their firmware), and the kernel numbers them
/dev/ttyACM0, 1 … in plug-in order.  So neither the USB serial number nor the
device name says which pump is on a port.  The pump's own serial number does:
parameter 355, read from the pump on every connect and compared with
`turbovac.pumps.<name>.serial` in config.yaml.  Swap the two USB cables and
the readings still carry the right names.

A pump whose `serial` is empty is "not pinned": it takes the one TURBOVAC on
USB that no other entry has pinned, and its status says so.  With two such
pumps to choose from it takes neither.  Pin it once the pump is connected.

How it is connected
───────────────────
USB B on the converter → the Pi.  The converter is a CDC-ACM device
(25e9:0bb8): the kernel's cdc_acm driver provides the tty, `xbox` can open it
through the dialout group, and ModemManager leaves it alone — no udev rule.
19200 baud 8E1, address 0.  The converter's USB side runs off the pump's own
24 V supply, so a pump that is switched off at its supply is simply not there;
that is reported as "not connected" and picked up again when it returns.

Things the pump does that shaped this code
──────────────────────────────────────────
Read on the GHS pump (TURBOVAC 90 i, firmware 2.10.01), 2026-10-11:

  • Every reply carries the process data — status word, rotor frequency,
    converter temperature, motor current, bearing temperature, supply voltage
    — whatever was asked.  The voltage there is in whole volts; parameter 4
    has it in 0.1 V.
  • A bare status poll is answered in about 1 ms.  Reading a parameter takes
    about 30 ms (the USB front end has to fetch it from the drive
    electronics), so a poll — four parameters and the status — is ~120 ms of
    conversation per pump.
  • The identification strings are indexed parameters, one character per
    telegram and zero-padded to 18.  Identifying a pump therefore costs about
    two seconds; it is done once per connect, never per poll.
  • A parameter the model does not have, or an index past the end, is
    answered with an error reply rather than silence.  Such a value is left
    out of what is published; only a missing or damaged reply drops the link.
  • The 16-bit values answer with reply code 1 and the hour counters with 2.
  • The error memory is parameter 171 (index 0 = most recent), with the rotor
    frequency and operating hours at that moment in 174 and 176.  Parameter
    40 counts the entries ever made, so a rise there means a new one.

Protocol reference: Leybold "Serial Interfaces for TURBOVAC i/iX",
300450826_002_C2 — sections 3.1 (telegram), 4 (access codes, status word),
5 (parameters), 6 (error codes), 7 (warning bits).

MQTT interface
──────────────
  Publish:
    xsphere/sensors/turbo/{name}   every poll while the pump answers (not
                                   retained; silent otherwise):
        {state, frequency_hz, setpoint_hz, current_a, voltage_v, power_w,
         motor_temp_c, bearing_temp_c, converter_temp_c, drive_on, normal,
         turning, accelerating, decelerating, fault, warning, remote,
         status_word, warning_bits, operating_hours, start_count,
         error_count, last_error_code}
    xsphere/status/turbo/{name}    every poll (retained):
        {connected, label, pinned, configured_serial, port, error, event,
         event_utc, last_read_utc, product, pump_serial, catalog_no,
         converter_serial, converter_type, firmware, drive_firmware,
         warnings, setpoint_hz, operating_hours, electronics_hours,
         start_count, error_count, last_error_code, last_error,
         last_error_hz, last_error_hours}
  Subscribe: nothing.

  state: stopped | accelerating | normal | running | decelerating | coasting
         | error          ("running" = drive on, below normal-operation speed
                           and neither gaining nor losing it)

Config: the `turbovac:` block of config.yaml (TurbovacConfig in core/config.py).
"""

from __future__ import annotations

import logging
import re
import struct
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional

from slowcontrol.drivers.base import SensorDriver

log = logging.getLogger(__name__)

USB_VID, USB_PID = 0x25E9, 0x0BB8       # "Leybold Vacuum TURBOVAC.INTEGRA USB VCom"

# ---------------------------------------------------------------------------
# USS telegram
# ---------------------------------------------------------------------------

STX = 0x02
LGE = 22                                # length byte: everything after it
FRAME_LEN = 24

# Parameter access codes (bits 15-12 of the PKE word).  The converter also has
# 2, 3, 7 and 8 — the writes.  They are left out on purpose.
ACCESS_NONE = 0                         # no parameter: a bare status poll
ACCESS_READ = 1
ACCESS_READ_FIELD = 6                   # one element of an indexed parameter
_READS = (ACCESS_NONE, ACCESS_READ, ACCESS_READ_FIELD)

# Reply codes (the same bits, in the answer).  Anything else — 7 "cannot do
# that", 8 "no permission", 0 "nothing" — means there is no value.
_REPLY_16 = (1, 4)                      # value / field element, 16 bit
_REPLY_32 = (2, 5)                      #                        32 bit

# Status word bits (manual section 4.4)
ZSW_DRIVE_ON = 1 << 2                   # "operation enabled"
ZSW_ERROR = 1 << 3
ZSW_ACCELERATING = 1 << 4
ZSW_DECELERATING = 1 << 5
ZSW_TEMP_WARNING = 1 << 7
ZSW_NORMAL = 1 << 10                    # at normal-operation speed
ZSW_TURNING = 1 << 11
ZSW_OVERLOAD_WARNING = 1 << 13
ZSW_WARNING = 1 << 14                   # collective warning
ZSW_REMOTE = 1 << 15                    # a bus master holds control (never us)
_ZSW_ANY_WARNING = ZSW_TEMP_WARNING | ZSW_OVERLOAD_WARNING | ZSW_WARNING

# Parameters (manual section 5)
P_CONVERTER_TYPE = 1
P_FIRMWARE = 2
P_VOLTAGE = 4                           # 0.1 V
P_POWER = 6                             # 0.1 W, drive input
P_MOTOR_TEMP = 7                        # °C, signed
P_SETPOINT = 24                         # Hz
P_STARTS = 38
P_ERROR_COUNT = 40
P_ERROR_CODE = 171                      # indexed; 0 = most recent
P_ERROR_FREQUENCY = 174                 # Hz at that error
P_ERROR_HOURS = 176                     # 0.01 h at that error
P_OPERATING_HOURS = 184                 # 0.01 h with the pump running
P_WARNING_BITS = 227
P_PRODUCT = 313                         # text
P_CONVERTER_SERIAL = 315                # text
P_CATALOG_NO = 350                      # text
P_PUMP_SERIAL = 355                     # text — what tells two pumps apart
P_ELECTRONICS_HOURS = 682               # 0.01 h powered
P_DRIVE_FIRMWARE = 1100

TEXT_LEN = 18                           # elements in an identification string

# Error codes (manual section 6).  Codes the manual's table does not make
# unambiguous are left to the number alone.
ERROR_CODES: Dict[int, str] = {
    1: "overspeed warning",
    2: "pass-through time error (did not get through the critical speed range in time)",
    3: "bearing temperature too high",
    4: "short circuit",
    5: "converter temperature too high",
    6: "run-up time error (did not reach normal operation in time)",
    7: "motor temperature too high",
    61: "bearing temperature warning",
    83: "motor undertemperature warning",
    84: "motor temperature warning",
    85: "converter overtemperature warning",
    86: "pump temperature 6 warning",
    87: "pump temperature 6 failure",
    94: "pump temperature 4 warning",
    95: "pump temperature 4 failure",
    96: "pump temperature 5 warning",
    97: "pump temperature 5 failure",
    101: "overload warning (speed fell below the normal-operation threshold)",
    103: "supply voltage warning",
    106: "overload failure (speed fell below the minimum)",
    111: "motor undertemperature error",
    116: "permanent overload error",
    117: "motor current error",
    143: "overspeed failure",
    213: "supply voltage error",
    221: "checksum error",
    225: "bearing run-in active",
    227: "frequency converter error",
    228: "frequency converter error",
    229: "frequency converter error",
    230: "frequency converter error",
}

# Warning bits of parameter 227 (manual section 7)
WARNING_BITS: Dict[int, str] = {
    0: "pump temperature 1 high",
    1: "pump temperature 2 high",
    2: "pump temperature 3 high",
    3: "ambient temperature low",
    6: "overspeed",
    7: "pump temperature 4 high",
    11: "overload",
    12: "pump temperature 5 high",
    13: "pump temperature 6 high",
    14: "supply voltage",
}

RETRY_PAUSE_S = 0.05        # before the one retry of a damaged reply
RESCAN_S = 5.0              # between looks for a pump that is not connected
STRANGER_S = 60.0           # how long an unassigned pump's serial is taken as read
BACKOFF_S = 60.0            # after an internal error


class TurbovacLinkError(Exception):
    """Talking to a pump failed — its port should be closed and found again."""


class TurbovacFrameError(TurbovacLinkError):
    """A reply arrived damaged or out of step; worth one more try."""


def _bcc(frame: bytes) -> int:
    x = 0
    for byte in frame[:FRAME_LEN - 1]:
        x ^= byte
    return x


def read_telegram(access: int = ACCESS_NONE, pnu: int = 0, index: int = 0) -> bytes:
    """The 24-byte telegram for one read.

    Bytes 7-22 — parameter value, control word, frequency setpoint and the
    rest of the process data — are always zero, and only the read access
    codes are accepted.  Nothing built here can operate a pump.
    """
    if access not in _READS:
        raise ValueError(f"access code {access} is not a read")
    if not 0 <= pnu <= 0x7FF or not 0 <= index <= 0xFF:
        raise ValueError(f"no such parameter: {pnu}[{index}]")
    frame = bytearray(FRAME_LEN)
    frame[0], frame[1] = STX, LGE       # frame[2], the address, is 0 on USB
    struct.pack_into(">H", frame, 3, (access << 12) | pnu)
    frame[6] = index
    frame[FRAME_LEN - 1] = _bcc(frame)
    return bytes(frame)


@dataclass(frozen=True)
class Reply:
    """One answer from the pump: the parameter part and the process data."""
    code: int               # reply code
    pnu: int
    index: int
    raw: int                # parameter value, as an unsigned 32-bit number
    status_word: int
    frequency_hz: int
    converter_temp_c: int
    current_a: float
    bearing_temp_c: int
    voltage_v: int          # whole volts

    def number(self, signed: bool = False) -> Optional[int]:
        """The parameter value, or None if the pump did not give one."""
        if self.code in _REPLY_16:
            value = self.raw & 0xFFFF
            return value - 0x10000 if signed and value & 0x8000 else value
        if self.code in _REPLY_32:
            return self.raw - (1 << 32) if signed and self.raw & (1 << 31) else self.raw
        return None


def parse_reply(frame: bytes) -> Reply:
    if (len(frame) != FRAME_LEN or frame[0] != STX or frame[1] != LGE
            or _bcc(frame) != frame[FRAME_LEN - 1]):
        raise TurbovacFrameError("damaged reply: " + (bytes(frame).hex(" ") or "nothing"))
    pke, = struct.unpack_from(">H", frame, 3)
    raw, zsw, freq, conv, cur, bear, volt = struct.unpack_from(">IHHhHhH", frame, 7)
    return Reply(code=pke >> 12, pnu=pke & 0x7FF, index=frame[6], raw=raw,
                 status_word=zsw, frequency_hz=freq, converter_temp_c=conv,
                 current_a=cur / 10.0, bearing_temp_c=bear, voltage_v=volt)


def state_of(status_word: int) -> str:
    """The pump's operating state as one word (see the module docstring)."""
    if status_word & ZSW_ERROR:
        return "error"
    if status_word & ZSW_ACCELERATING:
        return "accelerating"
    if status_word & ZSW_DECELERATING:
        return "decelerating"
    if status_word & ZSW_DRIVE_ON:
        return "normal" if status_word & ZSW_NORMAL else "running"
    return "coasting" if status_word & ZSW_TURNING else "stopped"


def error_text(code: Optional[int]) -> Optional[str]:
    """What an error-memory entry says; "none" for an empty one."""
    if code is None:
        return None
    if code == 0:
        return "none"
    return ERROR_CODES.get(code, f"error code {code}")


def warning_text(bits: Optional[int]) -> List[str]:
    if not bits:
        return []
    return [WARNING_BITS.get(n, f"warning bit {n}") for n in range(16) if bits >> n & 1]


# ---------------------------------------------------------------------------
# Link: the converter's virtual COM port
# ---------------------------------------------------------------------------

class SerialLink:
    """exchange / close on one pump's tty.

    Every failure — a timeout, a short reply, the port having gone because
    the pump was switched off — is raised as TurbovacLinkError.
    """

    def __init__(self, port: str, timeout_s: float = 0.5):
        try:
            import serial
        except ImportError as exc:                                  # pragma: no cover
            raise TurbovacLinkError("pyserial is not installed") from exc
        self.port = port
        ser = serial.Serial()
        ser.port = port
        ser.baudrate, ser.bytesize, ser.stopbits = 19200, 8, 1
        ser.parity = serial.PARITY_EVEN
        ser.timeout = ser.write_timeout = timeout_s
        # One talker per pump: a second process's telegram in the middle of
        # ours would garble both.
        ser.exclusive = True
        try:
            ser.open()
        except Exception as exc:                                    # noqa: BLE001
            raise TurbovacLinkError(f"cannot open {port}: {exc}") from exc
        self._ser = ser

    def exchange(self, telegram: bytes) -> bytes:
        try:
            self._ser.reset_input_buffer()
            self._ser.write(telegram)
            reply = self._ser.read(FRAME_LEN)
        except Exception as exc:                                    # noqa: BLE001
            # pyserial raises SerialException, OSError or termios.error
            # depending on how the port went away.
            raise TurbovacLinkError(f"{self.port}: {exc}") from exc
        if len(reply) != FRAME_LEN:
            raise TurbovacLinkError(
                f"{self.port}: " + (f"reply cut short at {len(reply)} bytes" if reply
                                    else "no reply"))
        return reply

    def close(self) -> None:
        try:
            self._ser.close()
        except Exception:                                           # noqa: BLE001
            pass


def usb_ports() -> List[str]:
    """The tty of every TURBOVAC converter on USB right now."""
    try:
        from serial.tools import list_ports
    except ImportError as exc:                                      # pragma: no cover
        raise TurbovacLinkError("pyserial is not installed") from exc
    return sorted(p.device for p in list_ports.comports()
                  if p.vid == USB_VID and p.pid == USB_PID)


# ---------------------------------------------------------------------------
# Instrument: reads from one pump
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Identity:
    pump_serial: str
    product: str
    catalog_no: str
    converter_serial: str
    converter_type: Optional[int]
    firmware: str
    drive_firmware: str


def _version(raw: Optional[int]) -> str:
    """21001 → "2.10.01" (version xx.yy, correction index zz)."""
    if raw is None:
        return ""
    return f"{raw // 10000}.{raw // 100 % 100:02d}.{raw % 100:02d}"


class Turbovac:
    """Read access to one TURBOVAC i / iX over an open link."""

    def __init__(self, link):
        self._link = link

    def _once(self, telegram: bytes, access: int, pnu: int, index: int) -> Reply:
        reply = parse_reply(self._link.exchange(telegram))
        if access != ACCESS_NONE and (reply.pnu, reply.index) != (pnu, index):
            raise TurbovacFrameError(f"asked for parameter {pnu}[{index}], the reply "
                                     f"is about {reply.pnu}[{reply.index}]")
        return reply

    def _ask(self, access: int, pnu: int = 0, index: int = 0) -> Reply:
        telegram = read_telegram(access, pnu, index)
        try:
            return self._once(telegram, access, pnu, index)
        except TurbovacFrameError as exc:
            log.debug("[turbovac] %s — asking once more", exc)
            time.sleep(RETRY_PAUSE_S)
            return self._once(telegram, access, pnu, index)

    def status(self) -> Reply:
        """A bare status poll: the process data and nothing else."""
        return self._ask(ACCESS_NONE)

    def read(self, pnu: int, index: Optional[int] = None,
             signed: bool = False) -> Optional[int]:
        """One parameter (or one element of an indexed one); None if this
        pump does not have it."""
        if index is None:
            return self._ask(ACCESS_READ, pnu).number(signed)
        return self._ask(ACCESS_READ_FIELD, pnu, index).number(signed)

    def read_text(self, pnu: int) -> str:
        """An identification string: one character per index, zero-padded."""
        chars: List[str] = []
        for index in range(TEXT_LEN):
            code = self.read(pnu, index)
            if not code:                    # the padding, or no such parameter
                break
            chars.append(chr(code) if 32 <= code < 127 else "?")
        return "".join(chars).strip()

    def pump_serial(self) -> str:
        return self.read_text(P_PUMP_SERIAL)

    def identify(self) -> Identity:
        return Identity(
            pump_serial=self.pump_serial(),
            product=self.read_text(P_PRODUCT),
            catalog_no=self.read_text(P_CATALOG_NO),
            converter_serial=self.read_text(P_CONVERTER_SERIAL),
            converter_type=self.read(P_CONVERTER_TYPE),
            firmware=_version(self.read(P_FIRMWARE)),
            drive_firmware=_version(self.read(P_DRIVE_FIRMWARE)),
        )

    def close(self) -> None:
        self._link.close()


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------

_NAME_RE = re.compile(r"[a-z][a-z0-9_]*")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _hours(raw: Optional[int]) -> Optional[float]:
    return None if raw is None else round(raw / 100.0, 2)


class _Pump:
    """One pump named in config.yaml, and what is known about it right now."""

    def __init__(self, name: str, cfg):
        self.name = name
        self.label = cfg.label or name
        self.serial = cfg.serial                    # "" = not pinned
        self.tv: Optional[Turbovac] = None
        self.port: Optional[str] = None
        self.identity: Optional[Identity] = None
        self.error: Optional[str] = "not looked for yet"
        self.event: Optional[str] = None
        self.event_utc: Optional[str] = None
        self.last_read_utc: Optional[str] = None
        self.reading: Optional[dict] = None         # this poll's measurements
        self.warnings: List[str] = []
        self.slow: dict = {}                        # counters, error memory, setpoint
        self.next_slow = 0.0
        self.state: Optional[str] = None
        self.alarm_bits = 0                         # error / warning bits last seen
        # The error counter last seen, and on which pump: survives a lost
        # link, so an entry made while nobody was looking is still noticed.
        self.counted: Optional[int] = None
        self.counted_on: Optional[str] = None


class TurbovacDriver(SensorDriver):
    NAME = "turbovac"

    def __init__(self, config, mqtt,
                 list_ports: Optional[Callable[[], List[str]]] = None,
                 open_link: Optional[Callable[[str], object]] = None):
        super().__init__(config, mqtt)
        self._cfg = config.turbovac
        self._list_ports = list_ports or usb_ports
        self._open_link = open_link or (lambda port: SerialLink(port, self._cfg.timeout_s))
        self._pumps: List[_Pump] = [_Pump(str(name), pump_cfg)
                                    for name, pump_cfg in self._cfg.pumps.items()]
        self._next_scan = 0.0
        # Ports whose pump belongs to nobody in config.yaml:
        # port → (pump serial, when to ask again)
        self._strangers: Dict[str, tuple] = {}

    @property
    def poll_interval(self) -> float:
        return self._cfg.poll_interval_s

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def connect(self) -> None:
        # Never raises for an absent pump: SensorDriver.start() would disable
        # the driver for good.  Pumps are found (and found again) from poll().
        # A config.yaml that cannot work is a different matter.
        if not self._pumps:
            raise RuntimeError("turbovac.pumps is empty — no pump to monitor")
        pinned: Dict[str, str] = {}
        for pump in self._pumps:
            if not _NAME_RE.fullmatch(pump.name):
                raise RuntimeError(f"turbovac.pumps: {pump.name!r} is not a usable name "
                                   "(lower-case letters, digits, underscores)")
            if pump.serial and pump.serial in pinned:
                raise RuntimeError(f"turbovac.pumps: {pinned[pump.serial]} and {pump.name} "
                                   f"are both pinned to pump serial {pump.serial}")
            if pump.serial:
                pinned[pump.serial] = pump.name
        for pump in self._pumps:
            log.info("[turbovac] %s — %s: %s", pump.name, pump.label,
                     f"pump serial {pump.serial}" if pump.serial
                     else "not pinned (takes the one unassigned TURBOVAC on USB)")

    def disconnect(self) -> None:
        for pump in self._pumps:
            self._drop(pump, "service stopped")
            self._publish(pump)

    # ------------------------------------------------------------------
    # Poll
    # ------------------------------------------------------------------

    def poll(self) -> None:
        try:
            if any(pump.tv is None for pump in self._pumps):
                self._scan()
            for pump in self._pumps:
                if pump.tv is not None:
                    self._read(pump)
        except Exception as exc:                                    # noqa: BLE001
            # Contained here on purpose.  Left to the base class, a fault that
            # repeats every poll reads as a stuck poll thread, and the service
            # watchdog would restart the whole slow control — PID feed and PV
            # interlock included — over a pump read-out.
            log.exception("[turbovac] unexpected error — dropping the pump links")
            for pump in self._pumps:
                self._drop(pump, f"internal error: {exc!r}")
            self._next_scan = time.monotonic() + BACKOFF_S
        try:
            for pump in self._pumps:
                self._publish(pump)
        except Exception:                                           # noqa: BLE001
            log.exception("[turbovac] could not publish")

    # ------------------------------------------------------------------
    # Finding the pumps
    # ------------------------------------------------------------------

    def _scan(self) -> None:
        """Look on USB for the pumps that are not connected."""
        now = time.monotonic()
        if now < self._next_scan:
            return
        self._next_scan = now + RESCAN_S
        waiting = [pump for pump in self._pumps if pump.tv is None]
        try:
            ports = list(self._list_ports())
        except Exception as exc:                                    # noqa: BLE001
            for pump in waiting:
                self._set_error(pump, f"cannot look for USB ports: {exc}")
            return
        taken = {pump.port for pump in self._pumps if pump.tv is not None}
        candidates = [port for port in ports if port not in taken]

        # Whose pump is on each free port?  A pump that belongs to nobody in
        # config.yaml is asked once a minute, not on every scan.
        serials: Dict[str, str] = {}
        silent: List[str] = []
        probed: List[str] = []
        for port in candidates:
            known = self._strangers.get(port)
            if known is not None and now < known[1]:
                serials[port] = known[0]
                continue
            try:
                serials[port] = self._probe(port)
                probed.append(port)
            except TurbovacLinkError as exc:
                silent.append(str(exc))

        refused: Dict[str, str] = {}                # pump name → why binding failed

        def bind(pump: _Pump, port: str) -> None:
            reason = self._bind(pump, port)
            if reason is None:
                del serials[port]
            else:
                refused[pump.name] = reason
                self._strangers.pop(port, None)     # ask that port afresh next time
                probed[:] = [p for p in probed if p != port]

        # 1. A pinned pump takes the port that reports its serial number.
        for pump in waiting:
            match = [port for port, serial in serials.items() if serial == pump.serial]
            if pump.serial and match:
                bind(pump, match[0])
        # 2. A pump that is not pinned takes the one that is left — if "the
        #    one" is what is left.  A serial number pinned elsewhere is never
        #    up for grabs, whether that pump is connected or not.
        pinned = {pump.serial for pump in self._pumps if pump.serial}
        free = [port for port, serial in serials.items() if serial not in pinned]
        loose = [pump for pump in waiting if not pump.serial]
        if len(loose) == 1 and len(free) == 1:
            bind(loose[0], free[0])

        expiry = now + STRANGER_S
        self._strangers = {
            port: (serial, expiry if port in probed else self._strangers[port][1])
            for port, serial in serials.items()
            if port in probed or port in self._strangers}

        # 3. Say why whoever is still waiting is still waiting.
        others = ", ".join(f"serial {serial or '(none)'} on {port}"
                           for port, serial in sorted(serials.items()))
        for pump in self._pumps:
            if pump.tv is not None:
                continue
            if pump.name in refused:
                why = refused[pump.name]
            elif pump.serial:
                why = f"pump serial {pump.serial} is not on USB"
                if others:
                    why += f" — found instead: {others}"
            elif len(loose) > 1:
                why = (f"{len(loose)} pumps in config.yaml have no serial number — only "
                       "one may go unpinned" + (f" (on USB: {others})" if others else ""))
            elif len(free) > 1:
                why = (f"{len(free)} unassigned TURBOVACs on USB ({others}) — set "
                       f"turbovac.pumps.{pump.name}.serial in config.yaml to say which")
            else:
                why = ("no unassigned TURBOVAC on USB yet — this pump has no serial number "
                       "in config.yaml, so it will take the one that no other entry "
                       "has pinned")
            if silent:
                why += " [" + "; ".join(sorted(silent)) + "]"
            self._set_error(pump, why)

    def _probe(self, port: str) -> str:
        """The serial number of the pump on `port`."""
        tv = Turbovac(self._open_link(port))
        try:
            return tv.pump_serial()
        finally:
            tv.close()

    def _bind(self, pump: _Pump, port: str) -> Optional[str]:
        """Open `port` as `pump`; returns why not, or None when it is bound.

        The serial number is read again here, on the link that will be used,
        so what is bound is what was checked — whatever `_scan` believed.
        """
        try:
            tv = Turbovac(self._open_link(port))
        except TurbovacLinkError as exc:
            return str(exc)
        try:
            identity = tv.identify()
        except TurbovacLinkError as exc:
            tv.close()
            return str(exc)
        serial = identity.pump_serial
        if pump.serial and serial != pump.serial:
            tv.close()
            return f"{port} now reports pump serial {serial or '(none)'}"
        if not pump.serial and serial in {other.serial for other in self._pumps if other.serial}:
            tv.close()
            return f"{port} now reports pump serial {serial}, which is pinned elsewhere"
        pump.tv, pump.port, pump.identity = tv, port, identity
        pump.error = None
        pump.slow, pump.next_slow = {}, 0.0
        pump.state, pump.alarm_bits = None, 0
        if pump.counted_on != serial:
            pump.counted, pump.counted_on = None, serial
        self._note(pump, f"connected — {identity.product or 'TURBOVAC'}, pump serial "
                         f"{serial or '(none)'}, on {port}"
                         + ("" if pump.serial else
                            f" (not pinned: set turbovac.pumps.{pump.name}.serial "
                            "in config.yaml)"),
                   logging.INFO if pump.serial else logging.WARNING)
        return None

    def _drop(self, pump: _Pump, reason: str) -> None:
        if pump.tv is not None:
            pump.tv.close()
        pump.tv = pump.port = pump.identity = pump.reading = pump.state = None
        pump.slow, pump.warnings, pump.alarm_bits = {}, [], 0
        pump.error = reason

    def _set_error(self, pump: _Pump, reason: str) -> None:
        if reason != pump.error:                    # log a new reason once
            log.warning("[turbovac] %s not connected: %s", pump.name, reason)
        pump.error = reason

    # ------------------------------------------------------------------
    # Reading one pump
    # ------------------------------------------------------------------

    def _read(self, pump: _Pump) -> None:
        try:
            self._measure(pump)
        except TurbovacLinkError as exc:
            log.warning("[turbovac] lost %s — will look for it again: %s", pump.name, exc)
            self._drop(pump, str(exc))

    def _measure(self, pump: _Pump) -> None:
        tv = pump.tv
        now = time.monotonic()
        volts = tv.read(P_VOLTAGE)
        power = tv.read(P_POWER)
        motor = tv.read(P_MOTOR_TEMP, signed=True)
        warning_bits = tv.read(P_WARNING_BITS)
        st = tv.status()                # last, so the process data are the newest
        zsw = st.status_word

        state = state_of(zsw)
        if state != pump.state:
            if pump.state is not None:
                self._note(pump, f"{pump.state} → {state} at {st.frequency_hz} Hz",
                           logging.WARNING if state == "error" else logging.INFO)
            pump.state = state
        pump.warnings = warning_text(warning_bits)

        # The error memory and the counters move rarely; read them now and
        # then, and at once when the pump raises or clears an error or warning.
        alarm_bits = (zsw & (ZSW_ERROR | _ZSW_ANY_WARNING)) | ((warning_bits or 0) << 16)
        if alarm_bits != pump.alarm_bits or now >= pump.next_slow:
            pump.alarm_bits = alarm_bits
            self._read_slow(pump)
            pump.next_slow = now + self._cfg.slow_interval_s

        reading = {
            "state":            state,
            "frequency_hz":     st.frequency_hz,
            "current_a":        round(st.current_a, 1),
            "voltage_v":        round(volts / 10.0, 1) if volts is not None else st.voltage_v,
            "bearing_temp_c":   st.bearing_temp_c,
            "converter_temp_c": st.converter_temp_c,
            "drive_on":         int(bool(zsw & ZSW_DRIVE_ON)),
            "normal":           int(bool(zsw & ZSW_NORMAL)),
            "turning":          int(bool(zsw & ZSW_TURNING)),
            "accelerating":     int(bool(zsw & ZSW_ACCELERATING)),
            "decelerating":     int(bool(zsw & ZSW_DECELERATING)),
            "fault":            int(bool(zsw & ZSW_ERROR)),
            "warning":          int(bool(zsw & _ZSW_ANY_WARNING) or bool(warning_bits)),
            "remote":           int(bool(zsw & ZSW_REMOTE)),
            "status_word":      zsw,
        }
        if power is not None:
            reading["power_w"] = round(power / 10.0, 1)
        if motor is not None:
            reading["motor_temp_c"] = motor
        if warning_bits is not None:
            reading["warning_bits"] = warning_bits
        for key in ("setpoint_hz", "operating_hours", "start_count", "error_count",
                    "last_error_code"):
            if pump.slow.get(key) is not None:
                reading[key] = pump.slow[key]
        pump.reading = reading
        pump.last_read_utc = _utc_now()

    def _read_slow(self, pump: _Pump) -> None:
        tv = pump.tv
        code = tv.read(P_ERROR_CODE, 0)
        slow = {
            "setpoint_hz":       tv.read(P_SETPOINT),
            "operating_hours":   _hours(tv.read(P_OPERATING_HOURS, signed=True)),
            "electronics_hours": _hours(tv.read(P_ELECTRONICS_HOURS, signed=True)),
            "start_count":       tv.read(P_STARTS),
            "error_count":       tv.read(P_ERROR_COUNT),
            "last_error_code":   code,
            "last_error":        error_text(code),
            "last_error_hz":     tv.read(P_ERROR_FREQUENCY, 0) if code else None,
            "last_error_hours":  _hours(tv.read(P_ERROR_HOURS, 0, signed=True)) if code else None,
        }
        pump.slow = slow
        count = slow["error_count"]
        if count is None:
            return
        if pump.counted is not None and count > pump.counted:
            new = count - pump.counted
            self._note(pump, f"the pump logged {new} new error{'s' if new > 1 else ''} — "
                             f"latest: {code} ({slow['last_error']}) at "
                             f"{slow['last_error_hz']} Hz", logging.WARNING)
        pump.counted = count

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------

    def _note(self, pump: _Pump, text: str, level: int = logging.INFO) -> None:
        """Log a notable event and keep it for the Control page."""
        pump.event, pump.event_utc = text, _utc_now()
        log.log(level, "[turbovac] %s: %s", pump.name, text)

    def _publish(self, pump: _Pump) -> None:
        ident = pump.identity
        status = {
            "connected":         pump.tv is not None,
            "label":             pump.label,
            "pinned":            bool(pump.serial),
            "configured_serial": pump.serial or None,
            "port":              pump.port,
            "error":             pump.error,
            "event":             pump.event,
            "event_utc":         pump.event_utc,
            "last_read_utc":     pump.last_read_utc,
            "product":           ident.product if ident else None,
            "pump_serial":       ident.pump_serial if ident else None,
            "catalog_no":        ident.catalog_no if ident else None,
            "converter_serial":  ident.converter_serial if ident else None,
            "converter_type":    ident.converter_type if ident else None,
            "firmware":          ident.firmware if ident else None,
            "drive_firmware":    ident.drive_firmware if ident else None,
            "warnings":          list(pump.warnings),
        }
        for key in ("setpoint_hz", "operating_hours", "electronics_hours", "start_count",
                    "error_count", "last_error_code", "last_error", "last_error_hz",
                    "last_error_hours"):
            status[key] = pump.slow.get(key)
        self._mqtt.publish_status("turbo", pump.name, payload=status)
        # Nothing on the sensor topic while the pump is away: the registry
        # lets those states go stale rather than show numbers nobody measured.
        if pump.reading is not None:
            self._mqtt.publish_sensor("turbo", pump.name, payload=pump.reading)
            pump.reading = None
