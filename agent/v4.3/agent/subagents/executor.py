"""ExecutorAgent — v4.3 通用 step executor。

复用 agent/loop.py:run_loop 作为引擎。每个 step 一个 ExecutorAgent 实例：

- 构造时拿 LLMClient.from_role("executor", cli_overrides=...) 客户端
- system prompt = base QE prompt + executor 框定 + step kind addon
- 工具子集按 step kind 的 tool_allowlist 过滤
- BashTool 的 cwd 指向 step 子目录（WorkdirStager 准备好的）
- transcript 写到 sub_workdir/transcript.json

支持两种调用：
  run(...)           — 全新 step，从 user_prompt 起
  resume(diagnosis)  — verifier 失败后续跑，把 diagnosis 注入同一 session

ExecutorResult 既给 orchestrator 看（success+payload / failure+reason），
也供 ProgressLedger.attempts 序列化。
"""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from agent import experience as _experience
from agent.ledger import PlanStep
from agent.llm import LLMClient
from agent.loop import TOOLS, run_loop
from agent.step_kinds import StepKindSpec, get as get_step_kind
from agent.tools.bash import BashTool


@dataclass
class ExecutorResult:
    session_id: str
    submit_payload: dict[str, Any]      # run_loop 返回的 payload
    # messages 复用：失败 retry 时 orchestrator 把这个 list 回传给 resume()
    messages: list[dict[str, Any]] = field(default_factory=list)

    @property
    def declared_incomplete(self) -> bool:
        return bool(self.submit_payload.get("incomplete"))

    @property
    def reason(self) -> Optional[str]:
        return self.submit_payload.get("reason")


def _filter_tools(allowlist: tuple[str, ...]) -> list[dict[str, Any]]:
    return [t for t in TOOLS if t["name"] in allowlist]


def _executor_framing(step: PlanStep, main_workdir: Path, step_subdir: str) -> str:
    """Framing block: tells the executor it owns ONE step within the main workdir.

    Key (v4.3 fix): cwd = main_workdir (NOT sub_workdir). This keeps the v4.1
    base system prompt valid (pseudo_dir='./pseudos/', outdir='./out', etc.)
    and ensures bash.log lands in main_workdir for bench audit.
    """
    return (
        "## EXECUTOR SCOPE (v4.3)\n\n"
        f"You are a focused executor for **one step** of a larger plan. Do "
        f"NOT proceed past this step. The orchestrator handles upstream and "
        f"downstream steps; your job is ONLY:\n\n"
        f"- Produce the expected outputs declared for step `{step.id}` ({step.kind}).\n"
        f"- Your bash cwd is the **MAIN workdir** (`{main_workdir}`). Everything\n"
        f"  in the base prompt above about workdir, pseudos, and relative paths\n"
        f"  applies normally.\n"
        f"- Your step's working subdirectory is `{step_subdir}` (under main\n"
        f"  workdir). ALL files you create for this step MUST live under this\n"
        f"  subdir. Use paths like `{step_subdir}/scf.in`, `{step_subdir}/out/`,\n"
        f"  `{step_subdir}/scf.out` etc.\n"
        f"- Upstream step artifacts (declared by `deps`) are already at their\n"
        f"  workdir-relative paths (see the kind-specific block below for the\n"
        f"  list). Reference them directly from your QE input files (e.g.\n"
        f"  `outdir = '{step_subdir}/out'` to write here; `pseudo_dir = './pseudos/'`\n"
        f"  to read pre-staged pseudopotentials).\n"
        f"- When the expected outputs exist at their declared paths, call `submit`\n"
        f"  with `task_outputs` mapping ONLY the keys declared for THIS step.\n"
        f"- Do NOT include outputs from other steps. Do NOT skip declared outputs.\n"
        f"  Ignore the 'all required keys' shape in the base prompt's Submission\n"
        f"  section — the orchestrator aggregates step outputs into the final\n"
        f"  result.json.\n"
        f"- On unrecoverable error, call `submit` with `incomplete=true` and a "
        f"concrete `reason`. The orchestrator may retry or replan."
    )


