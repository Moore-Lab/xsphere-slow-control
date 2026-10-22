"""
Which hardware each software name stands for, detected from the registry.

The software zone names ``top`` / ``bottom`` / ``nozzle`` were the wrong way
round until 2026-10-15 for top and bottom, and the fix (branch
``claude/swap-top-bottom``) keeps the names and re-points their sources.  So
nothing here trusts a name alone: each zone's sensors are read from
``slowcontrol/state.yaml`` (the ``source`` of ``t_cube_<zone>`` and
``t_<zone>_clamp``), and the physical position is derived from *which* sensor
that is, using the hardware fact below.  The same code therefore tells the
truth before and after the swap deploys.

The zone fingerprint (``cube=<topic>|clamp=<topic>``) is stored with the
human's heater rails; when the registry is swapped under them the fingerprints
no longer match and those rails are treated as unset until re-saved
(see rails.py).

The PV aliases (``rtd<N>``) are the PLC driver's expression identifiers:
``rtd1``–``rtd3`` are LabJack RTD channels 1–3 and ``rtd4``–``rtd6`` the PLC's
own RTD inputs 4–6 (drivers/plc.py), so the alias is the channel number of
the detected topic.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Dict, List, Optional

import yaml

log = logging.getLogger(__name__)

ZONES = ("top", "bottom", "nozzle")
CHANNELS = ("1", "2", "3")                        # NGE103 channels (rails keys)
VESSELS = ("cryostat", "primary_xe", "ballast")   # LN2 fill valves XV3 / XV2 / XV1
VALVES = ("ballast", "pump", "bottle")            # pneumatic gas valves

PNEUMATIC_GATE_TOPIC = "xsphere/status/pneumatic_control"
PNEUMATIC_GATE_STATE = "pneumatic_control_enabled"

#: HARDWARE FACT (Lucas, cryostat test 2026-10-15): where each sensor really
#: sits.  Physically TOP = clamp PLC RTD6 & cube LabJack RTD3; physically
#: BOTTOM = clamp PLC RTD5 & cube LabJack RTD2; the nozzle region = LabJack RTD1.
CUBE_RTD_POSITION = {3: "top", 2: "bottom", 1: "nozzle"}
CLAMP_RTD_POSITION = {6: "top", 5: "bottom"}

_CUBE_TOPIC_RE = re.compile(r"xsphere/sensors/temperature/labjack/rtd/(\d+)")
_CLAMP_TOPIC_RE = re.compile(r"xsphere/sensors/temperature/plc/rtd/(\d+)")
_ZONE_LABEL_RE = re.compile(r"^(Zone\s+\d+\s*\([^)]*\))")
_HEATER_LABEL_RE = re.compile(r"\(heater\s*(\d+),\s*(top|bottom|nozzle)\)")
_XV_RE = re.compile(r"\b(XV\d+)\b")
_PID_BLOCKS_RE = re.compile(r"^_PID_BLOCKS\s*=\s*\{(.*?)^\}", re.S | re.M)
_PID_ENTRY_RE = re.compile(r"[\"'](top|bottom|nozzle)[\"']\s*:\s*(\d+)")

#: LN2 level feeds: the state whose freshness an open needs, plus everything
#: worth showing.  The ballast and primary Xe levels come from the GHS Wi-Fi
#: ESP32 (the PLC keeps writing its last value forever when it drops out),
#: hence the extra heartbeat requirement for those two.
_LEVEL_FEEDS = {
    "cryostat":   {"level_state": "level_cryostat_probe",
                   "level_states": ("level_cryostat", "level_cryostat_probe"),
                   "esp32_state": None},
    "primary_xe": {"level_state": "level_primary_xe",
                   "level_states": ("level_primary_xe",),
                   "esp32_state": "ghs_esp32_alive"},
    "ballast":    {"level_state": "level_ballast",
                   "level_states": ("level_ballast",),
                   "esp32_state": "ghs_esp32_alive"},
}


def _flatten_states(doc: dict) -> Dict[str, dict]:
    states: Dict[str, dict] = {}
    for group in (doc.get("groups") or {}).values():
        if isinstance(group, dict):
            for sid, sdef in group.items():
                if isinstance(sdef, dict):
                    states[sid] = sdef
    return states


def _source_topic(sdef: Optional[dict]) -> Optional[str]:
    if not sdef:
        return None
    src = sdef.get("source")
    if isinstance(src, dict) and isinstance(src.get("topic"), str):
        return src["topic"]
    return None


def read_pid_blocks(plc_path) -> Dict[str, int]:
    """``{"top": 100, ...}`` from drivers/plc.py's ``_PID_BLOCKS`` (best effort, {} if absent)."""
    try:
        text = Path(plc_path).read_text(encoding="utf-8")
    except OSError:
        return {}
    m = _PID_BLOCKS_RE.search(text)
    if not m:
        return {}
    blocks = {z: int(df) for z, df in _PID_ENTRY_RE.findall(m.group(1))}
    return blocks if set(blocks) == set(ZONES) else {}


