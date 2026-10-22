"""
The human's rails: limits the AI works within and can never change.

Deny by default.  ``DEFAULT_RAILS`` allows nothing (every limit null, every
permission false), and so does a missing file, an unreadable file or one that
fails validation — the last two also set :attr:`RailsStore.error` so every
write is refused and the status says "rails file invalid".  The only way to
change the rails is :meth:`RailsStore.replace`, which the service calls for a
human's MQTT message on ``<cmd_prefix>/rails`` (the Control page's rails
editor); no AI tool reaches it.

Schema (version 2; every key required in a stored document, unknown keys
rejected, numbers finite int/float and never bool)::

    {"version": 2,
     "revision": int,                          # managed here, ignored in input
     "zone_fingerprints": {zone: str},         # set here on save, ignored in input
     "updated_utc": str, "updated_by": str,    # set here on save, ignored in input
     "general": {"arm_hours": num|null},       # >0 ≤168; null = arms never expire
     "heaters": {
       "zones": {"top"|"bottom"|"nozzle": {
          "setpoint_min_k": num|null, "setpoint_max_k": num|null,   # both or neither; 0<min<max≤400
          "max_step_k": num|null,                                   # >0
          "max_rate_k_per_min": num|null,                           # >0
          "pv_sources": ["cube"|"clamp", ...]}},                    # nozzle: "cube" only
       "supply": {"1"|"2"|"3": {
          "voltage_min_v": num|null, "voltage_max_v": num|null,     # both or neither; 0≤min≤max≤32
          "current_min_a": num|null, "current_max_a": num|null,     # both or neither; 0≤min≤max≤3
          "may_switch_on": bool, "may_switch_off": bool}}},
     "ln2": {"vessels": {"cryostat"|"primary_xe"|"ballast": {
          "may_operate": bool, "max_open_minutes": num|null,        # >0 ≤120; needed to open
          "may_set_auto_open": bool, "may_disarm_auto_close": bool}}},
     "pneumatic": {"valves": {"ballast"|"pump"|"bottle": {"may_open": bool, "may_close": bool}},
                   "forbid_open_together": [[valve, valve], ...],   # two different valves
                   "min_interval_s": num}}                          # ≥2

Heater rails are keyed by *software* zone name, and those names are about to
be re-pointed (branch claude/swap-top-bottom).  Each save therefore stores the
zone fingerprints of the registry it was made against; when a zone's
fingerprint no longer matches, that zone's heater rails are treated as unset
(:attr:`RailsStore.stale_zones`) until a human re-saves them.  Supply rails
are keyed by NGE channel, which the swap does not move.
"""

from __future__ import annotations

import copy
import json
import logging
import math
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from .zones import CHANNELS, VALVES, VESSELS, ZONES

log = logging.getLogger(__name__)

RAILS_VERSION = 2

CONTENT_KEYS = ("general", "heaters", "ln2", "pneumatic")
META_KEYS = ("revision", "zone_fingerprints", "updated_utc", "updated_by")
PV_SOURCES = ("cube", "clamp")

_ZONE_UNSET = {"setpoint_min_k": None, "setpoint_max_k": None, "max_step_k": None,
               "max_rate_k_per_min": None, "pv_sources": []}
_SUPPLY_UNSET = {"voltage_min_v": None, "voltage_max_v": None,
                 "current_min_a": None, "current_max_a": None,
                 "may_switch_on": False, "may_switch_off": False}
_VESSEL_UNSET = {"may_operate": False, "max_open_minutes": None,
                 "may_set_auto_open": False, "may_disarm_auto_close": False}
_VALVE_UNSET = {"may_open": False, "may_close": False}

#: Nothing writable.  Treat as read-only; use :func:`default_rails` for a copy.
DEFAULT_RAILS: Dict[str, Any] = {
    "version": RAILS_VERSION,
    "revision": 0,
    "zone_fingerprints": {z: "" for z in ZONES},
    "general": {"arm_hours": 8},
    "heaters": {
        "zones": {z: copy.deepcopy(_ZONE_UNSET) for z in ZONES},
        "supply": {ch: copy.deepcopy(_SUPPLY_UNSET) for ch in CHANNELS},
    },
    "ln2": {"vessels": {v: copy.deepcopy(_VESSEL_UNSET) for v in VESSELS}},
    "pneumatic": {
        "valves": {v: copy.deepcopy(_VALVE_UNSET) for v in VALVES},
        "forbid_open_together": [],
        "min_interval_s": 10,
    },
}


