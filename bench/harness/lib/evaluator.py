"""Extractors for T1 (vc-relax) and T2 (bands+DOS) evaluator pipelines.

T1: extract_t1_fields — pymatgen SpacegroupAnalyzer on final CELL_PARAMETERS
T2: extract_t2_fields — band_gap / gap_type / VBM&CBM k-labels (via seekpath) /
    DOS Fermi energy / DOS curve from bands.x + dos.x outputs

Helper parsers (used by T2, also testable individually):
    parse_alat_from_scf_out       — celldm(1) → alat in Å
    parse_fermi_energy_ev         — SCF Fermi or highest-occupied level
    parse_homo_lumo_ev            — NSCF "highest occupied, lowest unoccupied"
    parse_bands_dat               — QE bands.x output (k_cart_2pi_alat + eigs)
    read_dos_dat                  — QE dos.x output (E, dos curve + EFermi header)
    match_k_to_seekpath_label     — fractional Euclidean match (with periodicity)
"""
import logging
import re
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


# QE pw.x vc-relax 输出末尾通常带：
#   CELL_PARAMETERS (alat= X.XXX) 或 (angstrom) — 3 x 3 矩阵
#   ATOMIC_POSITIONS (crystal|alat|angstrom) — 每行一个原子
#   End final coordinates
# 我们找 "最后一次" 出现的这两组块（vc-relax 会迭代多轮）。


_BOHR_TO_ANGSTROM = 0.5291772109

# 见 schema/fields/evaluator.md §「T2 bands_dos 的 k-path 约定」。0.05 来自
# t2_extract.py 自跑 GT pipeline 经验值；后续可能在 v1.3 调（见 diy-bench P2-2）。
_SEEKPATH_LABEL_THRESHOLD = 0.05


def extract_t1_fields(
    *,
    relax_out_path: Path,
    extraction_config: dict[str, Any],
) -> dict[str, Any]:
    """返回 {a, b, c, alpha, beta, gamma, space_group_number, point_group, crystal_system}."""
    text = Path(relax_out_path).read_text(encoding="utf-8", errors="replace")

    cell_block, cell_unit, alat_bohr = _find_last_cell_parameters(text)
    if cell_block is None:
        raise ValueError("Could not find final CELL_PARAMETERS block in relax.out")

    positions_block, positions_unit = _find_last_atomic_positions(text)
    if positions_block is None:
        raise ValueError("Could not find final ATOMIC_POSITIONS block in relax.out")

    # cell 转成 angstrom 单位的 3x3 matrix
    lattice_ang = _cell_to_angstrom(cell_block, cell_unit, alat_bohr)
    species, frac_coords = _parse_positions(positions_block, positions_unit, lattice_ang)

    a, b, c, alpha, beta, gamma = _lattice_params(lattice_ang)

    # pymatgen SpacegroupAnalyzer
    from pymatgen.core import Lattice, Structure
    from pymatgen.symmetry.analyzer import SpacegroupAnalyzer

    lattice = Lattice(lattice_ang)
    structure = Structure(lattice, species, frac_coords, coords_are_cartesian=False)
    symprec = float(extraction_config.get("symprec", 0.001))
    angle_tol = float(extraction_config.get("angle_tolerance", 5))
    sga = SpacegroupAnalyzer(structure, symprec=symprec, angle_tolerance=angle_tol)

    result: dict[str, Any] = {
        "a": a,
        "b": b,
        "c": c,
        "alpha": alpha,
        "beta": beta,
        "gamma": gamma,
        "space_group_number": sga.get_space_group_number(),
        "point_group": sga.get_point_group_symbol(),
        "crystal_system": sga.get_crystal_system(),
    }

    # 磁化字段（M3, nspin=2）：QE 每个 SCF iter 末尾打印
    #   "     total magnetization       =    15.23 Bohr mag/cell"
    # 取最后一次（vc-relax converged 帧）。nspin=1 不打印 → 不进 result，
    # 留 M1/M2 的 extracted_fields shape 不变。
    total_mag, abs_mag = _parse_magnetic_moments(text)
    if total_mag is not None:
        result["total_magnetization_per_cell"] = total_mag
    if abs_mag is not None:
        result["absolute_magnetization_per_cell"] = abs_mag

    return result


def _find_last_cell_parameters(text: str) -> tuple[list[list[float]] | None, str | None, float | None]:
    """找末尾的 CELL_PARAMETERS 块 (3x3)。返回 (matrix, unit, alat_bohr)。
    unit ∈ {"alat", "bohr", "angstrom"}；alat_bohr 只有 unit=='alat' 时填。"""
    pattern = re.compile(
        r"CELL_PARAMETERS\s*\(?\s*(alat|bohr|angstrom)?\s*=?\s*([0-9.eE+-]+)?\)?\s*\n"
        r"((?:[^\n]*\n){3})",
        re.IGNORECASE,
    )
    matches = list(pattern.finditer(text))
    if not matches:
        return None, None, None
    m = matches[-1]
    unit = (m.group(1) or "alat").lower()
    alat_val = float(m.group(2)) if m.group(2) else 1.0
    rows_text = m.group(3)
    matrix = []
    for line in rows_text.strip().splitlines()[:3]:
        vals = [float(x) for x in line.split()[:3]]
        if len(vals) != 3:
            return None, None, None
        matrix.append(vals)
    return matrix, unit, alat_val


def _find_last_atomic_positions(text: str) -> tuple[str | None, str | None]:
    """找末尾的 ATOMIC_POSITIONS 块 (到 End final coordinates 或下一行空行)。"""
    pattern = re.compile(
        r"ATOMIC_POSITIONS\s*\(?\s*(crystal|alat|bohr|angstrom)?\s*\)?\s*\n"
        r"((?:[A-Za-z][\w]*\s+[-+0-9.eE\s]+\n)+)",
        re.IGNORECASE,
    )
    matches = list(pattern.finditer(text))
    if not matches:
        return None, None
    m = matches[-1]
    unit = (m.group(1) or "alat").lower()
    return m.group(2), unit


def _cell_to_angstrom(
    cell_block: list[list[float]],
    unit: str,
    alat_bohr: float,
) -> list[list[float]]:
    if unit == "angstrom":
        factor = 1.0
    elif unit == "bohr":
        factor = _BOHR_TO_ANGSTROM
    elif unit == "alat":
        factor = alat_bohr * _BOHR_TO_ANGSTROM
    else:
        factor = 1.0
    return [[v * factor for v in row] for row in cell_block]


def _parse_positions(
    block: str,
    unit: str,
    lattice_ang: list[list[float]],
):
    """返回 (species_list, fractional_coords_list)。非 crystal 单位会转成 fractional。"""
    import numpy as np
    species = []
    coords_raw = []
    for line in block.strip().splitlines():
        parts = line.split()
        if len(parts) < 4:
            continue
        species.append(parts[0])
        coords_raw.append([float(parts[1]), float(parts[2]), float(parts[3])])
    coords_raw = np.array(coords_raw)

    if unit == "crystal":
        return species, coords_raw.tolist()
    # alat / bohr / angstrom → cart → fractional
    if unit == "bohr":
        coords_raw = coords_raw * _BOHR_TO_ANGSTROM
    elif unit == "alat":
        # alat 已经在 cell_block 处理里嵌了；这里当作 angstrom 近似
        # （MVP 覆盖 crystal 即可；alat 情形先简化）
        pass
    lat = np.array(lattice_ang)
    inv = np.linalg.inv(lat)
    frac = coords_raw @ inv
    return species, frac.tolist()


def _lattice_params(lat: list[list[float]]) -> tuple[float, float, float, float, float, float]:
    import numpy as np
    mat = np.array(lat)
    a_vec, b_vec, c_vec = mat[0], mat[1], mat[2]
    a = float(np.linalg.norm(a_vec))
    b = float(np.linalg.norm(b_vec))
    c = float(np.linalg.norm(c_vec))
    alpha = float(np.degrees(np.arccos(np.dot(b_vec, c_vec) / (b * c))))
    beta = float(np.degrees(np.arccos(np.dot(a_vec, c_vec) / (a * c))))
    gamma = float(np.degrees(np.arccos(np.dot(a_vec, b_vec) / (a * b))))
    return a, b, c, alpha, beta, gamma


