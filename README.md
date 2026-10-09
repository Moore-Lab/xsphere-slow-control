# xsphere Slow Control

Slow control system for the xsphere cryostat experiment at Yale University.
Monitors and controls temperatures, LN2 fill levels, and gas handling for a
xenon cryostat used in levitated-particle physics experiments.

## What this system does

- **Reads temperatures** from the CLICK PLC (RTDs on the cryostat zones) and
  the Omega RDXL6SD-USB data logger (clamp RTDs, thermocouples)
- **Controls heaters** via three PID zones (top / bottom / nozzle) on the PLC,
  with a Python gradient abstraction layer (gradient mode or per-zone absolute)
- **Manages LN2 autofill** for the cryostat, ballast and primary xenon dewars
  via solenoid valves, with configurable level thresholds and fill-timeout
  safety, plus an optional **coast refill** for the cryostat that holds the
  fill back while the vessel runs on the thermal mass of its cold block, until
  two RTDs show that reserve is spent
- **Monitors pressure and vacuum** from the gas handling system (GHS) ESP32
- **Watches safety interlocks** — alerts on stale sensors, out-of-range
  temperatures, and saturated heater output
- **Logs everything** to InfluxDB via Telegraf, visualized in Grafana
- **Provides a control dashboard** in Node-RED (tablet-friendly)

## System overview

```
                    ┌─────────────────────────────────────────┐
                    │              xbox-pi (RPi)              │
                    │                                         │
  PLC ──Modbus TCP──► Python slow control service             │
                    │   · PlcDriver (poll + command)          │
                    │   · GradientController                  │
  Omega ──USB/RTU───► Omega logger service                    │
  (RDXL6SD-USB)    │                                         │
                    │   Mosquitto MQTT broker :1883           │
  GHS ESP32 ──WiFi─►                                         │
  Level ESP32s─WiFi►   Telegraf ──────────────► InfluxDB     │
                    │   Node-RED dashboard                    │
                    │   Grafana                               │
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
│   ├── requirements.txt
│   ├── xsphere-slowcontrol.service
│   ├── core/
│   │   ├── config.py           ← typed config dataclasses
│   │   ├── mqtt.py             ← thread-safe MQTT client wrapper
│   │   └── service.py          ← service orchestrator
│   ├── drivers/
│   │   ├── base.py             ← abstract sensor driver
│   │   └── plc.py              ← CLICK PLC Modbus TCP driver
│   ├── controllers/
│   │   ├── base.py             ← abstract controller
│   │   ├── gradient.py         ← gradient/absolute setpoint control
│   │   ├── autovalve.py        ← LN2 autofill state machines
│   │   └── interlocks.py       ← safety watchdog (alert-only)
│   └── plugins/
│       └── gradient_scanner.py ← automated temperature scan plugin
│
├── omega-logger/               ← standalone Omega RDXL6SD-USB service
│   ├── omega_logger.py
│   ├── config.yaml
│   ├── requirements.txt
│   └── xsphere-omega-logger.service
│
├── telegraf/                   ← Telegraf MQTT→InfluxDB pipeline
│   ├── telegraf.conf
│   └── .env.example            ← copy to .env and fill in secrets
│
├── firmware/
│   ├── ghs-esp32/              ← Gas Handling System ESP32 (pressure/vacuum)
│   │   ├── platformio.ini
│   │   └── src/main.cpp
│   └── level-sensor/           ← LN2 level sensor ESP32 (FDC1004)
│       ├── platformio.ini      ← builds two environments: ballast, primary_xe
│       └── src/main.cpp
│
└── nodered/
    └── dashboard-flows.json    ← import into Node-RED
```

## Quick orientation

| Component | Runs on | Language | Start command |
|---|---|---|---|
| Slow control service | xbox-pi | Python | `systemctl start xsphere-slowcontrol` |
| Omega logger | xbox-pi | Python | `systemctl start xsphere-omega-logger` |
| Telegraf | xbox-pi (Docker) | — | `systemctl start telegraf` (or Docker) |
| MQTT broker | xbox-pi (Docker) | — | already running via IOTstack |
| InfluxDB | xbox-pi (Docker) | — | already running via IOTstack |
| Node-RED | xbox-pi (Docker) | — | already running via IOTstack |
| GHS ESP32 | GHS board | C++ | flash with PlatformIO |
| Level sensor ESP32s | dewar boards | C++ | flash with PlatformIO |

## MQTT topic schema

| Topic | Direction | Description |
|---|---|---|
| `xsphere/sensors/temperature/plc/{ch}` | PLC→broker | RTD/TC readings from PLC |
| `xsphere/sensors/temperature/omega/{ch}` | Omega→broker | TC/RTD readings from Omega logger |
| `xsphere/sensors/level/{vessel}` | ESP32 / PLC driver→broker | LN2 level (raw pF); the cryostat channel comes from the PLC ADC |
| `xsphere/sensors/pressure/{gauge}` | GHS ESP32→broker | Pressure (PSI) |
| `xsphere/sensors/vacuum/{gauge}` | GHS ESP32→broker | Vacuum (mbar) |
| `xsphere/sensors/environment/{sensor}` | GHS ESP32→broker | Lab T/RH/P |
| `xsphere/status/pid/{zone}` | PLC driver→broker | PID setpoint/PV/output |
| `xsphere/status/valve/{vessel}` | PLC driver→broker | Valve state, auto enables, coast permit readback |
| `xsphere/status/level/{vessel}` | Python→broker | Raw and filtered LN2 level (not retained) |
| `xsphere/status/coast/{vessel}` | Python→broker | Coast gate state, hold reason and thresholds |
| `xsphere/status/gradient` | Python→broker | Gradient mode and parameters |
| `xsphere/status/interlocks` | Python→broker | Active alerts and ok flag |
| `xsphere/alerts/{rule}/{channel}` | Python→broker | Individual alert payloads |
| `xsphere/commands/gradient/{param}` | Dashboard→Python | Setpoint/mode commands |
| `xsphere/commands/valve/{vessel}/{action}` | Dashboard→Python | Valve control |
| `xsphere/commands/gradient_scanner/{cmd}` | Dashboard→Python | Scan start/stop |

## Key contacts / resources

- SYSTEM_ARCHITECTURE.md — full hardware inventory, register map, wiring details
- SETUP.md — first-time installation
- OPERATIONS.md — routine operations and troubleshooting
- VERIFICATION_CHECKLIST.md — pre-deployment hardware verification
