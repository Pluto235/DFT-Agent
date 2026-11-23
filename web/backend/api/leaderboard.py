"""Public leaderboard endpoint (`GET /api/leaderboard`)。

聚合 runs 表，按 (agent_id, model) 分组得出每组在选定 task scope 上的 metric。
- 公榜：不按 owner 默认过滤，登录后任何 role 可见（auth_middleware 仍要求 token，
  匿名访问被 401 拦在前面）。
- 'avg' 策略：所有合规 run 直接平均。
- 'best' 策略：每个 task 选 (agent, model) 在其上的最佳 run（按 score；
  failed 视为 0；done 同分胜失败）→ 然后对这些 best run 平均。
- score 视 done.total_score 为分；failed → 0；cancelled / queued / running 一律排除（leaderboard 只看终态）。
- wall / qe / turn / cost：仅 status='done' 的 run 入算（fail 早 = 消耗少，
  会污染均值——已在设计 doc 敲定）。
- task_scope：list[str] 形如 ["M1_T1","M2_T2"]，省略或空 list = 不过滤。custom 模式
  无 task_id 一律不参与（leaderboard 只比 bench）。
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, Request

from backend.auth.capabilities import VIEW_DEV_AGENTS, VIEW_DEV_TASKS
from backend.auth.tokens import User


router = APIRouter(prefix="/api", tags=["leaderboard"])

_TASK_PREFIX_RE = re.compile(r"^(M\d+)_(T\d+)_")


def _task_scope_key(task_id: Optional[str]) -> Optional[str]:
    """`M1_T1_Si_vc_relax` → `M1_T1`；不匹配返回 None（custom / 异常 task_id）。"""
    parts = _parse_milestone_task(task_id)
    return f"{parts[0]}_{parts[1]}" if parts else None


def _parse_milestone_task(task_id: Optional[str]) -> Optional[tuple[str, str]]:
    """`M1_T1_Si_vc_relax` → ('M1','T1')；不匹配返回 None。"""
    if not task_id:
        return None
    m = _TASK_PREFIX_RE.match(task_id)
    return (m.group(1), m.group(2)) if m else None


def _score_value(row: dict) -> Optional[float]:
    """row 对 score 的贡献：done → total_score（None 当 0）；failed → 0；其他（含 cancelled）→ None 不参与。"""
    st = row.get("status")
    if st == "done":
        s = row.get("total_score")
        return float(s) if s is not None else 0.0
    if st == "failed":
        return 0.0
    return None


def _pick_best_run(runs: list[dict]) -> Optional[dict]:
    """单 task 内选 score 最高的 run；同分时 done > failed；再同时取 created_at 较新者。

    runs 必须已 cancelled 排除。score-eligible 检查在内：全 None（譬如全 cancelled 漏过来）→ None。
    """
    best = None
    best_s = None
    for r in runs:
        s = _score_value(r)
        if s is None:
            continue
        if best is None or s > best_s:
            best, best_s = r, s
            continue
        if s < best_s:
            continue
        # 同分：done 胜 failed
        if best["status"] != "done" and r["status"] == "done":
            best = r; continue
        if best["status"] == "done" and r["status"] != "done":
            continue
        # 同 status：created_at 较新胜（ISO 字符串字典序可直接比较）
        if (r.get("created_at") or "") > (best.get("created_at") or ""):
            best = r
    return best


def _bench_task_ids(settings, *, include_unstable: bool = True) -> list[str]:
    """所有 bench 题目的 task_id（用于 leaderboard 端 / scope 总数计算）。

    优先用 task_loader 的内存 cache（已被 list_tasks 等热启动）；冷启动 fallback
    到 glob bench_questions_dir/*/*.json，避免触发 28s 的 raw load 阻塞 endpoint。

    capability filter：include_unstable=False 时过滤 metadata.is_stable=false 的题
    （与 task list / runs filter 一致）。caller 根据 user.has_capability(VIEW_DEV_TASKS)
    决定是否传 True。冷启动 fallback 路径不读 JSON 拿 is_stable（成本高），
    此时直返全集——leaderboard 极少在冷启动状态下被调用，task_loader cache
    会被任何 list_tasks 调用预热。
    """
    try:
        from backend.services import task_loader as tl
        if tl._CACHE_RAW:
            tids: list[str] = []
            for t in tl._CACHE_RAW:
                tid = t.get("task_id")
                if not tid:
                    continue
                if not include_unstable and not t.get("is_stable", True):
                    continue
                tids.append(tid)
            return tids
    except Exception:
        pass
    qdir = getattr(settings, "bench_questions_dir", None)
    if not qdir:
        return []
    p = Path(qdir)
    if not p.exists():
        return []
    return [f.stem for f in p.glob("*/*.json") if f.is_file()]