def default_rails() -> Dict[str, Any]:
    return copy.deepcopy(DEFAULT_RAILS)


def unset_zone_rails() -> Dict[str, Any]:
    return copy.deepcopy(_ZONE_UNSET)


# ──────────────────────────────────────────────────────────────────────────
# Validation
# ──────────────────────────────────────────────────────────────────────────

def _show(value: Any) -> str:
    try:
        text = json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        text = repr(value)
    return text if len(text) <= 40 else text[:37] + "..."


def _is_finite_number(x: Any) -> bool:
    if isinstance(x, bool) or not isinstance(x, (int, float)):
        return False
    try:
        return math.isfinite(float(x))
    except OverflowError:                       # an int too large for a float
        return False


def _check_obj(value: Any, path: str, keys, errs: List[str],
               optional=()) -> Optional[dict]:
    """``value`` must be an object with exactly ``keys`` (+ any of ``optional``)."""
    if not isinstance(value, dict):
        errs.append(f"{path}: must be an object (got {_show(value)})")
        return None
    for k in value:
        if k not in keys and k not in optional:
            errs.append(f"{path}: unknown key {_show(k)}")
    for k in keys:
        if k not in value:
            errs.append(f"{path}: missing key {_show(k)}")
    return value


def _check_num(obj: dict, key: str, path: str, errs: List[str], *, nullable: bool,
               gt: Optional[float] = None, ge: Optional[float] = None,
               le: Optional[float] = None, rule: str = "") -> Optional[float]:
    if key not in obj:
        return None
    v = obj[key]
    where = f"{path}.{key}"
    if v is None:
        if not nullable:
            errs.append(f"{where}: must be a number (got null)")
        return None
    if not _is_finite_number(v):
        errs.append(f"{where}: must be a finite number{' or null' if nullable else ''} "
                    f"(got {_show(v)})")
        return None
    if ((gt is not None and not v > gt) or (ge is not None and not v >= ge)
            or (le is not None and not v <= le)):
        errs.append(f"{where}: must be {rule} (got {_show(v)})")
        return None
    return float(v)


def _check_bool(obj: dict, key: str, path: str, errs: List[str]) -> None:
    if key in obj and not isinstance(obj[key], bool):
        errs.append(f"{path}.{key}: must be true or false (got {_show(obj[key])})")


def _check_pair(lo: Optional[float], hi: Optional[float], obj: dict, lo_key: str,
                hi_key: str, path: str, errs: List[str], strict: bool) -> None:
    # Only judge the pair when both members are individually valid or null.
    if lo_key not in obj or hi_key not in obj:
        return
    lo_raw, hi_raw = obj[lo_key], obj[hi_key]
    if (lo_raw is not None and lo is None) or (hi_raw is not None and hi is None):
        return                                  # already reported as a bad value
    if (lo_raw is None) != (hi_raw is None):
        errs.append(f"{path}: set both {lo_key} and {hi_key}, or neither")
        return
    if lo is None:
        return
    got = f"{_show(lo_raw)} vs {_show(hi_raw)}"
    if strict and not lo < hi:
        errs.append(f"{path}: {lo_key} must be below {hi_key} ({got})")
    elif not strict and not lo <= hi:
        errs.append(f"{path}: {lo_key} must not exceed {hi_key} ({got})")


