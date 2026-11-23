#!/usr/bin/env python3
"""Verify bench/materials/m1_m2.yaml against Materials Project.

目的
----
`m1_m2.yaml` 由 LLM 从领域知识召回生成，字段并未经过权威数据源校验。
本脚本对每个材料：

1. 若 yaml.mp_id 有值：直接以该 mp_id 从 MP 拉 summary + primitive structure
2. 若 mp_id 为空：按 formula + space_group_number 在 MP 搜索候选，
   取 energy_above_hull 最小的稳态多型体
3. 交叉校验 YAML 与 MP 的 space_group_number / space_group symbol /
   atoms_per_primitive_cell / formula；有冲突则记入 conflict_report.md
4. 将 MP 的权威值（含 primitive lattice + atomic positions）写入
   m1_m2_resolved.yaml，**不静默覆盖** —— 有冲突的字段标注
   review_flags，保留 YAML 原值便于人工复核

输入
----
- bench/materials/m1_m2.yaml
- 环境变量 MP_API_KEY（或命令行 --api-key）

输出
----
- bench/materials/m1_m2_resolved.yaml
- bench/materials/conflict_report.md
- bench/materials/resolve.log

运行
----
    export MP_API_KEY=<your key>
    python bench/materials/resolve.py
"""
from __future__ import annotations

import argparse
import logging
import os
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import yaml

# Import MP API lazily — keep import error messages user-friendly
try:
    from mp_api.client import MPRester
    from pymatgen.symmetry.analyzer import SpacegroupAnalyzer
except ImportError as e:
    print(f"missing dependency: {e}\nrun: pip install mp-api pymatgen", file=sys.stderr)
    sys.exit(1)

HERE = Path(__file__).resolve().parent
DEFAULT_INPUT = HERE / "m1_m2.yaml"
DEFAULT_OUTPUT = HERE / "m1_m2_resolved.yaml"
DEFAULT_REPORT = HERE / "conflict_report.md"
DEFAULT_LOG = HERE / "resolve.log"


# -----------------------------------------------------------------
# YAML loader that errors on duplicate keys
# -----------------------------------------------------------------
# PyYAML's default SafeLoader silently overwrites duplicate keys (last wins),
# which masked a real bug where M1_Rb's `notes` field had two entries — the
# first (a critical warning) was silently replaced by a `notes: null`.
# This loader raises so the user sees it.

class _DuplicateKeySafeLoader(yaml.SafeLoader):
    pass


def _construct_mapping_no_dups(loader, node, deep=False):
    mapping = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in mapping:
            raise yaml.constructor.ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                f"duplicate key '{key}' at line {key_node.start_mark.line + 1}",
                key_node.start_mark,
            )
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_DuplicateKeySafeLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_mapping_no_dups
)


# -----------------------------------------------------------------
# Space group symbol normalization
# -----------------------------------------------------------------

# Accepted alias groups — any pair within a group is considered equivalent.
# Keeps YAML values like "Fd-3m" compatible with MP's "Fd-3m" or equivalent variants.
SG_ALIASES = [
    {"Fd-3m", "Fd3m", "F d -3 m", "Fd-3m:2", "Fd-3m:1"},
    {"Fm-3m", "Fm3m", "F m -3 m"},
    {"Im-3m", "Im3m", "I m -3 m"},
    {"Pm-3m", "Pm3m", "P m -3 m"},
    {"F-43m", "F43m", "F -4 3 m"},
    {"I-43m", "I43m", "I -4 3 m"},
    {"Pm-3n", "Pm3n"},
    {"Im-3", "Im3", "I m -3"},
    {"I23"},
    {"P6_3mc", "P63mc", "P 6_3 m c"},
    {"P6_3/mmc", "P63/mmc", "P 6_3/m m c"},
    {"P3_1 2 1", "P3121", "P 3_1 2 1", "P3_2 2 1", "P3221", "P 3_2 2 1"},  # enantiomorph pair
    {"Cmce", "Cmca", "C m c a", "C m c e"},                               # old/new naming
    {"C2/c", "C 2/c"},
    {"Pnma", "P n m a"},
    {"P2_1/c", "P21/c", "P 2_1/c"},
    {"P2_1/m", "P21/m", "P 2_1/m"},
    {"P2_1/n", "P21/n", "P 2_1/n"},
    {"P4_1 2_1 2", "P41212", "P 4_1 2_1 2"},
    {"P4_2/mnm", "P42/mnm"},
    {"I4/mmm", "I 4/m m m"},
    {"I4_1/amd", "I41/amd", "I 4_1/a m d"},
    {"I4/mcm", "I 4/m c m"},
    {"R-3c", "R-3c:H", "R 3c"},
    {"Cmcm", "C m c m"},
    {"P2_1 2_1 2_1", "P212121", "P 2_1 2_1 2_1"},
]


