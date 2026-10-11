"""
A stand-in R&S NGE103 — no hardware needed.

Speaks the SCPI subset `slowcontrol.drivers.nge` uses and reproduces the
behaviour measured on the real supply (firmware 1.40), so the driver, the
state registry and the Control page can be exercised on a desk:

  • per-channel arm flag (OUTP:SEL) and one master switch (OUTP:GEN); `OUTP ON`
    arms the channel AND raises the master, energising every armed channel
  • the 33.6 W envelope: a current limit above 33.6 W / V is capped silently,
    and raising the voltage pulls an existing limit down
  • OVP / OPP / electronic fuse latch the channel off (disarm it) and leave the
    others alone; re-arming clears the flag and sticks once the cause is gone
  • fuse changes are refused (-221) while that channel's output is on
  • a power cycle brings everything up off, with the setpoints and protection
    settings of memory M1, and sets the power-on bit of *ESR?

Use it as the supply with `nge.resource: sim` in config.yaml, or drive it
directly from a test: `load_ohm`, `power_cycle()`, `unplug()`, `press_output()`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

from slowcontrol.drivers.nge import NgeLinkError

P_MAX_W = 33.6
V_MAX = 32.2
I_MAX = 3.0
I_MIN = 0.003
OVP_MAX_V = 32.5


@dataclass
class _Channel:
    volt: float = 5.0
    curr: float = 1.0
    armed: bool = False
    fuse: bool = False
    fuse_delay_ms: int = 0
    fuse_trip: bool = False
    links: List[int] = field(default_factory=list)
    ovp: bool = True
    ovp_level: float = OVP_MAX_V
    ovp_mode: str = "MEASured"
    ovp_trip: bool = False
    opp: bool = True                # the factory setting — the cause of the drop-outs
    opp_level: float = P_MAX_W
    opp_trip: bool = False


def _num(x: float) -> str:
    return f"{x:.6E}"


def _flag(x: bool) -> str:
    return "1" if x else "0"


class SimNge:
    """Simulated NGE103 with the write / query / close interface of a link."""

    def __init__(self, n_channels: int = 3, load_ohm: Optional[float] = None,
                 serial: str = "000000"):
        self.resource = "sim"
        self.idn = f"Rohde&Schwarz,NGE10{n_channels},5601.1414k0{n_channels}/{serial},1.40"
        self.channels = tuple(range(1, n_channels + 1))
        #: Resistance across each output; None = nothing connected.
        self.load_ohm: Dict[int, Optional[float]] = {c: load_ohm for c in self.channels}
        #: What memory M1 holds — reloaded at every power-up.
        self.m1: Dict[int, _Channel] = {c: _Channel() for c in self.channels}
        self.plugged = True
        self.log: List[str] = []            # every command received, for tests
        self._errors: List[str] = []
        self._esr = 0
        self.power_cycle()

    # -- things that happen to a real supply ------------------------------

    def power_cycle(self) -> None:
        """Mains off and on again."""
        self.master = False
        self._selected = 1
        self.ch: Dict[int, _Channel] = {}
        for c in self.channels:
            m = self.m1[c]
            self.ch[c] = _Channel(volt=m.volt, curr=m.curr, fuse=m.fuse,
                                  fuse_delay_ms=m.fuse_delay_ms, links=list(m.links),
                                  ovp=m.ovp, ovp_level=m.ovp_level, ovp_mode=m.ovp_mode,
                                  opp=m.opp, opp_level=m.opp_level)
        self._errors.clear()
        self._esr = 0x80

    def unplug(self) -> None:
        self.plugged = False

    def plug(self) -> None:
        self.plugged = True

    def press_output(self) -> None:
        """The front-panel Output key: toggles the master switch."""
        self.master = not self.master
        self._protect()

    def press_channel(self, c: int) -> None:
        """A front-panel channel key: toggles that channel's arm flag."""
        self._arm(c, not self.ch[c].armed)

    # -- output physics ----------------------------------------------------

    def output(self, c: int) -> bool:
        return self.ch[c].armed and self.master

    def _vi(self, c: int):
        ch = self.ch[c]
        if not self.output(c):
            return 0.0, 0.0
        r = self.load_ohm[c]
        if r is None:
            return ch.volt, 0.0
        if ch.volt / r > ch.curr:           # constant current: the voltage sags
            return ch.curr * r, ch.curr
        return ch.volt, ch.volt / r

    def _protect(self) -> None:
        for c in self.channels:
            ch = self.ch[c]
            if not self.output(c):
                continue
            v, i = self._vi(c)
            r = self.load_ohm[c]
            if ch.ovp and v > ch.ovp_level:
                ch.ovp_trip, ch.armed = True, False
            elif ch.opp and v * i > ch.opp_level:
                ch.opp_trip, ch.armed = True, False
            elif ch.fuse and r is not None and ch.volt / r > ch.curr:
                ch.fuse_trip, ch.armed = True, False
                for other in ch.links:
                    self.ch[other].armed = False

    def trip(self, c: int, kind: str) -> None:
        """Force a protection trip, as a load transient would: 'ovp' | 'opp' | 'fuse'."""
        setattr(self.ch[c], f"{kind}_trip", True)
        self.ch[c].armed = False

    def _arm(self, c: int, on: bool) -> None:
        ch = self.ch[c]
        ch.armed = on
        if on:
            # Switching a channel on clears its latched trips; it trips again
            # at once if the cause is still there.
            ch.ovp_trip = ch.opp_trip = ch.fuse_trip = False
        self._protect()

    def _i_cap(self, volt: float) -> float:
        return I_MAX if volt * I_MAX <= P_MAX_W else P_MAX_W / volt

    # -- link interface ----------------------------------------------------

    def close(self) -> None:
        pass

    def _fail(self, code: int, text: str, command: str) -> None:
        self._errors.append(f'{code},"{text};{command}"')
        self._esr |= 0x20 if code in (-113, -128, -104) else 0x10

    def write(self, command: str) -> None:
        if not self.plugged:
            raise NgeLinkError(f"write {command!r} failed: device not present")
        self.log.append(command)
        head, _, arg = command.strip().partition(" ")
        head, arg = head.upper().lstrip(":"), arg.strip()
        ch = self.ch[self._selected]
        try:
            if head == "INST:NSEL":
                if int(arg) not in self.channels:
                    raise ValueError(arg)
                self._selected = int(arg)
            elif head == "VOLT":
                ch.volt = round(min(max(float(arg), 0.0), V_MAX), 2)
                ch.curr = min(ch.curr, self._i_cap(ch.volt))
            elif head == "CURR":
                ch.curr = min(max(round(float(arg), 3), I_MIN), self._i_cap(ch.volt))
            elif head == "OUTP":
                if self._on(arg):
                    self.master = True
                self._arm(self._selected, self._on(arg))
            elif head == "OUTP:SEL":
                self._arm(self._selected, self._on(arg))
            elif head == "OUTP:GEN":
                self.master = self._on(arg)
            elif head == "FUSE":
                if self.output(self._selected):
                    self._fail(-221, "Settings conflict", command)
                else:
                    ch.fuse = self._on(arg)
            elif head == "FUSE:DEL":
                ch.fuse_delay_ms = int(float(arg))
            elif head == "FUSE:LINK":
                if int(arg) not in ch.links:
                    ch.links.append(int(arg))
            elif head == "FUSE:UNL":
                if int(arg) in ch.links:
                    ch.links.remove(int(arg))
            elif head == "VOLT:PROT":
                ch.ovp = self._on(arg)
            elif head == "VOLT:PROT:LEV":
                ch.ovp_level = OVP_MAX_V if arg.upper() == "MAX" else \
                    min(max(round(float(arg), 1), 0.0), OVP_MAX_V)
            elif head == "VOLT:PROT:MODE":
                ch.ovp_mode = "PROTected" if arg.upper().startswith("PROT") else "MEASured"
            elif head == "VOLT:PROT:CLE":
                ch.ovp_trip = False
            elif head == "POW:PROT":
                ch.opp = self._on(arg)
            elif head == "POW:PROT:LEV":
                ch.opp_level = min(max(round(float(arg), 1), 0.0), P_MAX_W)
            elif head == "POW:PROT:CLE":
                ch.opp_trip = False
            elif head in ("SYST:LOC", "SYST:REM", "*CLS"):
                pass
            else:
                self._fail(-113, "Undefined header", command)
        except ValueError:
            self._fail(-104, "Data type error", command)
        self._protect()

    @staticmethod
    def _on(arg: str) -> bool:
        if arg.upper() in ("ON", "1"):
            return True
        if arg.upper() in ("OFF", "0"):
            return False
        raise ValueError(arg)

    def query(self, command: str) -> str:
        if not self.plugged:
            raise NgeLinkError(f"query {command!r} failed: device not present")
        self.log.append(command)
        self._protect()
        head, _, arg = command.strip().partition(" ")
        head, arg = head.upper().lstrip(":"), arg.strip()
        ch = self.ch[self._selected]
        v, i = self._vi(self._selected)
        answers = {
            "*IDN?":           lambda: self.idn,
            "INST:NSEL?":      lambda: str(self._selected),
            "VOLT?":           lambda: _num(ch.volt),
            "CURR?":           lambda: _num(ch.curr),
            "OUTP?":           lambda: _flag(self.output(self._selected)),
            "OUTP:SEL?":       lambda: _flag(ch.armed),
            "OUTP:GEN?":       lambda: _flag(self.master),
            "MEAS:VOLT?":      lambda: _num(v),
            "MEAS:CURR?":      lambda: _num(i),
            "MEAS:POW?":       lambda: _num(v * i),
            "FUSE?":           lambda: _flag(ch.fuse),
            "FUSE:DEL?":       lambda: str(ch.fuse_delay_ms),
            "FUSE:TRIP?":      lambda: _flag(ch.fuse_trip),
            "FUSE:LINK?":      lambda: _flag(int(arg) in ch.links),
            "VOLT:PROT?":      lambda: _flag(ch.ovp),
            "VOLT:PROT:LEV?":  lambda: _num(ch.ovp_level),
            "VOLT:PROT:MODE?": lambda: ch.ovp_mode,
            "VOLT:PROT:TRIP?": lambda: _flag(ch.ovp_trip),
            "POW:PROT?":       lambda: _flag(ch.opp),
            "POW:PROT:LEV?":   lambda: _num(ch.opp_level),
            "POW:PROT:TRIP?":  lambda: _flag(ch.opp_trip),
        }
        if head == "*ESR?":
            value, self._esr = self._esr, 0
            return str(value)
        if head == "SYST:ERR?":
            return self._errors.pop(0) if self._errors else '0,"No error"'
        if head not in answers:
            # The real supply queues -113 and never answers: the read times out.
            self._fail(-113, "Undefined header", command)
            raise NgeLinkError(f"query {command!r} failed: timeout")
        return answers[head]()
