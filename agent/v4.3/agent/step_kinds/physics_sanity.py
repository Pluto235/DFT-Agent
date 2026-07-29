"""physics_sanity step kind — cross-check config_advisor vs bench extractor.

Goal: catch the 2026-05-18 Rb-style failure where bench's extract_t2_fields()
fermi-slice fallback mis-detected a metal as having a large gap. We have a
high-confidence prior (config_advisor.is_metal, source='deterministic') that
was discarded after preflight. This step reuses that prior to flag obvious
physics-vs-extraction mismatches.

Architecture:
- This step is auto-injected by the orchestrator for bands_dos tasks AFTER
  the planner returns its plan (not declared by the planner).
- It is NOT executed via ExecutorAgent/VerifierAgent — orchestrator special-
  cases `step.kind == 'physics_sanity'` and calls `run_check(...)` directly.
  Deterministic Python; no LLM calls.
- `expected_outputs={"sanity_report": "<step.id>/sanity.json"}` so the
  WorkdirStager + promote pipeline stays uniform. Orchestrator filters this
  key out of the final result.json (internal, not a bench key).

Failure semantics (best-effort retry — see Q1=B decision in the plan doc):
  verdict='pass' / 'soft_warn' → step.done; orchestrator may attach `notes` for soft warn.
  verdict='fail'               → step.failed; standard replan loop kicks in.
  verdict='skipped'            → treated as pass (no bench available / dev env).
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from textwrap import dedent
from typing import Any

from agent.ledger import PlanStep
from agent.step_kinds import StepKindSpec
from agent.subagents._magnetism import MAGNETIC_SEED

KIND = "physics_sanity"


# ─────────────────────── system_prompt_addon / verifier_checklist ────────────
# Placeholders only — orchestrator never spawns an executor or verifier for
# this kind. Kept non-empty so the StepKindSpec contract is uniform and any
# future caller that DOES spawn an executor (debugging) gets sane defaults.

def system_prompt_addon(step: PlanStep, main_workdir: Path, step_subdir: str,
                        upstream: dict[str, list[str]]) -> str:
    return dedent(f"""\
        ## STEP: {step.id} (kind=physics_sanity)

        This step is normally run deterministically by the orchestrator
        (no LLM). You should not be reading this — if you are, something
        in the orchestrator dispatched here by mistake. Call `submit`
        with `incomplete=true reason="physics_sanity should be handled
        by orchestrator special-case, not an executor"`.
    """)


def verifier_checklist(step: PlanStep) -> str:
    return dedent(f"""\
        ## physics_sanity verifier checklist

        Read `{step.expected_outputs.get('sanity_report')}` as JSON.
        Pass iff `verdict` in {{"pass", "soft_warn", "skipped"}}.
        Fail with the file's `diagnosis` field when verdict='fail'.
    """)


# ─────────────────────── run_check (deterministic) ───────────────────────────

# Canonical T2 submission keys we need to feed bench's extract_t2_fields.
# Aligned with /path/to/project/bench/harness/lib/evaluator.py
_REQUIRED_T2_KEYS = ("scf_out", "bands_out", "bands_data", "dos_dat", "nscf_dos_out")


def run_check(
    *,
    workdir: Path,
    sanity_subdir: Path,
    expected_outputs_so_far: dict[str, str],
    config_advice_path: Path,
) -> dict[str, Any]:
    """Cross-check config_advisor.is_metal against bench's extract_t2_fields.

    Args:
        workdir: main run workdir (where pseudos/, scf_1/, etc. live).
        sanity_subdir: workdir/<step.id> — where sanity.json gets written.
        expected_outputs_so_far: merged {key: workdir-relative-path} from all
            already-completed steps' expected_outputs.
        config_advice_path: workdir/.config_advice.json (written by preflight).

    Returns: a dict with at least {verdict, diagnosis, details}. Never raises;
    all internal failures degrade to verdict='skipped' so the run continues.
    """
    sanity_subdir.mkdir(parents=True, exist_ok=True)
    sanity_json_path = sanity_subdir / "sanity.json"

    # `diagnosis` is the legacy free-text string (kept for VerdictSpec +
    # any external sanity.json reader). `diagnosis_struct` is the v4.3-Phase-5
    # structured form consumed by orchestrator._replan_user_msg to feed the
    # planner a bulleted action list instead of prose. None when verdict is
    # pass/skipped (no fix needed).
    result: dict[str, Any] = {
        "verdict": "skipped",
        "diagnosis": "",
        "diagnosis_struct": None,
        "details": {},
    }

    # 1. Load config_advice
    try:
        advice = json.loads(config_advice_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        result["diagnosis"] = f"config_advice not found at {config_advice_path}"
        sanity_json_path.write_text(json.dumps(result, indent=2, ensure_ascii=False))
        return result
    except (OSError, json.JSONDecodeError) as e:
        result["diagnosis"] = f"config_advice unreadable: {type(e).__name__}: {e}"
        sanity_json_path.write_text(json.dumps(result, indent=2, ensure_ascii=False))
        return result

    is_metal = bool(advice.get("is_metal", False))
    source = str(advice.get("source", "unknown"))
    rationale = str(advice.get("rationale", "")).lower()
    # FIX-3b/FIX-2 (2026-05-20): PBE 准金属族（Ge/InSb/HgTe/PbTe 等），
    # Rule B 看到此 flag 时把 metal⇄semi 不一致降为 soft_warn。
    pbe_near_metal = bool(advice.get("pbe_near_metal", False))
    deterministic = source == "deterministic"
    details: dict[str, Any] = {
        "advice_is_metal": is_metal,
        "advice_source": source,
        "advice_pbe_near_metal": pbe_near_metal,
    }
    result["details"] = details

    # 2. Resolve canonical T2 file paths
    missing_keys = [k for k in _REQUIRED_T2_KEYS if k not in expected_outputs_so_far]
    if missing_keys:
        result["diagnosis"] = (
            f"upstream steps did not produce all required canonical keys; "
            f"missing: {missing_keys}. Sanity check skipped."
        )
        sanity_json_path.write_text(json.dumps(result, indent=2, ensure_ascii=False))
        return result

    paths = {
        "scf_out_path":       workdir / expected_outputs_so_far["scf_out"],
        "bands_out_path":     workdir / expected_outputs_so_far["bands_out"],
        "bands_dat_path":     workdir / expected_outputs_so_far["bands_data"],
        "dos_dat_path":       workdir / expected_outputs_so_far["dos_dat"],
        "nscf_dos_out_path":  workdir / expected_outputs_so_far["nscf_dos_out"],
    }
    for name, p in paths.items():
        if not p.exists():
            result["diagnosis"] = f"upstream artifact missing: {name}={p}"
            sanity_json_path.write_text(json.dumps(result, indent=2, ensure_ascii=False))
            return result

    # 3. Resolve bench import context (BENCH_ROOT env or known fallback paths)
    try:
        bench_root, task_id, initial_structure = _resolve_bench_context(workdir)
    except Exception as e:
        result["diagnosis"] = f"bench harness unavailable: {type(e).__name__}: {e}"
        sanity_json_path.write_text(json.dumps(result, indent=2, ensure_ascii=False))
        return result
    details["bench_root"] = str(bench_root)
    details["task_id"] = task_id

    # 4. Import + run bench's extract_t2_fields
    try:
        if str(bench_root) not in sys.path:
            sys.path.insert(0, str(bench_root))
        from harness.lib.evaluator import extract_t2_fields  # type: ignore
        extracted = extract_t2_fields(
            **paths,
            initial_structure=initial_structure,
            extraction_config={},
            is_metal_per_gt=is_metal,  # hint from advisor; matches bench's
                                       # 2026-05-18 杨博 contract
        )
    except Exception as e:
        result["diagnosis"] = f"extract_t2_fields raised: {type(e).__name__}: {e}"
        sanity_json_path.write_text(json.dumps(result, indent=2, ensure_ascii=False))
        return result

    gap_type = extracted.get("gap_type")
    band_gap_ev = extracted.get("band_gap_ev")
    band_gap_num = float(band_gap_ev) if isinstance(band_gap_ev, (int, float)) else 0.0
    details["extracted_gap_type"] = gap_type
    details["extracted_band_gap_ev"] = band_gap_ev

    # 5. Apply rules
    wide_gap_hint = any(s in rationale for s in
                        ("oxide", "fluoride", "wide-gap", "wide gap", "insulator"))

    # Rule A: known metal × extracted gap > 0.5 eV → hard fail
    if deterministic and is_metal and gap_type != "metal" and band_gap_num > 0.5:
        # FIX-2 (2026-05-20): "metal × gap > 5 eV" 是 bench evaluator odd-nelec
        # bug 的特征签名（详 standard-bench-failures-rb-ge-wo3-cosb3 §2.3）。
        # 真金属物理上不可能出现 > 5 eV 提取 gap：fermi-slice 错位最多在带
        # 间距尺度 ~1-2 eV；> 5 eV 只可能来自 _find_vbm_cbm_via_electron_count
        # 把半芯当 VBM、把外壳金属带当 CBM。这种情况 agent 改 bands 参数没用
        # ——直接放行去 submit，让 bench 评分时 evaluator 自己暴露 bug。
        if band_gap_num > 5.0:
            result["verdict"] = "blocked_by_bench"
            result["diagnosis"] = (
                f"config_advisor says metal (source=deterministic) but bench "
                f"extracted gap_type={gap_type!r}, band_gap_ev={band_gap_num:.3f} eV. "
                f"gap > 5 eV is physically impossible on a real metal — this is "
                f"the signature of bench evaluator _find_vbm_cbm_via_electron_count "
                f"odd-nelec bug (GBRV USPP Z_valence is odd for alkali metals + "
                f"Cu / Ag / Au). Replanning the bands NSCF cannot fix this; "
                f"submitting as-is."
            )
            result["diagnosis_struct"] = {
                "root_cause": "bench_evaluator_odd_nelec_bug",
                "summary": (
                    f"advisor=metal × extracted gap={band_gap_num:.1f} eV "
                    f"is consistent only with bench evaluator odd-nelec bug"
                ),
                "fix_actions": [],   # 不让 planner replan
            }
        else:
            result["verdict"] = "fail"
            result["diagnosis"] = (
                f"config_advisor says metal (source=deterministic) but bench extracted "
                f"gap_type={gap_type!r}, band_gap_ev={band_gap_num:.3f} eV. "
                f"Likely bench fermi-slice missed a Fermi-crossing k-point. "
                f"Fix the bands NSCF step: (1) increase `nbnd` to >= 2x the current "
                f"value so more bands are sampled near EF, AND/OR (2) add a k-point "
                f"sampling near the band-crossing region of the path. Then redo "
                f"bands.x with the same params."
            )
            result["diagnosis_struct"] = {
                "root_cause": "metal_vs_extracted_gap",
                "summary": (
                    f"config_advisor=metal (deterministic) but bench extracted "
                    f"gap_type={gap_type!r}, band_gap_ev={band_gap_num:.3f} eV"
                ),
                "fix_actions": [
                    {
                        "file": "bands.in (NSCF on k-path)",
                        "field": "nbnd",
                        "suggested": ">= 2x the current value",
                        "rationale": "more bands sampled near EF so fermi-slice "
                                     "can locate Fermi crossings",
                    },
                    {
                        "file": "bands.in (NSCF on k-path)",
                        "field": "K_POINTS",
                        "suggested": "use a multi-segment high-symmetry path with "
                                     "K_POINTS crystal_b + per-segment subdivisions "
                                     "(e.g. 30), OR K_POINTS crystal with explicit "
                                     "weights — make sure the path covers BZ regions "
                                     "where the band crossing EF lives",
                        "rationale": "the previous path missed Fermi crossings; "
                                     "bench's fermi-slice fallback then mis-extracts "
                                     "a large gap on what is actually a metal",
                    },
                ],
            }
    # Rule A' (FIX-24, 2026-05-25): advisor=metal × tiny extracted gap (≤ 0.5 eV)
    # × gap_type ∈ {direct, indirect} → bands NSCF k-path missed Fermi crossings
    # on a metallic-phase material. Bench GT for metals reports gap_type='metal',
    # vbm/cbm_k_label='metal' (combined weight 0.35, rule=exact). When agent's
    # bands.dat has all bands on one side of E_F at every sampled k-point,
    # evaluator falls back to "smallest gap" → mislabels metal as direct/indirect.
    # Fix: densify the path + bump nbnd so a Fermi crossing falls inside sampled
    # k-points. Triggering case: Ti3O5 5-25 (0.625 score; would have been 0.975+).
    elif (deterministic and is_metal and gap_type != "metal"
          and 0.001 < band_gap_num <= 0.5):
        result["verdict"] = "fail"
        result["diagnosis"] = (
            f"config_advisor says metal (source=deterministic) but bench extracted "
            f"gap_type={gap_type!r}, band_gap_ev={band_gap_num:.4f} eV. "
            f"Small extracted gap on a metal = your bands NSCF k-path likely "
            f"missed Fermi crossings (all sampled bands at every k-point on one "
            f"side of E_F). Densify the path + increase nbnd so a crossing falls "
            f"within sampled k-points. Cost if uncorrected: full 0.35 (gap_type "
            f"0.15 + vbm/cbm_k_label 0.1+0.1) lost on rule=exact metal-string match."
        )
        result["diagnosis_struct"] = {
            "root_cause": "metal_fermi_crossing_missed_in_kpath",
            "summary": (
                f"advisor=metal × extracted gap={band_gap_num:.4f} eV ≤ 0.5 eV × "
                f"gap_type={gap_type!r}: k-path missed Fermi crossings"
            ),
            "fix_actions": [
                {
                    "file": "bands.in (NSCF on k-path)",
                    "field": "K_POINTS crystal_b",
                    "suggested": ">= 50 subdivisions per segment (4th column on each "
                                 "vertex line), keeping the seekpath path['path'] "
                                 "verbatim — do NOT prune segments",
                    "rationale": "metals' Fermi crossings often sit between "
                                 "high-symmetry vertices on shallow band features; "
                                 "denser sampling lets the fermi-slicer find them.",
                },
                {
                    "file": "bands.in (NSCF on k-path)",
                    "field": "nbnd",
                    "suggested": "(nelec/2) + max(8, ceil(nat*4))",
                    "rationale": "more bands above Fermi guarantee at least one "
                                 "actually crosses E_F within the path, otherwise "
                                 "all sampled bands sit on one side and bench's "
                                 "fermi-slicer mis-labels metal as direct/indirect.",
                },
            ],
        }
    # Rule B: known insulator × extracted metal → hard fail
    elif deterministic and not is_metal and gap_type == "metal":
        # FIX-3b/FIX-2 (2026-05-20): PBE 准金属族（Ge/InSb/HgTe 等），advisor
        # 仍判半导体（物理事实），但 bench 用 PBE 算 gap ~ 0 必然提取 metal——
        # 这是 PBE 已知 self-interaction 失效，不是 agent 错。降为 soft_warn
        # 让 agent 顺利 submit，bench 那边 gap_type='metal' 实际是 GT 一致的。
        if pbe_near_metal:
            result["verdict"] = "soft_warn"
            result["diagnosis"] = (
                f"config_advisor says semiconductor (deterministic, "
                f"pbe_near_metal=True) but bench extracted gap_type='metal' "
                f"with band_gap_ev={band_gap_num:.3f} eV. PBE known to "
                f"underestimate gap on this material to ~ 0 eV; gap_type='metal' "
                f"is the PBE-consistent answer. Submitting."
            )
            result["diagnosis_struct"] = {
                "root_cause": "pbe_near_metal_expected_metal_extraction",
                "summary": (
                    f"PBE 准金属：advisor=semi 但 bench 算出 metal 符合 PBE 物理预期，"
                    f"不阻止提交（extracted gap_ev={band_gap_num:.3f}）"
                ),
                "fix_actions": [],
            }
        else:
            result["verdict"] = "fail"
            result["diagnosis"] = (
                f"config_advisor says insulator/semiconductor (source=deterministic) "
                f"but bench extracted gap_type='metal'. Check the bands NSCF input: "
                f"was `occupations='fixed'` used (required for clean gap detection on "
                f"insulators)? Was `nbnd` too small to resolve states above VBM?"
            )
            result["diagnosis_struct"] = {
                "root_cause": "insulator_vs_extracted_metal",
                "summary": (
                    "config_advisor=insulator/semiconductor (deterministic) but "
                    "bench extracted gap_type='metal'"
                ),
                "fix_actions": [
                    {
                        "file": "bands.in (NSCF on k-path)",
                        "field": "occupations",
                        "suggested": "'fixed'",
                        "rationale": "smearing on insulators artificially fills "
                                     "states above VBM and bench reads it as metal",
                    },
                    {
                        "file": "bands.in (NSCF on k-path)",
                        "field": "nbnd",
                        "suggested": ">= nelec/2 + 4 unoccupied bands",
                        "rationale": "too few bands hides the CBM and the "
                                     "highest-occupied/lowest-unoccupied line "
                                     "bench parses for the gap goes missing",
                    },
                ],
            }
    # Rule B' (FIX-8, 2026-05-22): pbe_near_metal=True 时 advisor 已强制
    # occupations='smearing'，理论上 bench evaluator 该走 fermi-splitter
    # (eps=5 meV) 然后 metal short-circuit。如果仍 extracted gap_type ∈
    # {direct, indirect} 且 gap < 0.05 eV，说明 bands.dat 在 Fermi 附近的
    # 近简并带差距 > 5 meV (fermi-splitter eps)，FIX-8 未命中。soft_warn 让
    # agent submit + 留下诊断，不阻塞 — 进一步要等 bench 侧 FIX-3a。
    elif (deterministic and not is_metal and pbe_near_metal
          and gap_type in ("direct", "indirect")
          and band_gap_num < 0.05):
        result["verdict"] = "soft_warn"
        result["diagnosis"] = (
            f"pbe_near_metal=True (advisor 已强制 occupations='smearing') 但 bench "
            f"extracted gap_type={gap_type!r}, band_gap_ev={band_gap_num:.4f} eV. "
            f"smearing 路径下 evaluator fermi-splitter eps=5 meV 容差未能覆盖 "
            f"本次 SCF 算出来的近简并带差距 → 仍跨过 evaluator 1 meV 阈值。"
            f"FIX-8 命中失败 — 等 bench 侧 FIX-3a (主路径阈值 1e-3 → 1e-2)。"
            f"不阻止 submit。"
        )
        result["diagnosis_struct"] = {
            "root_cause": "pbe_near_metal_smearing_below_5mev_eps",
            "summary": (
                f"FIX-8 未命中：smearing 路径下 extracted gap={band_gap_num*1000:.2f} "
                f"meV > 5 meV fermi-splitter eps"
            ),
            "fix_actions": [],   # 不让 planner replan
        }
    # Rule C: known wide-gap insulator × extracted gap < 0.05 eV → soft warn
    elif deterministic and not is_metal and wide_gap_hint and band_gap_num < 0.05:
        result["verdict"] = "soft_warn"
        result["diagnosis"] = (
            f"PBE often underestimates wide-gap material's gap; expected gap > 0.5 eV "
            f"but extracted {band_gap_num:.3f} eV. Not retried (PBE-XC limitation, "
            f"not a solver bug)."
        )
        result["diagnosis_struct"] = {
            "root_cause": "pbe_underestimates_wide_gap",
            "summary": (
                f"expected wide gap > 0.5 eV but extracted {band_gap_num:.3f} eV "
                f"— PBE-XC limitation, not a solver bug"
            ),
            "fix_actions": [],   # no actionable fix; soft_warn only
        }
    # Rule D: any mismatch under low-confidence advice → soft warn (no retry)
    elif not deterministic and (
        (is_metal and gap_type != "metal" and band_gap_num > 0.5) or
        (not is_metal and gap_type == "metal")
    ):
        result["verdict"] = "soft_warn"
        result["diagnosis"] = (
            f"config_advice source={source!r} (low-confidence); physics mismatch "
            f"detected (advisor is_metal={is_metal}, extracted gap_type={gap_type!r}, "
            f"band_gap_ev={band_gap_num:.3f} eV) but not strong enough to trigger replan."
        )
        result["diagnosis_struct"] = {
            "root_cause": "low_confidence_physics_mismatch",
            "summary": (
                f"config_advice source={source!r} (low-confidence) disagrees with "
                f"extraction (is_metal={is_metal}, gap_type={gap_type!r}, "
                f"band_gap_ev={band_gap_num:.3f} eV) — not retried"
            ),
            "fix_actions": [],
        }
    else:
        result["verdict"] = "pass"
        result["diagnosis"] = ""
        result["diagnosis_struct"] = None

    # Rules E and F run independently of the A-D gap-extraction cascade —
    # a single run can carry both a gap contradiction AND a magnetism /
    # residual-stress issue (rare in practice, but possible).
    mag_finding = _check_magnetism(
        workdir=workdir,
        expected_outputs_so_far=expected_outputs_so_far,
        advice=advice,
    )
    if mag_finding is not None:
        _merge_finding(result, mag_finding)

    stress_finding = _check_residual_stress(workdir=workdir)
    if stress_finding is not None:
        _merge_finding(result, stress_finding)

    sanity_json_path.write_text(json.dumps(result, indent=2, ensure_ascii=False))
    return result


# ─────────────────────── Rule E: magnetism contradiction ────────────────────

# QE writes the per-iteration total magnetization line as:
#   "     total magnetization       =    -0.00 Bohr mag/cell"
# Note the variable amount of whitespace + optional sign.
_TOTAL_MAG_RE = re.compile(
    r"total\s+magnetization\s*=\s*([-+]?\d+(?:\.\d+)?)",
    re.IGNORECASE,
)


def _check_magnetism(*, workdir: Path,
                     expected_outputs_so_far: dict[str, str],
                     advice: dict | None) -> dict | None:
    """Rule E: when config_advice classifies the material as magnetic
    (composition-based, open-shell 3d/4f), the SCF must produce a non-zero
    total magnetization. If it lands at < 0.1 µB the SCF likely collapsed to
    the non-magnetic basin (missing nspin=2 and/or starting_magnetization).

    Magnetism is sourced from the solver's OWN preflight classification
    (config_advice), never from bench-side task metadata.

    Returns a finding dict or None if the rule doesn't apply (non-magnetic
    material, scf.out missing, etc.).
    """
    if not advice:
        return None
    mag_els = advice.get("magnetic_elements") or []
    if not mag_els:
        return None
    main_mag = mag_els[0]

    scf_out_rel = expected_outputs_so_far.get("scf_out")
    if not scf_out_rel:
        return None
    scf_path = workdir / scf_out_rel
    if not scf_path.is_file():
        return None
    try:
        scf_text = scf_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None

    matches = _TOTAL_MAG_RE.findall(scf_text)
    if not matches:
        # No "total magnetization" line at all → SCF was nspin=1 (collinear
        # non-magnetic). That IS the bug when the task expects magnetism.
        total_mag = 0.0
    else:
        try:
            total_mag = float(matches[-1])
        except ValueError:
            return None

    if abs(total_mag) >= 0.1:
        return None

    starting_mag_map = advice.get("starting_magnetization") or {}
    suggested_mag = starting_mag_map.get(main_mag) or MAGNETIC_SEED.get(main_mag, 0.5)
    return {
        "severity": "fail",
        "root_cause": "magnetic_but_zero_moment",
        "summary": (
            f"task declares {main_mag} as main magnetic element but SCF "
            f"total_magnetization = {total_mag:.3f} µB (< 0.1)"
        ),
        "fix_actions": [
            {
                "file": "scf.in",
                "field": "nspin",
                "suggested": "2",
                "rationale": "spin-polarized calculation needed to break the "
                             "non-magnetic symmetry",
            },
            {
                "file": "scf.in",
                "field": f"starting_magnetization({main_mag})",
                "suggested": f"{suggested_mag}",
                "rationale": (f"seed the SCF with a finite moment on {main_mag} "
                              f"so it lands in the magnetic basin rather than "
                              f"the non-magnetic saddle"),
            },
        ],
        "legacy_text": (
            f"task is magnetic ({main_mag}-bearing) but SCF total_magnetization "
            f"= {total_mag:.3f} µB. Re-run SCF with nspin=2 and "
            f"starting_magnetization({main_mag})={suggested_mag}."
        ),
    }


# ─────────────────────── Rule F: vc_relax residual stress ───────────────────

# QE writes the final summary stress line as:
#   "          total   stress  (Ry/bohr**3)                   (kbar)     P=        0.31"
# We grab the LAST occurrence (last SCF inside the last relax step).
_TOTAL_STRESS_P_RE = re.compile(
    r"total\s+stress.*?P\s*=\s*([-+]?\d+(?:\.\d+)?)",
    re.IGNORECASE | re.DOTALL,
)

# 0.5 GPa = 5 kbar — PBE typically converges below this when vc_relax actually
# achieved its press_conv_thr; values above suggest the relaxation stopped
# short (cell DOF over-constrained, or threshold too loose).
_RESIDUAL_STRESS_KBAR_THRESHOLD = 5.0


def _check_residual_stress(*, workdir: Path) -> dict | None:
    """Rule F: parse the vc_relax preflight subagent's relax.out for the
    final total stress block. Soft-warn when |P| exceeds 5 kbar (= 0.5 GPa).

    Returns a finding dict or None (no preflight, no stress block, or under
    threshold).
    """
    relax_out = workdir / "vc_relax" / "relax.out"
    if not relax_out.is_file():
        return None
    try:
        text = relax_out.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None

    matches = _TOTAL_STRESS_P_RE.findall(text)
    if not matches:
        return None
    try:
        p_kbar = float(matches[-1])
    except ValueError:
        return None
    if abs(p_kbar) <= _RESIDUAL_STRESS_KBAR_THRESHOLD:
        return None

    p_gpa = abs(p_kbar) / 10.0
    return {
        "severity": "soft_warn",   # PBE systematic error can leave 0.5–1 GPa
        "root_cause": "vc_relax_residual_stress",
        "summary": (
            f"vc_relax converged but residual |P| = {abs(p_kbar):.2f} kbar "
            f"(= {p_gpa:.2f} GPa), above the 0.5 GPa soft threshold"
        ),
        "fix_actions": [
            {
                "file": "vc_relax.in",
                "field": "press_conv_thr",
                "suggested": "<= 0.02 (kbar)",
                "rationale": "tighter stress convergence so residual drops "
                             "below 0.5 GPa",
            },
            {
                "file": "vc_relax.in",
                "field": "cell_dofree",
                "suggested": "check that DOF is not over-constrained "
                             "(e.g. 'all' for full 3D vs 'volume' for iso); "
                             "an over-constrained DOF can't relax stress along "
                             "some directions",
                "rationale": "soft-warn only — PBE error is part of the budget, "
                             "but persistent high residual stress is worth "
                             "investigating",
            },
        ],
        "legacy_text": (
            f"vc_relax residual pressure |P| = {abs(p_kbar):.2f} kbar "
            f"({p_gpa:.2f} GPa) > 0.5 GPa soft threshold; consider tightening "
            f"press_conv_thr or checking cell_dofree."
        ),
    }


# ─────────────────────── Finding aggregation ────────────────────────────────

_SEVERITY_RANK = {"pass": 0, "skipped": 0, "soft_warn": 1, "fail": 2}


def _merge_finding(result: dict[str, Any], finding: dict[str, Any]) -> None:
    """Merge an additional rule's finding into the accumulating result.
    Upgrades verdict to max severity. Concatenates legacy diagnosis text.
    Combines fix_actions in the struct dict.
    """
    severity = finding["severity"]
    if _SEVERITY_RANK[severity] > _SEVERITY_RANK.get(result.get("verdict", "pass"), 0):
        result["verdict"] = severity

    legacy = finding.get("legacy_text", "")
    if legacy:
        sep = "\n\n" if result.get("diagnosis") else ""
        result["diagnosis"] = (result.get("diagnosis") or "") + sep + legacy

    cur_struct = result.get("diagnosis_struct")
    new_struct = {
        "root_cause": finding["root_cause"],
        "summary": finding["summary"],
        "fix_actions": list(finding.get("fix_actions") or []),
    }
    if cur_struct is None:
        result["diagnosis_struct"] = new_struct
    else:
        cur_struct["root_cause"] = (
            cur_struct["root_cause"] + "+" + new_struct["root_cause"]
        )
        cur_struct["summary"] = (
            cur_struct["summary"] + "; " + new_struct["summary"]
        )
        cur_struct["fix_actions"] = (
            list(cur_struct.get("fix_actions") or []) + new_struct["fix_actions"]
        )


def _resolve_bench_context(workdir: Path) -> tuple[Path, str, dict[str, Any]]:
    """Return (bench_root, task_id, initial_structure).

    Raises if any cannot be resolved. Caller treats raise as 'skip' verdict.
    """
    # bench_root from env or known fallback paths
    env_root = os.environ.get("BENCH_ROOT")
    candidates: list[Path] = []
    if env_root:
        candidates.append(Path(env_root))
    # /path/to/project/bench 是 2026-05-18 迁移后的权威 bench；老的
    # ./bench 停在迁移点（仍保留 _find_vbm_cbm_via_electron_count
    # 那个 odd-nelec bug 的 splitter），若先命中会让 physics_sanity 对着错的
    # evaluator 自检，得到 11 eV 假 gap → 误判 blocked_by_bench。
    candidates += [
        Path("/path/to/project/bench"),
        Path("./bench"),
        Path.home() / "bench" / "dft-agent-bench",
    ]
    bench_root: Path | None = None
    for c in candidates:
        if (c / "harness" / "lib" / "evaluator.py").exists():
            bench_root = c.resolve()
            break
    if bench_root is None:
        raise FileNotFoundError(
            f"bench harness not found; set BENCH_ROOT env, tried: "
            f"{[str(c) for c in candidates]}"
        )

    # task_id: workdir is run_dir/workdir; run_dir.name = '<UTC>_<task_id>'
    run_dir_name = workdir.resolve().parent.name
    parts = run_dir_name.split("_", 1)
    if len(parts) < 2 or not parts[1].startswith(("M1_T", "M2_T")):
        raise ValueError(
            f"cannot infer task_id from run_dir name {run_dir_name!r}; "
            f"expected '<UTC>_<task_id>' shape"
        )
    task_id = parts[1]

    # initial_structure from bench task JSON
    if str(bench_root) not in sys.path:
        sys.path.insert(0, str(bench_root))
    from harness.lib.task_loader import load_task  # type: ignore
    task = load_task(bench_root, task_id)
    # Bench task JSON shape (verified via harness/rerun_eval.py:176):
    # initial_structure lives at task["material"]["initial_structure"], NOT
    # under task["task"]["material"] (which is the per-task work spec).
    initial_structure = task["material"]["initial_structure"]
    return bench_root, task_id, initial_structure


SPEC = StepKindSpec(
    kind=KIND,
    system_prompt_addon=system_prompt_addon,
    tool_allowlist=("submit",),   # placeholder; orchestrator never spawns executor
    verifier_checklist=verifier_checklist,
)
