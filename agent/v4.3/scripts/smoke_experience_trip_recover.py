"""End-to-end smoke test of the experience-library trip-recover loop.

Drives `PlannerOrchestrator.run()` with stub LLMClients scripted to:
  1. Planner → submit one-step plan (kind=scf).
  2. Executor attempt 1 → submit task_outputs but write a BAD scf.out (no JOB DONE).
  3. Verifier round 1 → fail with a concrete diagnosis.
  4. Executor attempt 2 → resume() then submit, writing a GOOD scf.out.
  5. Verifier round 2 → pass.
  6. Reflection writer fires (real subagent, stubbed LLM) → submits a clean
     methodology lesson.
  7. Orchestrator validates + appends to experience/scf.jsonl.

After the run, the script prints workdir/experience.log and the new entry
in experience/scf.jsonl. Exits 0 on the expected outcome, 1 otherwise.

Run with: ~/miniconda3/envs/tritondft/bin/python scripts/smoke_experience_trip_recover.py
"""
from __future__ import annotations

import json
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from unittest.mock import patch

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from agent.llm import NormalizedResponse, NormalizedToolCall  # noqa: E402
from agent.orchestrator import PlannerOrchestrator             # noqa: E402
from agent.subagents.executor import ExecutorAgent, ExecutorResult  # noqa: E402
from agent.subagents.verifier import VerifierResult            # noqa: E402
from agent.ledger import VerdictSpec                           # noqa: E402


@dataclass
class _StubClient:
    name: str
    script: list[NormalizedResponse]
    calls: list[dict[str, Any]] = field(default_factory=list)
    model: str = "stub-model"
    provider: str = "stub"

    def call(self, *, messages, system, tools, max_output_tokens):
        self.calls.append({"tools": [t["name"] for t in tools],
                           "n_messages": len(messages)})
        if not self.script:
            raise RuntimeError(f"stub {self.name!r} exhausted (after {len(self.calls)} calls)")
        return self.script.pop(0)


def _resp(tool_calls):
    return NormalizedResponse(
        text="", tool_calls=tool_calls, stop_reason="tool_use",
        usage={"input_tokens": 0, "output_tokens": 0,
               "cache_hit_tokens": None, "cache_miss_tokens": None},
    )


def _tc(name, inp, tid="t1"):
    return NormalizedToolCall(id=tid, name=name, input=inp)


