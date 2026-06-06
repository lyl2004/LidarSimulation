"""公共缓存键 / identity 计算模块。

模块职责:
- 维护雾/霾/雨/仪器场景的 dataclass 与默认值
- 提供四类光学 cache key 函数(fog/haze/haze_mueller/rain),与
  ``temp/lidar_1d/lidar_1d_simulation.py`` 内的私有实现保持完全等价
- 提供 ``instrument_hash`` / ``compose_run_identity`` / ``args_from_precision``
  等前端可直接调用的工具,使前端可在不启动子进程的前提下推导出与后端
  完全一致的 identity,用于"已有 run 是否能复用"的查表判定

模块刻意不依赖 ``lidar_1d_simulation``,以便前端导入时不拖入大量绘图栈;
但内部 argparse defaults 与 PRECISION_PRESETS 必须与那两个上游模块逐字段对齐,
相应的回归测试 ``tests/test_cache_keys_regression.py`` 会断言一致性。
"""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import math
from dataclasses import dataclass


# ---------------------------------------------------------------------------
# 模块常量 — 必须与 lidar_1d_simulation.py 顶部完全一致
# ---------------------------------------------------------------------------
WAVELENGTH_NM = 1550.0
ALPHA_MOL = 1.6e-7
BETA_MOL = 1.9e-8

LIDAR_P0_W = 50.0
LIDAR_C_M_S = 3.0e8
LIDAR_PULSE_WIDTH_S = 2.0e-7
LIDAR_RECEIVER_RADIUS_M = 0.05
LIDAR_OPTICAL_EFFICIENCY = 0.8
LIDAR_RECEIVER_AREA_M2 = math.pi * LIDAR_RECEIVER_RADIUS_M ** 2
DEFAULT_SYSTEM_CONSTANT = (
    LIDAR_P0_W
    * LIDAR_C_M_S
    * LIDAR_PULSE_WIDTH_S
    * 0.5
    * LIDAR_RECEIVER_AREA_M2
    * LIDAR_OPTICAL_EFFICIENCY
)

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


# ---------------------------------------------------------------------------
# 场景 dataclass — 字段顺序、默认值必须与 lidar_1d_simulation.py 一致
# ``dataclasses.asdict`` 会把所有字段序列化进 hash payload,任何字段增减都会
# 改变 hash,务必保持同步。
# ---------------------------------------------------------------------------
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
    modes: tuple
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


# ---------------------------------------------------------------------------
# 默认场景集合 — 必须与 lidar_1d_simulation.py 中 default_*_specs() 完全一致
# ---------------------------------------------------------------------------
def default_fog_specs() -> list[FogSpec]:
    return [
        FogSpec("radiation_fog", "Radiation fog", n0_cm3=200.0, rg_um=2.0, sigma_g=1.4),
        FogSpec("advection_fog", "Advection fog", n0_cm3=40.0, rg_um=7.0, sigma_g=1.8),
    ]


def default_haze_specs() -> list[HazeSpec]:
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


# ---------------------------------------------------------------------------
# 覆盖应用 — 与 lidar_1d_simulation.py 中对应函数等价
# ---------------------------------------------------------------------------
_NUM_ALIASES_FOG = {"N0": "n0_cm3", "n0": "n0_cm3", "rg": "rg_um", "sigma": "sigma_g"}
_NUM_ALIASES_HAZE = {"N0": "n0_cm3", "n0": "n0_cm3", "rg": "rg_um", "sigma": "sigma_g"}
_NUM_ALIASES_RAIN = {"rate": "rain_rate_mm_h", "rain_rate": "rain_rate_mm_h"}


def apply_fog_overrides(specs: list[FogSpec], overrides: dict) -> list[FogSpec]:
    fog_ov = overrides.get("fog", {})
    if not fog_ov:
        return specs
    result: list[FogSpec] = []
    for spec in specs:
        kv = fog_ov.get(spec.key, {})
        if kv:
            normalized = {_NUM_ALIASES_FOG.get(k, k): v for k, v in kv.items()}
            spec = dataclasses.replace(spec, **{
                k: type(getattr(spec, k))(v)
                for k, v in normalized.items()
                if hasattr(spec, k)
            })
        result.append(spec)
    return result


