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
A 4-wire PT100 has two terminals on the sensing element.  Lead labels:

    V = voltage-sense wire      I = current-carry wire
    + = positive terminal       - = negative terminal

    Terminal +                Terminal -
        │                         │
    ────┤V+ (r_Vp)   [PT100]  V- (r_Vm)├────
        │                         │
    ────┤I+ (r_Ip)            I- (r_Im)├────

  V+ and I+ are both connected to Terminal +.
  V- and I- are both connected to Terminal -.

MEASUREMENTS (DMM, 4-wire Kelvin recommended; RTD in circuit, power off)
─────────────────────────────────────────────────────────────────────────
Because V+ and I+ share Terminal +, measuring R(V+–I+) bypasses the RTD
element entirely — current goes wire V+ → Terminal + → wire I+.
Same for R(V-–I-).  This is the key advantage of 4-wire RTDs.

Make five 2-wire DMM measurements from the connector end:

  R_VpIp  = R between V+ and I+  =  r_Vp + r_Ip              (NO RTD in path)
  R_VmIm  = R between V- and I-  =  r_Vm + r_Im              (NO RTD in path)
  R_VpVm  = R between V+ and V-  =  r_Vp + R_rtd + r_Vm      (through RTD)
  R_IpVm  = R between I+ and V-  =  r_Ip + R_rtd + r_Vm      (through RTD)
  R_VpIm  = R between V+ and I-  =  r_Vp + R_rtd + r_Im      (through RTD)

  → R_rtd is NOT needed as input; it falls out of the solution.

SOLUTION (overdetermined, exact with these 5 measurements)
──────────────────────────────────────────────────────────
  r_Vp = [R_VpIp + (R_VpVm - R_IpVm)] / 2
  r_Ip = [R_VpIp - (R_VpVm - R_IpVm)] / 2
  r_Vm = [R_VmIm + (R_VpVm - R_VpIm)] / 2
  r_Im = [R_VmIm - (R_VpVm - R_VpIm)] / 2
  R_rtd = R_VpVm - r_Vp - r_Vm

3-WIRE CONNECTION TO PLC
─────────────────────────
Connect these three wires to the PLC's 3-wire RTD input:

  PLC pin | Wire  | Terminal
  --------|-------|----------
  1       | V+    | +  (voltage-sense lead)
  2       | I+    | +  (compensation lead)
  3       | V-    | -  (measurement return)

  Leave I- disconnected (or tie it to V- at the connector — tying it adds
  an extra parallel path and can introduce noise; better to leave floating).

  Wires V+ and I+ are the "compensation pair" — the PLC's 3-wire circuit
  uses them to subtract lead resistance assuming V+ ≈ I+.
  Wire V- carries the measurement return current.

LEAD CORRECTION (delta_R)
─────────────────────────
For the standard 3-wire bridge / current-source connection above:
  The PLC assumes V+ = I+ (equal paired leads).
  The residual measurement error in resistance is:

      delta_R  =  r_Ip - r_Vm

  This is the resistance the PLC "adds" to the RTD reading because of
  lead imbalance.  Pass this to rtd_cvd_calibration.py.

  Physical meaning:
    delta_R > 0 → instrument reads R too HIGH → temperature reads too HIGH
    delta_R < 0 → instrument reads R too LOW  → temperature reads too LOW

