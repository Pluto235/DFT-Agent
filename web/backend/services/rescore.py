"""Rescore CLI — re-run bench scoring against current question JSONs without
re-running the solver. Useful when bench scoring rules change (e.g. the
2026-05-17 curve_l2 → curve_f1 migration on dos_curve).

The algorithm is purely a function of:
  - report.json.extracted_fields  (agent's submission, preserved per run)
  - questions/<MX_TY>/<task_id>.json.{ground_truth, scoring}  (current GT)

No QE recomputation needed; the dos.dat / bands.dat files remain on disk for
audit but rescore only reads what's already in report.json.

Usage
-----
    # rescore all bench done/failed/cancelled runs (in-place, no backup)
    python -m backend.services.rescore --all

    # dry-run: show diff only, don't write
    python -m backend.services.rescore --all --dry-run

    # single run
    python -m backend.services.rescore --run-id 2026-05-18T02-44-55Z_M1_T2_Zn_bands_dos

Default = apply (user 2026-05-18 decision: "直接对所有跑过的重新打分"). Use
--dry-run to inspect first.

Atomicity: rewrite report.json via tmp + os.replace; update DB total_score
via single UPDATE statement. State.db can stay open in backend during run
(SQLite allows readers concurrent with single writer; we're the only writer).
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path
from typing import Any, Optional


def _is_under(child: Path, parent: Path) -> bool:
    """Path.is_relative_to backport for symmetry; resolves both sides."""
    try:
        child.resolve().relative_to(parent.resolve())
        return True
    except (ValueError, OSError):
        return False


# ===== bench import (PEP 420 namespace package) =====

def _import_bench_scoring(bench_dir: Path):
    """Inject sys.path so 'bench.harness.lib.scoring' resolves, then import.
    Returns the score_fields callable."""
    bench_parent = str(bench_dir.resolve().parent)
    if bench_parent not in sys.path:
        sys.path.insert(0, bench_parent)
    from bench.harness.lib.scoring import score_fields  # type: ignore
    return score_fields


# ===== single-run rescore =====

def _load_question(bench_questions_dir: Path, task_id: str) -> dict | None:
    """Resolve question JSON: questions/<MX_TY>/<task_id>.json. Returns None
    if not found."""
    parts = task_id.split("_")
    if len(parts) < 3:
        return None
    subdir = f"{parts[0]}_{parts[1]}"
    p = bench_questions_dir / subdir / f"{task_id}.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _atomic_write_json(path: Path, data: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def rescore_one(
    run_id: str, run_dir: Path, task_id: str | None,
    bench_questions_dir: Path, score_fields, apply: bool,
    demo_root: Optional[Path] = None,
) -> dict:
    """Returns a diff record: {run_id, status, old_total, new_total,
    field_diffs: {field: (old, new)}, file_written, error}.

    If demo_root is given and run_dir is NOT under demo_root, this is an
    imported / archived run on read-only storage: skip the report.json
    rewrite (no write permission) but still let the caller update DB
    total_score. file_written reflects whether the on-disk JSON was
    refreshed."""
    out: dict[str, Any] = {
        "run_id": run_id, "status": "skipped", "old_total": None,
        "new_total": None, "field_diffs": {}, "file_written": False,
        "error": None,
    }
    if not task_id:
        out["error"] = "no task_id (custom run)"
        return out
    report_path = run_dir / "report.json"
    if not report_path.exists():
        out["error"] = "report.json missing"
        return out
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        out["error"] = f"report.json unreadable: {e}"
        return out
    extracted = report.get("extracted_fields") or {}
    if not extracted:
        out["error"] = "extracted_fields empty (early-fail run)"
        return out
    question = _load_question(bench_questions_dir, task_id)
    if question is None:
        out["error"] = f"question JSON not found for {task_id}"
        return out
    gt = question.get("ground_truth") or {}
    scoring = question.get("scoring") or {}
    if not gt or not scoring.get("field_weights"):
        out["error"] = "question missing ground_truth or scoring.field_weights"
        return out

    try:
        result = score_fields(ground_truth=gt, extracted=extracted, scoring=scoring)
    except Exception as e:
        out["error"] = f"score_fields raised: {e}"
        return out

    old_per_field = report.get("per_field_scores") or {}
    new_per_field = result["per_field_scores"]
    new_per_field_meta = result.get("per_field_meta") or {}
    new_total = result["total_score"]
    old_total = report.get("total_score")

    # field-level diffs (any field that changed by > 1e-6)
    diffs = {}
    for f in set(old_per_field) | set(new_per_field):
        o = old_per_field.get(f)
        n = new_per_field.get(f)
        if o is None or n is None or abs(float(o) - float(n)) > 1e-6:
            diffs[f] = (o, n)

    out["old_total"] = old_total
    out["new_total"] = new_total
    out["field_diffs"] = diffs
    out["status"] = "changed" if (diffs or (old_total != new_total)) else "unchanged"

    if apply:
        # archive runs live on read-only storage; for
        # those, skip the file rewrite — caller still updates DB.total_score
        # so the leaderboard reflects the current rule. Detection: run_dir
        # not under demo_root (where this web instance owns lifecycle).
        is_readonly_archive = (
            demo_root is not None
            and not _is_under(run_dir.resolve(), demo_root)
        )
        if not is_readonly_archive:
            report["per_field_scores"] = new_per_field
            if new_per_field_meta:
                report["per_field_meta"] = new_per_field_meta
            report["total_score"] = new_total
            _atomic_write_json(report_path, report)
            out["file_written"] = True
        else:
            out["file_written"] = False
    return out


# ===== CLI driver =====

def _list_target_runs(
    db_path: Path, only_run_id: str | None,
) -> list[tuple[str, str, str | None]]:
    """Returns list of (run_id, run_dir, task_id) tuples."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        if only_run_id:
            cur = conn.execute(
                "SELECT run_id, run_dir, task_id FROM runs WHERE run_id = ?",
                (only_run_id,),
            )
        else:
            cur = conn.execute(
                "SELECT run_id, run_dir, task_id FROM runs "
                "WHERE mode='bench' AND status IN ('done','failed','cancelled') "
                "ORDER BY created_at"
            )
        return [(r["run_id"], r["run_dir"], r["task_id"]) for r in cur.fetchall()]
    finally:
        conn.close()


