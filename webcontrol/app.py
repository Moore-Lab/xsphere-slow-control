#!/usr/bin/env python3
"""xsphere web control panel.

A small Flask app that bridges the MQTT slow-control bus to a browser UI:
it caches every xsphere/status/# and xsphere/sensors/# message, serves the
current state at /api/state, and publishes xsphere/commands/# messages on
behalf of the page. Also runs a "follow a sensor" loop and an ad-hoc ramp
sequencer in background threads.

Run:
    python webcontrol/app.py            # serves on 0.0.0.0:8088
Environment overrides:
    XSPHERE_MQTT_HOST (default localhost)   XSPHERE_MQTT_PORT (1883)
    XSPHERE_WEB_HOST  (default 0.0.0.0)      XSPHERE_WEB_PORT  (8088)
"""

from __future__ import annotations

import json
import os
import threading
import time

import paho.mqtt.client as mqtt
from flask import Flask, jsonify, render_template, request

MQTT_HOST = os.environ.get("XSPHERE_MQTT_HOST", "localhost")
MQTT_PORT = int(os.environ.get("XSPHERE_MQTT_PORT", "1883"))
WEB_HOST = os.environ.get("XSPHERE_WEB_HOST", "0.0.0.0")
WEB_PORT = int(os.environ.get("XSPHERE_WEB_PORT", "8088"))

_here = os.path.dirname(os.path.abspath(__file__))
app = Flask(__name__, template_folder=os.path.join(_here, "templates"))

# ---------------------------------------------------------------------------
# MQTT state cache
# ---------------------------------------------------------------------------
_state: dict = {}                 # topic -> {"payload": <decoded>, "ts": <epoch>}
_state_lock = threading.Lock()

_follow = {"on": False, "zone": "top", "src": ""}     # follow-a-sensor config
_seq = {"running": False, "status": "idle", "stop": threading.Event(), "thread": None}

_client = mqtt.Client(client_id="xsphere-webcontrol", protocol=mqtt.MQTTv5,
                      callback_api_version=mqtt.CallbackAPIVersion.VERSION2) \
    if hasattr(mqtt, "CallbackAPIVersion") else mqtt.Client(client_id="xsphere-webcontrol",
                                                            protocol=mqtt.MQTTv5)


def _on_connect(client, userdata, flags, reason_code, properties=None):
    client.subscribe("xsphere/status/#", qos=1)
    client.subscribe("xsphere/sensors/#", qos=1)


def _on_message(client, userdata, msg):
    try:
        payload = json.loads(msg.payload.decode())
    except Exception:
        payload = msg.payload.decode("utf-8", "replace")
    with _state_lock:
        _state[msg.topic] = {"payload": payload, "ts": time.time()}
    # follow-a-sensor: republish the chosen RTD's value as the zone's setpoint
    if (_follow["on"] and msg.topic == _follow["src"]
            and msg.topic.startswith("xsphere/sensors/temperature/")
            and isinstance(payload, dict)):
        vk = payload.get("value_k")
        if vk is not None:
            try:
                _publish(f"xsphere/commands/pid/{_follow['zone']}/setpoint",
                         {"value_k": float(vk)})
            except (TypeError, ValueError):
                pass


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
    return render_template("index.html")


@app.route("/api/state")
def api_state():
    with _state_lock:
        snap = {t: v for t, v in _state.items()}
    return jsonify({
        "now": time.time(),
        "state": snap,
        "follow": _follow,
        "seq": {"running": _seq["running"], "status": _seq["status"]},
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


@app.route("/api/follow", methods=["GET", "POST"])
def api_follow():
    if request.method == "POST":
        d = request.get_json(force=True, silent=True) or {}
        for k in ("on", "zone", "src"):
            if k in d:
                _follow[k] = bool(d[k]) if k == "on" else str(d[k])
    return jsonify(_follow)


def _seq_worker(steps):
    _seq["stop"].clear()
    _seq["running"] = True
    try:
        for i, (zone, vk, hold_min) in enumerate(steps):
            if _seq["stop"].is_set():
                _seq["status"] = "stopped"
                return
            topic = ("xsphere/commands/gradient/base" if zone == "base"
                     else f"xsphere/commands/pid/{zone}/setpoint")
            _publish(topic, {"value_k": vk})
            _seq["status"] = (f"step {i+1}/{len(steps)}: {zone} → {vk} K, "
                              f"hold {hold_min} min")
            if _seq["stop"].wait(hold_min * 60.0):
                _seq["status"] = "stopped"
                return
        _seq["status"] = f"done ({len(steps)} steps)"
    finally:
        _seq["running"] = False


@app.route("/api/seq", methods=["POST"])
def api_seq():
    d = request.get_json(force=True, silent=True) or {}
    action = d.get("action")
    if action == "stop":
        _seq["stop"].set()
        return jsonify({"ok": True})
    if action == "run":
        if _seq["running"]:
            return jsonify({"error": "already running"}), 409
        steps = []
        for line in str(d.get("steps", "")).splitlines():
            parts = line.replace(",", " ").split()
            if len(parts) >= 3:
                try:
                    steps.append((parts[0], float(parts[1]), float(parts[2])))
                except ValueError:
                    pass
        if not steps:
            return jsonify({"error": "no valid steps (expect: ZONE TARGET_K HOLD_MIN)"}), 400
        _seq["thread"] = threading.Thread(target=_seq_worker, args=(steps,), daemon=True)
        _seq["thread"].start()
        return jsonify({"ok": True, "steps": len(steps)})
    return jsonify({"error": "unknown action"}), 400


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
