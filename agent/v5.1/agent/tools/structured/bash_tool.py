"""bash — escape-hatch wrapper around v4's BashTool.

The structured tools cover the typical QE workflow (write/run/read/parse/
verify), but some operations have no first-class wrapper yet:
  - bands.x / dos.x / pp.x post-processing
  - seekpath k-path generation via heredoc'd Python
  - ad-hoc parsing not covered by read_qe_output windows

For those, the LLM can fall back to `mcp__dft_qe__bash`. Behaviour is
identical to v4's BashTool:
  - every command logged to <workdir>/bash.log (audit hard requirement)
  - qe_input_lint runs on any `pw.x` invocation (auto-tightens vc-relax)
  - bash_guard whitelist + denylist + path checks
  - background commands (&, nohup, disown) rejected
  - cwd persists ONLY for a bare `cd <dir>` line

stdout truncated to 8 KB / stderr to 4 KB before reaching the LLM
(SWE-agent ACI rule — never dump megabytes of raw output).
"""

from __future__ import annotations

import asyncio

from agent.step_timeout_floor import floor_for_command
from agent.tools.structured.context import ToolContext


_STDOUT_CAP = 8000
_STDERR_CAP = 4000


async def do_bash(args: dict, *, ctx: ToolContext) -> dict:
    """Execute a shell command.

    Args:
        command   str  — the command. Must not be backgrounded.
        timeout_s int  — wallclock cap (default 1800)
    """
    try:
        cmd = args["command"]
        if not isinstance(cmd, str) or not cmd.strip():
            return {
                "content": [{"type": "text", "text": "bash: empty command"}],
                "is_error": True,
            }
        timeout_s = int(args.get("timeout_s", 1800))

        # Floor the wallclock cap: non-pw.x commands keep a 60s floor (no-op for
        # quick ops), but a pw.x run via this escape hatch (bands.x/dos.x heredoc,
        # or someone bypassing run_pw) gets the same step-kind-aware floor so it
        # can't be killed mid-run. max(llm_timeout, floor) — overshoot is fine.
        floor = floor_for_command(cmd, ctx.bash_tool.workdir)
        timeout_s = max(timeout_s, floor)

        # BashTool.run is sync — offload to thread
        result = await asyncio.to_thread(ctx.bash_tool.run, cmd, timeout_s)

        stdout = (result.get("stdout") or "")[:_STDOUT_CAP]
        stderr = (result.get("stderr") or "")[:_STDERR_CAP]
        exit_code = result.get("exit_code", -1)
        wall_s = float(result.get("wall_s", 0.0))
        lint_diff = result.get("lint_diff") or ""
        cwd_after = result.get("cwd_after", "")

        parts: list[str] = [
            f"exit={exit_code} wall={wall_s:.1f}s cwd_after={cwd_after}",
        ]
        if lint_diff:
            parts.append(f"--- qe_input_lint ---\n{lint_diff}")
        if stdout:
            parts.append(f"--- stdout (truncated to {_STDOUT_CAP} chars) ---\n{stdout}")
        if stderr:
            parts.append(f"--- stderr (truncated to {_STDERR_CAP} chars) ---\n{stderr}")
        text = "\n".join(parts)

        return {
            "content": [{"type": "text", "text": text}],
            "is_error": exit_code != 0,
            "exit_code": exit_code,
            "wall_s": wall_s,
            "cwd_after": cwd_after,
        }
    except Exception as e:
        return {
            "content": [{"type": "text", "text": f"bash failed: {type(e).__name__}: {e}"}],
            "is_error": True,
        }
