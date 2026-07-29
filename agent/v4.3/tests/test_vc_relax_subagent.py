"""Unit tests for the T2 vc_relax preflight subagent.

These cover the deterministic surface area only (no LLM calls):
  - is_bands_dos_task task-type detection
  - parse_final_geometry regex extraction from QE pw.x .out tail
  - _validate_payload structural checks on subagent submit payload
  - render_prerelax_success / render_prerelax_failure prompt blocks
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from agent.subagents import vc_relax  # noqa: E402

# solve.is_bands_dos_task lives in the top-level solve module
import importlib  # noqa: E402
_solve = importlib.import_module("solve")
is_bands_dos_task = _solve.is_bands_dos_task


# ---------------------------------------------------------------------------
# task-type detection
# ---------------------------------------------------------------------------

class TestIsBandsDosTask:
    def test_positive_bands_data(self):
        assert is_bands_dos_task(["scf_out", "bands_out", "bands_data", "dos_dat"])

    def test_positive_dos_only(self):
        assert is_bands_dos_task(["scf_out", "dos_dat"])

    def test_positive_bands_only(self):
        assert is_bands_dos_task(["bands_data"])

    def test_negative_vc_relax(self):
        # M1_T1 keys: relaxed_structure (no bands/dos)
        assert not is_bands_dos_task(["relaxed_structure"])

    def test_negative_empty(self):
        assert not is_bands_dos_task([])


# ---------------------------------------------------------------------------
# QE .out parsing
# ---------------------------------------------------------------------------

_REAL_QE_TAIL = """\
End of BFGS Geometry Optimization

The maximum number of steps has been reached.

Begin final coordinates
     new unit-cell volume =    270.34672 a.u.^3 (    40.05677 Ang^3 )
     density =      2.41037 g/cm^3

CELL_PARAMETERS (angstrom)
   2.829828550   0.000000000   0.000000000
   1.414914275   2.450596862   0.000000000
   1.414914275   0.816865621   2.310404837

ATOMIC_POSITIONS (crystal)
Na             0.0000000000        0.0000000000        0.0000000000
Cl             0.5000000000        0.5000000000        0.5000000000
End final coordinates

Writing all to output data dir ./out/relax.save/ :
                                                    XML data file
     init_run     :      0.34s CPU      0.39s WALL (       1 calls)

     PWSCF        :      4.20s CPU      4.55s WALL


   This run was terminated on:  16:34: 5  15May2026

=------------------------------------------------------------------------------=
   JOB DONE.
=------------------------------------------------------------------------------=
"""


class TestParseFinalGeometry:
    def test_extracts_cell_and_positions(self):
        result = vc_relax.parse_final_geometry(_REAL_QE_TAIL)
        assert result is not None
        cell, positions = result
        assert cell.startswith("CELL_PARAMETERS")
        assert "2.829828550" in cell
        assert positions.startswith("ATOMIC_POSITIONS")
        assert "Na" in positions and "Cl" in positions
        # 3 cell rows, 2 atom rows
        assert cell.count("\n") == 3  # header + 3 rows = 4 lines, count("\n")==3 if no trailing
        assert positions.count("\n") == 2  # header + 2 rows = 3 lines, count("\n")==2

    def test_returns_none_on_missing_block(self):
        text = "no final coordinates anywhere\nJOB DONE.\n"
        assert vc_relax.parse_final_geometry(text) is None

    def test_returns_none_on_only_cell(self):
        text = """Begin final coordinates
