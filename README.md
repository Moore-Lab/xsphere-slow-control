# xsphere Slow Control

Slow control system for the xsphere cryostat experiment at Yale University.
Monitors and controls temperatures, LN2 fill levels, and gas handling for a
xenon cryostat used in levitated-particle physics experiments.

## What this system does

- **Reads temperatures** from the CLICK PLC (RTDs at the cryostat zones, base
  + clamps) and the LabJack T7 (cube RTDs + four type-K gradiometer TCs)
- **Controls heaters** via three PID zones (top / bottom / nozzle) on the PLC,
  with a Python gradient abstraction layer (gradient mode or per-zone absolute)
- **Manages LN2 autofill** for the ballast and primary xenon dewars via solenoid
  valves — the autofill ladder runs on the PLC; the Python service forwards
  level readings and exposes manual/arm controls
- **Monitors pressure and vacuum** from the gas-handling-system (GHS) ESP32
- **Holds a centralized state view** — the StateStore subscribes to every
  source topic, computes per-state freshness + moving averages + derived
  quantities, and republishes a consolidated snapshot on
  `xsphere/state/snapshot` (see [slowcontrol/STATE_LAYER_PLAN.md](slowcontrol/STATE_LAYER_PLAN.md))
- **Watches safety interlocks** — alerts on stale sensors, out-of-range
  temperatures, and saturated heater output
- **Logs everything** to InfluxDB via Telegraf, visualized in Grafana
- **Web GUIs** — a *register* page (everything, read-only) and a *control* page
  (valves, PID, gradient, automation), both schema-driven from the snapshot

## Using the web GUIs

Three browser pages, all served by the same Flask app on `xbox-pi:8088`:

| URL | What it is |
|---|---|
| `http://xbox-pi:8088/` | **Register GUI** — read-out of every state in the system, grouped, with freshness colouring |
| `http://xbox-pi:8088/control` | **Control GUI** — valves, PID, gradient, automation; each control shows the current state next to the input |
| `http://xbox-pi:8088/readme` | This page |
| `http://xbox-pi:3000/d/xsphere-slowcontrol` | **Grafana** — time-series history from InfluxDB |

The slow-control service publishes a consolidated snapshot (`xsphere/state/snapshot`, retained) every second; both GUIs read it via `/api/state` and update at ~1.5 s. If `/api/state` shows `mqtt: DOWN` or `snapshot_age` is large, the slow-control service is the suspect, not the GUI.

### Reading the register page

- Each card is one **group** of states (temperature, level, valve, pid, gradient, service, …).
- Every row shows: a label, the current value, the unit (analog) or yes/no + ● (binary) or the enum value, the moving average if one is configured (e.g. `⌀60s 207.13`), and a freshness chip.
- Freshness colouring:
  - **fresh** — updated within `1.5 × period_s`; trust the value.
  - **stale** (amber) — older than that; sensor probably skipped a few cycles, often clears on its own.
  - **invalid** (dim) — older than `5 × period_s`, or never received since the service started. **Don't trust the displayed value.**
- The header pills (`service`, `interlocks`, `labjack`, `ghs`, `mqtt`, fresh/stale/invalid counts, snapshot age) are the at-a-glance health check.

### Using the control page

Every control widget shows the **current** value of the thing it controls next to the input — the placeholder text in number inputs is the current value when the field is empty, and a small grey line below each control says "current …" so you know what setting the value would change.

**Valves** (one block per vessel — XV3 cryostat, XV2 primary Xe, XV1 ballast)

- "actual" ● = the energised state read back from the PLC; "desired" = the last command sent.
- `Open` / `Close` send the manual command (`xsphere/commands/valve/{vessel}/state`). The currently-desired button is highlighted green.
- `auto-open` / `auto-close` toggle the **PLC ladder's** autofill enables. They do **not** make Python the autofill brain — that's still the PLC. If both are armed, the PLC ladder watches the level and opens/closes the valve when thresholds are crossed (config in `state.yaml` / SYSTEM_ARCHITECTURE.md §6b).
- The Python autovalve controller is **disabled by default** (`config.yaml: autovalve.enabled: false`), to keep a single autofill authority.

