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
    # Not currently passed to pymodbus: CLICK PLUS documents a station address
    # only for its serial ports and ignores the unit identifier on Modbus TCP.
    # Kept for a future serial/RS-485 path.
    unit_id: int = 1
    poll_interval: float = 1.0      # seconds between register reads
    timeout: float = 3.0            # Modbus connection timeout
    # CLICK stores 32-bit floats low-word-first. Verify once on the bench:
    # write 1.0 into DF1 and read registers 28672/28673 — [0x0000, 0x3F80]
    # means low_first, [0x3F80, 0x0000] means high_first.
    float_word_order: str = "low_first"   # low_first | high_first


@dataclass
class OmegaConfig:
    port: str = "/dev/ttyUSB0"
    baudrate: int = 57600
    unit_id: int = 1
    poll_interval: float = 1.0


@dataclass
class CoastConfig:
    """Thermal-coast gate on the OPEN decision for one vessel.

    Normal autofill refills as soon as the level falls through `level_low`.
    Coast instead lets the vessel run dry and keeps running on the thermal
    mass of its cold block, refilling only once that reserve is spent.  The
    reserve is judged from two temperature sensors converging: while there is
    liquid the cold sink sits far below the heated load, and as the block dries
    the two readings close on each other.

    A refill is permitted only when BOTH terms hold:
        level < empty_threshold            (the vessel is actually empty)
        (T_warm - T_cold) < delta_max_k    (the cold reserve is spent)

    Coast gates `auto_open` ONLY.  It can never inhibit `auto_close`, the fill
    timeout, or a manual valve command, and every condition it cannot evaluate
    releases the inhibit rather than extending it — the failure direction is
    always "spend LN2", never "strand the vessel empty".
    """
    # --- Arming -------------------------------------------------------
    enabled: bool = False               # allow coast to be armed at runtime
    ladder_rung_entered: bool = False   # declare CLICK ladder addition 6 done

    # --- Operator-tunable gate (also settable over MQTT) --------------
    empty_threshold: float = 0.13       # refill permitted below this level
    sensor_warm: str = "plc/rtd/2"      # Ti — the heated load
    sensor_cold: str = "plc/rtd/4"      # Tj — the cold sink
    delta_max_k: float = 40.0           # X in (Ti - Tj) < X
    delta_mode: str = "signed"          # signed | absolute
    delta_hysteresis_k: float = 5.0     # re-inhibit above delta_max_k + this
    confirm_s: float = 60.0             # gate must hold this long to open

    # --- Backstops (config file ONLY — never settable over MQTT) ------
    base_k_nominal: float = 165.0       # operating base temp, for validation
    max_duration_s: float = 5400.0      # unconditional end of a coast episode
    ceiling_k: float = 175.0            # any cube RTD above this ends coast
    sink_warm_k: float = 135.0          # cold sensor above this means dry
    sink_cold_k: float = 90.0           # cold sensor below this proves liquid
    temp_stale_s: float = 30.0          # temperature freshness requirement
    level_stale_s: float = 60.0         # level freshness requirement
    temp_min_k: float = 50.0            # plausibility band, both channels
    temp_max_k: float = 400.0


@dataclass
class VesselAutofillConfig:
    level_high: float = 2.5         # close valve above this (0-10 scale)
    level_low: float = 0.5          # open valve below this (0-10 scale)
    fill_timeout_s: int = 600       # safety timeout for fill cycle
    coast: Optional[CoastConfig] = None   # None = coast never applies here


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
class MksConfig:
    """MKS M330B mass flow controller on the PLC analog I/O.

    The MFC is calibrated at the factory for a specific gas and full scale.
    Its analog interface is ratiometric: flow output and setpoint input both
    span 0 V (zero flow) to `signal_span_v` (100% of full scale).

    `gas_correction_factor` converts an N2-equivalent reading into the real
    gas.  Leave it at 1.0 if the unit was calibrated directly for xenon —
    check the calibration sticker on the flowbody.
    """
    enabled: bool = True
    full_scale_sccm: float = 1000.0        # device full scale (calibration gas)
    gas_name: str = "N2"                   # gas the unit is calibrated for
    gas_correction_factor: float = 1.0     # actual-gas sccm = N2 sccm × GCF
    signal_span_v: float = 5.0             # 0–5 V standard MKS analog interface
    setpoint_max_pct: float = 100.0        # refuse setpoints above this % of FS

    # End-to-end analog calibration.  The C2-08D2-6V is specified at ±2% of
    # full scale (±200 mV on its 10 V span) plus ±25 mV offset — on a 5 V
    # signal that is up to ±4% of the MFC's range, four times the MFC's own
    # ±1% accuracy.  The PLC module, not the flow meter, dominates the error
    # budget, so a two-point calibration against a good DMM is worth doing.
    #   corrected_v = raw_v * adc_gain + adc_offset_v      (flow input)
    #   register_v  = wanted_v * dac_gain + dac_offset_v   (setpoint output)
    adc_gain:     float = 1.0
    adc_offset_v: float = 0.0
    dac_gain:     float = 1.0
    dac_offset_v: float = 0.0
    # The valve override mode encoding (0 closed / 1 normal / 2 open) is not
    # configurable: it is fixed by the ladder's decode rungs, so it lives with
    # the register map in drivers/plc.py rather than here.


