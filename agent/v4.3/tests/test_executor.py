"""ExecutorAgent — system prompt composition + tool filtering + resume.

Uses a stub LLMClient that replays scripted responses; no real API calls.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from agent.ledger import PlanStep
from agent.llm import NormalizedResponse, NormalizedToolCall
from agent.subagents.executor import ExecutorAgent


# ─────────────────────────── stub LLMClient ───────────────────────────────────

@dataclass
class _StubClient:
    """Replays a list of NormalizedResponse objects in order."""
    responses: list[NormalizedResponse]
    calls: list[dict[str, Any]] = field(default_factory=list)
    model: str = "stub-model"
    provider: str = "stub"

    def call(self, *, messages, system, tools, max_output_tokens) -> NormalizedResponse:
        # Record each call so tests can inspect prompt/tool subsets.
        self.calls.append({
            "messages": list(messages),
            "system": system,
            "tools": [t["name"] for t in tools],
            "max_output_tokens": max_output_tokens,
        })
        if not self.responses:
            raise RuntimeError("stub exhausted")
        return self.responses.pop(0)


def _submit_resp(payload: dict) -> NormalizedResponse:
    return NormalizedResponse(
        text="",
        tool_calls=[NormalizedToolCall(id="t1", name="submit", input=payload)],
        stop_reason="tool_use",
        usage={"input_tokens": 0, "output_tokens": 0,
               "cache_hit_tokens": None, "cache_miss_tokens": None},
    )


@pytest.fixture
def workdir(tmp_path):
    return tmp_path


@pytest.fixture
def step(workdir):
    return PlanStep(
        id="scf_1", kind="scf", desc="Run SCF",
        deps=[],
        expected_outputs={"scf_out": "scf_1/scf.out"},
    )


# ─────────────────────────── system prompt composition ────────────────────────

def test_executor_system_prompt_includes_kind_addon(workdir, step):
    sub = workdir / step.id
    sub.mkdir()
    stub = _StubClient(responses=[_submit_resp({"task_outputs": {"scf_out": "scf_1/scf.out"}})])
    ex = ExecutorAgent(
        step=step, main_workdir=workdir,
        base_system_prompt="BASE_PROMPT_TOKEN",
        upstream={},
        client=stub,
    )
    assert "BASE_PROMPT_TOKEN" in ex.system_prompt
    assert "EXECUTOR SCOPE" in ex.system_prompt
    assert "kind=scf" in ex.system_prompt or "scf_1" in ex.system_prompt


def test_executor_filters_tools_by_allowlist(workdir, step):
    """scf kind allowlist = bash/write_file/read_file/submit + read_experience
    (v4.3: the latter is a no-op tool, declared so providers don't reject the
    pre-seeded synthetic tool_use in conversation history)."""
    sub = workdir / step.id
    sub.mkdir()
    stub = _StubClient(responses=[_submit_resp({"task_outputs": {"scf_out": "scf_1/scf.out"}})])
    ex = ExecutorAgent(
        step=step, main_workdir=workdir,
        base_system_prompt="base",
        upstream={},
        client=stub,
    )
    assert set(t["name"] for t in ex.tools) == {
        "bash", "write_file", "read_file", "submit", "read_experience",
    }


# ─────────────────────────── run / resume ─────────────────────────────────────

def test_run_returns_submit_payload(workdir, step):
    sub = workdir / step.id
    sub.mkdir()
    stub = _StubClient(responses=[
        _submit_resp({"task_outputs": {"scf_out": "scf_1/scf.out"}}),
    ])
    ex = ExecutorAgent(
        step=step, main_workdir=workdir,
        base_system_prompt="base",
        upstream={},
        client=stub,
    )
    result = ex.run("Initial task prompt.")
    assert result.submit_payload == {"task_outputs": {"scf_out": "scf_1/scf.out"}}
    assert result.declared_incomplete is False
    assert result.session_id  # non-empty
    # The stub was called once; first user message is the initial prompt
    assert stub.calls[0]["messages"][0] == {"role": "user", "content": "Initial task prompt."}


def test_run_cannot_be_called_twice(workdir, step):
    sub = workdir / step.id
    sub.mkdir()
    stub = _StubClient(responses=[
        _submit_resp({"task_outputs": {"scf_out": "scf_1/scf.out"}}),
    ])
    ex = ExecutorAgent(step=step, main_workdir=workdir,
                      base_system_prompt="base", upstream={}, client=stub)
    ex.run("first")
    with pytest.raises(RuntimeError, match="already called"):
        ex.run("second")


def test_resume_appends_diagnosis_as_user_msg(workdir, step):
    sub = workdir / step.id
    sub.mkdir()
    stub = _StubClient(responses=[
        # initial attempt
        _submit_resp({"task_outputs": {"scf_out": "scf_1/scf.out"}}),
        # resume attempt
        _submit_resp({"task_outputs": {"scf_out": "scf_1/scf.out"}, "notes": "fixed"}),
    ])
    ex = ExecutorAgent(step=step, main_workdir=workdir,
                      base_system_prompt="base", upstream={}, client=stub)
    ex.run("Initial task prompt.")

    result2 = ex.resume("scf.out is missing JOB DONE; check k-point grid")
    assert result2.submit_payload.get("notes") == "fixed"

    # The second call's messages list should contain:
    #   - the original "Initial task prompt." user message
    #   - the assistant submit + tool_result from the first attempt
    #   - the new user message with the diagnosis
    second_call_msgs = stub.calls[1]["messages"]
    assert second_call_msgs[0]["content"] == "Initial task prompt."
    # Last message is the new diagnosis user msg
    last = second_call_msgs[-1]
    assert last["role"] == "user"
    assert "VERIFIER FEEDBACK" in last["content"]
    assert "missing JOB DONE" in last["content"]
    # session_id persists across resume
    assert result2.session_id == ex.session_id


def test_resume_before_run_raises(workdir, step):
    sub = workdir / step.id
    sub.mkdir()
    stub = _StubClient(responses=[])
    ex = ExecutorAgent(step=step, main_workdir=workdir,
                      base_system_prompt="base", upstream={}, client=stub)
    with pytest.raises(RuntimeError, match="resume called before run"):
        ex.resume("diagnosis")


def test_executor_injects_experience_when_scope_set(workdir, step):
    """When scope+repo_root are provided, run() prepends a synthetic
    read_experience tool_use + tool_result pair before the user prompt."""
    sub = workdir / step.id
    sub.mkdir()

    # Seed one matching lesson into the experience library at repo_root.
    from agent import experience as exp_mod
    scope = {
        "task_kind": "vc_relax",
        "crystal_system": "cubic",
        "n_atoms_bucket": "≤2",
        "step_kind": "scf",
    }
    exp_mod.append_lesson("scf", {
        "trigger": "scf stalls on dense k-mesh",
        "action":  "raise ecutwfc and tighten conv_thr",
        "rationale": "denser k-mesh demands denser plane-wave basis",
    }, scope, workdir)   # repo_root = workdir for this test

    stub = _StubClient(responses=[
        _submit_resp({"task_outputs": {"scf_out": "scf_1/scf.out"}}),
    ])
    ex = ExecutorAgent(
        step=step, main_workdir=workdir,
        base_system_prompt="base", upstream={},
        client=stub, scope=scope, repo_root=workdir,
    )
    ex.run("Initial task prompt.")

    # The conversation sent to the LLM must start with:
    #   1. assistant message with a read_experience tool_call
    #   2. tool message with the lesson payload
    #   3. user message with the original task prompt
    msgs = stub.calls[0]["messages"]
    assert msgs[0]["role"] == "assistant"
    assert msgs[0]["tool_calls"][0]["function"]["name"] == "read_experience"
    assert msgs[1]["role"] == "tool"
    tool_payload = json.loads(msgs[1]["content"])
    assert tool_payload["n"] == 1
    assert tool_payload["lessons"][0]["lesson"]["trigger"].startswith("scf stalls")
    assert msgs[2] == {"role": "user", "content": "Initial task prompt."}

    # An inject event was logged to workdir/experience.log
    log = (workdir / "experience.log").read_text(encoding="utf-8").splitlines()
    assert any("inject" in line and "scf_1" in line for line in log)


def test_executor_injects_empty_lessons_when_library_empty(workdir, step):
    """No prior lessons → injection still happens but with empty list.
    This is the cold-start case; the LLM still sees that it consulted
    its memory and found nothing."""
    sub = workdir / step.id
    sub.mkdir()
    scope = {
        "task_kind": "vc_relax",
        "crystal_system": "cubic",
        "n_atoms_bucket": "≤2",
        "step_kind": "scf",
    }
    stub = _StubClient(responses=[
        _submit_resp({"task_outputs": {"scf_out": "scf_1/scf.out"}}),
    ])
    ex = ExecutorAgent(
        step=step, main_workdir=workdir,
        base_system_prompt="base", upstream={},
        client=stub, scope=scope, repo_root=workdir,
    )
    ex.run("Initial task prompt.")

    msgs = stub.calls[0]["messages"]
    assert msgs[0]["role"] == "assistant"
    payload = json.loads(msgs[1]["content"])
    assert payload["n"] == 0
    assert payload["lessons"] == []


def test_executor_does_not_inject_when_scope_none(workdir, step):
    """Backward-compat: callers that don't pass scope get no injection."""
    sub = workdir / step.id
    sub.mkdir()
    stub = _StubClient(responses=[
        _submit_resp({"task_outputs": {"scf_out": "scf_1/scf.out"}}),
    ])
    ex = ExecutorAgent(
        step=step, main_workdir=workdir,
        base_system_prompt="base", upstream={}, client=stub,
    )
    ex.run("Initial task prompt.")

    msgs = stub.calls[0]["messages"]
    # First message is the user prompt — no fake pair.
    assert msgs[0] == {"role": "user", "content": "Initial task prompt."}


