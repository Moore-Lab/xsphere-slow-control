# Sensor Calibration Guide

This guide covers calibration of the PT100 RTDs and the differential K-type
thermocouple used in the xsphere cryostat.  Run these procedures before the
first cool-down and repeat if a sensor is replaced or the wiring changes.

---

## Overview

| Sensor | Script(s) | What it corrects |
|--------|-----------|-----------------|
| PT100 RTD (each channel) | `rtd_leadwire_calibration.py` → `rtd_cvd_calibration.py` | Lead wire imbalance + RTD element offset and gain |
| Differential K-type TC | `tc_calibration.py` | Electronics gain + amplifier offset |

Both calibrations use the same two reference temperatures: **ice bath (0 °C)**
and **LN2 bath (~−195.8 °C, pressure-corrected)**.

---

## Equipment

- Digital multimeter (DMM) — 4-wire Kelvin mode preferred for lead resistance
- Crushed-ice bath: distilled water + crushed ice in a thermos, stirred gently
- LN2 dewar (small, bench-top is fine)
- Barometer or local weather station giving **station pressure** in Pa
  (not sea-level corrected — check the station-pressure field specifically)
- Omega RDXL6SD-USB logger running and publishing to MQTT
- A terminal to subscribe to MQTT and record values

---

## Part 1 — PT100 RTD calibration

RTD calibration is a two-step process.  Step 1 measures the physical lead wire
resistances so that step 2 can separate the wiring error from the RTD element
error.  If you skip step 1, step 2 still works but is less accurate.

### Step 1 — Lead wire resistance (`rtd_leadwire_calibration.py`)

**When to run:** once, before the RTD is wired into the PLC, while all 4 leads
are still accessible at the connector end.  Power off the PLC first.

**What you measure:** five 2-wire DMM readings between pairs of leads.  The RTD
can remain mounted in the cryostat.

```
R(A1–A2)  both wires on Terminal A    → r_A1 + r_A2  (no RTD in path)
R(B1–B2)  both wires on Terminal B    → r_B1 + r_B2  (no RTD in path)
R(A1–B1)  A1 to B1                    → r_A1 + R_rtd + r_B1
R(A2–B1)  A2 to B1                    → r_A2 + R_rtd + r_B1
R(A1–B2)  A1 to B2                    → r_A1 + R_rtd + r_B2
```

> **Why R(A1–A2) bypasses the RTD:** both A wires connect to the same terminal
> on the sensing element, so current travels A1 → terminal A → A2 without
> passing through the platinum wire.  Same logic for R(B1–B2).  This is the
> key advantage of a 4-wire RTD.

**Run the script:**

```bash
python rtd_leadwire_calibration.py
```

Enter each measurement in ohms when prompted.  The script prints individual
lead resistances and the key output value:

```
delta_R_lead = <value in Ω>
```

Copy this number — you will enter it in step 2.

**PLC wiring (do this after the measurement):**

| PLC pin | Wire | Terminal |
|---------|------|----------|
| 1       | A1   | A — single lead |
| 2       | A2   | A — compensation lead |
| 3       | B1   | B — measurement return |
| —       | B2   | leave disconnected |

---

### Step 2 — Two-point CVD calibration (`rtd_cvd_calibration.py`)

**What you measure:** the Omega logger's temperature reading at two reference
points.  The Omega does the resistance-to-temperature conversion internally;
the script works backwards from those readings.

**Procedure:**

1. Start the Omega logger and subscribe to its MQTT topic:
   ```bash
   mosquitto_sub -h 192.168.8.116 -t 'xsphere/sensors/temperature/omega/ch5' -v
   ```
   (Replace `ch5` / `ch6` with the channel wired to your RTD.)

2. **Ice bath:** immerse the RTD sensor in a well-stirred ice bath.  Wait for
   the reading to stabilise (~2 min).  Record:
   - `value_c` (the instrument temperature reading in °C)
   - `resistance_ohm` (back-calculated from the reading — useful for records)

3. **LN2 bath:** immerse the RTD in LN2.  Wait for boiling to calm and the
   reading to stabilise (~1 min).  Record:
   - `value_c`
   - `resistance_ohm`
   - Local **station pressure** in Pa at this moment

