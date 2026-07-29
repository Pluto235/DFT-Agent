"""T2 vc_relax preflight subagent.

Spawned by solve.py when parse_required_keys() detects a bands_dos task
(`bands_data` or `dos_dat` in required keys). The subagent runs a
vc_relax on the initial structure in its own workdir/vc_relax/ subdir,
then submits `relax_out_path`. The driver here parses the resulting
QE .out file to extract the converged CELL_PARAMETERS + ATOMIC_POSITIONS
blocks. Main solver loop then receives a `prerelax_block` to inject at
the top of its system prompt, instructing it to use the relaxed geometry
for the subsequent SCF + bands NSCF + DOS NSCF.

Failure modes (any -> RelaxResult.ok=False, main loop falls back to the
original initial_structure):
  - subagent loop raised an exception
  - payload["incomplete"] truthy
  - missing relax_out_path
  - relax_out_path file missing / no "JOB DONE." / no "Begin final coordinates"
"""
from __future__ import annotations

import re
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from textwrap import dedent

from ..llm import LLMClient
from ..loop import run_loop
from ..tools.bash import BashTool


@dataclass
class RelaxResult:
    ok: bool
    relaxed_cell: str = ""          # full "CELL_PARAMETERS angstrom\n  ...\n" block
    relaxed_positions: str = ""     # full "ATOMIC_POSITIONS crystal\n  ...\n" block
    relax_out_path: str | None = None  # workdir-relative path
    notes: str = ""
    failure_reason: str | None = None


# ---------------------------------------------------------------------------
# Prompt builders
# ---------------------------------------------------------------------------

