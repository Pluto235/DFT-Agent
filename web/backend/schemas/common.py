"""公共枚举：与 prototype StatusBadge / mode 分支 / docs/03 §2 enum 对齐。"""
from __future__ import annotations

from typing import Literal


ModeEnum = Literal["bench", "custom"]
StatusEnum = Literal["queued", "running", "evaluating", "done", "failed", "cancelled"]
SourceEnum = Literal["demo", "bench"]
SolverStatusEnum = Literal["ok", "timeout", "error"]
TaskTypeEnum = Literal["vc_relax", "bands_dos"]
MaterialTierEnum = Literal["M1", "M2", "M3", "M4"]
TaskTierEnum = Literal["T1", "T2", "T3", "T4"]
RoleEnum = Literal["assistant", "user"]
ToolNameEnum = Literal["bash", "write_file", "read_file", "submit"]
StructFormatEnum = Literal["cif", "poscar", "vasp"]
ModelTierEnum = Literal["frontier", "standard", "open"]
# Provider 字段：UI 按 provider 分组；solver 内部 dispatch 不同 client。
# Phase 2 done(2026-05-03):solver 走 native SDK 双路径(anthropic + openai-compat);
# 见 repos/dft-agent-bench-solver/agent/providers.yaml。
ModelProviderEnum = Literal[
    "anthropic", "openai", "google", "deepseek", "moonshot", "xai", "mistral",
    "openrouter", "ollama",
]
MaterialSourceEnum = Literal["upload", "describe", "pick"]

ErrorCodeEnum = Literal[
    "task_not_found",
    "model_invalid",
    "max_turns_invalid",
    "concurrency_full",
    "storage_error",
    "internal",
    "harness_boot_timeout",
    "spawn_failed",
    "priority_forbidden",
    "unknown",
]
