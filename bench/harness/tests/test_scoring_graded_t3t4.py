"""T3/T4 收紧打分曲线（2026-06-12 shijie + DFT 专家决议）。

统一曲线（ratio = 0.3）：满分带 err ≤ 0.3·tol；零分 err ≥ tol；中间线性。
tol 在这里语义是"零分线"（旧 `relative` 是"满分线"，故此处更严）。

适用：
  - 新规则 `graded_relative`         （T3 v0/b0、T4 K/G，从旧 `relative` 切过来）
  - `matrix_frobenius_relative`      （T4 Cij，原 full≤tol/zero≥2tol → 新曲线）
  - `eos_curve_rmse_mev_per_atom`    （T3 eos 曲线）
  - `stress_curve_rmse_gpa`          （T4 stress 曲线）

不动：`relative` / `absolute`（T1/T2 共享；b0' 保持宽松）。
"""
import pytest

from harness.lib.scoring import score_fields


def _scoring(field: str, partial: bool = True) -> dict:
    return {
        "aggregate": "all_fields_weighted",
        "field_weights": {field: 1.0},
        "partial_credit": partial,
    }


# ============ graded_relative：标量相对误差，满分=0.3·tol、零分=tol ============


def _gt_graded_rel(value: float, tol: float = 0.1) -> dict:
    return {"value": value, "rule": "graded_relative", "tolerance": tol, "unit": "GPa"}


def test_graded_relative_full_mark_floor():
    """相对误差 ≤ 0.3·tol → 1.0（tol=0.1 → 满分带 3%）。"""
    gt = {"b0": _gt_graded_rel(100.0, tol=0.1)}
    for v in [100.0, 101.5, 103.0]:  # err 0%, 1.5%, 3.0%
        r = score_fields(gt, {"b0": v}, _scoring("b0"))
        assert r["per_field_scores"]["b0"] == pytest.approx(1.0, abs=1e-9), f"v={v}"


def test_graded_relative_decay_band():
    """0.3·tol < err < tol 线性衰减；err=0.65·tol 是半分点。"""
    gt = {"b0": _gt_graded_rel(100.0, tol=0.1)}
    cases = [(105.0, 0.7142857), (106.5, 0.5), (108.0, 0.2857143)]  # err 5%,6.5%,8%
    for v, expected in cases:
        r = score_fields(gt, {"b0": v}, _scoring("b0"))
        assert r["per_field_scores"]["b0"] == pytest.approx(expected, abs=1e-4), f"v={v}"


def test_graded_relative_hard_zero_at_tol():
    """err = tol = 10% → 0.0（硬归零，不像旧 relative 在 [tol,2tol] 还有尾巴）。"""
    gt = {"b0": _gt_graded_rel(100.0, tol=0.1)}
    for v in [110.0, 120.0, 200.0]:  # err 10%, 20%, 100%
        r = score_fields(gt, {"b0": v}, _scoring("b0"))
        assert r["per_field_scores"]["b0"] == pytest.approx(0.0, abs=1e-9), f"v={v}"


def test_graded_relative_partial_false_is_boolean():
    """partial=False 退化为 boolean：err ≤ 0.3·tol → 1.0，否则 0.0。"""
    gt = {"b0": _gt_graded_rel(100.0, tol=0.1)}
    r = score_fields(gt, {"b0": 102.0}, _scoring("b0", partial=False))  # err 2% < 3%
    assert r["per_field_scores"]["b0"] == pytest.approx(1.0, abs=1e-9)
    r = score_fields(gt, {"b0": 105.0}, _scoring("b0", partial=False))  # err 5% in decay
    assert r["per_field_scores"]["b0"] == pytest.approx(0.0, abs=1e-9)


def test_graded_relative_tight_v0_field():
    """V0 字段 tol=0.01：满分带收到 0.3%，零分线 1%（按比例同步收紧）。"""
    gt = {"v0": _gt_graded_rel(46.0, tol=0.01)}
    r = score_fields(gt, {"v0": 46.0 * 1.002}, _scoring("v0"))  # 0.2% → 满分
    assert r["per_field_scores"]["v0"] == pytest.approx(1.0, abs=1e-9)
    r = score_fields(gt, {"v0": 46.0 * 1.01}, _scoring("v0"))   # 1.0% → 零分
    assert r["per_field_scores"]["v0"] == pytest.approx(0.0, abs=1e-9)


