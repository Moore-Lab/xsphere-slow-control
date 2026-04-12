# xsphere Slow Control System

> **Draft for Notion import — 2026-04-12**  
> This document is a comprehensive description of the xsphere slow control system as-built and as-planned. It is intended as a starting point for updating and expanding the Notion documentation pages.

---

## Overview

The xsphere slow control system monitors and controls the cryogenic and gas handling infrastructure for the xenon microsphere trapping experiment. The system spans three categories of hardware:

- A **CLICK Plus PLC** that executes real-time automation: temperature PID control, LN2 autofill, and safety interlocks
- A **Raspberry Pi server** (xbox-pi) that runs the data infrastructure: MQTT broker, InfluxDB database, and Node-RED
- Several **ESP32 microcontrollers** that read peripheral sensors: gas handling pressures and environmental sensors, and liquid level sensors on cryogenic vessels

Data from all devices flows into InfluxDB for logging and is accessible remotely through a browser interface.

---

## Experimental Context

Xenon gas is blown through a cooled sintered-mesh nozzle inside a 2.75" conflat cube. The mesh is cooled to liquid xenon temperatures (~165 K), causing xenon to liquefy and form droplets that are injected into the trapping chamber where they are captured optically or magnetically. Once trapped, pressure is reduced to freeze the droplets, and chamber wall temperatures are further reduced to minimize radiative heating of the frozen particle.

Convective airflow ("wind") inside the xenon chamber is a primary challenge. Temperature gradients across the chamber drive or suppress this wind. Controlling vertical and longitudinal gradients via three independent heater zones is the main experimental knob.

---

## Cryogenic System

### Physical Layout

All cryogenic components are housed inside an outer vacuum chamber. An LN2 vessel provides cooling, thermally coupled to the xenon chamber (Xe cube) through copper braids and aluminum clamps — there is no direct fluid connection between the LN2 circuit and the xenon circuit.

```
Outer Vacuum Chamber
├── LN2 Vessel
│   ├── Level sensor (coaxial FDC1004)
│   └── Aluminum block → copper braids → Top Clamp, Bottom Clamp, Aluminum Disk
│
├── Top Clamp (around Xe cube)       → Heater Zone 1
├── Bottom Clamp (around Xe cube)    → Heater Zone 2
│
└── Xe Cube (2.75" CF, 5 viewports)
    ├── Aluminum disk on fill flange → Heater Zone 3
    └── Nozzle (sintered mesh) → connected to gas handling via bellows
```

### Temperature Sensors

Ten temperature channels total, split between two readout instruments:

**PLC RTD Module (4 channels):**
| # | Location |
|---|---|
| 1 | Xe cube top |
| 2 | Xe cube bottom |
| 3 | Xe cube nozzle region |
| 4 | LN2 vessel base |

**Omega RDXL6SD-USB (6 channels, fully utilized):**
| # | Type | Location |
|---|---|---|
| 5 | RTD | Top clamp, Xe-cube side |
| 6 | RTD | Bottom clamp, Xe-cube side |
| 7 | K-type TC | LN2 vessel top |
| 8 | K-type TC | LN2 vessel bottom |
| 9 | K-type TC | Top clamp, vessel side of braid |
| 10 | K-type TC | Bottom clamp, vessel side of braid |

The Xe cube RTDs (channels 1–3) are the preferred PID feedback sensors — they measure the actual xenon chamber temperature. The clamp RTDs (5–6) are the backup. Thermocouples at the clamps (9–10) measure the LN2 vessel side of each braid, providing gradient information across the thermal links.

**Planned upgrade:** Replace K-type thermocouples with differential gradiometers (both junctions placed on opposite faces of the Xe cube) to directly measure spatial temperature differences across the chamber.

### Heater Zones

Three independent PID-controlled heater zones, all running on the CLICK Plus PLC:

| Zone | Heater location | Heater | Drive | Preferred feedback |
|---|---|---|---|---|
| 1 — Top | Top clamp | 36 W DC resistive | DIN SSR + PLC PWM | Xe cube top RTD |
| 2 — Bottom | Bottom clamp | 36 W DC resistive | DIN SSR + PLC PWM | Xe cube bottom RTD |
| 3 — Nozzle | Aluminum disk on fill flange | 36 W DC resistive | DIN SSR + PLC PWM | Xe cube nozzle RTD |

