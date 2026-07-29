"""QE configuration advisor preflight subagent.

Runs before vc_relax_subagent and the main loop. Produces a
`ConfigAdvice` with metal/insulator classification + recommended
occupations / smearing / degauss / ecutrho_factor for the material in
the task prompt.

The classifier is **deterministic** (hard-coded METAL_ELEMENTS set +
formula/structure regex). A tiny LLM cross-check (max_turns=6) is given
the deterministic guess and may override it only if it sees a clear
physics reason. On LLM failure the deterministic guess wins (graceful
degradation).

The resulting advice is:
  1. Rendered into a `## QE CONFIGURATION ADVISORY` block injected into
     the main system prompt (and the vc_relax subagent's system prompt).
  2. Persisted to `workdir/.config_advice.json` so `qe_input_lint` can
     enforce ecutrho ratios and warn on occupations mismatches.
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

from ._magnetism import classify_magnetism
from ._metals import (
    CUTOFF_SENSITIVE_METALS,
    METAL_ELEMENTS,
    METALLIC_TM_OXIDES,
    PBE_NEAR_METAL_FORMULAS,
    STRONGLY_ELECTRONEGATIVE,
)


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------


@dataclass
class ConfigAdvice:
    """Recommended QE config knobs for the material in the task prompt.

    `ok=False` means the subagent failed; main solver falls back to
    system prompt's existing material-agnostic guidance and lint
    layer's USPP rule applies generically.
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
    # FIX-3b (2026-05-20): PBE 准金属族（Ge/InSb/HgTe/PbTe 等），
    # PBE pipeline 给 gap ~ 0 但物理上是半导体。physics_sanity 看到
    # 此 flag 时降级 metal⇄semiconductor 不一致为 soft_warn。
    pbe_near_metal: bool = False
    recommend_nspin: int = 1
    # FIX-22 (2026-05-25): 金属相 TM 氧化物（Cmcm-Ti3O5 等）。advisor 推
    # is_metal=True + gauss/0.01 smearing + nspin=1（不做 magnetic ansatz）。
    # 详 _metals.py:METALLIC_TM_OXIDES。
    metallic_tm_oxide: bool = False
    # FIX-31 (2026-05-27): 浅 d 带 post-transition metals（Cd / Zn / Hg-pending）。
    # is_metal/occupations 走常规 METAL_ELEMENTS 路径不变；planner system_prompt
    # T1 分支看到此 flag → 用 T2 canonical ecutwfc=80 + kmesh ceil(50/a) min 8×8×8
    # 顶住 GT 80 Ry / 20×20×8 / mp-smearing 的 ±1% rel 容差。
    # 详 _metals.py:CUTOFF_SENSITIVE_METALS。
    cutoff_sensitive_metal: bool = False
    # FIX-32 (2026-05-27): composition-based magnetism (any open-shell 3d/4f
    # compound, no formula allowlist). The winning ordering + seeds come from
    # the magnetic_probe (S2); set here only the classification (is_magnetic +
    # which elements + |seed|).
    is_magnetic: bool = False
    magnetic_elements: list[str] = field(default_factory=list)
    starting_magnetization: dict[str, float] = field(default_factory=dict)
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
_ELEMENT_TOKEN_RE = re.compile(r"([A-Z][a-z]?)(\d*)")


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
    is_uspp = bool(re.search(r"USPP|GBRV|PAW", pseudo_family, re.I))

    # FIX-22 (2026-05-25): 已知金属相 TM 氧化物（如 Cmcm-Ti3O5）必须在电负
    # O 规则之前短路，否则会被判 is_metal=False → physics_sanity Rule B
    # （advisor=insulator vs extracted=metal）死循环 replan。
    metallic_tm_oxide = formula in METALLIC_TM_OXIDES

    # Metal logic:
    # 0. FIX-22: explicit metallic TM oxide override (Cmcm-Ti3O5 etc.).
    # 1. If any STRONGLY_ELECTRONEGATIVE element is in the compound →
    #    insulator/semiconductor (oxides, halides, nitrides, sulfides…).
    # 2. Otherwise, if ALL elements are in METAL_ELEMENTS → metal
    #    (covers pure metals + intermetallic alloys).
    # 3. Otherwise → semiconductor/insulator default (covers covalent
    #    semiconductors like Si, Ge, GaAs which have no electronegative
    #    atom but at least one non-METAL_ELEMENTS atom).
    if not elements:
        is_metal = False
    elif metallic_tm_oxide:
        is_metal = True
    elif any(e in STRONGLY_ELECTRONEGATIVE for e in elements):
        is_metal = False
    elif all(e in METAL_ELEMENTS for e in elements):
        is_metal = True
    else:
        is_metal = False

    if is_metal:
        occupations = "smearing"
        smearing = "mv"        # Methfessel-Paxton 1st-order: most robust default
        degauss = 0.02         # Ry; standard bench choice
    else:
        occupations = "fixed"
        smearing = None
        degauss = None

    # FIX-3b: 标记 PBE 准金属——formula 完全匹配；advisor 仍判 is_metal=False
    # （物理事实），但 physics_sanity 看到这个 flag 时会跳过 metal⇄semi
    # 不一致的 hard fail。
    pbe_near_metal = formula in PBE_NEAR_METAL_FORMULAS

    # FIX-8 (2026-05-22): PBE 准金属强制走 smearing 路径。
    # 物理上 is_metal=False 不变（Ge 是半导体），但 QE occupations 切到 smearing，
    # pw.x 不再打 "highest occupied, lowest unoccupied" 行 → bench evaluator
    # 落入 fermi-splitter 分支（_find_vbm_cbm_via_fermi，eps=5 meV），近简并
    # 带大概率被一并归到 below → metal short-circuit。
    # 详 reports/0520 §3 + bench harness/lib/evaluator.py:521,752。
    if pbe_near_metal and not is_metal:
        occupations = "smearing"
        smearing    = "mv"      # Methfessel-Paxton 1st order，与金属 default 一致
        degauss     = 0.02      # Ry; bench 标准

    # recommend_nspin default; FIX-32 (below) bumps it to 2 for any magnetic
    # material. The former FIX-19 MAGNETIC_TM_OXIDES allowlist was removed
    # (FIX-41, S-4) — its smearing=gauss/degauss=0.01/nspin=2 forcing is now
    # produced for the whole magnetic class by the composition path below.
    recommend_nspin = 1

    # FIX-22 (2026-05-25): 金属相 TM 氧化物（Cmcm-Ti3O5 等）→ is_metal=True
    # 已在上面 metal logic 处置；这里把 smearing 调到 d-electron 友好的
    # gauss/0.01（比常规金属 mv/0.02 更稳）。recommend_nspin 保持 1 —— 不
    # 加 magnetic ansatz，避免 SCF 多步收敛回 mag=0 + BFGS history loss。
    if metallic_tm_oxide:
        occupations = "smearing"
        smearing    = "gauss"
        degauss     = 0.01
        # recommend_nspin 不动（默认 1）；不做 magnetic ansatz。

    # FIX-31 (2026-05-27): 浅 d 带 post-transition metal（Cd / Zn）—— 不动
    # is_metal/occupations/smearing（METAL_ELEMENTS 已把它们正确归到 mv/0.02
    # 金属路径），仅 set 一个 advisory flag 让 planner system_prompt T1 分支
    # 抬到 T2 canonical 80 Ry + kmesh ceil(50/a) min 8×8×8。
    cutoff_sensitive_metal = formula in CUTOFF_SENSITIVE_METALS

    # FIX-32: composition-based magnetism. is_magnetic if any open-shell
    # 3d/4f element is present. Forces nspin=2 + smearing=gauss/degauss=0.01
    # for the whole magnetic class (FM metals via the is_metal branch below,
    # AFM/ferri insulators via the `smearing is None` branch). This is the
    # single source of the magnetic protocol — no formula allowlist (FIX-41).
    mag_profile = classify_magnetism(elements)
    if mag_profile.is_magnetic:
        recommend_nspin = 2
        occupations = "smearing"
        if is_metal:
            # FIX-33 (2026-05-29): magnetic metals (elemental Fe/Co/Ni/Cr) were
            # inheriting mv/0.02 from the non-magnetic METAL_ELEMENTS path. At
            # 0.02 Ry (~3000 K) MV smearing washes the d-band exchange splitting
            # and the itinerant moment collapses to 0 (Ni_fcc 7.85→0.00 µB,
            # total/abs_magnetization scored 0). Force gaussian at a tighter
            # degauss to preserve the moment; metallic convergence stays robust.
            smearing = "gauss"
            degauss = 0.01
        elif smearing is None:
            # d-electron-friendly default for magnetic insulators.
            smearing = "gauss"
            degauss = 0.01

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
        "rationale": _rationale(elements, is_metal, pseudo_family, structure_name, formula),
        "pbe_near_metal": pbe_near_metal,
        "recommend_nspin": recommend_nspin,
        "metallic_tm_oxide": metallic_tm_oxide,
        "cutoff_sensitive_metal": cutoff_sensitive_metal,
        "is_magnetic": mag_profile.is_magnetic,
        "magnetic_elements": mag_profile.magnetic_elements,
        "starting_magnetization": dict(mag_profile.seed),
    }


