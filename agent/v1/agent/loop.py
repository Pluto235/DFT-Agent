"""Agent 主循环 — provider-agnostic。

设计:
- messages 全程 OpenAI Chat Completions shape(canonical);LLMClient 内部按
  provider 转 Anthropic native API
- llm.call() 返 NormalizedResponse;loop 不接触 provider native objects
- transcript.json 仍写 Anthropic-flavor dict shape — fresh-repo backend
  `transcript_adapter` 期望 `{stop_reason, content[type=text|tool_use], usage}`,
  不能改

退出:
- exit=0:agent 调 submit
- exit=2:turn budget 耗尽 / assistant 无 tool_use 也未 submit(early end)
"""
import json
import time
from pathlib import Path
from typing import Any

from agent.llm import NormalizedResponse, NormalizedToolCall
from agent.tools.bash import run_bash
from agent.tools.file_io import read_file, write_file
from agent.tools.submit import write_result_json


# Generic tool spec(无 provider 特征)。LLMClient 内部转 Anthropic input_schema /
# OpenAI function format。
TOOLS: list[dict[str, Any]] = [
    {
        "name": "bash",
        "description": (
            "Execute a bash command. Each call is a fresh subprocess. "
            "Bare `cd <dir>` updates the persistent cwd for subsequent calls; "
            "`cd <dir> && ...` does not."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "cmd": {"type": "string", "description": "The command to execute."},
                "timeout_s": {"type": "integer", "default": 900,
                              "description": "Max seconds before kill. Default 900."},
            },
            "required": ["cmd"],
        },
    },
    {
        "name": "write_file",
        "description": "Create or overwrite a file with the given content. Parents auto-created.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path (relative to cwd)."},
                "content": {"type": "string"},
            },
            "required": ["path", "content"],
        },
    },
    {
        "name": "read_file",
        "description": "Read a text file. Optionally slice by line (offset, limit).",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "offset": {"type": "integer", "default": 0,
                           "description": "0-based line offset."},
                "limit": {"type": "integer",
                          "description": "Max lines to return (optional)."},
            },
            "required": ["path"],
        },
    },
    {
        "name": "submit",
        "description": (
            "Terminate with a structured answer. task_outputs is a dict mapping "
            "file-role keys to workdir-relative paths of your QE outputs. "
            "Required keys are listed in task_prompt.txt."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "task_outputs": {
                    "type": "object",
                    "description": "Role → path mapping.",
                    "additionalProperties": {"type": "string"},
                },
                "notes": {"type": "string"},
            },
            "required": ["task_outputs"],
        },
    },
]


def run_agent_loop(
    *,
    llm: Any,
    system_prompt: str,
    task_prompt_path: Path,
    workdir: Path,
    out_path: Path,
    max_turns: int,
    max_output_tokens: int,
) -> int:
    """驱动 agent 循环。返 exit_code。"""
    workdir = Path(workdir)
    out_path = Path(out_path)
    task_prompt = Path(task_prompt_path).read_text(encoding="utf-8")

    # OpenAI-shape canonical
    messages: list[dict[str, Any]] = [
        {"role": "user", "content": task_prompt}
    ]
    cwd = workdir
    transcript_path = workdir / "transcript.json"
    bash_log_path = workdir / "bash.log"

    transcript: list[dict[str, Any]] = []

    with open(bash_log_path, "a", encoding="utf-8") as bash_log:
        for turn in range(max_turns):
            # 主循环层 LLM wall 计时:与 9 provider adapter 内部实现解耦,
            # 一处加全部 provider 受益。计 send → 收完整 response 的墙上时间。
            _llm_t0 = time.monotonic()
            resp: NormalizedResponse = llm.call(
                messages=messages,
                system=system_prompt,
                tools=TOOLS,
                max_output_tokens=max_output_tokens,
            )
            _llm_wall_ms = (time.monotonic() - _llm_t0) * 1000.0

            # transcript 沿用 Anthropic-flavor dict shape(后端 adapter 契约)
            transcript.append(_serialize_response(resp, llm_wall_ms=_llm_wall_ms))
            _write_transcript(transcript_path, transcript)

            # assistant 消息(OpenAI shape)写回历史
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
            # DeepSeek v4 / o1 等 reasoning model 强制要求 reasoning_content 回传
            if resp.reasoning_content:
                asst_msg["reasoning_content"] = resp.reasoning_content
            messages.append(asst_msg)

            tool_results: list[dict[str, Any]] = []
            submitted = False

            for tc in resp.tool_calls:
                if tc.name == "submit":
                    write_result_json(out_path, dict(tc.input))
                    submitted = True
                    break
                result, cwd = _dispatch_tool(tc, cwd, bash_log)
                tool_results.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": json.dumps(result, ensure_ascii=False),
                })

            if submitted:
                return 0

            if not tool_results:
                # assistant 既不 submit 也未调工具 → 早退
                write_result_json(out_path, {
                    "incomplete": True,
                    "reason": "assistant_ended_without_submit",
                })
                return 2

            messages.extend(tool_results)

    # 循环结束仍未 submit
    write_result_json(out_path, {
        "incomplete": True,
        "reason": f"turn_budget_exceeded (max_turns={max_turns})",
    })
    return 2


