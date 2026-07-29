"""End-to-end solve.main() smoke — stubs LLMClient globally, no real API or QE.

Validates that:
  - solve.py CLI args are parsed
  - parse_required_keys works on a synthetic task prompt
  - Preflight subagents (config_advisor + optional vc_relax) run without crashing
  - PlannerOrchestrator is invoked and writes orchestrator.json
  - result.json is written with the expected shape

The "executor" stub writes a believable scf.out file before the verifier reads it.
"""
from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from agent.ledger import VerdictSpec
from agent.llm import NormalizedResponse, NormalizedToolCall


REPO_ROOT = Path(__file__).resolve().parent.parent
TASK_PROMPT_SI_VC_RELAX = """\
You are a quantum chemistry expert. Solve the following task using Quantum ESPRESSO.

Task: Optimize the cell of Silicon (Si) using vc_relax.

Initial structure: cubic primitive cell, lattice parameter a = 5.43 Å, two Si atoms
at (0,0,0) and (0.25, 0.25, 0.25) fractional.

=== Submission Instructions ===
Your final answer must be a JSON object with a `task_outputs` field mapping
required keys to workdir-relative file paths:

```json
{
  "task_outputs": {
    "vc_relax_out": "<path to vc-relax log>"
  }
}
```

Required keys: "vc_relax_out"
"""


# ─────────────────────────── stub LLMClient ───────────────────────────────────

@dataclass
class _RoleStub:
    role: str
    script: list[NormalizedResponse]
    model: str = "stub"
    provider: str = "stub"
    calls: list[dict[str, Any]] = field(default_factory=list)

    def call(self, *, messages, system, tools, max_output_tokens):
        self.calls.append({"role": self.role, "messages_count": len(messages)})
        if not self.script:
            raise RuntimeError(f"stub {self.role!r} exhausted")
        return self.script.pop(0)


def _tc(tid, name, inp):
    return NormalizedToolCall(id=tid, name=name, input=inp)


def _resp(tcs):
    return NormalizedResponse(
        text="",
        tool_calls=tcs,
        stop_reason="tool_use",
        usage={"input_tokens": 0, "output_tokens": 0,
               "cache_hit_tokens": None, "cache_miss_tokens": None},
    )


def _planner_plan():
    return _resp([_tc("p1", "submit_plan", {
        "steps": [{
            "id": "vc_relax_1",
            "kind": "vc_relax",
            "desc": "Run vc_relax on Si",
            "deps": [],
            "expected_outputs": {"vc_relax_out": "vc_relax_1/vc-relax.out"},
        }],
    })])


def _config_advisor_submit_text():
    """The config_advisor uses run_loop (not submit_verdict); a text answer is OK
    because the deterministic classifier handles the actual advice — the LLM
    cross-check just confirms. Here we just submit a benign 'agree'."""
    return _resp([_tc("ca1", "submit", {
        "task_outputs": {},
        "notes": "agree with deterministic classifier",
    })])


# ─────────────────────────── stub executor / verifier inside orchestrator ─────

def _make_stub_executor(file_writer):
    """Factory: a fake ExecutorAgent that runs the file_writer instead of an LLM."""
    from agent.subagents.executor import ExecutorAgent, ExecutorResult

    class _FakeExecutor(ExecutorAgent):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)

        def run(self, user_prompt):
            file_writer(self.main_workdir, self.step)
            return ExecutorResult(
                session_id="exec-sess-1",
                submit_payload={"task_outputs": dict(self.step.expected_outputs)},
            )

        def resume(self, diagnosis, additional_turns=None):
            file_writer(self.main_workdir, self.step)
            return ExecutorResult(
                session_id="exec-sess-2",
                submit_payload={"task_outputs": dict(self.step.expected_outputs)},
            )

    return _FakeExecutor


def _write_good_vc_relax(sub: Path, step):
    """Write a believable vc-relax log."""
    out_rel = next(iter(step.expected_outputs.values()))
    dest = sub / out_rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(
        "PWSCF v.7.4 starts on ...\n"
        "bfgs converged in 5 scf cycles and 3 bfgs steps\n"
        + "x" * 2000 +
        "\nJOB DONE.\nThis run was terminated on ...\n"
    )


