#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Pinned final-run wrapper for the temp 1D lidar figure workflow.

This script intentionally does not implement any physics or plotting logic. It
only forwards a fixed, reviewed parameter set to lidar_1d_simulation.py so the
final figures can be regenerated without copying a long command line.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path


HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
SIM_SCRIPT = HERE / "lidar_1d_simulation.py"
OVERRIDES_FILE = HERE / "param_overrides.json"
sys.path.insert(0, str(ROOT / "src"))
from diagnostics import get_or_create_session, new_component_logger  # noqa: E402
from path_resolver import resolve_julia_executable, get_root as _get_root  # noqa: E402

ROOT = _get_root()
SIM_SCRIPT = ROOT / "temp" / "lidar_1d" / "lidar_1d_simulation.py"
OVERRIDES_FILE = ROOT / "temp" / "lidar_1d" / "param_overrides.json"
_DIAG_SESSION = get_or_create_session("simulation_1d")
_DIAG_LOGGER = new_component_logger(_DIAG_SESSION, "simulation_1d")


def _diag_event(stage: str, *, status: str = "ok", elapsed_ms: float | None = None, payload: dict | None = None) -> None:
    _DIAG_LOGGER.event(stage, status=status, elapsed_ms=elapsed_ms, payload=payload)
    _DIAG_SESSION.write_component_event("compute_timeline.jsonl", stage, status=status, elapsed_ms=elapsed_ms, payload=payload)

def load_cli_overrides() -> list[str]:
    """Return extra CLI args from param_overrides.json "cli" section, if present."""
    if not OVERRIDES_FILE.exists():
        return []
    try:
        data = json.loads(OVERRIDES_FILE.read_text(encoding="utf-8"))
    except Exception:
        return []
    cli = data.get("cli", {})
    args: list[str] = []
    for key, val in cli.items():
        args.extend([f"--{key}", str(val)])
    return args

PRECISION_PRESETS = {
    "fast": [
        "--fog-grid", "1001",
        "--rain-grid", "4001",
        "--haze-mie-reference-grid", "1001",
        "--tmatrix-solver", "iitm_only",
        "--tmatrix-n-radii", "65",
        "--tmatrix-nr", "32",
        "--tmatrix-ntheta", "48",
        "--tmatrix-timeout-s", "3600",
        "--discrete-angle-bins", "181",
        "--stokes-max-orders", "4",
        "--mueller-angles", "400",
        "--haze-mueller-grid", "201",
    ],
    "medium": [
        "--fog-grid", "2001",
        "--rain-grid", "8001",
        "--haze-mie-reference-grid", "2001",
        "--tmatrix-solver", "iitm_only",
        "--tmatrix-n-radii", "97",
        "--tmatrix-nr", "48",
        "--tmatrix-ntheta", "72",
        "--tmatrix-timeout-s", "7200",
        "--discrete-angle-bins", "361",
        "--stokes-max-orders", "6",
    ],
    "high": [
        "--fog-grid", "4001",
        "--rain-grid", "16001",
        "--haze-mie-reference-grid", "4001",
        "--tmatrix-solver", "iitm_only",
        "--tmatrix-n-radii", "129",
        "--tmatrix-nr", "64",
        "--tmatrix-ntheta", "96",
        "--tmatrix-timeout-s", "25200",
        "--discrete-angle-bins", "721",
        "--stokes-max-orders", "8",
    ],
}

SEED_EXPORT_ROOT = ROOT / "temp" / "lidar_1d" / "cache_store"
SEED_EXPORT_VISIBLE_ROOT = SEED_EXPORT_ROOT / "seeds"
SEED_EXPORT_INDEX_ROOT = SEED_EXPORT_ROOT / "index"


def default_output_for(preset: str) -> Path:
    base = ROOT / "temp" / "lidar_1d" / "outputs_high_precision_latest"
    if preset == "local":
        return base / "local"
    raise ValueError(f"unsupported preset: {preset}")


def seed_output_for(preset: str) -> Path:
    return SEED_EXPORT_VISIBLE_ROOT / preset


def _seed_manifest_path() -> Path:
    return SEED_EXPORT_ROOT / "manifest.json"


def _load_seed_manifest() -> dict:
    path = _seed_manifest_path()
    if not path.exists():
        return {"precisions": {}, "visible_default": "high"}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"precisions": {}, "visible_default": "high"}
    if not isinstance(data, dict):
        return {"precisions": {}, "visible_default": "high"}
    data.setdefault("precisions", {})
    data.setdefault("visible_default", "high")
    return data


def _save_seed_manifest(data: dict) -> None:
    SEED_EXPORT_ROOT.mkdir(parents=True, exist_ok=True)
    _seed_manifest_path().write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _snapshot_dir(src: Path, dst: Path) -> None:
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src, dst)


def preset_args(preset: str) -> list[str]:
    if preset == "local":
        return []
    raise ValueError(f"unsupported preset: {preset}")


def resolve_julia_cmd() -> str:
    return resolve_julia_executable()


