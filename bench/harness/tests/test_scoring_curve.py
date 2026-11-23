"""curve_f1 rule 的单元测试（F1 / Dice 面积重叠评分，2026-05-17 决议）。

历史 curve_l2 测试在 S5 全量迁移到 curve_f1 后已删除（见
todo/bench/t2-dos-scoring-f1-overhaul.md S5）。
"""
import os
import pytest
from pathlib import Path

from harness.lib.scoring import score_fields


def _write_ref(tmp_path: Path, energies: list[float], dos: list[float]) -> Path:
    ref = tmp_path / "dos_reference.dat"
    lines = ["# E_eV(relative_to_EF)  DOS_states_per_eV_per_fu"]
    for e, d in zip(energies, dos):
        lines.append(f"{e:.6f}  {d:.6f}")
    ref.write_text("\n".join(lines) + "\n")
    return ref


def _scoring_dict(field: str = "dos_curve") -> dict:
    return {
        "aggregate": "all_fields_weighted",
        "field_weights": {field: 1.0},
        "partial_credit": True,
    }


# ============ curve_f1：F1 / Dice 面积重叠评分（2026-05-17 决议） ============


def _gt_curve_f1(ref_file: str, tolerance: float = 0.1) -> dict:
    """构造 curve_f1 GT 字段。tolerance 不使用，保留签名兼容。"""
    return {
        "value": {"ref_file": ref_file},
        "rule": "curve_f1",
        "tolerance": tolerance,
        "unit": "states/eV/fu",
    }


def test_curve_f1_perfect_match(tmp_path):
    """pred 与 GT 完全一致 → F1 = 1.0、P = R = 1.0。"""
    energies = [-2.0, -1.0, 0.0, 1.0, 2.0]
    dos = [0.5, 1.0, 0.0, 1.5, 2.0]
    ref = _write_ref(tmp_path, energies, dos)
    gt = {"dos_curve": _gt_curve_f1(str(ref))}
    extracted = {"dos_curve": {"energy_ev": list(energies), "dos": list(dos)}}
    result = score_fields(gt, extracted, _scoring_dict())
    assert result["per_field_scores"]["dos_curve"] == pytest.approx(1.0, abs=1e-9)
    meta = result["per_field_meta"]["dos_curve"]
    assert meta["precision"] == pytest.approx(1.0, abs=1e-9)
    assert meta["recall"] == pytest.approx(1.0, abs=1e-9)
    # 面积应 > 0
    assert meta["A_pred"] > 0
    assert meta["A_gt"] > 0
    assert meta["A_intersect"] == pytest.approx(meta["A_gt"], rel=1e-9)


def test_curve_f1_double_amplitude(tmp_path):
    """pred = 2 × GT（同 x，振幅翻倍）→ A_pred ≈ 2·A_gt, A_int ≈ A_gt
    → P ≈ 0.5, R ≈ 1.0, F1 ≈ 2/3 ≈ 0.667。"""
    energies = [-2.0, -1.0, 0.0, 1.0, 2.0]
    dos = [0.5, 1.0, 1.5, 1.0, 0.5]
    ref = _write_ref(tmp_path, energies, dos)
    pred_dos = [2 * d for d in dos]
    gt = {"dos_curve": _gt_curve_f1(str(ref))}
    extracted = {"dos_curve": {"energy_ev": list(energies), "dos": pred_dos}}
    result = score_fields(gt, extracted, _scoring_dict())
    meta = result["per_field_meta"]["dos_curve"]
    assert meta["precision"] == pytest.approx(0.5, abs=0.01)
    assert meta["recall"] == pytest.approx(1.0, abs=0.01)
    assert result["per_field_scores"]["dos_curve"] == pytest.approx(2.0 / 3.0, abs=0.01)


def test_curve_f1_half_range_pred(tmp_path):
    """pred 只覆盖 GT 一半 x 范围（GT [-5,5]、pred [-2.5,2.5]），重叠区 dos 完全匹配
    → P ≈ 1.0（pred 范围内积分 == 交集），R = pred 区面积 / GT 总面积 < 1，F1 < 1。"""
    # GT: flat dos=1.0 in [-5, 5]
    gt_energies = [float(e) for e in range(-5, 6)]  # -5..5
    gt_dos = [1.0] * len(gt_energies)
    ref = _write_ref(tmp_path, gt_energies, gt_dos)
    # pred: flat dos=1.0 但只在 [-2.5, 2.5]
    pred_energies = [-2.5, -1.5, -0.5, 0.5, 1.5, 2.5]
    pred_dos = [1.0] * len(pred_energies)
    gt = {"dos_curve": _gt_curve_f1(str(ref))}
    extracted = {"dos_curve": {"energy_ev": pred_energies, "dos": pred_dos}}
    result = score_fields(gt, extracted, _scoring_dict())
    meta = result["per_field_meta"]["dos_curve"]
    # P 应 ≈ 1.0：pred 范围内完全匹配
    assert meta["precision"] == pytest.approx(1.0, abs=0.05)
    # R < 1：pred 只覆盖部分 GT
    assert meta["recall"] < 0.95
    assert meta["recall"] > 0.3
    # F1 < 1
    assert result["per_field_scores"]["dos_curve"] < 1.0
    assert result["per_field_scores"]["dos_curve"] > 0.3