NOTE: The exact formula for delta_R depends on the PLC's internal circuit.
The expression (r_Ip - r_Vm) applies to the most common Wheatstone bridge
and current-source 3-wire configurations.  If you observe systematic offsets
larger than expected after applying this correction, verify the PLC wiring
against the PLC manual.
"""

import numpy as np


def solve_lead_resistances(R_VpIp, R_VmIm, R_VpVm, R_IpVm, R_VpIm):
    """
    Solve all four lead resistances and the RTD element resistance.

    Parameters
    ----------
    R_VpIp : float  — DMM R(V+–I+) in ohms  [no RTD in path]
    R_VmIm : float  — DMM R(V-–I-) in ohms  [no RTD in path]
    R_VpVm : float  — DMM R(V+–V-) in ohms  [through RTD]
    R_IpVm : float  — DMM R(I+–V-) in ohms  [through RTD]
    R_VpIm : float  — DMM R(V+–I-) in ohms  [through RTD]

    Returns
    -------
    r_Vp, r_Ip, r_Vm, r_Im : float  — individual lead resistances (Ω)
    R_rtd                  : float  — RTD element resistance at room temp (Ω)
    """
    dP = R_VpVm - R_IpVm   # = r_Vp - r_Ip
    dN = R_VpVm - R_VpIm   # = r_Vm - r_Im

    r_Vp = (R_VpIp + dP) / 2.0
    r_Ip = (R_VpIp - dP) / 2.0
    r_Vm = (R_VmIm + dN) / 2.0
    r_Im = (R_VmIm - dN) / 2.0

    R_rtd = R_VpVm - r_Vp - r_Vm

    return r_Vp, r_Ip, r_Vm, r_Im, R_rtd


def compute_delta_R(r_Ip, r_Vm):
    """
    Compute the lead-imbalance correction to pass to rtd_cvd_calibration.py.

    For the recommended 3-wire connection (V+, I+ paired; V- single):
        delta_R = r_Ip - r_Vm

    See module docstring for interpretation.
    """
    return r_Ip - r_Vm


def summarise(r_Vp, r_Ip, r_Vm, r_Im, R_rtd):
    """Print a human-readable measurement summary."""
    delta_R = compute_delta_R(r_Ip, r_Vm)

    print("\n─── Lead Wire Results ───────────────────────────────")
    print(f"  r_V+ (Terminal +, sense)   = {r_Vp*1e3:8.3f} mΩ")
    print(f"  r_I+ (Terminal +, current) = {r_Ip*1e3:8.3f} mΩ")
    print(f"  r_V- (Terminal -, sense)   = {r_Vm*1e3:8.3f} mΩ")
    print(f"  r_I- (Terminal -, current) = {r_Im*1e3:8.3f} mΩ")
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
    print("    PLC pin 1 → V+  (sense lead,        Terminal +)")
    print("    PLC pin 2 → I+  (compensation lead,  Terminal +)")
    print("    PLC pin 3 → V-  (measurement return, Terminal -)")
    print("    Leave I- disconnected.")
    print()
    print(f"  Lead imbalance:  r_V+ - r_I+ = {(r_Vp-r_Ip)*1e3:+.3f} mΩ")
    print(f"                   r_I+ - r_V- = {delta_R*1e3:+.3f} mΩ")
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
    print("  R(V+–I+) : across both + terminal wires   → r_V+ + r_I+  (no RTD)")
    print("  R(V-–I-) : across both - terminal wires   → r_V- + r_I-  (no RTD)")
    print("  R(V+–V-) : V+ to V-                       → r_V+ + Rrtd + r_V-")
    print("  R(I+–V-) : I+ to V-                       → r_I+ + Rrtd + r_V-")
    print("  R(V+–I-) : V+ to I-                       → r_V+ + Rrtd + r_I-")
    print()

    R_VpIp = float(input("R(V+ – I+)  [Ω]: "))
    R_VmIm = float(input("R(V- – I-)  [Ω]: "))
    R_VpVm = float(input("R(V+ – V-)  [Ω]: "))
    R_IpVm = float(input("R(I+ – V-)  [Ω]: "))
    R_VpIm = float(input("R(V+ – I-)  [Ω]: "))

    r_Vp, r_Ip, r_Vm, r_Im, R_rtd = solve_lead_resistances(
        R_VpIp, R_VmIm, R_VpVm, R_IpVm, R_VpIm
    )
    summarise(r_Vp, r_Ip, r_Vm, r_Im, R_rtd)
