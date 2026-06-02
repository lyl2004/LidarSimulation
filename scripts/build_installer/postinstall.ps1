#Requires -Version 5.1
param(
    [Parameter(Mandatory)]
    [string]$InstallDir
)

$ErrorActionPreference = "Stop"

$logFile = Join-Path $InstallDir "postinstall.log"
$diagEnabled = ($env:LIDAR_DIAGNOSTICS -eq "1")
$diagRunId = if ([string]::IsNullOrWhiteSpace($env:LIDAR_DIAGNOSTICS_RUN_ID)) { "postinstall_$(Get-Date -Format 'yyyyMMdd_HHmmss')" } else { $env:LIDAR_DIAGNOSTICS_RUN_ID }
$diagRoot = if ([string]::IsNullOrWhiteSpace($env:LIDAR_DIAGNOSTICS_ROOT)) { Join-Path $InstallDir "diagnostics" } else { $env:LIDAR_DIAGNOSTICS_ROOT }
$diagRunDir = Join-Path $diagRoot "runs\$diagRunId"
$diagInstallDir = Join-Path $diagRunDir "install"
$diagTimeline = Join-Path $diagInstallDir "install_timeline.jsonl"
$diagSummary = Join-Path $diagInstallDir "install_summary.json"

function Write-DiagJson([string]$path, $payload) {
    if (-not $diagEnabled) { return }
    New-Item -ItemType Directory -Force -Path (Split-Path $path -Parent) | Out-Null
    $payload | ConvertTo-Json -Depth 10 | Out-File -FilePath $path -Encoding utf8
}

function Write-DiagEvent([string]$stage, [string]$status = "ok", [double]$elapsedMs = 0, $payload = $null) {
    if (-not $diagEnabled) { return }
    New-Item -ItemType Directory -Force -Path $diagInstallDir | Out-Null
    $record = [ordered]@{
        run_id = $diagRunId
        ts = (Get-Date).ToUniversalTime().ToString("o")
        component = "install"
        stage = $stage
        status = $status
        elapsed_ms = $elapsedMs
        payload = $payload
    }
    ($record | ConvertTo-Json -Depth 10 -Compress) | Add-Content -Path $diagTimeline -Encoding UTF8
}

if ($diagEnabled) {
    New-Item -ItemType Directory -Force -Path $diagInstallDir | Out-Null
    Write-DiagEvent "postinstall_started" "begin" 0 @{ install_dir = $InstallDir }
}

function Write-Step([string]$msg) {
    $ts = Get-Date -Format "HH:mm:ss"
    $line = "[$ts] $msg"
    Write-Output $line
    Add-Content -Path $logFile -Value $line -Encoding UTF8
}

function Get-ElapsedMilliseconds($startTime) {
    if ($null -eq $startTime) {
        return 0.0
    }
    return (New-TimeSpan -Start ([datetime]$startTime) -End (Get-Date)).TotalMilliseconds
}

