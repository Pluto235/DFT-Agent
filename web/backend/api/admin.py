"""Admin endpoints (`/api/admin/*`)。

F3 Step 2 (2026-05-08)：原 middleware-level prefix gate（user.role == 'dev'）
已移除，改为 per-endpoint `Depends(require_capability(ADMIN_*))` 守门——让"normal
user 拿到 admin.view_usage 单 cap 也能调对应 endpoint"成为可能。is_dev_role 用户
通过 has_capability bypass 自动通过所有 admin.* gate。

提供：
- `GET  /api/admin/cluster_state`         —— admin.view_cluster        —— GPU / disk / 我们的 active 进程数 快照
- `GET  /api/admin/usage_stats`           —— admin.view_usage          —— runs 表按 status / model / owner 聚合
- `GET  /api/admin/gpu_policy`            —— admin.manage_gpu_policy   —— 当前 enabled GPU 集合
- `POST /api/admin/gpu_policy`            —— admin.manage_gpu_policy   —— 设 enabled GPU 集合
- `POST /api/admin/runs:recycle_running`  —— admin.manage_runs_bulk    —— 一键 pause + 把 running runs 重新入队
"""
from __future__ import annotations

import asyncio
import os
import re
import secrets
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, Optional

try:
    import psutil  # CPU/mem snapshot；缺也不致命，下方 helper 返回 {}
except ImportError:  # pragma: no cover
    psutil = None  # type: ignore

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from backend.auth.capabilities import (
    ADMIN_MANAGE_GPU_POLICY,
    ADMIN_MANAGE_RUNS_BULK,
    ADMIN_VIEW_CLUSTER,
    ADMIN_VIEW_USAGE,
)
from backend.auth.deps import require_capability
from backend.auth.tokens import User
from backend.persistence import dao


router = APIRouter(prefix="/api/admin", tags=["admin"])


# ============ cluster_state ============

# nvidia-smi 子进程开销 ~0.3s；前端 2s 刷新如果每次都跑会浪费。短 TTL cache。
# TTL 略小于 frontend 轮询间隔（2s），保证每次轮询都能拿到新数据。
_CLUSTER_CACHE: dict = {"data": None, "ts": 0.0}
_CLUSTER_CACHE_TTL = 1.0


async def _run_cmd(cmd: list[str], timeout: float = 5.0) -> tuple[int, str, str]:
    """Async subprocess。返 (exit_code, stdout, stderr)。"""
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return proc.returncode or 0, out.decode(errors="replace"), err.decode(errors="replace")
    except (FileNotFoundError, asyncio.TimeoutError) as e:
        return 1, "", str(e)


def _read_cpu_snapshot() -> dict:
    """CPU 状态：cores / load / 总 util / 每核 util。
    psutil.cpu_percent(interval=0.2) 阻塞 200ms 拿采样窗口——比 100ms 长一倍
    可消 per-core jitter（idle 核 100ms 采样常瞬跳 0→100%），200ms 已较稳；
    上层用 asyncio.to_thread 包，不卡事件循环。psutil 缺失 → 返 {}。"""
    if psutil is None:
        return {}
    try:
        per_core = psutil.cpu_percent(interval=0.2, percpu=True)
        overall = sum(per_core) / len(per_core) if per_core else 0.0
        load = os.getloadavg()
        freq = psutil.cpu_freq()
        return {
            "cores_physical": psutil.cpu_count(logical=False),
            "cores_logical": psutil.cpu_count(logical=True),
            "util_pct": round(overall, 1),
            "per_core_pct": [round(p, 1) for p in per_core],
            "load_1m": round(load[0], 2),
            "load_5m": round(load[1], 2),
            "load_15m": round(load[2], 2),
            "freq_mhz": round(freq.current) if freq else None,
        }
    except (OSError, AttributeError):
        return {}


def _read_memory_snapshot() -> dict:
    """内存 + swap 快照。psutil 缺失 → 返 {}。"""
    if psutil is None:
        return {}
    try:
        vm = psutil.virtual_memory()
        sm = psutil.swap_memory()
        return {
            "total_bytes": vm.total,
            "used_bytes": vm.used,
            "available_bytes": vm.available,
            "free_bytes": vm.free,
            "used_pct": round(vm.percent, 1),
            "swap_total_bytes": sm.total,
            "swap_used_bytes": sm.used,
            "swap_used_pct": round(sm.percent, 1),
        }
    except (OSError, AttributeError):
        return {}


