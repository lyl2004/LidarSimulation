"""LidarSim launcher — sets up environment variables and starts the GUI."""
import os
import subprocess
import sys
from pathlib import Path


def main() -> None:
    # Resolve install directory from this executable's location.
    install_dir = Path(sys.executable).parent

    gui_python = install_dir / ".pixi" / "envs" / "gui" / "python.exe"
    mie_python = install_dir / ".pixi" / "envs" / "mie" / "python.exe"
    script     = install_dir / "app" / "demo_ui.py"
    julia_bin  = install_dir / "julia" / "bin"
    log_path   = install_dir / "LidarSim.log"

    env = os.environ.copy()
    # Redirect all per-tool home directories into the install directory so
    # nothing is ever written outside it at runtime.
    env["PIXI_HOME"]          = str(install_dir / ".pixi_home")
    env["JULIA_DEPOT_PATH"]   = str(install_dir / "julia_depot")
    env["JULIA_BINDIR"]       = str(julia_bin)
    env["LIDAR_MIE_PYTHON"]   = str(mie_python)
    env["LIDAR_INSTALL_DIR"]  = str(install_dir)
    env["PATH"]               = str(julia_bin) + os.pathsep + env.get("PATH", "")

    with open(log_path, "w", encoding="utf-8") as log:
        try:
            subprocess.run(
                [str(gui_python), str(script)],
                cwd=str(install_dir),
                env=env,
                stdout=log,
                stderr=log,
                check=True,
            )
        except Exception as exc:
            log.write(f"\nLauncher error: {exc}\n")


if __name__ == "__main__":
    main()
