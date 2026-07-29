import pytest
from harness.lib.scoring import score_fields


def _gt(value, rule, tolerance=None, unit=None):
    d = {"value": value, "rule": rule}
    if tolerance is not None:
        d["tolerance"] = tolerance
    if unit is not None:
        d["unit"] = unit
    return d


def test_exact_pass():
    gt = {"space_group_number": _gt(227, "exact")}
    extracted = {"space_group_number": 227}
    scoring = {"aggregate": "all_fields_weighted", "field_weights": {"space_group_number": 1.0}}
    result = score_fields(gt, extracted, scoring)
    assert result["per_field_scores"]["space_group_number"] == 1.0
    assert result["total_score"] == 1.0


def test_exact_fail():
    gt = {"space_group_number": _gt(227, "exact")}
    extracted = {"space_group_number": 225}
    scoring = {"aggregate": "all_fields_weighted", "field_weights": {"space_group_number": 1.0}}
    result = score_fields(gt, extracted, scoring)
    assert result["per_field_scores"]["space_group_number"] == 0.0
    assert result["total_score"] == 0.0


def test_relative_within_tolerance():
    gt = {"a": _gt(3.849, "relative", tolerance=0.01, unit="angstrom")}
    extracted = {"a": 3.870}  # ~0.55% off
    scoring = {"aggregate": "all_fields_weighted", "field_weights": {"a": 1.0}}
    result = score_fields(gt, extracted, scoring)
    assert result["per_field_scores"]["a"] == 1.0


def test_relative_outside_tolerance_partial_credit():
    gt = {"a": _gt(3.849, "relative", tolerance=0.01, unit="angstrom")}
    extracted = {"a": 4.000}  # ~3.9% off, 超 1% 但在 2% 外
    scoring = {"aggregate": "all_fields_weighted", "field_weights": {"a": 1.0}, "partial_credit": True}
    result = score_fields(gt, extracted, scoring)
    # linear falloff: 1 - (err/tol - 1), clamp [0,1]
    # err=0.0392, tol=0.01 → ratio = 3.92 → score = 1 - (3.92-1) = -1.92 → clamp 0
    assert 0.0 <= result["per_field_scores"]["a"] <= 0.5


def test_absolute_within_tolerance():
    gt = {"alpha": _gt(60.0, "absolute", tolerance=1.0, unit="degree")}
    extracted = {"alpha": 60.5}
    scoring = {"aggregate": "all_fields_weighted", "field_weights": {"alpha": 1.0}}
    result = score_fields(gt, extracted, scoring)
    assert result["per_field_scores"]["alpha"] == 1.0


def test_weighted_aggregate():
    gt = {
        "a": _gt(3.849, "relative", tolerance=0.01, unit="angstrom"),
        "space_group_number": _gt(227, "exact"),
    }
    extracted = {"a": 3.849, "space_group_number": 225}
    scoring = {
        "aggregate": "all_fields_weighted",
        "field_weights": {"a": 0.25, "space_group_number": 0.75},
    }
    result = score_fields(gt, extracted, scoring)
    # a = 1.0 × 0.25 = 0.25
    # space_group_number = 0.0 × 0.75 = 0
    assert abs(result["total_score"] - 0.25) < 1e-9


def test_missing_field_scores_zero():
    gt = {"a": _gt(3.849, "relative", tolerance=0.01, unit="angstrom")}
    extracted = {}  # a 缺失
    scoring = {"aggregate": "all_fields_weighted", "field_weights": {"a": 1.0}}
    result = score_fields(gt, extracted, scoring)
    assert result["per_field_scores"]["a"] == 0.0
    assert result["total_score"] == 0.0


def test_extra_extracted_ignored():
    """extracted 多余字段（不在 weights 里）应被忽略。"""
    gt = {"a": _gt(3.849, "relative", tolerance=0.01, unit="angstrom")}
    extracted = {"a": 3.849, "point_group": "m-3m", "crystal_system": "cubic"}
    scoring = {"aggregate": "all_fields_weighted", "field_weights": {"a": 1.0}}
    result = score_fields(gt, extracted, scoring)
    assert result["total_score"] == 1.0
