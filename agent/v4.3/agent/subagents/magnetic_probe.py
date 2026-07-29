"""Deterministic FM-vs-AFM magnetic ground-state probe (S2).

Runs two single-point spin-polarized SCFs (FM and AFM) on the initial
structure, compares total energies, and returns the lower-energy ordering
plus the ready-to-use ATOMIC_SPECIES + starting_magnetization. Mirrors the
bench's tools/gt_selfrun/m3/m3_run.py:_decide_magnetic_ground_state, so the
solver matches the GT methodology without seeing magnetic_metadata.

Only runs for magnetic materials (gated by config_advisor.is_magnetic), so
M1/M2 nonmagnetic tasks are unaffected.
"""
from __future__ import annotations

import os
import signal
import subprocess
import traceback
from pathlib import Path

from ._magnetism import (
    MagneticGroundState,
    MagneticProfile,
    build_probe_scf_input,
    parse_initial_structure,
    parse_total_energy,
    prompt_indicates_afm,
    select_ground_state,
)

try:
    from qe_tools import build_subprocess_env  # type: ignore
except Exception:  # pragma: no cover - fallback if import path differs
    def build_subprocess_env() -> dict:  # type: ignore
        import os
        return dict(os.environ)


def _run_pw_x(input_text: str, out_path: Path, *, pw_x_path: str,
              qe_backend: str, cwd: Path, timeout_s: int) -> int:
    """Run pw.x once. Overridden in tests. Returns the process return code."""
    in_path = out_path.with_suffix(".in")
    in_path.write_text(input_text)
    np = 4 if qe_backend == "cpu" else 1
    cmd = ["mpirun", "--allow-run-as-root", "-np", str(np), pw_x_path,
           "-in", in_path.name]
    # FIX-35 (2026-05-29): run in its own process group so that on timeout we
    # SIGKILL the whole group (mpirun + the pw.x ranks it forked). subprocess'
    # timeout only kills mpirun, leaving the ranks orphaned and burning CPU/wall
    # on the shared host for the rest of the task (devlog S-followup).
    with out_path.open("w") as fh:
        proc = subprocess.Popen(cmd, cwd=str(cwd), stdout=fh,
                                stderr=subprocess.STDOUT, env=build_subprocess_env(),
                                start_new_session=True)
        try:
            proc.wait(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                proc.kill()
            proc.wait()
            raise
    return proc.returncode


def _energy_for(ordering: str, *, struct, profile: MagneticProfile, subwork: Path,
                pw_x_path: str, qe_backend: str, timeout_s: int) -> "float | None":
    prefix = f"probe_{ordering.lower()}"
    text = build_probe_scf_input(struct=struct, profile=profile, ordering=ordering,
                                 prefix=prefix)
    out_path = subwork / f"{prefix}.out"
    try:
        _run_pw_x(text, out_path, pw_x_path=pw_x_path, qe_backend=qe_backend,
                  cwd=subwork, timeout_s=timeout_s)
    except Exception:
        traceback.print_exc()
        return None
    if not out_path.is_file():
        return None
    return parse_total_energy(out_path.read_text(encoding="utf-8", errors="replace"))


def run(*, original_prompt: str, workdir: Path, pw_x_path: str,
        qe_backend: str = "gpu", is_magnetic: bool,
        magnetic_elements: list[str], seed: dict[str, float],
        timeout_s: int = 1200) -> MagneticGroundState:
    """Probe the magnetic ground state. Returns ok=False (no-op) when the
    material is nonmagnetic or the structure cannot be parsed — caller then
    skips magnetic handling entirely."""
    if not is_magnetic:
        return MagneticGroundState(ok=False, failure_reason="not magnetic")

    struct = parse_initial_structure(original_prompt)
    if struct is None:
        return MagneticGroundState(ok=False, failure_reason="could not parse structure")

    profile = MagneticProfile(is_magnetic=True, magnetic_elements=magnetic_elements,
                              seed=seed)
    workdir = Path(workdir).resolve()
    subwork = workdir / "magnetic_probe"
    subwork.mkdir(parents=True, exist_ok=True)

    fm_E = _energy_for("FM", struct=struct, profile=profile, subwork=subwork,
                       pw_x_path=pw_x_path, qe_backend=qe_backend, timeout_s=timeout_s)
    afm_E = _energy_for("AFM", struct=struct, profile=profile, subwork=subwork,
                        pw_x_path=pw_x_path, qe_backend=qe_backend, timeout_s=timeout_s)

    # FIX-42 (S-1): when both SCFs fail, fall back on the structure's AFM
    # indication (split sublattice labels in the prompt) instead of a silent FM.
    afm_indicated = prompt_indicates_afm(original_prompt)
    ordering, reason = select_ground_state(fm_E=fm_E, afm_E=afm_E,
                                           afm_indicated=afm_indicated)

    # Rebuild the winning input's ATOMIC_SPECIES + signed starting_magnetization
    # so the handoff block matches exactly what scored lowest (or, on total
    # failure, the structure-indicated ordering).
    winning_text = build_probe_scf_input(struct=struct, profile=profile,
                                         ordering=ordering, prefix="probe")
    species_block = _extract_block(winning_text, "ATOMIC_SPECIES", "CELL_PARAMETERS")
    start_mag = _extract_start_mag(winning_text, struct, profile, ordering)

    both_failed = fm_E is None and afm_E is None
    gs = MagneticGroundState(
        ok=not both_failed,
        ordering=ordering, nspin=2,
        magnetic_elements=magnetic_elements,
        atomic_species_block=species_block,
        starting_magnetization=start_mag,
        fm_energy_ry=fm_E, afm_energy_ry=afm_E, reason=reason,
        failure_reason=None if not both_failed else "both SCFs failed",
        structural_fallback=both_failed and afm_indicated,
    )
    (workdir / ".magnetic_ground_state.json").write_text(gs.to_json())
    return gs


def _extract_block(text: str, start_header: str, end_header: str) -> str:
    lines = text.splitlines()
    out, capture = [], False
    for ln in lines:
        if ln.strip() == start_header:
            capture = True
            continue
        if capture and ln.strip().startswith(end_header):
            break
        if capture and ln.strip():
            out.append(ln.strip())
    return "\n".join(out)


def _extract_start_mag(text: str, struct, profile, ordering) -> dict[str, float]:
    """Map species label -> signed seed for the handoff block."""
    from ._magnetism import _afm_labels
    if ordering == "AFM":
        labels = _afm_labels(struct.sites, profile.magnetic_elements)
    else:
        labels = [s.element for s in struct.sites]
    species: list[str] = []
    for lab in labels:
        if lab not in species:
            species.append(lab)
    import re as _re
    result: dict[str, float] = {}
    for sp in species:
        m = _re.match(r"^([A-Z][a-z]?)\d*$", sp)
        el = m.group(1) if m else sp
        if el in profile.seed:
            val = profile.seed[el]
            if ordering == "AFM" and sp.endswith("2"):
                val = -val
            result[sp] = val
    return result
