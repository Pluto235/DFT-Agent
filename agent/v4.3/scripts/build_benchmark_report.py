#!/usr/bin/env python3
"""Aggregate dft-bench-solver benchmark runs into a markdown report.

Usage:
    python scripts/build_benchmark_report.py \
        --runs-root /tmp/bench-eval-2026-05-09 \
        --out docs/benchmark_v1.md

For each per-task subdirectory under runs-root the script:
  - finds the harness-emitted report.json
  - reads workdir/transcript.json for turn count + token usage
  - extracts QE WALL from any *.out file (informational)
  - emits per-task and aggregate markdown tables

Sonnet-4-6 list pricing used for cost estimation: $3 / 1M input,
$15 / 1M output (Anthropic public price as of writing).
"""
from __future__ import annotations
import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

# Sonnet-4-6 list price (USD per token)
SONNET_IN = 3.0e-6
SONNET_OUT = 15.0e-6
HAIKU_IN = 0.8e-6
HAIKU_OUT = 4.0e-6

# Atom counts (for the 5 selected tasks)
ATOMS = {
    "M1_T1_Si_vc_relax": 2,
    "M1_T1_NaCl_vc_relax": 2,
    "M1_T1_Cu_vc_relax": 1,
    "M1_T1_SiO2_alpha_quartz_vc_relax": 9,
    "M2_T1_LiFePO4_olivine_vc_relax": 28,
}

NOTES = {
    "M1_T1_Si_vc_relax": "baseline (cubic semiconductor)",
    "M1_T1_NaCl_vc_relax": "ionic insulator",
    "M1_T1_Cu_vc_relax": "fcc metal — needs smearing",
    "M1_T1_SiO2_alpha_quartz_vc_relax": "trigonal, P3₂21 chirality",
    "M2_T1_LiFePO4_olivine_vc_relax": "M2 tier, 28 atoms, Fe magnetic (PBE non-magnetic OK)",
}


def find_run_dir(task_dir: Path) -> Path | None:
    """Harness creates a child dir like 2026-05-09T...Z_<task_id>. Pick the latest."""
    cands = sorted([p for p in task_dir.iterdir() if p.is_dir()], reverse=True)
    return cands[0] if cands else None


def parse_qe_wall(workdir: Path) -> float | None:
    """Best-effort extraction of QE 'PWSCF ... WALL' from any vc-relax .out file."""
    pat = re.compile(r"PWSCF\s*:\s*([\d.]+)s\s*CPU\s+([\d.]+)s\s*WALL")
    best = 0.0
    for out in workdir.rglob("*.out"):
        try:
            for line in out.read_text(encoding="utf-8", errors="replace").splitlines():
                m = pat.search(line)
                if m:
                    best += float(m.group(2))
        except Exception:
            pass
    return best if best > 0 else None


def transcript_stats(workdir: Path) -> tuple[int, int, int, int, int]:
    """Return (turns, charged_in, charged_out, cache_read, cache_create)."""
    p = workdir / "transcript.json"
    if not p.exists():
        return (0, 0, 0, 0, 0)
    try:
        ts = json.loads(p.read_text())
    except Exception:
        return (0, 0, 0, 0, 0)
    turns = len(ts)
    def s(k):
        return sum((t.get("usage") or {}).get(k, 0) or 0 for t in ts)
    return turns, s("input_tokens"), s("output_tokens"), \
           s("cache_read_input_tokens"), s("cache_creation_input_tokens")


def fmt_score(s: Any) -> str:
    if s is None:
        return "—"
    return f"{s:.3f}".rstrip("0").rstrip(".") or "0"


def fmt_audit(audit: dict | None) -> str:
    if not audit:
        return "—"
    n_pass = sum(1 for v in audit.values() if v is True)
    return f"{n_pass}/6"


def fmt_int(n: int | None) -> str:
    return "—" if n in (None, 0) else f"{n:,}"


def fmt_float(x: float | None, prec: int = 1) -> str:
    if x is None:
        return "—"
    return f"{x:.{prec}f}"


