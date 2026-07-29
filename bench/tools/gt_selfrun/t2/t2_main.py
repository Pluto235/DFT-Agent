"""Main entry: run T2 8-point pipeline for target materials.

Usage:
    # GPU mode (default)
    python -m tools.gt_selfrun.t2.t2_main --only M1_T2_Si_bands_dos --gpu 0

    # CPU mode (新机部署)
    python -m tools.gt_selfrun.t2.t2_main --only M1_T2_Si_bands_dos \\
            --cpu --cpu-mpi-ranks 26

    # all materials
    python -m tools.gt_selfrun.t2.t2_main --all --gpu 0
    python -m tools.gt_selfrun.t2.t2_main --all --cpu --cpu-mpi-ranks 26

    # debug: only one scan point of one material (no canonical dos)
    python -m tools.gt_selfrun.t2.t2_main --only M1_T2_Si_bands_dos \\
            --point e80_d50 --gpu 0

    # dry-run: print what would run, do nothing
    python -m tools.gt_selfrun.t2.t2_main --only M1_T2_Si_bands_dos --dry-run

并行：每个进程独占资源（GPU 1 张 / CPU 26 核），多进程 = 多开几个本命令的实例
传不同 --only 列表。
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from pathlib import Path

# Ensure dft-agent-bench root on sys.path
_THIS = Path(__file__).resolve()
_BENCH_REPO_ROOT = _THIS.parent.parent.parent.parent  # t2/t2_main.py → tools/gt_selfrun/t2 → tools/gt_selfrun → tools → dft-agent-bench/
if str(_BENCH_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_BENCH_REPO_ROOT))

from tools.gt_selfrun.t2 import t2_params, t2_run  # noqa: E402
from tools.gt_selfrun.t2.t2_params import UpstreamT1FailureError  # noqa: E402

HERE = Path(__file__).resolve().parent  # tools/gt_selfrun/t2/
GT_SELFRUN_ROOT = HERE.parent  # tools/gt_selfrun/
BENCH_ROOT = GT_SELFRUN_ROOT.parent.parent  # → dft-agent-bench/
T2_QUESTIONS_M1_DIR = BENCH_ROOT / "questions" / "M1_T2"
CANONICAL_T2_DIR = GT_SELFRUN_ROOT / "canonical_gt_t2"
DOS_DATA_DIR = CANONICAL_T2_DIR / "dos_data"
T2_LOG = GT_SELFRUN_ROOT / "t2_main.log"


def discover_materials() -> list[str]:
    """Return all retained M1 T2 material ids."""
    return sorted(p.stem for p in T2_QUESTIONS_M1_DIR.glob("M1_T2_*_bands_dos.json"))


def _strip_t2_id(material_id: str) -> str:
    """Strip the M1_T2 prefix and task suffix to get the material name."""
    prefix = "M1_T2_"
    head = material_id[len(prefix):] if material_id.startswith(prefix) else material_id
    if head.endswith("_bands_dos"):
        head = head[: -len("_bands_dos")]
    return head


def assemble_canonical_gt(material_id: str, run_summary: dict) -> dict:
    """Build the canonical_gt_t2/<id>.json from a run_one_material summary.

    The 5 GT fields come from `canonical` (e80_d50). `convergence_evidence`
    captures the full 8-point scan + classification.
    """
    if run_summary.get("status") != "OK":
        # Build a partial JSON for failed runs (audit-friendly).
        return {
            "id": material_id,
            "schema_version": "1.2",
            "status": "FAILED",
            "failed_at": run_summary.get("failed_at"),
            "is_metal": run_summary.get("is_metal"),
            "convergence_evidence": _evidence_block(run_summary),
            "metadata": {
                "author": "weilin",
                "generated_at": time.strftime("%Y-%m-%d"),
                "source": "QE GBRV_USPP_PBE_v1.5 self-run T2 pipeline (8-point scan)",
            },
        }

    canon = run_summary["canonical"]
    is_metal = run_summary["is_metal"]
    band_gap_ev = canon["band_gap_ev"] if canon["band_gap_ev"] is not None else 0.0
    gap_type = canon["gap_type"] if canon["gap_type"] else (
        "metal" if is_metal else "indirect")
    fermi = canon["dos_e_fermi"]

    # DOS curve: copy canonical dos.dat into dos_data/<head>.dat
    head = _strip_t2_id(material_id)
    dos_dat_target_rel = f"tools/gt_selfrun/canonical_gt_t2/dos_data/{head}.dat"

    gt = {
        "band_gap_ev": {"value": float(band_gap_ev), "unit": "eV",
                        "tolerance": 0.1, "rule": "absolute"},
        "gap_type": {"value": gap_type, "rule": "exact"},
        "vbm_k_label": {"value": canon["vbm_k_label"], "rule": "exact"},
        "cbm_k_label": {"value": canon["cbm_k_label"], "rule": "exact"},
        "dos_e_fermi": {
            "value": float(fermi) if fermi is not None else None,
            "unit": "eV", "tolerance": 0.1, "rule": "absolute",
        },
        "dos_curve": {
            "ref_file": dos_dat_target_rel,
            "unit": "states/eV/fu",
            "tolerance": 0.1, "rule": "curve_l2",
        },
    }

    return {
        "id": material_id,
        "schema_version": "1.2",
        "ground_truth": gt,
        "convergence_evidence": _evidence_block(run_summary),
        "metadata": {
            "author": "weilin",
            "generated_at": time.strftime("%Y-%m-%d"),
            "source": "QE GBRV_USPP_PBE_v1.5 self-run T2 pipeline (8-point scan)",
        },
        "extracted_raw": {
            "vbm_k_cryst": canon["vbm_k_cryst"],
            "cbm_k_cryst": canon["cbm_k_cryst"],
            "vbm_ev": canon["vbm_ev"],
            "cbm_ev": canon["cbm_ev"],
        },
    }


def _evidence_block(run_summary: dict) -> dict:
    """Build the convergence_evidence block from run_summary."""
    block: dict = {
        "scan_points": run_summary.get("scan_points", []),
        "classification": run_summary.get("classification"),
        "wall_time_sec": run_summary.get("wall_time_sec"),
    }
    if "canonical" in run_summary:
        block["canonical_point_key"] = run_summary["canonical"]["point_key"]
        block["canonical_kmesh"] = run_summary["canonical"]["kmesh"]
    if "dos_params" in run_summary:
        block["dos_params"] = run_summary["dos_params"]
    return block


def write_canonical(material_id: str, run_summary: dict) -> Path:
    """Write canonical_gt_t2/<id>.json + copy dos.dat into dos_data/."""
    CANONICAL_T2_DIR.mkdir(parents=True, exist_ok=True)
    DOS_DATA_DIR.mkdir(parents=True, exist_ok=True)

    canonical = assemble_canonical_gt(material_id, run_summary)
    out_path = CANONICAL_T2_DIR / f"{material_id}.json"
    out_path.write_text(json.dumps(canonical, indent=2), encoding="utf-8")

    # Copy canonical dos.dat into dos_data/<head>.dat
    if run_summary.get("status") == "OK":
        head = _strip_t2_id(material_id)
        src_path = run_summary["canonical"].get("dos_dat_path")
        if src_path:
            src = Path(src_path)
            if src.exists():
                shutil.copy2(src, DOS_DATA_DIR / f"{head}.dat")
    return out_path


def _log(line: str) -> None:
    T2_LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(T2_LOG, "a", encoding="utf-8") as f:
        f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {line}\n")


def main() -> int:
    parser = argparse.ArgumentParser()
    grp = parser.add_mutually_exclusive_group(required=True)
    grp.add_argument("--only", type=str, nargs="+", default=None,
                     help="Run only these material ids (space-separated)")
    grp.add_argument("--all", action="store_true",
                     help="Run all M1_T2_* materials discovered in bench/questions/M1_T2/")
    parser.add_argument("--point", type=str, default=None,
                        help="Run only this scan point (e.g. e80_d50). "
                             "When set, no canonical dos.x runs (debug mode).")
    parser.add_argument("--gpu", type=int, default=None,
                        help="GPU id to use (GPU mode; mutex with --cpu)")
    parser.add_argument("--cpu", action="store_true",
                        help="CPU mode: native pw.x via mpirun (mutex with --gpu)")
    parser.add_argument("--cpu-mpi-ranks", type=int, default=26,
                        help="MPI ranks per task in CPU mode (default 26 = "
                             "sweet spot; tested 0511 CPU vs GPU benchmark)")
    parser.add_argument("--force", action="store_true",
                        help="Re-run all steps even if outputs exist")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print plan, don't run any QE")
    args = parser.parse_args()

    # Resolve mode: --gpu and --cpu are mutex
    if args.cpu and args.gpu is not None:
        print("[t2_main] ERROR: --gpu and --cpu are mutually exclusive",
              file=sys.stderr)
        return 2
    if args.cpu:
        mode = "cpu"
        gpu_id = 0  # unused in CPU mode
    else:
        mode = "gpu"
        gpu_id = args.gpu if args.gpu is not None else 0

    if args.only:
        targets = list(args.only)
    else:
        targets = discover_materials()

    if not targets:
        print("[t2_main] no targets", file=sys.stderr)
        return 1

    mode_label = f"CPU x{args.cpu_mpi_ranks}" if mode == "cpu" else f"GPU {gpu_id}"
    print(f"[t2_main] {len(targets)} material(s) on {mode_label}; "
          f"point={args.point or 'ALL 8 + canonical_dos'}; "
          f"force={args.force}; dry_run={args.dry_run}",
          flush=True)

    if args.dry_run:
        print("[t2_main] DRY RUN — listing scan plan only:")
        scan = t2_params.scan_grid()
        for mid in targets:
            print(f"  {mid}:")
            for p in scan:
                print(f"    point {p.key} round={p.round} ecutwfc={p.ecutwfc} "
                      f"density={p.kmesh_density}")
            print(f"    canonical_dos at {t2_params.CANONICAL_POINT_KEY}")
        return 0

    n_ok = 0
    n_failed = 0
    _log(f"=== run start: {len(targets)} materials, {mode_label} ===")
    t0_total = time.time()
    for i, mid in enumerate(targets):
        print(f"\n=== [{i+1}/{len(targets)}] T2 pipeline for {mid} ({mode_label}) ===\n",
              flush=True)
        t0 = time.time()
        try:
            summary = t2_run.run_one_material(
                mid, gpu_id=gpu_id, force=args.force,
                only_point=args.point,
                mode=mode, cpu_mpi_ranks=args.cpu_mpi_ranks,
            )
        except UpstreamT1FailureError as e:
            print(f"[t2_main] SKIP {mid}: T1 upstream FAIL — {e}",
                  file=sys.stderr, flush=True)
            _log(f"SKIP_T1_FAIL {mid}: {e}")
            failed_summary = {
                "material_id": mid,
                "status": "FAILED",
                "failed_at": "upstream_t1",
                "is_metal": False,
                "point_summaries": [],
                "error": str(e),
            }
            try:
                write_canonical(mid, failed_summary)
            except Exception as werr:  # noqa: BLE001
                print(f"[t2_main] write_canonical failed for {mid}: {werr}",
                      file=sys.stderr)
            n_failed += 1
            continue
        except Exception as e:
            import traceback
            print(f"[t2_main] EXCEPTION for {mid}: {e}", file=sys.stderr)
            traceback.print_exc()
            _log(f"EXC {mid}: {e}")
            n_failed += 1
            continue

        wall = time.time() - t0
        if args.point is not None:
            # Partial run: don't write canonical_gt_t2 JSON
            print(f"[t2_main] partial(--point {args.point}) {mid}: "
                  f"status={summary['status']} wall={wall:.1f}s", flush=True)
            _log(f"PARTIAL {mid}/{args.point}: {summary['status']}")
            continue

        if summary["status"] != "OK":
            print(f"[t2_main] FAILED {mid}: at={summary.get('failed_at')} "
                  f"wall={wall:.1f}s", file=sys.stderr, flush=True)
            _log(f"FAILED {mid}: {summary.get('failed_at')}")
            # Still write JSON for audit
            try:
                write_canonical(mid, summary)
            except Exception as e:  # noqa: BLE001
                print(f"[t2_main] write_canonical failed for {mid}: {e}",
                      file=sys.stderr)
            n_failed += 1
            continue

        out_path = write_canonical(mid, summary)
        n_ok += 1
        canon = summary["canonical"]
        cls = summary["classification"]["classification"]
        print(f"[t2_main] OK   {mid}: gap={canon['band_gap_ev']:.4f} eV "
              f"({canon['gap_type']}), VBM={canon['vbm_k_label']}, "
              f"CBM={canon['cbm_k_label']}, Ef={canon['dos_e_fermi']}, "
              f"classification={cls}, wall={wall:.1f}s -> {out_path}",
              flush=True)
        _log(f"OK {mid}: gap={canon['band_gap_ev']} cls={cls} wall={wall:.1f}s")

    total_wall = time.time() - t0_total
    print(f"\n[t2_main] {n_ok}/{len(targets)} OK, {n_failed} failed; "
          f"total wall={total_wall:.1f}s", flush=True)
    _log(f"=== done: {n_ok} OK / {n_failed} failed / total {total_wall:.1f}s ===")
    return 0 if n_failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
