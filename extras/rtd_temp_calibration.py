import numpy as np

def ln2_boiling_temp_C(P, P0=101325):
    """
    Compute LN2 boiling temperature (°C) from pressure.

    Parameters:
        P  : pressure in Pa (can be scalar or numpy array)
        P0 : reference pressure (default = 101325 Pa)

    Returns:
        Temperature in °C
    """

    # Constants for nitrogen
    T0 = 77.355  # K (boiling point at 1 atm)
    dH = 5.56e3  # J/mol (latent heat of vaporization)
    R = 8.314    # J/mol/K

    # Clausius-Clapeyron
    T = 1.0 / ( (1.0 / T0) - (R / dH) * np.log(P / P0) )

    return T - 273.15  # convert to °C

def compute_calibration(T_meas_1, T_true_1, T_meas_2, T_true_2):
    """
    Compute linear calibration coefficients:
        T_true = a * T_meas + b
    """
    a = (T_true_2 - T_true_1) / (T_meas_2 - T_meas_1)
    b = T_true_1 - a * T_meas_1
    return a, b


def apply_calibration(T_meas, a, b):
    """
    Apply calibration to array or scalar
    """
    return a * T_meas + b


if __name__ == "__main__":
    # --- USER INPUTS ---
    # Measured values from your RTD
    T_meas_ice = float(input("Measured temp in ice bath (°C): "))
    T_meas_ln2 = float(input("Measured temp in LN2 (°C): "))

    # True reference temperatures
    T_true_ice = 0.0
    T_true_ln2 = ln2_boiling_temp_C(101325)  # adjust if needed (e.g. pressure corrected)

    # --- CALIBRATION ---
    a, b = compute_calibration(T_meas_ice, T_true_ice,
                              T_meas_ln2, T_true_ln2)

    print("\nCalibration coefficients:")
    print(f"a (scale)  = {a}")
    print(f"b (offset) = {b}")

    print("\nCalibration equation:")
    print(f"T_corrected = {a:.6f} * T_measured + {b:.6f}")

    # --- EXAMPLE DATA APPLICATION ---
    # Replace with your actual data
    sample_data = np.array([T_meas_ice, T_meas_ln2, -50, -100])

    corrected = apply_calibration(sample_data, a, b)

    print("\nExample correction:")
    for t_raw, t_corr in zip(sample_data, corrected):
        print(f"{t_raw:8.3f} °C  ->  {t_corr:8.3f} °C")