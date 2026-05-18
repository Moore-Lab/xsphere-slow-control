"""Live watcher for one CLICK PID block's coils and float registers.

Reads C100-C139 (the 40 PID coils) and DF100-DF124 (the 25 PID float
registers) for the chosen PID block every ~0.5 s and prints any value
that changed since the previous tick. Read-only — never writes.

Use this to discover which coil/register the CLICK PID instruction
actually uses for a given function. Toggle Auto/Manual (or autotune,
or whatever) in the PID Monitor of the CLICK Programming Software
while the watcher is running and read off which addresses move.

The slow-control service must be stopped first so the Modbus TCP port
is free (the CLICK only allows one Modbus session). The Programming
Software's connection is on a *different* TCP port (25425), so leaving
it open is fine — the two coexist.

Usage:
    sudo systemctl stop xsphere-slowcontrol
    python -m slowcontrol.tools.pid_watch              # default: PID1 (top)
    python -m slowcontrol.tools.pid_watch --zone bottom
    python -m slowcontrol.tools.pid_watch --zone nozzle
    sudo systemctl start xsphere-slowcontrol
"""

from __future__ import annotations

import argparse
import struct
import sys
import time
from datetime import datetime
from pathlib import Path

import yaml
from pymodbus.client import ModbusTcpClient


# CLICK PLC: DF1 -> Modbus 28672/28673, DS1 -> Modbus 0, C1 -> Modbus coil 0
DF_BASE = 28672
DS_BASE = 0
C_BASE = 0


# Block bases for each PID instruction's auto-allocated address ranges.
# CLICK Plus PID auto-assigns 40 coils (C), 15 integers (DS) and 25 floats (DF).
PID_BLOCKS = {
    "top":    {"df": 100, "ds": 100, "c": 100},   # PID1
    "bottom": {"df": 125, "ds": 115, "c": 140},   # PID2
    "nozzle": {"df": 150, "ds": 130, "c": 180},   # PID3
}


def df_addr(n: int) -> int:
    return DF_BASE + 2 * (n - 1)


def ds_addr(n: int) -> int:
    return DS_BASE + (n - 1)


def c_addr(n: int) -> int:
    return C_BASE + (n - 1)


def decode_float_click(lo_hi: tuple[int, int]) -> float:
    """CLICK stores floats low-word-first, big-endian byte order in each word."""
    lo, hi = lo_hi
    raw = (hi << 16) | lo
    return struct.unpack(">f", struct.pack(">I", raw))[0]


def read_coil_block(client: ModbusTcpClient, start_c: int, n: int) -> list[bool] | None:
    rr = client.read_coils(c_addr(start_c), count=n)
    if rr.isError():
        return None
    return [bool(b) for b in rr.bits[:n]]


def read_df_block(client: ModbusTcpClient, start_df: int, n: int) -> list[float] | None:
    rr = client.read_holding_registers(df_addr(start_df), count=2 * n)
    if rr.isError():
        return None
    regs = rr.registers
    return [decode_float_click((regs[2 * i], regs[2 * i + 1])) for i in range(n)]


def read_ds_block(client: ModbusTcpClient, start_ds: int, n: int) -> list[int] | None:
    rr = client.read_holding_registers(ds_addr(start_ds), count=n)
    if rr.isError():
        return None
    return list(rr.registers)


def fmt_coils(values: list[bool], start: int) -> str:
    """One-line summary of any coil that's currently ON."""
    on = [f"C{start + i}" for i, v in enumerate(values) if v]
    return ", ".join(on) if on else "(none on)"


def diff_print(label: str, old, new, fmt) -> None:
    if old is None:
        return
    for i, (a, b) in enumerate(zip(old, new)):
        if a != b:
            print(f"  {label}{i:+d}  {fmt(a)}  →  {fmt(b)}")


