# xsphere Slow Control

Slow control system for the xsphere cryostat experiment at Yale University.
Monitors and controls temperatures, LN2 fill levels, and gas handling for a
xenon cryostat used in levitated-particle physics experiments.

## What this system does

- **Reads temperatures** from the CLICK PLC (RTDs at the cryostat zones, base
  + clamps) and the LabJack T7 (cube RTDs + four type-K gradiometer TCs)
- **Controls heaters** via three PID zones (top / bottom / nozzle) on the PLC,
  with a Python gradient abstraction layer (gradient mode or per-zone absolute)
- **Looks after the heater supply** — the R&S NGE103 behind the heaters: output,
  voltage and current limit per channel, and switching a channel back on if
  it drops out
- **Manages LN2 autofill** for the ballast and primary xenon dewars via solenoid
  valves — the autofill ladder runs on the PLC; the Python service forwards
  level readings and exposes manual/arm controls
- **Monitors pressure and vacuum** from the gas-handling-system (GHS) ESP32
- **Watches the turbo pumps, and can start and stop them** — the Leybold
  TURBOVAC 90 i on the gas handling system (science chamber included) and the
  one on the outer vacuum, each over its USB port: speed, current, power,
  temperatures, state, warnings and the pump's own error memory, plus on / off
  from the Control page. Start and stop are all it can send
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
- **Open / close only work while valve control is enabled** — the `enable control` / `disable control` buttons at the top of the card. The gate is in the PLC driver (`xsphere/commands/pneumatic_control/enable`; its state, when it was last switched and the commands it has refused are on `xsphere/status/pneumatic_control`), so it holds for the Sequencer and anything else on the bus, not only this page. Control comes up **disabled** every time the slow-control service starts, and disabling it moves no valve. Once it is on there is deliberately no per-click confirmation.
- A Sequencer program can move them only while control is enabled, and cannot enable it itself: `pneumatic_control_enabled` has no control mapping in `state.yaml`.

**Heaters — PID** (one block per zone — top / bottom / nozzle)

- The summary line shows **PV** (process variable, actual temperature), **SET** (setpoint), **OUT** (heater output 0-100 %), and **err** (PV − SET).
- `setpoint (K)` + `set SP` writes the PID's setpoint register on the PLC.
  - **Important**: this only "sticks" when **gradient mode = absolute**. If gradient mode is `gradient`, the GradientController owns the per-zone setpoints and will overwrite anything you set here on the next gradient command. To control individual zones, switch to absolute first.
- `Kp / Ki / Kd` + `set gains` writes all three at once to the PLC's PID block. There's no commit-on-Tab — you must press the button.

**Heater supply — R&S NGE103** (the DC supply the heater SSRs switch; NGE channel *n* feeds heater *n*)

- One block per channel: `on` / `off`, `voltage (V)` + `set`, `current limit (A)` + `set`; `set all to 24 V / 1.5 A` writes the defaults from `config.yaml` to all three (setpoints only — it switches nothing). An empty field sends nothing. The V / A / W at the right of each block are instantaneous and catch the heater mid-PWM-cycle; read the 60 s averages on the Register page.
- **33.6 W per channel.** The supply caps the current limit at 33.6 W ÷ V without saying so: ask for 1.5 A at 24 V and it holds **1.400 A** (the card shows "1.50 A asked, capped by the supply"). A 36 W heater therefore runs current-limited — `ON · CC`, about 22.4 V / 1.4 A / 31 W — whenever its SSR is closed. That is normal here, not a fault.
- **Why channels switch themselves off, and what stops it.** Over-power protection ships enabled at exactly that 33.6 W, and unlike the current limit it switches the channel *off*; with 36 W heaters the supply works right at that level. The electronic fuse does the same at the current limit. The service keeps both off and the OVP level at its 32.5 V maximum (`nge.disable_auto_off`), so an overload just current-limits.
- **Hold** (on by default; the button on the card) keeps every channel at the output state, voltage and current limit set on this page:
  - a channel that drops out — protection trip, supply power-cycled, switched at the front panel — is switched back on, and setpoints lost in a power cycle are rewritten. The card counts the drop-outs and shows the reason for the last one;
  - it works the other way too: a channel switched **on** at the front panel is switched off again. Operate the supply from this page, or turn hold off first. Turning it back on holds whatever the supply is doing at that moment;
  - a channel that will not stay on is retried with back-off — at once, then after 2 s, 4 s … up to once a minute.
- The held state survives a service restart and a Pi reboot (`slowcontrol/nge_state.json`). After a power cut the supply comes up with every output off and the service puts it back as it was — unless that state is more than a day old (`nge.resume_max_age_s`), in which case it adopts what it finds.
- Stopping the slow-control service leaves the supply exactly as it is.
- While the supply is unreachable (`⚠ NOT CONNECTED`), `off` is remembered — the channel stays off when the supply returns — and `on` and setpoint changes are refused rather than queued.
- Each control is also a Sequencer target. `nge.resource: sim` in `config.yaml` runs all of this against a built-in simulated supply.

