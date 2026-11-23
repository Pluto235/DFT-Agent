"""
qe_tools.py — Helper utilities for Quantum ESPRESSO + ASE simulation projects.
The engineered solver uses these functions directly.

IMPORTANT: All numerical results (lattice constants, bulk moduli, band gaps,
magnetic moments, formation energies) MUST come from actual QE calculations
performed in this session. Never quote numbers from memory or training data.
If a calculation has not been run, say so explicitly — do not estimate or guess.

Usage:
    from qe_tools import (
        write_pw_input, parse_pw_output,
        write_pp_input, write_dos_input, write_bands_input,
        fit_eos, plot_eos, plot_band_structure, plot_dos_data,
        render_structure_png, embed_png,
        html_report_head, html_report_foot,
    )
"""

from __future__ import annotations

import os, sys, re, subprocess, json
from pathlib import Path

# Default OMP threads — matches the value advertised in the QE system prompt.
# Override globally via env OMP_NUM_THREADS before launching the agent.
DEFAULT_OMP_NUM_THREADS: str = "4"

def _matplotlib_pyplot():
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    return plt


def _numeric_threshold(value, *, name: str) -> float:
    try:
        return float(str(value).replace("D", "E").replace("d", "e"))
    except (TypeError, ValueError) as e:
        raise ValueError(f"{name} must be numeric, got {value!r}") from e


# ── Deployment env injection ──────────────────────────────────────────────
#
# The host's pw.x may be a custom GPU build (compiled against NVIDIA HPC SDK)
# whose runtime libraries aren't on the default PATH/LD_LIBRARY_PATH. The
# operator pins these via env vars set in the WarpDrive process; build_subprocess_env
# applies them when launching pw.x. All vars are optional — without them this
# function returns os.environ.copy() with only OMP_NUM_THREADS defaulted.
#
# WARPDRIVE_QE_HPC_SDK_ROOT     — NVIDIA HPC SDK root (auto-derives PATH/LD)
# WARPDRIVE_QE_PATH_PREPEND     — explicit ':'-separated PATH prefix
# WARPDRIVE_QE_LD_LIBRARY_PATH_PREPEND — explicit ':'-separated LD prefix
# WARPDRIVE_QE_OMPI_MCA         — JSON dict {flag: value} → OMPI_MCA_<flag>=<value>

def build_subprocess_env() -> dict:
    env = os.environ.copy()
    env.setdefault("OMP_NUM_THREADS", DEFAULT_OMP_NUM_THREADS)

    sdk_root = env.get("WARPDRIVE_QE_HPC_SDK_ROOT", "").strip()
    path_prepend = env.get("WARPDRIVE_QE_PATH_PREPEND", "").strip()
    ld_prepend = env.get("WARPDRIVE_QE_LD_LIBRARY_PATH_PREPEND", "").strip()

    if sdk_root and not path_prepend:
        path_prepend = f"{sdk_root}/compilers/bin:{sdk_root}/comm_libs/mpi/bin"
    if sdk_root and not ld_prepend:
        ld_prepend = f"{sdk_root}/compilers/lib:{sdk_root}/comm_libs/mpi/lib"

    if path_prepend:
        existing_path = env.get("PATH", "")
        env["PATH"] = path_prepend + (":" + existing_path if existing_path else "")
    if ld_prepend:
        existing_ld = env.get("LD_LIBRARY_PATH", "")
        env["LD_LIBRARY_PATH"] = ld_prepend + (":" + existing_ld if existing_ld else "")

    mca_json = env.get("WARPDRIVE_QE_OMPI_MCA", "").strip()
    if mca_json:
        try:
            mca = json.loads(mca_json)
            for flag, value in mca.items():
                env[f"OMPI_MCA_{flag}"] = str(value)
        except (ValueError, TypeError):
            print(f"  Warning: WARPDRIVE_QE_OMPI_MCA is not valid JSON, ignored.", file=sys.stderr)

    return env


# ── Pseudo-potential management ───────────────────────────────────────────

PSEUDO_DIR = Path.home() / "qe_pseudos"
try:
    PSEUDO_DIR.mkdir(exist_ok=True)
except OSError:
    # Bench tasks use pre-staged ./pseudos/. A read-only HOME should not make
    # importing this helper module fail before the agent can use local files.
    pass

# SSSP precision library bundled with conda-forge sssp-precision package.
# Installed at: $CONDA_PREFIX/share/sssp/precision/
# Operator override via WARPDRIVE_QE_SSSP_DIR env var; otherwise auto-detected.
def _find_sssp_dir() -> Path | None:
    """Return path to SSSP precision dir, or None.

    Resolution order:
      1. WARPDRIVE_QE_SSSP_DIR env var (operator override)
      2. <sys.prefix>/share/sssp/precision/ (active conda env's bundled SSSP)
    """
    import sysconfig
    def _safe_is_dir(p: Path) -> bool:
        try:
            return p.is_dir()
        except (PermissionError, OSError):
            return False

    override = os.environ.get("WARPDRIVE_QE_SSSP_DIR", "").strip()
    if override:
        p = Path(override)
        if _safe_is_dir(p):
            return p

    conda_prefix = Path(sysconfig.get_config_var("prefix") or "")
    sssp = conda_prefix / "share" / "sssp" / "precision"
    if _safe_is_dir(sssp):
        return sssp

    return None

SSSP_DIR = _find_sssp_dir()



# ── pw.x input generation ─────────────────────────────────────────────────

