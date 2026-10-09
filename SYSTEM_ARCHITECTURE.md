# xsphere Slow Control System — Architecture & Development Reference

**Last updated:** 2026-04-12  
**Status:** Architecture planning phase — system is operational; improvements in design

---

## 1. Experiment Overview

The xsphere experiment optically or magnetically traps frozen xenon microspheres in a cryogenic vacuum chamber. Xenon gas is blown through a cooled sintered mesh nozzle, liquefying into droplets that are injected into the trapping chamber. Once trapped, the droplets are frozen by reducing chamber pressure, and the walls are cooled further to minimize radiative heat load on the frozen particle.

A key open challenge is convective airflow ("wind") inside the xenon chamber that disrupts trapping. Controlling vertical and longitudinal temperature gradients across the chamber is the primary tool for suppressing this wind. Systematic exploration of the gradient parameter space — guided by optical wind measurements from cameras — is a central experimental goal driving the slow control development.

---

## 2. Physical System

### 2.1 Cryogenic Assembly

All cryogenic components reside inside an **outer vacuum chamber**:

```
Outer Vacuum Chamber
├── LN2 Vessel
│   ├── Coaxial FDC1004 liquid level sensor
│   ├── RTD: LN2 vessel base (PLC RTD module)
│   ├── Thermocouple (K): vessel top
│   ├── Thermocouple (K): vessel bottom
│   ├── Aluminum block (bottom of vessel)
│   │   ├── Copper braids → Top Clamp
│   │   ├── Copper braids → Bottom Clamp
│   │   └── Copper braids → Nozzle Clamp / Aluminum Disk
│   ├── Top Clamp (around Xe cube top)
│   │   ├── RTD: clamp body, Xe-cube side (Omega)
│   │   ├── Thermocouple (K): vessel side of braid (Omega)
│   │   └── Heater: 36W DC resistive (SSR + PLC PWM) — PID Zone 1
│   └── Bottom Clamp (around Xe cube bottom)
│       ├── RTD: clamp body, Xe-cube side (Omega)
│       ├── Thermocouple (K): vessel side of braid (Omega)
│       └── Heater: 36W DC resistive (SSR + PLC PWM) — PID Zone 2
│
└── Xenon Cube (2.75" CF cube, 5 viewports + 1 nipple)
    ├── RTD: cube top — primary feedback for Zone 1 (PLC RTD module)
    ├── RTD: cube bottom — primary feedback for Zone 2 (PLC RTD module)
    ├── RTD: nozzle region — primary feedback for Zone 3 (PLC RTD module)
    ├── Aluminum Disk (on Xe fill flange, back of cube)
    │   ├── Copper braids → LN2 vessel
    │   └── Heater: 36W DC resistive (SSR + PLC PWM) — PID Zone 3
    └── Nozzle (sintered mesh, cooled to LXe temp)
        └── Connected to gas handling system via convoluted bellows
```

**Gradient control philosophy:**  
- Zone 1 (top clamp + top cube RTD): controls vertical top temperature  
- Zone 2 (bottom clamp + bottom cube RTD): controls vertical bottom temperature  
- Zone 3 (aluminum disk + nozzle RTD): controls longitudinal / nozzle temperature  
- Vertical gradient ΔT = T_bottom − T_top is the primary wind-suppression parameter  
- Longitudinal gradient controls liquification efficiency at the nozzle

**Preferred feedback:** RTDs embedded in the Xe cube walls (actual Xe temperature), not the clamp RTDs. Either can be used as the PID process variable.

**Typical operating temperatures:** ~165 K (LXe) and below.

**Future sensor upgrade:** Replace K-type thermocouples with differential thermocouple gradiometers — both junctions on the Xe cube faces (e.g., east/west) to directly measure ΔT across the cube rather than absolute temperature at each location.

### 2.2 Temperature Sensor Assignment

| Channel | Sensor Type | Location | Readout |
|---|---|---|---|
| 1 | RTD | Xe cube top | PLC RTD module |
| 2 | RTD | Xe cube bottom | PLC RTD module |
| 3 | RTD | Xe cube nozzle region | PLC RTD module |
| 4 | RTD | LN2 vessel base | PLC RTD module |
| 5 | RTD | Top clamp (Xe-cube side) | Omega RDXL6SD |
| 6 | RTD | Bottom clamp (Xe-cube side) | Omega RDXL6SD |
| 7 | K-type TC | LN2 vessel top | Omega RDXL6SD |
| 8 | K-type TC | LN2 vessel bottom | Omega RDXL6SD |
| 9 | K-type TC | Top clamp (vessel side of braid) | Omega RDXL6SD |
| 10 | K-type TC | Bottom clamp (vessel side of braid) | Omega RDXL6SD |

Omega RDXL6SD-USB: 6 channels, fully utilized (2 RTD + 4 TC).

### 2.3 Heater / Actuator Summary

| Zone | Heater | Power | Drive | PID Feedback (preferred) | PID Feedback (alt) |
|---|---|---|---|---|---|
| 1 (top) | Top clamp | 36 W DC | DIN SSR + PLC PWM | Xe cube top RTD | Top clamp RTD |
| 2 (bottom) | Bottom clamp | 36 W DC | DIN SSR + PLC PWM | Xe cube bottom RTD | Bottom clamp RTD |
| 3 (nozzle) | Aluminum disk | 36 W DC | DIN SSR + PLC PWM | Xe cube nozzle RTD | — |

All PID loops run on the CLICK Plus PLC. PID tuning, setpoints, and process variable source are written via Modbus register values.

### 2.4 LN2 Supply & Distribution

```
Portable LN2 Dewar (on mass scale, RS-232) [future integration]
    └── Manifold
         ├── Solenoid Valve 1 (PLC) → Cryostat LN2 vessel
         ├── Solenoid Valve 2 (PLC) → Primary Xe bottle cryoflask
         └── Solenoid Valve 3 (PLC) → Ballast bottle cryoflask
```

**PLC fill control logic (per vessel):**
- **Manual open/close:** Operator command
- **Auto-close:** Triggers when level sensor reads full (or other conditions)
- **Auto-open:** Triggers when level sensor reads low (also arms auto-close)
- Safety: auto-open always activates auto-close watchdog
- **Coast (cryostat only, optional):** defers auto-open until the vessel is
  empty *and* the cold block behind it is spent — see §2.4b

### 2.4b Coast refill (cryostat LN2 vessel only)

Plain autofill refills the cryostat as soon as the filtered level falls
through `level_low` (0.25), which tops the vessel up while there is still
cooling authority left in its cold block. **Coast** instead lets the vessel
run dry and keeps the cube running on the thermal mass of that block,
refilling only once the reserve is spent. A refill is permitted only when
BOTH terms hold:

| Term | Condition | Default |
|---|---|---|
| Vessel actually empty | `level < empty_threshold` | 0.13 (estimate) |
| Cold reserve spent | `(T_warm − T_cold) < delta_max_k`, held `confirm_s` | 40 K (estimate), 60 s |

The reserve is judged from two temperature sensors converging. The default
pair is `plc/rtd/2` (DF2, Xe cube bottom — the heated load) against
`plc/rtd/4` (DF4, LN2 vessel base — the cold sink): with liquid present the
difference is ~88 K, and it shrinks as the block dries. `delta_mode` defaults
to `signed` rather than `absolute`, because a cold sensor reading hotter than
the load is a wiring or scaling fault and `abs()` would read that as
convergence.