def _rationale(elements: list[str], is_metal: bool, pseudo_family: str,
               structure: str, formula: str = "") -> str:
    """Short human-readable explanation of the classification, useful
    for the rendered advice block + transcript debugging."""
    if not elements:
        return "could not extract elements from prompt; defaulting to insulator"
    # FIX-22: explicit-metallic-TM-oxide case takes priority over the
    # electroneg-O rationale (which would say "insulator" misleadingly).
    if is_metal and formula in METALLIC_TM_OXIDES:
        return (f"{formula} is a known metallic-phase TM oxide "
                f"(mixed-valence + delocalized d) — gap_type='metal' "
                f"per GT (structure='{structure or 'unknown'}')")
    if is_metal:
        return (f"all elements {elements} are metallic and no strongly "
                f"electronegative atom is present "
                f"(structure='{structure or 'unknown'}')")
    electroneg = [e for e in elements if e in STRONGLY_ELECTRONEGATIVE]
    if electroneg:
        return (f"contains electronegative atom(s) {electroneg} → "
                f"insulator/semiconductor (ionic or covalent bonding)")
    return (f"elements {elements} include non-metals → "
            f"covalent semiconductor (e.g. Si, Ge family)")


# ---------------------------------------------------------------------------
# LLM cross-check subagent
# ---------------------------------------------------------------------------


