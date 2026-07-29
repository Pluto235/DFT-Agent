"""WorkdirStager v4.3 (post-cwd-fix) — subdir setup + post-execution verify.

No symlinks. No file movement. Executor runs from main workdir and writes
directly to workdir-relative paths; stager just creates the per-step subdir
and post-execution verifies files exist.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from agent.ledger import PlanStep
from agent.workdir_stager import WorkdirStager, list_stale_steps


@pytest.fixture
def workdir(tmp_path):
    return tmp_path


def _step(sid: str, kind: str, outputs: dict[str, str], deps=None) -> PlanStep:
    return PlanStep(
        id=sid,
        kind=kind,
        desc=f"{kind} step",
        deps=deps or [],
        expected_outputs=outputs,
    )


# ─────────────────────────── stage ────────────────────────────────────────────

def test_stage_creates_subworkdir(workdir):
    stager = WorkdirStager(workdir)
    step = _step("scf_1", "scf", {"scf_out": "scf_1/scf.out"})
    staged = stager.stage(step, {"scf_1": step})
    assert staged.sub_workdir == (workdir / "scf_1").resolve()
    assert staged.sub_workdir.is_dir()
    assert staged.upstream_paths == {}


def test_stage_records_upstream_paths_without_symlinks(workdir):
    """v4.3: upstream artifacts stay at their main-workdir-relative paths.
    Stager only records WHICH paths the executor should know about."""
    stager = WorkdirStager(workdir)
    # Pretend scf_1 already ran (file exists at the planner-declared path).
    (workdir / "scf_1").mkdir()
    (workdir / "scf_1" / "scf.out").write_text("JOB DONE.\n" + "x" * 2000)
    scf = _step("scf_1", "scf", {"scf_out": "scf_1/scf.out"})
    bands = _step("bands_1", "bands", {"bands_out": "bands_1/bands.out"}, deps=["scf_1"])
    staged = stager.stage(bands, {"scf_1": scf, "bands_1": bands})
    assert staged.upstream_paths == {"scf_1": ["scf_1/scf.out"]}
    # NO symlink created inside bands_1/
    assert not (workdir / "bands_1" / "scf_1" / "scf.out").exists()


def test_stage_raises_on_missing_upstream(workdir):
    stager = WorkdirStager(workdir)
    scf = _step("scf_1", "scf", {"scf_out": "scf_1/scf.out"})
    bands = _step("bands_1", "bands", {"bands_out": "bands_1/bands.out"}, deps=["scf_1"])
    with pytest.raises(RuntimeError, match="upstream scf_1 artifact missing"):
        stager.stage(bands, {"scf_1": scf, "bands_1": bands})


def test_stage_raises_on_unknown_dep_id(workdir):
    stager = WorkdirStager(workdir)
    step = _step("bands_1", "bands", {"bands_out": "bands_1/bands.out"}, deps=["ghost"])
    with pytest.raises(RuntimeError, match="dep 'ghost' not in plan"):
        stager.stage(step, {"bands_1": step})


# ─────────────────────────── promote (verify-only) ────────────────────────────

def test_promote_verifies_files_in_place(workdir):
    """v4.3: promote no longer moves files; it just verifies they exist at
    the planner-declared workdir-relative path where the executor wrote them."""
    stager = WorkdirStager(workdir)
    step = _step("scf_1", "scf", {"scf_out": "scf_1/scf.out"})
    stager.stage(step, {"scf_1": step})
    # Executor writes directly at the planner-declared path (cwd = main workdir)
    (workdir / "scf_1" / "scf.out").write_text("JOB DONE.\n" + "y" * 2000)
    verified = stager.promote(step)
    assert verified == {"scf_out": "scf_1/scf.out"}
    # File stays where the executor wrote it; no moving/linking happened.
    assert (workdir / "scf_1" / "scf.out").exists()


def test_promote_raises_when_output_missing(workdir):
    stager = WorkdirStager(workdir)
    step = _step("scf_1", "scf", {"scf_out": "scf_1/scf.out"})
    stager.stage(step, {"scf_1": step})
    with pytest.raises(RuntimeError, match="not produced by executor"):
        stager.promote(step)


def test_planstep_rejects_path_escaping_workdir(workdir):
    """v4.3 PATH CONVENTION enforces `<step.id>/` prefix at PlanStep construction,
    which incidentally rejects path escapes via `..`. Promote no longer needs
    its own escape check because no malformed path can reach it."""
    from pydantic import ValidationError
    with pytest.raises(ValidationError, match="must start with"):
        PlanStep(
            id="evil_1", kind="scf", desc="evil", deps=[],
            expected_outputs={"scf_out": "../../etc/passwd"},
        )


# ─────────────────────────── stale subdir helper ──────────────────────────────

def test_list_stale_steps(workdir):
    (workdir / "scf_1").mkdir()
    (workdir / "bands_1").mkdir()
    (workdir / "old_step").mkdir()
    (workdir / "out").mkdir()                  # well-known QE dir; should NOT flag
    (workdir / "pseudos").mkdir()              # well-known QE dir; should NOT flag
    (workdir / ".hidden").mkdir()
    stale = list_stale_steps(workdir, ["scf_1", "bands_1"])
    assert stale == ["old_step"]
