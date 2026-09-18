"""Single-range ideal coherent receiver; both plots use the same SPAD events."""
import math
import time

import numpy as np

from spad_detector import detect, positive_lag_histogram
from wind_io import ALGORITHM_VERSION, DEFAULTS, SCENE_DOPPLER

H = 6.62607015e-34
C = 299792458.0


def validate(source, settings):
    p = {**DEFAULTS, **settings}
    if any(not math.isfinite(float(value)) for value in p.values()):
        raise ValueError('测风参数必须为有限数值')
    for name in ('dt_ns', 'window_ns', 'prf_hz', 'afterpulse_tau_ns', 'max_lag_ns'):
        if p[name] <= 0:
            raise ValueError(f'{name} 必须大于零')
    for name in ('lo_power_w', 'background_power_w', 'dead_ns', 'dark_hz', 'reference_mhz'):
        if p[name] < 0:
            raise ValueError(f'{name} 不可为负')
    for name in ('visibility', 'efficiency', 'afterpulse_probability'):
        if not 0 <= p[name] <= 1:
            raise ValueError(f'{name} 必须在 0～1 内')
    if not 0 < p['split'] < 1:
        raise ValueError('分光比例必须在 0～1 之间')
    if int(p['pulses']) != p['pulses'] or not 1 <= p['pulses'] <= 20000:
        raise ValueError('累计脉冲数必须为 1～20000 的整数')
    if int(p['seed']) != p['seed'] or not 0 <= p['seed'] < 2**32:
        raise ValueError('随机种子必须为 0～2^32-1 的整数')
    for name in ('wavelength_m', 'pulse_width_s', 'actual_range_m', 'signal_power_w'):
        if not math.isfinite(source[name]) or source[name] < 0 or (name != 'signal_power_w' and source[name] == 0):
            raise ValueError(f'来源 {name} 无效')
    dt = p['dt_ns'] * 1e-9
    n = int(math.floor(p['window_ns'] / p['dt_ns'] + 1e-9))
    lag = int(math.floor(p['max_lag_ns'] / p['dt_ns'] + 1e-9))
    if p['window_ns'] * 1e-9 > source['pulse_width_s'] * (1 + 1e-9):
        raise ValueError('有效接收窗口不得超过来源脉宽；不能把多个脉冲拼成相干长信号')
    if n < 32 or n > 8192 or lag > 8192 or (n + lag) * p['pulses'] > 25_000_000:
        raise ValueError('任务规模超限：窗口至少 32 点，窗口/延迟各最多 8192 点，总采样不超过 2500 万')
    # The lag accumulator is memory bounded but dense events can still be costly.
    max_events = (n + lag) / max(1, math.ceil(p['dead_ns'] / p['dt_ns']))
    optical_rate = ((p['lo_power_w'] + source['signal_power_w'] + p['background_power_w']
                     + 2 * p['visibility'] * math.sqrt(p['lo_power_w'] * source['signal_power_w']))
                    * source['wavelength_m'] / (H * C) * p['split'] * p['efficiency'] + p['dark_hz'])
    expected_events = optical_rate * (n + lag) * dt * (1 + p['afterpulse_probability'])
    if min(max_events, expected_events) * (lag + 1) * p['pulses'] > 300_000_000:
        raise ValueError('时间差统计工作量过大，请降低脉冲数、延迟上限或光功率')
    # Isolated-gate approximation: omit exponentially tiny AP tails between pulses.
    quiet = max(p['dead_ns'], 20 * p['afterpulse_tau_ns']) * 1e-9
    if 2 * source['actual_range_m'] / C + (n + lag) * dt + quiet >= 1 / p['prf_hz']:
        raise ValueError('重复频率过高：回波往返时间、接收门和恢复余量超过脉冲周期')
    low, high = p['search_min_mhz'] * 1e6, p['search_max_mhz'] * 1e6
    beat = p['reference_mhz'] * 1e6 + 2 * p['velocity_m_s'] / source['wavelength_m']
    if not 0 < low < high < 0.5 / dt or not 0 < beat < 0.5 / dt:
        raise ValueError('搜索频带或合成拍频超出正频率奈奎斯特范围')
    if low < 2 / (n * dt) or high - low < 8 / (n * dt):
        raise ValueError('搜索带须远离直流且至少覆盖 8 个物理频率格；请调整窗口或频带')
    p['pulses'], p['seed'] = int(p['pulses']), int(p['seed'])
    return p, n, lag, dt, beat


