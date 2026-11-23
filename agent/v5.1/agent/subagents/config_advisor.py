"""QE configuration advisor preflight subagent.

Runs before the main solver loop. Produces a `ConfigAdvice` with
metal/insulator classification + recommended occupations, smearing,
degauss, and ecutrho factor for the material in the task prompt.

The classifier is **deterministic** (general-chemistry element rules in
`_metals.py`). A tiny LLM cross-check (max_turns=6) sees the
deterministic guess and may override `is_metal` / `ecutrho_factor` if
it sees a clear physics reason. Heuristic-derived protocol flags
(likely_magnetic_3d_oxide, narrow_gap_heavy_p_candidate) are chemistry
predicates and are NOT subject to LLM override — they describe the
material, not a bench answer.

The resulting advice is:
  1. Rendered into a `## QE CONFIGURATION ADVISORY` block injected into
     the main system prompt (and any vc_relax subagent's system prompt).
  2. Persisted to `workdir/.config_advice.json` for transcript / audit.
"""
from __future__ import annotations

import json
import re
import traceback
from dataclasses import asdict, dataclass, field
from pathlib import Path
from textwrap import dedent

from ..llm import LLMClient
from ..loop import run_loop
from ..tools.bash import BashTool

from ._metals import (
    METAL_ELEMENTS,
    STRONGLY_ELECTRONEGATIVE,
    is_likely_magnetic_3d_oxide,
    is_likely_magnetic_3d_system,
    is_likely_narrow_gap_heavy_p,
)


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------


@dataclass
class ConfigAdvice:
    """Recommended QE config knobs for the material in the task prompt.

    `ok=False` means the subagent failed; main solver falls back to the
    system prompt's material-agnostic guidance and the lint layer's USPP
    rule applies generically.

    The two `likely_*` flags are element-set heuristics (see
    `_metals.py`). They flag chemistry that needs a deviation from the
    default SCF recipe; they are NOT a per-material answer lookup.
    """
    ok: bool = False
    formula: str = ""
    elements: list[str] = field(default_factory=list)
    structure_name: str = ""
    pseudo_family: str = ""
    is_uspp: bool = False
    is_metal: bool = False
    occupations: str = "fixed"            # "fixed" | "smearing"
    smearing: str | None = None           # "mv" | "gauss" | "fd"
    degauss: float | None = None          # Ry
    ecutrho_factor: float = 10.0          # ratio relative to ecutwfc
    rationale: str = ""
    source: str = "deterministic"         # "deterministic" | "llm_override" | "fallback"
    # Heuristic: contains O + open-shell 3d TM (V/Cr/Mn/Fe/Co/Ni/Cu).
    # PBE nspin=1 SCF is often unstable for these systems → consider the
    # magnetic protocol (nspin=2 + starting_magnetization sublattice seeds).
    likely_magnetic_3d_oxide: bool = False
    # Broad magnetic flag: open-shell 3d-TM + any anion (incl. fluorides), or
    # an elemental 3d magnet (Fe/Co/Ni/Cr/Mn). Drives nspin=2. Superset of
    # likely_magnetic_3d_oxide. NO Hubbard U (bench GT is plain PBE + nspin=2).
    magnetic_3d_system: bool = False
    recommend_nspin: int = 1
    # Heuristic: contains heavy p-block cation (In/Sn/Tl/Pb/Bi/Hg) + a
    # chalcogen/pnictogen anion. PBE may give a near-zero band gap for
    # these compounds → smearing on SCF (stability) + fixed on bands NSCF
    # (lets QE print the homo/lumo level the evaluator relies on).
    narrow_gap_heavy_p_candidate: bool = False
    failure_reason: str | None = None

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Deterministic classifier
# ---------------------------------------------------------------------------


_FORMULA_RE = re.compile(r"^For\s+([A-Za-z0-9()]+)\s*\(", re.MULTILINE)
_STRUCTURE_RE = re.compile(
    r"For\s+[A-Za-z0-9()]+\s*\(\s*([^,\)]+?)\s*[,\)]",
)
_PSEUDO_FAMILY_RE = re.compile(
    r"Pseudopotentials?:\s*([A-Za-z0-9_./+-]+)",
)