def _count_tasks_in_scope(settings, milestone: Optional[str],
                          task_type: Optional[str], *,
                          subset_task_ids: Optional[set[str]] = None,
                          include_unstable: bool = True) -> int:
    """scope 内总 task 数（与 milestone / task_type / subset AND 过滤一致）。

    capability filter：include_unstable=False 时不计 unstable 题（缺
    view.dev_tasks 的 user 看到的分母 = 142 而非 172）。
    subset filter：仅计算 task_id 属于 subset_task_ids 的题（None = 不限）。
    """
    n = 0
    for tid in _bench_task_ids(settings, include_unstable=include_unstable):
        parts = _parse_milestone_task(tid)
        if not parts:
            continue
        m, t = parts
        if milestone and m != milestone:
            continue
        if task_type and t != task_type:
            continue
        if subset_task_ids is not None and tid not in subset_task_ids:
            continue
        n += 1
    return n


def _empty_metrics() -> dict:
    return {
        "score": {"avg": None, "n_done": 0, "n_failed_zero": 0, "n_total": 0, "n_tasks": 0},
        "wall":  {"avg": None, "n": 0, "n_tasks": 0},
        "qe":    {"avg": None, "n": 0, "n_tasks": 0},
        "turn":  {"avg": None, "n": 0, "n_tasks": 0},
        "cost":  {"avg": None, "total": None, "n": 0, "n_tasks": 0},
        "task_stats": {"attempted": 0, "succeeded": 0, "failed_only": 0},
    }


def aggregate_metrics(rows: list[dict], aggregation: str) -> dict:
    """两步聚合：每 task 计算代表值 → 跨 task 平均。这样多次跑同一题不会让该题主导均值。

    aggregation='avg': task 代表 = 该 task 内合规 run 的均值；wall/qe/turn/cost 仅 done 进 task 均值
    aggregation='best': task 代表 = 该 task 内 score 最高的那条 run；wall/qe/turn/cost 必须该 winner 是 done 才贡献

    score-eligible = done + failed (failed 计 0)；cancelled 上游已剔除。
    cost.total 跨所有 done run 累加（两种 mode 一致——它表达"花了多少 USD"，与平均值语义不同）。
    """
    if not rows:
        return _empty_metrics()

    by_task: dict[str, list[dict]] = {}
    for r in rows:
        tid = r.get("task_id")
        if tid:
            by_task.setdefault(tid, []).append(r)
    if not by_task:
        return _empty_metrics()

    # 每 task 维度的成功 / 失败 计数（与 metric 无关；用于 cell 头显示 attempted/total + ✓/✗）
    n_tasks_succeeded = sum(
        1 for runs in by_task.values()
        if any(r["status"] == "done" for r in runs)
    )
    n_tasks_attempted = len(by_task)
    n_tasks_failed_only = n_tasks_attempted - n_tasks_succeeded

    task_scores: list[float] = []
    task_walls: list[float] = []
    task_qes: list[float] = []
    task_turns: list[float] = []
    task_costs: list[float] = []

    n_done_total = 0
    n_failed_zero_total = 0
    n_score_total = 0
    cost_total_global = 0.0
    has_cost = False

    for _tid, runs in by_task.items():
        sv_pairs = [(r, _score_value(r)) for r in runs]
        sv_pairs = [(r, s) for r, s in sv_pairs if s is not None]
        if not sv_pairs:
            continue

        # 全局计数（score 池 = done + failed）
        n_score_total += len(sv_pairs)
        n_done_total += sum(1 for r, _ in sv_pairs if r["status"] == "done")
        n_failed_zero_total += sum(1 for r, _ in sv_pairs if r["status"] == "failed")

        # cost.total 跨 task / 跨 mode 累加 done run 实际花费
        for r, _ in sv_pairs:
            if r["status"] == "done" and r.get("cost_usd") is not None:
                cost_total_global += r["cost_usd"]
                has_cost = True

        if aggregation == "best":
            best_run = _pick_best_run(runs)
            if best_run is None:
                continue
            task_scores.append(_score_value(best_run))
            if best_run["status"] == "done":
                if best_run.get("wall_time_sec") is not None:
                    task_walls.append(best_run["wall_time_sec"])
                if best_run.get("qe_call_count") is not None:
                    task_qes.append(best_run["qe_call_count"])
                if best_run.get("current_turn") is not None:
                    task_turns.append(best_run["current_turn"])
                if best_run.get("cost_usd") is not None:
                    task_costs.append(best_run["cost_usd"])
        else:  # 'avg'
            task_scores.append(sum(s for _, s in sv_pairs) / len(sv_pairs))
            done_runs = [r for r, _ in sv_pairs if r["status"] == "done"]

            def _task_mean(field: str) -> Optional[float]:
                xs = [r[field] for r in done_runs if r.get(field) is not None]
                return sum(xs) / len(xs) if xs else None

            for tlst, fld in [(task_walls, "wall_time_sec"), (task_qes, "qe_call_count"),
                              (task_turns, "current_turn"), (task_costs, "cost_usd")]:
                v = _task_mean(fld)
                if v is not None:
                    tlst.append(v)

    def _mean(xs: list[float]) -> Optional[float]:
        return sum(xs) / len(xs) if xs else None

    return {
        "score": {
            "avg": _mean(task_scores),
            "n_done": n_done_total,
            "n_failed_zero": n_failed_zero_total,
            "n_total": n_score_total,
            "n_tasks": len(task_scores),
        },
        "wall":  {"avg": _mean(task_walls), "n": len(task_walls), "n_tasks": len(task_walls)},
        "qe":    {"avg": _mean(task_qes),   "n": len(task_qes),   "n_tasks": len(task_qes)},
        "turn":  {"avg": _mean(task_turns), "n": len(task_turns), "n_tasks": len(task_turns)},
        "cost":  {"avg": _mean(task_costs), "total": cost_total_global if has_cost else None,
                  "n": len(task_costs), "n_tasks": len(task_costs)},
        "task_stats": {
            "attempted":    n_tasks_attempted,
            "succeeded":    n_tasks_succeeded,
            "failed_only":  n_tasks_failed_only,
        },
    }


