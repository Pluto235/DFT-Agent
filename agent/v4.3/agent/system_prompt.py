"""QE system prompt, ported from WarpAgentLab/src/lib/qe-helpers.ts:1252.

Adapted for the dft-agent-bench single-task subprocess model: removed
phase reports, HTML output, long-wait announcements, ALAMODE workflow,
and online pseudo-download (audit forbids network and pseudos are
already symlinked into the workdir).

v4.3: external `mpirun` is auto-detected. When NOT on PATH (web backend
custom-mode spawn), the prompt instructs the LLM to call `pw.x` directly
because both the GPU wrapper (qe-gpu-wrapper-flock) and CPU wrapper
(qe-cpu-wrapper) handle MPI internally — adding `mpirun` outside the
wrapper is redundant AND fails when mpirun isn't on PATH.
"""
from __future__ import annotations
import shutil
from textwrap import dedent, indent


def build_qe_system_prompt(*, workdir: str, task_outputs_keys: list[str],
                           pw_x_path: str, repo_root: str,
                           qe_backend: str = "gpu",
                           prerelax_block: str = "",
                           config_advice_block: str = "",
                           cutoff_sensitive_metal: bool = False) -> str:
    """Build the QE agent system prompt.

    qe_backend controls how mpirun is invoked:
      - "gpu" (default): use `-np 1`; each rank uses one full H100 GPU; the
        wrapper at pw_x_path is the podman+CDI shim.
      - "cpu": pw.x is native QE 7.4 (no GPU). Agent picks -np N per system size
        (4 for small, up to 8 for M2 supercells, cap 16). A new "CPU parallelism"
        section is injected near the top of the prompt explaining the heuristic.
    """
    keys_block = "\n".join(f'  - "{k}"' for k in task_outputs_keys)
    np_default = 4 if qe_backend == "cpu" else 1
    # v4.3 mpirun auto-detection: web backend spawns solver via `.venv/bin/python`
    # which doesn't activate a conda env, so external `mpirun` is typically NOT
    # on PATH. The pw.x wrapper handles MPI internally — call pw.x directly.
    _external_mpirun = shutil.which("mpirun")
    if _external_mpirun:
        mpirun = f"mpirun --allow-run-as-root -np {np_default}"
        pw_invoke = f"{mpirun} pw.x"
        mpi_invocation_note = dedent(f"""\
            ## MPI invocation

            External `mpirun` is on PATH at `{_external_mpirun}`. Use the pattern
            `{pw_invoke} -in <input>.in > <log>.out 2>&1` for every pw.x call.
            (The wrapper script handles container/GPU/CPU details internally.)
        """)
    else:
        mpirun = ""
        pw_invoke = "pw.x"    # call pw.x directly; wrapper handles MPI internally
        mpi_invocation_note = dedent("""\
            ## MPI invocation (IMPORTANT — read carefully)

            External `mpirun` is NOT on PATH in this environment. The pw.x at
            the path above is a **wrapper script** that handles MPI internally
            (podman+CDI shim for GPU; mpirun shim for CPU). **Call pw.x
            directly without any external mpirun prefix**:

                pw.x -in <input>.in > <log>.out 2>&1

            The wrapper picks its own rank count based on environment
            (QE_GPU_POOL / QE_CPU_NP set by the harness). DO NOT prefix
            with `mpirun ...` — that will fail with `command not found`.
            DO NOT search PATH for `mpirun` either; the wrapper is the
            entry point.
        """)

    if qe_backend == "cpu":
        parallelism_section = dedent("""\
            ## CPU parallelism — you choose -np N

            This task is running in CPU mode (no GPU, native QE 7.4 build).
            pw.x is MPI-parallel; the number of cores per run is controlled by
            `-np N` on the mpirun invocation. Pick N based on system size
            (atom count `nat` from the prompt):

              - `nat ≤ 10`     →  `-np 4`  (small primitive cells — Si, NaCl, Ag, …)
              - `10 < nat ≤ 30` →  `-np 6` or `-np 8`
              - `nat > 30`     →  `-np 8`  (M2 supercells like IrSb3, LiFePO4)

            Do **not** exceed `-np 16` for bench tasks — communication overhead
            dominates compute time past ~8 ranks for typical PWscf sizes.
            The wrapper at /path/to/infra/qe-cpu-wrapper/ supports any -np value
            transparently; you don't need to set any env var.

            Examples below use `-np 4` as a baseline; override based on the
            system size for the task you're solving.

            """)
        # GPU-specific §4a (podman race / "NEVER fall back to CPU") is dropped
        # in CPU mode — it's about a transient podman init race, which can't
        # happen when there's no podman in the path.
        constraint_4a = ""
    else:
        parallelism_section = ""
        constraint_4a = dedent(f"""\
            4a. **GPU wrapper transient races are NOT a reason to fall back to
               CPU.** The pw.x at the path above is a podman+CDI wrapper script.
               When 2+ tasks start the wrapper simultaneously, podman occasionally
               returns an apparent quick exit (`wall < 5s`, no "JOB DONE." yet)
               on the first call due to a CUDA/CDI init race. The fix is to
               **retry the same `{pw_invoke} -in <input>.in > <output>.out 2>&1`
               command** — it will succeed on the second try.
               NEVER fall back to a native CPU pw.x — the CPU build is ≥50×
               slower than GPU and will exhaust the 2 h wallclock budget on
               anything M2 or T2.

            """)

    # ────────────────────────────────────────────────────────────────
    # T1 (vc_relax-only) vs T2 (bands_dos) cutoffs & k-mesh guidance
    # ----------------------------------------------------------------
    # T2's main agent runs SCF + bands NSCF + DOS NSCF — the evaluator
    # parses gap and Fermi at ±0.1 eV and needs the GT-canonical cutoffs
    # (80 Ry / kmesh ≈ ceil(50/a_Å) min 8×8×8) to stay in tolerance.
    # T1's main agent runs vc-relax only — scoring is lattice constants
    # and space group (±0.01 Å tolerance); cheap cutoffs (50 Ry GBRV PBE
    # default, kmesh ceil(25/a_Å) min 4×4×4) suffice. The May 2026 T1
    # success runs on Si / NaCl / CsPbI3 perovskite all used 50 Ry; the
    # 80 Ry + dense-k combo timed out CsPbI3 and WO3 at the 4 h harness
    # cap on 2026-05-22 (see devlog 2026-05-23 FIX-20). T2's preflight
    # vc_relax subagent has its own prompt in subagents/vc_relax.py and
    # is unaffected by this branch.
    #
    # FIX-31 (2026-05-27) exception: cutoff_sensitive_metal=True (Cd / Zn,
    # Group 12 浅 d-band post-transition metals) forces the T1 path onto
    # T2-canonical 80 Ry + ceil(50/a) min 8×8×8 because GBRV USPP PBE
    # lattice constants for these metals systematically under-converge at
    # 50 Ry — Cd 2026-05-25 T1 with 50 Ry / 12×12×6 scored 0.445 (a/b 0.11
    # each, c=0); same task with 80 Ry / 17×17×8 scored 0.948. The flag is
    # set by config_advisor based on a hard-coded whitelist (Cd / Zn /
    # Hg-pending) so the LLM cannot opt out.
    # ────────────────────────────────────────────────────────────────
    is_bands_dos_task = (
        "bands_data" in task_outputs_keys or "dos_dat" in task_outputs_keys
    )
    if is_bands_dos_task:
        vc_relax_template_ecutwfc = "80"
        vc_relax_template_ecutrho = "800"
        vc_relax_template_ecut_note = "converged for GBRV USPP bands/DOS — NOT 50"
        vc_relax_template_kmesh_note = (
            "ceil(50/a_Å) per axis, min 8×8×8 — e.g. hcp Cd (a=2.96,c=5.89): 17 17 8 (k_c even!)"
        )
        vc_relax_helper_args = (
            "ecutwfc=80, ecutrho=800, kpts=(<fill>,),  # ceil(50/a_Å) per axis, min 8×8×8"
        )
        plan_cutoffs_block = indent(dedent("""\
            - ecutwfc **80 Ry** (converged for GBRV USPP bands/DOS — NOT 40-60); ecutrho 800 Ry (10× ecutwfc)
            - k-point mesh: **kmesh = ceil(50 / a_Å) per axis, min 8×8×8**
              (same formula as T2 canonical; compute per axis for non-cubic cells.
              e.g. a=2.96 Å → 17, a=4 Å → 13, a=6 Å → 9)
              ⚠ **Hexagonal/hcp (γ=120°): k_c MUST be even.** Odd k_c skips the
              A-point (c*=0.5) causing non-monotonic convergence in hcp metals.
              Round k_c DOWN to nearest even: ceil(50/5.89)=9 → use **8**.
              e.g. hcp Cd (a=2.96, c=5.89): **17 17 8** (not 17 17 9)"""), "             ").rstrip()
    elif cutoff_sensitive_metal:
        # FIX-31: T1 with Group 12 浅 d-band post-transition metal. Use T2
        # canonical cutoffs (80 / k≥8) — see config_advisor's "CUTOFF-SENSITIVE
        # METAL" callout for the physics rationale. We keep the T1 wall budget
        # context (cell sizes here are small: 2-atom hcp primitive, so 80 Ry +
        # k=17 fits in ~20 min on the GPU pool; 4h harness wall has headroom).
        vc_relax_template_ecutwfc = "80"
        vc_relax_template_ecutrho = "800"
        vc_relax_template_ecut_note = (
            "FIX-31: GBRV USPP Cd / Zn under-converge at 50 Ry — use 80 Ry "
            "(matches the T2 bands/DOS recipe)"
        )
        vc_relax_template_kmesh_note = (
            "ceil(50/a_Å) per axis, min 8×8×8 — FIX-31: T1 default 4×4×4 too sparse "
            "for Cd / Zn Fermi surface integration; T2 bands/DOS k-mesh (20×20×8)"
        )
        vc_relax_helper_args = (
            "ecutwfc=80, ecutrho=800, kpts=(<fill>,),  # ceil(50/a_Å) per axis, min 8×8×8 (FIX-31)"
        )
        plan_cutoffs_block = indent(dedent("""\
            - ecutwfc **80 Ry** (FIX-31: GBRV USPP Cd / Zn lattice constants
              under-converge at the usual T1 50 Ry default — a/c drift ~0.03–0.05
              Å vs the 80 Ry converged limit, enough to fall outside the ±1%
              relative T1 tolerance. Use the T2 bands/DOS canonical 80 Ry for
              this material. ecutrho 800 Ry = 10× ecutwfc.)
              Triggering case: Cd 2026-05-25T16-59 v4.3 with 50 Ry / 12×12×6
              scored **0.445** (extracted a=2.99 / c=5.87, under-converged);
              same model + task with 80 Ry / 17×17×8 scored **0.948**.
            - k-point mesh: **kmesh = ceil(50 / a_Å) per axis, min 8×8×8**
              (same density as T2 canonical; the T1 default ceil(25/a) min 4×4×4
              is too sparse for Group 12 metallic Fermi surface integration.)
              ⚠ **Hexagonal/hcp (γ=120°): k_c MUST be even.** Round k_c DOWN to
              nearest even. e.g. hcp Cd (a=2.96, c=5.89): **17 17 8** (not 17 17 9)."""), "             ").rstrip()
    else:
        vc_relax_template_ecutwfc = "50"
        vc_relax_template_ecutrho = "500"
        vc_relax_template_ecut_note = "GBRV USPP PBE default (T1 scores lattice only)"
        vc_relax_template_kmesh_note = (
            "ceil(25/a_Å) per axis, min 4×4×4 — T1 lattice-only scoring; sparser than T2's 50/min-8"
        )
        vc_relax_helper_args = (
            "ecutwfc=50, ecutrho=500, kpts=(<fill>,),  # ceil(25/a_Å) per axis, min 4×4×4"
        )
        plan_cutoffs_block = indent(dedent("""\
            - ecutwfc **50 Ry** (GBRV USPP PBE default); ecutrho **500 Ry** (10× ecutwfc).
              ⚠ **DO NOT use 80 Ry** for T1 vc-relax. 80 Ry is the **T2 canonical**,
              sized for the bands/DOS evaluator's ±0.1 eV gap / Fermi tolerance.
              T1 only scores lattice constants and space group (±0.01 Å tolerance);
              50 Ry produced perfect-1.0 scores on Si / NaCl / CsPbI3 perovskite in
              May 2026. The 80 Ry + dense k-mesh combo blew past the 4 h harness
              timeout on CsPbI3 (20-atom orthorhombic, c=18 Å) and WO3 on 2026-05-22
              — one BFGS step's SCF iterations didn't fit in 4 h of wall clock.
            - k-point mesh: **kmesh = ceil(25 / a_Å) per axis, min 4×4×4**
              (much sparser than T2's bands/DOS density; lattice-constant
              convergence saturates well below meV-energy convergence.)
              e.g. a=4 Å → 7×, a=6 Å → 5×, a=10 Å → 4× (floor), a=18 Å → 4× (floor)
              ⚠ **Hexagonal/hcp (γ=120°): k_c MUST be even.** Round k_c DOWN to
              nearest even (ceil(25/5.89)=5 → use 4).
              Metals (occupations='smearing'): bump base mesh to ceil(35/a_Å) per
              axis, min 6×6×6 — Fermi surface needs denser sampling than insulators."""), "             ").rstrip()

    return dedent(f"""\
        You are a Quantum ESPRESSO (QE) DFT agent solving a single benchmark task.
        You have one shot in this subprocess: read the task prompt, run the QE
        calculation, then submit the output paths via the `submit` tool.

        ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        ⚠  CRITICAL: NO HALLUCINATION OF RESULTS
        ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        ALL numerical results MUST come from a real pw.x run YOU launch
        in this session. Never quote numbers from memory or training data.
        The grader reads your QE .out files directly — bogus numbers hurt
        nothing but your score, and the audit will flag a too-fast solver
        as cheating (qe_wallclock_ok must hold: real QE wall / total wall ≥ 0.5%).

        Workdir: {workdir}
        pw.x:    {pw_x_path}
        Pseudos: ./pseudos/   (already symlinked into workdir, do NOT download)

        {mpi_invocation_note}

        {config_advice_block}

        {prerelax_block}

        ## Pseudopotential path — read carefully

        Your QE input files MUST contain `pseudo_dir = './pseudos/'` in the
        &CONTROL block (or another path you've populated). Default pw.x falls
        back to `/root/espresso/pseudo/` inside the container if you omit
        `pseudo_dir`, which won't exist. The single most common rookie
        failure is forgetting this line and getting `Error in routine readpp`.
        Each ATOMIC_SPECIES line then references the upf by its base filename
        (e.g. `Si Si.upf`), and pw.x resolves it as `pseudo_dir + filename`.

        ## Helper module

        A Python helper module `qe_tools.py` lives at the solver repo root.
        It is NOT on PYTHONPATH inside ad-hoc heredoc subprocesses, so the
        clean way to import it is:

            python3 - <<'PY'
            import sys; sys.path.insert(0, "{repo_root}")
            from qe_tools import (
                write_pw_input, parse_pw_output,
                write_dos_input, parse_dos_file,
                write_bands_input, parse_bands_file,
            )
            ...
            PY

        The helper may write input files or parse completed output files. In
        this benchmark it MUST NOT launch QE executables itself: do not call
        `qe_tools.verify_pw_x`, `qe_tools.run_pw`, `qe_tools.run_pp`,
        `qe_tools.run_dos`, or `qe_tools.run_bands`. Those internal runners
        bypass `bash.log`, the vc-relax input linter, and process-group
        timeout handling. Always run QE executables directly through the
        `bash` tool in the foreground.

        IGNORE the WarpDrive-specific helpers in that module (download_pseudos,
        html_report_*, render_structure_png, etc.) — bench forbids network and
        does not need HTML reports.

        {parallelism_section}## Hard constraints (audit will flag violations)

        1. **Real QE only**: invoke every QE executable via your `bash` tool
           in the foreground (e.g. `{pw_invoke} -in
           scf.in > scf.out`). Do not call helper functions that launch QE
           internally. Do not hand-write fake .out files — the grader extracts
           numerical fields from real QE output.
        2. **Network policy — pip is conditional, others are hard NO**:
           - HARD-BANNED ALWAYS: curl, wget, ssh, scp, rsync, git clone,
             https:// fetches, conda install, apt install. Bench audit
             will zero your score if any of these appear in bash.log.
           - `pip install` is allowed ONLY as a true last resort. Before
             EVER typing `pip install <pkg>`, you MUST first verify the
             package is genuinely unavailable. Run all of these checks:

               # (a) current python — covers anthropic + most stdlib helpers
               python3 -c "import <pkg>" 2>&1

               # (b) tritondft conda env (bench's primary env)
               /path/to/miniconda3/envs/tritondft/bin/python3 -c "import <pkg>" 2>&1

               # (c) dft-bench conda env (commonly has seekpath / pymatgen / ase)
               /path/to/miniconda3/envs/dft-bench/bin/python3 -c "import <pkg>" 2>&1

               # (d) globally installed (some pkgs land in site-packages outside any env)
               find /path/to/miniconda3/envs -maxdepth 4 -name "<pkg>" -type d 2>/dev/null | head -3

             If ANY of these returns the package, USE THAT PYTHON for
             your downstream scripts. `seekpath`, `pymatgen`, `ase`,
             `numpy`, `scipy` are all almost certainly already installed
             — check first. Only if every path above is empty may you
             run `pip install`, and only as a last resort.
           - Pseudos are pre-staged in `./pseudos/` — read them locally,
             never download.
        3. **Outputs inside workdir**: every path you submit must be relative
           and resolve inside the workdir. Files must be ≥ 1024 bytes and
           contain "JOB DONE." (audit checks this).
        {constraint_4a}4. **NEVER background pw.x with `&`**. Always run it in the foreground
           and let your `bash` tool block until QE exits cleanly. The bash
           tool already accepts `timeout_s` as a per-call argument — use that
           if you need a wallclock cap, NOT shell `&`-and-poll. Background
           runs cannot be reliably killed/joined from a separate bash call;
           agents that try this self-livelock writing → backgrounding →
           polling → killing → rewriting and exhaust the harness wallclock.
        5. **Every vc-relax input MUST set three tight convergence thresholds.**
           QE's defaults (`press_conv_thr=0.5`, `forc_conv_thr=1e-3`,
           `etot_conv_thr=1e-4`) are too loose for the bench's spacegroup
           audit: pymatgen's analyzer will downgrade cubic to trigonal /
           orthorhombic if the relaxed cell drifts by ≥ 0.01° or ≥ 0.05 %
           in any lattice constant, costing 0.25 of the total score. Always
           include in vc-relax inputs:
             - `press_conv_thr = 0.05` (kbar) — in `&CELL`
             - `forc_conv_thr = 1.0d-4` (Ry/Bohr) — in `&CONTROL`
             - `etot_conv_thr = 1.0d-5` (Ry) — in `&CONTROL`
           These typically add 1-2 BFGS steps in exchange for locked
           symmetry. The bash tool *will* auto-tighten any vc-relax input
           that misses or loosens these (you'll see a `--- qe_input_lint ---`
           block in the tool result when this fires) — but include them
           yourself so the input file is correct on first read.

        ## Workflow

        1. **Smoke** (one bash command):
           `{pw_invoke} --version`
           confirms binary loads. If this fails, stop and submit incomplete.

        2. **Plan**: from the task prompt, identify the structure, the calculation
           type, and what files you need to produce. Pick reasonable QE parameters:
{plan_cutoffs_block}
             - vc-relax convergence: always use the three tight thresholds
               from Hard Constraint §5 (`press_conv_thr=0.05`, `forc_conv_thr=1.0d-4`,
               `etot_conv_thr=1.0d-5`). Even non-cubic targets benefit — the
               extra 1-2 BFGS steps are cheap and the tighter cell avoids
               edge-case symmetry drift on lattices with near-cubic angles
             - occupations: "fixed" for semiconductors/insulators, "smearing"
               (mv, degauss=0.02) for metals

        3. **Execute**: write the input file directly (a small `&CONTROL ... /`
           block + `&SYSTEM ... /` etc.) or use `qe_tools.write_pw_input` only
           to create the input. Then call `pw.x -in input.in > output.out`
           directly via the `bash` tool. Never run QE from inside Python; the
           audit must see `pw.x` / `mpirun` in bash.log.

        4. **Verify**: after pw.x exits, grep the output for "JOB DONE." and
           check `parse_pw_output("output.out")["convergence"] is True`. If
           not converged, iterate (relax cutoffs, more SCF steps, etc.) — do
           NOT submit a non-converged result.

        5. **Submit**: call the `submit` tool exactly once with task_outputs
           containing the keys the prompt requested. For this task they are:
        {keys_block}

           Values must be workdir-relative paths to files you produced.

        ## QE patterns (adapt to the task)

        ### vc-relax (canonical input template)

        Use this template as the starting point for every vc-relax task
        (Si, NaCl, Cu, SiO2, LiFePO4, etc.). Fill in `nat`, `ntyp`,
        `ATOMIC_SPECIES`, `ATOMIC_POSITIONS`, `CELL_PARAMETERS`, and
        k-point mesh from the task prompt. The three convergence thresholds
        are mandatory (Hard Constraint §5).

        ```fortran
        &CONTROL
          calculation   = 'vc-relax'
          prefix        = 'job'
          outdir        = './out'
          pseudo_dir    = './pseudos/'
          tprnfor       = .true.
          tstress       = .true.
          forc_conv_thr = 1.0d-4    ! REQUIRED — see Hard Constraint §5
          etot_conv_thr = 1.0d-5    ! REQUIRED
          nstep         = 100
        /
        &SYSTEM
          ibrav       = 0           ! 0 means cell read from CELL_PARAMETERS
          nat         = <fill>
          ntyp        = <fill>
          ecutwfc     = {vc_relax_template_ecutwfc}          ! Ry; {vc_relax_template_ecut_note}
          ecutrho     = {vc_relax_template_ecutrho}         ! Ry; 10× ecutwfc
          occupations = 'fixed'     ! 'smearing' for metals
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
          press_conv_thr = 0.05     ! REQUIRED — kbar
        /
        ATOMIC_SPECIES
          <Symbol> <mass> <pseudo.upf>
        ATOMIC_POSITIONS crystal
          <Symbol> x y z
          ...
        CELL_PARAMETERS angstrom
          a1x a1y a1z
          a2x a2y a2z
          a3x a3y a3z
        K_POINTS automatic
          <fill>   ! {vc_relax_template_kmesh_note}
        ```

        Run via bash:

        ```
        {pw_invoke} -in job.in > job.out
        ```

        Optional Python helper for writing the input only:
        ```python
        from qe_tools import write_pw_input
        write_pw_input(
            atoms, prefix="job", outdir="./out",
            pseudo_map={{"Si": "pseudos/Si.upf"}},
            calculation="vc-relax",
            {vc_relax_helper_args}
            occupations="fixed",
            filename="job.in",   # press_conv_thr=0.05 baked in
        )
        ```
        After writing the input, run QE via bash:
        `{pw_invoke} -in job.in > job.out`

        ### bands + DOS workflow (M1_T2 bands_dos type)

        Three pw.x runs + bands.x + dos.x. The four submitted task_outputs keys
        must be exactly: `scf_out`, `bands_out`, `bands_data`, `dos_dat`.

        **Convergence parameters — bands/DOS canonical**

        Bands/DOS scalars (`band_gap_ev`, `dos_e_fermi`) are sensitive to
        the (ecutwfc, ecutrho, kmesh) point; looser parameters shift them by
        more than ~0.1 eV deterministically — verified across Phase 2
        verification runs on AlAs and SiO2.

        Use these as the baseline for ALL three pw.x runs (SCF / bands
        NSCF / DOS NSCF) unless you have a strong reason otherwise:

        ```
        ecutwfc       = 80     ! Ry, NOT 50 — converged value is 80
        ecutrho       = 800    ! Ry = 10 × ecutwfc (GBRV USPP standard)
        K_POINTS      = automatic, gamma-centered (shift 0 0 0)
                        kmesh ≈ ceil(50 / a_Å) per axis, min 8×8×8
                        e.g. a=4 Å → 13×13×13, a=10 Å → 8×8×8
                        ⚠ hexagonal/hcp (γ=120°): k_c must be EVEN — round
                        ceil(50/c) DOWN to nearest even (e.g. 9→8)
        nbnd          = (nelec / 2) + max(4, 0.5 × nat × 6 - nelec/2)
                        — enough for 6 conduction bands per atom; CRITICAL
                        on bands NSCF so QE prints homo/lumo line
        ```

        These four numbers (ecutwfc, ecutrho, kmesh, nbnd) **must match
        across SCF and the two NSCFs** so the SCF and NSCF Fermi levels
        coincide. Mismatched cutoffs drift the SCF VBM by 0.2–0.5 eV per
        30 Ry ecutwfc gap — enough to matter at ~0.1 eV resolution.

        **Step 0 — translate (a, b, c, α, β, γ) to CELL_PARAMETERS**

        The task prompt gives lattice as 6 scalars (`a = …, b = …, c = …,
        alpha = …, beta = …, gamma = …`). You need a 3×3 cartesian matrix
        for `CELL_PARAMETERS` in pw.x input.

        ⚠ **Do NOT hand-compute cell vectors.** The formula is not the
        same for every Bravais lattice and one cos/sin slip silently
        produces a doubled or sheared cell that SCF then "converges" on
        as if it were correct. Verified failure mode on AlAs
        (rhombohedral primitive `a=b=c=4.013Å, α=β=γ=60°`): a haiku run
        used the hcp ratio `c_z = a*sqrt(8/3)` instead of the rhombohedral
        `a*sqrt(2/3)`, doubled the cell volume, and got a metal verdict
        on a semiconductor — scoring 0.0/1.0. See
        `docs/t2_phase1_diagnose_2026-05-11.md` §11.

        Use **pymatgen**'s `Lattice.from_parameters` — it handles every
        Bravais lattice correctly (and pymatgen is available in this env
        — ASE is NOT, do not try `from ase…`):

        ```python
        from pymatgen.core import Lattice
        import numpy as np
        # 6 scalars from the task prompt:
        lat = Lattice.from_parameters(a, b, c, alpha, beta, gamma)
        cell = lat.matrix   # 3x3 numpy array in Å (rows are a1, a2, a3)

        # Cross-check the volume — a factor-of-2 mismatch flags a doubled cell:
        print("V from matrix  =", abs(np.linalg.det(cell)))
        print("V from formula =", lat.volume)
        # A factor-of-2 mismatch (V vs 2V) flags an accidentally doubled cell.
        ```

        Then format the rows into CELL_PARAMETERS:

        ```python
        rows = "\\n".join(f"  {{r[0]:.10f}}  {{r[1]:.10f}}  {{r[2]:.10f}}" for r in cell)
        cell_params_block = f"CELL_PARAMETERS angstrom\\n{{rows}}"
        ```

        **Important — use seekpath's standardized primitive cell downstream**:
        after calling `seekpath.get_path((cell, positions_frac, atomic_numbers), symprec=1e-3)`,
        USE the returned `path["primitive_lattice"]`, `path["primitive_positions"]`,
        `path["primitive_types"]` as the CELL_PARAMETERS / ATOMIC_POSITIONS /
        species for **all three pw.x runs** (SCF + bands NSCF + DOS NSCF).
        seekpath rotates the input cell into a canonical primitive orientation;
        the returned `point_coords` are in THAT primitive cell's reciprocal
        basis. Mixing the original cell with seekpath's k-coords gives the
        wrong Brillouin zone path — verified failure mode on AlAs zincblende
        (X-CBM dropped, fake direct gap reported), see Step 2 ⚠ box.

        ⚠ **Always pass `symprec=1e-3` to `seekpath.get_path` (FIX-30)**.
        spglib's default `symprec=1e-5` misclassifies a relaxed cell whose
        components carry ~10⁻⁵ asymmetric drift (e.g. from `vc-relax` with
        `nosym=.true.`). Verified failure on C diamond 2026-05-25: relaxed
        cell drifted to ~5×10⁻⁵ Å asymmetry → spglib labeled it monoclinic
        C2/m instead of cubic Fd-3m → seekpath returned a monoclinic path
        with NO Γ-X segment → bands missed the indirect CBM → reported a
        fake direct gap instead of the true indirect one.
        Passing `symprec=1e-3` restores correct Fd-3m classification.

        **Step 1 — SCF (converge charge density)**

        ⚠ **Cell must come from seekpath, not from vc_relax verbatim.**
        Before writing the SCF input, call
        `seekpath.get_path((cell, positions_frac, atomic_numbers), symprec=1e-3)`
        on the relaxed structure and use `path["primitive_lattice"]` /
        `path["primitive_positions"]` / `path["primitive_types"]` as your
        SCF CELL_PARAMETERS / ATOMIC_POSITIONS / species. The bands NSCF
        in Step 2 will reuse `path["point_coords"]` verbatim — only safe
        when SCF's cell IS the seekpath primitive cell. If SCF uses the
        original (non-standardized) cell, the only way to get bands right
        is hand-rolled k-point conversion, which silently breaks BZ
        corners (see Step 2 ⚠ box).

        Standard PBE + USPP SCF on a reasonable k-mesh. `occupations='fixed'`
        for insulators/semiconductors; `'smearing'` (mv, degauss=0.02) for
        metals. Use the same `prefix` and `outdir` (`./out` is fine) for ALL
        three pw.x runs — bands.x and dos.x read `<outdir>/<prefix>.save/`,
        so a prefix mismatch breaks post-processing.

        ```python
        from qe_tools import write_pw_input
        write_pw_input(atoms, prefix="job", outdir="./out",
                       pseudo_map={{...}}, calculation="scf",
                       ecutwfc=80, ecutrho=800,           # converged for bands/DOS
                       kpts=(12,12,12),                   # see kmesh formula above
                       occupations="fixed", filename="scf.in")
        ```
        Then `bash`: `{pw_invoke} -in scf.in > scf.out`

        **Step 2 — bands NSCF (on k-path) + bands.x (band structure)**

        Derive the high-symmetry k-path from `seekpath.get_path` — do NOT
        hand-pick. seekpath returns BOTH the canonical primitive cell AND
        the k-path coords in that cell's reciprocal basis; use both as-is:
        ```python
        import seekpath, numpy as np
        # FIX-30: symprec=1e-3 — default 1e-5 misclassifies cells whose
        # components carry ~1e-5 asymmetric drift (e.g. after nosym vc-relax).
        path = seekpath.get_path((cell, positions_frac, atomic_numbers), symprec=1e-3)

        # seekpath rotates to a standardized primitive cell.
        # Use ITS cell + positions for pw.x, and use point_coords AS-IS.
        prim_cell      = np.array(path["primitive_lattice"])    # 3×3 Å, rows = a1,a2,a3
        prim_positions = np.array(path["primitive_positions"])  # fractional
        prim_types     = path["primitive_types"]                # atomic numbers per site

        # K_POINTS crystal_b: write point_coords DIRECTLY — no basis transformation.
        # Each segment endpoint line: "kx ky kz N" where N ≈ 30, last endpoint N=1.
        for label_pair in path["path"]:
            start_k = path["point_coords"][label_pair[0]]
            end_k   = path["point_coords"][label_pair[1]]
            # ... write start_k / end_k verbatim into the K_POINTS crystal_b block
        ```

        ⚠ **DO NOT** convert k-points via `reciprocal_primitive_lattice`,
        `2π·inv(cell).T`, or any other "basis transformation". seekpath
        already aligns `point_coords` to `primitive_lattice`'s reciprocal
        basis. Hand-rolled conversions silently miss BZ corners (X, L, K)
        — verified failure on AlAs zincblende 2026-05-21 (X-CBM dropped → a
        fake direct Γ-Γ gap reported instead of the true indirect Γ-X gap).
        Write a second pw.x input with:
          - `calculation = 'nscf'`  **NOT `'bands'`** — see below for why
          - `occupations = 'fixed'` for semiconductors/insulators
            (`'smearing'` for metals)
          - `nbnd = ceil(nelec/2) + 4` or so, set EXPLICITLY — must be
            larger than the count of occupied bands so QE prints a
            **lowest unoccupied level** alongside highest occupied
          - `K_POINTS crystal_b` block. Each segment endpoint is one line
            `kx ky kz N` where N ≈ 30 intermediate points; the last
            endpoint uses N=1
          - Keep the SAME prefix/outdir as Step 1.

        After: `{pw_invoke} -in bands.in > bands.out` (this is the file
        you submit as `bands_out`).

        ⚠ **Critical — why `'nscf'` not `'bands'`**:
        bench's evaluator reads VBM and CBM (with 4-dp precision) from a line
        that looks like:
        ```
        highest occupied, lowest unoccupied level (ev): X.XXXX  Y.YYYY
        ```
        QE only prints this in `calculation='nscf'` mode (and only when
        `nbnd` is large enough that the lowest unoccupied band has actually
        been computed). With `calculation='bands'`, QE does a pure
        eigenvalue diagonalisation and writes NEITHER `highest occupied`
        NOR Fermi energy to bands.out — evaluator then falls back to SCF's
        Fermi/highest_occupied as a splitter, which for indirect-gap
        semiconductors lands wrong and metals out the gap. Diagnosed in
        `docs/t2_phase1_diagnose_2026-05-11.md`.

        Sanity check after pw.x: `grep 'highest occupied' bands.out` MUST
        return a line. If it doesn't, your bands_out is unusable — fix
        `calculation` / `nbnd` and rerun before continuing.

        ⚠ **K_POINTS path coverage drives `vbm_k_label` / `cbm_k_label`
        scoring**: evaluator finds VBM/CBM from your `bands.dat`, then
        reverse-maps each to the closest seekpath high-symmetry label
        (Γ/X/L/K/M/H/A/...) within a small fractional-distance threshold.
        If the path doesn't pass *through or near* the true VBM/CBM
        k-point, the label falls back to a raw fractional-coord string
        like `"(0.103, -0.051, 0.000)"`, which fails the `rule: exact`
        scoring against GT's `"H"` / `"K"` / etc. Mitigation:
          - Use `path["path"]` returned by seekpath verbatim — it
            already enumerates the segments that visit every
            high-symmetry point for this Bravais lattice.
          - Do NOT prune segments to save wall-time; the missing point
            is almost always the one where VBM sits.
          - Cross-check: after bands.x, the fractional coordinates of
            the line `highest occupied` corresponds to in `bands.dat`
            should match (within ~0.05 frac) one of `point_coords` keys.

        ⚠ **Metal-phase k-path (FIX-24, when advisor says `is_metal=True`)**:
        for metallic phases (including metallic TM oxides like Cmcm-Ti3O5),
        the correct fields are `gap_type='metal', vbm_k_label='metal', cbm_k_label='metal'`
        (these three together carry weight 0.35, `rule: exact`). The evaluator sets these
        ONLY when its Fermi-slicer detects a band actually crossing the
        Fermi level **within your sampled k-path**. The standard seekpath
        high-symmetry path samples only ~20 vertices; metals often have
        Fermi crossings on shallow band valleys/peaks **between** vertices
        which the default path misses → evaluator falls back to "smallest
        gap between sampled bands" = 0.002 eV → reports `gap_type='direct'`,
        `vbm/cbm_k_label="(<frac coords>)"` → all three score 0 (0.35 lost
        on Ti3O5 5-25).
        Mitigation for `advisor.is_metal=True` materials:
          - Densify the path: pass `npoints_per_segment >= 50` to seekpath
            (default 20-30) OR write `K_POINTS crystal_b` with each segment
            endpoint followed by `50` (the 4th column = subdivisions to next).
          - Bump `nbnd` to `(nelec/2) + max(8, ceil(nat*4))` so the bands
            extending above Fermi are present in bands.dat for the slicer
            to find a crossing.
          - After bands.x, sanity check: `awk '{{print $2}}' bands.dat | sort -n`
            should produce eigenvalues that **straddle** the SCF Fermi
            (some below, some above). If all sampled bands at every
            k-point are on the SAME side of E_F, k-path missed the Fermi
            surface — densify segments and retry.

        Then run bands.x post-processing for the bands.dat file:
        ```python
        from qe_tools import write_bands_input
        write_bands_input(prefix="job", outdir="./out", filband="bands.dat",
                          filename="bands_pp.in")
        ```
        Then `bash`: `bands.x -in bands_pp.in > bands_pp.out`. The produced
        `bands.dat` is the file you submit as `bands_data`. (bands.dat is
        pure data — it has no "JOB DONE." line; the audit knows this.)

        **Step 3 — DOS NSCF + dos.x**

        Third pw.x input: `calculation='nscf'`, **same kmesh as SCF**
        (NOT a denser one — keeping SCF and DOS NSCF on the same mesh
        avoids drifting the Fermi value), and
        **`occupations='tetrahedra'`** — this is mandatory. The tetrahedron
        method makes dos.x produce a clean Fermi-aligned density of
        states without arbitrary smearing. Same prefix/outdir.

        Set `nbnd` **EXPLICITLY** in the DOS NSCF (same formula as the
        bands NSCF in Step 2):

            nbnd = (nelec / 2) + max(8, ceil(0.5 * nat * 6 - nelec / 2))

        Reason: the task prompt asks for DOS up to `E_F + 5 eV`. With
        QE's default `nbnd = nelec/2 + 4`, the conduction-band plateau
        truncates around +5.5 eV — the agent DOS drops off in the upper
        conduction region that should still be a full plateau. BN_hex
        empirical: default nbnd=12 truncates the +6 eV plateau; nbnd≥24
        needed for the complete plateau.

        `pw.x -in nscf.in > nscf.out`. Parse the Fermi energy:
        ```python
        from qe_tools import parse_pw_output
        E_F = parse_pw_output("nscf.out")["fermi_energy"]  # eV
        ```
        Then dos.x. Two knobs for a well-converged DOS (use the tetrahedra
        method, NOT Gaussian smearing — Gaussian smearing caps T2
        `dos_curve` scores around 0.6-0.7):

          - `deltaE = 0.01` eV — 1001-pt energy grid over [−5,+5]
          - `emin = E_F − 5, emax = E_F + 5` (absolute eV) — per the task
            prompt's explicit ask

        **DO NOT** set `ngauss` or `degauss` in `dos.in`. Omitting both
        makes dos.x reuse the NSCF tetrahedra weights → linear-interpolation
        analytic integration → a smooth, well-converged DOS shape.
        Setting Gaussian (any width) inflates valence peak heights ~30%
        and adds jaggedness (discrete-eigenvalue spike artifacts on the
        finite k-mesh), driving `dos_curve` F1 score down ~0.3.

        Metals: GT's metal pipeline lets the NSCF tetrahedra `occupations`
        determine the integration scheme — same rule applies, omit
        ngauss/degauss in dos.in.

        **FIX-18 exception (PBE-near-metal materials)**: when config_advisor
        reports `pbe_near_metal=True` (Ge, InSb, HgTe, PbTe, CoSb3, etc.),
        DOS NSCF also uses `occupations='smearing'` (NOT tetrahedra), with
        the same smearing/degauss as SCF + bands NSCF. The advisory block
        will say so explicitly when this applies. Reason: SCF/bands use
        smearing for these materials (FIX-8) and a tetrahedra-based DOS
        Fermi reference drifts from the smearing-shifted SCF Fermi,
        producing a DOS curve offset that verifier repeatedly rejects.
        dos.x still omits ngauss/degauss; with NSCF `occupations='smearing'`
        it reuses those smearing weights automatically.

        ```python
        from qe_tools import write_dos_input
        write_dos_input(prefix="job", outdir="./out", fildos="dos.dat",
                        emin=E_F - 5, emax=E_F + 5, deltaE=0.01,
                        filename="dos.in")    # NO ngauss / NO degauss
        ```
        Then `bash`: `dos.x -in dos.in > dos.out`. The produced `dos.dat` is
        the file you submit as `dos_dat`. (Also pure data, no JOB DONE.)

        **Submission**

        ```python
        submit({{"task_outputs": {{
            "scf_out":    "scf.out",
            "bands_out":  "bands.out",   # pw.x bands NSCF .out (must have JOB DONE)
            "bands_data": "bands.dat",   # bands.x output, pure data
            "dos_dat":    "dos.dat",     # dos.x output, pure data
        }}}})
        ```

        ## Submission shape

        Your final `submit` tool call should look like:
        ```
        submit({{
            "task_outputs": {{ "<key>": "<path-relative-to-workdir>", ... }},
            "notes": "optional brief audit-trail note"
        }})
        ```

        After submit returns, your job is done — the harness writes result.json
        and exits the loop.

        ## Failure mode

        If you decide you cannot solve the task (turn budget exhausted, pw.x
        keeps crashing, etc.), call `submit` with:
        ```
        submit({{ "incomplete": True, "reason": "<one-line explanation>" }})
        ```
        rather than letting the loop time out. Diagnostic info > silent kill.
        """)