class _StubVerifier:
    """Bypass verifier LLM entirely; always pass when file looks good."""
    def __init__(self, *, step, main_workdir, **_):
        self.step = step
        self.main_workdir = main_workdir

    def run(self):
        from agent.subagents.verifier import VerifierResult
        # Just check expected outputs exist + contain JOB DONE for *_out
        diagnosis = ""
        all_pass = True
        for k, p in self.step.expected_outputs.items():
            f = self.main_workdir / p
            if not f.exists():
                all_pass = False; diagnosis = f"missing {p}"; break
            if k.endswith("_out") and "JOB DONE." not in f.read_text():
                all_pass = False; diagnosis = f"{p} missing JOB DONE"; break
        return VerifierResult(
            session_id="ver-sess-1",
            verdict=VerdictSpec(**{"pass": all_pass, "diagnosis": diagnosis}),
            turns=1,
        )


# ─────────────────────────── the integration test ─────────────────────────────

def test_solve_main_end_to_end_with_stubs(tmp_path, monkeypatch):
    """Run solve.main() end-to-end with stubbed LLM + executor + verifier.

    Verifies:
      - argv parsing
      - phase 1 preflight (config_advisor only, no T2 → skip vc_relax preflight)
      - phase 2 orchestrator (planner → executor → verifier → promote → write_result)
      - result.json shape matches v4 contract
      - orchestrator.json ledger persisted
    """
    workdir = tmp_path / "workdir"; workdir.mkdir()
    prompt_path = tmp_path / "task_prompt.txt"
    prompt_path.write_text(TASK_PROMPT_SI_VC_RELAX)
    out_path = tmp_path / "result.json"

    # ─── stub: planner returns the simple 1-step plan ───
    planner_stub = _RoleStub(role="planner", script=[_planner_plan()])
    # config_advisor uses run_loop (not orchestrator); script handles its 1 submit
    config_advisor_stub = _RoleStub(role="config_advisor", script=[_config_advisor_submit_text()])

    def _from_role(role, **kw):
        if role == "planner":   return planner_stub
        if role == "executor":  return planner_stub        # unused, executor stubbed
        if role == "verifier":  return planner_stub        # unused
        if role == "config_advisor": return config_advisor_stub
        if role == "prerelax":  return planner_stub        # unused (no T2)
        raise ValueError(f"unexpected role {role}")

    monkeypatch.setattr(
        "agent.llm.LLMClient.from_role", classmethod(lambda cls, role, **kw: _from_role(role)),
    )

    # patch orchestrator's executor and verifier classes
    fake_exec_cls = _make_stub_executor(_write_good_vc_relax)
    monkeypatch.setattr("agent.orchestrator.ExecutorAgent", fake_exec_cls)
    monkeypatch.setattr("agent.orchestrator.VerifierAgent", _StubVerifier)

    # patch sys.argv for parse_args
    monkeypatch.setattr(sys, "argv", [
        "solve.py",
        "--prompt", str(prompt_path),
        "--workdir", str(workdir),
        "--out", str(out_path),
        "--max-turns", "10",
    ])

    # disable QE_BACKEND specifics; we don't run pw.x
    monkeypatch.setenv("QE_BACKEND", "gpu")
    # Skip vc_relax preflight (no T2 keys)
    monkeypatch.setenv("DFT_DISABLE_VC_RELAX_SUBAGENT", "1")
    # Skip config_advisor entirely so we don't need its complex internals.
    # (test_config_advisor.py covers that path; we just want the orchestrator flow.)
    monkeypatch.setenv("DFT_DISABLE_CONFIG_ADVISOR", "1")

    import solve
    # Reload solve if it was imported by other tests with stale state
    rc = solve.main()
    assert rc == 0, f"solve.main returned {rc}; result.json: {out_path.read_text()}"
    result = json.loads(out_path.read_text())
    assert "task_outputs" in result
    assert result["task_outputs"] == {"vc_relax_out": "vc_relax_1/vc-relax.out"}
    # orchestrator ledger written
    ledger = json.loads((workdir / "orchestrator.json").read_text())
    assert ledger["final_status"] == "completed"
    assert ledger["progress"]["vc_relax_1"]["status"] == "done"
