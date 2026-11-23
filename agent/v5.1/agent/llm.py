"""LLMClient — 多 provider 统一入口。

设计（替代单一 Anthropic 客户端）:
- 走 native SDK 双路径：`anthropic` 直调 Anthropic，`openai` SDK 配 base_url
  覆盖所有 OpenAI-兼容 provider（DeepSeek / Mistral / xAI / Gemini compat /
  Moonshot / OpenRouter / Ollama 等）
- 返回 `NormalizedResponse`（text + tool_calls + stop_reason + usage）
- 工具用 generic 描述 `{name, description, parameters}`，内部转两种 shape
- Anthropic provider：保留 3 个 prompt cache 断点（system / last tool / 首条 user msg）

Provider 路由通过 agent/providers.yaml 配置（详见该文件）。
认证：优先 providers.yaml key_env → CLAUDE_CODE_OAUTH_TOKEN（Anthropic 专用 fallback）
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from agent.provider_config import (
    ProviderConfig,
    ProviderRegistry,
    get_default_registry,
    load_registry,
)


@dataclass
class NormalizedToolCall:
    id: str
    name: str
    input: dict[str, Any]


@dataclass
class NormalizedResponse:
    """Provider-agnostic 响应。loop 只读这五字段。"""
    text: str
    tool_calls: list[NormalizedToolCall]
    stop_reason: str
    usage: dict[str, Optional[int]]   # {input_tokens, output_tokens, cache_hit_tokens, cache_miss_tokens}
    reasoning_content: Optional[str] = None


# ─────────────────────────── tool spec converters ───────────────────────────

def _tools_to_anthropic(tools: list[dict]) -> list[dict]:
    return [
        {
            "name": t["name"],
            "description": t["description"],
            "input_schema": t["parameters"],
        }
        for t in tools
    ]


def _tools_to_openai(tools: list[dict]) -> list[dict]:
    return [
        {
            "type": "function",
            "function": {
                "name": t["name"],
                "description": t["description"],
                "parameters": t["parameters"],
            },
        }
        for t in tools
    ]


# ─────────────────────────── message shape converters ───────────────────────

def _openai_to_anthropic_messages(msgs: list[dict]) -> list[dict]:
    """OpenAI Chat Completions msgs → Anthropic Messages API msgs。

    多个连续 OpenAI tool 消息合并为 1 个 Anthropic user 消息（API 要求
    user/assistant 严格交替）。
    """
    out: list[dict] = []
    for m in msgs:
        role = m["role"]
        if role == "user":
            out.append({"role": "user", "content": m["content"]})
        elif role == "assistant":
            blocks: list[dict] = []
            text = m.get("content")
            if text:
                blocks.append({"type": "text", "text": text})
            for tc in m.get("tool_calls") or []:
                try:
                    parsed = json.loads(tc["function"]["arguments"])
                except (json.JSONDecodeError, TypeError):
                    parsed = {}
                blocks.append({
                    "type": "tool_use",
                    "id": tc["id"],
                    "name": tc["function"]["name"],
                    "input": parsed,
                })
            out.append({"role": "assistant", "content": blocks})
        elif role == "tool":
            tr = {
                "type": "tool_result",
                "tool_use_id": m["tool_call_id"],
                "content": m["content"],
            }
            if out and out[-1]["role"] == "user" and isinstance(out[-1]["content"], list):
                out[-1]["content"].append(tr)
            else:
                out.append({"role": "user", "content": [tr]})
        else:
            raise ValueError(f"unknown message role: {role}")
    return out


# ─────────────────────────── response normalizers ───────────────────────────

def _anthropic_response_to_normalized(resp: Any) -> NormalizedResponse:
    text_parts: list[str] = []
    tool_calls: list[NormalizedToolCall] = []
    for b in resp.content:
        btype = getattr(b, "type", None)
        if btype == "text":
            text_parts.append(b.text)
        elif btype == "tool_use":
            tool_calls.append(NormalizedToolCall(
                id=b.id, name=b.name, input=dict(b.input),
            ))
    usage_obj = getattr(resp, "usage", None)
    return NormalizedResponse(
        text="\n".join(text_parts),
        tool_calls=tool_calls,
        stop_reason=getattr(resp, "stop_reason", "") or "",
        usage={
            "input_tokens": getattr(usage_obj, "input_tokens", None),
            "output_tokens": getattr(usage_obj, "output_tokens", None),
            "cache_hit_tokens": getattr(usage_obj, "cache_read_input_tokens", None),
            "cache_miss_tokens": getattr(usage_obj, "cache_creation_input_tokens", None),
        },
    )


def _openai_response_to_normalized(resp: Any) -> NormalizedResponse:
    msg = resp.choices[0].message
    text = msg.content or ""
    tool_calls: list[NormalizedToolCall] = []
    for tc in msg.tool_calls or []:
        try:
            parsed = json.loads(tc.function.arguments)
        except (json.JSONDecodeError, TypeError):
            parsed = {"_raw": tc.function.arguments}
        tool_calls.append(NormalizedToolCall(
            id=tc.id, name=tc.function.name, input=parsed,
        ))
    usage_obj = getattr(resp, "usage", None)
    reasoning = getattr(msg, "reasoning_content", None) or None
    return NormalizedResponse(
        text=text,
        tool_calls=tool_calls,
        stop_reason=resp.choices[0].finish_reason or "",
        usage={
            "input_tokens": getattr(usage_obj, "prompt_tokens", None) if usage_obj else None,
            "output_tokens": getattr(usage_obj, "completion_tokens", None) if usage_obj else None,
            "cache_hit_tokens": getattr(usage_obj, "prompt_cache_hit_tokens", None) if usage_obj else None,
            "cache_miss_tokens": getattr(usage_obj, "prompt_cache_miss_tokens", None) if usage_obj else None,
        },
        reasoning_content=reasoning,
    )


# ─────────────────────────── LLMClient ──────────────────────────────────────

class LLMClient:
    """Multi-provider LLM 入口。retry on rate limit / 5xx，指数退避。

    用法:
        client = LLMClient("claude-sonnet-4-6")           # 自动按 providers.yaml 检测
        client = LLMClient("kimi-latest")                 # 路由到 moonshot
        client = LLMClient("gpt-4o", provider="openai")   # 手动覆盖
        client = LLMClient("xxx", config_path=Path(...))  # 自定义配置文件
        resp = client.call(messages=[...], system="...", tools=[...], max_output_tokens=8192)
    """

    def __init__(
        self,
        model: str,
        *,
        provider: Optional[str] = None,
        config_path: Optional[Path] = None,
        retry_base_delay_s: float = 2.0,
        max_retries: int = 3,
    ):
        self.model = model
        self.registry: ProviderRegistry = (
            load_registry(config_path) if config_path else get_default_registry()
        )
        self.provider = provider or self.registry.detect(model)
        self.config: ProviderConfig = self.registry.get(self.provider)
        self.retry_base_delay_s = retry_base_delay_s
        self.max_retries = max_retries
        self.client = self._init_client()

    def _init_client(self) -> Any:
        cfg = self.config
        api_key = cfg.api_key or (os.environ.get(cfg.key_env) if cfg.key_env else None)

        if cfg.type == "anthropic_native":
            import anthropic
            kwargs: dict[str, Any] = {}
            if api_key:
                kwargs["api_key"] = api_key
            else:
                # Fallback: Claude Code OAuth token（Pro/Team subscription）
                oauth = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")
                if oauth:
                    kwargs["auth_token"] = oauth
                # else: let anthropic SDK fall back to ANTHROPIC_API_KEY env
            return anthropic.Anthropic(**kwargs)

        if cfg.type == "openai_compat":
            import openai
            kwargs: dict[str, Any] = {}
            if cfg.base_url:
                kwargs["base_url"] = cfg.base_url
            if api_key:
                kwargs["api_key"] = api_key
            elif cfg.key_env is None:
                kwargs["api_key"] = "no-key-required"
            else:
                raise RuntimeError(
                    f"env var {cfg.key_env} not set — required for "
                    f"provider '{self.provider}' (model '{self.model}'); "
                    f"or set api_key in providers.yaml (not for committed configs)"
                )
            return openai.OpenAI(**kwargs)

        raise ValueError(f"unknown provider type: {cfg.type!r}")

    def call(
        self,
        *,
        messages: list[dict[str, Any]],
        system: str,
        tools: list[dict[str, Any]],
        max_output_tokens: int,
    ) -> NormalizedResponse:
        last_err: Optional[Exception] = None
        for attempt in range(self.max_retries):
            try:
                if self.config.type == "anthropic_native":
                    return self._call_anthropic(messages, system, tools, max_output_tokens)
                return self._call_openai_compat(messages, system, tools, max_output_tokens)
            except Exception as e:
                if attempt < self.max_retries - 1 and self._is_retryable(e):
                    time.sleep(self.retry_base_delay_s * (2 ** attempt))
                    last_err = e
                    continue
                raise
        assert last_err is not None
        raise last_err

    def _call_anthropic(self, messages, system, tools, max_output_tokens):
        anth_msgs = _openai_to_anthropic_messages(messages)
        anth_tools = _tools_to_anthropic(tools)

        # Prompt caching: 3 breakpoints（system / last tool / 首条 user msg）。
        # 每轮读缓存约 7K tokens（10% 成本），首轮写缓存（+25% 一次性）。
        system_blocks = [{"type": "text", "text": system,
                          "cache_control": {"type": "ephemeral"}}]
        if anth_tools:
            anth_tools[-1] = {**anth_tools[-1], "cache_control": {"type": "ephemeral"}}
        if anth_msgs and anth_msgs[0]["role"] == "user":
            first = anth_msgs[0]["content"]
            if isinstance(first, str):
                anth_msgs[0]["content"] = [
                    {"type": "text", "text": first, "cache_control": {"type": "ephemeral"}}
                ]
            elif isinstance(first, list) and first:
                last_block = first[-1]
                if isinstance(last_block, dict) and last_block.get("type") == "text":
                    first[-1] = {**last_block, "cache_control": {"type": "ephemeral"}}

        resp = self.client.messages.create(
            model=self.model,
            max_tokens=max_output_tokens,
            system=system_blocks,
            tools=anth_tools,
            messages=anth_msgs,
        )
        return _anthropic_response_to_normalized(resp)

    def _call_openai_compat(self, messages, system, tools, max_output_tokens):
        msgs = [{"role": "system", "content": system}] + messages
        kwargs: dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_output_tokens,
            "messages": msgs,
        }
        oai_tools = _tools_to_openai(tools)
        if oai_tools:
            kwargs["tools"] = oai_tools
        resp = self.client.chat.completions.create(**kwargs)
        return _openai_response_to_normalized(resp)

    @staticmethod
    def _is_retryable(e: Exception) -> bool:
        """Rate limit + 5xx 重试。两家 SDK 错误类不一样，合并判 status_code。"""
        status = (
            getattr(getattr(e, "response", None), "status_code", None)
            or getattr(e, "status_code", None)
        )
        if status is not None and (status == 429 or 500 <= status < 600):
            return True
        cls_name = type(e).__name__
        if cls_name in (
            "RateLimitError", "APIConnectionError", "APITimeoutError",
            "InternalServerError", "ServiceUnavailableError",
        ):
            return True
        return False
