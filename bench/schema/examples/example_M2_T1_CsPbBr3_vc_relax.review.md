# Review 视图：M2_T1_CsPbBr3_vc_relax

对应 schema JSON：`example_M2_T1_CsPbBr3_vc_relax.json`

本文件对该题目 JSON 做两件"翻译"：

1. 按 `agent_visible.md` 的字段筛选规则 + harness 拼接约定，给出 **agent 实际看到的 prompt 文本**（预期形式）
2. 按 `evaluator` / `ground_truth` / `scoring` 三字段，给出 **评分规则的自然语言描述**

## 一、Agent 实际看到的 prompt

以下是 harness 用 `bench/prompts/vc_relax.txt` 模板 render 后 agent 看到的完整文本。方括号内是 slot 来源注释（agent 不会看到这些注释）。

```text
[rendered from bench/prompts/vc_relax.txt with slots from material.formula/structure/space_group/atoms_per_primitive_cell]
For CsPbBr3 (cubic perovskite, space group Pm-3m, 5 atoms per primitive
cell), perform a variable-cell relaxation (vc-relax) calculation using
the provided pseudopotentials. The initial structure is given below.
Choose appropriate convergence parameters. Converge stress below 0.5
kbar and forces below 1e-3 Ry/a.u. Return the fully relaxed structure
and verify the space group is preserved.

[initial_structure_block, rendered from material.initial_structure;
 lattice matrix derived from a/b/c/α/β/γ via Lennard-Jones convention]
CELL_PARAMETERS angstrom
6.000000 0.000000 0.000000
0.000000 6.000000 0.000000
0.000000 0.000000 6.000000

ATOMIC_POSITIONS crystal
Cs 0.500000 0.500000 0.500000
Pb 0.000000 0.000000 0.000000
Br 0.500000 0.000000 0.000000
Br 0.000000 0.500000 0.000000
Br 0.000000 0.000000 0.500000

[pseudopotential_block, rendered from material.pseudopotential]
Use GBRV USPP PBE v1.5 pseudopotentials (scalar relativistic):
Cs -> Cs.upf
Pb -> Pb.upf
Br -> Br.upf
```

`material.advisory_notes` 为 null，结尾无 Note 段。`task.prompt` 为 null，不触发 override，完全走模板。

Agent 需自行推断计算参数（ecutwfc、ecutrho、kmesh、收敛阈值等），bench 不提供参数建议。

### 哪些字段被 harness 过滤掉（不进 prompt）

参见 `fields/agent_visible.md`。本题被过滤的字段：

- `schema_version`、`id`、`difficulty`：JSON 元信息
- `material.reference_source`：包含 mp-1014168，暴露会泄题
- `material.name`：可读名称，仅供 bench 内部 reviewer 使用（prompt 用 formula + structure 组合，不走 name）
- `material.space_group_number`：评分字段（exact 匹配），不给 agent
- `material.initial_structure.source`、`.note`：内部溯源
- `material.pseudopotential.note`："GBRV USPP PBE 不含 SOC 赝势"——bench 作者的背景说明
- `task.type`：harness 控制信号
- `evaluator`、`ground_truth`、`scoring`、`convergence_evidence`、`metadata`：整体不给

## 二、评分规则（自然语言）

### 1. 数据抓取（evaluator）

Agent 执行完 vc-relax 后产出 `relax.out` 文件。Evaluator（`type = qe_vc_relax_output`）用 pymatgen 的 `SpacegroupAnalyzer`（`symprec = 1e-3`、`angle_tolerance = 5`）从最终收敛结构抓取 9 个字段：

- 晶格长度 a / b / c（angstrom）
- 晶格角度 α / β / γ（degree）
- 对称性标签 space_group_number / point_group / crystal_system

agent 不需要显式输出这些数值，evaluator 从 QE 输出文件直接提取。

### 2. 逐字段比对

| 字段 | 参考值 gt | 容忍度 tol | 规则 | 通过条件 |
|---|---|---|---|---|
| a | 6.0024 Å（杨博 NC PBE 收敛值；USPP/PAW 给 6.0018 Å，差 0.6 mÅ） | 0.005 Å | relative | $\|x - gt\| / \|gt\| \le \text{tol}$ |
| b | 6.0024 Å | 0.005 Å | relative | 同 |
| c | 6.0024 Å | 0.005 Å | relative | 同 |
| alpha | 90.0° | 0.5° | absolute | $\|x - gt\| \le \text{tol}$ |
| beta | 90.0° | 0.5° | absolute | 同 |
| gamma | 90.0° | 0.5° | absolute | 同 |
| space_group_number | 221 | — | exact | 数值相等 |
| point_group | "m-3m" | — | exact | 字符串相等 |
| crystal_system | "cubic" | — | exact | 字符串相等 |

单字段连续得分 $p_i \in [0, 1]$：

$$p_i^{\text{relative}} = \max\!\left(0,\; 1 - \frac{|x - gt|}{\text{tol} \cdot |gt|}\right)$$

$$p_i^{\text{absolute}} = \max\!\left(0,\; 1 - \frac{|x - gt|}{\text{tol}}\right)$$

$$p_i^{\text{exact}} = \begin{cases}1 & x = gt \\ 0 & \text{否则}\end{cases}$$

### 3. 题目分数聚合

聚合方式 `all_fields_weighted`，`partial_credit = true`。题目总分 $S = \sum_i w_i \cdot p_i$，权重（和 = 1.00）：

| 字段 | 权重 $w_i$ |
|---|---|
| a, b, c | 各 0.15（结构类主权重） |
| α, β, γ | 各 0.05 |
| space_group_number | 0.25 |
| point_group | 0.10 |
| crystal_system | 0.05 |

> **说明**：`space_group` 符号（如 "Pm-3m"）与 `space_group_number`（如 221）一一对应，评分只使用 `space_group_number`，`space_group` 符号不参与打分，避免重复加权。

### 4. 通过判据

`pass_criterion = all_fields_passed`：所有字段各自 $p_i > 0$，题目才算"通过"。`partial_credit = true` 意味着未通过时按 $S$ 给部分分。

### 本题的特殊难点（与 M1_T1 相比）

- 5 原子原胞 vs Si 的 2 原子，计算量约 2.5 倍
- 三种元素赝势（Cs, Pb, Br）—— agent 需要同时正确引用 3 个 UPF 文件
- 重元素 Pb 的 5d/6s 电子处理敏感
- 标量相对论赝势；如果改开 SOC（M5 讨论），带隙会降 0.4–0.5 eV（但 T1 vc-relax 对 SOC 相对不敏感）
