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
from typing import Any, Callable, Dict, Optional

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
        self._subscriptions: Dict[str, Callable] = {}

        self._client = mqtt.Client(client_id=client_id,
                                   protocol=mqtt.MQTTv5)
        self._client.on_connect = self._on_connect
        self._client.on_message = self._on_message
        self._client.on_disconnect = self._on_disconnect

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------

    def connect(self) -> None:
        log.info("Connecting to MQTT broker at %s:%d", self._host, self._port)
        self._client.connect(self._host, self._port, self._keepalive)
        self._client.loop_start()

    def disconnect(self) -> None:
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
        """Subscribe to a topic. callback(topic, payload_dict)."""
        with self._lock:
            self._subscriptions[topic] = callback
            self._client.subscribe(topic, qos=qos)
        log.debug("Subscribed to %s", topic)

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
            callbacks = list(self._subscriptions.items())
        for pattern, cb in callbacks:
            if mqtt.topic_matches_sub(pattern, topic):
                try:
                    cb(topic, payload)
                except Exception:
                    log.exception("Error in MQTT callback for %s", topic)

    def _on_disconnect(self, client, userdata, rc, properties=None):
        if rc != 0:
            log.warning("MQTT disconnected unexpectedly (rc=%d), "
                        "paho will attempt reconnect", rc)
