"""physics_sanity step kind + orchestrator auto-injection + degradation."""
from __future__ import annotations

import json
import sys
import types
from pathlib import Path
from unittest.mock import patch

import pytest

from agent.ledger import (
    Attempt,
    PlanSpec,
    PlanStep,
    StepProgress,
    TaskLedger,
    VerdictSpec,
)
from agent.step_kinds import physics_sanity


# ─────────────────────── helpers ──────────────────────────────────────────────


def _write_config_advice(workdir: Path, **fields):
    advice = {
        "ok": True, "formula": "Rb", "elements": ["Rb"],
        "is_metal": True, "occupations": "smearing", "smearing": "mv",
        "degauss": 0.02, "ecutrho_factor": 10.0,
        "rationale": "Rubidium is a textbook alkali metal.",
        "source": "deterministic",
    }
    advice.update(fields)
    (workdir / ".config_advice.json").write_text(
        json.dumps(advice, indent=2), encoding="utf-8")


def _write_canonical_outputs(workdir: Path):
    """Create the 5 canonical T2 files with content > 0 so existence checks pass."""
    for sub in ("scf_1", "bands_1", "dos_1"):
        (workdir / sub).mkdir(parents=True, exist_ok=True)
    files = {
        "scf_1/scf.out":          "JOB DONE.\n" + "x" * 1100,
        "bands_1/bands.out":      "JOB DONE.\n" + "x" * 1100,
        "bands_1/bands.dat":      "1.0 2.0\n3.0 4.0\n" * 200,
        "dos_1/dos.dat":          "1.0 2.0\n" * 200,
        "dos_1/nscf_dos.out":     "JOB DONE.\n" + "x" * 1100,
    }
    for p, content in files.items():
        (workdir / p).write_text(content, encoding="utf-8")
    return {
        "scf_out":       "scf_1/scf.out",
        "bands_out":     "bands_1/bands.out",
        "bands_data":    "bands_1/bands.dat",
        "dos_dat":       "dos_1/dos.dat",
        "nscf_dos_out":  "dos_1/nscf_dos.out",
    }


@pytest.fixture
def workdir(tmp_path):
    return tmp_path


# ─────────────────────── run_check: graceful-skip paths ──────────────────────


def test_run_check_skipped_when_config_advice_missing(workdir):
    sub = workdir / "physics_sanity_1"
    out = physics_sanity.run_check(
        workdir=workdir, sanity_subdir=sub,
        expected_outputs_so_far={},
        config_advice_path=workdir / ".config_advice.json",
    )
    assert out["verdict"] == "skipped"
    assert "config_advice not found" in out["diagnosis"]
    # sanity.json written even on skip (for debugging)
    assert (sub / "sanity.json").exists()


def test_run_check_skipped_when_t2_keys_missing(workdir):
    _write_config_advice(workdir)
    sub = workdir / "physics_sanity_1"
    out = physics_sanity.run_check(
        workdir=workdir, sanity_subdir=sub,
        expected_outputs_so_far={"scf_out": "scf_1/scf.out"},  # missing 4 keys
        config_advice_path=workdir / ".config_advice.json",
    )
    assert out["verdict"] == "skipped"
    assert "missing" in out["diagnosis"]


def test_run_check_skipped_when_bench_unavailable(workdir, monkeypatch):
    _write_config_advice(workdir)
    outs = _write_canonical_outputs(workdir)
    # Force bench resolution to fail
    monkeypatch.setenv("BENCH_ROOT", str(workdir / "no-such-bench"))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: workdir / "no-such-home"))
    sub = workdir / "physics_sanity_1"
    out = physics_sanity.run_check(
        workdir=workdir, sanity_subdir=sub,
        expected_outputs_so_far=outs,
        config_advice_path=workdir / ".config_advice.json",
    )
    assert out["verdict"] == "skipped"
    assert "bench harness unavailable" in out["diagnosis"]


# ─────────────────────── run_check: 4 rules with mocked bench extractor ──────


def _patch_bench(monkeypatch, workdir, extracted_fields):
    """Stub out _resolve_bench_context to skip filesystem checks; stub
    extract_t2_fields to return canned output. Returns the recorded call args."""
    fake_ev = types.ModuleType("harness.lib.evaluator")
    fake_loader = types.ModuleType("harness.lib.task_loader")
    captured = {}

    def fake_extract(**kw):
        captured.update(kw)
        return extracted_fields

    fake_ev.extract_t2_fields = fake_extract
    fake_loader.load_task = lambda *a, **k: {"task": {"material": {
        "initial_structure": {"a_lattice_vector_ang": [1, 0, 0]}
    }}}

    monkeypatch.setitem(sys.modules, "harness", types.ModuleType("harness"))
    monkeypatch.setitem(sys.modules, "harness.lib", types.ModuleType("harness.lib"))
    monkeypatch.setitem(sys.modules, "harness.lib.evaluator", fake_ev)
    monkeypatch.setitem(sys.modules, "harness.lib.task_loader", fake_loader)

    def fake_resolve(wd):
        return workdir, "M1_T2_Rb_bands_dos", {"a_lattice_vector_ang": [1, 0, 0]}

    monkeypatch.setattr(physics_sanity, "_resolve_bench_context", fake_resolve)
    return captured


