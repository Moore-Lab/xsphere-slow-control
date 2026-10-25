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

Published by the PLC driver itself (not the 15 s watchdog):

| Rule | Condition | Default threshold |
|---|---|---|
| `pid_stuck_off/{zone}` | Auto, output ≤ 0.5 %, PV > 1 K below SP, PID integrator (Bias) below 0 % | ≥ 20 s and 2 PID samples + 5 s |

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

**pid_stuck_off**: A heater zone is in Auto, below its setpoint, and held at
0 % by a wound-up integrator — the old "switch to Manual and back to Auto"
symptom ([docs/pid-windup.md](docs/pid-windup.md)).
- With `plc.pid_stuck_off_remedy: toggle` (the default) the service does that
  toggle itself, once per zone per 10 min, checks the zone is back in Auto and
  keeps asking until it is.  `rescue_count` / `rescue_last` in
  `xsphere/status/pid/{zone}` and the journal (`STUCK OFF`, `rescue`) show it.
- It only toggles a zone whose PV is fed by a live PV expression; with no PV
  source it alerts and logs `not fed by a live PV expression` instead.
- Pressing Auto or Manual for that zone on the Control page always takes over.
- Toggle by hand only a zone in Auto at 0 % with PV *below* SP — never one
  sitting at 100 % (that sets the integrator to 100 % and it overshoots).

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
4. Monitor `xsphere/sensors/pressure/ghs/setra/+` and `xsphere/sensors/vacuum/ghs/+` during transfer.
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

## AI control (Claude)

The `xsphere-aicontrol` service (port 8089) lets a Claude session read
everything and, when you allow it, change the heaters, the LN2 fill valves and
the pneumatic gas valves. Design, tool list and deploy steps:
`aicontrol/README.md`.

### Arming

The **AI control (Claude)** card at the top of the Control page
(`http://xbox-pi:8088/control`) has one row per group: heaters, ln2 and
pneumatic.

- **arm** lets Claude write in that group, within the rails, for
  `arm_hours` (8 h by default). **disarm**, or **disarm all**, stops it at
  once. Disarming moves nothing.
- Each row says what Claude may do right now, or why the group is disarmed.
  The header pill shows "AI: off" or which groups are ARMED. "AI: no status"
  (or "ARMED? (no status)") means the card has heard nothing from the service
  for over 10 s: it may still be running, with the arms shown last. Arm
  buttons are disabled then, and while this web panel has lost the broker;
  **disarm** always stays clickable, and the page says if it was not sent.
- Everything starts disarmed whenever the service starts. The service also
  disarms by itself in these cases:
  - **All groups:** the arm expires; slow control restarts; the snapshot goes
    stale; the AI service reconnects to the broker.
  - **Heaters:** the PV interlock trips, or the interlocks go not-ok.
  - **LN2:** a valve the AI opened is still open (by its live status) at its
    time limit. The service closes the valve, then disarms. It never acts on
    an unknown valve state; if the status stays unknown past the limit, the
    activity list says so — check the valve.
- Pneumatic valves have two gates. Claude needs both the AI arm and the
  existing "enable control" gate on the pneumatic card.

### Rails

Open **rails — limits the AI can never change** on the card. The rails set:

- the setpoint range, the step and the rate per zone, and which sensors may
  be the PID's PV;
- the voltage and current range per supply channel, and whether Claude may
  switch a channel on or off;
- per LN2 vessel, whether Claude may operate it, its maximum open time, and
  the auto flags;
- per pneumatic valve, open and close permission, the pairs that must never
  be open together, and the minimum interval between commands.

Empty fields work like this:

- An empty min / max pair means Claude may not change that quantity at all.
- An empty step, rate or arm time means no limit.
- A supply minimum of 0 V or 0 A needs "may switch off" too: it unpowers the
  heater just as switching the output off does.
- An unticked permission means "not allowed".
- With no rails saved, or an invalid rails file, nothing is writable.

**save rails** sends the whole set. If someone saved in between, your save is
refused: press **discard edits** to load the newer rails, re-apply your change
and save again (reloading the page also works). If the heater zones' sensor
mapping changes, for example after the top/bottom swap is deployed, the card
asks you to save the rails again before those zones are writable.

The rails cap what Claude may *command*. The PV interlock still caps the
temperature for everyone, Claude included.

Every AI action and refusal, with Claude's stated reason, is listed under
**recent AI activity** (the last 10). The last 20 are at
`http://xbox-pi:8089/`; the full log is `aicontrol/audit.jsonl` on the Pi (and
`audit.jsonl.1` … `.5` after rotation, 5 MB each). Notes are limited to 6 a
minute, and calls that send nothing (refusals, no-ops) beyond 30 a minute are
counted and summed up in one entry, so a looping session cannot flood the log.

### Checks after deploying it

Two paths have never run end to end before the first deploy; check them once
on the Pi (with everything disarmed — nothing below moves hardware):

