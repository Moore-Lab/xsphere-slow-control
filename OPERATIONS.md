# xsphere Slow Control — Operations Reference

Day-to-day guide for running the cryostat.  See SETUP.md for first-time
installation, and VERIFICATION_CHECKLIST.md for hardware commissioning.

---

## Dashboard

Open the control dashboard in any browser on the lab network:

```
http://192.168.8.116:1880/ui
```

Tabs:
| Tab | Contents |
|---|---|
| **Temperatures** | All RTD and TC channels (PLC + Omega) |
| **PID / Heaters** | Setpoint, process value, and output % per zone; gradient controls |
| **Level / Valves** | LN2 level readings; autofill arm/disarm switches |
| **Gas Handling** | Pressure and vacuum gauges; lab environment |
| **Interlocks** | Active alert list; overall ok/not-ok indicator |
| **Gradient Scan** | Configure and start an automated temperature scan |

---

## Starting and stopping the system

### Normal startup

The two Python services start automatically on boot via systemd.
If they are not running:

```bash
sudo systemctl start xsphere-slowcontrol
sudo systemctl start xsphere-omega-logger
```

Confirm they are healthy:
```bash
sudo systemctl status xsphere-slowcontrol
sudo systemctl status xsphere-omega-logger
# Or watch the heartbeat topic:
mosquitto_sub -h localhost -t 'xsphere/status/service/heartbeat' -v
```

The heartbeat publishes every 10 seconds with an uptime counter.  If it stops
updating, the Python service has crashed — check `journalctl -u xsphere-slowcontrol -f`.

### Normal shutdown

```bash
sudo systemctl stop xsphere-slowcontrol
sudo systemctl stop xsphere-omega-logger
```

The service sends SIGTERM to the Python process, which closes the Modbus
connection and disconnects from MQTT cleanly.

---

## Temperature control

### Gradient mode (normal operating mode)

In gradient mode, you set one base temperature and two offsets:

- **Base (K)** — setpoint for the top heater zone
- **ΔV (K)** — bottom zone setpoint = base + ΔV (vertical gradient)
- **ΔL (K)** — nozzle zone setpoint = base + ΔL (longitudinal gradient)

Use the sliders on the **PID / Heaters** tab. Typical starting values:
- Base: 165 K, ΔV: 0 K, ΔL: 0 K (isothermal)

To create a gradient between top and bottom: set ΔV negative (bottom colder
than top) or positive (bottom warmer than top).

### Absolute mode

For independent per-zone setpoints (e.g., during diagnostics):

1. Click **Absolute Mode** on the dashboard.
2. The PLC now accepts setpoints per zone independently.
3. Adjust each PID zone setpoint through the PLC programmer or by publishing
   directly:
   ```bash
   mosquitto_pub -h localhost -t xsphere/commands/pid/top/setpoint \
     -m '{"value_k": 165.0}'
   ```

Switch back to gradient mode by clicking **Gradient Mode** on the dashboard.
This immediately recomputes and applies all three zone setpoints.

### Changing setpoints via MQTT (command line)

```bash
# Set gradient base to 170 K
mosquitto_pub -h localhost -t xsphere/commands/gradient/base \
  -m '{"value_k": 170.0}'

# Set vertical gradient to -2 K (bottom 2 K colder than top)
mosquitto_pub -h localhost -t xsphere/commands/gradient/vertical \
  -m '{"delta_k": -2.0}'
```

---

## LN2 autofill

### Overview

The autovalve controller manages two solenoid valves:
- **XV1 (ballast)** — fills the ballast LN2 dewar
- **XV2 (primary_xe)** — fills the primary xenon dewar

Each valve has two independent enable flags:
- **auto_open** — service opens the valve when level falls below `level_low`
- **auto_close** — service closes the valve when level rises above `level_high`

Both flags are **disabled by default at startup**.  You must explicitly arm
them before autofill operates.

> **Safety note**: Before arming autofill, confirm that the level sensor
> thresholds in `config.yaml` have been calibrated for your sensor readings.
> If `level_low` is set too high relative to the actual reading, the valve
> will open immediately on arm.

### Arming autofill

Via the dashboard (**Level / Valves** tab): toggle the switches for each
vessel.

