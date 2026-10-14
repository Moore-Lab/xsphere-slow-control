"""
Leybold TURBOVAC i / iX turbo pumps over USB: read them, start and stop them.

Two turbo pumps serve the experiment, and the frequency converter on each has
a USB port that goes to the Pi:

    ghs    the gas handling system, science chamber included
    outer  the outer (insulation) vacuum

This driver reads them — rotor speed, motor current, power, temperatures,
operating state, warnings, the pump's own error memory — and publishes that.
If `turbovac.allow_control` is set it can also start and stop a pump.

What it can do to a pump, and what it cannot
────────────────────────────────────────────
The converter speaks USS (VDI/VDE 3689): fixed 24-byte telegrams, one request
→ one reply.  It acts on a telegram's control word only if bit 10 of that
word ("enable process data") is set, and changes a setting only for one of
the write access codes.  `read_telegram()` builds every telegram but one.  It
can put three control words on the wire —

    0x0000   nothing: a pure read.  The pump stays with whatever else
             commands it (see "Who commands a pump" below).
    0x0401   bit 10 + bit 0: the bus has control — run.
    0x0400   bit 10 alone:   the bus has control — stop.

— and no write access code.  The one write is `watchdog_telegram()`:
parameter 182 set to 0 or to its factory 100, nothing else, never saved to
the pump's memory.  So from here a pump can be started, stopped and told to
keep that command without us, and that is all: not its speed, not another
parameter, not its outputs, not an error reset.

Each pump has a mode, set on xsphere/commands/turbo/<name>/mode:

    local   read only: every telegram carries 0x0000.  The default, and the
            only mode there is unless `turbovac.allow_control` is true.
            The Control page calls it "release".
    on      held running: every telegram carries 0x0401.
    off     held stopped: every telegram carries 0x0400.

"Every telegram" is the point.  The converter takes each telegram's control
word as the present command, so while a pump is held, the parameter reads
between two status polls have to say the same thing as the polls do.

A mode command is acted on only while the gate is open: "enable control" on
the Control page, xsphere/commands/turbo_control/enable {"enabled": true}.  It
comes up shut on every service start, is not persisted, and shutting it
starts, stops or releases nothing.  The gate is here, not on the page, so it
holds for the Sequencer and anything else on the bus.

Who commands a pump
───────────────────
The converter takes start / stop from one source at a time, in this order:
its RS 485 bus (X104), then USB, then the start input on its X1 socket
(pins 7-8 closed = run, open = stop).  Both pumps stand on Leybold TURBOLAB
Core stations, whose controller is an RS 485 master: with its 9-pin lead
plugged in the Core starts and stops the pump, and everything this driver
sends is ignored (the pump never reports the bus in control, and the status
says so).  The outer pump's lead has been out since 2026-10-12 and the GHS
pump's since 2026-10-14, so both are ours: nothing is plugged into their X1
either, which means "stop" whenever nobody on the bus is holding them.  Seen
on the outer pump: a held command takes effect within the poll, and a
released pump lets go 10 s later.

Holding a pump is not locking it — unless it keeps the command
───────────────────────────────────────────────────────────────
A converter that stops hearing bit 10 — silence, or 0x0000 — hands control
back to the next source down after parameter 182 (10 s as delivered;
fallback word 179 = 0).  So when the Pi, the service or the USB cable goes
away, a pump held OFF does what is below — if that says run, it starts again —
and a pump held ON carries on only if that says run.

`turbovac.pumps.<name>.keep_command` changes that for one pump: while it is
held, the service sets its parameter 182 to 0, which the manual describes as
"a change of the control right is inhibited" — the pump keeps the last
command it was given, running or stopped, for as long as nobody says
otherwise.  Released, it gets its 100 back first, while the hold still
stands, and only then hears 0x0000.  Only a pump that is written to this way
is ever written to; one on its TURBOLAB's lead must not be (the watchdog also
governs the Core's own control).  The value is volatile: a supply cycle
brings back the saved 10 s, and clears the command with it.

When the service gets a pump back — after its own restart, or a USB glitch —
it carries on holding if it was out of touch for less than
`turbovac.resume_max_s`, or if it is a keep_command pump that still reports
the bus in control (it never stopped obeying).  Otherwise the mode drops to
local and the status says why.  The mode, the pump it was set on and the time
of the last contact are kept in slowcontrol/turbovac_state.json for this.

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
24 V supply — and, with that cut, off the rotor, which drives the motor as a
generator while it runs down (manual section 4.4).  So a pump whose supply is
cut stays on USB, reading "decelerating", until the rotor is slow; then it is
reported as "not connected", and picked up again when it returns.

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
  • Stopped in mid run-up, a pump keeps "accelerating" (status bit 4) set for
    some 20 s with its drive off and the rotor slowing; just arrived at speed
    it shows "accelerating" and "normal" together for ~40 s (outer pump,
    2026-10-12).  `state_of()` reads bit 4 only with the drive on.
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
         error_count, last_error_code, watchdog_s}
    xsphere/status/turbo/{name}    every poll (retained):
        {connected, label, pinned, configured_serial, port, error, event,
         event_utc, last_read_utc, product, pump_serial, catalog_no,
         converter_serial, converter_type, firmware, drive_firmware,
         warnings, setpoint_hz, operating_hours, electronics_hours,
         start_count, error_count, last_error_code, last_error,
         last_error_hz, last_error_hours, mode, mode_utc, control_allowed,
         control_enabled, keeps_command, watchdog_s}
    xsphere/status/turbo_control   on connect, with every poll and on change
                                   (retained):
        {enabled, since_utc, refused, control_allowed, last_refused}
  Subscribe (only if `turbovac.allow_control`):
    xsphere/commands/turbo/{name}/mode       {"mode": "on" | "off" | "local"}
    xsphere/commands/turbo_control/enable    {"enabled": true | false | 1 | 0}

  state: stopped | accelerating | normal | running | decelerating | coasting
         | error          ("running" = drive on, below normal-operation speed
                           and neither gaining nor losing it)
  mode:  local | on | off     (who has the pump: its stand, or this service
                               holding it on / off)
  remote: 1 while the pump itself reports the bus (USB) in control
  watchdog_s: parameter 182 — 10 as delivered, 0 while a keep_command pump
              is held

Config: the `turbovac:` block of config.yaml (TurbovacConfig in core/config.py).
"""

