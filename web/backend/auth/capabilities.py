"""Capability registry for granular permission system (F3, 2026-05-08).

is_dev_role (boolean User flag) is the root-level escape hatch:
- Bypasses all capability checks
- Sole gate for token CRUD UI
- NOT a capability (cannot be set via checkbox)

Capabilities are user-configurable via the dev-only token management UI.
A user with no capabilities is equivalent to current "normal" role behavior.
"""
from __future__ import annotations

from typing import Final


# ---- Visibility (3) ----
VIEW_DEV_TASKS: Final = "view.dev_tasks"
VIEW_DEV_AGENTS: Final = "view.dev_agents"
VIEW_OTHERS_RUNS: Final = "view.others_runs"

# ---- Submit (1 — submit.batch_large 被 user 2026-05-08 否决) ----
SUBMIT_URGENT_PRIORITY: Final = "submit.urgent_priority"

# ---- Manage (2) ----
MANAGE_CANCEL_ANY_RUN: Final = "manage.cancel_any_run"
MANAGE_DELETE_ANY_RUN: Final = "manage.delete_any_run"

# ---- Admin sub-modules (8 — 这些 cap 可 grant；is_dev_role 仍然 bypass 一切) ----
ADMIN_VIEW_CLUSTER: Final = "admin.view_cluster"
ADMIN_VIEW_USAGE: Final = "admin.view_usage"
ADMIN_VIEW_FAILURES: Final = "admin.view_failures"
ADMIN_VIEW_RUNS_BULK: Final = "admin.view_runs_bulk"
ADMIN_MANAGE_GPU_POLICY: Final = "admin.manage_gpu_policy"
ADMIN_MANAGE_SCHEDULER: Final = "admin.manage_scheduler"
ADMIN_MANAGE_RUNS_BULK: Final = "admin.manage_runs_bulk"
ADMIN_MANAGE_AGENTS: Final = "admin.manage_agents"  # 0508 加：register / deactivate 外部 agent


ALL_CAPABILITIES: Final[list[str]] = [
    VIEW_DEV_TASKS, VIEW_DEV_AGENTS, VIEW_OTHERS_RUNS,
    SUBMIT_URGENT_PRIORITY,
    MANAGE_CANCEL_ANY_RUN, MANAGE_DELETE_ANY_RUN,
    ADMIN_VIEW_CLUSTER, ADMIN_VIEW_USAGE, ADMIN_VIEW_FAILURES, ADMIN_VIEW_RUNS_BULK,
    ADMIN_MANAGE_GPU_POLICY, ADMIN_MANAGE_SCHEDULER, ADMIN_MANAGE_RUNS_BULK,
    ADMIN_MANAGE_AGENTS,
]


# UI 折叠分组用：前端 token CRUD checkbox grid 按这个 dict 渲染
CAPABILITY_GROUPS: Final[dict[str, list[str]]] = {
    "visibility": [VIEW_DEV_TASKS, VIEW_DEV_AGENTS, VIEW_OTHERS_RUNS],
    "submit":     [SUBMIT_URGENT_PRIORITY],
    "manage":     [MANAGE_CANCEL_ANY_RUN, MANAGE_DELETE_ANY_RUN],
    "admin":      [ADMIN_VIEW_CLUSTER, ADMIN_VIEW_USAGE, ADMIN_VIEW_FAILURES,
                   ADMIN_VIEW_RUNS_BULK, ADMIN_MANAGE_GPU_POLICY,
                   ADMIN_MANAGE_SCHEDULER, ADMIN_MANAGE_RUNS_BULK,
                   ADMIN_MANAGE_AGENTS],
}


# 中文描述：UI tooltip + token CRUD 列表说明
CAPABILITY_DESCRIPTIONS: Final[dict[str, str]] = {
    VIEW_DEV_TASKS:          "看 is_stable=False 题目（catalogue / leaderboard / runs filter）",
    VIEW_DEV_AGENTS:         "看 is_stable=False agent + 它们的 runs / leaderboard 列",
    VIEW_OTHERS_RUNS:        "看别的用户的 run（默认只看自己 owner 的）",
    SUBMIT_URGENT_PRIORITY:  "提交 urgent 优先级（队列插队）",
    MANAGE_CANCEL_ANY_RUN:   "取消别人的 run（默认只能取消自己的）",
    MANAGE_DELETE_ANY_RUN:   "删除别人的 run",
    ADMIN_VIEW_CLUSTER:      "看 cluster status 面板（CPU / GPU 占用）",
    ADMIN_VIEW_USAGE:        "看 usage stats 面板（含 daily chart）",
    ADMIN_VIEW_FAILURES:     "看 failure analyzer 面板",
    ADMIN_VIEW_RUNS_BULK:    "Bulk runs 管理面板",
    ADMIN_MANAGE_GPU_POLICY: "改 GPU 分配 policy",
    ADMIN_MANAGE_SCHEDULER:  "pause / resume scheduler",
    ADMIN_MANAGE_RUNS_BULK:  "批量删 / 回收 runs（写操作）",
    ADMIN_MANAGE_AGENTS:     "从 git URL 注册外部 agent + 停用 / 切版本",
}


# 新建 token（POST /api/tokens）+ migrate 旧 role:normal 时使用的默认 caps。
# user 0508 决策："view.others_runs 启用执行 + 默认值是都看得到" —— 等价于
# 现行 normal 用户体验：能看别人的 run；想限制只看自己的 run，token 管理
# UI 取消勾选 view.others_runs 即可。
# 注意：yaml 显式 capabilities: [] 不会自动注入此 cap —— 明确选择就是空，
# 不做隐式 magic。这是 yaml 手编辑路径与 UI/migration 路径的语义差异。
DEFAULT_NORMAL_TOKEN_CAPS: Final[list[str]] = [VIEW_OTHERS_RUNS]


def validate_capabilities(caps: list[str]) -> list[str]:
    """返 caps 中不在 ALL_CAPABILITIES 内的项；空列表 = 全部合法。"""
    return [c for c in caps if c not in ALL_CAPABILITIES]