def _parse_csv(stdout: str) -> list[dict]:
    """nvidia-smi `--format=csv` 输出 → list[dict]。第一行 header。"""
    lines = [l.strip() for l in stdout.strip().splitlines() if l.strip()]
    if len(lines) < 2:
        return []
    header = [h.strip() for h in lines[0].split(",")]
    rows = []
    for line in lines[1:]:
        cells = [c.strip() for c in line.split(",")]
        if len(cells) == len(header):
            rows.append(dict(zip(header, cells)))
    return rows


@router.get("/cluster_state")
async def cluster_state(
    request: Request,
    _user: User = Depends(require_capability(ADMIN_VIEW_CLUSTER)),
) -> dict:
    """返 GPU 卡状态 + 我们这边占用 + 别人占用 + workspace disk + 我们 active proc 数。
    需要 admin.view_cluster cap（is_dev_role 自动 bypass）。"""
    now = time.time()
    if _CLUSTER_CACHE["data"] is not None and now - _CLUSTER_CACHE["ts"] < _CLUSTER_CACHE_TTL:
        return _CLUSTER_CACHE["data"]

    # CPU + 内存 snapshot 与 GPU 信息并发拿（psutil 100ms 阻塞放线程池）
    cpu_task = asyncio.to_thread(_read_cpu_snapshot)
    mem_task = asyncio.to_thread(_read_memory_snapshot)

    # GPU 基础信息
    code, gpu_out, _err = await _run_cmd([
        "nvidia-smi",
        "--query-gpu=index,name,utilization.gpu,memory.used,memory.total,temperature.gpu",
        "--format=csv,noheader,nounits",
    ])
    gpus: list[dict] = []
    if code == 0 and gpu_out:
        for line in gpu_out.strip().splitlines():
            cells = [c.strip() for c in line.split(",")]
            if len(cells) >= 6:
                try:
                    gpus.append({
                        "index": int(cells[0]),
                        "name": cells[1],
                        "util_pct": int(cells[2]),
                        "memory_used_mib": int(cells[3]),
                        "memory_total_mib": int(cells[4]),
                        "temp_c": int(cells[5]),
                    })
                except ValueError:
                    pass

    # GPU 上跑的 compute apps（含其他用户）
    code2, apps_out, _ = await _run_cmd([
        "nvidia-smi",
        "--query-compute-apps=pid,gpu_uuid,used_memory,process_name",
        "--format=csv,noheader,nounits",
    ])
    apps: list[dict] = []
    if code2 == 0 and apps_out:
        for line in apps_out.strip().splitlines():
            cells = [c.strip() for c in line.split(",")]
            if len(cells) >= 4:
                try:
                    apps.append({
                        "pid": int(cells[0]),
                        "gpu_uuid": cells[1],
                        "used_memory_mib": int(cells[2]),
                        "process_name": cells[3],
                    })
                except ValueError:
                    pass

    # 把 apps 关联到 gpu index：用 nvidia-smi -L 拿 index→uuid
    code3, list_out, _ = await _run_cmd(["nvidia-smi", "-L"])
    uuid_to_idx: dict[str, int] = {}
    if code3 == 0:
        # 行格式：`GPU 0: NVIDIA A100 ... (UUID: GPU-fd66...)`
        for line in list_out.splitlines():
            m = re.match(r"GPU (\d+):.*\(UUID: ([^)]+)\)", line.strip())
            if m:
                uuid_to_idx[m.group(2).strip()] = int(m.group(1))
    for app in apps:
        app["gpu_index"] = uuid_to_idx.get(app["gpu_uuid"], -1)

    # 按 gpu_index 区分自己 vs 别人；用进程 owner 判（uid==当前进程 uid）。简化：
    # 进程名是 `pw.x` / `python` 都算我们的 candidate；用 `ps` 批 lookup。但这里不够稳——
    # 直接看 process_name 是否是我们 stack（python/pw.x），且 pid 在我们已知 active runs 里。
    # 拿 active runs 的 pid 集合：
    db = request.app.state.db
    active_rows = await dao.runs_list(db, status="running", limit=200)
    our_pids: set[int] = set()
    for r in active_rows:
        if r.get("pid") is not None:
            our_pids.add(int(r["pid"]))

    # workspace 磁盘占用
    s = request.app.state.settings
    workspace = str(s.workspace_root)
    disk_total = disk_used = disk_free = 0
    try:
        usage = shutil.disk_usage(workspace)
        disk_total = usage.total
        disk_used = usage.used
        disk_free = usage.free
    except OSError:
        pass

    # 当前 scheduler 状态（已 promote run 数 / 排队 / pause）
    scheduler = request.app.state.scheduler
    paused = bool(getattr(scheduler, "paused", False))
    queued_n = await dao.runs_count_by_status(db, "queued")
    live_n = await dao.runs_count_by_status(db, "running") + await dao.runs_count_by_status(db, "evaluating")

    cpu_snap = await cpu_task
    mem_snap = await mem_task

    # 当前 QE GPU 排队模式（spawn 时读 settings.qe_gpu_queueing_mode；lifespan 已用
    # config_kv 持久值 mutate；admin POST 同步两边）
    qe_gpu_mode = getattr(request.app.state.settings, "qe_gpu_queueing_mode", "backend_pin")

    data = {
        "ts": now,
        "gpus": gpus,
        "compute_apps": apps,
        "cpu": cpu_snap,
        "memory": mem_snap,
        "disk": {
            "path": workspace,
            "total_bytes": disk_total,
            "used_bytes": disk_used,
            "free_bytes": disk_free,
        },
        "scheduler": {
            "paused": paused,
            "queued": queued_n,
            "live": live_n,
        },
        "qe_gpu_queueing_mode": qe_gpu_mode,
        "our_active_pids": sorted(our_pids),
    }
    _CLUSTER_CACHE["data"] = data
    _CLUSTER_CACHE["ts"] = now
    return data