**Gradient control:** A vertical temperature gradient is established by offsetting Zone 2's setpoint relative to Zone 1 (e.g., bottom = top + 5 K). Currently this requires manually rewriting a PLC register. A planned software improvement will expose this as a simple gradient target input.

### LN2 Supply

LN2 is supplied from a portable dewar (sitting on a mass scale with RS-232 output — integration planned) through a manifold with three PLC-controlled solenoid valves:

| Valve | Destination |
|---|---|
| Solenoid 1 | Cryostat LN2 vessel |
| Solenoid 2 | Primary xenon bottle cryoflask |
| Solenoid 3 | Ballast bottle cryoflask |

Each valve has three operating modes: **manual open/close**, **auto-close** (closes when level sensor reads full), and **auto-open** (opens when level drops below threshold; automatically arms auto-close).

---

## Gas Handling System

### Overview

The gas handling system manages xenon storage, transfer to the Xe cube, and recovery. All gas-path connections use 1/4" tube with VCR fittings.

### Flow Path

```
[Xe Cube] — tube — [Valve A] — bellows — [Valve B] — 4-way Cross A
                                                            │
                    ┌───────────────────────────────────────┤
                    │                                       │
           Path 1 (gauges + pump)                 Path 3 (ballast)
           Setra 225 #1 (cube pressure)           Hand valve → needle valve
           PenningVAC #1 (vacuum, split)          → Ballast bottle (1L)
           → 2.75" CF tee                           (cryoflask + level sensor)
             ├── PenningVAC full-range gauge
             └── Hand valve → RGA (SRS 200) + Leybold turbo pumping stand
           → Valve → [Gas Purifier] → Valve ─┐
                                              │
           Path 2 (bypass, around purifier)   │
           Bypass valve ──────────────────────┘
                                              │
                                         Valve → Cross B
                                              │
                              ┌───────────────┤
                              │               │
                        Setra 225 #2    Valve → Primary Xe bottle (1L, ~3 bar)
                        (bottle press.)         cryoflask + level sensor
                              │
                        Valve → Secondary Xe bottle (4L, 50 bar, regulator)
                                 (manual cryohose for recovery)
```

### Xenon Bottles

| Bottle | Volume | Nominal pressure | Cryoflask | Level sensor | Manifold solenoid |
|---|---|---|---|---|---|
| Primary | 1 L | ~3 bar | Yes | Yes | Yes (solenoid 2) |
| Secondary | 4 L | 50 bar (regulator) | Yes | No | No (manual cryohose) |
| Ballast | 1 L | ~0 (normally empty) | Yes | Yes | Yes (solenoid 3) |

**Ballast bottle uses:** Allows slower, more controlled cryopumping. Also provides extra volume buffer for safe operation.

### Safe Operating Configuration

After transferring xenon to the Xe cube in liquid form, the safe configuration is: open valves to the primary bottle and ballast. If cooling is lost and xenon vaporizes, the combined primary + ballast volume absorbs the pressure without reaching the burst disk rating. This allows unattended operation without an automated recovery system at current xenon inventory levels.

### Pressure Sensors

All read via two ADS1115 I2C ADCs on the GHS ESP32, 0–10V:

| Sensor | Type | Location |
|---|---|---|
| Setra 225 #1 | Manometer | Xe cube line |
| Setra 225 #2 | Manometer | Primary Xe bottle |
| Setra 225 #3 | Manometer | Secondary Xe bottle |
| PenningVAC #1 | Full-range | GHS vacuum (signal split to pumping stand) |
| PenningVAC #2 | Full-range | Outer vacuum (signal split to pumping stand) |

The GHS ESP32 also reads ambient conditions: BMP3XX (barometric pressure + temperature) and DHT11 (humidity + temperature).

### Pumping

Two Leybold turbo-based pumping stands (same model), one for the GHS/Xe system, one for the outer vacuum. Manual gate valves between turbo and chamber. Serial interface to pumping stands is not yet implemented (planned future work).

### RGA

SRS 200 residual gas analyzer, connected via serial to the DAQ computer (xbox-DAQ). Not integrated with the slow control system.

---

## Computer & Network

### Machines

