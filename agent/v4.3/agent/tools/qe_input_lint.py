"""QE input linter — pre-spawn auto-corrections on pw.x inputs.

Hooked into BashTool.run() before subprocess spawn. Five independent rules:

  Rule A (v3): vc-relax convergence thresholds
    LLMs (haiku especially) silently drop the convergence thresholds the
    system prompt recommends — ~25 % miss rate across v1+v2, deterministic
    0.25-point loss (cubic → trigonal via pymatgen SpacegroupAnalyzer when
    relaxed cell drifts ≥0.01°/0.05 %). See docs/cpu_vs_gpu_2026-05-10.md.

  Rule B (v4 Phase 2): bands NSCF must use calculation='nscf', not 'bands'
    Bench evaluator parses VBM/CBM from a `highest occupied (lowest
    unoccupied) levels` line. QE only writes this line in calculation='nscf'
    mode (not 'bands'). LLMs that follow the legacy prompt write
    calculation='bands' on the k-path NSCF, evaluator falls back to SCF
    Fermi as a splitter → metal-misread or fermi-misread depending on the
    system.

  Rule C (v4.1 Phase 4): ecutrho ratio for USPP pseudos
    GBRV USPP and any other USPP/PAW pseudo family needs ecutrho ≈ 10 ×
    ecutwfc. Agents (or qe_tools.write_pw_input's 8× default) sometimes
    pick 4× or 8× → charge density cutoff insufficient → band gap drifts
    0.2–0.5 eV per 30 Ry ecutwfc gap. Auto-fix to 10× when pseudo is USPP.

  Rule D (v4.1 Phase 4): occupations vs config_advisor recommendation
    Reads workdir/.config_advice.json (written by config_advisor preflight
    subagent). Warns when SCF/bands-NSCF occupations don't match advice
    (metals need smearing, insulators need fixed). Warn-only — never
    auto-corrects because the advice can be wrong for edge cases.

  Rule E (v4.3 Phase 5 / 2026-05-19): K_POINTS crystal_b data-row validation
    crystal_b/tpiba_b's 4th column is the integer number of points to
    interpolate to the NEXT vertex (NOT a weight). LLMs sometimes write a
    pre-interpolated explicit path with column-4 = 0 on every row, which
    leaves QE emitting only the vertices, causing metals like Rb to miss
    EF crossings and bench's fermi-slice fallback to extract a bogus gap.
    Hard reject — pw.x is NOT run; LLM must rewrite the file. Rationale:
    bands.x on a broken k-path is wall-clock waste with no recoverable
    output. Reject reason explains both legal forms (crystal_b with
    subdivisions, or crystal with weights).

A/B/C auto-patch in place; D advises; E rejects (raises LintReject which
the bash tool surfaces as exit_code=2 + stderr). All rules are idempotent
when applied to already-good inputs.
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path


# v3 defaults: every vc-relax input is forced to be at least this tight.
# "Tighter" means SMALLER value — we always take min(LLM_value, default).
_V3_TIGHT_DEFAULTS: dict[str, float] = {
    "press_conv_thr": 0.05,    # kbar; QE default 0.5
    "forc_conv_thr": 1.0e-4,   # Ry/Bohr; QE default 1e-3
    "etot_conv_thr": 1.0e-5,   # Ry; QE default 1e-4
}

# Text serialization preserved across runs: matches the prompt's few-shot
# example so the LLM sees the same form whether it wrote it or we did.
_V3_DEFAULT_TEXT: dict[str, str] = {
    "press_conv_thr": "0.05",
    "forc_conv_thr": "1.0d-4",
    "etot_conv_thr": "1.0d-5",
}

# Each threshold belongs to a specific QE namelist (per pw.x INPUT_PW.html).
_NAMELIST_FOR: dict[str, str] = {
    "press_conv_thr": "cell",
    "forc_conv_thr": "control",
    "etot_conv_thr": "control",
}

# Detect pw.x invocation. Matches `pw.x` standalone or as `/path/to/pw.x`,
# wrapped or not by mpirun. The leading-boundary alternative covers both
# "command starts with pw.x" and "pw.x preceded by whitespace or path-sep".
_PW_X_RE = re.compile(r"(?:^|/|\s)pw\.x\b")

# pw.x input flag — supports -in, -inp, -input. Skip `-i` alone (ambiguous
# with mpirun's `-i`).
_FLAG_INPUT_RE = re.compile(r"-(?:input|inp|in)\s+(\S+)")

# Stdin redirect: `pw.x ... < input.in`
_STDIN_INPUT_RE = re.compile(r"<\s*(\S+)")

# Fortran namelist body: `&NAME ... /`. The closing `/` must be preceded
# by whitespace and followed by end-of-line or end-of-string, otherwise
# stray `/` inside string values like `'./pseudos/'` would be misread as
# the namelist terminator. Supports both multi-line and one-line forms:
#   &CONTROL\n  calculation = 'vc-relax',\n/\n
#   &CONTROL calculation='vc-relax', /\n
_NAMELIST_BLOCK_RE = re.compile(
    r"&(?P<name>[A-Za-z_]+)\b(?P<body>.*?)\s/[ \t]*(?:\n|\Z)",
    re.DOTALL,
)

# key = value entries inside a namelist body
_KEY_VALUE_RE = re.compile(
    r"(?P<key>[A-Za-z_][A-Za-z_0-9()]*)\s*=\s*(?P<val>[^,\n!]+?)\s*(?=[,\n!]|$)",
    re.MULTILINE,
)

# Bands-path mode signature: `K_POINTS crystal_b` (or tpiba_b / crystal_b /
# tpiba_b — bands.x post-processing wants a k-path, and these are the only
# QE k-point formats that take a path of segments). Case-insensitive.
# Used to distinguish "bands NSCF on k-path" from "DOS NSCF on dense mesh"
# — only the former should be rewritten to nscf+calc when calculation='bands'.
_BANDS_PATH_KPOINTS_RE = re.compile(
    r"^\s*K_POINTS\s+\{?\s*(?:crystal_b|tpiba_b)\s*\}?\s*$",
    re.MULTILINE | re.IGNORECASE,
)

# A `calculation = 'bands'` assignment we may need to rewrite. We match
# the whole assignment so we can replace just the value while preserving
# whatever quote style and trailing comma the LLM used.
_CALC_BANDS_RE = re.compile(
    r"(calculation\s*=\s*)(['\"])bands\2",
    re.IGNORECASE,
)


class LintReject(Exception):
    """A lint rule raises this when the input file is malformed in a way
    that wastes wall-clock if pw.x runs. Caller short-circuits the bash
    invocation and surfaces `reason` to the LLM so it rewrites the file.
    """
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


@dataclass
class LintResult:
    patched: bool = False
    file_path: Path | None = None
    changes: list[tuple[str, str, str]] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    diff_text: str = ""
    cubic_flag: bool | None = None
    skipped_reason: str = ""
    rejected: bool = False
    reject_reason: str = ""


def _fortran_to_float(s: str) -> float | None:
    """Convert a Fortran-style numeric literal to a Python float.
    Handles `1.0d-4`, `1.0e-4`, `1.D-5`, `0.05`, `.5`, `5.`, `5`.
    Returns None if unparseable.
    """
    if s is None:
        return None
    cleaned = s.strip().rstrip(",").strip()
    if not cleaned:
        return None
    cleaned = cleaned.replace("D", "E").replace("d", "e")
    try:
        return float(cleaned)
    except ValueError:
        return None


def _parse_input(text: str) -> dict[str, dict[str, str]]:
    """Parse a pw.x input file into {namelist_lower: {key_lower: raw_value}}.
    Preserves raw value text for round-trip serialization.
    """
    result: dict[str, dict[str, str]] = {}
    for m in _NAMELIST_BLOCK_RE.finditer(text):
        nl = m.group("name").lower()
        body = m.group("body")
        kv: dict[str, str] = {}
        for kvm in _KEY_VALUE_RE.finditer(body):
            kv[kvm.group("key").lower()] = kvm.group("val").strip()
        result[nl] = kv
    return result


def _is_vc_relax(parsed: dict) -> bool:
    """True if &CONTROL.calculation == 'vc-relax' (case-insensitive,
    quote-stripped)."""
    calc = parsed.get("control", {}).get("calculation", "")
    return calc.strip().strip("'\"").lower() == "vc-relax"


def _detect_cubic(parsed: dict, full_text: str) -> bool | None:
    """Best-effort cubic detection. Returns True / False / None.
    Logged only — not gating in v3 (every vc-relax gets tight thresholds).

    Cubic-ish criteria:
      - ibrav ∈ {1, 2, 3} → simple/FCC/BCC cubic
      - ibrav == 0 with CELL_PARAMETERS rows yielding a≈b≈c & all 90°
    """
    ibrav_raw = parsed.get("system", {}).get("ibrav")
    if ibrav_raw is None:
        return None
    try:
        ibrav = int(ibrav_raw.strip().rstrip(","))
    except (ValueError, AttributeError):
        return None
    if ibrav in (1, 2, 3):
        return True
    if ibrav != 0:
        return False
    # ibrav == 0: check CELL_PARAMETERS
    cp_match = re.search(
        r"CELL_PARAMETERS[^\n]*\n((?:[^&\n]+\n){3})",
        full_text,
    )
    if not cp_match:
        return None
    rows = []
    for line in cp_match.group(1).splitlines():
        parts = line.strip().split()
        if len(parts) < 3:
            return None
        try:
            rows.append([float(x) for x in parts[:3]])
        except ValueError:
            return None
    if len(rows) != 3:
        return None
    import math
    norms = [math.sqrt(sum(x * x for x in r)) for r in rows]
    if min(norms) <= 0:
        return None
    a, b, c = norms
    rel_diff = max(abs(a - b) / max(a, b), abs(b - c) / max(b, c), abs(a - c) / max(a, c))
    if rel_diff > 5e-3:
        return False

    def _ang(u, v):
        nu = math.sqrt(sum(x * x for x in u))
        nv = math.sqrt(sum(x * x for x in v))
        cos = max(-1.0, min(1.0, sum(u[i] * v[i] for i in range(3)) / (nu * nv)))
        return math.degrees(math.acos(cos))

    angles = [_ang(rows[1], rows[2]), _ang(rows[0], rows[2]), _ang(rows[0], rows[1])]
    return all(abs(a - 90.0) <= 0.5 for a in angles)


def _merge_value(key: str, current_val: str | None) -> tuple[str, str | None]:
    """Decide what value should land in the file for `key`, given `current_val`
    (raw text from the input, or None if missing).

    Returns (new_value_text, change_kind):
      change_kind = None       → no change needed
      change_kind = "added"    → key was missing
      change_kind = "tightened"→ value was looser than v3 default; replace
    """
    default_text = _V3_DEFAULT_TEXT[key]
    default_num = _V3_TIGHT_DEFAULTS[key]
    if current_val is None:
        return default_text, "added"
    cur_num = _fortran_to_float(current_val)
    if cur_num is None:
        return default_text, "tightened"
    if cur_num <= default_num:
        return current_val, None
    return default_text, "tightened"


def _ensure_in_namelist(text: str, namelist: str, key: str, value: str) -> str:
    """Make sure &<NAMELIST> contains `key = value,`. Replaces existing
    value if present; inserts before `/` if the namelist exists; otherwise
    appends a fresh namelist block after the last existing namelist closure.
    Case-insensitive on namelist name; preserves the user's whitespace style.
    """
    # Match a full `&<NAMELIST> ... /` block. Same close shape as the
    # block-level regex: `\s/` (preceded by whitespace, followed by EOL/EOF)
    # so `/` inside quoted values is not picked up.
    body_pattern = re.compile(
        rf"(&{re.escape(namelist)}\b)(.*?)(\s/[ \t]*(?:\n|\Z))",
        re.IGNORECASE | re.DOTALL,
    )
    m = body_pattern.search(text)
    if m is None:
        block = f"&{namelist.upper()}\n  {key} = {value},\n/\n"
        closures = list(re.finditer(r"(?m)^[ \t]*/[ \t]*$", text))
        if closures:
            ins = closures[-1].end()
            return text[:ins] + "\n" + block + text[ins:]
        return text.rstrip() + "\n" + block

    head, body, close = m.group(1), m.group(2), m.group(3)
    key_re = re.compile(
        rf"(?<![A-Za-z_]){re.escape(key)}\s*=\s*[^,\n!]+",
        re.IGNORECASE,
    )
    if key_re.search(body):
        new_body = key_re.sub(f"{key} = {value}", body)
    else:
        # Append a new entry to the end of body, before the closing slash.
        # 2-space indent matches qe_tools.write_pw_input output style.
        new_body = body.rstrip() + f"\n  {key} = {value},"
    return text[:m.start()] + head + new_body + close + text[m.end():]


def _apply_vc_relax_rule(
    text: str, parsed: dict,
) -> tuple[str, list[tuple[str, str, str]], list[str]]:
    """Rule A: enforce vc-relax convergence thresholds. Returns
    (new_text, changes, reasons). No-op (empty changes) if input is not
    vc-relax or already tight enough.
    """
    if not _is_vc_relax(parsed):
        return text, [], []
    new_text = text
    changes: list[tuple[str, str, str]] = []
    for key in ("press_conv_thr", "forc_conv_thr", "etot_conv_thr"):
        nl = _NAMELIST_FOR[key]
        cur = parsed.get(nl, {}).get(key)
        merged, kind = _merge_value(key, cur)
        if kind is None:
            continue
        old_repr = "(missing)" if kind == "added" else cur
        changes.append((key, old_repr, merged))
        new_text = _ensure_in_namelist(new_text, nl, key, merged)
    reasons = []
    if changes:
        reasons.append("vc-relax requires tight thresholds (v3 hard rule)")
    return new_text, changes, reasons


def _apply_bands_to_nscf_rule(
    text: str, parsed: dict,
) -> tuple[str, list[tuple[str, str, str]], list[str]]:
    """Rule B: rewrite calculation='bands' to 'nscf' when the input is a
    bands-path NSCF (signature: K_POINTS crystal_b/tpiba_b AND
    calculation='bands'). Returns (new_text, changes, reasons).

    Why: bench evaluator parses `highest occupied, lowest unoccupied
    level (ev): ...` from bands_out for VBM/CBM. QE only writes this line
    in calculation='nscf' (not 'bands'). Failure mode evidence in
    docs/t2_phase1_diagnose_2026-05-11.md.
    """
    calc = parsed.get("control", {}).get("calculation", "")
    calc_norm = calc.strip().strip("'\"").lower()
    if calc_norm != "bands":
        return text, [], []
    if not _BANDS_PATH_KPOINTS_RE.search(text):
        # calculation='bands' without K_POINTS crystal_b/tpiba_b is unusual
        # (maybe an automatic-mesh bands run?). Be conservative: don't touch.
        return text, [], []
    # Preserve the original quote style (single or double) via the \2 backref.
    new_text, n_sub = _CALC_BANDS_RE.subn(r"\1\2nscf\2", text, count=1)
    if n_sub == 0:
        # The parser saw calculation=bands but our serialization-aware
        # regex didn't match — refuse to silently break the file.
        return text, [], []
    # Detect actual quote style for the changes-list display.
    m = _CALC_BANDS_RE.search(text)
    q = m.group(2) if m else "'"
    changes: list[tuple[str, str, str]] = [
        ("calculation", f"{q}bands{q}", f"{q}nscf{q}"),
    ]
    reasons = [
        "bands NSCF on k-path must use calculation='nscf'; "
        "QE writes the homo/lumo line that bench evaluator needs only in "
        "'nscf' mode (see docs/t2_phase1_diagnose_2026-05-11.md)",
    ]
    # nbnd advisory: if nbnd is missing, bench evaluator may still miss
    # the lowest-unoccupied line. We can't safely compute nelec here
    # without parsing pseudos, so just warn.
    if "nbnd" not in parsed.get("system", {}):
        reasons.append(
            "hint: also set `nbnd = nelec/2 + 4` in &SYSTEM so QE prints "
            "the 'lowest unoccupied' level alongside highest occupied "
            "(no auto-injection — count nelec from your pseudos)"
        )
    return new_text, changes, reasons


# ---------------------------------------------------------------------------
# Rule C (Phase 4): ecutrho ratio for USPP pseudos
# ---------------------------------------------------------------------------

_PSEUDO_DIR_RE = re.compile(r"pseudo_dir\s*=\s*['\"]?([^'\"\n,]+)", re.I)
_ATOMIC_SPECIES_RE = re.compile(
    r"^\s*ATOMIC_SPECIES\s*\n((?:\s*\S+\s+[\d.eE+-]+\s+\S+\s*\n)+)",
    re.MULTILINE,
)
_USPP_HINT_RE = re.compile(r"USPP|GBRV|PAW|uspp|gbrv|paw", re.I)


def _detect_uspp(parsed: dict, text: str) -> tuple[bool, str]:
    """Best-effort USPP/PAW detection. Returns (is_uspp, reason)."""
    pseudo_dir_m = _PSEUDO_DIR_RE.search(text)
    if pseudo_dir_m and _USPP_HINT_RE.search(pseudo_dir_m.group(1)):
        return True, f"pseudo_dir={pseudo_dir_m.group(1).strip()!r} contains USPP hint"
    spec_m = _ATOMIC_SPECIES_RE.search(text)
    if spec_m and _USPP_HINT_RE.search(spec_m.group(1)):
        return True, "ATOMIC_SPECIES references USPP-named .upf file(s)"
    return False, ""


def _apply_ecutrho_uspp_rule(
    text: str, parsed: dict,
) -> tuple[str, list[tuple[str, str, str]], list[str]]:
    """Rule C: enforce ecutrho >= 10 × ecutwfc when pseudo family is USPP.
    Returns (new_text, changes, reasons)."""
    sys_nl = parsed.get("system", {})
    ecutwfc_raw = sys_nl.get("ecutwfc")
    ecutrho_raw = sys_nl.get("ecutrho")
    if not ecutwfc_raw or not ecutrho_raw:
        return text, [], []
    ecutwfc = _fortran_to_float(ecutwfc_raw)
    ecutrho = _fortran_to_float(ecutrho_raw)
    if ecutwfc is None or ecutrho is None or ecutwfc <= 0:
        return text, [], []

    is_uspp, why = _detect_uspp(parsed, text)
    if not is_uspp:
        return text, [], []

    ratio = ecutrho / ecutwfc
    if ratio >= 9.0:
        return text, [], []

    new_ecutrho = int(round(ecutwfc * 10))
    new_text = _ensure_in_namelist(text, "system", "ecutrho", str(new_ecutrho))
    changes = [("ecutrho", str(ecutrho_raw),
                f"{new_ecutrho}  (= 10 × ecutwfc={int(round(ecutwfc))})")]
    reasons = [
        f"USPP pseudo detected ({why}); GBRV/USPP convention requires "
        f"ecutrho ≈ 10 × ecutwfc. Original ratio {ratio:.2f}× → fixed to 10×. "
        f"Looser ratios drift band gap 0.2–0.5 eV per 30 Ry ecutwfc gap.",
    ]
    return new_text, changes, reasons


# ---------------------------------------------------------------------------
# Rule D (Phase 4): occupations vs config_advisor recommendation
# ---------------------------------------------------------------------------


def _find_config_advice(start: Path, max_up: int = 4) -> dict | None:
    """Walk up from `start` looking for .config_advice.json. Returns the
    parsed dict, or None if not found / unreadable."""
    p = Path(start).resolve()
    for _ in range(max_up + 1):
        candidate = p / ".config_advice.json"
        if candidate.is_file():
            try:
                return json.loads(candidate.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                return None
        if p.parent == p:
            break
        p = p.parent
    return None


def _find_task_prompt(start: Path, max_up: int = 4) -> str | None:
    """Walk up from `start` looking for task_prompt.txt (the bench-rendered
    prompt the solver was launched with). Returns its text, or None if not
    found / unreadable. Same walk-up shape as _find_config_advice."""
    p = Path(start).resolve()
    for _ in range(max_up + 1):
        candidate = p / "task_prompt.txt"
        if candidate.is_file():
            try:
                return candidate.read_text(encoding="utf-8", errors="replace")
            except OSError:
                return None
        if p.parent == p:
            break
        p = p.parent
    return None


def _apply_occupations_advisory_rule(
    text: str, parsed: dict, input_path: Path | None,
) -> tuple[str, list[tuple[str, str, str]], list[str]]:
    """Rule D: warn (no auto-fix) when occupations contradicts the
    config_advisor's recommendation.

    Skipped when:
      - no .config_advice.json found anywhere up the directory tree
      - advice.ok=False (subagent failed)
      - input is calculation='vc-relax' or DOS-NSCF (different occupation
        conventions apply)
    """
    if input_path is None:
        return text, [], []
    advice = _find_config_advice(input_path.parent)
    if advice is None or not advice.get("ok"):
        return text, [], []

    sys_nl = parsed.get("system", {})
    occ_raw = sys_nl.get("occupations", "")
    occ = occ_raw.strip().strip("'\"").lower()
    if not occ:
        # If missing entirely, pw.x defaults to 'fixed' — warn if advice says smearing
        occ = "fixed"

    calc = parsed.get("control", {}).get("calculation", "").strip().strip("'\"").lower()
    # Skip vc-relax (occupations choice less critical for geometry-only)
    # Skip DOS NSCF (bench mandates 'tetrahedra' regardless of metal/insulator)
    if calc == "vc-relax" or occ == "tetrahedra":
        return text, [], []

    expected = advice.get("occupations", "fixed")
    if occ == expected:
        return text, [], []

    # FIX-29 (2026-05-27): PBE-near-metal materials use per-step occupations —
    # smearing on vc-relax + SCF (convergence), but `fixed` on the bands NSCF so
    # QE emits the "highest occupied, lowest unoccupied" line the gap evaluator
    # needs (the advisory block carries this split). advice.occupations is the
    # SCF value ('smearing'); do NOT warn when an input legitimately uses 'fixed'
    # here, or the warning fights the FIX-29 directive and pushes the executor
    # back into the broken smearing-bands path (CoSb3 2026-05-25 → 0.16).
    if advice.get("pbe_near_metal") and occ == "fixed":
        return text, [], []

    # Warning only — no auto-correct (the advice itself can be wrong for
    # edge cases like Mott insulators with metal-element composition).
    is_metal = advice.get("is_metal", False)
    msg = (
        f"WARN: occupations='{occ}' but config_advisor classifies this "
        f"material as {'METAL' if is_metal else 'INSULATOR/SEMICONDUCTOR'} "
        f"(rationale: {advice.get('rationale', 'n/a')}). "
        f"For SCF + bands NSCF, prefer occupations='{expected}'"
    )
    if is_metal:
        s = advice.get("smearing") or "mv"
        d = advice.get("degauss") or 0.02
        msg += f" with smearing='{s}', degauss={d}."
    else:
        msg += " (sharp Fermi → correct gap)."
    msg += (" If you have a strong physics reason to override (e.g., known "
            "Mott insulator with metal-element composition), proceed and "
            "ignore this warning.")
    return text, [], [msg]


# ---------------------------------------------------------------------------
# Rule F (FIX-32): nspin=2 backstop for magnetic materials
# ---------------------------------------------------------------------------


def _rule_f_magnetic_nspin(input_path: Path, text: str) -> tuple[str, list[str]]:
    """Rule F (FIX-32): when config_advisor flags the material magnetic,
    ensure nspin=2 is present in &SYSTEM (auto-inject if missing/=1). Warn
    if starting_magnetization is absent (cannot safely synthesize it)."""
    advice = _find_config_advice(input_path.parent)
    if not advice or not advice.get("is_magnetic"):
        return text, []
    reasons: list[str] = []
    has_nspin2 = re.search(r"nspin\s*=\s*2", text)
    has_nspin1 = re.search(r"nspin\s*=\s*1", text)
    if has_nspin1:
        text = re.sub(r"nspin\s*=\s*1", "nspin = 2", text)
        reasons.append("Rule F: magnetic material — rewrote nspin=1 → nspin=2")
    elif not has_nspin2:
        # inject right after the &SYSTEM line
        text = re.sub(r"(&SYSTEM\s*\n)", r"\1    nspin = 2\n", text, count=1)
        reasons.append("Rule F: magnetic material — injected nspin = 2 into &SYSTEM")
    if not re.search(r"starting_magnetization", text):
        reasons.append("Rule F WARN: magnetic material but no starting_magnetization "
                       "set — use the MAGNETIC GROUND STATE block seeds.")
    # FIX-33 (2026-05-29): magnetic-metal smearing backstop. For a magnetic
    # *metal*, mv/0.02 (~3000 K) washes the d-band exchange splitting and the
    # itinerant moment collapses to 0 (Ni_fcc 7.85→0.00 µB). Pin gaussian/0.01.
    if advice.get("is_metal"):
        if re.search(r"smearing\s*=\s*['\"]?mv['\"]?", text, flags=re.IGNORECASE):
            text = re.sub(r"(smearing\s*=\s*)['\"]?mv['\"]?",
                          r"\1'gauss'", text, flags=re.IGNORECASE)
            reasons.append("Rule F: magnetic metal — rewrote smearing='mv' → 'gauss' "
                           "(mv/0.02 collapses the itinerant moment).")
        m = re.search(r"degauss\s*=\s*([0-9.eEdD+\-]+)", text)
        _dg = _fortran_to_float(m.group(1)) if m else None
        if _dg is not None and _dg > 0.0105:
            text = re.sub(r"(degauss\s*=\s*)[0-9.eEdD+\-]+",
                          r"\g<1>0.01", text, count=1)
            reasons.append("Rule F: magnetic metal — capped degauss at 0.01 Ry "
                           "to preserve the moment.")
    return text, reasons


# ---------------------------------------------------------------------------
# Rule G (FIX-34): magnetic-insulator bands NSCF must use occupations='fixed'
# ---------------------------------------------------------------------------


# ATOMIC_SPECIES block + a digit-suffixed sublattice label (Cr1/Cr2). A
# trailing digit on a species label is the collinear-AFM split signature: it
# marks an antiferromagnet AND is exactly what the bench evaluator's seekpath
# step chokes on (Element("Cr1") → ValueError). FM materials use bare element
# labels (Fe, Cr), so this cleanly separates the two without a material table.
_ATOMIC_SPECIES_BLOCK_RE = re.compile(
    r"^[ \t]*ATOMIC_SPECIES[^\n]*\n((?:[ \t]*[A-Za-z][\w.]*[ \t]+[^\n]*\n?)+)",
    re.MULTILINE | re.IGNORECASE,
)
_SPLIT_LABEL_RE = re.compile(r"^[A-Z][a-z]?\d+$")


def _has_split_labels(text: str) -> bool:
    """True if ATOMIC_SPECIES has a digit-suffixed sublattice label (Cr1/Cr2)
    — the collinear-AFM split signature, which is also the species label that
    crashes the bench evaluator's seekpath Element() call. Digits inside
    `starting_magnetization(1)` are NOT in the ATOMIC_SPECIES block, so they
    don't false-positive."""
    m = _ATOMIC_SPECIES_BLOCK_RE.search(text)
    if not m:
        return False
    for line in m.group(1).splitlines():
        toks = line.split()
        if toks and _SPLIT_LABEL_RE.match(toks[0]):
            return True
    return False


