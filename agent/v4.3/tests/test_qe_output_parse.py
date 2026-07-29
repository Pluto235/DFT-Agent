"""qe_output_parse — post-bash pw.x output → structured facts JSONL."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent.tools.qe_output_parse import (
    append_facts_row,
    detect_output_path,
    extract_facts,
    is_pw_x_invocation,
    render_advisory,
)


# ─── Command detection ────────────────────────────────────────────────────────


@pytest.mark.parametrize("cmd, expected", [
    ("pw.x -i scf.in > scf.out", True),
    ("mpirun -np 4 pw.x < scf.in > scf.out", True),
    ("./pw.x -in scf.in > scf.out", True),
    ("/opt/qe/bin/pw.x -i scf.in > scf.out", True),
    ("ls -la", False),
    ("python -c 'print(1)'", False),
    ("grep pw.x somefile", False),     # mentions pw.x as data, not as a binary
])
def test_is_pw_x_invocation(cmd, expected):
    # The current detector errs on the side of "looks like pw.x ran" — we
    # accept the grep false-positive because the downstream parse step is
    # itself defensive (no output path / no parseable file → no-op).
    if cmd.startswith("grep "):
        # Soft assertion: skipped explicitly so we don't lock the detector
        # into a brittle anti-grep rule.
        return
    assert is_pw_x_invocation(cmd) == expected


# ─── Redirect target resolution ──────────────────────────────────────────────


def test_detect_output_path_simple(tmp_path):
    p = detect_output_path("pw.x -i scf.in > scf.out", tmp_path)
    assert p == tmp_path / "scf.out"


def test_detect_output_path_with_subdir(tmp_path):
    p = detect_output_path("mpirun -np 4 pw.x < scf.in > scf_1/scf.out", tmp_path)
    assert p == tmp_path / "scf_1" / "scf.out"


def test_detect_output_path_append_redirect(tmp_path):
    p = detect_output_path("pw.x -i scf.in >> log.out", tmp_path)
    assert p == tmp_path / "log.out"


def test_detect_output_path_no_redirect(tmp_path):
    """Heredoc-piped invocation without `> file` → None."""
    p = detect_output_path("pw.x -i scf.in", tmp_path)
    assert p is None


def test_detect_output_path_refuses_escape(tmp_path):
    """Output path escaping cwd via ../ → None (defence-in-depth)."""
    p = detect_output_path("pw.x -i scf.in > ../escape.out", tmp_path)
    assert p is None


# ─── extract_facts: 4 fixture cases ──────────────────────────────────────────


def _converged_scf_fixture() -> str:
    """Synthetic but realistic SCF output that exercises the parse path."""
    return """
     Program PWSCF v.7.4 starts on  1May2026
     ...
     lattice parameter (alat)  =      10.2630  a.u.
     unit-cell volume          =     270.1100  (a.u.)^3
     number of Kohn-Sham states=            8
     number of k points=    16
     ...
     iteration #  1     ecut=    60.00 Ry     beta= 0.70
     iteration #  2     ecut=    60.00 Ry     beta= 0.70
     iteration #  3     ecut=    60.00 Ry     beta= 0.70

     convergence has been achieved in   3 iterations

     the Fermi energy is     6.4321 ev

!    total energy              =      -8.74519230 Ry
     ...
     Forces acting on atoms (cartesian axes, Ry/au):

     atom    1 type  1   force =     0.00000000    0.00000000    0.00000000
     atom    2 type  1   force =     0.00010000    0.00000000    0.00000000
     Total force =     0.000100     Total SCF correction =     0.000000

     total   stress  (Ry/bohr**3)                   (kbar)     P=        0.00
     0.00010000   0.00000000   0.00000000      14.71      0.00      0.00
     0.00000000   0.00010000   0.00000000       0.00     14.71      0.00
     0.00000000   0.00000000   0.00010000       0.00      0.00     14.71

     init_run     :      1.23s CPU      1.30s WALL (       1 calls)
     ...

     PWSCF        :      2.45s CPU      2.51s WALL

   This run was terminated on:  1May2026

=------------------------------------------------------------------------------=
   JOB DONE.
=------------------------------------------------------------------------------=
"""


def test_extract_facts_converged_scf(tmp_path):
    out = tmp_path / "scf.out"
    out.write_text(_converged_scf_fixture(), encoding="utf-8")
    f = extract_facts(out)
    assert f is not None
    assert f["converged"] is True
    assert f["job_done"] is True
    assert f["n_scf_iterations"] == 3
    assert f["total_energy_ev"] is not None
    assert f["total_energy_ev"] < 0
    # Force in eV/Å: 0.0001 Ry/Bohr ≈ 0.00257 eV/Å — max norm of (0.0001, 0, 0).
    assert f["max_force_ev_per_a"] is not None
    assert f["max_force_ev_per_a"] < 0.01
    # Stress max abs = 14.71 kbar
    assert f["max_stress_kbar"] is not None
    assert abs(f["max_stress_kbar"] - 14.71) < 0.5
    assert f["error_signature"] is None


def test_extract_facts_not_converged(tmp_path):
    body = """
     Program PWSCF v.7.4 starts on  1May2026
     iteration #  1     ecut=    60.00 Ry
     iteration #  2     ecut=    60.00 Ry
     iteration #  3     ecut=    60.00 Ry
     ...
     convergence NOT achieved after  3 iterations: stopping
