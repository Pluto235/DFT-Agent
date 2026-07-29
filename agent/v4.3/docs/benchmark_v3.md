# v3 Benchmark — 5 vc_relax Tasks (claude-haiku-4-5-20251001)

Source data: `/tmp/bench-eval-v3-2026-05-10` · Solver commit `ec51753` · Apply commits `66db43a 6969928 ec51753`

v3 introduces two complementary defenses against the LLM-strategy-noise
failure path documented in `docs/cpu_vs_gpu_2026-05-10.md` (NaCl CPU
0.75 from haiku silently dropping the v2 prompt's recommended convergence
thresholds, ~25 % miss rate measured):

1. **Path A — bash-tool interception layer** (`agent/tools/qe_input_lint.py`).
   Before each `pw.x` invocation, the linter parses the input, detects
   vc-relax, and ensures `press_conv_thr`, `forc_conv_thr`, `etot_conv_thr`
   are at least as tight as defaults via `min(LLM_value, default)`.
   Idempotent. Patches surface as a `--- qe_input_lint ---` section in the
   bash tool result so the LLM learns the fix.
2. **Path B — system prompt strengthening**. §5 upgraded from
   recommendation to mandatory ("Every vc-relax input MUST set three
   tight thresholds…"); a new "canonical input template" few-shot block
   shows the three keys in their correct namelists.

A third change rolled in mid-v3: `fix(bash): only flag '&' as background
when followed by whitespace/EOL` (commit `ec51753`). The v2-polish
bg-detection guard was over-eager — it rejected `cat << 'EOF' / &CONTROL
/ ... / EOF` heredoc bodies because `&CONTROL` left a stray `&` after
operator stripping. Without this fix, haiku's first v3 attempt on Cu CPU
timed out after 50 wasted turns of `chr(38)` / `sed` / `perl` contortions
to write a pw.x input. See "Sidebar: bg-detection regression" below.

## Per-task results

| # | Task | atoms | Path | score | audit | solver wall (s) | QE wall (s) | turns | in / out / cache_r / cache_w | cost | notes |
|---|---|---:|---|---:|---|---:|---:|---:|---|---:|---|
| 1 | `M1_T1_Si_vc_relax` | 2 | GPU | 1 | 6/6 | 142.6 | 56.0 | 18 | 32,025→6,790 / 214,707 / 20,348 | $0.090 | baseline (cubic semiconductor) |
| 2 | `M1_T1_NaCl_vc_relax` | 2 | GPU | 1 | 6/6 | 179.9 | — | 17 | 30,113→6,082 / 184,415 / 20,024 | $0.083 | ionic insulator (target failure case) |
| 3 | `M1_T1_Cu_vc_relax` | 1 | CPU | 1 | 6/6 | 1013.4 | — | 29 | 62,132→12,747 / 497,277 / 42,074 | $0.183 | fcc metal — needs smearing |
| 4 | `M1_T1_SiO2_alpha_quartz_vc_relax` | 9 | CPU | 1 | 6/6 | 2990.1 | — | 20 | 45,721→9,382 / 246,447 / 32,301 | $0.126 | trigonal, P3₂21 chirality |
| 5 | `M2_T1_LiFePO4_olivine_vc_relax` | 28 | CPU | — | — | 5400.0 | — | 20 | 62,423→8,315 / 185,454 / 65,898 | $0.164 | M2 tier, 28 atoms · solver did not produce result.json (timeout) |

## Aggregate

- Pass rate (score ≥ 0.99 ∧ audit 6/6): **4/5**
- Total LLM spend (haiku-4-5): **$0.48**
- Total solver wall clock (sequential): **72.1 min**
- Mean turns per run: **21.0**

## Audit-flag breakdown

| Task | qe_invoked | job_done | paths_in_workdir | files_nonempty | qe_wallclock_ok | no_network_access |
|---|:---:|:---:|:---:|:---:|:---:|:---:|
| `M1_T1_Si_vc_relax/gpu` | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| `M1_T1_NaCl_vc_relax/gpu` | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| `M1_T1_Cu_vc_relax/cpu` | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| `M1_T1_SiO2_alpha_quartz_vc_relax/cpu` | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| `M2_T1_LiFePO4_olivine_vc_relax/cpu` | — | — | — | — | — | — |

## v1 → v2 → v3 progression

| Metric | v1 | v2 | v3 | Δ v2→v3 |
|---|---:|---:|---:|---:|
| Pass rate | 2/5 | 4/5 | **4/5** | 0 |
| Mean turns / run | 43.3 | 30.2 | **21.0** | **−30 %** |
| Total cost (haiku) | $2.31 | $0.71 | **$0.48** | **−32 %** |
| Total wall clock | 23.4 min¹ | 78.6 min | 72.1 min | −8 % |
| Si GPU wall | 103.7 s | 145.2 s | 142.6 s | −2 % |
| NaCl GPU wall | 394.6 s | 271.0 s | **179.9 s** | **−34 %** |
| NaCl GPU score | **0.75** | 1.0 | 1.0 | (already fixed in v2) |
| Cu CPU wall | 904.0 s | 1618.0 s | **1013.4 s** | **−37 %** |
| SiO2 CPU wall | timeout | 2683.9 s | 2990.1 s | +11 % |
| LiFePO4 CPU wall | timeout | timeout | timeout | (M2-on-CPU is backend-bound; orthogonal to v3) |

¹ v1 wall was lower because 3/5 tasks bailed early at the 1 h harness cap
or hit the input-file livelock — the wall clock is misleading on a failure-
heavy run.

### Where v3 wins

- **Cost / turn count**: −30 % on both. The canonical input template
  (Path B's few-shot block) gives haiku a copy-pasteable starting point;
  fewer self-correction loops needed.
- **NaCl GPU wall −34 %**: same root cause — haiku writes the input
  correctly on first try instead of iterating to find tight thresholds.
- **Cu CPU wall −37 %**: directly attributable to the bg-detection regex
  fix (`ec51753`). Without that fix the Cu retry would have timed out
  again — observed in the first v3 attempt earlier the same day.
- **SiO2 wall +11 %**: noise. SiO2 wall is dominated by QE compute on
  9-atom α-quartz; LLM-side gains are masked by SCF-cost variance run
  to run.

### What did not improve

- **Pass rate stays 4/5**. LiFePO4 CPU is still the M2 outlier. Its
  failure mode is *not* LLM-strategy noise — it is genuinely too slow
  on CPU (28 atoms, Fe pseudo, 5+ BFGS cycles × 8-15 min/SCF). v2
  Sonnet validation (`docs/sonnet_validation_2026-05-10.md`) showed the
  same task completes 1.0 in 23 min on **GPU**. v3 has nothing to add
  here; the v4 lever is "default M2_T1+ to GPU".

## Path A vs Path B — which one carried the win?

**lint.log analysis across all 5 runs**: every run shows `patched=False`
on every `pw.x` invocation. The LLM wrote correct convergence thresholds
unprompted on every input file in this benchmark.

This means **Path B (prompt strengthening + canonical few-shot) was the
primary lever** for haiku in this run. Path A (bash-tool hard-inject) did
not have to fire — it is the **safety net**, not the workhorse.

That does not make Path A redundant:
- The cpu_vs_gpu experiment (haiku, v2) measured **25 % miss rate** on
  the same rule (1 of 4 runs dropped thresholds → 0.75 score).
  v3 Path A would catch that miss deterministically.
- Sonnet ran on v3 untested in this round but the
  `sonnet_validation_2026-05-10.md` data showed sonnet *also* sometimes
  ignores the threshold recommendation (Si GPU used QE default 0.5).
  Path A protects sonnet too without retraining the prompt.
- Path A's diff also surfaces in tool result, providing in-context
  feedback that helps the LLM generalise even when it's the one writing
  the input.

The conclusion: **Path B is doing the work, Path A is the insurance**.
Both kept.

## Sidebar: bg-detection regression

The first v3 attempt (started 06:20, killed 07:42) saw Cu CPU time out
at 1800 s. Investigation found the LLM had spent 50 turns trying to
write the pw.x input file, including:

- `cat > vc_relax.in << 'EOF' / &CONTROL / ...` → rejected
- `echo "&CONTROL"` → rejected
- `printf '%s' '&CONTROL'` → rejected
- `'ZIONS'.replace('Z', chr(38))` Python tricks
- `perl -pi -e 's/NAMP1/&/g'` substitution chains

Root cause: `agent/tools/bash.py:_is_background()` from commit `66db43a`
flagged any `&` left after stripping legitimate operators (`&&`, `2>&1`,
`&>`). `&CONTROL` inside heredoc bodies left an `&` followed by `C`,
which is text — not a shell-level backgrounding token. The fix
(`ec51753`):

```python
_BG_AMP_RE = re.compile(r"&(?=[\s;|]|$)", re.MULTILINE)
```

requires `&` to be followed by whitespace, `;`, `|`, or end-of-line for
a bg flag. Pinned by `tests/test_bash_bg_detection.py` with 18 cases
covering sequence/pipe/redirect operators, heredoc bodies, quoted
strings, true trailing `&` forms, and the `nohup`/`disown`/`setsid`
keyword set.

Without this fix the v3 prompt's expanded few-shot template made the
problem *worse* — the canonical input now reaches haiku earlier in the
search graph, so it tries the legitimate pattern first and gets
rejected before it falls back to anything tedious.

## Known issues / v4 candidates

1. **LiFePO4 CPU timeout**: same as v2. Backend issue, not LLM. Lever:
   default M2_T1+ tasks to the GPU wrapper (the v2 `sonnet_validation`
   already validated 28-atom LiFePO4 GPU finishing in 23 min).
2. **`_detect_cubic` false-negatives on FCC primitive cells**: lint.log
   shows `cubic=False` for Si (sg 227), NaCl (sg 225), Cu (sg 225) — all
   FCC primitives with 60° angles, all genuinely cubic. The detector
   currently checks for ~90° only and treats `ibrav=2` (FCC) plus 60°
   primitive form as "false". The flag is logged-only and non-gating, so
   v3 results are unaffected; but any v4 logic that branches on
   `cubic_flag` (e.g., per-symmetry threshold sets) would need this
   widened. Fix: also accept `ibrav ∈ {1,2,3}` directly *or* angles
   ~60°/120° on free-cell forms.
3. **Tool result cache break on patch-diff insertion**: when Path A
   fires, the patch-diff appearing in `--- qe_input_lint ---` breaks
   prompt-cache continuity for the rest of that turn. Negligible at
   the v3 0/5 fire-rate but could be 1-2 ¢/run on Path-A-heavy
   workloads.
4. **Per-symmetry threshold sets**: v3 uses one tight set for all
   vc-relax. Less-symmetric systems probably tolerate looser thresholds
   without losing the spacegroup score, which would save 1-2 BFGS steps
   per run on M1 SiO2-flavoured tasks.

## Verification ledger

- [x] Unit tests: 70/70 green (`pytest tests/`)
- [x] NaCl GPU 1.0 / 6/6 audit (target: confirm v3 hard-inject path
      doesn't regress the v2 fix)
- [x] No regression on Si / Cu / SiO2 (all three 1.0 / 6/6)
- [x] LiFePO4 CPU timeout matches v2 (M2 backend issue, out of v3 scope)
- [x] `--- qe_input_lint ---` section logic landed and tested in
      `tests/test_qe_input_lint.py::test_intercept_bash_writes_log`
- [x] bg-detection fix (`ec51753`) tested in
      `tests/test_bash_bg_detection.py::test_is_background_truthtable`
      (18 cases)
- [x] Commit on dev: `feat(v3): hard-inject vc-relax convergence
      thresholds + strengthen prompt` (`6969928`) followed by
      `fix(bash): only flag '&' as background when followed by
      whitespace/EOL` (`ec51753`)