Via MQTT:
```bash
# Arm ballast autofill (both directions)
mosquitto_pub -h localhost -t xsphere/commands/valve/ballast/auto_open  \
  -m '{"enabled": true}'
mosquitto_pub -h localhost -t xsphere/commands/valve/ballast/auto_close \
  -m '{"enabled": true}'

# Arm primary_xe autofill
mosquitto_pub -h localhost -t xsphere/commands/valve/primary_xe/auto_open  \
  -m '{"enabled": true}'
mosquitto_pub -h localhost -t xsphere/commands/valve/primary_xe/auto_close \
  -m '{"enabled": true}'
```

### Manual valve control

To open or close a valve manually regardless of level:
```bash
# Open the ballast valve
mosquitto_pub -h localhost -t xsphere/commands/valve/ballast/state \
  -m '{"state": 1}'

# Close the ballast valve
mosquitto_pub -h localhost -t xsphere/commands/valve/ballast/state \
  -m '{"state": 0}'
```

This overrides autofill temporarily.  The valve state is tracked — if
auto_close is armed and the level reaches `level_high` after a manual open,
the valve will still close automatically.

### Fill timeout safety

If a valve is open for longer than `fill_timeout_s` (default: 600 s ballast /
primary_xe, 920 s cryostat) without the level reaching `level_high`, the
service forces the valve closed and publishes an alert:
```
xsphere/alerts/fill_timeout/{vessel}
```
Check that the dewar supply line is not blocked.  Increase `fill_timeout_s`
in `config.yaml` if legitimate fills are taking longer than expected.

---

## Gradient temperature scan

The gradient scanner plugin steps the base temperature setpoint through a
defined range, dwelling at each step.

### Starting a scan via the dashboard

1. Go to the **Gradient Scan** tab.
2. Fill in the scan parameters form:
   - **Start (K)** — first setpoint (can be higher or lower than End)
   - **End (K)** — last setpoint
   - **Step (K)** — increment per step (negative for cooling scans)
   - **Dwell (s)** — how long to hold at each setpoint
3. Click **Start Scan**.
4. Progress is displayed in the status bar above the form.
5. Click **STOP SCAN** to abort at any time.

### Starting a scan via MQTT

```bash
mosquitto_pub -h localhost -t xsphere/commands/gradient_scanner/start \
  -m '{
    "start_k": 160.0,
    "end_k":   180.0,
    "step_k":  5.0,
    "dwell_s": 300,
    "stable_band_k": 1.0,
    "stable_timeout_s": 300
  }'
```

Optional parameters:
- `stable_band_k` — scan waits until all temperatures are within this window
  of the setpoint before starting the dwell timer (default: 1.0 K)
- `stable_timeout_s` — maximum wait for stability before moving on anyway
  (default: 300 s)

### Stopping a scan

```bash
mosquitto_pub -h localhost -t xsphere/commands/gradient_scanner/stop \
  -m '{}'
```

### Scan status

```bash
mosquitto_sub -h localhost -t 'xsphere/status/gradient_scanner' -v
```

Returns:
```json
{
  "state": "dwelling",
  "step": 2,
  "total_steps": 5,
  "setpoint_k": 170.0,
  "elapsed_s": 142.3,
  "ok": true
}
```

---

## Interlock alerts

The interlock watchdog runs every 15 seconds and checks:

| Rule | Condition | Default threshold |
|---|---|---|
| `temperature_stale/{ch}` | No temperature update | > 30 s |
| `temperature_range/{ch}` | Temperature out of range | < 50 K or > 400 K |
| `level_stale/{vessel}` | No level update | > 60 s |
| `pid_saturated/{zone}` | Heater at 100% continuously | > 300 s |

### When an alert fires

1. The **Interlocks** tab will show the alert in red.
2. An MQTT message is published (retained) to `xsphere/alerts/{rule}/{channel}`.
3. The `xsphere/status/interlocks` topic updates with `"ok": false`.

### Clearing an alert

Alerts clear automatically when the condition resolves.  The watchdog publishes
an empty retained message to the alert topic, which clears it from the broker
and the dashboard.

To inspect active alerts manually:
```bash
mosquitto_sub -h localhost -t 'xsphere/alerts/#' -v
```

### Responding to specific alerts

**temperature_stale**: A sensor has stopped publishing.
- Check that the Python service and Omega logger are running.
- Check the ESP32 boards (WiFi connection, power).
- Check Modbus connection to PLC.

**temperature_range**: A sensor is reading below 50 K or above 400 K.
- Below 50 K usually means a sensor is not connected or has failed open.
- Above 400 K is a genuine over-temperature condition — reduce heater setpoints.

**level_stale**: Level sensor ESP32 has stopped publishing.
- Check the WiFi connection of the affected ESP32.
- Check `xsphere/status/level_{vessel}` for the board's last uptime/RSSI.

