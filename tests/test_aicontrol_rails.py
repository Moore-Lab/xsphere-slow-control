"""
Offline tests for the AI control rails (aicontrol/rails.py) and the zone map
they are fingerprinted against (aicontrol/zones.py).

    python -m pytest tests/test_aicontrol_rails.py -q      (from the repo root)

The zone map is read from the REAL slowcontrol/state.yaml and from copies with
the clamp / cube sources in the main and the swapped (claude/swap-top-bottom)
layout.  Nothing here touches MQTT or the network.
"""

import copy
import json
import os
import re
import threading
from pathlib import Path

import pytest
import yaml

from aicontrol import rails as rails_mod
from aicontrol.rails import (DEFAULT_RAILS, RailsStore, default_rails, rails_diff,
                             validate_rails)
from aicontrol.zones import ZoneMap

REPO = Path(__file__).resolve().parent.parent
STATE_YAML = REPO / "slowcontrol" / "state.yaml"

CUBE = "xsphere/sensors/temperature/labjack/rtd/{}"
CLAMP = "xsphere/sensors/temperature/plc/rtd/{}"

# Software-name → sensor channel, before and after claude/swap-top-bottom.
MAIN_LAYOUT = {"t_cube_top": CUBE.format(2), "t_cube_bottom": CUBE.format(3),
               "t_top_clamp": CLAMP.format(5), "t_bottom_clamp": CLAMP.format(6)}
SWAPPED_LAYOUT = {"t_cube_top": CUBE.format(3), "t_cube_bottom": CUBE.format(2),
                  "t_top_clamp": CLAMP.format(6), "t_bottom_clamp": CLAMP.format(5)}


# ──────────────────────────────────────────────────────────────────────────
# helpers
# ──────────────────────────────────────────────────────────────────────────

def write_layout(tmp_path, layout, name="state.yaml"):
    """Copy of the real registry with the temperature sources set to ``layout``."""
    with open(STATE_YAML, encoding="utf-8") as f:
        doc = yaml.safe_load(f)
    temps = doc["groups"]["temperature"]
    for sid, topic in layout.items():
        temps[sid]["source"]["topic"] = topic
    d = tmp_path / name.replace(".yaml", "")
    d.mkdir(parents=True, exist_ok=True)
    out = d / "state.yaml"
    with open(out, "w", encoding="utf-8") as f:
        yaml.safe_dump(doc, f, allow_unicode=True, sort_keys=False)
    return out


@pytest.fixture
def main_map(tmp_path):
    return ZoneMap.from_state_yaml(write_layout(tmp_path, MAIN_LAYOUT, "main.yaml"))


@pytest.fixture
def swapped_map(tmp_path):
    return ZoneMap.from_state_yaml(write_layout(tmp_path, SWAPPED_LAYOUT, "swapped.yaml"))


def full_doc():
    """A valid input document with something set in every section."""
    d = default_rails()
    del d["revision"], d["zone_fingerprints"]          # input need not carry meta
    d["general"]["arm_hours"] = 4
    d["heaters"]["zones"]["top"] = {"setpoint_min_k": 150, "setpoint_max_k": 200,
                                    "max_step_k": 5, "max_rate_k_per_min": 1.0,
                                    "pv_sources": ["cube", "clamp"]}
    d["heaters"]["zones"]["bottom"] = {"setpoint_min_k": 155.5, "setpoint_max_k": 205,
                                       "max_step_k": None, "max_rate_k_per_min": 0.5,
                                       "pv_sources": ["clamp"]}
    d["heaters"]["zones"]["nozzle"] = {"setpoint_min_k": 150, "setpoint_max_k": 210,
                                       "max_step_k": 2.5, "max_rate_k_per_min": None,
                                       "pv_sources": ["cube"]}
    d["heaters"]["supply"]["1"] = {"voltage_min_v": 5, "voltage_max_v": 24,
                                   "current_min_a": 0.5, "current_max_a": 1.5,
                                   "may_switch_on": True, "may_switch_off": False}
    d["ln2"]["vessels"]["cryostat"] = {"may_operate": True, "max_open_minutes": 30,
                                       "may_set_auto_open": True, "may_disarm_auto_close": False}
    d["pneumatic"]["valves"]["ballast"] = {"may_open": True, "may_close": False}
    d["pneumatic"]["valves"]["pump"] = {"may_open": False, "may_close": True}
    d["pneumatic"]["forbid_open_together"] = [["pump", "bottle"], ["pump", "ballast"]]
    d["pneumatic"]["min_interval_s"] = 15
    return d


def put(doc, path, value):
    node = doc
    for k in path[:-1]:
        node = node[k]
    node[path[-1]] = value
    return doc


def drop(doc, path):
    node = doc
    for k in path[:-1]:
        node = node[k]
    del node[path[-1]]
    return doc


def errors_for(path, value):
    return validate_rails(put(full_doc(), path, value))


# ──────────────────────────────────────────────────────────────────────────
# validate_rails — valid documents
# ──────────────────────────────────────────────────────────────────────────

def test_default_rails_are_valid_and_allow_nothing():
    assert validate_rails(DEFAULT_RAILS, stored=True) == []
    d = default_rails()
    assert d["general"] == {"arm_hours": 2, "write_budget": 30}
    assert d["heaters"]["pv_cross_check_k"] is None
    assert d["pneumatic"]["min_interval_s"] == 10
    assert d["pneumatic"]["forbid_open_together"] == []
    for z in ("top", "bottom", "nozzle"):
        zr = d["heaters"]["zones"][z]
        assert zr["setpoint_min_k"] is None and zr["setpoint_max_k"] is None
        assert zr["pv_sources"] == []
    for ch in ("1", "2", "3"):
        s = d["heaters"]["supply"][ch]
        assert s["voltage_min_v"] is None and s["current_max_a"] is None
        assert s["may_switch_on"] is False and s["may_switch_off"] is False
    for v in ("cryostat", "primary_xe", "ballast"):
        assert d["ln2"]["vessels"][v] == {"may_operate": False, "max_open_minutes": None,
                                          "may_set_auto_open": False,
                                          "may_disarm_auto_close": False}
    for v in ("ballast", "pump", "bottle"):
        assert d["pneumatic"]["valves"][v] == {"may_open": False, "may_close": False}
    # The pressure guard denies by default: no limits, no thresholds, no
    # pump together with ballast or bottle.
    g = d["guard"]
    assert g["p_xe_cube"] == g["p_primary_bottle"] == {"warn_mbar": None, "trip_mbar": None}
    assert g["fresh_s"] is None and g["blind_disarm_s"] is None
    assert g["xenon_present_mbar"] is None and g["pump_open_max_mbar"] is None
    assert g["pump_open_with"] == []


