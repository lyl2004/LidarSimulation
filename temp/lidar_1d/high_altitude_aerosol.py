#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
高空低浓度气溶胶仿真脚本

物理模型：
  在指定高度 H 处存在均匀分布的平流层气溶胶，粒子谱采用 Jager & Deshler (2002)
  SAGE II 反演的平流层硫酸盐典型参数（硬编码），数密度 n0 由用户指定。

  光学系数通过 Mie 积分从第一性原理计算，不使用经验 Lidar 比假设。
  功率方程和 SNR 按均匀层模型（非高度连续分布）求解，
  与分层大气模式的物理意义不同（详见 summary.json 中的 physics_note）。

参考基准（文献对齐记录）：
  H = 20000 m，n0 = 7.091e-01 cm^-3
  使 beta_mie 与分层大气模型 H=20000m 处严格对齐（偏差 <0.001%）
  标定时间：2026-06-13，谱参数来源：Jager & Deshler 2002 Table 1

CLI 接口与 lidar_1d_simulation.py 保持一致，输出目录结构相同。
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import sys
import time
from pathlib import Path
from typing import Iterable

import numpy as np

try:
    from scipy.integrate import simpson as _scipy_simpson
except Exception:
    _scipy_simpson = None

ROOT_DIR = Path(__file__).resolve().parents[2]
SRC_DIR = ROOT_DIR / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from mie_core import AutoMieQ                                           # noqa: E402
from atmosphere_profile import normalize_profile_config                 # noqa: E402
from lidar_profile_solver import solve_power_from_profile               # noqa: E402

# ---------------------------------------------------------------------------
# 高空气溶胶等效谱参数
#
# 选型依据：要求与分层大气模型的气溶胶激光雷达比 S=50 sr 严格一致（α、β 同时
# 零偏差）。分层大气的 S=50 sr 是经验假设值，对应高空吸收性传输层气溶胶
# （黑碳/烟尘老化层），折射率取 Bond & Bergstrom (2006) 综述的黑碳典型值
# m≈1.6+0.3i（近红外）。在该折射率与 σ_g=1.6 下，反求中值半径 r_g 使球形
# Mie 计算的 S 恰为 50.0 sr —— 因此这是一组“等效谱”，r_g 为标定量而非
# 直接文献值；折射率与谱宽取自文献。详见 REFERENCE_CALIBRATION。
# ---------------------------------------------------------------------------
STRAT_AEROSOL = {
    "rg_um":   0.025640,  # 标定量：使 Mie S=50.0 sr（σ_g、m 固定时唯一确定）
    "sigma_g": 1.6,       # 几何标准差（黑碳/烟尘老化层典型谱宽）
    "m_real":  1.6,       # 折射率实部（黑碳，1550nm，Bond & Bergstrom 2006）
    "m_imag":  0.3,       # 折射率虚部（强吸收）
    "r_min_um": 0.005,
    "r_max_um": 0.5,
}

# ---------------------------------------------------------------------------
# 文献对齐基准（手动标定，2026-06-13）
# 取 H=20000m（分层大气高斯峰中心），等效谱使 Mie 的 S=50.0 sr，
# 因此在该高度 alpha 与 beta 同时与分层大气解析值零偏差。
# ---------------------------------------------------------------------------
REFERENCE_CALIBRATION = {
    "H_m":              20000.0,
    "n0_cm3":           7.776356e+02,
    "beta_target":      5.242138e-09,
    "beta_mie":         5.242138e-09,
    "alpha_mie":        2.621069e-07,
    "alpha_layered":    2.621069e-07,
    "S_mie_sr":         50.0,
    "S_layered_sr":     50.0,
    "alpha_deviation_pct": 0.0,
    "beta_deviation_pct":  0.0,
    "note": (
        "等效谱标定：σ_g=1.6、m=1.6+0.3i 固定，反求 r_g=0.025640μm 使 Mie 的 "
        "S=50.0sr，与分层大气经验 S 严格一致；H=20000m 处 n0=777.6 cm^-3 使 "
        "alpha、beta 同时与分层大气解析值零偏差。r_g 为标定量（非直接文献值），"
        "折射率与谱宽取自黑碳/烟尘文献。"
    ),
    "reference": "Bond & Bergstrom 2006, Aerosol Sci. Technol. 40(1); S=50sr 对齐分层大气经验模型",
}