**pid_saturated**: A heater zone has been at 100% output for > 5 minutes.
- The heater cannot keep up with heat load — likely LN2 is boiling off faster
  than the heater can compensate, or the setpoint is too far above current
  temperature.
- Consider reducing the setpoint or checking that the dewar is properly filled.

---

## Typical experiment sequence

### Loading xenon

1. Confirm all temperatures are stable at operating setpoint (e.g., 165 K).
2. Confirm pressure gauges are at expected values before transfer.
3. Open gas handling valves manually as per the gas handling procedure.
4. Monitor `xsphere/sensors/pressure/main` and `vacuum/xe_cube` during transfer.
5. After transfer, confirm xenon pressure stabilizes.

### Cooling to operating temperature

1. Set base_k to room temperature equivalent first if starting warm.
2. Arm autofill for ballast and primary_xe dewar.
3. Begin lowering base_k in steps.  Use the gradient scanner for systematic
   steps:
   - Start: current temperature
   - End: target (e.g., 160 K)
   - Step: −5 K, Dwell: 600 s
4. Monitor interlock status throughout.
5. Once at target temperature, confirm all three PID zones are stable (output
   not saturated, setpoint ≈ process value).

### Warming up

Reverse of cooling:
1. Disarm autofill (prevents unnecessary LN2 fills during warmup).
2. Ramp base_k upward using the gradient scanner or manual slider.
3. At ~200 K, confirm xenon has fully evaporated before disconnecting gas lines.

---

## Data access

### InfluxDB / Grafana

```
http://192.168.8.116:8086    InfluxDB UI — raw data explorer
http://192.168.8.116:3000    Grafana — dashboards and plots
```

All sensor measurements are stored in the `xsphere` bucket.  Key measurement
names (set by Telegraf):

| Measurement | Tags | Fields |
|---|---|---|
| `temperature` | `source` (plc/omega), `channel` | `value_k` |
| `level` | `vessel` | `raw_pf`, `filtered` |
| `pressure` | `gauge` | `value_psi` |
| `vacuum` | `gauge` | `value_mbar` |
| `pid` | `zone` | `setpoint_k`, `pv_k`, `output_pct` |
| `environment` | `sensor` | `temperature_c`, `humidity_pct`, `pressure_hpa` |

### Subscribing to raw MQTT (debugging)

```bash
# All sensor data
mosquitto_sub -h localhost -t 'xsphere/sensors/#' -v

# All status topics
mosquitto_sub -h localhost -t 'xsphere/status/#' -v

# All alerts
mosquitto_sub -h localhost -t 'xsphere/alerts/#' -v

# Everything (verbose — use carefully)
mosquitto_sub -h localhost -t 'xsphere/#' -v
```

---

## Service logs

```bash
# Python slow control service
journalctl -u xsphere-slowcontrol -f
journalctl -u xsphere-slowcontrol --since "1 hour ago"

# Omega logger
journalctl -u xsphere-omega-logger -f

# Telegraf
journalctl -u telegraf -f

# Node-RED (if running in Docker)
docker logs nodered -f
```

Log level for the Python service is set in `config.yaml` → `log_level`.
Change to `DEBUG` for verbose Modbus and MQTT tracing, restart the service.

---

## Modifying thresholds and parameters

All tunable parameters are in `slowcontrol/config.yaml`.  After any change:

```bash
sudo systemctl restart xsphere-slowcontrol
```

Key parameters to know:

| Parameter | Location | Effect |
|---|---|---|
| `plc.poll_interval` | `config.yaml` | How often PLC registers are read (seconds) |
| `autovalve.enabled` | `config.yaml` | Master enable for all autofill logic |
| `autovalve.vessels.*.level_high` | `config.yaml` | Close valve threshold (pF) |
| `autovalve.vessels.*.level_low` | `config.yaml` | Open valve threshold (pF) |
| `autovalve.vessels.*.fill_timeout_s` | `config.yaml` | Max fill duration safety |
| `heartbeat_interval` | `config.yaml` | Heartbeat publish interval |
| Interlock thresholds | `controllers/interlocks.py` (top of file) | Stale/range/saturation limits |

Interlock thresholds (`TEMP_MIN_K`, `TEMP_MAX_K`, `TEMP_STALE_S`, etc.) are
constants at the top of `slowcontrol/controllers/interlocks.py`.  They are not
yet exposed in `config.yaml` — edit the file directly and restart the service.
