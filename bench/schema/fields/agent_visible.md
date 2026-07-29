# Agent 可见字段

> agent 可见字段的划分——尤其是 material.* 的子字段取舍——是 bench 设计的核心决策（反映"bench 测什么、不测什么"的哲学）。请专家看看是否给 agent 暴露了不该暴露的线索，或反过来遗漏了 agent 跑 DFT 必要的上下文。

## 用途

明确划分题目 JSON 中哪些字段会拼入 agent 的 prompt（"题面"），哪些只用于评分（"答案与评分标准"）。

## 给 agent 看的字段（子字段级）

harness 会从以下子字段中提取信息，拼成 agent 的 prompt：

| 字段路径 | 典型用途 |
|---|---|
| task.prompt | 非空时直接作为 agent prompt（override 模板）；null 时由 `bench/prompts/<task>.txt` 模板 render |
| material.advisory_notes | 非空时拼接到模板 render 结果末尾；null 时不拼 |
| material.formula | prompt 开头描述（如 "For Si..."） |
| material.structure | prompt 描述（如 "diamond"、"cubic perovskite"） |
| material.space_group | prompt 描述（如 "space group Fd-3m"） |
| material.atoms_per_primitive_cell | prompt 描述（如 "2 atoms per primitive cell"） |
| material.initial_structure.lattice_type | 标注 primitive / conventional |
| material.initial_structure.lattice_constants_angstrom | 拼入 CELL_PARAMETERS 片段 |
| material.initial_structure.lattice_angles_degree | 同上 |
| material.initial_structure.atomic_positions_crystal | 拼入 ATOMIC_POSITIONS 块 |
| material.pseudopotential.family | 告诉 agent 使用哪个赝势族 |
| material.pseudopotential.relativistic | 标量 SR / 全相对论 FR |
| material.pseudopotential.files | 告诉 agent 具体赝势文件名 |

## 不给 agent 看的字段（含子字段级例外）

| 字段路径 | 原因 |
|---|---|
| ground_truth（整体） | 参考答案，暴露等于作弊 |
| scoring（整体） | 评分规则，agent 会针对规则作弊 |
| evaluator（整体） | 服务端评分逻辑 |
| convergence_evidence（整体） | GT 可信度证据 |
| metadata（整体） | 溯源信息 |
| difficulty（整体） | 难度标签给评测者/分数聚合用 |
| task.type | 任务类型 ID，harness 据此选 evaluator |
| material.name | 可读名称；仅供 bench 内部 reviewer 使用（prompt 用 formula + structure 组合） |
| material.space_group_number | 评分字段（exact 匹配），不给 agent |
| material.reference_source（整体） | 暴露 mp-ID 会让 agent 直接调 MP API 拿参考值 |
| material.initial_structure.source | 同 reference_source |
| material.initial_structure.note | bench 作者内部备注 |
| material.pseudopotential.note | bench 作者内部备注 |

## 拼接示例（概念性）

Agent 看到的 prompt 由 harness 用 `bench/prompts/<task>.txt` 模板对 material 字段 render 产生，大致形如（M1_T1_Si_vc_relax）：

```
For Si (diamond, space group Fd-3m, 2 atoms per primitive cell), perform a variable-cell relaxation (vc-relax) calculation using the provided pseudopotentials. The initial structure is given below. Choose appropriate convergence parameters. ...

CELL_PARAMETERS angstrom
3.866000 0.000000 0.000000
1.933000 3.348054 0.000000
1.933000 1.116018 3.156576

ATOMIC_POSITIONS crystal
Si 0.000000 0.000000 0.000000
Si 0.250000 0.250000 0.250000

Use GBRV USPP PBE v1.5 pseudopotentials (scalar relativistic):
Si -> Si.upf
```

（`material.advisory_notes` 非空时，会在末尾追加一段 "Note: ..."；`task.prompt` 非空时，整段被其 override）

具体 slot 字典、复合块（initial_structure / pseudopotential）渲染规则、harness 伪代码见 `../prompts/README.md`。

## 说明

- 拼接的具体顺序与格式由 `bench/prompts/<task>.txt` 模板决定，不由 schema 强制
- Agent 只有 prompt（含 initial_structure + pseudopotential 渲染块）+ `material.advisory_notes`（非空时）可用；计算参数（ecutwfc、kmesh、收敛阈值等）由 agent 自行推断