def fmt_dollars(d: float | None) -> str:
    return "—" if d is None else f"${d:.3f}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs-root", required=True, type=Path,
                    help="root that contains per-task subdirs")
    ap.add_argument("--out", required=True, type=Path,
                    help="markdown file to write")
    ap.add_argument("--model", default="claude-sonnet-4-6",
                    help="model id (selects pricing): sonnet or haiku")
    args = ap.parse_args()

    if "haiku" in args.model:
        in_price, out_price = HAIKU_IN, HAIKU_OUT
    else:
        in_price, out_price = SONNET_IN, SONNET_OUT

    if not args.runs_root.is_dir():
        print(f"runs-root not found: {args.runs_root}", file=sys.stderr)
        return 1

    # The 5 task subdirectories follow naming `<task_id>_<cpu|gpu>`.
    # Iterate in the canonical benchmark order.
    canonical = [
        ("M1_T1_Si_vc_relax", "gpu"),
        ("M1_T1_NaCl_vc_relax", "gpu"),
        ("M1_T1_Cu_vc_relax", "cpu"),
        ("M1_T1_SiO2_alpha_quartz_vc_relax", "cpu"),
        ("M2_T1_LiFePO4_olivine_vc_relax", "cpu"),
    ]

    rows: list[dict] = []
    for task_id, path_kind in canonical:
        task_dir = args.runs_root / f"{task_id}_{path_kind}"
        if not task_dir.is_dir():
            rows.append({"task_id": task_id, "path": path_kind, "missing": True})
            continue
        run = find_run_dir(task_dir)
        if not run:
            rows.append({"task_id": task_id, "path": path_kind, "missing": True})
            continue
        report_p = run / "report.json"
        if not report_p.exists():
            rows.append({"task_id": task_id, "path": path_kind, "run_dir": str(run),
                         "missing": True})
            continue
        rep = json.loads(report_p.read_text())
        wd = run / "workdir"
        turns, tin, tout, cread, ccreate = transcript_stats(wd)
        # Anthropic prompt cache pricing: read = 10% of input, create = 125%.
        cost = (tin * in_price + cread * in_price * 0.1
                + ccreate * in_price * 1.25 + tout * out_price)
        rows.append({
            "task_id": task_id,
            "path": path_kind,
            "score": rep.get("total_score"),
            "audit": rep.get("audit_flags"),
            "wall": rep.get("solver_wall_clock_sec"),
            "qe_wall": parse_qe_wall(wd),
            "turns": turns,
            "tin": tin,
            "tout": tout,
            "cread": cread,
            "ccreate": ccreate,
            "cost": cost,
            "failure": rep.get("failure_reason"),
            "model": rep.get("model", args.model),
        })

    # ── render markdown ──
    md: list[str] = []
    md.append(f"# v1 Benchmark — 5 vc_relax Tasks ({args.model})\n")
    md.append(f"Source data: `{args.runs_root}`\n")

    md.append("## Per-task results\n")
    md.append("| # | Task | atoms | Path | score | audit | solver wall (s) | QE wall (s) | turns | in / out / cache_r / cache_w | cost | notes |")
    md.append("|---|---|---:|---|---:|---|---:|---:|---:|---|---:|---|")
    for i, r in enumerate(rows, 1):
        atoms = ATOMS.get(r["task_id"], "?")
        note = NOTES.get(r["task_id"], "")
        if r.get("missing"):
            md.append(f"| {i} | `{r['task_id']}` | {atoms} | {r['path'].upper()} | — | — | — | — | — | — | — | **MISSING** ({note}) |")
            continue
        score = fmt_score(r["score"])
        if r.get("failure"):
            note = (note + " · " + r["failure"][:60]) if note else r["failure"][:80]
        md.append(
            f"| {i} | `{r['task_id']}` | {atoms} | {r['path'].upper()} | {score} | "
            f"{fmt_audit(r['audit'])} | {fmt_float(r['wall'])} | {fmt_float(r['qe_wall'])} | "
            f"{fmt_int(r['turns'])} | "
            f"{fmt_int(r['tin'])}→{fmt_int(r['tout'])} / {fmt_int(r.get('cread'))} / {fmt_int(r.get('ccreate'))} | "
            f"{fmt_dollars(r['cost'])} | {note} |"
        )
    md.append("")

    # ── aggregate ──
    md.append("## Aggregate\n")
    valid = [r for r in rows if not r.get("missing") and r.get("score") is not None]
    n_passed = sum(1 for r in valid if r["score"] >= 0.99 and fmt_audit(r["audit"]) == "6/6")
    md.append(f"- Pass rate (score≥0.99 ∧ audit 6/6): **{n_passed}/{len(canonical)}**")
    md.append(f"- Total LLM spend ({args.model}): **${sum(r.get('cost', 0) or 0 for r in valid):.2f}**")
    total_wall = sum((r.get("wall") or 0) for r in valid)
    md.append(f"- Total solver wall clock (sequential): **{total_wall/60:.1f} min**")
    md.append(f"- Mean turns per run: {sum(r.get('turns', 0) for r in valid) / max(1, len(valid)):.1f}")
    md.append("")

    # ── audit-flag breakdown ──
    md.append("## Audit-flag breakdown\n")
    flag_keys = ("qe_invoked", "job_done", "paths_in_workdir", "files_nonempty",
                 "qe_wallclock_ok", "no_network_access")
    md.append("| Task | " + " | ".join(flag_keys) + " |")
    md.append("|---|" + "|".join(":---:" for _ in flag_keys) + "|")
    for r in rows:
        if r.get("missing") or not r.get("audit"):
            md.append(f"| `{r['task_id']}/{r['path']}` | " + " | ".join("—" for _ in flag_keys) + " |")
            continue
        md.append("| `{}/{}` | ".format(r["task_id"], r["path"]) + " | ".join(
            "✅" if r["audit"].get(k) is True else "❌" for k in flag_keys) + " |")
    md.append("")

    # ── findings stub ──
    md.append("## Findings\n")
    md.append("(Auto-generated stub. Edit by hand once results land.)\n")
    md.append(f"- Model: {args.model}")
    md.append(f"- Sonnet's M2 tier robustness: see LiFePO4 row above")
    md.append("- haiku Si baseline (prior session): CPU 235.8s wall, $0.60; GPU 127.5s wall, $0.32")
    md.append("")

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text("\n".join(md) + "\n", encoding="utf-8")
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
