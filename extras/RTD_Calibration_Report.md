# PT100 RTD Two-Point Calibration (Ice + Liquid Nitrogen)

*Generated 2026-07-15 from `New Thermometry Calibration/` by `rtd_calibration.py`.*

## 1. Summary

Three PT100 RTDs were immersed in a stirred ice bath (0 °C) and in saturated liquid nitrogen while a LabJack logged raw voltage, resistance, and the temperature it derives from the resistance. Using the **resistance** as the primary measurement, a two-point (gain + offset) correction is fit so that each RTD's average resistance in ice maps to the ideal PT100 value **100.000 Ω** and in LN to **20.337 Ω**. The correction is then propagated through the standard PT100 curve to show how the reported temperature shifts from 77 K up to room temperature.

| RTD | $R_{ice}$ (Ω) | $R_{LN}$ (Ω) | raw err @ice | raw err @LN | gain $m$ | offset $c$ (Ω) |
|----|----|----|----|----|----|----|
| RTD1 | 100.1160 $\pm$ 0.0054 | 20.5212 $\pm$ 0.0016 | +0.1160 Ω | +0.1842 Ω | 1.000857 | -0.20179 |
| RTD2 | 100.0840 $\pm$ 0.0060 | 20.4609 $\pm$ 0.0019 | +0.0840 Ω | +0.1239 Ω | 1.000501 | -0.13415 |
| RTD3 | 100.3129 $\pm$ 0.0095 | 20.4527 $\pm$ 0.0020 | +0.3129 Ω | +0.1157 Ω | 0.997531 | -0.06516 |

*Uncertainties are the standard error of the mean (SEM) of the resistance samples in each bath.* The equivalent statistical uncertainty on the anchor temperatures (SEM ÷ sensitivity) is ≤ 24 mK at the ice point and ≤ 5 mK at LN, so digits of $m$ and $c$ below the fifth decimal are not physically significant. The fit reproduces both anchors to < 10⁻¹² Ω by construction (closure verified numerically).

## 2. Reference curve and anchor points

The sensors are PT100 elements and the logger's temperature column matches the **IEC 60751** Callendar–Van Dusen (CVD) curve applied to the measured resistance (verified to < 0.018 K over every logged point). The CVD relation is

$$R(T)=R_0\left[1+A\,T+B\,T^2+\;(T<0)\;C\,(T-100)\,T^3\right],\quad T\ \text{in }^\circ\text{C}$$

with $R_0=100\,\Omega$, $A=3.9083\times10^{-3}$, $B=-5.775\times10^{-7}$, $C=-4.183\times10^{-12}$ (units of $^\circ$C$^{-1}$, $^\circ$C$^{-2}$, $^\circ$C$^{-4}$).

The two calibration baths give the ideal anchor resistances

* **Ice point**: $T=0\,^\circ$C $=273.15$ K $\Rightarrow R_{ideal}=100.000\,\Omega$ (exact by definition of PT100).
* **LN point**: saturated N$_2$ at 1 atm, $T=77.36$ K $(-195.79\,^\circ$C$)\Rightarrow R_{ideal}=20.337\,\Omega$.

Sensitivity is $dR/dT\approx0.391\,\Omega/$K at the ice point and $0.431\,\Omega/$K at LN. The LN boiling point depends on ambient pressure (≈ 0.012 K/mbar), so typical weather (±0.4 K, i.e. ±0.17 Ω at the LN anchor) is the dominant systematic. Because the correction is pinned at the ice point, this propagates to only **±0.047 K** in the corrected reading at room temperature; near LN it maps roughly one-to-one. Both are small compared with the raw offsets corrected below.

## 3. Measured resistances and stability

Each average is taken over the full immersion record. The raw scatter is a few mΩ (a few hundredths of a kelvin); no significant drift is seen within a bath, so the simple mean is a good estimate.

![fig1_timeseries.png](figures/fig1_timeseries.png)

All three RTDs read **high** in both baths (positive residuals), and the ice and LN residuals differ from each other — the error is not a pure series lead resistance but a combination of offset and scale, which is exactly what a two-point fit removes.

![fig2_anchor_error.png](figures/fig2_anchor_error.png)

## 4. Two-point resistance correction

A linear map $R_c=m\,R+c$ is fit through the two anchors for each RTD:

$$m=\frac{R_{ideal}^{ice}-R_{ideal}^{LN}}{R_{ice}-R_{LN}},\qquad c=R_{ideal}^{ice}-m\,R_{ice}.$$

* **RTD1:** $m=1.000857$, $c=-0.20179\,\Omega$
* **RTD2:** $m=1.000501$, $c=-0.13415\,\Omega$
* **RTD3:** $m=0.997531$, $c=-0.06516\,\Omega$

![fig3_correction_line.png](figures/fig3_correction_line.png)

## 5. How the temperature curves change (77 K → room temperature)

Modelling the raw error as linear in resistance (from the two anchors), a sensor truly at temperature $T$ presents $R_{meas}=(R_{ideal}(T)-c)/m$, so the **raw** logger reports $T_{raw}=\text{PT100}^{-1}(R_{meas})$ while the **calibrated** value returns the true temperature by construction. The curve below is the raw logger error, $T_{raw}-T_{true}$ — exactly the amount the calibration subtracts. Note it does **not** vanish at the anchor temperatures: the uncalibrated logger over-reads even in the ice bath and in LN (that is why a correction is needed at all). The over-reading is 0.2–0.9 K across 77 K → room temperature; after the two-point correction the residual is zero at both anchors (next figure).

![fig4_correction_curve.png](figures/fig4_correction_curve.png)

Correction at a few representative temperatures (raw − true, in K):

| RTD | 100 K | 150 K | 200 K | 250 K | 295 K |
|----|----|----|----|----|----|
| RTD1 | +0.416 | +0.386 | +0.351 | +0.314 | +0.280 |
| RTD2 | +0.281 | +0.265 | +0.246 | +0.225 | +0.205 |
| RTD3 | +0.330 | +0.466 | +0.602 | +0.738 | +0.860 |

A single fixed point (removing only the ice offset) would leave the gain error uncorrected and therefore a residual at the far end of the range. The two-point fit is the minimum that zeroes the error at **both** ends:

![fig6_range_curves.png](figures/fig6_range_curves.png)

Applied to the actual bath data, the correction pulls every RTD's mean onto the true bath temperature (ice → 273.15 K, LN → 77.36 K), while leaving the point-to-point scatter unchanged:

![fig5_before_after.png](figures/fig5_before_after.png)

## 6. How to apply the calibration

For each RTD, correct every logged resistance and re-derive temperature:

```python
# per-RTD coefficients (R in ohm):
CAL = {
    1: dict(m=1.000857, c=-0.201785),
    2: dict(m=1.000501, c=-0.134148),
    3: dict(m=0.997531, c=-0.065161),
}
R_corr = CAL[rtd]['m'] * R_meas + CAL[rtd]['c']
T_corr_K = pt100_inverse(R_corr) + 273.15   # standard IEC-60751 curve
```

## 7. Notes / caveats

* The ice and LN temperatures are taken as their 1-atm values; local pressure shifts the LN point by ≤ a few tenths of a kelvin (small compared with the offsets corrected here).
* Excitation current is stable at ≈ 198.5 µA (from $I=V/R$), consistent between baths, so self-heating is negligible.
* A two-point fit captures offset + gain. Any residual curvature of the PT100 element between 77 K and 273 K is not calibrated out; add a third fixed point (e.g. dry-ice or a stirred ethanol slush) if sub-0.1 K accuracy in the mid-range is required.
