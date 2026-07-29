# Review 视图：M1_T2_Si_bands_dos

对应 schema JSON：`example_M1_T2_Si_bands_dos.json`

本文件对该题目 JSON 做两件"翻译"：

1. 按 `agent_visible.md` 的字段筛选规则 + harness 拼接约定，给出 **agent 实际看到的 prompt 文本**（预期形式）
2. 按 `evaluator` / `ground_truth` / `scoring` 三字段，给出 **评分规则的自然语言描述**

## 一、Agent 实际看到的 prompt

以下是 harness 用 `bench/prompts/bands_dos.txt` 模板 render 后 agent 看到的完整文本。方括号内是 slot 来源注释（agent 不会看到这些注释）。

```text
[rendered from bench/prompts/bands_dos.txt with slots from material.formula/structure/space_group/atoms_per_primitive_cell]
For Si (diamond, space group Fd-3m, 2 atoms per primitive cell), run a
full bands + DOS workflow on the initial structure provided below:
(1) SCF on an appropriate k-mesh,
(2) NSCF along a high-symmetry k-path for band structure using bands.x,
(3) NSCF on a denser k-mesh for DOS using dos.x.

Use the Python package `seekpath` (`seekpath.get_path`) to derive the
high-symmetry k-path from the initial structure — do not hand-pick the
path. Choose appropriate convergence parameters, k-mesh densities, and
other numerical settings. Report the band gap, gap type (direct/indirect),
VBM and CBM k-point labels (using seekpath's label convention), and
produce the DOS curve in the range E_F ± 5 eV.

[initial_structure_block, rendered from material.initial_structure]
CELL_PARAMETERS angstrom
3.866000 0.000000 0.000000
1.933000 3.348054 0.000000
1.933000 1.116018 3.156576

ATOMIC_POSITIONS crystal
Si 0.000000 0.000000 0.000000
Si 0.250000 0.250000 0.250000

[pseudopotential_block, rendered from material.pseudopotential]
Use GBRV USPP PBE v1.5 pseudopotentials (scalar relativistic):
Si -> Si.upf
```

`material.advisory_notes` 为 null，结尾无 Note 段。`task.prompt` 为 null，不触发 override，完全走模板。

Agent 需自行推断计算参数（ecutwfc、ecutrho、kmesh_scf、kmesh_nscf_dos、nbnd、conv_thr、DOS smearing 等）；k-path 由 agent 调 `seekpath` 推导（GT 侧同样用 seekpath，label 对齐）。

### 哪些字段被 harness 过滤掉（不进 prompt）

参见 `fields/agent_visible.md`。本题被过滤的字段：

- `schema_version`、`id`、`difficulty`：JSON 元信息
- `material.reference_source`：包含 mp-149，暴露会泄题
- `material.name`：可读名称，仅供 bench 内部 reviewer 使用（prompt 用 formula + structure 组合，不走 name）
- `material.space_group_number`：评分字段（exact 匹配），不给 agent
- `material.initial_structure.source`、`.note`：内部溯源
- `task.type`：harness 控制信号
- `evaluator`、`ground_truth`、`scoring`、`convergence_evidence`、`metadata`：整体不给

## 二、评分规则（自然语言）

### 1. 数据抓取（evaluator）

Agent 执行完 SCF → NSCF (bands) → NSCF (DOS) 三步 workflow 后，harness 得到：

- `bands.out` + `bands.dat`：能带结构数据
- `dos.dat`：DOS 曲线

Evaluator（`type = qe_bands_dos_combined`，`extraction_method = custom`，`extraction_config.k_label_source = "seekpath"`）抓取 6 个字段：

- `band_gap_ev`：从 bands 数据读 VBM / CBM 能级差
- `gap_type`：比较 VBM 与 CBM 的 k 坐标是否相同 → direct / indirect
- `vbm_k_label` / `cbm_k_label`：evaluator 在 **agent 提交的 initial_structure 上再调一次 seekpath** 得到 label 字典 {label: k_coord}，把 VBM/CBM 的 k 坐标映射到最近的 seekpath 高对称点。因为 agent 与 GT 都基于同一个初始结构走 seekpath，label 体系天然对齐（不存在"同一个 k 点有多种命名"的歧义）。
- `dos_e_fermi`：从 dos.dat 读取费米能级位置
- `dos_curve`：提取 `dos_energy_window_ev = [-5, 5]`（evaluator 的 extraction_config）范围内的 DOS(E) 曲线

agent 不需要显式输出这些数值，evaluator 从标准 QE 输出文件直接提取。

### 2. 逐字段比对

| 字段 | 参考值 gt | 容忍度 tol | 规则 | 通过条件 |
|---|---|---|---|---|
| band_gap_ev | TBD_MP（MP mp-149 PBE gap ≈ 0.61 eV；PBE 低估实验 ~1.17 eV） | 0.05 eV | absolute | $\|x - gt\| \le \text{tol}$ |
| gap_type | "indirect" | — | exact | 字符串相等 |
| vbm_k_label | "GAMMA"（seekpath 对 Fd-3m 对 Γ 点的字符串编码） | — | exact | 字符串相等 |
| cbm_k_label | "X"（PBE Si 的 CBM 在 Γ-X 线靠近 X，按 seekpath 路径映射到最近高对称点 X） | — | exact | 字符串相等 |
| dos_e_fermi | TBD_MP | 0.1 eV | absolute | $\|x - gt\| \le \text{tol}$ |
| dos_curve | 外部 ref_file | 0.1 | curve_l2 | $\|x(E) - gt(E)\|_2 \le \text{tol}$ |

单字段连续得分 $p_i \in [0, 1]$：

$$p_i^{\text{absolute}} = \max\!\left(0,\; 1 - \frac{|x - gt|}{\text{tol}}\right)$$

$$p_i^{\text{exact}} = \begin{cases}1 & x = gt \\ 0 & \text{否则}\end{cases}$$

$$p_i^{\text{curve\_l2}} = \max\!\left(0,\; 1 - \frac{\|x(E) - gt(E)\|_2}{\text{tol}}\right)$$

注：`curve_l2` 的具体实现（离散化采样间隔、L2 范数归一化方式）留待 P1 定稿时细化。当前 schema 只约定"E_F ± 5 eV 窗口内的 L2 距离"这个概念。

### 3. 题目分数聚合

聚合方式 `all_fields_weighted`，`partial_credit = true`。题目总分 $S = \sum_i w_i \cdot p_i$，权重（和 = 1.00）：

| 字段 | 权重 $w_i$ |
|---|---|
| band_gap_ev | 0.40（核心物理量） |
| gap_type | 0.15 |
| vbm_k_label | 0.10 |
| cbm_k_label | 0.10 |
| dos_e_fermi | 0.05 |
| dos_curve | 0.20 |

### 4. 通过判据

`pass_criterion = all_fields_passed`：所有字段各自 $p_i > 0$，题目才算"通过"。`partial_credit = true` 意味着未通过时按 $S$ 给部分分。
