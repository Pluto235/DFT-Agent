"""Post-bash hook: parse pw.x output files into structured facts (PR-B).

When the bash tool detects a pw.x invocation, it calls into this module
AFTER the subprocess returns. We:

  1. Locate the redirected output file from the bash command.
  2. Parse it via `qe_tools.parse_pw_output` plus a few extra rules
     (JOB DONE marker, SCF iteration count, error signature).
  3. Write a structured row to `<workdir>/qe_facts.jsonl` — this is the
     ground-truth source for the verifier subagent.
  4. Hand back a short advisory string the bash tool appends to the LLM
     tool_result so the executor sees structured convergence at-a-glance.

Two-surface design:
  - `qe_facts.jsonl` is the truth source; verifier reads it.
  - The `--- qe_facts ---` advisory line in tool_result is informational
    for the executor LLM — never authoritative on its own.

All operations are best-effort: any parse exception → None / "" return,
no log to stderr (audit greppability), no crash propagated to bash tool.
"""
from __future__ import annotations

import json
import re
import shlex
import time
from pathlib import Path
from typing import Any, Optional


# Output paths considered for `pw.x ... > FILE` redirect detection.
# Strict: only stdout redirects (>, >>, &>, &>>). Heredoc-piped invocations
# without a redirect target are not auto-parsed — the LLM can still parse
# them itself via bash; we just don't volunteer facts.
_REDIRECT_RE = re.compile(r"(?:&?>>?|2>&1\s*>)\s*([^\s|;&]+)")
_PWX_TOKEN_RE = re.compile(r"(?:^|[\s/])pw\.x(?:\s|$)")


# ─── Public API ──────────────────────────────────────────────────────────────


def is_pw_x_invocation(command: str) -> bool:
    """True if the bash command appears to invoke pw.x (directly or via mpirun)."""
    return _PWX_TOKEN_RE.search(command) is not None


def detect_output_path(command: str, cwd: Path) -> Optional[Path]:
    """Resolve the `> output.out` redirect target relative to cwd.

    Returns None when no stdout redirect is found (heredoc / pipe / etc.).
    Refuses absolute paths or paths escaping cwd (defence-in-depth — the
    bash tool already has its own escape guard, this is belt-and-braces).
    """
    matches = _REDIRECT_RE.findall(command)
    if not matches:
        return None
    target = matches[-1]    # last redirect wins
    # Strip shell-quote chars if any.
    try:
        target = shlex.split(target)[0]
    except ValueError:
        return None
    p = (cwd / target).resolve()
    try:
        p.relative_to(cwd.resolve())
    except ValueError:
        return None
    return p