def _parse_magnetic_moments(text: str) -> tuple[float | None, float | None]:
    """从 QE pw.x 输出取最后一次 (total_mag, abs_mag), 单位 Bohr mag/cell。

    nspin=2 才打印；nspin=1 (M1/M2) → (None, None)。vc-relax 多 SCF iter，
    每 iter 都打一行，取最后一次 = converged 值。镜像 m3_run.py 同名函数。

    格式样例：'     total magnetization       =    15.23 Bohr mag/cell'
    """
    total = None
    absol = None
    for line in text.splitlines():
        if "total magnetization" in line and "Bohr mag/cell" in line:
            try:
                total = float(line.split("=")[1].split()[0])
            except (IndexError, ValueError):
                pass
        elif "absolute magnetization" in line and "Bohr mag/cell" in line:
            try:
                absol = float(line.split("=")[1].split()[0])
            except (IndexError, ValueError):
                pass
    return total, absol


# ============================================================================
# T2: bands+DOS evaluator (qe_bands_dos_combined)
# ============================================================================
def parse_alat_from_scf_out(scf_out_path: Path) -> float:
    """Return alat in Å, parsed from a pw.x output (scf.out / bands_nscf.out).

    Prefer the higher-precision `celldm(1)= X.XXXXXX` line over the rounded
    `lattice parameter (alat) = X.XXXX a.u.` line. Both are in atomic units
    (bohr); convert to Å.

    Raises ValueError if neither line is present.
    """
    if not Path(scf_out_path).exists():
        raise ValueError(f"scf.out not found: {scf_out_path}")
    text = Path(scf_out_path).read_text(encoding="utf-8", errors="ignore")
    m = re.search(r"celldm\(1\)\s*=\s*([0-9.]+)", text)
    if m:
        return float(m.group(1)) * _BOHR_TO_ANGSTROM
    m2 = re.search(r"lattice parameter\s*\(alat\)\s*=\s*([0-9.]+)\s*a\.u\.", text,
                   flags=re.IGNORECASE)
    if m2:
        return float(m2.group(1)) * _BOHR_TO_ANGSTROM
    raise ValueError(f"Could not find celldm(1) or alat line in {scf_out_path}")


def parse_fermi_energy_ev(scf_out_path: Path) -> float | None:
    """Parse Fermi energy / highest-occupied level from a pw.x output.

    QE prints either:
      * "the Fermi energy is    X.XXXX ev"          (metallic / smearing)
      * "highest occupied level (ev):    X.XXXX"    (insulator, fixed occ)
      * "highest occupied, lowest unoccupied level (ev):  X  Y"  (insulator)

    Returns the *last* occurrence (final SCF iteration). Returns None if the
    file does not exist or no matching line is found.
    """
    if not Path(scf_out_path).exists():
        return None
    text = Path(scf_out_path).read_text(encoding="utf-8", errors="ignore")
    m = re.findall(r"the Fermi energy is\s+(-?\d+\.\d+)\s*ev", text, flags=re.IGNORECASE)
    if m:
        return float(m[-1])
    m2 = re.findall(
        r"highest occupied,\s+lowest unoccupied level\s*\(ev\)\s*:\s*(-?\d+\.\d+)\s+(-?\d+\.\d+)",
        text, flags=re.IGNORECASE,
    )
    if m2:
        return float(m2[-1][0])
    m3 = re.findall(r"highest occupied level\s*\(ev\)\s*:\s*(-?\d+\.\d+)",
                    text, flags=re.IGNORECASE)
    if m3:
        return float(m3[-1])
    return None


def parse_homo_lumo_ev(out_path: Path) -> tuple[float, float] | None:
    """Parse the 4-dp 'highest occupied, lowest unoccupied' line.

    Returns (homo, lumo) tuple or None (file missing / line absent → metal NSCF).
    Authoritative VBM/CBM splitter: bands.dat is rounded to 1 meV, but this line
    has 4 dp; using it avoids the spurious-gap bug seen in MgO at e40_d30.
    """
    if not Path(out_path).exists():
        return None
    text = Path(out_path).read_text(encoding="utf-8", errors="ignore")
    m = re.findall(
        r"highest occupied,\s+lowest unoccupied level\s*\(ev\)\s*:\s*(-?\d+\.\d+)\s+(-?\d+\.\d+)",
        text, flags=re.IGNORECASE,
    )
    if m:
        return (float(m[-1][0]), float(m[-1][1]))
    return None


# QE pw.x prints eigenvalues as fixed-width f9.4 (4 dp). findall on this pattern
# tolerates run-together values (e.g. deep semicore "-100.1234-99.5678").
_EIG_RE = re.compile(r"-?\d+\.\d{3,}")


def _eig_line_values(line: str) -> list[float] | None:
    """Return the eigenvalues on a pure-eigenvalue line, else None.

    A line counts as eigenvalue data iff it contains ≥1 float and nothing else
    but floats + whitespace. This rejects the `k = … ( N PWs) bands (ev):`
    header (residual text) and the `k( n) = (…) wk=` listing lines.
    """
    vals = _EIG_RE.findall(line)
    if not vals:
        return None
    if _EIG_RE.sub("", line).strip() != "":
        return None
    return [float(v) for v in vals]


def parse_bands_out_eigenvalues(bands_out_path: Path) -> dict:
    """Parse per-k eigenvalues (and spin channels) from a pw.x bands NSCF output.

    Unlike `bands.dat` (one `bands.x` pass = majority spin only), the pw.x output
    holds BOTH spin channels for an nspin=2 run, under `------ SPIN UP ------` /
    `------ SPIN DOWN ----------` markers. We read every `k = … bands (ev):`
    block and group by spin, preserving file k-order (so index i aligns with
    bands.dat k-point i).

    Returns {"nspin": 0|1|2,
             "eigs_up": list[list[float]],            # spin-up (or the only spin)
             "eigs_dn": list[list[float]] | None}.    # spin-down, None if nspin≠2
    nspin=0 (empty lists) when the file is missing or has no bands blocks.
    """
    if not Path(bands_out_path).exists():
        return {"nspin": 0, "eigs_up": [], "eigs_dn": None}
    lines = Path(bands_out_path).read_text(encoding="utf-8", errors="ignore").splitlines()

    blocks: list[tuple[int, list[float]]] = []  # (header_line_idx, eigenvalues)
    i, n = 0, len(lines)
    while i < n:
        if "bands (ev)" in lines[i]:
            header_idx = i
            eigs: list[float] = []
            j = i + 1
            while j < n and lines[j].strip() == "":  # skip blank(s) after header
                j += 1
            while j < n:  # collect contiguous pure-eigenvalue lines
                vals = _eig_line_values(lines[j])
                if vals is None:
                    break
                eigs.extend(vals)
                j += 1
            blocks.append((header_idx, eigs))
            i = j
        else:
            i += 1

    if not blocks:
        return {"nspin": 0, "eigs_up": [], "eigs_dn": None}

    # pw.x outputs often concatenate several reruns (agent retries append to the
    # same .out). Anchor on the FINAL run's spin sections, not the first — else
    # every later dump gets lumped into spin-down and the k-count check bails.
    su_lines = [idx for idx, ln in enumerate(lines) if "SPIN UP" in ln]
    sd_lines = [idx for idx, ln in enumerate(lines) if "SPIN DOWN" in ln]
    if su_lines and sd_lines and su_lines[-1] < sd_lines[-1]:
        su, sd = su_lines[-1], sd_lines[-1]  # last (final-run) spin markers
        eigs_up = [e for (ln, e) in blocks if su < ln < sd]
        eigs_dn = [e for (ln, e) in blocks if ln > sd]
        return {"nspin": 2, "eigs_up": eigs_up, "eigs_dn": eigs_dn}

    # nspin=1 (or malformed/partial spin ordering): take the final run's blocks,
    # i.e. those after the last "End of band structure calculation" if present.
    eob = [idx for idx, ln in enumerate(lines) if "End of band structure" in ln]
    start = eob[-1] if eob else -1
    eigs = [e for (ln, e) in blocks if ln > start]
    return {"nspin": 1, "eigs_up": eigs, "eigs_dn": None}


