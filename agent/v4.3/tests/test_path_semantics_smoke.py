"""Path semantics smoke — verifies the v4.3 cwd-fix bugs are gone.

These were the architectural bugs from the post-refactor audit:

Bug 1: pseudo_dir='./pseudos/' would not resolve from sub_workdir cwd.
Bug 2: expected_outputs at 'scf_1/scf.out' got double-prefixed.
Bug 3: bash.log landed in sub_workdir instead of main workdir.
Bug 4: base prompt vs framing path semantics contradicted each other.
Bug 5: cd outside sub_workdir was blocked, locking executor away from pseudos.

After fix: executor BashTool cwd = MAIN workdir, all files written under
<step_id>/ prefix, bash.log → main_workdir/bash.log.

These tests drive the ExecutorAgent with a stub LLM that emits real bash
commands, NOT mocking the BashTool. They exercise the actual file system
and assert post-conditions.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from agent.ledger import PlanStep
from agent.llm import NormalizedResponse, NormalizedToolCall
from agent.subagents.executor import ExecutorAgent


@dataclass
class _StubClient:
    responses: list[NormalizedResponse]
    calls: list[dict] = field(default_factory=list)
    model: str = "stub"
    provider: str = "stub"

    def call(self, *, messages, system, tools, max_output_tokens):
        self.calls.append({"messages_count": len(messages)})
        if not self.responses:
            raise RuntimeError("stub exhausted")
        return self.responses.pop(0)


def _r(tcs: list[NormalizedToolCall]) -> NormalizedResponse:
    return NormalizedResponse(
        text="", tool_calls=tcs, stop_reason="tool_use",
        usage={"input_tokens": 0, "output_tokens": 0,
               "cache_hit_tokens": None, "cache_miss_tokens": None},
    )


@pytest.fixture
def workdir(tmp_path):
    """Realistic workdir layout: pseudos pre-staged at root."""
    (tmp_path / "pseudos").mkdir()
    (tmp_path / "pseudos" / "Si.upf").write_text("fake pseudopotential")
    return tmp_path


# ─────────────────────────── Bug 3: bash.log location ────────────────────────

def test_bash_log_lands_in_main_workdir_not_sub_workdir(workdir):
    """After cwd fix: every bash call from executor logs to main_workdir/bash.log,
    not to workdir/<step_id>/bash.log. Bench audit reads main workdir/bash.log."""
    step = PlanStep(
        id="scf_1", kind="scf", desc="echo something",
        deps=[], expected_outputs={"scf_out": "scf_1/scf.out"},
    )
    (workdir / "scf_1").mkdir()
    stub = _StubClient(responses=[
        _r([NormalizedToolCall(id="t1", name="bash",
                               input={"cmd": "echo 'JOB DONE.' > scf_1/scf.out"})]),
        _r([NormalizedToolCall(id="t2", name="submit",
                               input={"task_outputs": {"scf_out": "scf_1/scf.out"}})]),
    ])
    ex = ExecutorAgent(
        step=step, main_workdir=workdir,
        base_system_prompt="(base prompt for QE)",
        upstream={}, client=stub, max_turns=4,
    )
    ex.run("dummy task prompt")
    # bash.log MUST be in main workdir (where bench audit looks)
    assert (workdir / "bash.log").exists(), \
        f"bash.log not in main workdir: {list(workdir.iterdir())}"
    # And NOT in the step subdir
    assert not (workdir / "scf_1" / "bash.log").exists()
    # And the echoed file landed at the planner-declared path
    assert (workdir / "scf_1" / "scf.out").read_text().strip() == "JOB DONE."


# ─────────────────────────── Bug 1+2: path semantics under cwd ────────────────

def test_pseudo_dir_relative_resolves_from_main_workdir(workdir):
    """Executor writes a fake `cat ./pseudos/Si.upf` — works because cwd = main workdir.
    Under the old (broken) v4.3 cwd=sub_workdir, this would fail with file-not-found."""
    step = PlanStep(
        id="scf_1", kind="scf", desc="cat pseudo",
        deps=[], expected_outputs={"scf_out": "scf_1/scf.out"},
    )
    (workdir / "scf_1").mkdir()
    stub = _StubClient(responses=[
        # The literal command `cat ./pseudos/Si.upf` — would FAIL on the old
        # sub_workdir cwd because ./pseudos/ resolves to workdir/scf_1/pseudos/
        _r([NormalizedToolCall(id="t1", name="bash",
                               input={"cmd": "cat ./pseudos/Si.upf > scf_1/scf.out && echo 'JOB DONE.' >> scf_1/scf.out"})]),
        _r([NormalizedToolCall(id="t2", name="submit",
                               input={"task_outputs": {"scf_out": "scf_1/scf.out"}})]),
    ])
    ex = ExecutorAgent(
        step=step, main_workdir=workdir,
        base_system_prompt="(base)", upstream={}, client=stub, max_turns=4,
    )
    ex.run("dummy")
    body = (workdir / "scf_1" / "scf.out").read_text()
    assert "fake pseudopotential" in body
    assert "JOB DONE." in body


def test_expected_output_no_double_prefix(workdir):
    """LLM writes `scf_1/scf.out` while in main_workdir cwd; file ends up at
    workdir/scf_1/scf.out (the planner-declared path), NOT workdir/scf_1/scf_1/scf.out."""
    step = PlanStep(
        id="scf_1", kind="scf", desc="write scf.out",
        deps=[], expected_outputs={"scf_out": "scf_1/scf.out"},
    )
    (workdir / "scf_1").mkdir()
    stub = _StubClient(responses=[
        _r([NormalizedToolCall(id="t1", name="write_file",
                               input={"path": "scf_1/scf.out",
                                      "content": "JOB DONE.\n" + "x" * 2000})]),
        _r([NormalizedToolCall(id="t2", name="submit",
                               input={"task_outputs": {"scf_out": "scf_1/scf.out"}})]),
    ])
    ex = ExecutorAgent(
        step=step, main_workdir=workdir,
        base_system_prompt="(base)", upstream={}, client=stub, max_turns=4,
    )
    ex.run("dummy")
    assert (workdir / "scf_1" / "scf.out").exists()
    # NOT here:
    assert not (workdir / "scf_1" / "scf_1" / "scf.out").exists()
    # And promote should be able to verify this directly:
    from agent.workdir_stager import WorkdirStager
    verified = WorkdirStager(workdir).promote(step)
    assert verified == {"scf_out": "scf_1/scf.out"}


# ─────────────────────────── Bug 4: framing matches base prompt cwd ───────────

def test_framing_mentions_main_workdir_as_cwd(workdir):
    """The system prompt should tell the executor cwd = main workdir."""
    step = PlanStep(
        id="scf_1", kind="scf", desc="x",
        deps=[], expected_outputs={"scf_out": "scf_1/scf.out"},
    )
    stub = _StubClient(responses=[])
    ex = ExecutorAgent(
        step=step, main_workdir=workdir,
        base_system_prompt="BASE", upstream={}, client=stub,
    )
    assert str(workdir) in ex.system_prompt
    assert "MAIN workdir" in ex.system_prompt
    # And NOT the old sub_workdir-as-cwd framing
    assert "cwd there" not in ex.system_prompt
