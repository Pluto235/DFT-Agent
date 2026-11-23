from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Any, Optional, Union

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field

from backend.auth.capabilities import (
    ADMIN_MANAGE_SCHEDULER,
    MANAGE_CANCEL_ANY_RUN,
    MANAGE_DELETE_ANY_RUN,
    SUBMIT_URGENT_PRIORITY,
    VIEW_DEV_AGENTS,
    VIEW_DEV_TASKS,
    VIEW_OTHERS_RUNS,
)
from backend.auth.deps import require_capability
from backend.auth.tokens import User
from backend.persistence import dao
from backend.runners.cancel import (
    RunInTerminalState,
    RunNotFound,
    cancel_run as do_cancel,
)
from backend.runners.harness_wrapper import spawn_bench_run
from backend.runners.solver_wrapper import spawn_custom_run
from backend.schemas.batch import (
    BatchAccepted,
    BatchFailed,
    BatchSpec,
    BatchSubmitRequest,
    BatchSubmitResponse,
)
from backend.schemas.run import (
    BenchRunStartRequest,
    CancelRequest,
    CancelResponse,
    CustomRunStartRequest,
    RunListItem,
    RunStartResponse,
    TranscriptSnapshot,
)
from backend.schemas.result import RunResult
from backend.services.case_bundle import build_bundle
from backend.services.cost import estimate_cost
from backend.services.curve_data import compute_curve_compare
from backend.services.qe_progress import get_qe_progress
from backend.services.result_reader import read_run_result
from backend.services.transcript_reader import read_transcript_snapshot


router = APIRouter(prefix="/api/runs", tags=["runs"])


# Discriminator union (Pydantic 2.x)
_RunStartRequest = Annotated[
    Union[BenchRunStartRequest, CustomRunStartRequest],
    Field(discriminator="custom"),
]


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _row_to_list_item(
    row: dict,
    queue_total: Optional[int],
    queue_rank: Optional[dict[str, int]] = None,
) -> RunListItem:
    # 运行时 current_turn：DB 行只在终态时被写（mark_done/mark_failed），
    # 运行中 column 始终 0 → RunStrip 进度条不更新（用户实测 bug）。
    # 对 running/evaluating 行实时调 transcript_reader 算 turn count；终态走 DB cache。
    # 单行磁盘 I/O 几 ms，max_concurrent 限制下最多 5-6 row 受影响，可接受。
    # 终态 fallback：早期 monitor 漏写 ct → DB 列 0；这里若仍 0 且 transcript.json 还在，
    # 算一次（cleanup 只删 workdir/tmp*，transcript.json 保留）。
    ct = int(row.get("current_turn") or 0)
    if row.get("run_dir") and (
        row["status"] in ("running", "evaluating") or ct == 0
    ):
        try:
            snap = read_transcript_snapshot(
                run_dir=Path(row["run_dir"]),
                run_id=row["run_id"],
                status=row["status"],
                since_turn=0,
            )
            if snap.current_turn:
                ct = snap.current_turn
        except Exception:
            pass  # transcript 还没写或已删，保 DB cache
    # queue_position：用 queue_entries 顺序的 enumerate rank（pop 留 gap，
    # raw queue_position 不再连续），不是 DB 列原值；Runs 表里 #N/M 显示用
    qp: Optional[int] = None
    if row["status"] == "queued" and queue_rank is not None:
        qp = queue_rank.get(row["run_id"])
    # QE stats fallback：与 ct fallback 同理，终态 row 若 DB 列空且 bash.log 还在，
    # 算一遍（修历史 run 显示）。bash.log 不在 cleanup 删除范围。
    qe_count = row.get("qe_call_count")
    qe_wall = row.get("qe_wall_sec")
    qe_wall_self = row.get("qe_wall_self_reported_sec")
    qe_wall_bash = row.get("qe_wall_bash_cmd_sec")
    is_terminal = row["status"] in ("done", "failed", "cancelled")
    if is_terminal and qe_count is None and row.get("run_dir"):
        try:
            from backend.services.qe_stats import compute_qe_stats_detailed
            d = compute_qe_stats_detailed(
                Path(row["run_dir"]), finished_at=row.get("finished_at"),
            )
            if d["call_count"]:
                qe_count = d["call_count"]
                qe_wall = d["wall_sec"] if d["wall_sec"] > 0 else qe_wall
                qe_wall_self = d["self_wall_sec"] if d["self_wall_sec"] > 0 else qe_wall_self
                qe_wall_bash = d["bash_wall_sec"] if d["bash_wall_sec"] > 0 else qe_wall_bash
        except Exception:
            pass
    return RunListItem(
        run_id=row["run_id"],
        mode=row["mode"],
        task_id=row.get("task_id"),
        custom_task_summary=row.get("custom_task_summary"),
        custom_spec=row.get("custom_spec"),
        model=row["model"],
        status=row["status"],
        source=row["source"],
        created_at=row["created_at"],
        started_at=row.get("started_at"),
        finished_at=row.get("finished_at"),
        wall_time_sec=row.get("wall_time_sec"),
        total_score=row.get("total_score"),
        current_turn=ct,
        max_turns=int(row.get("max_turns") or 50),
        exit_code=row.get("exit_code"),
        error=row.get("error"),
        queue_position=qp,
        queue_total=queue_total if row["status"] == "queued" else None,
        run_dir=row["run_dir"],
        input_tokens_total=row.get("input_tokens_total"),
        output_tokens_total=row.get("output_tokens_total"),
        cost_usd=row.get("cost_usd"),
        owner=row.get("owner") or "localhost",
        priority=row.get("priority") or "normal",
        agent_id=row.get("agent_id"),
        qe_backend=row.get("qe_backend") or "gpu",
        qe_call_count=qe_count,
        qe_wall_sec=qe_wall,
        qe_wall_self_reported_sec=qe_wall_self,
        qe_wall_bash_cmd_sec=qe_wall_bash,
        llm_wall_total_sec=row.get("llm_wall_total_sec"),
    )


def _can_cancel_run(user: User, row: dict) -> bool:
    """阿里云 cluster 逻辑：所有人都能看；只有 owner 自己 + 拿 manage.cancel_any_run cap
    的 user 能 cancel 别人的 run。is_dev_role 通过 has_capability bypass 自动通过。"""
    if user.has_capability(MANAGE_CANCEL_ANY_RUN):
        return True
    return (row.get("owner") or "localhost") == user.owner


