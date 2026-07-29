"""step_kinds registry — load + addon/checklist render smoke tests."""
from __future__ import annotations

from pathlib import Path

import pytest

from agent import step_kinds
from agent.ledger import PlanStep


def _step(kind: str) -> PlanStep:
    return PlanStep(
        id=f"{kind}_1",
        kind=kind,
        desc=f"Run {kind}",
        deps=[],
        expected_outputs={f"{kind}_out": f"{kind}_1/{kind}.out"},
    )


def test_all_kinds_registered():
    kinds = set(step_kinds.registered_kinds())
    assert kinds == {"scf", "vc_relax", "relax", "bands", "dos", "collect",
                     "physics_sanity"}


def test_get_unknown_raises():
    with pytest.raises(KeyError, match="unknown step kind"):
        step_kinds.get("nonexistent")


@pytest.mark.parametrize("kind", ["scf", "vc_relax", "relax", "bands", "dos", "collect"])
def test_kind_spec_has_required_fields(kind):
    spec = step_kinds.get(kind)
    assert spec.kind == kind
    assert "submit" in spec.tool_allowlist
    assert callable(spec.system_prompt_addon)
    assert callable(spec.verifier_checklist)


@pytest.mark.parametrize("kind", ["scf", "vc_relax", "relax", "bands", "dos", "collect"])
def test_addon_renders_with_step_info(kind, tmp_path):
    spec = step_kinds.get(kind)
    step = _step(kind)
    addon = spec.system_prompt_addon(step, tmp_path, step.id, {})
    assert step.id in addon
    assert step.desc in addon
    # expected_outputs path appears
    assert f"{kind}_1/{kind}.out" in addon


def test_addon_renders_upstream_section(tmp_path):
    spec = step_kinds.get("scf")
    step = _step("scf")
    addon = spec.system_prompt_addon(
        step, tmp_path, step.id,
        {"vc_relax_1": ["vc_relax_1/vc-relax.out"]},
    )
    assert "vc_relax_1" in addon
    assert "vc-relax.out" in addon


@pytest.mark.parametrize("kind", ["scf", "vc_relax", "relax", "bands", "dos", "collect"])
def test_checklist_mentions_expected_outputs(kind):
    spec = step_kinds.get(kind)
    step = _step(kind)
    checklist = spec.verifier_checklist(step)
    assert f"{kind}_1/{kind}.out" in checklist
    assert "JOB DONE" in checklist


def test_vc_relax_checklist_mentions_convergence_thresholds():
    spec = step_kinds.get("vc_relax")
    step = _step("vc_relax")
    checklist = spec.verifier_checklist(step)
    assert "press_conv_thr" in checklist or "pressure" in checklist.lower()