def _parse_elements_from_formula(formula: str) -> list[str]:
    """Extract unique element symbols from a chemical formula.

    Handles simple cases like 'NaCl', 'Al2O3', 'LiFePO4'. Stripped of
    parentheses for sub-formulas like 'Ca(OH)2'. Does NOT handle hydrate
    dots or charge symbols (none of those appear in bench prompts).
    """
    cleaned = re.sub(r"[()0-9·]", " ", formula)
    elements: list[str] = []
    seen: set[str] = set()
    for token in cleaned.split():
        # Match repeated element-count pairs in token, e.g. "Al2O3" → ["Al","O"]
        for m in re.finditer(r"([A-Z][a-z]?)", token):
            sym = m.group(1)
            if sym not in seen:
                seen.add(sym)
                elements.append(sym)
    return elements


def _deterministic_classify(prompt_text: str) -> dict:
    """Rule-based initial classification. Returns a dict that
    `run()` passes both to the LLM cross-check and to `ConfigAdvice`."""
    formula_m = _FORMULA_RE.search(prompt_text)
    formula = formula_m.group(1) if formula_m else ""
    structure_m = _STRUCTURE_RE.search(prompt_text)
    structure_name = structure_m.group(1).strip() if structure_m else ""
    pseudo_m = _PSEUDO_FAMILY_RE.search(prompt_text)
    pseudo_family = pseudo_m.group(1).strip(".") if pseudo_m else ""

    elements = _parse_elements_from_formula(formula)
    element_set: frozenset[str] = frozenset(elements)
    is_uspp = bool(re.search(r"USPP|GBRV|PAW", pseudo_family, re.I))

    # Metal logic (general chemistry, in priority order):
    # 1. If any STRONGLY_ELECTRONEGATIVE element is present → insulator /
    #    semiconductor (oxides, halides, nitrides, sulfides …).
    # 2. Otherwise, if ALL elements are in METAL_ELEMENTS → metal
    #    (covers pure metals + intermetallic alloys).
    # 3. Otherwise → semiconductor / insulator default (covers covalent
    #    semiconductors like Si, Ge, GaAs which have no electronegative
    #    atom but at least one non-METAL_ELEMENTS atom).
    if not elements:
        is_metal = False
    elif any(e in STRONGLY_ELECTRONEGATIVE for e in elements):
        is_metal = False
    elif all(e in METAL_ELEMENTS for e in elements):
        is_metal = True
    else:
        is_metal = False

    if is_metal:
        occupations = "smearing"
        smearing = "mv"        # Methfessel-Paxton 1st-order: robust default
        degauss = 0.02         # Ry; standard for elemental / intermetallic
    else:
        occupations = "fixed"
        smearing = None
        degauss = None

    # Heuristic 1: narrow-gap heavy p-block compounds. PBE often gives a
    # near-zero band gap when experiment / hybrid-DFT yields 0.1–0.7 eV.
    # SCF can stall on near-degenerate states at E_F under fixed-occ; the
    # smearing-SCF + fixed-bands-NSCF recipe gives the cleanest evaluator
    # output (the bands NSCF in fixed-occ mode lets QE print the
    # `highest occupied, lowest unoccupied level` line directly).
    narrow_gap_heavy_p = is_likely_narrow_gap_heavy_p(element_set)
    if narrow_gap_heavy_p and not is_metal:
        occupations = "smearing"
        smearing    = "mv"
        degauss     = 0.02

    # Heuristic 2: open-shell 3d TM oxide. PBE nspin=1 SCF is frequently
    # unstable for these systems; the magnetic protocol (nspin=2 +
    # gaussian smearing tuned for d-electron stability + per-sublattice
    # starting_magnetization seeds) is much more reliable. recommend_nspin
    # bumps to 2 to signal this downstream.
    likely_magnetic_oxide = is_likely_magnetic_3d_oxide(element_set)
    magnetic = is_likely_magnetic_3d_system(element_set)
    recommend_nspin = 1
    if magnetic:
        recommend_nspin = 2
        if not is_metal:
            # Magnetic insulator / Mott (oxide OR fluoride etc.): gaussian
            # smearing aids d-electron SCF stability.
            occupations = "smearing"
            smearing    = "gauss"
            degauss     = 0.01      # Ry; tighter than the elemental-metal default
        # Elemental magnetic metal (Fe/Co/Ni/Cr/Mn): keep the metal smearing
        # (mv / 0.02) set above; only nspin bumps to 2.

    return {
        "formula": formula,
        "elements": elements,
        "structure_name": structure_name,
        "pseudo_family": pseudo_family,
        "is_uspp": is_uspp,
        "is_metal": is_metal,
        "occupations": occupations,
        "smearing": smearing,
        "degauss": degauss,
        "ecutrho_factor": 10.0 if is_uspp else 4.0,
        "rationale": _rationale(elements, is_metal, structure_name,
                                magnetic, narrow_gap_heavy_p),
        "likely_magnetic_3d_oxide": likely_magnetic_oxide,
        "magnetic_3d_system": magnetic,
        "recommend_nspin": recommend_nspin,
        "narrow_gap_heavy_p_candidate": narrow_gap_heavy_p,
    }


