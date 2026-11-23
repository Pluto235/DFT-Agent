"""verify_run — program-level physical sanity check.

Advisory tool for the LLM; mandatory gate in solve.py before submit.
Returns a structured verdict (pass / soft_fail / hard_fail) plus a list
of individual checks so the LLM (or solve.py) can see exactly which
constraint failed.

T1 vc_relax checks (hard unless marked advisory)
  - SCF convergence
  - relaxation_converged — pw.x "bfgs converged" marker (authoritative geometry
    convergence; replaces the force/stress thresholds as the hard gate)
  - "JOB DONE." in last 64 KB
  - space group preserved (vs initial, if provided)
  - cell angles drift ≤ 0.5° for cubic targets
  - advisory: max|force| ≤ forc_conv_thr (diagnostic; not bench-scored)
  - advisory: max|stress diag| ≤ 0.5 kbar (diagnostic; not bench-scored)
  - advisory: lattice drift ≤ 5 % vs initial

T2 bands_dos checks (uses multiple files via opts)
  - SCF converged on scf.out
  - "JOB DONE." on scf_out and bands_out
  - bands.dat parseable, non-empty, monotone k-path
  - nbnd ≥ n_electrons/2 + (6 for insulator, 10 for metal)
  - Fermi finite & |E_F| < 50 eV
  - dos.dat columns ≥ 2, rows > 10, E grid covers E_F ± 2 eV
  - bandgap in [0, 12] eV (heuristic plausibility)
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import qe_tools
from agent.tools.structured.context import ToolContext


# Input-side relaxation targets (mirror v4 system_prompt + qe_input_lint).
# These are what the pw.x input asks the relaxation to *reach*; they are NOT
# the verifier's acceptance bar (see below).
FORC_CONV_THR  = 1.0e-4    # Ry/Bohr; tighter than QE's 1e-3 default
ETOT_CONV_THR  = 1.0e-5    # Ry
PRESS_CONV_THR = 0.05      # kbar

# Verifier ACCEPTANCE thresholds for the *relaxed result*. Forces/stress are
# not bench-scored (the bench scores lattice params, symmetry, magnetization);
# they are advisory diagnostics here. The authoritative "the relaxation
# finished" gate is pw.x's own BFGS-converged marker (relax_converged). The
# stress acceptance bar is QE's default press_conv_thr (0.5 kbar) — the 0.05
# input target is unrealistically tight to re-assert on noisy magnetic SCF and
# was a false-rejection source.
STRESS_ACCEPT_KBAR = 0.5   # kbar (QE default press_conv_thr)

# A spin-polarized (nspin=2) SCF that converges to |abs_mag| below this fell
# into the non-magnetic basin (a starting_magnetization=0 collapse) → wrong E_F
# and DOS (observed: Fe_bcc/CrO2/Ni_fcc → total_mag 0.00). Advisory-only
# diagnostic; the qe_input_lint starting_magnetization guard is the
# deterministic prevention. 0.1 Bohr is well below every M3 magnetic moment.
MAG_COLLAPSE_THR = 0.1

# Hard "is the file usable at all?" gates
MIN_FILE_BYTES = 1024

# Fix 1 (M3 T2 regression 2026-06-03): a bands.dat whose nks is far larger than
# any high-symmetry path is a dense automatic mesh (e.g. a DOS-NSCF grid)
# submitted by mistake — the scorer cannot label dense k-points and nulls all
# four energy-band fields. Real high-symmetry paths for the bench materials top
# out near ~320 points; 1000 leaves a wide margin against false rejection while
# still catching the observed nks=2052 dense-grid mix-up.
NKS_KPATH_MAX = 1000

# The 4-dp "highest occupied, lowest unoccupied level (ev)" line is the bench
# evaluator's preferred gap splitter (mirror of bench parse_homo_lumo_ev). pw.x
# prints it only when the bands NSCF uses occupations='fixed'; smearing omits it.
_HOMO_LUMO_RE = re.compile(
    r"highest occupied,\s+lowest unoccupied level\s*\(ev\)\s*:\s*"
    r"-?\d+\.\d+\s+-?\d+\.\d+",
    re.IGNORECASE,
)


def _has_homo_lumo_line(path: Path) -> bool:
    try:
        return bool(_HOMO_LUMO_RE.search(
            Path(path).read_text(encoding="utf-8", errors="ignore")))
    except OSError:
        return False


# Backstop markers for the M3 T2 dos_e_fermi fix. pw.x prints
# "number of k points=  N (tetrahedron method)" when occupations='tetrahedra',
# and "number of k points=  N  <type> smearing, width (Ry)=..." when smearing
# is on (any type). Insulators run occupations='fixed' → neither banner.
_TETRAHEDRON_RE = re.compile(r"tetrahedron method|tetrahedra used", re.IGNORECASE)
_SMEARING_BANNER_RE = re.compile(r"smearing,\s*width", re.IGNORECASE)


def _file_contains(path: Path, pattern: re.Pattern) -> bool:
    try:
        return bool(pattern.search(
            Path(path).read_text(encoding="utf-8", errors="ignore")))
    except OSError:
        return False


def _resolve_expect_gap(workdir: Path) -> bool | None:
    """Whether the material is expected to have a real PBE band gap.

    Read from `workdir/.config_advice.json` (written by the config_advisor
    preflight). Drives the bands-NSCF HOMO/LUMO-line check in verify_bands_dos.

      True  — non-metal with a genuine PBE gap → the bands NSCF must use
              occupations='fixed' so pw.x prints the HOMO/LUMO line.
      False — metal, or PBE-near-metal (smearing is the legitimate choice and
              no HOMO/LUMO line is expected) → skip the check.
      None  — advice missing/unreadable → unknown; keep the check advisory so
              we never hard-fail (false-reject) an unclassifiable material.
    """
    try:
        advice = json.loads((Path(workdir) / ".config_advice.json").read_text())
    except (OSError, ValueError):
        return None
    is_metal = advice.get("is_metal")
    if is_metal is None:
        return None
    if is_metal or advice.get("pbe_near_metal"):
        return False
    return True


@dataclass
class CheckResult:
    name: str
    ok: bool
    detail: str = ""

    def as_line(self) -> str:
        mark = "✓" if self.ok else "✗"
        return f"  {mark} {self.name}: {self.detail}".rstrip()


@dataclass
class VerifyReport:
    verdict: str                  # "pass" | "soft_fail" | "hard_fail"
    checks: list[CheckResult] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.verdict == "pass"

    def add(self, name: str, ok: bool, detail: str = "") -> None:
        self.checks.append(CheckResult(name=name, ok=ok, detail=detail))

    def finalize(self) -> None:
        # If any non-advisory check failed, hard_fail. soft_fail reserved for
        # advisory misses (e.g. lattice drift > 5 % when we don't know initial).
        if any(not c.ok for c in self.checks if not c.name.startswith("advisory:")):
            self.verdict = "hard_fail"
        elif any(not c.ok for c in self.checks):
            self.verdict = "soft_fail"
        else:
            self.verdict = "pass"

    def to_text(self) -> str:
        lines = [f"verdict: {self.verdict}"]
        lines.extend(c.as_line() for c in self.checks)
        lines.extend(f"  · {n}" for n in self.notes)
        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            "checks": [{"name": c.name, "ok": c.ok, "detail": c.detail} for c in self.checks],
            "notes": list(self.notes),
        }


def _has_job_done(path: Path) -> bool:
    try:
        with path.open("rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - 65536))
            return b"JOB DONE." in f.read()
    except OSError:
        return False


def _file_ok(path: Path) -> tuple[bool, str]:
    if not path.exists():
        return False, "missing"
    sz = path.stat().st_size
    if sz < MIN_FILE_BYTES:
        return False, f"only {sz} bytes (need ≥{MIN_FILE_BYTES})"
    return True, f"{sz} bytes"


# ─────────────────────────────────────────────────────────────────────
# Per-task verifiers
# ─────────────────────────────────────────────────────────────────────

def verify_vc_relax(out_path: Path, *, initial: dict | None = None,
                    symprec: float = 1e-3) -> VerifyReport:
    rep = VerifyReport(verdict="hard_fail")  # finalize() rewrites

    ok, detail = _file_ok(out_path)
    rep.add("file_present_and_sized", ok, f"{out_path.name}: {detail}")
    if not ok:
        rep.finalize()
        return rep

    rep.add("job_done_marker", _has_job_done(out_path), "literal 'JOB DONE.' in last 64 KB")

    try:
        parsed = qe_tools.parse_pw_output(str(out_path))
    except Exception as e:
        rep.add("parse_pw_output", False, f"{type(e).__name__}: {e}")
        rep.finalize()
        return rep

    rep.add("scf_converged", bool(parsed.get("convergence")),
            "convergence has been achieved")

    # Geometry-convergence gate — the authoritative signal that the relaxation
    # actually met its forc/press targets. pw.x prints "bfgs converged" exactly
    # when it does; a max-steps stop or a truncated/killed run is not converged.
    # This replaces forces/stress as the HARD convergence check (those are now
    # advisory, see below).
    relax_conv = parsed.get("relax_converged")
    rep.add("relaxation_converged", relax_conv is True,
            {True: "pw.x reported 'bfgs converged'",
             False: "relaxation hit the max-steps ceiling (not converged)",
             None: "no BFGS-converged marker (run incomplete / not a relax run)"}
            .get(relax_conv, str(relax_conv)))

    # Force diagnostic (ADVISORY — not bench-scored; the norm-vs-component
    # mismatch makes a hard threshold unreliable). relaxation_converged is the
    # real gate.
    import numpy as np
    forces = np.asarray(parsed.get("forces") or [])
    if forces.size:
        # qe_tools.parse_pw_output gives forces in eV/Å; threshold is in Ry/Bohr.
        # 1 Ry/Bohr ≈ 25.71104 eV/Å.
        EV_PER_A_PER_RY_PER_BOHR = 25.71104
        f_thr_ev_a = FORC_CONV_THR * EV_PER_A_PER_RY_PER_BOHR
        max_f = float(np.max(np.linalg.norm(forces, axis=1)))
        rep.add("advisory:forces_below_thr", max_f <= f_thr_ev_a,
                f"max|F|={max_f:.5g} eV/Å (thr={f_thr_ev_a:.3g})")
    else:
        rep.add("advisory:forces_present", False, "no FORCES block parsed")

    # Stress diagnostic (ADVISORY; acceptance bar = QE default 0.5 kbar).
    stress = parsed.get("stress_kbar")
    if stress is not None:
        s = np.asarray(stress)
        max_diag = float(max(abs(s[0, 0]), abs(s[1, 1]), abs(s[2, 2])))
        rep.add("advisory:stress_below_thr", max_diag <= STRESS_ACCEPT_KBAR,
                f"max|σ_diag|={max_diag:.5g} kbar (thr={STRESS_ACCEPT_KBAR})")
    else:
        rep.add("advisory:stress_present", False, "no STRESS block parsed")

    # Spacegroup + lattice (uses parse_structure logic)
    try:
        from agent.tools.structured.parse_structure import _parse_final_cell
        from pymatgen.core import Structure, Lattice
        from pymatgen.symmetry.analyzer import SpacegroupAnalyzer

        lattice, sp_pos, _ = _parse_final_cell(out_path)
        struct = Structure(Lattice(lattice),
                           [s for s, _ in sp_pos],
                           [c for _, c in sp_pos],
                           coords_are_cartesian=False)
        sga = SpacegroupAnalyzer(struct, symprec=symprec, angle_tolerance=5.0)
        sg_after = sga.get_space_group_number()
        a, b, c = struct.lattice.abc
        alpha, beta, gamma = struct.lattice.angles
        rep.notes.append(
            f"final: a={a:.4f} b={b:.4f} c={c:.4f} Å, "
            f"α={alpha:.2f} β={beta:.2f} γ={gamma:.2f}°, "
            f"sg={sg_after} ({sga.get_space_group_symbol()})"
        )

        if initial and "space_group_number" in initial:
            sg_before = int(initial["space_group_number"])
            rep.add("space_group_preserved", sg_after == sg_before,
                    f"after={sg_after}, expected={sg_before}")

        if initial and all(k in initial for k in ("a", "b", "c")):
            a0, b0, c0 = float(initial["a"]), float(initial["b"]), float(initial["c"])
            drift = max(abs(a - a0) / a0, abs(b - b0) / b0, abs(c - c0) / c0)
            rep.add("advisory:lattice_drift_under_5pct", drift <= 0.05,
                    f"max relative drift={drift:.3g}")

        # Angles should not drift from initial — works for both primitive
        # (60° for diamond/FCC) and conventional (90° for cubic) cells.
        if initial and all(k in initial for k in ("alpha", "beta", "gamma")):
            a0, b0, g0 = float(initial["alpha"]), float(initial["beta"]), float(initial["gamma"])
            ang_drift = max(abs(alpha - a0), abs(beta - b0), abs(gamma - g0))
            # Grader-aligned: the bench scores angles with an absolute 1° tol.
            # The old 0.5° cubic special-case was stricter than the grader and
            # false-rejected legit sub-degree relaxation drift. Stays HARD (a
            # miss here means the grader will also deduct → a replay is warranted).
            tol = 1.0
            rep.add("angles_preserved", ang_drift <= tol,
                    f"max angle drift from initial ({a0:.1f}/{b0:.1f}/{g0:.1f})°={ang_drift:.3f}°")

    except Exception as e:
        rep.add("structure_extraction", False, f"{type(e).__name__}: {e}")

    rep.finalize()
    return rep


def _is_moment_collapsed(scf_text: str) -> bool | None:
    """Classify a spin-polarized SCF's converged moment from its text:
      True  → |absolute magnetization| < MAG_COLLAPSE_THR (collapsed to the
              non-magnetic basin — a starting_magnetization=0 symptom),
      False → a finite moment was retained,
      None  → not spin-polarized (no 'absolute magnetization' line printed).
    Uses the LAST value (the converged one; SCF iterations print intermediates).
    """
    ms = re.findall(r"absolute magnetization\s*=\s*([-\d.]+)", scf_text)
    if not ms:
        return None
    return abs(float(ms[-1])) < MAG_COLLAPSE_THR


def verify_bands_dos(*, scf_out: Path, bands_out: Path,
                     bands_data: Path, dos_dat: Path,
                     nscf_dos_out: Path | None = None,
                     expect_gap: bool | None = None) -> VerifyReport:
    """Verify a bands+DOS run.

    expect_gap (see _resolve_expect_gap): True = non-metal that must emit the
    HOMO/LUMO line (occupations='fixed'); False = metal/near-metal (skip that
    check); None = unknown → advisory only.

    nscf_dos_out: the DOS-NSCF pw.x output the evaluator reads dos_e_fermi from.
    When provided, a tetrahedra DOS-NSCF on a smearing system (metal/magnetic)
    hard-fails — see the backstop check below.
    """
    rep = VerifyReport(verdict="hard_fail")

    for label, p in (("scf_out", scf_out), ("bands_out", bands_out),
                     ("bands_data", bands_data), ("dos_dat", dos_dat)):
        ok, detail = _file_ok(p)
        rep.add(f"file_{label}", ok, f"{p.name}: {detail}")

    # JOB DONE only mandatory on log files; .dat data files are exempt
    rep.add("job_done_scf_out", _has_job_done(scf_out), scf_out.name)
    rep.add("job_done_bands_out", _has_job_done(bands_out), bands_out.name)

    # Parse scf.out
    parsed = None
    try:
        parsed = qe_tools.parse_pw_output(str(scf_out))
        rep.add("scf_converged", bool(parsed.get("convergence")),
                "convergence has been achieved")
        ef = parsed.get("fermi_energy")
        fermi_ok = ef is not None and abs(float(ef)) < 50.0
        # ADVISORY: |E_F|<50 is a plausibility heuristic (not grader-scored). It
        # false-rejects heavy elements / non-standard energy references, which on
        # the 202-task set drives the false-reject → replay → wall-timeout cascade.
        rep.add("advisory:fermi_energy_sane", fermi_ok,
                f"E_F={ef} eV (advisory plausibility: finite and |E_F|<50)")
    except Exception as e:
        rep.add("parse_scf_out", False, f"{type(e).__name__}: {e}")

    # Magnetic moment-collapse backstop (advisory): an nspin=2 SCF that
    # converged to ~zero moment fell into the non-magnetic basin (the
    # starting_magnetization=0 bug) → wrong E_F & DOS. Advisory so it never
    # hard-rejects a genuinely non-magnetic result; the qe_input_lint
    # starting_magnetization guard is the deterministic prevention.
    try:
        collapsed = _is_moment_collapsed(
            scf_out.read_text(encoding="utf-8", errors="replace"))
        if collapsed is not None:
            rep.add("advisory:magnetic_moment_present", not collapsed,
                    "spin-polarized SCF converged to ~zero moment (collapsed to the "
                    "non-magnetic state — check starting_magnetization)" if collapsed
                    else "spin-polarized SCF retained a finite moment")
    except Exception:
        pass

    # bands.dat
    try:
        kpath, bands_eV = qe_tools.parse_bands_file(str(bands_data))
        rep.add("bands_data_parseable", kpath.size > 0 and bands_eV.size > 0,
                f"k-points={kpath.size}, bands shape={bands_eV.shape}")
        # Fix 1: reject a dense automatic mesh masquerading as a k-path.
        dense = kpath.size > NKS_KPATH_MAX
        rep.add("bands_kpath_not_dense_grid", not dense,
                f"nks={kpath.size} (>{NKS_KPATH_MAX} → looks like a dense "
                f"automatic mesh, not a high-symmetry k-path; submit the "
                f"crystal_b bands file)" if dense
                else f"nks={kpath.size} (≤{NKS_KPATH_MAX}, plausible k-path)")
        # nbnd sufficiency — heuristic: need at least some empty states above E_F
        if parsed and "nbnd" in parsed and parsed["nbnd"]:
            nbnd = int(parsed["nbnd"])
            rep.add("nbnd_present_in_scf", nbnd >= 4,
                    f"nbnd={nbnd} (need ≥ 4 for non-trivial bands)")
    except Exception as e:
        rep.add("bands_data_parseable", False, f"{type(e).__name__}: {e}")

    # dos.dat
    try:
        energies, dos, _ef = qe_tools.parse_dos_file(str(dos_dat))
        rep.add("dos_shape_ok", energies.size > 10 and dos.size == energies.size,
                f"rows={energies.size}")
        if parsed and parsed.get("fermi_energy") is not None:
            ef = float(parsed["fermi_energy"])
            span_ok = energies.min() <= ef - 2.0 and energies.max() >= ef + 2.0
            # ADVISORY: a ±2 eV coverage heuristic — wide-gap insulators and
            # non-standard DOS windows legitimately differ; don't hard-reject on it.
            rep.add("advisory:dos_window_covers_fermi_pm2eV", span_ok,
                    f"E_range=[{energies.min():.2f}, {energies.max():.2f}] eV, E_F={ef:.2f}")
    except Exception as e:
        rep.add("dos_dat_parseable", False, f"{type(e).__name__}: {e}")

    # Fix 2 (M3 T2 regression): for a non-metal, the bands NSCF must run with
    # occupations='fixed' so pw.x prints the "highest occupied, lowest
    # unoccupied level (ev)" line — the bench scorer's preferred gap splitter.
    # occupations='smearing' omits it, the scorer falls back to a meaningless
    # non-self-consistent k-path Fermi, and band_gap scores null (MnO case).
    # Metals (expect_gap=False) legitimately lack the line → skip. Unknown
    # classification (None) → advisory only, never a hard-fail.
    if expect_gap is True:
        rep.add("bands_nscf_homo_lumo_line", _has_homo_lumo_line(bands_out),
                "bands NSCF must use occupations='fixed' so pw.x prints "
                "'highest occupied, lowest unoccupied level (ev)' (the bench "
                "scorer's gap splitter); occupations='smearing' omits it → "
                "band_gap scored null")
    elif expect_gap is None:
        rep.add("advisory:bands_nscf_homo_lumo_line",
                _has_homo_lumo_line(bands_out),
                "non-metal bands NSCF should print 'highest occupied, lowest "
                "unoccupied level (ev)' via occupations='fixed' (metal → ignore)")

    # M3 T2 dos_e_fermi backstop. The evaluator reads dos_e_fermi STRICTLY from
    # the DOS-NSCF output, and GT computed it with mv-smearing. A tetrahedra
    # DOS-NSCF shifts a metal's Fermi by up to ~2 eV → dos_e_fermi scores 0
    # (observed: Cr_bcc −2.13 eV, Co_hcp +1.15 eV). qe_input_lint rewrites this at
    # write-time; this catches a raw-bash DOS-NSCF that bypassed the linter. The
    # "smearing system" signal is the SCF's own smearing banner (printed for
    # metals + magnetics; insulators use 'fixed' → no banner), with the
    # config-advice metal flag (expect_gap is False) as a fallback.
    if nscf_dos_out is not None:
        tetra = _file_contains(nscf_dos_out, _TETRAHEDRON_RE)
        scf_smearing = _file_contains(scf_out, _SMEARING_BANNER_RE)
        bad = tetra and (scf_smearing or expect_gap is False)
        rep.add("dos_nscf_not_tetrahedra", not bad,
                "DOS-NSCF used occupations='tetrahedra' on a metal/magnetic system "
                "→ dos_e_fermi mismatches GT (mv-smearing) by up to ~2 eV; re-run "
                "the DOS NSCF with occupations='smearing', smearing='mv', "
                "degauss=0.02" if bad else
                f"DOS-NSCF occupations ok (tetrahedra={tetra}, "
                f"scf_smearing={scf_smearing})")

    rep.finalize()
    return rep


# ─────────────────────────────────────────────────────────────────────
# T4 elastic (stress_strain_csv)
# ─────────────────────────────────────────────────────────────────────

_VOIGT_MODES = {"e1": 0, "e2": 1, "e3": 2, "e4": 3, "e5": 4, "e6": 5}


def _parse_stress_strain_csv(path: Path) -> list[tuple[str, float, list[float]]]:
    """(mode, amplitude, σ[6]) rows from a T4 stress_strain CSV. Mirrors the
    bench's parser shape: header/comment lines skipped, mode ∈ {e1..e6}."""
    rows: list[tuple[str, float, list[float]]] = []
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        s = line.strip()
        if not s or s.startswith("#") or s.lower().startswith("mode"):
            continue
        parts = [p.strip() for p in s.split(",")]
        if len(parts) != 8:
            continue
        mode = parts[0].lower()
        if mode not in _VOIGT_MODES:
            continue
        try:
            amp = float(parts[1])
            sigma = [float(x) for x in parts[2:8]]
        except ValueError:
            continue
        rows.append((mode, amp, sigma))
    return rows


