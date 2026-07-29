# Sonnet validation — 3 GPU runs (post-v2)

OAuth tier 429-blocks sonnet/opus on this account; the runs below used a
**raw `ANTHROPIC_API_KEY`** (independent quota pool) the operator
provisioned for this experiment. Solver code unchanged from v2 commit
`08df1f4`. All 3 runs on the GPU wrapper (qe-7.3.1 NGC image).

## Results

| Run | Score | Audit | Solver wall | Turns | Cost (sonnet-4-6) |
|---|---:|---|---:|---:|---:|
| `M1_T1_Si_vc_relax` / GPU | **1.0** | 6/6 | 143 s | 12 | $0.161 |
| `M1_T1_NaCl_vc_relax` / GPU | **1.0** | 6/6 | 240 s | 11 | $0.164 |
| `M2_T1_LiFePO4_olivine_vc_relax` / GPU | **1.0** | 6/6 | 1408 s (23 min) | 18 | $0.312 |

Total spend: **$0.64**. Total wall: **30 min**.

## Why this matters

The single open issue at end of v2 was M2_T1_LiFePO4 — 28-atom
orthorhombic olivine with Fe pseudo, both v1 (CPU) and v2 (CPU)
attempts hit the harness 90 min cap because each SCF cycle on CPU
takes 8-15 min and BFGS needs 5+ such cycles. Recommended fix path
in `docs/benchmark_v1.md` was "run M2 tasks on GPU wrapper + try sonnet".

This validation closes that gap: **sonnet + GPU lifts LiFePO4 from
timeout to clean 1.0 in 23 min**. Extracted lattice constants
(a=4.672, b=5.789, c=9.882 Å) match the bench's ground truth within
0.15 %; orthorhombic Pnma (sg 62) preserved.

## Comparison: haiku v2 vs sonnet (GPU only)

| Task | haiku v2 turns / wall / cost | sonnet turns / wall / cost |
|---|---|---|
| Si | 26 / 145 s / $0.118 | **12** / 143 s / $0.161 |
| NaCl | 32 / 271 s / $0.193 | **11** / 240 s / $0.164 |
| LiFePO4 | timeout | **18** / 1408 s / $0.312 |

Sonnet uses ~50 % fewer turns. On Si it costs ~35 % more (small
overhead), on NaCl it costs ~15 % less (fewer turns + cache offset
the per-token rate), on LiFePO4 it solves what haiku couldn't.
For M1 tier sonnet ≈ haiku $-wise; for M2 tier sonnet is the only
one that lands.

## Operator notes

- The OAuth `CLAUDE_CODE_OAUTH_TOKEN` shipped via Pro/Team
  subscription only unlocks haiku on the Messages API path (sonnet
  and opus are routed to a separate, much tighter rate-limit pool).
- A raw `ANTHROPIC_API_KEY` from console.anthropic.com bypasses that
  pool entirely (independent billing). Solver auto-detects which
  to use: `agent/llm.py` prefers OAuth if present, falls through to
  ANTHROPIC_API_KEY otherwise; for sonnet runs unset OAuth before
  invoking.

## Convergence parameter observations

Sonnet sometimes does NOT apply the v2 system prompt's tight cubic
convergence preset (`press_conv_thr=0.05`) — the Si run used QE's
loose default `0.5` — yet still converged to 9-decimal-place cubic
symmetry (sg 227). This contrasts with haiku which needs the explicit
preset to land cubic on NaCl. Hypothesis: sonnet's better SCF intuition
picks BFGS step lengths that converge to higher-symmetry minima
naturally, where haiku is more sensitive to threshold settings.

The v3 lever ("auto-augment any vc-relax input missing the cubic preset")
remains useful for haiku-default deployments but is less critical
for sonnet.