def _good_plan_payload():
    return {
        "steps": [{
            "id": "scf_1", "kind": "scf", "desc": "Run SCF on a test material.",
            "deps": [], "expected_outputs": {"scf_out": "scf_1/scf.out"},
        }],
    }


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="smoke-exp-"))
    workdir = tmp / "workdir"
    workdir.mkdir(parents=True, exist_ok=True)

    # Use a private repo_root so we don't pollute the real `experience/` dir.
    repo_fake = tmp / "repo"
    repo_fake.mkdir(parents=True, exist_ok=True)
    print(f"[smoke] tmp workdir : {workdir}")
    print(f"[smoke] fake repo   : {repo_fake}")

    # ─── Stubs ──────────────────────────────────────────────────────────────

    planner_stub = _StubClient("planner", [
        _resp([_tc("submit_plan", _good_plan_payload(), tid="p1")]),
    ])
    verifier_stub = _StubClient("verifier", [
        _resp([_tc("submit_verdict",
                   {"pass": False, "diagnosis": "scf.out missing JOB DONE"})]),
        _resp([_tc("submit_verdict", {"pass": True, "diagnosis": ""})]),
    ])
    # reflection_writer LLM: one clean lesson, no decline.
    reflection_stub = _StubClient("reflection_writer", [
        _resp([_tc("submit_lesson", {
            "trigger":   "scf log missing JOB DONE marker",
            "action":    "ensure ATOMIC_POSITIONS card has trailing newline",
            "rationale": "QE silently truncates malformed input blocks",
        }, tid="r1")]),
    ])

    # Stubbed ExecutorAgent — bypasses the real run_loop entirely. Two attempts:
    # attempt 1 writes a BAD scf.out (no JOB DONE), attempt 2 writes a GOOD one.
    call_idx = {"n": 0}

    def _executor_factory(*args, **kwargs):
        class _ExWrap(ExecutorAgent):
            def run(self, user_prompt):
                idx = call_idx["n"]; call_idx["n"] += 1
                sub = self.main_workdir / "scf_1"; sub.mkdir(parents=True, exist_ok=True)
                if idx == 0:
                    (sub / "scf.out").write_text("error: convergence not achieved\n" + "x" * 2000)
                else:
                    (sub / "scf.out").write_text("JOB DONE.\n" + "y" * 2000)
                return ExecutorResult(
                    session_id=f"sess-{idx}",
                    submit_payload={"task_outputs": dict(self.step.expected_outputs)},
                )

            def resume(self, diagnosis, additional_turns=None):
                idx = call_idx["n"]; call_idx["n"] += 1
                sub = self.main_workdir / "scf_1"; sub.mkdir(parents=True, exist_ok=True)
                (sub / "scf.out").write_text("JOB DONE.\n" + "y" * 2000)
                return ExecutorResult(
                    session_id=f"sess-{idx}",
                    submit_payload={"task_outputs": dict(self.step.expected_outputs)},
                )
        return _ExWrap(*args, **kwargs)

    # Stub VerifierAgent — pops the next scripted verdict from verifier_stub.
    class _StubVerifier:
        def __init__(self, **kw):
            self.step = kw["step"]
            self.main_workdir = kw["main_workdir"]

        def run(self):
            resp = verifier_stub.script.pop(0)
            tc = resp.tool_calls[0]
            return VerifierResult(
                session_id="vsess",
                verdict=VerdictSpec(**tc.input),
                turns=1,
            )

    def _from_role(role, **kw):
        return {
            "planner": planner_stub,
            "verifier": verifier_stub,
            "reflection_writer": reflection_stub,
            # executor not requested since we stub ExecutorAgent above.
        }.get(role, planner_stub)

    # ─── Drive orchestrator ─────────────────────────────────────────────────

    scope_features = {
        "task_kind": "vc_relax",      # M1_T1 default
        "crystal_system": "cubic",
        "n_atoms_bucket": "≤2",
    }

    with patch("agent.orchestrator.LLMClient.from_role", side_effect=_from_role), \
         patch("agent.orchestrator.ExecutorAgent", side_effect=_executor_factory), \
         patch("agent.orchestrator.VerifierAgent", side_effect=lambda **kw: _StubVerifier(**kw)):
        orch = PlannerOrchestrator(
            workdir=workdir,
            required_keys=["scf_out"],
            task_prompt="Run SCF on a test material.",
            base_executor_prompt="BASE",
            scope_features=scope_features,
            repo_root=repo_fake,
        )
        result = orch.run()

    # ─── Inspect outputs ────────────────────────────────────────────────────

    print(f"\n[smoke] orchestrator finish: status={result.ledger.final_status}")
    print(f"[smoke] total_attempts={result.total_attempts} replans={result.replans}")

    # 1. experience.log should contain inject + write events.
    log_path = workdir / "experience.log"
    print(f"\n--- {log_path} ---")
    if not log_path.exists():
        print("MISSING — no experience.log written!")
        return 1
    log_lines = log_path.read_text(encoding="utf-8").splitlines()
    for line in log_lines:
        print(line)
    events = [json.loads(line)["event"] for line in log_lines]
    has_inject = "inject" in events
    has_write = "write" in events
    print(f"\n[smoke] events seen: {events}")
    print(f"[smoke] inject={has_inject} write={has_write}")

    # 2. experience/scf.jsonl in the fake repo should have one new entry.
    jsonl_path = repo_fake / "experience" / "scf.jsonl"
    print(f"\n--- {jsonl_path} ---")
    if not jsonl_path.exists():
        print("MISSING — no scf.jsonl created!")
        return 1
    entries = [json.loads(line) for line in jsonl_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    print(json.dumps(entries, indent=2, ensure_ascii=False))
    print(f"\n[smoke] entries in scf.jsonl: {len(entries)}")
    if not entries:
        return 1

    entry = entries[0]
    expected_scope = {**scope_features, "step_kind": "scf"}
    if entry["scope"] != expected_scope:
        print(f"[smoke] FAIL: scope mismatch — got {entry['scope']}, want {expected_scope}")
        return 1
    if entry["lesson"]["trigger"] != "scf log missing JOB DONE marker":
        print(f"[smoke] FAIL: lesson trigger mismatch — got {entry['lesson']['trigger']!r}")
        return 1

    # 3. Audit grep on the library — must not leak element symbols / answers.
    raw = jsonl_path.read_text(encoding="utf-8")
    import re
    leaks = []
    if re.search(r"\b(Si|Ge|Fe|Cu|GaAs|Al|O)\b", raw):
        leaks.append("element_symbol")
    if re.search(r"\bgap\b.*=.*eV", raw, re.IGNORECASE):
        leaks.append("answer_value")
    if leaks:
        print(f"[smoke] FAIL: audit leaks detected: {leaks}")
        return 1

    print("\n[smoke] ✅ all checks passed — experience library grew exactly as planned.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
