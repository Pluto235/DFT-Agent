# 开放讨论点

> 本文件按问题清单组织，每条标了"当前默认"和选项。请专家按需逐条回复——确认当前默认、改选其他选项，或指出遗漏的问题。

Schema v1.2 草案中尚待进一步讨论的设计决策。每条标注"当前默认"，review 时只需确认或修改。

**0423 会议已定稿（不再讨论，已在 v1.2 落地）**：

- 赝势族：默认改为 `GBRV_USPP_PBE_v1.5`（取代 PseudoDojo NC）
- T2 k-path：prompt 指示 agent 用 `seekpath` 推导；GT 侧也用 seekpath，label 体系对齐；VBM/CBM k-label 作为评分对象保留（rule=exact）
- T2 initial_structure：不再暗示"已弛豫"，agent 应把它当作一般的初始结构
- convergence_evidence 在 GT 来自 MP 查询时可 `null`；起步 16 题全 null

**0422 会议已定稿（不再讨论，已在 v1.1 落地）**：

- 参数策略：彻底删除 `parameters_hint` / `parameters_policy`，agent 自行推断
- 材料分级命名：`L1–L5` → `M1–M5`，`L` 前缀让给题目整体难度
- 难度字段：新增 `difficulty.overall_level`（取值 L0/L1/L2/...），综合人工标注，不由 M × T 自动导出；当前 16 题起步统一留空（`null`）
- space_group 评分：评分只用 `space_group_number`（整数 exact），`space_group` 符号不参与打分
- prompt 生成机制：采用纯模板化（方案 A），每 task 一份 `bench/prompts/<task>.txt` 模板，由 harness 按 slot 字典渲染；`task.prompt` 字段语义变为"可选 override"，起步 16 题全部 null。详见 `prompt_generation_strategy.md` 第七节"决策落定"
- 材料特例提醒：新增 `material.advisory_notes` 字段（string / null），默认 null。放在 `material` 下而非 `task` 下

## 必须反馈的点

### 1. T2 DOS 曲线评分方式

**当前默认**：`curve_l2` 距离，比对 E_F ± 5 eV 窗口。

**请确认 / 改选**：

| 选项 | 计算 | 特点 |
|---|---|---|
| curve_l2（当前） | $$\|DOS_{agent}(E) - DOS_{gt}(E)\|_2$$ | 对全谱平均敏感 |
| curve_kl | KL 散度 | 对峰值位置敏感 |
| fermi_window_integral | 仅比对 E_F 附近 ±2 eV 的积分 | 对金属 / 半导体电子学性质最相关 |

### 2. T2 带隙的 VBM/CBM k 点（0423 定稿，保留）

**当前默认**：`vbm_k_label` / `cbm_k_label` 均作为评分对象（rule=exact）。M1_T2 示例要求 VBM="GAMMA" / CBM="X"；M2_T2 要求 VBM=CBM="R"。Label 由 seekpath 派生，agent 与 GT 两侧对齐。

### 3. M2 大晶胞 initial_structure 颗粒度

**当前默认**：给完整 lattice + 全部 5 原子坐标（M2_T1_CsPbBr3 示例）。

**请确认 / 改选**：

| 选项 | 给 agent 的信息 | 考察重点 |
|---|---|---|
| 完整坐标（当前） | 全部 lattice + 原子坐标 | 仅测 agent 跑 DFT 能力 |
| 仅 formula + space group | agent 需自己查数据库或构结构 | 测 agent 的"结构构建"能力 |
| 中间态：space group + Wyckoff 位点 | 需要 agent 从对称性推坐标 | 测对称性理解 |

### 4. 赝势路径约定

**当前默认**：相对路径 `Si.upf`，`material.pseudopotential.family` 指定赝势族完整 ID（v1.2 默认 `GBRV_USPP_PBE_v1.5`），实际文件存在 `bench/pseudos/<family>/` 下。

**请确认 / 改选**：

| 选项 | 路径规则 | 优点 | 缺点 |
|---|---|---|---|
| 相对路径到 bench 仓库（当前） | `bench/pseudos/<family>/Si.upf` | 自包含，易分发 | 需同步管理赝势库 |
| 绝对路径到共享赝势库 | `/.../pseudos/GBRV_USPP_PBE_v1.5/Si.upf` | 不重复存储 | 依赖机器环境 |
| 环境变量 | `$PSEUDO_DIR/<family>/Si.upf` | 灵活 | 需每台机配置 |

### 5. tolerance 确定流程

**当前默认**：采用"数据驱动"流程（`fields/convergence_evidence.md` 可信度规则节）：

1. 按材料硬度分档选 ΔE 起步阈值：硬（>150 GPa）→ 0.1 meV/atom；中（50–150 GPa）→ 0.05 meV/atom；软（<50 GPa）→ 0.01 meV/atom
2. 跑 ecutwfc + kmesh 两轮扫描
3. 从扫描最后两点直接读 Δa
4. 反定 tolerance = max(5 × Δa, 0.001 Å)

**请确认 / 改动**：

- 硬度分档的 ΔE 阈值（0.1 / 0.05 / 0.01 meV/atom）是否合适？
- 5× 系数是否合理（2–10 区间内）？
- 物理最小值 0.001 Å 是否合适？
- 是否每题自动走完这个流程，还是每题人工审批 Δa / tolerance？
- 是否仍然需要一张"全 bench 统一 tolerance 表"作为对比基线？

### 6. `overall_level` 定级规则

**当前默认**：16 题起步阶段全部留 `null`，待 bench 跑通首批样本后人工定级。

**待讨论**：

- 取值空间上限（L0–L2 够用，还是需要 L3+）？
- 是否存在可半机械化的定级规则（如"需要 DFT+U / SOC / 自旋极化 → L1"）？
- 谁来定级（bench 作者 / 杨蒋专家 review / 自动规则 + 专家微调）？
- 整体 L 是否会影响权重或 scoring.aggregate 方式（越难的题是否放宽 tolerance）？

## 可选讨论

### 7. `scoring.aggregate` 默认值

`max_rel_error` / `all_fields_weighted` / `all_or_nothing` 三选一作为默认。当前倾向 `all_fields_weighted` + `partial_credit=true` 以获得更平滑的分数分布。

### 8. 机读 schema（已决策延后）

**决策**（2026-04-21）：起步阶段**暂不写** `bench_schema_v1.json`（JSON Schema 格式的机读 schema）。`fields/*.md` 的 markdown 规范 + 人工 review 在 16–20 题规模下够用；等 schema 定稿、题目扩展到 50+ 时再补机读 schema 做自动校验。

术语说明（留给未来接手的人）：

- **markdown 规范**：即 `fields/*.md`，给人看的规则描述
- **机读 schema**：用 JSON Schema 业界标准把规则表达成 JSON 文件，给机器看
- **validator**：配合机读 schema 的工具（如 Python `jsonschema` 包），自动校验题目 JSON 是否合规，用于 CI/CD 防呆

## 说明

- 讨论结论请直接在本文件里注释或勾选，或作为注释加到对应 `fields/*.md`