1. **History** (needs the InfluxDB token, see `aicontrol/README.md`). Each of
   these must return non-empty series, the expected `every_s`, no unexpected
   `truncated`, and log an elapsed time well under 20 s
   (`journalctl -u xsphere-aicontrol | grep history:`):
   ```bash
   curl -s 'http://localhost:8089/api/v1/history?ids=t_cube_top&start=-6h'
   curl -s 'http://localhost:8089/api/v1/history?ids=level_cryostat&start=-6h'   # field filtered|raw
   curl -s 'http://localhost:8089/api/v1/history?ids=valve_cryostat&start=-7d&fn=changes'
   curl -s 'http://localhost:8089/api/v1/history?ids=turbo_ghs_state&start=-6h'  # text, last
   curl -s 'http://localhost:8089/api/v1/history?ids=t_cube_top,t_cube_bottom,t_cube_nozzle,pid_top_pv,pid_bottom_pv,pid_nozzle_pv,level_cryostat,valve_cryostat&start=-30d'
   ```
2. **A real MCP tool call.** In the operator profile, ask Claude for
   `get_overview`, then (nothing armed) for a heater setpoint: the first must
   return the overview, the second must be refused with "not armed"
   (acceptance step 6 in `aicontrol/operator/README.md`). The journal shows
   the protocol era of the first request at INFO; start the service once with
   `XSPHERE_AICONTROL_LOG_LEVEL=DEBUG` to see every `tools/call`. Note the
   date in the operator README's "Verified on" paragraph.

### Connecting Claude

To confine a session to the AI control tools, copy the operator profile
(`aicontrol/operator/`, see its README) to `C:\Users\xbox\xsphere-ai-operator`,
open Claude Code there, and run the acceptance check.

Any other Claude Code session can add the tools with
`claude mcp add --transport http xsphere http://xbox-pi:8089/mcp`, but it keeps
its shell and browser.

### Limitations

Arms and rails only bind clients that go through this service. The MQTT
broker is anonymous, `/api/cmd` on the Control page relays commands, and
code-server and Node-RED are open on the LAN. A session with a shell or a
browser can therefore bypass all of it, arming included. Use the operator
profile for any AI that should be confined.

---

## Data access

### InfluxDB / Grafana

```
http://192.168.8.116:8087    InfluxDB UI — raw data explorer
http://192.168.8.116:3000    Grafana — dashboards and plots
```

All sensor measurements are stored in the `xsphere` bucket.  Key measurement
names (set by Telegraf):

| Measurement | Tags | Fields |
|---|---|---|
| `temperature` | `source` (plc/omega), `sensor_type` (rtd/tc), `channel` | `value_k`, `value_c` |
| `level` | `vessel` | `raw`, `filtered` |
| `pressure` | `source` (ghs), `gauge` (setra), `channel` | `value` (mbar) |
| `vacuum` | `source` (ghs), `channel` | `value` (mbar) |
| `pid_state` | `zone` | `setpoint_k`, `pv_k`, `output_pct`, `kp`, `ki`, `kd` |
| `valve_state` | `vessel` | `state`, `desired`, `auto_open`, `auto_close` |
| `pneumatic_state` | `valve` (ballast/pump/bottle) | `state`, `desired` (1 = open), `relay` (1 = energised) |
| `pneumatic_control` | — | `enabled` (1 = pneumatic open/close commands acted on), `refused` (count since the service started) |
| `environment` | `location` (ghs), `parameter` (temperature/humidity/baro_pressure) | `value` |
| `service_heartbeat` | — | `uptime_s` |
| `ai_control` | — | `online`, `armed_heaters`, `armed_ln2`, `armed_pneumatic` (1 = armed for Claude), `actions`, `refused`, `auto_disarms` (counts since the AI service started) |

### Changing what Telegraf records

Telegraf runs in the IOTstack `telegraf` container and reads
`~/IOTstack/volumes/telegraf/telegraf.conf`, not the copy in this repo.
After any change to `telegraf/telegraf.conf` (a new topic, field or
measurement), `git pull` on xbox-pi is not enough — redeploy it:

```bash
cd ~/xsphere-slow-control
bash telegraf/deploy-iotstack.sh            # preview: diff + validation, changes nothing
bash telegraf/deploy-iotstack.sh --apply    # install, restart telegraf
```

The restart loses the few seconds of data published while Telegraf is down.
Until the redeploy the new measurement is simply not stored, with no error
anywhere — and it cannot be backfilled.

Then confirm every measurement is arriving (points in the last 2 minutes):

```bash
cd ~/xsphere-slow-control
TOKEN=$(grep '^INFLUX_TOKEN=' .env | cut -d= -f2-)
curl -s 'http://localhost:8087/api/v2/query?org=xbox-server' \
  -H "Authorization: Token $TOKEN" -H 'Content-Type: application/vnd.flux' \
  --data-binary 'from(bucket:"xsphere") |> range(start:-2m) |> count()
    |> group(columns:["_measurement"]) |> sum()
    |> keep(columns:["_measurement","_value"])'
```

`pressure`, `vacuum` and `environment` all come from the GHS ESP32, so they
are missing together whenever that board is offline; that is not a Telegraf
problem.

### Subscribing to raw MQTT (debugging)

```bash
# mosquitto_sub is not installed on the xbox-pi host; there, prefix each
# command with:  docker exec mosquitto

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

# AI control service
journalctl -u xsphere-aicontrol -f

# Telegraf (IOTstack container)
docker logs telegraf -f

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
