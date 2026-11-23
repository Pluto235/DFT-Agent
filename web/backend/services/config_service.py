"""GET /api/config 合成；PUT 写到 config_kv 持久化（max_concurrent_runs）。

settings.ModelInfo / TaskTypeInfo 字段已对齐 prototype data.js（compose.jsx 行 38/125-126/149 消费）。

F3 Step 2 (2026-05-08)：API gate 由 role-string 改 capability：
  - get_config(can_see_dev_agents=...)：缺 view.dev_agents 不返 dev-only agent
  - update_config(is_dev_role=...)：max_concurrent_runs 是全局资源上限，仍走硬
    is_dev_role 守门（没有 cap 对应；详见 backend/auth/capabilities.py 13 cap
    清单——admin.* 都是 admin 面板内的细分，全局资源上限不属其中）
"""
from __future__ import annotations

from typing import Any

from backend.persistence import dao
from backend.persistence.db import Database
from backend.schemas.config import AgentInfo, ConfigResponse, ModelInfo, TaskTypeInfo


_KV_MAX_CONCURRENT = "max_concurrent_runs"
_KV_CLEANUP_INTERMEDIATES = "cleanup_intermediates_on_finish"
_KV_SCHEDULER_PAUSED = "scheduler_paused"
_KV_GPU_POLICY = "gpu_enabled_indices"  # 逗号分隔 int list；空字符串 = 全 disable
_KV_QE_GPU_MODE = "qe_gpu_queueing_mode"  # "backend_pin" | "wrapper_flock"
_QE_GPU_MODES = ("backend_pin", "wrapper_flock")


async def get_effective_max_concurrent(settings: Any, db: Database) -> int:
    """endpoint / scheduler 用的"当前生效"上限：persisted (config_kv) 优先，
    fallback 到 settings 默认。PUT /api/config 后立即生效。"""
    persisted = await dao.config_kv_get(db, _KV_MAX_CONCURRENT)
    return int(persisted) if persisted else int(settings.max_concurrent_runs)


async def get_effective_cleanup(settings: Any, db: Database) -> bool:
    """同 get_effective_max_concurrent，cleanup toggle 持久化到 config_kv。
    默认 True（节磁盘），用户可在前端 nav settings 关掉。"""
    persisted = await dao.config_kv_get(db, _KV_CLEANUP_INTERMEDIATES)
    if persisted is not None:
        return persisted == "1"
    return bool(getattr(settings, "cleanup_intermediates_on_finish", True))


async def get_effective_paused(db: Database) -> bool:
    """软暂停状态：只读 config_kv，没有 settings fallback（默认 False）。
    scheduler.run_forever 启动时读一次恢复 self.paused；endpoint 设值时同步写 KV + scheduler 内存。"""
    persisted = await dao.config_kv_get(db, _KV_SCHEDULER_PAUSED)
    return persisted == "1"


async def set_paused(db: Database, paused: bool) -> None:
    await dao.config_kv_set(db, _KV_SCHEDULER_PAUSED, "1" if paused else "0")


async def get_effective_gpu_policy(db: Database, default_indices: set[int]) -> set[int]:
    """启用 GPU 集合：persisted (config_kv) 优先；没设过 → fallback 到 default（一般是全启用）。

    persisted 格式：逗号分隔 int 字符串；空串 = 显式 disable 全部（与 fallback 区分）。
    检测到的 GPU 数变化时，过滤掉超出范围的 stale 索引（譬如重启后 GPU 数变少）。
    """
    raw = await dao.config_kv_get(db, _KV_GPU_POLICY)
    if raw is None:
        return set(default_indices)
    raw = raw.strip()
    if not raw:
        return set()  # 显式 empty = 全 disable
    out: set[int] = set()
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            v = int(part)
        except ValueError:
            continue
        if v in default_indices:  # 过滤已不存在的 GPU 索引
            out.add(v)
    return out


async def set_gpu_policy(db: Database, enabled: set[int]) -> None:
    await dao.config_kv_set(
        db, _KV_GPU_POLICY,
        ",".join(str(i) for i in sorted(enabled)),
    )


async def get_effective_qe_gpu_mode(settings: Any, db: Database) -> str:
    """QE GPU 排队模式：persisted (config_kv) 优先；没设过 → fallback 到 settings.qe_gpu_queueing_mode
    （即 backend/settings.py 默认值 "backend_pin"）。invalid 值兜底为 backend_pin（最安全）。"""
    persisted = await dao.config_kv_get(db, _KV_QE_GPU_MODE)
    if persisted in _QE_GPU_MODES:
        return persisted
    fallback = getattr(settings, "qe_gpu_queueing_mode", "backend_pin")
    return fallback if fallback in _QE_GPU_MODES else "backend_pin"