def _normalize_sg_symbol(sym: str | None) -> str:
    if not sym:
        return ""
    # Strip non-printable whitespace but preserve content
    return re.sub(r"\s+", "", sym).strip()


def sg_symbols_equivalent(a: str | None, b: str | None) -> bool:
    if not a or not b:
        return False
    na, nb = _normalize_sg_symbol(a), _normalize_sg_symbol(b)
    if na == nb:
        return True
    # Check any alias group contains both (normalized to no-whitespace form)
    for group in SG_ALIASES:
        norm_group = {_normalize_sg_symbol(s) for s in group}
        if na in norm_group and nb in norm_group:
            return True
    return False


# -----------------------------------------------------------------
# Data structures
# -----------------------------------------------------------------

@dataclass
class MaterialRecord:
    """A single material's verification result."""

    yaml_entry: dict
    mp_id: str | None = None
    mp_summary: dict | None = None
    primitive_info: dict | None = None
    status: str = "UNRESOLVED"  # VERIFIED / CONFLICT / UNRESOLVED
    conflicts: list[dict] = field(default_factory=list)
    resolution_notes: list[str] = field(default_factory=list)
    candidates_considered: list[str] = field(default_factory=list)

    @property
    def id(self) -> str:
        return self.yaml_entry["id"]

    @property
    def formula(self) -> str:
        return self.yaml_entry["formula"]


# -----------------------------------------------------------------
# MP query helpers
# -----------------------------------------------------------------

_QUERY_FIELDS = [
    "material_id",
    "formula_pretty",
    "symmetry",
    "structure",
    "nsites",
    "formation_energy_per_atom",
    "energy_above_hull",
    "is_stable",
    "deprecated",
]


def _mpdoc_to_dict(doc) -> dict:
    """Convert a ``MPDataDoc`` (pydantic v1, lacks ``model_dump``) to a plain dict.

    Uses direct attribute access rather than ``.dict()`` because mp_api's
    restricted-attribute proxy will raise AttributeError for unrequested fields.
    """
    sym = getattr(doc, "symmetry", None)
    sym_dict = None
    if sym is not None:
        sym_dict = {
            "symbol": getattr(sym, "symbol", None),
            "number": getattr(sym, "number", None),
        }
    return {
        "material_id": str(doc.material_id) if doc.material_id else None,
        "formula_pretty": getattr(doc, "formula_pretty", None),
        "symmetry": sym_dict,
        "structure": getattr(doc, "structure", None),
        "nsites": getattr(doc, "nsites", None),
        "formation_energy_per_atom": getattr(doc, "formation_energy_per_atom", None),
        "energy_above_hull": getattr(doc, "energy_above_hull", None),
        "is_stable": getattr(doc, "is_stable", None),
        "deprecated": getattr(doc, "deprecated", None),
    }


def _fetch_summary_by_mp_id(mpr: MPRester, mp_id: str) -> dict | None:
    docs = mpr.summary.search(material_ids=[mp_id], fields=_QUERY_FIELDS)
    return _mpdoc_to_dict(docs[0]) if docs else None


def _search_by_formula(
    mpr: MPRester,
    formula: str,
    sg_number: int | None,
    limit: int = 20,
) -> list[dict]:
    kwargs = {"formula": formula, "fields": _QUERY_FIELDS}
    if sg_number is not None:
        kwargs["spacegroup_number"] = sg_number
    docs = mpr.summary.search(**kwargs)
    return [_mpdoc_to_dict(d) for d in docs[:limit]]


