# 字段规范：`convergence_evidence`

> tolerance 反定流程（按材料硬度分档起步 → 扫描读 Δa → tolerance = max(5×Δa, 0.001 Å)）是草案实操方案。请专家看看 ΔE 阈值的硬/中/软分档、5× 系数、物理最小值 0.001 Å 是否合理，以及收敛判据是否够严。

## 用途

记录 GT 数值的收敛性扫描结果，证明 ground_truth 可信。

扫描方法学与收敛判据详见 `doc/learning/convergence-testing.md`。

## 什么时候必填 / 什么时候可以留空

v1.2 起 `convergence_evidence` 允许为 **`null`**——取决于 GT 来源：

| GT 来源 | `convergence_evidence` 填写要求 |
|---|---|
| Materials Project / 其他外部数据库直接查询 | 可 `null`（数据库背后已有大规模收敛计算，不在本题重复） |
| 伟霖/杨博团队本地 QE 自跑产生 | **必填**（两轮扫描数据 + 收敛参数 + 阈值） |
| 文献数值 + 本地校核 | 推荐填（至少补一轮 ecutwfc 扫描） |

**0423 会议定稿**：起步 16 题（M1+M2 × T1+T2）GT 全部由 MP 查询直接填入，`convergence_evidence` 统一 `null`。更高层（M3+）和更难任务（T3/T4）必须自跑 → 必填。

## Agent 可见性

本字段**整体不给 agent**。收敛扫描数据（ΔE / ΔP 列表）是给 reviewer 看的 GT 可信度证据。详见 `agent_visible.md`。

## JSON 片段

```json
"convergence_evidence": {
  "methodology": "先扫 ecutwfc（固定 kmesh=8x8x8），再扫 kmesh（固定收敛 ecutwfc），两轮均达到 ΔE<0.1 meV/atom 平台",
  "ecutwfc_scan": {
    "values_ry": [40, 50, 60, 70, 80],
    "kmesh_fixed": [8, 8, 8],
    "delta_E_meV_per_atom": [2.1, 0.5, 0.1, 0.02, 0.0],
    "delta_P_kbar": [3.2, 0.8, 0.15, 0.05, 0.0]
  },
  "kmesh_scan": {
    "values": [[4, 4, 4], [6, 6, 6], [8, 8, 8], [10, 10, 10]],
    "ecutwfc_fixed_ry": 80,
    "delta_E_meV_per_atom": [3.5, 0.4, 0.04, 0.0],
    "delta_P_kbar": [5.1, 0.6, 0.08, 0.0]
  },
  "converged_params": {
    "ecutwfc_ry": 80,
    "ecutrho_ry": 320,
    "kmesh": [8, 8, 8]
  },
  "convergence_threshold": {
    "delta_E_meV_per_atom": 0.1,
    "delta_P_kbar": 0.1
  },
  "raw_outputs_dir": "gt_data/M1_T1_Si_vc_relax/raw/"
}
```

## 子字段规范

| 子字段 | 必填 | 类型 | 说明 |
|---|---|---|---|
| methodology | 是 | string | 扫描方法描述 |
| ecutwfc_scan | 是 | object | ecutwfc 扫描数据 |
| kmesh_scan | 是 | object | k-mesh 扫描数据 |
| converged_params | 是 | object | GT 扫描得到的收敛参考参数（仅 bench 内部留存；**不给 agent**） |
| convergence_threshold | 是 | object | 收敛判定阈值 |
| raw_outputs_dir | 否 | string | 原始 QE 输出目录（便于溯源） |

### `ecutwfc_scan`

| 子字段 | 必填 | 类型 | 说明 |
|---|---|---|---|
| values_ry | 是 | array[number] | 扫描的 ecutwfc 取值列表（Ry） |
| kmesh_fixed | 是 | array[int, int, int] | 扫描期间固定的 k-mesh |
| delta_E_meV_per_atom | 是 | array[number] | 相对最后一个值的能量差（meV/atom） |
| delta_P_kbar | 否 | array[number] | 相对最后一个值的应力差（kbar），vc_relax 推荐填 |

### `kmesh_scan`

| 子字段 | 必填 | 类型 | 说明 |
|---|---|---|---|
| values | 是 | array[[int, int, int]] | 扫描的 k-mesh 列表 |
| ecutwfc_fixed_ry | 是 | number | 扫描期间固定的 ecutwfc（通常取第一轮收敛值） |
| delta_E_meV_per_atom | 是 | array[number] | 相对最后一个值的能量差（meV/atom） |
| delta_P_kbar | 否 | array[number] | 应力差（kbar），vc_relax 推荐填 |

### `convergence_threshold`

题目判定"收敛"的数值门槛：

| 子字段 | 单位 | 典型值 |
|---|---|---|
| delta_E_meV_per_atom | meV/atom | 1.0（T1 常用）/ 0.1（T4 EOS 需更严） |
| delta_P_kbar | kbar | 0.5（T1 vc_relax 需要应力双收敛） |
| delta_gap_meV | meV | 10（T2 带隙相关）|

### `converged_params`

| 子字段 | 单位 | 说明 |
|---|---|---|
| ecutwfc_ry | Ry | 扫描得到的收敛 ecutwfc |
| ecutrho_ry | Ry | 推导值：NC 为 4×ecutwfc，USPP/PAW 为 8×ecutwfc |
| kmesh | array[int,int,int] | 收敛 k-mesh |

## 可信度规则

核心原则：tolerance 必须明显大于 GT 本身的数值不确定性 $\Delta a$（"GT 自己的误差"）。否则评分噪声盖过 agent 真实差异，bench 失去区分度。经验上 tolerance 取 $\Delta a$ 的 5–10 倍。

