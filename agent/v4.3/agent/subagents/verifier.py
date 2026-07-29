"""VerifierAgent — v4.3 通用 verifier subagent。

读 step.expected_outputs 路径 + step kind 提供的 checklist，决定 pass/fail。
工具集小且安全（read_file / list_dir / submit_verdict），用便宜模型即可。

为什么不复用 run_loop？因为：
- verifier 的 submit 工具是 submit_verdict（强 schema），不是 generic submit
- verifier 不应跑 bash（避免动文件，避免 charge density 误删）
- verifier 失败时返回 VerdictSpec 而非 generic dict

工具说明：
- read_file: 读文件（已存在的工具）
- list_dir:  列目录（轻量补一个）
- submit_verdict: 终止，返回 {pass: bool, diagnosis: str}
"""
from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from pydantic import ValidationError

from agent.ledger import (
    SUBMIT_VERDICT_SCHEMA,
    VerdictSpec,
    format_validation_error,
    parse_verdict,
    PlanStep,
)
from agent.llm import LLMClient, NormalizedResponse
from agent.loop import _append_transcript, _serialize_response  # noqa: F401
from agent.step_kinds import get as get_step_kind
from agent.tools.file_io import read_file


@dataclass
class VerifierResult:
    session_id: str
    verdict: VerdictSpec     # always set, even on internal failure (synthesized)
    turns: int


VERIFIER_TOOLS: list[dict[str, Any]] = [
    {
        "name": "read_file",
        "description": "Read a text file. Optionally slice by line number.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "offset": {"type": "integer", "default": 0},
                "limit": {"type": "integer"},
            },
            "required": ["path"],
        },
    },
    {
        "name": "list_dir",
        "description": "List entries of a directory (non-recursive). Returns names + sizes.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
            },
            "required": ["path"],
        },
    },
    {
        "name": "submit_verdict",
        "description": (
            "Submit your final pass/fail verdict. After this call, the verifier "
            "loop ends. ALWAYS call this exactly once."
        ),
        "parameters": SUBMIT_VERDICT_SCHEMA,
    },
]


def _read_qe_facts_for_step(main_workdir: Path, step_id: str,
                             *, max_bytes: int = 5 * 1024) -> str:
    """Read `<main_workdir>/qe_facts.jsonl`, filter rows by step_id, render
    a `===== QE FACTS =====` markdown block. Empty string if no rows match.

    Truth-source contract (PR-B): the JSONL rows are authoritative on pw.x
    convergence. Verifier should NOT re-grep .out files for JOB DONE when
    a row for that step exists.
    """
    path = main_workdir / "qe_facts.jsonl"
    if not path.is_file():
        return ""
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    matching: list[str] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if row.get("step_id") == step_id:
            matching.append(line)
    if not matching:
        return ""
    body = "\n".join(matching)
    if len(body) > max_bytes:
        # Keep the most recent rows (tail) — older invocations are usually
        # superseded by retries that came after.
        body = body[-max_bytes:]
        nl = body.find("\n")
        if nl > 0:
            body = body[nl + 1:]
    return (
        "===== QE FACTS =====\n"
        "(post-bash hook output for this step's pw.x invocations — "
        "treat as authoritative on convergence/JOB DONE/forces/stress)\n"
        f"{body}\n"
        "===== END QE FACTS =====\n"
    )


def _verifier_system_prompt(step: PlanStep, main_workdir: Path, checklist: str) -> str:
    paths = "\n".join(
        f"  - {k!r}: workdir-relative `{p}`" for k, p in step.expected_outputs.items()
    )
    return (
        "You are a verifier subagent for a DFT (Quantum ESPRESSO) calculation pipeline.\n"
        "Your job: decide whether the executor's outputs for ONE step satisfy the "
        "kind-specific checklist below, and report pass/fail with a concrete "
        "diagnosis. You CANNOT modify files. You SHOULD inspect them.\n\n"
        f"## Step under review\n"
        f"- id: `{step.id}`\n"
        f"- kind: `{step.kind}`\n"
        f"- goal: {step.desc}\n"
        f"- main workdir (your read base): `{main_workdir}`\n\n"
        f"## Expected outputs (paths relative to main workdir)\n"
        f"{paths}\n\n"
        f"{checklist}\n\n"
        "## How to operate\n"
        "1. For each expected output, use `read_file` (for logs) or `list_dir` "
        "   (for directories like *.save) to confirm existence and content.\n"
        "   All paths you pass are interpreted relative to the main workdir.\n"
        "2. For log files, read the tail (last ~100 lines) to find JOB DONE / "
        "   error messages.\n"
        "3. If the user message contains a `===== QE FACTS =====` block, treat "
        "   those parsed rows as authoritative on convergence and JOB DONE for "
        "   the pw.x invocations they describe. You don't need to re-grep .out "
        "   files for the same signal — use the block and spend your read budget "
        "   on signals the block does NOT cover (file sizes, data shape, etc.).\n"
        "4. When you have enough evidence, call `submit_verdict` exactly once.\n"
        "5. Be concrete in diagnosis on failure — name the missing file, the "
        "   missing marker, or the bad value. On success, diagnosis can be empty."
    )