def _can_delete_run(user: User, row: dict) -> bool:
    """同 _can_cancel_run，但用 manage.delete_any_run cap。两 cap 拆开是因为
    delete 比 cancel 多一步物理删 run_dir，破坏性更大；现实可能希望某 user
    能 cancel 别人但不能删别人的 run。"""
    if user.has_capability(MANAGE_DELETE_ANY_RUN):
        return True
    return (row.get("owner") or "localhost") == user.owner


def _check_priority_allowed(priority: str, user: User) -> None:
    """urgent 优先级要求 submit.urgent_priority cap；is_dev_role 自动 bypass。
    normal / high 全开放。缺 cap 用 urgent → 403。"""
    if priority == "urgent" and not user.has_capability(SUBMIT_URGENT_PRIORITY):
        raise HTTPException(
            status_code=403,
            detail="priority 'urgent' requires capability submit.urgent_priority",
        )


async def _stable_task_ids(settings, db) -> set[str]:
    """normal role 可见的 bench task_id 集合（is_stable=True 或缺省）。

    与 backend/api/tasks.py:14 的 _user_can_see_task 同样的 role-routing 语义；
    runs / leaderboard 端共用此 helper 决定是否 hide 一条 row。
    custom run（task_id is None）不参与本集合，由调用端单独处理。
    """
    from backend.services.task_loader import load_all_tasks
    items = await load_all_tasks(settings, db)
    return {t.task_id for t in items if t.is_stable}


def _unstable_agent_ids(settings) -> set[str]:
    """dev-only agent 的 id 集合（settings.agents 中 is_stable=False）。

    agent.is_stable=False 用来标记开发中 / 测试期 agent（如 v2 早期）；
    normal role 不应在 nav 下拉栏看到（已由 ConfigResponse 过滤），
    也不应看到这些 agent 起的 run（本集合用于此处过滤）。
    """
    return {a.id for a in getattr(settings, "agents", []) if not getattr(a, "is_stable", True)}


def _user_can_see_run(
    row: dict, user: User, stable_ids: set[str],
    unstable_agents: Optional[set[str]] = None,
) -> bool:
    """三层 capability filter；缺哪个挡哪一边，is_dev_role bypass 一切。

      - task 维度：缺 VIEW_DEV_TASKS 时，bench run 的 task_id 必须在 stable_ids
      - agent 维度：缺 VIEW_DEV_AGENTS 时，row.agent_id 不能落在 unstable_agents
      - owner 维度：缺 VIEW_OTHERS_RUNS 时，row.owner 必须 == user.owner
        （default 新建 token 带此 cap，行为等价历史 normal）

    custom run（无 task_id）的 task 维度一律 visible（leaderboard 已排除 custom），
    agent 维度按规则过滤。
    """
    if not user.has_capability(VIEW_DEV_TASKS):
        tid = row.get("task_id")
        if tid and tid not in stable_ids:
            return False
    if not user.has_capability(VIEW_DEV_AGENTS) and unstable_agents:
        aid = row.get("agent_id")
        if aid and aid in unstable_agents:
            return False
    if not user.has_capability(VIEW_OTHERS_RUNS):
        row_owner = row.get("owner") or "localhost"
        if row_owner != user.owner:
            return False
    return True


# ============ list / detail ============

