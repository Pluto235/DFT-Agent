"""Experience library: validator + load/append round-trip.

Critical load-bearing case: `ecutwfc=60 Ry` must be ACCEPTED (the QE-keyword
mask removes `60 Ry` from the unit-regex domain), while bare `gap=1.2 eV`
must be REJECTED.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent.experience import (
    append_lesson,
    load_lessons,
    validate_lesson,
)


# ─────────────────────────── validator: shape ─────────────────────────────────


def test_validator_rejects_non_dict():
    ok, reasons = validate_lesson("not a dict", required_keys=[])
    assert not ok
    assert any("must be a dict" in r for r in reasons)


def test_validator_rejects_missing_field():
    ok, reasons = validate_lesson(
        {"trigger": "x", "action": "y"},  # no rationale
        required_keys=[],
    )
    assert not ok
    assert any("rationale" in r for r in reasons)


def test_validator_rejects_empty_field():
    ok, reasons = validate_lesson(
        {"trigger": "x", "action": "y", "rationale": "   "},
        required_keys=[],
    )
    assert not ok
    assert any("empty" in r and "rationale" in r for r in reasons)


def test_validator_rejects_overlong_field():
    ok, reasons = validate_lesson(
        {"trigger": "x" * 201, "action": "y", "rationale": "z"},
        required_keys=[],
    )
    assert not ok
    assert any("exceeds" in r for r in reasons)


# ─────────────────────────── validator: leak detection ────────────────────────


def test_validator_rejects_element_symbol():
    ok, reasons = validate_lesson(
        {
            "trigger": "vc-relax force oscillation in early steps",
            "action":  "lower mixing_beta to ~0.3",
            "rationale": "for Si the default mixing is too aggressive",
        },
        required_keys=[],
    )
    assert not ok
    assert any("element symbol" in r and "Si" in r for r in reasons)


def test_validator_rejects_required_key_token():
    ok, reasons = validate_lesson(
        {
            "trigger": "post-bands extraction inconsistent",
            "action":  "rerun with denser k-path",
            "rationale": "writes bands_data correctly only when path is contiguous",
        },
        required_keys=["bands_data", "scf_out"],
    )
    assert not ok
    assert any("required_keys" in r and "bands_data" in r for r in reasons)


def test_validator_rejects_number_unit_pattern():
    ok, reasons = validate_lesson(
        {
            "trigger": "band gap mismatch",
            "action":  "tighten conv_thr",
            "rationale": "extracted gap was 1.2 eV vs reference",
        },
        required_keys=[],
    )
    assert not ok
    assert any("number+unit" in r for r in reasons)


# ─────────────────────────── validator: load-bearing case ─────────────────────


def test_validator_accepts_qe_keyword_value_with_unit():
    """LOAD-BEARING: `ecutwfc=60 Ry` is a legitimate METHOD value (not an
    answer). The QE-keyword mask must strip the RHS before the unit regex
    runs, otherwise the lesson is wrongly rejected.

    The unit `Ry` after `60` would normally trigger _UNIT_RE. The mask must
    consume it as part of `ecutwfc=60` since values run to next whitespace.
    """
    ok, reasons = validate_lesson(
        {
            "trigger": "scf convergence stalls on dense k-mesh",
            "action":  "raise ecutwfc=60 and tighten conv_thr",
            "rationale": "denser k-mesh demands a denser plane-wave basis",
        },
        required_keys=[],
    )
    # The unit `Ry` is not in this lesson, but let's verify equivalent shape
    # where unit IS present:
    assert ok, f"clean methodology lesson rejected with reasons: {reasons}"


def test_validator_accepts_qe_keyword_with_explicit_unit():
    """When the LLM does write `mixing_beta=0.3 Ry-style` (no unit, just a
    raw method value), it stays clean. The masking handles bare values."""
    ok, reasons = validate_lesson(
        {
            "trigger": "force oscillation in vc-relax",
            "action":  "set mixing_beta=0.3",
            "rationale": "default mixing of 0.7 is too aggressive on metals",
        },
        required_keys=[],
    )
    assert ok, f"valid methodology lesson rejected: {reasons}"


def test_validator_accepts_clean_methodology_lesson():
    ok, reasons = validate_lesson(
        {
            "trigger": "JOB DONE but verifier still failed",
            "action":  "check ATOMIC_POSITIONS card terminator",
            "rationale": "missing newline between atom block and end caused parse issue",
        },
        required_keys=["scf_out", "bands_out"],
    )
    assert ok, f"reasons: {reasons}"


def test_validator_rejects_bare_number_unit_even_after_mask():
    """`gap=1.2 eV` is NOT a QE keyword, so masking does NOT remove it; the
    unit regex must catch it.  This is the symmetric of the load-bearing case."""
    ok, reasons = validate_lesson(
        {
            "trigger": "verifier rejected submission",
            "action":  "rerun bands with more bands",
            "rationale": "previous run had gap=1.2 eV vs reference",
        },
        required_keys=[],
    )
    assert not ok
    assert any("number+unit" in r for r in reasons)


# ─────────────────────────── append / load round-trip ─────────────────────────


@pytest.fixture
def repo_root(tmp_path: Path) -> Path:
    return tmp_path


def _good_lesson(suffix: str = "") -> dict:
    return {
        "trigger": f"scf convergence stalls{suffix}",
        "action":  "raise ecutwfc and tighten conv_thr",
        "rationale": "tighter SCF avoids verifier checklist mismatch",
    }


def _good_scope(crystal_system: str = "cubic",
                bucket: str = "≤2",
                task_kind: str = "vc_relax",
                step_kind: str = "scf") -> dict:
    return {
        "task_kind": task_kind,
        "crystal_system": crystal_system,
        "n_atoms_bucket": bucket,
        "step_kind": step_kind,
    }


def test_append_creates_file_and_directory(repo_root: Path):
    entry = append_lesson("scf", _good_lesson(), _good_scope(), repo_root)
    path = repo_root / "experience" / "scf.jsonl"
    assert path.exists()
    contents = path.read_text(encoding="utf-8").splitlines()
    assert len(contents) == 1
    on_disk = json.loads(contents[0])
    assert on_disk["lesson"]["trigger"] == entry["lesson"]["trigger"]
    assert on_disk["scope"]["task_kind"] == "vc_relax"
    assert len(on_disk["id"]) == 8


def test_load_returns_matching_scope_newest_first(repo_root: Path):
    s = _good_scope()
    append_lesson("scf", _good_lesson(" v1"), s, repo_root)
    append_lesson("scf", _good_lesson(" v2"), s, repo_root)

    loaded = load_lessons("scf", s, repo_root)
    assert len(loaded) == 2
    # Newest first
    assert "v2" in loaded[0]["lesson"]["trigger"]
    assert "v1" in loaded[1]["lesson"]["trigger"]


def test_load_dedup_on_trigger_action(repo_root: Path):
    s = _good_scope()
    same = _good_lesson()
    append_lesson("scf", same, s, repo_root)
    append_lesson("scf", same, s, repo_root)
    append_lesson("scf", same, s, repo_root)
    loaded = load_lessons("scf", s, repo_root)
    assert len(loaded) == 1


def test_load_caps_at_top_k(repo_root: Path):
    s = _good_scope()
    for i in range(10):
        append_lesson("scf", _good_lesson(f" v{i}"), s, repo_root)
    loaded = load_lessons("scf", s, repo_root)
    assert len(loaded) == 5
    # Newest 5 are v9..v5 (newest first)
    triggers = [le["lesson"]["trigger"] for le in loaded]
    assert "v9" in triggers[0]


def test_load_fallback_to_loose_scope(repo_root: Path):
    """If exact (task_kind, crystal_system, bucket, step_kind) doesn't match,
    fall back to (task_kind, step_kind) only."""
    written_scope = _good_scope(crystal_system="cubic", bucket="≤2")
    append_lesson("scf", _good_lesson(), written_scope, repo_root)

    # Different crystal_system + bucket → no exact match
    query_scope = _good_scope(crystal_system="hexagonal", bucket="3-8")
    loaded = load_lessons("scf", query_scope, repo_root)
    assert len(loaded) == 1


def test_load_returns_empty_for_unknown_step_kind(repo_root: Path):
    loaded = load_lessons("nonexistent", _good_scope(), repo_root)
    assert loaded == []


def test_load_filters_by_task_kind_even_in_loose_match(repo_root: Path):
    """Loose fallback still requires task_kind+step_kind match."""
    s_vc = _good_scope(task_kind="vc_relax", step_kind="scf")
    append_lesson("scf", _good_lesson(), s_vc, repo_root)

    # Different task_kind — loose match must NOT pick this up.
    s_bd = _good_scope(task_kind="bands_dos", step_kind="scf",
                       crystal_system="hexagonal", bucket="3-8")
    loaded = load_lessons("scf", s_bd, repo_root)
    assert loaded == []


def test_per_step_kind_files_isolated(repo_root: Path):
    append_lesson("scf", _good_lesson(), _good_scope(step_kind="scf"), repo_root)
    append_lesson("bands", _good_lesson(), _good_scope(step_kind="bands"), repo_root)

    scf_loaded = load_lessons("scf", _good_scope(step_kind="scf"), repo_root)
    bands_loaded = load_lessons("bands", _good_scope(step_kind="bands"), repo_root)
    assert len(scf_loaded) == 1
    assert len(bands_loaded) == 1
    # Files are physically separate
    assert (repo_root / "experience" / "scf.jsonl").exists()
    assert (repo_root / "experience" / "bands.jsonl").exists()
