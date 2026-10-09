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

- [ ] **IMPORTANT** Gas-side solenoid valves (ballast / pump / bottle). These
  are the first coil reads (Modbus FC1) the driver makes, so the Y address
  mapping is exercised here for the first time:
  1. In CLICK Address Picker with "Display MODBUS Address" ticked, confirm
     Y105 / Y106 / Y107 are coils 8229 / 8230 / 8231 (1-based; the driver
     uses 8228 / 8229 / 8230) and DS151–DS153 are holding registers
     400151–400153.
  2. With each valve isolated or otherwise safe to stroke, press Open then
     Close on the GUI Cryostat tab and confirm the right relay LED follows
     and the row reads `OPEN   (desired 1)` then `closed   (desired 0)`.
  3. A row stuck on `?   (desired N)` means the DS register reads but the
     coil does not — recheck the Y address before trusting the readback.
  4. Confirm nothing else in the ladder writes Y105–Y107. They were once
     reserved for the MKS valve override decode (ladder addition 2); that
     decode must not be entered against these outputs.

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
  A coast refill starts from a dry vessel and is a much longer transfer than
  the top-up 920 s was sized for — see section 3b before enabling coast.

- [ ] **IMPORTANT** Confirm the level sensor FDC1004 channel assignments in
  `firmware/level-sensor/platformio.ini`:
  - `FDC1004_CHANNEL=FDC1004_CHANNEL_0` for both ballast and primary_xe.
  If the probes are wired to different FDC1004 channels, update accordingly.

- [ ] **IMPORTANT** Calibrate `CAPDAC_OFFSET_PF` in `firmware/level-sensor/src/main.cpp`
  for each vessel. With the dewar empty (or at a known reference level),
  note the raw_pf value and set CAPDAC_OFFSET_PF to that value so the
  sensor reads ~0 at empty.

---

## 3b. Coast Refill — Cryostat Only

Coast lets the cryostat boil dry and keeps running on the thermal mass of its
cold block, refilling only once BOTH terms hold: the level is below
`empty_threshold`, and the configured sensor pair has converged
(`T_warm - T_cold` < `delta_max_k`, i.e. the cold reserve is spent). It gates
`auto_open` only — it can never inhibit `auto_close`, the fill timeout, or a
manual valve command, and every input it cannot evaluate releases the inhibit
and reverts to today's `level_low` = 0.25 behaviour.

> **Nothing in this section is exercised by default.** `coast.enabled` and
> `coast.ladder_rung_entered` are both `false` in `slowcontrol/config.yaml`,
> and coast additionally has to be armed over MQTT after every service start.
> Work the three CRITICAL items below to completion before arming it for the
> first time.

- [ ] **CRITICAL** Hand-enter CLICK ladder addition 6 (XV3 coast-aware
  auto-open, see `SYSTEM_ARCHITECTURE.md`) and pass the five-step permit test
  below. The `.ckp` project is a password-encrypted blob, so this rung cannot
  be scripted, diffed, or reviewed offline — it is typed in once and verified
  by observation. Until it exists, the ladder's own auto-open window
  (0.25 < DF303 < 0.8) tops the vessel up during the descent, coast never sees
  an empty vessel, and the feature silently does nothing.

  New register DS1107, XV3 coast permit. Integer, write, RETENTIVE, initial
  value 1. 0 = the Python service is deliberately withholding a refill; any
  other value, including a never-written PLC, means permit granted, so a
  project without this rung behaves exactly as it does today.

  **Before you start**: the service re-asserts DS1107 once per control tick
  (10 s), so a value forced from CLICK will not stick while the autovalve
  controller is running. Run steps 1, 2, 4 and 5 with the service started but
  `autovalve.enabled: false` in `config.yaml` — the PLC driver still polls
  and still kicks the DS1099 watchdog, but nothing writes DS1107 and nothing
  commands XV3. Confirm DS1099 is advancing first (see the watchdog item
  below): if it has stalled, `wd_stale` is true, ladder branch B grants every
  auto-open in the trusted window, and the result looks identical to a missing
  permit rung.

  1. Dewar at mid level (0.25 < DF303 < 0.8), DS1106 armed, DS1006 = 0, force
     DS1107 = 0 from CLICK. XV3 must NOT open (Y103 off, X003 de-energised).
     This single step is the whole feature: if XV3 opens here, coast can never
     withhold a fill.
  2. Set DS1107 = 1. XV3 must open. The permit can only ever subtract from
     what the ladder would otherwise do inside its trusted window.
  3. Branch B — Python dead, level still trusted. With DS1107 = 0 and the
     dewar still at mid level, kill the service uncleanly (`kill -9`, or pull
     its network cable). XV3 must open within ~10 s of DS1099 stopping. Do
     NOT use `systemctl stop`: a clean shutdown writes DS1107 = 1 directly
     over Modbus from `PlcDriver._release_coast_permits()`, so XV3 opens via
     branch A and branch B stays untested. DS1107 is retentive, so without
     branch B a crashed service that left DS1107 = 0 would inhibit XV3
     forever.
  4. Branch C — independent dry evidence, any level. With DS1107 = 0 and
     DF303 below 0.25, bring DF4 above -138.0 °C (135 K). XV3 must open. Both
     registers are rewritten every scan (DF303 by the ladder filter rung, DF4
     by the RTD module), so a data-view write will not hold either of them:
     get DF303 down by actually draining the vessel — fold this step into the
     supervised boil-dry below — and DF4 up with a heat gun on the LN2 base
     RTD, or by substituting a resistance box for it (Pt1000 is about 447 Ω at
     -138 °C per IEC 60751). Branch C is what restores the backup fill layer
     that coast would otherwise delete: it drops the 0.25 floor, but demands
     DF4 evidence from a different sensor on a different module in different
     units, so two independent sensors must agree before XV3 opens below the
     trusted window.
  5. Refill to DF303 > 2.5 with DS1107 still 0 and confirm auto-close still
     fires and timer T3 still behaves. DS1107 must never appear in the close
     rung. Coast also never writes DS1105 or DS1106 — those stay purely
     operator-controlled.

  Set `coast.ladder_rung_entered: true` only after all five steps pass. Arming
  with it `false` is allowed and logs a loud warning; that failure wastes LN2
  rather than endangering anything, but it looks exactly like a broken
  feature.

