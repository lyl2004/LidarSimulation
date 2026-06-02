# 专用测试机完整测试流程（含可迁移命令版）

## 1. 目标

本文档用于在**当前测试机**和**后续迁移到的其他测试机**上，完整验证本项目的：

1. 安装后首次运行
2. 首次计算
3. 缓存复用
4. 失败路径
5. diagnostics 自动日志链路

本流程的目标不是只确认“能运行”，而是系统性回答以下问题：

1. 安装后首次启动到底做了哪些事。
2. 首次计算的主要耗时集中在哪些阶段。
3. 本地缓存、默认结果回退、T-matrix 任务缓存是否按预期工作。
4. diagnostics 开启/关闭时，行为是否符合设计。
5. 测试机上是否存在会污染结果的**本项目局部残留**。
6. 出错时是否仍能留下完整结构化诊断证据。

---

## 2. 适用范围与迁移原则

### 2.1 适用范围

本文档不仅适用于当前设备，也适用于后续任何迁移测试设备。

因此本文档中的命令：

- **一律使用变量和占位路径**
- **不依赖当前机器的固定盘符**
- **不假设某个固定安装目录**
- **不假设当前项目源码目录一定存在于测试机**

### 2.2 迁移原则

迁移到其他设备时，只需要修改本文档最前面的变量区，不需要重写后面的命令。

### 2.3 不允许的动作

测试过程中**不得**修改测试机全局环境，包括但不限于：

- 系统级 Python 缓存
- 系统级 Julia 缓存
- pixi 全局缓存
- PATH 全局变量
- 注册表全局项
- Defender / 杀毒 / 系统策略
- 其他项目的目录、缓存、进程

---

## 3. 测试前准备

### 3.1 测试输入

准备以下内容：

1. 安装包产物
2. 本次测试对应代码版本标识
3. 一个空白或可控目录作为测试根目录
4. 一份测试记录表，用于人工记录结论

### 3.2 建议目录结构

建议在任意测试机上统一使用如下结构：

```text
<TestRoot>
├─ installer
├─ install
├─ diagnostics
├─ records
└─ scratch
```

例如：

```text
D:\LidarSimTest
├─ installer
├─ install
├─ diagnostics
├─ records
└─ scratch
```

### 3.3 测试机手工记录项

在正式开始前，人工记录以下信息：

- 机器型号
- CPU 型号 / 核数 / 线程数
- 内存容量
- 系统版本
- 磁盘类型（SSD/HDD）
- 当前是否联网
- 杀毒/安全软件状态（只记录，不修改）
- 安装目录所在盘符剩余空间

说明：这些信息 diagnostics 中会记录一部分，但建议人工同步记录，便于最终汇总。

---

## 4. 统一变量区（所有设备先改这里）

以下 PowerShell 变量块是整份文档的入口。迁移到其他设备时，**先改这里，再执行后续命令**。

```powershell
$TestRoot     = "D:\LidarSimTest"
$InstallerDir = Join-Path $TestRoot "installer"
$InstallDir   = Join-Path $TestRoot "install"
$DiagRoot     = Join-Path $TestRoot "diagnostics"
$RecordDir    = Join-Path $TestRoot "records"
$ScratchDir   = Join-Path $TestRoot "scratch"

# 安装包路径：按现场实际文件名修改
$InstallerExe = Join-Path $InstallerDir "<InstallerName>.exe"

# 本轮测试编号：每轮都改，避免覆盖
$RunId        = "first_run_01"

# diagnostics 模式
$DiagMode     = "first_run"
```

先执行初始化：

```powershell
New-Item -ItemType Directory -Force -Path $TestRoot, $InstallerDir, $DiagRoot, $RecordDir, $ScratchDir | Out-Null
```

如果安装包文件名不确定，可先列出：

```powershell
Get-ChildItem $InstallerDir -File
```

如果安装后 launcher 可执行文件名不确定，可在安装后执行：

```powershell
Get-ChildItem $InstallDir -File *.exe
```

---

## 5. 通用辅助命令

### 5.1 开启 diagnostics

