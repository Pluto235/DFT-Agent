"""LLMClient 测试 — provider 路由 / message+response 转换 / retry。"""
import json
from unittest.mock import MagicMock, patch

import pytest

from agent.llm import (
    LLMClient,
    NormalizedResponse,
    _openai_to_anthropic_messages,
    _tools_to_anthropic,
    _tools_to_openai,
    _anthropic_response_to_normalized,
    _openai_response_to_normalized,
)
from agent.provider_config import get_default_registry


# ─────────────────────── provider detection (via default registry) ──

@pytest.mark.parametrize("model,expected", [
    ("claude-sonnet-4-6", "anthropic"),
    ("claude-haiku-4-5", "anthropic"),
    ("gpt-4o", "openai"),
    ("gpt-4o-mini", "openai"),
    ("o1-preview", "openai"),
    ("o3-mini", "openai"),
    ("gemini-2.5-pro", "google"),
    ("gemini-2.0-flash", "google"),
    ("deepseek-reasoner", "deepseek"),
    ("deepseek-chat", "deepseek"),
    ("kimi-latest", "moonshot"),
    ("kimi-k1.5-preview", "moonshot"),
    ("moonshot-v1-32k", "moonshot"),
    ("grok-2-latest", "xai"),
    ("mistral-large-latest", "mistral"),
    ("codestral-latest", "mistral"),
    ("ollama/llama3.3:70b", "ollama"),
    ("meta-llama/llama-3.3-70b-instruct", "openrouter"),
    ("qwen/qwen-2.5-72b-instruct", "openrouter"),
])
def test_detect_provider(model, expected):
    """Bundled providers.yaml 的路由检测。"""
    assert get_default_registry().detect(model) == expected


def test_detect_provider_unknown_raises():
    with pytest.raises(ValueError, match="cannot detect provider"):
        get_default_registry().detect("unknown-model-name")


# ─────────────────────── tool spec converters ────────────────────────

def test_tools_to_anthropic_shape():
    generic = [{
        "name": "bash",
        "description": "exec",
        "parameters": {"type": "object", "properties": {"cmd": {"type": "string"}}, "required": ["cmd"]},
    }]
    out = _tools_to_anthropic(generic)
    assert out == [{
        "name": "bash",
        "description": "exec",
        "input_schema": {"type": "object", "properties": {"cmd": {"type": "string"}}, "required": ["cmd"]},
    }]


def test_tools_to_openai_shape():
    generic = [{
        "name": "bash",
        "description": "exec",
        "parameters": {"type": "object", "properties": {"cmd": {"type": "string"}}, "required": ["cmd"]},
    }]
    out = _tools_to_openai(generic)
    assert out == [{
        "type": "function",
        "function": {
            "name": "bash",
            "description": "exec",
            "parameters": {"type": "object", "properties": {"cmd": {"type": "string"}}, "required": ["cmd"]},
        },
    }]


# ─────────────────────── message converters ──────────────────────────

def test_openai_to_anthropic_simple_user():
    out = _openai_to_anthropic_messages([{"role": "user", "content": "hi"}])
    assert out == [{"role": "user", "content": "hi"}]


def test_openai_to_anthropic_assistant_with_tool_call():
    msgs = [
        {"role": "user", "content": "do x"},
        {
            "role": "assistant",
            "content": "running",
            "tool_calls": [{
                "id": "t1",
                "type": "function",
                "function": {"name": "bash", "arguments": json.dumps({"cmd": "ls"})},
            }],
        },
        {"role": "tool", "tool_call_id": "t1", "content": '{"stdout": "a"}'},
    ]
    out = _openai_to_anthropic_messages(msgs)
    assert out[0] == {"role": "user", "content": "do x"}
    # assistant: text + tool_use blocks
    assert out[1]["role"] == "assistant"
    blocks = out[1]["content"]
    assert blocks[0] == {"type": "text", "text": "running"}
    assert blocks[1] == {"type": "tool_use", "id": "t1", "name": "bash", "input": {"cmd": "ls"}}
    # tool 消息 → user role with tool_result block
    assert out[2]["role"] == "user"
    assert out[2]["content"] == [{"type": "tool_result", "tool_use_id": "t1", "content": '{"stdout": "a"}'}]


