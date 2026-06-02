# run_ui.ps1  —  启动展示界面
# Usage:  .\run_ui.ps1
Set-Location $PSScriptRoot
pixi run -e gui python app/demo_ui.py
