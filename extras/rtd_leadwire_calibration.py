"""
4-wire PT100 lead resistance characterisation (for 3-wire reader).

SITUATION
─────────
You have a 4-wire PT100 (4 leads, 2 per terminal) but your reader (PLC RTD
input) is a 3-wire instrument.  Procedure:

  1. Use this script to measure all four individual lead resistances with a
     DMM while all 4 wires are accessible.
  2. Determine which 3 wires to connect to the PLC (see wiring guide below).
  3. Pass delta_R to rtd_cvd_calibration.py so the two-point calibration
     can account for the known lead imbalance.

WHY THIS MATTERS
────────────────
A 3-wire instrument compensates for lead resistance by assuming all three
leads are equal.  With a 4-wire RTD, you know the actual lead resistances,
so you can compute the exact residual error instead of treating it as unknown.

WIRING CONVENTION
─────────────────
A 4-wire PT100 has two terminals on the sensing element:

    Terminal A                Terminal B
        │                         │
    ────┤A1 (r_A1)   [PT100]  B1 (r_B1)├────
        │                         │
    ────┤A2 (r_A2)            B2 (r_B2)├────

  A1, A2 are both connected to Terminal A.
  B1, B2 are both connected to Terminal B.

MEASUREMENTS (DMM, 4-wire Kelvin recommended; RTD in circuit, power off)
─────────────────────────────────────────────────────────────────────────
Because A1 and A2 share Terminal A, measuring R(A1-A2) bypasses the RTD
element entirely — current goes wire A1 → Terminal A → wire A2.
Same for R(B1-B2).  This is the key advantage of 4-wire RTDs.

Make five 2-wire DMM measurements from the connector end:

  R_A1A2  = R between A1 and A2  =  r_A1 + r_A2       (NO RTD in path)
  R_B1B2  = R between B1 and B2  =  r_B1 + r_B2       (NO RTD in path)
  R_A1B1  = R between A1 and B1  =  r_A1 + R_rtd + r_B1  (through RTD)
  R_A2B1  = R between A2 and B1  =  r_A2 + R_rtd + r_B1  (through RTD)
  R_A1B2  = R between A1 and B2  =  r_A1 + R_rtd + r_B2  (through RTD)

  → R_rtd is NOT needed as input; it falls out of the solution.

SOLUTION (overdetermined, exact with these 5 measurements)
──────────────────────────────────────────────────────────
  r_A1 = [R_A1A2 + (R_A1B1 - R_A2B1)] / 2
  r_A2 = [R_A1A2 - (R_A1B1 - R_A2B1)] / 2
  r_B1 = [R_B1B2 + (R_A1B1 - R_A1B2)] / 2
  r_B2 = [R_B1B2 - (R_A1B1 - R_A1B2)] / 2
  R_rtd = R_A1B1 - r_A1 - r_B1

3-WIRE CONNECTION TO PLC
─────────────────────────
Connect these three wires to the PLC's 3-wire RTD input:

  PLC pin | Wire  | Terminal
  --------|-------|----------
  1       | A1    | A  (single lead)
  2       | A2    | A  (paired lead — compensation wire)
  3       | B1    | B  (measurement return)

  Leave B2 disconnected (or tie it to B1 at the connector — tying it adds
  an extra parallel path and can introduce noise; better to leave floating).

  Wire A1 and A2 are the "compensation pair" — the PLC's 3-wire circuit
  uses them to subtract lead resistance assuming A1 ≈ A2.
  Wire B1 carries the measurement return current.

LEAD CORRECTION (delta_R)
─────────────────────────
For the standard 3-wire bridge / current-source connection above:
  The PLC assumes A1 = A2 (equal paired leads).
  The residual measurement error in resistance is:

      delta_R  =  r_A2 - r_B1

  This is the resistance the PLC "adds" to the RTD reading because of
  lead imbalance.  Pass this to rtd_cvd_calibration.py.

  Physical meaning:
    delta_R > 0 → instrument reads R too HIGH → temperature reads too HIGH
    delta_R < 0 → instrument reads R too LOW  → temperature reads too LOW

NOTE: The exact formula for delta_R depends on the PLC's internal circuit.
The expression (r_A2 - r_B1) applies to the most common Wheatstone bridge
and current-source 3-wire configurations.  If you observe systematic offsets
larger than expected after applying this correction, verify the PLC wiring
against the PLC manual.
"""

import numpy as np


def solve_lead_resistances(R_A1A2, R_B1B2, R_A1B1, R_A2B1, R_A1B2):
    """
    Solve all four lead resistances and the RTD element resistance.

    Parameters
    ----------
    R_A1A2 : float  — DMM R(A1-A2) in ohms  [no RTD in path]
    R_B1B2 : float  — DMM R(B1-B2) in ohms  [no RTD in path]
    R_A1B1 : float  — DMM R(A1-B1) in ohms  [through RTD]
    R_A2B1 : float  — DMM R(A2-B1) in ohms  [through RTD]
    R_A1B2 : float  — DMM R(A1-B2) in ohms  [through RTD]

    Returns
    -------
    r_A1, r_A2, r_B1, r_B2 : float  — individual lead resistances (Ω)
    R_rtd                  : float  — RTD element resistance at room temp (Ω)
    """
    dA = R_A1B1 - R_A2B1   # = r_A1 - r_A2
    dB = R_A1B1 - R_A1B2   # = r_B1 - r_B2

    r_A1 = (R_A1A2 + dA) / 2.0
    r_A2 = (R_A1A2 - dA) / 2.0
    r_B1 = (R_B1B2 + dB) / 2.0
    r_B2 = (R_B1B2 - dB) / 2.0

    R_rtd = R_A1B1 - r_A1 - r_B1

    return r_A1, r_A2, r_B1, r_B2, R_rtd