def _rule_g_magnetic_insulator_bands_fixed(input_path: Path, text: str) -> tuple[str, list[str]]:
    """Rule G: on a magnetic material's bands-path NSCF (signature:
    K_POINTS crystal_b/tpiba_b), force occupations='fixed'. Two cases:

    - magnetic INSULATOR (FIX-34): fixed occ makes QE print the
      `highest occupied, lowest unoccupied level` line the band-gap evaluator
      relies on; smearing emits no gap line → band_gap/gap_type/VBM/CBM nulled
      (CoO 2026-05-28 scored 0.16).
    - AFM-split magnetic METAL (FIX-40, S-2): a metal with split sublattice
      labels (Cr1/Cr2 — e.g. Cr_bcc). Smearing → the evaluator takes its
      fermi-mode path, which calls seekpath BEFORE the metal short-circuit;
      seekpath crashes on the split-label species → gap_type/vbm/cbm nulled
      (Cr_bcc 2026-05-28 scored 0.08). fixed occ → homo_lumo mode → the metal
      short-circuit fires FIRST → metal labelled correctly, seekpath never
      reached. FM metals (bare labels, no split) keep smearing — no regression.

    Gated on the crystal_b signature, so it never touches the SCF or the
    DOS-NSCF (both K_POINTS automatic), which legitimately keep smearing."""
    advice = _find_config_advice(input_path.parent)
    if not advice or not advice.get("is_magnetic"):
        return text, []
    is_metal = bool(advice.get("is_metal"))
    # Magnetic metals only qualify when they carry AFM split labels (the
    # seekpath-crash trigger). FM magnetic metals stay on the smearing path.
    if is_metal and not _has_split_labels(text):
        return text, []
    if not _BANDS_PATH_KPOINTS_RE.search(text):
        return text, []
    if not re.search(r"occupations\s*=\s*['\"]?smearing['\"]?", text, flags=re.IGNORECASE):
        return text, []
    text = re.sub(r"(occupations\s*=\s*)['\"]?smearing['\"]?",
                  r"\1'fixed'", text, count=1, flags=re.IGNORECASE)
    kind = "metal (AFM split-label)" if is_metal else "insulator"
    return text, [
        f"Rule G: magnetic {kind} bands NSCF — rewrote occupations='smearing' "
        "→ 'fixed' so the band-gap evaluator labels it correctly (HOMO/LUMO "
        "line for insulators; metal short-circuit before seekpath for AFM "
        "metals). Keep a generous nbnd to cover the conduction-band edge."
    ]