from __future__ import annotations

import json
import logging
import os
import re
import struct
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional

from slowcontrol.core.mqtt import command_topic
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
# 2, 3, 7 and 8 — the writes.  Of those only 2 (a 16-bit value) is ever built,
# and only by watchdog_telegram(), for one parameter and two values.
ACCESS_NONE = 0                         # no parameter: a bare status poll
ACCESS_READ = 1
ACCESS_WRITE = 2                        # write a 16-bit value
ACCESS_READ_FIELD = 6                   # one element of an indexed parameter
_READS = (ACCESS_NONE, ACCESS_READ, ACCESS_READ_FIELD)

# Control word (manual section 4.3).  Two of its bits are used, in three
# combinations; the rest — speed setpoint enable, error reset, standby, the
# relay and 24 V outputs — are never set.
STW_START = 1 << 0
STW_REMOTE = 1 << 10                    # "enable process data": the bus has control
CONTROL_LOCAL = 0x0000                  # a pure read; the stand keeps the pump
CONTROL_STOP = STW_REMOTE               # 0x0400
CONTROL_START = STW_REMOTE | STW_START  # 0x0401
_CONTROL_WORDS = (CONTROL_LOCAL, CONTROL_STOP, CONTROL_START)
#: The control word that goes with each mode a pump can be put in.
MODES: Dict[str, int] = {"local": CONTROL_LOCAL, "off": CONTROL_STOP, "on": CONTROL_START}
# What each mode command is called on the Control page.
_COMMAND_WORD = {"on": "ON", "off": "OFF", "local": "RELEASE"}

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
ZSW_REMOTE = 1 << 15                    # a bus master holds control
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
P_WATCHDOG = 182                        # 0.1 s; how long the bus may go quiet
P_OPERATING_HOURS = 184                 # 0.01 h with the pump running
P_WARNING_BITS = 227
P_PRODUCT = 313                         # text
P_CONVERTER_SERIAL = 315                # text
P_CATALOG_NO = 350                      # text
P_PUMP_SERIAL = 355                     # text — what tells two pumps apart
P_ELECTRONICS_HOURS = 682               # 0.01 h powered
P_DRIVE_FIRMWARE = 1100

TEXT_LEN = 18                           # elements in an identification string

