"""T4 符号约定防御(2026-06-23,fix #1)。

背景:`692473a`("采纳杨博 freed-grid prompt")重写 elastic_stiffness.txt 时,顺手删掉了
唯一一句应力符号指引("positive σ = tension; if QE prints negative-of-stress, negate
accordingly")。QE 应力反号,没这句 → v1/deepseek 照搬原值 → 整张张量负定(Born 不稳)→
打分 0。同一 agent 有这句(06-08)均分 0.769、没这句(06-19)均分 0.205/34 零分/32 负定。

两道防线:
  1. prompt 把符号约定句加回(本文件 test_elastic_prompt_states_stress_sign_convention)。
  2. 打分器 born 稳定性特判:提交负定/含负特征值 → 标 born_unstable(全负另标
     sign_convention_suspected)并把分数闸到 0,让"符号 bug"在 report 里一眼可见,
     而不是和"张量彻底算错"长成一样的不明 0,也防止"碰巧排序特征值接近"的不稳张量蒙混。
"""
from pathlib import Path

import pytest

from harness.lib.scoring import score_fields

_BENCH_ROOT = Path(__file__).resolve().parents[2]


# ───────────────────────── helpers ─────────────────────────
def _scoring(field: str, partial: bool = True) -> dict:
    return {"aggregate": "all_fields_weighted",
            "field_weights": {field: 1.0}, "partial_credit": partial}


def _eig_gt(value, tol: float = 0.08) -> dict:
    return {"value": value, "rule": "matrix_eigenvalue_relative",
            "tolerance": tol, "unit": "GPa"}


def _cubic(c11, c12, c44):
    C = [[0.0] * 6 for _ in range(6)]
    for i in range(3):
        C[i][i] = float(c11)
    for i in range(3):
        for j in range(3):
            if i != j:
                C[i][j] = float(c12)
    for i in range(3, 6):
        C[i][i] = float(c44)
    return C


def _diag(vals):
    """对角 6×6(其特征值即 vals);用于精确构造稳/不稳特征谱。"""
    C = [[0.0] * 6 for _ in range(6)]
    for i, v in enumerate(vals):
        C[i][i] = float(v)
    return C


# ─────────────── 打分器:born 稳定性特判(active rule) ───────────────
def test_sign_flipped_submission_is_flagged_and_zeroed():
    """提交 = −GT(整体符号翻转,负定)。必须:① 分数 0 ② born_unstable=True
    ③ sign_convention_suspected=True(全负 = QE 应力没取负的特征签名)。"""
    gt = _cubic(200.0, 120.0, 100.0)            # 稳定立方,正定
    flipped = [[-v for v in row] for row in gt]   # 负定
    r = score_fields({"cij": _eig_gt(gt)}, {"cij": flipped}, _scoring("cij"))
    assert r["per_field_scores"]["cij"] == 0.0
    meta = r["per_field_meta"]["cij"]
    assert meta["born_unstable"] is True
    assert meta["sign_convention_suspected"] is True


def test_stable_correct_submission_not_flagged():
    """正确稳定提交:满分,两面 born 旗都为 False(不能误伤物理合法答案)。"""
    gt = _cubic(180.0, 50.0, 90.0)
    r = score_fields({"cij": _eig_gt(gt)}, {"cij": gt}, _scoring("cij"))
    assert r["per_field_scores"]["cij"] == pytest.approx(1.0, abs=1e-9)
    meta = r["per_field_meta"]["cij"]
    assert meta["born_unstable"] is False
    assert meta["sign_convention_suspected"] is False


def test_partially_unstable_submission_zeroed_despite_near_eigenvalues():
    """单个负特征值(其余与 GT 几乎一致):不闸分的话排序特征值 L2 仅 ~2.5% → ~0.99 高分,
    但张量 Born 不稳(不是稳定材料的合法刚度)。born 闸把它归 0;sign_convention_suspected
    为 False(只是局部失稳,不是整体翻号)。这就是"特判而非只贴标签"的价值。"""
    gt = _diag([2.0, 60.0, 60.0, 80.0, 80.0, 80.0])      # 全正 → 稳定
    sub = _diag([-2.0, 60.0, 60.0, 80.0, 80.0, 80.0])     # 一个模式翻负 → 不稳
    r = score_fields({"cij": _eig_gt(gt, tol=0.08)}, {"cij": sub}, _scoring("cij"))
    assert r["per_field_scores"]["cij"] == 0.0
    meta = r["per_field_meta"]["cij"]
    assert meta["born_unstable"] is True
    assert meta["sign_convention_suspected"] is False


# ─────────────── prompt:符号约定句必须在场(防 692473a 再发生) ───────────────
def test_elastic_prompt_states_stress_sign_convention():
    """渲染真实 M1_T4_Si_elastic 的 prompt(agent 实际收到的文本),断言它传达了应力
    符号约定(tension-positive / 对 QE 输出取负)。这正是会拦下 692473a 的回归闸。"""
    from harness.lib.task_loader import load_task, render_prompt

    task = load_task(_BENCH_ROOT, "M1_T4_Si_elastic")
    prompt = render_prompt(_BENCH_ROOT, task).lower()
    assert "sign" in prompt and "tension" in prompt, \
        "prompt 未声明应力符号约定(tension-positive)"
    assert ("negate" in prompt or "negative-of-stress" in prompt), \
        "prompt 未提示对 QE 反号应力取负"
