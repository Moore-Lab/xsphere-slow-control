#!/usr/bin/env python3
"""xsphere web control panel.

A small Flask app that bridges the MQTT slow-control bus to a browser UI:

* It subscribes to the consolidated state snapshot the slow-control service
  publishes on  xsphere/state/snapshot  (see slowcontrol/STATE_LAYER_PLAN.md)
  and serves it at /api/state — the page renders both its read-out and its
  control widgets from that (the snapshot carries each state's kind, unit,
  freshness, moving averages and, where applicable, its command topic/payload).
* It also keeps the raw xsphere/status/# and xsphere/sensors/# cache, for the
  few bespoke cards (gradient scan, follow-a-sensor, ramp sequencer) that need
  detail not in the registry.
* It publishes xsphere/commands/# messages on behalf of the page, and runs a
  "follow a sensor" loop and an ad-hoc ramp sequencer in background threads.

Run:
    python webcontrol/app.py            # serves on 0.0.0.0:8088
Environment overrides:
    XSPHERE_MQTT_HOST (default localhost)   XSPHERE_MQTT_PORT (1883)
    XSPHERE_WEB_HOST  (default 0.0.0.0)      XSPHERE_WEB_PORT  (8088)
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from pathlib import Path
from typing import Optional

import markdown as _md
import numpy as np
import paho.mqtt.client as mqtt
import yaml
from flask import Flask, jsonify, redirect, render_template, request, url_for

MQTT_HOST = os.environ.get("XSPHERE_MQTT_HOST", "localhost")
MQTT_PORT = int(os.environ.get("XSPHERE_MQTT_PORT", "1883"))
WEB_HOST = os.environ.get("XSPHERE_WEB_HOST", "0.0.0.0")
WEB_PORT = int(os.environ.get("XSPHERE_WEB_PORT", "8088"))

_here = os.path.dirname(os.path.abspath(__file__))
app = Flask(__name__, template_folder=os.path.join(_here, "templates"))
README_PATH = os.path.normpath(os.path.join(_here, "..", "README.md"))

# ---------------------------------------------------------------------------
# MQTT state cache
# ---------------------------------------------------------------------------
_state: dict = {}                 # topic -> {"payload": <decoded>, "ts": <epoch>}
_snapshot: dict = {}              # latest xsphere/state/snapshot payload
_snapshot_ts: float = 0.0         # epoch when it was received
_state_lock = threading.Lock()

SNAPSHOT_TOPIC = "xsphere/state/snapshot"

_client = mqtt.Client(client_id="xsphere-webcontrol", protocol=mqtt.MQTTv5,
                      callback_api_version=mqtt.CallbackAPIVersion.VERSION2) \
    if hasattr(mqtt, "CallbackAPIVersion") else mqtt.Client(client_id="xsphere-webcontrol",
                                                            protocol=mqtt.MQTTv5)


def _on_connect(client, userdata, flags, reason_code, properties=None):
    client.subscribe("xsphere/status/#", qos=1)
    client.subscribe("xsphere/sensors/#", qos=1)
    client.subscribe(SNAPSHOT_TOPIC, qos=1)


def _on_message(client, userdata, msg):
    global _snapshot, _snapshot_ts
    try:
        payload = json.loads(msg.payload.decode())
    except Exception:
        payload = msg.payload.decode("utf-8", "replace")
    if msg.topic == SNAPSHOT_TOPIC:
        if isinstance(payload, dict):
            with _state_lock:
                _snapshot = payload
                _snapshot_ts = time.time()
        return
    with _state_lock:
        _state[msg.topic] = {"payload": payload, "ts": time.time()}


_client.on_connect = _on_connect
_client.on_message = _on_message


def _publish(topic: str, payload) -> None:
    if not isinstance(payload, (str, bytes)):
        payload = json.dumps(payload)
    _client.publish(topic, payload, qos=1)


# ---------------------------------------------------------------------------
# HTTP API
# ---------------------------------------------------------------------------
@app.route("/")
def index():
    # The "register GUI": a centralized read-out of every state.
    return render_template("index.html")


@app.route("/control")
def control():
    # The "control GUI": valves, heaters/PID, gradient, and automation.
    return render_template("control.html")


@app.route("/readme")
def readme():
    # Renders the repo-root README.md so operators can read it in the browser.
    try:
        with open(README_PATH) as fh:
            src = fh.read()
    except OSError as exc:
        return (f"<h1>README not found</h1><pre>{exc}</pre>", 404)
    body = _md.markdown(src, extensions=["fenced_code", "tables", "toc", "sane_lists"])
    return render_template("readme.html", body=body)


@app.route("/api/state")
def api_state():
    with _state_lock:
        raw = {t: v for t, v in _state.items()}
        snapshot = dict(_snapshot)
        snap_ts = _snapshot_ts
    return jsonify({
        "now": time.time(),
        "snapshot": snapshot,                       # consolidated state (xsphere/state/snapshot)
        "snapshot_age": (time.time() - snap_ts) if snap_ts else None,
        "state": raw,                               # raw xsphere/status|sensors cache (bespoke cards)
        "mqtt_connected": _client.is_connected(),
    })


@app.route("/api/cmd", methods=["POST"])
def api_cmd():
    d = request.get_json(force=True, silent=True) or {}
    topic = str(d.get("topic", ""))
    if not topic.startswith("xsphere/commands/"):
        return jsonify({"error": "topic must be under xsphere/commands/"}), 400
    _publish(topic, d.get("payload", {}))
    return jsonify({"ok": True})


@app.route("/sequencer")
def sequencer():
    # The sequencer page: build, save and run a multi-step program.
    return render_template("sequencer.html")


# ---------------------------------------------------------------------------
# LabJack thermometry diagnostic — high-rate scope + histograms
# ---------------------------------------------------------------------------
# When the user clicks Start on /diag, this process:
#   1. Stops xsphere-slowcontrol (the T7 only allows one LJM session)
#   2. Opens its own LJM session
#   3. Spawns a background thread that bulk-reads all configured AINs and
#      AIN_EF_READ_As at the target rate, filling a lock-protected ring buffer.
#   4. Serves /diag/data, returning a downsampled snapshot + per-channel
#      histograms as JSON for the page's Plotly.js plots.
# Clicking Stop tears down (close LJ, restart slow-control).
#
# All of this is in-process inside webcontrol so the Pi can stay headless —
# no matplotlib window, no extra service to manage.

_DIAG_CFG_PATH = Path(__file__).resolve().parent.parent / "slowcontrol" / "config.yaml"

_diag_lock = threading.Lock()
_diag = {
    "active": False,
    "channels": [],              # list of channel-config dicts
    "ljm_handle": None,
    "poll_thread": None,
    "stop_event": None,
    "buffer_ts": None,           # np.float64[capacity]
    "buffer_v":  None,           # np.float64[n_ch, capacity]
    "buffer_t":  None,           # np.float64[n_ch, capacity]
    "buf_next": 0,
    "buf_filled": 0,
    "buf_capacity": 0,
    "rate_hz": 100.0,
    "window_s": 300.0,
    "achieved_hz": 0.0,
    "started_at": None,
    "last_error": None,
}


def _diag_load_lj_cfg() -> dict:
    with open(_DIAG_CFG_PATH) as fh:
        return (yaml.safe_load(fh) or {}).get("labjack") or {}


def _diag_poll_loop():
    """Read voltages every cycle and temperatures at ~1/10 the rate.

    Why split: AIN_EF_READ_A includes the AIN_EF's internal settling and
    computation, which dominates the round-trip; reading {AIN<n>,
    AIN<n>_EF_READ_A} for all 7 channels in one transaction tops out around
    5–10 Hz. Voltages alone (just AIN<n>) reach ~100 Hz comfortably.

    Each push to the ring buffer carries the *current* voltage and the
    *most-recent* temperature — fine for visual diagnostics where the
    temperature panel only needs to track much slower physical change.
    """
    from labjack import ljm  # noqa: WPS433 — local import so webcontrol starts even without ljm
    state = _diag
    h        = state["ljm_handle"]
    channels = state["channels"]
    stop_ev  = state["stop_event"]
    rate     = state["rate_hz"]
    period   = 1.0 / rate
    n_ch     = len(channels)
    v_names  = [f"AIN{int(c['ain'])}"             for c in channels]
    t_names  = [f"AIN{int(c['ain'])}_EF_READ_A"   for c in channels]
    # Decimate temperature reads to ~10 Hz max (or every cycle if the
    # voltage rate is already slower than that).
    t_min_period = max(period, 0.1)

    cached_t = np.full(n_ch, float("nan"), dtype=np.float64)
    last_t_read = 0.0
    last_log_time  = time.monotonic()
    last_log_count = 0
    count = 0
    while not stop_ev.is_set():
        t0 = time.monotonic()
        try:
            v_values = ljm.eReadNames(h, n_ch, v_names)
        except Exception as exc:
            state["last_error"] = str(exc)
            if stop_ev.wait(0.2):
                break
            continue
        v = np.asarray(v_values, dtype=np.float64)

        # Periodic temperature read — synchronous with the voltage cycle so
        # we don't need a second LJ thread / lock.
        if (t0 - last_t_read) >= t_min_period:
            try:
                t_values = ljm.eReadNames(h, n_ch, t_names)
                cached_t = np.asarray(t_values, dtype=np.float64)
                last_t_read = t0
            except Exception as exc:
                # Bad EF read shouldn't kill the V stream; just keep cached_t.
                state["last_error"] = f"temp read: {exc}"

        with _diag_lock:
            i = state["buf_next"]
            cap = state["buf_capacity"]
            state["buffer_ts"][i] = t0
            state["buffer_v"][:, i] = v
            state["buffer_t"][:, i] = cached_t
            state["buf_next"] = (i + 1) % cap
            if state["buf_filled"] < cap:
                state["buf_filled"] += 1
        count += 1

        now = time.monotonic()
        if now - last_log_time >= 2.0:
            state["achieved_hz"] = (count - last_log_count) / (now - last_log_time)
            last_log_time = now
            last_log_count = count

        sleep_for = period - (time.monotonic() - t0)
        if sleep_for > 0:
            stop_ev.wait(sleep_for)


def _diag_teardown_and_restart_slowcontrol():
    """Stop the polling thread, close LJ, restart slow-control."""
    with _diag_lock:
        if not _diag["active"]:
            return
        stop_event = _diag["stop_event"]
        thread     = _diag["poll_thread"]
        handle     = _diag["ljm_handle"]
        _diag["active"] = False
        _diag["ljm_handle"] = None
        _diag["poll_thread"] = None
    if stop_event is not None:
        stop_event.set()
    if thread is not None:
        thread.join(timeout=3.0)
    if handle is not None:
        try:
            from labjack import ljm
            ljm.close(handle)
        except Exception:
            pass
    # reset-failed in case the watchdog tripped during diagnostics; then start
    subprocess.run(["sudo", "-n", "systemctl", "reset-failed", "xsphere-slowcontrol"],
                   capture_output=True, timeout=5)
    subprocess.run(["sudo", "-n", "systemctl", "start", "xsphere-slowcontrol"],
                   capture_output=True, timeout=10)


# Ensure slow-control is back if webcontrol exits while diag is active.
import atexit as _atexit
_atexit.register(_diag_teardown_and_restart_slowcontrol)


@app.route("/diag")
def diag_page():
    return render_template("diag.html")


@app.route("/diag/start", methods=["POST"])
def diag_start():
    body = request.get_json(force=True, silent=True) or {}
    try:
        rate     = float(body.get("rate_hz", 100.0))
        window_s = float(body.get("window_s", 300.0))
    except (TypeError, ValueError):
        return jsonify({"error": "rate_hz / window_s must be numeric"}), 400
    if not (1.0 <= rate <= 1000.0):
        return jsonify({"error": "rate_hz must be in [1, 1000]"}), 400
    if not (5.0 <= window_s <= 3600.0):
        return jsonify({"error": "window_s must be in [5, 3600]"}), 400

    with _diag_lock:
        if _diag["active"]:
            return jsonify({"error": "diagnostic already active — Stop first"}), 409

    try:
        lj_cfg = _diag_load_lj_cfg()
        channels = lj_cfg.get("thermometry_channels") or []
        if not channels:
            return jsonify({"error": "no labjack.thermometry_channels in config"}), 500

        # Stop slow-control to release the LJM session
        r = subprocess.run(
            ["sudo", "-n", "systemctl", "stop", "xsphere-slowcontrol"],
            capture_output=True, text=True, timeout=15)
        if r.returncode != 0:
            return jsonify({"error":
                f"stop xsphere-slowcontrol failed: {r.stderr.strip() or r.stdout.strip()}"
            }), 500
        # LJM library may take a beat to release the socket
        time.sleep(2.0)

        # Open LJ — lazy import so webcontrol can start without ljm installed
        from labjack import ljm
        handle = ljm.openS("T7",
                           lj_cfg.get("connection_type", "ETHERNET"),
                           lj_cfg.get("device_identifier", "ANY"))

        # Force fastest single-shot ADC mode on the AINs we'll poll.
        # Default RESOLUTION_INDEX (0=auto) picks slower settling for accuracy;
        # measured cost vs index=1 was ~80 ms vs ~24 ms per 7-channel read,
        # i.e. command-response tops out near ~40 Hz at index=1 vs ~13 Hz
        # at the default. This change is transient: slow-control's own
        # connect() resets RESOLUTION_INDEX back to 0 the next time the
        # service starts, so we never leave the LJ in a non-default state.
        for c in channels:
            try:
                ljm.eWriteName(handle, f"AIN{int(c['ain'])}_RESOLUTION_INDEX", 1.0)
            except Exception as exc:
                # Non-fatal; we'll just run at the slower default rate.
                _diag["last_error"] = f"set RES_INDEX: {exc}"

        # Allocate ring buffer with 20% headroom over window × rate
        n_ch = len(channels)
        capacity = max(1024, int(window_s * rate * 1.2))
        stop_event = threading.Event()
        with _diag_lock:
            _diag.update({
                "active":        True,
                "channels":      channels,
                "ljm_handle":    handle,
                "stop_event":    stop_event,
                "rate_hz":       rate,
                "window_s":      window_s,
                "buffer_ts":     np.zeros(capacity, dtype=np.float64),
                "buffer_v":      np.zeros((n_ch, capacity), dtype=np.float64),
                "buffer_t":      np.zeros((n_ch, capacity), dtype=np.float64),
                "buf_next":      0,
                "buf_filled":    0,
                "buf_capacity":  capacity,
                "achieved_hz":   0.0,
                "started_at":    time.monotonic(),
                "last_error":    None,
            })
        t = threading.Thread(target=_diag_poll_loop, name="diag-poll", daemon=True)
        with _diag_lock:
            _diag["poll_thread"] = t
        t.start()

        return jsonify({
            "ok": True,
            "rate_hz": rate,
            "window_s": window_s,
            "channels": [
                {"name": c.get("name"), "kind": c.get("kind"),
                 "label": c.get("label", ""), "ain": c.get("ain")}
                for c in channels
            ],
        })
    except Exception as exc:
        # If anything failed after stopping slow-control, restart it so the
        # cryostat doesn't get left without its safety interlock.
        subprocess.run(["sudo", "-n", "systemctl", "reset-failed", "xsphere-slowcontrol"],
                       capture_output=True, timeout=5)
        subprocess.run(["sudo", "-n", "systemctl", "start", "xsphere-slowcontrol"],
                       capture_output=True, timeout=10)
        with _diag_lock:
            _diag["active"] = False
            _diag["last_error"] = str(exc)
        return jsonify({"error": f"start failed: {exc}"}), 500


@app.route("/diag/stop", methods=["POST"])
def diag_stop():
    with _diag_lock:
        if not _diag["active"]:
            return jsonify({"error": "diagnostic not active"}), 400
    _diag_teardown_and_restart_slowcontrol()
    return jsonify({"ok": True})


@app.route("/diag/status")
def diag_status():
    with _diag_lock:
        if not _diag["active"]:
            return jsonify({"active": False, "last_error": _diag["last_error"]})
        return jsonify({
            "active":       True,
            "rate_hz":      _diag["rate_hz"],
            "window_s":     _diag["window_s"],
            "achieved_hz":  _diag["achieved_hz"],
            "sample_count": _diag["buf_filled"],
            "elapsed_s":    time.monotonic() - _diag["started_at"]
                            if _diag["started_at"] else 0.0,
            "last_error":   _diag["last_error"],
            "channels": [
                {"name": c.get("name"), "kind": c.get("kind"),
                 "label": c.get("label", ""), "ain": c.get("ain")}
                for c in _diag["channels"]
            ],
        })


@app.route("/diag/data")
def diag_data():
    """Snapshot + downsample + histogram, return JSON.

    Query args:
      display_points : max points per channel in time series (default 1000)
      bins           : histogram bin count (default 80)
    """
    try:
        n_disp = max(50, min(20_000, int(request.args.get("display_points", 1000))))
    except ValueError:
        n_disp = 1000
    try:
        n_bins = max(10, min(400, int(request.args.get("bins", 80))))
    except ValueError:
        n_bins = 80

    with _diag_lock:
        if not _diag["active"] or _diag["buf_filled"] == 0:
            return jsonify({
                "active": _diag["active"],
                "samples": 0,
                "achieved_hz": _diag["achieved_hz"],
                "channels": [],
            })
        n   = _diag["buf_filled"]
        cap = _diag["buf_capacity"]
        i   = _diag["buf_next"]
        window_s = _diag["window_s"]
        channels = list(_diag["channels"])
        achieved = _diag["achieved_hz"]
        if n < cap:
            ts = _diag["buffer_ts"][:n].copy()
            v  = _diag["buffer_v"][:, :n].copy()
            t  = _diag["buffer_t"][:, :n].copy()
        else:
            ts = np.concatenate([_diag["buffer_ts"][i:], _diag["buffer_ts"][:i]])
            v  = np.concatenate([_diag["buffer_v"][:, i:], _diag["buffer_v"][:, :i]], axis=1)
            t  = np.concatenate([_diag["buffer_t"][:, i:], _diag["buffer_t"][:, :i]], axis=1)

    # Clip to display window
    now = time.monotonic()
    x = ts - now                                  # seconds ago (≤ 0)
    mask = x >= -window_s
    x = x[mask]; v = v[:, mask]; t = t[:, mask]

    # Downsample time series for display
    n_pts = x.size
    if n_pts > n_disp:
        step = max(1, n_pts // n_disp)
        x_disp = x[::step]
        v_disp = v[:, ::step]
        t_disp = t[:, ::step]
    else:
        x_disp, v_disp, t_disp = x, v, t

    # Per-channel histograms over the FULL window (not the downsampled view)
    v_hist, t_hist = [], []
    for ch in range(v.shape[0]):
        if v[ch].size > 1:
            vc, ve = np.histogram(v[ch], bins=n_bins)
            tc, te = np.histogram(t[ch], bins=n_bins)
        else:
            vc, ve, tc, te = (np.empty(0),) * 4
        v_hist.append({"counts": vc.tolist(), "edges": ve.tolist()})
        t_hist.append({"counts": tc.tolist(), "edges": te.tolist()})

    return jsonify({
        "active":       True,
        "samples":      int(n_pts),
        "achieved_hz":  achieved,
        "window_s":     window_s,
        "channels": [
            {"name": c.get("name"), "kind": c.get("kind"),
             "label": c.get("label", ""), "ain": c.get("ain")}
            for c in channels
        ],
        "time_series": {
            "t_ago_s": x_disp.tolist(),
            "v":       [vc.tolist() for vc in v_disp],
            "t":       [tc.tolist() for tc in t_disp],
        },
        "v_hist": v_hist,
        "t_hist": t_hist,
    })


@app.route("/favicon.ico")
def favicon_ico():
    # Some browsers ignore <link rel="icon"> and request the legacy path.
    # Redirect to the SVG so the tab/bookmark icon shows up consistently.
    return redirect(url_for("static", filename="favicon.svg"), code=301)


# ---- Legacy /api/seq stub (preserved as an explicit 410) ---------------------
# The old in-process ramp sequencer was replaced by the SequencerController in
# the slow-control service.  Anything that POSTed to /api/seq should now
# publish to xsphere/commands/sequencer/{set,run,stop,clear,append} (the
# Sequencer page does that through /api/cmd).
@app.route("/api/seq", methods=["POST"])
def api_seq_gone():
    return jsonify({
        "error": "use xsphere/commands/sequencer/... via /api/cmd instead",
    }), 410


# ---------------------------------------------------------------------------
def main():
    _client.connect(MQTT_HOST, MQTT_PORT, keepalive=60)
    _client.loop_start()
    try:
        from waitress import serve
        serve(app, host=WEB_HOST, port=WEB_PORT, threads=8)
    except ImportError:
        app.run(host=WEB_HOST, port=WEB_PORT, threaded=True)


if __name__ == "__main__":
    main()