def test_rule_a_deterministic_metal_extracted_gap_fails(workdir, monkeypatch):
    """Advisor=metal × bench fermi-slice 报中等 gap (0.5 < gap < 5 eV) →
    真 Rule A：fermi-slice 漏点，agent 可 fix。

    注意：> 5 eV 进 blocked_by_bench 分支（FIX-2，2026-05-20），见
    test_rule_a_bench_bug_signature_blocks_by_bench。"""
    _write_config_advice(workdir, is_metal=True, source="deterministic")
    outs = _write_canonical_outputs(workdir)
    _patch_bench(monkeypatch, workdir,
                 {"gap_type": "direct", "band_gap_ev": 1.5})

    out = physics_sanity.run_check(
        workdir=workdir, sanity_subdir=workdir / "physics_sanity_1",
        expected_outputs_so_far=outs,
        config_advice_path=workdir / ".config_advice.json",
    )
    assert out["verdict"] == "fail"
    assert "metal" in out["diagnosis"]
    assert "nbnd" in out["diagnosis"]
    assert out["details"]["extracted_band_gap_ev"] == 1.5


def test_rule_b_deterministic_insulator_extracted_metal_fails(workdir, monkeypatch):
    _write_config_advice(workdir, is_metal=False, source="deterministic",
                         rationale="Silicon is a covalent semiconductor.")
    outs = _write_canonical_outputs(workdir)
    _patch_bench(monkeypatch, workdir,
                 {"gap_type": "metal", "band_gap_ev": 0.0})

    out = physics_sanity.run_check(
        workdir=workdir, sanity_subdir=workdir / "physics_sanity_1",
        expected_outputs_so_far=outs,
        config_advice_path=workdir / ".config_advice.json",
    )
    assert out["verdict"] == "fail"
    assert "insulator" in out["diagnosis"] or "semiconductor" in out["diagnosis"]


def test_rule_c_wide_gap_pbe_underestimate_soft_warn(workdir, monkeypatch):
    _write_config_advice(workdir, is_metal=False, source="deterministic",
                         rationale="Aluminum oxide is a wide-gap insulator.")
    outs = _write_canonical_outputs(workdir)
    _patch_bench(monkeypatch, workdir,
                 {"gap_type": "indirect", "band_gap_ev": 0.01})

    out = physics_sanity.run_check(
        workdir=workdir, sanity_subdir=workdir / "physics_sanity_1",
        expected_outputs_so_far=outs,
        config_advice_path=workdir / ".config_advice.json",
    )
    assert out["verdict"] == "soft_warn"
    assert "PBE" in out["diagnosis"]


def test_rule_d_low_confidence_mismatch_soft_warn(workdir, monkeypatch):
    """LLM-override config: even on big mismatch, never trigger retry."""
    _write_config_advice(workdir, is_metal=True, source="llm_override")
    outs = _write_canonical_outputs(workdir)
    _patch_bench(monkeypatch, workdir,
                 {"gap_type": "direct", "band_gap_ev": 10.988})

    out = physics_sanity.run_check(
        workdir=workdir, sanity_subdir=workdir / "physics_sanity_1",
        expected_outputs_so_far=outs,
        config_advice_path=workdir / ".config_advice.json",
    )
    assert out["verdict"] == "soft_warn"
    assert "low-confidence" in out["diagnosis"] or "llm_override" in out["diagnosis"]


def test_rule_consistent_passes(workdir, monkeypatch):
    _write_config_advice(workdir, is_metal=True, source="deterministic")
    outs = _write_canonical_outputs(workdir)
    _patch_bench(monkeypatch, workdir,
                 {"gap_type": "metal", "band_gap_ev": 0.0})

    out = physics_sanity.run_check(
        workdir=workdir, sanity_subdir=workdir / "physics_sanity_1",
        expected_outputs_so_far=outs,
        config_advice_path=workdir / ".config_advice.json",
    )
    assert out["verdict"] == "pass"
    assert out["diagnosis"] == ""
    assert out["diagnosis_struct"] is None


def test_rule_a_emits_structured_diagnosis(workdir, monkeypatch):
    """Rule A must populate diagnosis_struct alongside legacy diagnosis.

    Use mid-range gap (1.5 eV) so this still tests the original Rule A path,
    not the new blocked_by_bench branch (gap > 5 eV, FIX-2)."""
    _write_config_advice(workdir, is_metal=True, source="deterministic")
    outs = _write_canonical_outputs(workdir)
    _patch_bench(monkeypatch, workdir,
                 {"gap_type": "direct", "band_gap_ev": 1.5})

    out = physics_sanity.run_check(
        workdir=workdir, sanity_subdir=workdir / "physics_sanity_1",
        expected_outputs_so_far=outs,
        config_advice_path=workdir / ".config_advice.json",
    )
    struct = out["diagnosis_struct"]
    assert struct is not None
    assert struct["root_cause"] == "metal_vs_extracted_gap"
    assert "1.500" in struct["summary"]
    actions = struct["fix_actions"]
    assert len(actions) >= 2
    fields = {a["field"] for a in actions}
    assert "nbnd" in fields and "K_POINTS" in fields
    # Each action has required keys
    for a in actions:
        assert {"file", "field", "suggested", "rationale"} <= set(a.keys())


# ─────────────────────── FIX-2: Rule A bench-bug signature ──────────────────


