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
    90 % of the setpoint and above; bit 11 while the rotor turns
  • a telegram that is damaged gets no answer at all

It also keeps every telegram it is sent, and `violations` lists any that was
not a pure read.  The tests use that to hold the driver to "cannot operate a
pump".  The simulated pump does not act on such a telegram — it has no notion
of being started over the bus — it only tells on it.

Drive it from a test: `start()`, `at_speed()`, `stop()`, `standstill()`,
`trip(code)`, `log_error(code)`, `unplug()`, `plug()`, `garble(n)`, `mute()`.
"""

from __future__ import annotations

import struct
from typing import Dict, List

from slowcontrol.drivers.turbovac import (FRAME_LEN, LGE, STX, TEXT_LEN, TurbovacLinkError,
                                          _bcc)

_WIDE = (176, 184, 682)             # parameters answered as 32-bit values


def _text(s: str) -> List[int]:
    return [ord(c) for c in s] + [0] * (TEXT_LEN - len(s))


class SimTurbovac:
    def __init__(self, pump_serial: str = "31003077799", product: str = "TMP T 90i",
                 catalog_no: str = "810041V1000", converter_serial: str = "9801750017"):
        self.plugged = True
        self.log: List[bytes] = []          # every telegram received
        self._garble = 0
        self._muted = False

        # the pump as it was found: at full speed, no error, no warning
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
        self.remote = False
        self.missing: set = set()           # parameters this pump "does not have"

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

    def garble(self, n: int = 1) -> None:
        """Damage the next `n` replies."""
        self._garble = n

    def mute(self, muted: bool = True) -> None:
        """On USB, but not answering."""
        self._muted = muted

    @property
    def violations(self) -> List[str]:
        """Telegrams that were anything other than a read."""
        return [t.hex(" ") for t in self.log
                if (t[3] >> 4) not in (0, 1, 6) or any(t[7:23]) or t[2] or t[5]]

    # ── the link interface ─────────────────────────────────────────────────

    def exchange(self, telegram: bytes) -> bytes:
        if not self.plugged:
            raise TurbovacLinkError("sim: device disconnected")
        self.log.append(bytes(telegram))
        if (self._muted or len(telegram) != FRAME_LEN or telegram[0] != STX
                or telegram[1] != LGE or _bcc(telegram) != telegram[-1]):
            raise TurbovacLinkError("sim: no reply")
        pke, = struct.unpack_from(">H", telegram, 3)
        access, pnu, index = pke >> 12, pke & 0x7FF, telegram[6]
        code, value = self._answer(access, pnu, index)
        reply = bytearray(FRAME_LEN)
        reply[0], reply[1] = STX, LGE
        struct.pack_into(">H", reply, 3, (code << 12) | (pnu if access else 0))
        reply[6] = index
        struct.pack_into(">IHHhHhH", reply, 7, value & 0xFFFFFFFF, self._status_word(),
                         self.frequency, self.converter_temp, round(self.motor_current * 10),
                         self.bearing_temp, round(self.supply))
        reply[-1] = _bcc(reply)
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
        if self.remote:
            word |= 1 << 15
        return word