CELL_PARAMETERS (angstrom)
1.0 0.0 0.0
0.0 1.0 0.0
0.0 0.0 1.0
End final coordinates
"""
        assert vc_relax.parse_final_geometry(text) is None


# ---------------------------------------------------------------------------
# Payload validation
# ---------------------------------------------------------------------------

class TestValidatePayload:
    def _setup(self, tmp_path, out_content: str | None = _REAL_QE_TAIL,
               out_size_bytes: int | None = None):
        workdir = tmp_path
        subwork = workdir / "vc_relax"
        subwork.mkdir()
        if out_content is not None:
            target = subwork / "relax.out"
            # Pad to ≥1024 B if needed
            content = out_content
            if out_size_bytes is not None:
                content = content + ("X" * max(0, out_size_bytes - len(content)))
            elif len(content) < 1024:
                content = content + ("X" * (1024 - len(content)))
            target.write_text(content)
        return workdir, subwork

    def test_happy_path(self, tmp_path):
        workdir, subwork = self._setup(tmp_path)
        payload = {"task_outputs": {"relax_out_path": "relax.out"},
                   "notes": "converged in 5 steps"}
        result = vc_relax._validate_payload(payload, workdir=workdir, subwork=subwork)
        assert result.ok
        assert result.relax_out_path == "vc_relax/relax.out"
        assert "CELL_PARAMETERS" in result.relaxed_cell
        assert "ATOMIC_POSITIONS" in result.relaxed_positions
        assert result.notes == "converged in 5 steps"

    def test_workdir_relative_path(self, tmp_path):
        workdir, subwork = self._setup(tmp_path)
        payload = {"task_outputs": {"relax_out_path": "vc_relax/relax.out"}}
        result = vc_relax._validate_payload(payload, workdir=workdir, subwork=subwork)
        assert result.ok

    def test_incomplete(self, tmp_path):
        workdir, subwork = self._setup(tmp_path, out_content=None)
        payload = {"incomplete": True, "reason": "ran out of turns"}
        result = vc_relax._validate_payload(payload, workdir=workdir, subwork=subwork)
        assert not result.ok
        assert "ran out of turns" in result.failure_reason

    def test_missing_relax_out_path(self, tmp_path):
        workdir, subwork = self._setup(tmp_path)
        payload = {"task_outputs": {"scf_out": "nope.out"}}
        result = vc_relax._validate_payload(payload, workdir=workdir, subwork=subwork)
        assert not result.ok
        assert "missing" in result.failure_reason.lower()

    def test_file_does_not_exist(self, tmp_path):
        workdir, subwork = self._setup(tmp_path, out_content=None)
        payload = {"task_outputs": {"relax_out_path": "ghost.out"}}
        result = vc_relax._validate_payload(payload, workdir=workdir, subwork=subwork)
        assert not result.ok
        assert "not found" in result.failure_reason.lower()

    def test_file_too_small(self, tmp_path):
        workdir, subwork = self._setup(tmp_path, out_content=None)
        (subwork / "relax.out").write_text("tiny\n")
        payload = {"task_outputs": {"relax_out_path": "relax.out"}}
        result = vc_relax._validate_payload(payload, workdir=workdir, subwork=subwork)
        assert not result.ok
        assert "too small" in result.failure_reason.lower()

    def test_missing_job_done(self, tmp_path):
        workdir, subwork = self._setup(tmp_path,
            out_content=_REAL_QE_TAIL.replace("JOB DONE.", "JOB_NOT_DONE"))
        payload = {"task_outputs": {"relax_out_path": "relax.out"}}
        result = vc_relax._validate_payload(payload, workdir=workdir, subwork=subwork)
        assert not result.ok
        assert "JOB DONE" in result.failure_reason

    def test_missing_final_coords(self, tmp_path):
        # has JOB DONE but no "Begin final coordinates" block
        text = "Some QE run output\n" + ("X" * 2000) + "\nJOB DONE.\n"
        workdir, subwork = self._setup(tmp_path, out_content=text)
        payload = {"task_outputs": {"relax_out_path": "relax.out"}}
        result = vc_relax._validate_payload(payload, workdir=workdir, subwork=subwork)
        assert not result.ok
        assert "final coordinates" in result.failure_reason.lower()


# ---------------------------------------------------------------------------
# Render helpers
# ---------------------------------------------------------------------------

class TestRenderPrerelaxBlocks:
    def test_success_block_contains_geometry_and_warnings(self):
        r = vc_relax.RelaxResult(
            ok=True,
            relaxed_cell="CELL_PARAMETERS (angstrom)\n  1.0 0.0 0.0\n  0.0 1.0 0.0\n  0.0 0.0 1.0",
            relaxed_positions="ATOMIC_POSITIONS (crystal)\n  Si  0.0 0.0 0.0",
            relax_out_path="vc_relax/relax.out",
            notes="3 ionic steps",
        )
        block = vc_relax.render_prerelax_success(r)
        assert "PRE-RELAXED GEOMETRY" in block
        assert "CELL_PARAMETERS" in block
        assert "ATOMIC_POSITIONS" in block
        assert "vc_relax/relax.out" in block
        assert "Do NOT run vc_relax again" in block
        assert "scf_out" in block  # caveat about fresh SCF
        assert "3 ionic steps" in block

    def test_failure_block_contains_reason(self):
        r = vc_relax.RelaxResult(ok=False, failure_reason="loop crashed: foo")
        block = vc_relax.render_prerelax_failure(r)
        assert "PRE-RELAX FALLBACK NOTICE" in block
        assert "loop crashed: foo" in block
        assert "ORIGINAL initial_structure" in block


# ---------------------------------------------------------------------------
# Initial structure extraction from rendered task_prompt.txt
# ---------------------------------------------------------------------------

_REAL_TASK_PROMPT = """\
For InP (zincblende, space group F-43m, 2 atoms per primitive cell), run a full bands + DOS workflow on the initial structure provided below:
(1) SCF on an appropriate k-mesh,
(2) NSCF along a high-symmetry k-path for band structure using bands.x,
(3) NSCF on a denser k-mesh for DOS using dos.x.