def estimate_spectrum(frequency, spectrum, settings, wavelength_m, event_count, noise_reference=None,
                      spectrum_variance=None, reference_variance=None):
    """Blind peak search: deliberately has no access to the simulation truth."""
    low, high = settings['search_min_mhz'] * 1e6, settings['search_max_mhz'] * 1e6
    band = np.flatnonzero((frequency >= low) & (frequency <= high))
    result = dict(valid=False, reason='事件不足', velocity_m_s=None, snr_db=None,
                  peak_hz=None, noise_power_per_bin=None)
    if len(band) < 9 or event_count < 100:
        return result
    reference = np.ones_like(spectrum) if noise_reference is None else np.asarray(noise_reference)
    if np.any(reference[band] <= 0):
        result['reason'] = '参考噪声统计不足'
        return result
    whitened = np.divide(spectrum, reference, out=np.zeros_like(spectrum), where=reference > 0)
    peak = int(band[np.argmax(whitened[band])])
    # Hann main lobe: integrate peak +/- 1 physical bin, guard +/- 3 bins.
    noise_bins = band[np.abs(band - peak) > 3]
    if len(noise_bins) < 5:
        result['reason'] = '噪声带不足'
        return result
    scale = float(np.mean(whitened[noise_bins]))
    noise = float(np.mean(reference[max(0, peak-1):peak+2])) * scale
    result['noise_power_per_bin'] = noise
    if noise <= 0:
        result['reason'] = '无法估计噪声底'
        return result
    power = float(np.sum(spectrum[max(0, peak-1):peak+2]))
    excess = power - 3 * noise
    if excess <= 0:
        result['reason'] = '未检出高于噪声底的谱峰'
        return result
    snr = 10 * math.log10(excess / (3 * noise))
    result['snr_db'] = snr
    if spectrum_variance is not None and reference_variance is not None:
        # Cauchy bound accounts conservatively for correlated adjacent Hann bins.
        region = slice(max(0, peak-1), peak+2)
        variance = 3 * np.sum(spectrum_variance[region] + scale**2 * reference_variance[region])
        scale_error = (3 * noise)**2 * 4 / (settings['pulses'] * len(noise_bins))
        stderr = math.sqrt(max(0, float(variance) + scale_error))
        result['detection_z'] = excess / stderr if stderr > 0 else None
        if stderr <= 0 or excess < 6 * stderr:
            result['reason'] = '谱峰统计显著性不足（需超过 6 倍标准误差）'
            return result
    if peak <= band[0] + 1 or peak >= band[-1] - 1:
        result['reason'] = '谱峰位于搜索边界，请检查频带'
        return result
    if snr < settings['min_snr_db']:
        result['reason'] = '频谱信噪比不足'
        return result
    # Sub-bin interpolation improves grid bias, not physical resolution.
    a, b, c = np.log(np.maximum(whitened[peak-1:peak+2], np.finfo(float).tiny))
    delta = float(np.clip(0.5 * (a-c) / (a-2*b+c), -0.5, 0.5)) if a-2*b+c else 0.0
    peak_hz = float(frequency[peak] + delta * (frequency[1] - frequency[0]))
    result.update(valid=True, reason='检出谱峰（理想单距离模型）', peak_hz=peak_hz,
                  velocity_m_s=(peak_hz - settings['reference_mhz'] * 1e6) * wavelength_m / 2)
    return result


