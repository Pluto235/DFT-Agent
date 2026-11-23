"""Agent registry schema —— `agent_registry.yaml` 持久化结构。

每次成功注册写入一条 entry；同 id 的多个 commit 都保留（旧版本可回滚），
但同 id 任何时刻仅一条 `is_active=True` —— UI 默认显示 active 那条。

文件位置：`<REPO_ROOT>/agent_registry.yaml`（未入 git，类似 tokens.yaml）。
读：startup time + admin endpoint；写：仅 admin endpoint（`backend/services/agent_registry.py`，
带 file lock 避免并发污染）。
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from pydantic import BaseModel, Field

from .agent_manifest import AgentManifest


class SmokeTaskResult(BaseModel):
    """单题 smoke 运行结果，挂到 AgentRegistryEntry.smoke_results。"""

    task_id: str
    total_score: Optional[float] = Field(
        default=None, description="None = run 未完成（timeout / crash）"
    )
    audit_flags_ok: int = Field(default=0, description="6 个 audit flag 中 True 数量")
    audit_flags_total: int = Field(default=6)
    exit_code: Optional[int] = None
    wall_time_sec: Optional[float] = None
    stderr_excerpt: str = Field(
        default="", description="末尾 ~2KB stderr，失败时 UI 显示用"
    )
    failed_reason: str = Field(
        default="", description="高层失败描述（timeout / not_invoked / score_below_threshold 等）"
    )


class AgentRegistryEntry(BaseModel):
    """一条注册记录。同 id 多个 entry 区分 commit_sha + is_active。"""

    # ---- 来自 manifest（注册时复制）----
    manifest: AgentManifest

    # ---- 注册时新增 ----
    git_url: str = Field(..., description="HTTPS git URL（私库走 bench 主机 gh CLI 已登录态）")
    commit_sha: str = Field(..., min_length=7, max_length=40, description="clone 时的 commit hash")
    clone_path: str = Field(
        ..., description="本地 clone 路径，绝对（如 ~/workspace/.agent-cache/<id>-<sha>/）"
    )
    registered_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    registered_by: str = Field(
        ..., description="注册操作的 user.name / token id（admin endpoint 写入）"
    )
    smoke_results: list[SmokeTaskResult] = Field(
        default_factory=list,
        description="本次注册的 smoke 题运行结果",
    )
    is_active: bool = Field(
        default=True,
        description="同 id 任意时刻仅一条 True；reactivate 旧版本会先把现有 active 切 False",
    )

    @property
    def short_sha(self) -> str:
        return self.commit_sha[:7]


class AgentRegistry(BaseModel):
    """`agent_registry.yaml` 顶层结构。"""

    schema_version: str = Field(default="1.0", description="future-proofing")
    entries: list[AgentRegistryEntry] = Field(default_factory=list)

    def active_by_id(self, agent_id: str) -> Optional[AgentRegistryEntry]:
        for e in self.entries:
            if e.manifest.id == agent_id and e.is_active:
                return e
        return None

    def all_versions_of(self, agent_id: str) -> list[AgentRegistryEntry]:
        """按 registered_at 倒序返回该 id 所有版本（含 inactive）。"""
        out = [e for e in self.entries if e.manifest.id == agent_id]
        out.sort(key=lambda e: e.registered_at, reverse=True)
        return out

    def list_active(self) -> list[AgentRegistryEntry]:
        return [e for e in self.entries if e.is_active]
