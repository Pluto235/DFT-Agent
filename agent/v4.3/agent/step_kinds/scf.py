"""SCF step kind — self-consistent field calculation.

Pre-condition: structure given (initial or from upstream relax/vc_relax).
Post-condition: scf.out with JOB DONE, and out/<prefix>.save/ with charge density.

v4.3 path semantics: executor cwd = MAIN workdir. The step writes all files
under `<step_subdir>/` prefix (typically `<step_id>/`). Pseudo path stays
`./pseudos/` (from main workdir).
"""
from __future__ import annotations

from pathlib import Path
from textwrap import dedent

from agent.ledger import PlanStep
from agent.step_kinds import StepKindSpec

KIND = "scf"


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
        ## STEP: {step.id} (kind=scf)

        **Goal**: {step.desc}

        **Path discipline (v4.3)**:
        - Your bash cwd is the **MAIN workdir** (`{main_workdir}`).
        - Pseudos live at `./pseudos/` (use this exact path in your QE input).
        - ALL files for this step MUST be under `{step_subdir}/`. Examples:
            - input file: `{step_subdir}/scf.in`
            - outdir: `{step_subdir}/out`
            - log: `{step_subdir}/scf.out` (run pw.x with `> {step_subdir}/scf.out`)
        - Do NOT write anywhere else under workdir.

        {upstream_block}

        **Expected outputs** (workdir-relative paths the verifier will check):
        {outputs_block}

        **SCF specifics**:
        - `calculation = 'scf'`
        - `outdir = '{step_subdir}/out'`
        - `pseudo_dir = './pseudos/'`
        - Set `prefix` consistently; downstream bands/dos need `{step_subdir}/out/<prefix>.save`
        - Convergence: `conv_thr = 1.0d-8` (tight; downstream bands need it)
        - Use occupations / smearing / ecutrho from the CONFIG ADVISORY block in
          the base prompt above (matched to the material).

        Submit when the scf log contains `JOB DONE.` AND the
        `{step_subdir}/out/<prefix>.save/` directory has the charge density.
    """)


def verifier_checklist(step: PlanStep) -> str:
    paths_block = "\n".join(
        f"- `{k}` at `{p}`" for k, p in step.expected_outputs.items()
    )
    return dedent(f"""\
        ## SCF verifier checklist

        Required (failure on any → pass=False):
        1. Each expected_output file/dir exists at its workdir-relative path:
        {paths_block}
        2. The `*_out` log contains the literal string `JOB DONE.` near the end
           (last 64 KB). Read the tail to confirm.
        3. The log does NOT contain `convergence NOT achieved`.
        4. If a key references a `.save/` directory, list it and confirm
           it contains `charge-density.dat` (or `charge-density.hdf5`) of
           non-zero size.

        On failure, write a concrete diagnosis:
          - "scf_out file scf_1/scf.out missing or 0 bytes; pw.x likely crashed"
          - "JOB DONE. not found in tail of scf_1/scf.out; look for error msg"
          - "scf_1/out/si.save directory empty; charge density not written"
    """)


SPEC = StepKindSpec(
    kind=KIND,
    system_prompt_addon=system_prompt_addon,
    tool_allowlist=("bash", "write_file", "read_file", "submit", "read_experience"),
    verifier_checklist=verifier_checklist,
)
