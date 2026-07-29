"""Pure-function helpers for T2 (bands + DOS) input rendering.

Generates pw.x SCF / NSCF / bands input files and pp-tool inputs for bands.x
and dos.x. Geometry is taken from the T1 canonical GT (final relaxed lattice
parameters + initial fractional positions).

No side effects: read-only on JSON inputs, returns strings to caller.
"""
from __future__ import annotations

import json as _json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


class UpstreamT1FailureError(Exception):
    """Raised by load_t2_geometry when T1 canonical_gt has FAIL status (no
    ground_truth block) — caller should write a FAILED canonical_gt_t2 JSON
    rather than crashing the run."""

# --------------------------------------------------------------------------
# Scan grid (杨博 0427: T2 与 T1 同款收敛性扫描)
# 2026-05-13 (杨博 confirm): ecutwfc 起点从 30 提到 50, 删除 30/40 (对 3d 元素截断太低,
# Co_hcp/Ni_fcc e30 跑出 mag=0 非磁伪基态; M3+ 体系 50 起步更安全)
# 新 grid: 3 ecutwfc + 3 kmesh = 6 点
# --------------------------------------------------------------------------
ECUTWFC_SWEEP = (50.0, 60.0, 80.0)                    # Ry, round 1
KMESH_DENSITY_SWEEP = (20.0, 30.0, 40.0, 50.0)        # pts/Å⁻¹, round 2
ROUND1_KMESH_DENSITY = 30.0                            # round 1 fixed kmesh
ROUND2_ECUTWFC = 80.0                                  # round 2 fixed ecutwfc
ECUTRHO_MULTIPLIER = 10.0                              # USPP standard

# Canonical anchor point (last point of round 2): ecutwfc=80, density=50
CANONICAL_ECUTWFC_RY = 80.0
CANONICAL_KMESH_DENSITY = 50.0
CANONICAL_ECUTRHO_RY = CANONICAL_ECUTWFC_RY * ECUTRHO_MULTIPLIER  # 800
CANONICAL_POINT_KEY = f"e{int(CANONICAL_ECUTWFC_RY)}_d{int(CANONICAL_KMESH_DENSITY)}"

# Backwards compat (旧单点版默认值；扫描版调用方应 pass per-point 值)
ECUTWFC_RY = CANONICAL_ECUTWFC_RY
ECUTRHO_RY = CANONICAL_ECUTRHO_RY
KMESH_DENSITY = CANONICAL_KMESH_DENSITY

# DOS post-processing (canonical 点一次性)
DOS_SIGMA_EV = 0.02        # Gaussian smearing
DOS_DE_EV = 0.01           # energy step
DOS_EMIN_EV = -25.0        # full DOS window
DOS_EMAX_EV = 25.0

# Bands path sampling density along seekpath (k-points per segment, approx)
BANDS_REFERENCE_DISTANCE_INV_ANG = 0.025   # seekpath reference_distance


@dataclass(frozen=True)
class ScanPoint:
    """One point in the 2D (ecutwfc, kmesh_density) scan grid for T2."""
    ecutwfc: float          # Ry
    ecutrho: float          # Ry
    kmesh_density: float    # pts / Å⁻¹
    round: int              # 1 or 2

    @property
    def key(self) -> str:
        """Filesystem-safe identifier: e.g. 'e40_d30'."""
        return f"e{int(self.ecutwfc)}_d{int(self.kmesh_density)}"


def scan_grid() -> list[ScanPoint]:
    """Return the 8 scan points per material (5 ecutwfc + 3 kmesh).

    Round 1: sweep ecutwfc ∈ ECUTWFC_SWEEP at kmesh density = ROUND1_KMESH_DENSITY.
    Round 2: sweep kmesh density ∈ KMESH_DENSITY_SWEEP \\ {ROUND1_KMESH_DENSITY}
             at ecutwfc = ROUND2_ECUTWFC (避开与 round 1 的重叠点).

    Total: 5 + 3 = 8 distinct points; canonical = e80_d50 (last of round 2).
    """
    points: list[ScanPoint] = []
    for e in ECUTWFC_SWEEP:
        points.append(ScanPoint(
            ecutwfc=e,
            ecutrho=e * ECUTRHO_MULTIPLIER,
            kmesh_density=ROUND1_KMESH_DENSITY,
            round=1,
        ))
    for d in KMESH_DENSITY_SWEEP:
        if d == ROUND1_KMESH_DENSITY:
            continue  # skip overlap with round 1's (ecutwfc=80, density=30)
        points.append(ScanPoint(
            ecutwfc=ROUND2_ECUTWFC,
            ecutrho=ROUND2_ECUTWFC * ECUTRHO_MULTIPLIER,
            kmesh_density=d,
            round=2,
        ))
    return points


