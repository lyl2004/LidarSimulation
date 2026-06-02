# 两套核心求解库差异审定报告

生成日期：2026-06-01  
工作区：`D:\Code\Python\LidarSimulation`

本文档用于系统整理当前项目中两套核心散射求解库的差异审定结论：

- `pytmatrix`：Mishchenko Fortran T-matrix 内核，经 Python 封装调用
- 本地 `TransitionMatrices.jl`：项目实际采用的 `IITM / EBCM` 链路

本文档汇总历史发现与本轮追加验证，重点回答以下问题：

1. 两套库是否在求解同一个几何与物理量
2. 最初“外来脚本结果小一个数量级”的真正来源是什么
3. 当前剩余差异主要发生在哪一层
4. coarse 大粒径尾部为何敏感，对哪些参数敏感，差异是否可接受
5. 当前哪一组结果更可信，后续应如何使用与修补

## 1. 审定范围与对象

本次审定聚焦对象包括：

- 外来独立实现脚本：`temp/PlotFig5_Final.py`
- 本地项目 coarse 非球形求解链：`src/julia/iitm_physics.jl`
- `pytmatrix` 源码与 Fortran 内核：
  - `D:\Code\Python\Tmatrix\pytmatrix_fixed\pytmatrix\tmatrix.py`
  - `D:\Code\Python\Tmatrix\pytmatrix_fixed\pytmatrix\orientation.py`
  - `D:\Code\Python\Tmatrix\pytmatrix_fixed\pytmatrix\scatter.py`
  - `D:\Code\Python\Tmatrix\pytmatrix_fixed\pytmatrix\fortran_tm\ampld.lp.f`
- 本地 `TransitionMatrices.jl` 依赖源码：
  - `scripts/build_installer/dist/julia_depot/packages/TransitionMatrices/uD12Y/src/shapes/spheroid.jl`
  - `.../common/AxisymmetricTransitionMatrix.jl`
  - `.../common/AbstractTransitionMatrix.jl`
  - `.../common/RandomOrientationTransitionMatrix.jl`

本次工作只做源码阅读、临时测试与审定，不修改本地项目求解库实现。

## 2. 最终摘要

先给最终结论：

1. 最初“外来脚本 coarse 结果比本地小一个数量级”的主因，已经确认是几何口径没有统一，尤其是 spheroid 半径口径误把几何半轴当成了 `pytmatrix` 要求的等体积半径。
2. 几何口径统一后，coarse `alpha` 已基本对齐，这说明两套库在总体散射能量尺度上并不存在根本冲突。
3. 当前剩余的主要差异集中在 coarse `beta`，更具体地说，集中在 `F11(180°)` / `phase_m11_back` / 后向散射核。
4. 该差异不是单一错误，而是三类因素叠加：
   - 两套库的随机取向平均实现层级不同
   - 外来脚本当前姿态积分精度过低
   - coarse 大粒径尾部本身就是高敏感区，粒径、轴比、吸收、姿态和截断阶数都会显著影响 `F11(180°)`
5. 当前没有证据表明某一套库整体“算错了”；但也不能把 coarse tail 的 `beta` 当成高置信绝对真值。
6. 项目内部现阶段更可信的参考仍是本地 `TransitionMatrices` 链路，因为其内部自洽性更强，且中等粒径区 `IITM` 与 `EBCM` 能互相验证。
7. 但本地链路在 coarse 大粒径 tail 上也发现了 `nmax` 对 `F11(180°)` 的显著敏感性，因此本地结果也不应被视作完全无需审查的“绝对基准”。

## 3. 源码级几何语义

### 3.1 本地 `TransitionMatrices` 的 spheroid 定义

依赖源码：

- `scripts/build_installer/dist/julia_depot/packages/TransitionMatrices/uD12Y/src/shapes/spheroid.jl`

已确认实现：

- `Spheroid(a, c, m)`
- 体积：
  - `V = 4/3 * pi * a^2 * c`
- 等体积半径：
  - `r_eqv = (a^2 * c)^(1/3)`
- 几何判据：
  - `(x/a)^2 + (y/a)^2 + (z/c)^2 <= 1`

因此对本项目来说，应按实现而不是注释理解：

