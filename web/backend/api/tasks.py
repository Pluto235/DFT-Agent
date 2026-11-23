from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, HTTPException, Query, Request

from backend.auth.capabilities import VIEW_DEV_TASKS
from backend.auth.tokens import User
from backend.schemas.task import AgentViewResponse, TaskListItem
from backend.services.task_loader import get_task, load_all_tasks


router = APIRouter(prefix="/api/tasks", tags=["tasks"])


def _user_can_see_task(task: TaskListItem, user: User) -> bool:
    """capability-based bench routing：缺 view.dev_tasks 的 user 只看 stable；
    有该 cap（含 is_dev_role bypass）→ 看全部。"""
    if user.has_capability(VIEW_DEV_TASKS):
        return True
    return task.is_stable


@router.get("", response_model=list[TaskListItem])
async def list_tasks(
    request: Request,
    milestone: Optional[str] = Query(default=None),
    task_type: Optional[str] = Query(default=None),
    task_subtype: Optional[str] = Query(default=None),
    formula_q: Optional[str] = Query(default=None),
    sort: str = Query(default="recent"),
    limit: int = Query(default=200, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
) -> list[TaskListItem]:
    s = request.app.state.settings
    db = request.app.state.db
    items = await load_all_tasks(s, db)
    user: User = request.state.user

    # 客户端 / 服务端都允许过滤（前端通常自己过滤；后端 query 兜底）
    def _match(t: TaskListItem) -> bool:
        if not _user_can_see_task(t, user):
            return False
        if milestone and t.milestone != milestone:
            return False
        if task_type and t.task_type != task_type:
            return False
        if task_subtype and t.task_subtype != task_subtype:
            return False
        if formula_q:
            q = formula_q.lower()
            if q not in (t.material_formula or "").lower() and q not in t.task_id.lower():
                return False
        return True

    filtered = [t for t in items if _match(t)]
    if sort == "recent":
        filtered.sort(key=lambda t: t.last_run_at or "", reverse=True)
    elif sort == "score_desc":
        filtered.sort(key=lambda t: (t.last_score or -1.0), reverse=True)
    elif sort == "score_asc":
        filtered.sort(key=lambda t: (t.last_score if t.last_score is not None else 1e9))
    elif sort == "id":
        filtered.sort(key=lambda t: t.task_id)
    return filtered[offset : offset + limit]


@router.get("/{task_id}", response_model=TaskListItem)
async def get_task_detail(task_id: str, request: Request) -> TaskListItem:
    s = request.app.state.settings
    db = request.app.state.db
    item = await get_task(s, db, task_id)
    if item is None:
        raise HTTPException(status_code=404, detail=f"task '{task_id}' not found")
    if not _user_can_see_task(item, request.state.user):
        # 不暴露存在性：缺 view.dev_tasks 的 user 看 dev-only 题目按 404 返
        raise HTTPException(status_code=404, detail=f"task '{task_id}' not found")
    return item


@router.get("/{task_id}/agent_view", response_model=AgentViewResponse)
async def get_task_agent_view(task_id: str, request: Request) -> AgentViewResponse:
    """task-level：返回 prompt + initial_files（与 docs/03 §3.3 一致）。"""
    s = request.app.state.settings
    db = request.app.state.db
    item = await get_task(s, db, task_id)
    if item is None:
        raise HTTPException(status_code=404, detail=f"task '{task_id}' not found")
    if not _user_can_see_task(item, request.state.user):
        raise HTTPException(status_code=404, detail=f"task '{task_id}' not found")
    return AgentViewResponse(
        task_id=item.task_id,
        prompt=item.rendered_prompt,
        initial_files=item.initial_files,
    )