| Name | Hardware | IP | Role |
|---|---|---|---|
| xbox-pi | Raspberry Pi | 192.168.8.116 | Server (MQTT, InfluxDB, Node-RED) |
| xbox-DAQ | Desktop | static | PLC programming, DAQ, RGA |
| xbox-PLC | CLICK Plus PLC | static | Automation controller |
| xbox-radio | GL-SFT1200 router | 192.168.8.1 | Local WiFi hub |

**Network:** 192.168.8.x subnet, WiFi SSID "xbox-radio"  
**Remote access:** Yale VPN → SSH tunnel through router  
**Port forwarding:** 2500–2534 (MQTT, Node-RED, InfluxDB, Portainer, SSH)

### xbox-pi Software Stack (IOTstack / Docker)

| Service | Purpose |
|---|---|
| Mosquitto | MQTT broker (port 1883) |
| InfluxDB 2.x | Time-series database |
| Node-RED | Data parsing, dashboard, flow automation |
| Portainer | Container management |

Additional systemd service: `RDXL6SD-mqtt.service` — Python logger for Omega temperature device (pymodbus serial → MQTT).

### CLICK Plus PLC

- Built-in Ethernet: Modbus TCP (native, no module required)
- Node-RED module: installed, used for register inspection and manual commands
- Ladder logic: programmed from xbox-DAQ; controls heater PIDs, solenoid valves, RTD readout
- All PID loops run on the PLC; setpoints written via Modbus register values

---

## Current Data Pipeline

```
Device → MQTT → Node-RED (RPi) → InfluxDB 2.x
```

All sensor data from all devices (PLC, Omega logger, GHS ESP32, level sensor ESP32s) is published to MQTT, received by Node-RED on the RPi, parsed, and written to InfluxDB via the InfluxDB Node-RED node.

Monitoring is done through the InfluxDB browser interface. A minimal Node-RED dashboard exists with buttons for the three LN2 solenoid valves.

---

## Planned Improvements

### 1. Data Pipeline — Telegraf replaces Node-RED

Replace Node-RED's data ingestion role with Telegraf (MQTT consumer plugin → InfluxDB). This removes a fragile intermediary from the critical data path and simplifies Node-RED to a pure UI layer.

### 2. Grafana Monitoring Dashboard

Connect Grafana to the existing InfluxDB instance. Build dashboards for: all temperature channels, pressures, vacuum levels, LN2 levels. Accessible remotely from any browser.

### 3. Python Slow Control Service

A new Python service running on xbox-pi (systemd) that:
- Talks to the PLC directly via Modbus TCP (read RTDs, write setpoints)
- Exposes gradient control abstraction (set ΔT targets rather than raw register values)
- Manages autovalve state machines
- Runs safety interlocks
- Hosts a plugin system for advanced experimental modules

### 4. Rebuilt Node-RED Dashboard

A proper operator panel in Node-RED covering:
- Live temperature display (all channels)
- Solenoid valve controls + autovalve toggles
- PID setpoint inputs per zone
- Gradient mode selector and ΔT input
- Plugin activation buttons
- Interlock status and service health

### 5. Gradient Scanner Plugin

An experimental automation module that sweeps vertical and longitudinal temperature gradients across a configurable parameter space, records the wind response from camera analysis, and identifies optimal gradient conditions for stable trapping.

### 6. Future Integrations (lower priority)
- LN2 dewar mass scale (RS-232)
- Leybold pumping stand serial interface
- Thermocouple → gradiometer hardware upgrade

---

## Standard Operating Procedures (Outline)

*(To be expanded)*

1. **Cool down:** Set Zone 1/2/3 setpoints; enable autofill on LN2 vessel; monitor temperatures in Grafana until stable
2. **Xenon fill:** Open ballast and primary bottle valves; open bellows valves; monitor cube pressure and nozzle RTD
3. **Trapping:** Confirm liquid formation at nozzle; operate optical/magnetic trap; adjust gradients if wind observed
4. **Freeze:** Reduce chamber pressure; monitor cube temperatures; confirm freeze
5. **Safe configuration:** Open valves to primary bottle and ballast; verify pressure stable
6. **Warm up / recovery:** Close bellows valves; cryopump xenon into primary bottle (fill cryoflask with LN2); verify bottle pressure; close all valves