def build_vc_relax_system_prompt(*, workdir: str, pw_x_path: str,
                                 qe_backend: str = "gpu",
                                 config_advice_block: str = "") -> str:
    """Focused subagent system prompt — vc_relax only, no SCF / bands / DOS.

    Mirrors the canonical vc-relax template from agent/system_prompt.py:205-276
    so qe_input_lint thresholds stay honored. Intentionally short (~150 lines)
    to keep subagent context tight.

    config_advice_block is prepended above the workflow guidance so the
    relax step uses the right occupations + ecutrho for this material.
    """
    np_default = 4 if qe_backend == "cpu" else 1
    mpirun = f"mpirun --allow-run-as-root -np {np_default}"

    if qe_backend == "cpu":
        parallelism = dedent("""\
            ## CPU parallelism
            pw.x is the native CPU build. Use `-np 4` for small primitive
            cells (≤10 atoms — what T2 normally is). The wrapper handles
            -np transparently.
            """)
    else:
        parallelism = dedent("""\
            ## GPU parallelism
            pw.x is a podman+CDI wrapper around GPU QE 7.4. Always use
            `-np 1`. If a run exits in <5s without JOB DONE., retry the
            same command — that's a transient CDI init race, not a real
            failure. NEVER fall back to a CPU pw.x path.
            """)

    return dedent(f"""\
        You are a Quantum ESPRESSO vc_relax preflight subagent.

        {config_advice_block}

        Your ONLY job: run vc_relax on the structure given in the user
        prompt, get a converged relaxation (final pressure under
        press_conv_thr, max force under forc_conv_thr), then submit the
        path to your vc_relax output file via the `submit` tool.

        DO NOT run SCF / bands / DOS / dos.x / bands.x. Those belong to
        the main agent that comes after you. You produce the relaxed
        geometry and stop.

        ## Hard constraints
        - pw.x at: {pw_x_path}
        - cwd is already your private subdirectory; write files here.
        - No network — no curl, wget, pip, conda, git clone, etc.
        - vc-relax convergence thresholds (qe_input_lint enforces these
          automatically — do NOT loosen them):
            press_conv_thr = 0.05    ! kbar
            forc_conv_thr  = 1.0d-4  ! Ry/Bohr
            etot_conv_thr  = 1.0d-5  ! Ry
        - Do NOT background pw.x with `&`, `nohup`, `disown`, or `setsid`.
        - `cd <dir>` persists only on its own line, not chained with `&&`.

        {parallelism}

        ## vc-relax input template (start from this)

        ```fortran
        &CONTROL
          calculation   = 'vc-relax'
          prefix        = 'relax'
          outdir        = './out'
          pseudo_dir    = './pseudos/'
          tprnfor       = .true.
          tstress       = .true.
          forc_conv_thr = 1.0d-4
          etot_conv_thr = 1.0d-5
          nstep         = 100
        /
        &SYSTEM
          ibrav       = 0
          nat         = <fill>
          ntyp        = <fill>
          ecutwfc     = 50          ! Ry; 40-60 typical for PBE+USPP
          ecutrho     = 400         ! Ry; 8× ecutwfc for USPP
          occupations = 'fixed'     ! 'smearing' only for metals
        /
        &ELECTRONS
          conv_thr     = 1.0d-8
          mixing_beta  = 0.7
        /
        &IONS
          ion_dynamics = 'bfgs'
        /
        &CELL
          cell_dynamics  = 'bfgs'
          press_conv_thr = 0.05
        /
        ATOMIC_SPECIES
          <Symbol> <mass> <pseudo.upf>
        ATOMIC_POSITIONS crystal
          ...
        CELL_PARAMETERS angstrom
          ...
        K_POINTS automatic
          6 6 6 0 0 0
        ```

        Pseudopotential files are already symlinked in the parent
        workdir under `pseudos/`. From your cwd they're at `../pseudos/`.
        Use `pseudo_dir = '../pseudos/'`.

        ## Pseudopotential lookup
        Read the user prompt for the GBRV pseudo filenames (e.g.
        "Si: Si.upf"). Use those exact filenames.

        ## k-mesh
        Choose 6×6×6 for cubic/orthorhombic primitive cells, 4×4×4 for
        larger cells; this is a relax, not the production SCF k-mesh.

        ## Run command
        ```
        {mpirun} pw.x -in relax.in > relax.out 2>&1
        ```
        Then verify with `grep "JOB DONE." relax.out` (must be present).

        ## On crash / timeout: warm-restart, do NOT `rm -rf out` (FIX-27)

        If the previous pw.x crashed (vcsmd error, SCF non-convergence) or
        was timed out by the bash watchdog, do **NOT** wipe the `out/`
        directory between retries. `out/relax.save/` holds the
        wavefunctions + BFGS history from the partial run — preserving it
        lets pw.x resume instead of restarting from initial guess.

        Restart procedure:

        1. **KEEP** `out/` and `out/relax.save/` intact. Do not `rm -rf`.
        2. In your relax.in's `&ELECTRONS` namelist, add:
           ```
           startingwfc = 'file'
           startingpot = 'file'
           ```
        3. Re-run the SAME command: `{mpirun} pw.x -in relax.in > relax.out 2>&1`
           — pw.x reads `out/relax.save/` and resumes from the last
           converged ionic step + the last SCF iteration's wavefunctions.

        Why this matters: on a 16-atom Ti-oxide cell vc_relax typically
        runs ~2 h to BFGS step ~20. Each `rm -rf out` between retries
        throws that ~2 h away — you'll never make it past BFGS step ~5
        within the budget. Observed 5-24 Ti3O5: agent did 3× `rm -rf out`
        retries and never converged in 8 h.

        ONLY exception: if `out/relax.save/charge-density.dat` is
        corrupted (relax.out says "cannot read"), `rm -rf out` + start
        fresh is appropriate — but only after seeing that explicit error.

        ## Submission

        Call submit ONCE with:
        ```json
        {{
          "task_outputs": {{
            "relax_out_path": "relax.out"
          }},
          "notes": "converged in N ionic steps"
        }}
        ```
        The path is relative to your cwd (which is workdir/vc_relax/).
        DO NOT include relaxed_cell / relaxed_positions yourself — the
        driver parses the converged geometry from your relax.out directly.

        If the relax fails to converge after nstep=100 or pw.x errors
        out, call submit with `incomplete=true` and a `reason` string.
        The main agent will then fall back to the unrelaxed initial
        structure.
        """)


def _extract_initial_structure_block(prompt_text: str) -> str:
    """Pull out everything from 'Initial structure' header through the
    pseudopotentials block — what the subagent needs to write its input."""
    m = re.search(
        r"(Initial structure.*?)(?:Execution hint:|=== Submission Instructions ===|=== Network Policy ===)",
        prompt_text,
        re.DOTALL,
    )
    if m:
        return m.group(1).rstrip()
    # Fallback: return the whole prompt; the subagent will figure it out.
    return prompt_text


