"""Experience library — Reflexion-style methodology lessons per step kind.

Lessons describe HOW to do DFT (e.g. "metals need smearing for scf"),
NEVER task-answer values (e.g. "Si gap is indirect"). A regex validator
enforces the methodology-only constraint at write time so the library
can be defended as "a QE engineering manual" rather than a cached
answer set.

Public surface:
  - `load_lessons(step_kind, scope, repo_root) -> list[dict]`
  - `append_lesson(step_kind, lesson, scope, repo_root) -> dict`
  - `validate_lesson(lesson, required_keys) -> tuple[bool, list[str]]`

Storage: `<repo_root>/experience/<step_kind>.jsonl`, one lesson per line,
append-only forever. No TTL, no pruning, no votes.

Validator strategy:
  1. Shape check: dict with `trigger`/`action`/`rationale`, each a string ≤ 200 chars.
  2. QE-keyword mask: replace `<keyword>=<value>` (whitelist of QE method params)
     with `<keyword>=__masked__` so legitimate method values don't trip the unit regex.
  3. Reject if the MASKED text contains:
       - any 118-element symbol (word-bounded)
       - any verbatim token from required_keys (e.g. `bands_data`, `gap_type`)
       - any `<number><unit>` pattern (eV/GPa/Å/Ry/bohr/kbar) — answer-leak shape
"""
from __future__ import annotations

import json
import re
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


# ─────────────────────── element-symbol denylist ─────────────────────────────

def _build_element_symbols() -> frozenset[str]:
    """Enumerate the 118 IUPAC element symbols. Use pymatgen if available
    (single source of truth), else a hardcoded fallback."""
    try:
        from pymatgen.core.periodic_table import Element  # type: ignore
        return frozenset(e.symbol for e in Element)
    except Exception:
        # Hardcoded 118-element fallback. Order matches IUPAC.
        return frozenset((
            "H","He","Li","Be","B","C","N","O","F","Ne",
            "Na","Mg","Al","Si","P","S","Cl","Ar",
            "K","Ca","Sc","Ti","V","Cr","Mn","Fe","Co","Ni","Cu","Zn",
            "Ga","Ge","As","Se","Br","Kr",
            "Rb","Sr","Y","Zr","Nb","Mo","Tc","Ru","Rh","Pd","Ag","Cd",
            "In","Sn","Sb","Te","I","Xe",
            "Cs","Ba","La","Ce","Pr","Nd","Pm","Sm","Eu","Gd","Tb","Dy",
            "Ho","Er","Tm","Yb","Lu",
            "Hf","Ta","W","Re","Os","Ir","Pt","Au","Hg",
            "Tl","Pb","Bi","Po","At","Rn",
            "Fr","Ra","Ac","Th","Pa","U","Np","Pu","Am","Cm","Bk","Cf",
            "Es","Fm","Md","No","Lr",
            "Rf","Db","Sg","Bh","Hs","Mt","Ds","Rg","Cn",
            "Nh","Fl","Mc","Lv","Ts","Og",
        ))


_ELEMENT_SYMBOLS: frozenset[str] = _build_element_symbols()
# Word-boundary regex matching any element symbol. Compiled once.
_ELEMENT_RE = re.compile(
    r"\b(" + "|".join(sorted(_ELEMENT_SYMBOLS, key=len, reverse=True)) + r")\b"
)


# ─────────────────────── QE-keyword whitelist (masked first) ─────────────────

_QE_KEYWORDS = (
    "ecutwfc", "ecutrho",
    "mixing_beta", "mixing_mode",
    "conv_thr", "etot_conv_thr", "forc_conv_thr", "press_conv_thr",
    "occupations", "smearing", "degauss",
    "nbnd", "nstep", "electron_maxstep",
    "diagonalization", "startingwfc",
    "k_points", "kpath",
    "cell_dofree", "ion_dynamics", "cell_dynamics",
)
# `<keyword> = <value>` where value runs up to the next comma / whitespace / EOL.
# Anchored at word boundary so `pre_mixing_beta` won't match.
_QE_KEYWORD_RE = re.compile(
    r"\b(" + "|".join(_QE_KEYWORDS) + r")\s*=\s*[^,\s]+",
    flags=re.IGNORECASE,
)


# ─────────────────────── numeric-unit denylist (after masking) ───────────────

_UNIT_RE = re.compile(
    r"\b\d+(?:\.\d+)?\s*(?:eV|GPa|Å|Angstrom|Ry|bohr|kbar)\b",
    flags=re.IGNORECASE,
)


# ─────────────────────── public API ──────────────────────────────────────────

_FIELDS = ("trigger", "action", "rationale")
_MAX_FIELD_CHARS = 200
_TOP_K = 5


def _experience_path(step_kind: str, repo_root: Path) -> Path:
    return repo_root / "experience" / f"{step_kind}.jsonl"


def _now_iso() -> str:
    # microsecond precision — tests and production both call append_lesson()
    # multiple times per second; seconds-only resolution makes ordering ambiguous.
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _lesson_id() -> str:
    return secrets.token_hex(4)   # 8 hex chars


