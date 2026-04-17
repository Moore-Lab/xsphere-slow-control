"""
PT100 two-point calibration using the Callendar-Van Dusen (CVD) equation.

PURPOSE
───────
Compute a linear correction in *resistance* space:

    R_true = alpha * R_meas + beta

where R_meas is the resistance implied by the instrument's temperature
reading, and R_true is the resistance implied by a known reference
temperature.  Working in resistance space is more accurate than a
temperature-space linear fit because the PT100 R-T relationship is
non-linear — particularly below −100 °C.

WORKFLOW
────────
This script is Step 2 of the three-step calibration workflow:

  Step 1  rtd_leadwire_calibration.py
          → Measures individual lead resistances r_Vp, r_Ip, r_Vm, r_Im.
          → Outputs delta_R  (= r_Ip − r_Vm, the lead imbalance correction).

  Step 2  rtd_cvd_calibration.py  (this script)
          → You supply delta_R from Step 1 and two reference measurements:
              · Ice bath  (T_true = 0.000 °C)
              · LN2 bath  (T_true = f(local pressure), ~−195.8 °C at 1 atm)
          → Outputs alpha, beta.

  Step 3  Apply correction to all future readings:
              T_corrected = T_from_R( alpha * R_from_T(T_meas) + beta )
          Convenience function: apply_calibration(T_meas, alpha, beta)

HOW delta_R IMPROVES THE CALIBRATION
─────────────────────────────────────
Without a lead correction, alpha and beta absorb both the lead-wire error
(a constant resistance offset) and any intrinsic RTD non-linearity.  The
two effects are conflated, so the calibration is less reliable outside the
two reference points.

When delta_R is supplied:
  1. The lead contribution is removed first (constant offset, known physics).
  2. alpha and beta then capture only the RTD-intrinsic correction.
  3. The result extrapolates better to temperatures outside −196 °C … 0 °C.

REFERENCE TEMPERATURES
──────────────────────
  Ice bath:  0.000 °C  (melting point of ice at 1 atm — use crushed ice
             and distilled water; stir gently; confirm the bath is at 0 °C,
             not just "cold")

  LN2:       calculated from the Clausius-Clapeyron equation using the
             measured local barometric pressure.  At sea level (101325 Pa)
             this is approximately −195.80 °C.  Every 1 kPa difference from
             1 atm shifts the boiling point by ≈ 0.11 °C.
             Measure pressure with a barometer or check a weather station
             for the current station pressure (not sea-level-corrected).
"""

import numpy as np

# ── CVD constants for PT100 (IEC 60751) ──────────────────────────────────────
R0 = 100.0        # Ω — resistance at 0 °C
A  =  3.9083e-3
B  = -5.775e-7
C  = -4.183e-12   # only applies for T < 0 °C


# ── CVD forward: T (°C) → R (Ω) ──────────────────────────────────────────────
def R_from_T(T):
    """
    Temperature (°C) → resistance (Ω).  Vectorised over numpy arrays.
    Full IEC 60751 range: −200 °C to +850 °C.
    """
    T = np.asarray(T, dtype=float)
    R = np.zeros_like(T)

    pos = T >= 0
    R[pos] = R0 * (1 + A*T[pos] + B*T[pos]**2)

    neg = T < 0
    R[neg] = R0 * (1 + A*T[neg] + B*T[neg]**2
                   + C * (T[neg] - 100) * T[neg]**3)
    return R


# ── CVD inverse: R (Ω) → T (°C) ──────────────────────────────────────────────
def T_from_R(R):
    """
    Resistance (Ω) → temperature (°C).
    T ≥ 0 °C: analytic quadratic solution.
    T < 0 °C: Newton–Raphson (converges in < 10 iterations to < 1 µ°C).
    """
    R = np.asarray(R, dtype=float)
    T = np.zeros_like(R)

    # ── T ≥ 0 °C ─────────────────────────────────────────────────────
    pos = R >= R0
    if np.any(pos):
        r = R[pos] / R0
        # Quadratic:  B*T² + A*T + (1 - r) = 0
        T[pos] = (-A + np.sqrt(A**2 - 4*B*(1 - r))) / (2*B)

    # ── T < 0 °C ─────────────────────────────────────────────────────
    neg = R < R0
    if np.any(neg):
        T_g = (R[neg]/R0 - 1) / A          # linear initial guess
        for _ in range(20):
            f    = R_from_T(T_g) - R[neg]
            dfdT = R0 * (A + 2*B*T_g
                         + C*(4*T_g**3 - 300*T_g**2 + 30000*T_g))
            step = f / dfdT
            T_g -= step
            if np.max(np.abs(step)) < 1e-9:
                break
        T[neg] = T_g

    return T


# ── LN2 boiling point from barometric pressure ────────────────────────────────
def ln2_boiling_temp_C(P_Pa, P0_Pa=101325.0):
    """
    LN2 boiling temperature (°C) at pressure P_Pa using Clausius-Clapeyron.

    Parameters
    ----------
    P_Pa  : float  — local barometric pressure in Pascals.
                     Use *station* pressure (not sea-level-corrected).
    P0_Pa : float  — reference pressure (default 101325 Pa = 1 atm).

    Returns
    -------
    float  — boiling temperature in °C.
    """
    T0_K = 77.355    # K  (boiling point at 1 atm)
    dH   = 5.56e3    # J/mol  (latent heat of vaporisation)
    Rg   = 8.314     # J/mol/K

    T_K = 1.0 / ((1.0/T0_K) - (Rg/dH) * np.log(P_Pa / P0_Pa))
    return float(T_K - 273.15)