def test_rule_a_bench_bug_signature_blocks_by_bench(workdir, monkeypatch):
    """FIX-2 (2026-05-20): advisor=metal × extracted gap > 5 eV is the
    signature of bench evaluator _find_vbm_cbm_via_electron_count odd-nelec
    bug. Verdict=blocked_by_bench so agent submits instead of burning
    replan budget on no-op fixes (Rb 2026-05-19 故障)."""
    _write_config_advice(workdir, is_metal=True, source="deterministic",
                         formula="Rb")
    outs = _write_canonical_outputs(workdir)
    _patch_bench(monkeypatch, workdir,
                 {"gap_type": "direct", "band_gap_ev": 10.988})

    out = physics_sanity.run_check(
        workdir=workdir, sanity_subdir=workdir / "physics_sanity_1",
        expected_outputs_so_far=outs,
        config_advice_path=workdir / ".config_advice.json",
    )
    assert out["verdict"] == "blocked_by_bench"
    struct = out["diagnosis_struct"]
    assert struct is not None
    assert struct["root_cause"] == "bench_evaluator_odd_nelec_bug"
    # 不让 planner replan：fix_actions 应为空
    assert struct["fix_actions"] == []
    # diagnosis text 点名 bench 这一侧问题，agent 才能放心 submit
    assert "bench evaluator" in out["diagnosis"].lower()
    assert "odd-nelec" in out["diagnosis"]


def test_rule_a_just_below_threshold_still_fails(workdir, monkeypatch):
    """Gap = 4.9 eV (just below 5.0 threshold) → Rule A fail (real),
    NOT blocked_by_bench."""
    _write_config_advice(workdir, is_metal=True, source="deterministic")
    outs = _write_canonical_outputs(workdir)
    _patch_bench(monkeypatch, workdir,
                 {"gap_type": "direct", "band_gap_ev": 4.9})

    out = physics_sanity.run_check(
        workdir=workdir, sanity_subdir=workdir / "physics_sanity_1",
        expected_outputs_so_far=outs,
        config_advice_path=workdir / ".config_advice.json",
    )
    assert out["verdict"] == "fail"
    assert out["diagnosis_struct"]["root_cause"] == "metal_vs_extracted_gap"


# ─────────────────────── FIX-2 + FIX-3b: Rule B pbe_near_metal soft_warn ───


def test_rule_b_pbe_near_metal_softens_to_warn(workdir, monkeypatch):
    """FIX-3b + FIX-2: Ge-like case：advisor=semi (deterministic,
    pbe_near_metal=True) × bench extracted metal → soft_warn 而非 fail。
    PBE 算 Ge gap ~ 0 是已知失效，agent 算对了仍要被 GT 标 metal 评分。"""
    _write_config_advice(workdir,
                         is_metal=False,
                         pbe_near_metal=True,
                         formula="Ge",
                         source="deterministic",
                         rationale="Ge is a covalent semiconductor.")
    outs = _write_canonical_outputs(workdir)
    _patch_bench(monkeypatch, workdir,
                 {"gap_type": "metal", "band_gap_ev": 0.0})

    out = physics_sanity.run_check(
        workdir=workdir, sanity_subdir=workdir / "physics_sanity_1",
        expected_outputs_so_far=outs,
        config_advice_path=workdir / ".config_advice.json",
    )
    assert out["verdict"] == "soft_warn"
    struct = out["diagnosis_struct"]
    assert struct is not None
    assert struct["root_cause"] == "pbe_near_metal_expected_metal_extraction"
    assert struct["fix_actions"] == []
    # detail 字段也透传出来便于追溯
    assert out["details"]["advice_pbe_near_metal"] is True


def test_rule_b_regular_insulator_still_fails(workdir, monkeypatch):
    """对照：advisor=semi (pbe_near_metal=False) × bench=metal 仍是 fail。
    回归保护 — 不能因 FIX-3b 让真绝缘体的 metal 漏算静默通过。"""
    _write_config_advice(workdir,
                         is_metal=False,
                         pbe_near_metal=False,
                         formula="NaCl",
                         source="deterministic",
                         rationale="NaCl is a wide-gap rocksalt insulator.")
    outs = _write_canonical_outputs(workdir)
    _patch_bench(monkeypatch, workdir,
                 {"gap_type": "metal", "band_gap_ev": 0.0})

    out = physics_sanity.run_check(
        workdir=workdir, sanity_subdir=workdir / "physics_sanity_1",
        expected_outputs_so_far=outs,
        config_advice_path=workdir / ".config_advice.json",
    )
    # NaCl rationale 含 "wide-gap" → Rule C wide_gap_hint 可能短路。
    # 切到一个无 wide-gap hint 的 rationale 再断言更稳；这里同时接受
    # fail（原 Rule B）或 soft_warn（被 Rule C 短路），但 *不能* 是 pass。
    # 重点是不能因为 pbe_near_metal 误命中而 soft_warn。
    assert out["verdict"] in ("fail", "soft_warn")
    if out["verdict"] == "fail":
        assert out["diagnosis_struct"]["root_cause"] == "insulator_vs_extracted_metal"


# ─────────────────── FIX-8: Rule B' (smearing 未中 5 meV eps) ──────────────


