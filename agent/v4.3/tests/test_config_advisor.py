"""Unit tests for the config_advisor preflight subagent.

Covers deterministic surface (no LLM):
  - METAL_ELEMENTS coverage / sanity
  - _parse_elements_from_formula on tricky formulas
  - _deterministic_classify on insulator / semiconductor / metal / intermetallic
  - _validate_payload happy path + fallback to deterministic
  - render_config_advice_block content for metal vs insulator
  - ConfigAdvice.to_json round-trip
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from agent.subagents import config_advisor as ca  # noqa: E402
from agent.subagents._metals import (  # noqa: E402
    CUTOFF_SENSITIVE_METALS,
    METAL_ELEMENTS,
    METALLIC_TM_OXIDES,
    PBE_NEAR_METAL_FORMULAS,
    STRONGLY_ELECTRONEGATIVE,
)


# ---------------------------------------------------------------------------
# Metal table sanity
# ---------------------------------------------------------------------------


class TestMetalTable:
    def test_alkalis_in(self):
        for e in ("Li", "Na", "K", "Rb", "Cs"):
            assert e in METAL_ELEMENTS

    def test_transition_metals_in(self):
        for e in ("Cu", "Fe", "Ni", "Ag", "Au", "Pt", "W", "Mo"):
            assert e in METAL_ELEMENTS

    def test_semiconductors_out(self):
        for e in ("B", "C", "Si", "Ge", "P", "S", "Se"):
            assert e not in METAL_ELEMENTS

    def test_electronegatives_separate_set(self):
        for e in ("O", "F", "Cl", "N"):
            assert e in STRONGLY_ELECTRONEGATIVE
            assert e not in METAL_ELEMENTS


# ---------------------------------------------------------------------------
# Formula parsing
# ---------------------------------------------------------------------------


class TestParseElements:
    @pytest.mark.parametrize("formula,expected", [
        ("NaCl",      ["Na", "Cl"]),
        ("Al2O3",     ["Al", "O"]),
        ("LiFePO4",   ["Li", "Fe", "P", "O"]),
        ("Si",        ["Si"]),
        ("Cu",        ["Cu"]),
        ("Fe3O4",     ["Fe", "O"]),
        ("CaCO3",     ["Ca", "C", "O"]),
        ("Ca(OH)2",   ["Ca", "O", "H"]),
        ("",          []),
    ])
    def test_parse_formula(self, formula, expected):
        assert ca._parse_elements_from_formula(formula) == expected


# ---------------------------------------------------------------------------
# Deterministic classifier
# ---------------------------------------------------------------------------


def _make_prompt(formula: str, structure: str = "fcc",
                 pseudo: str = "GBRV_USPP_PBE_v1.5") -> str:
    return (f"For {formula} ({structure}, space group Fm-3m, 2 atoms)...\n"
            f"Pseudopotentials: {pseudo} (scalar-relativistic).\n")


class TestDeterministicClassify:
    @pytest.mark.parametrize("formula,expect_metal", [
        # Pure metals
        ("Ca",       True),
        ("Cu",       True),
        ("Fe",       True),
        ("Mg",       True),
        ("Ag",       True),
        ("Au",       True),
        ("Al",       True),
        ("Li",       True),
        # Semiconductors (covalent, no electronegative anion)
        ("Si",       False),
        ("Ge",       False),
        # Compounds with electronegative atoms → insulator/semiconductor
        ("NaCl",     False),
        ("AlN",      False),
        ("CuO",      False),
        ("Fe2O3",    False),
        ("Al2O3",    False),
        ("LiFePO4",  False),
        ("ZnO",      False),
        ("MgO",      False),
        # Intermetallic (all elements metallic) → metal
        ("Cu3Au",    True),
        ("FeAl",     True),
    ])
    def test_metal_classification(self, formula, expect_metal):
        prompt = _make_prompt(formula)
        d = ca._deterministic_classify(prompt)
        assert d["is_metal"] == expect_metal, \
            f"{formula}: expected metal={expect_metal}, got {d['is_metal']}, rationale={d['rationale']}"

    def test_uspp_detection(self):
        prompt = _make_prompt("Si", pseudo="GBRV_USPP_PBE_v1.5")
        d = ca._deterministic_classify(prompt)
        assert d["is_uspp"]
        assert d["ecutrho_factor"] == 10.0

    def test_metal_occupations(self):
        d = ca._deterministic_classify(_make_prompt("Ca"))
        assert d["occupations"] == "smearing"
        assert d["smearing"] == "mv"
        assert d["degauss"] == 0.02

    def test_insulator_occupations(self):
        d = ca._deterministic_classify(_make_prompt("NaCl", structure="rocksalt"))
        assert d["occupations"] == "fixed"
        assert d["smearing"] is None
        assert d["degauss"] is None

    def test_magnetic_metal_gets_gauss_not_mv(self):
        # FIX-33: elemental magnetic metals (Fe/Co/Ni) must NOT inherit the
        # non-magnetic METAL path's mv/0.02 — that collapses the itinerant
        # moment (Ni_fcc 7.85→0.00 µB). Deterministic classify pins gauss/0.01.
        for el in ("Fe", "Co", "Ni"):
            d = ca._deterministic_classify(_make_prompt(el, structure="bcc"))
            assert d["is_metal"] is True, el
            assert d["is_magnetic"] is True, el
            assert d["occupations"] == "smearing", el
            assert d["smearing"] == "gauss", f"{el}: expected gauss, got {d['smearing']}"
            assert d["degauss"] == 0.01, el

    def test_extracts_formula(self):
        prompt = _make_prompt("NaCl", structure="rocksalt")
        d = ca._deterministic_classify(prompt)
        assert d["formula"] == "NaCl"
        assert d["structure_name"] == "rocksalt"
        assert d["elements"] == ["Na", "Cl"]


# ---------------------------------------------------------------------------
# FIX-3b: PBE 准金属族标记
# ---------------------------------------------------------------------------


class TestPbeNearMetal:
    """PBE 给近零 gap 的窄带半导体应被 advisor 标记 pbe_near_metal=True，
    但 is_metal 仍是 False（物理事实）。physics_sanity 看到此 flag 后
    metal⇄semiconductor 矛盾降级为 soft_warn。

    触发案例：Ge bands_dos 得分卡 0.63（standard-bench-failures
    报告 §3）。"""

    def test_ge_marked_pbe_near_metal(self):
        d = ca._deterministic_classify(_make_prompt("Ge", structure="diamond"))
        assert d["pbe_near_metal"] is True
        assert d["is_metal"] is False, "Ge 物理上仍是半导体（advisor 不改判）"

    def test_insb_marked_pbe_near_metal(self):
        d = ca._deterministic_classify(_make_prompt("InSb", structure="zincblende"))
        assert d["pbe_near_metal"] is True

    def test_hgte_marked_pbe_near_metal(self):
        d = ca._deterministic_classify(_make_prompt("HgTe", structure="zincblende"))
        assert d["pbe_near_metal"] is True

    def test_si_not_marked_pbe_near_metal(self):
        # Si gap PBE ~ 0.6 eV，不是 PBE 准金属
        d = ca._deterministic_classify(_make_prompt("Si", structure="diamond"))
        assert d["pbe_near_metal"] is False

    def test_rb_not_marked_pbe_near_metal(self):
        # Rb 是真金属，不是 PBE 准金属
        d = ca._deterministic_classify(_make_prompt("Rb", structure="bcc"))
        assert d["pbe_near_metal"] is False
        assert d["is_metal"] is True

    def test_nacl_not_marked_pbe_near_metal(self):
        # NaCl 是真绝缘体（PBE gap 5+ eV）
        d = ca._deterministic_classify(_make_prompt("NaCl", structure="rocksalt"))
        assert d["pbe_near_metal"] is False

    def test_pbte_marked_pbe_near_metal(self):
        d = ca._deterministic_classify(_make_prompt("PbTe", structure="rocksalt"))
        assert d["pbe_near_metal"] is True

    def test_pbe_near_metal_set_is_disjoint_from_strongly_electronegative_elements(self):
        # PBE_NEAR_METAL_FORMULAS 是 formula 集合，不应混淆 elements 集
        # （比如 "Te" 是元素不是 formula，不该在这里）
        for f in PBE_NEAR_METAL_FORMULAS:
            # 至少 formula 不是单元素电负性原子（O/F 等）
            assert f not in STRONGLY_ELECTRONEGATIVE

    def test_advice_dataclass_propagates_pbe_near_metal(self):
        det = ca._deterministic_classify(_make_prompt("Ge", structure="diamond"))
        advice = ca._from_deterministic(det)
        assert advice.pbe_near_metal is True
        # JSON 序列化也得有
        import json as _j
        d = _j.loads(advice.to_json())
        assert d["pbe_near_metal"] is True

    def test_advice_dataclass_pbe_near_metal_false_for_normal(self):
        det = ca._deterministic_classify(_make_prompt("Si", structure="diamond"))
        advice = ca._from_deterministic(det)
        assert advice.pbe_near_metal is False


# ---------------------------------------------------------------------------
# FIX-8: pbe_near_metal 强制 smearing
# ---------------------------------------------------------------------------


class TestPbeNearMetalForcesSmearing:
    """FIX-8 (2026-05-22)：当 pbe_near_metal=True 时，无论 is_metal 还是 LLM
    cross-check 怎么填，advisor 都必须强制 occupations='smearing' / mv / 0.02。
    目的是让 bench evaluator 走 fermi-splitter (5 meV eps) 而不是 homo_lumo
    主路径 (1 meV 严苛阈)。"""

    def test_ge_deterministic_gets_smearing(self):
        d = ca._deterministic_classify(_make_prompt("Ge", structure="diamond"))
        assert d["pbe_near_metal"] is True
        assert d["is_metal"] is False, "Ge 物理上仍是半导体"
        assert d["occupations"] == "smearing", "FIX-8 强制 smearing"
        assert d["smearing"] == "mv"
        assert d["degauss"] == 0.02

    def test_insb_deterministic_gets_smearing(self):
        d = ca._deterministic_classify(_make_prompt("InSb", structure="zincblende"))
        assert d["occupations"] == "smearing"
        assert d["degauss"] == 0.02

    def test_si_still_fixed(self):
        # Si 不是 PBE-near-metal (gap ~0.6 eV)，应保持 fixed
        d = ca._deterministic_classify(_make_prompt("Si", structure="diamond"))
        assert d["pbe_near_metal"] is False
        assert d["occupations"] == "fixed"
        assert d["smearing"] is None

    def test_validate_payload_llm_cannot_regress_smearing_to_fixed(self):
        # LLM cross-check 看到 is_metal=False 想把 occupations 改回 fixed，
        # FIX-8 的 re-apply 必须把它再次拉回 smearing。
        det = ca._deterministic_classify(_make_prompt("Ge", structure="diamond"))
        payload = {"task_outputs": {
            "is_metal": "false",
            "occupations": "fixed",     # LLM 试图回退
            "smearing": "none",
            "degauss": "none",
            "ecutrho_factor": "10",
            "rationale": "Ge is covalent semiconductor",
        }}
        adv = ca._validate_payload(payload, det)
        assert adv.is_metal is False, "is_metal 物理事实保留"
        assert adv.occupations == "smearing", "FIX-8 re-apply 必须覆盖 LLM"
        assert adv.smearing == "mv"
        assert adv.degauss == 0.02

    def test_validate_payload_normal_semi_keeps_fixed(self):
        # NaCl 是真绝缘体，LLM 同意 fixed，结果应保持 fixed
        det = ca._deterministic_classify(_make_prompt("NaCl", structure="rocksalt"))
        payload = {"task_outputs": {
            "is_metal": "false", "occupations": "fixed",
            "smearing": "none", "degauss": "none",
            "ecutrho_factor": "10", "rationale": "ionic insulator",
        }}
        adv = ca._validate_payload(payload, det)
        assert adv.occupations == "fixed"
        assert adv.smearing is None

    def test_render_block_pbe_near_metal_tags_classification(self):
        # FIX-29: Classification 行标 "(PBE-near-metal — smearing SCF, fixed bands NSCF ...)"
        det = ca._deterministic_classify(_make_prompt("Ge", structure="diamond"))
        adv = ca._from_deterministic(det)
        block = ca.render_config_advice_block(adv)
        assert "PBE-near-metal" in block
        assert "fixed bands NSCF" in block

    def test_render_block_pbe_near_metal_includes_why_explanation(self):
        # FIX-29: WHY 段解释 "SCF smearing 保收敛 + bands NSCF fixed 保抽取" 的拆分，
        # 取代旧的 "WHY smearing on a semiconductor → fermi-splitter" 说辞。
        det = ca._deterministic_classify(_make_prompt("Ge", structure="diamond"))
        adv = ca._from_deterministic(det)
        block = ca.render_config_advice_block(adv)
        assert "WHY smearing on SCF but fixed on the bands NSCF" in block
        assert "bands NSCF" in block
        # Ge 名字应出现在解释里
        assert "Ge" in block

    def test_render_block_pbe_near_metal_directs_smearing_scf_fixed_bands(self):
        # FIX-29: smearing 只覆盖 vc-relax + SCF；bands NSCF 用 fixed。
        # （旧 FIX-8 行为 "vc-relax + SCF + bands NSCF" 三阶段 smearing 已废止。）
        det = ca._deterministic_classify(_make_prompt("Ge", structure="diamond"))
        adv = ca._from_deterministic(det)
        block = ca.render_config_advice_block(adv)
        assert "vc-relax + SCF only" in block
        assert "occupations='fixed'" in block            # bands NSCF fixed
        assert "vc-relax + SCF + bands NSCF" not in block

    def test_render_block_normal_metal_unchanged(self):
        # 真金属（Ca）的 render 不应受 FIX-8 影响 — 仍走 METAL 分类、smearing 指令
        det = ca._deterministic_classify(_make_prompt("Ca"))
        adv = ca._from_deterministic(det)
        block = ca.render_config_advice_block(adv)
        assert "METAL" in block
        assert "PBE-near-metal" not in block, "Ca 不是 PBE-near-metal"

    def test_render_block_normal_insulator_unchanged(self):
        # 真绝缘体（NaCl）的 render 不应受 FIX-8 影响 — 仍走 fixed 指令
        det = ca._deterministic_classify(_make_prompt("NaCl", structure="rocksalt"))
        adv = ca._from_deterministic(det)
        block = ca.render_config_advice_block(adv)
        assert "occupations='fixed'" in block
        assert "PBE-near-metal" not in block
        assert "WHY smearing" not in block


# ---------------------------------------------------------------------------
# FIX-29: PBE-near-metal bands NSCF must use fixed (not smearing)
# ---------------------------------------------------------------------------


class TestPbeNearMetalBandsNscfFixed:
    """FIX-29 (2026-05-27): PBE-near-metal 半导体（CoSb3/Ge/...）的 bands NSCF
    必须用 `occupations='fixed'`（不是 smearing）；vc-relax + SCF 仍 smearing 保收敛。

    根因（CoSb3 2026-05-25 实测 0.16）：bands NSCF 一旦 smearing，QE 不打
    `highest occupied, lowest unoccupied level` 行 → bench evaluator 退回
    fermi-splitter，而 degauss=0.02 Ry (~0.27 eV) 宽于 ~0.15 eV 真带隙、把 Fermi
    拉到真 VBM 下方 ~0.25 eV → 误切价带 → band_gap/gap_type/vbm/cbm + dos_e_fermi
    五项全 0（dos_e_fermi 也跟着 splitter 的 garbage VBM 走，evaluator.py:783）。
    fixed 让 QE 打 homo/lumo 行 → 走权威 splitter；真·近零隙由 evaluator 的 10 meV
    短路（议题1）兜住，Ge 类不回退。gate 用 pbe_near_metal（稳定、LLM 不可改），
    不用 is_metal（CoSb3 deterministic=True、LLM 修正后 False，不稳定）。"""

    def _advice(self, *, is_metal: bool, pbe_near_metal: bool = True,
                formula: str = "CoSb3") -> "ca.ConfigAdvice":
        return ca.ConfigAdvice(
            ok=True, formula=formula, elements=["Co", "Sb"],
            structure_name="cubic skutterudite",
            pseudo_family="GBRV_USPP_PBE_v1.5", is_uspp=True,
            is_metal=is_metal, occupations="smearing", smearing="mv",
            degauss=0.02, ecutrho_factor=10.0, rationale="narrow-gap skutterudite",
            source="deterministic", pbe_near_metal=pbe_near_metal,
        )

    def test_bands_nscf_directed_to_fixed(self):
        # production CoSb3: LLM 修 is_metal=False
        block = ca.render_config_advice_block(self._advice(is_metal=False))
        assert "bands NSCF" in block
        assert "occupations='fixed'" in block
        # 旧 bug 行为：smearing 三阶段全覆盖 —— 必须消失
        assert "vc-relax + SCF + bands NSCF" not in block

    def test_bands_fixed_even_when_is_metal_true(self):
        # deterministic-only fallback：CoSb3 是 is_metal=True（Co+Sb 都在 METAL_ELEMENTS）。
        # fix 按 pbe_near_metal gate，不依赖 is_metal → bands 仍 fixed。
        block = ca.render_config_advice_block(self._advice(is_metal=True))
        assert "occupations='fixed'" in block
        assert "vc-relax + SCF + bands NSCF" not in block

    def test_scf_and_vcrelax_still_smearing(self):
        block = ca.render_config_advice_block(self._advice(is_metal=False))
        # SCF/vc-relax 仍 smearing 保收敛
        assert "smearing" in block
        assert "vc-relax + SCF" in block

    def test_genuine_metal_keeps_smearing_on_bands(self):
        # 真金属（Ca，非 pbe_near_metal）bands NSCF 仍 smearing —— 回归守卫
        det = ca._deterministic_classify(_make_prompt("Ca"))
        adv = ca._from_deterministic(det)
        assert adv.is_metal is True and adv.pbe_near_metal is False
        block = ca.render_config_advice_block(adv)
        assert "vc-relax + SCF + bands NSCF" in block


# ---------------------------------------------------------------------------
# FIX-34: magnetic-insulator bands NSCF must use fixed (not smearing)
# ---------------------------------------------------------------------------


class TestMagneticInsulatorBandsNscfFixed:
    """FIX-34 (2026-05-29): magnetic insulators (CoO/NiO/MnO/FeO/Cr2O3/Fe2O3/
    CuO/MnF2/LaMnO3) — bands NSCF must use occupations='fixed' so QE prints the
    HOMO/LUMO line; vc-relax + SCF stay smearing (nspin=2 magnetic stability).
    The band-gap evaluator computes the gap from that line BEFORE its seekpath
    step, so band_gap survives even where the bench's seekpath labelling fails
    on AFM-split species (CoO 2026-05-28 scored 0.16 with smearing bands)."""

    def _mag_insulator(self):
        # NiO: magnetic insulator via the composition path → is_magnetic, is_metal=False
        det = ca._deterministic_classify(_make_prompt("NiO", structure="rocksalt"))
        assert det["is_magnetic"] is True and det["is_metal"] is False
        return ca._from_deterministic(det)

    def test_bands_nscf_directed_to_fixed(self):
        block = ca.render_config_advice_block(self._mag_insulator())
        # FIX-34 directive: bands NSCF switches to fixed (occupations directive,
        # distinct from the FIX-19 nspin-on-all-steps line).
        assert "bands NSCF MUST use `occupations='fixed'`" in block
        # old bug behaviour: the metal/all-chain smearing occ-directive
        # ("... degauss=X (Ry) for vc-relax + SCF + bands NSCF.") must be gone.
        assert "(Ry) for vc-relax + SCF + bands NSCF." not in block

    def test_scf_and_vcrelax_still_smearing_and_nspin2_kept(self):
        block = ca.render_config_advice_block(self._mag_insulator())
        assert "vc-relax + SCF" in block
        assert "smearing" in block
        assert "nspin = 2" in block  # magnetic protocol kept on every step

    def test_classification_tagged_fix34(self):
        block = ca.render_config_advice_block(self._mag_insulator())
        assert "FIX-34" in block

    def test_magnetic_metal_keeps_smearing_on_bands(self):
        # Fe_bcc: is_magnetic AND is_metal → smearing all chain (metal path).
        det = ca._deterministic_classify(_make_prompt("Fe", structure="bcc"))
        adv = ca._from_deterministic(det)
        assert adv.is_magnetic is True and adv.is_metal is True
        block = ca.render_config_advice_block(adv)
        assert "vc-relax + SCF + bands NSCF" in block
        assert "FIX-33" in block


# ---------------------------------------------------------------------------
# Payload validation
# ---------------------------------------------------------------------------


class TestValidatePayload:
    def _det(self, is_metal: bool = False) -> dict:
        return ca._deterministic_classify(
            _make_prompt("Ca" if is_metal else "NaCl",
                         structure="fcc" if is_metal else "rocksalt"),
        )

    def test_happy_path_matches_deterministic(self):
        det = self._det(is_metal=False)
        payload = {"task_outputs": {
            "is_metal": "false",
            "occupations": "fixed",
            "smearing": "none",
            "degauss": "none",
            "ecutrho_factor": "10",
            "rationale": "NaCl is ionic insulator",
        }}
        advice = ca._validate_payload(payload, det)
        assert advice.ok
        assert not advice.is_metal
        assert advice.occupations == "fixed"
        assert advice.source == "deterministic"

    def test_metal_payload(self):
        det = self._det(is_metal=True)
        payload = {"task_outputs": {
            "is_metal": "true",
            "occupations": "smearing",
            "smearing": "mv",
            "degauss": "0.02",
            "ecutrho_factor": "10",
            "rationale": "Ca is metallic fcc",
        }}
        advice = ca._validate_payload(payload, det)
        assert advice.ok
        assert advice.is_metal
        assert advice.smearing == "mv"
        assert advice.degauss == 0.02

    def test_magnetic_metal_llm_cannot_keep_mv(self):
        # FIX-33: even if the LLM cross-check echoes mv/0.02 for a magnetic
        # metal, the post-validate must pin gauss/0.01 (moment-collapse guard).
        det = ca._deterministic_classify(_make_prompt("Fe", structure="bcc"))
        assert det["is_magnetic"] and det["is_metal"]
        payload = {"task_outputs": {
            "is_metal": "true",
            "occupations": "smearing",
            "smearing": "mv", "degauss": "0.02",
            "ecutrho_factor": "10", "rationale": "Fe bcc ferromagnet",
        }}
        adv = ca._validate_payload(payload, det)
        assert adv.is_metal is True
        assert adv.smearing == "gauss", "FIX-33 must override mv → gauss"
        assert adv.degauss == 0.01

    def test_magnetic_metal_llm_cannot_regress_to_fixed(self):
        # LLM tries occupations=fixed + flips is_metal=False for Ni; FIX-33
        # restores is_metal and forces smearing gauss/0.01.
        det = ca._deterministic_classify(_make_prompt("Ni", structure="fcc"))
        payload = {"task_outputs": {
            "is_metal": "false",
            "occupations": "fixed", "smearing": "none", "degauss": "none",
            "ecutrho_factor": "10", "rationale": "Ni",
        }}
        adv = ca._validate_payload(payload, det)
        assert adv.is_metal is True, "FIX-33 restores deterministic metal truth"
        assert adv.occupations == "smearing"
        assert adv.smearing == "gauss"
        assert adv.degauss == 0.01

    def test_llm_overrides_deterministic(self):
        # Deterministic says insulator (NaCl), LLM says metal — flag as override
        det = self._det(is_metal=False)
        payload = {"task_outputs": {
            "is_metal": "true",
            "occupations": "smearing",
            "smearing": "mv",
            "degauss": "0.02",
            "ecutrho_factor": "10",
            "rationale": "overriding for testing",
        }}
        advice = ca._validate_payload(payload, det)
        assert advice.ok
        assert advice.is_metal
        assert advice.source == "llm_override"

    def test_incomplete_falls_back_to_deterministic(self):
        det = self._det(is_metal=True)
        payload = {"incomplete": True, "reason": "ran out of turns"}
        advice = ca._validate_payload(payload, det)
        # Falling back is still ok — the deterministic guess is usable
        assert advice.ok
        assert advice.is_metal  # deterministic said so

    def test_empty_task_outputs_falls_back(self):
        det = self._det(is_metal=False)
        payload = {"task_outputs": {}}
        advice = ca._validate_payload(payload, det)
        assert advice.ok
        assert advice.occupations == "fixed"  # deterministic for NaCl

    def test_metal_missing_smearing_gets_defaults(self):
        det = self._det(is_metal=True)
        payload = {"task_outputs": {
            "is_metal": "true",
            "occupations": "smearing",
            "smearing": "none",  # LLM forgot
            "degauss": "none",
        }}
        advice = ca._validate_payload(payload, det)
        assert advice.smearing == "mv"
        assert advice.degauss == 0.02


# ---------------------------------------------------------------------------
# Render helpers
# ---------------------------------------------------------------------------


class TestRender:
    def _metal_advice(self) -> ca.ConfigAdvice:
        return ca.ConfigAdvice(
            ok=True, formula="Ca", elements=["Ca"], structure_name="fcc",
            pseudo_family="GBRV_USPP_PBE_v1.5", is_uspp=True, is_metal=True,
            occupations="smearing", smearing="mv", degauss=0.02,
            ecutrho_factor=10.0, rationale="fcc metal",
            source="deterministic",
        )

    def _insulator_advice(self) -> ca.ConfigAdvice:
        return ca.ConfigAdvice(
            ok=True, formula="NaCl", elements=["Na", "Cl"],
            structure_name="rocksalt", pseudo_family="GBRV_USPP_PBE_v1.5",
            is_uspp=True, is_metal=False,
            occupations="fixed", smearing=None, degauss=None,
            ecutrho_factor=10.0, rationale="ionic insulator",
            source="deterministic",
        )

    def test_metal_block(self):
        block = ca.render_config_advice_block(self._metal_advice())
        assert "QE CONFIGURATION ADVISORY" in block
        assert "METAL" in block
        assert "smearing='mv'" in block
        assert "degauss=0.02" in block
        assert "10 × ecutwfc" in block
        assert "tetrahedra" in block  # DOS NSCF reminder

    def test_insulator_block(self):
        block = ca.render_config_advice_block(self._insulator_advice())
        assert "SEMICONDUCTOR" in block
        assert "occupations='fixed'" in block
        assert "smearing" not in block.lower()[:block.lower().index("dos nscf")]  # no smearing before DOS line
        assert "10 × ecutwfc" in block

    def test_failed_advice_empty(self):
        bad = ca.ConfigAdvice(ok=False, failure_reason="loop crashed")
        assert ca.render_config_advice_block(bad) == ""

    def test_override_tag_visible(self):
        advice = self._metal_advice()
        advice.source = "llm_override"
        block = ca.render_config_advice_block(advice)
        assert "[llm_override]" in block


# ---------------------------------------------------------------------------
# JSON round-trip
# ---------------------------------------------------------------------------


class TestJsonRoundtrip:
    def test_to_json_loads(self):
        advice = ca.ConfigAdvice(
            ok=True, formula="Ca", elements=["Ca"], is_metal=True,
            occupations="smearing", smearing="mv", degauss=0.02,
            ecutrho_factor=10.0, is_uspp=True, source="deterministic",
        )
        s = advice.to_json()
        loaded = json.loads(s)
        assert loaded["ok"] is True
        assert loaded["is_metal"] is True
        assert loaded["occupations"] == "smearing"
        assert loaded["smearing"] == "mv"
        assert loaded["degauss"] == 0.02
        assert loaded["ecutrho_factor"] == 10.0


# ---------------------------------------------------------------------------
# FIX-18: dos.x NSCF 对 pbe_near_metal 材料保留 smearing
# ---------------------------------------------------------------------------


class TestFix18DosNscfSmearingForPbeNearMetal:
    def _ge_prompt(self):
        return "For Ge (cubic Fd-3m, 2 atoms). Pseudopotentials: GBRV_USPP."

    def _si_prompt(self):
        return "For Si (cubic Fd-3m, 2 atoms). Pseudopotentials: GBRV_USPP."

    def test_ge_render_block_says_dos_nscf_uses_smearing(self):
        det = ca._deterministic_classify(self._ge_prompt())
        assert det["pbe_near_metal"] is True
        advice = ca._from_deterministic(det)
        block = ca.render_config_advice_block(advice)
        # FIX-18: DOS NSCF must say smearing (not tetrahedra) for pbe_near_metal
        assert "DOS NSCF **ALSO uses `occupations='smearing'`**" in block
        assert "FIX-18 exception" in block
        # WHY paragraph mentions the dos rationale (FIX-29 reworded; DOS smearing kept)
        assert "DOS NSCF 仍保留 smearing" in block

    def test_si_render_block_keeps_tetrahedra(self):
        """Si is NOT pbe_near_metal — DOS NSCF directive should still say tetrahedra."""
        det = ca._deterministic_classify(self._si_prompt())
        assert det["pbe_near_metal"] is False
        advice = ca._from_deterministic(det)
        block = ca.render_config_advice_block(advice)
        assert "tetrahedra" in block
        assert "FIX-18 exception" not in block

    def test_cosb3_added_to_pbe_near_metal_and_gets_dos_smearing(self):
        """FIX-18 explicitly addresses CoSb3 — verify it's on the list AND gets the directive."""
        prompt = "For CoSb3 (cubic skutterudite, 32 atoms). Pseudopotentials: GBRV_USPP."
        det = ca._deterministic_classify(prompt)
        assert det["pbe_near_metal"] is True, "CoSb3 must be in PBE_NEAR_METAL_FORMULAS"
        advice = ca._from_deterministic(det)
        block = ca.render_config_advice_block(advice)
        assert "DOS NSCF **ALSO uses `occupations='smearing'`**" in block

    def test_metal_block_unchanged_by_fix18(self):
        """Real metals (Cu, Al) still get the standard 'DOS NSCF tetrahedra' line."""
        det = ca._deterministic_classify(
            "For Cu (cubic Fm-3m, 1 atom). Pseudopotentials: GBRV_USPP."
        )
        assert det["is_metal"] is True
        assert det["pbe_near_metal"] is False
        advice = ca._from_deterministic(det)
        block = ca.render_config_advice_block(advice)
        assert "tetrahedra" in block
        assert "FIX-18 exception" not in block


