"""Read-only Modbus TCP probe for the CLICK PLC.

Connects to the PLC, reads every register the slow-control driver
(`slowcontrol.drivers.plc`) uses, and prints the values. It writes nothing —
safe to run at any time, including against a live cryostat.

Use it to:
  * confirm Modbus TCP connectivity and the configured host/port/unit-id,
  * sanity-check the float word order (both interpretations are shown),
  * cross-check the register map against the CLICK programming software,
  * see current PID setpoints / PVs / outputs / gains and valve states
    before starting the full service.

Usage (from the repo root):
    python -m slowcontrol.tools.plc_probe                 # uses slowcontrol/config.yaml
    python -m slowcontrol.tools.plc_probe -c path/to/config.yaml
    python -m slowcontrol.tools.plc_probe --host 192.168.8.118
"""

from __future__ import annotations

import argparse
import struct
import sys
from typing import List, Optional

from pymodbus.client import ModbusTcpClient

from slowcontrol.core import config as cfg_mod
from slowcontrol.drivers import plc as P


# --------------------------------------------------------------------------
# Low-level helpers
# --------------------------------------------------------------------------

def _read_regs(client: ModbusTcpClient, addr: int, count: int, unit: int) -> Optional[List[int]]:
    """Read `count` holding registers, tolerating pymodbus signature changes."""
    try:
        rr = client.read_holding_registers(addr, count=count, slave=unit)
    except TypeError:
        rr = client.read_holding_registers(addr, count=count)
    if rr.isError():
        return None
    return list(rr.registers)


def _read_coil(client: ModbusTcpClient, addr: int, unit: int):
    try:
        rr = client.read_coils(addr, count=1, slave=unit)
    except TypeError:
        rr = client.read_coils(addr, count=1)
    if rr.isError():
        return None
    return bool(rr.bits[0])


def _decode_float(regs: Optional[List[int]], low_word_first: bool) -> Optional[float]:
    if regs is None:
        return None
    if low_word_first:
        raw = (regs[1] << 16) | regs[0]   # CLICK default — matches plc.py
    else:
        raw = (regs[0] << 16) | regs[1]
    return struct.unpack(">f", struct.pack(">I", raw))[0]


def _ds_name(addr: int) -> str:
    return f"DS{(addr - P.DS_BASE) + 1}"


def _df_name_for_addr(addr: int) -> str:
    n = (addr - P.DF_BASE) // 2 + 1
    return f"DF{n}"


# --------------------------------------------------------------------------
# Probe
# --------------------------------------------------------------------------

def probe(host: str, port: int, unit: int, timeout: float) -> int:
    client = ModbusTcpClient(host=host, port=port, timeout=timeout)
    if not client.connect():
        print(f"ERROR: could not open TCP connection to {host}:{port}", file=sys.stderr)
        return 1
    print(f"Connected to {host}:{port} (unit id {unit})\n")

    # quick liveness check on DF1
    if _read_regs(client, P._df(1), 2, unit) is None:
        print("WARNING: DF1 read failed — check the unit id / Modbus TCP server / register map.\n")

    def show_float(label: str, df_addr: int):
        regs = _read_regs(client, df_addr, 2, unit)
        lo = _decode_float(regs, low_word_first=True)
        hi = _decode_float(regs, low_word_first=False)
        rs = f"[{regs[0]:5d},{regs[1]:5d}]" if regs else "[  err  ]"
        los = f"{lo:14.4f}" if lo is not None else "      n/a     "
        his = f"{hi:14.4f}" if hi is not None else "      n/a"
        print(f"  {label:22s} {_df_name_for_addr(df_addr):7s} regs={rs}  "
              f"low-word-first={los}  high-word-first={his}")

    def show_int(label: str, ds_addr: int):
        regs = _read_regs(client, ds_addr, 1, unit)
        print(f"  {label:22s} {_ds_name(ds_addr):8s} = {regs[0] if regs else 'err'}")

    def show_coil(label: str, addr: int):
        v = _read_coil(client, addr, unit)
        print(f"  {label:22s} coil#{addr:<6d} = {v if v is not None else 'err'}")

    print("== RTD inputs (DF1..DF3) — labels per plc.py.REG_RTD ==")
    for key, addr in P.REG_RTD.items():
        show_float(key, addr)

    print("\n== LabJack temperatures mirrored into the PLC (written by the service, °C) ==")
    for ch, addr in getattr(P, "REG_LABJACK_RTD_WRITE", {}).items():
        show_float(f"labjack rtd {ch} (abs)", addr)
    for ch, addr in getattr(P, "REG_LABJACK_TC_WRITE", {}).items():
        show_float(f"labjack tc {ch} (gradient)", addr)

    print("\n== Liquid level (DF) ==")
    show_float("cryostat raw",      P.REG_LEVEL_RAW["cryostat"])
    show_float("cryostat filtered", P.REG_LEVEL_FILTERED["cryostat"])
    show_float("ballast filtered",  P.REG_LEVEL_FILTERED["ballast"])
    show_float("primary_xe filt.",  P.REG_LEVEL_FILTERED["primary_xe"])
    show_float("ballast raw (DF251, written by service)",   P.REG_LEVEL_WRITE["ballast"])
    show_float("primary_xe raw (DF252, written by service)", P.REG_LEVEL_WRITE["primary_xe"])

    print("\n== PID blocks (sp / pv in °C, output in %, gains raw) ==")
    for zone in ("top", "bottom", "nozzle"):
        print(f"  -- {zone}  (HTR, DF block {P._PID_BLOCKS[zone]}) --")
        for field in ("sp", "pv", "pv_raw", "output", "kp", "ki", "kd", "bias"):
            show_float(f"{zone}.{field}", P._pid_reg(zone, field))

    print("\n== Valve registers (DS, integer) ==")
    for key, addr in P.REG_VALVE.items():
        show_int(key, addr)

    print("\n== Output coils ==")
    for key, addr in P.REG_VALVE_COIL.items():
        show_coil(f"valve_{key} (output)", addr)
    for key, addr in P.REG_HTR_COIL.items():
        show_coil(f"htr_{key}_pwm", addr)

    client.close()
    print("\n(read-only probe complete — nothing was written)")
    return 0


def main() -> None:
    ap = argparse.ArgumentParser(description="Read-only Modbus probe of the CLICK PLC")
    ap.add_argument("-c", "--config", default="slowcontrol/config.yaml",
                    help="path to the slow-control config (for plc.host/port/unit_id/timeout)")
    ap.add_argument("--host", help="override plc.host from the config")
    ap.add_argument("--port", type=int, help="override plc.port (default 502)")
    ap.add_argument("--unit", type=int, help="override plc.unit_id (default 1)")
    ap.add_argument("--timeout", type=float, help="override plc.timeout (seconds)")
    args = ap.parse_args()

    host = args.host
    port = args.port
    unit = args.unit
    timeout = args.timeout
    if host is None or port is None or unit is None or timeout is None:
        try:
            cfg = cfg_mod.load(args.config)
            host = host if host is not None else cfg.plc.host
            port = port if port is not None else cfg.plc.port
            unit = unit if unit is not None else cfg.plc.unit_id
            timeout = timeout if timeout is not None else cfg.plc.timeout
        except Exception as exc:  # noqa: BLE001 - config is optional if --host given
            if host is None:
                ap.error(f"could not load config {args.config!r} ({exc}); pass --host explicitly")
            port = port or 502
            unit = unit or 1
            timeout = timeout or 3.0

    sys.exit(probe(host, port, unit, timeout))


if __name__ == "__main__":
    main()
