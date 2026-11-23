# Review 视图：M2_T2_CsPbBr3_bands_dos

对应 schema JSON：`example_M2_T2_CsPbBr3_bands_dos.json`

本文件对该题目 JSON 做两件"翻译"：

1. 按 `agent_visible.md` 的字段筛选规则 + harness 拼接约定，给出 **agent 实际看到的 prompt 文本**（预期形式）
2. 按 `evaluator` / `ground_truth` / `scoring` 三字段，给出 **评分规则的自然语言描述**

## 一、Agent 实际看到的 prompt

以下是 harness 用 `bench/prompts/bands_dos.txt` 模板 render 后 agent 看到的完整文本。方括号内是 slot 来源注释（agent 不会看到这些注释）。

```text
[rendered from bench/prompts/bands_dos.txt with slots from material.formula/structure/space_group/atoms_per_primitive_cell]
For CsPbBr3 (cubic perovskite, space group Pm-3m, 5 atoms per primitive
cell), run a full bands + DOS workflow on the initial structure provided
below:
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
6.002400 0.000000 0.000000
0.000000 6.002400 0.000000
0.000000 0.000000 6.002400

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

Agent 需自行推断计算参数（ecutwfc、ecutrho、kmesh_scf、kmesh_nscf_dos、nbnd、conv_thr、DOS smearing 等）；k-path 由 agent 调 `seekpath` 推导。GT 侧同样用 seekpath，label 体系对齐。

### 哪些字段被 harness 过滤掉（不进 prompt）

参见 `fields/agent_visible.md`。本题被过滤的字段：

- `schema_version`、`id`、`difficulty`：JSON 元信息
- `material.reference_source`：包含 mp-1014168，暴露会泄题
- `material.name`：可读名称，仅供 bench 内部 reviewer 使用（prompt 用 formula + structure 组合，不走 name）
- `material.space_group_number`：评分字段（exact 匹配），不给 agent
- `material.initial_structure.source`、`.note`：内部溯源
- `material.pseudopotential.note`："GBRV USPP PBE 不开 SOC" 等背景说明
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
- `vbm_k_label` / `cbm_k_label`：evaluator 在 agent 提交的 initial_structure 上再调一次 seekpath 得到 label 字典 {label: k_coord}，把 VBM/CBM 的 k 坐标映射到最近的 seekpath 高对称点。agent 与 GT 都基于同一个初始结构走 seekpath，label 体系天然对齐。
- `dos_e_fermi`：从 dos.dat 读取费米能级位置
- `dos_curve`：提取 `dos_energy_window_ev = [-5, 5]` 范围内的 DOS(E) 曲线

agent 不需要显式输出这些数值，evaluator 从标准 QE 输出文件直接提取。

### 2. 逐字段比对

| 字段 | 参考值 gt | 容忍度 tol | 规则 | 通过条件 |
|---|---|---|---|---|
| band_gap_ev | TBD_MP（mp-1014168 PBE gap，文献 ~1.79 eV；实验 ~2.3 eV，PBE 低估 ~22%） | 0.05 eV | absolute | $\|x - gt\| \le \text{tol}$ |
| gap_type | "direct" | — | exact | 字符串相等 |
| vbm_k_label | "R"（seekpath 对 Pm-3m 输出 R=(1/2,1/2,1/2)；VBM 成分 Br 4p 与 Pb 6s 反键） | — | exact | 字符串相等 |
| cbm_k_label | "R"（CBM 成分 Pb 6p；VBM/CBM 同在 R → direct gap） | — | exact | 字符串相等 |
| dos_e_fermi | TBD_MP | 0.1 eV | absolute | $\|x - gt\| \le \text{tol}$ |
| dos_curve | 外部 ref_file | 0.15 | curve_l2 | $\|x(E) - gt(E)\|_2 \le \text{tol}$ |

单字段连续得分 $p_i \in [0, 1]$：

$$p_i^{\text{absolute}} = \max\!\left(0,\; 1 - \frac{|x - gt|}{\text{tol}}\right)$$

$$p_i^{\text{exact}} = \begin{cases}1 & x = gt \\ 0 & \text{否则}\end{cases}$$

$$p_i^{\text{curve\_l2}} = \max\!\left(0,\; 1 - \frac{\|x(E) - gt(E)\|_2}{\text{tol}}\right)$$

注：钙钛矿 DOS 结构比 Si 丰富（Pb 6p、Br 4p 贡献多），`dos_curve` tolerance 放宽到 0.15（vs M1_T2 Si 的 0.1），允许更多曲线形状的差异。

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

### 本题的特殊难点（与 M1_T2 相比）

- 直接带隙 at R 点（区别于 Si 的间接带隙 Γ→X）——agent 要能从能带数据正确判断 direct/indirect
- 5 原子原胞 + 更多条带，bands 数据规模约是 M1 Si 的 5 倍
- PBE 低估 22% 的已知偏差；如果开 SOC 还会再降 0.4–0.5 eV（M5 讨论）—— tolerance 在这里指 agent 算出的 PBE 值与我们 PBE 参考值之间，与实验值无关