def _pick_ground_state(candidates: list[dict]) -> tuple[dict | None, list[str]]:
    """Pick the lowest-energy non-deprecated candidate."""
    notes = []
    if not candidates:
        return None, ["no candidates"]

    alive = [c for c in candidates if not c.get("deprecated")]
    if len(alive) < len(candidates):
        notes.append(f"filtered {len(candidates) - len(alive)} deprecated entries")

    pool = alive or candidates

    def _sort_key(c):
        eah = c.get("energy_above_hull")
        return float("inf") if eah is None else eah

    pool.sort(key=_sort_key)

    if len(pool) > 1:
        top_id = pool[0]["material_id"]
        top_eah = pool[0].get("energy_above_hull")
        also = [(p["material_id"], p.get("energy_above_hull")) for p in pool[1:5]]
        notes.append(
            f"{len(pool)} candidates; picked {top_id} (E_hull={top_eah}), "
            f"others: {also}"
        )

    return pool[0], notes


# -----------------------------------------------------------------
# Per-material resolution
# -----------------------------------------------------------------

def resolve_one(mpr: MPRester, yaml_entry: dict, logger: logging.Logger) -> MaterialRecord:
    rec = MaterialRecord(yaml_entry=yaml_entry)

    # -------- Step 1: locate canonical MP entry --------
    if yaml_entry.get("mp_id"):
        mp_id_in = yaml_entry["mp_id"]
        try:
            summary = _fetch_summary_by_mp_id(mpr, mp_id_in)
        except Exception as e:
            rec.resolution_notes.append(f"mp_id direct fetch raised {type(e).__name__}: {e}")
            summary = None

        if summary is None:
            rec.resolution_notes.append(
                f"mp_id {mp_id_in} returned no result — falling back to formula search"
            )
            # fall through to formula search
        elif summary.get("deprecated"):
            rec.resolution_notes.append(
                f"mp_id {mp_id_in} is deprecated on MP — falling back to formula search"
            )
            summary = None
        else:
            rec.mp_id = mp_id_in
            rec.mp_summary = summary

    if rec.mp_summary is None:
        # Formula search
        try:
            candidates = _search_by_formula(
                mpr,
                yaml_entry["formula"],
                yaml_entry.get("space_group_number"),
            )
        except Exception as e:
            rec.resolution_notes.append(f"formula search raised {type(e).__name__}: {e}")
            candidates = []

        if yaml_entry.get("space_group_number") is None and not candidates:
            # retry without SG filter — unlikely path, but defensive
            candidates = _search_by_formula(mpr, yaml_entry["formula"], None)

        # Track all candidate ids for the conflict report
        rec.candidates_considered = [c["material_id"] for c in candidates]

        if not candidates and yaml_entry.get("space_group_number") is not None:
            # Try once more without SG filter
            try:
                candidates = _search_by_formula(mpr, yaml_entry["formula"], None)
                rec.candidates_considered = [c["material_id"] for c in candidates]
                if candidates:
                    rec.resolution_notes.append(
                        f"no match with SG filter={yaml_entry['space_group_number']}; "
                        f"relaxed filter returned {len(candidates)} candidates"
                    )
            except Exception as e:
                rec.resolution_notes.append(f"relaxed search raised {type(e).__name__}: {e}")

        chosen, pick_notes = _pick_ground_state(candidates)
        rec.resolution_notes.extend(pick_notes)
        if chosen is None:
            rec.status = "UNRESOLVED"
            return rec
        rec.mp_id = chosen["material_id"]
        rec.mp_summary = chosen

    # -------- Step 2: derive primitive structure info --------
    try:
        struct = rec.mp_summary["structure"]
        # mp-api returns a pymatgen Structure object already deserialized
        from pymatgen.core import Structure as _Struct
        if not isinstance(struct, _Struct):
            struct = _Struct.from_dict(struct)
        sga = SpacegroupAnalyzer(struct, symprec=0.01, angle_tolerance=5)
        prim = sga.get_primitive_standard_structure()
        rec.primitive_info = {
            "atoms_per_primitive_cell": int(len(prim)),
            "sga_space_group_number": int(sga.get_space_group_number()),
            "sga_space_group_symbol": str(sga.get_space_group_symbol()),
            "lattice_a": float(round(prim.lattice.a, 6)),
            "lattice_b": float(round(prim.lattice.b, 6)),
            "lattice_c": float(round(prim.lattice.c, 6)),
            "lattice_alpha": float(round(prim.lattice.alpha, 4)),
            "lattice_beta": float(round(prim.lattice.beta, 4)),
            "lattice_gamma": float(round(prim.lattice.gamma, 4)),
            "sites": [
                {
                    "element": str(site.specie.symbol),
                    "position": [float(round(c, 6)) for c in site.frac_coords],
                }
                for site in prim.sites
            ],
        }
    except Exception as e:
        rec.resolution_notes.append(
            f"primitive structure derivation raised {type(e).__name__}: {e}"
        )

    # -------- Step 3: cross-check YAML vs MP authoritative --------
    # MP has TWO sources of truth per entry: summary.symmetry (metadata) and the
    # actual structure (from which spglib can independently derive SG). For at
    # least one material (In / mp-85) these disagree — MP metadata says Fm-3m
    # but the structure is unambiguously I4/mmm. We trust spglib-on-structure.
    sym = rec.mp_summary.get("symmetry") or {}
    mp_meta_sg_number = sym.get("number") if isinstance(sym, dict) else getattr(sym, "number", None)
    mp_meta_sg_symbol = sym.get("symbol") if isinstance(sym, dict) else getattr(sym, "symbol", None)
    mp_formula = rec.mp_summary.get("formula_pretty")

    # Prefer spglib-derived values as the authoritative MP value.
    mp_sg_number = None
    mp_sg_symbol = None
    if rec.primitive_info:
        mp_sg_number = rec.primitive_info["sga_space_group_number"]
        mp_sg_symbol = rec.primitive_info["sga_space_group_symbol"]
    if mp_sg_number is None:
        mp_sg_number = mp_meta_sg_number
    if mp_sg_symbol is None:
        mp_sg_symbol = mp_meta_sg_symbol

    def _record_conflict(field_name, yaml_val, mp_val, severity="warning"):
        rec.conflicts.append({
            "field": field_name,
            "yaml_value": yaml_val,
            "mp_value": mp_val,
            "severity": severity,
        })

    # Internal MP inconsistency: summary.symmetry ≠ spglib-on-structure.
    # This is a quality issue in MP's own data and should NOT silently override
    # YAML — log it loudly so downstream code knows to trust structure-derived SG.
    if (
        rec.primitive_info
        and mp_meta_sg_number is not None
        and mp_meta_sg_number != rec.primitive_info["sga_space_group_number"]
    ):
        rec.resolution_notes.append(
            f"⚠ MP internal inconsistency: summary.symmetry reports SG "
            f"{mp_meta_sg_symbol} ({mp_meta_sg_number}) but spglib on the "
            f"returned structure gives {rec.primitive_info['sga_space_group_symbol']} "
            f"({rec.primitive_info['sga_space_group_number']}). Trusting spglib."
        )

    # Formula — compare via pymatgen Composition to treat
    # "ZnAl2O4" / "Al2ZnO4" / "Al2O4Zn" as equivalent (same composition).
    # Only flag when the actual element composition differs.
    yaml_formula = yaml_entry["formula"]
    if mp_formula and yaml_formula != mp_formula:
        try:
            from pymatgen.core import Composition
            yaml_comp = Composition(yaml_formula).reduced_composition
            mp_comp = Composition(mp_formula).reduced_composition
            if yaml_comp != mp_comp:
                _record_conflict("formula", yaml_formula, mp_formula, severity="notice")
            # else: same composition, different string form — not a real conflict
        except Exception:
            _record_conflict("formula", yaml_formula, mp_formula, severity="notice")

    # Space group number — compare against structure-derived SG (authoritative)
    yaml_sg_num = yaml_entry.get("space_group_number")
    if yaml_sg_num is not None and mp_sg_number is not None and yaml_sg_num != mp_sg_number:
        _record_conflict("space_group_number", yaml_sg_num, mp_sg_number, severity="critical")

    # Space group symbol — use alias-aware comparison
    yaml_sg_sym = yaml_entry.get("space_group")
    if yaml_sg_sym and mp_sg_symbol and not sg_symbols_equivalent(yaml_sg_sym, mp_sg_symbol):
        _record_conflict("space_group", yaml_sg_sym, mp_sg_symbol, severity="warning")

    # Atoms per primitive cell
    yaml_atoms = yaml_entry.get("atoms_per_primitive_cell")
    if rec.primitive_info and yaml_atoms is not None:
        mp_atoms = rec.primitive_info["atoms_per_primitive_cell"]
        if yaml_atoms != mp_atoms:
            _record_conflict("atoms_per_primitive_cell", yaml_atoms, mp_atoms, severity="critical")

    # MP id sanity: if YAML gave one, did we end up using it?
    yaml_mp_id = yaml_entry.get("mp_id")
    if yaml_mp_id and rec.mp_id and yaml_mp_id != rec.mp_id:
        _record_conflict("mp_id", yaml_mp_id, rec.mp_id, severity="notice")

    # -------- Step 4: set status --------
    if not rec.conflicts:
        rec.status = "VERIFIED"
    else:
        rec.status = "CONFLICT"

    return rec


