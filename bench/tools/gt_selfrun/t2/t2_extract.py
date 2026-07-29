"""Output parsers + GT-field extractors for the T2 pipeline.

Functions split into small pure pieces so they're individually testable:
- parse_fermi_energy_ev       : SCF "highest occupied" / "Fermi energy"
- parse_nscf_eigenvalues      : NSCF k-points + eigenvalues + occupations (legacy)
- parse_bands_dat             : QE bands.x output (k-cart + per-k eigenvalues)
- reconstruct_path_samples    : 8 点扫描版用，重建 bands_pw 的 k-sample 在 cryst 坐标
- extract_band_gap_from_bands_dat : 从 bands.dat 沿高对称路径抽 VBM/CBM/gap（新版 canonical 抽法）
- find_vbm_cbm                : VBM = max(occupied), CBM = min(unoccupied) — 旧 NSCF 抽法
- match_k_to_seekpath_label   : B1 strategy (fractional-distance < 0.05)
- read_dos_dat                : (E_eV, dos) curve + e_fermi line
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable

# Threshold for B1 (fractional-coordinate Euclidean distance)
SEEKPATH_LABEL_THRESHOLD = 0.05


# --------------------------------------------------------------------------
# SCF Fermi energy
# --------------------------------------------------------------------------
def parse_fermi_energy_ev(scf_out_path: Path) -> float | None:
    """Parse Fermi energy (or highest-occupied level) from a pw.x SCF output.

    QE prints either:
      * "the Fermi energy is    X.XXXX ev"          (metallic / smearing)
      * "highest occupied level (ev):    X.XXXX"    (insulator, fixed occupations)
      * "highest occupied, lowest unoccupied level (ev):    X    Y"  (insulator)

    Returns the *last* occurrence (final SCF iteration). Returns None if not found.
    """
    text = Path(scf_out_path).read_text(encoding="utf-8", errors="ignore")
    # Try Fermi energy first
    matches = re.findall(r"the Fermi energy is\s+(-?\d+\.\d+)\s*ev",
                         text, flags=re.IGNORECASE)
    if matches:
        return float(matches[-1])
    # Try "highest occupied, lowest unoccupied"
    m = re.findall(
        r"highest occupied,\s+lowest unoccupied level\s*\(ev\)\s*:\s*(-?\d+\.\d+)\s+(-?\d+\.\d+)",
        text, flags=re.IGNORECASE,
    )
    if m:
        # Use the highest-occupied (VBM-style) as Fermi level for insulators
        return float(m[-1][0])
    # Fall back to "highest occupied level" alone
    m2 = re.findall(r"highest occupied level\s*\(ev\)\s*:\s*(-?\d+\.\d+)",
                    text, flags=re.IGNORECASE)
    if m2:
        return float(m2[-1])
    return None


def parse_homo_lumo_ev(out_path: Path) -> tuple[float, float] | None:
    """Parse the 'highest occupied, lowest unoccupied' line from a pw.x output.

    QE writes this line for insulator band structure runs (NSCF with
    occupations='fixed' or with sufficient nbnd above the gap). It is the most
    accurate VBM/CBM splitter (4 dp), unaffected by bands.dat rounding noise.

    Returns (homo_ev, lumo_ev) or None if line not present (e.g. metal NSCF).
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


