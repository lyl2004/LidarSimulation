#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Independent 1D lidar simulation for the packaged 1D workflow.

The core signal model is:
    P(R) = C * O(R) * beta * exp(-2 * alpha * R) / R^2

For the local figure workflow, the plotted power curves are range-gate
received-equation quantities. Plot labels are localized in Chinese.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass
from functools import lru_cache
from pathlib import Path
from typing import Iterable

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

matplotlib.rcParams["font.sans-serif"] = [
    "Microsoft YaHei",
    "SimHei",
    "Noto Sans CJK SC",
    "Arial Unicode MS",
    "DejaVu Sans",
]
matplotlib.rcParams["axes.unicode_minus"] = False

try:
    from scipy.integrate import simpson as _scipy_simpson
except Exception:  # pragma: no cover - scipy is available in the project env.
    _scipy_simpson = None


ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from diagnostics import get_or_create_session, new_component_logger  # noqa: E402
from mie_core import AutoMieQ, mie_effective_polarized  # noqa: E402
from path_resolver import resolve_julia_executable as _resolve_julia_unified, get_root as _get_root, get_julia_depot_path as _get_julia_depot_path  # noqa: E402
import cache_keys as _cache_keys  # noqa: E402
import cache_runtime as _cache_runtime  # noqa: E402

ROOT = _get_root()
_DIAG_SESSION = get_or_create_session("simulation_1d")
_DIAG_LOGGER = new_component_logger(_DIAG_SESSION, "simulation_1d")

CACHE_STORE_ROOT = _cache_runtime.LAYOUT.cache_store_root
CACHE_RUNTIME_ROOT = _cache_runtime.LAYOUT.cache_runtime_root
CACHE_SEED_ROOT = _cache_runtime.LAYOUT.cache_seed_root
CACHE_INDEX_ROOT = _cache_runtime.LAYOUT.cache_index_root
VISIBLE_DEFAULT_RESULT_ROOT = _cache_runtime.LAYOUT.default_result_root


def _precision_profile(args: argparse.Namespace) -> str:
    value = str(getattr(args, "precision_profile", "high") or "high").strip().lower()
    return value if value in {"fast", "medium", "high"} else "high"


def _cache_tier_root(source: str, precision: str, *parts: str) -> Path:
    return _cache_runtime.cache_tier_root(source, precision, *parts)


def _cache_index_path(layer: str) -> Path:
    return _cache_runtime.cache_index_path(layer)


def _load_cache_index(layer: str) -> dict:
    return _cache_runtime.load_cache_index(layer)


def _save_cache_index(layer: str, data: dict) -> None:
    _cache_runtime.save_cache_index(layer, data)


def _upsert_cache_index(layer: str, record: dict[str, object]) -> None:
    _cache_runtime.upsert_cache_index(layer, record)


def _record_cache_artifact(layer: str, *, precision: str, semantic_key: str, artifact_path: Path, source: str, visibility: str, input_hashes: dict[str, str] | None = None, extra: dict[str, object] | None = None) -> None:
    _cache_runtime.record_cache_artifact(
        layer,
        precision=precision,
        semantic_key=semantic_key,
        artifact_path=artifact_path,
        source=source,
        visibility=visibility,
        input_hashes=input_hashes,
        extra=extra,
    )


def _precision_manifest_path() -> Path:
    return CACHE_STORE_ROOT / "manifest.json"


def _load_cache_manifest() -> dict:
    path = _precision_manifest_path()
    if not path.exists():
        return {"precisions": {}, "visible_default": "high"}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"precisions": {}, "visible_default": "high"}
    if not isinstance(data, dict):
        return {"precisions": {}, "visible_default": "high"}
    data.setdefault("precisions", {})
    data.setdefault("visible_default", "high")
    return data


def _save_cache_manifest(data: dict) -> None:
    CACHE_STORE_ROOT.mkdir(parents=True, exist_ok=True)
    _precision_manifest_path().write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _diag_event(stage: str, *, status: str = "ok", elapsed_ms: float | None = None, payload: dict | None = None) -> None:
    _DIAG_LOGGER.event(stage, status=status, elapsed_ms=elapsed_ms, payload=payload)
    _DIAG_SESSION.write_component_event("compute_timeline.jsonl", stage, status=status, elapsed_ms=elapsed_ms, payload=payload)


WAVELENGTH_NM = 1550.0
ALPHA_MOL = 1.6e-7
BETA_MOL = 1.9e-8
LIDAR_P0_W = 50.0
LIDAR_C_M_S = 3.0e8
LIDAR_PULSE_WIDTH_S = 2.0e-7
LIDAR_RECEIVER_RADIUS_M = 0.05
LIDAR_RECEIVER_AREA_M2 = math.pi * LIDAR_RECEIVER_RADIUS_M**2
LIDAR_OPTICAL_EFFICIENCY = 0.8
DEFAULT_SYSTEM_CONSTANT = (
    LIDAR_P0_W
    * LIDAR_C_M_S
    * LIDAR_PULSE_WIDTH_S
    * 0.5
    * LIDAR_RECEIVER_AREA_M2
    * LIDAR_OPTICAL_EFFICIENCY
)
LIDAR_PULSE_RANGE_RESOLUTION_M = LIDAR_C_M_S * LIDAR_PULSE_WIDTH_S * 0.5
TRAPZ = getattr(np, "trapezoid", np.trapz)
PLANCK_CONSTANT_J_S = 6.62607015e-34
ELECTRON_CHARGE_C = 1.602176634e-19

DEFAULT_NOISE_MODEL = {
    "enabled": True,
    "quantum_efficiency": 0.6,
    "background_power_W": 1.0e-12,
    "dark_current_A": 1.0e-9,
    "read_noise_e": 10.0,
    "average_pulses": 1000,
    "generate_noisy_curve": False,
    "random_seed": 202606,
}

CN_SCENARIO_TITLES = {
    "radiation_fog": "辐射雾",
    "advection_fog": "平流雾",
    "urban_industrial_haze": "城市/工业型霾",
    "rural_continental_haze": "乡村/大陆背景型霾",
    "dust_desert_haze": "沙尘型霾",
    "maritime_haze": "海洋性霾",
    "light_rain": "小雨",
    "moderate_rain": "中雨",
    "heavy_rain": "大雨",
}


def cn_scenario_title(key: str, fallback: str) -> str:
    return CN_SCENARIO_TITLES.get(key, fallback)


def molecular_backscatter_reference(beta_mol_input: float) -> float:
    """Convert standard molecular beta [m^-1 sr^-1] to the 1D qback reference."""
    return float(4.0 * math.pi * float(beta_mol_input))


def spectral_integral(y: np.ndarray, x: np.ndarray) -> float:
    """Integrate particle spectra for PDF Eq. (7)/(8).

    The script uses odd grid sizes, so composite Simpson is the preferred
    quadrature. It handles the nonuniform log-radius grids used by fog/haze and
    reduces bias in the oscillatory rain backscatter integral. Fall back to
    trapezoidal integration only if SciPy is unavailable.
    """
    if _scipy_simpson is not None and len(x) >= 3:
        return float(_scipy_simpson(y, x=x))
    return float(TRAPZ(y, x))


@dataclass(frozen=True)
class ModeSpec:
    name: str
    n0_cm3: float
    rg_um: float
    sigma_g: float
    m_real: float
    m_imag: float
    depol_model: float
    use_tmatrix: bool = False
    shape_type: str = "sphere"
    axis_ratio: float = 1.0


@dataclass(frozen=True)
class FogSpec:
    key: str
    title: str
    n0_cm3: float
    rg_um: float
    sigma_g: float
    r_min_um: float = 0.2
    r_max_um: float = 40.0
    m_real: float = 1.314
    m_imag: float = 1.0e-4


@dataclass(frozen=True)
class HazeSpec:
    key: str
    title: str
    modes: tuple[ModeSpec, ...]
    r_min_um: float = 0.01
    r_max_um: float = 10.0


@dataclass(frozen=True)
class RainSpec:
    key: str
    title: str
    rain_rate_mm_h: float
    r_min_um: float = 50.0
    r_max_um: float = 3000.0
    m_real: float = 1.314
    m_imag: float = 1.0e-4


@dataclass
class OpticalSummary:
    key: str
    alpha_particle: float
    beta_particle: float
    alpha_total: float
    beta_total: float
    alpha_rel_grid_error: float
    beta_rel_grid_error: float
    depol_ratio: float | None
    elapsed_s: float
    particle_volume_depol_ratio: float | None = None
    total_volume_depol_ratio: float | None = None
    legacy_weighted_depol_ratio: float | None = None
    beta_parallel_particle: float | None = None
    beta_perpendicular_particle: float | None = None
    beta_parallel_molecular: float | None = None
    beta_perpendicular_molecular: float | None = None
    beta_parallel_total: float | None = None
    beta_perpendicular_total: float | None = None


@dataclass(frozen=True)
class NoiseModel:
    enabled: bool = True
    quantum_efficiency: float = 0.6
    background_power_W: float = 1.0e-12
    dark_current_A: float = 1.0e-9
    read_noise_e: float = 10.0
    average_pulses: int = 1000
    generate_noisy_curve: bool = False
    random_seed: int | None = 202606


def _optical_summary_from_cache(cached: dict) -> "OpticalSummary":
    """从缓存 dict 安全恢复 OpticalSummary，缺失字段用默认值填充。"""
    fields = OpticalSummary.__dataclass_fields__
    defaults = {
        "depol_ratio": None,
        "particle_volume_depol_ratio": None,
        "total_volume_depol_ratio": None,
        "legacy_weighted_depol_ratio": None,
        "beta_parallel_particle": None,
        "beta_perpendicular_particle": None,
        "beta_parallel_molecular": None,
        "beta_perpendicular_molecular": None,
        "beta_parallel_total": None,
        "beta_perpendicular_total": None,
    }
    kwargs = {}
    for k in fields:
        if k in cached:
            kwargs[k] = cached[k]
        elif k in defaults:
            kwargs[k] = defaults[k]
        else:
            raise KeyError(f"缓存缺少必填字段 '{k}'，请删除缓存后重新运行")
    return OpticalSummary(**kwargs)


@dataclass
class MuellerLibrary:
    """Effective angular Mueller library for one haze scenario."""

    angles_deg: np.ndarray
    M11: np.ndarray
    M12: np.ndarray
    M33: np.ndarray
    M34: np.ndarray
    sigma_sca: float
    sigma_back_ref: float
    depol_back: float
    omega_eff: float
    source: str


def ensure_odd_grid_count(n: int) -> int:
    n = max(int(n), 101)
    return n if n % 2 == 1 else n + 1


def cm3_to_m3(value_cm3: float) -> float:
    return float(value_cm3) * 1.0e6


def generate_lidar_angle_grid(
    num_total: int = 1200,
    forward_res_deg: float = 0.01,
    forward_max_deg: float = 2.0,
    back_res_deg: float = 0.02,
    back_min_deg: float = 175.0,
) -> np.ndarray:
    """Dense angle grid for lidar propagation, refined near 0 and 180 degrees."""
    num_total = max(int(num_total), 181)
    forward = np.arange(0.0, max(forward_max_deg, forward_res_deg) + forward_res_deg, forward_res_deg)
    back = np.arange(max(back_min_deg, 0.0), 180.0 + back_res_deg, back_res_deg)
    middle_count = max(2, num_total - len(forward) - len(back))
    middle = np.linspace(float(forward[-1]), float(back[0]), middle_count)
    angles = np.concatenate([forward, middle[1:-1], back])
    return np.unique(np.clip(angles, 0.0, 180.0))


def lognormal_number_density_per_m(
    r_m: np.ndarray,
    n0_cm3: float,
    rg_um: float,
    sigma_g: float,
) -> np.ndarray:
    """Return n(r) in m^-4 for integration over radius in meters."""
    n0_m3 = cm3_to_m3(n0_cm3)
    rg_m = rg_um * 1.0e-6
    ln_sigma = math.log(sigma_g)
    exponent = -((np.log(r_m) - math.log(rg_m)) ** 2) / (2.0 * ln_sigma**2)
    return n0_m3 / (math.sqrt(2.0 * math.pi) * r_m * ln_sigma) * np.exp(exponent)


@lru_cache(maxsize=None)
def mie_efficiencies(radius_um_rounded: float, m_real: float, m_imag: float) -> tuple[float, float]:
    """Return Qext and Qback from the project Mie kernel.

    The scenario tables store a positive absorption coefficient k. PyMieScatt
    uses the n + i*k convention for absorbing media; the report may still list
    PDF-style m = n - i*k, but the numerical library receives +i*k.
    """
    radius_um = float(radius_um_rounded)
    m_imag_abs = abs(float(m_imag))
    diameter_nm = 2.0 * radius_um * 1000.0
    qext, _qsca, _qabs, _g, _qpr, qback, _qratio = AutoMieQ(
        complex(float(m_real), m_imag_abs),
        WAVELENGTH_NM,
        diameter_nm,
        asDict=False,
    )
    return float(qext), float(qback)


def cross_sections_for_grid(
    radius_um: np.ndarray,
    m_real: float,
    m_imag: float,
) -> tuple[np.ndarray, np.ndarray]:
    radius_um = np.asarray(radius_um, dtype=float)
    m_imag_abs = abs(float(m_imag))
    sigma_ext = np.empty_like(radius_um, dtype=float)
    sigma_back = np.empty_like(radius_um, dtype=float)
    for i, r_um in enumerate(radius_um):
        qext, qback = mie_efficiencies(round(float(r_um), 8), float(m_real), m_imag_abs)
        area = math.pi * (float(r_um) * 1.0e-6) ** 2
        sigma_ext[i] = qext * area
        # The PDF's Eq. (8) uses Qback*pi*r^2 directly for beta.
        sigma_back[i] = qback * area
    return sigma_ext, sigma_back


def relative_error(full_value: float, coarse_value: float) -> float:
    denom = max(abs(full_value), 1.0e-300)
    return abs(full_value - coarse_value) / denom


def grid_precision_warnings(summary: OpticalSummary, alpha_tol: float, beta_tol: float) -> list[str]:
    warnings: list[str] = []
    if summary.alpha_rel_grid_error > alpha_tol:
        warnings.append(
            f"alpha half-grid relative change {summary.alpha_rel_grid_error:.3g} exceeds {alpha_tol:.3g}"
        )
    if summary.beta_rel_grid_error > beta_tol:
        warnings.append(
            f"beta half-grid relative change {summary.beta_rel_grid_error:.3g} exceeds {beta_tol:.3g}"
        )
    return warnings


def split_backscatter_channels(beta: float, depol_ratio: float) -> tuple[float, float]:
    delta = float(np.clip(depol_ratio, 0.0, 1.0))
    beta_parallel = float(beta) / (1.0 + delta)
    beta_perpendicular = beta_parallel * delta
    return beta_parallel, beta_perpendicular


def channel_depolarization(beta_parallel: float, beta_perpendicular: float) -> float:
    return float(beta_perpendicular) / max(float(beta_parallel), 1.0e-300)


def add_channel_fields(detail: dict[str, object], beta: float, depol_ratio: float) -> tuple[float, float]:
    beta_parallel, beta_perpendicular = split_backscatter_channels(beta, depol_ratio)
    detail["linear_depolarization_ratio"] = float(np.clip(depol_ratio, 0.0, 1.0))
    detail["beta_parallel_particle"] = beta_parallel
    detail["beta_perpendicular_particle"] = beta_perpendicular
    return beta_parallel, beta_perpendicular


def build_haze_optical_summary(
    key: str,
    alpha_particle: float,
    beta_particle: float,
    alpha_rel_grid_error: float,
    beta_rel_grid_error: float,
    elapsed_s: float,
    beta_parallel_particle: float,
    beta_perpendicular_particle: float,
    legacy_weighted_depol_ratio: float,
    molecular_depol_ratio: float,
) -> OpticalSummary:
    beta_mol_ref = molecular_backscatter_reference(BETA_MOL)
    beta_parallel_mol, beta_perpendicular_mol = split_backscatter_channels(beta_mol_ref, molecular_depol_ratio)
    beta_parallel_total = beta_parallel_particle + beta_parallel_mol
    beta_perpendicular_total = beta_perpendicular_particle + beta_perpendicular_mol
    particle_volume_depol = channel_depolarization(beta_parallel_particle, beta_perpendicular_particle)
    total_volume_depol = channel_depolarization(beta_parallel_total, beta_perpendicular_total)
    return OpticalSummary(
        key=key,
        alpha_particle=alpha_particle,
        beta_particle=beta_particle,
        alpha_total=alpha_particle + ALPHA_MOL,
        beta_total=beta_particle + beta_mol_ref,
        alpha_rel_grid_error=alpha_rel_grid_error,
        beta_rel_grid_error=beta_rel_grid_error,
        depol_ratio=total_volume_depol,
        elapsed_s=elapsed_s,
        particle_volume_depol_ratio=particle_volume_depol,
        total_volume_depol_ratio=total_volume_depol,
        legacy_weighted_depol_ratio=float(np.clip(legacy_weighted_depol_ratio, 0.0, 1.0)),
        beta_parallel_particle=beta_parallel_particle,
        beta_perpendicular_particle=beta_perpendicular_particle,
        beta_parallel_molecular=beta_parallel_mol,
        beta_perpendicular_molecular=beta_perpendicular_mol,
        beta_parallel_total=beta_parallel_total,
        beta_perpendicular_total=beta_perpendicular_total,
    )