def apply_haze_overrides(specs: list[HazeSpec], overrides: dict) -> list[HazeSpec]:
    haze_ov = overrides.get("haze", {})
    if not haze_ov:
        return specs
    result: list[HazeSpec] = []
    for spec in specs:
        kv = haze_ov.get(spec.key, {})
        if not kv:
            result.append(spec)
            continue
        spec_fields = {
            k: type(getattr(spec, k))(v)
            for k, v in kv.items()
            if hasattr(spec, k) and k != "modes"
        }
        modes_ov = kv.get("modes", {})
        new_modes = []
        for mode in spec.modes:
            mv = modes_ov.get(mode.name, {})
            if mv:
                normalized = {_NUM_ALIASES_HAZE.get(k, k): v for k, v in mv.items()}
                mode = dataclasses.replace(mode, **{
                    k: type(getattr(mode, k))(v)
                    for k, v in normalized.items()
                    if hasattr(mode, k)
                })
            new_modes.append(mode)
        result.append(dataclasses.replace(spec, modes=tuple(new_modes), **spec_fields))
    return result


def apply_rain_overrides(specs: list[RainSpec], overrides: dict) -> list[RainSpec]:
    rain_ov = overrides.get("rain", {})
    if not rain_ov:
        return specs
    result: list[RainSpec] = []
    for spec in specs:
        kv = rain_ov.get(spec.key, {})
        if kv:
            normalized = {_NUM_ALIASES_RAIN.get(k, k): v for k, v in kv.items()}
            spec = dataclasses.replace(spec, **{
                k: type(getattr(spec, k))(v)
                for k, v in normalized.items()
                if hasattr(spec, k)
            })
        result.append(spec)
    return result


# ---------------------------------------------------------------------------
# 精度档解析 — 与 lidar_1d_simulation._precision_profile 等价
# ---------------------------------------------------------------------------
def precision_profile_of(args: argparse.Namespace | dict) -> str:
    if isinstance(args, dict):
        raw = args.get("precision_profile", "high")
    else:
        raw = getattr(args, "precision_profile", "high")
    value = str(raw or "high").strip().lower()
    return value if value in {"fast", "medium", "high"} else "high"


def _resolve_wavelength(args) -> float:
    raw = getattr(args, "wavelength_nm", None)
    return float(raw) if raw is not None else WAVELENGTH_NM


def _resolve_alpha_mol(args) -> float:
    raw = getattr(args, "alpha_mol", None)
    return float(raw) if raw is not None else ALPHA_MOL


def _resolve_beta_mol(args) -> float:
    raw = getattr(args, "beta_mol", None)
    return float(raw) if raw is not None else BETA_MOL


def _resolve_beta_mol_internal(args) -> float:
    return 4.0 * math.pi * _resolve_beta_mol(args)


def _sha256_json(payload) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, default=str, ensure_ascii=False).encode()
    ).hexdigest()


# ---------------------------------------------------------------------------
# 四类光学缓存键 — payload 必须与 lidar_1d_simulation.py 内对应函数完全一致
# ---------------------------------------------------------------------------
def fog_cache_key(fog_specs: list[FogSpec], args: argparse.Namespace) -> str:
    payload = {
        "fog": [dataclasses.asdict(s) for s in fog_specs],
        "fog_grid": args.fog_grid,
        "precision_profile": precision_profile_of(args),
        "wavelength_nm": _resolve_wavelength(args),
        "alpha_mol": _resolve_alpha_mol(args),
        "beta_mol_input_m_inv_sr": _resolve_beta_mol(args),
        "beta_mol_internal_qback_reference_m_inv": _resolve_beta_mol_internal(args),
        "molecular_depol_ratio": args.molecular_depol_ratio,
    }
    return _sha256_json(payload)


def haze_cache_key(haze_specs: list[HazeSpec], args: argparse.Namespace) -> str:
    payload = {
        "haze": [dataclasses.asdict(s) for s in haze_specs],
        "haze_mie_reference_grid": args.haze_mie_reference_grid,
        "precision_profile": precision_profile_of(args),
        "tmatrix_solver": args.tmatrix_solver,
        "tmatrix_n_radii": args.tmatrix_n_radii,
        "tmatrix_nr": args.tmatrix_nr,
        "tmatrix_ntheta": args.tmatrix_ntheta,
        "wavelength_nm": _resolve_wavelength(args),
        "alpha_mol": _resolve_alpha_mol(args),
        "beta_mol_input_m_inv_sr": _resolve_beta_mol(args),
        "beta_mol_internal_qback_reference_m_inv": _resolve_beta_mol_internal(args),
        "molecular_depol_ratio": args.molecular_depol_ratio,
    }
    return _sha256_json(payload)