def test_curve_f1_all_zero_pred(tmp_path):
    """pred 全 0 → A_pred = 0 → score = 0, P = R = 0。"""
    energies = [-2.0, -1.0, 0.0, 1.0, 2.0]
    dos = [1.0, 1.0, 1.0, 1.0, 1.0]
    ref = _write_ref(tmp_path, energies, dos)
    gt = {"dos_curve": _gt_curve_f1(str(ref))}
    extracted = {"dos_curve": {"energy_ev": list(energies), "dos": [0.0] * len(energies)}}
    result = score_fields(gt, extracted, _scoring_dict())
    assert result["per_field_scores"]["dos_curve"] == 0.0
    meta = result["per_field_meta"]["dos_curve"]
    assert meta["precision"] == 0.0
    assert meta["recall"] == 0.0
    assert meta["A_pred"] == 0.0


def test_curve_f1_sn_real_data():
    """真实 Sn α Run 1 dos.dat（已减 EF）vs GT dos_reference.dat
    → F1 ≈ 0.949 ± 0.005。"""
    pred_path = Path(os.getenv("BENCH_WEB_RUNS_DIR", "runs_local")) / "2026-05-10T18-44-28Z_M1_T2_Sn_alpha_bands_dos/workdir/runs/sn_bands_dos/dos.dat"
    gt_path = Path(__file__).resolve().parents[2] / "questions/M1_T2" / "gt_data/M1_T2_Sn_alpha_bands_dos/dos_reference.dat"
    if not pred_path.exists() or not gt_path.exists():
        pytest.skip(f"real data missing: pred={pred_path.exists()} gt={gt_path.exists()}")

    # 读 pred 的 col0 (E rel EF) + col1 (dos)
    e_pred, y_pred = [], []
    for line in pred_path.read_text().splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        parts = s.split()
        if len(parts) < 2:
            continue
        try:
            e_pred.append(float(parts[0]))
            y_pred.append(float(parts[1]))
        except ValueError:
            continue

    gt = {"dos_curve": _gt_curve_f1(str(gt_path))}
    extracted = {"dos_curve": {"energy_ev": e_pred, "dos": y_pred}}
    result = score_fields(gt, extracted, _scoring_dict())
    score = result["per_field_scores"]["dos_curve"]
    meta = result["per_field_meta"]["dos_curve"]
    assert score == pytest.approx(0.949, abs=0.005), (
        f"Sn 真数据 F1 期望 ≈0.949，实际 {score:.4f} "
        f"(P={meta['precision']:.4f} R={meta['recall']:.4f})"
    )
    assert meta["precision"] == pytest.approx(0.949, abs=0.01)
    assert meta["recall"] == pytest.approx(0.949, abs=0.01)


# ============ graded_absolute：分级评分（议题 1, 2026-05-25 决议）============
#
# 曲线（tol=0.1）：
#   err=0.000 → 1.0      err=0.050 → 1.0  (地板边缘)
#   err=0.025 → 1.0      err=0.060 → 0.80
#   err=0.075 → 0.50     err=0.090 → 0.20
#   err=0.100 → 0.0      err=0.150 → 0.0  (硬归零)


def _gt_graded(value: float, tol: float = 0.1) -> dict:
    return {"value": value, "rule": "graded_absolute", "tolerance": tol,
            "unit": "eV"}


def _scoring_band_gap(partial: bool = True) -> dict:
    return {"aggregate": "all_fields_weighted",
            "field_weights": {"band_gap_ev": 1.0},
            "partial_credit": partial}


