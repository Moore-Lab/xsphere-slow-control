# xsphere Slow Control

Slow control system for the xsphere cryostat experiment at Yale University.
Monitors and controls temperatures, LN2 fill levels, and gas handling for a
xenon cryostat used in levitated-particle physics experiments.

## What this system does

- **Reads temperatures** from the CLICK PLC (RTDs on the cryostat zones) and
  the Omega RDXL6SD-USB data logger (clamp RTDs, thermocouples)
- **Controls heaters** via three PID zones (top / bottom / nozzle) on the PLC,
  with a Python gradient abstraction layer (gradient mode or per-zone absolute)
- **Manages LN2 autofill** for the ballast and primary xenon dewars via solenoid
  valves, with configurable level thresholds and fill-timeout safety
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
├── firmware/                   ← git submodules (run: git submodule update --init)
│   ├── gas-handling-system/    ← Moore-Lab/gas-handling-system
│   │   └── Software/Xenon Gas Handling System Sensor Suite/   (ESP32, branch slowcontrol-v2)
│   └── liquid-level-sensor/    ← Moore-Lab/liquid-level-sensor
│       └── Software/FDC1004 Level Sensor/   (ESP32, branch slowcontrol-v2; per-vessel envs)
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

All sensor/status payloads are JSON.  Full schema and payload shapes:
`SYSTEM_ARCHITECTURE.md` §6.5 (must match `telegraf/telegraf.conf`).

| Topic | Direction | Payload |
|---|---|---|
| `xsphere/sensors/temperature/{plc\|omega}/{rtd\|tc}/{ch}` | PLC / Omega→broker | `{"value_k","value_c"}` |
| `xsphere/sensors/pressure/ghs/setra/{1,2}` | GHS ESP32→broker | `{"value"}` (mbar) |
| `xsphere/sensors/vacuum/ghs/{1,2}` | GHS ESP32→broker | `{"value"}` (mbar) |
| `xsphere/sensors/environment/ghs/{temperature\|humidity\|baro_pressure}` | GHS ESP32→broker | `{"value"}` |
| `xsphere/sensors/level/{vessel}` | FDC1004 ESP32→broker | `{"raw","filtered"}` (pF) |
| `xsphere/status/pid/{zone}` | PLC driver→broker | `{"setpoint_k","pv_k","output_pct","kp","ki","kd"}` (retained) |
| `xsphere/status/valve/{vessel}` | Python→broker | `{"state","desired","auto_open","auto_close"}` (retained) |
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
