"""Batch-run the 21 T1 tasks of questions_subset42 against this solver.

Per-task pipeline:
  1. Skip if a complete report.json already exists (--resume default).
  2. Spawn gpu_sampler.py against THIS process's pid (sampler exits with us).
  3. Spawn `python harness/run.py --task-id ... --questions-dir questions_subset42
     --solver dft-bench-solver/solve.py --solver-args '--model X --max-turns Y'`.
  4. After harness finishes (success or failure), terminate sampler, copy
     gpu_metrics.json into run_dir alongside report.json.
  5. Append a one-line dashboard row.

Usage:
  python scripts/run_subset42_batch.py \
      --runs-dir /path/to/bench/runs_subset42/ \
      --concurrency 2 \
      --limit 1   # smoke
      --model claude-sonnet-4-6 --max-turns 80
"""
from __future__ import annotations
import argparse
import asyncio
import json
import os
import shutil
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

REPO_BENCH = Path("./bench")
REPO_SOLVER = Path("/path/to/solvers/current")
QE_GPU_WRAPPER = Path("/path/to/infra/qe-gpu-wrapper")

PYTHON_BIN = Path("/path/to/miniconda3/envs/tritondft/bin/python")
SOLVER = REPO_SOLVER / "solve.py"
HARNESS_RUN = REPO_BENCH / "harness" / "run.py"
GPU_SAMPLER = REPO_SOLVER / "scripts" / "gpu_sampler.py"


def load_tasks(manifest_path: Path, t1_only: bool) -> list[str]:
    m = json.loads(manifest_path.read_text())
    rows = m["selected_tasks"]
    if t1_only:
        rows = [r for r in rows if r["group"] == "T1"]
    rows.sort(key=lambda r: r["task_id"])
    return [r["task_id"] for r in rows]


def find_existing_run(runs_dir: Path, task_id: str) -> Path | None:
    """Return the most recent run_dir whose report.json says success for this task."""
    if not runs_dir.exists():
        return None
    cands = sorted(
        [p for p in runs_dir.iterdir()
         if p.is_dir() and p.name.endswith(f"_{task_id}")],
        reverse=True,
    )
    for p in cands:
        rj = p / "report.json"
        if rj.exists():
            try:
                d = json.loads(rj.read_text())
                if d.get("solver_exit_code") == 0 and "total_score" in d:
                    return p
            except Exception:
                continue
    return None


async def run_one_task(
    task_id: str,
    runs_dir: Path,
    model: str,
    max_turns: int,
    timeout_s: int,
    gpu_index: int,
    questions_dir: str,
    sem: asyncio.Semaphore,
    dashboard: list[dict],
    log_file: Path,
) -> dict:
    async with sem:
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")
        # NB: harness creates a child dir <ts>_<task_id> inside runs_dir
        log_line = f"[{datetime.now().strftime('%H:%M:%S')}] start {task_id} (gpu {gpu_index})"
        print(log_line, flush=True)
        with log_file.open("a") as f:
            f.write(log_line + "\n")

        # -- Spawn harness --
        env = os.environ.copy()
        # Solver auth: ANTHROPIC_API_KEY must already be in env when this script is called
        env["QE_GPU_WRAPPER_DIR"] = str(QE_GPU_WRAPPER)
        env["CUDA_VISIBLE_DEVICES"] = str(gpu_index)
        cmd = [
            str(PYTHON_BIN), "-m", "harness.run",
            "--task-id", task_id,
            "--solver", str(SOLVER),
            "--solver-args", f"--model {model} --max-turns {max_turns}",
            "--questions-dir", questions_dir,
            "--runs-dir", str(runs_dir),
            "--timeout", str(timeout_s),
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(REPO_BENCH),
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )

        # -- Spawn GPU sampler bound to harness pid --
        # NB: sampler is best-effort; if it fails to start we still run.
        # We attach it to harness pid (the harness process is the parent of solver+pw.x).
        # Best-effort delay so harness has a moment to spin up.
        await asyncio.sleep(1.0)
        # We'll write gpu_metrics.json into a per-task tmp; copy into the run_dir at end.
        tmp_metrics = runs_dir / f".pending_gpu_{task_id}_{ts}.json"
        sampler_proc = await asyncio.create_subprocess_exec(
            str(PYTHON_BIN), str(GPU_SAMPLER),
            "--pid", str(proc.pid),
            "--out", str(tmp_metrics),
            "--gpu-index", str(gpu_index),
            "--interval", "2",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )

        # -- Capture harness output to a per-task log --
        # (Don't store all in memory; tail it.)
        per_task_log = runs_dir / f"batch_log_{ts}_{task_id}.log"
        with per_task_log.open("w") as f:
            while True:
                line = await proc.stdout.readline()
                if not line:
                    break
                f.write(line.decode(errors="replace"))
                f.flush()
        rc = await proc.wait()

        # -- Stop sampler --
        try:
            sampler_proc.terminate()
            await asyncio.wait_for(sampler_proc.wait(), timeout=5)
        except (ProcessLookupError, asyncio.TimeoutError):
            try:
                sampler_proc.kill()
            except ProcessLookupError:
                pass

        # -- Locate the run_dir harness created (latest one ending with _task_id) --
        run_dir = None
        latest_t = -1.0
        for p in runs_dir.iterdir():
            if p.is_dir() and p.name.endswith(f"_{task_id}"):
                t = p.stat().st_mtime
                if t > latest_t:
                    latest_t = t; run_dir = p

        # -- Move gpu_metrics.json into run_dir --
        if tmp_metrics.exists() and run_dir is not None:
            shutil.move(str(tmp_metrics), str(run_dir / "gpu_metrics.json"))
        elif tmp_metrics.exists():
            tmp_metrics.unlink()

        # -- Read report.json to summarize --
        score = None
        wall = None
        rj_path = (run_dir / "report.json") if run_dir else None
        if rj_path and rj_path.exists():
            try:
                d = json.loads(rj_path.read_text())
                score = d.get("total_score")
                wall = d.get("solver_wall_clock_sec")
            except Exception:
                pass

        result = {
            "task_id": task_id,
            "rc": rc,
            "score": score,
            "wall_s": wall,
            "run_dir": str(run_dir) if run_dir else None,
        }
        dashboard.append(result)

        line2 = (
            f"[{datetime.now().strftime('%H:%M:%S')}] DONE {task_id} "
            f"rc={rc} score={score} wall={wall}s ({run_dir})"
        )
        print(line2, flush=True)
        with log_file.open("a") as f:
            f.write(line2 + "\n")

        return result