@router.get("", response_model=list[RunListItem])
async def list_runs(
    request: Request,
    source: str = Query(default="all"),
    status: str = Query(default="all"),
    mode: str = Query(default="all"),
    task_id: Optional[str] = Query(default=None),
    limit: int = Query(default=200, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
) -> list[RunListItem]:
    s = request.app.state.settings
    db = request.app.state.db
    user: User = request.state.user
    rows = await dao.runs_list(
        db, source=source, status=status, mode=mode, task_id=task_id,
        limit=limit, offset=offset,
    )
    queue_rows = await dao.queue_list(db)
    queue_rank: dict[str, int] = {r["run_id"]: i + 1 for i, r in enumerate(queue_rows)}
    queue_total = len(queue_rows)
    # status=queued 但已不在 queue_entries：scheduler 已 pop、spawn 在 wait_for_workdir_marker
    # （bench 最长 15s）期间，placeholder 行还存在但用户不该再看到它"排队"。直接从
    # 列表里过滤；real_id 行随后由 spawn_*_run 创建，前端 polling 自然刷上来。
    visible = [
        r for r in rows
        if r["status"] != "queued" or r["run_id"] in queue_rank
    ]
    # capability filter：3 维（task / agent / owner）每维独立 gate；
    # is_dev_role 短路全部。post-filter 后 limit 数量可能减少（已知 trade-off，不动 dao）。
    # 始终调 _user_can_see_run（dev 内部短路，开销小；少一个分支条件让逻辑清晰）。
    stable_ids = await _stable_task_ids(s, db)
    unstable_agents = _unstable_agent_ids(s)
    visible = [r for r in visible if _user_can_see_run(r, user, stable_ids, unstable_agents)]
    return [_row_to_list_item(r, queue_total, queue_rank) for r in visible]


@router.get("/{run_id}", response_model=RunListItem)
async def get_run(run_id: str, request: Request) -> RunListItem:
    s = request.app.state.settings
    db = request.app.state.db
    user: User = request.state.user
    row = await dao.run_get(db, run_id)
    if not row:
        raise HTTPException(status_code=404, detail=f"run '{run_id}' not found")
    # capability filter：缺 view.dev_tasks/dev_agents 的 user 看 unstable / dev-only agent
    # 的 run，按 404 不暴露存在性
    stable_ids = await _stable_task_ids(s, db)
    unstable_agents = _unstable_agent_ids(s)
    if not _user_can_see_run(row, user, stable_ids, unstable_agents):
        raise HTTPException(status_code=404, detail=f"run '{run_id}' not found")
    if row["status"] == "queued":
        queue_rows = await dao.queue_list(db)
        queue_rank = {r["run_id"]: i + 1 for i, r in enumerate(queue_rows)}
        queue_total = len(queue_rows)
        return _row_to_list_item(row, queue_total, queue_rank)
    return _row_to_list_item(row, None)


# ============ transcript / result ============

@router.get("/{run_id}/transcript", response_model=TranscriptSnapshot)
async def get_transcript(
    run_id: str, request: Request, since_turn: int = Query(default=0, ge=0),
) -> TranscriptSnapshot:
    s = request.app.state.settings
    db = request.app.state.db
    user: User = request.state.user
    row = await dao.run_get(db, run_id)
    if not row:
        raise HTTPException(status_code=404, detail=f"run '{run_id}' not found")
    # capability filter：缺 view.dev_* cap 的 user 按 404 不暴露存在性
    stable_ids = await _stable_task_ids(s, db)
    unstable_agents = _unstable_agent_ids(s)
    if not _user_can_see_run(row, user, stable_ids, unstable_agents):
        raise HTTPException(status_code=404, detail=f"run '{run_id}' not found")
    return read_transcript_snapshot(
        run_id=run_id, status=row["status"],
        run_dir=Path(row["run_dir"]), since_turn=since_turn,
        started_at=row.get("started_at"),
        finished_at=row.get("finished_at"),
    )


@router.get("/{run_id}/result", response_model=RunResult)
async def get_result(run_id: str, request: Request) -> RunResult:
    s = request.app.state.settings
    db = request.app.state.db
    user: User = request.state.user
    row = await dao.run_get(db, run_id)
    if not row:
        raise HTTPException(status_code=404, detail=f"run '{run_id}' not found")
    # capability filter：缺 view.dev_* cap 的 user 按 404 不暴露存在性
    stable_ids = await _stable_task_ids(s, db)
    unstable_agents = _unstable_agent_ids(s)
    if not _user_can_see_run(row, user, stable_ids, unstable_agents):
        raise HTTPException(status_code=404, detail=f"run '{run_id}' not found")
    res = read_run_result(
        run_id=run_id, mode=row["mode"], run_dir=Path(row["run_dir"]),
        model=row["model"], exit_code=row.get("exit_code"),
        wall_time_sec=row.get("wall_time_sec"),
        solver_status=row.get("solver_status"),
        custom_task_dir=s.custom_task_dir,
        input_tokens_total=row.get("input_tokens_total"),
        output_tokens_total=row.get("output_tokens_total"),
        cost_usd=row.get("cost_usd"),
        bench_questions_dir=s.bench_questions_dir,
    )
    if res is None:
        raise HTTPException(status_code=404, detail=f"result not yet available for '{run_id}'")
    return res


# ============ failure analysis (AI-powered) ============

@router.get("/{run_id}/failure_analysis")
async def get_failure_analysis(run_id: str, request: Request) -> dict:
    """读 run_dir/failure_analysis.md 缓存。无缓存返 cached=False。"""
    s = request.app.state.settings
    db = request.app.state.db
    user: User = request.state.user
    row = await dao.run_get(db, run_id)
    if not row:
        raise HTTPException(status_code=404, detail=f"run '{run_id}' not found")
    # capability filter：缺 view.dev_* cap 的 user 按 404 不暴露存在性
    stable_ids = await _stable_task_ids(s, db)
    unstable_agents = _unstable_agent_ids(s)
    if not _user_can_see_run(row, user, stable_ids, unstable_agents):
        raise HTTPException(status_code=404, detail=f"run '{run_id}' not found")
    from backend.services.failure_analyzer import get_cached_analysis
    cached = get_cached_analysis(Path(row["run_dir"]))
    return {"cached": cached is not None, "content": cached, "model": "claude-opus-4-7"}


@router.post("/{run_id}/failure_analysis")
async def trigger_failure_analysis(
    run_id: str, request: Request, force: bool = False,
) -> dict:
    """触发 AI 故障分析（调 anthropic API）。force=True 跳过 cache 强制重算。"""
    db = request.app.state.db
    s = request.app.state.settings
    row = await dao.run_get(db, run_id)
    if not row:
        raise HTTPException(status_code=404, detail=f"run '{run_id}' not found")
    if row["status"] not in ("failed", "cancelled"):
        raise HTTPException(
            status_code=400,
            detail=f"failure analysis only for failed/cancelled runs, got status={row['status']}",
        )
    api_key = (s.anthropic_api_key or "").strip()
    if not api_key:
        raise HTTPException(status_code=503, detail="ANTHROPIC_API_KEY not configured")
    from backend.services.failure_analyzer import analyze_failure
    try:
        content = await analyze_failure(
            run_dir=Path(row["run_dir"]),
            error=row.get("error"),
            wall_time_sec=row.get("wall_time_sec"),
            qe_call_count=row.get("qe_call_count"),
            current_turn=row.get("current_turn"),
            max_turns=row.get("max_turns"),
            api_key=api_key,
            force=force,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"analyze failed: {e}") from e
    return {"cached": True, "content": content, "model": "claude-opus-4-7"}


# ============ start ============

def _build_start_response(
    row: dict, max_turns: int, *,
    queue_position: Optional[int] = None,
    queue_total: Optional[int] = None,
) -> RunStartResponse:
    return RunStartResponse(
        run_id=row["run_id"],
        task_id=row.get("task_id"),
        mode=row["mode"],
        model=row["model"],
        status=row.get("status", "queued"),
        created_at=row["created_at"],
        transcript_url=f"/api/runs/{row['run_id']}/transcript",
        result_url=f"/api/runs/{row['run_id']}/result",
        run_dir=row["run_dir"],
        queue_position=queue_position,
        queue_total=queue_total,
        max_turns=max_turns,
        current_turn=0,
        owner=row.get("owner") or "localhost",
        priority=row.get("priority") or "normal",
        agent_id=row.get("agent_id"),
        qe_backend=row.get("qe_backend") or "gpu",
    )


def _make_placeholder_id(prefix: str) -> str:
    """生成 queued placeholder run_id；scheduler promote 时调 spawn 生成 real_id 写入新 row，
    placeholder 行被 dao.run_delete 清理。前缀 = task_id (bench) 或 'custom'。"""
    import secrets
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")
    return f"{ts}_{prefix}_pending_{secrets.token_hex(3)}"


_CAPACITY_WAIT_TIMEOUT_S = 60.0


async def _await_capacity(s: Any, db) -> None:
    """串行等容量空：spawn_lock 持有 + while live >= cap → asyncio.sleep(2) 轮询。
    超过 _CAPACITY_WAIT_TIMEOUT_S 仍无容量 → raise TimeoutError → endpoint 返 429。

    用户期望"超出 max_concurrent_runs 的请求自动排队"。本实现不显式入 queue 表
    （需要 placeholder run_id 切换 UX），改为 endpoint 端 await。短时排队 OK；
    超过 60s 仍无容量则报错（避免浏览器 fetch timeout 死等）。
    cap 从 config_kv 读最新值（PUT /api/config 后立即生效），不读 settings stale 值。"""
    from backend.services.config_service import get_effective_max_concurrent
    deadline = asyncio.get_event_loop().time() + _CAPACITY_WAIT_TIMEOUT_S
    while True:
        cap = await get_effective_max_concurrent(s, db)
        live = await dao.runs_count_live(db)
        if live < cap:
            return
        if asyncio.get_event_loop().time() >= deadline:
            raise asyncio.TimeoutError(
                f"capacity full (live={live}/cap={cap}) for {int(_CAPACITY_WAIT_TIMEOUT_S)}s; "
                f"please wait, increase max_parallel, or cancel a running run"
            )
        await asyncio.sleep(2.0)


@router.post("", response_model=RunStartResponse)
async def start_run(req: _RunStartRequest, request: Request) -> RunStartResponse:
    """统一入队：所有 launch 都走 placeholder + queue_entries，scheduler 看
    max_concurrent_runs 决定 promote 时机。endpoint 立即返回（不阻塞），
    user 看到 status='queued' 与 queue_position；scheduler tick 1s 内 promote
    （容量空时）→ placeholder 行删除 + real_id 行 status='running' 出现,
    frontend 通过 polling 自然看到切换。"""
    s = request.app.state.settings
    db = request.app.state.db
    user: User = request.state.user

    # 模型校验
    _by_id = {m.id: m for m in s.available_models}
    if req.model not in _by_id:
        raise HTTPException(status_code=400, detail=f"model '{req.model}' not in available_models")
    _m = _by_id[req.model]
    if not getattr(_m, "available", True):
        raise HTTPException(
            status_code=400,
            detail=(
                f"model '{req.model}' (provider={_m.provider}) is listed but not enabled in this "
                f"deployment — solver code path supports it (multi-provider native SDK dispatch), "
                f"but the API key for {_m.provider} is not configured or the model has not been "
                f"smoke-verified here yet. Pick a model with available=true."
            ),
        )

    # agent 校验 + resolve solver_path：req.agent_id 给则查表，否则默认 agents[0]
    if not s.agents:
        raise HTTPException(status_code=500, detail="no agents configured in settings")
    agents_by_id = {a.id: a for a in s.agents}
    agent_id = req.agent_id or s.agents[0].id
    if agent_id not in agents_by_id:
        raise HTTPException(
            status_code=400,
            detail=f"agent '{agent_id}' not in available agents (configured: {list(agents_by_id)})",
        )
    agent = agents_by_id[agent_id]
    if not agent.available:
        raise HTTPException(
            status_code=400,
            detail=f"agent '{agent_id}' ({agent.name}) is not enabled in this deployment",
        )
    # capability 校验：缺 view.dev_agents 不能用 dev-only agent（is_stable=False）。
    # dev-only agent 不会出现在 GET /api/config 返回的 agents 列表里（同 cap 守门），
    # 正常 UI 选不到；这里挡 API 直调。
    if not user.has_capability(VIEW_DEV_AGENTS) and not bool(getattr(agent, "is_stable", True)):
        raise HTTPException(
            status_code=403,
            detail=f"agent '{agent_id}' is dev-only; missing capability view.dev_agents",
        )
    # req.solver_path 显式给（backward compat / dev override）→ 用它；否则用 resolved agent path
    if req.solver_path and req.solver_path != agent.solver_path:
        raise HTTPException(status_code=400, detail="Choose a registered agent.")
    req.solver_path = agent.solver_path

    # priority 校验：urgent 要求 submit.urgent_priority cap
    _check_priority_allowed(req.priority, user)

    if isinstance(req, CustomRunStartRequest):
        if len(req.prompt) < 20:
            raise HTTPException(status_code=400, detail="prompt must be at least 20 chars")
        custom_task_type_ids = {t.id for t in s.task_types if t.supported_in_custom}
        if req.task_type not in custom_task_type_ids:
            raise HTTPException(
                status_code=400,
                detail=f"task_type '{req.task_type}' not supported in custom mode",
            )
        if not _custom_material_has_source(req.material):
            raise HTTPException(
                status_code=400,
                detail="material spec must have at least one of: struct_data / formula_text / bench_task_id",
            )
        placeholder_id = _make_placeholder_id("custom")
        custom_summary = (req.prompt[:60] + "…") if len(req.prompt) > 60 else req.prompt
        custom_spec = {
            "prompt": req.prompt,
            "material": req.material.model_dump() if hasattr(req.material, "model_dump") else dict(req.material),
            "task_type": req.task_type,
            "model": req.model,
            "max_turns": req.max_turns,
            "max_output_tokens": req.max_output_tokens,
        }
        await dao.run_insert(
            db, run_id=placeholder_id, mode="custom", model=req.model,
            status="queued",
            run_dir=str(s.demo_runs_dir / ".pending" / placeholder_id),
            custom_task_summary=custom_summary, custom_spec=custom_spec,
            max_turns=req.max_turns,
            solver_timeout_sec=req.solver_timeout_sec,
            owner=user.owner,
            priority=req.priority,
            agent_id=agent_id,
            qe_backend=req.qe_backend,
        )
    else:  # BenchRunStartRequest
        from backend.services.task_loader import get_task
        task = await get_task(s, db, req.task_id)
        if task is None:
            raise HTTPException(status_code=400, detail=f"task '{req.task_id}' not found in bench library")
        # capability 校验：缺 view.dev_tasks 不能跑 unstable 题目。不暴露存在性 → 按 not found 报。
        if not user.has_capability(VIEW_DEV_TASKS) and not task.is_stable:
            raise HTTPException(status_code=400, detail=f"task '{req.task_id}' not found in bench library")
        placeholder_id = _make_placeholder_id(req.task_id)
        await dao.run_insert(
            db, run_id=placeholder_id, mode="bench", model=req.model,
            status="queued",
            run_dir=str(s.demo_runs_dir / ".pending" / placeholder_id),
            task_id=req.task_id,
            max_turns=req.max_turns,
            solver_timeout_sec=req.solver_timeout_sec,
            owner=user.owner,
            priority=req.priority,
            agent_id=agent_id,
            qe_backend=req.qe_backend,
        )

    # 先 insert 入队（DAO 返 raw queue_position，可能有 pop/cancel 留下的 gap）；
    # toast / response 暴露的应该是用户可见 rank，而不是 raw position。
    # 新插入的总在队尾 → rank == queue_count（包含自身）== queue_total。
    await dao.queue_insert(db, placeholder_id)
    queue_total = await dao.queue_count(db)
    row = await dao.run_get(db, placeholder_id)
    return _build_start_response(
        row, max_turns=req.max_turns,
        queue_position=queue_total, queue_total=queue_total,
    )


def _custom_material_has_source(m) -> bool:
    src = getattr(m, "source", None) if not isinstance(m, dict) else m.get("source")
    if src == "describe":
        return bool(getattr(m, "formula_text", None) or (isinstance(m, dict) and m.get("formula_text")))
    if src == "pick":
        return bool(getattr(m, "bench_task_id", None) or (isinstance(m, dict) and m.get("bench_task_id")))
    if src == "upload":
        return bool(getattr(m, "struct_data", None) or (isinstance(m, dict) and m.get("struct_data")))
    return False


# ============ batch ============

@router.post(":batch", response_model=BatchSubmitResponse)
async def start_batch(req: BatchSubmitRequest, request: Request) -> BatchSubmitResponse:
    """batch enqueue — 全 placeholder 入队；scheduler 按 max_concurrent_runs 自动 promote。

    串行 enqueue：避免 queue_insert MAX(position)+1 race（async 并发同时读会产生
    重复 position）。50 spec 串行 ≈ 400ms 可接受。spec 校验失败的不影响其他。
    """
    s = request.app.state.settings
    db = request.app.state.db
    user: User = request.state.user

    _by_id = {m.id: m for m in s.available_models}
    if req.model not in _by_id:
        raise HTTPException(status_code=400, detail=f"model '{req.model}' not in available_models")
    if not getattr(_by_id[req.model], "available", True):
        raise HTTPException(
            status_code=400,
            detail=(
                f"model '{req.model}' (provider={_by_id[req.model].provider}) listed but not "
                f"enabled in this deployment. Pick a model with available=true."
            ),
        )

    # batch-level priority 一次性校验；spec-level override 进 loop 时再校验
    _check_priority_allowed(req.priority, user)

    # batch 共享一个 agent（spec 不区分），从 req.agent_id resolve；缺省走 default
    if not s.agents:
        raise HTTPException(status_code=500, detail="no agents configured in settings")
    agents_by_id = {a.id: a for a in s.agents}
    batch_agent_id = getattr(req, "agent_id", None) or s.agents[0].id
    if batch_agent_id not in agents_by_id:
        raise HTTPException(status_code=400, detail=f"agent '{batch_agent_id}' not in available agents")

    from backend.services.task_loader import get_task

    accepted: list[BatchAccepted] = []
    failed: list[BatchFailed] = []
    for idx, spec in enumerate(req.specs):
        spec_model = spec.model or req.model
        spec_max_turns = spec.max_turns or req.max_turns
        spec_priority = spec.priority or req.priority
        if spec_model not in _by_id:
            failed.append(BatchFailed(
                spec_index=idx, spec=spec,
                error_code="model_invalid", error_msg=f"model '{spec_model}' not in available_models",
            ))
            continue
        if not getattr(_by_id[spec_model], "available", True):
            failed.append(BatchFailed(
                spec_index=idx, spec=spec,
                error_code="model_invalid",
                error_msg=f"model '{spec_model}' (provider={_by_id[spec_model].provider}) not enabled in this deployment",
            ))
            continue
        task = await get_task(s, db, spec.task_id)
        if task is None:
            failed.append(BatchFailed(
                spec_index=idx, spec=spec,
                error_code="task_not_found", error_msg=f"task '{spec.task_id}' not found",
            ))
            continue
        # capability 校验：缺 view.dev_tasks 不能跑 unstable 题目。同步按 task_not_found 报，不暴露存在性。
        if not user.has_capability(VIEW_DEV_TASKS) and not task.is_stable:
            failed.append(BatchFailed(
                spec_index=idx, spec=spec,
                error_code="task_not_found", error_msg=f"task '{spec.task_id}' not found",
            ))
            continue
        # priority 校验：spec 显式 urgent 但 user 缺 submit.urgent_priority → 单条失败，其他仍走
        if spec_priority == "urgent" and not user.has_capability(SUBMIT_URGENT_PRIORITY):
            failed.append(BatchFailed(
                spec_index=idx, spec=spec,
                error_code="priority_forbidden",
                error_msg="priority 'urgent' requires capability submit.urgent_priority",
            ))
            continue
        placeholder_id = _make_placeholder_id(spec.task_id)
        try:
            await dao.run_insert(
                db, run_id=placeholder_id, mode="bench", model=spec_model,
                status="queued",
                run_dir=str(s.demo_runs_dir / ".pending" / placeholder_id),
                task_id=spec.task_id,
                max_turns=spec_max_turns,
                solver_timeout_sec=getattr(req, "solver_timeout_sec", 7200),
                owner=user.owner,
                priority=spec_priority,
                agent_id=batch_agent_id,
                qe_backend=getattr(req, "qe_backend", "gpu"),
            )
            await dao.queue_insert(db, placeholder_id)
        except Exception as e:
            failed.append(BatchFailed(
                spec_index=idx, spec=spec, error_code="enqueue_failed",
                error_msg=str(e),
            ))
            continue
        # 同 POST /api/runs：rank == queue_count（新行在尾），不用 raw queue_position。
        rank = await dao.queue_count(db)
        accepted.append(BatchAccepted(
            spec_index=idx, run_id=placeholder_id, task_id=spec.task_id,
            queue_position=rank,
            queue_total=rank,
        ))
    return BatchSubmitResponse(accepted=accepted, failed=failed)


# ============ cancel ============

@router.post("/{run_id}:cancel", response_model=CancelResponse)
async def cancel_run(
    run_id: str, request: Request,
    body: CancelRequest = Body(default=CancelRequest()),
) -> CancelResponse:
    db = request.app.state.db
    registry = request.app.state.run_registry
    s = request.app.state.settings
    user = request.state.user

    # ownership：缺 manage.cancel_any_run 只能 cancel 自己的；is_dev_role 通吃。
    # row 不存在仍走 do_cancel 路径返 404。
    row = await dao.run_get(db, run_id)
    if row and not _can_cancel_run(user, row):
        raise HTTPException(
            status_code=403,
            detail=f"run '{run_id}' belongs to another user; cancel denied",
        )

    try:
        result = await do_cancel(
            db=db, registry=registry, run_id=run_id, reason=body.reason, settings=s,
        )
    except RunNotFound:
        raise HTTPException(status_code=404, detail=f"run '{run_id}' not found")
    except RunInTerminalState:
        raise HTTPException(status_code=409, detail="run already in terminal state")

    now = _now_iso()
    return CancelResponse(
        run_id=run_id,
        status="cancelled",
        cancel_requested_at=now,
        cancelled_at=now,
        error=body.reason or "cancelled by user",
    )


# ============ bulk cancel-active ============

class _CancelActiveResponse(BaseModel):
    cancelled: list[str]
    failed: list[dict]  # [{run_id, error_msg}]


@router.post(":cancel_active", response_model=_CancelActiveResponse)
async def cancel_active(request: Request) -> _CancelActiveResponse:
    """一键取消所有 queued + running + evaluating runs。

    阿里云逻辑：缺 manage.cancel_any_run 的 user 只批量 cancel 自己的；
    持该 cap（含 is_dev_role bypass）通吃。并发处理：每条调 do_cancel；
    queued 即时返；running/evaluating 走 SIGTERM 5s grace。cap=2 时总 wall ~5-10s。
    """
    import asyncio as _asyncio
    db = request.app.state.db
    registry = request.app.state.run_registry
    s = request.app.state.settings
    user: User = request.state.user
    rows = await dao.runs_list(db, status="all", limit=10000)
    active_ids = [
        r["run_id"] for r in rows
        if r["status"] in ("queued", "running", "evaluating")
        and _can_cancel_run(user, r)
    ]
    cancelled: list[str] = []
    failed: list[dict] = []

    async def _one(rid: str) -> tuple[str, str | None]:
        try:
            await do_cancel(db=db, registry=registry, run_id=rid, reason="bulk cancel-active", settings=s)
            return (rid, None)
        except RunInTerminalState:
            return (rid, None)  # 已终态，等价已取消，算成功
        except RunNotFound:
            return (rid, "not found")
        except Exception as e:
            return (rid, str(e))

    results = await _asyncio.gather(*(_one(rid) for rid in active_ids))
    for rid, err in results:
        if err is None:
            cancelled.append(rid)
        else:
            failed.append({"run_id": rid, "error_msg": err})
    return _CancelActiveResponse(cancelled=cancelled, failed=failed)


# ============ bulk delete ============

class _DeleteBatchRequest(BaseModel):
    run_ids: list[str]


class _DeleteBatchResponse(BaseModel):
    deleted: list[str]
    failed: list[dict]  # [{run_id, error_msg}]


# ============ scheduler pause / resume ============

class _SchedulerStateResponse(BaseModel):
    paused: bool
    queued: int        # 当前 queue 排队数（信息性，给 UI 显示 "N queued waiting"）
    live: int          # 当前 running + evaluating 数（paused 时仍可能 > 0，跑完为止）
    gpu_allocation: dict[int, int] = {}  # {gpu_id: active_run_count}；空池（CPU-only）{}


@router.get(":scheduler_state", response_model=_SchedulerStateResponse)
async def scheduler_state(request: Request) -> _SchedulerStateResponse:
    db = request.app.state.db
    sched = request.app.state.scheduler
    queued = await dao.queue_count(db)
    live = await dao.runs_count_live(db)
    allocator = getattr(request.app.state, "gpu_allocator", None)
    gpu_alloc = allocator.snapshot() if allocator is not None else {}
    return _SchedulerStateResponse(paused=bool(sched.paused), queued=queued, live=live,
                                    gpu_allocation=gpu_alloc)


@router.post(":pause", response_model=_SchedulerStateResponse)
async def pause_scheduler(
    request: Request,
    _user: User = Depends(require_capability(ADMIN_MANAGE_SCHEDULER)),
) -> _SchedulerStateResponse:
    """软暂停：scheduler 不再 promote queue → running；正在跑的 runs 不动，跑完为止。
    config_kv 持久化，restart 后保持 paused。需要 admin.manage_scheduler cap
    （is_dev_role 自动 bypass）。"""
    from backend.services.config_service import set_paused
    db = request.app.state.db
    sched = request.app.state.scheduler
    sched.paused = True
    await set_paused(db, True)
    queued = await dao.queue_count(db)
    live = await dao.runs_count_live(db)
    return _SchedulerStateResponse(paused=True, queued=queued, live=live)


@router.post(":resume", response_model=_SchedulerStateResponse)
async def resume_scheduler(
    request: Request,
    _user: User = Depends(require_capability(ADMIN_MANAGE_SCHEDULER)),
) -> _SchedulerStateResponse:
    """恢复 promote loop。下次 tick (≤1s) 内开始往容量空位灌 queue。
    需要 admin.manage_scheduler cap（is_dev_role 自动 bypass）。"""
    from backend.services.config_service import set_paused
    db = request.app.state.db
    sched = request.app.state.scheduler
    sched.paused = False
    await set_paused(db, False)
    queued = await dao.queue_count(db)
    live = await dao.runs_count_live(db)
    return _SchedulerStateResponse(paused=False, queued=queued, live=live)


@router.post(":delete_batch", response_model=_DeleteBatchResponse)
async def delete_batch(req: _DeleteBatchRequest, request: Request) -> _DeleteBatchResponse:
    """批量删 runs：active(queued/running/evaluating) 先 cancel，再 DELETE row + rmtree run_dir。

    安全守护：rmtree 仅当 run_dir 物理位于 settings.demo_runs_dir 之下才执行。
    任何指向 fresh repo 之外（譬如 bench_questions_dir / bench_runs_dir / 用户家目录其他位置）
    的路径都不会被删——row 仍清掉，但磁盘文件保留并在 failed 列表里报告。

    单条失败不阻塞其他；返回 deleted/failed 两份清单。
    """
    import shutil
    s = request.app.state.settings
    db = request.app.state.db
    registry = request.app.state.run_registry
    user: User = request.state.user
    # 解析一次：rmtree 边界 = demo_runs_dir 真实路径（去 symlink、去 ..）
    demo_root = Path(s.demo_runs_dir).resolve()
    deleted: list[str] = []
    failed: list[dict] = []
    for run_id in req.run_ids:
        row = await dao.run_get(db, run_id)
        if row is None:
            failed.append({"run_id": run_id, "error_msg": "not found"})
            continue
        if not _can_delete_run(user, row):
            failed.append({"run_id": run_id, "error_msg": "belongs to another user"})
            continue
        try:
            if row["status"] in ("queued", "running", "evaluating"):
                try:
                    await do_cancel(db=db, registry=registry, run_id=run_id, reason="bulk delete", settings=s)
                except (RunNotFound, RunInTerminalState):
                    pass
            run_dir = row.get("run_dir")
            await dao.run_delete(db, run_id)
            if run_dir:
                rd = Path(run_dir)
                # 安全：必须严格在 demo_runs_dir 之下（且不是 demo_root 本身）才允许 rmtree。
                # is_relative_to 在 Python 3.9+；为兼容用 try/relative_to。
                inside_demo = False
                try:
                    rel = rd.resolve().relative_to(demo_root)
                    # rel == "." 意味着 rd == demo_root → 不删根
                    inside_demo = str(rel) not in ("", ".")
                except ValueError:
                    inside_demo = False
                if not inside_demo:
                    failed.append({
                        "run_id": run_id,
                        "error_msg": (
                            f"run_dir outside demo_runs_dir → row deleted, files kept: "
                            f"{run_dir}"
                        ),
                    })
                    continue
                if rd.exists() and rd.is_dir():
                    try:
                        shutil.rmtree(rd)
                    except OSError as e:
                        # row 已删；run_dir 残留只是磁盘垃圾，不阻塞业务
                        failed.append({
                            "run_id": run_id,
                            "error_msg": f"row deleted but run_dir rmtree failed: {e}",
                        })
                        continue
            deleted.append(run_id)
        except Exception as e:
            failed.append({"run_id": run_id, "error_msg": str(e)})
    return _DeleteBatchResponse(deleted=deleted, failed=failed)


# ============ curve compare (T2 dos_curve viz) ============

# Public per USER 2026-05-06 decision: any role can fetch GT curve data — score
# already exposes relative diff, viz is a step further but no new fundamental leak.

@router.get("/{run_id}/curve_field/{field_name}")
async def get_curve_field(run_id: str, field_name: str, request: Request) -> dict:
    """Return curve compare payload (pred + gt arrays + score + summary) for
    a curve_f1-typed field of a bench run. T2 dos_curve is the current case.

    400 if field is not curve_f1; 404 if run / field / ref_file missing.
    """
    s = request.app.state.settings
    db = request.app.state.db
    row = await dao.run_get(db, run_id)
    if not row:
        raise HTTPException(status_code=404, detail=f"run '{run_id}' not found")
    if row.get("mode") != "bench":
        raise HTTPException(status_code=400, detail="curve_field only supported for bench runs")
    run_dir = Path(row["run_dir"])
    report_path = run_dir / "report.json"
    if not report_path.exists():
        raise HTTPException(status_code=404, detail=f"report.json not found for '{run_id}'")
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise HTTPException(status_code=500, detail=f"report.json read failed: {e}")
    extracted = report.get("extracted_fields") or {}
    if field_name not in extracted:
        raise HTTPException(
            status_code=404,
            detail=f"field '{field_name}' not in extracted_fields"
        )
    pred = extracted[field_name]

    # ground_truth comes from question JSON (report.json doesn't carry full GT)
    task_id = row.get("task_id")
    if not task_id:
        raise HTTPException(status_code=400, detail="run has no task_id (custom run?)")
    parts = task_id.split("_")
    if len(parts) < 3:
        raise HTTPException(status_code=400, detail=f"unrecognized task_id format: {task_id}")
    subdir = f"{parts[0]}_{parts[1]}"
    task_path = s.bench_questions_dir / subdir / f"{task_id}.json"
    if not task_path.exists():
        raise HTTPException(status_code=404, detail=f"task JSON not found: {task_path.name}")
    try:
        task_data = json.loads(task_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise HTTPException(status_code=500, detail=f"task JSON read failed: {e}")
    gt = (task_data.get("ground_truth") or {}).get(field_name)
    if not isinstance(gt, dict):
        raise HTTPException(status_code=404, detail=f"ground_truth['{field_name}'] missing")
    rule = gt.get("rule")
    if rule != "curve_f1":
        raise HTTPException(
            status_code=400,
            detail=f"field '{field_name}' is not curve_f1 (rule={rule})"
        )
    tolerance = gt.get("tolerance", 0.1)
    gt_value = gt.get("value")

    # 拿 GT 的 dos_e_fermi 作为 gt_reference_ev（VBM for insulators, EF for metals）
    # 用于把 canonical raw dos.dat (绝对能量) 转成相对 → 前端可视化扩展范围
    gt_ref_ev = None
    if field_name == "dos_curve":
        gt_ef_entry = (task_data.get("ground_truth") or {}).get("dos_e_fermi") or {}
        if isinstance(gt_ef_entry.get("value"), (int, float)):
            gt_ref_ev = float(gt_ef_entry["value"])

    try:
        payload = compute_curve_compare(
            pred, gt_value, tolerance, s.bench_dir,
            task_id=task_id, gt_reference_ev=gt_ref_ev,
        )
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))

    return {
        "run_id": run_id,
        "field_name": field_name,
        "rule": rule,
        "unit": gt.get("unit"),
        "tolerance": tolerance,
        **payload,
    }