# --------------------------------------------------------------------------
# NSCF eigenvalues parser
# --------------------------------------------------------------------------
def parse_nscf_eigenvalues(nscf_out_path: Path) -> dict:
    """Parse the per-k-point eigenvalue blocks in a pw.x output.

    Output blocks look like:

        k =  0.0000 0.0000 0.0000   ( 28537 PWs)   bands (ev):

         -5.6789  -2.3456 ...
         ...
            occupation numbers
              1.0000  1.0000 ...
              ...

    Returns:
        {
            "kpoints_frac": list[(kx,ky,kz)] in QE 'crystal' units (i.e. the
                            same units the input file used: "K_POINTS automatic"
                            yields k in 2π/alat by default — but for our purpose
                            we don't care about absolute scaling because we
                            convert to fractional via cryst-coord block below).
            "eigenvalues_ev": list[list[float]] (nk × nbnd),
            "occupations": list[list[float]] (nk × nbnd) or None if absent,
            "fermi_energy_ev": float | None (if NSCF reports it),
            "kpoints_cryst": list[(fx,fy,fz)] (k in fractional / crystal coords)
                              if QE printed "cryst. coord." block; else equal
                              to kpoints_frac.
        }
    """
    text = Path(nscf_out_path).read_text(encoding="utf-8", errors="ignore")

    # Locate end of preamble — find the first "End of band structure calculation"
    # or "End of self-consistent calculation" block. NSCF uses the former.
    end_marker = re.search(
        r"End of (band structure|self-consistent) calculation",
        text,
    )
    if end_marker is None:
        # Fall back: parse from beginning (best effort)
        body = text
    else:
        body = text[end_marker.end():]

    # Split into per-k-point blocks. Each block starts with "k =  fx fy fz".
    blocks = re.split(r"^\s*k =\s*", body, flags=re.MULTILINE)
    # blocks[0] is preamble before first k=
    blocks = blocks[1:]

    kpoints_frac = []
    eigs_per_k: list[list[float]] = []
    occs_per_k: list[list[float]] = []
    have_occ = True
    for blk in blocks:
        # First line: kx ky kz   ( N PWs )   bands (ev):
        # QE may pack negatives without whitespace, e.g. "0.2887 0.2041-0.5000"
        # — so extract the first three floats from the header line directly.
        first_line = blk.split("\n", 1)[0]
        nums = re.findall(r"-?\d+\.\d+", first_line)
        if len(nums) < 3:
            continue
        kx, ky, kz = float(nums[0]), float(nums[1]), float(nums[2])
        kpoints_frac.append((kx, ky, kz))

        # The rest of the block: eigenvalues, then optionally "occupation numbers"
        # with another block of floats.
        # Find "occupation numbers" anchor
        occ_idx = blk.find("occupation numbers")
        if occ_idx < 0:
            eig_text = blk
            occ_text = None
            have_occ = False
        else:
            eig_text = blk[:occ_idx]
            occ_text = blk[occ_idx + len("occupation numbers"):]

        eigs = [float(x) for x in re.findall(r"-?\d+\.\d+", eig_text)]
        # The first 3 numbers are kx,ky,kz; drop them.
        eigs = eigs[3:]
        eigs_per_k.append(eigs)

        if occ_text is not None:
            occs = [float(x) for x in re.findall(r"-?\d+\.\d+", occ_text)]
            # Truncate to len(eigs) to avoid bleed into the next block.
            occs = occs[: len(eigs)]
            occs_per_k.append(occs)

    # Try to parse cryst. coord block (if QE printed it) so we have fractional kpts.
    kpoints_cryst = _parse_cryst_kpoints(text, n_expected=len(kpoints_frac))
    if kpoints_cryst is None:
        kpoints_cryst = list(kpoints_frac)  # fallback: assume same

    # Fermi energy in NSCF (sometimes printed)
    fe = None
    fe_match = re.findall(r"the Fermi energy is\s+(-?\d+\.\d+)\s*ev", text, flags=re.IGNORECASE)
    if fe_match:
        fe = float(fe_match[-1])

    return {
        "kpoints_frac": kpoints_frac,
        "kpoints_cryst": kpoints_cryst,
        "eigenvalues_ev": eigs_per_k,
        "occupations": occs_per_k if have_occ else None,
        "fermi_energy_ev": fe,
    }


def _parse_cryst_kpoints(text: str, n_expected: int) -> list[tuple[float, float, float]] | None:
    """Extract the 'cryst. coord.' k-point block from a pw.x output.

    QE prints something like:
        cryst. coord.
        k(    1) = (   0.0000  0.0000  0.0000), wk =   0.012
        k(    2) = (   ...
    """
    m = re.search(r"cryst\.\s*coord\.", text)
    if not m:
        return None
    sub = text[m.end():]
    # Match up to ~2*n_expected lines after the marker
    matches = re.findall(
        r"k\(\s*\d+\)\s*=\s*\(\s*(-?\d+\.\d+)\s+(-?\d+\.\d+)\s+(-?\d+\.\d+)\s*\)",
        sub,
    )
    if not matches:
        return None
    pts = [(float(a), float(b), float(c)) for a, b, c in matches]
    # If pts has more than n_expected (e.g. multiple "cryst. coord." blocks),
    # take the *last* n_expected — those correspond to the NSCF mesh.
    if len(pts) >= n_expected and n_expected > 0:
        return pts[-n_expected:]
    return pts if pts else None