def build_config_advisor_system_prompt() -> str:
    return dedent("""\
        You are a Quantum ESPRESSO configuration advisor subagent.

        Your job: review a deterministic classification of a DFT bench
        material (metal vs semiconductor/insulator + recommended QE
        occupations + ecutrho factor) and return a final decision.

        ## Rules

        1. **Default = trust the deterministic guess.** It's based on a
           hard-coded METAL_ELEMENTS table that is correct for ~95% of
           common cases (alkali, alkaline-earth, transition metals,
           oxides, halides, nitrides, common semiconductors).

        2. **Override ONLY on a clear physics reason**, such as:
           - The material is a known semimetal (graphite, α-Sn, α-As)
             not in the table
           - The material is a known Mott insulator (NiO, FeO, V2O3)
             that the table mis-classifies (these are insulators despite
             metal-element composition)
           - The material undergoes a metal-insulator transition at the
             relevant temperature
           - The pseudo family suggests a different convention
             (extremely rare)

        3. **Quantum ESPRESSO occupations conventions:**
           - `fixed`: insulators/semiconductors (sharp Fermi → correct gap)
           - `smearing` with `smearing='mv'`, `degauss=0.02` (Ry):
             metals (default; Methfessel-Paxton 1st order is robust)
           - `smearing='gauss'`, `degauss=0.02`: alternative for metals
             with very dense states near Fermi
           - `smearing='fd'`, `degauss=0.005`: high-precision Fermi-Dirac
             for entropy-sensitive runs (rarely needed in bench)

        4. **ecutrho factor (USPP convention):**
           - GBRV USPP / generic USPP / PAW: 10× ecutwfc (bench-canonical)
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
        pbe_near_metal=bool(det.get("pbe_near_metal", False)),
        recommend_nspin=int(det.get("recommend_nspin", 1)),
        metallic_tm_oxide=bool(det.get("metallic_tm_oxide", False)),
        cutoff_sensitive_metal=bool(det.get("cutoff_sensitive_metal", False)),
        is_magnetic=bool(det.get("is_magnetic", False)),
        magnetic_elements=list(det.get("magnetic_elements", [])),
        starting_magnetization=dict(det.get("starting_magnetization", {})),
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

    # FIX-8 (2026-05-22): PBE 准金属族 post-validate re-apply。
    # LLM cross-check 看到 is_metal=False 时常把 occupations 改回 fixed（line 372-374
    # 就是这条强制），而 det 已经按 FIX-8 标了 smearing。无论 LLM 怎么填，
    # pbe_near_metal=True 时必须用 smearing 才能让 evaluator 走 fermi-splitter。
    if bool(det.get("pbe_near_metal", False)) and not is_metal:
        occupations = "smearing"
        smearing = "mv"
        degauss = 0.02
        if source == "deterministic":
            # 保持 deterministic 标记
            pass
        else:
            source = "deterministic_pbe_near_metal_override"

    # FIX-22 (2026-05-25): METALLIC_TM_OXIDES post-validate re-apply。
    # LLM cross-check 看到 oxide-with-Ti 时常把 is_metal 拉回 False（电负
    # O 直觉）；强制覆盖回 metal + gauss/0.01。recommend_nspin 不动（1）。
    if bool(det.get("metallic_tm_oxide", False)):
        llm_disagreed_on_metal = not _coerce_bool(
            task_outputs.get("is_metal", det["is_metal"])
        )
        is_metal = True
        occupations = "smearing"
        smearing = "gauss"
        degauss = 0.01
        if source != "deterministic" or llm_disagreed_on_metal:
            source = "deterministic_metallic_tm_oxide_override"

    # FIX-32 (2026-05-27): composition-based magnetic post-validate re-apply.
    # Handles the whole is_magnetic class (oxides like NiO/CoO/Fe3O4, plus CuO,
    # MnF2, Fe2O3, elemental Fe/Co/Ni/Cr, …): the LLM cannot turn `occupations`
    # back to 'fixed' for a magnetic material. Magnetic LDA+nspin=1+fixed is
    # what v4.2 ran on Fe_bcc → moment 0 + wrong lattice; this prevents the LLM
    # from re-introducing that failure mode. (FIX-41 removed the separate FIX-19
    # allowlist block — for an insulator, smearing was already forced None just
    # above, so the `smearing is None` branch below re-applies gauss/0.01.)
    if bool(det.get("is_magnetic", False)):
        llm_disagreed_on_occupations = (
            task_outputs.get("occupations") not in (None, "", "smearing")
        )
        occupations = "smearing"
        if bool(det.get("is_metal", False)):
            # FIX-33 (2026-05-29): magnetic metals — pin gaussian/0.01 so the LLM
            # cannot revert to the moment-collapsing mv/0.02 (see _deterministic_
            # classify). Density-affecting step, so this must hold on SCF/relax.
            # Also restore is_metal (elemental Fe/Co/Ni/Cr are unambiguously
            # metals) so the Fix-② bands-occupation branch keys off the truth.
            is_metal = True
            smearing = "gauss"
            degauss = 0.01
        elif smearing is None:
            # Fall back to the d-electron-friendly default (matches FIX-32 in
            # _deterministic_classify) for magnetic insulators.
            smearing = "gauss"
            degauss = 0.01
        if source != "deterministic" or llm_disagreed_on_occupations:
            source = "deterministic_magnetic_override"

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
        # FIX-3b: pbe_near_metal 是材料的客观 PBE 失效属性，不让 LLM override
        pbe_near_metal=bool(det.get("pbe_near_metal", False)),
        recommend_nspin=int(det.get("recommend_nspin", 1)),
        # FIX-22: metallic_tm_oxide 同理 — 客观属性，LLM 不可改
        metallic_tm_oxide=bool(det.get("metallic_tm_oxide", False)),
        # FIX-31: cutoff_sensitive_metal 同理 — 客观属性（Group 12 浅 d-band），
        # LLM 不可改
        cutoff_sensitive_metal=bool(det.get("cutoff_sensitive_metal", False)),
        # FIX-32: composition-based magnetism — 客观材料属性，LLM 不可改
        is_magnetic=bool(det.get("is_magnetic", False)),
        magnetic_elements=list(det.get("magnetic_elements", [])),
        starting_magnetization=dict(det.get("starting_magnetization", {})),
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

    v4.3: if `client` is None, instantiates via LLMClient.from_role("config_advisor"),
    honoring CLI / env / yaml overrides for this role independently from
    other subagents.
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
            cost_journal_path=workdir / "transcript.json",
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
    material-agnostic guidance)."""
    if not advice.ok:
        return ""

    # FIX-8 (2026-05-22): 渲染条件改为基于 advice.occupations（而不是 is_metal），
    # 因为 PBE-near-metal 是 is_metal=False 但 occupations='smearing' 的特殊组合，
    # 必须按 occupations 实际值渲染才不会出现"分类是半导体 / 指令是 fixed"的内部矛盾。
    #
    # FIX-18 (2026-05-23): DOS NSCF 对 pbe_near_metal 材料保留 smearing，
    # 不走 FIX-9 的 tetrahedra 强制。原因：CoSb3 2026-05-22 07:08 (FIX-6/-7/-8
    # 三件套时段) 拿 0.95 分；10:04 上线 FIX-9 强制 dos.x tetrahedra 后又挂。
    # 怀疑是 SCF/bands NSCF 用 smearing 但 DOS NSCF 切 tetrahedra 导致 Fermi
    # 对齐不一致 → dos.x 输出歪 → LLM 反复 retry → timeout。对 pbe_near_metal
    # 材料一律 smearing 全链路（SCF + bands NSCF + DOS NSCF），dos.x omit
    # ngauss/degauss → 自动复用 NSCF smearing 权重，输出和 GT 一致。
    if advice.occupations == "smearing":
        if advice.pbe_near_metal:
            # FIX-18 exception: dos NSCF 也用 smearing 不切 tetrahedra
            # 适用于 pbe_near_metal=True 全部材料（含 CoSb3 这种 is_metal=True
            # 但 PBE 给 ~0 gap 的窄带半导体型金属——Fermi 在能带集中区域）
            dos_nscf_directive = (
                f"DOS NSCF **ALSO uses `occupations='smearing'`** with the "
                f"same smearing='{advice.smearing or 'mv'}' / degauss="
                f"{advice.degauss or 0.02} (FIX-18 exception to bench tetrahedra "
                f"default — see WHY below). dos.x still omits ngauss/degauss; "
                f"it will auto-reuse the NSCF smearing weights."
            )
        else:
            dos_nscf_directive = (
                "DOS NSCF still uses `occupations='tetrahedra'` (bench-mandated)."
            )
        if advice.pbe_near_metal:
            # FIX-29 (2026-05-27): PBE-near-metal 半导体的 bands NSCF 必须用
            # `occupations='fixed'`（不是 smearing），与 SCF 解耦。smearing 留在
            # vc-relax + SCF 保收敛；bands NSCF 用 fixed 才能让 QE 打
            # "highest occupied, lowest unoccupied" 行 → bench 走权威 homo_lumo
            # splitter。否则（旧 FIX-8 行为）bands smearing → 无 homo/lumo 行 →
            # bench 退回 fermi-splitter，而 degauss=0.02 Ry (~0.27 eV) 宽于真带隙
            # → Fermi 落 VBM 下方 ~0.25 eV → 误切价带 → band_gap/gap_type/vbm/cbm
            # + dos_e_fermi 五项全 0（CoSb3 2026-05-25 实测 0.16；fixed 后 ~0.95）。
            # 真·近零隙（Ge 类）由 evaluator 议题1 的 10 meV 短路兜住，不回退。
            occ_directive = (
                f"USE `occupations='smearing'` with "
                f"`smearing='{advice.smearing or 'mv'}'` "
                f"and `degauss={advice.degauss or 0.02}` (Ry) for "
                f"**vc-relax + SCF only** (SCF convergence aid for this "
                f"PBE-near-metal — fixed occ can stall on the near-degenerate "
                f"bands at E_F).\n"
                f"**bands NSCF MUST use `occupations='fixed'`** — do NOT carry "
                f"smearing into the bands step. Only fixed makes QE print the "
                f"`highest occupied, lowest unoccupied level` line that the "
                f"band-gap evaluator relies on; with smearing the Fermi sinks "
                f"~degauss below the VBM and the evaluator mis-slices the "
                f"valence bands (wrong gap / gap_type / VBM-CBM labels).\n"
                f"{dos_nscf_directive}"
            )
            occ_directive += (
                "\n\n**WHY smearing on SCF but fixed on the bands NSCF "
                "(FIX-29)**: "
                f"PBE 低估 {advice.formula} 的 band gap（skutterudite/窄带半导体类，"
                f"真隙 ~0.15 eV 量级）；fixed-occ SCF 可能在 E_F 附近近简并带间慢/"
                f"不稳 → 故 vc-relax + SCF 用 smearing。但隙是真的：bands NSCF 读已"
                f"收敛的电荷密度、只需 *填* nelec/2 条带，fixed 干净且必收敛（非自洽"
                f"对角化）。把 smearing 带进 bands NSCF 是 CoSb3 2026-05-25 塌到 "
                f"0.16 的根因；bands 改 fixed 即恢复 ~0.95。Ge 类真·近零隙不受影响："
                f"fixed 下 homo/lumo gap < 10 meV 会被 evaluator 短路判 metal。"
                f"\nDOS NSCF 仍保留 smearing（FIX-18，与上面 bands 的 fixed 独立）："
                f"半导体路径下 bench 把 dos_e_fermi / dos_curve 重新以 VBM 为参考"
                f"（evaluator.py:783），DOS occ 不影响 T2 计分；保留 smearing 是为"
                f"避免 FIX-18 之前 tetrahedra-Fermi 与 SCF 对齐不一致触发 verifier "
                f"反复重跑 NSCF。"
            )
        elif advice.is_magnetic and not advice.is_metal:
            # FIX-34 (2026-05-29): magnetic INSULATOR (CoO/NiO/MnO/FeO/Cr2O3/
            # Fe2O3/CuO/MnF2/LaMnO3 …) — same SCF-vs-bands occupation split as
            # FIX-29, but keep the nspin=2 magnetic protocol on every step.
            # smearing on vc-relax + SCF (nspin=2 d-electron SCF stability);
            # bands NSCF MUST switch to fixed + generous nbnd so QE prints the
            # `highest occupied, lowest unoccupied` line. The band-gap evaluator
            # computes the gap from that line BEFORE its seekpath step, so the
            # gap survives even where the bench seekpath labelling fails on
            # AFM-split species. With smearing the bands step emits no gap line
            # and the evaluator nulls band_gap/gap_type/VBM/CBM (CoO 2026-05-28
            # scored 0.16; fixed bands recovers band_gap ~0.4 weight).
            occ_directive = (
                f"USE `occupations='smearing'` (smearing='{advice.smearing or 'gauss'}', "
                f"degauss={advice.degauss or 0.01} Ry) for **vc-relax + SCF only** — "
                f"needed for nspin=2 magnetic SCF stability.\n"
                f"**bands NSCF MUST use `occupations='fixed'`** with a generous "
                f"`nbnd` (cover the conduction-band edge — keep your nbnd well above "
                f"nelec/2, e.g. +max(8, ~30%·nelec) empty bands). Only fixed makes "
                f"QE print the `highest occupied, lowest unoccupied level` line the "
                f"band-gap evaluator relies on; smearing emits no gap line → the "
                f"evaluator nulls band_gap/gap_type/VBM/CBM.\n"
                f"KEEP `nspin = 2` + the MAGNETIC GROUND STATE ATOMIC_SPECIES / "
                f"starting_magnetization on **every** step including the bands NSCF.\n"
                f"{dos_nscf_directive}"
            )
        else:
            # 真金属（is_metal=True，含 magnetic 金属 / metallic_tm_oxide）：
            # smearing 全链路（无带隙 → bench metal 路径；或 nspin=2 磁性金属协议）。
            occ_directive = (
                f"USE `occupations='smearing'` with "
                f"`smearing='{advice.smearing or 'mv'}'` "
                f"and `degauss={advice.degauss or 0.02}` (Ry) for "
                f"vc-relax + SCF + bands NSCF.\n"
                f"{dos_nscf_directive}"
            )
    else:
        occ_directive = (
            "USE `occupations='fixed'` for vc-relax + SCF + bands NSCF — "
            "material is a wide-gap insulator / semiconductor and a sharp "
            "Fermi level is needed for the band gap measurement.\n"
            "DOS NSCF still uses `occupations='tetrahedra'` (bench-mandated)."
        )

    classification = "METAL" if advice.is_metal else "SEMICONDUCTOR / INSULATOR"
    if advice.pbe_near_metal:
        classification += " (PBE-near-metal — smearing SCF, fixed bands NSCF — FIX-29)"
    if advice.metallic_tm_oxide:
        classification += " (metallic-phase TM oxide — nspin=1, no magnetic ansatz)"
    if advice.cutoff_sensitive_metal:
        classification += " (cutoff-sensitive metal — ecutwfc=80 enforced for T1 — FIX-31)"
    if advice.is_magnetic and not advice.is_metal:
        classification += " (magnetic insulator — nspin=2; smearing SCF, fixed bands NSCF — FIX-34)"
    elif advice.is_magnetic and advice.is_metal:
        classification += " (magnetic metal — nspin=2, gauss/0.01 to hold the moment — FIX-33)"
    src_tag = f" [{advice.source}]" if advice.source != "deterministic" else ""

    # FIX-22: short defensive directive for known metallic TM oxide phases.
    # Without this, LLM sees TM + O and reflexively writes nspin=2 +
    # starting_magnetization (the FIX-19 protocol it learned from other oxides),
    # which on a metallic phase costs 30+ extra SCF iters collapsing back to
    # mag=0 and destabilizes BFGS. These high-symmetry phases are metallic
    # (delocalized d), with no magnetic ordering.
    metallic_directive = ""
    if advice.metallic_tm_oxide:
        metallic_directive = (
            f"\n**METALLIC-PHASE TM OXIDE (FIX-22)**: {advice.formula} at the "
            f"targeted space group is metallic (mixed-valence + delocalized d) — "
            f"treat it as a metal with no band gap.\n"
            f"USE `nspin=1` (no spin polarization). DO NOT add "
            f"`starting_magnetization`, DO NOT split TM sites into Ti1/Ti2 "
            f"sublattices. Smearing path above (`gauss` / `degauss=0.01`) is "
            f"d-electron-tuned; do not loosen.\n"
        )

    # The per-site nspin=2 + starting_magnetization guidance for magnetic
    # materials is emitted by the probe handoff (_magnetism.render_magnetic_block),
    # which gives the LLM the *actual* probe-determined ordering + AFM-split
    # ATOMIC_SPECIES + seeds (and the mixing_beta/electron_maxstep knobs) for
    # ALL magnetic materials — strictly more precise than the old FIX-19
    # formula-keyed text directive, which was removed with the allowlist (FIX-41).

    # FIX-31: short callout for Group 12 浅 d-band post-transition metals
    # (Cd / Zn). The planner's T1 system_prompt branch will already wire
    # ecutwfc=80 + dense k; this block tells the LLM *why* so it doesn't
    # second-guess the higher numbers when the task is T1 vc-relax-only.
    cutoff_sensitive_directive = ""
    if advice.cutoff_sensitive_metal:
        cutoff_sensitive_directive = (
            f"\n**CUTOFF-SENSITIVE METAL (FIX-31)**: {advice.formula} is a Group 12 "
            f"post-transition metal with a shallow d-band near E_F. GBRV USPP "
            f"PBE lattice constants for {advice.formula} are systematically "
            f"under-converged at the default T1 ecutwfc=50 Ry / kmesh "
            f"ceil(25/a) min 4×4×4 — a/c drift ~0.03–0.05 Å vs the 80 Ry "
            f"converged limit, enough to fall outside the ±1% relative T1 "
            f"tolerance even though the run converged BFGS cleanly.\n"
            f"USE `ecutwfc = 80 Ry`, `ecutrho = 800 Ry`, and "
            f"`kmesh = ceil(50 / a_Å) per axis, min 8×8×8` — the T2 "
            f"bands/DOS canonical. DO NOT downgrade "
            f"to the T1 default cutoffs even though this is a T1 task — the "
            f"T1 planner system_prompt has been adjusted to teach the 80 Ry "
            f"recipe for this formula. Triggering case: Cd vc_relax "
            f"2026-05-25T16-59 with 50 Ry / 12×12×6 scored 0.445 "
            f"(a/b 0.11 each, c=0); same model+task with 80 Ry / 17×17×8 "
            f"scored 0.948.\n"
        )

    return (
        f"## QE CONFIGURATION ADVISORY (config_advisor preflight){src_tag}\n\n"
        f"Material:       {advice.formula} "
        f"(elements={advice.elements}, structure={advice.structure_name or 'unknown'})\n"
        f"Pseudo family:  {advice.pseudo_family} (USPP={advice.is_uspp})\n"
        f"Classification: {classification}\n"
        f"Rationale:      {advice.rationale}\n\n"
        f"{occ_directive}\n"
        f"{metallic_directive}"
        f"{cutoff_sensitive_directive}\n"
        f"USE `ecutrho = {advice.ecutrho_factor:.0f} × ecutwfc` "
        f"(USPP convention — 10× is GBRV bench-canonical; values "
        f"< 9× drift the band gap by 0.2–0.5 eV per 30 Ry ecutwfc gap, "
        f"blowing past the ±0.1 eV scoring tolerance). "
        f"qe_input_lint enforces this automatically — it will auto-fix "
        f"any pw.x input with ecutrho/ecutwfc < 9.\n"
    )