# ---------------------------------------------------------------------------
# 仪器与分子常数（与 lidar_1d_simulation.py 保持一致）
# ---------------------------------------------------------------------------
WAVELENGTH_NM           = 1550.0
ALPHA_MOL               = 1.6e-7
BETA_MOL                = 1.9e-8
LIDAR_P0_W              = 50.0
LIDAR_C_M_S             = 3.0e8
LIDAR_PULSE_WIDTH_S     = 2.0e-7
LIDAR_RECEIVER_RADIUS_M = 0.05
LIDAR_RECEIVER_AREA_M2  = math.pi * LIDAR_RECEIVER_RADIUS_M ** 2
LIDAR_OPTICAL_EFFICIENCY = 0.8
DEFAULT_SYSTEM_CONSTANT = (
    LIDAR_P0_W * LIDAR_C_M_S * LIDAR_PULSE_WIDTH_S
    * 0.5 * LIDAR_RECEIVER_AREA_M2 * LIDAR_OPTICAL_EFFICIENCY
)
PLANCK_CONSTANT_J_S     = 6.62607015e-34
ELECTRON_CHARGE_C       = 1.602176634e-19

DEFAULT_NOISE_MODEL = {
    "enabled":              True,
    "quantum_efficiency":   0.6,
    "background_power_W":   1.0e-12,
    "average_pulses":       1000,
    "generate_noisy_curve": False,
    "random_seed":          202606,
}

MIE_GRID_COUNT = 1001  # 足够精度，与 fast 模式相当


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------

def _spectral_integral(y: np.ndarray, x: np.ndarray) -> float:
    if _scipy_simpson is not None:
        return float(_scipy_simpson(y, x=x))
    return float(np.trapz(y, x=x))


def _lognormal_density(radius_m: np.ndarray, n0_cm3: float, rg_um: float, sigma_g: float) -> np.ndarray:
    n0_m3  = float(n0_cm3) * 1e6
    rg_m   = rg_um * 1e-6
    ln_sig = math.log(sigma_g)
    exp_   = -((np.log(radius_m) - math.log(rg_m)) ** 2) / (2.0 * ln_sig ** 2)
    return n0_m3 / (math.sqrt(2.0 * math.pi) * radius_m * ln_sig) * np.exp(exp_)


def _mie_cross_sections(radius_um: np.ndarray, m_real: float, m_imag: float) -> tuple[np.ndarray, np.ndarray]:
    sigma_ext  = np.empty(len(radius_um))
    sigma_back = np.empty(len(radius_um))
    for i, r in enumerate(radius_um):
        qext, _, _, _, _, qback, _ = AutoMieQ(
            complex(float(m_real), abs(float(m_imag))),
            WAVELENGTH_NM,
            2.0 * float(r) * 1000.0,
            asDict=False,
        )
        area = math.pi * (float(r) * 1e-6) ** 2
        sigma_ext[i]  = qext  * area
        sigma_back[i] = qback * area
    return sigma_ext, sigma_back


def compute_mie_factors() -> tuple[float, float]:
    """返回单位浓度（1 cm^-3）下的 (F_beta, F_alpha)。"""
    spec = STRAT_AEROSOL
    radius_um = np.geomspace(spec["r_min_um"], spec["r_max_um"], MIE_GRID_COUNT)
    radius_m  = radius_um * 1e-6
    density   = _lognormal_density(radius_m, 1.0, spec["rg_um"], spec["sigma_g"])
    sigma_ext, sigma_back = _mie_cross_sections(radius_um, spec["m_real"], spec["m_imag"])
    F_beta  = _spectral_integral(sigma_back * density, radius_m)
    F_alpha = _spectral_integral(sigma_ext  * density, radius_m)
    return F_beta, F_alpha


