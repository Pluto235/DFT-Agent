"""cancel logic：endpoint 同步等到 status=cancelled 才返回（不暴露 cancelling 中间态）。

queued    → mark_cancelled + queue_remove；不需要触 process
running   → SIGTERM + 5s grace + SIGKILL；monitor 完成 mark_cancelled；本函数等待 proc.wait
evaluating → 同上（harness 处于 phase 4-7，evaluator 阶段；SIGTERM 杀 harness）
terminal  → raise RuntimeError("already in terminal")

进程树清理（关键）：harness/solver 是 spawn 时 start_new_session=True 的 session leader，
所以 os.killpg(pgid, SIG) 一发送就杀整棵 pgroup（harness/solver/bash 工具子进程/podman 客户端
等都在同一组）。但 conmon 主动 setsid 自己 detach 进自己的 session，
container 内的 mpirun/pw.x 都是 conmon 的 session 成员（不在 harness 的 pgroup），
killpg 抓不到。于是 SIGKILL 阶段额外做一遍 cwd sweep：扫 /proc 找 cwd 在 run_dir 之下的 PID
全 SIGKILL — run_dir 是带时间戳的唯一路径，匹配范围严格。
"""
from __future__ import annotations

import asyncio
import logging
import os
import signal
import time
from pathlib import Path
from typing import Any, Optional

from backend.persistence import dao
from backend.runners.cleanup import kill_run_dir_processes
from backend.services import run_registry
from backend.services.scheduler import RunHandle


log = logging.getLogger("cancel")

DEFAULT_GRACE_S = 5.0


class RunNotFound(Exception):
    pass


class RunInTerminalState(Exception):
    pass


def _signal_pgroup(proc, sig: int, run_id: str) -> None:
    """向 proc 所在 process group 发信号；safety guard 防止 spawn 时漏 start_new_session
    导致打到 backend 自己的 pgroup。fallback 到 send_signal(单 PID)。"""
    pid = proc.pid
    if pid is None:
        return
    try:
        pgid = os.getpgid(pid)
    except ProcessLookupError:
        return  # 已死
    except OSError as e:
        log.warning("run %s getpgid(%d) failed: %s; falling back to single-pid signal", run_id, pid, e)
        try:
            proc.send_signal(sig)
        except ProcessLookupError:
            pass
        return
    # safety: 只有 spawn 时设 start_new_session=True 才会有 pgid == pid。
    # 否则 pgid 可能是 backend 自身的 pgroup → killpg 会自杀。降级到单 PID 信号。
    if pgid != pid:
        log.warning(
            "run %s pid=%d pgid=%d ≠ pid (start_new_session was likely False); "
            "falling back to single-pid signal — descendant tree will not be killed via pgroup",
            run_id, pid, pgid,
        )
        try:
            proc.send_signal(sig)
        except ProcessLookupError:
            pass
        return
    try:
        os.killpg(pgid, sig)
    except ProcessLookupError:
        pass


