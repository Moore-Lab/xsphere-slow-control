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
pneumatic. Arming needs the 6-digit code from your authenticator app (set up
once: `aicontrol/README.md`, "Enrolling").

To arm a group:

1. Type the current code into the code box next to the arm buttons.
2. Check the client (default `operator`), the hours (default and maximum: the
   rails' `arm_hours`) and the write budget (default: the rails'
   `write_budget`).
3. Click **arm** on the group's row.

- An arm is a lease: Claude (that client only) may write in that group,
  within the rails and the pressure guard, until the hours run out or it has
  made that many write calls, whichever comes first. Other Claude sessions,
  and any without a write token, stay refused.
- Each code works once. To arm a second group, wait for the next code (up to
  30 s). After 5 wrong codes in 5 minutes, codes are refused for 5 minutes.
- **disarm** and **disarm all** need no code. They stop Claude at once.
  Disarming moves nothing.
- The card shows the result of the last arm (refused codes say why), and for
  each armed group the client, when it lapses, and the writes used.
- Each row says what Claude may do right now, or why the group is disarmed.
  The card also shows the pressure guard (readings, xenon present, whether
  warming writes are allowed) and any zone whose PV source failed its
  plausibility check.
  The header pill shows "AI: off" or which groups are ARMED. "AI: no status"
  (or "ARMED? (no status)") means the card has heard nothing from the service
  for over 10 s: it may still be running, with the arms shown last. Arm
  buttons are disabled then, and while this web panel has lost the broker;
  **disarm** always stays clickable, and the page says if it was not sent.
- Everything starts disarmed whenever the service starts. The service also
  disarms by itself in these cases:
  - **All groups:** the arm expires; slow control restarts; the snapshot goes
    stale; the AI service reconnects to the broker.
  - **The group whose write budget is used up.**
  - **Heaters:** the PV interlock trips, the interlocks go not-ok, or a zone's
    PV source fails its plausibility check.
  - **Heaters and pneumatic:** the pressure guard trips, or a pressure gauge
    has had no fresh reading for `guard.blind_disarm_s`.
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
- how far a clamp sensor may be from the zone's cube RTD to be used as a PV
  (`heaters.pv_cross_check_k`);
- the voltage and current range per supply channel, and whether Claude may
  switch a channel on or off;
- per LN2 vessel, whether Claude may operate it, its maximum open time, and
  the auto flags;
- per pneumatic valve, open and close permission, the pairs that must never
  be open together, and the minimum interval between commands;
- the longest an arm may last (`general.arm_hours`, at most 12 h) and the most
  write calls one arm allows (`general.write_budget`);
- the pressure guard (`guard`, below).

Empty fields work like this:

- An empty min / max pair means Claude may not change that quantity at all.
- An empty step or rate means no limit. The arm time and write budget cannot
  be empty.
- An empty `pv_cross_check_k` means Claude may not pick a clamp sensor.
- An empty guard field refuses the writes that need it (see below).
- A supply minimum of 0 V or 0 A needs "may switch off" too: it unpowers the
  heater just as switching the output off does.
- An unticked permission means "not allowed".
- With no rails saved, or an invalid rails file, nothing is writable. Rails
  saved before the guard was added (schema version 2) count as invalid: save
  them again once.

**save rails** needs a code too: type it into the code box next to the button.
It sends the whole set. If someone saved in between, your save is refused:
press **discard edits** to load the newer rails, re-apply your change and save
again (reloading the page also works). A save refused for its content does not
use up the code. If the heater zones' sensor mapping changes, for example after
the top/bottom swap is deployed, the card asks you to save the rails again
before those zones are writable.

**The pressure guard** (`guard`) watches `p_xe_cube` (Setra #1, which tops out
at about 1766 mbar) and `p_primary_bottle` (Setra #2, about 7064 mbar). A
reading at 97 % of a gauge's top counts as "this much or more", i.e. above
every limit. With the guard set up, Claude may not warm anything while a
pressure is unknown or at its warn level, or with xenon present while ballast
or bottle is closed; it may never close ballast or bottle with xenon present;
it may open the pump only at low pressure, and never together with ballast or
bottle unless `pump_open_with` allows it. At a trip level the service stops
ramps, disarms heaters and pneumatic, and lowers the zones Claude set to their
`setpoint_min_k`. The values are yours to set; as starting points:

| Field | Starting point (your choice) |
|---|---|
| `p_xe_cube` warn / trip | 1200 / 1400 mbar |
| `p_primary_bottle` warn / trip | about 3800 / 4000 mbar |
| `fresh_s` | 20 s |
| `blind_disarm_s` | 180 s (the GHS ESP32 routinely stalls 5–80 s) |
| `xenon_present_mbar` | above the empty-system cube reading (about 200 mbar today) |
| `pump_open_max_mbar` | 10 mbar |
| `pump_open_with` | empty |
| `heaters.pv_cross_check_k` | 15 K |

The rails cap what Claude may *command*. The PV interlock still caps the
temperature for everyone, Claude included.

Every AI action, refusal and dry run, with Claude's stated reason and the
client, is listed under **recent AI activity** (the last 10). The last 20 are
at `http://xbox-pi:8089/`; the full log is `aicontrol/audit.jsonl` on the Pi
(and `audit.jsonl.1` … `.5` after rotation, 5 MB each). Notes are limited to 6
a minute, and calls that send nothing (refusals, no-ops, dry runs) beyond 30 a
minute are counted and summed up in one entry, so a looping session cannot
flood the log. A note is shown as "note by AI client …", never as yours.

### If the AI did something unexpected

1. **Disarm** on the card (or **disarm all**). No code is needed, and nothing
   moves.
2. Find the entry in **recent AI activity** (or `aicontrol/audit.jsonl`). An
   action that sent something records `previous`: the setpoint and
   expression, PV source, supply V / I / output, or valve state and auto flags
   as they were before. Put those back on the matching card.
3. Entries of kind `guard` (a pressure trip: what was disarmed and which
   setpoints were lowered) and `pv_check` (a PV source that failed, and
   whether the service put the zone back on its cube RTD) are the service's
   own actions. `auth_failed` entries are refused codes: if you did not send
   them, someone else tried to arm.

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
   `get_ai_control`, then (nothing armed) for a heater setpoint: the first must
   say `you_are: AI client operator` (the token arrived), the second must be
   refused with "not armed" (acceptance step 6 in
   `aicontrol/operator/README.md`). The journal shows
   the protocol era of the first request at INFO; start the service once with
   `XSPHERE_AICONTROL_LOG_LEVEL=DEBUG` to see every `tools/call`. Note the
   date in the operator README's "Verified on" paragraph.

### Connecting Claude

To confine a session to the AI control tools, copy the operator profile
(`aicontrol/operator/`, see its README) to `C:\Users\xbox\xsphere-ai-operator`,
set `XSPHERE_AI_TOKEN` for the user that runs it, open Claude Code there, and
run the acceptance check.

Any other Claude Code session can add the tools with
`claude mcp add --transport http xsphere http://xbox-pi:8089/mcp`. Without a
write token it can only read, but it keeps its shell and browser.

### Limitations

Arms, rails and the guard only bind clients that go through this service. The
MQTT broker is anonymous, `/api/cmd` on the Control page relays commands, and
code-server and Node-RED are open on the LAN. A session with a shell or a
browser can still send hardware commands directly. It can no longer arm the AI
or save rails without your code, nor make AI writes without a token. Use the
operator profile for any AI that should be confined.

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