# ---------------------------------------------------------------------------
# FIX-32: magnetic non-allowlist post-validate guard
# ---------------------------------------------------------------------------


def test_validate_payload_locks_smearing_for_non_allowlist_magnetic():
    """FIX-32: LLM cannot turn occupations back to 'fixed' for a non-allowlist
    magnetic material (e.g., CuO). Mirrors FIX-19's allowlist override."""
    from agent.subagents.config_advisor import _validate_payload, _deterministic_classify
    # CuO: is_magnetic=True (via Cu) through the composition path (FIX-32).
    det = _deterministic_classify("For CuO (tenorite).\nPseudopotentials: GBRV_USPP_PBE_v1.5")
    assert det["is_magnetic"] is True
    # Simulate a "helpful" LLM that flipped occupations back to 'fixed'.
    llm_payload = {
        "task_outputs": {
            "is_metal": False,
            "occupations": "fixed",        # the LLM error we are guarding against
            "smearing": None,
            "degauss": None,
            "ecutrho_factor": det["ecutrho_factor"],
            "rationale": "CuO insulator (LLM override)",
        }
    }
    advice = _validate_payload(llm_payload, det)
    assert advice.is_magnetic is True
    assert advice.occupations == "smearing"   # forced back by FIX-32 guard
    assert advice.smearing is not None         # gauss fallback applied
    assert advice.degauss is not None
    assert "magnetic" in advice.source.lower() or advice.source == "deterministic"


