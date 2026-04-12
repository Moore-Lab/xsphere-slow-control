# xsphere Slow Control — Verification Checklist

This is a living document. Work through it top to bottom before declaring
the new system operational. Items marked **CRITICAL** can cause hardware
damage or data loss if wrong. Items marked **IMPORTANT** will cause
incorrect behavior. Items marked **INFO** should be confirmed but are
lower risk.

---

## 1. Network / Infrastructure

- [ ] **INFO** Confirm xbox-pi IP is `192.168.8.116`. Update all config files if different:
  - `slowcontrol/config.yaml` → `mqtt.host`
  - `omega-logger/config.yaml` → `mqtt_host`
  - `firmware/ghs-esp32/src/main.cpp` → `MQTT_BROKER`
  - `firmware/level-sensor/src/main.cpp` → `MQTT_BROKER`
  - `nodered/dashboard-flows.json` → broker node

- [ ] **INFO** Confirm Mosquitto is running on xbox-pi port 1883:
  ```
  mosquitto_pub -h 192.168.8.116 -t test/ping -m hello
  mosquitto_sub -h 192.168.8.116 -t test/ping
  ```

- [ ] **INFO** Confirm WiFi SSID is `xbox-radio` and password matches in all ESP32 firmware.

---

## 2. PLC Modbus TCP — CRITICAL

- [ ] **CRITICAL** Confirm PLC IP address. Update `slowcontrol/config.yaml` → `plc.host`.
  Current placeholder: `192.168.8.xxx`.
  Find it: check Node-RED existing connection, or PLC front panel display, or
  router DHCP table (look for CLICK PLC MAC prefix).

- [ ] **CRITICAL** Verify Modbus base addresses in `slowcontrol/drivers/plc.py`.
  The following constants were derived from the CLICK PLC C2-USERM manual
  and must be confirmed against the actual PLC project file:
  - `DS_BASE = 0`     — first DS (data store, 16-bit integer) register
  - `DF_BASE = 28672` — first DF (data float, 32-bit) register
  - `Y_BASE  = 8192`  — first Y (discrete output coil) address
  - `X_BASE  = 0`     — first X (discrete input) address
  - `C_BASE  = 0`     — first C (control relay) coil address

  **How to verify**: Open the PLC project in CLICK Programming Software,
  go to the Modbus/TCP address map, and cross-reference each symbolic address
  (e.g., DF1, DS1001, Y1) against its numeric Modbus register number.

- [ ] **CRITICAL** Verify each register in the `REG_RTD`, `REG_LEVEL_RAW`,
  `REG_LEVEL_FILTERED`, `REG_VALVE`, `REG_VALVE_COIL`, `_PID_BLOCKS` dictionaries
  in `plc.py` maps to the correct PLC variable. Spot-check by:
  1. Starting the Python service with verbose logging (`-v`)
  2. Comparing published MQTT values against PLC programmer display readings
  3. Sending a test valve command and confirming the correct PLC Y output changes

- [ ] **CRITICAL** Confirm float byte order (endianness) for 32-bit registers.
  CLICK PLC stores 32-bit floats as big-endian word pairs by default.
  In `plc.py`, `_read_float` uses `>f` format. If readings are garbage,
  try `<f` (little-endian) or swapped word order.
  Test: read DF1 (PLC display shows, e.g., 165.0) → Python must read 165.0.

- [ ] **IMPORTANT** Confirm PLC RTD channel assignments:
  - DF1/DF3/DF5 = RTD-A/B/C (top/bottom/nozzle zones) — verify wiring
  - PLC vs Omega RTD channels: which RTDs are on each device?
    Record the mapping in `slowcontrol/config.yaml` comments.

- [ ] **IMPORTANT** Confirm PID register block layout for all three zones
  (top, bottom, nozzle) — specifically that `_PID_BLOCKS` and `_PID_OFF`
  offsets produce the correct setpoint/gain/output registers for each zone.
  Test: write a setpoint via Python, confirm PLC display updates.

---

## 3. Autovalve / Level Sensors

- [ ] **CRITICAL** The Python autovalve controller compares `level_filtered`
  against thresholds in `config.yaml`. These thresholds were copied from the
  PLC ladder (cryostat high=2.5/low=0.25, primary_xe/ballast high=2.5/low=0.5).
  The old system used ADS1115 voltage values (0–10 V scale).
  **The new level sensor ESP32 publishes FDC1004 raw capacitance in pF,
  which has different units.**
  You MUST recalibrate `level_high` and `level_low` in `config.yaml` to
  match the pF readings from your specific probe geometry before enabling
  autofill.

