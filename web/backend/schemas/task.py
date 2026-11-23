"""Task schemas — TaskListItem (fat shape, prototype data.js MOCK_TASKS 对齐) + AgentViewResponse。

TaskListItem 把 catalogue 渲染所需的 detail 全部塞进来（rendered_prompt / initial_files / extract_fields /
convergence_evidence），让前端 GET /api/tasks 一次拿全；prototype 的 Preview 直接读 task.rendered_prompt，
不再调 /api/tasks/{id} 与 /agent_view（保留 endpoint 仅为 schema 完整性）。
"""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel

from .common import MaterialTierEnum, TaskTierEnum


class TaskListItem(BaseModel):
    task_id: str
    milestone: MaterialTierEnum                 # JSON difficulty.material_tier
    task_type: TaskTierEnum                     # JSON difficulty.task_tier
    task_subtype: str                           # "vc_relax" / "bands_dos"
    material_formula: str
    material_name: Optional[str] = None
    schema_version: str
    network_allowed: bool                       # JSON task.policy.network_allowed
    gt_quality: str                             # "self_run_converged" / "marginal" / "keep_mp" / "failed_*"
    # is_stable=True：normal + dev role 都可见；
    # is_stable=False：仅 dev role 可见（开发中题目，未公开）
    # JSON 路径：metadata.is_stable，缺省 True 兼容老题
    is_stable: bool = True
    last_score: Optional[float] = None          # derived: SELECT MAX(total_score) FROM runs WHERE ...
    last_run_at: Optional[str] = None           # derived; ISO Z
    last_run_count: int = 0                     # derived
    material: dict                              # 短键摘要 {f, n, sys, sg, fam, m, a}（详 docs/03 §3.1）
    rendered_prompt: str                        # harness.lib.task_loader.render_prompt
    initial_files: list[dict]                   # harness.lib.task_loader.derive_initial_files
    extract_fields: list[dict]                  # JSON evaluator.extract_fields（嵌套一层，不是顶级）
    convergence_evidence: Optional[dict] = None # JSON convergence_evidence


class AgentViewResponse(BaseModel):
    """与 §3.1 fat list 字段重叠（保留备前端按需单查 / share-link 直跳）。"""
    task_id: str
    prompt: str
    initial_files: list[dict]
