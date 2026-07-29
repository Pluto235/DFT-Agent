"""FIX-39 (S-3): reap orphaned QE processes after a wallclock timeout.

When a pw.x run is killed at the timeout, mpirun may have `setsid`'d pw.x into
its own session, escaping the killpg() of the bash child's process group — so
pw.x orphans onto init and keeps burning cores. A later restart then contends
with the orphan (the Fe3O4/LaMnO3 M3_T2 death-spiral: 6h wall for 1.8h of CPU).

The reap is scoped to THIS run's workdir (every run has a unique workdir under
runs_local/<ts>_<task>/), so a concurrent run's pw.x is never touched.
"""
from __future__ import annotations
from pathlib import Path

from agent.tools.bash import _proc_matches_workdir


def test_pwx_under_workdir_matches(tmp_path):
    """A pw.x process whose cwd is the run workdir is a reap candidate."""
    assert _proc_matches_workdir(tmp_path, "pw.x -in scf.in", tmp_path) is True


def test_pwx_in_subdir_of_workdir_matches(tmp_path):
    """The probe runs pw.x in workdir/magnetic_probe/ — still in scope."""
    sub = tmp_path / "magnetic_probe"
    sub.mkdir()
    assert _proc_matches_workdir(sub, "mpirun -np 4 pw.x -in probe_afm.in", tmp_path) is True


def test_pwx_outside_workdir_not_matched(tmp_path):
    """A pw.x from a DIFFERENT run (different workdir) must never be reaped."""
    other = tmp_path.parent / "other_run_workdir"
    other.mkdir(exist_ok=True)
    assert _proc_matches_workdir(other, "pw.x -in scf.in", tmp_path) is False


def test_non_qe_process_under_workdir_not_matched(tmp_path):
    """A plain shell process in the workdir is not a QE process — leave it."""
    assert _proc_matches_workdir(tmp_path, "bash -c 'echo hi'", tmp_path) is False
    assert _proc_matches_workdir(tmp_path, "python3 analyze.py", tmp_path) is False


def test_none_cwd_not_matched(tmp_path):
    """Unreadable /proc/<pid>/cwd (process already gone) → no match."""
    assert _proc_matches_workdir(None, "pw.x -in scf.in", tmp_path) is False


def test_other_qe_executables_matched(tmp_path):
    """dos.x / bands.x / projwfc.x orphans are reaped too."""
    for marker in ("dos.x -in dos.in", "bands.x -in bands.in", "projwfc.x -in pdos.in"):
        assert _proc_matches_workdir(tmp_path, marker, tmp_path) is True
