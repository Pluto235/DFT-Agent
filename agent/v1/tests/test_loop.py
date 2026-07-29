"""Loop 测试 — 用 NormalizedResponse 对 llm.call() 做 mock,不接触 provider native。"""
import json
from pathlib import Path
from unittest.mock import MagicMock

from agent.llm import NormalizedResponse, NormalizedToolCall
from agent.loop import run_agent_loop, _dispatch_tool


def _resp(text="", tool_calls=None, stop="tool_calls", reasoning_content=None):
    return NormalizedResponse(
        text=text,
        tool_calls=tool_calls or [],
        stop_reason=stop,
        usage={"input_tokens": 10, "output_tokens": 5},
        reasoning_content=reasoning_content,
    )


def _tc(name, inp, tc_id="tu_1"):
    return NormalizedToolCall(id=tc_id, name=name, input=inp)


def test_immediate_submit(tmp_path):
    """Agent 第一轮就调 submit → result.json 写出,exit=0。"""
    (tmp_path / "task_prompt.txt").write_text("do the thing")
    out_path = tmp_path / "result.json"

    mock_llm = MagicMock()
    mock_llm.call.return_value = _resp(
        text="done",
        tool_calls=[_tc("submit", {"task_outputs": {"k": "v"}, "notes": "ok"})],
    )

    exit_code = run_agent_loop(
        llm=mock_llm,
        system_prompt="sys",
        task_prompt_path=tmp_path / "task_prompt.txt",
        workdir=tmp_path,
        out_path=out_path,
        max_turns=5,
        max_output_tokens=100,
    )
    assert exit_code == 0
    data = json.loads(out_path.read_text())
    assert data["task_outputs"] == {"k": "v"}


def test_bash_then_submit(tmp_path):
    """第一轮 bash → 第二轮 submit。"""
    (tmp_path / "task_prompt.txt").write_text("x")
    out_path = tmp_path / "result.json"

    mock_llm = MagicMock()
    mock_llm.call.side_effect = [
        _resp(tool_calls=[_tc("bash", {"cmd": "echo hi"}, "tu_1")]),
        _resp(tool_calls=[_tc("submit", {"task_outputs": {}, "notes": ""}, "tu_2")]),
    ]

    exit_code = run_agent_loop(
        llm=mock_llm, system_prompt="s",
        task_prompt_path=tmp_path / "task_prompt.txt",
        workdir=tmp_path, out_path=out_path,
        max_turns=5, max_output_tokens=100,
    )
    assert exit_code == 0
    assert (tmp_path / "bash.log").exists()
    bash_log = (tmp_path / "bash.log").read_text()
    assert "echo hi" in bash_log


def test_turn_budget_exhausted(tmp_path):
    """永不 submit → exit=2,result.json 标 incomplete。"""
    (tmp_path / "task_prompt.txt").write_text("x")
    out_path = tmp_path / "result.json"

    mock_llm = MagicMock()
    mock_llm.call.return_value = _resp(
        tool_calls=[_tc("bash", {"cmd": "echo a"}, "tu_1")],
    )

    exit_code = run_agent_loop(
        llm=mock_llm, system_prompt="s",
        task_prompt_path=tmp_path / "task_prompt.txt",
        workdir=tmp_path, out_path=out_path,
        max_turns=3, max_output_tokens=100,
    )
    assert exit_code == 2
    data = json.loads(out_path.read_text())
    assert data.get("incomplete") is True
    assert "turn_budget" in data.get("reason", "")


def test_assistant_no_tools_early_exit(tmp_path):
    """Assistant 既不 submit 也不调工具 → 早退 exit=2。"""
    (tmp_path / "task_prompt.txt").write_text("x")
    out_path = tmp_path / "result.json"

    mock_llm = MagicMock()
    mock_llm.call.return_value = _resp(text="I give up", tool_calls=[], stop="stop")

    exit_code = run_agent_loop(
        llm=mock_llm, system_prompt="s",
        task_prompt_path=tmp_path / "task_prompt.txt",
        workdir=tmp_path, out_path=out_path,
        max_turns=5, max_output_tokens=100,
    )
    assert exit_code == 2
    data = json.loads(out_path.read_text())
    assert data.get("reason") == "assistant_ended_without_submit"


def test_transcript_flushed_every_turn(tmp_path):
    """transcript.json 每轮都写 + shape Anthropic-flavor (backend adapter 契约)。"""
    (tmp_path / "task_prompt.txt").write_text("x")
    out_path = tmp_path / "result.json"

    def side_effect(*args, **kwargs):
        return _resp(text="ok", tool_calls=[
            _tc("submit", {"task_outputs": {}, "notes": ""}),
        ])

    mock_llm = MagicMock()
    mock_llm.call.side_effect = side_effect

    run_agent_loop(
        llm=mock_llm, system_prompt="s",
        task_prompt_path=tmp_path / "task_prompt.txt",
        workdir=tmp_path, out_path=out_path,
        max_turns=3, max_output_tokens=100,
    )

    tpath = tmp_path / "transcript.json"
    assert tpath.exists()
    raw = json.loads(tpath.read_text())
    assert isinstance(raw, list) and len(raw) >= 1
    turn0 = raw[0]
    # 后端 transcript_adapter 期望 shape
    assert "stop_reason" in turn0
    assert "content" in turn0 and isinstance(turn0["content"], list)
    assert "usage" in turn0
    types = {b.get("type") for b in turn0["content"]}
    assert "text" in types or "tool_use" in types
    # 含 submit tool_use block
    submit_blocks = [b for b in turn0["content"]
                     if b.get("type") == "tool_use" and b.get("name") == "submit"]
    assert len(submit_blocks) == 1
    assert "input" in submit_blocks[0]
    # per-turn LLM wall:主循环层测,与 provider 解耦;非负 float
    assert "llm_wall_ms" in turn0
    assert isinstance(turn0["llm_wall_ms"], (int, float))
    assert turn0["llm_wall_ms"] >= 0


