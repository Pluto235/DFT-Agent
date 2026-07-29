"""Pure-function helpers for gt_selfrun Phase 1.

Generates scan grid (strategy A), computes kmesh from density,
determines is_metal from sibling T2 bench JSON, renders QE input files.

No side effects except reading JSON files. Safe to import and unit-test.
"""
from __future__ import annotations

import json as _json
import math
from dataclasses import dataclass
from pathlib import Path


# Strategy A scan grid constants (from settings_plan.md §4.1)
# 2026-05-13 (杨博 confirm): ecutwfc 起点从 30 提到 50, 对 3d 元素 (M3+) 30/40 截断太低
# 实测 Co_hcp/Ni_fcc e30 跑出 mag=0 非磁伪基态; lattice 偏差 4.8%/7.4% 远超 1% 判分阈值
ECUTWFC_SWEEP = (50.0, 60.0, 80.0, 100.0, 120.0, 150.0)   # Ry, round 1
KMESH_DENSITY_SWEEP = (20.0, 30.0, 40.0, 50.0)            # pts/Å⁻¹, round 2
ROUND1_KMESH_DENSITY = 30.0                          # round 1 fixed kmesh
ROUND2_ECUTWFC = 80.0                                # round 2 fixed ecutwfc
ECUTRHO_MULTIPLIER = 10.0                            # USPP standard


@dataclass(frozen=True)
class ScanPoint:
    """One point in the 2D (ecutwfc, kmesh_density) scan grid."""
    ecutwfc: float          # Ry
    ecutrho: float          # Ry
    kmesh_density: float    # pts/Å⁻¹
    round: int              # 1 or 2

    @property
    def key(self) -> str:
        """Filesystem-safe identifier: e.g. 'e40_d30'."""
        return f"e{int(self.ecutwfc)}_d{int(self.kmesh_density)}"