def test_openai_to_anthropic_merges_parallel_tool_results():
    """OpenAI 多个连续 tool 消息要合并为 1 个 Anthropic user 消息(API 要求 user/assistant 严格交替)。"""
    msgs = [
        {"role": "user", "content": "do"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {"id": "t1", "type": "function", "function": {"name": "bash", "arguments": "{}"}},
                {"id": "t2", "type": "function", "function": {"name": "bash", "arguments": "{}"}},
            ],
        },
        {"role": "tool", "tool_call_id": "t1", "content": "r1"},
        {"role": "tool", "tool_call_id": "t2", "content": "r2"},
    ]
    out = _openai_to_anthropic_messages(msgs)
    # 末尾应只有 1 个 user 消息含 2 个 tool_result block
    assert out[-1]["role"] == "user"
    assert len(out[-1]["content"]) == 2
    assert out[-1]["content"][0]["tool_use_id"] == "t1"
    assert out[-1]["content"][1]["tool_use_id"] == "t2"
    # 总长 user/assistant/user(merged) = 3
    assert len(out) == 3


def test_openai_to_anthropic_assistant_only_tool_calls_no_text():
    msgs = [
        {"role": "user", "content": "x"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [{"id": "t1", "type": "function",
                           "function": {"name": "bash", "arguments": "{}"}}],
        },
    ]
    out = _openai_to_anthropic_messages(msgs)
    # text 块不出现(content=None);只 tool_use
    assert out[1]["content"] == [
        {"type": "tool_use", "id": "t1", "name": "bash", "input": {}}
    ]


# ─────────────────────── response normalizers ────────────────────────

def test_anthropic_response_normalize():
    text_b = MagicMock(); text_b.type = "text"; text_b.text = "thinking"
    tu_b = MagicMock(); tu_b.type = "tool_use"; tu_b.id = "t1"; tu_b.name = "bash"
    tu_b.input = {"cmd": "ls"}
    resp = MagicMock()
    resp.content = [text_b, tu_b]
    resp.stop_reason = "tool_use"
    resp.usage = MagicMock(
        input_tokens=100, output_tokens=20,
        cache_read_input_tokens=80, cache_creation_input_tokens=15,
    )
    norm = _anthropic_response_to_normalized(resp)
    assert norm.text == "thinking"
    assert len(norm.tool_calls) == 1
    assert norm.tool_calls[0].id == "t1"
    assert norm.tool_calls[0].name == "bash"
    assert norm.tool_calls[0].input == {"cmd": "ls"}
    assert norm.stop_reason == "tool_use"
    assert norm.usage == {
        "input_tokens": 100, "output_tokens": 20,
        "cache_hit_tokens": 80, "cache_miss_tokens": 15,
    }


def test_openai_response_normalize():
    tc = MagicMock()
    tc.id = "call_1"
    tc.function = MagicMock()
    tc.function.name = "bash"
    tc.function.arguments = json.dumps({"cmd": "ls"})

    msg = MagicMock()
    msg.content = "thinking"
    msg.tool_calls = [tc]
    choice = MagicMock(); choice.message = msg; choice.finish_reason = "tool_calls"
    resp = MagicMock()
    resp.choices = [choice]
    resp.usage = MagicMock(
        prompt_tokens=100, completion_tokens=20,
        prompt_cache_hit_tokens=70, prompt_cache_miss_tokens=30,
    )
    norm = _openai_response_to_normalized(resp)
    assert norm.text == "thinking"
    assert norm.tool_calls[0].id == "call_1"
    assert norm.tool_calls[0].input == {"cmd": "ls"}
    assert norm.stop_reason == "tool_calls"
    assert norm.usage == {
        "input_tokens": 100, "output_tokens": 20,
        "cache_hit_tokens": 70, "cache_miss_tokens": 30,
    }


def test_openai_response_with_reasoning_content():
    """DeepSeek v4 / o1 等 reasoning model:reasoning_content 字段必须捕获。"""
    msg = MagicMock()
    msg.content = ""
    msg.tool_calls = None
    msg.reasoning_content = "Let me think step by step..."
    choice = MagicMock(); choice.message = msg; choice.finish_reason = "stop"
    resp = MagicMock(); resp.choices = [choice]
    resp.usage = MagicMock(prompt_tokens=10, completion_tokens=50)
    norm = _openai_response_to_normalized(resp)
    assert norm.reasoning_content == "Let me think step by step..."


