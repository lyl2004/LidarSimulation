"""Cross-solver and propagation-limit truth tests."""

from __future__ import annotations

import json
import math
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

import numpy as np


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import mie_core  # noqa: E402


class TestTMatrixSphereLimit(unittest.TestCase):
    WAVELENGTH_M = 1.55e-6
    REFRACTIVE_INDEX = 1.33 + 0.001j
    RADII_UM = (0.08, 0.25, 0.50)
    ANGLES_DEG = np.array([0.0, 30.0, 90.0, 150.0, 180.0])

    @classmethod
    def setUpClass(cls):
        command = [
            "pixi",
            "run",
            "-e",
            "julia",
            "julia",
            f"--project={ROOT / 'src' / 'julia'}",
            str(ROOT / "tests" / "julia_tmatrix_sphere_probe.jl"),
            str(cls.WAVELENGTH_M),
            str(cls.REFRACTIVE_INDEX.real),
            str(cls.REFRACTIVE_INDEX.imag),
            ",".join(map(str, cls.RADII_UM)),
            ",".join(map(str, cls.ANGLES_DEG)),
            "40",
            "64",
        ]
        completed = subprocess.run(
            command,
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=180,
        )
        marker = "THEORY_PROBE_JSON="
        line = next(
            (item for item in completed.stdout.splitlines() if item.startswith(marker)),
            None,
        )
        if line is None:
            raise RuntimeError(f"Julia probe did not return JSON:\n{completed.stdout}\n{completed.stderr}")
        cls.tmatrix_rows = json.loads(line[len(marker) :])

    def test_cross_sections_and_asymmetry_match_mie(self):
        for tmatrix in self.tmatrix_rows:
            radius_um = tmatrix["radius_um"]
            mie = mie_core.mie_effective_polarized(
                "mono",
                radius_um,
                radius_um,
                0.35,
                self.REFRACTIVE_INDEX,
                self.WAVELENGTH_M,
                angles_deg=self.ANGLES_DEG,
            )
            with self.subTest(radius_um=radius_um):
                self.assertLess(abs(tmatrix["sigma_ext"] / mie.sigma_ext - 1.0), 2.0e-3)
                self.assertLess(abs(tmatrix["sigma_sca"] / mie.sigma_sca - 1.0), 2.0e-3)
                self.assertLess(abs(tmatrix["g"] - mie.g), 2.0e-3)

    def test_selected_mueller_elements_match_mie(self):
        for tmatrix in self.tmatrix_rows:
            radius_um = tmatrix["radius_um"]
            mie = mie_core.mie_effective_polarized(
                "mono",
                radius_um,
                radius_um,
                0.35,
                self.REFRACTIVE_INDEX,
                self.WAVELENGTH_M,
                angles_deg=self.ANGLES_DEG,
            )
            tm_m11 = np.asarray(tmatrix["M11"], dtype=float)
            tm_m12 = np.asarray(tmatrix["M12"], dtype=float)
            tm_norm = np.trapezoid(
                tm_m11 * np.sin(np.deg2rad(self.ANGLES_DEG)),
                np.deg2rad(self.ANGLES_DEG),
            )
            tm_m11 = tm_m11 * (2.0 / tm_norm)
            tm_m12 = tm_m12 * (2.0 / tm_norm)
            scale = np.maximum(np.abs(mie.M11), 1.0e-8)
            with self.subTest(radius_um=radius_um):
                self.assertLess(float(np.max(np.abs(tm_m11 - mie.M11) / scale)), 3.0e-2)
                self.assertLess(float(np.max(np.abs(tm_m12 - mie.M12) / scale)), 3.0e-2)

    def test_sphere_tmatrix_reports_zero_lidar_linear_depolarization(self):
        for tmatrix in self.tmatrix_rows:
            m11_back = float(tmatrix["M11"][-1])
            m22_back = float(tmatrix["M22"][-1])
            delta = mie_core.safe_depol_ratio(m11_back, m22_back)
            with self.subTest(radius_um=tmatrix["radius_um"]):
                self.assertAlmostEqual(
                    m22_back, m11_back, delta=2.0e-10 * max(abs(m11_back), 1.0)
                )
                self.assertAlmostEqual(delta, 0.0, delta=2.0e-10)


