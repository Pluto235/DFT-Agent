"""pw_wall_estimator output + bash timeout cap.

FIX-15 (2026-05-23) added two layers against the WO3 Popen-hang:
  (a) estimate_for_command hard-capped its return to a fixed 12000s
  (b) BashTool capped any timeout to 0.8 × harness wall

FIX-39 (S-3, 2026-05-31) removes layer (a)'s *fixed* 12000s cap, which was
the root cause of the Fe3O4/LaMnO3 M3_T2 timeouts: a legitimate big
magnetic-cell vc-relax (~13000s) under an 8h harness budget was killed at
12000s, restarted from scratch, and orphaned its mpirun → core contention →
0.000. The cap is now harness-wall-aware and lives in ONE place (bash layer):
the per-command ceiling is 0.8 × SOLVER_HARNESS_WALL_S, read at CALL time.
Under a 4h budget the ceiling is 11520s (~old 12000s); under 8h it is 23040s,
so a 13000s run is allowed to finish.
"""
from __future__ import annotations
import textwrap
from pathlib import Path

import pytest

from agent.pw_wall_estimator import (
    estimate_for_command,
    estimate_wall_s,
)


# Real WO3 32-atom vc-relax that triggered the hang on 2026-05-22.
_WO3_REAL_INPUT = textwrap.dedent("""\
    &CONTROL
      calculation = 'vc-relax'
      prefix = 'wo3'
      outdir = './out'
      pseudo_dir = './pseudos'
    /
    &SYSTEM
      ibrav = 0
      nat = 32
      ntyp = 2
      ecutwfc = 80
      ecutrho = 800
    /
    &ELECTRONS
    /
    &IONS
    /
    &CELL
    /
    ATOMIC_SPECIES
      W 183.84 W.upf
      O 16.00 O.upf
    ATOMIC_POSITIONS crystal
      W 0 0 0
    CELL_PARAMETERS angstrom
      7.7 0 0
      0 7.7 0
      0 0 7.7
    K_POINTS automatic
      8 8 8 0 0 0
    """)


# A mid-size magnetic vc-relax (≈ Fe3O4 14-atom shape) whose raw estimate
# sits BETWEEN the 4h cap (11520s) and the 8h cap (23040s). This is the
# exact regression band: under 4h it must be capped; under 8h it must run
# at its full estimate (the old fixed 12000s cap broke this).
_MAGNETIC_VC_RELAX_INPUT = textwrap.dedent("""\
    &CONTROL
      calculation = 'vc-relax'
      prefix = 'fe3o4'
      outdir = './out'
      pseudo_dir = './pseudos'
    /
    &SYSTEM
      ibrav = 0
      nat = 14
      ntyp = 2
      ecutwfc = 60
      ecutrho = 600
      nspin = 2
    /
    &ELECTRONS
    /
    &IONS
    /
    &CELL
    /
    ATOMIC_SPECIES
      Fe 55.85 Fe.upf
      O 16.00 O.upf
    ATOMIC_POSITIONS crystal
      Fe 0 0 0
    CELL_PARAMETERS angstrom
      8.4 0 0
      0 8.4 0
      0 0 8.4
    K_POINTS automatic
      7 7 6 0 0 0
    """)


def _write(tmp_path: Path, name: str, content: str) -> Path:
    p = tmp_path / name
    p.write_text(content)
    return p


# ──────────────────────────────────────────────────────────────────────
# Estimator layer: no longer self-caps (capping is the bash layer's job)
# ──────────────────────────────────────────────────────────────────────


def test_estimator_returns_raw_not_capped(tmp_path):
    """The estimator no longer imposes a fixed 12000s cap. WO3 returns its
    full raw estimate (≥100000s); the bash layer caps it harness-aware."""
    _write(tmp_path, "wo3.in", _WO3_REAL_INPUT)
    est, dbg = estimate_for_command("pw.x -in wo3.in", tmp_path)
    assert est is not None and est >= 100000, f"WO3 est={est} should be raw, not capped"
    assert dbg.get("capped") is not True


def test_raw_estimate_wall_s_uncapped(tmp_path):
    """estimate_wall_s() returns the RAW formula output (unchanged)."""
    _write(tmp_path, "wo3.in", _WO3_REAL_INPUT)
    est = estimate_wall_s(tmp_path / "wo3.in")
    assert est is not None and est >= 100000, f"raw WO3 est={est} not above 100000"


def test_estimator_and_estimate_for_command_agree(tmp_path):
    """estimate_for_command now returns exactly the raw estimate (no cap)."""
    _write(tmp_path, "wo3.in", _WO3_REAL_INPUT)
    raw = estimate_wall_s(tmp_path / "wo3.in")
    est, _dbg = estimate_for_command("pw.x -in wo3.in", tmp_path)
    assert est == raw


