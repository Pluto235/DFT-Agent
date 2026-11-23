#!/usr/bin/env python3
"""Resolve m3_m4.yaml against Materials Project; output candidates yaml.

Sanity-check rationale (see notes/2026-05-04-m3m4-mp-id-audit.md Q7):
~10/30 wishlist mp_id_hint values were found wrong via reviewer cross-check
(NOT_FOUND or MISMATCH like mp-4651 SrRuO3 → actually SrTiO3). This script's
formula sanity check + auto-fallback ensures wishlist data errors don't silently
poison candidate selection.

For each material in m3_m4.yaml:
- If mp_id_hint set: lookup by mp_id, then verify returned formula matches declared formula.
  If mismatch (e.g., wishlist hint pointing at wrong compound), fall back to formula+spacegroup search.
- If mp_id_hint=null: search by formula+spacegroup.

Outputs m3_m4_candidates.yaml with hint_mismatch_warning field for materials whose hint failed sanity check.
"""
from __future__ import annotations
import argparse
import logging
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import yaml
from mp_api.client import MPRester
from pymatgen.core.composition import Composition

HERE = Path(__file__).resolve().parent
INPUT_YAML = HERE / "m3_m4.yaml"
OUTPUT_YAML = HERE / "m3_m4_candidates.yaml"
LOG_FILE = HERE / "resolve_m3_m4.log"


@dataclass
class CandidateEntry:
    mp_id: str
    formula: str
    space_group_number: int | None
    space_group_symbol: str
    n_atoms: int
    energy_above_hull: float
    is_stable: bool
    magnetic_ordering: str | None
    formation_energy_per_atom: float


def _doc_to_candidate(d) -> CandidateEntry:
    # Coerce MP API return objects (MPID, Ordering enum, etc.) to plain
    # Python builtins so yaml.safe_dump can serialize.
    ordering = getattr(d, "ordering", None)
    if ordering is not None:
        ordering = str(getattr(ordering, "value", ordering))
    return CandidateEntry(
        mp_id=str(d.material_id),
        formula=str(d.formula_pretty),
        space_group_number=int(d.symmetry.number) if d.symmetry else None,
        space_group_symbol=str(d.symmetry.symbol) if d.symmetry else "",
        n_atoms=int(d.nsites),
        energy_above_hull=float(d.energy_above_hull or 0.0),
        is_stable=bool(d.is_stable),
        magnetic_ordering=ordering,
        formation_energy_per_atom=float(d.formation_energy_per_atom or 0.0),
    )


def search_candidates(mpr: MPRester, formula: str, target_sg: int) -> list[CandidateEntry]:
    """For the given formula + target spacegroup, return up to 5 candidates sorted by (sg_match desc, energy_above_hull asc)."""
    docs = mpr.summary.search(formula=formula, fields=[
        "material_id", "formula_pretty", "symmetry", "energy_above_hull",
        "is_stable", "ordering", "formation_energy_per_atom", "nsites",
    ])
    out = [_doc_to_candidate(d) for d in docs]
    out.sort(key=lambda c: (c.space_group_number != target_sg, c.energy_above_hull))
    return out[:5]


def lookup_by_mp_id(mpr: MPRester, mp_id: str) -> CandidateEntry | None:
    """Direct mp_id lookup (used when mp_id_hint already supplied)."""
    docs = mpr.summary.search(material_ids=[mp_id], fields=[
        "material_id", "formula_pretty", "symmetry", "energy_above_hull",
        "is_stable", "ordering", "formation_energy_per_atom", "nsites",
    ])
    if not docs:
        return None
    return _doc_to_candidate(docs[0])


def formulas_compatible(declared: str, mp_returned: str) -> bool:
    """Loose formula match using pymatgen reduced_formula normalization.

    Handles: 'FeS2' vs 'Fe1S2', 'SrRuO3' vs 'Sr1Ru1O3', etc.
    Returns True if both reduce to same canonical formula.
    """
    try:
        return Composition(declared).reduced_formula == Composition(mp_returned).reduced_formula
    except Exception:
        return declared == mp_returned


