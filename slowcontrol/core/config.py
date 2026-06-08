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
    # PlcDriver._write_labjack_to_plc).
    #
    # Defaults below are the *initial* values seeded into PlcDriver on first
    # boot. The driver persists the user's runtime adjustments to
    # slowcontrol/pv_interlock.json — that file takes precedence over these
    # defaults on subsequent boots, so a bakeout limit set via the web GUI
    # survives a restart.
    pv_stale_s:           float = 10.0     # no LJ MQTT update in this many s ⇒ trip
    # Trip if any LJ RTD reads outside the band [pv_min_k, pv_max_k]. Catches
    # both over-temp (PID windup, runaway heat) AND sensor faults that show
    # up as wild values (e.g. broken RTD reading -100000 K). Defaults give
    # room from LN₂ (77 K) up to a hair above room temp (310 K ≈ 37 °C).
    # Raise pv_max_k via the GUI when intentionally baking.
    pv_min_k:             float = 77.0
    pv_max_k:             float = 310.0
    pv_safe_surrogate_k:  float = 500.0    # written to pv_raw when tripped (must exceed any plausible setpoint)
    sp_safe_surrogate_k:  float = 3.0      # written to SP when tripped (must be below any plausible PV — error stays large negative ⇒ output → 0 regardless of where the PID actually sources its PV)


@dataclass
class OmegaConfig:
    """Omega RDXL6SD-USB temperature logger (Modbus RTU over USB-serial).

    The device has 4 type-K thermocouple inputs + 2 PT100 RTD inputs in
    the lab's wiring (device channels 1-4 = TC, 5-6 = RTD by default, but
    the `channels` list below is authoritative — only listed channels are
    published, so you can omit ones that aren't wired yet)."""
    enabled: bool = False
    port: str = "/dev/ttyUSB0"
    baud_rate: int = 9600
    modbus_address: int = 1
    poll_interval_s: float = 5.0
    timeout_s: float = 2.0
    reg_base: int = 0x0000
    # Each entry: {ch: 1-6, kind: "tc" | "rtd", label: str}.
    # Operator-friendly subpath numbering (tc/1..4, rtd/1..2) is assigned in
    # order encountered here, so keep entries sorted by ch.
    channels: list = field(default_factory=list)


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
    omega: OmegaConfig = field(default_factory=OmegaConfig)
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
            pv_min_k=float(p.get("pv_min_k", cfg.plc.pv_min_k)),
            pv_max_k=float(p.get("pv_max_k", cfg.plc.pv_max_k)),
            pv_safe_surrogate_k=float(p.get("pv_safe_surrogate_k", cfg.plc.pv_safe_surrogate_k)),
            sp_safe_surrogate_k=float(p.get("sp_safe_surrogate_k", cfg.plc.sp_safe_surrogate_k)),
        )

    if "omega" in raw:
        o = raw["omega"]
        cfg.omega = OmegaConfig(
            enabled=bool(o.get("enabled", cfg.omega.enabled)),
            port=str(o.get("port", cfg.omega.port)),
            baud_rate=int(o.get("baud_rate", cfg.omega.baud_rate)),
            modbus_address=int(o.get("modbus_address", cfg.omega.modbus_address)),
            poll_interval_s=float(o.get("poll_interval_s", cfg.omega.poll_interval_s)),
            timeout_s=float(o.get("timeout_s", cfg.omega.timeout_s)),
            reg_base=int(o.get("reg_base", cfg.omega.reg_base)),
            channels=list(o.get("channels", []) or []),
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
