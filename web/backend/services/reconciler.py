"""Startup PID-alive 校验 + 孤儿目录 warn。

SQLite 是唯一 SSOT；harness 不写 metadata.json，文件系统不参与状态恢复（round 4 audit P0-recon）。
本模块只做：
  1. 用 PID 存活校验把死进程的 active (running/evaluating) 行改 failed
  2. demo_runs_dir 下有但 SQLite 无的 run_dir → log warning（无元信息可恢复）
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

from backend.persistence import dao
from backend.persistence.db import Database
from backend.services import run_registry


log = logging.getLogger("reconciler")


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        # 进程存在但当前用户无权 signal；保守认为 alive
        return True
    except OSError:
        return False


async def reconcile_runs(db: Database, demo_runs_dir: Path, settings=None) -> None:
    """settings: 可选；传入则在 mark_failed 路径同时把 transcript 总结成 cost 写回 db。
    None 时降级（不算 cost）。"""
    from backend.services.cost import compute_run_token_cost, sum_transcript_llm_wall_sec
    runs = await dao.runs_list(db, status="all", limit=10000)
    queue_run_ids = {r["run_id"] for r in await dao.queue_list(db)}
    for r in runs:
        if r.get("status") in ("running", "evaluating"):
            pid = r.get("pid")
            if not pid or not _pid_alive(int(pid)):
                log.warning(
                    "run %s status=%s pid=%s not alive; mark failed",
                    r["run_id"], r["status"], pid,
                )
                in_t, out_t, hit_t, miss_t, cost = (None, None, None, None, None)
                llm_wall = None
                run_dir_str = r.get("run_dir")
                if settings is not None and run_dir_str:
                    transcript_path = Path(run_dir_str) / "workdir" / "transcript.json"
                    in_t, out_t, hit_t, miss_t, cost = compute_run_token_cost(
                        transcript_path, settings, r.get("model") or "",
                    )
                    llm_wall = sum_transcript_llm_wall_sec(transcript_path)
                await run_registry.mark_failed(
                    db, r["run_id"], error="process died, found at startup",
                    input_tokens_total=in_t, output_tokens_total=out_t,
                    input_cache_hit_tokens_total=hit_t, input_cache_miss_tokens_total=miss_t,
                    cost_usd=cost,
                    llm_wall_total_sec=llm_wall,
                )
        elif r.get("status") == "queued" and r["run_id"] not in queue_run_ids:
            # 孤儿 placeholder：上次 server 崩在 scheduler pop 后、spawn delete
            # placeholder 之前 → row 留 status=queued 但已不在 queue_entries。
            # 不能再 promote（spawn 进度丢失），mark failed 让用户看到原因。
            # placeholder 没真启动 LLM call，cost = 0（与 queued cancel 同语义）。
            log.warning(
                "orphan placeholder run %s status=queued not in queue_entries; mark failed",
                r["run_id"],
            )
            await run_registry.mark_failed(
                db, r["run_id"],
                error="placeholder orphaned by server restart during spawn",
                input_tokens_total=0, output_tokens_total=0, cost_usd=0.0,
            )
    db_run_ids = {r["run_id"] for r in runs}
    if demo_runs_dir.exists():
        for sub in demo_runs_dir.iterdir():
            if not sub.is_dir():
                continue
            # 白名单:custom_tasks(custom run dir 容器) / _gpu_log(baseline-audit-rerun 采样)
            # / 隐藏目录(.startswith('.'))均不是 run_dir,跳过
            if sub.name in {"custom_tasks", "_gpu_log"} or sub.name.startswith("."):
                continue
            if sub.name not in db_run_ids:
                log.warning(
                    "orphan run_dir without SQLite row: %s (manual cleanup)", sub
                )
