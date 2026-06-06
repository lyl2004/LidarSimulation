# 1D Lidar Source Evaluation Package

本目录为源码评估包，只保留正式 1D 主链路所需内容。

## 运行前提

本包不是独立可执行软件，目标设备若要直接运行源码，至少需要以下条件：

- 已安装 `pixi`
- 已安装 `Julia`
- `Julia` 已加入 `PATH`，或通过环境变量 `JULIA_EXE` 指定

本包附带 `julia_depot/`，用于支持霾场景相关 T-matrix 依赖加载；但它不包含 `Julia` 解释器本体。

## 首次环境初始化

首次使用前，建议在当前目录至少执行以下两条命令，使 `gui` 与 `mie` 环境都完成初始化：

```powershell
pixi run -e gui python -V
pixi run -e mie python -V
```

仅执行 `pixi run -e mie python -V` 不足以启动界面；
仅执行 `pixi run -e gui python -V` 也不足以保证后续重算可用。

## 启动方式

在当前目录执行：

```powershell
.\run_ui.ps1
```

## 目录说明

- `app/`：正式 UI 入口
- `src/`：1D 主链路运行所需公共模块
- `temp/lidar_1d/lidar_1d_simulation.py`：1D 核心计算脚本
- `temp/lidar_1d/make_final_figures.py`：UI 内部调用的固定工作流重算入口
- `temp/lidar_1d/julia/`：T-matrix 相关 Julia 脚本
- `temp/lidar_1d/default_result/`：默认展示结果
- `temp/lidar_1d/cache_store/seeds/`：默认缓存种子
- `temp/lidar_1d/cache_store/index/`：缓存索引
- `julia_depot/`：Julia 依赖缓存

## 说明

- 当前包未包含安装包、打包脚本、日志、历史运行结果和开发过程文档
- 首次启动或首次新参数计算时，程序可能在 `temp/lidar_1d/` 下自动生成运行态文件
- 若仅进行功能评估，建议优先使用默认结果和默认缓存
