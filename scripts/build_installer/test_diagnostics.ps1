#Requires -Version 5.1
param(
    [ValidateSet("Dev", "Installed")]
    [string]$Mode = "Installed",

    [ValidateSet("ColdOnly", "ColdHotCompare")]
    [string]$TestProfile = "ColdHotCompare",

    [string]$RepoRoot,
    [string]$InstallDir = (Join-Path $env:LOCALAPPDATA "Programs\LidarSim"),
    [string]$SetupExe,
    [string]$BaseRunId = ("diag_" + (Get-Date -Format "yyyyMMdd_HHmmss")),
    [string]$DiagnosticsRoot,
    [int]$TimeoutSec = 30,
    [int]$ComputeTimeoutSec = 3600,
    [switch]$KeepInstallDir,
    [switch]$SkipComputeSmoke,
    [switch]$SkipColdGuiSmoke,
    [switch]$SkipHotGuiSmoke,
    [switch]$HotComputeNoClean,
    [switch]$KeepOutputs
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

function Write-Step([string]$msg) {
    Write-Host "`n>>> $msg" -ForegroundColor Cyan
}

function Fail([string]$msg) {
    Write-Host "[FAIL] $msg" -ForegroundColor Red
    exit 1
}

function Assert-PathExists([string]$path, [string]$label) {
    if (-not (Test-Path $path)) {
        Fail "$label missing: $path"
    }
}

function Read-JsonFile([string]$path) {
    Assert-PathExists $path "JSON file"
    return Get-Content $path -Raw | ConvertFrom-Json
}

function Wait-Path([string]$path, [int]$timeoutSec, [string]$label) {
    $deadline = (Get-Date).AddSeconds($timeoutSec)
    while ((Get-Date) -lt $deadline) {
        if (Test-Path $path) {
            Write-Host "[PASS] $label created: $path" -ForegroundColor Green
            return
        }
        Start-Sleep -Milliseconds 500
    }
    Fail "$label timed out: $path"
}

function Stop-TestProcess($proc, [string]$name) {
    if ($null -eq $proc) { return }
    try {
        if (-not $proc.HasExited) {
            Stop-Process -Id $proc.Id -Force -Confirm:$false
            Write-Host "[INFO] Stopped process: $name (PID=$($proc.Id))"
        }
    } catch {
        Write-Host "[WARN] Failed to stop process: $name - $($_.Exception.Message)" -ForegroundColor Yellow
    }
}

function Resolve-TestContext {
    $ctx = [ordered]@{}
    $ctx.Mode = $Mode
    $ctx.BaseRunId = $BaseRunId

    if ($Mode -eq "Dev") {
        if ([string]::IsNullOrWhiteSpace($RepoRoot)) {
            Fail "RepoRoot is required in Dev mode"
        }
        $ctx.RepoRoot = (Resolve-Path $RepoRoot).Path
        $ctx.Root = $ctx.RepoRoot
        $ctx.GuiPython = Join-Path $ctx.Root ".pixi\envs\gui\python.exe"
        $ctx.MiePython = Join-Path $ctx.Root ".pixi\envs\mie\python.exe"
        $ctx.GuiEntry = Join-Path $ctx.Root "app\demo_ui.py"
        $ctx.ComputeEntry = Join-Path $ctx.Root "temp\lidar_1d\lidar_1d_simulation.py"
        $ctx.ComputeWrapper = Join-Path $ctx.Root "temp\lidar_1d\make_final_figures.py"
        $ctx.LauncherExe = $null
        if ([string]::IsNullOrWhiteSpace($DiagnosticsRoot)) {
            $ctx.DiagnosticsRoot = Join-Path $ctx.Root "diagnostics"
        } else {
            $ctx.DiagnosticsRoot = $DiagnosticsRoot
        }
    } else {
        $ctx.Root = $InstallDir
        if ([string]::IsNullOrWhiteSpace($ctx.Root)) {
            Fail "Could not resolve install directory"
        }

        $resolvedSetupExe = $SetupExe
        if ([string]::IsNullOrWhiteSpace($resolvedSetupExe)) {
            $defaultSetupExe = Join-Path $PSScriptRoot "LidarSimSetup.exe"
            if (-not (Test-Path $ctx.Root) -and (Test-Path $defaultSetupExe)) {
                $resolvedSetupExe = $defaultSetupExe
            }
        }

        if (-not (Test-Path $ctx.Root) -and [string]::IsNullOrWhiteSpace($resolvedSetupExe)) {
            Fail "Default install directory does not exist, and LidarSimSetup.exe was not found next to the script"
        }

        $ctx.SetupExe = $resolvedSetupExe
        $ctx.GuiPython = Join-Path $ctx.Root ".pixi\envs\gui\python.exe"
        $ctx.MiePython = Join-Path $ctx.Root ".pixi\envs\mie\python.exe"
        $ctx.GuiEntry = Join-Path $ctx.Root "app\demo_ui.py"
        $ctx.ComputeEntry = Join-Path $ctx.Root "temp\lidar_1d\lidar_1d_simulation.py"
        $ctx.ComputeWrapper = Join-Path $ctx.Root "temp\lidar_1d\make_final_figures.py"
        $ctx.LauncherExe = Join-Path $ctx.Root "LidarSim.exe"
        $ctx.JuliaExe = Join-Path $ctx.Root "julia\bin\julia.exe"
        if ([string]::IsNullOrWhiteSpace($DiagnosticsRoot)) {
            $ctx.DiagnosticsRoot = Join-Path $ctx.Root "diagnostics"
        } else {
            $ctx.DiagnosticsRoot = $DiagnosticsRoot
        }
    }

    return [PSCustomObject]$ctx
}

function New-ChildRunContext($ctx, [string]$Phase) {
    $runId = "{0}_{1}" -f $ctx.BaseRunId, $Phase.ToLowerInvariant()
    $child = [ordered]@{}
    foreach ($property in $ctx.PSObject.Properties) {
        $child[$property.Name] = $property.Value
    }
    $child.RunPhase = $Phase.ToLowerInvariant()
    $child.RunId = $runId
    $child.RunDir = Join-Path $ctx.DiagnosticsRoot (Join-Path "runs" $runId)
    $child.OutputDir = Join-Path $ctx.Root ("temp\diagnostic_smoke_output_{0}" -f $child.RunPhase)
    return [PSCustomObject]$child
}

function Set-DiagnosticsEnv($ctx) {
    $env:LIDAR_DIAGNOSTICS = "1"
    $env:LIDAR_DIAGNOSTICS_MODE = if ($ctx.Mode -eq "Dev") { "dev_smoke_{0}" -f $ctx.RunPhase } else { "installed_smoke_{0}" -f $ctx.RunPhase }
    $env:LIDAR_DIAGNOSTICS_PHASE = $ctx.RunPhase
    $env:LIDAR_DIAGNOSTICS_ROOT = $ctx.DiagnosticsRoot
    $env:LIDAR_DIAGNOSTICS_RUN_ID = $ctx.RunId

    if ($ctx.Mode -eq "Installed") {
        $env:LIDAR_INSTALL_DIR = $ctx.Root
        $env:LIDAR_MIE_PYTHON = $ctx.MiePython
        $env:JULIA_EXE = $ctx.JuliaExe
        $env:JULIA_BINDIR = Split-Path $ctx.JuliaExe -Parent
        $env:JULIA_DEPOT_PATH = Join-Path $ctx.Root "julia_depot"
    } else {
        Remove-Item Env:LIDAR_INSTALL_DIR -ErrorAction SilentlyContinue
        $env:LIDAR_MIE_PYTHON = $ctx.MiePython
        Remove-Item Env:JULIA_EXE -ErrorAction SilentlyContinue
        Remove-Item Env:JULIA_BINDIR -ErrorAction SilentlyContinue

        $devJuliaDepot = Join-Path $ctx.Root "scripts\build_installer\dist\julia_depot"
        if (Test-Path $devJuliaDepot) {
            $env:JULIA_DEPOT_PATH = $devJuliaDepot
        } else {
            Remove-Item Env:JULIA_DEPOT_PATH -ErrorAction SilentlyContinue
        }
    }
}

function Clear-DiagnosticsEnv {
    Remove-Item Env:LIDAR_DIAGNOSTICS -ErrorAction SilentlyContinue
    Remove-Item Env:LIDAR_DIAGNOSTICS_MODE -ErrorAction SilentlyContinue
    Remove-Item Env:LIDAR_DIAGNOSTICS_PHASE -ErrorAction SilentlyContinue
    Remove-Item Env:LIDAR_DIAGNOSTICS_ROOT -ErrorAction SilentlyContinue
    Remove-Item Env:LIDAR_DIAGNOSTICS_RUN_ID -ErrorAction SilentlyContinue
    Remove-Item Env:LIDAR_INSTALL_DIR -ErrorAction SilentlyContinue
    Remove-Item Env:LIDAR_MIE_PYTHON -ErrorAction SilentlyContinue
    Remove-Item Env:JULIA_EXE -ErrorAction SilentlyContinue
    Remove-Item Env:JULIA_BINDIR -ErrorAction SilentlyContinue
    Remove-Item Env:JULIA_DEPOT_PATH -ErrorAction SilentlyContinue
}

function Invoke-SilentInstall($ctx) {
    if ([string]::IsNullOrWhiteSpace($ctx.SetupExe)) {
        return
    }
    Assert-PathExists $ctx.SetupExe "Installer"

    if (Test-Path $ctx.Root) {
        Remove-Item -Recurse -Force $ctx.Root
    }
    New-Item -ItemType Directory -Force -Path (Split-Path $ctx.Root -Parent) | Out-Null

    Write-Step "Silent install to $($ctx.Root)"
    $args = @(
        "/VERYSILENT",
        "/SUPPRESSMSGBOXES",
        "/NORESTART",
        "/SP-",
        "/DIR=`"$($ctx.Root)`""
    )
    $proc = Start-Process -FilePath $ctx.SetupExe -ArgumentList $args -Wait -PassThru
    if ($proc.ExitCode -ne 0) {
        Fail "Installer exited with code: $($proc.ExitCode)"
    }
}

function Assert-Preflight($ctx) {
    Write-Step "Preflight"
    Assert-PathExists $ctx.GuiPython "GUI Python"
    Assert-PathExists $ctx.MiePython "MIE Python"
    Assert-PathExists $ctx.GuiEntry "GUI entry"
    Assert-PathExists $ctx.ComputeEntry "Compute entry"
    Assert-PathExists $ctx.ComputeWrapper "Compute wrapper"

    if ($ctx.Mode -eq "Installed") {
        Assert-PathExists $ctx.LauncherExe "Launcher"
        Assert-PathExists $ctx.JuliaExe "Julia"
    }
}

function Assert-InstallDiagnostics($ctx) {
    if ($ctx.Mode -ne "Installed") { return }
    Write-Step "Check install diagnostics"

    $postinstallLog = Join-Path $ctx.Root "postinstall.log"
    $installTimeline = Join-Path $ctx.RunDir "install\install_timeline.jsonl"
    $installSummary = Join-Path $ctx.RunDir "install\install_summary.json"

    Wait-Path $postinstallLog $TimeoutSec "postinstall.log"

    $postLog = Get-Content $postinstallLog -Raw
    if ($postLog -match "POST-INSTALL ERROR") {
        Fail "postinstall.log contains POST-INSTALL ERROR"
    }

    $deadline = (Get-Date).AddSeconds($TimeoutSec)
    while ((Get-Date) -lt $deadline) {
        if ((Test-Path $installTimeline) -and (Test-Path $installSummary)) {
            Write-Host "[PASS] install timeline created: $installTimeline" -ForegroundColor Green
            Write-Host "[PASS] install summary created: $installSummary" -ForegroundColor Green
            return
        }
        Start-Sleep -Milliseconds 500
    }

    Write-Host "[WARN] install diagnostics were not written for this run; continuing because postinstall completed successfully." -ForegroundColor Yellow
}

function Invoke-GuiSmoke($ctx, [switch]$SkipGuiSmoke) {
    if ($SkipGuiSmoke) { return }

    Write-Step ("GUI smoke ({0})" -f $ctx.RunPhase)
    $proc = $null
    try {
        if ($ctx.Mode -eq "Dev") {
            $proc = Start-Process -FilePath $ctx.GuiPython -ArgumentList @($ctx.GuiEntry) -PassThru -WindowStyle Hidden
            Wait-Path (Join-Path $ctx.RunDir "summary.json") $TimeoutSec "run summary"
            Wait-Path (Join-Path $ctx.RunDir "gui\startup_timeline.jsonl") $TimeoutSec "GUI startup timeline"
        } else {
            $proc = Start-Process -FilePath $ctx.LauncherExe -PassThru -WindowStyle Hidden
            Wait-Path (Join-Path $ctx.RunDir "summary.json") $TimeoutSec "run summary"
            Wait-Path (Join-Path $ctx.RunDir "launcher\launcher_timeline.jsonl") $TimeoutSec "launcher timeline"
            Wait-Path (Join-Path $ctx.RunDir "gui\startup_timeline.jsonl") $TimeoutSec "GUI startup timeline"
        }
    } finally {
        Stop-TestProcess $proc ("GUI smoke {0}" -f $ctx.RunPhase)
    }
}

function Invoke-ComputeSmokeWithOptions($ctx, [bool]$UseClean) {
    if ($SkipComputeSmoke) { return }

    Write-Step ("1D compute smoke ({0})" -f $ctx.RunPhase)
    if ($UseClean -and (Test-Path $ctx.OutputDir)) {
        Remove-Item -Recurse -Force $ctx.OutputDir
    }

    $args = @(
        $ctx.ComputeWrapper,
        "--precision", "fast",
        "--output", $ctx.OutputDir,
        "--python-cmd", $ctx.MiePython
    )
    if ($UseClean) {
        $args += "--clean"
    }

    $proc = Start-Process -FilePath $ctx.MiePython -ArgumentList $args -PassThru -Wait -WindowStyle Hidden
    if ($proc.ExitCode -ne 0) {
        Fail "1D compute smoke exited with code: $($proc.ExitCode)"
    }

    Wait-Path (Join-Path $ctx.RunDir "simulation_1d\compute_timeline.jsonl") $ComputeTimeoutSec "compute timeline"
    Wait-Path (Join-Path $ctx.RunDir "simulation_1d\cache_report.json") $ComputeTimeoutSec "cache report"
    Wait-Path (Join-Path $ctx.RunDir "simulation_1d\compute_summary.json") $ComputeTimeoutSec "compute summary"
    Wait-Path (Join-Path $ctx.RunDir "simulation_1d\cache_analysis.json") $ComputeTimeoutSec "cache analysis"
    Wait-Path (Join-Path $ctx.RunDir "simulation_1d\tmatrix_task_report.json") $ComputeTimeoutSec "tmatrix task report"
    Wait-Path $ctx.OutputDir $ComputeTimeoutSec "output directory"
    Wait-Path (Join-Path $ctx.OutputDir "summary.json") $ComputeTimeoutSec "output summary"
}

function Assert-GuiDiagnostics($ctx, [switch]$SkipGuiSmoke) {
    if ($SkipGuiSmoke) { return }
    Write-Step ("Check GUI / launcher diagnostics ({0})" -f $ctx.RunPhase)
    $summary = Read-JsonFile (Join-Path $ctx.RunDir "summary.json")
    if (-not $summary.components) {
        Fail "summary.json missing components"
    }
}

function Assert-ExtendedComputeDiagnostics($ctx) {
    if ($SkipComputeSmoke) { return }
    Write-Step ("Check 1D compute diagnostics ({0})" -f $ctx.RunPhase)
    $cache = Read-JsonFile (Join-Path $ctx.RunDir "simulation_1d\cache_report.json")
    $computeSummary = Read-JsonFile (Join-Path $ctx.RunDir "simulation_1d\compute_summary.json")
    $cacheAnalysis = Read-JsonFile (Join-Path $ctx.RunDir "simulation_1d\cache_analysis.json")
    $tmatrixTaskReport = Read-JsonFile (Join-Path $ctx.RunDir "simulation_1d\tmatrix_task_report.json")

    if (-not $cache.optical) {
        Fail "cache_report.json missing optical"
    }
    if (-not $computeSummary.cache_report) {
        Fail "compute_summary.json missing cache_report"
    }
    if (-not $cacheAnalysis.optical) {
        Fail "cache_analysis.json missing optical"
    }
    if ($null -eq $tmatrixTaskReport.task_reports) {
        Fail "tmatrix_task_report.json missing task_reports"
    }
}

function Get-StageElapsedMs($timelinePath, [string]$stageName) {
    $records = Get-Content $timelinePath | ForEach-Object { $_ | ConvertFrom-Json }
    $record = $records | Where-Object { $_.stage -eq $stageName } | Select-Object -Last 1
    if ($null -eq $record) { return $null }
    return [double]$record.elapsed_ms
}

function Get-ScenarioMetrics($timelinePath) {
    $records = Get-Content $timelinePath | ForEach-Object { $_ | ConvertFrom-Json }
    $scenarioRecords = $records | Where-Object { $_.stage -eq "scenario_compute" }
    $result = @{ fog = @{}; haze = @{}; rain = @{} }
    foreach ($record in $scenarioRecords) {
        $group = [string]$record.payload.group
        $key = [string]$record.payload.scenario_key
        if (-not $result.ContainsKey($group)) {
            $result[$group] = @{}
        }
        $result[$group][$key] = @{
            elapsed_ms = [double]$record.elapsed_ms
            cache_source = [string]$record.payload.cache_source
        }
        if ($record.payload.PSObject.Properties.Name -contains "mueller_cache_source") {
            $result[$group][$key]["mueller_cache_source"] = [string]$record.payload.mueller_cache_source
        }
    }
    return $result
}

function Get-CacheFallbackSummary($cacheReport) {
    $opticalFallbackGroups = @()
    if ($null -ne $cacheReport.optical) {
        foreach ($group in @("fog", "haze", "rain")) {
            $entry = $cacheReport.optical.$group
            if ($null -ne $entry -and [bool]$entry.default_fallback_hit) {
                $opticalFallbackGroups += $group
            }
        }
    }

    $muellerFallbackHit = $false
    if ($null -ne $cacheReport.mueller) {
        $muellerFallbackHit = [bool]$cacheReport.mueller.default_fallback_hit
    }

    $tmatrixFallbackHitCount = 0
    if ($null -ne $cacheReport.tmatrix_tasks) {
        $tmatrixFallbackHitCount = [int]$cacheReport.tmatrix_tasks.local_hit_count + [int]$cacheReport.tmatrix_tasks.default_hit_count
    }

    $anyFallback = ($opticalFallbackGroups.Count -gt 0) -or $muellerFallbackHit -or ($tmatrixFallbackHitCount -gt 0)
    return [PSCustomObject]@{
        any_fallback = $anyFallback
        optical_fallback_count = $opticalFallbackGroups.Count
        optical_fallback_groups = $opticalFallbackGroups
        mueller_fallback_hit = $muellerFallbackHit
        tmatrix_fallback_hit_count = $tmatrixFallbackHitCount
    }
}

function Read-RunMetrics($ctx) {
    $computeSummaryPath = Join-Path $ctx.RunDir "simulation_1d\compute_summary.json"
    $cacheReportPath = Join-Path $ctx.RunDir "simulation_1d\cache_report.json"
    $cacheAnalysisPath = Join-Path $ctx.RunDir "simulation_1d\cache_analysis.json"
    $tmatrixTaskReportPath = Join-Path $ctx.RunDir "simulation_1d\tmatrix_task_report.json"
    $timelinePath = Join-Path $ctx.RunDir "simulation_1d\compute_timeline.jsonl"
    $computeSummary = Read-JsonFile $computeSummaryPath
    $cacheReport = Read-JsonFile $cacheReportPath
    $cacheAnalysis = Read-JsonFile $cacheAnalysisPath
    $tmatrixTaskReport = Read-JsonFile $tmatrixTaskReportPath
    return [PSCustomObject]@{
        run_id = $ctx.RunId
        run_phase = $ctx.RunPhase
        run_dir = $ctx.RunDir
        compute_elapsed_s = [double]$computeSummary.elapsed_s
        compute_elapsed_ms = [double]$computeSummary.elapsed_s * 1000.0
        cache_report = $cacheReport
        cache_analysis = $cacheAnalysis
        tmatrix_task_report = $tmatrixTaskReport
        stage_elapsed_ms = [ordered]@{
            tmatrix_batch = Get-StageElapsedMs $timelinePath "tmatrix_batch"
            fog_compute = Get-StageElapsedMs $timelinePath "fog_compute"
            haze_compute = Get-StageElapsedMs $timelinePath "haze_compute"
            rain_compute = Get-StageElapsedMs $timelinePath "rain_compute"
        }
        scenario_metrics = Get-ScenarioMetrics $timelinePath
    }
}

function Assert-WarmRunExpectations($coldMetrics, $hotMetrics) {
    if ($SkipComputeSmoke) { return }
    Write-Step "Check warm run expectations"
    $coldFallback = Get-CacheFallbackSummary $coldMetrics.cache_report
    $hotFallback = Get-CacheFallbackSummary $hotMetrics.cache_report
    if (-not $hotFallback.any_fallback -and $coldFallback.any_fallback) {
        Write-Host "[WARN] Warm run did not show any fallback reuse" -ForegroundColor Yellow
    }
    if ($hotMetrics.compute_elapsed_ms -ge $coldMetrics.compute_elapsed_ms) {
        Write-Host "[WARN] Warm run is not faster than cold run" -ForegroundColor Yellow
    }
}

function Write-CompareSummary($ctx, $coldMetrics, $hotMetrics) {
    Write-Step "Write compare summary"
    $compareRunDir = Join-Path $ctx.DiagnosticsRoot (Join-Path "runs" ("{0}_compare" -f $ctx.BaseRunId))
    New-Item -ItemType Directory -Force -Path $compareRunDir | Out-Null
    $compareSummaryPath = Join-Path $compareRunDir "compare_summary.json"
    $compareReportPath = Join-Path $compareRunDir "compare_report.txt"

    $warmRunImproved = $hotMetrics.compute_elapsed_ms -lt $coldMetrics.compute_elapsed_ms
    $coldFallback = Get-CacheFallbackSummary $coldMetrics.cache_report
    $hotFallback = Get-CacheFallbackSummary $hotMetrics.cache_report
    $fallbackEffective = $hotFallback.any_fallback
    $fallbackDetail = [ordered]@{
        cold = $coldFallback
        hot = $hotFallback
    }
    $topColdBottlenecks = @(
        [PSCustomObject]@{ stage = "tmatrix_batch"; elapsed_ms = $coldMetrics.stage_elapsed_ms.tmatrix_batch },
        [PSCustomObject]@{ stage = "rain_compute"; elapsed_ms = $coldMetrics.stage_elapsed_ms.rain_compute },
        [PSCustomObject]@{ stage = "haze_compute"; elapsed_ms = $coldMetrics.stage_elapsed_ms.haze_compute },
        [PSCustomObject]@{ stage = "fog_compute"; elapsed_ms = $coldMetrics.stage_elapsed_ms.fog_compute }
    ) | Sort-Object elapsed_ms -Descending

    $summary = [ordered]@{
        base_run_id = $ctx.BaseRunId
        cold = $coldMetrics
        hot = $hotMetrics
        warm_run_improved = $warmRunImproved
        fallback_effective = $fallbackEffective
        fallback_detail = $fallbackDetail
        top_cold_bottlenecks = $topColdBottlenecks
    }
    $summary | ConvertTo-Json -Depth 20 | Out-File -FilePath $compareSummaryPath -Encoding utf8

    $lines = @(
        ("base_run_id={0}" -f $ctx.BaseRunId),
        ("cold_run_id={0}" -f $coldMetrics.run_id),
        ("hot_run_id={0}" -f $hotMetrics.run_id),
        ("cold_compute_ms={0}" -f [math]::Round($coldMetrics.compute_elapsed_ms, 3)),
        ("hot_compute_ms={0}" -f [math]::Round($hotMetrics.compute_elapsed_ms, 3)),
        ("warm_run_improved={0}" -f $warmRunImproved.ToString().ToLowerInvariant()),
        ("fallback_effective={0}" -f $fallbackEffective.ToString().ToLowerInvariant()),
        ("fallback_optical_groups={0}" -f ($hotFallback.optical_fallback_groups -join ",")),
        ("fallback_mueller_hit={0}" -f $hotFallback.mueller_fallback_hit.ToString().ToLowerInvariant()),
        ("fallback_tmatrix_hits={0}" -f $hotFallback.tmatrix_fallback_hit_count),
        ("cold_tmatrix_ms={0}" -f [math]::Round($coldMetrics.stage_elapsed_ms.tmatrix_batch, 3)),
        ("hot_tmatrix_ms={0}" -f [math]::Round($hotMetrics.stage_elapsed_ms.tmatrix_batch, 3)),
        ("cold_rain_ms={0}" -f [math]::Round($coldMetrics.stage_elapsed_ms.rain_compute, 3)),
        ("hot_rain_ms={0}" -f [math]::Round($hotMetrics.stage_elapsed_ms.rain_compute, 3))
    )
    Set-Content -Path $compareReportPath -Value $lines -Encoding ascii

    Write-Host "[PASS] compare summary created: $compareSummaryPath" -ForegroundColor Green
    Write-Host "[PASS] compare report created: $compareReportPath" -ForegroundColor Green
}

function Invoke-Phase($ctx, [bool]$UseClean, [bool]$SkipGuiPhase) {
    Set-DiagnosticsEnv $ctx
    try {
        if ($ctx.Mode -eq "Installed" -and $ctx.RunPhase -eq "cold") {
            Assert-InstallDiagnostics $ctx
        }
        Invoke-GuiSmoke $ctx -SkipGuiSmoke:$SkipGuiPhase
        Assert-GuiDiagnostics $ctx -SkipGuiSmoke:$SkipGuiPhase
        Invoke-ComputeSmokeWithOptions $ctx -UseClean:$UseClean
        Assert-ExtendedComputeDiagnostics $ctx
    }
    finally {
        Clear-DiagnosticsEnv
    }
}

function Write-TestSummary($ctx) {
    Write-Step "Test complete"
    Write-Host "Mode            : $($ctx.Mode)" -ForegroundColor Green
    Write-Host "Root            : $($ctx.Root)" -ForegroundColor Green
    Write-Host "DiagnosticsRoot : $($ctx.DiagnosticsRoot)" -ForegroundColor Green
    Write-Host "BaseRunId       : $($ctx.BaseRunId)" -ForegroundColor Green
}

$ctx = Resolve-TestContext

try {
    if ($Mode -eq "Installed" -and -not [string]::IsNullOrWhiteSpace($ctx.SetupExe)) {
        Set-DiagnosticsEnv (New-ChildRunContext $ctx "cold")
        try {
            Invoke-SilentInstall $ctx
        }
        finally {
            Clear-DiagnosticsEnv
        }
    }

    Assert-Preflight $ctx

    if (-not (Test-Path $ctx.DiagnosticsRoot)) {
        New-Item -ItemType Directory -Force -Path $ctx.DiagnosticsRoot | Out-Null
    }

    $coldCtx = New-ChildRunContext $ctx "cold"
    Invoke-Phase $coldCtx -UseClean:$true -SkipGuiPhase:$SkipColdGuiSmoke
    $coldMetrics = Read-RunMetrics $coldCtx

    if ($TestProfile -eq "ColdHotCompare") {
        $hotCtx = New-ChildRunContext $ctx "hot"
        $useCleanForHot = -not $HotComputeNoClean
        if ($HotComputeNoClean) {
            $useCleanForHot = $false
        }
        Invoke-Phase $hotCtx -UseClean:$useCleanForHot -SkipGuiPhase:$SkipHotGuiSmoke
        $hotMetrics = Read-RunMetrics $hotCtx
        Assert-WarmRunExpectations $coldMetrics $hotMetrics
        Write-CompareSummary $ctx $coldMetrics $hotMetrics
    }

    Write-TestSummary $ctx
}
finally {
    Clear-DiagnosticsEnv

    if ((-not $KeepOutputs) -and (Test-Path (Join-Path $ctx.Root "temp\diagnostic_smoke_output_cold"))) {
        Remove-Item -Recurse -Force (Join-Path $ctx.Root "temp\diagnostic_smoke_output_cold")
    }
    if ((-not $KeepOutputs) -and (Test-Path (Join-Path $ctx.Root "temp\diagnostic_smoke_output_hot"))) {
        Remove-Item -Recurse -Force (Join-Path $ctx.Root "temp\diagnostic_smoke_output_hot")
    }

    if ($Mode -eq "Installed" -and -not $KeepInstallDir -and -not [string]::IsNullOrWhiteSpace($ctx.SetupExe)) {
        Write-Host "[INFO] Install directory kept for inspection. Remove it manually later if desired."
    }
}
