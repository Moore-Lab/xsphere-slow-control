"""
Main service orchestrator.

Starts all drivers and controllers in dependency order, runs the heartbeat
loop, and handles clean shutdown on SIGTERM / SIGINT.

Startup order
─────────────
  1. MQTT connect
  2. Drivers start (PlcDriver polls Modbus registers and publishes sensor data)
  3. Controllers start (subscribe to MQTT, no polling)
     a. GradientController   — setpoint coupling logic
     b. AutovalveController  — autofill state machines
     c. InterlocksController — safety watchdog
  4. Plugins start (optional experiment automation)
     a. GradientScannerPlugin
  5. Heartbeat loop (blocks until SIGTERM / SIGINT)
  6. Shutdown in reverse order
"""

from __future__ import annotations

import logging
import signal
import threading
import time
from typing import List

from slowcontrol.core.config import ServiceConfig
from slowcontrol.core.mqtt import MqttClient, status_topic
from slowcontrol.controllers.autovalve import AutovalveController
from slowcontrol.controllers.base import Controller
from slowcontrol.controllers.gradient import GradientController
from slowcontrol.controllers.interlocks import InterlocksController
from slowcontrol.drivers.base import SensorDriver
from slowcontrol.drivers.plc import PlcDriver
from slowcontrol.plugins.gradient_scanner import GradientScannerPlugin

try:
    from labjack_t7 import LabJackT7Controller
    _HAS_LABJACK = True
except ImportError:
    _HAS_LABJACK = False

log = logging.getLogger(__name__)


class SlowControlService:
    def __init__(self, config: ServiceConfig):
        self._config = config
        self._mqtt = MqttClient(
            host=config.mqtt.host,
            port=config.mqtt.port,
            client_id=config.mqtt.client_id,
            keepalive=config.mqtt.keepalive,
        )
        self._drivers: List[SensorDriver] = []
        self._controllers: List[Controller] = []
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
            # Future: OmegaDriver, LevelSensorDriver if integrated here
        ]
        for driver in self._drivers:
            driver.start()
        log.info("Drivers started")

        # ── Controllers ───────────────────────────────────────────────
        # Gradient must start before interlocks so its retained status
        # topic exists before the watchdog reads setpoints.
        self._controllers = [
            GradientController(self._config, self._mqtt),
            AutovalveController(self._config, self._mqtt),
            InterlocksController(self._config, self._mqtt),
            GradientScannerPlugin(self._config, self._mqtt),
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

        log.info("Shutting down drivers")
        for driver in reversed(self._drivers):
            driver.stop()

        self._mqtt.disconnect()
        log.info("Service stopped")

    def _heartbeat_loop(self) -> None:
        interval = self._config.heartbeat_interval
        while not self._stop_event.is_set():
            uptime = int(time.monotonic() - self._start_time)
            self._mqtt.publish_status(
                "service", "heartbeat",
                payload={"uptime_s": uptime},
                retain=True,
            )
            self._stop_event.wait(interval)

    def _handle_signal(self, signum, frame) -> None:
        log.info("Received signal %d — stopping", signum)
        self._stop_event.set()
