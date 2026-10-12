"""Capture what a device prints on its USB serial console, with timestamps.

A diagnostic, not part of the service.  It only listens — nothing is ever
written to the device — and it

  * keeps every line, timestamped, in a daily file  <out>/<name>-YYYYMMDD.log
  * prints to stdout only what is *not* routine: boot text, reconnect
    attempts, error messages, the output stopping and resuming, the port
    going away.  Run under systemd, that is the journal.

Written for the gas handling cart's ESP32 (October 2026: it kept going silent
on MQTT with its LEDs still on), which prints every reading at 9600 baud:

    python -m slowcontrol.tools.serial_capture --name ghs --wait-new \\
        --routine '^(Temperature: |Pressure: |CH[0-9])'

    --port PATH   the tty to listen on; a /dev/serial/by-path/ name is the
                  stable choice
    --wait-new    instead: take the first USB-serial adapter that is plugged in
                  after this started, and stay with that socket from then on
                  (remembered in <out>/<name>.port)

Reading the cart's output:

    unreadable bytes, then "Connecting to WiFi"     the board restarted
    "Attempting MQTT connection...failed, rc=…"     it lost Wi-Fi or the broker
                                                    and is retrying (it reads
                                                    no sensors meanwhile)
    output stops after a "CHn:" line                it is stuck reading an ADC

To leave it running on xbox-pi (until the next reboot):

    sudo systemd-run --unit=ghs-serial-capture --uid=xbox \\
        -p Restart=always -p RestartSec=5 -p WorkingDirectory=$PWD \\
        ~/miniconda3/envs/slowcontrol/bin/python -u -m slowcontrol.tools.serial_capture ...
    journalctl -u ghs-serial-capture -f          # the events
    sudo systemctl stop ghs-serial-capture
"""

from __future__ import annotations

import argparse
import datetime
import glob
import os
import re
import sys
import time
from typing import Callable, Dict, Optional

BY_PATH = "/dev/serial/by-path"
PARTIAL_AFTER_QUIET_S = 2.0     # an unfinished line is logged once the port has been quiet this long …
PARTIAL_AFTER_AGE_S = 10.0      # … or once it is this old ("Connecting to WiFi......")
REPEAT_WINDOW_S = 300.0         # the same event again within this long is counted, not printed
REOPEN_S = 3.0


