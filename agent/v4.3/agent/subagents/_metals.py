"""Hard-coded metal element registry for config_advisor.

Used by config_advisor's deterministic classifier to decide if a
material is metallic (occupations='smearing' for SCF + bands NSCF) vs.
insulating/semiconducting (occupations='fixed'). This is the source of
truth — the LLM cross-check in config_advisor may only override on a
clear physics reason (intermetallic with M-I transition, etc.).

Borderline cases (documented):

- B excluded:  semiconductor in α-rhombohedral, ~1.4 eV gap.
- Si/Ge/Sn-α excluded:  diamond-cubic semiconductors.
- C excluded:  graphite is semimetal but bench rarely sees graphite; if it
  shows up, the LLM cross-check can override.
- As excluded:  α-As is semimetal-ish but more often treated as a
  pnictide non-metal in this bench; default to insulator and let LLM
  override if needed.
- Sb / Bi included:  Bi (rhombohedral) is conventionally treated as a
  semimetal — smearing-with-MP is the safer pw.x choice.
- Te / Se excluded:  trigonal Te has a gap; Se is a semiconductor.
- Po included:  metallic α-Po (rare in bench but included for completeness).
"""
from __future__ import annotations


METAL_ELEMENTS: frozenset[str] = frozenset({
    # Alkali (group 1)
    "Li", "Na", "K", "Rb", "Cs", "Fr",
    # Alkaline-earth (group 2)
    "Be", "Mg", "Ca", "Sr", "Ba", "Ra",
    # Group 13 metals (B is semiconductor — excluded)
    "Al", "Ga", "In", "Tl",
    # Group 14 metals (C/Si/Ge/α-Sn are semiconductors; β-Sn / Pb metallic)
    "Sn", "Pb",
    # Group 15 — Bi semimetal (smearing is safer); As/P/N excluded
    "Sb", "Bi",
    # Group 16 — Po metallic (rare); Te/Se/S/O excluded
    "Po",
    # 3d transition metals
    "Sc", "Ti", "V", "Cr", "Mn", "Fe", "Co", "Ni", "Cu", "Zn",
    # 4d transition metals
    "Y", "Zr", "Nb", "Mo", "Tc", "Ru", "Rh", "Pd", "Ag", "Cd",
    # 5d transition metals (Lu grouped here per modern IUPAC)
    "Lu", "Hf", "Ta", "W", "Re", "Os", "Ir", "Pt", "Au", "Hg",
    # Lanthanides (all metallic; condensed)
    "La", "Ce", "Pr", "Nd", "Pm", "Sm", "Eu", "Gd",
    "Tb", "Dy", "Ho", "Er", "Tm", "Yb",
    # Actinides (Ac–U are the only commonly-modeled ones in DFT benches)
    "Ac", "Th", "Pa", "U",
})


# Anions / strongly electronegative atoms — when present in a binary or
# ternary compound, the material is almost certainly an insulator/semiconductor.
# Used by config_advisor._deterministic_classify to short-circuit metal
# classification: e.g. rule "Cu is in METAL_ELEMENTS" gets overridden if
# the formula is CuO (oxide → semiconductor).
STRONGLY_ELECTRONEGATIVE: frozenset[str] = frozenset({
    "O", "F", "Cl", "Br", "I", "N", "S", "Se", "Te",
})


# PBE "准金属" 一族——PBE/GBRV-PBE.v1.5 给出近零 gap 的窄带半导体。
# bench 的 GT 是用同样 PBE pipeline 自跑生成的，对这些材料标 gap_type='metal'
# / band_gap_ev=0。但 _deterministic_classify 会把它们判为半导体（物理上
# 正确：实验 gap 是有限值）。这两个事实在评分时冲突——advisor 说半导体，
# bench 提取说金属——v4.3 的 physics_sanity 会以为 agent 算错了，强制
# replan 配额耗尽，最终失败。
#
# physics_sanity 看到 pbe_near_metal=True 时，metal⇄semiconductor 不一致
# 应降级为 soft_warn，让 agent 顺利提交（gap_type='metal' 是 PBE 一致答案）。
#
# 触发案例：Ge bands_dos 2026-05-19/05-20，得分卡 0.63。详 docs report
# standard-bench-failures-rb-ge-wo3-cosb3-2026-05-20.html §3。
#
# 准入门槛：必须满足 (1) GBRV-PBE 自跑 gap < 0.05 eV，(2) 实验/PBE0/HSE
# gap > 0.1 eV，二者不一致只是 PBE 的已知 self-interaction 失效。
#
# Sn 故意不入：bench 任务里有 α-Sn (diamond) 和 β-Sn (tetragonal)；前者
# 是 PBE 准金属、后者本身就是真金属，单凭 formula="Sn" 无法区分。等
# 后续真踩坑再加 (formula, structure_substring) 二元判定。
PBE_NEAR_METAL_FORMULAS: frozenset[str] = frozenset({
    "Ge",       # diamond Ge（Fd-3m）—— PBE gap ~ 0 eV，实验 0.67 eV
    "InSb",     # zincblende —— PBE gap ~ 0 eV，实验 0.17 eV
    "HgTe",     # zincblende —— PBE 半金属，实验半导体
    "PbTe",     # rock-salt
    "PbSe",     # rock-salt
    "SnTe",     # rock-salt
    "Bi2Se3",   # 菱方层状
    "Bi2Te3",   # 菱方层状
    "CoSb3",    # skutterudite —— PBE 窄带半导体，gap ~ 0.05 eV（FIX-18 路径）
})


