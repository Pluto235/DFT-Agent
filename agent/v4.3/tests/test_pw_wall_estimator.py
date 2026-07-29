"""Tests for FIX-6: pw.x wall-time estimator (agent/pw_wall_estimator.py).

Covers:
- K_POINTS card parser across automatic / gamma / crystal_b / crystal forms
- Anchor inputs: Si vc-relax / Ge bands / CoSb3 nscf / WO3 vc-relax — assert
  the estimator returns the right order of magnitude (lower bound)
- Edge cases: non-pw.x command → None; missing .in file → None; malformed
  namelist → None (safe degradation)
"""
from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from agent import pw_wall_estimator
from agent.pw_wall_estimator import (
    _extract_n_kpoints,
    estimate_for_command,
    estimate_wall_s,
)


# ──────────────────────────────────────────────────────────────────────
# K_POINTS card parser
# ──────────────────────────────────────────────────────────────────────


def test_kpoints_automatic_6x6x6():
    text = textwrap.dedent("""\
        K_POINTS automatic
          6 6 6 0 0 0
    """)
    assert _extract_n_kpoints(text) == 216


def test_kpoints_automatic_with_braces():
    text = textwrap.dedent("""\
        K_POINTS {automatic}
          4 4 4 0 0 0
    """)
    assert _extract_n_kpoints(text) == 64


def test_kpoints_gamma():
    text = "K_POINTS gamma\n"
    assert _extract_n_kpoints(text) == 1


def test_kpoints_crystal_b_three_segments():
    # crystal_b form: header n_segments, then n_segments rows of "kx ky kz n_interp".
    text = textwrap.dedent("""\
        K_POINTS crystal_b
          3
          0.0  0.0  0.0  20
          0.5  0.0  0.0  20
          0.5  0.5  0.0  20
    """)
    assert _extract_n_kpoints(text) == 60


def test_kpoints_crystal_explicit_count():
    text = textwrap.dedent("""\
        K_POINTS crystal
          8
          0.0 0.0 0.0 0.125
          0.5 0.0 0.0 0.125
          0.5 0.5 0.0 0.125
          0.5 0.5 0.5 0.125
          0.0 0.5 0.5 0.125
          0.0 0.0 0.5 0.125
          0.25 0.25 0.0 0.125
          0.25 0.5 0.25 0.125
    """)
    assert _extract_n_kpoints(text) == 8


def test_kpoints_missing_returns_none():
    text = "&CONTROL\n  calculation = 'scf'\n/\n"
    assert _extract_n_kpoints(text) is None


def test_kpoints_malformed_returns_none():
    # automatic but header line missing 3 ints
    text = "K_POINTS automatic\n  6 6\n"
    assert _extract_n_kpoints(text) is None


# ──────────────────────────────────────────────────────────────────────
# Full input fixture estimates (anchor cases)
# ──────────────────────────────────────────────────────────────────────


_SI_VC_RELAX_IN = """\
&CONTROL
  calculation   = 'vc-relax'
  prefix        = 'si'
/
&SYSTEM
  ibrav   = 0
  nat     = 2
  ntyp    = 1
  ecutwfc = 40
/
&ELECTRONS
  conv_thr = 1.0d-8
/
&IONS
/
&CELL
/
ATOMIC_SPECIES
  Si  28.0855  Si.upf
ATOMIC_POSITIONS crystal
  Si  0.00 0.00 0.00
  Si  0.25 0.25 0.25
CELL_PARAMETERS angstrom
  3.0 0.0 0.0
  0.0 3.0 0.0
  0.0 0.0 3.0
K_POINTS automatic
  6 6 6 0 0 0
"""

_WO3_VC_RELAX_IN = """\
&CONTROL
  calculation = 'vc-relax'
  prefix      = 'wo3'
/
&SYSTEM
  ibrav   = 0
  nat     = 32
  ntyp    = 2
  ecutwfc = 80
/
&ELECTRONS
/
&IONS
/
&CELL
/
ATOMIC_SPECIES
  W  183.84  W.upf
  O  15.999   O.upf
ATOMIC_POSITIONS crystal
""" + "\n".join(f"  W  0.0 0.0 0.{i}" for i in range(8)) + "\n" + \
    "\n".join(f"  O  0.0 0.0 0.{i}" for i in range(24)) + textwrap.dedent("""
CELL_PARAMETERS angstrom
  7.7 0.0 0.0
  0.0 7.7 0.0
  0.0 0.0 7.8
K_POINTS automatic
  4 4 4 0 0 0
""")

_GE_BANDS_IN = """\
&CONTROL
  calculation = 'bands'
  prefix      = 'ge'
/
&SYSTEM
  ibrav   = 0
  nat     = 2
  ntyp    = 1
  ecutwfc = 50
/
&ELECTRONS
/
ATOMIC_SPECIES
  Ge  72.64  Ge.upf
ATOMIC_POSITIONS crystal
  Ge  0.0 0.0 0.0
  Ge  0.25 0.25 0.25
CELL_PARAMETERS angstrom
  3.0 0.0 0.0
  0.0 3.0 0.0
  0.0 0.0 3.0
K_POINTS crystal_b
  4
  0.0  0.0  0.0  20
  0.5  0.0  0.0  20
  0.5  0.5  0.0  20
  0.0  0.0  0.0  20
"""


