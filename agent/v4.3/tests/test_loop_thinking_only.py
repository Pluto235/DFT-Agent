"""FIX-16: defend against deepseek thinking-only responses.

When deepseek-v4-pro returns stop_reason='stop' with content="" (no visible
text) and tool_calls=[] (no action) but reasoning_content set, agent/loop.py
constructs an assistant message. Pre-FIX-16, that message had content=None
and no tool_calls — both are required by OpenAI/deepseek API to be set on
the next call. The malformed message persisted in the in/out messages list
and triggered 400 "Invalid assistant message" on the next executor.resume().

Two-layer defense:
  (a) `content: resp.text or ""` so content is at least empty string
  (b) Entry guard: if both content=="" AND no tool_calls, fill content with
      an explicit "(internal: thinking-only step, ...)" placeholder

Affects deepseek/openai-compat only; Claude never sets reasoning_content on
this code path so the bug is invisible there.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

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
    """Returns a pre-set sequence of NormalizedResponse-like objects."""
    def __init__(self, responses):
        self._iter = iter(responses)

    def call(self, *args, **kwargs):
        return next(self._iter)


def test_thinking_only_response_fills_placeholder(tmp_path):
    """deepseek thinking-only return → placeholder in content, loop exits."""
    client = _FakeClient([
        _FakeResp(text="", tool_calls=[], reasoning_content="thinking..."),
    ])
    messages: list[dict[str, Any]] = []
    result = run_loop(
        client=client,
        messages=messages,
        user_prompt="hello",
        system_prompt="sys",
        max_turns=1,
        max_output_tokens=100,
        bash_tool=None,
    )
    # Loop should have returned "incomplete" since no tool_calls
    assert result.get("incomplete") is True
    # And messages should now contain user + assistant
    assert len(messages) == 2
    asst = messages[1]
    assert asst["role"] == "assistant"
    # Critical: content MUST be non-empty so the next API call (e.g. resume)
    # doesn't 400 with "Invalid assistant message".
    assert asst["content"], f"empty content remained: {asst}"
    assert "thinking-only" in asst["content"]
    assert "tool_calls" not in asst


def test_normal_text_response_unchanged(tmp_path):
    """LLM returned visible text → that text becomes content (not placeholder)."""
    client = _FakeClient([
        _FakeResp(text="hello world", tool_calls=[], reasoning_content=None),
    ])
    messages = []
    result = run_loop(
        client=client, messages=messages,
        user_prompt="hi", system_prompt="sys",
        max_turns=1, max_output_tokens=100, bash_tool=None,
    )
    assert result.get("incomplete") is True
    asst = messages[1]
    assert asst["content"] == "hello world"
    assert "thinking-only" not in asst["content"]


def test_tool_call_with_empty_text_keeps_empty_content(tmp_path):
    """Tool call present + no text → content="" allowed (API accepts that)."""
    tc = _FakeToolCall(id="t1", name="submit", input={"ok": True})
    client = _FakeClient([
        _FakeResp(text="", tool_calls=[tc], reasoning_content=None),
    ])
    messages = []
    _ = run_loop(
        client=client, messages=messages,
        user_prompt="go", system_prompt="sys",
        max_turns=1, max_output_tokens=100, bash_tool=None,
    )
    asst = messages[1]
    # Content stays empty string (FIX-16 `or ""`), tool_calls present.
    assert asst["content"] == ""
    assert "tool_calls" in asst
    assert len(asst["tool_calls"]) == 1
    # Placeholder MUST NOT be inserted when tool_calls present.
    assert "thinking-only" not in (asst["content"] or "")


def test_resume_path_messages_are_api_clean(tmp_path):
    """Simulate the LiAlSi2O6 scenario: first call returns thinking-only,
    messages get appended, then a hypothetical second call (resume) would
    re-send messages. The first appended assistant message must be API-valid
    (content non-empty when no tool_calls)."""
    client = _FakeClient([
        _FakeResp(text="", tool_calls=[], reasoning_content="deep thoughts"),
    ])
    messages = []
    _ = run_loop(
        client=client, messages=messages,
        user_prompt="task", system_prompt="sys",
        max_turns=1, max_output_tokens=100, bash_tool=None,
    )
    # Now `messages` would be the input to executor.resume(). Verify each
    # assistant message satisfies the OpenAI invariant: content OR tool_calls.
    for m in messages:
        if m.get("role") == "assistant":
            has_content = bool(m.get("content"))
            has_tools = bool(m.get("tool_calls"))
            assert has_content or has_tools, \
                f"assistant message violates OpenAI invariant: {m}"