def simulate(source, settings, progress=None):
    settings = dict(settings)
    mode = settings.pop('mode', 'unified_event_mode')
    if mode in ('matlab_compat_raw', 'matlab_compat_corrected'):
        from wind_matlab_receiver import simulate_compat
        return simulate_compat(source, settings, mode, progress)
    if mode != 'unified_event_mode':
        raise ValueError('未知接收器模式')
    start = time.perf_counter()
    p, n, lag, dt, beat = validate(source, settings)
    phase_rng, rng1, rng2, noise_rng, scene_rng = [
        np.random.default_rng(s) for s in np.random.SeedSequence(p['seed']).spawn(5)
    ]
    t = (np.arange(n) + 0.5) * dt
    eph = H * C / source['wavelength_m']
    signal = source['signal_power_w']
    window = np.hanning(n)
    normalization = float(np.sum(window**2))
    spectrum = np.zeros(n // 2 + 1)
    reference_spectrum = np.zeros_like(spectrum)
    spectrum_squared = np.zeros_like(spectrum)
    reference_squared = np.zeros_like(spectrum)
    histogram = np.zeros(lag + 1, dtype=np.int64)
    diagnostics = [dict(events=0, illuminated_events=0), dict(events=0, illuminated_events=0)]
    reference_mean = np.full(n, (p['lo_power_w'] + signal + p['background_power_w']) * dt / eph * p['split'])
    scene_name = source.get('scene')
    scene_model = SCENE_DOPPLER.get(scene_name, dict(velocity_std_m_s=0.0))
    scene_velocity_std = float(scene_model['velocity_std_m_s'])

    def event_spectrum(events):
        train = np.zeros(n)
        train[events[events < n]] = 1.0
        train = (train - np.mean(train)) * window
        # Wiener-Khinchin: |FFT(x)|² equals FFT(circular autocorrelation(x)).
        ps = np.abs(np.fft.rfft(train))**2 / normalization
        ps[1:-1 if n % 2 == 0 else None] *= 2
        return ps

    for pulse in range(p['pulses']):
        phase = phase_rng.uniform(0, 2 * np.pi)
        # A receive gate contains scatterers with a distribution of radial
        # velocities.  Sampling one velocity per pulse is an efficient
        # approximation to a broadened Doppler line and keeps photon-level
        # detection unchanged.
        pulse_velocity = p['velocity_m_s'] + scene_rng.normal(0.0, scene_velocity_std)
        pulse_beat = p['reference_mhz'] * 1e6 + 2 * pulse_velocity / source['wavelength_m']
        # Exact integral of cosine per bin, rectangular echo limited to n*dt.
        mixed = np.full(n + lag, p['lo_power_w'] + p['background_power_w'], dtype=float)
        mixed[:n] += signal + 2 * p['visibility'] * math.sqrt(p['lo_power_w'] * signal) * np.sinc(pulse_beat * dt) * np.cos(2*np.pi*pulse_beat*t + phase)
        mean = np.maximum(mixed, 0) * dt / eph
        events = []
        for channel, (fraction, rng) in enumerate(((p['split'], rng1), (1-p['split'], rng2))):
            ev, stats = detect(mean * fraction, rng, dt_s=dt, efficiency=p['efficiency'],
                               dead_s=p['dead_ns']*1e-9, dark_hz=p['dark_hz'],
                               afterpulse_probability=p['afterpulse_probability'],
                               afterpulse_tau_s=p['afterpulse_tau_ns']*1e-9)
            events.append(ev)
            diagnostics[channel]['events'] += len(ev)
            diagnostics[channel]['illuminated_events'] += int(np.sum(ev < n))
            for key, value in stats.items():
                diagnostics[channel][key] = diagnostics[channel].get(key, 0) + value
        current_spectrum = event_spectrum(events[0])
        spectrum += current_spectrum
        spectrum_squared += current_spectrum**2
        # Independent constant-light calibration captures dead-time/AP spectral colour.
        # This uses mean optical power, not velocity or beat frequency.
        reference_events, _ = detect(reference_mean, noise_rng, dt_s=dt, efficiency=p['efficiency'],
                                     dead_s=p['dead_ns']*1e-9, dark_hz=p['dark_hz'],
                                     afterpulse_probability=p['afterpulse_probability'],
                                     afterpulse_tau_s=p['afterpulse_tau_ns']*1e-9)
        current_reference = event_spectrum(reference_events)
        reference_spectrum += current_reference
        reference_squared += current_reference**2
        histogram += positive_lag_histogram(events[0], events[1], lag)
        if progress and (pulse + 1) % 200 == 0:
            progress(pulse + 1, p['pulses'])
    spectrum /= p['pulses']
    reference_spectrum /= p['pulses']
    frequency = np.fft.rfftfreq(n, dt)
    spectrum_variance = np.maximum(0, spectrum_squared / p['pulses'] - spectrum**2) / max(1, p['pulses'] - 1)
    reference_variance = np.maximum(0, reference_squared / p['pulses'] - reference_spectrum**2) / max(1, p['pulses'] - 1)
    summary = estimate_spectrum(frequency, spectrum, p, source['wavelength_m'], diagnostics[0]['illuminated_events'],
                                reference_spectrum, spectrum_variance, reference_variance)
    if p['pulses'] < 32:
        summary.update(valid=False, velocity_m_s=None, peak_hz=None, reason='统计脉冲不足（至少 32 次）')
    summary['truth_m_s'] = p['velocity_m_s']
    summary['error_m_s'] = summary['velocity_m_s'] - p['velocity_m_s'] if summary['valid'] else None
    return dict(algorithm_version=ALGORITHM_VERSION, mode=mode, source=source,
                settings={**p, 'mode': mode}, summary=summary,
                spectrum=dict(frequency_hz=frequency.tolist(), power=spectrum.tolist(), noise_reference=reference_spectrum.tolist()),
                histogram=dict(lag_ns=(np.arange(lag+1)*p['dt_ns']).tolist(), counts=histogram.tolist()),
                diagnostics=dict(channels=diagnostics, elapsed_s=time.perf_counter()-start,
                                 photon_energy_j=eph, signal_photon_rate_hz=signal/eph,
                                 signal_mean_per_bin=signal*dt/eph,
                                 scene_doppler_model=scene_model,
                                 effective_window_ns=n*p['dt_ns'], frequency_resolution_hz=1/(n*dt),
                                 velocity_resolution_m_s=source['wavelength_m']/(2*n*dt)),
                conventions=dict(spectrum='mean-removed Hann, averaged per-pulse one-sided power; FFT of circular autocorrelation',
                                 snr='independent constant-light detector calibration; whitened peak search; local calibrated noise scaled by mean ratio outside +/-3 bins; 10log10((sum(peak +/-1 bin)-noise_band_power)/noise_band_power)',
                                 lag='t_ch1 - t_ch2; within each receive gate only',
                                 model='ideal single range with scene-dependent radial-velocity broadening; LO/background continue through histogram tail; independent gates; no AP cascade; AP tails beyond gate discarded'))