def _resolve_agent_name(agent_id: Optional[str], settings) -> str:
    """从 settings.agents 查显示名；老 row agent_id 为 None / 不在列表里都回退到 id 字符串。"""
    if not agent_id:
        return "(unset)"
    for a in getattr(settings, "agents", []):
        if a.id == agent_id:
            return a.name
    return agent_id  # 注册表里找不到时直接用 id 显示，不静默抹除


@router.get("/leaderboard")
async def leaderboard(
    request: Request,
    date_from: Optional[str] = Query(None, description="ISO Z timestamp; created_at >= date_from"),
    date_to: Optional[str] = Query(None, description="ISO Z timestamp; created_at < date_to"),
    owner: Optional[str] = Query(None, description="filter by owner（默认 all = 公榜）"),
    agent_id: Optional[str] = Query(None, description="filter to specific agent_id"),
    milestone: Optional[str] = Query(None, description="e.g. 'M1' / 'M2'；省略 = 不过滤材料分组"),
    task_type: Optional[str] = Query(None, description="e.g. 'T1' / 'T2'；省略 = 不过滤任务类型"),
    subset: Optional[str] = Query(None, description="代表性子集键：'fast' / 'standard' / 'full' / 不传 = 不按子集过滤（全集）"),
    aggregation: str = Query("best", description="avg | best (default best：每 task 选最高分代表，避免一题多跑同时拉低/抬高均值)"),
) -> dict:
    if aggregation not in ("avg", "best"):
        raise HTTPException(status_code=400, detail=f"aggregation must be avg|best, got {aggregation!r}")

    db = request.app.state.db
    settings = request.app.state.settings
    user: User = request.state.user
    can_see_dev_tasks = user.has_capability(VIEW_DEV_TASKS)
    can_see_dev_agents = user.has_capability(VIEW_DEV_AGENTS)

    # 终态 run only：mode='bench' + task_id NOT NULL + status IN (done, failed)。
    # queued / running 不进 leaderboard——之前只排除 cancelled，导致 queued task 被算成
    # attempted 但既不 done 也不 failed，task_stats.failed_only = attempted - succeeded
    # 把 queued task 算成 failed（4-7 列 91 false-fail bug）。
    where: list[str] = ["mode = 'bench'", "task_id IS NOT NULL", "status IN ('done', 'failed')"]
    params: list = []
    if date_from:
        where.append("created_at >= ?"); params.append(date_from)
    if date_to:
        where.append("created_at < ?"); params.append(date_to)
    if owner:
        where.append("owner = ?"); params.append(owner)
    if agent_id:
        where.append("agent_id = ?"); params.append(agent_id)

    sql = f"SELECT * FROM runs WHERE {' AND '.join(where)}"
    rows = await db.fetchall(sql, tuple(params))
    rows = [dict(r) for r in rows]

    # capability filter（两层，互不依赖）：
    #   - task 维度（缺 view.dev_tasks）：看不到 unstable task 的 run（task.is_stable=False）
    #   - agent 维度（缺 view.dev_agents）：看不到 dev-only agent 的 run（agent.is_stable=False）
    # 两 cap 拆开是因为现实可能某 user 只该看 dev agent（评估 v2）但不该看 unstable
    # task（v2 还未对齐题库），或反之。
    if not (can_see_dev_tasks and can_see_dev_agents):
        if not can_see_dev_tasks:
            stable_ids = set(_bench_task_ids(settings, include_unstable=False))
            rows = [r for r in rows if (r.get("task_id") or "") in stable_ids]
        if not can_see_dev_agents:
            unstable_agents = {a.id for a in getattr(settings, "agents", []) if not getattr(a, "is_stable", True)}
            rows = [r for r in rows if (r.get("agent_id") or "") not in unstable_agents]

    # subset 过滤（fast / standard / full / None）：通过 task_id ∈ subset_task_ids 实现 AND 组合
    subset_task_ids: Optional[set[str]] = None
    if subset:
        from backend.services.subset_loader import get_subset_task_ids
        all_stable = _bench_task_ids(settings, include_unstable=can_see_dev_tasks)
        subset_task_ids = get_subset_task_ids(settings.bench_dir, subset, all_stable_ids=all_stable)

    # milestone / task_type / subset 过滤（Python 侧 + AND 语义；可选）
    milestones_seen: set[str] = set()
    task_types_seen: set[str] = set()
    filtered: list[dict] = []
    for r in rows:
        parts = _parse_milestone_task(r.get("task_id"))
        if parts is None:
            continue
        m, t = parts
        milestones_seen.add(m); task_types_seen.add(t)
        if milestone and m != milestone:
            continue
        if task_type and t != task_type:
            continue
        if subset_task_ids is not None and (r.get("task_id") or "") not in subset_task_ids:
            continue
        filtered.append(r)

    # 按 (agent_id, model) 分桶；老 row agent_id=None 时落到默认 agent
    default_agent_id = settings.agents[0].id if getattr(settings, "agents", []) else None
    buckets: dict[tuple[str, str], list[dict]] = {}
    for r in filtered:
        aid = r.get("agent_id") or default_agent_id
        model = r.get("model")
        if not aid or not model:
            continue
        buckets.setdefault((aid, model), []).append(r)

    out_rows = []
    for (aid, model), bucket in sorted(buckets.items()):
        out_rows.append({
            "agent_id": aid,
            "agent_name": _resolve_agent_name(aid, settings),
            "model": model,
            "metrics": aggregate_metrics(bucket, aggregation),
        })

    # 排序方便前端默认展示：按 score.avg 降序（None 沉底）
    def _sort_key(r):
        s = r["metrics"]["score"]["avg"]
        return (-(s if s is not None else float("-inf")), r["agent_id"], r["model"])
    out_rows.sort(key=_sort_key)

    return {
        "rows": out_rows,
        "milestones_available": sorted(milestones_seen),
        "task_types_available": sorted(task_types_seen),
        # scope 总 task 数：每 cell 的"attempted/total"分母（从 bench questions 目录数）
        # capability-aware：缺 view.dev_tasks 看到 166（不计 6 个 unstable issue cases），有 cap 看 172
        # subset 过滤后分母也按 subset 收缩（标准档 27 / 快速档 12 等）
        "n_tasks_in_scope": _count_tasks_in_scope(
            settings, milestone, task_type, subset_task_ids=subset_task_ids,
            include_unstable=can_see_dev_tasks,
        ),
        "filters_applied": {
            "date_from": date_from,
            "date_to": date_to,
            "owner": owner,
            "agent_id": agent_id,
            "milestone": milestone,
            "task_type": task_type,
            "subset": subset,
            "aggregation": aggregation,
        },
    }


