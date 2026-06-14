# 噪声引入与 SNR 说明

本文档说明当前项目引入了哪些噪声，这些噪声如何进入计算，涉及哪些文件和函数，以及当前项目如何计算信噪比。

## 0. 改动总览

噪声引入的全部改动集中在三个文件：

| 文件 | 作用 |
|---|---|
| `app/demo_ui.py` | 增加前端噪声参数输入、收集噪声参数、展示 SNR 曲线和摘要 |
| `temp/lidar_1d/lidar_1d_simulation.py` | 增加噪声模型、噪声计算、SNR 计算、结果写入 |
| `src/cache_keys.py` | 将噪声参数纳入结果身份 hash，避免噪声参数变化后误用旧结果 |

它不改变 Mie、T-matrix 或光学散射主求解，只在雷达方程得到回波功率之后，作为接收端后处理模型计算噪声底和 SNR。

## 1. 噪声模型位置

当前噪声模型集中在：

```text
temp/lidar_1d/lidar_1d_simulation.py
```

主要定义：

| 位置 | 内容 |
|---|---|
| `DEFAULT_NOISE_MODEL` | 噪声默认参数 |
| `NoiseModel` | 噪声参数数据结构 |
| `resolve_noise_model()` | 解析前端输入，生成噪声模型 |
| `noise_model_snapshot()` | 写入结果摘要的噪声模型快照 |
| `compute_noise_metrics()` | 噪声和 SNR 核心计算 |
| `snr_curve_summary()` | SNR 曲线摘要 |

前端输入位置：

```text
app/demo_ui.py::_build_noise_editor()
```

前端收集位置：

```text
app/demo_ui.py::_collect_overrides()
```

缓存身份位置：

```text
src/cache_keys.py::noise_hash_from_overrides()
```

## 2. 当前引入的噪声来源

当前项目采用“直接探测光电子模型”。

引入的噪声项包括：

| 噪声项 | 代码参数 | 物理含义 |
|---|---|---|
| 信号散粒噪声 | `signal_e` | 回波信号本身由光子/光电子计数产生，服从近似泊松涨落，信号越强，散粒噪声方差也越大。 |
| 背景光噪声 | `background_power_W` | 环境背景光或杂散光进入接收端，转成背景光电子，既抬高观测功率，也贡献噪声方差。 |
| 暗电流噪声 | `dark_current_A` | 探测器无光照时仍存在暗电流，按门宽转换成暗电流电子数，进入噪声方差。 |
| 读出噪声 | `read_noise_e` | 电子学读出链路噪声，单位为电子数 RMS，代码中以平方形式进入方差。 |
| 多脉冲平均 | `average_pulses` | 不是噪声源，而是降噪机制；平均脉冲数越大，噪声标准差按 `sqrt(N)` 降低。 |
| 可选随机带噪曲线 | `generate_noisy_curve` | 若开启，按高斯分布生成一条随机扰动后的曲线，随机种子由 `random_seed` 控制。 |

当前没有显式建模的噪声包括：

| 未建模项 | 说明 |
|---|---|
| 大气湍流闪烁 | 没有距离相关湍流强度模型 |
| 散斑噪声 | 没有相干探测或 speckle 模型 |
| APD 过剩噪声 | 没有倍增噪声因子 |
| 热噪声带宽模型 | 当前没有电路带宽和负载电阻热噪声公式 |
| 量化噪声 | 没有 ADC 位宽量化模型 |
| 距离相关背景光 | 背景光当前为常量 |
| 非理想重叠误差 | 当前几何重叠 `O(R)=1` |

## 3. 噪声参数默认值

当前源码默认值：

| 参数 | 默认值 | 含义 |
|---|---:|---|
| `enabled` | `True` | 默认启用噪声模型 |
| `quantum_efficiency` | `0.6` | 量子效率 |
| `background_power_W` | `1.0e-12` | 背景光功率 |
| `dark_current_A` | `1.0e-9` | 暗电流 |
| `read_noise_e` | `10.0` | 读出噪声，单位 e- |
| `average_pulses` | `1000` | 平均脉冲数 |
| `generate_noisy_curve` | `False` | 默认不生成随机带噪曲线 |
| `random_seed` | `202606` | 随机种子 |

注意：仓库中已有的 `default_result/summary.json` 可能来自旧版本预计算，里面噪声底可能为 `0`。本说明以当前源码重新计算逻辑为准。

## 4. 噪声参数进入链路

前端展示：