# ============ matrix_frobenius_relative：Cij 矩阵，换成同一条曲线 ============


def _gt_cij(scale: float = 100.0, tol: float = 0.08) -> tuple[dict, list]:
    base = [[0.0] * 6 for _ in range(6)]
    for i in range(6):
        base[i][i] = scale
    gt = {"value": base, "rule": "matrix_frobenius_relative", "tolerance": tol,
          "unit": "GPa"}
    return gt, base


def _scaled_matrix(base: list, factor: float) -> list:
    # ext = base*(1+δ) → ‖diff‖/‖base‖ = δ
    return [[x * factor for x in row] for row in base]


def test_matrix_frobenius_full_mark_floor():
    """frobenius 相对误差 ≤ 0.3·tol (=2.4%) → 1.0。"""
    gt, base = _gt_cij(tol=0.08)
    for delta in [0.0, 0.024]:
        ext = {"cij": _scaled_matrix(base, 1.0 + delta)}
        r = score_fields({"cij": gt}, ext, _scoring("cij"))
        assert r["per_field_scores"]["cij"] == pytest.approx(1.0, abs=1e-6), f"δ={delta}"


def test_matrix_frobenius_decay_and_zero():
    """decay 段半分点 δ=0.052；δ≥tol=0.08 → 0。"""
    gt, base = _gt_cij(tol=0.08)
    r = score_fields({"cij": gt}, {"cij": _scaled_matrix(base, 1.052)}, _scoring("cij"))
    assert r["per_field_scores"]["cij"] == pytest.approx(0.5, abs=1e-3)
    r = score_fields({"cij": gt}, {"cij": _scaled_matrix(base, 1.08)}, _scoring("cij"))
    assert r["per_field_scores"]["cij"] == pytest.approx(0.0, abs=1e-6)


def test_matrix_frobenius_old_curve_would_pass_now_decays():
    """回归：δ=5% 在旧曲线(full≤8%)是满分，新曲线必须 < 1（区分度回来了）。"""
    gt, base = _gt_cij(tol=0.08)
    r = score_fields({"cij": gt}, {"cij": _scaled_matrix(base, 1.05)}, _scoring("cij"))
    got = r["per_field_scores"]["cij"]
    assert 0.0 < got < 1.0
    assert got == pytest.approx(0.5357143, abs=1e-4)


# ============ eos_curve_rmse_mev_per_atom：曲线跟着收紧 ============


def _write_2col(tmp_path, name, col1):
    p = tmp_path / name
    p.write_text("\n".join(f"{i} {v}" for i, v in enumerate(col1)) + "\n")
    return p


def _gt_eos_curve(ref: str, tol: float = 5.0) -> dict:
    return {"value": {"ref_file": ref}, "rule": "eos_curve_rmse_mev_per_atom",
            "tolerance": tol, "unit": "meV/atom"}


def test_eos_curve_rmse_full_floor_and_zero(tmp_path):
    """gt=[0,0,0,0]，ext=[0,0,0,d] → rmse=d/2。tol=5 → full≤1.5、zero≥5。"""
    ref = _write_2col(tmp_path, "eos_ref.csv", [0.0, 0.0, 0.0, 0.0])
    gt = {"curve": _gt_eos_curve(str(ref), tol=5.0)}
    # rmse=1.5 (d=3) → 满分
    r = score_fields(gt, {"curve": [0.0, 0.0, 0.0, 3.0]}, _scoring("curve"))
    assert r["per_field_scores"]["curve"] == pytest.approx(1.0, abs=1e-6)
    # rmse=5.0 (d=10) → 零分
    r = score_fields(gt, {"curve": [0.0, 0.0, 0.0, 10.0]}, _scoring("curve"))
    assert r["per_field_scores"]["curve"] == pytest.approx(0.0, abs=1e-6)


def test_eos_curve_rmse_decay_halfpoint(tmp_path):
    """rmse=3.25 (d=6.5) = (1.5,5) 半分点 → 0.5（旧曲线此处=1.0）。"""
    ref = _write_2col(tmp_path, "eos_ref.csv", [0.0, 0.0, 0.0, 0.0])
    gt = {"curve": _gt_eos_curve(str(ref), tol=5.0)}
    r = score_fields(gt, {"curve": [0.0, 0.0, 0.0, 6.5]}, _scoring("curve"))
    assert r["per_field_scores"]["curve"] == pytest.approx(0.5, abs=1e-3)