def _read_subagent_tool(tool_name: str, inp: dict[str, Any], main_workdir: Path) -> str:
    """Dispatch the safe subset of tools verifier is allowed."""
    if tool_name == "read_file":
        path = inp.get("path")
        if not path:
            return json.dumps({"error": "read_file: missing 'path'"})
        result = read_file(
            path, cwd=str(main_workdir),
            offset=int(inp.get("offset") or 0),
            limit=int(inp["limit"]) if inp.get("limit") is not None else None,
        )
        return json.dumps(result, ensure_ascii=False)[:8000]
    if tool_name == "list_dir":
        path = inp.get("path")
        if not path:
            return json.dumps({"error": "list_dir: missing 'path'"})
        # Resolve relative to main_workdir for safety/consistency with read_file.
        target = (main_workdir / path).resolve() if not os.path.isabs(path) else Path(path)
        if not target.exists():
            return json.dumps({"error": f"path does not exist: {target}"})
        if not target.is_dir():
            return json.dumps({"error": f"not a directory: {target}"})
        entries = []
        for child in sorted(target.iterdir()):
            try:
                size = child.stat().st_size if child.is_file() else None
            except OSError:
                size = None
            entries.append({
                "name": child.name,
                "type": ("dir" if child.is_dir() else "file"),
                "size": size,
            })
        return json.dumps({"entries": entries}, ensure_ascii=False)[:8000]
    return json.dumps({"error": f"unknown tool: {tool_name!r}"})