def test_executor_upstream_paths_appear_in_prompt(workdir):
    """When step has deps and stager linked them, addon should list them."""
    step = PlanStep(
        id="bands_1", kind="scf",          # use scf kind so addon renders deps
        desc="Bands SCF", deps=["scf_1"],
        expected_outputs={"scf_out": "bands_1/scf.out"},
    )
    sub = workdir / step.id
    sub.mkdir()
    stub = _StubClient(responses=[
        _submit_resp({"task_outputs": {"scf_out": "bands_1/scf.out"}}),
    ])
    ex = ExecutorAgent(
        step=step, main_workdir=workdir,
        base_system_prompt="base",
        upstream={"scf_1": ["scf_1/scf.out"]},
        client=stub,
    )
    assert "scf_1" in ex.system_prompt
    assert "scf.out" in ex.system_prompt


# ─────────────────────── advisory_block (PR-A) ────────────────────────────────


_ADVISORY = (
    "===== MATERIAL ADVISORIES =====\n"
    "(human-authored playbooks matched by structural classification)\n\n"
    "[alkali-metal-advisor] alkali playbook\n"
    "Use smearing on metals.\n\n"
    "===== END MATERIAL ADVISORIES =====\n"
)


def test_executor_appends_advisory_block_to_first_user_prompt(workdir, step):
    """When advisory_block is non-empty, run() appends it to user_prompt."""
    sub = workdir / step.id
    sub.mkdir()
    stub = _StubClient(responses=[
        _submit_resp({"task_outputs": {"scf_out": "scf_1/scf.out"}}),
    ])
    ex = ExecutorAgent(
        step=step, main_workdir=workdir,
        base_system_prompt="base", upstream={}, client=stub,
        advisory_block=_ADVISORY,
    )
    ex.run("Initial task prompt.")

    msgs = stub.calls[0]["messages"]
    # No experience scope set → user prompt is first message.
    assert msgs[0]["role"] == "user"
    content = msgs[0]["content"]
    assert content.startswith("Initial task prompt.")
    assert "===== MATERIAL ADVISORIES =====" in content
    assert "[alkali-metal-advisor]" in content
    assert "===== END MATERIAL ADVISORIES =====" in content


