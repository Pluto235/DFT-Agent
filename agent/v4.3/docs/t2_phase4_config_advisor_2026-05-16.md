# Phase 4 — config_advisor preflight + qe_input_lint rule extensions

**Date:** 2026-05-16
**Scope:** v4.1 only
**Status:** code landed; smoke pending

## Motivation

Phase 3 (vc_relax preflight) brought M1_T2_NaCl smoke from 0.0 → 0.75
by adding a relaxed-geometry step. Two **independent QE-config error
modes** remained:

1. **`occupations` mis-selection.** SCF + bands NSCF need `fixed` for
   semiconductors/insulators and `smearing` (Methfessel-Paxton / `mv`,
   `degauss=0.02`) for metals. The agent currently infers metal vs
   insulator from prompt hints like "calcium (fcc)" vs "sodium
   chloride (rocksalt)". Right on easy cases, wrong on harder ones.

2. **`ecutrho` ratio mis-selection.** GBRV USPP requires
   `ecutrho ≈ 10 × ecutwfc`. The agent — or `qe_tools.write_pw_input`'s
   default of `ecutrho=480, ecutwfc=60` (8× = NCPP convention) — can
   silently pick 8× or 4×, drifting band gap by 0.2–0.5 eV per 30 Ry
   ecutwfc gap (well past the ±0.1 eV bench tolerance).

This phase adds **deterministic detection + light LLM cross-check +
lint enforcement** for both, across **all pw.x calls** (vc-relax, SCF,
bands NSCF, DOS NSCF — not just T2).

## Design

### config_advisor subagent (`agent/subagents/config_advisor.py`)

Hybrid: deterministic classifier produces an initial `ConfigAdvice`;
the LLM gets the deterministic guess + raw prompt and emits final
advice. LLM is told to override **only on a clear physics reason**
(known semimetal, Mott insulator, etc.).

**Deterministic classifier rules** (`_deterministic_classify`):

1. Parse formula, structure name, pseudo family from prompt regex.
2. If any element in `STRONGLY_ELECTRONEGATIVE` (O, F, Cl, Br, I, N, S,
   Se, Te) → insulator/semiconductor.
3. Else if all elements in `METAL_ELEMENTS` → metal (covers pure
   metals + intermetallic alloys).
4. Else → insulator/semiconductor default (covers Si/Ge/covalent).

`METAL_ELEMENTS` (`agent/subagents/_metals.py`): hand-curated frozenset
of ~70 elements covering alkali, alkaline-earth, group 13/14/15
metals, transition metals (3d/4d/5d), lanthanides, common actinides.
Borderline cases documented (Bi semimetal in; Si/Ge/Sn-α
semiconductors out; As ambiguous, defaulted to insulator).

Sanity-check on 11 representative materials at write time: 11/11
classified correctly (NaCl, Ca, Si, Cu, CuO, Al2O3, LiFePO4, Fe, Mg,
AlN, Ag).

**LLM cross-check** (`run`): 1–3 turn LLM loop with `max_turns=4`.
Falls back to deterministic guess on any failure. Cost: ~$0.01–0.03
per task at deepseek-v4-pro rates.

**Output**: `ConfigAdvice` dataclass with `is_metal`, `occupations`,
`smearing`, `degauss`, `ecutrho_factor`, `source` (deterministic /
llm_override / fallback), `rationale`. Persisted to
`workdir/.config_advice.json` for the lint layer.

### qe_input_lint rule extensions (`agent/tools/qe_input_lint.py`)

**Rule C — ecutrho ratio for USPP pseudos** (auto-fix):
- Detect USPP via `pseudo_dir` path (contains "USPP" / "GBRV" / "PAW")
  or `ATOMIC_SPECIES` filenames.
- If `ecutrho / ecutwfc < 9` and pseudo is USPP → rewrite ecutrho to
  `round(ecutwfc * 10)`.
- Auto-correct (not warn-only) because the rule is physics-mandated by
  USPP convention; LLM has no good reason to override.

**Rule D — occupations vs advice** (warn-only):
- Reads `workdir/.config_advice.json` (walks up parent dirs).
- For SCF or bands NSCF: if input `occupations` ≠ `advice.occupations`
  → emit `WARN` block in lint diff.
- **Never auto-corrects** — the advice itself can be wrong on edge
  cases (Mott insulators with metal-element composition, etc.). The
  warning surfaces the disagreement; LLM keeps decision authority.
- Skipped for `calculation='vc-relax'` (occupations choice less
  critical for geometry-only).
- Skipped for `occupations='tetrahedra'` (DOS NSCF — bench-mandated
  regardless of metal/insulator).

### `qe_tools.write_pw_input` hardening

