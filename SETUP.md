# xsphere Slow Control — Setup Guide

Follow this guide to install the new slow control system on a fresh xbox-pi.
The old Node-RED pipeline can remain running in parallel until you are satisfied
with the new system (see Step 8).

---

## Prerequisites

The xbox-pi should already be running [IOTstack](https://sensorsiot.github.io/IOTstack/)
with the following Docker services active:

- **Mosquitto** (MQTT broker) on port 1883
- **InfluxDB 2.x** on host port 8087 (8086 inside the Docker network)
- **Telegraf** (no UI; see Step 4)
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

Pull in the LabJack submodule (skip if you cloned with `--recurse-submodules`):
```bash
cd /home/xbox/xsphere-slow-control
git submodule update --init --recursive
```

---

## Step 2 — Python slow control service

### 2a. Create a conda environment

```bash
conda create -n slowcontrol python=3.11 -y
conda activate slowcontrol
cd /home/xbox/xsphere-slow-control
pip install -r requirements.txt
pip install -e LJ-python-controller/      # LabJack T7 plugin (optional; needs the LJM runtime to actually talk to a T7)
```

> The systemd unit files run `/home/xbox/miniconda3/envs/slowcontrol/bin/python`,
> i.e. this `slowcontrol` env. If you use a different env name, edit the
> `ExecStart=` line in both `.service` files.

### 2b. Edit the configuration

```bash
nano /home/xbox/xsphere-slow-control/slowcontrol/config.yaml
```

**Must change before first run:**
- `plc.host` — set to the PLC's actual IP address (check router DHCP table
  or PLC front panel; it is on the `192.168.8.x` subnet)

Everything else can be left as default for first boot.

### 2c. Test-run manually

Run from the **repo root** — `slowcontrol` is a package, so `python -m slowcontrol.app`
must be launched from the directory that contains it:

```bash
conda activate slowcontrol
cd /home/xbox/xsphere-slow-control
python -m slowcontrol.app -c slowcontrol/config.yaml -v
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

### 2e. Keep the journal on disk

xbox-pi's `/etc/systemd/journald.conf` sets `Storage=volatile`: the journal lives
in RAM (about 75 MB) and is lost on every reboot. This drop-in overrides that and
keeps up to 1 GB under `/var/log/journal`, so `journalctl -u xsphere-slowcontrol`
still has the lines from before a reboot or power cut.

```bash
sudo mkdir -p /etc/systemd/journald.conf.d
sudo cp /home/xbox/xsphere-slow-control/slowcontrol/xsphere-journald.conf \
        /etc/systemd/journald.conf.d/xsphere.conf
sudo systemctl restart systemd-journald
sudo journalctl --flush        # move what is in RAM onto disk
```

Restarting `systemd-journald` does not restart any other service.

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

### 3f. Heater supply — R&S NGE103 (USB-TMC)

The NGE103 behind the three heaters plugs straight into a USB port of
xbox-pi. Leave the supply in **USB-TMC** mode (Menu → Interface → USB; the
status bar shows `TMC`). No VISA library or extra Python package is needed:
the kernel's `usbtmc` driver turns the supply into `/dev/usbtmcN`, and the
slow control service reads and writes that file.

One udev rule lets the service's user open it and gives it a name that does
not depend on plug-in order:

```bash
cd /home/xbox/xsphere-slow-control
sudo cp slowcontrol/99-xsphere-nge.rules /etc/udev/rules.d/
sudo udevadm control --reload-rules
sudo udevadm trigger --subsystem-match=usbmisc
ls -l /dev/nge100        # → a symlink to usbtmc0, group dialout, mode rw-rw----
```

(`xbox` is already in `dialout` from step 3a.) The `nge:` block in
`slowcontrol/config.yaml` then needs `enabled: true`, `resource: /dev/nge100`
and — so that no other NGE in the lab is ever driven by mistake — `serial:`
set to this unit's serial number (Menu → Device → Information on the supply;
if it is wrong, the card's "this is NGE serial …" error names the right one).
Restart the service and look for the **Heater supply** card on the Control
page; `journalctl -u xsphere-slowcontrol | grep nge` shows what it found and
did.

On first contact the service adopts whatever the supply is doing and switches
nothing — except the protection settings described in the README (over-power
protection and electronic fuse off, OVP level at maximum), unless
`nge.disable_auto_off` is `false`.

### 3g. Turbo pumps — Leybold TURBOVAC 90 i (USB, read-only)

Each pump's frequency converter has a USB B socket; run a USB cable (5 m at
most) from it to xbox-pi, directly or through a hub. Nothing needs installing:
the converter is a standard USB serial device (`25e9:0bb8`, kernel driver
`cdc_acm`), `xbox` can open it through the `dialout` group from step 3a, and
ModemManager leaves it alone. Leybold asks for interface cables to be plugged
and unplugged with the pump stopped and unpowered.

```bash
lsusb -d 25e9:0bb8        # one line per pump that is on USB and powered
ls -l /dev/ttyACM*        # one tty each
```

The two pumps cannot be told apart by anything on the USB side — they report
the same USB serial number, and `/dev/ttyACM0` / `1` just follow plug-in order
— so the `turbovac:` block in `slowcontrol/config.yaml` names each pump by
**its own serial number** (on the nameplate; the pump reports the same number
when asked):

```yaml
turbovac:
  enabled: true
  pumps:
    ghs:   {label: "GHS turbo — gas handling system + science chamber", serial: "31003077799"}
    outer: {label: "Outer vacuum turbo", serial: ""}