def canonical_scan_point() -> ScanPoint:
    """Return the canonical scan point (round 2, ecutwfc=80, density=50)."""
    for p in scan_grid():
        if p.key == CANONICAL_POINT_KEY:
            return p
    raise RuntimeError("canonical point not in scan_grid (params bug)")


# --------------------------------------------------------------------------
# Magnetic体系 starting_magnetization 表（复用 m3_run.py 的 STARTING_MAG）
# 仅用于 M3 题目（M1/M2 默认 spin-unpolarized）
# 同号正值 → FM 体系；AFM 体系需要 species 拆分（M3 v1 暂不支持）
# --------------------------------------------------------------------------
MAGNETIC_STARTING_MAG_TABLE = {
    "Fe": 0.5, "Ni": 0.5, "Co": 0.5, "Mn": 0.7,
    "Cr": 0.5, "Cu": 0.4, "Eu": 0.7, "Gd": 0.7,
}


@dataclass(frozen=True)
class T2Geometry:
    """Geometry passed into the T2 pipeline.

    Comes from the T1 canonical GT (lattice parameters) + the T2 question JSON
    (atomic positions in fractional coords, since T1 vc-relax does not change
    fractional positions for these high-symmetry cubic structures).
    """
    a: float
    b: float
    c: float
    alpha: float
    beta: float
    gamma: float
    species: list[str]              # per-site element labels
    fractional_positions: list[list[float]]   # nat × 3
    pseudo_files: dict[str, str]    # element -> filename
    is_metal: bool
    is_magnetic: bool = False
    main_magnetic_element: str | None = None
    starting_magnetization: dict[str, float] | None = None
    target_spacegroup: int | None = None   # material.space_group_number → seekpath symprec target


# --------------------------------------------------------------------------
# kmesh
# --------------------------------------------------------------------------
def kmesh_from_density(lattice, density: float) -> list[int]:
    """Same algorithm as T1 params.kmesh_from_density (round to even, ≥2)."""
    recip = lattice.reciprocal_lattice.abc
    out: list[int] = []
    for b in recip:
        raw = density * b / (2.0 * math.pi)
        n = max(2, round(raw))
        if n % 2 == 1:
            n += 1
        out.append(n)
    return out


