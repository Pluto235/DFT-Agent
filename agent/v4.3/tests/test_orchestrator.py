"""PlannerOrchestrator — plan validation, retry, replan, success path.

Uses stub clients for planner/executor/verifier; no real API or pw.x.
The executor stub simulates writing expected_outputs to disk so promote
+ verifier path is exercised.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional
from unittest.mock import patch

import pytest

from agent.ledger import VerdictSpec
from agent.llm import NormalizedResponse, NormalizedToolCall
from agent.orchestrator import PlannerOrchestrator


# ─────────────────────────── stub LLMClient ───────────────────────────────────

@dataclass
class _StubClient:
    """Replays NormalizedResponse from a script keyed by call order.

    Each call records (system_prompt_first_line, message_count) so tests
    can assert which role/turn the call belongs to.
    """
    name: str
    script: list[NormalizedResponse]
    calls: list[dict[str, Any]] = field(default_factory=list)
    model: str = "stub-model"
    provider: str = "stub"

    def call(self, *, messages, system, tools, max_output_tokens):
        self.calls.append({
            "system_first_line": (system.splitlines()[0] if system else ""),
            "messages_count": len(messages),
            "tools": [t["name"] for t in tools],
            "last_user": next(
                (m["content"] for m in reversed(messages) if m.get("role") == "user"),
                None,
            ),
        })
        if not self.script:
            raise RuntimeError(f"stub {self.name!r} exhausted")
        return self.script.pop(0)


def _resp(tcs: list[tuple[str, str, dict]]):
    """tcs: list of (tool_id, name, input)."""
    return NormalizedResponse(
        text="",
        tool_calls=[NormalizedToolCall(id=tid, name=name, input=inp)
                    for tid, name, inp in tcs],
        stop_reason="tool_use",
        usage={"input_tokens": 0, "output_tokens": 0,
               "cache_hit_tokens": None, "cache_miss_tokens": None},
    )


# ─────────────────────────── fixtures ─────────────────────────────────────────

@pytest.fixture
def workdir(tmp_path):
    return tmp_path


def _good_plan_payload(workdir_relative_out="scf_1/scf.out"):
    return {
        "steps": [
            {
                "id": "scf_1",
                "kind": "scf",
                "desc": "Run SCF",
                "deps": [],
                "expected_outputs": {"scf_out": workdir_relative_out},
            },
        ],
    }


def _executor_submit(out_key="scf_out", out_path="scf_1/scf.out"):
    return _resp([("e1", "submit", {"task_outputs": {out_key: out_path}})])


def _verifier_pass():
    return _resp([("v1", "submit_verdict", {"pass": True, "diagnosis": ""})])


def _verifier_fail(diagnosis: str):
    return _resp([("v1", "submit_verdict", {"pass": False, "diagnosis": diagnosis})])


# Helper that creates a fake "scf.out with JOB DONE" before the verifier reads it.
# We simulate the executor writing files by patching ExecutorAgent.run / resume.
def _make_executor_factory(file_writers: list[Callable[[Path], None]]):
    """Each call to ExecutorAgent.run / resume:
       - runs the next file_writer (which creates expected output files)
       - returns a successful ExecutorResult.
    """
    from agent.subagents.executor import ExecutorResult
    call_idx = {"n": 0}

    def _factory(*args, **kwargs):
        from agent.subagents.executor import ExecutorAgent

        class _ExWrap(ExecutorAgent):
            def __init__(self, *a, **kw):
                super().__init__(*a, **kw)

            def run(self, user_prompt):
                idx = call_idx["n"]; call_idx["n"] += 1
                file_writers[idx](self.main_workdir)
                return ExecutorResult(
                    session_id=f"sess-{idx}",
                    submit_payload={"task_outputs": dict(self.step.expected_outputs)},
                )

            def resume(self, diagnosis, additional_turns=None):
                idx = call_idx["n"]; call_idx["n"] += 1
                file_writers[idx](self.main_workdir)
                return ExecutorResult(
                    session_id=f"sess-{idx}",
                    submit_payload={"task_outputs": dict(self.step.expected_outputs)},
                )

        return _ExWrap(*args, **kwargs)
    return _factory


def _write_good_scf(sub: Path):
    """Helper for executor stub: write a believable scf.out."""
    (sub / "scf_1").mkdir(parents=True, exist_ok=True)
    (sub / "scf_1" / "scf.out").write_text("JOB DONE.\n" + "x" * 2000)


def _write_bad_scf(sub: Path):
    """Helper: write scf.out without JOB DONE."""
    (sub / "scf_1").mkdir(parents=True, exist_ok=True)
    (sub / "scf_1" / "scf.out").write_text("error\n")


# ─────────────────────────── happy path ───────────────────────────────────────

def test_orchestrator_single_step_success(workdir):
    planner_stub = _StubClient("planner", [
        _resp([("p1", "submit_plan", _good_plan_payload())]),
    ])
    verifier_stub = _StubClient("verifier", [_verifier_pass()])
    executor_factory = _make_executor_factory([_write_good_scf])

    with patch("agent.orchestrator.LLMClient.from_role",
               side_effect=lambda role, **kw: planner_stub if role == "planner" else verifier_stub):
        with patch("agent.orchestrator.ExecutorAgent", side_effect=executor_factory):
            with patch("agent.orchestrator.VerifierAgent",
                       side_effect=lambda **kw: _StubVerifier(verifier_stub, kw)):
                orch = PlannerOrchestrator(
                    workdir=workdir,
                    required_keys=["scf_out"],
                    task_prompt="Run SCF on Si.",
                    base_executor_prompt="BASE",
                )
                result = orch.run()
    assert result.payload == {"task_outputs": {"scf_out": "scf_1/scf.out"}}
    assert result.ledger.final_status == "completed"
    assert result.replans == 0
    assert result.total_attempts == 1
    # promote: file exists at workdir root
    assert (workdir / "scf_1" / "scf.out").exists()


# A tiny verifier wrapper using the stub client so we don't need to monkey-patch
# the whole verifier class.
class _StubVerifier:
    def __init__(self, stub: _StubClient, kwargs: dict):
        self.stub = stub
        # mirror real verifier interface
        self.step = kwargs["step"]
        self.main_workdir = kwargs["main_workdir"]

    def run(self):
        from agent.subagents.verifier import VerifierResult
        resp = self.stub.script.pop(0)
        self.stub.calls.append({"role": "verifier-stub-direct"})
        # extract verdict from tool_calls
        tc = resp.tool_calls[0]
        return VerifierResult(
            session_id=f"vsess-{len(self.stub.calls)}",
            verdict=VerdictSpec(**tc.input),
            turns=1,
        )


# ─────────────────────────── retry on verifier fail ──────────────────────────

def test_orchestrator_retries_on_verifier_fail(workdir):
    planner_stub = _StubClient("planner", [
        _resp([("p1", "submit_plan", _good_plan_payload())]),
    ])
    # verifier: first fails, then passes
    verifier_stub = _StubClient("verifier", [
        _verifier_fail("scf.out missing JOB DONE"),
        _verifier_pass(),
    ])
    # executor: first attempt writes bad scf, second writes good
    executor_factory = _make_executor_factory([_write_bad_scf, _write_good_scf])

    with patch("agent.orchestrator.LLMClient.from_role",
               side_effect=lambda role, **kw: planner_stub if role == "planner" else verifier_stub):
        with patch("agent.orchestrator.ExecutorAgent", side_effect=executor_factory):
            with patch("agent.orchestrator.VerifierAgent",
                       side_effect=lambda **kw: _StubVerifier(verifier_stub, kw)):
                orch = PlannerOrchestrator(
                    workdir=workdir,
                    required_keys=["scf_out"],
                    task_prompt="Run SCF on Si.",
                    base_executor_prompt="BASE",
                )
                result = orch.run()
    assert result.payload == {"task_outputs": {"scf_out": "scf_1/scf.out"}}
    assert result.total_attempts == 2
    assert result.replans == 0


# ─────────────────────────── replan after retry exhaustion ───────────────────

def test_orchestrator_replans_after_retry_exhaustion(workdir):
    """Step fails all retries → planner replans → second plan succeeds."""
    planner_stub = _StubClient("planner", [
        # initial plan
        _resp([("p1", "submit_plan", _good_plan_payload())]),
        # replan with same shape (just to exercise the replan path)
        _resp([("p2", "submit_plan", {
            "steps": [{
                "id": "scf_2",
                "kind": "scf",
                "desc": "Retry SCF with tighter conv",
                "deps": [],
                "expected_outputs": {"scf_out": "scf_2/scf.out"},
            }],
        })]),
    ])
    # First 3 verifier results = all fail; then second plan's verifier passes
    verifier_stub = _StubClient("verifier", [
        _verifier_fail("scf.out missing JOB DONE"),
        _verifier_fail("still missing JOB DONE"),
        _verifier_fail("still missing JOB DONE"),
        _verifier_pass(),
    ])
    # First plan: 3 attempts all write bad. Second plan writes good.
    def _write_good_scf_2(sub: Path):
        (sub / "scf_2").mkdir(parents=True, exist_ok=True)
        (sub / "scf_2" / "scf.out").write_text("JOB DONE.\n" + "x" * 2000)
    executor_factory = _make_executor_factory([
        _write_bad_scf, _write_bad_scf, _write_bad_scf, _write_good_scf_2,
    ])

    with patch("agent.orchestrator.LLMClient.from_role",
               side_effect=lambda role, **kw: planner_stub if role == "planner" else verifier_stub):
        with patch("agent.orchestrator.ExecutorAgent", side_effect=executor_factory):
            with patch("agent.orchestrator.VerifierAgent",
                       side_effect=lambda **kw: _StubVerifier(verifier_stub, kw)):
                orch = PlannerOrchestrator(
                    workdir=workdir,
                    required_keys=["scf_out"],
                    task_prompt="Run SCF on Si.",
                    base_executor_prompt="BASE",
                )
                result = orch.run()
    assert result.payload == {"task_outputs": {"scf_out": "scf_2/scf.out"}}
    assert result.replans == 1


# ─────────────────────────── planner schema retry ─────────────────────────────

def test_orchestrator_rejects_invalid_plan_and_recovers(workdir):
    """Planner submits invalid plan → orchestrator returns schema error to planner;
    planner submits valid plan on second try."""
    planner_stub = _StubClient("planner", [
        # bad plan (kind not in enum)
        _resp([("p0", "submit_plan", {
            "steps": [{"id": "x", "kind": "magic", "desc": "d",
                       "expected_outputs": {"k": "p"}}],
        })]),
        # good plan
        _resp([("p1", "submit_plan", _good_plan_payload())]),
    ])
    verifier_stub = _StubClient("verifier", [_verifier_pass()])
    executor_factory = _make_executor_factory([_write_good_scf])

    with patch("agent.orchestrator.LLMClient.from_role",
               side_effect=lambda role, **kw: planner_stub if role == "planner" else verifier_stub):
        with patch("agent.orchestrator.ExecutorAgent", side_effect=executor_factory):
            with patch("agent.orchestrator.VerifierAgent",
                       side_effect=lambda **kw: _StubVerifier(verifier_stub, kw)):
                orch = PlannerOrchestrator(
                    workdir=workdir,
                    required_keys=["scf_out"],
                    task_prompt="Run SCF on Si.",
                    base_executor_prompt="BASE",
                )
                result = orch.run()
    assert result.ledger.final_status == "completed"


# ─────────────────────────── planner give_up ──────────────────────────────────

def test_orchestrator_handles_planner_give_up(workdir):
    planner_stub = _StubClient("planner", [
        _resp([("p1", "give_up", {"reason": "task is unsolvable in our config"})]),
    ])
    verifier_stub = _StubClient("verifier", [])

    with patch("agent.orchestrator.LLMClient.from_role",
               side_effect=lambda role, **kw: planner_stub if role == "planner" else verifier_stub):
        orch = PlannerOrchestrator(
            workdir=workdir,
            required_keys=["scf_out"],
            task_prompt="Run SCF on Si.",
            base_executor_prompt="BASE",
        )
        result = orch.run()
    assert result.payload.get("incomplete") is True
    assert "gave up" in result.payload["reason"]


# ─────────────────────────── ledger written ──────────────────────────────────

def test_success_surfaces_bonus_keys_beyond_required(workdir):
    """Regression: bench task definitions sometimes require keys (e.g.
    `nscf_dos_out` in M1_T2_*_bands_dos) that are absent from
    task_prompt's Submission section. The planner is encouraged to
    declare every artifact it produces; _success() must surface ALL
    expected_outputs of every step, not just those in required_keys.

    Reproduces the 2026-05-18 Rb bands_dos failure where the solver
    produced dos_1/nscf_dos.out but filtered it out of result.json.
    """
    bonus_plan = {
        "steps": [{
            "id": "scf_1", "kind": "scf", "desc": "Run SCF", "deps": [],
            "expected_outputs": {
                "scf_out":     "scf_1/scf.out",
                "extra_bonus_out": "scf_1/extra.out",  # NOT in required_keys
            },
        }],
    }
    planner_stub = _StubClient("planner", [
        _resp([("p1", "submit_plan", bonus_plan)]),
    ])
    verifier_stub = _StubClient("verifier", [_verifier_pass()])

    def _write_both(sub: Path):
        (sub / "scf_1").mkdir(parents=True, exist_ok=True)
        (sub / "scf_1" / "scf.out").write_text("JOB DONE.\n" + "x" * 2000)
        (sub / "scf_1" / "extra.out").write_text("JOB DONE.\n" + "y" * 2000)

    executor_factory = _make_executor_factory([_write_both])

    with patch("agent.orchestrator.LLMClient.from_role",
               side_effect=lambda role, **kw: planner_stub if role == "planner" else verifier_stub):
        with patch("agent.orchestrator.ExecutorAgent", side_effect=executor_factory):
            with patch("agent.orchestrator.VerifierAgent",
                       side_effect=lambda **kw: _StubVerifier(verifier_stub, kw)):
                orch = PlannerOrchestrator(
                    workdir=workdir,
                    required_keys=["scf_out"],  # only 1 required, but planner declared 2
                    task_prompt="Run SCF on Si.",
                    base_executor_prompt="BASE",
                )
                result = orch.run()
    # Both keys must surface — the bonus key would have been silently
    # dropped under the old filter-by-required_keys logic.
    assert result.payload == {"task_outputs": {
        "scf_out":         "scf_1/scf.out",
        "extra_bonus_out": "scf_1/extra.out",
    }}, f"got {result.payload}"


def test_orchestrator_writes_experience_on_verifier_trip_recover(workdir):
    """When scope_features is set AND attempt_no>1 ends in verifier_pass,
    the orchestrator should call reflection_writer.run AND experience.append_lesson
    exactly once. Single-pass successes don't trigger anything.
    """
    planner_stub = _StubClient("planner", [
        _resp([("p1", "submit_plan", _good_plan_payload())]),
    ])
    # verifier: first fails, then passes — the verifier-trip-recover signal.
    verifier_stub = _StubClient("verifier", [
        _verifier_fail("scf.out missing JOB DONE"),
        _verifier_pass(),
    ])
    executor_factory = _make_executor_factory([_write_bad_scf, _write_good_scf])

    seen_calls = {"reflection": 0, "append": 0, "scope": None, "lesson": None}

    def _fake_reflection(*, step, attempts, scope, workdir, **kw):
        seen_calls["reflection"] += 1
        seen_calls["scope"] = scope
        return {
            "trigger":   "scf.out missing JOB DONE",
            "action":    "check ATOMIC_POSITIONS terminator",
            "rationale": "QE silently truncates malformed cards",
        }

    def _fake_append(step_kind, lesson, scope, repo_root):
        seen_calls["append"] += 1
        seen_calls["lesson"] = lesson
        return {"id": "deadbeef", "scope": scope, "lesson": lesson,
                "written_at": "2026-05-19T00:00:00.000000+00:00"}

    scope_features = {
        "task_kind": "vc_relax",
        "crystal_system": "cubic",
        "n_atoms_bucket": "≤2",
    }

    with patch("agent.orchestrator.LLMClient.from_role",
               side_effect=lambda role, **kw: planner_stub if role == "planner" else verifier_stub), \
         patch("agent.orchestrator.ExecutorAgent", side_effect=executor_factory), \
         patch("agent.orchestrator.VerifierAgent",
               side_effect=lambda **kw: _StubVerifier(verifier_stub, kw)), \
         patch("agent.orchestrator._reflection_writer.run", side_effect=_fake_reflection), \
         patch("agent.orchestrator._experience.append_lesson", side_effect=_fake_append):
        orch = PlannerOrchestrator(
            workdir=workdir,
            required_keys=["scf_out"],
            task_prompt="Run SCF.",
            base_executor_prompt="BASE",
            scope_features=scope_features,
            repo_root=workdir,
        )
        result = orch.run()

    assert result.ledger.final_status == "completed"
    assert seen_calls["reflection"] == 1
    assert seen_calls["append"] == 1
    # Scope passed to reflection_writer includes step_kind merged in
    assert seen_calls["scope"]["step_kind"] == "scf"
    assert seen_calls["scope"]["task_kind"] == "vc_relax"
    assert seen_calls["lesson"]["trigger"].startswith("scf.out missing")

    # experience.log should record the write event.
    log = (workdir / "experience.log").read_text(encoding="utf-8").splitlines()
    assert any("write" in line and "scf_1" in line for line in log)


def test_orchestrator_skips_experience_on_single_pass_success(workdir):
    """Single-pass success → no reflection_writer, no append_lesson. The
    verifier-trip-recover gate is `attempt_no > 1`.
    """
    planner_stub = _StubClient("planner", [
        _resp([("p1", "submit_plan", _good_plan_payload())]),
    ])
    verifier_stub = _StubClient("verifier", [_verifier_pass()])
    executor_factory = _make_executor_factory([_write_good_scf])

    seen = {"reflection": 0, "append": 0}

    def _fake_reflection(**kw):
        seen["reflection"] += 1
        return {"trigger": "x", "action": "y", "rationale": "z"}

    def _fake_append(*args, **kw):
        seen["append"] += 1
        return {}

    with patch("agent.orchestrator.LLMClient.from_role",
               side_effect=lambda role, **kw: planner_stub if role == "planner" else verifier_stub), \
         patch("agent.orchestrator.ExecutorAgent", side_effect=executor_factory), \
         patch("agent.orchestrator.VerifierAgent",
               side_effect=lambda **kw: _StubVerifier(verifier_stub, kw)), \
         patch("agent.orchestrator._reflection_writer.run", side_effect=_fake_reflection), \
         patch("agent.orchestrator._experience.append_lesson", side_effect=_fake_append):
        orch = PlannerOrchestrator(
            workdir=workdir,
            required_keys=["scf_out"],
            task_prompt="Run SCF.",
            base_executor_prompt="BASE",
            scope_features={"task_kind": "vc_relax",
                            "crystal_system": "cubic",
                            "n_atoms_bucket": "≤2"},
            repo_root=workdir,
        )
        orch.run()

    assert seen["reflection"] == 0
    assert seen["append"] == 0


def test_orchestrator_rejects_lesson_that_violates_validator(workdir):
    """If the lesson contains forbidden tokens, the orchestrator validator
    must reject it (no append) and log a write_rejected event.
    """
    planner_stub = _StubClient("planner", [
        _resp([("p1", "submit_plan", _good_plan_payload())]),
    ])
    verifier_stub = _StubClient("verifier", [
        _verifier_fail("scf.out missing JOB DONE"),
        _verifier_pass(),
    ])
    executor_factory = _make_executor_factory([_write_bad_scf, _write_good_scf])

    leaky_lesson = {
        # Contains element symbol "Si" — must be rejected
        "trigger":   "Si vc-relax fails on coarse k-mesh",
        "action":    "increase k-mesh",
        "rationale": "diamond cubic needs ≥ 6³ grid",
    }
    appended = []
    def _fake_append(step_kind, lesson, scope, repo_root):
        appended.append((step_kind, lesson))
        return {"id": "x", "scope": scope, "lesson": lesson, "written_at": ""}

    with patch("agent.orchestrator.LLMClient.from_role",
               side_effect=lambda role, **kw: planner_stub if role == "planner" else verifier_stub), \
         patch("agent.orchestrator.ExecutorAgent", side_effect=executor_factory), \
         patch("agent.orchestrator.VerifierAgent",
               side_effect=lambda **kw: _StubVerifier(verifier_stub, kw)), \
         patch("agent.orchestrator._reflection_writer.run", return_value=leaky_lesson), \
         patch("agent.orchestrator._experience.append_lesson", side_effect=_fake_append):
        orch = PlannerOrchestrator(
            workdir=workdir,
            required_keys=["scf_out"],
            task_prompt="Run SCF.",
            base_executor_prompt="BASE",
            scope_features={"task_kind": "vc_relax",
                            "crystal_system": "cubic",
                            "n_atoms_bucket": "≤2"},
            repo_root=workdir,
        )
        orch.run()

    assert appended == []
    log_text = (workdir / "experience.log").read_text(encoding="utf-8")
    assert "write_rejected" in log_text
    assert "Si" in log_text  # the rejection reason names the leaked token


def test_orchestrator_writes_ledger_to_disk(workdir):
    planner_stub = _StubClient("planner", [
        _resp([("p1", "submit_plan", _good_plan_payload())]),
    ])
    verifier_stub = _StubClient("verifier", [_verifier_pass()])
    executor_factory = _make_executor_factory([_write_good_scf])

    with patch("agent.orchestrator.LLMClient.from_role",
               side_effect=lambda role, **kw: planner_stub if role == "planner" else verifier_stub):
        with patch("agent.orchestrator.ExecutorAgent", side_effect=executor_factory):
            with patch("agent.orchestrator.VerifierAgent",
                       side_effect=lambda **kw: _StubVerifier(verifier_stub, kw)):
                orch = PlannerOrchestrator(
                    workdir=workdir,
                    required_keys=["scf_out"],
                    task_prompt="Run SCF on Si.",
                    base_executor_prompt="BASE",
                )
                orch.run()
    ledger_disk = json.loads((workdir / "orchestrator.json").read_text())
    assert ledger_disk["final_status"] == "completed"
    assert ledger_disk["initial_plan"]["steps"][0]["id"] == "scf_1"
    assert ledger_disk["progress"]["scf_1"]["status"] == "done"
    assert len(ledger_disk["progress"]["scf_1"]["attempts"]) == 1


# ─────────────────────── advisory_block (PR-A) ──────────────────────────────


_ADVISORY = (
    "===== MATERIAL ADVISORIES =====\n"
    "[alkali-metal-advisor] alkali playbook\n"
    "Use smearing on metals.\n"
    "===== END MATERIAL ADVISORIES =====\n"
)


def test_orchestrator_advisory_block_appears_in_planner_user_msg(workdir):
    planner_stub = _StubClient("planner", [
        _resp([("p1", "submit_plan", _good_plan_payload())]),
    ])
    verifier_stub = _StubClient("verifier", [_verifier_pass()])
    executor_factory = _make_executor_factory([_write_good_scf])

    with patch("agent.orchestrator.LLMClient.from_role",
               side_effect=lambda role, **kw: planner_stub if role == "planner" else verifier_stub):
        with patch("agent.orchestrator.ExecutorAgent", side_effect=executor_factory):
            with patch("agent.orchestrator.VerifierAgent",
                       side_effect=lambda **kw: _StubVerifier(verifier_stub, kw)):
                orch = PlannerOrchestrator(
                    workdir=workdir,
                    required_keys=["scf_out"],
                    task_prompt="Run SCF on Rb.",
                    base_executor_prompt="BASE",
                    advisory_block=_ADVISORY,
                )
                orch.run()
    # Planner first call's user message should contain the advisory block.
    first_user = planner_stub.calls[0]["last_user"]
    assert first_user is not None
    assert "===== MATERIAL ADVISORIES =====" in first_user
    assert "[alkali-metal-advisor]" in first_user
    # And the original task prompt is still there.
    assert "Run SCF on Rb." in first_user


def test_orchestrator_passes_advisory_block_to_executor(workdir):
    """The same advisory_block flows into every ExecutorAgent constructor."""
    captured: dict[str, Any] = {}
    planner_stub = _StubClient("planner", [
        _resp([("p1", "submit_plan", _good_plan_payload())]),
    ])
    verifier_stub = _StubClient("verifier", [_verifier_pass()])

    def _factory(*args, **kwargs):
        captured["kwargs"] = kwargs
        return _make_executor_factory([_write_good_scf])(*args, **kwargs)

    with patch("agent.orchestrator.LLMClient.from_role",
               side_effect=lambda role, **kw: planner_stub if role == "planner" else verifier_stub):
        with patch("agent.orchestrator.ExecutorAgent", side_effect=_factory):
            with patch("agent.orchestrator.VerifierAgent",
                       side_effect=lambda **kw: _StubVerifier(verifier_stub, kw)):
                orch = PlannerOrchestrator(
                    workdir=workdir,
                    required_keys=["scf_out"],
                    task_prompt="Run SCF on Rb.",
                    base_executor_prompt="BASE",
                    advisory_block=_ADVISORY,
                )
                orch.run()
    assert "advisory_block" in captured["kwargs"]
    assert captured["kwargs"]["advisory_block"] == _ADVISORY


# ─────────────────────── upcoming_steps_block (PR-C) ──────────────────────


def _ps(sid: str, kind: str, *, desc: str = "x") -> "Any":
    """Helper: construct a PlanStep with a valid id-prefixed output path."""
    from agent.ledger import PlanStep
    return PlanStep(
        id=sid, kind=kind, desc=desc, deps=[],
        expected_outputs={"out": f"{sid}/out.txt"},
    )


def test_build_upcoming_steps_block_skips_physics_sanity():
    """The helper exposes id+kind only; physics_sanity steps are hidden."""
    from agent.orchestrator import _build_upcoming_steps_block
    plan = [
        _ps("scf_1", "scf"),
        _ps("sanity_1", "physics_sanity"),
        _ps("bands_1", "bands"),
        _ps("dos_1", "dos"),
    ]
    block = _build_upcoming_steps_block(plan, "scf_1")
    assert "UPCOMING STEPS" in block
    assert "bands_1 (bands)" in block
    assert "dos_1 (dos)" in block
    assert "sanity_1" not in block
    assert "physics_sanity" not in block


def test_build_upcoming_steps_block_hides_desc():
    """Step descriptions must NEVER leak — biases the executor toward pre-work."""
    from agent.orchestrator import _build_upcoming_steps_block
    plan = [
        _ps("scf_1", "scf"),
        _ps("bands_1", "bands", desc="LEAK_CANARY use 16x16x16 k-mesh"),
    ]
    block = _build_upcoming_steps_block(plan, "scf_1")
    assert "LEAK_CANARY" not in block


def test_build_upcoming_steps_block_empty_when_last_step():
    from agent.orchestrator import _build_upcoming_steps_block
    plan = [_ps("scf_1", "scf"), _ps("collect_1", "collect")]
    assert _build_upcoming_steps_block(plan, "collect_1") == ""


def test_build_upcoming_steps_block_empty_when_only_physics_sanity_remains():
    """Trailing physics_sanity step shouldn't keep the block alive."""
    from agent.orchestrator import _build_upcoming_steps_block
    plan = [
        _ps("scf_1", "scf"),
        _ps("collect_1", "collect"),
        _ps("sanity_1", "physics_sanity"),
    ]
    assert _build_upcoming_steps_block(plan, "collect_1") == ""