def _dispatch_tool(tc: NormalizedToolCall, cwd: Path, bash_log) -> tuple[dict, Path]:
    """非 submit 工具 dispatch。返 (tool_result_dict, new_cwd)。

    错误处理:LLM 偶尔返 truncated/malformed tool args → LLMClient fallback 把原 string
    放 `_raw` 字段(没 'cmd' / 'path' 等必需 key)。原代码直接 inp['cmd'] 会 raise KeyError
    crash 整个 solver。改:检测 `_raw` + 必需 key 缺失 → 返 error tool result 让 LLM
    self-recover(典型 v1 fragile-error-handling fix,与 file_io 同款 commit 5fb4d8d 同期)。
    """
    name = tc.name
    inp = dict(tc.input)

    # LLM 返 malformed/truncated JSON 兜底 — LLMClient 把原 string 放 `_raw` 字段
    if "_raw" in inp:
        raw = str(inp.get("_raw", ""))[:200]
        return ({"error": f"tool args malformed (LLM returned non-JSON or truncated): {raw}"}, cwd)

    if name == "bash":
        if "cmd" not in inp:
            return ({"error": "bash tool: missing required 'cmd' arg"}, cwd)
        return run_bash(
            cmd=inp["cmd"],
            cwd=cwd,
            timeout_s=_coerce_int(inp.get("timeout_s"), 900),
            log_file=bash_log,
        )
    if name == "write_file":
        if "path" not in inp or "content" not in inp:
            return ({"error": "write_file tool: missing required 'path' or 'content' arg"}, cwd)
        result = write_file(inp["path"], inp["content"], cwd=cwd)
        return result, cwd
    if name == "read_file":
        if "path" not in inp:
            return ({"error": "read_file tool: missing required 'path' arg"}, cwd)
        result = read_file(
            inp["path"],
            cwd=cwd,
            offset=_coerce_int(inp.get("offset"), 0),
            limit=_coerce_int(inp.get("limit"), None),
        )
        return result, cwd
    return {"error": f"unknown tool: {name}"}, cwd


def _coerce_int(v: Any, default: Any) -> Any:
    """LLM 偶尔传非干净 int（如 '180, ' / '180px' / float string）；strip 后再 int，
    全失败用 default。crash 一次会丢掉整个 run 的 token / wall 投资，宽容一点更稳。
    （v2 0506 commit 0e08283 backport — v1 同款 bug fix。）"""
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
    """NormalizedResponse → transcript 单 turn dict(Anthropic-flavor shape)。

    Backend `transcript_adapter` 要求:
      `{stop_reason: str, content: [{type:'text'|'tool_use', ...}], usage: {...}}`
    可选附加:
    - reasoning_content(reasoning model 才有,backend adapter 忽略)
    - llm_wall_ms(per-turn LLM call 墙上时间,毫秒;主循环层测,与 provider 无关;
      missing/None 落 null 不写字段,backend audit 时区分缺失 vs 0)
    """
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


def _write_transcript(path: Path, transcript: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(transcript, indent=2, ensure_ascii=False), encoding="utf-8")
