# Bench Schema Changelog: v1 → v1.1

时间节点：2026-04-22（0422 杨博会议反馈 + 伟霖侧实施决策）

## 一句话总览

v1.1 针对 0422 会议杨博 5 条反馈 + 模板化 prompt 机制的实施，做了 7 个方向的调整：参数策略、命名体系、难度字段、评分口径、特例说明、prompt 生成、材料描述。

## 按主题详细清单

### 1. 参数策略（杨博意见 2）

| 改动 | 影响 |
|---|---|
| 彻底删除 `task.parameters_hint` 字段 | bench 不再向 agent 提供计算参数建议 |
| 彻底删除 `task.parameters_policy` 字段 | agent 需自行推断 ecutwfc、ecutrho、kmesh、收敛阈值等 |
| 同步清理所有相关讨论 | open_questions 原第 1 条"参数策略"整条删除 |

### 2. 材料分级命名（杨博意见 1）

| 改动 | 影响 |
|---|---|
| `material_tier` 取值从 L1–L5 改为 M1–M5 | `L` 前缀让给题目整体难度字段 |
| id 格式 `Li_Tj_...` → `Mi_Tj_...` | 4 份 examples 文件重命名 |
| 目录结构 `questions/L1_T1/` → `questions/M1_T1/` | schema_overview 的 bench 目录树更新 |
| gt_data 路径同步 | 所有 `raw_outputs_dir` 字段改 M 前缀 |

### 3. 难度字段重构（杨博意见 1 的澄清）

| 改动 | 影响 |
|---|---|
| `difficulty.l0_l1_classification` 字段改名为 `difficulty.overall_level` | 语义变化：不再由 task_tier 自动导出 |
| 新语义：题目整体难度，由 M 和 T 共同决定，需人工综合判断 | 取值 L0 / L1 / L2 / ...（不限上限）|
| 起步 16 题 `overall_level` 全部填 `null` | 定级规则延后讨论（open_questions 新第 6 条）|

### 4. 评分口径（杨博意见 5）

| 改动 | 影响 |
|---|---|
| `evaluator.extract_fields` 去掉 `space_group` | 评分不再抓取空间群符号 |
| `ground_truth` 示例去掉 `space_group` entry | 只保留 `space_group_number` |
| `scoring.field_weights` 去掉 `space_group: 0.15` | 合并到 `space_group_number: 0.25`（原 0.10 + 收回的 0.15）|
| `material.space_group` 仍保留给 agent（prompt 用）| 但不参与评分 |
| 理由：两者一一对应，同时评分等于重复加权 | 避免对"空间群识别"这一能力维度加权过高 |

### 5. 特例说明字段（新增）

| 改动 | 影响 |
|---|---|
| 新增 `material.advisory_notes` 字段（string / null）| 为 M3+ 材料预留（AFM 磁矩、DFT+U、SOC 等提示）|
| 起步 16 题全部填 `null` | 默认不启用，朴素 prompt 不带任何特例提醒 |
| 放在 `material` 下而非 `task` 下 | 因为这类提醒本质是材料物理属性，不随任务变化 |
| 非空时 harness 拼接到 prompt 末尾为 `"\n\nNote: <notes>"` | null 时完全不出现在 prompt 里 |

### 6. Prompt 生成机制（杨博意见 3 + 4）

| 改动 | 影响 |
|---|---|
| 事实调查：原 TritonDFT 是纯模板填字段，无 LLM 介入 | 伟霖查代码证实，参见 `prompt_generation_strategy.md` 第一节 |
| 决策：方案 A（纯模板化），不用 LLM 生成 | 理由：bench 的评测可复现 + 可审计 |
| 新增 `bench/prompts/<task>.txt` 模板机制 | 每 task 一份独立模板，文件名即 task.type |
| 当前交付 `prompts/vc_relax.txt`（T1）、`prompts/bands_dos.txt`（T2）| T3/T4 待扩展 |
| slot 粒度：细粒度 | 模板里明确列出每个字段 slot，便于审计 |
| 晶格矩阵采用 Lennard-Jones 标准形式 | 从 a/b/c/α/β/γ 唯一确定，无歧义 |
| initial_structure / pseudopotential 采用 QE 原生语法块 | `CELL_PARAMETERS angstrom` + `ATOMIC_POSITIONS crystal` |
| `task.prompt` 字段语义变更 | null → 用模板 render；非空 → 作为 override 直接使用 |
| 起步 16 题 `task.prompt` 全部填 `null` | 保证同 task 内跨材料的 prompt 骨架一致 |