# Parameter 182, the USS control watchdog: once the bus master that has the
# pump stops sending bit 10 — it falls silent, or says "local" — control goes
# back to the next source down (the TURBOLAB, else the X1 start input) after
# this long.  0 means never: the pump keeps the last command it was given.
# Both pumps came with 100 (10 s).  The write is volatile — parameter 8, which
# would save it, is never written — so a power cycle brings back the 10 s.
WATCHDOG_FACTORY = 100                  # 10.0 s
WATCHDOG_KEEP = 0                       # keep the last command, however long
_WATCHDOG_VALUES = (WATCHDOG_KEEP, WATCHDOG_FACTORY)

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
SAVE_EVERY_S = 30.0         # how often a held pump's time of last contact goes to disk
UNHEEDED_POLLS = 3          # polls a held pump may go without reporting bus control


class TurbovacLinkError(Exception):
    """Talking to a pump failed — its port should be closed and found again."""


class TurbovacFrameError(TurbovacLinkError):
    """A reply arrived damaged or out of step; worth one more try."""


def _bcc(frame: bytes) -> int:
    x = 0
    for byte in frame[:FRAME_LEN - 1]:
        x ^= byte
    return x


def read_telegram(access: int = ACCESS_NONE, pnu: int = 0, index: int = 0,
                  control: int = CONTROL_LOCAL) -> bytes:
    """The 24-byte telegram for one read, carrying one of three control words.

    The parameter part can only read: no write access code is accepted, and
    the parameter value (bytes 7-10) is always zero.  The control word (bytes
    11-12) is 0x0000, 0x0400 or 0x0401 and nothing else — a read that leaves
    the pump alone, or one that also tells it to stop or to run.  The
    frequency setpoint and the rest of the process data (bytes 13-22) are
    always zero.
    """
    if access not in _READS:
        raise ValueError(f"access code {access} is not a read")
    if not 0 <= pnu <= 0x7FF or not 0 <= index <= 0xFF:
        raise ValueError(f"no such parameter: {pnu}[{index}]")
    # Exactly an int: to Python, False == 0 and 1025.0 == 0x0401.
    if type(control) is not int or control not in _CONTROL_WORDS:
        raise ValueError(f"control word {control!r} is not one this driver sends")
    frame = bytearray(FRAME_LEN)
    frame[0], frame[1] = STX, LGE       # frame[2], the address, is 0 on USB
    struct.pack_into(">H", frame, 3, (access << 12) | pnu)
    frame[6] = index
    struct.pack_into(">H", frame, 11, control)
    frame[FRAME_LEN - 1] = _bcc(frame)
    return bytes(frame)


