"""TranscriptWriter — v4-compatible <workdir>/transcript.json appender.

v4 writes a JSON array of per-turn dicts in Anthropic-flavor shape so that
report builders in ~/solvers/current/scripts/build_*_report.py can parse
v3 and v4 runs uniformly. v5 keeps the same shape so existing report
tools continue to work without modification.

Shape of one entry:
    {
      "stop_reason": "end_turn" | "tool_use" | "max_tokens" | ...,
      "content": [
        {"type": "text", "text": ...},
        {"type": "tool_use", "id": ..., "name": ..., "input": ...}
      ],
      "usage": {input_tokens, output_tokens, ...},
      "reasoning_content": ...,        # optional, for thinking-mode models
      "llm_wall_ms": ...               # optional
    }

For tool results (which are user messages in the Anthropic shape) we use:
    {"stop_reason": "tool_result",
     "content": [{"type": "tool_result", "tool_use_id": id,
                  "content": [{"type": "text", "text": ...}],
                  "is_error": bool}]}

Atomic append via tmp-file + rename so a crash mid-write never corrupts.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Any


class TranscriptWriter:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._entries: list[dict[str, Any]] = []
        if self.path.exists():
            try:
                self._entries = json.loads(self.path.read_text(encoding="utf-8"))
                if not isinstance(self._entries, list):
                    self._entries = []
            except json.JSONDecodeError:
                self._entries = []

    def append(self, entry: dict[str, Any]) -> None:
        self._entries.append(entry)
        self._flush()

    def append_assistant(
        self,
        *,
        text: str | None,
        tool_calls: list[dict[str, Any]] | None = None,
        stop_reason: str | None = None,
        usage: dict | None = None,
        llm_wall_ms: float | None = None,
        reasoning_content: str | None = None,
    ) -> None:
        blocks: list[dict[str, Any]] = []
        if text:
            blocks.append({"type": "text", "text": text})
        for tc in tool_calls or []:
            blocks.append({"type": "tool_use",
                           "id": tc.get("id") or tc.get("tool_use_id") or "",
                           "name": tc.get("name") or tc.get("tool_name") or "",
                           "input": tc.get("input") or {}})
        # Default stop_reason: "tool_use" if any tool_use block was emitted,
        # else "end_turn" — matches v4 transcript and Anthropic API semantics.
        if stop_reason is None:
            stop_reason = "tool_use" if tool_calls else "end_turn"
        entry: dict[str, Any] = {
            "stop_reason": stop_reason,
            "content": blocks,
            "usage": usage or {},
        }
        if reasoning_content:
            entry["reasoning_content"] = reasoning_content
        if llm_wall_ms is not None:
            entry["llm_wall_ms"] = llm_wall_ms
        self.append(entry)

    def append_tool_result(
        self,
        *,
        tool_use_id: str,
        text: str,
        is_error: bool = False,
    ) -> None:
        self.append({
            "stop_reason": "tool_result",
            "content": [{
                "type": "tool_result",
                "tool_use_id": tool_use_id,
                "content": [{"type": "text", "text": text}],
                "is_error": is_error,
            }],
        })

    def append_ledger_nudge(self, signal: str, text: str) -> None:
        """Record stuck-detector nudges so the transcript explains
        the agent's behavior change post-mortem."""
        self.append({
            "stop_reason": "ledger_nudge",
            "content": [{"type": "text", "text": text, "ledger_signal": signal}],
        })

    def reconcile_orphan_tool_uses(self) -> int:
        """Drop tool_use blocks that never produced a tool_result.

        The deepseek Anthropic-compat endpoint emits tool_use blocks during
        streaming that the SDK mirrors via PreToolUse (→ transcript) but
        never executes (no PostToolUse → no tool_result). Left in, they make
        the transcript over-count tool calls (observed: write_qe_input ×7 when
        only 2 actually ran), which misleads transcript readers and tool-stat
        tooling. The ledger is unaffected (it is fed only by PostToolUse, i.e.
        real executions) — this only cleans the transcript record.

        Removes any tool_use block whose id has no matching tool_result;
        assistant entries that become empty are dropped. Returns the number
        of orphan tool_use blocks removed. Call once after the loop ends.
        """
        resolved = {
            b.get("tool_use_id")
            for e in self._entries
            for b in (e.get("content") or [])
            if isinstance(b, dict) and b.get("type") == "tool_result"
        }
        dropped = 0
        new_entries: list[dict[str, Any]] = []
        for e in self._entries:
            content = e.get("content")
            if not isinstance(content, list):
                new_entries.append(e)
                continue
            kept = []
            n_orphan = 0
            for b in content:
                if (isinstance(b, dict) and b.get("type") == "tool_use"
                        and b.get("id") not in resolved):
                    n_orphan += 1
                else:
                    kept.append(b)
            if n_orphan == 0:
                new_entries.append(e)
                continue
            dropped += n_orphan
            if kept:  # entry still has text / resolved tool_use blocks → keep it
                new_entries.append({**e, "content": kept})
            # else: entry was nothing but orphan tool_use(s) → drop entirely
        if dropped:
            self._entries = new_entries
            self._flush()
        return dropped

    def _flush(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self._entries, indent=2, default=str),
                       encoding="utf-8")
        tmp.replace(self.path)

    def __len__(self) -> int:
        return len(self._entries)