def integrate_lognormal_modes(
    modes: Iterable[ModeSpec],
    r_min_um: float,
    r_max_um: float,
    grid_count: int,
) -> tuple[float, float, float, float, dict[str, dict[str, float]]]:
    radius_um = np.geomspace(r_min_um, r_max_um, ensure_odd_grid_count(grid_count))
    radius_m = radius_um * 1.0e-6
    alpha_total = 0.0
    beta_total = 0.0
    alpha_coarse = 0.0
    beta_coarse = 0.0
    mode_details: dict[str, dict[str, float]] = {}

    for mode in modes:
        density = lognormal_number_density_per_m(radius_m, mode.n0_cm3, mode.rg_um, mode.sigma_g)
        sigma_ext, sigma_back = cross_sections_for_grid(radius_um, mode.m_real, mode.m_imag)
        alpha = spectral_integral(sigma_ext * density, radius_m)
        beta = spectral_integral(sigma_back * density, radius_m)
        alpha_half = spectral_integral((sigma_ext * density)[::2], radius_m[::2])
        beta_half = spectral_integral((sigma_back * density)[::2], radius_m[::2])
        alpha_total += alpha
        beta_total += beta
        alpha_coarse += alpha_half
        beta_coarse += beta_half
        mode_details[mode.name] = {
            "alpha_particle": alpha,
            "beta_particle": beta,
            "alpha_rel_grid_error": relative_error(alpha, alpha_half),
            "beta_rel_grid_error": relative_error(beta, beta_half),
            "depol_model": mode.depol_model,
        }

    return alpha_total, beta_total, alpha_coarse, beta_coarse, mode_details


def compute_fog(spec: FogSpec, grid_count: int) -> tuple[OpticalSummary, dict[str, dict[str, float]]]:
    t0 = time.perf_counter()
    mode = ModeSpec(
        name=spec.key,
        n0_cm3=spec.n0_cm3,
        rg_um=spec.rg_um,
        sigma_g=spec.sigma_g,
        m_real=spec.m_real,
        m_imag=spec.m_imag,
        depol_model=0.0,
    )
    alpha, beta, alpha_half, beta_half, details = integrate_lognormal_modes(
        [mode], spec.r_min_um, spec.r_max_um, grid_count
    )
    summary = OpticalSummary(
        key=spec.key,
        alpha_particle=alpha,
        beta_particle=beta,
        alpha_total=alpha + ALPHA_MOL,
        beta_total=beta + molecular_backscatter_reference(BETA_MOL),
        alpha_rel_grid_error=relative_error(alpha, alpha_half),
        beta_rel_grid_error=relative_error(beta, beta_half),
        depol_ratio=None,
        elapsed_s=time.perf_counter() - t0,
    )
    return summary, details


def compute_mie_lognormal_reference(
    mode: ModeSpec,
    r_min_um: float,
    r_max_um: float,
    n_radii: int,
) -> tuple[float, float, float, dict[str, float]]:
    radius_um = np.geomspace(r_min_um, r_max_um, ensure_odd_grid_count(n_radii))
    radius_m = radius_um * 1.0e-6
    density = lognormal_number_density_per_m(radius_m, mode.n0_cm3, mode.rg_um, mode.sigma_g)
    sigma_ext, sigma_back = cross_sections_for_grid(radius_um, mode.m_real, mode.m_imag)
    alpha = spectral_integral(sigma_ext * density, radius_m)
    beta = spectral_integral(sigma_back * density, radius_m)
    alpha_half = spectral_integral((sigma_ext * density)[::2], radius_m[::2])
    beta_half = spectral_integral((sigma_back * density)[::2], radius_m[::2])
    details = {
        "alpha_particle": alpha,
        "beta_particle": beta,
        "alpha_rel_grid_error": relative_error(alpha, alpha_half),
        "beta_rel_grid_error": relative_error(beta, beta_half),
        "depol_model": 0.0,
        "backend": "mie_direct_qback_grid",
        "beta_convention": "PDF Eq. (8): integral(Qback*pi*r^2*n(r)dr)",
    }
    return alpha, beta, 0.0, details


def build_tmatrix_tasks(haze_specs: list[HazeSpec], args: argparse.Namespace) -> list[dict[str, object]]:
    tasks: list[dict[str, object]] = []
    precision = _precision_profile(args)
    for spec in haze_specs:
        for mode in spec.modes:
            if not mode.use_tmatrix:
                continue
            tasks.append(
                {
                    "key": f"{spec.key}:{mode.name}",
                    "precision_profile": precision,
                    "solver": args.tmatrix_solver,
                    "n0_cm3": mode.n0_cm3,
                    "median_radius_um": mode.rg_um,
                    "sigma_g": mode.sigma_g,
                    "r_min_um": spec.r_min_um,
                    "r_max_um": spec.r_max_um,
                    "m_real": mode.m_real,
                    "m_imag": abs(mode.m_imag),
                    "shape_type": mode.shape_type,
                    "axis_ratio": mode.axis_ratio,
                    "n_radii": args.tmatrix_n_radii,
                    "radius_quadrature": "log_uniform_simpson",
                    "radius_segments": 1,
                    "Nr": args.tmatrix_nr,
                    "Ntheta": args.tmatrix_ntheta,
                }
            )
    return tasks


def _resolve_julia_executable(julia_cmd: str) -> str:
    """统一 Julia 路径解析（使用 path_resolver 模块）。"""
    return _resolve_julia_unified(julia_cmd if julia_cmd != "julia" else None)


def _win_short_path(path: Path) -> str:
    s = str(path)
    if sys.platform != "win32":
        return s
    try:
        import ctypes

        get_short_path_name = ctypes.windll.kernel32.GetShortPathNameW
        buf_len = get_short_path_name(s, None, 0)
        if buf_len <= 0:
            return s
        buffer = ctypes.create_unicode_buffer(buf_len)
        get_short_path_name(s, buffer, buf_len)
        return buffer.value or s
    except Exception:
        return s


def _resolve_julia_depot_dir() -> Path:
    depot = _get_julia_depot_path()
    if depot is not None and depot.exists():
        return depot
    raise RuntimeError("Julia depot 未找到；请检查项目根目录下的 julia_depot 或 JULIA_DEPOT_PATH。")


def _display_path(value: str | Path) -> str:
    path = Path(value)
    try:
        resolved = path.resolve()
    except Exception:
        resolved = path
    try:
        rel = resolved.relative_to(ROOT.resolve())
        return "." if not str(rel) else str(rel)
    except Exception:
        name = resolved.name or str(value)
        return name


def _sanitize_cmd_token(token: object) -> str:
    text = str(token)
    if text.startswith("--project="):
        return f"--project={_display_path(text.split('=', 1)[1])}"
    if any(sep in text for sep in ("\\", "/")):
        return _display_path(text)
    return text


def _sanitized_command(tokens: list[object]) -> str:
    return " ".join(_sanitize_cmd_token(token) for token in tokens)


def _sanitized_command_list(tokens: list[object]) -> list[str]:
    return [_sanitize_cmd_token(token) for token in tokens]