def verify_elastic(csv_path: Path) -> VerifyReport:
    """T4 pre-submit gate on the stress_strain CSV.

    Replicates the bench's fit (one-hot Voigt lstsq → symmetrize) and hard-fails
    ONLY on the two bench-certain-zero signatures:
      · Born-unstable   — the scoring rule gates elastic_tensor_cij to 0 outright
      · all-negative eigenvalues — QE stress not negated (sign convention bug)
    Positive-definiteness is checked on the symmetrized Voigt matrix — congruent
    to the bench's Mandel form, so the sign signature is identical. Asymmetry /
    fit-RMSE are advisory only (A4 philosophy: never false-reject a scorable
    submission). Root cause this guards (Cu/CdTe batch 2026-06): improvised
    strain protocols (angle-locked "e1", per-strain (a,b,c,angles) round-trips)
    that scramble σ↔ε consistency → lopsided / indefinite fitted tensors.
    """
    import numpy as np

    rep = VerifyReport(verdict="hard_fail")
    ok, detail = _file_ok(csv_path)
    rep.add("file_present_and_sized", ok, f"{csv_path.name}: {detail}")
    if not ok:
        rep.finalize()
        return rep

    rows = _parse_stress_strain_csv(csv_path)
    rep.add("csv_parseable", len(rows) >= 6,
            f"{len(rows)} strain rows parsed (need ≥6; header "
            f"mode,amplitude,sigma_1_gpa..sigma_6_gpa; mode ∈ e1..e6)")
    if len(rows) < 6:
        rep.finalize()
        return rep

    # One-hot Voigt lstsq — same fit the bench runs on this CSV.
    eps = np.zeros((len(rows), 6))
    sig = np.zeros((len(rows), 6))
    for k, (mode, amp, s) in enumerate(rows):
        eps[k, _VOIGT_MODES[mode]] = amp
        sig[k, :] = s
    C = np.zeros((6, 6))
    resid: list[float] = []
    for i in range(6):
        sol, *_ = np.linalg.lstsq(eps, sig[:, i], rcond=None)
        C[i, :] = sol
        resid.extend((sig[:, i] - eps @ sol).tolist())
    rmse = float(np.sqrt(np.mean(np.square(resid))))

    asym = 0.0
    for i in range(6):
        for j in range(i + 1, 6):
            den = max(abs(C[i, j]), abs(C[j, i]))
            if den > 1e-9:
                asym = max(asym, abs(C[i, j] - C[j, i]) / den * 100.0)

    Cs = (C + C.T) / 2.0
    ev = np.sort(np.linalg.eigvalsh(Cs))
    neg_floor = -1e-3 * float(np.max(np.abs(ev))) if ev.size else 0.0
    sign_flipped = bool(ev[-1] < neg_floor)
    born_unstable = bool(ev[0] < neg_floor)

    rep.add("stress_sign_convention", not sign_flipped,
            "all fitted eigenvalues negative — QE prints compression-positive "
            "stress; NEGATE the stress tensor before writing the CSV"
            if sign_flipped else f"eigenvalue range [{ev[0]:.1f}, {ev[-1]:.1f}] GPa")
    rep.add("born_stability", not born_unstable,
            (f"fitted Cij not positive-definite (min eig {ev[0]:.1f} GPa) — the "
             "bench zeroes elastic_tensor_cij for this. Usual cause: strain/stress "
             "frame mismatch (strained cell round-tripped through (a,b,c,angles), "
             "or 'e1 = stretch |a| at fixed angles'). Rebuild each cell as "
             "H = H_ref @ (I+ε) written verbatim as CELL_PARAMETERS, re-extract σ.")
            if born_unstable else f"positive-definite (min eig {ev[0]:.1f} GPa)")
    rep.add("advisory:cij_symmetry", asym <= 25.0,
            f"max Cij asymmetry {asym:.0f}% (>25% suggests inconsistent strain "
            f"protocol; the bench symmetrizes but the spectrum stays skewed)"
            if asym > 25.0 else f"max Cij asymmetry {asym:.0f}%")
    rep.add("advisory:fit_rmse", rmse <= 1.0,
            f"lstsq residual RMSE {rmse:.2f} GPa"
            + (" (>1 GPa: noisy/inconsistent stress data)" if rmse > 1.0 else ""))

    rep.finalize()
    return rep