def test_build_upcoming_steps_block_empty_when_step_not_in_plan():
    from agent.orchestrator import _build_upcoming_steps_block
    plan = [_ps("scf_1", "scf")]
    assert _build_upcoming_steps_block(plan, "ghost") == ""


def test_orchestrator_passes_upcoming_block_to_executor(workdir):
    """In a 2-step plan, the first step's executor sees the second step in
    its upcoming block; the second step's executor sees an empty block."""
    captured: list[str] = []

    def _factory(*args, **kwargs):
        captured.append(kwargs.get("upcoming_steps_block", ""))
        from agent.subagents.executor import ExecutorAgent, ExecutorResult

        class _ExWrap(ExecutorAgent):
            def run(self, user_prompt):
                (self.main_workdir / self.step.id).mkdir(parents=True, exist_ok=True)
                for k, p in self.step.expected_outputs.items():
                    fp = self.main_workdir / p
                    fp.parent.mkdir(parents=True, exist_ok=True)
                    fp.write_text("JOB DONE.\n" + "x" * 2000)
                return ExecutorResult(
                    session_id=f"sess-{self.step.id}",
                    submit_payload={"task_outputs": dict(self.step.expected_outputs)},
                )

        return _ExWrap(*args, **kwargs)

    two_step_plan = {
        "steps": [
            {"id": "scf_1", "kind": "scf", "desc": "Run SCF", "deps": [],
             "expected_outputs": {"scf_out": "scf_1/scf.out"}},
            {"id": "collect_1", "kind": "collect", "desc": "Collect", "deps": ["scf_1"],
             "expected_outputs": {"final_log": "collect_1/final.log"}},
        ],
    }
    planner_stub = _StubClient("planner", [
        _resp([("p1", "submit_plan", two_step_plan)]),
    ])
    verifier_stub = _StubClient("verifier", [_verifier_pass(), _verifier_pass()])

    with patch("agent.orchestrator.LLMClient.from_role",
               side_effect=lambda role, **kw: planner_stub if role == "planner" else verifier_stub):
        with patch("agent.orchestrator.ExecutorAgent", side_effect=_factory):
            with patch("agent.orchestrator.VerifierAgent",
                       side_effect=lambda **kw: _StubVerifier(verifier_stub, kw)):
                orch = PlannerOrchestrator(
                    workdir=workdir,
                    required_keys=["scf_out", "final_log"],
                    task_prompt="t",
                    base_executor_prompt="BASE",
                )
                orch.run()

    assert "collect_1 (collect)" in captured[0]
    assert captured[1] == ""