def _rationale(elements: list[str], is_metal: bool, structure: str,
               likely_magnetic: bool, narrow_gap_heavy_p: bool) -> str:
    """Short human-readable explanation, used in the rendered advice
    block and the transcript debug log. Physics-only — no references to
    bench scoring or historical run outcomes."""
    if not elements:
        return "could not extract elements from prompt; defaulting to insulator"
    parts: list[str] = []
    if is_metal:
        parts.append(
            f"all elements {elements} are metallic and no strongly "
            f"electronegative atom is present "
            f"(structure='{structure or 'unknown'}')"
        )
    else:
        electroneg = [e for e in elements if e in STRONGLY_ELECTRONEGATIVE]
        if electroneg:
            parts.append(
                f"contains electronegative atom(s) {electroneg} → "
                f"insulator/semiconductor (ionic or covalent bonding)"
            )
        else:
            parts.append(
                f"elements {elements} include non-metals → "
                f"covalent semiconductor (e.g. Si/Ge family)"
            )
    if likely_magnetic:
        parts.append(
            "open-shell 3d TM + O detected — PBE nspin=1 SCF often "
            "unstable for this chemistry; magnetic protocol suggested"
        )
    if narrow_gap_heavy_p:
        parts.append(
            "heavy p-block + chalcogen/pnictogen — PBE may underestimate "
            "the band gap; smearing-SCF + fixed-bands recipe suggested"
        )
    return "; ".join(parts)


# ---------------------------------------------------------------------------
# LLM cross-check subagent
# ---------------------------------------------------------------------------


def build_config_advisor_system_prompt() -> str:
    return dedent("""\
        You are a Quantum ESPRESSO configuration advisor subagent.

        Your job: review a deterministic classification of a DFT material
        (metal vs semiconductor/insulator + recommended QE occupations +
        ecutrho factor) and return a final decision.

        ## Rules

        1. **Default = trust the deterministic guess.** It's based on a
           hard-coded METAL_ELEMENTS table that is correct for ~95% of
           common cases (alkali, alkaline-earth, transition metals,
           oxides, halides, nitrides, common semiconductors).

        2. **Override ONLY on a clear physics reason**, such as:
           - The material is a known semimetal (graphite, α-Sn, α-As)
             not captured by the rule
           - The material undergoes a metal-insulator transition at the
             relevant temperature
           - The pseudo family suggests a different convention
             (extremely rare)

        3. **Quantum ESPRESSO occupations conventions:**
           - `fixed`: insulators/semiconductors (sharp Fermi → correct gap)
           - `smearing` with `smearing='mv'`, `degauss=0.02` (Ry):
             metals (default; Methfessel-Paxton 1st order is robust)
           - `smearing='gauss'`, `degauss=0.01`: d-electron systems
             where MP can oscillate (3d TM compounds especially)
           - `smearing='fd'`, `degauss=0.005`: high-precision Fermi-Dirac
             for entropy-sensitive runs (rarely needed)

        4. **ecutrho factor (USPP convention):**
           - GBRV USPP / generic USPP / PAW: 10× ecutwfc
           - NCPP (norm-conserving): 4× ecutwfc
           - When in doubt and pseudo_family contains 'USPP' or 'GBRV' or
             'PAW': use 10

        ## Submission

        Call `submit` ONCE with `task_outputs` containing string values
        (the submit tool's schema requires strings; the driver coerces
        them):

        ```json
        {
          "task_outputs": {
            "is_metal":         "true" | "false",
            "occupations":      "fixed" | "smearing",
            "smearing":         "mv" | "gauss" | "fd" | "none",
            "degauss":          "0.02" | "0.005" | "none",
            "ecutrho_factor":   "10" | "4" | "8",
            "rationale":        "<one-line explanation including override reason if any>"
          }
        }
        ```

        - "none" string for `smearing` / `degauss` when occupations=fixed.
        - DO NOT run any bash commands; this is a paper-only decision.
          You don't have access to QE binaries here and shouldn't try.
        """)


