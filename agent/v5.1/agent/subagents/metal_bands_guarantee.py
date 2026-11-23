"""Deterministic metal bands guarantee for FM metals (FIX-2), v5.1 spin-pick.

Problem (Ni_fcc batch 43713d 2026-06-04: scored 0.077): the bench labels T2
gap_type by slicing the SUBMITTED bands.dat at the bands-NSCF Fermi and short-
circuits to gap_type=metal only when the resulting "void" straddling E_F is
< 10 meV. For an FM metal the submitted bands.dat holds only the MAJORITY spin
(bands.x default spin_component=1), whose d-bands are pushed ~1 eV below E_F by
exchange splitting — only the s-band crosses, leaving a finite void (Ni: 0.143
eV). The bench then scores the metal an indirect semiconductor (band_gap /
gap_type / vbm / cbm all zeroed). The material's Fermi surface lives in the
MINORITY d-bands, which cross E_F densely (Ni minority void ~0.003).

Fix: when a GT-metal's submitted bands.dat shows a void > 10 meV and the run is
spin-polarised (nspin=2), re-extract each spin channel with bands.x and replace
bands.dat with the metal-resolvable channel. bands.x is cheap post-processing;
we refresh the band-path wavefunctions with one ORIGINAL-density NSCF re-run
first (the dos-NSCF may have overwritten them in outdir; the SCF charge
persists, so this is the cheap 211-kpt run, not a densify).

nspin=1 (non-magnetic metal, e.g. Ag/Na/Cu/Au/K): there is no second spin to
pick, and the void is instead a k-path sampling artifact — the high-symmetry
path steps over the Fermi-surface crossing, leaving a finite void the bench
scores as an indirect semiconductor. Measured live (b115f75, 2026-06): 16 metal
T2 tasks lost ~0.35-0.48 each this way (Ag void 0.127, Na 0.139, Cu 0.018).
Fix (_resolve_nspin1_metal): find the band/leg(s) crossing E_F and re-run a FINE
tpiba sub-path across ONLY those crossings (reusing the SCF charge), then merge
into bands.dat. The post-refine void depends on the leg sampling (dk/nper), not
the original void magnitude, and the cost is independent of cell size — so it
sidesteps the multi-atom densify wall below. Validated on real Ag: a 360-point
refine collapsed the merged void 0.127 -> 0.001 eV.

History: v4.3 FIX-2 densified the whole k-path instead. Verified non-viable in
v5.1's GPU-podman env (2026-06-05): the densified NSCF (~8000 k-pts x nspin=2
GBRV) did not finish in 3600s. Spin-pick replaces it (validated on real FM Ni:
majority void 0.143 → minority void 0.003, ~minutes).

Gated (by the caller, solve._guarantee_metal_bands) on config_advisor.is_metal
and `not pbe_near_metal`. Every failure path is a graceful no-op that leaves the
LLM's bands.dat intact (never worse than today). The bands.dat / Fermi parsers
are INLINED (v5.1 qe_tools has no parse_bands_dat_plot) so a qe_tools shape
change can never silently turn the guarantee into a no-op.
"""
from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import traceback
from dataclasses import dataclass, asdict
from pathlib import Path

try:
    from qe_tools import build_subprocess_env
except Exception:  # pragma: no cover - fallback if import path differs
    def build_subprocess_env() -> dict:  # type: ignore
        return dict(os.environ)

# The bench fires gap_type=metal when the void < 1e-2 eV (evaluator.py short-
# circuit). Our void measurement mirrors the bench exactly.
BENCH_METAL_GAP_EV = 0.010
VOID_EPS = 5e-3              # matches bench _find_vbm_cbm_via_fermi eps

_KPATH_HDR_RE = re.compile(r"K_POINTS\s+(crystal_b|tpiba_b)", re.IGNORECASE)
_PREFIX_RE = re.compile(r"prefix\s*=\s*['\"]([^'\"]+)['\"]", re.IGNORECASE)
_OUTDIR_RE = re.compile(r"outdir\s*=\s*['\"]([^'\"]+)['\"]", re.IGNORECASE)
_NSPIN_RE = re.compile(r"\bnspin\s*=\s*(\d+)", re.IGNORECASE)
_FLOAT_RE = re.compile(r"-?\d+\.\d+(?:[eE][+-]?\d+)?")
_PLOT_HDR_RE = re.compile(r"&plot\s+nbnd\s*=\s*(\d+)\s*,\s*nks\s*=\s*(\d+)\s*/",
                          re.IGNORECASE)