# ---------------------------------------------------------------------------
# S-4 (FIX-41): formula-level MAGNETIC_TM_OXIDES allowlist removed. The magnetic
# protocol (nspin=2 + smearing=gauss/0.01) now comes purely from the composition
# path (FIX-32) for the whole magnetic class — see the TestFormerAllowlist*
# characterization tests below for the oxides' effective config. These guard the
# two remaining concerns.
# ---------------------------------------------------------------------------


def test_closed_shell_oxides_not_magnetic():
    """Al2O3 / MgO have no open-shell 3d/4f element → not magnetic, nspin=1
    (the composition classifier must not over-trigger on 'is an oxide')."""
    for prompt in [
        "For Al2O3 (corundum R-3c, 10 atoms). Pseudopotentials: GBRV_USPP.",
        "For MgO (rocksalt Fm-3m, 2 atoms). Pseudopotentials: GBRV_USPP.",
    ]:
        det = ca._deterministic_classify(prompt)
        assert det["is_magnetic"] is False
        assert det["recommend_nspin"] == 1


def test_config_block_has_no_formula_keyed_magnetic_directive():
    """The removed FIX-19 directive hardcoded a roster of material names and a
    'MAGNETIC PROTOCOL (FIX-19)' block in the config-advice text. After S-4 the
    block must NOT emit it — per-site seeds come from the probe handoff
    (_magnetism.render_magnetic_block). Guards the bench-leaking / hardcoded
    text from creeping back in."""
    det = ca._deterministic_classify(
        "For NiO (rocksalt Fm-3m, 2 atoms). Pseudopotentials: GBRV_USPP."
    )
    block = ca.render_config_advice_block(ca._from_deterministic(det))
    assert "MAGNETIC PROTOCOL (FIX-19)" not in block
    assert "alternating signs" not in block
    assert "NiO, MnO, CoO" not in block  # no hardcoded AFM material roster