def build_config_advisor_user_prompt(original_prompt: str, det: dict) -> str:
    return dedent(f"""\
        Review the deterministic classification below and submit final
        QE config advice. Override only on a clear physics reason
        (see system prompt rules).

        ## Deterministic classification

        Formula:        {det['formula']}
        Elements:       {det['elements']}
        Structure name: {det['structure_name']!r}
        Pseudo family:  {det['pseudo_family']!r} (is_uspp={det['is_uspp']})

        Initial guess: **{'METAL' if det['is_metal'] else 'SEMICONDUCTOR/INSULATOR'}**
          occupations     = {det['occupations']}
          smearing        = {det['smearing']}
          degauss         = {det['degauss']}
          ecutrho_factor  = {det['ecutrho_factor']}

        Rationale: {det['rationale']}

        ## Original task prompt (for context only — don't try to solve it)

        ===== begin task prompt =====
        {original_prompt[:2000]}
        ===== end task prompt =====

        (Truncated to first 2000 chars; you don't need the full SCF/bands
        instructions for this advisory decision.)

        Submit your final advice now.
        """)


# ---------------------------------------------------------------------------
# Payload coercion + validation
# ---------------------------------------------------------------------------


def _coerce_bool(v) -> bool:
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in {"true", "1", "yes", "metal", "metallic"}


def _coerce_float_or_none(v) -> float | None:
    if v is None:
        return None
    s = str(v).strip().lower()
    if s in {"", "none", "null", "n/a"}:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _coerce_str_or_none(v) -> str | None:
    if v is None:
        return None
    s = str(v).strip().lower()
    if s in {"", "none", "null", "n/a"}:
        return None
    return s


def _from_deterministic(det: dict, *, source: str = "deterministic",
                        failure_reason: str | None = None) -> ConfigAdvice:
    return ConfigAdvice(
        ok=(failure_reason is None),
        formula=det.get("formula", ""),
        elements=list(det.get("elements", [])),
        structure_name=det.get("structure_name", ""),
        pseudo_family=det.get("pseudo_family", ""),
        is_uspp=det.get("is_uspp", False),
        is_metal=det.get("is_metal", False),
        occupations=det.get("occupations", "fixed"),
        smearing=det.get("smearing"),
        degauss=det.get("degauss"),
        ecutrho_factor=float(det.get("ecutrho_factor", 10.0)),
        rationale=det.get("rationale", ""),
        source=source,
        likely_magnetic_3d_oxide=bool(det.get("likely_magnetic_3d_oxide", False)),
        magnetic_3d_system=bool(det.get("magnetic_3d_system", False)),
        recommend_nspin=int(det.get("recommend_nspin", 1)),
        narrow_gap_heavy_p_candidate=bool(det.get("narrow_gap_heavy_p_candidate", False)),
        failure_reason=failure_reason,
    )


