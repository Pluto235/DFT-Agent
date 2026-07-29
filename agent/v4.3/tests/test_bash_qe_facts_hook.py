"""BashTool post-pw.x hook (PR-B) — qe_facts.jsonl + tool_result advisory.

The hook fires AFTER the subprocess returns. We can't run real pw.x in a
unit test, so each scenario:
  1. Pre-creates the redirect target via a `cat > … <<EOF … EOF` heredoc
     inside the bash command itself.
  2. Includes the literal `pw.x` token (in a comment is fine) so the
     command-text detector trips.
The hook then parses the heredoc-written file as if pw.x had produced it.
"""
from __future__ import annotations

import json
from pathlib import Path

from agent.tools.bash import BashTool


_CONVERGED_OUT = """\
     Program PWSCF v.7.4 starts on  1May2026
     lattice parameter (alat)  =      10.2630  a.u.
     unit-cell volume          =     270.1100  (a.u.)^3
     number of Kohn-Sham states=            8
     iteration #  1     ecut=    60.00 Ry
     iteration #  2     ecut=    60.00 Ry
     iteration #  3     ecut=    60.00 Ry
     convergence has been achieved in   3 iterations
!    total energy              =      -8.74519230 Ry

     Forces acting on atoms (cartesian axes, Ry/au):

     atom    1 type  1   force =     0.00010000    0.00000000    0.00000000
     Total force =     0.000100     Total SCF correction =     0.000000

     total   stress  (Ry/bohr**3)                   (kbar)     P=        0.00
     0.00010000   0.00000000   0.00000000      14.71      0.00      0.00
     0.00000000   0.00010000   0.00000000       0.00     14.71      0.00
     0.00000000   0.00000000   0.00010000       0.00      0.00     14.71

   This run was terminated on:  1May2026

=----=
   JOB DONE.
=----=
"""


def _pw_heredoc(workdir_rel_out: str, fixture: str) -> str:
    """Construct a bash command that (a) writes the fixture to the
    redirect target via heredoc, (b) carries a `pw.x` token so the
    BashTool post-hook detects it as a pw.x invocation."""
    parent = str(Path(workdir_rel_out).parent)
    mkdir = f"mkdir -p {parent} && " if parent and parent != "." else ""
    return (
        f"{mkdir}cat > {workdir_rel_out} <<'PWFIXTUREEOF'\n"
        f"{fixture}"
        f"PWFIXTUREEOF\n"
        f"# mpirun -np 4 pw.x -i scf.in > {workdir_rel_out}"
    )


def test_post_hook_writes_jsonl_and_appends_advisory(tmp_path):
    tool = BashTool(workdir=tmp_path)
    cmd = _pw_heredoc("scf_1/scf.out", _CONVERGED_OUT)
    r = tool.run(cmd)
    assert r["exit_code"] == 0

    # 1. JSONL row exists at workdir/qe_facts.jsonl.
    jsonl = tmp_path / "qe_facts.jsonl"
    assert jsonl.is_file(), "qe_facts.jsonl was not created"
    rows = [json.loads(line) for line in jsonl.read_text().splitlines() if line.strip()]
    assert len(rows) == 1
    row = rows[0]
    assert row["step_id"] == "scf_1"
    assert row["output_path"] == "scf_1/scf.out"
    assert row["facts"]["converged"] is True
    assert row["facts"]["job_done"] is True
    assert row["facts"]["n_scf_iterations"] == 3

    # 2. Advisory block appears in stdout (visible to the LLM).
    assert "--- qe_facts ---" in r["stdout"]
    assert "JOB_DONE" in r["stdout"]
    assert "converged" in r["stdout"]


def test_post_hook_does_not_fire_for_non_pw_x_command(tmp_path):
    tool = BashTool(workdir=tmp_path)
    r = tool.run("echo 'just a plain bash command' > /dev/null")
    assert r["exit_code"] == 0
    # No facts written, no advisory.
    assert not (tmp_path / "qe_facts.jsonl").exists()
    assert "qe_facts" not in r["stdout"]


