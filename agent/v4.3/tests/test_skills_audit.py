"""Audit guarantees for shipped skill markdown files.

Skill bodies are part of the public manifest — bench reviewers can grep
them to check we're not hard-coding answers. The audit policy:

  - Element symbols ARE allowed in body (family advisors must name them).
  - Numeric defaults with physics units are NOT allowed
    (`60 Ry`, `1.5 eV`, `8 GPa`, `0.05 kbar`, etc.).
  - Frontmatter is exempt — `material_family: alkali_metal` is a tag, not
    a numeric configuration.
  - Each skill must have a non-empty trigger and a well-formed frontmatter.

This is a different policy from the experience-library validator (which
denies element symbols too): skills are pre-shipped manifesto-style
content, experience entries are LLM-generated lessons. The audit goals
differ.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from agent.skills.loader import DEFAULT_SKILLS_ROOT, load_all


# Number+unit combo: this is what we forbid (means "specific value
# hardcoded in advice text").
_NUMERIC_UNIT_RE = re.compile(
    r"\b\d+(\.\d+)?\s*(eV|GPa|Ry|Ha|bohr|kbar|Å|Angstrom|Pa|MPa|kPa|"
    r"meV|µeV|μeV)\b"
)


def _ship_skills():
    return load_all(DEFAULT_SKILLS_ROOT)


def test_skills_directory_has_files():
    skills = _ship_skills()
    assert len(skills) >= 4, f"expected ≥4 family skills, got {len(skills)}"


@pytest.mark.parametrize("name", [
    "alkali-metal-advisor",
    "polar-iii-v-advisor",
    "low-symmetry-oxide-advisor",
    "complex-silicate-advisor",
    "layered-vdw-advisor",
])
def test_skill_present(name):
    skills = _ship_skills()
    names = {s.name for s in skills}
    assert name in names, f"missing skill: {name}"


def test_no_numeric_unit_in_skill_bodies():
    """Audit: skill bodies must not pin specific values like '60 Ry'."""
    skills = _ship_skills()
    offenders = []
    for s in skills:
        for m in _NUMERIC_UNIT_RE.finditer(s.body):
            offenders.append(f"{s.name}: matched {m.group(0)!r}")
    assert not offenders, "numeric-unit values leaked into skill bodies:\n" + "\n".join(offenders)


def test_all_shipped_skills_have_well_formed_trigger():
    """Every shipped skill must declare a non-empty any_of or all_of."""
    skills = _ship_skills()
    bad = []
    for s in skills:
        any_clauses = s.trigger.get("any_of") or []
        all_clauses = s.trigger.get("all_of") or []
        if not any_clauses and not all_clauses:
            bad.append(s.name)
    assert not bad, f"skills with empty trigger: {bad}"


def test_layered_vdw_advisor_triggers_on_bn_hex_facts():
    """End-to-end: shipped layered-vdw-advisor must fire on facts that
    structure_features.extract() returns for h-BN (the failure mode that
    motivated this advisor — BNhex bands_dos run 2026-05-22)."""
    from agent.skills.loader import select_and_render
    skills = _ship_skills()
    # Simulate what extract() returns for BN_hex (verified by
    # test_layered_vdw_bn_hex_triggers in test_structure_features.py).
    bn_facts = {
        "material_families": ["polar_III_V", "layered_vdw"],
        "crystal_system": "hexagonal",
        "formula": "BN",
        "elements": ["B", "N"],
    }
    out = select_and_render(skills, bn_facts)
    assert "[layered-vdw-advisor]" in out
    # polar-iii-v also fires (multi-label intentional)
    assert "[polar-iii-v-advisor]" in out


def test_layered_vdw_advisor_does_not_fire_on_wurtzite_gan():
    """Negative: GaN wurtzite has polar_III_V but NOT layered_vdw (c/a < 1.8).
    Only polar-iii-v advisor should fire, not layered-vdw."""
    from agent.skills.loader import select_and_render
    skills = _ship_skills()
    gan_facts = {
        "material_families": ["polar_III_V"],
        "crystal_system": "hexagonal",
        "formula": "GaN",
        "elements": ["Ga", "N"],
    }
    out = select_and_render(skills, gan_facts)
    assert "[layered-vdw-advisor]" not in out
    assert "[polar-iii-v-advisor]" in out


def test_skill_filename_matches_name(tmp_path: Path):
    """The `name:` field must match the filename stem — keeps reviewers honest."""
    for path in DEFAULT_SKILLS_ROOT.glob("*.md"):
        if path.name.startswith(("_", ".")):
            continue
        skills = load_all(DEFAULT_SKILLS_ROOT)
        names = {s.path.stem: s.name for s in skills}
        for stem, name in names.items():
            assert stem == name, f"filename {stem}.md != skill name {name!r}"
        return    # one pass is enough; loop just ensures the dir is non-empty
