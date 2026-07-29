"""PlannerOrchestrator — v4.3 主调度循环。

设计要点（plan §架构）：
- 双 ledger：TaskLedger（初始 plan + replans）+ ProgressLedger（每个 step 的 attempts）
- Planner 纯文本决策，仅靠 verifier diagnosis 重规划（不直接读文件）
- 调度顺序：拓扑序选 ready step → stage → executor → verifier → promote 或 retry/replan
- 每 step ≤2 次 retry；replan ≤2 次；耗尽则 submit incomplete
- 输出 result.json (shape 不变) + workdir/orchestrator.json (ledger 序列化)
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from pydantic import ValidationError

from agent import experience as _experience
from agent.ledger import (
    Attempt,
    PlanSpec,
    PlanStep,
    ReplanEvent,
    StepProgress,
    SUBMIT_PLAN_SCHEMA,
    TaskLedger,
    VerdictSpec,
    format_validation_error,
    now_iso,
    parse_plan,
)
from agent.llm import LLMClient, NormalizedResponse
from agent.loop import _append_transcript, _serialize_response
from agent.step_kinds import physics_sanity as _physics_sanity
from agent.subagents.executor import ExecutorAgent
from agent.subagents import reflection_writer as _reflection_writer
from agent.subagents.verifier import VerifierAgent
from agent.workdir_stager import WorkdirStager


# Submission keys that signal a bands_dos task (mirror of solve.py:is_bands_dos_task
# kept here to avoid solve.py import cycle).
_BANDS_DOS_TRIGGER_KEYS = frozenset({"bands_data", "dos_dat"})


# ─────────────────────────── tool schemas for planner ─────────────────────────

PLANNER_TOOLS: list[dict[str, Any]] = [
    {
        "name": "submit_plan",
        "description": (
            "Submit a structured plan. The orchestrator will execute the steps "
            "in topological order. Call this once at the start, and again only "
            "if you need to replan after step failures."
        ),
        "parameters": SUBMIT_PLAN_SCHEMA,
    },
    {
        "name": "give_up",
        "description": (
            "Use only when retry/replan budget is exhausted and the task "
            "genuinely cannot be completed. The orchestrator writes incomplete."
        ),
        "parameters": {
            "type": "object",
            "required": ["reason"],
            "properties": {"reason": {"type": "string"}},
        },
    },
]


# ─────────────────────────── prompts ─────────────────────────────────────────

def _build_planner_system_prompt(required_keys: list[str],
                                 advice_block: str,
                                 prerelax_block: str) -> str:
    keys_block = ", ".join(f"`{k}`" for k in required_keys)
    parts = [
        "You are the **PLANNER** of a multi-agent DFT (Quantum ESPRESSO) solver.\n",
        "Your job: decompose the task into a sequence of steps that, when "
        "executed, produces the requested outputs. You do NOT execute steps — "
        "executor subagents do. You do NOT read files — verifier subagents do.\n\n",
        f"## Required outputs\n{keys_block}\n\n",
        "## Available step kinds\n"
        "- `scf`      — self-consistent field; needs structure; produces scf.out + out/<prefix>.save\n"
        "- `vc_relax` — variable-cell relax; produces vc-relax.out with tight convergence\n"
        "- `relax`    — fixed-cell relax (rare; usually use vc_relax instead)\n"
        "- `bands`    — band structure along k-path; needs upstream scf charge density\n"
        "- `dos`      — density of states; dense k-mesh nscf + projwfc/dos\n"
        "- `collect`  — gather/post-process files (rename, copy, build pdos sum)\n\n",
        "## Plan rules\n"
        "- Each step has a unique `id` (e.g. `scf_1`, `bands_1`).\n"
        "- `deps` declares which step ids must complete first; the orchestrator runs them in topological order.\n"
        "- `expected_outputs` maps a submission key (or intermediate label) to a workdir-relative path. The verifier checks these.\n"
        "- **PATH CONVENTION (REQUIRED)**: every path in `expected_outputs` MUST start with "
        "  `<step.id>/` (the step's own id followed by `/`). Example: for `id=vc_relax_1`, "
        "  declare `\"vc_relax_out\": \"vc_relax_1/vc-relax.out\"`, NOT just `\"vc-relax.out\"`. "
        "  The executor writes every file for this step under that subdir, and the verifier "
        "  + bench audit will read paths verbatim. **Plans with un-prefixed paths will be rejected.**\n"
        "- For the FINAL step that produces a required output, the key in `expected_outputs` MUST match the required-keys list above.\n"
        "- Keep plans MINIMAL — fewer steps = less to debug. For vc_relax tasks, often a single `vc_relax` step suffices.\n\n",
        "## When you are called\n"
        "1. Initial call: produce a plan via `submit_plan`.\n"
        "2. Replan call: a step failed retry; produce a NEW plan via `submit_plan` "
        "   that fixes the issue (different parameters, different decomposition).\n"
        "3. Give-up call: only after multiple replans fail; call `give_up` with a reason.\n",
    ]
    if advice_block:
        parts.append("\n## CONFIG ADVISORY (apply consistently across all steps)\n")
        parts.append(advice_block)
        parts.append("\n")
    if prerelax_block:
        parts.append("\n## PRE-RELAXED GEOMETRY (T2; use this for SCF/bands/DOS instead of original cell)\n")
        parts.append(prerelax_block)
        parts.append("\n")
    return "".join(parts)


def _format_diagnosis_for_planner(diag: Any) -> str:
    """Format a diagnosis (str or structured dict) into a body suitable for
    the planner's replan prompt. Structured form gets rendered as a
    bulleted action list so the planner makes specific, scoped changes
    rather than free-form prose.
    """
    if isinstance(diag, dict) and "fix_actions" in diag:
        summary = diag.get("summary") or ""
        actions = diag.get("fix_actions") or []
        if not actions:
            # soft_warn dict with no concrete fix (e.g. PBE underestimate)
            return summary or "(no actionable fix; advisory only)"
        bullets = []
        for a in actions:
            file_part = a.get("file", "?")
            field_part = a.get("field", "?")
            suggested = a.get("suggested", "?")
            rationale = a.get("rationale", "")
            line = f"  - In `{file_part}`, set `{field_part}` to {suggested}"
            if rationale:
                line += f"\n      (reason: {rationale})"
            bullets.append(line)
        return summary + "\n\nMust fix in next plan:\n" + "\n".join(bullets)
    return str(diag) if diag is not None else ""


def _replan_user_msg(failed_step: PlanStep, last_diagnosis: Any, attempts: int) -> str:
    body = _format_diagnosis_for_planner(last_diagnosis)
    tail = (
        "Produce a NEW plan via `submit_plan`. You may change parameters "
        "(e.g. tighten conv_thr, change k-points, add a vc_relax preceding the scf), "
        "restructure dependencies, or split the step into smaller pieces."
    )
    if isinstance(last_diagnosis, dict) and last_diagnosis.get("fix_actions"):
        tail += " Address EVERY item in the 'Must fix' list above."
    else:
        tail += " Try to address the diagnosis directly."
    return (
        f"Step `{failed_step.id}` ({failed_step.kind}) failed after {attempts} attempts. "
        f"Verifier diagnosis from the last attempt:\n\n{body}\n\n"
        f"{tail}"
    )


# ─────────────────────────── result ─────────────────────────────────────────

@dataclass
class OrchestratorResult:
    """Final outcome handed back to solve.py."""
    payload: dict[str, Any]            # 形状 = result.json content
    ledger: TaskLedger
    # Stats for solve.py 日志:
    total_steps: int = 0
    total_attempts: int = 0
    replans: int = 0


# ─────────────────────────── PR-C: upcoming-steps hint ────────────────────


def _build_upcoming_steps_block(steps: "list[PlanStep]", current_id: str) -> str:
    """Render a markdown block listing the steps the orchestrator will run
    AFTER the current one. Skips physics_sanity (internal verifier flow)
    and exposes only id+kind — never `desc` (avoids biasing the executor
    into pre-doing later work).

    Returns "" when current is the last non-sanity step or not in the list.
    """
    idx = next((i for i, s in enumerate(steps) if s.id == current_id), -1)
    if idx < 0:
        return ""
    after = [s for s in steps[idx + 1:] if s.kind != "physics_sanity"]
    if not after:
        return ""
    listing = "\n".join(f"  - {s.id} ({s.kind})" for s in after)
    return (
        "===== UPCOMING STEPS (informational, do not act on) =====\n"
        "After this step, the orchestrator will continue with:\n"
        f"{listing}\n"
        "Don't overoptimise the current step — leave budget for the rest.\n"
        "===== END UPCOMING STEPS =====\n"
    )


# ─────────────────────────── orchestrator ──────────────────────────────────

class PlannerOrchestrator:
    """Drives the planner + step execution + verifier loop."""

    def __init__(
        self,
        *,
        workdir: Path,
        required_keys: list[str],
        task_prompt: str,
        base_executor_prompt: str,
        cli_overrides: Optional[dict[str, str]] = None,
        config_path: Optional[Path] = None,
        config_advice_block: str = "",
        prerelax_block: str = "",
        max_replans: int = 5,
        max_retries_per_step: int = 2,
        executor_max_turns: int = 40,
        verifier_max_turns: int = 12,
        max_output_tokens: int = 8192,
        scope_features: Optional[dict[str, Any]] = None,
        repo_root: Optional[Path] = None,
        advisory_block: str = "",
    ):
        self.workdir = workdir.resolve()
        self.required_keys = required_keys
        self.task_prompt = task_prompt
        self.base_executor_prompt = base_executor_prompt
        self.cli_overrides = cli_overrides or {}
        self.config_path = config_path
        self.config_advice_block = config_advice_block
        self.prerelax_block = prerelax_block
        self.max_replans = max_replans
        self.max_retries_per_step = max_retries_per_step
        self.executor_max_turns = executor_max_turns
        self.verifier_max_turns = verifier_max_turns
        self.max_output_tokens = max_output_tokens
        # v4.3 experience-library plumbing. When scope_features is None we
        # behave exactly as v4.1 (no injection, no reflection).
        self.scope_features = scope_features
        self.repo_root = repo_root
        # PR-A: material-family advisory block. Empty string when no skill
        # matches (or library disabled). Same string is appended to both
        # planner-side initial prompt and every executor user prompt.
        self.advisory_block = advisory_block or ""

        self.stager = WorkdirStager(self.workdir)
        self.planner_client = LLMClient.from_role(
            "planner", cli_overrides=self.cli_overrides, config_path=self.config_path,
        )
        self.planner_transcript = self.workdir / "planner_transcript.json"
        # Cost journal: web reconciler (backend/services/cost.py) reads only
        # workdir/transcript.json. Dual-write every planner turn there too.
        self.cost_journal = self.workdir / "transcript.json"
        self.ledger_path = self.workdir / "orchestrator.json"

        # Conversation with planner kept alive for replans.
        self._planner_system = _build_planner_system_prompt(
            required_keys, config_advice_block, prerelax_block,
        )
        self._planner_messages: list[dict[str, Any]] = []
        self._initial_user_msg = (
            "Original task prompt below. Produce the initial plan via `submit_plan`.\n\n"
            "================================================\n"
            f"{task_prompt}"
        )
        # PR-A: append the material-family advisory block (if any) to the
        # planner's initial user prompt. Empty string is a no-op.
        if self.advisory_block:
            self._initial_user_msg = (
                self._initial_user_msg + "\n\n" + self.advisory_block
            )

    # ─────────────────────── public entrypoint ───────────────────────────────

    def run(self) -> OrchestratorResult:
        # 1. Planner: initial plan
        try:
            initial_plan = self._planner_call(self._initial_user_msg)
        except _PlannerGaveUp as gu:
            return self._fail("planner gave up before producing a plan: " + gu.reason,
                              initial_plan=None)
        initial_plan = self._maybe_inject_sanity_step(initial_plan)

        ledger = TaskLedger(
            required_keys=self.required_keys,
            initial_plan=initial_plan,
            progress={s.id: StepProgress(step_id=s.id) for s in initial_plan.steps},
        )
        ledger.write(self.ledger_path)

        # 2. Outer execution loop with replan budget
        replan_no = 0
        while True:
            current_plan = ledger.current_plan()
            failed_step, last_diag = self._execute_plan(ledger, current_plan)
            if failed_step is None:
                # All steps done — collect promoted task_outputs
                return self._success(ledger)

            if replan_no >= self.max_replans:
                # last_diag may be a dict (structured) or str — render via the
                # same helper used for the planner replan prompt.
                diag_text = _format_diagnosis_for_planner(last_diag)
                return self._fail(
                    f"step {failed_step.id} failed; replan budget exhausted "
                    f"({self.max_replans} replans). Last diagnosis: {diag_text}",
                    ledger=ledger,
                )

            replan_no += 1
            # Ask planner for a new plan
            try:
                new_plan = self._planner_call(
                    _replan_user_msg(failed_step, last_diag, self.max_retries_per_step + 1),
                )
            except _PlannerGaveUp as gu:
                return self._fail("planner gave up on replan: " + gu.reason, ledger=ledger)
            new_plan = self._maybe_inject_sanity_step(new_plan)
            ledger.replans.append(ReplanEvent(
                replan_no=replan_no,
                reason=f"step {failed_step.id} failed: {_format_diagnosis_for_planner(last_diag)[:200]}",
                new_plan=new_plan,
                at=now_iso(),
            ))
            # Initialize progress for new step ids that weren't in the previous plan
            for s in new_plan.steps:
                if s.id not in ledger.progress:
                    ledger.progress[s.id] = StepProgress(step_id=s.id)
            ledger.write(self.ledger_path)

    # ─────────────────────── planner call ────────────────────────────────────

    def _planner_call(self, user_msg: str) -> PlanSpec:
        """One round-trip with planner; returns parsed PlanSpec or raises _PlannerGaveUp."""
        # Append user message and run; up to 5 internal turns for schema fixes.
        self._planner_messages.append({"role": "user", "content": user_msg})
        for _ in range(5):
            t0 = time.monotonic()
            resp: NormalizedResponse = self.planner_client.call(
                messages=self._planner_messages,
                system=self._planner_system,
                tools=PLANNER_TOOLS,
                max_output_tokens=self.max_output_tokens,
            )
            wall_ms = (time.monotonic() - t0) * 1000.0
            entry = _serialize_response(resp, llm_wall_ms=wall_ms)
            _append_transcript(self.planner_transcript, entry)
            _append_transcript(self.cost_journal, entry)

            asst_msg: dict[str, Any] = {"role": "assistant", "content": resp.text or None}
            if resp.tool_calls:
                asst_msg["tool_calls"] = [{
                    "id": tc.id, "type": "function",
                    "function": {"name": tc.name,
                                 "arguments": json.dumps(tc.input, ensure_ascii=False)},
                } for tc in resp.tool_calls]
            # Thinking-mode models (deepseek-v4-pro etc.) require reasoning_content
            # from prior turns to be passed back; otherwise 400. See verifier.py.
            if resp.reasoning_content:
                asst_msg["reasoning_content"] = resp.reasoning_content
            self._planner_messages.append(asst_msg)

            if not resp.tool_calls:
                # Planner produced plain text but no tool call. Nudge it.
                self._planner_messages.append({
                    "role": "user",
                    "content": "You must call either submit_plan or give_up. Plain text is ignored.",
                })
                continue

            for tc in resp.tool_calls:
                inp = dict(tc.input)
                if "_raw" in inp:
                    self._planner_messages.append({
                        "role": "tool", "tool_call_id": tc.id,
                        "content": f"tool args malformed: {str(inp.get('_raw'))[:200]}",
                    })
                    continue
                if tc.name == "give_up":
                    self._planner_messages.append({
                        "role": "tool", "tool_call_id": tc.id, "content": "give_up acknowledged",
                    })
                    raise _PlannerGaveUp(inp.get("reason", "no reason given"))
                if tc.name == "submit_plan":
                    try:
                        plan = parse_plan(inp)
                    except ValidationError as e:
                        err = format_validation_error(e)
                        self._planner_messages.append({
                            "role": "tool", "tool_call_id": tc.id,
                            "content": f"submit_plan rejected:\n{err}\nFix and resubmit.",
                        })
                        break  # retry
                    self._planner_messages.append({
                        "role": "tool", "tool_call_id": tc.id, "content": "plan accepted",
                    })
                    return plan
            # If we got here without returning, we either hit a bad payload or
            # missed the tool call; loop will retry up to 3 total times.

        raise _PlannerGaveUp("planner failed to produce a valid plan in 5 attempts")

    # ─────────────────────── execute plan ──────────────────────────────────

    def _execute_plan(self, ledger: TaskLedger, plan: PlanSpec
                      ) -> tuple[Optional[PlanStep], str]:
        """Run all steps in topological order. Returns (failed_step, diagnosis)
        or (None, "") on full success.
        """
        all_steps = {s.id: s for s in plan.steps}
        completed: set[str] = {
            sid for sid, sp in ledger.progress.items()
            if sid in all_steps and sp.status == "done"
        }
        remaining = [s for s in plan.steps if s.id not in completed]

        # Topological iteration: pick steps whose deps are all completed.
        # Loop until either we run out, or no progress can be made.
        while remaining:
            ready = [s for s in remaining if all(d in completed for d in s.deps)]
            if not ready:
                missing = {s.id: [d for d in s.deps if d not in completed] for s in remaining}
                return None, f"plan has unresolved dependencies after replan: {missing}"
            for step in ready:
                last_diag = self._execute_step(ledger, step, all_steps)
                ledger.write(self.ledger_path)
                if ledger.progress[step.id].status != "done":
                    return step, last_diag
                completed.add(step.id)
                remaining.remove(step)
        return None, ""

    def _execute_step(self, ledger: TaskLedger, step: PlanStep,
                      all_steps: dict[str, PlanStep]) -> str:
        """Run a single step (executor + verifier) with up to N retries.
        Updates ledger in place. Returns last diagnosis (empty if pass).
        """
        prog = ledger.progress[step.id]
        prog.status = "in_progress"
        ledger.write(self.ledger_path)

        # Stage sub-workdir + link upstream artifacts
        try:
            staged = self.stager.stage(step, all_steps)
        except RuntimeError as e:
            prog.status = "failed"
            return f"workdir staging failed: {e}"

        # physics_sanity step: deterministic, no LLM. Special-case below.
        if step.kind == "physics_sanity":
            return self._execute_physics_sanity_step(ledger, step, staged.sub_workdir)

        step_scope: Optional[dict[str, Any]] = None
        if self.scope_features is not None and self.repo_root is not None:
            step_scope = {**self.scope_features, "step_kind": step.kind}

        # PR-C: render "upcoming steps" hint so executor knows global position
        # and doesn't over-optimise the current step at the expense of later
        # ones. Empty string when current is the last non-sanity step.
        upcoming_block = _build_upcoming_steps_block(
            ledger.current_plan().steps, step.id,
        )

        executor = ExecutorAgent(
            step=step,
            main_workdir=self.workdir,
            base_system_prompt=self.base_executor_prompt,
            upstream=staged.upstream_paths,
            cli_overrides=self.cli_overrides,
            config_path=self.config_path,
            max_turns=self.executor_max_turns,
            max_output_tokens=self.max_output_tokens,
            scope=step_scope,
            repo_root=self.repo_root,
            advisory_block=self.advisory_block,
            upcoming_steps_block=upcoming_block,
        )

        last_diagnosis = ""
        for attempt_no in range(1, self.max_retries_per_step + 2):  # initial + retries
            started = now_iso()
            if attempt_no == 1:
                ex_result = executor.run(self.task_prompt)
            else:
                ex_result = executor.resume(last_diagnosis)
            attempt = Attempt(
                attempt_no=attempt_no,
                executor_session_id=ex_result.session_id,
                executor_submit=ex_result.submit_payload,
                started_at=started,
                finished_at=now_iso(),
            )

            if ex_result.declared_incomplete:
                attempt.verdict = VerdictSpec(**{
                    "pass": False,
                    "diagnosis": f"executor declared incomplete: {ex_result.reason}",
                })
                prog.attempts.append(attempt)
                last_diagnosis = attempt.verdict.diagnosis
                continue  # try resume / next retry

            # Run verifier
            verifier = VerifierAgent(
                step=step,
                main_workdir=self.workdir,
                cli_overrides=self.cli_overrides,
                config_path=self.config_path,
                max_turns=self.verifier_max_turns,
                max_output_tokens=self.max_output_tokens,
            )
            vresult = verifier.run()
            attempt.verdict = vresult.verdict
            prog.attempts.append(attempt)
            last_diagnosis = vresult.verdict.diagnosis

            if vresult.verdict.pass_:
                # Promote expected_outputs to workdir root
                try:
                    self.stager.promote(step)
                except RuntimeError as e:
                    last_diagnosis = f"verifier passed but promote failed: {e}"
                    continue
                prog.status = "done"
                # v4.3 experience-library hook: write a methodology lesson
                # only when this step recovered from a verifier trip (attempt
                # >1 with at least one prior fail) — single-pass successes
                # contribute nothing. Best-effort: never fail the step here.
                if (attempt_no > 1
                        and step_scope is not None
                        and self.repo_root is not None
                        and step.kind != "physics_sanity"):
                    self._reflect_and_record(step, step_scope, prog.attempts)
                return ""
            # verifier failed → next attempt (executor.resume)

        prog.status = "failed"
        return last_diagnosis

    # ─────────────────────── experience reflection (v4.3) ───────────────────

    def _reflect_and_record(self, step: PlanStep,
                            step_scope: dict[str, Any],
                            attempts: list[Attempt]) -> None:
        """Run the reflection_writer subagent and append a validated lesson
        to the experience library. Best-effort: any exception is logged and
        swallowed so a successful step is never demoted.
        """
        try:
            lesson = _reflection_writer.run(
                step=step, attempts=attempts, scope=step_scope,
                workdir=self.workdir,
                cli_overrides=self.cli_overrides, config_path=self.config_path,
            )
            if not lesson:
                return
            ok, reasons = _experience.validate_lesson(lesson, self.required_keys)
            if not ok:
                self._log_experience({
                    "event": "write_rejected", "step_id": step.id,
                    "step_kind": step.kind, "scope": step_scope,
                    "reasons": reasons, "lesson": lesson,
                })
                return
            entry = _experience.append_lesson(
                step.kind, lesson, step_scope, self.repo_root,
            )
            self._log_experience({
                "event": "write", "step_id": step.id,
                "step_kind": step.kind, "scope": step_scope,
                "lesson_id": entry["id"],
                "trigger": entry["lesson"]["trigger"],
                "action":  entry["lesson"]["action"],
            })
        except Exception as e:
            self._log_experience({
                "event": "reflection_error",
                "step_id": step.id,
                "err": f"{type(e).__name__}: {e}",
            })

    def _log_experience(self, record: dict[str, Any]) -> None:
        """Append one JSON line to <workdir>/experience.log. Best-effort."""
        record.setdefault("at", now_iso())
        try:
            log_path = self.workdir / "experience.log"
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with log_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError:
            pass

    # ─────────────────────── result helpers ────────────────────────────────

    def _success(self, ledger: TaskLedger) -> OrchestratorResult:
        # Collect ALL promoted expected_outputs from every step.
        # Do NOT filter by self.required_keys: some bench task definitions
        # (e.g. M1_T2_*_bands_dos) require keys (`nscf_dos_out`) that are
        # absent from task_prompt's Submission section but checked by the
        # bench extractor. Planner is told to declare every artifact; if it
        # produced something legitimate, surface it. write_result still
        # audits the full set (size + JOB DONE for *_out keys).
        # physics_sanity steps are internal; their outputs (sanity_report)
        # are NOT part of the bench submission contract — skip.
        task_outputs: dict[str, str] = {}
        for s in ledger.current_plan().steps:
            if s.kind == "physics_sanity":
                continue
            for k, p in s.expected_outputs.items():
                task_outputs[k] = p

        payload: dict[str, Any] = {"task_outputs": task_outputs}
        # If physics_sanity reported a soft_warn or fail anywhere in the
        # ledger, surface it in result.json `notes` for downstream review.
        sanity_note = self._collect_sanity_notes(ledger)
        if sanity_note:
            payload["notes"] = sanity_note

        ledger.final_status = "completed"
        ledger.write(self.ledger_path)
        total_attempts = sum(len(p.attempts) for p in ledger.progress.values())
        return OrchestratorResult(
            payload=payload,
            ledger=ledger,
            total_steps=len(ledger.current_plan().steps),
            total_attempts=total_attempts,
            replans=len(ledger.replans),
        )

    def _collect_sanity_notes(self, ledger: TaskLedger) -> str:
        """Read sanity.json for any physics_sanity step that ran and surface
        soft_warn/fail diagnoses for the result.json notes field. Returns ""
        if all sanity checks passed or none ran.
        """
        notes: list[str] = []
        for s in ledger.current_plan().steps:
            if s.kind != "physics_sanity":
                continue
            sanity_path = self.workdir / s.expected_outputs.get(
                "sanity_report", f"{s.id}/sanity.json")
            if not sanity_path.exists():
                continue
            try:
                data = json.loads(sanity_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            verdict = data.get("verdict")
            # Prefer legacy string diagnosis for the notes-on-success path
            # (it's short and already prose-shaped). Fall back to a flat
            # rendering of the structured form if no string exists.
            diag_text = data.get("diagnosis") or ""
            if not diag_text:
                struct = data.get("diagnosis_struct")
                if isinstance(struct, dict):
                    diag_text = struct.get("summary", "")
            # FIX-2: blocked_by_bench 也要进 notes — agent 没错，但 user
            # 需要看到 bench 评分会有 hit，便于后续追溯。
            if verdict in ("soft_warn", "fail", "blocked_by_bench") and diag_text:
                notes.append(f"[physics_sanity verdict={verdict}] {diag_text}")
        return " ".join(notes)

    # ─────────────────────── physics_sanity special case ───────────────────

    def _execute_physics_sanity_step(self, ledger: TaskLedger, step: PlanStep,
                                     sub_workdir: Path) -> str:
        """Run the deterministic physics sanity check (no LLM) and record a
        synthetic Attempt in the ledger. Returns the diagnosis on fail or
        "" on pass/soft_warn (which both let the plan complete).
        """
        prog = ledger.progress[step.id]
        # Collect every key promoted by prior completed steps (their
        # expected_outputs paths are workdir-relative and the files exist
        # because their verifier passed + promote ran).
        expected_so_far: dict[str, str] = {}
        for s in ledger.current_plan().steps:
            if s.kind == "physics_sanity":
                continue
            if ledger.progress.get(s.id, StepProgress(step_id=s.id)).status != "done":
                continue
            for k, p in s.expected_outputs.items():
                expected_so_far[k] = p

        started = now_iso()
        try:
            result = _physics_sanity.run_check(
                workdir=self.workdir,
                sanity_subdir=sub_workdir,
                expected_outputs_so_far=expected_so_far,
                config_advice_path=self.workdir / ".config_advice.json",
            )
        except Exception as e:
            # Defense: run_check is supposed to never raise, but if it does,
            # degrade to skipped (don't crash the whole run).
            result = {"verdict": "skipped",
                      "diagnosis": f"run_check raised unexpectedly: {type(e).__name__}: {e}",
                      "details": {}}

        verdict = result.get("verdict", "skipped")
        # Legacy free-text — kept on VerdictSpec (which still requires str)
        # for ledger / external sanity.json readers.
        diagnosis_text = result.get("diagnosis", "")
        # Structured form (when a rule produced one) — preferred by the
        # planner replan path because the bulleted action list survives
        # LLM paraphrase better than free prose.
        diagnosis_struct = result.get("diagnosis_struct")
        # 'pass' / 'soft_warn' / 'skipped' all let the step succeed; only
        # 'fail' triggers retry/replan.
        verdict_pass = verdict != "fail"
        attempt = Attempt(
            attempt_no=1,
            executor_session_id=f"physics-sanity-{step.id}",
            executor_submit={"task_outputs": dict(step.expected_outputs),
                             "notes": f"sanity verdict={verdict}"},
            verdict=VerdictSpec(**{"pass": verdict_pass, "diagnosis": diagnosis_text}),
            started_at=started,
            finished_at=now_iso(),
        )
        prog.attempts.append(attempt)

        if verdict_pass:
            # Promote sanity_report (file already at the declared path)
            try:
                self.stager.promote(step)
            except RuntimeError as e:
                prog.status = "failed"
                return f"sanity step promote failed: {e}"
            prog.status = "done"
            return ""

        # verdict == 'fail' → leave status as in_progress; outer loop will
        # see failed_step and trigger replan (we don't retry the same check
        # in-place — the fix is upstream in bands params, only planner can
        # repair via new plan). Return the STRUCTURED diagnosis when one
        # exists so _replan_user_msg can render bulleted fix actions.
        prog.status = "failed"
        return diagnosis_struct if diagnosis_struct else diagnosis_text

    # ─────────────────────── sanity step injection ─────────────────────────

    def _maybe_inject_sanity_step(self, plan: PlanSpec) -> PlanSpec:
        """For bands_dos tasks, append a physics_sanity step if absent.

        The step deps on all bands_* + dos_* + scf_* steps so it runs after
        every QE artifact exists. Idempotent: if planner already declared a
        physics_sanity step (or we're on a replan that still has one), skip.
        """
        is_bands_dos = bool(_BANDS_DOS_TRIGGER_KEYS & set(self.required_keys))
        if not is_bands_dos:
            return plan
        if any(s.kind == "physics_sanity" for s in plan.steps):
            return plan

        # Deps: every prior step that produced physical artifacts (we want
        # the check to run last). Restrict to scf / bands / dos / collect
        # / vc_relax / relax — exclude the placeholder kind itself.
        physics_kinds = {"scf", "vc_relax", "relax", "bands", "dos", "collect"}
        deps = [s.id for s in plan.steps if s.kind in physics_kinds]
        if not deps:
            return plan

        # Pick a unique sanity step id; reuse `physics_sanity_1` unless
        # planner happened to use that id for something else.
        sid = "physics_sanity_1"
        existing_ids = {s.id for s in plan.steps}
        i = 2
        while sid in existing_ids:
            sid = f"physics_sanity_{i}"
            i += 1

        sanity_step = PlanStep(
            id=sid,
            kind="physics_sanity",
            desc=("Auto-injected: cross-check config_advisor.is_metal against "
                  "bench's extract_t2_fields to catch metal-vs-insulator "
                  "extraction mismatches before submission."),
            deps=deps,
            expected_outputs={"sanity_report": f"{sid}/sanity.json"},
        )
        # PlanSpec is a pydantic model; rebuild to re-run validators.
        return PlanSpec(steps=list(plan.steps) + [sanity_step])

    def _fail(self, reason: str, *,
              ledger: Optional[TaskLedger] = None,
              initial_plan: Optional[PlanSpec] = None) -> OrchestratorResult:
        if ledger is None:
            # Planner never produced a plan; synthesize minimal ledger
            empty_plan = initial_plan or PlanSpec.model_construct(steps=[])
            ledger = TaskLedger(
                required_keys=self.required_keys,
                initial_plan=empty_plan if empty_plan.steps else PlanSpec.model_construct(steps=[]),
                final_status="incomplete",
                final_reason=reason,
            )
        else:
            ledger.final_status = "incomplete"
            ledger.final_reason = reason
        try:
            ledger.write(self.ledger_path)
        except Exception:
            pass
        return OrchestratorResult(
            payload={"incomplete": True, "reason": reason},
            ledger=ledger,
            total_steps=len(ledger.current_plan().steps) if ledger.progress else 0,
            total_attempts=sum(len(p.attempts) for p in ledger.progress.values()),
            replans=len(ledger.replans),
        )


class _PlannerGaveUp(Exception):
    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)
