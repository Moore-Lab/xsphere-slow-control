"""
Differential K-type thermocouple calibration for cryogenic gradient measurement.

SETUP
─────
Two TC junctions in series with reversed polarity:

    output EMF  =  E_K(T_J1)  −  E_K(T_J2)

where E_K is the NIST ITS-90 K-type EMF function (mV, reference 0 °C).
J2 sits next to a calibrated PT100 RTD, so T_J2 = T_ref is known at runtime.

WHY THIS APPROACH
─────────────────
The K-type Seebeck coefficient is strongly temperature-dependent:
  ~41 µV/°C at 25 °C
  ~23 µV/°C at 165 K  (-108 °C)   ← operating point
  ~17 µV/°C at 80 K   (-193 °C)

Calibrating `a` (electronics gain) over a large 196 °C span (ice/LN2) and
then using the NIST polynomial at the operating temperature accounts for this
variation correctly.  A temperature-space linear calibration (old approach)
would conflate the varying Seebeck coefficient with the electronics gain and
give wrong answers below its calibration range.

CALIBRATION PROCEDURE
─────────────────────
Step 1 — zero offset
  Put both junctions at the same temperature (both in ice bath, both in
  ambient air, anywhere ΔT ≈ 0).  Record V_zero.  Ideally ≈ 0 but will
  capture amplifier input offset.

Step 2 — gain
  Put J1 in ice bath (0 °C), J2 in LN2.  Record V_diff and local pressure.
  True differential EMF:
      E_true = E_K(0 °C) − E_K(T_LN2)
             = 0 − E_K(T_LN2)
             ≈ +5.89 mV  (ice warmer, J1 on hot side)
  If the sign of V_diff is negative, J1 and J2 are reversed — the calibrated
  ΔT will still be correct (convention: positive ΔT means J1 is warmer).

CALIBRATION RESULT
──────────────────
    a  — electronics gain correction  (ideal ≈ 1.0)
    b  — offset in mV                 (ideal ≈ 0.0)

    E_cal = a * E_raw + b   ≈   E_K(T_J1) − E_K(T_J2)

RUNTIME MEASUREMENT
───────────────────
Given E_raw (measured) and T_ref (RTD reading for J2 junction, °C):

    E_cal  = a * E_raw + b
    T_J1   = T_from_E_K( E_K(T_ref) + E_cal )
    ΔT     = T_J1 − T_ref

At 165 K (−108 °C) this is accurate to ~1 mK for a 1 K gradient provided
the electronics gain is stable.  The RTD uncertainty dominates below that.
"""

import numpy as np

# ── NIST ITS-90 K-type EMF polynomial coefficients ───────────────────────────
# E in mV, T in °C.

_K_NEG_COEFFS = np.array([   # −200 °C to 0 °C
    0.0,
    3.9450128025e-2,
    2.3622373598e-5,
   -3.2858906784e-7,
   -4.9904828777e-9,
   -6.7509059173e-11,
   -5.7410327428e-13,
   -3.1088872894e-15,
   -1.0451609365e-17,
   -1.9889266878e-20,
   -1.6322697486e-23,
])

_K_POS_COEFFS = np.array([   # 0 °C to 1372 °C
   -1.7600413686e-2,
    3.8921204975e-2,
    1.8558770032e-5,
   -9.9457592874e-8,
    3.1840945719e-10,
   -5.6072844889e-13,
    5.6075059059e-16,
   -3.2020720003e-19,
    9.7151147152e-23,
   -1.2104721275e-26,
])
_K_EXP = (0.118597600000e0, -0.118343200000e-3, 0.126968600000e3)


def E_from_T_K(T):
    """K-type EMF (mV) from temperature (°C).  Vectorised, full ITS-90 range."""
    T = np.asarray(T, dtype=float)
    E = np.zeros_like(T)

    neg = T < 0.0
    if np.any(neg):
        t = T[neg]
        E[neg] = np.polyval(_K_NEG_COEFFS[::-1], t)

    pos = ~neg
    if np.any(pos):
        t = T[pos]
        a0, a1, a2 = _K_EXP
        E[pos] = np.polyval(_K_POS_COEFFS[::-1], t) + a0 * np.exp(a1 * (t - a2)**2)

    return E


def T_from_E_K(E):
    """K-type temperature (°C) from EMF (mV).  Newton–Raphson, vectorised."""
    E = np.asarray(E, dtype=float)
    T = E / 3.945e-2   # linear initial guess (good for cryogenic range)
    for _ in range(25):
        f    = E_from_T_K(T) - E
        dfdT = (E_from_T_K(T + 1e-6) - E_from_T_K(T - 1e-6)) / 2e-6
        step = f / dfdT
        T   -= step
        if np.max(np.abs(step)) < 1e-9:
            break
    return T


def seebeck_at(T_C):
    """K-type Seebeck coefficient (µV/°C) at temperature T_C (°C)."""
    dE = (E_from_T_K(T_C + 0.001) - E_from_T_K(T_C - 0.001)) / 0.002
    return float(dE * 1e3)   # mV/°C → µV/°C


