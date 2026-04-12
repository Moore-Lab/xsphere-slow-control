"""
Abstract base class for controllers.

Controllers implement higher-level logic on top of raw sensor data:
  - Gradient abstraction (compute PV source from ΔT target)
  - Autovalve state machine
  - Interlocks / safety watchdog
  - Plugin experiment modules (gradient scanner, etc.)

Each controller subscribes to relevant MQTT topics and publishes
commands back through the MQTT bus.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from slowcontrol.core.config import ServiceConfig
    from slowcontrol.core.mqtt import MqttClient

log = logging.getLogger(__name__)


class Controller(ABC):
    """Base class for all controllers."""

    NAME: str = "unnamed_controller"

    def __init__(self, config: "ServiceConfig", mqtt: "MqttClient"):
        self._config = config
        self._mqtt = mqtt

    @abstractmethod
    def start(self) -> None:
        """Subscribe to topics and begin control logic."""
        ...

    @abstractmethod
    def stop(self) -> None:
        """Unsubscribe and clean up."""
        ...