# ---------------------------------------------------------------------------
# Rule H (FIX-#4): vc-relax must START from the bench-given cell
# ---------------------------------------------------------------------------

# The vc-relax STARTING cell IS the bench-given cell (relaxation hasn't run),
# so a true match is near-exact; these tolerances absorb only float-formatting
# / orientation noise, not a real reparameterization.
_CELL_LEN_RTOL = 1e-3      # relative, on a/b/c (Å)
_CELL_ANG_ATOL = 0.05      # absolute, on α/β/γ (deg)
_BOHR_TO_ANG = 0.529177210903

_CELL_PARAM_HDR_RE = re.compile(
    r"CELL_PARAMETERS\s*\(?\s*(angstrom|bohr|alat)?", re.IGNORECASE)
_CELL_PARAM_BLOCK_RE = re.compile(
    r"CELL_PARAMETERS[^\n]*\n((?:[^&\n]+\n){3})", re.IGNORECASE)
# A position row is an element label (Fe / Mn1 / O) followed by ≥3 numeric
# columns. Requiring the 3 numbers stops the block from greedily eating the
# next card (e.g. `K_POINTS automatic`, which looks like "label value").
_POS_NUM = r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eEdD][+-]?\d+)?"
_ATOMIC_POSITIONS_BLOCK_RE = re.compile(
    r"(?m)^[ \t]*ATOMIC_POSITIONS[^\n]*\n"
    r"(?:[ \t]*[A-Za-z][a-z]?\d*[ \t]+"
    + _POS_NUM + r"[ \t]+" + _POS_NUM + r"[ \t]+" + _POS_NUM
    + r"[^\n]*\n?)+")
