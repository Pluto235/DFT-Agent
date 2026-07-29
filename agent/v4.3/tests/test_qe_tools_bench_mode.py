"""Regression tests for bench-mode safety in qe_tools."""
from __future__ import annotations

import importlib
import sys
import types
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import qe_tools  # noqa: E402



def test_qe_tools_import_without_matplotlib(monkeypatch):
    real_import = __import__

    def guarded_import(name, *args, **kwargs):
        if name == "matplotlib" or name.startswith("matplotlib."):
            raise ModuleNotFoundError(name)
        return real_import(name, *args, **kwargs)

    monkeypatch.delitem(sys.modules, "qe_tools", raising=False)
    monkeypatch.setattr("builtins.__import__", guarded_import)
    module = importlib.import_module("qe_tools")
    assert module.DEFAULT_OMP_NUM_THREADS == "4"
    monkeypatch.setitem(sys.modules, "qe_tools", qe_tools)


def test_write_pw_input_tightens_vc_relax_thresholds(monkeypatch, tmp_path):
    captured = {}

    def fake_write_espresso_in(buf, atoms, input_data, pseudopotentials, kpts, koffset):
        captured["input_data"] = input_data
        buf.write("fake qe input\n")

    espresso = types.ModuleType("ase.io.espresso")
    espresso.write_espresso_in = fake_write_espresso_in
    ase_io = types.ModuleType("ase.io")
    ase_io.espresso = espresso
    ase = types.ModuleType("ase")
    ase.io = ase_io
    monkeypatch.setitem(sys.modules, "ase", ase)
    monkeypatch.setitem(sys.modules, "ase.io", ase_io)
    monkeypatch.setitem(sys.modules, "ase.io.espresso", espresso)

    class FakeAtoms:
        def get_chemical_symbols(self):
            return ["Si"]

    pseudo = tmp_path / "pseudos" / "Si.upf"
    pseudo.parent.mkdir()
    pseudo.write_text("pseudo")
    outdir = tmp_path / "out"
    filename = tmp_path / "job.in"

    qe_tools.write_pw_input(
        FakeAtoms(),
        prefix="job",
        outdir=str(outdir),
        pseudo_map={"Si": str(pseudo)},
        calculation="vc-relax",
        ecutwfc=80,
        ecutrho=800,
        occupations="fixed",
        forc_conv_thr=1.0e-3,
        etot_conv_thr=1.0e-4,
        press_conv_thr=0.5,
        filename=str(filename),
    )

    input_data = captured["input_data"]
    assert input_data["control"]["forc_conv_thr"] == pytest.approx(1.0e-4)
    assert input_data["control"]["etot_conv_thr"] == pytest.approx(1.0e-5)
    assert input_data["cell"]["press_conv_thr"] == pytest.approx(0.05)


def test_write_pw_input_keeps_tighter_vc_relax_thresholds(monkeypatch, tmp_path):
    captured = {}

    def fake_write_espresso_in(buf, atoms, input_data, pseudopotentials, kpts, koffset):
        captured["input_data"] = input_data
        buf.write("fake qe input\n")

    espresso = types.ModuleType("ase.io.espresso")
    espresso.write_espresso_in = fake_write_espresso_in
    ase_io = types.ModuleType("ase.io")
    ase_io.espresso = espresso
    ase = types.ModuleType("ase")
    ase.io = ase_io
    monkeypatch.setitem(sys.modules, "ase", ase)
    monkeypatch.setitem(sys.modules, "ase.io", ase_io)
    monkeypatch.setitem(sys.modules, "ase.io.espresso", espresso)

    class FakeAtoms:
        def get_chemical_symbols(self):
            return ["Si"]

    pseudo = tmp_path / "pseudos" / "Si.upf"
    pseudo.parent.mkdir()
    pseudo.write_text("pseudo")

    qe_tools.write_pw_input(
        FakeAtoms(),
        prefix="job",
        outdir=str(tmp_path / "out"),
        pseudo_map={"Si": str(pseudo)},
        calculation="vc-relax",
        ecutwfc=80,
        ecutrho=800,
        occupations="fixed",
        forc_conv_thr=1.0e-5,
        etot_conv_thr=1.0e-6,
        press_conv_thr=0.01,
        filename=str(tmp_path / "job.in"),
    )

    input_data = captured["input_data"]
    assert input_data["control"]["forc_conv_thr"] == pytest.approx(1.0e-5)
    assert input_data["control"]["etot_conv_thr"] == pytest.approx(1.0e-6)
    assert input_data["cell"]["press_conv_thr"] == pytest.approx(0.01)


