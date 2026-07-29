"""collect step kind — pure post-processing: rename, copy, merge data files."""
from __future__ import annotations

from pathlib import Path
from textwrap import dedent

from agent.ledger import PlanStep
from agent.step_kinds import StepKindSpec

KIND = "collect"


def system_prompt_addon(step: PlanStep, main_workdir: Path, step_subdir: str,
                        upstream: dict[str, list[str]]) -> str:
    upstream_block = ""
    if upstream:
        lines = ["## Upstream artifacts (read-only)"]
        for dep_id, paths in upstream.items():
            lines.append(f"- from step `{dep_id}`:")
            for p in paths:
                lines.append(f"    - `{p}`")
        upstream_block = "\n".join(lines)
    outputs_block = "\n".join(
        f"  - `{k}` → `{p}`" for k, p in step.expected_outputs.items()
    )
    return dedent(f"""\
        ## STEP: {step.id} (kind=collect)

        **Goal**: {step.desc}

        cwd = main workdir (`{main_workdir}`). All files under `{step_subdir}/`.

        {upstream_block}

        **Expected outputs**:
        {outputs_block}

        **Collect rules**:
        - Post-processing only — file rename / copy / data aggregation.
        - NO pw.x / bands.x / dos.x calls.
        - Read upstream files at their workdir-relative paths; write outputs
          under `{step_subdir}/`. A `cp` or symlink in bash usually suffices.

        Submit when every expected_output exists at its declared path with non-zero size.
    """)


def verifier_checklist(step: PlanStep) -> str:
    paths_block = "\n".join(
        f"- `{k}` at `{p}`" for k, p in step.expected_outputs.items()
    )
    return dedent(f"""\
        ## collect verifier checklist

        Required:
        1. Each expected_output exists and is non-empty:
        {paths_block}
        2. If a `*_out` key is in the outputs, it should contain `JOB DONE.`
           (rare for collect steps).
        3. `*_dat`/`*_data` files: ≥ 1024 B and look numeric.
    """)


SPEC = StepKindSpec(
    kind=KIND,
    system_prompt_addon=system_prompt_addon,
    tool_allowlist=("bash", "write_file", "read_file", "submit", "read_experience"),
    verifier_checklist=verifier_checklist,
)