def test_default_rails_copy_is_independent():
    d = default_rails()
    d["heaters"]["zones"]["top"]["pv_sources"].append("cube")
    assert DEFAULT_RAILS["heaters"]["zones"]["top"]["pv_sources"] == []


def test_full_document_is_valid():
    assert validate_rails(full_doc()) == []


@pytest.mark.parametrize("path, value", [
    (("general", "arm_hours"), 12),
    (("general", "arm_hours"), 0.25),
    (("general", "write_budget"), 1),
    (("general", "write_budget"), 1000),
    (("heaters", "pv_cross_check_k"), 50),
    (("heaters", "pv_cross_check_k"), None),
    (("guard", "p_xe_cube"), {"warn_mbar": 1200, "trip_mbar": 1400}),
    (("guard", "p_xe_cube"), {"warn_mbar": None, "trip_mbar": None}),
    (("guard", "fresh_s"), 300),
    (("guard", "blind_disarm_s"), 3600),
    (("guard", "xenon_present_mbar"), 10000),
    (("guard", "pump_open_max_mbar"), 2000),
    (("guard", "pump_open_with"), ["bottle", "ballast"]),
    (("heaters", "zones", "top", "setpoint_max_k"), 400),
    (("heaters", "zones", "top", "pv_sources"), []),
    (("heaters", "zones", "top", "pv_sources"), ["clamp", "cube"]),
    (("heaters", "supply", "1", "voltage_min_v"), 24),          # min == max allowed
    (("heaters", "supply", "1", "current_max_a"), 3),
    (("ln2", "vessels", "cryostat", "max_open_minutes"), 120),
    (("ln2", "vessels", "cryostat", "max_open_minutes"), None),
    (("pneumatic", "min_interval_s"), 2),
    (("pneumatic", "min_interval_s"), 3600.5),
    (("pneumatic", "forbid_open_together"), []),
])
def test_boundary_values_accepted(path, value):
    assert errors_for(path, value) == []


def test_meta_keys_optional_in_input_required_when_stored():
    doc = full_doc()
    assert validate_rails(doc) == []
    errs = validate_rails(doc, stored=True)
    assert any("missing key \"revision\"" in e for e in errs)
    assert any("missing key \"zone_fingerprints\"" in e for e in errs)
    doc.update(revision=3, zone_fingerprints={"top": "a", "bottom": "b", "nozzle": "c"},
               updated_utc="2026-10-22T00:00:00Z", updated_by="control page")
    assert validate_rails(doc, stored=True) == []
    assert validate_rails(doc) == []


# ──────────────────────────────────────────────────────────────────────────
# validate_rails — every field's type / range rule
# ──────────────────────────────────────────────────────────────────────────

NOT_NUMBERS = [True, False, "5", [5], {"v": 5}, float("nan"), float("inf"), float("-inf"),
               10 ** 400]

NUMERIC_FIELDS = [
    ("general", "arm_hours"),
    ("heaters", "pv_cross_check_k"),
    ("guard", "p_xe_cube", "warn_mbar"),
    ("guard", "fresh_s"),
    ("guard", "blind_disarm_s"),
    ("guard", "xenon_present_mbar"),
    ("guard", "pump_open_max_mbar"),
    ("heaters", "zones", "top", "setpoint_min_k"),
    ("heaters", "zones", "top", "setpoint_max_k"),
    ("heaters", "zones", "top", "max_step_k"),
    ("heaters", "zones", "top", "max_rate_k_per_min"),
    ("heaters", "supply", "1", "voltage_min_v"),
    ("heaters", "supply", "1", "voltage_max_v"),
    ("heaters", "supply", "1", "current_min_a"),
    ("heaters", "supply", "1", "current_max_a"),
    ("ln2", "vessels", "cryostat", "max_open_minutes"),
    ("pneumatic", "min_interval_s"),
]


@pytest.mark.parametrize("path", NUMERIC_FIELDS, ids=lambda p: ".".join(p))
@pytest.mark.parametrize("value", NOT_NUMBERS, ids=repr)
def test_numbers_must_be_finite_and_not_bool(path, value):
    errs = errors_for(path, value)
    where = ".".join(path)
    assert any(e.startswith(where) and "finite number" in e for e in errs), errs


BOOL_FIELDS = [
    ("heaters", "supply", "2", "may_switch_on"),
    ("heaters", "supply", "2", "may_switch_off"),
    ("ln2", "vessels", "ballast", "may_operate"),
    ("ln2", "vessels", "ballast", "may_set_auto_open"),
    ("ln2", "vessels", "ballast", "may_disarm_auto_close"),
    ("pneumatic", "valves", "bottle", "may_open"),
    ("pneumatic", "valves", "bottle", "may_close"),
]


@pytest.mark.parametrize("path", BOOL_FIELDS, ids=lambda p: ".".join(p))
@pytest.mark.parametrize("value", [0, 1, "true", "false", None, 1.0], ids=repr)
def test_permissions_must_be_json_bools(path, value):
    errs = errors_for(path, value)
    assert any(e.startswith(".".join(path)) and "true or false" in e for e in errs), errs