def test_openai_response_no_reasoning_content():
    """普通 model:无 reasoning_content 字段时 normalize 应给 None。"""
    msg = MagicMock(); msg.content = "hi"; msg.tool_calls = None
    msg.reasoning_content = None  # 也兼容字段直接缺失
    choice = MagicMock(); choice.message = msg; choice.finish_reason = "stop"
    resp = MagicMock(); resp.choices = [choice]; resp.usage = None
    norm = _openai_response_to_normalized(resp)
    assert norm.reasoning_content is None


def test_openai_response_normalize_invalid_args_json():
    """模型偶尔吐 invalid JSON arguments;normalize 不崩,留 _raw。"""
    tc = MagicMock()
    tc.id = "c1"; tc.function = MagicMock()
    tc.function.name = "bash"
    tc.function.arguments = "{not valid json}"

    msg = MagicMock(); msg.content = None; msg.tool_calls = [tc]
    choice = MagicMock(); choice.message = msg; choice.finish_reason = "tool_calls"
    resp = MagicMock(); resp.choices = [choice]; resp.usage = None
    norm = _openai_response_to_normalized(resp)
    assert norm.tool_calls[0].input == {"_raw": "{not valid json}"}


def test_openai_response_no_tool_calls():
    """OpenAI 响应只有 text(stop)。"""
    msg = MagicMock(); msg.content = "answer"; msg.tool_calls = None
    choice = MagicMock(); choice.message = msg; choice.finish_reason = "stop"
    resp = MagicMock(); resp.choices = [choice]
    resp.usage = MagicMock(prompt_tokens=10, completion_tokens=5)
    norm = _openai_response_to_normalized(resp)
    assert norm.text == "answer"
    assert norm.tool_calls == []


# ─────────────────────── LLMClient call paths ────────────────────────

def _mock_anthropic_resp(text="ok", tool_use=None, stop="end_turn"):
    blocks = []
    if text:
        b = MagicMock(); b.type = "text"; b.text = text
        blocks.append(b)
    if tool_use:
        b = MagicMock(); b.type = "tool_use"
        b.id = tool_use["id"]; b.name = tool_use["name"]; b.input = tool_use["input"]
        blocks.append(b)
    r = MagicMock()
    r.content = blocks; r.stop_reason = stop
    r.usage = MagicMock(input_tokens=10, output_tokens=5)
    return r


@patch("anthropic.Anthropic")
def test_llmclient_anthropic_basic(mock_cls):
    mock_client = MagicMock()
    mock_client.messages.create.return_value = _mock_anthropic_resp(text="hi")
    mock_cls.return_value = mock_client

    client = LLMClient("claude-sonnet-4-6")
    norm = client.call(
        messages=[{"role": "user", "content": "hi"}],
        system="sys",
        tools=[],
        max_output_tokens=100,
    )
    assert isinstance(norm, NormalizedResponse)
    assert norm.text == "hi"
    call_kwargs = mock_client.messages.create.call_args.kwargs
    assert call_kwargs["model"] == "claude-sonnet-4-6"
    assert call_kwargs["system"] == "sys"
    assert call_kwargs["messages"] == [{"role": "user", "content": "hi"}]


def _mock_openai_resp(text="ok", tool_calls=None, finish="stop"):
    tcs = []
    for tc_d in tool_calls or []:
        tc = MagicMock()
        tc.id = tc_d["id"]
        tc.function = MagicMock()
        tc.function.name = tc_d["name"]
        tc.function.arguments = json.dumps(tc_d["input"])
        tcs.append(tc)
    msg = MagicMock(); msg.content = text; msg.tool_calls = tcs or None
    choice = MagicMock(); choice.message = msg; choice.finish_reason = finish
    resp = MagicMock(); resp.choices = [choice]
    resp.usage = MagicMock(prompt_tokens=10, completion_tokens=5)
    return resp


