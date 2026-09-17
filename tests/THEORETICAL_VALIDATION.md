# 核心算法理论真实性验证

本目录针对 `temp/论文完整验证与撰写准备方案.md` 3.2 节前三项缺口建立自动化证据。
测试只读取生产算法，不修改仿真输出、缓存或依赖。

## 覆盖范围

| 缺口 | 验证内容 | 测试文件 |
|---|---|---|
| 瑞利极限、光学定理、能量守恒、相函数归一化 | Rayleigh 截面公式，`r^6` 与 `lambda^-4` 斜率，`Qext=Qsca+Qabs`，前向振幅光学定理，Mueller 物理边界与 CDF | `test_mie_physical_truth.py` |
| Mie/T-matrix 球形极限、偏振传播极限 | Python Mie 与 Julia IITM 球形截面、`g`、角响应交叉验证，单次 Mueller 解析作用，光学薄层单散射极限，球形退偏 | `test_cross_solver_and_transport_limits.py`、`julia_tmatrix_sphere_probe.jl` |
| 网格、角度、粒径积分、随机光子收敛 | 距离网格解析收敛，角矩收敛，lognormal 粒径积分收敛，40 个独立种子的 `N^-1/2` 统计误差规律 | `test_numerical_convergence.py` |

## 接受准则

- Rayleigh 区相对误差小于 `2e-6`，拟合斜率分别为 `6 +/- 2e-4` 和 `-4 +/- 2e-4`。
- 光学定理和 Mie 系数截面相对/绝对残差小于 `2e-10`。
- 相函数采用 `integral(M11 sin(theta), theta)=2`，并检查 Mueller 元素基本物理界限。
- T-matrix 球形极限截面与 `g` 误差小于 `0.2%`，选定角度 Mueller 元素误差小于 `3%`。
- 光学厚度 `0.01` 的 Monte Carlo 后向率在单散射解析值的 `5 sigma` 内。
- 细化网格后的误差必须下降；粒径积分最终相对变化小于 `0.5%`。
- Monte Carlo 样本标准差比应与 `sqrt(N_high/N_low)` 相符，允许统计比值偏差 `40%`。

## 运行方式

PowerShell：

```powershell
.\.pixi\envs\mie\python.exe -m unittest discover -s tests -p "test_*.py" -v
```

T-matrix 测试会通过 `pixi run -e julia julia` 加载 `src/julia` 项目。首次运行可能触发 Julia 预编译，并需要 Julia launcher/depot 的正常读写权限。

## 2026-09-01 验证结果

修复后的 15 项测试全部通过。

通过的关键定量结果：

- Rayleigh 最大相对误差 `1.11e-16`，半径幂律斜率 `6.000000000000014`。
- 光学定理三个尺寸参数样例的相对残差不超过 `2.22e-16`。
- Mie/IITM 球形极限三个半径样例的截面与 `g` 差异处于 `1e-14` 量级。
- 距离步长由 `60 m` 细化到 `7.5 m` 时，透过率绝对误差由 `2.1744e-4` 降至 `3.3981e-6`。
- 角积分点由 181 增至 1441 时，`g` 误差由 `8.9008e-5` 降至 `1.3898e-6`。
- 粒径积分粗层级变化为 `8.4288e-3`，最终层级变化为 `4.9392e-6`。
- 光子数由 500 增至 4000 时，40 个独立种子的标准差比为 `2.9186`，理论值为 `sqrt(8)=2.8284`。

原失败项已定位并修复：球形粒子的生产 Stokes 传播保持线偏振，但
`mie_scatter_observables()` 曾报告 `depol_back=1.0` 和 `depol_forward=1.0`。根因是
`safe_depol_ratio()` 误用 `(M11-M12)/(M11+M12)`；球形前/后向散射的 `M12=0`，
所以该式必然得到 1。lidar 线性退偏的目标定义是
`delta=beta_perpendicular/beta_parallel=(F11-F22)/(F11+F22)`。球形满足 `F22=F11`，
因此理论值和修复后结果均为 0。

Julia IITM 原实现还在 `extract_scatter_fields()` 中丢弃了散射矩阵第三列 `F22`，使
`compute_scatter_params()` 只能将 `F12` 误作 `F22`。修复后生产链会提取、验证、粒径加权、
归一化并输出 `M22`，再以真实 `F11/F22` 计算 `depol_back` 和 `depol_forward`。

现有三个非球形 T-matrix 缓存给出了直接的误差量级：旧 `F12` 公式均为 `1.0000`，
正确 `F22` 公式分别为 `0.2970`、`0.3129`、`0.3060`。这证明问题不是折射率、粒径、
轴比等输入参数造成，而是 Mueller 元素映射错误。

## 退偏字段使用边界

- `linear_depol_ratio`：接收机平行/垂直通道比，`P_perp/P_parallel=(I-Q)/(I+Q)`，可作为论文 lidar 线性退偏量。
- `depol_back`：单散射后向 lidar 线性退偏，必须由 `F11/F22` 计算。
- `echo_depol`、Monte Carlo `depolarization_ratio`：当前定义为 `1-sqrt(Q^2+U^2+V^2)/I=1-DoP`，表示偏振度损失，不等于 lidar 线性退偏比，不应互换用于论文曲线或表格。
- `proxy_depol_ratio`：由逐层 `depol_back` 广播得到的介质代理场，不是 Monte Carlo 反演的逐体素精确退偏场。