# ============ usage_stats ============

# SQLite strftime 格式串：注意 %W 是 ISO-ish 周（00-53，周一为首日）。前端只用作
# label 字符串排序 / 显示，不解析回 Date，故无需严格 ISO-8601 周编号。
_BUCKET_FORMATS: dict[str, str] = {
    "day": "%Y-%m-%d",     # → 2026-05-08
    "week": "%Y-W%W",      # → 2026-W19
    "month": "%Y-%m",      # → 2026-05
}


@router.get("/usage_stats")
async def usage_stats(
    request: Request,
    since: Optional[str] = Query(None, description="ISO Z timestamp; only count runs created_at >= since"),
    bucket: Optional[Literal["day", "week", "month"]] = Query(
        None, description="time-series breakdown granularity; None → no by_period field returned (backward compat)"
    ),
    _user: User = Depends(require_capability(ADMIN_VIEW_USAGE)),
) -> dict:
    """runs 表聚合：总量 / 按 status / 按 model / 按 owner，可选 by_period 时间序列。
    cost_usd 累加（None 当 0）；wall_time_sec / qe_wall_sec 同样。
    bucket=day/week/month 时额外返回 by_period 列表（升序按 period 字符串），空 bucket 不发。
    需要 admin.view_usage cap（is_dev_role 自动 bypass）。"""
    db = request.app.state.db
    where = ""
    params: tuple = ()
    if since:
        where = "WHERE created_at >= ?"
        params = (since,)

    # 总览
    total_row = await db.fetchone(
        f"SELECT COUNT(*) AS n, "
        f"COALESCE(SUM(cost_usd), 0) AS cost, "
        f"COALESCE(SUM(wall_time_sec), 0) AS wall, "
        f"COALESCE(SUM(qe_wall_sec), 0) AS qe_wall, "
        f"COALESCE(SUM(input_tokens_total), 0) AS input_tokens, "
        f"COALESCE(SUM(output_tokens_total), 0) AS output_tokens "
        f"FROM runs {where}",
        params,
    )

    # 按 status
    status_rows = await db.fetchall(
        f"SELECT status, COUNT(*) AS n FROM runs {where} GROUP BY status",
        params,
    )

    # 按 model
    model_rows = await db.fetchall(
        f"SELECT model, COUNT(*) AS n, "
        f"COALESCE(SUM(cost_usd), 0) AS cost, "
        f"COALESCE(SUM(input_tokens_total), 0) AS input_tokens, "
        f"COALESCE(SUM(output_tokens_total), 0) AS output_tokens "
        f"FROM runs {where} GROUP BY model ORDER BY n DESC",
        params,
    )

    # 按 owner
    owner_rows = await db.fetchall(
        f"SELECT owner, COUNT(*) AS n, "
        f"COALESCE(SUM(cost_usd), 0) AS cost, "
        f"COALESCE(SUM(wall_time_sec), 0) AS wall, "
        f"COALESCE(SUM(qe_wall_sec), 0) AS qe_wall, "
        f"COALESCE(SUM(input_tokens_total), 0) AS input_tokens, "
        f"COALESCE(SUM(output_tokens_total), 0) AS output_tokens "
        f"FROM runs {where} GROUP BY owner ORDER BY cost DESC",
        params,
    )

    out: dict = {
        "since": since,
        "total": dict(total_row) if total_row else {},
        "by_status": [dict(r) for r in status_rows],
        "by_model": [dict(r) for r in model_rows],
        "by_owner": [dict(r) for r in owner_rows],
    }

    # 可选 by_period（仅当 bucket 显式传入；保持向后兼容）
    if bucket is not None:
        fmt = _BUCKET_FORMATS[bucket]
        # strftime 在 SQLite 里直接对 created_at（ISO-Z 文本时间）切桶。
        # 升序排列，空桶天然不出现（GROUP BY 不会生成空组）。
        period_rows = await db.fetchall(
            f"SELECT strftime(?, created_at) AS period, "
            f"COUNT(*) AS n, "
            f"COALESCE(SUM(cost_usd), 0) AS cost_usd "
            f"FROM runs {where} "
            f"GROUP BY period ORDER BY period ASC",
            (fmt, *params),
        )
        out["by_period"] = [dict(r) for r in period_rows]

    return out


