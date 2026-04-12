"""
Omega RDXL6SD-USB temperature logger.

Reads all 6 thermocouple/RTD channels from the Omega RDXL6SD-USB data
logger via Modbus RTU over USB-serial, then publishes each channel to
MQTT using the xsphere topic schema.

Channel map (configurable in config section below):
  ch1 — TC-1  (K-type, typically cryostat top TC)
  ch2 — TC-2  (K-type, typically cryostat bottom TC)
  ch3 — TC-3  (K-type, typically nozzle TC)
  ch4 — TC-4  (K-type, spare / reference)
  ch5 — RTD-1 (PT100, clamp RTD — ballast dewar)
  ch6 — RTD-2 (PT100, clamp RTD — cryostat exterior)

MQTT topics published:
  xsphere/sensors/temperature/omega/ch{1-6}
  payload: {"value_k": <float>, "value_c": <float>, "channel": <int>}

HARDWARE NOTES (VERIFY BEFORE USE):
  ─────────────────────────────────
  The RDXL6SD-USB uses Modbus RTU at 9600 baud, 8N1 by default.
  Default device address is 1.

  Register map (VERIFY against RDXL6SD documentation / actual device):
    Each channel temperature is stored as a signed 16-bit integer
    in units of 0.1 °C (tenths of degrees Celsius).
    Registers start at holding register 0x0000:
      0x0000 → Channel 1
      0x0001 → Channel 2
      0x0002 → Channel 3
      0x0003 → Channel 4
      0x0004 → Channel 5
      0x0005 → Channel 6

  OPEN-CIRCUIT / FAULT sentinel values:
    The RDXL6SD returns a specific out-of-range value when a sensor
    is disconnected. Common values are 32767 (0x7FFF) or -32768 (0x8000).
    This code treats |value| > 5000 (i.e., > 500 °C) as a fault.

Usage:
  python omega_logger.py [-c config.yaml] [-v]

  Or as a systemd service:
  see xsphere-omega-logger.service
"""

from __future__ import annotations

import argparse
import json
import logging
import signal
import sys
import time
from pathlib import Path
from typing import Optional

import paho.mqtt.client as mqtt
from pymodbus.client import ModbusSerialClient
from pymodbus.exceptions import ModbusException

# ──────────────────────────────────────────────────────────────────────
# Configuration (override via config.yaml or command-line)
# ──────────────────────────────────────────────────────────────────────

DEFAULTS = {
    "serial_port":    "/dev/ttyUSB0",   # VERIFY: check `ls /dev/ttyUSB*`
    "baud_rate":      9600,
    "modbus_address": 1,                # VERIFY: matches device DIP switches
    "poll_interval":  5.0,              # seconds between reads
    "mqtt_host":      "192.168.8.116",  # xbox-pi broker
    "mqtt_port":      1883,
    "mqtt_client_id": "xsphere-omega-logger",
    "topic_prefix":   "xsphere/sensors/temperature/omega",
    "num_channels":   6,
    "reg_base":       0x0000,           # VERIFY: first holding register
    "fault_threshold": 5000,            # raw value above which = fault (|°C| > 500)
}

# Channel labels for logging (no effect on MQTT topics)
CHANNEL_LABELS = {
    1: "TC-1",
    2: "TC-2",
    3: "TC-3",
    4: "TC-4",
    5: "RTD-1",
    6: "RTD-2",
}

CELSIUS_TO_KELVIN = 273.15

# ──────────────────────────────────────────────────────────────────────

log = logging.getLogger(__name__)

_stop = False


def handle_signal(signum, frame):
    global _stop
    log.info("Signal %d received — stopping", signum)
    _stop = True


def load_config(path: Optional[str]) -> dict:
    cfg = dict(DEFAULTS)
    if path is None:
        return cfg
    try:
        import yaml
        with open(path) as f:
            data = yaml.safe_load(f)
        if data and "omega" in data:
            cfg.update(data["omega"])
        log.debug("Loaded config from %s", path)
    except FileNotFoundError:
        log.warning("Config file not found: %s — using defaults", path)
    except Exception as exc:
        log.warning("Could not load config %s: %s — using defaults", path, exc)
    return cfg


def connect_mqtt(cfg: dict) -> mqtt.Client:
    client = mqtt.Client(
        client_id=cfg["mqtt_client_id"],
        protocol=mqtt.MQTTv5,
    )
    client.connect(cfg["mqtt_host"], cfg["mqtt_port"], keepalive=60)
    client.loop_start()
    log.info("MQTT connected to %s:%d", cfg["mqtt_host"], cfg["mqtt_port"])
    return client