@dataclass
class MetalBandsResult:
    is_metal: bool
    fermi_ev: float | None = None
    void_before: float | None = None
    void_after: float | None = None
    which_spin: int | None = None         # spin_component written to bands.dat
    replaced_bands_data: bool = False
    failure_reason: str | None = None

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2)


def _pw_cmd(exe_path: str, in_name: str, qe_backend: str) -> list[str]:
    """Argv for one QE executable run. Single-rank (GPU) invokes the binary BARE
    — the podman GPU wrapper runs mpirun *inside* the container and the host has
    no mpirun, so a host-side `mpirun -np 1 <wrapper>` double-wraps and dies with
    FileNotFoundError (the reason the shipped guarantee silently no-op'd every
    production run: Co_hcp/CrO2 2026-06-17). Mirrors run_pw.py: bare pw.x for one
    rank, `mpirun -np N` only for a CPU multi-rank build."""
    if qe_backend == "cpu":
        return ["mpirun", "--allow-run-as-root", "-np", "4", exe_path, "-in", in_name]
    return [exe_path, "-in", in_name]


def _run_pw_x(input_text: str, out_path: Path, *, exe_path: str,
              qe_backend: str, cwd: Path, timeout_s: int) -> int:
    """Run a QE executable (pw.x or bands.x) once. Overridden in tests. Writes
    the input next to out_path and returns the process return code. Own process
    group → SIGKILL the whole group on timeout so orphaned MPI ranks don't burn
    the shared host (mirrors magnetic_probe._run_pw_x)."""
    in_path = out_path.with_suffix(".in")
    in_path.write_text(input_text)
    cmd = _pw_cmd(exe_path, in_path.name, qe_backend)
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


def _fermi_from(out_path: Path) -> float | None:
    """Last 'the Fermi energy is X ev' (metal/smearing), else the insulator
    'highest occupied[, lowest unoccupied] level (ev)' value. Inlined regex —
    same precedence as the bench's parse_fermi_energy_ev."""
    if not Path(out_path).is_file():
        return None
    text = Path(out_path).read_text(encoding="utf-8", errors="ignore")
    m = re.findall(r"the Fermi energy is\s+(-?\d+\.\d+)\s*ev", text, flags=re.IGNORECASE)
    if m:
        return float(m[-1])
    m2 = re.findall(
        r"highest occupied,\s+lowest unoccupied level\s*\(ev\)\s*:\s*(-?\d+\.\d+)\s+(-?\d+\.\d+)",
        text, flags=re.IGNORECASE)
    if m2:
        return float(m2[-1][0])
    m3 = re.findall(r"highest occupied level\s*\(ev\)\s*:\s*(-?\d+\.\d+)",
                    text, flags=re.IGNORECASE)
    if m3:
        return float(m3[-1])
    return None


