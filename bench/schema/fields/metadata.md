# 字段规范：`metadata`

> 溯源字段仅做记录用，基本不影响评分逻辑。如果专家认为还有"审核必需"的信息没覆盖到（如 review_date、git commit hash、计算拓扑等），请补充。

## 用途

题目的溯源信息。不参与评分，仅用于记录"谁在什么环境下生成的 GT、哪位专家审过"等。

## Agent 可见性

本字段**整体不给 agent**。溯源信息（作者 / 时间 / QE 版本 / 审核人）与 agent 任务无关，也不应出现在 prompt 里。详见 `agent_visible.md`。

## JSON 片段

```json
"metadata": {
  "author": "weilin",
  "generated_at": "2026-04-21",
  "reviewer": null,
  "gt_qe_version": "7.5",
  "gt_machine": "H100 GPU, QuantumE-gpu-wrapper",
  "gt_wall_time_sec": 180,
  "notes": ""
}
```

## 子字段规范

| 子字段 | 必填 | 类型 | 说明 |
|---|---|---|---|
| author | 否 | string | GT 生成者 |
| generated_at | 否 | string | 生成日期（ISO 8601，如 "2026-04-21"） |
| reviewer | 否 | string / null | 审核人签名（DFT 专家 review 后填） |
| gt_qe_version | 否 | string | 用于生成 GT 的 QE 版本（如 "7.5"） |
| gt_machine | 否 | string | 计算机/集群信息 |
| gt_wall_time_sec | 否 | number / null | 产生 GT 的墙钟时间（参考用） |
| notes | 否 | string | 自由文字备注 |
| is_stable | 否 | bool | true=普通用户可见，false=仅 dev role；缺失默认 true（v1.3 兼容） |
| tags | 否 | list[string] | 标签集合（前端 filter 用）；值集开放，本次用 `magnetic` / `correlated` / `placeholder_gt`；缺失默认 `[]` |
| physics_notes | 否 | string | 题目物理背景（磁序 / +U 必要性 / 收敛难点等），给 reviewer + 后续 self-run 参考；agent 看不到 |
| suggested_initial_moments | 否 | dict | M3 用：原子初始磁矩字典（如 `{"Ni1": 2.0, "Ni2": -2.0, "O": 0.0}`），后续 self-run 时 nspin 起点参考 |
| suggested_hubbard_u | 否 | dict | M4 用：Hubbard U 建议值字典（如 `{"Ni": 6.5}`），后续 self-run 时 +U 计算参考 |

## 说明

- metadata 字段整体可选，但强烈建议填写以便后续复现与追责
- `reviewer` 留给杨博/蒋在 QA 时签名，空置时填 `null`
- `gt_qe_version` 很重要——不同 QE 版本可能因默认算法差异给出略不同的数值
- 已扩展字段（2026-05-04，schema v1.3 不破改）：`is_stable` / `tags` / `physics_notes` / `suggested_initial_moments` / `suggested_hubbard_u`——专为 M3/M4 placeholder 上线设计
- 未来可扩展字段：`review_date`、`review_comments`、`bench_version` 等