# ============================================================================
# Drill-down：单 (agent, model) cell 下的 run 列表
# ============================================================================

@router.get("/leaderboard/runs")
async def leaderboard_runs(
    request: Request,
    agent_id: str = Query(..., description="must match (NULL agent_id falls back to default agent)"),
    model: str = Query(...),
    date_from: Optional[str] = Query(None),
    date_to: Optional[str] = Query(None),
    owner: Optional[str] = Query(None),
    milestone: Optional[str] = Query(None, description="e.g. 'M1'；省略 = 不限"),
    task_type: Optional[str] = Query(None, description="e.g. 'T1'；省略 = 不限"),
    subset: Optional[str] = Query(None, description="代表性子集键：'fast' / 'standard' / 'full' / 不传 = 不限"),
    limit: int = Query(default=500, ge=1, le=2000),
) -> dict:
    """返回某 (agent_id, model) cell 下所有合规 run（mode=bench / 非 cancelled / task_id 命中 milestone+task_type AND）。

    与 /api/leaderboard 的 cell 聚合范围一致——展示用户点击的某 cell 实际包含哪些 run。
    aggregation（avg/best）不影响列表内容；UI 端可按 score 排序。
    """
    db = request.app.state.db
    settings = request.app.state.settings
    user: User = request.state.user
    can_see_dev_tasks = user.has_capability(VIEW_DEV_TASKS)
    can_see_dev_agents = user.has_capability(VIEW_DEV_AGENTS)
    default_agent_id = settings.agents[0].id if getattr(settings, "agents", []) else None

    # 终态 run only — 与 /api/leaderboard 计 cell 时的 SQL 一致
    where: list[str] = ["mode = 'bench'", "task_id IS NOT NULL", "status IN ('done', 'failed')"]
    params: list = []
    if date_from:
        where.append("created_at >= ?"); params.append(date_from)
    if date_to:
        where.append("created_at < ?"); params.append(date_to)
    if owner:
        where.append("owner = ?"); params.append(owner)
    where.append("model = ?"); params.append(model)
    # agent_id：当 cell 的 aid == default 时也要包括 NULL 老 row（leaderboard 端做了同样回退）
    if default_agent_id is not None and agent_id == default_agent_id:
        where.append("(agent_id = ? OR agent_id IS NULL)")
        params.append(agent_id)
    else:
        where.append("agent_id = ?"); params.append(agent_id)

    sql = (
        f"SELECT * FROM runs WHERE {' AND '.join(where)} "
        f"ORDER BY created_at DESC LIMIT ?"
    )
    params.append(limit)
    rows = await db.fetchall(sql, tuple(params))

    # capability filter（两层）：缺 view.dev_tasks/dev_agents 的 user 不能在 drill-down
    # 看到 unstable task / dev-only agent 的 run。两 cap 互不依赖。
    stable_ids: Optional[set[str]] = None
    unstable_agents: Optional[set[str]] = None
    if not can_see_dev_tasks:
        stable_ids = set(_bench_task_ids(settings, include_unstable=False))
    if not can_see_dev_agents:
        unstable_agents = {a.id for a in getattr(settings, "agents", []) if not getattr(a, "is_stable", True)}

    # subset filter：与 leaderboard /api/leaderboard 同源
    subset_task_ids: Optional[set[str]] = None
    if subset:
        from backend.services.subset_loader import get_subset_task_ids
        all_stable = _bench_task_ids(settings, include_unstable=can_see_dev_tasks)
        subset_task_ids = get_subset_task_ids(settings.bench_dir, subset, all_stable_ids=all_stable)

    out = []
    for r in rows:
        rd = dict(r)
        parts = _parse_milestone_task(rd.get("task_id"))
        if parts is None:
            continue
        m, t = parts
        if milestone and m != milestone:
            continue
        if task_type and t != task_type:
            continue
        if stable_ids is not None and (rd.get("task_id") or "") not in stable_ids:
            continue
        if unstable_agents and (rd.get("agent_id") or "") in unstable_agents:
            continue
        if subset_task_ids is not None and (rd.get("task_id") or "") not in subset_task_ids:
            continue
        out.append({
            "run_id": rd["run_id"],
            "task_id": rd["task_id"],
            "milestone": m,
            "task_type": t,
            "owner": rd.get("owner"),
            "status": rd["status"],
            "total_score": rd.get("total_score"),
            "wall_time_sec": rd.get("wall_time_sec"),
            "qe_call_count": rd.get("qe_call_count"),
            "qe_wall_sec": rd.get("qe_wall_sec"),
            "current_turn": rd.get("current_turn"),
            "cost_usd": rd.get("cost_usd"),
            "created_at": rd.get("created_at"),
            "finished_at": rd.get("finished_at"),
        })

    return {
        "runs": out,
        "filters_applied": {
            "agent_id": agent_id,
            "model": model,
            "date_from": date_from,
            "date_to": date_to,
            "owner": owner,
            "milestone": milestone,
            "task_type": task_type,
            "subset": subset,
        },
    }