# --------------------------------------------------------------------------
# VBM / CBM finder
# --------------------------------------------------------------------------
def find_vbm_cbm(
    eigenvalues_ev: list[list[float]],
    occupations: list[list[float]] | None,
    kpoints_cryst: list[tuple[float, float, float]],
    *,
    is_metal: bool,
    fermi_ev: float | None = None,
    occ_threshold: float = 0.5,
) -> dict:
    """Find VBM and CBM across the NSCF k-mesh.

    Args:
        eigenvalues_ev: nk × nbnd
        occupations: same shape, or None (then derived from fermi_ev)
        kpoints_cryst: k-points in crystal/fractional coords (nk × 3)
        is_metal: if True, returns gap=0 / metal labels regardless of bands
        fermi_ev: required if occupations is None (used to define VBM cut)
        occ_threshold: the occupation threshold (band > thr → occupied)

    Returns:
        {
            "vbm_ev": float | None,
            "cbm_ev": float | None,
            "vbm_k": (fx,fy,fz) | None,
            "cbm_k": (fx,fy,fz) | None,
            "band_gap_ev": float,
            "is_metal_band": bool,
        }
    """
    if is_metal:
        return {
            "vbm_ev": None, "cbm_ev": None,
            "vbm_k": None, "cbm_k": None,
            "band_gap_ev": 0.0,
            "is_metal_band": True,
        }

    if not eigenvalues_ev:
        return {
            "vbm_ev": None, "cbm_ev": None,
            "vbm_k": None, "cbm_k": None,
            "band_gap_ev": 0.0,
            "is_metal_band": True,
        }

    vbm_e = -float("inf")
    cbm_e = float("inf")
    vbm_k = None
    cbm_k = None
    band_crosses_fermi = False

    for ki, eigs in enumerate(eigenvalues_ev):
        if not eigs:
            continue
        kpt = kpoints_cryst[ki] if ki < len(kpoints_cryst) else (0.0, 0.0, 0.0)
        if occupations is not None and ki < len(occupations) and occupations[ki]:
            occs = occupations[ki]
            # Find max-band-index where occ > thr
            occupied_band_idx = -1
            for bi, o in enumerate(occs):
                if o > occ_threshold:
                    occupied_band_idx = bi
            # Detect metallicity: occupations strictly between (thr, 1-thr)
            for o in occs:
                if 0.05 < o < 0.95:
                    band_crosses_fermi = True
            if occupied_band_idx >= 0 and eigs[occupied_band_idx] > vbm_e:
                vbm_e = eigs[occupied_band_idx]
                vbm_k = kpt
            if occupied_band_idx + 1 < len(eigs) and eigs[occupied_band_idx + 1] < cbm_e:
                cbm_e = eigs[occupied_band_idx + 1]
                cbm_k = kpt
        else:
            # Use fermi_ev as splitter
            if fermi_ev is None:
                # last resort: assume half-filled
                n_occ = len(eigs) // 2
                if n_occ > 0 and eigs[n_occ - 1] > vbm_e:
                    vbm_e = eigs[n_occ - 1]
                    vbm_k = kpt
                if n_occ < len(eigs) and eigs[n_occ] < cbm_e:
                    cbm_e = eigs[n_occ]
                    cbm_k = kpt
            else:
                below = [e for e in eigs if e <= fermi_ev + 1e-6]
                above = [e for e in eigs if e > fermi_ev + 1e-6]
                if below and max(below) > vbm_e:
                    vbm_e = max(below)
                    vbm_k = kpt
                if above and min(above) < cbm_e:
                    cbm_e = min(above)
                    cbm_k = kpt

    if vbm_e == -float("inf") or cbm_e == float("inf"):
        # could not determine a gap
        return {
            "vbm_ev": None, "cbm_ev": None,
            "vbm_k": None, "cbm_k": None,
            "band_gap_ev": 0.0,
            "is_metal_band": True,
        }
    gap = cbm_e - vbm_e
    return {
        "vbm_ev": vbm_e,
        "cbm_ev": cbm_e,
        "vbm_k": vbm_k,
        "cbm_k": cbm_k,
        "band_gap_ev": max(0.0, gap),
        "is_metal_band": band_crosses_fermi or gap < 1e-3,
    }