def test_graded_absolute_err_in_satin_floor():
    """err ∈ {0, 0.025, 0.05} 全在 satin floor → 1.0."""
    for err in [0.0, 0.025, 0.05]:
        gt = {"band_gap_ev": _gt_graded(0.0)}
        extracted = {"band_gap_ev": err}
        r = score_fields(gt, extracted, _scoring_band_gap())
        assert r["per_field_scores"]["band_gap_ev"] == pytest.approx(1.0, abs=1e-9), \
            f"err={err}: expected 1.0, got {r['per_field_scores']['band_gap_ev']}"


def test_graded_absolute_err_in_decay_band():
    """err ∈ (0.05, 0.10) 线性衰减；err=0.075 是半分点."""
    cases = [(0.06, 0.80), (0.075, 0.50), (0.09, 0.20)]
    for err, expected in cases:
        gt = {"band_gap_ev": _gt_graded(0.0)}
        extracted = {"band_gap_ev": err}
        r = score_fields(gt, extracted, _scoring_band_gap())
        got = r["per_field_scores"]["band_gap_ev"]
        assert got == pytest.approx(expected, abs=0.01), \
            f"err={err}: expected {expected}, got {got}"


def test_graded_absolute_err_at_hard_zero():
    """err = tol = 0.10 → 0.0（硬归零，与 absolute 的 [tol, 2*tol] 部分分尾巴不同）."""
    gt = {"band_gap_ev": _gt_graded(0.0)}
    extracted = {"band_gap_ev": 0.10}
    r = score_fields(gt, extracted, _scoring_band_gap())
    assert r["per_field_scores"]["band_gap_ev"] == pytest.approx(0.0, abs=1e-9)


def test_graded_absolute_err_beyond_tol():
    """err > tol → 0.0."""
    for err in [0.12, 0.15, 0.20, 1.0]:
        gt = {"band_gap_ev": _gt_graded(0.0)}
        extracted = {"band_gap_ev": err}
        r = score_fields(gt, extracted, _scoring_band_gap())
        assert r["per_field_scores"]["band_gap_ev"] == pytest.approx(0.0, abs=1e-9), \
            f"err={err}: expected 0.0, got {r['per_field_scores']['band_gap_ev']}"


def test_graded_absolute_partial_false_strict():
    """partial=False 退化为 boolean: err ≤ tol/2 → 1.0，否则 0.0（Q5 escape hatch）."""
    # err=0.04 < tol/2 → 1.0
    gt = {"band_gap_ev": _gt_graded(0.0)}
    extracted = {"band_gap_ev": 0.04}
    r = score_fields(gt, extracted, _scoring_band_gap(partial=False))
    assert r["per_field_scores"]["band_gap_ev"] == pytest.approx(1.0, abs=1e-9)

    # err=0.075 在 decay band 但 partial=False → 0.0
    extracted = {"band_gap_ev": 0.075}
    r = score_fields(gt, extracted, _scoring_band_gap(partial=False))
    assert r["per_field_scores"]["band_gap_ev"] == pytest.approx(0.0, abs=1e-9)


def test_graded_absolute_nonzero_target():
    """GT.value ≠ 0 时也按相同曲线（abs(v-t)）."""
    gt = {"band_gap_ev": _gt_graded(1.5)}      # GaAs-like target

    # err=0.04 → satin floor 1.0
    r = score_fields(gt, {"band_gap_ev": 1.54}, _scoring_band_gap())
    assert r["per_field_scores"]["band_gap_ev"] == pytest.approx(1.0, abs=1e-9)

    # err=0.075 → 0.5 半分点
    r = score_fields(gt, {"band_gap_ev": 1.425}, _scoring_band_gap())
    assert r["per_field_scores"]["band_gap_ev"] == pytest.approx(0.5, abs=0.01)

    # err=0.12 → 0 硬归零
    r = score_fields(gt, {"band_gap_ev": 1.38}, _scoring_band_gap())
    assert r["per_field_scores"]["band_gap_ev"] == pytest.approx(0.0, abs=1e-9)


def test_graded_absolute_invalid_input():
    """非数值 / 无 tol → 安全 fallback."""
    # 非数值
    gt = {"band_gap_ev": _gt_graded(0.0)}
    r = score_fields(gt, {"band_gap_ev": "not a number"}, _scoring_band_gap())
    assert r["per_field_scores"]["band_gap_ev"] == 0.0

    # tol = 0 → exact match
    gt = {"band_gap_ev": _gt_graded(1.5, tol=0.0)}
    r = score_fields(gt, {"band_gap_ev": 1.5}, _scoring_band_gap())
    assert r["per_field_scores"]["band_gap_ev"] == 1.0
    r = score_fields(gt, {"band_gap_ev": 1.51}, _scoring_band_gap())
    assert r["per_field_scores"]["band_gap_ev"] == 0.0