_CELLDM_RE = re.compile(
    r"[ \t]*celldm\(\d+\)\s*=\s*[^,\n!]+,?[ \t]*\n?", re.IGNORECASE)
_LEGACY_CELL_KEY_RE = re.compile(
    r"(?m)^[ \t]*(?:A|B|C|cosAB|cosAC|cosBC)\s*=\s*[^,\n!]+,?[ \t]*\n",
    re.IGNORECASE)


def _lattice_invariants(vectors: list) -> tuple | None:
    """3 Cartesian vectors → (a,b,c,alpha,beta,gamma) in Å/deg, or None."""
    import math

    def _norm(v):
        return math.sqrt(sum(x * x for x in v))

    def _ang(u, v):
        nu, nv = _norm(u), _norm(v)
        if nu <= 0 or nv <= 0:
            return None
        cos = max(-1.0, min(1.0, sum(u[i] * v[i] for i in range(3)) / (nu * nv)))
        return math.degrees(math.acos(cos))

    a, b, c = (_norm(v) for v in vectors)
    if min(a, b, c) <= 0:
        return None
    alpha = _ang(vectors[1], vectors[2])
    beta = _ang(vectors[0], vectors[2])
    gamma = _ang(vectors[0], vectors[1])
    if None in (alpha, beta, gamma):
        return None
    return (a, b, c, alpha, beta, gamma)


