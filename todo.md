# 噪声参数剥离工作手册（草案）

## 目标

将当前噪声模型收缩为仅保留两类散粒噪声：

- 信号散粒噪声
- 背景光散粒噪声

完成后，模型应只保留对这两类噪声有直接物理意义的输入项，删除不再参与求解的接收机电子学噪声参数。

## 删除原则

1. 只删除不再进入计算链路的参数。
2. 参数删除后，前端输入、参数快照、结果摘要、缓存身份、历史记录语义应同步收缩。
3. 删除目标是减少噪声模型复杂度，而不是改变大气回波主链路。
4. 不做旧历史兼容回填，后续历史结果统一采用简化噪声模型语义。

## 建议删除的参数

| 参数名 | 当前物理角色 | 删除原因 | 删除目的 |
|---|---|---|---|
| `dark_current_A` | 表示探测器无光时的暗电流噪声 | 暗电流不再参与“两类散粒噪声”模型 | 去除电子学暗噪声项，保留更纯粹的光子统计模型 |
| `read_noise_e` | 表示读出链路噪声标准差，单位为电子数 RMS | 读出噪声不再参与“两类散粒噪声”模型 | 去除接收机电子学读出项，降低模型理解门槛 |

## 建议保留的参数

| 参数名 | 保留原因 |
|---|---|
| `background_power_W` | 直接决定背景光散粒噪声 |
| `quantum_efficiency` | 决定功率到光电子的转换效率 |
| `average_pulses` | 决定平均后的统计降噪效果 |
| `enabled` | 如仍需支持噪声开关，可保留；若要极简模型，可一并删除 |
| `generate_noisy_curve` | 如仍需展示随机扰动曲线，可保留；否则可删除 |
| `random_seed` | 仅在保留随机曲线时需要 |

## 公式收缩目标

### 删除前

```text
Var_total(R) = N_signal(R) + N_background + N_dark + read_noise_e^2
sigma_e_avg(R) = sqrt(Var_total(R) / average_pulses)
```

### 删除后

```text
Var_total(R) = N_signal(R) + N_background
sigma_e_avg(R) = sqrt(Var_total(R) / average_pulses)
```

### 保留的核心物理链路

```text
P_signal(R) -> N_signal(R) -> Var_total(R) -> sigma_e_avg(R) -> SNR(R)
```

也就是说，删除参数后，噪声模型只保留：

- 信号光子的泊松涨落
- 背景光子的泊松涨落

## 需要同步收缩的模块

### 前端输入层

- 删除噪声面板中的 `dark_current_A`
- 删除噪声面板中的 `read_noise_e`
- 视产品目标决定是否进一步删除：
  - `enabled`
  - `generate_noisy_curve`
  - `random_seed`

### 参数收集层

- 删除 overrides 中 `dark_current_A`
- 删除 overrides 中 `read_noise_e`
- 检查前端默认值和表单回填逻辑，避免残留空字段

### 后端噪声模型层

- 从默认噪声参数中移除 `dark_current_A`
- 从默认噪声参数中移除 `read_noise_e`
- 从噪声数据结构中移除对应字段
- 从噪声解析函数中移除对应读取逻辑
- 从噪声求解函数中删除：
  - `N_dark`
  - `read_noise_e^2`

### 结果快照层

- 从 `summary.json -> global.noise_model` 中删除：
  - `dark_current_A`
  - `read_noise_e`
- 从 `params.json -> instrument.receiver_noise` 中删除：
  - `dark_current_A`
  - `read_noise_e`

### 缓存身份层

- 从噪声归一化逻辑中删除 `dark_current_A`
- 从噪声归一化逻辑中删除 `read_noise_e`
- 更新 `noise_hash` 语义，使缓存身份只由保留噪声参数决定
- 确认新的 `noise_hash` 与当前历史结果区分明确，不与旧参数语义混用

## 建议补充的删除参数表

实施前应先形成一份明确的删除参数表，至少包含以下列：

| 参数名 | 所在界面 | 是否进入后端 | 是否进入结果快照 | 是否进入缓存身份 | 删除后替代关系 |
|---|---|---|---|---|---|
| `dark_current_A` | 噪声面板 | 是 | 是 | 是 | 无，直接移除 |
| `read_noise_e` | 噪声面板 | 是 | 是 | 是 | 无，直接移除 |

这张表用于避免“前端删了，但结果字段和缓存还保留”的半删除状态。

## 建议验证步骤

1. 前端检查
   - 确认噪声面板中不再显示 `dark_current_A` 和 `read_noise_e`
   - 确认加载历史结果时不会尝试回填这两个字段

2. 参数文件检查
   - 重新计算后，`params.json` 中不再出现 `dark_current_A`
   - 重新计算后，`params.json` 中不再出现 `read_noise_e`

3. 结果文件检查
   - `summary.json -> global.noise_model` 中不再出现这两个字段
   - `snr_linear`、`snr_db` 仍能正常生成

4. 公式行为检查
   - 确认总噪声仅由 `N_signal + N_background` 构成
   - 远距离低信号区的噪声底相较旧模型应明显降低

5. 缓存检查
   - 新结果生成新的 `noise_hash`
   - 不应继续复用带有旧噪声字段语义的结果身份

## 风险提示

- 删除 `dark_current_A` 与 `read_noise_e` 后，夜间弱信号区的 SNR 会变得更乐观。
- 如果仍保留“工程真实性”的口径，则文档中必须明确说明：当前噪声模型已简化为仅保留两类散粒噪声。
- 如果只删除前端输入而不删除后端和缓存字段，会造成模型语义不一致。

## 回退注意事项

- 若后续发现远距离噪声底过低、结果过于理想化，可按完整接收机噪声模型回退。
- 回退时需同步恢复：
  - 前端输入项
  - 后端噪声字段
  - 结果快照字段
  - 噪声缓存身份

## 最终交付物

本项工作完成时，应至少交付以下内容：

1. 删除参数表
2. 已更新的前端噪声输入项
3. 已收缩的噪声公式说明
4. 已同步收缩的 `summary.json` / `params.json` 字段
5. 已更新的噪声缓存身份规则
6. 一份验证记录，说明删除后结果与模型目标一致
