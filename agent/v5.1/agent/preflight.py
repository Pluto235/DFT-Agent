"""Shared preflight: structure-matched skills + config advice (deterministic
classifier + LLM cross-check).

Ported knowledge layer from v4.2, MINUS the orchestrator. Produces one advice
block that solve.py prepends to the user prompt for BOTH the legacy and claude
paths. Every sub-step is wrapped so a failure degrades to a partial/empty block
— the preflight must never crash the solver.

The config_advisor step runs the deterministic classifier and then an LLM
cross-check (config_advisor.run) that may only override is_metal / ecutrho_factor
(chemistry-derived flags are immutable). The cross-check is opt-out via
DFT_DISABLE_CONFIG_ADVISOR_LLM and its model is set by DFT_CONFIG_ADVISOR_MODEL
(default a cheap small model). Any cross-check failure (client init, timeout,
loop crash) degrades silently to the deterministic result.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

log = logging.getLogger("preflight")

# Cheap "agree-or-override" decision — a small model is plenty. Default is the
# cheap DeepSeek model: it works with the DEEPSEEK_API_KEY this deploy already
# uses, whereas claude-haiku needs an ANTHROPIC_API_KEY that isn't provisioned
# here (the CLAUDE_CODE_OAUTH_TOKEN 401s against the raw Anthropic API).
# Overridable so deployments can route it to whatever provider/key they have.
_CONFIG_ADVISOR_MODEL = os.environ.get(
    "DFT_CONFIG_ADVISOR_MODEL", "deepseek-v4-flash")


def run_preflight(prompt_text: str, workdir: Path) -> str:
    """Return a material-specific advice block (skills + deterministic config).

    Empty string if nothing matched. Never raises.
    """
    blocks: list[str] = []

    # 1) Structure-matched skill advisories (low-symmetry-oxide for WO3, etc.)
    try:
        from agent import structure_features
        from agent.skills import load_all, select_and_render
        feats = structure_features.extract(prompt_text)
        advisory = select_and_render(load_all(), feats)
        if advisory.strip():
            blocks.append(advisory)
    except Exception as e:  # noqa: BLE001
        log.warning("preflight: skills step failed: %s", e)

    # 2) Config advice: deterministic classifier + LLM cross-check.
    try:
        from agent.subagents import config_advisor as ca
        advice = None
        if not os.getenv("DFT_DISABLE_CONFIG_ADVISOR_LLM"):
            # LLM cross-check. v5.1's LLMClient has no from_role (model-routed,
            # not role-routed), so we build the client explicitly here and pass
            # it in — config_advisor.run(client=...) then skips its own
            # from_role path. Any failure (no key, timeout, loop crash) →
            # deterministic fallback with source="fallback".
            try:
                from agent.llm import LLMClient
                client = LLMClient(_CONFIG_ADVISOR_MODEL)
                advice = ca.run(
                    client=client, original_prompt=prompt_text, workdir=workdir,
                    max_turns=6, max_output_tokens=2048,
                )
            except Exception as e:  # noqa: BLE001
                log.warning("preflight: config_advisor LLM cross-check failed; "
                            "deterministic fallback: %s", e)
                advice = ca._from_deterministic(
                    ca._deterministic_classify(prompt_text),
                    source="fallback", failure_reason=f"{type(e).__name__}: {e}")
        if advice is None:  # cross-check disabled → deterministic only
            advice = ca._from_deterministic(ca._deterministic_classify(prompt_text))

        block = ca.render_config_advice_block(advice)
        try:
            (workdir / ".config_advice.json").write_text(advice.to_json())
        except OSError as e:  # noqa: BLE001
            log.warning("preflight: could not persist .config_advice.json: %s", e)
        if block.strip():
            blocks.append(block)
    except Exception as e:  # noqa: BLE001
        log.warning("preflight: config_advisor step failed: %s", e)

    return "\n\n".join(blocks)