@pytest.mark.parametrize("path, value, expect", [
    (("general", "arm_hours"), 0, "> 0 and ≤ 12"),
    (("general", "arm_hours"), -1, "> 0 and ≤ 12"),
    (("general", "arm_hours"), 12.01, "> 0 and ≤ 12"),
    (("general", "arm_hours"), 168, "> 0 and ≤ 12"),
    (("general", "arm_hours"), None, "must be a number (got null)"),
    (("general", "write_budget"), 0, "whole number from 1 to 1000"),
    (("general", "write_budget"), 1001, "whole number from 1 to 1000"),
    (("general", "write_budget"), 2.5, "whole number from 1 to 1000"),
    (("general", "write_budget"), True, "whole number from 1 to 1000"),
    (("general", "write_budget"), None, "whole number from 1 to 1000"),
    (("heaters", "pv_cross_check_k"), 0, "> 0 and ≤ 50 K"),
    (("heaters", "pv_cross_check_k"), 51, "> 0 and ≤ 50 K"),
    (("guard", "p_xe_cube"), {"warn_mbar": 1400, "trip_mbar": 1200}, "must be below"),
    (("guard", "p_xe_cube"), {"warn_mbar": 1400, "trip_mbar": None}, "set both"),
    (("guard", "p_primary_bottle"), {"warn_mbar": 0, "trip_mbar": 10}, "> 0 and ≤ 20000"),
    (("guard", "p_primary_bottle"), {"warn_mbar": 10, "trip_mbar": 20001}, "> 0 and ≤ 20000"),
    (("guard", "p_xe_cube"), {"warn_mbar": 1, "trip_mbar": 2, "x": 3}, "unknown key"),
    (("guard", "fresh_s"), 301, "> 0 and ≤ 300 s"),
    (("guard", "blind_disarm_s"), 0, "> 0 and ≤ 3600 s"),
    (("guard", "xenon_present_mbar"), 10001, "> 0 and ≤ 10000"),
    (("guard", "pump_open_max_mbar"), 2001, "> 0 and ≤ 2000"),
    (("guard", "pump_open_with"), ["pump"], "unknown valve"),
    (("guard", "pump_open_with"), ["ballast", "ballast"], "listed twice"),
    (("guard", "pump_open_with"), "ballast", "must be a list"),
    (("heaters", "zones", "top", "setpoint_min_k"), 0, "> 0 and ≤ 400"),
    (("heaters", "zones", "top", "setpoint_min_k"), -5, "> 0 and ≤ 400"),
    (("heaters", "zones", "top", "setpoint_max_k"), 400.001, "> 0 and ≤ 400"),
    (("heaters", "zones", "top", "max_step_k"), 0, "> 0 K"),
    (("heaters", "zones", "top", "max_step_k"), -2, "> 0 K"),
    (("heaters", "zones", "top", "max_rate_k_per_min"), 0, "> 0 K/min"),
    (("heaters", "zones", "top", "max_rate_k_per_min"), -0.1, "> 0 K/min"),
    (("heaters", "supply", "1", "voltage_min_v"), -0.01, "≥ 0 and ≤ 32"),
    (("heaters", "supply", "1", "voltage_max_v"), 32.5, "≥ 0 and ≤ 32"),
    (("heaters", "supply", "1", "current_min_a"), -1, "≥ 0 and ≤ 3"),
    (("heaters", "supply", "1", "current_max_a"), 3.01, "≥ 0 and ≤ 3"),
    (("ln2", "vessels", "cryostat", "max_open_minutes"), 0, "> 0 and ≤ 120"),
    (("ln2", "vessels", "cryostat", "max_open_minutes"), 121, "> 0 and ≤ 120"),
    (("pneumatic", "min_interval_s"), 1.99, "≥ 2"),
    (("pneumatic", "min_interval_s"), 0, "≥ 2"),
    (("pneumatic", "min_interval_s"), None, "must be a number (got null)"),
])
def test_range_rules(path, value, expect):
    errs = errors_for(path, value)
    assert any(e.startswith(".".join(path)) and expect in e for e in errs), errs


@pytest.mark.parametrize("lo, hi", [(200, 200), (210, 200)])
def test_setpoint_min_must_be_below_max(lo, hi):
    doc = full_doc()
    doc["heaters"]["zones"]["top"].update(setpoint_min_k=lo, setpoint_max_k=hi)
    errs = validate_rails(doc)
    assert any("setpoint_min_k must be below setpoint_max_k" in e for e in errs), errs


@pytest.mark.parametrize("kind", ["voltage", "current"])
def test_supply_min_must_not_exceed_max(kind):
    unit = "v" if kind == "voltage" else "a"
    doc = full_doc()
    s = doc["heaters"]["supply"]["1"]
    s[f"{kind}_min_{unit}"], s[f"{kind}_max_{unit}"] = (25, 24) if kind == "voltage" else (2, 1.5)
    errs = validate_rails(doc)
    assert any(f"{kind}_min_{unit} must not exceed {kind}_max_{unit}" in e for e in errs), errs


@pytest.mark.parametrize("section, lo_key, hi_key", [
    (("heaters", "zones", "top"), "setpoint_min_k", "setpoint_max_k"),
    (("heaters", "supply", "1"), "voltage_min_v", "voltage_max_v"),
    (("heaters", "supply", "1"), "current_min_a", "current_max_a"),
])
@pytest.mark.parametrize("which", ["lo", "hi"])
def test_limit_pairs_are_both_or_neither(section, lo_key, hi_key, which):
    doc = full_doc()
    node = doc
    for k in section:
        node = node[k]
    node[lo_key if which == "lo" else hi_key] = None
    errs = validate_rails(doc)
    assert any(f"set both {lo_key} and {hi_key}, or neither" in e for e in errs), errs
    node[lo_key] = node[hi_key] = None
    assert validate_rails(doc) == []


@pytest.mark.parametrize("zone, value, expect", [
    ("top", "cube", "must be a list"),
    ("top", None, "must be a list"),
    ("top", ["cube", "cube"], "listed twice"),
    ("top", ["pv"], "unknown source"),
    ("top", [1], "unknown source"),
    ("top", ["rtd5"], "unknown source"),
    ("nozzle", ["clamp"], "nozzle has no clamp sensor"),
    ("nozzle", ["cube", "clamp"], "nozzle has no clamp sensor"),
])
def test_pv_sources_rules(zone, value, expect):
    errs = errors_for(("heaters", "zones", zone, "pv_sources"), value)
    assert any(expect in e for e in errs), errs


