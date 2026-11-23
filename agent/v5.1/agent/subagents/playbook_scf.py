"""SCF non-convergence playbook — symptom → remedy decision table.

Pure data + a `match_symptom()` lookup. Lives outside the LLM prompt so
unit tests can verify each (regex, remedy) row independently.

The remedies map to a dict of overrides for `write_qe_input`. The sub-agent
spawns a new calc using the parent's original args merged with the remedy
patch, then `run_pw` + `verify_run` it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass
class Remedy:
    name: str
    apply: dict                # overrides merged into write_qe_input args
    rationale: str             # one-line why
    next_remedy: str | None = None  # name to try if this one also fails


@dataclass
class PlaybookEntry:
    symptom: str                # human-readable label
    pattern: re.Pattern         # regex on pw.x output (tail / errors window)
    remedies: list[Remedy] = field(default_factory=list)


# Concrete table. Order matters: pattern matching is first-hit-wins, so
# put more-specific patterns ahead of more-generic ones.
PLAYBOOK: list[PlaybookEntry] = [
    PlaybookEntry(
        symptom="SCF did not converge in N iterations",
        pattern=re.compile(
            r"convergence NOT achieved (?:after|in)\s*\d*\s*iterations",
            re.IGNORECASE,
        ),
        remedies=[
            Remedy(
                name="raise_electron_maxstep",
                apply={"extra_electrons": {"electron_maxstep": 200}},
                rationale="default 100 iter wasn't enough; allow more",
                next_remedy="lower_mixing_beta",
            ),
            Remedy(
                name="lower_mixing_beta",
                apply={"extra_electrons": {"mixing_beta": 0.3, "mixing_ndim": 12}},
                rationale="aggressive mixing oscillates; damp & widen subspace",
                next_remedy="local_tf_mixing",
            ),
            Remedy(
                name="local_tf_mixing",
                apply={"extra_electrons": {"mixing_mode": "local-TF"}},
                rationale="systems with charge sloshing need local-TF preconditioner",
            ),
        ],
    ),
    PlaybookEntry(
        symptom="charge density negative",
        pattern=re.compile(r"charge density is (?:too )?negative", re.IGNORECASE),
        remedies=[
            Remedy(
                name="local_tf_mixing",
                apply={"extra_electrons": {"mixing_mode": "local-TF",
                                            "mixing_beta": 0.4}},
                rationale="negative density → switch preconditioner",
                next_remedy="densify_kgrid",
            ),
            Remedy(
                name="densify_kgrid",
                apply={"kpts_multiplier": 2},
                rationale="under-sampled BZ producing fractional occupations",
            ),
        ],
    ),
    PlaybookEntry(
        symptom="S matrix not positive definite (metallic / no smearing)",
        pattern=re.compile(r"S matrix (?:is )?not positive definite", re.IGNORECASE),
        remedies=[
            Remedy(
                name="enforce_metal_smearing",
                apply={"occupations": "smearing", "smearing": "mv", "degauss": 0.02},
                rationale="metal without smearing → MV + 0.02 Ry standard",
                next_remedy="raise_ecutrho_50pct",
            ),
            Remedy(
                name="raise_ecutrho_50pct",
                apply={"ecutrho_multiplier": 1.5},
                rationale="overlap matrix ill-conditioned at low ecutrho",
            ),
        ],
    ),
    PlaybookEntry(
        symptom="c_bands / eigenvalues not converged",
        pattern=re.compile(
            r"(eigenvalues not converged|Error in routine c_bands)",
            re.IGNORECASE,
        ),
        remedies=[
            Remedy(
                name="loose_diago_thr",
                apply={"extra_electrons": {"diago_thr_init": 1.0e-2,
                                            "startingwfc": "atomic+random"}},
                rationale="initial guess bad; loosen diag threshold + perturb wfc",
                next_remedy="switch_to_cg",
            ),
            Remedy(
                name="switch_to_cg",
                apply={"extra_electrons": {"diagonalization": "cg"}},
                rationale="Davidson stuck; CG is slower but more robust",
            ),
        ],
    ),
    PlaybookEntry(
        symptom="oscillating tot_charge (sloshing)",
        pattern=re.compile(
            r"(estimated scf accuracy.*?(?:E[\-+]\d{2}|0\.\d+E[\-+]\d{2}).*?\n.*?){5,}",
            re.IGNORECASE | re.DOTALL,
        ),
        remedies=[
            Remedy(
                name="damp_aggressive",
                apply={"extra_electrons": {"mixing_beta": 0.2, "mixing_ndim": 12,
                                            "electron_maxstep": 300}},
                rationale="oscillation → strong damping + larger Pulay subspace",
            ),
        ],
    ),
    PlaybookEntry(
        symptom="symmetry error",
        pattern=re.compile(r"symmetry (?:error|inconsistent|not preserved)",
                           re.IGNORECASE),
        remedies=[
            Remedy(
                name="force_symmorphic",
                apply={"extra_system": {"force_symmorphic": True}},
                rationale="non-symmorphic symmetries causing trouble; enforce symmorphic",
            ),
        ],
    ),
]


def match_symptom(error_text: str) -> PlaybookEntry | None:
    """First-hit-wins regex lookup over PLAYBOOK. None if no rule matches."""
    if not error_text:
        return None
    for entry in PLAYBOOK:
        if entry.pattern.search(error_text):
            return entry
    return None


def merge_remedy(base_args: dict, remedy: Remedy) -> dict:
    """Return a new write_qe_input args dict with remedy patches applied.

    Handles three special multiplier keys (not real QE keywords) inline:
      - kpts_multiplier (densify k-mesh)
      - ecutrho_multiplier (raise ecutrho)
      - ecutwfc_multiplier (raise ecutwfc)
    """
    out = dict(base_args)
    patch = dict(remedy.apply)

    if "kpts_multiplier" in patch:
        mult = patch.pop("kpts_multiplier")
        out["kpts"] = [int(k * mult) for k in out.get("kpts", [4, 4, 4])]
    if "ecutrho_multiplier" in patch:
        mult = patch.pop("ecutrho_multiplier")
        out["ecutrho"] = float(out.get("ecutrho", 480.0)) * mult
    if "ecutwfc_multiplier" in patch:
        mult = patch.pop("ecutwfc_multiplier")
        out["ecutwfc"] = float(out.get("ecutwfc", 60.0)) * mult

    # extra_control / extra_system / extra_electrons → deep merge
    for k in ("extra_control", "extra_system", "extra_electrons"):
        if k in patch:
            merged = dict(out.get(k) or {})
            merged.update(patch.pop(k))
            out[k] = merged

    # Anything else (occupations, smearing, degauss, ...) is a flat override
    out.update(patch)
    return out
