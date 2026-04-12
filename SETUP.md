# xsphere Slow Control — Setup Guide

Follow this guide to install the new slow control system on a fresh xbox-pi.
The old Node-RED pipeline can remain running in parallel until you are satisfied
with the new system (see Step 8).

---

## Prerequisites

The xbox-pi should already be running [IOTstack](https://sensorsiot.github.io/IOTstack/)
with the following Docker services active:

- **Mosquitto** (MQTT broker) on port 1883
- **InfluxDB 2.x** on port 8086
- **Node-RED** on port 1880
- **Grafana** on port 3000 (optional at first)

Confirm they are running:
```bash
cd ~/IOTstack
docker compose ps
```

You also need:
- **Miniconda** installed at `/home/xbox/miniconda3` (or adjust paths in
  the systemd service files)
- **PlatformIO** on your development machine (for flashing ESP32 firmware)

---

## Step 1 — Clone / copy the repository onto xbox-pi

```bash
# From your development machine:
scp -r /path/to/xsphere-slow-control xbox@192.168.8.116:/home/xbox/

# Or on xbox-pi directly if the repo is on a shared drive / USB:
cp -r /media/usb/xsphere-slow-control /home/xbox/
```

The rest of this guide assumes the repo lives at:
```
/home/xbox/xsphere-slow-control/
```

---

## Step 2 — Python slow control service

### 2a. Create a conda environment

```bash
conda create -n slowcontrol python=3.11 -y
conda activate slowcontrol
cd /home/xbox/xsphere-slow-control/slowcontrol
pip install -r requirements.txt
```

### 2b. Edit the configuration

```bash
nano /home/xbox/xsphere-slow-control/slowcontrol/config.yaml
```

**Must change before first run:**
- `plc.host` — set to the PLC's actual IP address (check router DHCP table
  or PLC front panel; it is on the `192.168.8.x` subnet)

Everything else can be left as default for first boot.

### 2c. Test-run manually

```bash
conda activate slowcontrol
cd /home/xbox/xsphere-slow-control/slowcontrol
python -m slowcontrol.app -c config.yaml -v
```

Watch for:
- `[plc] connected` — Modbus TCP connected to PLC
- `[mqtt] connected` — MQTT broker connected
- `[gradient] started` — gradient controller up
- `[autovalve] started` — autovalve controller up
- `[interlocks] started` — watchdog up

If the PLC IP is wrong you will see a Modbus connection error — fix `config.yaml`
and retry. Press Ctrl-C to stop.

### 2d. Install as a systemd service

```bash
sudo cp /home/xbox/xsphere-slow-control/slowcontrol/xsphere-slowcontrol.service \
        /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable xsphere-slowcontrol
sudo systemctl start xsphere-slowcontrol
```

Check status:
```bash
sudo systemctl status xsphere-slowcontrol
journalctl -u xsphere-slowcontrol -f
```

---

## Step 3 — Omega RDXL6SD-USB logger

### 3a. Add the `xbox` user to the `dialout` group

```bash
sudo usermod -aG dialout xbox
# Log out and back in for the change to take effect
```

### 3b. Identify the serial port

Plug the Omega USB cable into xbox-pi and run:
```bash
ls /dev/ttyUSB*
```
before and after plugging in. The new device is the Omega.  Common result:
`/dev/ttyUSB0`.

### 3c. Edit the configuration

```bash
nano /home/xbox/xsphere-slow-control/omega-logger/config.yaml
```

Set `serial_port` to the device identified above.

### 3d. Test-run manually

```bash
conda activate slowcontrol
cd /home/xbox/xsphere-slow-control/omega-logger
python omega_logger.py -c config.yaml -v
```

You should see 6 channel readings logged every 5 seconds. If you see Modbus
errors, check the baud rate and device address settings in config.yaml (see
VERIFICATION_CHECKLIST.md §4 for details).

### 3e. Install as a systemd service

```bash
sudo cp /home/xbox/xsphere-slow-control/omega-logger/xsphere-omega-logger.service \
        /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable xsphere-omega-logger
sudo systemctl start xsphere-omega-logger
```

---

## Step 4 — Telegraf (MQTT → InfluxDB pipeline)

### 4a. Install Telegraf on xbox-pi

If Telegraf is not already installed:
```bash
# Add InfluxData repo
curl -s https://repos.influxdata.com/influxdata.key | sudo apt-key add -
echo "deb https://repos.influxdata.com/debian stable main" \
  | sudo tee /etc/apt/sources.list.d/influxdata.list
sudo apt update && sudo apt install telegraf -y
```

### 4b. Create InfluxDB credentials

1. Open InfluxDB UI: `http://192.168.8.116:8086`
2. Create an organisation (e.g., `xsphere`) and a bucket (e.g., `xsphere`)
   if they do not already exist.
3. Go to **Data → API Tokens → Generate API Token → All Access Token**.
   Copy the token.

### 4c. Configure environment variables

```bash
cp /home/xbox/xsphere-slow-control/telegraf/.env.example \
   /home/xbox/xsphere-slow-control/telegraf/.env
nano /home/xbox/xsphere-slow-control/telegraf/.env
```

Fill in:
```
INFLUX_URL=http://192.168.8.116:8086
INFLUX_TOKEN=<paste token here>
INFLUX_ORG=xsphere
INFLUX_BUCKET=xsphere
MQTT_HOST=localhost
MQTT_PORT=1883
```

### 4d. Install the Telegraf configuration

