# Bench Schema Changelog: v1.1 → v1.2

时间节点：2026-04-23（杨博会议反馈）

## 一句话总览

v1.2 针对 0423 会议杨博 4 条反馈落地：赝势换 GBRV USPP、T2 prompt 不再暗示"已弛豫"并引入 seekpath、M1-M2 × T1-T2 的 convergence_evidence 允许 `null`、为批量生成脚本做准备。

## 按主题详细清单

### 1. T2 bands_dos prompt 重写（杨博意见 1.2 + 1.3）

| 改动 | 影响 |
|---|---|
| 删除 "already relaxed" 字样 | agent 不再假设 initial_structure 已被弛豫；把它当作一般的初始结构处理 |
| 新增 "Use seekpath.get_path to derive the k-path" 指令 | k-path 由 agent 调 Python `seekpath` 库自动产出，不手挑 |
| 新增 "using seekpath's label convention" 限定 | 统一 VBM/CBM label 的命名空间，避免多种命名导致"多正确答案"歧义 |
| `prompts/bands_dos.txt` 完整重写 | slot 字典不变（formula / structure_type / space_group / atoms_per_primitive_cell / 三个 block） |

### 2. 赝势族默认值（杨博意见 2）

| 改动 | 影响 |
|---|---|
| `pseudopotential.family` 默认值从 `PseudoDojo_SR_v0.4.1_PBE_standard` 改为 `GBRV_USPP_PBE_v1.5` | 4 份 example JSON 同步；material.md / agent_visible.md / prompts/README.md 的示例同步 |
| `relativistic` 保持 `scalar`（GBRV USPP 只有 SR 版本）| M5 SOC 题目未来需要另选 full-relativistic 族（后续再议）|
| 赝势文件本身暂未落地 | P1 启动前需下载 GBRV v1.5 UPF 到 `bench/pseudos/GBRV_USPP_PBE_v1.5/`（单独任务）|

### 3. Convergence evidence 允许为 null（杨博意见 3）

| 改动 | 影响 |
|---|---|
| `convergence_evidence` 从"必填"改为"条件必填" | schema_overview 顶层字段表标注改为 "条件必填" + "object / null" |
| 新增"何时必填 / 何时可空"规则表 | MP-sourced GT → null；自跑 GT → 必填；文献值 + 本地校核 → 推荐填 |
| 起步 16 题全部 `null` | 4 份 example JSON 的 `convergence_evidence` 改为 `null`（原有的 TBD_P1 扫描数据 + 杨博 PDF 数据全部移除）|
| `fields/convergence_evidence.md` 加相应章节 | "什么时候必填 / 什么时候可以留空" |

### 4. T2 bands_dos evaluator 引入 seekpath 派生 k-label（杨博意见 1.3 延伸）

| 改动 | 影响 |
|---|---|
| evaluator `extraction_config` 新增 `k_label_source = "seekpath"` 配置 | 明确 evaluator 侧也用 seekpath |
| `fields/evaluator.md` 新增 "T2 bands_dos 的 k-path 约定" 节 | 说明 agent 与 GT 两侧均用 seekpath、evaluator 在 agent 提交的 initial_structure 上独立跑一次 seekpath 得到 label 字典、版本锁定约定 |
| `ground_truth.vbm_k_label` / `cbm_k_label` value 约定 | 必须是 seekpath 对该空间群输出的字符串，如 "GAMMA"（立方 Fd-3m 的 Γ）、"R"（Pm-3m）|
| M1_T2 的 vbm_k_label 由 "Γ" 改为 "GAMMA" | 对齐 seekpath 字符串 |
| evaluator_type 表补 `qe_bands_dos_combined` 行 | v1.1 漏了，v1.2 补全 |

### 5. GT 值取数方式（杨博意见 3 的延伸）

| 改动 | 影响 |
|---|---|
| 4 份 example JSON 的 ground_truth 数值字段由 `"TBD_P1"` 改为 `"TBD_MP"` | 语义变化：数值由 MP 查询填入（之前是本地自跑） |
| metadata 的 `notes` 改写 | 不再提"杨博 PDF 扫描数据"——GT 统一走 MP |
| M2 example 保留 "杨博 PDF 的 NC a=6.0024 Å / USPP=6.0018 Å" 作为与 MP 值的一致性校核参考 | 不作为 GT 本身 |