**Turbo pumps — Leybold TURBOVAC**

- At the top, **`enable control` / `disable control`** — the lock on every pump's buttons, as on the pneumatic valve card. It comes up *disabled* each time the slow-control service starts; while it is disabled `on` / `off` / `release` are greyed out and refused by the service itself (the Sequencer included), and the line says what was last refused. Disabling it starts, stops and releases nothing. Once it is enabled nothing asks again.
- Then one block per pump: **GHS turbo** (gas handling system + science chamber) and **Outer vacuum turbo**.
- **`on` / `off`** start and stop that pump over its USB link. The slow control then *holds* the pump there by repeating the command with every telegram (about every 2 s); the line beside the buttons says `held ON …` / `held OFF … since hh:mm`. **`release`** ends the hold: about 10 s later the pump goes back to whatever else commands it — its TURBOLAB, or its own X1 start input, where nothing plugged in means *stop*. A pump nobody has pressed a button for is `released` and is only read.
- **The TURBOLAB Core outranks USB.** Both pumps sit on TURBOLAB Core stands whose controller talks to the pump over a 9-pin RS 485 lead; while that lead is in, the Core's START key runs the pump and `on` / `off` here do nothing (the card says the pump does not report the bus in control). Both pumps' leads are now out (outer since 2026-10-12, GHS since 2026-10-14), so the Control page is what starts and stops them — and their Cores show `---`.
- **Both turbos keep their command without the Pi** (`keep_command: true` in `config.yaml`): while it is held, the service sets the pump's watchdog (parameter 182) to 0, and the pump stays running — or stopped — with the Pi, the service or the USB cable gone; the line says `keeps it without the Pi`. `release` gives it its 10 s back first. For any other pump **holding is not locking**: a pump that hears nothing from the Pi for 10 s lets go by itself, so a pump held **off** starts again if what is below says run, and one held **on** keeps running only if that says run.
- After a service restart or a USB glitch the hold carries on if the pump was out of touch for less than two minutes (`turbovac.resume_max_s`), or for any time if it kept the command by itself; otherwise it is released and `last event` says so. After a cut of its supply a pump comes back stopped with its 10 s watchdog — and is started again only if it was back within those two minutes. `on` and `off` are refused, not queued, while a pump is not connected.
- Start, stop and that one watchdog setting are all that can be sent: never a speed, another setting or an error reset — an error still has to be cleared at the stand. `turbovac.allow_control: false` in `config.yaml` takes the buttons away altogether and makes the service read-only.
- The first line is the pump's state and speed: `NORMAL` (at normal-operation speed — 90 % of the setpoint and above), `ACCELERATING`, `DECELERATING`, `RUNNING` (drive on, but the rotor is held below normal-operation speed), `COASTING`, `STOPPED` or `ERROR`, then `1200 Hz of 1200 · 72,000 rpm`, then supply voltage, motor current and drive power. Below it are the pump's own temperatures — motor, bearing, converter.
- A red line appears when the pump has an error or a warning active, saying which. `last entry` is the newest line of the pump's own error memory, with the speed it happened at and how many running-hours ago; the service also logs a line (`journalctl -u xsphere-slowcontrol | grep turbovac`) whenever a pump changes state or adds an entry.
- **Which pump is which** is decided by each pump's own serial number (on its nameplate; `turbovac.pumps` in `config.yaml`), never by the USB port — both pumps look identical on USB, so swapping the cables cannot swap the names. A block that says `not pinned` is a pump the service picked up only because it was the one unassigned TURBOVAC on USB: put the serial number it shows into `config.yaml` and restart the service.
- `⚠ NOT CONNECTED` means that pump is not on USB. Its USB port runs off the pump's own 24 V supply — and, with that cut, off the rotor while it runs down — so a pump whose supply is cut reads `DECELERATING` for a while and then shows this. The service finds it again within a few seconds of it coming back.
- Everything shown is also on the Register page (group *turbo*) and in InfluxDB (measurement `turbo`, tag `pump`; `remote` = 1 while the pump is under bus control, `watchdog_s` its parameter 182; measurement `turbo_control` for the lock). The mode is a Sequencer target like any other control — and is refused like any other while control is disabled.

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