def _eigs_aligned(dat_eigs: list[list[float]], out_up_eigs: list[list[float]],
                  tol: float = 1e-2) -> bool:
    """Sanity-check that bands.dat (spin-up, 3 dp) lines up with the pw.x SPIN UP
    channel (4 dp), so appending SPIN DOWN by k-index is sound. Compares the
    sorted per-k eigenvalues at a few sampled k-points within `tol` (3-dp noise).
    """
    if len(dat_eigs) != len(out_up_eigs):
        return False
    n = len(dat_eigs)
    for ki in {0, n // 2, n - 1}:
        a, b = sorted(dat_eigs[ki]), sorted(out_up_eigs[ki])
        if len(a) != len(b):
            return False
        if any(abs(x - y) > tol for x, y in zip(a, b)):
            return False
    return True


def _union_spin_down_into(bands: dict, bands_out_path: Path,
                          is_metal_per_gt: bool | None):
    """For an nspin=2 run, union the spin-DOWN eigenvalues (from the pw.x bands
    output) into `bands["eigenvalues_ev"]` per k-point, IN PLACE.

    bands.dat from a single `bands.x` pass holds only the majority (spin-up)
    channel; an FM metal whose Fermi level is minority-spin-dominated then looks
    gapped. Merging both spins per k restores the true (metallic) band set
    before VBM/CBM detection.

    Scope (deliberate): gated to GT≠insulator (`is_metal_per_gt is not False`).
    For a metal, GT is "gap 0 / metal" regardless of spin method, so the union
    only makes agent extraction *match* GT. For GT insulators we leave bands.dat
    alone: the baked GT band_gap/VBM was generated single-spin
    (tools/gt_selfrun), so two-spin extraction there would diverge from GT — a
    magnetic-insulator two-spin pass needs GT regen too (follow-up).

    Returns a diagnostics note: True if unioned, a "skipped: …" string if it
    bailed (logged + surfaced for audit), or None for a no-op (nspin≠2, or GT
    insulator). Never raises — any failure falls back to bands.dat-only.
    """
    if is_metal_per_gt is False:
        return None  # GT insulator: out of scope (see docstring), no-op
    try:
        sp = parse_bands_out_eigenvalues(bands_out_path)
    except Exception as e:  # pragma: no cover - defensive
        return f"skipped: parse error {type(e).__name__}"
    if sp["nspin"] != 2 or sp["eigs_dn"] is None:
        return None  # nspin=1 (or unreadable): no-op, no diagnostics noise
    up, dn = sp["eigs_up"], sp["eigs_dn"]
    nks = bands["nks"]
    if len(up) != nks or len(dn) != nks:
        return (f"skipped: k-count mismatch "
                f"(bands.dat nks={nks}, out up={len(up)} dn={len(dn)})")
    if not _eigs_aligned(bands["eigenvalues_ev"], up):
        return "skipped: bands.dat/out spin-up eigenvalue mismatch"
    for ki in range(nks):
        bands["eigenvalues_ev"][ki] = sorted(bands["eigenvalues_ev"][ki] + dn[ki])
    logger.info("T2 nspin=2: unioned spin-down eigenvalues from %s into bands "
                "(nks=%d)", bands_out_path, nks)
    return True


def parse_bands_dat(bands_dat_path: Path) -> dict:
    """Parse a QE bands.x output (`bands.dat`).

    Format:
         &plot nbnd= N, nks= M /
                  k1x  k1y  k1z
           e1  e2  ... eN  (wrapping over multiple lines)
                  k2x  k2y  k2z
           ...

    k-coords are in 2π/alat cartesian units. Eigenvalues are in eV.

    Returns:
        nbnd, nks, kpoints_cart_2pi_over_alat (list[(kx,ky,kz)]),
        eigenvalues_ev (list[list[float]] of shape nks × nbnd)
    """
    text = Path(bands_dat_path).read_text(encoding="utf-8", errors="ignore")
    m = re.search(r"&plot\s+nbnd\s*=\s*(\d+)\s*,\s*nks\s*=\s*(\d+)\s*/", text,
                  flags=re.IGNORECASE)
    if not m:
        raise ValueError(f"bands.dat header not found in {bands_dat_path}")
    nbnd = int(m.group(1))
    nks = int(m.group(2))

    body = text[m.end():]
    floats = [float(x) for x in re.findall(r"-?\d+\.\d+(?:[eE][+-]?\d+)?", body)]

    # Each k-block = 3 (kx,ky,kz) + nbnd eigenvalues
    block = 3 + nbnd
    if len(floats) < nks * block:
        actual_nks = len(floats) // block
        if actual_nks < 1:
            raise ValueError(
                f"bands.dat truncated: have {len(floats)} floats, need {nks * block}")
        nks = actual_nks

    kpts: list[tuple[float, float, float]] = []
    eigs: list[list[float]] = []
    for i in range(nks):
        off = i * block
        kpts.append((floats[off], floats[off + 1], floats[off + 2]))
        eigs.append(floats[off + 3: off + 3 + nbnd])

    return {
        "nbnd": nbnd,
        "nks": nks,
        "kpoints_cart_2pi_over_alat": kpts,
        "eigenvalues_ev": eigs,
    }


def read_dos_dat(dos_dat_path: Path) -> dict:
    """Parse a QE dos.x output.

    Format:
        # E (eV)   dos(E)   Int dos(E)   EFermi = X.XXXX eV
        E_1  dos_1  int_1
        E_2  dos_2  int_2
        ...

    Returns: {energy_ev, dos, int_dos (or None), e_fermi_header (or None)}.
    """
    if not Path(dos_dat_path).exists():
        return {"energy_ev": [], "dos": [], "int_dos": None, "e_fermi_header": None,
                "_missing": True}
    text = Path(dos_dat_path).read_text(encoding="utf-8", errors="ignore")
    e_fermi_header = None
    energies, dos, int_dos = [], [], []
    for line in text.splitlines():
        s = line.strip()
        if not s:
            continue
        if s.startswith("#"):
            mef = re.search(r"EFermi\s*=\s*(-?\d+\.\d+)", s, flags=re.IGNORECASE)
            if mef:
                e_fermi_header = float(mef.group(1))
            continue
        parts = s.split()
        try:
            vals = [float(p) for p in parts]
        except ValueError:
            continue
        if len(vals) >= 2:
            energies.append(vals[0])
            dos.append(vals[1])
            if len(vals) >= 3:
                int_dos.append(vals[2])
    return {
        "energy_ev": energies,
        "dos": dos,
        "int_dos": int_dos if int_dos else None,
        "e_fermi_header": e_fermi_header,
    }


def match_k_to_seekpath_label(
    k_frac: tuple[float, float, float] | list[float] | None,
    seekpath_point_coords: dict[str, list[float]],
    threshold: float = _SEEKPATH_LABEL_THRESHOLD,
) -> str:
    """Match a fractional k-point to the closest seekpath high-symmetry label.

    Distance is plain Euclidean in fractional coordinates with periodic-image
    correction (so k=(1,0,0) maps to GAMMA via wrap). 当没有 label 在 threshold
    内时（之前返 "OFF_PATH"），改为返回 fractional coord 字符串
    "(x.xxx, y.yyy, z.zzz)"（杨博 0521 建议）— scoring 用 k_label_match rule
    解析 coord 字符串走 frac distance 评分，而不是 "OFF_PATH" 二元 exact 死板。

    k_frac is None: 返回 "OFF_PATH" sentinel（VBM/CBM 完全没定位到，无 coord 可用）。
    """
    if k_frac is None:
        return "OFF_PATH"
    fk = (float(k_frac[0]), float(k_frac[1]), float(k_frac[2]))
    best_label = None
    best_dist = float("inf")
    for label, coord in seekpath_point_coords.items():
        d = _frac_distance(fk, (float(coord[0]), float(coord[1]), float(coord[2])))
        if d < best_dist:
            best_dist = d
            best_label = label
    if best_dist > threshold or best_label is None:
        return f"({fk[0]:.3f}, {fk[1]:.3f}, {fk[2]:.3f})"
    return best_label


def _frac_distance(a: tuple[float, float, float], b: tuple[float, float, float]) -> float:
    """Minimum-image fractional Euclidean distance."""
    import math
    s = 0.0
    for ai, bi in zip(a, b):
        d = ai - bi
        d -= round(d)  # bring into [-0.5, 0.5]
        s += d * d
    return math.sqrt(s)


def _build_seekpath_input(initial_structure: dict) -> tuple:
    """Convert a bench task initial_structure dict to seekpath's input tuple.

    Returns (cell, positions, atomic_numbers) where cell is 3x3 lattice vectors
    (rows, in Å), positions is N×3 fractional crystal coords, and
    atomic_numbers is a list of int Z values.

    Tolerates two shapes for lattice fields (M1/M2 promote 写 dict, M3 promote
    历史上写 list；B 那侧统一前两者并存）：
      dict: {"a": ..., "b": ..., "c": ...} / {"alpha":..., "beta":..., "gamma":...}
      list: [a, b, c] / [alpha, beta, gamma]
    """
    from pymatgen.core import Element, Lattice
    lc = initial_structure["lattice_constants_angstrom"]
    la = initial_structure["lattice_angles_degree"]
    if isinstance(lc, dict):
        a_val, b_val, c_val = lc["a"], lc["b"], lc["c"]
    else:
        a_val, b_val, c_val = lc[0], lc[1], lc[2]
    if isinstance(la, dict):
        alpha_val, beta_val, gamma_val = la["alpha"], la["beta"], la["gamma"]
    else:
        alpha_val, beta_val, gamma_val = la[0], la[1], la[2]
    lat = Lattice.from_parameters(
        a=a_val, b=b_val, c=c_val,
        alpha=alpha_val, beta=beta_val, gamma=gamma_val,
    )
    cell = [list(row) for row in lat.matrix]
    positions = []
    numbers = []
    for site in initial_structure["atomic_positions_crystal"]:
        positions.append(list(site["position"]))
        # AFM 子晶格标号 'Fe1'/'Fe2' → 基元素 'Fe' (同 Z)。剥尾号与 GT 侧
        # tools/gt_selfrun/t2/t2_params.py:_atomic_species_block 保持一致，
        # 让 seekpath 看到晶体学（非磁）对称 —— 这正是 GT 坐标所在的基底。
        base = re.sub(r'\d+$', '', site["element"]) or site["element"]
        numbers.append(int(Element(base).Z))
    return (cell, positions, numbers)


# seekpath 默认 symprec=1e-5 对 agent-facing 结构的 ~1e-2 坐标噪声太紧 ——
# 畸变磁性原胞会塌成 P1 → 三斜通用标签 + 错基底。逐级放松直到命中题目已知的
# 晶体学空间群，evaluator 与 GT pipeline 才会落到同一个标准化原胞（frac 坐标
# 可比的前提）。见 plan-grill-me-bug-report-t2-*。
_SYMPREC_LADDER = (1e-5, 1e-3, 1e-2, 5e-2)


def _seekpath_to_target_sg(sp_input: tuple, target_sg: int | None) -> tuple:
    """Run seekpath, escalating symprec until the detected spacegroup matches the
    material's known target. Returns (sp_result, symprec_used).

    target_sg is the question JSON's material.space_group_number. We stop at the
    first ladder rung whose seekpath spacegroup_number == target_sg, so both the
    evaluator and the GT re-run tool land on the identical standardized primitive
    cell. Escalating *to a target* (rather than "most symmetric") also avoids
    over-symmetrizing borderline cells (e.g. CuO #15 → Cccm #66 at 5e-2).

    If target_sg is None (legacy tasks without space_group_number) accept the
    first non-P1 result. If no rung matches, return the loosest attempt and let
    the caller flag it via diagnostics. Hard failures (seekpath missing /
    malformed input) propagate to the caller's except.
    """
    import seekpath
    last = None
    for sp in _SYMPREC_LADDER:
        res = seekpath.get_path(sp_input, symprec=sp)
        last = (res, sp)
        sg = res.get("spacegroup_number")
        if target_sg is not None:
            if sg == target_sg:
                return res, sp
        elif sg and sg > 1:
            return res, sp
    return last


def _convert_kpts_cart_to_seekpath_frac(
    kpts_cart_2pi_alat: list[tuple[float, float, float]],
    alat_angstrom: float,
    reciprocal_primitive_per_A: list[list[float]],
) -> list[tuple[float, float, float]]:
    """Convert k-points from QE's bands.dat units (2π/alat cartesian) to
    seekpath primitive fractional coords.

    Math (physics convention a_i · b_j = 2π δ_ij):
        k_cart_per_A = k_cart_2pi_alat * (2π / alat_A)        # real 1/Å
        k_frac       = k_cart_per_A · inverse(B)
                       where B is seekpath's reciprocal_primitive_lattice
                       (rows = b_i in 1/Å; a @ B.T = 2π·I).
    """
    import numpy as np
    B = np.array(reciprocal_primitive_per_A)
    B_inv = np.linalg.inv(B)
    scale = 2.0 * np.pi / alat_angstrom
    out: list[tuple[float, float, float]] = []
    for k_cart in kpts_cart_2pi_alat:
        k_real_per_A = np.array(k_cart) * scale
        k_frac = k_real_per_A @ B_inv
        out.append((float(k_frac[0]), float(k_frac[1]), float(k_frac[2])))
    return out


def _find_vbm_cbm_via_homo_lumo(
    eigenvalues_per_k: list[list[float]],
    homo: float,
    lumo: float,
) -> tuple[int, int, float, float]:
    """Find k-indices of VBM/CBM using NSCF authoritative homo/lumo splitter.

    Mid = (homo+lumo)/2 splits VB from CB at each k. Pick the k whose
    max(eigs ≤ mid) is closest to homo (VBM_idx) and similarly for CBM_idx.
    Returns (vbm_idx, cbm_idx, vbm_e=homo, cbm_e=lumo). Energies are the
    4-dp authoritative values from NSCF (not bands.dat 1 meV rounding).
    Returns (-1, -1, None, None) if no candidate found.
    """
    mid = 0.5 * (homo + lumo)
    vbm_idx, cbm_idx = -1, -1
    best_vbm_diff, best_cbm_diff = float("inf"), float("inf")
    for ki, eigs in enumerate(eigenvalues_per_k):
        if not eigs:
            continue
        below = [e for e in eigs if e <= mid]
        if below:
            cand = max(below)
            if abs(cand - homo) < best_vbm_diff:
                best_vbm_diff = abs(cand - homo)
                vbm_idx = ki
        above = [e for e in eigs if e > mid]
        if above:
            cand = min(above)
            if abs(cand - lumo) < best_cbm_diff:
                best_cbm_diff = abs(cand - lumo)
                cbm_idx = ki
    if vbm_idx < 0 or cbm_idx < 0:
        return -1, -1, None, None
    return vbm_idx, cbm_idx, homo, lumo


def _find_vbm_cbm_via_fermi(
    eigenvalues_per_k: list[list[float]],
    fermi_ev: float,
    eps: float = 5e-3,
) -> tuple[int, int, float | None, float | None]:
    """Fallback: use Fermi level as splitter when NSCF was run with smearing
    (no homo/lumo line). Each k contributes a local VBM (max eig ≤ Fermi+eps)
    and CBM (min eig > Fermi+eps). Global VBM = max over k; CBM = min over k.

    `eps=5e-3` (5 meV) absorbs bands.dat 1 meV rounding noise: a band whose
    true energy lies just below Fermi may round to just above (or vice versa);
    eps gives slack so a band ~Fermi doesn't flip categories.

    Returns (vbm_idx, cbm_idx, vbm_e, cbm_e). If max(VBM_per_k) ≥ min(CBM_per_k)
    the gap is non-positive → still returned; caller decides metal-or-not.
    """
    vbm_idx, cbm_idx = -1, -1
    vbm_e, cbm_e = -float("inf"), float("inf")
    for ki, eigs in enumerate(eigenvalues_per_k):
        if not eigs:
            continue
        below = [e for e in eigs if e <= fermi_ev + eps]
        above = [e for e in eigs if e > fermi_ev + eps]
        if below:
            cand = max(below)
            if cand > vbm_e:
                vbm_e = cand
                vbm_idx = ki
        if above:
            cand = min(above)
            if cand < cbm_e:
                cbm_e = cand
                cbm_idx = ki
    if vbm_idx < 0 or cbm_idx < 0:
        return -1, -1, None, None
    return vbm_idx, cbm_idx, vbm_e, cbm_e


def _none_t2_result() -> dict:
    """All 6 fields = None. Used when extraction cannot proceed (missing files,
    seekpath failure, etc). Scoring layer judges as fail.

    vbm_k_frac / cbm_k_frac 是 info 字段（display 用，前端在 OFF_PATH cell
    下方展示坐标）；不进 evaluator.extract_fields 也不进 field_weights，
    scoring 路径忽略。
    """
    return {
        "band_gap_ev": None,
        "gap_type": None,
        "vbm_k_label": None,
        "cbm_k_label": None,
        "vbm_k_frac": None,
        "cbm_k_frac": None,
        "dos_e_fermi": None,
        "dos_curve": None,
        "diagnostics": None,
    }


def _metal_t2_result(dos_e_fermi: float | None, dos_curve: dict | None,
                     diagnostics: dict | None = None) -> dict:
    """Metal short-circuit: gap=0, gap_type='metal', labels='metal',
    k_frac=None (metals 无单点 VBM/CBM). `diagnostics` carries audit info such
    as the nspin=2 spin-union note when the metal call follows a union."""
    return {
        "band_gap_ev": 0.0,
        "gap_type": "metal",
        "vbm_k_label": "metal",
        "cbm_k_label": "metal",
        "vbm_k_frac": None,
        "cbm_k_frac": None,
        "dos_e_fermi": dos_e_fermi,
        "dos_curve": dos_curve,
        "diagnostics": diagnostics,
    }


def extract_t2_fields(
    *,
    scf_out_path: Path,
    bands_out_path: Path,
    bands_dat_path: Path,
    dos_dat_path: Path,
    nscf_dos_out_path: Path,
    initial_structure: dict[str, Any],
    extraction_config: dict[str, Any],
    dos_dat_already_relative: bool = False,    # NEW (S6, 2026-05-17): legacy-detect support
    is_metal_per_gt: bool | None = None,       # NEW (2026-05-18, 杨博): insulator→VBM, metal→nscf EF
    target_spacegroup: int | None = None,      # NEW: material.space_group_number → seekpath symprec escalation target
) -> dict[str, Any]:
    """Extract the 6 T2 evaluator fields from a bands+DOS run.

    Inputs (all paths point into the agent's workdir):
        scf_out_path:    pw.x SCF output (provides alat + Fermi fallback for splitter)
        bands_out_path:  pw.x NSCF-bands output (provides 4-dp HOMO/LUMO line)
        bands_dat_path:  bands.x output `bands.dat` (k_cart_2pi_alat + eigs)
        dos_dat_path:    dos.x output `dos.dat` (E, dos curve)
        nscf_dos_out_path: pw.x NSCF-DOS output (the run that produced wavefunctions
            for dos.x — NOT the bands NSCF). MUST contain the
            "the Fermi energy is X.XXXX ev" line. (杨博 2026-05-17 拍板)
        initial_structure: bench task material.initial_structure dict (used for
            seekpath.get_path → label dictionary + reciprocal lattice)
        extraction_config: reserved (currently unused; future: OFF_PATH thr etc)
        dos_dat_already_relative: When True, skip the `e - reference` subtraction
            step for `dos_curve.energy_ev` (caller already detected that dos.dat
            col 1 is in relative coordinates, e.g. for old runs where the
            agent mutated dos.dat post-dos.x). Default False = normal flow.
        is_metal_per_gt: 杨博 2026-05-18 拍板的 VBM-vs-EF 分流开关。
            - True (材料客观是金属): dos_e_fermi = nscf_dos_out 的 EF；
              dos_curve.energy_ev = E - nscf_EF（化学势在 EF 附近 DOS≠0、唯一确定）
            - False (材料客观是绝缘体/半导体): dos_e_fermi = VBM（带顶）；
              dos_curve.energy_ev = E - VBM（绝缘体 EF 在 gap 内多解、tetrahedra
              数值落点不可重现，用 VBM 这个物理量做参考）
            - None (默认，legacy back-compat): 走 metal 路径（= nscf EF）
            evaluator 只有在 caller (run.py / rerun_eval) 从 GT 拿到 gap_type
            后才能传这个参数；evaluator 自己不读 ground_truth。
        target_spacegroup: material.space_group_number（晶体学空间群号）。传入后
            seekpath 调用走 _seekpath_to_target_sg 逐级放松 symprec 直到命中此群，
            保证 evaluator 与 GT pipeline 落到同一标准化原胞。None → 接受首个非-P1。

    Returns: {band_gap_ev, gap_type, vbm_k_label, cbm_k_label, dos_e_fermi,
              dos_curve, diagnostics}. dos_e_fermi 含义随 is_metal_per_gt 切换；
              diagnostics 记 symprec_used / spacegroup_number 等（scoring 忽略）。
    """
    del extraction_config  # reserved for future use
    # ------------------------------------------------------------------
    # 1) Parse files (graceful failure: return Nones if critical files missing)
    # ------------------------------------------------------------------
    if not Path(scf_out_path).exists() or not Path(bands_out_path).exists() \
            or not Path(bands_dat_path).exists():
        # Without scf.out (alat) / bands_out (homo_lumo) / bands.dat (eigs),
        # we cannot determine k-labels nor the gap. dos_dat alone isn't enough.
        return _none_t2_result()

    try:
        alat_A = parse_alat_from_scf_out(scf_out_path)
    except ValueError:
        return _none_t2_result()

    homo_lumo = parse_homo_lumo_ev(bands_out_path)
    fermi_scf = parse_fermi_energy_ev(scf_out_path)
    try:
        bands = parse_bands_dat(bands_dat_path)
    except ValueError:
        return _none_t2_result()
    # nspin=2: bands.dat is majority-spin only; union spin-down from the pw.x
    # bands output so FM metals (minority-spin-dominated E_F) aren't mislabeled
    # as gapped. Mutates bands["eigenvalues_ev"] in place; benefits both the
    # fermi and homo/lumo VBM/CBM paths below. (See evaluator.md §T2 spin union.)
    union_note = _union_spin_down_into(bands, bands_out_path, is_metal_per_gt)
    if isinstance(union_note, str):
        logger.warning("T2 nspin=2 spin union %s", union_note)
    union_diag = {"nspin2_union": union_note} if union_note is not None else None
    dos = read_dos_dat(dos_dat_path)
    # Fermi energy (杨博 2026-05-17 拍板): 严格只从 dos NSCF 输出抽，
    # 不接受 dos.dat header / scf.out 任何 fallback —— 抽不到 evaluator 直接 fail。
    dos_e_fermi = parse_fermi_energy_ev(nscf_dos_out_path)
    if dos_e_fermi is None:
        raise ValueError(
            f"dos_e_fermi: failed to parse Fermi energy from "
            f"nscf_dos_out_path={nscf_dos_out_path} — file may be missing or "
            f"NSCF did not converge (look for 'the Fermi energy is' line). "
            f"Agent must provide a valid DOS-NSCF pw.x output via task_outputs.nscf_dos_out."
        )
    # dos_curve.energy_ev: 相对参考能量（金属=nscf EF，绝缘体=VBM；杨博 2026-05-18）。
    # nscf_ef 是 dos.dat 的"原始"参考点 — agent 如果减过 EF（dos_dat_already_relative=
    # True），col1 = E - nscf_ef；如果没减，col1 = E_abs。
    # Helper: 给定 target_reference_ev 构造 dos_curve，自动处理两种 col1 语义。
    nscf_ef = dos_e_fermi  # snapshot before any later reassignment (insulator VBM path)
    def _build_dos_curve(target_reference_ev):
        if not dos["energy_ev"]:
            return None
        if dos_dat_already_relative:
            # col1 = E - nscf_ef → 转到 target: col1 + (nscf_ef - target)
            shift = nscf_ef - target_reference_ev
            energies_rel = [e + shift for e in dos["energy_ev"]]
        else:
            # col1 = E_abs → E - target
            energies_rel = [e - target_reference_ev for e in dos["energy_ev"]]
        return {"energy_ev": energies_rel, "dos": list(dos["dos"])}

    # 默认 dos_curve 用 nscf EF（适用于：metal 短路返回 + 早期 fail 路径 +
    # is_metal_per_gt=None 的 back-compat）。
    dos_curve_via_nscf_ef = _build_dos_curve(dos_e_fermi)
    dos_curve = dos_curve_via_nscf_ef  # default, may override at end

    # ------------------------------------------------------------------
    # 2) Choose splitter: NSCF homo/lumo line (preferred, 4 dp) or Fermi level
    # ------------------------------------------------------------------
    splitter_mode: str  # "homo_lumo" | "fermi" | "metal"
    if homo_lumo is not None:
        homo, lumo = homo_lumo
        gap_value = max(0.0, lumo - homo)
        # 议题 1 (2026-05-25, v2 §1-Q1) — 短路阈值 1e-3 → 1e-2，吸收 PBE 准金属
        # 数值噪声 (Ge ≤10 meV 那批救分)。同时加 is_metal_per_gt gate 防 GT=insulator
        # 但 agent 算出 5–10 meV gap 时误短路成 metal（gap_type/vbm/cbm 三分类全错
        # 丢 0.35 weight 塌方）。三态语义：True / None(legacy) 允许短路、False 拒绝。
        if gap_value < 1e-2 and is_metal_per_gt is not False:
            # Numerically near-zero gap = metal (MgO bug regression: trust line)
            return _metal_t2_result(dos_e_fermi, dos_curve, union_diag)
        splitter_mode = "homo_lumo"
    else:
        # Fallback: agent ran NSCF with smearing → no homo/lumo line. Use Fermi
        # as splitter. Source preference: bands_nscf.out > scf.out > dos.dat header
        fermi = (parse_fermi_energy_ev(bands_out_path)
                 or fermi_scf
                 or dos.get("e_fermi_header"))
        if fermi is None:
            return _metal_t2_result(dos_e_fermi, dos_curve, union_diag)
        splitter_mode = "fermi"
        # vbm_e/cbm_e/gap_value resolved from bands.dat below

    # ------------------------------------------------------------------
    # 3) seekpath: get high-sym labels (frac) + recip primitive lattice (2π/Å)
    # ------------------------------------------------------------------
    diagnostics: dict[str, Any] = {"target_spacegroup": target_spacegroup}
    if union_note is not None:
        diagnostics["nspin2_union"] = union_note
    try:
        sp_result, symprec_used = _seekpath_to_target_sg(
            _build_seekpath_input(initial_structure), target_spacegroup,
        )
    except Exception as e:
        # seekpath missing / structure malformed → cannot label k-points.
        # 不再静默：之前 bare except 把 crash/P1 全吞成 null，0.35 黑洞悄悄扣分数周。
        diagnostics["seekpath_error"] = f"{type(e).__name__}: {e}"
        logger.warning("T2 seekpath failed (k-labels → null): %s",
                       diagnostics["seekpath_error"])
        return _none_t2_result_partial(
            band_gap_ev=(gap_value if splitter_mode == "homo_lumo" else None),
            dos_e_fermi=dos_e_fermi, dos_curve=dos_curve, diagnostics=diagnostics,
        )
    sg_num = sp_result.get("spacegroup_number")
    diagnostics.update({
        "symprec_used": symprec_used,
        "spacegroup_number": sg_num,
        "spacegroup_international": sp_result.get("spacegroup_international"),
    })
    if target_spacegroup is not None and sg_num != target_spacegroup:
        diagnostics["sg_fallback"] = True
        logger.warning(
            "T2 seekpath did not reach target spacegroup %s (got %s at symprec=%s) "
            "— k-labels in a fallback basis, may not match GT.",
            target_spacegroup, sg_num, symprec_used,
        )
    point_coords_frac = sp_result["point_coords"]
    recip_primitive = sp_result["reciprocal_primitive_lattice"]  # 1/Å (a · b.T = 2π·I)

    # ------------------------------------------------------------------
    # 4) Convert bands.dat k from 2π/alat cartesian → seekpath primitive frac
    # ------------------------------------------------------------------
    kpts_seek_frac = _convert_kpts_cart_to_seekpath_frac(
        bands["kpoints_cart_2pi_over_alat"], alat_A, recip_primitive,
    )

    # ------------------------------------------------------------------
    # 5) Find VBM/CBM indices + energies (via chosen splitter)
    # ------------------------------------------------------------------
    if splitter_mode == "homo_lumo":
        vbm_idx, cbm_idx, vbm_e, cbm_e = _find_vbm_cbm_via_homo_lumo(
            bands["eigenvalues_ev"], homo, lumo,
        )
        # gap_value already set above
    else:
        vbm_idx, cbm_idx, vbm_e, cbm_e = _find_vbm_cbm_via_fermi(
            bands["eigenvalues_ev"], fermi,
        )
        if vbm_idx < 0 or cbm_idx < 0:
            return _metal_t2_result(dos_e_fermi, dos_curve, union_diag)
        gap_value = max(0.0, cbm_e - vbm_e)
        # 议题 1 (2026-05-25, v2 §1-Q1) — 同 homo_lumo path: 1e-2 + is_metal_per_gt gate
        if gap_value < 1e-2 and is_metal_per_gt is not False:
            return _metal_t2_result(dos_e_fermi, dos_curve, union_diag)

    if vbm_idx < 0 or cbm_idx < 0:
        return _none_t2_result_partial(
            band_gap_ev=gap_value, dos_e_fermi=dos_e_fermi, dos_curve=dos_curve,
            diagnostics=diagnostics,
        )

    vbm_k_frac = kpts_seek_frac[vbm_idx]
    cbm_k_frac = kpts_seek_frac[cbm_idx]
    vbm_label = match_k_to_seekpath_label(vbm_k_frac, point_coords_frac)
    cbm_label = match_k_to_seekpath_label(cbm_k_frac, point_coords_frac)

    # ------------------------------------------------------------------
    # 6) gap_type: direct iff VBM and CBM at the same k (in frac space)
    # ------------------------------------------------------------------
    d = _frac_distance(vbm_k_frac, cbm_k_frac)
    gap_type = "direct" if d < _SEEKPATH_LABEL_THRESHOLD else "indirect"

    # ------------------------------------------------------------------
    # 7) VBM 参考切换（杨博 2026-05-18）：GT 标的绝缘体 → 用 VBM 作 dos_e_fermi。
    # 走到这里说明 splitter 成功 + VBM/CBM 可定位 → vbm_e 可用。
    # 金属路径 (is_metal_per_gt=True / None) 走 _metal_t2_result 早返，
    # 不会到这里。所以这里只剩"GT 说绝缘体 / agent 也算出 gap"的情况。
    # ------------------------------------------------------------------
    if is_metal_per_gt is False and vbm_e is not None:
        dos_e_fermi = vbm_e
        dos_curve = _build_dos_curve(vbm_e)

    return {
        "band_gap_ev": gap_value,
        "gap_type": gap_type,
        "vbm_k_label": vbm_label,
        "cbm_k_label": cbm_label,
        # info-only fields (前端 OFF_PATH 时展示坐标比对，不进 scoring)
        "vbm_k_frac": [float(vbm_k_frac[0]), float(vbm_k_frac[1]), float(vbm_k_frac[2])],
        "cbm_k_frac": [float(cbm_k_frac[0]), float(cbm_k_frac[1]), float(cbm_k_frac[2])],
        "dos_e_fermi": dos_e_fermi,
        "dos_curve": dos_curve,
        "diagnostics": diagnostics,
    }


def _none_t2_result_partial(*, band_gap_ev, dos_e_fermi, dos_curve,
                            diagnostics=None) -> dict:
    """Returned when seekpath fails or VBM/CBM idx not found, but gap is known.
    Labels and gap_type are None (cannot determine without high-sym path).
    diagnostics surfaces why (seekpath_error / spacegroup) for visibility."""
    return {
        "band_gap_ev": band_gap_ev,
        "gap_type": None,
        "vbm_k_label": None,
        "cbm_k_label": None,
        "vbm_k_frac": None,
        "cbm_k_frac": None,
        "dos_e_fermi": dos_e_fermi,
        "dos_curve": dos_curve,
        "diagnostics": diagnostics,
    }


# ---------------------------------------------------------------------------
# T3 (EOS) — Birch-Murnaghan 3rd-order fit on (volume, energy) points
# ---------------------------------------------------------------------------
#
# 杨博 2026-05-20 T3 设计：7 体积点 [0.94..1.06]×V0 同比缩放 → 每点 fixed-cell
# ionic relax → 收集 (volume, energy) 拟合 BM3 → V0/B0/B0'。
#
# Agent 提交 eos_points.csv (2-col: volume_ang3, energy_ry per cell) + 可选
# fit.json。evaluator 重新做 BM3 fit (不信任 agent 的 fit，以独立计算为准)。

_RY_TO_EV = 13.605693122994
_EV_PER_ANG3_TO_GPA = 160.21766208  # 1 eV/Å³ = 160.218 GPa


def _parse_eos_points_csv(path: Path) -> list[tuple[float, float]]:
    """Read EOS points CSV → list of (volume_ang3, energy_ry).

    Supports two formats:
      A. 2-col: volume_ang3, energy_ry
      B. 4-col: ratio, volume_ang3, energy_ry, pressure_kbar (pipeline 自跑产物)

    Header line + comment lines (# prefix) skipped. Accepts comma- or
    whitespace-separated. Column count detected per data row; mixed
    formats in the same file rejected (4-col wins if any row has 4).
    """
    text = Path(path).read_text(encoding="utf-8", errors="replace")
    raw_rows: list[list[float]] = []
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        parts = [p.strip() for p in s.replace(",", " ").split() if p.strip()]
        if len(parts) < 2:
            continue
        try:
            nums = [float(p) for p in parts]
        except ValueError:
            # likely header row
            continue
        raw_rows.append(nums)

    if not raw_rows:
        return []

    max_cols = max(len(r) for r in raw_rows)
    rows: list[tuple[float, float]] = []
    if max_cols >= 4:
        # 4-col format: ratio, volume, energy, pressure → take col 1, 2
        for r in raw_rows:
            if len(r) >= 3:
                rows.append((r[1], r[2]))
    else:
        # 2-col format: volume, energy
        for r in raw_rows:
            if len(r) >= 2:
                rows.append((r[0], r[1]))
    return rows


def _bm3_energy(V, V0, B0, B0p, E0):
    """Birch-Murnaghan 3rd-order EOS in (V0/V)^(2/3) form.

    E(V) = E0 + (9*V0*B0/16) * { x³·B0' + x²·[6 − 4·(V0/V)^(2/3)] }
       where  x = (V0/V)^(2/3) − 1

    Units: V, V0 in Å³; B0 in eV/Å³; E0, E in eV; B0' dimensionless.
    """
    import numpy as np
    eta = (V0 / V) ** (2.0 / 3.0)
    x = eta - 1.0
    return E0 + (9.0 * V0 * B0 / 16.0) * (x ** 3 * B0p + x ** 2 * (6.0 - 4.0 * eta))


def _bm3_fit(volumes_ang3: list[float], energies_ev: list[float]) -> dict[str, Any]:
    """Least-squares BM3 fit. Returns {V0_ang3, B0_eV_per_ang3, B0p, E0_ev, residuals_ev, rms_ev}."""
    import numpy as np
    from scipy.optimize import curve_fit

    V = np.array(volumes_ang3, dtype=float)
    E = np.array(energies_ev, dtype=float)

    # 初值：从数据估计
    i_min = int(np.argmin(E))
    V0_0 = float(V[i_min])
    E0_0 = float(E[i_min])
    # B0 ≈ 100 GPa ≈ 0.6 eV/Å³；用稍宽 prior
    B0_0 = 0.6
    B0p_0 = 4.0

    popt, _pcov = curve_fit(
        _bm3_energy, V, E,
        p0=[V0_0, B0_0, B0p_0, E0_0],
        maxfev=10000,
    )
    V0, B0_evA3, B0p, E0 = (float(x) for x in popt)

    E_fit = _bm3_energy(V, V0, B0_evA3, B0p, E0)
    residuals = (E - E_fit).tolist()
    rms_ev = float(np.sqrt(np.mean(np.array(residuals) ** 2)))

    return {
        "V0_ang3": V0,
        "B0_eV_per_ang3": B0_evA3,
        "B0p": B0p,
        "E0_ev": E0,
        "residuals_ev": residuals,
        "rms_ev": rms_ev,
    }


def _compute_eos_curve(
    fit: dict[str, Any], V0_t1: float, ratio_grid: list[float], nat: int
) -> list[float]:
    """在 ratio_grid (基于 V0_t1) 上算 BM3 fit 的 ΔE/atom (meV/atom)，zero-aligned 到最低点。"""
    import numpy as np
    volumes = [r * V0_t1 for r in ratio_grid]
    energies = np.array([
        _bm3_energy(v, fit["V0_ang3"], fit["B0_eV_per_ang3"], fit["B0p"], fit["E0_ev"])
        for v in volumes
    ])
    energies -= float(energies.min())
    return (energies * 1000.0 / nat).tolist()


def extract_t3_fields(
    *,
    eos_points_csv_path: Path,
    fit_json_path: Path | None,
    atoms_per_primitive_cell: int,
    extraction_config: dict[str, Any],
) -> dict[str, Any]:
    """T3 EOS: parse 7 (V, E) points → BM3 fit → V0/B0/B0'/eos_curve.

    Returns:
      v0_ang3_cell : float (Å³)
      b0_gpa       : float (GPa)
      b0_prime     : float (dimensionless)
      eos_curve    : list[float] (ΔE/atom in meV/atom on volume_ratio_grid)
      fit_rms_mev_per_atom : float
      v0_fit_within_scan_range : bool (sanity check from 杨博 trap list)

    Raises ValueError if fewer than 5 points provided (BM3 has 4 params).

    fit_json_path: optional — agent's own fit; we ignore it and refit independently.
    """
    rows = _parse_eos_points_csv(eos_points_csv_path)
    if len(rows) < 5:
        raise ValueError(
            f"T3 EOS needs ≥5 (V, E) points for BM3 fit, got {len(rows)} from "
            f"{eos_points_csv_path}"
        )

    volumes_ang3 = [r[0] for r in rows]
    energies_ry = [r[1] for r in rows]
    energies_ev = [e * _RY_TO_EV for e in energies_ry]

    fit = _bm3_fit(volumes_ang3, energies_ev)
    V0 = fit["V0_ang3"]
    B0_gpa = fit["B0_eV_per_ang3"] * _EV_PER_ANG3_TO_GPA
    B0p = fit["B0p"]

    ratio_grid = extraction_config.get(
        "volume_ratio_grid",
        [0.94, 0.96, 0.98, 1.0, 1.02, 1.04, 1.06],
    )
    eos_curve = _compute_eos_curve(fit, V0, ratio_grid, atoms_per_primitive_cell)

    rms_mev_per_atom = fit["rms_ev"] * 1000.0 / atoms_per_primitive_cell

    V_min, V_max = min(volumes_ang3), max(volumes_ang3)
    v0_in_range = V_min <= V0 <= V_max

    return {
        "v0_ang3_cell": V0,
        "b0_gpa": B0_gpa,
        "b0_prime": B0p,
        "eos_curve": eos_curve,
        "fit_rms_mev_per_atom": rms_mev_per_atom,
        "v0_fit_within_scan_range": v0_in_range,
    }


# ---------------------------------------------------------------------------
# T4 (Elastic stiffness) — Voigt strain → stress lstsq fit → Cij 6x6 + VRH
# ---------------------------------------------------------------------------
#
# 杨博 2026-05-20 T4 设计：6 Voigt modes × 4 amplitudes = 24 strained cells，
# 每点 fixed-cell ionic relax → final stress tensor。lstsq fit σ = C·ε →
# 21 个 indep Cij (after symmetrize)，对称化后 VRH 平均得 K/G/E/ν。
#
# Convention (设计 D5):
#   Voigt 应变向量 ε=(ε1,ε2,ε3,γ4,γ5,γ6)；engineering shear γ = 2·ε_tensor_off_diag
#   Voigt 应力向量 σ=(σ_xx,σ_yy,σ_zz,σ_yz,σ_xz,σ_xy)；tensor 对称，单位 GPa

_VOIGT_MODE_IDX = {"e1": 0, "e2": 1, "e3": 2, "e4": 3, "e5": 4, "e6": 5}


def _parse_stress_strain_csv(path: Path) -> list[tuple[str, float, list[float]]]:
    """Read 8-col CSV: (mode_str, amplitude, σ1..σ6 GPa). Returns list of (mode, amp, [σ1..σ6])."""
    text = Path(path).read_text(encoding="utf-8", errors="replace")
    rows: list[tuple[str, float, list[float]]] = []
    bad_modes: set[str] = set()
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        parts = [p.strip() for p in s.replace(",", " ").split() if p.strip()]
        if len(parts) < 8:
            continue
        if parts[0] not in _VOIGT_MODE_IDX:
            # Header row 检测：第 2 列也不是数字 → 真 header, skip
            try:
                float(parts[1])
            except ValueError:
                continue
            # 第 2 列是 amplitude (数字) 但 mode 列不在 {e1..e6} → solver 写错 mode 值。
            # 0606 shijie handoff issue #4: v1 写 "1..6" 而非 "e1..e6", 老版 silent
            # continue 把 24 行全吞 → "got 0 strain points" 误导。改为收集 bad mode
            # 留待循环结束 raise 清晰错误 (附带建议正确取值)。
            bad_modes.add(parts[0])
            continue
        try:
            mode = parts[0]
            amp = float(parts[1])
            sigma = [float(parts[2 + i]) for i in range(6)]
        except ValueError:
            continue
        rows.append((mode, amp, sigma))
    if bad_modes and not rows:
        # 没有任何 valid 行 + 有 bad mode → 一定是 mode 标签格式错。
        raise ValueError(
            f"stress_strain_csv at {path}: unrecognized strain mode label(s) "
            f"{sorted(bad_modes)!r}; expected one of e1, e2, e3, e4, e5, e6 "
            f"(see schema/prompts/elastic_stiffness.txt for the submit contract)."
        )
    return rows


def _lstsq_cij_from_stress_strain(
    rows: list[tuple[str, float, list[float]]],
) -> tuple[list[list[float]], float]:
    """N strain rows → 6×6 Cij (unsymmetric) + fit RMSE in GPa.

    Build Ε (N×6) and Σ (N×6).  For each row k applying mode m at amplitude a:
      ε_k = a · e_m (one-hot in 6-dim Voigt space)
      σ_k = (σ1..σ6) measured
    Solve column-wise: σ[:, i] = Ε · C[i, :] → C[i, :] = lstsq(Ε, σ[:, i]).
    """
    import numpy as np
    n = len(rows)
    if n < 6:
        raise ValueError(f"T4 needs ≥6 strain points for lstsq, got {n}")

    epsilon = np.zeros((n, 6), dtype=float)
    sigma = np.zeros((n, 6), dtype=float)
    for k, (mode, amp, s) in enumerate(rows):
        idx = _VOIGT_MODE_IDX[mode]
        epsilon[k, idx] = amp
        sigma[k, :] = s

    cij = np.zeros((6, 6), dtype=float)
    all_residuals = []
    for i in range(6):
        sol, _, _, _ = np.linalg.lstsq(epsilon, sigma[:, i], rcond=None)
        cij[i, :] = sol
        pred = epsilon @ sol
        all_residuals.extend((sigma[:, i] - pred).tolist())

    rmse_gpa = float(np.sqrt(np.mean(np.array(all_residuals) ** 2)))
    return cij.tolist(), rmse_gpa


def _cij_asymmetry_percent(cij: list[list[float]]) -> float:
    """max_{i<j} |C_ij - C_ji| / max(|C_ij|, |C_ji|) × 100%."""
    max_asym = 0.0
    for i in range(6):
        for j in range(i + 1, 6):
            denom = max(abs(cij[i][j]), abs(cij[j][i]))
            if denom > 1e-9:
                asym = abs(cij[i][j] - cij[j][i]) / denom
                if asym > max_asym:
                    max_asym = asym
    return max_asym * 100.0


def _symmetrize_cij(cij: list[list[float]]) -> list[list[float]]:
    import numpy as np
    C = np.array(cij)
    return ((C + C.T) / 2.0).tolist()


def _vrh_moduli(cij_sym: list[list[float]]) -> dict[str, float]:
    """Voigt-Reuss-Hill (Hill) 平均 K + G，及 E + ν.

    Voigt 上界 (按 C):
      K_V = (C11+C22+C33 + 2(C12+C13+C23)) / 9
      G_V = (C11+C22+C33 - C12-C13-C23 + 3(C44+C55+C66)) / 15
    Reuss 下界 (按 S = inv(C)):
      K_R = 1 / (S11+S22+S33 + 2(S12+S13+S23))
      G_R = 15 / (4(S11+S22+S33) - 4(S12+S13+S23) + 3(S44+S55+S66))
    Hill: K_H = (K_V + K_R) / 2 ; G_H 同.
    E = 9KG / (3K + G) ;  ν = (3K − 2G) / (2(3K + G))
    """
    import numpy as np
    C = np.array(cij_sym, dtype=float)
    K_V = (C[0, 0] + C[1, 1] + C[2, 2] + 2.0 * (C[0, 1] + C[0, 2] + C[1, 2])) / 9.0
    G_V = (
        C[0, 0] + C[1, 1] + C[2, 2] - C[0, 1] - C[0, 2] - C[1, 2]
        + 3.0 * (C[3, 3] + C[4, 4] + C[5, 5])
    ) / 15.0
    try:
        S = np.linalg.inv(C)
        K_R = 1.0 / (S[0, 0] + S[1, 1] + S[2, 2] + 2.0 * (S[0, 1] + S[0, 2] + S[1, 2]))
        G_R = 15.0 / (
            4.0 * (S[0, 0] + S[1, 1] + S[2, 2])
            - 4.0 * (S[0, 1] + S[0, 2] + S[1, 2])
            + 3.0 * (S[3, 3] + S[4, 4] + S[5, 5])
        )
    except np.linalg.LinAlgError:
        K_R, G_R = float("nan"), float("nan")

    K_H = (K_V + K_R) / 2.0
    G_H = (G_V + G_R) / 2.0
    E_H = 9.0 * K_H * G_H / (3.0 * K_H + G_H) if (3.0 * K_H + G_H) != 0 else float("nan")
    nu_H = (3.0 * K_H - 2.0 * G_H) / (2.0 * (3.0 * K_H + G_H)) if (3.0 * K_H + G_H) != 0 else float("nan")

    return {
        "K_voigt_gpa": float(K_V),
        "G_voigt_gpa": float(G_V),
        "K_reuss_gpa": float(K_R),
        "G_reuss_gpa": float(G_R),
        "K_hill_gpa": float(K_H),
        "G_hill_gpa": float(G_H),
        "E_hill_gpa": float(E_H),
        "nu_hill": float(nu_H),
    }


def _born_stability(cij: list[list[float]]) -> dict[str, Any]:
    """Born 力学稳定准则: C 所有特征值 > 0 (C 必须正定)."""
    import numpy as np
    C = np.array(cij, dtype=float)
    eigvals = np.linalg.eigvalsh(C).tolist()
    return {
        "eigenvalues_gpa": [float(v) for v in eigvals],
        "all_positive": bool(all(v > 0 for v in eigvals)),
        "min_eigenvalue_gpa": float(min(eigvals)),
    }


def extract_t4_fields(
    *,
    stress_strain_csv_path: Path,
    elastic_tensor_json_path: Path | None,
    extraction_config: dict[str, Any],
) -> dict[str, Any]:
    """T4 弹性刚度 evaluator。

    Parse 24 (mode, amplitude, σ_voigt[6]) rows → lstsq fit Cij → symmetrize →
    VRH 平均 → Born stability.

    Returns:
      elastic_tensor_cij_gpa     : 6×6 list (symmetrized)
      bulk_modulus_vrh_gpa       : K_Hill in GPa
      shear_modulus_vrh_gpa      : G_Hill in GPa
      young_modulus_vrh_gpa      : E_Hill in GPa
      poisson_ratio_vrh          : ν_Hill
      stress_strain_curve        : 24×6=144 floats flat (mode-major, amp-minor, σ comp)
      stress_rmse_gpa            : lstsq residual RMSE
      max_cij_asymmetry_percent  : before-symmetrize 最大不对称度
      all_eigenvalues_positive   : Born stability check

    elastic_tensor_json_path: optional — agent 自报的 Cij; 我们独立 lstsq 不依赖它。
    """
    rows = _parse_stress_strain_csv(stress_strain_csv_path)
    if len(rows) < 6:
        raise ValueError(
            f"T4 elastic needs ≥6 strain points, got {len(rows)} from "
            f"{stress_strain_csv_path}"
        )

    cij_unsym, rmse_gpa = _lstsq_cij_from_stress_strain(rows)
    asymmetry_pct = _cij_asymmetry_percent(cij_unsym)

    if extraction_config.get("symmetrize_cij", True):
        cij_sym = _symmetrize_cij(cij_unsym)
    else:
        cij_sym = cij_unsym

    vrh = _vrh_moduli(cij_sym)

    if extraction_config.get("born_stability_check", True):
        stability = _born_stability(cij_sym)
    else:
        stability = {"all_positive": None, "eigenvalues_gpa": None, "min_eigenvalue_gpa": None}

    # Flatten stress_strain curve: 24 rows × 6 stress comp = 144 floats
    stress_strain_curve_flat: list[float] = []
    for (_mode, _amp, sigma) in rows:
        stress_strain_curve_flat.extend(sigma)

    return {
        "elastic_tensor_cij_gpa": cij_sym,
        "bulk_modulus_vrh_gpa": vrh["K_hill_gpa"],
        "shear_modulus_vrh_gpa": vrh["G_hill_gpa"],
        "young_modulus_vrh_gpa": vrh["E_hill_gpa"],
        "poisson_ratio_vrh": vrh["nu_hill"],
        "stress_strain_curve": stress_strain_curve_flat,
        "stress_rmse_gpa": rmse_gpa,
        "max_cij_asymmetry_percent": asymmetry_pct,
        "all_eigenvalues_positive": stability["all_positive"],
    }