def run_tmatrix_batch(
    haze_specs: list[HazeSpec],
    args: argparse.Namespace,
    output_dir: Path,
) -> tuple[dict[str, dict[str, object]], dict[str, object]]:
    precision = _precision_profile(args)
    artifact_source = str(getattr(args, "cache_source", "runtime") or "runtime").strip().lower()
    if artifact_source not in {"runtime", "seed"}:
        artifact_source = "runtime"
    tasks = build_tmatrix_tasks(haze_specs, args)
    tm_dir = output_dir / "tmatrix"
    tm_dir.mkdir(parents=True, exist_ok=True)
    task_cache_dir = tm_dir / "task_cache"
    task_cache_dir.mkdir(parents=True, exist_ok=True)

    default_tm_dir = _cache_runtime.LAYOUT.default_result_root / "tmatrix"
    default_task_cache_dir = default_tm_dir / "task_cache"

    results: dict[str, dict[str, object]] = {}
    tasks_to_run: list[dict[str, object]] = []
    pending_reports: dict[str, dict[str, object]] = {}
    task_reports: list[dict[str, object]] = []
    hit_local = 0
    hit_default = 0

    stats: dict[str, object] = {
        "local_hit_count": 0,
        "default_hit_count": 0,
        "executed_count": 0,
        "total_count": len(tasks),
        "task_reports": task_reports,
        "local_cache_dir": str(task_cache_dir),
        "default_cache_dir": str(default_task_cache_dir),
        "request_path": str(tm_dir / "tmatrix_request.json"),
        "response_path": str(tm_dir / "tmatrix_response.json"),
        "invocation_path": str(tm_dir / "tmatrix_invocation.json"),
        "stdout_path": str(tm_dir / "tmatrix_stdout.txt"),
        "stderr_path": str(tm_dir / "tmatrix_stderr.txt"),
    }
    if not tasks:
        _DIAG_SESSION.write_component_json("tmatrix_task_report.json", stats)
        return {}, stats

    # Per-task cache lookup
    for task in tasks:
        task_id, scenario_key, mode_name, task_hash = _tmatrix_task_metadata(task)
        cache_file, local_candidates = _tmatrix_task_candidate_files(task_cache_dir, task_id, task_hash)
        default_cache_file, default_candidates = _tmatrix_task_candidate_files(default_task_cache_dir, task_id, task_hash)
        report_base = {
            "task_key": task_id,
            "scenario_key": scenario_key,
            "mode": mode_name,
            "task_hash": task_hash,
            "task_hash12": task_hash[:12],
            "expected_local_cache_file": str(cache_file),
            "expected_default_cache_file": str(default_cache_file),
            "local_candidate_files": local_candidates,
            "default_candidate_files": default_candidates,
        }

        indexed_result, indexed_info = _load_indexed_layer_artifact("tmatrix_task", precision, task_hash)
        if indexed_result is not None:
            results[task_id] = indexed_result
            hit_source = str(indexed_info.get("hit_source") or "runtime")
            if hit_source == "seed":
                hit_default += 1
                hit_label = "default_fallback"
            else:
                hit_local += 1
                hit_label = "local"
            if not cache_file.exists():
                cache_file.write_text(json.dumps(indexed_result, ensure_ascii=False, indent=2), encoding="utf-8")
            payload = {
                **report_base,
                "cache_source": hit_label,
                "ok": bool(indexed_result.get("ok", True)),
                "cached_elapsed_ms": float(indexed_result.get("elapsed_s", 0.0)) * 1000.0,
                "indexed_cache_hit": True,
                "indexed_cache_source": hit_source,
                "indexed_cache_path": indexed_info.get("artifact_path"),
            }
            task_reports.append(payload)
            _diag_event("tmatrix_task", elapsed_ms=0.0, payload=payload)
            _save_indexed_layer_artifact(
                "tmatrix_task",
                precision,
                task_hash,
                indexed_result,
                source=artifact_source,
                visibility="hidden",
                input_hashes={"precision_profile": precision},
            )
            continue

        local_miss_reason: str | None = None
        if cache_file.exists():
            try:
                result = json.loads(cache_file.read_text(encoding="utf-8"))
                results[task_id] = result
                hit_local += 1
                payload = {
                    **report_base,
                    "cache_source": "local",
                    "ok": bool(result.get("ok", True)),
                    "cached_elapsed_ms": float(result.get("elapsed_s", 0.0)) * 1000.0,
                }
                task_reports.append(payload)
                _diag_event("tmatrix_task", elapsed_ms=0.0, payload=payload)
                _save_indexed_layer_artifact(
                    "tmatrix_task",
                    precision,
                    task_hash,
                    result,
                    source=artifact_source,
                    visibility="hidden",
                    input_hashes={"precision_profile": precision},
                )
                continue
            except Exception as exc:
                local_miss_reason = f"json_read_failed:{exc}"
        else:
            local_miss_reason = "task_hash_mismatch" if local_candidates else "cache_file_missing"

        default_miss_reason: str | None = None
        if default_task_cache_dir.exists() and default_cache_file.exists():
            try:
                result = json.loads(default_cache_file.read_text(encoding="utf-8"))
                results[task_id] = result
                cache_file.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
                hit_default += 1
                payload = {
                    **report_base,
                    "cache_source": "default_fallback",
                    "ok": bool(result.get("ok", True)),
                    "cached_elapsed_ms": float(result.get("elapsed_s", 0.0)) * 1000.0,
                }
                task_reports.append(payload)
                _diag_event("tmatrix_task", elapsed_ms=0.0, payload=payload)
                _save_indexed_layer_artifact(
                    "tmatrix_task",
                    precision,
                    task_hash,
                    result,
                    source=artifact_source,
                    visibility="hidden",
                    input_hashes={"precision_profile": precision},
                )
                continue
            except Exception as exc:
                default_miss_reason = f"json_read_failed:{exc}"
        elif default_task_cache_dir.exists():
            default_miss_reason = "task_hash_mismatch" if default_candidates else "cache_file_missing"
        else:
            default_miss_reason = "cache_dir_missing"

        tasks_to_run.append(task)
        pending_reports[task_id] = {
            **report_base,
            "cache_source": "executed",
            "local_miss_reason": local_miss_reason,
            "default_miss_reason": default_miss_reason,
            "miss_reason": default_miss_reason or local_miss_reason,
        }

    stats["local_hit_count"] = hit_local
    stats["default_hit_count"] = hit_default


    # Report cache status
    if hit_local > 0:
        print(f"[tmatrix] {hit_local}/{len(tasks)} task(s) hit local cache")
    if hit_default > 0:
        print(f"[tmatrix] {hit_default}/{len(tasks)} task(s) hit default cache")

    if not tasks_to_run:
        print(f"[tmatrix] all {len(tasks)} task(s) cached — skipping Julia")
        _DIAG_SESSION.write_component_json("tmatrix_task_report.json", stats)
        return results, stats

    # Run uncached tasks via Julia
    request_path = tm_dir / "tmatrix_request.json"
    response_path = tm_dir / "tmatrix_response.json"
    request = {
        "tasks": tasks_to_run,
        "note": "Temp-local Julia T-matrix batch for haze coarse modes.",
    }
    request_path.write_text(json.dumps(request, ensure_ascii=False, indent=2), encoding="utf-8")

    julia_dir = Path(__file__).resolve().parent / "julia"
    wrapper = julia_dir / "tmatrix_batch.jl"
    julia_exe = _resolve_julia_executable(args.julia_cmd)
    julia_depot_dir = _resolve_julia_depot_dir()

    project_arg = f"--project={_win_short_path(julia_dir)}"
    wrapper_arg = _win_short_path(wrapper)
    request_arg = _win_short_path(request_path)
    response_arg = _win_short_path(response_path)

    cmd = [
        julia_exe,
        "--compiled-modules=no",
        "--threads",
        str(args.julia_threads),
        project_arg,
        wrapper_arg,
        request_arg,
        response_arg,
    ]
    debug_meta = {
        "resolved_julia": _sanitize_cmd_token(julia_exe),
        "cwd": ".",
        "project": _display_path(julia_dir),
        "wrapper": _display_path(wrapper),
        "request": _display_path(request_path),
        "response": _display_path(response_path),
        "julia_depot": _display_path(julia_depot_dir),
    }
    (tm_dir / "tmatrix_invocation.json").write_text(json.dumps(debug_meta, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"[tmatrix] running {len(tasks_to_run)}/{len(tasks)} task(s), solver={args.tmatrix_solver}, n_radii={args.tmatrix_n_radii}")
    print(f"[tmatrix] julia depot: {_display_path(julia_depot_dir)}")
    print(f"[tmatrix] command: {_sanitized_command(cmd)}")
    t0 = time.perf_counter()
    run_env = os.environ.copy()
    run_env["JULIA_DEPOT_PATH"] = str(julia_depot_dir)
    run_env["JULIA_PROJECT"] = str(julia_dir)
    run_env.setdefault("JULIA_PKG_PRECOMPILE_AUTO", "0")
    run_env.setdefault("JULIA_NUM_PRECOMPILE_TASKS", "1")
    _run_kwargs: dict = dict(
        cwd=str(ROOT),
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        timeout=args.tmatrix_timeout_s,
        env=run_env,
    )
    if os.name == "nt":
        import ctypes
        _run_kwargs["creationflags"] = 0x08000000  # CREATE_NO_WINDOW
    completed = subprocess.run(cmd, **_run_kwargs)
    elapsed = time.perf_counter() - t0
    stdout_path = tm_dir / "tmatrix_stdout.txt"
    stderr_path = tm_dir / "tmatrix_stderr.txt"
    stdout_path.write_text(completed.stdout or "", encoding="utf-8")
    stderr_path.write_text(completed.stderr or "", encoding="utf-8")
    if completed.returncode != 0:
        _DIAG_SESSION.write_component_json("tmatrix_task_report.json", stats)
        raise RuntimeError(
            f"Julia T-matrix batch failed with code {completed.returncode}; "
            f"see {stderr_path} and {tm_dir / 'tmatrix_invocation.json'}"
        )

    # Parse results and write to per-task cache
    response = json.loads(response_path.read_text(encoding="utf-8"))
    response_by_task = {str(item.get("key", "")): item for item in response.get("results", [])}
    failed_tasks: list[str] = []
    for task in tasks_to_run:
        task_id, _scenario_key, _mode_name, task_hash = _tmatrix_task_metadata(task)
        item = response_by_task.get(task_id)
        if item is None:
            failed_tasks.append(task_id)
            payload = {
                **pending_reports[task_id],
                "ok": False,
                "error": "missing_result",
                "stdout_path": str(stdout_path),
                "stderr_path": str(stderr_path),
            }
            task_reports.append(payload)
            _diag_event("tmatrix_task", status="error", payload=payload)
            continue

        task_elapsed_ms = float(item.get("elapsed_s", 0.0)) * 1000.0
        payload = {
            **pending_reports[task_id],
            "ok": bool(item.get("ok", False)),
            "solver_used": item.get("solver_used"),
            "solver_requested": item.get("solver_requested"),
            "elapsed_ms": task_elapsed_ms,
            "stdout_path": str(stdout_path),
            "stderr_path": str(stderr_path),
        }
        task_reports.append(payload)
        _diag_event(
            "tmatrix_task",
            status="ok" if bool(item.get("ok", False)) else "error",
            elapsed_ms=task_elapsed_ms,
            payload=payload,
        )
        if not bool(item.get("ok", False)):
            failed_tasks.append(task_id)
            continue

        results[task_id] = item
        cache_file, _matches = _tmatrix_task_candidate_files(task_cache_dir, task_id, task_hash)
        cache_file.write_text(json.dumps(item, ensure_ascii=False, indent=2), encoding="utf-8")
        _save_indexed_layer_artifact(
            "tmatrix_task",
            precision,
            task_hash,
            item,
            source=artifact_source,
            visibility="hidden",
            input_hashes={"precision_profile": precision},
        )

    stats["executed_count"] = len(tasks_to_run)
    stats["batch_elapsed_ms"] = elapsed * 1000.0
    _DIAG_SESSION.write_component_json("tmatrix_task_report.json", stats)
    if failed_tasks:
        raise RuntimeError(
            f"Julia T-matrix returned failed tasks: {failed_tasks}; "
            f"see {stderr_path} and {response_path}"
        )

    print(f"[tmatrix] completed {len(tasks_to_run)} task(s) in {elapsed:.1f}s")
    return results, stats


def compute_haze_with_tmatrix(
    spec: HazeSpec,
    mie_reference_grid: int,
    tmatrix_results: dict[str, dict[str, object]],
    molecular_depol_ratio: float,
) -> tuple[OpticalSummary, dict[str, dict[str, object]]]:
    t0 = time.perf_counter()
    alpha_total = 0.0
    beta_total = 0.0
    legacy_num = 0.0
    beta_parallel_particle = 0.0
    beta_perpendicular_particle = 0.0
    details: dict[str, dict[str, object]] = {}
    alpha_err_num = 0.0
    beta_err_num = 0.0

    for mode in spec.modes:
        mode_key = f"{spec.key}:{mode.name}"
        if mode.use_tmatrix:
            tm = tmatrix_results.get(mode_key)
            if not tm or not bool(tm.get("ok", False)):
                raise RuntimeError(f"T-matrix result missing or failed for {mode_key}: {tm}")
            alpha = float(tm["alpha_particle_m_inv"])
            beta = float(tm["beta_particle_m_inv_sr"])
            depol = float(tm["depol_back"])
            detail = dict(tm)
            detail.update(
                {
                    "alpha_particle": alpha,
                    "beta_particle": beta,
                    "alpha_rel_grid_error": None,
                    "beta_rel_grid_error": None,
                    "depol_model": depol,
                    "backend": "julia_tmatrix",
                    "beta_convention": "PDF Eq. (8) analog: sigma_sca*F11(180)",
                }
            )
        else:
            alpha, beta, depol, detail = compute_mie_lognormal_reference(
                mode, spec.r_min_um, spec.r_max_um, mie_reference_grid
            )

        alpha_total += alpha
        beta_total += beta
        mode_parallel, mode_perpendicular = add_channel_fields(detail, beta, depol)
        beta_parallel_particle += mode_parallel
        beta_perpendicular_particle += mode_perpendicular
        legacy_num += beta * depol
        details[mode.name] = detail
        if detail.get("alpha_rel_grid_error") is not None:
            alpha_err_num = max(alpha_err_num, float(detail["alpha_rel_grid_error"]))
        if detail.get("beta_rel_grid_error") is not None:
            beta_err_num = max(beta_err_num, float(detail["beta_rel_grid_error"]))

    legacy_den = max(beta_total, 1.0e-300)
    summary = build_haze_optical_summary(
        key=spec.key,
        alpha_particle=alpha_total,
        beta_particle=beta_total,
        alpha_rel_grid_error=alpha_err_num,
        beta_rel_grid_error=beta_err_num,
        elapsed_s=time.perf_counter() - t0,
        beta_parallel_particle=beta_parallel_particle,
        beta_perpendicular_particle=beta_perpendicular_particle,
        legacy_weighted_depol_ratio=legacy_num / legacy_den,
        molecular_depol_ratio=molecular_depol_ratio,
    )
    return summary, details


def _interp_mueller(angles_src: np.ndarray, values: np.ndarray, angles_dst: np.ndarray) -> np.ndarray:
    return np.interp(angles_dst, np.asarray(angles_src, dtype=float), np.asarray(values, dtype=float))


def mie_mueller_library_for_mode(
    mode: ModeSpec,
    angles_deg: np.ndarray,
    n_radii: int,
) -> dict[str, object]:
    mie_res = mie_effective_polarized(
        size_mode="lognormal",
        radius_um=mode.rg_um,
        median_radius_um=mode.rg_um,
        sigma_ln=math.log(mode.sigma_g),
        m_complex=complex(mode.m_real, abs(mode.m_imag)),
        wavelength_m=WAVELENGTH_NM * 1.0e-9,
        angles_deg=angles_deg,
        n_radii=ensure_odd_grid_count(n_radii),
    )
    m11_back = float(mie_res.M11[-1])
    return {
        "angles_deg": angles_deg,
        "M11": np.asarray(mie_res.M11, dtype=float),
        "M12": np.asarray(mie_res.M12, dtype=float),
        "M33": np.asarray(mie_res.M33, dtype=float),
        "M34": np.asarray(mie_res.M34, dtype=float),
        "sigma_sca": float(mie_res.sigma_sca),
        "sigma_back_ref": float(mie_res.sigma_sca * m11_back),
        "depol_back": float(np.clip(mode.depol_model, 0.0, 1.0)),
        "source": "mie_effective_polarized",
    }


def tmatrix_mueller_library_from_detail(detail: dict[str, object], angles_deg: np.ndarray) -> dict[str, object] | None:
    required = ("angles_deg", "M11", "M12", "M33", "M34")
    if not all(key in detail for key in required):
        return None
    src_angles = np.asarray(detail["angles_deg"], dtype=float)
    return {
        "angles_deg": angles_deg,
        "M11": _interp_mueller(src_angles, np.asarray(detail["M11"], dtype=float), angles_deg),
        "M12": _interp_mueller(src_angles, np.asarray(detail["M12"], dtype=float), angles_deg),
        "M33": _interp_mueller(src_angles, np.asarray(detail["M33"], dtype=float), angles_deg),
        "M34": _interp_mueller(src_angles, np.asarray(detail["M34"], dtype=float), angles_deg),
        "sigma_sca": float(detail.get("sigma_sca_m2", 0.0)),
        "sigma_back_ref": float(detail.get("sigma_back_ref_m2_sr", 0.0)),
        "depol_back": float(detail.get("depol_back", detail.get("depol_model", 0.0))),
        "source": "julia_tmatrix_angular_response",
    }


def build_haze_mueller_library(
    spec: HazeSpec,
    details: dict[str, dict[str, object]],
    args: argparse.Namespace,
) -> MuellerLibrary:
    angles = generate_lidar_angle_grid(
        num_total=args.mueller_angles,
        forward_res_deg=args.mueller_forward_res_deg,
        forward_max_deg=args.mueller_forward_max_deg,
        back_res_deg=args.mueller_back_res_deg,
        back_min_deg=args.mueller_back_min_deg,
    )
    weighted = {name: np.zeros_like(angles, dtype=float) for name in ("M11", "M12", "M33", "M34")}
    total_weight = 0.0
    sigma_sca_sum = 0.0
    sigma_back_sum = 0.0
    beta_sum = 0.0
    beta_sca_sum = 0.0
    depol_weighted_sum = 0.0
    sources: list[str] = []

    for mode in spec.modes:
        detail = details.get(mode.name, {})
        if mode.use_tmatrix:
            lib = tmatrix_mueller_library_from_detail(detail, angles)
            if lib is None:
                raise RuntimeError(f"missing T-matrix angular Mueller response for {spec.key}:{mode.name}")
        else:
            lib = mie_mueller_library_for_mode(mode, angles, args.haze_mueller_grid)

        weight = max(float(detail.get("beta_particle", 0.0)), 0.0)
        if weight <= 0.0:
            weight = max(float(mode.n0_cm3), 0.0)
        for name in weighted:
            weighted[name] += weight * np.asarray(lib[name], dtype=float)
        total_weight += weight
        sigma_sca_sum += weight * float(lib.get("sigma_sca", 0.0))
        sigma_back_sum += weight * float(lib.get("sigma_back_ref", 0.0))
        beta_sum += max(float(detail.get("beta_particle", 0.0)), 0.0)
        omega0 = float(detail.get("omega0", 1.0))
        beta_sca_sum += max(float(detail.get("beta_particle", 0.0)), 0.0) * float(np.clip(omega0, 0.0, 1.0))
        depol_weighted_sum += weight * float(lib.get("depol_back", 0.0))
        sources.append(str(lib.get("source", "unknown")))

    if total_weight <= 0.0:
        total_weight = 1.0
    for name in weighted:
        weighted[name] /= total_weight

    theta = np.deg2rad(angles)
    norm = spectral_integral(np.maximum(weighted["M11"], 0.0) * np.sin(theta), theta)
    if norm > 1.0e-20:
        factor = 2.0 / norm
        for name in weighted:
            weighted[name] *= factor

    depol_back = float(np.clip(depol_weighted_sum / total_weight, 0.0, 1.0))
    return MuellerLibrary(
        angles_deg=angles,
        M11=weighted["M11"],
        M12=weighted["M12"],
        M33=weighted["M33"],
        M34=weighted["M34"],
        sigma_sca=sigma_sca_sum / total_weight,
        sigma_back_ref=sigma_back_sum / total_weight,
        depol_back=depol_back,
        omega_eff=float(np.clip(beta_sca_sum / max(beta_sum, 1.0e-300), 0.0, 1.0)),
        source=", ".join(sorted(set(sources))),
    )


def resample_mueller_library(library: MuellerLibrary, target_count: int) -> MuellerLibrary:
    if target_count >= len(library.angles_deg):
        return library
    target = generate_lidar_angle_grid(
        num_total=max(int(target_count), 181),
        forward_res_deg=0.02,
        forward_max_deg=2.0,
        back_res_deg=0.05,
        back_min_deg=175.0,
    )
    return MuellerLibrary(
        angles_deg=target,
        M11=_interp_mueller(library.angles_deg, library.M11, target),
        M12=_interp_mueller(library.angles_deg, library.M12, target),
        M33=_interp_mueller(library.angles_deg, library.M33, target),
        M34=_interp_mueller(library.angles_deg, library.M34, target),
        sigma_sca=library.sigma_sca,
        sigma_back_ref=library.sigma_back_ref,
        depol_back=library.depol_back,
        omega_eff=library.omega_eff,
        source=library.source + " | resampled",
    )


def discrete_angle_order_depolarization(
    library: MuellerLibrary,
    orders: int,
    angle_bins: int,
) -> tuple[np.ndarray, dict[str, object]]:
    """Discrete-angle scattering-order propagation for Stokes polarization loss.

    State shape: (N_angles, 4) — each row is the Stokes contribution [I,Q,U,V]
    arriving from that scattering direction.  After each order the state is
    scattered again: the total incoming Stokes vector (summed over all angles)
    is redistributed according to the phase function (prob) and the per-angle
    Mueller matrix ratios M12/M11, M33/M11, M34/M11.

    The degree of polarisation after k orders drives the echo depolarisation
    estimate via the single-backscatter depol (lib.depol_back).
    """
    lib = resample_mueller_library(library, angle_bins)
    theta = np.deg2rad(lib.angles_deg)
    mu = np.cos(theta)
    phase = np.maximum(np.asarray(lib.M11, dtype=float), 0.0)
    weights_raw = phase * np.sin(theta)
    norm = spectral_integral(weights_raw, theta)
    if norm <= 1.0e-20:
        raise RuntimeError("Mueller M11 phase normalization failed")

    weights = weights_raw / norm
    dtheta = np.gradient(theta)
    prob = np.maximum(weights * dtheta, 0.0)
    prob_sum = float(np.sum(prob))
    if prob_sum <= 1.0e-20:
        prob = np.full_like(prob, 1.0 / len(prob))
    else:
        prob /= prob_sum

    # Per-angle Mueller depolarisation ratios (clipped to physical range).
    q_ratio = np.clip(np.asarray(lib.M12, dtype=float) / np.maximum(phase, 1.0e-300), -1.0, 1.0)
    u_ratio = np.clip(np.asarray(lib.M33, dtype=float) / np.maximum(phase, 1.0e-300), -1.0, 1.0)
    v_ratio = np.clip(np.asarray(lib.M34, dtype=float) / np.maximum(phase, 1.0e-300), -1.0, 1.0)

    # Initialise: each angle direction carries its phase-weighted share of a
    # fully linearly polarised beam [I=prob, Q=prob, U=0, V=0].
    state = np.zeros((len(prob), 4), dtype=float)
    state[:, 0] = prob
    state[:, 1] = prob
    order_depol = np.empty(orders + 1, dtype=float)
    retention_by_order: list[float] = []

    for order in range(orders + 1):
        total_i = max(float(np.sum(state[:, 0])), 1.0e-300)
        total_pol = float(np.linalg.norm(np.sum(state[:, 1:], axis=0)) / total_i)
        total_pol = float(np.clip(total_pol, 0.0, 1.0))
        order_depol[order] = float(np.clip(
            lib.depol_back + (1.0 - lib.depol_back) * 0.5 * (1.0 - total_pol), 0.0, 1.0
        ))
        retention_by_order.append(total_pol)
        if order == orders:
            break
        # Scatter: sum the total incoming Stokes vector, then redistribute it
        # to each outgoing angle using the phase function and Mueller ratios.
        # This models one isotropic scattering event: the angular distribution
        # of the scattered field is set by prob (∝ M11·sinθ), while the
        # polarisation components are attenuated by the per-angle Mueller ratios.
        incoming_i = float(np.sum(state[:, 0]))
        incoming_q = float(np.sum(state[:, 1]))
        incoming_u = float(np.sum(state[:, 2]))
        incoming_v = float(np.sum(state[:, 3]))
        state[:, 0] = prob * incoming_i
        state[:, 1] = prob * incoming_q * q_ratio
        state[:, 2] = prob * (incoming_u * u_ratio + incoming_v * v_ratio)
        state[:, 3] = prob * (-incoming_u * v_ratio + incoming_v * u_ratio)

    meta = {
        "kernel": "discrete-angle scattering-order propagation",
        "angle_bins_used": int(len(lib.angles_deg)),
        "phase_norm": float(norm),
        "mu_mean": float(np.sum(prob * mu)),
        "probability_sum": float(np.sum(prob)),
        "polarization_retention_by_order": [float(x) for x in retention_by_order],
    }
    return order_depol, meta


def depolarization_profile_from_order_curve(
    range_m: np.ndarray,
    alpha_total: float,
    library: MuellerLibrary,
    orders: int,
    scale: float,
    angle_bins: int,
) -> tuple[np.ndarray, np.ndarray, dict[str, object]]:
    orders = max(1, int(orders))
    scale = max(float(scale), 0.0)
    tau = np.maximum(float(alpha_total) * float(np.clip(library.omega_eff, 0.0, 1.0)) * np.asarray(range_m, dtype=float), 0.0)
    mean_orders = np.clip(scale * tau, 0.0, float(orders))
    order_depol, kernel_meta = discrete_angle_order_depolarization(library, orders, angle_bins)

    depol = np.empty_like(tau, dtype=float)
    for idx, lam in enumerate(mean_orders):
        weights = np.empty(orders + 1, dtype=float)
        weights[0] = math.exp(-lam)
        for order in range(1, orders + 1):
            weights[order] = weights[order - 1] * lam / order
        total = float(np.sum(weights))
        depol[idx] = float(np.sum(weights * order_depol) / total) if total > 0.0 else float(order_depol[0])

    meta = {
        "kernel": kernel_meta,
        "order_depolarization": [float(x) for x in order_depol],
        "mean_order_min": float(np.min(mean_orders)),
        "mean_order_max": float(np.max(mean_orders)),
    }
    return np.clip(depol, 0.0, 1.0), order_depol, meta


def stokes_depolarization_profile(
    range_m: np.ndarray,
    alpha_total: float,
    library: MuellerLibrary,
    args: argparse.Namespace,
) -> tuple[np.ndarray, dict[str, object]]:
    orders = max(1, int(args.stokes_max_orders))
    scale = max(float(args.stokes_order_scale), 0.0)
    tau = np.maximum(float(alpha_total) * float(np.clip(library.omega_eff, 0.0, 1.0)) * np.asarray(range_m, dtype=float), 0.0)
    mean_orders = np.clip(scale * tau, 0.0, float(orders))

    stokes_by_order = [np.asarray([1.0, 1.0, 0.0, 0.0], dtype=float)]
    depol, order_depol, kernel_meta = depolarization_profile_from_order_curve(
        range_m,
        alpha_total,
        library,
        orders,
        scale,
        args.discrete_angle_bins,
    )

    meta = {
        "method": "1D discrete-angle Stokes propagation",
        "stokes_initial": [1.0, 1.0, 0.0, 0.0],
        "max_orders": orders,
        "order_scale_per_optical_depth": scale,
        "discrete_angle_operator": kernel_meta["kernel"],
        "single_backscatter_depol": float(order_depol[0]),
        "order_depolarization": [float(x) for x in order_depol],
        "mean_order_min": kernel_meta["mean_order_min"],
        "mean_order_max": kernel_meta["mean_order_max"],
        "mueller_source": library.source,
        "effective_single_scattering_albedo": float(library.omega_eff),
        "angle_count": int(len(library.angles_deg)),
        "angle_min_deg": float(library.angles_deg[0]),
        "angle_max_deg": float(library.angles_deg[-1]),
    }
    return np.clip(depol, 0.0, 1.0), meta


def marshall_palmer_density_per_mm(diameter_mm: np.ndarray, rain_rate_mm_h: float) -> np.ndarray:
    if rain_rate_mm_h <= 0.0:
        return np.zeros_like(diameter_mm)
    lambda_mp = 4.1 * rain_rate_mm_h ** (-0.21)
    return 8000.0 * np.exp(-lambda_mp * diameter_mm)


def rain_diameter_grid(
    spec: RainSpec,
    grid_count: int,
) -> tuple[np.ndarray, dict[str, object]]:
    n_points = ensure_odd_grid_count(grid_count)
    d_min = 2.0 * spec.r_min_um * 1.0e-3
    d_max = 2.0 * spec.r_max_um * 1.0e-3
    diameter = np.linspace(d_min, d_max, n_points)
    return diameter, {
        "mode": "uniform",
        "dense_factor": 1.0,
        "breakpoints_mm": [d_min, d_max],
        "interval_subdivisions": [n_points - 1],
        "min_step_mm": float(diameter[1] - diameter[0]) if n_points > 1 else 0.0,
        "max_step_mm": float(diameter[1] - diameter[0]) if n_points > 1 else 0.0,
    }


def compute_rain(
    spec: RainSpec,
    grid_count: int,
) -> tuple[OpticalSummary, dict[str, object]]:
    t0 = time.perf_counter()
    diameter_mm, grid_meta = rain_diameter_grid(spec, grid_count)
    radius_um = diameter_mm * 500.0
    density = marshall_palmer_density_per_mm(diameter_mm, spec.rain_rate_mm_h)
    sigma_ext, sigma_back = cross_sections_for_grid(radius_um, spec.m_real, spec.m_imag)
    alpha = spectral_integral(sigma_ext * density, diameter_mm)
    beta = spectral_integral(sigma_back * density, diameter_mm)
    alpha_half = spectral_integral((sigma_ext * density)[::2], diameter_mm[::2])
    beta_half = spectral_integral((sigma_back * density)[::2], diameter_mm[::2])
    summary = OpticalSummary(
        key=spec.key,
        alpha_particle=alpha,
        beta_particle=beta,
        alpha_total=alpha + ALPHA_MOL,
        beta_total=beta + molecular_backscatter_reference(BETA_MOL),
        alpha_rel_grid_error=relative_error(alpha, alpha_half),
        beta_rel_grid_error=relative_error(beta, beta_half),
        depol_ratio=None,
        elapsed_s=time.perf_counter() - t0,
    )
    return summary, grid_meta


def overlap_profile(range_m: np.ndarray) -> np.ndarray:
    """Current report uses ideal geometric overlap O(R)=1."""
    return np.ones_like(np.asarray(range_m, dtype=float), dtype=float)


def apply_noise_floor(power: np.ndarray) -> np.ndarray:
    """Current report uses no noise floor."""
    return np.asarray(power, dtype=float)


def _bool_from_value(value, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on", "enabled"}:
        return True
    if text in {"0", "false", "no", "off", "disabled"}:
        return False
    return default


def resolve_noise_model(overrides: dict | None = None) -> NoiseModel:
    raw = {}
    if isinstance(overrides, dict):
        inst = overrides.get("instrument", {})
        if isinstance(inst, dict) and isinstance(inst.get("receiver_noise"), dict):
            raw = inst.get("receiver_noise", {})
        elif isinstance(overrides.get("noise"), dict):
            raw = overrides.get("noise", {})
    defaults = DEFAULT_NOISE_MODEL
    seed_raw = raw.get("random_seed", defaults["random_seed"]) if isinstance(raw, dict) else defaults["random_seed"]
    try:
        random_seed = int(seed_raw) if seed_raw not in (None, "") else None
    except Exception:
        random_seed = defaults["random_seed"]
    try:
        average_pulses = max(1, int(raw.get("average_pulses", defaults["average_pulses"])))
    except Exception:
        average_pulses = int(defaults["average_pulses"])
    return NoiseModel(
        enabled=_bool_from_value(raw.get("enabled"), bool(defaults["enabled"])),
        quantum_efficiency=max(float(raw.get("quantum_efficiency", defaults["quantum_efficiency"])), 1.0e-12),
        background_power_W=max(float(raw.get("background_power_W", defaults["background_power_W"])), 0.0),
        dark_current_A=max(float(raw.get("dark_current_A", defaults["dark_current_A"])), 0.0),
        read_noise_e=max(float(raw.get("read_noise_e", defaults["read_noise_e"])), 0.0),
        average_pulses=average_pulses,
        generate_noisy_curve=_bool_from_value(raw.get("generate_noisy_curve"), bool(defaults["generate_noisy_curve"])),
        random_seed=random_seed,
    )


def noise_model_snapshot(noise: NoiseModel, *, wavelength_nm: float, gate_time_s: float) -> dict[str, object]:
    return {
        "enabled": bool(noise.enabled),
        "model": "direct_detection_photoelectron",
        "snr_definition": "SNR=N_signal/sigma_noise, SNR_dB=20log10(SNR)",
        "quantum_efficiency": float(noise.quantum_efficiency),
        "background_power_W": float(noise.background_power_W),
        "dark_current_A": float(noise.dark_current_A),
        "read_noise_e": float(noise.read_noise_e),
        "average_pulses": int(noise.average_pulses),
        "gate_time_s": float(gate_time_s),
        "wavelength_nm": float(wavelength_nm),
        "generate_noisy_curve": bool(noise.generate_noisy_curve),
        "random_seed": noise.random_seed,
    }


def compute_noise_metrics(
    power_signal_W: np.ndarray,
    *,
    wavelength_nm: float,
    gate_time_s: float,
    noise: NoiseModel,
    rng: np.random.Generator | None = None,
) -> dict[str, object]:
    power_signal = np.asarray(power_signal_W, dtype=float)
    if not noise.enabled:
        zeros = np.zeros_like(power_signal, dtype=float)
        nan_arr = np.full_like(power_signal, np.nan, dtype=float)
        return {
            "power_observed_expected_raw": power_signal,
            "power_observed_raw": power_signal,
            "power_observed_noisy_raw": power_signal,
            "noise_std_power_W": zeros,
            "noise_floor_rms_W": 0.0,
            "snr_linear": nan_arr,
            "snr_db": nan_arr,
            "signal_photoelectrons": nan_arr,
            "background_photoelectrons": 0.0,
            "dark_photoelectrons": 0.0,
        }

    photon_energy = PLANCK_CONSTANT_J_S * LIDAR_C_M_S / (float(wavelength_nm) * 1.0e-9)
    gate_time = max(float(gate_time_s), 1.0e-30)
    eta_q = max(float(noise.quantum_efficiency), 1.0e-12)
    avg = max(int(noise.average_pulses), 1)
    electron_per_watt = eta_q * gate_time / photon_energy

    signal_e = np.maximum(power_signal, 0.0) * electron_per_watt
    background_e = float(noise.background_power_W) * electron_per_watt
    dark_e = float(noise.dark_current_A) * gate_time / ELECTRON_CHARGE_C
    fixed_var_e = max(background_e, 0.0) + max(dark_e, 0.0) + float(noise.read_noise_e) ** 2
    total_var_e = np.maximum(signal_e + fixed_var_e, 0.0)
    sigma_e_avg = np.sqrt(total_var_e / avg)
    noise_std_power = sigma_e_avg / electron_per_watt
    noise_floor_rms = math.sqrt(max(fixed_var_e, 0.0) / avg) / electron_per_watt
    snr_linear = np.divide(
        signal_e,
        sigma_e_avg,
        out=np.zeros_like(signal_e, dtype=float),
        where=sigma_e_avg > 0.0,
    )
    snr_db = np.full_like(snr_linear, np.nan, dtype=float)
    positive = snr_linear > 0.0
    snr_db[positive] = 20.0 * np.log10(snr_linear[positive])

    power_expected = power_signal + float(noise.background_power_W)
    if noise.generate_noisy_curve and rng is not None:
        noise_sample = rng.normal(0.0, noise_std_power)
        power_noisy = power_expected + noise_sample
    else:
        power_noisy = power_expected

    return {
        "power_observed_expected_raw": power_expected,
        "power_observed_raw": power_expected,
        "power_observed_noisy_raw": power_noisy,
        "noise_std_power_W": noise_std_power,
        "noise_floor_rms_W": float(noise_floor_rms),
        "snr_linear": snr_linear,
        "snr_db": snr_db,
        "signal_photoelectrons": signal_e,
        "background_photoelectrons": float(background_e),
        "dark_photoelectrons": float(dark_e),
    }


def lidar_power(
    range_m: np.ndarray,
    alpha: float,
    beta: float,
    system_constant: float = 1.0,
    overlap: np.ndarray | float = 1.0,
) -> np.ndarray:
    r2 = np.maximum(np.asarray(range_m, dtype=float) ** 2, 1.0e-30)
    return system_constant * np.asarray(overlap, dtype=float) * beta * np.exp(-2.0 * alpha * range_m) / r2


def cumulative_curve(range_m: np.ndarray, values: np.ndarray) -> np.ndarray:
    """Return the cumulative range integral of a distance-resolved curve."""
    range_m = np.asarray(range_m, dtype=float)
    values = np.asarray(values, dtype=float)
    if len(values) == 0:
        return values.copy()
    if len(values) == 1:
        return np.zeros_like(values)
    step = np.diff(range_m)
    if not np.all(np.isfinite(step)) or np.any(step <= 0.0):
        raise ValueError("range_m must be strictly increasing")
    cumulative = np.empty_like(values, dtype=float)
    cumulative[0] = values[0] * step[0]
    increments = 0.5 * (values[1:] + values[:-1]) * step
    cumulative[1:] = cumulative[0] + np.cumsum(increments)
    return cumulative


def raw_power_summary(range_m: np.ndarray, power_signal: np.ndarray, power_observed: np.ndarray) -> dict[str, float]:
    peak_signal_idx = int(np.argmax(power_signal))
    peak_observed_idx = int(np.argmax(power_observed))
    return {
        "peak_signal_raw": float(power_signal[peak_signal_idx]),
        "peak_signal_range_m": float(range_m[peak_signal_idx]),
        "peak_observed_raw": float(power_observed[peak_observed_idx]),
        "peak_observed_range_m": float(range_m[peak_observed_idx]),
        "final_cumulative_signal_raw_m": float(cumulative_curve(range_m, power_signal)[-1]),
        "final_cumulative_observed_raw_m": float(cumulative_curve(range_m, power_observed)[-1]),
    }


def snr_curve_summary(range_m: np.ndarray, snr_linear: np.ndarray, snr_db: np.ndarray) -> dict[str, float | None]:
    range_m = np.asarray(range_m, dtype=float)
    snr_linear = np.asarray(snr_linear, dtype=float)
    snr_db = np.asarray(snr_db, dtype=float)
    finite = np.isfinite(snr_linear) & np.isfinite(snr_db)
    if not np.any(finite):
        return {
            "peak_snr_linear": None,
            "peak_snr_db": None,
            "snr_db_at_100m": None,
            "snr_db_at_500m": None,
            "snr_db_at_1000m": None,
            "snr_db_at_2000m": None,
            "max_range_snr_ge_3": None,
            "max_range_snr_ge_10": None,
            "min_snr_db": None,
            "median_snr_db": None,
        }
    valid_range = range_m[finite]
    valid_linear = snr_linear[finite]
    valid_db = snr_db[finite]
    peak_idx = int(np.nanargmax(valid_linear))

    def _snr_at(distance_m: float) -> float | None:
        if len(valid_range) == 0:
            return None
        if distance_m < valid_range[0] or distance_m > valid_range[-1]:
            return None
        return float(np.interp(distance_m, valid_range, valid_db))

    def _max_range_for(threshold: float) -> float | None:
        mask = finite & (snr_linear >= threshold)
        if not np.any(mask):
            return None
        return float(np.max(range_m[mask]))

    return {
        "peak_snr_linear": float(valid_linear[peak_idx]),
        "peak_snr_db": float(valid_db[peak_idx]),
        "snr_db_at_100m": _snr_at(100.0),
        "snr_db_at_500m": _snr_at(500.0),
        "snr_db_at_1000m": _snr_at(1000.0),
        "snr_db_at_2000m": _snr_at(2000.0),
        "max_range_snr_ge_3": _max_range_for(3.0),
        "max_range_snr_ge_10": _max_range_for(10.0),
        "min_snr_db": float(np.nanmin(valid_db)),
        "median_snr_db": float(np.nanmedian(valid_db)),
    }


def depol_curve_summary(range_m: np.ndarray, depol: np.ndarray) -> dict[str, float]:
    depol = np.asarray(depol, dtype=float)
    peak_idx = int(np.nanargmax(depol))
    return {
        "min": float(np.nanmin(depol)),
        "max": float(np.nanmax(depol)),
        "mean": float(np.nanmean(depol)),
        "first": float(depol[0]),
        "last": float(depol[-1]),
        "peak_range_m": float(range_m[peak_idx]),
    }


def write_curve_csv(path: Path, headers: list[str], rows: Iterable[Iterable[float]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(headers)
        writer.writerows(rows)


def save_figure_with_svg(fig: plt.Figure, path: Path) -> None:
    fig.savefig(path)
    if path.suffix.lower() != ".svg":
        fig.savefig(path.with_suffix(".svg"), format="svg")


def save_power_curve(
    path: Path,
    range_m: np.ndarray,
    curves: list[tuple[str, np.ndarray]],
    title: str,
    ylabel: str = "归一化激光雷达量",
    yscale: str = "log",
    dual_scale: bool = False,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if dual_scale:
        fig, axes = plt.subplots(1, 2, figsize=(14.0, 6.0), dpi=150)
        fig.suptitle(title, fontsize=14, fontweight="bold")
        ax_linear, ax_log = axes
        for label, values in curves:
            values = np.asarray(values, dtype=float)
            ax_linear.plot(range_m, values, linewidth=1.8, label=label)
            ax_log.semilogy(range_m, np.clip(values, 1.0e-300, None), linewidth=1.8, label=label)
        ax_linear.set_xlabel("距离 R (m)")
        ax_linear.set_ylabel(ylabel)
        ax_linear.set_title("线性坐标")
        ax_linear.grid(True, linestyle="--", linewidth=0.5, alpha=0.55)
        ax_log.set_xlabel("距离 R (m)")
        ax_log.set_ylabel(ylabel)
        ax_log.set_title("对数坐标")
        ax_log.grid(True, which="both", linestyle="--", linewidth=0.5, alpha=0.55)
        if len(curves) > 1:
            ax_linear.legend()
            ax_log.legend()
        else:
            ax_linear.legend()
            ax_log.legend()
        fig.tight_layout()
        save_figure_with_svg(fig, path)
        plt.close(fig)
        return

    plt.figure(figsize=(7.2, 4.8), dpi=160)
    for label, values in curves:
        if yscale == "log":
            plt.semilogy(range_m / 1000.0, np.clip(values, 1.0e-300, None), linewidth=1.8, label=label)
        else:
            plt.plot(range_m / 1000.0, values, linewidth=1.8, label=label)
    plt.xlabel("距离 R (km)")
    plt.ylabel(ylabel)
    plt.title(title)
    plt.grid(True, which="both", linestyle="--", linewidth=0.5, alpha=0.55)
    if len(curves) > 1:
        plt.legend()
    plt.tight_layout()
    fig = plt.gcf()
    save_figure_with_svg(fig, path)
    plt.close(fig)


def save_depol_curve(path: Path, range_m: np.ndarray, depol: np.ndarray | float, title: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if np.isscalar(depol):
        y = np.full_like(range_m, float(depol), dtype=float)
    else:
        y = np.asarray(depol, dtype=float)
    plt.figure(figsize=(7.2, 4.8), dpi=160)
    plt.plot(range_m / 1000.0, y, linewidth=1.8)
    plt.xlabel("距离 R (km)")
    plt.ylabel("回波退偏比 δ")
    plt.title(title)
    plt.ylim(0.0, max(0.45, float(np.nanmax(y)) * 1.25 + 0.02))
    plt.grid(True, linestyle="--", linewidth=0.5, alpha=0.55)
    plt.tight_layout()
    fig = plt.gcf()
    save_figure_with_svg(fig, path)
    plt.close(fig)


def default_fog_specs() -> list[FogSpec]:
    return [
        FogSpec("radiation_fog", "Radiation fog", n0_cm3=200.0, rg_um=2.0, sigma_g=1.4),
        FogSpec("advection_fog", "Advection fog", n0_cm3=40.0, rg_um=7.0, sigma_g=1.8),
    ]


def default_haze_specs() -> list[HazeSpec]:
    # Refractive indices follow temp/2.png. Values are stored as positive
    # absorption imaginary parts k and passed to PyMieScatt / T-matrix as n+i*k.
    return [
        HazeSpec(
            "urban_industrial_haze",
            "Urban / industrial haze",
            (
                ModeSpec("fine", 15000.0, 0.08, 2.0, 1.50, 0.005, 0.005),
                ModeSpec("coarse", 5.0, 0.60, 2.3, 1.55, 0.005, 0.250, True, "spheroid", 1.6),
            ),
        ),
        HazeSpec(
            "rural_continental_haze",
            "Rural / continental haze",
            (
                ModeSpec("fine", 5000.0, 0.08, 2.0, 1.45, 0.001, 0.004),
                ModeSpec("coarse", 3.0, 0.60, 2.3, 1.53, 0.003, 0.200, True, "spheroid", 1.6),
            ),
        ),
        HazeSpec(
            "dust_desert_haze",
            "Dust / desert haze",
            (
                ModeSpec("fine", 1500.0, 0.075, 2.0, 1.55, 0.005, 0.010),
                ModeSpec("coarse", 100.0, 1.10, 2.15, 1.55, 0.010, 0.350, True, "spheroid", 1.6),
            ),
        ),
        HazeSpec(
            "maritime_haze",
            "Maritime haze",
            (
                ModeSpec("fine", 500.0, 0.075, 2.0, 1.35, 0.0, 0.003),
                ModeSpec("coarse", 25.0, 0.55, 2.0, 1.34, 0.0, 0.020),
            ),
        ),
    ]


def default_rain_specs() -> list[RainSpec]:
    return [
        RainSpec("light_rain", "Light rain", rain_rate_mm_h=1.0),
        RainSpec("moderate_rain", "Moderate rain", rain_rate_mm_h=5.0),
        RainSpec("heavy_rain", "Heavy rain", rain_rate_mm_h=12.0),
    ]


def load_param_overrides(root: Path) -> dict:
    """Load optional param_overrides.json written by the UI."""
    p = root / "temp" / "lidar_1d" / "param_overrides.json"
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def resolve_instrument_parameters(system_constant: float, instrument_overrides: dict | None = None) -> dict[str, float]:
    """Resolve a self-consistent instrument tuple for the current run."""
    inst = instrument_overrides or {}
    tau_s = float(inst.get("pulse_width_s", LIDAR_PULSE_WIDTH_S))
    receiver_radius_m = float(inst.get("receiver_radius_m", LIDAR_RECEIVER_RADIUS_M))
    optical_efficiency = float(inst.get("optical_efficiency", LIDAR_OPTICAL_EFFICIENCY))
    receiver_area_m2 = math.pi * receiver_radius_m**2
    pulse_range_resolution_m = LIDAR_C_M_S * tau_s * 0.5
    denominator = LIDAR_C_M_S * tau_s * 0.5 * receiver_area_m2 * optical_efficiency
    if abs(denominator) > 1.0e-300:
        laser_peak_power_w = float(system_constant) / denominator
    else:
        laser_peak_power_w = float(inst.get("laser_peak_power_W", 0.0))
    laser_peak_power_w = float(round(laser_peak_power_w, 12))
    return {
        "laser_peak_power_W": laser_peak_power_w,
        "pulse_width_s": tau_s,
        "receiver_radius_m": receiver_radius_m,
        "receiver_area_m2": receiver_area_m2,
        "optical_efficiency": optical_efficiency,
        "pulse_range_resolution_m": pulse_range_resolution_m,
    }


def build_effective_params_snapshot(
    fog_specs: list[FogSpec],
    haze_specs: list[HazeSpec],
    rain_specs: list[RainSpec],
    *,
    wavelength_nm: float,
    alpha_mol: float,
    beta_mol: float,
    molecular_depol_ratio: float,
    system_constant: float,
    precision_profile: str,
    instrument_parameters: dict[str, float],
    noise_model: NoiseModel,
) -> dict[str, object]:
    """Persist the effective run inputs in the UI override schema."""
    return {
        "fog": {
            spec.key: {
                "n0_cm3": float(spec.n0_cm3),
                "rg_um": float(spec.rg_um),
                "sigma_g": float(spec.sigma_g),
                "m_real": float(spec.m_real),
                "m_imag": float(spec.m_imag),
            }
            for spec in fog_specs
        },
        "haze": {
            spec.key: {
                "modes": {
                    mode.name: {
                        "n0_cm3": float(mode.n0_cm3),
                        "rg_um": float(mode.rg_um),
                        "sigma_g": float(mode.sigma_g),
                        "m_real": float(mode.m_real),
                        "m_imag": float(mode.m_imag),
                    }
                    for mode in spec.modes
                }
            }
            for spec in haze_specs
        },
        "rain": {
            spec.key: {
                "rain_rate_mm_h": float(spec.rain_rate_mm_h),
            }
            for spec in rain_specs
        },
        "cli": {
            "wavelength-nm": float(wavelength_nm),
            "alpha-mol": float(alpha_mol),
            "beta-mol": float(beta_mol),
            "molecular-depol-ratio": float(molecular_depol_ratio),
            "system-constant": float(system_constant),
        },
        "instrument": {
            "laser_peak_power_W": float(instrument_parameters["laser_peak_power_W"]),
            "pulse_width_s": float(instrument_parameters["pulse_width_s"]),
            "receiver_radius_m": float(instrument_parameters["receiver_radius_m"]),
            "optical_efficiency": float(instrument_parameters["optical_efficiency"]),
            "receiver_noise": {
                "enabled": bool(noise_model.enabled),
                "quantum_efficiency": float(noise_model.quantum_efficiency),
                "background_power_W": float(noise_model.background_power_W),
                "dark_current_A": float(noise_model.dark_current_A),
                "read_noise_e": float(noise_model.read_noise_e),
                "average_pulses": int(noise_model.average_pulses),
                "generate_noisy_curve": bool(noise_model.generate_noisy_curve),
                "random_seed": noise_model.random_seed,
            },
        },
        "precision": str(precision_profile),
    }


def apply_fog_overrides(specs: list[FogSpec], overrides: dict) -> list[FogSpec]:
    fog_ov = overrides.get("fog", {})
    if not fog_ov:
        return specs
    import dataclasses
    # Field aliases for user convenience (case-insensitive, common variants)
    _aliases = {"N0": "n0_cm3", "n0": "n0_cm3", "rg": "rg_um", "sigma": "sigma_g"}
    result = []
    for spec in specs:
        kv = fog_ov.get(spec.key, {})
        if kv:
            normalized = {_aliases.get(k, k): v for k, v in kv.items()}
            spec = dataclasses.replace(spec, **{k: type(getattr(spec, k))(v) for k, v in normalized.items() if hasattr(spec, k)})
        result.append(spec)
    return result


def apply_haze_overrides(specs: list[HazeSpec], overrides: dict) -> list[HazeSpec]:
    haze_ov = overrides.get("haze", {})
    if not haze_ov:
        return specs
    import dataclasses
    # Field aliases for user convenience (case-insensitive, common variants)
    _aliases = {"N0": "n0_cm3", "n0": "n0_cm3", "rg": "rg_um", "sigma": "sigma_g"}
    result = []
    for spec in specs:
        kv = haze_ov.get(spec.key, {})
        if not kv:
            result.append(spec)
            continue
        # top-level HazeSpec fields (r_min_um, r_max_um)
        spec_fields = {k: type(getattr(spec, k))(v) for k, v in kv.items()
                       if hasattr(spec, k) and k != "modes"}
        # per-mode overrides
        modes_ov = kv.get("modes", {})
        new_modes = []
        for mode in spec.modes:
            mv = modes_ov.get(mode.name, {})
            if mv:
                normalized = {_aliases.get(k, k): v for k, v in mv.items()}
                mode = dataclasses.replace(mode, **{k: type(getattr(mode, k))(v)
                                                    for k, v in normalized.items() if hasattr(mode, k)})
            new_modes.append(mode)
        result.append(dataclasses.replace(spec, modes=tuple(new_modes), **spec_fields))
    return result


def apply_rain_overrides(specs: list[RainSpec], overrides: dict) -> list[RainSpec]:
    rain_ov = overrides.get("rain", {})
    if not rain_ov:
        return specs
    import dataclasses
    # Field aliases for user convenience
    _aliases = {"rate": "rain_rate_mm_h", "rain_rate": "rain_rate_mm_h"}
    result = []
    for spec in specs:
        kv = rain_ov.get(spec.key, {})
        if kv:
            normalized = {_aliases.get(k, k): v for k, v in kv.items()}
            spec = dataclasses.replace(spec, **{k: type(getattr(spec, k))(v) for k, v in normalized.items() if hasattr(spec, k)})
        result.append(spec)
    return result


def normalized(power: np.ndarray) -> np.ndarray:
    max_value = float(np.max(power))
    if max_value <= 0.0:
        return power
    return power / max_value


def clean_outputs(output_dir: Path) -> None:
    for sub in ("figures", "data"):
        folder = output_dir / sub
        if not folder.exists():
            continue
        for item in folder.glob("*"):
            if item.is_file():
                item.unlink()


# ---------------------------------------------------------------------------
# Optical results cache — per-group independent keys
#
# Each of fog / haze / rain has its own hash so that changing, e.g., a haze
# particle parameter does not invalidate the fog or rain cache entries.
# The single cache file stores all three groups plus a "_hashes" sub-dict
# that records the key used for each group when it was last saved.
# ---------------------------------------------------------------------------

def _fog_cache_key(fog_specs: list, args: argparse.Namespace) -> str:
    return _cache_keys.fog_cache_key(fog_specs, args)


def _haze_cache_key(haze_specs: list, args: argparse.Namespace) -> str:
    """Key for haze optical cache (α/β only). Mueller/Stokes params excluded."""
    return _cache_keys.haze_cache_key(haze_specs, args)


def _haze_mueller_key(haze_specs: list, args: argparse.Namespace) -> str:
    """Key for haze Mueller library cache (angular response). Depends on optical key + Mueller grid params."""
    return _cache_keys.haze_mueller_key(haze_specs, args)


def _rain_cache_key(rain_specs: list, args: argparse.Namespace) -> str:
    return _cache_keys.rain_cache_key(rain_specs, args)


def _load_mueller_cache(cache_dir: Path, haze_mueller_key: str) -> dict:
    return _cache_runtime.load_mueller_cache(cache_dir, haze_mueller_key)


def _save_mueller_cache(cache_dir: Path, haze_mueller_key: str, mc: dict) -> None:
    _cache_runtime.save_mueller_cache(cache_dir, haze_mueller_key, mc)



def _load_optical_cache(cache_dir: Path, fog_key: str, haze_key: str, rain_key: str) -> dict:
    return _cache_runtime.load_optical_cache(cache_dir, fog_key, haze_key, rain_key)


_CACHE_MAX_VERSIONS = 5


def _evict_old_versions(versions: dict) -> None:
    """Keep only the most recent _CACHE_MAX_VERSIONS entries, evict the rest."""
    if len(versions) > _CACHE_MAX_VERSIONS:
        excess = len(versions) - _CACHE_MAX_VERSIONS
        for key in list(versions.keys())[:excess]:
            del versions[key]


def _save_optical_cache(cache_dir: Path,
                        fog_key: str, haze_key: str, rain_key: str,
                        oc: dict) -> None:
    _cache_runtime.save_optical_cache(cache_dir, fog_key, haze_key, rain_key, oc)


def _layer_artifact_path(layer: str, source: str, precision: str, semantic_key: str) -> Path:
    digest = hashlib.sha256(f"{layer}|{source}|{precision}|{semantic_key}".encode("utf-8")).hexdigest()[:16]
    return _cache_tier_root(source, precision, layer) / f"{digest}.json"


def _load_indexed_layer_artifact(layer: str, precision: str, semantic_key: str) -> tuple[dict | None, dict[str, object]]:
    return _cache_runtime.load_indexed_layer_artifact(layer, precision, semantic_key)


def _save_indexed_layer_artifact(
    layer: str,
    precision: str,
    semantic_key: str,
    artifact: dict[str, object],
    *,
    source: str = "runtime",
    visibility: str = "hidden",
    input_hashes: dict[str, str] | None = None,
    extra: dict[str, object] | None = None,
) -> Path:
    return _cache_runtime.save_indexed_layer_artifact(
        layer,
        precision,
        semantic_key,
        artifact,
        source=source,
        visibility=visibility,
        input_hashes=input_hashes,
        extra=extra,
    )



def _load_json_or_none(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _optical_cache_lookup_details(cache_dir: Path, group: str, cache_key: str) -> dict[str, object]:
    return _cache_runtime.optical_cache_lookup_details(cache_dir, group, cache_key)


def _mueller_cache_lookup_details(cache_dir: Path, cache_key: str) -> dict[str, object]:
    return _cache_runtime.mueller_cache_lookup_details(cache_dir, cache_key)


def _resolve_lookup_miss_reason(local_info: dict[str, object], fallback_info: dict[str, object]) -> str | None:
    return _cache_runtime.resolve_lookup_miss_reason(local_info, fallback_info)


def _tmatrix_task_metadata(task: dict[str, object]) -> tuple[str, str, str, str]:
    task_id = str(task.get("key", "unknown"))
    scenario_key, _, mode_name = task_id.partition(":")
    task_hash = hashlib.sha256(
        json.dumps(task, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()
    return task_id, scenario_key, mode_name or "unknown", task_hash


def _tmatrix_task_candidate_files(cache_dir: Path, task_id: str, task_hash: str) -> tuple[Path, list[str]]:
    safe_task_id = task_id.replace(":", "_")
    cache_file = cache_dir / f"{safe_task_id}_{task_hash[:12]}.json"
    prefix_matches = sorted(path.name for path in cache_dir.glob(f"{safe_task_id}_*.json")) if cache_dir.exists() else []
    return cache_file, prefix_matches


_RUN_T0: float = 0.0


def _ts() -> str:
    """Return elapsed-since-run-start as '[+MM:SS.s]' string for step logs."""
    elapsed = time.perf_counter() - _RUN_T0
    m, s = divmod(elapsed, 60)
    return f"[+{int(m):02d}:{s:04.1f}]"


def run(args: argparse.Namespace) -> int:
    global _RUN_T0, WAVELENGTH_NM, ALPHA_MOL, BETA_MOL
    _RUN_T0 = time.perf_counter()
    if args.wavelength_nm is not None:
        WAVELENGTH_NM = float(args.wavelength_nm)
    if args.alpha_mol is not None:
        ALPHA_MOL = float(args.alpha_mol)
    if args.beta_mol is not None:
        BETA_MOL = float(args.beta_mol)
    precision = _precision_profile(args)
    artifact_source = str(getattr(args, "cache_source", "runtime") or "runtime").strip().lower()
    if artifact_source not in {"runtime", "seed"}:
        artifact_source = "runtime"
    # mie_efficiencies uses WAVELENGTH_NM internally; clear cache so any
    # wavelength override takes effect instead of returning stale results.
    mie_efficiencies.cache_clear()
    out = Path(args.output).resolve()
    figures = out / "figures"
    data = out / "data"
    out.mkdir(parents=True, exist_ok=True)
    if args.clean:
        clean_outputs(out)

    range_m = np.arange(args.range_step_m, args.range_max_m + args.range_step_m, args.range_step_m, dtype=float)
    overlap = overlap_profile(range_m)

    # Load overrides early so instrument values are available for summaries dict
    fog_specs = default_fog_specs()
    haze_specs = default_haze_specs()
    rain_specs = default_rain_specs()
    overrides = load_param_overrides(ROOT)
    _inst_ov = overrides.get("instrument", {}) if overrides else {}
    _instrument = resolve_instrument_parameters(args.system_constant, _inst_ov)
    _noise_model = resolve_noise_model(overrides)
    _noise_rng = np.random.default_rng(_noise_model.random_seed) if _noise_model.generate_noisy_curve else None
    _p0_w = _instrument["laser_peak_power_W"]
    _tau_s = _instrument["pulse_width_s"]
    _r_m = _instrument["receiver_radius_m"]
    _eta = _instrument["optical_efficiency"]
    _area = _instrument["receiver_area_m2"]
    _res_m = _instrument["pulse_range_resolution_m"]
    if overrides:
        fog_specs  = apply_fog_overrides(fog_specs, overrides)
        haze_specs = apply_haze_overrides(haze_specs, overrides)
        rain_specs = apply_rain_overrides(rain_specs, overrides)

    summaries: dict[str, dict[str, object]] = {
        "global": {
            "wavelength_nm": WAVELENGTH_NM,
            "range_min_m": float(range_m[0]),
            "range_max_m": float(range_m[-1]),
            "range_step_m": args.range_step_m,
            "command": _sanitized_command(list(sys.argv)),
            "calculation_scheme": "local",
            "generated_curve_families": {"local": True},
            "alpha_mol_m_inv": ALPHA_MOL,
            "beta_mol_input_m_inv_sr": BETA_MOL,
            "beta_mol_internal_qback_reference_m_inv": molecular_backscatter_reference(BETA_MOL),
            "molecular_depolarization_ratio": args.molecular_depol_ratio,
            "molecular_backscatter_usage": "Input beta_mol is standard molecular backscatter in m^-1 sr^-1; the 1D chain converts it to 4*pi*beta_mol before mixing with particle Qback*pi*r^2 / sigma_sca*F11(180) backscatter.",
            "mie_refractive_index_convention": "scenario tables store positive k; PyMieScatt Mie paths pass m=n+i*k, while the report may display PDF-style m=n-i*k.",
            "system_constant": args.system_constant,
            "system_constant_source": "supplemental temp PNG table: C=P0*c*tau/2*Ar*eta",
            "instrument_parameters": {
                "laser_peak_power_W": _p0_w,
                "speed_of_light_m_s": LIDAR_C_M_S,
                "pulse_width_s": _tau_s,
                "pulse_range_resolution_m": _res_m,
                "receiver_radius_m": _r_m,
                "receiver_area_m2": _area,
                "optical_efficiency": _eta,
                "geometric_overlap_default": 1.0,
            },
            "noise_floor": float(compute_noise_metrics(
                np.asarray([0.0], dtype=float),
                wavelength_nm=WAVELENGTH_NM,
                gate_time_s=_tau_s,
                noise=_noise_model,
            )["noise_floor_rms_W"]),
            "noise_model": noise_model_snapshot(
                _noise_model,
                wavelength_nm=WAVELENGTH_NM,
                gate_time_s=_tau_s,
            ),
            "overlap": {
                "mode": "ideal",
                "blind_range_m": 0.0,
                "full_overlap_range_m": 0.0,
                "min": float(np.min(overlap)),
                "max": float(np.max(overlap)),
            },
            "spectral_integral": "composite Simpson over particle radius/diameter grids; trapezoid fallback only if SciPy is unavailable",
            "determinism": "No Monte Carlo sampling is used in this temp 1D workflow; outputs are deterministic for fixed software versions, CLI parameters, and Julia thread-independent T-matrix solver behavior.",
            "tmatrix_radius_integral": "non-spherical coarse modes use deterministic Gauss-Legendre quadrature over PDF table bounds in ln(r), optionally split into equal log-radius segments, without renormalizing truncated probability mass",
            "recommended_high_precision_profile": {
                "fog_grid": 4001,
                "rain_grid": 16001,
                "rain_grid_mode": "uniform",
                "rain_dense_factor": 1.0,
                "tmatrix_solver": "iitm_only",
                "tmatrix_n_radii": 129,
                "tmatrix_radius_quadrature": "log_uniform_simpson",
                "tmatrix_radius_segments": 1,
                "tmatrix_nr": 64,
                "tmatrix_ntheta": 96,
                "haze_mie_reference_grid": 4001,
            },
            "precision_warning_thresholds": {
                "alpha_rel_grid_error": args.alpha_precision_tol,
                "beta_rel_grid_error": args.beta_precision_tol,
            },
            "local_figure_definition": "Figures 1-6 and 11 plot raw range-gate received-equation power P(R) with the configured system constant C and ideal O(R)=1. Figures 7-10 plot Stokes-corrected echo depolarization.",
            "power_normalization": "Local power figures plot raw observed P(R) in linear/log subplots; CSV files also keep normalized columns for comparison. Cumulative figures remain normalized.",
            "qback_convention": "PDF Eq. (8): particle beta uses integral(Qback*pi*r^2*n(r)dr); molecular beta is converted from standard m^-1 sr^-1 to the same reference by multiplying 4*pi.",
            "raw_power_summary": "Each scenario summary reports peak and cumulative raw P(R) before figure normalization.",
            "snr_output": "CSV files include noise_std_power_W, noise_floor_rms_W, snr_linear and snr_db computed in the direct-detection photoelectron domain.",
            "stokes_depolarization_enabled": True,
            "stokes_depolarization_model": "1D discrete-angle Stokes [I,Q,U,V] propagation; figures 7-10 use the corrected echo depolarization profile. Single-scattering volume depolarization is retained in summary diagnostics only.",
            "stokes_max_orders": args.stokes_max_orders,
            "stokes_order_scale": args.stokes_order_scale,
            "discrete_angle_bins": args.discrete_angle_bins,
            "mueller_angle_grid": {
                "target_count": args.mueller_angles,
                "forward_res_deg": args.mueller_forward_res_deg,
                "forward_max_deg": args.mueller_forward_max_deg,
                "back_res_deg": args.mueller_back_res_deg,
                "back_min_deg": args.mueller_back_min_deg,
                "haze_mie_radius_nodes": args.haze_mueller_grid,
            },
            "rain_grid_note": "Rain uses a uniform diameter grid because Marshall-Palmer n(D) is defined per mm; after the PyMieScatt refractive-index convention fix, 16001 Simpson nodes give sub-0.1% beta half-grid changes.",
            "rain_grid_mode": "uniform",
            "rain_dense_factor": 1.0,
            "tmatrix_coarse_enabled": True,
            "tmatrix_solver": args.tmatrix_solver,
            "cache_enabled": False,
            "tmatrix_depol_definition": "delta=(F11-F22)/(F11+F22) at 180 deg; computed in temp wrapper from TransitionMatrices columns 1 and 3",
            "tmatrix_convergence_note": "Compare separate runs with increasing --tmatrix-n-radii; single-run half-grid diagnostics apply only to Mie radius/diameter integrations.",
            "precision_warnings": [],
        },
        "fog": {},
        "haze": {},
        "rain": {},
    }
    _diag_event(
        "compute_init",
        status="begin",
        payload={
            "output": str(out),
            "clean": args.clean,
            "command": _sanitized_command_list(list(sys.argv)),
            "range_grid_points": int(len(range_m)),
            "range_step_m": args.range_step_m,
            "range_max_m": args.range_max_m,
            "fog_grid": args.fog_grid,
            "rain_grid": args.rain_grid,
            "haze_mie_reference_grid": args.haze_mie_reference_grid,
            "haze_mueller_grid": args.haze_mueller_grid,
            "tmatrix_solver": args.tmatrix_solver,
            "tmatrix_n_radii": args.tmatrix_n_radii,
            "tmatrix_nr": args.tmatrix_nr,
            "tmatrix_ntheta": args.tmatrix_ntheta,
            "julia_cmd": _sanitize_cmd_token(args.julia_cmd),
            "julia_threads": args.julia_threads,
        },
    )

    print(f"[info] output: {out}")
    print(f"[info] range grid: {len(range_m)} points, {range_m[0]:.0f}-{range_m[-1]:.0f} m, dR={args.range_step_m:g} m")
    print(f"[info] particle grids: fog={args.fog_grid}, rain={args.rain_grid}")
    print("[info] calculation scheme: local")
    print(
        f"[info] lidar system: C={args.system_constant:g}, "
        f"noise={'enabled' if _noise_model.enabled else 'disabled'}, overlap=ideal"
    )

    # Optical cache: per-group independent keys so changing only fog/haze/rain
    # parameters does not invalidate the other groups.
    optical_cache_dir = out / "optical_cache"
    fog_key    = _fog_cache_key(fog_specs, args)
    haze_key   = _haze_cache_key(haze_specs, args)
    rain_key   = _rain_cache_key(rain_specs, args)
    mueller_key = _haze_mueller_key(haze_specs, args)
    local_optical_lookup = {
        "fog": _optical_cache_lookup_details(optical_cache_dir, "fog", fog_key),
        "haze": _optical_cache_lookup_details(optical_cache_dir, "haze", haze_key),
        "rain": _optical_cache_lookup_details(optical_cache_dir, "rain", rain_key),
    }
    local_mueller_lookup = _mueller_cache_lookup_details(optical_cache_dir, mueller_key)
    oc = _load_optical_cache(optical_cache_dir, fog_key, haze_key, rain_key)
    raw_oc = dict(oc)
    indexed_optical_lookup = {
        "fog": _load_indexed_layer_artifact("fog_scene", precision, fog_key),
        "haze": _load_indexed_layer_artifact("haze_scene", precision, haze_key),
        "rain": _load_indexed_layer_artifact("rain_scene", precision, rain_key),
    }
    for group, (artifact, info) in indexed_optical_lookup.items():
        if artifact is not None and group not in oc:
            oc[group] = artifact
            print(f"[optical-cache] {group} loaded from indexed cache ({info.get('hit_source', 'runtime')})")

    # Mueller library cache: independent from optical cache so changing Mueller/Stokes
    # params does not invalidate α/β results.
    mc = _load_mueller_cache(optical_cache_dir, mueller_key)
    raw_mc = dict(mc)
    indexed_mueller_artifact, indexed_mueller_info = _load_indexed_layer_artifact("haze_mueller", precision, mueller_key)
    if indexed_mueller_artifact is not None and not mc:
        mc = indexed_mueller_artifact
        print(f"[mueller-cache] loaded from indexed cache ({indexed_mueller_info.get('hit_source', 'runtime')})")
    default_mueller_cache_dir = _cache_runtime.LAYOUT.default_result_root / "optical_cache"

    # Fallback: try loading from default result set's cache for groups that missed
    default_cache_dir = _cache_runtime.LAYOUT.default_result_root / "optical_cache"
    fallback_optical_lookup = {
        "fog": _optical_cache_lookup_details(default_cache_dir, "fog", fog_key),
        "haze": _optical_cache_lookup_details(default_cache_dir, "haze", haze_key),
        "rain": _optical_cache_lookup_details(default_cache_dir, "rain", rain_key),
    }
    fallback_mueller_lookup = _mueller_cache_lookup_details(default_mueller_cache_dir, mueller_key)
    if default_cache_dir.exists():
        default_oc = _load_optical_cache(default_cache_dir, fog_key, haze_key, rain_key)
        for group in ["fog", "haze", "rain"]:
            if group not in oc and group in default_oc:
                oc[group] = default_oc[group]
                print(f"[optical-cache] {group} loaded from default result set")
        if not mc and default_mueller_cache_dir.exists():
            mc = _load_mueller_cache(default_mueller_cache_dir, mueller_key)
            if mc:
                print(f"[mueller-cache] loaded from default result set")

    fog_hit  = "fog"  in oc
    haze_hit = "haze" in oc
    rain_hit = "rain" in oc
    fog_default_hit = "fog" not in raw_oc and "fog" in oc
    haze_default_hit = "haze" not in raw_oc and "haze" in oc
    rain_default_hit = "rain" not in raw_oc and "rain" in oc
    mueller_default_hit = not raw_mc and bool(mc)
    cache_analysis = {
        "optical": {
            "fog": {
                "current_key": fog_key,
                "local_lookup": local_optical_lookup["fog"],
                "fallback_lookup": fallback_optical_lookup["fog"],
                "hit_source": "local" if "fog" in raw_oc else ("default_fallback" if fog_default_hit else "miss"),
                "miss_reason": None if fog_hit else _resolve_lookup_miss_reason(local_optical_lookup["fog"], fallback_optical_lookup["fog"]),
            },
            "haze": {
                "current_key": haze_key,
                "local_lookup": local_optical_lookup["haze"],
                "fallback_lookup": fallback_optical_lookup["haze"],
                "hit_source": "local" if "haze" in raw_oc else ("default_fallback" if haze_default_hit else "miss"),
                "miss_reason": None if haze_hit else _resolve_lookup_miss_reason(local_optical_lookup["haze"], fallback_optical_lookup["haze"]),
            },
            "rain": {
                "current_key": rain_key,
                "local_lookup": local_optical_lookup["rain"],
                "fallback_lookup": fallback_optical_lookup["rain"],
                "hit_source": "local" if "rain" in raw_oc else ("default_fallback" if rain_default_hit else "miss"),
                "miss_reason": None if rain_hit else _resolve_lookup_miss_reason(local_optical_lookup["rain"], fallback_optical_lookup["rain"]),
            },
        },
        "mueller": {
            "current_key": mueller_key,
            "local_lookup": local_mueller_lookup,
            "fallback_lookup": fallback_mueller_lookup,
            "hit_source": "local" if bool(raw_mc) else ("default_fallback" if mueller_default_hit else "miss"),
            "miss_reason": None if bool(mc) else _resolve_lookup_miss_reason(local_mueller_lookup, fallback_mueller_lookup),
        },
    }
    cache_report = {
        "optical": {
            "fog": {
                "hit": fog_hit,
                "default_fallback_hit": fog_default_hit,
                "miss": not fog_hit,
                "hit_source": cache_analysis["optical"]["fog"]["hit_source"],
                "miss_reason": cache_analysis["optical"]["fog"]["miss_reason"],
            },
            "haze": {
                "hit": haze_hit,
                "default_fallback_hit": haze_default_hit,
                "miss": not haze_hit,
                "hit_source": cache_analysis["optical"]["haze"]["hit_source"],
                "miss_reason": cache_analysis["optical"]["haze"]["miss_reason"],
            },
            "rain": {
                "hit": rain_hit,
                "default_fallback_hit": rain_default_hit,
                "miss": not rain_hit,
                "hit_source": cache_analysis["optical"]["rain"]["hit_source"],
                "miss_reason": cache_analysis["optical"]["rain"]["miss_reason"],
            },
        },
        "mueller": {
            "hit": bool(raw_mc),
            "default_fallback_hit": mueller_default_hit,
            "miss": not bool(mc),
            "hit_source": cache_analysis["mueller"]["hit_source"],
            "miss_reason": cache_analysis["mueller"]["miss_reason"],
        },
        "tmatrix_tasks": {
            "local_hit_count": 0,
            "default_hit_count": 0,
            "executed_count": 0,
            "total_count": 0,
        },
        "cache_analysis_path": str(_DIAG_SESSION.component_dir / "cache_analysis.json"),
    }
    _DIAG_SESSION.write_component_json("cache_analysis.json", cache_analysis)
    _diag_event(
        "cache_plan_built",
        payload={
            "optical": cache_report["optical"],
            "mueller": cache_report["mueller"],
            "cache_analysis": cache_analysis,
        },
    )

    # Explain recompute scope by stage (optical vs equation mapping)
    if fog_hit and haze_hit and rain_hit:
        print("[recompute-stage] 光学散射参数已命中缓存：将跳过 Mie/T-matrix，仅重建回波方程与图像/CSV")
    else:
        print("[recompute-stage] 存在光学缓存未命中：将执行对应组的 Mie/T-matrix，并重建回波方程与图像/CSV")

    # Print recompute plan upfront so the log header is immediately informative.
    _plan_lines = []
    for _grp, _hit in (("雾 (fog)", fog_hit), ("霾 (haze)", haze_hit), ("雨 (rain)", rain_hit)):
        _plan_lines.append(f"  {'[缓存命中  跳过]' if _hit else '[参数变化  重算]'}  {_grp}")
    print("=" * 60)
    print("[重算计划]")
    for _l in _plan_lines:
        print(_l)
    if not haze_hit:
        print("  → 霾需要 T-matrix (Julia)，耗时较长")
    print("=" * 60)

    if fog_hit and haze_hit and rain_hit:
        print(f"[optical-cache] all groups hit — skipping all Mie computations")
        tmatrix_results = {}
        tmatrix_cache_stats = {
            "local_hit_count": 0,
            "default_hit_count": 0,
            "executed_count": 0,
            "total_count": 0,
            "task_reports": [],
        }
        _diag_event("tmatrix_batch", payload=tmatrix_cache_stats)
    elif not fog_hit and not haze_hit and not rain_hit:
        print(f"[optical-cache] all groups miss — running full Mie computations")
        print(f"{_ts()} [step] T-matrix batch starting ({len(haze_specs)} haze scenarios)")
        _tmatrix_t0 = time.perf_counter()
        tmatrix_results, tmatrix_cache_stats = run_tmatrix_batch(haze_specs, args, out)
        _diag_event(
            "tmatrix_batch",
            elapsed_ms=(time.perf_counter() - _tmatrix_t0) * 1000.0,
            payload=tmatrix_cache_stats,
        )
        print(f"{_ts()} [step] T-matrix batch done ({time.perf_counter()-_tmatrix_t0:.1f}s)")
    else:
        hits   = [g for g, h in (("fog", fog_hit), ("haze", haze_hit), ("rain", rain_hit)) if h]
        misses = [g for g, h in (("fog", fog_hit), ("haze", haze_hit), ("rain", rain_hit)) if not h]
        print(f"[optical-cache] partial hit — cached: {hits}  recomputing: {misses}")
        if not haze_hit:
            print(f"{_ts()} [step] T-matrix batch starting ({len(haze_specs)} haze scenarios)")
            _tmatrix_t0 = time.perf_counter()
            tmatrix_results, tmatrix_cache_stats = run_tmatrix_batch(haze_specs, args, out)
            _diag_event(
                "tmatrix_batch",
                elapsed_ms=(time.perf_counter() - _tmatrix_t0) * 1000.0,
                payload=tmatrix_cache_stats,
            )
            print(f"{_ts()} [step] T-matrix batch done ({time.perf_counter()-_tmatrix_t0:.1f}s)")
        else:
            tmatrix_results = {}
            tmatrix_cache_stats = {
                "local_hit_count": 0,
                "default_hit_count": 0,
                "executed_count": 0,
                "total_count": 0,
                "task_reports": [],
            }
            _diag_event("tmatrix_batch", payload=tmatrix_cache_stats)
    cache_report["tmatrix_tasks"] = tmatrix_cache_stats
    _DIAG_SESSION.write_component_json("tmatrix_task_report.json", tmatrix_cache_stats)

    fog_stage_t0 = time.perf_counter()
    for index, spec in enumerate(fog_specs, start=1):
        scenario_t0 = time.perf_counter()
        scenario_cache_source = "local" if oc.get("fog", {}).get(spec.key) else "miss_computed"
        if oc.get("fog", {}).get(spec.key):
            cached = oc["fog"][spec.key]
            summary = _optical_summary_from_cache(cached)
            details = cached.get("modes", {})
            print(f"{_ts()} [step] fog {index}/{len(fog_specs)} {spec.key}: cache hit  alpha={summary.alpha_total:.6e} beta={summary.beta_total:.6e}")
        else:
            print(f"{_ts()} [step] fog {index}/{len(fog_specs)} {spec.key}: Mie computing…")
            _step_t = time.perf_counter()
            summary, details = compute_fog(spec, args.fog_grid)
            oc.setdefault("fog", {})[spec.key] = {**{k: getattr(summary, k) for k in OpticalSummary.__dataclass_fields__}, "modes": details}
            print(f"{_ts()} [step] fog {index}/{len(fog_specs)} {spec.key}: done ({time.perf_counter()-_step_t:.2f}s)  alpha={summary.alpha_total:.6e} beta={summary.beta_total:.6e}")
        power_signal = lidar_power(range_m, summary.alpha_total, summary.beta_total, args.system_constant, overlap)
        noise_metrics = compute_noise_metrics(
            power_signal,
            wavelength_nm=WAVELENGTH_NM,
            gate_time_s=_tau_s,
            noise=_noise_model,
            rng=_noise_rng,
        )
        power_observed = np.asarray(noise_metrics["power_observed_raw"], dtype=float)
        label_cn = cn_scenario_title(spec.key, spec.title)
        p_norm = normalized(power_signal)
        save_power_curve(
            figures / f"fig{index:02d}_{spec.key}_power.png",
            range_m,
            [(label_cn, power_signal)],
            f"{label_cn}距离门回波功率",
            ylabel="回波功率 P(R) (W)",
            dual_scale=True,
        )
        write_curve_csv(
            data / f"{spec.key}_power.csv",
            [
                "range_m",
                "overlap",
                "power_signal_raw",
                "power_observed_expected_raw",
                "power_observed_raw",
                "power_observed_noisy_raw",
                "noise_std_power_W",
                "noise_floor_rms_W",
                "snr_linear",
                "snr_db",
                "power_normalized",
            ],
            zip(
                range_m,
                overlap,
                power_signal,
                np.asarray(noise_metrics["power_observed_expected_raw"], dtype=float),
                power_observed,
                np.asarray(noise_metrics["power_observed_noisy_raw"], dtype=float),
                np.asarray(noise_metrics["noise_std_power_W"], dtype=float),
                np.full_like(range_m, float(noise_metrics["noise_floor_rms_W"]), dtype=float),
                np.asarray(noise_metrics["snr_linear"], dtype=float),
                np.asarray(noise_metrics["snr_db"], dtype=float),
                p_norm,
            ),
        )
        entry = asdict(summary)
        entry["spec"] = asdict(spec)
        entry["modes"] = details
        entry["power_raw"] = raw_power_summary(range_m, power_signal, power_observed)
        entry["noise"] = {
            "noise_floor_rms_W": float(noise_metrics["noise_floor_rms_W"]),
            "background_power_W": float(_noise_model.background_power_W),
            "gate_time_s": float(_tau_s),
        }
        entry["snr_summary"] = snr_curve_summary(
            range_m,
            np.asarray(noise_metrics["snr_linear"], dtype=float),
            np.asarray(noise_metrics["snr_db"], dtype=float),
        )
        entry["precision_warnings"] = grid_precision_warnings(
            summary, args.alpha_precision_tol, args.beta_precision_tol
        )
        summaries["fog"][spec.key] = entry
        for warning in entry["precision_warnings"]:
            summaries["global"]["precision_warnings"].append(f"{spec.key}: {warning}")
        scenario_elapsed_ms = (time.perf_counter() - scenario_t0) * 1000.0
        _diag_event(
            "scenario_compute",
            elapsed_ms=scenario_elapsed_ms,
            payload={
                "group": "fog",
                "scenario_key": spec.key,
                "index": index,
                "total": len(fog_specs),
                "cache_source": scenario_cache_source,
                "elapsed_ms": scenario_elapsed_ms,
            },
        )
        print(
            f"       alpha={summary.alpha_total:.6e}, beta={summary.beta_total:.6e}, "
            f"grid_err(alpha/beta)={summary.alpha_rel_grid_error:.2e}/{summary.beta_rel_grid_error:.2e}, "
            f"{summary.elapsed_s:.2f}s"
        )
        print(f"{_ts()} [step] fog {index}/{len(fog_specs)} {spec.key}: figures+CSV written")
    _diag_event(
        "fog_compute",
        elapsed_ms=(time.perf_counter() - fog_stage_t0) * 1000.0,
        payload={
            "scenario_count": len(fog_specs),
            "cache": cache_report["optical"]["fog"],
        },
    )

    haze_stage_t0 = time.perf_counter()
    for offset, spec in enumerate(haze_specs, start=3):
        scenario_t0 = time.perf_counter()
        scenario_cache_source = "local" if oc.get("haze", {}).get(spec.key) else "miss_computed"
        if oc.get("haze", {}).get(spec.key):
            cached = oc["haze"][spec.key]
            summary = _optical_summary_from_cache(cached)
            details = cached.get("modes", {})
            print(f"{_ts()} [step] haze {offset-2}/{len(haze_specs)} {spec.key}: cache hit  alpha={summary.alpha_total:.6e} beta={summary.beta_total:.6e}")
        else:
            print(f"{_ts()} [step] haze {offset-2}/{len(haze_specs)} {spec.key}: Mie computing…")
            _step_t = time.perf_counter()
            summary, details = compute_haze_with_tmatrix(
                spec,
                args.haze_mie_reference_grid,
                tmatrix_results,
                args.molecular_depol_ratio,
            )
            oc.setdefault("haze", {})[spec.key] = {**{k: getattr(summary, k) for k in OpticalSummary.__dataclass_fields__}, "modes": details}
            print(f"{_ts()} [step] haze {offset-2}/{len(haze_specs)} {spec.key}: done ({time.perf_counter()-_step_t:.2f}s)  alpha={summary.alpha_total:.6e} beta={summary.beta_total:.6e}")
        power_signal = lidar_power(range_m, summary.alpha_total, summary.beta_total, args.system_constant, overlap)
        noise_metrics = compute_noise_metrics(
            power_signal,
            wavelength_nm=WAVELENGTH_NM,
            gate_time_s=_tau_s,
            noise=_noise_model,
            rng=_noise_rng,
        )
        power_observed = np.asarray(noise_metrics["power_observed_raw"], dtype=float)

        # Mueller library: check independent mueller cache first
        mueller_cache_source = "local" if spec.key in raw_mc else ("default_fallback" if mueller_default_hit and spec.key in mc else "built")
        cached_mueller = mc.get(spec.key)
        if cached_mueller:
            mueller_library = MuellerLibrary(
                angles_deg=np.asarray(cached_mueller["angles_deg"], dtype=float),
                M11=np.asarray(cached_mueller["M11"], dtype=float),
                M12=np.asarray(cached_mueller["M12"], dtype=float),
                M33=np.asarray(cached_mueller["M33"], dtype=float),
                M34=np.asarray(cached_mueller["M34"], dtype=float),
                sigma_sca=float(cached_mueller["sigma_sca"]),
                sigma_back_ref=float(cached_mueller["sigma_back_ref"]),
                depol_back=float(cached_mueller["depol_back"]),
                omega_eff=float(cached_mueller["omega_eff"]),
                source=str(cached_mueller["source"]),
            )
            print(f"{_ts()} [step] haze {offset-2}/{len(haze_specs)} {spec.key}: Mueller library cache hit")
        else:
            _mueller_t = time.perf_counter()
            mueller_library = build_haze_mueller_library(spec, details, args)
            mc[spec.key] = {
                "angles_deg": mueller_library.angles_deg.tolist(),
                "M11": mueller_library.M11.tolist(),
                "M12": mueller_library.M12.tolist(),
                "M33": mueller_library.M33.tolist(),
                "M34": mueller_library.M34.tolist(),
                "sigma_sca": float(mueller_library.sigma_sca),
                "sigma_back_ref": float(mueller_library.sigma_back_ref),
                "depol_back": float(mueller_library.depol_back),
                "omega_eff": float(mueller_library.omega_eff),
                "source": str(mueller_library.source),
            }
            print(f"{_ts()} [step] haze {offset-2}/{len(haze_specs)} {spec.key}: Mueller library built ({time.perf_counter()-_mueller_t:.2f}s)")
        particle_mueller_depol = float(mueller_library.depol_back)
        _depol_val = summary.total_volume_depol_ratio if summary.total_volume_depol_ratio is not None else summary.depol_ratio
        single_scattering_volume_depol = float(_depol_val) if _depol_val is not None else 0.0
        mueller_library.depol_back = single_scattering_volume_depol
        single_scattering_volume_profile = np.full_like(range_m, single_scattering_volume_depol, dtype=float)
        depol_profile, stokes_meta = stokes_depolarization_profile(
            range_m,
            summary.alpha_total,
            mueller_library,
            args,
        )
        label_cn = cn_scenario_title(spec.key, spec.title)
        p_norm = normalized(power_signal)
        save_power_curve(
            figures / f"fig{offset:02d}_{spec.key}_power.png",
            range_m,
            [(label_cn, power_signal)],
            f"{label_cn}距离门回波功率",
            ylabel="回波功率 P(R) (W)",
            dual_scale=True,
        )
        write_curve_csv(
            data / f"{spec.key}_power.csv",
            [
                "range_m",
                "overlap",
                "power_signal_raw",
                "power_observed_expected_raw",
                "power_observed_raw",
                "power_observed_noisy_raw",
                "noise_std_power_W",
                "noise_floor_rms_W",
                "snr_linear",
                "snr_db",
                "power_normalized",
            ],
            zip(
                range_m,
                overlap,
                power_signal,
                np.asarray(noise_metrics["power_observed_expected_raw"], dtype=float),
                power_observed,
                np.asarray(noise_metrics["power_observed_noisy_raw"], dtype=float),
                np.asarray(noise_metrics["noise_std_power_W"], dtype=float),
                np.full_like(range_m, float(noise_metrics["noise_floor_rms_W"]), dtype=float),
                np.asarray(noise_metrics["snr_linear"], dtype=float),
                np.asarray(noise_metrics["snr_db"], dtype=float),
                p_norm,
            ),
        )
        save_depol_curve(
            figures / f"fig{offset + 4:02d}_{spec.key}_depol.png",
            range_m,
            depol_profile,
            f"{label_cn}回波退偏比",
        )
        write_curve_csv(
            data / f"{spec.key}_depolarization.csv",
            ["range_m", "echo_depolarization_ratio"],
            zip(range_m, depol_profile),
        )
        entry = asdict(summary)
        entry["spec"] = {
            "key": spec.key,
            "title": spec.title,
            "r_min_um": spec.r_min_um,
            "r_max_um": spec.r_max_um,
            "modes": [asdict(mode) for mode in spec.modes],
        }
        entry["modes"] = details
        entry["power_raw"] = raw_power_summary(range_m, power_signal, power_observed)
        entry["noise"] = {
            "noise_floor_rms_W": float(noise_metrics["noise_floor_rms_W"]),
            "background_power_W": float(_noise_model.background_power_W),
            "gate_time_s": float(_tau_s),
        }
        entry["snr_summary"] = snr_curve_summary(
            range_m,
            np.asarray(noise_metrics["snr_linear"], dtype=float),
            np.asarray(noise_metrics["snr_db"], dtype=float),
        )
        entry["mueller_library"] = {
            "source": mueller_library.source,
            "angle_count": int(len(mueller_library.angles_deg)),
            "particle_depol_back_from_effective_mueller": particle_mueller_depol,
            "volume_depol_back_used_for_stokes": float(mueller_library.depol_back),
            "effective_single_scattering_albedo": float(mueller_library.omega_eff),
            "sigma_sca_weighted_reference": float(mueller_library.sigma_sca),
            "sigma_back_weighted_reference": float(mueller_library.sigma_back_ref),
        }
        entry["stokes_depolarization"] = stokes_meta
        entry["single_scattering_volume_depolarization_curve"] = depol_curve_summary(
            range_m,
            single_scattering_volume_profile,
        )
        entry["stokes_corrected_echo_depolarization_curve"] = depol_curve_summary(range_m, depol_profile)
        entry["depolarization_curve"] = depol_curve_summary(range_m, depol_profile)
        entry["precision_warnings"] = grid_precision_warnings(
            summary, args.alpha_precision_tol, args.beta_precision_tol
        )
        summaries["haze"][spec.key] = entry
        for warning in entry["precision_warnings"]:
            summaries["global"]["precision_warnings"].append(f"{spec.key}: {warning}")
        scenario_elapsed_ms = (time.perf_counter() - scenario_t0) * 1000.0
        _diag_event(
            "scenario_compute",
            elapsed_ms=scenario_elapsed_ms,
            payload={
                "group": "haze",
                "scenario_key": spec.key,
                "index": offset - 2,
                "total": len(haze_specs),
                "cache_source": scenario_cache_source,
                "mueller_cache_source": mueller_cache_source,
                "elapsed_ms": scenario_elapsed_ms,
            },
        )
        print(
            f"       alpha={summary.alpha_total:.6e}, beta={summary.beta_total:.6e}, "
            f"volume_depol={single_scattering_volume_depol:.4f}, "
            f"echo_depol={float(np.nanmin(depol_profile)):.4f}-{float(np.nanmax(depol_profile)):.4f}, "
            f"grid_err(alpha/beta)={summary.alpha_rel_grid_error:.2e}/{summary.beta_rel_grid_error:.2e}, "
            f"{summary.elapsed_s:.2f}s"
        )
        print(f"{_ts()} [step] haze {offset-2}/{len(haze_specs)} {spec.key}: figures+CSV written")
    cache_report["mueller"] = {
        "hit": bool(raw_mc),
        "default_fallback_hit": mueller_default_hit,
        "miss": not bool(mc),
        "hit_source": cache_analysis["mueller"]["hit_source"],
        "miss_reason": cache_analysis["mueller"]["miss_reason"],
    }
    _diag_event(
        "haze_compute",
        elapsed_ms=(time.perf_counter() - haze_stage_t0) * 1000.0,
        payload={
            "scenario_count": len(haze_specs),
            "cache": cache_report["optical"]["haze"],
            "mueller": cache_report["mueller"],
        },
    )

    rain_stage_t0 = time.perf_counter()
    rain_raw_curves: list[tuple[str, str, np.ndarray, np.ndarray, dict[str, object]]] = []
    rain_csv_headers = ["range_m"]
    for ri, spec in enumerate(rain_specs, start=1):
        scenario_t0 = time.perf_counter()
        scenario_cache_source = "local" if oc.get("rain", {}).get(spec.key) else "miss_computed"
        if oc.get("rain", {}).get(spec.key):
            cached = oc["rain"][spec.key]
            summary = _optical_summary_from_cache(cached)
            rain_grid_meta = cached.get("diameter_grid", {})
            print(f"{_ts()} [step] rain {ri}/{len(rain_specs)} {spec.key}: cache hit  alpha={summary.alpha_total:.6e} beta={summary.beta_total:.6e}")
        else:
            print(f"{_ts()} [step] rain {ri}/{len(rain_specs)} {spec.key}: Mie computing…")
            _step_t = time.perf_counter()
            summary, rain_grid_meta = compute_rain(spec, args.rain_grid)
            oc.setdefault("rain", {})[spec.key] = {**{k: getattr(summary, k) for k in OpticalSummary.__dataclass_fields__}, "diameter_grid": rain_grid_meta}
            print(f"{_ts()} [step] rain {ri}/{len(rain_specs)} {spec.key}: done ({time.perf_counter()-_step_t:.2f}s)  alpha={summary.alpha_total:.6e} beta={summary.beta_total:.6e}")
        power_signal = lidar_power(range_m, summary.alpha_total, summary.beta_total, args.system_constant, overlap)
        noise_metrics = compute_noise_metrics(
            power_signal,
            wavelength_nm=WAVELENGTH_NM,
            gate_time_s=_tau_s,
            noise=_noise_model,
            rng=_noise_rng,
        )
        power_observed = np.asarray(noise_metrics["power_observed_raw"], dtype=float)
        rain_raw_curves.append((spec.key, spec.title, power_signal, power_observed, noise_metrics))
        rain_csv_headers += [
            f"{spec.key}_power_signal_raw",
            f"{spec.key}_power_observed_expected_raw",
            f"{spec.key}_power_observed_raw",
            f"{spec.key}_power_observed_noisy_raw",
            f"{spec.key}_noise_std_power_W",
            f"{spec.key}_noise_floor_rms_W",
            f"{spec.key}_snr_linear",
            f"{spec.key}_snr_db",
            f"{spec.key}_power_normalized_in_fig11",
        ]
        entry = asdict(summary)
        entry["spec"] = asdict(spec)
        entry["diameter_grid"] = rain_grid_meta
        entry["marshall_palmer_lambda_mm_inv"] = 4.1 * spec.rain_rate_mm_h ** (-0.21) if spec.rain_rate_mm_h > 0.0 else None
        entry["power_raw"] = raw_power_summary(range_m, power_signal, power_observed)
        entry["noise"] = {
            "noise_floor_rms_W": float(noise_metrics["noise_floor_rms_W"]),
            "background_power_W": float(_noise_model.background_power_W),
            "gate_time_s": float(_tau_s),
        }
        entry["snr_summary"] = snr_curve_summary(
            range_m,
            np.asarray(noise_metrics["snr_linear"], dtype=float),
            np.asarray(noise_metrics["snr_db"], dtype=float),
        )
        entry["precision_warnings"] = grid_precision_warnings(
            summary, args.alpha_precision_tol, args.beta_precision_tol
        )
        summaries["rain"][spec.key] = entry
        for warning in entry["precision_warnings"]:
            summaries["global"]["precision_warnings"].append(f"{spec.key}: {warning}")
        scenario_elapsed_ms = (time.perf_counter() - scenario_t0) * 1000.0
        _diag_event(
            "scenario_compute",
            elapsed_ms=scenario_elapsed_ms,
            payload={
                "group": "rain",
                "scenario_key": spec.key,
                "index": ri,
                "total": len(rain_specs),
                "cache_source": scenario_cache_source,
                "elapsed_ms": scenario_elapsed_ms,
            },
        )
        print(
            f"       R={spec.rain_rate_mm_h:g} mm/h, alpha={summary.alpha_total:.6e}, beta={summary.beta_total:.6e}, "
            f"grid_err(alpha/beta)={summary.alpha_rel_grid_error:.2e}/{summary.beta_rel_grid_error:.2e}, "
            f"{summary.elapsed_s:.2f}s"
        )
        print(f"{_ts()} [step] rain {ri}/{len(rain_specs)} {spec.key}: done")
    if rain_raw_curves:
        rain_common_max = max(
            float(np.max(power_signal))
            for _key, _title, power_signal, _power_observed, _noise_metrics in rain_raw_curves
        )
        rain_common_max = max(rain_common_max, 1.0e-300)
        rain_curves = [
            (cn_scenario_title(key, title), power_signal)
            for key, title, power_signal, _power_observed, _noise_metrics in rain_raw_curves
        ]
        rain_csv_cols: list[np.ndarray] = [range_m]
        for _key, _title, power_signal, power_observed, noise_metrics in rain_raw_curves:
            rain_csv_cols += [
                power_signal,
                np.asarray(noise_metrics["power_observed_expected_raw"], dtype=float),
                power_observed,
                np.asarray(noise_metrics["power_observed_noisy_raw"], dtype=float),
                np.asarray(noise_metrics["noise_std_power_W"], dtype=float),
                np.full_like(range_m, float(noise_metrics["noise_floor_rms_W"]), dtype=float),
                np.asarray(noise_metrics["snr_linear"], dtype=float),
                np.asarray(noise_metrics["snr_db"], dtype=float),
                power_signal / rain_common_max,
            ]
        save_power_curve(
            figures / "fig11_rain_power.png",
            range_m,
            rain_curves,
            "小雨、中雨、大雨距离门回波功率",
            ylabel="回波功率 P(R) (W)",
            dual_scale=True,
        )
        write_curve_csv(data / "rain_power.csv", rain_csv_headers, zip(*rain_csv_cols))
        print(f"{_ts()} [step] rain figure+CSV written")
    else:
        print(f"{_ts()} [step] rain: no scenarios configured, skipping")
    _diag_event(
        "rain_compute",
        elapsed_ms=(time.perf_counter() - rain_stage_t0) * 1000.0,
        payload={
            "scenario_count": len(rain_specs),
            "cache": cache_report["optical"]["rain"],
        },
    )

    # Persist optical cache — only groups that were (re)computed are written;
    # groups that were cache hits already exist on disk and are preserved.
    cache_persist_t0 = time.perf_counter()
    cache_persist_warnings: list[str] = []
    try:
        _save_optical_cache(optical_cache_dir, fog_key, haze_key, rain_key, oc)
        print(f"{_ts()} [step] optical cache saved")
    except Exception as exc:
        msg = f"optical cache save skipped: {exc}"
        cache_persist_warnings.append(msg)
        print(f"{_ts()} [warn] {msg}")
    mueller_cache_saved = False
    if mc:
        try:
            _save_mueller_cache(optical_cache_dir, mueller_key, mc)
            mueller_cache_saved = True
            print(f"{_ts()} [step] Mueller cache saved")
        except Exception as exc:
            msg = f"Mueller cache save skipped: {exc}"
            cache_persist_warnings.append(msg)
            print(f"{_ts()} [warn] {msg}")
    for group_name, artifact, semantic_key in (
        ("fog_scene", oc.get("fog"), fog_key),
        ("haze_scene", oc.get("haze"), haze_key),
        ("rain_scene", oc.get("rain"), rain_key),
    ):
        if artifact is not None:
            try:
                _save_indexed_layer_artifact(
                    group_name,
                    precision,
                    semantic_key,
                    artifact,
                    source=artifact_source,
                    visibility="hidden",
                    input_hashes={"precision_profile": precision},
                )
            except Exception as exc:
                msg = f"{group_name} indexed cache save skipped: {exc}"
                cache_persist_warnings.append(msg)
                print(f"{_ts()} [warn] {msg}")
    if mc:
        try:
            _save_indexed_layer_artifact(
                "haze_mueller",
                precision,
                mueller_key,
                mc,
                source=artifact_source,
                visibility="hidden",
                input_hashes={"precision_profile": precision},
            )
        except Exception as exc:
            msg = f"haze_mueller indexed cache save skipped: {exc}"
            cache_persist_warnings.append(msg)
            print(f"{_ts()} [warn] {msg}")
    _diag_event(
        "cache_persist",
        elapsed_ms=(time.perf_counter() - cache_persist_t0) * 1000.0,
        payload={
            "optical_cache_dir": str(optical_cache_dir),
            "mueller_cache_saved": mueller_cache_saved,
            "warnings": cache_persist_warnings,
        },
    )
    if cache_persist_warnings:
        cache_report["persist_warnings"] = cache_persist_warnings

    summary_write_t0 = time.perf_counter()
    # 注入 cache_identity:5 子 key + 派生 identity。
    # 写在 summary 顶层与 global/scenes 同级,供前端 hash 表查询匹配。
    _instr_for_hash = {
        "laser_peak_power_W": _p0_w,
        "pulse_width_s": _tau_s,
        "receiver_radius_m": _r_m,
        "optical_efficiency": _eta,
    }
    _instrument_hash = _cache_keys.instrument_hash(_instr_for_hash)
    _noise_hash = _cache_keys.noise_hash(asdict(_noise_model))
    summaries["cache_identity"] = {
        "fog_key": fog_key,
        "haze_key": haze_key,
        "haze_mueller_key": mueller_key,
        "rain_key": rain_key,
        "instrument_hash": _instrument_hash,
        "noise_hash": _noise_hash,
        "identity": _cache_keys.compose_run_identity(
            fog_key, haze_key, mueller_key, rain_key, _instrument_hash, _noise_hash
        ),
        "precision_profile": precision,
        "schema_version": 2 if _noise_hash else 1,
    }
    params_snapshot = build_effective_params_snapshot(
        fog_specs,
        haze_specs,
        rain_specs,
        wavelength_nm=WAVELENGTH_NM,
        alpha_mol=ALPHA_MOL,
        beta_mol=BETA_MOL,
        molecular_depol_ratio=args.molecular_depol_ratio,
        system_constant=args.system_constant,
        precision_profile=precision,
        instrument_parameters=_instrument,
        noise_model=_noise_model,
    )
    with (out / "summary.json").open("w", encoding="utf-8") as fh:
        json.dump(summaries, fh, ensure_ascii=False, indent=2)
    with (out / "params.json").open("w", encoding="utf-8") as fh:
        json.dump(params_snapshot, fh, ensure_ascii=False, indent=2)
    compute_summary = {
        "output": str(out),
        "figures_dir": str(figures),
        "data_dir": str(data),
        "summary_path": str(out / "summary.json"),
        "params_path": str(out / "params.json"),
        "scenario_counts": {
            "fog": len(fog_specs),
            "haze": len(haze_specs),
            "rain": len(rain_specs),
        },
        "cache_report": cache_report,
        "elapsed_s": time.perf_counter() - _RUN_T0,
    }
    _DIAG_SESSION.write_component_json("cache_report.json", cache_report)
    _DIAG_SESSION.write_component_json("compute_summary.json", compute_summary)
    _diag_event(
        "summary_write",
        elapsed_ms=(time.perf_counter() - summary_write_t0) * 1000.0,
        payload={
            "summary_path": str(out / "summary.json"),
            "diagnostics_summary": "compute_summary.json",
            "diagnostics_cache_report": "cache_report.json",
        },
    )

    elapsed_total = time.perf_counter() - _RUN_T0
    _diag_event(
        "compute_completed",
        elapsed_ms=elapsed_total * 1000.0,
        payload={
            "output": str(out),
            "cache_report": cache_report,
        },
    )
    _DIAG_SESSION.update_summary(
        "simulation_1d",
        {
            "output": str(out),
            "elapsed_s": elapsed_total,
            "cache_report": cache_report,
            "summary_path": str(out / "summary.json"),
            "compute_summary_path": str(_DIAG_SESSION.component_dir / "compute_summary.json"),
        },
        status="success",
    )

    elapsed_total = time.perf_counter() - _RUN_T0
    m_tot, s_tot = divmod(elapsed_total, 60)
    print(f"[done] figures: {figures}")
    print(f"[done] data: {data}")
    print(f"[done] summary: {out / 'summary.json'}")
    print(f"[done] total elapsed: {int(m_tot):02d}:{s_tot:04.1f}")
    return 0


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="1D lidar simulation and plotting for fog/haze/rain scenarios.")
    parser.add_argument("--output", default=str(Path(__file__).resolve().parent / "outputs"))
    parser.add_argument("--range-max-m", type=float, default=2000.0)
    parser.add_argument("--range-step-m", type=float, default=1.0)
    parser.add_argument("--fog-grid", type=int, default=4001)
    parser.add_argument("--rain-grid", type=int, default=4001)
    parser.add_argument("--system-constant", type=float, default=DEFAULT_SYSTEM_CONSTANT, help="Lidar equation system constant C.")
    parser.add_argument("--molecular-depol-ratio", type=float, default=0.00365, help="Molecular linear depolarization ratio used to split molecular backscatter channels.")
    parser.add_argument("--alpha-precision-tol", type=float, default=0.01)
    parser.add_argument("--beta-precision-tol", type=float, default=0.05)
    parser.add_argument("--tmatrix-solver", choices=["iitm_only"], default="iitm_only")
    parser.add_argument("--tmatrix-n-radii", type=int, default=129)
    parser.add_argument("--tmatrix-nr", type=int, default=64)
    parser.add_argument("--tmatrix-ntheta", type=int, default=96)
    parser.add_argument("--tmatrix-timeout-s", type=int, default=1800)
    parser.add_argument("--julia-cmd", default="julia")
    parser.add_argument("--julia-threads", default="auto")
    parser.add_argument("--precision-profile", choices=["fast", "medium", "high"], default="high")
    parser.add_argument("--cache-source", choices=["runtime", "seed"], default="runtime")
    parser.add_argument("--haze-mie-reference-grid", type=int, default=401)
    parser.add_argument("--haze-mueller-grid", type=int, default=401, help="Mie radius nodes used for haze angular Mueller libraries.")
    parser.add_argument("--mueller-angles", type=int, default=1200, help="Target angular samples for 1D Stokes propagation.")
    parser.add_argument("--mueller-forward-res-deg", type=float, default=0.01)
    parser.add_argument("--mueller-forward-max-deg", type=float, default=2.0)
    parser.add_argument("--mueller-back-res-deg", type=float, default=0.02)
    parser.add_argument("--mueller-back-min-deg", type=float, default=175.0)
    parser.add_argument("--stokes-max-orders", type=int, default=8, help="Maximum effective multiple-scattering order for 1D Stokes propagation.")
    parser.add_argument("--stokes-order-scale", type=float, default=0.65, help="Mean Stokes scattering order per optical depth.")
    parser.add_argument("--discrete-angle-bins", type=int, default=721, help="Angular bins used by the 1D discrete-angle propagation closure.")
    parser.add_argument("--wavelength-nm", type=float, default=None, help="Override laser wavelength (nm). Default: built-in value.")
    parser.add_argument("--alpha-mol", type=float, default=None, help="Override molecular extinction coefficient (m⁻¹).")
    parser.add_argument("--beta-mol", type=float, default=None, help="Override molecular backscatter coefficient (m⁻¹sr⁻¹).")
    parser.add_argument("--clean", action="store_true", help="Remove previous output files before running.")
    return parser


def parse_args() -> argparse.Namespace:
    return build_arg_parser().parse_args()


if __name__ == "__main__":
    raise SystemExit(run(parse_args()))