# --------------------------------------------------------------------------
# JSON loaders for upstream inputs
# --------------------------------------------------------------------------
def load_t2_geometry(t2_json_path: Path, t1_canonical_gt_json_path: Path) -> T2Geometry:
    """Load T2 question + T1 canonical GT and assemble a T2Geometry.

    Lattice parameters: from T1 canonical GT (final relaxed values).
    Fractional positions, species, pseudos: from T2 question JSON.
    is_metal: from T2 question JSON's ground_truth.gap_type == "metal".
    """
    t2 = _json.loads(Path(t2_json_path).read_text(encoding="utf-8"))
    t1 = _json.loads(Path(t1_canonical_gt_json_path).read_text(encoding="utf-8"))

    if "ground_truth" not in t1:
        # T1 vc-relax is FAIL — no relaxed structure available. Raise a typed
        # error so t2_main can write a FAILED canonical_gt_t2 JSON instead of
        # crashing.
        raise UpstreamT1FailureError(
            f"T1 canonical_gt for {t2_json_path.stem} is missing ground_truth "
            f"(status={t1.get('status')})"
        )
    gt = t1["ground_truth"]
    a = float(gt["a"]["value"])
    b = float(gt["b"]["value"])
    c = float(gt["c"]["value"])
    alpha = float(gt["alpha"]["value"])
    beta = float(gt["beta"]["value"])
    gamma = float(gt["gamma"]["value"])

    initial = t2["material"]["initial_structure"]
    positions = initial["atomic_positions_crystal"]
    species = [site["element"] for site in positions]
    fractional_positions = [list(site["position"]) for site in positions]
    pseudo_files = dict(t2["material"]["pseudopotential"]["files"])

    gap_type = t2.get("ground_truth", {}).get("gap_type", {}).get("value")
    is_metal = (gap_type == "metal")

    # M3 题目：从 question JSON 的 material.magnetic_metadata 读磁性配置
    # 字段缺失 → 默认非磁（兼容 M1/M2）
    mag_meta = t2["material"].get("magnetic_metadata") or {}
    is_magnetic = bool(mag_meta.get("nspin", 1) >= 2)
    main_magnetic_element = mag_meta.get("main_magnetic_element")
    # M3 题目 ground_truth 在 promote 阶段是 placeholder（gap_type=None），
    # 用 magnetic_metadata.expected_is_metal hint 来识别 metal 体系
    if not is_metal and mag_meta.get("expected_is_metal"):
        is_metal = True
    starting_magnetization: dict[str, float] | None = None
    if is_magnetic and main_magnetic_element:
        # FM 路径：仅 main_magnetic_element 设正值，其它元素 = 0
        # 优先用 question 自带的 starting_magnetization 字典；否则查表
        sm_from_question = mag_meta.get("starting_magnetization")
        if isinstance(sm_from_question, dict):
            starting_magnetization = {k: float(v) for k, v in sm_from_question.items()}
        else:
            mag_value = MAGNETIC_STARTING_MAG_TABLE.get(main_magnetic_element, 0.5)
            starting_magnetization = {
                el: (mag_value if el == main_magnetic_element else 0.0)
                for el in set(species)
            }

    target_spacegroup = t2["material"].get("space_group_number")

    return T2Geometry(
        a=a, b=b, c=c,
        alpha=alpha, beta=beta, gamma=gamma,
        species=species,
        fractional_positions=fractional_positions,
        pseudo_files=pseudo_files,
        is_metal=is_metal,
        is_magnetic=is_magnetic,
        main_magnetic_element=main_magnetic_element,
        starting_magnetization=starting_magnetization,
        target_spacegroup=target_spacegroup,
    )


def build_pmg_lattice(geom: T2Geometry):
    from pymatgen.core import Lattice as _Lattice
    return _Lattice.from_parameters(geom.a, geom.b, geom.c,
                                    geom.alpha, geom.beta, geom.gamma)


# --------------------------------------------------------------------------
# Common pw.x input pieces
# --------------------------------------------------------------------------
def _atomic_species_block(geom: T2Geometry) -> list[str]:
    """Render ATOMIC_SPECIES block.

    Supports AFM species split (e.g. Co → Co1, Co2 共用 Co.upf). When a species
    name is not a valid pymatgen Element (e.g. 'Co1'), strip trailing digits to
    find the base element for atomic mass lookup; the displayed species name
    keeps the split suffix so QE can apply per-species starting_magnetization.
    """
    import re as _re
    from pymatgen.core import Element
    seen: list[str] = []
    for el in geom.species:
        if el not in seen:
            seen.append(el)
    lines = ["ATOMIC_SPECIES"]
    for el in seen:
        base = _re.sub(r'\d+$', '', el) or el
        mass = Element(base).atomic_mass
        upf = geom.pseudo_files[el]
        lines.append(f"  {el}  {float(mass):.4f}  {upf}")
    return lines


def _cell_parameters_block(geom: T2Geometry) -> list[str]:
    matrix = build_pmg_lattice(geom).matrix
    lines = ["CELL_PARAMETERS (angstrom)"]
    for row in matrix:
        lines.append(f"  {row[0]:>14.10f}  {row[1]:>14.10f}  {row[2]:>14.10f}")
    return lines