def write_pw_input(
    atoms,
    prefix: str,
    outdir: str,
    pseudo_map: dict[str, str],
    calculation: str = "scf",
    ecutwfc: float = 60.0,
    ecutrho: float = 480.0,
    kpts=(8, 8, 8),
    kshift=(0, 0, 0),
    occupations: str = "smearing",
    smearing: str = "mv",
    degauss: float = 0.02,
    nspin: int = 1,
    starting_magnetization: dict = None,
    ion_dynamics: str = "bfgs",
    forc_conv_thr: float = 1e-3,
    etot_conv_thr: float = 1e-4,
    press_conv_thr: float = None,
    nstep: int = 100,
    extra_control: dict = None,
    extra_system: dict = None,
    extra_electrons: dict = None,
    kpath: list = None,
    filename: str = None,
) -> str:
    """
    Write a pw.x input file and return its path.

    Args:
        atoms: ASE Atoms object
        prefix: calculation prefix (label)
        outdir: QE scratch directory
        pseudo_map: {symbol: upf_path}
        calculation: 'scf', 'relax', 'vc-relax', 'nscf', 'bands'
        ecutwfc: plane-wave cutoff in Ry
        ecutrho: charge density cutoff in Ry
        kpts: Monkhorst-Pack grid (tuple) or list of explicit k-points
        kshift: MP grid offset
        occupations: 'smearing' (metals) or 'fixed' (insulators/semiconductors)
        smearing: 'mv' (Methfessel-Paxton, good for metals), 'gauss', 'fd'
        degauss: smearing width in Ry
        nspin: 1 (non-spin-polarized) or 2 (spin-polarized)
        starting_magnetization: {symbol: value} for nspin=2
        ion_dynamics: 'bfgs' (default) or 'sd'  (vc-relax & relax only)
        forc_conv_thr: force tolerance in Ry/Bohr (relax & vc-relax only)
        etot_conv_thr: total-energy tolerance in Ry (relax & vc-relax only)
        press_conv_thr: pressure tolerance in kbar (vc-relax only). Default
            None routes to 0.05, the cubic-tight value the prompt requires
            for the bench's spacegroup audit. Pass 0.5 for non-cubic targets.
        extra_control: extra key=value pairs for &CONTROL
        extra_system: extra key=value pairs for &SYSTEM
        extra_electrons: extra key=value pairs for &ELECTRONS (e.g.
            electron_maxstep, mixing_beta, mixing_mode, diagonalization,
            diago_thr_init) — used by the SCF convergence sub-agent's remedies.
        kpath: high-symmetry band path for a bands/nscf run. A list of rows
            [kx, ky, kz, npts] in crystal (fractional reciprocal) coordinates;
            emits a `K_POINTS crystal_b` card (overrides `kpts`). Write the
            seekpath point_coords verbatim — do NOT transform the basis.

    Returns:
        Path to the written input file.
    """
    from ase.io.espresso import write_espresso_in
    import io as _io

    # Build pseudo dict expected by ASE: {symbol: filename (basename only)}
    pseudopotentials = {}
    pseudo_dir_set = set()
    for sym, upf_path in pseudo_map.items():
        pseudopotentials[sym] = Path(upf_path).name
        pseudo_dir_set.add(str(Path(upf_path).parent))
    if len(pseudo_dir_set) != 1:
        raise ValueError(f"All pseudos must be in the same directory. Got: {pseudo_dir_set}")
    pseudo_dir = pseudo_dir_set.pop()

    if filename is None:
        filename = f"{prefix}.in"

    # Build the four namelists explicitly. ASE's write_espresso_in accepts
    # both a flat input_data dict (auto-routing keys to namelists by name)
    # and a nested {namelist: {...}} dict; the nested form is robust across
    # ASE versions and matches QE's actual file structure.
    control_data: dict = {
        "calculation": calculation,
        "prefix": prefix,
        "outdir": outdir,
        "pseudo_dir": pseudo_dir,
        "verbosity": "high",
        "tprnfor": True,
        "tstress": True,
    }
    system_data: dict = {
        "ecutwfc": ecutwfc,
        "ecutrho": ecutrho,
        "occupations": occupations,
        "nspin": nspin,
    }
    if occupations == "smearing":
        system_data["smearing"] = smearing
        system_data["degauss"] = degauss

    if nspin == 2 and starting_magnetization:
        # QE expects starting_magnetization(i) where i is species index
        species = list(dict.fromkeys(atoms.get_chemical_symbols()))
        for i, sym in enumerate(species, 1):
            if sym in starting_magnetization:
                system_data[f"starting_magnetization({i})"] = starting_magnetization[sym]

    if extra_system:
        system_data.update(extra_system)

    ions_data: dict = {}
    cell_data: dict = {}

    if calculation in ("relax", "vc-relax"):
        if calculation == "vc-relax":
            forc_conv_thr = min(_numeric_threshold(forc_conv_thr, name="forc_conv_thr"), 1.0e-4)
            etot_conv_thr = min(_numeric_threshold(etot_conv_thr, name="etot_conv_thr"), 1.0e-5)
        control_data["forc_conv_thr"] = forc_conv_thr
        control_data["etot_conv_thr"] = etot_conv_thr
        control_data["nstep"] = nstep
        ions_data["ion_dynamics"] = ion_dynamics

    if calculation == "vc-relax":
        cell_data["cell_dynamics"] = "bfgs"
        # press_conv_thr=None defaults to 0.05 kbar (cubic-tight). v1 NaCl
        # scored 0.75 because the LLM picked ≥0.5 here and let symmetry drift.
        press_value = (
            0.05 if press_conv_thr is None
            else min(_numeric_threshold(press_conv_thr, name="press_conv_thr"), 0.05)
        )
        cell_data["press_conv_thr"] = press_value

    # extra_control overrides last so callers can tweak any &CONTROL key.
    if extra_control:
        control_data.update(extra_control)
    if calculation == "vc-relax":
        control_data["forc_conv_thr"] = min(
            _numeric_threshold(control_data["forc_conv_thr"], name="forc_conv_thr"),
            1.0e-4,
        )
        control_data["etot_conv_thr"] = min(
            _numeric_threshold(control_data["etot_conv_thr"], name="etot_conv_thr"),
            1.0e-5,
        )

    electrons_data: dict = {}
    if extra_electrons:
        electrons_data.update(extra_electrons)

    input_data = {
        "control": control_data,
        "system": system_data,
        "electrons": electrons_data,
        "ions": ions_data,
        "cell": cell_data,
    }

    # Ensure parent dirs exist; pw.x crashes if outdir is missing, and many
    # callers pass filename="calcs/foo.in" without pre-mkdir of calcs/.
    Path(filename).parent.mkdir(parents=True, exist_ok=True)
    Path(outdir).mkdir(parents=True, exist_ok=True)

    # ASE write_espresso_in writes the file. ASE cannot emit QE's per-segment
    # `K_POINTS crystal_b`, so for a band path we let ASE write a gamma card
    # (kpts=None) and swap in our crystal_b block.
    buf = _io.StringIO()
    write_espresso_in(
        buf, atoms,
        input_data=input_data,
        pseudopotentials=pseudopotentials,
        kpts=(None if kpath else kpts),
        koffset=kshift,
    )
    content = buf.getvalue()

    if kpath:
        block = _kpoints_crystal_b_block(kpath)
        if "K_POINTS gamma" in content:
            content = content.replace("K_POINTS gamma", block, 1)
        else:  # pragma: no cover — ASE always emits a K_POINTS card
            content = content.rstrip() + "\n" + block + "\n"

    with open(filename, "w") as f:
        f.write(content)

    return filename