# -----------------------------------------------------------------
# Output writers
# -----------------------------------------------------------------

def _yaml_sanitize(obj):
    """Recursively convert numpy scalars and other non-native types to yaml-safe python types."""
    try:
        import numpy as np
    except ImportError:
        np = None
    if obj is None or isinstance(obj, (bool, int, float, str)):
        return obj
    if np is not None and isinstance(obj, np.generic):
        return obj.item()
    if isinstance(obj, dict):
        return {k: _yaml_sanitize(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_yaml_sanitize(v) for v in obj]
    # fall back: stringify anything else
    return str(obj)


def write_resolved_yaml(records: list[MaterialRecord], yaml_data: dict, path: Path) -> None:
    """Write authoritative (MP-valued) YAML, preserving YAML structure + adding review flags."""

    out = {
        "library_version": yaml_data.get("library_version"),
        "pseudo_path": yaml_data.get("pseudo_path"),
        "generated_at": yaml_data.get("generated_at"),
        "generated_by": yaml_data.get("generated_by"),
        "resolved_at": time.strftime("%Y-%m-%d"),
        "source_yaml": "m1_m2.yaml",
        "source_document": yaml_data.get("source_document"),
        "first_batch_plan": yaml_data.get("first_batch_plan"),
        "resolution_summary": {
            "total": len(records),
            "verified": sum(1 for r in records if r.status == "VERIFIED"),
            "conflict": sum(1 for r in records if r.status == "CONFLICT"),
            "unresolved": sum(1 for r in records if r.status == "UNRESOLVED"),
        },
    }

    # Re-pack by tier/category, overwriting fields with MP-authoritative values
    for tier in ("M1", "M2"):
        out[tier] = {
            "description": yaml_data[tier].get("description"),
            "categories": {},
        }
        for cat_key, cat in yaml_data[tier]["categories"].items():
            out[tier]["categories"][cat_key] = {
                "label": cat.get("label"),
                "materials": [],
            }
            for mat in cat["materials"]:
                rec = next((r for r in records if r.id == mat["id"]), None)
                if rec is None:
                    out[tier]["categories"][cat_key]["materials"].append(mat)
                    continue

                mp_sum = rec.mp_summary or {}
                sym = mp_sum.get("symmetry") or {}

                # Build merged entry: MP values take precedence (if available),
                # review_flags captures every conflict
                merged = dict(mat)
                merged["resolution_status"] = rec.status
                merged["mp_id"] = rec.mp_id or mat.get("mp_id")
                # Authoritative SG comes from spglib on the MP structure, NOT from
                # MP's summary.symmetry metadata — they can disagree (e.g. mp-85 In).
                if rec.primitive_info:
                    merged["space_group"] = rec.primitive_info["sga_space_group_symbol"]
                    merged["space_group_number"] = rec.primitive_info["sga_space_group_number"]
                    merged["atoms_per_primitive_cell"] = rec.primitive_info["atoms_per_primitive_cell"]
                    merged["initial_structure_from_mp"] = {
                        "lattice_a": rec.primitive_info["lattice_a"],
                        "lattice_b": rec.primitive_info["lattice_b"],
                        "lattice_c": rec.primitive_info["lattice_c"],
                        "alpha": rec.primitive_info["lattice_alpha"],
                        "beta": rec.primitive_info["lattice_beta"],
                        "gamma": rec.primitive_info["lattice_gamma"],
                        "sites": rec.primitive_info["sites"],
                    }
                    # Keep MP's own metadata for audit — useful to detect MP quality issues
                    merged["mp_metadata_symmetry"] = {
                        "symbol": sym.get("symbol") if isinstance(sym, dict) else None,
                        "number": sym.get("number") if isinstance(sym, dict) else None,
                    }
                elif mp_sum.get("symmetry"):
                    # fallback if primitive derivation failed
                    merged["space_group"] = sym.get("symbol") or mat.get("space_group")
                    merged["space_group_number"] = sym.get("number") or mat.get("space_group_number")
                merged["mp_energy_above_hull"] = mp_sum.get("energy_above_hull")
                merged["mp_is_stable"] = mp_sum.get("is_stable")

                if rec.conflicts:
                    merged["review_flags"] = {
                        "conflicts": rec.conflicts,
                        "notes": rec.resolution_notes,
                        "yaml_original_values": {
                            "mp_id": mat.get("mp_id"),
                            "space_group": mat.get("space_group"),
                            "space_group_number": mat.get("space_group_number"),
                            "atoms_per_primitive_cell": mat.get("atoms_per_primitive_cell"),
                        },
                    }
                elif rec.resolution_notes:
                    merged["resolution_notes"] = rec.resolution_notes

                out[tier]["categories"][cat_key]["materials"].append(merged)

    with path.open("w") as f:
        yaml.safe_dump(_yaml_sanitize(out), f, allow_unicode=True, sort_keys=False, default_flow_style=False)


def write_conflict_report(records: list[MaterialRecord], path: Path) -> None:
    """Human-readable markdown report, sorted by severity."""

    verified = [r for r in records if r.status == "VERIFIED"]
    conflicts = [r for r in records if r.status == "CONFLICT"]
    unresolved = [r for r in records if r.status == "UNRESOLVED"]

    lines = [
        "# Material Resolution Conflict Report",
        "",
        f"Generated: {time.strftime('%Y-%m-%d %H:%M')}",
        "",
        "Cross-checked `bench/materials/m1_m2.yaml` against Materials Project.",
        "Conflicts below require human triage before question generation runs.",
        "",
        "## Summary",
        "",
        f"- Total materials: **{len(records)}**",
        f"- Verified (no conflicts): **{len(verified)}**",
        f"- Conflicts (MP ≠ YAML on one or more fields): **{len(conflicts)}**",
        f"- Unresolved (MP returned no match): **{len(unresolved)}**",
        "",
    ]

    if unresolved:
        lines += [
            "## UNRESOLVED — MP query returned no match",
            "",
            "These materials could not be located in MP. Either the formula / space group in YAML is wrong, or MP truly lacks this polymorph. Requires manual lookup.",
            "",
        ]
        for r in unresolved:
            lines += [
                f"### `{r.id}` — {r.formula}",
                "",
                f"- YAML mp_id: `{r.yaml_entry.get('mp_id')}`",
                f"- YAML formula: `{r.formula}`",
                f"- YAML space_group_number: `{r.yaml_entry.get('space_group_number')}`",
                f"- Candidates searched: {r.candidates_considered or 'none'}",
                "- Notes:",
            ]
            lines += [f"  - {n}" for n in r.resolution_notes] or ["  - (none)"]
            lines.append("")

    # Group conflicts by severity
    if conflicts:
        by_sev = {"critical": [], "warning": [], "notice": []}
        for r in conflicts:
            max_sev = "notice"
            for c in r.conflicts:
                if c["severity"] == "critical":
                    max_sev = "critical"
                    break
                if c["severity"] == "warning" and max_sev != "critical":
                    max_sev = "warning"
            by_sev[max_sev].append(r)

        for sev, header in [
            ("critical", "CRITICAL — SG number or atom-count mismatch (likely wrong polymorph)"),
            ("warning", "WARNING — SG symbol disagreement (often notation difference)"),
            ("notice", "NOTICE — minor disagreement (mp_id substitution / formula casing)"),
        ]:
            if not by_sev[sev]:
                continue
            lines += [f"## {header}", ""]
            for r in by_sev[sev]:
                mp_sum = r.mp_summary or {}
                sym = mp_sum.get("symmetry") or {}
                lines += [
                    f"### `{r.id}` — {r.formula}",
                    "",
                    f"- Resolved to **mp_id: `{r.mp_id}`**",
                    f"- MP formula_pretty: `{mp_sum.get('formula_pretty')}`",
                    f"- MP energy_above_hull: `{mp_sum.get('energy_above_hull')}` eV/atom",
                    f"- MP is_stable: `{mp_sum.get('is_stable')}`",
                    "",
                    "| Field | YAML | MP (authoritative) | Severity |",
                    "|---|---|---|---|",
                ]
                for c in r.conflicts:
                    lines.append(
                        f"| {c['field']} | `{c['yaml_value']}` | `{c['mp_value']}` | {c['severity']} |"
                    )
                if r.resolution_notes:
                    lines += ["", "Notes:"]
                    lines += [f"- {n}" for n in r.resolution_notes]
                lines.append("")

    if verified:
        lines += [
            "## VERIFIED — YAML values match MP",
            "",
            "These passed all checks (formula, space group number, space group symbol, atoms per primitive cell).",
            "",
        ]
        for r in verified:
            lines.append(f"- `{r.id}` — {r.formula} (mp_id: `{r.mp_id}`)")

    lines.append("")
    path.write_text("\n".join(lines))


# -----------------------------------------------------------------
# Main
# -----------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    ap.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    ap.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    ap.add_argument("--log", type=Path, default=DEFAULT_LOG)
    ap.add_argument("--api-key", default=os.environ.get("MP_API_KEY"))
    ap.add_argument("--limit", type=int, default=None, help="(debug) process only N materials")
    ap.add_argument("--dry-run", action="store_true", help="list materials but do not query MP")
    args = ap.parse_args()

    if not args.api_key and not args.dry_run:
        sys.exit(
            "error: MP_API_KEY not set.\n"
            "  export MP_API_KEY=<key>\n"
            "  or pass --api-key <key>\n"
        )

    # Logger
    logger = logging.getLogger("resolve")
    logger.setLevel(logging.INFO)
    fh = logging.FileHandler(args.log, mode="w")
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(fh)
    ch = logging.StreamHandler()
    ch.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(ch)

    # Load YAML with duplicate-key detection
    with args.input.open() as f:
        try:
            yaml_data = yaml.load(f, Loader=_DuplicateKeySafeLoader)
        except yaml.constructor.ConstructorError as e:
            sys.exit(
                f"error: YAML parse failed — {e.problem}\n"
                f"  in {args.input}\n"
                f"  (duplicate keys silently swallow preceding values — fix before rerunning)"
            )

    all_entries = []
    for tier in ("M1", "M2"):
        for cat in yaml_data[tier]["categories"].values():
            for m in cat["materials"]:
                all_entries.append(m)

    if args.limit:
        all_entries = all_entries[: args.limit]

    logger.info(f"loaded {len(all_entries)} materials from {args.input}")

    if args.dry_run:
        for m in all_entries:
            logger.info(f"  {m['id']:35s} {m['formula']:12s} mp_id={m.get('mp_id')}")
        return

    # Query MP
    records: list[MaterialRecord] = []
    with MPRester(args.api_key) as mpr:
        for i, entry in enumerate(all_entries, 1):
            logger.info(f"[{i:3d}/{len(all_entries)}] {entry['id']:35s} ...")
            try:
                rec = resolve_one(mpr, entry, logger)
            except Exception as e:
                logger.exception(f"  FAILED {entry['id']}: {e}")
                rec = MaterialRecord(yaml_entry=entry, status="UNRESOLVED")
                rec.resolution_notes.append(f"exception: {type(e).__name__}: {e}")
            logger.info(
                f"    → {rec.status}  mp_id={rec.mp_id}  conflicts={len(rec.conflicts)}"
            )
            records.append(rec)
            time.sleep(0.1)

    # Summary
    n_verified = sum(1 for r in records if r.status == "VERIFIED")
    n_conflict = sum(1 for r in records if r.status == "CONFLICT")
    n_unresolved = sum(1 for r in records if r.status == "UNRESOLVED")
    logger.info("")
    logger.info("=" * 60)
    logger.info(f"total:     {len(records)}")
    logger.info(f"verified:  {n_verified}  ({100*n_verified/len(records):.0f}%)")
    logger.info(f"conflict:  {n_conflict}  ({100*n_conflict/len(records):.0f}%)")
    logger.info(f"unresolved: {n_unresolved}  ({100*n_unresolved/len(records):.0f}%)")
    logger.info("=" * 60)

    # Write outputs
    write_resolved_yaml(records, yaml_data, args.output)
    write_conflict_report(records, args.report)
    logger.info(f"wrote {args.output}")
    logger.info(f"wrote {args.report}")
    logger.info(f"log   {args.log}")


if __name__ == "__main__":
    main()