def test_orchestrator_default_advisory_block_is_empty(workdir):
    """Backward-compat: omitting advisory_block leaves user msg unchanged."""
    planner_stub = _StubClient("planner", [
        _resp([("p1", "submit_plan", _good_plan_payload())]),
    ])
    verifier_stub = _StubClient("verifier", [_verifier_pass()])
    executor_factory = _make_executor_factory([_write_good_scf])

    with patch("agent.orchestrator.LLMClient.from_role",
               side_effect=lambda role, **kw: planner_stub if role == "planner" else verifier_stub):
        with patch("agent.orchestrator.ExecutorAgent", side_effect=executor_factory):
            with patch("agent.orchestrator.VerifierAgent",
                       side_effect=lambda **kw: _StubVerifier(verifier_stub, kw)):
                orch = PlannerOrchestrator(
                    workdir=workdir,
                    required_keys=["scf_out"],
                    task_prompt="Run SCF on Si.",
                    base_executor_prompt="BASE",
                )
                orch.run()
    first_user = planner_stub.calls[0]["last_user"]
    assert "===== MATERIAL ADVISORIES =====" not in first_user


# ─────────────────────── replan prompt formatting (v4.3 Phase 5) ────────────


def test_format_diagnosis_legacy_string_passthrough():
    """A plain string diagnosis must be returned verbatim (back-compat with
    pre-Phase-5 callers and non-physics_sanity steps that still emit str)."""
    from agent.orchestrator import _format_diagnosis_for_planner
    out = _format_diagnosis_for_planner("verifier failed: missing scf.out")
    assert out == "verifier failed: missing scf.out"