def validate_rails(doc: Any, stored: bool = False) -> List[str]:
    """Every problem with a rails document, as readable strings ([] = valid).

    ``stored=True`` also requires the meta keys a saved file carries
    (``revision``, ``zone_fingerprints``); in input they are optional and
    ignored by :meth:`RailsStore.replace`.
    """
    errs: List[str] = []
    required = ("version",) + CONTENT_KEYS + (("revision", "zone_fingerprints") if stored else ())
    optional = tuple(k for k in META_KEYS if k not in required)
    top = _check_obj(doc, "rails", required, errs, optional=optional)
    if top is None:
        return errs

    version = top.get("version")
    if "version" in top and (isinstance(version, bool) or not isinstance(version, int)
                             or version != RAILS_VERSION):
        errs.append(f"rails.version: must be {RAILS_VERSION} (got {_show(version)})")
    revision = top.get("revision")
    if "revision" in top and (isinstance(revision, bool) or not isinstance(revision, int)
                              or revision < 0):
        errs.append(f"rails.revision: must be a non-negative integer (got {_show(revision)})")
    if "zone_fingerprints" in top:
        fps = _check_obj(top["zone_fingerprints"], "zone_fingerprints", ZONES, errs)
        if fps is not None:
            for z in ZONES:
                if z in fps and not isinstance(fps[z], str):
                    errs.append(f"zone_fingerprints.{z}: must be a string (got {_show(fps[z])})")
    for k in ("updated_utc", "updated_by"):
        if k in top and top[k] is not None and not isinstance(top[k], str):
            errs.append(f"rails.{k}: must be a string or null (got {_show(top[k])})")

    # general
    if "general" in top:
        g = _check_obj(top["general"], "general", ("arm_hours",), errs)
        if g is not None:
            _check_num(g, "arm_hours", "general", errs, nullable=True, gt=0, le=168,
                       rule="> 0 and ≤ 168 hours")

    # heaters
    if "heaters" in top:
        h = _check_obj(top["heaters"], "heaters", ("zones", "supply"), errs)
        if h is not None and "zones" in h:
            zones = _check_obj(h["zones"], "heaters.zones", ZONES, errs)
            for z in ZONES if zones is not None else ():
                if z not in zones:
                    continue
                p = f"heaters.zones.{z}"
                zr = _check_obj(zones[z], p, tuple(_ZONE_UNSET), errs)
                if zr is None:
                    continue
                lo = _check_num(zr, "setpoint_min_k", p, errs, nullable=True, gt=0, le=400,
                                rule="> 0 and ≤ 400 K")
                hi = _check_num(zr, "setpoint_max_k", p, errs, nullable=True, gt=0, le=400,
                                rule="> 0 and ≤ 400 K")
                _check_pair(lo, hi, zr, "setpoint_min_k", "setpoint_max_k", p, errs, strict=True)
                _check_num(zr, "max_step_k", p, errs, nullable=True, gt=0, rule="> 0 K")
                _check_num(zr, "max_rate_k_per_min", p, errs, nullable=True, gt=0,
                           rule="> 0 K/min")
                if "pv_sources" in zr:
                    _check_pv_sources(zr["pv_sources"], z, f"{p}.pv_sources", errs)
        if h is not None and "supply" in h:
            supply = _check_obj(h["supply"], "heaters.supply", CHANNELS, errs)
            for ch in CHANNELS if supply is not None else ():
                if ch not in supply:
                    continue
                p = f"heaters.supply.{ch}"
                sr = _check_obj(supply[ch], p, tuple(_SUPPLY_UNSET), errs)
                if sr is None:
                    continue
                vlo = _check_num(sr, "voltage_min_v", p, errs, nullable=True, ge=0, le=32,
                                 rule="≥ 0 and ≤ 32 V")
                vhi = _check_num(sr, "voltage_max_v", p, errs, nullable=True, ge=0, le=32,
                                 rule="≥ 0 and ≤ 32 V")
                _check_pair(vlo, vhi, sr, "voltage_min_v", "voltage_max_v", p, errs, strict=False)
                ilo = _check_num(sr, "current_min_a", p, errs, nullable=True, ge=0, le=3,
                                 rule="≥ 0 and ≤ 3 A")
                ihi = _check_num(sr, "current_max_a", p, errs, nullable=True, ge=0, le=3,
                                 rule="≥ 0 and ≤ 3 A")
                _check_pair(ilo, ihi, sr, "current_min_a", "current_max_a", p, errs, strict=False)
                _check_bool(sr, "may_switch_on", p, errs)
                _check_bool(sr, "may_switch_off", p, errs)

    # ln2
    if "ln2" in top:
        l2 = _check_obj(top["ln2"], "ln2", ("vessels",), errs)
        if l2 is not None and "vessels" in l2:
            vessels = _check_obj(l2["vessels"], "ln2.vessels", VESSELS, errs)
            for v in VESSELS if vessels is not None else ():
                if v not in vessels:
                    continue
                p = f"ln2.vessels.{v}"
                vr = _check_obj(vessels[v], p, tuple(_VESSEL_UNSET), errs)
                if vr is None:
                    continue
                _check_bool(vr, "may_operate", p, errs)
                _check_num(vr, "max_open_minutes", p, errs, nullable=True, gt=0, le=120,
                           rule="> 0 and ≤ 120 minutes")
                _check_bool(vr, "may_set_auto_open", p, errs)
                _check_bool(vr, "may_disarm_auto_close", p, errs)

    # pneumatic
    if "pneumatic" in top:
        pn = _check_obj(top["pneumatic"], "pneumatic",
                        ("valves", "forbid_open_together", "min_interval_s"), errs)
        if pn is not None:
            if "valves" in pn:
                valves = _check_obj(pn["valves"], "pneumatic.valves", VALVES, errs)
                for v in VALVES if valves is not None else ():
                    if v not in valves:
                        continue
                    p = f"pneumatic.valves.{v}"
                    vr = _check_obj(valves[v], p, tuple(_VALVE_UNSET), errs)
                    if vr is not None:
                        _check_bool(vr, "may_open", p, errs)
                        _check_bool(vr, "may_close", p, errs)
            if "forbid_open_together" in pn:
                _check_forbid_pairs(pn["forbid_open_together"], errs)
            _check_num(pn, "min_interval_s", "pneumatic", errs, nullable=False, ge=2,
                       rule="≥ 2 s")
    return errs


