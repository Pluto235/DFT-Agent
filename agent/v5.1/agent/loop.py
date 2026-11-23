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
]


def run_loop(
    *,
    client: LLMClient,
    system_prompt: str,
    user_prompt: str,
    bash_tool: BashTool,
    max_turns: int,
    max_output_tokens: int = 8192,
    transcript_path: Path | None = None,
) -> dict:
    """Run the LLM tool-use loop. Returns the submit payload (dict) on the
    first `submit` tool call, or {"incomplete": True, "reason": "..."} if
    we exhaust max_turns."""
    # OpenAI Chat Completions shape (canonical)
    messages: list[dict[str, Any]] = [
        {"role": "user", "content": user_prompt}
    ]

    for turn in range(max_turns):
        t0 = time.monotonic()
        resp: NormalizedResponse = client.call(
            messages=messages,
            system=system_prompt,
            tools=TOOLS,
            max_output_tokens=max_output_tokens,
        )
        llm_wall_ms = (time.monotonic() - t0) * 1000.0

        if transcript_path:
            _append_transcript(transcript_path, _serialize_response(resp, llm_wall_ms=llm_wall_ms))

        # Assistant message (OpenAI shape)
        asst_msg: dict[str, Any] = {
            "role": "assistant",
            "content": resp.text or None,
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
                timeout = _coerce_int(inp.get("timeout_s"), None)
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