def test_format_diagnosis_structured_renders_bullets():
    """Structured diagnosis from physics_sanity must render as a bulleted
    'Must fix' list with file/field/suggested/rationale per action."""
    from agent.orchestrator import _format_diagnosis_for_planner
    diag = {
        "root_cause": "metal_vs_extracted_gap",
        "summary": "config_advisor=metal but extracted gap=10.988 eV",
        "fix_actions": [
            {"file": "bands.in", "field": "nbnd",
             "suggested": ">= 2x current",
             "rationale": "more bands sampled near EF"},
            {"file": "bands.in", "field": "K_POINTS",
             "suggested": "multi-segment high-symmetry path",
             "rationale": "fermi-slice missed crossing"},
        ],
    }
    out = _format_diagnosis_for_planner(diag)
    assert "10.988" in out
    assert "Must fix in next plan" in out
    assert "bands.in" in out
    assert "`nbnd`" in out
    assert "`K_POINTS`" in out
    assert "EF" in out
    assert "fermi-slice" in out
    # Output is intended for an LLM — must be human-readable, not JSON repr
    assert "'fix_actions'" not in out
    assert "{'file'" not in out


def test_format_diagnosis_structured_empty_fix_actions():
    """soft_warn rules (Rule C/D) carry no actionable fix — render summary only,
    no 'Must fix' suffix that would confuse the planner."""
    from agent.orchestrator import _format_diagnosis_for_planner
    diag = {
        "root_cause": "pbe_underestimates_wide_gap",
        "summary": "expected wide gap > 0.5 eV but extracted 0.01 eV (PBE)",
        "fix_actions": [],
    }
    out = _format_diagnosis_for_planner(diag)
    assert out.startswith("expected wide gap")
    assert "PBE" in out
    assert "Must fix" not in out


