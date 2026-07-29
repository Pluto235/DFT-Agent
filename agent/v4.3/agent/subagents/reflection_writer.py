"""ReflectionWriter subagent — produces a methodology lesson AFTER a
verifier-trip-recover sequence (attempt 1 failed verification, a later
attempt passed).

Input (minimal — per design decision §10):
  - failed_attempt.verdict.diagnosis (the verifier's first complaint)
  - "what changed" diff: from the per-step transcript.json, extract the
    last write_file content per path for the failed and passed attempts;
    emit a unified diff for matching paths. Capped at 4 KB total.

Output: a dict {trigger, action, rationale} via the `submit_lesson` tool,
or None when the LLM declines (or after 2 schema retries).

Model role: "reflection_writer" — same cheap tier as verifier. Not in
prod usage hot path (only fires on actual recoveries).
"""
from __future__ import annotations

import difflib
import json
import time
import uuid
from pathlib import Path
from typing import Any, Optional

from agent.ledger import Attempt, PlanStep
from agent.llm import LLMClient, NormalizedResponse
from agent.loop import _append_transcript, _serialize_response


# Hard cap on the diff segment fed to the LLM. The reflection_writer
# already has the verifier diagnosis to anchor on; the diff is supplemental.
_DIFF_BUDGET_BYTES = 4096


REFLECTION_TOOLS: list[dict[str, Any]] = [
    {
        "name": "submit_lesson",
        "description": (
            "Submit a single methodology lesson learned from this verifier-trip "
            "recovery, OR decline if there is nothing generalizable to teach. "
            "ALWAYS call this exactly once and then stop."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "trigger":   {"type": "string",
                              "description": "Symptom that prompted the fix (≤200 chars)."},
                "action":    {"type": "string",
                              "description": "Concrete corrective action (≤200 chars)."},
                "rationale": {"type": "string",
                              "description": "Why the action works (≤200 chars)."},
                "decline":   {"type": "boolean",
                              "description": "True if no generalizable lesson; submit with just decline=true and reason."},
                "reason":    {"type": "string",
                              "description": "Required if decline=true."},
            },
        },
    },
]


_REFLECTION_SYSTEM_PROMPT = """\
You are a reflection subagent for a DFT (Quantum ESPRESSO) workflow.

A previous executor attempt FAILED its verifier, and a subsequent
attempt PASSED. Your job: write ONE methodology lesson that captures
the recoverable pattern, so a future executor on a DIFFERENT material
benefits from this experience.

# Hard rules (these define the entire scope of acceptable lessons)

A lesson is METHODOLOGY ONLY — never an answer to the specific task.

DO write things like:
  - "vc-relax force oscillation in early steps"  → "lower mixing_beta"
  - "SCF charge density density-step warnings"   → "tighten conv_thr"
  - "verifier fails on missing JOB DONE in NSCF" → "check k-path block terminator"

DO NOT write things like:
  - any element symbol (Si, Fe, O, ...) — ALL 118 are forbidden
  - any concrete output value like "gap = 1.2 eV", "lattice = 5.43 Å"
  - any verbatim task_outputs key (e.g. bands_data, scf_out, gap_type)
  - any `<number><unit>` pattern OUTSIDE of QE-keyword = value assignments
    (e.g. `ecutwfc=60` is fine; bare `60 Ry` is not)

If nothing generalizable can be said (the recovery was idiosyncratic,
or the diff is just whitespace), call submit_lesson with `decline=true`
and a brief reason. Empty libraries are better than poisoned ones.

# Format

Call `submit_lesson` exactly once with:
  trigger:   ≤200 chars, symptom phrasing
  action:    ≤200 chars, the corrective action
  rationale: ≤200 chars, the underlying mechanism

Both trigger and action should be material-agnostic (apply to whole
classes of materials, not "for this case"). Keep rationale short and
physical.
"""


def _user_prompt(
    step: PlanStep,
    diagnosis: str,
    diff_text: str,
    scope: dict[str, Any],
) -> str:
    parts = [
        f"## Step: {step.id} (kind={step.kind})",
        f"## Scope: {json.dumps(scope, ensure_ascii=False)}",
        "",
        "## Verifier diagnosis from the FAILED attempt",
        diagnosis or "(no diagnosis recorded)",
        "",
        "## Diff of what changed between the failed and the passed attempt",
    ]
    if diff_text:
        parts.append("```diff")
        parts.append(diff_text)
        parts.append("```")
    else:
        parts.append("(no relevant diff available)")
    parts.append("")
    parts.append(
        "Submit a methodology lesson via submit_lesson, OR submit "
        "decline=true if there's nothing generalizable to teach.")
    return "\n".join(parts)