# ============ stress_curve_rmse_gpa：曲线跟着收紧 ============


def _write_stress_ref(tmp_path, name, rows_sigma):
    p = tmp_path / name
    lines = ["mode,amplitude,s1,s2,s3,s4,s5,s6"]
    for i, sig in enumerate(rows_sigma):
        lines.append("e1,-0.01," + ",".join(str(x) for x in sig))
    p.write_text("\n".join(lines) + "\n")
    return p


def _gt_stress(ref: str, tol: float = 1.0) -> dict:
    return {"value": {"ref_file": ref}, "rule": "stress_curve_rmse_gpa",
            "tolerance": tol, "unit": "GPa"}


def test_stress_curve_rmse_full_floor_and_zero(tmp_path):
    """1 行 σ=0；ext=[0,0,0,0,0,d] → rmse=d/√6。tol=1 → full≤0.3、zero≥1。"""
    ref = _write_stress_ref(tmp_path, "ss_ref.csv", [[0, 0, 0, 0, 0, 0]])
    gt = {"ss": _gt_stress(str(ref), tol=1.0)}
    import math
    d_full = 0.3 * math.sqrt(6)   # rmse=0.3
    d_zero = 1.0 * math.sqrt(6)   # rmse=1.0
    r = score_fields(gt, {"ss": [0, 0, 0, 0, 0, d_full]}, _scoring("ss"))
    assert r["per_field_scores"]["ss"] == pytest.approx(1.0, abs=1e-6)
    r = score_fields(gt, {"ss": [0, 0, 0, 0, 0, d_zero]}, _scoring("ss"))
    assert r["per_field_scores"]["ss"] == pytest.approx(0.0, abs=1e-6)


def test_stress_curve_rmse_decay_halfpoint(tmp_path):
    """rmse=0.65 = (0.3,1.0) 半分点 → 0.5（旧曲线此处=1.0）。"""
    import math
    ref = _write_stress_ref(tmp_path, "ss_ref.csv", [[0, 0, 0, 0, 0, 0]])
    gt = {"ss": _gt_stress(str(ref), tol=1.0)}
    r = score_fields(gt, {"ss": [0, 0, 0, 0, 0, 0.65 * math.sqrt(6)]}, _scoring("ss"))
    assert r["per_field_scores"]["ss"] == pytest.approx(0.5, abs=1e-3)


# ============ matrix_eigenvalue_relative：Cij 改用旋转不变的特征值比较 ============
#
# raw 6×6 Voigt Frobenius 随原胞取向变化（GT/solver 取向不同 → 同张量差 10–50%），
# 但弹性张量特征值（Kelvin 模量）是旋转不变量。这里比排序后特征值的相对 L2 距离，
# 套同一条收紧曲线（满分 0.3·tol、零分 tol）。tol 默认 0.08。


def _givens(n, i, j, theta):
    import numpy as np
    g = np.eye(n)
    c, s = np.cos(theta), np.sin(theta)
    g[i, i] = c; g[j, j] = c; g[i, j] = -s; g[j, i] = s
    return g


def _gt_eig(value, tol=0.08) -> dict:
    return {"value": value, "rule": "matrix_eigenvalue_relative",
            "tolerance": tol, "unit": "GPa"}


def test_eigenvalue_invariant_under_frame_rotation():
    """同一张量、不同取向（正交相似 C'=Q·C·Qᵀ）：特征值不变 → 1.0，
    哪怕 raw Frobenius 差得很大（这正是换特征值的全部理由）。"""
    import numpy as np
    base = np.diag([200.0, 180.0, 170.0, 60.0, 55.0, 50.0])
    Q = _givens(6, 0, 5, 0.5) @ _givens(6, 1, 4, 0.4) @ _givens(6, 2, 3, 0.3)
    rotated = (Q @ base @ Q.T)
    frob_rel = np.linalg.norm(rotated - base) / np.linalg.norm(base)
    assert frob_rel >= 0.08, f"测试前提：frobenius 必须够大才有意义，实测 {frob_rel:.3f}"
    gt = {"cij": _gt_eig(base.tolist(), tol=0.08)}
    r = score_fields(gt, {"cij": rotated.tolist()}, _scoring("cij"))
    assert r["per_field_scores"]["cij"] == pytest.approx(1.0, abs=1e-6)