def extract_facts(out_path: Path) -> Optional[dict[str, Any]]:
    """Parse a pw.x output file into a structured facts dict.

    Returns None if the file is missing, unreadable, or the parser crashes.
    Otherwise returns a stable-shape dict with these keys (any may be None):

      converged       : bool   — SCF convergence flag from parse_pw_output
      job_done        : bool   — literal "JOB DONE." marker in the last 64KB
      total_energy_ev : float | None
      n_scf_iterations: int | None
      max_force_ev_per_a: float | None
      max_stress_kbar : float | None    (max |component| over the 3×3 tensor)
      nstep_ions      : int | None
      wall_time_s     : float | None    (file mtime - ctime as a rough proxy)
      error_signature : str | None      (one-line excerpt if abnormal exit)
    """
    if not out_path.is_file():
        return None
    try:
        from qe_tools import parse_pw_output
    except Exception:
        return None
    try:
        raw = parse_pw_output(str(out_path))
    except Exception:
        return None

    # JOB DONE marker — bench audit relies on this string in the LAST 64 KB.
    job_done = False
    try:
        sz = out_path.stat().st_size
        with out_path.open("rb") as f:
            if sz > 64 * 1024:
                f.seek(sz - 64 * 1024)
            tail = f.read().decode("utf-8", errors="replace")
        job_done = "JOB DONE." in tail
    except OSError:
        tail = ""

    # SCF iteration count — parse_pw_output doesn't surface this. Count
    # "iteration #" occurrences in the file (cheap; bounded by I/O above).
    n_scf = None
    try:
        with out_path.open("r", encoding="utf-8", errors="replace") as f:
            text = f.read()
        n_scf = len(re.findall(r"iteration\s*#\s*\d+", text))
        if n_scf == 0:
            n_scf = None
    except OSError:
        text = ""

    # Force max norm over per-atom forces (eV/Å).
    max_force = None
    try:
        if raw.get("forces"):
            norms = []
            for f3 in raw["forces"]:
                if len(f3) >= 3:
                    norms.append((f3[0] ** 2 + f3[1] ** 2 + f3[2] ** 2) ** 0.5)
            if norms:
                max_force = max(norms)
    except (TypeError, ValueError):
        pass

    # Stress max abs (kbar) over the 3×3 tensor.
    max_stress = None
    try:
        if raw.get("stress_kbar"):
            flat = [abs(v) for row in raw["stress_kbar"] for v in row]
            if flat:
                max_stress = max(flat)
    except (TypeError, ValueError):
        pass

    # Wall time: proxy via mtime - ctime if both are sensible; otherwise None.
    wall_s = None
    try:
        st = out_path.stat()
        delta = st.st_mtime - st.st_ctime
        if delta > 0:
            wall_s = round(float(delta), 3)
    except OSError:
        pass

    # Error signature: look for known abnormal-exit lines in the tail.
    error_signature = None
    if not raw.get("convergence"):
        for pat in (
            r"convergence NOT achieved",
            r"%%%%%%%%%%%.*Error.*%%%%%%%%%%%",
            r"from\s+\w+\s*:\s*error\s+#\s*\d+",
            r"forrtl:\s+severe",
        ):
            m = re.search(pat, text or tail, re.IGNORECASE)
            if m:
                error_signature = m.group(0)[:200]
                break

    return {
        "converged": bool(raw.get("convergence")),
        "job_done": job_done,
        "total_energy_ev": raw.get("total_energy"),
        "n_scf_iterations": n_scf,
        "max_force_ev_per_a": max_force,
        "max_stress_kbar": max_stress,
        "nstep_ions": raw.get("nstep_ions"),
        "wall_time_s": wall_s,
        "error_signature": error_signature,
    }


def append_facts_row(
    workdir: Path,
    *,
    command: str,
    output_path: Path,
    facts: Optional[dict[str, Any]],
) -> None:
    """Append a single JSONL row to `<workdir>/qe_facts.jsonl`. Best-effort.

    Row shape:
        {at, command, output_path (workdir-relative), step_id, facts | parse_error}

    `step_id` is inferred from the first path component of output_path
    relative to workdir — i.e. for `scf_1/scf.out`, step_id = "scf_1".
    The verifier filters by step_id when reading.
    """
    try:
        workdir = workdir.resolve()
        rel = output_path.resolve().relative_to(workdir)
    except (OSError, ValueError):
        rel = output_path

    step_id = rel.parts[0] if rel.parts else None

    row: dict[str, Any] = {
        "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "command": command[:500],
        "output_path": str(rel),
        "step_id": step_id,
    }
    if facts is None:
        row["parse_error"] = "extract_facts returned None"
    else:
        row["facts"] = facts

    try:
        path = workdir / "qe_facts.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except OSError:
        return


def render_advisory(facts: Optional[dict[str, Any]]) -> str:
    """Render a ≤200-char single-line advisory string for the bash tool_result.

    Returns "" when facts is None (parse failed) — caller should not append
    anything to stdout in that case.
    """
    if not facts:
        return ""
    parts = []
    if facts.get("job_done"):
        parts.append("JOB_DONE")
    else:
        parts.append("NO_JOB_DONE")
    if facts.get("converged"):
        parts.append("converged")
    if (n := facts.get("n_scf_iterations")) is not None:
        parts.append(f"scf_iter={n}")
    if (te := facts.get("total_energy_ev")) is not None:
        parts.append(f"E_tot={te:.4f}eV")
    if (mf := facts.get("max_force_ev_per_a")) is not None:
        parts.append(f"|F|max={mf:.2e}eV/A")
    if (ms := facts.get("max_stress_kbar")) is not None:
        parts.append(f"|σ|max={ms:.2f}kbar")
    if (ni := facts.get("nstep_ions")):
        parts.append(f"ion_steps={ni}")
    if (w := facts.get("wall_time_s")) is not None:
        parts.append(f"wall={w:.1f}s")
    if (err := facts.get("error_signature")):
        parts.append(f"err={err[:40]!r}")
    line = "  ".join(parts)
    if len(line) > 200:
        line = line[:197] + "..."
    return f"--- qe_facts --- {line}"
