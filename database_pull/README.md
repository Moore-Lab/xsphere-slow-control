# ambient_temp_pull.py

Pulls the GHS ambient temperature recorded by the GHS ESP32 sensor from the `xsphere` InfluxDB bucket over a time range you choose.
Times are entered in Yale local time, and the data is averaged into windows sized to the range (about 720 points in total).
Saves the data as a CSV and a PNG plot (`ambient_temperature_<start>_<end>.csv` / `.png`) in the current directory.

## Running

Install the dependencies once:

```
pip install influxdb-client pandas matplotlib
```

You need to be on the Yale network (or VPN) to reach the InfluxDB server.

Interactive (prompts for start/end in `MM/DD/YYYY, HH:MM`, 24-hour local time):

```
python ambient_temp_pull.py
```

With the range given on the command line:

```
python ambient_temp_pull.py --start "07/05/2026, 00:00" --end "07/10/2026, 00:00"
```

Add `--no-show` to save the plot without opening a window.

## Example output

`EXAMPLE_ambient_temperature_20260705_0000_20260710_0000.csv` / `.png` are the output of the second command above (07/05/2026 – 07/10/2026).