```powershell
$env:LIDAR_DIAGNOSTICS = "1"
$env:LIDAR_DIAGNOSTICS_MODE = $DiagMode
$env:LIDAR_DIAGNOSTICS_ROOT = $DiagRoot
$env:LIDAR_DIAGNOSTICS_RUN_ID = $RunId
```

### 5.2 关闭 diagnostics

```powershell
Remove-Item Env:LIDAR_DIAGNOSTICS -ErrorAction SilentlyContinue
Remove-Item Env:LIDAR_DIAGNOSTICS_MODE -ErrorAction SilentlyContinue
Remove-Item Env:LIDAR_DIAGNOSTICS_ROOT -ErrorAction SilentlyContinue
Remove-Item Env:LIDAR_DIAGNOSTICS_RUN_ID -ErrorAction SilentlyContinue
```

### 5.3 查看当前 diagnostics 环境变量

```powershell
Get-ChildItem Env:LIDAR_DIAGNOSTICS*
```

### 5.4 本轮 diagnostics run 目录

```powershell
$RunDir = Join-Path $DiagRoot (Join-Path "runs" $RunId)
$RunDir
```

---

## 6. 诊断原则

- diagnostics 默认关闭。
- 只有显式设置环境变量时才生成 diagnostics。
- diagnostics 的目标是提供**完整性能瓶颈诊断参考**，而不是仅记录总耗时。
- diagnostics 文件应自动生成，不依赖人工截图或人工保留控制台输出。
- 若迁移到其他设备，仍应复用同样的 diagnostics 目录结构与命名方式。

---

## 7. 项目级残留检查与清理流程

本节只处理**本项目残留**，不触碰全局环境。

### 7.1 应检查的内容

检查以下路径或对象是否存在旧残留：

1. 旧安装目录
2. 本项目输出目录
3. 本项目日志目录
4. 本项目 diagnostics 目录
5. 本项目运行历史目录
6. 本项目临时缓存目录
7. 本项目相关残留进程
8. 本项目快捷方式（如历史测试生成过）

### 7.2 建议检查路径

按实际安装位置替换路径，重点检查：

- `<InstallDir>`
- `<InstallDir>\outputs`
- `<InstallDir>\log`
- `<InstallDir>\diagnostics`
- `<InstallDir>\temp\lidar_1d\run_history`
- `<InstallDir>\temp\lidar_1d\outputs_high_precision_latest`
- `<InstallDir>\temp\lidar_1d\default_result`
- `<InstallDir>\temp\mie`
- `<InstallDir>\outputs\mie`
- `<InstallDir>\outputs\iitm`

### 7.3 残留检查命令

#### 检查目录残留

```powershell
@(
    $InstallDir,
    (Join-Path $InstallDir "outputs"),
    (Join-Path $InstallDir "log"),
    (Join-Path $InstallDir "diagnostics"),
    (Join-Path $InstallDir "temp\lidar_1d\run_history"),
    (Join-Path $InstallDir "temp\lidar_1d\outputs_high_precision_latest"),
    (Join-Path $InstallDir "temp\lidar_1d\default_result"),
    (Join-Path $InstallDir "temp\mie"),
    (Join-Path $InstallDir "outputs\mie"),
    (Join-Path $InstallDir "outputs\iitm")
) | ForEach-Object {
    [PSCustomObject]@{
        Path   = $_
        Exists = Test-Path $_
    }
}
```

#### 检查与本项目相关的 Python / Julia 进程

```powershell
Get-CimInstance Win32_Process |
Where-Object {
    $_.Name -match 'python|julia' -and (
        $_.CommandLine -match 'demo_ui.py' -or
        $_.CommandLine -match 'mie_worker.py' -or
        $_.CommandLine -match 'iitm_http_worker.py' -or
        $_.CommandLine -match 'iitm_server.jl' -or
        $_.CommandLine -match [regex]::Escape($InstallDir)
    )
} |
Select-Object ProcessId, Name, CommandLine
```

### 7.4 清理要求

若要清理，推荐仅删除以下与本项目直接相关的内容：

- 本项目旧安装目录
- 本项目旧 `outputs`
- 本项目旧 `log`
- 本项目旧 `diagnostics`
- 本项目旧 `temp\lidar_1d\run_history`
- 本项目测试时生成的快捷方式

### 7.5 清理命令模板

#### 删除本项目旧目录

