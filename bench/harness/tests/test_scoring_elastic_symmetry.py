"""P1: T4 Cij 打分 rule=elastic_symmetry_reduced。

设计(2026-06-25 修正后):
  - **分数 = Mandel/Kelvin 归一化特征值的相对 L2 距离**(真旋转不变量)。旧的"lab-frame
    逐独立常数"做法非旋转不变(实测六方 Be 绕轴 30° → 0.49),已退为**仅诊断**。
  - space_group_number → Laue 类:投影出独立常数(cubic C11/C12/C44; hex +C13/C33; …)只
    放进 meta["constants"] 供人看"哪个常数偏",**不参与打分**。无 spg/不支持的晶系也照常
    用 Mandel 打分。
  - 提交张量**非正定**(min Mandel eig<0)而 GT 稳定 → mode=`sign_convention_error`、score 0
    (多半是应力符号未翻;给个明确标签而非泛化 0)。
打分曲线沿用 _graded_score(满分 err≤0.3·tol、零分 err≥tol)。
"""
import math

import pytest

from harness.lib.scoring import score_fields


# ───────────────────────── helpers ─────────────────────────
def _scoring(field: str, partial: bool = True) -> dict:
    return {"aggregate": "all_fields_weighted",
            "field_weights": {field: 1.0}, "partial_credit": partial}