# ============ gpu_policy ============

class GpuPolicyUpdate(BaseModel):
    enabled: list[int] = Field(default_factory=list, description="GPU indices that scheduler may use")


@router.get("/gpu_policy")
async def get_gpu_policy(
    request: Request,
    _user: User = Depends(require_capability(ADMIN_MANAGE_GPU_POLICY)),
) -> dict:
    """当前 enabled GPU 集合 + 检测到的总 GPU 数。
    需要 admin.manage_gpu_policy cap；GET / POST 共用一个 cap 因为 GET 暴露
    的是 management 面板的当前态，没有"只读 cluster GPU"的更轻量场景。"""
    policy: set[int] = request.app.state.gpu_policy
    detected_total: int = request.app.state.gpu_detected_total
    return {
        "enabled": sorted(policy),
        "detected_total": detected_total,
        "all_indices": list(range(detected_total)),
    }


@router.post("/gpu_policy")
async def set_gpu_policy(
    request: Request, payload: GpuPolicyUpdate,
    _user: User = Depends(require_capability(ADMIN_MANAGE_GPU_POLICY)),
) -> dict:
    """设置 enabled GPU 集合（同时持久化到 config_kv 跨 restart 保留）。

    - 已 running 的 run 不动（GPU 已锁，强 kill 会丢工作）；只影响未来 promote
    - allocator 在 acquire() 里只挑 enabled 集合里的卡
    - 如果 enabled 为空 → 等价 scheduler 暂停所有新 run（promote 永远拿不到 GPU）
    需要 admin.manage_gpu_policy cap（is_dev_role 自动 bypass）。
    """
    from backend.services.config_service import set_gpu_policy as persist_gpu_policy
    detected_total: int = request.app.state.gpu_detected_total
    invalid = [i for i in payload.enabled if i < 0 or i >= detected_total]
    if invalid:
        raise HTTPException(
            status_code=400,
            detail=f"invalid gpu indices {invalid}; detected total = {detected_total}",
        )
    new_set = set(payload.enabled)
    request.app.state.gpu_policy = new_set
    db = request.app.state.db
    await persist_gpu_policy(db, new_set)
    return {
        "enabled": sorted(new_set),
        "detected_total": detected_total,
        "all_indices": list(range(detected_total)),
    }