# ============ case bundle (workdir + GT + report tar.gz download) ============

# Public per same policy as curve_field — score / GT data already exposed via
# other endpoints; bundle is convenience wrapper for offline review.

@router.get("/{run_id}/case_bundle")
async def get_case_bundle(run_id: str, request: Request) -> Response:
    """Return tar.gz of run's key files for sharing / review.

    Bundles workdir (in/out/dat/log/json/txt files <= 5 MB; excludes tmp_*/
    wfc*/charge_density*/pseudos), GT data (bench mode), report.json, and an
    auto-generated README.md with scoring summary.
    """
    s = request.app.state.settings
    db = request.app.state.db
    row = await dao.run_get(db, run_id)
    if not row:
        raise HTTPException(status_code=404, detail=f"run '{run_id}' not found")
    run_dir = Path(row["run_dir"])
    if not run_dir.exists():
        raise HTTPException(status_code=404, detail="run_dir not found on disk")

    bundle_bytes = build_bundle(
        run_id=run_id,
        run_dir=run_dir,
        task_id=row.get("task_id"),
        bench_questions_dir=s.bench_questions_dir,
    )

    return Response(
        content=bundle_bytes,
        media_type="application/gzip",
        headers={
            "Content-Disposition": f'attachment; filename="{run_id}.tar.gz"',
            "Content-Length": str(len(bundle_bytes)),
        },
    )


