"""relax step kind — fixed-cell ionic relaxation."""
from __future__ import annotations

from pathlib import Path
from textwrap import dedent

from agent.ledger import PlanStep
from agent.step_kinds import StepKindSpec

KIND = "relax"


def system_prompt_addon(step: PlanStep, main_workdir: Path, step_subdir: str,
                        upstream: dict[str, list[str]]) -> str:
    upstream_block = ""
    if upstream:
        lines = ["## Upstream artifacts (workdir-relative)"]
        for dep_id, paths in upstream.items():
            lines.append(f"- from step `{dep_id}`:")
            for p in paths:
                lines.append(f"    - `{p}`")
        upstream_block = "\n".join(lines)
    outputs_block = "\n".join(
        f"  - `{k}` → `{p}`" for k, p in step.expected_outputs.items()
    )
    return dedent(f"""\
        ## STEP: {step.id} (kind=relax)

        **Goal**: {step.desc}

        cwd = main workdir (`{main_workdir}`). All files under `{step_subdir}/`.
        Pseudos at `./pseudos/`.

        {upstream_block}

        **Expected outputs**:
        {outputs_block}

        **relax specifics**:
        - `calculation = 'relax'`  (NOT 'vc-relax' — cell stays fixed)
        - `forc_conv_thr = 1.0d-4`, `etot_conv_thr = 1.0d-5`
        - `outdir = '{step_subdir}/out'`, `pseudo_dir = './pseudos/'`

        Submit when `JOB DONE.` AND `bfgs converged in` both appear in the log.
    """)


def verifier_checklist(step: PlanStep) -> str:
    paths_block = "\n".join(
        f"- `{k}` at `{p}`" for k, p in step.expected_outputs.items()
    )
    return dedent(f"""\
        ## relax verifier checklist

        Required:
        1. Each expected_output exists, ≥ 1024 B:
        {paths_block}
        2. Log contains `JOB DONE.`
        3. Log contains `bfgs converged in` OR `End final coordinates`.
        4. NO `convergence NOT achieved` / `maximum number of steps has been reached`.
    """)


SPEC = StepKindSpec(
    kind=KIND,
    system_prompt_addon=system_prompt_addon,
    tool_allowlist=("bash", "write_file", "read_file", "submit", "read_experience"),
    verifier_checklist=verifier_checklist,
)