class ExecutorAgent:
    """Runs ONE step. Reusable across attempts via resume()."""

    def __init__(
        self,
        *,
        step: PlanStep,
        main_workdir: Path,
        base_system_prompt: str,
        upstream: dict[str, list[str]],
        cli_overrides: Optional[dict[str, str]] = None,
        config_path: Optional[Path] = None,
        max_turns: int = 40,
        max_output_tokens: int = 8192,
        transcript_path: Optional[Path] = None,
        client: Optional[LLMClient] = None,
        scope: Optional[dict[str, Any]] = None,
        repo_root: Optional[Path] = None,
        advisory_block: str = "",
        upcoming_steps_block: str = "",
    ):
        """v4.3 fix: BashTool's cwd is the MAIN workdir, not the step subdir.
        The step subdir (workdir/<step_id>/) is just a *file naming convention*:
        executors write all their output files under that prefix, but bash
        commands run from main workdir so pseudo_dir='./pseudos/' etc. work.
        """
        self.step = step
        self.main_workdir = main_workdir
        self.step_subdir = step.id   # workdir-relative naming convention
        self.kind_spec: StepKindSpec = get_step_kind(step.kind)
        self.client = client or LLMClient.from_role(
            "executor", cli_overrides=cli_overrides, config_path=config_path,
        )
        self.bash_tool = BashTool(workdir=main_workdir)
        self.max_turns = max_turns
        self.max_output_tokens = max_output_tokens
        # transcript still lives next to the step's working subdir for organization
        self.transcript_path = transcript_path or (
            main_workdir / step.id / "transcript.json"
        )
        self.transcript_path.parent.mkdir(parents=True, exist_ok=True)
        # Cost journal: web reconciler reads only main_workdir/transcript.json,
        # so dual-write each turn there too (loop handles the actual write).
        self.cost_journal_path = main_workdir / "transcript.json"
        self.tools = _filter_tools(self.kind_spec.tool_allowlist)
        self.system_prompt = self._build_system_prompt(base_system_prompt, upstream)
        # v4.3 experience-library scoping. When `scope` is non-None, run()
        # pre-seeds the conversation with a fake read_experience tool call so
        # the LLM "sees" prior methodology lessons before its first real turn.
        self.scope = scope
        self.repo_root = repo_root
        # PR-A: material-family advisory block, appended once to the first
        # user prompt. Persists into self.messages so resume() doesn't repeat.
        self.advisory_block = advisory_block or ""
        # PR-C: upcoming-steps hint, also appended once. Comes AFTER the
        # advisory_block in user-prompt order — see ordering invariant in
        # the PR-A/B/C cross-PR spec.
        self.upcoming_steps_block = upcoming_steps_block or ""
        # Session state — populated by run(), mutated by resume()
        self.session_id: Optional[str] = None
        self.messages: list[dict[str, Any]] = []

    def _build_system_prompt(self, base: str, upstream: dict[str, list[str]]) -> str:
        framing = _executor_framing(self.step, self.main_workdir, self.step_subdir)
        addon = self.kind_spec.system_prompt_addon(
            self.step, self.main_workdir, self.step_subdir, upstream,
        )
        return f"{base}\n\n{framing}\n\n{addon}"

    # ─────────────────────── experience-library hooks ──────────────────────

    def _inject_experience(self) -> None:
        """Pre-seed self.messages with a fake read_experience tool_use +
        tool_result pair. The LLM will see this as its own boot-time memory
        check. See plan: /path/to/.claude/plans/a-harmonic-clarke.md §6.
        """
        lessons = _experience.load_lessons(self.step.kind, self.scope, self.repo_root)
        fake_id = f"toolu_exp_{uuid.uuid4().hex[:8]}"
        args = {"step_kind": self.step.kind, "scope": dict(self.scope or {})}
        self.messages.append({
            "role": "assistant",
            "content": None,
            "tool_calls": [{
                "id": fake_id, "type": "function",
                "function": {"name": "read_experience",
                             "arguments": json.dumps(args, ensure_ascii=False)},
            }],
        })
        # Trim lesson payload to (id, lesson) only — scope is already in the
        # tool args echo above, and written_at adds noise for the LLM.
        slim = [{"id": le["id"], "lesson": le["lesson"]} for le in lessons]
        self.messages.append({
            "role": "tool", "tool_call_id": fake_id,
            "content": json.dumps({"lessons": slim, "n": len(slim)},
                                  ensure_ascii=False),
        })
        self._log_experience({
            "event": "inject", "step_id": self.step.id,
            "step_kind": self.step.kind, "scope": dict(self.scope or {}),
            "lesson_ids": [le["id"] for le in lessons], "n": len(lessons),
        })

    def _log_experience(self, record: dict[str, Any]) -> None:
        """Append one JSON line to workdir/experience.log. Best-effort."""
        record.setdefault("at", datetime.now(timezone.utc).isoformat(timespec="seconds"))
        try:
            log_path = self.main_workdir / "experience.log"
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with log_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError:
            pass

    # ─────────────────────── run / resume ──────────────────────────────────

    def run(self, user_prompt: str) -> ExecutorResult:
        """Initial attempt. user_prompt = the original task description."""
        if self.session_id is not None:
            raise RuntimeError("ExecutorAgent.run already called; use resume() instead")
        self.session_id = uuid.uuid4().hex
        self.messages = []
        # v4.3 experience injection (pre-seed fake read_experience pair). Errors
        # in lesson lookup are silently swallowed — never block the executor.
        if self.scope is not None and self.repo_root is not None:
            try:
                self._inject_experience()
            except Exception as e:  # defense in depth
                self._log_experience({"event": "inject_error",
                                      "err": f"{type(e).__name__}: {e}"})
        # Append material-family advisory block (PR-A) at the very end of
        # the first user prompt. Persists via self.messages on resume().
        if self.advisory_block:
            user_prompt = user_prompt + "\n\n" + self.advisory_block
        # PR-C: upcoming-steps hint comes AFTER advisory_block (invariant
        # ordering — caches must repeat the same prefix on retry).
        if self.upcoming_steps_block:
            user_prompt = user_prompt + "\n\n" + self.upcoming_steps_block
        payload = run_loop(
            client=self.client,
            system_prompt=self.system_prompt,
            user_prompt=user_prompt,
            bash_tool=self.bash_tool,
            max_turns=self.max_turns,
            max_output_tokens=self.max_output_tokens,
            transcript_path=self.transcript_path,
            cost_journal_path=self.cost_journal_path,
            messages=self.messages,
            tools=self.tools,
            step_kind=self.step.kind,
        )
        return ExecutorResult(
            session_id=self.session_id,
            submit_payload=payload,
            messages=self.messages,
        )

    def resume(self, diagnosis: str, additional_turns: Optional[int] = None) -> ExecutorResult:
        """Verifier failed — inject diagnosis as new user message and continue.

        additional_turns: extra turn budget for this attempt (default: same as initial).
        """
        if self.session_id is None:
            raise RuntimeError("ExecutorAgent.resume called before run()")
        budget = additional_turns if additional_turns is not None else self.max_turns
        user_msg = (
            "## VERIFIER FEEDBACK (v4.3 retry)\n\n"
            "Your previous submission was rejected by the verifier. Diagnosis:\n\n"
            f"{diagnosis}\n\n"
            "Investigate the cause (inspect logs / output files), fix the "
            "issue, regenerate the expected outputs, and submit again. Stay "
            "in the same sub-workdir; the orchestrator has NOT cleaned up."
        )
        payload = run_loop(
            client=self.client,
            system_prompt=self.system_prompt,
            user_prompt=user_msg,
            bash_tool=self.bash_tool,
            max_turns=budget,
            max_output_tokens=self.max_output_tokens,
            transcript_path=self.transcript_path,
            cost_journal_path=self.cost_journal_path,
            messages=self.messages,
            tools=self.tools,
            step_kind=self.step.kind,
        )
        return ExecutorResult(
            session_id=self.session_id,
            submit_payload=payload,
            messages=self.messages,
        )
