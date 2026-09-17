"""Convergence evidence for distance, angle, size, and photon discretization."""

from __future__ import annotations

import math
import sys
import unittest
from pathlib import Path
from unittest import mock

import numpy as np


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import lidar_profile_solver  # noqa: E402
import mie_core  # noqa: E402


def relative_change(current: float, reference: float) -> float:
    return abs(current - reference) / max(abs(reference), 1.0e-30)


class TestDeterministicGridConvergence(unittest.TestCase):
    def test_distance_grid_converges_to_linear_profile_integral(self):
        distance = 1200.0
        alpha0 = 1.5e-4
        gradient = 1.0e-7
        exact_tau = alpha0 * distance + 0.5 * gradient * distance**2
        exact = math.exp(-2.0 * exact_tau)
        errors = []

        for step in (60.0, 30.0, 15.0, 7.5):
            ranges = np.arange(step, distance + 0.5 * step, step)
            alpha = alpha0 + gradient * ranges
            numerical = lidar_profile_solver.two_way_transmittance(ranges, alpha)[-1]
            errors.append(abs(float(numerical) - exact))

        self.assertTrue(all(later < earlier for earlier, later in zip(errors, errors[1:])))
        self.assertLess(errors[-1], 2.0e-4)

    def test_angular_moments_converge_to_mie_asymmetry(self):
        errors = []
        for count in (181, 361, 721, 1441):
            angles = np.linspace(0.0, 180.0, count)
            result = mie_core.mie_effective_polarized(
                "mono", 0.45, 0.45, 0.35, 1.50 + 0.01j, 532e-9, angles_deg=angles
            )
            theta = np.deg2rad(angles)
            angular_g = 0.5 * np.trapezoid(
                result.M11 * np.cos(theta) * np.sin(theta), theta
            )
            errors.append(abs(float(angular_g) - result.g))

        self.assertLess(errors[-1], errors[0])
        self.assertLess(errors[-1], 3.0e-4)

    def test_lognormal_radius_quadrature_converges(self):
        values = []
        for count in (17, 33, 65, 129):
            result = mie_core.mie_effective_polarized(
                "lognormal",
                0.2,
                0.2,
                0.35,
                1.45 + 0.01j,
                532e-9,
                angles_deg=np.linspace(0.0, 180.0, 181),
                n_radii=count,
            )
            values.append((result.sigma_ext, result.sigma_sca, result.g))

        coarse_change = max(
            relative_change(values[1][i], values[0][i]) for i in range(3)
        )
        fine_change = max(
            relative_change(values[3][i], values[2][i]) for i in range(3)
        )
        self.assertLess(fine_change, coarse_change)
        self.assertLess(fine_change, 5.0e-3)


class TestMonteCarloConvergence(unittest.TestCase):
    @staticmethod
    def _isotropic_mie():
        angles = np.linspace(0.0, 180.0, 361)
        ones = np.ones_like(angles)
        return mie_core.MiePolarizedResult(
            1.0,
            1.0,
            0.0,
            angles,
            ones,
            np.zeros_like(angles),
            np.zeros_like(angles),
            np.zeros_like(angles),
        )

    def _replicate_backscatter(self, photons: int, seeds: range) -> np.ndarray:
        mie = self._isotropic_mie()
        theta, cdf = mie_core.get_phase_function_cdf(mie.angles_deg, mie.M11)
        values = []
        for seed in seeds:
            generator = np.random.Generator(np.random.PCG64(seed))
            with mock.patch.object(mie_core.np.random, "default_rng", return_value=generator):
                result = mie_core.monte_carlo_stats_polarized_profile(
                    0.2, 1.0, 1.0, 0.0, photons, theta, cdf, mie
                )
            values.append(result.backscatter_ratio)
        return np.asarray(values)

    def test_standard_error_scales_as_inverse_square_root_photons(self):
        seeds = range(40)
        low = self._replicate_backscatter(500, seeds)
        high = self._replicate_backscatter(4000, seeds)
        ratio = np.std(low, ddof=1) / np.std(high, ddof=1)

        expected_ratio = math.sqrt(4000 / 500)
        self.assertGreater(ratio, 0.60 * expected_ratio)
        self.assertLess(ratio, 1.40 * expected_ratio)
        self.assertLess(np.std(high, ddof=1), np.std(low, ddof=1))


if __name__ == "__main__":
    unittest.main()
