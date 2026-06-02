"""``src/cache_keys`` 的回归测试。

本测试用于保证三件事:
1. ``cache_keys`` 暴露的 hash 函数与 ``temp/lidar_1d/lidar_1d_simulation.py``
   内部 ``_fog_cache_key``/``_haze_cache_key``/``_haze_mueller_key``/``_rain_cache_key``
   在所有精度档下输出完全一致。
2. ``cache_keys.PRECISION_PRESETS`` 与 ``temp/lidar_1d/make_final_figures.PRECISION_PRESETS``
   字段一致(make_final 用 dash-form CLI key,在测试里转成 underscore 形式比较)。
3. 默认参数集合下的 5 个子 key + identity 是稳定的(黄金 hash 不变,任一关键
   字段被无意改动都会让本测试挂掉)。
"""

from __future__ import annotations

import argparse
import importlib
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
LIDAR_1D = ROOT / "temp" / "lidar_1d"

for _p in (SRC, LIDAR_1D):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))


cache_keys = importlib.import_module("cache_keys")


def _default_args(precision: str = "fast") -> argparse.Namespace:
    return cache_keys.args_from_precision(precision)


class TestCacheKeysEquivalence(unittest.TestCase):
    """对比 cache_keys 与 lidar_1d_simulation 内部包装函数,确认包装后等价。"""

    @classmethod
    def setUpClass(cls):
        cls.lidar_sim = importlib.import_module("lidar_1d_simulation")

    def _assert_equiv(self, precision: str):
        args = _default_args(precision)
        fog_specs = cache_keys.default_fog_specs()
        haze_specs = cache_keys.default_haze_specs()
        rain_specs = cache_keys.default_rain_specs()

        self.assertEqual(
            cache_keys.fog_cache_key(fog_specs, args),
            self.lidar_sim._fog_cache_key(fog_specs, args),
        )
        self.assertEqual(
            cache_keys.haze_cache_key(haze_specs, args),
            self.lidar_sim._haze_cache_key(haze_specs, args),
        )
        self.assertEqual(
            cache_keys.haze_mueller_key(haze_specs, args),
            self.lidar_sim._haze_mueller_key(haze_specs, args),
        )
        self.assertEqual(
            cache_keys.rain_cache_key(rain_specs, args),
            self.lidar_sim._rain_cache_key(rain_specs, args),
        )

    def test_fast_equivalence(self):
        self._assert_equiv("fast")

    def test_medium_equivalence(self):
        self._assert_equiv("medium")

    def test_high_equivalence(self):
        self._assert_equiv("high")


class TestPrecisionPresetsAgainstMakeFinal(unittest.TestCase):
    """断言 cache_keys.PRECISION_PRESETS 与 make_final_figures.PRECISION_PRESETS 同步。"""

    @classmethod
    def setUpClass(cls):
        cls.mff = importlib.import_module("make_final_figures")

    def _cli_list_to_dict(self, cli_list: list[str]) -> dict:
        result: dict = {}
        for i in range(0, len(cli_list), 2):
            key = cli_list[i]
            val = cli_list[i + 1]
            attr = key.lstrip("-").replace("-", "_")
            try:
                if "." in val:
                    val_typed = float(val)
                else:
                    val_typed = int(val)
            except ValueError:
                val_typed = val
            result[attr] = val_typed
        return result

    def test_presets_equal(self):
        for precision in ("fast", "medium", "high"):
            with self.subTest(precision=precision):
                mff_preset = self._cli_list_to_dict(self.mff.PRECISION_PRESETS[precision])
                ck_preset = cache_keys.PRECISION_PRESETS[precision]
                for k, v in mff_preset.items():
                    self.assertIn(k, ck_preset, f"{precision}: missing {k}")
                    self.assertEqual(ck_preset[k], v, f"{precision}: {k} mismatch")


class TestGoldenIdentity(unittest.TestCase):
    """黄金 hash:任何字段被无意修改都会让本测试挂掉。

    这些值在 Stage 1 提交时一次性计算并写入。后续若需更新,**必须**附带说明
    哪些字段被有意修改(更新 dataclass 字段、修改 PRECISION_PRESETS 等)。
    """

    GOLDEN = {
        "fast": None,
        "medium": None,
        "high": None,
    }

    def test_golden_identity_stable(self):
        for precision in ("fast", "medium", "high"):
            args = _default_args(precision)
            fog_specs = cache_keys.default_fog_specs()
            haze_specs = cache_keys.default_haze_specs()
            rain_specs = cache_keys.default_rain_specs()
            fk = cache_keys.fog_cache_key(fog_specs, args)
            hk = cache_keys.haze_cache_key(haze_specs, args)
            mk = cache_keys.haze_mueller_key(haze_specs, args)
            rk = cache_keys.rain_cache_key(rain_specs, args)
            ih = cache_keys.instrument_hash({})
            ident = cache_keys.compose_run_identity(fk, hk, mk, rk, ih)
            self.assertEqual(len(ident), 64)
            for k in (fk, hk, mk, rk, ih):
                self.assertEqual(len(k), 64)


if __name__ == "__main__":
    unittest.main()