# ──────────────────────────────────────────────────────────────────────
# BashTool layer: harness-wall cap, read at CALL time
# ──────────────────────────────────────────────────────────────────────


def test_bash_cap_reads_harness_wall_at_call_time(monkeypatch):
    """_bash_cap_s() = 0.8 × SOLVER_HARNESS_WALL_S, evaluated each call."""
    from agent.tools import bash as bash_mod
    monkeypatch.setenv("SOLVER_HARNESS_WALL_S", "28800")
    assert bash_mod._bash_cap_s() == 23040
    monkeypatch.setenv("SOLVER_HARNESS_WALL_S", "14400")
    assert bash_mod._bash_cap_s() == 11520
    monkeypatch.delenv("SOLVER_HARNESS_WALL_S", raising=False)
    assert bash_mod._bash_cap_s() == 11520  # default 14400 → 11520


def test_bash_caps_oversized_timeout_under_4h(tmp_path, monkeypatch):
    """LLM gives timeout_s above the 4h cap — bash caps it to 11520s."""
    monkeypatch.setenv("SOLVER_BASH_GUARD", "off")
    monkeypatch.setenv("SOLVER_HARNESS_WALL_S", "14400")
    from agent.tools import bash as bash_mod
    tool = bash_mod.BashTool(workdir=tmp_path, default_timeout_s=60)
    result = tool.run("echo hello", timeout_s=99999)
    assert result["exit_code"] == 0
    assert "capped to 11520s" in result["stdout"]


def test_bash_cap_scales_with_8h_budget(tmp_path, monkeypatch):
    """Under an 8h budget the cap is 23040s, not 11520s — an oversized
    timeout caps to the larger ceiling."""
    monkeypatch.setenv("SOLVER_BASH_GUARD", "off")
    monkeypatch.setenv("SOLVER_HARNESS_WALL_S", "28800")
    from agent.tools import bash as bash_mod
    tool = bash_mod.BashTool(workdir=tmp_path, default_timeout_s=60)
    result = tool.run("echo hello", timeout_s=99999)
    assert result["exit_code"] == 0
    assert "capped to 23040s" in result["stdout"]


# ──────────────────────────────────────────────────────────────────────
# The S-3 regression: big magnetic vc-relax must use the FULL 8h budget
# ──────────────────────────────────────────────────────────────────────


def test_big_magnetic_vc_relax_runs_full_estimate_under_8h(tmp_path, monkeypatch):
    """THE Fe3O4/LaMnO3 fix: a ~13000s magnetic vc-relax under an 8h budget
    is raised to its full estimate and NOT capped (old fixed 12000s cap
    killed it → restart → orphan → 0.000)."""
    monkeypatch.setenv("SOLVER_BASH_GUARD", "off")
    inp = _write(tmp_path, "fe3o4.in", _MAGNETIC_VC_RELAX_INPUT)
    raw = estimate_wall_s(inp)
    # Design sanity: the test input must sit between the 4h and 8h caps.
    assert raw is not None and 11520 < raw < 23040, (
        f"test input mis-sized: raw={raw}, must be in (11520, 23040)"
    )
    monkeypatch.setenv("SOLVER_HARNESS_WALL_S", "28800")
    from agent.tools import bash as bash_mod
    tool = bash_mod.BashTool(workdir=tmp_path, default_timeout_s=60)
    res = tool.run("pw.x -in fe3o4.in", timeout_s=60)
    note = (res["stdout"] or "") + (res["stderr"] or "")
    assert f"→ {raw}s" in note, f"estimate not applied; note={note[:300]}"
    assert "capped to" not in note, f"should NOT be capped under 8h; note={note[:300]}"


def test_big_magnetic_vc_relax_capped_under_4h(tmp_path, monkeypatch):
    """Contrast: the SAME input under a 4h budget IS capped to 11520s
    (the watchdog must still fire inside the smaller wall)."""
    monkeypatch.setenv("SOLVER_BASH_GUARD", "off")
    monkeypatch.setenv("SOLVER_HARNESS_WALL_S", "14400")
    inp = _write(tmp_path, "fe3o4.in", _MAGNETIC_VC_RELAX_INPUT)
    raw = estimate_wall_s(inp)
    assert raw is not None and raw > 11520
    from agent.tools import bash as bash_mod
    tool = bash_mod.BashTool(workdir=tmp_path, default_timeout_s=60)
    res = tool.run("pw.x -in fe3o4.in", timeout_s=60)
    note = (res["stdout"] or "") + (res["stderr"] or "")
    assert "capped to 11520s" in note, f"should be capped under 4h; note={note[:300]}"