```powershell
@(
    $InstallDir,
    $DiagRoot
) | ForEach-Object {
    if (Test-Path $_) {
        Remove-Item -Recurse -Force $_
    }
}
```

#### 结束与本项目相关的残留进程

执行前先人工确认上一条进程检查结果只包含本项目进程。

```powershell
Get-CimInstance Win32_Process |
Where-Object {
    $_.Name -match 'python|julia' -and (
        $_.CommandLine -match 'demo_ui.py' -or
        $_.CommandLine -match 'mie_worker.py' -or
        $_.CommandLine -match 'iitm_http_worker.py' -or
        $_.CommandLine -match 'iitm_server.jl' -or
        $_.CommandLine -match [regex]::Escape($InstallDir)
    )
} |
ForEach-Object {
    Stop-Process -Id $_.ProcessId -Force -Confirm:$false
}
```

### 7.6 明确禁止的清理动作

以下动作**禁止执行**：

- 清空用户主目录下的 `.julia`
- 清空系统级 pixi 缓存
- 改 PATH
- 改注册表全局项
- 卸载其他软件
- 清理其他项目目录

---

## 8. diagnostics 期望产物

当 diagnostics 开启后，期望在：

- `<DiagRoot>\runs\<RunId>\`

看到至少以下文件。

### 8.1 运行级文件

- `session.json`
- `host_info.json`
- `runtime_env.json`
- `timeline.jsonl`
- `summary.json`
- `residual_check.json`
- `raw_refs\known_logs.json`

### 8.2 launcher / install / gui / simulation 子目录文件

按实际链路，至少应出现下列中的相应文件：

- `launcher\launcher_timeline.jsonl`
- `install\install_timeline.jsonl`
- `install\install_summary.json`
- `gui\startup_timeline.jsonl`
- `gui\recompute_timeline.jsonl`
- `gui\recompute_stdout.log`
- `simulation_1d\compute_timeline.jsonl`
- `simulation_1d\cache_report.json`
- `simulation_1d\compute_summary.json`
- `iitm_worker\worker_timeline.jsonl`
- `mie_worker\worker_timeline.jsonl`

### 8.3 产物检查命令

```powershell
$RunDir = Join-Path $DiagRoot (Join-Path "runs" $RunId)
Get-ChildItem $RunDir -Recurse | Select-Object FullName
```

---

## 9. 标准测试流程总览

完整测试按以下顺序执行：

1. 残留检查与项目级清理
2. 默认关闭验证
3. 开启 diagnostics 的安装阶段验证
4. 开启 diagnostics 的首次启动验证
5. 开启 diagnostics 的首次 fast 重算验证
6. 第二次相同参数重算验证缓存行为
7. 切换参数验证部分缓存失效范围
8. 失败路径验证
9. 结果归档与结论记录

---

## 10. 阶段一：残留检查与基线确认

### 10.1 目标

确保测试机处于“只保留系统原状、不保留本项目历史运行污染”的状态。

### 10.2 操作步骤

1. 记录测试时间与测试机信息。
2. 检查旧安装目录是否存在。
3. 检查旧 diagnostics 是否存在。
4. 检查旧 outputs / log / run_history 是否存在。
5. 检查本项目相关 Python / Julia 残留进程是否存在。
6. 如存在本项目残留，执行**项目级**清理。
7. 清理后再次确认：
   - 不存在旧安装目录残留
   - 不存在旧 diagnostics 残留
   - 不存在本项目残留进程

### 10.3 推荐命令顺序

```powershell
# 1) 目录残留检查
@(
    $InstallDir,
    $DiagRoot
) | ForEach-Object {
    [PSCustomObject]@{ Path = $_; Exists = Test-Path $_ }
}