def haze_mueller_key(haze_specs: list[HazeSpec], args: argparse.Namespace) -> str:
    payload = {
        "haze": [dataclasses.asdict(s) for s in haze_specs],
        "haze_mie_reference_grid": args.haze_mie_reference_grid,
        "precision_profile": precision_profile_of(args),
        "tmatrix_solver": args.tmatrix_solver,
        "tmatrix_n_radii": args.tmatrix_n_radii,
        "tmatrix_nr": args.tmatrix_nr,
        "tmatrix_ntheta": args.tmatrix_ntheta,
        "wavelength_nm": _resolve_wavelength(args),
        "molecular_depol_ratio": args.molecular_depol_ratio,
        "haze_mueller_grid": args.haze_mueller_grid,
        "mueller_angles": args.mueller_angles,
        "mueller_forward_res_deg": args.mueller_forward_res_deg,
        "mueller_forward_max_deg": args.mueller_forward_max_deg,
        "mueller_back_res_deg": args.mueller_back_res_deg,
        "mueller_back_min_deg": args.mueller_back_min_deg,
    }
    return _sha256_json(payload)


def rain_cache_key(rain_specs: list[RainSpec], args: argparse.Namespace) -> str:
    payload = {
        "rain": [dataclasses.asdict(s) for s in rain_specs],
        "rain_grid": args.rain_grid,
        "precision_profile": precision_profile_of(args),
        "wavelength_nm": _resolve_wavelength(args),
        "alpha_mol": _resolve_alpha_mol(args),
        "beta_mol_input_m_inv_sr": _resolve_beta_mol(args),
        "beta_mol_internal_qback_reference_m_inv": _resolve_beta_mol_internal(args),
        "molecular_depol_ratio": args.molecular_depol_ratio,
    }
    return _sha256_json(payload)


# ---------------------------------------------------------------------------
# 仪器维度 hash — 用于"仅 P0/τ/r/η 变化时复用光学量"快速路径判定
# 这些字段不进入 α/β 计算,仅决定输出功率缩放
# ---------------------------------------------------------------------------
INSTRUMENT_FIELDS = (
    "laser_peak_power_W",
    "pulse_width_s",
    "receiver_radius_m",
    "optical_efficiency",
)


def instrument_hash(instrument: dict) -> str:
    """对仪器维度字段做 sha256;缺失字段用 LIDAR_* 默认值填充。"""
    payload = {
        "laser_peak_power_W": float(instrument.get("laser_peak_power_W", LIDAR_P0_W)),
        "pulse_width_s": float(instrument.get("pulse_width_s", LIDAR_PULSE_WIDTH_S)),
        "receiver_radius_m": float(instrument.get("receiver_radius_m", LIDAR_RECEIVER_RADIUS_M)),
        "optical_efficiency": float(instrument.get("optical_efficiency", LIDAR_OPTICAL_EFFICIENCY)),
    }
    return _sha256_json(payload)


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


def normalize_noise_model(noise: dict | None) -> dict:
    raw = noise or {}
    defaults = DEFAULT_NOISE_MODEL
    seed = raw.get("random_seed", defaults["random_seed"])
    try:
        seed = int(seed) if seed not in (None, "") else None
    except Exception:
        seed = defaults["random_seed"]
    try:
        avg = max(1, int(raw.get("average_pulses", defaults["average_pulses"])))
    except Exception:
        avg = int(defaults["average_pulses"])
    return {
        "enabled": _bool_from_value(raw.get("enabled"), bool(defaults["enabled"])),
        "quantum_efficiency": max(float(raw.get("quantum_efficiency", defaults["quantum_efficiency"])), 1.0e-12),
        "background_power_W": max(float(raw.get("background_power_W", defaults["background_power_W"])), 0.0),
        "dark_current_A": max(float(raw.get("dark_current_A", defaults["dark_current_A"])), 0.0),
        "read_noise_e": max(float(raw.get("read_noise_e", defaults["read_noise_e"])), 0.0),
        "average_pulses": avg,
        "generate_noisy_curve": _bool_from_value(raw.get("generate_noisy_curve"), bool(defaults["generate_noisy_curve"])),
        "random_seed": seed,
    }


def noise_hash(noise: dict | None) -> str | None:
    normalized = normalize_noise_model(noise)
    if not normalized["enabled"]:
        return None
    return _sha256_json(normalized)


def noise_hash_from_overrides(overrides: dict | None) -> str | None:
    if not isinstance(overrides, dict):
        return noise_hash(None)
    instrument = overrides.get("instrument", {})
    if isinstance(instrument, dict) and isinstance(instrument.get("receiver_noise"), dict):
        return noise_hash(instrument.get("receiver_noise"))
    return noise_hash(overrides.get("noise", {}))


# ---------------------------------------------------------------------------
# 派生 run identity
# ---------------------------------------------------------------------------
def compose_run_identity(
    fog_key: str,
    haze_key: str,
    haze_mueller_key: str,
    rain_key: str,
    instrument_hash_value: str,
    noise_hash_value: str | None = None,
) -> str:
    """Run identity. When noise is disabled, keep the legacy 5-tuple identity."""
    parts = [fog_key, haze_key, haze_mueller_key, rain_key, instrument_hash_value]
    if noise_hash_value:
        parts.append(noise_hash_value)
    joined = "|".join(parts)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# argparse 默认值 — 必须与 lidar_1d_simulation.parse_args() 字段对应
