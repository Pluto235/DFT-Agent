"""Driver — claude-agent-sdk orchestration for v5.

Builds ClaudeAgentOptions with:
  - executor system prompt (rendered with workdir / pw.x path / required_keys)
  - the dft_qe MCP server (7 structured tools incl. submit + bash escape)
  - convergence_fixer sub-agent
  - PreToolUse / PostToolUse hooks (transcript mirror + stuck detector)

Drives the loop via `query()` and exits the moment the submit tool sets
ctx.submit_payload, or max_turns fires, or a ResultMessage arrives.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    HookMatcher,
    ResultMessage,
    SystemMessage,
    TextBlock,
    UserMessage,
    query,
)

from agent.runtime.hooks import make_post_tool_use_hook, make_pre_tool_use_hook
from agent.runtime.progress_ledger import ProgressLedger
from agent.runtime.transcript import TranscriptWriter
from agent.subagents.convergence import build_convergence_agent
from agent.tools.structured.context import ToolContext
from agent.tools.structured.server import (
    allowed_tools_for_server, build_qe_mcp_server,
)


log = logging.getLogger("dft_agent_v5.driver")


def _is_transient_sdk_error(exc: BaseException) -> bool:
    """True for transient SDK transport drops worth one retry — e.g. the
    deepseek Anthropic-compat endpoint dropping the stream mid-run.

    Deliberately NOT our own deadline cancellation: asyncio.CancelledError is a
    BaseException, so it never reaches the `except Exception` that calls this,
    and its str() is empty anyway (no marker matches).
    """
    msg = str(exc).lower()
    return any(s in msg for s in (
        "stream closed", "connection reset", "connection error",
        "broken pipe", "peer closed",
    ))


def _render_required_keys_block(required_keys: list[str]) -> str:
    if not required_keys:
        return "(none — agent must infer from prompt body)"
    return "Required output keys (must appear in submit's task_outputs):\n" + \
        "\n".join(f"  - {k}" for k in required_keys)


def build_executor_prompt(
    *,
    workdir: Path,
    task_type: str,
    required_keys: list[str],
    pw_x_path: str,
    qe_backend: str,
    template_path: Path | None = None,
) -> str:
    """Render the executor.md template with runtime substitutions.

    Uses plain str.replace rather than str.format because the template
    contains literal `{...}` examples (JSON shapes) that would confuse
    format()'s field parser.
    """
    if template_path is None:
        template_path = Path(__file__).parent.parent / "prompts" / "executor.md"
    text = template_path.read_text(encoding="utf-8")
    for placeholder, value in (
        ("{workdir}", str(workdir)),
        ("{pw_x_path}", pw_x_path),
        ("{qe_backend}", qe_backend),
        ("{required_keys_block}", _render_required_keys_block(required_keys)),
    ):
        text = text.replace(placeholder, value)
    return text


class Driver:
    """One Driver per solver subprocess; not reused across tasks."""

    def __init__(
        self,
        *,
        workdir: Path,
        ctx: ToolContext,
        ledger: ProgressLedger,
        transcript: TranscriptWriter,
        model: str,
        max_turns: int = 60,
        pw_x_path: str = "pw.x",
        qe_backend: str = "gpu",
        max_thinking_tokens: int = 0,
        extra_env: dict[str, str] | None = None,
    ):
        self.workdir = Path(workdir)
        self.ctx = ctx
        self.ledger = ledger
        self.transcript = transcript
        self.model = model
        self.max_turns = max_turns
        self.pw_x_path = pw_x_path
        self.qe_backend = qe_backend
        self.max_thinking_tokens = max_thinking_tokens
        # Env overrides forwarded to the SDK subprocess (e.g. ANTHROPIC_BASE_URL
        # + token for a DeepSeek Anthropic-compatible endpoint).
        self.extra_env = dict(extra_env or {})
        self._wall_t0: float = 0.0
        self._final_usage: dict[str, Any] | None = None
        # Set True when run() catches a transient SDK transport drop (e.g.
        # "Stream closed"); read by solve.py to decide on a one-shot retry.
        self.transient_error: bool = False

    def _build_options(self, task_type: str, required_keys: list[str]) -> ClaudeAgentOptions:
        system_prompt = build_executor_prompt(
            workdir=self.workdir,
            task_type=task_type,
            required_keys=required_keys,
            pw_x_path=self.pw_x_path,
            qe_backend=self.qe_backend,
        )

        pre_hook = make_pre_tool_use_hook(self.ledger, self.transcript)
        post_hook = make_post_tool_use_hook(self.ledger, self.transcript)

        return ClaudeAgentOptions(
            model=self.model,
            system_prompt=system_prompt,
            mcp_servers={"dft_qe": build_qe_mcp_server(self.ctx)},
            allowed_tools=allowed_tools_for_server() + ["Agent"],
            # opus-4.8 otherwise reaches for the native ScheduleWakeup tool to
            # "wait for compute" instead of submitting, burning its turn budget
            # with outputs already on disk (M2_T2_HfO2, 2026-06-12). run_pw is
            # blocking, so an async wait is never needed; remove it from the toolset.
            disallowed_tools=["ScheduleWakeup"],
            agents={"convergence_fixer": build_convergence_agent()},
            hooks={
                "PreToolUse": [HookMatcher(matcher=None, hooks=[pre_hook])],
                "PostToolUse": [HookMatcher(matcher=None, hooks=[post_hook])],
            },
            max_turns=self.max_turns,
            permission_mode="bypassPermissions",
            cwd=str(self.workdir),
            setting_sources=[],   # never load .claude/settings.json from user env
            env=self.extra_env,   # ANTHROPIC_BASE_URL + token for compat endpoints
        )

    async def run(
        self,
        *,
        user_prompt: str,
        task_type: str,
        required_keys: list[str],
    ) -> dict:
        """Drive the SDK loop. Returns a submit payload dict (always)."""
        options = self._build_options(task_type, required_keys)
        self._wall_t0 = time.monotonic()
        self._final_usage = None
        self.transient_error = False
        assistant_turns = 0

        # Hold a reference so we can close it deterministically. `async for`
        # does NOT close its iterator on `break` (PEP 533 deferred); leaving
        # the SDK generator to GC raises "aclose(): asynchronous generator is
        # already running" at event-loop teardown, because its finally-chain
        # (terminate subprocess + cleanup temp dirs) never ran in a controlled
        # context. We aclose() it ourselves in the finally below.
        agen = query(prompt=user_prompt, options=options)
        try:
            async for msg in agen:
                self._handle_message(msg)
                if isinstance(msg, AssistantMessage):
                    assistant_turns += 1
                if isinstance(msg, ResultMessage):
                    self._final_usage = getattr(msg, "usage", None) or {}
                # Submit observed inside a tool → exit ASAP
                if self.ctx.submit_payload is not None:
                    log.info("driver: submit_payload set, breaking loop")
                    break
        except Exception as e:
            self.transient_error = _is_transient_sdk_error(e)
            log.exception("driver: query() raised (transient=%s) — returning incomplete",
                          self.transient_error)
        finally:
            # Deterministic teardown from THIS task: runs the SDK generator's
            # finally-chain cleanly (vs. an "already running" error at GC).
            try:
                await agen.aclose()
            except Exception:
                log.debug("driver: query() aclose during teardown", exc_info=True)

        # Drop phantom tool_use blocks the deepseek endpoint emitted (mirrored
        # via PreToolUse) but never executed — they inflate the transcript's
        # tool counts. The ledger is fed only by PostToolUse (real executions),
        # so it is unaffected; this only cleans the transcript record.
        orphans = self.transcript.reconcile_orphan_tool_uses()

        wall_s = time.monotonic() - self._wall_t0
        log.info("driver: loop done, turns=%d wall=%.1fs orphan_tool_uses_dropped=%d ledger=%s",
                 assistant_turns, wall_s, orphans, self.ledger)

        if self.ctx.submit_payload is not None:
            return self.ctx.submit_payload

        # Loop ended without submit (max_turns or stop) — return graceful incomplete.
        notes_parts = [f"loop ended after {assistant_turns} assistant turns "
                       f"({wall_s:.0f}s wall)"]
        if self.ledger.fired_signals:
            notes_parts.append("ledger signals: " +
                               ", ".join(s.signal for s in self.ledger.fired_signals))
        return {
            "task_outputs": {},
            "notes": "; ".join(notes_parts),
            "incomplete": True,
        }

    @property
    def final_usage(self) -> dict:
        return self._final_usage or {}

    # ----- per-message handling -------------------------------------------

    def _handle_message(self, msg) -> None:
        """Forward SDK messages to ledger/transcript.

        SDK message types (from claude_agent_sdk):
          - SystemMessage:    setup messages (we record but don't react)
          - AssistantMessage: text + tool_use blocks; record text to ledger (S5)
                              and append assistant entry to transcript
          - UserMessage:      tool_result echoes; hooks already mirror these
          - ResultMessage:    final usage stats
        """
        if isinstance(msg, AssistantMessage):
            text_parts: list[str] = []
            tool_calls: list[dict] = []
            for block in msg.content or []:
                # TextBlock has .text; ToolUseBlock has .id .name .input
                if hasattr(block, "text") and block.text:
                    text_parts.append(block.text)
                elif hasattr(block, "name") and hasattr(block, "input"):
                    tool_calls.append({
                        "id": getattr(block, "id", ""),
                        "name": block.name,
                        "input": block.input,
                    })
            text = "\n".join(text_parts) if text_parts else None
            if text:
                self.ledger.record_assistant_text(text)
            usage = getattr(msg, "usage", None) or {}
            if usage:
                inp = usage.get("input_tokens", 0) or 0
                if inp:
                    self.ledger.record_token_usage(int(inp))
            # Append once per assistant turn (PreToolUse hook also writes
            # tool_use mirror entries, but we keep this for text-only turns)
            if text and not tool_calls:
                self.transcript.append_assistant(
                    text=text, tool_calls=None, usage=usage)
        elif isinstance(msg, ResultMessage):
            # ResultMessage carries final aggregated usage; record token total
            usage = getattr(msg, "usage", None) or {}
            if usage:
                self.ledger.record_token_usage(int(usage.get("input_tokens", 0)) or 0)
        # SystemMessage / UserMessage: nothing to do; hooks handle the
        # tool_result flow.


__all__ = ["Driver", "build_executor_prompt"]