class TestPolarizedTransportLimits(unittest.TestCase):
    def test_single_mueller_application_matches_matrix_algebra(self):
        stokes = np.array([1.0, 0.25, -0.15, 0.10])
        m11, m12, m33, m34 = 2.0, -0.30, 1.20, 0.20
        actual, weight = mie_core.apply_mueller(stokes, m11, m12, m33, m34)

        normalized = np.array(
            [
                [1.0, m12 / m11, 0.0, 0.0],
                [m12 / m11, 1.0, 0.0, 0.0],
                [0.0, 0.0, m33 / m11, -m34 / m11],
                [0.0, 0.0, m34 / m11, m33 / m11],
            ]
        )
        raw = normalized @ stokes
        expected = raw / raw[0]
        self.assertTrue(np.allclose(actual, expected, rtol=0.0, atol=1.0e-14))
        self.assertAlmostEqual(weight, raw[0], delta=1.0e-14)
        self.assertLessEqual(float(np.linalg.norm(actual[1:])), 1.0 + 1.0e-14)

    def test_spherical_backscatter_preserves_linear_polarization(self):
        result = mie_core.mie_effective_polarized(
            "mono",
            0.25,
            0.25,
            0.35,
            1.33 + 0.0j,
            1.55e-6,
            angles_deg=np.linspace(0.0, 180.0, 721),
        )
        m11 = float(result.M11[-1])
        m12 = float(result.M12[-1])
        output, _ = mie_core.apply_mueller(np.array([1.0, 1.0, 0.0, 0.0]), m11, m12, 0.0, 0.0)
        self.assertAlmostEqual(abs(float(output[1])), 1.0, delta=2.0e-10)
        self.assertAlmostEqual(float(output[2]), 0.0, delta=2.0e-10)
        self.assertAlmostEqual(float(output[3]), 0.0, delta=2.0e-10)

    def test_spherical_particle_reports_zero_linear_depolarization(self):
        result = mie_core.mie_effective_polarized(
            "mono",
            0.25,
            0.25,
            0.35,
            1.33 + 0.0j,
            1.55e-6,
            angles_deg=np.linspace(0.0, 180.0, 721),
        )
        observables = mie_core.mie_scatter_observables(result)
        self.assertAlmostEqual(observables["depol_back"], 0.0, delta=2.0e-10)
        self.assertAlmostEqual(observables["depol_forward"], 0.0, delta=2.0e-10)

    def test_optically_thin_mc_approaches_single_scattering_solution(self):
        angles = np.linspace(0.0, 180.0, 721)
        m11 = np.ones_like(angles)
        mie = mie_core.MiePolarizedResult(
            sigma_ext=1.0,
            sigma_sca=1.0,
            g=0.0,
            angles_deg=angles,
            M11=m11,
            M12=np.zeros_like(angles),
            M33=np.zeros_like(angles),
            M34=np.zeros_like(angles),
        )
        theta, cdf = mie_core.get_phase_function_cdf(angles, m11)
        optical_depth = 0.01
        expected_backscatter = 0.5 * (1.0 - math.exp(-optical_depth))

        with mock.patch.object(
            mie_core.np.random, "default_rng", return_value=np.random.Generator(np.random.PCG64(2026))
        ):
            result = mie_core.monte_carlo_stats_polarized_profile(
                optical_depth,
                1.0,
                1.0,
                0.0,
                200_000,
                theta,
                cdf,
                mie,
            )

        standard_error = math.sqrt(expected_backscatter * (1.0 - expected_backscatter) / 200_000)
        self.assertLess(abs(result.backscatter_ratio - expected_backscatter), 5.0 * standard_error)
        self.assertLess(result.avg_collisions, 0.011)
        self.assertAlmostEqual(
            result.backscatter_ratio + result.transmit_ratio + result.absorbed_ratio,
            1.0,
            delta=1.0e-14,
        )


if __name__ == "__main__":
    unittest.main()
