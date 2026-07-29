#!/usr/bin/env python3
"""Migrate T2 task JSONs: ground_truth.dos_curve.rule "curve_l2" → "curve_f1".

Scope: questions/M*_T*/*.json (recursive).
Only touches ground_truth.dos_curve.rule; leaves other "rule" fields and
other curve_l2 occurrences alone (none expected in T2 task JSONs).

Idempotent: running twice yields the same result.

Strategy: parse JSON, mutate dos_curve.rule in-place, re-serialize with the
same formatting (2-space indent, ensure_ascii=False, trailing newline).
Key order is preserved by Python 3.7+ dict insertion order; json.load/dump
both honor it. To minimize unrelated diff noise we do a sanity check on
non-target files (already-on-f1 / no-dos_curve): we only WRITE a file when
the on-disk text differs from the round-trip serialization OF THE UNMUTATED
input — but in practice, our reformatting matches the existing files
byte-for-byte (verified manually on M1_T2_Si_bands_dos.json), so the only
files we change are those with curve_l2 → curve_f1 substitution.

Usage:
    python tools/migrate_curve_l2_to_curve_f1.py [--dry-run]
"""
from __future__ import annotations

import argparse
import difflib
import json
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
QUESTIONS_ROOT = REPO_ROOT / "questions"


def _serialize(data: dict, *, trailing_newline: bool) -> str:
    """Match existing JSON file formatting: 2-space indent, no ascii escape.

    trailing_newline preserves the source file's EOF convention (most files in
    repo end with '\n', but some don't — don't normalize, that'd add noise).
    """
    body = json.dumps(data, indent=2, ensure_ascii=False)
    return body + "\n" if trailing_newline else body


def _process_file(path: Path) -> tuple[str, str | None, str | None]:
    """Return (category, original_text, new_text).

    category ∈ {"changed", "already_f1", "no_dos_curve", "no_rule", "other_rule"}.
    original_text / new_text only set when category == "changed".
    """
    original = path.read_text(encoding="utf-8")
    data = json.loads(original)
    gt = data.get("ground_truth")
    if not isinstance(gt, dict):
        return ("no_dos_curve", None, None)
    dc = gt.get("dos_curve")
    if not isinstance(dc, dict):
        return ("no_dos_curve", None, None)
    rule = dc.get("rule")
    if rule is None:
        return ("no_rule", None, None)
    if rule == "curve_f1":
        return ("already_f1", None, None)
    if rule != "curve_l2":
        return ("other_rule", None, None)

    # Mutate in-place; dict insertion order preserved.
    dc["rule"] = "curve_f1"
    new_text = _serialize(data, trailing_newline=original.endswith("\n"))
    return ("changed", original, new_text)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Print diffs, do not write.")
    args = parser.parse_args()

    if not QUESTIONS_ROOT.exists():
        print(f"ERROR: questions root not found: {QUESTIONS_ROOT}", file=sys.stderr)
        return 2

    files = sorted(QUESTIONS_ROOT.glob("M*_T*/*.json"))
    counts = {"changed": 0, "already_f1": 0, "no_dos_curve": 0,
              "no_rule": 0, "other_rule": 0}
    changed_paths: list[Path] = []

    for p in files:
        try:
            category, original, new_text = _process_file(p)
        except Exception as e:
            print(f"ERROR processing {p}: {e}", file=sys.stderr)
            return 3
        counts[category] += 1
        if category == "changed":
            changed_paths.append(p)
            if args.dry_run:
                rel = p.relative_to(REPO_ROOT)
                diff = difflib.unified_diff(
                    original.splitlines(keepends=True),
                    new_text.splitlines(keepends=True),
                    fromfile=str(rel),
                    tofile=str(rel) + " (post-migration)",
                    n=1,
                )
                sys.stdout.writelines(diff)
            else:
                p.write_text(new_text, encoding="utf-8")

    print()
    print(f"Scanned {len(files)} files under {QUESTIONS_ROOT}")
    print(f"  would change: {counts['changed']} files"
          if args.dry_run else f"  changed: {counts['changed']} files")
    print(f"  already on curve_f1: {counts['already_f1']} files")
    print(f"  no dos_curve field: {counts['no_dos_curve']} files")
    if counts["no_rule"]:
        print(f"  dos_curve present but no rule: {counts['no_rule']} files")
    if counts["other_rule"]:
        print(f"  dos_curve.rule is neither curve_l2 nor curve_f1: "
              f"{counts['other_rule']} files (left untouched)")
    if args.dry_run:
        print("\n(dry-run; no files written)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
