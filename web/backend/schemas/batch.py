"""Batch submit schemas — POST /api/runs:batch（iter-4 新增）。

部分失败容忍：accepted / failed 两数组并存；spec_index 与 request specs[] 下标一一对应（prototype 用作 retry 键）。
error_code 必须含 "task_not_found"（prototype batch.jsx 用作 soft 黄/hard 红 className 分支）。
"""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field

from .common import ErrorCodeEnum
from .run import PriorityEnum


class BatchSpec(BaseModel):
    task_id: str
    model: Optional[str] = None                     # 缺省继承 batch.model
    max_turns: Optional[int] = None                 # 缺省继承 batch.max_turns
    priority: Optional[PriorityEnum] = None         # 缺省继承 batch.priority


class BatchSubmitRequest(BaseModel):
    # 上限放宽到 250：bench v1.2 共 172 题，用户希望一口气提交全部 T1 (86) 或全部 (172)。
    # 串行 enqueue 175 题约 1.5s wall（每条 2 次 SQLite + 校验，~10ms），不会撞超时。
    specs: list[BatchSpec] = Field(min_length=1, max_length=250)
    model: str
    agent_id: Optional[str] = Field(default=None, max_length=128)
    max_turns: int = Field(default=50, ge=1, le=200)
    solver_timeout_sec: int = Field(default=7200, ge=60, le=86400)
    priority: PriorityEnum = "normal"
    qe_backend: Literal["gpu", "cpu"] = "gpu"   # applies to every spec in the batch


class BatchAccepted(BaseModel):
    spec_index: int                                 # 0-based，对应 request specs[idx]
    run_id: str
    task_id: str
    queue_position: Optional[int] = None            # null 表 v1 直接 spawn 没排队（audit P1-2）
    queue_total: int                                # 发请求时刻队列总数


class BatchFailed(BaseModel):
    spec_index: int
    spec: BatchSpec                                 # echo 失败的那条
    error_code: ErrorCodeEnum
    error_msg: str


class BatchSubmitResponse(BaseModel):
    accepted: list[BatchAccepted]
    failed: list[BatchFailed]
