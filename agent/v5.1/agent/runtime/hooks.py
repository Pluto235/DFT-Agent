"""SDK hook wrappers — wire PreToolUse / PostToolUse into ledger + transcript.

claude-agent-sdk hook callbacks have the signature

    async def hook(input_data, tool_use_id, context) -> dict | None

We bind ledger + transcript via closure so the hook factory can be passed
to HookMatcher.

Convention (from ccguide doc):
  - PreToolUse return  : {"hookSpecificOutput": {"hookEventName": "PreToolUse",
                          "permissionDecision": "allow"|"deny"|"ask"|"defer",
                          "updatedInput": dict, ...}}
  - PostToolUse return : {"hookSpecificOutput": {"hookEventName": "PostToolUse",
                          "additionalContext": str, "updatedToolOutput": Any}}

For our DFT solver we only need PostToolUse to inject ledger nudges; the
PreToolUse path is observation-only (the structured tools and BashTool
already do lint/guard internally).
"""

from __future__ import annotations

from typing import Any

from agent.runtime.progress_ledger import ProgressLedger
from agent.runtime.transcript import TranscriptWriter


def _extract_tool_text(tool_response: Any) -> tuple[str, bool]:
    """Pull a human-readable summary out of an SDK tool response.

    Tool responses come in a few shapes:
      - dict with `content`: a list of MCP content blocks → join text blocks
      - dict with text under various keys → str()
      - raw str → str
    Returns (text, is_error).
    """
    if isinstance(tool_response, dict):
        # The claude-agent-sdk delivers MCP results with the MCP-standard
        # camelCase `isError` (omitted entirely when False); our own tool dicts
        # use snake_case `is_error`. Accept either so the flag survives the SDK
        # serialization boundary (see _internal/query.py / __init__.py).
        is_error = bool(tool_response.get("isError",
                                          tool_response.get("is_error", False)))
        content = tool_response.get("content")
        if isinstance(content, list):
            parts = []
            for blk in content:
                if isinstance(blk, dict) and blk.get("type") == "text":
                    parts.append(str(blk.get("text", "")))
                else:
                    parts.append(str(blk))
            return "\n".join(parts), is_error
        if "text" in tool_response:
            return str(tool_response["text"]), is_error
        return str(tool_response), is_error
    return str(tool_response), False


# ─────────────────────────────────────────────────────────────────────
# Hook factories
# ─────────────────────────────────────────────────────────────────────

def make_pre_tool_use_hook(
    ledger: ProgressLedger,
    transcript: TranscriptWriter,
):
    """PreToolUse: just observe; no input mutation.

    The structured tools and BashTool handle lint/guard internally; this
    hook exists purely to mirror tool_use blocks into transcript.json with
    the same shape as v4.
    """

    async def _pre(input_data, tool_use_id, context):
        tool_name = input_data.get("tool_name", "")
        tool_input = input_data.get("tool_input") or {}
        # Mirror v4 transcript: an assistant entry with a single tool_use block.
        transcript.append_assistant(
            text=None,
            tool_calls=[{"id": tool_use_id or "", "name": tool_name, "input": tool_input}],
            stop_reason="tool_use",
        )
        # Empty hook output = "carry on with original input".
        return {}

    return _pre


def make_post_tool_use_hook(
    ledger: ProgressLedger,
    transcript: TranscriptWriter,
):
    """PostToolUse: record turn, mirror tool_result to transcript,
    and inject a stuck-detector nudge as additionalContext when fired."""

    async def _post(input_data, tool_use_id, context):
        tool_name = input_data.get("tool_name", "")
        tool_input = input_data.get("tool_input") or {}
        tool_response = input_data.get("tool_response")

        text, is_error = _extract_tool_text(tool_response)

        # 1. Mirror tool_result to transcript
        transcript.append_tool_result(
            tool_use_id=tool_use_id or "",
            text=text,
            is_error=is_error,
        )

        # 2. Record turn in ledger. Pass the already-extracted `text` so the
        #    ledger can detect verify_run verdicts from the content (the SDK
        #    strips custom dict keys like `verdict`, so the dict is not enough).
        ledger.record_tool_use(
            tool=tool_name,
            tool_use_id=tool_use_id or "",
            tool_input=tool_input,
            tool_response=tool_response,
            error=is_error,
            text=text,
        )

        # 3. Check stuck
        sig = ledger.check_stuck()
        if sig is None:
            return {}

        nudge = sig.render(recent_calc_ids=ledger.recent_calc_ids(n=5))
        transcript.append_ledger_nudge(sig.signal, nudge)

        return {
            "hookSpecificOutput": {
                "hookEventName": "PostToolUse",
                "additionalContext": nudge,
            },
        }

    return _post