# ============ QE progress hint (transcript live ETA, 2026-05-08) ============
#
# 实时探测 QE 计算进度：扫 run_dir/workdir/ 下 .out 文件 → 提 SCF iter / per-iter
# 时间 → ETA。前端 transcript 渲染 in-flight bash turn 时下挂灰色小字
# "⏳ QE pw.x [scf] · iter 12/50 · elapsed 3:42 · ETA ~5:30"。
#
# 设计要点（user 0508 决策）：web-fresh 自包含——不依赖 v1/v2/strong solver 的
# qe_tools.py，唯一信号源是 QE 自己写到磁盘的 .out 文件，所以对所有 solver 都生效。
#
# Cap gate：复用 list_runs/get_run 的 _user_can_see_run 三维 filter（task / agent /
# owner）；缺哪一维都按 404 不暴露存在性。status != "running" 时直接返
# in_flight=false 不读 fs，便于前端无脑 polling。
@router.get("/{run_id}/qe_progress")
async def get_qe_progress_endpoint(run_id: str, request: Request) -> dict:
    s = request.app.state.settings
    db = request.app.state.db
    user: User = request.state.user
    row = await dao.run_get(db, run_id)
    if not row:
        raise HTTPException(status_code=404, detail=f"run '{run_id}' not found")
    stable_ids = await _stable_task_ids(s, db)
    unstable_agents = _unstable_agent_ids(s)
    if not _user_can_see_run(row, user, stable_ids, unstable_agents):
        raise HTTPException(status_code=404, detail=f"run '{run_id}' not found")
    # done / failed / cancelled / queued / evaluating 都直接返 in_flight=false：
    # 不读 fs，前端 polling 可以无脑挂着，不增加磁盘 I/O 负担
    if row["status"] != "running":
        return {"in_flight": False, "reason": f"run_status_{row['status']}"}
    run_dir_str = row.get("run_dir")
    if not run_dir_str:
        return {"in_flight": False, "reason": "no_run_dir"}
    return get_qe_progress(Path(run_dir_str))