新增文件：`prompts/README.md`（slot 字典、复合块渲染规则、harness 伪代码、测试断言）。

### 7. 材料描述字段微调（Prompt 模板化派生）

| 改动 | 影响 |
|---|---|
| `material.name` 从 agent 可见降级为"仅 reviewer 内部使用" | 因为 name 自带括号（如 "silicon (diamond)"）会导致 prompt 里嵌套括号 |
| prompt 用 `formula` + `structure` 裸字段组合 | 生成如 "For Si (diamond, space group Fd-3m, ...)" |
| `material.structure` 字段值约定不得带括号 | 如 "perovskite (cubic)" → "cubic perovskite" |
| 4 份 examples JSON 中 M2 的 structure 已改 | "perovskite (cubic)" → "cubic perovskite" |

## 受影响的文件

| 类别 | 文件 | 状态 |
|---|---|---|
| 顶层说明 | schema_overview.md | 改：目录树、导读、changelog 段、对照表、字段总览、相关文件节 |
| 开放讨论 | fields/open_questions.md | 改：顶部"已定稿"列表 + 删除参数策略/命名/特例字段等原开放条 |
| 字段规范 | fields/task.md | 改：删除 hint/policy 字段，prompt 改 override 语义 |
| 字段规范 | fields/material.md | 改：新增 advisory_notes；name 降级；structure 禁括号；space_group_number 语义更新 |
| 字段规范 | fields/agent_visible.md | 改：字段可见性表、拼接示例更新 |
| 字段规范 | fields/scoring.md | 改：权重示例重分配 + 说明 space_group 不参与评分 |
| 字段规范 | fields/evaluator.md | 改：extract_fields 去 space_group |
| 字段规范 | fields/ground_truth.md | 改：示例去 space_group entry + 说明 |
| 字段规范 | fields/difficulty.md | 重写：M1-M5 + overall_level |
| 字段规范 | fields/convergence_evidence.md | 改：删除提 hint 的句子；converged_params 改描述 |
| 示例 JSON | examples/example_M[12]_T[12]_*.json | 全改 + 重命名：task.prompt=null；material.advisory_notes=null；去 hint；权重改；M2 structure 改 |
| Review 视图 | examples/example_M[12]_T[12]_*.review.md | 全改 + 重命名：prompt 段展示 render 结果；被过滤字段加 material.name |
| 新增 | prompts/vc_relax.txt | T1 模板 |
| 新增 | prompts/bands_dos.txt | T2 模板 |
| 新增 | prompts/README.md | slot 字典 + 渲染规则 + harness 伪代码 |
| 新增 | prompt_generation_strategy.md | 方案权衡 + 决策落定 6 点 |

## 关键量化

| 指标 | v1 → v1.1 |
|---|---|
| JSON schema_version | "1.0" → "1.1" |
| 顶层字段数 | 10（不变）|
| material 子字段数 | 9 → 10（+ advisory_notes）|
| task 子字段数 | 4（type、prompt、parameters_hint、parameters_policy）→ 2（type、prompt）|
| ground_truth 评分字段（T1） | 10 → 9（- space_group）|
| scoring 权重项（T1） | 10 → 9（- space_group）|
| examples 文件命名 | L1/L2 前缀 → M1/M2 前缀 |
| Prompts 模板 | 无 → 2 份（vc_relax.txt、bands_dos.txt）+ README |
| 开放讨论点 | 10 条（6 必答 + 4 可选）→ 8 条（6 必答 + 2 可选），4 条已定稿转到顶部 |