# 2) 进程检查
Get-CimInstance Win32_Process |
Where-Object {
    $_.Name -match 'python|julia' -and (
        $_.CommandLine -match 'demo_ui.py' -or
        $_.CommandLine -match 'mie_worker.py' -or
        $_.CommandLine -match 'iitm_http_worker.py' -or
        $_.CommandLine -match 'iitm_server.jl' -or
        $_.CommandLine -match [regex]::Escape($InstallDir)
    )
} |
Select-Object ProcessId, Name, CommandLine
```

如确认需要清理：

```powershell
if (Test-Path $InstallDir) { Remove-Item -Recurse -Force $InstallDir }
if (Test-Path $DiagRoot)   { Remove-Item -Recurse -Force $DiagRoot }
New-Item -ItemType Directory -Force -Path $TestRoot, $InstallerDir, $DiagRoot, $RecordDir, $ScratchDir | Out-Null
```

### 10.4 通过标准

- 本项目历史输出已清理
- 测试目录干净
- 没有本项目残留进程
- 未触碰任何系统级全局环境

---

## 11. 阶段二：默认关闭验证

### 11.1 目标

验证在 diagnostics 默认关闭时，程序不生成多余诊断文件，且原行为不变。

### 11.2 操作步骤

1. 清空本轮安装目录。
2. 不设置任何 `LIDAR_DIAGNOSTICS*` 环境变量。
3. 安装程序。
4. 启动 GUI。
5. 观察是否正常进入界面。
6. 执行一次 fast 重算。
7. 等待完成。
8. 检查安装目录下是否自动出现 diagnostics 目录。

### 11.3 命令模板

#### 关闭 diagnostics

```powershell
Remove-Item Env:LIDAR_DIAGNOSTICS -ErrorAction SilentlyContinue
Remove-Item Env:LIDAR_DIAGNOSTICS_MODE -ErrorAction SilentlyContinue
Remove-Item Env:LIDAR_DIAGNOSTICS_ROOT -ErrorAction SilentlyContinue
Remove-Item Env:LIDAR_DIAGNOSTICS_RUN_ID -ErrorAction SilentlyContinue
```

#### 启动安装程序

如果安装包支持图形界面，直接运行：

```powershell
& $InstallerExe
```

如果安装包支持静默安装，请将下面命令中的占位参数替换成现场实际参数：

```powershell
& $InstallerExe <SilentInstallArgs>
```

#### 定位 launcher 可执行文件

```powershell
Get-ChildItem $InstallDir -File *.exe
```

从上一步中确认真正的 launcher 路径后，设为：

```powershell
$LauncherExe = Join-Path $InstallDir "<LauncherName>.exe"
```

#### 启动 GUI

```powershell
& $LauncherExe
```

#### 检查默认关闭时是否生成 diagnostics

```powershell
Test-Path (Join-Path $InstallDir "diagnostics")
Test-Path $DiagRoot
```

### 11.4 期望结果

- GUI 正常打开
- fast 重算成功
- 不应自动生成 diagnostics 目录
- 不应因为 diagnostics 代码接入而改变既有业务结果

---

## 12. 阶段三：安装阶段 diagnostics 验证

### 12.1 目标

验证 postinstall 阶段能自动记录结构化诊断信息。

### 12.2 操作步骤

1. 清空安装目录与 diagnostics 目录。
2. 设置 diagnostics 环境变量。
3. 执行安装。
4. 等待安装流程结束。
5. 检查 diagnostics run 目录。

### 12.3 命令模板

```powershell
# 清理
if (Test-Path $InstallDir) { Remove-Item -Recurse -Force $InstallDir }
if (Test-Path $DiagRoot)   { Remove-Item -Recurse -Force $DiagRoot }
New-Item -ItemType Directory -Force -Path $TestRoot, $InstallerDir, $DiagRoot, $RecordDir, $ScratchDir | Out-Null

# 开启 diagnostics
$env:LIDAR_DIAGNOSTICS = "1"
$env:LIDAR_DIAGNOSTICS_MODE = $DiagMode
$env:LIDAR_DIAGNOSTICS_ROOT = $DiagRoot
$env:LIDAR_DIAGNOSTICS_RUN_ID = $RunId

