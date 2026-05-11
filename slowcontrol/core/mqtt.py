"""
Thin paho-mqtt wrapper.

Provides a single MqttClient class used by the service, drivers, and
controllers. All publish calls go through here so topic prefixes and QoS
are consistent.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from typing import Any, Callable, Dict, List, Optional

import paho.mqtt.client as mqtt

log = logging.getLogger(__name__)

# All topics live under this prefix.
TOPIC_PREFIX = "xsphere"


def sensor_topic(*parts: str) -> str:
    return f"{TOPIC_PREFIX}/sensors/" + "/".join(parts)


def status_topic(*parts: str) -> str:
    return f"{TOPIC_PREFIX}/status/" + "/".join(parts)


def command_topic(*parts: str) -> str:
    return f"{TOPIC_PREFIX}/commands/" + "/".join(parts)


class MqttClient:
    """Thread-safe MQTT client wrapper."""

    def __init__(self, host: str, port: int = 1883,
                 client_id: str = "xsphere-slowcontrol",
                 keepalive: int = 60):
        self._host = host
        self._port = port
        self._keepalive = keepalive
        self._lock = threading.Lock()
        self._connected = threading.Event()
        # topic pattern -> list of callbacks. Multiple components may subscribe
        # to the same topic (e.g. the PLC driver and the gradient controller
        # both listen on xsphere/commands/pid/+/setpoint); every matching
        # callback is invoked.
        self._subscriptions: Dict[str, List[Callable]] = {}

        self._client = mqtt.Client(client_id=client_id,
                                   protocol=mqtt.MQTTv5)
        self._client.on_connect = self._on_connect
        self._client.on_message = self._on_message
        self._client.on_disconnect = self._on_disconnect

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------

    def connect(self, wait_timeout: float = 10.0) -> None:
        log.info("Connecting to MQTT broker at %s:%d", self._host, self._port)
        self._connected.clear()
        self._client.connect(self._host, self._port, self._keepalive)
        self._client.loop_start()
        # Block until CONNACK so that any subscribe() calls made by drivers /
        # controllers immediately after this return actually reach the broker
        # (and are registered before the first command is published).
        if not self._connected.wait(timeout=wait_timeout):
            log.warning("MQTT broker did not acknowledge connection within %.1fs; "
                        "continuing anyway", wait_timeout)

    def disconnect(self) -> None:
        self._connected.clear()
        self._client.loop_stop()
        self._client.disconnect()
        log.info("Disconnected from MQTT broker")

    # ------------------------------------------------------------------
    # Publish
    # ------------------------------------------------------------------

    def publish(self, topic: str, payload: Any,
                qos: int = 1, retain: bool = False) -> None:
        """Publish a value. Dicts/lists are JSON-serialised automatically."""
        if isinstance(payload, (dict, list)):
            payload = json.dumps(payload)
        with self._lock:
            result = self._client.publish(topic, payload, qos=qos,
                                          retain=retain)
        if result.rc != mqtt.MQTT_ERR_SUCCESS:
            log.warning("MQTT publish failed on %s: rc=%d", topic, result.rc)

    def publish_sensor(self, *path_parts: str, payload: Any,
                       retain: bool = False) -> None:
        self.publish(sensor_topic(*path_parts), payload, retain=retain)

    def publish_status(self, *path_parts: str, payload: Any,
                       retain: bool = True) -> None:
        self.publish(status_topic(*path_parts), payload, retain=retain)

    # ------------------------------------------------------------------
    # Subscribe
    # ------------------------------------------------------------------

    def subscribe(self, topic: str,
                  callback: Callable[[str, Any], None],
                  qos: int = 1) -> None:
        """Register a callback for a topic. callback(topic, payload).

        Multiple callbacks may be registered for the same topic; all of them
        are invoked when a matching message arrives. The broker subscription
        is sent once per topic.
        """
        with self._lock:
            first = topic not in self._subscriptions
            self._subscriptions.setdefault(topic, []).append(callback)
            if first:
                self._client.subscribe(topic, qos=qos)
        log.debug("Subscribed to %s (%d callback(s))",
                  topic, len(self._subscriptions[topic]))

    # ------------------------------------------------------------------
    # Callbacks
    # ------------------------------------------------------------------

    def _on_connect(self, client, userdata, flags, rc, properties=None):
        if rc == 0:
            log.info("MQTT connected")
            # Re-subscribe after reconnect
            with self._lock:
                for topic in self._subscriptions:
                    client.subscribe(topic)
            self._connected.set()
        else:
            log.error("MQTT connect failed: rc=%d", rc)

    def _on_message(self, client, userdata, msg):
        topic = msg.topic
        try:
            payload = json.loads(msg.payload.decode())
        except (json.JSONDecodeError, UnicodeDecodeError):
            payload = msg.payload.decode(errors="replace")

        # Match against subscriptions (including wildcards via paho)
        with self._lock:
            matched = [
                cb
                for pattern, cbs in self._subscriptions.items()
                if mqtt.topic_matches_sub(pattern, topic)
                for cb in cbs
            ]
        for cb in matched:
            try:
                cb(topic, payload)
            except Exception:
                log.exception("Error in MQTT callback for %s", topic)

    def _on_disconnect(self, client, userdata, rc, properties=None):
        if rc != 0:
            log.warning("MQTT disconnected unexpectedly (rc=%d), "
                        "paho will attempt reconnect", rc)