4. Run the script:
   ```bash
   python rtd_cvd_calibration.py
   ```
   Enter when prompted:
   - Ice bath instrument reading (°C)
   - LN2 bath instrument reading (°C)
   - Barometric pressure (Pa)
   - `delta_R_lead` from step 1 (enter 0 if you skipped step 1)

**Output:**

```
alpha = <value>    (scale; ideal = 1.000000)
beta  = <value> Ω  (offset; ideal = 0)
```

**Applying the calibration** to a live temperature reading:

```python
from rtd_cvd_calibration import apply_calibration

T_corrected = apply_calibration(T_meas, alpha, beta)
```

Or inline:
```python
from rtd_cvd_calibration import R_from_T, T_from_R

R_corr = alpha * R_from_T(T_meas) + beta
T_corr = T_from_R(R_corr)
```

Record `alpha` and `beta` somewhere permanent (e.g., a lab notebook entry
and/or the channel label in the Grafana dashboard).

---

## Part 2 — Differential TC calibration (`tc_calibration.py`)

The differential TC measures temperature *differences* between two points
inside the cryostat.  J2 sits next to a calibrated PT100 RTD, which provides
the absolute reference temperature at runtime.

> **Why the ice/LN2 calibration works for sub-1 K measurements at 165 K:**
> The calibration determines the electronics gain `a` over a large 196 °C
> signal.  The NIST K-type polynomial then handles the temperature-dependent
> Seebeck coefficient at the operating point (~23 µV/°C at 165 K vs.
> ~41 µV/°C at room temperature).  A simple linear T-space calibration
> would give ~40 % error in sensitivity at 165 K.

### Procedure

**Step 1 — zero measurement**

With both TC junctions at the same temperature (both in the same ice bath, or
both hanging in ambient air), record the raw EMF reading: **V_zero**.

Subscribe to the TC channel:
```bash
mosquitto_sub -h 192.168.8.116 -t 'xsphere/sensors/temperature/omega/ch1' -v
```

Record the `emf_mv` field.  This captures amplifier input offset.
Ideally V_zero ≈ 0 mV.

**Step 2 — differential measurement**

Put **J1** in the ice bath (0 °C) and **J2** in LN2.  Wait for both to
stabilise.  Record:
- `emf_mv` as **V_diff**
- Local station pressure in Pa

> If V_diff is negative, J1 and J2 are reversed relative to the assumed
> convention.  That is fine — `a` will come out negative and the sign of ΔT
> will be correct.

**Run the script:**

```bash
python tc_calibration.py
```

Enter V_zero, V_diff, and pressure when prompted.

**Output:**

```
a (gain correction) = <value>   (ideal 1.0)
b (offset)          = <value> mV (ideal 0.0)
```

The script also prints the Seebeck coefficient at 165 K and the equivalent
voltage per kelvin, so you can confirm your amplifier/ADC has enough
resolution.

### Runtime usage

At runtime, the calibration is applied as:

```python
from tc_calibration import delta_T_from_emf

# T_ref_C comes from the co-located, calibrated PT100 RTD
delta_T = delta_T_from_emf(E_raw_mv, T_ref_C, a, b)
```

`delta_T` is T_J1 − T_J2 in °C (positive = J1 is warmer).

---

## Recording your calibration results

Keep a table like this in your lab notebook or in a `calibration_values.yaml`
file in this directory:

```yaml
# Last updated: YYYY-MM-DD
rtd:
  ch5:
    alpha: 1.00000000
    beta:  0.000000   # Ω
    delta_R_lead: 0.000000  # Ω
    notes: "ballast dewar clamp, calibrated YYYY-MM-DD"
  ch6:
    alpha: 1.00000000
    beta:  0.000000
    delta_R_lead: 0.000000
    notes: "cryostat exterior clamp"

tc:
  ch1:
    a: 1.000000
    b: 0.0000   # mV
    notes: "differential pair, J2 next to ch5 RTD"
```

---

## Quick reference: what to record from the Omega logger

During calibration, subscribe and save the full JSON payloads.  The fields
you need are:

| Channel type | Field | Used in |
|---|---|---|
| RTD | `value_c` | `rtd_cvd_calibration.py` — ice and LN2 readings |
| RTD | `resistance_ohm` | Cross-check / records |
| TC  | `emf_mv` | `tc_calibration.py` — V_zero and V_diff |
| TC  | `value_c` | Cross-check / records |