def main(argv: list[str] | None = None) -> int:
    here = Path(__file__).resolve().parent.parent
    default_cfg = here / "config.yaml"

    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-c", "--config", default=str(default_cfg),
                    help=f"slow-control config (default: {default_cfg})")
    ap.add_argument("--zone", choices=list(PID_BLOCKS.keys()), default="top",
                    help="which PID block to watch (default: top / PID1)")
    ap.add_argument("--interval", type=float, default=0.5,
                    help="polling interval seconds (default: 0.5)")
    ap.add_argument("--host", default=None,
                    help="override PLC host (default: from config.yaml)")
    ap.add_argument("--noise", action="store_true",
                    help="don't suppress integral/output micro-creep "
                         "(useful if you suspect those registers carry "
                         "the signal you want to see)")
    args = ap.parse_args(argv)

    cfg = yaml.safe_load(open(args.config)) or {}
    plc = cfg.get("plc") or {}
    host = args.host or plc.get("host", "192.168.8.118")
    port = int(plc.get("port", 502))

    block = PID_BLOCKS[args.zone]
    df_start = block["df"]
    ds_start = block["ds"]
    c_start = block["c"]

    print(f"connecting to PLC at {host}:{port} …")
    client = ModbusTcpClient(host=host, port=port, timeout=2.0)
    if not client.connect():
        print(f"ERROR: could not connect to {host}:{port}", file=sys.stderr)
        return 1

    print(f"watching zone={args.zone}: "
          f"C{c_start}-C{c_start + 39}  +  "
          f"DS{ds_start}-DS{ds_start + 14}  +  "
          f"DF{df_start}-DF{df_start + 24}")
    print("toggle Auto/Manual (or anything else) in the PID Monitor; "
          "changes will print here.")
    print("Ctrl-C to stop.")
    print()

    prev_c: list[bool] | None = None
    prev_ds: list[int] | None = None
    prev_df: list[float] | None = None
    # DF104 = integral accumulator; ticks every poll under auto, so we
    # suppress single-LSB jitter in the diff print unless explicitly --noise.
    NOISE_DF: set[int] = set() if args.noise else {df_start + 4}
    # DF108 = output; it slowly drifts with the integral. Same treatment.
    if not args.noise:
        NOISE_DF.add(df_start + 8)
    tick = 0

    try:
        while True:
            coils = read_coil_block(client, c_start, 40)
            dss   = read_ds_block(client, ds_start, 15)
            dfs   = read_df_block(client, df_start, 25)
            if coils is None or dss is None or dfs is None:
                print(f"[{datetime.now().strftime('%H:%M:%S')}] read error — retrying")
                time.sleep(args.interval)
                continue

            stamp = datetime.now().strftime("%H:%M:%S.%f")[:-3]
            if prev_c is None:
                # Baseline dump
                print(f"[{stamp}] baseline")
                print(f"  coils ON: {fmt_coils(coils, c_start)}")
                for i, v in enumerate(dss):
                    if v != 0:
                        print(f"  DS{ds_start + i:<5d}  {v}  (= 0x{v:04x}  bin {v:016b})")
                for i, v in enumerate(dfs):
                    print(f"  DF{df_start + i:<5d}  {v:>14.4f}")
                print()
            else:
                changed = False
                for i, (a, b) in enumerate(zip(prev_c, coils)):
                    if a != b:
                        if not changed:
                            print(f"[{stamp}] changes")
                            changed = True
                        arrow = "0 → 1" if b else "1 → 0"
                        print(f"  C{c_start + i:<5d}  {arrow}")
                for i, (a, b) in enumerate(zip(prev_ds, dss)):
                    if a != b:
                        if not changed:
                            print(f"[{stamp}] changes")
                            changed = True
                        print(f"  DS{ds_start + i:<5d}  {a}  →  {b}  "
                              f"(0x{a:04x} → 0x{b:04x})")
                for i, (a, b) in enumerate(zip(prev_df, dfs)):
                    addr = df_start + i
                    if abs(a - b) <= 1e-6:
                        continue
                    if addr in NOISE_DF and abs(a - b) < 0.1:
                        continue   # ignore integral/output micro-creep
                    if not changed:
                        print(f"[{stamp}] changes")
                        changed = True
                    print(f"  DF{addr:<5d}  {a:>12.4f}  →  {b:>12.4f}")
                if changed:
                    print()

            prev_c = coils
            prev_ds = dss
            prev_df = dfs
            tick += 1
            time.sleep(args.interval)

    except KeyboardInterrupt:
        print("\nstopped.")
    finally:
        client.close()

    return 0


if __name__ == "__main__":
    sys.exit(main())