实操中**不需要每道题手算物理公式推 $\Delta a$**——扫描数据本身就能告诉你 $\Delta a$ 是多少。

### 实操流程（推荐）

#### Step 1 先验估 ΔE 阈值（按材料硬度分档）

按体模量 $B_0$ 分三档，起步阈值如下。目标是让不同材料跑出的 $\Delta a$ 大致在同一量级：

| 材料硬度 | $B_0$ 范围 | 起步 ΔE 阈值 |
|---|---|---|
| 硬（diamond C、BN、Al₂O₃） | > 150 GPa | 0.1 meV/atom |
| 中（Si、GaAs、Cu、Al） | 50–150 GPa | 0.05 meV/atom |
| 软（NaCl、钙钛矿、vdW 材料） | < 50 GPa | 0.01 meV/atom |

$B_0$ 可以查文献或 Materials Project；精度不重要，起步档位用。

#### Step 2 按该阈值跑两轮扫描

ecutwfc 扫（固定 kmesh）+ kmesh 扫（固定收敛 ecutwfc）。每个扫描都扫到 ΔE 进入平台。

#### Step 3 从扫描数据直接读 $\Delta a$

取扫描最后两个点的晶格常数差值即为 $\Delta a$——不需要物理公式。示例：

```
ecutwfc=60 Ry  → a = 5.4325 Å
ecutwfc=70 Ry  → a = 5.4332 Å   (Δa = 0.0007 Å，相对上一点)
ecutwfc=80 Ry  → a = 5.4333 Å   (Δa = 0.0001 Å，相对上一点)
```

最后两点 $\Delta a \approx 0.0001$ Å——这就是 a 的数值不确定性。kmesh 扫描也同样读。取两轮的**较大值**作为总 $\Delta a$。

#### Step 4 用读出的 $\Delta a$ 反定 tolerance

$$\text{tolerance} = \max\left(5 \times \Delta a, \; \text{物理最小值}\right)$$

物理最小值约束：即使 $\Delta a$ 很小，tolerance 一般不低于 0.001 Å（避免浮点精度、pymatgen 对称分析等二阶数值噪声影响）。

#### Step 5 合理性检查

如果 $\Delta a > \text{tolerance}/5$，说明扫描没扫够；回到 Step 2 增加扫描点（提高 ecutwfc 上限或加密 kmesh），直到 $\Delta a$ 满足约束或接受更宽松的 tolerance。

### 先验物理估算（背景参考）

上面流程不需要物理公式。以下公式用于扫描前心里有数"大约该扫到多大 ecutwfc"，或扫完后做合理性检查（实测 $\Delta a$ 是否与理论预期接近）。

能量 $E(a)$ 在平衡晶格常数 $a_0$ 附近做二阶 Taylor 展开（弹性能公式），对 cubic 各向同性材料：

$$\Delta E / \text{atom} \approx \frac{9\, B_0\, V_0}{2N} \cdot \left(\frac{\Delta a}{a_0}\right)^2$$

其中 $B_0$ 是体模量，$V_0/N$ 是每原子体积，$N$ 是原胞原子数。反解：

$$\Delta a \approx a_0 \sqrt{\frac{2\, \Delta E \cdot N}{9\, B_0\, V_0}}$$

物理直觉：

- 材料越硬（$B_0$ 大），同样 $\Delta E$ 下 $\Delta a$ 越小——硬材料的 GT 更稳
- 平方根关系 → $\Delta E$ 降 100 倍 $\Delta a$ 只降 10 倍（扫描边际收益递减，不用盲目扫到 $\Delta E = 0$）

#### Si 实例对照

代入 Si 参数（$a_0 = 5.43$ Å、$B_0 = 88$ GPa ≈ 0.55 eV/Å³、$V_0/N = 20$ Å³、$N=2$），$9 B_0 V_0 / N \approx 50$ eV：

| ΔE 阈值 | 公式预测 Δa |
|---|---|
| 0.1 meV/atom | ≈ 0.011 Å |
| 0.05 meV/atom | ≈ 0.008 Å |
| 0.01 meV/atom | ≈ 0.0035 Å |
| 0.005 meV/atom | ≈ 0.0024 Å |

实测 $\Delta a$ 若与公式预测相差几倍以上，说明扫描或计算有问题（例如 kmesh 不够导致 $\Delta a$ 被 kmesh 主导而非 ecutwfc），需排查。

### 常见误区

- **tolerance 定得比 $\Delta a$ 还小**：评分噪声盖过能力差异，bench 失效。Step 4 的 $5\times$ 约束避免这个
- **只扫 ecutwfc 不扫 kmesh（或反之）**：$\Delta a$ 由两者共同决定，单边扫描会低估 $\Delta a$
- **用中间值的"假平台"判定收敛**：有时 $\Delta E$ 在中间出现假平台，继续扫到更高 ecutwfc 才看出真平台；两个相邻点 $\Delta a < \text{阈值}$ 才算真收敛

## 说明

- 需要 `convergence_evidence` 的题目，必须跑完两轮扫描（ecutwfc + kmesh）才能生成
- 扫描的最后一个点作为 0 参考点（已收敛的金标准）
- `raw_outputs_dir` 指向 bench 仓库下的 gt_data 目录，存原始 QE 输出便于复核
- **MP-sourced GT 直接填 `convergence_evidence: null`**（见上方表格），不要填成空 object `{}` ——后者在 JSON Schema 下是"必填子字段缺失"错误