def test_reasoning_content_round_trip(tmp_path):
    """DeepSeek v4 / o1:reasoning_content 必须在下一轮 assistant 消息里回传(API 强制)。"""
    (tmp_path / "task_prompt.txt").write_text("x")
    out_path = tmp_path / "result.json"

    captured: list[list[dict]] = []

    def side_effect(*, messages, **kwargs):
        captured.append([dict(m) for m in messages])
        if len(captured) == 1:
            return _resp(
                tool_calls=[_tc("bash", {"cmd": "echo hi"}, "tu_1")],
                reasoning_content="thinking about command...",
            )
        return _resp(
            tool_calls=[_tc("submit", {"task_outputs": {}}, "tu_2")],
            reasoning_content="ok done",
        )

    mock_llm = MagicMock()
    mock_llm.call.side_effect = side_effect

    run_agent_loop(
        llm=mock_llm, system_prompt="s",
        task_prompt_path=tmp_path / "task_prompt.txt",
        workdir=tmp_path, out_path=out_path,
        max_turns=5, max_output_tokens=100,
    )

    # 第二轮 messages:assistant 块应带 reasoning_content
    second = captured[1]
    asst = next(m for m in second if m["role"] == "assistant")
    assert asst.get("reasoning_content") == "thinking about command..."

    # transcript 也应保留 reasoning_content
    import json as _json
    raw = _json.loads((tmp_path / "transcript.json").read_text())
    assert raw[0].get("reasoning_content") == "thinking about command..."


def test_messages_use_openai_shape(tmp_path):
    """验:loop 喂给 llm.call 的 messages 是 OpenAI shape(LLMClient 契约)。"""
    (tmp_path / "task_prompt.txt").write_text("hi")
    out_path = tmp_path / "result.json"

    captured: list[list[dict]] = []

    def side_effect(*, messages, **kwargs):
        captured.append([dict(m) for m in messages])
        # 第一轮 bash;第二轮 submit
        if len(captured) == 1:
            return _resp(tool_calls=[_tc("bash", {"cmd": "echo a"}, "t1")])
        return _resp(tool_calls=[_tc("submit", {"task_outputs": {}}, "t2")])

    mock_llm = MagicMock()
    mock_llm.call.side_effect = side_effect

    run_agent_loop(
        llm=mock_llm, system_prompt="s",
        task_prompt_path=tmp_path / "task_prompt.txt",
        workdir=tmp_path, out_path=out_path,
        max_turns=5, max_output_tokens=100,
    )

    # 第一轮:只 user prompt
    assert captured[0][0] == {"role": "user", "content": "hi"}
    # 第二轮:user / assistant(tool_calls) / tool 三段(OpenAI shape)
    second = captured[1]
    assert second[0]["role"] == "user"
    assert second[1]["role"] == "assistant"
    assert "tool_calls" in second[1]
    assert second[1]["tool_calls"][0]["type"] == "function"
    assert second[1]["tool_calls"][0]["function"]["name"] == "bash"
    assert second[2]["role"] == "tool"
    assert second[2]["tool_call_id"] == "t1"


# ============ _dispatch_tool error path: malformed/truncated input ============

def test_dispatch_malformed_raw_fallback(tmp_path):
    """LLMClient 把 truncated JSON 放 _raw 字段 → 返 error 不 raise。"""
    tc = _tc("bash", {"_raw": '{"cmd": "echo hi'})
    result, cwd = _dispatch_tool(tc, tmp_path, bash_log=None)
    assert "error" in result
    assert "malformed" in result["error"].lower()
    assert cwd == tmp_path


def test_dispatch_bash_missing_cmd_returns_error(tmp_path):
    """bash 缺 'cmd' arg → 返 error 不 raise KeyError(原 v1 bug 实证：M1_T2_AlN_bands_dos)。"""
    tc = _tc("bash", {"timeout_s": 60})
    result, cwd = _dispatch_tool(tc, tmp_path, bash_log=None)
    assert "error" in result
    assert "missing required 'cmd'" in result["error"]


def test_dispatch_write_file_missing_args_returns_error(tmp_path):
    tc = _tc("write_file", {"path": "x.txt"})  # 缺 content
    result, _ = _dispatch_tool(tc, tmp_path, bash_log=None)
    assert "error" in result


def test_dispatch_read_file_missing_path_returns_error(tmp_path):
    tc = _tc("read_file", {"offset": 0})
    result, _ = _dispatch_tool(tc, tmp_path, bash_log=None)
    assert "error" in result
    assert "missing required 'path'" in result["error"]