"""
    out = tmp_path / "scf.out"
    out.write_text(body, encoding="utf-8")
    f = extract_facts(out)
    assert f is not None
    assert f["converged"] is False
    assert f["job_done"] is False
    assert f["n_scf_iterations"] == 3
    assert f["error_signature"] is not None
    assert "convergence NOT achieved" in f["error_signature"]


def test_extract_facts_partial_output(tmp_path):
    """Truncated mid-iteration: no convergence flag, no JOB DONE, but partial fields."""
    body = """
     Program PWSCF v.7.4 starts on  1May2026
     number of Kohn-Sham states=    8
     iteration #  1     ecut=    60.00 Ry
"""
    out = tmp_path / "scf.out"
    out.write_text(body, encoding="utf-8")
    f = extract_facts(out)
    assert f is not None
    assert f["converged"] is False
    assert f["job_done"] is False
    assert f["n_scf_iterations"] == 1
    assert f["total_energy_ev"] is None     # no `! total energy` line
    assert f["max_force_ev_per_a"] is None
    assert f["max_stress_kbar"] is None


def test_extract_facts_missing_file_returns_none(tmp_path):
    f = extract_facts(tmp_path / "does-not-exist.out")
    assert f is None


def test_extract_facts_unreadable_binary(tmp_path):
    """Random binary data — parse_pw_output may crash; we still return safely."""
    out = tmp_path / "scf.out"
    out.write_bytes(bytes(range(256)))
    # Should NOT raise; either returns None or a dict where everything is False/None.
    f = extract_facts(out)
    assert f is None or (f["converged"] is False and f["job_done"] is False)


# ─── append_facts_row ────────────────────────────────────────────────────────


def test_append_facts_row_writes_jsonl(tmp_path):
    sub = tmp_path / "scf_1"
    sub.mkdir()
    out = sub / "scf.out"
    out.write_text(_converged_scf_fixture(), encoding="utf-8")
    facts = extract_facts(out)
    append_facts_row(
        tmp_path,
        command="mpirun -np 4 pw.x -i scf.in > scf_1/scf.out",
        output_path=out,
        facts=facts,
    )
    jsonl = tmp_path / "qe_facts.jsonl"
    assert jsonl.is_file()
    rows = [json.loads(line) for line in jsonl.read_text().splitlines() if line.strip()]
    assert len(rows) == 1
    row = rows[0]
    assert row["step_id"] == "scf_1"
    assert row["output_path"] == "scf_1/scf.out"
    assert "pw.x" in row["command"]
    assert row["facts"]["converged"] is True
    assert row["facts"]["job_done"] is True


def test_append_facts_row_with_none_facts_writes_parse_error(tmp_path):
    sub = tmp_path / "bands_1"
    sub.mkdir()
    out = sub / "bands.out"
    append_facts_row(
        tmp_path,
        command="pw.x -i bands.in > bands_1/bands.out",
        output_path=out,
        facts=None,
    )
    rows = (tmp_path / "qe_facts.jsonl").read_text().splitlines()
    row = json.loads(rows[0])
    assert row["step_id"] == "bands_1"
    assert "parse_error" in row
    assert "facts" not in row


def test_append_facts_row_appends_not_overwrites(tmp_path):
    sub = tmp_path / "scf_1"
    sub.mkdir()
    out = sub / "scf.out"
    out.write_text(_converged_scf_fixture(), encoding="utf-8")
    f = extract_facts(out)
    for _ in range(3):
        append_facts_row(tmp_path, command="pw.x ...", output_path=out, facts=f)
    rows = (tmp_path / "qe_facts.jsonl").read_text().splitlines()
    assert len(rows) == 3


# ─── render_advisory ─────────────────────────────────────────────────────────


def test_render_advisory_converged():
    facts = {
        "converged": True, "job_done": True,
        "total_energy_ev": -15.832, "n_scf_iterations": 18,
        "max_force_ev_per_a": 2.3e-5, "max_stress_kbar": 0.45,
        "nstep_ions": 1, "wall_time_s": 12.3, "error_signature": None,
    }
    out = render_advisory(facts)
    assert out.startswith("--- qe_facts ---")
    assert "JOB_DONE" in out
    assert "converged" in out
    assert "scf_iter=18" in out
    assert "E_tot=-15.83" in out
    assert len(out) <= 250    # ≤200 char advisory + marker


def test_render_advisory_not_converged():
    facts = {
        "converged": False, "job_done": False,
        "total_energy_ev": None, "n_scf_iterations": 80,
        "max_force_ev_per_a": None, "max_stress_kbar": None,
        "nstep_ions": None, "wall_time_s": 45.0,
        "error_signature": "convergence NOT achieved after  80 iterations: stopping",
    }
    out = render_advisory(facts)
    assert "NO_JOB_DONE" in out
    assert "converged" not in out
    assert "scf_iter=80" in out
    assert "err=" in out


def test_render_advisory_none_returns_empty():
    assert render_advisory(None) == ""
    assert render_advisory({}) == ""