def test_executor_empty_advisory_block_is_noop(workdir, step):
    sub = workdir / step.id
    sub.mkdir()
    stub = _StubClient(responses=[
        _submit_resp({"task_outputs": {"scf_out": "scf_1/scf.out"}}),
    ])
    ex = ExecutorAgent(
        step=step, main_workdir=workdir,
        base_system_prompt="base", upstream={}, client=stub,
        advisory_block="",
    )
    ex.run("Initial task prompt.")
    msgs = stub.calls[0]["messages"]
    assert msgs[0] == {"role": "user", "content": "Initial task prompt."}


def test_executor_advisory_block_after_experience_injection(workdir, step):
    """When both scope and advisory_block are set, the synthetic
    read_experience tool pair comes FIRST (so it caches with the rest of
    the messages prefix), and the advisory_block lives inside the user
    prompt content — last in the prefix as designed."""
    sub = workdir / step.id
    sub.mkdir()
    scope = {
        "task_kind": "vc_relax", "crystal_system": "cubic",
        "n_atoms_bucket": "≤2", "step_kind": "scf",
    }
    stub = _StubClient(responses=[
        _submit_resp({"task_outputs": {"scf_out": "scf_1/scf.out"}}),
    ])
    ex = ExecutorAgent(
        step=step, main_workdir=workdir,
        base_system_prompt="base", upstream={}, client=stub,
        scope=scope, repo_root=workdir, advisory_block=_ADVISORY,
    )
    ex.run("Initial task prompt.")
    msgs = stub.calls[0]["messages"]
    # Order: fake_assistant, fake_tool, user (with advisory tail).
    assert msgs[0]["role"] == "assistant"
    assert msgs[0]["tool_calls"][0]["function"]["name"] == "read_experience"
    assert msgs[1]["role"] == "tool"
    assert msgs[2]["role"] == "user"
    assert "Initial task prompt." in msgs[2]["content"]
    assert "===== MATERIAL ADVISORIES =====" in msgs[2]["content"]