def test_eigenvalue_identical_full_mark():
    import numpy as np
    base = np.diag([200.0, 180.0, 170.0, 60.0, 55.0, 50.0]).tolist()
    gt = {"cij": _gt_eig(base, tol=0.08)}
    r = score_fields(gt, {"cij": base}, _scoring("cij"))
    assert r["per_field_scores"]["cij"] == pytest.approx(1.0, abs=1e-9)


def test_eigenvalue_graded_decay_and_zero():
    """ext = base·(1+δ) → 特征值整体缩放 → eig 相对差 = δ。
    δ=0.024 满分、δ=0.052 半分、δ=0.08 零分。"""
    import numpy as np
    base = np.diag([200.0, 180.0, 170.0, 60.0, 55.0, 50.0])
    gt = {"cij": _gt_eig(base.tolist(), tol=0.08)}
    for delta, expected in [(0.024, 1.0), (0.052, 0.5), (0.08, 0.0)]:
        r = score_fields(gt, {"cij": (base * (1 + delta)).tolist()}, _scoring("cij"))
        assert r["per_field_scores"]["cij"] == pytest.approx(expected, abs=1e-3), f"δ={delta}"


def test_eigenvalue_pathological_matrix_scores_zero():
    """含 NaN/inf 的病态矩阵（10 个真 run 的 eigvalsh 不收敛）→ 0 分，不崩。"""
    import numpy as np
    base = np.diag([200.0, 180.0, 170.0, 60.0, 55.0, 50.0]).tolist()
    gt = {"cij": _gt_eig(base, tol=0.08)}
    bad = [[float("nan")] * 6 for _ in range(6)]
    r = score_fields(gt, {"cij": bad}, _scoring("cij"))
    assert r["per_field_scores"]["cij"] == 0.0


def test_eigenvalue_wrong_shape_scores_zero():
    base = [[1.0] * 6 for _ in range(6)]
    gt = {"cij": _gt_eig(base, tol=0.08)}
    r = score_fields(gt, {"cij": [[1.0, 2.0, 3.0]]}, _scoring("cij"))
    assert r["per_field_scores"]["cij"] == 0.0


def test_eigenvalue_real_si_orientation_case():
    """真实回归：M1_T4_Si v1 run 的 extracted Cij vs GT Cij。
    raw Frobenius ≈14%（旧 0.15 险过、0.08 必挂），但取向不同、特征值差 0.16% → 满分。"""
    gt_cij = [
        [179.32000000000005, 39.2, 47.970000000000006, 0.0, 0.0, -12.360000000000003],
        [39.2, 188.06, 39.2, 0.0, 0.0, 0.0],
        [47.970000000000006, 39.2, 179.36000000000004, 0.0, 0.0, 12.350000000000001],
        [0.0, 0.0, 0.0, 56.99999999999999, 12.400000000000002, 2.742838647325548e-17],
        [0.0, 0.0, 0.0, 12.400000000000002, 65.72000000000001, 0.0],
        [-12.360000000000003, 0.0, 12.350000000000001, 2.742838647325548e-17, 0.0, 57.0],
    ]
    ext_cij = [
        [179.64000000000007, 48.02000000000001, 39.34, 12.330000000000002, 0.0, 0.0],
        [48.02000000000001, 179.56, 39.330000000000005, -12.310000000000002, 0.0, 0.0],
        [39.34, 39.330000000000005, 188.28, 0.0, 0.0, 0.0],
        [12.330000000000002, -12.310000000000002, 0.0, 57.10000000000001, 0.0, 0.0],
        [0.0, 0.0, 0.0, 0.0, 57.120000000000005, 12.340000000000003],
        [0.0, 0.0, 0.0, 0.0, 12.340000000000003, 65.80000000000001],
    ]
    gt = {"cij": _gt_eig(gt_cij, tol=0.08)}
    r = score_fields(gt, {"cij": ext_cij}, _scoring("cij"))
    assert r["per_field_scores"]["cij"] == pytest.approx(1.0, abs=1e-6)
    assert r["per_field_meta"]["cij"]["eig_rel"] < 0.01