```bash
# Make Telegraf load the .env file automatically
sudo mkdir -p /etc/telegraf
sudo cp /home/xbox/xsphere-slow-control/telegraf/telegraf.conf \
        /etc/telegraf/telegraf.conf

# Pass environment variables to the service
sudo mkdir -p /etc/systemd/system/telegraf.service.d
sudo tee /etc/systemd/system/telegraf.service.d/env.conf <<'EOF'
[Service]
EnvironmentFile=/home/xbox/xsphere-slow-control/telegraf/.env
EOF

sudo systemctl daemon-reload
sudo systemctl restart telegraf
sudo systemctl status telegraf
```

### 4e. Verify data is flowing

```bash
# Subscribe to any sensor topic and confirm messages arrive
mosquitto_sub -h localhost -t 'xsphere/sensors/#' -v

# In InfluxDB UI: Data Explorer → select bucket 'xsphere'
# → measurement 'temperature' → should have fields within ~60 seconds
```

---

## Step 5 — Flash ESP32 firmware

Install PlatformIO on your **development machine** (not xbox-pi):
```bash
pip install platformio
```

### 5a. GHS ESP32 (pressure / vacuum / environment)

Before flashing, edit `firmware/ghs-esp32/src/main.cpp` and verify:
- `WIFI_SSID` / `WIFI_PASSWORD`
- `MQTT_BROKER` IP

```bash
cd firmware/ghs-esp32
pio run -t upload       # connects via USB to the GHS ESP32
```

Confirm data arrives:
```bash
mosquitto_sub -h 192.168.8.116 -t 'xsphere/sensors/pressure/#' -v
```

### 5b. Level sensor ESP32s

There are two level sensor boards (ballast and primary_xe).
Flash each one separately:

```bash
cd firmware/level-sensor

# Flash the ballast board (connect it via USB)
pio run -e ballast -t upload

# Swap USB to the primary_xe board
pio run -e primary_xe -t upload
```

Confirm data:
```bash
mosquitto_sub -h 192.168.8.116 -t 'xsphere/sensors/level/#' -v
```

> **Before enabling autofill**, read the level calibration section in
> VERIFICATION_CHECKLIST.md §3. The raw pF values need to be mapped to
> meaningful thresholds before the autovalve will behave correctly.

---

## Step 6 — Node-RED dashboard

### 6a. Install the dashboard package

SSH into xbox-pi:
```bash
cd ~/.node-red
npm install node-red-dashboard
# Restart Node-RED
docker restart nodered   # if running in Docker
```

### 6b. Import the flows

1. Open Node-RED: `http://192.168.8.116:1880`
2. Hamburger menu (top right) → **Import**
3. Select **Upload file** → choose `nodered/dashboard-flows.json`
4. Click **Import**, then **Deploy** (red button, top right)

### 6c. Open the dashboard

`http://192.168.8.116:1880/ui`

You should see six tabs: Temperatures, PID/Heaters, Level/Valves, Gas Handling,
Interlocks, and Gradient Scan.

---

## Step 7 — Verify the full system

Work through **VERIFICATION_CHECKLIST.md** top to bottom.

The minimum checks before operating the cryostat:
- [ ] PLC temperatures match PLC programmer display
- [ ] Gradient mode setpoint change reaches PLC
- [ ] Interlock status shows `ok: true` in nominal conditions
- [ ] Level sensor pF values are stable and sensible
- [ ] **Level thresholds recalibrated** for pF units before enabling autofill

Then run the **First Run Smoke Test** in VERIFICATION_CHECKLIST.md §10.

---

## Step 8 — Parallel operation and cutover

The old Node-RED pipeline publishes to `sensor/ch4_voltage` and `sensor/ch5_voltage`
and writes DF251/DF252 in the PLC. The new Python service writes those same
registers. To avoid conflicts during transition:

1. Run both systems in parallel briefly and compare readings.
2. In the old Node-RED, **disable** (but do not delete) the nodes that write
   DF251 and DF252 once you confirm the Python service is doing it correctly.
3. Disable the old InfluxDB output nodes in Node-RED to avoid duplicate data.
4. When satisfied, you can delete the old flows.

---

## Routine service management

```bash
# Start / stop / restart
sudo systemctl start   xsphere-slowcontrol
sudo systemctl stop    xsphere-slowcontrol
sudo systemctl restart xsphere-slowcontrol

sudo systemctl start   xsphere-omega-logger
sudo systemctl stop    xsphere-omega-logger
sudo systemctl restart xsphere-omega-logger

# Live logs
journalctl -u xsphere-slowcontrol  -f
journalctl -u xsphere-omega-logger -f

# Check MQTT heartbeat (confirms Python service is running)
mosquitto_sub -h localhost -t 'xsphere/status/service/heartbeat' -v
```

---

## Troubleshooting quick reference

| Symptom | Likely cause | Fix |
|---|---|---|
| `[plc] Modbus connection refused` | Wrong PLC IP | Update `plc.host` in `config.yaml` |
| PLC temperatures read as garbage floats | Wrong byte order | Toggle `>f` ↔ `<f` in `plc.py:_read_float` |
| Omega logger: `Cannot open serial port` | Wrong port or missing dialout permission | Update `serial_port` in `config.yaml`; add user to `dialout` |
| Omega reads all channels as FAULT | Wrong baud rate or Modbus address | Check `baud_rate` and `modbus_address` in `config.yaml` |
| Level sensor pF reading drifts with no liquid | CAPDAC adjusting; probe settling | Wait ~30 s after power-on; calibrate offset |
| Autovalve fires immediately at startup | Level thresholds not recalibrated for pF | Update `level_low`/`level_high` in `config.yaml` |
| Interlock alert fires: `temperature_stale` | Sensor not publishing or wrong topic | Check ESP32/Omega connection; check topic names |
| Dashboard shows no data | Node-RED MQTT broker node wrong | Edit broker node, set host to `localhost` |
| Telegraf not writing to InfluxDB | Bad token or wrong org/bucket | Check `telegraf/.env`; verify token in InfluxDB UI |