**Pneumatic valves — gas handling** (ballast, pump, bottle — relay outputs Y105 / Y106 / Y107)

- `Open` / `Close` always mean the **valve**, not the relay, and send `xsphere/commands/pneumatic/{ballast|pump|bottle}/state`.
- The **ballast and bottle valves are normally open**: their relay is energised to *close* them. The **pump valve is normally closed**. The PLC driver applies that sense (`PNEUMATIC_VALVES` in `slowcontrol/drivers/plc.py`), so nothing else has to — change it there if a valve is ever re-plumbed.
- "actual" ● is worked out from the PLC output read back, "desired" from the register (DS151 / DS152 / DS153), and "relay" shows the output itself (`energised` / `off`). Seeing ballast `OPEN` with relay `off` is correct.
- With the PLC outputs off — power loss, PLC in STOP — ballast and bottle sit **open** and the pump valve shut.
- There is no auto-open, auto-close or timer behind these, in the ladder or in Python. A valve stays where it was last put, including through a restart of the slow-control service.
- Each one is also a Sequencer target, like any other control on this page.

**Heaters — PID** (one block per zone — top / bottom / nozzle)

- The summary line shows **PV** (process variable, actual temperature), **SET** (setpoint), **OUT** (heater output 0-100 %), and **err** (PV − SET).
- `setpoint (K)` + `set SP` writes the PID's setpoint register on the PLC.
  - **Important**: this only "sticks" when **gradient mode = absolute**. If gradient mode is `gradient`, the GradientController owns the per-zone setpoints and will overwrite anything you set here on the next gradient command. To control individual zones, switch to absolute first.
- `Kp / Ki / Kd` + `set gains` writes all three at once to the PLC's PID block. There's no commit-on-Tab — you must press the button.

**Gradient**

- `mode`: `gradient` (the GradientController computes the three PID setpoints from base + Δv + Δl) or `absolute` (each zone is set independently via the PID card).
- `base (top, K)` is the top zone's setpoint and the base of the gradient.
- `Δvertical (b−t, K)` adds to base for the bottom zone; `Δlongitudinal (n−t, K)` adds to base for the nozzle zone.
- The "current N K · measured M K" line shows the setpoint Δ and the **measured** Δ from the cube RTDs (the `dt_vertical_meas` / `dt_longitudinal_meas` derived states) — handy for seeing how well the gradient is actually realised.
- The `→ setpoints: top … · bottom … · nozzle …` line is the resulting per-zone setpoints the GradientController is publishing.


**Trackers — keep one state tied to another (+ offset)**

- A *tracker* writes one state to `source + offset` on every tick. Example: `pid_top_setpoint = t_cube_bottom + 10` keeps the top zone's PID setpoint always 10 K above the bottom-cube RTD.
- *target* must be an analog state with a `control:` block in `state.yaml` (every PID setpoint and the gradient parameters qualify); *source* can be any analog state.
- Set `offset = 0` for plain follow ("setpoint = sensor").
- Optional clamp via `min_value` / `max_value` (typed at the MQTT level; not currently in the GUI form).
- **Enabling a tracker writes the target on the next tick** — there's no grace period. Confirm `source + offset` is sane before flipping the toggle.
- Trackers persist to `slowcontrol/trackers.json` and come back on a service restart. Add/remove/enable them via the GUI's "Trackers" card or directly with MQTT: `xsphere/commands/trackers/{set,remove,enable}`.
- A PID-setpoint tracker only sticks when **gradient mode = `absolute`**. In `gradient` mode the GradientController overwrites the per-zone setpoints.

### Sequencer (the third tab)

`http://xbox-pi:8088/sequencer` — build and run an ordered list of steps. Each step is one or more *actions* plus a *hold time*; when the step is entered the actions fire, then it holds for the duration, then advances.