def _input_cell_vectors(text: str) -> list | None:
    """Extract the 3 CELL_PARAMETERS rows (converted to Å) from a pw.x input.
    Returns None when absent/malformed or in `alat` units (needs celldm(1) to
    convert — conservatively unhandled so Rule H never clobbers on ambiguity)."""
    hdr = _CELL_PARAM_HDR_RE.search(text)
    if not hdr:
        return None
    unit = (hdr.group(1) or "angstrom").lower()
    block = _CELL_PARAM_BLOCK_RE.search(text)
    if not block:
        return None
    rows = []
    for line in block.group(1).splitlines():
        parts = line.strip().split()
        if len(parts) < 3:
            return None
        try:
            rows.append([float(x) for x in parts[:3]])
        except ValueError:
            return None
    if len(rows) != 3:
        return None
    if unit == "bohr":
        return [[x * _BOHR_TO_ANG for x in r] for r in rows]
    if unit == "alat":
        return None
    return rows


def _invariants_match(inv_a: tuple, inv_b: tuple) -> bool:
    """Field-by-field (a→a, b→b, …) compare — the bench scores raw a/b/c/angles,
    so a permuted/reset axis order must count as a mismatch (rotation-invariant
    norms+angles already ignore pure rotation)."""
    a1, b1, c1, al1, be1, ga1 = inv_a
    a2, b2, c2, al2, be2, ga2 = inv_b
    for u, v in ((a1, a2), (b1, b2), (c1, c2)):
        if abs(u - v) / max(abs(v), 1e-12) > _CELL_LEN_RTOL:
            return False
    for u, v in ((al1, al2), (be1, be2), (ga1, ga2)):
        if abs(u - v) > _CELL_ANG_ATOL:
            return False
    return True