def candidate_to_dict(c: CandidateEntry) -> dict:
    return {
        "mp_id": c.mp_id,
        "formula": c.formula,
        "space_group_symbol": c.space_group_symbol,
        "space_group_number": c.space_group_number,
        "n_atoms": c.n_atoms,
        "energy_above_hull": float(c.energy_above_hull),
        "is_stable": c.is_stable,
        "magnetic_ordering": c.magnetic_ordering,
        "formation_energy_per_atom": float(c.formation_energy_per_atom),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--api-key", default=os.environ.get("MP_API_KEY"))
    parser.add_argument("--input", type=Path, default=INPUT_YAML)
    parser.add_argument("--output", type=Path, default=OUTPUT_YAML)
    args = parser.parse_args()
    if not args.api_key:
        print("ERROR: MP_API_KEY not set; pass --api-key or env var", file=sys.stderr)
        return 1

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.FileHandler(LOG_FILE, mode="w"), logging.StreamHandler()],
    )

    doc = yaml.safe_load(args.input.read_text(encoding="utf-8"))
    out = {
        "library_version": doc["library_version"],
        "generated_at": time.strftime("%Y-%m-%d"),
        "source_yaml": str(args.input.name),
        "M3": {"description": doc["M3"]["description"], "categories": {}},
        "M4": {"description": doc["M4"]["description"], "categories": {}},
    }

    n_total = 0
    n_hint_ok = 0
    n_hint_notfound = 0
    n_hint_mismatch = 0
    n_search_only = 0

    with MPRester(args.api_key) as mpr:
        for tier_key in ("M3", "M4"):
            for cat_key, cat in doc[tier_key]["categories"].items():
                logging.info(f"=== {tier_key} / {cat_key} ===")
                materials_out = []
                for m in cat["materials"]:
                    n_total += 1
                    logging.info(f"  resolving {m['id']} (formula={m['formula']}, hint={m.get('mp_id_hint')})")
                    hint_mismatch_warning: str | None = None

                    if m.get("mp_id_hint"):
                        c = lookup_by_mp_id(mpr, m["mp_id_hint"])
                        if c is None:
                            hint_mismatch_warning = f"mp_id_hint {m['mp_id_hint']} not found in MP"
                            logging.warning(f"  ⚠ {hint_mismatch_warning}; falling back to formula+sg search")
                            cands = search_candidates(mpr, m["formula"], m["space_group_number"])
                            n_hint_notfound += 1
                        elif formulas_compatible(m["formula"], c.formula):
                            cands = [c]
                            n_hint_ok += 1
                        else:
                            hint_mismatch_warning = (
                                f"mp_id_hint {m['mp_id_hint']} formula mismatch "
                                f"(MP returned {c.formula}, wishlist declared {m['formula']})"
                            )
                            logging.warning(f"  ⚠ {hint_mismatch_warning}; falling back to formula+sg search")
                            cands = search_candidates(mpr, m["formula"], m["space_group_number"])
                            n_hint_mismatch += 1
                    else:
                        cands = search_candidates(mpr, m["formula"], m["space_group_number"])
                        n_search_only += 1

                    logging.info(f"  -> {len(cands)} candidate(s)")
                    if not cands:
                        empty_warning = f"resolved 0 candidates for formula={m['formula']} sg={m['space_group_number']}"
                        if hint_mismatch_warning:
                            hint_mismatch_warning += f"; {empty_warning}"
                        else:
                            hint_mismatch_warning = empty_warning
                        logging.warning(f"  ⚠ {hint_mismatch_warning}")
                    materials_out.append({
                        **{k: m[k] for k in (
                            "id", "formula", "structure_hint",
                            "space_group", "space_group_number",
                            "atoms_per_primitive_cell", "notes",
                        )},
                        "mp_id_hint": m.get("mp_id_hint"),
                        "hint_mismatch_warning": hint_mismatch_warning,
                        "candidates": [candidate_to_dict(c) for c in cands],
                    })
                    time.sleep(0.5)  # rate limit be polite
                out[tier_key]["categories"][cat_key] = {
                    "label": cat["label"],
                    "materials": materials_out,
                }

    out["summary"] = {
        "n_total": n_total,
        "n_hint_ok": n_hint_ok,
        "n_hint_notfound": n_hint_notfound,
        "n_hint_mismatch": n_hint_mismatch,
        "n_search_only": n_search_only,
    }

    args.output.write_text(
        yaml.safe_dump(out, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
    logging.info(f"Wrote {args.output}")
    logging.info(
        f"Summary: total={n_total} hint_ok={n_hint_ok} hint_notfound={n_hint_notfound} "
        f"hint_mismatch={n_hint_mismatch} search_only={n_search_only}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