def build_vc_relax_user_prompt(original_prompt: str) -> str:
    """Subagent's user message: focused instructions + structure + pseudos
    extracted from the original task prompt."""
    structure_block = _extract_initial_structure_block(original_prompt)
    return dedent(f"""\
        Run vc_relax (variable-cell structural relaxation) on the
        structure below, then submit only the path to your relax.out
        file. Do NOT run SCF, bands, or DOS — those follow in a
        separate stage.

        Mandatory convergence thresholds (qe_input_lint auto-tightens
        if you loosen them):
          press_conv_thr = 0.05
          forc_conv_thr  = 1.0d-4
          etot_conv_thr  = 1.0d-5

        Pseudopotential files are symlinked at `../pseudos/` from your
        cwd (use `pseudo_dir = '../pseudos/'` in the &CONTROL namelist).

        ===== Structure to relax =====

        {structure_block}

        ===== End structure =====

        When relax.out contains "JOB DONE." and "Begin final
        coordinates", call submit with task_outputs={{"relax_out_path":
        "relax.out"}} and a brief note on convergence.
        """)


# ---------------------------------------------------------------------------
# QE output parser
# ---------------------------------------------------------------------------

# vc-relax converged geometry sits between these two markers in pw.x .out:
#
#   Begin final coordinates
#        new unit-cell volume = ...
#   CELL_PARAMETERS (angstrom)
#      ...
#   ATOMIC_POSITIONS (crystal)
#      ...
#   End final coordinates
#
# Both blocks always appear together when the BFGS algorithm reports
# convergence, including when calculation='vc-relax' completes the final
# SCF on the relaxed cell.

_FINAL_BLOCK_RE = re.compile(
    r"Begin final coordinates(.*?)End final coordinates",
    re.DOTALL,
)
_CELL_RE = re.compile(
    r"^(CELL_PARAMETERS\s*\([^)]+\)\s*\n(?:\s*[-+0-9.eE]+\s+[-+0-9.eE]+\s+[-+0-9.eE]+\s*\n){3})",
    re.MULTILINE,
)
_POSITIONS_RE = re.compile(
    r"^(ATOMIC_POSITIONS\s*\([^)]+\)\s*\n(?:\s*\S+\s+[-+0-9.eE]+\s+[-+0-9.eE]+\s+[-+0-9.eE]+\s*\n)+)",
    re.MULTILINE,
)


def parse_final_geometry(out_text: str) -> tuple[str, str] | None:
    """Extract (CELL_PARAMETERS block, ATOMIC_POSITIONS block) from a
    converged vc-relax pw.x .out. Returns None if either block is missing."""
    block_m = _FINAL_BLOCK_RE.search(out_text)
    if not block_m:
        return None
    block = block_m.group(1)
    cell_m = _CELL_RE.search(block)
    pos_m = _POSITIONS_RE.search(block)
    if not (cell_m and pos_m):
        return None
    return cell_m.group(1).rstrip(), pos_m.group(1).rstrip()


def _validate_payload(payload: dict, workdir: Path, subwork: Path) -> RelaxResult:
    """Apply structural validation to subagent's submit payload + parse the
    relax.out file. Returns a populated RelaxResult."""
    if payload.get("incomplete"):
        return RelaxResult(
            ok=False,
            failure_reason=f"subagent declared incomplete: {payload.get('reason', 'no reason')}",
        )

    task_outputs = payload.get("task_outputs") or {}
    rel_path = task_outputs.get("relax_out_path")
    if not rel_path:
        return RelaxResult(
            ok=False,
            failure_reason=f"subagent submit missing 'relax_out_path' in task_outputs (got keys: {list(task_outputs)})",
        )

    # Accept either workdir-relative or subwork-relative path. Try both.
    candidates = []
    p = Path(rel_path)
    if p.is_absolute():
        candidates.append(p)
    else:
        candidates.extend([(subwork / rel_path).resolve(),
                           (workdir / rel_path).resolve()])

    out_path = None
    for c in candidates:
        if c.is_file():
            out_path = c
            break
    if out_path is None:
        return RelaxResult(
            ok=False,
            failure_reason=f"relax_out_path not found on disk: {rel_path!r} (tried {[str(c) for c in candidates]})",
        )

    # Confirm path resolves inside the run's workdir tree
    try:
        out_path.relative_to(workdir.resolve())
    except ValueError:
        return RelaxResult(
            ok=False,
            failure_reason=f"relax_out_path escapes workdir: {out_path}",
        )

    try:
        size = out_path.stat().st_size
    except OSError as e:
        return RelaxResult(ok=False, failure_reason=f"stat failed on relax.out: {e}")
    if size < 1024:
        return RelaxResult(
            ok=False,
            failure_reason=f"relax.out too small ({size} B < 1024)",
        )

    try:
        text = out_path.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        return RelaxResult(ok=False, failure_reason=f"read failed: {e}")

    if "JOB DONE." not in text[-65536:]:
        return RelaxResult(
            ok=False,
            failure_reason="relax.out missing 'JOB DONE.' in last 64 KB",
        )

    geom = parse_final_geometry(text)
    if geom is None:
        return RelaxResult(
            ok=False,
            failure_reason="could not parse 'Begin final coordinates' / CELL_PARAMETERS / ATOMIC_POSITIONS block from relax.out",
        )

    cell, positions = geom
    # Normalize relax_out_path to workdir-relative for downstream consumers
    try:
        workdir_rel = str(out_path.relative_to(workdir.resolve()))
    except ValueError:
        workdir_rel = str(out_path)

    return RelaxResult(
        ok=True,
        relaxed_cell=cell,
        relaxed_positions=positions,
        relax_out_path=workdir_rel,
        notes=str(payload.get("notes", "")),
    )