def test_write_pw_input_tightens_extra_control_overrides(monkeypatch, tmp_path):
    captured = {}

    def fake_write_espresso_in(buf, atoms, input_data, pseudopotentials, kpts, koffset):
        captured["input_data"] = input_data
        buf.write("fake qe input\n")

    espresso = types.ModuleType("ase.io.espresso")
    espresso.write_espresso_in = fake_write_espresso_in
    ase_io = types.ModuleType("ase.io")
    ase_io.espresso = espresso
    ase = types.ModuleType("ase")
    ase.io = ase_io
    monkeypatch.setitem(sys.modules, "ase", ase)
    monkeypatch.setitem(sys.modules, "ase.io", ase_io)
    monkeypatch.setitem(sys.modules, "ase.io.espresso", espresso)

    class FakeAtoms:
        def get_chemical_symbols(self):
            return ["Si"]

    pseudo = tmp_path / "pseudos" / "Si.upf"
    pseudo.parent.mkdir()
    pseudo.write_text("pseudo")

    qe_tools.write_pw_input(
        FakeAtoms(),
        prefix="job",
        outdir=str(tmp_path / "out"),
        pseudo_map={"Si": str(pseudo)},
        calculation="vc-relax",
        ecutwfc=80,
        ecutrho=800,
        occupations="fixed",
        extra_control={"forc_conv_thr": "1.0d-2", "etot_conv_thr": "1.0d-3"},
        filename=str(tmp_path / "job.in"),
    )

    input_data = captured["input_data"]
    assert input_data["control"]["forc_conv_thr"] == pytest.approx(1.0e-4)
    assert input_data["control"]["etot_conv_thr"] == pytest.approx(1.0e-5)


# ─── write_dos_input — tetrahedra default, smearing as opt-in ───────────────
#
# Bench GT switched to tetrahedra on 2026-04-30. write_dos_input's DEFAULT
# (no ngauss/degauss → dos.x reuses NSCF tetrahedra weights → linear
# analytic integration that matches GT shape) IS the canonical path.
# Below tests verify: (1) default omits both lines so dos.x falls through
# to NSCF tetrahedra; (2) helper still accepts Gaussian params for legacy
# callers (no longer the recommended path — see FIX-9 in report
# `standard-bench-failures-rb-ge-wo3-cosb3-2026-05-20.html` §9).

def test_write_dos_input_default_no_smearing(tmp_path):
    """Default call (no ngauss/degauss): emit a clean &DOS block with
    NO ngauss/degauss lines — so dos.x falls back to the NSCF method
    (tetrahedra)."""
    out = tmp_path / "dos.in"
    qe_tools.write_dos_input(
        prefix="job", outdir="./out", emin=-5, emax=5, deltaE=0.01,
        filename=str(out),
    )
    text = out.read_text()
    assert "ngauss" not in text
    assert "degauss" not in text
    assert "deltaE = 0.01" in text


def test_write_dos_input_gaussian_smearing(tmp_path):
    """Legacy / opt-in path: caller passes ngauss=0 + degauss=0.0015 (≈ 0.02 eV)
    to force Gaussian smearing in dos.x. No longer recommended for bench
    GT comparison (FIX-9), but helper must still emit these lines verbatim."""
    out = tmp_path / "dos.in"
    qe_tools.write_dos_input(
        prefix="job", outdir="./out",
        emin=-5, emax=5, deltaE=0.01,
        ngauss=0, degauss=0.0015,
        filename=str(out),
    )
    text = out.read_text()
    assert "ngauss = 0" in text
    assert "degauss = 0.0015" in text