`slowcontrol.servicectl` is a small window with Start / Restart / Stop / Logs for `xsphere-slowcontrol`, `xsphere-webcontrol` and `xsphere-aicontrol` (the AI control service, `aicontrol/README.md`; shown as not installed until its unit is), a status dot per service, and the full `systemctl status` text in its output pane (on open, after each action, and on **Print Status**). It runs the commands on xbox-pi over SSH. The status dots refresh every 5 s over one SSH connection that stays open with the window, so leaving it open adds nothing to the Pi's journal; each button press is a login of its own.

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
- **`supply: DOWN` / heater supply `⚠ NOT CONNECTED`** — the card shows the reason. `cannot open /dev/nge100` means the supply is off, its USB cable is out, or the udev rule is missing (`ls -l /dev/nge100`; SETUP.md step 3f). The supply carries on as it was; the service picks it up again within seconds of it reappearing.
- **A heater supply channel shows `ON · CC`** — it is current-limited, which is expected for a 36 W heater on a 33.6 W channel (see the control-page notes above).
- **A turbo pump shows `⚠ NOT CONNECTED`** — the card says why. `pump serial … is not on USB` means that pump's 24 V supply is off or its USB cable is out (`lsusb -d 25e9:0bb8` on the Pi prints one line per pump that is on USB); `found instead: serial …` means a TURBOVAC is there but it is not the one `config.yaml` names. A pump that was being held on or off goes back to its stand by itself when this happens — unless it keeps its command without the Pi (both turbos do).
- **`on` / `off` do nothing and the card says the pump does not report the bus in control** — that pump's TURBOLAB Core RS 485 lead is plugged in; the Core is in charge of it.
- **`… refused — turbo control is disabled`** — press `enable control` at the top of the turbo card first (it comes up disabled after every service start).

## System overview

```
                    ┌─────────────────────────────────────────┐
                    │              xbox-pi (RPi)              │
                    │                                         │
  PLC ──Modbus TCP──► Python slow control service             │
                    │   · PlcDriver  (poll + command)         │
  NGE103 ──USB-TMC──►   · NgeDriver  (heater supply)          │
  Turbo pumps ──USB─►   · TurbovacDriver  (read, on / off)    │
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
│   ├── drivers/  (PLC Modbus TCP; NGE heater supply and TURBOVAC turbo pump
│   │              monitor, each with its simulator)
│   ├── 99-xsphere-nge.rules    ← udev rule for the heater supply (/dev/nge100)
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
├── tests/                      ← offline tests (python -m pytest tests -q); the
│                                 heater supply and turbo pump ones run against
│                                 their simulators
│
├── telegraf/                   ← Telegraf MQTT→InfluxDB pipeline
│   ├── telegraf.conf           ← template; the container reads a deployed copy
│   ├── deploy-iotstack.sh      ← render + install it into the IOTstack container
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
| Telegraf | xbox-pi (Docker) | — | already running via IOTstack; after editing `telegraf/telegraf.conf` run `bash telegraf/deploy-iotstack.sh --apply` |
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
| `xsphere/sensors/turbo/{ghs\|outer}` | Python→broker | turbo pump readings: `{"state","frequency_hz","setpoint_hz","current_a","voltage_v","power_w","motor_temp_c","bearing_temp_c","converter_temp_c","normal","fault","warning","operating_hours",…}` (not published while the pump is unreachable) |
| `xsphere/status/turbo/{ghs\|outer}` | Python→broker | turbo pump link, identity and mode: `{"connected","pinned","pump_serial","product","port","error","event","warnings","last_error","mode","control_allowed","control_enabled","keeps_command","watchdog_s",…}` (retained) |
| `xsphere/status/turbo_control` | Python→broker | the lock on the turbo buttons: `{"enabled","since_utc","refused","last_refused","control_allowed"}` (retained) |
| `xsphere/commands/turbo/{ghs\|outer}/mode` | Dashboard→Python | `{"mode": "on"\|"off"\|"local"}` — hold the pump running / stopped, or release it (only with `turbovac.allow_control`, and only while turbo control is enabled) |
| `xsphere/commands/turbo_control/enable` | Dashboard→Python | `{"enabled": true\|false}` — open / shut that lock (shut at every service start) |
| `xsphere/status/pid/{zone}` | PLC driver→broker | `{"setpoint_k","pv_k","output_pct","kp","ki","kd"}` (retained) |
| `xsphere/status/valve/{vessel}` | Python→broker | `{"state","desired","auto_open","auto_close"}` (retained) |
| `xsphere/status/pneumatic/{name}` | Python→broker | `{"state","desired","relay"}` — valve open / commanded open / relay energised (retained) |
| `xsphere/status/pneumatic_control` | Python→broker | `{"enabled","since_utc","refused","last_refused"}` — the gate on the pneumatic valve commands (retained, 1 Hz) |
| `xsphere/commands/pneumatic_control/enable` | Dashboard→Python | `{"enabled": true\|false}` — open / shut that gate (Control page buttons) |
| `xsphere/status/nge` | Python→broker | heater supply: `{"connected","hold","master","model","serial","error","event",…}` (retained) |
| `xsphere/status/nge/{1\|2\|3}` | Python→broker | per channel: `{"output","desired","voltage_set_v","current_limit_a","voltage_v","current_a","power_w","mode","trip","dropouts",…}` (retained; not published while the supply is unreachable) |
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
