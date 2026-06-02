# verify_env.ps1  —  检查运行环境前置条件
# Usage:  .\verify_env.ps1
Set-Location $PSScriptRoot

$ok = $true

Write-Host "`n[1/4] 检查 pixi ..." -ForegroundColor Cyan
if (Get-Command pixi -ErrorAction SilentlyContinue) {
    Write-Host "  pixi 可用: $(pixi --version)" -ForegroundColor Green
} else {
    Write-Host "  ✗ 未找到 pixi，请安装后重试" -ForegroundColor Red
    $ok = $false
}

Write-Host "`n[2/4] 检查 gui 环境 (NiceGUI)..." -ForegroundColor Cyan
$guiCheck = pixi run -e gui python -c "import nicegui; print('nicegui', nicegui.__version__)" 2>&1
if ($LASTEXITCODE -eq 0) {
    Write-Host "  $guiCheck" -ForegroundColor Green
} else {
    Write-Host "  ✗ gui 环境异常: $guiCheck" -ForegroundColor Red
    $ok = $false
}

Write-Host "`n[3/4] 检查 mie 环境 (numpy / scipy / numba)..." -ForegroundColor Cyan
$mieCheck = pixi run -e mie python -c "import numpy, scipy, numba; print('numpy', numpy.__version__, '/ scipy', scipy.__version__, '/ numba', numba.__version__)" 2>&1
if ($LASTEXITCODE -eq 0) {
    Write-Host "  $mieCheck" -ForegroundColor Green
} else {
    Write-Host "  ✗ mie 环境异常: $mieCheck" -ForegroundColor Red
    $ok = $false
}

Write-Host "`n[4/4] 检查 Julia 物理模块..." -ForegroundColor Cyan
$juliaCheck = julia --project=temp/lidar_1d/julia -e 'include("temp/lidar_1d/julia/iitm_physics.jl"); println("iitm_physics ok")' 2>&1
if ($LASTEXITCODE -eq 0) {
    Write-Host "  $juliaCheck" -ForegroundColor Green
} else {
    Write-Host "  ✗ Julia 模块加载失败: $juliaCheck" -ForegroundColor Red
    $ok = $false
}

Write-Host ""
if ($ok) {
    Write-Host "所有检查通过，可以运行 .\run_ui.ps1" -ForegroundColor Green
} else {
    Write-Host "存在环境问题，请根据上述提示修复后重试" -ForegroundColor Yellow
}