# ============ F2 cost preview (pre-launch USD estimate) ============
#
# 两个 endpoint：单题 + 批量；都仅要求登录，不做 capability gate。
# 算法见 backend/services/cost.py:estimate_cost——历史均值（≥3 done runs）or per-model
# 启发式 token × max_turns × 价格。前端 compose / batch dialog 在用户调 launch 前预估
# 让 user 知道大概 burn 多少美元。
class CostPreviewRequest(BaseModel):
    task_id: str = Field(max_length=128)
    agent_id: str = Field(max_length=128)
    model_id: str = Field(max_length=128)
    max_turns: int = Field(ge=1, le=200)


class CostPreviewResponse(BaseModel):
    est_usd: float
    source: str           # "history" | "heuristic"
    n_samples: int        # history 时 ≥3；heuristic 时 0


class CostPreviewBatchRequest(BaseModel):
    # 上限与 BatchSubmitRequest specs (250) 同；批量 preview 同样上限
    task_ids: list[str] = Field(min_length=1, max_length=250)
    agent_id: str = Field(max_length=128)
    model_id: str = Field(max_length=128)
    max_turns: int = Field(ge=1, le=200)


class _BatchCostPerTask(BaseModel):
    task_id: str
    est_usd: float
    source: str
    n_samples: int


