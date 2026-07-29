# 字段规范：`ground_truth`

> 5 种评分规则（relative / absolute / exact / curve_l2 / tensor_frobenius）和示例 tolerance 数值都是抛砖引玉。请专家看看是否覆盖物理直觉、是否需要新增规则类型（如混合判据、非对称容忍度等），以及 T2/T3 的复杂字段怎么存（DOS 文件、张量）的选择是否合适。

## 用途

存储每个评分字段的参考数值与比对规则。本字段与 `evaluator.extract_fields` 一一对应——evaluator 抓到的值与此处的 `value` 按 `rule` 比对。

## Agent 可见性

本字段**整体不给 agent**。这是参考答案，暴露等于作弊——agent 可以直接把 `value` 填进 QE 输出骗过 evaluator。详见 `agent_visible.md`。

## JSON 片段

```json
"ground_truth": {
  "a": {"value": 3.866, "unit": "angstrom", "tolerance": 0.005, "rule": "relative"},
  "alpha": {"value": 60.0, "unit": "degree", "tolerance": 0.5, "rule": "absolute"},
  "space_group_number": {"value": 227, "rule": "exact"}
}
```

> **注**：`space_group` 符号（如 "Fd-3m"）与 `space_group_number`（如 227）一一对应；为避免在 scoring 里重复加权，评分只使用 `space_group_number`。`space_group` 符号可在 `material.space_group` 保留作 agent prompt 可见信息，但不出现在 `ground_truth` 中。

## 单字段结构

每个 ground_truth 条目是一个对象：

| 子字段 | 必填 | 类型 | 说明 |
|---|---|---|---|
| value | 是 | number / string / array | 参考值 |
| unit | 条件必填 | string | 单位（数值类字段必填，exact 类可省）|
| tolerance | 条件必填 | number | 容忍度（exact 规则无需） |
| rule | 是 | string | 比对规则，见下表 |
| note | 否 | string | 自由备注（如"P1 真跑后填"） |

## 评分规则类型

| rule | 判定公式 | 适用 |
|---|---|---|
| relative | $$\frac{|x - gt|}{|gt|} \le tolerance$$ | 晶格长度、能量、体模量等有非零基准的数值 |
| absolute | $$|x - gt| \le tolerance$$ | 角度（基准可能为 0 有奇异）、带隙偏差等 |
| exact | $$str(x) = str(gt)$$ | 空间群、晶系等离散标签 |
| curve_l2 | $$\|x(E) - gt(E)\|_2 \le tolerance$$ | DOS、能带等连续曲线 |
| tensor_frobenius | $$\frac{\|T - gt\|_F}{\|gt\|_F} \le tolerance$$ | Born 电荷、介电张量 |

## 典型字段示例（按 task 分）

### T1 vc_relax

```json
{
  "a": {"value": 3.866, "unit": "angstrom", "tolerance": 0.005, "rule": "relative"},
  "alpha": {"value": 60.0, "unit": "degree", "tolerance": 0.5, "rule": "absolute"},
  "space_group_number": {"value": 227, "rule": "exact"}
}
```

### T2 bands + DOS

```json
{
  "band_gap_ev": {"value": 0.62, "unit": "eV", "tolerance": 0.05, "rule": "absolute"},
  "gap_type": {"value": "indirect", "rule": "exact"},
  "dos_curve": {
    "value": {"ref_file": "gt_data/M1_T2_Si/dos_reference.dat"},
    "unit": "states/eV/fu",
    "tolerance": 0.1,
    "rule": "curve_l2"
  }
}
```

### T3 DFPT Born + 介电

```json
{
  "eps_infinity": {
    "value": [[12.1, 0.0, 0.0], [0.0, 12.1, 0.0], [0.0, 0.0, 12.1]],
    "unit": "dimensionless",
    "tolerance": 0.05,
    "rule": "tensor_frobenius"
  },
  "born_z_star_Si": {
    "value": [[0.0, 0.0, 0.0], [0.0, 0.0, 0.0], [0.0, 0.0, 0.0]],
    "unit": "e",
    "tolerance": 0.1,
    "rule": "tensor_frobenius",
    "note": "Si diamond 中 Si 位点对称性使 Z*=0"
  }
}
```

### T4 EOS

```json
{
  "v0": {"value": 20.45, "unit": "angstrom^3", "tolerance": 0.01, "rule": "relative"},
  "b0": {"value": 88.7, "unit": "GPa", "tolerance": 0.05, "rule": "relative"},
  "b0_prime": {"value": 4.2, "unit": "dimensionless", "tolerance": 0.1, "rule": "relative"}
}
```

## 说明

- `unit` 字段在数值类 rule（relative/absolute）下强制要求，禁止默认单位
- `curve_l2` 的 value 指向外部数据文件（避免 JSON 臃肿），文件格式为两列 `(energy, dos)` 或类似
- `tensor_frobenius` 的 value 是嵌套数组表示的张量
- 所有 value 可用字符串 `"TBD_P1"` 临时占位（示例阶段使用），但发布前必须填入真实数值
