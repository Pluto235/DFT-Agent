"""Skill library loader — trigger evaluation + rendering."""
from __future__ import annotations

from pathlib import Path

import pytest

from agent.skills.loader import (
    SkillMeta,
    load_all,
    match,
    select_and_render,
)


# ─── Helpers ─────────────────────────────────────────────────────────────────


def _skill(
    name: str,
    trigger: dict,
    *,
    description: str = "test skill",
    body: str = "BODY",
    priority: int = 0,
) -> SkillMeta:
    return SkillMeta(
        name=name,
        description=description,
        trigger=trigger,
        body=body,
        path=Path("/fake.md"),
        priority=priority,
    )


# ─── Trigger predicate semantics ─────────────────────────────────────────────


def test_match_material_family_any_of():
    s = _skill("alkali", {"any_of": [{"material_family": "alkali_metal"}]})
    assert match(s, {"material_families": ["alkali_metal"]})
    assert not match(s, {"material_families": ["oxide"]})
    assert not match(s, {"material_families": []})


def test_match_crystal_system_all_of():
    s = _skill("low_sym",
               {"all_of": [{"crystal_system": "monoclinic"}]})
    assert match(s, {"crystal_system": "monoclinic"})
    assert not match(s, {"crystal_system": "cubic"})


def test_match_formula_regex():
    s = _skill("iiiv", {"any_of": [{"formula_regex": r"^(Ga|Al|In)(N|P|As)$"}]})
    assert match(s, {"formula": "GaN"})
    assert match(s, {"formula": "AlP"})
    assert not match(s, {"formula": "SiO2"})
    assert not match(s, {"formula": None})


def test_match_contains_elements():
    s = _skill("alkali_elems",
               {"any_of": [{"contains_elements": ["Li", "Na", "K", "Rb", "Cs"]}]})
    assert match(s, {"elements": ["Rb"]})
    assert match(s, {"elements": ["Na", "Cl"]})    # any-overlap
    assert not match(s, {"elements": ["Si"]})


def test_match_any_of_or_semantics():
    s = _skill("multi", {"any_of": [
        {"material_family": "alkali_metal"},
        {"formula_regex": r"^GaN$"},
    ]})
    assert match(s, {"material_families": ["alkali_metal"]})
    assert match(s, {"material_families": [], "formula": "GaN"})
    assert not match(s, {"material_families": [], "formula": "SiO2"})


def test_match_all_of_and_semantics():
    s = _skill("low_sym_oxide", {"all_of": [
        {"material_family": "oxide"},
        {"crystal_system": "monoclinic"},
    ]})
    assert match(s, {"material_families": ["oxide"], "crystal_system": "monoclinic"})
    # Both required — failing one fails the whole AND.
    assert not match(s, {"material_families": ["oxide"], "crystal_system": "cubic"})
    assert not match(s, {"material_families": ["halide"], "crystal_system": "monoclinic"})


def test_match_any_and_all_combined():
    """Combining any_of + all_of: any_of OR'd internally, then AND'd with all_of."""
    s = _skill("complex", {
        "any_of": [
            {"material_family": "silicate"},
            {"material_family": "oxide"},
        ],
        "all_of": [{"crystal_system": "monoclinic"}],
    })
    # silicate + monoclinic → hits any_of, hits all_of → match.
    assert match(s, {"material_families": ["silicate"], "crystal_system": "monoclinic"})
    # oxide + monoclinic → match.
    assert match(s, {"material_families": ["oxide"], "crystal_system": "monoclinic"})
    # silicate + cubic → fails all_of.
    assert not match(s, {"material_families": ["silicate"], "crystal_system": "cubic"})
    # halide + monoclinic → fails any_of.
    assert not match(s, {"material_families": ["halide"], "crystal_system": "monoclinic"})


def test_match_empty_trigger_matches_nothing():
    """Skill with neither any_of nor all_of should never match — defensive."""
    s = _skill("empty", {})
    assert not match(s, {"material_families": ["alkali_metal"]})
    assert not match(s, {"crystal_system": "cubic"})


def test_match_unknown_predicate_key_returns_false():
    s = _skill("bad", {"any_of": [{"bogus_key": "anything"}]})
    assert not match(s, {"material_families": ["alkali_metal"]})


# ─── select_and_render ───────────────────────────────────────────────────────


