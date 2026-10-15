"""
R&S NGE100-series heater supply driver (NGE102 / NGE103 on USB-TMC).

The NGE103 is the DC supply behind the three cryostat heaters, one channel per
heater:

    NGE ch 1  →  heater 1  (zone 1, bottom clamp)
    NGE ch 2  →  heater 2  (zone 2, top clamp)
    NGE ch 3  →  heater 3  (zone 3, nozzle / disk)

The PLC still does the regulating — each PID loop PWMs its heater through an
SSR.  This driver looks after the supply those SSRs switch: output on/off,
voltage and current limit per channel, and keeping the channels from dropping
out.

How it is connected
───────────────────
USB, straight into the Pi.  The kernel's usbtmc driver turns the supply into a
character device and does the USB-TMC framing, so this is plain file I/O — no
VISA library.  slowcontrol/99-xsphere-nge.rules gives the device a stable name
(/dev/nge100) and lets the service's user open it.

Why a channel switches itself off, and what is done about it
────────────────────────────────────────────────────────────
Measured on the bench (NGE103, firmware 1.40) and in the user manual:

  • Each channel is limited to 33.6 W, and over-power protection (OPP) ships
    ENABLED at exactly that level.  OPP does not limit — it latches the channel
    off.  The heaters are 36 W at 24 V, so the supply works right at the trip
    level every time an SSR closes.  (No load was on the bench, so an OPP trip
    itself was not seen; it is the mechanism that fits the drop-outs.)
  • The electronic fuse, if on, likewise switches the channel off when the
    current limit is reached instead of letting the supply limit.
  • OVP latches the channel off when the measured voltage passes its level.
  • After a mains interruption the supply comes up with every output off and
    its setpoints reloaded from memory M1.

So with `disable_auto_off` (the default) the driver switches OPP and the
fuse off and keeps the OVP level at its maximum, which leaves plain constant-
current limiting as the only response to an overload.  And with `hold` on it
keeps a target (output, voltage, current limit) per channel and puts the
supply back on it whenever the two differ: a channel that tripped is cleared
and switched on again, setpoints lost in a power cycle are rewritten.

Hold makes the slow control the authority in both directions.  A channel
switched at the front panel is switched back, on or off — operate the supply
from the Control page, or turn hold off there first.  Stopping the service
leaves the supply exactly as it is.

Things the supply does that shaped this code
────────────────────────────────────────────
  • It caps the current limit to 33.6 W / V without an error: at 24 V a
    request for 1.5 A reads back as 1.400 A.  The request is kept and re-sent
    whenever the voltage changes; "in sync" means the supply still shows what
    it answered last time, not that it shows the request.
  • A channel delivers power when its arm flag (OUTP:SEL) and the master
    switch (OUTP:GEN) are both on.  `OUTP ON` raises the master, which also
    switches on every OTHER armed channel — so outputs are only ever set by
    writing all the arm flags and then the master (Nge100.set_outputs).
  • It refuses fuse changes (-221) while that channel's output is on.
  • Turning a tripped channel back on clears its trip flag, and works as soon
    as the cause is gone.  A trip on one channel leaves the others alone.
  • No status bit reports CC vs CV, so the mode is inferred from readings.

MQTT interface
──────────────
  Publish (retained):
    xsphere/status/nge        {connected, hold, master, idn, model, serial,
                               firmware, resource, error, event, event_utc,
                               last_read_utc, default_voltage_v,
                               default_current_a, max_voltage_v, max_current_a}
    xsphere/status/nge/{ch}   {output, desired, armed, voltage_set_v,
                               current_limit_a, voltage_request_v,
                               current_request_a, voltage_v, current_a,
                               power_w, mode, trip, opp, fuse, ovp_level_v,
                               dropouts[, last_dropout, last_dropout_utc]}
  Subscribe:
    xsphere/commands/nge/{ch}/output    {"state": 0|1}
    xsphere/commands/nge/{ch}/voltage   {"value_v": X}
    xsphere/commands/nge/{ch}/current   {"value_a": X}
    xsphere/commands/nge/hold           {"enabled": true|false}

Config: the `nge:` block of config.yaml (NgeConfig in core/config.py).
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import struct
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional, Tuple

from slowcontrol.core.mqtt import command_topic
from slowcontrol.drivers.base import SensorDriver

log = logging.getLogger(__name__)

ESR_PON = 0x80               # *ESR? bit 7: the supply was switched on since last read
ESR_ERRORS = 0x3C            # bits 2-5: query / device / execution / command error

OVP_MAX_V = 32.5             # the highest OVP level the supply accepts

# Setpoint readback tolerances: half the supply's 10 mV / 1 mA resolution.
V_TOL = 0.006
I_TOL = 0.0006

RECONNECT_S = 5.0            # between attempts to open an absent supply
RETRY_MIN_S = 2.0            # back-off between attempts to switch a channel back on …
RETRY_MAX_S = 60.0           # … doubling up to this while it keeps dropping out,
STABLE_S = 60.0              # and forgotten once it has stayed on this long
SAVE_EVERY_S = 60.0          # how often `last_contact` is refreshed on disk


class NgeError(Exception):
    """The instrument is not an NGE100, or cannot be identified."""


class NgeLinkError(NgeError):
    """Talking to the supply failed — the link should be dropped and reopened."""


# ---------------------------------------------------------------------------
# Link: the Linux kernel's USB-TMC driver
# ---------------------------------------------------------------------------

# ioctl USBTMC_IOCTL_SET_TIMEOUT = _IOW(91, 10, __u32), from <linux/usb/tmc.h>
_USBTMC_SET_TIMEOUT = 0x40045B0A


class UsbtmcLink:
    """write / query / close on a /dev/usbtmcN node.

    One write() is one message to the supply and one read() returns one
    reply — the kernel driver does the framing.  Every failure is raised as
    NgeLinkError, including the supply having been unplugged or powered off.
    """

    def __init__(self, path: str, timeout_s: float = 2.0):
        self.resource = path
        self._fd: Optional[int] = None
        try:
            self._fd = os.open(path, os.O_RDWR)
        except OSError as exc:
            raise NgeLinkError(f"cannot open {path}: {exc.strerror or exc}") from exc
        try:
            import fcntl
            fcntl.ioctl(self._fd, _USBTMC_SET_TIMEOUT,
                        struct.pack("I", int(timeout_s * 1000)))
        except (ImportError, OSError):
            pass                    # kernels before 4.20: a fixed 5 s timeout

    def write(self, command: str) -> None:
        try:
            os.write(self._fd, command.encode("ascii") + b"\n")
        except (OSError, TypeError) as exc:
            raise NgeLinkError(f"write {command!r} failed: {exc}") from exc

    def query(self, command: str) -> str:
        self.write(command)
        try:
            return os.read(self._fd, 4096).decode("ascii", "replace").strip()
        except (OSError, TypeError) as exc:
            raise NgeLinkError(f"no reply to {command!r}: {exc}") from exc

    def close(self) -> None:
        if self._fd is not None:
            try:
                os.close(self._fd)
            except OSError:
                pass
            self._fd = None


def open_link(cfg) -> object:
    """The link named by `nge.resource`: a usbtmc device node, or "sim"."""
    if cfg.resource.lower() == "sim":
        from slowcontrol.drivers.nge_sim import SimNge
        return SimNge(load_ohm=16.0)        # three 36 W / 24 V heaters
    return UsbtmcLink(cfg.resource, cfg.timeout_s)


# ---------------------------------------------------------------------------
# Instrument: the SCPI the NGE100 actually speaks
# ---------------------------------------------------------------------------

@dataclass
class ChannelState:
    """One channel as read back from the supply."""
    voltage_set_v: float
    current_limit_a: float
    output: bool            # delivering power: armed AND master on
    armed: bool             # OUTP:SEL
    voltage_v: float        # measured
    current_a: float
    power_w: float
    fuse: bool              # electronic fuse enabled
    fuse_tripped: bool
    ovp_level_v: float
    ovp_tripped: bool
    opp: bool               # over-power protection enabled
    opp_tripped: bool

    @property
    def trips(self) -> List[str]:
        return [name for name, hit in (("ovp", self.ovp_tripped),
                                       ("opp", self.opp_tripped),
                                       ("fuse", self.fuse_tripped)) if hit]

    @property
    def mode(self) -> str:
        """off | cv | cc.  The supply has no status bit for this; in constant-
        current mode it sits at the limit and lets the voltage sag."""
        if not self.output:
            return "off"
        sag = self.voltage_set_v - self.voltage_v
        if (self.current_a >= 0.97 * self.current_limit_a
                and sag > max(0.05, 0.005 * self.voltage_set_v)):
            return "cc"
        return "cv"


class Nge100:
    """SCPI for one NGE102 / NGE103 over an open link.

    The supply works select-then-command: INST:NSEL picks the channel the
    following commands apply to.  It is not thread-safe; the caller serialises.
    """

    def __init__(self, link):
        self._link = link
        self.idn = link.query("*IDN?")
        # e.g. "Rohde&Schwarz,NGE103,5601.1414k03/101129,1.40"
        m = re.search(r"NGE10([23])", self.idn.upper())
        if m is None:
            raise NgeError(f"not an R&S NGE100: {self.idn!r}")
        self.channels: Tuple[int, ...] = tuple(range(1, int(m.group(1)) + 1))
        parts = [p.strip() for p in self.idn.split(",")]
        self.model = parts[1] if len(parts) > 1 else ""
        self.serial = parts[2].rsplit("/", 1)[-1] if len(parts) > 2 else ""
        self.firmware = parts[3] if len(parts) > 3 else ""

    # -- plumbing --------------------------------------------------------

    def _select(self, ch: int) -> None:
        self._link.write(f"INST:NSEL {ch}")

    def _float(self, query: str) -> float:
        reply = self._link.query(query)
        try:
            return float(reply)
        except ValueError as exc:
            # A reply that does not parse means request and reply are out of
            # step; reopening the session is the way back.
            raise NgeLinkError(f"unexpected reply to {query}: {reply!r}") from exc

    def _bool(self, query: str) -> bool:
        return self._link.query(query).upper() in ("1", "ON")

    # -- reads -----------------------------------------------------------

    def event_status(self) -> int:
        """*ESR? — reading clears it."""
        return int(self._float("*ESR?"))

    def errors(self) -> List[str]:
        """Drain the supply's error queue."""
        out: List[str] = []
        for _ in range(20):
            entry = self._link.query("SYST:ERR?")
            if entry.startswith("0"):
                break
            out.append(entry)
        return out

    def master(self) -> bool:
        return self._bool("OUTP:GEN?")

    def read(self, ch: int) -> ChannelState:
        self._select(ch)
        return ChannelState(
            voltage_set_v=self._float("VOLT?"),
            current_limit_a=self._float("CURR?"),
            output=self._bool("OUTP?"),
            armed=self._bool("OUTP:SEL?"),
            voltage_v=self._float("MEAS:VOLT?"),
            current_a=self._float("MEAS:CURR?"),
            power_w=self._float("MEAS:POW?"),
            fuse=self._bool("FUSE?"),
            fuse_tripped=self._bool("FUSE:TRIP?"),
            ovp_level_v=self._float("VOLT:PROT:LEV?"),
            ovp_tripped=self._bool("VOLT:PROT:TRIP?"),
            opp=self._bool("POW:PROT?"),
            opp_tripped=self._bool("POW:PROT:TRIP?"),
        )

    # -- writes ----------------------------------------------------------

    def set_setpoints(self, ch: int, volts: float, amps: float) -> Tuple[float, float]:
        """Write voltage then current limit; return what the supply holds.

        Voltage first: raising it can make the supply lower the limit, and
        lowering it can make room for a limit it capped before.
        """
        self._select(ch)
        self._link.write(f"VOLT {volts:.2f}")
        self._link.write(f"CURR {amps:.3f}")
        return self._float("VOLT?"), self._float("CURR?")

    def set_outputs(self, want: Dict[int, bool]) -> None:
        """Make every channel's output match `want` (a missing channel = off).

        Never `OUTP ON`: that raises the master switch and with it every other
        armed channel.  Disarming comes first, so that raising the master can
        only ever energise channels that are meant to be on.
        """
        want = {ch: bool(want.get(ch, False)) for ch in self.channels}
        for ch in self.channels:
            if not want[ch]:
                self._select(ch)
                self._link.write("OUTP:SEL OFF")
        for ch in self.channels:
            if want[ch]:
                self._select(ch)
                self._link.write("OUTP:SEL ON")
        self._link.write("OUTP:GEN ON" if any(want.values()) else "OUTP:GEN OFF")

    def clear_trips(self, ch: int) -> None:
        self._select(ch)
        self._link.write("VOLT:PROT:CLE")
        self._link.write("POW:PROT:CLE")

    def remove_auto_off(self, ch: int, state: ChannelState) -> List[str]:
        """Take away the supply's reasons to switch `ch` off by itself.

        Returns what was changed, for the log.
        """
        done: List[str] = []
        if state.opp:
            self._select(ch)
            self._link.write("POW:PROT OFF")
            done.append("over-power protection switched off")
        # The supply answers -221 to a fuse change while the channel is on, so
        # an enabled fuse is left until the channel is next off.
        if state.fuse and not state.output:
            self._select(ch)
            self._link.write("FUSE OFF")
            done.append("electronic fuse switched off")
        if state.ovp_level_v < OVP_MAX_V - 0.05:
            self._select(ch)
            self._link.write(f"VOLT:PROT:LEV {OVP_MAX_V}")
            done.append(f"OVP level raised from {state.ovp_level_v:.1f} V to {OVP_MAX_V} V")
        return done

    def close(self) -> None:
        """Hand the front panel back and close the session."""
        try:
            self._link.write("SYST:LOC")
        except NgeLinkError:
            pass
        self._link.close()


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------

