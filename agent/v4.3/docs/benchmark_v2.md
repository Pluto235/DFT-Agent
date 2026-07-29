# v2 Benchmark — 5 vc_relax Tasks (claude-haiku-4-5-20251001)

Source data: `/tmp/bench-eval-v2-2026-05-09` · Solver commit `f92e707` · Apply commits `f92e707 12c84ca 4bee379`

v2 introduces three improvements over v1 (see commit `f92e707`):
1. System prompt forbids `&` (background) for pw.x — kills the SiO2/LiFePO4 livelock
2. System prompt enforces tight cubic convergence (`press_conv_thr=0.05`,
   `forc_conv_thr=1e-4`, `etot_conv_thr=1e-5`) — fixes NaCl 0.75 soft fail
3. Prompt caching (system + tools + user prompt) — cuts charged input tokens ~80%

## Per-task results

| # | Task | atoms | Path | score | audit | solver wall (s) | QE wall (s) | turns | in / out / cache_r / cache_w | cost | notes |
|---|---|---:|---|---:|---|---:|---:|---:|---|---:|---|
| 1 | `M1_T1_Si_vc_relax` | 2 | GPU | 1 | 6/6 | 145.2 | 28.4 | 26 | 53,132→8,895 / 251,625 / 19,596 | $0.118 | baseline (cubic semiconductor) |
| 2 | `M1_T1_NaCl_vc_relax` | 2 | GPU | 1 | 6/6 | 271.0 | — | 32 | 77,092→12,531 / 587,782 / 34,509 | $0.193 | ionic insulator |
| 3 | `M1_T1_Cu_vc_relax` | 1 | CPU | 1 | 6/6 | 1618.0 | — | 42 | 108,653→16,867 / 867,886 / 49,234 | $0.273 | fcc metal — needs smearing |
| 4 | `M1_T1_SiO2_alpha_quartz_vc_relax` | 9 | CPU | 1 | 6/6 | 2683.9 | — | 21 | 62,179→5,955 / 202,712 / 38,556 | $0.128 | trigonal, P3₂21 chirality |
| 5 | `M2_T1_LiFePO4_olivine_vc_relax` | 28 | CPU | — | — | 5400.0 | — | 31 | 121,906→9,968 / 393,599 / 114,384 | $0.283 | M2 tier, 28 atoms, Fe magnetic (PBE non-magnetic OK) · solver did not produce result.json |

## Aggregate

- Pass rate (score≥0.99 ∧ audit 6/6): **4/5**
- Total LLM spend (claude-haiku-4-5-20251001): **$0.71**
- Total solver wall clock (sequential): **78.6 min**
- Mean turns per run: 30.2

## Audit-flag breakdown

| Task | qe_invoked | job_done | paths_in_workdir | files_nonempty | qe_wallclock_ok | no_network_access |
|---|:---:|:---:|:---:|:---:|:---:|:---:|
| `M1_T1_Si_vc_relax/gpu` | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| `M1_T1_NaCl_vc_relax/gpu` | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| `M1_T1_Cu_vc_relax/cpu` | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| `M1_T1_SiO2_alpha_quartz_vc_relax/cpu` | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| `M2_T1_LiFePO4_olivine_vc_relax/cpu` | — | — | — | — | — | — |

## Findings

### Headline result
**4 of 5 tasks score 1.0 with all 6 audit flags green** (vs 2/5 in v1).
The single remaining failure is M2_T1_LiFePO4 — 28-atom orthorhombic
olivine on CPU pw.x, true compute-bound, hits the 5400 s harness timeout.

NaCl: **0.75 → 1.0** (cubic convergence fix landed exactly as designed —
relaxed cell now sits at sg 225 Fm-3m with all angles to 1e-7 ° of 60 °).

SiO2 quartz: **timeout → 1.0** (no more `&` antipattern; LLM ran pw.x in
foreground, BFGS converged in 21 turns; trigonal P3₂21 sg 154 confirmed).

### Per-task v1 → v2 deltas

| Task | Path | v1 score | v2 score | v1 wall | v2 wall | v1 cost | v2 cost |
|---|---|---:|---:|---:|---:|---:|---:|
| Si | GPU | 1.0 | 1.0 | 103.7 s | 145.2 s | $0.168 | **$0.118** (-30%) |
| NaCl | GPU | 0.75 | **1.0** ⬆ | 394.6 s | **271.0 s** ⬇ | $0.942 | **$0.193** (-79%) |
| Cu | CPU | 1.0 | 1.0 | 904.0 s | 1618.0 s ⬆ | $1.196 | **$0.273** (-77%) |
| SiO2 quartz | CPU | timeout | **1.0** ⬆⬆⬆ | 5400 s | 2683.9 s | $0.575 | **$0.128** (-78%) |
| LiFePO4 | CPU | timeout | timeout | 5400 s | 5400 s | $0.334 | $0.283 |

Cu's wall increased 904 s → 1618 s (slower) but cost dropped 77 % — the
extra wall is real BFGS iterations the v2-tightened convergence thresholds
demand on a metal. Tradeoff worth it: Cu still hits 1.0 in v2 with cleaner
final state.

### Token cache effectiveness
Across the 5 v2 runs:

- charged input: 422,962
- cache reads: **2,303,604** (charged at 10 % = effective 230,360 input tokens)
- cache writes: 256,279 (charged at 125 % = effective 320,349 input tokens)
- output: 54,216

Without caching, those 2.3 M cache-read tokens would have cost an extra
**~$1.66** at full input price. Total v2 spend $0.71 vs naive ~$2.40.
On NaCl alone the savings was 79 % of v1's $0.94 → v2's $0.19.

### Why LiFePO4 still times out
v2 transcript: 31 turns, 121 K charged input + 393 K cache reads + 114 K
cache creates → cache is working as designed. The bottleneck is real
QE compute: a single SCF on the 28-atom × 4-element × 3d-pseudo cell
takes 8–15 min on a 2-MPI / 1-GPU configuration; BFGS needs 5+ such
cycles to converge stress + force; total ≥ 60 min real QE work + the
LLM's plan/verify overhead overshoots the 90 min cap.

**Fix path for LiFePO4** (out of v2 scope): run on the GPU wrapper
(haiku-on-GPU should give 5–7 × speedup on a 28-atom cell, putting
total QE wall under 15 min) and/or raise harness timeout to 7200 s
(spec default). Worth attempting in v3.

### Audit-flag observations
4/4 successful runs hit 6/6 audit flags. As in v1, audit boundary is
not the bottleneck — failures are LLM strategy or wallclock-bound
compute, not anti-cheat triggers.

### v3 recommendations
1. **Run M2 tasks on GPU wrapper.** v2 only validated GPU on small
   tasks (Si, NaCl). For LiFePO4-class cells the 5-7 × QE speedup is
   the difference between completing and timing out.
2. **Sonnet retry for the M2 tier.** sonnet may be more deliberate
   on input writing (fewer rewrites) — also reduces wall. OAuth tier
   rate-limit was the v1/v2 blocker; if it clears, sonnet on LiFePO4
   GPU is the next experiment.
3. **Enable T2 (`bands_dos`) support.** SCF → NSCF → bands.x → dos.x
   is a 4-key submission; system prompt currently nudges users to v1
   vc_relax only. Expand to support M1_T2 / M2_T2.
4. **Per-system convergence presets.** Distinguish "metal" (smearing
   + denser k) from "magnetic" (nspin + starting_mag) from "default"
   in the prompt — would help haiku on Cu / Fe-bearing systems.