def test_write_dos_input_only_ngauss(tmp_path):
    """Asymmetric: only ngauss without degauss should still emit a valid
    file (QE will fall back to its built-in default degauss)."""
    out = tmp_path / "dos.in"
    qe_tools.write_dos_input(
        prefix="job", outdir="./out",
        ngauss=0,
        filename=str(out),
    )
    text = out.read_text()
    assert "ngauss = 0" in text
    assert "degauss" not in text


# ─── FIX-9 regression guard ─────────────────────────────────────────────────
#
# 2026-04-30: bench GT switched MP→self-run + tetrahedra. The earlier prompt
# advice to set Gaussian smearing in dos.x ("# match GT smearing") was
# silently invalidated, leaving 86 T2 tasks scoring ~0.6-0.7 on dos_curve.
# FIX-9 reverts the prompt to teach the tetrahedra default. This test
# catches any future re-introduction of the stale "match GT" Gaussian
# claim in the system prompt.

def test_prompt_does_not_teach_gaussian_match_gt():
    """Regression guard: system_prompt must not contain the stale claim
    that Gaussian dos.x smearing matches GT. GT switched to tetrahedra
    on 2026-04-30; any future edit that re-introduces 'match GT smearing'
    or the `ngauss=0, degauss=0.0015` example will fail here."""
    from agent.system_prompt import build_qe_system_prompt
    p = build_qe_system_prompt(
        workdir="/tmp/stub-workdir",
        task_outputs_keys=["scf_out", "bands_out", "bands_data", "dos_dat"],
        pw_x_path="/usr/bin/pw.x",
        repo_root=str(REPO_ROOT),
    )
    # Reject the exact stale phrases that drove the BN_hex bug:
    assert "match GT smearing" not in p, (
        "Stale advice resurfaced: 'match GT smearing'. GT uses tetrahedra "
        "since 2026-04-30; do not set ngauss/degauss in dos.in."
    )
    assert "ngauss = 0" not in p, (
        "Stale Gaussian example resurfaced in prompt — see FIX-9."
    )
    assert "degauss = 0.0015" not in p, (
        "Stale Gaussian example resurfaced in prompt — see FIX-9."
    )
    # Sanity: the new tetrahedra guidance must be there.
    assert "tetrahedra" in p
    assert "DO NOT" in p  # Step 3 forbids ngauss/degauss explicitly


def test_prompt_does_not_leak_gt_answer_values():
    """Anti-leak guard (FIX-43): the executor system prompt must not embed
    literal ground-truth answer values for specific bench materials, nor
    attribute parameters to 'the bench GT'. The engineering numbers (80 Ry,
    tetrahedra) and the no-+U guidance survive; the GT answers + attribution go."""
    from agent.system_prompt import build_qe_system_prompt
    p = build_qe_system_prompt(
        workdir="/tmp/stub-workdir",
        task_outputs_keys=["scf_out", "bands_out", "bands_data", "dos_dat"],
        pw_x_path="/usr/bin/pw.x",
        repo_root=str(REPO_ROOT),
    )
    p_cd = build_qe_system_prompt(
        workdir="/tmp/stub-workdir",
        task_outputs_keys=["final_structure"],   # T1 path
        pw_x_path="/usr/bin/pw.x",
        repo_root=str(REPO_ROOT),
        cutoff_sensitive_metal=True,             # renders the Cd directive
    )
    both = p + "\n" + p_cd
    # literal GT answers for specific bench materials must be gone
    for leak in ("GT: 4.14", "GT=1.78", "GT a=3.05", "c=5.65",
                 "Γ-X 1.50", "45.7"):
        assert leak not in both, f"GT answer leaked into prompt: {leak!r}"
    # GT-protocol / bench-marker attribution must be gone
    for leak in ("GT canonical", "match bench GT", "bench GT reports",
                 "bench GT pipeline", "matches GT shape"):
        assert leak not in both, f"GT attribution leaked into prompt: {leak!r}"
    # engineering guidance preserved (D1-B keeps the numbers)
    assert "80" in p and "tetrahedra" in p


