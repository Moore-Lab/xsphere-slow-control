"""
LabJack T7 thermometry high-rate diagnostic — for debugging ground loops,
RF pickup, and sensor noise that the 1 Hz slow-control polling smooths over.

Standalone tool, NOT part of the slow-control service. Connects directly
to the LabJack, polls every configured RTD + thermocouple channel at
~100 Hz, and plots a rolling 5-minute window in a single matplotlib
figure with 4 panels:

    Top-left   raw differential voltage     time series
    Top-right  raw differential voltage     histogram
    Bottom-L   temperature (from AIN_EF)    time series
    Bottom-R   temperature (from AIN_EF)    histogram

Each channel gets its own colour; RTDs draw with solid lines and TCs
with dashed lines so the two sensor families are visually separable
even when their voltage scales overlap.

The T7 only allows ONE Modbus/LJM session, so the slow-control service
must release it before this tool can connect.  Either stop it manually
or pass --stop-service (uses the passwordless sudoers entry installed in
/etc/sudoers.d/xsphere-restart on this Pi).  --stop-service also
registers an atexit handler that restarts slow-control when this tool
closes — useful in the common "run diag → close window → resume normal
operation" flow.

Closing the matplotlib window exits cleanly.  Ctrl-C in the terminal
also works.

Usage:
    # from the repo root
    python -m slowcontrol.tools.labjack_thermometry_diag --stop-service

    # custom rate / window
    python -m slowcontrol.tools.labjack_thermometry_diag --rate 200 --window 60

The temperature is read straight from the LabJack's AIN_EF_READ_A
register — i.e. the device does the PT100 / type-K conversion itself,
using the AIN_EF configuration that slow-control already wrote to flash.
No software side conversion needed.

Implementation notes
--------------------
Polling happens in a background thread that bulk-reads all configured
AINs + AIN_EF_READ_As in one Modbus transaction per cycle (typically
14 names for 3 RTDs + 4 TCs), sleeping just enough between cycles to
hit the target rate.  The matplotlib animation pulls a lock-protected
snapshot of the ring buffer at ~5 Hz and redraws.

For a usable visual at 30,000 samples × 7 channels, the time-series
plot downsamples by stride (configurable via --display-points); the
histogram always uses the full buffer.
"""

from __future__ import annotations

import argparse
import atexit
import logging
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import List

try:
    import numpy as np
except ImportError:                                                     # pragma: no cover
    sys.stderr.write("numpy not installed; pip install numpy\n"); sys.exit(2)

try:
    import yaml
except ImportError:                                                     # pragma: no cover
    sys.stderr.write("pyyaml not installed; pip install pyyaml\n"); sys.exit(2)

try:
    import matplotlib
    matplotlib.use("TkAgg")
    import matplotlib.pyplot as plt
    from matplotlib.animation import FuncAnimation
except ImportError:                                                     # pragma: no cover
    sys.stderr.write("matplotlib not installed; pip install matplotlib\n"); sys.exit(2)

try:
    from labjack import ljm
except ImportError:                                                     # pragma: no cover
    sys.stderr.write("labjack-ljm not installed; pip install labjack-ljm\n"); sys.exit(2)


log = logging.getLogger("labjack_thermometry_diag")


# ---------------------------------------------------------------------------
# Service-control helpers (uses the passwordless sudo entry on this Pi)
# ---------------------------------------------------------------------------

_SERVICE = "xsphere-slowcontrol"


def stop_slow_control() -> None:
    log.info("stopping %s ...", _SERVICE)
    subprocess.run(["sudo", "-n", "systemctl", "stop", _SERVICE], check=False)
    # The slow-control's stop hook closes its LJ session, but the LJM
    # library can take a second or two to release the socket. Wait briefly.
    time.sleep(2.0)


def start_slow_control() -> None:
    log.info("starting %s ...", _SERVICE)
    # reset-failed in case the watchdog tripped during diagnostics
    subprocess.run(["sudo", "-n", "systemctl", "reset-failed", _SERVICE], check=False)
    subprocess.run(["sudo", "-n", "systemctl", "start", _SERVICE], check=False)


