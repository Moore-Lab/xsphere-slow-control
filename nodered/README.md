# Node-RED flows

The Pi runs Node-RED inside the IOTstack (`http://<pi>:1880`, dashboard at
`http://<pi>:1880/ui`). It needs the `node-red-dashboard` package
(`docker exec -w /data nodered npm install node-red-dashboard && docker restart nodered`).

## Files here

- **`build_control_flow.py`** — generator for the **"xsphere Control"** dashboard
  flow (groups: System status, Valves, Heater PID per zone, Gradient, Follow a
  sensor, Ramp sequencer, Gradient scan). Run it:
  - `python nodered/build_control_flow.py` → writes `control-flows.json`
  - `python nodered/build_control_flow.py --deploy` → also merges it into the
    running Node-RED (idempotent: it removes any previously-deployed `xsc_*`/
    `xsphere_*` nodes first; nothing else is touched). It reuses the existing
    `ui_base` config node and creates its own `mqtt-broker` (`mosquitto:1883`).
- **`control-flows.json`** — the generated flow (the artifact `--deploy` pushes).
- **`flows-running-backup.json`** — snapshot of the *old/existing* Node-RED flows
  (8 tabs incl. `Valve UI`, `Heater UI`, `Cryostat (...)`, `Nanosphere ADC`, …)
  kept as a backup/reference. These are *not* what `control-flows.json` builds;
  edit them in the Node-RED editor if you want to change them.
- **`dashboard-flows.json`** — an early single-tab quick-start stub (superseded by
  the generator above).

> The old flows still contain `influxdb out` nodes that write directly to
> InfluxDB — that double-writes alongside Telegraf, so disable those output
> nodes (and the dead `Cryostat (Omega RDXL6S)` / `Nanosphere ADC` tabs).

(The CLICK PLUS PLC also runs its own embedded Node-RED — those flows are in
`../plc_nodered.json` (`CLICK Read`/`CLICK Write` nodes); kept for reference.)