- [ ] **CRITICAL** Run one supervised boil-dry with coast DISARMED, logging
  DF1, DF2, DF3, DF4 and the cryostat level channel throughout, before coast
  is ever armed. Three values currently in `config.yaml` are estimates, not
  measurements, and this is the only way they acquire defensible ones:
  1. `empty_threshold: 0.13` — no defensible default exists; nothing in the
     repo maps the probe's pF reading to litres. Set it to the measured dry
     reading + 0.05.
  2. `delta_max_k: 40.0` — rests on an estimated thermal time constant, not a
     measured one. Take it from the real (DF2 - DF4) trace: with liquid
     present the difference is about 88 K and it shrinks as the block dries,
     so pick the value where the curve has flattened while DF1–DF3 are still
     well below `ceiling_k`.
  3. `max_duration_s: 5400.0` — a sanity bound, not a tuned value. Set it
     from the measured dry-to-ceiling time, with margin.

  Watch DF1–DF3 against `ceiling_k` = 175 K throughout (the Xe triple point
  is 161.4 K) and abort by hand if the cube warms faster than expected.

- [ ] **CRITICAL** Confirm the configured pair is the intended physical pair
  before arming. `sensor_warm: plc/rtd/2` must be the Xe cube bottom (DF2, the
  heated load) and `sensor_cold: plc/rtd/4` the LN2 vessel base (DF4, the cold
  sink). Warm one channel with a heat gun and watch which value actually
  moves:
  ```
  mosquitto_sub -h localhost -t 'xsphere/status/coast/cryostat'
  ```
  `t_warm_k` must follow the channel you heated. Do NOT pair `plc/rtd/1` with
  `plc/rtd/2`: that difference IS the commanded vertical gradient
  (`gradient.delta_v_k`), so the gate would sit near 0 K and coast would
  silently never engage. A reversed pair is caught at runtime (the gate
  releases and raises `coast_unverified`), but a plausible-looking wrong pair
  is not caught by anything.

- [ ] **IMPORTANT** Confirm DS1099 is advancing before trusting the permit at
  all. The driver only kicks the watchdog when the MKS flow read succeeds
  (`gasflow.enabled` and `gasflow.mks.enabled`, and the registers actually
  read), so on a rack with no MFC wired the counter stalls permanently,
  `wd_stale` is permanently true, and ladder branch B auto-opens XV3 through
  the whole 0.25 < DF303 < 0.8 window regardless of DS1107. Coast then saves
  nothing — the same silent no-op as a missing rung.