# ---------------------------------------------------------------------------
# Ring buffer
# ---------------------------------------------------------------------------

class PollingBuffer:
    """Lock-protected ring buffer of (timestamp, voltages, temperatures)
    for `n_channels` channels and `capacity` samples each."""

    def __init__(self, n_channels: int, capacity: int):
        self._lock = threading.Lock()
        self._capacity = capacity
        self._ts = np.zeros(capacity, dtype=np.float64)
        self._v  = np.zeros((n_channels, capacity), dtype=np.float64)
        self._t  = np.zeros((n_channels, capacity), dtype=np.float64)
        self._next = 0
        self._filled = 0

    def push(self, t: float, voltages: np.ndarray, temps: np.ndarray) -> None:
        with self._lock:
            i = self._next
            self._ts[i] = t
            self._v[:, i] = voltages
            self._t[:, i] = temps
            self._next = (i + 1) % self._capacity
            if self._filled < self._capacity:
                self._filled += 1

    def snapshot(self):
        """Return chronologically-ordered (ts, v, t) arrays — copies."""
        with self._lock:
            if self._filled == 0:
                return None
            if self._filled < self._capacity:
                idx = slice(0, self._filled)
                return (self._ts[idx].copy(),
                        self._v[:, idx].copy(),
                        self._t[:, idx].copy())
            i = self._next
            return (
                np.concatenate([self._ts[i:], self._ts[:i]]),
                np.concatenate([self._v[:, i:], self._v[:, :i]], axis=1),
                np.concatenate([self._t[:, i:], self._t[:, :i]], axis=1),
            )


# ---------------------------------------------------------------------------
# Polling thread
# ---------------------------------------------------------------------------

def polling_loop(handle: int, channels: List[dict], buffer: PollingBuffer,
                 target_hz: float, stop_event: threading.Event,
                 stats: dict) -> None:
    """Bulk-read [AIN<n>, AIN<n>_EF_READ_A] for each channel at ~target_hz."""
    names: List[str] = []
    for c in channels:
        ain = int(c["ain"])
        names += [f"AIN{ain}", f"AIN{ain}_EF_READ_A"]
    n_names = len(names)
    n_channels = len(channels)
    period = 1.0 / target_hz

    last_log_time = time.monotonic()
    last_log_count = 0
    count = 0
    while not stop_event.is_set():
        t0 = time.monotonic()
        try:
            values = ljm.eReadNames(handle, n_names, names)
        except Exception as exc:
            log.warning("read error: %s — retrying", exc)
            if stop_event.wait(0.2):
                break
            continue
        arr = np.asarray(values, dtype=np.float64)
        # Interleaved: [V0, T0, V1, T1, ...].  Split with stride-2 views.
        v = arr[0::2][:n_channels]
        t = arr[1::2][:n_channels]
        buffer.push(t0, v, t)
        count += 1

        now = time.monotonic()
        if now - last_log_time >= 5.0:
            rate = (count - last_log_count) / (now - last_log_time)
            stats["rate_hz"] = rate
            log.debug("polling at %.1f Hz", rate)
            last_log_time = now
            last_log_count = count

        sleep_for = period - (time.monotonic() - t0)
        if sleep_for > 0 and not stop_event.wait(sleep_for):
            continue


# ---------------------------------------------------------------------------
# Plotting
# ---------------------------------------------------------------------------