# ---------------------------------------------------------------------------
# Public entry
# ---------------------------------------------------------------------------

def run(*, client: LLMClient | None = None,
        original_prompt: str, workdir: Path,
        pw_x_path: str, qe_backend: str = "gpu",
        max_turns: int = 120, max_output_tokens: int = 8192,
        config_advice_block: str = "",
        cli_overrides: dict[str, str] | None = None,
        config_path: Path | None = None,
        max_wall_s: int | None = None) -> RelaxResult:
    """Spawn the vc_relax subagent in a dedicated workdir/vc_relax/
    subdirectory. Returns a RelaxResult — ok=True means main solver should
    inject the relaxed geometry; ok=False means fall back to original
    initial_structure.

    config_advice_block (optional) is the rendered output of
    config_advisor.render_config_advice_block(advice) — when present,
    it's prepended to the subagent's system prompt so the relax step
    uses the right occupations + ecutrho for this material.

    max_wall_s (FIX-25, 2026-05-25): hard wall-clock budget for this
    preflight subagent. When exceeded, the loop injects a "submit
    incomplete" directive to the LLM and (on the next turn) returns
    incomplete so the main solver's fallback (unrelaxed input cell)
    kicks in. Designed to be set to ≤ 50% of the harness wall by the
    caller, guaranteeing the main loop (SCF + bands + DOS) has time
    to run. None disables (legacy behavior; v4.1-compat).

    v4.3: if `client` is None, instantiates via LLMClient.from_role("prerelax").
    """
    if client is None:
        client = LLMClient.from_role(
            "prerelax",
            cli_overrides=cli_overrides,
            config_path=config_path,
        )
    workdir = workdir.resolve()
    subwork = workdir / "vc_relax"
    subwork.mkdir(parents=True, exist_ok=True)

    # Symlink pseudos from parent so vc-relax input can reference ../pseudos/
    # OR pseudos/ (subagent will use ../pseudos/ per its system prompt).
    # Bench harness has already symlinked workdir/pseudos/; nothing to do.

    sys_prompt = build_vc_relax_system_prompt(
        workdir=str(subwork), pw_x_path=pw_x_path, qe_backend=qe_backend,
        config_advice_block=config_advice_block,
    )
    user_prompt = build_vc_relax_user_prompt(original_prompt)

    bash = BashTool(workdir=subwork)
    transcript = subwork / "transcript.json"

    try:
        payload = run_loop(
            client=client,
            system_prompt=sys_prompt, user_prompt=user_prompt,
            bash_tool=bash, max_turns=max_turns,
            max_output_tokens=max_output_tokens,
            transcript_path=transcript,
            cost_journal_path=workdir / "transcript.json",
            max_wall_s=max_wall_s,
        )
    except Exception as e:
        traceback.print_exc()
        return RelaxResult(
            ok=False,
            failure_reason=f"subagent loop crashed: {type(e).__name__}: {e}",
        )

    return _validate_payload(payload, workdir=workdir, subwork=subwork)


# ---------------------------------------------------------------------------
# Render helpers for main solver's prerelax_block
# ---------------------------------------------------------------------------

