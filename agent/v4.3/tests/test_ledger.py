"""ledger.py — plan/verdict schema 校验 + TaskLedger 序列化测试。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from agent.ledger import (
    Attempt,
    PlanSpec,
    PlanStep,
    ReplanEvent,
    StepProgress,
    SUBMIT_PLAN_SCHEMA,
    SUBMIT_VERDICT_SCHEMA,
    TaskLedger,
    VerdictSpec,
    format_validation_error,
    now_iso,
    parse_plan,
    parse_verdict,
)


# ─────────────────────────── PlanSpec / PlanStep ──────────────────────────────

def _good_plan() -> dict:
    return {
        "steps": [
            {
                "id": "scf_1",
                "kind": "scf",
                "desc": "Run SCF on initial cell",
                "deps": [],
                "expected_outputs": {"scf_out": "scf_1/scf.out"},
            },
            {
                "id": "bands_1",
                "kind": "bands",
                "desc": "Compute band structure",
                "deps": ["scf_1"],
                "expected_outputs": {"bands_out": "bands_1/bands.out",
                                     "bands_data": "bands_1/bands.dat"},
            },
        ],
    }


def test_parse_plan_happy_path():
    p = parse_plan(_good_plan())
    assert len(p.steps) == 2
    assert p.steps[0].id == "scf_1"
    assert p.steps[1].deps == ["scf_1"]


def test_plan_rejects_unknown_kind():
    bad = _good_plan()
    bad["steps"][0]["kind"] = "magic"
    with pytest.raises(ValidationError):
        parse_plan(bad)


def test_plan_rejects_duplicate_ids():
    bad = _good_plan()
    # Make both steps the same id; also update step 2's paths to use scf_1/ prefix
    # so we exercise the duplicate-id validator (not the path-prefix one).
    bad["steps"][1]["id"] = "scf_1"
    bad["steps"][1]["expected_outputs"] = {
        "bands_out": "scf_1/bands.out", "bands_data": "scf_1/bands.dat",
    }
    bad["steps"][1]["deps"] = []
    with pytest.raises(ValidationError, match="duplicate"):
        parse_plan(bad)


def test_plan_rejects_outputs_without_step_id_prefix():
    """v4.3 PATH CONVENTION: every expected_outputs path must start with `<step.id>/`.
    Forces the planner to align step subdir with file locations so promote/verify finds them."""
    bad = _good_plan()
    bad["steps"][0]["expected_outputs"] = {"scf_out": "scf.out"}  # missing scf_1/ prefix
    with pytest.raises(ValidationError, match="must start with"):
        parse_plan(bad)


def test_plan_rejects_unknown_dep():
    bad = _good_plan()
    bad["steps"][1]["deps"] = ["nonexistent"]
    with pytest.raises(ValidationError, match="unknown step"):
        parse_plan(bad)


def test_plan_rejects_self_dep():
    bad = _good_plan()
    bad["steps"][1]["deps"] = ["bands_1"]
    with pytest.raises(ValidationError, match="self-reference"):
        parse_plan(bad)


def test_plan_rejects_empty_steps():
    with pytest.raises(ValidationError):
        parse_plan({"steps": []})


def test_plan_rejects_invalid_step_id():
    bad = _good_plan()
    bad["steps"][0]["id"] = "../escape"
    with pytest.raises(ValidationError, match="invalid step id"):
        parse_plan(bad)


def test_plan_rejects_absolute_output_path():
    bad = _good_plan()
    bad["steps"][0]["expected_outputs"]["scf_out"] = "/abs/path"
    with pytest.raises(ValidationError, match="relative path"):
        parse_plan(bad)


def test_plan_rejects_empty_expected_outputs():
    bad = _good_plan()
    bad["steps"][0]["expected_outputs"] = {}
    with pytest.raises(ValidationError, match="at least one entry"):
        parse_plan(bad)


# ─────────────────────────── VerdictSpec ──────────────────────────────────────

def test_verdict_parse_pass():
    v = parse_verdict({"pass": True, "diagnosis": ""})
    assert v.pass_ is True
    assert v.diagnosis == ""


def test_verdict_parse_fail():
    v = parse_verdict({
        "pass": False,
        "diagnosis": "scf.out missing JOB DONE; pw.x likely crashed on k-point read",
    })
    assert v.pass_ is False
    assert "JOB DONE" in v.diagnosis


def test_verdict_requires_both_fields():
    with pytest.raises(ValidationError):
        parse_verdict({"pass": True})
    with pytest.raises(ValidationError):
        parse_verdict({"diagnosis": "..."})


# ─────────────────────────── validation error formatter ──────────────────────

def test_format_validation_error_human_readable():
    try:
        parse_plan({"steps": [{"id": "x", "kind": "magic", "desc": "d",
                                "expected_outputs": {"k": "p"}}]})
    except ValidationError as e:
        msg = format_validation_error(e)
        # location ("steps.0.kind") and an actionable message should both appear
        assert "kind" in msg
        assert "scf" in msg or "should be" in msg.lower()
    else:
        pytest.fail("expected ValidationError")


# ─────────────────────────── TaskLedger serialize ────────────────────────────

def test_task_ledger_roundtrip(tmp_path):
    plan = parse_plan(_good_plan())
    ledger = TaskLedger(
        required_keys=["scf_out", "bands_out", "bands_data"],
        initial_plan=plan,
        progress={
            "scf_1": StepProgress(
                step_id="scf_1",
                status="done",
                attempts=[
                    Attempt(
                        attempt_no=1,
                        executor_session_id="abc-123",
                        verdict=VerdictSpec.model_validate({"pass": True, "diagnosis": ""}),
                        started_at=now_iso(),
                        finished_at=now_iso(),
                    ),
                ],
            ),
        },
    )
    out = tmp_path / "orchestrator.json"
    ledger.write(out)
    on_disk = json.loads(out.read_text())
    assert on_disk["required_keys"] == ["scf_out", "bands_out", "bands_data"]
    assert on_disk["initial_plan"]["steps"][0]["id"] == "scf_1"
    assert on_disk["progress"]["scf_1"]["status"] == "done"
    # verdict serializes back as "pass" key (alias), not "pass_"
    assert on_disk["progress"]["scf_1"]["attempts"][0]["verdict"]["pass"] is True


def test_task_ledger_current_plan_returns_latest_replan():
    plan_a = parse_plan(_good_plan())
    plan_b = parse_plan({
        "steps": [{"id": "scf_only", "kind": "scf", "desc": "SCF only",
                   "expected_outputs": {"scf_out": "scf_only/scf.out"}}],
    })
    ledger = TaskLedger(
        required_keys=["scf_out"],
        initial_plan=plan_a,
        replans=[ReplanEvent(replan_no=1, reason="too complex", new_plan=plan_b, at=now_iso())],
    )
    assert ledger.current_plan().steps[0].id == "scf_only"


# ─────────────────────────── Tool schemas exist ──────────────────────────────

def test_schemas_are_valid_jsonschema_shape():
    assert SUBMIT_PLAN_SCHEMA["type"] == "object"
    assert "steps" in SUBMIT_PLAN_SCHEMA["properties"]
    assert SUBMIT_VERDICT_SCHEMA["type"] == "object"
    assert "pass" in SUBMIT_VERDICT_SCHEMA["properties"]
    assert "diagnosis" in SUBMIT_VERDICT_SCHEMA["properties"]