# ─── FIX-20 regression guard ────────────────────────────────────────────────
#
# 2026-05-22 [59a8e80] bundled an unannounced change with FIX-9: bumped the
# Plan Step 2 + vc-relax canonical template cutoffs from 50 Ry / 6×6×6
# (T1-appropriate) to 80 Ry / kmesh=ceil(50/a) min 8×8×8 (T2 canonical).
# Result: CsPbI3 (20-atom orthorhombic, c=18 Å) and WO3 timed out at 4 h —
# one BFGS step's SCF didn't fit on the 11×8×8 + 80 Ry input. Other
# materials in the batch hit the same pattern.
#
# FIX-20 splits the cutoff guidance: T1 (vc_relax-only) uses 50 Ry +
# kmesh=ceil(25/a) min 4×4×4 — the same regime that produced 1.0 scores
# in May 2026. T2 keeps the 80 Ry GT canonical (evaluator parses gap and
# Fermi at ±0.1 eV; needs the resolution).

def test_t1_prompt_does_not_recommend_t2_canonical_cutoffs():
    """Regression guard: T1 (vc_relax-only) prompt must NOT teach the
    80 Ry / dense k-mesh T2 canonical in the Plan + vc-relax canonical
    sections. T1 only scores lattice constants and space group — 80 Ry
    is overkill and timed out CsPbI3 + WO3 on 2026-05-22. See devlog for
    FIX-20.

    Scope: assertions cover the *vc-relax-relevant prefix* of the prompt,
    up to the "### bands + DOS workflow" subsection. The bands+DOS
    section legitimately keeps 80 Ry / ceil(50/a) — that's the T2
    workflow's intrinsic guidance and is harmless for T1 since the T1
    executor never runs SCF/bands/DOS.
    """
    from agent.system_prompt import build_qe_system_prompt
    p = build_qe_system_prompt(
        workdir="/tmp/stub-workdir",
        task_outputs_keys=["vc_relax_out"],
        pw_x_path="/usr/bin/pw.x",
        repo_root=str(REPO_ROOT),
    )
    # Scope assertions to the part of the prompt the T1 executor will
    # actually act on.
    bands_idx = p.find("### bands + DOS workflow")
    assert bands_idx > 0, "expected bands+DOS workflow heading in prompt"
    t1_section = p[:bands_idx]

    # Plan Step 2 + vc-relax canonical template must show the T1 numbers.
    # (The "DO NOT use 80 Ry" warning legitimately mentions "80 Ry" in
    # its prose, so we check the **bolded recommendation** form, which
    # only appears for the chosen value.)
    assert "ecutwfc **50 Ry**" in t1_section, (
        "T1 Plan Step 2 should recommend ecutwfc=50 Ry."
    )
    assert "ecutwfc **80 Ry**" not in t1_section, (
        "T1 prompt's vc-relax section resurfaces 80 Ry recommendation — "
        "see FIX-20. 80 Ry is T2 canonical; T1 should use 50."
    )
    assert "ceil(25 / a_Å) per axis, min 4×4×4" in t1_section, (
        "T1 k-mesh formula should be the sparser ceil(25/a) min 4×4×4."
    )
    assert "ceil(50 / a_Å) per axis, min 8×8×8" not in t1_section, (
        "T1 prompt's vc-relax section resurfaces the T2 k-mesh formula — "
        "see FIX-20."
    )
    # The canonical vc-relax &SYSTEM block must also be T1-appropriate.
    assert "ecutwfc     = 50" in t1_section
    assert "ecutrho     = 500" in t1_section
    assert "ecutwfc     = 80" not in t1_section
    assert "ecutrho     = 800" not in t1_section
    # The qe_tools.write_pw_input helper example must match.
    assert "ecutwfc=50, ecutrho=500" in t1_section
    assert "ecutwfc=80, ecutrho=800" not in t1_section