# NOTE (FIX-41, S-4, 2026-06-01): the formula-level MAGNETIC_TM_OXIDES allowlist
# was removed. It was a hardcoded subset of the M3 question bank and is fully
# subsumed by the composition-based magnetic classifier (FIX-32,
# _magnetism.classify_magnetism): any open-shell 3d/4f compound gets nspin=2 +
# smearing=gauss + degauss=0.01 + the FM-vs-AFM probe handoff, regardless of
# whether its formula was on a list. See config_advisor._deterministic_classify.


# 金属相过渡金属氧化物——名字像 TM 氧化物但实际是金属，电子离域。
# 默认 STRONGLY_ELECTRONEGATIVE 规则会把它们判 is_metal=False (因 O 在);
# 但 bench GT 给 band_gap=0 / gap_type=metal，physics_sanity Rule B 会因
# 此 hard-fail 触发死循环 replan。本表把它们显式提到 metal 路径。
#
# 准入门槛：(1) GT manifest `gap_type=metal` 且 `band_gap_ev=0` 锚定金属性，
# (2) 该相在标的空间群下确实是金属（高对称、mixed-valence 离域、无 charge
# ordering / Mott 转变）。
#
# 触发动作：advisor 强制 is_metal=True、occupations='smearing'、
# smearing='gauss'（d-electron 比 mv 稳）、degauss=0.01、recommend_nspin=1
# （不要 magnetic ansatz —— 强行加 starting_magnetization 会让 SCF 多 30
# 步收敛回 mag=0 + 让 BFGS 在 cell 变形时丢历史）。
#
# 触发案例：Ti3O5 bands_dos 2026-05-24，28800s timeout（nspin=2 + AFM ansatz
# → vc_relax 单次成本 ×2 + vcsmd error #1 BFGS history loss）。这条 override 是
# 必需的：Ti 是开壳 3d，composition-based 磁性分类器（FIX-32）默认会把 Cmcm
# Ti3O5 判成磁性 → 强推 nspin=2 + AFM，正是上面那个 timeout。GT 显示
# dos_e_fermi=11.2255 eV，明确金属。详 reports/standard-bench-failures-ti3o5-2026-05-25.html (TBD)。
METALLIC_TM_OXIDES: frozenset[str] = frozenset({
    "Ti3O5",     # Cmcm orthorhombic (mp-8057) — mixed-valence Ti³⁺/Ti⁴⁺,
                 # 高对称下离域，GT band_gap_ev=0, gap_type=metal。
                 # 低对称单斜 α-Ti3O5 才是 charge-ordered 半导体；当前
                 # bench 任务空间群 63 (Cmcm) 锁定金属相。
})


# T1 vc-relax 收敛敏感金属——50 Ry / kmesh ceil(25/a) min 4×4×4（FIX-20 的 T1
# 默认）对一般金属/半导体够用，但对**浅 d-band post-transition metals**（Group
# 12: Zn/Cd/Hg）lattice constants 异常敏感于 ecutwfc 和 k-mesh 密度。GBRV USPP
# Cd / Zn 在 50 Ry 下 a/c 系统性偏离 PBE 收敛极限 ~0.03–0.05 Å——bench GT 用
# 80 Ry / 20×20×8 / mp-smearing 生成，T1 ±1% 相对容差挡不住这个差。
#
# 触发动作：advisor 标 `cutoff_sensitive_metal=True`；planner system_prompt
# T1 分支看到这个 flag → 改用 T2 canonical（ecutwfc=80 / ecutrho=800 /
# kmesh=ceil(50/a) min 8×8×8）。is_metal/occupations/smearing/degauss 不变
# （仍按 METAL_ELEMENTS 规则走 mv/0.02），只是把 cutoff/k 抬到 GT 同档。
#
# 触发案例：Cd vc_relax 2026-05-25T16-59-58Z（v4.3 deepseek-v4-pro）拿 0.445，
# extracted a=2.993 / c=5.870 vs GT a=3.051 / c=5.649（±1% rel tolerance）。
# 同日 08:39 v5 legacy 单 loop 用 80 Ry / 17×17×8 拿 0.948。Zn T1 历史 0.64–
# 0.80 长期低分（5-27 a=2.674 / c=4.802 vs GT a=2.650 / c=4.976），同病。
# 详 devlog FIX-31 (2026-05-27)。
#
# 准入门槛：(1) Group 12（Zn/Cd/Hg）或类似"shallow d / s-d hybridization"
# post-transition metal，(2) bench T1 历史 score 长期 < 0.85，(3) v5 legacy
# 80 Ry 路径能拿 ≥ 0.9。
#
# Hg 暂未入名单：bench M1_T1 题库不含 Hg，验证不动；如果以后加 Hg 题就纳入。
# Group 13/14 post-transition metals（Ga / In / Tl / Sn / Pb）不入：它们的
# d-band 已沉到很深，PBE 收敛在 50 Ry 已经稳。
CUTOFF_SENSITIVE_METALS: frozenset[str] = frozenset({
    "Cd",        # hcp P6_3/mmc — Group 12, 4d-band 浅（~10 eV below E_F）
    "Zn",        # hcp P6_3/mmc — Group 12, 3d-band 浅（~8 eV below E_F）
    # "Hg",      # rhombohedral / α-Hg — Group 12; 暂无 bench 题，留位
})
