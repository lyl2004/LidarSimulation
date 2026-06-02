# 几何语义说明与审定结论

本文档用于统一当前项目 coarse spheroid 非球形粒子的几何语义，并汇总 `TransitionMatrices` 与 `pytmatrix` 的对照审定结论。

## 1. 目标与范围

本次审定聚焦于以下问题：

- 外来脚本 `temp/PlotFig5_Final.py` 与本地 1D 链路在 coarse 模态上的结果差异来自哪里。
- `axis_ratio`、半径口径、后向散射口径在两套引擎中的正式定义分别是什么。
- 在源码级统一几何定义后，剩余差异还有多少，是否属于可忽略误差。

本文不改动本地项目求解链路，只做源码阅读、单粒子核对与分布级对照审定。

## 2. 源码级几何语义

### 2.1 本地 `TransitionMatrices`

依赖源码路径：

- `scripts/build_installer/dist/julia_depot/packages/TransitionMatrices/uD12Y/src/shapes/spheroid.jl`

源码给出的实现要点：

- `Spheroid(a, c, m)`
- 体积：`V = 4/3 * pi * a^2 * c`
- 等体积半径：`r_eqv = (a^2 * c)^(1/3)`
- 空间判据：`(x/a)^2 + (y/a)^2 + (z/c)^2 <= 1`

因此，`TransitionMatrices` 在实现层面的真实几何语义应理解为：

- `a`：赤道半轴，作用在 `x,y`
- `c`：旋转轴半轴，作用在 `z`

注意：

- 源码注释把 `a` 写成 `semi-major axis`、`c` 写成 `semi-minor axis`
- 但实现允许 `a < c` 与 `a > c`
- 因而对项目口径来说，更准确的叫法应是：
  - `a = equatorial semiaxis`
  - `c = polar / rotational semiaxis`

### 2.2 本地项目对 spheroid 的输入口径

项目源码路径：

- `src/julia/iitm_physics.jl`
- `temp/lidar_1d/julia/iitm_physics.jl`
- `src/gui.py`

项目当前对 spheroid 的构造是：

```julia
Spheroid(r, r * axis_ratio, m)
```

因此项目当前输入口径为：

- `r = a`
- `axis_ratio = c / a`

例如当 `axis_ratio = 1.6` 时，本地实际粒子几何为：

- `a = r`
- `c = 1.6 r`
- 这是一类 **prolate spheroid**
- 对应等体积半径：
  - `r_eqv = r * 1.6^(1/3)`

### 2.3 `pytmatrix`

依赖源码路径：

- `D:/Code/Python/Tmatrix/pytmatrix_fixed/pytmatrix/tmatrix.py`

源码给出的正式定义：

- `radius_type == RADIUS_EQUAL_VOLUME` 时，`radius` 为等体积半径
- `radius_type == RADIUS_MAXIMUM` 时，`radius` 为最大半径
- `radius_type == RADIUS_EQUAL_AREA` 时，`radius` 为等面积半径
- `axis_ratio` 为 `horizontal-to-rotational axis ratio`

对 spheroid，`pytmatrix` 的几何语义应理解为：

- `axis_ratio = a / c`
- `axis_ratio > 1` 走 oblate 分支
- `axis_ratio < 1` 走 prolate 分支

源码里 `equal_volume_from_maximum()` 的公式也明确证明了这一点。

## 3. 两边的严格映射公式

如果项目内部统一采用：

- `r_input = a`
- `axis_ratio_local = c / a`

那么映射到 `pytmatrix` 的严格等价写法应为：

- `pyt_axis_ratio = a / c = 1 / axis_ratio_local`
- `pyt_radius_eqv = r_input * axis_ratio_local^(1/3)`

也即：

```text
本地:        a = r, c = r * axis_ratio_local
pytmatrix:   radius = r * axis_ratio_local^(1/3)
             axis_ratio = 1 / axis_ratio_local
             radius_type = RADIUS_EQUAL_VOLUME
```

对当前 coarse 参数：

- `axis_ratio_local = 1.6`

严格映射为：

- `pyt_radius = r * 1.6^(1/3)`
- `pyt_axis_ratio = 1 / 1.6 = 0.625`

## 4. 已审定的一致项

### 4.1 半径口径差异是 coarse `alpha` 主差异来源

外来脚本原版直接把 `r` 喂给 `pytmatrix` 时，coarse `alpha` 明显偏低。

把 `pytmatrix.radius` 改为等体积半径：

- `radius = r * axis_ratio_local^(1/3)`

后，coarse `alpha` 能与本地结果基本对齐。

### 4.2 `beta` 的 4π 归一化不是主问题

对球形粒子做过 `PyMieScatt` 与 `pytmatrix` 单粒子对照，确认：

- `4π * sca_intensity(back)` 与 `Qback * πr^2`

在球形情况下可以数值一致。

### 4.3 本地链路内部自洽

在本地 `summary.json` 中，coarse 模态满足：

- `sigma_back_ref / sigma_sca = phase_m11_back`
- `∫ M11 sin(theta) dtheta = 2`

说明本地 `M11`、`sigma_sca`、`sigma_back` 的口径内部自洽。

### 4.4 本地单粒子求解器内部一致

对 `TransitionMatrices` 包内部：

- IITM
- EBCM

做了同一几何单粒子交叉核对，`sigma_ext / sigma_sca / sigma_back / phase_back` 基本一致。