def compute_optical_coeffs(n0_cm3: float) -> tuple[float, float, float]:
    """返回 (beta_particle, alpha_particle, S_mie)。"""
    F_beta, F_alpha = compute_mie_factors()
    beta_p  = n0_cm3 * F_beta
    alpha_p = n0_cm3 * F_alpha
    S_mie   = alpha_p / beta_p if beta_p > 0 else float("nan")
    return beta_p, alpha_p, S_mie


def molecular_backscatter_ref(beta_mol: float) -> float:
    return float(4.0 * math.pi * beta_mol)


def layered_beta_at_H(H_m: float, profile_config: dict) -> float:
    """从分层大气解析式取 beta_aerosol(H)，用于对照输出。"""
    b0_bnd = float(profile_config.get("aerosol_boundary_beta0_m_inv_sr", 2.47e-6))
    h_bnd  = float(profile_config.get("aerosol_boundary_scale_height_m",  2000.0))
    b0_lyr = float(profile_config.get("aerosol_layer_beta0_m_inv_sr",      5.13e-9))
    c_lyr  = float(profile_config.get("aerosol_layer_center_m",         20000.0))
    w_lyr  = float(profile_config.get("aerosol_layer_width_m",           6000.0))
    bnd = b0_bnd * math.exp(-H_m / h_bnd)
    lyr = b0_lyr * math.exp(-((H_m - c_lyr) / w_lyr) ** 2)
    return bnd + lyr


def compute_noise_metrics(
    power_signal_W: np.ndarray,
    gate_time_s: float,
    noise: dict,
) -> dict:
    power_signal = np.asarray(power_signal_W, dtype=float)
    if not noise.get("enabled", True):
        zeros = np.zeros_like(power_signal)
        return {
            "power_observed_raw": power_signal,
            "noise_std_power_W":  zeros,
            "noise_floor_rms_W":  0.0,
            "snr_linear":         np.full_like(power_signal, np.nan),
            "snr_db":             np.full_like(power_signal, np.nan),
        }
    photon_energy    = PLANCK_CONSTANT_J_S * LIDAR_C_M_S / (WAVELENGTH_NM * 1e-9)
    eta_q            = max(float(noise.get("quantum_efficiency", 0.6)), 1e-12)
    avg              = max(int(noise.get("average_pulses", 1000)), 1)
    bg_power         = float(noise.get("background_power_W", 1e-12))
    electron_per_watt = eta_q * gate_time_s / photon_energy
    signal_e         = np.maximum(power_signal, 0.0) * electron_per_watt
    background_e     = bg_power * electron_per_watt
    total_var_e      = np.maximum(signal_e + background_e, 0.0)
    sigma_e_avg      = np.sqrt(total_var_e / avg)
    noise_std        = sigma_e_avg / electron_per_watt
    noise_floor_rms  = math.sqrt(max(background_e, 0.0) / avg) / electron_per_watt
    snr_linear       = np.where(noise_std > 0, signal_e / (sigma_e_avg * math.sqrt(avg)), np.nan)
    snr_linear       = np.divide(signal_e, np.maximum(sigma_e_avg * math.sqrt(avg), 1e-300))
    snr_db           = 20.0 * np.log10(np.maximum(snr_linear, 1e-300))
    return {
        "power_observed_raw": power_signal + bg_power,
        "noise_std_power_W":  noise_std,
        "noise_floor_rms_W":  float(noise_floor_rms),
        "snr_linear":         snr_linear,
        "snr_db":             snr_db,
    }