def _rule_h_vc_relax_cell_preservation(
    input_path: Path, text: str,
) -> tuple[str, list[str]]:
    """Rule H (FIX-#4): a vc-relax must START from the bench-given cell so the
    relaxed output stays in the GT's parameterization. The bench reads
    a/b/c/α/β/γ RAW from the submitted vc-relax.out (no standardization) and
    each GT is the *relaxed given initial_structure*. If the LLM reparameterizes
    — e.g. `ibrav=3, nat=1` collapsing a given 2-atom cell (Fe_bcc 2026-06-02
    scored 0.35: c/angles/point_group=0, magnetization halved 1-atom vs 2-atom)
    — the lattice params + magnetization mis-score deterministically.

    Fires on a vc-relax pw.x input whose STARTING cell deviates from the given
    structure (`ibrav != 0`, `nat != given`, or cell invariants mismatch), and
    rewrites to `ibrav=0` + the given CELL_PARAMETERS (angstrom) + all given
    ATOMIC_POSITIONS + nat/ntyp, preserving every other namelist/card the LLM
    wrote (ecut*, thresholds, smearing, nspin, starting_magnetization, K_POINTS).

    AFM guard: skipped entirely for split-sublattice inputs (Mn1/Mn2) — those
    are already ibrav=0 with the given cell and must never be touched.
    """
    parsed = _parse_input(text)
    if not _is_vc_relax(parsed):
        return text, []
    # AFM split-sublattice inputs are ibrav=0 with the given cell — never touch.
    if _has_split_labels(text):
        return text, []

    prompt = _find_task_prompt(input_path.parent)
    if prompt is None:
        return text, []
    try:
        from ..subagents._magnetism import (  # lazy: keep module import light
            parse_initial_structure,
            _cell_vectors_from_params,
        )
    except Exception:
        return text, []
    given = parse_initial_structure(prompt)
    if given is None or not given.sites:
        return text, []

    sys_nl = parsed.get("system", {})

    def _as_int(raw):
        try:
            return int(str(raw).strip().rstrip(","))
        except (ValueError, AttributeError, TypeError):
            return None

    ibrav = _as_int(sys_nl.get("ibrav"))
    nat = _as_int(sys_nl.get("nat"))
    given_nat = len(given.sites)
    given_inv = (given.a, given.b, given.c, given.alpha, given.beta, given.gamma)

    if ibrav is not None and ibrav != 0:
        should_fire = True
    elif nat is not None and nat != given_nat:
        should_fire = True
    else:
        in_vecs = _input_cell_vectors(text)
        if in_vecs is None:
            # An ibrav=0 vc-relax MUST carry a parseable angstrom/bohr
            # CELL_PARAMETERS; if we can't read it, only fire when ibrav is
            # explicitly 0 (a real broken input), else stay conservative.
            should_fire = (ibrav == 0)
        else:
            in_inv = _lattice_invariants(in_vecs)
            should_fire = (in_inv is None) or not _invariants_match(in_inv, given_inv)

    if not should_fire:
        return text, []

    old_ibrav = "(none)" if ibrav is None else str(ibrav)
    new_text = text

    # 1) &SYSTEM: ibrav→0, strip celldm(*) / legacy A,B,C cell keys, set nat/ntyp.
    new_text = _ensure_in_namelist(new_text, "system", "ibrav", "0")
    new_text = _CELLDM_RE.sub("", new_text)
    new_text = _LEGACY_CELL_KEY_RE.sub("", new_text)
    new_text = _ensure_in_namelist(new_text, "system", "nat", str(given_nat))
    distinct: list[str] = []
    for s in given.sites:
        if s.element not in distinct:
            distinct.append(s.element)
    new_text = _ensure_in_namelist(new_text, "system", "ntyp", str(len(distinct)))

    # 2) CELL_PARAMETERS (given vectors, Å). Replace if present, else insert
    #    before ATOMIC_SPECIES (canonical QE card order).
    vecs = _cell_vectors_from_params(
        given.a, given.b, given.c, given.alpha, given.beta, given.gamma)
    cell_block = "CELL_PARAMETERS (angstrom)\n" + "".join(
        f"  {v[0]:.10f}  {v[1]:.10f}  {v[2]:.10f}\n" for v in vecs)
    if _CELL_PARAM_BLOCK_RE.search(new_text):
        new_text = _CELL_PARAM_BLOCK_RE.sub(lambda _m: cell_block, new_text, count=1)
    else:
        as_m = re.search(r"(?m)^[ \t]*ATOMIC_SPECIES\b", new_text)
        if as_m:
            new_text = new_text[:as_m.start()] + cell_block + new_text[as_m.start():]
        else:
            new_text = new_text.rstrip() + "\n" + cell_block

    # 3) ATOMIC_POSITIONS (crystal) = the given fractional sites (base-element
    #    labels; AFM split inputs were excluded above).
    pos_block = "ATOMIC_POSITIONS (crystal)\n" + "".join(
        f"  {s.element}  {s.x:.10f}  {s.y:.10f}  {s.z:.10f}\n" for s in given.sites)
    if _ATOMIC_POSITIONS_BLOCK_RE.search(new_text):
        new_text = _ATOMIC_POSITIONS_BLOCK_RE.sub(lambda _m: pos_block, new_text, count=1)
    else:
        new_text = new_text.rstrip() + "\n" + pos_block

    reasons = [
        f"Rule H (FIX-#4): vc-relax must start from the bench-given cell so the "
        f"relaxed output stays in the GT's parameterization. Rewrote ibrav="
        f"{old_ibrav}→0, restored the given CELL_PARAMETERS (angstrom) and all "
        f"{given_nat} atom(s) (nat→{given_nat}). The bench reads lattice params "
        f"raw from vc-relax.out and GT == the relaxed given cell, so a reduced / "
        f"reparameterized cell mis-scores c/angles/point_group + magnetization "
        f"(Fe_bcc 2-atom γ≈109.48 case)."
    ]
    return new_text, reasons