@dataclass
class GasFlowConfig:
    """MKS flow path + pneumatic bypass path around it.

    The two paths run in parallel between the same tee points, so under
    normal operation exactly one of them is open.
    """
    enabled: bool = True
    exclusive_paths: bool = True           # forbid MKS and bypass open together
    default_path: str = "isolated"         # mks | bypass | isolated
    apply_default_on_start: bool = False   # never actuate valves on service restart
    mks: MksConfig = field(default_factory=MksConfig)


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
    gasflow: GasFlowConfig = field(default_factory=GasFlowConfig)
    heartbeat_interval: float = 10.0    # seconds between heartbeat publishes
    log_level: str = "INFO"


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

# Temperature channels a coast sensor pair may be chosen from.  These are the
# MQTT sub-paths under xsphere/sensors/temperature/, i.e. exactly the values of
# plc.RTD_MQTT_PATH joined with "/" — keep the two in sync.
COAST_SENSOR_CHANNELS = frozenset(
    {f"plc/rtd/{n}" for n in range(1, 5)}
    | {f"omega/ch{n}" for n in range(1, 7)}
)

# A cube RTD above this is past the point where any coast is defensible; the
# warm-up procedure calls 200 K the "xenon fully evaporated" mark.
COAST_CEILING_HARD_CAP_K = 200.0

# Below this the Pt100 (DF1–DF3) / Pt1000 (DF4) systematic offset is a
# comparable size to the gate itself, so the gate stops meaning anything.
COAST_DELTA_MIN_K = 10.0


def _load_coast(raw: Optional[dict]) -> Optional[CoastConfig]:
    """Build a CoastConfig from a raw YAML mapping, or None if absent.

    An unknown key is fatal for the same reason a bad sensor name is: a typo
    in `empty_threshold` would silently leave the default in place while the
    operator reads their own value back off the file.
    """
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError("autovalve.vessels.*.coast must be a mapping")
    fields = CoastConfig.__dataclass_fields__
    unknown = sorted(set(raw) - set(fields))
    if unknown:
        raise ValueError(
            f"unknown autovalve coast keys: {unknown}; expected "
            f"{sorted(fields)}"
        )
    return CoastConfig(**raw)