def test_t2_prompt_keeps_canonical_cutoffs():
    """Regression guard: T2 (bands_dos) prompt must KEEP the 80 Ry / kmesh
    ceil(50/a) min 8×8×8 GT canonical — the evaluator parses gap and Fermi
    at the 0.1 eV level and that resolution requires those cutoffs."""
    from agent.system_prompt import build_qe_system_prompt
    p = build_qe_system_prompt(
        workdir="/tmp/stub-workdir",
        task_outputs_keys=["scf_out", "bands_out", "bands_data", "dos_dat"],
        pw_x_path="/usr/bin/pw.x",
        repo_root=str(REPO_ROOT),
    )
    # T2 path keeps the GT-canonical 80 Ry guidance.
    assert "ecutwfc **80 Ry**" in p
    assert "ceil(50 / a_Å) per axis, min 8×8×8" in p
    # The canonical vc-relax template is also at 80 (some T2 agents may
    # re-do vc-relax post-prerelax; cutoffs must match the SCF/bands ones).
    assert "ecutwfc     = 80" in p
    assert "ecutrho     = 800" in p
    # Dos.x bands+DOS section also keeps 80 Ry.
    assert "ecutwfc       = 80" in p  # bands+DOS section's K_POINTS table

    # The T1-specific "DO NOT use 80 Ry" warning must NOT appear in T2.
    assert "DO NOT use 80 Ry" not in p, (
        "T2 prompt should not contain the T1 warning against 80 Ry."
    )


def test_t1_t2_dispatch_uses_required_keys():
    """The T1/T2 cutoff split keys off `task_outputs_keys`. Confirm the
    dispatch matches solve.py:is_bands_dos_task — bands_data OR dos_dat
    triggers T2; anything else is T1."""
    from agent.system_prompt import build_qe_system_prompt
    common = dict(
        workdir="/tmp/x", pw_x_path="/usr/bin/pw.x", repo_root=str(REPO_ROOT)
    )
    # Various T1 shapes
    for keys in [["vc_relax_out"], ["relax_out_path"], ["something_else"]]:
        p = build_qe_system_prompt(task_outputs_keys=keys, **common)
        assert "ecutwfc **50 Ry**" in p, f"T1 dispatch failed for keys={keys}"
    # T2 trigger: bands_data
    p = build_qe_system_prompt(task_outputs_keys=["bands_data"], **common)
    assert "ecutwfc **80 Ry**" in p
    # T2 trigger: dos_dat alone
    p = build_qe_system_prompt(task_outputs_keys=["dos_dat"], **common)
    assert "ecutwfc **80 Ry**" in p


# ─── FIX-31 regression guard ──────────────────────────────────────────────
#
# 2026-05-27 [FIX-31]: T1 default ecutwfc=50 (per FIX-20) is too sparse for
# GBRV USPP Group 12 metals (Cd / Zn). 2026-05-25T16-59 Cd vc_relax at
# 50 Ry / 12×12×6 scored 0.445 (extracted a/c off by ~0.045 Å / 0.22 Å vs
# GT, 1% rel tolerance blown). The cutoff_sensitive_metal flag from
# config_advisor flips the T1 branch onto T2-canonical 80 Ry + k≥8.