- `a`：赤道半轴，作用于 `x, y`
- `c`：旋转轴半轴，作用于 `z`

虽然源码注释把 `a` / `c` 写成 `semi-major` / `semi-minor`，但实现允许 `a < c` 与 `a > c`，因此注释命名本身并不稳妥，真实语义应以空间方程为准。

### 3.2 本地项目对 spheroid 的输入口径

本地项目构造方式见：

- `src/julia/iitm_physics.jl`
- `temp/lidar_1d/julia/iitm_physics.jl`

当前项目对 spheroid 的构造是：

```julia
Spheroid(r, r * axis_ratio, m)
```

也就是说项目当前口径为：

- `r = a`
- `axis_ratio = c / a`

对 coarse 设定 `axis_ratio = 1.6` 来说，本地实际粒子是：

- `a = r`
- `c = 1.6r`
- 这是 prolate spheroid
- 对应等体积半径：
  - `r_eqv = r * 1.6^(1/3)`

### 3.3 `pytmatrix` 的 spheroid 定义

已确认源码：

- `D:\Code\Python\Tmatrix\pytmatrix_fixed\pytmatrix\tmatrix.py`
- `D:\Code\Python\Tmatrix\pytmatrix_fixed\pytmatrix\fortran_tm\ampld.lp.f`

`pytmatrix` / Mishchenko Fortran 的正式定义为：

- `radius` 默认是等体积半径
- `axis_ratio = horizontal-to-rotational = a / c`
- `axis_ratio > 1` 对应 oblate
- `axis_ratio < 1` 对应 prolate

Fortran 源码中已明确写出：

- `AXI - equivalent-sphere radius`
- spheroid 的 `EPS` 为 `horizontal / rotational`
- `EPS > 1` 为 oblate
- `EPS < 1` 为 prolate

### 3.4 严格几何映射公式

如果本地项目口径固定为：

- `r_input = a`
- `axis_ratio_local = c / a`

那么映射到 `pytmatrix` 的严格方式应为：

- `pyt_axis_ratio = a / c = 1 / axis_ratio_local`
- `pyt_radius_eqv = r_input * axis_ratio_local^(1/3)`

也就是：

```text
本地:
  a = r
  c = r * axis_ratio_local

pytmatrix:
  radius = r * axis_ratio_local^(1/3)
  axis_ratio = 1 / axis_ratio_local
  radius_type = equal-volume
```

对当前 coarse 参数 `axis_ratio_local = 1.6`：

- `pyt_axis_ratio = 0.625`
- `pyt_radius = r * 1.6^(1/3)`

## 4. 已确认的一致项

### 4.1 几何口径差异是原始数量级偏差主因

外来脚本原始做法直接把 `r` 送入 `pytmatrix.radius`。这会把几何半轴尺度误当成等体积半径，导致 coarse `alpha` 系统偏低。

将 `pytmatrix.radius` 改为：

```text
r_eqv = r * axis_ratio_local^(1/3)
```

后，coarse `alpha` 基本与本地值对齐。  
这说明原始“差一个数量级”的主因已经查明，而且不属于深层物理错误。

### 4.2 backscatter 的 `4π` 口径不是主要矛盾

通过球形粒子对照已确认：

- `4π * sca_intensity(back)`

与：

- `Qback * πr^2`

在球形情况下可数值一致。

因此当前剩余差异并不是简单的：

- `4π` 漏乘/多乘
- `Qback` 与 `sigma_back` 口径混淆
- `beta` 单位写错

### 4.3 本地链路内部自洽

在本地 `summary.json` 与单粒子结果中，已确认：

- `sigma_back_ref / sigma_sca = phase_m11_back`
- `∫ M11 sin(theta) dtheta = 2`

说明本地 `M11`、`sigma_sca`、`sigma_back` 的口径是闭合且自洽的。

### 4.4 本地 `IITM` 与 `EBCM` 在中等粒径区可互证

已完成的粒子级对照表明：

- `r=0.8, 1.1, 1.5 μm` 一带
- 本地 `TransitionMatrices` 的 `IITM` 与 `EBCM`
- 在 `phase_back`、`sigma_sca` 等量上通常较接近

这说明本地结果并非来源于单一求解器的孤立异常。

## 5. 已确认的不一致项

