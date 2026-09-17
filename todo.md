# 雨仿真误差分析与纠偏 — 工作留档

> 参考论文：Wang et al., "Simulation and field evaluation of the effects of
> raindrops on coherent Doppler wind lidar spectrum", Opt. Express 34(5), 2026。
> 1550nm 相干多普勒激光雷达，实测 Table 3 双点数据是本工作的校准基准。

## 一、校准基准（论文 Table 3，1550nm）

| 量 | Case1 (R=6.13mm/h) | Case2 (R=4.65mm/h) |
|----|--------------------|--------------------|
| κ_rain（消光） | 3.30e-3 m⁻¹ | 2.29e-3 m⁻¹ |
| β_rain（后向） | 1.21e-5 m⁻¹ | 2.25e-6 m⁻¹ |
| S=κ/β | 273 sr | 1018 sr |

关键事实：雨率仅差 1.3×，β 却差 **5.4×**。论文核心论点——
**雨率不足以代表雨滴谱**（解析分布给不出这种 case 间差异）。

## 二、核心发现（按确证顺序）

1. **σ_ext 完全正确**：与几何光学极限 2πr² 误差 <2%，Q_ext→2（消光佯谬）。
   → α 偏差不在截面侧。
2. **α 偏低 ~60%（两 case 一致）**：根因是数浓度 N(D)。代码用解析
   Marshall-Palmer，论文用 MRR 实测谱，后者 N(D) 高约 2.5×。换 Joss
   各型分布均无效（仍偏低 40~70%）。
3. **球形 Mie 后向散射高估小滴**：D=0.5mm 处 σ_back 比 Fresnel 几何基线
   高 7.4×，D=1mm 高 3.5×，D≥2mm 已收敛到基线(~1)。
4. **Case2 的 β「+3%」是假对齐**：N(D) 偏低 2.5× 与小滴 σ_back 高估
   ~7× 在 β 上恰好抵消（Case2 小滴占 β 的 82%）。不是真准。
   → 改 N(D) 修 α 会打破抵消，β 暴涨，故 α/β 须联合纠偏。
5. **粒径上限非问题**：6mm→10mm，κ 不变、β 仅 +1~3%。MP 在 D>6mm 已
   指数衰减。当前 r_max=3000μm 足够。
6. **折射率**：教科书纯水值 1.318 反而让 β 偏离实测（+6%→+31%），
   已回退到 **1.314**（实测优先于理论值）。

## 三、已落地设计（分区均摊模型，默认开启）

论文方法：小滴(D≤1mm)用谱平滑球形 Mie，大滴(D>1mm)用非球形 Q_bk。

改动文件：
- `temp/lidar_1d/lidar_1d_simulation.py`：核心物理层
- `src/cache_keys.py`：RainSpec 同步加字段（两份定义必须一致，
  否则 `tests/test_cache_keys_regression.py` 挂）

RainSpec 新增字段（两文件同步）：
- `use_partition: bool = True`
- `partition_split_mm: float = 1.0`
- `smooth_frac: float = 0.30`

新增函数：
- `smoothed_mie_backscatter()`：沿直径做相对宽度 smooth_frac×D 高斯平滑
- `large_drop_backscatter()`：大滴非球形 Q_bk（真实表插值，缺表时回落占位解析式）
- `rain_number_density()`：优先实测谱，回落 Marshall-Palmer
- `load_rain_large_qbk_csv()`：模块加载即从 `data/Raindrop_Qback_Realistic.csv` 注入真实曲线
- `RAIN_LARGE_DROP_GAIN = 1.0`：真实曲线接入后均摊退役（占位式兜底参数）

`compute_rain`：β 走分区组合；α 仍走原球形 Mie（截面已验证准确）。

### 真实大滴 Q_bk 已接入（2026-06-14）

- 数据：`temp/lidar_1d/data/Raindrop_Qback_Realistic.csv`（D=1~6mm，5001 点，
  1550nm 水滴，经核准）+ 参考脚本 `Calcul_Qback_raindrop.py`。
- 口径换算：CSV 第 2 行是「每立体角后向散射效率」，注入时
  `σ_back = Q_raw × πr²`（**不 ÷4π**，Q_raw 本就是每立体角口径）。
  口径已核验：D=1.2mm 处 CSV/球形Mie≈1.21，无 4π 量级错位。
- D>6mm 保持 6mm 端值（np.interp 默认右端常数外推；MP 在 D>6mm 已指数衰减）。
- 大滴曲线趋势：随 D 先增、D≈5mm 见顶后回落（论文图2 VCRM 大滴特征，
  与球形 Mie 单调递减相反）。

当前效果（真实 Q_bk + gain=1.0 + 解析 MP 谱）：
- Case1 R=6.13 β=2.71e-6（**0.22× ref，-78%**），S=423
- Case2 R=4.65 β=2.43e-6（**1.08× ref，+8%**），S=396
- α：Case1 1.15e-3、Case2 9.64e-4（均偏低 ~60%，未变，根因 N(D)）。