class Capture:
    """Turns the bytes read from the port into log lines and events."""

    def __init__(self, write_line: Callable[[str], None], emit: Callable[[str], None],
                 routine: Optional[re.Pattern] = None, silence_s: float = 15.0):
        self._write_line = write_line       # every line → the file
        self._emit = emit                   # what is worth a look → stdout
        self._routine = routine
        self._silence_s = silence_s
        self._buf = b""
        self._buf_since = 0.0
        self._last_rx: Optional[float] = None
        self._silent = False
        self._last_line = ""
        self._last_event: Optional[str] = None
        self._last_event_ts = 0.0
        self._repeats = 0

    def opened(self, now: float) -> None:
        """The port was (re)opened: start the silence clock from here."""
        self._buf = b""
        self._last_rx = now
        self._silent = False
        self._last_line = "(nothing since the port was opened)"

    def closed(self) -> None:
        self._last_rx = None

    def note(self, text: str, now: float) -> None:
        """Something this tool observed, rather than a line the device printed."""
        self._write_line("# " + text)
        self._event(text, now)

    def feed(self, data: bytes, now: float) -> None:
        if not data:
            return
        if self._silent:
            self._silent = False
            self.note(f"output resumed after {now - self._last_rx:.0f} s", now)
        self._last_rx = now
        if not self._buf:
            self._buf_since = now
        self._buf += data
        while b"\n" in self._buf:
            raw, self._buf = self._buf.split(b"\n", 1)
            self._buf_since = now
            self._line(raw, now)

    def tick(self, now: float) -> None:
        """Call regularly, whether or not anything was read."""
        if self._last_rx is None:
            return
        quiet = now - self._last_rx
        if self._buf and (quiet >= PARTIAL_AFTER_QUIET_S
                          or now - self._buf_since >= PARTIAL_AFTER_AGE_S):
            raw, self._buf = self._buf, b""
            self._line(raw, now, partial=True)
        if not self._silent and quiet >= self._silence_s:
            self._silent = True
            self.note(f"no output for {quiet:.0f} s - last line: {self._last_line}", now)

    def _line(self, raw: bytes, now: float, partial: bool = False) -> None:
        raw = raw.rstrip(b"\r")
        text = raw.decode("utf-8", errors="replace")
        if not text.strip():
            return
        bad = sum(1 for c in text if c == "�" or (ord(c) < 32 and c != "\t"))
        if bad > max(2, len(text) // 4):
            # The ESP32's boot loader talks at 115200 baud whatever the sketch uses.
            self._write_line(f"# {len(raw)} unreadable bytes: {raw[:48]!r}")
            self._event("unreadable bytes - what the boot text after a reset looks like", now)
            return
        self._write_line(text + ("  [no line end yet]" if partial else ""))
        self._last_line = text
        if self._routine is None or not self._routine.search(text):
            self._event(text, now)

    def _event(self, text: str, now: float) -> None:
        if text == self._last_event and now - self._last_event_ts < REPEAT_WINDOW_S:
            self._repeats += 1
            return
        if self._repeats:
            self._emit(f"(the line above came {self._repeats} more time"
                       f"{'s' if self._repeats > 1 else ''})")
        self._last_event, self._last_event_ts, self._repeats = text, now, 0
        self._emit(text)


class DailyLog:
    """<directory>/<name>-YYYYMMDD.log, each line stamped HH:MM:SS.mmm; old days are deleted."""

    def __init__(self, directory: str, name: str, keep_days: int = 14):
        self._dir, self._name, self._keep_days = directory, name, keep_days
        self._day = ""
        self._fh = None

    def write(self, text: str) -> None:
        now = datetime.datetime.now()
        day = now.strftime("%Y%m%d")
        if day != self._day:
            if self._fh is not None:
                self._fh.close()
            self._day = day
            self._fh = open(os.path.join(self._dir, f"{self._name}-{day}.log"), "a",
                            encoding="utf-8", buffering=1)
            self._prune(now)
        self._fh.write(f"{now.strftime('%H:%M:%S.%f')[:-3]} {text}\n")

    def _prune(self, now: datetime.datetime) -> None:
        oldest = (now - datetime.timedelta(days=self._keep_days)).strftime("%Y%m%d")
        for path in glob.glob(os.path.join(self._dir, f"{self._name}-????????.log")):
            if os.path.basename(path)[len(self._name) + 1:-4] < oldest:
                os.remove(path)


def usb_serial_adapters() -> Dict[str, str]:
    """by-path name → tty for the USB-serial adapters present (ttyUSB*: CH340,
    CP210x, FTDI).  Not ttyACM* — those are instruments such as the turbo
    pumps, which this must never open."""
    found = {}
    for path in sorted(glob.glob(BY_PATH + "/*")):
        tty = os.path.basename(os.path.realpath(path))
        if tty.startswith("ttyUSB"):
            found[path] = tty
    return found


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    which = parser.add_mutually_exclusive_group(required=True)
    which.add_argument("--port", help="tty to listen on (preferably a /dev/serial/by-path/ name)")
    which.add_argument("--wait-new", action="store_true",
                       help="take the first USB-serial adapter plugged in from now on")
    parser.add_argument("--baud", type=int, default=9600)
    parser.add_argument("--name", default="serial", help="names the log files")
    parser.add_argument("--out", default="~/serial-capture", help="directory for the log files")
    parser.add_argument("--routine", help="regex: lines matching it go to the file only")
    parser.add_argument("--silence-s", type=float, default=15.0,
                        help="report when nothing has been printed for this long")
    parser.add_argument("--keep-days", type=int, default=14)
    args = parser.parse_args(argv)

    import serial       # deferred: the logic above is testable without pyserial

    out = os.path.expanduser(args.out)
    os.makedirs(out, exist_ok=True)
    log = DailyLog(out, args.name, args.keep_days)

    def emit(text: str) -> None:
        print(text, flush=True)

    capture = Capture(log.write, emit, re.compile(args.routine) if args.routine else None,
                      args.silence_s)

    port = args.port
    remembered = os.path.join(out, args.name + ".port")
    if port is None and os.path.exists(remembered):
        with open(remembered) as fh:
            port = fh.read().strip() or None
        if port:
            capture.note(f"staying with {port}, chosen earlier ({remembered})", time.monotonic())
    present_at_start = set(usb_serial_adapters())
    if port is None:
        capture.note("waiting for a USB-serial adapter to be plugged in", time.monotonic())

    link = None
    next_open = 0.0
    last_error = ""
    try:
        while True:
            now = time.monotonic()
            if port is None:
                new = [p for p in usb_serial_adapters() if p not in present_at_start]
                if not new:
                    time.sleep(1.0)
                    continue
                port = new[0]
                with open(remembered, "w") as fh:
                    fh.write(port + "\n")
                time.sleep(1.0)         # let udev finish with the new device
            if link is None:
                if now < next_open:
                    time.sleep(0.5)
                    continue
                try:
                    # exclusive: nothing else on the Pi may read this console by mistake
                    link = serial.Serial(port, args.baud, timeout=1.0, exclusive=True)
                except Exception as exc:
                    if str(exc) != last_error:
                        capture.note(f"cannot open {port}: {exc}", now)
                    last_error = str(exc)
                    next_open = now + REOPEN_S
                    continue
                last_error = ""
                capture.opened(now)
                capture.note(f"listening on {port} ({os.path.basename(os.path.realpath(port))}) "
                             f"at {args.baud} baud", now)
            try:
                data = link.read(max(1, link.in_waiting))
            except Exception as exc:    # unplugged
                capture.note(f"port lost: {exc}", time.monotonic())
                capture.closed()
                try:
                    link.close()
                except Exception:
                    pass
                link = None
                next_open = time.monotonic() + REOPEN_S
                continue
            now = time.monotonic()
            capture.feed(data, now)
            capture.tick(now)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
