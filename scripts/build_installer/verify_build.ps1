#Requires -Version 5.1
<#
.SYNOPSIS
    验证构建产物的完整性和可用性。

.DESCRIPTION
    检查 build.ps1 生成的所有必需文件，并可选地执行静默安装测试。

.PARAMETER TestInstall
    执行静默安装到临时目录并验证环境解包成功。

.EXAMPLE
    .\verify_build.ps1
    .\verify_build.ps1 -TestInstall
#>
param(
    [switch]$TestInstall
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$scriptDir = $PSScriptRoot
$distDir   = Join-Path $scriptDir "dist"

function Test-File([string]$path, [string]$desc) {
    if (Test-Path $path) {
        $size = (Get-Item $path).Length
        $sizeMB = [math]::Round($size / 1MB, 1)
        Write-Host "[PASS] $desc : $sizeMB MB" -ForegroundColor Green
        return $true
    } else {
        Write-Host "[FAIL] $desc : 文件不存在" -ForegroundColor Red
        return $false
    }
}

function Test-Dir([string]$path, [string]$desc) {
    if (Test-Path $path -PathType Container) {
        Write-Host "[PASS] $desc" -ForegroundColor Green
        return $true
    } else {
        Write-Host "[FAIL] $desc : 目录不存在" -ForegroundColor Red
        return $false
    }
}

Write-Host "`n=== 验证构建产物 ===" -ForegroundColor Cyan

$failures = 0

# 检查必需文件
if (-not (Test-File (Join-Path $distDir "LidarSimSetup.exe") "安装包")) { $failures++ }
if (-not (Test-File (Join-Path $distDir "LidarSim.exe") "启动器")) { $failures++ }
if (-not (Test-File (Join-Path $distDir "gui_packed.tar") "GUI 环境包")) { $failures++ }
if (-not (Test-File (Join-Path $distDir "mie_packed.tar") "MIE 环境包")) { $failures++ }
if (-not (Test-Dir (Join-Path $distDir "julia") "Julia 运行时")) { $failures++ }

# 检查 Julia 关键文件
$juliaExe = Join-Path $distDir "julia\bin\julia.exe"
if (Test-Path $juliaExe) {
    Write-Host "[PASS] Julia 可执行文件存在" -ForegroundColor Green
} else {
    Write-Host "[FAIL] Julia 可执行文件缺失: $juliaExe" -ForegroundColor Red
    $failures++
}

if ($failures -gt 0) {
    Write-Host "`n验证失败: $failures 项检查未通过。" -ForegroundColor Red
    exit 1
}

Write-Host "`n所有基础检查通过。" -ForegroundColor Green

# 可选：静默安装测试
if ($TestInstall) {
    Write-Host "`n=== 执行静默安装测试 ===" -ForegroundColor Cyan

    $testInstallDir = Join-Path $env:TEMP "LidarSimTest_$(Get-Date -Format 'yyyyMMdd_HHmmss')"
    $setupExe = Join-Path $distDir "LidarSimSetup.exe"

    Write-Host "安装到临时目录: $testInstallDir"

    try {
        # 静默安装参数：
        # /VERYSILENT - 完全静默
        # /SUPPRESSMSGBOXES - 抑制消息框
        # /NORESTART - 不重启
        # /SP- - 禁用启动提示
        # /DIR - 指定安装目录
        $installArgs = "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/SP-", "/DIR=`"$testInstallDir`""

        Write-Host "运行安装程序（这可能需要 3-5 分钟）..."
        $proc = Start-Process -FilePath $setupExe -ArgumentList $installArgs -Wait -PassThru

        if ($proc.ExitCode -ne 0) {
            Write-Host "[FAIL] 安装程序退出码: $($proc.ExitCode)" -ForegroundColor Red
            exit 1
        }

        Write-Host "[PASS] 安装程序执行成功" -ForegroundColor Green

        # 验证关键文件
        $testFailures = 0
        if (-not (Test-File (Join-Path $testInstallDir "LidarSim.exe") "启动器")) { $testFailures++ }
        if (-not (Test-Dir (Join-Path $testInstallDir ".pixi\envs\gui") "GUI 环境")) { $testFailures++ }
        if (-not (Test-Dir (Join-Path $testInstallDir ".pixi\envs\mie") "MIE 环境")) { $testFailures++ }
        if (-not (Test-File (Join-Path $testInstallDir ".pixi\envs\gui\python.exe") "GUI Python")) { $testFailures++ }
        if (-not (Test-File (Join-Path $testInstallDir ".pixi\envs\mie\python.exe") "MIE Python")) { $testFailures++ }
        if (-not (Test-Dir (Join-Path $testInstallDir "julia") "Julia 运行时")) { $testFailures++ }
        if (-not (Test-File (Join-Path $testInstallDir "app\demo_ui.py") "主程序")) { $testFailures++ }

        # 检查 postinstall.log
        $postinstallLog = Join-Path $testInstallDir "postinstall.log"
        if (Test-Path $postinstallLog) {
            $logContent = Get-Content $postinstallLog -Raw
            if ($logContent -match "POST-INSTALL ERROR") {
                Write-Host "[FAIL] postinstall.log 包含错误" -ForegroundColor Red
                Write-Host $logContent
                $testFailures++
            } elseif ($logContent -match "Post-install completed successfully") {
                Write-Host "[PASS] 后安装脚本执行成功" -ForegroundColor Green
            } else {
                Write-Host "[WARN] postinstall.log 状态不明确" -ForegroundColor Yellow
            }
        } else {
            Write-Host "[WARN] postinstall.log 不存在" -ForegroundColor Yellow
        }

        if ($testFailures -gt 0) {
            Write-Host "`n安装测试失败: $testFailures 项检查未通过。" -ForegroundColor Red
            Write-Host "测试安装目录保留在: $testInstallDir" -ForegroundColor Yellow
            exit 1
        }

        Write-Host "`n安装测试通过！" -ForegroundColor Green
        Write-Host "清理测试安装目录..."
        Remove-Item -Path $testInstallDir -Recurse -Force -ErrorAction SilentlyContinue

    } catch {
        Write-Host "[FAIL] 安装测试异常: $_" -ForegroundColor Red
        Write-Host "测试安装目录保留在: $testInstallDir" -ForegroundColor Yellow
        exit 1
    }
}

Write-Host "`n验证完成。" -ForegroundColor Green
