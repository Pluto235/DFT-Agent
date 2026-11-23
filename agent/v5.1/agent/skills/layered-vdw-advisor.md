---
name: layered-vdw-advisor
description: 层状 vdW 材料（h-BN, graphite, MoS2 等）—— PBE 不要自由 relax c-axis
trigger:
  any_of:
    - material_family: layered_vdw
priority: 10
---

# Key principles

层状（sp² 共价层 + 弱 vdW 层间）材料用裸 PBE 跑 vc-relax 会**显著
缩短 c-axis**，因为 PBE 不含 London dispersion，层间吸引被低估，
BFGS 会把 c 一路压到层内 σ 键斥力开始起作用为止。物理后果：

- 收缩后的 cell 在 BZ kz 方向变宽，所有 A/H/L 点的能量被推高；
  VBM 偏移会让 dos_e_fermi 对应位置偏离真物理值
- bands.dat 上的 VBM/CBM 落在错位置 → `vbm_k_label` 也跟着错
- gap_type 通常仍正确，但 `band_gap_ev` 偶尔被推过物理量级以外

对这类体系的推荐做法：

1. **首选不跑 vc-relax**：input cell 通常已经是 PBE+vdW 拟合或实验
   值的合理近似；裸 PBE 的 relaxed c 在 0 K 下没有物理意义。当 task
   prompt 没要求 vc-relax 时，直接用 input cell 跑 SCF/bands/dos。
2. **必须 relax 时只动原子坐标**：vc-relax 的 `&CELL` namelist 加
   `cell_dofree = '2Dxy'`（只 in-plane）；或者退一步用 `calculation
   = 'relax'`（只动 ATOMIC_POSITIONS，cell 完全冻结）。
3. **绝对不要**：默认 `vc-relax` + `cell_dofree = 'all'` —— 那就是
   把 c 喂给 BFGS。

# Common failure modes

- vc-relax 跑完 c 比 input 缩水（h-BN / graphite / MoS2 都是典型
  几 % 量级）。看 relax.out 的 "Begin final coordinates" 跟 input
  比 c 缩超过几 % 即认定中招。回退到 input cell 跑 SCF 即可挽救本步。
- 即使 c 没缩到那么离谱，PBE 缺 vdW 也会让层间相对几何（堆叠相位）
  飘 —— 对 h-BN AA' stacking 这是 sub-electron-volt 量级的扰动。

# Debug heuristics

- 看到 c/a 比例明显大（> 2 量级）且 hex/trig → 默认走 layered-vdw
  协议（输入即合理，不 relax cell）。
- task prompt 给的 c 跟现有 vc-relax 输出对比偏差超过几 % 时，优先
  trust input cell：裸 PBE 缺色散，relaxed c 在 0 K 下没有物理意义。
- 若仍想验证结构（防御性），跑一次 `calculation='relax'`（atoms only）
  看 forces，不要 vc-relax。

# Counter-examples (do NOT do this)

- ❌ vc-relax + `cell_dofree='all'` —— c-axis 必缩。
- ❌ relax 后用缩水的 cell 跑 SCF/bands/dos —— VBM 位置错位，
  `dos_e_fermi` 失分。
- ❌ 给 BFGS 一个超紧的 `press_conv_thr` 期望它在 0 K PBE 上"找到
  真物理 c-axis" —— 物理就不在那儿，pressure-converged ≠ 实验 c。