### 5.1 几何口径统一后，`beta` 差异仍显著存在

在外来脚本中，使用严格等体积半径后得到的 coarse 对照曾表现为：

- `alpha`：已基本贴近本地
- `beta`：仍存在约 `16%~30%` 级系统差

这说明当前剩余问题已不再主要属于：

- 半径口径不统一
- `c/a` 与 `a/c` 直接写反

而是已经上升为：

- 两套引擎对同一非球形粒子的后向散射响应差异

### 5.2 差异主要集中在 `F11(180°)`，而不是总截面

单粒子与分布级验证均表明：

- `sigma_ext`
- `sigma_sca`

通常相对稳定。

而：

- `phase_back = sigma_back / sigma_sca`
- `F11(180°)`
- `phase_m11_back`

则在不同半径上会更明显偏离。

这说明当前主要分歧层不在总体散射能量，而在正后向单角点的相干响应。

## 6. 两套库的随机取向平均差异

### 6.1 `pytmatrix` 的随机取向平均路径

源码路径：

- `D:\Code\Python\Tmatrix\pytmatrix_fixed\pytmatrix\orientation.py`
- `D:\Code\Python\Tmatrix\pytmatrix_fixed\pytmatrix\tmatrix.py`
- `D:\Code\Python\Tmatrix\pytmatrix_fixed\pytmatrix\fortran_tm\ampld.lp.f`

已确认的调用链是：

1. 先由 `calctmat(...)` 计算 T-matrix
2. 再由 `calcampl(...)` / `AMPL` 计算某一单姿态的 `S` 与 `Z`
3. `orientation.orient_averaged_fixed` 对不同 `alpha/beta` 的单姿态 `S/Z` 做数值积分平均
4. `scatter.sca_intensity()` 再从平均后的 `Z` 中取后向散射强度

这条链路的特征是：

- 姿态平均发生在单姿态散射矩阵层
- 当前外来脚本使用的是低阶数值姿态平均

### 6.2 本地 `TransitionMatrices` 的随机取向平均路径

源码路径：

- `.../common/RandomOrientationTransitionMatrix.jl`
- `.../common/AxisymmetricTransitionMatrix.jl`
- `.../common/AbstractTransitionMatrix.jl`

已确认：

- 本地很多公开量直接使用均匀随机取向平均的解析/展开公式
- `scattering_cross_section(...)` 与 `extinction_cross_section(...)` 文档明确对应 uniform orientation averaged quantity
- `scattering_matrix(T, λ, θ)` 是由展开系数重建随机取向散射矩阵

这条链路更接近：

- 先在 T-matrix / 展开系数层得到随机取向平均量
- 再构造 `F11(θ)`

### 6.3 这一差异为什么重要

对：

- 球体
- 总截面
- 平滑积分量

两条链路经常会很接近。

但对：

- 非球形
- 正后向
- 单角点 `180°`

这类高敏感量，下面两种操作不必数值等价：

- 先构造单姿态观测量，再数值平均
- 先对散射算子做随机取向平均，再构造观测量

这正是当前 `beta` 剩余差异的深层来源之一。

## 7. 单粒子层面的关键发现

### 7.1 中等粒径区：单姿态结果本身较接近

在严格几何映射下，对 `r≈1.1 μm` 等中等粒径的代表姿态点，已验证：

- `pytmatrix` 与本地 `TransitionMatrices`
- 在单姿态 `S`
- 单姿态 `Z`
- 特别是 `Z11(180°)`

上通常只差几个百分点。

这说明两套库在“基本散射定义”上并没有明显根本冲突。

### 7.2 大粒径 tail：单姿态 `180°` 响应开始明显分叉

当半径进入 coarse 尾部，例如 `r=3.0 μm` 附近时，已发现：

- 某些姿态下，两套求解器的单姿态 `Z11(180°)` 会出现更明显分离
- 这类分离不再是单纯 `1%~3%`
- 说明 tail 区高阶模态对正后向响应的处理已进入高敏感区

这一发现意味着：

- 剩余差异不只是“姿态平均太粗”
- 粒子级后向散射核本身在大粒径 tail 上也开始分叉

## 8. coarse 大粒径尾部的敏感性来源

### 8.1 为什么 tail 会特别敏感

