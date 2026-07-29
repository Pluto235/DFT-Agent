"""VerifierAgent — tool dispatch + verdict parsing + retry-on-bad-payload."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from agent.ledger import PlanStep
from agent.llm import NormalizedResponse, NormalizedToolCall
from agent.subagents.verifier import VerifierAgent


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


@pytest.fixture
def workdir(tmp_path):
    return tmp_path


@pytest.fixture
def step():
    return PlanStep(
        id="scf_1", kind="scf", desc="Run SCF",
        deps=[], expected_outputs={"scf_out": "scf_1/scf.out"},
    )


# ─────────────────────────── happy path ───────────────────────────────────────

def test_verifier_passes_when_verdict_submitted(workdir, step):
    sub = workdir / step.id
    sub.mkdir()
    stub = _StubClient(responses=[
        _resp([_tc("submit_verdict", {"pass": True, "diagnosis": ""})]),
    ])
    v = VerifierAgent(step=step, main_workdir=workdir, client=stub)
    result = v.run()
    assert result.verdict.pass_ is True
    assert result.turns == 1


def test_verifier_fails_with_diagnosis(workdir, step):
    sub = workdir / step.id
    sub.mkdir()
    stub = _StubClient(responses=[
        _resp([_tc("submit_verdict",
                   {"pass": False, "diagnosis": "scf.out missing JOB DONE"})]),
    ])
    v = VerifierAgent(step=step, main_workdir=workdir, client=stub)
    result = v.run()
    assert result.verdict.pass_ is False
    assert "JOB DONE" in result.verdict.diagnosis


# ─────────────────────────── tool dispatch ────────────────────────────────────

def test_verifier_reads_file_then_submits(workdir, step):
    sub = workdir / step.id
    sub.mkdir()
    # Verifier reads paths relative to main_workdir; "scf_1/scf.out" → workdir/scf_1/scf.out
    (sub / "scf.out").write_text("JOB DONE.\n")
    stub = _StubClient(responses=[
        _resp([_tc("read_file", {"path": "scf_1/scf.out"})]),
        _resp([_tc("submit_verdict", {"pass": True, "diagnosis": "OK"})]),
    ])
    v = VerifierAgent(step=step, main_workdir=workdir, client=stub)
    result = v.run()
    assert result.verdict.pass_
    # second LLM call should have seen the file content as tool result
    second_msgs = stub.calls[1]["messages"]
    tool_msg = [m for m in second_msgs if m.get("role") == "tool"][-1]
    assert "JOB DONE" in tool_msg["content"]


def test_verifier_list_dir(workdir, step):
    sub = workdir / step.id
    sub.mkdir()
    (sub / "out").mkdir()
    (sub / "out" / "si.save").mkdir()
    (sub / "out" / "si.save" / "charge-density.dat").write_text("x" * 100)
    stub = _StubClient(responses=[
        # path is workdir-relative
        _resp([_tc("list_dir", {"path": "scf_1/out/si.save"})]),
        _resp([_tc("submit_verdict", {"pass": True, "diagnosis": ""})]),
    ])
    v = VerifierAgent(step=step, main_workdir=workdir, client=stub)
    v.run()
    tool_content = stub.calls[1]["messages"][-1]["content"]
    assert "charge-density.dat" in tool_content


# ─────────────────────────── bad payload retry ────────────────────────────────

def test_verifier_retries_on_bad_payload(workdir, step):
    sub = workdir / step.id
    sub.mkdir()
    stub = _StubClient(responses=[
        # First: missing diagnosis
        _resp([_tc("submit_verdict", {"pass": True})]),
        # Then: valid
        _resp([_tc("submit_verdict", {"pass": True, "diagnosis": ""})]),
    ])
    v = VerifierAgent(step=step, main_workdir=workdir, client=stub)
    result = v.run()
    assert result.verdict.pass_ is True
    # The error message should have been fed back to the LLM
    rejected_tool_msg = stub.calls[1]["messages"][-1]
    assert "rejected" in rejected_tool_msg["content"]


# ─────────────────────────── budget exhaustion ────────────────────────────────

def test_verifier_synthesizes_fail_on_budget_exhaustion_when_outputs_missing(workdir, step):
    """FIX-17: budget exhausted + expected_outputs missing → fail (preserved)."""
    sub = workdir / step.id
    sub.mkdir()  # but NOT writing scf_1/scf.out → expected_outputs missing
    # Verifier keeps calling list_dir forever — should give up
    stub = _StubClient(responses=[
        _resp([_tc("list_dir", {"path": "."})]) for _ in range(20)
    ])
    v = VerifierAgent(step=step, main_workdir=workdir, client=stub, max_turns=3)
    result = v.run()
    assert result.verdict.pass_ is False
    assert "exhausted" in result.verdict.diagnosis
    assert "expected_outputs check also failed" in result.verdict.diagnosis


def test_verifier_default_passes_on_budget_exhaustion_when_outputs_complete(workdir, step):
    """FIX-17: budget exhausted but executor's expected_outputs all on disk
    + ≥ 1024 B → default-pass. Saves bench from wasting full pw.x reruns on
    a verifier that timed out reading good output."""
    sub = workdir / step.id
    sub.mkdir()
    # Write the expected scf.out (>= 1024 B) so the FIX-17 condition trips
    (sub / "scf.out").write_text("dummy output content\n" * 100, encoding="utf-8")
    assert (workdir / "scf_1/scf.out").stat().st_size >= 1024
    # Verifier keeps calling list_dir forever — exhausts budget
    stub = _StubClient(responses=[
        _resp([_tc("list_dir", {"path": "."})]) for _ in range(20)
    ])
    v = VerifierAgent(step=step, main_workdir=workdir, client=stub, max_turns=3)
    result = v.run()
    assert result.verdict.pass_ is True
    assert "defaulting to PASS" in result.verdict.diagnosis
    assert "exhausted turn budget 3" in result.verdict.diagnosis


def test_verifier_budget_exhaustion_undersized_output_still_fails(workdir, step):
    """FIX-17: file exists but < 1024 B → still fail (executor short-cut)."""
    sub = workdir / step.id
    sub.mkdir()
    (sub / "scf.out").write_text("tiny\n", encoding="utf-8")  # 5 bytes
    stub = _StubClient(responses=[
        _resp([_tc("list_dir", {"path": "."})]) for _ in range(20)
    ])
    v = VerifierAgent(step=step, main_workdir=workdir, client=stub, max_turns=3)
    result = v.run()
    assert result.verdict.pass_ is False
    assert "expected_outputs check also failed" in result.verdict.diagnosis


def test_verifier_synthesizes_fail_when_no_tool_calls(workdir, step):
    """LLM returns plain text (no tool_calls) → verifier fails fast.

    FIX-17 explicitly does NOT route through this path — empty tool_calls is
    a different signal from budget exhaustion (LLM said "nothing to do"
    instead of "still working"), so original fail behavior is preserved."""
    sub = workdir / step.id
    sub.mkdir()
    # Even with expected_outputs present, no-tool-calls path remains fail
    (sub / "scf.out").write_text("x" * 2048, encoding="utf-8")
    stub = _StubClient(responses=[
        _resp([]),  # no tool calls
    ])
    v = VerifierAgent(step=step, main_workdir=workdir, client=stub)
    result = v.run()
    assert result.verdict.pass_ is False
    assert "without calling submit_verdict" in result.verdict.diagnosis


# ─── PR-B: qe_facts injection ────────────────────────────────────────────────


def _seed_qe_facts(workdir: Path, rows: list[dict]) -> None:
    import json as _json
    text = "\n".join(_json.dumps(r) for r in rows) + "\n"
    (workdir / "qe_facts.jsonl").write_text(text, encoding="utf-8")


def test_verifier_user_prompt_contains_qe_facts_block(workdir, step):
    """When qe_facts.jsonl has rows for this step, they're injected into the
    verifier's user prompt with a ===== QE FACTS ===== marker."""
    sub = workdir / step.id
    sub.mkdir()
    _seed_qe_facts(workdir, [
        {"at": "2026-05-20T00:00:00Z", "step_id": "scf_1",
         "output_path": "scf_1/scf.out",
         "facts": {"converged": True, "job_done": True,
                   "n_scf_iterations": 18, "total_energy_ev": -15.83}},
    ])
    stub = _StubClient(responses=[
        _resp([_tc("submit_verdict", {"pass": True, "diagnosis": ""})]),
    ])
    v = VerifierAgent(step=step, main_workdir=workdir, client=stub)
    v.run()
    user_content = stub.calls[0]["messages"][0]["content"]
    assert "===== QE FACTS =====" in user_content
    assert "===== END QE FACTS =====" in user_content
    assert "scf_1/scf.out" in user_content
    assert "converged" in user_content