### 6. T2 initial_structure 去"pre-relaxed"语义（杨博意见 1.2）

| 改动 | 影响 |
|---|---|
| M1_T2 的 `initial_structure.source` 从 `"pre-relaxed from M1_T1_Si_vc_relax GT"` 改为 `"mp-149"` | 与 M1_T1 一致，不再暴露来源于 T1 输出 |
| M1_T2 / M2_T2 的 `initial_structure.note` 改写 | 新版："Starting geometry provided to agent. Agent should treat it as an initial structure (relaxed or not is not stated)." |
| review.md 的 prompt render 段去"已弛豫"注解 | 同步 |
| 结构坐标数值不变 | M1_T2 仍用 a=3.866 Å（原 T1 GT 占位值）；M2_T2 保留 a=6.0024 Å（杨博 PDF 值）作为合理的初始结构；agent 会重新做（或不做）弛豫，与 bench 立场一致 |

### 7. 受影响的文件汇总

| 类别 | 文件 | 状态 |
|---|---|---|
| 顶层说明 | schema_overview.md | 改：版本号 1.1 → 1.2、changelog 段、convergence_evidence 必填标注 |
| 开放讨论 | fields/open_questions.md | 改：顶部"已定稿"列表新增 0423 四条、Q2 k-label 标注为已定稿保留 |
| 字段规范 | fields/material.md | 改：pseudopotential 示例族、说明加 v1.2 GBRV 切换条 |
| 字段规范 | fields/evaluator.md | 改：evaluator_type 表补 qe_bands_dos_combined、extraction_config 新增 k_label_source、新增"T2 k-path 约定"节 |
| 字段规范 | fields/convergence_evidence.md | 改：加"何时必填 / 何时可空"节、说明段更新 |
| 字段规范 | fields/agent_visible.md | 改：prompt 示例里赝势改 GBRV |
| 示例 JSON | examples/example_M[12]_T[12]_*.json | 全改：schema_version 1.2、pseudopotential.family、convergence_evidence=null、GT 数值占位改 TBD_MP、metadata.notes |
| Review 视图 | examples/example_M[12]_T[12]_*.review.md | 全改：赝势渲染、T2 prompt 加 seekpath、T2 评分章节加 seekpath 细节、M1_T2 vbm_k_label "Γ"→"GAMMA" |
| Prompt 模板 | prompts/bands_dos.txt | 重写：去 "already relaxed"、加 seekpath 指令 |
| Prompt 模板 | prompts/README.md | 改：family_description 映射示例、M1_T1 拼接示例的赝势段 |
| 新增 | changelog_v1.1_to_v1.2.md | 本文件 |

## 关键量化

| 指标 | v1.1 → v1.2 |
|---|---|
| JSON schema_version | "1.1" → "1.2" |
| 顶层字段必填性 | convergence_evidence: 必填 → 条件必填 |
| material.pseudopotential.family 默认 | PseudoDojo_SR_v0.4.1_PBE_standard → GBRV_USPP_PBE_v1.5 |
| bands_dos prompt 字数 | 略有增加（新增 seekpath 两句指令）|
| example convergence_evidence | 4 份非空 → 4 份 null |
| example ground_truth 数值占位 | "TBD_P1" → "TBD_MP" |
| evaluator_type 表条目 | 6 → 7（补 qe_bands_dos_combined）|
| open_questions 必答条 | 6 → 6（Q2 k-label 标注为已定稿保留，未减少条数但减少了"待讨论"量）|

## 下一步（未纳入本版 changelog）

- 下载并本地化 GBRV USPP PBE v1.5 UPF 文件到 `bench/pseudos/GBRV_USPP_PBE_v1.5/`（P1 启动前必须完成）
- 根据杨博 0423 提供的 M1 (62 种) + M2 (29 种) 材料清单整理成结构化 YAML
- 实现题目生成脚本：读 YAML + 查 MP → 批量生成 T1 / T2 题目 JSON（convergence_evidence 留空）