coarse 大粒径 tail 的后向散射本质是：

- 大尺寸参数
- 非球形粒子
- 多个高阶散射模态
- 在 `180°` 正后向做相干叠加

因此它天然具有如下特性：

- `sigma_sca` 这类总量仍然平滑
- `F11(180°)` 会因为相位关系变化而剧烈摆动
- 很小的参数变化就可能导致相长与相消状态明显切换

简言之：

- 总散射能量不敏感
- 正后向单点强度非常敏感

### 8.2 对哪些参数敏感

以本地 `TransitionMatrices / IITM` 为例，针对：

- `r=3.0 μm`
- `axis_ratio=1.6`
- `m = 1.55 + 0.01i`
- 姿态 `alpha=0°, beta=60°`

做代表点测试后，已确认 `Z11(180°)` 对下列参数明显敏感：

- 半径 `r`
- 轴比 `c/a`
- 折射率实部 `n`
- 折射率虚部 `k`
- 姿态角 `beta`
- 姿态角 `alpha`

而对“是不是正好 `180°`”本身，反而没那么夸张。

### 8.3 代表性敏感度量级

在上述 `r=3.0 μm` 基准点上：

- `r -1%`：
  - `Z11(180°)` 约 `-9.8%`
- `r +1%`：
  - `Z11(180°)` 约 `+46.5%`
- `axis_ratio -5%`：
  - `Z11(180°)` 约 `-16.6%`
- `axis_ratio +5%`：
  - `Z11(180°)` 约 `+18.6%`
- `m_real -0.02`：
  - `Z11(180°)` 约 `-4.0%`
- `m_real +0.02`：
  - `Z11(180°)` 约 `+12.9%`
- `m_imag -0.005`：
  - `Z11(180°)` 约 `+43.0%`
- `m_imag +0.005`：
  - `Z11(180°)` 约 `-30.7%`
- `beta = 55°`：
  - `Z11(180°)` 约 `+16.0%`
- `beta = 65°`：
  - `Z11(180°)` 约 `-51.5%`
- `alpha = 45°`：
  - `Z11(180°)` 约 `-17.2%`

与此同时：

- `sigma_sca` 通常只变化几个百分点

这已经充分证明：

- coarse tail 的主敏感量确实是 `F11(180°)` / `Z11(180°)`
- 不是 `sigma_sca` 这类总截面本身不稳

### 8.4 对散射角 `180°` 附近的敏感性

同一代表点上，还额外检查了：

- `180°`
- `179.5°`
- `179°`

结果显示：

- `179.5°` 对比 `180°`，差约 `-0.2%`
- `179°` 对比 `180°`，差约 `-0.9%`

这说明当前最敏感的并不是“角点采样正好取在 180°”，而是粒子参数与姿态本身。

## 9. 数值离散与收敛性问题

### 9.1 外来脚本的姿态平均精度不足

已在 Julia 中用本地同一粒子做对照：

- 若把随机取向从解析平均，改为类似外来脚本的 `3×6` 数值姿态平均
- `F11(180°)` 在不同粒径上可偏到约 `+5% ~ -16%`
- 提升到 `9×18` 后，多数代表点能回到约 `1%` 内

这说明：

- 外来脚本当前 coarse `beta` 的一部分偏差，确实来自取向积分过稀
- 这属于可修补的数值精度问题

### 9.2 本地 `Nr/Ntheta` 分辨率对 tail backscatter 也有影响

在本地 `IITM` 中，对 `r=3.0 μm` 代表点做了 `Nr/Ntheta` 收敛测试：

- `40×60` 或 `48×72`
  - 相比 `56×84`
  - `Z11(180°)` 低约 `12.8%`
- `64×96`、`80×120`
  - 相比 `56×84`
  - `Z11(180°)` 已回到约 `0.4%` 内

这说明：

- 本地若使用过低离散度，也会明显偏
- 但一旦上到中高精度，`Nr/Ntheta` 本身已不是主要残差来源

### 9.3 本地 `nmax` 对 tail `F11(180°)` 很敏感

这是本轮最重要的新发现之一。

对 `r=3.0 μm` 代表点，仅改变 `nmax`：

- `nmax=23`：
  - `Z11(180°)` 约为一组基准值
