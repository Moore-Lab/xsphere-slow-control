"""Pull GHS ambient environment data from the InfluxDB "xsphere" bucket.

The GHS ESP32 publishes xsphere/sensors/environment/ghs/{temperature|humidity|
baro_pressure}; Telegraf ingests those as measurement "environment" with tags
location/parameter/system and a single field "value" (see
xsphere-slow-control/telegraf/telegraf.conf, Input 5).

Run in a terminal:
    python ambient_temp_pull.py
    python ambient_temp_pull.py --start "07/05/2026, 00:00" --end "07/10/2026, 00:00"

Without --start/--end you are prompted for them, in the format MM/DD/YYYY, HH:MM
(24-hour Yale local time).
"""

import argparse
import os
import warnings
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import pandas as pd
from influxdb_client import InfluxDBClient
from influxdb_client.client.warnings import MissingPivotFunction

# We pivot in pandas on `parameter`, not in Flux on `_field` (there is only one
# field, "value"), so the client's pivot nag does not apply here.
warnings.simplefilter("ignore", MissingPivotFunction)

# ----------------------------------------------------------------------------
# InfluxDB configuration
# ----------------------------------------------------------------------------
URL    = "http://gl-sft1200.stdusr.yale.internal:2504"
ORG    = "xbox-server"
TOKEN  = "51LcCgsoNcCd026VcAHUYkbLGHQ7ftIBxRRGFjYN7akKT5030DhkAwQ3Aq2ol32qnmE-ab7rQInjVQITc-2H8g=="
BUCKET = "xsphere"

# Series selection. PARAMETERS may list any of temperature / humidity /
# baro_pressure; each becomes one column in the CSV and one trace on the plot.
MEASUREMENT = "environment"
LOCATION    = "ghs"
SYSTEM      = "xsphere"
PARAMETERS  = ("temperature",)

# Yale local time. ZoneInfo handles the EDT/EST switch; a fixed UTC-4 offset
# would be an hour off for any range between November and March.
LOCAL_TZ = ZoneInfo("America/New_York")

# Aggregation: aim for roughly this many points across the range, then pick a
# window to match. Mimics Grafana's v.windowPeriod, which auto-scales to the view
# instead of using a fixed window (a fixed 1m over multi-day ranges looks aliased).
TARGET_POINTS = 720

INPUT_FMT = "%m/%d/%Y, %H:%M"


def pick_window_seconds(total_seconds, target_points=TARGET_POINTS):
    """Choose an aggregateWindow size so the range yields ~target_points samples."""
    ladder = [60, 120, 300, 600, 900, 1800, 3600, 7200, 21600, 43200, 86400]
    ideal = total_seconds / max(target_points, 1)
    for w in ladder:
        if w >= ideal:
            return w
    return ladder[-1]


def flux_duration(seconds):
    """Format whole seconds as a Flux duration string (e.g., 300 -> '5m')."""
    if seconds % 86400 == 0:
        return f"{seconds // 86400}d"
    if seconds % 3600 == 0:
        return f"{seconds // 3600}h"
    if seconds % 60 == 0:
        return f"{seconds // 60}m"
    return f"{seconds}s"


def parse_local(raw):
    """Parse 'MM/DD/YYYY, HH:MM' into a LOCAL_TZ-aware datetime."""
    return datetime.strptime(raw.strip(), INPUT_FMT).replace(tzinfo=LOCAL_TZ)


def prompt_datetime(label):
    """Prompt until the user enters a valid 'MM/DD/YYYY, HH:MM' string."""
    while True:
        try:
            return parse_local(input(f"{label} (MM/DD/YYYY, HH:MM): "))
        except ValueError:
            print("  Invalid format. Example: 05/14/2026, 17:10")