class VerifierAgent:
    """One verifier per step. Cheap model (haiku/deepseek-chat). One-shot."""

    def __init__(
        self,
        *,
        step: PlanStep,
        main_workdir: Path,
        cli_overrides: Optional[dict[str, str]] = None,
        config_path: Optional[Path] = None,
        max_turns: int = 12,
        max_output_tokens: int = 2048,
        transcript_path: Optional[Path] = None,
        client: Optional[LLMClient] = None,
    ):
        self.step = step
        self.main_workdir = main_workdir
        self.kind_spec = get_step_kind(step.kind)
        self.client = client or LLMClient.from_role(
            "verifier", cli_overrides=cli_overrides, config_path=config_path,
        )
        self.max_turns = max_turns
        self.max_output_tokens = max_output_tokens
        self.transcript_path = transcript_path or (
            main_workdir / step.id / "verifier_transcript.json"
        )
        self.transcript_path.parent.mkdir(parents=True, exist_ok=True)
        # Cost journal: web reconciler reads only main_workdir/transcript.json.
        # Dual-write each verifier turn there too.
        self.cost_journal = main_workdir / "transcript.json"
        self.checklist = self.kind_spec.verifier_checklist(step)
        self.system_prompt = _verifier_system_prompt(step, main_workdir, self.checklist)

    def run(self) -> VerifierResult:
        session_id = uuid.uuid4().hex
        user_content = (
            "Inspect the expected outputs and submit your verdict. "
            "Use the available tools to read files / list directories first."
        )
        # PR-B: inject the post-bash qe_facts JSONL block (filtered to this
        # step's rows) so the verifier can short-circuit JOB-DONE/convergence
        # checks instead of re-reading .out tails.
        qe_facts_block = _read_qe_facts_for_step(self.main_workdir, self.step.id)
        if qe_facts_block:
            user_content = user_content + "\n\n" + qe_facts_block
        messages: list[dict[str, Any]] = [
            {"role": "user", "content": user_content},
        ]
        last_validation_error: Optional[str] = None

        for turn in range(self.max_turns):
            t0 = time.monotonic()
            resp: NormalizedResponse = self.client.call(
                messages=messages,
                system=self.system_prompt,
                tools=VERIFIER_TOOLS,
                max_output_tokens=self.max_output_tokens,
            )
            llm_wall_ms = (time.monotonic() - t0) * 1000.0
            entry = _serialize_response(resp, llm_wall_ms=llm_wall_ms)
            if self.transcript_path:
                _append_transcript(self.transcript_path, entry)
            _append_transcript(self.cost_journal, entry)

            asst_msg: dict[str, Any] = {
                "role": "assistant",
                "content": resp.text or None,
            }
            if resp.tool_calls:
                asst_msg["tool_calls"] = [{
                    "id": tc.id, "type": "function",
                    "function": {"name": tc.name,
                                 "arguments": json.dumps(tc.input, ensure_ascii=False)},
                } for tc in resp.tool_calls]
            # Thinking-mode models (deepseek-v4-pro, deepseek-reasoner, etc.)
            # require reasoning_content from prior turns to be passed back on
            # every subsequent API call — otherwise 400 "The reasoning_content
            # in the thinking mode must be passed back to the API."
            if resp.reasoning_content:
                asst_msg["reasoning_content"] = resp.reasoning_content
            messages.append(asst_msg)

            if not resp.tool_calls:
                # Verifier said nothing actionable; synthesize fail.
                return VerifierResult(
                    session_id=session_id,
                    verdict=VerdictSpec(**{"pass": False, "diagnosis":
                        f"verifier exited without calling submit_verdict on turn {turn + 1}"}),
                    turns=turn + 1,
                )

            tool_results: list[dict[str, Any]] = []
            for tc in resp.tool_calls:
                inp = dict(tc.input)
                if "_raw" in inp:
                    tool_results.append({
                        "role": "tool", "tool_call_id": tc.id,
                        "content": f"tool args malformed: {str(inp.get('_raw'))[:200]}",
                    })
                    continue
                if tc.name == "submit_verdict":
                    try:
                        verdict = parse_verdict(inp)
                    except ValidationError as e:
                        # Tell the verifier exactly what's wrong; let it retry.
                        err = format_validation_error(e)
                        last_validation_error = err
                        tool_results.append({
                            "role": "tool", "tool_call_id": tc.id,
                            "content": (
                                f"submit_verdict rejected: {err}\n"
                                "Retry with a valid payload."
                            ),
                        })
                        continue
                    messages.extend(tool_results)
                    return VerifierResult(
                        session_id=session_id, verdict=verdict, turns=turn + 1,
                    )
                # Otherwise: read_file / list_dir
                content = _read_subagent_tool(tc.name, inp, self.main_workdir)
                tool_results.append({
                    "role": "tool", "tool_call_id": tc.id, "content": content,
                })

            messages.extend(tool_results)

        # === FIX-17 (2026-05-23): conditional default-pass on budget exhaustion ===
        # The LLM-verifier failed to emit a verdict in max_turns, but the
        # executor's output may still be complete. Pre-FIX-17 we synthesized
        # a fail, which made the orchestrator retry the entire pw.x step
        # (the WO3 2026-05-23 regression burned 4h wall on 3 vc_relax reruns
        # + 1 strategy replan after a successful first attempt). FIX-17:
        # if every expected_output path exists on disk and is ≥ 1024 B
        # (matching the bench harness audit criterion in
        # solve.py:_audit_task_outputs), default to pass and let the
        # downstream harness make the final call. Otherwise preserve the
        # original fail behavior — that path catches executors that didn't
        # actually write the files.
        all_outputs_present = bool(self.step.expected_outputs)
        for rel_path in self.step.expected_outputs.values():
            full = self.main_workdir / rel_path
            try:
                if not full.is_file() or full.stat().st_size < 1024:
                    all_outputs_present = False
                    break
            except OSError:
                all_outputs_present = False
                break

        if all_outputs_present:
            diag = (
                f"verifier indecisive (exhausted turn budget {self.max_turns} "
                f"without submit_verdict), but executor's expected_outputs all "
                f"exist on disk and are ≥ 1024 B — defaulting to PASS. "
                f"Bench harness audit will re-verify JOB DONE / 1024 B at submission."
            )
            if last_validation_error:
                diag += (
                    f"\nNote: last submit_verdict attempt had schema errors:\n"
                    f"{last_validation_error}"
                )
            return VerifierResult(
                session_id=session_id,
                verdict=VerdictSpec(**{"pass": True, "diagnosis": diag}),
                turns=self.max_turns,
            )

        # Outputs missing / too small — original fail path.
        diag = (
            f"verifier exhausted turn budget ({self.max_turns}) without submitting; "
            f"expected_outputs check also failed (missing or < 1024 B)"
        )
        if last_validation_error:
            diag += f"; last submit_verdict had schema errors:\n{last_validation_error}"
        return VerifierResult(
            session_id=session_id,
            verdict=VerdictSpec(**{"pass": False, "diagnosis": diag}),
            turns=self.max_turns,
        )