```text
app/demo_ui.py::_build_noise_editor()
```

该函数提供以下可编辑项：

| 前端名称 | 参数名 |
|---|---|
| 启用噪声模型 | `enabled` |
| 量子效率 | `quantum_efficiency` |
| 背景光功率 | `background_power_W` |
| 暗电流 | `dark_current_A` |
| 读出噪声 | `read_noise_e` |
| 平均脉冲数 | `average_pulses` |
| 生成带噪曲线 | `generate_noisy_curve` |
| 随机种子 | `random_seed` |

前端收集：

```text
app/demo_ui.py::_collect_overrides()
```

噪声参数会被写入：

```json
{
  "instrument": {
    "receiver_noise": {
      "enabled": true,
      "quantum_efficiency": 0.6,
      "background_power_W": 1e-12,
      "dark_current_A": 1e-9,
      "read_noise_e": 10.0,
      "average_pulses": 1000,
      "generate_noisy_curve": false,
      "random_seed": 202606
    }
  }
}
```

参数文件路径：

```text
temp/lidar_1d/param_overrides.json
```

核心脚本读取：

```text
lidar_1d_simulation.py::load_param_overrides()
lidar_1d_simulation.py::resolve_noise_model()
```

## 5. 噪声计算模型

核心函数：

```text
compute_noise_metrics(power_signal_W, wavelength_nm, gate_time_s, noise, rng)
```

输入的 `power_signal_W` 来自雷达方程：

```text
P_signal(R) = C * O(R) * beta * exp(-2 * alpha * R) / R^2
```

随后进入光电子域。

单光子能量：

```text
E_photon = h * c / lambda
```

单位功率在一个门宽内可产生的电子数：

```text
electron_per_watt = quantum_efficiency * gate_time / E_photon
```

信号电子数：

```text
N_signal(R) = P_signal(R) * electron_per_watt
```

背景光电子数：

```text
N_background = background_power_W * electron_per_watt
```

暗电流电子数：

```text
N_dark = dark_current_A * gate_time / electron_charge
```

固定噪声方差：

```text
fixed_variance = N_background + N_dark + read_noise_e^2
```

总方差：

```text
total_variance(R) = N_signal(R) + fixed_variance
```

多脉冲平均后的电子标准差：

```text
sigma_e_avg(R) = sqrt(total_variance(R) / average_pulses)
```

折算成功率噪声标准差：

```text
noise_std_power_W(R) = sigma_e_avg(R) / electron_per_watt
```

无信号时的 RMS 噪声底：

```text
noise_floor_rms_W = sqrt(fixed_variance / average_pulses) / electron_per_watt
```

## 6. 观测功率曲线

代码中输出三类功率：

| 字段 | 含义 |
|---|---|
| `power_signal_raw` | 雷达方程计算得到的纯信号回波功率 |
| `power_observed_expected_raw` | 信号功率 + 背景光功率 |
| `power_observed_raw` | 当前主图使用的观测功率，等于期望观测功率 |
| `power_observed_noisy_raw` | 若开启随机带噪曲线，则为期望观测功率叠加高斯扰动 |

随机带噪曲线：

```text
power_observed_noisy_raw = power_observed_expected_raw + Normal(0, noise_std_power_W)
```

当前图表主曲线使用 `power_observed_raw`，随机带噪曲线主要写入 CSV。

## 7. SNR 计算方式

线性信噪比：

```text
SNR_linear(R) = N_signal(R) / sigma_e_avg(R)
```

dB 信噪比：

```text
SNR_dB(R) = 20 * log10(SNR_linear(R))
```

这里：

| 项 | 含义 |
|---|---|
| 分子 | 信号光电子数，不包含背景光 |
| 分母 | 信号散粒噪声、背景光噪声、暗电流噪声、读出噪声共同形成的标准差 |

如果关闭噪声模型，代码会将 `snr_linear` 和 `snr_db` 设为 `NaN`，表示不计算有效 SNR。

## 8. SNR 摘要

函数：

```text
snr_curve_summary()
```

输出字段：

| 字段 | 含义 |
|---|---|
| `peak_snr_linear` | 最大线性 SNR |
| `peak_snr_db` | 最大 SNR，单位 dB |
| `snr_db_at_100m` | 100 m 处 SNR |
| `snr_db_at_500m` | 500 m 处 SNR |
| `snr_db_at_1000m` | 1000 m 处 SNR |
| `snr_db_at_2000m` | 2000 m 处 SNR |
| `max_range_snr_ge_3` | SNR >= 3 的最大距离 |
| `max_range_snr_ge_10` | SNR >= 10 的最大距离 |
| `min_snr_db` | 全距离最小 SNR |
| `median_snr_db` | 全距离中位 SNR |

