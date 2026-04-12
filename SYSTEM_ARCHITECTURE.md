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

xsphere/status/service/heartbeat          # Python service uptime (retained)
xsphere/status/controllers/{name}         # Controller state JSON (retained)
xsphere/status/valves/{name}              # Solenoid valve state (retained)
xsphere/alerts/{rule}                     # Interlock alerts

xsphere/commands/pid/{zone}/setpoint      # Write PID setpoint (K)
xsphere/commands/pid/{zone}/pv_source     # Set PV source (e.g., "cube_top")
xsphere/commands/gradient/vertical        # Set vertical ΔT (K)
xsphere/commands/gradient/longitudinal    # Set longitudinal ΔT (K)
xsphere/commands/valve/{name}/state       # Open/close solenoid valve
xsphere/commands/autovalve/{vessel}/mode  # Enable/disable autofill
xsphere/commands/plugin/{name}/start      # Activate plugin
xsphere/commands/plugin/{name}/stop       # Deactivate plugin
```

PID zone naming: `top`, `bottom`, `nozzle`  
Valve naming: `ln2_cryostat`, `ln2_primary`, `ln2_ballast`  
Vessel naming: `cryostat`, `primary_xe`, `ballast`

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
| DF201 | IN ch1 | 0–10V | TBD |
| DF202 | IN ch2 | 0–10V | TBD |
| DF203 | IN ch3 | 0–10V | Cryostat LN2 level sensor (raw) |
| DF204 | IN ch4 | 0–10V | TBD |
| DF205 | OUT ch1 | 0–10V | Analog output (TBD) |
| DF206 | OUT ch2 | 0–10V | Analog output (TBD) |

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
| DS1101 | Integer (write) | XV1 auto-close enable (1=on) |
| DS1102 | Integer (write) | XV1 auto-open enable (1=on) |
| DS1103 | Integer (write) | XV2 auto-close enable |
| DS1104 | Integer (write) | XV2 auto-open enable |
| DS1105 | Integer (write) | XV3 auto-close enable |
| DS1106 | Integer (write) | XV3 auto-open enable |

**Valve identity:**
- XV1 → Y101: ballast bottle LN2 fill (level sensor: DF351)
- XV2 → Y102: primary Xe bottle LN2 fill (level sensor: DF352)
- XV3 → Y103: cryostat LN2 vessel fill (level sensor: DF303)

**Autofill thresholds (from ladder):**
- XV1/XV2: auto-close when level > 2.5; auto-open when level < 0.5 (timer: 600 s)
- XV3 (cryostat): auto-close when level > 2.5; auto-open when 0.25 < level < 0.8 (timer: 920 s, safety shutoff if timer expires while level empty)

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
