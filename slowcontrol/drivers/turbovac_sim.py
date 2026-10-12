"""
A stand-in TURBOVAC 90 i — no hardware needed.

Answers the reads `slowcontrol.drivers.turbovac` sends the way the GHS pump
did when it was read over USB on 2026-10-11 (firmware 2.10.01), so the driver
and the state registry can be exercised on a desk:

  • every reply carries the process data, whatever was asked; the voltage
    there is in whole volts while parameter 4 has it in 0.1 V
  • 16-bit values answer with reply code 1 (4 for an element of an indexed
    parameter), the hour counters with 2 (5)
  • a parameter it does not have, an unindexed parameter read as a field, and
    an index past the end are refused with reply code 7 — never with silence
  • an indexed parameter read without an index gives its element 0
  • identification strings are one character per index, zero-padded to 18
  • status word: bit 9 always; bits 2 and 8 while the drive is on; bit 10 at
    90 % of the setpoint and above; bit 11 while the rotor turns; bit 15
    while a bus master has control
  • a telegram that is damaged gets no answer at all

Who starts and stops it follows the manual (sections 4.3 and 5, parameters
179 / 182) — this part has NOT been checked against a real pump yet:

  • the stand's start input, `x1_start`, runs the pump unless a bus master
    has control
  • a telegram whose control word has bit 10 set gives the bus control; bit 0
    of that word is then start / stop
  • 10 s without such a telegram and control goes back to the stand
  • switching the supply off and on (`power_off()` / `power_on()`) clears
    the control bits, and takes the USB port away while it is off
  • the status word in a reply is the state before that telegram took effect

It also keeps every telegram it is sent.  `violations` lists any that was
something other than a read carrying one of the three control words the
driver may send (0x0000, 0x0400, 0x0401), and `control_words` says which of
those it has seen — the tests use both to hold the driver to what it claims.

Drive it from a test: `start()`, `at_speed()`, `stop()`, `standstill()`,
`trip(code)`, `log_error(code)`, `unplug()`, `plug()`, `power_off()`,
`power_on()`, `settle()`, `garble(n)`, `mute()`.  With `dynamics = True` the
rotor speed also moves by itself between telegrams, for a live demo.
"""

from __future__ import annotations

import struct
from typing import Dict, List, Set

from slowcontrol.drivers import turbovac as _tv
from slowcontrol.drivers.turbovac import (FRAME_LEN, LGE, STX, TEXT_LEN, TurbovacLinkError,
                                          _bcc)

_WIDE = (176, 184, 682)             # parameters answered as 32-bit values
_ALLOWED_CONTROL = (0x0000, 0x0400, 0x0401)
WATCHDOG_S = 10.0                   # parameter 182 as found on the real pumps
RUN_UP_HZ_PER_S = 9.0               # only with `dynamics`: about 2¼ min to 1200 Hz
RUN_DOWN_HZ_PER_S = 5.0             # … and about 4 min back down


def _text(s: str) -> List[int]:
    return [ord(c) for c in s] + [0] * (TEXT_LEN - len(s))