def _write(tmp: Path, name: str, content: str) -> Path:
    p = tmp / name
    p.write_text(content)
    return p


def test_estimate_si_vc_relax_order_of_magnitude(tmp_path):
    p = _write(tmp_path, "si.in", _SI_VC_RELAX_IN)
    est = estimate_wall_s(p)
    # 公式：C × nat² × nk × (ecut/40)^1.5 × mult × safety
    #     = 4e-4 × 4 × 216 × 1 × 200 × 1.5 = 104s
    # Si vc-relax 应在 60-300s 之间（实际跑约 12s，估算允许 8× 过估）
    assert est is not None
    assert 60 <= est <= 300, f"Si est={est}s outside [60, 300]"


def test_estimate_wo3_vc_relax_raw_above_4h(tmp_path):
    p = _write(tmp_path, "wo3.in", _WO3_VC_RELAX_IN)
    # estimate_wall_s returns the raw formula output (NOT capped). Cap is
    # applied at estimate_for_command's exit (the BashTool integration
    # path). Tests at this layer assert the formula still puts WO3 above
    # the harness 4h wall so that BashTool's cap pathway is exercised.
    est = estimate_wall_s(p)
    # 公式：4e-4 × 32² × 64 × (80/40)^1.5 × 200 × 1.5 ≈ 22243s
    assert est is not None
    assert est >= 14400, f"WO3 raw est={est}s < 4h floor"


def test_estimate_ge_bands_returns_int(tmp_path):
    p = _write(tmp_path, "ge.in", _GE_BANDS_IN)
    est = estimate_wall_s(p)
    # bands multiplier=8, nk=80 (4 segments × 20)
    # 4e-4 × 4 × 80 × (50/40)^1.5 × 8 × 1.5 ≈ 5.4s → clamped to 30s lower bound
    assert est is not None
    assert est >= 30


# ──────────────────────────────────────────────────────────────────────
# Command-level entry: estimate_for_command
# ──────────────────────────────────────────────────────────────────────


def test_estimate_for_command_pw_x(tmp_path):
    _write(tmp_path, "wo3.in", _WO3_VC_RELAX_IN)
    cmd = "pw.x -in wo3.in > wo3.out 2>&1"
    est, dbg = estimate_for_command(cmd, tmp_path)
    # FIX-39 (S-3): estimate_for_command returns the RAW estimate (no fixed
    # self-cap). WO3 32-atom vc-relax is a large raw value; the harness-aware
    # ceiling is applied later by BashTool._bash_cap_s(), not here.
    assert est >= 14400, f"WO3 raw est={est} should be the full (large) estimate"
    assert dbg.get("capped") is not True
    assert "uncapped_estimate_s" not in dbg
    assert dbg["n_atoms"] == 32
    assert dbg["n_kpoints"] == 64
    assert dbg["ecutwfc"] == 80.0
    assert dbg["calculation"] == "vc-relax"


def test_estimate_for_command_small_system_not_capped(tmp_path):
    """Small system (Si vc-relax) should NOT be capped — raw est ~ 100s."""
    _write(tmp_path, "si.in", _SI_VC_RELAX_IN)
    cmd = "pw.x -in si.in > si.out 2>&1"
    est, dbg = estimate_for_command(cmd, tmp_path)
    assert est is not None
    assert 60 <= est <= 300, f"Si est={est} outside [60, 300]"
    assert dbg.get("capped") is not True
    assert "uncapped_estimate_s" not in dbg


def test_estimate_for_command_non_pwx_returns_none(tmp_path):
    est, dbg = estimate_for_command("ls -la && echo hello", tmp_path)
    assert est is None
    assert dbg == {}


def test_estimate_for_command_pwx_but_no_input(tmp_path):
    # pw.x token present but no -in / stdin redirect
    est, dbg = estimate_for_command("pw.x --version", tmp_path)
    assert est is None


def test_estimate_for_command_input_not_found(tmp_path):
    cmd = "pw.x -in does_not_exist.in"
    est, dbg = estimate_for_command(cmd, tmp_path)
    assert est is None


def test_estimate_for_command_malformed_input(tmp_path):
    p = _write(tmp_path, "bad.in", "this is not a QE input file\n")
    cmd = f"pw.x -in {p.name}"
    est, dbg = estimate_for_command(cmd, tmp_path)
    # Missing &SYSTEM.nat / .ecutwfc / K_POINTS → safe degradation
    assert est is None


def test_estimate_mpirun_pwx(tmp_path):
    _write(tmp_path, "si.in", _SI_VC_RELAX_IN)
    cmd = "mpirun --allow-run-as-root -np 1 pw.x -in si.in > si.out 2>&1"
    est, dbg = estimate_for_command(cmd, tmp_path)
    assert est is not None
    assert dbg["n_atoms"] == 2
