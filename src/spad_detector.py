"""Binned SPAD: Poisson thinning, non-paralyzable recovery, no AP cascade."""
import heapq
import math

import numpy as np


def detect(mean_photons, rng, *, dt_s, efficiency, dead_s, dark_hz=0.0,
           afterpulse_probability=0.0, afterpulse_tau_s=50e-9):
    """Return registered bin indices and diagnostics for one isolated receive gate.

    QE is applied here, exactly once. Dark rate is already a detector rate.
    AP release times are rounded up; blocked candidates do not extend recovery.
    """
    mean = np.asarray(mean_photons, dtype=float)
    if (mean.ndim != 1 or np.any(~np.isfinite(mean)) or np.any(mean < 0)
            or dt_s <= 0 or not 0 <= efficiency <= 1 or dead_s < 0
            or dark_hz < 0 or not 0 <= afterpulse_probability <= 1
            or afterpulse_tau_s <= 0):
        raise ValueError("无效的 SPAD 参数")
    probability = -np.expm1(-(mean * efficiency + dark_hz * dt_s))
    native = np.flatnonzero(rng.random(mean.size) < probability)
    pending = [(int(i), 0) for i in native]
    heapq.heapify(pending)
    recovery = max(1, math.ceil(dead_s / dt_s - 1e-12))
    ready = 0
    events = []
    stats = dict(native_candidates=len(native), blocked=0, afterpulse_candidates=0,
                 afterpulse_recorded=0, afterpulse_outside_gate=0)
    while pending:
        index, is_ap = heapq.heappop(pending)
        if index < ready:
            stats['blocked'] += 1
            continue
        events.append(index)
        ready = index + recovery
        if is_ap:
            stats['afterpulse_recorded'] += 1
        elif rng.random() < afterpulse_probability:
            stats['afterpulse_candidates'] += 1
            delay = max(1, math.ceil(rng.exponential(afterpulse_tau_s) / dt_s))
            if index + delay < mean.size:
                heapq.heappush(pending, (index + delay, 1))
            else:
                stats['afterpulse_outside_gate'] += 1
    return np.asarray(events, dtype=int), stats


def positive_lag_histogram(first, second, max_lag_bins):
    """Count within-pulse pairs first-second in [0, max_lag_bins], bounded memory."""
    counts = np.zeros(max_lag_bins + 1, dtype=np.int64)
    for index in first:
        lo = np.searchsorted(second, index - max_lag_bins, side='left')
        hi = np.searchsorted(second, index, side='right')
        counts += np.bincount(index - second[lo:hi], minlength=len(counts))
    return counts