这说明本地 coarse 粒子级结果不是某个单独求解器的偶然异常。

## 5. 已审定的不一致项

### 5.1 几何语义统一后，`beta` 差异仍显著存在

用严格几何映射后，分布级 coarse 结果仍然与本地存在约 `16%~26%` 的系统差异。

这说明：

- 差异不再主要来自半径口径
- 也不再主要来自 `c/a` 与 `a/c` 的简单写反
- 剩余差异已上升为两套引擎对同一非球形粒子的后向散射响应差异

### 5.2 差异主要发生在粒子级后向散射

单粒子对照显示：

- `sigma_ext` 与 `sigma_sca` 通常较接近
- `phase_back = sigma_back / sigma_sca`

在不同粒径上会明显偏离，而且本地结果并不稳定贴近 `pytmatrix` 的某一边。

这表明分歧主要集中在：

- `M11(180°)` 或等价的后向散射核

而不是集中在体积分布积分或最终雷达方程层。

## 6. 关键对照结果

### 6.1 分布级 coarse 对照

本地 coarse 参考值：

- `alpha_coarse = 3.809513707310495e-03`
- `beta_coarse  = 1.0774915944820495e-03`

在外来脚本中，采用低成本诊断模式得到：

| 口径 | pyt axis_ratio | radius 口径 | alpha 相对本地 | beta 相对本地 |
| --- | --- | --- | ---: | ---: |
| current_oblate | `1.6` | `r * 1.6^(1/3)` | `+0.77%` | `+32.72%` |
| semantic_prolate | `0.625` | `r * 1.6^(1/3)` | `-0.99%` | `-26.46%` |

结论：

- `alpha` 在两边都已很接近
- `beta` 在两边一高一低，且都不能自然落到本地值

### 6.2 单粒子对照

示例半径点：

- `r = 0.8, 1.1, 1.5 um`

本地 Julia `TransitionMatrices`：

| r (um) | sigma_ext | sigma_sca | sigma_back | phase_back |
| ---: | ---: | ---: | ---: | ---: |
| 0.8 | `1.149650220680579e-11` | `1.0960844507495657e-11` | `2.2308592271845573e-12` | `0.20352986721588534` |
| 1.1 | `1.7735129213493404e-11` | `1.6358328227249045e-11` | `7.155564323331455e-12` | `0.4374263814692264` |
| 1.5 | `2.125603195955688e-11` | `1.7855255316686318e-11` | `1.2528966298592895e-11` | `0.7016962836081189` |

同几何在 `pytmatrix` 下：

- `current_oblate`：`axis_ratio = 1.6`
- `semantic_prolate`：`axis_ratio = 0.625`

单粒子结果显示：

- 本地结果在多数半径上更像 `semantic_prolate`
- 但在某些半径上又更像 `current_oblate`

因此本地结果不是简单稳定贴近某一种 `pytmatrix` 几何解释。

## 7. 审定结论表

| 项目 | 结论 | 审定意见 |
| --- | --- | --- |
| coarse `alpha` 差异来源 | 主要来自半径口径 | 已基本修复 |
| coarse `beta` 差异来源 | 主要来自粒子级后向散射响应差异 | 不能忽略 |
| 是否主要是精度问题 | 否 | 已基本排除 |
| 是否主要是 `4π` 归一化问题 | 否 | 已基本排除 |
| 是否主要是本地后处理链路错误 | 当前无明显证据 | 暂不支持 |
| 是否应继续审查本地粒子级实现 | 是 | 有必要，但应聚焦粒子级 |
| 当前更可信的项目内结果 | 本地 `TransitionMatrices` 结果 | 因其内部求解器交叉一致、口径自洽 |
| 外来脚本当前默认结果是否可当严格对照 | 否 | 只能作参考，不应视作同模型严格等价 |

## 8. 当前建议

### 8.1 对项目口径的正式建议

项目层面建议以后明确使用以下命名：

- `a_eq`：赤道半轴
- `c_pol`：极轴 / 旋转轴半轴
- `axis_ratio_ca = c_pol / a_eq`
- `r_eqv = (a_eq^2 * c_pol)^(1/3)`

并规定：

- 本地 `TransitionMatrices`：`Spheroid(a_eq, c_pol, m)`
- `pytmatrix`：`radius = r_eqv`, `axis_ratio = a_eq / c_pol`

### 8.2 对当前结果使用的建议

- 若只需图形趋势或工程级近似，可使用当前修补版外来脚本
- 若需与本地链路进行严格定量一致性讨论，不应再把当前外来脚本默认 coarse `beta` 当作“同口径结果”
- 若需继续深入，应优先做粒子级而非整分布级的进一步审计

## 9. 后续仍可深入的方向

仍然存在更细致差异调研的可能，主要包括：

- 审查本地 `TransitionMatrices` 的 `scattering_matrix(T, λ, θ)` 与 `sigma_sca` 是否完全共享同一随机取向定义
- 比较本地 `TransitionMatrices` 与 `pytmatrix` 在 180° 回波的偏振平均方式是否完全等价
- 在单粒子层面对更多半径点绘制：
  - `sigma_ext`
  - `sigma_sca`
  - `sigma_back`
  - `phase_back`
  的差异曲线
- 若能获得 Mishchenko 原始 Fortran 说明或额外基准文献，可进一步判断两套引擎谁更贴近目标物理设定

---

生成时间：基于当前工作区源码、缓存结果与外部诊断脚本运行结果整理。