# ============ qe_gpu_mode ============

_QE_GPU_MODE_VALUES = ("backend_pin", "wrapper_flock")


class QeGpuModeUpdate(BaseModel):
    mode: Literal["backend_pin", "wrapper_flock"] = Field(..., description="QE GPU 排队模式")


@router.get("/qe_gpu_mode")
async def get_qe_gpu_mode(
    request: Request,
    _user: User = Depends(require_capability(ADMIN_MANAGE_GPU_POLICY)),
) -> dict:
    """当前 QE GPU 排队模式 + 可选值。
    复用 admin.manage_gpu_policy cap（语义上同属 GPU 调度配置）。

    backend_pin   = spawn 时 pin 单卡，整 solver 生命周期持卡（默认，安全）
    wrapper_flock = 不 pin，wrapper 在 pw.x 调用级 flock 抢卡（需先部署 flock-aware wrapper）
    """
    mode = getattr(request.app.state.settings, "qe_gpu_queueing_mode", "backend_pin")
    return {"mode": mode, "available_modes": list(_QE_GPU_MODE_VALUES)}


@router.post("/qe_gpu_mode")
async def set_qe_gpu_mode_endpoint(
    request: Request, payload: QeGpuModeUpdate,
    _user: User = Depends(require_capability(ADMIN_MANAGE_GPU_POLICY)),
) -> dict:
    """切换模式：同时 mutate app.state.settings 运行时态 + 写 config_kv 跨重启保留。

    - 仅影响**未来 spawn 的 run**：现存 running run 已被 pin/分配，模式切换不影响
    - 切到 wrapper_flock 前 preflight：检查 settings.qe_gpu_wrapper_dir_wrapper_flock
      指向的 dir 下 qe-gpu-wrap.sh 含 `flock` 字面量；缺失/不含 → 400 + 部署提示
    - 切回 backend_pin 永远安全（不检查）
    需要 admin.manage_gpu_policy cap。
    """
    from backend.services.config_service import set_qe_gpu_mode as persist_mode

    s = request.app.state.settings

    if payload.mode == "wrapper_flock":
        wrapper_dir = Path(s.qe_gpu_wrapper_dir_wrapper_flock).expanduser()
        wrap_script = wrapper_dir / "qe-gpu-wrap.sh"
        if not wrapper_dir.is_dir() or not wrap_script.exists():
            raise HTTPException(
                status_code=400,
                detail=(
                    f"flock-aware wrapper 未部署：{wrap_script} 不存在。"
                    f"先 cp 原 wrapper 到 {wrapper_dir} 并在 qe-gpu-wrap.sh 加 flock 逻辑，"
                    f"或在 config.toml 设 qe_gpu_wrapper_dir_wrapper_flock 指向已部署位置。"
                ),
            )
        try:
            content = wrap_script.read_text()
        except OSError as e:
            raise HTTPException(status_code=400, detail=f"读 {wrap_script} 失败：{e}")
        if "flock" not in content:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"{wrap_script} 不含 `flock` 字面量——疑似不是 flock-aware wrapper。"
                    f"切到 wrapper_flock 模式但 wrapper 不支持 flock 会让所有 solver 抢 GPU 0。拒绝切换。"
                ),
            )

    db = request.app.state.db
    s.qe_gpu_queueing_mode = payload.mode  # 运行时态
    await persist_mode(db, payload.mode)   # 持久化
    return {"mode": payload.mode, "available_modes": list(_QE_GPU_MODE_VALUES)}


# ============ recycle running ============

class _RecycleRunningResponse(BaseModel):
    paused: bool                                  # 操作后 scheduler.paused（永远 True）
    recycled: list[dict]                           # [{old_run_id, new_run_id, task_id, owner}]
    failed: list[dict]                             # [{run_id, error_msg}]


def _make_placeholder_id(prefix: str) -> str:
    """与 api/runs._make_placeholder_id 同款；这里再实现一次避免循环依赖。"""
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")
    return f"{ts}_{prefix}_pending_{secrets.token_hex(3)}"