async def cancel_run(
    *,
    db,
    registry: dict[str, RunHandle],
    run_id: str,
    reason: Optional[str] = None,
    grace_s: float = DEFAULT_GRACE_S,
    settings: Optional[Any] = None,                     # 终态算 cost 用；None 兜底跳过 cost
) -> dict:
    """取消 run；返回 {"status": "cancelled", "exit_code": int|None}。"""
    row = await dao.run_get(db, run_id)
    if row is None:
        raise RunNotFound(f"run '{run_id}' not found")
    status = row["status"]
    if status in ("done", "failed", "cancelled"):
        raise RunInTerminalState(f"run '{run_id}' is already {status}")

    if status == "queued":
        await dao.queue_remove(db, run_id)
        # queued cancel = 用户从未真启动 LLM 调用 → 明确写 0 cost 而非 NULL（"$0.00" 比 "—" 信息量大）
        await run_registry.mark_cancelled(
            db, run_id, reason=reason,
            input_tokens_total=0, output_tokens_total=0, cost_usd=0.0,
        )
        return {"status": "cancelled", "exit_code": None}

    handle = registry.get(run_id)
    if handle is None or handle.proc is None or handle.proc.returncode is not None:
        # process 已退出或不在 registry —— 但磁盘上 cwd 仍可能有 detach 的 conmon/container
        # （registry 在 backend restart 时丢；DB row 还指向 run_dir）。
        # 这里走一遍 cwd sweep 兜底，避免遗留孤儿。
        run_dir_str = row.get("run_dir") if row else None
        run_dir = Path(run_dir_str) if run_dir_str else None
        kill_run_dir_processes(run_dir, run_id)
        # transcript 在 disk 上能算 cost；handle 丢了也不影响
        in_t, out_t, hit_t, miss_t, cost = (None, None, None, None, None)
        llm_wall = None
        if run_dir is not None and settings is not None:
            from backend.services.cost import compute_run_token_cost, sum_transcript_llm_wall_sec
            transcript_path = run_dir / "workdir" / "transcript.json"
            in_t, out_t, hit_t, miss_t, cost = compute_run_token_cost(
                transcript_path, settings, row.get("model") or "",
            )
            llm_wall = sum_transcript_llm_wall_sec(transcript_path)
        await run_registry.mark_cancelled(
            db, run_id, reason=reason,
            input_tokens_total=in_t, output_tokens_total=out_t,
            input_cache_hit_tokens_total=hit_t, input_cache_miss_tokens_total=miss_t,
            cost_usd=cost,
            llm_wall_total_sec=llm_wall,
        )
        return {"status": "cancelled", "exit_code": None}

    handle.cancel_requested = True
    await run_registry.request_cancel(db, run_id, reason=reason)

    proc = handle.proc
    # SIGTERM 整个 pgroup（harness + solver + bash 工具子进程 + podman 客户端）
    _signal_pgroup(proc, signal.SIGTERM, run_id)

    try:
        await asyncio.wait_for(proc.wait(), timeout=grace_s)
    except asyncio.TimeoutError:
        log.warning("run %s did not exit on SIGTERM within %.1fs; SIGKILL", run_id, grace_s)
        # SIGKILL pgroup + cwd sweep（conmon/container 内 mpirun/pw.x 不在 pgroup，靠 sweep 抓）
        _signal_pgroup(proc, signal.SIGKILL, run_id)
        run_dir = Path(handle.run_dir) if handle.run_dir else None
        kill_run_dir_processes(run_dir, run_id)
        try:
            await asyncio.wait_for(proc.wait(), timeout=2.0)
        except asyncio.TimeoutError:
            log.error("run %s SIGKILL did not reap proc within 2s", run_id)
    else:
        # SIGTERM 内 proc 已退出，但 detach 的 conmon 子树可能还在 — 也走 sweep
        run_dir = Path(handle.run_dir) if handle.run_dir else None
        kill_run_dir_processes(run_dir, run_id)

    # monitor task 会在 wait() 返回后调 mark_cancelled；本函数同步等到那一刻
    # 给 monitor 一点时间完成 SQLite 写入；轮询 SQLite 直到 status=cancelled or 超时
    # audit P0-3: poll 上限 3s（覆盖 monitor stream-task 2s wait + 余量）
    for _ in range(60):  # up to 3s
        await asyncio.sleep(0.05)
        cur = await dao.run_get(db, run_id)
        if cur and cur["status"] == "cancelled":
            return {"status": "cancelled", "exit_code": cur.get("exit_code")}
    # fallback: monitor 还没写 — 自己写一下避免泄漏；带 wall_time_sec 以免前端展示 "—"
    wall = (time.time() - handle.started_at) if handle.started_at else None
    in_t, out_t, hit_t, miss_t, cost = (None, None, None, None, None)
    llm_wall = None
    if settings is not None and handle.transcript_path is not None:
        from backend.services.cost import compute_run_token_cost, sum_transcript_llm_wall_sec
        in_t, out_t, hit_t, miss_t, cost = compute_run_token_cost(
            handle.transcript_path, settings, handle.model,
        )
        llm_wall = sum_transcript_llm_wall_sec(handle.transcript_path)
    await run_registry.mark_cancelled(
        db, run_id, reason=reason, exit_code=proc.returncode, wall_time_sec=wall,
        input_tokens_total=in_t, output_tokens_total=out_t,
        input_cache_hit_tokens_total=hit_t, input_cache_miss_tokens_total=miss_t,
        cost_usd=cost,
        llm_wall_total_sec=llm_wall,
    )
    return {"status": "cancelled", "exit_code": proc.returncode}
