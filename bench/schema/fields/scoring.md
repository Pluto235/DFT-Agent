# 字段规范：`scoring`

> 这部分的设计和权重属于抛砖引玉，需要专家看看合不合理。

## 用途

定义题目级的分数聚合方式与通过判据。evaluator 抓取 + ground_truth 比对后，由 scoring 决定"一道题的最终得分"。

## Agent 可见性

本字段**整体不给 agent**。评分规则暴露会让 agent 针对规则作弊——比如只优化高权重字段、放弃低权重字段以省算力。详见 `agent_visible.md`。

## JSON 片段

```json
"scoring": {
  "aggregate": "all_fields_weighted",
  "field_weights": {
    "a": 0.15, "b": 0.15, "c": 0.15,
    "alpha": 0.05, "beta": 0.05, "gamma": 0.05,
    "space_group_number": 0.25,
    "point_group": 0.10,
    "crystal_system": 0.05
  },
  "pass_criterion": "all_fields_passed",
  "partial_credit": true
}
```

## 子字段规范

| 子字段 | 必填 | 类型 | 说明 |
|---|---|---|---|
| aggregate | 是 | string | 分数聚合方式 |
| field_weights | 条件必填 | object | 字段权重（aggregate=all_fields_weighted 时必填） |
| pass_criterion | 是 | string | 题目通过判据 |
| partial_credit | 否 | boolean | 是否允许部分字段通过拿部分分（默认 true） |

## `aggregate` 取值

| 取值 | 含义 | 分数计算 |
|---|---|---|
| max_rel_error | 取最大相对误差，分数为"1 - max_rel_error / tolerance" | 与原 DFTBench 兼容 |
| all_fields_weighted | 各字段按权重加权平均 | $$score = \sum_i w_i \cdot p_i$$，p_i 为字段 i 的通过分数（0/1 或连续值） |
| all_or_nothing | 全部字段通过才得满分，否则 0 | 最严格 |

## `pass_criterion` 取值

| 取值 | 含义 |
|---|---|
| all_fields_passed | 所有 ground_truth 字段均通过容忍度 |
| critical_fields_passed | 仅 `critical` 字段必须通过（字段级 `critical: true` 标注，可选扩展） |
| any_field_passed | 任一字段通过即算题目通过（宽松兜底） |

## `field_weights`

- 所有字段权重之和建议等于 1.0（规范化）
- 字段名需与 `ground_truth` 的 key 严格对应
- 权重分配反映字段重要性：结构类字段（a/b/c/space_group）通常权重较高

## 连续评分 vs 二值评分

`partial_credit=true` 时，字段通过度可为连续值：

数值类字段（relative/absolute）：

$$p_i = \max\left(0,\; 1 - \frac{|x - gt|}{tolerance}\right)$$

exact 类字段：

$$p_i = \begin{cases}1 & \text{若匹配} \\ 0 & \text{否则}\end{cases}$$

`partial_credit=false` 时，所有字段只输出 0/1。

## 示例

### T1 vc_relax（权重偏向结构）

```json
"scoring": {
  "aggregate": "all_fields_weighted",
  "field_weights": {
    "a": 0.15, "b": 0.15, "c": 0.15,
    "alpha": 0.05, "beta": 0.05, "gamma": 0.05,
    "space_group_number": 0.25,
    "point_group": 0.10, "crystal_system": 0.05
  },
  "pass_criterion": "all_fields_passed",
  "partial_credit": true
}
```

> **注**：`space_group` 符号（如 "Fd-3m"）与 `space_group_number`（如 227）一一对应，属于同一物理信息。评分只使用 `space_group_number`（整数 exact 匹配）；`space_group` 符号作为 GT 保留但不参与打分，避免重复加权。

### T2 bands + DOS（权重偏向带隙）

```json
"scoring": {
  "aggregate": "all_fields_weighted",
  "field_weights": {
    "band_gap_ev": 0.5, "gap_type": 0.2, "dos_curve": 0.3
  },
  "pass_criterion": "all_fields_passed",
  "partial_credit": true
}
```

## 说明

- 所有题目的评分口径由 scoring 字段内嵌定义，不依赖全局配置
- 题目级得分可进一步在 bench 级按材料/任务 tier 聚合（不在本字段管辖范围内）
