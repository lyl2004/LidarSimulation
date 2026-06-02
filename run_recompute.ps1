# run_recompute.ps1  —  触发完整十一图重算
# Usage:  .\run_recompute.ps1
Set-Location $PSScriptRoot
pixi run -e mie python temp\lidar_1d\make_final_figures.py --clean