@pytest.mark.parametrize("value, expect", [
    ("pump+bottle", "must be a list"),
    ({"pump": "bottle"}, "must be a list"),
    ([["pump"]], "must be a pair"),
    ([["pump", "bottle", "ballast"]], "must be a pair"),
    (["pump", "bottle"], "must be a pair"),
    ([["pump", "pump"]], "two different valves"),
    ([["pump", "vent"]], "unknown valve"),
    ([["pump", 1]], "unknown valve"),
    ([["pump", None]], "unknown valve"),
])
def test_forbid_pairs_must_be_two_distinct_known_valves(value, expect):
    errs = errors_for(("pneumatic", "forbid_open_together"), value)
    assert any(expect in e for e in errs), errs


@pytest.mark.parametrize("value", [1, 2, "3", True, 3.0, 4, None])
def test_version_must_be_3(value):
    """Version 2 documents (before the guard and the leases) are refused."""
    errs = errors_for(("version",), value)
    assert any(e.startswith("rails.version: must be 3") for e in errs), errs


def test_a_version_2_file_leaves_nothing_writable(tmp_path, main_map):
    old = full_doc()
    old["version"] = 2
    del old["guard"], old["heaters"]["pv_cross_check_k"], old["general"]["write_budget"]
    old.update(revision=4, zone_fingerprints=main_map.fingerprints())
    path = tmp_path / "rails.json"
    path.write_text(json.dumps(old), encoding="utf-8")
    store = RailsStore(path, main_map)
    store.load()
    assert store.error and "rails.version: must be 3" in store.error
    assert store.revision == 4 and nothing_writable(store)


@pytest.mark.parametrize("value", [True, -1, 1.5, "3"])
def test_revision_type_when_present(value):
    errs = errors_for(("revision",), value)
    assert any("rails.revision: must be a non-negative integer" in e for e in errs), errs


def test_zone_fingerprints_shape_when_present():
    assert any("zone_fingerprints" in e for e in errors_for(("zone_fingerprints",), "x"))
    assert any("zone_fingerprints.top: must be a string" in e
               for e in errors_for(("zone_fingerprints",),
                                   {"top": 1, "bottom": "b", "nozzle": "c"}))
    assert any("unknown key \"middle\"" in e
               for e in errors_for(("zone_fingerprints",),
                                   {"top": "a", "bottom": "b", "nozzle": "c", "middle": "d"}))


@pytest.mark.parametrize("path", [
    (), ("general",), ("heaters",), ("heaters", "zones"), ("heaters", "zones", "top"),
    ("heaters", "supply"), ("heaters", "supply", "1"), ("ln2",), ("ln2", "vessels"),
    ("ln2", "vessels", "cryostat"), ("pneumatic",), ("pneumatic", "valves"),
    ("pneumatic", "valves", "pump"),
], ids=lambda p: ".".join(p) or "top")
def test_unknown_keys_rejected_at_every_level(path):
    doc = full_doc()
    node = doc
    for k in path:
        node = node[k]
    node["sneaky"] = 1
    errs = validate_rails(doc)
    assert any("unknown key \"sneaky\"" in e for e in errs), errs


@pytest.mark.parametrize("path, extra_key", [
    (("heaters", "zones"), "middle"),
    (("heaters", "supply"), "4"),
    (("ln2", "vessels"), "dewar"),
    (("pneumatic", "valves"), "vent"),
])
def test_unknown_items_rejected(path, extra_key):
    doc = full_doc()
    node = doc
    for k in path:
        node = node[k]
    node[extra_key] = copy.deepcopy(next(iter(node.values())))
    errs = validate_rails(doc)
    assert any(f"unknown key \"{extra_key}\"" in e for e in errs), errs


@pytest.mark.parametrize("path", [
    ("version",), ("general",), ("heaters",), ("ln2",), ("pneumatic",),
    ("general", "arm_hours"), ("heaters", "zones"), ("heaters", "supply"),
    ("heaters", "zones", "nozzle"), ("heaters", "zones", "top", "pv_sources"),
    ("heaters", "zones", "top", "max_step_k"), ("heaters", "supply", "3"),
    ("heaters", "supply", "1", "may_switch_off"), ("ln2", "vessels"),
    ("ln2", "vessels", "ballast"), ("ln2", "vessels", "cryostat", "max_open_minutes"),
    ("pneumatic", "valves"), ("pneumatic", "valves", "bottle"),
    ("pneumatic", "valves", "pump", "may_close"), ("pneumatic", "forbid_open_together"),
    ("pneumatic", "min_interval_s"),
], ids=lambda p: ".".join(p))
def test_missing_keys_rejected(path):
    errs = validate_rails(drop(full_doc(), path))
    assert any(f"missing key \"{path[-1]}\"" in e for e in errs), errs


@pytest.mark.parametrize("path", [
    ("general",), ("heaters",), ("heaters", "zones"), ("heaters", "zones", "top"),
    ("heaters", "supply", "1"), ("ln2", "vessels"), ("ln2", "vessels", "primary_xe"),
    ("pneumatic",), ("pneumatic", "valves", "ballast"),
], ids=lambda p: ".".join(p))
@pytest.mark.parametrize("value", [None, [], "x", 3])
def test_sections_must_be_objects(path, value):
    errs = errors_for(path, value)
    assert any("must be an object" in e for e in errs), errs


@pytest.mark.parametrize("doc", [None, [], "rails", 2, True])
def test_document_must_be_an_object(doc):
    assert validate_rails(doc) and "must be an object" in validate_rails(doc)[0]