## 9. 噪声结果写入位置

每个场景的 CSV 中会写入：

| 字段 | 含义 |
|---|---|
| `noise_std_power_W` | 每个距离点的功率噪声标准差 |
| `noise_floor_rms_W` | RMS 噪声底 |
| `snr_linear` | 线性 SNR |
| `snr_db` | dB SNR |

每个场景在 `summary.json` 中会写入：

```json
{
  "noise": {
    "noise_floor_rms_W": 0.0,
    "background_power_W": 1e-12,
    "gate_time_s": 2e-7
  },
  "snr_summary": {}
}
```

全局 `summary.json` 中还会写入：

```json
{
  "global": {
    "noise_model": {},
    "snr_output": "CSV files include noise_std_power_W, noise_floor_rms_W, snr_linear and snr_db computed in the direct-detection photoelectron domain."
  }
}
```

## 10. 是否方便增删某项噪声

当前结构中，噪声模型相对集中，因此增删某项噪声比较方便。

以剔除暗电流为例，最简单方式是前端把：

```text
dark_current_A = 0
```

这样暗电流项：

```text
N_dark = dark_current_A * gate_time / electron_charge
```

自动为 0，不再影响噪声底和 SNR。

如果要从代码中彻底删除暗电流，需要同步修改：

| 修改位置 | 说明 |
|---|---|
| `lidar_1d_simulation.py::DEFAULT_NOISE_MODEL` | 删除默认字段 |
| `lidar_1d_simulation.py::NoiseModel` | 删除数据结构字段 |
| `lidar_1d_simulation.py::resolve_noise_model()` | 删除解析逻辑 |
| `lidar_1d_simulation.py::noise_model_snapshot()` | 删除结果快照字段 |
| `lidar_1d_simulation.py::compute_noise_metrics()` | 删除 `dark_e` 和方差贡献 |
| `lidar_1d_simulation.py::build_effective_params_snapshot()` | 删除 `params.json` 中的字段 |
| `app/demo_ui.py::_build_noise_editor()` | 删除前端输入项 |
| `app/demo_ui.py::_DEFAULTS` | 删除前端默认值 |
| `src/cache_keys.py::normalize_noise_model()` | 删除缓存 key 中的字段 |

建议：

| 目标 | 推荐方式 |
|---|---|
| 做对比实验 | 前端将该噪声项设为 0 |
| 产品层面不再支持该噪声 | 从代码、前端和缓存 key 中同步删除 |

## 11. 噪声内容在各求解层的作用位置

| 求解层 | 噪声内容发挥的作用 |
|---|---|
| 参数解析层 | `resolve_noise_model()` 将前端噪声输入转换为 `NoiseModel` |
| 雷达方程后处理层 | `compute_noise_metrics()` 在 `power_signal` 基础上加入背景光、暗电流、读出噪声和散粒噪声 |
| SNR 求解层 | 根据光电子数和噪声标准差计算 `snr_linear` 与 `snr_db` |
| 结果摘要层 | `snr_curve_summary()` 生成峰值 SNR、1km SNR、SNR 达标距离 |
| 输出层 | CSV 写入噪声和 SNR 曲线，`summary.json` 写入噪声模型和 SNR 摘要 |
| 缓存层 | 噪声参数进入 `noise_hash`，影响运行结果身份匹配 |
| 前端展示层 | `fig_all_snr()` 和摘要表读取 SNR 结果进行展示 |

## 12. 缓存身份层细节

文件 `src/cache_keys.py` 中与噪声相关的内容：

| 内容 | 作用 |
|---|---|
| `DEFAULT_NOISE_MODEL` | 与核心脚本保持一致的噪声默认值 |
| `normalize_noise_model()` | 标准化噪声参数，保证 hash 稳定 |
| `noise_hash()` | 对噪声参数生成 hash |
| `noise_hash_from_overrides()` | 从前端 overrides 中提取噪声参数并生成 hash |
| `compose_run_identity(..., noise_hash_value)` | 将噪声 hash 纳入完整运行身份 |

缓存层作用链路：

```text
噪声参数变化
-> noise_hash 变化
-> run identity 变化
-> 避免误用旧的 SNR / 噪声结果
```