def _gt(value, tol: float = 0.08) -> dict:
    return {"value": value, "rule": "elastic_symmetry_reduced",
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


def _hex(c11, c33, c12, c13, c44):
    c66 = (c11 - c12) / 2.0
    C = [[0.0] * 6 for _ in range(6)]
    C[0][0] = C[1][1] = float(c11); C[2][2] = float(c33)
    C[0][1] = C[1][0] = float(c12)
    C[0][2] = C[2][0] = C[1][2] = C[2][1] = float(c13)
    C[3][3] = C[4][4] = float(c44); C[5][5] = float(c66)
    return C


def _rotate_voigt(C, R):
    """按物理 3D 旋转 R∈SO(3)(Bond 变换)旋转 6×6 Voigt 张量。"""
    import numpy as np
    vm = [(0, 0), (1, 1), (2, 2), (1, 2), (0, 2), (0, 1)]
    T = np.zeros((3, 3, 3, 3))
    for I, (i, j) in enumerate(vm):
        for J, (k, l) in enumerate(vm):
            for a, b in {(i, j), (j, i)}:
                for c, d in {(k, l), (l, k)}:
                    T[a, b, c, d] = C[I][J]
    Tr = np.einsum("ai,bj,ck,dl,ijkl->abcd", R, R, R, R, T)
    out = np.zeros((6, 6))
    for I, (i, j) in enumerate(vm):
        for J, (k, l) in enumerate(vm):
            out[I, J] = Tr[i, j, k, l]
    return out.tolist()


def _tilt(deg):
    """绕 y 轴转(会把 c 轴混进基面,对六方是"真"旋转)。"""
    import numpy as np
    t = math.radians(deg); c, s = math.cos(t), math.sin(t)
    return np.array([[c, 0.0, s], [0.0, 1.0, 0.0], [-s, 0.0, c]])


# ───────────────────── 分数 = Mandel 不变量 ─────────────────────
def test_identical_full_mark():
    base = _cubic(200, 120, 100)
    r = score_fields({"cij": _gt(base)}, {"cij": base}, _scoring("cij"), space_group_number=225)
    assert r["per_field_scores"]["cij"] == pytest.approx(1.0, abs=1e-9)
    assert r["per_field_meta"]["cij"]["mode"] == "mandel_invariant"


def test_graded_decay_on_mandel_rel():
    """整体缩放 (1+δ) → Mandel 特征值同比缩放 → rel=δ;δ=0.024 满分、0.08 零分。"""
    base = _cubic(200.0, 120.0, 100.0)
    for delta, expected in [(0.024, 1.0), (0.08, 0.0)]:
        sub = [[v * (1 + delta) for v in row] for row in base]
        r = score_fields({"cij": _gt(base)}, {"cij": sub}, _scoring("cij"), space_group_number=225)
        assert r["per_field_scores"]["cij"] == pytest.approx(expected, abs=1e-3), f"δ={delta}"


def test_score_invariant_under_physical_rotation_hexagonal():
    """**核心修复**:六方 Be 绕轴转 30°(真 3D 旋转),分数必须不变 → 1.0。
    旧 per_constant 路在这里掉到 ~0.49,这正是改成 Mandel 打分的理由。"""
    base = _hex(c11=294.0, c33=357.0, c12=27.0, c13=14.0, c44=162.0)   # Be 量级
    rotated = _rotate_voigt(base, _tilt(30.0))
    r = score_fields({"cij": _gt(base)}, {"cij": rotated}, _scoring("cij"), space_group_number=194)
    assert r["per_field_scores"]["cij"] == pytest.approx(1.0, abs=1e-6)


def test_score_invariant_under_physical_rotation_cubic():
    base = _cubic(347.0, 251.0, 77.0)   # Pt 量级
    rotated = _rotate_voigt(base, _tilt(37.0))
    r = score_fields({"cij": _gt(base)}, {"cij": rotated}, _scoring("cij"), space_group_number=225)
    assert r["per_field_scores"]["cij"] == pytest.approx(1.0, abs=1e-6)


# ───────────────────── 符号翻转特判 ─────────────────────
def test_sign_flipped_born_unstable_and_flagged():
    """提交 = −GT(全负 = 整体翻号,应力符号没翻):score 闸到 0,且 born_unstable=True、
    sign_convention_suspected=True(与 matrix_eigenvalue_relative 同口径)。不是泛化 0——
    让"删了 prompt 符号句"那类回归在 report 里一眼可见。"""
    gt = _cubic(200.0, 120.0, 100.0)
    flipped = [[-v for v in row] for row in gt]
    r = score_fields({"cij": _gt(gt)}, {"cij": flipped}, _scoring("cij"), space_group_number=225)
    assert r["per_field_scores"]["cij"] == 0.0
    meta = r["per_field_meta"]["cij"]
    assert meta["born_unstable"] is True
    assert meta["sign_convention_suspected"] is True


# ───────────────────── 诊断:per-constant 仍可读,但不打分 ─────────────────────
def test_spurious_offdiagonal_high_score_and_clean_diagnostic():
    """杂散 C16/C45(破对称噪声):Mandel 分数仍很高(噪声小);且诊断 constants 完全不被
    污染(投影忽略 off-diagonal)→ C11/C12/C44 诊断 rel≈0。"""
    base = _cubic(200, 120, 100)
    noisy = [row[:] for row in base]
    noisy[0][5] = noisy[5][0] = 8.0
    noisy[3][4] = noisy[4][3] = -6.0
    r = score_fields({"cij": _gt(base)}, {"cij": noisy}, _scoring("cij"), space_group_number=225)
    # Mandel 分数会**如实**看见杂散 off-diagonal(它本就是相对 GT 的真实偏差),故高但非满分。
    assert r["per_field_scores"]["cij"] > 0.85
    # 而逐常数诊断完全不被 off-diagonal 污染(投影只读对称允许的分量)→ 精确定位"哪个常数偏"。
    cs = r["per_field_meta"]["cij"]["constants"]
    for name in ("C11", "C12", "C44"):
        assert cs[name]["rel"] < 1e-9, name


def test_wrong_C44_partial_and_diagnostic():
    """C44 偏 30%:Mandel 分数落部分分区间;诊断 constants 指出 C44 偏 ~30%、C11/C12 不偏。"""
    gt = _cubic(108.0, 64.0, 30.0)
    sub = _cubic(108.0, 64.0, 30.0 * 1.30)
    r = score_fields({"cij": _gt(gt, tol=0.15)}, {"cij": sub}, _scoring("cij"), space_group_number=225)
    assert 0.0 < r["per_field_scores"]["cij"] < 1.0
    cs = r["per_field_meta"]["cij"]["constants"]
    assert cs["C44"]["rel"] == pytest.approx(0.30, abs=1e-6)
    assert cs["C11"]["rel"] < 1e-9 and cs["C12"]["rel"] < 1e-9


def test_hexagonal_diagnostic_constants_present():
    gt = _hex(c11=160.0, c33=180.0, c12=60.0, c13=50.0, c44=45.0)
    r = score_fields({"cij": _gt(gt)}, {"cij": gt}, _scoring("cij"), space_group_number=194)
    assert r["per_field_scores"]["cij"] == pytest.approx(1.0, abs=1e-9)
    meta = r["per_field_meta"]["cij"]
    assert meta["laue"] == "hexagonal"
    assert set(meta["constants"]) >= {"C11", "C33", "C12", "C13", "C44"}


# ───────────────────── 无 spg / 防御 ─────────────────────
def test_no_spacegroup_still_scores_invariant():
    base = _cubic(200.0, 120.0, 100.0)
    r = score_fields({"cij": _gt(base)}, {"cij": base}, _scoring("cij"), space_group_number=None)
    assert r["per_field_scores"]["cij"] == pytest.approx(1.0, abs=1e-9)
    assert r["per_field_meta"]["cij"]["mode"] == "mandel_invariant"
    assert "constants" not in r["per_field_meta"]["cij"]   # 无 laue → 无逐常数诊断


def test_wrong_shape_scores_zero():
    gt = _cubic(200, 120, 100)
    with pytest.warns(UserWarning, match="6×6"):
        r = score_fields({"cij": _gt(gt)}, {"cij": [[1.0, 2.0, 3.0]]},
                         _scoring("cij"), space_group_number=225)
    assert r["per_field_scores"]["cij"] == 0.0


def test_nan_scores_zero():
    gt = _cubic(200, 120, 100)
    bad = [[float("nan")] * 6 for _ in range(6)]
    with pytest.warns(UserWarning, match="NaN"):
        r = score_fields({"cij": _gt(gt)}, {"cij": bad}, _scoring("cij"), space_group_number=225)
    assert r["per_field_scores"]["cij"] == 0.0


# ───────────────────── 对照 + 真实回归 ─────────────────────
def test_old_eigenvalue_rule_penalizes_same_input():
    """对照:旧 matrix_eigenvalue_relative(原始 Voigt 特征值)对杂散 off-diagonal 会 <1。"""
    base = _cubic(200, 120, 100)
    noisy = [row[:] for row in base]
    noisy[0][5] = noisy[5][0] = 20.0
    noisy[3][4] = noisy[4][3] = -15.0
    old = {"cij": {"value": base, "rule": "matrix_eigenvalue_relative",
                   "tolerance": 0.08, "unit": "GPa"}}
    r = score_fields(old, {"cij": noisy}, _scoring("cij"))
    assert r["per_field_scores"]["cij"] < 1.0


def test_real_si_axis_permutation_full_mark():
    """真实 M1_T4_Si:GT 与 v1 提交是同一立方张量、anisotropy 项落在不同剪切轴(90° 轴
    置换)。Mandel 特征值在置换下不变 → 满分。spg=227。"""
    gt_cij = [
        [179.32, 39.2, 47.97, 0.0, 0.0, -12.36],
        [39.2, 188.06, 39.2, 0.0, 0.0, 0.0],
        [47.97, 39.2, 179.36, 0.0, 0.0, 12.35],
        [0.0, 0.0, 0.0, 57.0, 12.4, 0.0],
        [0.0, 0.0, 0.0, 12.4, 65.72, 0.0],
        [-12.36, 0.0, 12.35, 0.0, 0.0, 57.0],
    ]
    ext_cij = [
        [179.64, 48.02, 39.34, 12.33, 0.0, 0.0],
        [48.02, 179.56, 39.33, -12.31, 0.0, 0.0],
        [39.34, 39.33, 188.28, 0.0, 0.0, 0.0],
        [12.33, -12.31, 0.0, 57.1, 0.0, 0.0],
        [0.0, 0.0, 0.0, 0.0, 57.12, 12.34],
        [0.0, 0.0, 0.0, 0.0, 12.34, 65.8],
    ]
    r = score_fields({"cij": _gt(gt_cij)}, {"cij": ext_cij}, _scoring("cij"), space_group_number=227)
    assert r["per_field_scores"]["cij"] == pytest.approx(1.0, abs=1e-3)


def test_wiring_real_task_spacegroup_drives_scoring():
    """run.py / rerun_eval.py 用 task['material']['space_group_number'] 调 score_fields。
    跑通真实链路 + GT 自洽(GT 对自身必 1.0 = CI 闸)。"""
    from pathlib import Path

    from harness.lib.task_loader import load_task

    bench_root = Path(__file__).resolve().parents[2]
    task = load_task(bench_root, "M1_T4_NaCl_elastic")
    spg = task.get("material", {}).get("space_group_number")
    assert spg == 225
    gt_cij = task["ground_truth"]["elastic_tensor_cij_gpa"]["value"]
    gt = {"cij": {"value": gt_cij, "rule": "elastic_symmetry_reduced", "tolerance": 0.08}}
    r = score_fields(gt, {"cij": gt_cij}, _scoring("cij"), space_group_number=spg)
    assert r["per_field_meta"]["cij"]["mode"] == "mandel_invariant"
    assert r["per_field_meta"]["cij"]["laue"] == "cubic"
    assert r["per_field_scores"]["cij"] == pytest.approx(1.0, abs=1e-9)