# 启动安装程序
& $InstallerExe
```

安装完成后检查：

```powershell
$RunDir = Join-Path $DiagRoot (Join-Path "runs" $RunId)
Get-ChildItem $RunDir -Recurse | Select-Object FullName
```

### 12.4 重点检查文件

- `install\install_timeline.jsonl`
- `install\install_summary.json`
- `summary.json`

### 12.5 重点检查事件

应包含或等价包含：

- `postinstall_started`
- `unpack_env_started`
- `unpack_env_completed`
- `conda_unpack_started`
- `conda_unpack_completed`
- `julia_precompile_started`
- `julia_precompile_completed`
- `default_result_initialized`
- `packs_cleanup_started`
- `packs_cleanup_completed`
- `postinstall_completed`

### 12.6 检查命令

```powershell
Get-Content (Join-Path $RunDir "install\install_timeline.jsonl")
Get-Content (Join-Path $RunDir "install\install_summary.json")
Get-Content (Join-Path $RunDir "summary.json")
```

---

## 13. 阶段四：首次启动 diagnostics 验证

### 13.1 目标

验证安装后第一次启动 GUI 时，launcher 与 GUI 启动链的结构化日志完整。

### 13.2 操作步骤

1. 保持 diagnostics 开启。
2. 通过 launcher 启动程序。
3. 不进行重算，先只观察启动阶段。
4. 等待 GUI 完全进入可交互状态。
5. 关闭程序。
6. 检查 diagnostics 文件。

### 13.3 命令模板

```powershell
$LauncherExe = Join-Path $InstallDir "<LauncherName>.exe"
& $LauncherExe
```

关闭 GUI 后检查：

```powershell
$RunDir = Join-Path $DiagRoot (Join-Path "runs" $RunId)
Get-Content (Join-Path $RunDir "launcher\launcher_timeline.jsonl")
Get-Content (Join-Path $RunDir "gui\startup_timeline.jsonl")
Get-Content (Join-Path $RunDir "summary.json")
Get-Content (Join-Path $RunDir "host_info.json")
Get-Content (Join-Path $RunDir "runtime_env.json")
```

### 13.4 通过标准

- GUI 能正常打开
- header 文件齐全
- 启动阶段事件完整
- `host_info.json` 与 `runtime_env.json` 内容合理

---

## 14. 阶段五：首次 fast 重算验证

### 14.1 目标

验证首次计算时，diagnostics 能完整记录主耗时阶段、缓存命中情况和输出摘要。

### 14.2 操作步骤

1. 保持 diagnostics 开启。
2. 启动 GUI。
3. 保持默认参数或当前基准参数。
4. 选择 `fast` 精度。
5. 执行一次重算。
6. 等待重算完全完成。
7. 检查 GUI 输出、结果文件和 diagnostics 文件。

### 14.3 命令模板

这一阶段的“重算动作”主要通过 GUI 执行。

如需在命令行先验证 wrapper 命令解析是否正确，可执行 dry-run：

```powershell
$ScriptRoot = Join-Path $InstallDir "temp\lidar_1d"
$WrapperPy  = Join-Path $ScriptRoot "make_final_figures.py"
$GuiPython  = Join-Path $InstallDir ".pixi\envs\gui\python.exe"

& $GuiPython $WrapperPy --dry-run
```

完成 GUI 重算后检查：

```powershell
$RunDir = Join-Path $DiagRoot (Join-Path "runs" $RunId)