async def amain(args) -> int:
    runs_dir = args.runs_dir.resolve()
    runs_dir.mkdir(parents=True, exist_ok=True)
    log_file = runs_dir / "batch.log"

    if "ANTHROPIC_API_KEY" not in os.environ:
        print("ERROR: ANTHROPIC_API_KEY not in env. Export it before running.",
              file=sys.stderr)
        return 2

    manifest_path = (
        args.manifest_path
        if args.manifest_path
        else REPO_BENCH / args.questions_dir / "MANIFEST.json"
    )
    if not manifest_path.exists():
        print(f"ERROR: manifest not found: {manifest_path}", file=sys.stderr)
        return 2

    if args.gpu_list:
        gpu_list = [int(x) for x in args.gpu_list.split(",") if x.strip()]
    else:
        gpu_list = list(range(args.concurrency))

    tasks = load_tasks(manifest_path, t1_only=args.t1_only)
    if args.only:
        wanted = set(args.only)
        tasks = [t for t in tasks if t in wanted]

    # Resume filter
    skipped = []
    queue = []
    for t in tasks:
        if args.resume:
            existing = find_existing_run(runs_dir, t)
            if existing is not None:
                skipped.append(t)
                continue
        queue.append(t)

    if args.limit:
        queue = queue[: args.limit]

    print(f"[batch] tasks total={len(tasks)} skipped(resume)={len(skipped)} "
          f"to_run={len(queue)} concurrency={args.concurrency} gpus={gpu_list} "
          f"timeout={args.timeout}s")
    print(f"[batch] runs_dir={runs_dir}")
    print(f"[batch] questions_dir={args.questions_dir} manifest={manifest_path}")
    if args.dry_run:
        for i, t in enumerate(queue):
            print(f"  WOULD RUN: {t} (gpu {gpu_list[i % len(gpu_list)]})")
        return 0

    sem = asyncio.Semaphore(args.concurrency)
    dashboard: list[dict] = []

    coros = []
    for i, tid in enumerate(queue):
        gpu_idx = gpu_list[i % len(gpu_list)]
        coros.append(run_one_task(
            tid, runs_dir, args.model, args.max_turns, args.timeout,
            gpu_idx, args.questions_dir, sem, dashboard, log_file,
        ))

    await asyncio.gather(*coros, return_exceptions=False)

    # Final summary
    n = len(dashboard)
    n_ok = sum(1 for d in dashboard if d["rc"] == 0)
    sum_wall = sum((d["wall_s"] or 0) for d in dashboard)
    avg_score = (
        sum((d["score"] or 0) for d in dashboard if d["score"] is not None)
        / max(1, sum(1 for d in dashboard if d["score"] is not None))
    )
    print()
    print(f"[batch] done: {n} runs, {n_ok} rc=0, "
          f"avg_score={avg_score:.3f}, wall_total={sum_wall:.0f}s")
    print(f"[batch] log: {log_file}")
    return 0 if n_ok == n else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs-dir", type=Path, required=True)
    ap.add_argument("--concurrency", type=int, default=2)
    ap.add_argument("--model", default="claude-sonnet-4-6")
    ap.add_argument("--max-turns", type=int, default=80)
    ap.add_argument("--timeout", type=int, default=7200,
                    help="Per-task harness timeout (seconds).")
    ap.add_argument("--limit", type=int, default=0,
                    help="If >0, only run first N tasks (smoke).")
    ap.add_argument("--only", nargs="*", default=None,
                    help="Run only these specific task_ids.")
    ap.add_argument("--resume", action="store_true",
                    help="Skip tasks that already have a complete report.json.")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--questions-dir", default="questions_subset42",
                    help="harness --questions-dir value (relative to "
                         "bench/dft-agent-bench/).")
    ap.add_argument("--manifest-path", type=Path, default=None,
                    help="Absolute path to MANIFEST.json. Default: "
                         "bench/dft-agent-bench/<questions-dir>/MANIFEST.json")
    ap.add_argument("--gpu-list", type=str, default=None,
                    help="Comma-separated GPU indices, e.g. '0,1,2,5'. "
                         "Slot i → list[i % len(list)]. Default: 0..N-1.")
    ap.add_argument("--t1-only", action="store_true", default=False,
                    help="Only run T1 tasks from MANIFEST (legacy budget-gate).")
    ap.add_argument("--include-t2", dest="t1_only", action="store_false",
                    help="Include T2 tasks (now the default).")
    args = ap.parse_args()
    return asyncio.run(amain(args))


if __name__ == "__main__":
    sys.exit(main())