def watchdog_telegram(value: int, control: int = CONTROL_LOCAL) -> bytes:
    """The one write this driver can build: parameter 182 set to 0 (keep the
    last command) or to its factory 100 (10 s).

    Like a read in every other respect: the same three control words, no
    index, no setpoint, no other process data.  The value goes in the low
    word of the parameter value (bytes 9-10), where a 16-bit value travels.
    """
    if type(value) is not int or value not in _WATCHDOG_VALUES:
        raise ValueError(f"parameter {P_WATCHDOG} is only ever set to "
                         f"{' or '.join(map(str, _WATCHDOG_VALUES))}, not {value!r}")
    if type(control) is not int or control not in _CONTROL_WORDS:
        raise ValueError(f"control word {control!r} is not one this driver sends")
    frame = bytearray(FRAME_LEN)
    frame[0], frame[1] = STX, LGE
    struct.pack_into(">H", frame, 3, (ACCESS_WRITE << 12) | P_WATCHDOG)
    struct.pack_into(">I", frame, 7, value)
    struct.pack_into(">H", frame, 11, control)
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
    drive_on = status_word & ZSW_DRIVE_ON
    # Stopped in mid run-up, the pump leaves "accelerating" set for some 20 s
    # with its drive off and the rotor slowing — so that bit needs the drive.
    if status_word & ZSW_ACCELERATING and drive_on:
        return "accelerating"
    if status_word & ZSW_DECELERATING:
        return "decelerating"
    if drive_on:
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
    """One TURBOVAC i / iX over an open link: reads, and the control word
    that goes out with every one of them."""

    def __init__(self, link):
        self._link = link
        self.control = CONTROL_LOCAL

    def hold(self, control: int) -> None:
        """Set the control word every telegram carries from now on.  The pump
        hears of it with the next telegram, whatever that telegram asks."""
        if type(control) is not int or control not in _CONTROL_WORDS:
            raise ValueError(f"control word {control!r} is not one this driver sends")
        self.control = control

    def _once(self, telegram: bytes, access: int, pnu: int, index: int) -> Reply:
        reply = parse_reply(self._link.exchange(telegram))
        if access != ACCESS_NONE and (reply.pnu, reply.index) != (pnu, index):
            raise TurbovacFrameError(f"asked for parameter {pnu}[{index}], the reply "
                                     f"is about {reply.pnu}[{reply.index}]")
        return reply

    def _send(self, telegram: bytes, access: int, pnu: int, index: int) -> Reply:
        try:
            return self._once(telegram, access, pnu, index)
        except TurbovacFrameError as exc:
            # Sending it again is safe: a read, or a write of the same value.
            log.debug("[turbovac] %s — asking once more", exc)
            time.sleep(RETRY_PAUSE_S)
            return self._once(telegram, access, pnu, index)

    def _ask(self, access: int, pnu: int = 0, index: int = 0) -> Reply:
        return self._send(read_telegram(access, pnu, index, self.control), access, pnu, index)

    def set_watchdog(self, value: int) -> Optional[int]:
        """Set parameter 182 to 0 or 100; returns the value the pump answers
        with (None if it refused — reply code 7 or 8)."""
        return self._send(watchdog_telegram(value, self.control),
                          ACCESS_WRITE, P_WATCHDOG, 0).number()

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

    def identify(self, pump_serial: Optional[str] = None) -> Identity:
        """Everything that says which pump this is (`pump_serial` if it has
        just been read and need not be read again)."""
        return Identity(
            pump_serial=self.pump_serial() if pump_serial is None else pump_serial,
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


def _age_text(seconds: float) -> str:
    if seconds < 90:
        return f"{seconds:.0f} s"
    if seconds < 5400:
        return f"{seconds / 60:.0f} min"
    return f"{seconds / 3600:.1f} h"


class _Pump:
    """One pump named in config.yaml, and what is known about it right now."""

    def __init__(self, name: str, cfg):
        self.name = name
        self.label = cfg.label or name
        self.serial = cfg.serial                    # "" = not pinned
        # keep_command: while held, its watchdog (parameter 182) is set to 0,
        # so the hold outlasts the service; `watchdog` is the value last read.
        self.keep = bool(getattr(cfg, "keep_command", False))
        self.watchdog: Optional[int] = None
        self.watchdog_trouble: Optional[str] = None # why it could not be set, as last said
        # Who has the pump: "local" (its stand; we only read), or this service
        # holding it "on" / "off".  A hold belongs to the pump it was set on
        # (`held_serial`) and is only carried across a gap in contact shorter
        # than turbovac.resume_max_s (`last_contact`, epoch seconds).
        self.mode = "local"
        self.mode_utc: Optional[str] = None
        self.held_serial: Optional[str] = None
        self.last_contact: Optional[float] = None
        self.unheeded = 0                           # polls held without the pump saying so
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
                 open_link: Optional[Callable[[str], object]] = None,
                 state_path: Optional[str] = None):
        super().__init__(config, mqtt)
        self._cfg = config.turbovac
        self._list_ports = list_ports or usb_ports
        self._open_link = open_link or (lambda port: SerialLink(port, self._cfg.timeout_s))
        self._state_path = state_path or os.path.normpath(os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "..", "turbovac_state.json"))
        # The poll thread and the MQTT callback thread both talk to the pumps,
        # and a telegram and its reply must not be interleaved with another.
        self._io = threading.RLock()
        self._pumps: List[_Pump] = [_Pump(str(name), pump_cfg)
                                    for name, pump_cfg in self._cfg.pumps.items()]
        self._next_scan = 0.0
        # Ports whose pump belongs to nobody in config.yaml:
        # port → (pump serial, when to ask again)
        self._strangers: Dict[str, tuple] = {}
        self._last_save = 0.0
        # The gate in front of every mode command (on / off / local): they are
        # acted on only while it is open.  It comes up shut on every service
        # start and is not persisted; shutting it changes nothing on a pump.
        # What brings a hold back after a restart (`_resume`) is not a command
        # and does not need it.  Written on the MQTT thread under `_io`.
        self._control_enabled = False
        self._control_enabled_utc: Optional[str] = None
        self._refused = 0                           # mode commands refused since start
        self._last_refused: Optional[dict] = None   # {"pump", "mode", "utc"}
        if self._cfg.allow_control:
            self._load_state()

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
            log.info("[turbovac] %s — %s: %s%s", pump.name, pump.label,
                     f"pump serial {pump.serial}" if pump.serial
                     else "not pinned (takes the one unassigned TURBOVAC on USB)",
                     "; keeps its command without the service while held"
                     if pump.keep and self._cfg.allow_control else "")
        if self._cfg.allow_control:
            self._mqtt.subscribe(command_topic("turbo", "+", "mode"), self._on_mode)
            self._mqtt.subscribe(command_topic("turbo_control", "enable"), self._on_control_enable)
            log.info("[turbovac] start / stop from the Control page is allowed — once "
                     "\"enable control\" is pressed there")
        else:
            log.info("[turbovac] read-only (turbovac.allow_control is off)")
        # Retained, so the Control page locks the buttons from the moment we are up.
        self._publish_control_status()

    def disconnect(self) -> None:
        # Nothing is sent to a pump on the way out.  A held pump hears no more
        # from us: a keep_command one carries on as it was told, any other goes
        # back to its stand by itself 10 s later — unless the service is back
        # before that and carries on (see `_bind`).
        with self._io:
            self._save_state()
            for pump in self._pumps:
                self._drop(pump, "service stopped")
                self._publish(pump)

    # ------------------------------------------------------------------
    # Poll
    # ------------------------------------------------------------------

    def poll(self) -> None:
        with self._io:
            try:
                if any(pump.tv is None for pump in self._pumps):
                    self._scan()
                for pump in self._pumps:
                    if pump.tv is not None:
                        self._read(pump)
                held = any(pump.mode != "local" for pump in self._pumps)
                if held and time.monotonic() - self._last_save > SAVE_EVERY_S:
                    self._save_state()
            except Exception as exc:                                # noqa: BLE001
                # Contained here on purpose.  Left to the base class, a fault
                # that repeats every poll reads as a stuck poll thread, and the
                # service watchdog would restart the whole slow control — PID
                # feed and PV interlock included — over a pump read-out.
                log.exception("[turbovac] unexpected error — dropping the pump links")
                for pump in self._pumps:
                    self._drop(pump, f"internal error: {exc!r}")
                self._next_scan = time.monotonic() + BACKOFF_S
            try:
                for pump in self._pumps:
                    self._publish(pump)
                # every poll, so the registry tells a live "disabled" from a dead driver
                self._publish_control_status()
            except Exception:                                       # noqa: BLE001
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
            serial = tv.pump_serial()
        except TurbovacLinkError as exc:
            tv.close()
            return str(exc)
        if pump.serial and serial != pump.serial:
            tv.close()
            return f"{port} now reports pump serial {serial or '(none)'}"
        if not pump.serial and serial in {other.serial for other in self._pumps if other.serial}:
            tv.close()
            return f"{port} now reports pump serial {serial}, which is pinned elsewhere"
        # A keep_command pump that still reports the bus in control has kept
        # the hold by itself, however long we were away.  The question goes
        # out as a bare read: with its watchdog at 0 that does not make it let
        # go, and a pump that has lost the command (its supply was cycled) has
        # nothing to let go of.
        kept = False
        if pump.mode != "local" and pump.keep and self._cfg.allow_control:
            try:
                kept = bool(tv.status().status_word & ZSW_REMOTE)
            except TurbovacLinkError as exc:
                tv.close()
                return str(exc)
        # Now that it is known to be this pump: if it was being held on or off
        # a moment ago, say so again before anything else is asked of it, so
        # that the stretch it spends hearing no control word stays short.
        resumed = self._resume(pump, serial, kept)
        if resumed is not None and pump.mode != "local":
            tv.hold(MODES[pump.mode])
        try:
            identity = tv.identify(serial)
            watchdog = self._sync_watchdog(pump, tv)
        except TurbovacLinkError as exc:
            tv.close()
            return str(exc)
        pump.tv, pump.port, pump.identity = tv, port, identity
        pump.error = None
        pump.slow, pump.next_slow = {}, 0.0
        pump.state, pump.alarm_bits, pump.unheeded = None, 0, 0
        if pump.counted_on != serial:
            pump.counted, pump.counted_on = None, serial
        self._note(pump, f"connected — {identity.product or 'TURBOVAC'}, pump serial "
                         f"{serial or '(none)'}, on {port}"
                         + ("" if pump.serial else
                            f" (not pinned: set turbovac.pumps.{pump.name}.serial "
                            "in config.yaml)"),
                   logging.INFO if pump.serial else logging.WARNING)
        if resumed is not None:
            self._note(pump, *resumed)
        if watchdog == "set":
            self._note(pump, self._watchdog_news(pump))
        return None

    def _resume(self, pump: _Pump, serial: str, kept: bool = False):
        """Does a hold from before the link was lost still stand?

        `kept`: the pump is a keep_command one and reports the bus still in
        control — it never stopped obeying the hold.  Returns None if the pump
        was not being held, else the (text, level) to report; `pump.mode` is
        "local" afterwards unless the hold carries on.
        """
        if pump.mode == "local":
            return None
        was = pump.mode.upper()
        away = None if pump.last_contact is None else max(0.0, time.time() - pump.last_contact)
        gap = "an unknown time" if away is None else _age_text(away)
        if not self._cfg.allow_control:
            why = "turbovac.allow_control is off"
        elif not serial or pump.held_serial != serial:
            why = f"it was set on pump {pump.held_serial or '?'}, and this is {serial or '?'}"
        elif away is None or away > self._cfg.resume_max_s:
            if kept:
                return (f"holding it {was} again — it kept that by itself through {gap} "
                        "out of touch", logging.INFO)
            why = f"out of touch for {gap}, so its stand has had the pump since"
        else:
            return (f"holding it {was} again after {gap} out of touch", logging.INFO)
        pump.mode, pump.mode_utc, pump.held_serial = "local", _utc_now(), None
        self._save_state()
        return (f"no longer holding it {was}: {why}", logging.WARNING)

    def _drop(self, pump: _Pump, reason: str) -> None:
        # `pump.mode` stays: whether a hold carries on is decided when (and
        # if) the pump is back — see `_resume`.
        if pump.tv is not None:
            pump.tv.close()
        pump.tv = pump.port = pump.identity = pump.reading = pump.state = None
        pump.watchdog = None
        pump.slow, pump.warnings, pump.alarm_bits = {}, [], 0
        pump.error = reason

    # ------------------------------------------------------------------
    # The watchdog (parameter 182) of a keep_command pump
    # ------------------------------------------------------------------

    def _watchdog_wanted(self, pump: _Pump) -> Optional[int]:
        """What parameter 182 should be: 0 while a keep_command pump is held,
        its factory 10 s while it is not.  None: leave it alone — this pump
        is not ours to set (keep_command or allow_control is off)."""
        if not (pump.keep and self._cfg.allow_control):
            return None
        return WATCHDOG_KEEP if pump.mode != "local" else WATCHDOG_FACTORY

    def _sync_watchdog(self, pump: _Pump, tv: Turbovac) -> Optional[str]:
        """Read parameter 182, and set it to `_watchdog_wanted` if it is not.

        Returns "set" when it was written and reads back as wanted, a
        sentence when that failed (said once, until it changes), or None.
        Raises TurbovacLinkError.  Caller holds `_io`.
        """
        have = tv.read(P_WATCHDOG)
        pump.watchdog = have
        want = self._watchdog_wanted(pump)
        if want is None or have == want:
            pump.watchdog_trouble = None
            return None
        if have is None:
            trouble = (f"it gives no parameter {P_WATCHDOG}, so it cannot keep a command "
                       "without the service")
        else:
            tv.set_watchdog(want)
            pump.watchdog = have = tv.read(P_WATCHDOG)
            if have == want:
                pump.watchdog_trouble = None
                return "set"
            trouble = (f"its watchdog (parameter {P_WATCHDOG}) will not go to {want / 10:g} s — "
                       f"it reads {'nothing' if have is None else f'{have / 10:g} s'}")
        if trouble != pump.watchdog_trouble:
            pump.watchdog_trouble = trouble
            self._note(pump, trouble, logging.WARNING)
        return trouble

    def _watchdog_news(self, pump: _Pump) -> str:
        """What a watchdog just set to suit the mode means, in a sentence."""
        if pump.watchdog == WATCHDOG_KEEP:
            return (f"keeps that {pump.mode.upper()} without the service too (its watchdog, "
                    f"parameter {P_WATCHDOG}, is 0)")
        return (f"its watchdog (parameter {P_WATCHDOG}) is back to {(pump.watchdog or 0) / 10:g} s: "
                "it lets go of a command once the service has gone quiet that long")

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

        if pump.mode != "local":
            pump.last_contact = time.time()
            # The pump says for itself whether a bus master has it.  A status
            # word describes the moment before its telegram took effect, so a
            # poll or two of "no" after a command means nothing.
            pump.unheeded = 0 if zsw & ZSW_REMOTE else pump.unheeded + 1
            if pump.unheeded == UNHEEDED_POLLS:
                self._note(pump, f"told to stay {pump.mode.upper()}, but the pump does not "
                                 "report that the bus has control of it", logging.WARNING)

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
        if pump.watchdog is not None:
            reading["watchdog_s"] = pump.watchdog / 10.0
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
        # The watchdog too: read for the status, and put right if it has moved
        # under a held keep_command pump (a supply cycle brings back the 10 s).
        if self._sync_watchdog(pump, tv) == "set":
            self._note(pump, self._watchdog_news(pump))
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
    # Start / stop: the command, and what survives a restart
    # ------------------------------------------------------------------

    def _on_mode(self, topic: str, payload) -> None:
        """xsphere/commands/turbo/{name}/mode  → {"mode": "on" | "off" | "local"}"""
        name = topic.split("/")[-2]
        pump = next((p for p in self._pumps if p.name == name), None)
        if pump is None:
            log.warning("[turbovac] no such pump in %s", topic)
            return
        # Only the three literal words move a pump — anything else off the bus
        # is dropped rather than interpreted.
        mode = payload.get("mode") if isinstance(payload, dict) else None
        if not isinstance(mode, str) or mode not in MODES:
            log.warning("[turbovac] %s: ignoring mode %r", name, mode)
            return
        with self._io:
            # The gate.  Checked after the name and the mode, so what is said
            # is exactly what was asked for and dropped; nothing is queued.
            if not self._control_enabled:
                self._refused += 1
                self._last_refused = {"pump": name, "mode": mode, "utc": _utc_now()}
                self._note(pump, f"{_COMMAND_WORD[mode]} refused — turbo control is disabled: "
                                 "enable it on the Control page first", logging.WARNING)
                self._publish_control_status()
            else:
                self._set_mode(pump, mode)
            self._publish(pump)

    def _on_control_enable(self, topic: str, payload) -> None:
        """xsphere/commands/turbo_control/enable  → {"enabled": true | false}

        Opens or shuts the gate on the mode commands.  `enabled` must be a
        bool or a literal 0 / 1; anything else is dropped.  Shutting it starts,
        stops or releases nothing — it only stops further commands being
        acted on — and it is not remembered across a restart of the service.
        """
        raw = payload.get("enabled") if isinstance(payload, dict) else None
        if isinstance(raw, bool):
            want = raw
        elif type(raw) is int and raw in (0, 1):
            want = bool(raw)
        else:
            log.warning("[turbovac] turbo control: ignoring enabled=%r", raw)
            return
        with self._io:
            if want != self._control_enabled:
                self._control_enabled, self._control_enabled_utc = want, _utc_now()
                log.info("[turbovac] turbo control %s", "ENABLED — on / off / release are now "
                         "acted on" if want else "DISABLED — on / off / release are now refused")
            self._publish_control_status()
            for pump in self._pumps:        # their status carries it too
                self._publish(pump)

    def _set_mode(self, pump: _Pump, mode: str) -> None:
        """Carry out one operator command.  Caller holds `_io`."""
        if mode != "local":
            # Nothing should start or stop later because of a click made while
            # there was no pump to obey it — and a pump is only ever commanded
            # as the one config.yaml names by serial number.
            refusal = ("the pump is not connected" if pump.tv is None
                       else "it is not pinned to a serial number in config.yaml" if not pump.serial
                       else None)
            if refusal:
                self._note(pump, f"{mode.upper()} ignored — {refusal}", logging.WARNING)
                return
        repeated = mode == pump.mode
        pump.mode, pump.mode_utc = mode, _utc_now()
        pump.held_serial = pump.serial if mode != "local" else None
        pump.last_contact = time.time()
        pump.unheeded = 0
        self._save_state()
        text = {"on":    "ON — told to run, and held there",
                "off":   "OFF — told to stop, and held there",
                "local": "released — in about 10 s it goes back to its stand (its TURBOLAB, or "
                         "its own X1 start input: with nothing plugged in there, it stops)"}[mode]
        text += " (again)" if repeated else ""
        if pump.tv is None:
            self._note(pump, text)
            return
        watchdog = None
        ours = self._watchdog_wanted(pump) is not None      # a keep_command pump
        try:
            if mode == "local":
                # A keep_command pump gets its 10 s back first, while the hold
                # still stands — at 0 it would never let go at all.
                if ours:
                    watchdog = self._sync_watchdog(pump, pump.tv)
                pump.tv.hold(MODES[mode])
                pump.tv.status()        # so that it hears of it now, not at the next poll
            else:
                pump.tv.hold(MODES[mode])
                pump.tv.status()
                if ours:
                    watchdog = self._sync_watchdog(pump, pump.tv)
        except TurbovacLinkError as exc:
            self._note(pump, text)
            log.warning("[turbovac] lost %s during a command: %s", pump.name, exc)
            self._drop(pump, str(exc))
            return
        if watchdog == "set" or (pump.keep and pump.watchdog == WATCHDOG_KEEP and mode != "local"):
            text += " — " + self._watchdog_news(pump)
        self._note(pump, text)
        if watchdog not in (None, "set"):
            # Already logged by _sync_watchdog; shown again so the card has it last.
            pump.event, pump.event_utc = watchdog, _utc_now()

    def _load_state(self) -> None:
        try:
            with open(self._state_path) as fh:
                saved = json.load(fh)["pumps"]
            for pump in self._pumps:
                entry = saved.get(pump.name)
                if entry and entry["mode"] in ("on", "off"):
                    pump.mode, pump.mode_utc = entry["mode"], entry.get("mode_utc")
                    pump.held_serial = str(entry["pump_serial"])
                    pump.last_contact = float(entry["last_contact"])
        except FileNotFoundError:
            return
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
            log.warning("[turbovac] cannot use %s (%s) — every pump starts in local mode",
                        self._state_path, exc)
            for pump in self._pumps:
                pump.mode, pump.mode_utc = "local", None
                pump.held_serial = pump.last_contact = None
            return
        held = {pump.name: pump.mode for pump in self._pumps if pump.mode != "local"}
        if held:
            log.info("[turbovac] held when last running: %s — taken up again only if that "
                     "was less than %.0f s ago", held, self._cfg.resume_max_s)

    def _save_state(self) -> None:
        self._last_save = time.monotonic()
        held = {pump.name: {"mode": pump.mode, "mode_utc": pump.mode_utc,
                            "pump_serial": pump.held_serial, "last_contact": pump.last_contact}
                for pump in self._pumps if pump.mode != "local"}
        if not self._cfg.allow_control or (not held and not os.path.exists(self._state_path)):
            return
        tmp = self._state_path + ".tmp"
        try:
            with open(tmp, "w") as fh:
                json.dump({"pumps": held}, fh, indent=2)
                fh.write("\n")
            os.replace(tmp, self._state_path)
        except OSError as exc:
            log.warning("[turbovac] cannot persist the pump modes to %s: %s",
                        self._state_path, exc)

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------

    def _publish_control_status(self) -> None:
        """xsphere/status/turbo_control
               → {"enabled": 0|1, "since_utc": str|None, "refused": int,
                  "control_allowed": bool,
                  "last_refused": {"pump", "mode", "utc"}  (once one has been)}

        `enabled` is the gate in `_on_mode`; `since_utc` is when it was last
        switched (None = shut since the service started); `refused` counts the
        mode commands dropped because it was shut.
        """
        payload = {
            "enabled":         int(self._control_enabled),
            "since_utc":       self._control_enabled_utc,
            "refused":         self._refused,
            "control_allowed": bool(self._cfg.allow_control),
        }
        if self._last_refused is not None:
            payload["last_refused"] = dict(self._last_refused)
        self._mqtt.publish_status("turbo_control", payload=payload)

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
            "mode":              pump.mode,
            "mode_utc":          pump.mode_utc,
            "control_allowed":   bool(self._cfg.allow_control),
            "control_enabled":   self._control_enabled,
            # keep_command in force for this pump, and parameter 182 as last read
            "keeps_command":     bool(pump.keep and self._cfg.allow_control),
            "watchdog_s":        None if pump.watchdog is None else pump.watchdog / 10.0,
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
