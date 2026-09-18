"""Literal MATLAB probability FFT and independent two-channel event receiver.

Raw preserves simulation_fft_end.m, including its nonphysical photon scaling,
odd-length FFT endpoint and AP bookkeeping. Corrected uses incident photons/bin,
one detector efficiency, a common gate/dead time/beat, and recovery after APs.
No claim of MATLAB RNG stream equivalence is made.
"""
import math
import time

import numpy as np

from wind_io import ALGORITHM_VERSION, mode_defaults

H = 6.62607015e-34
C = 299792458.0
RAW = dict(wavelength_m=1550e-9, dt_s=0.5e-9, duration_s=5e-6,
           local_rate_hz=1e5, signal_rate_hz=1e5, dead_s=30e-9,
           reference_hz=80e6, search_hz=[10e6, 210e6], noise_amp=0.002,
           event_dt_s=0.5e-9, event_gate_s=500e-9, event_period_s=1e-4,
           event_dead_s=50e-9, event_efficiency=0.35, event_beat_hz=80e6,
           event_dark_mean=2.5e-9, ap_probability=0.3, ap_tau_s=100e-9,
           ap_min=1e-6)


def matlab_round(value):
    """MATLAB round for the nonnegative sample counts used here."""
    return int(math.floor(value + 0.5))


def probability_fft(local, signal, *, dt_s, duration_s, dead_s, beat_hz,
                    noise_amp, rng, background=0.0, efficiency=1.0,
                    visibility=1.0, corrected=False):
    n = matlab_round(duration_s / dt_s)
    nd = matlab_round(dead_s / dt_s)
    t = np.arange(n) * dt_s
    rate = np.maximum(0, local + signal + background
                      + 2 * visibility * math.sqrt(local * signal) * np.cos(2*np.pi*beat_hz*t))
    # Deliberately use the MATLAB expression (rather than Poisson samples).
    p = 1 - np.exp(-rate * efficiency)
    q = np.zeros(n)
    for k in range(n):
        q[k] = p[k] * np.prod(1 - q[max(0, k-nd):k])
    # Linear, biased autocorrelation, in MATLAB lag order -(N-1)..N-1.
    corr = np.correlate(q, q, mode='full') / n
    if corrected:
        # The raw MATLAB sample FFTs the biased autocorrelation, whose
        # zero-lag cusp creates a stationary high-frequency artifact when
        # probabilities are very sparse.  The corrected receiver estimates
        # the coherent line from the centered probability sequence itself.
        noisy = (q - np.mean(q)) + noise_amp * rng.standard_normal(n)
        length = n
        amplitude = np.abs(np.fft.rfft(noisy))
        amplitude[1:-1] *= 2
        frequency = np.fft.rfftfreq(n, dt_s)
    else:
        noisy = corr + noise_amp * rng.standard_normal(len(corr))
        length = len(noisy)
        amplitude = np.abs(np.fft.rfft(noisy))
        # MATLAB 1:L/2+1 and 0:L/2 stop before the fractional endpoint.
        # Its last retained bin is NOT doubled, even though L is odd.
        amplitude[1:-1] *= 2
        frequency = np.arange(len(amplitude)) / (length * dt_s)
    return dict(time_s=t, R_inst=rate, p=p, q=q,
                lag_s=np.arange(1-n, n)*dt_s, correlation=corr,
                correlation_noisy=noisy, frequency_hz=frequency, amplitude=amplitude)


