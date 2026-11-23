"""DAO: aiosqlite-based CRUD on runs / queue_entries / config_kv tables.

字段映射规则（DDL ↔ schemas/ layer，docs/04 §3 round 4 audit P1）：

| DDL 列                       | schemas 字段                       | 转换           |
|------------------------------|-------------------------------------|----------------|
| runs.custom_spec_json (TEXT) | RunListItem.custom_spec (dict?)     | json.loads/dumps |
| runs.wall_time_sec (REAL)    | RunListItem.wall_time_sec (float?)  | 直通           |
| runs.wall_time_sec (REAL)    | BenchRunResult.solver_wall_clock_sec | 由 result_reader 改名（dao 不动）|
| runs.solver_status (TEXT)    | RunResult.solver_status             | 直通           |
| —                             | RunListItem.queue_total             | derived: queue_count() |
| runs.current_turn (INTEGER)  | RunListItem.current_turn            | 终态 cache；运行时由 transcript_reader 计算 |

dao 只负责 DDL ↔ Python dict 的字段命名转换；schema 包装由上层 (run_registry / result_reader) 完成。
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Optional

from .db import Database


# ============ helpers ============

def utc_now_iso() -> str:
    # 毫秒精度：批量 insert 时同秒内的多行能稳定排序（原来 %S 秒级精度，
    # batch 86 行全压在同一秒 → string compare 全相等 → 排序不稳定）。
    # JS new Date(...) 接受 .ssZ 格式，前端 fmtRel / created_at.localeCompare 不变。
    now = datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


def _row_to_run_dict(row) -> dict:
    """aiosqlite Row → dict；展开 custom_spec_json TEXT → custom_spec dict。"""
    d = dict(row)
    spec_json = d.pop("custom_spec_json", None)
    d["custom_spec"] = json.loads(spec_json) if spec_json else None
    return d


# ============ runs ============

_RUN_INSERT_SQL = """
INSERT INTO runs (
    run_id, mode, task_id, custom_task_summary, custom_spec_json, model,
    status, source, created_at, max_turns, solver_timeout_sec, run_dir, owner,
    priority, agent_id, qe_backend
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

# priority 排序映射：urgent > high > normal。queue_pop_next + queue_list 都用此 CASE。
# 老 row 没 priority 列 → 走 DEFAULT 'normal'（rank=0）。
_PRIORITY_RANK_SQL = (
    "CASE r.priority WHEN 'urgent' THEN 2 WHEN 'high' THEN 1 ELSE 0 END"
)


async def run_insert(
    db: Database,
    *,
    run_id: str,
    mode: str,
    model: str,
    status: str,
    run_dir: str,
    task_id: Optional[str] = None,
    custom_task_summary: Optional[str] = None,
    custom_spec: Optional[dict] = None,
    source: str = "demo",
    max_turns: int = 50,
    solver_timeout_sec: int = 7200,  # 2h；与 schemas/run.py BenchRunStartRequest / BatchSubmitRequest 默认对齐
    created_at: Optional[str] = None,
    owner: str = "localhost",
    priority: str = "normal",
    agent_id: Optional[str] = None,
    qe_backend: str = "gpu",
) -> None:
    if priority not in ("normal", "high", "urgent"):
        raise ValueError(f"invalid priority {priority!r}")
    if qe_backend not in ("gpu", "cpu"):
        raise ValueError(f"invalid qe_backend {qe_backend!r}")
    spec_json = json.dumps(custom_spec) if custom_spec is not None else None
    await db.execute(
        _RUN_INSERT_SQL,
        (
            run_id, mode, task_id, custom_task_summary, spec_json, model,
            status, source, created_at or utc_now_iso(), max_turns, solver_timeout_sec, run_dir,
            owner, priority, agent_id, qe_backend,
        ),
    )


async def run_get(db: Database, run_id: str) -> Optional[dict]:
    row = await db.fetchone("SELECT * FROM runs WHERE run_id = ?", (run_id,))
    return _row_to_run_dict(row) if row else None


async def runs_list(
    db: Database,
    *,
    source: Optional[str] = None,
    status: Optional[str] = None,
    mode: Optional[str] = None,
    task_id: Optional[str] = None,
    limit: int = 200,
    offset: int = 0,
) -> list[dict]:
    where: list[str] = []
    params: list[Any] = []
    if source and source != "all":
        where.append("source = ?")
        params.append(source)
    if status and status != "all":
        where.append("status = ?")
        params.append(status)
    if mode and mode != "all":
        where.append("mode = ?")
        params.append(mode)
    if task_id:
        where.append("task_id = ?")
        params.append(task_id)
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    sql = f"SELECT * FROM runs {where_sql} ORDER BY created_at DESC LIMIT ? OFFSET ?"
    params += [limit, offset]
    rows = await db.fetchall(sql, tuple(params))
    return [_row_to_run_dict(r) for r in rows]


async def runs_count_live(db: Database) -> int:
    """status IN (running, evaluating)；scheduler 用作 slot 计算。"""
    row = await db.fetchone(
        "SELECT COUNT(*) AS n FROM runs WHERE status IN ('running','evaluating')"
    )
    return int(row["n"]) if row else 0


async def runs_count_by_status(db: Database, status: str) -> int:
    """单 status 计数（admin cluster_state 用）。"""
    row = await db.fetchone(
        "SELECT COUNT(*) AS n FROM runs WHERE status = ?", (status,)
    )
    return int(row["n"]) if row else 0


async def runs_aggregate_for_task(db: Database, task_id: str) -> dict:
    """TaskListItem 的 derived last_score / last_run_at / last_run_count（单 task 版）。"""
    row = await db.fetchone(
        """
        SELECT
            COUNT(*)          AS run_count,
            MAX(finished_at)  AS last_at,
            (SELECT total_score FROM runs WHERE task_id = ? AND status='done'
             ORDER BY finished_at DESC LIMIT 1) AS last_score
        FROM runs WHERE task_id = ?
        """,
        (task_id, task_id),
    )
    if not row:
        return {"last_run_count": 0, "last_run_at": None, "last_score": None}
    return {
        "last_run_count": int(row["run_count"]) if row["run_count"] else 0,
        "last_run_at": row["last_at"],
        "last_score": row["last_score"],
    }


async def runs_aggregate_all_tasks(db: Database) -> dict[str, dict]:
    """list_tasks 用的批量 group-by；audit P2-4 替代 172x 单 task fetchone。

    返回 {task_id: {"last_run_count", "last_run_at", "last_score"}}。
    last_score 取最近一条 status=done 的 total_score（按 finished_at DESC）。
    """
    # 总数 + 最近 finished 时间一次拿全
    rows_total = await db.fetchall(
        """
        SELECT
            task_id,
            COUNT(*)         AS run_count,
            MAX(finished_at) AS last_at
        FROM runs
        WHERE task_id IS NOT NULL
        GROUP BY task_id
        """
    )
    # last_score：每 task 最近一条 done 的 total_score（窗口函数避免相关子查询炸开）
    rows_score = await db.fetchall(
        """
        SELECT task_id, total_score, finished_at
        FROM (
            SELECT task_id, total_score, finished_at,
                   ROW_NUMBER() OVER (
                       PARTITION BY task_id ORDER BY finished_at DESC
                   ) AS rn
            FROM runs
            WHERE status = 'done' AND task_id IS NOT NULL
        )
        WHERE rn = 1
        """
    )
    score_map = {r["task_id"]: r["total_score"] for r in rows_score}
    out: dict[str, dict] = {}
    for r in rows_total:
        tid = r["task_id"]
        out[tid] = {
            "last_run_count": int(r["run_count"]) if r["run_count"] else 0,
            "last_run_at": r["last_at"],
            "last_score": score_map.get(tid),
        }
    return out


async def run_update_state(
    db: Database,
    run_id: str,
    *,
    status: Optional[str] = None,
    started_at: Optional[str] = None,
    finished_at: Optional[str] = None,
    wall_time_sec: Optional[float] = None,
    total_score: Optional[float] = None,
    current_turn: Optional[int] = None,
    exit_code: Optional[int] = None,
    error: Optional[str] = None,
    pid: Optional[int] = None,
    cancel_requested_at: Optional[str] = None,
    cancel_reason: Optional[str] = None,
    solver_status: Optional[str] = None,
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
    clear_pid: bool = False,
) -> None:
    """None 默认是 skip 该字段；`clear_pid=True` 显式把 pid 写成 NULL（终态用）。"""
    sets: list[str] = []
    params: list[Any] = []
    for col, val in [
        ("status", status),
        ("started_at", started_at),
        ("finished_at", finished_at),
        ("wall_time_sec", wall_time_sec),
        ("total_score", total_score),
        ("current_turn", current_turn),
        ("exit_code", exit_code),
        ("error", error),
        ("pid", pid),
        ("cancel_requested_at", cancel_requested_at),
        ("cancel_reason", cancel_reason),
        ("solver_status", solver_status),
        ("input_tokens_total", input_tokens_total),
        ("output_tokens_total", output_tokens_total),
        ("input_cache_hit_tokens_total", input_cache_hit_tokens_total),
        ("input_cache_miss_tokens_total", input_cache_miss_tokens_total),
        ("cost_usd", cost_usd),
        ("qe_call_count", qe_call_count),
        ("qe_wall_sec", qe_wall_sec),
        ("qe_wall_self_reported_sec", qe_wall_self_reported_sec),
        ("qe_wall_bash_cmd_sec", qe_wall_bash_cmd_sec),
        ("llm_wall_total_sec", llm_wall_total_sec),
    ]:
        if val is not None:
            sets.append(f"{col} = ?")
            params.append(val)
    if clear_pid and pid is None:
        sets.append("pid = NULL")
    if not sets:
        return
    sql = f"UPDATE runs SET {', '.join(sets)} WHERE run_id = ?"
    params.append(run_id)
    await db.execute(sql, tuple(params))


# ============ queue ============

async def queue_insert(db: Database, run_id: str) -> int:
    """Append to tail；返回 queue_position (1-based)。"""
    row = await db.fetchone(
        "SELECT COALESCE(MAX(queue_position), 0) + 1 AS pos FROM queue_entries"
    )
    pos = int(row["pos"]) if row else 1
    await db.execute(
        "INSERT INTO queue_entries (run_id, queue_position, enqueued_at) VALUES (?, ?, ?)",
        (run_id, pos, utc_now_iso()),
    )
    return pos


async def queue_pop_next(db: Database) -> Optional[str]:
    """取队首 queued run_id 并出队；空队列返 None。

    排序：priority desc (urgent > high > normal) → queue_position asc。
    JOIN runs 拿 priority 列；scheduler promote 时 urgent 总是优先。"""
    row = await db.fetchone(
        f"""
        SELECT q.run_id
        FROM queue_entries q
        JOIN runs r ON r.run_id = q.run_id
        ORDER BY {_PRIORITY_RANK_SQL} DESC, q.queue_position ASC
        LIMIT 1
        """
    )
    if not row:
        return None
    run_id = row["run_id"]
    await db.execute("DELETE FROM queue_entries WHERE run_id = ?", (run_id,))
    return run_id


async def queue_remove(db: Database, run_id: str) -> None:
    """cancel queued run / 直接出队不 promote 用。"""
    await db.execute("DELETE FROM queue_entries WHERE run_id = ?", (run_id,))


async def run_delete(db: Database, run_id: str) -> None:
    """硬删除 run row（仅 placeholder 用：scheduler promote 后 placeholder 转 real_id 时调）。
    queue_entries FK ON DELETE CASCADE 自动清理。"""
    await db.execute("DELETE FROM runs WHERE run_id = ?", (run_id,))


async def queue_count(db: Database) -> int:
    """RunListItem.queue_total derived 字段用。"""
    row = await db.fetchone("SELECT COUNT(*) AS n FROM queue_entries")
    return int(row["n"]) if row else 0


async def queue_position(db: Database, run_id: str) -> Optional[int]:
    row = await db.fetchone(
        "SELECT queue_position FROM queue_entries WHERE run_id = ?", (run_id,)
    )
    return int(row["queue_position"]) if row else None


async def queue_list(db: Database) -> list[dict]:
    """全队按"调度顺序"返回：priority desc → queue_position asc。

    list_runs 端用 enumerate(idx+1) 作为用户可见 rank（与 queue_pop_next 同序，
    保证 RunRow 显示的 #N 与下一次 promote 取到的位置一致）。"""
    rows = await db.fetchall(
        f"""
        SELECT q.run_id, q.queue_position, q.enqueued_at, r.priority
        FROM queue_entries q
        JOIN runs r ON r.run_id = q.run_id
        ORDER BY {_PRIORITY_RANK_SQL} DESC, q.queue_position ASC
        """
    )
    return [dict(r) for r in rows]


# ============ config_kv ============

async def config_kv_get(db: Database, key: str) -> Optional[str]:
    row = await db.fetchone("SELECT value FROM config_kv WHERE key = ?", (key,))
    return row["value"] if row else None


async def config_kv_set(db: Database, key: str, value: str) -> None:
    await db.execute(
        "INSERT INTO config_kv (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
