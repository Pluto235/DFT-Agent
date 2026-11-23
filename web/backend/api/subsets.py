"""GET /api/subsets — 返代表性子集预设清单（fast / standard / full）。

数据源：bench repo 的 tools/subset_presets.yaml。
前端用：
- Catalogue 的「快速档 / 标准档 / 完整档」快捷选按钮（取 task_ids 数组）
- Leaderboard 的「子集视图切换」按钮组（按 key 过滤 endpoint）
"""
from __future__ import annotations

from fastapi import APIRouter, Request

from backend.api.leaderboard import _bench_task_ids
from backend.auth.capabilities import VIEW_DEV_TASKS
from backend.auth.tokens import User
from backend.services.subset_loader import load_subsets


router = APIRouter(prefix="/api", tags=["subsets"])


@router.get("/subsets")
async def list_subsets(request: Request) -> dict:
    """返回三档子集 + 每档 task_id 数组 + metadata。

    full 档 task_ids 字段动态展开为当前 is_stable=true 全集（仅当前 user 可见）。
    capability-aware：缺 view.dev_tasks 的 user 看到的 full 数 = 142（不含 unstable）；
    有 cap 看到 = 全集（含 dev-only 题）。

    返回 shape：
      {
        "version": "1.1",
        "date": "2026-05-11",
        "subsets": [
          {key: "fast", name: "快速档", name_en: "Fast",
           description: "...", task_ids: [...], count: 12},
          {key: "standard", ..., count: 27},
          {key: "full", ..., task_ids: [...], count: 142}
        ]
      }
    """
    s = request.app.state.settings
    user: User = request.state.user
    can_see_dev_tasks = user.has_capability(VIEW_DEV_TASKS)

    data = load_subsets(s.bench_dir)
    subsets_raw = data.get("subsets") or {}

    # full 档动态展开
    all_stable_ids = _bench_task_ids(s, include_unstable=can_see_dev_tasks)

    out_subsets = []
    for key, meta in subsets_raw.items():
        tids = meta.get("task_ids")
        if tids is None:
            tids = list(all_stable_ids)
        tids = [tid for tid in tids if tid in all_stable_ids]
        out_subsets.append({
            "key": key,
            "name": meta.get("name", key),
            "name_en": meta.get("name_en", key),
            "description": meta.get("description", ""),
            "task_ids": tids,
            "count": len(tids),
            "expected_count": meta.get("expected_count"),
        })

    return {
        "version": data.get("version"),
        "date": data.get("date"),
        "spec_doc": data.get("spec_doc"),
        "subsets": out_subsets,
    }