# --------------------------------------------------------------------------
# k → seekpath label (B1 strategy)
# --------------------------------------------------------------------------
def match_k_to_seekpath_label(
    k_frac: tuple[float, float, float] | list[float],
    seekpath_point_coords: dict[str, list[float]],
    threshold: float = SEEKPATH_LABEL_THRESHOLD,
) -> str:
    """Find the closest seekpath high-symmetry label, else 'OFF_PATH'.

    Distance is plain Euclidean in fractional-coordinate space (no minimum-
    image — seekpath labels live in the first BZ corner, and our NSCF mesh
    points are already in [-0.5, 0.5)).
    """
    if k_frac is None:
        return "OFF_PATH"
    fx, fy, fz = float(k_frac[0]), float(k_frac[1]), float(k_frac[2])
    best_label = None
    best_dist = float("inf")
    for label, coord in seekpath_point_coords.items():
        cx, cy, cz = float(coord[0]), float(coord[1]), float(coord[2])
        # account for periodicity: try unit-cell shifts
        d = _frac_distance((fx, fy, fz), (cx, cy, cz))
        if d < best_dist:
            best_dist = d
            best_label = label
    if best_dist > threshold:
        return "OFF_PATH"
    return best_label or "OFF_PATH"


def _frac_distance(a, b) -> float:
    """Minimum-image fractional Euclidean distance (handles periodicity)."""
    import math as _m
    s = 0.0
    for ai, bi in zip(a, b):
        d = ai - bi
        d -= round(d)  # bring into [-0.5, 0.5]
        s += d * d
    return _m.sqrt(s)


# --------------------------------------------------------------------------
# bands.dat parser (QE bands.x output) + path-based VBM/CBM
# --------------------------------------------------------------------------
def parse_bands_dat(bands_dat_path: Path) -> dict:
    """Parse a QE bands.x output (`bands.dat`).

    Format produced by bands.x with default settings:

         &plot nbnd=  16, nks=  91 /
                  0.000000  0.000000  0.000000
           -5.7700  -1.4800   4.4400   ...
                  0.025000  0.012500  0.025000
           -5.7600  -1.4750   4.4300   ...
           ...

    The k-coordinates are in 2π/alat units (cartesian), printed on a single
    "header" line per block. Eigenvalues are space-separated floats wrapping
    multiple lines; their count equals nbnd.

    Returns:
        {
            "nbnd": int,
            "nks": int,
            "kpoints_cart_2pi_over_alat": list[(kx,ky,kz)] (length nks),
            "eigenvalues_ev": list[list[float]] (nks × nbnd),
        }
    """
    text = Path(bands_dat_path).read_text(encoding="utf-8", errors="ignore")
    # Header: " &plot nbnd=  N, nks=  M /"
    m = re.search(r"&plot\s+nbnd\s*=\s*(\d+)\s*,\s*nks\s*=\s*(\d+)\s*/", text,
                  flags=re.IGNORECASE)
    if not m:
        raise ValueError(f"bands.dat header not found in {bands_dat_path}")
    nbnd = int(m.group(1))
    nks = int(m.group(2))

    # Body after header
    body = text[m.end():]
    # Tokenize all floats
    floats = [float(x) for x in re.findall(r"-?\d+\.\d+(?:[eE][+-]?\d+)?", body)]

    # Each k-block = 3 (kx,ky,kz) + nbnd eigenvalues
    block = 3 + nbnd
    if len(floats) < nks * block:
        # Truncated file — try best-effort
        actual_nks = len(floats) // block
        if actual_nks < 1:
            raise ValueError(
                f"bands.dat truncated: have {len(floats)} floats, need {nks*block}")
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