def test_render_empty_when_no_hit():
    skills = [_skill("a", {"any_of": [{"material_family": "alkali_metal"}]})]
    out = select_and_render(skills, {"material_families": ["oxide"]})
    assert out == ""


def test_render_includes_markers_and_body():
    s = _skill("alkali", {"any_of": [{"material_family": "alkali_metal"}]},
              description="碱金属配方", body="body content")
    out = select_and_render([s], {"material_families": ["alkali_metal"]})
    assert "===== MATERIAL ADVISORIES =====" in out
    assert "===== END MATERIAL ADVISORIES =====" in out
    assert "[alkali] 碱金属配方" in out
    assert "body content" in out


def test_render_priority_descending_then_name():
    a = _skill("a", {"any_of": [{"material_family": "x"}]}, body="zzz_BODY_A", priority=1)
    b = _skill("b", {"any_of": [{"material_family": "x"}]}, body="zzz_BODY_B", priority=10)
    c = _skill("c", {"any_of": [{"material_family": "x"}]}, body="zzz_BODY_C", priority=10)
    facts = {"material_families": ["x"]}
    out = select_and_render([a, b, c], facts)
    pos_a = out.index("zzz_BODY_A")
    pos_b = out.index("zzz_BODY_B")
    pos_c = out.index("zzz_BODY_C")
    # b and c (priority=10) come first, alphabetically b before c.
    # Then a (priority=1).
    assert pos_b < pos_c < pos_a


def test_render_only_hits_no_misses():
    hit = _skill("hit", {"any_of": [{"material_family": "alkali_metal"}]}, body="HIT")
    miss = _skill("miss", {"any_of": [{"material_family": "oxide"}]}, body="MISS")
    out = select_and_render([hit, miss], {"material_families": ["alkali_metal"]})
    assert "HIT" in out
    assert "MISS" not in out


# ─── load_all ────────────────────────────────────────────────────────────────


def test_load_all_parses_frontmatter(tmp_path: Path):
    (tmp_path / "alkali-metal-advisor.md").write_text(
        "---\n"
        "name: alkali-metal-advisor\n"
        "description: short desc\n"
        "trigger:\n"
        "  any_of:\n"
        "    - material_family: alkali_metal\n"
        "priority: 10\n"
        "---\n"
        "# Body content\n"
        "stuff\n",
        encoding="utf-8",
    )
    skills = load_all(tmp_path)
    assert len(skills) == 1
    s = skills[0]
    assert s.name == "alkali-metal-advisor"
    assert s.description == "short desc"
    assert s.priority == 10
    assert s.trigger == {"any_of": [{"material_family": "alkali_metal"}]}
    assert "Body content" in s.body


def test_load_all_skips_malformed(tmp_path: Path):
    # No frontmatter
    (tmp_path / "bad1.md").write_text("just text\n", encoding="utf-8")
    # Frontmatter not valid yaml
    (tmp_path / "bad2.md").write_text("---\nfoo: [unclosed\n---\nbody\n",
                                       encoding="utf-8")
    # Missing required name
    (tmp_path / "bad3.md").write_text("---\ndescription: x\n---\nbody\n",
                                       encoding="utf-8")
    # Good
    (tmp_path / "good.md").write_text(
        "---\nname: good\ntrigger: {any_of: [{material_family: x}]}\n---\nB\n",
        encoding="utf-8")
    skills = load_all(tmp_path)
    names = [s.name for s in skills]
    assert names == ["good"]


def test_load_all_ignores_underscore_and_dot(tmp_path: Path):
    (tmp_path / "_template.md").write_text(
        "---\nname: template\ntrigger: {any_of: [{material_family: x}]}\n---\nB\n",
        encoding="utf-8")
    (tmp_path / ".hidden.md").write_text(
        "---\nname: hidden\ntrigger: {any_of: [{material_family: x}]}\n---\nB\n",
        encoding="utf-8")
    (tmp_path / "real.md").write_text(
        "---\nname: real\ntrigger: {any_of: [{material_family: x}]}\n---\nB\n",
        encoding="utf-8")
    skills = load_all(tmp_path)
    assert [s.name for s in skills] == ["real"]


def test_load_all_missing_dir_returns_empty(tmp_path: Path):
    assert load_all(tmp_path / "does-not-exist") == []
