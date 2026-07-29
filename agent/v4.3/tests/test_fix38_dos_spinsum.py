"""FIX-38: sum nspin=2 spin channels in the submitted dos.dat.

QE's dos.x writes per-spin columns (E dosup dosdw intdos) for nspin=2, but the
bench dos_curve reference is the TOTAL DOS and the bench's read_dos_dat only
reads col 1 (dosup) — half the DOS — so recall caps near 0.5. FIX-38 collapses
the file to (E dosup+dosdw intdos) so the bench reads the total. Proven offline:
CrO2 dos_curve 0.647 -> 0.945. nspin=1 (3-column) files are left unchanged.
"""
from __future__ import annotations

from pathlib import Path

from solve import _sum_dos_dat_text, _sum_dos_spin_channels

NSPIN2 = """\
#  E (eV)   dosup(E)     dosdw(E)   Int dos(E) EFermi =   10.531 eV
  -2.000  1.0000E+00  0.5000E+00  0.1000E+01
  -1.000  2.0000E+00  1.0000E+00  0.3000E+01
   0.000  3.0000E+00  2.0000E+00  0.6000E+01
"""

NSPIN1 = """\
#  E (eV)   dos(E)     Int dos(E) EFermi =   17.823 eV
  -2.000  1.5000E+00  0.1000E+01
  -1.000  3.0000E+00  0.3000E+01
   0.000  5.0000E+00  0.6000E+01
"""


def _dos_col(line):
    return [float(x) for x in line.split()]


def test_sums_spin_channels_into_col1():
    out = _sum_dos_dat_text(NSPIN2)
    data = [l for l in out.splitlines() if l.strip() and not l.startswith("#")]
    # now 3 columns, col1 = dosup + dosdw
    cols = [_dos_col(l) for l in data]
    assert all(len(c) == 3 for c in cols)
    assert cols[0][1] == 1.5    # 1.0 + 0.5
    assert cols[1][1] == 3.0    # 2.0 + 1.0
    assert cols[2][1] == 5.0    # 3.0 + 2.0
    # intdos (was col4) preserved as col3
    assert cols[2][2] == 6.0


def test_preserves_header_and_efermi():
    out = _sum_dos_dat_text(NSPIN2)
    assert out.splitlines()[0].startswith("#")
    assert "EFermi =   10.531 eV" in out  # read_dos_dat parses this


def test_nspin1_is_noop():
    assert _sum_dos_dat_text(NSPIN1) == NSPIN1


def test_idempotent():
    once = _sum_dos_dat_text(NSPIN2)
    # after the first pass it is 3-column → second pass is a no-op
    assert _sum_dos_dat_text(once) == once


def _setup(tmp_path: Path, key="dos_dat", text=NSPIN2):
    sub = tmp_path / "dos_1"
    sub.mkdir(exist_ok=True)
    f = sub / "dos.dat"
    f.write_text(text)
    return {"task_outputs": {key: "dos_1/dos.dat"}}, f


def test_spin_channels_rewrites_dos_dat(tmp_path):
    payload, f = _setup(tmp_path)
    _sum_dos_spin_channels(payload, tmp_path)
    cols = [_dos_col(l) for l in f.read_text().splitlines()
            if l.strip() and not l.startswith("#")]
    assert cols[0][1] == 1.5 and all(len(c) == 3 for c in cols)


def test_skips_non_dos_keys(tmp_path):
    payload, f = _setup(tmp_path, key="bands_data")
    before = f.read_text()
    _sum_dos_spin_channels(payload, tmp_path)
    assert f.read_text() == before


def test_nspin1_file_untouched(tmp_path):
    payload, f = _setup(tmp_path, text=NSPIN1)
    before = f.read_text()
    _sum_dos_spin_channels(payload, tmp_path)
    assert f.read_text() == before


def test_missing_file_does_not_raise(tmp_path):
    payload = {"task_outputs": {"dos_dat": "dos_1/missing.dat"}}
    _sum_dos_spin_channels(payload, tmp_path)