def _kpoints_crystal_b_block(kpath: list) -> str:
    """Render a `K_POINTS crystal_b` card from a list of [kx, ky, kz, npts] rows.

    Coordinates are crystal (fractional reciprocal) — written verbatim. `npts`
    is the number of points along the segment starting at that endpoint.
    """
    rows = list(kpath)
    if not rows:
        raise ValueError("kpath must be a non-empty list of [kx, ky, kz, npts] rows")
    lines = ["K_POINTS crystal_b", str(len(rows))]
    for r in rows:
        if len(r) != 4:
            raise ValueError(f"each kpath row must be [kx, ky, kz, npts]; got {r!r}")
        kx, ky, kz, n = r
        lines.append(f"{float(kx):.10f} {float(ky):.10f} {float(kz):.10f} {int(n)}")
    return "\n".join(lines)



# ── pp.x (post-processing / charge density) ──────────────────────────────

def write_pp_input(prefix: str, outdir: str, filplot: str = "charge.dat",
                   plot_num: int = 0, filename: str = "pp.in") -> str:
    """Write pp.x input. plot_num: 0=charge, 1=V_KS, 6=spin-up-minus-down."""
    content = f"""&INPUTPP
  prefix = '{prefix}',
  outdir = '{outdir}',
  filplot = '{filplot}',
  plot_num = {plot_num},
/
&PLOT
  nfile = 1,
  filepp(1) = '{filplot}',
  weight(1) = 1.0,
  iflag = 3,
  output_format = 6,
  fileout = '{filplot}.xsf',
/
"""
    with open(filename, "w") as f:
        f.write(content)
    return filename



# ── DOS calculation ───────────────────────────────────────────────────────

def write_dos_input(prefix: str, outdir: str, fildos: str = "dos.dat",
                    emin: float = -20, emax: float = 20, deltaE: float = 0.02,
                    filename: str = "dos.in") -> str:
    content = f"""&DOS
  prefix = '{prefix}',
  outdir = '{outdir}',
  fildos = '{fildos}',
  emin = {emin},
  emax = {emax},
  deltaE = {deltaE},
/
"""
    with open(filename, "w") as f:
        f.write(content)
    return filename



def write_bands_input(prefix: str, outdir: str, filband: str = "bands.dat",
                      filename: str = "bands_pp.in") -> str:
    content = f"""&BANDS
  prefix = '{prefix}',
  outdir = '{outdir}',
  filband = '{filband}',
/
"""
    with open(filename, "w") as f:
        f.write(content)
    return filename



# ── Output parsing ────────────────────────────────────────────────────────