> **Do not pair DF1 with DF2.** That difference *is* the commanded vertical
> gradient (the gradient controller's `delta_v_k`, §6.3), so the gate would
> sit near 0 K and coast would silently never engage.

**The gate applies to `auto_open` and nothing else.** It can never inhibit
auto-close, the fill timeout, or a manual valve command. Every condition it
cannot evaluate *releases* the inhibit — a stale or missing temperature, an
implausible reading, a stale or missing level, a reversed pair, or a level
channel claiming liquid while the sink reads dry all revert to the `level_low`
behaviour and raise `coast_unverified`. The failure direction is always
"spend LN2", never "leave the vessel empty and warming".

Three backstops force a refill and **latch** coast off until the vessel is
proven wet again — level back at or above `level_high` AND the cold sensor
below `sink_cold_k` (90 K), i.e. two independent sensors agreeing:

| Backstop | Trip condition | Default |
|---|---|---|
| `max_duration_s` | coast episode has run this long | 5400 s (sanity bound) |
| `ceiling_k` | any of DF1–DF3 above this (not the configured pair) | 175 K |
| `sink_warm_k` | cold sensor above this — the sink itself is dry | 135 K |

The `confirm_s` dwell only ever delays a fill; a backstop acts on the first
tick it is true.

Coast always comes up **disarmed**. There is deliberately no arm-on-start
key: arming is an explicit operator act over MQTT
(`xsphere/commands/valve/cryostat/coast`), and that command must never be
published retained. The backstops are config-file only — runtime retuning
over `coast_config` accepts only `empty_threshold`, `sensor_warm`,
`sensor_cold`, `delta_max_k`, `delta_mode`, `delta_hysteresis_k` and
`confirm_s`, validated by the same predicates as the file loader,
all-or-nothing, and never persisted, so `config.yaml` wins after a restart.

The autovalve control loop re-evaluates every vessel every 10 s. That tick is
what lets a temperature-only change move the valve: the ΔT term does not
arrive on a level message or an operator command, so nothing else would carry
it to the OPEN decision.

> **Coast depends on a ladder change that has to be typed in by hand.** Until
> "Required CLICK ladder additions" item 6 (§6b) is entered, the ladder's own
> XV3 auto-open window (0.25 < DF303 < 0.8) tops the vessel up during the
> descent and coast never sees an empty vessel — the feature silently does
> nothing. Arming with `ladder_rung_entered: false` is allowed but logs a loud
> warning, because that failure wastes LN2 rather than endangering anything.

> **Re-arm coast before re-arming auto-open.** Coast comes up disarmed, so
> after a restart re-arming `auto_open` without re-arming coast restores the
> `level_low` = 0.25 threshold while the vessel is still sitting below
> `empty_threshold`, and it fills immediately. That is fail-safe — a fill,
> never a strand — but it silently ends the coast.

`empty_threshold` = 0.13, `delta_max_k` = 40 K and `max_duration_s` = 5400 s
are **estimates, not measurements**; see §8 for what has to be measured before
they mean anything.

---

## 3. Gas Handling System

### 3.1 Xenon Flow Path

```
[Xe Cube]
    └── tube
         └── [Valve A] ── convoluted bellows ── [Valve B]
                                                     └── 4-way CROSS (A)
```

**From Cross A — three paths:**

**Path 1 (downward — gauges & pump):**
```
Cross A
  └── Setra 225 #1 (0–10V, Xe cube pressure)
  └── PenningVAC #1 full-range (0–10V, split: pumping stand + ADS1115)
  └── 2.75" CF Tee
       ├── PenningVAC full-range gauge
       └── Bellows hand valve
            └── [RGA: SRS 200, serial → DAQ computer] + [Leybold turbo pumping stand]
  └── Valve → [Gas Purifier] → Valve ──┐
                                        │ (join at tee)
```

**Path 2 (bypass — around purifier):**
```
Cross A
  └── Bypass Valve ────────────────────┘ (join at tee behind purifier)
                                        │
                                   Valve → CROSS (B)
```

**Cross B — supply bottles:**
```
Cross B
  ├── Setra 225 #2 (0–10V, primary bottle pressure)
  ├── Valve → [Primary Xe Bottle, 1L, ~3 bar]
  │              (surrounded by cryoflask with LN2 level sensor)
  └── Valve → Setra 225 #3 (0–10V) → [Secondary Xe Bottle, 4L, 50 bar]
                                        (regulator on bottle, manual cryohose for recovery)
```

**Path 3 (ballast — from Cross A):**
```
Cross A
  └── Hand Valve → Needle Valve → [Ballast Bottle, 1L, normally empty]
                                    (cryoflask with LN2 level sensor)
```

### 3.1b Metered Flow Leg (MKS M330B + pneumatic bypass)

A mass flow controller and a pneumatic bypass valve sit as two **parallel legs
between the same pair of tees**, so gas can be routed through either one:

```
        ┌────── [MKS M330B MFC]  ──────┐
  ──────┤  metered, one direction      ├──────
        └────── [Bypass valve XV4] ────┘
                 pneumatic, solenoid-piloted
```

- **Forward / fill flow** runs through the MFC with the bypass shut, so the
  rate is metered, controllable, and logged.
- **Return / recovery flow** runs through the bypass with the MFC hard-shut,
  because the MFC is a metering restriction and is not intended for reverse
  flow.
- Both legs open at once makes the flow reading meaningless (an unknown
  fraction takes the unmetered path), so the software treats it as a fault.

> **Naming:** this is the **MKS bypass** (`bypass`, XV4). It is a different
> valve from the *purifier* bypass in §3.1 Path 2, which remains a hand valve
> and is not under PLC control.

**Exact tee locations in the gas panel are still to be confirmed** — the
control software is agnostic to where the pair sits in the manifold.

### 3.1c MKS M330B electrical interface

> ### ⚠ Confirm the model code before landing a single wire
>
> The same 15-pin D shell carries **two electrically incompatible valve-override
> conventions** across MKS generations:
>
> | Generation | Valve Open (pin 4) | Valve Close (pin 3) | Pin 6 | Pin 7 |
> |---|---|---|---|---|
> | **Legacy analog** (M330B, 1179A, 1479A, M100B) | pull **LOW** / short to signal common | pull **LOW** / short to signal common | **−15 V in** | +15 V in |
> | **G-Series** (GM50A, GE50A) | pull **HIGH**, +5…+15 V | pull to ground or −5…−15 V | **No connection** | +15…+25 V in |
>
> Wiring a G-Series harness to a legacy unit drives −15 V into a TTL-level
> input. MKS publishes no absolute-maximum rating or input schematic for those
> pins, so treat it as a destruction risk. **Read the label on your unit and
> confirm the model code with MKS before connecting anything.**
>
> Also note the datasheet attached to this work is the **M330H/M330AH** — a
> *different*, 9-pin product whose override *is* ±15 V on a single pin (pin 1).
> On the 15-pin M330B, **pin 1 is a valve-monitor output**, not an input.
> Applying +15 V to it drives a supply rail into an op-amp output.

**M330B 15-pin Type "D" (male on the MFC)**

| Pin | Signal | Direction | Connect to |
|---|---|---|---|
| 1 | MKS test point / valve monitor (0–10 V) | out | *leave open* |
| 2 | **Flow signal output**, 0–5 V = 0–100% FS | out | PLC Slot0 **AD1V** (DF201) |
| 3 | **Valve Close** override (active low) | in | override contact → pin 12 (*output unassigned — see Relay commons*) |
| 4 | **Valve Open / purge** override (active low) | in | override contact → pin 12 (*output unassigned — see Relay commons*) |
| 5 | Power supply common | — | ±15 V supply return **only** |
| 6 | **−15 VDC** supply | in | −15 V rail |
| 7 | **+15 VDC** supply | in | +15 V rail |
| 8 | **Setpoint input**, 0–5 V = 0–100% FS | in | PLC Slot0 **DA1V** (DF205) |
| 9 | Reserved | — | **do not connect** |
| 10 | Optional input (external pressure closed-loop) | in | *leave open* |
| 11 | Signal common | — | (alternate to pin 12) |
| 12 | **Signal common** | — | PLC **ACOM** + the override contacts' common (**not C2**) |
| 13 | Reserved | — | **do not connect** |
| 14 | Reserved | — | **do not connect** |
| 15 | Chassis ground | — | cable shield / panel earth |

**Valve override truth table** — both pins floating is the normal mode, and it
needs no relay at all:

| Open contact (pin 4) | Close contact (pin 3) | MFC behaviour |
|---|---|---|
| open | open | **NORMAL** — follows the pin-8 setpoint |
| open | closed | Valve driven shut |
| closed | open | Valve forced open (purge) |
| closed | closed | Valve **OPEN** — open has priority over close |

Both-asserted is documented and non-destructive, but it fails *open*, which is
the wrong direction. The ladder's mutual-exclusion rung exists to make that
state unreachable.

**Relay commons.** The C0-08TR splits its 8 points into **two isolated commons
of four** — C1 serves Y101–Y104, C2 serves Y105–Y108 — and the grouping is
fixed. Each common must therefore carry exactly one voltage domain:

| Common | Tied to | Points |
|---|---|---|
| **C1** | Solenoid supply (24 V) | Y101–Y103 LN2 fill, **Y104 MKS bypass pilot** |
| **C2** | Solenoid supply | **Y105 ballast valve, Y106 pump valve, Y107 bottle valve**, Y108 spare |

> **Y105/Y106 are no longer available for the MKS override.** They were
> earmarked for the two override contacts with C2 tied to the MFC signal
> common, but that ladder decode was never entered and Y105–Y107 now drive the
> gas-side solenoid valves (§6b, Solenoid Valve Registers). Both commons on the
> C0-08TR therefore carry solenoid supply, and **the MFC signal common must
> not be landed on either of them** — a signal-level contact sharing a common
> with a solenoid ties pin 12 to the solenoid supply. The override contacts
> need their own isolated outputs: a second relay module, or the interposer
> below driven from discrete DC points.

> ### ⚠ Low-level switching — the override contacts are a dry circuit
>
> Shorting pin 3 or pin 4 to signal common switches essentially no voltage and
> no current. The C0-08TR's published floor is **5 mA @ 5 VDC**, and
> AutomationDirect does not publish the contact alloy — assume it is a
> silver-alloy power relay, not gold. Below the minimum load there is not
> enough energy to fritt through the oxide film that forms on the contact, and
> the failure mode is intermittent high-resistance closures that still pass a
> continuity test.
>
> **Recommended:** drive the two override lines through an interposing
> solid-state switch (PhotoMOS / optocoupler-MOSFET) or a gold bifurcated /
> reed signal relay, with the PLC relay point energising the interposer's
> input. This also matches MKS's own instruction to drive the override pins
> "with a tri-stated device".
>
> Nothing in the register map or the software changes if you add an
> interposer — the ladder still drives two override outputs, which now energise
> the interposer's input instead of touching the MFC directly. If the option-slot
> module turns out to expose usable discrete DC outputs (the `08D2` in
> `C2-08D2-6V` suggests 8 DC discrete points — confirm in the module setup
> dialog), those could drive the interposer without needing any relay points.

**Power supply.** ±15 VDC ±5%, **200 mA per channel during the first 5 s** of
start-up, 100 mA steady. Size for the inrush, not the steady figure.

**Grounding.** Keep **power common (pin 5)** and **signal common (pin 12)**
separate all the way back to the panel. The PLC's analog inputs are
single-ended and its ACOM is almost certainly *not* isolated from logic ground
(AutomationDirect publishes no isolation spec for the -6V analog section, and
its equivalent circuit shows ACOM tied to internal 0 V). Bond the MFC signal
ground to ACOM at **one** point only. On a 5 V full-scale signal, a ground loop
through the ±15 V supply return is easily worth several percent of full scale.

**Loading.** The MFC flow output drives ≥10 kΩ; the PLC analog input is 40 kΩ,
so this is fine. Do **not** use a 250 Ω current-loop input. The MFC setpoint
input wants a source below 20 kΩ; the PLC analog output qualifies easily
(it drives ≥4 kΩ, 2.5 mA max).

**Power-up transient.** Sibling MKS analog Mass-Flo units park the flow output
at roughly **+7.0 to +7.5 V for one to two minutes** after power-on while the
sensor heaters stabilise. This is why the PLC channel must stay on its
**0–10 V range** — a 0–5 V-configured input would be driven past its rating
every time the MFC powers up. The driver flags any reading above 110% of full
scale as `over_range`; the dashboard shows "WARMING UP" instead of a flow rate
and the interlock watchdog ignores the sample, so consumers of
`xsphere/sensors/flow/mks` must honour that flag rather than trusting
`value_sccm` unconditionally. Full thermal warm-up
for the M330 family is **~30 minutes**; keep gas isolated until then.

### 3.2 Safe Operating Condition

After transferring xenon to the Xe cube in liquid form: open valves to primary bottle and ballast. The combined volume (1L primary at ~3 bar + 1L ballast) provides sufficient buffer that if cooling is lost and xenon vaporizes, system pressure remains below burst disk rating. This is the walk-away-safe configuration — no automated recovery system required at current xenon inventory.

**Future upgrade path:** If a gas regulator is added between primary bottle and fill path (enabling continuous-flow fill of significantly more liquid), automated recovery via pneumatic valves would be required. This is not currently planned but is tracked as a possible upgrade.

### 3.3 Gas Handling Sensors (ESP32)

All analog sensors → ADS1115 (two I2C ADCs) on GHS ESP32:

| Sensor | Type | Signal | Notes |
|---|---|---|---|
| Setra 225 #1 | Pressure (manometer) | 0–10V → ADS1115 | Xe cube pressure |
| Setra 225 #2 | Pressure (manometer) | 0–10V → ADS1115 | Primary Xe bottle |
| Setra 225 #3 | Pressure (manometer) | 0–10V → ADS1115 | Backup Xe bottle |
| PenningVAC #1 | Full-range vacuum | 0–10V → ADS1115 | GHS vacuum (split to stand) |
| PenningVAC #2 | Full-range vacuum | 0–10V → ADS1115 | Outer vacuum (split to stand) |
| BMP3XX | Barometer + temp | I2C | Ambient on GHS panel |
| DHT11 | Humidity + temp | GPIO | Ambient on GHS panel |

ESP32 publishes JSON payloads via MQTT to Mosquitto broker on xbox-pi.

### 3.4 Liquid Level Sensors (FDC1004)

Three coaxial capacitance probes, each connected to a dedicated FDC1004 IC on an ESP32:

| Vessel | Purpose |
|---|---|
| Cryostat LN2 vessel | Monitor + trigger autofill (solenoid valve 1) |
| Primary Xe bottle cryoflask | Monitor LN2 level for cryo-recovery |
| Ballast bottle cryoflask | Monitor LN2 level for cryo-recovery |

### 3.5 Pumping

- **GHS pump:** Leybold turbo-based pumping stand (manual gate valve between turbo and system)
- **Outer vacuum pump:** Same model Leybold stand (manual gate valve)
- **Serial interface:** Not currently implemented — future work (low priority)
- **RGA:** SRS 200, serial connection to DAQ computer — out of scope for slow control

---

## 4. Network & Compute

### 4.1 Machine Inventory

| Hostname | Hardware | Role | IP |
|---|---|---|---|
| xbox-pi | Raspberry Pi | Server: MQTT, InfluxDB, Node-RED, Portainer | 192.168.8.116 |
| xbox-DAQ | Desktop | Primary DAQ, PLC programming interface, RGA | (static) |
| xbox-PLC | CLICK Plus PLC | Automation, PID, valve control | (static) |
| xbox-radio | GL-SFT1200 router | Local network hub, SSID "xbox-radio" | 192.168.8.1 |

**Local network:** 192.168.8.x, WiFi SSID "xbox-radio"  
**Remote access:** Yale VPN + SSH tunnel through router  
**Port forwarding (2500–2534):** MQTT (1883), Node-RED, InfluxDB, Portainer, SSH

### 4.2 Software Stack (Current)

Running on xbox-pi via **IOTstack** (Docker Compose):

| Service | Container | Purpose |
|---|---|---|
| Mosquitto | `mosquitto` | MQTT broker, port 1883 |
| InfluxDB 2.x | `influxdb` | Time-series database |
| Node-RED | `nodered` | Flow programming, dashboard, data parsing |
| Portainer | `portainer` | Container management UI |

Additional systemd services on xbox-pi (outside Docker):
- `RDXL6SD-mqtt.service` — Omega temperature logger (Python, pymodbus serial → MQTT)

### 4.3 PLC

- **Model:** CLICK Plus (C2-series)
- **Built-in:** Ethernet port with native Modbus TCP support
- **Modules:** Node-RED module (installed), Modbus module (installed, not in active use)
- **Programming:** Ladder logic on DAQ computer via CLICK programming software
- **Key functions:** 3× PID loops (heaters), 3× solenoid valve control (LN2 manifold), RTD module readout

---

## 5. Current Data Pipeline

All device data currently flows through the same pattern:

```
[Device] → MQTT (publish) → Mosquitto (xbox-pi) → Node-RED (RPi) → InfluxDB 2.x
```

Specific flows:
- **PLC:** PLC Node-RED module → MQTT → RPi Node-RED parse → InfluxDB
- **Omega:** Python service (`RDXL6SD-mqtt.service`) → MQTT `RDXL6SD/temps` → RPi Node-RED parse → InfluxDB
- **GHS ESP32:** ESP32 firmware → MQTT → RPi Node-RED parse → InfluxDB
- **Level sensors:** ESP32 firmware → MQTT → RPi Node-RED parse → InfluxDB

**Current monitoring:** InfluxDB browser UI  
**Current control:** PLC interface on DAQ computer + RPi Node-RED inject nodes  
**Current dashboard:** Minimal Node-RED dashboard (3 solenoid valve buttons only)

---

## 6. Proposed Architecture

### 6.1 Overview

```
┌──────────────────────────────────────────────────────────────────┐
│                    HARDWARE / FIRMWARE LAYER                      │
│                                                                    │
│  CLICK Plus PLC          ESP32 (GHS)         ESP32s (Level)       │
│  - Ladder logic          - ADS1115           - FDC1004 ×3         │
│  - 3× PID (heaters)      - DHT11, BMP3XX     - Coaxial probes     │
│  - 3× solenoid valves    - 5 pressure gauges                      │
│  - 4× RTD readout        → MQTT                                   │
│  → Modbus TCP + MQTT                                              │
│                                                                    │
│  Omega RDXL6SD-USB                                                │
│  - 4 K-type TC + 2 RTD                                           │
│  → Python service → MQTT                                          │
└────────────────────────────────┬─────────────────────────────────┘
                                 │ MQTT
                    ┌────────────▼──────────────┐
                    │   Mosquitto MQTT Broker    │
                    │   xbox-pi :1883            │
                    └──┬─────────────┬──────────┘
                       │             │
          ┌────────────▼───┐   ┌─────▼──────────────────────────┐
          │   Telegraf     │   │   Python Slow Control Service   │
          │   MQTT→InfluxDB│   │   (xbox-pi, systemd service)   │
          └────────┬───────┘   │                                │
                   │           │  Drivers                       │
          ┌────────▼───────┐   │  - PLC (Modbus TCP, direct)    │
          │  InfluxDB 2.x  │   │  - Omega (already running)     │
          └────────┬───────┘   │  - Scale RS-232 (future)       │
                   │           │  - Leybold serial (future)     │
          ┌────────▼───────┐   │                                │
          │    Grafana     │   │  Controllers                   │
          │  (monitoring)  │   │  - PID wrapper + gradient mgr  │
          └────────────────┘   │  - Autovalve state machine      │
                               │  - Interlocks / safety         │
                               │                                │
                               │  Plugins                       │
                               │  - Temp gradient scanner       │
                               │  - Future experiment modules   │
                               └────────────┬───────────────────┘
                                            │ MQTT (commands + status)
                               ┌────────────▼───────────────────┐
                               │   Node-RED Dashboard (web)     │
                               │   Multi-user, remote access    │
                               │                                │
                               │  • Real-time temp display      │
                               │  • Solenoid valve buttons      │
                               │  • Autovalve enable/disable    │
                               │  • PID setpoint controls       │
                               │  • Gradient config dropdowns   │
                               │  • Plugin activation panel     │
                               │  • Service heartbeat / alerts  │
                               └────────────────────────────────┘
```

### 6.2 Key Design Decisions

**Python service talks to PLC via Modbus TCP directly**  
The CLICK Plus PLC's built-in Ethernet port supports Modbus TCP natively. The Python service uses `pymodbus` to read RTD register values and write PID setpoints without going through Node-RED. The PLC Node-RED module remains available for manual override and debugging.

**Telegraf replaces Node-RED for data ingestion**  
Node-RED is removed from the critical data path. Telegraf's MQTT consumer plugin subscribes to all sensor topics and writes directly to InfluxDB. This makes the data pipeline more robust (Telegraf is purpose-built for this), removes a single point of failure, and simplifies Node-RED to a pure UI/control layer.

**Node-RED is the web operations panel**  
Node-RED's browser-accessible dashboard is the right tool for multi-user remote operations: it requires no client install, supports simultaneous users, and can be accessed from home via VPN. The dashboard is rebuilt with a proper operator interface. Node-RED does not contain business logic — it only sends commands to the Python service via MQTT and displays incoming status/data.

**Python service handles all control logic**  
Complex logic — gradient abstraction, state machines, interlocks, plugin modules — lives in Python. This is maintainable, testable, version-controlled, and easier for future developers than deep Node-RED function node chains.

**Grafana replaces InfluxDB browser for monitoring**  
Grafana connects to the existing InfluxDB 2.x instance and provides a proper monitoring dashboard accessible remotely. Historical data, multi-panel layouts, and alerting are all significantly better than the raw InfluxDB UI.

### 6.3 Gradient Control Abstraction

A key usability improvement. Currently, setting a 5 K vertical gradient requires manually rewiring a PLC register to set the bottom PID's process variable to `top_RTD + 5`. This will be replaced with a clean interface:

- Operator selects gradient mode: "Top–Bottom ΔT = 5 K"
- Python service computes the correct PV source and setpoint, writes to PLC registers
- Node-RED dashboard exposes this as a dropdown + numeric field

Supported gradient configurations:
- Vertical gradient (Zone 1 vs Zone 2): ΔT = T_bottom − T_top
- Longitudinal gradient (Zone 3 vs Zone 1 or 2): ΔT = T_nozzle − T_top or T_bottom
- Direct setpoint mode (absolute temperature on any zone)

### 6.4 Plugin Architecture

Modeled on the usphere-DAQ pattern. Each plugin is a self-contained Python module that:
- Registers itself with the service core
- Subscribes to relevant MQTT data topics
- Publishes commands or status via MQTT
- Can be activated/deactivated from the Node-RED dashboard

**Planned plugins:**
- `gradient_scanner` — Sweeps vertical and longitudinal gradient parameter space, records wind response from camera analysis scripts
- `autovalve` — LN2 fill state machine (already exists in PLC; Python wrapper adds monitoring and override)

**Future plugins:**
- `scale_reader` — LN2 dewar mass scale (RS-232)
- `pump_monitor` — Leybold pumping stand status (serial)

### 6.5 MQTT Topic Schema (Proposed)

```
xsphere/sensors/plc/rtd/{1..4}            # RTD values from PLC (K)
xsphere/sensors/omega/rtd/{1..2}          # RTD values from Omega (K)
xsphere/sensors/omega/tc/{1..4}           # TC values from Omega (K)
xsphere/sensors/ghs/pressure/{gauge}      # Pressure gauges (Pa or mbar)
xsphere/sensors/ghs/vacuum/{gauge}        # PenningVAC full-range (mbar)
xsphere/sensors/ghs/ambient/temp          # BMP3XX temperature (C)
xsphere/sensors/ghs/ambient/pressure      # BMP3XX barometric (hPa)
xsphere/sensors/ghs/ambient/humidity      # DHT11 humidity (%)
xsphere/sensors/level/{vessel}            # FDC1004 level (%)
xsphere/sensors/flow/mks                  # MKS MFC flow (sccm, %FS, raw V)

xsphere/status/service/heartbeat          # Python service uptime (retained)
xsphere/status/controllers/{name}         # Controller state JSON (retained)
xsphere/status/valves/{name}              # Solenoid valve state (retained)
xsphere/status/valve/mks                  # MKS valve mode + setpoint readback
xsphere/status/valve/bypass               # MKS bypass valve state
xsphere/status/gasflow                    # Resolved flow path + setpoint
xsphere/status/level/{vessel}             # {"raw","filtered"} level
xsphere/status/coast/{vessel}             # Coast gate state (retained)
xsphere/alerts/{rule}                     # Interlock alerts
xsphere/alerts/gasflow_path_conflict      # Both parallel legs open at once
xsphere/alerts/coast_backstop/{vessel}    # A backstop forced a refill (latched)
xsphere/alerts/coast_unverified/{vessel}  # Coast released on unusable data
xsphere/alerts/coast_refused/{vessel}     # An arm or coast_config was rejected

xsphere/commands/pid/{zone}/setpoint      # Write PID setpoint (K)
xsphere/commands/pid/{zone}/pv_source     # Set PV source (e.g., "cube_top")
xsphere/commands/gradient/vertical        # Set vertical ΔT (K)
xsphere/commands/gradient/longitudinal    # Set longitudinal ΔT (K)
xsphere/commands/valve/{name}/state       # Open/close solenoid valve
xsphere/commands/valve/bypass/state       # Open/close MKS bypass valve
xsphere/commands/valve/mks/mode           # "closed" | "normal" | "open"
xsphere/commands/valve/{vessel}/coast     # {"enabled": true|false} — arm coast
xsphere/commands/valve/{vessel}/coast_config    # Subset of 7 tunable gate keys
xsphere/commands/valve/{vessel}/coast_permit    # {"enabled": 0|1} → DS1107
xsphere/commands/flow/mks/setpoint        # {"value_sccm": X} or {"percent": X}
xsphere/commands/flow/mks/setpoint_v      # {"volts": X} (internal, driver-facing)
xsphere/commands/gasflow/path             # "mks" | "bypass" | "isolated"
xsphere/commands/autovalve/{vessel}/mode  # Enable/disable autofill
xsphere/commands/plugin/{name}/start      # Activate plugin
xsphere/commands/plugin/{name}/stop       # Deactivate plugin
```

PID zone naming: `top`, `bottom`, `nozzle`  
Valve naming: `ln2_cryostat`, `ln2_primary`, `ln2_ballast`; gas-side solenoids `gas_ballast`, `gas_pump`, `gas_bottle`  
Vessel naming: `cryostat`, `primary_xe`, `ballast`  
Gas path naming: `mks`, `bypass`, `isolated` (plus reported-only `conflict`, `unknown`)

**Note on the MKS valve topic.** Every other valve takes
`.../valve/{name}/state` with a boolean. The MKS valve is tri-state — closed,
normal (follow setpoint), open (purge) — so it deliberately uses
`.../valve/mks/mode` with a string instead. The PLC driver rejects a boolean
`state` command addressed to `mks` rather than silently coercing it.

**Note on the coast topics** (§2.4b; `cryostat` is the only vessel with a
coast block today). `xsphere/status/coast/{vessel}` is retained, so the answer
to "why has this not refilled yet" is on screen the instant the dashboard
loads rather than one tick later. Its fields include `state`, `hold_reason`,
`permit_open`, `open_threshold`, `level`, `empty_threshold`, `sensor_warm`,
`sensor_cold`, `t_warm_k`, `t_cold_k`, `delta_k`, `delta_max_k`, `dwell_s`,
`confirm_s`, `elapsed_s`, `max_duration_s`, `ceiling_k`, `ceiling_max_k`,
`sink_warm_k`, `latched_off`, `release_reason`, `ladder_rung_entered`,
`ladder_permit` and `ladder_permit_readback`. `state` is one of:

| State | Meaning |
|---|---|
| `disarmed` | no coast block, disabled in the file, or not armed |
| `latched_off` | a backstop fired and the vessel is not yet proven wet |
| `released` | data unusable — reverted to the `level_low` threshold |
| `monitoring` | above `empty_threshold`, waiting for the vessel to empty |
| `coasting` | empty, ΔT still high — the only state that withholds a fill |
| `converged` | ΔT held below `delta_max_k` for `confirm_s` — refilling |
| `backstop:max_duration` | episode ran past `max_duration_s` |
| `backstop:sink_warm` | cold sensor above `sink_warm_k` |
| `backstop:ceiling` | a cube RTD above `ceiling_k` |

**The arm command must never be published retained.** Coast coming up
disarmed after a restart is a safety property, and a retained arm on
`.../coast` would defeat it. `xsphere/status/valve/{vessel}` now also carries
a `coast_permit` field — the DS1107 readback, so an operator can tell a permit
that was *written* from one that actually *landed*.

**Note on `xsphere/status/level/{vessel}`.** This topic is new. The filtered
level used to be republished onto `xsphere/sensors/level/{vessel}`, which is
the same topic the autovalve controller subscribes to, so the exponential
filter was being fed its own output on every pass. The controller's raw and
filtered values now go out together under `status/`; `sensors/level/...` stays
the sensor publisher's topic — the PLC driver's DF203/DF303 pair for the
cryostat, the ESP32 for the two bottles — and the controller reads only `raw`
from it.

---

## 6b. PLC Register Map (Confirmed from ladder logic PDF + Node-RED flows)

### Hardware Modules (CLICK Plus rack)
| Slot | Module | Description |
|---|---|---|
| — | C2-01CPU-2 | CPU |
| Slot0 | C2-08D2-6V | 4× analog in (0–10V) + 2× analog out (0–10V) |
| Slot1 | C2-NRED | Node-RED module, IP 192.168.8.190, port 1880 |
| I/O 1 | C0-08TR | Relay outputs |
| I/O 2 | C0-04RTD | 4-channel RTD input (Pt100/Pt1000) |

**PLC CPU Modbus TCP:** Port1 = Modbus TCP, port 502, DHCP (gateway 192.168.8.1)

### RTD Inputs (read-only)
| Register | Physical channel | Sensor type | Location |
|---|---|---|---|
| DF1 | RTD ch1 | Pt100, –200 to 850°C | Xe cube top |
| DF2 | RTD ch2 | Pt100, –200 to 850°C | Xe cube bottom |
| DF3 | RTD ch3 | Pt100, –200 to 850°C | Xe cube nozzle |
| DF4 | RTD ch4 | Pt1000, –200 to 595°C | LN2 vessel base |

### Analog I/O (Slot0, C2-08D2-6V)
| Register | Direction | Signal | Use |
|---|---|---|---|
| DF201 | IN ch1 | 0–10V | **MKS M330B flow signal output** (0–5 V = 0–100% FS) |
| DF202 | IN ch2 | 0–10V | TBD |
| DF203 | IN ch3 | 0–10V | Cryostat LN2 level sensor (raw) |
| DF204 | IN ch4 | 0–10V | TBD |
| DF205 | OUT ch1 | 0–10V | **MKS M330B setpoint input** (0–5 V = 0–100% FS) |
| DF206 | OUT ch2 | 0–10V | Analog output (TBD) |

The MFC uses a 0–5 V span on a 0–10 V module, so only the lower half of the
converter range is exercised — one bit of resolution is given up on both the
input and the output. This is accepted in exchange for not adding external
scaling amplifiers. Configure the module's engineering-unit scaling so DF201
and DF205 read and write **directly in volts** (0.0–10.0); all conversion to
sccm happens in Python.

### Level Sensor Registers
| Register | Description | Notes |
|---|---|---|
| DF203 | Cryostat LN2 level (raw, 0–10) | From PLC ADC ch3 |
| DF303 | Cryostat LN2 level (filtered) | α=0.01 exponential filter applied by ladder |
| DF251 | Ballast bottle level (raw, 0–10) | Written by Python service via Modbus TCP (was PLC Node-RED from MQTT `sensor/ch4_voltage`) |
| DF252 | Primary Xe bottle level (raw, 0–10) | Written by Python service via Modbus TCP (was PLC Node-RED from MQTT `sensor/ch5_voltage`) |
| DF351 | Ballast bottle level (filtered) | α=0.01 filter applied by ladder |
| DF352 | Primary Xe bottle level (filtered) | α=0.01 filter applied by ladder |

**Important:** The ladder logic uses DF351 and DF352 (filtered values) for XV1 and XV2 autofill decisions. The Python service must write fresh raw values to DF251/DF252 continuously so the PLC's filter stays current.

### Solenoid Valve Registers
| Register | Type | Description |
|---|---|---|
| X001 | Input bit (read) | XV1 coil state (actual energized state) |
| X002 | Input bit (read) | XV2 coil state |
| X003 | Input bit (read) | XV3 coil state |
| Y101 | Output bit (read) | XV1 output (SET=open, RST=close) |
| Y102 | Output bit (read) | XV2 output |
| Y103 | Output bit (read) | XV3 output |
| DS1001 | Integer (read) | XV1 present state (1=energized, 0=de-energized) |
| DS1002 | Integer (write) | XV1 desired state (1=open, 0=close) |
| DS1003 | Integer (read) | XV2 present state |
| DS1004 | Integer (write) | XV2 desired state |
| DS1005 | Integer (read) | XV3 present state |
| DS1006 | Integer (write) | XV3 desired state |
| DS3 | Integer (read) | Cryostat fill status latch. `0` = empty (wants filling), `1` = full. Driven by ladder rungs 26/27 from DF303 as a Schmitt trigger — see below |
| DS1101 | Integer (write) | XV1 auto-close enable (1=on) |
| DS1102 | Integer (write) | XV1 auto-open enable (1=on) |
| DS1103 | Integer (write) | XV2 auto-close enable |
| DS1104 | Integer (write) | XV2 auto-open enable |
| DS1105 | Integer (write) | XV3 auto-close enable |
| DS1106 | Integer (write) | XV3 auto-open enable |
| DS1107 | Integer (write) | XV3 coast permit. **RETENTIVE, initial value 1.** 0 = the Python service is deliberately withholding a refill; any other value, including a never-written PLC, means permit granted |
| DS151 | Integer (write) | Ballast gas valve desired state (1=open, anything else=closed) → Y105 |
| DS152 | Integer (write) | Pump gas valve desired state → Y106 |
| DS153 | Integer (write) | Bottle gas valve desired state → Y107 |
| Y105 | Output bit (read) | Ballast gas valve output (SET while DS151 = 1, RST otherwise) |
| Y106 | Output bit (read) | Pump gas valve output |
| Y107 | Output bit (read) | Bottle gas valve output |

**Valve identity:**
- XV1 → Y101: ballast bottle LN2 fill (level sensor: DF351)
- XV2 → Y102: primary Xe bottle LN2 fill (level sensor: DF352)
- XV3 → Y103: cryostat LN2 vessel fill (level sensor: DF303)
- `gas_ballast` → Y105: solenoid valve on the ballast
- `gas_pump` → Y106: solenoid valve on the pump
- `gas_bottle` → Y107: solenoid valve on the bottle

**Gas-side solenoid valves (Y105–Y107).** These are direct relay commands, not
a copy of the XV1–XV3 pattern. Each is one pair of rungs — `DS15x = 1` SETs the
output, `DS15x ≠ 1` RSTs it — with no present-state register, no X-input
feedback, no auto-open/auto-close and no timer. The driver publishes
`xsphere/status/valve/gas_{ballast,pump,bottle}` as `{"desired": DS15x,
"state": Y10x}`, reading `state` from the output coil itself (FC1) and omitting
it when that read fails. They are commanded on the ordinary
`xsphere/commands/valve/{name}/state` topic. The `gas_` prefix is there because
`ballast` already names XV1, the LN2 fill valve for the ballast cryoflask.

Nothing closes these valves on its own: the service watchdog (ladder addition
4) does not cover them, so a stopped service or a PLC restart leaves each one
wherever its DS register last put it.

DS1105 and DS1106 stay purely operator-controlled — the coast gate never
writes them. It only ever writes DS1107, and DS1107 must never appear in the
XV3 close rung.

**Cryostat fill status latch (DS3) — ladder rungs 26/27**

The cryostat no longer compares the raw filtered level in the fill decision.
Two rungs drive an integer latch instead:

```
rung 26:   DF303 < 0.8   →   DS3 = 0     (empty — wants filling)
rung 27:   DF303 > 2.5   →   DS3 = 1     (full)
```

Between 0.8 and 2.5 neither rung fires and DS3 holds its previous value, so
DS3 is a Schmitt trigger with a wide deadband rather than a comparison. That
is what makes it usable as a control term: a bare `DF303 < 0.8` chatters as
the level noise crosses the threshold, whereas DS3 changes exactly twice per
fill cycle. Rung 28 uses DS3 — not DF303 — for both the open and the close
decision, so anything that wants to agree with the valve logic must read DS3
rather than re-deriving fill state from the level.

The driver publishes it as `fill_status` on
`xsphere/sensors/level/cryostat`, alongside `raw` and `filtered`. The field is
omitted, not defaulted, when the register read fails, so a consumer can tell
"the PLC says full" from "we could not ask the PLC".

**Autofill thresholds (from ladder):**
- XV1/XV2: auto-close when level > 2.5; auto-open when level < 0.5 (timer: 600 s)
- XV3 (cryostat), rung 28, current ladder:
  - **close** when `DS1105 = 1` (auto-close on) and `DS1005 = 1` (XV3 open)
    and (`DS3 = 1` **or** the max-open timer T3 has expired)
  - **lockout** — the same branch with `T3` expired *and* `DS3 = 0` writes
    `DS1106 = 0`, latching auto-open off. A fill that ran the full timer
    without ever reaching "full" is a leak, an empty dewar or a stuck valve,
    and the ladder refuses to retry it unattended
  - **open** when `DS1105 = 1` and `DS1106 = 1` and `DS1005 = 0` and
    `DS3 = 0` and `DF303 > 0.25` and T3 has not expired. The `> 0.25` term is
    a sensor-sanity floor, not a fill threshold: a probe reading below 0.25 is
    treated as disconnected rather than as a very empty vessel
  - **timer T3 SetPoint is 1200 s**, enabled by `DS1105`
- XV3, once ladder addition 6 is entered: auto-close and the fill-initiation
  condition (`DS3 = 0`) are unchanged, but the 0.25 lower bound becomes
  conditional. Auto-open holds off while the Python coast gate writes
  DS1107 = 0, still fires inside the trusted window if the service watchdog
  DS1099 goes stale, and gains a third branch that opens *below* 0.25 on
  independent dry evidence from a different sensor (DF4 > −138.0 °C, i.e. the
  LN2 vessel base above 135 K). See item 6 below.

> **Ladder addition 6 (DS1107) is still outstanding.** The current ladder
> export in `extras/plc-block-2.pdf` contains rungs 1–36 with no reference to
> DS1107 anywhere, so the coast permit has not been hand-entered yet and
> `coast.ladder_rung_entered` must stay `false`.

### Gas Flow Registers (MKS M330B + MKS bypass valve)

| Register | Type | R/W | Description |
|---|---|---|---|
| DF201 | Float | R | MKS flow signal, volts as read by ADC ch1 |
| DF205 | Float | W | MKS setpoint, volts commanded to DAC ch1 |
| DS1007 | Integer | R | Bypass valve (XV4) present state (1=open, 0=closed) |
| DS1008 | Integer | W | Bypass valve desired state (1=open, 0=closed) |
| DS1009 | Integer | R | MKS valve present mode (0=closed, 1=normal, 2=open) |
| DS1010 | Integer | W | MKS valve desired mode (0=closed, 1=normal, 2=open) |
| DS1099 | Integer | W | Slow-control service watchdog counter |

| Output | Point | Common | Drives |
|---|---|---|---|
| Y104 | C0-08TR relay | C1 | MKS bypass solenoid pilot (XV4) |
| *unassigned* | — | — | MKS valve **OPEN** override — MFC pin 4 → signal common |
| *unassigned* | — | — | MKS valve **CLOSE** override — MFC pin 3 → signal common |

**Why the MKS valve is a mode integer, not two bits.** The override pins are
active-low and independent, and asserting both is resolved by the MFC as
**valve open** — it fails in the direction that lets gas through. Software
therefore writes **one integer** to DS1010 and the ladder decodes it with a
mutual-exclusion rung, so no software fault — a bad MQTT payload, a race, a
half-written register — can produce an unintended purge. It also makes NORMAL
unambiguous: it is the state where neither relay is energised, so a de-energised
or unpowered relay module lands in setpoint-following mode rather than a
latched override.

Coil addresses follow CLICK's 32-per-slot bit stride, **not** the point number:
Y101 → 8224, Y104 → 8227, Y105 → 8228, Y106 → 8229, Y107 → 8230.

### Required CLICK ladder additions

These must be added by hand in the CLICK programming software; the Python
service cannot create them:

1. **Analog scaling.** Configure Slot0 AI ch1 → DF201 and AO ch1 → DF205 for
   0–10 V engineering units (0.0–10.0), so the registers are in volts. The
   module is 0–10 V only — it is not range-selectable, which is what makes it
   survive the MFC's ~7.5 V power-up transient.

2. **MKS valve mode decode**, with mutual exclusion. `Y_open` and `Y_close`
   are placeholders — the points first earmarked for them, Y105 and Y106, now
   drive the ballast and pump solenoid valves, so pick the outputs before
   entering this (see Relay commons in §3.1c):
   ```
   Y_open       = (DS1010 == 2) AND NOT Y_close
   Y_close      = (DS1010 == 0) AND NOT Y_open
   DS1009       = 2 if Y_open else (0 if Y_close else 1)   ; readback
   ```
   With neither relay energised both override pins float and the MFC runs in
   its NORMAL mode, following the DF205 setpoint. That is the intended
   `mode = 1` state — it needs no relay.

3. **Bypass valve**: `Y104 = DS1008`, and `DS1007 = Y104` for readback.
   Mirror the existing XV1–XV3 rungs.

4. **Service watchdog (safety-critical).** DS1099 is incremented by the Python
   service every poll. If it stops changing for ~10 s, the ladder must shut
   **both** legs:
   - force `DS1010 = 0` (MKS valve hard shut),
   - force `DF205 = 0.0` (setpoint to zero), and
   - force `DS1008 = 0` (**bypass valve shut**).

   Shutting only the MFC is not enough. The bypass is the *unmetered* leg — if
   the service dies mid-recovery with the bypass open, gas keeps moving and the
   flow reading reads a confident zero, because the MFC it is measuring is
   closed. The unmonitored path is the one that most needs the interlock.

   Without this rung, a crashed slow-control service or a pulled network cable
   leaves the last setpoint latched in the analog output and xenon flowing
   indefinitely. The PLC retains register values across a Modbus disconnect —
   it does not fail safe on its own.

   The driver withholds the kick when it cannot read the gas-flow registers, so
   a driver that has gone blind stops asserting liveness rather than vouching
   for hardware it is not seeing.

5. **Bypass maximum-open timer.** Mirror the XV1–XV3 autofill pattern: if the
   bypass has been open longer than a configured limit, shut it and latch a
   fault. Until this exists, an unattended fill through the bypass has no
   time-bounded protection.

6. **XV3 coast-aware auto-open (safety-critical).** The `.ckp` project file is
   a password-encrypted blob, so this cannot be scripted, generated, or
   diffed — it has to be hand-entered in the CLICK programming software.

   New register **DS1107**, XV3 coast permit. Integer, write, **RETENTIVE**,
   initial value = **1**. 0 = the Python service is deliberately withholding a
   refill; any other value, including a never-written PLC, means permit
   granted — so a project without this rung behaves exactly as it does today.

   ```
   ladder_dry = DF4 > -138.0                  ; 135 K, DF4 is in degC
   wd_stale   = DS1099 unchanged for 10 s     ; reuse the item-4 timer

   XV3_auto_open_request =
         DS1106                                    ; armed (unchanged)
     AND DF303 < 0.8                               ; fill-initiation upper
                                                   ;   bound (unchanged)
     AND (   (DF303 > 0.25 AND DS1107 = 1)         ; A: level in the trusted
                                                   ;    window, Python permits
          OR (DF303 > 0.25 AND wd_stale)           ; B: Python dead, level
                                                   ;    still trusted
          OR  ladder_dry )                         ; C: independent dry
                                                   ;    evidence, any level
   ```

   Auto-close (DF303 > 2.5) and timer T3 are otherwise **UNCHANGED**. DS1107
   must never appear in the close rung.

   **A** is normal operation: the permit can only ever subtract from what the
   ladder would otherwise do inside its trusted window. **B** fails safe to
   autonomous filling — DS1107 is retentive, so without this branch a crashed
   service that left DS1107 = 0 would inhibit XV3 forever. **C** restores the
   backup layer that coast would otherwise delete: it drops the 0.25 floor,
   which coast deliberately violates, but substitutes DF4 evidence from a
   different sensor on a different module in different units, so two
   independent sensors must agree before XV3 opens below the trusted window.

   Coast never writes DS1105 or DS1106; those stay purely operator-controlled.
   On a clean shutdown the PLC driver writes DS1107 = 1 directly over Modbus,
   rather than publishing an MQTT command that would race its own broker
   round-trip against the disconnect. An unclean death is covered by branch B.

   Without this rung the ladder's own auto-open window (0.25 < DF303 < 0.8)
   tops the vessel up during the descent, coast never sees an empty vessel,
   and the feature silently does nothing — the level is held at the very
   threshold coast is waiting to fall below. Arming with
   `coast.ladder_rung_entered: false` is allowed but logs a loud warning,
   because that failure wastes LN2 rather than endangering anything while
   looking exactly like a broken feature.

   **T3 and `fill_timeout_s` must be changed together.** A from-dry coast fill
   is a much longer transfer than the top-up 920 s was sized for. If T3 is
   raised to 1800 s, raise `autovalve.vessels.cryostat.fill_timeout_s` to 1800
   in the SAME visit — the two must match.

### PID Registers (Float, read/write)

All temperatures in °C (PLC native). Python service converts to/from Kelvin.

**HTR1 — Zone 1 (top clamp heater), PWM → Y004:**
| Register | Name | R/W | Description |
|---|---|---|---|
| DF100 | SP_Setpoint | R/W | Temperature setpoint |
| DF105 | P_Gain | R/W | Proportional gain (Kp) |
| DF106 | I_Reset | R/W | Integral reset time (Ki) |
| DF107 | D_Rate | R/W | Derivative rate (Kd) |
| DF108 | OUT_Control | R | Current control output (0–100%) |
| DF111 | PV_ProcessRaw | R | Raw process variable (°C) |
| DF112 | PV_ProcessVar | R | Filtered process variable (°C) |
| DF104 | Bias | R/W | Manual bias |

**HTR2 — Zone 2 (bottom clamp heater), PWM → Y003:**
| Register | Name | R/W | Description |
|---|---|---|---|
| DF125 | SP_Setpoint | R/W | Temperature setpoint |
| DF130 | P_Gain | R/W | Kp |
| DF131 | I_Reset | R/W | Ki |
| DF132 | D_Rate | R/W | Kd |
| DF133 | OUT_Control | R | Output (0–100%) |
| DF136 | PV_ProcessRaw | R | Raw PV (°C) |
| DF137 | PV_ProcessVar | R | Filtered PV (°C) |
| DF129 | Bias | R/W | Manual bias |

**HTR3 — Zone 3 (nozzle/disk heater), PWM → Y002:**
| Register | Name | R/W | Description |
|---|---|---|---|
| DF151 | SP_Setpoint | R/W | Temperature setpoint |
| DF155 | P_Gain | R/W | Kp (DF156 = Ki, DF157 = Kd per cross-ref) |
| DF156 | I_Reset | R/W | Ki |
| DF157 | D_Rate | R/W | Kd |
| DF158 | OUT_Control | R | Output (0–100%) |
| DF161 | PV_ProcessRaw | R | Raw PV (°C) |
| DF162 | PV_ProcessVar | R | Filtered PV (°C) |
| DF155 | Bias | R/W | Manual bias |

**Note on HTR3:** DF Memory Start = DF150 per PID config, but cross-reference confirms SP_Setpoint = DF151. DF150 is the first block register (likely PID internal). Verify on live system at commissioning.

### Current MQTT Topics (existing schema, to be replaced)
| Topic | Direction | Content | Consumer |
|---|---|---|---|
| `sensor/ch4_voltage` | ESP32 → PLC NR | Ballast level raw | PLC Node-RED → DF251 |
| `sensor/ch5_voltage` | ESP32 → PLC NR | Primary bottle level raw | PLC Node-RED → DF252 |
| `PLC RTD` | PLC NR → RPi NR | RTD1–4 JSON | RPi Node-RED → InfluxDB |
| `PLC XV1/XV2/XV3` | PLC NR → RPi NR | Valve state JSON | RPi Node-RED → InfluxDB |
| `PLC PID1/PID2/PID3` | PLC NR → RPi NR | PID state JSON | RPi Node-RED → InfluxDB |
| `PLC ADC` | PLC NR → RPi NR | Analog inputs JSON | RPi Node-RED → InfluxDB |
| `RDXL6SD/temps` | Omega svc → RPi NR | TC+RTD JSON | RPi Node-RED → InfluxDB |

In the new architecture, all `PLC *` topics are replaced by the Python service reading via Modbus TCP directly and publishing to `xsphere/sensors/...`. The `sensor/ch4_voltage` and `sensor/ch5_voltage` topics are replaced by the new `xsphere/sensors/level/...` schema, with the Python service responsible for writing values to DF251/DF252.

---

## 7. Development Roadmap

### Phase 1 — Infrastructure (No new features, foundation only)
- [ ] Migrate data ingestion from Node-RED to Telegraf (MQTT consumer → InfluxDB)
- [ ] Establish new `xsphere/` MQTT topic schema; update ESP32 firmware and Omega logger
- [ ] Deploy Grafana; build monitoring dashboard (all temperatures, pressures, levels)
- [ ] Clean up Node-RED: remove parse/DB flows, keep only valve button dashboard

### Phase 2 — Python Service Core
- [ ] Python service skeleton (systemd, YAML config, MQTT pub/sub, plugin registry)
- [ ] PLC Modbus TCP driver (read RTD registers, write PID setpoints)
- [ ] Gradient controller abstraction (compute PV source from ΔT target, write to PLC)
- [ ] Autovalve controller (state machine wrapping PLC solenoid valve logic)
- [ ] Interlock watchdog (e.g., temp too high → alert; level sensor fail → alert)
- [ ] Service heartbeat and status publishing

### Phase 3 — Node-RED Dashboard
- [ ] Real-time temperature panel (all 10 channels, live, no history)
- [ ] Pressure & level panel
- [ ] Solenoid valve controls (open/close + autovalve toggle per vessel)
- [ ] PID setpoint controls (per zone)
- [ ] Gradient configuration (mode dropdown + ΔT numeric input)
- [ ] Plugin activation panel
- [ ] Alert / interlock status display
- [ ] Service heartbeat indicator

### Phase 4 — Advanced Modules
- [ ] Temperature gradient scanner plugin (integrate with wind camera analysis)
- [ ] Mass scale RS-232 driver (LN2 dewar mass tracking)
- [ ] Leybold pumping stand serial interface

### Phase 4b — Gas handling sequencing

The `GasFlowController` path abstraction (`mks` / `bypass` / `isolated`) is the
primitive a sequencer drives: a step says "route through MKS at 50 sccm", not
"energise Y104 and de-energise Y106". Extending sequencing to the full gas
panel needs, in order:

- [ ] **Hardware prerequisite.** Three gas-side valves are now relay-actuated
      — ballast (Y105), pump (Y106) and bottle (Y107), commanded through
      DS151–DS153 (§6b). The other valves in §3.1 are still **hand
      valves**; automating one needs a solenoid-actuated valve and a relay
      point. Relay budget: **one point
      (Y108) remains.** C1 (Y101–Y104) is fully committed to the LN2 fill
      valves plus the MKS bypass pilot, and C2 (Y105–Y108) now carries the
      three gas valves. The MKS override contacts were displaced by this and
      cannot share either common (§3.1c), so they need a second module or an
      interposer before the MFC valve override can be wired at all.
- [ ] Generalise the named-path concept from the MKS/bypass pair to a
      **manifold state** covering every controlled valve, so a sequence step
      names a whole configuration and the controller works out which valves
      must move and in what order.
- [ ] Sequencer plugin (modelled on `gradient_scanner`): ordered steps with
      per-step hold conditions — target pressure reached, target integrated
      volume delivered, dwell elapsed — plus abort-to-safe-state.
- [ ] Integrate pressure feedback (Setra gauges via GHS ESP32) as step
      transition conditions, and totalise delivered volume by integrating the
      MFC flow signal.
- [ ] Extend interlocks with cross-valve rules (e.g. refuse to open the bottle
      valve while the pump path is open).

### Phase 5 — Documentation
- [ ] Update Notion pages (Computer & Network, Slow Control sections)
- [ ] Update gas handling system Notion documentation
- [ ] Add wiring diagrams and sensor maps

---

## 8. Outstanding Questions / Decisions

- **PLC Modbus register map:** Need the full register map from the CLICK PLC project file (XMS-control.ckp) to know which registers correspond to RTD inputs, PID setpoints, PV sources, and solenoid valve outputs. User will provide ladder logic.
- **Telegraf vs. direct InfluxDB write in Python service:** Telegraf handles all raw sensor data ingestion; Python service may write its own derived quantities (e.g., gradient ΔT, controller state) directly to InfluxDB or publish them to MQTT for Telegraf to pick up. TBD.
- **Gradiometer upgrade timeline:** If TC channels are converted to differential gradiometers, the Omega logger will need firmware/config updates and the Grafana dashboard will need new panels. This is a hardware change that should be coordinated with software updates.
- **Scale model and RS-232 protocol:** To be looked up from manual when prioritized.
- **Pumping stand serial interface:** Same — low priority, look up model specs when prioritized.
- **Measured dry level reading (cryostat probe):** `coast.empty_threshold` is
  shipped at 0.13, which is a placeholder, not a measurement. Nothing in the
  repo maps the FDC1004 coaxial probe's pF reading to litres, so there is no
  defensible default. It has to be set to the reading observed with the vessel
  actually dry, plus 0.05, after a supervised boil-dry. Until that is done,
  coast either saves nothing (threshold too close to `level_low`, so it
  triggers barely later than plain autofill) or ends every episode on a
  backstop (threshold below the probe's dry floor, so the level term is never
  satisfied and only `max_duration_s`, `sink_warm_k` or `ceiling_k` can
  release the fill).
- **Measured cold-block thermal time constant:** `coast.delta_max_k` = 40 K
  is derived from an *estimated* time constant for the aluminium block and
  braids, not a measured one, and `coast.max_duration_s` = 5400 s is a sanity
  bound rather than a tuned value. Both need a logged boil-dry — DF2 and DF4
  through the whole descent — before the ΔT figure can be claimed to
  correspond to any particular amount of cooling authority left.
- **Measured DF2 − DF4 offset at 295 K:** DF1–DF3 are Pt100 and DF4 is Pt1000
  on the same C0-04RTD module, so the coast gate's ΔT includes a systematic
  sensor-type mismatch of unknown size. Log both channels with the whole
  assembly at room temperature and subtract the offset, otherwise it is not
  known how much of a 40 K reading is a real temperature difference.
  (`config.py` already refuses `delta_max_k` below 10 K on the grounds that
  the mismatch band is comparable to the gate itself.)

---

## 9. Users & Access Model

| User | Role | Primary interface | Technical level |
|---|---|---|---|
| PI | Periodic monitoring | Grafana (read-only) | Non-technical |
| System owner | Architect, primary operator | All layers | Expert |
| Graduate student | Daily operator, experiments | Node-RED dashboard | Power user, needs guardrails |

The Node-RED dashboard must be sufficiently self-explanatory that the graduate student can execute standard operating procedures (cool down, autofill, xenon fill, gradient set, safe shutdown) without understanding the underlying layers. Complex experimental modes (gradient scanning) are activated via the dashboard but configured and understood by the system owner.

---

## 10. Reference Projects

| Project | Location | Relevance |
|---|---|---|
| ETS-pythonSLOWDAQ | `references/ETS-pythonSLOWDAQ/` | Primary architecture reference for Python service |
| usphere-DAQ | `references/usphere-DAQ/` | Plugin architecture and module patterns |
| gas-handling-system | `references/gas-handling-system/` | Current GHS ESP32 firmware |
| liquid-level-sensor | `references/liquid-level-sensor/` | Current FDC1004 firmware |
| XMS-PLC | `references/XMS-PLC/` | Current production Node-RED flows |
| RDXL6SD-temperature-logger | `references/RDXL6SD-temperature-logger/` | Current Omega Python service |
| Slow Control (Notion export) | `references/Slow Control/` | Existing documentation |
| Computer and Network (Notion export) | `references/Computer and Network/` | Network topology docs |