def test_t1_cutoff_sensitive_metal_uses_t2_canonical():
    """When the cutoff_sensitive_metal flag is True, T1 vc-relax-only path
    must use the T2-canonical 80 Ry + ceil(50/a) min 8×8×8 numbers — not
    the FIX-20 50/4³ defaults."""
    from agent.system_prompt import build_qe_system_prompt
    p = build_qe_system_prompt(
        workdir="/tmp/stub-workdir",
        task_outputs_keys=["vc_relax_out"],  # T1
        pw_x_path="/usr/bin/pw.x",
        repo_root=str(REPO_ROOT),
        cutoff_sensitive_metal=True,
    )
    bands_idx = p.find("### bands + DOS workflow")
    assert bands_idx > 0
    t1_section = p[:bands_idx]
    # T1 with the flag = 80 Ry / k≥8 (same numbers as T2)
    assert "ecutwfc **80 Ry**" in t1_section
    assert "ecutwfc **50 Ry**" not in t1_section
    assert "ceil(50 / a_Å) per axis, min 8×8×8" in t1_section
    assert "ceil(25 / a_Å) per axis, min 4×4×4" not in t1_section
    assert "ecutwfc     = 80" in t1_section
    assert "ecutrho     = 800" in t1_section
    assert "ecutwfc=80, ecutrho=800" in t1_section
    # The "DO NOT use 80 Ry" warning must be suppressed when the flag is on
    assert "DO NOT use 80 Ry" not in t1_section
    # FIX-31 rationale must be visible so the LLM doesn't fight the prompt
    assert "FIX-31" in t1_section


def test_t1_without_flag_keeps_fix20_defaults():
    """Default T1 path (flag absent or False) must keep the FIX-20 50 Ry /
    4³ regime intact — otherwise we'd reintroduce CsPbI3 / WO3 timeouts."""
    from agent.system_prompt import build_qe_system_prompt
    common = dict(
        workdir="/tmp/x",
        task_outputs_keys=["vc_relax_out"],
        pw_x_path="/usr/bin/pw.x",
        repo_root=str(REPO_ROOT),
    )
    # default
    p_default = build_qe_system_prompt(**common)
    # explicit False
    p_explicit = build_qe_system_prompt(cutoff_sensitive_metal=False, **common)
    for p in (p_default, p_explicit):
        bands_idx = p.find("### bands + DOS workflow")
        t1_section = p[:bands_idx]
        assert "ecutwfc **50 Ry**" in t1_section
        assert "ecutwfc **80 Ry**" not in t1_section
        assert "ceil(25 / a_Å) per axis, min 4×4×4" in t1_section
        assert "DO NOT use 80 Ry" in t1_section
        # FIX-31 rationale must NOT leak into the no-flag path
        assert "FIX-31" not in t1_section


def test_t2_ignores_cutoff_sensitive_flag():
    """T2 (bands_dos) prompt is already at 80 Ry / k≥8; the flag must not
    change its output. (Sanity: ensures the flag only affects the T1 elif
    branch, not the T2 branch — otherwise a future refactor could leak.)"""
    from agent.system_prompt import build_qe_system_prompt
    common = dict(
        workdir="/tmp/x",
        task_outputs_keys=["bands_data", "dos_dat"],
        pw_x_path="/usr/bin/pw.x",
        repo_root=str(REPO_ROOT),
    )
    p_off = build_qe_system_prompt(**common)
    p_on = build_qe_system_prompt(cutoff_sensitive_metal=True, **common)
    # Functionally identical for T2 — the elif branch is unreachable when
    # is_bands_dos_task=True.
    bands_idx = p_off.find("### bands + DOS workflow")
    assert "ecutwfc **80 Ry**" in p_off[:bands_idx]
    assert "ecutwfc **80 Ry**" in p_on[:bands_idx]
    # T2 must not pick up the FIX-31 rationale text
    assert "FIX-31" not in p_off[:bands_idx]
    assert "FIX-31" not in p_on[:bands_idx]


# ─── FIX-24 regression guard ──────────────────────────────────────────────
#
# 2026-05-25 Ti3O5 5-25 run: advisor=metal (FIX-22), bands NSCF k-path was
# seekpath verbatim default (~20 vertices, no per-segment density), bands.dat
# all bands on one side of E_F at every sampled k-point → evaluator's
# fermi-slicer fallback: gap_type='direct', band_gap=0.002 eV, vbm_k_label=
# fractional-coord string. Lost full 0.35 score on rule=exact mismatch.
# FIX-24 adds metal-specific NSCF k-path / nbnd guidance in Step 2.