# ---------------------------------------------------------------------------
# Rule E (v4.3 Phase 5): K_POINTS crystal_b / tpiba_b data-row validation
# ---------------------------------------------------------------------------

# Matches a single 4-token numeric row (kx, ky, kz, w). Tolerates
# Fortran-style exponents (`1.0d-4`) but the typical case is `1.0e-4` /
# `0.5` / `0`.
_NUM_TOK = r"[-+]?\d+(?:\.\d*)?(?:[eEdD][+-]?\d+)?"
_KPOINT_DATA_ROW_RE = re.compile(
    rf"^\s*({_NUM_TOK})\s+({_NUM_TOK})\s+({_NUM_TOK})\s+({_NUM_TOK})\s*$",
)


def _apply_kpoints_crystal_b_rule(
    text: str, parsed: dict,
) -> tuple[str, list[tuple[str, str, str]], list[str]]:
    """Rule E: validate K_POINTS crystal_b / tpiba_b data rows.

    crystal_b's 4th column is the integer count of points to interpolate
    to the next vertex (NOT a k-point weight). The Rb 2026-05-19 footgun
    was 331 rows with column-4 = 0 everywhere — QE emitted just the
    vertices, leaving a dense-looking but EF-uncovering path. Hard reject
    so pw.x doesn't waste 30+ minutes on a useless bands run.

    Trigger conditions:
      - File has K_POINTS crystal_b / tpiba_b
      - The block after has ≥ 2 four-column data rows (skipping the
        optional integer-only vertex-count header line)
      - Either:
          (a) any 4th-column value is a non-integer float or negative
          (b) row count ≥ 5 AND every 4th column == 0

    Otherwise pass through (no changes, no reasons).
    """
    m = _BANDS_PATH_KPOINTS_RE.search(text)
    if m is None:
        return text, [], []

    rows: list[tuple[str, str, str, str]] = []
    saw_count_header = False
    for line in text[m.end():].splitlines():
        s = line.strip()
        if not s:
            continue
        # Optional N-vertices header: an integer alone on its own line.
        if not saw_count_header and re.fullmatch(r"\d+", s):
            saw_count_header = True
            continue
        # End of block: hit next namelist, K_POINTS, or CELL_PARAMETERS-like card
        if s.startswith("&") or s.startswith("/") or re.match(r"[A-Z_]{3,}", s):
            break
        row_m = _KPOINT_DATA_ROW_RE.match(line)
        if row_m is None:
            break
        rows.append(row_m.groups())

    if len(rows) < 2:
        return text, [], []

    weights = [w for _, _, _, w in rows]
    weights_num: list[float | None] = [_fortran_to_float(w) for w in weights]

    # Sub-rule 1: each 4th column must be a non-negative integer.
    for raw, num in zip(weights, weights_num):
        if num is None:
            continue
        if num < 0:
            raise LintReject(
                f"K_POINTS crystal_b/tpiba_b: data row 4th column is negative "
                f"({raw}). That column is the integer number of interpolation "
                f"segments to the NEXT vertex; it must be >= 0. If you want "
                f"weighted explicit k-points instead, use `K_POINTS crystal` "
                f"(or `tpiba`) with weight in column 4."
            )
        if abs(num - round(num)) > 1e-6:
            raise LintReject(
                f"K_POINTS crystal_b/tpiba_b: data row 4th column is a "
                f"non-integer float ({raw}). That column is the integer "
                f"number of interpolation segments to the next vertex. If "
                f"you intended weights, switch to `K_POINTS crystal` "
                f"(or `tpiba`); weights typically `1.0`."
            )

    # Sub-rule 2: many rows + every 4th column == 0  →  near-certain bug.
    if len(rows) >= 5 and all(
        (n is not None and abs(n) < 1e-9) for n in weights_num
    ):
        raise LintReject(
            f"K_POINTS crystal_b/tpiba_b has {len(rows)} data rows with the "
            f"4th column = 0 on every row. In crystal_b mode the 4th column "
            f"is the number of points to interpolate to the next vertex "
            f"(NOT a k-point weight); all zeros means QE will not generate "
            f"any extra k-points between your listed vertices. For a metal "
            f"task this typically leaves EF crossings uncovered and bench's "
            f"fermi-slice extractor falls back to a bogus gap (root cause "
            f"of the 2026-05-19 Rb run failure).\n"
            f"\n"
            f"Fix — pick ONE form:\n"
            f"  (a) Pre-interpolated explicit path: change the tag to "
            f"`K_POINTS crystal` (or `tpiba`) and put weight=1.0 in column 4 "
            f"of EVERY row.\n"
            f"  (b) Vertex-only with QE interpolation: list only the "
            f"high-symmetry vertices (typically 4–8 points, e.g. Γ-X-W-K-Γ-L) "
            f"and put the per-segment point count (e.g. 30) in column 4 of "
            f"every row EXCEPT the last, which is 0."
        )

    return text, [], []


