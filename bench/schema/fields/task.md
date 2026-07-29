# 字段规范：`task`

> task.type 的枚举与 prompt 写作风格是草案选择。请专家看看 prompt 的措辞是否贴合真实考察意图。

## 用途

定义任务类型。给 agent 的自然语言 prompt 默认由 `bench/prompts/<task>.txt` 模板 + material 字段 render 产出；`prompt` 字段仅作为少数题的"override"入口存在。

## Agent 可见性

本字段**部分给 agent**。`prompt` 非空时作为 override 直接作为 agent prompt；null 时（默认）由模板 render 产出 agent 看到的文本。`type` 是 harness 与评测器的开关，不进 agent prompt。子字段粒度见下表的"给 agent"列。

## JSON 片段

```json
"task": {
  "type": "vc_relax",
  "prompt": null,
  "policy": {
    "network_allowed": false
  }
}
```

## 子字段规范

| 子字段 | 必填 | 类型 | 给 agent | 说明 |
|---|---|---|---|---|
| type | 是 | string | ✗ | 任务类型 ID；harness 据此选 evaluator |
| prompt | 是 | string / null | 条件 | null 时 harness 按 `bench/prompts/<task>.txt` 模板 render 产出 prompt；非空时作为完整 override 直接作为 agent prompt（跳过模板） |
| policy | v1.3+ 必填 | object | 部分 | runtime constraint 容器；当前唯一 key `network_allowed`（默认 false）；详见 `fields/policy.md`。harness 在 prompt 尾追加 "Network Policy" 段（仅 false 时）让 agent 知晓禁网 |

## 取值对照

### `type`

| 取值 | 对应 task_tier | 含义 |
|---|---|---|
| vc_relax | T1 | 变胞结构优化 |
| bands_dos | T2 | 能带 + DOS 计算 |
| dfpt_born_dielectric | T3 | Born 有效电荷 + 介电张量（DFPT） |
| eos | T4 | 状态方程 E–V 曲线与 BM 拟合 |

## 说明

- **起步 16 题 `prompt` 字段全部填 `null`**——所有题目用统一的 `bench/prompts/<task>.txt` 模板 render，保证同 task 内跨材料的 prompt 骨架一致
- 模板 render 的逻辑 + slot 字典见 `bench/prompts/README.md`（草案阶段路径：`doc/reports/bench_schema_v1/prompts/README.md`）
- agent 需自行推断计算参数（ecutwfc、kmesh、收敛阈值等）；bench 不提供参数建议
- 材料相关的特例提醒（AFM 磁矩、DFT+U、SOC 等）放在 `material.advisory_notes`，不在本字段
- `prompt` 字段非空（override）仅留给极罕见的"自定义 prompt"场景（如未来某些 L2 科研级题目）；起步阶段不使用