def _check_pv_sources(value: Any, zone: str, path: str, errs: List[str]) -> None:
    if not isinstance(value, list):
        errs.append(f"{path}: must be a list (got {_show(value)})")
        return
    seen = set()
    for item in value:
        if not isinstance(item, str) or item not in PV_SOURCES:
            errs.append(f"{path}: unknown source {_show(item)} (allowed: cube, clamp)")
        elif item == "clamp" and zone == "nozzle":
            errs.append(f"{path}: the nozzle has no clamp sensor — only \"cube\" is allowed")
        elif item in seen:
            errs.append(f"{path}: {_show(item)} listed twice")
        else:
            seen.add(item)


def _check_forbid_pairs(value: Any, errs: List[str]) -> None:
    path = "pneumatic.forbid_open_together"
    if not isinstance(value, list):
        errs.append(f"{path}: must be a list of [valve, valve] pairs (got {_show(value)})")
        return
    for i, pair in enumerate(value):
        p = f"{path}[{i}]"
        if not isinstance(pair, list) or len(pair) != 2:
            errs.append(f"{p}: must be a pair [valve, valve] (got {_show(pair)})")
            continue
        bad = [x for x in pair if not isinstance(x, str) or x not in VALVES]
        if bad:
            errs.append(f"{p}: unknown valve {_show(bad[0])} (valves: {', '.join(VALVES)})")
        elif pair[0] == pair[1]:
            errs.append(f"{p}: a pair needs two different valves (got {_show(pair)})")


# ──────────────────────────────────────────────────────────────────────────
# Store
# ──────────────────────────────────────────────────────────────────────────

def _reject_constant(name: str):
    raise ValueError(f"non-finite JSON constant {name}")


def _finite_float(text: str) -> float:
    value = float(text)
    if not math.isfinite(value):
        raise ValueError(f"non-finite JSON number {text}")
    return value


def _flatten(obj: Any, prefix: str, out: Dict[str, Any]) -> None:
    if isinstance(obj, dict) and obj:
        for k, v in obj.items():
            _flatten(v, f"{prefix}.{k}" if prefix else str(k), out)
    else:
        out[prefix] = obj


def rails_diff(old: dict, new: dict) -> str:
    """Human-readable ``path: old → new`` list over the content keys."""
    a: Dict[str, Any] = {}
    b: Dict[str, Any] = {}
    for k in CONTENT_KEYS:
        _flatten(old.get(k), k, a)
        _flatten(new.get(k), k, b)
    changes = [f"{p}: {_show(a.get(p))} → {_show(b.get(p))}"
               for p in list(dict.fromkeys(list(a) + list(b))) if a.get(p) != b.get(p)]
    if not changes:
        return "no changes (rails re-confirmed)"
    if len(changes) > 20:
        changes = changes[:20] + [f"… and {len(changes) - 20} more"]
    return "; ".join(changes)


