"""Config schemas — GET/PUT /api/config。"""
from __future__ import annotations

from pydantic import BaseModel, Field

from .common import ModelProviderEnum, ModelTierEnum


class ModelInfo(BaseModel):
    id: str                                         # "claude-sonnet-4-6" / "gpt-4o" / "gemini-2.5-pro"
    provider: ModelProviderEnum                     # "anthropic" / "openai" / "google" 等；前端按此分组
    tier: ModelTierEnum
    desc: str                                       # "Recommended · balanced cost / quality"
    # available=True ⇔ deployment 有配 key + 已冒烟过；False = UI disabled + tooltip 提示
    available: bool = True                          # False = 后端拒绝 spawn（model_not_available）
    # 原币 cost rate（cache miss / standard）；前端结合 price_currency + 汇率显示 USD。
    input_price_per_mtok: float = 0.0
    output_price_per_mtok: float = 0.0
    price_currency: str = "USD"                     # "USD" / "CNY"


class TaskTypeInfo(BaseModel):
    id: str                                         # "vc_relax" / "bands_dos"
    label_en: str
    label_zh: str
    description: str
    supported_in_custom: bool                       # custom mode 是否暴露此 task_type


class AgentInfo(BaseModel):
    id: str                                         # "v1-stable" / "warplab-v2"
    name: str                                       # "Agent v1 (stable)" — frontend dropdown 显示
    description: str = ""
    available: bool = True                          # False = UI disable + 提示
    # 不返回 solver_path（敏感：绝对路径）；前端只需 id/name 选择，backend 自己 resolve


class ConfigResponse(BaseModel):
    bench_questions_dir: str
    bench_runs_dir: str
    demo_runs_dir: str
    custom_task_dir: str
    agent_solver_path: str                          # deprecated：等价 agents[0].solver_path
    agent_solver_args: str
    available_models: list[ModelInfo]               # [0] 是默认（prototype compose.jsx 行 149）
    enhance_model: str                              # "claude-haiku-4-5" 单独字段，不在 available_models
    max_turns_default: int                          # 100（0505 升级 from 50）
    custom_run_max_turns_default: int               # 100（0505 升级 from 50）
    max_turns_max: int                              # 200
    solver_timeout_default: int                     # 7200 = 2h（0505 加：batch + compose 共用 SSOT）
    max_concurrent_runs: int                        # 当前值
    max_concurrent_runs_limit: int                  # 8 hard cap
    cleanup_intermediates_on_finish: bool = True    # 终态后删 QE tmp/ 子目录（节磁盘）
    schema_version_supported: str                   # "1.3"
    task_types: list[TaskTypeInfo]                  # config-driven，前端不硬编码
    agents: list[AgentInfo]                         # [0] 是默认；前端 nav dropdown 选择


class ConfigUpdateRequest(BaseModel):
    # audit P2-1：上限交给 service 层（动态 settings.max_concurrent_runs_limit），不在 schema 硬编码 le=8。
    max_concurrent_runs: int | None = Field(default=None, ge=1)
    cleanup_intermediates_on_finish: bool | None = None
