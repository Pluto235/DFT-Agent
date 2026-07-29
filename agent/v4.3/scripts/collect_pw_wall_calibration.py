#!/usr/bin/env python3
"""Harvest (n_atoms, n_kpoints, ecutwfc, calculation, actual_wall_s) tuples
from historical bench runs for pw_wall_estimator calibration.

Walk runs_local/, for each workdir/bash.log find `pw.x -in <input>` lines
with rc=0 and wall > 1.0s. For each such line, locate the .in file (relative
to bash log's recorded cwd, allowing `cd X && ...` prefix in the command),
parse n_atoms / n_kpoints / ecutwfc / calculation via the same helpers that
pw_wall_estimator uses, and emit one TSV row per (pw.x command, wall) pair.

Usage:
    python scripts/collect_pw_wall_calibration.py \\
        --runs-root /path/to/project/web/runs_local \\
        --out tests/calibration_data/pw_wall_pairs.tsv
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agent.pw_wall_estimator import (  # noqa: E402
    _extract_calculation,
    _extract_ecutwfc,
    _extract_n_kpoints,
    _extract_nat,
)
from agent.tools.qe_input_lint import _parse_input  # noqa: E402

# bash.log header line: [ISO ts] cwd=PATH rc=N wall=Fs
_HEADER_RE = re.compile(
    r"^\[(?P<ts>[^\]]+)\]\s+cwd=(?P<cwd>\S+)\s+rc=(?P<rc>-?\d+)\s+wall=(?P<wall>[\d.]+)s\s*$"
)
_CMD_RE = re.compile(r"^\s*\$ (?P<cmd>.+)$")
# pw.x invocation: capture input file from `-in <path>` or `< <path>`
_PWX_INPUT_RE = re.compile(r"\bpw\.x\b[^\n]*?(?:-in(?:put)?\s+|<\s*)([^\s<>|;&]+)")
# `cd X &&` prefix
_CD_PREFIX_RE = re.compile(r"^\s*cd\s+(\S+)\s*&&\s*(.*)$")


def parse_bash_log(path: Path):
    """Yield (rc, wall_s, recorded_cwd, cmd) tuples for every entry."""
    try:
        text = path.read_text(errors="replace")
    except OSError:
        return
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        m = _HEADER_RE.match(lines[i])
        if not m:
            i += 1
            continue
        # Subsequent lines until next header are the command (usually 1 line,
        # but multi-line heredocs span more). For our purposes we only need
        # the FIRST command line — pw.x is always on a single bash line.
        cmd_lines = []
        i += 1
        while i < len(lines) and not _HEADER_RE.match(lines[i]):
            cm = _CMD_RE.match(lines[i])
            if cm:
                cmd_lines.append(cm.group("cmd"))
                break  # only first $ line
            i += 1
        if cmd_lines:
            yield {
                "rc": int(m.group("rc")),
                "wall": float(m.group("wall")),
                "cwd": m.group("cwd"),
                "cmd": cmd_lines[0],
            }


def find_pwx_input(cmd: str, recorded_cwd: str) -> tuple[str | None, str]:
    """Extract the input file path from a `pw.x -in <file>` command.
    Returns (resolved_abs_path, effective_cwd)."""
    m = _CD_PREFIX_RE.match(cmd)
    if m:
        effective_cwd = m.group(1)
        rest = m.group(2)
    else:
        effective_cwd = recorded_cwd
        rest = cmd
    pm = _PWX_INPUT_RE.search(rest)
    if not pm:
        return None, effective_cwd
    inp = pm.group(1)
    if not os.path.isabs(inp):
        inp = os.path.join(effective_cwd, inp)
    return inp, effective_cwd


def features_from_input(inp_path: str) -> dict | None:
    """Parse n_atoms / n_kpoints / ecutwfc / calculation from a pw.x .in file."""
    p = Path(inp_path)
    if not p.is_file():
        return None
    try:
        text = p.read_text(errors="replace")
    except OSError:
        return None
    parsed = _parse_input(text)
    nat = _extract_nat(parsed)
    ecut = _extract_ecutwfc(parsed)
    nk = _extract_n_kpoints(text)
    calc = _extract_calculation(parsed)
    if nat is None or ecut is None or nk is None:
        return None
    if nat <= 0 or ecut <= 0 or nk <= 0:
        return None
    return {
        "n_atoms": nat,
        "n_kpoints": nk,
        "ecutwfc": ecut,
        "calculation": calc or "scf",
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs-root", required=True, type=Path)
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--min-wall", type=float, default=10.0,
                    help="Skip pw.x calls under this wall (avoid bash_guard rejections / smoke)")
    args = ap.parse_args()

    pairs = []
    seen_runs = 0
    seen_pwx = 0
    for run_dir in sorted(args.runs_root.iterdir()):
        if not run_dir.is_dir():
            continue
        # Skip hidden / pending
        if run_dir.name.startswith("."):
            continue
        seen_runs += 1
        bash_log = run_dir / "workdir" / "bash.log"
        if not bash_log.is_file():
            continue
        # task_id = strip leading timestamp from dir name
        parts = run_dir.name.split("_", 1)
        task_id = parts[1] if len(parts) > 1 else run_dir.name
        for entry in parse_bash_log(bash_log):
            if entry["rc"] != 0:
                continue
            if entry["wall"] < args.min_wall:
                continue
            if "pw.x" not in entry["cmd"]:
                continue
            seen_pwx += 1
            inp_path, _ = find_pwx_input(entry["cmd"], entry["cwd"])
            if inp_path is None:
                continue
            feats = features_from_input(inp_path)
            if feats is None:
                continue
            pairs.append({
                "task_id": task_id,
                "run_id": run_dir.name,
                "calculation": feats["calculation"],
                "n_atoms": feats["n_atoms"],
                "n_kpoints": feats["n_kpoints"],
                "ecutwfc": feats["ecutwfc"],
                "actual_wall_s": int(round(entry["wall"])),
            })

    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w") as f:
        f.write("task_id\trun_id\tcalculation\tn_atoms\tn_kpoints\tecutwfc\tactual_wall_s\n")
        for r in pairs:
            f.write(f"{r['task_id']}\t{r['run_id']}\t{r['calculation']}\t"
                    f"{r['n_atoms']}\t{r['n_kpoints']}\t{r['ecutwfc']}\t"
                    f"{r['actual_wall_s']}\n")
    print(f"Scanned {seen_runs} runs, found {seen_pwx} pw.x calls "
          f"(rc=0, wall≥{args.min_wall}s), wrote {len(pairs)} parseable pairs "
          f"to {args.out}", file=sys.stderr)


if __name__ == "__main__":
    main()