def test_errors_are_readable_and_complete():
    doc = full_doc()
    doc["general"]["arm_hours"] = 0
    doc["heaters"]["zones"]["nozzle"]["pv_sources"] = ["clamp"]
    doc["pneumatic"]["forbid_open_together"] = [["pump", "pump"]]
    errs = validate_rails(doc)
    assert len(errs) == 3
    assert all(isinstance(e, str) and ":" in e for e in errs)


# ──────────────────────────────────────────────────────────────────────────
# ZoneMap — content detection
# ──────────────────────────────────────────────────────────────────────────

def test_zone_map_from_the_real_registry():
    """main (2d20373): software top = clamp plc/rtd/5 & cube labjack/rtd/2 →
    physically BOTTOM.  Once claude/swap-top-bottom merges the registry says
    the opposite and software top is physically TOP; both are spelled out."""
    with open(STATE_YAML, encoding="utf-8") as f:
        doc = yaml.safe_load(f)
    top_clamp = doc["groups"]["temperature"]["t_top_clamp"]["source"]["topic"]
    zm = ZoneMap.from_state_yaml(STATE_YAML)
    assert zm.errors == []
    top, bottom, nozzle = zm.zones["top"], zm.zones["bottom"], zm.zones["nozzle"]
    if top_clamp == CLAMP.format(5):                    # main layout
        assert top["clamp_alias"] == "rtd5" and top["cube_alias"] == "rtd2"
        assert top["physical"] == "bottom"
        assert bottom["clamp_alias"] == "rtd6" and bottom["cube_alias"] == "rtd3"
        assert bottom["physical"] == "top"
        assert zm.fingerprint("top") == f"cube={CUBE.format(2)}|clamp={CLAMP.format(5)}"
        assert zm.describe("top").endswith("— physically BOTTOM")
        assert "clamp PLC RTD5 (rtd5)" in zm.describe("top")
        assert "cube LJ RTD2 (rtd2)" in zm.describe("top")
        assert top["pid_block"] == 1 and top["pid_df"] == 100       # drivers/plc.py _PID_BLOCKS
        assert "PID block 1 (DF100)" in zm.describe("top")
        assert zm.channels["1"]["zone"] == "top"
    elif top_clamp == CLAMP.format(6):                  # swapped layout
        assert top["clamp_alias"] == "rtd6" and top["cube_alias"] == "rtd3"
        assert top["physical"] == "top" and bottom["physical"] == "bottom"
        assert zm.describe("top").endswith("— physically TOP")
    else:
        pytest.fail(f"unexpected t_top_clamp source {top_clamp}")

    assert top["status_topic"] == "xsphere/status/pid/top"
    assert top["cube_state"] == "t_cube_top" and top["clamp_state"] == "t_top_clamp"
    assert zm.sensor_state("top", "clamp") == "t_top_clamp"
    assert zm.sensor_state("bottom", "cube") == "t_cube_bottom"
    assert nozzle["cube_alias"] == "rtd1" and nozzle["physical"] == "nozzle"
    assert nozzle["clamp_alias"] is None and nozzle["clamp_state"] is None
    assert zm.alias("nozzle", "clamp") is None and zm.sensor_state("nozzle", "clamp") is None
    assert zm.alias("nozzle", "cube") == "rtd1"
    assert zm.alias("top", "pv") is None
    assert zm.fingerprint("nozzle") == f"cube={CUBE.format(1)}|clamp=-"
    assert zm.describe("nozzle").endswith("— physically NOZZLE")
    assert "no clamp sensor" in zm.describe("nozzle")
    assert re.match(r"Zone \d \(top\)", top["pid_label"])

    assert set(zm.channels) == {"1", "2", "3"}
    assert zm.channels["3"]["label"].startswith("NGE ch 3")
    assert zm.channels["3"]["zone"] == "nozzle"
    assert zm.vessels["cryostat"]["xv"] == "XV3"
    assert zm.vessels["primary_xe"]["xv"] == "XV2"
    assert zm.vessels["ballast"]["xv"] == "XV1"
    assert zm.vessels["cryostat"]["level_state"] == "level_cryostat_probe"
    assert zm.vessels["cryostat"]["esp32_state"] is None
    assert zm.vessels["ballast"]["level_state"] == "level_ballast"
    assert zm.vessels["ballast"]["esp32_state"] == "ghs_esp32_alive"
    assert zm.vessels["primary_xe"]["esp32_state"] == "ghs_esp32_alive"
    assert zm.valves["pump"]["normally"] == "closed"
    assert zm.valves["ballast"]["normally"] == "open"
    assert zm.valves["bottle"]["status_topic"] == "xsphere/status/pneumatic/bottle"
    assert "LN₂" in zm.vessels["cryostat"]["label"]          # read as UTF-8
    assert ZoneMap.VESSELS == ("cryostat", "primary_xe", "ballast")
    assert ZoneMap.VALVES == ("ballast", "pump", "bottle")
    assert ZoneMap.CHANNELS == ("1", "2", "3")


def test_zone_map_main_layout(main_map):
    assert main_map.zones["top"]["physical"] == "bottom"
    assert main_map.zones["bottom"]["physical"] == "top"
    assert main_map.alias("top", "clamp") == "rtd5"
    assert main_map.alias("top", "cube") == "rtd2"
    assert "physically BOTTOM" in main_map.describe("top")
    assert main_map.zones["top"]["pid_block"] is None          # no plc.py next to the copy
    assert main_map.errors == []


def test_zone_map_swapped_layout(swapped_map, main_map):
    zm = swapped_map
    assert zm.zones["top"]["physical"] == "top"
    assert zm.zones["bottom"]["physical"] == "bottom"
    assert zm.alias("top", "clamp") == "rtd6" and zm.alias("top", "cube") == "rtd3"
    assert zm.alias("bottom", "clamp") == "rtd5" and zm.alias("bottom", "cube") == "rtd2"
    assert zm.describe("top").endswith("— physically TOP")
    assert zm.describe("bottom").endswith("— physically BOTTOM")
    # The swap changes the top/bottom fingerprints, not the nozzle's.
    assert zm.fingerprint("top") != main_map.fingerprint("top")
    assert zm.fingerprint("bottom") != main_map.fingerprint("bottom")
    assert zm.fingerprint("nozzle") == main_map.fingerprint("nozzle")
    assert zm.fingerprint("top") == main_map.fingerprint("bottom")