Defaults `ecutwfc=60.0`, `ecutrho=480.0`, `occupations="smearing"`
removed. All three are now `None` and raise `TypeError` with a
descriptive message pointing to the system prompt's
`## QE CONFIGURATION ADVISORY` block.

Rationale: silent defaults biased toward wrong (smearing + 8× = NCPP
convention) for the dominant case (USPP + semiconductor). A loud
`TypeError` forces the LLM to think about each value explicitly. The
system prompt's config_advisor block always tells it what to pass.

### Wire-up in `solve.py`

```
main()
  ├─ parse_required_keys
  ├─ LLMClient
  ├─ config_advisor.run() → ConfigAdvice
  │     ├─ advice_block injected into system prompt
  │     └─ persisted to workdir/.config_advice.json
  ├─ if is_bands_dos_task: vc_relax_subagent.run(config_advice_block=...)
  ├─ build_qe_system_prompt(prerelax_block, config_advice_block)
  └─ run_loop (main)
```

The vc_relax subagent's system prompt also gets the advice block, so
the relax step uses the right occupations for metal/insulator + the
right ecutrho ratio.

**Per-run kill switch:** `DFT_DISABLE_CONFIG_ADVISOR=1` (matches
`DFT_DISABLE_VC_RELAX_SUBAGENT` for emergency rollback).

## Files

| Path | Status |
|---|---|
| `agent/subagents/_metals.py` | NEW (~30 lines) |
| `agent/subagents/config_advisor.py` | NEW (~350 lines) |
| `agent/subagents/vc_relax.py` | `run()` + `build_vc_relax_system_prompt` gain `config_advice_block` kwarg |
| `agent/system_prompt.py` | `build_qe_system_prompt` gains `config_advice_block` kwarg |
| `agent/tools/qe_input_lint.py` | Rule C + Rule D added (+200 lines) |
| `qe_tools.py` | `write_pw_input` rejects implicit ecutwfc/ecutrho/occupations |
| `solve.py` | config_advisor spawn before vc_relax; persist advice JSON |
| `tests/test_config_advisor.py` | NEW — 48 tests |
| `tests/test_qe_input_lint.py` | EXTENDED — 9 new Rule C/D tests |
| `tests/test_qe_tools_bench_mode.py` | updated 3 call-sites to pass explicit ecutwfc/ecutrho/occupations |

## Tests

```
.venv/bin/pytest -q
....
205 passed in 0.23s
```

- 145 pre-existing v4.1 tests pass
- 48 new config_advisor tests pass
- 9 new lint Rule C/D tests pass
- 3 qe_tools tests updated to use explicit cutoffs

## A/B plan

```bash
# Smoke 3a — same NaCl case as Phase 3 smoke 2 (target ≥ 0.75 / no regression)
# Smoke 3b — Ca metal (the new failure mode this phase targets)
# Smoke 3c (optional) — AlN (where Phase 1 phase1 diagnose found the
#                       'occupations=fixed' on DOS NSCF bug; should now
#                       at least carry occupations=fixed for SCF correctly)
```

Pass criteria for 3a: `total_score >= 0.75` and `is_metal=False` in
the run's `.config_advice.json`.

Pass criteria for 3b: `is_metal=True` in `.config_advice.json`, the
SCF input has `occupations='smearing'` with `smearing='mv'` and
`degauss=0.02`, and any non-trivial `total_score > 0` (Ca is a metal
with complex bands; bench scoring may still be lossy).

## Risks

- **Metal table coverage**: Bi/Sb included as semimetals, Si/Ge/Sn out
  as semiconductors. Edge cases (carbon, As, gray-Sn) default to
  insulator and rely on LLM cross-check to override.
- **`qe_tools.write_pw_input` TypeError**: existing v4 tasks that
  relied on defaults will break. Acceptable because v4.1 system prompt
  always includes the advice block telling the LLM what to pass; first
  call may TypeError, LLM reads error, retries with explicit args. +1
  turn worst case.
- **Rule D is warn-only**: doesn't actually prevent a wrong-occupations
  SCF. Tradeoff: auto-correcting `fixed → smearing` would silently
  change semantics; warning lets the LLM acknowledge and override on
  legitimate Mott-insulator-style cases.
- **Cost**: config_advisor adds ~2k LLM tokens per task. Negligible
  next to main loop's 50k+ tokens.

## Out of scope

- `dos_e_fermi` deviation (Phase 3 smoke 2 was 2.86 vs GT 4.88,
  off by 2 eV). Likely SCF Fermi alignment in DOS NSCF k-mesh
  output. Phase 5.
- Touching v4 / v5 / v1.
- Auto-correcting occupations (Rule D stays warn-only by design).