def test_t2_prompt_contains_metal_kpath_guidance():
    """T2 prompt must educate the agent on metal-phase k-path density +
    nbnd requirement (FIX-24, 2026-05-25)."""
    from agent.system_prompt import build_qe_system_prompt
    p = build_qe_system_prompt(
        workdir="/tmp/stub-workdir",
        task_outputs_keys=["scf_out", "bands_out", "bands_data", "dos_dat"],
        pw_x_path="/usr/bin/pw.x",
        repo_root=str(REPO_ROOT),
    )
    # The FIX-24 callout is keyed off advisor.is_metal=True
    assert "FIX-24" in p, "FIX-24 metal-kpath callout missing from T2 prompt"
    assert "is_metal=True" in p
    # Concrete densification guidance
    assert "50 subdivisions per segment" in p or ">= 50" in p
    # nbnd formula reference
    assert "nelec" in p and "nbnd" in p
    # The score-cost framing (motivates LLM to take this seriously)
    assert "0.35" in p
    # Sanity check the agent can perform: bands should straddle E_F
    assert "straddle" in p or "on the SAME side" in p


def test_fix24_callout_lives_in_bands_dos_section_not_vcrelax_prefix():
    """FIX-24's metal-kpath guidance belongs to the bands NSCF Step 2.
    It MAY appear in both T1 and T2 prompts (the bands+DOS workflow
    section is shared content per existing convention; T1 executor
    just doesn't act on it), but it MUST NOT pollute the vc-relax
    canonical prefix that the T1 executor reads."""
    from agent.system_prompt import build_qe_system_prompt
    p = build_qe_system_prompt(
        workdir="/tmp/stub-workdir",
        task_outputs_keys=["vc_relax_out"],
        pw_x_path="/usr/bin/pw.x",
        repo_root=str(REPO_ROOT),
    )
    bands_idx = p.find("### bands + DOS workflow")
    assert bands_idx > 0
    vc_relax_prefix = p[:bands_idx]
    # FIX-24 callout must NOT leak into the T1-relevant vc-relax prefix
    assert "FIX-24" not in vc_relax_prefix, (
        "FIX-24 metal-kpath callout leaked above the bands+DOS heading "
        "— it must stay inside the Step 2 bands NSCF subsection."
    )
    # But the bands+DOS suffix (shared) does contain it
    assert "FIX-24" in p[bands_idx:]


# Root cause of 2026-05-25T23-40Z M1_T2_C_diamond_bands_dos drop to 0.348
# (gap=5.581 direct GAMMA vs GT 4.1449 indirect OFF_PATH): vc_relax with
# nosym=.true. allowed ~5e-5 asymmetric drift in the relaxed cell, then
# the executor called `seekpath.get_path(...)` at the default symprec=1e-5
# → spglib labeled diamond as monoclinic C2/m → returned a path with no
# Γ-X segment → bands missed the indirect CBM. FIX-30 teaches the executor
# to pass symprec=1e-3 in every seekpath.get_path call.