def test_format_diagnosis_empty_dict_falls_back_to_placeholder():
    """Pathological dict with no summary and no actions still gives a usable
    string (placeholder) rather than an empty body."""
    from agent.orchestrator import _format_diagnosis_for_planner
    out = _format_diagnosis_for_planner({"fix_actions": []})
    assert out  # non-empty
    assert "advisory" in out or "no actionable" in out


def test_replan_user_msg_structured_emphasizes_must_fix():
    """When the diagnosis carries fix_actions, the tail of the prompt must
    push the planner to address EVERY item, not just paraphrase."""
    from agent.orchestrator import _replan_user_msg
    from agent.ledger import PlanStep
    step = PlanStep(id="bands_1", kind="bands", desc="bands NSCF",
                    deps=["scf_1"], expected_outputs={"bands_out": "bands_1/bands.out"})
    diag = {
        "root_cause": "metal_vs_extracted_gap",
        "summary": "metal but gap=10 eV",
        "fix_actions": [
            {"file": "bands.in", "field": "nbnd",
             "suggested": ">= 40", "rationale": "more bands"},
        ],
    }
    msg = _replan_user_msg(step, diag, attempts=3)
    assert "bands_1" in msg and "(bands)" in msg
    assert "Address EVERY item" in msg
    # Free-text path NOT used
    assert "Try to address the diagnosis directly." not in msg