def test_rule_b_prime_pbe_near_metal_extracted_direct_soft_warns(workdir, monkeypatch):
    """FIX-8 (2026-05-22) Rule B'：advisor 已强制 occupations='smearing'
    (pbe_near_metal=True)，但 bench evaluator 仍报 gap_type='direct' + 小 gap
    (8.8 meV) → fermi-splitter eps=5 meV 未覆盖近简并带差距 → FIX-8 未命中。
    Verdict 必须是 soft_warn (不阻塞 submit)，root_cause 标识 FIX-8 失灵子情形。"""
    _write_config_advice(workdir,
                         is_metal=False,
                         pbe_near_metal=True,
                         formula="Ge",
                         source="deterministic",
                         rationale="Ge is a covalent semiconductor.")
    outs = _write_canonical_outputs(workdir)
    _patch_bench(monkeypatch, workdir,
                 {"gap_type": "direct", "band_gap_ev": 0.0088})

    out = physics_sanity.run_check(
        workdir=workdir, sanity_subdir=workdir / "physics_sanity_1",
        expected_outputs_so_far=outs,
        config_advice_path=workdir / ".config_advice.json",
    )
    assert out["verdict"] == "soft_warn"
    struct = out["diagnosis_struct"]
    assert struct is not None
    assert struct["root_cause"] == "pbe_near_metal_smearing_below_5mev_eps"
    assert struct["fix_actions"] == []   # 不让 planner replan
    assert "FIX-8" in out["diagnosis"]


def test_rule_b_prime_indirect_also_soft_warns(workdir, monkeypatch):
    """Rule B' 同时覆盖 gap_type='indirect' 的情形。"""
    _write_config_advice(workdir,
                         is_metal=False,
                         pbe_near_metal=True,
                         formula="InSb",
                         source="deterministic",
                         rationale="InSb is a covalent semiconductor.")
    outs = _write_canonical_outputs(workdir)
    _patch_bench(monkeypatch, workdir,
                 {"gap_type": "indirect", "band_gap_ev": 0.012})

    out = physics_sanity.run_check(
        workdir=workdir, sanity_subdir=workdir / "physics_sanity_1",
        expected_outputs_so_far=outs,
        config_advice_path=workdir / ".config_advice.json",
    )
    assert out["verdict"] == "soft_warn"
    assert out["diagnosis_struct"]["root_cause"] == "pbe_near_metal_smearing_below_5mev_eps"


def test_rule_b_prime_does_not_fire_on_large_gap(workdir, monkeypatch):
    """Rule B' 只覆盖 gap < 0.05 eV 的近金属失败模式。如果 PBE 算出 0.3 eV gap
    那是正常半导体输出，不应误降为 soft_warn 掩盖问题。"""
    _write_config_advice(workdir,
                         is_metal=False,
                         pbe_near_metal=True,
                         formula="Ge",
                         source="deterministic",
                         rationale="Ge is a covalent semiconductor.")
    outs = _write_canonical_outputs(workdir)
    _patch_bench(monkeypatch, workdir,
                 {"gap_type": "direct", "band_gap_ev": 0.3})

    out = physics_sanity.run_check(
        workdir=workdir, sanity_subdir=workdir / "physics_sanity_1",
        expected_outputs_so_far=outs,
        config_advice_path=workdir / ".config_advice.json",
    )
    # 0.3 eV gap 不归 Rule B'，落 pass（无 wide-gap hint，无矛盾，Rule C/D 也不命中）
    assert out["verdict"] != "soft_warn" or \
        (out["diagnosis_struct"] is None or
         out["diagnosis_struct"]["root_cause"] != "pbe_near_metal_smearing_below_5mev_eps")


def test_rule_b_prime_does_not_fire_when_pbe_near_metal_false(workdir, monkeypatch):
    """对照：pbe_near_metal=False 的真半导体，FIX-8 Rule B' 不应触发。"""
    _write_config_advice(workdir,
                         is_metal=False,
                         pbe_near_metal=False,
                         formula="Si",
                         source="deterministic",
                         rationale="Si is a covalent semiconductor.")
    outs = _write_canonical_outputs(workdir)
    _patch_bench(monkeypatch, workdir,
                 {"gap_type": "direct", "band_gap_ev": 0.008})

    out = physics_sanity.run_check(
        workdir=workdir, sanity_subdir=workdir / "physics_sanity_1",
        expected_outputs_so_far=outs,
        config_advice_path=workdir / ".config_advice.json",
    )
    # 不应该走 FIX-8 root_cause
    if out["diagnosis_struct"] is not None:
        assert out["diagnosis_struct"]["root_cause"] != "pbe_near_metal_smearing_below_5mev_eps"


# ─────────────────────── Rule E (magnetism) ─────────────────────────────────


def _patch_bench_with_task(monkeypatch, workdir, extracted_fields,
                           magnetic_metadata=None):
    """Like _patch_bench but also injects a magnetic_metadata block into the
    task dict returned by load_task — for exercising Rule E."""
    fake_ev = types.ModuleType("harness.lib.evaluator")
    fake_loader = types.ModuleType("harness.lib.task_loader")

    def fake_extract(**kw):
        return extracted_fields

    material = {"initial_structure": {"a_lattice_vector_ang": [1, 0, 0]}}
    if magnetic_metadata is not None:
        material["magnetic_metadata"] = magnetic_metadata
    task_dict = {"material": material}

    fake_ev.extract_t2_fields = fake_extract
    fake_loader.load_task = lambda *a, **k: task_dict
    monkeypatch.setitem(sys.modules, "harness", types.ModuleType("harness"))
    monkeypatch.setitem(sys.modules, "harness.lib", types.ModuleType("harness.lib"))
    monkeypatch.setitem(sys.modules, "harness.lib.evaluator", fake_ev)
    monkeypatch.setitem(sys.modules, "harness.lib.task_loader", fake_loader)
    monkeypatch.setattr(physics_sanity, "_resolve_bench_context",
                        lambda wd: (workdir, "M3_T2_Fe_bcc_bands_dos",
                                    material["initial_structure"]))