Two kinds of **program item**:

- **Step** — one or more actions plus a hold time. Each action is one of:
  - `set constant` — writes a target state to a number (e.g. `pid_top_setpoint = 170`). Target must be any analog state with a `control:` block in `state.yaml`.
  - `track source + offset` — creates or updates a *sequencer-owned* tracker (id prefix `seq:`) that keeps `target = source + offset` for the duration of this step. When a later step is entered, any sequencer-owned tracker not in that step's track actions is automatically removed.
- **Sweep** — a compact "scan one analog target from `start` to `stop` in increments of `step`, dwelling `dwell` at each value". Expanded inline at run time; one program item, N writes. Replaces the old standalone gradient scanner — sweep `gradient_base` to recover the old behaviour, or sweep `pid_top_setpoint` / `gradient_dv` / anything else with a `control:` block.

Workflow:

1. **Build a step (staging)** — pick a target, pick `set` or `track`, fill in the value (or source + offset), click `+ add action`. Repeat for multiple actions in the same step. Give the step a label + hold (minutes), click `+ add step to program`.
2. **Or add a sweep** — pick a target, fill in start / stop / step (in the target's units, usually K) and the per-point dwell (minutes). The card shows a live preview (`9 points · total 90 min`). Click `+ add sweep to program`.
3. The program list below shows all queued items; sweeps render as one row with `point 4/9` while running.
4. The program is persisted to `slowcontrol/sequencer.json` and survives a service restart.
5. `run` walks the items in order; `stop` aborts at the next check. A progress bar shows remaining time on the current (sub-)step.
6. At end-of-program **the last step's trackers are left in place** — the cryostat stays where the program landed it. Clean up the `seq:*` trackers from the **Control** page if needed.

Sequencer tips:

- Disable any conflicting manual trackers (Control page → Trackers card) before running, or both will write each tick.
- A `set` or `track` targeting `pid_*_setpoint` only sticks when `gradient_mode == absolute`; in `gradient` mode the GradientController overwrites per-zone setpoints. To use the gradient as the lever, target `gradient_base` / `gradient_dv` / `gradient_dl` instead.
- The program is mutable only when *not running* — `set`/`append`/`clear` are rejected with a log warning during a run.

### Adding a new tracked state

Edit `slowcontrol/state.yaml`, add an entry under the right `groups:` (or `derived:`) block, restart the slow-control service:

```bash
sudo systemctl restart xsphere-slowcontrol
```

It appears in the register GUI immediately and, if it has a `control:` block, in the control GUI **only if** a card references it (the bespoke cards are hand-wired). Validate the YAML before restarting:

```bash
cd /home/xbox/xsphere-slow-control && python -m slowcontrol.state.schema
```

See `slowcontrol/STATE_LAYER_PLAN.md` for the full field reference.

### Starting and stopping the services from the DAQ PC

`slowcontrol.servicectl` is a small window with Start / Restart / Stop / Logs for `xsphere-slowcontrol` and `xsphere-webcontrol`, a status dot per service, and the full `systemctl status` text in its output pane (on open, after each action, and on **Print Status**). It runs the commands on xbox-pi over SSH. The status dots refresh every 5 s over one SSH connection that stays open with the window, so leaving it open adds nothing to the Pi's journal; each button press is a login of its own.

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\Install-Shortcut.ps1 -NoStartMenu
```

puts an **xSphere Slow Control** icon on the Windows desktop. It needs key-based SSH from the PC to `xbox@192.168.8.116` (it cannot answer a password prompt) and tells you how to set that up if it is missing. The same module is a CLI: `python -m slowcontrol.servicectl status`, `… restart slowcontrol`, `… logs webcontrol -n 100`; on the Pi itself add `--local`.

**Stopping `xsphere-slowcontrol` is not inert.** The heater PIDs run in the PLC but this service feeds them their temperatures and enforces the PV safety interlock, so while it is stopped the PLC regulates on the last values written. Prefer Restart; the window asks before a Stop.

### Logs and troubleshooting

```bash
# what the service is doing right now
journalctl -u xsphere-slowcontrol -f
journalctl -u xsphere-webcontrol -f

# restart after editing config or state.yaml
sudo systemctl restart xsphere-slowcontrol
sudo systemctl restart xsphere-webcontrol

# read-only Modbus probe of the PLC (safe to run any time)
cd /home/xbox/xsphere-slow-control && python -m slowcontrol.tools.plc_probe
```

Common things and what they mean:

- **Many states INVALID after restart** — wait ~2 s; retained MQTT messages take a moment to redeliver to a fresh subscriber. If they stay invalid, the publisher (PLC driver / LabJack controller / ESP32) is down.
- **A LabJack/PLC restart kills the connection** — drivers reconnect automatically; the corresponding states will go stale then fresh.
- **`pid_*_setpoint` set has no effect** — gradient mode is probably `gradient`; switch to `absolute` first (or use the gradient card).

## System overview

```
                    ┌─────────────────────────────────────────┐
                    │              xbox-pi (RPi)              │
                    │                                         │
  PLC ──Modbus TCP──► Python slow control service             │
                    │   · PlcDriver  (poll + command)         │
                    │   · LabJackT7Controller                 │
                    │   · GradientController, AutoValve, …    │
                    │   · StateStore  →  xsphere/state/snapshot │
  LabJack T7 ──Eth─►                                          │
  GHS ESP32 ──WiFi─►   Mosquitto MQTT broker :1883            │
  Level ESP32s─WiFi►                                          │
                    │   Telegraf  →  InfluxDB  →  Grafana     │
                    │   webcontrol Flask:  /  register GUI    │
                    │                      /control  ctl GUI  │
                    └─────────────────────────────────────────┘
```

## Repository layout

```
xsphere-slow-control/
├── README.md                   ← you are here
├── SETUP.md                    ← step-by-step installation guide
├── OPERATIONS.md               ← day-to-day operations reference
├── VERIFICATION_CHECKLIST.md   ← hardware commissioning checklist
├── SYSTEM_ARCHITECTURE.md      ← full system reference document
│
├── slowcontrol/                ← Python slow control service
│   ├── app.py                  ← entry point
│   ├── config.yaml             ← all tunable parameters
│   ├── state.yaml              ← state-layer registry ("proprioception")
│   ├── STATE_LAYER_PLAN.md     ← state-layer design contract (read this first)
│   ├── SESSION_0_KICKOFF.md    ← session-workflow guidance
│   ├── SESSION_LOG.md          ← running record of state-layer sessions
│   ├── xsphere-slowcontrol.service
│   ├── core/  (config, mqtt, service orchestrator)
│   ├── drivers/  (PLC Modbus TCP)
│   ├── controllers/  (gradient, autovalve [disabled], interlocks)
│   ├── plugins/  (gradient_scanner)
│   └── state/  (schema loader + StateStore: subscribe, freshness, averages,
│                derived states, publishes xsphere/state/snapshot)
│
├── webcontrol/                 ← Flask web GUIs (consume xsphere/state/snapshot)
│   ├── app.py                  ← Flask + paho-mqtt; /api/state, /api/cmd, …
│   ├── templates/
│   │   ├── index.html          ← register GUI ( / ): everything, read-only
│   │   └── control.html        ← control GUI ( /control ): valves / PID /
│   │                             gradient / automation
│   └── xsphere-webcontrol.service
│
├── LJ-python-controller/       ← LabJack T7 plugin (pip install -e .)
│
├── telegraf/                   ← Telegraf MQTT→InfluxDB pipeline
│   ├── telegraf.conf
│   └── .env.example            ← copy to .env and fill in secrets
│
├── grafana/                    ← Grafana dashboard JSON
│
├── firmware/                   ← git submodules (run: git submodule update --init)
│   ├── gas-handling-system/    ← Moore-Lab/gas-handling-system
│   │   └── Software/Xenon Gas Handling System Sensor Suite/   (ESP32, branch slowcontrol-v2)
│   └── liquid-level-sensor/    ← Moore-Lab/liquid-level-sensor
│       └── Software/FDC1004 Level Sensor/   (ESP32, branch slowcontrol-v2; per-vessel envs)
│
├── nodered/                    ← historical Node-RED dashboard flows (the
│                                 Pi's Node-RED container is still running but
│                                 webcontrol is the primary GUI now)
└── plc_nodered.json            ← backup of the PLC's *embedded* Node-RED flows
```

## Quick orientation

| Component | Runs on | Language | Start command |
|---|---|---|---|
| Slow control service | xbox-pi | Python | `systemctl start xsphere-slowcontrol` |
| Web control panel | xbox-pi | Python (Flask) | `systemctl start xsphere-webcontrol` |
| Telegraf | xbox-pi (Docker) | — | `systemctl start telegraf` (or Docker) |
| MQTT broker | xbox-pi (Docker) | — | already running via IOTstack |
| InfluxDB | xbox-pi (Docker) | — | already running via IOTstack |
| Node-RED | xbox-pi (Docker) | — | **stopped** — bypassed by Telegraf + webcontrol; revert with `docker start nodered` |
| GHS ESP32 | GHS board | C++ | flash with PlatformIO |
| Level sensor ESP32s | dewar boards | C++ | flash with PlatformIO |

## MQTT topic schema

All sensor/status payloads are JSON.  Full schema and payload shapes:
`SYSTEM_ARCHITECTURE.md` §6.5 (must match `telegraf/telegraf.conf`).

| Topic | Direction | Payload |
|---|---|---|
| `xsphere/sensors/temperature/{plc\|labjack}/{rtd\|tc}/{ch}` | PLC / LabJack→broker | `{"value_k","value_c"[,"delta_c"]}` |
| `xsphere/state/snapshot` | StateStore→broker | consolidated `{generated_at, counts, states}` (retained) |
| `xsphere/sensors/pressure/ghs/setra/{1,2,3}` | GHS ESP32→broker | `{"value"}` (mbar) |
| `xsphere/sensors/vacuum/ghs/{1,2}` | GHS ESP32→broker | `{"value"}` (mbar) |
| `xsphere/sensors/environment/ghs/{temperature\|humidity\|baro_pressure}` | GHS ESP32→broker | `{"value"}` |
| `xsphere/sensors/level/{vessel}` | FDC1004 ESP32→broker | `{"raw","filtered"}` (pF) |
| `xsphere/status/pid/{zone}` | PLC driver→broker | `{"setpoint_k","pv_k","output_pct","kp","ki","kd"}` (retained) |
| `xsphere/status/valve/{vessel}` | Python→broker | `{"state","desired","auto_open","auto_close"}` (retained) |
| `xsphere/status/pneumatic/{name}` | Python→broker | `{"state","desired","relay"}` — valve open / commanded open / relay energised (retained) |
| `xsphere/status/service/heartbeat` | Python→broker | `{"uptime_s"}` (retained) |
| `xsphere/status/ghs_esp32`, `xsphere/status/level_{vessel}` | ESP32→broker | `{"uptime_s","rssi","ip"}` (device health; not ingested) |
| `xsphere/status/gradient`, `xsphere/status/gradient_scanner`, `xsphere/status/interlocks` | Python→broker | controller state |
| `xsphere/alerts/{rule}/{channel}` | Python→broker | individual alert payloads |
| `xsphere/commands/...` | Dashboard→Python | setpoint / valve / scan commands (Telegraf ignores) |

## Key contacts / resources

- SYSTEM_ARCHITECTURE.md — full hardware inventory, register map, wiring details
- SETUP.md — first-time installation
- OPERATIONS.md — routine operations and troubleshooting
- VERIFICATION_CHECKLIST.md — pre-deployment hardware verification