def validate_vessel(vessel: str, v: VesselAutofillConfig) -> None:
    """Raise ValueError on a vessel autofill/coast config that cannot work.

    Validation is fatal rather than a warning.  The worst outcome for coast is
    a well-formed but wrong configuration: the gate then silently never fires
    (the vessel is stranded empty) or always fires (the feature is a no-op and
    the operator believes LN2 is being saved).  Neither is visible from the
    dashboard, so it has to be caught at load.
    """
    p = f"autovalve.vessels.{vessel}"
    if v.level_low >= v.level_high:
        raise ValueError(f"{p}: level_low must be < level_high")

    c = v.coast
    if c is None:
        return

    for key in ("sensor_warm", "sensor_cold"):
        if getattr(c, key) not in COAST_SENSOR_CHANNELS:
            raise ValueError(
                f"{p}.coast.{key}: {getattr(c, key)!r} is not a known "
                f"temperature channel; expected one of "
                f"{sorted(COAST_SENSOR_CHANNELS)}"
            )
    if c.sensor_warm == c.sensor_cold:
        raise ValueError(
            f"{p}.coast: sensor_warm == sensor_cold, so the difference is "
            "identically zero and the gate would always be satisfied"
        )
    if c.delta_mode not in ("signed", "absolute"):
        raise ValueError(f"{p}.coast.delta_mode must be 'signed' or 'absolute'")
    if c.delta_max_k < COAST_DELTA_MIN_K:
        raise ValueError(
            f"{p}.coast.delta_max_k must be >= {COAST_DELTA_MIN_K} K — below "
            "that you are inside the Pt100/Pt1000 mismatch band"
        )
    if c.delta_hysteresis_k <= 0.0:
        raise ValueError(f"{p}.coast.delta_hysteresis_k must be > 0")
    if not 0.0 < c.empty_threshold <= v.level_low:
        raise ValueError(
            f"{p}.coast.empty_threshold must be in (0, level_low="
            f"{v.level_low}] — coast has to trigger later than plain autofill "
            "or it saves nothing"
        )
    if not 0.0 < c.confirm_s <= c.max_duration_s:
        raise ValueError(f"{p}.coast: need 0 < confirm_s <= max_duration_s")
    if not c.sink_cold_k < c.sink_warm_k:
        raise ValueError(f"{p}.coast: sink_cold_k must be < sink_warm_k")
    if c.sink_warm_k <= c.base_k_nominal - c.delta_max_k:
        raise ValueError(
            f"{p}.coast.sink_warm_k ({c.sink_warm_k} K) would trip before the "
            f"delta gate reaches {c.base_k_nominal - c.delta_max_k} K, making "
            "delta_max_k inoperative"
        )
    if not c.base_k_nominal < c.ceiling_k <= COAST_CEILING_HARD_CAP_K:
        raise ValueError(
            f"{p}.coast.ceiling_k must be in (base_k_nominal="
            f"{c.base_k_nominal}, {COAST_CEILING_HARD_CAP_K}]"
        )
    if not (c.temp_min_k < c.sink_cold_k and c.sink_warm_k < c.temp_max_k):
        raise ValueError(
            f"{p}.coast: sink_cold_k and sink_warm_k must lie inside the "
            f"plausibility band [{c.temp_min_k}, {c.temp_max_k}] K"
        )


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
            float_word_order=p.get("float_word_order",
                                   cfg.plc.float_word_order),
        )

    if "omega" in raw:
        o = raw["omega"]
        cfg.omega = OmegaConfig(
            port=o.get("port", cfg.omega.port),
            baudrate=o.get("baudrate", cfg.omega.baudrate),
            unit_id=o.get("unit_id", cfg.omega.unit_id),
            poll_interval=o.get("poll_interval", cfg.omega.poll_interval),
        )

    if "autovalve" in raw:
        av = raw["autovalve"]
        vessels = {}
        for name, vc in av.get("vessels", {}).items():
            vessels[name] = VesselAutofillConfig(
                level_high=vc.get("level_high", 2.5),
                level_low=vc.get("level_low", 0.5),
                fill_timeout_s=vc.get("fill_timeout_s", 600),
                coast=_load_coast(vc.get("coast")),
            )
            validate_vessel(name, vessels[name])
        cfg.autovalve = AutovalveConfig(
            enabled=av.get("enabled", True),
            vessels=vessels or cfg.autovalve.vessels,
        )

    if "gasflow" in raw:
        gf = raw["gasflow"]
        m = gf.get("mks", {})
        mks = MksConfig(
            enabled=m.get("enabled", cfg.gasflow.mks.enabled),
            full_scale_sccm=m.get("full_scale_sccm",
                                  cfg.gasflow.mks.full_scale_sccm),
            gas_name=m.get("gas_name", cfg.gasflow.mks.gas_name),
            gas_correction_factor=m.get("gas_correction_factor",
                                        cfg.gasflow.mks.gas_correction_factor),
            signal_span_v=m.get("signal_span_v", cfg.gasflow.mks.signal_span_v),
            setpoint_max_pct=m.get("setpoint_max_pct",
                                   cfg.gasflow.mks.setpoint_max_pct),
            adc_gain=m.get("adc_gain", cfg.gasflow.mks.adc_gain),
            adc_offset_v=m.get("adc_offset_v", cfg.gasflow.mks.adc_offset_v),
            dac_gain=m.get("dac_gain", cfg.gasflow.mks.dac_gain),
            dac_offset_v=m.get("dac_offset_v", cfg.gasflow.mks.dac_offset_v),
        )
        cfg.gasflow = GasFlowConfig(
            enabled=gf.get("enabled", cfg.gasflow.enabled),
            exclusive_paths=gf.get("exclusive_paths",
                                   cfg.gasflow.exclusive_paths),
            default_path=gf.get("default_path", cfg.gasflow.default_path),
            apply_default_on_start=gf.get("apply_default_on_start",
                                          cfg.gasflow.apply_default_on_start),
            mks=mks,
        )

    cfg.heartbeat_interval = raw.get("heartbeat_interval", cfg.heartbeat_interval)
    cfg.log_level = raw.get("log_level", cfg.log_level)

    return cfg
