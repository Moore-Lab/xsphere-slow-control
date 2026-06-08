"""
Configuration loader.

Reads config.yaml and exposes typed dataclasses to the rest of the service.
All network addresses, poll intervals, thresholds, and PID defaults live here
so that no magic numbers are scattered through driver/controller code.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Dict, Optional

import yaml


# ---------------------------------------------------------------------------
# Sub-configs
# ---------------------------------------------------------------------------

@dataclass
class MqttConfig:
    host: str = "localhost"
    port: int = 1883
    client_id: str = "xsphere-slowcontrol"
    keepalive: int = 60


@dataclass
class InfluxConfig:
    """Only used if the service writes directly to InfluxDB (e.g. derived
    quantities not on any MQTT topic). Primary ingestion goes through
    Telegraf, so this is optional."""
    url: str = "http://localhost:8086"
    token: str = ""
    org: str = "xsphere"
    bucket: str = "xsphere"
    enabled: bool = False


@dataclass
class PlcConfig:
    host: str = "192.168.8.1"       # update to actual PLC IP (DHCP from router)
    port: int = 502                  # Modbus TCP default
    unit_id: int = 1
    poll_interval: float = 1.0      # seconds between register reads
    timeout: float = 3.0            # Modbus connection timeout
    # Safety interlocks on the LabJack → PLC PV mirror (see
    # PlcDriver._write_labjack_to_plc). The PID's process variable is read
    # from DF210-212, written by this driver from cached MQTT data. Either
    # interlock substitutes a "safe high" temperature into those DFs, which
    # drives the PID output to 0 without us needing to touch the loop.
    pv_stale_s:           float = 10.0     # no LJ MQTT update in this many s ⇒ trip
    pv_over_temp_k:       float = 303.15   # measured PV ≥ this ⇒ trip (30 °C; raise when baking)
    pv_safe_surrogate_k:  float = 500.0    # written to pv_raw when tripped (must exceed any plausible setpoint)
    sp_safe_surrogate_k:  float = 3.0      # written to SP when tripped (must be below any plausible PV — error stays large negative ⇒ output → 0 regardless of where the PID actually sources its PV)


@dataclass
class VesselAutofillConfig:
    level_high: float = 2.5         # close valve above this (0-10 scale)
    level_low: float = 0.5          # open valve below this (0-10 scale)
    fill_timeout_s: int = 600       # safety timeout for fill cycle


@dataclass
class AutovalveConfig:
    enabled: bool = True
    vessels: Dict[str, VesselAutofillConfig] = field(default_factory=lambda: {
        "cryostat":   VesselAutofillConfig(level_high=2.5, level_low=0.25,
                                           fill_timeout_s=920),
        "primary_xe": VesselAutofillConfig(level_high=2.5, level_low=0.5,
                                           fill_timeout_s=600),
        "ballast":    VesselAutofillConfig(level_high=2.5, level_low=0.5,
                                           fill_timeout_s=600),
    })


@dataclass
class GradientConfig:
    """Maps PID zone names to their preferred and fallback RTD sources."""
    enabled: bool = True
    # Preferred RTD label → PLC register name (see plc.py REGISTER map)
    zone_preferred: Dict[str, str] = field(default_factory=lambda: {
        "top":    "rtd_cube_top",
        "bottom": "rtd_cube_bottom",
        "nozzle": "rtd_cube_nozzle",
    })
    zone_fallback: Dict[str, str] = field(default_factory=lambda: {
        "top":    "rtd_clamp_top",
        "bottom": "rtd_clamp_bottom",
        "nozzle": "rtd_cube_nozzle",
    })


@dataclass
class ServiceConfig:
    mqtt: MqttConfig = field(default_factory=MqttConfig)
    influx: InfluxConfig = field(default_factory=InfluxConfig)
    plc: PlcConfig = field(default_factory=PlcConfig)
    autovalve: AutovalveConfig = field(default_factory=AutovalveConfig)
    gradient: GradientConfig = field(default_factory=GradientConfig)
    # Raw `labjack:` block from the YAML, passed through to the LabJack T7
    # controller (see LJ-python-controller / LabjackT7Config.from_dict).
    # None ⇒ the controller uses its built-in defaults (no channels).
    labjack: Optional[dict] = None
    heartbeat_interval: float = 10.0    # seconds between heartbeat publishes
    # Self-watchdog: if the MQTT client hasn't successfully published in this
    # many seconds, the service exits non-zero so systemd restarts it with a
    # fresh paho client. 0 disables. Failure mode this guards against: paho
    # stuck post-network-blip, publishes returning rc=15 (queue full).
    watchdog_timeout_s: float = 60.0
    log_level: str = "INFO"


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------

def load(path: str = "config.yaml") -> ServiceConfig:
    """Load configuration from YAML file, falling back to defaults."""
    if not os.path.exists(path):
        return ServiceConfig()

    with open(path) as fh:
        raw = yaml.safe_load(fh) or {}

    cfg = ServiceConfig()

    if "mqtt" in raw:
        m = raw["mqtt"]
        cfg.mqtt = MqttConfig(
            host=m.get("host", cfg.mqtt.host),
            port=m.get("port", cfg.mqtt.port),
            client_id=m.get("client_id", cfg.mqtt.client_id),
            keepalive=m.get("keepalive", cfg.mqtt.keepalive),
        )

    if "influx" in raw:
        i = raw["influx"]
        cfg.influx = InfluxConfig(
            url=i.get("url", cfg.influx.url),
            token=i.get("token", cfg.influx.token),
            org=i.get("org", cfg.influx.org),
            bucket=i.get("bucket", cfg.influx.bucket),
            enabled=i.get("enabled", cfg.influx.enabled),
        )

    if "plc" in raw:
        p = raw["plc"]
        cfg.plc = PlcConfig(
            host=p.get("host", cfg.plc.host),
            port=p.get("port", cfg.plc.port),
            unit_id=p.get("unit_id", cfg.plc.unit_id),
            poll_interval=p.get("poll_interval", cfg.plc.poll_interval),
            timeout=p.get("timeout", cfg.plc.timeout),
            pv_stale_s=float(p.get("pv_stale_s", cfg.plc.pv_stale_s)),
            pv_over_temp_k=float(p.get("pv_over_temp_k", cfg.plc.pv_over_temp_k)),
            pv_safe_surrogate_k=float(p.get("pv_safe_surrogate_k", cfg.plc.pv_safe_surrogate_k)),
            sp_safe_surrogate_k=float(p.get("sp_safe_surrogate_k", cfg.plc.sp_safe_surrogate_k)),
        )

    if "autovalve" in raw:
        av = raw["autovalve"]
        vessels = {}
        for name, vc in av.get("vessels", {}).items():
            vessels[name] = VesselAutofillConfig(
                level_high=vc.get("level_high", 2.5),
                level_low=vc.get("level_low", 0.5),
                fill_timeout_s=vc.get("fill_timeout_s", 600),
            )
        cfg.autovalve = AutovalveConfig(
            enabled=av.get("enabled", True),
            vessels=vessels or cfg.autovalve.vessels,
        )

    if "labjack" in raw:
        cfg.labjack = raw["labjack"]

    cfg.heartbeat_interval = raw.get("heartbeat_interval", cfg.heartbeat_interval)
    cfg.watchdog_timeout_s = float(raw.get("watchdog_timeout_s", cfg.watchdog_timeout_s))
    cfg.log_level = raw.get("log_level", cfg.log_level)

    return cfg
