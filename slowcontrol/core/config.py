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
    # Verified protocol (RDXL6SD-USB Modbus manual + working ref logger):
    # 57600 baud, 8N2, input registers FC=4, 6 × Int32 at fixed addresses
    # (see OmegaDriver). Raw / 10 = temperature in the meter's currently-
    # configured unit (°C or °F), read live from 0x106E.
    enabled: bool = False
    port: str = "/dev/ttyUSB0"
    baud_rate: int = 57600
    stop_bits: int = 2
    modbus_address: int = 1
    poll_interval_s: float = 5.0
    timeout_s: float = 2.0
    # Each entry: {ch: 1-6, kind: "tc" | "rtd", label: str}.
    # Operator-friendly subpath numbering (tc/1..4, rtd/1..2) is assigned in
    # order encountered here, so keep entries sorted by ch.
    channels: list = field(default_factory=list)


@dataclass
class NgeConfig:
    """R&S NGE100-series bench supply feeding the three heaters (NGE channel
    n → heater n; the PLC still PWMs each heater through its SSR).  USB-TMC,
    plugged into the Pi.  See slowcontrol/drivers/nge.py."""
    enabled: bool = False
    # The kernel usbtmc device node (the udev rule in
    # slowcontrol/99-xsphere-nge.rules names it /dev/nge100), or "sim" for the
    # built-in simulator.
    resource: str = "/dev/nge100"
    # If set, refuse to drive an NGE with any other serial number.
    serial: str = ""
    timeout_s: float = 2.0
    poll_interval_s: float = 1.0
    # Initial hold setting; the operator's later choice persists to
    # slowcontrol/nge_state.json together with the held outputs / setpoints.
    hold: bool = True
    # Take away the supply's own reasons to switch a channel off in normal
    # heater operation: over-power protection off, electronic fuse off, OVP
    # left armed at its maximum.
    disable_auto_off: bool = True
    # A held state older than this is not re-imposed when the service next
    # reaches the supply; the supply's present state is adopted instead.
    resume_max_age_s: float = 86400.0
    # What the Control page's "defaults" button sends.
    default_voltage_v: float = 24.0
    default_current_a: float = 1.5
    # Ceilings on what a command may ask for.
    max_voltage_v: float = 32.0
    max_current_a: float = 3.0


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
    nge: NgeConfig = field(default_factory=NgeConfig)
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
    # Per-driver poll-thread watchdog. A driver whose thread is alive but
    # hasn't completed a poll() in this many seconds is treated as stuck;
    # the service exits so systemd restarts every thread cleanly. 0 disables.
    # Failure mode: blocked Modbus call post-power-outage where heartbeats
    # from other threads kept the MQTT watchdog satisfied (observed 2026-06-23).
    driver_poll_watchdog_s: float = 60.0
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
            stop_bits=int(o.get("stop_bits", cfg.omega.stop_bits)),
            modbus_address=int(o.get("modbus_address", cfg.omega.modbus_address)),
            poll_interval_s=float(o.get("poll_interval_s", cfg.omega.poll_interval_s)),
            timeout_s=float(o.get("timeout_s", cfg.omega.timeout_s)),
            channels=list(o.get("channels", []) or []),
        )

    if "nge" in raw:
        n = raw["nge"] or {}
        cfg.nge = NgeConfig(
            enabled=bool(n.get("enabled", cfg.nge.enabled)),
            resource=str(n.get("resource", cfg.nge.resource)),
            serial=str(n.get("serial", cfg.nge.serial) or ""),
            timeout_s=float(n.get("timeout_s", cfg.nge.timeout_s)),
            poll_interval_s=float(n.get("poll_interval_s", cfg.nge.poll_interval_s)),
            hold=bool(n.get("hold", cfg.nge.hold)),
            disable_auto_off=bool(n.get("disable_auto_off", cfg.nge.disable_auto_off)),
            resume_max_age_s=float(n.get("resume_max_age_s", cfg.nge.resume_max_age_s)),
            default_voltage_v=float(n.get("default_voltage_v", cfg.nge.default_voltage_v)),
            default_current_a=float(n.get("default_current_a", cfg.nge.default_current_a)),
            max_voltage_v=float(n.get("max_voltage_v", cfg.nge.max_voltage_v)),
            max_current_a=float(n.get("max_current_a", cfg.nge.max_current_a)),
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
    cfg.driver_poll_watchdog_s = float(raw.get("driver_poll_watchdog_s", cfg.driver_poll_watchdog_s))
    cfg.log_level = raw.get("log_level", cfg.log_level)

    return cfg
