# Prompt 生成策略讨论稿

0422 会议上杨博提出一个新议题：当前 bench schema v1.1 的 examples 里 `task.prompt` 是手写的固定字符串——看起来"每道题 prompt 都不一样"，但其实这只是 4 份示例分别手工定稿的结果，**没有明确规定"从材料 + 任务怎么生成 prompt"**。进入 P1 批量生产（16 题、后续 50+ 题）时，这套机制必须先定下来。

本文基于三件事：

1. 原 TritonDFT 怎么做的（事实基础）
2. 新 bench v1.1 的需求是什么
3. 三种候选方案的权衡

## 一、原 TritonDFT 现状：纯模板填字段

调查结论（证据见附录 A）：

原 TritonDFT 的 benchmark prompt 是 **Python `.format()` 模板填字段**，无 LLM 介入。

每个 task 一个独立模板文件（`benchmark/questions/vc_relax.json`、`scf_energy.json` 等），模板里有 `{material_context}` / `{parameter_context}` / `{pseudopotential_text}` 三个固定 slot。BenchmarkDataset 在 collect 时从 material JSON 把字段拍平成 "formula=Si structure=diamond..." 风格的文本，填入 slot。过程完全确定（`sorted()` 遍历、无 shuffle、无采样），同一输入永远得同一 prompt。

LLM 只出现在 agent 收到 prompt 之后的四个阶段：planner 分解子问题、参数推断、脚本生成、结果解析。**prompt 本身的生成没有 LLM 介入。**

换句话说，原 TritonDFT 用的就是下面方案 A 的一个简化版本。

## 二、新 bench v1.1 对 prompt 的需求

从 v1.1 schema 定的规则倒推 prompt 生成该满足什么：

需求 1：评测可复现。同一题多次跑 agent，agent 看到的 prompt 必须字字相同。这是 bench 的最低要求——prompt 本身带随机性会让失败归因从"agent 不会"滑到"prompt 说法不一致导致 agent 误解"。

需求 2：agent 可见字段受控。`agent_visible.md` 明确列出哪些 material 子字段可以进 prompt（如 formula、space_group、initial_structure 坐标）、哪些不能（如 reference_source、space_group_number、ground_truth）。生成机制必须保证这套白名单/黑名单被严格执行。

需求 3：跨题的 prompt 措辞一致。agent 不应因为"T1 Si 题"和"T1 CsPbBr3 题" prompt 措辞差异而表现出系统性偏差。同 task 下的 prompt 骨架应一致，只在材料字段处有差异。

需求 4：bench 生产成本可控。起步 16 题手写 prompt 可接受，但 P2 扩到 50+ 甚至 100+ 题时，每题独立手写 prompt 不现实——要么模板化生成，要么用生产辅助工具。

需求 5：L2+ 大晶胞题目的结构信息能拼入 prompt。M2_T1_CsPbBr3 的 initial_structure 有 lattice + 5 个原子坐标；未来 M3/M4 材料可能更大（Ba8Sn46 60+ 原子）。生成机制要能把这些几何信息结构化地拼进去。

## 三、三种候选方案

### 方案 A：纯模板化（每 task 一个固定模板 + 字段插槽）

每个 task（T1/T2/T3/T4）对应一份 prompt 模板文件，用类似 `{material_context}` 这样的 slot 从 material JSON 取字段填入。沿袭原 TritonDFT 做法。

示例模板（T1 vc_relax）：

```text
For the {material_name} ({material_structure}, space group {space_group},
{atoms_per_primitive_cell} atoms per primitive cell), perform a
variable-cell relaxation (vc-relax) calculation using the provided
pseudopotentials. The initial structure is given. Choose appropriate
convergence parameters. Converge stress below 0.5 kbar and forces below
1e-3 Ry/a.u. Return the fully relaxed structure and verify the space
group is preserved.

[initial_structure block rendered from material.initial_structure]
{cell_parameters_block}
{atomic_positions_block}

[pseudopotential block rendered from material.pseudopotential]
{pseudopotential_block}
```

生产流程：bench harness 读 material JSON → 按 `agent_visible.md` 白名单取字段 → 填 T1 模板 → 产出最终 prompt 字符串。

评估五个维度：

