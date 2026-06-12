from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np


DEFAULT_PROFILE = {
    "mode": "uniform",
    "range_max_m": 40000.0,
    "range_step_m": 30.0,
    "molecular_scale_height_m": 7000.0,
    "molecular_beta0_m_inv_sr": 1.54e-6,
    "molecular_reference_wavelength_nm": 532.0,
    "aerosol_boundary_beta0_m_inv_sr": 2.47e-6,
    "aerosol_boundary_scale_height_m": 2000.0,
    "aerosol_layer_beta0_m_inv_sr": 5.13e-9,
    "aerosol_layer_center_m": 20000.0,
    "aerosol_layer_width_m": 6000.0,
    "aerosol_lidar_ratio_sr": 50.0,
    "molecular_lidar_ratio_sr": 8.0 * math.pi / 3.0,
}

@dataclass(frozen=True)
class OpticalProfile:
    range_m: np.ndarray
    alpha_total_m_inv: np.ndarray
    beta_total_m_inv_sr: np.ndarray
    alpha_molecular_m_inv: np.ndarray
    beta_molecular_m_inv_sr: np.ndarray
    alpha_aerosol_m_inv: np.ndarray
    beta_aerosol_m_inv_sr: np.ndarray
    meta: dict[str, object] = field(default_factory=dict)


def normalize_profile_config(
    profile: dict | None,
    *,
    range_max_m: float | None = None,
    range_step_m: float | None = None,
) -> dict[str, float | str]:
    raw = dict(DEFAULT_PROFILE)
    if isinstance(profile, dict):
        for key, value in profile.items():
            raw[key] = value
    if range_max_m is not None:
        raw["range_max_m"] = float(range_max_m)
    if range_step_m is not None:
        raw["range_step_m"] = float(range_step_m)

    normalized: dict[str, float | str] = {}
    normalized["mode"] = str(raw.get("mode", "uniform") or "uniform").strip().lower()
    for key in DEFAULT_PROFILE:
        if key == "mode":
            continue
        normalized[key] = float(raw.get(key, DEFAULT_PROFILE[key]))
    if normalized["range_step_m"] <= 0.0:
        normalized["range_step_m"] = float(DEFAULT_PROFILE["range_step_m"])
    if normalized["range_max_m"] <= 0.0:
        normalized["range_max_m"] = float(DEFAULT_PROFILE["range_max_m"])
    if normalized["molecular_scale_height_m"] <= 0.0:
        normalized["molecular_scale_height_m"] = float(DEFAULT_PROFILE["molecular_scale_height_m"])
    if normalized["aerosol_boundary_scale_height_m"] <= 0.0:
        normalized["aerosol_boundary_scale_height_m"] = float(DEFAULT_PROFILE["aerosol_boundary_scale_height_m"])
    if normalized["aerosol_layer_width_m"] <= 0.0:
        normalized["aerosol_layer_width_m"] = float(DEFAULT_PROFILE["aerosol_layer_width_m"])
    return normalized


def build_range_grid(config: dict[str, float | str]) -> np.ndarray:
    range_max_m = float(config["range_max_m"])
    range_step_m = float(config["range_step_m"])
    return np.arange(range_step_m, range_max_m + range_step_m, range_step_m, dtype=float)


def build_uniform_profile(
    range_m: np.ndarray,
    *,
    alpha_total: float,
    beta_total: float,
    alpha_molecular: float,
    beta_molecular: float,
    label: str = "uniform",
) -> OpticalProfile:
    range_arr = np.asarray(range_m, dtype=float)
    alpha_total_arr = np.full_like(range_arr, float(alpha_total), dtype=float)
    beta_total_arr = np.full_like(range_arr, float(beta_total), dtype=float)
    alpha_molecular_arr = np.full_like(range_arr, float(alpha_molecular), dtype=float)
    beta_molecular_arr = np.full_like(range_arr, float(beta_molecular), dtype=float)
    alpha_aerosol_arr = np.maximum(alpha_total_arr - alpha_molecular_arr, 0.0)
    beta_aerosol_arr = np.maximum(beta_total_arr - beta_molecular_arr, 0.0)
    return OpticalProfile(
        range_m=range_arr,
        alpha_total_m_inv=alpha_total_arr,
        beta_total_m_inv_sr=beta_total_arr,
        alpha_molecular_m_inv=alpha_molecular_arr,
        beta_molecular_m_inv_sr=beta_molecular_arr,
        alpha_aerosol_m_inv=alpha_aerosol_arr,
        beta_aerosol_m_inv_sr=beta_aerosol_arr,
        meta={"mode": label},
    )


def build_ideal_layered_profile(
    *,
    wavelength_nm: float,
    profile: dict | None = None,
    range_max_m: float | None = None,
    range_step_m: float | None = None,
) -> OpticalProfile:
    config = normalize_profile_config(profile, range_max_m=range_max_m, range_step_m=range_step_m)
    range_m = build_range_grid(config)

    molecular_beta0 = float(config["molecular_beta0_m_inv_sr"])
    molecular_scale_height = float(config["molecular_scale_height_m"])
    molecular_ref_nm = float(config["molecular_reference_wavelength_nm"])
    molecular_lidar_ratio = float(config["molecular_lidar_ratio_sr"])

    beta_molecular = (
        molecular_beta0
        * np.exp(-range_m / molecular_scale_height)
        * (molecular_ref_nm / float(wavelength_nm)) ** 4
    )
    alpha_molecular = molecular_lidar_ratio * beta_molecular

    aerosol_boundary = float(config["aerosol_boundary_beta0_m_inv_sr"]) * np.exp(
        -range_m / float(config["aerosol_boundary_scale_height_m"])
    )
    aerosol_layer = float(config["aerosol_layer_beta0_m_inv_sr"]) * np.exp(
        -((range_m - float(config["aerosol_layer_center_m"])) / float(config["aerosol_layer_width_m"])) ** 2
    )
    beta_aerosol = aerosol_boundary + aerosol_layer
    alpha_aerosol = float(config["aerosol_lidar_ratio_sr"]) * beta_aerosol

    return OpticalProfile(
        range_m=range_m,
        alpha_total_m_inv=alpha_molecular + alpha_aerosol,
        beta_total_m_inv_sr=beta_molecular + beta_aerosol,
        alpha_molecular_m_inv=alpha_molecular,
        beta_molecular_m_inv_sr=beta_molecular,
        alpha_aerosol_m_inv=alpha_aerosol,
        beta_aerosol_m_inv_sr=beta_aerosol,
        meta={
            "mode": "ideal_layered",
            "config": config,
            "wavelength_nm": float(wavelength_nm),
        },
    )