def test_replan_user_msg_string_uses_legacy_tail():
    """Plain-string diagnosis must keep the original 'Try to address the
    diagnosis directly' tail so non-physics_sanity replans are unchanged."""
    from agent.orchestrator import _replan_user_msg
    from agent.ledger import PlanStep
    step = PlanStep(id="scf_1", kind="scf", desc="scf", deps=[],
                    expected_outputs={"scf_out": "scf_1/scf.out"})
    msg = _replan_user_msg(step, "verifier failed: scf.out missing", attempts=2)
    assert "verifier failed: scf.out missing" in msg
    assert "Try to address the diagnosis directly" in msg
    assert "Address EVERY item" not in msg


def test_replan_event_reason_handles_dict_diagnosis(workdir):
    """Regression for orchestrator.py ReplanEvent construction when the
    failed step returned a structured diagnosis (dict) instead of a string.

    Before the fix, `reason=f"step {sid} failed: {last_diag[:200]}"` slicing
    a dict triggered `TypeError: unhashable type: 'slice'` (dict treated the
    slice as a key and tried to hash it). The 2026-05-25/05-26 Rb T2 runs
    crashed here when physics_sanity's `metal_fermi_crossing_missed_in_kpath`
    rule (added by FIX-24) emitted a `diagnosis_struct` dict.

    The fix: route the dict through `_format_diagnosis_for_planner`, same as
    the helper already used at orchestrator.py:310 and inside _replan_user_msg.
    """
    from agent.ledger import PlanStep
    from agent.orchestrator import PlannerOrchestrator

    # The exact shape physics_sanity Rule A' returns in production.
    dict_diag = {
        "root_cause": "metal_fermi_crossing_missed_in_kpath",
        "summary": "advisor=metal × extracted gap=0.0170 eV ≤ 0.5 eV: k-path missed Fermi crossings",
        "fix_actions": [
            {"file": "bands.in", "field": "nbnd",
             "suggested": "(nelec/2) + max(8, ceil(nat*4))",
             "rationale": "more bands above EF guarantee a crossing in the path"},
        ],
    }

    planner_stub = _StubClient("planner", [
        _resp([("p1", "submit_plan", _good_plan_payload())]),
        _resp([("p2", "submit_plan", _good_plan_payload())]),
    ])
    verifier_stub = _StubClient("verifier", [])

    call_count = {"n": 0}

    def _fake_execute_plan(self, ledger, plan):
        call_count["n"] += 1
        if call_count["n"] == 1:
            failed = plan.steps[0]
            ledger.progress[failed.id].status = "failed"
            return failed, dict_diag
        return None, ""

    with patch("agent.orchestrator.LLMClient.from_role",
               side_effect=lambda role, **kw: planner_stub if role == "planner" else verifier_stub):
        with patch.object(PlannerOrchestrator, "_execute_plan", _fake_execute_plan):
            orch = PlannerOrchestrator(
                workdir=workdir,
                required_keys=["scf_out"],
                task_prompt="Run SCF.",
                base_executor_prompt="BASE",
            )
            result = orch.run()  # must not raise TypeError

    assert result.replans == 1
    ledger_disk = json.loads((workdir / "orchestrator.json").read_text())
    reason = ledger_disk["replans"][0]["reason"]
    assert isinstance(reason, str)
    # Helper output appears (not raw dict repr like "{'root_cause'": ...).
    assert "metal" in reason.lower() or "fermi" in reason.lower() or "0.0170" in reason
    assert "{'root_cause'" not in reason