def test_verifier_user_prompt_filters_qe_facts_by_step_id(workdir, step):
    """Only rows matching this step's id are surfaced — rows for other
    steps in the same task must not leak."""
    sub = workdir / step.id
    sub.mkdir()
    _seed_qe_facts(workdir, [
        {"step_id": "scf_1", "facts": {"converged": True, "tag": "MINE"}},
        {"step_id": "bands_1", "facts": {"converged": True, "tag": "OTHER"}},
        {"step_id": "dos_1", "facts": {"converged": True, "tag": "ANOTHER"}},
    ])
    stub = _StubClient(responses=[
        _resp([_tc("submit_verdict", {"pass": True, "diagnosis": ""})]),
    ])
    v = VerifierAgent(step=step, main_workdir=workdir, client=stub)
    v.run()
    user_content = stub.calls[0]["messages"][0]["content"]
    assert "MINE" in user_content
    assert "OTHER" not in user_content
    assert "ANOTHER" not in user_content


def test_verifier_user_prompt_omits_block_when_no_jsonl(workdir, step):
    """No qe_facts.jsonl file → no block, user prompt is unchanged."""
    sub = workdir / step.id
    sub.mkdir()
    stub = _StubClient(responses=[
        _resp([_tc("submit_verdict", {"pass": True, "diagnosis": ""})]),
    ])
    v = VerifierAgent(step=step, main_workdir=workdir, client=stub)
    v.run()
    user_content = stub.calls[0]["messages"][0]["content"]
    assert "===== QE FACTS =====" not in user_content


def test_verifier_user_prompt_omits_block_when_no_matching_rows(workdir, step):
    """JSONL file exists but no row matches this step_id → no block."""
    sub = workdir / step.id
    sub.mkdir()
    _seed_qe_facts(workdir, [
        {"step_id": "different_step", "facts": {"converged": True}},
    ])
    stub = _StubClient(responses=[
        _resp([_tc("submit_verdict", {"pass": True, "diagnosis": ""})]),
    ])
    v = VerifierAgent(step=step, main_workdir=workdir, client=stub)
    v.run()
    user_content = stub.calls[0]["messages"][0]["content"]
    assert "===== QE FACTS =====" not in user_content


def test_verifier_system_prompt_mentions_qe_facts_authority(workdir, step):
    """Verifier system prompt must instruct trusting qe_facts block."""
    sub = workdir / step.id
    sub.mkdir()
    stub = _StubClient(responses=[
        _resp([_tc("submit_verdict", {"pass": True, "diagnosis": ""})]),
    ])
    v = VerifierAgent(step=step, main_workdir=workdir, client=stub)
    assert "QE FACTS" in v.system_prompt or "qe_facts" in v.system_prompt.lower()
