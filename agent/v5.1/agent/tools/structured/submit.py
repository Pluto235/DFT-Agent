"""submit — LLM signals 'I'm done; here's the result'.

claude-agent-sdk has no built-in 'finalize' tool. We model termination by
having the LLM call mcp__dft_qe__submit({task_outputs, notes, incomplete}).
The Driver polls ctx.submit_payload after each SDK message and breaks the
loop the moment it sees a value.

solve.py then runs verify_run on each `task_outputs` path as the final gate
before writing result.json.
"""

from __future__ import annotations

from agent.tools.structured.context import ToolContext


async def do_submit(args: dict, *, ctx: ToolContext) -> dict:
    """Capture the final submission payload.

    Args:
        task_outputs  dict[str, str] mapping required keys to workdir-relative paths
                      (e.g., {"vc_relax_out": "si_vc_relax.out"})
        notes         str (optional, free-form)
        incomplete    bool (optional, default False — set True when giving up)
    """
    task_outputs = args.get("task_outputs") or {}
    notes = args.get("notes") or ""
    incomplete = bool(args.get("incomplete", False))

    if not isinstance(task_outputs, dict):
        return {
            "content": [{"type": "text",
                         "text": f"submit: task_outputs must be dict, got {type(task_outputs).__name__}"}],
            "is_error": True,
        }

    # Set the sentinel; Driver loop reads this after each message.
    payload = {
        "task_outputs": task_outputs,
        "notes": notes,
        "incomplete": incomplete,
    }
    ctx.submit_payload = payload

    head = (
        f"Submission accepted (incomplete={incomplete}).\n"
        f"task_outputs: {task_outputs}\n"
        + (f"notes: {notes}\n" if notes else "")
        + "Driver will now run the program-level verifier gate."
    )
    return {
        "content": [{"type": "text", "text": head}],
        "is_error": False,
        "submitted": True,
        **payload,
    }