@patch.dict("os.environ", {"OPENAI_API_KEY": "test-key"})
@patch("openai.OpenAI")
def test_llmclient_openai_basic(mock_cls):
    mock_client = MagicMock()
    mock_client.chat.completions.create.return_value = _mock_openai_resp(text="hi")
    mock_cls.return_value = mock_client

    client = LLMClient("gpt-4o-mini")
    norm = client.call(
        messages=[{"role": "user", "content": "hi"}],
        system="sys",
        tools=[],
        max_output_tokens=100,
    )
    assert isinstance(norm, NormalizedResponse)
    assert norm.text == "hi"
    call_kwargs = mock_client.chat.completions.create.call_args.kwargs
    assert call_kwargs["model"] == "gpt-4o-mini"
    # system 应作为 first message 注入
    msgs = call_kwargs["messages"]
    assert msgs[0] == {"role": "system", "content": "sys"}
    assert msgs[1] == {"role": "user", "content": "hi"}


@patch.dict("os.environ", {"DEEPSEEK_API_KEY": "test-key"})
@patch("openai.OpenAI")
def test_llmclient_deepseek_uses_base_url(mock_cls):
    mock_client = MagicMock()
    mock_client.chat.completions.create.return_value = _mock_openai_resp()
    mock_cls.return_value = mock_client

    LLMClient("deepseek-chat")
    init_kwargs = mock_cls.call_args.kwargs
    assert init_kwargs["base_url"] == "https://api.deepseek.com/v1"
    assert init_kwargs["api_key"] == "test-key"


@patch.dict("os.environ", {}, clear=False)
def test_llmclient_missing_key_raises():
    # 故意清掉所有可能的 key
    import os
    for k in ("OPENAI_API_KEY", "DEEPSEEK_API_KEY"):
        os.environ.pop(k, None)
    with pytest.raises(RuntimeError, match="DEEPSEEK_API_KEY"):
        LLMClient("deepseek-chat")


# ─────────────────────── retry on 5xx / rate limit ───────────────────

@patch("anthropic.Anthropic")
def test_retry_on_anthropic_5xx(mock_cls):
    import anthropic as anth
    mock_client = MagicMock()
    err = anth.APIStatusError(message="500", response=MagicMock(status_code=500), body=None)
    mock_client.messages.create.side_effect = [err, err, _mock_anthropic_resp(text="final")]
    mock_cls.return_value = mock_client

    client = LLMClient("claude-sonnet-4-6", retry_base_delay_s=0.0)
    norm = client.call(messages=[{"role": "user", "content": "x"}],
                       system="s", tools=[], max_output_tokens=100)
    assert norm.text == "final"
    assert mock_client.messages.create.call_count == 3


@patch("anthropic.Anthropic")
def test_retry_gives_up_after_max(mock_cls):
    import anthropic as anth
    mock_client = MagicMock()
    err = anth.APIStatusError(message="500", response=MagicMock(status_code=500), body=None)
    mock_client.messages.create.side_effect = [err, err, err, err]
    mock_cls.return_value = mock_client

    client = LLMClient("claude-sonnet-4-6", retry_base_delay_s=0.0)
    with pytest.raises(anth.APIStatusError):
        client.call(messages=[{"role": "user", "content": "x"}],
                    system="s", tools=[], max_output_tokens=100)
    assert mock_client.messages.create.call_count == 3


@patch.dict("os.environ", {"OPENAI_API_KEY": "test"})
@patch("openai.OpenAI")
def test_retry_on_openai_5xx(mock_cls):
    """openai SDK 的 InternalServerError 也应触发重试。"""
    mock_client = MagicMock()
    err = type("InternalServerError", (Exception,), {"status_code": 503})("svc unavail")
    mock_client.chat.completions.create.side_effect = [
        err, _mock_openai_resp(text="final"),
    ]
    mock_cls.return_value = mock_client

    client = LLMClient("gpt-4o-mini", retry_base_delay_s=0.0)
    norm = client.call(messages=[{"role": "user", "content": "x"}],
                       system="s", tools=[], max_output_tokens=100)
    assert norm.text == "final"
    assert mock_client.chat.completions.create.call_count == 2


def test_explicit_provider_override():
    """--provider 参数覆盖自动检测(模糊 model id 用)。"""
    with patch("anthropic.Anthropic"):
        # 强制走 anthropic 路径(虽然 model id 不像 claude)
        client = LLMClient("custom-fine-tune-name", provider="anthropic")
        assert client.provider == "anthropic"
