"""Agent manifest schema —— 外部 agent repo 根 `manifest.yaml` 的 Pydantic 校验。

合作者交付的 agent repo 必须含 `manifest.yaml` 声明入口、smoke 题、依赖等元信息。
本 schema 即解析契约，详见 doc/reports/agent/report_agent_integration_for_external.md §3。

注册流程（`backend/services/agent_smoke.py`）：
  1. clone repo → read repo_root / "manifest.yaml" → AgentManifest.model_validate(yaml)
  2. 校验失败 → 422，UI 显示 missing_fields / invalid_value
  3. 校验通过 → 用 manifest.smoke 配置跑 harness，pass 后写入 AgentRegistry
"""
from __future__ import annotations

import re
from typing import Optional

from pydantic import BaseModel, Field, field_validator


# 与 backend/auth/capabilities.py 同款简单 slug 校验：lower kebab，1-64 字符。
_AGENT_ID_RE = re.compile(r"^[a-z][a-z0-9\-]{0,63}$")


class AgentSmokeConfig(BaseModel):
    """注册时跑哪些题验证 agent 跑得起来。"""

    task_ids: list[str] = Field(
        default_factory=lambda: ["M1_T1_Si_vc_relax", "M1_T1_NaCl_vc_relax"],
        min_length=1,
        max_length=10,
        description="要跑的 task_id 列表；任一 score >= pass_threshold 即 pass",
    )
    model: str = Field(
        default="deepseek-v4-flash",
        description="smoke 跑用的 model（建议 cheap 一档，~$0.01-0.02 / 题）",
    )
    pass_threshold: float = Field(
        default=0.5,
        ge=0.0,
        le=1.0,
        description="任一 task total_score >= 此阈值即 pass；不需要全过",
    )
    timeout_per_task_sec: int = Field(
        default=1800,
        ge=60,
        le=7200,
        description="单题最长 wall-time，超时即算 fail",
    )


class AgentManifest(BaseModel):
    """`manifest.yaml` schema。详见 §3 of agent integration doc。"""

    # ---- 必需 ----
    id: str = Field(
        ...,
        description="唯一 slug，写进 DB runs.agent_id；leaderboard 按 (agent_id, model) 分组",
    )
    name: str = Field(..., min_length=1, max_length=128, description="web UI 显示名")
    description: str = Field(default="", max_length=512)
    entrypoint: str = Field(
        ...,
        description="相对 repo 根的 solve.py 路径（如 'agent/solve.py' 或 'solve.py'）",
    )

    # ---- 可选 ----
    version: str = Field(default="0.0.1", max_length=32, description="作者自己的 version；bench 另存 git commit SHA")
    default_args: list[str] = Field(
        default_factory=list,
        description="启动时透传给 solve.py 的额外 args（如 ['--model', 'deepseek-v4-pro']）",
    )
    supported_task_types: Optional[list[str]] = Field(
        default=None,
        description="不写则视为支持全部；写了则只派该列表内 task_type 的题",
    )
    python_requires: str = Field(default=">=3.10", description="提示用，bench 跑在 tritondft (Python 3.10)")
    declared_deps: list[str] = Field(
        default_factory=list,
        description="作者声明的 Python 依赖（review 用；bench 不会 pip install——见 §6）",
    )
    smoke: AgentSmokeConfig = Field(default_factory=AgentSmokeConfig)

    @field_validator("id")
    @classmethod
    def _validate_id(cls, v: str) -> str:
        if not _AGENT_ID_RE.match(v):
            raise ValueError(
                f"id 必须 lower kebab-case 1-64 字符首字母，得 {v!r}"
            )
        return v

    @field_validator("entrypoint")
    @classmethod
    def _validate_entrypoint(cls, v: str) -> str:
        # 拒绝绝对路径 / .. 跳出 repo 根
        if v.startswith("/"):
            raise ValueError("entrypoint 必须是相对路径，不能以 / 开头")
        if ".." in v.split("/"):
            raise ValueError("entrypoint 不能含 .. 跳出 repo 根")
        if not v.endswith(".py"):
            raise ValueError(f"entrypoint 应是 .py 文件，得 {v!r}")
        return v