@router.post("/runs:recycle_running", response_model=_RecycleRunningResponse)
async def recycle_running_runs(
    request: Request,
    _user: User = Depends(require_capability(ADMIN_MANAGE_RUNS_BULK)),
) -> _RecycleRunningResponse:
    """一键 pause + 把当前 running/evaluating 的 run 用相同超参重新入队 + kill+delete 旧 run。

    需要 admin.manage_runs_bulk cap（is_dev_role 自动 bypass）。

    步骤：
    1. set scheduler.paused = True（持久化到 config_kv），防止新 queued 立即被 promote
    2. 列出所有 status IN ('running','evaluating') 的 run（dev 通吃，不按 owner gate）
    3. 对每条：
       a. 用同 (mode, model, agent_id, max_turns, solver_timeout_sec, owner, priority,
          task_id 或 custom_spec) 创建新 placeholder + queue_insert
       b. cancel 旧 run（SIGTERM/SIGKILL pgroup，等 monitor 收尾）
       c. 删除旧 row + rmtree run_dir（在 demo_runs_dir 之内）
    4. 单条失败不阻塞其他；返回成功 / 失败两份清单
    """
    from backend.runners.cancel import (
        RunInTerminalState,
        RunNotFound,
        cancel_run as do_cancel,
    )
    from backend.services.config_service import set_paused

    db = request.app.state.db
    s = request.app.state.settings
    sched = request.app.state.scheduler
    registry = request.app.state.run_registry
    demo_root = Path(s.demo_runs_dir).resolve()

    # 1) pause first（用 set_paused 持久化；与 /api/runs:pause 同款）
    sched.paused = True
    await set_paused(db, True)

    # 2) 找 active run
    rows_all = await dao.runs_list(db, status="all", limit=10000)
    active = [r for r in rows_all if r["status"] in ("running", "evaluating")]
    recycled: list[dict] = []
    failed: list[dict] = []

    for r in active:
        run_id = r["run_id"]
        try:
            # 3a) 用同超参插入新 placeholder
            mode = r["mode"]
            new_id = _make_placeholder_id(r.get("task_id") or "custom")
            await dao.run_insert(
                db,
                run_id=new_id,
                mode=mode,
                model=r["model"],
                status="queued",
                run_dir=str(s.demo_runs_dir / ".pending" / new_id),
                task_id=r.get("task_id"),
                custom_task_summary=r.get("custom_task_summary"),
                custom_spec=r.get("custom_spec"),
                source=r.get("source") or "demo",
                max_turns=int(r.get("max_turns") or 50),
                solver_timeout_sec=int(r.get("solver_timeout_sec") or 7200),
                owner=r.get("owner") or "localhost",
                priority=r.get("priority") or "normal",
                agent_id=r.get("agent_id"),
            )
            await dao.queue_insert(db, new_id)

            # 3b) kill 旧 run
            try:
                await do_cancel(db=db, registry=registry, run_id=run_id,
                                reason="admin recycle_running", settings=s)
            except (RunNotFound, RunInTerminalState):
                pass  # 已不在 registry / 已终态，不阻塞 delete

            # 3c) 删旧 row + rmtree run_dir（仅 demo_runs_dir 之内才物理删；安全护栏）
            run_dir = r.get("run_dir")
            await dao.run_delete(db, run_id)
            if run_dir:
                rd = Path(run_dir)
                inside_demo = False
                try:
                    rel = rd.resolve().relative_to(demo_root)
                    inside_demo = str(rel) not in ("", ".")
                except ValueError:
                    inside_demo = False
                if inside_demo and rd.exists() and rd.is_dir():
                    try:
                        shutil.rmtree(rd)
                    except OSError as e:
                        # 旧 row 已删；磁盘残留只是垃圾，不阻塞结果
                        failed.append({
                            "run_id": run_id,
                            "error_msg": f"row+queue ok but run_dir rmtree failed: {e}",
                        })
                        # 仍把 recycled 算进去——主操作（重入队 + cancel + 删 row）成功
            recycled.append({
                "old_run_id": run_id,
                "new_run_id": new_id,
                "task_id": r.get("task_id"),
                "owner": r.get("owner"),
            })
        except Exception as e:
            failed.append({"run_id": run_id, "error_msg": str(e)})

    return _RecycleRunningResponse(paused=True, recycled=recycled, failed=failed)