def test_t2_prompt_teaches_seekpath_symprec():
    """Bands NSCF guidance must pass symprec=1e-3 to seekpath.get_path
    (FIX-30, 2026-05-27). Default 1e-5 misclassifies cells with ~1e-5
    asymmetric drift, producing a path that misses BZ extrema."""
    from agent.system_prompt import build_qe_system_prompt
    p = build_qe_system_prompt(
        workdir="/tmp/stub-workdir",
        task_outputs_keys=["scf_out", "bands_out", "bands_data", "dos_dat"],
        pw_x_path="/usr/bin/pw.x",
        repo_root=str(REPO_ROOT),
    )
    # The FIX-30 callout
    assert "FIX-30" in p, "FIX-30 symprec callout missing from T2 prompt"
    # Every example invocation of seekpath.get_path in the T2 path-building
    # section must pass symprec=1e-3 — no bare calls allowed.
    import re
    bare_calls = re.findall(
        r"seekpath\.get_path\(\([^)]*\)\)(?!\s*,)",  # ((...)) with no trailing arg
        p,
    )
    assert not bare_calls, (
        f"Found seekpath.get_path call(s) without symprec kwarg: {bare_calls}. "
        "FIX-30: every example must pass symprec=1e-3."
    )
    # And at least one explicit symprec=1e-3 should be visible to anchor the
    # convention for the LLM to copy.
    assert "symprec=1e-3" in p, "T2 prompt must show symprec=1e-3 explicitly"


def test_vc_relax_subagent_handoff_uses_seekpath_symprec():
    """The vc_relax subagent's handoff block (injected at the top of the
    main solver's system prompt after vc_relax succeeds) must also
    instruct symprec=1e-3 when calling seekpath — vc_relax outputs are
    the highest-risk input distribution for spglib misclassification."""
    from agent.subagents.vc_relax import RelaxResult, render_prerelax_success
    stub = RelaxResult(
        ok=True,
        relaxed_cell="CELL_PARAMETERS angstrom\n  1.0 0.0 0.0\n  0.0 1.0 0.0\n  0.0 0.0 1.0",
        relaxed_positions="ATOMIC_POSITIONS crystal\n  Si 0.0 0.0 0.0",
        relax_out_path="vc_relax/relax.out",
        notes="stub",
    )
    msg = render_prerelax_success(stub)
    assert "symprec=1e-3" in msg, (
        "vc_relax handoff must teach symprec=1e-3 in its seekpath instruction "
        "(FIX-30) — vc_relax outputs are the highest-risk input for spglib "
        "misclassification."
    )


# ─── parse_bands_dat_plot (FIX-2): QE filband &plot format ──────────────────

def test_parse_bands_dat_plot_matches_filband_layout(tmp_path):
    """FIX-2: parse_bands_dat_plot reads the QE bands.x `filband` (&plot) layout
    the bench scores — eigenvalues may wrap across lines; result must be nks×nbnd.
    Mirrors harness/lib/evaluator.parse_bands_dat so the solver's near-E_F void
    measurement equals the bench's gap extraction."""
    # nbnd=3, nks=2, eigenvalues wrapped over two lines per k-block
    txt = (
        " &plot nbnd=  3, nks=   2 /\n"
        "           0.000000  0.000000  0.000000\n"
        "   -5.1234   3.4567\n"
        "    8.9000\n"
        "           0.500000  0.000000  0.500000\n"
        "   -4.9876   3.2109   8.5000\n"
    )
    p = tmp_path / "bands.dat"
    p.write_text(txt)
    out = qe_tools.parse_bands_dat_plot(str(p))
    assert out["nbnd"] == 3 and out["nks"] == 2
    assert out["eigenvalues_ev"] == [[-5.1234, 3.4567, 8.9000],
                                     [-4.9876, 3.2109, 8.5000]]


def test_parse_bands_dat_plot_truncated_recovers_nks(tmp_path):
    """A truncated bands.dat (fewer floats than nks×block) recovers the usable
    nks rather than raising — matches the bench parser's defensive behavior."""
    txt = (
        " &plot nbnd=  2, nks=   5 /\n"
        "           0.0  0.0  0.0\n"
        "   1.1   2.2\n"
        "           0.5  0.0  0.5\n"
        "   1.3   2.4\n"
    )
    p = tmp_path / "bands.dat"
    p.write_text(txt)
    out = qe_tools.parse_bands_dat_plot(str(p))
    assert out["nks"] == 2  # only 2 complete blocks present
    assert out["eigenvalues_ev"] == [[1.1, 2.2], [1.3, 2.4]]
