"""dao 状态切换的薄封装。6 个状态切换（queued→running→evaluating→done/failed/cancelled）。

run_id 生成不在这里（runner 自己取 harness/solver 创建的目录名 / custom 自己生成）。
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from backend.persistence import dao
from backend.persistence.db import Database


def _now() -> str:
    # 毫秒精度（同 dao.utc_now_iso）
    now = datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


async def create_run(
    db: Database,
    *,
    run_id: str,
    mode: str,
    model: str,
    run_dir: str,
    task_id: Optional[str] = None,
    custom_task_summary: Optional[str] = None,
    custom_spec: Optional[dict] = None,
    source: str = "demo",
    max_turns: int = 50,
    solver_timeout_sec: int = 7200,  # 与 dao.run_insert + BenchRunStartRequest schema default 对齐
    initial_status: str = "queued",
    created_at: Optional[str] = None,
    owner: str = "localhost",
    priority: str = "normal",
    agent_id: Optional[str] = None,
    qe_backend: str = "gpu",
) -> None:
    await dao.run_insert(
        db,
        run_id=run_id, mode=mode, model=model, status=initial_status,
        run_dir=run_dir, task_id=task_id,
        custom_task_summary=custom_task_summary,
        custom_spec=custom_spec, source=source, max_turns=max_turns,
        solver_timeout_sec=solver_timeout_sec,
        created_at=created_at,
        owner=owner, priority=priority,
        agent_id=agent_id,
        qe_backend=qe_backend,
    )


async def mark_running(db: Database, run_id: str, *, pid: Optional[int] = None) -> None:
    await dao.run_update_state(db, run_id, status="running", started_at=_now(), pid=pid)


async def mark_evaluating(db: Database, run_id: str) -> None:
    await dao.run_update_state(db, run_id, status="evaluating")


async def mark_done(
    db: Database,
    run_id: str,
    *,
    exit_code: Optional[int] = None,
    wall_time_sec: Optional[float] = None,
    total_score: Optional[float] = None,
    solver_status: Optional[str] = None,
    current_turn: Optional[int] = None,
    input_tokens_total: Optional[int] = None,
    output_tokens_total: Optional[int] = None,
    input_cache_hit_tokens_total: Optional[int] = None,
    input_cache_miss_tokens_total: Optional[int] = None,
    cost_usd: Optional[float] = None,
    qe_call_count: Optional[int] = None,
    qe_wall_sec: Optional[float] = None,
    qe_wall_self_reported_sec: Optional[float] = None,
    qe_wall_bash_cmd_sec: Optional[float] = None,
    llm_wall_total_sec: Optional[float] = None,
) -> None:
    await dao.run_update_state(
        db, run_id, status="done",
        finished_at=_now(),
        exit_code=exit_code, wall_time_sec=wall_time_sec,
        total_score=total_score, solver_status=solver_status,
        current_turn=current_turn,
        input_tokens_total=input_tokens_total,
        output_tokens_total=output_tokens_total,
        input_cache_hit_tokens_total=input_cache_hit_tokens_total,
        input_cache_miss_tokens_total=input_cache_miss_tokens_total,
        cost_usd=cost_usd,
        qe_call_count=qe_call_count, qe_wall_sec=qe_wall_sec,
        qe_wall_self_reported_sec=qe_wall_self_reported_sec,
        qe_wall_bash_cmd_sec=qe_wall_bash_cmd_sec,
        llm_wall_total_sec=llm_wall_total_sec,
        clear_pid=True,
    )


async def mark_failed(
    db: Database,
    run_id: str,
    *,
    error: str,
    exit_code: Optional[int] = None,
    wall_time_sec: Optional[float] = None,
    solver_status: Optional[str] = None,
    current_turn: Optional[int] = None,
    input_tokens_total: Optional[int] = None,
    output_tokens_total: Optional[int] = None,
    input_cache_hit_tokens_total: Optional[int] = None,
    input_cache_miss_tokens_total: Optional[int] = None,
    cost_usd: Optional[float] = None,
    qe_call_count: Optional[int] = None,
    qe_wall_sec: Optional[float] = None,
    qe_wall_self_reported_sec: Optional[float] = None,
    qe_wall_bash_cmd_sec: Optional[float] = None,
    llm_wall_total_sec: Optional[float] = None,
) -> None:
    await dao.run_update_state(
        db, run_id, status="failed",
        finished_at=_now(),
        error=error, exit_code=exit_code, wall_time_sec=wall_time_sec,
        solver_status=solver_status,
        current_turn=current_turn,
        input_tokens_total=input_tokens_total,
        output_tokens_total=output_tokens_total,
        input_cache_hit_tokens_total=input_cache_hit_tokens_total,
        input_cache_miss_tokens_total=input_cache_miss_tokens_total,
        cost_usd=cost_usd,
        qe_call_count=qe_call_count, qe_wall_sec=qe_wall_sec,
        qe_wall_self_reported_sec=qe_wall_self_reported_sec,
        qe_wall_bash_cmd_sec=qe_wall_bash_cmd_sec,
        llm_wall_total_sec=llm_wall_total_sec,
        clear_pid=True,
    )


async def mark_cancelled(
    db: Database,
    run_id: str,
    *,
    reason: Optional[str] = None,
    exit_code: Optional[int] = None,
    wall_time_sec: Optional[float] = None,
    current_turn: Optional[int] = None,
    input_tokens_total: Optional[int] = None,
    output_tokens_total: Optional[int] = None,
    input_cache_hit_tokens_total: Optional[int] = None,
    input_cache_miss_tokens_total: Optional[int] = None,
    cost_usd: Optional[float] = None,
    qe_call_count: Optional[int] = None,
    qe_wall_sec: Optional[float] = None,
    qe_wall_self_reported_sec: Optional[float] = None,
    qe_wall_bash_cmd_sec: Optional[float] = None,
    llm_wall_total_sec: Optional[float] = None,
) -> None:
    now = _now()
    await dao.run_update_state(
        db, run_id, status="cancelled",
        finished_at=now, cancel_requested_at=now,
        cancel_reason=reason or "user", error="cancelled by user",
        exit_code=exit_code, wall_time_sec=wall_time_sec,
        current_turn=current_turn,
        input_tokens_total=input_tokens_total,
        output_tokens_total=output_tokens_total,
        input_cache_hit_tokens_total=input_cache_hit_tokens_total,
        input_cache_miss_tokens_total=input_cache_miss_tokens_total,
        cost_usd=cost_usd,
        qe_call_count=qe_call_count, qe_wall_sec=qe_wall_sec,
        qe_wall_self_reported_sec=qe_wall_self_reported_sec,
        qe_wall_bash_cmd_sec=qe_wall_bash_cmd_sec,
        llm_wall_total_sec=llm_wall_total_sec,
        clear_pid=True,
    )


async def request_cancel(db: Database, run_id: str, *, reason: Optional[str] = None) -> None:
    """先记录 cancel_requested_at（DB 即时记录意图）；最终状态由 runner monitor 写。"""
    await dao.run_update_state(
        db, run_id, cancel_requested_at=_now(), cancel_reason=reason or "user",
    )
