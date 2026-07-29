"""ReflectionWriter — LLM-driven lesson extraction after verifier-trip-recover.

Uses a stub LLMClient that replays scripted NormalizedResponse objects.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from agent.ledger import Attempt, PlanStep, VerdictSpec
from agent.llm import NormalizedResponse, NormalizedToolCall
from agent.subagents import reflection_writer


# ─────────────────────────── stub client ─────────────────────────────────────


@dataclass
class _StubClient:
    responses: list[NormalizedResponse]
    calls: list[dict[str, Any]] = field(default_factory=list)
    model: str = "stub"
    provider: str = "stub"

    def call(self, *, messages, system, tools, max_output_tokens):
        self.calls.append({
            "messages": list(messages),
            "system": system,
            "tools": [t["name"] for t in tools],
        })
        if not self.responses:
            raise RuntimeError("stub exhausted")
        return self.responses.pop(0)


def _resp(tool_calls):
    return NormalizedResponse(
        text="",
        tool_calls=tool_calls,
        stop_reason="tool_use",
        usage={"input_tokens": 0, "output_tokens": 0,
               "cache_hit_tokens": None, "cache_miss_tokens": None},
    )


def _tc(name, inp, tid="t1"):
    return NormalizedToolCall(id=tid, name=name, input=inp)


# ─────────────────────────── fixtures ────────────────────────────────────────


@pytest.fixture
def workdir(tmp_path: Path) -> Path:
    return tmp_path


@pytest.fixture
def step():
    return PlanStep(
        id="scf_1", kind="scf", desc="Run SCF",
        deps=[], expected_outputs={"scf_out": "scf_1/scf.out"},
    )


def _failed_then_passed_attempts(diagnosis: str = "scf.out missing JOB DONE.") -> list[Attempt]:
    return [
        Attempt(
            attempt_no=1, executor_session_id="s1",
            executor_submit={"task_outputs": {"scf_out": "scf_1/scf.out"}},
            verdict=VerdictSpec(**{"pass": False, "diagnosis": diagnosis}),
            started_at="2026-05-19T00:00:00+00:00",
            finished_at="2026-05-19T00:01:00+00:00",
        ),
        Attempt(
            attempt_no=2, executor_session_id="s1",
            executor_submit={"task_outputs": {"scf_out": "scf_1/scf.out"}},
            verdict=VerdictSpec(**{"pass": True, "diagnosis": ""}),
            started_at="2026-05-19T00:02:00+00:00",
            finished_at="2026-05-19T00:03:00+00:00",
        ),
    ]


def _scope():
    return {
        "task_kind": "vc_relax",
        "crystal_system": "cubic",
        "n_atoms_bucket": "≤2",
        "step_kind": "scf",
    }


# ─────────────────────────── happy path: submit_lesson ───────────────────────


def test_reflection_returns_lesson_dict(workdir, step):
    lesson_payload = {
        "trigger": "scf log missing JOB DONE.",
        "action":  "verify input file ATOMIC_POSITIONS card terminator",
        "rationale": "QE silently truncates when block is malformed",
    }
    stub = _StubClient(responses=[
        _resp([_tc("submit_lesson", lesson_payload)]),
    ])
    result = reflection_writer.run(
        step=step, attempts=_failed_then_passed_attempts(),
        scope=_scope(), workdir=workdir, client=stub,
    )
    assert result == lesson_payload


def test_reflection_returns_none_on_decline(workdir, step):
    stub = _StubClient(responses=[
        _resp([_tc("submit_lesson", {"decline": True, "reason": "too task-specific"})]),
    ])
    result = reflection_writer.run(
        step=step, attempts=_failed_then_passed_attempts(),
        scope=_scope(), workdir=workdir, client=stub,
    )
    assert result is None


# ─────────────────────────── bad payload retry ────────────────────────────────


def test_reflection_retries_missing_field_then_accepts(workdir, step):
    """First submit_lesson is missing rationale; subagent feeds the error
    back to LLM; second submit_lesson is complete."""
    stub = _StubClient(responses=[
        # First: missing rationale
        _resp([_tc("submit_lesson", {
            "trigger": "scf log missing JOB DONE.",
            "action":  "check ATOMIC_POSITIONS terminator",
            # rationale missing
        }, tid="t1")]),
        # Second: complete
        _resp([_tc("submit_lesson", {
            "trigger": "scf log missing JOB DONE.",
            "action":  "check ATOMIC_POSITIONS terminator",
            "rationale": "QE silently truncates malformed blocks",
        }, tid="t2")]),
    ])
    result = reflection_writer.run(
        step=step, attempts=_failed_then_passed_attempts(),
        scope=_scope(), workdir=workdir, client=stub,
    )
    assert result is not None
    assert result["rationale"].startswith("QE silently")

    # The retry tool message should contain the missing-fields feedback
    second_msgs = stub.calls[1]["messages"]
    tool_msg = [m for m in second_msgs if m.get("role") == "tool"][-1]
    assert "missing fields" in tool_msg["content"]


# ─────────────────────────── precondition: attempts shape ────────────────────


def test_reflection_returns_none_with_single_attempt(workdir, step):
    """Caller should gate on attempt_no>1; we also defensively reject."""
    attempts = [
        Attempt(
            attempt_no=1, executor_session_id="s1",
            verdict=VerdictSpec(**{"pass": True, "diagnosis": ""}),
            started_at="t", finished_at="t",
        ),
    ]
    stub = _StubClient(responses=[])
    result = reflection_writer.run(
        step=step, attempts=attempts, scope=_scope(),
        workdir=workdir, client=stub,
    )
    assert result is None


def test_reflection_returns_none_when_all_attempts_failed(workdir, step):
    """If the final attempt didn't pass, there's nothing to reflect on."""
    attempts = [
        Attempt(
            attempt_no=1, executor_session_id="s1",
            verdict=VerdictSpec(**{"pass": False, "diagnosis": "fail 1"}),
            started_at="t", finished_at="t",
        ),
        Attempt(
            attempt_no=2, executor_session_id="s1",
            verdict=VerdictSpec(**{"pass": False, "diagnosis": "fail 2"}),
            started_at="t", finished_at="t",
        ),
    ]
    stub = _StubClient(responses=[])
    result = reflection_writer.run(
        step=step, attempts=attempts, scope=_scope(),
        workdir=workdir, client=stub,
    )
    assert result is None


# ─────────────────────────── prompt content ───────────────────────────────────


def test_reflection_prompt_includes_diagnosis(workdir, step):
    stub = _StubClient(responses=[
        _resp([_tc("submit_lesson", {"decline": True, "reason": "x"})]),
    ])
    reflection_writer.run(
        step=step,
        attempts=_failed_then_passed_attempts(diagnosis="charge density dir is empty"),
        scope=_scope(), workdir=workdir, client=stub,
    )
    first_user = stub.calls[0]["messages"][0]["content"]
    assert "charge density dir is empty" in first_user
    assert "scf_1" in first_user


def test_reflection_no_tool_call_returns_none(workdir, step):
    """If LLM produces only text (no tool call), there's nothing to record."""
    stub = _StubClient(responses=[_resp([])])
    result = reflection_writer.run(
        step=step, attempts=_failed_then_passed_attempts(),
        scope=_scope(), workdir=workdir, client=stub,
    )
    assert result is None


