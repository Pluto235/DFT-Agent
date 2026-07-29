# 字段规范：`difficulty`

> 分层方案沿袭杨博 0421 框架。三个子字段的语义：`material_tier` 是材料复杂度（硬分类）、`task_tier` 是任务类型（硬分类）、`overall_level` 是题目的整体难度（综合判断，不从前两者自动导出）。请专家看看分层是否合理、是否遗漏了应单列的材料类型。

## 用途

标注题目的难度层级，便于按 tier 批量过滤、分数聚合。

## Agent 可见性

本字段**整体不给 agent**。难度标签给评测者、分数聚合器、bench 报告用；暴露给 agent 反而引入作弊风险（按 tier 调整策略）。详见 `agent_visible.md`。

## JSON 片段

```json
"difficulty": {
  "material_tier": "M1",
  "task_tier": "T1",
  "overall_level": null
}
```

## 子字段规范

| 子字段 | 必填 | 类型 | 取值 | 说明 |
|---|---|---|---|---|
| material_tier | 是 | string | M1 / M2 / M3 / M4 / M5 | 材料复杂度层（5 层分类）|
| task_tier | 是 | string | T1 / T2 / T3 / T4 | 任务类型（4 种）|
| overall_level | 是 | string / null | L0 / L1 / L2 / ... 或 null | 题目整体难度；null 表示待人工评估 |

## 取值对照

material_tier 层级含义：

| 取值 | 核心挑战 | 代表材料 |
|---|---|---|
| M1 | 标准 GGA/LDA 能描述 | Si、GaAs、NaCl；Al、Cu |
| M2 | 低对称 + 大晶胞 | CsPbBr3、CaTiO3；Ba8Sn46 |
| M3 | 磁性 + 自旋 | bcc Fe、NiO；LaMnGe、LaMnSi |
| M4 | 强关联电子 | CuO、未掺杂铜酸盐 |
| M5 | vdW / SOC | 2D 材料、分子晶体；5d 过渡金属、重 p 区 |

task_tier 对照：

| 取值 | 任务 |
|---|---|
| T1 | vc-relax 结构优化 |
| T2 | 能带 + DOS |
| T3 | Born 有效电荷 + 介电张量（DFPT） |
| T4 | 状态方程（E–V 曲线） |

## `overall_level` 说明

`overall_level` 表达题目的整体难度，取值沿用 `L0 / L1 / L2 / ...` 形式。

**关键**：整体难度**不是** `material_tier × task_tier` 的简单映射——同一个 task 在不同材料上难度可能截然不同（例如 T1 vc-relax 在 Si 上很基础，在 NiO 上却涉及 AFM 磁序、DFT+U 等陷阱）。由题目设计者综合判断后人工标注。

当前批 bench 起步阶段（16 题）**统一留空**（填 `null`）；后续 review 时再逐题评估定值。具体取值空间、边界、评级规则待后续讨论（见 `open_questions.md`）。

## 说明

- `material_tier` 与 `task_tier` 是硬性分类，可程序化过滤；`overall_level` 是软性综合判断
- 未来引入 M6+ 或 T5+ 任务时需同步更新本文档
