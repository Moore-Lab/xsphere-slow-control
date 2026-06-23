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
        # Monotonic timestamp of the last poll() call that returned without
        # raising. The service heartbeat loop uses this as a poll-thread
        # liveness signal: if the thread is alive but this stamp is too old,
        # the poll is stuck (typically on a half-open Modbus socket post-
        # power-outage — observed 2026-06-23) and we exit so systemd can
        # restart with a fresh thread. Seeded at start() so a freshly
        # spawned driver doesn't immediately look stuck.
        self._last_poll_ok_ts: float = time.monotonic()

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
        self._last_poll_ok_ts = time.monotonic()
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
                self._last_poll_ok_ts = t0
            except Exception:
                log.exception("[%s] error during poll", self.NAME)
            elapsed = time.monotonic() - t0
            wait = max(0.0, self.poll_interval - elapsed)
            self._stop_event.wait(wait)

    # ------------------------------------------------------------------
    # Watchdog hooks (read by the service heartbeat loop)
    # ------------------------------------------------------------------

    @property
    def is_polling(self) -> bool:
        """True iff the poll thread is alive (i.e. supposed to be working).
        A driver whose start() failed and whose thread never began isn't
        eligible for the poll-watchdog — that's a different failure mode
        (no recovery available without operator action)."""
        return self._thread is not None and self._thread.is_alive()

    def seconds_since_poll_ok(self) -> float:
        """How long since poll() last returned without raising. Combined
        with `is_polling`, this is the per-driver liveness signal the
        service uses to detect a stuck poll thread."""
        return time.monotonic() - self._last_poll_ok_ts