def _utc_iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class RailsStore:
    """The persisted rails document (``state_dir/rails.json``).

    :meth:`get` returns the EFFECTIVE rails — what every write checks: the
    defaults when there is no valid file, and stale-fingerprint zones replaced
    by unset rails.  :meth:`stored` returns the document as saved, for the
    human's editor.  Thread-safe.
    """

    def __init__(self, path, zone_map=None, *, clock: Callable[[], float] = time.time):
        self.path = Path(path)
        self._zone_map = zone_map
        self._clock = clock
        self._lock = threading.RLock()
        self._stored: Dict[str, Any] = self._defaults()
        self._effective: Dict[str, Any] = copy.deepcopy(self._stored)
        self._error: Optional[str] = None
        self._stale: List[str] = []

    # state ---------------------------------------------------------------

    @property
    def revision(self) -> int:
        with self._lock:
            return int(self._stored.get("revision", 0))

    @property
    def error(self) -> Optional[str]:
        with self._lock:
            return self._error

    @property
    def stale_zones(self) -> List[str]:
        with self._lock:
            return list(self._stale)

    @property
    def updated_utc(self) -> Optional[str]:
        with self._lock:
            return self._stored.get("updated_utc")

    @property
    def updated_by(self) -> Optional[str]:
        with self._lock:
            return self._stored.get("updated_by")

    def _fingerprints(self) -> Dict[str, str]:
        if self._zone_map is None:
            return {z: "" for z in ZONES}
        return {z: self._zone_map.fingerprint(z) for z in ZONES}

    def _defaults(self, revision: int = 0) -> Dict[str, Any]:
        d = default_rails()
        d["revision"] = revision
        d["zone_fingerprints"] = self._fingerprints()
        return d

    def _set(self, stored: Dict[str, Any], error: Optional[str], check_fingerprints: bool) -> None:
        stale: List[str] = []
        effective = copy.deepcopy(stored)
        if check_fingerprints and self._zone_map is not None:
            current = self._fingerprints()
            for z in ZONES:
                zone_rails = stored["heaters"]["zones"][z]
                if (stored["zone_fingerprints"].get(z) != current[z]
                        and zone_rails != _ZONE_UNSET):
                    stale.append(z)
                    effective["heaters"]["zones"][z] = unset_zone_rails()
        self._stored, self._effective, self._error, self._stale = stored, effective, error, stale

    # loading -------------------------------------------------------------

    def load(self) -> None:
        """(Re)read the file.  Never raises; problems end in deny-all + :attr:`error`."""
        with self._lock:
            try:
                text = self.path.read_text(encoding="utf-8")
            except FileNotFoundError:
                self._set(self._defaults(), None, check_fingerprints=False)
                log.info("No rails file at %s — nothing is writable until rails are saved",
                         self.path)
                return
            except (OSError, UnicodeDecodeError) as e:
                self._fail(f"unreadable: {e}", revision=0)
                return
            try:
                doc = json.loads(text, parse_constant=_reject_constant, parse_float=_finite_float)
            except ValueError as e:
                self._fail(f"not JSON: {e}", revision=0)
                return
            errs = validate_rails(doc, stored=True)
            if errs:
                rev = doc.get("revision") if isinstance(doc, dict) else None
                rev = rev if isinstance(rev, int) and not isinstance(rev, bool) and rev >= 0 else 0
                more = f" (+{len(errs) - 5} more)" if len(errs) > 5 else ""
                self._fail("; ".join(errs[:5]) + more, revision=rev)
                return
            self._set(doc, None, check_fingerprints=True)
            log.info("Rails revision %d loaded from %s (updated %s by %s)%s", self.revision,
                     self.path, doc.get("updated_utc"), doc.get("updated_by"),
                     f"; STALE zones {self._stale} — zone names changed since these rails "
                     f"were set" if self._stale else "")

    def _fail(self, detail: str, revision: int) -> None:
        msg = f"rails file invalid — nothing writable until saved again ({detail})"
        log.error("%s: %s", self.path, msg)
        self._set(self._defaults(revision), msg, check_fingerprints=False)

    # reading -------------------------------------------------------------

    def get(self) -> Dict[str, Any]:
        """Deep copy of the EFFECTIVE rails (what writes are checked against)."""
        with self._lock:
            return copy.deepcopy(self._effective)

    def stored(self) -> Dict[str, Any]:
        """Deep copy of the rails as saved (defaults if none / invalid)."""
        with self._lock:
            return copy.deepcopy(self._stored)

    def zone(self, zone: str) -> Dict[str, Any]:
        with self._lock:
            return copy.deepcopy(self._effective["heaters"]["zones"][zone])

    def channel(self, channel) -> Dict[str, Any]:
        with self._lock:
            return copy.deepcopy(self._effective["heaters"]["supply"][str(channel)])

    def vessel(self, vessel: str) -> Dict[str, Any]:
        with self._lock:
            return copy.deepcopy(self._effective["ln2"]["vessels"][vessel])

    def valve(self, valve: str) -> Dict[str, Any]:
        with self._lock:
            return copy.deepcopy(self._effective["pneumatic"]["valves"][valve])

    def pneumatic(self) -> Dict[str, Any]:
        """``{"forbid_open_together": [...], "min_interval_s": n}`` (effective)."""
        with self._lock:
            pn = self._effective["pneumatic"]
            return {"forbid_open_together": copy.deepcopy(pn["forbid_open_together"]),
                    "min_interval_s": pn["min_interval_s"]}

    def general(self) -> Dict[str, Any]:
        with self._lock:
            return copy.deepcopy(self._effective["general"])

    @property
    def arm_hours(self) -> Optional[float]:
        with self._lock:
            return self._effective["general"]["arm_hours"]

    def zone_limits(self, zone: str) -> Optional[Tuple[float, float]]:
        """(setpoint_min_k, setpoint_max_k) when both are set and the zone is not stale."""
        z = self.zone(zone)
        if z["setpoint_min_k"] is None or z["setpoint_max_k"] is None:
            return None
        return float(z["setpoint_min_k"]), float(z["setpoint_max_k"])

    # writing -------------------------------------------------------------

    def replace(self, doc: Any, base_revision: Any, by: Any) -> Tuple[bool, Optional[str], str]:
        """Save a complete new rails document from a human.

        Returns ``(ok, error, diff_summary)``.  Refused when ``base_revision``
        is not the current revision (someone saved in between), when the
        document is invalid, or when the file cannot be written; the rails in
        force are then unchanged.
        """
        who = by.strip()[:80] if isinstance(by, str) and by.strip() else "unknown"
        if isinstance(base_revision, bool) or not isinstance(base_revision, int):
            return False, f"base_revision must be an integer (got {_show(base_revision)})", ""
        with self._lock:
            current = self.revision
            if base_revision != current:
                return (False, f"rails changed since you opened the editor (now revision "
                               f"{current}, yours {base_revision}) — reload and re-apply", "")
            errs = validate_rails(doc)
            if errs:
                more = f" (+{len(errs) - 10} more)" if len(errs) > 10 else ""
                return False, "; ".join(errs[:10]) + more, ""
            new: Dict[str, Any] = {
                "version": RAILS_VERSION,
                "revision": current + 1,
                "zone_fingerprints": self._fingerprints(),
                "updated_utc": _utc_iso(self._clock()),
                "updated_by": who,
            }
            for k in CONTENT_KEYS:
                new[k] = copy.deepcopy(doc[k])
            diff = rails_diff(self._stored, new)
            try:
                self._write(new)
            except (OSError, ValueError, TypeError) as e:
                log.error("Could not save rails to %s: %s", self.path, e)
                return False, f"could not save the rails file: {e}", ""
            self._set(new, None, check_fingerprints=False)
            log.info("Rails revision %d saved by %s: %s", new["revision"], who, diff)
            return True, None, diff

    def replace_from_command(self, payload: Any, default_by: str = "unknown"
                             ) -> Tuple[bool, Optional[str], str]:
        """The MQTT ``<cmd_prefix>/rails`` message:
        ``{"rails": <full doc>, "base_revision": int, "by"?: str}``."""
        if not isinstance(payload, dict):
            return False, "rails command must be a JSON object", ""
        unknown = [k for k in payload if k not in ("rails", "base_revision", "by")]
        if unknown:
            return False, f"rails command: unknown key {_show(unknown[0])}", ""
        if "rails" not in payload or "base_revision" not in payload:
            return False, "rails command needs \"rails\" and \"base_revision\"", ""
        return self.replace(payload["rails"], payload["base_revision"],
                            payload.get("by", default_by))

    def _write(self, doc: Dict[str, Any]) -> None:
        """Atomic: write ``rails.json.tmp``, fsync, then ``os.replace`` over the file."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        text = json.dumps(doc, indent=2, ensure_ascii=False, allow_nan=False) + "\n"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(text)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.path)
        except BaseException:
            try:
                tmp.unlink()
            except OSError:
                pass
            raise