Get-Content (Join-Path $RunDir "gui\recompute_timeline.jsonl")
Get-Content (Join-Path $RunDir "gui\recompute_stdout.log")
Get-Content (Join-Path $RunDir "simulation_1d\compute_timeline.jsonl")
Get-Content (Join-Path $RunDir "simulation_1d\cache_report.json")
Get-Content (Join-Path $RunDir "simulation_1d\compute_summary.json")
Get-Content (Join-Path $RunDir "summary.json")
```

### 14.4 重点检查事件

#### gui
- `recompute_requested`
- `change_detection_finished`
- `param_overrides_written`
- `recompute_subprocess_started`
- `recompute_subprocess_finished`
- `history_saved`
- `active_run_switched`
- `ui_reload_requested`

#### simulation_1d
- `make_final_figures_started`
- `precision_preset_resolved`
- `simulation_wrapper_subprocess_start`
- `simulation_wrapper_subprocess_end`
- `compute_init`
- `cache_plan_built`
- `tmatrix_batch`
- `fog_compute`
- `haze_compute`
- `rain_compute`
- `cache_persist`
- `summary_write`
- `compute_completed`

### 14.5 重点检查缓存报告

`cache_report.json` 至少应能区分：

#### optical
- fog: `hit` / `default_fallback_hit` / `miss`
- haze: `hit` / `default_fallback_hit` / `miss`
- rain: `hit` / `default_fallback_hit` / `miss`

#### mueller
- `hit`
- `default_fallback_hit`
- `miss`

#### tmatrix_tasks
- `local_hit_count`
- `default_hit_count`
- `executed_count`
- `total_count`

---

## 15. 阶段六：第二次同参数重算验证缓存复用

### 15.1 目标

验证完全相同参数下第二次重算能复用已生成缓存，而不是重复做完整首次计算。

### 15.2 操作步骤

1. 不修改任何参数。
2. 保持输出目录与当前 active run 逻辑不变。
3. 再执行一次 `fast` 重算。
4. 等待完成。
5. 对比本次与上次 diagnostics。

### 15.3 命令模板

这一阶段仍通过 GUI 执行第二次重算。

完成后检查：

```powershell
$RunDir = Join-Path $DiagRoot (Join-Path "runs" $RunId)
Get-Content (Join-Path $RunDir "simulation_1d\cache_report.json")
Get-Content (Join-Path $RunDir "simulation_1d\compute_summary.json")
Get-Content (Join-Path $RunDir "simulation_1d\compute_timeline.jsonl")
```

### 15.4 重点关注

- `cache_report.json` 是否体现更多 `hit`
- `tmatrix_tasks.executed_count` 是否下降
- `gui\recompute_stdout.log` 中是否显示缓存命中
- `compute_timeline.jsonl` 中的耗时是否明显下降

---

## 16. 阶段七：参数变更后的局部失效验证

### 16.1 目标

验证缓存失效范围正确，即只重算受影响部分，而不是全部重算或错误复用。

### 16.2 推荐变更方式

分三类分别测试：

#### A. 仅改 fog 参数
例如修改某个 fog 的粒径或折射率虚部。

期望：
- fog 相关缓存失效
- haze/rain 尽量复用

#### B. 仅改 haze 参数
例如修改霾模式参数。

期望：
- haze optical 失效
- 若影响 Mueller/T-matrix，则对应链路重算
- fog/rain 尽量复用

#### C. 仅改 rain 参数
例如修改降雨率。

期望：
- rain 缓存失效
- fog/haze 尽量复用

### 16.3 命令模板

这一阶段参数修改主要通过 GUI 执行。

修改后检查：

```powershell
$RunDir = Join-Path $DiagRoot (Join-Path "runs" $RunId)
Get-Content (Join-Path $RunDir "simulation_1d\cache_report.json")
Get-Content (Join-Path $RunDir "simulation_1d\compute_timeline.jsonl")
```

---

## 17. 阶段八：失败路径验证

### 17.1 目标

验证出错时 diagnostics 仍能保留足够证据。

### 17.2 推荐失败场景

选择一种可控失败即可，不要破坏系统环境。建议优先以下方式：

1. 将输出目录临时指向一个无权限目录
2. 临时让必要文件路径无效
3. 临时让 Julia 可执行路径不可用
4. 人为制造一个可恢复的输入配置错误

### 17.3 推荐命令方式

#### 方案 A：命令行制造 Julia 路径失败

```powershell
$BadRunId = "failure_case_01"
$env:LIDAR_DIAGNOSTICS = "1"
$env:LIDAR_DIAGNOSTICS_MODE = "first_run"
$env:LIDAR_DIAGNOSTICS_ROOT = $DiagRoot
$env:LIDAR_DIAGNOSTICS_RUN_ID = $BadRunId

$GuiPython = Join-Path $InstallDir ".pixi\envs\gui\python.exe"
$WrapperPy = Join-Path $InstallDir "temp\lidar_1d\make_final_figures.py"

