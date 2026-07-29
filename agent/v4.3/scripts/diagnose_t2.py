#!/usr/bin/env python3
"""Diagnose a single T2 (bands_dos) run — show per-field score breakdown.

This is the v4 Phase 1 diagnostic tool: given a harness run_dir, it
extracts the 6 T2 evaluator fields from the workdir's QE outputs and
prints what each field would score under bench's official rules.

It deliberately does NOT re-implement evaluation. It imports bench's
own `extract_t2_fields` + `score_fields` from `harness/lib/`, so the
numbers match what `harness/run.py` would compute on a fresh run.

Usage:
    python scripts/diagnose_t2.py <run_dir> [options]

Required input:
    <run_dir>      Harness run directory (contains workdir/result.json)
                   Either the proper `<UTC>_<task_id>` shape, OR pass
                   --task-id explicitly when the dir was renamed.

Options:
    --task-id TASK_ID         Override task_id (e.g. M1_T2_AlN_bands_dos)
    --bench-root PATH         dft-agent-bench checkout root
                              (default: ~/bench/dft-agent-bench)
    --questions-dir NAME      Questions subdir under bench-root
                              (default: questions)
    --out PATH                Also write a markdown report to this path
    --quiet                   Suppress informational lines (only print table)

Recovers from these cases automatically:
    * result.json is incomplete (no task_outputs): falls back to scanning
      workdir for known QE output filenames (scf.out / bands.out / dos.out
      / bands.dat / dos.dat) and synthesizes a default task_outputs.
    * task_outputs uses `dos_data` instead of `dos_dat` (bench accepts
      `dos_dat`): aliased automatically.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


# ─── bench import path ──────────────────────────────────────────────────────

def _ensure_bench_imports(bench_root: Path) -> None:
    bench_root = bench_root.resolve()
    if not bench_root.is_dir():
        raise SystemExit(
            f"[diagnose_t2] --bench-root not found: {bench_root}\n"
            f"  Pass --bench-root <path-to-dft-agent-bench> or set "
            f"DFT_AGENT_BENCH_ROOT env var."
        )
    if str(bench_root) not in sys.path:
        sys.path.insert(0, str(bench_root))


# ─── core: build task_outputs from result.json or workdir scan ─────────────

# T2 submission keys (canonical, per harness/rerun_eval.py).
_T2_KEYS = ("scf_out", "bands_out", "bands_data", "dos_dat")

# Aliases agents sometimes emit. We map them onto the canonical keys
# above so extract_t2_fields can find them.
_T2_KEY_ALIASES = {
    "dos_data": "dos_dat",
    "dos_out": "dos_dat",   # last-resort fallback; not always correct
}

# Default filename heuristics for fallback workdir scan.
_FALLBACK_FILES = {
    "scf_out":    ("scf.out", "pw.out", "relax.out"),
    "bands_out":  ("bands.out", "bands_nscf.out"),
    "bands_data": ("bands.dat", "bands.dat.gnu"),
    "dos_dat":    ("dos.dat",),
}


def _resolve_task_outputs(workdir: Path, result_data: dict) -> tuple[dict[str, str], list[str]]:
    """Return (task_outputs, notes) — task_outputs is keyed by the canonical
    bench T2 names; notes describes what we did to get there.

    Strategy:
      1. If result.json has task_outputs → use it, applying aliases.
      2. If result.json is incomplete → scan workdir for fallback filenames.
    """
    notes: list[str] = []
    raw = result_data.get("task_outputs") or {}
    if raw:
        out: dict[str, str] = {}
        for key, val in raw.items():
            canon = _T2_KEY_ALIASES.get(key, key)
            out[canon] = val
        for k in _T2_KEYS:
            if k not in out:
                # Try to backfill from workdir scan
                for fname in _FALLBACK_FILES.get(k, ()):
                    candidate = workdir / fname
                    if candidate.is_file():
                        out[k] = fname
                        notes.append(f"backfilled {k!r} = {fname!r} (missing from result.json)")
                        break
        return out, notes

    # Incomplete result.json: build entirely from workdir scan.
    notes.append("result.json is incomplete; reconstructing task_outputs from workdir scan")
    out = {}
    for key in _T2_KEYS:
        for fname in _FALLBACK_FILES[key]:
            candidate = workdir / fname
            if candidate.is_file():
                out[key] = fname
                break
        else:
            notes.append(f"could not locate fallback file for {key!r} in workdir")
    return out, notes


# ─── parse run_dir → task_id ────────────────────────────────────────────────

def _infer_task_id(run_dir: Path, override: str | None) -> str:
    if override:
        return override
    name = run_dir.name
    # `<UTC>_<task_id>` shape — UTC timestamps start with 4-digit year and
    # contain digits/colons/Z. Split on the first underscore after the
    # timestamp block (look for the first non-timestamp char).
    parts = name.split("_", 1)
    if len(parts) < 2 or not parts[1].startswith(("M1_T", "M2_T")):
        raise SystemExit(
            f"[diagnose_t2] cannot infer task_id from run_dir name {name!r}.\n"
            f"  Either rename to '<UTC>_<task_id>' shape, or pass --task-id."
        )
    return parts[1]


# ─── pretty printing ───────────────────────────────────────────────────────

def _fmt(v: Any, *, max_width: int = 14) -> str:
    if v is None:
        return "—"
    if isinstance(v, float):
        return f"{v:.4g}"
    if isinstance(v, (dict, list)):
        if isinstance(v, dict) and "energy_ev" in v:
            return f"<curve {len(v['energy_ev'])} pts>"
        return repr(v)[:max_width]
    s = str(v)
    return s if len(s) <= max_width else s[:max_width - 1] + "…"


def _format_table(
    *, task_id: str, scoring_rule: dict, weights: dict, gt: dict,
    extracted: dict, per_field: dict, total: float,
) -> str:
    """Render a 7-column table: field / pred / gt / err / score / weight / contrib."""
    lines = []
    lines.append(f"=== {task_id} ===")
    head = f"{'field':<16} {'pred':<14} {'gt':<14} {'err':>8}  {'score':>5}  {'weight':>6}  {'contrib':>7}"
    lines.append(head)
    lines.append("─" * len(head))
    total_w = sum(weights.values()) or 1.0
    for field, w in weights.items():
        pred = extracted.get(field) if extracted else None
        gt_entry = gt.get(field) or {}
        gt_val = gt_entry.get("value")
        rule = gt_entry.get("rule")
        score = per_field.get(field, 0.0)
        # err: only meaningful for absolute/relative numeric rules
        err = ""
        try:
            if rule in ("absolute", "relative") and pred is not None and gt_val is not None:
                fv, ft = float(pred), float(gt_val)
                err = f"{abs(fv - ft):.4g}" if rule == "absolute" else f"{abs(fv - ft) / max(abs(ft), 1e-12):.4g}"
        except (TypeError, ValueError):
            err = ""
        gt_disp = gt_val if rule != "curve_l2" else "<ref>"
        contrib = score * w / total_w
        flag = ""
        if score < 1.0 and rule == "exact" and pred is not None and gt_val is not None:
            # Highlight case-mismatch as a likely fix
            if isinstance(pred, str) and isinstance(gt_val, str) and pred.lower() == gt_val.lower():
                flag = "  ⚠ case"
            else:
                flag = "  ⚠"
        elif score < 1.0 and rule == "curve_l2":
            flag = "  ⚠"
        elif score < 1.0:
            flag = "  ⚠"
        lines.append(
            f"{field:<16} {_fmt(pred):<14} {_fmt(gt_disp):<14} {err:>8}  "
            f"{score:>5.2f}  {w:>6.2f}  {contrib:>7.3f}{flag}"
        )
    lines.append("─" * len(head))
    lines.append(f"{'total':<16} {'':<14} {'':<14} {'':>8}  {'':>5}  {'':>6}  {total:>7.3f}")
    missing = total_w - total
    if missing > 1e-6:
        lost_fields = [f for f, w in weights.items()
                       if per_field.get(f, 0.0) * w / total_w < w / total_w]
        lines.append(f"missing {missing:.3f} from: "
                     + ", ".join(f for f in lost_fields if per_field.get(f, 0.0) < 1.0))
    return "\n".join(lines)


def _write_markdown(out_path: Path, *, task_id: str, table: str,
                    weights: dict, extracted: dict, per_field: dict,
                    total: float, notes: list[str], gt: dict) -> None:
    lines = []
    lines.append(f"# T2 diagnose — `{task_id}`")
    lines.append("")
    if notes:
        lines.append("## Resolution notes")
        for n in notes:
            lines.append(f"- {n}")
        lines.append("")
    lines.append("## Score breakdown")
    lines.append("")
    lines.append("```")
    lines.append(table)
    lines.append("```")
    lines.append("")
    lines.append("## Raw extracted fields")
    lines.append("")
    lines.append("```json")
    safe = {k: ("<curve>" if isinstance(v, dict) and "energy_ev" in v else v)
            for k, v in extracted.items()}
    lines.append(json.dumps(safe, indent=2, ensure_ascii=False, default=str))
    lines.append("```")
    out_path.write_text("\n".join(lines), encoding="utf-8")


# ─── main ──────────────────────────────────────────────────────────────────

def diagnose(
    run_dir: Path,
    *,
    bench_root: Path,
    task_id_override: str | None = None,
    questions_dirname: str = "questions",
    out_md: Path | None = None,
    quiet: bool = False,
) -> int:
    """Programmatic entry point — returns 0 on success, non-zero on error."""
    run_dir = run_dir.resolve()
    workdir = run_dir / "workdir"
    if not workdir.is_dir():
        print(f"[diagnose_t2] workdir not found: {workdir}", file=sys.stderr)
        return 1
    result_path = workdir / "result.json"
    if not result_path.is_file():
        print(f"[diagnose_t2] result.json not found: {result_path}", file=sys.stderr)
        return 1

    _ensure_bench_imports(bench_root)
    try:
        from harness.lib.task_loader import load_task
        from harness.lib.evaluator import extract_t2_fields
        from harness.lib.scoring import score_fields
    except ImportError as e:
        print(f"[diagnose_t2] failed to import bench harness modules from "
              f"{bench_root}: {e}\n"
              f"  Make sure --bench-root points to a checkout of "
              f"dft-agent-bench.", file=sys.stderr)
        return 2

    task_id = _infer_task_id(run_dir, task_id_override)
    try:
        task = load_task(bench_root, task_id, questions_dirname=questions_dirname)
    except Exception as e:
        print(f"[diagnose_t2] could not load task {task_id!r} from "
              f"{bench_root}/{questions_dirname}/: {e}", file=sys.stderr)
        return 1

    if task["task"]["type"] != "bands_dos":
        print(f"[diagnose_t2] task type is {task['task']['type']!r}, "
              f"not 'bands_dos'. This tool is T2-only.", file=sys.stderr)
        return 1

    result_data = json.loads(result_path.read_text(encoding="utf-8"))
    task_outputs, notes = _resolve_task_outputs(workdir, result_data)

    if not quiet:
        if result_data.get("incomplete"):
            reason = result_data.get("reason", "(no reason)")
            print(f"[diagnose_t2] result.json was incomplete: {reason}")
        for n in notes:
            print(f"[diagnose_t2] {n}")
        print(f"[diagnose_t2] task_outputs = {task_outputs}")

    missing = [k for k in _T2_KEYS if k not in task_outputs]
    if missing:
        print(f"[diagnose_t2] cannot proceed: missing task_outputs for {missing!r} "
              f"(no result.json entry and no fallback file found in workdir). "
              f"Pass --task-outputs-patch to inject paths.", file=sys.stderr)
        return 1

    def _r(rel: str) -> Path:
        p = Path(rel)
        return p if p.is_absolute() else workdir / p

    extracted = extract_t2_fields(
        scf_out_path=_r(task_outputs["scf_out"]),
        bands_out_path=_r(task_outputs["bands_out"]),
        bands_dat_path=_r(task_outputs["bands_data"]),
        dos_dat_path=_r(task_outputs["dos_dat"]),
        initial_structure=task["material"]["initial_structure"],
        extraction_config=task["evaluator"].get("extraction_config", {}),
    )

    scoring_result = score_fields(
        ground_truth=task["ground_truth"],
        extracted=extracted,
        scoring=task["scoring"],
    )

    weights = task["scoring"].get("field_weights", {})
    gt = task["ground_truth"]
    per_field = scoring_result["per_field_scores"]
    total = scoring_result["total_score"]

    table = _format_table(
        task_id=task_id,
        scoring_rule=task["scoring"],
        weights=weights,
        gt=gt,
        extracted=extracted,
        per_field=per_field,
        total=total,
    )
    print(table)

    if out_md is not None:
        _write_markdown(
            out_md,
            task_id=task_id, table=table,
            weights=weights, extracted=extracted, per_field=per_field,
            total=total, notes=notes, gt=gt,
        )
        if not quiet:
            print(f"\n[diagnose_t2] markdown report → {out_md}")

    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Diagnose a single T2 run's per-field score")
    ap.add_argument("run_dir", type=Path,
                    help="Harness run directory (must contain workdir/result.json)")
    ap.add_argument("--task-id", default=None,
                    help="Override task_id (e.g. M1_T2_AlN_bands_dos)")
    ap.add_argument("--bench-root", type=Path,
                    default=Path("~/bench/dft-agent-bench").expanduser(),
                    help="dft-agent-bench checkout root")
    ap.add_argument("--questions-dir", default="questions",
                    help="Questions subdir under bench-root (default: questions)")
    ap.add_argument("--out", type=Path, default=None,
                    help="Also write a markdown report to this path")
    ap.add_argument("--quiet", action="store_true",
                    help="Suppress informational lines")
    args = ap.parse_args(argv)

    return diagnose(
        args.run_dir,
        bench_root=args.bench_root,
        task_id_override=args.task_id,
        questions_dirname=args.questions_dir,
        out_md=args.out,
        quiet=args.quiet,
    )


if __name__ == "__main__":
    raise SystemExit(main())