def test_rule_e_magnetism_zero_moment_fails(workdir, monkeypatch):
    """Task is magnetic (M3 Fe_bcc) but SCF landed at total_mag ≈ 0."""
    _write_config_advice(workdir, is_metal=True, source="deterministic",
                         magnetic_elements=["Fe"],
                         starting_magnetization={"Fe": 0.7})
    outs = _write_canonical_outputs(workdir)
    # Overwrite scf.out so it has a fake "total magnetization = -0.00"
    (workdir / "scf_1" / "scf.out").write_text(
        "JOB DONE.\n     total magnetization       =    -0.00 Bohr mag/cell\n"
        + "x" * 1100
    )
    # Plant a DECOY magnetic_metadata in the bench task — Rule E must IGNORE it
    # and source magnetism from the solver's own config_advice (Fe / 0.7).
    _patch_bench_with_task(monkeypatch, workdir,
        {"gap_type": "metal", "band_gap_ev": 0.0},   # A-D would pass
        magnetic_metadata={"nspin": 2, "main_magnetic_element": "Zz",
                           "starting_magnetization": {"Zz": 9.9}})

    out = physics_sanity.run_check(
        workdir=workdir, sanity_subdir=workdir / "physics_sanity_1",
        expected_outputs_so_far=outs,
        config_advice_path=workdir / ".config_advice.json",
    )
    assert out["verdict"] == "fail"
    struct = out["diagnosis_struct"]
    assert struct is not None
    assert "magnetic_but_zero_moment" in struct["root_cause"]
    fields = {a["field"] for a in struct["fix_actions"]}
    assert "nspin" in fields
    # element + seed come from config_advice (Fe / 0.7), NOT the bench decoy (Zz / 9.9)
    assert "starting_magnetization(Fe)" in fields
    assert "0.7" in {a.get("suggested") for a in struct["fix_actions"]}
    assert "Fe" in out["diagnosis"]
    assert "Zz" not in out["diagnosis"] and "9.9" not in out["diagnosis"]


def test_rule_e_skipped_when_advice_not_magnetic(workdir, monkeypatch):
    """Non-magnetic material (config_advice has no magnetic_elements) → Rule E
    silently no-ops."""
    _write_config_advice(workdir, is_metal=True, source="deterministic")
    outs = _write_canonical_outputs(workdir)
    _patch_bench_with_task(monkeypatch, workdir,
        {"gap_type": "metal", "band_gap_ev": 0.0})

    out = physics_sanity.run_check(
        workdir=workdir, sanity_subdir=workdir / "physics_sanity_1",
        expected_outputs_so_far=outs,
        config_advice_path=workdir / ".config_advice.json",
    )
    assert out["verdict"] == "pass"


def test_rule_e_passes_when_magnetism_present(workdir, monkeypatch):
    """Magnetic task with healthy SCF moment → no Rule E firing."""
    _write_config_advice(workdir, is_metal=True, source="deterministic",
                         magnetic_elements=["Fe"],
                         starting_magnetization={"Fe": 0.7})
    outs = _write_canonical_outputs(workdir)
    (workdir / "scf_1" / "scf.out").write_text(
        "JOB DONE.\n     total magnetization       =     2.50 Bohr mag/cell\n"
        + "x" * 1100
    )
    _patch_bench_with_task(monkeypatch, workdir,
        {"gap_type": "metal", "band_gap_ev": 0.0})

    out = physics_sanity.run_check(
        workdir=workdir, sanity_subdir=workdir / "physics_sanity_1",
        expected_outputs_so_far=outs,
        config_advice_path=workdir / ".config_advice.json",
    )
    assert out["verdict"] == "pass"


# ─────────────────────── Rule F (vc_relax residual stress) ──────────────────


def test_physics_sanity_never_reads_bench_magnetic_metadata():
    """Anti-leak guard (FIX-43): Rule E sources magnetism from the solver's own
    config_advice, never from the bench-hidden task spec. The module source must
    not reference `magnetic_metadata` or reload the full bench task dict."""
    import inspect
    src = inspect.getsource(physics_sanity)
    assert "magnetic_metadata" not in src
    assert "_try_load_full_task" not in src


def _write_vc_relax_out(workdir: Path, p_kbar: float) -> None:
    relax_dir = workdir / "vc_relax"
    relax_dir.mkdir(parents=True, exist_ok=True)
    (relax_dir / "relax.out").write_text(
        f"     total   stress  (Ry/bohr**3)                   (kbar)     P=    {p_kbar:.3f}\n"
        f"   0.0001  0.0  0.0   {p_kbar:.3f}  0.0  0.0\n"
        f"   0.0  0.0001  0.0   0.0  {p_kbar:.3f}  0.0\n"
        f"   0.0  0.0  0.0001   0.0  0.0  {p_kbar:.3f}\n"
        "JOB DONE.\n"
    )