& $GuiPython $WrapperPy --precision fast --output (Join-Path $ScratchDir "failure_output") --python-cmd $GuiPython --dry-run
```

上面只验证 dry-run。

如果要做真实失败验证，可在**确认不会影响其他测试**的前提下，使用错误 Julia 路径运行底层脚本：

```powershell
$SimPy = Join-Path $InstallDir "temp\lidar_1d\lidar_1d_simulation.py"
& $GuiPython $SimPy --output (Join-Path $ScratchDir "failure_output") --julia-cmd "Z:\not_exists\julia.exe"
```

### 17.4 检查命令

```powershell
$BadRunDir = Join-Path $DiagRoot (Join-Path "runs" $BadRunId)
Get-ChildItem $BadRunDir -Recurse | Select-Object FullName
Get-Content (Join-Path $BadRunDir "summary.json")
Get-Content (Join-Path $BadRunDir "timeline.jsonl")
```

### 17.5 通过标准

- 主流程失败时 diagnostics 仍然落盘
- 能定位失败阶段
- 能看到错误信息与上下文
- 不因为 diagnostics 自身异常导致主错误被掩盖

---

## 18. diagnostics 快速检查命令清单

### 18.1 查看所有 diagnostics 文件

```powershell
$RunDir = Join-Path $DiagRoot (Join-Path "runs" $RunId)
Get-ChildItem $RunDir -Recurse | Select-Object FullName
```

### 18.2 查看运行级摘要

```powershell
Get-Content (Join-Path $RunDir "summary.json")
```

### 18.3 查看 simulation_1d 缓存报告

```powershell
Get-Content (Join-Path $RunDir "simulation_1d\cache_report.json")
```

### 18.4 查看 simulation_1d 计算摘要

```powershell
Get-Content (Join-Path $RunDir "simulation_1d\compute_summary.json")
```

### 18.5 查看 GUI 重算 stdout

```powershell
Get-Content (Join-Path $RunDir "gui\recompute_stdout.log")
```

### 18.6 查看 timeline 中的 error 事件

```powershell
Get-Content (Join-Path $RunDir "timeline.jsonl") | Select-String '"status":"error"|"status": "error"'
```

---

## 19. 结果记录模板

每完成一轮测试，建议按下表记录：

| 项目 | 内容 |
|---|---|
| 测试日期 |  |
| 测试机名称 |  |
| 安装包版本 |  |
| 安装目录 |  |
| diagnostics root |  |
| run_id |  |
| diagnostics 是否开启 |  |
| 是否全新安装 |  |
| 是否完成残留清理 |  |
| 首次启动是否成功 |  |
| 首次 fast 重算是否成功 |  |
| 第二次同参重算是否成功 |  |
| 缓存命中是否符合预期 |  |
| 失败路径验证是否成功 |  |
| 主要耗时阶段 |  |
| 异常说明 |  |
| 最终结论 | 通过 / 不通过 |

---

## 20. 最终判定标准

满足以下条件，才能认为本轮“首次运行诊断测试”通过：

1. 默认关闭时不生成 diagnostics，业务行为正常。
2. 开启 diagnostics 后，安装、启动、重算链路能自动生成结构化日志。
3. `host_info.json`、`runtime_env.json`、`summary.json`、各组件 timeline 文件齐全。
4. 首次重算能生成 `cache_report.json` 与 `compute_summary.json`。
5. 第二次同参数重算能体现缓存复用。
6. 局部参数变更时，缓存失效范围符合预期。
7. 失败路径下仍能保留 error 事件和失败摘要。
8. 全流程未修改测试机全局环境。

若以上任一项不满足，则本轮测试结论为**不通过**，需要根据 diagnostics 回溯问题后再重测。

---

## 21. 建议执行顺序（现场简版）

如果现场时间有限，按以下顺序执行：

1. 修改第 4 节变量区
2. 残留检查与项目级清理
3. 默认关闭验证
4. diagnostics 开启后的安装验证
5. diagnostics 开启后的首次启动验证
6. diagnostics 开启后的首次 fast 重算验证
7. 第二次同参数重算验证
8. 单项失败路径验证
9. 汇总 diagnostics 与结论

---

## 22. 备注

- 正式首轮数据应以**专用测试机实测结果**为准，不以开发机替代。
- 如果首轮测试发现首次计算慢，不应先拍脑袋优化；应先依据 diagnostics 拆分慢段，再决定优化顺序。
- 如果日志显示默认结果回退、Mueller 缓存、T-matrix 任务缓存之间的语义不一致，优先修正诊断与缓存语义，再谈优化。
- 迁移到其他设备时，原则上只改第 4 节变量区和安装包文件名，不改流程本身。