class SimTurbovac:
    def __init__(self, pump_serial: str = "31003077799", product: str = "TMP T 90i",
                 catalog_no: str = "810041V1000", converter_serial: str = "9801750017"):
        self.plugged = True
        self.log: List[bytes] = []          # every telegram received
        self._garble = 0
        self._muted = False

        # the pump as it was found: at full speed, no error, no warning,
        # started by its stand
        self.setpoint = 1200
        self.frequency = 1200
        self.drive_on = True
        self.trend = 0                      # +1 gaining speed, -1 losing it
        self.fault = False
        self.motor_current = 0.8            # A
        self.converter_temp = 33
        self.bearing_temp = 38
        self.motor_temp = 56
        self.supply = 23.8                  # V
        self.power = 12.9                   # W
        self.warning_bits = 0
        self.other_master = False           # some other bus master has control
        self.deaf = False                   # ignores control words (for a test of that)
        self.missing: set = set()           # parameters this pump "does not have"

        # who has it
        self.x1_start = True                # the stand says run
        self.remote = False                 # a bus master (us) has control
        self._remote_start = False
        self._remote_at = 0.0
        self._commanded = True              # the start command in force
        self.dynamics = False
        self._moved_at = _tv.time.monotonic()

        # scalars are ints, indexed parameters are lists
        self.params: Dict[int, object] = {
            1: 190,                         # TURBOVAC 90/250 i
            2: 21001,
            17: 100, 18: 1200, 19: 750, 20: 700, 25: 90,
            38: 108,
            40: 8,
            150: 800,
            171: [101, 101, 106, 101, 61, 106, 101, 2] + [0] * 246,
            174: [1081, 1164, 988, 1174, 1200, 731, 1152, 115] + [0] * 246,
            176: [651144, 608673, 395664, 395664, 384122, 362024, 362024, 53] + [0] * 246,
            184: 656050,
            313: _text(product),
            315: _text(converter_serial),
            350: _text(catalog_no),
            355: _text(pump_serial),
            682: 1119878,
            1100: 30300,
        }

    # ── what a test can do to it ───────────────────────────────────────────

    def start(self, frequency: int = 300) -> None:
        self.drive_on, self.fault, self.trend, self.frequency = True, False, +1, frequency
        self.params[38] += 1

    def at_speed(self) -> None:
        self.drive_on, self.trend, self.frequency = True, 0, self.setpoint

    def stop(self, frequency: int = 900) -> None:
        self.drive_on, self.trend, self.frequency = False, -1, frequency

    def standstill(self) -> None:
        self.drive_on, self.trend, self.frequency = False, 0, 0

    def log_error(self, code: int) -> None:
        """A new entry in the error memory, as the pump makes one."""
        self.params[171] = [code] + self.params[171][:-1]
        self.params[174] = [self.frequency] + self.params[174][:-1]
        self.params[176] = [self.params[184]] + self.params[176][:-1]
        self.params[40] += 1

    def trip(self, code: int) -> None:
        """An error that switches the drive off."""
        self.log_error(code)
        self.fault, self.drive_on, self.trend = True, False, -1

    def unplug(self) -> None:
        self.plugged = False

    def plug(self) -> None:
        self.plugged = True

    def power_off(self) -> None:
        """The pump's supply is switched off: the rotor coasts, the control
        bits are gone, and so is the USB port."""
        self.plugged = False
        self.remote = self._remote_start = False
        self._commanded = False
        self.drive_on, self.trend = False, (-1 if self.frequency else 0)

    def power_on(self) -> None:
        """Supply back: the port returns and the stand has the pump."""
        self.plugged = True
        self.settle()

    def garble(self, n: int = 1) -> None:
        """Damage the next `n` replies."""
        self._garble = n

    def mute(self, muted: bool = True) -> None:
        """On USB, but not answering."""
        self._muted = muted

    def settle(self) -> None:
        """Bring the pump up to date with the clock: the watchdog, whoever has
        control now, and (with `dynamics`) the rotor."""
        now = _tv.time.monotonic()
        if self.remote and now - self._remote_at > WATCHDOG_S:
            self.remote = False             # nobody has spoken for it: back to the stand
        want = self._remote_start if self.remote else self.x1_start
        if want != self._commanded:
            self._commanded = want
            if not want:
                self.drive_on = False
                self.trend = -1 if self.frequency else 0
            elif not self.fault:            # an error has to be cleared at the stand first
                self.drive_on, self.trend = True, (+1 if self.frequency < self.setpoint else 0)
                if not self.frequency:
                    self.params[38] += 1
        if self.dynamics:
            dt = max(0.0, now - self._moved_at)
            if self.drive_on and self.frequency < self.setpoint:
                self.frequency = min(self.setpoint, round(self.frequency + RUN_UP_HZ_PER_S * dt))
            elif not self.drive_on and self.frequency > 0:
                self.frequency = max(0, round(self.frequency - RUN_DOWN_HZ_PER_S * dt))
            if self.drive_on:
                self.trend = +1 if self.frequency < self.setpoint else 0
            else:
                self.trend = -1 if self.frequency else 0
            self.motor_current = 3.5 if self.trend > 0 else 0.8 if self.drive_on else 0.0
            self.power = round(self.motor_current * 16.0, 1)
        self._moved_at = now

    @property
    def violations(self) -> List[str]:
        """Telegrams that were anything other than a read carrying one of the
        control words the driver is allowed to send."""
        return [t.hex(" ") for t in self.log
                if (t[3] >> 4) not in (0, 1, 6) or t[2] or t[5]
                or any(t[7:11]) or any(t[13:23])
                or struct.unpack_from(">H", t, 11)[0] not in _ALLOWED_CONTROL]

    @property
    def control_words(self) -> Set[int]:
        """Every control word this pump has been sent."""
        return {struct.unpack_from(">H", t, 11)[0] for t in self.log}

    # ── the link interface ─────────────────────────────────────────────────

    def exchange(self, telegram: bytes) -> bytes:
        if not self.plugged:
            raise TurbovacLinkError("sim: device disconnected")
        self.log.append(bytes(telegram))
        if (self._muted or len(telegram) != FRAME_LEN or telegram[0] != STX
                or telegram[1] != LGE or _bcc(telegram) != telegram[-1]):
            raise TurbovacLinkError("sim: no reply")
        self.settle()
        pke, = struct.unpack_from(">H", telegram, 3)
        access, pnu, index = pke >> 12, pke & 0x7FF, telegram[6]
        code, value = self._answer(access, pnu, index)
        reply = bytearray(FRAME_LEN)
        reply[0], reply[1] = STX, LGE
        struct.pack_into(">H", reply, 3, (code << 12) | (pnu if access else 0))
        reply[6] = index
        # The status word is taken before this telegram's control word is
        # acted on: the reply to "stop" still says the drive is on.
        struct.pack_into(">IHHhHhH", reply, 7, value & 0xFFFFFFFF, self._status_word(),
                         self.frequency, self.converter_temp, round(self.motor_current * 10),
                         self.bearing_temp, round(self.supply))
        reply[-1] = _bcc(reply)
        control, = struct.unpack_from(">H", telegram, 11)
        if control & (1 << 10) and not self.deaf:
            self.remote, self._remote_at = True, _tv.time.monotonic()
            self._remote_start = bool(control & 1)
            self.settle()
        if self._garble:
            self._garble -= 1
            reply[9] ^= 0x40                # the checksum no longer fits
        return bytes(reply)

    def close(self) -> None:
        pass

    # ── internals ──────────────────────────────────────────────────────────

    def _live(self) -> Dict[int, int]:
        return {3: self.frequency, 4: round(self.supply * 10), 5: round(self.motor_current * 10),
                6: round(self.power * 10), 7: self.motor_temp, 11: self.converter_temp,
                24: self.setpoint, 125: self.bearing_temp, 227: self.warning_bits}

    def _answer(self, access: int, pnu: int, index: int):
        """(reply code, value) for one parameter access."""
        if access not in (1, 6):
            return 0, 0                     # a status poll — or something it should not get
        value = self._live().get(pnu, self.params.get(pnu))
        if value is None or pnu in self.missing:
            return 7, 0                     # no such parameter
        wide = pnu in _WIDE
        if isinstance(value, list):
            if access == 1:
                return (7, 3) if index else (2 if wide else 1, value[0])
            if index >= len(value):
                return 7, 3                 # faulty index
            return (5 if wide else 4), value[index]
        if access == 6:
            return 7, 5                     # not an indexed parameter
        if index:
            return 7, 3
        return (2 if wide else 1), value

    def _status_word(self) -> int:
        word = 1 << 9
        if self.fault:
            word |= 1 << 3
        elif self.drive_on:
            word |= (1 << 2) | (1 << 8)
            if self.frequency >= 0.9 * self.setpoint:
                word |= 1 << 10
        else:
            word |= 1 << 0                  # ready for operation
        if self.trend > 0:
            word |= 1 << 4
        elif self.trend < 0 and self.frequency > 0:
            word |= 1 << 5
        if self.frequency > 0:
            word |= 1 << 11
        if self.warning_bits:
            word |= 1 << 14
            if self.warning_bits & (1 << 11):
                word |= 1 << 13
        if self.remote or self.other_master:
            word |= 1 << 15
        return word