class TestFix22MetallicTmOxides:
    """FIX-22 (2026-05-25): metallic-phase TM oxides (Cmcm-Ti3O5 etc.) bypass
    the electronegative-O insulator rule. Without this, advisor says insulator,
    GT extractor says metal, physics_sanity Rule B hard-fails, replan loop
    burns the whole budget. See _metals.py:METALLIC_TM_OXIDES."""

    def test_ti3o5_classified_as_metallic_tm_oxide(self):
        prompt = ("For Ti3O5 (orthorhombic Cmcm, 16 atoms). "
                  "Pseudopotentials: GBRV_USPP_PBE_v1.5.")
        det = ca._deterministic_classify(prompt)
        # FIX-22: is_metal=True overrides the default electroneg-O rule
        assert det["is_metal"] is True
        assert det["metallic_tm_oxide"] is True
        # Smearing tuned for d-electron stability, but recommend_nspin stays 1
        # (metallic phase — no magnetic ansatz, unlike the composition path).
        assert det["occupations"] == "smearing"
        assert det["smearing"] == "gauss"
        assert det["degauss"] == 0.01
        assert det["recommend_nspin"] == 1
        # The composition path must NOT flag Ti3O5 magnetic (Ti not in
        # MAGNETIC_ELEMENTS), else nspin=2 + AFM would be forced.
        assert det["is_magnetic"] is False

    def test_ti3o5_render_block_emits_metallic_directive(self):
        prompt = ("For Ti3O5 (orthorhombic Cmcm, 16 atoms). "
                  "Pseudopotentials: GBRV_USPP_PBE_v1.5.")
        det = ca._deterministic_classify(prompt)
        advice = ca._from_deterministic(det)
        block = ca.render_config_advice_block(advice)
        # Classification tag
        assert "metallic-phase TM oxide" in block
        assert "Classification: METAL" in block
        # METALLIC directive present
        assert "METALLIC-PHASE TM OXIDE (FIX-22)" in block
        assert "nspin=1" in block
        # Defensive: must explicitly tell LLM NOT to add magnetic ansatz
        assert "DO NOT add" in block
        assert "starting_magnetization" in block  # in the "DO NOT add" clause
        # Smearing values
        assert "smearing='gauss'" in block
        assert "degauss=0.01" in block
        # MUST NOT emit FIX-19 magnetic protocol
        assert "MAGNETIC PROTOCOL (FIX-19)" not in block
        assert "alternating signs" not in block
        # FIX-43: no GT-answer attribution in the metallic directive
        assert "Bench GT confirms" not in block
        assert "band_gap_ev=0" not in block

    def test_ti3o5_llm_cannot_unset_metal(self):
        """LLM cross-check seeing Ti3O5+O often reflexively says is_metal=False.
        _validate_payload must re-apply is_metal=True + gauss/0.01."""
        det = ca._deterministic_classify(
            "For Ti3O5 (orthorhombic Cmcm, 16 atoms). "
            "Pseudopotentials: GBRV_USPP_PBE_v1.5."
        )
        rogue_payload = {
            "task_outputs": {
                "is_metal": False,
                "occupations": "fixed",
                "smearing": None,
                "degauss": None,
                "ecutrho_factor": 10.0,
                "rationale": "LLM thinks oxide → insulator",
            }
        }
        result = ca._validate_payload(rogue_payload, det)
        # FIX-22 post-validate re-apply forces metal back
        assert result.is_metal is True
        assert result.occupations == "smearing"
        assert result.smearing == "gauss"
        assert result.degauss == 0.01
        assert result.metallic_tm_oxide is True
        assert result.recommend_nspin == 1
        assert result.source == "deterministic_metallic_tm_oxide_override"

    def test_ti3o5_does_not_trigger_magnetic_path(self):
        """Ti3O5 is metallic-phase, NOT magnetic: the composition classifier
        must leave is_magnetic=False (Ti not in MAGNETIC_ELEMENTS) so the
        metallic nspin=1 path holds and no AFM ansatz is forced."""
        det = ca._deterministic_classify(
            "For Ti3O5 (orthorhombic Cmcm, 16 atoms). "
            "Pseudopotentials: GBRV_USPP_PBE_v1.5."
        )
        assert det["metallic_tm_oxide"] is True
        assert det["is_magnetic"] is False

    def test_nio_not_polluted_by_fix22(self):
        """NiO is a magnetic insulator (composition path); FIX-22's metallic
        override must not touch it."""
        det = ca._deterministic_classify(
            "For NiO (rocksalt Fm-3m, 2 atoms). Pseudopotentials: GBRV_USPP."
        )
        assert det["is_magnetic"] is True
        assert det["metallic_tm_oxide"] is False
        assert det["recommend_nspin"] == 2
        assert det["is_metal"] is False     # NiO is an AFM insulator

    def test_rationale_explains_metallic_phase(self):
        """Rationale string for Ti3O5 should mention metallic-phase TM oxide,
        not the misleading 'insulator/semiconductor' default."""
        det = ca._deterministic_classify(
            "For Ti3O5 (orthorhombic Cmcm, 16 atoms). "
            "Pseudopotentials: GBRV_USPP_PBE_v1.5."
        )
        assert "metallic-phase TM oxide" in det["rationale"]
        assert "insulator" not in det["rationale"]