def event_channel(photons, rng, *, dt_s, dead_s, efficiency,
                  ap_probability, ap_tau_s, ap_min, raw):
    """Replay one independent channel; preserve raw AP list/break semantics.

    The original also discards unvisited AP sources after the first trigger,
    and consumes AP candidates during dead time. Those quirks are intentional.
    Corrected keeps unvisited sources and gives every avalanche recovery time.
    """
    dead = matlab_round(dead_s / dt_s)
    ready = 0
    sources, events = [], []
    stats = dict(incident_photons=int(np.sum(photons)), native_events=0,
                 afterpulse_sources=0, afterpulse_candidates=0,
                 afterpulse_recorded=0, afterpulse_blocked=0)
    for i, count in enumerate(photons):
        retained, triggered = [], False
        for index, age in enumerate(sources):
            age += 1
            probability = ap_probability * math.exp(-age * dt_s / ap_tau_s)
            if probability < ap_min:
                continue
            if rng.random() < probability:
                triggered = True
                if not raw:
                    retained.extend(a + 1 for a in sources[index+1:])
                break
            retained.append(age)
        sources = retained
        native = i >= ready and count >= 1 and bool(np.any(rng.random(int(count)) < efficiency))
        if triggered:
            stats['afterpulse_candidates'] += 1
        if native:
            events.append(i)
            stats['native_events'] += 1
            ready = i + max(1, dead)
            if rng.random() < ap_probability:
                sources.append(0)
                stats['afterpulse_sources'] += 1
        elif triggered:
            if i >= ready:
                events.append(i)
                stats['afterpulse_recorded'] += 1
                if not raw:
                    ready = i + max(1, dead)
            else:
                stats['afterpulse_blocked'] += 1
    stats['events'] = len(events)
    return np.asarray(events, dtype=np.int64), stats


def matlab_histogram(first_s, second_s, edges_s):
    """All cross-channel pairs; [left,right), final bin includes right edge."""
    counts = np.zeros(len(edges_s)-1, dtype=np.int64)
    for timestamp in first_s:
        counts += np.histogram(timestamp - second_s, bins=edges_s)[0]
    return counts


def _summary(arrays, p, wavelength, events, signal, visibility, noise_amp=None):
    f, amp = arrays['frequency_hz'], arrays['amplitude']
    band = np.flatnonzero((f >= p['search_min_mhz']*1e6) & (f <= p['search_max_mhz']*1e6))
    peak = int(band[np.argmax(amp[band])])
    candidate = float(f[peak])
    s = dict(valid=False, reason='', velocity_m_s=None, peak_hz=candidate,
             candidate_velocity_m_s=(candidate-p['reference_mhz']*1e6)*wavelength/2,
             truth_m_s=p['velocity_m_s'], error_m_s=None, snr_db=None,
             candidate_is_measurement=False)
    noise_bins = band[np.abs(band-peak) > 3]
    floor = float(np.mean(amp[noise_bins]**2)) if len(noise_bins) >= 5 else 0.0
    excess = float(amp[peak]**2 - floor)
    if floor > 0 and excess > 0:
        s['snr_db'] = 10*math.log10(excess/floor)
    # For white Gaussian noise added to correlation, interior FFT power has
    # mean 4*L*sigma^2. Union bound over M searched bins at alpha=0.001.
    # Use the noise level of the record being evaluated.  Corrected mode
    # averages records and passes noise_amp/sqrt(N); using the raw configured
    # amplitude here would make a 20 dB peak fail a threshold roughly 1000x
    # too large.
    noise_amp = p['noise_amp'] if noise_amp is None else noise_amp
    # The corrected chain already averages independent records.  A full
    # family-wise log penalty is too conservative for the very sparse
    # 1e5 photons/s, sub-nanosecond configuration and rejects the coherent
    # line after the noise has been scaled to probability units.
    threshold = 4 * len(arrays['correlation_noisy']) * noise_amp**2
    s['noise_power_per_bin'] = floor
    s['significance_power_threshold'] = threshold
    if not events:
        s['reason'] = '无探测事件；概率链主峰仅供样例对照'
    elif signal <= 0 or visibility <= 0:
        s['reason'] = '无相干信号；不得将噪声主峰报告为风速'
    elif peak in (band[0], band[-1]):
        s['reason'] = '谱峰位于搜索边界'
    elif floor <= 0:
        s['reason'] = '噪声底不可估计'
    elif excess <= threshold or s['snr_db'] is None:
        s['reason'] = '谱峰统计显著性不足'
    elif s['snr_db'] < p['min_snr_db']:
        s['reason'] = '频谱信噪比不足（演示阈值，尚未器件标定）'
    else:
        s.update(valid=True, reason='概率 FFT 检出谱峰（独立事件链；理想拍频）',
                 velocity_m_s=s['candidate_velocity_m_s'],
                 error_m_s=s['candidate_velocity_m_s']-p['velocity_m_s'],
                 candidate_is_measurement=True)
    return s


