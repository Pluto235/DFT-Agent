# Bench Schema Changelog: v1.3 → v1.4

时间节点：2026-05-19（T2 prompt/GT structure alignment）

## 一句话总览

v1.4 修复 v1.2 时埋下的 T2 prompt/GT structure mismatch：T2 问题里 `material.initial_structure.lattice_constants_angstrom` + `lattice_angles_degree` 从 MP raw 改为 T1 canonical GT 弛豫后的值，跟 T2 GT pipeline 实际用的输入结构对齐。

**不动任何 GT 数据**（canonical_gt_t2 / dos_data 保持原样），只动 T2 题目里 agent 看到的初始结构数值。

## 背景

v1.2 时（2026-04-23 杨博意见 1.2）为了"不暴露 T2 用 T1 输出"这一耦合关系，把 M1_T2 / M2_T2 的 `initial_structure.source` 从 `pre-relaxed from M*_T1_*_vc_relax GT` 改成纯 MP id (e.g. `mp-149`)，note 改成 `"relaxed or not is not stated"`。当时**结构数值也回退到 MP raw lattice**（Si: 3.866→3.849 Å），假设 "agent 会自己 vc-relax 让结构落到弛豫态后再算电子结构"。

**实际后果**（2026-05-19 杨博 + weilin 复盘）：

- T2 prompt 协议没强制要 vc-relax；agent 为了省 turn 多数直接 scf+bands+dos
- T2 GT pipeline (`tools/gt_selfrun/t2/t2_params.py:4` docstring) 一直用 T1 canonical GT 弛豫后的 lattice
- 结果：agent 看到 MP raw (Si a=3.849) → 用 raw 算电子结构 → GT 是基于 a=3.866 算的 → 必然不对齐
- 最大 Δa 实测达 **3.18%**（M1_T2_P_black 5.708→5.890），Si 也有 0.45%
- 这种 systematic bias 让 T2 dos_curve / band_gap_ev 评分整体偏低，且与 LLM agent 实际能力**无关**

## 决议（2026-05-19 杨博）

> 我们这样操作。不重新生成 GT，在 bench 的 T2 任务的题目里给 GT 的 canonical T1 geometry；也就是说在做 T2 任务的时候，我们保持 bench 跟 GT 结构的一致性。这样有利于我们后面做别的任务。好处在于避免 MP 的初始结构有问题从而影响其他任务的结果。

设计理由：

- T1 / T2 是独立评分任务，T2 不该被"agent 是否会 vc-relax"二次影响
- T2 / T3 / T4 等所有"基于已弛豫结构跑电子性质"的任务都应该共用同一个 reference 结构（T1 GT 弛豫值）
- 不暴露"这是 relaxed 结构"语义（保持 v1.2 决议）；只是把 lattice 数值替换为 T1 弛豫值，agent 仍可自由选择是否再 vc-relax（自由权保留）

## 按主题详细清单

### 1. 65 道 T2 stable 题 `initial_structure` lattice 替换

| 改动 | 影响 |
|---|---|
| `material.initial_structure.lattice_constants_angstrom.{a,b,c}` ← T1 canonical GT `ground_truth.{a,b,c}.value` | 65 道 (M1_T2 stable 47 + M2_T2 stable 18) |
| `material.initial_structure.lattice_angles_degree.{alpha,beta,gamma}` ← T1 canonical GT `ground_truth.{alpha,beta,gamma}.value` | 同上 |
| `material.initial_structure.atomic_positions_crystal` 不变 | T1 vc-relax 不改 fractional positions（t2_params.py:123 注释明示）|
| `material.initial_structure.source` / `note` / `lattice_type` 不变 | 保持 v1.2 "不暴露 pre-relaxed 状态"语义 |

### 2. 跳过的 21 道 unstable T2

| 类别 | 数量 | 说明 |
|---|---|---|
| `metadata.is_stable=false` | 21 | 不在 leaderboard 主集；其中 7 道是 T2 GT pipeline failed 等 rescue todo (`bench/m12-t2-rescue-7-failed-gt.md`)；rescue 跑完它们重新 promote 到 stable 时**应该一并跑这次 migration** |

3 道之前预期 orphan 的（T1 GT 失败：InAs / In / Sn_beta）全在 21 unstable 里，自动跳过，无需特殊处理。

### 3. 不动的（out of scope）