$InstallDir = $InstallDir.TrimEnd('\', '/')

$packsDir = Join-Path $InstallDir "_packs"
$envsRoot = Join-Path $InstallDir ".pixi\envs"

function Unpack-Env([string]$tarPath, [string]$envName) {
    $stepT0 = Get-Date
    $dest = Join-Path $envsRoot $envName
    if (-not (Test-Path $dest)) {
        New-Item -ItemType Directory -Force -Path $dest | Out-Null
    }

    Write-DiagEvent "unpack_env_started" "begin" 0 @{ env_name = $envName; tar_path = $tarPath }
    Write-Step "Extracting $envName env from $tarPath ..."
    $tar = "$env:SystemRoot\System32\tar.exe"
    if (-not (Test-Path $tar)) {
        throw "tar.exe not found at $tar — requires Windows 10 1903 or later."
    }
    & $tar -xf $tarPath -C $dest
    if ($LASTEXITCODE -ne 0) { throw "tar extraction failed for $envName (exit $LASTEXITCODE)" }

    Write-DiagEvent "conda_unpack_started" "begin" 0 @{ env_name = $envName }
    Write-Step "Running conda-unpack for $envName ..."
    $unpack = Join-Path $dest "Scripts\conda-unpack.exe"
    if (Test-Path $unpack) {
        & $unpack
    } else {
        $py     = Join-Path $dest "python.exe"
        $script = Join-Path $dest "Scripts\conda-unpack"
        if (Test-Path $py) {
            & $py $script
        } else {
            throw "conda-unpack not found: $unpack"
        }
    }
    if ($LASTEXITCODE -ne 0) { throw "conda-unpack failed for $envName (exit $LASTEXITCODE)" }
    Write-DiagEvent "conda_unpack_completed" "ok" (Get-ElapsedMilliseconds -startTime $stepT0) @{ env_name = $envName; dest = $dest }
    Write-Step "$envName env ready."
    Write-DiagEvent "unpack_env_completed" "ok" (Get-ElapsedMilliseconds -startTime $stepT0) @{ env_name = $envName; dest = $dest }
}

try {
    $overallT0 = Get-Date
    Write-Step "Post-install started. InstallDir=$InstallDir"
    New-Item -ItemType Directory -Force -Path $envsRoot | Out-Null

    Unpack-Env (Join-Path $packsDir "gui_packed.tar") "gui"
    Unpack-Env (Join-Path $packsDir "mie_packed.tar") "mie"

    $juliaDepot = Join-Path $InstallDir "julia_depot"
    New-Item -ItemType Directory -Force -Path $juliaDepot | Out-Null

    $juliaDepotPackages = Join-Path $juliaDepot "packages"
    if (-not (Test-Path $juliaDepotPackages)) {
        Write-Step "Warning: julia_depot does not contain preinstalled packages; first run may require network."
    } else {
        Write-Step "Julia depot preloaded: $juliaDepotPackages"
    }

    $juliaExe = Join-Path $InstallDir "julia\bin\julia.exe"
    if (-not (Test-Path $juliaExe)) {
        throw "Julia runtime missing: $juliaExe"
    }
    Write-Step "Julia runtime found: $juliaExe"

    $juliaProject = Join-Path $InstallDir "temp\lidar_1d\julia"
    if (-not (Test-Path $juliaProject)) {
        throw "Julia project directory missing: $juliaProject"
    }
    Write-Step "Julia project found: $juliaProject"

    # 在用户机现场执行 Pkg.precompile()。
    # 这样生成的 .ji / pkgimage 嵌入用户机的 depot 路径（而不是构建机路径），
    # 彻底避开 PackageCompiler sysimage 把 _jll 包绝对路径烘焙进二进制的问题
    # （TransitionMatrices → Arblib → FLINT_jll 会 dlopen libflint.dll，路径必须正确）。
    $precompileT0 = Get-Date
    Write-DiagEvent "julia_precompile_started" "begin" 0 @{ julia_exe = $juliaExe; julia_project = $juliaProject }
    Write-Step "Precompiling Julia packages on this machine (1-3 min)..."
    $env:JULIA_DEPOT_PATH = $juliaDepot
    $env:JULIA_PKG_PRECOMPILE_AUTO = "1"
    & $juliaExe "--project=$juliaProject" -e "using Pkg; Pkg.precompile()"
    if ($LASTEXITCODE -ne 0) {
        Write-Step "Warning: Julia precompile returned non-zero ($LASTEXITCODE); first run may fall back to JIT compile."
        Write-DiagEvent "julia_precompile_completed" "error" (Get-ElapsedMilliseconds -startTime $precompileT0) @{ exit_code = $LASTEXITCODE }
    } else {
        Write-Step "Julia precompile complete."
        Write-DiagEvent "julia_precompile_completed" "ok" (Get-ElapsedMilliseconds -startTime $precompileT0) @{ exit_code = 0 }
    }

    $cacheStoreRoot = Join-Path $InstallDir "temp\lidar_1d\cache_store"
    $cacheStoreSeeds = Join-Path $cacheStoreRoot "seeds"
    $cacheStoreIndex = Join-Path $cacheStoreRoot "index"
    New-Item -ItemType Directory -Force -Path $cacheStoreSeeds | Out-Null
    New-Item -ItemType Directory -Force -Path $cacheStoreIndex | Out-Null
    Write-Step "Cache store ready: $cacheStoreRoot"

    # Initialize default result set as active state
    $defaultResult = Join-Path $InstallDir "temp\lidar_1d\default_result"
    if (Test-Path $defaultResult) {
        Write-Step "Initializing default result set as active state"
        $historyDir = Join-Path $InstallDir "temp\lidar_1d\run_history"
        New-Item -ItemType Directory -Force -Path $historyDir | Out-Null

        $manifestPath = Join-Path $historyDir "manifest.json"
        $manifest = @{
            runs = @()
            active_id = "default"
        }
        $manifest | ConvertTo-Json -Depth 10 | Out-File -FilePath $manifestPath -Encoding utf8
        Write-Step "Default result set is now active"
        Write-DiagEvent "default_result_initialized" "ok" 0 @{ manifest_path = $manifestPath }
    } else {
        Write-Step "No default result set found, skipping initialization"
    }

    Write-DiagEvent "packs_cleanup_started" "begin" 0 @{ packs_dir = $packsDir }
    Write-Step "Cleaning up pack files ..."
    Remove-Item -Path $packsDir -Recurse -Force
    Write-DiagEvent "packs_cleanup_completed" "ok" 0 @{ packs_dir = $packsDir }

    Write-Step "Post-install completed successfully."
    Write-DiagJson $diagSummary @{
        status = "success"
        install_dir = $InstallDir
        elapsed_ms = (Get-ElapsedMilliseconds -startTime $overallT0)
    }
    Write-DiagEvent "postinstall_completed" "ok" (Get-ElapsedMilliseconds -startTime $overallT0) @{ install_dir = $InstallDir }
} catch {
    $errMsg = "POST-INSTALL ERROR: $_"
    Add-Content -Path $logFile -Value $errMsg -Encoding UTF8
    # Write error marker beside the launcher so LidarSim.log also captures it
    Add-Content -Path (Join-Path $InstallDir "LidarSim.log") -Value $errMsg -Encoding UTF8
    Write-DiagJson $diagSummary @{
        status = "error"
        install_dir = $InstallDir
        error = $errMsg
    }
    Write-DiagEvent "postinstall_failed" "error" 0 @{ error = $errMsg }
    exit 1
}