| 维度 | 方案 A 表现 |
|---|---|
| 评测可复现性 | 最好。纯 `.format()` 确定映射，同输入永远同输出 |
| 可 diff 性 | 最好。模板只 4 份（T1–T4），改一次影响所有题；diff 很清楚 |
| 材料表达灵活度 | 中等。slot 固定，特殊材料（如需要补磁矩初值、U 值）要么加新 slot 要么在 material JSON 侧预渲染文本 |
| bench 生产成本 | 最低。material JSON 填好即可，prompt 零手写 |
| 随机性对评测的影响 | 无随机性 |

核心风险：模板僵化。出现超出模板预设的题型（例如 T3 要求描述 Born 电荷的计算协议、或 M3 需要在 prompt 里提醒 agent"这是 AFM 材料，需要设置初始磁矩"）时，要么扩 slot，要么重开分支模板。长期扩题时模板会碎片化。

### 方案 B：LLM 动态生成（给一般化 task 描述 + 材料信息，让 LLM 写 prompt）

每个 task 写一份"生成指令"（meta-prompt），bench 生产时用该 meta-prompt + material JSON 喂给一个"prompt 生成器 LLM"，让它为这道题写出最终 prompt。

示例 meta-prompt（T1）：

```text
You are creating a benchmark question. The agent will receive your output
as its task description. Task type: variable-cell relaxation (vc-relax).
Convergence criteria: stress < 0.5 kbar, forces < 1e-3 Ry/a.u. Material
to study (do NOT include reference_source or ground_truth fields in the
output):
{material_json_filtered}

Write a natural, clear prompt asking the agent to perform the calculation.
Keep the prompt concise and include the initial structure in QE-compatible
format.
```

生产流程：每道题调用一次 LLM，输出即 prompt 字符串；为保证可复现，把结果缓存在 question JSON 里（不运行时再调 LLM）。

评估五个维度：

| 维度 | 方案 B 表现 |
|---|---|
| 评测可复现性 | 合格但靠缓存。生成一次后写入 question JSON；之后 agent 永远读缓存 |
| 可 diff 性 | 差。meta-prompt 改动后，所有题需要重生成；新 prompt 与旧 prompt 的 diff 基本不可读，无法逐字审查改动是否按预期 |
| 材料表达灵活度 | 最好。LLM 能根据材料特性灵活组织描述（如对磁性材料自然地提到 AFM / 磁矩） |
| bench 生产成本 | 中。每题需要一次 LLM 调用 + 一次人工 review |
| 随机性对评测的影响 | 生成时有采样随机性（可用 temperature=0 缓解，但不同模型版本仍可能不一致）；**缓存后 agent 看到的无随机性** |

核心风险：

1. meta-prompt 改动难以追溯影响。比如把"Keep the prompt concise"改成"Keep the prompt detailed"，所有题都要重生成——但生成结果的好坏要逐题人工看，成本极高。
2. LLM 生成的 prompt 可能**悄悄泄漏**不该给 agent 的信息。比如 meta-prompt 没特别强调"不要提 mp-ID"，LLM 可能为了写得自然把材料来源一起带上。agent_visible.md 的白名单很难让 LLM 严格遵守。
3. LLM 可能为不同材料写出**结构差异极大的 prompt**（Si 的写法和 NaCl 的写法可能组织方式完全不同），引入 prompt 措辞本身作为"难度差异源"的噪声，破坏同 task 内的跨材料可比性。

### 方案 C：混合（模板骨架 + LLM 填特定 slot）

主结构用模板（方案 A），但个别需要"自然语言描述"的 slot 让 LLM 填。例子：

```text
For the material described below, perform a variable-cell relaxation...

[Material description: LLM-filled slot]
{material_description}  ← LLM 根据 material JSON 写一段 2–3 句的自然语言描述

[Initial structure: template-filled]
{cell_parameters_block}
{atomic_positions_block}

...
```

LLM 只负责写 `{material_description}` 这一个 slot，其他字段（初始结构、赝势指定、收敛标准）全由模板写死。

评估五个维度：

