"""custom mode subprocess wrapper：直接调 solve.py（绕过 harness）。

CLI（spike 实测）：
  python solve.py --prompt P --workdir W --out O --model X --max-turns N --max-output-tokens N

solver 自己写：
  - workdir/{transcript.json, result.json, bash.log}
  - stdout/stderr 一般空（spike2 §2.4 实测：solver 不打 print）

run_id 由 web 生成（custom 没有 harness 决定的 ID 限制）。
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from pathlib import Path
from typing import Any

from backend.persistence import dao
from backend.runners.cleanup import cleanup_run_dir
from backend.runners.harness_wrapper import _solver_error_summary
from backend.runners.log_router import stream_subprocess_stream
from backend.runners.workdir_setup import (
    build_custom_spec,
    generate_custom_run_id,
    render_task_prompt_for_custom,
    write_custom_spec,
)
from backend.services import run_registry
from backend.services.config_service import get_effective_cleanup
from backend.services.scheduler import RunHandle


log = logging.getLogger("solver_wrapper")


async def spawn_custom_run(
    *,
    settings: Any,
    db,
    registry: dict[str, RunHandle],
    request,
    placeholder_id: str | None = None,
    gpu_allocator=None,
) -> RunHandle:
    """spawn solve.py + 写 SQLite → 启动 monitor task。

    `placeholder_id`：scheduler promote 时传入；create_run real_id 之前先删 placeholder，
    避免 UI 截到 queued + running 同时存在。
    """
    run_id = generate_custom_run_id(
        prompt=request.prompt,
        material=request.material,
        task_type=request.task_type,
    )
    run_dir = settings.demo_runs_dir / run_id
    workdir = run_dir / "workdir"
    workdir.mkdir(parents=True, exist_ok=True)

    spec = build_custom_spec(
        prompt=request.prompt,
        material=request.material,
        task_type=request.task_type,
        model=request.model,
        max_turns=request.max_turns,
        max_output_tokens=request.max_output_tokens,
    )
    write_custom_spec(settings.custom_task_dir, run_id, spec)
    prompt_path = render_task_prompt_for_custom(
        workdir, prompt=request.prompt, material=request.material,
        task_type=request.task_type,
    )
    result_path = workdir / "result.json"

    solver_path = str(request.solver_path or settings.agent_solver_path)

    # 按 request.agent_id 查 AgentEntry，拿 python_path（独立 venv）和 env（额外环境变量）。
    # 没传 agent_id 或 agent_id 不在配置里 → python_path 空 + env 空，行为与旧版完全一致。
    # `getattr(settings, "agents", [])` 防御测试桩 Settings 不含 agents 字段。
    agent_entry = next(
        (a for a in getattr(settings, "agents", [])
         if a.id == getattr(request, "agent_id", None)),
        None,
    )
    python_bin = (
        agent_entry.python_path
        if (agent_entry is not None and agent_entry.python_path)
        else "python"
    )
    cmd = [
        python_bin, solver_path,
        "--prompt",  str(prompt_path),
        "--workdir", str(workdir),
        "--out",     str(result_path),
        "--model",   request.model,
        "--max-turns", str(request.max_turns),
        "--max-output-tokens", str(request.max_output_tokens),
    ]
    env = os.environ.copy()
    env.pop("AUTH_TOKEN", None)
    env["PYTHONNOUSERSITE"] = "1"
    env["PYTHONUNBUFFERED"] = "1"
    if settings.anthropic_api_key:
        env["ANTHROPIC_API_KEY"] = settings.anthropic_api_key
    # solver 内部 `from agent.llm import LLMClient` 需要 solver repo root 在 sys.path。
    # cwd 设 workdir（solver 在 workdir 下做事），靠 PYTHONPATH 注入 agent 包。
    # 老 layout: agent/solve.py → repo root = parent.parent。
    # 新 v4/v5 layout: solve.py 在仓库根，agent/ 是子目录 → repo root = parent。
    # 通过检测 `<solver_path>.parent/agent/` 是否存在挑选哪种。
    sp = Path(solver_path).resolve()
    solver_root = str(sp.parent if (sp.parent / "agent").is_dir() else sp.parent.parent)
    env["PYTHONPATH"] = solver_root + os.pathsep + env.get("PYTHONPATH", "")

    # QE backend dispatch (see harness_wrapper.spawn_bench_run for details):
    # cpu → QE_CPU_WRAPPER_DIR + QE_CPU_NP, skip GPU allocator.
    # gpu → upstream's mode-aware allocator + _inject_wrapper_dir.
    # Solver subprocess reads QE_BACKEND env to adjust QE parallelism guidance.
    from backend.runners.harness_wrapper import _inject_wrapper_dir
    qe_backend = getattr(request, "qe_backend", "gpu")
    env["QE_BACKEND"] = qe_backend
    gpu_id = None
    if qe_backend == "cpu":
        cpu_dir = os.environ.get("QE_CPU_WRAPPER_DIR")
        if not cpu_dir or not Path(cpu_dir).is_dir():
            raise RuntimeError(
                "qe_backend=cpu requested but QE_CPU_WRAPPER_DIR env not set or dir missing"
            )
        # 对齐 GPU 路径 _inject_wrapper_dir 的 pw.x 存在性检查
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

    # AgentEntry.env 放最后：允许 agent 显式覆盖前面任何标准注入
    # （QE_GPU_WRAPPER_DIR / PSEUDO_DIR / PATH 等），typical use-case 是 agent 自带
    # 私有 wrapper / 不同 conda env / pseudo dir。空 dict → no-op。
    if agent_entry is not None and agent_entry.env:
        env.update(agent_entry.env)

    log.info("spawning solver: run_id=%s model=%s gpu=%s", run_id, request.model, gpu_id)
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, cwd=str(workdir), env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            # 同 harness_wrapper：solver 进新 session/pgroup，cancel 时 killpg 杀整树（含 bash 工具子进程）
            start_new_session=True,
        )
    except Exception:
        if gpu_allocator is not None:
            await gpu_allocator.release(gpu_id)
        raise
    started_at = time.time()

    custom_summary = (request.prompt[:60] + "…") if len(request.prompt) > 60 else request.prompt

    # 先删 placeholder 再 create real_id（同 spawn_bench_run 注释）。
    # 继承 placeholder.created_at 让 Runs 排序保用户提交时序。
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
        run_id=run_id, mode="custom", model=request.model, run_dir=str(run_dir),
        custom_task_summary=custom_summary, custom_spec=spec,
        max_turns=request.max_turns,
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
        run_id=run_id, mode="custom", proc=proc, run_dir=run_dir,
        transcript_path=workdir / "transcript.json",
        result_path=result_path,
        started_at=started_at, model=request.model,
        gpu_id=gpu_id,
    )
    registry[run_id] = handle
    asyncio.create_task(_monitor_custom_run(handle, db, registry, started_at, settings, gpu_allocator))
    return handle


async def _monitor_custom_run(
    handle: RunHandle, db, registry: dict[str, RunHandle], started_at: float,
    settings: Any, gpu_allocator=None,
) -> None:
    """等 proc.wait → 读 result.json → mark_done/failed/cancelled。
    终态后按 effective cleanup flag 删 workdir/tmp* 中间文件。

    custom mode 没有 evaluator，没有 report.json；solver_status 由 exit_code 推断。
    """
    proc = handle.proc
    stdout_task = asyncio.create_task(
        stream_subprocess_stream(proc.stdout, handle.run_dir / "solver_stdout.log")
    )
    stderr_task = asyncio.create_task(
        stream_subprocess_stream(proc.stderr, handle.run_dir / "solver_stderr.log")
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
    exit_code = proc.returncode

    from backend.services.cost import compute_run_token_cost, sum_transcript_llm_wall_sec
    in_total, out_total, hit_total, miss_total, cost_usd = compute_run_token_cost(
        handle.transcript_path, settings, handle.model,
    )
    llm_wall_total_sec = sum_transcript_llm_wall_sec(handle.transcript_path)

    # 终态前从 transcript 算 final turn 数（理由同 harness_wrapper）
    final_turn = None
    try:
        from backend.services.transcript_reader import read_transcript_snapshot
        snap = read_transcript_snapshot(
            run_dir=handle.run_dir, run_id=handle.run_id,
            status="done", since_turn=0,
        )
        final_turn = snap.current_turn or None
    except Exception:
        log.exception("read final transcript turn count failed for %s", handle.run_id)

    qe_call_count = None
    qe_wall_sec = None
    qe_wall_self_reported_sec = None
    qe_wall_bash_cmd_sec = None
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
                db, handle.run_id, exit_code=exit_code, wall_time_sec=wall_observed,
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

        # 失败分支：exit≠0 / 没产 result.json → mark_failed（之前所有路径都 mark_done，
        # 22ms 内 ModuleNotFoundError 也被标 done，UI 误以为成功）。
        result_exists = handle.result_path and handle.result_path.exists()
        if exit_code != 0 or not result_exists:
            if not result_exists:
                base = "solver did not produce result.json"
            else:
                base = f"solver exit_code={exit_code}"
            error_msg = _solver_error_summary(handle.run_dir, base)
            await run_registry.mark_failed(
                db, handle.run_id,
                error=error_msg,
                exit_code=exit_code, wall_time_sec=wall_observed,
                solver_status="error",
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

        await run_registry.mark_done(
            db, handle.run_id,
            exit_code=exit_code, wall_time_sec=wall_observed,
            solver_status="ok",
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
        try:
            if await get_effective_cleanup(settings, db):
                cleanup_run_dir(handle.run_dir)
        except Exception:
            log.exception("cleanup failed for %s", handle.run_id)
        if gpu_allocator is not None:
            await gpu_allocator.release(handle.gpu_id)
        registry.pop(handle.run_id, None)