```

An entry with an empty `serial` takes the one TURBOVAC on USB that no other
entry names, which is how a newly connected pump first shows up. Its block on
the Control page then says `not pinned` and shows the serial number it read:
copy that into `config.yaml` and restart the service, so the entry can never
end up on the wrong pump. The names `ghs` and `outer` are fixed — `state.yaml`,
`telegraf.conf` and the Control page refer to them.

The service only ever reads the pumps (see the top of
`slowcontrol/drivers/turbovac.py`). `journalctl -u xsphere-slowcontrol | grep
turbovac` shows what it found, and from then on every change of pump state and
every new entry in a pump's error memory.

---

## Step 4 — Telegraf (MQTT → InfluxDB pipeline)

On xbox-pi Telegraf runs as the `telegraf` container of the IOTstack stack
(4a–4e).  A native apt install also works (4f) but is not what the lab runs.

> **The container does not read the repo copy of the config.**  It reads
> `~/IOTstack/volumes/telegraf/telegraf.conf` (bind-mounted to `/etc/telegraf`).
> Pulling a commit that changes `telegraf/telegraf.conf` therefore changes
> nothing until you redeploy it (4d); until then a newly added measurement is
> silently not recorded.

### 4a. Add the Telegraf container

If `docker compose ps` (Prerequisites) does not list `telegraf`:
```bash
cd ~/IOTstack
./menu.sh                      # Build Stack → tick telegraf → build
docker compose up -d telegraf
```

### 4b. Create InfluxDB credentials

1. Open InfluxDB UI: `http://192.168.8.116:8087` (the stack maps the
   container's port 8086 to host port 8087).
2. Create an organisation and a bucket if they do not already exist.  The
   running system uses organisation `xbox-server` and bucket `xsphere`.
3. Go to **Data → API Tokens → Generate API Token** and create a token with
   write access to the bucket.  Copy the token.

### 4c. Connection values (first deploy only)

```bash
cp /home/xbox/xsphere-slow-control/telegraf/.env.example \
   /home/xbox/xsphere-slow-control/telegraf/.env
nano /home/xbox/xsphere-slow-control/telegraf/.env
```

Fill in (these are addresses as seen from inside the Docker network, so use the
container names and container ports, not `localhost` or host port 8087):
```
INFLUX_URL=http://influxdb2:8086
INFLUX_TOKEN=<paste token here>
INFLUX_ORG=xbox-server
INFLUX_BUCKET=xsphere
MQTT_HOST=mosquitto
MQTT_PORT=1883
```

### 4d. Deploy the Telegraf configuration

`telegraf/deploy-iotstack.sh` substitutes the `@PLACEHOLDER@` tokens in
`telegraf/telegraf.conf`, validates the result with the container's own
Telegraf build, shows a diff against what is deployed (token hidden), and with
`--apply` backs up the old file, installs the new one and restarts the
container.

```bash
cd /home/xbox/xsphere-slow-control

# First deploy: take the values from telegraf/.env
bash telegraf/deploy-iotstack.sh --env            # preview, changes nothing
bash telegraf/deploy-iotstack.sh --env --apply    # install + restart

# Every later change to telegraf/telegraf.conf: the values already deployed
# are carried over, so no .env is needed
bash telegraf/deploy-iotstack.sh
bash telegraf/deploy-iotstack.sh --apply
```

The restart loses the MQTT messages published while Telegraf is down (a few
seconds).  The script finishes by checking that every MQTT input reconnected
and prints the rollback command if one did not.

### 4e. Verify data is flowing

```bash
# Messages are arriving at the broker
docker exec mosquitto mosquitto_sub -h localhost -t 'xsphere/sensors/#' -v -C 5

# Telegraf connected one consumer per input section and logs no errors
docker logs --tail 20 telegraf
```

