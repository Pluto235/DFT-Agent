"""bench mode subprocess wrapper：拉 harness/run.py + monitor task。

CLI（spike 实测）：
  python harness/run.py --task-id ID --solver PATH \\
                         --solver-args "--model X --max-turns N" \\
                         --runs-dir DIR --timeout T

harness 自己写：
  - <runs-dir>/<run_id>/{report.json, solver_stdout.log, solver_stderr.log, workdir/}
  - run_id 由 harness 生成 = <ISO_TS>_<task_id>，所以 web 必须从 stdout `Phase 2 - workdir`
    marker 抓 workdir → run_dir = workdir.parent → run_id = run_dir.name

env 注入：
  - PYTHONNOUSERSITE=1 + PYTHONUNBUFFERED=1 + ANTHROPIC_API_KEY (inline)
  - **不写 SOLVER_MODEL** —— harness/solver 都不读 env，model 必须拼进 --solver-args（spike 实测）
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shlex
import time
from pathlib import Path
from typing import Any, Optional

from backend.persistence import dao
from backend.runners.cleanup import cleanup_run_dir
from backend.runners.log_router import (
    HarnessProgress,
    stream_harness_stdout,
    stream_subprocess_stream,
    wait_for_workdir_marker,
)
from backend.services import run_registry
from backend.services.config_service import get_effective_cleanup


# 从 solver_stderr.log 末尾提取 Exception 真因，附在 failure_reason 后
# 让前端 banner（run.jsx:198 渲染 `Run failed — {run.error}`）能展示实际原因。
# 例：solver 内部 KeyError: 'cmd' 这种 robustness bug 必须冒泡到 UI，否则
# 用户只看到 "solver did not produce result.json"，无法判断是 agent 崩 / 超时 / 其它。
_EXC_LINE_RE = re.compile(
    r"^([A-Z]\w+(?:Error|Exception|Interrupt))\s*:\s*(.+)$",
    re.MULTILINE,
)


def _solver_error_summary(run_dir: Optional[Path], failure_reason: str) -> str:
    if not run_dir:
        return failure_reason
    stderr_path = run_dir / "solver_stderr.log"
    if not stderr_path.exists():
        return failure_reason
    try:
        text = stderr_path.read_text(errors="replace")
    except OSError:
        return failure_reason
    matches = _EXC_LINE_RE.findall(text)
    if not matches:
        return failure_reason
    exc_type, exc_msg = matches[-1]
    exc_msg = exc_msg.strip()
    if len(exc_msg) > 120:
        exc_msg = exc_msg[:117] + "..."
    return f"{failure_reason} — {exc_type}: {exc_msg}"
from backend.services.scheduler import RunHandle


log = logging.getLogger("harness_wrapper")

WORKDIR_BOOT_TIMEOUT_S = 15.0


def _inject_wrapper_dir(env: dict[str, str], settings: Any, mode: str) -> None:
    """按 mode 选 wrapper dir 写到 env["QE_GPU_WRAPPER_DIR"]，并做 spawn-time
    可用性检查（dir 存在 + 包含 pw.x）。失败 → raise FileNotFoundError 让 caller
    把 run 标 failed，避免静默用错 wrapper 把 GPU 烧坏。

    solver_wrapper.py spawn_custom_run 也调本函数；放 harness_wrapper.py 是因为
    它已是 GPU/env 注入的中心。
    """
    if mode == "wrapper_flock":
        wrapper_dir = Path(settings.qe_gpu_wrapper_dir_wrapper_flock).expanduser()
    else:
        wrapper_dir = Path(settings.qe_gpu_wrapper_dir_backend_pin).expanduser()
    pwx = wrapper_dir / "pw.x"
    if not wrapper_dir.is_dir() or not pwx.exists():
        raise FileNotFoundError(
            f"qe_gpu_queueing_mode={mode!r} 对应 wrapper dir 不可用：{wrapper_dir} "
            f"(pw.x exists={pwx.exists()})。检查 settings.qe_gpu_wrapper_dir_{mode} 或 "
            f"切回 backend_pin。"
        )
    env["QE_GPU_WRAPPER_DIR"] = str(wrapper_dir)


async def spawn_bench_run(
    *,
    settings: Any,
    db,
    registry: dict[str, RunHandle],
    request,
    placeholder_id: Optional[str] = None,
    gpu_allocator=None,
) -> RunHandle:
    """spawn harness/run.py + 等 Phase 2 marker → 写 SQLite → 启动 monitor task。

    阻塞最长 WORKDIR_BOOT_TIMEOUT_S（15s）。返回时 run 已 status=running。

    `placeholder_id`：scheduler.promote 调用时传入 queue placeholder run_id；real_id 行
    create 之前先 dao.run_delete(placeholder)，保证 UI 任意时刻不会同时看到 queued
    placeholder + running real（dual-row window 收紧到 0）。

    `gpu_allocator`：若传入，acquire 一张 GPU id 注入 `CUDA_VISIBLE_DEVICES` env；
    spawn 失败 / boot timeout 路径回滚释放；正常路径写到 RunHandle.gpu_id 由 monitor
    终态时 release。空池（CPU-only）不注入。
    """
    extra_parts: list[str] = []
    if request.solver_args:
        extra_parts.append(request.solver_args)
    extra_parts.append(f"--model {shlex.quote(request.model)}")
    if request.max_turns:
        extra_parts.append(f"--max-turns {request.max_turns}")
    solver_args = " ".join(extra_parts)

    solver_path = str(request.solver_path or settings.agent_solver_path)
    solver_timeout = int(getattr(request, "solver_timeout_sec", None) or 1800)

    cmd = [
        "python",
        str(settings.bench_dir / "harness" / "run.py"),
        "--task-id", request.task_id,
        "--solver", solver_path,
        "--solver-args", solver_args,
        "--runs-dir", str(settings.demo_runs_dir),
        "--timeout", str(solver_timeout),
    ]
    env = os.environ.copy()
    env.pop("AUTH_TOKEN", None)
    env["PYTHONNOUSERSITE"] = "1"
    env["PYTHONUNBUFFERED"] = "1"
    if settings.anthropic_api_key:
        env["ANTHROPIC_API_KEY"] = settings.anthropic_api_key
    # 不写 SOLVER_MODEL —— harness/solver 都不读 env；model 已拼进 --solver-args

    # QE backend dispatch:
    #   qe_backend=cpu  → use native CPU pw.x via QE_CPU_WRAPPER_DIR (env-supplied).
    #                     Skip GPU allocator entirely. QE_CPU_NP tunes mpirun -np
    #                     (default 1 = serial; operator sets via env per host).
    #   qe_backend=gpu  → use the configured GPU wrapper dir + allocator. Two
    #                     queueing modes via settings.qe_gpu_queueing_mode:
    #                       backend_pin   → pin CUDA_VISIBLE_DEVICES at spawn,
    #                                       hold the card until solver exits.
    #                       wrapper_flock → no pin; pass QE_GPU_POOL=0,1,…,
    #                                       wrapper acquires per pw.x via flock.
    # qe_backend is also exported to the solver subprocess so solver prompts can
    # adjust QE parallelism guidance (e.g. -np 1 on GPU vs -np N on CPU).
    qe_backend = getattr(request, "qe_backend", "gpu")
    env["QE_BACKEND"] = qe_backend
    gpu_id = None
    if qe_backend == "cpu":
        cpu_dir = os.environ.get("QE_CPU_WRAPPER_DIR")
        if not cpu_dir or not Path(cpu_dir).is_dir():
            raise RuntimeError(
                "qe_backend=cpu requested but QE_CPU_WRAPPER_DIR env not set or dir missing"
            )
        # 对齐 GPU 路径 _inject_wrapper_dir 的 pw.x 存在性检查：
        # dir 在但缺 pw.x 等于静默 fail；显式 raise 让 run 标 failed 而不是跑乱
        if not (Path(cpu_dir) / "pw.x").exists():
            raise RuntimeError(
                f"qe_backend=cpu but {cpu_dir}/pw.x missing — symlink CPU pw.x 进去或换 dir"
            )
        env["QE_GPU_WRAPPER_DIR"] = cpu_dir
        env["QE_CPU_NP"] = os.environ.get("QE_CPU_NP", "1")
    else:
        mode = getattr(settings, "qe_gpu_queueing_mode", "backend_pin")
        if gpu_allocator is not None:
            if mode == "wrapper_flock":
                pool = gpu_allocator.pool_ids()
                if pool:
                    env["QE_GPU_POOL"] = ",".join(str(g) for g in pool)
            else:  # backend_pin (default)
                gpu_id = await gpu_allocator.acquire()
                if gpu_id is not None:
                    env["CUDA_VISIBLE_DEVICES"] = str(gpu_id)
        _inject_wrapper_dir(env, settings, mode)

    log.info("spawning harness: task_id=%s model=%s gpu=%s", request.task_id, request.model, gpu_id)
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(settings.bench_dir),
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            # 让 harness 成为新 session/进程组 leader（pgid == pid），
            # cancel.py 用 os.killpg 才能杀整棵树（solver / bash 工具 / podman 客户端 / mpirun / pw.x），
            # 不再发生"SIGTERM 只杀 harness PID 本身、子孙变孤儿继续跑"的情况。
            start_new_session=True,
        )
    except Exception:
        if gpu_allocator is not None:
            await gpu_allocator.release(gpu_id)
        raise

    started_at = time.time()
    workdir = await wait_for_workdir_marker(proc, timeout_s=WORKDIR_BOOT_TIMEOUT_S)
    if workdir is None:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        await proc.wait()
        if gpu_allocator is not None:
            await gpu_allocator.release(gpu_id)
        raise RuntimeError(f"harness boot timeout ({WORKDIR_BOOT_TIMEOUT_S}s)")

    run_dir = workdir.parent
    run_id = run_dir.name
    transcript_path = workdir / "transcript.json"
    report_path = run_dir / "report.json"

    # 先删 placeholder 再 create real_id：UI 看不到 queued placeholder + running real
    # 同时存在的 dual-row 窗口；之前实现把 delete 放 _BenchRunner.spawn 末尾，window
    # 50ms-数百 ms，polling 偶尔截到，用户报"队列+running 同时多一条"。
    # 同时把 placeholder 的 created_at 继承到 real_id：保证 Runs 页面"newest first"
    # 排序与用户提交顺序一致；否则 real_id 拿 spawn 时间（T0+15s）会顶到最上面。
    placeholder_created_at = None
    placeholder_owner = "localhost"
    placeholder_priority = "normal"
    placeholder_agent_id = None
    placeholder_qe_backend = "gpu"
    if placeholder_id:
        ph_row = await dao.run_get(db, placeholder_id)
        if ph_row:
            placeholder_created_at = ph_row.get("created_at")
            placeholder_owner = ph_row.get("owner") or "localhost"
            placeholder_priority = ph_row.get("priority") or "normal"
            placeholder_agent_id = ph_row.get("agent_id")
            placeholder_qe_backend = ph_row.get("qe_backend") or "gpu"
        await dao.run_delete(db, placeholder_id)
    await run_registry.create_run(
        db,
        run_id=run_id, mode="bench", model=request.model, run_dir=str(run_dir),
        task_id=request.task_id,
        max_turns=request.max_turns or settings.max_turns_default,
        solver_timeout_sec=int(getattr(request, "solver_timeout_sec", None) or 7200),
        initial_status="running",
        created_at=placeholder_created_at,
        owner=placeholder_owner,
        priority=placeholder_priority,
        agent_id=placeholder_agent_id or getattr(request, "agent_id", None),
        qe_backend=placeholder_qe_backend if placeholder_qe_backend != "gpu" else getattr(request, "qe_backend", "gpu"),
    )
    await run_registry.mark_running(db, run_id, pid=proc.pid)

    handle = RunHandle(
        run_id=run_id, mode="bench", proc=proc, run_dir=run_dir,
        transcript_path=transcript_path, report_path=report_path,
        started_at=started_at, model=request.model,
        gpu_id=gpu_id,
    )
    registry[run_id] = handle
    asyncio.create_task(_monitor_bench_run(handle, db, registry, started_at, settings, gpu_allocator))
    return handle


async def _monitor_bench_run(
    handle: RunHandle, db, registry: dict[str, RunHandle], started_at: float,
    settings: Any, gpu_allocator=None,
) -> None:
    """等 proc.wait → 读 report.json → mark_done/failed/cancelled → registry.pop。
    终态后按 effective cleanup flag 删 workdir/tmp* 中间文件。"""
    proc = handle.proc
    progress = HarnessProgress(workdir=handle.transcript_path.parent)
    stdout_task = asyncio.create_task(
        stream_harness_stdout(proc, handle.run_dir, progress)
    )
    stderr_task = asyncio.create_task(
        stream_subprocess_stream(proc.stderr, handle.run_dir / "harness_stderr.log")
    )
    try:
        await proc.wait()
    finally:
        for t in (stdout_task, stderr_task):
            if not t.done():
                try:
                    await asyncio.wait_for(t, timeout=2.0)
                except asyncio.TimeoutError:
                    t.cancel()

    wall_observed = time.time() - started_at

    # 终态前一次性算 token + cost；任何分支（cancel/failure/done/no-report）都带这三字段写 dao
    from backend.services.cost import compute_run_token_cost, sum_transcript_llm_wall_sec
    in_total, out_total, hit_total, miss_total, cost_usd = compute_run_token_cost(
        handle.transcript_path, settings, handle.model,
    )
    llm_wall_total_sec = sum_transcript_llm_wall_sec(handle.transcript_path)

    # 终态前从 transcript 算 final turn 数写进 DB cache；运行中 row 由 runs API
    # 实时调 transcript_reader 算 ct，但终态后该实时分支不再走，DB 列若仍是 0 → result
    # 页"总轮数"显示 0 是这条路径的 bug。补这一步把 final ct 写入。
    final_turn: Optional[int] = None
    try:
        from backend.services.transcript_reader import read_transcript_snapshot
        snap = read_transcript_snapshot(
            run_dir=handle.run_dir, run_id=handle.run_id,
            status="done", since_turn=0,
        )
        final_turn = snap.current_turn or None
    except Exception:
        log.exception("read final transcript turn count failed for %s", handle.run_id)

    # QE 子进程统计：解析 bash.log + .out self-reported + transcript fallback
    # finished_at = 当下（与即将 mark_done 的实际 finished_at 误差 <100ms，对 killed wall 估算无影响）
    qe_call_count: Optional[int] = None
    qe_wall_sec: Optional[float] = None
    qe_wall_self_reported_sec: Optional[float] = None
    qe_wall_bash_cmd_sec: Optional[float] = None
    try:
        from datetime import datetime, timezone
        from backend.services.qe_stats import compute_qe_stats_detailed
        now_iso = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        d = compute_qe_stats_detailed(handle.run_dir, finished_at=now_iso)
        qe_call_count = d["call_count"] if d["call_count"] else None
        qe_wall_sec = d["wall_sec"] if d["wall_sec"] > 0 else None
        qe_wall_self_reported_sec = d["self_wall_sec"] if d["self_wall_sec"] > 0 else None
        qe_wall_bash_cmd_sec = d["bash_wall_sec"] if d["bash_wall_sec"] > 0 else None
    except Exception:
        log.exception("compute QE stats failed for %s", handle.run_id)

    try:
        if handle.cancel_requested:
            await run_registry.mark_cancelled(
                db, handle.run_id, exit_code=proc.returncode, wall_time_sec=wall_observed,
                current_turn=final_turn,
                input_tokens_total=in_total, output_tokens_total=out_total,
                input_cache_hit_tokens_total=hit_total, input_cache_miss_tokens_total=miss_total,
                cost_usd=cost_usd,
                qe_call_count=qe_call_count, qe_wall_sec=qe_wall_sec,
                qe_wall_self_reported_sec=qe_wall_self_reported_sec,
                qe_wall_bash_cmd_sec=qe_wall_bash_cmd_sec,
                llm_wall_total_sec=llm_wall_total_sec,
            )
            return

        report = handle.report_path
        if report and report.exists():
            try:
                data = json.loads(report.read_text())
            except Exception as e:
                log.exception("read report.json failed for %s", handle.run_id)
                await run_registry.mark_failed(
                    db, handle.run_id, error=f"report read failed: {e}",
                    exit_code=proc.returncode, wall_time_sec=wall_observed,
                    current_turn=final_turn,
                    input_tokens_total=in_total, output_tokens_total=out_total,
                input_cache_hit_tokens_total=hit_total, input_cache_miss_tokens_total=miss_total,
                cost_usd=cost_usd,
                    qe_call_count=qe_call_count, qe_wall_sec=qe_wall_sec,
                    qe_wall_self_reported_sec=qe_wall_self_reported_sec,
                    qe_wall_bash_cmd_sec=qe_wall_bash_cmd_sec,
                    llm_wall_total_sec=llm_wall_total_sec,
                )
                return
            # spike §3.3 实测：harness FAILURE 也写 report.json + 含 failure_reason 字段；
            # 必须按 failure_reason 分支判 done vs failed（否则失败 run 被记为 done）。
            failure_reason = data.get("failure_reason")
            exit_code = int(data.get("solver_exit_code") or proc.returncode or 0)
            wall = float(data.get("solver_wall_clock_sec") or wall_observed)
            solver_status = data.get("solver_status")
            if failure_reason:
                run_dir = report.parent if report else None
                await run_registry.mark_failed(
                    db, handle.run_id,
                    error=_solver_error_summary(run_dir, failure_reason),
                    exit_code=exit_code, wall_time_sec=wall,
                    solver_status=solver_status,
                    current_turn=final_turn,
                    input_tokens_total=in_total, output_tokens_total=out_total,
                input_cache_hit_tokens_total=hit_total, input_cache_miss_tokens_total=miss_total,
                cost_usd=cost_usd,
                    qe_call_count=qe_call_count, qe_wall_sec=qe_wall_sec,
                    qe_wall_self_reported_sec=qe_wall_self_reported_sec,
                    qe_wall_bash_cmd_sec=qe_wall_bash_cmd_sec,
                    llm_wall_total_sec=llm_wall_total_sec,
                )
            else:
                await run_registry.mark_done(
                    db, handle.run_id,
                    exit_code=exit_code, wall_time_sec=wall,
                    total_score=data.get("total_score"),
                    solver_status=solver_status,
                    current_turn=final_turn,
                    input_tokens_total=in_total, output_tokens_total=out_total,
                input_cache_hit_tokens_total=hit_total, input_cache_miss_tokens_total=miss_total,
                cost_usd=cost_usd,
                    qe_call_count=qe_call_count, qe_wall_sec=qe_wall_sec,
                    qe_wall_self_reported_sec=qe_wall_self_reported_sec,
                    qe_wall_bash_cmd_sec=qe_wall_bash_cmd_sec,
                    llm_wall_total_sec=llm_wall_total_sec,
                )
        else:
            # harness 完全没写 report.json — process crash 或 wrapper kill
            reason = progress.failure_reason or "harness exited without report.json"
            run_dir = handle.report_path.parent if handle.report_path else None
            await run_registry.mark_failed(
                db, handle.run_id,
                error=_solver_error_summary(run_dir, reason),
                exit_code=proc.returncode, wall_time_sec=wall_observed,
                solver_status=progress.solver_status,
                current_turn=final_turn,
                input_tokens_total=in_total, output_tokens_total=out_total,
                input_cache_hit_tokens_total=hit_total, input_cache_miss_tokens_total=miss_total,
                cost_usd=cost_usd,
                qe_call_count=qe_call_count, qe_wall_sec=qe_wall_sec,
                qe_wall_self_reported_sec=qe_wall_self_reported_sec,
                qe_wall_bash_cmd_sec=qe_wall_bash_cmd_sec,
                llm_wall_total_sec=llm_wall_total_sec,
            )
    finally:

        # 终态写完后按当前 effective cleanup 设置删 tmp/ 中间文件；从 cancel /
        # report-fail / 正常 done 任何分支退出都会进这里。
        try:
            if await get_effective_cleanup(settings, db):
                cleanup_run_dir(handle.run_dir)
        except Exception:
            log.exception("cleanup failed for %s", handle.run_id)
        # 归还 GPU；老 RunHandle（重启前 spawn）gpu_id=None 时是 no-op。
        if gpu_allocator is not None:
            await gpu_allocator.release(handle.gpu_id)
        registry.pop(handle.run_id, None)