# ── Core calibration function ─────────────────────────────────────────────────
def compute_calibration(T_meas_ice, T_meas_ln2, P_ln2_Pa,
                        delta_R_lead=0.0):
    """
    Compute resistance-space calibration coefficients alpha, beta such that:

        R_true = alpha * R_meas + beta

    where R_meas = R_from_T(T_meas) is the resistance implied by the
    instrument's temperature reading, and R_true is the resistance
    corresponding to the known reference temperature.

    Parameters
    ----------
    T_meas_ice    : float — instrument reading in ice bath (°C).
    T_meas_ln2    : float — instrument reading in LN2 bath (°C).
    P_ln2_Pa      : float — barometric pressure during LN2 measurement (Pa).
    delta_R_lead  : float — lead imbalance correction from
                            rtd_leadwire_calibration.py (Ω).
                            Default 0 (no lead correction applied).

    Returns
    -------
    alpha, beta : float
        Correction coefficients.  alpha ≈ 1 for a good PT100;
        beta accounts for systematic offset.
    T_true_ln2  : float — computed true LN2 temperature (°C), for reference.
    """
    # True reference temperatures
    T_true_ice = 0.0
    T_true_ln2 = ln2_boiling_temp_C(P_ln2_Pa)

    # Instrument-implied resistances (what the instrument "thinks" the R is)
    R_meas_ice = R_from_T(T_meas_ice)
    R_meas_ln2 = R_from_T(T_meas_ln2)

    # Apply lead correction: shift the inferred resistances by the known
    # lead imbalance so that alpha and beta fit only the RTD-intrinsic error.
    R_meas_ice_corr = R_meas_ice - delta_R_lead
    R_meas_ln2_corr = R_meas_ln2 - delta_R_lead

    # True resistances from reference temperatures
    R_true_ice = R_from_T(T_true_ice)
    R_true_ln2 = R_from_T(T_true_ln2)

    # Solve the 2×2 linear system:
    #   R_true_ice = alpha * R_meas_ice_corr + beta
    #   R_true_ln2 = alpha * R_meas_ln2_corr + beta
    alpha = (R_true_ln2 - R_true_ice) / (R_meas_ln2_corr - R_meas_ice_corr)
    beta  = R_true_ice - alpha * R_meas_ice_corr

    return alpha, beta, T_true_ln2


def apply_calibration(T_meas, alpha, beta):
    """
    Apply resistance-space calibration to a temperature measurement.

        T_meas  →  R_meas = R_from_T(T_meas)
                →  R_corr = alpha * R_meas + beta
                →  T_corr = T_from_R(R_corr)

    Parameters
    ----------
    T_meas       : float or array — instrument temperature reading(s) in °C.
    alpha, beta  : float — coefficients from compute_calibration().

    Returns
    -------
    float or array — corrected temperature(s) in °C.
    """
    R_meas = R_from_T(np.asarray(T_meas, dtype=float))
    R_corr = alpha * R_meas + beta
    return T_from_R(R_corr)


# ── Interactive entry point ───────────────────────────────────────────────────
if __name__ == "__main__":
    print("=== PT100 CVD two-point calibration ===")
    print("Step 2 of 2 (preceded by rtd_leadwire_calibration.py)")
    print()

    print("Enter the instrument reading at each reference point.")
    T_meas_ice = float(input("Instrument reading — ice bath [°C]: "))
    T_meas_ln2 = float(input("Instrument reading — LN2 bath  [°C]: "))
    P_ln2_Pa   = float(input("Barometric pressure during LN2 measurement [Pa]: "))

    print()
    print("Lead correction from rtd_leadwire_calibration.py")
    print("(Enter 0 if you skipped the lead wire measurement)")
    delta_R = float(input("delta_R_lead [Ω]: "))

    alpha, beta, T_true_ln2 = compute_calibration(
        T_meas_ice, T_meas_ln2, P_ln2_Pa, delta_R
    )

    print("\n─── Calibration Results ─────────────────────────────")
    print(f"  True LN2 temperature at {P_ln2_Pa:.0f} Pa: {T_true_ln2:.4f} °C")
    print()
    print(f"  alpha = {alpha:.8f}  (scale; ideal = 1.000000)")
    print(f"  beta  = {beta:.6f} Ω  (offset; ideal = 0)")

    if abs(delta_R) > 0:
        print()
        print(f"  Lead correction applied: delta_R = {delta_R*1000:.3f} mΩ")
        alpha_no_lead, beta_no_lead, _ = compute_calibration(
            T_meas_ice, T_meas_ln2, P_ln2_Pa, delta_R=0.0
        )
        print(f"  Without lead correction: alpha = {alpha_no_lead:.8f}, "
              f"beta = {beta_no_lead:.6f} Ω")

    print()
    print("  Correction equation:")
    print("    R_true = alpha * R_from_T(T_meas) + beta")
    print("    T_corr = T_from_R(R_true)")
    print()
    print("  Or use:  apply_calibration(T_meas, alpha, beta)")
    print()

    # Show correction at the two calibration points and a few intermediate values
    test_temps = np.array([T_meas_ice, -100.0, T_meas_ln2])
    corrected  = apply_calibration(test_temps, alpha, beta)

    print("─── Correction at sample temperatures ───────────────")
    print(f"  {'T_meas (°C)':>14}  {'T_corrected (°C)':>18}  {'ΔT (m°C)':>10}")
    for t_raw, t_corr in zip(test_temps, corrected):
        delta_mC = (t_corr - t_raw) * 1000
        print(f"  {t_raw:14.3f}  {t_corr:18.4f}  {delta_mC:+10.1f}")
    print("─────────────────────────────────────────────────────")
