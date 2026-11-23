"""read_qe_output — windowed view onto a QE .out file.

SWE-agent ACI rule: never dump megabytes of raw stdout into the model's
context. Each call returns at most a few hundred lines, scoped to a specific
"window" (tail, errors, forces, stress, final_cell, scf_steps, summary,
or grep:PATTERN).

Reads the path recorded in the calc_registry — the model passes calc_id,
not a path it might guess wrong.
"""

from __future__ import annotations

import re
from pathlib import Path

import qe_tools
from agent.tools.structured.context import ToolContext


_DEFAULT_MAX_LINES = 200
_HARD_CAP_LINES = 1000
_HARD_CAP_CHARS = 12_000


# error / warning sigils that pw.x emits when something went wrong
_ERR_PAT = re.compile(
    r"%%%%|^\s*Error|Stopping|forrtl:|convergence NOT achieved|"
    r"S matrix not positive|charge is wrong|too small mixing_beta|"
    r"eigenvalues not converged",
    re.IGNORECASE | re.MULTILINE,
)

# QE block headers (capture the line and its successors until blank)
_BLOCK_STARTS = {
    "forces":     re.compile(r"^\s*Forces acting on atoms.*$", re.MULTILINE),
    "stress":     re.compile(r"^\s*total\s+stress.*\(kbar\).*$", re.MULTILINE),
    "final_cell": re.compile(r"^\s*Begin final coordinates|^\s*CELL_PARAMETERS \(alat=", re.MULTILINE),
}


def _slice_lines(lines: list[str], n: int) -> str:
    return "".join(lines[-n:])


def _grep(lines: list[str], pattern: str, *, context: int = 0,
          max_hits: int = 50) -> str:
    try:
        pat = re.compile(pattern, re.IGNORECASE)
    except re.error as e:
        return f"[bad regex {pattern!r}: {e}]"
    hits: list[str] = []
    for i, ln in enumerate(lines):
        if pat.search(ln):
            if context:
                lo = max(0, i - context)
                hi = min(len(lines), i + context + 1)
                hits.append("".join(lines[lo:hi]))
                hits.append("--\n")
            else:
                hits.append(ln)
            if len(hits) >= max_hits:
                break
    return "".join(hits) if hits else "[no matches]"


def _block(lines: list[str], start_re: re.Pattern, *, max_block_lines: int = 30) -> str:
    text = "".join(lines)
    matches = list(start_re.finditer(text))
    if not matches:
        return "[block not found]"
    # use the LAST occurrence (usually the most recent / converged one)
    start = matches[-1].start()
    # find line index for start position
    head = text[:start]
    start_line = head.count("\n")
    block = lines[start_line:start_line + max_block_lines]
    return "".join(block)


def _scf_steps(lines: list[str]) -> str:
    """Extract one line per SCF iteration with iter# and dE."""
    rows: list[str] = []
    for i, ln in enumerate(lines):
        m = re.match(r"\s*iteration\s*#\s*(\d+)\s+ecut.*", ln)
        if m:
            it = int(m.group(1))
            # estimated scf accuracy line typically appears within next 12 lines
            de = ""
            for j in range(i + 1, min(i + 14, len(lines))):
                m2 = re.search(r"estimated scf accuracy\s*<\s*([\d.E+-]+)\s*Ry", lines[j])
                if m2:
                    de = m2.group(1)
                    break
            rows.append(f"iter {it:3d}  dE={de}")
    return "\n".join(rows[-_DEFAULT_MAX_LINES:]) if rows else "[no SCF iterations found]"


def _summary_text(out_path: Path) -> str:
    try:
        d = qe_tools.parse_pw_output(str(out_path))
    except Exception as e:
        return f"[parse_pw_output failed: {type(e).__name__}: {e}]"
    fields = ("convergence", "total_energy", "fermi_energy", "nbnd", "nkpts",
              "alat", "final_alat", "volume", "final_volume", "nstep_ions",
              "mag_total", "mag_absolute")
    return "\n".join(f"{k} = {d.get(k)}" for k in fields)


async def do_read_qe_output(args: dict, *, ctx: ToolContext) -> dict:
    """Read a slice of a QE .out by calc_id.

    Args:
        calc_id    str
        window     one of {"tail","errors","forces","stress","final_cell",
                          "scf_steps","bands_table","summary"}  or "grep:PATTERN"
        max_lines  int (default 200, hard cap 1000)
    """
    try:
        calc_id = args["calc_id"]
        rec = ctx.calc_registry.get(calc_id)
        if rec is None:
            raise KeyError(f"unknown calc_id: {calc_id!r}")
        if not rec.output_path:
            raise FileNotFoundError(f"{calc_id} has no output yet (status={rec.status})")
        out_abs = ctx.rel(rec.output_path)
        if not out_abs.exists():
            raise FileNotFoundError(f"output file vanished: {rec.output_path}")

        window = args.get("window", "tail")
        max_lines = min(int(args.get("max_lines", _DEFAULT_MAX_LINES)), _HARD_CAP_LINES)

        with out_abs.open("r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()

        if window == "tail":
            text = _slice_lines(lines, max_lines)
        elif window == "errors":
            text = _grep(lines, _ERR_PAT.pattern, context=2, max_hits=20)
        elif window in _BLOCK_STARTS:
            text = _block(lines, _BLOCK_STARTS[window])
        elif window == "scf_steps":
            text = _scf_steps(lines)
        elif window == "summary":
            text = _summary_text(out_abs)
        elif window == "bands_table":
            # Quick heuristic — most useful from bands.out: list of k-pt energies
            text = _grep(lines, r"^\s*k\s*=", context=2, max_hits=40)
        elif window.startswith("grep:"):
            text = _grep(lines, window[len("grep:"):], context=1, max_hits=50)
        else:
            raise ValueError(
                f"unknown window {window!r}; expected one of "
                f"tail/errors/forces/stress/final_cell/scf_steps/bands_table/summary/grep:PATTERN")

        if len(text) > _HARD_CAP_CHARS:
            text = text[-_HARD_CAP_CHARS:]
            text = f"[truncated to last {_HARD_CAP_CHARS} chars]\n" + text

        return {
            "content": [{"type": "text", "text":
                f"--- {calc_id} {rec.output_path} window={window} ---\n{text}"}],
            "is_error": False,
            "calc_id": calc_id,
            "window": window,
            "lines_in_file": len(lines),
        }
    except Exception as e:
        return {
            "content": [{"type": "text", "text": f"read_qe_output failed: {type(e).__name__}: {e}"}],
            "is_error": True,
        }