def connect_modbus(cfg: dict) -> ModbusSerialClient:
    client = ModbusSerialClient(
        port=cfg["serial_port"],
        baudrate=cfg["baud_rate"],
        bytesize=8,
        parity="N",
        stopbits=1,
        timeout=2,
    )
    if not client.connect():
        raise ConnectionError(
            f"Cannot open serial port {cfg['serial_port']}"
        )
    log.info("Modbus RTU connected on %s @ %d baud", cfg["serial_port"], cfg["baud_rate"])
    return client


def read_channels(
    modbus: ModbusSerialClient,
    cfg: dict,
) -> dict[int, Optional[float]]:
    """Read all channels; returns {channel_number: value_c or None on fault}."""
    results: dict[int, Optional[float]] = {}
    n = cfg["num_channels"]
    addr = cfg["modbus_address"]
    reg_base = cfg["reg_base"]
    fault_thresh = cfg["fault_threshold"]

    try:
        resp = modbus.read_holding_registers(
            address=reg_base,
            count=n,
            slave=addr,
        )
    except ModbusException as exc:
        log.error("Modbus read error: %s", exc)
        return {ch: None for ch in range(1, n + 1)}

    if resp.isError():
        log.error("Modbus error response: %s", resp)
        return {ch: None for ch in range(1, n + 1)}

    for i, reg in enumerate(resp.registers):
        ch = i + 1
        # Convert unsigned register to signed int16
        raw = reg if reg < 0x8000 else reg - 0x10000
        if abs(raw) > fault_thresh:
            log.warning("Channel %d (%s): fault/open-circuit (raw=%d)",
                        ch, CHANNEL_LABELS.get(ch, "?"), raw)
            results[ch] = None
        else:
            results[ch] = raw / 10.0   # 0.1 °C resolution

    return results


def publish_channels(
    mqttc: mqtt.Client,
    cfg: dict,
    readings: dict[int, Optional[float]],
) -> None:
    prefix = cfg["topic_prefix"]
    for ch, value_c in readings.items():
        topic = f"{prefix}/ch{ch}"
        if value_c is None:
            payload = json.dumps({
                "channel": ch,
                "label": CHANNEL_LABELS.get(ch, ""),
                "fault": True,
            })
        else:
            value_k = value_c + CELSIUS_TO_KELVIN
            payload = json.dumps({
                "channel": ch,
                "label":   CHANNEL_LABELS.get(ch, ""),
                "value_c": round(value_c, 2),
                "value_k": round(value_k, 2),
                "fault":   False,
            })
        mqttc.publish(topic, payload, qos=0, retain=False)


def run(cfg: dict) -> None:
    global _stop
    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    mqttc = connect_mqtt(cfg)

    modbus: Optional[ModbusSerialClient] = None
    reconnect_delay = 5.0

    while not _stop:
        # (Re)connect Modbus if needed
        if modbus is None:
            try:
                modbus = connect_modbus(cfg)
            except ConnectionError as exc:
                log.error("%s — retrying in %.0f s", exc, reconnect_delay)
                time.sleep(reconnect_delay)
                continue

        try:
            readings = read_channels(modbus, cfg)
            publish_channels(mqttc, cfg, readings)
            log.debug("Published %d channels", len(readings))
        except Exception as exc:
            log.error("Read cycle error: %s — reconnecting", exc)
            try:
                modbus.close()
            except Exception:
                pass
            modbus = None

        # Sleep in small increments so SIGINT is responsive
        target = time.monotonic() + cfg["poll_interval"]
        while not _stop and time.monotonic() < target:
            time.sleep(0.2)

    log.info("Stopping")
    if modbus:
        modbus.close()
    mqttc.loop_stop()
    mqttc.disconnect()


def main() -> None:
    parser = argparse.ArgumentParser(description="Omega RDXL6SD-USB MQTT logger")
    parser.add_argument("-c", "--config", default=None,
                        help="Path to config YAML (optional)")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="Debug logging")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )

    cfg = load_config(args.config)
    log.info("Omega logger starting — port=%s addr=%d interval=%.0f s",
             cfg["serial_port"], cfg["modbus_address"], cfg["poll_interval"])
    run(cfg)


if __name__ == "__main__":
    main()