- [ ] **CRITICAL** Before enabling autovalve (`auto_open_en` / `auto_close_en`):
  1. Run the system with autovalve disabled.
  2. Observe the `xsphere/sensors/level/{vessel}` MQTT values at known
     empty and full states (manually fill and drain the dewar).
  3. Set `level_low` ≈ empty + 20% margin and `level_high` ≈ full − 10% margin.
  4. Update `slowcontrol/config.yaml` accordingly.

- [ ] **IMPORTANT** The cryostat level sensor is read directly by the PLC ADC
  (not an ESP32). The Python service reads this from a PLC DF register.
  Confirm which PLC register holds the cryostat level reading and that
  `REG_LEVEL_RAW["cryostat"]` in `plc.py` maps to it correctly.

- [ ] **IMPORTANT** Confirm fill timeout values make sense for actual fill rates:
  - ballast / primary_xe: `fill_timeout_s = 600` (10 min) — is this long enough?
  - cryostat: `fill_timeout_s = 920` (15 min) — is this long enough?
  If a fill timeout fires during normal operation, increase the value.

- [ ] **IMPORTANT** Confirm the level sensor FDC1004 channel assignments in
  `firmware/level-sensor/platformio.ini`:
  - `FDC1004_CHANNEL=FDC1004_CHANNEL_0` for both ballast and primary_xe.
  If the probes are wired to different FDC1004 channels, update accordingly.

- [ ] **IMPORTANT** Calibrate `CAPDAC_OFFSET_PF` in `firmware/level-sensor/src/main.cpp`
  for each vessel. With the dewar empty (or at a known reference level),
  note the raw_pf value and set CAPDAC_OFFSET_PF to that value so the
  sensor reads ~0 at empty.

---

## 4. Omega RDXL6SD-USB Logger

- [ ] **IMPORTANT** Identify the serial port for the Omega device:
  ```bash
  ls /dev/ttyUSB*   # before and after plugging in
  ```
  Update `omega-logger/config.yaml` → `serial_port`.

- [ ] **IMPORTANT** Verify Modbus address. The default is 1 but may have been
  changed via the device's Modbus address setting. Update `config.yaml` → `modbus_address`.

- [ ] **IMPORTANT** Verify register map. The logger assumes holding registers
  starting at address 0, one per channel, in 0.1 °C integer format.
  Confirm by reading register 0 and comparing with the display reading.
  If wrong, check the RDXL6SD-USB user manual for the correct Modbus
  register map and update `omega_logger.py` → `reg_base` and the
  register read logic.

- [ ] **INFO** Verify channel-to-sensor mapping. Update the `CHANNEL_LABELS`
  dict in `omega_logger.py` and the comments in `config.yaml` to record
  which physical sensor is connected to each channel (ch1–ch6).

- [ ] **INFO** Confirm user `xbox` is in the `dialout` group:
  ```bash
  groups xbox   # should include 'dialout'
  sudo usermod -aG dialout xbox   # if not
  ```

---

## 5. GHS ESP32 Firmware

- [ ] **IMPORTANT** Verify voltage divider constant `FEG = 180.1 / 36.0 = 5.003` in
  `firmware/ghs-esp32/src/main.cpp` matches the actual resistor values on
  the GHS board. Measure the actual resistors or check the schematic.
  Test: apply a known voltage (e.g., 5.000 V) to an ADC input with no sensor
  connected, confirm the published `voltage_v` reads 5.000.

- [ ] **IMPORTANT** Verify pressure conversion coefficients:
  - `PSI_PER_VOLT_MAIN = 2.5`  (10 V → 25 PSI): confirm gauge range
  - `PSI_PER_VOLT_HIGH = 10.0` (10 V → 100 PSI): confirm gauge range
  Cross-check against the pressure gauge data sheets.

- [ ] **IMPORTANT** Verify vacuum gauge formula `p = 10^(1.667 × V − 11.33)`.
  This is correct for the Pfeiffer PKR-251 Pirani gauge and similar.
  Check the data sheet for your specific gauge model. Update `VAC_A` and
  `VAC_B` constants if different.

- [ ] **IMPORTANT** Confirm physical channel assignments:
  - ADC1 CH0 → which pressure gauge?
  - ADC1 CH1 → which pressure gauge?
  - ADC1 CH2 → which vacuum gauge?
  - ADC1 CH3 → which vacuum gauge?
  Update the MQTT topic names in `main.cpp` and Telegraf config if needed
  (e.g., `xsphere/sensors/vacuum/xe_cube` → `xsphere/sensors/vacuum/pump_line`).

- [ ] **INFO** Confirm DHT11 pin assignment (`DHT_PIN = 4`).

---

## 6. Telegraf Configuration