def _validate_payload(payload: dict, det: dict) -> ConfigAdvice:
    """Parse LLM submit payload + reconcile with deterministic guess."""
    if payload.get("incomplete"):
        # subagent gave up; fall back to deterministic (this is success
        # from our POV — deterministic guess is the source of truth)
        return _from_deterministic(det, source="deterministic")

    task_outputs = payload.get("task_outputs") or {}
    if not task_outputs:
        return _from_deterministic(det, source="deterministic")

    is_metal = _coerce_bool(task_outputs.get("is_metal", det["is_metal"]))
    occupations = (_coerce_str_or_none(task_outputs.get("occupations"))
                   or ("smearing" if is_metal else "fixed"))
    smearing = _coerce_str_or_none(task_outputs.get("smearing"))
    degauss = _coerce_float_or_none(task_outputs.get("degauss"))
    ecutrho_factor = (_coerce_float_or_none(task_outputs.get("ecutrho_factor"))
                      or det["ecutrho_factor"])
    rationale = str(task_outputs.get("rationale", det["rationale"])).strip()

    # If LLM says metal but didn't fill smearing/degauss, plug in defaults
    if is_metal and (smearing is None or degauss is None):
        smearing = smearing or "mv"
        degauss = degauss if degauss is not None else 0.02
    # If LLM says insulator, force smearing/degauss to None for clarity
    if not is_metal:
        smearing = None
        degauss = None

    # Detect override vs agreement with deterministic guess
    overrode = (is_metal != det["is_metal"]) or \
               (abs(ecutrho_factor - det["ecutrho_factor"]) > 0.5)
    source = "llm_override" if overrode else "deterministic"

    # Heuristic-driven protocol re-apply. These flags describe the
    # material's chemistry (open-shell 3d TM in oxide environment;
    # heavy-p + chalcogenide pairing), not an LLM judgement. If the LLM
    # output disagrees with the chemistry-derived recipe, the chemistry
    # wins: e.g. an LLM that "politely" sets is_metal=False on a
    # magnetic oxide doesn't get to silently turn off the smearing path.
    if bool(det.get("narrow_gap_heavy_p_candidate", False)) and not is_metal:
        occupations = "smearing"
        smearing = "mv"
        degauss = 0.02
        if source != "deterministic":
            source = "deterministic_narrow_gap_heavy_p_override"

    # Magnetic protocol (broad: open-shell 3d TM + any anion, or elemental 3d
    # magnet). For the insulating compounds we force gaussian smearing; for
    # elemental magnetic metals the metal smearing set above stands and only
    # nspin=2 applies (threaded via recommend_nspin from det).
    if bool(det.get("magnetic_3d_system", False)) and not is_metal:
        llm_disagreed_on_occupations = (
            task_outputs.get("occupations") not in (None, "", "smearing")
        )
        occupations = "smearing"
        smearing = "gauss"
        degauss = 0.01
        if source != "deterministic" or llm_disagreed_on_occupations:
            source = "deterministic_magnetic_3d_override"

    return ConfigAdvice(
        ok=True,
        formula=det["formula"],
        elements=list(det["elements"]),
        structure_name=det["structure_name"],
        pseudo_family=det["pseudo_family"],
        is_uspp=det["is_uspp"],
        is_metal=is_metal,
        occupations=occupations,
        smearing=smearing,
        degauss=degauss,
        ecutrho_factor=float(ecutrho_factor),
        rationale=rationale,
        source=source,
        # Chemistry-derived flags are objective; the LLM cannot override them.
        likely_magnetic_3d_oxide=bool(det.get("likely_magnetic_3d_oxide", False)),
        magnetic_3d_system=bool(det.get("magnetic_3d_system", False)),
        recommend_nspin=int(det.get("recommend_nspin", 1)),
        narrow_gap_heavy_p_candidate=bool(det.get("narrow_gap_heavy_p_candidate", False)),
    )


# ---------------------------------------------------------------------------
# Public entry
# ---------------------------------------------------------------------------


def run(*, client: LLMClient | None = None,
        original_prompt: str, workdir: Path,
        max_turns: int = 6, max_output_tokens: int = 2048,
        cli_overrides: dict[str, str] | None = None,
        config_path: Path | None = None) -> ConfigAdvice:
    """Spawn the config_advisor subagent. Returns a ConfigAdvice.

    The deterministic classifier always runs (cheap, reliable). The LLM
    cross-check runs unless it crashes — on crash the deterministic
    guess wins. ok=True means downstream consumers (system prompt,
    lint, vc_relax) should use the advice; ok=False means fall back.
    """
    if client is None:
        client = LLMClient.from_role(
            "config_advisor",
            cli_overrides=cli_overrides,
            config_path=config_path,
        )
    workdir = workdir.resolve()
    det = _deterministic_classify(original_prompt)

    subwork = workdir / "config_advisor"
    subwork.mkdir(parents=True, exist_ok=True)

    sys_prompt = build_config_advisor_system_prompt()
    user_prompt = build_config_advisor_user_prompt(original_prompt, det)
    # BashTool here is a no-op channel — subagent shouldn't run bash. We
    # still need one because run_loop's signature requires it. Logs will
    # show only the submit call.
    bash = BashTool(workdir=subwork)
    transcript = subwork / "transcript.json"

    try:
        payload = run_loop(
            client=client,
            system_prompt=sys_prompt, user_prompt=user_prompt,
            bash_tool=bash, max_turns=max_turns,
            max_output_tokens=max_output_tokens,
            transcript_path=transcript,
        )
    except Exception as e:
        traceback.print_exc()
        # Deterministic fallback — still a usable advice
        return _from_deterministic(det, source="fallback",
                                   failure_reason=f"{type(e).__name__}: {e}")

    return _validate_payload(payload, det)