def validate_lesson(lesson: Any, required_keys: list[str]) -> tuple[bool, list[str]]:
    """Return (is_valid, reasons). Reasons is empty on valid.

    Validates a candidate lesson dict against the methodology-only contract.
    Both fields must be strings ≤ 200 chars; the concatenated text must not
    contain element symbols, required_keys tokens, or `<number><unit>` outside
    of legitimate `<qe_keyword>=<value>` pairs.
    """
    reasons: list[str] = []

    # 1. Shape check
    if not isinstance(lesson, dict):
        return False, [f"lesson must be a dict, got {type(lesson).__name__}"]
    for f in _FIELDS:
        v = lesson.get(f)
        if not isinstance(v, str):
            reasons.append(f"missing/non-string field: {f!r}")
            continue
        if not v.strip():
            reasons.append(f"empty field: {f!r}")
            continue
        if len(v) > _MAX_FIELD_CHARS:
            reasons.append(f"field {f!r} exceeds {_MAX_FIELD_CHARS} chars ({len(v)})")
    if reasons:
        return False, reasons

    # 2. Mask QE-keyword RHS values, then run denylists on the remainder
    text = " || ".join(str(lesson[f]) for f in _FIELDS)
    masked = _QE_KEYWORD_RE.sub(lambda m: f"{m.group(1)}=__masked__", text)

    # 2a. element symbols
    el_matches = _ELEMENT_RE.findall(masked)
    if el_matches:
        # Dedup preserving order so the message is short
        seen: dict[str, None] = {}
        for e in el_matches:
            seen.setdefault(e, None)
        reasons.append(f"contains element symbol(s): {','.join(seen)}")

    # 2b. required_keys (verbatim, case-sensitive — keys are mechanical tokens)
    leaked_keys = [k for k in required_keys if k and k in masked]
    if leaked_keys:
        reasons.append(f"contains required_keys token(s): {','.join(leaked_keys)}")

    # 2c. number+unit shape
    unit_matches = _UNIT_RE.findall(masked)
    if unit_matches:
        # findall on the unit regex returns the unit group; surface the full match instead
        full = _UNIT_RE.findall(masked)
        reasons.append(f"contains number+unit pattern(s): {len(full)} match(es)")

    return (len(reasons) == 0), reasons


def append_lesson(
    step_kind: str,
    lesson: dict[str, str],
    scope: dict[str, Any],
    repo_root: Path,
) -> dict[str, Any]:
    """Append a validated lesson to `<repo_root>/experience/<step_kind>.jsonl`.

    Caller is responsible for validating first (this function does not re-run
    validate_lesson). Returns the full entry written (including id + written_at).
    """
    entry = {
        "id": _lesson_id(),
        "scope": dict(scope),
        "lesson": {f: lesson[f] for f in _FIELDS},
        "written_at": _now_iso(),
    }
    path = _experience_path(step_kind, repo_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(entry, ensure_ascii=False)
    with path.open("a", encoding="utf-8") as f:
        f.write(line + "\n")
        f.flush()
    return entry


def load_lessons(
    step_kind: str,
    scope: dict[str, Any],
    repo_root: Path,
) -> list[dict[str, Any]]:
    """Retrieve up to TOP_K lessons matching scope (newest first, deduped).

    Match policy:
      1. Exact match on (task_kind, crystal_system, n_atoms_bucket, step_kind).
      2. If 0 matches, fall back to {task_kind, step_kind}-only match.
      3. If still 0, return [].

    Dedup is on (trigger, action) tuple — same advice from multiple runs
    only contributes once.
    """
    path = _experience_path(step_kind, repo_root)
    if not path.exists():
        return []

    entries: list[dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        return []

    if not entries:
        return []

    want_full = {
        "task_kind":         scope.get("task_kind"),
        "crystal_system":    scope.get("crystal_system"),
        "n_atoms_bucket":    scope.get("n_atoms_bucket"),
        "step_kind":         step_kind,
    }
    want_loose = {
        "task_kind": scope.get("task_kind"),
        "step_kind": step_kind,
    }

    def _matches(entry_scope: dict[str, Any], want: dict[str, Any]) -> bool:
        return all(entry_scope.get(k) == v for k, v in want.items())

    full = [e for e in entries if _matches(e.get("scope", {}), want_full)]
    chosen = full or [e for e in entries if _matches(e.get("scope", {}), want_loose)]
    if not chosen:
        return []

    # Newest first (written_at is iso8601, lex-sortable).
    chosen.sort(key=lambda e: e.get("written_at", ""), reverse=True)

    # Dedup on (trigger, action) preserving newest-first order.
    seen: set[tuple[str, str]] = set()
    out: list[dict[str, Any]] = []
    for e in chosen:
        les = e.get("lesson", {})
        key = (str(les.get("trigger", "")), str(les.get("action", "")))
        if key in seen:
            continue
        seen.add(key)
        out.append(e)
        if len(out) >= _TOP_K:
            break
    return out