def build_command(args: argparse.Namespace) -> list[str]:
    output = Path(args.output).resolve() if args.output else default_output_for(args.preset).resolve()
    base_preset = preset_args(args.preset)
    precision_args = PRECISION_PRESETS[args.precision]
    cli_overrides = load_cli_overrides()
    # cli_overrides may repeat keys already in preset/precision args; argparse uses the last value,
    # so appending overrides after them causes overrides to win.
    cmd = [
        args.python_cmd,
        str(SIM_SCRIPT),
        "--output",
        str(output),
        *base_preset,
        "--precision-profile",
        args.precision,
        *precision_args,
        *cli_overrides,
    ]
    if args.clean:
        cmd.append("--clean")

    julia_cmd = resolve_julia_cmd()
    cmd.extend(["--julia-cmd", julia_cmd])

    return cmd


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the pinned final temp/lidar_1d figure preset without changing algorithms."
    )
    parser.add_argument(
        "--preset",
        choices=["local"],
        default="local",
        help="Named fixed run for the current eleven-figure report.",
    )
    parser.add_argument(
        "--precision",
        choices=["fast", "medium", "high"],
        default="fast",
        help="Computation precision preset: fast (~1min), medium (~5min), high (~10-20min).",
    )
    parser.add_argument("--output", default=None, help="Override the preset output directory.")
    parser.add_argument("--python-cmd", default=sys.executable)
    parser.add_argument("--clean", action="store_true", help="Forward --clean to the underlying simulation script.")
    parser.add_argument("--dry-run", action="store_true", help="Print the resolved command and exit.")
    parser.add_argument("--generate-seeds", action="store_true", help="Generate hidden seed snapshots for all precision presets.")
    return parser.parse_args()


def build_seed_commands(args: argparse.Namespace) -> list[list[str]]:
    env = [
        args.python_cmd,
        str(SIM_SCRIPT),
        "--output",
    ]
    commands: list[list[str]] = []
    for precision, preset_args in PRECISION_PRESETS.items():
        commands.append([
            *env,
            str(seed_output_for(precision)),
            *preset_args,
            "--precision-profile",
            precision,
            "--cache-source",
            "seed",
            *load_cli_overrides(),
            "--julia-cmd",
            resolve_julia_cmd(),
        ])
    return commands


def main() -> int:
    args = parse_args()
    t0 = time.perf_counter()
    diag_phase = os.environ.get("LIDAR_DIAGNOSTICS_PHASE", "unspecified").strip() or "unspecified"
    _diag_event(
        "make_final_figures_started",
        status="begin",
        payload={
            "preset": args.preset,
            "precision": args.precision,
            "clean": args.clean,
            "dry_run": args.dry_run,
            "diagnostics_phase": diag_phase,
        },
    )
    cmd = build_command(args)
    print("[make_final_figures] command:")
    print(" ".join(cmd))
    _diag_event(
        "precision_preset_resolved",
        payload={
            "precision": args.precision,
            "command": cmd,
            "output": str(Path(args.output).resolve() if args.output else default_output_for(args.preset).resolve()),
            "diagnostics_phase": diag_phase,
        },
    )
    if args.dry_run:
        if args.generate_seeds:
            for cmd in build_seed_commands(args):
                print("[make_final_figures] seed command:")
                print(" ".join(cmd))
        else:
            print("[make_final_figures] command:")
            print(" ".join(cmd))
        _DIAG_SESSION.update_summary("simulation_1d", {"dry_run": True, "command": cmd, "diagnostics_phase": diag_phase}, status="success")
        return 0
    env = os.environ.copy()
    if args.generate_seeds:
        generated: list[dict[str, object]] = []
        for precision, seed_cmd in zip(PRECISION_PRESETS.keys(), build_seed_commands(args)):
            output = seed_output_for(precision)
            if args.clean and output.exists():
                shutil.rmtree(output)
            _diag_event("seed_run_started", status="begin", payload={"precision": precision, "output": str(output), "diagnostics_phase": diag_phase})
            rc = subprocess.run(seed_cmd, cwd=str(ROOT), env=env).returncode
            if rc != 0:
                _DIAG_SESSION.update_summary("simulation_1d", {"seed_precision": precision, "returncode": rc, "diagnostics_phase": diag_phase}, status="error")
                return rc
            generated.append({"precision": precision, "output": str(output)})
        manifest = _load_seed_manifest()
        manifest["precisions"] = {
            item["precision"]: {"output": item["output"], "visible": item["precision"] == "high"}
            for item in generated
        }
        manifest["visible_default"] = "high"
        _save_seed_manifest(manifest)
        _DIAG_SESSION.update_summary("simulation_1d", {"generated_seeds": generated, "diagnostics_phase": diag_phase}, status="success")
        return 0
    _diag_event("simulation_wrapper_subprocess_start", status="begin", payload={"cmd": cmd, "cwd": str(ROOT), "diagnostics_phase": diag_phase})
    rc = subprocess.run(cmd, cwd=str(ROOT), env=env).returncode
    elapsed_ms = (time.perf_counter() - t0) * 1000.0
    _diag_event(
        "simulation_wrapper_subprocess_end",
        status="ok" if rc == 0 else "error",
        elapsed_ms=elapsed_ms,
        payload={"returncode": rc, "diagnostics_phase": diag_phase},
    )
    _DIAG_SESSION.update_summary(
        "simulation_1d",
        {"wrapper_returncode": rc, "precision": args.precision, "command": cmd, "diagnostics_phase": diag_phase, "clean": args.clean},
        status="success" if rc == 0 else "error",
    )
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
