"""
Abstract base class for all hardware drivers.

Each driver runs in its own thread, polling hardware at a configurable
interval and publishing readings to MQTT.
"""

from __future__ import annotations

import logging
import threading
import time
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from slowcontrol.core.config import ServiceConfig
    from slowcontrol.core.mqtt import MqttClient

log = logging.getLogger(__name__)


class SensorDriver(ABC):
    """
    Base class for all sensor/actuator drivers.

    Subclasses implement:
        connect()    — open hardware connection; raise on failure
        disconnect() — clean up hardware connection
        poll()       — read hardware and publish to MQTT; called every
                       poll_interval seconds
    """

    #: Override in subclass to give the driver a human-readable name.
    NAME: str = "unnamed_driver"

    def __init__(self, config: "ServiceConfig", mqtt: "MqttClient"):
        self._config = config
        self._mqtt = mqtt
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._connected = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Connect and start the polling thread."""
        try:
            self.connect()
            self._connected = True
            log.info("[%s] connected", self.NAME)
        except Exception:
            log.exception("[%s] failed to connect — driver disabled", self.NAME)
            return

        self._stop_event.clear()
        self._thread = threading.Thread(target=self._loop,
                                        name=f"driver-{self.NAME}",
                                        daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Signal the polling thread to stop and clean up."""
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=10)
        if self._connected:
            try:
                self.disconnect()
            except Exception:
                log.exception("[%s] error during disconnect", self.NAME)
            self._connected = False
        log.info("[%s] stopped", self.NAME)

    @property
    def poll_interval(self) -> float:
        """Override in subclass or pull from config."""
        return 1.0

    # ------------------------------------------------------------------
    # Abstract interface
    # ------------------------------------------------------------------

    @abstractmethod
    def connect(self) -> None: ...

    @abstractmethod
    def disconnect(self) -> None: ...

    @abstractmethod
    def poll(self) -> None: ...

    # ------------------------------------------------------------------
    # Internal loop
    # ------------------------------------------------------------------

    def _loop(self) -> None:
        while not self._stop_event.is_set():
            t0 = time.monotonic()
            try:
                self.poll()
            except Exception:
                log.exception("[%s] error during poll", self.NAME)
            elapsed = time.monotonic() - t0
            wait = max(0.0, self.poll_interval - elapsed)
            self._stop_event.wait(wait)
