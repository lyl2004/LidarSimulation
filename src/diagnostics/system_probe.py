from __future__ import annotations

import json
import os
import platform
import socket
import subprocess
import sys
from pathlib import Path


def _run_command(command: list[str]) -> str | None:
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=5,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0,
        )
        output = (completed.stdout or completed.stderr or "").strip()
        return output or None
    except Exception:
        return None


def collect_host_info() -> dict:
    cpu_total = os.cpu_count() or 1
    return {
        "hostname": socket.gethostname(),
        "platform": sys.platform,
        "os_name": os.name,
        "python_version": sys.version,
        "python_executable": sys.executable,
        "machine": platform.machine(),
        "processor": platform.processor(),
        "cpu_count": cpu_total,
        "architecture": platform.architecture()[0],
        "system": platform.system(),
        "release": platform.release(),
        "version": platform.version(),
        "node": platform.node(),
    }


def collect_runtime_env() -> dict:
    interesting_keys = [
        "LIDAR_INSTALL_DIR",
        "LIDAR_MIE_PYTHON",
        "LIDAR_DIAGNOSTICS",
        "LIDAR_DIAGNOSTICS_MODE",
        "LIDAR_DIAGNOSTICS_ROOT",
        "LIDAR_DIAGNOSTICS_RUN_ID",
        "JULIA_EXE",
        "JULIA_BINDIR",
        "JULIA_DEPOT_PATH",
        "JULIA_NUM_THREADS",
        "JULIA_PKG_PRECOMPILE_AUTO",
        "NUMBA_NUM_THREADS",
        "OMP_NUM_THREADS",
        "PIXI_HOME",
    ]
    env = {key: os.environ.get(key) for key in interesting_keys if os.environ.get(key)}
    env["cwd"] = os.getcwd()
    env["argv"] = list(sys.argv)
    env["path_head"] = os.environ.get("PATH", "").split(os.pathsep)[:12]
    return env


def collect_tool_versions() -> dict:
    versions: dict[str, str | None] = {
        "python": sys.version.splitlines()[0],
        "pixi": _run_command(["pixi", "--version"]),
        "julia": _run_command(["julia", "--version"]),
        "go": _run_command(["go", "version"]),
    }
    return versions


def build_session_header() -> dict:
    return {
        "host_info": collect_host_info(),
        "runtime_env": collect_runtime_env(),
        "tool_versions": collect_tool_versions(),
    }


def dump_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