def compute_delta_R(r_A2, r_B1):
    """
    Compute the lead-imbalance correction to pass to rtd_cvd_calibration.py.

    For the recommended 3-wire connection (A1, A2 paired; B1 single):
        delta_R = r_A2 - r_B1

    See module docstring for interpretation.
    """
    return r_A2 - r_B1


def summarise(r_A1, r_A2, r_B1, r_B2, R_rtd):
    """Print a human-readable measurement summary."""
    delta_R = compute_delta_R(r_A2, r_B1)

    print("\n─── Lead Wire Results ───────────────────────────────")
    print(f"  r_A1 (Terminal A, lead 1)  = {r_A1*1e3:8.3f} mΩ")
    print(f"  r_A2 (Terminal A, lead 2)  = {r_A2*1e3:8.3f} mΩ")
    print(f"  r_B1 (Terminal B, lead 1)  = {r_B1*1e3:8.3f} mΩ")
    print(f"  r_B2 (Terminal B, lead 2)  = {r_B2*1e3:8.3f} mΩ")
    print()
    print(f"  RTD element resistance     = {R_rtd:.4f} Ω")

    # Sanity: CVD predicts ~109.7 Ω at 25 °C, ~100.0 Ω at 0 °C
    if 95 <= R_rtd <= 150:
        T_approx = (R_rtd - 100.0) / (100.0 * 3.9083e-3)
        print(f"  → Corresponds to ≈ {T_approx:.1f} °C (linear approx)")
    else:
        print(f"  ⚠ R_rtd is outside expected range (95–150 Ω for RT)")

    print()
    print("  Recommended 3-wire connection to PLC:")
    print("    PLC pin 1 → A1  (single lead, Terminal A)")
    print("    PLC pin 2 → A2  (compensation lead, Terminal A)")
    print("    PLC pin 3 → B1  (measurement return, Terminal B)")
    print("    Leave B2 disconnected.")
    print()
    print(f"  Lead imbalance:  r_A1 - r_A2 = {(r_A1-r_A2)*1e3:+.3f} mΩ")
    print(f"                   r_A2 - r_B1 = {delta_R*1e3:+.3f} mΩ")
    print()

    # Temperature equivalent error
    alpha_approx = 3.9083e-3 / 100.0   # Ω/Ω/°C ≈ A/R0
    T_err_mC = delta_R / (100.0 * alpha_approx) * 1000
    print(f"  Estimated systematic temperature error from lead imbalance:")
    print(f"    ≈ {T_err_mC:+.1f} m°C  (if the PLC has no compensation)")
    print(f"    ≈ {T_err_mC/2:+.1f} m°C  (typical 3-wire compensation halves this)")
    print()
    print(f"  ─── Feed this value to rtd_cvd_calibration.py ───")
    print(f"  delta_R_lead = {delta_R:.6f} Ω")
    print("─────────────────────────────────────────────────────\n")

    return delta_R


if __name__ == "__main__":
    print("=== 4-wire PT100 lead resistance measurement ===")
    print("(For use with a 3-wire reader such as a PLC RTD input)")
    print()
    print("Make 5 two-wire DMM measurements from the connector end.")
    print("The RTD can remain in the cryostat — power off the PLC first.")
    print()
    print("Measurement guide:")
    print("  R_A1A2 : across the two A-terminal wires   → r_A1 + r_A2  (no RTD)")
    print("  R_B1B2 : across the two B-terminal wires   → r_B1 + r_B2  (no RTD)")
    print("  R_A1B1 : across A1 and B1                  → r_A1 + Rrtd + r_B1")
    print("  R_A2B1 : across A2 and B1                  → r_A2 + Rrtd + r_B1")
    print("  R_A1B2 : across A1 and B2                  → r_A1 + Rrtd + r_B2")
    print()

    R_A1A2 = float(input("R(A1 – A2)  [Ω]: "))
    R_B1B2 = float(input("R(B1 – B2)  [Ω]: "))
    R_A1B1 = float(input("R(A1 – B1)  [Ω]: "))
    R_A2B1 = float(input("R(A2 – B1)  [Ω]: "))
    R_A1B2 = float(input("R(A1 – B2)  [Ω]: "))

    r_A1, r_A2, r_B1, r_B2, R_rtd = solve_lead_resistances(
        R_A1A2, R_B1B2, R_A1B1, R_A2B1, R_A1B2
    )
    summarise(r_A1, r_A2, r_B1, r_B2, R_rtd)
