"""vc_relax step kind — variable-cell relaxation (M1_T1 target)."""
from __future__ import annotations

from pathlib import Path
from textwrap import dedent

from agent.ledger import PlanStep
from agent.step_kinds import StepKindSpec

KIND = "vc_relax"


def system_prompt_addon(step: PlanStep, main_workdir: Path, step_subdir: str,
                        upstream: dict[str, list[str]]) -> str:
    upstream_block = ""
    if upstream:
        lines = ["## Upstream artifacts (already at workdir-relative paths)"]
        for dep_id, paths in upstream.items():
            lines.append(f"- from step `{dep_id}`:")
            for p in paths:
                lines.append(f"    - `{p}`")
        upstream_block = "\n".join(lines)
    outputs_block = "\n".join(
        f"  - `{k}` → `{p}`" for k, p in step.expected_outputs.items()
    )
    return dedent(f"""\
        ## STEP: {step.id} (kind=vc_relax)

        **Goal**: {step.desc}

        **Path discipline (v4.3)**:
        - cwd = main workdir (`{main_workdir}`).
        - Pseudos: `./pseudos/`
        - ALL step files under `{step_subdir}/`:
            - input: `{step_subdir}/vc-relax.in`
            - outdir: `{step_subdir}/out`
            - log: `{step_subdir}/vc-relax.out`

        {upstream_block}

        **Expected outputs**:
        {outputs_block}

        **vc_relax specifics (HARD requirements; qe_input_lint enforces)**:
        - `calculation = 'vc-relax'`
        - `press_conv_thr = 0.05`   (kbar; tighter than QE default 0.5)
        - `forc_conv_thr = 1.0d-4`  (Ry/Bohr)
        - `etot_conv_thr = 1.0d-5`  (Ry)
        - For cubic systems, the bench's SpacegroupAnalyzer is intolerant of
          drift; tight convergence avoids cubic → trigonal downgrades.
        - `outdir = '{step_subdir}/out'`
        - `pseudo_dir = './pseudos/'`

        Submit when the log contains `JOB DONE.` AND `bfgs converged in` (or
        equivalent) AND the final pressure is below `press_conv_thr`.
    """)


def verifier_checklist(step: PlanStep) -> str:
    paths_block = "\n".join(
        f"- `{k}` at `{p}`" for k, p in step.expected_outputs.items()
    )
    return dedent(f"""\
        ## vc_relax verifier checklist

        Required:
        1. Each expected_output exists at its workdir-relative path:
        {paths_block}
        2. `*_out` log contains `JOB DONE.` in the last 64 KB.
        3. Log contains `bfgs converged in` OR `End final coordinates`.
        4. NO `convergence NOT achieved` or `maximum number of steps has been reached`.

        Optional warnings (fail only if multiple violated):
        - Final stress ≤ 0.05 kbar (check the last `total   stress` block).
        - For a declared cubic system, final a, b, c equal within 0.01 Å.

        On failure, write a concrete diagnosis (missing marker, unconverged pressure, etc.).
    """)


SPEC = StepKindSpec(
    kind=KIND,
    system_prompt_addon=system_prompt_addon,
    tool_allowlist=("bash", "write_file", "read_file", "submit", "read_experience"),
    verifier_checklist=verifier_checklist,
)