def parse_pw_output(output_file: str) -> dict:
    """
    Parse pw.x output file. Returns dict with:
      - total_energy (eV): final total energy
      - alat (Angstrom): lattice parameter a
      - volume (Angstrom^3): unit cell volume
      - forces: list of [Fx, Fy, Fz] in eV/Angstrom per atom
      - stress_kbar (kbar): 3x3 stress tensor (QE's native unit) or None
      - stress_GPa  (GPa): 3x3 stress tensor (= stress_kbar / 10) or None
      - fermi_energy (eV): Fermi energy
      - convergence: True if SCF converged
      - nbnd: number of bands
      - nkpts: number of k-points
      - mag_total (mu_B): total magnetization (spin-pol only)
      - mag_absolute (mu_B): absolute magnetization (spin-pol only)
      - eigenvalues: last k-point eigenvalues (eV) or None
      - nstep_ions: number of ionic steps (relax/vc-relax)
      - final_alat (Angstrom): optimized |a_1| from last CELL_PARAMETERS block
        (vc-relax). For cubic cells this equals the conventional lattice
        constant; for non-cubic systems it is only the length of the first
        lattice vector — read CELL_PARAMETERS directly for b, c, angles.
        Falls back to initial alat for non-relax runs.

    All values are parsed from the output file; no values are assumed.
    """
    RY_TO_EV = 13.605693122994
    BOHR_TO_ANG = 0.529177210903

    result = {
        "total_energy": None, "alat": None, "volume": None,
        "forces": [], "stress_kbar": None, "stress_GPa": None, "fermi_energy": None,
        "convergence": False, "nbnd": None, "nkpts": None,
        "mag_total": None, "mag_absolute": None,
        "eigenvalues": None, "nstep_ions": 0,
        "final_alat": None, "final_volume": None,
        # relax/vc-relax geometry convergence (authoritative BFGS marker).
        # None = not a relax run (or marker absent / run truncated);
        # True = "bfgs converged"; False = hit the max-steps ceiling.
        "relax_converged": None,
    }

    with open(output_file) as f:
        lines = f.readlines()

    energies = []
    alats = []
    volumes = []
    forces_block = False
    stress_block = False
    force_lines = []
    stress_lines = []
    eig_block = False
    eig_lines = []

    for i, line in enumerate(lines):
        # Convergence
        if "convergence has been achieved" in line or "convergence achieved" in line.lower():
            result["convergence"] = True

        # Geometry (relax/vc-relax) convergence — authoritative pw.x markers.
        if "bfgs converged" in line or "End of BFGS Geometry Optimization" in line:
            result["relax_converged"] = True
        elif "maximum number of steps has been reached" in line.lower():
            # Don't downgrade a prior "converged"; only mark unconverged if
            # we haven't already seen the bfgs-converged marker.
            if result["relax_converged"] is None:
                result["relax_converged"] = False

        # Total energy
        m = re.search(r"!\s+total energy\s+=\s+([-\d.]+)\s+Ry", line)
        if m:
            energies.append(float(m.group(1)) * RY_TO_EV)

        # Lattice constant (alat in Bohr)
        m = re.search(r"lattice parameter \(alat\)\s+=\s+([\d.]+)\s+a\.u\.", line)
        if m:
            alats.append(float(m.group(1)) * BOHR_TO_ANG)

        # Unit cell volume
        m = re.search(r"unit-cell volume\s+=\s+([\d.]+)\s+\(a\.u\.\)\^3", line)
        if m:
            volumes.append(float(m.group(1)) * BOHR_TO_ANG**3)

        # Fermi energy
        m = re.search(r"the Fermi energy is\s+([-\d.]+)\s+ev", line, re.I)
        if m:
            result["fermi_energy"] = float(m.group(1))

        # Number of bands
        m = re.search(r"number of Kohn-Sham states\s*=\s*(\d+)", line)
        if m:
            result["nbnd"] = int(m.group(1))

        # Number of k-points
        m = re.search(r"number of k points\s*=\s*(\d+)", line)
        if m:
            result["nkpts"] = int(m.group(1))

        # Magnetization
        m = re.search(r"total magnetization\s+=\s+([-\d.]+)\s+Bohr", line)
        if m:
            result["mag_total"] = float(m.group(1))
        m = re.search(r"absolute magnetization\s+=\s+([-\d.]+)\s+Bohr", line)
        if m:
            result["mag_absolute"] = float(m.group(1))

        # Forces block — capture ONLY the physical per-atom forces.
        # With verbosity='high', pw.x additionally prints force-decomposition
        # sub-blocks ("The ionic contribution to forces", "The local
        # contribution to forces", ...) whose per-atom lines share the exact
        # "atom N type M force = ..." format. Those contributions are large
        # individually (e.g. ionic ~1.5 Ry/au) even when the net force is ~0,
        # so folding them into the force array yields a spurious huge max|F|
        # (~54 eV/Å) and false vc-relax rejections. The physical forces are
        # the first block; it ends at the first decomposition header
        # ("... to forces") or at the "Total force" summary line.
        if "Forces acting on atoms" in line:
            forces_block = True
            force_lines = []
        elif forces_block:
            m = re.search(r"atom\s+\d+.*?force\s*=\s*([-\d.]+)\s+([-\d.]+)\s+([-\d.]+)", line)
            if m:
                # Convert Ry/Bohr → eV/Ang
                f = [float(m.group(j)) * RY_TO_EV / BOHR_TO_ANG for j in (1, 2, 3)]
                force_lines.append(f)
            elif "Total force" in line or "to forces" in line:
                forces_block = False
                result["forces"] = force_lines

        # Stress block (Ry/Bohr^3)
        if "total   stress" in line:
            stress_block = True
            stress_lines = []
        elif stress_block:
            nums = re.findall(r"[-\d.]+", line)
            if len(nums) >= 6:
                # First 3 are Ry/Bohr^3, last 3 are kbar
                kbar = [float(nums[3]), float(nums[4]), float(nums[5])]
                stress_lines.append(kbar)
            if len(stress_lines) == 3:
                result["stress_kbar"] = stress_lines
                result["stress_GPa"] = [[v / 10.0 for v in row] for row in stress_lines]
                stress_block = False

        # Ionic steps
        m = re.search(r"number of ionic steps\s+=\s+(\d+)", line)
        if m:
            result["nstep_ions"] = int(m.group(1))

        # Final lattice constant after vc-relax
        if "End of BFGS" in line or "Final enthalpy" in line:
            # Capture next alat update
            pass

    if energies:
        result["total_energy"] = energies[-1]
    if alats:
        result["alat"] = alats[0]
        # Default final_alat to initial; overwritten below if a CELL_PARAMETERS
        # block is found (vc-relax case).
        result["final_alat"] = alats[0]

    # Parse the LAST CELL_PARAMETERS (alat= X) block to recover the optimized
    # lattice constant after vc-relax. `lattice parameter (alat) =` is printed
    # once at the start and never updated, so the trailing CELL_PARAMETERS block
    # is the only reliable source.
    last_cell_alat_bohr = None
    last_cell_a1_norm = None
    for i, line in enumerate(lines):
        m = re.search(r"CELL_PARAMETERS \(alat=\s*([\d.]+)\)", line)
        if m and i + 3 < len(lines):
            try:
                vec = [float(x) for x in lines[i + 1].split()[:3]]
                a1_norm = (vec[0]**2 + vec[1]**2 + vec[2]**2) ** 0.5
                last_cell_alat_bohr = float(m.group(1))
                last_cell_a1_norm = a1_norm
            except (ValueError, IndexError):
                continue
    if last_cell_alat_bohr is not None and last_cell_a1_norm is not None:
        result["final_alat"] = last_cell_alat_bohr * last_cell_a1_norm * BOHR_TO_ANG

    if volumes:
        result["volume"] = volumes[0]
        result["final_volume"] = volumes[-1]

    return result


