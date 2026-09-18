"""Standard-library-only bridge; safe to import in the GUI environment."""
import csv
import hashlib
import json
import math
from pathlib import Path

ALGORITHM_VERSION = 'wind-receiver-3'
MODES = {
    'unified_event_mode': '同一事件源（理想单距离）',
    'matlab_compat_raw': 'MATLAB 原始样例（独立于场景功率）',
    'matlab_compat_corrected': 'MATLAB 修正版（显式光子率）',
}
SCENES = {
    'radiation_fog': '辐射雾', 'advection_fog': '平流雾',
    'light_rain': '小雨', 'moderate_rain': '中雨', 'heavy_rain': '大雨',
    'urban_industrial_haze': '城市/工业型霾', 'rural_continental_haze': '乡村/大陆背景型霾',
    'dust_desert_haze': '沙尘型霾', 'maritime_haze': '海洋性霾',
    'layered_atmosphere': '分层大气',
}
# First-order scene response model.  The width is the radial-velocity
# standard deviation of scatterers inside one receive gate.  It broadens the
# Doppler line without inventing a scene-dependent mean wind direction.
SCENE_DOPPLER = {
    'radiation_fog': dict(velocity_std_m_s=0.15),
    'advection_fog': dict(velocity_std_m_s=0.25),
    'light_rain': dict(velocity_std_m_s=0.45),
    'moderate_rain': dict(velocity_std_m_s=0.75),
    'heavy_rain': dict(velocity_std_m_s=1.10),
    'urban_industrial_haze': dict(velocity_std_m_s=0.35),
    'rural_continental_haze': dict(velocity_std_m_s=0.25),
    'dust_desert_haze': dict(velocity_std_m_s=0.65),
    'maritime_haze': dict(velocity_std_m_s=0.30),
    'layered_atmosphere': dict(velocity_std_m_s=1.25),
}
DEFAULTS = dict(reference_mhz=80.0, velocity_m_s=0.0, lo_power_w=1e-10,
                background_power_w=0.0, visibility=1.0, split=0.5,
                dt_ns=0.5, window_ns=5000.0, prf_hz=10000.0, pulses=200,
                efficiency=0.35, dead_ns=30.0, dark_hz=5.0,
                afterpulse_probability=0.3, afterpulse_tau_ns=100.0,
                max_lag_ns=500.0, seed=20260917, search_min_mhz=10.0,
                search_max_mhz=210.0, min_snr_db=-6.0,
                wavelength_nm=1550.0, local_rate_hz=1e5,
                signal_rate_hz=1e5)

COMPAT_DEFAULTS = dict(noise_amp=0.002, afterpulse_min=1e-6)


def mode_defaults(mode):
    if mode not in MODES:
        raise ValueError('未知接收器模式')
    values = dict(DEFAULTS)
    if mode != 'unified_event_mode':
        values.update(COMPAT_DEFAULTS, reference_mhz=80.0, velocity_m_s=0.0,
                      efficiency=0.35, dead_ns=30.0,
                      afterpulse_probability=0.3, afterpulse_tau_ns=100.0,
                      dark_hz=5.0, prf_hz=10000.0,
                      search_min_mhz=10.0, search_max_mhz=210.0)
    if mode == 'matlab_compat_raw':
        # Exact simulation_fft_end.m sample: T_total=5 us and one pulse.
        values.update(window_ns=5000.0, pulses=1)
    elif mode == 'matlab_compat_corrected':
        # The receive gate spans the full MATLAB record.  Keep the
        # time-difference histogram over that gate; a 500 ns lag cap would
        # leave almost every sparse-photon pair outside the plotted range.
        values.update(window_ns=5000.0, pulses=200, max_lag_ns=5000.0)
    return values


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, allow_nan=False,
                                     ensure_ascii=False).encode('utf-8')).hexdigest()


def read_source(data_dir, summary_path, run_id, scene, distance_m):
    if scene not in SCENES:
        raise ValueError('不支持的大气类型')
    distance_m = float(distance_m)
    if not math.isfinite(distance_m) or distance_m <= 0:
        raise ValueError('测量距离必须为有限正数')
    rain = scene in ('light_rain', 'moderate_rain', 'heavy_rain')
    path = Path(data_dir) / ('rain_power.csv' if rain else f'{scene}_power.csv')
    column = f'{scene}_power_signal_raw' if rain else 'power_signal_raw'
    try:
        raw = path.read_bytes()
        reader = csv.DictReader(raw.decode('utf-8-sig').splitlines())
        if column not in (reader.fieldnames or []) or 'range_m' not in reader.fieldnames:
            raise ValueError('来源缺少无噪声功率字段，请重新计算光学结果')
        samples = [(float(row['range_m']), float(row[column])) for row in reader]
        global_meta = json.loads(Path(summary_path).read_text(encoding='utf-8'))['global']
        wavelength = float(global_meta['wavelength_nm']) * 1e-9
        pulse_width = float(global_meta['instrument_parameters']['pulse_width_s'])
    except (OSError, KeyError) as exc:
        raise ValueError(f'来源缺少数据或仪器元数据，请先重算：{exc}') from exc
    if (not samples or any(not math.isfinite(r) or not math.isfinite(p) or r <= 0 or p < 0
                           for r, p in samples)
            or not math.isfinite(wavelength) or wavelength <= 0
            or not math.isfinite(pulse_width) or pulse_width <= 0):
        raise ValueError('来源距离、功率或仪器元数据无效')
    if not min(r for r, _ in samples) <= distance_m <= max(r for r, _ in samples):
        raise ValueError('测量距离超出来源曲线范围')
    actual, power = min(samples, key=lambda pair: abs(pair[0] - distance_m))
    source = dict(run_id=run_id, scene=scene, requested_range_m=distance_m,
                  actual_range_m=actual, signal_power_w=power, wavelength_m=wavelength,
                  pulse_width_s=pulse_width, csv_sha256=hashlib.sha256(raw).hexdigest(),
                  power_column=column)
    source['digest'] = digest(source)
    return source


def make_request(source, settings):
    mode = settings.get('mode', 'unified_event_mode')
    defaults = mode_defaults(mode)
    unknown = set(settings) - set(defaults) - {'mode'}
    if unknown:
        raise ValueError(f'未知测风参数：{sorted(unknown)}')
    request = dict(algorithm_version=ALGORITHM_VERSION, source=source,
                   settings={**defaults, **settings, 'mode': mode})
    request['key'] = digest(request)
    return request