def _atomic_positions_block(geom: T2Geometry) -> list[str]:
    lines = ["ATOMIC_POSITIONS (crystal)"]
    for el, (x, y, z) in zip(geom.species, geom.fractional_positions):
        lines.append(f"  {el:<4}  {x:>14.10f}  {y:>14.10f}  {z:>14.10f}")
    return lines


def _ntyp(geom: T2Geometry) -> int:
    return len(set(geom.species))


def _nat(geom: T2Geometry) -> int:
    return len(geom.species)


def _system_block(
    geom: T2Geometry,
    ecutwfc: float,
    ecutrho: float,
    *,
    nbnd: int | None = None,
) -> list[str]:
    lines = ["&SYSTEM"]
    lines.append("    ibrav = 0")
    lines.append(f"    nat = {_nat(geom)}")
    lines.append(f"    ntyp = {_ntyp(geom)}")
    lines.append(f"    ecutwfc = {ecutwfc}")
    lines.append(f"    ecutrho = {ecutrho}")
    if nbnd is not None:
        lines.append(f"    nbnd = {int(nbnd)}")
    # 磁性体系（M3）必须用 smearing；金属也用 smearing
    if geom.is_metal or geom.is_magnetic:
        lines.append("    occupations = 'smearing'")
        lines.append("    smearing = 'mv'")
        lines.append("    degauss = 0.02")
    else:
        lines.append("    occupations = 'fixed'")
    if geom.is_magnetic:
        lines.append("    nspin = 2")
        # starting_magnetization 按 ATOMIC_SPECIES 中元素首次出现的顺序写
        seen: list[str] = []
        for el in geom.species:
            if el not in seen:
                seen.append(el)
        sm = geom.starting_magnetization or {}
        for i, el in enumerate(seen, start=1):
            mag = float(sm.get(el, 0.0))
            lines.append(f"    starting_magnetization({i}) = {mag}")
    lines.append("/")
    return lines


