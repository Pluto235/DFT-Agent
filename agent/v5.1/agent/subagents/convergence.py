"""Convergence Fixer sub-agent registration for claude-agent-sdk.

The actual fix decision logic lives in:
  - agent/prompts/convergence_subagent.md (LLM prompt with playbook table)
  - agent/subagents/playbook_scf.py (programmatic decision table)

The LLM-driven sub-agent reads errors via read_qe_output, picks a remedy
from the playbook, writes a new input via write_qe_input, runs pw.x, and
verifies. The playbook module is also exposed so tests / the executor
can call match_symptom() directly without spawning the sub-agent.
"""

from __future__ import annotations

from pathlib import Path

from claude_agent_sdk import AgentDefinition


_PROMPT_PATH = Path(__file__).parent.parent / "prompts" / "convergence_subagent.md"


def build_convergence_agent(*, model: str = "inherit") -> AgentDefinition:
    """Build the AgentDefinition for the convergence_fixer sub-agent.

    model="inherit" → uses parent's model (saves cost on simple cases).
    Override to e.g. "claude-sonnet-4-6" when SCF failures need more reasoning.
    """
    return AgentDefinition(
        description=(
            "Diagnose and fix Quantum ESPRESSO SCF non-convergence. "
            "Input: a failing calc_id and one-line symptom. "
            "Output: a new successful calc_id, or a clear diagnosis if "
            "≥3 remedies failed."
        ),
        prompt=_PROMPT_PATH.read_text(encoding="utf-8"),
        tools=[
            "mcp__dft_qe__read_qe_output",
            "mcp__dft_qe__write_qe_input",
            "mcp__dft_qe__run_pw",
            "mcp__dft_qe__verify_run",
        ],
        model=model,
    )
