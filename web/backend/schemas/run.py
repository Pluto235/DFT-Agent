"""Run schemas — start request union / list item / transcript / cancel。

discriminated union by `custom`：bench (False) vs custom (True)。
prototype `app.jsx launchBenchRun / launchCustomRun` 直接用此 shape。
"""
from __future__ import annotations

from typing import Annotated, Literal, Optional, Union

from pydantic import BaseModel, Field

from .common import ModeEnum, RoleEnum, SourceEnum, StatusEnum
from .material import MaterialSpec


# 调度优先级。urgent 仅 dev role 可设；start_run / start_batch 校验。
PriorityEnum = Literal["normal", "high", "urgent"]


# ============ start request union ============

class BenchRunStartRequest(BaseModel):
    custom: Literal[False] = False
    # task_id 由 generate.py 生成 ID，最长格式 `M2_T2_LiAlSi2O6_spodumene_bands_dos`
    # 实测最长 38 字。max_length=128 给足余量
    task_id: str = Field(max_length=128)
    model: str = Field(max_length=128)              # ∈ ConfigResponse.available_models[].id
    agent_id: Optional[str] = Field(default=None, max_length=128)
    max_turns: int = Field(default=100, ge=1, le=200)  # 与 settings.py max_turns_default 同步（0505 升级 50→100）
    # harness `--timeout`：solver 总 wall-clock 上限（秒）。lower 60s 防 user error，
    # upper 86400s = 24h（M2/T2 大体系单题动辄数小时；前端按 h 显示）。
    # 默认 7200s = 2h，覆盖典型 vc-relax；超长任务由 dev 显式调高。
    solver_timeout_sec: int = Field(default=7200, ge=60, le=86400)
    solver_path: Optional[str] = Field(default=None, max_length=2048)  # deprecated
    solver_args: Optional[str] = Field(default=None, max_length=4096)  # 透传到 harness
    priority: PriorityEnum = "normal"
    # QE backend：gpu (default, podman wrapper) / cpu (native CPU build via qe-cpu-wrapper).
    # CPU 模式跳过 gpu_allocator.acquire() 且在 spawner 里把 env["QE_GPU_WRAPPER_DIR"]
    # 覆盖到 QE_CPU_WRAPPER_DIR 指向的 CPU wrapper 目录。
    qe_backend: Literal["gpu", "cpu"] = "gpu"


class CustomRunStartRequest(BaseModel):
    custom: Literal[True] = True
    # prompt 长度校验在 api/runs.py:start_run 内做（docs §5.1.4: < 20 → 400 not 422）。
    # max_length 防超大 payload 撑爆 SQLite（custom_spec_json 列）；
    # 上限 50000（远超合理 prompt + 留 task spec 嵌入空间）
    prompt: str = Field(max_length=50000)
    material: MaterialSpec
    task_type: str = Field(max_length=64)           # ∈ task_types[].id where supported_in_custom=True
    model: str = Field(max_length=128)
    agent_id: Optional[str] = Field(default=None, max_length=128)
    max_turns: int = Field(default=100, ge=1, le=200)  # 与 settings.py max_turns_default 同步（0505 升级 50→100）
    max_output_tokens: int = Field(default=16384, ge=512, le=32000)
    # custom 暂未接线（solver_wrapper 不读 timeout，由 max_turns + agent 自决定）；保留字段供未来对齐。
    solver_timeout_sec: int = Field(default=7200, ge=60, le=86400)
    solver_path: Optional[str] = Field(default=None, max_length=2048)
    solver_args: Optional[str] = Field(default=None, max_length=4096)
    priority: PriorityEnum = "normal"
    qe_backend: Literal["gpu", "cpu"] = "gpu"


RunStartRequest = Annotated[
    Union[BenchRunStartRequest, CustomRunStartRequest],
    Field(discriminator="custom"),
]


class RunStartResponse(BaseModel):
    run_id: str                                     # "{ISO_TS}_{task_id}" / "{ISO_TS}_custom_{shorthash6}"
    task_id: Optional[str] = None
    mode: ModeEnum
    model: str
    status: Literal["queued", "running"]
    created_at: str
    transcript_url: str
    result_url: str
    run_dir: str
    queue_position: Optional[int] = None            # status=queued 时
    queue_total: Optional[int] = None
    max_turns: int
    current_turn: int = 0
    # 多租户 + 优先级回显——前端立即写进 RunStrip / History，不等下次 polling
    owner: str = "localhost"
    priority: PriorityEnum = "normal"
    agent_id: Optional[str] = None
    qe_backend: Literal["gpu", "cpu"] = "gpu"


# ============ list item (fat shape) ============