- [ ] **IMPORTANT** Set environment variables before starting Telegraf.
  Copy `telegraf/.env.example` to `telegraf/.env` and fill in real values:
  - `INFLUX_URL` — InfluxDB URL (usually `http://localhost:8086`)
  - `INFLUX_TOKEN` — InfluxDB API token (create one in InfluxDB UI)
  - `INFLUX_ORG` — InfluxDB organization name
  - `INFLUX_BUCKET` — target bucket name
  - `MQTT_HOST` — `localhost` or `192.168.8.116`
  - `MQTT_PORT` — `1883`

- [ ] **IMPORTANT** Verify the Telegraf topic patterns in `telegraf/telegraf.conf`
  match the actual MQTT topics published by all sources. Subscribe to `#` on
  the broker and compare with the patterns in the config.

- [ ] **INFO** Confirm Telegraf is in the `dialout` group if it also reads
  the Omega serial port directly (not currently the case — Omega goes via
  the Python logger, but verify the intended data path).

---

## 7. Node-RED Dashboard

- [ ] **IMPORTANT** Install the `node-red-dashboard` package on xbox-pi:
  ```bash
  cd ~/.node-red
  npm install node-red-dashboard
  # Restart Node-RED
  ```

- [ ] **IMPORTANT** Import `nodered/dashboard-flows.json` into Node-RED:
  Hamburger menu → Import → select file. Then deploy.

- [ ] **INFO** Verify the MQTT broker node in Node-RED points to `localhost:1883`
  (if Node-RED runs on xbox-pi) or `192.168.8.116:1883` (if remote).

- [ ] **INFO** The dashboard uses `ui_text` nodes to display all sensor values.
  If multiple sensors share the same group (e.g., 6 PLC temperature channels),
  only the last message is shown per node. You may want to duplicate the
  `ui_text` node for each channel and wire each to a separate filtered
  function node. The current flow uses a single text node per group as a
  quick-start — expand as needed.

---

## 8. Python Slow Control Service

- [ ] **IMPORTANT** Install the service on xbox-pi:
  ```bash
  cd /home/xbox/xsphere-slow-control/slowcontrol
  pip install -r requirements.txt
  sudo cp xsphere-slowcontrol.service /etc/systemd/system/
  sudo systemctl daemon-reload
  sudo systemctl enable xsphere-slowcontrol
  ```

- [ ] **IMPORTANT** Run once manually before enabling as a service to catch
  config errors:
  ```bash
  python -m slowcontrol.app -c config.yaml -v
  ```
  Watch for Modbus connection errors, MQTT errors, and any exceptions.

- [ ] **INFO** The service starts with `GradientController` in gradient mode
  at `base_k = 165.0 K`, `delta_v_k = 0.0`, `delta_l_k = 0.0`. Confirm
  these defaults are safe before first start.

---

## 9. Parallel Operation (Old → New System Transition)

- [ ] **INFO** The old Node-RED based data pipeline can run in parallel while
  the new system is being commissioned. To avoid double-writes to InfluxDB,
  either disable the old Node-RED InfluxDB output nodes, or write to a
  separate test bucket in the new Telegraf config.

- [ ] **INFO** Before decommissioning the old system, confirm:
  1. All sensor data flows through the new pipeline into InfluxDB
  2. The autovalve state machines behave correctly on both the PLC (hardware
     backup) and Python (primary) for at least one complete fill cycle
  3. Interlock alerts fire correctly for a simulated condition (e.g.,
     temporarily disconnect a temperature sensor to trigger a stale alert)

- [ ] **INFO** Remove the old Node-RED flows that write to `DF251` / `DF252`
  (level values for the PLC ladder logic) only after confirming the new
  Python `PlcDriver` is writing them correctly. If both write simultaneously,
  the last writer wins — confirm there is no race condition during transition.

---

## 10. First Run Smoke Test

After completing hardware setup, run through this sequence:

1. Start Mosquitto (already running as Docker service on xbox-pi)
2. Start slow control service: `sudo systemctl start xsphere-slowcontrol`
3. Start omega logger: `sudo systemctl start xsphere-omega-logger`
4. Start Telegraf: confirm it connects and data appears in InfluxDB
5. Open Node-RED dashboard — confirm all sensors show live data
6. Confirm heartbeat topic is updating:
   ```
   mosquitto_sub -h localhost -t 'xsphere/status/service/heartbeat'
   ```
7. Confirm PLC temperature readings match PLC programmer display values
8. Confirm gradient mode: set base_k = 165.0 K via dashboard, verify PLC
   setpoint registers update
9. Confirm interlock status topic shows `ok: true`
10. Manually trigger one alert (disconnect a sensor or set a threshold
    temporarily narrow) and confirm the alert appears on the dashboard

---

*Last updated: 2026-04-12*
