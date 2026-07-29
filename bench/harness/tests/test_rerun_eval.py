"""Unit tests for harness/rerun_eval.py helpers (S6).

Covers the legacy-mode helpers added for `--legacy-dos-detect`:
- _detect_dos_dat_relative: heuristic for EF-shifted dos.dat
- _find_nscf_dos_out_legacy: heuristic search for NSCF DOS pw.x output

CLI / full-pipeline integration is covered by the legacy probe (S6.6) which
runs against real runs_local data; here we only unit-test the helpers in
isolation with synthetic fixtures.
"""
from __future__ import annotations

from pathlib import Path

from harness.rerun_eval import _detect_dos_dat_relative, _find_nscf_dos_out_legacy


# --------------------------------------------------------------------------
# _detect_dos_dat_relative
# --------------------------------------------------------------------------
def test_detect_dos_dat_relative_true(tmp_path):
    """col 1 spans 0 ([-5, +5]) → True (agent-mutated, already EF-relative)."""
    p = tmp_path / "dos.dat"
    p.write_text(
        "#  E (eV)   dos(E)   Int dos(E)   EFermi =   7.0000 eV\n"
        "  -5.0000  1.0000E-01  1.0000E-01\n"
        "  -2.5000  1.0000E-01  1.0000E-01\n"
        "   0.0000  1.0000E-01  1.0000E-01\n"
        "   2.5000  1.0000E-01  1.0000E-01\n"
        "   5.0000  1.0000E-01  1.0000E-01\n",
        encoding="utf-8",
    )
    assert _detect_dos_dat_relative(p) is True


def test_detect_dos_dat_relative_false(tmp_path):
    """col 1 all positive ([2.4, 12.4]) → False (raw, absolute eV)."""
    p = tmp_path / "dos.dat"
    p.write_text(
        "#  E (eV)   dos(E)   Int dos(E)   EFermi =   7.4000 eV\n"
        "   2.4000  1.0000E-01  1.0000E-01\n"
        "   5.0000  1.0000E-01  1.0000E-01\n"
        "   7.4000  1.0000E-01  1.0000E-01\n"
        "  10.0000  1.0000E-01  1.0000E-01\n"
        "  12.4000  1.0000E-01  1.0000E-01\n",
        encoding="utf-8",
    )
    assert _detect_dos_dat_relative(p) is False


def test_detect_dos_dat_relative_empty(tmp_path):
    """Empty file (no numeric rows) → False (defensive)."""
    p = tmp_path / "dos.dat"
    p.write_text("#  E (eV)   dos(E)   Int dos(E)   EFermi =   7.0000 eV\n",
                 encoding="utf-8")
    assert _detect_dos_dat_relative(p) is False


def test_detect_dos_dat_relative_missing(tmp_path):
    """Missing file → False (catches the OSError)."""
    p = tmp_path / "does_not_exist.dat"
    assert _detect_dos_dat_relative(p) is False


# --------------------------------------------------------------------------
# _find_nscf_dos_out_legacy
# --------------------------------------------------------------------------
def test_find_nscf_dos_out_legacy_locates_file(tmp_path):
    """Common pattern `dos_nscf.out` with Fermi line → returned."""
    workdir = tmp_path
    dos_dat = workdir / "dos.dat"
    dos_dat.write_text("dummy", encoding="utf-8")
    nscf = workdir / "dos_nscf.out"
    nscf.write_text(
        "     End of band structure calculation\n"
        "     the Fermi energy is    7.4120 ev\n",
        encoding="utf-8",
    )
    found = _find_nscf_dos_out_legacy(workdir, dos_dat)
    assert found == nscf


def test_find_nscf_dos_out_legacy_no_fermi_line(tmp_path):
    """`dos_nscf.out` exists but has no Fermi line → return None
    (don't return a useless file)."""
    workdir = tmp_path
    dos_dat = workdir / "dos.dat"
    dos_dat.write_text("dummy", encoding="utf-8")
    nscf = workdir / "dos_nscf.out"
    nscf.write_text(
        "     End of band structure calculation\n"
        "     (no fermi line here)\n",
        encoding="utf-8",
    )
    assert _find_nscf_dos_out_legacy(workdir, dos_dat) is None


def test_find_nscf_dos_out_legacy_fallback_glob(tmp_path):
    """Non-canonical name `bands_nscf_dos.out` should still be picked up by
    the fallback `*.out` glob (contains 'nscf' or 'dos')."""
    workdir = tmp_path
    dos_dat = workdir / "dos.dat"
    dos_dat.write_text("dummy", encoding="utf-8")
    weird = workdir / "weird_nscf_for_dos_calc.out"
    weird.write_text(
        "     the Fermi energy is    5.0000 ev\n",
        encoding="utf-8",
    )
    found = _find_nscf_dos_out_legacy(workdir, dos_dat)
    assert found == weird


def test_find_nscf_dos_out_legacy_returns_none_when_no_candidates(tmp_path):
    """Directory has only unrelated files → None."""
    workdir = tmp_path
    dos_dat = workdir / "dos.dat"
    dos_dat.write_text("dummy", encoding="utf-8")
    (workdir / "scf.out").write_text("no fermi line", encoding="utf-8")
    assert _find_nscf_dos_out_legacy(workdir, dos_dat) is None