def test_zone_map_conflicting_sensors_are_unknown(tmp_path):
    layout = dict(MAIN_LAYOUT, t_cube_top=CUBE.format(3))     # cube says top, clamp says bottom
    zm = ZoneMap.from_state_yaml(write_layout(tmp_path, layout))
    assert zm.zones["top"]["physical"] == "unknown"
    assert "physical position UNKNOWN" in zm.describe("top")
    assert any("zone top" in e for e in zm.errors)


def test_zone_map_unrecognised_source(tmp_path):
    layout = dict(MAIN_LAYOUT, t_cube_top="xsphere/sensors/temperature/labjack_cal/rtd/2")
    zm = ZoneMap.from_state_yaml(write_layout(tmp_path, layout))
    assert zm.alias("top", "cube") is None
    assert zm.zones["top"]["physical"] == "bottom"           # the clamp still says where
    assert any("t_cube_top" in e for e in zm.errors)
    assert "cube sensor not recognised" in zm.describe("top")


# ──────────────────────────────────────────────────────────────────────────
# RailsStore
# ──────────────────────────────────────────────────────────────────────────

def nothing_writable(store):
    r = store.get()
    for z in ("top", "bottom", "nozzle"):
        assert store.zone_limits(z) is None
        assert r["heaters"]["zones"][z]["pv_sources"] == []
    for ch in ("1", "2", "3"):
        c = store.channel(ch)
        assert c["voltage_max_v"] is None and c["current_max_a"] is None
        assert not c["may_switch_on"] and not c["may_switch_off"]
    for v in ("cryostat", "primary_xe", "ballast"):
        assert not store.vessel(v)["may_operate"]
    for v in ("ballast", "pump", "bottle"):
        assert store.valve(v) == {"may_open": False, "may_close": False}
    return True


def test_missing_file_gives_defaults(tmp_path, main_map):
    store = RailsStore(tmp_path / "rails.json", main_map)
    store.load()
    assert store.error is None
    assert store.revision == 0
    assert store.stale_zones == []
    assert store.updated_utc is None and store.updated_by is None
    assert nothing_writable(store)
    assert store.stored()["zone_fingerprints"] == main_map.fingerprints()
    assert validate_rails(store.stored(), stored=True) == []


@pytest.mark.parametrize("content", [
    "{not json",
    "",
    "[]",
    '{"version": 2, "general": {"arm_hours": NaN}}',
    "\x00\x01",
])
def test_unparseable_file_denies_everything(tmp_path, main_map, content):
    path = tmp_path / "rails.json"
    path.write_text(content, encoding="utf-8")
    store = RailsStore(path, main_map)
    store.load()
    assert store.error and store.error.startswith(
        "rails file invalid — nothing writable until saved again")
    assert nothing_writable(store)


def test_schema_invalid_file_denies_everything_and_keeps_revision(tmp_path, main_map):
    path = tmp_path / "rails.json"
    doc = full_doc()
    doc.update(revision=7, zone_fingerprints=main_map.fingerprints())
    doc["heaters"]["zones"]["top"]["setpoint_max_k"] = "200"
    path.write_text(json.dumps(doc), encoding="utf-8")
    store = RailsStore(path, main_map)
    store.load()
    assert "rails file invalid" in store.error
    assert "heaters.zones.top.setpoint_max_k" in store.error
    assert store.revision == 7
    assert nothing_writable(store)
    # Saving again (against the revision shown) is the way out.
    ok, err, _ = store.replace(full_doc(), 7, "Lucas")
    assert ok and err is None
    assert store.error is None and store.revision == 8
    assert store.zone_limits("top") == (150.0, 200.0)


def test_file_missing_meta_is_invalid(tmp_path, main_map):
    path = tmp_path / "rails.json"
    path.write_text(json.dumps(full_doc()), encoding="utf-8")   # no revision / fingerprints
    store = RailsStore(path, main_map)
    store.load()
    assert store.error and "revision" in store.error
    assert nothing_writable(store)


def test_replace_saves_and_reloads(tmp_path, main_map):
    path = tmp_path / "sub" / "rails.json"                      # directory created on save
    t = [1792650000.0]
    store = RailsStore(path, main_map, clock=lambda: t[0])
    store.load()
    ok, err, diff = store.replace(full_doc(), 0, "control page")
    assert (ok, err) == (True, None)
    assert "heaters.zones.top.setpoint_max_k: null → 200" in diff
    assert "general.arm_hours: 2 → 4" in diff
    assert diff.endswith("… and 6 more")                    # long diffs are cut at 20
    assert store.revision == 1
    assert store.updated_by == "control page"
    assert store.updated_utc == "2026-10-22T06:20:00Z"
    assert store.zone_limits("top") == (150.0, 200.0)
    assert store.zone("top")["pv_sources"] == ["cube", "clamp"]
    assert store.channel(1)["voltage_max_v"] == 24 and store.channel("1")["may_switch_on"]
    assert store.vessel("cryostat")["max_open_minutes"] == 30
    assert store.valve("pump") == {"may_open": False, "may_close": True}
    assert store.pneumatic() == {"forbid_open_together": [["pump", "bottle"], ["pump", "ballast"]],
                                 "min_interval_s": 15}
    assert store.general() == {"arm_hours": 4, "write_budget": 30} and store.arm_hours == 4
    assert store.write_budget == 30 and store.pv_cross_check_k is None
    assert store.guard()["pump_open_with"] == []

    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert validate_rails(on_disk, stored=True) == []
    assert on_disk["revision"] == 1
    assert on_disk["zone_fingerprints"] == main_map.fingerprints()
    assert not (tmp_path / "sub" / "rails.json.tmp").exists()

    again = RailsStore(path, main_map)
    again.load()
    assert again.error is None and again.stale_zones == []
    assert again.stored() == store.stored()
    assert again.get() == store.get()


