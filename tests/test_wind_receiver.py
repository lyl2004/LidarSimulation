"""Wind physics and file-contract regressions; uses the existing mie environment."""
import json
import math
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from spad_detector import detect, positive_lag_histogram
from wind_io import DEFAULTS, make_request, mode_defaults, read_source
from wind_receiver import estimate_spectrum, simulate, validate
from wind_matlab_receiver import matlab_histogram, probability_fft


def source(power=1e-10):
    return dict(wavelength_m=1550e-9, pulse_width_s=1e-6,
                actual_range_m=1000.0, signal_power_w=power)


class DetectorTests(unittest.TestCase):
    def test_poisson_thinning_and_efficiency_once(self):
        events, _ = detect(np.full(200000, 0.4), np.random.default_rng(1),
                           dt_s=1e-9, efficiency=0.3, dead_s=0)
        self.assertAlmostEqual(len(events)/200000, -math.expm1(-0.12), delta=0.002)

    def test_dead_time_is_nonparalyzable_and_boundary_inclusive(self):
        events, _ = detect(np.full(21, 1000), np.random.default_rng(2),
                           dt_s=1e-9, efficiency=1, dead_s=5e-9)
        np.testing.assert_array_equal(events, [0, 5, 10, 15, 20])

    def test_afterpulse_has_no_cascade_and_respects_recovery(self):
        mean = np.zeros(1000)
        mean[0] = 1000
        events, stats = detect(mean, np.random.default_rng(3), dt_s=1e-9,
                               efficiency=1, dead_s=5e-9,
                               afterpulse_probability=1, afterpulse_tau_s=100e-9)
        self.assertEqual(stats['afterpulse_candidates'], 1)
        self.assertLessEqual(len(events), 2)
        self.assertTrue(np.all(np.diff(events) >= 5))
        _, disabled = detect(mean, np.random.default_rng(3), dt_s=1e-9,
                             efficiency=1, dead_s=5e-9)
        self.assertEqual(disabled['afterpulse_candidates'], 0)

    def test_histogram_matches_enumeration_and_empty(self):
        first, second = np.array([0, 2, 5, 9]), np.array([1, 2, 4, 9])
        expected = [sum(a-b == lag for a in first for b in second) for lag in range(5)]
        np.testing.assert_array_equal(positive_lag_histogram(first, second, 4), expected)
        self.assertEqual(positive_lag_histogram(np.array([], dtype=int), second, 4).sum(), 0)