Then check InfluxDB itself — Data Explorer → bucket `xsphere`, or the
per-measurement count query in OPERATIONS.md ("Changing what Telegraf
records").  Every measurement with a live source should show points within a
few seconds.

### 4f. Alternative: native apt Telegraf

Not used on xbox-pi; do not run it alongside the container or every point is
written twice.

```bash
# Add InfluxData repo
curl -s https://repos.influxdata.com/influxdata.key | sudo apt-key add -
echo "deb https://repos.influxdata.com/debian stable main" \
  | sudo tee /etc/apt/sources.list.d/influxdata.list
sudo apt update && sudo apt install telegraf -y

# Turn @VAR@ into ${VAR} so Telegraf reads the values from its environment
sed -E '/^[[:space:]]*#/!s/@([A-Z_]+)@/${\1}/g' \
    /home/xbox/xsphere-slow-control/telegraf/telegraf.conf \
  | sudo tee /etc/telegraf/telegraf.conf > /dev/null

# Pass telegraf/.env (4c, but with host addresses: localhost and port 8087)
sudo mkdir -p /etc/systemd/system/telegraf.service.d
sudo tee /etc/systemd/system/telegraf.service.d/env.conf <<'EOF'
[Service]
EnvironmentFile=/home/xbox/xsphere-slow-control/telegraf/.env
EOF

sudo systemctl daemon-reload
sudo systemctl restart telegraf
sudo systemctl status telegraf
```

---

## Step 5 — Flash ESP32 firmware

Install PlatformIO on your **development machine** (not xbox-pi):
```bash
pip install platformio
```

The firmware lives in git submodules under `firmware/` (run
`git submodule update --init --recursive` after cloning — `git submodule
update` always checks out the commit this repo pins, which is on each
submodule's `main`).

### 5a. GHS ESP32 (pressure / vacuum / environment / legacy LN2 level)

`firmware/gas-handling-system/Software/Xenon Gas Handling System Sensor Suite/`

Before flashing, verify in `src/main.cpp`:
- `ssid` / `password` (WiFi), `mqtt_server` IP
- `FEG`, `SETRA{1,2,3}_PSI_PER_VOLT`, `PSI_TO_MBAR`, `VAC_A`/`VAC_B`
  (see VERIFICATION_CHECKLIST.md §5)
- `PUBLISH_LEGACY_ANALOG_LEVEL` — set to 1 if this board's ADC2 CH0/CH1 are
  reading the ballast / primary_xe analog level signals (it then publishes
  `xsphere/sensors/level/{ballast,primary_xe}`); 0 otherwise.

```bash
pio run -t upload       # connect the GHS ESP32 via USB
```

Confirm:
```bash
mosquitto_sub -h 192.168.8.116 -t 'xsphere/sensors/pressure/#'    -v
mosquitto_sub -h 192.168.8.116 -t 'xsphere/sensors/vacuum/#'      -v
mosquitto_sub -h 192.168.8.116 -t 'xsphere/sensors/environment/#' -v
```

### 5b. LN2 level sensors — MPR121 (analog output)

`firmware/liquid-level-sensor/Software/MPR121 Level Sensor/`

Each board reads an MPR121 capacitive electrode and emits an **analog (PWM)
level signal on GPIO26** — it does *not* use WiFi/MQTT, so the autofill loop
keeps working if the network is down. That analog signal goes to the GHS
ESP32's ADC2 (which publishes `xsphere/sensors/level/{ballast,primary_xe}`)
and/or to a PLC analog input.

For each electrode/vessel, edit `src/main.cpp`:
- `ELECTRODE` (MPR121 electrode number, e.g. 0 = internal LN, 5 = ballast/bottle)
- `EMPTY_COUNT` / `FULL_COUNT` (calibration counts — set both to 0.0 to run an
  interactive calibration over serial at startup)
- the per-electrode charge-current / charge-time registers in
  `apply_high_sensitivity_settings()`

then build and flash:
```bash
cd "firmware/liquid-level-sensor/Software/MPR121 Level Sensor"
pio run -e esp32dev -t upload
```

The cryostat LN2 level is read on a CLICK analog input (the PLC publishes it
on `xsphere/sensors/level/cryostat` via the slow-control service). Confirm all
levels:
```bash
mosquitto_sub -h 192.168.8.116 -t 'xsphere/sensors/level/#' -v
```

### 5c. LabJack T7 (RTDs and gradiometer thermocouples)

The T7 is not "flashed" — it is configured via its registers (AIN extended
features: `AINx_EF_INDEX=40` for the PT100 RTDs with the 200 µA current
source, `AINx_EF_INDEX=22` for the type-K thermocouples; differential AIN
pairs). On xbox-pi, install the LJM runtime and point the `labjack:` block of
`slowcontrol/config.yaml` at the device:
```bash
# LJM runtime (aarch64) — https://support.labjack.com/docs/ljm-software-installer-linux-arm-family
curl -fsSLO "https://files.labjack.com/installers/LJM/Linux/AArch64/release/LabJack-LJM_2025-05-07.zip"
unzip LabJack-LJM_2025-05-07.zip && cd LabJack-LJM_2025-05-07
sudo ./labjack_ljm_installer.run -- --without-kipling
```
The slow-control service then reads the EF registers and publishes
`xsphere/sensors/temperature/labjack/{rtd,tc}/<channel>`; the type-K
gradiometers are re-referenced in software to a nearby RTD (configured in the
`labjack:` block — a LabJack RTD by name, or `plc/rtd/<n>` for a PLC RTD).

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
| Telegraf not writing to InfluxDB | Bad token or wrong org/bucket | `docker logs telegraf`; check the `[[outputs.influxdb_v2]]` values in `~/IOTstack/volumes/telegraf/telegraf.conf`; verify token in InfluxDB UI |
| One measurement missing from InfluxDB while its MQTT topic is publishing | `telegraf/telegraf.conf` was changed but not redeployed | `bash telegraf/deploy-iotstack.sh` shows the pending diff; add `--apply` (Step 4d) |