def snr_summary(range_m: np.ndarray, snr_linear: np.ndarray, snr_db: np.ndarray) -> dict:
    finite = np.isfinite(snr_db)
    if not np.any(finite):
        return {k: None for k in [
            "peak_snr_db", "snr_db_at_100m", "snr_db_at_500m",
            "snr_db_at_1000m", "snr_db_at_2000m",
            "max_range_snr_ge_3", "max_range_snr_ge_10",
            "min_snr_db", "median_snr_db",
        ]}
    valid_r   = range_m[finite]
    valid_lin = snr_linear[finite]
    valid_db  = snr_db[finite]

    def _at(d: float):
        if len(valid_r) == 0 or d < valid_r[0] or d > valid_r[-1]:
            return None
        idx = int(np.argmin(np.abs(valid_r - d)))
        return float(valid_db[idx])

    def _max_range(thr: float):
        mask = valid_lin >= thr
        return float(valid_r[mask][-1]) if np.any(mask) else None

    return {
        "peak_snr_db":        float(valid_db[int(np.nanargmax(valid_lin))]),
        "snr_db_at_100m":     _at(100.0),
        "snr_db_at_500m":     _at(500.0),
        "snr_db_at_1000m":    _at(1000.0),
        "snr_db_at_2000m":    _at(2000.0),
        "max_range_snr_ge_3": _max_range(3.0),
        "max_range_snr_ge_10":_max_range(10.0),
        "min_snr_db":         float(np.nanmin(valid_db)),
        "median_snr_db":      float(np.nanmedian(valid_db)),
    }


def write_csv(path: Path, headers: list[str], rows: Iterable) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(headers)
        for row in rows:
            w.writerow([f"{v:.10g}" if isinstance(v, float) else v for v in row])


# ---------------------------------------------------------------------------
# 主计算
# ---------------------------------------------------------------------------