def build_flux(start_iso, end_iso, window):
    """Flux query for the selected environment parameters over [start, end)."""
    param_set = ", ".join(f'"{p}"' for p in PARAMETERS)
    return f'''
from(bucket: "{BUCKET}")
  |> range(start: {start_iso}, stop: {end_iso})
  |> filter(fn: (r) => r["_measurement"] == "{MEASUREMENT}")
  |> filter(fn: (r) => r["_field"] == "value")
  |> filter(fn: (r) => r["location"] == "{LOCATION}")
  |> filter(fn: (r) => r["system"] == "{SYSTEM}")
  |> filter(fn: (r) => contains(value: r["parameter"], set: [{param_set}]))
  |> aggregateWindow(every: {window}, fn: mean, createEmpty: false)
  |> yield(name: "mean")
'''


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--start", help='start time, "MM/DD/YYYY, HH:MM" local')
    ap.add_argument("--end",   help='end time, "MM/DD/YYYY, HH:MM" local')
    ap.add_argument("--no-show", action="store_true",
                    help="save the plot but do not open a window")
    args = ap.parse_args()

    print(f"Pulling from bucket: {BUCKET!r}  ({MEASUREMENT} / {LOCATION} / "
          f"{', '.join(PARAMETERS)})\n")

    start_dt_local = parse_local(args.start) if args.start else prompt_datetime("Start")
    end_dt_local   = parse_local(args.end)   if args.end   else prompt_datetime("End")

    if end_dt_local <= start_dt_local:
        print("\nEnd time must be after start time. Exiting.")
        return

    # Convert local -> UTC ISO 8601 for Flux
    start_iso = start_dt_local.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    end_iso   = end_dt_local.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    total_seconds = (end_dt_local - start_dt_local).total_seconds()
    window = flux_duration(pick_window_seconds(total_seconds))
    print(f"Local time range: {start_dt_local.strftime(INPUT_FMT)} "
          f"to {end_dt_local.strftime(INPUT_FMT)}")
    print(f"UTC time range:   {start_iso} to {end_iso}")
    print(f"Duration: {total_seconds / 3600:.1f} hours")
    print(f"Aggregation window: {window}\n")

    flux = build_flux(start_iso, end_iso, window)

    with InfluxDBClient(url=URL, token=TOKEN, org=ORG, timeout=120_000) as client:
        df = client.query_api().query_data_frame(query=flux, org=ORG)

    # query_data_frame returns a list when the result has more than one table
    # (one per parameter here), a single DataFrame otherwise.
    if isinstance(df, list):
        df = pd.concat(df, ignore_index=True) if df else pd.DataFrame()

    if df.empty:
        print("No data returned for this time range.")
        return

    # One column per parameter, indexed by time. Every row shares
    # _measurement == "environment", so `parameter` is what distinguishes series.
    df = df.rename(columns={"_time": "time", "_value": "value"})
    df["time"] = pd.to_datetime(df["time"], utc=True)
    df = df.pivot_table(index="time", columns="parameter", values="value", aggfunc="mean")
    df.columns.name = None

    # Convert the UTC index to local time so the CSV reads in Yale local time.
    df.index = df.index.tz_convert(LOCAL_TZ)

    start_tag = start_dt_local.strftime("%Y%m%d_%H%M")
    end_tag   = end_dt_local.strftime("%Y%m%d_%H%M")
    out_csv = f"ambient_{'_'.join(PARAMETERS)}_{start_tag}_{end_tag}.csv"
    df.to_csv(out_csv)

    print("Data retrieved:")
    print(df.head())
    print(f"\nShape: {df.shape}")
    print(f"Parameters: {df.columns.tolist()}")
    print(f"Saved to: {os.path.abspath(out_csv)}")

    # Plot vs time. Index is tz-aware local; DateFormatter renders LOCAL_TZ.
    fig, ax = plt.subplots(figsize=(14, 6))
    for col in df.columns:
        ax.plot(df.index, df[col], label=col, linewidth=1.5)

    if total_seconds > 24 * 3600:
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%m/%d %H:%M", tz=LOCAL_TZ))
    else:
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M", tz=LOCAL_TZ))
    plt.xticks(rotation=45)
    ax.set_title(f"GHS ambient {', '.join(df.columns)} "
                 f"({start_dt_local.strftime(INPUT_FMT)} – {end_dt_local.strftime(INPUT_FMT)})")
    ax.set_xlabel("Time (local)")
    ax.set_ylabel(df.columns[0] if len(df.columns) == 1 else "value")
    ax.legend()
    ax.grid(True, alpha=0.3)
    plt.tight_layout()

    out_png = f"ambient_{'_'.join(PARAMETERS)}_{start_tag}_{end_tag}.png"
    fig.savefig(out_png, dpi=150, bbox_inches="tight")
    print(f"Plot saved to: {os.path.abspath(out_png)}")
    if not args.no_show:
        plt.show()


if __name__ == "__main__":
    main()
