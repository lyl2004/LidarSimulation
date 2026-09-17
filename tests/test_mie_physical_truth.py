"""Analytical truth tests for the production Mie implementation."""

from __future__ import annotations

import math
import sys
import unittest
from pathlib import Path

import numpy as np


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import mie_core  # noqa: E402


def rayleigh_qsca(m: complex, x: float) -> float:
    contrast = (m * m - 1.0) / (m * m + 2.0)
    return (8.0 / 3.0) * x**4 * abs(contrast) ** 2


class TestRayleighLimit(unittest.TestCase):
    def test_cross_section_matches_rayleigh_formula_and_power_laws(self):
        m = 1.33 + 0.0j
        wavelength_nm = 1550.0
        radii_nm = np.array([0.35, 0.50, 0.70, 1.00, 1.40])
        cross_sections = []

        for radius_nm in radii_nm:
            diameter_nm = 2.0 * radius_nm
            x = math.pi * diameter_nm / wavelength_nm
            qext, qsca, qabs, *_ = mie_core.AutoMieQ(
                m, wavelength_nm, diameter_nm, asDict=False
            )
            expected = rayleigh_qsca(m, x)
            self.assertLess(abs(qsca - expected) / expected, 2.0e-6)
            self.assertLess(abs(qext - qsca) / qsca, 2.0e-10)
            self.assertLess(abs(qabs), 1.0e-25)
            cross_sections.append(qsca * math.pi * (radius_nm * 1e-9) ** 2)

        radius_slope = np.polyfit(np.log(radii_nm), np.log(cross_sections), 1)[0]
        self.assertAlmostEqual(radius_slope, 6.0, delta=2.0e-4)

        wavelengths_nm = np.array([532.0, 780.0, 1064.0, 1550.0, 2100.0])
        fixed_radius_nm = 0.5
        wavelength_cross_sections = []
        for current_wavelength_nm in wavelengths_nm:
            values = mie_core.AutoMieQ(
                m,
                current_wavelength_nm,
                2.0 * fixed_radius_nm,
                asDict=False,
            )
            wavelength_cross_sections.append(
                values[1] * math.pi * (fixed_radius_nm * 1e-9) ** 2
            )
        wavelength_slope = np.polyfit(
            np.log(wavelengths_nm), np.log(wavelength_cross_sections), 1
        )[0]
        self.assertAlmostEqual(wavelength_slope, -4.0, delta=2.0e-4)


class TestEnergyAndOpticalTheorem(unittest.TestCase):
    def test_extinction_is_scattering_plus_absorption(self):
        cases = [
            (1.33 + 0.0j, 532.0, 100.0),
            (1.50 + 0.02j, 532.0, 500.0),
            (1.31 + 0.10j, 1550.0, 3000.0),
        ]
        for m, wavelength_nm, diameter_nm in cases:
            with self.subTest(m=m, diameter_nm=diameter_nm):
                qext, qsca, qabs, *_ = mie_core.AutoMieQ(
                    m, wavelength_nm, diameter_nm, asDict=False
                )
                scale = max(abs(qext), 1.0)
                self.assertLess(abs(qext - qsca - qabs) / scale, 2.0e-12)
                self.assertGreaterEqual(qsca, 0.0)
                self.assertGreaterEqual(qabs, -2.0e-12)

    def test_forward_amplitude_satisfies_optical_theorem(self):
        cases = [
            (1.33 + 0.0j, 0.3),
            (1.50 + 0.02j, 2.0),
            (1.31 + 0.10j, 8.0),
        ]
        for m, x in cases:
            with self.subTest(m=m, x=x):
                an, bn = mie_core.PMS.Mie_ab(m, x)
                order = np.arange(1, len(an) + 1, dtype=float)
                forward_amplitude = 0.5 * np.sum((2.0 * order + 1.0) * (an + bn))
                qext_optical = 4.0 * forward_amplitude.real / x**2
                qsca_coefficients = (
                    2.0
                    / x**2
                    * np.sum((2.0 * order + 1.0) * (abs(an) ** 2 + abs(bn) ** 2))
                )

                wavelength_nm = 1000.0
                diameter_nm = x * wavelength_nm / math.pi
                qext, qsca, *_ = mie_core.AutoMieQ(
                    m, wavelength_nm, diameter_nm, asDict=False
                )
                self.assertAlmostEqual(qext_optical, qext, delta=2.0e-10 * max(qext, 1.0))
                self.assertAlmostEqual(
                    qsca_coefficients, qsca, delta=2.0e-10 * max(qsca, 1.0)
                )


class TestPhaseAndMuellerPhysics(unittest.TestCase):
    def test_phase_normalization_and_mueller_bounds(self):
        angles_deg = np.linspace(0.0, 180.0, 1441)
        result = mie_core.mie_effective_polarized(
            "mono",
            0.35,
            0.35,
            0.35,
            1.50 + 0.02j,
            532e-9,
            angles_deg=angles_deg,
        )
        theta = np.deg2rad(angles_deg)
        normalization = np.trapezoid(result.M11 * np.sin(theta), theta)

        self.assertAlmostEqual(normalization, 2.0, delta=2.0e-10)
        self.assertTrue(np.all(np.isfinite(result.M11)))
        self.assertGreaterEqual(float(np.min(result.M11)), -1.0e-14)
        self.assertTrue(np.all(np.abs(result.M12) <= result.M11 + 2.0e-12))
        self.assertTrue(np.all(np.abs(result.M33) <= result.M11 + 2.0e-12))
        self.assertTrue(np.all(np.abs(result.M34) <= result.M11 + 2.0e-12))

        theta_grid, cdf = mie_core.get_phase_function_cdf(angles_deg, result.M11)
        self.assertEqual(theta_grid.shape, cdf.shape)
        self.assertAlmostEqual(float(cdf[0]), 0.0, delta=1.0e-15)
        self.assertAlmostEqual(float(cdf[-1]), 1.0, delta=1.0e-12)
        self.assertTrue(np.all(np.diff(cdf) >= -1.0e-14))


if __name__ == "__main__":
    unittest.main()