def test_replace_ignores_meta_in_input(tmp_path, main_map):
    store = RailsStore(tmp_path / "rails.json", main_map)
    store.load()
    doc = full_doc()
    doc.update(revision=99, zone_fingerprints={"top": "x", "bottom": "y", "nozzle": "z"},
               updated_by="someone else", updated_utc="1999-01-01T00:00:00Z")
    ok, err, _ = store.replace(doc, 0, "Lucas")
    assert ok, err
    s = store.stored()
    assert s["revision"] == 1
    assert s["zone_fingerprints"] == main_map.fingerprints()
    assert s["updated_by"] == "Lucas" and s["updated_utc"] != "1999-01-01T00:00:00Z"


def test_replace_needs_the_current_revision(tmp_path, main_map):
    path = tmp_path / "rails.json"
    store = RailsStore(path, main_map)
    store.load()
    assert store.replace(full_doc(), 0, "tab 1")[0]
    before = path.read_text(encoding="utf-8")
    doc = full_doc()
    doc["heaters"]["zones"]["top"]["setpoint_max_k"] = 300
    ok, err, diff = store.replace(doc, 0, "tab 2")                 # stale editor
    assert not ok and diff == ""
    assert err.startswith("rails changed since you opened the editor")
    assert store.revision == 1 and store.zone_limits("top") == (150.0, 200.0)
    assert path.read_text(encoding="utf-8") == before
    ok, err, _ = store.replace(doc, 1, "tab 2")
    assert ok and store.revision == 2 and store.zone_limits("top") == (150.0, 300.0)


@pytest.mark.parametrize("base", [True, False, "0", 0.0, None, [0]])
def test_base_revision_must_be_an_integer(tmp_path, main_map, base):
    store = RailsStore(tmp_path / "rails.json", main_map)
    store.load()
    ok, err, _ = store.replace(full_doc(), base, "x")
    assert not ok and "base_revision must be an integer" in err
    assert store.revision == 0


def test_invalid_document_refused_and_nothing_changes(tmp_path, main_map):
    path = tmp_path / "rails.json"
    store = RailsStore(path, main_map)
    store.load()
    assert store.replace(full_doc(), 0, "Lucas")[0]
    before = path.read_text(encoding="utf-8")
    bad = full_doc()
    bad["heaters"]["zones"]["nozzle"]["pv_sources"] = ["clamp"]
    bad["ln2"]["vessels"]["cryostat"]["max_open_minutes"] = 600
    ok, err, diff = store.replace(bad, 1, "Lucas")
    assert not ok and diff == ""
    assert "nozzle has no clamp sensor" in err and "max_open_minutes" in err
    assert path.read_text(encoding="utf-8") == before
    assert store.revision == 1 and store.error is None


def test_replace_with_no_changes_says_so(tmp_path, main_map):
    store = RailsStore(tmp_path / "rails.json", main_map)
    store.load()
    store.replace(full_doc(), 0, "a")
    ok, _, diff = store.replace(full_doc(), 1, "a")
    assert ok and diff == "no changes (rails re-confirmed)" and store.revision == 2


def test_by_is_sanitised(tmp_path, main_map):
    store = RailsStore(tmp_path / "rails.json", main_map)
    store.load()
    store.replace(full_doc(), 0, None)
    assert store.updated_by == "unknown"
    # Only a short plain name is kept; anything else is "unknown".
    store.replace(full_doc(), 1, "x" * 500)
    assert store.updated_by == "unknown"
    store.replace(full_doc(), 2, "Lucas\nWARNINGS: none")
    assert store.updated_by == "unknown"
    store.replace(full_doc(), 3, "  Lucas D.  ")
    assert store.updated_by == "Lucas D."