async def set_qe_gpu_mode(db: Database, mode: str) -> None:
    """切换模式：仅写 config_kv（运行时态由 caller 同步 mutate app.state.settings）。
    caller 必须先校验 mode in _QE_GPU_MODES，本函数不做校验（与 set_gpu_policy 一致风格）。"""
    await dao.config_kv_set(db, _KV_QE_GPU_MODE, mode)


async def get_config(
    settings: Any, db: Database, *, can_see_dev_agents: bool = False,
) -> ConfigResponse:
    max_concurrent = await get_effective_max_concurrent(settings, db)
    cleanup_intermediates = await get_effective_cleanup(settings, db)
    available_models = [
        ModelInfo(
            id=m.id,
            provider=(m.provider if m.provider in (
                "anthropic", "openai", "google", "deepseek", "moonshot", "xai", "mistral",
                "openrouter", "ollama",
            ) else "anthropic"),
            tier=(m.tier if m.tier in ("frontier", "standard", "open") else "frontier"),
            desc=m.desc or "",
            available=bool(getattr(m, "available", True)),
            input_price_per_mtok=float(getattr(m, "input_price_per_mtok", 0.0) or 0.0),
            output_price_per_mtok=float(getattr(m, "output_price_per_mtok", 0.0) or 0.0),
            price_currency=str(getattr(m, "price_currency", "USD") or "USD"),
        )
        for m in settings.available_models
    ]
    task_types = [
        TaskTypeInfo(
            id=t.id,
            label_en=t.label_en,
            label_zh=t.label_zh or t.label_en,
            description=t.description or "",
            supported_in_custom=bool(t.supported_in_custom),
        )
        for t in settings.task_types
    ]
    # capability-based filter：缺 view.dev_agents 只看 stable agent；持该 cap（含
    # is_dev_role bypass）看全部。caller 用 user.has_capability(VIEW_DEV_AGENTS) 决定。
    agents = [
        AgentInfo(
            id=a.id,
            name=a.name,
            description=a.description or "",
            available=bool(a.available),
        )
        for a in settings.agents
        if can_see_dev_agents or bool(getattr(a, "is_stable", True))
    ]
    return ConfigResponse(
        bench_questions_dir=str(settings.bench_questions_dir),
        bench_runs_dir=str(settings.bench_runs_dir),
        demo_runs_dir=str(settings.demo_runs_dir),
        custom_task_dir=str(settings.custom_task_dir),
        agent_solver_path=str(settings.agent_solver_path),
        agent_solver_args=settings.agent_solver_args,
        available_models=available_models,
        enhance_model=settings.enhance_model,
        max_turns_default=settings.max_turns_default,
        custom_run_max_turns_default=settings.custom_run_max_turns_default,
        max_turns_max=settings.max_turns_max,
        solver_timeout_default=settings.solver_timeout_default,
        max_concurrent_runs=max_concurrent,
        max_concurrent_runs_limit=settings.max_concurrent_runs_limit,
        cleanup_intermediates_on_finish=cleanup_intermediates,
        schema_version_supported=settings.schema_version_supported,
        task_types=task_types,
        agents=agents,
    )


async def update_config(
    settings: Any, db: Database, *,
    max_concurrent_runs: int | None = None,
    cleanup_intermediates_on_finish: bool | None = None,
    is_dev_role: bool = False,
    can_see_dev_agents: bool = False,
) -> ConfigResponse:
    if max_concurrent_runs is not None:
        # 与 /api/runs:pause / :resume 同款守门：max_concurrent 是全局资源上限
        # （影响所有 user 的 promote 节奏），且是"权限管理本身的 meta-knob"——
        # 没有为它专设 capability（详见 capabilities.py 13 cap 清单），仍走硬
        # is_dev_role 守门。0505 fix：之前 user_role 只在 read 通道用，write
        # 通道没 enforce，normal 用户能改全局上限——已在 F3 Step 1/2 收紧。
        if not is_dev_role:
            raise PermissionError(
                "max_concurrent_runs change is restricted to dev role"
            )
        if max_concurrent_runs < 1 or max_concurrent_runs > settings.max_concurrent_runs_limit:
            raise ValueError(
                f"max_concurrent_runs out of range [1, {settings.max_concurrent_runs_limit}]"
            )
        await dao.config_kv_set(db, _KV_MAX_CONCURRENT, str(max_concurrent_runs))
    if cleanup_intermediates_on_finish is not None:
        await dao.config_kv_set(
            db, _KV_CLEANUP_INTERMEDIATES, "1" if cleanup_intermediates_on_finish else "0",
        )
    return await get_config(settings, db, can_see_dev_agents=can_see_dev_agents)