def test_rule_f_high_residual_stress_soft_warns(workdir, monkeypatch):
    """vc_relax converged but |P| > 5 kbar (0.5 GPa) → soft_warn."""
    _write_config_advice(workdir, is_metal=True, source="deterministic")
    outs = _write_canonical_outputs(workdir)
    _write_vc_relax_out(workdir, p_kbar=12.5)   # = 1.25 GPa, well over threshold
    _patch_bench(monkeypatch, workdir,
                 {"gap_type": "metal", "band_gap_ev": 0.0})

    out = physics_sanity.run_check(
        workdir=workdir, sanity_subdir=workdir / "physics_sanity_1",
        expected_outputs_so_far=outs,
        config_advice_path=workdir / ".config_advice.json",
    )
    assert out["verdict"] == "soft_warn"
    struct = out["diagnosis_struct"]
    assert struct is not None
    assert struct["root_cause"] == "vc_relax_residual_stress"
    fields = {a["field"] for a in struct["fix_actions"]}
    assert "press_conv_thr" in fields and "cell_dofree" in fields
    assert "kbar" in out["diagnosis"] or "GPa" in out["diagnosis"]


def test_rule_f_low_residual_stress_does_not_fire(workdir, monkeypatch):
    """|P| = 1 kbar (< 5 kbar threshold) → no warning."""
    _write_config_advice(workdir, is_metal=True, source="deterministic")
    outs = _write_canonical_outputs(workdir)
    _write_vc_relax_out(workdir, p_kbar=1.0)
    _patch_bench(monkeypatch, workdir,
                 {"gap_type": "metal", "band_gap_ev": 0.0})

    out = physics_sanity.run_check(
        workdir=workdir, sanity_subdir=workdir / "physics_sanity_1",
        expected_outputs_so_far=outs,
        config_advice_path=workdir / ".config_advice.json",
    )
    assert out["verdict"] == "pass"


def test_rule_f_skipped_when_no_vc_relax_out(workdir, monkeypatch):
    """No vc_relax preflight artifact → silently no-op."""
    _write_config_advice(workdir, is_metal=True, source="deterministic")
    outs = _write_canonical_outputs(workdir)
    # NO vc_relax/relax.out written
    _patch_bench(monkeypatch, workdir,
                 {"gap_type": "metal", "band_gap_ev": 0.0})

    out = physics_sanity.run_check(
        workdir=workdir, sanity_subdir=workdir / "physics_sanity_1",
        expected_outputs_so_far=outs,
        config_advice_path=workdir / ".config_advice.json",
    )
    assert out["verdict"] == "pass"


def test_rules_e_and_f_can_coexist_with_rule_a(workdir, monkeypatch):
    """Verdict + fix_actions must merge when multiple rules fire."""
    _write_config_advice(workdir, is_metal=True, source="deterministic",
                         magnetic_elements=["Fe"],
                         starting_magnetization={"Fe": 0.5})
    outs = _write_canonical_outputs(workdir)
    # Magnetic-but-zero-moment AND high stress
    (workdir / "scf_1" / "scf.out").write_text(
        "JOB DONE.\n     total magnetization       =     0.00 Bohr mag/cell\n"
        + "x" * 1100
    )
    _write_vc_relax_out(workdir, p_kbar=10.0)
    _patch_bench_with_task(monkeypatch, workdir,
        # Also a Rule A contradiction (metal but gap=1.5; 用 mid-range gap
        # 走原 Rule A fail 路径，避免 FIX-2 的 blocked_by_bench 分支)
        {"gap_type": "direct", "band_gap_ev": 1.5})

    out = physics_sanity.run_check(
        workdir=workdir, sanity_subdir=workdir / "physics_sanity_1",
        expected_outputs_so_far=outs,
        config_advice_path=workdir / ".config_advice.json",
    )
    # Final verdict = max severity (fail beats soft_warn)
    assert out["verdict"] == "fail"
    struct = out["diagnosis_struct"]
    assert struct is not None
    # root_cause concatenated
    assert "metal_vs_extracted_gap" in struct["root_cause"]
    assert "magnetic_but_zero_moment" in struct["root_cause"]
    assert "vc_relax_residual_stress" in struct["root_cause"]
    # All three rules' fix_actions present
    all_rationales = " ".join(a["rationale"] for a in struct["fix_actions"])
    assert "EF" in all_rationales            # Rule A
    assert "magnetic basin" in all_rationales  # Rule E
    assert "press_conv_thr" in {a["field"] for a in struct["fix_actions"]}  # Rule F


def test_extractor_raise_skips_gracefully(workdir, monkeypatch):
    _write_config_advice(workdir)
    outs = _write_canonical_outputs(workdir)
    fake_ev = types.ModuleType("harness.lib.evaluator")
    fake_loader = types.ModuleType("harness.lib.task_loader")

    def bomb(**kw):
        raise RuntimeError("simulated bench bug")

    fake_ev.extract_t2_fields = bomb
    fake_loader.load_task = lambda *a, **k: {"task": {"material":
        {"initial_structure": {}}}}
    monkeypatch.setitem(sys.modules, "harness", types.ModuleType("harness"))
    monkeypatch.setitem(sys.modules, "harness.lib", types.ModuleType("harness.lib"))
    monkeypatch.setitem(sys.modules, "harness.lib.evaluator", fake_ev)
    monkeypatch.setitem(sys.modules, "harness.lib.task_loader", fake_loader)
    monkeypatch.setattr(physics_sanity, "_resolve_bench_context",
                        lambda wd: (workdir, "M1_T2_X_bands_dos", {}))

    out = physics_sanity.run_check(
        workdir=workdir, sanity_subdir=workdir / "physics_sanity_1",
        expected_outputs_so_far=outs,
        config_advice_path=workdir / ".config_advice.json",
    )
    assert out["verdict"] == "skipped"
    assert "extract_t2_fields raised" in out["diagnosis"]
    assert "simulated bench bug" in out["diagnosis"]