def parse_dos_file(dos_file: str) -> tuple[np.ndarray, np.ndarray, float]:
    """Parse dos.dat. Returns (energies_eV, dos_states_per_eV, efermi_eV)."""
    import numpy as np

    efermi = 0.0
    with open(dos_file) as f:
        header = f.readline()
    m = re.search(r"EFermi\s*=\s*([-\d.]+)", header)
    if m:
        efermi = float(m.group(1))
    data = np.loadtxt(dos_file, comments="#")
    return data[:, 0], data[:, 1], efermi


def parse_bands_file(bands_file: str) -> tuple[np.ndarray, np.ndarray]:
    """
    Parse bands.dat or bands.dat.gnu (written by bands.x).
    Tries QE namelist format first (&plot nbnd=N, nks=M /), then gnuplot format.
    Returns (kpath_coords, eigenvalues_eV) as 2D arrays.
    kpath_coords: (nkpts,), eigenvalues_eV: (nbnd, nkpts).
    """
    import numpy as np
    import re
    from pathlib import Path

    text = Path(bands_file).read_text(encoding="utf-8", errors="ignore")

    # ── Try QE namelist format: &plot nbnd=N, nks=M / ──────────────────────
    m = re.search(r"&plot\s+nbnd\s*=\s*(\d+)\s*,\s*nks\s*=\s*(\d+)\s*/",
                  text, flags=re.IGNORECASE)
    if m:
        nbnd = int(m.group(1))
        nks_hdr = int(m.group(2))
        body = text[m.end():]
        floats = [float(x) for x in re.findall(
            r"-?\d+\.\d+(?:[eE][+-]?\d+)?", body)]
        block = 3 + nbnd  # 3 k-coords + nbnd eigenvalues per k-point
        nks = min(nks_hdr, len(floats) // block)
        if nks < 1:
            raise ValueError(f"bands.dat too short: {len(floats)} floats for "
                             f"block={block}, nks_hdr={nks_hdr}")
        eigs = []
        kdists = []
        prev = np.zeros(3)
        cum = 0.0
        for i in range(nks):
            off = i * block
            kxyz = np.array(floats[off:off + 3])
            dist = float(np.linalg.norm(kxyz - prev))
            cum += dist
            prev = kxyz
            kdists.append(cum)
            eigs.append(floats[off + 3: off + 3 + nbnd])
        kpath = np.array(kdists)
        bands = np.array(eigs).T  # shape (nbnd, nks)
        return kpath, bands

    # ── Fall back to gnuplot format (bands.dat.gnu) ─────────────────────────
    data = np.loadtxt(bands_file)
    # col 0 = k-path coord, col 1 = energy
    k_vals = np.unique(data[:, 0])
    nk = len(k_vals)
    nb = len(data) // nk
    if nb * nk != len(data):
        # Duplicate k-distances (e.g. at jump points): use length-based split
        nk = len(data) // max(1, nb)
        nb = len(data) // max(1, nk)
    kpath = data[:nk, 0]
    bands = data[:, 1].reshape(nb, nk)
    return kpath, bands


# ── EOS fitting ───────────────────────────────────────────────────────────

def fit_eos(volumes: list, energies: list, eos_name: str = "birchmurnaghan"):
    """
    Fit Birch-Murnaghan equation of state.
    Returns (v0_Ang3, e0_eV, B0_GPa, B_prime, eos_obj).

    volumes: list of unit-cell volumes in Angstrom^3
    energies: list of total energies in eV
    """
    from ase.eos import EquationOfState
    eos = EquationOfState(volumes, energies, eos=eos_name)
    v0, e0, B = eos.fit()
    B_GPa = B * 160.21766  # eV/Ang^3 → GPa
    return v0, e0, B_GPa, eos


# ── Plotting ──────────────────────────────────────────────────────────────

def plot_eos(volumes: list, energies: list, v0: float, e0: float, B_GPa: float,
             title: str = "Equation of State") -> str:
    """Return base64 PNG of E-V curve with Birch-Murnaghan fit."""
    from ase.eos import EquationOfState
    plt = _matplotlib_pyplot()

    eos = EquationOfState(volumes, energies, eos="birchmurnaghan")
    eos.fit()
    fig, ax = plt.subplots(figsize=(6, 4))
    eos.plot(ax=ax, show=False)
    ax.set_xlabel("Volume (Å³/cell)", fontsize=11)
    ax.set_ylabel("Energy (eV)", fontsize=11)
    ax.set_title(title, fontsize=12)
    ax.annotate(f"V₀ = {v0:.3f} Å³\nB₀ = {B_GPa:.1f} GPa",
                xy=(0.05, 0.95), xycoords="axes fraction",
                va="top", fontsize=9,
                bbox=dict(boxstyle="round,pad=0.3", fc="white", alpha=0.7))
    ax.grid(True, alpha=0.3)
    fig.tight_layout()
    return _fig_to_b64(fig)


def plot_band_structure(kpath: np.ndarray, bands_eV: np.ndarray,
                        efermi: float = 0.0, emin: float = -10, emax: float = 10,
                        high_sym_labels: list = None, high_sym_coords: list = None,
                        title: str = "Band Structure") -> str:
    """
    Plot band structure. Returns base64 PNG.

    kpath: 1D array of k-path coordinates
    bands_eV: 2D array (nbnd, nkpts)
    efermi: Fermi energy in eV (bands shifted so E_F = 0)
    high_sym_labels/coords: tick labels and their k-path positions
    """
    plt = _matplotlib_pyplot()

    fig, ax = plt.subplots(figsize=(7, 5))
    bands_shifted = bands_eV - efermi
    for band in bands_shifted:
        ax.plot(kpath, band, color="#2563eb", lw=0.8, alpha=0.85)
    ax.axhline(0, color="gray", ls="--", lw=0.7, label="E_F")
    ax.set_xlim(kpath[0], kpath[-1])
    ax.set_ylim(emin, emax)
    ax.set_ylabel("E − E_F (eV)", fontsize=11)
    ax.set_title(title, fontsize=12)
    if high_sym_labels and high_sym_coords:
        ax.set_xticks(high_sym_coords)
        ax.set_xticklabels(high_sym_labels, fontsize=11)
        for x in high_sym_coords:
            ax.axvline(x, color="gray", lw=0.5, alpha=0.5)
    ax.grid(True, alpha=0.15, axis="y")
    fig.tight_layout()
    return _fig_to_b64(fig)


def plot_dos_data(energies: np.ndarray, dos: np.ndarray, efermi: float = 0.0,
                  emin: float = -10, emax: float = 10,
                  title: str = "Density of States") -> str:
    """Plot DOS. Returns base64 PNG."""
    plt = _matplotlib_pyplot()

    fig, ax = plt.subplots(figsize=(4, 5))
    e = energies - efermi
    mask = (e >= emin) & (e <= emax)
    ax.plot(dos[mask], e[mask], color="#2563eb", lw=1.5)
    ax.fill_betweenx(e[mask], 0, dos[mask], where=(e[mask] <= 0),
                     alpha=0.3, color="#2563eb", label="Occupied")
    ax.fill_betweenx(e[mask], 0, dos[mask], where=(e[mask] > 0),
                     alpha=0.3, color="#dc2626", label="Unoccupied")
    ax.axhline(0, color="gray", ls="--", lw=0.8)
    ax.set_xlabel("DOS (states/eV)", fontsize=11)
    ax.set_ylabel("E − E_F (eV)", fontsize=11)
    ax.set_title(title, fontsize=12)
    ax.legend(fontsize=9)
    ax.grid(True, alpha=0.2, axis="x")
    fig.tight_layout()
    return _fig_to_b64(fig)


def render_structure_png(atoms, rotation: str = "40x,25y,10z",
                         title: str = None, supercell: tuple = (1, 1, 1)) -> str:
    """Return base64 PNG of an ASE Atoms structure."""
    from ase.visualize.plot import plot_atoms
    plt = _matplotlib_pyplot()

    if supercell != (1, 1, 1):
        atoms = atoms * supercell
    fig, ax = plt.subplots(figsize=(5, 5))
    # rotation must be a plain string, not a tuple
    plot_atoms(atoms, ax, radii=0.5, rotation=rotation)
    ax.set_axis_off()
    if title:
        ax.set_title(title, fontsize=11)
    fig.tight_layout()
    return _fig_to_b64(fig)


# ── HTML report helpers ───────────────────────────────────────────────────

def embed_png(b64: str, alt: str = "", caption: str = "") -> str:
    cap = f"<figcaption>{caption}</figcaption>" if caption else ""
    return f'<figure><img src="data:image/png;base64,{b64}" alt="{alt}" style="max-width:100%;border-radius:8px;">{cap}</figure>'


def html_report_head(title: str, summary: str, subtitle: str = "") -> str:
    sub = f'<p class="subtitle">{subtitle}</p>' if subtitle else ""
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="summary" content="{summary}">
<title>{title}</title>
<style>
  body {{ font-family: 'Inter', system-ui, sans-serif; max-width: 960px; margin: 0 auto;
          padding: 2rem 1.5rem; color: #1a1a2e; background: #f9f9fb; }}
  h1 {{ font-size: 1.6rem; font-weight: 700; margin-bottom: 0.25rem; }}
  h2 {{ font-size: 1.1rem; font-weight: 600; margin-top: 2rem;
        border-bottom: 1px solid #ddd; padding-bottom: 0.3rem; }}
  .subtitle {{ color: #666; font-size: 0.9rem; margin-bottom: 1.5rem; }}
  .card {{ background: white; border: 1px solid #e0e0e8; border-radius: 10px;
           padding: 1.25rem; box-shadow: 0 1px 4px rgba(0,0,0,0.05); margin: 1.5rem 0; }}
  .highlight {{ background: #f0fdf4; border: 1px solid #86efac; border-radius: 8px;
                padding: 0.75rem 1rem; font-size: 0.9rem; margin: 1rem 0; }}
  .warn {{ background: #fffbeb; border: 1px solid #fcd34d; border-radius: 8px;
           padding: 0.75rem 1rem; font-size: 0.88rem; margin: 1rem 0; }}
  .note {{ background: #f0f9ff; border-left: 3px solid #38bdf8;
           padding: 0.75rem 1rem; font-size: 0.88rem;
           border-radius: 0 6px 6px 0; margin: 1rem 0; }}
  figure {{ margin: 1.5rem 0; }}
  figcaption {{ font-size: 0.82rem; color: #666; margin-top: 0.4rem; text-align: center; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 0.9rem; }}
  th {{ background: #f0f0f5; text-align: left; padding: 0.5rem 0.75rem; }}
  td {{ padding: 0.5rem 0.75rem; border-bottom: 1px solid #eee; }}
  code {{ background: #f3f4f6; padding: 0.1em 0.3em; border-radius: 3px;
          font-family: monospace; font-size: 0.88em; }}
  pre {{ background: #1e1e2e; color: #cdd6f4; padding: 1rem; border-radius: 8px;
         overflow-x: auto; font-size: 0.82rem; line-height: 1.5; }}
</style>
</head>
<body>
<h1>{title}</h1>
{sub}"""


def html_report_foot() -> str:
    return "</body></html>"


# ── Internal helpers ──────────────────────────────────────────────────────

def _fig_to_b64(fig) -> str:
    import base64
    import io

    plt = _matplotlib_pyplot()
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=150, bbox_inches="tight")
    buf.seek(0)
    b64 = base64.b64encode(buf.read()).decode()
    plt.close(fig)
    return b64


# ── ALAMODE wrappers ──────────────────────────────────────────────────────
#
# ALAMODE is an alternative to phono3py for force-constant fitting and
# thermal conductivity. Use it when the user explicitly requests ALAMODE,
# the system is strongly anharmonic (PbTe, SnSe, halide perovskites), or
# 4th-order force constants are needed. Otherwise default to phono3py.

ALAMODE_BIN_DIR = os.environ.get(
    "WARPDRIVE_ALAMODE_BIN",
    "/opt/alamode/bin",
)


def verify_alamode(alm_bin: str = None, anphon_bin: str = None,
                   timeout: float = 10.0) -> dict:
    """
    Smoke-test ALAMODE binaries. Returns:
      {ok, alm, anphon, version, error, stderr_tail}
    Call this ONCE before any ALAMODE workflow.
    """
    alm_bin = alm_bin or os.path.join(ALAMODE_BIN_DIR, "alm")
    anphon_bin = anphon_bin or os.path.join(ALAMODE_BIN_DIR, "anphon")
    for name, p in [("alm", alm_bin), ("anphon", anphon_bin)]:
        if not Path(p).exists():
            return {"ok": False, "alm": alm_bin, "anphon": anphon_bin,
                    "version": None, "error": f"{name} binary not found at {p}",
                    "stderr_tail": None}
    env = build_subprocess_env()
    try:
        # alm with /dev/null exits non-zero (parse error) but loads libraries.
        proc = subprocess.run([alm_bin, "/dev/null"], capture_output=True,
                              env=env, timeout=timeout)
        out = (proc.stdout or b"").decode("utf-8", errors="replace") + \
              (proc.stderr or b"").decode("utf-8", errors="replace")
        if "error while loading shared libraries" in out:
            return {"ok": False, "alm": alm_bin, "anphon": anphon_bin,
                    "version": None,
                    "error": "alm shared library load failure (check libsymspg.so.1)",
                    "stderr_tail": out.splitlines()[-1] if out.strip() else None}
        m = re.search(r"Ver\.\s*([\d.]+)", out)
        version = m.group(1) if m else None
        return {"ok": True, "alm": alm_bin, "anphon": anphon_bin,
                "version": version, "error": None, "stderr_tail": None}
    except subprocess.TimeoutExpired:
        return {"ok": False, "alm": alm_bin, "anphon": anphon_bin,
                "version": None, "error": f"alm hung for >{timeout}s",
                "stderr_tail": None}
    except OSError as e:
        return {"ok": False, "alm": alm_bin, "anphon": anphon_bin,
                "version": None, "error": str(e), "stderr_tail": None}


def write_alm_input(filename: str, prefix: str, mode: str, atoms,
                    norder: int = 1, dfset: str = None,
                    cutoff: dict = None) -> str:
    """
    Write an ALAMODE 'alm' input file.

    Args:
        filename: output .in path
        prefix: ALM PREFIX (output files named <prefix>.fcs, <prefix>.xml, etc.)
        mode: 'suggest' (generate displacement patterns) or 'optimize' (fit FCs)
        atoms: ASE Atoms — typically a supercell (e.g., 2x2x2 of conventional cell)
        norder: 1=harmonic only, 2=cubic (3rd order), 3=quartic (4th order)
        dfset: DFSET filename — required for optimize mode
        cutoff: {(sym1, sym2): cutoff_bohr|None}, default None for all (harmonic)

    Returns: path to written input file.
    """
    cell_bohr = atoms.cell.array / 0.529177210903  # Å → Bohr
    species = list(dict.fromkeys(atoms.get_chemical_symbols()))
    sym_to_idx = {s: i + 1 for i, s in enumerate(species)}
    positions_frac = atoms.get_scaled_positions()

    L = []
    L.append("&general")
    L.append(f"  PREFIX = {prefix}")
    L.append(f"  MODE = {mode}")
    L.append(f"  NAT = {len(atoms)}")
    L.append(f"  NKD = {len(species)}")
    L.append(f"  KD = {' '.join(species)}")
    L.append("/")
    L.append("")
    L.append("&interaction")
    L.append(f"  NORDER = {norder}")
    L.append("/")
    L.append("")
    if mode == "optimize":
        if not dfset:
            raise ValueError("optimize mode requires dfset filename")
        L.append("&optimize")
        L.append(f"  DFSET = {dfset}")
        L.append("/")
        L.append("")
    L.append("&cutoff")
    if cutoff:
        for (s1, s2), c in cutoff.items():
            L.append(f"  {s1}-{s2} {'None' if c is None else c}")
    else:
        # Default: no cutoff for any pair (harmonic uses crystal symmetry)
        for s1 in species:
            for s2 in species:
                if sym_to_idx[s2] >= sym_to_idx[s1]:
                    L.append(f"  {s1}-{s2} None")
    L.append("/")
    L.append("")
    L.append("&cell")
    L.append("  1.0")
    for row in cell_bohr:
        L.append(f"  {row[0]:.10f}  {row[1]:.10f}  {row[2]:.10f}")
    L.append("/")
    L.append("")
    L.append("&position")
    for sym, pos in zip(atoms.get_chemical_symbols(), positions_frac):
        L.append(f"  {sym_to_idx[sym]} {pos[0]:.12f} {pos[1]:.12f} {pos[2]:.12f}")
    L.append("/")

    Path(filename).parent.mkdir(parents=True, exist_ok=True)
    with open(filename, "w") as f:
        f.write("\n".join(L) + "\n")
    return filename


def run_alm(input_file: str, output_file: str = None, alm_bin: str = None) -> str:
    """Run alm on input_file. Raises RuntimeError on failure."""
    alm_bin = alm_bin or os.path.join(ALAMODE_BIN_DIR, "alm")
    if not Path(alm_bin).exists():
        raise FileNotFoundError(
            f"alm not found at {alm_bin}. Set WARPDRIVE_ALAMODE_BIN env var, "
            f"or set WARPDRIVE_ALAMODE_BIN to the ALAMODE binary directory."
        )
    if output_file is None:
        output_file = input_file.replace(".in", ".out")

    env = build_subprocess_env()
    print(f"  Running: {alm_bin} {input_file}")
    with open(output_file, "w") as fout:
        proc = subprocess.run([alm_bin, input_file], stdout=fout,
                              stderr=subprocess.STDOUT, env=env,
                              cwd=str(Path(input_file).parent or "."))
    if proc.returncode != 0:
        with open(output_file) as f:
            tail = "".join(f.readlines()[-30:])
        print("\n--- ALM OUTPUT (last 30 lines) ---\n" + tail)
        raise RuntimeError(f"alm failed (exit {proc.returncode}). See {output_file}.")
    return output_file


def write_dfset_entry(out_lines: list, atoms_disp, forces_eV_per_A) -> None:
    """
    Append one supercell entry to a DFSET buffer.

    Args:
        out_lines: list to append to (caller writes to file at end)
        atoms_disp: ASE Atoms with displaced positions
        forces_eV_per_A: list of [Fx, Fy, Fz] in eV/Å (from parse_pw_output)
    """
    BOHR = 0.529177210903
    RY_PER_EV = 1.0 / 13.605693122994
    # ALAMODE DFSET wants displacements in Bohr and forces in Ry/Bohr
    # Caller must provide undisplaced reference positions via atoms_disp.info["ref_positions"]
    if "ref_positions" not in atoms_disp.info:
        raise ValueError("atoms_disp.info['ref_positions'] (undisplaced positions in Å) required")
    ref = atoms_disp.info["ref_positions"]
    cur = atoms_disp.get_positions()
    out_lines.append(f"# {atoms_disp.info.get('label', 'supercell')}")
    for i in range(len(atoms_disp)):
        d = (cur[i] - ref[i]) / BOHR  # Å → Bohr
        f = [fc * RY_PER_EV * BOHR for fc in forces_eV_per_A[i]]  # eV/Å → Ry/Bohr
        out_lines.append(f"  {d[0]: .12e}  {d[1]: .12e}  {d[2]: .12e}    "
                         f"{f[0]: .12e}  {f[1]: .12e}  {f[2]: .12e}")


def write_anphon_input(filename: str, prefix: str, fcsxml: str, atoms,
                       mass_dict: dict, kpts: tuple = (15, 15, 15),
                       tmin: float = 100.0, tmax: float = 1000.0, dt: float = 50.0,
                       mode: str = "phonons") -> str:
    """
    Write anphon input.

    mode: 'phonons' (band structure / DOS) or 'RTA' (BTE thermal conductivity κ(T))
    fcsxml: path to <prefix>.xml from a successful run_alm(optimize) call
    mass_dict: {symbol: atomic_mass_amu} — required, e.g. {"Si": 28.085}
    kpts: q-mesh for BZ integration
    """
    cell_bohr = atoms.cell.array / 0.529177210903
    species = list(dict.fromkeys(atoms.get_chemical_symbols()))

    L = []
    L.append("&general")
    L.append(f"  PREFIX = {prefix}")
    L.append(f"  MODE = {mode}")
    L.append(f"  FCSXML = {fcsxml}")
    L.append(f"  NKD = {len(species)}")
    L.append(f"  KD = {' '.join(species)}")
    L.append(f"  MASS = {' '.join(str(mass_dict[s]) for s in species)}")
    L.append("/")
    L.append("")
    if mode == "RTA":
        L.append("&analysis")
        L.append(f"  TMIN = {tmin}")
        L.append(f"  TMAX = {tmax}")
        L.append(f"  DT = {dt}")
        L.append("/")
        L.append("")
    L.append("&cell")
    L.append("  1.0")
    for row in cell_bohr:
        L.append(f"  {row[0]:.10f}  {row[1]:.10f}  {row[2]:.10f}")
    L.append("/")
    L.append("")
    L.append("&kpoint")
    L.append(f"  2 {kpts[0]} {kpts[1]} {kpts[2]}")
    L.append("/")

    Path(filename).parent.mkdir(parents=True, exist_ok=True)
    with open(filename, "w") as f:
        f.write("\n".join(L) + "\n")
    return filename


def run_anphon(input_file: str, output_file: str = None, anphon_bin: str = None) -> str:
    """Run anphon. Raises RuntimeError on failure."""
    anphon_bin = anphon_bin or os.path.join(ALAMODE_BIN_DIR, "anphon")
    if not Path(anphon_bin).exists():
        raise FileNotFoundError(f"anphon not found at {anphon_bin}.")
    if output_file is None:
        output_file = input_file.replace(".in", ".out")

    env = build_subprocess_env()
    print(f"  Running: {anphon_bin} {input_file}")
    with open(output_file, "w") as fout:
        proc = subprocess.run([anphon_bin, input_file], stdout=fout,
                              stderr=subprocess.STDOUT, env=env,
                              cwd=str(Path(input_file).parent or "."))
    if proc.returncode != 0:
        with open(output_file) as f:
            tail = "".join(f.readlines()[-30:])
        print("\n--- ANPHON OUTPUT (last 30 lines) ---\n" + tail)
        raise RuntimeError(f"anphon failed (exit {proc.returncode}). See {output_file}.")
    return output_file


def parse_anphon_kl(kl_file: str) -> dict:
    """
    Parse anphon thermal-conductivity output (.kl file).
    Returns {"T": ndarray, "kappa_xx": ndarray, "kappa_yy": ndarray, "kappa_zz": ndarray}.
    For cubic systems kxx≈kyy≈kzz; report kxx as the scalar κ.
    """
    import numpy as np

    data = np.loadtxt(kl_file, comments="#")
    if data.ndim == 1:
        data = data.reshape(1, -1)
    return {
        "T": data[:, 0],
        "kappa_xx": data[:, 1],
        "kappa_yy": data[:, 2] if data.shape[1] > 2 else data[:, 1],
        "kappa_zz": data[:, 3] if data.shape[1] > 3 else data[:, 1],
    }