def reconstruct_path_samples(
    flat_high_sym: list[tuple[str, list[float], int]],
) -> list[dict]:
    """Reconstruct what crystal-coord each bands.x k-sample should have.

    The bands_pw input uses K_POINTS crystal_b with `flat_high_sym`:

        [(label_a, [fx,fy,fz], n_per_seg), ...]

    where n_per_seg is the number of k-samples (inclusive of start, exclusive
    of end) BETWEEN that vertex and the next, except the last vertex which
    has n_per_seg=1 and contributes only the vertex itself.

    Returns a list of {idx, k_cryst, source_label, t} where:
      - idx: 0..total_samples-1
      - k_cryst: fractional crystal coords [fx,fy,fz]
      - source_label: 'vertex_<label>' if exactly at a vertex, else 'interior'
      - t: 0..n in segment (0=at vertex_a, n=at vertex_b).

    Note: QE crystal_b semantics — for a list of vertices V[0..M-1], the path
    goes V[0]→V[1]→...→V[M-1] with segment i having n_per_seg[i] points
    INCLUDING V[i] and EXCLUDING V[i+1] for i<M-1; V[M-1] is included as a
    single point (n_per_seg[M-1]=1). Total = sum(n_per_seg).
    """
    samples: list[dict] = []
    M = len(flat_high_sym)
    if M == 0:
        return samples
    cum = 0
    for i, (label, kp, n) in enumerate(flat_high_sym):
        if i == M - 1:
            # Last vertex contributes 1 sample
            samples.append({
                "idx": cum,
                "k_cryst": list(kp),
                "source_label": f"vertex_{label}",
                "t": 0,
            })
            cum += 1
            continue
        # Segment from this vertex to next; n samples inclusive of start, exclusive of end
        next_label, next_kp, _ = flat_high_sym[i + 1]
        for j in range(n):
            t = j / n  # fraction along segment, t=0 at start vertex
            kx = kp[0] + t * (next_kp[0] - kp[0])
            ky = kp[1] + t * (next_kp[1] - kp[1])
            kz = kp[2] + t * (next_kp[2] - kp[2])
            samples.append({
                "idx": cum + j,
                "k_cryst": [kx, ky, kz],
                "source_label": f"vertex_{label}" if j == 0 else "interior",
                "t": j,
            })
        cum += n
    return samples