def test_executor_appends_upcoming_steps_block_after_advisory(workdir, step):
    """When both advisory and upcoming are non-empty, they appear in the
    invariant order: base prompt → advisory → upcoming."""
    sub = workdir / step.id
    sub.mkdir()
    stub = _StubClient(responses=[
        _submit_resp({"task_outputs": {"scf_out": "scf_1/scf.out"}}),
    ])
    upcoming = (
        "===== UPCOMING STEPS (informational, do not act on) =====\n"
        "After this step, the orchestrator will continue with:\n"
        "  - bands_1 (bands)\n"
        "  - dos_1 (dos)\n"
        "===== END UPCOMING STEPS =====\n"
    )
    ex = ExecutorAgent(
        step=step, main_workdir=workdir,
        base_system_prompt="base", upstream={}, client=stub,
        advisory_block=_ADVISORY, upcoming_steps_block=upcoming,
    )
    ex.run("Initial task prompt.")
    content = stub.calls[0]["messages"][0]["content"]
    pos_task = content.index("Initial task prompt.")
    pos_advisory = content.index("===== MATERIAL ADVISORIES =====")
    pos_upcoming = content.index("===== UPCOMING STEPS")
    assert pos_task < pos_advisory < pos_upcoming
    assert "bands_1 (bands)" in content
    assert "dos_1 (dos)" in content


def test_executor_empty_upcoming_block_is_noop(workdir, step):
    sub = workdir / step.id
    sub.mkdir()
    stub = _StubClient(responses=[
        _submit_resp({"task_outputs": {"scf_out": "scf_1/scf.out"}}),
    ])
    ex = ExecutorAgent(
        step=step, main_workdir=workdir,
        base_system_prompt="base", upstream={}, client=stub,
        upcoming_steps_block="",
    )
    ex.run("Initial task prompt.")
    content = stub.calls[0]["messages"][0]["content"]
    assert "UPCOMING STEPS" not in content


def test_executor_upcoming_block_not_repeated_on_resume(workdir, step):
    sub = workdir / step.id
    sub.mkdir()
    stub = _StubClient(responses=[
        _submit_resp({"task_outputs": {"scf_out": "scf_1/scf.out"}}),
        _submit_resp({"task_outputs": {"scf_out": "scf_1/scf.out"}}),
    ])
    upcoming = (
        "===== UPCOMING STEPS (informational, do not act on) =====\n"
        "  - bands_1 (bands)\n"
        "===== END UPCOMING STEPS =====\n"
    )
    ex = ExecutorAgent(
        step=step, main_workdir=workdir,
        base_system_prompt="base", upstream={}, client=stub,
        upcoming_steps_block=upcoming,
    )
    ex.run("Initial task prompt.")
    ex.resume("fix this")
    # The resume()'s new user message (last in second call's messages) must
    # NOT include the upcoming block — it persists from the first turn via
    # self.messages.
    last_user = stub.calls[1]["messages"][-1]
    assert last_user["role"] == "user"
    assert "UPCOMING STEPS" not in last_user["content"]
    assert "VERIFIER FEEDBACK" in last_user["content"]


def test_executor_advisory_block_persists_on_resume(workdir, step):
    """Resume should NOT re-append the advisory block — it's already in
    self.messages from run()."""
    sub = workdir / step.id
    sub.mkdir()
    stub = _StubClient(responses=[
        _submit_resp({"task_outputs": {"scf_out": "scf_1/scf.out"}}),
        _submit_resp({"task_outputs": {"scf_out": "scf_1/scf.out"}}),
    ])
    ex = ExecutorAgent(
        step=step, main_workdir=workdir,
        base_system_prompt="base", upstream={}, client=stub,
        advisory_block=_ADVISORY,
    )
    ex.run("Initial task prompt.")
    ex.resume("things broke")

    # First call: advisory block in user message.
    first_user = stub.calls[0]["messages"][0]
    assert first_user["role"] == "user"
    assert "===== MATERIAL ADVISORIES =====" in first_user["content"]

    # Second call: the new user_msg is the diagnosis-only message — must NOT
    # contain the advisory block (avoid double injection).
    new_user_in_second_call = stub.calls[1]["messages"][-1]
    assert new_user_in_second_call["role"] == "user"
    assert "===== MATERIAL ADVISORIES =====" not in new_user_in_second_call["content"]
    assert "VERIFIER FEEDBACK" in new_user_in_second_call["content"]