| 维度 | 方案 C 表现 |
|---|---|
| 评测可复现性 | 靠缓存，同方案 B |
| 可 diff 性 | 中。骨架改动可直接 diff；LLM 填的 slot 改动不可 diff |
| 材料表达灵活度 | 好。LLM 处理自然语言描述部分；关键字段走模板 |
| bench 生产成本 | 中。每题一次 LLM 调用 + 人工 review（但只需 review 一段） |
| 随机性对评测的影响 | 同 B，缓存后无随机性 |

核心风险：

1. 方案 C 本质上是方案 B 的受控版本——收窄了 LLM 的作用范围，降低了"自然度提升"的上限，也减小了"泄漏/不一致"的风险。是一个"不偏不倚"的折中，但不偏不倚意味着两边的优点都只吃到一半。
2. "哪些 slot 让 LLM 填"的边界本身是新的设计决策，需要逐 task 规定。

## 四、核心权衡

三个方案的差异归根结底是三个 trade-off：

权衡 1：**评测的可审计性 vs 自然语言的灵活性**。模板化把 prompt 完全固定在可逐字审计的范围；LLM 生成把灵活性让出来换牺牲审计性。bench 作为评测工具，可审计性是本职要求。

权衡 2：**前置成本 vs 后置成本**。模板化前期要做好模板设计和字段规范（已经在做，就是 fields/*.md），但之后出题零成本。LLM 生成前期快（写个 meta-prompt 就能批量产），但每次 meta-prompt 改动都要重生成+全部 review，长期成本高。

权衡 3：**跨题 prompt 一致性 vs 单题 prompt 流畅度**。模板化保证同 task 下所有题骨架一致，agent 不会因 prompt 措辞差异表现出偏差；LLM 生成为单题最优化，但同 task 不同题的 prompt 可能组织差别大，成为评测噪声。

## 五、推荐

**倾向方案 A（纯模板化），理由如下**：

1. bench 的核心价值是**评测的可复现和可审计**。方案 A 在这两点上都是最优，其他两个方案都是"通过缓存来勉强拿到可复现性"。
2. 新 bench v1.1 已经通过 fields/*.md 把材料、任务、评分各字段都结构化了——模板化是与这套结构化最自然对齐的 prompt 生成方式。fields/agent_visible.md 的白名单直接就是模板的 slot 来源清单。
3. 原 TritonDFT 的实践证明方案 A 够用（9 种 task 都跑通了），我们新 bench 只需要实现得更规范一些。
4. 方案 A 的主要风险"模板僵化"，在起步 16 题（T1+T2 两个 task）阶段影响最小；真到 T3/T4 出现"需要 LLM 描述"的场景，可以再升级到方案 C。方案 C 作为**方案 A 的向后兼容升级路径**存在，比一步到位走方案 B 更稳妥。

方案 B 不推荐，主要因为"LLM 悄悄泄漏不该给的字段"这个风险对 bench 是致命的——我们的评测信度建立在"agent 看到的信息严格符合 agent_visible.md"这一假设上，破了这一层整个 bench 就没意义。

## 六、待拍板的决策点

如果选方案 A，下面几点需要确认后再动手写 T1/T2 prompt 模板（即原计划的 P0-3）：

1. 模板文件放哪。候选：`bench/prompts/T1_vc_relax.txt`（每 task 一个独立文件）或直接写在 `fields/task.md` 里（规范与实现合一）。倾向前者，便于程序加载。
2. 模板的 slot 粒度。是粗粒度（`{material_context}` 一个 slot 包所有材料字段）还是细粒度（`{formula}`、`{space_group}` 分别是独立 slot）？倾向细粒度，便于测试字段过滤逻辑。
3. initial_structure 的渲染格式。QE 原生的 `CELL_PARAMETERS` + `ATOMIC_POSITIONS` 块（像现在 examples 里那样），还是另一种结构化格式？倾向沿用 QE 原生格式，agent 可以直接复用。
4. 同 task 不同材料是否要针对特例加说明。比如 M3 NiO 是否要在 prompt 里自动加一句"This is an antiferromagnetic material, remember to set initial magnetic moments"？模板化下这意味着要有 conditional slot，增加模板复杂度。起步建议**不加**，用朴素模板，后续如果 agent 在 M3 上普遍卡在"没设磁矩"这一步，再加条件提示。

如果杨博倾向方案 B 或 C，上述决策点的设计会不同，此讨论稿可作为进一步讨论的起点。

## 七、决策落定（伟霖侧，2026-04-22）

伟霖 review 本稿后定：

1. 总方案：**方案 A（纯模板化）**
2. 模板文件位置：**`bench/prompts/<task>.txt`**（如 `vc_relax.txt`、`bands_dos.txt`），每 task 一个独立文件，文件名直接用任务名（与 `fields/task.md` 的 `task.type` 枚举对齐）。不带 T 编号前缀，因为 T 和 task 是 1:1 对应，加前缀冗余；且未来编号若调整（如 T3/T4 换序）不影响文件名
3. slot 粒度：**细粒度**。理由是"模板即 agent_visible 白名单"——读模板即可审计 agent 看到哪些字段；fields/agent_visible.md 的规范由模板本身承载
4. initial_structure 渲染格式：**QE 原生 `CELL_PARAMETERS` + `ATOMIC_POSITIONS` 块**
5. 特例材料条件说明（AFM 磁矩、DFT+U、SOC 等）：在 schema 里**保留字段 `material.advisory_notes`（string / null），默认 `null`（不启用）**。起步 16 题全部填 null，朴素 prompt 不加任何特例提醒。字段放在 `material` 下而非 `task` 下——这类提醒本质是材料物理属性，不随任务变化，随材料走更干净（一份材料说明对应该材料的所有任务）。M3+ 扩展时再与杨博讨论每题是否启用及具体措辞；届时的"启用策略"（甲/乙/丙方案）另做决定，不在本轮确定

下一步：启动原计划的 P0-3（vc_relax.txt、bands_dos.txt 两份 prompt 模板起草），M1/M2 题的 16 题按无特例说明走。特例启用规则与 M3+ 扩展时再讨论。

## 附录 A：原 TritonDFT prompt 生成证据

调查结论详见 [TritonDFT 公开仓库](https://github.com/Leo9660/TritonDFT) 源码：

调用栈：

```
benchmark_agent_test.py:190-191 (collect_prompts)
  → benchmark/dataset.py:147-159 (BenchmarkDataset.__init__)
    → dataset.py:266-278 (_load_templates, 加载 questions/*.json)
    → dataset.py:280-325 (_load_materials)
  → benchmark_agent_test.py:151-154 (dataset.collect)
    → dataset.py:331-391 (BenchmarkDataset.collect)
      → dataset.py:364-370 (template.prompt_template.format)  ← 核心
        返回 DataItem(prompt=...)
  → benchmark_agent_test.py:210 (agent.run(item.prompt))
```

核心 `.format()` 调用：

```python
prompt = template.prompt_template.format(
    material_summary=material.summary(),
    material_context=material.query_context(),
    parameter_text=_format_parameter_block(parameters),
    parameter_context=_parameter_context(parameters),
    pseudopotential_text=pseudo_text,
).strip()
```

T1 模板（`benchmark/questions/vc_relax.json:4`）：

```json
"prompt_template": "For {material_context}, perform a variable-cell relaxation (vc-relax) calculation, only use pw.x and set calculation = \"vc-relax\". {parameter_context}. {pseudopotential_text}. Return the fully relaxed structure (atomic parameters)."
```

Si vc_relax 最终 prompt：

```text
For formula=Si structure=diamond atoms_per_primitive_cell=1 space_group=Fd-3m,
perform a variable-cell relaxation (vc-relax) calculation, only use pw.x and
set calculation = "vc-relax". Set use a gamma-centered grid grid, make a
educated guess for etot_conv_thr, forc_conv_thr, conv_thr, ecutwfc, k-point
sampling. Use LDA pseudopotentials. Return the fully relaxed structure
(atomic parameters).
```

关键观察：

1. 每 task 一个独立模板（9 份 questions/*.json）
2. material 字段通过 `query_context()` 拍平成 `key=value key=value ...` 风格文本
3. 完全确定，无随机性（`sorted()` 遍历、`.format()` 调用）
4. agent 可见字段的"过滤"完全由 `query_context()` 的实现决定（取哪些 info 字段写入文本），不是通过 agent_visible 白名单机制