# 这里只暴露 hash 函数会读到的字段;其它字段(如 julia_cmd)不在 hash 内
# ---------------------------------------------------------------------------
DEFAULT_ARGS: dict = {
    "fog_grid": 4001,
    "rain_grid": 4001,
    "haze_mie_reference_grid": 401,
    "haze_mueller_grid": 401,
    "mueller_angles": 1200,
    "mueller_forward_res_deg": 0.01,
    "mueller_forward_max_deg": 2.0,
    "mueller_back_res_deg": 0.02,
    "mueller_back_min_deg": 175.0,
    "tmatrix_solver": "iitm_only",
    "tmatrix_n_radii": 129,
    "tmatrix_nr": 64,
    "tmatrix_ntheta": 96,
    "stokes_max_orders": 8,
    "discrete_angle_bins": 721,
    "molecular_depol_ratio": 0.00365,
    "precision_profile": "high",
    "wavelength_nm": None,
    "alpha_mol": None,
    "beta_mol": None,
}


# ---------------------------------------------------------------------------
# 精度预设 — 必须与 temp/lidar_1d/make_final_figures.py PRECISION_PRESETS 一致
# 这里转成 dict-of-dict 形式以便与 DEFAULT_ARGS 合并;键用 underscore 而非 dash
# ---------------------------------------------------------------------------
PRECISION_PRESETS: dict[str, dict] = {
    "fast": {
        "fog_grid": 1001,
        "rain_grid": 4001,
        "haze_mie_reference_grid": 1001,
        "tmatrix_solver": "iitm_only",
        "tmatrix_n_radii": 65,
        "tmatrix_nr": 32,
        "tmatrix_ntheta": 48,
        "tmatrix_timeout_s": 3600,
        "discrete_angle_bins": 181,
        "stokes_max_orders": 4,
        "mueller_angles": 400,
        "haze_mueller_grid": 201,
    },
    "medium": {
        "fog_grid": 2001,
        "rain_grid": 8001,
        "haze_mie_reference_grid": 2001,
        "tmatrix_solver": "iitm_only",
        "tmatrix_n_radii": 97,
        "tmatrix_nr": 48,
        "tmatrix_ntheta": 72,
        "tmatrix_timeout_s": 7200,
        "discrete_angle_bins": 361,
        "stokes_max_orders": 6,
    },
    "high": {
        "fog_grid": 4001,
        "rain_grid": 16001,
        "haze_mie_reference_grid": 4001,
        "tmatrix_solver": "iitm_only",
        "tmatrix_n_radii": 129,
        "tmatrix_nr": 64,
        "tmatrix_ntheta": 96,
        "tmatrix_timeout_s": 25200,
        "discrete_angle_bins": 721,
        "stokes_max_orders": 8,
    },
}


# CLI key (dash form) → namespace attr (underscore form) 别名表
_CLI_TO_ATTR = {
    "molecular-depol-ratio": "molecular_depol_ratio",
    "wavelength-nm": "wavelength_nm",
    "alpha-mol": "alpha_mol",
    "beta-mol": "beta_mol",
    "system-constant": "system_constant",
    "fog-grid": "fog_grid",
    "rain-grid": "rain_grid",
    "haze-mie-reference-grid": "haze_mie_reference_grid",
    "haze-mueller-grid": "haze_mueller_grid",
    "mueller-angles": "mueller_angles",
    "tmatrix-solver": "tmatrix_solver",
    "tmatrix-n-radii": "tmatrix_n_radii",
    "tmatrix-nr": "tmatrix_nr",
    "tmatrix-ntheta": "tmatrix_ntheta",
    "precision-profile": "precision_profile",
}


def args_from_precision(precision: str, overrides: dict | None = None) -> argparse.Namespace:
    """从精度档 + 前端 overrides 构造一个等价于 lidar_1d_simulation.parse_args() 的 namespace。

    overrides 形状与 ``param_overrides.json`` 一致:``{"cli": {...}, "fog": {...}, ...}``。
    其中 cli 字段使用 dash-form key(与 demo_ui ``_collect_overrides`` 一致)。
    """
    precision = precision if precision in PRECISION_PRESETS else "fast"
    ns = argparse.Namespace(**DEFAULT_ARGS)
    ns.precision_profile = precision

    for k, v in PRECISION_PRESETS[precision].items():
        setattr(ns, k, v)

    cli_overrides = (overrides or {}).get("cli", {}) if overrides else {}
    for k, v in cli_overrides.items():
        attr = _CLI_TO_ATTR.get(k, k.replace("-", "_"))
        setattr(ns, attr, v)
    return ns