def test_write_is_atomic(tmp_path, main_map, monkeypatch):
    path = tmp_path / "rails.json"
    store = RailsStore(path, main_map)
    store.load()
    calls = []
    real_replace = os.replace

    def spy(src, dst):
        calls.append((Path(src).name, Path(dst).name, json.loads(Path(src).read_text("utf-8"))))
        return real_replace(src, dst)

    monkeypatch.setattr(rails_mod.os, "replace", spy)
    assert store.replace(full_doc(), 0, "Lucas")[0]
    assert calls and calls[0][:2] == ("rails.json.tmp", "rails.json")
    assert calls[0][2]["revision"] == 1                     # complete file before the rename
    before = path.read_text(encoding="utf-8")

    def boom(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(rails_mod.os, "replace", boom)
    doc = full_doc()
    doc["general"]["arm_hours"] = 2
    ok, err, _ = store.replace(doc, 1, "Lucas")
    assert not ok and "could not save the rails file" in err and "disk full" in err
    assert path.read_text(encoding="utf-8") == before        # old file intact
    assert not (tmp_path / "rails.json.tmp").exists()        # temp file cleaned up
    assert store.revision == 1 and store.arm_hours == 4      # in-memory rails unchanged


def test_fingerprint_mismatch_marks_zone_stale(tmp_path, main_map, swapped_map):
    path = tmp_path / "rails.json"
    saved = RailsStore(path, main_map)
    saved.load()
    assert saved.replace(full_doc(), 0, "Lucas")[0]

    # The registry is swapped (claude/swap-top-bottom deployed) under the rails.
    store = RailsStore(path, swapped_map)
    store.load()
    assert store.error is None
    assert store.stale_zones == ["top", "bottom"]
    eff = store.get()
    for z in ("top", "bottom"):
        assert eff["heaters"]["zones"][z] == {"setpoint_min_k": None, "setpoint_max_k": None,
                                              "max_step_k": None, "max_rate_k_per_min": None,
                                              "pv_sources": []}
        assert store.zone_limits(z) is None
    assert store.zone_limits("nozzle") == (150.0, 210.0)       # nozzle did not move
    assert store.channel("1")["voltage_max_v"] == 24          # supply keyed by channel
    assert store.vessel("cryostat")["may_operate"]
    # What was saved is still shown to the human for re-confirmation.
    assert store.stored()["heaters"]["zones"]["top"]["setpoint_max_k"] == 200

    # Re-saving against the new registry confirms them.
    content = {k: v for k, v in store.stored().items()}
    ok, err, diff = store.replace(content, store.revision, "Lucas")
    assert ok, err
    assert store.stale_zones == []
    assert store.zone_limits("top") == (150.0, 200.0)
    reread = RailsStore(path, swapped_map)
    reread.load()
    assert reread.stale_zones == []


def test_unset_zone_is_not_reported_stale(tmp_path, main_map, swapped_map):
    path = tmp_path / "rails.json"
    doc = full_doc()
    doc["heaters"]["zones"]["bottom"] = {"setpoint_min_k": None, "setpoint_max_k": None,
                                         "max_step_k": None, "max_rate_k_per_min": None,
                                         "pv_sources": []}
    s = RailsStore(path, main_map)
    s.load()
    assert s.replace(doc, 0, "Lucas")[0]
    store = RailsStore(path, swapped_map)
    store.load()
    assert store.stale_zones == ["top"]


def test_concurrent_replaces_one_wins(tmp_path, main_map):
    store = RailsStore(tmp_path / "rails.json", main_map)
    store.load()
    results = []
    barrier = threading.Barrier(8)

    def worker(i):
        doc = full_doc()
        doc["general"]["arm_hours"] = i + 1
        barrier.wait()
        results.append(store.replace(doc, 0, f"tab {i}"))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    assert sum(1 for ok, _, _ in results if ok) == 1
    assert store.revision == 1


def test_get_and_stored_are_copies(tmp_path, main_map):
    store = RailsStore(tmp_path / "rails.json", main_map)
    store.load()
    store.replace(full_doc(), 0, "Lucas")
    g = store.get()
    g["heaters"]["zones"]["top"]["setpoint_max_k"] = 400
    store.stored()["pneumatic"]["valves"]["pump"]["may_open"] = True
    store.zone("top")["pv_sources"].append("x")
    assert store.zone_limits("top") == (150.0, 200.0)
    assert store.valve("pump")["may_open"] is False
    assert store.zone("top")["pv_sources"] == ["cube", "clamp"]


def test_replace_from_command(tmp_path, main_map):
    store = RailsStore(tmp_path / "rails.json", main_map)
    store.load()
    assert store.replace_from_command([1]) == (False, "rails command must be a JSON object", "")
    ok, err, _ = store.replace_from_command({"rails": full_doc()})
    assert not ok and "base_revision" in err
    ok, err, _ = store.replace_from_command({"rails": full_doc(), "base_revision": 0, "x": 1})
    assert not ok and "unknown key" in err
    ok, err, _ = store.replace_from_command({"rails": full_doc(), "base_revision": 0},
                                            default_by="control page")
    assert ok and store.updated_by == "control page"
    ok, _, _ = store.replace_from_command({"rails": full_doc(), "base_revision": 1, "by": "Lucas"})
    assert ok and store.updated_by == "Lucas"
    # request_id: the editor's nonce, echoed by the service, not part of the rails.
    ok, err, _ = store.replace_from_command({"rails": full_doc(), "base_revision": 2,
                                             "request_id": "abc123"})
    assert ok and store.revision == 3 and "request_id" not in store.stored()
    for bad in (5, None, "x" * 65, ["a"]):
        ok, err, _ = store.replace_from_command({"rails": full_doc(), "base_revision": 3,
                                                 "request_id": bad})
        assert not ok and "request_id must be a string" in err
    assert store.revision == 3


@pytest.mark.parametrize("key", ["voltage_min_v", "current_min_a"])
def test_zero_supply_minimum_needs_may_switch_off(key):
    """0 V or a 0 A limit unpowers the heater just as "output off" does."""
    doc = full_doc()
    sr = doc["heaters"]["supply"]["1"]
    assert sr["may_switch_off"] is False
    sr[key] = 0
    errs = validate_rails(doc)
    assert len(errs) == 1 and f"heaters.supply.1.{key}" in errs[0]
    assert "unpower the heater" in errs[0] and "may_switch_off" in errs[0]
    sr["may_switch_off"] = True
    assert validate_rails(doc) == []
    sr[key] = 0.01
    sr["may_switch_off"] = False
    assert validate_rails(doc) == []


def test_zero_supply_minimum_in_a_saved_file_makes_it_invalid(tmp_path):
    """A rails file saved before this rule, with a 0 minimum and no
    may_switch_off, now loads as invalid: nothing writable (deny by default)."""
    doc = full_doc()
    doc["heaters"]["supply"]["1"]["voltage_min_v"] = 0
    doc.update(revision=4, zone_fingerprints={"top": "", "bottom": "", "nozzle": ""})
    (tmp_path / "rails.json").write_text(json.dumps(doc), encoding="utf-8")
    store = RailsStore(tmp_path / "rails.json")
    store.load()
    assert store.error and "voltage_min_v" in store.error
    assert store.channel("1")["voltage_min_v"] is None


def test_store_without_zone_map(tmp_path):
    store = RailsStore(tmp_path / "rails.json")
    store.load()
    assert store.replace(full_doc(), 0, "t")[0]
    again = RailsStore(tmp_path / "rails.json")
    again.load()
    assert again.stale_zones == [] and again.zone_limits("top") == (150.0, 200.0)


def test_rails_diff_lists_changes():
    a = default_rails()
    b = default_rails()
    b["heaters"]["zones"]["top"]["pv_sources"] = ["cube"]
    b["ln2"]["vessels"]["ballast"]["may_operate"] = True
    d = rails_diff(a, b)
    assert 'heaters.zones.top.pv_sources: [] → ["cube"]' in d
    assert "ln2.vessels.ballast.may_operate: false → true" in d
    assert d.count("→") == 2
    assert rails_diff(a, a) == "no changes (rails re-confirmed)"
    b["pneumatic"]["min_interval_s"] = 15
    assert "pneumatic.min_interval_s: 10 → 15" in rails_diff(a, b)