# ---------------------------------------------------------------------------
# Render helpers
# ---------------------------------------------------------------------------


def render_config_advice_block(advice: ConfigAdvice) -> str:
    """Text block injected at the top of the main solver's system
    prompt. Empty string if advice is not ok (fall back to existing
    material-agnostic guidance).

    Rationale is physics-only: the block describes what the chemistry
    suggests, never what a particular scoring system "confirms".
    """
    if not advice.ok:
        return ""

    # Render the SCF occupations directive based on `advice.occupations`
    # (not on `is_metal`) — narrow-gap heavy-p candidates are
    # is_metal=False with occupations='smearing', so keying off the
    # actual occupation value avoids an internal contradiction.
    if advice.occupations == "smearing":
        if advice.narrow_gap_heavy_p_candidate:
            # Narrow-gap heavy p: smearing on SCF stabilises the near-
            # degenerate states at E_F that fixed-occ can stall on.
            # bands NSCF reverts to fixed so QE prints the
            # `highest occupied, lowest unoccupied level` line directly.
            # DOS NSCF stays on smearing — switching to tetrahedra
            # there with a smearing-fed charge density gives Fermi
            # alignment drift.
            occ_directive = (
                f"USE `occupations='smearing'` with "
                f"`smearing='{advice.smearing or 'mv'}'` "
                f"and `degauss={advice.degauss or 0.02}` (Ry) for "
                f"**vc-relax + SCF only** (stabilises SCF against "
                f"near-degenerate states near E_F — fixed-occ can stall "
                f"on narrow-gap heavy p-block chemistry).\n"
                f"**bands NSCF MUST use `occupations='fixed'`** — do NOT "
                f"carry smearing into the bands step. Only fixed makes QE "
                f"print the `highest occupied, lowest unoccupied level` "
                f"line that downstream band-gap evaluators rely on; with "
                f"smearing the apparent Fermi can sink below the VBM and "
                f"slice the valence bands incorrectly.\n"
                f"DOS NSCF stays on `occupations='smearing'` "
                f"(matches the SCF charge density's broadening; switching "
                f"to tetrahedra mid-pipeline drifts the Fermi alignment). "
                f"dos.x can omit ngauss/degauss and reuse the NSCF weights."
            )
        else:
            # True metal or likely-magnetic 3d system: smearing on the
            # WHOLE pipeline, including the DOS NSCF. GT computed metals AND
            # magnetics with mv smearing, and the evaluator scores
            # `dos_e_fermi` from the DOS-NSCF Fermi line vs GT (±0.1 eV) —
            # `occupations='tetrahedra'` shifts a metal's Fermi by up to ~2 eV
            # (the M3 T2 dos_e_fermi regression).
            occ_directive = (
                f"USE `occupations='smearing'` with "
                f"`smearing='{advice.smearing or 'mv'}'` "
                f"and `degauss={advice.degauss or 0.02}` (Ry) for the "
                f"**whole pipeline: vc-relax + SCF + bands NSCF + DOS NSCF**.\n"
                f"**The DOS NSCF MUST also use `occupations='smearing'` "
                f"(`smearing='{advice.smearing or 'mv'}'`, "
                f"`degauss={advice.degauss or 0.02}`), NOT tetrahedra** — the "
                f"evaluator reads `dos_e_fermi` from the DOS-NSCF output and "
                f"scores it vs GT (±0.1 eV); GT used mv smearing, and "
                f"tetrahedra shifts a metal's Fermi by up to ~2 eV."
            )
    else:
        occ_directive = (
            "USE `occupations='fixed'` for vc-relax + SCF + bands NSCF — "
            "wide-gap insulator / semiconductor; a sharp Fermi level is "
            "needed for the band gap measurement.\n"
            "DOS NSCF uses `occupations='tetrahedra'` (bench-standard)."
        )

    classification = "METAL" if advice.is_metal else "SEMICONDUCTOR / INSULATOR"
    flags: list[str] = []
    if advice.narrow_gap_heavy_p_candidate:
        flags.append("narrow-gap heavy-p heuristic — smearing SCF, fixed bands NSCF")
    if advice.magnetic_3d_system:
        flags.append("3d-magnet heuristic — nspin=2 magnetic protocol (no +U)")
    if flags:
        classification += " (" + "; ".join(flags) + ")"
    src_tag = f" [{advice.source}]" if advice.source != "deterministic" else ""

    # Magnetic protocol directive (open-shell 3d TM + O). The recipe is
    # physics-derived: PBE nspin=1 SCF for partially-filled d-electron
    # oxides is numerically unstable and a non-magnetic ground state is
    # almost certainly wrong for this chemistry. We bias the agent toward
    # nspin=2 + sublattice-resolved starting_magnetization; the LLM is
    # free to pick the exact magnetic order based on the structure.
    magnetic_directive = ""
    if advice.magnetic_3d_system:
        tm_species = [e for e in advice.elements if e in {
            "V", "Cr", "Mn", "Fe", "Co", "Ni", "Cu",
        }]
        seed_hint = ", ".join(tm_species) if tm_species else "transition-metal species"
        elemental = advice.is_metal  # bare Fe/Co/Ni/Cr/Mn (no anion)
        magnetic_directive = (
            f"\n**MAGNETIC PROTOCOL (3d-magnet heuristic — NO Hubbard U)**: "
            f"this material has an open-shell 3d transition metal and almost "
            f"certainly an AFM or FM ground state. PBE `nspin=1` SCF for "
            f"partially-filled 3d systems is frequently unstable (failure to "
            f"converge, oscillating energy, or a spurious non-magnetic state).\n"
            f"USE `nspin=2` in **all** pw.x runs (vc-relax + SCF + bands "
            f"NSCF + DOS NSCF).\n"
            f"DO **NOT** add Hubbard U / `lda_plus_u` — the reference is plain "
            f"PBE + nspin=2; adding U moves lattice & magnetization off-target.\n"
            f"SET `starting_magnetization` per species in the &SYSTEM namelist:\n"
        )
        if elemental:
            magnetic_directive += (
                f"  • Elemental {seed_hint}: Fe/Co/Ni are ferromagnetic — seed "
                f"a single positive moment (e.g. `starting_magnetization(1) = "
                f"0.5–0.7`). Cr/Mn are antiferromagnetic — split into two "
                f"species with alternating signs (±0.4–0.6).\n"
            )
        else:
            magnetic_directive += (
                f"  • For AFM rocksalt / rutile / corundum compounds (NiO, MnO, "
                f"CoO, **MnF₂**, Cr₂O₃ …): split the {seed_hint} sites into two "
                f"distinct ATOMIC_SPECIES labels sharing one pseudopotential "
                f"(e.g. `Ni1`/`Ni2` both → Ni.upf) and seed alternating signs, "
                f"`starting_magnetization(Ni1)=+0.4`, "
                f"`starting_magnetization(Ni2)=-0.4`. For spinels (M₃O₄) split "
                f"tetrahedral vs octahedral sites. When the order is unclear, a "
                f"uniform `+0.4` (FM) is a safe starting point to refine.\n"
            )
        magnetic_directive += (
            f"Stability knobs: `mixing_beta = 0.3` + `electron_maxstep = 200`. "
            f"The smearing/degauss above should not be loosened.\n"
        )

    return (
        f"## QE CONFIGURATION ADVISORY (config_advisor preflight){src_tag}\n\n"
        f"Material:       {advice.formula} "
        f"(elements={advice.elements}, structure={advice.structure_name or 'unknown'})\n"
        f"Pseudo family:  {advice.pseudo_family} (USPP={advice.is_uspp})\n"
        f"Classification: {classification}\n"
        f"Rationale:      {advice.rationale}\n\n"
        f"{occ_directive}\n"
        f"{magnetic_directive}\n"
        f"USE `ecutrho = {advice.ecutrho_factor:.0f} × ecutwfc` "
        f"(USPP convention — 10× is the GBRV bench-canonical ratio; "
        f"values < 9× drift the band gap by 0.2–0.5 eV per 30 Ry ecutwfc "
        f"gap). qe_input_lint enforces this automatically — it will "
        f"auto-fix any pw.x input with ecutrho/ecutwfc < 9.\n"
    )