- `nmax=24/25`：
  - `Z11(180°)` 会再高出约 `13%~15%`

但：

- `sigma_sca` 只变化约 `1%`

这意味着：

- 本地默认 `nmax` 经验式对总截面通常够用
- 但对 `180°` backscatter 这类高敏感量，未必总是充分收敛

这也解释了为什么：

- tail 区不同求解器的 `beta` 可能即使总体趋势一致，单点也仍显著分离

## 10. `quadrature.get_points_and_weights()` 崩溃的定位

在 `D:\Code\Python\Tmatrix\.pixi\envs\default\python.exe` 这套 `pytmatrix` 环境中，已稳定复现：

- `quadrature.get_points_and_weights(...)`
- 或 `Scatterer._init_orient()`

会发生硬崩溃，甚至还没进入真正的 `orient_averaged_fixed` 主循环。

这说明：

- 当前 `pytmatrix` 环境存在额外的二进制/数值库稳定性问题
- 该问题不是本项目物理口径差异的核心来源
- 但它会妨碍直接在该环境下做更高阶姿态积分复现

按当前任务口径，这一问题暂时可记为“已定位但非核心阻塞”。

## 11. 当前可信度审定

### 11.1 哪些结论已经较稳

以下结论当前可信度高：

- 原始数量级偏差主因是几何半径口径没统一
- `alpha` 统一后基本一致
- 剩余主要差异在 `F11(180°)` / coarse `beta`
- 外来脚本当前姿态积分过稀，确实会带来额外偏差
- coarse tail 本身就是高敏感区
- 本地 tail 的 `nmax` 也需要单独审查

### 11.2 当前哪一组结果更可信

现阶段仍建议把本地 `TransitionMatrices` 链路作为项目内主参考，原因是：

- 项目正式使用它
- 内部定义闭合
- `IITM` 与 `EBCM` 在中等粒径区能互相验证
- 本地 `sigma_sca`、`M11`、`sigma_back` 的口径关系已确认自洽

但必须附带保留意见：

- coarse tail 的 `F11(180°)` 仍存在 `nmax` 敏感性
- 因而本地结果也不应被当作无需审查的绝对真值

### 11.3 是否存在“一边明显错误、另一边明显正确”

当前不支持这种判断。

更准确的说法是：

- 两边在总体几何与散射定义上已可统一
- 中等粒径区相互接近
- 剩余差异主要来自高敏感 tail backscatter 的数值实现差异

因此它更像：

- “同一类物理量在高敏感区被两套不同数值链放大了分离”

而不是：

- “一边明显算错”

## 12. 当前差异是否可接受

这要按用途分层判断。

### 12.1 如果是工程展示、趋势分析、方案比选

当前差异通常可以接受，但应明确：

- coarse `beta` 尤其 tail 区仍有约 `10%~30%`，局部更高的敏感性不确定度
- 不应把它误解为完全可忽略的小误差

### 12.2 如果是严格定量对标、报告归档、论文级比较

当前不能直接视为完全可忽略。

理由：

- 外来脚本姿态积分仍偏粗
- tail 的 `F11(180°)` 对 `nmax`、粒径、姿态、吸收都高度敏感
- 本地和外来在 tail 区仍存在粒子级核差异

### 12.3 如果目标是“让两套方案工程上可认为一致”

这是可行的，但要接受“准一致”而不是“逐点严格相同”：

- `alpha` 已经基本做到
- `beta` 仍需靠姿态积分与 tail 稳定化策略进一步压缩

## 13. 修补与后续建议

### 13.1 第一优先级：提高外来脚本姿态积分精度

这是最直接、最值得做的改动。

理由：

- 当前 `3×6` 对 `180°` backscatter 明显偏粗
- 提升到更高阶后，有望显著缩小一部分 coarse `beta` 差异
- 这是外来脚本侧最便宜、最明确的收益项

### 13.2 第二优先级：对本地 tail 做 `nmax` 收敛审查

重点不是看：

- `sigma_sca` 是否收敛

而是直接看：

- `F11(180°)`
- `sigma_back`
- `phase_m11_back`

因为当前已确认：

- 总截面稳，并不代表正后向 backscatter 已稳

### 13.3 第三优先级：若面向工程应用，可考虑后向小锥角平均