...some flavor text...

Initial structure (primitive cell, lattice constants in angstrom, angles in degree):
  a = 4.175, b = 4.175, c = 4.175
  alpha = 60.0, beta = 60.0, gamma = 60.0

ATOMIC_POSITIONS (crystal, i.e. fractional coordinates of the primitive cell):
  In  0.000000  0.000000  0.000000
  P  0.250000  0.250000  0.250000

Note: Starting geometry provided to agent.

Pseudopotentials: GBRV_USPP_PBE_v1.5 (scalar-relativistic).
UPF files available in the symlinked ./pseudos/ directory:
  In: In.upf
  P: P.upf

Execution hint: bands NSCF can take 30 min.

=== Submission Instructions ===
...
"""


class TestExtractInitialStructure:
    def test_extracts_through_pseudopotentials(self):
        block = vc_relax._extract_initial_structure_block(_REAL_TASK_PROMPT)
        assert "Initial structure" in block
        assert "a = 4.175" in block
        assert "ATOMIC_POSITIONS" in block
        assert "In.upf" in block
        assert "Execution hint" not in block
        assert "=== Submission" not in block

    def test_falls_back_when_no_marker(self):
        text = "no structure header here, just some prose"
        block = vc_relax._extract_initial_structure_block(text)
        assert block == text


# ---------------------------------------------------------------------------
# User-prompt injection (swap original Initial-structure block with relaxed)
# ---------------------------------------------------------------------------

class TestInjectRelaxedIntoUserPrompt:
    def _make_ok_result(self) -> vc_relax.RelaxResult:
        return vc_relax.RelaxResult(
            ok=True,
            relaxed_cell=(
                "CELL_PARAMETERS (angstrom)\n"
                "   4.031  0.000  0.000\n"
                "   2.016  3.491  0.000\n"
                "   2.016  1.164  3.291"
            ),
            relaxed_positions=(
                "ATOMIC_POSITIONS (crystal)\n"
                "  In   0.000  0.000  0.000\n"
                "  P    0.250  0.250  0.250"
            ),
            relax_out_path="vc_relax/relax.out",
            notes="converged",
        )

    def test_swaps_original_block(self):
        new = vc_relax.inject_relaxed_into_user_prompt(
            _REAL_TASK_PROMPT, self._make_ok_result(),
        )
        # Original Initial-structure header & lattice constants removed
        assert "Initial structure (primitive cell" not in new
        assert "a = 4.175" not in new
        # Relaxed geometry present
        assert "4.031" in new
        assert "CELL_PARAMETERS" in new
        assert "ATOMIC_POSITIONS" in new
        assert "Pre-relaxed structure" in new
        # Downstream sections preserved
        assert "Pseudopotentials:" in new
        assert "Execution hint" in new
        assert "=== Submission Instructions ===" in new

    def test_no_op_when_subagent_failed(self):
        bad = vc_relax.RelaxResult(ok=False, failure_reason="loop crashed")
        new = vc_relax.inject_relaxed_into_user_prompt(_REAL_TASK_PROMPT, bad)
        assert new == _REAL_TASK_PROMPT

    def test_fallback_prepends_when_no_match(self):
        text = "weird prompt with no Initial structure marker\nPseudopotentials: foo\n"
        # Subtle: regex requires 'Initial structure (primitive cell' header;
        # absent here → fallback prepends the replacement at the top.
        new = vc_relax.inject_relaxed_into_user_prompt(text, self._make_ok_result())
        assert new.startswith("[Pre-relaxed structure")
        assert "4.031" in new
        # original tail still preserved
        assert "weird prompt" in new


class TestFix25WallBudget:
    """FIX-25 (2026-05-25): vc_relax.run must forward max_wall_s to run_loop
    so the preflight subagent caps itself at the configured budget."""

    def test_run_accepts_max_wall_s_param(self, monkeypatch, tmp_path):
        """run() signature accepts max_wall_s kwarg and forwards to run_loop."""
        captured = {}

        def fake_run_loop(**kwargs):
            captured.update(kwargs)
            # Return an "incomplete" payload so _validate_payload routes to
            # the fallback path without trying to read a real relax.out
            return {"incomplete": True, "reason": "test stub"}

        monkeypatch.setattr(vc_relax, "run_loop", fake_run_loop)
        # Stub the LLM client (we never actually call it via fake_run_loop)
        class _StubClient:
            pass

        result = vc_relax.run(
            client=_StubClient(),
            original_prompt="For Si (cubic). Pseudopotentials: GBRV.",
            workdir=tmp_path,
            pw_x_path="/usr/bin/pw.x",
            qe_backend="gpu",
            max_turns=10,
            max_wall_s=7200,
        )
        # run_loop received the forwarded budget
        assert captured.get("max_wall_s") == 7200
        # _validate_payload's incomplete branch → ok=False
        assert result.ok is False
        assert "test stub" in (result.failure_reason or "")

    def test_run_default_max_wall_s_is_none(self, monkeypatch, tmp_path):
        """Backward-compat: caller omitting max_wall_s yields None → run_loop
        gets None → legacy unbounded behavior."""
        captured = {}

        def fake_run_loop(**kwargs):
            captured.update(kwargs)
            return {"incomplete": True, "reason": "test stub"}

        monkeypatch.setattr(vc_relax, "run_loop", fake_run_loop)

        class _StubClient:
            pass

        vc_relax.run(
            client=_StubClient(),
            original_prompt="For Si (cubic). Pseudopotentials: GBRV.",
            workdir=tmp_path,
            pw_x_path="/usr/bin/pw.x",
            qe_backend="gpu",
            max_turns=10,
        )
        # Default — not bumped
        assert captured.get("max_wall_s") is None


class TestFix27RetryPreserveOut:
    """FIX-27 (2026-05-25): vc_relax subagent prompt must instruct the LLM
    to KEEP `out/` between retries and use startingwfc/startingpot='file'
    to resume, not `rm -rf out` which wipes BFGS history + wavefunctions."""

    def test_prompt_forbids_rm_rf_out(self):
        prompt = vc_relax.build_vc_relax_system_prompt(
            workdir="/tmp/stub", pw_x_path="/usr/bin/pw.x",
            qe_backend="gpu", config_advice_block="",
        )
        # The explicit "do NOT" + rm -rf out guidance is present.
        assert "rm -rf out" in prompt
        assert "DO NOT" in prompt or "Do not" in prompt
        # The warm-restart section heading + FIX-27 marker
        assert "FIX-27" in prompt
        assert "warm-restart" in prompt or "resume" in prompt

    def test_prompt_teaches_startingwfc_file_restart(self):
        prompt = vc_relax.build_vc_relax_system_prompt(
            workdir="/tmp/stub", pw_x_path="/usr/bin/pw.x",
            qe_backend="gpu", config_advice_block="",
        )
        # Both directives required to resume
        assert "startingwfc = 'file'" in prompt
        assert "startingpot = 'file'" in prompt
        # Explanation of where the state lives
        assert "out/relax.save" in prompt

    def test_prompt_includes_corruption_escape_hatch(self):
        """Edge case: if charge-density.dat is corrupted, rm -rf out IS
        appropriate. Prompt must teach the agent when this escape hatch
        applies, otherwise the strict 'don't rm' rule prevents recovery."""
        prompt = vc_relax.build_vc_relax_system_prompt(
            workdir="/tmp/stub", pw_x_path="/usr/bin/pw.x",
            qe_backend="gpu", config_advice_block="",
        )
        assert "charge-density.dat" in prompt
        assert "corrupted" in prompt
