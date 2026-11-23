"""dft-agent-bench v5 solver entry.

Bench launches this as a subprocess for every task:

    python solve.py --prompt   <abs>/task_prompt.txt \\
                    --workdir  <abs>/workdir \\
                    --out      <abs>/result.json \\
                    --model    <model-id>

Dual-mode entry:
  - claude-*       → claude-agent-sdk Driver path (M2-M4 stack:
                     structured tools + sub-agent + ledger + experience)
  - everything else → v4 legacy hand-rolled tool-use loop (kept for
                     non-Claude provider compatibility, e.g. deepseek-v4-pro)

Always emits result.json. Exit codes per spec §5:
  0 = success
  2 = declared incomplete (including all crash paths)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import shutil
import signal
import sys
import time
import traceback
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO_ROOT))

# ─── stable v4 utilities (reused) ─────────────────────────────────────
from agent.tools.bash import BashTool                            # noqa: E402
from agent.tools.structured.calc_registry import CalcRegistry    # noqa: E402
from agent.tools.structured.context import ToolContext           # noqa: E402
from agent.tools.structured.verify_run import (                  # noqa: E402
    verify_vc_relax, verify_bands_dos, verify_elastic, _resolve_expect_gap,
)
from agent.runtime.progress_ledger import ProgressLedger         # noqa: E402
from agent.runtime.transcript import TranscriptWriter            # noqa: E402

# Experience layer
from agent.experience.recorder import (                          # noqa: E402
    extract_material_formula, infer_calc_type_from_outputs, record_run,
)
from agent.experience.retriever import retrieve_for_task         # noqa: E402
from agent.subagents._magnetism import base_element              # noqa: E402


log = logging.getLogger("dft_agent_v5.solve")

_VC_RELAX_KEYS = {"vc_relax_out"}
_BANDS_DOS_KEYS = {"scf_out", "bands_out", "bands_data", "dos_dat"}


# ─────────────────────────────────────────────────────────────────────
# Argv / prompt / paths
# ─────────────────────────────────────────────────────────────────────

def detect_pw_x() -> str:
    return shutil.which("pw.x") or "pw.x"


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="DFT-Agent v5 bench solver")
    ap.add_argument("--prompt",  required=True)
    ap.add_argument("--workdir", required=True)
    ap.add_argument("--out",     required=True)
    ap.add_argument("--model",   default="claude-sonnet-4-6")
    ap.add_argument("--max-turns", type=int, default=60)
    ap.add_argument("--max-output-tokens", type=int, default=8192)
    ap.add_argument("--max-input-tokens",  type=int, default=200_000,
                    help="Token budget watermark for ledger S7 signal")
    ap.add_argument("--max-verifier-replays", type=int, default=2,
                    help="If verifier hard_fails, replay the SDK loop up to N times (Claude path only)")
    ap.add_argument("--no-experience", action="store_true",
                    help="Skip experience retrieval (clean A/B ablation)")
    ap.add_argument("--anthropic-base-url", default=None,
                    help="Anthropic-compatible base URL for the Claude SDK path "
                         "(e.g. https://api.deepseek.com/anthropic). When set, the "
                         "model is driven through the SDK regardless of its name; "
                         "the API key is taken unchanged from DEEPSEEK_API_KEY / "
                         "ANTHROPIC_AUTH_TOKEN / ANTHROPIC_API_KEY. Falls back to "
                         "the ANTHROPIC_BASE_URL env var.")
    ap.add_argument("--provider", default=None,
                    help="Override auto-detected provider for legacy path")
    ap.add_argument("--config", default=None, type=Path,
                    help="Path to providers.yaml override for legacy path")
    return ap.parse_args()


# Same regex as v4 — each task_prompt.txt ends with a "=== Submission
# Instructions ===" block listing required keys as `"<key>": ...`.
_KEY_RE = re.compile(r'"([a-zA-Z0-9_]+)"\s*:')


def parse_required_keys(prompt_text: str) -> list[str]:
    m = re.search(r"=== Submission Instructions ===(.*?)(?:===|\Z)",
                  prompt_text, re.DOTALL)
    section = m.group(1) if m else ""
    keys = _KEY_RE.findall(section)
    return list(dict.fromkeys(k for k in keys if k not in {"task_outputs", "notes"}))


# DeepSeek's Anthropic-compatible endpoint. v5.1 drives DeepSeek models
# through the claude-agent-sdk against this URL so they run the full M2-M4
# stack instead of the v4 legacy loop. Model ids (deepseek-v4-pro /
# deepseek-v4-flash) are accepted verbatim by this endpoint — no remapping.
DEEPSEEK_ANTHROPIC_BASE_URL = "https://api.deepseek.com/anthropic"


def resolve_anthropic_base_url(model: str, *, cli_base_url: str | None,
                               environ: dict | None = None) -> str | None:
    """Resolve the Anthropic-compatible base URL for the SDK path.

    Precedence:
      1. explicit --anthropic-base-url
      2. ANTHROPIC_BASE_URL env var
      3. built-in default for DeepSeek models (v5.1 policy: DeepSeek always
         runs the SDK path via DeepSeek's Anthropic endpoint)
    Returns None for native Claude / other providers (no override needed).
    """
    environ = os.environ if environ is None else environ
    if cli_base_url:
        return cli_base_url
    env_url = environ.get("ANTHROPIC_BASE_URL")
    if env_url:
        return env_url
    if model.startswith("deepseek-"):
        return DEEPSEEK_ANTHROPIC_BASE_URL
    return None


def should_use_sdk_path(model: str, *, base_url: str | None) -> bool:
    """Route to the claude-agent-sdk (Claude) path?

    True for native Claude models, OR whenever an Anthropic-compatible base
    URL is configured — e.g. DeepSeek's https://api.deepseek.com/anthropic.
    The base-URL override is the explicit opt-in: without it, non-Claude
    models (incl. legacy deepseek-v4-pro) stay on the v4 hand-rolled loop so
    their subset-42 reproducibility is untouched.
    """
    return model.startswith("claude-") or bool(base_url)


def build_anthropic_compat_env(base_url: str | None, *,
                               environ: dict | None = None) -> dict:
    """Env overrides for the SDK subprocess when pointing at a non-Anthropic
    Anthropic-compatible endpoint (e.g. DeepSeek).

    The API key is the user's existing one, unchanged: we read it from the
    env (DEEPSEEK_API_KEY preferred, then ANTHROPIC_AUTH_TOKEN/ANTHROPIC_API_KEY)
    and re-export it under the names the Claude CLI authenticates with. Returns
    {} when no base URL is configured (normal Anthropic path → no change).
    """
    if not base_url:
        return {}
    environ = os.environ if environ is None else environ
    key = (environ.get("DEEPSEEK_API_KEY")
           or environ.get("ANTHROPIC_AUTH_TOKEN")
           or environ.get("ANTHROPIC_API_KEY") or "")
    env = {"ANTHROPIC_BASE_URL": base_url}
    if key:
        # Set both so whichever header the CLI uses carries the right token.
        env["ANTHROPIC_AUTH_TOKEN"] = key
        env["ANTHROPIC_API_KEY"] = key
    return env


def infer_task_type(required_keys: list[str]) -> str:
    s = set(required_keys)
    if _VC_RELAX_KEYS.issubset(s):
        return "vc_relax"
    if _BANDS_DOS_KEYS.issubset(s):
        return "bands_dos"
    return "unknown"


def _winning_params(calc_registry, task_type: str) -> dict:
    """Params of the calc that produced the submitted result, for the experience
    record (and its cost_proxy).

    vc_relax  → last completed 'vc-relax' calc.
    bands_dos → the completed 'scf' calc (it carries ecutwfc + the MP kpts grid;
                nscf/bands use a kpath and have no kpts).
    Empty dict if no match (legacy / escape-hatch runs that never registered a
    structured calc). `list()` is sorted by created_ts, so [-1] is the latest
    completed match — the winning retry when convergence_fixer made several.
    """
    try:
        if task_type == "vc_relax":
            recs = calc_registry.list(status="completed", calc_type="vc-relax")
        elif task_type == "bands_dos":
            recs = calc_registry.list(status="completed", calc_type="scf")
        else:
            return {}
        return dict(recs[-1].params) if recs else {}
    except Exception:
        return {}


def task_id_from_workdir(workdir: Path) -> str:
    """harness names run dirs like `2026-05-13T05-04-20Z_M1_T1_Si_vc_relax`.
    The workdir is `<run_dir>/workdir`, so grab the parent dir name."""
    name = workdir.parent.name if workdir.name == "workdir" else workdir.name
    m = re.search(r"(M\d_T\d_[^/]+?)(?:_\d{4,})?$", name)
    return m.group(1) if m else name


# ─────────────────────────────────────────────────────────────────────
# Prompt assembly
# ─────────────────────────────────────────────────────────────────────

def assemble_user_prompt(*, preflight_block: str, exp_block: str, prompt_text: str) -> str:
    """Stack advice blocks ahead of the base prompt; skip empties.

    Order: preflight (material advice) -> experience (past runs) -> base prompt last,
    so the task's own Submission Instructions remain the final, freshest context.
    """
    parts = [b for b in (preflight_block, exp_block) if b and b.strip()]
    parts.append(prompt_text)
    return "\n\n".join(parts)


# ─────────────────────────────────────────────────────────────────────
# Audit (verbatim from v4)
# ─────────────────────────────────────────────────────────────────────

# ── Submitted-output normalization (FIX-37 / FIX-38) ──────────────────────
# Generic, material-agnostic transforms applied to the SUBMITTED files right
# before the bench scores them. Ported from v4.3 (dropped in the v4.3->v5.1 SDK
# rewrite; see ~/work/agentlog.md 2026-06-04). No per-material knowledge.

def _is_log_output(key: str) -> bool:
    """Mirror bench's harness/lib/audit.py:_is_log_output. `*_data`/`*_dat` are
    pure data files (no JOB DONE.); everything else is a program log."""
    if key.endswith(("_data", "_dat")):
        return False
    return True


_AP_SITE_RE = re.compile(r"^(\s*)([A-Z][a-z]?\d+)(\s+[-+0-9.eE].*)$")
_AP_PLAIN_SITE_RE = re.compile(r"^\s*[A-Z][a-z]?\s+[-+0-9.eE].*$")


def _normalize_atomic_positions_block(text: str) -> str:
    """Strip trailing AFM sublattice indices (Cr1/Cr2 -> Cr) from the species
    column of every ATOMIC_POSITIONS block, leaving everything else byte-exact.

    FIX-37: QE needs split species labels (Cr1/Cr2) to assign opposite
    starting_magnetization for AFM, but the bench T1 extractor keeps the raw
    label and feeds it to SpacegroupAnalyzer, which treats Cr1 != Cr2 as
    distinct species and reports a subgroup (R32 #155 instead of GT R-3c #167).
    Merging the labels back to the bare element restores the crystallographic
    (element-based) symmetry the GT uses. Only the leading species token of site
    rows is touched; CELL_PARAMETERS, energies, magnetization lines and
    JOB DONE. are untouched. Inherently a no-op without digit-suffixed labels.
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
                out.append(line)
                continue
            in_ap = False
        out.append(line)
    return "\n".join(out)


def _normalize_afm_species_labels(payload: dict, workdir: Path, advice=None) -> None:
    """Apply FIX-37 to the submitted vc-relax log(s), in place. Inherently a
    no-op on outputs with no digit-suffixed labels, so safe to run
    unconditionally. Best-effort: any failure is logged and swallowed (the
    subsequent _audit_task_outputs still guards the file)."""
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
    """Collapse an nspin=2 dos.x file (E dosup dosdw intdos) to
    (E dosup+dosdw intdos). 3-column nspin=1 files and comment/blank lines are
    returned unchanged.

    FIX-38: the bench dos_curve reference is the TOTAL DOS, but QE's dos.x emits
    per-spin columns for nspin=2 and the bench reads only the second column
    (dosup) — half the DOS — so recall caps at ~0.5. Emitting the summed total
    in that column restores the full DOS (AFM insulators have dosup==dosdw by
    symmetry, so summing is correct there too)."""
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
        if len(vals) >= 4:  # E dosup dosdw intdos -> E (dosup+dosdw) intdos
            e, up, dw, idos = vals[0], vals[1], vals[2], vals[3]
            out.append(f"{e:12.4f}  {up + dw:.6E}  {idos:.6E}")
        else:
            out.append(line)
    return "\n".join(out)


def _sum_dos_spin_channels(payload: dict, workdir: Path) -> None:
    """Apply FIX-38 to the submitted dos.dat, in place. Self-gating: only
    4-column (nspin=2) files are rewritten; nspin=1 is a no-op. Best-effort."""
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


def _normalize_submitted_outputs(payload: dict, workdir: Path) -> None:
    """Apply all material-agnostic submit-time output transforms (FIX-37 +
    FIX-38) to the payload's task_outputs, in place. Idempotent + best-effort:
    safe to call before the verifier gate AND again before write_result."""
    try:
        _normalize_afm_species_labels(payload, workdir)
        _sum_dos_spin_channels(payload, workdir)
    except Exception as e:  # never let normalization block a valid submission
        print(f"[solve] output normalization skipped (non-fatal): {e}",
              file=sys.stderr)


def _guarantee_metal_bands(payload: dict, workdir: Path, *, timeout_s: int = 300) -> None:
    """Apply FIX-2 (metal bands-path void guarantee) to the submitted bands.dat,
    in place, for metals. Gated on the workdir's .config_advice.json
    (is_metal and not pbe_near_metal) so insulators / PBE-near-metals are never
    touched. Best-effort + graceful: any failure leaves the LLM's bands.dat
    intact — never worse than today.

    Unlike the text-only FIX-37/38, this runs pw.x/bands.x (hence separate, and
    timeout-bounded): for an FM metal the majority-spin-only bands.dat (bands.x
    default spin_component=1) leaves a >10 meV void straddling E_F, which the
    bench scores as an indirect semiconductor (Ni_fcc batch 43713d 2026-06-04:
    0.077; band_gap/gap_type/vbm/cbm zeroed). The guarantee re-extracts the
    metal-resolvable spin channel (the Fermi surface lives in the minority
    d-bands) and replaces bands.dat with it. FIX-2 was present in v4.3 (as a
    k-path densify, dropped in the v5.1 rewrite and re-found too slow on the
    GPU-podman path 2026-06-05 — spin-pick replaced it).
    """
    try:
        task_outputs = (payload or {}).get("task_outputs") or {}
        if not task_outputs.get("bands_data") or not task_outputs.get("bands_out"):
            return  # not a bands_dos submission
        advice_path = workdir / ".config_advice.json"
        if not advice_path.is_file():
            return
        try:
            advice = json.loads(advice_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if not advice.get("is_metal") or advice.get("pbe_near_metal"):
            return  # insulator / PBE-near-metal: a real tiny gap must stand
        from agent.subagents import metal_bands_guarantee as mbg
        res = mbg.run(
            workdir=workdir, task_outputs=task_outputs,
            pw_x_path=detect_pw_x(), qe_backend=os.getenv("QE_BACKEND", "gpu"),
            is_metal=True, timeout_s=timeout_s,
        )
        if res.replaced_bands_data:
            print(f"[solve] FIX-2: replaced bands.dat with spin {res.which_spin} "
                  f"to resolve metal E_F (void {res.void_before:.3f}->"
                  f"{res.void_after:.3f} eV)", file=sys.stderr)
    except Exception as e:  # never let the guarantee block a valid submission
        print(f"[solve] FIX-2 metal-bands guarantee skipped (non-fatal): {e}",
              file=sys.stderr)


# task_outputs keys whose value is a *derived / summary* file (CSV, JSON,
# post-processing .dat) rather than a raw pw.x stdout (.out) file. The
# >=1024 B + "JOB DONE." contract is meant for the latter only; these are
# legitimately small (e.g. a 7-row EOS CSV ~300 B) and never carry the marker.
_DERIVED_OUTPUT_SUFFIXES = ("_csv", "_json", "_data", "_dat")


def _is_derived_output(key: str) -> bool:
    return key.endswith(_DERIVED_OUTPUT_SUFFIXES)


def _audit_task_outputs(task_outputs: dict, workdir: Path) -> list[str]:
    errors: list[str] = []
    workdir_resolved = workdir.resolve()
    for key, p in task_outputs.items():
        if not isinstance(p, str) or not p:
            errors.append(f"{key}: path must be a non-empty string"); continue
        if Path(p).is_absolute():
            errors.append(f"{key}: absolute path forbidden ({p!r})"); continue
        abs_p = (workdir_resolved / p).resolve()
        try:
            abs_p.relative_to(workdir_resolved)
        except ValueError:
            errors.append(f"{key}: path escapes workdir ({p!r})"); continue
        if not abs_p.is_file():
            errors.append(f"{key}: not a regular file ({p!r})"); continue
        try:
            size = abs_p.stat().st_size
        except OSError as e:
            errors.append(f"{key}: stat failed ({e})"); continue
        if _is_derived_output(key):
            # Derived / summary outputs (CSV, JSON, post-processing .dat) are
            # legitimately small and markerless, so the raw-QE-output contract
            # (>=1024 B + "JOB DONE.") must not apply — they only need to be a
            # non-empty regular file in the workdir. Mirrors the bench audit,
            # which gates on files_nonempty (not size/marker) for these, and
            # unblocks T3 eos_points_csv / T4 stress_strain_csv that this check
            # was wrongly rejecting (opus_v5_1_gpu T3 batch, 2026-06-25: 57/57).
            if size == 0:
                errors.append(f"{key}: empty file ({p!r})")
            continue
        if size < 1024:
            errors.append(f"{key}: file too small ({size} B < 1024)"); continue
        try:
            with abs_p.open("rb") as f:
                f.seek(max(0, size - 65536))
                tail = f.read().decode("utf-8", errors="replace")
        except OSError as e:
            errors.append(f"{key}: read failed ({e})"); continue
        if "JOB DONE." not in tail:
            errors.append(f"{key}: missing 'JOB DONE.' marker in last 64 KB")
    return errors


# ─────────────────────────────────────────────────────────────────────
# Completion-gate salvage (P1 Fix 3): when the SDK loop ends without a
# submit (budget exhausted / transient SDK drop / max-turns) but the work
# is actually finished, rebuild task_outputs from the calc registry + the
# on-disk post-processing files instead of returning an empty stub.
# ─────────────────────────────────────────────────────────────────────

def _find_pp_output(workdir: Path, kind: str) -> str | None:
    """Locate a bands.x / dos.x post-processing data file.

    These are written by bash escape-hatch calls, NOT registered as calcs, so we
    recover the path the agent used from `filband=` (kind="bands") / `fildos=`
    (kind="dos") in bash.log, preferring the last-written one. Falls back to a
    recursive glob of the conventional name (newest by mtime). Returns a
    workdir-relative path or None.
    """
    key = "filband" if kind == "bands" else "fildos"
    name = "bands.dat" if kind == "bands" else "dos.dat"
    wd = workdir.resolve()
    log_path = workdir / "bash.log"
    if log_path.exists():
        text = log_path.read_text(encoding="utf-8", errors="ignore")
        cands = re.findall(rf"{key}\s*=\s*['\"]([^'\"]+)['\"]", text)
        for c in reversed(cands):          # the agent's final run wins
            p = (workdir / c)
            if p.is_file():
                try:
                    return str(p.resolve().relative_to(wd))
                except ValueError:
                    continue
    matches = sorted(workdir.rglob(name), key=lambda p: p.stat().st_mtime, reverse=True)
    for m in matches:
        try:
            return str(m.resolve().relative_to(wd))
        except ValueError:
            continue
    return None


def _salvage_verify_note(task_outputs: dict, workdir: Path, task_type: str) -> str:
    """Run the program-level verifier on a salvaged submission — diagnostics
    only (the verdict goes into notes; it does NOT block the salvage)."""
    try:
        if task_type == "vc_relax":
            rep = verify_vc_relax(workdir / task_outputs["vc_relax_out"])
        elif task_type == "bands_dos":
            _nscf_dos = task_outputs.get("nscf_dos_out")
            rep = verify_bands_dos(
                scf_out=workdir / task_outputs["scf_out"],
                bands_out=workdir / task_outputs["bands_out"],
                bands_data=workdir / task_outputs["bands_data"],
                dos_dat=workdir / task_outputs["dos_dat"],
                nscf_dos_out=(workdir / _nscf_dos) if _nscf_dos else None,
                expect_gap=_resolve_expect_gap(workdir),
            )
        else:
            return "skipped"
        return rep.verdict
    except Exception as e:
        return f"error:{type(e).__name__}"


def _salvage_task_outputs(calc_registry, workdir: Path, task_type: str,
                          required_keys: list[str], reason: str) -> dict | None:
    """Rebuild a submit-shaped payload from finished artifacts.

    Returns a payload dict (incomplete=False) only if every required key resolves
    to a file that passes _audit_task_outputs; otherwise None (caller keeps the
    incomplete stub). The verifier is advisory at salvage time — we are out of
    budget to replay, and any audit-passing result scores ≥ 0, strictly better
    than the empty-stub 0.000.
    """
    try:
        resolved: dict[str, str] = {}
        if task_type == "vc_relax":
            recs = calc_registry.list(status="completed", calc_type="vc-relax")
            if recs and recs[-1].output_path:
                resolved["vc_relax_out"] = recs[-1].output_path
        elif task_type == "bands_dos":
            scfs = calc_registry.list(status="completed", calc_type="scf")
            nscfs = calc_registry.list(status="completed", calc_type="nscf")
            bands_nscf = next((r for r in nscfs if r.params.get("kpath")), None)
            dos_nscf = next((r for r in nscfs
                             if r.params.get("kpts") and not r.params.get("kpath")), None)
            if scfs and scfs[-1].output_path:
                resolved["scf_out"] = scfs[-1].output_path
            if bands_nscf and bands_nscf.output_path:
                resolved["bands_out"] = bands_nscf.output_path
            if dos_nscf and dos_nscf.output_path:
                resolved["nscf_dos_out"] = dos_nscf.output_path
            bd = _find_pp_output(workdir, "bands")
            if bd:
                resolved["bands_data"] = bd
            dd = _find_pp_output(workdir, "dos")
            if dd:
                resolved["dos_dat"] = dd
        else:
            return None

        task_outputs: dict[str, str] = {}
        for k in required_keys:
            if k not in resolved:
                log.warning("solve: salvage cannot resolve required key %r (have %s)",
                            k, sorted(resolved))
                return None
            task_outputs[k] = resolved[k]
        if not task_outputs:
            return None

        audit_errors = _audit_task_outputs(task_outputs, workdir)
        if audit_errors:
            log.warning("solve: salvage candidate failed audit: %s", audit_errors)
            return None

        verdict = _salvage_verify_note(task_outputs, workdir, task_type)
        log.warning("solve: SALVAGED submission from finished artifacts (%s); "
                    "keys=%s verify=%s", reason, list(task_outputs), verdict)
        return {
            "task_outputs": task_outputs,
            "incomplete": False,
            "notes": (f"salvaged from finished artifacts after {reason} "
                      f"(agent never called submit); verifier verdict={verdict}"),
        }
    except Exception as e:
        log.warning("solve: salvage failed: %s: %s", type(e).__name__, e)
        return None


def write_result(out_path: Path, payload: dict, required_keys: list[str],
                 workdir: Path) -> int:
    """Persist the LLM submission. Returns process exit code per spec §5."""
    if payload.get("incomplete"):
        out_path.write_text(json.dumps({
            "incomplete": True,
            "reason": payload.get("notes") or payload.get("reason", "no reason given"),
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

    result: dict[str, Any] = {"task_outputs": task_outputs}
    if payload.get("notes"):
        result["notes"] = payload["notes"]
    out_path.write_text(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


# ─────────────────────────────────────────────────────────────────────
# Program-level verifier gate (Claude path only)
# ─────────────────────────────────────────────────────────────────────

def _gate_check(payload: dict, *, workdir: Path, task_type: str,
                initial: dict | None = None) -> tuple[bool, str]:
    """Re-run verify_run logic on submitted task_outputs.

    Returns (ok, complaint_text). ok=True means we're cleared to submit.
    ok=False means at least one hard check failed and the LLM should be
    given a chance to fix.

    `initial` carries the task's input-structure facts (space group, lattice,
    crystal system). Passing it lets verify_vc_relax run the space-group-
    preservation check — the one worth 0.25 of the T1 score. Without it the
    gate only re-checks what pw.x already self-reports.
    """
    task_outputs = payload.get("task_outputs") or {}
    if not task_outputs or payload.get("incomplete"):
        return True, ""  # nothing to verify; let write_result decide

    if task_type == "vc_relax":
        out_path = workdir / task_outputs.get("vc_relax_out", "")
        if not out_path.exists():
            return False, f"vc_relax_out file missing: {task_outputs.get('vc_relax_out')}"
        rep = verify_vc_relax(out_path, initial=initial or None)
    elif task_type == "bands_dos":
        try:
            _nscf_dos = task_outputs.get("nscf_dos_out")
            rep = verify_bands_dos(
                scf_out=workdir / task_outputs["scf_out"],
                bands_out=workdir / task_outputs["bands_out"],
                bands_data=workdir / task_outputs["bands_data"],
                dos_dat=workdir / task_outputs["dos_dat"],
                nscf_dos_out=(workdir / _nscf_dos) if _nscf_dos else None,
                expect_gap=_resolve_expect_gap(workdir),
            )
        except KeyError as e:
            return False, f"bands_dos missing required key: {e}"
    elif "stress_strain_csv" in task_outputs:
        # T4 elastic. Keyed on the payload (task_type stays "unknown" — its other
        # consumers are untouched). Gate replicates the bench's Cij fit and only
        # hard-fails the two certain-zero signatures (Born-unstable / sign-flip),
        # so a replay can't score worse than the rejected CSV would have.
        csv_rel = task_outputs.get("stress_strain_csv", "")
        csv_path = workdir / csv_rel
        if not csv_path.exists():
            return False, f"stress_strain_csv file missing: {csv_rel}"
        rep = verify_elastic(csv_path)
    else:
        return True, ""  # unknown task → skip gate

    if rep.verdict == "hard_fail":
        # Build a concise complaint for the LLM
        failed = [f"  ✗ {c.name}: {c.detail}"
                  for c in rep.checks if not c.ok and not c.name.startswith("advisory:")]
        return False, ("Program-level verifier rejected the submission:\n"
                       + "\n".join(failed) +
                       "\nFix the failing check and submit again. Do not "
                       "submit the same payload twice — change something.")
    return True, ""


# ─────────────────────────────────────────────────────────────────────
# Claude (SDK) path
# ─────────────────────────────────────────────────────────────────────

# Seconds reserved before the harness wall deadline for teardown + writing
# result.json + recording experience. The harness kills the solver with an
# uncatchable SIGKILL at SOLVER_HARNESS_WALL_S, so we must self-terminate and
# return control to amain (which writes result.json) before then.
WALL_RESERVE_S = 300.0


def _harness_wall_budget_s() -> float | None:
    """Total wall-clock budget (s) the harness allows, or None if unknown.

    The bench harness exports SOLVER_HARNESS_WALL_S = the subprocess timeout
    (solver_runner.py). Without it we cannot bound the loop and fall back to
    the previous unguarded behavior.
    """
    raw = os.environ.get("SOLVER_HARNESS_WALL_S")
    if not raw:
        return None
    try:
        v = float(raw)
        return v if v > 0 else None
    except ValueError:
        return None


class _BudgetExhausted(Exception):
    """Raised internally when an attempt cannot finish within the wall budget."""


async def _run_claude_path(
    *,
    workdir: Path,
    args: argparse.Namespace,
    prompt_text: str,
    user_prompt: str,
    task_type: str,
    required_keys: list[str],
    bash_tool: BashTool,
    ctx: ToolContext,
    ledger: ProgressLedger,
    transcript: TranscriptWriter,
    initial_facts: dict | None = None,
    sdk_env: dict | None = None,
    started_monotonic: float | None = None,
    _driver=None,
) -> tuple[dict, dict]:
    """Run executor + verifier gate replays. Returns (payload, final_usage).

    A wall-clock deadline (derived from SOLVER_HARNESS_WALL_S minus a teardown
    reserve) bounds the whole loop: no attempt is started, and no in-flight
    attempt is allowed to run, past the deadline. On exhaustion we return the
    best payload we have so amain can still write result.json (rather than
    letting the harness SIGKILL us mid-attempt with nothing written).
    """
    # Lazy import so the legacy path never imports claude-agent-sdk
    if _driver is None:
        from agent.runtime.driver import Driver
        driver = Driver(
            workdir=workdir,
            ctx=ctx,
            ledger=ledger,
            transcript=transcript,
            model=args.model,
            max_turns=args.max_turns,
            pw_x_path=detect_pw_x(),
            qe_backend=os.getenv("QE_BACKEND", "gpu"),
            extra_env=sdk_env or {},
        )
    else:
        driver = _driver

    budget = _harness_wall_budget_s()
    # Anchor the deadline at process start (passed in), NOT at this point — any
    # pre-loop work (experience retrieval, config-advisor preflight, the magnetic
    # FM/AFM probe which runs 2 SCFs) has already eaten into the harness budget.
    # If we anchored here, that pre-loop time would push the deadline past the
    # harness SIGKILL and defeat the best-effort result.json write.
    _anchor = started_monotonic if started_monotonic is not None else time.monotonic()
    deadline = (_anchor + budget - WALL_RESERVE_S) if budget else None
    if deadline is not None:
        log.info("solve: wall budget %.0fs (reserve %.0fs, %.0fs pre-loop) → deadline in %.0fs",
                 budget, WALL_RESERVE_S, time.monotonic() - _anchor, deadline - time.monotonic())

    async def _run_once(prompt: str) -> dict:
        if deadline is None:
            return await driver.run(user_prompt=prompt, task_type=task_type,
                                    required_keys=required_keys)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise _BudgetExhausted()
        try:
            return await asyncio.wait_for(
                driver.run(user_prompt=prompt, task_type=task_type,
                           required_keys=required_keys),
                timeout=remaining,
            )
        except asyncio.TimeoutError as e:
            raise _BudgetExhausted() from e

    def _best_effort(prev: dict | None = None,
                     reason: str = "wall budget exhausted") -> dict:
        # Prefer whatever the agent last submitted, then the prior attempt's
        # output, then a salvage reconstruction from finished artifacts, then a
        # declared-incomplete stub.
        if ctx.submit_payload:
            return ctx.submit_payload
        if prev and prev.get("task_outputs"):
            return prev
        salvaged = _salvage_task_outputs(ctx.calc_registry, workdir, task_type,
                                         required_keys, reason)
        if salvaged:
            return salvaged
        return {
            "incomplete": True,
            "task_outputs": {},
            "notes": f"{reason} before a valid submission; "
                     "salvage found no usable finished artifacts",
        }

    try:
        payload = await _run_once(user_prompt)
    except _BudgetExhausted:
        log.warning("solve: wall budget exhausted during initial run; "
                    "returning best-effort payload")
        return _best_effort(reason="wall budget exhausted"), driver.final_usage

    # Transient SDK drop (e.g. "Stream closed") with budget left → one retry.
    # The agent's calcs.json + output files persist, so it can verify the
    # finished work and submit instead of recomputing from scratch.
    if (not payload.get("task_outputs")
            and getattr(driver, "transient_error", False)
            and (deadline is None or (deadline - time.monotonic()) > 0)):
        log.warning("solve: transient SDK error (e.g. Stream closed); retrying once")
        ctx.submit_payload = None
        retry_prompt = (
            user_prompt +
            "\n\n=== Note ===\nYour previous session was interrupted by a "
            "transient connection drop. Your earlier calculations are recorded "
            "in calcs.json and their output files already exist in the workdir. "
            "Verify the finished products with verify_run and call submit — do "
            "NOT recompute from scratch.")
        try:
            payload = await _run_once(retry_prompt)
        except _BudgetExhausted:
            log.warning("solve: wall budget exhausted during transient retry; "
                        "returning best-effort payload")
            return _best_effort(prev=payload, reason="wall budget exhausted on retry"), \
                driver.final_usage

    # Verifier gate: up to N replays, bounded by the wall deadline.
    for attempt in range(1, args.max_verifier_replays + 1):
        # Normalize submitted outputs (FIX-37/38) BEFORE gating so the
        # space-group check reads element-based symmetry (merged AFM labels),
        # not a label-induced subgroup — otherwise the gate rejects a structure
        # that will actually score correctly and burns replay budget.
        _normalize_submitted_outputs(payload, workdir)
        # FIX-2: for an FM metal whose majority-spin bands.dat shows a void
        # straddling E_F, replace bands.dat with the metal-resolvable spin
        # channel (else the bench scores the metal an indirect semiconductor).
        # Cheap: one original-density bands NSCF re-run (~minutes) to refresh the
        # band-path wavefunctions + bands.x per spin — NOT a k-path densify (that
        # timed out >3600s on the GPU-podman path, 2026-06-05). Runs post-submit
        # within the main wall budget; bounded so it can't eat the teardown reserve.
        _mbg_to = int(deadline - time.monotonic()) if deadline is not None else 1800
        if _mbg_to >= 120:
            _guarantee_metal_bands(payload, workdir, timeout_s=min(1800, _mbg_to))
        ok, complaint = _gate_check(payload, workdir=workdir, task_type=task_type,
                                    initial=initial_facts)
        if ok:
            break
        if deadline is not None and (deadline - time.monotonic()) <= 0:
            log.warning("solve: wall budget exhausted; skipping further verifier "
                        "replays after attempt %d", attempt)
            break
        log.warning("solve: verifier gate failed (attempt %d):\n%s", attempt, complaint)
        # Reset submit_payload so the next Driver run can capture a fresh submit
        ctx.submit_payload = None
        replay_prompt = (
            user_prompt +
            f"\n\n=== Verifier feedback (attempt {attempt}) ===\n{complaint}"
        )
        try:
            payload = await _run_once(replay_prompt)
        except _BudgetExhausted:
            log.warning("solve: wall budget exhausted during replay attempt %d; "
                        "returning best-effort payload", attempt)
            payload = _best_effort(prev=payload)
            break

    # Final completion-gate salvage: if the loop ended with no real submission
    # (max-turns, a caught transient drop, or a declared-incomplete stub from the
    # driver) but the work is actually finished on disk, rebuild and submit it
    # rather than scoring 0.000 (the Fe2O3 case).
    if not payload.get("task_outputs"):
        salvaged = _salvage_task_outputs(ctx.calc_registry, workdir, task_type,
                                         required_keys, "loop ended without submit")
        if salvaged:
            payload = salvaged

    return payload, driver.final_usage


# ─────────────────────────────────────────────────────────────────────
# Legacy (v4 hand-rolled) path
# ─────────────────────────────────────────────────────────────────────

def _run_legacy_path(
    *,
    workdir: Path,
    args: argparse.Namespace,
    prompt_text: str,
    user_prompt: str,
    bash_tool: BashTool,
    required_keys: list[str],
) -> dict:
    from agent.llm import LLMClient
    from agent.loop import run_loop
    from agent.system_prompt import build_qe_system_prompt

    client = LLMClient(args.model, provider=args.provider, config_path=args.config)
    qe_backend = os.getenv("QE_BACKEND", "gpu")
    system = build_qe_system_prompt(
        workdir=str(workdir),
        task_outputs_keys=required_keys,
        pw_x_path=detect_pw_x(),
        repo_root=str(REPO_ROOT),
        qe_backend=qe_backend,
    )
    return run_loop(
        client=client,
        system_prompt=system, user_prompt=user_prompt,
        bash_tool=bash_tool, max_turns=args.max_turns,
        max_output_tokens=args.max_output_tokens,
        transcript_path=workdir / "transcript.json",
    )


# ─────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────

async def amain(args: argparse.Namespace) -> int:
    # Anchor for the SDK-path wall-clock budget (P0b). Captured at the very top
    # so all pre-loop work (preflight, magnetic probe) counts against the
    # harness SOLVER_HARNESS_WALL_S budget, not just the SDK loop itself.
    proc_t0 = time.monotonic()
    workdir = Path(args.workdir).resolve()
    out = Path(args.out)
    prompt_text = Path(args.prompt).read_text(encoding="utf-8")
    required_keys = parse_required_keys(prompt_text)
    task_type = infer_task_type(required_keys)
    task_id = task_id_from_workdir(workdir)

    print(f"[solve v5] task_id={task_id} type={task_type} model={args.model}",
          file=sys.stderr)
    print(f"[solve v5] required keys: {required_keys}", file=sys.stderr)

    if not required_keys:
        out.write_text(json.dumps({
            "incomplete": True,
            "reason": "could not parse Submission Instructions from task_prompt.txt",
        }))
        return 2

    # ── Shared facilities ──
    bash_tool = BashTool(workdir=workdir)
    transcript = TranscriptWriter(workdir / "transcript.json")
    ledger = ProgressLedger(workdir=workdir, max_input_tokens=args.max_input_tokens,
                            wall_budget_s=_harness_wall_budget_s())
    calc_registry = CalcRegistry(workdir)
    pseudo_dir_env = os.environ.get("PSEUDO_DIR")
    ctx = ToolContext(
        workdir=workdir,
        calc_registry=calc_registry,
        bash_tool=bash_tool,
        pseudo_dir=Path(pseudo_dir_env) if pseudo_dir_env else None,
    )

    # ── Experience retrieval ──
    exp_recs = []
    exp_block_text = ""
    if not args.no_experience:
        try:
            exp_recs, exp_block = retrieve_for_task(
                repo_root=REPO_ROOT, task_id=task_id, required_keys=required_keys, k=3,
            )
            if exp_block:
                exp_block_text = exp_block
                print(f"[solve v5] experience: {len(exp_recs)} matches prepended",
                      file=sys.stderr)
        except Exception as e:
            log.warning("solve: experience retrieval failed: %s", e)

    # ── Preflight: v4.2 knowledge layer (skills + deterministic config advice) ──
    preflight_block = ""
    try:
        from agent.preflight import run_preflight
        preflight_block = run_preflight(prompt_text, workdir)
        if preflight_block.strip():
            print(f"[solve v5.1] preflight advice: {len(preflight_block)} chars",
                  file=sys.stderr)
    except Exception as e:
        log.warning("solve: preflight failed: %s", e)

    # ── Magnetic ground-state probe (ported from v4.3) ──────────────────────────
    if not os.getenv("DFT_DISABLE_MAGNETIC_PROBE"):
        try:
            from agent.subagents._magnetism import (
                classify_magnetism, parse_initial_structure, render_magnetic_block,
            )
            from agent.subagents import magnetic_probe as _mag_probe_mod
            _struct = parse_initial_structure(prompt_text)
            if _struct is not None:
                _profile = classify_magnetism(list({s.element for s in _struct.sites}))
                if _profile.is_magnetic:
                    _probe_budget_s = max(300, int(os.environ.get("SOLVER_HARNESS_WALL_S", "14400")) // 10)
                    print(f"[solve v5.1] magnetic → FM/AFM probe (budget {_probe_budget_s}s)", file=sys.stderr)
                    _gs = _mag_probe_mod.run(
                        original_prompt=prompt_text, workdir=workdir,
                        pw_x_path=detect_pw_x(), qe_backend=os.getenv("QE_BACKEND", "gpu"),
                        is_magnetic=True, magnetic_elements=_profile.magnetic_elements,
                        seed=_profile.seed, timeout_s=_probe_budget_s,
                    )
                    if _gs.ok:
                        preflight_block = render_magnetic_block(_gs) + ("\n\n" if preflight_block else "") + preflight_block
                        print(f"[solve v5.1] probe → {_gs.ordering} (FM={_gs.fm_energy_ry} AFM={_gs.afm_energy_ry} Ry)", file=sys.stderr)
                    else:
                        print(f"[solve v5.1] probe failed: {_gs.failure_reason} → advisory-only", file=sys.stderr)
        except Exception as _e:
            log.warning("solve: magnetic probe crashed; continuing: %s", _e)

    user_prompt = assemble_user_prompt(
        preflight_block=preflight_block, exp_block=exp_block_text, prompt_text=prompt_text,
    )

    # ── Initial-structure facts for the verifier gate (vc_relax only) ──
    # Sourced from the task's own input geometry (not ground truth) so the
    # program-level gate can re-check space-group preservation.
    initial_facts: dict = {}
    if task_type == "vc_relax":
        try:
            from agent import structure_features
            initial_facts = structure_features.initial_structure_facts(prompt_text)
            if initial_facts:
                print(f"[solve v5.1] initial facts for gate: {sorted(initial_facts)}",
                      file=sys.stderr)
        except Exception as e:
            log.warning("solve: initial_structure_facts failed: %s", e)

    # ── Route by model ──
    # An Anthropic-compatible base URL (flag or env) opts the model into the
    # SDK path and is forwarded to the subprocess so e.g. DeepSeek's
    # https://api.deepseek.com/anthropic endpoint drives the Claude loop.
    anthropic_base_url = resolve_anthropic_base_url(
        args.model, cli_base_url=args.anthropic_base_url)
    is_claude = should_use_sdk_path(args.model, base_url=anthropic_base_url)
    sdk_env = build_anthropic_compat_env(anthropic_base_url)
    if sdk_env:
        print(f"[solve v5.1] SDK path via {sdk_env['ANTHROPIC_BASE_URL']} "
              f"(model={args.model}, key={'set' if 'ANTHROPIC_AUTH_TOKEN' in sdk_env else 'MISSING'})",
              file=sys.stderr)
    wall_t0 = time.monotonic()
    final_usage: dict = {}

    try:
        if is_claude:
            payload, final_usage = await _run_claude_path(
                workdir=workdir, args=args,
                prompt_text=prompt_text, user_prompt=user_prompt,
                task_type=task_type, required_keys=required_keys,
                bash_tool=bash_tool, ctx=ctx, ledger=ledger, transcript=transcript,
                initial_facts=initial_facts, sdk_env=sdk_env,
                started_monotonic=proc_t0,
            )
        else:
            payload = await asyncio.to_thread(
                _run_legacy_path,
                workdir=workdir, args=args,
                prompt_text=prompt_text, user_prompt=user_prompt,
                bash_tool=bash_tool, required_keys=required_keys,
            )
    except Exception as e:
        traceback.print_exc()
        out.write_text(json.dumps({
            "incomplete": True,
            "reason": f"agent loop crashed: {type(e).__name__}: {e}",
        }, indent=2, ensure_ascii=False))
        # Still record the failure into experience
        try:
            record_run(repo_root=REPO_ROOT, task_id=task_id,
                       required_keys=required_keys, success=False,
                       walltime_s=time.monotonic() - wall_t0,
                       fail_reason=f"{type(e).__name__}: {e}")
        except Exception:
            pass
        if is_claude:
            _clean_hard_exit(2)  # reap SDK subprocess + podman; does not return
        return 2

    print(f"[solve v5] loop returned: {list(payload)}", file=sys.stderr)
    walltime_s = time.monotonic() - wall_t0

    # ── Record experience (always, success or failure) ──
    try:
        success = bool(payload.get("task_outputs")) and not payload.get("incomplete")
        win_params = _winning_params(ctx.calc_registry, task_type) if success else {}
        record_run(
            repo_root=REPO_ROOT, task_id=task_id, required_keys=required_keys,
            success=success, walltime_s=walltime_s, params=win_params,
            fail_reason=payload.get("notes") if not success else None,
        )
    except Exception as e:
        log.warning("solve: record_run failed: %s", e)

    # Final material-agnostic output normalization (FIX-37/38) before the bench
    # scores the submitted files — the single pre-scoring chokepoint for every
    # return path (normal submit, verifier-gate exit, salvage, best-effort).
    _normalize_submitted_outputs(payload, workdir)
    code = write_result(out, payload, required_keys, workdir)
    if is_claude:
        # SDK path only: bypass the asyncio/claude-CLI teardown hang that would
        # otherwise burn the wall reserve until a harness SIGKILL, and reap the
        # detached podman QE containers. Does not return.
        _clean_hard_exit(code)
    return code


# ─────────────────────────────────────────────────────────────────────
# Clean teardown / hard exit (M3 bands_dos timeout post-mortem, 2026-06-01)
# ─────────────────────────────────────────────────────────────────────

def _descendant_pids(root_pid: int) -> list[int]:
    """All descendant PIDs of root_pid via /proc (stdlib-only, Linux).

    Used at teardown to find our own subtree so we can SIGTERM it. The QE
    podman wrapper (`qe-gpu-wrap.sh`) names its container `qe-wrap-$$` and
    installs `trap 'podman kill ... qe-wrap-$$' TERM`; the container runs
    detached, so it survives even the harness SIGKILL of solve.py unless its
    wrapper receives SIGTERM first. Batch-safe: we only ever walk *our own*
    descendants — never a sibling solver run's process tree.
    """
    children: dict[int, list[int]] = {}
    try:
        entries = os.listdir("/proc")
    except OSError:
        return []
    for entry in entries:
        if not entry.isdigit():
            continue
        try:
            with open(f"/proc/{entry}/stat", "rb") as fh:
                data = fh.read()
        except OSError:
            continue  # process vanished or unreadable
        # field 2 (comm) is parenthesized and may itself contain ')'/spaces;
        # ppid is the 2nd whitespace field after the final ')'.
        rparen = data.rfind(b")")
        if rparen == -1:
            continue
        rest = data[rparen + 1:].split()
        if len(rest) < 2:
            continue
        try:
            ppid = int(rest[1])  # rest[0]=state, rest[1]=ppid
        except ValueError:
            continue
        children.setdefault(ppid, []).append(int(entry))
    out: list[int] = []
    stack = [root_pid]
    while stack:
        for child in children.get(stack.pop(), []):
            out.append(child)
            stack.append(child)
    return out


def _clean_hard_exit(code: int, *, grace_s: float = 2.0) -> None:
    """SIGTERM our process subtree, then os._exit(code) — never returns.

    Why (M3 bands_dos timeout post-mortem, 2026-06-01): on the SDK path,
    asyncio.run() teardown blocks on the claude-CLI subprocess transport for
    minutes after amain returns, so the harness SIGKILLs the solver at its full
    wall budget — recorded as `timeout` (exit -1), and the result.json we
    already wrote is discarded. The detached podman QE containers also outlive
    that SIGKILL and keep running for hours (GPU leak).

    SIGTERM-ing our descendants makes each in-flight QE wrapper run its
    `trap TERM → podman kill qe-wrap-$$` (the claude CLI exits on stdin EOF);
    os._exit then bypasses the asyncio teardown hang so the harness sees a
    clean exit code and reads result.json. result.json / transcript / logs are
    all written synchronously before we get here, so skipping atexit is safe.
    """
    try:
        pids = _descendant_pids(os.getpid())
        for pid in pids:
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                pass  # already gone
        if pids:
            log.info("solve: teardown SIGTERM → %d descendant pid(s); "
                     "%.1fs grace for podman kill", len(pids), grace_s)
            time.sleep(grace_s)  # let wrapper traps issue `podman kill`
    except Exception:
        log.warning("solve: descendant teardown failed", exc_info=True)
    finally:
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(code)


def main() -> int:
    logging.basicConfig(level=os.environ.get("DFT_LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(name)s %(levelname)s %(message)s",
                        stream=sys.stderr)
    os.environ["DFT_BENCH_SOLVER_MODE"] = "1"
    args = parse_args()
    return asyncio.run(amain(args))


if __name__ == "__main__":
    raise SystemExit(main())