若最终目标不是“纯数学上单点 `180°` 真值”，而是工程一致性与探测可比性，则可考虑：

- 用极小后向锥角平均代替严格单点 `180°`

这样通常会：

- 比单点 `F11(180°)` 更平滑
- 更接近真实仪器有限接收立体角
- 缓解 tail 上的高阶相干振荡敏感性

### 13.4 第四优先级：保留“tail 本身不可过度解释”的使用原则

即使做完上述修补，也应保留以下原则：

- coarse tail 的 backscatter 不应被过度解读为高置信绝对真值
- 它更适合用于相对比较、趋势分析和工程估计

## 14. 对后续工作的建议口径

建议后续对外或对内汇报时，用如下口径表述：

1. 两套库的主差异已经从“几何口径错误”收敛为“高敏感 tail backscatter 的数值差异”。
2. 原始数量级错误已经修正，`alpha` 可以认为一致。
3. 剩余 `beta` 差异主要落在 `F11(180°)`，这是物理上与数值上都高度敏感的量。
4. 外来脚本还有姿态积分精度不足的问题，可继续修补。
5. 本地结果当前更适合作为项目主参考，但 tail `nmax` 仍有审查价值。

## 15. 结论表

| 审定问题 | 当前结论 | 说明 |
| --- | --- | --- |
| 原始数量级差异来源 | 已查明 | 主因是 spheroid 半径口径未对齐 |
| `alpha` 是否已对齐 | 基本是 | 统一等体积半径后已接近一致 |
| 剩余主差异在哪里 | `F11(180°)` / coarse `beta` | 不在总散射能量 |
| 是否主要是 `4π` 或单位错误 | 否 | 已基本排除 |
| 是否主要是姿态积分过稀 | 是，属于主因之一 | 尤其影响外来脚本 |
| 是否还存在粒子级求解器差异 | 是 | 尾部大粒径单姿态核已出现明显分叉 |
| 本地结果是否完全无问题 | 否 | tail `nmax` 对 `F11(180°)` 敏感 |
| 当前更可信的项目内参考 | 本地 `TransitionMatrices` | 但应附带 tail 敏感性保留意见 |
| 当前差异是否可忽略 | 视用途而定 | 工程可接受，严格定量不宜直接忽略 |
| 是否存在明确“某一边算错” | 当前无证据 | 更像高敏感区数值链差异 |

## 16. 本次报告依赖的关键文件

- [temp/PlotFig5_Final.py](D:\Code\Python\LidarSimulation\temp\PlotFig5_Final.py)
- [src/julia/iitm_physics.jl](D:\Code\Python\LidarSimulation\src\julia\iitm_physics.jl)
- [geometry_semantics_audit.md](D:\Code\Python\LidarSimulation\geometry_semantics_audit.md)
- `D:\Code\Python\Tmatrix\pytmatrix_fixed\pytmatrix\tmatrix.py`
- `D:\Code\Python\Tmatrix\pytmatrix_fixed\pytmatrix\orientation.py`
- `D:\Code\Python\Tmatrix\pytmatrix_fixed\pytmatrix\scatter.py`
- `D:\Code\Python\Tmatrix\pytmatrix_fixed\pytmatrix\fortran_tm\ampld.lp.f`
- `scripts/build_installer/dist/julia_depot/packages/TransitionMatrices/uD12Y/src/shapes/spheroid.jl`
- `scripts/build_installer/dist/julia_depot/packages/TransitionMatrices/uD12Y/src/common/AxisymmetricTransitionMatrix.jl`
- `scripts/build_installer/dist/julia_depot/packages/TransitionMatrices/uD12Y/src/common/AbstractTransitionMatrix.jl`
- `scripts/build_installer/dist/julia_depot/packages/TransitionMatrices/uD12Y/src/common/RandomOrientationTransitionMatrix.jl`

## 17. 附记

本次报告基于：

- 源码级只读分析
- 系统临时脚本数值验证
- 已存在的项目缓存与中间结果

未修改本地核心求解库实现。  
`quadrature.get_points_and_weights()` 的环境崩溃问题已定位到 `pytmatrix` 当前 Python 环境层，但按当前审定目标暂不作为主结论展开。