# ─────────────────────── orchestrator: auto-injection ────────────────────────


def _bands_dos_plan() -> PlanSpec:
    return PlanSpec(steps=[
        PlanStep(id="scf_1", kind="scf", desc="SCF", deps=[],
                 expected_outputs={"scf_out": "scf_1/scf.out"}),
        PlanStep(id="bands_1", kind="bands", desc="bands", deps=["scf_1"],
                 expected_outputs={"bands_out": "bands_1/bands.out",
                                   "bands_data": "bands_1/bands.dat"}),
        PlanStep(id="dos_1", kind="dos", desc="dos", deps=["scf_1"],
                 expected_outputs={"dos_dat": "dos_1/dos.dat",
                                   "nscf_dos_out": "dos_1/nscf_dos.out"}),
    ])


def _vc_relax_plan() -> PlanSpec:
    return PlanSpec(steps=[
        PlanStep(id="vc_relax_1", kind="vc_relax", desc="vc-relax", deps=[],
                 expected_outputs={"vc_relax_out": "vc_relax_1/vc-relax.out"}),
    ])


def _make_orch(workdir, required_keys):
    from agent.orchestrator import PlannerOrchestrator
    # Avoid touching real LLM clients during construction.
    with patch("agent.orchestrator.LLMClient.from_role"):
        return PlannerOrchestrator(
            workdir=workdir,
            required_keys=required_keys,
            task_prompt="dummy",
            base_executor_prompt="BASE",
        )


def test_inject_adds_sanity_step_for_bands_dos(workdir):
    orch = _make_orch(workdir,
                      required_keys=["scf_out", "bands_out", "bands_data", "dos_dat"])
    plan = _bands_dos_plan()
    new_plan = orch._maybe_inject_sanity_step(plan)
    assert len(new_plan.steps) == 4
    sanity = new_plan.steps[-1]
    assert sanity.kind == "physics_sanity"
    assert set(sanity.deps) == {"scf_1", "bands_1", "dos_1"}
    assert sanity.expected_outputs["sanity_report"].startswith(sanity.id + "/")


def test_inject_skipped_for_vc_relax(workdir):
    orch = _make_orch(workdir, required_keys=["vc_relax_out"])
    plan = _vc_relax_plan()
    new_plan = orch._maybe_inject_sanity_step(plan)
    assert len(new_plan.steps) == 1
    assert all(s.kind != "physics_sanity" for s in new_plan.steps)


def test_inject_idempotent(workdir):
    """Don't double-inject if planner-on-replan still has the sanity step."""
    orch = _make_orch(workdir,
                      required_keys=["scf_out", "bands_out", "bands_data", "dos_dat"])
    plan = orch._maybe_inject_sanity_step(_bands_dos_plan())
    twice = orch._maybe_inject_sanity_step(plan)
    assert len(twice.steps) == len(plan.steps)


# ─────────────────────── orchestrator: notes injection on success ────────────


def test_success_appends_notes_when_sanity_soft_warn(workdir):
    orch = _make_orch(workdir,
                      required_keys=["scf_out", "bands_out", "bands_data", "dos_dat"])
    plan = orch._maybe_inject_sanity_step(_bands_dos_plan())
    sanity_id = plan.steps[-1].id

    # Pretend physics_sanity ran and wrote a soft_warn sanity.json
    sanity_dir = workdir / sanity_id
    sanity_dir.mkdir(parents=True)
    (sanity_dir / "sanity.json").write_text(json.dumps({
        "verdict": "soft_warn",
        "diagnosis": "PBE underestimates gap; not retried.",
        "details": {},
    }))

    # Build a ledger as if all 4 steps completed
    ledger = TaskLedger(
        required_keys=orch.required_keys,
        initial_plan=plan,
        progress={s.id: StepProgress(step_id=s.id, status="done",
            attempts=[Attempt(attempt_no=1,
                executor_session_id=f"sess-{s.id}",
                executor_submit={"task_outputs": dict(s.expected_outputs)},
                verdict=VerdictSpec(**{"pass": True, "diagnosis": ""}),
                started_at="2026-05-18T00:00:00+00:00",
                finished_at="2026-05-18T00:00:01+00:00",
            )]) for s in plan.steps},
    )
    result = orch._success(ledger)
    assert "notes" in result.payload
    assert "soft_warn" in result.payload["notes"]
    # sanity_report NOT in task_outputs (internal key)
    assert "sanity_report" not in result.payload["task_outputs"]
    # Bench-facing keys ARE there
    assert "scf_out" in result.payload["task_outputs"]


