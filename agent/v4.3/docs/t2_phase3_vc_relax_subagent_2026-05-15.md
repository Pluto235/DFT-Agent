# T2 Phase 3 — vc_relax preflight subagent

**Date:** 2026-05-15
**Author:** v4.1 fork
**Scope:** v4.1 only (`/path/to/projects/solver/v4.1/`)

## Motivation

Working hypothesis: **v4 underperforms on T2 (`bands_dos`) because the
`initial_structure` shipped with each task isn't precise enough.** The
prompt is rendered from Materials Project entries and is *usually* PBE
relaxed, but the agent's GBRV+chosen-k-mesh combo can differ from MP's
relax conditions enough that a fresh in-bench vc_relax would land on a
slightly different cell — closer to "this agent's self-consistent
ground truth" — and therefore yield band gap / Fermi values closer to
the bench ground truth.

### Honest caveats — see `docs/t2_phase1_diagnose_2026-05-11.md`

Phase 1's diagnosis on AlN (the one detailed T2 failure case we have)
puts the 1.17 eV `dos_e_fermi` error on **`occupations='fixed'`** in
the DOS NSCF (it should be `'tetrahedra'`), **not on geometry drift**.
There is no new failure case in hand that demonstrates a geometry-only
T2 failure. So this change is a precautionary belt-and-suspenders, not
a root-cause fix.

The A/B plan in §A/B below is the test of whether keeping it is worth
the ~2–3× per-task token cost.

## Design

`v4.1/solve.py::main()` checks `is_bands_dos_task(required_keys)` —
true when the parsed submission keys contain `bands_data` or `dos_dat`.
When true (and `DFT_DISABLE_VC_RELAX_SUBAGENT` is not set), it spawns
the subagent at `v4.1/agent/subagents/vc_relax.py`:

```
v4.1/solve.py:main()
  ├─ parse_required_keys(prompt)
  ├─ LLMClient(...)
  ├─ if is_bands_dos_task(required_keys) and not DFT_DISABLE_VC_RELAX_SUBAGENT:
  │     ├─ vc_relax.run(client, prompt, workdir, pw_x_path, qe_backend,
  │     │              max_turns, max_output_tokens) -> RelaxResult
  │     ├─ if ok:    prerelax_block = render_prerelax_success(result)
  │     └─ else:     prerelax_block = render_prerelax_failure(result)
  ├─ build_qe_system_prompt(..., prerelax_block=prerelax_block)
  └─ run_loop (main)
```

The subagent uses the same `agent.loop.run_loop` + `BashTool` +
`LLMClient` as the main loop — only system prompt, user prompt, and
workdir differ. Key isolation:

- **Workdir**: `workdir/vc_relax/` — own bash.log, own transcript.json,
  own `out/`, own input files. The main loop's workdir stays
  bench-shape (transcript.json, bash.log, lint.log at the root).
- **Pseudos**: parent workdir's `./pseudos/` is referenced as
  `../pseudos/` from the subagent's cwd. No new symlink.
- **qe_input_lint**: same `BashTool` class, same pre-spawn hook —
  press_conv_thr / forc_conv_thr / etot_conv_thr auto-tightened on the
  subagent's vc-relax just like the main loop.
- **Same model**: the subagent reuses the parent `LLMClient`, so it
  runs the exact same model the user picked for the main task. Token
  cost is roughly additive.

### Submit contract

The subagent submits `task_outputs = {"relax_out_path": "relax.out"}`.
The driver in `vc_relax._validate_payload` then:

1. Resolves `relax_out_path` as subwork-relative OR workdir-relative
   (accepts either; subwork-relative is what the system prompt asks for).
2. Asserts the path is inside workdir, is a regular file, is ≥1024 B,
   and contains `JOB DONE.` in the last 64 KB (mirrors bench's
   `_audit_task_outputs`).
3. Parses the converged geometry from the `Begin final coordinates ...
   End final coordinates` block via the regex pair
   `_CELL_RE` / `_POSITIONS_RE`.
4. Builds a `RelaxResult` dataclass with the full CELL_PARAMETERS and
   ATOMIC_POSITIONS text blocks ready for verbatim injection.

The LLM is **not** asked to reformat the geometry into JSON — the
driver pulls it from QE's output, which is robust against LLM
formatting hiccups.

### Failure handling

Any of: subagent loop exception, `payload["incomplete"]`, missing
`relax_out_path`, file missing, file too small, no JOB DONE., no
"Begin final coordinates" block → `RelaxResult.ok = False`. The driver
falls back: main solver receives `render_prerelax_failure(result)`
which tells the main LLM "preflight failed, use the original
initial_structure as you normally would." Total run still produces
result.json; bench audit doesn't care which path produced the SCF.

### Per-run rollback

Set `DFT_DISABLE_VC_RELAX_SUBAGENT=1` in the spawner env (web backend
or harness) to skip the subagent entirely. v4.1 then degrades to v4
behavior with zero LLM cost overhead. Use for emergency A/B without
redeploying or unregistering.

## A/B plan

Run M1_T2 cases with BOTH agents on the same model. Suggested:

| Case | Why it's interesting |
|---|---|
| `M1_T2_NaCl_bands_dos` | Direct gap at Γ, cubic — simplest baseline |
| `M1_T2_AlN_bands_dos` | Phase 1's known failure case — was `occupations='fixed'`, not geometry. If v4.1 still misses, that's evidence geometry isn't the bottleneck |
| `M1_T2_InP_bands_dos` | Soft cell, MP relax is generally good — risk of v4.1 over-relaxing |
| `M1_T2_Rb_bands_dos` | Metallic, occupations='smearing' is mandatory anyway — sanity check that the subagent handles non-insulating prompts |

For each, record `total_score`, `band_gap_ev` deviation, `dos_e_fermi`
deviation, and total wall-clock. v4.1 wins iff the score delta exceeds
the wall-clock penalty by a usable margin.

## Files

| Path | Status |
|---|---|
| `agent/subagents/__init__.py` | NEW (empty package marker) |
| `agent/subagents/vc_relax.py` | NEW — subagent driver, prompt builders, parsers, render helpers (~370 lines) |
| `agent/system_prompt.py` | `prerelax_block` kwarg added; injects in §header |
| `solve.py` | `is_bands_dos_task` helper; subagent dispatch in `main()` |
| `tests/test_vc_relax_subagent.py` | NEW — 20 deterministic tests (no LLM) |

## Tests

`pytest -q tests/test_vc_relax_subagent.py` covers:

- `is_bands_dos_task` positive/negative
- `parse_final_geometry` happy path + missing block
- `_validate_payload` happy path + 6 failure modes (incomplete, missing
  key, file absent, too small, no JOB DONE., no final coords)
- `render_prerelax_{success,failure}` block content
- `_extract_initial_structure_block` regex on real T2 task_prompt.txt

Full suite: 145 passed (125 pre-existing v4 + 20 new). No regressions.

## Wallclock budget note

Bench T2 SCF + bands NSCF + DOS NSCF can already eat 60+ minutes. The
subagent's vc_relax adds another full pw.x convergence loop (~5–10
ionic steps × full SCF). On GPU mode (`-np 1` H100), this is usually
2–5 min for primitive cells. On CPU mode (`-np 4`), can be 10–20 min.
The web backend's per-run timeout may need a bump before deploying CPU
sweeps.
