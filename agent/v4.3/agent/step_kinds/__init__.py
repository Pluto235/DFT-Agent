"""Step kind 注册表 — v4.3 类型化扩展点。

每个 kind 模块导出三样：
- `KIND` (str)               — 名字，与 PlanStep.kind 对应
- `system_prompt_addon(...)` — 给 executor 的指令片段（在 base QE prompt 之后追加）
- `tool_allowlist` (list)    — executor 可见的工具子集
- `verifier_checklist(...)`  — 给 verifier 的检查清单（自然语言 + 物理约束）

新增 task type 只需添加一个新模块 + 注册即可，不动 orchestrator/executor 类。

工具白名单的可选值见 `agent/loop.py:TOOLS`：bash / write_file / read_file / submit
（submit 永远在 allowlist 里，因为 executor 必须能终止）。
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from agent.ledger import PlanStep


@dataclass(frozen=True)
class StepKindSpec:
    kind: str
    # signature: (step, main_workdir, step_subdir_name, upstream_paths) -> str
    # step_subdir_name is the workdir-relative dir name where this step's
    # files MUST live (typically == step.id). The addon should mention paths
    # like `<step_subdir>/scf.in`, not bare `scf.in`.
    system_prompt_addon: Callable[[PlanStep, Path, str, dict[str, list[str]]], str]
    tool_allowlist: tuple[str, ...]    # subset of {bash, write_file, read_file, submit}
    verifier_checklist: Callable[[PlanStep], str]


# Modules imported lazily inside _registry() to avoid heavy imports when not used.
def _registry() -> dict[str, StepKindSpec]:
    from . import bands as _bands
    from . import collect as _collect
    from . import dos as _dos
    from . import physics_sanity as _physics_sanity
    from . import relax as _relax
    from . import scf as _scf
    from . import vc_relax as _vc_relax
    return {
        "scf":             _scf.SPEC,
        "vc_relax":        _vc_relax.SPEC,
        "relax":           _relax.SPEC,
        "bands":           _bands.SPEC,
        "dos":             _dos.SPEC,
        "collect":         _collect.SPEC,
        "physics_sanity":  _physics_sanity.SPEC,
    }


def get(kind: str) -> StepKindSpec:
    reg = _registry()
    if kind not in reg:
        raise KeyError(
            f"unknown step kind {kind!r}; registered: {sorted(reg.keys())}"
        )
    return reg[kind]


def registered_kinds() -> list[str]:
    return sorted(_registry().keys())
