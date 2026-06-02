"""Backfill ``cache_identity`` block into historical summary.json files.

用法:
    pixi run -e mie python tools/backfill_cache_identity.py

会扫描:
- ``temp/lidar_1d/default_result/summary.json``
- ``temp/lidar_1d/run_history/run_*/summary.json``

对缺 ``cache_identity`` 的:
1. 解析 ``summary["global"]["command"]``,通过 lidar_1d_simulation.build_arg_parser
   反推完整 args。
2. 应用 params.json 中的 fog/haze/rain overrides(如存在)。
3. 调用 cache_keys 算 5 子 key + identity。
4. 原地回写 summary.json(只追加 cache_identity 块,其它字段不动)。

补不出来(命令缺失/解析失败)的条目会写 ``{"legacy": true}`` 标记,
前端识别后跳过该 run 的匹配。

脚本可重入:已有 cache_identity 的条目直接跳过。
"""

from __future__ import annotations

import argparse
import json
import shlex
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
LIDAR_1D = ROOT / "temp" / "lidar_1d"

for _p in (SRC, LIDAR_1D):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import cache_keys  # noqa: E402


def _safe_load_json(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _parse_command(command: str) -> argparse.Namespace | None:
    """Run summary.global.command back through the simulation's argparse."""
    import lidar_1d_simulation  # 延迟导入,避免脚本入口时加载 matplotlib

    if not command:
        return None
    try:
        tokens = shlex.split(command, posix=False)
    except Exception:
        return None
    if not tokens:
        return None
    # 去掉脚本路径(第一个 token 一般是 ...\lidar_1d_simulation.py)
    if tokens[0].lower().endswith(".py"):
        tokens = tokens[1:]
    parser = lidar_1d_simulation.build_arg_parser()
    try:
        args, _unknown = parser.parse_known_args(tokens)
    except SystemExit:
        return None
    return args


def backfill_one(summary_path: Path, params_path: Path | None = None) -> dict:
    """Returns ``{"action": "skipped"|"filled"|"legacy"|"missing", "identity": ...}``."""
    summary = _safe_load_json(summary_path)
    if summary is None:
        return {"action": "missing", "path": str(summary_path)}

    existing = summary.get("cache_identity")
    if isinstance(existing, dict) and existing.get("identity") and not existing.get("legacy"):
        return {"action": "skipped", "identity": existing.get("identity")}

    command = (summary.get("global") or {}).get("command", "")
    args = _parse_command(command)
    if args is None:
        summary["cache_identity"] = {"legacy": True, "reason": "command_unparseable", "schema_version": 1}
        summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        return {"action": "legacy", "reason": "command_unparseable"}

    overrides: dict = {}
    if params_path is not None:
        params_data = _safe_load_json(params_path) or {}
        for k in ("fog", "haze", "rain"):
            if k in params_data:
                overrides[k] = params_data[k]

    try:
        fog_specs = cache_keys.apply_fog_overrides(cache_keys.default_fog_specs(), overrides)
        haze_specs = cache_keys.apply_haze_overrides(cache_keys.default_haze_specs(), overrides)
        rain_specs = cache_keys.apply_rain_overrides(cache_keys.default_rain_specs(), overrides)

        fog_key = cache_keys.fog_cache_key(fog_specs, args)
        haze_key = cache_keys.haze_cache_key(haze_specs, args)
        mueller_key = cache_keys.haze_mueller_key(haze_specs, args)
        rain_key = cache_keys.rain_cache_key(rain_specs, args)

        instrument = (summary.get("global") or {}).get("instrument_parameters", {})
        instr_hash = cache_keys.instrument_hash({
            "laser_peak_power_W": instrument.get("laser_peak_power_W"),
            "pulse_width_s": instrument.get("pulse_width_s"),
            "receiver_radius_m": instrument.get("receiver_radius_m"),
            "optical_efficiency": instrument.get("optical_efficiency"),
        })
        identity = cache_keys.compose_run_identity(fog_key, haze_key, mueller_key, rain_key, instr_hash)
    except Exception as exc:
        summary["cache_identity"] = {"legacy": True, "reason": f"compute_failed: {exc}", "schema_version": 1}
        summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        return {"action": "legacy", "reason": str(exc)}

    summary["cache_identity"] = {
        "fog_key": fog_key,
        "haze_key": haze_key,
        "haze_mueller_key": mueller_key,
        "rain_key": rain_key,
        "instrument_hash": instr_hash,
        "identity": identity,
        "precision_profile": cache_keys.precision_profile_of(args),
        "schema_version": 1,
        "backfilled": True,
    }
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"action": "filled", "identity": identity}


def scan_and_backfill(root: Path = LIDAR_1D) -> dict:
    targets: list[tuple[Path, Path | None]] = []
    default_summary = root / "default_result" / "summary.json"
    if default_summary.exists():
        targets.append((default_summary, None))

    run_history_dir = root / "run_history"
    if run_history_dir.exists():
        for run_dir in sorted(run_history_dir.iterdir()):
            if not run_dir.is_dir():
                continue
            summary_path = run_dir / "summary.json"
            params_path = run_dir / "params.json"
            if summary_path.exists():
                targets.append((summary_path, params_path if params_path.exists() else None))

    stats = {"filled": 0, "skipped": 0, "legacy": 0, "missing": 0}
    details: list[dict] = []
    for summary_path, params_path in targets:
        result = backfill_one(summary_path, params_path)
        action = result.get("action", "missing")
        stats[action] = stats.get(action, 0) + 1
        details.append({"summary": str(summary_path), **result})

    return {"stats": stats, "details": details}


def main() -> int:
    report = scan_and_backfill()
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
