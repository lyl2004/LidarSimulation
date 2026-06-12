from __future__ import annotations

import numpy as np


def two_way_transmittance(range_m: np.ndarray, alpha_profile_m_inv: np.ndarray) -> np.ndarray:
    range_arr = np.asarray(range_m, dtype=float)
    alpha_arr = np.asarray(alpha_profile_m_inv, dtype=float)
    if range_arr.shape != alpha_arr.shape:
        raise ValueError("range_m and alpha_profile_m_inv must have the same shape")
    if len(range_arr) == 0:
        return np.asarray([], dtype=float)
    optical_depth = np.empty_like(range_arr, dtype=float)
    optical_depth[0] = alpha_arr[0] * range_arr[0]
    if len(range_arr) > 1:
        step = np.diff(range_arr)
        increments = 0.5 * (alpha_arr[1:] + alpha_arr[:-1]) * step
        optical_depth[1:] = optical_depth[0] + np.cumsum(increments)
    return np.exp(-2.0 * optical_depth)


def solve_power_from_profile(
    *,
    range_m: np.ndarray,
    alpha_profile_m_inv: np.ndarray,
    beta_profile_m_inv_sr: np.ndarray,
    system_constant: float,
    overlap: np.ndarray | float = 1.0,
) -> dict[str, np.ndarray]:
    range_arr = np.asarray(range_m, dtype=float)
    alpha_arr = np.asarray(alpha_profile_m_inv, dtype=float)
    beta_arr = np.asarray(beta_profile_m_inv_sr, dtype=float)
    overlap_arr = np.asarray(overlap, dtype=float)
    if overlap_arr.ndim == 0:
        overlap_arr = np.full_like(range_arr, float(overlap_arr), dtype=float)
    if not (range_arr.shape == alpha_arr.shape == beta_arr.shape == overlap_arr.shape):
        raise ValueError("profile arrays must have the same shape")

    trans = two_way_transmittance(range_arr, alpha_arr)
    r2 = np.maximum(range_arr**2, 1.0e-30)
    power_signal = float(system_constant) * overlap_arr * beta_arr * trans / r2
    return {
        "range_m": range_arr,
        "two_way_transmittance": trans,
        "power_signal_raw": power_signal,
    }