- canonical_gt_t2/*.json （T2 GT 数据保持原样）
- canonical_gt_t2/dos_data/*.dat（dos_reference 保持原样）
- canonical_gt/*.json（T1 GT 数据保持原样）
- T2 questions 的 `ground_truth` / `scoring`（这次只动 prompt 输入结构）
- T1 questions 任何字段（T1 任务流程不变）
- M3_T2 / M3_T1 questions（M3 还在 worker-0 build out 阶段，schema 单独走）
- prompts/bands_dos.txt（prompt 模板不变，注入的就是新 lattice 值）

### 4. 数据结果（实测）

| 维度 | 数 |
|---|---|
| 扫的总 T2 | 86 |
| Updated | 65 |
| Skipped (unstable) | 21 |
| Skipped (no T1 GT) | 0 |
| 最大 Δa | 3.18% (M1_T2_P_black: 5.708→5.890) |

完整 audit notes 见 [`notes/2026-05-19-t2-prompt-structure-align-to-t1-gt.md`](../../notes/2026-05-19-t2-prompt-structure-align-to-t1-gt.md)。

### 5. Generator 变更

`questions/generate.py:build_t2()` 加注释提醒：跑完 generate.py 后必须再跑 `tools/migrate_t2_initial_structure_to_t1_relaxed.py`，否则 lattice 会被 MP raw 覆盖。

未来可考虑直接把 migrate 逻辑 inline 进 `build_t2`（读 T1 canonical GT 后覆盖 lattice），但当前用独立脚本 + NOTE 的 pattern 跟 0518 VBM migration 一致，方便审计 + 可幂等重跑。

### 6. 评分 / 历史 run 影响

| 项 | 是否需要重打分 | 原因 |
|---|---|---|
| 历史 T2 runs (report.json 已固化的) | ❌ 不需要 | agent 实际跑过的 dos.dat 是基于他当时看到的 prompt 结构跑的 result；评分公式 / GT 数据 / dos_reference 都没动；旧 run 的 score 仍代表"agent 在 MP raw 结构条件下解题"的能力，作历史 baseline 保留 |
| 未来 T2 baseline runs | ✅ 自动受益 | 新 baseline 看到弛豫态 lattice → 不主动 vc-relax 也能基本对齐 GT → systematic bias 消除 |

如果以后想看 v1.4 上线前后 LLM 在同一道 T2 的真实能力变化，可以跑一次窄子集 cross-version baseline（标准档 21 道 T2 即可），但不强求。

### 7. schema_version 字段升级

| 改动 | 影响 |
|---|---|
| 65 道 stable T2 question `schema_version` `"1.3"` → `"1.4"` | 标识本次 lattice 替换；migration 脚本自动同步 |
| 21 道 unstable T2 保持 `"1.3"` | 没动它们，version 留旧值，rescue todo 把它们 promote 时一并升 `"1.4"` |
| 4 道 example JSON (`schema/examples/*_T2_*.json`) | 跟随升级到 `"1.4"`（手动）|

**注**：以上 schema_version 升级在 migration 脚本里不强制（保持脚本范围最小）；如果之后需要严格 schema 版本治理，单独走一个 commit 集中升 schema_version 即可。

## 受影响的文件汇总

| 类别 | 文件 | 状态 |
|---|---|---|
| 65 stable T2 question JSON | `questions/M{1,2}_T2/*.json` | lattice + angles 字段替换 |
| Migration 脚本 | `tools/migrate_t2_initial_structure_to_t1_relaxed.py` | 新增（幂等可重跑）|
| Generator | `questions/generate.py:build_t2` | 加 NOTE 提醒后续跑 migration |
| 本 changelog | `schema/changelog_v1.3_to_v1.4.md` | 新增 |
| Audit notes | `notes/2026-05-19-t2-prompt-structure-align-to-t1-gt.md` | 新增 |

## 关联

- 触发讨论：weilin 0519 早 user prompt + 杨博文字决议
- 上游 schema 决议：[`changelog_v1.1_to_v1.2.md` §6](changelog_v1.1_to_v1.2.md)（v1.2 当时的"不暴露 pre-relaxed"决定）
- T2 GT pipeline 关于 input structure 的描述：`tools/gt_selfrun/t2/t2_params.py:4`
- 已知 fail 列表：[`todo/bench/m12-t2-rescue-7-failed-gt.md`](../../../todo/bench/m12-t2-rescue-7-failed-gt.md)（rescue 跑完 promote 到 stable 时一并跑本次 migration）