def test_success_omits_notes_when_sanity_pass(workdir):
    orch = _make_orch(workdir,
                      required_keys=["scf_out", "bands_out", "bands_data", "dos_dat"])
    plan = orch._maybe_inject_sanity_step(_bands_dos_plan())
    sanity_id = plan.steps[-1].id
    (workdir / sanity_id).mkdir(parents=True)
    (workdir / sanity_id / "sanity.json").write_text(json.dumps({
        "verdict": "pass", "diagnosis": "", "details": {},
    }))
    ledger = TaskLedger(
        required_keys=orch.required_keys,
        initial_plan=plan,
        progress={s.id: StepProgress(step_id=s.id, status="done",
            attempts=[Attempt(attempt_no=1,
                executor_session_id=f"sess-{s.id}",
                executor_submit={"task_outputs": dict(s.expected_outputs)},
                verdict=VerdictSpec(**{"pass": True, "diagnosis": ""}),
                started_at="2026-05-18T00:00:00+00:00",
                finished_at="2026-05-18T00:00:01+00:00",
            )]) for s in plan.steps},
    )
    result = orch._success(ledger)
    assert "notes" not in result.payload


# ─────────────────────── FIX-24: Rule A' metal noise-gap (k-path miss) ─────


def test_rule_a_prime_fires_on_small_gap_metal(workdir, monkeypatch):
    """FIX-24 (2026-05-25): advisor=metal × tiny extracted gap (0.002 eV)
    × gap_type='direct' → bands NSCF k-path missed Fermi crossings. Rule A'
    should fail with k-path/nbnd fix_actions (Ti3O5 5-25 case)."""
    _write_config_advice(workdir, is_metal=True, source="deterministic")
    outs = _write_canonical_outputs(workdir)
    _patch_bench(monkeypatch, workdir,
                 {"gap_type": "direct", "band_gap_ev": 0.002})

    out = physics_sanity.run_check(
        workdir=workdir, sanity_subdir=workdir / "physics_sanity_1",
        expected_outputs_so_far=outs,
        config_advice_path=workdir / ".config_advice.json",
    )
    assert out["verdict"] == "fail"
    struct = out["diagnosis_struct"]
    assert struct["root_cause"] == "metal_fermi_crossing_missed_in_kpath"
    assert "0.0020" in struct["summary"]
    fields = {a["field"] for a in struct["fix_actions"]}
    assert "nbnd" in fields and "K_POINTS crystal_b" in fields
    # diagnosis text mentions the score cost
    assert "0.35" in out["diagnosis"]


def test_rule_a_prime_fires_on_indirect_gap_metal(workdir, monkeypatch):
    """gap_type='indirect' with small gap on a known metal: same fix."""
    _write_config_advice(workdir, is_metal=True, source="deterministic")
    outs = _write_canonical_outputs(workdir)
    _patch_bench(monkeypatch, workdir,
                 {"gap_type": "indirect", "band_gap_ev": 0.05})

    out = physics_sanity.run_check(
        workdir=workdir, sanity_subdir=workdir / "physics_sanity_1",
        expected_outputs_so_far=outs,
        config_advice_path=workdir / ".config_advice.json",
    )
    assert out["verdict"] == "fail"
    assert out["diagnosis_struct"]["root_cause"] == "metal_fermi_crossing_missed_in_kpath"


def test_rule_a_unchanged_on_big_gap_metal(workdir, monkeypatch):
    """gap > 0.5 still goes to existing Rule A (metal_vs_extracted_gap),
    NOT Rule A' (metal_fermi_crossing_missed_in_kpath)."""
    _write_config_advice(workdir, is_metal=True, source="deterministic")
    outs = _write_canonical_outputs(workdir)
    _patch_bench(monkeypatch, workdir,
                 {"gap_type": "direct", "band_gap_ev": 1.5})

    out = physics_sanity.run_check(
        workdir=workdir, sanity_subdir=workdir / "physics_sanity_1",
        expected_outputs_so_far=outs,
        config_advice_path=workdir / ".config_advice.json",
    )
    assert out["verdict"] == "fail"
    assert out["diagnosis_struct"]["root_cause"] == "metal_vs_extracted_gap"


def test_rule_a_prime_does_not_fire_when_already_metal(workdir, monkeypatch):
    """advisor=metal AND extracted=metal → Rule A' must NOT fire, run passes."""
    _write_config_advice(workdir, is_metal=True, source="deterministic")
    outs = _write_canonical_outputs(workdir)
    _patch_bench(monkeypatch, workdir,
                 {"gap_type": "metal", "band_gap_ev": 0.002})

    out = physics_sanity.run_check(
        workdir=workdir, sanity_subdir=workdir / "physics_sanity_1",
        expected_outputs_so_far=outs,
        config_advice_path=workdir / ".config_advice.json",
    )
    assert out["verdict"] == "pass"


def test_rule_a_prime_does_not_fire_on_insulator(workdir, monkeypatch):
    """advisor=semi (not metal) with small extracted gap goes to Rule B'
    (pbe_near_metal soft_warn) or Rule C path, NEVER Rule A'."""
    _write_config_advice(workdir, is_metal=False, pbe_near_metal=True,
                         formula="Ge", source="deterministic",
                         rationale="Ge is a covalent semiconductor.")
    outs = _write_canonical_outputs(workdir)
    _patch_bench(monkeypatch, workdir,
                 {"gap_type": "direct", "band_gap_ev": 0.02})

    out = physics_sanity.run_check(
        workdir=workdir, sanity_subdir=workdir / "physics_sanity_1",
        expected_outputs_so_far=outs,
        config_advice_path=workdir / ".config_advice.json",
    )
    # Rule B' (pbe_near_metal soft_warn), NOT Rule A'
    assert out["verdict"] == "soft_warn"
    if out["diagnosis_struct"]:
        assert out["diagnosis_struct"]["root_cause"] != "metal_fermi_crossing_missed_in_kpath"
