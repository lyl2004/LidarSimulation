#Requires -Version 5.1
<#
.SYNOPSIS
    开发机构建脚本 — 生成 LidarSimSetup.exe 安装包。

    前置条件（首次运行前手动安装）：
      - Go 1.21+          https://go.dev/dl/
      - Inno Setup 6      https://jrsoftware.org/isdl.php
      - conda-pack        在 gui 与 mie 两个 pixi 环境中均已安装
                          pixi run -e gui  pip install conda-pack
                          pixi run -e mie  pip install conda-pack

    Julia 便携包（必需，手动下载放到 scripts/build_installer/dist/julia-win64.zip）：
      Julia 官网 https://julialang.org/downloads/ → Windows x86_64 → Portable (.zip)
      下载后重命名为 julia-win64.zip 放入本脚本所在目录的 dist/ 子目录。

    用法：
      cd scripts\build_installer
      .\build.ps1                          # 使用已有 tar 包（如存在）
      .\build.ps1 -Repack                  # 强制重新打包 conda 环境
      .\build.ps1 -CleanBuild              # 清理所有构建产物后重新构建
      .\build.ps1 -AppVersion "1.1"        # 指定版本号
#>
param(
    [string]$AppVersion = "1.0",
    [switch]$Repack,
    [switch]$CleanBuild
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

# --------------------------------------------------------------------------
# Paths
# --------------------------------------------------------------------------
$scriptDir  = $PSScriptRoot
$repoRoot   = Resolve-Path (Join-Path $scriptDir "..\..") | Select-Object -ExpandProperty Path
$distDir    = Join-Path $scriptDir "dist"
$launcherDir = Join-Path $scriptDir "launcher"

$guiEnvPath = Join-Path $repoRoot ".pixi\envs\gui"
$mieEnvPath = Join-Path $repoRoot ".pixi\envs\mie"
$seedExportRoot = Join-Path $repoRoot "temp\lidar_1d\cache_store"
$seedExportVisibleRoot = Join-Path $seedExportRoot "seeds"
$seedExportIndexRoot = Join-Path $seedExportRoot "index"

$isccCandidates = @(
    "D:\BuildTools\Inno Setup 6\ISCC.exe",
    "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
    "${env:ProgramFiles}\Inno Setup 6\ISCC.exe"
)
$iscc = $null
$go   = $null

function Write-Step([string]$msg) {
    Write-Host "`n>>> $msg" -ForegroundColor Cyan
}

function Copy-Tree([string]$Source, [string]$Destination) {
    if (Test-Path $Destination) {
        Remove-Item -Path $Destination -Recurse -Force
    }
    New-Item -ItemType Directory -Force -Path (Split-Path $Destination -Parent) | Out-Null
    Copy-Item -Path $Source -Destination $Destination -Recurse -Force
}

function Assert-Exists([string]$path, [string]$hint) {
    if (-not (Test-Path $path)) {
        Write-Error "找不到: $path`n提示: $hint"
        exit 1
    }
}

# --------------------------------------------------------------------------
# 0. Pre-flight checks
# --------------------------------------------------------------------------
Write-Step "检查前置条件"

# Go
$goSearch = Get-Command go -ErrorAction SilentlyContinue
if ($goSearch) {
    $go = $goSearch.Source
} else {
    $goCandidates = @(
        "D:\BuildTools\Go\bin\go.exe",
        "${env:ProgramFiles}\Go\bin\go.exe",
        "${env:LOCALAPPDATA}\Programs\Go\bin\go.exe"
    )
    foreach ($candidate in $goCandidates) {
        if (Test-Path $candidate) {
            $go = $candidate
            break
        }
    }
}
Assert-Exists $go "Install Go 1.21+ from https://go.dev/dl/ or add to PATH"
Write-Host "Go: $(& $go version)"

# Inno Setup
$isccSearch = Get-Command ISCC -ErrorAction SilentlyContinue
if ($isccSearch) {
    $iscc = $isccSearch.Source
} else {
    foreach ($candidate in $isccCandidates) {
        if (Test-Path $candidate) {
            $iscc = $candidate
            break
        }
    }
}
Assert-Exists $iscc "请从 https://jrsoftware.org/isdl.php 安装 Inno Setup 6。"
Write-Host "Inno Setup: $iscc"

# pixi environments
Assert-Exists $guiEnvPath "请先运行 pixi install 完成 gui 环境构建。"
Assert-Exists $mieEnvPath "请先运行 pixi install 完成 mie 环境构建。"

# conda-pack in gui env
$condaPackGui = Join-Path $guiEnvPath "Scripts\conda-pack.exe"
if (-not (Test-Path $condaPackGui)) {
    Write-Error "gui 环境中未安装 conda-pack。请运行:`n  pixi run -e gui pip install conda-pack"
    exit 1
}

# conda-pack in mie env
$condaPackMie = Join-Path $mieEnvPath "Scripts\conda-pack.exe"
if (-not (Test-Path $condaPackMie)) {
    Write-Error "mie 环境中未安装 conda-pack。请运行:`n  pixi run -e mie pip install conda-pack"
    exit 1
}

New-Item -ItemType Directory -Force -Path $distDir | Out-Null

if ($CleanBuild) {
    Write-Step "清理旧构建产物"
    $cleanTargets = @(
        (Join-Path $distDir "gui_packed.tar"),
        (Join-Path $distDir "mie_packed.tar"),
        (Join-Path $distDir "LidarSim.exe"),
        (Join-Path $distDir "LidarSimSetup.exe")
    )
    foreach ($target in $cleanTargets) {
        if (Test-Path $target) {
            Remove-Item -Path $target -Force
            Write-Host "  removed: $target"
        }
    }
}

# --------------------------------------------------------------------------
# 1. Patch tarfile.py in both envs to bypass Windows MAX_PATH (260-char) limit.
#    conda-pack follows hardlinks back to the rattler cache, where some package
#    paths (e.g. viskores headers) exceed 260 chars and crash os.stat().
#    The patch prepends \\?\ at the two call sites that receive the raw path.
# --------------------------------------------------------------------------
Write-Step "修补 tarfile.py（\\?\长路径支持）"
$patchScript = Join-Path $scriptDir "_patch_tarfile.py"
foreach ($envName in @("gui", "mie")) {
    $py       = Join-Path $repoRoot ".pixi\envs\$envName\python.exe"
    $tarfile  = Join-Path $repoRoot ".pixi\envs\$envName\Lib\tarfile.py"
    if (Test-Path $py) {
        & $py $patchScript $tarfile
    }
}

# --------------------------------------------------------------------------
# 2. Pack conda environments (uncompressed .tar — Inno Setup LZMA is better)
# --------------------------------------------------------------------------
Write-Step "打包 gui 环境（conda-pack）"
$guiTar = Join-Path $distDir "gui_packed.tar"
if ($Repack -and (Test-Path $guiTar)) {
    Write-Host "  -Repack 指定，删除旧包: $guiTar"
    Remove-Item -Path $guiTar -Force
}
if (Test-Path $guiTar) {
    Write-Host "  已存在，跳过（使用 -Repack 强制重新打包）: $guiTar"
} else {
    & $condaPackGui -p $guiEnvPath --format tar --output $guiTar `
        --exclude "Library/include/*" `
        --exclude "Library/lib/*.lib" `
        --exclude "Library/lib/*.a" `
        --exclude "Library/lib/cmake/*" `
        --exclude "__pycache__/*"
    if ($LASTEXITCODE -ne 0) { Write-Error "gui 环境打包失败"; exit 1 }
}

Write-Step "打包 mie 环境（conda-pack）"
$mieTar = Join-Path $distDir "mie_packed.tar"
if ($Repack -and (Test-Path $mieTar)) {
    Write-Host "  -Repack 指定，删除旧包: $mieTar"
    Remove-Item -Path $mieTar -Force
}
if (Test-Path $mieTar) {
    Write-Host "  已存在，跳过（使用 -Repack 强制重新打包）: $mieTar"
} else {
    & $condaPackMie -p $mieEnvPath --format tar --output $mieTar `
        --exclude "Library/include/*" `
        --exclude "Library/lib/*.lib" `
        --exclude "Library/lib/*.a" `
        --exclude "Library/lib/cmake/*" `
        --exclude "__pycache__/*"
    if ($LASTEXITCODE -ne 0) { Write-Error "mie 环境打包失败"; exit 1 }
}

# --------------------------------------------------------------------------
# 3. Extract Julia portable zip (user must place it in dist/ beforehand)
# --------------------------------------------------------------------------
Write-Step "检查 Julia 便携包"
$juliaZip  = Join-Path $distDir "julia-win64.zip"
$juliaDir  = Join-Path $distDir "julia"

if (Test-Path $juliaDir) {
    Write-Host "  Julia 目录已存在，跳过解压: $juliaDir"
} elseif (Test-Path $juliaZip) {
    Write-Host "  解压 $juliaZip …"
    Expand-Archive -Path $juliaZip -DestinationPath $distDir -Force
    # Julia zip 内通常有一层以版本命名的子目录，将其重命名为 julia/
    $inner = Get-ChildItem -Path $distDir -Directory | Where-Object { $_.Name -like "julia-*" } | Select-Object -First 1
    if ($inner) {
        Rename-Item -Path $inner.FullName -NewName "julia"
    }
    Write-Host "  Julia 解压完成: $juliaDir"
} else {
    Write-Error "Julia portable zip not found at $juliaZip`n`nDownload Windows x86_64 Portable (.zip) from https://julialang.org/downloads/`nRename it to julia-win64.zip and place it in $distDir"
    exit 1
}

Write-Step "预装 Julia 项目依赖（离线首跑保障）"
$juliaExe = Join-Path $juliaDir "bin\julia.exe"
$juliaProject = Join-Path $repoRoot "temp\lidar_1d\julia"
$juliaDepotDist = Join-Path $distDir "julia_depot"
Assert-Exists $juliaExe "Julia executable not found in dist/julia/bin"
Assert-Exists $juliaProject "Julia project not found: temp/lidar_1d/julia"

if (Test-Path $juliaDepotDist) {
    if ($Repack) {
        Write-Host "  -Repack 指定，清理旧 Julia depot: $juliaDepotDist"
        try {
            Get-ChildItem -Path $juliaDepotDist -Recurse -Force -ErrorAction SilentlyContinue | ForEach-Object {
                try { $_.Attributes = $_.Attributes -band (-bnot [IO.FileAttributes]::ReadOnly) } catch {}
            }
            Remove-Item -Path $juliaDepotDist -Recurse -Force -ErrorAction Stop
        } catch {
            Write-Warning "无法完全删除旧 Julia depot（将复用目录继续更新）: $($_.Exception.Message)"
        }
    } else {
        Write-Host "  复用已有 Julia depot（使用 -Repack 强制重建）: $juliaDepotDist"
    }
}
New-Item -ItemType Directory -Force -Path $juliaDepotDist | Out-Null

$oldDepot = $env:JULIA_DEPOT_PATH
try {
    $env:JULIA_DEPOT_PATH = $juliaDepotDist
    Write-Host "  Julia instantiate（下载包源码到 depot）..."
    & $juliaExe "--project=$juliaProject" -e "using Pkg; Pkg.instantiate()"
    if ($LASTEXITCODE -ne 0) { Write-Error "Julia Pkg.instantiate() 失败"; exit 1 }

    Write-Host "  Julia smoke test（验证包源码完整性）..."
    & $juliaExe "--project=$juliaProject" -e "using JSON3, TransitionMatrices; @assert isdefined(Main, :JSON3)"
    if ($LASTEXITCODE -ne 0) { Write-Error "Julia 关键依赖加载失败"; exit 1 }
} finally {
    if ($null -ne $oldDepot -and $oldDepot -ne "") {
        $env:JULIA_DEPOT_PATH = $oldDepot
    } else {
        Remove-Item Env:JULIA_DEPOT_PATH -ErrorAction SilentlyContinue
    }
}
Write-Host "  Julia 依赖已预装到: $juliaDepotDist"

# --------------------------------------------------------------------------
# 3.5. Copy default result set (if exists)
# --------------------------------------------------------------------------
Write-Step "复制默认结果集（如果存在）"
$defaultResultSrc = Join-Path $repoRoot "temp\lidar_1d\default_result"
$defaultResultDist = Join-Path $distDir "default_result"

if (Test-Path $defaultResultSrc) {
    Write-Host "  发现默认结果集，复制到 dist/"
    if (Test-Path $defaultResultDist) {
        Remove-Item -Path $defaultResultDist -Recurse -Force
    }
    Copy-Item -Path $defaultResultSrc -Destination $defaultResultDist -Recurse -Force
    Write-Host "  默认结果集已复制"
} else {
    Write-Error "  默认结果集不存在，已中止打包。请先生成并放置 temp/lidar_1d/default_result/"
    exit 1
}

Write-Step "导出隐藏种子缓存"
$seedDistRoot = Join-Path $distDir "cache_store"
$seedDistVisibleRoot = Join-Path $seedDistRoot "seeds"
$seedDistIndexRoot = Join-Path $seedDistRoot "index"
New-Item -ItemType Directory -Force -Path $seedDistRoot | Out-Null
if (Test-Path $seedExportVisibleRoot) {
    Copy-Tree $seedExportVisibleRoot $seedDistVisibleRoot
} else {
    Write-Host "  未找到种子缓存: $seedExportVisibleRoot"
}
if (Test-Path $seedExportIndexRoot) {
    Copy-Tree $seedExportIndexRoot $seedDistIndexRoot
}
if (Test-Path (Join-Path $seedExportRoot "manifest.json")) {
    Copy-Item -Path (Join-Path $seedExportRoot "manifest.json") -Destination (Join-Path $seedDistRoot "manifest.json") -Force
}

# --------------------------------------------------------------------------
# 4. Compile Go launcher
# --------------------------------------------------------------------------
Write-Step "编译启动器 LidarSim.exe"
$launcherExe = Join-Path $distDir "LidarSim.exe"
Push-Location $launcherDir
try {
    # -H windowsgui: no console window; -s: strip debug symbols
    $ldflags = "-H windowsgui -s -w"
    & $go build -ldflags $ldflags -o $launcherExe .
    if ($LASTEXITCODE -ne 0) { Write-Error "Go 编译失败"; exit 1 }
} finally {
    Pop-Location
}
Write-Host "  生成: $launcherExe"

# --------------------------------------------------------------------------
# 5. Run Inno Setup
# --------------------------------------------------------------------------
Write-Step "运行 Inno Setup 生成安装包"
$issFile = Join-Path $scriptDir "installer.iss"
Assert-Exists $issFile "installer.iss 缺失，无法生成安装包。"
& $iscc "/DMyAppVersion=$AppVersion" $issFile
if ($LASTEXITCODE -ne 0) { Write-Error "Inno Setup 编译失败"; exit 1 }

$setupExe = Join-Path $distDir "LidarSimSetup.exe"
Assert-Exists $setupExe "Inno Setup 未输出安装包。"
Write-Host ""
Write-Host "======================================================" -ForegroundColor Green
Write-Host "  安装包已生成: $setupExe" -ForegroundColor Green
$sizeMB = [math]::Round((Get-Item $setupExe).Length / 1MB, 0)
Write-Host "  文件大小: ${sizeMB} MB" -ForegroundColor Green
Write-Host "======================================================" -ForegroundColor Green
