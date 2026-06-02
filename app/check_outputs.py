#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Output completeness check for the 1D lidar simulation.

Exit code: 0 = all checks passed, 1 = one or more failures.
"""
from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from path_resolver import get_root as _get_root  # noqa: E402
ROOT     = _get_root()
OUTPUTS  = ROOT / "temp" / "lidar_1d" / "outputs_high_precision_latest" / "local"
FIGURES  = OUTPUTS / "figures"
DATA_DIR = OUTPUTS / "data"
SUMMARY  = OUTPUTS / "summary.json"
OVERRIDE = ROOT / "temp" / "lidar_1d" / "param_overrides.json"

EXPECTED_FIGURE_STEMS = [
    "fig01_radiation_fog_power",
    "fig02_advection_fog_power",
    "fig03_urban_industrial_haze_power",
    "fig04_rural_continental_haze_power",
    "fig05_dust_desert_haze_power",
    "fig06_maritime_haze_power",
    "fig07_urban_industrial_haze_depol",
    "fig08_rural_continental_haze_depol",
    "fig09_dust_desert_haze_depol",
    "fig10_maritime_haze_depol",
    "fig11_rain_power",
]

EXPECTED_CSVS = [
    "radiation_fog_power.csv",
    "advection_fog_power.csv",
    "urban_industrial_haze_power.csv",
    "rural_continental_haze_power.csv",
    "dust_desert_haze_power.csv",
    "maritime_haze_power.csv",
    "urban_industrial_haze_depolarization.csv",
    "rural_continental_haze_depolarization.csv",
    "dust_desert_haze_depolarization.csv",
    "maritime_haze_depolarization.csv",
    "rain_power.csv",
]

EXPECTED_SUMMARY_KEYS = {
    "fog":  ["radiation_fog", "advection_fog"],
    "haze": ["urban_industrial_haze", "rural_continental_haze",
             "dust_desert_haze", "maritime_haze"],
    "rain": ["light_rain", "moderate_rain", "heavy_rain"],
}

MIN_CSV_ROWS = 100


def check(label: str, ok: bool, detail: str = "") -> bool:
    status = "PASS" if ok else "FAIL"
    msg = f"  [{status}] {label}"
    if detail:
        msg += f"  — {detail}"
    print(msg)
    return ok


def main() -> int:
    failures = 0

    print("\n=== 图像文件（PNG / SVG）===")
    for stem in EXPECTED_FIGURE_STEMS:
        for ext in ("png", "svg"):
            p = FIGURES / f"{stem}.{ext}"
            ok = p.exists()
            failures += 0 if check(f"{stem}.{ext}", ok) else 1

    print("\n=== CSV 数据文件 ===")
    for fname in EXPECTED_CSVS:
        p = DATA_DIR / fname
        if not p.exists():
            failures += 1
            check(fname, False, "文件不存在")
            continue
        try:
            with open(p, newline="", encoding="utf-8") as f:
                rows = sum(1 for _ in csv.reader(f)) - 1  # minus header
            ok = rows >= MIN_CSV_ROWS
            failures += 0 if check(fname, ok, f"{rows} 行") else 1
        except Exception as e:
            failures += 1
            check(fname, False, str(e))

    print("\n=== summary.json ===")
    if not SUMMARY.exists():
        failures += 1
        check("summary.json 存在", False)
    else:
        try:
            data = json.loads(SUMMARY.read_text(encoding="utf-8"))
            check("summary.json 有效 JSON", True)
            for cat, keys in EXPECTED_SUMMARY_KEYS.items():
                cat_data = data.get(cat, {})
                for key in keys:
                    ok = key in cat_data
                    failures += 0 if check(f"summary[{cat}][{key}]", ok) else 1
        except Exception as e:
            failures += 1
            check("summary.json 解析", False, str(e))

    print("\n=== param_overrides.json（如存在）===")
    if OVERRIDE.exists():
        try:
            json.loads(OVERRIDE.read_text(encoding="utf-8"))
            check("param_overrides.json 有效 JSON", True)
        except Exception as e:
            failures += 1
            check("param_overrides.json 解析", False, str(e))
    else:
        print("  [INFO] param_overrides.json 不存在（使用默认参数）")

    print()
    if failures == 0:
        print("全部通过。")
    else:
        print(f"失败 {failures} 项。")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