class RunListItem(BaseModel):
    """fat shape — 把 RunStrip + History + 单查所需字段全合并；前端 GET /api/runs 一次拿全。"""
    run_id: str
    mode: ModeEnum
    task_id: Optional[str] = None                   # custom 时 null
    custom_task_summary: Optional[str] = None       # custom: prompt 前 60 字 + "…"
    custom_spec: Optional[dict] = None              # custom: 完整 spec dict（dao 从 custom_spec_json TEXT loads）
    model: str
    agent_id: Optional[str] = None                  # 跑这个 run 时用的 agent；老 row（migration 前）= null
    status: StatusEnum
    source: SourceEnum
    created_at: str
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    wall_time_sec: Optional[float] = None
    total_score: Optional[float] = None             # custom 始终 null
    current_turn: int = 0                           # 运行时由 transcript_reader 实时算；终态由 monitor 写回 cache
    max_turns: int
    solver_timeout_sec: int = 7200                  # 用户提交时设的 wall 上限（harness `--timeout`）；
                                                    # 暴露给前端是为了 re-run 能精确复刻原 run 配置
    exit_code: Optional[int] = None
    error: Optional[str] = None
    queue_position: Optional[int] = None            # status=queued 时
    queue_total: Optional[int] = None
    run_dir: str
    # 终态写入：sum(transcript usage) + 按 ModelInfo 价格转 USD（CNY 走固定汇率）。
    # 价格未配 / transcript 缺失时仍 None，前端显示 "—"。
    input_tokens_total: Optional[int] = None
    output_tokens_total: Optional[int] = None
    cost_usd: Optional[float] = None
    # 多租户：阿里云 cluster 逻辑——all see all，UI 加 owner 列；只有自己 + dev 能 cancel/delete。
    # 老库 migration 后 default 'localhost'；disabled mode 下也是 'localhost'。
    owner: str = "localhost"
    priority: PriorityEnum = "normal"
    qe_backend: Literal["gpu", "cpu"] = "gpu"   # 老 row migration 后 default 'gpu'
    # QE 子进程统计：兼容老 row（NULL）；新 run 由 monitor 解析 bash.log 写
    qe_call_count: Optional[int] = None
    qe_wall_sec: Optional[float] = None
    # QE wall 双信号：分别保留 .out 自报 + bash 求和，让前端在两路明显不一致时（agent 重跑
    # 覆盖 .out / 后台启动等）能用括号补充提示。两值都是 ≥ 0；NULL = 没数据（老 row）。
    qe_wall_self_reported_sec: Optional[float] = None
    qe_wall_bash_cmd_sec: Optional[float] = None
    # 总 LLM call wall（秒）= sum(transcript[*].llm_wall_ms) / 1000；主循环层 time.monotonic
    # 测，与 9 provider adapter 解耦。baseline-audit-rerun 用 "qe_time ≈ wall − llm − bash" 间接推；
    # 历史 v1/v2 老 run 不记此字段 → NULL。
    llm_wall_total_sec: Optional[float] = None


# ============ transcript ============

class TranscriptTurn(BaseModel):
    """flat shape — backend transcript_adapter 从 raw transcript.json 适配后产出。"""
    turn: int                                       # 1-based
    role: RoleEnum
    stop_reason: Optional[str] = None               # "tool_use" / "end_turn" / "max_tokens" / null
    text_blocks: list[str]                          # raw content[].type=="text"
    tool_calls: list[dict]                          # [{tool_id, tool_name, input}]；单 turn 可多个（parallel tool use）
    tool_results: list[dict]                        # [{tool_id, stdout_tail, has_content}] 重建自 workdir/bash.log
    usage: Optional[dict] = None                    # {input_tokens, output_tokens}
    # wall_offset_sec：从 run.started_at 起算到该 turn 的第一条 bash 完成时刻的偏移（秒）；hover 显示
    wall_offset_sec: Optional[float] = None
    # cmd_wall_sec：该 turn 内所有 bash 调用的 wall_s 之和（"这个 turn 实际占了多少 wall"）；
    # 聊天 UI 主时间标显示，避免用户做 turn N+1 - turn N 减法。无 bash 的 turn = 0 / None。
    cmd_wall_sec: Optional[float] = None
    # qe_call_count：该 turn 内通过 qe_tools.run_pw / run_dos / run_bands / run_pp 实际启动
    # 的 QE binary 数（来自 workdir/qe_calls.jsonl trace 的 ts ∈ [bash.ts, bash.ts+wall] 窗口
    # 匹配）。前端用它判断是否高亮 "QE-tools" 标签——失败的 import / 只是 cat qe_tools.py
    # 这些 cmd 表面看像 qe_tools 但 trace 没记录，所以 count = 0 不高亮。
    # null = 没数据（非 v2 solver / 老 row / qe_calls.jsonl 不存在）；与 0（v2 但本 turn 没真
    # 启动 binary）语义不同，前端可分别处理。
    qe_call_count: Optional[int] = None


class TranscriptSnapshot(BaseModel):
    run_id: str
    status: StatusEnum
    current_turn: int                               # 总 turn 数（raw transcript len）
    since_turn: int                                 # echo
    turns: list[TranscriptTurn]                     # raw[since_turn:]
    etag: Optional[str] = None


# ============ cancel ============

class CancelRequest(BaseModel):
    reason: Optional[str] = None


class CancelResponse(BaseModel):
    run_id: str
    status: Literal["cancelled", "running"]         # 不暴露 "cancelling" 中间态（prototype StatusBadge 不识此值）
    cancel_requested_at: str
    cancelled_at: Optional[str] = None
    error: Optional[str] = None
