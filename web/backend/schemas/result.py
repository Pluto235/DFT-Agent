"""Result schemas — bench (源 report.json) / custom (源 workdir/result.json + transcript) discriminated union。

字段命名以 spike 0502 实测 report.json 为准：`solver_wall_clock_sec`（不是 wall_time_sec）；
solver_status 收紧为 3 值（ok/timeout/error）；turn_budget_exceeded 走 incomplete_reason（custom only）。
"""
from __future__ import annotations

from typing import Literal, Optional, Union

from pydantic import BaseModel

from .common import SolverStatusEnum


class BenchRunResult(BaseModel):
    """bench mode：字段直接源于 run_dir/report.json（spike 实测 happy 12 字段，failure 仅 7）。
    failure path 时大部分字段缺失，必须 Optional；前端按 failure_reason is not None 切红 banner。
    """
    mode: Literal["bench"] = "bench"
    run_id: str
    task_id: str
    solver_exit_code: int
    solver_status: SolverStatusEnum
    wall_time_sec: float                            # prototype result-history.jsx 行 34/92 读 `report.wall_time_sec`；
                                                    # 内部由 result_reader 从 report.json.solver_wall_clock_sec mapping
    run_dir: str
    # ---- happy path 才有 ----
    model: Optional[str] = None
    extracted_fields: Optional[dict] = None         # T1/T2 不同字段集，详 docs/03 §5.6.1
    per_field_scores: Optional[dict] = None         # {field_name: float ∈ [0,1]}
    # ground_truth：从 task JSON 取出，让 result UI 展示 pred vs gt 对比；
    # API 公开 task object 时这个字段被 hide（不让 agent 看见 GT），但 result endpoint
    # 是评分完后的 view，可以暴露。结构 {field: {value, unit, tolerance, rule}}。
    ground_truth: Optional[dict] = None
    total_score: Optional[float] = None
    audit_flags: Optional[dict] = None
    task_outputs: dict = {}                         # {vc_relax_out, scf_out, ...}（failure 也可能有）
    notes: Optional[str] = None
    failure_reason: Optional[str] = None            # is not None → 前端红色 banner
    # 终态时计算的 LLM token + USD cost；result_reader 从 SQLite row 读出（dao 终态写入）。
    input_tokens_total: Optional[int] = None
    output_tokens_total: Optional[int] = None
    cost_usd: Optional[float] = None
    # harness 被外层 reaper 杀掉，没产 report.json，但 solver 已 submit 写了 workdir/result.json
    # → 降级展示 task_outputs / notes，前端配黄色 banner。
    harness_interrupted: bool = False


class CustomRunResult(BaseModel):
    """custom mode：来源 = SQLite runs 行 + workdir/result.json + workdir/transcript.json + custom_task_dir/<run_id>.json。"""
    mode: Literal["custom"] = "custom"
    run_id: str
    no_score: Literal[True] = True                  # 前端用此判断隐藏 score hero / per-field
    model: str                                      # 来自 SQLite runs.model
    solver_exit_code: int
    solver_status: SolverStatusEnum
    incomplete_reason: Optional[str] = None         # "turn_budget_exceeded (max_turns=N)" / "assistant_ended_without_submit" / null
    wall_time_sec: float                            # prototype 字段名（与 RunListItem 对齐，宪法 2）
    transcript_summary: dict                        # {turn_count, tool_call_count, wall_time_sec, last_tool_name?}
    final_answer: Optional[dict] = None             # 来自 workdir/result.json（submit tool 写）；incomplete 时 None
    task_outputs: dict = {}                         # = (final_answer or {}).get('task_outputs', {})
    custom_task_spec: dict                          # 从 custom_task_dir/<run_id>.json 读
    run_dir: str
    input_tokens_total: Optional[int] = None
    output_tokens_total: Optional[int] = None
    cost_usd: Optional[float] = None


RunResult = Union[BenchRunResult, CustomRunResult]