class TestFix31CutoffSensitiveMetals:
    """FIX-31 (2026-05-27): Group 12 浅 d-band post-transition metals (Cd / Zn)
    flagged so planner T1 system_prompt uses T2-canonical 80 Ry + ceil(50/a)
    min 8×8×8 instead of the FIX-20 T1 default (50 Ry / ceil(25/a) min 4×4×4).
    Triggering case: Cd vc_relax 2026-05-25T16-59 scored 0.445 at 50 Ry; the
    same task at 80 Ry scored 0.948. See _metals.py:CUTOFF_SENSITIVE_METALS."""

    def test_cd_classified_as_cutoff_sensitive(self):
        det = ca._deterministic_classify(
            "For Cd (hexagonal P6_3/mmc, 2 atoms). "
            "Pseudopotentials: GBRV_USPP_PBE_v1.5."
        )
        assert det["is_metal"] is True            # still a normal metal
        assert det["occupations"] == "smearing"   # METAL_ELEMENTS path
        assert det["smearing"] == "mv"            # not gauss (not TM oxide)
        assert det["degauss"] == 0.02             # standard, not 0.01
        assert det["cutoff_sensitive_metal"] is True
        # Mutually independent flags
        assert det["metallic_tm_oxide"] is False
        assert det["pbe_near_metal"] is False

    def test_zn_classified_as_cutoff_sensitive(self):
        det = ca._deterministic_classify(
            "For Zn (hexagonal P6_3/mmc, 2 atoms). "
            "Pseudopotentials: GBRV_USPP_PBE_v1.5."
        )
        assert det["is_metal"] is True
        assert det["cutoff_sensitive_metal"] is True

    def test_non_group12_metals_not_flagged(self):
        """Sanity: ordinary metals (Rb / Ag / Cu / Al / Ti) must NOT trigger
        the flag — FIX-31 is targeted at Group 12 only. Otherwise we'd
        re-introduce the FIX-20 timeout for bigger T1 cells."""
        for elem in ("Rb", "Ag", "Cu", "Al", "Ti", "Ca", "Mg"):
            det = ca._deterministic_classify(
                f"For {elem} (cubic, 1 atom). Pseudopotentials: GBRV_USPP_PBE_v1.5."
            )
            assert det["cutoff_sensitive_metal"] is False, (
                f"{elem} should not be cutoff-sensitive — only Group 12 is"
            )

    def test_semiconductors_not_flagged(self):
        """Sanity: Si / Ge / GaAs etc. obviously not flagged."""
        for prompt in (
            "For Si (diamond, 2 atoms). Pseudopotentials: GBRV_USPP_PBE_v1.5.",
            "For Ge (diamond, 2 atoms). Pseudopotentials: GBRV_USPP_PBE_v1.5.",
            "For GaAs (zincblende, 2 atoms). Pseudopotentials: GBRV_USPP_PBE_v1.5.",
            "For NaCl (rocksalt, 2 atoms). Pseudopotentials: GBRV_USPP_PBE_v1.5.",
        ):
            det = ca._deterministic_classify(prompt)
            assert det["cutoff_sensitive_metal"] is False

    def test_cd_render_block_emits_cutoff_directive(self):
        det = ca._deterministic_classify(
            "For Cd (hexagonal P6_3/mmc, 2 atoms). "
            "Pseudopotentials: GBRV_USPP_PBE_v1.5."
        )
        advice = ca._from_deterministic(det)
        block = ca.render_config_advice_block(advice)
        # Classification tag
        assert "cutoff-sensitive metal" in block
        assert "FIX-31" in block
        # Directive section
        assert "CUTOFF-SENSITIVE METAL (FIX-31)" in block
        # Teaches the 80 Ry + dense-k recipe
        assert "ecutwfc = 80 Ry" in block
        assert "ecutrho = 800 Ry" in block
        assert "ceil(50 / a_Å)" in block
        assert "min 8×8×8" in block
        # References the triggering case for LLM context
        assert "0.445" in block
        assert "0.948" in block
        # FIX-43: no GT lattice answer / GT-generation attribution
        assert "GT a=3.05" not in block and "c=5.65" not in block
        assert "bench used to generate the GT" not in block

    def test_non_cutoff_metals_render_no_directive(self):
        """Sanity: Ag / Rb advice block must NOT contain the FIX-31 directive."""
        for elem in ("Rb", "Ag"):
            det = ca._deterministic_classify(
                f"For {elem} (cubic, 1 atom). Pseudopotentials: GBRV_USPP_PBE_v1.5."
            )
            advice = ca._from_deterministic(det)
            block = ca.render_config_advice_block(advice)
            assert "CUTOFF-SENSITIVE METAL (FIX-31)" not in block
            assert "cutoff-sensitive metal" not in block

    def test_cutoff_sensitive_survives_validate_payload(self):
        """LLM cross-check cannot unset the flag (it's an objective material
        property, like pbe_near_metal / metallic_tm_oxide)."""
        det = ca._deterministic_classify(
            "For Cd (hexagonal P6_3/mmc, 2 atoms). "
            "Pseudopotentials: GBRV_USPP_PBE_v1.5."
        )
        # Suppose the LLM (correctly) submits is_metal=true / occupations=smearing
        # — the cutoff_sensitive_metal flag still must carry through.
        payload = {
            "task_outputs": {
                "is_metal": True,
                "occupations": "smearing",
                "smearing": "mv",
                "degauss": 0.02,
                "ecutrho_factor": 10.0,
                "rationale": "Cd is a Group 12 metal — smearing default OK",
            }
        }
        result = ca._validate_payload(payload, det)
        assert result.cutoff_sensitive_metal is True
        assert result.is_metal is True
        # Standard metal path knobs unchanged — FIX-31 only affects cutoffs
        # via the system_prompt branch, not advisor occupations/smearing.
        assert result.occupations == "smearing"
        assert result.smearing == "mv"
        assert result.degauss == 0.02

    def test_cutoff_sensitive_survives_fallback(self):
        """If LLM crashes, _from_deterministic fallback must preserve the flag."""
        det = ca._deterministic_classify(
            "For Cd (hexagonal P6_3/mmc, 2 atoms). "
            "Pseudopotentials: GBRV_USPP_PBE_v1.5."
        )
        advice = ca._from_deterministic(det, source="fallback",
                                        failure_reason="LLMTimeout: simulated")
        assert advice.cutoff_sensitive_metal is True
        assert advice.ok is False  # ok=False because failure_reason set

    def test_membership_set_is_small_and_specific(self):
        """Sanity: the whitelist must stay narrow. Adding non-Group-12 elements
        here would re-introduce the FIX-20 timeout regression for bigger T1
        cells. Hg may be added later when bench grows a Hg task."""
        assert "Cd" in CUTOFF_SENSITIVE_METALS
        assert "Zn" in CUTOFF_SENSITIVE_METALS
        # Must NOT contain Group 13/14 post-transition metals (their d-band
        # is too deep to be cutoff-sensitive at 50 Ry).
        for elem in ("Ga", "In", "Tl", "Sn", "Pb", "Al", "Ag", "Cu", "Au"):
            assert elem not in CUTOFF_SENSITIVE_METALS, (
                f"{elem} should not be in CUTOFF_SENSITIVE_METALS — narrow scope only"
            )


