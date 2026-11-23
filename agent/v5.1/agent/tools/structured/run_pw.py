"""run_pw — execute pw.x on a registered calc_id.

Routes through BashTool so every invocation lands in <workdir>/bash.log
(bench audit greps this for `pw.x` token and forbidden network commands).
Never returns raw stdout; just a short tail summary. Full output is read
on demand via read_qe_output.

Updates calc registry with output_path, exit_code, wall_s, status, summary.
"""

from __future__ import annotations

import asyncio
import os
import shlex
from dataclasses import dataclass
from pathlib import Path

import qe_tools
from agent.step_timeout_floor import floor_for_command
from agent.tools.structured.context import ToolContext

# Calc types that run a self-consistent field cycle — for these, electronic
# convergence is meaningful and a non-converged result must not pass as
# "completed". nscf/bands/dos read an existing density and don't print the
# SCF convergence marker, so they are exempt.
_SCF_LIKE = frozenset({"scf", "relax", "vc-relax"})


@dataclass
class _RunHealth:
    ok: bool
    reason: str = ""


def _job_done(out_path: Path) -> bool:
    try:
        with out_path.open("rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - 65536))
            return b"JOB DONE." in f.read()
    except OSError:
        return False


def _run_health(out_path: Path, calc_type: str) -> _RunHealth:
    """Post-exit physical-health gate.

    A zero exit code is necessary but not sufficient: pw.x exits 0 even when
    it writes "convergence NOT achieved", and a run killed mid-write can exit
    0 without ever reaching "JOB DONE.". Such runs must not be registered as
    `completed`, or the stuck-detector and the LLM treat a physically-wrong
    run as a success (C4).
    """
    if not _job_done(out_path):
        return _RunHealth(False, "no 'JOB DONE.' marker (run truncated or killed)")
    if calc_type in _SCF_LIKE:
        try:
            parsed = qe_tools.parse_pw_output(str(out_path))
        except Exception:
            # Parse trouble: don't false-fail; verify_run will catch real issues.
            return _RunHealth(True)
        if not parsed.get("convergence"):
            return _RunHealth(False, "SCF convergence NOT achieved")
    return _RunHealth(True)


def _summarize_output(out_path: Path, *, max_lines: int = 80) -> str:
    """Concise post-run summary: parse key fields + JOB DONE check + tail snippet."""
    parts: list[str] = []

    try:
        parsed = qe_tools.parse_pw_output(str(out_path))
        parts.append(
            f"convergence={parsed.get('convergence')} "
            f"total_energy={parsed.get('total_energy')} eV "
            f"fermi={parsed.get('fermi_energy')} eV "
            f"nbnd={parsed.get('nbnd')} nkpts={parsed.get('nkpts')}"
        )
        if parsed.get("forces"):
            try:
                import numpy as np
                f = np.asarray(parsed["forces"])
                if f.size:
                    parts.append(f"max|F|={float(np.max(np.linalg.norm(f, axis=1))):.4g} eV/Å")
            except Exception:  # pragma: no cover  (numpy/forces shape oddities)
                pass
        if parsed.get("stress_kbar") is not None:
            try:
                import numpy as np
                s = np.asarray(parsed["stress_kbar"])
                if s.size:
                    parts.append(f"max|σ|={float(np.max(np.abs(s))):.4g} kbar")
            except Exception:  # pragma: no cover
                pass
        if parsed.get("nstep_ions"):
            parts.append(f"ion_steps={parsed['nstep_ions']}")
    except Exception as e:
        parts.append(f"(parse failed: {type(e).__name__}: {e})")

    # JOB DONE in last 64 KB?
    job_done = False
    try:
        with out_path.open("rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - 65536))
            job_done = b"JOB DONE." in f.read()
    except OSError:
        pass
    parts.append(f"JOB_DONE={job_done}")

    # Plus last `max_lines` of output (truncated to 2000 chars)
    tail_text = ""
    try:
        with out_path.open("r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        tail_text = "".join(lines[-max_lines:])
        if len(tail_text) > 2000:
            tail_text = tail_text[-2000:]
    except OSError:
        pass

    summary = " | ".join(parts)
    if tail_text:
        summary += f"\n--- last {max_lines} lines (truncated to 2000 chars) ---\n{tail_text}"
    return summary


async def do_run_pw(args: dict, *, ctx: ToolContext) -> dict:
    """Run pw.x on a prepared input.

    Args:
        calc_id    str — must already be registered via write_qe_input
        timeout_s  int — wallclock cap (default 1800 = 30 min)
        np         int — MPI rank count (optional; uses $QE_GPU_NP or 1)
    """
    try:
        calc_id = args["calc_id"]
        rec = ctx.calc_registry.get(calc_id)
        if rec is None:
            raise KeyError(f"unknown calc_id: {calc_id!r}")
        timeout_s = int(args.get("timeout_s", 1800))
        np_ranks = int(args.get("np", os.environ.get("QE_GPU_NP", 1)))

        input_abs = ctx.rel(rec.input_path)
        if not input_abs.exists():
            raise FileNotFoundError(f"input file missing: {rec.input_path}")

        # Output: same dir as input, suffix swapped .in → .out
        out_rel = str(Path(rec.input_path).with_suffix(".out"))
        out_abs = ctx.rel(out_rel)

        # Invoke. Single-rank uses bare pw.x (wrapper handles GPU); >1 uses mpirun.
        in_q  = shlex.quote(str(input_abs))
        out_q = shlex.quote(str(out_abs))
        cmd = (f"mpirun -np {np_ranks} pw.x -in {in_q} > {out_q} 2>&1"
               if np_ranks > 1
               else f"pw.x -in {in_q} > {out_q} 2>&1")

        # Floor the wallclock cap to a step-kind-aware minimum so an LLM (or
        # the 1800s default) can't undershoot a heavy QE step and get pw.x
        # killed mid-run. max(llm_timeout, floor) — overshoot is fine.
        floor = floor_for_command(cmd, ctx.bash_tool.workdir)
        timeout_s = max(timeout_s, floor)

        ctx.calc_registry.update(calc_id, status="running", bash_command=cmd)

        # BashTool.run is sync — run in a thread so we don't block the event loop.
        result = await asyncio.to_thread(ctx.bash_tool.run, cmd, timeout_s)

        exit_code = result["exit_code"]
        wall_s = float(result["wall_s"])
        status = "completed" if exit_code == 0 and out_abs.exists() else "failed"
        summary = _summarize_output(out_abs) if out_abs.exists() else (result.get("stderr") or "")

        # A clean exit is not enough: a non-converged or truncated run must not
        # register as completed (C4), else the stuck-detector and LLM see it as
        # success and never trigger a fix.
        health_reason: str | None = None
        if status == "completed":
            health = _run_health(out_abs, rec.calc_type)
            if not health.ok:
                status = "failed"
                health_reason = health.reason

        # Capture a short error signature for the stuck-detector
        err_sig: str | None = None
        if status == "failed":
            err_sig = health_reason or (result.get("stderr") or "")[:200] or summary[:200]

        ctx.calc_registry.update(
            calc_id,
            status=status,
            output_path=out_rel,
            exit_code=exit_code,
            wall_s=wall_s,
            summary=summary,
            error_signature=err_sig,
        )

        # Human-readable text content for the LLM
        health_line = f"\n⚠ marked failed: {health_reason}" if health_reason else ""
        head = (
            f"calc_id={calc_id} status={status} exit={exit_code} wall={wall_s:.1f}s"
            f"{health_line}\n"
            f"output={out_rel}\n\n{summary}"
        )

        return {
            "content": [{"type": "text", "text": head}],
            "is_error": status == "failed",
            "calc_id": calc_id,
            "status": status,
            "exit_code": exit_code,
            "wall_s": wall_s,
            "output_path": out_rel,
        }
    except Exception as e:
        return {
            "content": [{"type": "text", "text": f"run_pw failed: {type(e).__name__}: {e}"}],
            "is_error": True,
        }