def _extract_write_files(turns: list[dict[str, Any]]) -> dict[str, str]:
    """Walk a list of transcript turns (Anthropic-flavor blocks) and return
    {path: content} for the LATEST write_file per path.
    """
    out: dict[str, str] = {}
    for turn in turns:
        for block in (turn.get("content") or []):
            if block.get("type") != "tool_use":
                continue
            if block.get("name") != "write_file":
                continue
            inp = block.get("input") or {}
            path = inp.get("path")
            content = inp.get("content")
            if isinstance(path, str) and isinstance(content, str):
                out[path] = content
    return out


def _load_attempt_writes(
    workdir: Path, step: PlanStep, target_session_id: str,
) -> dict[str, str]:
    """Read <workdir>/<step.id>/transcript.json. Returns the latest write_file
    map for the given session_id, or {} if nothing matches.

    NOTE: the executor transcript is shared across attempts in the same step
    (run() and resume() both append to the same file). We can't filter by
    session_id directly because turns don't carry one in the Anthropic shape.
    Best-effort: parse the entire file and return all writes. The diff caller
    can still tell which paths matter via the per-attempt slicing below.
    """
    path = workdir / step.id / "transcript.json"
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, list):
        return {}
    return _extract_write_files(data)


def _build_diff_section(
    *, workdir: Path, step: PlanStep,
    failed: Attempt, passed: Attempt,
) -> str:
    """Produce a unified diff between the failed and passed attempts' last
    write_file contents (per path). Caps total output at _DIFF_BUDGET_BYTES.
    """
    transcript_path = workdir / step.id / "transcript.json"
    if not transcript_path.exists():
        return ""
    try:
        all_turns = json.loads(transcript_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    if not isinstance(all_turns, list):
        return ""

    # We can't perfectly slice by attempt, but conversation order is preserved:
    # the failed attempt's turns come BEFORE a "VERIFIER FEEDBACK" user message
    # block (injected by executor.resume()). Detect that split point.
    split_idx = len(all_turns)
    for i, turn in enumerate(all_turns):
        # In Anthropic transcript shape, only assistant entries are recorded;
        # the user injection comes through the conversation but not directly
        # in serialized turns. Fall back: half-half if no marker found.
        text_blocks = [b for b in (turn.get("content") or []) if b.get("type") == "text"]
        if any("VERIFIER FEEDBACK" in (b.get("text") or "") for b in text_blocks):
            split_idx = i
            break
    if split_idx == len(all_turns):
        # No explicit marker; assume first half is failed, second half is passed.
        split_idx = max(1, len(all_turns) // 2)

    failed_writes = _extract_write_files(all_turns[:split_idx])
    passed_writes = _extract_write_files(all_turns[split_idx:])

    pieces: list[str] = []
    used = 0
    # Prioritize paths that appear in both (likely re-written input files).
    common_paths = sorted(set(failed_writes) & set(passed_writes))
    for path in common_paths:
        if used >= _DIFF_BUDGET_BYTES:
            break
        before = failed_writes[path].splitlines(keepends=False)
        after = passed_writes[path].splitlines(keepends=False)
        diff = difflib.unified_diff(before, after,
                                    fromfile=f"{path} (failed)",
                                    tofile=f"{path} (passed)",
                                    lineterm="")
        chunk = "\n".join(diff)
        if not chunk:
            continue
        if used + len(chunk) > _DIFF_BUDGET_BYTES:
            chunk = chunk[: _DIFF_BUDGET_BYTES - used] + "\n... (truncated)"
        pieces.append(chunk)
        used += len(chunk) + 1  # +1 for the joiner

    # If no overlapping paths, include the last passed write verbatim so the
    # LLM has SOMETHING concrete to look at.
    if not pieces and passed_writes:
        latest_path = sorted(passed_writes.keys())[-1]
        snippet = passed_writes[latest_path][: _DIFF_BUDGET_BYTES // 2]
        pieces.append(f"# Final write_file in the passing attempt: {latest_path}\n{snippet}")

    return "\n\n".join(pieces)


def run(
    *,
    step: PlanStep,
    attempts: list[Attempt],
    scope: dict[str, Any],
    workdir: Path,
    cli_overrides: Optional[dict[str, str]] = None,
    config_path: Optional[Path] = None,
    max_turns: int = 4,
    max_output_tokens: int = 1024,
    transcript_path: Optional[Path] = None,
    client: Optional[LLMClient] = None,
) -> Optional[dict[str, str]]:
    """Run the reflection writer for one step. Returns a lesson dict
    {trigger, action, rationale} or None when no lesson is produced.

    Caller (orchestrator) MUST gate on `attempt_no > 1 && verdict.pass_`
    BEFORE calling this — we don't re-check the criterion here.
    """
    if len(attempts) < 2:
        return None
    # First failed attempt (the trip) and the last attempt (the recovery).
    failed = next((a for a in attempts if a.verdict is not None and not a.verdict.pass_), None)
    passed = attempts[-1]
    if failed is None or passed.verdict is None or not passed.verdict.pass_:
        return None

    diagnosis = (failed.verdict.diagnosis or "").strip()
    diff_text = _build_diff_section(
        workdir=workdir, step=step, failed=failed, passed=passed,
    )

    sys_prompt = _REFLECTION_SYSTEM_PROMPT
    user_msg = _user_prompt(step, diagnosis, diff_text, scope)

    if client is None:
        client = LLMClient.from_role(
            "reflection_writer",
            cli_overrides=cli_overrides, config_path=config_path,
        )

    if transcript_path is None:
        transcript_path = workdir / step.id / "reflection_transcript.json"
    transcript_path.parent.mkdir(parents=True, exist_ok=True)
    cost_journal = workdir / "transcript.json"

    messages: list[dict[str, Any]] = [{"role": "user", "content": user_msg}]
    last_lesson: Optional[dict[str, str]] = None
    last_decline: bool = False

    for _ in range(max_turns):
        t0 = time.monotonic()
        try:
            resp: NormalizedResponse = client.call(
                messages=messages, system=sys_prompt, tools=REFLECTION_TOOLS,
                max_output_tokens=max_output_tokens,
            )
        except Exception:
            return None
        wall_ms = (time.monotonic() - t0) * 1000.0
        entry = _serialize_response(resp, llm_wall_ms=wall_ms)
        try:
            _append_transcript(transcript_path, entry)
            _append_transcript(cost_journal, entry)
        except OSError:
            pass

        asst_msg: dict[str, Any] = {"role": "assistant", "content": resp.text or None}
        if resp.tool_calls:
            asst_msg["tool_calls"] = [{
                "id": tc.id, "type": "function",
                "function": {"name": tc.name,
                             "arguments": json.dumps(tc.input, ensure_ascii=False)},
            } for tc in resp.tool_calls]
        if resp.reasoning_content:
            asst_msg["reasoning_content"] = resp.reasoning_content
        messages.append(asst_msg)

        if not resp.tool_calls:
            return None  # LLM produced only text; nothing to record

        for tc in resp.tool_calls:
            inp = dict(tc.input or {})
            if "_raw" in inp:
                messages.append({"role": "tool", "tool_call_id": tc.id,
                                 "content": f"tool args malformed: {str(inp.get('_raw'))[:200]}"})
                continue
            if tc.name != "submit_lesson":
                messages.append({"role": "tool", "tool_call_id": tc.id,
                                 "content": f"unknown tool: {tc.name!r}; call submit_lesson"})
                continue
            if inp.get("decline"):
                last_decline = True
                return None
            # Extract candidate lesson; orchestrator runs the validator on it.
            trigger = str(inp.get("trigger") or "").strip()
            action = str(inp.get("action") or "").strip()
            rationale = str(inp.get("rationale") or "").strip()
            if not (trigger and action and rationale):
                # Tell the LLM what's missing and let it retry.
                missing = [f for f in ("trigger", "action", "rationale")
                           if not str(inp.get(f) or "").strip()]
                messages.append({
                    "role": "tool", "tool_call_id": tc.id,
                    "content": (f"submit_lesson rejected: missing fields {missing}. "
                                "Either fill them in or set decline=true with a reason."),
                })
                continue
            last_lesson = {"trigger": trigger, "action": action, "rationale": rationale}
            return last_lesson

    return last_lesson if not last_decline else None