def lint_pw_input(path: Path | str) -> LintResult:
    """Read the pw.x input at `path`; apply all applicable lint rules in
    sequence (vc-relax thresholds, bands→nscf rewrite, ecutrho USPP,
    occupations advisory). Modifies the file in place when any rule
    patches. Idempotent.
    """
    path = Path(path)
    if not path.is_file():
        return LintResult(file_path=path, skipped_reason=f"file not found: {path}")
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        return LintResult(file_path=path, skipped_reason=f"read failed: {e}")

    parsed = _parse_input(text)
    cubic = _detect_cubic(parsed, text)

    all_changes: list[tuple[str, str, str]] = []
    all_reasons: list[str] = []
    new_text = text

    # Rule E runs FIRST so we don't bother auto-patching a file that's
    # about to be rejected wholesale. If it raises LintReject, the caller
    # short-circuits pw.x and surfaces the reason to the LLM.
    try:
        _apply_kpoints_crystal_b_rule(new_text, parsed)
    except LintReject as e:
        return LintResult(
            file_path=path,
            patched=False,
            rejected=True,
            reject_reason=str(e.reason),
            cubic_flag=cubic,
        )

    # Each rule operates on the current (possibly already-patched) text.
    # Re-parse only if a rule actually rewrote namelists; for the rules
    # below the parsed dict from before the patches is still authoritative
    # (vc-relax patches threshold values inside namelists; bands→nscf
    # patches the calculation value — neither invalidates the other's
    # decision criteria).
    for rule_fn in (_apply_vc_relax_rule, _apply_bands_to_nscf_rule,
                    _apply_ecutrho_uspp_rule):
        new_text, changes, reasons = rule_fn(new_text, parsed)
        all_changes.extend(changes)
        all_reasons.extend(reasons)

    # Re-parse after possible ecutrho rewrite so Rule D sees fresh state.
    parsed_after = _parse_input(new_text)
    _occ_text, _occ_changes, occ_reasons = _apply_occupations_advisory_rule(
        new_text, parsed_after, path,
    )
    all_reasons.extend(occ_reasons)

    # Rule F (FIX-32): magnetic nspin=2 backstop. Runs after Rule C so it
    # sees the (possibly ecutrho-fixed) text; mutates text in place when
    # nspin is missing or =1 on a magnetic material.
    new_text, f_reasons = _rule_f_magnetic_nspin(path, new_text)
    all_reasons.extend(f_reasons)

    # Rule G (FIX-34): magnetic-insulator bands NSCF → occupations='fixed'.
    new_text, g_reasons = _rule_g_magnetic_insulator_bands_fixed(path, new_text)
    all_reasons.extend(g_reasons)

    # Rule H (FIX-#4): vc-relax cell preservation. Runs LAST so it operates on
    # the fully threshold/nspin-patched text and byte-preserves those edits;
    # rewrites a reduced/reparameterized starting cell back to the given cell.
    new_text, h_reasons = _rule_h_vc_relax_cell_preservation(path, new_text)
    all_reasons.extend(h_reasons)

    fg_changed = new_text != text and bool(
        (f_reasons and any("WARN" not in r for r in f_reasons)) or g_reasons or h_reasons
    )

    if (not all_changes and not occ_reasons and not fg_changed
            and not f_reasons and not g_reasons and not h_reasons):
        # Skipped-reason heuristic — informational, used by tests + log:
        #   * vc-relax already-tight        → "vc-relax input already meets v3 thresholds"
        #   * bands input already calculation='nscf' → "bands-path already in nscf mode"
        #   * everything else                → "not a vc-relax input" (preserves the
        #     pre-v4 message that existing test_qe_input_lint cases assert on)
        if _is_vc_relax(parsed):
            reason = "vc-relax input already meets v3 thresholds"
        elif _BANDS_PATH_KPOINTS_RE.search(text):
            reason = "bands-path input already uses calculation='nscf'"
        else:
            reason = "not a vc-relax input"
        return LintResult(
            file_path=path,
            patched=False,
            cubic_flag=cubic,
            skipped_reason=reason,
        )

    # Only rewrite the file if there are actual content changes; warn-only
    # rules (Rule D occupations) emit reasons without changing bytes.
    if all_changes or fg_changed:
        path.write_text(new_text, encoding="utf-8")

    header = ("auto-injected by qe_input_lint" if (all_changes or fg_changed)
              else "qe_input_lint advisory (warn-only)")
    diff_lines = [
        f"──── {header} ────",
        f"file: {path.name}",
    ]
    for key, old, new in all_changes:
        if old == "(missing)":
            diff_lines.append(f"+ {key} = {new}   (key was missing)")
        else:
            diff_lines.append(f"- {key} = {old}   →   {key} = {new}")
    for reason in all_reasons:
        diff_lines.append(f"reason: {reason}")
    diff_lines.append("────")

    return LintResult(
        file_path=path,
        patched=bool(all_changes or fg_changed),
        changes=all_changes,
        reasons=all_reasons,
        diff_text="\n".join(diff_lines),
        cubic_flag=cubic,
    )


def find_pw_input(cmd: str, cwd: Path | str) -> Path | None:
    """Resolve the pw.x input file path from a bash command, or None if the
    command is not a pw.x invocation. `cwd` resolves relative paths.
    """
    if not _PW_X_RE.search(cmd):
        return None
    cwd = Path(cwd)
    # Prefer explicit -in / -inp / -input
    for m in _FLAG_INPUT_RE.finditer(cmd):
        candidate = m.group(1)
        # Defensive: skip pure integers (they can't be a path)
        try:
            int(candidate)
            continue
        except ValueError:
            pass
        p = Path(candidate)
        if not p.is_absolute():
            p = cwd / p
        return p.resolve()
    # Fallback: stdin redirect
    m = _STDIN_INPUT_RE.search(cmd)
    if m:
        p = Path(m.group(1))
        if not p.is_absolute():
            p = cwd / p
        return p.resolve()
    return None


def intercept_bash(cmd: str, cwd: Path | str, log_path: Path | None = None) -> LintResult:
    """Public hook for BashTool.run(). Returns a LintResult; if patched,
    the caller should propagate diff_text into the tool result so the LLM
    sees what was injected.
    """
    inp = find_pw_input(cmd, cwd)
    if inp is None:
        return LintResult(skipped_reason="no pw.x invocation detected")
    result = lint_pw_input(inp)
    if log_path is not None:
        _append_log(Path(log_path), cmd, result)
    return result


def _append_log(log_path: Path, cmd: str, result: LintResult) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    line = (
        f"[{ts}] file={result.file_path or '?'} "
        f"patched={result.patched} rejected={result.rejected} "
        f"cubic={result.cubic_flag} skipped={result.skipped_reason!r}\n"
    )
    for k, o, n in result.changes:
        line += f"  - {k}: {o} → {n}\n"
    if result.rejected:
        line += f"  REJECT: {result.reject_reason!r}\n"
    line += f"  $ {cmd}\n"
    with log_path.open("a", encoding="utf-8") as f:
        f.write(line)