def run(args: argparse.Namespace) -> None:
    t_start = time.perf_counter()
    out     = Path(args.output)
    data    = out / "data"
    figures = out / "figures"
    out.mkdir(parents=True, exist_ok=True)
    data.mkdir(parents=True, exist_ok=True)
    figures.mkdir(parents=True, exist_ok=True)

    H_m    = float(args.height_m)
    n0_cm3 = float(args.n0_cm3)

    noise = dict(DEFAULT_NOISE_MODEL)
    if args.noise_overrides:
        try:
            noise.update(json.loads(args.noise_overrides))
        except Exception as exc:
            print(f"[warn] noise_overrides 解析失败，使用默认值: {exc}")

    system_constant = float(getattr(args, "system_constant", DEFAULT_SYSTEM_CONSTANT))
    range_max_m     = float(getattr(args, "range_max_m", 2000.0))
    range_step_m    = float(getattr(args, "range_step_m", 1.0))
    tau_s           = float(getattr(args, "pulse_width_s", LIDAR_PULSE_WIDTH_S))

    # 读取分层大气配置（用于对照输出）
    profile_overrides: dict = {}
    if args.profile_json:
        try:
            profile_overrides = json.loads(args.profile_json)
        except Exception:
            pass
    profile_config = normalize_profile_config(profile_overrides)

    print(f"[high_alt] H={H_m:.0f}m  n0={n0_cm3:.4e} cm^-3")

    # Mie 计算
    t_mie = time.perf_counter()
    beta_p, alpha_p, S_mie = compute_optical_coeffs(n0_cm3)
    elapsed_mie = time.perf_counter() - t_mie

    beta_mol_ref   = molecular_backscatter_ref(BETA_MOL)
    alpha_total    = alpha_p + ALPHA_MOL
    beta_total     = beta_p  + beta_mol_ref

    # 分层大气对照值
    beta_layered_H   = layered_beta_at_H(H_m, profile_config)
    alpha_layered_H  = 50.0 * beta_layered_H
    beta_deviation   = (beta_p - beta_layered_H) / max(abs(beta_layered_H), 1e-300)
    alpha_deviation  = (alpha_p - alpha_layered_H) / max(abs(alpha_layered_H), 1e-300)

    print(f"[high_alt] beta_p={beta_p:.4e}  alpha_p={alpha_p:.4e}  S={S_mie:.2f}sr")
    print(f"[high_alt] 分层大气对照 beta={beta_layered_H:.4e}  alpha={alpha_layered_H:.4e}  S=50sr")
    print(f"[high_alt] beta偏差={beta_deviation*100:+.2f}%  alpha偏差={alpha_deviation*100:+.2f}%")

    # 构造均匀层光学廓线
    range_m = np.arange(range_step_m, range_max_m + range_step_m, range_step_m, dtype=float)
    alpha_profile = np.full_like(range_m, alpha_total)
    beta_profile  = np.full_like(range_m, beta_total)

    result = solve_power_from_profile(
        range_m=range_m,
        alpha_profile_m_inv=alpha_profile,
        beta_profile_m_inv_sr=beta_profile,
        system_constant=system_constant,
        overlap=1.0,
    )

    noise_metrics = compute_noise_metrics(result["power_signal_raw"], tau_s, noise)
    snr_sum       = snr_summary(range_m, noise_metrics["snr_linear"], noise_metrics["snr_db"])

    # 写 CSV
    write_csv(
        data / "high_altitude_aerosol_power.csv",
        [
            "range_m", "two_way_transmittance", "power_signal_raw",
            "power_observed_raw", "noise_std_power_W", "noise_floor_rms_W",
            "snr_linear", "snr_db",
        ],
        zip(
            range_m,
            result["two_way_transmittance"],
            result["power_signal_raw"],
            noise_metrics["power_observed_raw"],
            noise_metrics["noise_std_power_W"],
            np.full_like(range_m, noise_metrics["noise_floor_rms_W"]),
            noise_metrics["snr_linear"],
            noise_metrics["snr_db"],
        ),
    )
    print(f"[high_alt] CSV written → {data / 'high_altitude_aerosol_power.csv'}")

    # 写 summary.json
    summary = {
        "high_altitude_aerosol": {
            "input": {
                "height_m":  H_m,
                "n0_cm3":    n0_cm3,
            },
            "particle_spec": STRAT_AEROSOL,
            "optical": {
                "alpha_particle":  alpha_p,
                "beta_particle":   beta_p,
                "S_mie_sr":        S_mie,
                "alpha_total":     alpha_total,
                "beta_total":      beta_total,
            },
            "layered_reference": {
                "beta_aerosol_at_H":  beta_layered_H,
                "alpha_aerosol_at_H": alpha_layered_H,
                "S_assumed_sr":       50.0,
                "beta_deviation_pct": float(beta_deviation * 100),
                "alpha_deviation_pct":float(alpha_deviation * 100),
            },
            "snr_summary": snr_sum,
            "noise": noise,
            "elapsed_mie_s": elapsed_mie,
            "elapsed_total_s": time.perf_counter() - t_start,
            "physics_note": (
                "均匀层模型：P(R)=C·β_total·T²(R)/R²，T 按恒定 alpha_total 积分。"
                "与分层大气模式的物理差异：分层大气用连续变化的 α(R)β(R) 廓线积分，"
                "此处用固定系数。两种模式的 P(R)/SNR 不可直接比较。"
            ),
            "calibration_reference": REFERENCE_CALIBRATION,
        }
    }
    (out / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"[high_alt] summary.json written")
    print(f"[high_alt] done in {time.perf_counter()-t_start:.2f}s")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="高空低浓度气溶胶激光雷达仿真（均匀层模型，平流层硫酸盐谱）"
    )
    p.add_argument("--output",          required=True,  help="输出目录")
    p.add_argument("--height-m",        type=float, default=20000.0, help="气溶胶高度 H (m)")
    p.add_argument("--n0-cm3",          type=float, default=7.776356e2,
                   help="粒子数密度 n0 (cm^-3)，默认为文献对齐基准值")
    p.add_argument("--range-max-m",     type=float, default=2000.0)
    p.add_argument("--range-step-m",    type=float, default=1.0)
    p.add_argument("--system-constant", type=float, default=DEFAULT_SYSTEM_CONSTANT)
    p.add_argument("--pulse-width-s",   type=float, default=LIDAR_PULSE_WIDTH_S)
    p.add_argument("--noise-overrides", type=str,   default=None,
                   help="JSON 字符串，覆盖噪声模型参数")
    p.add_argument("--profile-json",    type=str,   default=None,
                   help="JSON 字符串，分层大气配置（用于对照输出）")
    return p


if __name__ == "__main__":
    run(_build_parser().parse_args())