def test_post_hook_silent_when_output_file_missing(tmp_path):
    """pw.x in command but no `> file` redirect → no parse, no crash."""
    tool = BashTool(workdir=tmp_path)
    # Bare invocation without redirect — detector picks up pw.x, but
    # detect_output_path returns None. Hook should be a quiet no-op.
    r = tool.run("echo 'pretending'; # mpirun -np 4 pw.x without redirect")
    assert r["exit_code"] == 0
    assert not (tmp_path / "qe_facts.jsonl").exists()
    assert "qe_facts" not in r["stdout"]


def test_post_hook_silent_when_output_file_unreadable(tmp_path):
    """pw.x command + `>` target points at a non-existent path because the
    redirect was never executed (e.g., the command failed before pw.x ran).
    The hook should write a parse_error row, no advisory, no crash."""
    tool = BashTool(workdir=tmp_path)
    # `false` before the pw.x — bash short-circuits, no redirect file made.
    cmd = "false ; true # mpirun -np 4 pw.x -i x.in > does/not/exist/scf.out"
    r = tool.run(cmd)
    # Exit code is 0 because `true` is last.
    assert r["exit_code"] == 0
    # JSONL row IS written (with parse_error), advisory is empty.
    jsonl = tmp_path / "qe_facts.jsonl"
    assert jsonl.is_file()
    rows = [json.loads(line) for line in jsonl.read_text().splitlines() if line.strip()]
    assert len(rows) == 1
    assert "parse_error" in rows[0]
    assert "qe_facts" not in r["stdout"]


def test_post_hook_records_not_converged_run(tmp_path):
    not_converged = """\
     Program PWSCF v.7.4 starts
     iteration #  1     ecut=    60.00 Ry
     iteration #  2     ecut=    60.00 Ry
     iteration #  3     ecut=    60.00 Ry
     convergence NOT achieved after  3 iterations: stopping
"""
    tool = BashTool(workdir=tmp_path)
    cmd = _pw_heredoc("scf_1/scf.out", not_converged)
    r = tool.run(cmd)
    assert r["exit_code"] == 0

    rows = [json.loads(line) for line in
            (tmp_path / "qe_facts.jsonl").read_text().splitlines() if line.strip()]
    assert rows[0]["facts"]["converged"] is False
    assert rows[0]["facts"]["job_done"] is False
    assert rows[0]["facts"]["error_signature"] is not None
    assert "NO_JOB_DONE" in r["stdout"]


def test_post_hook_appends_multiple_invocations(tmp_path):
    """Multi-step task: scf followed by bands. Two pw.x calls → 2 JSONL rows."""
    tool = BashTool(workdir=tmp_path)
    tool.run(_pw_heredoc("scf_1/scf.out", _CONVERGED_OUT))
    tool.run(_pw_heredoc("bands_1/bands.out", _CONVERGED_OUT))
    rows = [json.loads(line) for line in
            (tmp_path / "qe_facts.jsonl").read_text().splitlines() if line.strip()]
    step_ids = {r["step_id"] for r in rows}
    assert step_ids == {"scf_1", "bands_1"}


def test_post_hook_failure_does_not_break_bash(tmp_path, monkeypatch):
    """If qe_output_parse internally raises, bash must still return cleanly."""
    import agent.tools.qe_output_parse as qe_op

    def _boom(*a, **kw):
        raise RuntimeError("simulated internal crash")
    monkeypatch.setattr(qe_op, "extract_facts", _boom)

    tool = BashTool(workdir=tmp_path)
    cmd = _pw_heredoc("scf_1/scf.out", _CONVERGED_OUT)
    r = tool.run(cmd)
    # Bash still returns exit_code=0, raw stdout intact, no advisory tail.
    assert r["exit_code"] == 0
    assert "qe_facts" not in r["stdout"]
