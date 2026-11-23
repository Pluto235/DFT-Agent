"""Import historical run archives into the local web DB.

Use case: a collaborator on another machine produced bench runs before / outside
this web UI. They handed you a slim archive (report.json + solver logs +
workdir/, but *.dat / *.save / *.wfc stripped). You want those runs to show
up in your leaderboard / runs list **as a distinct (agent_id, model) cell** —
without re-running QE and without copying the archive into your demo_runs_dir.

This CLI scans the archive, parses each report.json, and INSERTs a row into
the runs table with `run_dir` pointing to the archive directory (reference in
place). The web read paths (GET /api/runs/<rid>/result, curve_field, etc.)
will read from the configured archive at request time.

Safety: the archive lives outside settings.demo_runs_dir, so the existing
delete_batch / cleanup_run_dir code paths (which all gate on
`run_dir.is_relative_to(demo_root)`) will skip physical rmtree even if the
user clicks delete in the UI — DB row gets removed, archive files survive.
A re-run of this CLI restores the rows (idempotent on primary-key conflict).

CLI
---
    python -m backend.services.import_archive \\
        --archive-dir /path/to/archive/ \\
        --agent-id <label_for_leaderboard_cell> \\
        --owner <collaborator_name> \\
        [--dry-run]

Defaults to apply. Use --dry-run to inspect first.

Idempotent: re-running the script sees DB primary key conflicts on existing
runs and skips them with a "already imported" note.
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional


# ===== archive scan =====

# basename like "2026-04-30T03-28-03Z_M1_T2_Si_bands_dos"
_RUN_ID_RE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}Z)_(?P<task>.+)$"
)


def scan_archive(archive_dir: Path, max_depth: int = 2) -> list[Path]:
    """Walk archive_dir up to `max_depth` levels deep; return every directory
    that contains a report.json. Skips files (e.g. README.md, *.tar.gz).
    """
    found: list[Path] = []
    def _walk(d: Path, depth: int) -> None:
        if depth > max_depth:
            return
        for entry in sorted(d.iterdir()):
            if not entry.is_dir():
                continue
            if (entry / "report.json").exists():
                found.append(entry)
                # don't recurse into a run dir (workdir/ has its own structure)
                continue
            _walk(entry, depth + 1)
    if archive_dir.is_dir():
        _walk(archive_dir, depth=0)
    return found


# ===== single run mapping =====

def _parse_created_at(basename: str) -> Optional[str]:
    """Convert run_id timestamp prefix 'YYYY-MM-DDTHH-MM-SSZ' to ISO 8601
    'YYYY-MM-DDTHH:MM:SS.000Z' matching existing rows."""
    m = _RUN_ID_RE.match(basename)
    if not m:
        return None
    ts = m.group("ts")  # e.g. "2026-04-30T03-28-03Z"
    # split into date + time parts
    try:
        date_part, time_part = ts.split("T", 1)
        time_part = time_part.rstrip("Z").replace("-", ":")  # 03:28:03
        return f"{date_part}T{time_part}.000Z"
    except (ValueError, AttributeError):
        return None


def _parse_task_id(basename: str, report_task_id: Optional[str]) -> Optional[str]:
    """Pick task_id: report.json's value first, else parse from basename
    (after the timestamp prefix). Handle special case '_resumed' → coerce to
    base task_id with '_vc_relax' suffix."""
    tid = report_task_id
    if not tid:
        m = _RUN_ID_RE.match(basename)
        if m:
            tid = m.group("task")
    if tid and tid.endswith("_resumed"):
        # Defensive: some archives label resumed T1 runs as '<material>_resumed'
        # (non-canonical task_id). Map back to the canonical vc_relax form so
        # task_loader lookups succeed and leaderboard groups correctly.
        tid = tid[: -len("_resumed")] + "_vc_relax"
    return tid


def _select_report(run_dir: Path) -> tuple[dict, str]:
    """Prefer report_rerun.json (post-hoc scored), fall back to report.json.
    Returns (parsed_report, source_filename)."""
    rerun = run_dir / "report_rerun.json"
    if rerun.exists():
        try:
            return json.loads(rerun.read_text(encoding="utf-8")), "report_rerun.json"
        except (OSError, json.JSONDecodeError):
            pass
    rep = run_dir / "report.json"
    return json.loads(rep.read_text(encoding="utf-8")), "report.json"


def _derive_status(report: dict) -> tuple[str, str]:
    """Map archive report.json → (status, solver_status) for the DB CHECK
    constraints.

    - status ∈ ('done','failed') for archive (never queued/running)
    - solver_status ∈ ('ok','timeout','error', None) per schema
    """
    total = report.get("total_score")
    exit_code = report.get("solver_exit_code")
    solver_status_raw = report.get("solver_status")
    # normalize solver_status to allowed values
    if solver_status_raw in ("ok", "timeout", "error"):
        solver_status = solver_status_raw
    elif solver_status_raw == "done":
        solver_status = "ok"
    elif exit_code == 0:
        solver_status = "ok"
    else:
        solver_status = "error"
    if total is not None and (exit_code in (0, None)):
        status = "done"
    else:
        status = "failed"
    return status, solver_status


def map_run(
    run_dir: Path, agent_id: str, owner: str,
) -> Optional[dict]:
    """Build the DB INSERT payload for one archive run dir. Returns None if
    the row cannot be constructed (no parsable timestamp / task_id). On
    mapping failure returns a dict with a single '_mapping_error' key (NOT
    'error' — that's the DB column, which holds failure_reason from report)."""
    basename = run_dir.name
    try:
        report, _src = _select_report(run_dir)
    except (OSError, json.JSONDecodeError) as e:
        return {"_mapping_error": f"report.json unreadable: {e}", "basename": basename}

    created_at = _parse_created_at(basename)
    if not created_at:
        # Fallback for non-timestamped names like 'walkthrough_M1_T1_Si': use
        # report.json mtime. Keeps these in the leaderboard at roughly the
        # right epoch.
        try:
            mtime = (run_dir / "report.json").stat().st_mtime
            created_at = datetime.utcfromtimestamp(mtime).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        except OSError:
            created_at = None
    task_id = _parse_task_id(basename, report.get("task_id"))
    if not created_at or not task_id:
        return {
            "_mapping_error": f"could not parse created_at / task_id from basename '{basename}'",
            "basename": basename,
        }

    status, solver_status = _derive_status(report)
    model = report.get("model") or "unknown"
    total_score = report.get("total_score")
    wall_time = report.get("solver_wall_clock_sec")
    exit_code = report.get("solver_exit_code")
    failure_reason = report.get("failure_reason")

    # run_id == basename; web's convention
    return {
        "run_id": basename,
        "mode": "bench",
        "task_id": task_id,
        "model": model,
        "status": status,
        "solver_status": solver_status,
        "source": "bench",
        "created_at": created_at,
        "started_at": created_at,
        "finished_at": created_at,  # archive doesn't have exact finish; reuse created
        "wall_time_sec": float(wall_time) if isinstance(wall_time, (int, float)) else None,
        "total_score": float(total_score) if isinstance(total_score, (int, float)) else None,
        "current_turn": 0,
        "max_turns": 80,
        "solver_timeout_sec": 1800,
        "exit_code": int(exit_code) if isinstance(exit_code, int) else None,
        "error": failure_reason,
        "run_dir": str(run_dir.resolve()),
        "owner": owner,
        "priority": "normal",
        "agent_id": agent_id,
    }


# ===== DB ops =====

def _row_exists(conn: sqlite3.Connection, run_id: str) -> bool:
    cur = conn.execute("SELECT 1 FROM runs WHERE run_id = ?", (run_id,))
    return cur.fetchone() is not None


_INSERT_SQL = """
INSERT INTO runs (
    run_id, mode, task_id, model, status, solver_status, source,
    created_at, started_at, finished_at, wall_time_sec, total_score,
    current_turn, max_turns, solver_timeout_sec, exit_code, error,
    run_dir, owner, priority, agent_id
) VALUES (
    :run_id, :mode, :task_id, :model, :status, :solver_status, :source,
    :created_at, :started_at, :finished_at, :wall_time_sec, :total_score,
    :current_turn, :max_turns, :solver_timeout_sec, :exit_code, :error,
    :run_dir, :owner, :priority, :agent_id
)
"""


def _insert_row(conn: sqlite3.Connection, row: dict) -> None:
    conn.execute(_INSERT_SQL, row)


# ===== CLI driver =====

def main() -> int:
    ap = argparse.ArgumentParser(description="Import historical run archive into web DB.")
    ap.add_argument("--archive-dir", required=True,
                    help="Path to archive root (recursively scans 1-2 levels deep)")
    ap.add_argument("--agent-id", required=True,
                    help="DB agent_id label for all imported rows; pick a leaderboard "
                         "cell distinct from your local agents (e.g. 'v1_collaborator_x')")
    ap.add_argument("--owner", required=True,
                    help="DB owner for all imported rows (e.g. collaborator name)")
    ap.add_argument("--dry-run", action="store_true",
                    help="Print mapping only; do not write DB")
    ap.add_argument("--db-path", default=None,
                    help="Override DB path (default: settings.demo_runs_dir/state.db)")
    args = ap.parse_args()

    from backend.settings import load_settings
    s = load_settings()
    db_path = Path(args.db_path) if args.db_path else (s.demo_runs_dir / "state.db")
    archive_dir = Path(args.archive_dir).resolve()
    if not archive_dir.is_dir():
        print(f"ERROR: archive dir not found: {archive_dir}", file=sys.stderr)
        return 2
    if not db_path.exists():
        print(f"ERROR: db not found: {db_path}", file=sys.stderr)
        return 2

    run_dirs = scan_archive(archive_dir)
    print(f"[import] scanned {archive_dir}: found {len(run_dirs)} runs "
          f"(agent_id={args.agent_id}, owner={args.owner}, dry_run={args.dry_run})")

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    n_imported = 0
    n_skipped_dup = 0
    n_skipped_err = 0
    statuses = {"done": 0, "failed": 0}
    try:
        for rd in run_dirs:
            row = map_run(rd, agent_id=args.agent_id, owner=args.owner)
            if row is None or "_mapping_error" in row:
                err = row.get("_mapping_error") if row else "unknown"
                print(f"  SKIP-ERR  {rd.name}: {err}")
                n_skipped_err += 1
                continue
            if _row_exists(conn, row["run_id"]):
                print(f"  SKIP-DUP  {row['run_id']} (already in DB)")
                n_skipped_dup += 1
                continue
            print(f"  {'WOULD' if args.dry_run else 'INS  '}    "
                  f"{row['run_id']}  task={row['task_id']:<40} "
                  f"status={row['status']:<7} score={row['total_score']}")
            statuses[row["status"]] = statuses.get(row["status"], 0) + 1
            if not args.dry_run:
                _insert_row(conn, row)
                n_imported += 1
        if not args.dry_run:
            conn.commit()
    finally:
        conn.close()

    print()
    print(f"[import] summary: "
          f"{'would_import' if args.dry_run else 'imported'}={n_imported if not args.dry_run else (len(run_dirs) - n_skipped_dup - n_skipped_err)} "
          f"skipped_dup={n_skipped_dup} skipped_err={n_skipped_err}")
    print(f"[import] status breakdown: done={statuses.get('done', 0)} "
          f"failed={statuses.get('failed', 0)}")
    if args.dry_run:
        print("[import] DRY-RUN: no DB writes. Drop --dry-run to apply.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
