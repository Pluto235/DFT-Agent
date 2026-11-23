"""FIFO scheduler + RunnerProtocol 抽象。

P3a 阶段（本文件）：scheduler 骨架仅依赖 RunnerProtocol；用 dummy runner 验证 queue→running→done 通路。
P3b 阶段（5-10 min 接线）：从 backend.runners 注入真 harness_wrapper / solver_wrapper。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional, Protocol

from backend.persistence import dao
from backend.persistence.db import Database
from backend.services import run_registry


log = logging.getLogger("scheduler")


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except (PermissionError, OSError):
        return True  # 保守：alive


def _check_natural_completion(row: dict, settings: Any = None) -> Optional[tuple[bool, dict]]:
    """Reaper 边缘 race：run 自然 exit 写完 report.json/result.json 后，monitor task
    可能还没来得及 mark_done/mark_failed，就被 reaper 看到 pid 死了误标 failed。
    本函数检查 run_dir 是否有终态产物：
      - bench：report.json 存在 → 按 failure_reason 分支返 (is_done, kwargs)
      - bench fallback：harness 死前 agent 已 submit（workdir/result.json 存在但 report 没写）
        → 现场 import harness.lib 跑 Phase 4-7 算分回写 report.json → mark_done
      - custom：workdir/result.json 存在 → 视为 done
      - 都没有 → 返 None（reaper 仍走原 mark_failed 路径，没误判）
    返 (True, kwargs) 表 mark_done；(False, kwargs) 表 mark_failed 但带从产物里拿到的字段。"""
    run_dir_str = row.get("run_dir")
    if not run_dir_str:
        return None
    run_dir = Path(run_dir_str)
    mode = row.get("mode")
    if mode == "bench":
        report = run_dir / "report.json"
        if report.exists():
            try:
                data = json.loads(report.read_text())
            except Exception:
                return None
            kwargs = {
                "exit_code": int(data.get("solver_exit_code") or 0),
                "wall_time_sec": float(data.get("solver_wall_clock_sec") or 0.0),
                "solver_status": data.get("solver_status"),
            }
            failure_reason = data.get("failure_reason")
            if failure_reason:
                kwargs["error"] = f"reaped after natural exit: {failure_reason}"
                return (False, kwargs)
            kwargs["total_score"] = data.get("total_score")
            return (True, kwargs)
        # bench fallback：agent 实际 submit 了（result.json 在）但 harness 没走完 Phase 4-7
        # 这是 0506 reap 调查发现的 bug—— 4 个 case (AlAs/AlN/AlP/Be) 全成功的 run 被错标 failed。
        # 现场 import harness.lib 跑评分，避免把 agent 的成功结果丢掉。
        return _try_score_from_result_json(row, run_dir, settings)
    if mode == "custom":
        result = run_dir / "workdir" / "result.json"
        if not result.exists():
            return None
        # custom 没有 evaluator；result.json 存在即视为 solver 自报成功
        return (True, {"exit_code": 0, "solver_status": "ok"})
    return None


def _try_score_from_result_json(
    row: dict, run_dir: Path, settings: Any,
) -> Optional[tuple[bool, dict]]:
    """bench mode：harness 死前 agent 已 submit → 兜底跑 Phase 4-7 评分。

    步骤：load_task → extract → score → audit → 写 report.json。
    任何一步失败返 None（让上层走原 mark_failed 路径，不引入新风险）。
    成功返 (True, kwargs with total_score) → mark_done。
    """
    if settings is None:
        return None
    result_json = run_dir / "workdir" / "result.json"
    if not result_json.exists():
        return None
    try:
        data = json.loads(result_json.read_text())
    except Exception:
        return None
    if data.get("incomplete"):
        return None
    task_outputs = data.get("task_outputs") or {}
    if not task_outputs:
        return None
    task_id = row.get("task_id")
    if not task_id:
        return None

    bench_dir = Path(getattr(settings, "bench_dir", "") or "").resolve()
    if not bench_dir.exists():
        return None
    if str(bench_dir) not in sys.path:
        sys.path.insert(0, str(bench_dir))
    try:
        from harness.lib.task_loader import get_network_allowed, load_task
        from harness.lib.evaluator import extract_t1_fields, extract_t2_fields
        from harness.lib.scoring import score_fields
        from harness.lib.audit import audit_run
    except Exception:
        log.exception("bench fallback: import harness.lib failed")
        return None

    try:
        task = load_task(bench_dir, task_id)
    except Exception:
        log.exception("bench fallback: load_task(%s) failed", task_id)
        return None

    workdir = run_dir / "workdir"
    task_type = task["task"]["type"]

    def _resolve(rel_or_abs: str) -> Path:
        p = Path(rel_or_abs)
        return p if p.is_absolute() else workdir / p

    try:
        if task_type == "vc_relax":
            rel = task_outputs.get("vc_relax_out")
            if not rel:
                return None
            extracted = extract_t1_fields(
                relax_out_path=_resolve(rel),
                extraction_config=task["evaluator"].get("extraction_config", {}),
            )
        elif task_type == "bands_dos":
            required = ("scf_out", "bands_out", "bands_data", "dos_dat")
            missing = [k for k in required if not task_outputs.get(k)]
            if missing:
                return None
            extracted = extract_t2_fields(
                scf_out_path=_resolve(task_outputs["scf_out"]),
                bands_out_path=_resolve(task_outputs["bands_out"]),
                bands_dat_path=_resolve(task_outputs["bands_data"]),
                dos_dat_path=_resolve(task_outputs["dos_dat"]),
                initial_structure=task["material"]["initial_structure"],
                extraction_config=task["evaluator"].get("extraction_config", {}),
            )
        else:
            return None
    except Exception:
        log.exception("bench fallback: extract failed for %s", row.get("run_id"))
        return None

    try:
        scoring_result = score_fields(
            ground_truth=task["ground_truth"],
            extracted=extracted,
            scoring=task["scoring"],
        )
    except Exception:
        log.exception("bench fallback: score failed for %s", row.get("run_id"))
        return None

    solver_wall_s = float(row.get("wall_time_sec") or 0.0)
    try:
        audit_flags = audit_run(
            workdir=workdir,
            task_outputs=task_outputs,
            task_type=task_type,
            solver_wall_s=solver_wall_s,
            enforce_no_network=not get_network_allowed(task),
        )
    except Exception:
        log.exception("bench fallback: audit failed for %s", row.get("run_id"))
        audit_flags = {}

    report = {
        "task_id": task_id,
        "model": row.get("model") or "unknown",
        "solver_exit_code": 0,
        "solver_status": "ok",
        "solver_wall_clock_sec": round(solver_wall_s, 2),
        "extracted_fields": extracted,
        "per_field_scores": scoring_result["per_field_scores"],
        "total_score": scoring_result["total_score"],
        "audit_flags": audit_flags,
        "task_outputs": task_outputs,
        "notes": data.get("notes", ""),
        "run_dir": str(run_dir),
        "scored_by": "scheduler_bench_fallback",  # 标记是兜底评分而非 harness 写
    }
    try:
        (run_dir / "report.json").write_text(
            json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8"
        )
    except OSError:
        log.exception("bench fallback: write report.json failed for %s", row.get("run_id"))
        # 即便写 report 失败，DB row 仍能 update（kwargs 已备好）

    log.info("bench fallback scored %s: total=%.3f (harness died before Phase 7)",
             row.get("run_id"), scoring_result["total_score"])

    return (True, {
        "exit_code": 0,
        "wall_time_sec": solver_wall_s,
        "solver_status": "ok",
        "total_score": scoring_result["total_score"],
    })


@dataclass
class RunHandle:
    """active run 的内存映射；scheduler / runner / monitor 共享。

    `proc` 在真 runner 下是 asyncio.subprocess.Process；dummy runner 下可为 None。
    `report_path` (bench) 与 `result_path` (custom) 互斥（详 docs/04 §4.2 round 4 audit）。
    `started_at` 是 spawn 时的 time.time()；cancel fallback 用来回写 wall_time_sec
    （audit P0-3：cancel 与 monitor race 时 fallback 不再丢 wall_time_sec）。
    """
    run_id: str
    mode: str
    proc: Any                                   # asyncio.subprocess.Process | None
    run_dir: Any                                # Path
    transcript_path: Any                        # Path = run_dir / "workdir" / "transcript.json"
    report_path: Any = None                     # bench: run_dir / "report.json"；custom: None
    result_path: Any = None                     # custom: workdir / "result.json"；bench: None
    cancel_requested: bool = False
    started_at: Optional[float] = None          # time.time() 时间戳；None 表 dummy runner
    model: str = ""                             # 终态算 cost 时按 settings.available_models 查价格
    gpu_id: Optional[int] = None                # CUDA_VISIBLE_DEVICES；monitor 终态时归还到 GpuAllocator


class RunnerProtocol(Protocol):
    """B5.1 harness_wrapper / B5.2 solver_wrapper 实现此协议。"""

    async def spawn(self, db: Database, run_id: str) -> RunHandle: ...


@dataclass
class Scheduler:
    db: Database
    settings: Any
    runners: dict[str, RunnerProtocol] = field(default_factory=dict)
    registry: dict[str, RunHandle] = field(default_factory=dict)
    tick_interval: float = 1.0
    paused: bool = False
    _stop: asyncio.Event = field(default_factory=asyncio.Event)

    async def run_forever(self) -> None:
        # restart 后从 config_kv 恢复 paused 状态（用户上次软暂停后 restart 不应该自动 unpause）
        try:
            from backend.services.config_service import get_effective_paused
            self.paused = await get_effective_paused(self.db)
            if self.paused:
                log.info("scheduler started in paused state (restored from config_kv)")
        except Exception:
            log.exception("could not restore scheduler.paused from config_kv")
        log.info("scheduler started (tick=%.1fs, paused=%s)", self.tick_interval, self.paused)
        while not self._stop.is_set():
            try:
                await self._tick()
            except Exception:
                log.exception("scheduler tick failed")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.tick_interval)
            except asyncio.TimeoutError:
                pass
        log.info("scheduler stopped")

    def stop(self) -> None:
        self._stop.set()

    async def _tick(self) -> None:
        # 周期性 reap：扫 running 行的 pid，已死 → mark failed
        # （reconciler 只在 startup 跑一次；如果 backend restart 后 1-2s 子进程才死，
        # reconciler 不会回头扫 → DB row 永远卡 running，UI 误以为还在跑）
        await self._reap_dead_runs()
        # 软暂停：不动 running（让它们跑完）；只跳过 promote。reap 仍跑，避免 paused 期间死进程卡 running 状态
        if self.paused:
            return
        # cap 从 config_kv 读最新值（PUT /api/config 后立即生效）
        from backend.services.config_service import get_effective_max_concurrent
        cap = await get_effective_max_concurrent(self.settings, self.db)
        live = await dao.runs_count_live(self.db)
        slots = max(0, cap - live)
        if slots <= 0:
            return
        for _ in range(slots):
            run_id = await dao.queue_pop_next(self.db)
            if run_id is None:
                return
            await self._promote(run_id)

    async def _reap_dead_runs(self) -> None:
        # harness pid 死但 conmon / container 内 mpirun / pw.x 可能 detach 还活着
        # （监控正常 cancel 路径有 sweep；reaper 路径走的是 mark_failed，需要同样补刀）
        from backend.runners.cleanup import kill_run_dir_processes
        from backend.services.cost import compute_run_token_cost, sum_transcript_llm_wall_sec
        rows = await dao.runs_list(self.db, status="running", limit=1000)
        for r in rows:
            pid = r.get("pid")
            if not pid or _pid_alive(int(pid)):
                continue
            run_dir_str = r.get("run_dir")
            run_dir = Path(run_dir_str) if run_dir_str else None
            # reaper 路径没 RunHandle，从 row 推 transcript_path（与 RunHandle 同算法）
            transcript_path = (run_dir / "workdir" / "transcript.json") if run_dir else None
            in_total, out_total, hit_total, miss_total, cost_usd = (None, None, None, None, None)
            llm_wall_total_sec = None
            if transcript_path is not None:
                in_total, out_total, hit_total, miss_total, cost_usd = compute_run_token_cost(
                    transcript_path, self.settings, r.get("model") or "",
                )
                llm_wall_total_sec = sum_transcript_llm_wall_sec(transcript_path)
            cost_kwargs = dict(input_tokens_total=in_total,
                               output_tokens_total=out_total,
                               input_cache_hit_tokens_total=hit_total,
                               input_cache_miss_tokens_total=miss_total,
                               cost_usd=cost_usd,
                               llm_wall_total_sec=llm_wall_total_sec)
            check = _check_natural_completion(r, settings=self.settings)
            if check is None:
                log.warning("scheduler reap: %s pid=%s dead, no report/result, mark failed",
                            r["run_id"], pid)
                kill_run_dir_processes(run_dir, r["run_id"])
                await run_registry.mark_failed(
                    self.db, r["run_id"],
                    error="process died (reaped by scheduler)",
                    **cost_kwargs,
                )
                continue
            is_done, kwargs = check
            if is_done:
                log.info("scheduler reap: %s pid=%s dead but report.json complete, mark done",
                         r["run_id"], pid)
                # 正常完成路径：QE 已退出（report.json 才会 finalize），不需要 sweep
                await run_registry.mark_done(self.db, r["run_id"], **kwargs, **cost_kwargs)
            else:
                log.warning("scheduler reap: %s pid=%s dead with failure_reason in report, mark failed",
                            r["run_id"], pid)
                kill_run_dir_processes(run_dir, r["run_id"])
                await run_registry.mark_failed(self.db, r["run_id"], **kwargs, **cost_kwargs)

    async def _promote(self, run_id: str) -> None:
        row = await dao.run_get(self.db, run_id)
        if not row:
            log.warning("queued run not found in runs table: %s", run_id)
            return
        runner = self.runners.get(row["mode"])
        if runner is None:
            log.error("no runner for mode=%s; mark failed", row["mode"])
            await run_registry.mark_failed(
                self.db, run_id, error=f"no runner registered for mode={row['mode']}"
            )
            return
        try:
            handle = await runner.spawn(self.db, run_id)
        except Exception as e:
            log.exception("runner spawn failed for %s", run_id)
            await run_registry.mark_failed(
                self.db, run_id, error=f"spawn failed: {e}"
            )
            return
        self.registry[run_id] = handle
        # runner 自己负责 mark_running（spawn 后立刻），不在 scheduler 重复 update


@dataclass
class DummyRunner:
    """P3a smoke：直接 mark_running + mark_done，验证 queue→running→done 通路。
    不真实拉 subprocess；P5 之后被 harness_wrapper / solver_wrapper 取代。
    """

    async def spawn(self, db: Database, run_id: str) -> RunHandle:
        await run_registry.mark_running(db, run_id, pid=None)
        # 立刻完成（dummy）
        await run_registry.mark_done(
            db, run_id, exit_code=0, wall_time_sec=0.0,
            total_score=None, solver_status="ok", current_turn=0,
        )
        from pathlib import Path
        row = await dao.run_get(db, run_id)
        run_dir = Path(row["run_dir"]) if row else Path("/tmp")
        return RunHandle(
            run_id=run_id, mode=row["mode"] if row else "bench",
            proc=None, run_dir=run_dir,
            transcript_path=run_dir / "workdir" / "transcript.json",
        )
