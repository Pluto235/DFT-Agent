"""ToolContext — shared state injected into every structured tool.

Tools are plain async functions taking (args: dict, *, ctx: ToolContext) so
they can be unit-tested without claude-agent-sdk. server.py wraps them with
the @tool decorator and binds ctx via a closure.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent.tools.structured.calc_registry import CalcRegistry


@dataclass
class ToolContext:
    workdir: Path
    calc_registry: CalcRegistry
    bash_tool: Any  # BashTool — avoid circular import in __init__
    pseudo_dir: Path | None = None
    # Set by the submit tool when the LLM decides it's done. Driver polls
    # this after each message and exits the SDK loop when non-None.
    submit_payload: dict | None = None

    def rel(self, p: str | Path) -> Path:
        """Resolve a workdir-relative path; reject anything outside workdir."""
        p = Path(p)
        full = (self.workdir / p).resolve() if not p.is_absolute() else p.resolve()
        try:
            full.relative_to(self.workdir.resolve())
        except ValueError as e:
            raise ValueError(f"path escapes workdir: {p}") from e
        return full