def scan_grid() -> list[ScanPoint]:
    """Return the 8 scan points per material.

    Round 1: sweep ecutwfc ∈ ECUTWFC_SWEEP at kmesh density = ROUND1_KMESH_DENSITY.
    Round 2: sweep kmesh density ∈ KMESH_DENSITY_SWEEP \\ {ROUND1_KMESH_DENSITY}
             at ecutwfc = ROUND2_ECUTWFC (避开与 round 1 的重叠点).

    Total: 5 + 3 = 8 distinct points.
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


def kmesh_from_density(lattice, density: float) -> list[int]:
    """Compute Monkhorst-Pack kmesh divisions from reciprocal-lattice density.

    Args:
        lattice: pymatgen.core.Lattice (real-space)
        density: target k-point density in pts/Å⁻¹

    Returns:
        [n1, n2, n3] — each ≥ 2, each even (rounded up if odd).

    Formula: n_i = max(2, round(density × |b_i| / 2π)), where |b_i| is
             the reciprocal-lattice vector length in Å⁻¹.
             Round up to even for MP grid symmetry.
    """
    # pymatgen's reciprocal_lattice.abc returns |b_i| * 2π / a (already includes 2π factor)
    # So we need abc / (2π) to get raw |b_i| in Å⁻¹, but the density formula is
    # n_i = density / |b_i|_in_units_of_2π/a, which simplifies to
    # n_i = density × a_i / (2π × a_i) ... actually just use reciprocal.abc / (2π).
    recip = lattice.reciprocal_lattice.abc  # these are 2π/a, 2π/b, 2π/c in Å⁻¹
    result: list[int] = []
    for b in recip:
        raw = density * b / (2.0 * math.pi)
        n = max(2, round(raw))
        if n % 2 == 1:
            n += 1  # round up to even for MP symmetry
        result.append(n)
    return result


def is_metal(bench_t1_json_path: Path) -> bool:
    """Determine if a material is metallic, by reading its sibling T2 JSON's gap_type.

    Args:
        bench_t1_json_path: Path to the T1 bench JSON
                            (e.g. bench/questions/M1_T1/M1_T1_Si_vc_relax.json)

    Returns:
        True if T2 JSON's ground_truth.gap_type.value == "metal", else False.

    Raises:
        FileNotFoundError: if the sibling T2 JSON doesn't exist.

    Sibling derivation: M?_T1/<id>_vc_relax.json → M?_T2/<id-head>_bands_dos.json.
    The <id-head> is the part between 'M?_T1_' and '_vc_relax'.
    """
    path = Path(bench_t1_json_path)
    # Rewrite the file name: '_T1_' → '_T2_', '_vc_relax.json' → '_bands_dos.json'
    t2_name = path.name.replace("_T1_", "_T2_").replace("_vc_relax.json", "_bands_dos.json")
    # Rewrite the parent dir: M?_T1 → M?_T2
    parent_name = path.parent.name.replace("_T1", "_T2")
    t2_path = path.parent.parent / parent_name / t2_name
    if not t2_path.exists():
        raise FileNotFoundError(f"T2 sibling JSON not found: {t2_path}")
    t2 = _json.loads(t2_path.read_text(encoding="utf-8"))
    gap_type = t2.get("ground_truth", {}).get("gap_type", {}).get("value")
    return gap_type == "metal"


def render_qe_input(
    bench_json: dict,
    scan_point: ScanPoint,
    kmesh: list[int],
    is_metal: bool,
    pseudo_dir: Path,
) -> str:
    """Render pw.x vc-relax input as a string.

    Args:
        bench_json: dict loaded from bench/questions/M?_T1/<id>.json
        scan_point: one of 8 ScanPoint objects
        kmesh: [n1, n2, n3] MP grid
        is_metal: from is_metal()
        pseudo_dir: absolute path to pseudo files (e.g. bench/pseudos/GBRV_USPP_PBE_v1.5/)

    Returns:
        Full pw.x input as a string.
    """
    from pymatgen.core import Element, Lattice as _Lattice

    material = bench_json["material"]
    initial = material["initial_structure"]
    lat = initial["lattice_constants_angstrom"]
    ang = initial["lattice_angles_degree"]
    positions = initial["atomic_positions_crystal"]
    pseudo_files = material["pseudopotential"]["files"]

    # Build CELL_PARAMETERS from (a,b,c,α,β,γ) using pymatgen.Lattice
    pmg_lat = _Lattice.from_parameters(
        lat["a"], lat["b"], lat["c"],
        ang["alpha"], ang["beta"], ang["gamma"],
    )
    matrix = pmg_lat.matrix  # 3x3 numpy array, rows are lattice vectors

    # Unique elements in order they first appear in positions
    seen: list[str] = []
    for site in positions:
        el = site["element"]
        if el not in seen:
            seen.append(el)
    nat = len(positions)
    ntyp = len(seen)

    # Build namelists
    lines: list[str] = []
    lines.append("&CONTROL")
    lines.append("    calculation = 'vc-relax'")
    lines.append(f"    pseudo_dir = '{Path(pseudo_dir).resolve()}/'")
    lines.append("    outdir = './tmp'")
    lines.append("    etot_conv_thr = 1.0e-4")
    lines.append("    forc_conv_thr = 1.0e-3")
    lines.append("    nstep = 100")
    lines.append("    tstress = .true.")
    lines.append("    tprnfor = .true.")
    lines.append("/")

    lines.append("&SYSTEM")
    lines.append("    ibrav = 0")
    lines.append(f"    nat = {nat}")
    lines.append(f"    ntyp = {ntyp}")
    lines.append(f"    ecutwfc = {scan_point.ecutwfc}")
    lines.append(f"    ecutrho = {scan_point.ecutrho}")
    if is_metal:
        lines.append("    occupations = 'smearing'")
        lines.append("    smearing = 'mv'")
        lines.append("    degauss = 0.02")
    else:
        lines.append("    occupations = 'fixed'")
    lines.append("/")

    lines.append("&ELECTRONS")
    lines.append("    conv_thr = 1.0e-8")
    lines.append("    mixing_beta = 0.7")
    lines.append("/")

    lines.append("&IONS")
    lines.append("    ion_dynamics = 'bfgs'")
    lines.append("/")

    lines.append("&CELL")
    lines.append("    cell_dynamics = 'bfgs'")
    lines.append("    press_conv_thr = 0.5")
    lines.append("/")

    # Cards
    lines.append("ATOMIC_SPECIES")
    for el in seen:
        mass = Element(el).atomic_mass
        pseudo = pseudo_files[el]
        lines.append(f"  {el}  {float(mass):.4f}  {pseudo}")

    lines.append("CELL_PARAMETERS (angstrom)")
    for row in matrix:
        lines.append(f"  {row[0]:>14.10f}  {row[1]:>14.10f}  {row[2]:>14.10f}")

    lines.append("ATOMIC_POSITIONS (crystal)")
    for site in positions:
        el = site["element"]
        x, y, z = site["position"]
        lines.append(f"  {el:<4}  {x:>14.10f}  {y:>14.10f}  {z:>14.10f}")

    lines.append("K_POINTS (automatic)")
    n1, n2, n3 = kmesh
    lines.append(f"  {n1} {n2} {n3} 0 0 0")

    return "\n".join(lines) + "\n"
