"""FIX-25 (2026-05-25): per-loop wall-clock budget cap.

`run_loop(max_wall_s=N)` adds a soft + hard exit gate on elapsed time:
  - First turn where `elapsed > max_wall_s` AND no warning yet → inject a
    "submit incomplete now" user message; continue (give LLM 1 grace turn)
  - Next turn still exceeded AND warning already injected → hard-exit with
    `{"incomplete": True, "reason": "wall-time budget exhausted (...)"}`

Used by vc_relax preflight subagent to cap itself at ≤ 50% of harness wall
so the main loop has guaranteed budget for SCF + bands + DOS.

Legacy callers (max_wall_s=None / not passed) get unchanged behavior.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from agent.loop import run_loop


@dataclass
class _FakeResp:
    text: str = ""
    tool_calls: list = field(default_factory=list)
    reasoning_content: str | None = None
    stop_reason: str = "stop"
    usage: dict = field(default_factory=lambda: {"input_tokens": 1, "output_tokens": 1})


@dataclass
class _FakeToolCall:
    id: str
    name: str
    input: dict


class _FakeClient:
    def __init__(self, responses):
        self._iter = iter(responses)

    def call(self, *args, **kwargs):
        return next(self._iter)


def test_max_wall_s_none_legacy_behavior_unchanged():
    """Backward-compat: max_wall_s=None (or unspecified) → no budget check,
    loop behavior identical to pre-FIX-25."""
    client = _FakeClient([
        _FakeResp(text="hi", tool_calls=[], reasoning_content=None),
    ])
    messages: list = []
    result = run_loop(
        client=client, messages=messages,
        user_prompt="task", system_prompt="sys",
        max_turns=1, max_output_tokens=100, bash_tool=None,
        max_wall_s=None,  # explicit None
    )
    assert result.get("incomplete") is True
    assert "end_turn" in result.get("reason", "")
    # Critically: no warning message injected
    user_msgs = [m for m in messages if m.get("role") == "user"]
    assert all("wall-time budget" not in m["content"] for m in user_msgs)


def test_max_wall_s_default_unset_legacy_behavior():
    """Same as above but max_wall_s not passed at all (uses default None)."""
    client = _FakeClient([
        _FakeResp(text="hi", tool_calls=[], reasoning_content=None),
    ])
    messages: list = []
    result = run_loop(
        client=client, messages=messages,
        user_prompt="task", system_prompt="sys",
        max_turns=1, max_output_tokens=100, bash_tool=None,
    )
    assert result.get("incomplete") is True


def test_max_wall_s_negative_always_exceeded_injects_warning_then_exits():
    """max_wall_s=-1 means elapsed > budget on EVERY turn. The first turn
    injects a warning user message; the second turn (with warning already
    injected) hard-exits incomplete."""
    # LLM returns two tool-less responses; the loop would normally just
    # incomplete-exit after the first ("end_turn without submit"). But our
    # budget logic should fire BEFORE the second iteration.
    client = _FakeClient([
        # Turn 0 — budget check fires, warning injected before LLM call.
        # LLM (fake) returns text-only, loop would exit "end_turn".
        _FakeResp(text="ok i'll keep going", tool_calls=[], reasoning_content=None),
    ])
    messages: list = []
    result = run_loop(
        client=client, messages=messages,
        user_prompt="task", system_prompt="sys",
        max_turns=5, max_output_tokens=100, bash_tool=None,
        max_wall_s=-1,  # always exceeded
    )
    # The first turn: budget exceeded → warning injected, LLM called (1 fake
    # response consumed), LLM returned no tool_calls → loop's existing
    # "end_turn without submit" exit. Note: the budget hard-exit doesn't
    # fire because the legacy exit gets there first when no tool_calls.
    assert result.get("incomplete") is True
    # The warning user message should be in messages
    user_msgs = [m for m in messages if m.get("role") == "user"
                 and "wall-time budget" in m.get("content", "")]
    assert len(user_msgs) == 1
    warning = user_msgs[0]["content"]
    assert "exhausted" in warning
    assert "submit" in warning
    assert "incomplete=True" in warning


def test_max_wall_s_warning_then_hard_exit_on_continued_tool_calls():
    """Turn 0 budget warning injected → LLM ignores, calls bash again →
    Turn 1 budget hard-exits with 'warning was injected but did not submit'."""
    from unittest.mock import MagicMock

    # Turn 0: LLM calls a tool (not submit). Turn 1 won't even start — budget
    # hard-exit fires at the top of turn 1 before client.call.
    tc0 = _FakeToolCall(id="t0", name="bash", input={"cmd": "echo hi"})
    client = _FakeClient([
        _FakeResp(text="", tool_calls=[tc0], reasoning_content=None),
    ])

    fake_bash = MagicMock()
    fake_bash.run = MagicMock(return_value={
        "stdout": "hi", "stderr": "", "exit_code": 0,
        "wall_s": 0.01, "cwd_after": "/tmp", "lint_diff": "",
    })
    fake_bash.workdir = "/tmp"

    messages: list = []
    result = run_loop(
        client=client, messages=messages,
        user_prompt="task", system_prompt="sys",
        max_turns=5, max_output_tokens=100, bash_tool=fake_bash,
        max_wall_s=-1,
    )
    # Hard exit with budget-exhausted reason
    assert result.get("incomplete") is True
    assert "budget exhausted" in result.get("reason", "")
    assert "warning was injected but model did not submit" in result.get("reason", "")
    # Warning message is in messages
    user_msgs = [m for m in messages if m.get("role") == "user"
                 and "wall-time budget" in m.get("content", "")]
    assert len(user_msgs) == 1


def test_max_wall_s_warning_then_graceful_submit_succeeds():
    """Turn 0 budget warning injected → LLM submits incomplete → loop
    returns the submit payload, NOT the budget hard-exit reason."""
    tc_submit = _FakeToolCall(
        id="t0", name="submit",
        input={"incomplete": True, "reason": "vc_relax wall budget exhausted"},
    )
    client = _FakeClient([
        _FakeResp(text="", tool_calls=[tc_submit], reasoning_content=None),
    ])
    messages: list = []
    result = run_loop(
        client=client, messages=messages,
        user_prompt="task", system_prompt="sys",
        max_turns=5, max_output_tokens=100, bash_tool=None,
        max_wall_s=-1,
    )
    # Returns the LLM's submit payload (graceful exit per FIX-25 design)
    assert result.get("incomplete") is True
    assert "vc_relax wall budget" in result.get("reason", "")
    # The warning was injected
    user_msgs = [m for m in messages if m.get("role") == "user"
                 and "wall-time budget" in m.get("content", "")]
    assert len(user_msgs) == 1