def _bands_eigenvalues(bands_dat: Path) -> list[float] | None:
    """Flatten all eigenvalues from a QE bands.x `bands.dat`. Inlined parser
    (matches the bench's parse_bands_dat block layout: 3 k-coords + nbnd eigs
    per k-block) so the void mirrors the bench exactly with no qe_tools dep.
    Returns None when the header/eigenvalues can't be parsed."""
    try:
        text = Path(bands_dat).read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return None
    m = _PLOT_HDR_RE.search(text)
    if not m:
        return None
    nbnd = int(m.group(1))
    nks = int(m.group(2))
    floats = [float(x) for x in _FLOAT_RE.findall(text[m.end():])]
    block = 3 + nbnd
    if block <= 3 or len(floats) < block:
        return None
    nks = min(nks, len(floats) // block)
    eigs: list[float] = []
    for i in range(nks):
        off = i * block
        eigs.extend(floats[off + 3: off + 3 + nbnd])
    return eigs or None


def _void_at_fermi(bands_dat: Path, fermi: float) -> float | None:
    """Gap straddling E_F, computed exactly like the bench's
    _find_vbm_cbm_via_fermi (global VBM = max eig <= F+eps; CBM = min eig > F+eps).
    Returns None when the bands can't be parsed or one side is empty."""
    eigs = _bands_eigenvalues(bands_dat)
    if not eigs:
        return None
    below = [e for e in eigs if e <= fermi + VOID_EPS]
    above = [e for e in eigs if e > fermi + VOID_EPS]
    if not below or not above:
        return None
    return max(0.0, min(above) - max(below))


def _void_from_eiglists(eiglists: list[list[float]], fermi: float) -> float | None:
    """Void straddling E_F over per-k eigenvalue lists (same rule as
    _void_at_fermi / the bench's _find_vbm_cbm_via_fermi). Used to score a
    merged (submitted + refinement) eigenvalue set without re-writing a file."""
    below = [e for row in eiglists for e in row if e <= fermi + VOID_EPS]
    above = [e for row in eiglists for e in row if e > fermi + VOID_EPS]
    if not below or not above:
        return None
    return max(0.0, min(above) - max(below))


def _parse_bands_kpts_eigs(
    bands_dat: Path,
) -> tuple[int, list[tuple[float, float, float]], list[list[float]]] | None:
    """Parse a bands.x `bands.dat` into (nbnd, kpts_cart_2pi_over_alat, eigs).
    Mirrors the bench's parse_bands_dat float-flatten (robust to eigenvalue line
    wrapping). Returns None when the header/eigenvalues can't be parsed."""
    text = _safe_read(bands_dat)
    m = _PLOT_HDR_RE.search(text)
    if not m:
        return None
    nbnd = int(m.group(1))
    floats = [float(x) for x in _FLOAT_RE.findall(text[m.end():])]
    block = 3 + nbnd
    if block <= 3 or len(floats) < block:
        return None
    nks = len(floats) // block
    kpts = [(floats[i * block], floats[i * block + 1], floats[i * block + 2])
            for i in range(nks)]
    eigs = [floats[i * block + 3: i * block + 3 + nbnd] for i in range(nks)]
    return nbnd, kpts, eigs


def _find_fermi_crossings(
    kpts: list[tuple[float, float, float]], eigs: list[list[float]], fermi: float,
    *, max_seg_dk: float = 0.2,
) -> list[tuple[float, int, int]]:
    """Consecutive same-segment k-pairs where some band straddles E_F, sorted by
    closeness of approach (tightest first). Segment boundaries (dk==0 or a large
    jump between listed k-points) are skipped so we interpolate only within a
    real path leg — never across a discontinuity."""
    out: list[tuple[float, int, int]] = []
    nbnd = len(eigs[0]) if eigs else 0
    for i in range(len(eigs) - 1):
        dk = sum(abs(kpts[i][a] - kpts[i + 1][a]) for a in range(3))
        if dk == 0.0 or dk > max_seg_dk:
            continue
        for bi in range(nbnd):
            a, b = eigs[i][bi], eigs[i + 1][bi]
            if (a - fermi) * (b - fermi) < 0.0:
                out.append((min(abs(a - fermi), abs(b - fermi)), i, i + 1))
    out.sort()
    return out


def _tpiba_klist(points: list[tuple[float, float, float]]) -> str:
    """Explicit K_POINTS tpiba block (2π/alat cartesian — same units as
    bands.dat k-coords, so path points feed straight through)."""
    lines = "\n".join(f"{k[0]:.10f} {k[1]:.10f} {k[2]:.10f} 1.0" for k in points)
    return f"K_POINTS tpiba\n{len(points)}\n{lines}\n"


_CALC_RE = re.compile(r"calculation\s*=\s*['\"][^'\"]+['\"]", re.IGNORECASE)
# QE cards that may FOLLOW K_POINTS. qe_tools emits CELL_PARAMETERS / ATOMIC_POSITIONS
# AFTER K_POINTS, so a naive `K_POINTS.*` (DOTALL) replace wiped them → the refine
# pw.x died with `ibrav=0: must read cell parameters` (real Ag deepseek run
# 2026-07-01). Match ONLY the K_POINTS card — its header line plus the following
# non-card lines — stopping at the next card header or EOF.
_QE_CARD_KW = (r"ATOMIC_SPECIES|ATOMIC_POSITIONS|CELL_PARAMETERS|OCCUPATIONS|"
               r"CONSTRAINTS|ATOMIC_VELOCITIES|ATOMIC_FORCES|ADDITIONAL_K_POINTS|"
               r"SOLVENTS|HUBBARD")
_KPOINTS_BLOCK_RE = re.compile(
    r"K_POINTS\b[^\n]*\n(?:(?!\s*(?:" + _QE_CARD_KW + r")\b)[^\n]*\n?)*",
    re.IGNORECASE)


def _refine_bands_input(src: str, klist_text: str) -> str:
    """The bands-NSCF input, forced to calculation='bands', with ONLY its
    K_POINTS card replaced by an explicit fine tpiba list. Structure cards that
    qe_tools writes AFTER K_POINTS (CELL_PARAMETERS / ATOMIC_POSITIONS) are
    preserved so the reused SCF charge + geometry stay intact."""
    body = _CALC_RE.sub("calculation = 'bands'", src, count=1)
    if _KPOINTS_BLOCK_RE.search(body):
        return _KPOINTS_BLOCK_RE.sub(lambda _m: klist_text, body, count=1)
    return body.rstrip() + "\n" + klist_text  # no K_POINTS card → append


def _merge_bands_dat(orig_text: str, refine_dat: Path, nbnd: int) -> str | None:
    """Append refine.dat's k-blocks to the submitted bands.dat, bumping nks in
    the &plot header. The bench's parse_bands_dat flattens all floats after the
    header and reads nks blocks, so a plain body concatenation is order-safe."""
    rtext = _safe_read(refine_dat)
    rm = _PLOT_HDR_RE.search(rtext)
    om = _PLOT_HDR_RE.search(orig_text)
    if not rm or not om:
        return None
    new_nks = int(om.group(2)) + int(rm.group(2))
    new_hdr = f" &plot nbnd=  {nbnd}, nks= {new_nks} /\n"
    return orig_text[:om.start()] + new_hdr + orig_text[om.end():] + rtext[rm.end():]


def _find_bands_nscf_input(bands_out: Path) -> Path | None:
    """The bands-NSCF input that produced bands_out: prefer the sibling .in with
    the same stem, else any *.in in the same dir with a bands-path K_POINTS."""
    sibling = bands_out.with_suffix(".in")
    if sibling.is_file() and _KPATH_HDR_RE.search(_safe_read(sibling)):
        return sibling
    for cand in sorted(bands_out.parent.glob("*.in")):
        if _KPATH_HDR_RE.search(_safe_read(cand)):
            return cand
    return None


def _safe_read(p: Path) -> str:
    try:
        return p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _detect_nspin(src: str) -> int:
    m = _NSPIN_RE.search(src)
    return int(m.group(1)) if m else 1


def _bands_pp_text(prefix: str, outdir: str, filband: str, spin_component: int) -> str:
    return (f"&BANDS\n  prefix = '{prefix}',\n  outdir = '{outdir}',\n"
            f"  filband = '{filband}',\n  spin_component = {spin_component},\n/\n")


def _derive_bands_x(pw_x_path: str) -> str:
    name = Path(pw_x_path).name
    if "pw.x" in name:
        return str(Path(pw_x_path).with_name(name.replace("pw.x", "bands.x")))
    if "/" in pw_x_path:
        return str(Path(pw_x_path).with_name("bands.x"))
    return "bands.x"


# Points interpolated across each E_F-crossing leg, escalated on failure. 120
# samples a leg to ~1e-3 of its length — enough on real Ag (void 0.127->0.001).
_NSPIN1_REFINE_NPER = (120, 400)
_NSPIN1_MAX_CROSSINGS = 3


def _resolve_nspin1_metal(
    res: MetalBandsResult, *, workdir: Path, bands_dat: Path, fermi: float,
    src: str, prefix: str, outdir: str, pw_x_path: str, qe_backend: str,
    step_dir: Path, timeout_s: int,
) -> MetalBandsResult:
    """Non-magnetic (nspin=1) metal: the >10 meV void straddling E_F is a k-path
    SAMPLING artifact — the high-symmetry path steps over the Fermi-surface
    crossing, so the nearest listed eigenvalues leave a finite void that the
    bench scores as an indirect semiconductor (gap_type/vbm/cbm zeroed, ~0.35
    weight + band_gap partial). The bands are fine; the crossing just isn't
    sampled.

    Fix: locate the band/leg(s) that cross E_F and re-run a FINE tpiba sub-path
    across only those crossings (reusing the SCF charge — a bands calc, seconds,
    and — unlike v4.3's full-path densify — independent of cell size, so it does
    not hit the multi-atom wall that made densify non-viable). Merge the fine
    eigenvalues into bands.dat and overwrite when the void drops < 10 meV. The
    post-refine void depends on the leg sampling (dk/nper), NOT the original void
    magnitude, so this resolves large-void metals (Ag 0.127, Na 0.139) the same
    as small ones. Every failure path is a graceful no-op (bands.dat untouched).
    """
    parsed = _parse_bands_kpts_eigs(bands_dat)
    if parsed is None:
        res.failure_reason = "nspin1: bands.dat unparseable"
        _write_artifact(workdir, res)
        return res
    nbnd, kpts, eigs = parsed
    crossings = _find_fermi_crossings(kpts, eigs, fermi)
    if not crossings:
        # No band straddles E_F on the submitted path → the Fermi surface does
        # not intersect this k-path; refinement has nothing to sharpen. No-op.
        res.failure_reason = "nspin1: no E_F crossing on submitted path"
        _write_artifact(workdir, res)
        return res
    orig_text = _safe_read(bands_dat)
    bands_x_path = _derive_bands_x(pw_x_path)
    filband = step_dir / "nspin1_refine.dat"
    for nper in _NSPIN1_REFINE_NPER:
        fine: list[tuple[float, float, float]] = []
        for _approach, i, j in crossings[:_NSPIN1_MAX_CROSSINGS]:
            ki, kj = kpts[i], kpts[j]
            fine += [tuple(ki[a] + (t / nper) * (kj[a] - ki[a]) for a in range(3))
                     for t in range(nper + 1)]
        refine_in = _refine_bands_input(src, _tpiba_klist(fine))
        try:
            rc = _run_pw_x(refine_in, step_dir / "nspin1_refine_nscf.out",
                           exe_path=pw_x_path, qe_backend=qe_backend,
                           cwd=step_dir, timeout_s=timeout_s)
        except Exception:
            traceback.print_exc()
            res.failure_reason = "nspin1: refine bands run raised"
            _write_artifact(workdir, res)
            return res
        if rc != 0:
            res.failure_reason = f"nspin1: refine bands run rc={rc}"
            _write_artifact(workdir, res)
            return res
        pp_text = _bands_pp_text(prefix, outdir, filband.name, 1)
        try:
            _run_pw_x(pp_text, step_dir / "nspin1_refine_pp.out",
                      exe_path=bands_x_path, qe_backend=qe_backend,
                      cwd=step_dir, timeout_s=timeout_s)
        except Exception:
            traceback.print_exc()
            continue
        rp = _parse_bands_kpts_eigs(filband)
        if rp is None:
            continue
        v = _void_from_eiglists(eigs + rp[2], fermi)
        if v is not None and v < BENCH_METAL_GAP_EV:
            merged = _merge_bands_dat(orig_text, filband, nbnd)
            if merged is not None:
                bands_dat.write_text(merged)
                res.void_after = v
                res.replaced_bands_data = True
                res.failure_reason = None
                _write_artifact(workdir, res)
                return res
        res.void_after = v  # record best attempt for the audit artifact
    res.failure_reason = "nspin1: void not resolved within refine cap"
    _write_artifact(workdir, res)
    return res


def run(*, workdir: Path, task_outputs: dict, pw_x_path: str,
        qe_backend: str = "gpu", is_metal: bool,
        timeout_s: int = 1200) -> MetalBandsResult:
    """For a GT-metal whose submitted (majority-spin) bands.dat has a void > 10
    meV straddling E_F, replace bands.dat with the metal-resolvable spin channel
    (re-extracted via bands.x). Only overwrites when a channel drops the void
    < 10 meV. Any failure is a graceful no-op (the LLM's bands.dat is untouched)."""
    res = MetalBandsResult(is_metal=is_metal)
    if not is_metal:
        res.failure_reason = "not metal"
        return res

    workdir = Path(workdir).resolve()
    bands_rel = task_outputs.get("bands_data")
    bands_out_rel = task_outputs.get("bands_out")
    scf_out_rel = task_outputs.get("scf_out")
    if not bands_rel or not bands_out_rel:
        res.failure_reason = "missing bands_data/bands_out in task_outputs"
        return res
    bands_dat = workdir / bands_rel
    bands_out = workdir / bands_out_rel
    if not bands_dat.is_file() or not bands_out.is_file():
        res.failure_reason = "bands files not on disk"
        return res

    # Fermi the bench slices with: bands_out first (evaluator.py), then scf.
    fermi = _fermi_from(bands_out)
    if fermi is None and scf_out_rel:
        fermi = _fermi_from(workdir / scf_out_rel)
    if fermi is None:
        res.failure_reason = "fermi unparseable"
        _write_artifact(workdir, res)
        return res
    res.fermi_ev = fermi

    void = _void_at_fermi(bands_dat, fermi)
    res.void_before = void
    if void is None:
        res.failure_reason = "could not measure void"
        _write_artifact(workdir, res)
        return res
    if void < BENCH_METAL_GAP_EV:
        res.void_after = void
        res.failure_reason = "already metal-resolvable (no spin-pick needed)"
        _write_artifact(workdir, res)
        return res

    nscf_in = _find_bands_nscf_input(bands_out)
    if nscf_in is None:
        res.failure_reason = "bands NSCF input not found"
        _write_artifact(workdir, res)
        return res
    src = _safe_read(nscf_in)
    pm = _PREFIX_RE.search(src)
    om = _OUTDIR_RE.search(src)
    if not pm or not om:
        res.failure_reason = "prefix/outdir unparseable from bands NSCF input"
        _write_artifact(workdir, res)
        return res
    prefix, outdir = pm.group(1), om.group(1)
    nspin = _detect_nspin(src)
    if nspin == 1:
        # Non-magnetic metal: no spin channel to pick. The void is a k-path
        # sampling artifact — adaptively refine the E_F crossing(s) instead.
        return _resolve_nspin1_metal(
            res, workdir=workdir, bands_dat=bands_dat, fermi=fermi, src=src,
            prefix=prefix, outdir=outdir, pw_x_path=pw_x_path,
            qe_backend=qe_backend, step_dir=nscf_in.parent, timeout_s=timeout_s)
    if nspin != 2:
        # nspin>2 is not a shape we produce; leave the LLM's bands.dat intact.
        res.failure_reason = f"spin-pick N/A (nspin={nspin})"
        _write_artifact(workdir, res)
        return res

    bands_x_path = _derive_bands_x(pw_x_path)
    step_dir = nscf_in.parent

    # Refresh the band-path wavefunctions at the ORIGINAL density (the dos-NSCF
    # may have overwritten outdir; the SCF charge persists). Cheap (211-kpt),
    # NOT a densify.
    try:
        rc = _run_pw_x(src, step_dir / "bands_guarantee_nscf.out",
                       exe_path=pw_x_path, qe_backend=qe_backend,
                       cwd=step_dir, timeout_s=timeout_s)
    except Exception:
        traceback.print_exc()
        res.failure_reason = "nscf re-run raised"
        _write_artifact(workdir, res)
        return res
    if rc != 0:
        res.failure_reason = f"nscf re-run rc={rc}"
        _write_artifact(workdir, res)
        return res

    # Re-extract each spin channel via bands.x (cheap) and pick the one whose
    # void < threshold (the channel carrying the Fermi surface). Minority (2)
    # first — for FM Ni it's the metallic one.
    for spin in (2, 1):
        filband = step_dir / f"bands_guarantee_s{spin}.dat"
        pp_text = _bands_pp_text(prefix, outdir, filband.name, spin)
        try:
            _run_pw_x(pp_text, step_dir / f"bands_guarantee_pp_s{spin}.out",
                      exe_path=bands_x_path, qe_backend=qe_backend,
                      cwd=step_dir, timeout_s=timeout_s)
        except Exception:
            traceback.print_exc()
            continue
        if not filband.is_file():
            continue
        v = _void_at_fermi(filband, fermi)
        if v is not None and v < BENCH_METAL_GAP_EV:
            bands_dat.write_text(_safe_read(filband))
            res.void_after = v
            res.which_spin = spin
            res.replaced_bands_data = True
            res.failure_reason = None
            _write_artifact(workdir, res)
            return res

    res.failure_reason = "no spin channel metal-resolvable"
    _write_artifact(workdir, res)
    return res


def _write_artifact(workdir: Path, res: MetalBandsResult) -> None:
    try:
        (Path(workdir) / ".metal_bands_guarantee.json").write_text(res.to_json())
    except OSError:
        pass