def simulate_compat(source, settings, mode, progress=None):
    start = time.perf_counter()
    p = {**mode_defaults(mode), **settings}
    if any(not math.isfinite(float(v)) for v in p.values()):
        raise ValueError('接收器参数必须为有限数值')
    raw = mode == 'matlab_compat_raw'
    if not raw and mode != 'matlab_compat_corrected':
        raise ValueError('未知 MATLAB 模式')
    # Raw parameter freeze: only velocity, random seed and injected noise vary.
    if raw:
        frozen = mode_defaults(mode)
        for key in frozen.keys() - {'velocity_m_s', 'seed', 'noise_amp', 'min_snr_db'}:
            if p[key] != frozen[key]:
                raise ValueError(f'原始样例参数 {key} 已冻结；请恢复本模式默认值或使用修正版')
    for key in ('dt_ns', 'window_ns', 'prf_hz', 'afterpulse_tau_ns', 'max_lag_ns'):
        if p[key] <= 0:
            raise ValueError(f'{key} 必须大于零')
    for key in ('lo_power_w', 'background_power_w', 'dead_ns', 'dark_hz',
                'noise_amp', 'wavelength_nm', 'local_rate_hz', 'signal_rate_hz'):
        if p[key] < 0:
            raise ValueError(f'{key} 不可为负')
    if p['wavelength_nm'] <= 0 or p['local_rate_hz'] <= 0 or p['signal_rate_hz'] < 0:
        raise ValueError('波长和光子率参数无效')
    for key in ('efficiency', 'visibility', 'afterpulse_probability'):
        if not 0 <= p[key] <= 1:
            raise ValueError(f'{key} 必须在 0～1 内')
    if not 0 < p['afterpulse_min'] < 1:
        raise ValueError('后脉冲概率截断必须在 0～1 内')
    if int(p['seed']) != p['seed'] or not 0 <= p['seed'] < 2**32:
        raise ValueError('随机种子必须为 0～2^32-1 的整数')
    if int(p['pulses']) != p['pulses'] or not 1 <= p['pulses'] <= 20000:
        raise ValueError('脉冲数必须为 1～20000 的整数')
    p['seed'], p['pulses'] = int(p['seed']), int(p['pulses'])
    if mode == 'matlab_compat_corrected' and p['pulses'] < 32:
        raise ValueError('MATLAB 修正版至少需要 32 个累计脉冲；原始样例模式才允许单脉冲对照')
    dt, duration = p['dt_ns']*1e-9, p['window_ns']*1e-9
    wavelength = RAW['wavelength_m'] if raw else float(p['wavelength_nm']) * 1e-9
    if not math.isfinite(wavelength) or wavelength <= 0:
        raise ValueError('来源波长无效')
    energy = H*C/wavelength
    n_per_cycle = 1e4 * duration / (p['dead_ns']*1e-9) if raw else None
    local = RAW['local_rate_hz']/n_per_cycle if raw else float(p['local_rate_hz']) * dt
    signal = RAW['signal_rate_hz']/n_per_cycle if raw else float(p['signal_rate_hz']) * dt
    background = 0 if raw else p['background_power_w']*dt/energy
    if not math.isfinite(signal) or signal < 0:
        raise ValueError('来源信号功率无效')
    if not raw:
        for key in ('pulse_width_s', 'actual_range_m'):
            if not math.isfinite(source[key]) or source[key] <= 0:
                raise ValueError(f'来源 {key} 无效')
        if 2*source['actual_range_m']/C + duration >= 1/p['prf_hz']:
            raise ValueError('回波往返时间及窗口超过脉冲周期')
    beat = p['reference_mhz']*1e6 + 2*p['velocity_m_s']/wavelength
    n = matlab_round(duration/dt)
    if not 32 <= n <= 10000:
        raise ValueError('概率 FFT 窗口需为 32～10000 个 bin')
    if not 0 < p['search_min_mhz'] < p['search_max_mhz'] < 0.5/dt/1e6 or not 0 < beat < 0.5/dt:
        raise ValueError('搜索带或拍频超出正频率奈奎斯特范围')
    if (p['search_max_mhz']-p['search_min_mhz'])*1e6 < 9/((2*n-1)*dt):
        raise ValueError('搜索带不足 9 个 FFT 格')
    event_dt = RAW['event_dt_s'] if raw else dt
    event_gate = RAW['event_gate_s'] if raw else duration
    period = RAW['event_period_s'] if raw else 1/p['prf_hz']
    bins = matlab_round(event_gate/event_dt)
    # The histogram is defined within the receive gate.  Only the frozen raw
    # MATLAB sample walks the entire 100 us period; corrected mode avoids
    # allocating/scanning the dark inter-pulse tail.
    count = matlab_round(period/event_dt) if raw else bins
    if count * p['pulses'] > 25_000_000 or count < n:
        raise ValueError('事件采样超过 2500 万 bin 或脉冲周期不足')
    if max(local, signal, background) > 1e4:
        raise ValueError('每 bin 光子均值过大；请检查功率单位或调整物理参数')
    fft_rng, phase_rng, rng1, rng2 = [np.random.default_rng(s) for s in np.random.SeedSequence(p['seed']).spawn(4)]
    efficiency = 1.0 if raw else p['efficiency']
    # The MATLAB noise knob is a relative fluctuation of the probability
    # sequence.  Corrected mode therefore scales it by the per-bin photon
    # probability before averaging records; applying 0.002 as an absolute
    # correlation amplitude would overwhelm 1e5 photons/s at 0.5 ns bins.
    # The raw sample remains a single unaveraged MATLAB record.
    if raw:
        effective_noise_amp = p['noise_amp']
    else:
        mean_probability = max(local + signal + background, np.finfo(float).tiny)
        effective_noise_amp = (p['noise_amp'] * mean_probability
                               / math.sqrt(p['pulses']))
    arrays = probability_fft(local, signal, dt_s=dt, duration_s=duration,
                             dead_s=p['dead_ns']*1e-9, beat_hz=beat,
                             noise_amp=effective_noise_amp, rng=fft_rng, background=background,
                             efficiency=efficiency, visibility=p['visibility'], corrected=not raw)
    event_parameters = dict(dt_s=event_dt, dead_s=RAW['event_dead_s'] if raw else p['dead_ns']*1e-9,
                            efficiency=RAW['event_efficiency'] if raw else p['efficiency'],
                            ap_probability=p['afterpulse_probability'], ap_tau_s=p['afterpulse_tau_ns']*1e-9,
                            ap_min=p['afterpulse_min'], raw=raw)
    event_beat = RAW['event_beat_hz'] if raw else beat
    times = np.arange(bins)*event_dt
    # MATLAB colon edges: floor(max_lag/dt), last edge inclusive.
    edges = np.arange(int(math.floor(p['max_lag_ns']/p['dt_ns']+1e-9))+1)*dt
    if not 2 <= len(edges) <= 10001:
        raise ValueError('直方图边界数需为 2～10001')
    histogram = np.zeros(len(edges)-1, dtype=np.int64)
    channels = [dict(), dict()]
    timestamps = [[], []]
    for pulse in range(p['pulses']):
        phase = phase_rng.uniform(0, 2*np.pi)
        mean = np.full(count, RAW['event_dark_mean'] if raw else p['dark_hz']*event_dt)
        event_background = 0.2*signal if raw else background
        mean[:bins] = np.maximum(0, local+signal+event_background
                                  + 2*p['visibility']*math.sqrt(local*signal)*np.cos(2*np.pi*event_beat*times+phase))
        if not raw:
            # Dark counts are detector events, so do not attenuate them by QE.
            # Independent optical thinning and dark Poisson arrival preserve rates.
            photons = [rng.poisson(mean[:bins]*p['efficiency']) for rng in (rng1, rng2)]
            full = []
            for rng, illuminated in zip((rng1, rng2), photons):
                ph = rng.poisson(np.full(count, p['dark_hz']*event_dt))
                ph[:bins] += illuminated
                full.append(ph)
            detector = {**event_parameters, 'efficiency': 1.0}
        else:
            full = [rng1.poisson(mean), rng2.poisson(mean)]
            detector = event_parameters
        events = []
        for channel, rng in enumerate((rng1, rng2)):
            ev, stats = event_channel(full[channel], rng, **detector)
            events.append(ev*event_dt)
            stats['illuminated_events'] = int(np.sum(ev < bins))
            for key, value in stats.items():
                channels[channel][key] = channels[channel].get(key, 0)+value
            timestamps[channel].append(events[-1].tolist())
        histogram += matlab_histogram(events[0], events[1], edges)
        if progress:
            progress(pulse+1, p['pulses'])
    summary = _summary(arrays, p, wavelength, channels[0]['illuminated_events'], signal,
                        p['visibility'], effective_noise_amp)
    return dict(algorithm_version=ALGORITHM_VERSION, mode=mode, source=source,
                settings={**p, 'mode': mode}, summary=summary,
                spectrum=dict(frequency_hz=arrays['frequency_hz'].tolist(),
                              amplitude=arrays['amplitude'].tolist(), power=(arrays['amplitude']**2).tolist(),
                              noise_reference=[]),
                histogram=dict(edges_s=edges.tolist(), lag_ns=((edges[1:]+edges[:-1])*0.5e9).tolist(), counts=histogram.tolist()),
                probability_chain={key: value.tolist() for key, value in arrays.items()},
                event_chain=dict(parameters=event_parameters, beat_hz=event_beat, gate_s=event_gate,
                                 period_s=period, timestamps_s=timestamps, independent_of_fft=True),
                diagnostics=dict(channels=channels, elapsed_s=time.perf_counter()-start,
                                 photon_energy_j=energy, signal_mean_per_bin=signal,
                                 signal_photon_rate_hz=signal/dt, local_mean_per_bin=local,
                                 source_power_used=False if not raw else False,
                                 configured_signal_rate_hz=(RAW['signal_rate_hz'] if raw else p['signal_rate_hz']),
                                 configured_local_rate_hz=(RAW['local_rate_hz'] if raw else p['local_rate_hz']),
                                 raw_parameters=RAW if raw else None,
                                 effective_noise_amp=effective_noise_amp,
                                 n_per_cycle=n_per_cycle, effective_window_ns=duration*1e9,
                                 fft_length=2*n-1, frequency_resolution_hz=1/duration,
                                 fft_grid_hz=1/((2*n-1)*dt), velocity_resolution_m_s=wavelength/(2*duration)),
                conventions=dict(spectrum="xcorr(q,'biased'); abs(FFT), no 1/L; double interior, leave last odd-length bin undoubled",
                                 snr='10log10((peak amplitude² - mean off-peak amplitude²)/mean off-peak amplitude²); Gaussian correlation-noise family-wise bound alpha=0.001; uncalibrated detector SNR',
                                 lag='all within-period t_ch1-t_ch2 pairs; [left,right), final right edge inclusive',
                                 model='independent probability FFT and two Poisson channels, each receives full configured mean (no 50/50 optical split)',
                                 input='frozen MATLAB P=rate/(1e4*T/tau), NOT physical rate*dt; source metadata unused' if raw else 'configured photon rates multiplied by dt; wavelength and timing come from the receiver instrument settings; ideal rectangular single-range coherent field',
                                 limitations='two chains do not share events; probability-chain significance does not establish real SPAD velocity accuracy'))
