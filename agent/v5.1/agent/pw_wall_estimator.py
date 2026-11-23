"""pw.x wall-time estimator — FIX-6.

LLM 给 `timeout_s` 时严重低估 pw.x 计算量是 WO3 / CoSb3 反复失败的主因之一
（0519 WO3 run：120s → ... → 7200s 累计逐步加，最终因默认 cap 仍被截）。
本模块基于 nat / n_kpoints / ecutwfc / calculation 推出粗量级 wall_s（已含
1.5× 安全系数），由 BashTool 调用，与 LLM 的 timeout_s 取 max。

公式（order-of-magnitude，校准至 1-process pw.x）：

    T_scf_per_iter ≈ C × nat² × n_kpoints × (ecutwfc / 40)^1.5
    T_total        ≈ T_scf_per_iter × iter_multiplier(calculation)
    T_estimate     ≈ T_total × 1.5      # safety

iter_multiplier 表（仅粗量级）：
    scf       25     # SCF 收敛通常 ~25 iter
    nscf       8     # 单次评估 + tetrahedra 上密 k 网格的代价
    bands      8
    vc-relax 200     # ~8 BFGS × 25 SCF
    relax    150
    其他      25     # 当 scf 处理（保守）

C = 4e-4，按以下 anchor 拟合：
    - Si vc-relax (nat=2, kpts=216, ecut=40)   → 估算 104s  (实测 ~12s, 8× 过估，无害)
    - Ge scf      (nat=2, kpts=216, ecut=50)   → 估算 18s   (实测 ~14s, 近似)
    - WO3 vc-relax(nat=32, kpts=64, ecut=80)   → 估算 22000s (~6h, 满足 ≥ 14400s 目标)

估算偏小风险：LLM 给的 timeout 不被抬，退化为原行为 —— 仍由 FIX-7 诊断兜底。
估算偏大风险：subprocess 跑完即 return，timeout 只是 cap —— 无副作用。

集成在 agent/tools/bash.py 的 lint 通过后、Popen 前；非 pw.x 命令、找不到
.in 文件、parse 失败均返回 None（安全降级）。
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Optional

from .tools.qe_input_lint import (
    _fortran_to_float,
    _parse_input,
    find_pw_input,
)
from .tools.qe_output_parse import is_pw_x_invocation


# ──────────────────────────────────────────────────────────────────────
# 公式常数
# ──────────────────────────────────────────────────────────────────────

# 单位 s / (nat² × nk × (ecut/40)^1.5 × iter)。按 1-process pw.x anchor 拟合。
# 见模块顶部 docstring 的校准过程。
#
# 2026-05-23 FIX-15 calibration（基于 303 真实 PASS pw.x run 的 wall=Ns）：
#   旧公式 (C=4e-4) median est/actual = 0.81，p75 = 2.41 — 整体偏估值合理。
#   问题是右尾爆炸：WO3 (32 atoms, 512 kpts, 80 Ry, vc-relax) 估到 177949s = 49h
#   → bash Popen.communicate 等到 harness 4h wall 才砍 → tool 永不返回。
#   修法是加 _MAX_ESTIMATE_SEC cap（在 estimate_for_command 出口）而非重标 C，
#   因为公式的 nat² 项对 large-nat 系统天然过 aggressive，C 调小会让 small-nat
#   underestimate。详 reports/standard-bench-failures-5fail-2026-05-22.html §2。
_FORMULA_C = 4e-4

# safety factor — 估算偏大无害，偏小是灾难（pw.x 被 wallclock 截）。
_SAFETY_FACTOR = 1.5

# Cap on the estimator output, applied in estimate_for_command() before
# returning. Pre-FIX-15, an estimator output of 49h would override the LLM's
# 7200s timeout and hang Popen.communicate until the harness wall killed the
# solver. A3 (2026-06): the cap now scales to the harness wall when
# SOLVER_HARNESS_WALL_S is exported — a single large-cell pw.x step may
# legitimately need most of the budget, and a fixed 12000s prematurely SIGKILLs
# M2 (≥30-atom) jobs that could finish in time. The harness wall stays the
# ultimate bound; the cap only keeps the bash watchdog firing INSIDE it.
_MAX_ESTIMATE_SEC = 12000  # fallback when the wall budget is unknown


def _max_estimate_sec() -> int:
    """Estimator cap. ~0.92× the harness wall budget (env SOLVER_HARNESS_WALL_S)
    when known, else the static fallback. Kept below the harness wall so the
    bash watchdog always fires before the uncatchable harness SIGKILL."""
    raw = os.environ.get("SOLVER_HARNESS_WALL_S")
    if raw:
        try:
            wall = float(raw)
            if wall > 0:
                return max(300, int(0.92 * wall))
        except (ValueError, TypeError):
            pass
    return _MAX_ESTIMATE_SEC

# calculation → 总 SCF-equivalent iter 数。一律走小写、strip quote 后查表。
_ITER_MULTIPLIER: dict[str, int] = {
    "scf":      25,
    "nscf":      8,
    "bands":     8,
    "vc-relax": 200,
    "vc_relax": 200,
    "relax":   150,
    "md":       50,
    "vc-md":   200,
}
_DEFAULT_ITER_MULTIPLIER = 25  # 未知 calculation 当 scf 兜底


# ──────────────────────────────────────────────────────────────────────
# K_POINTS 卡解析（qe_input_lint 没暴露 row-count 形式的解析，单独写）
# ──────────────────────────────────────────────────────────────────────

# 找 K_POINTS 卡 header。允许 {automatic} / (automatic) / automatic。
_KPOINTS_HEADER_RE = re.compile(
    r"^[ \t]*K_POINTS[ \t]*[\{\(]?\s*([A-Za-z_]+)\s*[\}\)]?[ \t]*$",
    re.MULTILINE | re.IGNORECASE,
)


def _extract_n_kpoints(text: str) -> Optional[int]:
    """从 pw.x 输入文件文本里提取 k 点总数。

    Returns:
        - automatic: nx × ny × nz
        - gamma:     1
        - crystal_b / tpiba_b: sum of the 4th column over n_segments lines
          （= n_segments-1 段路径插值点之和；每段 N 个点）
        - crystal  / tpiba   : 头行的 nk
        - 解析失败 / 无 K_POINTS 卡 → None

    设计：宁可返回 None 不要返回错的值。estimator 拿到 None 就不抬 timeout，
    退化到 LLM 自己给的值 —— 安全降级。
    """
    m = _KPOINTS_HEADER_RE.search(text)
    if m is None:
        return None
    form = m.group(1).lower()
    # 头行结尾的位置；下面所有 form 都需要读 header 之后的若干行
    after = text[m.end():].lstrip("\n")
    lines = after.splitlines()
    if form == "gamma":
        return 1
    if form == "automatic":
        if not lines:
            return None
        toks = lines[0].split()
        if len(toks) < 3:
            return None
        try:
            nx, ny, nz = int(toks[0]), int(toks[1]), int(toks[2])
        except ValueError:
            return None
        if nx <= 0 or ny <= 0 or nz <= 0:
            return None
        return nx * ny * nz
    if form in ("crystal_b", "tpiba_b"):
        # 头一行是 n_segments；后续 n_segments 行每行末列是该段插值点数
        if not lines:
            return None
        try:
            n_seg = int(lines[0].split()[0])
        except (ValueError, IndexError):
            return None
        if n_seg <= 0 or n_seg > len(lines) - 1:
            return None
        total = 0
        for i in range(1, n_seg + 1):
            toks = lines[i].split()
            if len(toks) < 4:
                # 没有插值点列：默认 1 点
                total += 1
                continue
            try:
                total += int(toks[3])
            except ValueError:
                return None
        return total if total > 0 else None
    if form in ("crystal", "tpiba"):
        if not lines:
            return None
        try:
            nk = int(lines[0].split()[0])
        except (ValueError, IndexError):
            return None
        return nk if nk > 0 else None
    return None


# ──────────────────────────────────────────────────────────────────────
# 估算入口
# ──────────────────────────────────────────────────────────────────────


def _extract_nat(parsed: dict) -> Optional[int]:
    raw = parsed.get("system", {}).get("nat")
    if raw is None:
        return None
    try:
        return int(raw.strip().rstrip(","))
    except (ValueError, AttributeError):
        return None


def _extract_ecutwfc(parsed: dict) -> Optional[float]:
    raw = parsed.get("system", {}).get("ecutwfc")
    if raw is None:
        return None
    return _fortran_to_float(raw)


def _extract_calculation(parsed: dict) -> str:
    raw = parsed.get("control", {}).get("calculation", "")
    if not raw:
        return ""
    return raw.strip().strip("'\"").lower()


def estimate_wall_s(input_path: Path) -> Optional[int]:
    """读 pw.x 输入文件，返回估算 wall_s（已含 safety），None 表示无法估算。

    任何 parse 失败都返回 None —— BashTool 拿到 None 就不抬 timeout。
    """
    info = _gather_info(input_path)
    if info is None:
        return None
    return info["estimated_wall_s"]


def _gather_info(input_path: Path) -> Optional[dict]:
    """内部：返回带 debug 字段的完整 dict，给 estimate_for_command 用。
    None 表示参数不全或 parse 失败。
    """
    try:
        text = Path(input_path).read_text(errors="replace")
    except (OSError, UnicodeError):
        return None

    parsed = _parse_input(text)
    nat = _extract_nat(parsed)
    ecut = _extract_ecutwfc(parsed)
    nk = _extract_n_kpoints(text)
    calc = _extract_calculation(parsed)
    if nat is None or ecut is None or nk is None:
        return None
    if nat <= 0 or ecut <= 0 or nk <= 0:
        return None

    mult = _ITER_MULTIPLIER.get(calc, _DEFAULT_ITER_MULTIPLIER)
    ecut_factor = (ecut / 40.0) ** 1.5
    raw_s = _FORMULA_C * (nat ** 2) * nk * ecut_factor * mult
    est_s = int(round(raw_s * _SAFETY_FACTOR))
    # 下界：再小也至少给 30s（avoid 0s 报告）
    est_s = max(est_s, 30)
    return {
        "n_atoms": nat,
        "n_kpoints": nk,
        "ecutwfc": ecut,
        "calculation": calc or "(unknown)",
        "iter_multiplier": mult,
        "estimated_wall_s": est_s,
    }


def estimate_for_command(cmd: str, cwd: Path) -> tuple[Optional[int], dict]:
    """命令级入口。返回 (estimated_wall_s, debug_info)。

    BashTool.run() 直接调用：
        est, dbg = estimate_for_command(cmd, self.cwd)
        if est is not None and est > timeout:
            timeout = est
            note = f"[pw_wall_estimator] timeout_s raised → {est}s based on {dbg}..."

    Returns:
        (None, {})  if cmd is not pw.x, .in not found, or parse fails
        (int, dict) otherwise
    """
    if not is_pw_x_invocation(cmd):
        return None, {}
    inp = find_pw_input(cmd, cwd)
    if inp is None:
        return None, {}
    info = _gather_info(inp)
    if info is None:
        return None, {}
    raw_est = info["estimated_wall_s"]
    # FIX-15 / A3: cap so the bash watchdog fires inside the harness wall
    # (the cap scales to the wall budget when SOLVER_HARNESS_WALL_S is known).
    cap = _max_estimate_sec()
    if raw_est > cap:
        info["uncapped_estimate_s"] = raw_est
        info["estimated_wall_s"] = cap
        info["capped"] = True
    return info["estimated_wall_s"], info