# --------------------------------------------------------------------------
# Auto nbnd estimate
# --------------------------------------------------------------------------
def estimate_nbnd(geom: T2Geometry) -> int:
    """Estimate a generous nbnd that covers the conduction-band edge.

    Heuristic: count valence electrons via GBRV USPP nominal Z_valence
    for the elements we use, then add a buffer of max(8, n_val) for CBM.

    GBRV USPP valence (covers Si, Cu, Mg, O, Ga, As):
        Si=4, Cu=19, Mg=10, O=6, Ga=19, As=15.
    Spin-unpolarized → bands needed = n_val/2; we then double-add a buffer.
    """
    Z_VAL = {"Si": 4, "Cu": 19, "Mg": 10, "O": 6, "Ga": 19, "As": 15,
             "Al": 3, "C": 4, "N": 5, "Be": 4, "P": 5, "Sb": 15,
             "B": 3, "Ca": 10, "Cd": 12, "S": 6, "Se": 6, "Te": 6, "Cs": 9, "Cl": 7,
             "Au": 19, "Ag": 19,
             # M3 磁性元素（GBRV USPP valence）
             "Fe": 16, "Co": 17, "Ni": 18, "Mn": 15, "Cr": 14}
    n_val = 0
    for el in geom.species:
        n_val += Z_VAL.get(el, 8)  # default 8 if element not in table
    if geom.is_magnetic:
        # nspin=2: 每个 spin channel 占据 ≈ n_val / 2 个 band，但因 spin polarization 不一定 5050
        # nbnd 是单 spin channel 的上限 → 给充足 buffer 让 spin-up/down 都有空间
        # metal + magnetic 需要更多 bands 描述 Fermi level 附近态密度
        n_filled = (n_val + 1) // 2
        n_buffer = max(20, n_filled)  # magnetic 给更宽 buffer（spin polarization + metal Fermi）
        return n_filled + n_buffer
    # filled bands (spin-unpolarized) = n_val / 2 (rounded up for odd electron systems)
    n_filled = (n_val + 1) // 2
    n_buffer = max(8, n_filled // 2)
    return n_filled + n_buffer


# --------------------------------------------------------------------------
# pw.x SCF input
# --------------------------------------------------------------------------
def render_scf_input(
    geom: T2Geometry,
    pseudo_dir: Path,
    kmesh: list[int],
    *,
    ecutwfc: float = ECUTWFC_RY,
    ecutrho: float = ECUTRHO_RY,
    prefix: str = "pwscf",
) -> str:
    """Render pw.x SCF input string."""
    lines: list[str] = []
    lines.append("&CONTROL")
    lines.append("    calculation = 'scf'")
    lines.append(f"    prefix = '{prefix}'")
    lines.append(f"    pseudo_dir = '{Path(pseudo_dir).resolve()}/'")
    lines.append("    outdir = './tmp'")
    lines.append("    tstress = .true.")
    lines.append("    tprnfor = .true.")
    lines.append("    verbosity = 'high'")
    lines.append("/")
    lines += _system_block(geom, ecutwfc, ecutrho)
    lines.append("&ELECTRONS")
    lines.append("    conv_thr = 1.0e-9")
    lines.append("    mixing_beta = 0.7")
    lines.append("/")
    lines += _atomic_species_block(geom)
    lines += _cell_parameters_block(geom)
    lines += _atomic_positions_block(geom)
    lines.append("K_POINTS (automatic)")
    n1, n2, n3 = kmesh
    lines.append(f"  {n1} {n2} {n3} 0 0 0")
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------
# pw.x NSCF input
# --------------------------------------------------------------------------
def render_nscf_input(
    geom: T2Geometry,
    pseudo_dir: Path,
    kmesh: list[int],
    *,
    nbnd: int,
    ecutwfc: float = ECUTWFC_RY,
    ecutrho: float = ECUTRHO_RY,
    prefix: str = "pwscf",
) -> str:
    """Render pw.x NSCF input string. Uses the same outdir/prefix as SCF."""
    lines: list[str] = []
    lines.append("&CONTROL")
    lines.append("    calculation = 'nscf'")
    lines.append(f"    prefix = '{prefix}'")
    lines.append(f"    pseudo_dir = '{Path(pseudo_dir).resolve()}/'")
    lines.append("    outdir = './tmp'")
    lines.append("    verbosity = 'high'")
    lines.append("/")
    lines += _system_block(geom, ecutwfc, ecutrho, nbnd=nbnd)
    lines.append("&ELECTRONS")
    # 磁性 metal 在高 ecutwfc + 中等 kmesh 下 Davidson NSCF 可能数值崩
    # （e.g. Fe_bcc e80_d40 nscf 失败，2026-05-13）。CG 更稳但慢 ~2x。
    if geom.is_magnetic:
        lines.append("    conv_thr = 1.0e-8")
        lines.append("    diagonalization = 'cg'")
        lines.append("    diago_full_acc = .true.")
    else:
        lines.append("    conv_thr = 1.0e-9")
        lines.append("    diago_full_acc = .true.")
    lines.append("/")
    lines += _atomic_species_block(geom)
    lines += _cell_parameters_block(geom)
    lines += _atomic_positions_block(geom)
    lines.append("K_POINTS (automatic)")
    n1, n2, n3 = kmesh
    lines.append(f"  {n1} {n2} {n3} 0 0 0")
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------
# pw.x bands input (along seekpath)
# --------------------------------------------------------------------------
def render_bands_pw_input(
    geom: T2Geometry,
    pseudo_dir: Path,
    seekpath_path: list[tuple[str, str]],
    seekpath_point_coords: dict[str, list[float]],
    *,
    nbnd: int,
    points_per_segment: int = 30,
    ecutwfc: float = ECUTWFC_RY,
    ecutrho: float = ECUTRHO_RY,
    prefix: str = "pwscf",
) -> str:
    """Render pw.x calculation='bands' input with K_POINTS crystal_b along path.

    Path format (crystal_b): the first line is the number of high-symmetry
    points; each subsequent line gives (kx, ky, kz, n_points_to_next). The
    n_points_to_next on the *last* point is 1 (used as terminator).
    """
    # Build a flat list of unique consecutive k-points on the seekpath
    flat: list[tuple[str, list[float]]] = []
    for i, (a, b) in enumerate(seekpath_path):
        if not flat or flat[-1][0] != a:
            flat.append((a, list(seekpath_point_coords[a])))
        flat.append((b, list(seekpath_point_coords[b])))

    lines: list[str] = []
    lines.append("&CONTROL")
    lines.append("    calculation = 'bands'")
    lines.append(f"    prefix = '{prefix}'")
    lines.append(f"    pseudo_dir = '{Path(pseudo_dir).resolve()}/'")
    lines.append("    outdir = './tmp'")
    lines.append("    verbosity = 'high'")
    lines.append("/")
    lines += _system_block(geom, ecutwfc, ecutrho, nbnd=nbnd)
    lines.append("&ELECTRONS")
    # 磁性 metal 在 bands path 上某些 k-point Davidson 会触发
    # "too many bands not converged"。CG 更稳但慢约 2 倍。
    if geom.is_magnetic:
        lines.append("    conv_thr = 1.0e-8")
        lines.append("    diagonalization = 'cg'")
        lines.append("    diago_full_acc = .true.")
    else:
        lines.append("    conv_thr = 1.0e-9")
        lines.append("    diago_full_acc = .true.")
    lines.append("/")
    lines += _atomic_species_block(geom)
    lines += _cell_parameters_block(geom)
    lines += _atomic_positions_block(geom)

    lines.append("K_POINTS crystal_b")
    lines.append(f"  {len(flat)}")
    for i, (label, kp) in enumerate(flat):
        # Last point uses 1 as terminator (no segment after it).
        n_per_seg = points_per_segment if i < len(flat) - 1 else 1
        # Detect a "discontinuity" pair: if next point in flat is identical label
        # but at index of (label_a -> label_b) start of new segment, treat it
        # the same. seekpath path itself encodes that as separate edges, so the
        # flattening already handles it.
        lines.append(f"  {kp[0]:.10f} {kp[1]:.10f} {kp[2]:.10f} {n_per_seg}")
    return "\n".join(lines) + "\n"


def serialize_seekpath_for_bandsx(
    flat_high_sym_labels: list[tuple[str, list[float]]],
    points_per_segment: int = 30,
) -> list[tuple[str, list[float], int]]:
    """Helper used by t2_extract: returns the same flat list the bands input
    used, augmented with n-points so the bands.dat ordering can be reproduced.
    """
    out = []
    for i, (lbl, kp) in enumerate(flat_high_sym_labels):
        n = points_per_segment if i < len(flat_high_sym_labels) - 1 else 1
        out.append((lbl, kp, n))
    return out


# --------------------------------------------------------------------------
# bands.x post-processing input
# --------------------------------------------------------------------------
def render_bands_pp_input(
    *,
    prefix: str = "pwscf",
    outdir: str = "./tmp",
    filband: str = "bands.dat",
    lsym: bool = True,
) -> str:
    lines = ["&BANDS"]
    lines.append(f"    prefix = '{prefix}'")
    lines.append(f"    outdir = '{outdir}'")
    lines.append(f"    filband = '{filband}'")
    lines.append(f"    lsym = .{'true' if lsym else 'false'}.")
    lines.append("/")
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------
# dos.x post-processing input
# --------------------------------------------------------------------------
def render_dos_input(
    *,
    prefix: str = "pwscf",
    outdir: str = "./tmp",
    fildos: str = "dos.dat",
    sigma_ev: float = DOS_SIGMA_EV,
    de_ev: float = DOS_DE_EV,
    emin_ev: float = DOS_EMIN_EV,
    emax_ev: float = DOS_EMAX_EV,
) -> str:
    lines = ["&DOS"]
    lines.append(f"    prefix = '{prefix}'")
    lines.append(f"    outdir = '{outdir}'")
    lines.append(f"    fildos = '{fildos}'")
    lines.append(f"    DeltaE = {de_ev}")
    lines.append(f"    Emin = {emin_ev}")
    lines.append(f"    Emax = {emax_ev}")
    lines.append(f"    degauss = {sigma_ev}")
    lines.append("    ngauss = 0")  # 0 = simple Gaussian
    lines.append("/")
    return "\n".join(lines) + "\n"
