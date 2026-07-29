"""Task Ledger + Progress Ledger — v4.3 Magentic-One-style 双 ledger。

Orchestrator 持有这两个 ledger：
- TaskLedger: 题目要求、初始 plan、replan 历史（用于 prompt 给 planner 看上下文）
- ProgressLedger: 每个 step 的执行状态、attempts、verifier diagnosis

序列化到 workdir/orchestrator.json，供事后分析 + bench 调试。

数据契约由 pydantic 模型守住：planner 用 submit_plan 工具产出 PlanSpec，
verifier 用 submit_verdict 工具产出 VerdictSpec，两端都强校验，schema 漏
字段时 LLMClient 重发一次（错误信息含 missing field 提示）。
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator


# ───────────────────────────── Plan schema (submit_plan) ─────────────────────

StepKind = Literal["scf", "relax", "vc_relax", "bands", "dos", "collect", "physics_sanity"]


class PlanStep(BaseModel):
    """Planner 在 submit_plan 中产出的单个 step。"""
    id: str = Field(..., description="Unique step id, e.g. 'scf_1'")
    kind: StepKind = Field(..., description="One of: scf, relax, vc_relax, bands, dos, collect")
    desc: str = Field(..., description="One-line goal for the executor")
    deps: list[str] = Field(
        default_factory=list,
        description="Step ids that must complete before this one (topological order)",
    )
    expected_outputs: dict[str, str] = Field(
        ...,
        description="key → workdir-relative path; verifier checks these exist and are valid",
    )

    @field_validator("id")
    @classmethod
    def _id_nonempty(cls, v: str) -> str:
        if not v or "/" in v or v.startswith("."):
            raise ValueError(f"invalid step id {v!r}: must be a simple identifier")
        return v

    @field_validator("expected_outputs")
    @classmethod
    def _outputs_nonempty(cls, v: dict[str, str]) -> dict[str, str]:
        if not v:
            raise ValueError("expected_outputs must have at least one entry")
        for k, p in v.items():
            if not isinstance(p, str) or not p or Path(p).is_absolute():
                raise ValueError(f"expected_outputs[{k!r}] must be a non-empty relative path")
        return v

    @model_validator(mode="after")
    def _outputs_have_step_prefix(self) -> "PlanStep":
        """Every output path MUST start with `<step.id>/` so the file location
        matches the executor's per-step subdir convention. See
        agent/orchestrator.py:_build_planner_system_prompt §PATH CONVENTION."""
        prefix = f"{self.id}/"
        bad = {k: p for k, p in self.expected_outputs.items() if not p.startswith(prefix)}
        if bad:
            entries = ", ".join(f"{k!r}={p!r}" for k, p in bad.items())
            raise ValueError(
                f"step {self.id!r}: expected_outputs paths must start with "
                f"{prefix!r}; offending: {entries}"
            )
        return self


class PlanSpec(BaseModel):
    """Planner 的完整 plan。submit_plan tool 输入 schema。"""
    steps: list[PlanStep] = Field(..., min_length=1)

    @field_validator("steps")
    @classmethod
    def _no_duplicate_ids(cls, v: list[PlanStep]) -> list[PlanStep]:
        ids = [s.id for s in v]
        if len(ids) != len(set(ids)):
            dup = [i for i in ids if ids.count(i) > 1]
            raise ValueError(f"duplicate step ids: {sorted(set(dup))}")
        return v

    @field_validator("steps")
    @classmethod
    def _deps_resolve(cls, v: list[PlanStep]) -> list[PlanStep]:
        ids = {s.id for s in v}
        for s in v:
            for d in s.deps:
                if d not in ids:
                    raise ValueError(f"step {s.id!r}.deps references unknown step {d!r}")
                if d == s.id:
                    raise ValueError(f"step {s.id!r}.deps contains self-reference")
        return v


# ───────────────────────────── Verdict schema (submit_verdict) ───────────────

class VerdictSpec(BaseModel):
    """Verifier 的最终判决。submit_verdict tool 输入 schema。

    Both fields required so the LLM always thinks about both. Empty string OK
    when pass=True; concrete diagnosis required when pass=False.
    """
    pass_: bool = Field(..., alias="pass",
                        description="True iff all expected_outputs exist and pass checklist")
    diagnosis: str = Field(
        ...,
        description="If pass=False: concrete error description (file missing, "
                    "convergence failed, wrong value) for the executor to fix. "
                    "If pass=True: may be empty or a one-line success note.",
    )

    model_config = {"populate_by_name": True}


# ───────────────────────────── Progress ledger ───────────────────────────────

class Attempt(BaseModel):
    """单次 executor + verifier 尝试。"""
    attempt_no: int
    executor_session_id: str            # 用来串联 transcript.json 中的 turns
    executor_submit: Optional[dict[str, Any]] = None   # executor 的 submit payload
    verdict: Optional[VerdictSpec] = None
    started_at: str
    finished_at: Optional[str] = None


class StepProgress(BaseModel):
    step_id: str
    status: Literal["pending", "in_progress", "done", "failed"] = "pending"
    attempts: list[Attempt] = Field(default_factory=list)


# ───────────────────────────── Task ledger ───────────────────────────────────

class ReplanEvent(BaseModel):
    """每次 replan 都记录原因和新 plan。"""
    replan_no: int
    reason: str
    new_plan: PlanSpec
    at: str


class TaskLedger(BaseModel):
    """主 orchestrator 持有的全局状态。序列化到 workdir/orchestrator.json。"""
    required_keys: list[str]
    initial_plan: PlanSpec
    replans: list[ReplanEvent] = Field(default_factory=list)
    progress: dict[str, StepProgress] = Field(default_factory=dict)
    final_status: Literal["in_progress", "completed", "incomplete"] = "in_progress"
    final_reason: Optional[str] = None

    def current_plan(self) -> PlanSpec:
        """Latest replan if any, else initial."""
        return self.replans[-1].new_plan if self.replans else self.initial_plan

    def write(self, path: Path) -> None:
        path.write_text(json.dumps(
            self.model_dump(by_alias=True, mode="json"),
            indent=2, ensure_ascii=False,
        ))


# ───────────────────────────── Helpers ──────────────────────────────────────

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_plan(raw: dict[str, Any]) -> PlanSpec:
    """对 planner submit_plan tool input 做强校验，错误信息适合塞回 prompt。

    抛 ValidationError 时调用方（orchestrator）应把 e.errors() 拼成
    "submit_plan rejected: <fields>: <messages>" 续跑 planner。
    """
    return PlanSpec.model_validate(raw)


def parse_verdict(raw: dict[str, Any]) -> VerdictSpec:
    """Verifier submit_verdict 强校验，同上。"""
    return VerdictSpec.model_validate(raw)


def format_validation_error(e: ValidationError) -> str:
    """把 pydantic ValidationError 拼成给 LLM 看的简明错误。"""
    parts = []
    for err in e.errors():
        loc = ".".join(str(x) for x in err["loc"])
        parts.append(f"  - {loc}: {err['msg']}")
    return "schema validation failed:\n" + "\n".join(parts)


# Tool schemas (JSONSchema dict, fed to LLMClient as tool['parameters'])
SUBMIT_PLAN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["steps"],
    "properties": {
        "steps": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "required": ["id", "kind", "desc", "expected_outputs"],
                "properties": {
                    "id":   {"type": "string", "description": "Unique step id"},
                    "kind": {"type": "string",
                             "enum": ["scf", "relax", "vc_relax", "bands", "dos", "collect"]},
                    "desc": {"type": "string"},
                    "deps": {"type": "array", "items": {"type": "string"}, "default": []},
                    "expected_outputs": {
                        "type": "object",
                        "additionalProperties": {"type": "string"},
                        "description": "key → workdir-relative path",
                    },
                },
            },
        },
    },
}

SUBMIT_VERDICT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["pass", "diagnosis"],
    "properties": {
        "pass": {"type": "boolean",
                 "description": "True iff all expected_outputs exist and pass checklist"},
        "diagnosis": {"type": "string",
                      "description": "If pass=False: concrete error to fix. If pass=True: empty or success note."},
    },
}
