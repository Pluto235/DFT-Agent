"""Per-step minimum timeout_s floor for the LLM-facing bash tool.

The LLM picks `timeout_s` per `bash` tool call (see loop.py:228-231). For
heavy QE steps it routinely underestimates wallclock — concrete case:

  CoSb3 bands_dos 2026-05-19T18-17-00Z 4 次给 dos_nscf 调 timeout_s = 120s
  / 600s，每次被 bash 工具按时砍掉。CoSb3 32 原子方钴矿在 dense kmesh +
  tetrahedra 下 dos_nscf 单次要 20+ 分钟。结果 4 小时 harness budget 全
  被切碎重启吃光，没产出 dos.dat。

This module returns a step-kind-aware floor; loop.py takes
`max(LLM_timeout, floor)` so the LLM can't undershoot but can overshoot.

Hint priority:
  1) explicit `step_kind_hint`（ExecutorAgent 总能给——它拿到的是 PlanStep.kind）
  2) `pw.x -in <input>` 的 input 文件里 `calculation = '<calc>'`（preflight
     subagents 没 step kind 但走 pw.x；按 input 决定）
  3) `60s` 兜底（非 pw.x 命令——grep / ls / heredoc 这些瞬时活）

Tests: tests/test_step_timeout_floor.py
"""
from __future__ import annotations

import re
from pathlib import Path


# Per-step_kind floor (seconds). Conservative — these是 99 分位
# 真实跑工件（/path/to/project/web/runs_local）的下沿稍带余量。
STEP_KIND_FLOOR_S: dict[str, int] = {
    "scf":           300,    # 5 min — Si / Rb 级别完成有余
    "vc_relax":      900,    # 15 min — bfgs 多次迭代
    "bands":         600,    # 10 min — bands NSCF on k-path (sparse)
    "bands_nscf":    600,
    "dos":          1800,    # 30 min — DOS NSCF dense kmesh + tetrahedra
    "dos_nscf":     1800,
    "bands_pp":       60,    # bands.x post-process
    "dos_pp":         60,    # dos.x post-process
    "physics_sanity": 60,    # 仅本地脚本，不跑 QE
}

# 默认兜底（非 pw.x bash 调用）
_DEFAULT_FLOOR_S = 60

# pw.x -in <input> 抓 input 路径
_PWX_INPUT_RE = re.compile(
    r"\bpw\.x\b\s+(?:--?[A-Za-z0-9_]+\s+)*"  # optional flags
    r"-in[ =]\s*(\S+)",
)

# &CONTROL.calculation
_CALC_RE = re.compile(
    r"calculation\s*=\s*['\"]?([A-Za-z\-_]+)['\"]?",
    re.IGNORECASE,
)

# K_POINTS card line: crystal_b / tpiba_b → bands；其他（automatic / gamma）→ DOS
_KPOINTS_RE = re.compile(r"^\s*K_POINTS\s+([A-Za-z_]+)", re.IGNORECASE | re.MULTILINE)


def floor_for_command(
    cmd: str,
    workdir: Path,
    step_kind_hint: str | None = None,
) -> int:
    """Return a wallclock floor (s) for the given bash command + workdir.

    Args:
        cmd: 原始 bash 命令字符串
        workdir: BashTool.workdir，用于解析 pw.x input 路径
        step_kind_hint: ExecutorAgent 调用时传 step.kind；preflight 传 None

    Returns:
        floor in seconds. Caller does `max(llm_timeout, floor)`.
    """
    # 1) explicit hint from executor — 最权威
    if step_kind_hint is not None:
        norm = step_kind_hint.lower().replace("-", "_")
        if norm in STEP_KIND_FLOOR_S:
            return STEP_KIND_FLOOR_S[norm]
        # 未知 step_kind：给 scf 级 floor，避免给过低
        return STEP_KIND_FLOOR_S["scf"]

    # 2) infer from pw.x input
    m = _PWX_INPUT_RE.search(cmd)
    if not m:
        return _DEFAULT_FLOOR_S
    input_rel = m.group(1)
    input_path = workdir / input_rel
    try:
        text = input_path.read_text(encoding="utf-8", errors="ignore")[:4096]
    except (OSError, ValueError):
        # 文件不存在或读不了：当 scf 级处理（保守）
        return STEP_KIND_FLOOR_S["scf"]
    calc_m = _CALC_RE.search(text)
    if not calc_m:
        return STEP_KIND_FLOOR_S["scf"]
    calc = calc_m.group(1).lower().replace("-", "_")
    if calc == "vc_relax":
        return STEP_KIND_FLOOR_S["vc_relax"]
    if calc == "relax":
        return STEP_KIND_FLOOR_S["vc_relax"]  # ions-only relax 也按 vc_relax 兜
    if calc == "scf":
        return STEP_KIND_FLOOR_S["scf"]
    if calc == "nscf":
        # bands NSCF 用 crystal_b / tpiba_b；DOS NSCF 用 automatic / gamma
        kp = _KPOINTS_RE.search(text)
        if kp and kp.group(1).lower() in ("crystal_b", "tpiba_b"):
            return STEP_KIND_FLOOR_S["bands_nscf"]
        return STEP_KIND_FLOOR_S["dos_nscf"]
    if calc == "bands":
        return STEP_KIND_FLOOR_S["bands_nscf"]
    return STEP_KIND_FLOOR_S["scf"]