# ---------------------------------------------------------------------------
# FIX-32: composition-based magnetism in _deterministic_classify
# ---------------------------------------------------------------------------


from agent.subagents.config_advisor import _deterministic_classify  # noqa: E402


def test_classify_marks_elemental_iron_magnetic():
    det = _deterministic_classify("For Fe (bcc, space group Im-3m).\nPseudopotentials: GBRV_USPP_PBE_v1.5")
    assert det["is_magnetic"] is True
    assert det["magnetic_elements"] == ["Fe"]
    assert det["recommend_nspin"] == 2


def test_classify_marks_cuo_magnetic_even_though_not_in_old_allowlist():
    det = _deterministic_classify("For CuO (tenorite).\nPseudopotentials: GBRV_USPP_PBE_v1.5")
    assert det["is_magnetic"] is True
    assert det["recommend_nspin"] == 2


def test_classify_nonmagnetic_unchanged():
    det = _deterministic_classify("For Si (diamond).\nPseudopotentials: GBRV_USPP_PBE_v1.5")
    assert det["is_magnetic"] is False
    assert det["recommend_nspin"] == 1


# ---------------------------------------------------------------------------
# S-4 (FIX-41): MAGNETIC_TM_OXIDES allowlist removed. The composition-based
# is_magnetic path (FIX-32) must produce the SAME effective config for the
# former-allowlist oxides. This characterization test pins occ/smearing/
# degauss/nspin so removing the allowlist cannot silently change behaviour.
# (The `source` audit string legitimately changes from *_magnetic_tm_oxide_
# override to *_magnetic_override — config is what matters, so we pin config.)
# ---------------------------------------------------------------------------

