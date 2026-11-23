"""Skill library loader — frontmatter-aware markdown playbooks.

A skill file is a single markdown document with YAML frontmatter:

    ---
    name: alkali-metal-advisor
    description: 碱金属 SCF/bands 必备配方
    trigger:
      any_of:
        - material_family: alkali_metal
      all_of:
        - crystal_system: cubic           # optional constraint
    priority: 10
    ---

    # Key constraints
    ...

The trigger evaluator is intentionally minimal — four predicate keys, fixed
semantics:

    material_family   : str      → string ∈ facts["material_families"]
    crystal_system    : str      → exact match against facts["crystal_system"]
    formula_regex     : str      → re.search against facts["formula"]
    contains_elements : list[str] → any-overlap with facts["elements"]

Both `any_of` (OR) and `all_of` (AND) clauses default to neutral when absent;
having BOTH absent means the skill matches nothing (defensive — a "global"
advisor is not a v1 concept).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

import yaml

# Resolve repo root the same way solve.py does: agent/skills/loader.py → repo.
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_SKILLS_ROOT = _REPO_ROOT / "agent" / "skills"


@dataclass
class SkillMeta:
    name: str
    description: str
    trigger: dict[str, Any]
    body: str
    path: Path
    priority: int = 0


# ─── Loading ─────────────────────────────────────────────────────────────────


_FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n(.*)\Z", re.DOTALL)


def _parse_skill_file(path: Path) -> Optional[SkillMeta]:
    """Parse a single skill markdown file. Returns None on malformed input."""
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return None
    m = _FRONTMATTER_RE.match(raw)
    if not m:
        return None
    try:
        meta = yaml.safe_load(m.group(1)) or {}
    except yaml.YAMLError:
        return None
    if not isinstance(meta, dict):
        return None
    name = meta.get("name")
    if not isinstance(name, str) or not name.strip():
        return None
    description = meta.get("description", "")
    if not isinstance(description, str):
        description = ""
    trigger = meta.get("trigger", {})
    if not isinstance(trigger, dict):
        trigger = {}
    priority = meta.get("priority", 0)
    if not isinstance(priority, int):
        priority = 0
    return SkillMeta(
        name=name.strip(),
        description=description.strip(),
        trigger=trigger,
        body=m.group(2).strip(),
        path=path,
        priority=priority,
    )


def load_all(root: Optional[Path] = None) -> list[SkillMeta]:
    """Scan a skills directory for *.md files. Malformed files are skipped.

    Files starting with "_" or "." are ignored (room for stubs / templates).
    """
    root = root or DEFAULT_SKILLS_ROOT
    if not root.is_dir():
        return []
    skills: list[SkillMeta] = []
    for path in sorted(root.glob("*.md")):
        if path.name.startswith(("_", ".")):
            continue
        skill = _parse_skill_file(path)
        if skill is not None:
            skills.append(skill)
    return skills


# ─── Trigger evaluation ──────────────────────────────────────────────────────


def _eval_predicate(pred: dict[str, Any], facts: dict[str, Any]) -> bool:
    """Evaluate a single trigger clause (one dict entry) against facts.

    Unknown keys evaluate False (defensive — typos shouldn't silently pass).
    """
    if not isinstance(pred, dict) or len(pred) != 1:
        # Permit a multi-key dict by AND-ing — useful syntactic sugar even
        # though we recommend one-key clauses. Returns False if any key fails.
        if isinstance(pred, dict) and len(pred) > 1:
            return all(_eval_predicate({k: v}, facts) for k, v in pred.items())
        return False

    key, val = next(iter(pred.items()))

    if key == "material_family":
        if not isinstance(val, str):
            return False
        return val in (facts.get("material_families") or [])

    if key == "crystal_system":
        if not isinstance(val, str):
            return False
        return val == facts.get("crystal_system")

    if key == "formula_regex":
        if not isinstance(val, str):
            return False
        formula = facts.get("formula")
        if not isinstance(formula, str):
            return False
        try:
            return re.search(val, formula) is not None
        except re.error:
            return False

    if key == "contains_elements":
        if not isinstance(val, (list, tuple)):
            return False
        elems = set(facts.get("elements") or [])
        return any(e in elems for e in val if isinstance(e, str))

    # Unknown predicate key → False.
    return False


def match(skill: SkillMeta, facts: dict[str, Any]) -> bool:
    """Evaluate `skill.trigger` against `facts`.

    Trigger schema:
      trigger:
        any_of: [predicate, predicate, ...]    # OR — at least one must match
        all_of: [predicate, predicate, ...]    # AND — all must match
    Both clauses default to neutral (no constraint) when absent. If BOTH are
    absent / empty, the skill does not match (avoids accidental global skills).
    """
    trigger = skill.trigger or {}
    any_clauses = trigger.get("any_of") or []
    all_clauses = trigger.get("all_of") or []
    if not isinstance(any_clauses, list):
        any_clauses = []
    if not isinstance(all_clauses, list):
        all_clauses = []
    if not any_clauses and not all_clauses:
        return False

    any_ok = (not any_clauses) or any(_eval_predicate(p, facts) for p in any_clauses)
    all_ok = all(_eval_predicate(p, facts) for p in all_clauses)
    return any_ok and all_ok


# ─── Rendering ───────────────────────────────────────────────────────────────


_MARKER_OPEN = "===== MATERIAL ADVISORIES ====="
_MARKER_CLOSE = "===== END MATERIAL ADVISORIES ====="


def select_and_render(
    skills: Iterable[SkillMeta],
    facts: dict[str, Any],
) -> str:
    """Match all `skills` against `facts`, render hits as a markdown block.

    Returns an empty string when no skill matches. Skills are sorted by
    `priority` descending, then by name ascending (deterministic tiebreak).
    """
    hits = [s for s in skills if match(s, facts)]
    if not hits:
        return ""
    hits.sort(key=lambda s: (-s.priority, s.name))
    parts = [
        _MARKER_OPEN,
        "(human-authored playbooks matched by structural classification — informational, do not act blindly)",
        "",
    ]
    for s in hits:
        parts.append(f"[{s.name}] {s.description}".rstrip())
        parts.append(s.body)
        parts.append("")
    parts.append(_MARKER_CLOSE)
    return "\n".join(parts).rstrip() + "\n"