# ─────────────────────────── diff extraction ──────────────────────────────────


def test_reflection_diff_section_with_overlapping_writes(workdir, step):
    """When a per-step transcript records write_file calls on the same path
    in both attempts, the diff block surfaces in the user prompt."""
    # Seed a transcript that resembles a real per-step transcript.json.
    transcript_dir = workdir / step.id
    transcript_dir.mkdir(parents=True, exist_ok=True)
    transcript = [
        # attempt 1: writes scf.in with loose conv_thr
        {"content": [
            {"type": "tool_use", "id": "x", "name": "write_file",
             "input": {"path": "scf_1/scf.in",
                       "content": "&control\ncalc='scf'\nconv_thr=1e-6\n/\n"}},
        ]},
        # VERIFIER FEEDBACK marker turn (split point)
        {"content": [{"type": "text",
                      "text": "## VERIFIER FEEDBACK (v4.3 retry)\nfail..."}]},
        # attempt 2: writes scf.in with tighter conv_thr
        {"content": [
            {"type": "tool_use", "id": "y", "name": "write_file",
             "input": {"path": "scf_1/scf.in",
                       "content": "&control\ncalc='scf'\nconv_thr=1e-9\n/\n"}},
        ]},
    ]
    (transcript_dir / "transcript.json").write_text(
        __import__("json").dumps(transcript), encoding="utf-8",
    )

    stub = _StubClient(responses=[
        _resp([_tc("submit_lesson", {"decline": True, "reason": "x"})]),
    ])
    reflection_writer.run(
        step=step, attempts=_failed_then_passed_attempts(),
        scope=_scope(), workdir=workdir, client=stub,
    )
    user_content = stub.calls[0]["messages"][0]["content"]
    # The diff should mention the path and the differing line
    assert "scf_1/scf.in" in user_content
    assert "conv_thr=1e-9" in user_content or "conv_thr=1e-6" in user_content
