"""调 anthropic SDK haiku-4-5 同步一次 + 30s timeout。

system prompt 内联（待 P3 后期搬到 backend/prompts/enhance_system.md，目前 file 还没生）。
"""
from __future__ import annotations

import asyncio

from backend.schemas.material import EnhanceRequest, EnhanceResponse


_SYSTEM_PROMPT = """You are a DFT computational task prompt enhancer.
Your goal: take a user's loose description of a DFT calculation and rewrite it
into a precise, structured task spec that an LLM agent can execute via Quantum ESPRESSO.

Output the enhanced prompt directly (no preamble, no markdown headers). Include:
- material identification (formula / structure / space group if known)
- target observables and convergence criteria (with explicit thresholds)
- explicit submission format: call the `submit` tool with a `task_outputs` dict
- network policy reminder: network_allowed = false; agent must not access the network
"""


def _format_user_msg(req: EnhanceRequest) -> str:
    parts = [f"task_type: {req.task_type}"]
    if req.material_hint:
        parts.append(f"material_hint: {req.material_hint}")
    parts.append(f"user_description:\n{req.user_description}")
    return "\n\n".join(parts)


async def enhance_prompt(
    req: EnhanceRequest,
    *,
    model: str,
    api_key: str,
    timeout_s: float = 30.0,
) -> EnhanceResponse:
    if not api_key:
        raise RuntimeError("enhance API key not configured (set ANTHROPIC_API_KEY)")
    import anthropic
    client = anthropic.AsyncAnthropic(api_key=api_key)
    msg = await asyncio.wait_for(
        client.messages.create(
            model=model,
            max_tokens=2000,
            system=_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": _format_user_msg(req)}],
        ),
        timeout=timeout_s,
    )
    enhanced = "".join(
        getattr(b, "text", "") for b in msg.content if getattr(b, "type", None) == "text"
    )
    diff_summary = _diff_summary(req.user_description, enhanced)
    return EnhanceResponse(
        enhanced_prompt=enhanced,
        diff_summary=diff_summary,
        model=model,
        usage={
            "input_tokens": getattr(msg.usage, "input_tokens", None),
            "output_tokens": getattr(msg.usage, "output_tokens", None),
        },
    )


def _diff_summary(orig: str, enhanced: str) -> str:
    delta_chars = len(enhanced) - len(orig)
    sign = "+" if delta_chars >= 0 else ""
    return f"rewritten ({sign}{delta_chars} chars)"