- [ ] **IMPORTANT** Verify the fail-open path. Mid-coast, pull one lead of the
  configured pair and confirm the inhibit RELEASES: `open_threshold` on
  `xsphere/status/coast/cryostat` returns to `level_low` = 0.25, `state`
  becomes `released`, and `xsphere/alerts/coast_unverified/cryostat` fires.
  Note which mechanism catches it — an open lead that reads over-range is
  rejected on the next tick by the plausibility band (`temp_min_k` 50 K,
  `temp_max_k` 400 K), while a channel that stops updating altogether takes
  `temp_stale_s` = 30 s. Record what DF4 actually does with an open lead: the
  freshness test is MQTT arrival time, not value change, so an RTD module that
  HOLDS its last good reading on a broken lead passes both tests and the gate
  keeps coasting on a frozen number.

- [ ] **IMPORTANT** Confirm `fill_timeout_s` and ladder timer T3 agree. A
  from-dry coast fill is a much longer transfer than the top-up 920 s was
  sized for. If T3 is raised to 1800 s, raise
  `autovalve.vessels.cryostat.fill_timeout_s` to 1800 in the SAME visit — the
  two must match, or one of them forces a close part-way through a normal
  coast refill.

- [ ] **IMPORTANT** Calibrate `empty_threshold` against the number the gate
  actually compares, which is not DF303. The driver publishes DF203 as `raw`
  and DF303 as `filtered` on `xsphere/sensors/level/cryostat`; the autovalve
  controller takes `raw` only and applies its own α = 0.01 filter,
  republishing the result on `xsphere/status/level/cryostat`. So the ladder
  rungs compare DF303 while the Python gate compares the `filtered` field of:
  ```
  mosquitto_sub -h localhost -t 'xsphere/status/level/cryostat'
  ```
  Confirm the two track each other at empty and at full, and take the
  calibration number from the status topic. At the 1 s PLC poll interval the
  software filter has a time constant of about 100 s, so the gate sees "empty"
  a minute or more after the vessel is.

- [ ] **IMPORTANT** Exercise the three backstops and the latch. Each of
  `max_duration_s` (5400 s), `ceiling_k` (175 K on ANY of DF1–DF3, not just
  the configured pair) and `sink_warm_k` (135 K on the cold sensor) must force
  a refill on the first tick it is true — the `confirm_s` dwell only ever
  delays a fill, never a backstop — publish
  `xsphere/alerts/coast_backstop/cryostat`, and latch coast off. Confirm the
  latch clears only once the vessel is proven wet again: level at or above
  `level_high` = 2.5 AND the cold sensor below `sink_cold_k` = 90 K, two
  independent sensors agreeing. `sink_warm_k` is the easiest to provoke during
  the boil-dry; `max_duration_s` needs a temporarily shortened value in
  `config.yaml` plus a restart, because the backstops are deliberately not
  runtime-tunable.

- [ ] **INFO** Confirm the DS1107 write is landing, not just being sent. On
  `xsphere/status/coast/cryostat`, `ladder_permit` is the value the controller
  last commanded and `ladder_permit_readback` is what the PLC driver read back
  out of DS1107. They must agree within one control tick; a persistent
  mismatch means the Modbus address is wrong or something else owns the
  register.

- [ ] **INFO** Coast comes up DISARMED after every service start, restart and
  crash. There is deliberately no arm-on-start key — arming is an explicit
  operator act:
  ```
  mosquitto_pub -h localhost -t 'xsphere/commands/valve/cryostat/coast' \
    -m '{"enabled": true}'
  ```
  Never publish that topic retained; a retained arm would defeat the property.

  > **Re-arm coast before re-arming auto_open.** After a restart, re-arming
  > `auto_open` while coast is still disarmed restores the `level_low` = 0.25
  > threshold with the vessel sitting below `empty_threshold`, so it fills
  > immediately. That is fail-safe — a fill, never a strand — but it
  > silently ends the coast, and the only evidence is the fill itself.

- [ ] **INFO** Confirm runtime retuning is limited as intended.
  `xsphere/commands/valve/cryostat/coast_config` accepts only
  `empty_threshold`, `sensor_warm`, `sensor_cold`, `delta_max_k`,
  `delta_mode`, `delta_hysteresis_k` and `confirm_s`, validated by the same
  predicates as the file loader, applied all-or-nothing, and never
  persisted — `config.yaml` wins after a restart. Every backstop is
  config-file only. Send one deliberately bad command (e.g.
  `{"max_duration_s": 60}`) and confirm it is refused on
  `xsphere/alerts/coast_refused/cryostat` and changes nothing.

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
11. Confirm `xsphere/status/coast/cryostat` is being published and reads
    `state: disarmed` — coast must never come up armed after a restart:
    ```
    mosquitto_sub -h localhost -t 'xsphere/status/coast/cryostat'
    ```

---

*Last updated: 2026-09-09*
