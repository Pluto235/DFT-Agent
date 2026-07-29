# 字段规范：`evaluator`

> evaluator 类型枚举（qe_vc_relax_output / qe_bands_dos_combined 等）和每类的 extract_fields 选择都是草案。请专家看看是否漏掉该评分的字段、是否选错了数据源、extraction_method 是否可靠。

## 用途

声明如何从 QE 输出抓取评分字段。agent 只负责把 QE 跑对、产出标准 QE 输出文件，数据提取由 evaluator 做确定性后处理。

## Agent 可见性

本字段**整体不给 agent**。evaluator 是服务端评分逻辑，agent 无需感知。暴露会让 agent 针对抓取规则作弊（比如硬编码特定字符串到 QE 输出）。详见 `agent_visible.md`。

## JSON 片段

```json
"evaluator": {
  "type": "qe_vc_relax_output",
  "extract_fields": [
    "a", "b", "c",
    "alpha", "beta", "gamma",
    "space_group_number",
    "point_group", "crystal_system"
  ],
  "data_sources": {"primary": "relax.out"},
  "extraction_method": "pymatgen_spacegroup_analyzer",
  "extraction_config": {"symprec": 1e-3, "angle_tolerance": 5}
}
```

## 子字段规范

| 子字段 | 必填 | 类型 | 说明 |
|---|---|---|---|
| type | 是 | string | Evaluator 类型 ID，见下表 |
| extract_fields | 是 | array | 要从输出中抓取的字段名（需与 `ground_truth` 的 key 对齐） |
| data_sources | 是 | object | 数据源文件映射 |
| extraction_method | 否 | string | 底层提取器（pymatgen/regex/custom 等）|
| extraction_config | 否 | object | extractor 的配置参数 |

## Evaluator 类型枚举