# ── LN2 boiling point ─────────────────────────────────────────────────────────
def ln2_boiling_temp_C(P_Pa, P0_Pa=101325.0):
    """LN2 boiling temperature (°C) at pressure P_Pa via Clausius-Clapeyron."""
    T0_K, dH, Rg = 77.355, 5.56e3, 8.314
    T_K = 1.0 / ((1.0 / T0_K) - (Rg / dH) * np.log(P_Pa / P0_Pa))
    return float(T_K - 273.15)


# ── Calibration ───────────────────────────────────────────────────────────────
def calibrate_thermocouple(V_zero, V_diff, P_ln2_Pa):
    """
    Compute calibration coefficients from a two-point measurement.

    Parameters
    ----------
    V_zero   : float — EMF (mV) with both junctions at the same temperature
                       (ΔT ≈ 0).  Captures amplifier offset.
    V_diff   : float — EMF (mV) with J1 in ice bath (0 °C), J2 in LN2.
                       Sign encodes which junction is hotter.
    P_ln2_Pa : float — barometric pressure during LN2 measurement (Pa).

    Returns
    -------
    a, b      : float — calibration coefficients.
                        E_cal = a * E_raw + b  ≈  E_K(T_J1) − E_K(T_J2)
    T_ln2_C   : float — computed LN2 temperature used (°C), for reference.
    """
    T_ln2_C = ln2_boiling_temp_C(P_ln2_Pa)

    # True differential EMF for J1=ice (0°C), J2=LN2
    E_true_diff = float(E_from_T_K(0.0)) - float(E_from_T_K(T_ln2_C))
    E_true_zero = 0.0

    a = (E_true_diff - E_true_zero) / (V_diff - V_zero)
    b = E_true_zero - a * V_zero

    return a, b, T_ln2_C


# ── Runtime conversion ────────────────────────────────────────────────────────
def delta_T_from_emf(E_raw, T_ref_C, a, b):
    """
    Convert raw differential EMF to ΔT using the RTD reference temperature.

    Parameters
    ----------
    E_raw    : float or array — raw differential EMF from TC (mV).
    T_ref_C  : float — J2 junction temperature from nearby RTD (°C).
    a, b     : float — coefficients from calibrate_thermocouple().

    Returns
    -------
    delta_T  : float or array — T_J1 − T_J2 (°C).
                                Positive means J1 is warmer than J2.
    """
    E_cal  = a * np.asarray(E_raw, dtype=float) + b
    E_ref  = float(E_from_T_K(T_ref_C))
    T_J1   = T_from_E_K(E_ref + E_cal)
    return T_J1 - T_ref_C


# ── Interactive entry point ───────────────────────────────────────────────────
if __name__ == "__main__":
    print("=== Differential K-type TC calibration ===")
    print("J1 = measurement junction")
    print("J2 = reference junction (next to RTD)")
    print()

    V_zero   = float(input("V_zero : EMF with both junctions at same T [mV]: "))
    V_diff   = float(input("V_diff : EMF with J1 in ice bath, J2 in LN2  [mV]: "))
    P_ln2_Pa = float(input("Barometric pressure during LN2 measurement   [Pa]: "))

    a, b, T_ln2_C = calibrate_thermocouple(V_zero, V_diff, P_ln2_Pa)

    T_op = -108.15   # 165 K operating point
    S_op = seebeck_at(T_op)

    print("\n─── Calibration results ─────────────────────────────────")
    print(f"  LN2 temperature            = {T_ln2_C:.4f} °C")
    print(f"  a (gain correction)        = {a:.6f}  (ideal 1.0)")
    print(f"  b (offset)                 = {b:.4f} mV  (ideal 0.0)")
    print()
    print(f"  Seebeck coefficient at 165 K operating point:")
    print(f"    S(−108 °C) = {S_op:.2f} µV/°C")
    print(f"    → 1 K gradient ≈ {S_op:.1f} µV,  0.1 K ≈ {S_op/10:.1f} µV")
    print()
    print("  Runtime usage:")
    print("    delta_T = delta_T_from_emf(E_raw, T_ref_C, a, b)")
    print("    where T_ref_C comes from the co-located PT100 RTD")
    print()

    # Sanity check at calibration points
    dT_zero = delta_T_from_emf(V_zero, 0.0, a, b)
    dT_diff = delta_T_from_emf(V_diff, T_ln2_C, a, b)
    print("─── Sanity check at calibration points ──────────────────")
    print(f"  V_zero → ΔT = {dT_zero*1000:+.2f} m°C  (should be ≈ 0)")
    print(f"  V_diff (J2 at LN2) → ΔT = {dT_diff:.4f} °C  "
          f"(should be ≈ {-T_ln2_C:.2f} °C)")
    print("─────────────────────────────────────────────────────────")