class ReceiverTests(unittest.TestCase):
    def test_mode_defaults_follow_sample_and_statistics_requirements(self):
        self.assertEqual(mode_defaults('matlab_compat_raw')['window_ns'], 5000.0)
        self.assertEqual(mode_defaults('matlab_compat_raw')['pulses'], 1)
        self.assertEqual(mode_defaults('matlab_compat_corrected')['window_ns'], 1000.0)
        self.assertEqual(mode_defaults('matlab_compat_corrected')['pulses'], 200)
        with self.assertRaises(ValueError):
            simulate(source(), dict(mode='matlab_compat_corrected', pulses=1))

    def test_matlab_compatibility_modes_are_explicit_and_independent(self):
        compat = simulate(source(), dict(mode='matlab_compat_corrected', pulses=32,
                                         window_ns=1000, max_lag_ns=100))
        self.assertEqual(compat['mode'], 'matlab_compat_corrected')
        self.assertTrue(compat['event_chain']['independent_of_fft'])
        self.assertTrue(compat['diagnostics']['source_power_used'])
        self.assertEqual(len(compat['probability_chain']['p']), 2000)
        raw = simulate(dict(wavelength_m=1550e-9, pulse_width_s=5e-6,
                            actual_range_m=1000, signal_power_w=1e-10),
                       dict(mode='matlab_compat_raw'))
        self.assertEqual(raw['mode'], 'matlab_compat_raw')
        self.assertFalse(raw['diagnostics']['source_power_used'])
        self.assertIn('frozen MATLAB', raw['conventions']['input'])

    def test_corrected_gate_does_not_scale_with_dark_prf_tail(self):
        result = simulate(source(), dict(mode='matlab_compat_corrected', pulses=200,
                                         max_lag_ns=100))
        self.assertEqual(result['settings']['pulses'], 200)
        self.assertLess(result['diagnostics']['effective_window_ns'],
                        result['event_chain']['period_s'] * 1e9)
        self.assertLess(result['diagnostics']['effective_noise_amp'],
                        result['settings']['noise_amp'])
        self.assertTrue(result['summary']['valid'], result['summary'])

    def test_raw_event_histogram_keeps_matlab_fixed_beat(self):
        src = dict(wavelength_m=1550e-9, pulse_width_s=5e-6,
                   actual_range_m=1000, signal_power_w=1e-10)
        a = simulate(src, dict(mode='matlab_compat_raw', velocity_m_s=-20))
        b = simulate(src, dict(mode='matlab_compat_raw', velocity_m_s=20))
        self.assertEqual(a['histogram'], b['histogram'])

    def test_matlab_arrays_and_histogram_boundaries(self):
        out = probability_fft(0.1, 0.1, dt_s=1e-9, duration_s=32e-9,
                              dead_s=0, beat_hz=1e6, noise_amp=0,
                              rng=np.random.default_rng(1))
        self.assertEqual(len(out['q']), 32)
        self.assertEqual(len(out['frequency_hz']), 32)  # 2N-1 is odd; MATLAB endpoint
        np.testing.assert_array_equal(matlab_histogram(np.array([2e-9]),
            np.array([0, 1e-9, 2e-9]), np.array([-1e-9, 0, 1e-9, 2e-9])), [0, 1, 2])
    def test_known_signed_velocities_and_repeatability(self):
        for velocity in (-8, 0, 8):
            with self.subTest(velocity=velocity):
                settings = dict(velocity_m_s=velocity, pulses=300, dead_ns=0,
                                afterpulse_probability=0, max_lag_ns=100)
                result = simulate(source(), settings)
                self.assertTrue(result['summary']['valid'], result['summary'])
                self.assertAlmostEqual(result['summary']['velocity_m_s'], velocity, delta=0.8)
        a = simulate(source(), dict(pulses=30, max_lag_ns=100))
        b = simulate(source(), dict(pulses=30, max_lag_ns=100))
        self.assertEqual(a['summary'], b['summary'])
        self.assertEqual(a['spectrum'], b['spectrum'])
        self.assertEqual(a['histogram'], b['histogram'])

    def test_no_signal_false_positives_with_coloured_detector_noise(self):
        for seed in range(8):
            with self.subTest(seed=seed):
                result = simulate(source(0), dict(pulses=300, seed=seed, max_lag_ns=100))
                self.assertFalse(result['summary']['valid'], result['summary'])
                self.assertIsNone(result['summary']['velocity_m_s'])

    def test_no_events_is_invalid_and_json_safe(self):
        result = simulate(source(0), dict(pulses=5, lo_power_w=0, background_power_w=0, dark_hz=0))
        self.assertFalse(result['summary']['valid'])
        self.assertEqual(sum(result['histogram']['counts']), 0)
        json.dumps(result, allow_nan=False)

    def test_snr_power_definition_and_truth_independent_estimator(self):
        frequency = np.arange(101) * 2e6
        power = np.ones(101)
        power[39:42] += [3, 6, 3]
        settings = {**DEFAULTS, 'search_min_mhz': 20, 'search_max_mhz': 180}
        result = estimate_spectrum(frequency, power, settings, 1550e-9, 1000)
        self.assertTrue(result['valid'])
        self.assertAlmostEqual(result['snr_db'], 10*math.log10(4))
        settings['velocity_m_s'] = 999999
        self.assertEqual(result, estimate_spectrum(frequency, power, settings, 1550e-9, 1000))

    def test_odd_even_frequency_axis_and_energy_split(self):
        even = simulate(source(), dict(pulses=100, dead_ns=0, split=0.2, max_lag_ns=100))
        odd = simulate(source(), dict(pulses=2, window_ns=199.5))
        self.assertEqual(len(even['spectrum']['frequency_hz']), 201)
        self.assertEqual(len(odd['spectrum']['frequency_hz']), 200)
        one, two = even['diagnostics']['channels']
        self.assertLess(one['events'], two['events'])
        self.assertLess(odd['spectrum']['frequency_hz'][-1], 1e9)

    def test_invalid_windows_aliasing_prf_and_resource_limits(self):
        for settings in (dict(window_ns=5000), dict(dt_ns=10), dict(prf_hz=200000),
                         dict(pulses=20001), dict(pulses=1.1), dict(split=1),
                         dict(velocity_m_s=1000), dict(lo_power_w=float('nan'))):
            with self.subTest(settings=settings), self.assertRaises(ValueError):
                validate(source(), settings)


class SourceContractTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.folder = Path(self.temp.name)
        self.summary = self.folder / 'summary.json'
        self.summary.write_text(json.dumps({'global': {'wavelength_nm': 1550,
            'instrument_parameters': {'pulse_width_s': 1e-6}}}), encoding='utf-8')
        (self.folder / 'rain_power.csv').write_text(
            'range_m,light_rain_power_signal_raw,light_rain_power_observed_raw\n100,1e-10,999\n200,2e-11,999\n', encoding='utf-8')

    def tearDown(self):
        self.temp.cleanup()

    def read(self):
        return read_source(self.folder, self.summary, 'test-run', 'light_rain', 110)

    def test_raw_power_mapping_and_digest_invalidation(self):
        s = self.read()
        self.assertEqual(s['signal_power_w'], 1e-10)
        self.assertEqual(s['actual_range_m'], 100)
        old = make_request(s, {})['key']
        self.assertNotEqual(old, make_request(s, dict(seed=1))['key'])
        path = self.folder / 'rain_power.csv'
        path.write_text(path.read_text().replace('1e-10', '2e-10'))
        self.assertNotEqual(old, make_request(self.read(), {})['key'])

    def test_missing_metadata_and_out_of_range_are_not_silently_replaced(self):
        with self.assertRaises(ValueError):
            read_source(self.folder, self.summary, 'test', 'light_rain', 201)
        self.summary.write_text('{}')
        with self.assertRaises(ValueError):
            self.read()

    def test_worker_roundtrip_preserves_source(self):
        request = make_request(self.read(), dict(pulses=5))
        request_path, output = self.folder / 'request.json', self.folder / 'result.json'
        request_path.write_text(json.dumps(request), encoding='utf-8')
        completed = subprocess.run([sys.executable, str(ROOT / 'src/wind_worker.py'),
                                    '--request', str(request_path), '--output', str(output)],
                                   capture_output=True, timeout=30)
        self.assertEqual(completed.returncode, 0, completed.stdout)
        result = json.loads(output.read_text(encoding='utf-8'))
        self.assertEqual(result['key'], request['key'])
        self.assertEqual(result['source'], request['source'])
        self.assertFalse(output.with_suffix('.tmp').exists())


if __name__ == '__main__':
    unittest.main()