**关键诊断**：换真实曲线后 Case2 几乎命中（+8%），Case1 严重偏低（-78%）。
两点 S 都收敛到 ~400，而论文 S 是 273/1018（差 3.7×）。**项目 S 不随 case 变、
论文剧烈变** —— 再次锁定根因是解析 MP 谱给不出 case 间的大滴数浓度差异。
旧占位 gain=5.764 是「用一个错误掩盖另一个错误」（占位曲线偏低 × gain 顶高），
已废弃；现在物理诚实：Case1 偏低如实暴露，唯一解是接入 MRR 实测谱。

## 四、真实数据接入口

1. **大滴 Q_bk 数据**：✅ **已接入**（见第三节「真实大滴 Q_bk 已接入」）。
   `data/Raindrop_Qback_Realistic.csv`，模块加载即 `load_rain_large_qbk_csv()` 注入。
   口径 σ_back = Q_raw×πr²（不÷4π）。如换新曲线，替换 CSV 即可，gain 保持 1.0。

2. **实测谱 N(D)**（⏳ 仍待提供 MRR 谱）：
   ```python
   sim.set_rain_observed_psd(fn)  # fn(D_mm, R)->密度[m^-3 mm^-1]
   ```
   - 这是同时修好 α(-60%) 和 Case1 β(-78%) 绝对值的唯一途径。
   - 仅有解析 MP 谱时，两个 case 只能对齐一个（现状：Case2 准、Case1 偏低）。

## 五、遗留问题

- **α 偏低 -60% 未解**：必须靠实测谱接入（接口已留）。截面无 bug。
- **Case1 β 偏低 -78%**（仅 MP 谱时）：见第 5.1 节，已用 Joss 雨型谱大幅改善。
- **复现 Table 3 双点绝对值**：解析谱不可能（论文论点）。需两个 case
  各自的 MRR 实测谱。

## 5.1 解析谱框架下的最优对标（Joss 雨型 + 数浓度增益）

无实测谱时，用「每 case 选 Joss 雨型 + 全局数浓度增益」逼近论文双点：
- Case1 → drizzle 谱（30000, Λ=5.7），Case2 → thunder 谱（1400, Λ=3.0）。
- `density_gain=2.4609`（两点 β geo-mean 标定，对应 MRR 谱较解析谱偏高 ~2.5×）。
- 效果：**β 平均偏差 43%→13%**（Case1 +14%、Case2 −12%）；Case1 的 S=284
  几乎命中论文 273。

落地方式（决策：只加验证 spec，产品三档不动）：
- RainSpec 两文件同步加 `joss_type: str|None=None`、`density_gain: float=1.0`。
- 新增 `paper_validation_rain_specs()`（case1_drizzle / case2_thunder），
  `PAPER_RAIN_DENSITY_GAIN=2.4609`。产品 `default_rain_specs` 不设这两字段，
  走 MP+gain=1，**输出与改动前完全一致**。
- `rain_number_density(D, R, joss_type, density_gain)`：实测谱 > Joss三型 > MP。

**硬边界**：Case2 的 α 仍 −54%、S 只到 538（论文 1018）。论文 Case2 是
「消光强但后向极弱」的反常组合，本模型 α/β 同向随数浓度缩放，S 上限 ~560，
无法触及 1018。要复现需 MRR 实测谱。这是解析谱框架的极限，非可调参问题。

## 六、验证记录

- `tests/test_cache_keys_regression.py`：5 项全过（缓存键两份定义一致，
  新增 joss_type/density_gain 字段后等价性保持）。
- 端到端 compute_rain：产品三档走 MP+gain=1（输出不变）；
  论文双点 spec β 偏差 +14%/−12%。
- `tests/test_numpy_chain_determinism.py`：5 项全过。同进程内雨/雾/功率/
  固定种子噪声两次跑位位一致（精确浮点相等，非容差）。
- **跨进程完整重算一致性**（2026-06-14）：fast 档跑两次到不同目录，
  全部 data/*.csv 逐字节一致（0 差异）；summary.json 排除 elapsed_s 后
  唯一差异是 `/global/command`（记录的输出目录名不同，非物理量）。
  结论：代码不变前提下，长时间完整重算结果确定可复现。
  确定性保证：噪声用 `np.random.default_rng(202606)` 固定种子独立生成；
  T-matrix 跨目录走缓存命中，不触发 Julia 重算。

## 七、环境兼容（脚本内部处理，不依赖环境改动）

- **scipy 1.14 移除 trapz 的兼容已在脚本内部解决**：`src/mie_core.py:17-37`
  在 `import PyMieScatt` 之前给 `scipy.integrate` 补 trapz/simps/cumtrapz 别名。
  雨仿真经 `from mie_core import AutoMieQ` 间接用 PyMieScatt，自动享有此兼容层。
  且 PyMieScatt 自身 `try: from scipy.integrate import trapz / except: from numpy`
  也能 fallback——双重保险。**已在 scipy 1.14.1 + numpy 2.2.6 干净环境验证通过。**
- ⚠️ 历史教训：曾错误地 patch `.pixi/.../PyMieScatt/*.py` 源码污染环境，违反
  「脚本内部处理兼容、不动环境」原则。该 patch 已随环境重建清除，且本就多余。
  **禁止再 patch 环境内任何第三方库源码。**
- 读论文用的 pypdf 等仅为临时分析工具，不是仿真运行依赖。