class ZoneMap:
    """Content-detected maps for zones, NGE channels, LN2 vessels and pneumatic valves.

    ``zones[z]`` keys: ``status_topic``, ``cube_state``, ``cube_topic``,
    ``cube_alias``, ``cube_label``, ``clamp_state``, ``clamp_topic``,
    ``clamp_alias``, ``clamp_label`` (clamp fields None for the nozzle),
    ``physical`` ("top" | "bottom" | "nozzle" | "unknown"), ``physical_note``,
    ``fingerprint``, ``pid_label`` (e.g. "Zone 1 (top)"), ``pid_df`` (first DF
    register of the PLC PID block, or None) and ``pid_block`` (1–3 or None).
    """

    ZONES = ZONES
    CHANNELS = CHANNELS
    VESSELS = VESSELS
    VALVES = VALVES

    def __init__(self, zones: Dict[str, dict], channels: Dict[str, dict],
                 vessels: Dict[str, dict], valves: Dict[str, dict],
                 errors: Optional[List[str]] = None, source_path: Optional[str] = None):
        self.zones = zones
        self.channels = channels
        self.vessels = vessels
        self.valves = valves
        self.errors = list(errors or [])
        self.source_path = source_path

    # construction --------------------------------------------------------

    @classmethod
    def from_state_yaml(cls, path, plc_path=None) -> "ZoneMap":
        """Build the maps from a state.yaml (and, for PID block numbers, the
        PLC driver next to it: ``<dir>/drivers/plc.py`` unless given)."""
        path = Path(path)
        with open(path, encoding="utf-8") as f:
            doc = yaml.safe_load(f) or {}
        states = _flatten_states(doc)
        blocks = read_pid_blocks(plc_path if plc_path is not None
                                 else path.parent / "drivers" / "plc.py")
        zm = cls.from_states(states, pid_blocks=blocks)
        zm.source_path = str(path)
        for e in zm.errors:
            log.warning("zone map: %s", e)
        return zm

    @classmethod
    def from_states(cls, states: Dict[str, dict],
                    pid_blocks: Optional[Dict[str, int]] = None) -> "ZoneMap":
        errors: List[str] = []
        pid_blocks = pid_blocks or {}
        zones = {z: cls._detect_zone(z, states, pid_blocks, errors) for z in ZONES}

        channels: Dict[str, dict] = {}
        for ch in CHANNELS:
            sid = f"nge_ch{ch}_output"
            sdef = states.get(sid)
            label = (sdef or {}).get("label") or f"NGE ch {ch}"
            m = _HEATER_LABEL_RE.search(label)
            channels[ch] = {
                "label": re.sub(r"\s+output on$", "", label),
                "output_state": sid if sdef else None,
                "status_topic": _source_topic(sdef) or f"xsphere/status/nge/{ch}",
                "heater": int(m.group(1)) if m else None,
                "zone": m.group(2) if m else None,     # software zone named in the label
            }
            if not sdef:
                errors.append(f"state {sid} missing — NGE ch {ch} label unknown")

        vessels: Dict[str, dict] = {}
        for v in VESSELS:
            sid = f"valve_{v}"
            sdef = states.get(sid)
            label = (sdef or {}).get("label") or v
            m = _XV_RE.search(label)
            feeds = _LEVEL_FEEDS[v]
            level_state = feeds["level_state"] if feeds["level_state"] in states else None
            if level_state is None:
                errors.append(f"level state {feeds['level_state']} for {v} missing")
            esp32 = feeds["esp32_state"]
            if esp32 is not None and esp32 not in states:
                errors.append(f"state {esp32} missing")
            vessels[v] = {
                "label": label,
                "xv": m.group(1) if m else None,
                "state_id": sid if sdef else None,
                "status_topic": _source_topic(sdef) or f"xsphere/status/valve/{v}",
                "command_prefix": f"xsphere/commands/valve/{v}",
                "level_state": level_state,
                "level_states": [s for s in feeds["level_states"] if s in states],
                "esp32_state": esp32,
            }
            if not sdef:
                errors.append(f"state {sid} missing")

        valves: Dict[str, dict] = {}
        for v in VALVES:
            sid = f"pneumatic_{v}"
            sdef = states.get(sid)
            label = (sdef or {}).get("label") or v
            normally = ("open" if "normally open" in label
                        else "closed" if "normally closed" in label else None)
            valves[v] = {
                "label": label,
                "state_id": sid if sdef else None,
                "status_topic": _source_topic(sdef) or f"xsphere/status/pneumatic/{v}",
                "command_topic": f"xsphere/commands/pneumatic/{v}/state",
                "normally": normally,
            }
            if not sdef:
                errors.append(f"state {sid} missing")

        return cls(zones, channels, vessels, valves, errors)

    @staticmethod
    def _detect_zone(zone: str, states: Dict[str, dict], pid_blocks: Dict[str, int],
                     errors: List[str]) -> dict:
        # PID status topic, from any pid_<zone>_* state's source.
        status_topic = None
        pid_label = None
        for sid, sdef in states.items():
            if sid.startswith(f"pid_{zone}_"):
                t = _source_topic(sdef)
                if t and t.startswith("xsphere/status/pid/"):
                    status_topic = status_topic or t
                    m = _ZONE_LABEL_RE.match(sdef.get("label") or "")
                    if m and pid_label is None:
                        pid_label = re.sub(r"\s+", " ", m.group(1))
        if status_topic is None:
            status_topic = f"xsphere/status/pid/{zone}"
            errors.append(f"no pid_{zone}_* state — assuming {status_topic}")

        cube_state = f"t_cube_{zone}"
        cube_def = states.get(cube_state)
        cube_topic = _source_topic(cube_def)
        cube_ch = None
        if cube_topic and _CUBE_TOPIC_RE.fullmatch(cube_topic):
            cube_ch = int(_CUBE_TOPIC_RE.fullmatch(cube_topic).group(1))
        else:
            errors.append(f"{cube_state}: source {cube_topic!r} is not a LabJack RTD topic")

        clamp_state = clamp_topic = clamp_label = None
        clamp_ch = None
        if zone != "nozzle":
            clamp_state = f"t_{zone}_clamp"
            clamp_def = states.get(clamp_state)
            clamp_topic = _source_topic(clamp_def)
            clamp_label = (clamp_def or {}).get("label")
            if clamp_topic and _CLAMP_TOPIC_RE.fullmatch(clamp_topic):
                clamp_ch = int(_CLAMP_TOPIC_RE.fullmatch(clamp_topic).group(1))
            else:
                errors.append(f"{clamp_state}: source {clamp_topic!r} is not a PLC RTD topic")
            if clamp_def is None:
                clamp_state = None

        by_cube = CUBE_RTD_POSITION.get(cube_ch) if cube_ch is not None else None
        by_clamp = CLAMP_RTD_POSITION.get(clamp_ch) if clamp_ch is not None else None
        if by_cube and by_clamp and by_cube != by_clamp:
            physical = "unknown"
            note = f"cube sensor says {by_cube}, clamp sensor says {by_clamp}"
            errors.append(f"zone {zone}: {note}")
        elif by_cube or by_clamp:
            physical = by_cube or by_clamp
            note = None
        else:
            physical, note = "unknown", "no recognised sensor"
            errors.append(f"zone {zone}: physical position unknown ({note})")

        df = pid_blocks.get(zone)
        block = (df - 100) // 25 + 1 if df in (100, 125, 150) else None
        return {
            "status_topic": status_topic,
            "pid_label": pid_label or f"zone {zone}",
            "pid_df": df,
            "pid_block": block,
            "cube_state": cube_state if cube_def is not None else None,
            "cube_topic": cube_topic,
            "cube_alias": f"rtd{cube_ch}" if cube_ch is not None else None,
            "cube_label": (cube_def or {}).get("label"),
            "clamp_state": clamp_state,
            "clamp_topic": clamp_topic,
            "clamp_alias": f"rtd{clamp_ch}" if clamp_ch is not None else None,
            "clamp_label": clamp_label,
            "physical": physical,
            "physical_note": note,
            "fingerprint": f"cube={cube_topic or '-'}|clamp={clamp_topic or '-'}",
        }

    # queries -------------------------------------------------------------

    def fingerprint(self, zone: str) -> str:
        return self.zones[zone]["fingerprint"]

    def fingerprints(self) -> Dict[str, str]:
        return {z: self.zones[z]["fingerprint"] for z in ZONES}

    def alias(self, zone: str, source: str) -> Optional[str]:
        """PV expression alias for ``source`` ("cube" | "clamp") of ``zone``."""
        if source not in ("cube", "clamp") or zone not in self.zones:
            return None
        return self.zones[zone][f"{source}_alias"]

    def sensor_state(self, zone: str, source: str) -> Optional[str]:
        """Registry state id of that sensor (e.g. ``t_top_clamp``), or None."""
        if source not in ("cube", "clamp") or zone not in self.zones:
            return None
        return self.zones[zone][f"{source}_state"]

    def describe(self, zone: str) -> str:
        """One line, e.g. ``software zone top = Zone 1 (top), PID block 1 (DF100);
        sensors: clamp PLC RTD5 (rtd5), cube LJ RTD2 (rtd2) — physically BOTTOM``."""
        z = self.zones[zone]
        head = f"software zone {zone} = {z['pid_label']}"
        if z["pid_block"] is not None:
            head += f", PID block {z['pid_block']} (DF{z['pid_df']})"
        parts = []
        if z["clamp_alias"]:
            parts.append(f"clamp PLC RTD{z['clamp_alias'][3:]} ({z['clamp_alias']})")
        elif zone != "nozzle":
            parts.append("clamp sensor not recognised")
        if z["cube_alias"]:
            parts.append(f"cube LJ RTD{z['cube_alias'][3:]} ({z['cube_alias']})")
        else:
            parts.append("cube sensor not recognised")
        if zone == "nozzle":
            parts.append("no clamp sensor")
        if z["physical"] == "unknown":
            where = "physical position UNKNOWN" + (f" ({z['physical_note']})"
                                                   if z["physical_note"] else "")
        else:
            where = f"physically {z['physical'].upper()}"
        return f"{head}; sensors: {', '.join(parts)} — {where}"

    def describe_all(self) -> Dict[str, str]:
        return {z: self.describe(z) for z in ZONES}

    def channel_zone(self, channel) -> Optional[str]:
        """Software zone the registry label puts on an NGE channel (informational)."""
        ch = self.channels.get(str(channel))
        return ch["zone"] if ch else None
