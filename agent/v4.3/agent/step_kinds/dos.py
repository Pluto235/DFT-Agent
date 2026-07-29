"""dos step kind — density of states (nscf + dos.x)."""
from __future__ import annotations

from pathlib import Path
from textwrap import dedent

from agent.ledger import PlanStep
from agent.step_kinds import StepKindSpec

KIND = "dos"


def system_prompt_addon(step: PlanStep, main_workdir: Path, step_subdir: str,
                        upstream: dict[str, list[str]]) -> str:
    upstream_block = ""
    if upstream:
        lines = ["## Upstream SCF artifacts"]
        for dep_id, paths in upstream.items():
            lines.append(f"- from step `{dep_id}`:")
            for p in paths:
                lines.append(f"    - `{p}`")
            lines.append(f"  (charge density at `{dep_id}/out/<prefix>.save/`)")
        upstream_block = "\n".join(lines)
    outputs_block = "\n".join(
        f"  - `{k}` → `{p}`" for k, p in step.expected_outputs.items()
    )
    return dedent(f"""\
        ## STEP: {step.id} (kind=dos)

        **Goal**: {step.desc}

        cwd = main workdir (`{main_workdir}`). All files under `{step_subdir}/`.
        Pseudos at `./pseudos/`.

        {upstream_block}

        **Expected outputs**:
        {outputs_block}

        **DOS workflow (nscf + dos.x)**:
        1. **nscf on dense k-grid** (~2-3× SCF density): `calculation='nscf'`.
           Reuse the upstream SCF's save dir: set `outdir = '<scf_dep_id>/out'`
           and matching `prefix`. Write log to `{step_subdir}/nscf.out`.
        2. **dos.x post-process**: `dos.in` with the SCF `prefix`,
           `outdir = '<scf_dep_id>/out'`, `fildos` pointing at the expected
           dos_dat path (e.g. `{step_subdir}/dos.dat`). **DO NOT set
           `ngauss` or `degauss`** — let dos.x reuse the NSCF occupations
           method (tetrahedra for insulators, SCF smearing for metals).
           This is the well-converged choice; setting Gaussian explicitly
           was the old advice and hurts dos_curve F1 score by ~0.3.
    """)


def verifier_checklist(step: PlanStep) -> str:
    parts = [
        "## DOS verifier checklist", "",
        "Required:",
        "1. Each expected_output exists and is non-empty:",
    ]
    for k, p in step.expected_outputs.items():
        parts.append(f"   - `{k}` at `{p}`")
    parts.append("2. `*_out` log contains `JOB DONE.`")
    parts.append("3. `*_dat` data file: numeric columns or QE header line; ≥ 1024 B.")
    parts.append("4. No `Error in routine` lines.")
    return "\n".join(parts)


SPEC = StepKindSpec(
    kind=KIND,
    system_prompt_addon=system_prompt_addon,
    tool_allowlist=("bash", "write_file", "read_file", "submit", "read_experience"),
    verifier_checklist=verifier_checklist,
)
