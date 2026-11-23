"""bench: 读 run_dir/report.json (failure-aware Optional 字段)。
custom: 读 workdir/result.json (final_answer / incomplete_reason) +
       transcript.json (经 adapter 算 transcript_summary) +
       custom_task_dir/<run_id>.json (spec)；其余字段从 SQLite 行来。

关键事实（spike 实测，docs/04 §4.6）：
- harness 不写 metadata.json — bench mode 所有终态字段在 report.json
- final_answer 落盘点是 workdir/result.json（solver submit 写）；不从 transcript 反查
- model: bench 读 report.json.model；custom 从 SQLite runs.model
- mode 由 SQLite runs.mode 权威，不从文件分支推断
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

from backend.schemas.result import BenchRunResult, CustomRunResult, RunResult
from backend.services.transcript_adapter import adapt_raw_transcript


def read_run_result(
    *,
    run_id: str,
    mode: str,
    run_dir: Path,
    model: str,
    exit_code: Optional[int],
    wall_time_sec: Optional[float],
    solver_status: Optional[str],
    custom_task_dir: Path,
    input_tokens_total: Optional[int] = None,
    output_tokens_total: Optional[int] = None,
    cost_usd: Optional[float] = None,
    bench_questions_dir: Optional[Path] = None,
) -> Optional[RunResult]:
    """根据 mode 分发；bench 不存在 report.json 时返 None。
    `bench_questions_dir`：若给则尝试加载 task JSON 拿 ground_truth 字段（pred vs gt 对比用）。
    """
    if mode == "bench":
        return _read_bench_result(
            run_id=run_id, run_dir=run_dir,
            model=model, wall_time_sec=wall_time_sec,
            input_tokens_total=input_tokens_total,
            output_tokens_total=output_tokens_total, cost_usd=cost_usd,
            bench_questions_dir=bench_questions_dir,
        )
    return _read_custom_result(
        run_id=run_id, run_dir=run_dir, model=model,
        exit_code=exit_code, wall_time_sec=wall_time_sec,
        solver_status=solver_status, custom_task_dir=custom_task_dir,
        input_tokens_total=input_tokens_total,
        output_tokens_total=output_tokens_total, cost_usd=cost_usd,
    )


def _load_task_ground_truth(bench_questions_dir: Path, task_id: str) -> Optional[dict]:
    """从 questions/<milestone>_<tasktype>/<task_id>.json 加载 ground_truth 字段；
    给 result UI 展示 pred vs gt 对比用。task_id 形如 'M1_T1_Si_vc_relax'，
    JSON 在 questions/M1_T1/M1_T1_Si_vc_relax.json。"""
    parts = task_id.split("_")
    if len(parts) < 3:
        return None
    subdir = f"{parts[0]}_{parts[1]}"
    p = bench_questions_dir / subdir / f"{task_id}.json"
    if not p.exists():
        return None
    try:
        d = json.loads(p.read_text())
        return d.get("ground_truth")
    except (OSError, json.JSONDecodeError):
        return None


def _read_bench_result(
    *, run_id: str, run_dir: Path,
    model: Optional[str] = None,
    wall_time_sec: Optional[float] = None,
    input_tokens_total: Optional[int] = None,
    output_tokens_total: Optional[int] = None,
    cost_usd: Optional[float] = None,
    bench_questions_dir: Optional[Path] = None,
) -> Optional[BenchRunResult]:
    report_path = run_dir / "report.json"
    if not report_path.exists():
        return _read_bench_partial(
            run_id=run_id, run_dir=run_dir,
            model=model, wall_time_sec=wall_time_sec,
            input_tokens_total=input_tokens_total,
            output_tokens_total=output_tokens_total, cost_usd=cost_usd,
        )
    report: dict[str, Any] = json.loads(report_path.read_text())
    gt: Optional[dict] = None
    if bench_questions_dir is not None:
        gt = _load_task_ground_truth(bench_questions_dir, report["task_id"])
    return BenchRunResult(
        mode="bench",
        run_id=run_id,
        task_id=report["task_id"],
        solver_exit_code=report["solver_exit_code"],
        solver_status=report["solver_status"],
        # report.json 字段名是 solver_wall_clock_sec（spike 实测）；prototype 期望 wall_time_sec（宪法 2）
        wall_time_sec=float(report["solver_wall_clock_sec"]),
        run_dir=str(run_dir),
        # failure path 的 report.json 没 model（实测仅 7 字段），fallback 到 SQLite row 传进来的 model
        model=report.get("model") or model,
        extracted_fields=report.get("extracted_fields"),
        per_field_scores=report.get("per_field_scores"),
        ground_truth=gt,
        total_score=report.get("total_score"),
        audit_flags=report.get("audit_flags"),
        task_outputs=report.get("task_outputs", {}) or {},
        notes=report.get("notes"),
        failure_reason=report.get("failure_reason"),
        input_tokens_total=input_tokens_total,
        output_tokens_total=output_tokens_total,
        cost_usd=cost_usd,
    )


def _infer_task_id_from_run_id(run_id: str) -> Optional[str]:
    """run_id 形如 '2026-05-03T03-14-58Z_M1_T2_AlP_bands_dos' 或带 '_pending_xxx' 后缀。
    bench mode 下 task_id 是 ISO 时间戳后到（可能的）'_pending_...' 之前的部分。"""
    parts = run_id.split("_", 1)
    if len(parts) < 2:
        return None
    rest = parts[1]
    suffix = rest.split("_pending_", 1)[0]
    return suffix or None


def _read_bench_partial(
    *, run_id: str, run_dir: Path,
    model: Optional[str],
    wall_time_sec: Optional[float],
    input_tokens_total: Optional[int],
    output_tokens_total: Optional[int],
    cost_usd: Optional[float],
) -> Optional[BenchRunResult]:
    """harness 被中断没写 report.json，但 solver 已写 workdir/result.json → 降级展示。
    返 None 表示也没 result.json（彻底无任何产出）→ endpoint 仍 404。"""
    workdir = run_dir / "workdir"
    result_path = workdir / "result.json"
    if not result_path.exists():
        return None
    try:
        rj: dict[str, Any] = json.loads(result_path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    task_id = _infer_task_id_from_run_id(run_id) or run_id
    return BenchRunResult(
        mode="bench",
        run_id=run_id,
        task_id=task_id,
        solver_exit_code=0,
        solver_status="ok",
        wall_time_sec=float(wall_time_sec) if wall_time_sec is not None else 0.0,
        run_dir=str(run_dir),
        model=model,
        task_outputs=rj.get("task_outputs", {}) or {},
        notes=rj.get("notes"),
        input_tokens_total=input_tokens_total,
        output_tokens_total=output_tokens_total,
        cost_usd=cost_usd,
        harness_interrupted=True,
    )


def _read_custom_result(
    *,
    run_id: str,
    run_dir: Path,
    model: str,
    exit_code: Optional[int],
    wall_time_sec: Optional[float],
    solver_status: Optional[str],
    custom_task_dir: Path,
    input_tokens_total: Optional[int] = None,
    output_tokens_total: Optional[int] = None,
    cost_usd: Optional[float] = None,
) -> CustomRunResult:
    workdir = run_dir / "workdir"
    final_answer: Optional[dict] = None
    incomplete_reason: Optional[str] = None
    result_path = workdir / "result.json"
    if result_path.exists():
        try:
            result_data = json.loads(result_path.read_text())
            if result_data.get("incomplete"):
                incomplete_reason = result_data.get("reason")
            else:
                final_answer = result_data
        except json.JSONDecodeError:
            pass

    transcript_path = workdir / "transcript.json"
    flat = []
    if transcript_path.exists():
        try:
            raw = json.loads(transcript_path.read_text())
            if isinstance(raw, list):
                flat = adapt_raw_transcript(raw, workdir / "bash.log")
        except json.JSONDecodeError:
            flat = []

    spec_path = custom_task_dir / f"{run_id}.json"
    spec = json.loads(spec_path.read_text()) if spec_path.exists() else {}

    last_tool_name: Optional[str] = None
    if flat and flat[-1].tool_calls:
        last_tool_name = flat[-1].tool_calls[-1].get("tool_name")

    wall = float(wall_time_sec) if wall_time_sec is not None else 0.0
    return CustomRunResult(
        mode="custom",
        run_id=run_id,
        no_score=True,
        model=model,
        solver_exit_code=int(exit_code) if exit_code is not None else 0,
        solver_status=solver_status or "ok",
        incomplete_reason=incomplete_reason,
        wall_time_sec=wall,
        transcript_summary={
            "turn_count": len(flat),
            "tool_call_count": sum(len(t.tool_calls) for t in flat),
            "wall_time_sec": wall,
            "last_tool_name": last_tool_name,
        },
        final_answer=final_answer,
        task_outputs=(final_answer or {}).get("task_outputs", {}) or {},
        custom_task_spec=spec,
        run_dir=str(run_dir),
        input_tokens_total=input_tokens_total,
        output_tokens_total=output_tokens_total,
        cost_usd=cost_usd,
    )
