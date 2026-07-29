"""FIX-37: AFM split-label normalization in the submitted vc-relax log.

The solver runs AFM vc-relax with QE-required split species labels (Cr1/Cr2) so
opposite starting_magnetization can be assigned. The bench T1 extractor keeps
the raw label and feeds it to SpacegroupAnalyzer, which treats Cr1 != Cr2 as
distinct species and reports a subgroup. FIX-37 merges the labels back to the
bare element (Cr1/Cr2 -> Cr) in the ATOMIC_POSITIONS block of the submitted
vc-relax.out, restoring the crystallographic symmetry the GT uses — while
leaving energies, magnetization lines, CELL_PARAMETERS and `JOB DONE.` intact.

These tests are string-level (no pymatgen) so they run under the minimal venv.
The end-to-end "sg recovers GT via the real bench extractor" check is validated
offline against the 15 M3 T1 outputs (see devlog / plan) and is not reproduced
here to keep the suite pymatgen-free.
"""
from __future__ import annotations

from pathlib import Path

from solve import (
    _normalize_atomic_positions_block,
    _normalize_afm_species_labels,
)

# A compact but realistic vc-relax.out tail: an intermediate ATOMIC_POSITIONS
# block, magnetization lines, a "Begin/End final coordinates" block with if_pos
# flags, and JOB DONE.
SAMPLE = """\
     Program PWSCF v.7.4 starts on 1Jan2026

     bravais-lattice index     =            0

CELL_PARAMETERS (angstrom)
   4.950000000   0.000000000   0.000000000
  -2.475000000   4.286687000   0.000000000
   0.000000000   0.000000000  13.600000000

ATOMIC_POSITIONS (crystal)
Cr1      0.000000000   0.000000000   0.347000000
Cr2      0.000000000   0.000000000   0.653000000
O        0.305000000   0.000000000   0.250000000

     total magnetization       =     0.00 Bohr mag/cell
     absolute magnetization    =    11.29 Bohr mag/cell

Begin final coordinates
CELL_PARAMETERS (angstrom)
   4.951000000   0.000000000   0.000000000
  -2.475500000   4.287000000   0.000000000
   0.000000000   0.000000000  13.610000000
ATOMIC_POSITIONS (crystal)
Cr1      0.000000000   0.000000000   0.347100000   1   1   1
Cr2      0.000000000   0.000000000   0.652900000   1   1   1
O        0.305100000   0.000000000   0.250000000
End final coordinates

     JOB DONE.
"""

# Non-magnetic analog: plain element labels, no trailing index.
SAMPLE_PLAIN = SAMPLE.replace("Cr1", "Cr ").replace("Cr2", "Cr ")


# --------------------------------------------------------------------------
# _normalize_atomic_positions_block
# --------------------------------------------------------------------------

def test_strips_trailing_index_from_atomic_positions():
    out = _normalize_atomic_positions_block(SAMPLE)
    # No split labels remain anywhere
    assert "Cr1" not in out and "Cr2" not in out
    # Both ATOMIC_POSITIONS blocks now use bare "Cr"
    assert out.count("Cr ") >= 4  # 2 Cr rows × 2 blocks


def test_preserves_cell_magnetization_and_jobdone():
    out = _normalize_atomic_positions_block(SAMPLE)
    # CELL_PARAMETERS numeric rows untouched
    assert "4.286687000" in out and "13.610000000" in out
    # magnetization lines (parsed by literal match in the bench) untouched
    assert "absolute magnetization    =    11.29 Bohr mag/cell" in out
    assert "total magnetization       =     0.00 Bohr mag/cell" in out
    # JOB DONE preserved
    assert "JOB DONE." in out
    # markers around the final-coordinates block intact
    assert "Begin final coordinates" in out and "End final coordinates" in out


def test_preserves_if_pos_flags_and_coords():
    out = _normalize_atomic_positions_block(SAMPLE)
    # the if_pos flags on the final-coordinate Cr rows survive
    assert "0.347100000   1   1   1" in out
    assert "0.652900000   1   1   1" in out


def test_noop_on_plain_labels():
    # Already-bare labels (non-magnetic case) must be returned unchanged.
    assert _normalize_atomic_positions_block(SAMPLE_PLAIN) == SAMPLE_PLAIN


def test_idempotent():
    once = _normalize_atomic_positions_block(SAMPLE)
    twice = _normalize_atomic_positions_block(once)
    assert once == twice


def test_only_atomic_positions_region_changes():
    # Every line that is NOT a split-label site row must be byte-identical.
    before = SAMPLE.split("\n")
    after = _normalize_atomic_positions_block(SAMPLE).split("\n")
    assert len(before) == len(after)
    for b, a in zip(before, after):
        if b.lstrip().startswith(("Cr1", "Cr2")):
            assert a != b  # the only lines that may change
        else:
            assert a == b


# --------------------------------------------------------------------------
# _normalize_afm_species_labels (gating + file I/O)
# --------------------------------------------------------------------------

class _Advice:
    def __init__(self, is_magnetic):
        self.is_magnetic = is_magnetic


def _setup(tmp_path: Path, key="vc_relax_out", text=SAMPLE):
    sub = tmp_path / "vc_relax_1"
    sub.mkdir()
    f = sub / "vc-relax.out"
    f.write_text(text)
    payload = {"task_outputs": {key: "vc_relax_1/vc-relax.out"}}
    return payload, f


def test_gate_magnetic_advice_rewrites(tmp_path):
    payload, f = _setup(tmp_path)
    _normalize_afm_species_labels(payload, tmp_path, _Advice(is_magnetic=True))
    assert "Cr1" not in f.read_text()


def test_gate_nonmagnetic_no_marker_untouched(tmp_path):
    payload, f = _setup(tmp_path)
    before = f.read_text()
    _normalize_afm_species_labels(payload, tmp_path, _Advice(is_magnetic=False))
    assert f.read_text() == before  # split labels left as-is (gate closed)


def test_gate_magnetic_ground_state_marker_rewrites(tmp_path):
    # advice missing/None but the probe wrote .magnetic_ground_state.json
    payload, f = _setup(tmp_path)
    (tmp_path / ".magnetic_ground_state.json").write_text('{"ordering": "AFM"}')
    _normalize_afm_species_labels(payload, tmp_path, None)
    assert "Cr1" not in f.read_text()


def test_skips_non_relax_keys(tmp_path):
    # A bands/dos output (no "relax" in key) must never be touched.
    payload, f = _setup(tmp_path, key="bands_data")
    before = f.read_text()
    _normalize_afm_species_labels(payload, tmp_path, _Advice(is_magnetic=True))
    assert f.read_text() == before


def test_missing_file_does_not_raise(tmp_path):
    payload = {"task_outputs": {"vc_relax_out": "nope/missing.out"}}
    # must be a no-op, not an exception
    _normalize_afm_species_labels(payload, tmp_path, _Advice(is_magnetic=True))