def build_figure(channels: List[dict], window_s: float):
    fig, axes = plt.subplots(2, 2, figsize=(14, 9))
    (ax_v_ts, ax_v_hist), (ax_t_ts, ax_t_hist) = axes

    ax_v_ts.set_title(f"Differential voltage — rolling {window_s:.0f} s")
    ax_v_ts.set_xlabel("time (s ago)")
    ax_v_ts.set_ylabel("voltage (V)")
    ax_v_ts.grid(alpha=0.3)

    ax_v_hist.set_title(f"Voltage histogram — {window_s:.0f} s")
    ax_v_hist.set_xlabel("voltage (V)")
    ax_v_hist.set_ylabel("counts")
    ax_v_hist.grid(alpha=0.3)

    ax_t_ts.set_title(f"Temperature (LJ AIN_EF) — rolling {window_s:.0f} s")
    ax_t_ts.set_xlabel("time (s ago)")
    ax_t_ts.set_ylabel("temperature (K)")
    ax_t_ts.grid(alpha=0.3)

    ax_t_hist.set_title(f"Temperature histogram — {window_s:.0f} s")
    ax_t_hist.set_xlabel("temperature (K)")
    ax_t_hist.set_ylabel("counts")
    ax_t_hist.grid(alpha=0.3)

    # Persistent Line2D objects for the time-series panels. Histograms
    # are redrawn from scratch each frame (matplotlib has no native
    # update-in-place primitive for histograms; redrawing 80-bin hists
    # for 7 channels is cheap).
    colors = plt.colormaps["tab10"](np.linspace(0, 1, max(10, len(channels)))[:len(channels)])
    v_lines, t_lines = [], []
    for ch, color in zip(channels, colors):
        kind = ch.get("kind", "tc").lower()
        # Visual separator between RTDs and TCs without forcing the user
        # to read the legend: solid = RTD, dashed = TC.
        linestyle = "-" if kind == "rtd" else "--"
        label = f"{ch.get('name', 'ch')} {kind.upper()} — {ch.get('label', '')}"
        v_line, = ax_v_ts.plot([], [], color=color, linestyle=linestyle,
                               linewidth=0.8, label=label)
        t_line, = ax_t_ts.plot([], [], color=color, linestyle=linestyle,
                               linewidth=0.8, label=label)
        v_lines.append(v_line); t_lines.append(t_line)

    ax_v_ts.legend(loc="upper right", fontsize=7, ncol=2)
    ax_t_ts.legend(loc="upper right", fontsize=7, ncol=2)

    fig.tight_layout(rect=[0, 0, 1, 0.96])
    return fig, axes, v_lines, t_lines, colors


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    default_cfg = Path(__file__).resolve().parents[1] / "config.yaml"
    ap.add_argument("-c", "--config", default=str(default_cfg),
                    help=f"slow-control config.yaml (default: {default_cfg})")
    ap.add_argument("--rate", type=float, default=100.0,
                    help="polling rate target in Hz (default: 100)")
    ap.add_argument("--window", type=float, default=300.0,
                    help="rolling display window in seconds (default: 300)")
    ap.add_argument("--display-points", type=int, default=10_000,
                    help="downsample time-series to at most this many points "
                         "per channel for plotting (histograms still use all "
                         "samples) (default: 10000)")
    ap.add_argument("--refresh-ms", type=int, default=200,
                    help="GUI refresh interval in milliseconds (default: 200)")
    ap.add_argument("--stop-service", action="store_true",
                    help="stop xsphere-slowcontrol on entry and restart it on "
                         "exit; uses passwordless sudo")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")

    with open(args.config) as fh:
        cfg = yaml.safe_load(fh) or {}
    lj_cfg = cfg.get("labjack") or {}
    channels = lj_cfg.get("thermometry_channels") or []
    if not channels:
        log.error("no labjack.thermometry_channels in %s", args.config)
        return 1

    if args.stop_service:
        stop_slow_control()
        atexit.register(start_slow_control)

    conn_type = lj_cfg.get("connection_type", "ETHERNET")
    dev_id    = lj_cfg.get("device_identifier", "ANY")
    log.info("opening T7 over %s @ %s ...", conn_type, dev_id)
    try:
        handle = ljm.openS("T7", conn_type, dev_id)
    except Exception as exc:
        log.error("LabJack open failed: %s", exc)
        log.error("Hint: the slow-control service may still hold the LJ session. "
                  "Either stop it first, or rerun with --stop-service.")
        return 1
    serial = int(ljm.eReadName(handle, "SERIAL_NUMBER"))
    log.info("connected to T7 serial=%d (%d channels)", serial, len(channels))

    # Ring buffer big enough for the requested window at the requested rate
    # with 20% headroom (so a brief over-rate burst doesn't lose history).
    capacity = max(1024, int(args.window * args.rate * 1.2))
    buffer = PollingBuffer(len(channels), capacity)

    stop_event = threading.Event()
    stats = {"rate_hz": 0.0}
    pt = threading.Thread(target=polling_loop,
                          args=(handle, channels, buffer, args.rate,
                                stop_event, stats),
                          name="lj-poll", daemon=True)
    pt.start()

    fig, axes, v_lines, t_lines, colors = build_figure(channels, args.window)
    (ax_v_ts, ax_v_hist), (ax_t_ts, ax_t_hist) = axes

    def downsample(x, y, n_max):
        if x.size <= n_max:
            return x, y
        step = max(1, x.size // n_max)
        return x[::step], y[..., ::step]

    def update(frame):
        snap = buffer.snapshot()
        if snap is None:
            return ()
        ts, v, t = snap
        now = time.monotonic()
        x_full = ts - now                  # negative seconds: now is 0
        mask = x_full >= -args.window
        x_full = x_full[mask]
        v_full = v[:, mask]
        t_full = t[:, mask]

        # Time-series downsample (display only — histograms use all data).
        x_disp, v_disp = downsample(x_full, v_full, args.display_points)
        _,      t_disp = downsample(x_full, t_full, args.display_points)

        for line, vch in zip(v_lines, v_disp):
            line.set_data(x_disp, vch)
        for line, tch in zip(t_lines, t_disp):
            line.set_data(x_disp, tch)

        for ax in (ax_v_ts, ax_t_ts):
            ax.relim(); ax.autoscale_view()
            ax.set_xlim(-args.window, 0)

        # Histograms — clear & redraw from full data.
        ax_v_hist.cla()
        ax_v_hist.set_title(f"Voltage histogram — {args.window:.0f} s")
        ax_v_hist.set_xlabel("voltage (V)"); ax_v_hist.set_ylabel("counts")
        ax_v_hist.grid(alpha=0.3)
        ax_t_hist.cla()
        ax_t_hist.set_title(f"Temperature histogram — {args.window:.0f} s")
        ax_t_hist.set_xlabel("temperature (K)"); ax_t_hist.set_ylabel("counts")
        ax_t_hist.grid(alpha=0.3)
        for ch, color, vch, tch in zip(channels, colors, v_full, t_full):
            label = ch.get("name", "ch")
            # `step` histtype overlays cleanly across multiple channels;
            # filling would obscure overlap.
            ax_v_hist.hist(vch, bins=80, histtype="step", color=color, label=label)
            ax_t_hist.hist(tch, bins=80, histtype="step", color=color, label=label)
        ax_v_hist.legend(loc="upper right", fontsize=7)
        ax_t_hist.legend(loc="upper right", fontsize=7)

        n = ts.size
        elapsed = ts[-1] - ts[0] if n > 1 else 0.0
        achieved = (n - 1) / elapsed if elapsed > 0 else 0.0
        fig.suptitle(
            f"LabJack T7 (serial {serial}) thermometry diagnostic — "
            f"target {args.rate:.0f} Hz, achieved {achieved:.1f} Hz, "
            f"{n} samples / {args.window:.0f} s window",
            fontsize=10,
        )
        return ()

    # cache_frame_data=False because each frame returns nothing reusable
    ani = FuncAnimation(fig, update, interval=args.refresh_ms,
                        blit=False, cache_frame_data=False)

    # Keep a reference to `ani` so it isn't garbage-collected (it doesn't
    # actually need to be used, but losing the ref stops the animation).
    fig._diag_ani = ani

    log.info("polling at %.1f Hz; close the window or Ctrl-C to exit.",
             args.rate)
    try:
        plt.show()
    except KeyboardInterrupt:
        pass

    log.info("shutting down")
    stop_event.set()
    pt.join(timeout=2.0)
    try:
        ljm.close(handle)
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
