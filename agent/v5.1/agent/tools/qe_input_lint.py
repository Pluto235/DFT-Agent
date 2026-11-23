"""QE input linter — enforce vc-relax convergence thresholds before pw.x runs.

Hooked into BashTool.run() before subprocess spawn. When a pw.x invocation
is detected, the linter parses the input file and, if it's a vc-relax that
doesn't already have at-least-as-tight convergence thresholds as v3 defaults,
injects them in place.

This exists because LLMs (haiku especially) silently drop the convergence
thresholds the system prompt recommends — measured ~25 % miss rate across
v1+v2 runs, deterministic 0.25-point loss when it strikes (cubic gets
demoted to trigonal by pymatgen's SpacegroupAnalyzer when the relaxed cell
drifts off symmetry by ≥0.01° / 0.05 %). See docs/cpu_vs_gpu_2026-05-10.md.
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from agent.subagents._magnetism import (
    MAGNETIC_ELEMENTS, MAGNETIC_SEED, base_element,
)


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

# Electronic-accuracy hard guards (anti-cheat). Unlike the geometry thresholds
# above, these run for ALL calc types and only clamp a value that is PRESENT
# and too loose — they are NEVER inserted when absent, because QE's own defaults
# are already safe (conv_thr defaults to 1e-6, and write_pw_input only emits
# degauss when occupations=smearing). They exist to stop an LLM from forcing SCF
# convergence / "JOB DONE." by loosening conv_thr or over-smearing with degauss,
# which silently degrades forces / energies / the space group (a bench-score loss).
_CONV_THR_MAX: float = 1.0e-6        # &ELECTRONS electronic SCF threshold
_DEGAUSS_MAX: float = 0.03           # &SYSTEM smearing width (Ry); only when smearing
_CONV_THR_CLAMP_TEXT: str = "1.0d-6"
_DEGAUSS_CLAMP_TEXT: str = "0.03"

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


@dataclass
class LintResult:
    patched: bool = False
    file_path: Path | None = None
    changes: list[tuple[str, str, str]] = field(default_factory=list)
    diff_text: str = ""
    cubic_flag: bool | None = None
    skipped_reason: str = ""
    # Advisory messages that don't mutate the file (e.g. k-mesh suggestions for
    # hex/trig cells). Propagated to the LLM alongside `diff_text` even when
    # patched=False.
    advisory_text: str = ""


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


def _detect_hex_trig(parsed: dict, full_text: str) -> bool | None:
    """Best-effort hexagonal/trigonal detection.

    Triggers True for:
      - ibrav ∈ {4} (hex) or {5} (trig rhombohedral)
      - ibrav == 0 with CELL_PARAMETERS where one inter-vector angle is
        ≈ 120° (hex) or ≈ 60° (trig with rhombohedral primitive)

    Returns True / False / None (None = cannot determine).
    """
    ibrav_raw = parsed.get("system", {}).get("ibrav")
    if ibrav_raw is not None:
        try:
            ibrav = int(ibrav_raw.strip().rstrip(","))
        except (ValueError, AttributeError):
            ibrav = None
        if ibrav in (4, 5):
            return True
        if ibrav is not None and ibrav != 0:
            return False
    cp_match = re.search(
        r"CELL_PARAMETERS[^\n]*\n((?:[^&\n]+\n){3})",
        full_text,
    )
    if not cp_match:
        return None
    import math
    rows: list[list[float]] = []
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

    def _ang(u, v):
        nu = math.sqrt(sum(x * x for x in u))
        nv = math.sqrt(sum(x * x for x in v))
        if nu == 0 or nv == 0:
            return None
        cos = max(-1.0, min(1.0, sum(u[i] * v[i] for i in range(3)) / (nu * nv)))
        return math.degrees(math.acos(cos))

    angles = [_ang(rows[i], rows[j]) for i, j in ((0, 1), (1, 2), (0, 2))]
    if any(a is None for a in angles):
        return None
    # Hex: exactly one pair at 120° (a-b plane), others 90°.
    # Trig (rhombohedral primitive): all three angles equal ≈ 60° or ≈ 120°.
    if any(abs(a - 120.0) < 2.0 for a in angles):
        return True
    if all(abs(a - 60.0) < 2.0 for a in angles):
        return True
    return False


def _parse_kpoints_automatic(text: str) -> tuple[int, int, int] | None:
    """Parse the first non-comment line under `K_POINTS automatic` as 3 ints.
    Returns None if absent or malformed (gamma-only, shift-only, etc).
    """
    m = re.search(
        r"K_POINTS\s+automatic\s*\n((?:\s*!.*\n)*)([^\n]+)",
        text, flags=re.IGNORECASE,
    )
    if not m:
        return None
    parts = m.group(2).split()
    if len(parts) < 3:
        return None
    try:
        nx, ny, nz = int(parts[0]), int(parts[1]), int(parts[2])
    except ValueError:
        return None
    return nx, ny, nz


_SPECIES_ROW_RE = re.compile(r"^\s*([A-Z][a-z]?\d*)\s+[-+\d.eEdD]+\s+\S+\s*$")


def _parse_atomic_species(text: str) -> list[str]:
    """Ordered species labels (first token of each ATOMIC_SPECIES row). The
    1-based position is the QE species index used by starting_magnetization(i).
    Returns [] if the card is absent."""
    out: list[str] = []
    in_block = False
    for line in text.split("\n"):
        if not in_block:
            if "ATOMIC_SPECIES" in line.upper():
                in_block = True
            continue
        if line.strip() == "":
            continue
        m = _SPECIES_ROW_RE.match(line)
        if m:
            out.append(m.group(1))
        else:
            break  # first non-species line ends the card
    return out


def _check_hex_trig_kmesh(parsed: dict, full_text: str) -> str | None:
    """Return an advisory string when this cell is hex/trig but the K_POINTS
    automatic mesh has N_x or N_y not divisible by 3 (would skip K/H corners
    where VBM/CBM commonly live). Returns None when not applicable or OK.
    """
    if not _detect_hex_trig(parsed, full_text):
        return None
    mesh = _parse_kpoints_automatic(full_text)
    if mesh is None:
        return None
    nx, ny, nz = mesh
    if nx % 3 == 0 and ny % 3 == 0:
        return None
    import math
    suggested_x = max(3, int(math.ceil(nx / 3.0)) * 3)
    suggested_y = max(3, int(math.ceil(ny / 3.0)) * 3)
    return (
        "──── qe_input_lint advisory ────\n"
        "Hexagonal/trigonal cell detected (ibrav=4/5 or 120°/60° angles),\n"
        f"but K_POINTS automatic = [{nx}, {ny}, {nz}] has N_x or N_y not\n"
        "divisible by 3. This skips K=(1/3,1/3,0) and H=(1/3,1/3,1/2)\n"
        "corner points where VBM/CBM commonly live. Result: SCF Fermi will\n"
        "be biased low and the evaluator may report VBM/CBM as OFF_PATH.\n"
        f"Suggest: [{suggested_x}, {suggested_y}, {nz}] (or denser, all ÷3).\n"
        "────"
    )


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


def _clamp_present(current_val: str | None, max_num: float,
                   clamp_text: str) -> tuple[str, str | None]:
    """Clamp a PRESENT value down to <= max_num.

    Unlike `_merge_value`, this NEVER inserts when the key is absent — QE's own
    defaults are already safe, and we only want to catch an LLM loosening an
    explicitly-set value.

    Returns (new_value_text, change_kind):
      change_kind = None       → absent, or present and already tight enough
      change_kind = "clamped"  → present and too loose (or unparseable); replace
    """
    if current_val is None:
        return "", None
    cur_num = _fortran_to_float(current_val)
    if cur_num is None:
        # A value we can't parse is not trustworthy → clamp defensively.
        return clamp_text, "clamped"
    if cur_num <= max_num:
        return current_val, None
    return clamp_text, "clamped"


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


def _format_guard_diff(path: Path, changes: list[tuple[str, str, str]]) -> str:
    """Render the anti-cheat guard clamps as a block the LLM will see — same
    style as the auto-injected geometry block, but labeled `guard` so the model
    distinguishes accuracy-floor enforcement from vc-relax tightening.
    """
    lines = [
        "──── qe_input_lint guard ────",
        f"file: {path.name}",
    ]
    for key, old, new in changes:
        lines.append(f"- {key} = {old}   →   {key} = {new}")
    if any(k in ("conv_thr", "degauss") for k, _, _ in changes):
        lines.append("reason: electronic-accuracy hard guard — SCF convergence must "
                     "not be forced by loosening conv_thr or raising degauss.")
    if any(k == "occupations" for k, _, _ in changes):
        lines.append("reason: GT-matching guard — a scored T2 NSCF uses mv-smearing "
                     "(metal/magnetic) or fixed (insulator), never tetrahedra; "
                     "tetrahedra shifts a metal's Fermi by up to ~2 eV → dos_e_fermi "
                     "scores 0.")
    if any(k.startswith("starting_magnetization") for k, _, _ in changes):
        lines.append("reason: magnetic guard — nspin=2 with all starting_magnetization=0 "
                     "collapses to the non-magnetic state (zero moment → wrong Fermi "
                     "level and DOS). Seeded the magnetic species with a generic "
                     "high-spin guess; keep nspin=2 and these seeds on every step.")
    lines.append("────")
    return "\n".join(lines)


def read_is_metal(workdir: Path | str | None) -> bool | None:
    """is_metal from `workdir/.config_advice.json` (written by the config_advisor
    preflight). None when workdir is None or the advice is missing/unreadable/
    lacks the key — callers must treat None as 'unknown' and fall back safely.
    A leaf reader (no config_advisor import) so the lint/bash modules stay acyclic.
    """
    if workdir is None:
        return None
    try:
        advice = json.loads((Path(workdir) / ".config_advice.json").read_text())
    except (OSError, ValueError):
        return None
    val = advice.get("is_metal")
    return bool(val) if val is not None else None


def lint_pw_input(path: Path | str, *, workdir: Path | str | None = None) -> LintResult:
    """Read the pw.x input at `path`; if it's a vc-relax, ensure the three
    v3 convergence thresholds are at least as tight as the defaults.
    Modifies the file in place when needed. Idempotent.

    `workdir` (the run root holding .config_advice.json) drives the A2
    is_metal-aware tetrahedra guard; None → metal-safe default (back-compat).
    """
    path = Path(path)
    if not path.is_file():
        return LintResult(file_path=path, skipped_reason=f"file not found: {path}")
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        return LintResult(file_path=path, skipped_reason=f"read failed: {e}")

    parsed = _parse_input(text)
    hex_trig_warning = _check_hex_trig_kmesh(parsed, text) or ""

    # ── Electronic-accuracy hard guards (all calc types) ──────────────────
    # Clamp an LLM-loosened conv_thr / over-smeared degauss before pw.x runs.
    # Runs BEFORE the vc-relax gate so scf/nscf/relax/bands are covered too.
    # Only clamps PRESENT values; never creates a namelist.
    guard_changes: list[tuple[str, str, str]] = []

    conv_cur = parsed.get("electrons", {}).get("conv_thr")
    conv_new, conv_kind = _clamp_present(conv_cur, _CONV_THR_MAX, _CONV_THR_CLAMP_TEXT)
    if conv_kind == "clamped":
        guard_changes.append(("conv_thr", conv_cur, conv_new))
        text = _ensure_in_namelist(text, "electrons", "conv_thr", conv_new)

    occ_norm = parsed.get("system", {}).get("occupations", "").strip().strip("'\"").lower()
    if occ_norm == "smearing":
        deg_cur = parsed.get("system", {}).get("degauss")
        deg_new, deg_kind = _clamp_present(deg_cur, _DEGAUSS_MAX, _DEGAUSS_CLAMP_TEXT)
        if deg_kind == "clamped":
            guard_changes.append(("degauss", deg_cur, deg_new))
            text = _ensure_in_namelist(text, "system", "degauss", deg_new)

    # Tetrahedra → mv-smearing guard (M3 T2 dos_e_fermi fix). The bench GT
    # generates every *scored* T2 NSCF with occupations='smearing' (smearing='mv',
    # degauss=0.02) for metals/magnetics and 'fixed' for insulators — it NEVER
    # uses tetrahedra. On a metal the tetrahedron Fermi differs from the mv-smeared
    # GT Fermi by up to ~2 eV → dos_e_fermi scores 0 (observed: Cr_bcc −2.13 eV,
    # Co_hcp +1.15 eV). Rewrite tetrahedra → mv-smearing, but ONLY on a uniform
    # mesh (K_POINTS automatic): that is the SCF / DOS-NSCF. A bands NSCF uses
    # K_POINTS crystal_b and must keep occupations='fixed' for the HOMO/LUMO line
    # (the MnO band_gap fix) — _parse_kpoints_automatic returns None there, so the
    # rewrite never touches it.
    if occ_norm.startswith("tetrahedra") and _parse_kpoints_automatic(text) is not None:
        old_occ = parsed.get("system", {}).get("occupations")
        # A2: route by is_metal. GT computes a scored NSCF with mv-smearing for
        # metals/magnetics but occupations='fixed' for insulators — never
        # tetrahedra. Unknown (no workdir / no advice) keeps the metal-safe
        # smearing default (back-compat with the original metal-only guard).
        if read_is_metal(workdir) is False:
            text = _ensure_in_namelist(text, "system", "occupations", "'fixed'")
            guard_changes.append(
                ("occupations", old_occ,
                 "'fixed' (insulator — GT uses fixed, not tetrahedra)")
            )
        else:
            text = _ensure_in_namelist(text, "system", "occupations", "'smearing'")
            text = _ensure_in_namelist(text, "system", "smearing", "'mv'")
            text = _ensure_in_namelist(text, "system", "degauss", "0.02")
            guard_changes.append(
                ("occupations", old_occ, "'smearing' (smearing='mv', degauss=0.02)")
            )

    # Magnetic-moment guard: a spin-polarized run (nspin=2) with EVERY
    # starting_magnetization zero/absent collapses to the non-magnetic solution
    # (observed: Fe_bcc/CrO2/Ni_fcc → total_mag 0.00 → wrong E_F, wrong DOS).
    # The probe/config_advisor already know the seed but deliver it only as
    # prose, which the LLM drops. Seed it deterministically from the GENERIC
    # element table (MAGNETIC_SEED, NOT GT values): a single magnetic label →
    # FM (+seed); split AFM labels (Fe1/Fe2) → alternating signs (the probe's
    # E1/E2 convention). Only fires when ALL seeds are zero/absent, so an
    # LLM/probe-set moment is always respected.
    nspin_raw = parsed.get("system", {}).get("nspin")
    try:
        nspin = int(str(nspin_raw).strip().rstrip(",")) if nspin_raw is not None else 1
    except (ValueError, AttributeError):
        nspin = 1
    if nspin == 2:
        sysd = parsed.get("system", {})
        sm_present = {k: v for k, v in sysd.items()
                      if k.startswith("starting_magnetization")}
        any_nonzero = any(abs(_fortran_to_float(v) or 0.0) > 1e-9
                          for v in sm_present.values())
        if not any_nonzero:
            for i, label in enumerate(_parse_atomic_species(text), 1):
                el = base_element(label)
                if el not in MAGNETIC_ELEMENTS:
                    continue
                seed = MAGNETIC_SEED.get(el, 0.5)
                if label.endswith("2"):
                    seed = -seed  # AFM down-sublattice
                key = f"starting_magnetization({i})"
                old = sm_present.get(key)
                text = _ensure_in_namelist(text, "system", key, str(seed))
                guard_changes.append(
                    (key, old if old is not None else "(absent)", str(seed)))

    guard_diff = ""
    if guard_changes:
        path.write_text(text, encoding="utf-8")
        guard_diff = _format_guard_diff(path, guard_changes)
        parsed = _parse_input(text)  # refresh so the vc-relax block sees post-guard text

    if not _is_vc_relax(parsed):
        return LintResult(
            file_path=path,
            patched=bool(guard_changes),
            changes=list(guard_changes),
            diff_text=guard_diff,
            cubic_flag=_detect_cubic(parsed, text),
            skipped_reason="" if guard_changes else "not a vc-relax input",
            advisory_text=hex_trig_warning,
        )

    cubic = _detect_cubic(parsed, text)
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

    if not changes:
        # No geometry tightening needed — but electronic-accuracy guards may
        # still have fired above (their write already happened).
        return LintResult(file_path=path, patched=bool(guard_changes),
                          changes=list(guard_changes), diff_text=guard_diff,
                          cubic_flag=cubic, advisory_text=hex_trig_warning)

    path.write_text(new_text, encoding="utf-8")

    diff_lines = [
        "──── auto-injected by qe_input_lint ────",
        f"file: {path.name}",
    ]
    for key, old, new in changes:
        if old == "(missing)":
            diff_lines.append(f"+ {key} = {new}   (key was missing)")
        else:
            diff_lines.append(f"- {key} = {old}   →   {key} = {new}")
    diff_lines.append("reason: vc-relax requires tight thresholds (v3 hard rule)")
    diff_lines.append("────")

    # Prepend any guard diff so both anti-cheat clamps and geometry tightening
    # are surfaced to the LLM in one block.
    combined_diff = "\n".join(p for p in (guard_diff, "\n".join(diff_lines)) if p)
    return LintResult(
        file_path=path,
        patched=True,
        changes=guard_changes + changes,
        diff_text=combined_diff,
        cubic_flag=cubic,
        advisory_text=hex_trig_warning,
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


def intercept_bash(cmd: str, cwd: Path | str, log_path: Path | None = None,
                   *, workdir: Path | str | None = None) -> LintResult:
    """Public hook for BashTool.run(). Returns a LintResult; if patched,
    the caller should propagate diff_text into the tool result so the LLM
    sees what was injected. `workdir` (the run root holding .config_advice.json,
    distinct from `cwd` when the LLM cd'd into a step dir) drives the A2
    is_metal-aware occupations guard.
    """
    inp = find_pw_input(cmd, cwd)
    if inp is None:
        return LintResult(skipped_reason="no pw.x invocation detected")
    result = lint_pw_input(inp, workdir=workdir)
    if log_path is not None:
        _append_log(Path(log_path), cmd, result)
    return result


def _append_log(log_path: Path, cmd: str, result: LintResult) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    line = (
        f"[{ts}] file={result.file_path or '?'} "
        f"patched={result.patched} cubic={result.cubic_flag} "
        f"skipped={result.skipped_reason!r}\n"
    )
    for k, o, n in result.changes:
        line += f"  - {k}: {o} → {n}\n"
    line += f"  $ {cmd}\n"
    with log_path.open("a", encoding="utf-8") as f:
        f.write(line)