def render_prerelax_success(result: RelaxResult) -> str:
    """Block injected at the TOP of the main solver's system prompt when
    the subagent succeeded. Tells the main LLM to use the relaxed
    geometry for SCF + bands NSCF + DOS NSCF, NOT re-run vc_relax."""
    notes_line = f"  Subagent notes: {result.notes}\n" if result.notes else ""
    return dedent(f"""\
        ## PRE-RELAXED GEOMETRY (from vc_relax preflight subagent)

        A vc_relax preflight subagent has ALREADY converged a relaxed
        cell + positions for this task. Use the geometry below for your
        SCF + bands NSCF + DOS NSCF. **Do NOT run vc_relax again** —
        the relaxed structure here is authoritative.

        ```
        {result.relaxed_cell}

        {result.relaxed_positions}
        ```

        The subagent's full relax output is at: `{result.relax_out_path}`
        {notes_line}
        IMPORTANT:
          - For your SCF input, set `calculation = 'scf'`. The relaxed
            CELL_PARAMETERS + ATOMIC_POSITIONS above contain the correct
            **physical geometry**, but may be in a non-canonical orientation
            (rotation/permutation relative to seekpath's standardized
            primitive cell). **Before writing SCF**, call
            `seekpath.get_path((cell, positions_frac, atomic_numbers), symprec=1e-3)`
            on this relaxed structure and use `path["primitive_lattice"]`
            / `path["primitive_positions"]` / `path["primitive_types"]`
            as your SCF CELL_PARAMETERS / ATOMIC_POSITIONS / species.
            This guarantees the bands NSCF k-path (Step 2) lives in the
            **same reciprocal basis** as your SCF — without this, k-point
            transformation has to be hand-rolled and silently breaks BZ
            corners (verified failure on AlAs 2026-05-21: X-CBM dropped,
            fake direct gap reported instead of true Γ→X indirect).
            See system_prompt "Step 0" + "Step 2" for full pattern.
          - The submitted `scf_out` MUST be from a fresh
            `calculation='scf'` run — do NOT submit the subagent's
            relax.out as scf_out (it won't be a pure SCF and will fail
            audit conventions).
          - You still control the SCF / NSCF k-mesh choice, ecutwfc /
            ecutrho, and all bands+DOS workflow specifics.
        """)


def render_prerelax_failure(result: RelaxResult) -> str:
    """Block injected when the subagent failed. Tells the main LLM to
    proceed with the original initial_structure as if no preflight
    happened, plus a brief diagnostic note for transparency."""
    return dedent(f"""\
        ## PRE-RELAX FALLBACK NOTICE

        A vc_relax preflight subagent attempted to relax the input
        structure before this main loop started, but FAILED:

          {result.failure_reason or '(no reason captured)'}

        Proceed with the ORIGINAL initial_structure provided in the
        task prompt below. Treat this exactly as a normal T2 run.
        """)


# ---------------------------------------------------------------------------
# User-prompt injection
# ---------------------------------------------------------------------------

# Matches the rendered "Initial structure (primitive cell, ...)" block in
# task_prompt.txt — from the header through the "Note: Starting geometry..."
# line. The next major section is always "Pseudopotentials:" so we stop
# just before it.
_INITIAL_STRUCTURE_BLOCK_RE = re.compile(
    r"Initial structure \(primitive cell.*?(?=Pseudopotentials:)",
    re.DOTALL,
)


def inject_relaxed_into_user_prompt(prompt_text: str, result: RelaxResult) -> str:
    """Replace the original 'Initial structure ...' block in the task
    prompt with the relaxed CELL_PARAMETERS + ATOMIC_POSITIONS from the
    subagent. Returns the modified prompt verbatim if no block matches
    (so callers can swap unconditionally without checking)."""
    if not result.ok:
        return prompt_text
    # Build without dedent — embedded blocks have zero indent and would
    # break textwrap.dedent's common-prefix detection.
    replacement = (
        "[Pre-relaxed structure — converged by vc_relax preflight subagent in this same workdir]\n"
        "NOTE: The CELL_PARAMETERS + ATOMIC_POSITIONS blocks immediately below\n"
        "have ALREADY been relaxed (variable-cell BFGS, press_conv_thr=0.05,\n"
        "forc_conv_thr=1e-4, etot_conv_thr=1e-5). Use them VERBATIM as the\n"
        "structure for your SCF + bands NSCF + DOS NSCF. Do NOT re-run\n"
        "vc_relax. Do NOT use the original (unrelaxed) lattice constants\n"
        "from any earlier version of this task prompt.\n"
        "\n"
        f"{result.relaxed_cell}\n"
        "\n"
        f"{result.relaxed_positions}\n"
        "\n"
        f"Pre-relax log: {result.relax_out_path}\n"
        "\n"
    )
    new_text, n = _INITIAL_STRUCTURE_BLOCK_RE.subn(replacement, prompt_text, count=1)
    if n == 0:
        # Could not find the standard Initial-structure block — fall back
        # to prepending the relaxed block at the top of the prompt. The
        # main system prompt's prerelax_block still carries the message.
        return replacement + "\n" + prompt_text
    return new_text