def extract_band_gap_from_bands_dat(
    bands_dat_path: Path,
    flat_high_sym: list[tuple[str, list[float], int]],
    seekpath_point_coords: dict[str, list[float]],
    *,
    fermi_ev: float,
    is_metal: bool,
    homo_lumo_ev: tuple[float, float] | None = None,
) -> dict:
    """Find VBM/CBM/gap along the bands.x path (canonical抽法 for T2).

    Two splitter modes:

    1) Authoritative (preferred): pass `homo_lumo_ev=(homo, lumo)` parsed from
       NSCF's "highest occupied, lowest unoccupied level" line. Then:
         - vbm_ev = homo, cbm_ev = lumo (4 dp, no rounding artifacts)
         - mid = (homo+lumo)/2 used purely as a splitter to label k-points
         - gap = lumo - homo

    2) Fallback (metal NSCF, or HOMO/LUMO line absent): use `fermi_ev` as
       splitter. For each k-point, max(eig <= fermi) → VBM_k, min(eig > fermi)
       → CBM_k. Floats with eps=0.005 eV (bands.dat is rounded to 1 meV, so
       this absorbs rounding noise that previously misclassified Γ-VB at some
       ecutwfc as a conduction band).

    The k-coord of VBM/CBM is recovered from `flat_high_sym` (NOT from
    bands.dat header which is in 2π/alat cartesian).

    Args:
        bands_dat_path: path to bands.dat output of bands.x
        flat_high_sym: same list passed to render_bands_pw_input's
                       seekpath_path → flatten + n_per_seg
        seekpath_point_coords: {label: [fx,fy,fz]} for label resolution
        fermi_ev: Fermi energy from SCF (used only when homo_lumo_ev is None)
        is_metal: if True, returns gap=0 / metal labels
        homo_lumo_ev: optional (homo, lumo) tuple from NSCF; if given,
                      overrides energy values and uses mid-gap as splitter

    Returns: dict with vbm_ev, cbm_ev, vbm_k_cryst, cbm_k_cryst,
        vbm_k_label, cbm_k_label, band_gap_ev, gap_type, is_metal_band.
    """
    if is_metal:
        return {
            "vbm_ev": None, "cbm_ev": None,
            "vbm_k_cryst": None, "cbm_k_cryst": None,
            "vbm_k_label": "metal", "cbm_k_label": "metal",
            "band_gap_ev": 0.0,
            "gap_type": "metal",
            "is_metal_band": True,
        }

    parsed = parse_bands_dat(bands_dat_path)
    eigs_per_k = parsed["eigenvalues_ev"]
    samples = reconstruct_path_samples(flat_high_sym)

    if len(eigs_per_k) != len(samples):
        n = min(len(eigs_per_k), len(samples))
        eigs_per_k = eigs_per_k[:n]
        samples = samples[:n]

    if homo_lumo_ev is not None:
        # Authoritative mode: NSCF gave us 4-dp HOMO/LUMO. Use mid-gap as
        # splitter for k-label assignment, but trust the energy values directly.
        homo, lumo = homo_lumo_ev
        mid = 0.5 * (homo + lumo)
        # Find k-sample whose highest-occupied eigenvalue is closest to homo
        vbm_idx = -1
        best_vbm_diff = float("inf")
        for ki, eigs in enumerate(eigs_per_k):
            if not eigs:
                continue
            below = [e for e in eigs if e <= mid]
            if not below:
                continue
            cand_vbm = max(below)
            diff = abs(cand_vbm - homo)
            if diff < best_vbm_diff:
                best_vbm_diff = diff
                vbm_idx = ki
        cbm_idx = -1
        best_cbm_diff = float("inf")
        for ki, eigs in enumerate(eigs_per_k):
            if not eigs:
                continue
            above = [e for e in eigs if e > mid]
            if not above:
                continue
            cand_cbm = min(above)
            diff = abs(cand_cbm - lumo)
            if diff < best_cbm_diff:
                best_cbm_diff = diff
                cbm_idx = ki
        if vbm_idx < 0 or cbm_idx < 0:
            return {
                "vbm_ev": None, "cbm_ev": None,
                "vbm_k_cryst": None, "cbm_k_cryst": None,
                "vbm_k_label": "OFF_PATH", "cbm_k_label": "OFF_PATH",
                "band_gap_ev": 0.0,
                "gap_type": None,
                "is_metal_band": True,
            }
        vbm_e = homo
        cbm_e = lumo
        vbm_kc = samples[vbm_idx]["k_cryst"]
        cbm_kc = samples[cbm_idx]["k_cryst"]
    else:
        # Fallback: fermi-splitter mode. eps=0.005 eV (bands.dat rounding).
        vbm_e = -float("inf")
        cbm_e = float("inf")
        vbm_idx = -1
        cbm_idx = -1
        eps = 5e-3
        for ki, eigs in enumerate(eigs_per_k):
            if not eigs:
                continue
            below = [e for e in eigs if e <= fermi_ev + eps]
            above = [e for e in eigs if e > fermi_ev + eps]
            if below:
                cand_vbm = max(below)
                if cand_vbm > vbm_e:
                    vbm_e = cand_vbm
                    vbm_idx = ki
            if above:
                cand_cbm = min(above)
                if cand_cbm < cbm_e:
                    cbm_e = cand_cbm
                    cbm_idx = ki
        if vbm_idx < 0 or cbm_idx < 0:
            return {
                "vbm_ev": None, "cbm_ev": None,
                "vbm_k_cryst": None, "cbm_k_cryst": None,
                "vbm_k_label": "OFF_PATH", "cbm_k_label": "OFF_PATH",
                "band_gap_ev": 0.0,
                "gap_type": None,
                "is_metal_band": True,
            }
        vbm_kc = samples[vbm_idx]["k_cryst"]
        cbm_kc = samples[cbm_idx]["k_cryst"]

    vbm_label = match_k_to_seekpath_label(tuple(vbm_kc), seekpath_point_coords)
    cbm_label = match_k_to_seekpath_label(tuple(cbm_kc), seekpath_point_coords)

    gap = max(0.0, cbm_e - vbm_e)
    if gap < 1e-3:
        gap_type = "metal"
    else:
        d = _frac_distance(vbm_kc, cbm_kc)
        gap_type = "direct" if d < 0.05 else "indirect"
    band_crosses_fermi = gap < 1e-3

    return {
        "vbm_ev": vbm_e,
        "cbm_ev": cbm_e,
        "vbm_k_cryst": list(vbm_kc),
        "cbm_k_cryst": list(cbm_kc),
        "vbm_k_label": vbm_label,
        "cbm_k_label": cbm_label,
        "band_gap_ev": gap,
        "gap_type": gap_type,
        "is_metal_band": band_crosses_fermi,
    }


