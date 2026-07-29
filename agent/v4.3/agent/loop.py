"""Tool-use loop. Provider-agnostic via LLMClient.

Four tools exposed to the model:
  - bash:       run a shell command in the workdir
  - write_file: create or overwrite a file
  - read_file:  read a file (with optional line slicing)
  - submit:     signal "I'm done"; payload becomes result.json content

Messages are kept in OpenAI Chat Completions shape (canonical); LLMClient
converts to the appropriate provider format internally.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from .llm import LLMClient, NormalizedResponse, NormalizedToolCall
from .step_timeout_floor import floor_for_command
from .tools.bash import BashTool
from .tools.file_io import read_file, write_file


TOOLS: list[dict[str, Any]] = [
    {
        "name": "bash",
        "description": (
            "Run a shell command in the workdir. cwd persists across calls only "
            "if the command is exactly `cd <dir>` on a single line; `cd && cmd` "
            "does NOT persist. Use this for everything: writing input files via "
            "heredoc, invoking pw.x via mpirun, parsing outputs with grep/awk, "
            "running short Python scripts."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "cmd": {"type": "string", "description": "The shell command to execute."},
                "timeout_s": {"type": "integer",
                              "description": "Optional timeout in seconds (default 1800)."},
            },
            "required": ["cmd"],
        },
    },
    {
        "name": "write_file",
        "description": "Create or overwrite a file with the given content. Parent directories are auto-created.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path (relative to cwd or absolute)."},
                "content": {"type": "string", "description": "Text content to write."},
            },
            "required": ["path", "content"],
        },
    },
    {
        "name": "read_file",
        "description": "Read a text file. Optionally slice by line number (offset, limit).",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path (relative to cwd or absolute)."},
                "offset": {"type": "integer", "description": "0-based line offset (default 0)."},
                "limit": {"type": "integer", "description": "Max lines to return (optional)."},
            },
            "required": ["path"],
        },
    },
    {
        "name": "submit",
        "description": (
            "Submit your final answer. After this call, the loop ends and "
            "result.json is written from your payload. For success, pass "
            "task_outputs (a dict mapping each requested key to a workdir-relative "
            "path). For failure, pass incomplete=true with reason."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "task_outputs": {
                    "type": "object",
                    "description": "Map of submission key -> workdir-relative path.",
                    "additionalProperties": {"type": "string"},
                },
                "notes": {"type": "string", "description": "Optional brief audit note."},
                "incomplete": {"type": "boolean", "description": "True for declared failure."},
                "reason": {"type": "string", "description": "Required if incomplete=true."},
            },
        },
    },
    {
        # Pre-seeded by ExecutorAgent at boot (synthetic tool_use + tool_result
        # pair). The LLM never invokes this live; it only sees it as already-
        # completed history in the conversation prefix. We declare it here
        # because providers (Anthropic, OpenAI) reject conversation history
        # containing tool_use whose name is not in the current tools array.
        # If the LLM ever does invoke it live, the dispatcher returns an empty
        # lesson list rather than erroring.
        "name": "read_experience",
        "description": (
            "Read prior methodology lessons for the current step kind. "
            "Pre-seeded by the orchestrator at boot; do not invoke directly."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "step_kind": {"type": "string",
                              "description": "Step kind (scf, bands, dos, vc_relax, relax, collect)."},
                "scope":     {"type": "object",
                              "description": "Optional scope filter passed by the orchestrator."},
            },
            "required": ["step_kind"],
        },
    },
]


def run_loop(
    *,
    client: LLMClient,
    system_prompt: str,
    user_prompt: str | None = None,
    bash_tool: BashTool,
    max_turns: int,
    max_output_tokens: int = 8192,
    transcript_path: Path | None = None,
    cost_journal_path: Path | None = None,
    messages: list[dict[str, Any]] | None = None,
    tools: list[dict[str, Any]] | None = None,
    step_kind: str | None = None,
    max_wall_s: int | None = None,
) -> dict:
    """Run the LLM tool-use loop. Returns the submit payload (dict) on the
    first `submit` tool call, or {"incomplete": True, "reason": "..."} if
    we exhaust max_turns.

    Args (v4.3 additions):
      messages: IN/OUT list of messages in OpenAI Chat Completions shape.
        - None (default): function creates a fresh list seeded from user_prompt.
          Backward-compatible with v4.1 callers (config_advisor, vc_relax preflight).
        - list provided: function appends new turns to this list in place,
          so a caller (e.g. ExecutorAgent on verifier-failure retry) can
          resume the session by passing the same list back with an extra
          user message appended.
      user_prompt: If None, messages must be provided and non-empty. If both
        provided, user_prompt is appended as a new user message before running
        (handy for "inject diagnosis then continue").
      tools: Optional override of the global TOOLS allowlist. Used by
        ExecutorAgent to filter to a step kind's tool subset; verifiers pass
        their own tool list entirely. If None, uses TOOLS.
    """
    active_tools = tools if tools is not None else TOOLS
    if messages is None:
        if user_prompt is None:
            raise ValueError("run_loop: must provide user_prompt or non-empty messages")
        messages = [{"role": "user", "content": user_prompt}]
    elif user_prompt is not None:
        # Continue-session: caller has prior history, wants to append a new
        # user message (e.g. verifier diagnosis) before resuming.
        messages.append({"role": "user", "content": user_prompt})
    elif not messages:
        raise ValueError("run_loop: messages list is empty and no user_prompt given")

    # FIX-25 (2026-05-25): per-turn wall-clock budget check. When the budget
    # is exceeded we inject a "submit incomplete now" user message and give
    # the LLM ONE more turn to gracefully submit. If it still doesn't, hard
    # exit with incomplete reason. None disables (legacy behavior).
    # Used by vc_relax preflight subagent to cap itself at ≤ 50% of harness
    # wall, leaving the main loop ≥ 50% to run SCF + bands + DOS.
    wall_t_start = time.monotonic()
    wall_warning_injected = False

    for turn in range(max_turns):
        if max_wall_s is not None:
            elapsed = time.monotonic() - wall_t_start
            if elapsed > max_wall_s:
                if wall_warning_injected:
                    # LLM ignored the warning last turn and kept going;
                    # hard exit so caller (vc_relax) can fall back.
                    return {
                        "incomplete": True,
                        "reason": (
                            f"wall-time budget exhausted "
                            f"({elapsed:.0f}s of {max_wall_s}s, "
                            f"warning was injected but model did not submit)"
                        ),
                    }
                # First time we see budget exceeded — give LLM a graceful
                # exit window by injecting a directive to submit now.
                messages.append({
                    "role": "user",
                    "content": (
                        f"[wall-time budget exhausted: {elapsed:.0f}s of "
                        f"{max_wall_s}s used]\n"
                        f"You MUST now call `submit` with "
                        f"`incomplete=True` and a one-line `reason` (e.g. "
                        f"`reason='vc_relax wall budget exhausted before "
                        f"convergence'`). The caller has a fallback path "
                        f"(use the original unrelaxed input cell). Do NOT "
                        f"launch any more pw.x — every additional bash "
                        f"call only delays the fallback."
                    ),
                })
                wall_warning_injected = True

        t0 = time.monotonic()
        resp: NormalizedResponse = client.call(
            messages=messages,
            system=system_prompt,
            tools=active_tools,
            max_output_tokens=max_output_tokens,
        )
        llm_wall_ms = (time.monotonic() - t0) * 1000.0

        entry = _serialize_response(resp, llm_wall_ms=llm_wall_ms)
        if transcript_path:
            _append_transcript(transcript_path, entry)
        # Dual-write to the main-workdir cost journal so the web reconciler
        # (backend/services/cost.py reads only workdir/transcript.json) sees
        # every LLM call's usage. No-op if not set or same as transcript_path.
        if cost_journal_path and cost_journal_path != transcript_path:
            _append_transcript(cost_journal_path, entry)

        # Assistant message (OpenAI shape)
        # FIX-16 (2026-05-23): `resp.text or ""` instead of `or None`, plus
        # an entry guard for the deepseek "thinking-only" path where the
        # model returns reasoning_content but no visible text and no
        # tool_calls. Pre-FIX-16, that produced asst_msg={content:None,
        # reasoning_content:...} which OpenAI-compat APIs (deepseek) reject
        # with 400 "Invalid assistant message: content or tool_calls must
        # be set" on the NEXT call (e.g. executor.resume() replays messages).
        # Affects deepseek only; Claude never sets reasoning_content on this
        # code path (see agent/llm.py:163). Detail in
        # reports/standard-bench-failures-5fail-2026-05-22.html §7.
        asst_msg: dict[str, Any] = {
            "role": "assistant",
            "content": resp.text or "",
        }
        if resp.tool_calls:
            asst_msg["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.name,
                        "arguments": json.dumps(tc.input, ensure_ascii=False),
                    },
                }
                for tc in resp.tool_calls
            ]
        if resp.reasoning_content:
            asst_msg["reasoning_content"] = resp.reasoning_content
        # Entry guard: API requires content or tool_calls non-empty. If both
        # are empty (thinking-only response on deepseek), fill content with
        # a placeholder so the next API call doesn't 400.
        if not asst_msg["content"] and "tool_calls" not in asst_msg:
            asst_msg["content"] = (
                "(internal: thinking-only step, model returned no visible "
                "output or tool call; continuing)"
            )
        messages.append(asst_msg)

        if not resp.tool_calls:
            return {"incomplete": True,
                    "reason": f"end_turn without submit (turn {turn + 1})"}

        tool_results: list[dict[str, Any]] = []
        submit_payload: dict | None = None

        for tc in resp.tool_calls:
            inp = dict(tc.input)

            # Malformed/truncated JSON from LLM — let it self-recover
            if "_raw" in inp:
                raw = str(inp.get("_raw", ""))[:200]
                tool_results.append({
                    "role": "tool", "tool_call_id": tc.id,
                    "content": f"tool args malformed (truncated JSON): {raw}",
                })
                continue

            if tc.name == "submit":
                submit_payload = inp
                tool_results.append({
                    "role": "tool", "tool_call_id": tc.id,
                    "content": "submitted",
                })

            elif tc.name == "bash":
                cmd = inp.get("cmd", inp.get("command", ""))
                llm_timeout = _coerce_int(inp.get("timeout_s"), None)
                # FIX-5: step-kind × command-shape 查表得到最小 wallclock，
                # max(LLM 选择, floor)。防 agent 给重 step 设过短 timeout（如
                # CoSb3 2026-05-19 dos_nscf 给 120s 反复自砍那种）。
                floor = floor_for_command(
                    cmd, bash_tool.workdir, step_kind_hint=step_kind,
                )
                if llm_timeout is None:
                    timeout = floor
                else:
                    timeout = max(llm_timeout, floor)
                r = bash_tool.run(cmd, timeout_s=timeout)
                stdout = (r.get("stdout") or "")[-8000:]
                stderr = (r.get("stderr") or "")[-4000:]
                extras = ""
                if r.get("blocked"):
                    extras += f"[bash_guard blocked]\n"
                if r.get("lint_diff"):
                    extras += f"--- qe_input_lint ---\n{r['lint_diff']}\n"
                cwd_val = r.get("cwd_after", r.get("cwd", ""))
                tool_results.append({
                    "role": "tool", "tool_call_id": tc.id,
                    "content": (
                        f"exit_code={r['exit_code']} wall={r['wall_s']:.2f}s cwd={cwd_val}\n"
                        f"{extras}"
                        f"--- stdout ---\n{stdout}\n--- stderr ---\n{stderr}"
                    ),
                })

            elif tc.name == "write_file":
                if "path" not in inp or "content" not in inp:
                    result = {"error": "write_file: missing 'path' or 'content'"}
                else:
                    result = write_file(inp["path"], inp["content"], cwd=bash_tool.cwd)
                tool_results.append({
                    "role": "tool", "tool_call_id": tc.id,
                    "content": json.dumps(result, ensure_ascii=False),
                })

            elif tc.name == "read_file":
                if "path" not in inp:
                    result = {"error": "read_file: missing 'path'"}
                else:
                    result = read_file(
                        inp["path"], cwd=bash_tool.cwd,
                        offset=_coerce_int(inp.get("offset"), 0),
                        limit=_coerce_int(inp.get("limit"), None),
                    )
                tool_results.append({
                    "role": "tool", "tool_call_id": tc.id,
                    "content": json.dumps(result, ensure_ascii=False),
                })

            elif tc.name == "read_experience":
                # Defensive: this tool is only ever pre-seeded as completed
                # history by ExecutorAgent. If the LLM somehow invokes it
                # live, return an empty lesson list rather than erroring.
                tool_results.append({
                    "role": "tool", "tool_call_id": tc.id,
                    "content": json.dumps({"lessons": [], "n": 0}, ensure_ascii=False),
                })

            else:
                tool_results.append({
                    "role": "tool", "tool_call_id": tc.id,
                    "content": f"unknown tool: {tc.name!r}",
                })

        messages.extend(tool_results)
        if submit_payload is not None:
            return submit_payload

    return {"incomplete": True,
            "reason": f"turn budget exhausted (max_turns={max_turns})"}


def _coerce_int(v: Any, default: Any) -> Any:
    """LLM 偶尔传非干净 int（如 '180,' / float string）；宽松转换，失败用 default。"""
    if v is None:
        return default
    if isinstance(v, int):
        return v
    if isinstance(v, float):
        return int(v)
    s = str(v).strip().rstrip(",").strip()
    if not s:
        return default
    try:
        return int(s)
    except ValueError:
        try:
            return int(float(s))
        except ValueError:
            return default


def _serialize_response(resp: NormalizedResponse, llm_wall_ms: Any = None) -> dict[str, Any]:
    """NormalizedResponse → transcript 单 turn dict（Anthropic-flavor shape）。"""
    blocks: list[dict[str, Any]] = []
    if resp.text:
        blocks.append({"type": "text", "text": resp.text})
    for tc in resp.tool_calls:
        blocks.append({
            "type": "tool_use",
            "id": tc.id,
            "name": tc.name,
            "input": tc.input,
        })
    out: dict[str, Any] = {
        "stop_reason": resp.stop_reason,
        "content": blocks,
        "usage": resp.usage,
    }
    if resp.reasoning_content:
        out["reasoning_content"] = resp.reasoning_content
    if llm_wall_ms is not None:
        out["llm_wall_ms"] = llm_wall_ms
    return out


def _append_transcript(path: Path, entry: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        existing = json.loads(path.read_text(encoding="utf-8"))
    else:
        existing = []
    existing.append(entry)
    path.write_text(json.dumps(existing, indent=2, ensure_ascii=False), encoding="utf-8")