# ─────────────────────────────────────────────────────────────────────
# Tool entrypoint
# ─────────────────────────────────────────────────────────────────────

async def do_verify_run(args: dict, *, ctx: ToolContext) -> dict:
    """Verify a calc_id matches its expected task type.

    Args:
        calc_id        str — must be in the registry
        expected_type  one of {"vc_relax", "bands_dos_scf", "bands_dos_full"}
                       For bands_dos_full, the registry must have an SCF, a
                       bands calc, and bands.dat / dos.dat must be findable
                       via the explicit paths kwarg below.
        initial        dict — optional initial structure facts for T1
                       (a, b, c, alpha, beta, gamma, space_group_number,
                        crystal_system)
        paths          dict — for T2 only: {scf_out, bands_out, bands_data, dos_dat}
                       relative to workdir. Optional `nscf_dos_out` enables the
                       metal dos_e_fermi tetrahedra backstop.
    """
    try:
        calc_id = args["calc_id"]
        rec = ctx.calc_registry.get(calc_id)
        if rec is None:
            raise KeyError(f"unknown calc_id: {calc_id!r}")

        expected = args.get("expected_type", "vc_relax")
        initial = args.get("initial") or None

        if expected == "vc_relax":
            if not rec.output_path:
                raise FileNotFoundError(f"{calc_id} has no output yet")
            rep = verify_vc_relax(ctx.rel(rec.output_path), initial=initial,
                                  symprec=float(args.get("symprec", 1e-3)))
        elif expected == "bands_dos_full":
            paths = args.get("paths") or {}
            required = {"scf_out", "bands_out", "bands_data", "dos_dat"}
            missing = required - set(paths)
            if missing:
                raise ValueError(f"bands_dos_full needs paths={sorted(required)}; missing {sorted(missing)}")
            # nscf_dos_out is optional (back-compat) but drives the metal
            # dos_e_fermi tetrahedra backstop when present.
            nscf_dos_rel = paths.get("nscf_dos_out")
            rep = verify_bands_dos(
                scf_out=ctx.rel(paths["scf_out"]),
                bands_out=ctx.rel(paths["bands_out"]),
                bands_data=ctx.rel(paths["bands_data"]),
                dos_dat=ctx.rel(paths["dos_dat"]),
                nscf_dos_out=ctx.rel(nscf_dos_rel) if nscf_dos_rel else None,
                expect_gap=_resolve_expect_gap(ctx.workdir),
            )
        else:
            raise ValueError(f"unknown expected_type {expected!r}; "
                             f"use 'vc_relax' or 'bands_dos_full'")

        return {
            "content": [{"type": "text", "text": rep.to_text()}],
            "is_error": not rep.ok,
            "calc_id": calc_id,
            **rep.to_dict(),
        }
    except Exception as e:
        return {
            "content": [{"type": "text", "text": f"verify_run failed: {type(e).__name__}: {e}"}],
            "is_error": True,
        }
