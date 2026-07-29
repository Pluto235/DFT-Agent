# v1 Benchmark — 5 vc_relax Tasks (claude-haiku-4-5-20251001)

Source data: `/tmp/bench-eval-2026-05-09`

## Per-task results

| # | Task | atoms | Path | score | audit | solver wall (s) | QE wall (s) | turns | in / out / cache_r / cache_w | cost | notes |
|---|---|---:|---|---:|---|---:|---:|---:|---|---:|---|
| 1 | `M1_T1_Si_vc_relax` | 2 | GPU | 1 | 6/6 | 103.7 | 32.0 | 20 | 183,400→5,313 / — / — | $0.168 | baseline (cubic semiconductor) |
| 2 | `M1_T1_NaCl_vc_relax` | 2 | GPU | 0.75 | 6/6 | 394.6 | — | 51 | 1,103,189→14,779 / — / — | $0.942 | ionic insulator |
| 3 | `M1_T1_Cu_vc_relax` | 1 | CPU | 1 | 6/6 | 904.0 | — | 59 | 1,410,060→16,941 / — / — | $1.196 | fcc metal — needs smearing |
| 4 | `M1_T1_SiO2_alpha_quartz_vc_relax` | 9 | CPU | — | — | 5400.0 | — | 36 | 675,630→8,529 / — / — | $0.575 | trigonal, P3₂21 chirality · solver did not produce result.json |
| 5 | `M2_T1_LiFePO4_olivine_vc_relax` | 28 | CPU | — | — | 5400.0 | — | 24 | 373,192→8,744 / — / — | $0.334 | M2 tier, 28 atoms, Fe magnetic (PBE non-magnetic OK) · solver did not produce result.json |

## Aggregate

- Pass rate (score≥0.99 ∧ audit 6/6): **2/5**
- Total LLM spend (claude-haiku-4-5-20251001): **$2.31**
- Total solver wall clock (sequential): **23.4 min**
- Mean turns per run: 43.3

## Audit-flag breakdown

| Task | qe_invoked | job_done | paths_in_workdir | files_nonempty | qe_wallclock_ok | no_network_access |
|---|:---:|:---:|:---:|:---:|:---:|:---:|
| `M1_T1_Si_vc_relax/gpu` | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| `M1_T1_NaCl_vc_relax/gpu` | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| `M1_T1_Cu_vc_relax/cpu` | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| `M1_T1_SiO2_alpha_quartz_vc_relax/cpu` | — | — | — | — | — | — |
| `M2_T1_LiFePO4_olivine_vc_relax/cpu` | — | — | — | — | — | — |

## Findings

### Headline result
**2 of 5 tasks score 1.0 with all 6 audit flags green.** A third (NaCl GPU)
audits clean and produces numerically correct lattice constants but loses
0.25 points on space-group classification — see "soft failure" below.
The two M2-leaning tasks (SiO2 α-quartz, LiFePO4 olivine) timed out at
the 90 min harness cap.

Aggregate above counts only the 3 runs that submitted; SiO2 and LiFePO4
each ate 5400 s of harness wall on top of that, so the *real* sequential
wall for the 5-run benchmark was ~3 h.

### Per-task notes

**M1_T1_Si_vc_relax / GPU — 1.0** (baseline). Cleanest run: 20 turns,
~$0.17, 32 s of QE WALL inside the 104 s solver budget. GPU acceleration
is real — PWSCF reports `GPU acceleration is ACTIVE` in the .out file.

**M1_T1_NaCl_vc_relax / GPU — 0.75 (soft failure).** Lattice constants
correct to ≤0.03 % (a=b≈4.030 Å, c=4.0312 Å), angles within 0.01 ° of 60 °.
The relaxed cell is *visually* cubic Fm-3m but `pymatgen.SpacegroupAnalyzer`
classifies it as trigonal R-3m (#166) because c and γ deviate just enough.
Per-field weights: 6/7 fields score 1.0 (a,b,c × 0.2 + α,β,γ × 0.05) plus
sg × 0.25 = 0.0 → total 0.75. Diagnostic root cause: the LLM picked a
loose `press_conv_thr` (≥0.5 kbar) which lets the off-diagonal stress
reach a non-zero relaxed minimum. Fix is in the system prompt
(recommend ≤0.1 kbar for cubic targets).

**M1_T1_Cu_vc_relax / CPU — 1.0.** fcc metal, 1-atom primitive,
needs smearing. 59 turns, the longest of the successful runs (904 s).
Most of the wall is iterative SCF and BFGS steps; haiku spent ~$1.20.
No audit issues.

**M1_T1_SiO2_alpha_quartz_vc_relax / CPU — TIMEOUT.** 36 turns spent;
the LLM repeatedly wrote `runs/sio2_vc_relax_v{1,2,3}.in`, launched
`pw.x ... &` (background), polled output, decided no progress was
visible, killed the job, and rewrote. Self-inflicted livelock. The
9-atom trigonal cell is *not* prohibitively expensive in QE (single
relaxation should finish in 5-15 min on this CPU), but the LLM's
`&`-and-poll pattern wasted >80 % of the 90 min budget on
orchestration, not science.

**M2_T1_LiFePO4_olivine_vc_relax / CPU — TIMEOUT.** Same orchestration
pattern. 24 turns; 373 K input tokens means each turn ate context
faster than new ones were generated. 28-atom orthorhombic + 3d-metal
pseudos make a single SCF expensive (5-15 min per cycle), and BFGS
needs multiple cycles, so even a *clean* run might brush against the
cap. Combined with the same `&` antipattern as SiO2, the ceiling held.

### Audit-flag observations
On the three runs that produced result.json, **all 6 audit flags pass
unanimously**. The bench's anti-cheat checks are not the bottleneck for
this agent — failures are higher in the stack (LLM strategy, convergence
parameters), not at the audit boundary.

### v2 recommendations (since landed in commit f92e707)
1. **System prompt: forbid `&` for pw.x.** Should reclaim 80 % of the
   wasted wall on hard tasks. ✅ implemented in v2; see benchmark_v2.md
2. **System prompt: tighten convergence defaults for cubic targets.**
   `press_conv_thr ≤ 0.1 kbar`, `forc_conv_thr ≤ 1e-4`. ✅ implemented in v2.
3. **Prompt caching.** NaCl burned 1.1 M input tokens across 51 turns;
   Anthropic prompt cache (5-min TTL) cuts input cost ≈80 %. ✅ in v2.
4. **Sonnet 5/5 attempt:** Anthropic OAuth-tier rate-limit 429-ed sonnet
   at the start of this session. Deferred.

