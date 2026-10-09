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
| **Gas Handling** | MKS flow rate and setpoint; flow path selection; pressure and vacuum gauges; lab environment |
| **Interlocks** | Active alert list; overall ok/not-ok indicator |
| **Gradient Scan** | Configure and start an automated temperature scan |

---

## Starting and stopping the system

### Service control GUI (easiest)

A small GUI does start / restart / stop / logs for both services, from your
Windows desktop over SSH or from the Pi itself. Launch it from the **xSphere
Slow Control** desktop shortcut, or:

```bash
python -m slowcontrol.servicectl
```

It is also the **Services** tab of the main `slowcontrol.gui` window.

The window opens with the full `systemctl status` block for both units in its
Output pane, prints it again for a unit after each start / restart / stop, and
**Print Status** fetches it on demand. The coloured dot beside each unit
refreshes every 5 s (every 30 s while the Pi cannot be reached).

First-time setup is in [SETUP.md](SETUP.md#service-control-gui) — it needs an
SSH key and one sudoers snippet on the Pi. Without those, the GUI will tell you
exactly which one is missing rather than hanging on a password prompt.

The same module works as a command line tool:

```bash
python -m slowcontrol.servicectl status
```

```bash
python -m slowcontrol.servicectl restart slowcontrol
```

### From a shell on the Pi

`scripts/slowcontrol-ctl.sh` wraps both units:

```bash
./scripts/slowcontrol-ctl.sh restart          # both services
```

```bash
./scripts/slowcontrol-ctl.sh follow slowcontrol
```

Actions: `start`, `stop`, `restart`, `status`, `logs`, `follow`, `enable`,
`disable`. Services: `slowcontrol` (`sc`), `omega` (`om`), `all`.

> **Stopping the slow control service closes the gas path.** Its PLC watchdog
> counter (DS1099) stops advancing, so the ladder shuts the MKS valve and
> drives the flow setpoint to 0 V. That is the intended fail-safe — but do not
> stop the service mid-fill and expect flow to continue. The GUI asks for
> confirmation before stopping; the shell script prints a warning.

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

The autovalve controller manages three solenoid valves:
- **XV1 (ballast)** — fills the ballast LN2 dewar
- **XV2 (primary_xe)** — fills the primary xenon dewar
- **XV3 (cryostat)** — fills the cryostat LN2 vessel

Each valve has two independent enable flags:
- **auto_open** — service opens the valve when level falls below `level_low`
- **auto_close** — service closes the valve when level rises above `level_high`

Both flags are **disabled by default at startup**.  You must explicitly arm
them before autofill operates.

The cryostat has one extra, optional mechanism: **coast**, which can withhold
an `auto_open` until the cold block has given up its thermal reserve.  Coast
gates `auto_open` **only** — it can never inhibit `auto_close`, the fill
timeout, or a manual valve command.  See "Coast mode" below.

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

# Arm cryostat autofill
mosquitto_pub -h localhost -t xsphere/commands/valve/cryostat/auto_open  \
  -m '{"enabled": true}'
mosquitto_pub -h localhost -t xsphere/commands/valve/cryostat/auto_close \
  -m '{"enabled": true}'
```

If coast is in use on the cryostat, arm coast **before** `auto_open` — see
"Coast mode" below.

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

A manual open is honoured during a coast as well: coast gates `auto_open`
only and is never consulted for a commanded valve state.  The fill timeout
still bounds the open.  Coast itself does not notice — it keeps reporting
`coasting` and keeps DS1107 at 0 until the level you have just restored
rises back past `empty_threshold`, at which point it moves to `monitoring`.

### Temperature-gated cryostat fill (GUI)

The GUI's **Cryostat** tab does the same thing as the `mosquitto_pub` above,
but refuses to send the open unless a chosen pair of temperature sensors has
converged — a one-button version of the coast decision, taken by you rather
than by the controller.

```bash
python -m slowcontrol.gui -c slowcontrol/config.yaml
```

The tab shows the live LN2 level (DF303), the ladder's fill-status latch
(DS3), the XV3 present/desired state and the auto-close/auto-open flags, then
a gate you configure:

| Field | Meaning | Default |
|---|---|---|
| Ti (warm / load) | the heated sensor | `coast.sensor_warm` (`plc/rtd/2`) |
| Tj (cold / sink) | the cold sensor | `coast.sensor_cold` (`plc/rtd/4`) |
| Comparison | `Ti - Tj` (signed) or `\|Ti - Tj\|` (absolute) | `coast.delta_mode` (signed) |
| Threshold X | open permitted below this | `coast.delta_max_k` (40 K) |

Defaults come from the cryostat `coast:` block, so the button and the
automatic coast gate start out agreeing. Changing them in the GUI affects
only the button; it does not rewrite the config or retune the controller.

**"Open XV3 (desired = 1)"** is disabled unless the gate passes, and the gate
is re-evaluated on click rather than trusting what is on screen. It refuses
whenever it cannot produce a number — no MQTT link, a reading older than 15 s,
a non-numeric threshold, or the same channel picked for both sensors. Note
that this is the opposite of the automatic coast gate's failure direction:
coast releases its inhibit when it cannot evaluate a term, because the
alternative is stranding a vessel empty, whereas the button does nothing,
because the alternative is opening a cryogen valve on evidence you cannot see.

**"Close XV3 (desired = 0)"** is never gated.

Two things the button does *not* do:

- It does not check the level. If DS3 already reads FULL and auto-close is
  armed, ladder rung 28 closes XV3 again within a scan or two of the manual
  open. The panel warns when it is about to happen rather than letting the
  button look broken.
- It does not bypass anything downstream. `auto_close`, the ladder lockout and
  the fill timeout all still apply, and the command travels the ordinary
  `xsphere/commands/valve/cryostat/state` topic, so the service remains the
  only writer on the PLC connection.

### Gas-side solenoid valves (GUI)

Below the XV3 buttons, the **Cryostat** tab has an Open and a Close button for
each of the three relay-actuated gas valves:

| Row | MQTT name | Register | Output |
|---|---|---|---|
| Ballast valve | `gas_ballast` | DS151 | Y105 |
| Pump valve | `gas_pump` | DS152 | Y106 |
| Bottle valve | `gas_bottle` | DS153 | Y107 |

Open writes 1 to the register and Close writes 0; the ladder SETs the output
while the register is 1 and RSTs it otherwise. Each row shows the output coil
as read back from the PLC next to the register value, e.g.
`OPEN   (desired 1)`, and `— stale` if the service has not reported in 15 s.

The same commands from a shell:
```bash
mosquitto_pub -h localhost -t xsphere/commands/valve/gas_ballast/state -m '{"state": 1}'
mosquitto_pub -h localhost -t xsphere/commands/valve/gas_ballast/state -m '{"state": 0}'
```

These buttons are **not gated on anything** — no temperature gate, no level,
no interlock between the three — and each click only asks for confirmation.
Nothing closes these valves automatically either: there is no timer or
auto-close behind them and the service watchdog does not cover them, so they
stay where they were last put through a service restart or a PLC power cycle.
Note that `gas_ballast` is not `ballast`: the latter is XV1, the LN2 fill
valve for the ballast bottle's cryoflask.

### Fill timeout safety

If a valve is open for longer than `fill_timeout_s` (default: 600 s ballast /
primary_xe, 920 s cryostat) without the level reaching `level_high`, the
service forces the valve closed and publishes an alert:
```
xsphere/alerts/fill_timeout/{vessel}
```
Check that the dewar supply line is not blocked.  Increase `fill_timeout_s`
in `config.yaml` if legitimate fills are taking longer than expected.

### Coast mode

Coast is an optional gate on the **cryostat's** `auto_open` decision, and the
cryostat is the only vessel it is configured for.  Plain autofill refills as
soon as the filtered level falls through `level_low` (0.25 for the cryostat),
which tops the vessel up while there is still cooling authority left in it.
Coast instead lets the vessel run dry and keeps the cube on temperature from
the thermal mass of its cold block, refilling only once that reserve is
spent.

A refill is permitted only when **both** terms hold:

| Term | Condition | Default |
|---|---|---|
| The vessel is actually empty | filtered level < `empty_threshold` | 0.13 (estimate — calibrate) |
| The cold reserve is spent | (T_warm − T_cold) < `delta_max_k` | 40 K (estimate) |

`confirm_s` (60 s) is a dwell on the second term: the ΔT must stay below
`delta_max_k` for that long before the valve opens, and
`delta_hysteresis_k` (5 K) is how far back above the threshold ΔT has to go
before the gate re-inhibits.  The control loop re-evaluates every vessel
every 10 s, which is what lets a temperature-only change move the valve — no
new level reading is required.

Coast gates `auto_open` **only**.  It can never inhibit `auto_close`, the
fill timeout, or a manual valve command.  Every condition it cannot
evaluate — a stale or missing level, a stale, missing or implausible RTD, a
reversed sensor pair, or a level channel claiming liquid while the cold sink
reads dry — releases the inhibit, reverts to the ordinary `level_low`
behaviour, and raises `coast_unverified`.  The failure direction is always
"spend LN2", never "leave the vessel empty and warming".

> **Coast deliberately runs the cryostat dry, and that inverts the polarity
> of the level-calibration risk noted at the top of this section.**  Under
> plain autofill a miscalibrated threshold causes over-filling: wasteful,
> but visible at the dewar and obvious on the level trace.  Under coast a
> gate that never converges strands the vessel empty and warming, which is
> silent — the dashboard shows an armed autofill that simply has not fired.
> Read `xsphere/status/coast/cryostat`, not the valve state, to find out why
> a fill has not started.

**The sensor pair.** The default pair is `sensor_warm: plc/rtd/2` (DF2, Xe
cube bottom — the heated load) against `sensor_cold: plc/rtd/4` (DF4, LN2
vessel base — the cold sink).  With liquid in the vessel the difference is
around 88 K, and it shrinks as the block dries.  `delta_mode` defaults to
`signed` rather than `absolute` because a cold sensor reading hotter than
the load it is supposed to be cooling is a wiring or scaling fault, and an
absolute value would read that fault as convergence.

> **Do not pair `plc/rtd/1` with `plc/rtd/2`.** That difference *is* the
> commanded vertical gradient (the gradient controller's ΔV), so the gate
> would sit near 0 K, be satisfied permanently, and coast would silently
> never engage.  Every fill would look normal while nothing was being saved.

**The DF4 threshold ladder.** Four numbers watch the same cold-sink sensor,
so it helps to see them in order:

| DF4 reading | Meaning | Key |
|---|---|---|
| ~77 K | LN2 plateau — the vessel is wet | — |
| < 90 K | cold enough to prove a refill landed | `sink_cold_k` |
| ~125 K | ΔT gate satisfied against a 165 K load | none (`base_k_nominal` − `delta_max_k`) |
| > 135 K | the cold sink itself is dry — backstop | `sink_warm_k` |

The 125 K row is not a configured key: it is where a 40 K `delta_max_k`
lands when the load sits at the 165 K nominal base.  Config validation
refuses a `sink_warm_k` at or below that value, because the backstop would
then always trip before the gate could act and `delta_max_k` would be
inoperative.

**Backstops.** Three conditions force a refill regardless of the gate, raise
`xsphere/alerts/coast_backstop/{vessel}`, and latch coast off.  They act on
the first tick they are true — the `confirm_s` dwell only ever delays a
fill, never a backstop.

| Backstop | Trips when | Default |
|---|---|---|
| `max_duration_s` | the vessel has read empty for this long | 5400 s (sanity bound, not tuned) |
| `ceiling_k` | any of DF1, DF2 or DF3 goes above this | 175 K |
| `sink_warm_k` | the cold sensor goes above this | 135 K |

`ceiling_k` watches all three cube RTDs, not the configured pair, because it
is there to protect the xenon rather than to judge the reserve.  A latch
clears only when the vessel is **proven wet again** by two independent
sensors agreeing: filtered level >= `level_high` (2.5) *and* the cold sensor
below `sink_cold_k` (90 K).  Until then coast reports `latched_off` and
behaves exactly like plain autofill.  Backstops are config-file only and are
deliberately not retunable over MQTT.

**Coast states**, as reported in `state`:

| State | Meaning |
|---|---|
| `disarmed` | not configured, disabled in `config.yaml`, or not armed |
| `latched_off` | a backstop fired; waiting for proof the vessel is wet |
| `released` | data unusable; inhibit released, `coast_unverified` raised |
| `monitoring` | level still above `empty_threshold`; waiting for it to empty |
| `coasting` | empty and withholding the fill — the only inhibiting state |
| `converged` | ΔT held below `delta_max_k` for `confirm_s`; refilling |
| `backstop:max_duration` | forced refill, the episode ran too long |
| `backstop:sink_warm` | forced refill, the cold sink read warm |
| `backstop:ceiling` | forced refill, a cube RTD passed `ceiling_k` |

**Arming coast.** Coast always comes up **disarmed**.  There is deliberately
no arm-on-start key: deliberately running a cryostat dry is not something a
service restart or a config reload should re-enter on its own.  Set
`coast.enabled: true` in `config.yaml` first — that only makes coast
*armable* — then arm it explicitly, either with the **Cryostat coast mode**
switch in the **Coast Refill (Cryostat)** group of the **Level / Valves**
tab, or over MQTT:

```bash
# Arm coast on the cryostat
mosquitto_pub -h localhost -t xsphere/commands/valve/cryostat/coast \
  -m '{"enabled": true}'

# Disarm (also clears any latch, the episode clock and the ΔT dwell)
mosquitto_pub -h localhost -t xsphere/commands/valve/cryostat/coast \
  -m '{"enabled": false}'
```

Never publish that topic retained (`-r`).  A retained arm would survive a
restart and defeat the disarmed-at-startup guarantee.  An arm request is
refused, with `xsphere/alerts/coast_refused/{vessel}`, if `coast.enabled` is
`false` for the vessel.

**Retuning at runtime.** Seven keys can be changed without a restart —
`empty_threshold`, `sensor_warm`, `sensor_cold`, `delta_max_k`,
`delta_mode`, `delta_hysteresis_k` and `confirm_s`.  They are validated by
the same predicates as the file loader, applied all-or-nothing, and never
persisted: `config.yaml` wins after a restart.  Use the **Retune coast
gate** form on the dashboard, or publish:

```bash
# Loosen the gate to 50 K and require a 120 s dwell
mosquitto_pub -h localhost -t xsphere/commands/valve/cryostat/coast_config \
  -m '{"delta_max_k": 50.0, "confirm_s": 120.0}'
```

Anything else in the payload — a backstop key, a typo, a non-numeric
value — rejects the whole message and raises
`xsphere/alerts/coast_refused/{vessel}`.  An accepted change resets the
dwell, so the gate has to re-earn its `confirm_s` against the new threshold.

**Watching a coast.** The status document is retained, so the answer to "why
has this not refilled yet" is on screen the moment you subscribe:

```bash
mosquitto_sub -h localhost -t 'xsphere/status/coast/cryostat' -v
```

```json
{
  "vessel": "cryostat",
  "enabled": true,
  "armed": true,
  "state": "coasting",
  "hold_reason": "ΔT 62.4 K still above 40.0 K — coasting on thermal mass",
  "permit_open": false,
  "open_threshold": 0.13,
  "level": 0.0912,
  "empty_threshold": 0.13,
  "sensor_warm": "plc/rtd/2",
  "sensor_cold": "plc/rtd/4",
  "t_warm_k": 164.82,
  "t_cold_k": 102.4,
  "delta_k": 62.42,
  "delta_max_k": 40.0,
  "delta_mode": "signed",
  "confirm_s": 60.0,
  "dwell_s": null,
  "elapsed_s": 1284.0,
  "max_duration_s": 5400.0,
  "ceiling_k": 175.0,
  "ceiling_max_k": 164.82,
  "sink_warm_k": 135.0,
  "latched_off": false,
  "release_reason": null,
  "ladder_rung_entered": true,
  "ladder_permit": 0,
  "ladder_permit_readback": 0
}
```

`hold_reason` is one operator-readable sentence for the current state and is
the field to read first.  `elapsed_s` is how long the vessel has read empty,
against `max_duration_s`.  `ladder_permit` is what the service last
commanded into DS1107 and `ladder_permit_readback` is what the PLC actually
holds — if the two differ, the permit write is not landing and the ladder is
not being gated.

The filtered level has its own topic (not retained; it publishes on every
level reading):

```bash
mosquitto_sub -h localhost -t 'xsphere/status/level/cryostat' -v
```

**Before you arm coast the first time.** Coast needs CLICK ladder addition 6
("XV3 coast-aware auto-open") hand-entered in the CLICK programming
software — the `.ckp` project file is a password-encrypted blob, so it
cannot be patched from the repo.  The rung adds DS1107, a retentive integer
coast permit with an initial value of 1, so a PLC project without the rung
behaves exactly as it does today.  Until the rung exists, the ladder's own
XV3 auto-open (DS3 = 0 and DF303 > 0.25) tops the vessel up during the
descent, coast never sees an empty vessel, and the feature silently does
nothing.  Full rung text is under "Required CLICK ladder additions" in
SYSTEM_ARCHITECTURE.md.

The ladder export currently in `extras/plc-block-2.pdf` does **not** contain
DS1107 anywhere in its 36 rungs, so as of that export the rung is still
outstanding.

Set `coast.ladder_rung_entered: true` only after the rung is entered and
tested.  Arming with it `false` is *allowed* and logs a loud warning: that
failure wastes LN2 rather than endangering anything, but it looks exactly
like a broken feature, so check the log before concluding coast is broken.

If ladder timer T3 is raised to 1800 s to accommodate the much longer
from-dry transfer, raise `autovalve.vessels.cryostat.fill_timeout_s` to 1800
in the **same visit**.  The two must match.

> **After a restart, re-arm coast BEFORE re-arming `auto_open`.** Coast comes
> up disarmed and `auto_open` is a separate command, and a disarmed coast
> means the ordinary `level_low` = 0.25 threshold is back in force.  If the
> vessel is sitting below `empty_threshold` mid-coast, arming `auto_open` on
> its own fills it immediately.  That is fail-safe — a fill, never a
> strand — but it silently ends the coast and nothing on the dashboard says
> so.

On a clean shutdown the PLC driver writes DS1107 = 1 directly over Modbus,
handing the backup fill back to the ladder.  An unclean death is covered by
the rung's own watchdog-stale branch, which restores autonomous filling
about 10 s after DS1099 stops advancing.

---

## Gas flow control (MKS M330B + bypass)

### The two parallel legs

The MKS mass flow controller and the pneumatic bypass valve sit between the
same two tees, so gas takes one leg or the other:

| Path | MKS valve | Bypass | Use |
|---|---|---|---|
| `mks` | NORMAL (follows setpoint) | closed | Metered forward flow |
| `bypass` | CLOSED | open | Unmetered return / recovery flow |
| `isolated` | CLOSED | closed | Both legs shut |

Use the three **Route** buttons on the **Gas Handling** tab rather than moving
valves individually — they always close the leg that is being left before
opening the leg being entered, so the two are never both open in transit.

Having both legs open is treated as a fault: an unknown fraction of the gas
takes the unmetered path, so the flow reading becomes meaningless. The service
closes the bypass and raises `xsphere/alerts/gasflow_path_conflict`.

### MKS valve modes

The MFC valve has three states, not two:

| Dashboard button | Wire value | What it does |
|---|---|---|
| **CLOSE (hard shut)** | `"closed"` | Close override asserted; valve driven shut, setpoint ignored |
| **OPEN (follow setpoint)** | `"normal"` | Both overrides released; the MFC's own loop controls flow to the setpoint |
| **PURGE (force full open)** | `"open"` *(alias `"purge"`)* | Open override asserted; valve forced fully open — **flow is unmetered and uncontrolled** |

> **Watch the wire value.** The button labelled *OPEN* sends `"normal"`, and
> the value `"open"` is **PURGE**. Publishing `{"mode":"open"}` by hand forces
> the valve wide open — it does not put the MFC into setpoint control. The
> alias `"purge"` is accepted and is the safer thing to type when you mean it.

PURGE bypasses flow control entirely. It exists for pump-down and line
clearing — do not use it as a "flow faster" button.

> **Neither CLOSE nor a 0 sccm setpoint is a positive shutoff.** MKS documents
> leak-by of 0.1–1% of full scale through a closed MFC valve. If you need true
> no-flow — long idle periods, or isolating the xenon inventory — close a
> separate valve in series. Use the **Isolate** path button, which also shuts
> the bypass, and back it up with a hand valve for anything unattended.

A 0 V setpoint does drive the valve fully closed (MKS specifies a threshold
around 1% of full scale below which the valve shuts), so **Zero Setpoint** and
**CLOSE** end up in a similar place — but only CLOSE is immune to something
later writing a nonzero setpoint.

### Warm-up

The M330 family needs roughly **30 minutes** from power-on before the flow
reading is trustworthy. For the first one to two minutes the flow output sits
near +7 V while the sensor heaters stabilise; the dashboard shows this as an
over-range reading and the service logs a warning. Keep gas isolated until the
reading has settled — MKS warns that erroneous flow can occur before the unit
stabilises.

### Setting a flow rate

On the **Gas Handling** tab: enter the value in **Setpoint (sccm)**, then press
**OPEN (follow setpoint)** if the valve is not already in normal mode.

Via MQTT:
```bash
# Set 50 sccm
mosquitto_pub -h localhost -t xsphere/commands/flow/mks/setpoint \
  -m '{"value_sccm": 50.0}'

# Or as a percentage of full scale
mosquitto_pub -h localhost -t xsphere/commands/flow/mks/setpoint \
  -m '{"percent": 25.0}'
```

Setpoints are clamped to `0 … gasflow.mks.setpoint_max_pct` percent of full
scale; a clamped request is logged as a warning.

### Valve and path commands via MQTT

```bash
# MKS valve mode
mosquitto_pub -h localhost -t xsphere/commands/valve/mks/mode -m '{"mode":"normal"}'  # follow setpoint
mosquitto_pub -h localhost -t xsphere/commands/valve/mks/mode -m '{"mode":"closed"}'  # hard shut
mosquitto_pub -h localhost -t xsphere/commands/valve/mks/mode -m '{"mode":"purge"}'   # FORCE FULL OPEN, unmetered

# Bypass valve on its own
mosquitto_pub -h localhost -t xsphere/commands/valve/bypass/state -m '{"state":1}'

# Whole path at once (preferred)
mosquitto_pub -h localhost -t xsphere/commands/gasflow/path -m '{"path":"mks"}'
mosquitto_pub -h localhost -t xsphere/commands/gasflow/path -m '{"path":"bypass"}'
mosquitto_pub -h localhost -t xsphere/commands/gasflow/path -m '{"path":"isolated"}'
```

### Reading the flow

```bash
mosquitto_sub -h localhost -t 'xsphere/sensors/flow/mks' -v
mosquitto_sub -h localhost -t 'xsphere/status/gasflow' -v
```

`value_sccm` is the flow in the **actual** gas — the raw N2-equivalent reading
scaled by `gasflow.mks.gas_correction_factor`. `value_sccm_cal` is the
unscaled reading in the calibration gas. If the MFC was calibrated directly
for xenon, leave the GCF at 1.0 and the two are identical.

### Calibration values you must set before first use

In `slowcontrol/config.yaml` under `gasflow.mks`, read these off the
calibration sticker on the flowbody:

| Key | Meaning |
|---|---|
| `full_scale_sccm` | Full-scale flow for the calibration gas |
| `gas_name` | Gas the unit was calibrated with |
| `gas_correction_factor` | Multiplier to the actual gas (1.0 if calibrated for Xe) |
| `signal_span_v` | 5.0 for the standard MKS analog interface |

Until these match the physical device, every flow number on the dashboard is
wrong by a constant factor.

### Safety behaviour on service loss

The slow control service increments a watchdog counter (DS1099) in the PLC on
every poll. If the service crashes or the network drops, the PLC ladder forces
the MKS valve closed and the setpoint output to 0 V. Without that rung the PLC
would hold the last analog output value indefinitely and keep flowing — see
"Required CLICK ladder additions" in SYSTEM_ARCHITECTURE.md.

Restarting the service does **not** move any gas valve. `apply_default_on_start`
is `false` by default, so valves stay where the operator (or the PLC watchdog)
left them.

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
| `flow_stale/{device}` | No MFC flow update | > 30 s |
| `flow_while_isolated/{device}` | Flow with every gas leg shut | > 6% FS for 2 passes |
| `gasflow_path_conflict` | MKS valve open *and* bypass open | ~2 s sustained |

The autovalve controller raises three more alerts of its own on the same
`xsphere/alerts/` tree, on its 10 s control tick rather than the watchdog's
15 s pass:

| Rule | Condition | Cleared when |
|---|---|---|
| `coast_backstop/{vessel}` | A coast backstop forced a refill | the next control tick, ~10 s — the latch outlives the alert |
| `coast_unverified/{vessel}` | Coast released its inhibit on unusable data | the data becomes usable again |
| `coast_refused/{vessel}` | A coast arm or `coast_config` was rejected | the next accepted arm or `coast_config` |

### When an alert fires

1. The **Interlocks** tab will show the alert in red.
2. An MQTT message is published (retained) to `xsphere/alerts/{rule}/{channel}`.
3. The `xsphere/status/interlocks` topic updates with `"ok": false`.

Steps 1 and 3 apply to the watchdog rules only.  `fill_timeout` and the
three `coast_*` rules are published by the autovalve controller and are not
part of `xsphere/status/interlocks`, so they reach the retained
`xsphere/alerts/#` tree and the service log but do **not** turn the
Interlocks tab red.  Watch `xsphere/alerts/#` directly, or the coast group
on the **Level / Valves** tab, when a coast is running.

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
- **Not a fault while a cryostat coast is refilling.** A from-dry coast fill
  drops 77 K liquid onto a cold block that has warmed towards 125 K, a far
  larger step in cooling authority than the top-up this threshold was sized
  around, so a saturated zone for several minutes at the end of a coast
  episode is expected.  Check `state` in `xsphere/status/coast/cryostat`
  first: if it reads `converged` or `backstop:*`, the coast is doing what it
  was armed to do.  Do not act on the "check that the dewar is properly
  filled" advice above during a coast — deliberately not filling it is the
  whole point, and a manual fill silently ends the episode.  See "Coast
  mode" under LN2 autofill.

**flow_stale**: The MFC flow reading has stopped updating.
- This alert is armed from service start, so it also fires if the MFC has
  never published at all — check that the unit is powered and that DF201 is
  actually mapped to the analog input in the CLICK module setup.
- Check the Modbus connection; a stale flow reading usually means the whole
  PLC read path is down, so look for other `*_stale` alerts alongside it.
- Note the slow control service withholds its PLC watchdog kick while the gas
  registers are unreadable, so the ladder will shut the MFC. That is intended.

**flow_while_isolated**: Gas is moving with both legs commanded shut.
- Check that the bypass valve (XV4) actually seated, and that the MKS override
  contacts released — an open-override contact welded or oxidised closed would
  hold the MFC in purge.
- Remember the MFC leaks by 0.1–1% of full scale even when override-closed;
  the 6% threshold is set above that plus the analog error band. A reading
  only slightly over threshold on an uncalibrated system is more likely ADC
  offset than a real leak — trim `adc_offset_v` before chasing hardware.
- Back up with the series hand valve for anything unattended.

**gasflow_path_conflict**: The MKS valve is flowing while the bypass is open.
- The service closes the bypass automatically and keeps retrying while the
  conflict persists; the alert payload's `passes_unresolved` counts how long
  it has been trying. A rising count means the close command is not taking —
  check the XV4 pilot and the Y104 relay.
- Until it clears, the flow reading is meaningless: an unknown fraction of the
  gas is taking the unmetered bypass.

**coast_backstop**: A coast backstop forced a refill and latched coast off.
- Read `state` and `release_reason` in `xsphere/status/coast/{vessel}`.
  `backstop:sink_warm` means DF4 passed 135 K, `backstop:ceiling` means one
  of DF1–DF3 passed 175 K, `backstop:max_duration` means the vessel read
  empty for longer than 5400 s.
- The refill is already happening — the backstop releases the gate, it does
  not close a valve.  Nothing needs doing to make LN2 flow.
- Coast stays latched off until the vessel is proven wet again (filtered
  level >= `level_high` *and* DF4 below 90 K).  It then has to be re-armed
  explicitly; a latch clearing does not re-arm it.
- A `sink_warm` trip every episode means the gate can never fire before the
  backstop: the gate wants DF4 above (load − `delta_max_k`), and if that
  lands above 135 K the backstop always wins.  Load validation only rules
  that out for a load at `base_k_nominal` (165 K), so this is what a cube
  running hotter than 165 K looks like.  Raise `delta_max_k`, and set
  `base_k_nominal` to the real operating temperature so the file loader
  catches the next such combination.
- A `ceiling` trip is a genuine cube over-temperature (DF1–DF3 above 175 K,
  well past the 161.4 K xenon triple point) and should be treated as such
  even though coast handled it — check the PID zones, not the gate.
- Repeated `max_duration` trips mean the reserve outlasts the sanity bound,
  so 5400 s is the wrong number for this system rather than evidence of a
  fault.  It is a config-file key: edit it and restart.

**coast_unverified**: Coast released its inhibit because it could not judge
the gate, and the vessel reverted to the ordinary `level_low` threshold.
- This is the designed failure direction, not a hazard: an unverifiable gate
  spends LN2 rather than leaving the vessel empty and warming.  Nothing is
  at risk while it is active, but coast is saving nothing.
- `hold_reason` names the failing input verbatim — a stale or never-seen
  level, a stale, missing or implausible RTD, a reversed pair, or a level
  channel reading liquid while DF4 reads dry.
- A reversed pair ("cold sensor ... is above the load") means
  `sensor_warm`/`sensor_cold` are swapped, or an RTD is miswired or scaled
  wrong.  Fix the assignment; do not switch `delta_mode` to `absolute` to
  make the message go away, because that turns the fault into an apparently
  satisfied gate.
- "level says liquid but DF4 reads dry" is the dangerous combination that
  the check exists for: a level channel stuck high with a genuinely dry
  vessel.  Trust DF4 and check the capacitance probe.
- Expect this alongside `temperature_stale` or `level_stale` — those name
  the underlying sensor and are the ones to chase.

**coast_refused**: An arm request or a `coast_config` retune was rejected;
nothing was applied.
- The message text says which.  An arm is refused when `coast.enabled` is
  `false` for that vessel in `config.yaml` — set it and restart.
- A `coast_config` is refused whole if it names any key outside the seven
  runtime-tunable ones (the backstops are deliberately file-only), if a
  value is not numeric, or if the resulting configuration fails the same
  validation the file loader applies — for example an `empty_threshold`
  above `level_low`, a `delta_max_k` below 10 K, or `sensor_warm` equal to
  `sensor_cold`.
- The previous configuration is still in force, so there is no half-applied
  state to undo.  Fix the payload and republish.

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
6. Leave cryostat coast **disarmed** through the cool-down.  Arm it only
   after commissioning: `empty_threshold` has to come from a supervised
   boil-dry, and CLICK ladder addition 6 has to be in the PLC, or coast is
   either unsafe to trust or a no-op.  Arming coast is never part of getting
   cold; it is a steady-state LN2-saving measure.

### Warming up

Reverse of cooling:
1. **Disarm cryostat coast first**, before touching autofill.  A warming
   system satisfies the ΔT gate trivially: the load and the sink converge
   because everything is heading for room temperature, not because a
   reserve was spent, so an armed coast on an empty vessel reports
   `converged` and permits a fill.  It also holds DS1107 at 0 while it is
   `monitoring`, which inhibits the ladder's own backup fill.  The
   `sink_warm_k` backstop does eventually catch it — DF4 passes 135 K on the
   way up and coast latches off — but it gets there via a nuisance
   `coast_backstop` alert on every warm-up.
2. Disarm autofill (prevents unnecessary LN2 fills during warmup).
3. Ramp base_k upward using the gradient scanner or manual slider.
4. At ~200 K, confirm xenon has fully evaporated before disconnecting gas lines.

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
| `level` | `vessel` | `raw`, `filtered`, `fill_status` (cryostat only) |
| `pressure` | `gauge` | `value_psi` |
| `vacuum` | `gauge` | `value_mbar` |
| `pid` | `zone` | `setpoint_k`, `pv_k`, `output_pct` |
| `environment` | `sensor` | `temperature_c`, `humidity_pct`, `pressure_hpa` |

`level.fill_status` is the CLICK ladder's own DS3 latch — `0` = empty,
`1` = full, held between 0.8 and 2.5 — so plotting it as a step against
`filtered` shows what the valve logic actually decided, which is not always
what a threshold line drawn across the level would suggest.

> Only `vessel="cryostat"` currently reaches InfluxDB at all. The ballast and
> primary_xe ESP32s publish `raw_pf` on the same topic rather than `raw`, so
> Telegraf's parser finds none of its configured paths and drops those
> messages. Pre-existing, and unrelated to `fill_status`.

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
| `autovalve.vessels.*.level_high` | `config.yaml` | Close valve threshold, pF at the capacitance probe |
| `autovalve.vessels.*.level_low` | `config.yaml` | Open valve threshold, pF at the capacitance probe |
| `autovalve.vessels.*.fill_timeout_s` | `config.yaml` | Max fill duration safety |
| `heartbeat_interval` | `config.yaml` | Heartbeat publish interval |
| Interlock thresholds | `controllers/interlocks.py` (top of file) | Stale/range/saturation limits |

The two level thresholds and `coast.empty_threshold` are all in the same
units — pF as read by the FDC1004 capacitance probe, not litres and not a
percentage.  Nothing in the repo maps pF to a liquid volume, so every one of
them has to be set from measured readings on the actual vessel.

Interlock thresholds (`TEMP_MIN_K`, `TEMP_MAX_K`, `TEMP_STALE_S`, etc.) are
constants at the top of `slowcontrol/controllers/interlocks.py`.  They are not
yet exposed in `config.yaml` — edit the file directly and restart the service.

### Coast parameters

All of these live under `autovalve.vessels.*.coast` and only the cryostat
has the block.  The whole block is optional: omit it and coast never applies
to that vessel.  An unknown key here is fatal at load rather than ignored,
because a typo would otherwise leave the default silently in force.

| Parameter | Location | Effect |
|---|---|---|
| `autovalve.vessels.*.coast.enabled` | `config.yaml` | Allow coast to be armed at runtime; `false` refuses every arm request |
| `autovalve.vessels.*.coast.ladder_rung_entered` | `config.yaml` | Declare CLICK ladder addition 6 hand-entered; `false` still arms but logs a loud warning |
| `autovalve.vessels.*.coast.empty_threshold` | `config.yaml` | Permit a refill below this filtered level (pF).  Must be in (0, `level_low`] |
| `autovalve.vessels.*.coast.sensor_warm` | `config.yaml` | MQTT channel for the heated load, e.g. `plc/rtd/2` |
| `autovalve.vessels.*.coast.sensor_cold` | `config.yaml` | MQTT channel for the cold sink, e.g. `plc/rtd/4`.  Must differ from `sensor_warm` |
| `autovalve.vessels.*.coast.delta_max_k` | `config.yaml` | Permit a refill below this (T_warm − T_cold), in K.  Minimum 10 K |
| `autovalve.vessels.*.coast.delta_mode` | `config.yaml` | `signed` or `absolute`; keep `signed` so a reversed pair reads as a fault, not convergence |
| `autovalve.vessels.*.coast.delta_hysteresis_k` | `config.yaml` | Re-inhibit only above `delta_max_k` + this, in K |
| `autovalve.vessels.*.coast.confirm_s` | `config.yaml` | Hold the ΔT gate this long, in s, before opening |
| `autovalve.vessels.*.coast.base_k_nominal` | `config.yaml` | Operating load temperature, in K; only used to reject a `sink_warm_k` that would pre-empt the gate |
| `autovalve.vessels.*.coast.max_duration_s` | `config.yaml` | End the episode unconditionally after this many s of an empty vessel |
| `autovalve.vessels.*.coast.ceiling_k` | `config.yaml` | End the episode if any cube RTD (DF1–DF3) passes this, in K.  Must be in (`base_k_nominal`, 200 K] |
| `autovalve.vessels.*.coast.sink_warm_k` | `config.yaml` | End the episode if the cold sensor passes this, in K — the sink is dry |
| `autovalve.vessels.*.coast.sink_cold_k` | `config.yaml` | Treat a latched coast as clearable once the cold sensor drops below this, in K |
| `autovalve.vessels.*.coast.temp_stale_s` | `config.yaml` | Release the inhibit on a temperature older than this, in s |
| `autovalve.vessels.*.coast.level_stale_s` | `config.yaml` | Release the inhibit on a level older than this, in s |
| `autovalve.vessels.*.coast.temp_min_k` | `config.yaml` | Release the inhibit on a temperature below this, in K |
| `autovalve.vessels.*.coast.temp_max_k` | `config.yaml` | Release the inhibit on a temperature above this, in K |

The first group down to `confirm_s` — seven keys, `empty_threshold` through
`confirm_s`, excluding `enabled` and `ladder_rung_entered` — is also
settable over MQTT on `xsphere/commands/valve/{vessel}/coast_config` without
a restart, validated identically and never persisted.  Everything from
`base_k_nominal` down is a backstop or a data-quality limit and is **file
only**: no MQTT path exists to widen or remove it.

> **Three of these defaults are estimates, not measurements.**
> `empty_threshold` = 0.13 has no defensible default at all — nothing maps
> the probe's pF reading to litres, so it must be set to the measured dry
> reading plus 0.05 after a supervised boil-dry.  `delta_max_k` = 40 rests
> on an estimated thermal time constant rather than a measured one, and
> `max_duration_s` = 5400 is a sanity bound rather than a tuned value.
> Treat all three as starting points to be replaced by numbers from this
> cryostat.
