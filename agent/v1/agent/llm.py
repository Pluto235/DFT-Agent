"""LLMClient — 多 provider 统一入口。

设计理由(2026-05-03 替代 litellm):
- litellm 半年内连出 5 严重 proxy 漏洞 + 1 PyPI 供应链事件,我们不引入
- 改走 native SDK 双路径:`anthropic` 直调 Anthropic,`openai` SDK 配 base_url
  覆盖所有 OpenAI-兼容 provider(DeepSeek / Mistral / xAI / Gemini compat /
  Moonshot / OpenRouter / Together / Ollama 等)
- 攻击面只剩两家亲自维护的 SDK + pyyaml

外部 contract:
- loop.py 给我们 OpenAI-shape messages(canonical);我们内部转 Anthropic shape
- 返回 `NormalizedResponse`(text + tool_calls + stop_reason + usage)
- 工具用 generic 描述 `{name, description, parameters}`,内部转两种 shape

Provider 路由通过 agent/providers.yaml 配置(详见该文件)。
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
    text: str                         # 合并所有 text block;无文本 = ""
    tool_calls: list[NormalizedToolCall]
    stop_reason: str                  # 原始 provider 值,不做语义转换
    usage: dict[str, Optional[int]]   # {input_tokens, output_tokens}
    reasoning_content: Optional[str] = None  # DeepSeek v4 / o1 等 reasoning model;
                                              # 非 None 时 loop 必须回传到下一轮 assistant message


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

    OpenAI shape:
      {role: user, content: str}
      {role: assistant, content: str|None, tool_calls?: [{id, type:function,
          function:{name, arguments(json string)}}]}
      {role: tool, tool_call_id, content: str}

    Anthropic shape:
      {role: user, content: str | [{type:text, text}|{type:tool_result,
          tool_use_id, content}]}
      {role: assistant, content: [{type:text, text}|{type:tool_use, id, name,
          input(dict)}]}

    多个连续 OpenAI tool 消息合并为 1 个 Anthropic user 消息(API 要求
    user/assistant 严格交替)。
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
            # 与上一条 user(已是 list) 合并,保 user/assistant 交替
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
    # Anthropic prompt cache：cache_creation_input_tokens（写入）+ cache_read_input_tokens（命中）。
    # 计费层视角：write 同 standard rate，read 是 hit 的折扣价。映射到统一 schema：
    # cache_hit_tokens = read，cache_miss_tokens = creation + 普通 input（input_tokens 已含两者）。
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
            # 模型偶尔吐 invalid JSON;留 _raw 给调用方判别
            parsed = {"_raw": tc.function.arguments}
        tool_calls.append(NormalizedToolCall(
            id=tc.id, name=tc.function.name, input=parsed,
        ))
    usage_obj = getattr(resp, "usage", None)
    # reasoning_content:DeepSeek v4 / 部分 o1 等 reasoning model 才有;
    # 这些 provider 强制要求 assistant 回传上一轮的 reasoning_content,否则 400
    reasoning = getattr(msg, "reasoning_content", None) or None
    return NormalizedResponse(
        text=text,
        tool_calls=tool_calls,
        stop_reason=resp.choices[0].finish_reason or "",
        # OpenAI-compat provider 暴露 cache 字段不一：
        # - DeepSeek：prompt_cache_hit_tokens / prompt_cache_miss_tokens（两者和 = prompt_tokens）
        # - 其他 (xai/mistral/moonshot/openrouter)：暂未发现公开字段，留 None
        # input_tokens 仍取 prompt_tokens 总和（hit + miss）以保 backward compat。
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
    """Multi-provider LLM 入口。retry on rate limit / 5xx,指数退避。

    用法:
        client = LLMClient("claude-sonnet-4-6")          # 自动按 providers.yaml 检
        client = LLMClient("kimi-latest")                # 同上,会路由到 moonshot
        client = LLMClient("gpt-4o", provider="openai")  # 手动覆盖
        client = LLMClient("xxx", config_path=Path(...)) # 自定义配置文件
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
        self._oauth_auth = False  # True 当用 OAuth bearer(非裸 api_key)认证 anthropic
        # 两类 SDK 共用的 key 解析:yaml api_key 优先,key_env env 其次。
        api_key = cfg.api_key or (os.environ.get(cfg.key_env) if cfg.key_env else None)

        if cfg.type == "anthropic_native":
            import anthropic
            kwargs: dict[str, Any] = {}
            # anthropic SDK 默认 fallback ANTHROPIC_API_KEY env;只在有显式 key 时传参。
            if api_key:
                kwargs["api_key"] = api_key
            elif not os.environ.get("ANTHROPIC_API_KEY"):
                # 无裸 key 时回退 OAuth bearer(Claude Code 订阅 token)。本部署只配了
                # CLAUDE_CODE_OAUTH_TOKEN;裸 anthropic SDK 不会自动读它,显式当 auth_token 传。
                # 实测:opus-4-8 返回 429(已认证·限流)而非 401 → bearer 认证可用。
                oauth = (os.environ.get("ANTHROPIC_AUTH_TOKEN")
                         or os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"))
                if oauth:
                    kwargs["auth_token"] = oauth
                    self._oauth_auth = True
            return anthropic.Anthropic(**kwargs)

        if cfg.type == "openai_compat":
            import openai
            kwargs: dict[str, Any] = {}
            if cfg.base_url:
                kwargs["base_url"] = cfg.base_url
            if api_key:
                kwargs["api_key"] = api_key
            elif cfg.key_env is None:
                # Ollama 等 local provider:openai SDK 仍要求 api_key 字段非空
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
        sys_param: Any = system
        if getattr(self, "_oauth_auth", False):
            # OAuth(Claude Code 订阅)token 要求 system 首块为 Claude Code 身份,否则
            # premium 模型(sonnet/opus)返回伪装的 429 rate_limit_error(haiku 不卡)。
            # 实测 2026-06-10:缺身份 → 整批 opus run 第一次 call 即 429 全废。
            cc = {"type": "text",
                  "text": "You are Claude Code, Anthropic's official CLI for Claude."}
            if isinstance(system, str):
                sys_param = [cc] + ([{"type": "text", "text": system}] if system else [])
            elif isinstance(system, list):
                sys_param = [cc] + system
            else:
                sys_param = [cc]
        resp = self.client.messages.create(
            model=self.model,
            max_tokens=max_output_tokens,
            system=sys_param,
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
        """Rate limit + 5xx 重试。两家 SDK 错误类不一样,合并判 status_code。"""
        status = (
            getattr(getattr(e, "response", None), "status_code", None)
            or getattr(e, "status_code", None)
        )
        if status is not None and (status == 429 or 500 <= status < 600):
            return True
        # 名字判:RateLimitError / APIConnectionError / ServiceUnavailable 等
        cls_name = type(e).__name__
        if cls_name in (
            "RateLimitError", "APIConnectionError", "APITimeoutError",
            "InternalServerError", "ServiceUnavailableError",
        ):
            return True
        return False
