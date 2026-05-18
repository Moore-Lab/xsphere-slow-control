"""
One-shot AIN_EF configurator for the LabJack T7.

The T7 stores AIN_EF (extended-feature) configuration in RAM, so a power
cycle wipes it and the slow-control service then sees errors like

    Address 7320, LJM library error code 2583 AIN_EF_CHANNEL_INACTIVE

Run this tool against the T7 to re-apply the thermometry config defined in
slowcontrol/config.yaml:

  - RTD channels (kind: rtd)  → EF index 40 (PT100), 4-wire, °K output, ±0.1 V
  - TC  channels (kind: tc)   → EF index 22 (type-K), CJC = TEMPERATURE_DEVICE_K,
                                gain 1.0, offset 0.0, ±0.01 V

Pass --save to also call IO_CONFIG_SET_DEFAULT_TO_CURRENT so the settings
survive the next power cycle (written to flash). Without --save the config
is RAM-only.

The slow-control service must be stopped first — the T7 only allows one
TCP session at a time. Verify with:

    sudo systemctl stop xsphere-slowcontrol
    python -m slowcontrol.tools.labjack_setup --save
    sudo systemctl start xsphere-slowcontrol
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml

try:
    from labjack import ljm
except ImportError:
    sys.stderr.write("labjack-ljm is not installed (`pip install labjack-ljm`)\n")
    sys.exit(2)


# AIN_EF feature indices (see LabJack T-series datasheet, "Extended Features")
EF_INDEX_PT100 = 40        # 4-wire PT100, RTD curve, output in K
EF_INDEX_TYPEK = 22        # Type-K thermocouple, output in K

# Modbus address of TEMPERATURE_DEVICE_K — used as the cold-junction source
# for the type-K EF. Documented in the T-series datasheet (§ Thermocouples).
TEMPERATURE_DEVICE_K_ADDR = 60052


def _channel_writes(ch: dict) -> tuple[list[str], list[float]]:
    """Build (names, values) for ljm.eWriteNames for one channel.

    Layout, per the LabJack docs: configure the AIN first (RANGE, NEGATIVE_CH,
    RESOLUTION_INDEX, SETTLING), then EF_INDEX, then the EF_CONFIG_* params.
    EF_INDEX must be 0 (cleared) before reconfiguring the other EF fields, but
    LJM applies the writes in order, so writing EF_INDEX up-front is enough.
    """
    ain = int(ch["ain"])
    kind = str(ch["kind"]).lower()
    p = f"AIN{ain}"

    if kind == "rtd":
        names = [
            f"{p}_NEGATIVE_CH",      # paired with ain+1 (differential, 4-wire)
            f"{p}_RANGE",            # ±0.1 V
            f"{p}_RESOLUTION_INDEX", # 0 = default (highest the chip will offer)
            f"{p}_EF_INDEX",         # PT100
            f"{p}_EF_CONFIG_A",      # 0 = output in K
        ]
        values = [float(ain + 1), 0.1, 0.0, float(EF_INDEX_PT100), 0.0]
        return names, values

    if kind == "tc":
        names = [
            f"{p}_NEGATIVE_CH",      # differential pair (ain+1)
            f"{p}_RANGE",            # ±0.01 V (TC EMF is tens of mV at most)
            f"{p}_RESOLUTION_INDEX",
            f"{p}_EF_INDEX",         # type-K
            f"{p}_EF_CONFIG_A",      # 0 = K, 1 = C, 2 = F
            f"{p}_EF_CONFIG_B",      # CJC source register address
            f"{p}_EF_CONFIG_D",      # CJC gain   (1.0 → use TEMPERATURE_DEVICE_K verbatim)
            f"{p}_EF_CONFIG_E",      # CJC offset (0.0 → no bias)
        ]
        values = [
            float(ain + 1), 0.01, 0.0,
            float(EF_INDEX_TYPEK), 0.0,
            float(TEMPERATURE_DEVICE_K_ADDR), 1.0, 0.0,
        ]
        return names, values

    raise ValueError(f"unknown kind {kind!r} for channel {ch}")


def configure(handle: int, channels: list[dict]) -> list[tuple[dict, float]]:
    """Apply the AIN_EF config and return (channel, EF_READ_A) for each."""
    results: list[tuple[dict, float]] = []
    for ch in channels:
        names, values = _channel_writes(ch)
        ljm.eWriteNames(handle, len(names), names, values)
        read_a = ljm.eReadName(handle, f"AIN{int(ch['ain'])}_EF_READ_A")
        results.append((ch, read_a))
    return results


def main(argv: list[str] | None = None) -> int:
    here = Path(__file__).resolve().parent.parent  # slowcontrol/
    default_cfg = here / "config.yaml"

    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-c", "--config", default=str(default_cfg),
                    help=f"path to slow-control config.yaml (default: {default_cfg})")
    ap.add_argument("--save", action="store_true",
                    help="persist the new config to T7 flash "
                         "(survives power cycle)")
    args = ap.parse_args(argv)

    with open(args.config) as fh:
        cfg = yaml.safe_load(fh) or {}
    lj = (cfg.get("labjack") or {})
    channels = lj.get("thermometry_channels") or []
    if not channels:
        sys.stderr.write(f"no labjack.thermometry_channels in {args.config}\n")
        return 1

    conn_type = lj.get("connection_type", "ETHERNET")
    dev_id    = lj.get("device_identifier", "ANY")

    print(f"opening T7 over {conn_type} @ {dev_id} …")
    handle = ljm.openS("T7", conn_type, dev_id)
    try:
        serial = int(ljm.eReadName(handle, "SERIAL_NUMBER"))
        print(f"connected to T7 serial={serial}")

        results = configure(handle, channels)

        print()
        print(f"{'name':<6} {'ain':>3} {'kind':<4} {'label':<22} EF_READ_A")
        print("-" * 60)
        for ch, read_a in results:
            unit = "K" if str(ch["kind"]).lower() in ("rtd", "tc") else ""
            print(f"{ch.get('name',''):<6} {int(ch['ain']):>3} "
                  f"{str(ch['kind']).lower():<4} {ch.get('label',''):<22} "
                  f"{read_a:>10.3f} {unit}")

        if args.save:
            print()
            print("persisting current config to flash (IO_CONFIG_SET_DEFAULT_TO_CURRENT) …")
            ljm.eWriteName(handle, "IO_CONFIG_SET_DEFAULT_TO_CURRENT", 1.0)
            print("saved — config will survive a power cycle")
        else:
            print()
            print("(--save not given: config is RAM-only; another power cycle "
                  "will wipe it)")
    finally:
        ljm.close(handle)

    return 0


if __name__ == "__main__":
    sys.exit(main())
