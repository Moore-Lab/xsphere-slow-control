"""
Main service orchestrator.

Starts all drivers and controllers in dependency order, runs the heartbeat
loop, and handles clean shutdown on SIGTERM / SIGINT.

Startup order
─────────────
  1. MQTT connect
  2. Drivers start (PlcDriver polls Modbus registers and publishes sensor data)
  3. StateStore start (subscribes to every source topic in state.yaml; the
     subscriptions catch the retained status messages before the controllers
     start, and the consolidated snapshot is republished from then on)
  4. Controllers start (subscribe to MQTT, no polling)
     a. GradientController   — setpoint coupling logic
     b. AutovalveController  — autofill state machines
     c. InterlocksController — safety watchdog
     d. TrackerController    — keeps one state tied to another (+ offset)
     e. SequencerController  — runs an ordered program of write/track actions
        (also subsumes the old GradientScannerPlugin via its Sweep item type)
  5. Heartbeat loop (blocks until SIGTERM / SIGINT)
  6. Shutdown in reverse order
"""

from __future__ import annotations

import logging
import signal
import sys
import threading
import time
from typing import List

from slowcontrol.core.config import ServiceConfig
from slowcontrol.core.mqtt import MqttClient, status_topic
from slowcontrol.controllers.autovalve import AutovalveController
from slowcontrol.controllers.base import Controller
from slowcontrol.controllers.gradient import GradientController
from slowcontrol.controllers.interlocks import InterlocksController
from slowcontrol.controllers.sequencer import SequencerController
from slowcontrol.controllers.trackers import TrackerController
from slowcontrol.drivers.base import SensorDriver
from slowcontrol.drivers.plc import PlcDriver
from slowcontrol.state import SchemaError, default_schema_path, load_state_schema
from slowcontrol.state.store import StateStore

try:
    from labjack_t7 import LabJackT7Controller
    _HAS_LABJACK = True
except ImportError:
    _HAS_LABJACK = False

log = logging.getLogger(__name__)


class SlowControlService:
    def __init__(self, config: ServiceConfig, config_path: str | None = None):
        self._config = config
        self._config_path = config_path
        self._mqtt = MqttClient(
            host=config.mqtt.host,
            port=config.mqtt.port,
            client_id=config.mqtt.client_id,
            keepalive=config.mqtt.keepalive,
        )
        self._drivers: List[SensorDriver] = []
        self._controllers: List[Controller] = []
        self._state_store: StateStore | None = None
        self._stop_event = threading.Event()
        self._start_time = time.monotonic()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def run(self) -> None:
        """Connect everything and block until shutdown signal."""
        signal.signal(signal.SIGTERM, self._handle_signal)
        signal.signal(signal.SIGINT,  self._handle_signal)

        log.info("xsphere slow control service starting")

        self._mqtt.connect()

        # ── Drivers ───────────────────────────────────────────────────
        self._drivers = [
            PlcDriver(self._config, self._mqtt),
            # The level-sensor / GHS ESP32s and the LabJack T7 publish their
            # own MQTT topics; they are *not* drivers in this list.  Add a new
            # driver here only for a polled, in-process device.
        ]
        for driver in self._drivers:
            driver.start()
        log.info("Drivers started")

        # ── State store (proprioception layer) ────────────────────────
        # Loads state.yaml (next to the config file), subscribes to every
        # source topic, and republishes the consolidated snapshot on
        # xsphere/state/snapshot.  Optional — if the schema is missing or
        # malformed the rest of the service still runs.
        schema_path = default_schema_path(self._config_path)
        try:
            schema = load_state_schema(schema_path)
        except SchemaError as exc:
            log.warning("State layer disabled — %s", exc)
        else:
            self._state_store = StateStore(self._config, self._mqtt, schema)
            self._state_store.start()
            log.info("State store started (%s)", schema_path)

        # ── Controllers ───────────────────────────────────────────────
        # Gradient must start before interlocks so its retained status
        # topic exists before the watchdog reads setpoints.
        self._controllers = [
            GradientController(self._config, self._mqtt),
            AutovalveController(self._config, self._mqtt),
            InterlocksController(self._config, self._mqtt),
            TrackerController(self._config, self._mqtt),
            SequencerController(self._config, self._mqtt),
        ]
        if _HAS_LABJACK:
            self._controllers.append(LabJackT7Controller(self._config, self._mqtt))
            log.info("LabJack T7 controller registered")
        else:
            log.info("labjack_t7 package not found — LabJack T7 controller skipped")
        for ctrl in self._controllers:
            ctrl.start()
        log.info("Controllers started")

        log.info("All components running — entering heartbeat loop")
        self._heartbeat_loop()

        # ── Shutdown ─────────────────────────────────────────────────
        log.info("Shutting down controllers")
        for ctrl in reversed(self._controllers):
            ctrl.stop()

        if self._state_store is not None:
            log.info("Shutting down state store")
            self._state_store.stop()

        log.info("Shutting down drivers")
        for driver in reversed(self._drivers):
            driver.stop()

        self._mqtt.disconnect()
        log.info("Service stopped")

    def _heartbeat_loop(self) -> None:
        interval = self._config.heartbeat_interval
        watchdog_timeout = self._config.watchdog_timeout_s
        while not self._stop_event.is_set():
            uptime = int(time.monotonic() - self._start_time)
            self._mqtt.publish_status(
                "service", "heartbeat",
                payload={"uptime_s": uptime},
                retain=True,
            )
            # Self-watchdog: if no publish has returned MQTT_ERR_SUCCESS in
            # watchdog_timeout_s, paho is stuck (typically queue-full rc=15
            # after a network blip — observed post power outage 2026-06-07).
            # Exit non-zero so systemd's Restart=on-failure brings us back
            # with a fresh paho client. The exit is intentional and safe:
            # the LJ→PLC mirror in PlcDriver has the safe-surrogate interlock,
            # so even with the service down or restarting the PID won't run
            # away on stale data.
            since_ok = self._mqtt.seconds_since_publish_ok()
            if watchdog_timeout > 0 and since_ok > watchdog_timeout:
                log.error("MQTT watchdog tripped: no successful publish in "
                          "%.0fs (timeout %.0fs). Exiting so systemd can "
                          "restart with a fresh client.",
                          since_ok, watchdog_timeout)
                sys.exit(1)
            self._stop_event.wait(interval)

    def _handle_signal(self, signum, frame) -> None:
        log.info("Received signal %d — stopping", signum)
        self._stop_event.set()