def _update_db_total(db_path: Path, run_id: str, new_total: float) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("UPDATE runs SET total_score = ? WHERE run_id = ?",
                     (new_total, run_id))
        conn.commit()
    finally:
        conn.close()


def main() -> int:
    ap = argparse.ArgumentParser(description="Rescore bench runs against current question GT.")
    group = ap.add_mutually_exclusive_group(required=True)
    group.add_argument("--all", action="store_true",
                       help="Rescore all bench done/failed/cancelled runs")
    group.add_argument("--run-id", help="Rescore a single run by run_id")
    ap.add_argument("--dry-run", action="store_true",
                    help="Print diffs only; do not modify report.json or DB")
    ap.add_argument("--bench-dir", default=None,
                    help="Override bench_dir (default: from config.toml via settings)")
    ap.add_argument("--db-path", default=None,
                    help="Override DB path (default: from config.toml via settings)")
    args = ap.parse_args()

    # Lazy settings load so the CLI also works under unusual env (e.g. tests)
    from backend.settings import load_settings  # noqa: E402
    s = load_settings()
    bench_dir = Path(args.bench_dir) if args.bench_dir else s.bench_dir
    db_path = Path(args.db_path) if args.db_path else (s.demo_runs_dir / "state.db")
    bench_questions_dir = bench_dir / "questions"

    if not (bench_questions_dir).exists():
        print(f"ERROR: bench_questions_dir not found: {bench_questions_dir}", file=sys.stderr)
        return 2
    if not db_path.exists():
        print(f"ERROR: db not found: {db_path}", file=sys.stderr)
        return 2

    score_fields = _import_bench_scoring(bench_dir)
    apply = not args.dry_run
    rows = _list_target_runs(db_path, args.run_id)
    demo_root = Path(s.demo_runs_dir).resolve()
    print(f"[rescore] target runs: {len(rows)}; apply={apply}; "
          f"bench_dir={bench_dir}; db={db_path}; demo_root={demo_root}")

    n_changed = 0
    n_unchanged = 0
    n_skipped = 0
    n_readonly = 0
    max_delta = 0.0
    max_delta_run = None

    for run_id, run_dir_s, task_id in rows:
        run_dir = Path(run_dir_s)
        res = rescore_one(
            run_id=run_id, run_dir=run_dir, task_id=task_id,
            bench_questions_dir=bench_questions_dir,
            score_fields=score_fields, apply=apply,
            demo_root=demo_root,
        )
        if res["status"] == "skipped":
            n_skipped += 1
            print(f"  SKIP  {run_id}: {res['error']}")
            continue
        if res["status"] == "unchanged":
            n_unchanged += 1
            continue
        n_changed += 1
        old = res["old_total"]
        new = res["new_total"]
        delta = (new - old) if (old is not None and new is not None) else 0.0
        if abs(delta) > abs(max_delta):
            max_delta = delta
            max_delta_run = run_id
        diff_str = ", ".join(
            f"{f}={o}→{n}" for f, (o, n) in sorted(res["field_diffs"].items())
        )
        tag = "CHG  " if res.get("file_written") else "CHG-R"  # -R = readonly: DB only
        if apply and not res.get("file_written") and apply:
            n_readonly += 1
        print(f"  {tag} {run_id}  total {old}→{new}  ({diff_str})")
        if apply and res["new_total"] is not None:
            _update_db_total(db_path, run_id, float(res["new_total"]))

    print()
    print(f"[rescore] summary: changed={n_changed} unchanged={n_unchanged} "
          f"skipped={n_skipped} readonly_db_only={n_readonly} (total={len(rows)})")
    if max_delta_run:
        sign = "+" if max_delta > 0 else ""
        print(f"[rescore] biggest delta: {sign}{max_delta:.4f} on {max_delta_run}")
    if not apply:
        print("[rescore] DRY-RUN: no files / DB written. Drop --dry-run to apply.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