| type | 适用 task | 典型 extract_fields | 数据源 |
|---|---|---|---|
| qe_vc_relax_output | T1 vc_relax | a/b/c/α/β/γ/space_group_number/point_group/crystal_system | relax.out |
| qe_scf_total_energy | SCF 单点 | total_energy_ev_per_fu | scf.out |
| qe_bands_gap | T2 带隙 | band_gap_ev / vbm_energy / cbm_energy / gap_type / vbm_k / cbm_k | bands.out + bands.dat |
| qe_bands_dos_combined | T2 完整能带+DOS | band_gap_ev / gap_type / vbm_k_label / cbm_k_label / dos_e_fermi / dos_curve | bands.out + bands.dat + dos.dat |
| qe_dos_curve | T2 DOS 单独 | dos_e_fermi / dos_curve（引用文件） | dos.dat |
| qe_dfpt_born_dielectric | T3 | born_z_star（按原子张量列表） / eps_infinity（3×3 张量） | ph.out + tensors.xml |
| eos_bm_fit | T4 | v0 / b0 / b0_prime / e0 | volume_scan/*.out 聚合 |

## `data_sources`

常见 key：

| key | 含义 |
|---|---|
| primary | 主输出文件（如 relax.out） |
| input | 对应的输入文件（如 relax.in），部分 evaluator 需要原始 input 做参考 |
| bands_data | 能带数据文件 |
| dos_data | DOS 数据文件 |
| scan_dir | 扫描类任务的结果目录（T4 EOS 需要） |

文件路径相对于 agent 的工作目录 `work_dir/`。

## `extraction_method` 可选值

| 取值 | 适用 | 说明 |
|---|---|---|
| pymatgen_spacegroup_analyzer | T1 对称性分析 | 用 pymatgen `SpacegroupAnalyzer` 算 SG、PG、CS |
| regex_total_energy | SCF 能量 | 正则抓 `!\s+total energy\s*=` 行 |
| bands_x_parser | 能带解析 | 解析 bands.x 输出的 `.dat` 文件 |
| dos_x_parser | DOS 解析 | 解析 dos.x 输出的 `.dat` 文件 |
| ph_x_parser | DFPT 解析 | 解析 ph.x 输出的张量块 |
| bm_eos_fitter | EOS 拟合 | Birch–Murnaghan 方程拟合 |
| custom | 其他 | 由具体 evaluator 实现类指定 |

## `extraction_config` 常用 key

| key | 适用 evaluator | 说明 |
|---|---|---|
| symprec | qe_vc_relax_output | pymatgen SpacegroupAnalyzer 对称性容差 |
| angle_tolerance | qe_vc_relax_output | 同上 |
| dos_energy_window_ev | qe_dos_curve / qe_bands_dos_combined | DOS 比对窗口（相对 E_F）|
| k_label_source | qe_bands_dos_combined / qe_bands_gap | VBM/CBM k-label 的来源；取值 `"seekpath"`（默认，v1.2 起）表示 evaluator 在 agent 提交的 initial_structure 上调 `seekpath.get_path` 得到 {label: k_coord} 字典，再把 VBM/CBM 的 k 坐标映射到最近 label |

## T2 bands_dos 的 k-path 约定（v1.2 新增）

原则：**bench prompt 指示 agent 用 `seekpath` 推导 k-path；evaluator 侧也用 seekpath，两侧 label 体系天然对齐**。这样可以避免"同一个 k 点在不同文献里被命名不同"导致的评分歧义（如某材料的 VBM 可能被一份文献写成 "M"、另一份写成 "L"）。

实现细节：

- `ground_truth.vbm_k_label` / `cbm_k_label` 的 value 必须是 seekpath 对该材料空间群输出的 label 字符串（例如立方 Fd-3m 的 "GAMMA"、Pm-3m 的 "R"）
- evaluator 对 agent 输出的处理：在 agent 提交的 initial_structure 上调一次 `seekpath.get_path(structure)` 得到高对称点字典，把 agent 计算出的 VBM/CBM k 坐标映射到**最近的**字典 key，作为 `vbm_k_label` / `cbm_k_label` 的抽取结果
- seekpath 版本锁定：P1 确定一个版本（如 seekpath==2.1.0）写入 bench 仓库的 `requirements.txt`，避免未来版本迭代改变 label 命名

## T2 nspin=2 双自旋并集（2026-06-05 新增）

背景：QE `bands.x` 单次运行（`spin_component` 默认 1）只把**多数自旋**（spin-up）
写进 `bands.dat`。对铁磁金属，若 Fermi 面主要由**少数自旋**态贡献（如 fcc Ni：
多数 d 带全在 E_F 以下，少数 d 带跨过 E_F），只看多数自旋会把金属误判成小带隙
半导体——`gap_type` 从 `metal` 翻成 `indirect`，连带 `band_gap` / `vbm_k_label` /
`cbm_k_label` 全错，塌掉约 0.9 权重（实测 Ni_fcc 0.077 vs 应得 0.83）。

修复：`extract_t2_fields` 在 `parse_bands_dat` 之后，从 pw.x bands NSCF 输出
（`bands_out`，含 `------ SPIN UP/DOWN ------` 两段全本征值）解析少数自旋，按 k
点把两自旋本征值**并集**进 `bands["eigenvalues_ev"]`，再做 VBM/CBM 判定。要点：

- `parse_bands_out_eigenvalues` 锚定**最后一次** SPIN UP/DOWN 段（agent 重试会把
  多次 pw.x 运行追加进同一个 .out）。
- 对齐自检：`bands.dat`（spin-up，3 dp）与 pw.x SPIN UP（4 dp）逐 k 比对，k 数/
  本征值不符则跳过并集，回落 `bands.dat`-only（不回退现状）。
- **作用域：仅 GT≠绝缘体**（`is_metal_per_gt is not False`）。金属的 GT 是
  "gap 0 / metal"，与自旋方法无关，并集只会让 agent 抽取**对齐** GT；而磁性
  绝缘体的 baked GT（band_gap / VBM）是 `tools/gt_selfrun` 单自旋生成的，对绝缘体
  做双自旋抽取会与 GT 发散——磁性绝缘体的双自旋抽取需同步重生成 GT（后续工作）。
- 审计：并集触发时 `diagnostics.nspin2_union = true`；跳过时为 `"skipped: …"`。
- 数据已就绪、与 solver 解耦：`bands_out` 本就是 T2 必交产物且含两自旋，故历史
  run 可直接 `rerun_eval` 复评救分。

## 说明

- Evaluator 是**服务端**逻辑，agent 不感知其存在
- `extract_fields` 必须是 `ground_truth` 的键的子集（否则评分时找不到 gt）
- extraction 失败时，evaluator 返回 `{"<field>": null}`，由 scoring 层判失败