@dataclass
class Target:
    """What one channel is held at."""
    output: bool
    voltage_v: float
    current_a: float


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _age_text(seconds: float) -> str:
    if seconds < 90:
        return f"{seconds:.0f} s"
    if seconds < 5400:
        return f"{seconds / 60:.0f} min"
    return f"{seconds / 3600:.1f} h"


def _why_off(state: ChannelState, master: bool, power_cycled: bool) -> str:
    """Best explanation for a channel that should be on being off."""
    if state.trips:
        return " + ".join(t.upper() if t != "fuse" else t for t in state.trips) + " trip"
    if power_cycled:
        return "the supply was power-cycled"
    if state.armed and not master:
        return "master output switched off at the supply"
    return "switched off at the supply"


class NgeDriver(SensorDriver):
    NAME = "nge"

    def __init__(self, config, mqtt,
                 link_factory: Optional[Callable[[], object]] = None,
                 state_path: Optional[str] = None):
        super().__init__(config, mqtt)
        self._cfg = config.nge
        self._link_factory = link_factory or (lambda: open_link(self._cfg))
        self._state_path = state_path or os.path.normpath(os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "..", "nge_state.json"))
        # The poll thread and the MQTT callback thread both reach the supply,
        # and a select-then-command exchange must not be interleaved.
        self._io = threading.RLock()
        self._nge: Optional[Nge100] = None
        self._fresh_link = False
        self._next_open = 0.0
        self._error: Optional[str] = "not connected yet"

        self._hold: bool = self._cfg.hold
        self._desired: Dict[int, Target] = {}
        # Per channel, what we last asked for and what the supply answered:
        # (asked V, asked A, answered V, answered A).  See `_in_sync`.
        self._applied: Dict[int, Tuple[float, float, float, float]] = {}
        self._last_contact: Optional[float] = None      # epoch of the last good cycle
        self._last_save = 0.0
        self._dirty = False
        self._load_state()

        self._master = False
        self._last: Dict[int, ChannelState] = {}
        self._last_read_utc: Optional[str] = None
        self._event: Optional[str] = None
        self._event_utc: Optional[str] = None
        # Drop-out bookkeeping per channel.
        self._down: Dict[int, bool] = {}
        self._dropouts: Dict[int, int] = {}
        self._last_drop: Dict[int, Tuple[str, str]] = {}    # (utc, reason)
        self._retry_at: Dict[int, float] = {}
        self._retry_delay: Dict[int, float] = {}
        self._up_since: Dict[int, float] = {}
        # Channels whose protection settings would not take, per link session.
        self._auto_off_pending: Dict[int, int] = {}

    @property
    def poll_interval(self) -> float:
        return self._cfg.poll_interval_s

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def connect(self) -> None:
        # Never raises for an absent supply: SensorDriver.start() would
        # disable the driver for good.  The link is opened (and reopened)
        # from poll().
        self._mqtt.subscribe(command_topic("nge", "+", "output"),  self._on_output)
        self._mqtt.subscribe(command_topic("nge", "+", "voltage"), self._on_voltage)
        self._mqtt.subscribe(command_topic("nge", "+", "current"), self._on_current)
        self._mqtt.subscribe(command_topic("nge", "hold"),         self._on_hold)

    def disconnect(self) -> None:
        # Deliberately no output change: the heaters keep their supply when
        # the service stops.
        with self._io:
            self._save_state()
            self._close_link("service stopped")
            self._publish()

    # ------------------------------------------------------------------
    # Poll
    # ------------------------------------------------------------------

    def poll(self) -> None:
        with self._io:
            if self._nge is None and not self._open():
                self._publish()
                return
            try:
                self._cycle()
            except NgeLinkError as exc:
                log.warning("[nge] lost the supply — will reconnect: %s", exc)
                self._close_link(str(exc))
            except Exception as exc:                                # noqa: BLE001
                # Contained here on purpose.  Left to the base class, a fault
                # that repeats every poll reads as a stuck poll thread, and the
                # service watchdog would restart the whole slow control — PID
                # feed and PV interlock included — over a bench supply.
                log.exception("[nge] unexpected error — dropping the link")
                self._close_link(f"internal error: {exc!r}", retry_s=60.0)
            self._publish()

    def _open(self) -> bool:
        now = time.monotonic()
        if now < self._next_open:
            return False
        self._next_open = now + RECONNECT_S
        link = None
        try:
            link = self._link_factory()
            nge = Nge100(link)
            # Other NGE supplies exist in the lab; only ever drive ours.
            if self._cfg.serial and nge.serial != self._cfg.serial:
                raise NgeError(f"this is NGE serial {nge.serial}, but nge.serial "
                               f"in config.yaml is {self._cfg.serial}")
        except NgeError as exc:
            if link is not None:
                link.close()
            if str(exc) != self._error:             # log a new reason once
                log.warning("[nge] supply not reachable: %s", exc)
            self._error = str(exc)
            return False
        self._nge = nge
        self._fresh_link = True
        self._error = None
        self._auto_off_pending.clear()
        for ch in nge.channels:
            self._down.setdefault(ch, False)
            self._dropouts.setdefault(ch, 0)
            self._retry_at.setdefault(ch, 0.0)
            self._retry_delay.setdefault(ch, 0.0)
            self._up_since.setdefault(ch, now)
        log.info("[nge] connected to %s (%s)", nge.idn,
                 getattr(link, "resource", self._cfg.resource))
        return True

    def _close_link(self, reason: str, retry_s: float = RECONNECT_S) -> None:
        if self._nge is not None:
            self._nge.close()
            self._nge = None
        self._last = {}
        self._error = reason
        self._next_open = time.monotonic() + retry_s

    def _read_all(self) -> Tuple[bool, Dict[int, ChannelState]]:
        nge = self._nge
        return nge.master(), {ch: nge.read(ch) for ch in nge.channels}

    def _cycle(self) -> None:
        nge = self._nge
        esr = nge.event_status()
        power_cycled = bool(esr & ESR_PON)
        if esr & ESR_ERRORS:
            for entry in nge.errors():
                log.warning("[nge] supply reported: %s", entry)
        master, states = self._read_all()

        if self._fresh_link:
            self._fresh_link = False
            self._on_connected(states, power_cycled)

        changed = False
        if self._cfg.disable_auto_off:
            changed |= self._remove_auto_off(states)
        if self._hold:
            changed |= self._enforce(states, master, power_cycled)
        else:
            self._adopt(states)
        if changed:
            master, states = self._read_all()

        self._master, self._last = master, states
        self._last_read_utc = _utc_now()
        self._last_contact = time.time()
        if self._dirty or time.monotonic() - self._last_save > SAVE_EVERY_S:
            self._save_state()

    def _on_connected(self, states: Dict[int, ChannelState],
                      power_cycled: bool) -> None:
        """Decide whether to carry on holding the remembered targets.

        Carrying on is what restores the heater supply after a power cut.
        But targets that have not been checked against the supply for a long
        time say nothing about what the operator wants now, so past
        `resume_max_age_s` the supply's present state is adopted instead.
        """
        age = None if self._last_contact is None else time.time() - self._last_contact
        complete = all(ch in self._desired for ch in self._nge.channels)
        if (self._hold and complete and age is not None
                and age <= self._cfg.resume_max_age_s):
            # Whatever the supply holds now, check it against the targets.
            self._applied.clear()
            self._note(f"connected — holding the state last set {_age_text(age)} ago"
                       + (" (the supply was power-cycled)" if power_cycled else ""))
            return
        if not self._hold:
            why = "hold is off"
        elif not complete or age is None:
            why = "nothing held yet"
        else:
            why = f"the held state is {_age_text(age)} old"
        self._adopt(states)
        self._note(f"connected — adopted the supply's present state ({why})")

    # ------------------------------------------------------------------
    # Targets
    # ------------------------------------------------------------------

    def _adopt(self, states: Dict[int, ChannelState]) -> None:
        """Make the targets equal to what the supply is doing now."""
        for ch, st in states.items():
            target = Target(st.output, st.voltage_set_v, st.current_limit_a)
            if self._desired.get(ch) != target:
                self._desired[ch] = target
                self._dirty = True
            self._applied[ch] = (target.voltage_v, target.current_a,
                                 st.voltage_set_v, st.current_limit_a)
            self._down[ch] = False

    def _in_sync(self, ch: int, st: ChannelState) -> bool:
        """Does the supply still hold the setpoints we last gave it?

        Compared with what it ANSWERED then, not with the request: it caps the
        current limit to 33.6 W / V, so a 1.5 A request legitimately reads
        back as 1.400 A at 24 V and would otherwise be rewritten every poll.
        """
        target = self._desired[ch]
        applied = self._applied.get(ch)
        if applied is None or applied[:2] != (target.voltage_v, target.current_a):
            return False
        return (abs(st.voltage_set_v - applied[2]) <= V_TOL
                and abs(st.current_limit_a - applied[3]) <= I_TOL)

    def _enforce(self, states: Dict[int, ChannelState], master: bool,
                 power_cycled: bool, by_operator: Optional[int] = None) -> bool:
        """Put the supply on its targets.  True if anything was written.

        `by_operator` names a channel whose target the operator has just
        changed: a difference there is a command being carried out, not a
        drop-out to count or a setting to restore.
        """
        nge = self._nge
        now = time.monotonic()
        changed = False

        for ch in nge.channels:
            st = states[ch]
            if self._in_sync(ch, st):
                continue
            target = self._desired[ch]
            volts, amps = nge.set_setpoints(ch, target.voltage_v, target.current_a)
            self._applied[ch] = (target.voltage_v, target.current_a, volts, amps)
            changed = True
            moved = (abs(volts - st.voltage_set_v) > V_TOL
                     or abs(amps - st.current_limit_a) > I_TOL)
            if ch == by_operator:
                log.info("[nge] ch%d set to %.2f V / %.3f A (asked %.2f V / %.3f A)",
                         ch, volts, amps, target.voltage_v, target.current_a)
            elif moved:
                self._note(f"ch{ch} setpoints were {st.voltage_set_v:.2f} V / "
                           f"{st.current_limit_a:.3f} A at the supply — restored "
                           f"{volts:.2f} V / {amps:.3f} A", logging.WARNING)

        want: Dict[int, bool] = {}
        for ch in nge.channels:
            st, target = states[ch], self._desired[ch]
            want[ch] = target.output
            if ch == by_operator:
                self._down[ch] = False
                self._retry_at[ch] = self._retry_delay[ch] = 0.0
                if target.output and not st.output:
                    nge.clear_trips(ch)
                continue
            if target.output and not st.output:
                new = not self._down[ch]            # first sight of this drop-out
                if new:
                    self._down[ch] = True
                    self._dropouts[ch] += 1
                    self._last_drop[ch] = (_utc_now(), _why_off(st, master, power_cycled))
                reason = self._last_drop[ch][1]
                if now < self._retry_at[ch]:
                    want[ch] = False                # backing off; leave it disarmed
                    if new:
                        self._note(f"ch{ch} dropped out again ({reason}) — next attempt "
                                   f"in {self._retry_at[ch] - now:.0f} s", logging.WARNING)
                    continue
                nge.clear_trips(ch)
                self._retry_delay[ch] = min(RETRY_MAX_S,
                                            max(RETRY_MIN_S, 2 * self._retry_delay[ch]))
                self._retry_at[ch] = now + self._retry_delay[ch]
                self._note(f"ch{ch} dropped out ({reason}) — switched back on",
                           logging.WARNING if new else logging.INFO)
            elif st.output and not target.output:
                self._note(f"ch{ch} was switched on at the supply — switched off "
                           "again (hold)", logging.WARNING)
            elif st.output:
                if self._down[ch]:
                    self._down[ch] = False
                    self._up_since[ch] = now
                    log.info("[nge] ch%d output is back on", ch)
                elif self._retry_delay[ch] and now - self._up_since[ch] >= STABLE_S:
                    self._retry_at[ch] = self._retry_delay[ch] = 0.0

        # On target means the switches themselves match, not just the outputs:
        # a channel left armed behind a master that is off delivers nothing,
        # but is one press of the Output key away from doing so.
        if (master != any(want.values())
                or any(states[ch].armed != want[ch] for ch in nge.channels)):
            nge.set_outputs(want)
            changed = True
        return changed

    def _remove_auto_off(self, states: Dict[int, ChannelState]) -> bool:
        changed = False
        for ch, st in states.items():
            tries = self._auto_off_pending.get(ch, 0)
            if tries >= 3:
                continue                            # it will not take; said so already
            done = self._nge.remove_auto_off(ch, st)
            if not done:
                self._auto_off_pending.pop(ch, None)
                continue
            changed = True
            self._auto_off_pending[ch] = tries + 1
            if tries == 0:
                log.info("[nge] ch%d: %s", ch, "; ".join(done))
            elif tries == 2:
                self._note(f"ch{ch}: the supply will not take its protection settings "
                           f"({'; '.join(done)})", logging.ERROR)
        return changed

    # ------------------------------------------------------------------
    # Persistence — hold flag and targets, so a restart of the service (or
    # of the Pi) picks up where it left off
    # ------------------------------------------------------------------

    def _load_state(self) -> None:
        try:
            with open(self._state_path) as fh:
                raw = json.load(fh)
            self._hold = bool(raw["hold"])
            self._last_contact = float(raw["last_contact"])
            self._desired = {
                int(ch): Target(bool(t["output"]), float(t["voltage_v"]), float(t["current_a"]))
                for ch, t in raw["channels"].items()
            }
        except FileNotFoundError:
            return
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
            log.warning("[nge] cannot use %s (%s) — starting from the supply's "
                        "present state", self._state_path, exc)
            self._desired = {}
            self._last_contact = None
            return
        log.info("[nge] loaded held state from %s (hold %s)",
                 self._state_path, "on" if self._hold else "off")

    def _save_state(self) -> None:
        self._dirty = False
        self._last_save = time.monotonic()
        if self._last_contact is None:
            return                                  # nothing learned from the supply yet
        tmp = self._state_path + ".tmp"
        try:
            with open(tmp, "w") as fh:
                json.dump({
                    "hold": self._hold,
                    "last_contact": self._last_contact,
                    "channels": {str(ch): {"output": t.output,
                                           "voltage_v": t.voltage_v,
                                           "current_a": t.current_a}
                                 for ch, t in sorted(self._desired.items())},
                }, fh, indent=2)
                fh.write("\n")
            os.replace(tmp, self._state_path)
        except OSError as exc:
            log.warning("[nge] cannot persist held state to %s: %s",
                        self._state_path, exc)

    # ------------------------------------------------------------------
    # MQTT command callbacks
    # ------------------------------------------------------------------

    def _channel_of(self, topic: str) -> Optional[int]:
        # xsphere/commands/nge/{ch}/{what}
        try:
            ch = int(topic.split("/")[-2])
        except ValueError:
            ch = 0
        known = self._nge.channels if self._nge is not None else (1, 2, 3)
        if ch not in known:
            log.warning("[nge] no such channel in %s", topic)
            return None
        return ch

    def _command(self, ch: Optional[int], change: Callable[[], None]) -> None:
        """Carry out one operator command on a connected supply.

        The targets are first brought level with the supply if hold is off
        (so only the commanded thing moves), `change` edits them, and one
        enforcement pass makes the supply match.  Caller holds `_io`.
        """
        try:
            master, states = self._read_all()
            if not self._hold:
                self._adopt(states)
            change()
            self._dirty = True
            self._enforce(states, master, False, by_operator=ch)
            self._master, self._last = self._read_all()
            self._last_read_utc = _utc_now()
            self._last_contact = time.time()
        except NgeLinkError as exc:
            log.warning("[nge] lost the supply during a command: %s", exc)
            self._close_link(str(exc))
        self._save_state()

    def _on_output(self, topic: str, payload) -> None:
        """xsphere/commands/nge/{ch}/output  → {"state": 0|1}"""
        ch = self._channel_of(topic)
        if ch is None:
            return
        # Only a literal 0/1 switches a heater supply — anything else off the
        # bus is dropped rather than coerced.
        raw = payload.get("state") if isinstance(payload, dict) else None
        if raw not in (0, 1):
            log.warning("[nge] ch%d output: ignoring state %r", ch, raw)
            return
        on = bool(raw)
        with self._io:
            if self._nge is None:
                # Off is remembered, so the channel stays off when the supply
                # returns.  On is refused: nothing should come on later
                # because of a click made while there was no supply to obey.
                if on:
                    self._note(f"ch{ch} ON ignored — the supply is not connected")
                elif ch in self._desired and self._desired[ch].output:
                    self._desired[ch].output = False
                    self._save_state()
                    self._note(f"ch{ch} will stay off when the supply comes back")
                self._publish()
                return

            def change() -> None:
                self._desired[ch].output = on

            self._command(ch, change)
            log.info("[nge] ch%d output → %s", ch, "ON" if on else "OFF")
            self._publish()

    def _on_setpoint(self, topic: str, payload, key: str, unit: str,
                     ceiling: float, field: str) -> None:
        ch = self._channel_of(topic)
        if ch is None:
            return
        raw = payload.get(key) if isinstance(payload, dict) else None
        try:
            value = float(raw)
        except (TypeError, ValueError):
            value = math.nan
        with self._io:
            # bool is an int to Python; `true` must not become 1 V.
            if isinstance(raw, bool) or not (0.0 <= value <= ceiling):
                self._note(f"ch{ch}: {raw!r} {unit} rejected — allowed 0 … "
                           f"{ceiling:g} {unit}", logging.WARNING)
            elif self._nge is None:
                self._note(f"ch{ch} {unit} setting ignored — the supply is not connected")
            else:
                def change() -> None:
                    setattr(self._desired[ch], field, value)

                self._command(ch, change)
            self._publish()

    def _on_voltage(self, topic: str, payload) -> None:
        """xsphere/commands/nge/{ch}/voltage  → {"value_v": X}"""
        self._on_setpoint(topic, payload, "value_v", "V",
                          self._cfg.max_voltage_v, "voltage_v")

    def _on_current(self, topic: str, payload) -> None:
        """xsphere/commands/nge/{ch}/current  → {"value_a": X}"""
        self._on_setpoint(topic, payload, "value_a", "A",
                          self._cfg.max_current_a, "current_a")

    def _on_hold(self, topic: str, payload) -> None:
        """xsphere/commands/nge/hold  → {"enabled": true|false}

        Switching hold on holds the supply as it is at that moment.
        """
        raw = payload.get("enabled") if isinstance(payload, dict) else None
        if raw not in (True, False, 0, 1):
            log.warning("[nge] hold: ignoring %r", raw)
            return
        with self._io:
            self._hold = bool(raw)
            if self._hold:
                if self._nge is None:
                    self._desired.clear()           # adopt whatever is there on connect
                else:
                    try:
                        self._adopt(self._read_all()[1])
                    except NgeLinkError as exc:
                        self._close_link(str(exc))
            self._last_contact = self._last_contact or time.time()
            self._save_state()
            self._note("hold switched " + ("on — holding the present state"
                                           if self._hold else "off"))
            self._publish()

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------

    def _note(self, text: str, level: int = logging.INFO) -> None:
        """Log a notable event and keep it for the Control page."""
        self._event, self._event_utc = text, _utc_now()
        log.log(level, "[nge] %s", text)

    def _publish(self) -> None:
        nge = self._nge
        self._mqtt.publish_status("nge", payload={
            "connected":         nge is not None,
            "hold":              self._hold,
            "master":            int(self._master) if nge is not None else None,
            "idn":               nge.idn if nge is not None else None,
            "model":             nge.model if nge is not None else None,
            "serial":            nge.serial if nge is not None else None,
            "firmware":          nge.firmware if nge is not None else None,
            "resource":          self._cfg.resource,
            "error":             self._error,
            "event":             self._event,
            "event_utc":         self._event_utc,
            "last_read_utc":     self._last_read_utc,
            "default_voltage_v": self._cfg.default_voltage_v,
            "default_current_a": self._cfg.default_current_a,
            "max_voltage_v":     self._cfg.max_voltage_v,
            "max_current_a":     self._cfg.max_current_a,
        })
        # Nothing per channel while the supply is away: the registry lets
        # those states go stale rather than show numbers nobody measured.
        for ch, st in self._last.items():
            target = self._desired.get(ch)
            payload = {
                "output":          int(st.output),
                "armed":           int(st.armed),
                "voltage_set_v":   round(st.voltage_set_v, 3),
                "current_limit_a": round(st.current_limit_a, 4),
                "voltage_v":       round(st.voltage_v, 3),
                "current_a":       round(st.current_a, 4),
                "power_w":         round(st.power_w, 3),
                "mode":            st.mode,
                "trip":            ",".join(st.trips) or "none",
                "opp":             int(st.opp),
                "fuse":            int(st.fuse),
                "ovp_level_v":     round(st.ovp_level_v, 2),
                "dropouts":        self._dropouts.get(ch, 0),
            }
            if target is not None:
                payload["desired"] = int(target.output)
                payload["voltage_request_v"] = round(target.voltage_v, 3)
                payload["current_request_a"] = round(target.current_a, 4)
            if ch in self._last_drop:
                payload["last_dropout_utc"], payload["last_dropout"] = self._last_drop[ch]
            self._mqtt.publish_status("nge", str(ch), payload=payload)
