"""bands step kind — band structure along high-symmetry k-path.

Requires upstream SCF charge density (`<scf_step>/out/<prefix>.save/`).
"""
from __future__ import annotations

from pathlib import Path
from textwrap import dedent

from agent.ledger import PlanStep
from agent.step_kinds import StepKindSpec

KIND = "bands"


def system_prompt_addon(step: PlanStep, main_workdir: Path, step_subdir: str,
                        upstream: dict[str, list[str]]) -> str:
    upstream_block = ""
    if upstream:
        lines = ["## Upstream SCF artifacts (use these — do NOT re-run scf)"]
        for dep_id, paths in upstream.items():
            lines.append(f"- from step `{dep_id}`:")
            for p in paths:
                lines.append(f"    - `{p}`")
            lines.append(f"  (charge density typically at `{dep_id}/out/<prefix>.save/`)")
        upstream_block = "\n".join(lines)
    outputs_block = "\n".join(
        f"  - `{k}` → `{p}`" for k, p in step.expected_outputs.items()
    )
    return dedent(f"""\
        ## STEP: {step.id} (kind=bands)

        **Goal**: {step.desc}

        cwd = main workdir (`{main_workdir}`). All files under `{step_subdir}/`.
        Pseudos at `./pseudos/`.

        {upstream_block}

        **Expected outputs**:
        {outputs_block}

        **Bands workflow (two QE calls)**:
        1. **nscf with k-path**: `calculation = 'bands'`, `K_POINTS {{tpiba_b}}`
           listing each high-symmetry segment with point counts.
           `pseudo_dir = './pseudos/'`. The CHARGE DENSITY comes from the
           upstream SCF: set `outdir = '<scf_dep_id>/out'` to read from the
           upstream save directory directly (or copy/link it if you need
           a clean prefix). Write log to `{step_subdir}/bands.out`.
        2. **bands.x post-process**: `bands.in` with the SCF's `prefix` and
           `outdir = '<scf_dep_id>/out'`. Write `filband` to the path the
           expected_outputs declares (e.g. `{step_subdir}/bands.dat`).

        `*_dat` files are pure data (no JOB DONE expected); `*_out` logs MUST have JOB DONE.
    """)


def verifier_checklist(step: PlanStep) -> str:
    out_keys = [k for k in step.expected_outputs if k.endswith("_out")]
    dat_keys = [k for k in step.expected_outputs if k.endswith(("_data", "_dat"))]
    parts = [
        "## bands verifier checklist", "",
        "Required:",
        "1. Each expected_output exists, non-empty:",
    ]
    for k, p in step.expected_outputs.items():
        parts.append(f"   - `{k}` at `{p}`")
    parts.append("2. Log files (`*_out`) contain `JOB DONE.`:")
    for k in out_keys:
        parts.append(f"   - `{k}` → `{step.expected_outputs[k]}`")
    parts.append("3. Data files (`*_dat`/`*_data`) need not have JOB DONE but")
    parts.append("   must be ≥ 1024 B and look like ASCII columns:")
    for k in dat_keys:
        parts.append(f"   - `{k}` → `{step.expected_outputs[k]}`")
    return "\n".join(parts)


SPEC = StepKindSpec(
    kind=KIND,
    system_prompt_addon=system_prompt_addon,
    tool_allowlist=("bash", "write_file", "read_file", "submit", "read_experience"),
    verifier_checklist=verifier_checklist,
)
