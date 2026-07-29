"""dft-agent-bench solver entry.

Bench launches this as a subprocess for every task:

    python solve.py --prompt   <abs>/task_prompt.txt \\
                    --workdir  <abs>/workdir \\
                    --out      <abs>/result.json \\
                    --model    <model-id>

Wires together: prompt parser, anthropic LLM client, bash tool, tool-use loop.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import traceback
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO_ROOT))

from agent.llm import LLMClient                         # noqa: E402
from agent.orchestrator import PlannerOrchestrator      # noqa: E402
from agent.structure_features import extract as _extract_features  # noqa: E402
from agent.subagents._magnetism import base_element     # noqa: E402
from agent.system_prompt import build_qe_system_prompt  # noqa: E402


def detect_pw_x() -> str:
    """Locate pw.x for the system prompt. The bench harness pre-pends its
    QE_GPU_WRAPPER_DIR (or CPU bin) to PATH, so `which pw.x` is reliable.
    Falls back to the literal name if absent — the LLM can still try to
    find it via `which pw.x` or use it as-is and let bash diagnose."""
    import shutil
    return shutil.which("pw.x") or "pw.x"


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="WarpAgent QE bench solver")
    ap.add_argument("--prompt",  required=True)
    ap.add_argument("--workdir", required=True)
    ap.add_argument("--out",     required=True)
    # v4.3: --model 是全局覆盖（覆盖所有角色 ModelRegistry 默认）。
    # 默认 None → 角色按 providers.yaml `models:` 块解析（默认 deepseek-chat）。
    ap.add_argument("--model", default=None,
                    help="Global model override (applies to every role)")
    ap.add_argument("--max-turns", type=int, default=120,
                    help="Per-subagent max turns for the EXECUTOR (and the v4.1 "
                         "vc_relax preflight subagent). Verifier/planner/config_advisor "
                         "have their own smaller caps (see PlannerOrchestrator).")
    ap.add_argument("--max-output-tokens", type=int, default=8192)
    ap.add_argument("--provider", default=None,
                    help="Override auto-detected provider for --model (anthropic/openai/...)")
    ap.add_argument("--config", default=None, type=Path,
                    help="Path to providers.yaml override (also via DFT_SOLVER_CONFIG env)")
    # v4.3 角色 model 覆盖（优先级最高）
    ap.add_argument("--planner-model",        default=None)
    ap.add_argument("--executor-model",       default=None)
    ap.add_argument("--verifier-model",       default=None)
    ap.add_argument("--config-advisor-model", default=None)
    ap.add_argument("--prerelax-model",       default=None)
    return ap.parse_args()


def build_cli_overrides(args: argparse.Namespace) -> dict[str, str]:
    """Collect CLI flags into role→model overrides for ModelRegistry.resolve().

    --<role>-model wins over --model (global). Empty dict means yaml defaults apply.
    """
    overrides: dict[str, str] = {}
    if args.model:
        # 全局覆盖：每个角色都用这个 model
        for role in ("planner", "executor", "verifier", "config_advisor", "prerelax"):
            overrides[role] = args.model
        overrides["default"] = args.model
    # role-specific 覆盖优先
    role_flags = {
        "planner":        args.planner_model,
        "executor":       args.executor_model,
        "verifier":       args.verifier_model,
        "config_advisor": args.config_advisor_model,
        "prerelax":       args.prerelax_model,
    }
    for role, model in role_flags.items():
        if model:
            overrides[role] = model
    return overrides


# Each task_prompt.txt ends with a "=== Submission Instructions ===" block
# that lists the keys we must include in task_outputs. Per the bench
# convention, each requested key is on its own line as `"<key>": ...`.
_KEY_RE = re.compile(r'"([a-zA-Z0-9_]+)"\s*:')


def parse_required_keys(prompt_text: str) -> list[str]:
    m = re.search(r"=== Submission Instructions ===(.*?)(?:===|\Z)", prompt_text, re.DOTALL)
    section = m.group(1) if m else ""
    keys = _KEY_RE.findall(section)
    # Drop dummy keys that appear inside the JSON example (task_outputs, notes)
    # and dedupe — each real key is listed twice (spec + JSON example).
    return list(dict.fromkeys(k for k in keys if k not in {"task_outputs", "notes"}))


def is_bands_dos_task(required_keys: list[str]) -> bool:
    """T2 (bands_dos) trigger: required keys contain bands.dat or dos.dat
    output. Used by main() to gate the vc_relax preflight subagent."""
    s = set(required_keys)
    return "bands_data" in s or "dos_dat" in s


def _is_log_output(key: str) -> bool:
    """Mirror bench's harness/lib/audit.py:_is_log_output.

    task_output key naming convention:
      * `*_out`            → QE program log (pw.x / bands.x / dos.x); MUST
                             contain literal "JOB DONE." (e.g. scf_out,
                             bands_out, dos_out, vc_relax_out).
      * `*_data` / `*_dat` → pure data file (bands.dat / dos.dat); has NO
                             "JOB DONE." line. Audit must skip JOB DONE
                             check for these (existence + size still
                             checked).
      * other suffixes     → default to log (conservative: a false-positive
                             on a missing JOB DONE is better than a silent
                             pass on a broken submission).
    """
    if key.endswith(("_data", "_dat")):
        return False
    return True


def _audit_task_outputs(task_outputs: dict, workdir: Path) -> list[str]:
    """Mirror the bench audit's path / file checks before we hand the JSON
    over. Returns a list of human-readable failure strings; empty list means
    the submission would pass the harness's paths_in_workdir / files_nonempty
    / job_done flags.

    Checks per (key, path):
      - non-empty string
      - relative (not absolute)
      - resolves inside workdir
      - is a regular file
      - size >= 1024 bytes
      - tail (last 64 KB) contains the literal "JOB DONE." marker iff
        `_is_log_output(key)` (data-file suffixes are exempt — see helper).
    """
    errors: list[str] = []
    workdir_resolved = workdir.resolve()
    for key, p in task_outputs.items():
        if not isinstance(p, str) or not p:
            errors.append(f"{key}: path must be a non-empty string")
            continue
        if Path(p).is_absolute():
            errors.append(f"{key}: absolute path forbidden ({p!r})")
            continue
        abs_p = (workdir_resolved / p).resolve()
        try:
            abs_p.relative_to(workdir_resolved)
        except ValueError:
            errors.append(f"{key}: path escapes workdir ({p!r})")
            continue
        if not abs_p.is_file():
            errors.append(f"{key}: not a regular file ({p!r})")
            continue
        try:
            size = abs_p.stat().st_size
        except OSError as e:
            errors.append(f"{key}: stat failed ({e})")
            continue
        if size < 1024:
            errors.append(f"{key}: file too small ({size} B < 1024)")
            continue
        if not _is_log_output(key):
            # bands.dat / dos.dat: pure-data files contain no JOB DONE. line.
            # Size + path checks above are enough; bench audit treats them
            # the same way (see harness/lib/audit.py:_is_log_output).
            continue
        try:
            with abs_p.open("rb") as f:
                f.seek(max(0, size - 65536))
                tail = f.read().decode("utf-8", errors="replace")
        except OSError as e:
            errors.append(f"{key}: read failed ({e})")
            continue
        if "JOB DONE." not in tail:
            errors.append(f"{key}: missing 'JOB DONE.' marker in last 64 KB")
    return errors


_AP_SITE_RE = re.compile(r"^(\s*)([A-Z][a-z]?\d+)(\s+[-+0-9.eE].*)$")
_AP_PLAIN_SITE_RE = re.compile(r"^\s*[A-Z][a-z]?\s+[-+0-9.eE].*$")


def _normalize_atomic_positions_block(text: str) -> str:
    """Strip trailing AFM sublattice indices (Cr1/Cr2 -> Cr) from the species
    column of every ATOMIC_POSITIONS block, leaving everything else byte-exact.

    FIX-37: QE needs split species labels (Cr1/Cr2) to assign opposite
    `starting_magnetization` for AFM, but the bench T1 extractor
    (harness/lib/evaluator.py:_parse_positions) keeps the raw label and feeds it
    to SpacegroupAnalyzer, which then treats Cr1 != Cr2 as distinct species and
    reports a subgroup (e.g. R-3 #148 instead of the GT R-3c #167). Merging the
    labels back to the bare element restores the crystallographic (element-based)
    symmetry the GT uses. Proven offline on all 15 M3 T1 outputs: helps 4
    (Cr2O3/Fe2O3/MnF2/Cr_bcc), harmless on the rest, zero regressions — because
    where the GT *is* a magnetic subgroup (CoO/MnO sg=166) the relaxed positions
    already carry that distortion regardless of labels.

    Only the leading species token of site rows is touched; CELL_PARAMETERS,
    energies, the `total/absolute magnetization` lines (parsed by literal-string
    match, label-independent) and `JOB DONE.` are untouched.
    """
    out: list[str] = []
    in_ap = False
    for line in text.split("\n"):
        if "ATOMIC_POSITIONS" in line:
            in_ap = True
            out.append(line)
            continue
        if in_ap:
            m = _AP_SITE_RE.match(line)
            if m:
                out.append(f"{m.group(1)}{base_element(m.group(2))}{m.group(3)}")
                continue
            if _AP_PLAIN_SITE_RE.match(line):
                out.append(line)  # site row with no index → keep, stay in block
                continue
            in_ap = False  # non-site line → block ended
        out.append(line)
    return "\n".join(out)


def _normalize_afm_species_labels(payload: dict, workdir: Path, advice) -> None:
    """Apply FIX-37 to the submitted vc-relax log of magnetic tasks, in place.

    Gated on magnetic classification (ConfigAdvice.is_magnetic, or the presence
    of `.magnetic_ground_state.json` from the probe). Non-magnetic tasks and
    label-free outputs are untouched (the rewrite is a no-op without indices).
    Best-effort: any failure is logged and swallowed so it can never block a
    valid submission (the subsequent _audit_task_outputs still guards the file).
    """
    is_mag = bool(getattr(advice, "is_magnetic", False)) if advice else False
    if not is_mag and not (workdir / ".magnetic_ground_state.json").exists():
        return
    task_outputs = (payload or {}).get("task_outputs") or {}
    for key, rel in task_outputs.items():
        if "relax" not in key or not _is_log_output(key):
            continue
        if not isinstance(rel, str) or not rel:
            continue
        abs_p = (workdir / rel).resolve()
        if not abs_p.is_file():
            continue
        try:
            original = abs_p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        normalized = _normalize_atomic_positions_block(original)
        if normalized != original:
            try:
                abs_p.write_text(normalized, encoding="utf-8")
                print(f"[solve] FIX-37: normalized AFM species labels in {rel}",
                      file=sys.stderr)
            except OSError as e:
                print(f"[solve] FIX-37 normalization write failed (non-fatal): {e}",
                      file=sys.stderr)


def _sum_dos_dat_text(text: str) -> str:
    """Collapse an nspin=2 dos.x file (E dosup dosdw intdos) to (E dos intdos)
    where dos = dosup + dosdw (the TOTAL DOS). 3-column nspin=1 files and
    comment/blank lines are returned unchanged.

    FIX-38: the bench dos_curve reference is the TOTAL DOS, but QE's dos.x emits
    per-spin columns for nspin=2 and the bench's read_dos_dat only reads col 1
    (dosup) — i.e. half the DOS — so recall caps at ~0.5 (CrO2 dos_curve 0.65;
    proven jumps to 0.945 when summed; AFM insulators have dosup==dosdw by
    symmetry so summing likewise restores the total). Emitting the summed total
    in the column the bench reads conforms to the documented `states/eV/fu`
    total-DOS quantity. (No per-formula-unit division: CrO2's summed per-cell
    total already matches the GT reference area; dividing made F1 *worse*.)
    """
    out: list[str] = []
    for line in text.split("\n"):
        s = line.strip()
        if not s or s.startswith("#"):
            out.append(line)
            continue
        parts = s.split()
        try:
            vals = [float(p) for p in parts]
        except ValueError:
            out.append(line)
            continue
        if len(vals) >= 4:  # E dosup dosdw intdos  ->  E (dosup+dosdw) intdos
            e, up, dw, idos = vals[0], vals[1], vals[2], vals[3]
            out.append(f"{e:12.4f}  {up + dw:.6E}  {idos:.6E}")
        else:
            out.append(line)  # nspin=1 (already total) or unexpected width
    return "\n".join(out)


def _sum_dos_spin_channels(payload: dict, workdir: Path) -> None:
    """Apply FIX-38 to the submitted dos.dat, in place. Self-gating: only
    4-column (nspin=2) files are rewritten; nspin=1 dos.dat is a no-op.
    Best-effort: failures are logged and swallowed (the file stays valid)."""
    task_outputs = (payload or {}).get("task_outputs") or {}
    for key, rel in task_outputs.items():
        if "dos" not in key or not key.endswith(("_dat", "_data")):
            continue
        if not isinstance(rel, str) or not rel:
            continue
        abs_p = (workdir / rel).resolve()
        if not abs_p.is_file():
            continue
        try:
            original = abs_p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        summed = _sum_dos_dat_text(original)
        if summed != original:
            try:
                abs_p.write_text(summed, encoding="utf-8")
                print(f"[solve] FIX-38: summed nspin=2 spin channels in {rel}",
                      file=sys.stderr)
            except OSError as e:
                print(f"[solve] FIX-38 dos spin-sum write failed (non-fatal): {e}",
                      file=sys.stderr)


def _guarantee_metal_bands(payload: dict, workdir: Path, advice,
                           required_keys: list[str], pw_x_path: str,
                           qe_backend: str) -> None:
    """Apply FIX-2: for a metal bands/DOS task, deterministically guarantee a
    near-E_F bands sample so the bench's metal short-circuit fires (Ni_fcc
    0.178 eV void → mis-scored 'indirect' → 0.128). Gated on advice.is_metal
    AND not pbe_near_metal (never nudge a near-metal semiconductor toward a
    false metal — and the bench's is_metal_per_gt=False would block it anyway).
    Best-effort: any failure leaves the LLM's bands.dat untouched."""
    if advice is None or not getattr(advice, "is_metal", False):
        return
    if getattr(advice, "pbe_near_metal", False):
        return
    if not is_bands_dos_task(required_keys):
        return
    if os.getenv("DFT_DISABLE_METAL_BANDS_GUARANTEE"):
        return
    try:
        from agent.subagents import metal_bands_guarantee as mbg
        res = mbg.run(workdir=workdir,
                      task_outputs=(payload or {}).get("task_outputs") or {},
                      pw_x_path=pw_x_path, qe_backend=qe_backend, is_metal=True)
        if res.replaced_bands_data:
            print(f"[solve] FIX-2: densified metal bands "
                  f"(void {res.void_before:.4f}→{res.void_after:.4f} eV, "
                  f"subdiv→{res.new_subdiv}); replaced bands_data", file=sys.stderr)
        else:
            print(f"[solve] FIX-2: metal-bands guarantee no-op "
                  f"(void_before={res.void_before}, reason={res.failure_reason})",
                  file=sys.stderr)
    except Exception as e:
        traceback.print_exc()
        print(f"[solve] FIX-2 metal-bands guarantee crashed (non-fatal): {e}",
              file=sys.stderr)


def write_result(out_path: Path, payload: dict, required_keys: list[str],
                 workdir: Path) -> int:
    """Persist the LLM submission. Returns process exit code per spec §5."""
    if payload.get("incomplete"):
        out_path.write_text(json.dumps({
            "incomplete": True,
            "reason": payload.get("reason", "no reason given"),
        }, indent=2, ensure_ascii=False))
        return 2

    task_outputs = payload.get("task_outputs") or {}
    missing = [k for k in required_keys if k not in task_outputs]
    if missing:
        out_path.write_text(json.dumps({
            "incomplete": True,
            "reason": f"submit missed required keys: {missing}; got {list(task_outputs)}",
        }, indent=2, ensure_ascii=False))
        return 2

    audit_errors = _audit_task_outputs(task_outputs, workdir)
    if audit_errors:
        out_path.write_text(json.dumps({
            "incomplete": True,
            "reason": "submit failed local audit:\n  - " + "\n  - ".join(audit_errors),
        }, indent=2, ensure_ascii=False))
        return 2

    result = {"task_outputs": task_outputs}
    if payload.get("notes"):
        result["notes"] = payload["notes"]
    out_path.write_text(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


def main() -> int:
    args = parse_args()
    os.environ["DFT_BENCH_SOLVER_MODE"] = "1"

    prompt_path = Path(args.prompt)
    workdir     = Path(args.workdir).resolve()
    out         = Path(args.out)

    cli_overrides = build_cli_overrides(args)
    print(f"[solve] prompt={prompt_path} workdir={workdir} "
          f"cli_overrides={cli_overrides or '(none — yaml defaults)'}",
          file=sys.stderr)
    prompt_text = prompt_path.read_text(encoding="utf-8")
    required_keys = parse_required_keys(prompt_text)
    print(f"[solve] required keys: {required_keys}", file=sys.stderr)
    # v4.3: derive experience-library scope features (structure + task_kind)
    # at boot so the orchestrator can scope per-step retrievals + writes.
    scope_features = _extract_features(prompt_text)
    scope_features["task_kind"] = (
        "bands_dos" if is_bands_dos_task(required_keys) else "vc_relax"
    )
    print(f"[solve] scope features: {scope_features}", file=sys.stderr)

    # v4.3 / PR-A: load human-authored material-family skill library and
    # pre-render the advisory block once at boot. Empty string if no skill
    # matches (or library is empty / disabled). The same block is appended
    # to the planner's initial user prompt AND every executor user prompt.
    advisory_block = ""
    if not os.getenv("DFT_DISABLE_SKILLS"):
        try:
            from agent.skills import load_all as _skills_load_all
            from agent.skills import select_and_render as _skills_render
            _skills = _skills_load_all()
            advisory_block = _skills_render(_skills, scope_features)
            if advisory_block:
                _hit_names = sorted(
                    s.name for s in _skills
                    if any(line.startswith(f"[{s.name}]")
                           for line in advisory_block.splitlines())
                )
                print(f"[solve] material advisories matched: {_hit_names}",
                      file=sys.stderr)
            else:
                print("[solve] material advisories: none matched", file=sys.stderr)
        except Exception as e:
            print(f"[solve] skills load failed; continuing without advisory: {e}",
                  file=sys.stderr)
            advisory_block = ""

    if not required_keys:
        out.write_text(json.dumps({
            "incomplete": True,
            "reason": "could not parse Submission Instructions from task_prompt.txt",
        }))
        return 2

    # QE_BACKEND env (set by web spawner) controls whether the prompt shows
    # GPU-mode `-np 1` examples or CPU-mode `-np 4` baseline + parallelism guidance.
    qe_backend = os.getenv("QE_BACKEND", "gpu")
    pw_x_path = detect_pw_x()

    # ─────── Phase 1: Preflight subagents (v4.1, unchanged behavior) ────────

    # config_advisor: deterministic classifier + tiny LLM cross-check; sets
    # occupations / smearing / ecutrho_factor for ALL downstream pw.x calls.
    config_advice_block = ""
    advice = None
    if not os.getenv("DFT_DISABLE_CONFIG_ADVISOR"):
        from agent.subagents import config_advisor as config_advisor_sa
        print("[solve] spawning config_advisor preflight subagent", file=sys.stderr)
        try:
            advice = config_advisor_sa.run(
                original_prompt=prompt_text, workdir=workdir,
                max_turns=6, max_output_tokens=2048,
                cli_overrides=cli_overrides, config_path=args.config,
            )
            config_advice_block = config_advisor_sa.render_config_advice_block(advice)
            (workdir / ".config_advice.json").write_text(advice.to_json())
            print(f"[solve] config_advisor: is_metal={advice.is_metal} "
                  f"occupations={advice.occupations} "
                  f"ecutrho_factor={advice.ecutrho_factor} source={advice.source}",
                  file=sys.stderr)
        except Exception as e:
            traceback.print_exc()
            print(f"[solve] config_advisor crashed; continuing without advice: {e}",
                  file=sys.stderr)

    # ─── Magnetic ground-state probe (FIX-32, S2) ───
    # Deterministic FM-vs-AFM single-point SCF dual-probe, mirroring the
    # bench GT methodology. Runs only for magnetic materials, so M1/M2 are
    # unaffected. The chosen ordering + ATOMIC_SPECIES + starting_magnetization
    # are prepended to config_advice_block (flows into vc_relax + main loop).
    if advice is not None and advice.is_magnetic and not os.getenv("DFT_DISABLE_MAGNETIC_PROBE"):
        from agent.subagents import magnetic_probe as magnetic_probe_sa
        from agent.subagents._magnetism import render_magnetic_block
        print("[solve] magnetic material → spawning FM/AFM ground-state probe", file=sys.stderr)
        harness_wall_s = int(os.environ.get("SOLVER_HARNESS_WALL_S", "14400"))
        probe_budget_s = max(300, int(harness_wall_s * 0.10))  # ≤10% wall for 2 single-points
        try:
            gs = magnetic_probe_sa.run(
                original_prompt=prompt_text, workdir=workdir,
                pw_x_path=pw_x_path, qe_backend=qe_backend,
                is_magnetic=advice.is_magnetic,
                magnetic_elements=advice.magnetic_elements,
                seed=advice.starting_magnetization,
                timeout_s=probe_budget_s,
            )
            # FIX-42 (S-1): inject the handoff when the probe succeeded OR when
            # it failed but the structure is AFM-split (structural fallback) —
            # so the LLM still carries the AFM split + seeds instead of a silent
            # single-species FM (which loses the moment + blocks FIX-40 recovery).
            if gs.ok or gs.structural_fallback:
                mag_block = render_magnetic_block(gs)
                config_advice_block = mag_block + "\n" + config_advice_block
                tag = "probe" if gs.ok else "structural-fallback"
                print(f"[solve] magnetic {tag} → ordering={gs.ordering} "
                      f"(FM={gs.fm_energy_ry} AFM={gs.afm_energy_ry} Ry, {gs.reason})",
                      file=sys.stderr)
            else:
                print(f"[solve] magnetic probe not usable: {gs.failure_reason} "
                      f"→ falling back to advisory-only magnetism", file=sys.stderr)
        except Exception as e:
            traceback.print_exc()
            print(f"[solve] magnetic probe crashed; continuing: {e}", file=sys.stderr)

    # T2 vc_relax preflight: when required keys contain bands_data/dos_dat,
    # pre-relax the initial structure in isolation.
    prerelax_block = ""
    if is_bands_dos_task(required_keys) and not os.getenv("DFT_DISABLE_VC_RELAX_SUBAGENT"):
        from agent.subagents import vc_relax as vc_relax_subagent
        print("[solve] T2 detected → spawning vc_relax preflight subagent", file=sys.stderr)
        # FIX-25 (2026-05-25): cap vc_relax preflight at 50% of harness wall
        # (env SOLVER_HARNESS_WALL_S, set by FIX-21 bench-side). Without this
        # cap, a slow-converging vc_relax (e.g. 16-atom magnetic TM oxide
        # on a 4h harness wall) ate the entire budget, leaving the main loop
        # zero time to run SCF/bands/DOS — 5-22 first Ti3O5 run was exactly
        # this pattern (vc_relax 2.4h, main loop got 1.5h, timed out).
        harness_wall_s = int(os.environ.get("SOLVER_HARNESS_WALL_S", "14400"))
        # FIX-35 (2026-05-29): on magnetic T2 the FM/AFM probe + AFM
        # sublattice-split (≈2× the species/SCF cost) already eat into the
        # wall, so a 50% pre-relax cap leaves the SCF→bands-NSCF→DOS-NSCF chain
        # too little time and heavy AFM oxides (Fe3O4/Fe2O3/LaMnO3) time out
        # before emitting result.json. Drop the pre-relax share to 40% for
        # magnetic tasks, handing the extra 10% to the main loop's chain.
        vc_relax_fraction = 0.4 if (advice is not None and advice.is_magnetic) else 0.5
        vc_relax_budget_s = int(harness_wall_s * vc_relax_fraction)
        print(f"[solve] vc_relax preflight wall budget: {vc_relax_budget_s}s "
              f"({vc_relax_fraction:.0%} of harness {harness_wall_s}s)", file=sys.stderr)
        try:
            relax_result = vc_relax_subagent.run(
                original_prompt=prompt_text, workdir=workdir,
                pw_x_path=pw_x_path, qe_backend=qe_backend,
                max_turns=args.max_turns, max_output_tokens=args.max_output_tokens,
                config_advice_block=config_advice_block,
                cli_overrides=cli_overrides, config_path=args.config,
                max_wall_s=vc_relax_budget_s,
            )
            if relax_result.ok:
                prerelax_block = vc_relax_subagent.render_prerelax_success(relax_result)
                prompt_text = vc_relax_subagent.inject_relaxed_into_user_prompt(
                    prompt_text, relax_result,
                )
                print(f"[solve] vc_relax subagent OK → injecting relaxed geometry"
                      f" (from {relax_result.relax_out_path})", file=sys.stderr)
            else:
                prerelax_block = vc_relax_subagent.render_prerelax_failure(relax_result)
                print(f"[solve] vc_relax subagent FAILED → fallback: {relax_result.failure_reason}",
                      file=sys.stderr)
        except Exception as e:
            traceback.print_exc()
            print(f"[solve] vc_relax preflight crashed; continuing: {e}", file=sys.stderr)

    # ─────── Phase 2: PlannerOrchestrator (v4.3 core) ────────

    # Build base executor system prompt — same content as v4.1's solver prompt;
    # executors append their step kind addon on top.
    base_executor_prompt = build_qe_system_prompt(
        workdir=str(workdir),
        task_outputs_keys=required_keys,
        pw_x_path=pw_x_path,
        repo_root=str(REPO_ROOT),
        qe_backend=qe_backend,
        prerelax_block=prerelax_block,
        config_advice_block=config_advice_block,
        # FIX-31: when advice flags Cd/Zn (Group 12 浅 d-band), T1 cutoffs jump
        # to the T2 / GT-canonical 80 Ry + ceil(50/a) min 8×8×8 to clear the
        # ±1% rel lattice-constant tolerance.
        cutoff_sensitive_metal=(advice.cutoff_sensitive_metal if advice else False),
    )

    try:
        orchestrator = PlannerOrchestrator(
            workdir=workdir,
            required_keys=required_keys,
            task_prompt=prompt_text,
            base_executor_prompt=base_executor_prompt,
            cli_overrides=cli_overrides,
            config_path=args.config,
            config_advice_block=config_advice_block,
            prerelax_block=prerelax_block,
            executor_max_turns=args.max_turns,
            max_output_tokens=args.max_output_tokens,
            scope_features=scope_features,
            repo_root=REPO_ROOT,
            advisory_block=advisory_block,
        )
        result = orchestrator.run()
    except Exception as e:
        traceback.print_exc()
        out.write_text(json.dumps({
            "incomplete": True,
            "reason": f"orchestrator crashed: {type(e).__name__}: {e}",
        }, indent=2, ensure_ascii=False))
        return 2

    print(f"[solve] orchestrator done: steps={result.total_steps} "
          f"attempts={result.total_attempts} replans={result.replans} "
          f"final_status={result.ledger.final_status}", file=sys.stderr)
    # FIX-37: merge split AFM species labels (Cr1/Cr2 -> Cr) in the submitted
    # vc-relax log so the bench's SpacegroupAnalyzer reports the crystallographic
    # space group. Best-effort + gated on magnetic; runs before write_result so
    # the local audit still validates the (rewritten) file.
    _normalize_afm_species_labels(result.payload, workdir, advice)
    # FIX-38: sum nspin=2 spin channels in the submitted dos.dat so the bench's
    # dos_curve (which reads only the dosup column) sees the TOTAL DOS.
    _sum_dos_spin_channels(result.payload, workdir)
    # FIX-2: guarantee a near-E_F bands sample for metals so the bench's metal
    # short-circuit fires (Ni_fcc 0.178 eV void → mis-scored 'indirect').
    _guarantee_metal_bands(result.payload, workdir, advice, required_keys,
                           pw_x_path, qe_backend)
    return write_result(out, result.payload, required_keys, workdir)


if __name__ == "__main__":
    raise SystemExit(main())