class CostPreviewBatchResponse(BaseModel):
    per_task: list[_BatchCostPerTask]
    total_est_usd: float
    n_with_history: int
    n_heuristic: int


@router.post(":preview-cost", response_model=CostPreviewResponse)
async def preview_cost(req: CostPreviewRequest, request: Request) -> CostPreviewResponse:
    """单题 cost 预估。任意已登录 user 可调；中间件已挡未授权，不再加 capability gate。"""
    s = request.app.state.settings
    db = request.app.state.db
    out = await estimate_cost(
        s, db,
        task_id=req.task_id,
        agent_id=req.agent_id,
        model_id=req.model_id,
        max_turns=req.max_turns,
    )
    return CostPreviewResponse(**out)


@router.post(":preview-cost-batch", response_model=CostPreviewBatchResponse)
async def preview_cost_batch(
    req: CostPreviewBatchRequest, request: Request,
) -> CostPreviewBatchResponse:
    """批量 cost 预估。串行调 estimate_cost（每条 1 SQL fetchall），250 条 ~ms 量级。"""
    s = request.app.state.settings
    db = request.app.state.db
    per_task: list[_BatchCostPerTask] = []
    total = 0.0
    n_hist = 0
    n_heur = 0
    for tid in req.task_ids:
        out = await estimate_cost(
            s, db,
            task_id=tid,
            agent_id=req.agent_id,
            model_id=req.model_id,
            max_turns=req.max_turns,
        )
        per_task.append(_BatchCostPerTask(task_id=tid, **out))
        total += float(out["est_usd"])
        if out["source"] == "history":
            n_hist += 1
        else:
            n_heur += 1
    return CostPreviewBatchResponse(
        per_task=per_task,
        total_est_usd=total,
        n_with_history=n_hist,
        n_heuristic=n_heur,
    )