def extract_fermi_for_point(scf_out_path: Path) -> float | None:
    """Convenience wrapper: parse SCF output Fermi energy for a single scan
    point. Returns None if file missing or no fermi line found."""
    if not Path(scf_out_path).exists():
        return None
    return parse_fermi_energy_ev(scf_out_path)


# --------------------------------------------------------------------------
# DOS file
# --------------------------------------------------------------------------
def read_dos_dat(dos_dat_path: Path) -> dict:
    """Parse a dos.x output file. 兼容 spin-unpolarized + spin-polarized 两种格式。

    Format (spin-unpolarized, nspin=1)：3 列
        # E (eV)   dos(E)   Int dos(E)
        -25.0000   0.0000E+00  0.0000E+00
        ...

    Format (spin-polarized, nspin=2)：4 列
        # E (eV)  dos_up(E)  dos_dn(E)  Int dos(E)
        -25.0000  0.0000E+00 0.0000E+00 0.0000E+00
        ...
    The first comment line typically also contains "EFermi = X eV".

    Returns:
        {
            "energy_ev": list[float],
            "dos": list[float],                   # nspin=1: dos; nspin=2: dos_up + dos_dn
            "dos_up": list[float] | None,         # only when nspin=2
            "dos_dn": list[float] | None,         # only when nspin=2
            "int_dos": list[float] | None,
            "e_fermi_header": float | None,
            "is_spin_polarized": bool,
        }
    """
    text = Path(dos_dat_path).read_text(encoding="utf-8", errors="ignore")
    e_fermi_header = None
    energies: list[float] = []
    col1: list[float] = []
    col2: list[float] = []
    col3: list[float] = []
    max_data_cols = 0  # excluding energy column
    for line in text.splitlines():
        s = line.strip()
        if not s:
            continue
        if s.startswith("#"):
            m = re.search(r"EFermi\s*=\s*(-?\d+\.\d+)", s, flags=re.IGNORECASE)
            if m:
                e_fermi_header = float(m.group(1))
            continue
        parts = s.split()
        try:
            vals = [float(p) for p in parts]
        except ValueError:
            continue
        if len(vals) < 2:
            continue
        energies.append(vals[0])
        col1.append(vals[1])
        col2.append(vals[2] if len(vals) >= 3 else 0.0)
        col3.append(vals[3] if len(vals) >= 4 else 0.0)
        max_data_cols = max(max_data_cols, len(vals) - 1)

    is_spin_polarized = max_data_cols >= 3
    if is_spin_polarized:
        dos_up = col1
        dos_dn = col2
        int_dos = col3 if any(c != 0.0 for c in col3) else None
        dos_total = [u + d for u, d in zip(dos_up, dos_dn)]
        return {
            "energy_ev": energies,
            "dos": dos_total,
            "dos_up": dos_up,
            "dos_dn": dos_dn,
            "int_dos": int_dos,
            "e_fermi_header": e_fermi_header,
            "is_spin_polarized": True,
        }
    # spin-unpolarized
    dos = col1
    int_dos = col2 if any(c != 0.0 for c in col2) else None
    return {
        "energy_ev": energies,
        "dos": dos,
        "dos_up": None,
        "dos_dn": None,
        "int_dos": int_dos,
        "e_fermi_header": e_fermi_header,
        "is_spin_polarized": False,
    }