_FORMER_ALLOWLIST_OXIDES = ["NiO", "MnO", "FeO", "CoO", "Fe3O4", "Mn3O4",
                            "Co3O4", "V2O3", "VO2", "Cr2O3"]


@pytest.mark.parametrize("formula", _FORMER_ALLOWLIST_OXIDES)
def test_former_allowlist_oxide_config_via_composition_path(formula):
    det = _deterministic_classify(_make_prompt(formula, structure="rocksalt"))
    adv = ca._from_deterministic(det)
    assert adv.is_magnetic is True
    assert adv.is_metal is False
    assert adv.occupations == "smearing"
    assert adv.smearing == "gauss"
    assert adv.degauss == 0.01
    assert adv.recommend_nspin == 2


@pytest.mark.parametrize("formula", _FORMER_ALLOWLIST_OXIDES)
def test_former_allowlist_oxide_protocol_forced_against_hostile_llm(formula):
    det = _deterministic_classify(_make_prompt(formula, structure="rocksalt"))
    adv = ca._validate_payload(
        {"task_outputs": {"occupations": "fixed", "smearing": "mv",
                          "degauss": 0.02, "is_metal": False}},
        det,
    )
    assert adv.occupations == "smearing"
    assert adv.smearing == "gauss"
    assert adv.degauss == 0.01
    assert adv.recommend_nspin == 2
