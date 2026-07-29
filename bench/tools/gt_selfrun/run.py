"""Batch runner for gt_selfrun Phase 1 strategy A convergence scans.

Orchestrates 58 M1 materials across the configured convergence scan points.

Usage:
    python tools/gt_selfrun/run.py [--dry-run] [--only ID] [--gpus 0,1]
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict
from pathlib import Path
from queue import Queue

# Ensure dft-agent-bench root on sys.path so `from tools.gt_selfrun.X import ...` works
# regardless of how this script is invoked (direct python vs. conda run).
_THIS_FILE = Path(__file__).resolve()
_BENCH_REPO_ROOT = _THIS_FILE.parent.parent.parent  # tools/gt_selfrun/run.py → dft-agent-bench/
if str(_BENCH_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_BENCH_REPO_ROOT))

from pymatgen.core import Lattice

HERE = Path(__file__).resolve().parent
BENCH_ROOT = HERE.parent.parent  # tools/gt_selfrun/ → dft-agent-bench/
WORKSPACE_ROOT = BENCH_ROOT.parent.parent  # → ~/workspace/
PSEUDO_DIR = BENCH_ROOT / "pseudos" / "GBRV_USPP_PBE_v1.5"
BENCH_T1_DIR = BENCH_ROOT / "questions"
WORKDIR_ROOT = HERE / "workdir"
PARAMS_DIR = HERE / "params"
RUN_LOG = HERE / "run.log"

# QE binary (absolute path — note: PW_BIN_* point to the pw.x symlink, NOT the dir)
PW_BIN_GPU = WORKSPACE_ROOT / "repos" / "TritonDFT" / "tritonDFT-src" / "QuantumE-gpu-wrapper" / "pw.x"
PW_BIN_CPU = WORKSPACE_ROOT / "repos" / "TritonDFT" / "tritonDFT-src" / "QuantumE" / "bin" / "pw.x"

# Per-run wall time cap (seconds). If exceeded, kill the job.
# Tuned per material in future; constant for now.
RUN_TIMEOUT_SEC = 6 * 3600  # 6 hours


def discover_materials(bench_t1_dir: Path = BENCH_T1_DIR) -> list[Path]:
    """Return absolute paths to all retained M1 T1 question files."""
    return sorted((bench_t1_dir / "M1_T1").glob("*.json"))


def run_one_scan_point(
    bench_json_path: Path,
    scan_point,  # params.ScanPoint
    gpu_id: int,
    pw_bin: Path = PW_BIN_GPU,
    run_timeout_sec: int = RUN_TIMEOUT_SEC,
) -> dict:
    """Run one (material, scan_point) combination. Returns result dict.

    Creates workdir/<material_id>/<scan_point.key>/, writes relax.in,
    invokes pw.x with CUDA_VISIBLE_DEVICES=<gpu_id>, parses output.

    Idempotent: if result.json already exists with status OK, skip.
    """
    from tools.gt_selfrun import analyze, params

    bench_json = json.loads(bench_json_path.read_text(encoding="utf-8"))
    material_id = bench_json["id"]
    workdir = WORKDIR_ROOT / material_id / scan_point.key
    workdir.mkdir(parents=True, exist_ok=True)

    relax_in = workdir / "relax.in"
    relax_out = workdir / "relax.out"
    relax_err = workdir / "relax.err"
    result_json = workdir / "result.json"

    # Idempotent: skip if already done successfully
    if result_json.exists():
        existing = json.loads(result_json.read_text())
        if existing.get("status") == "OK":
            return existing

    # Determine kmesh and is_metal
    init = bench_json["material"]["initial_structure"]
    lat = Lattice.from_parameters(
        init["lattice_constants_angstrom"]["a"],
        init["lattice_constants_angstrom"]["b"],
        init["lattice_constants_angstrom"]["c"],
        init["lattice_angles_degree"]["alpha"],
        init["lattice_angles_degree"]["beta"],
        init["lattice_angles_degree"]["gamma"],
    )
    kmesh = params.kmesh_from_density(lat, scan_point.kmesh_density)
    metal = params.is_metal(bench_json_path)

    # Render and write QE input
    qe_input = params.render_qe_input(
        bench_json=bench_json,
        scan_point=scan_point,
        kmesh=kmesh,
        is_metal=metal,
        pseudo_dir=PSEUDO_DIR,
    )
    relax_in.write_text(qe_input, encoding="utf-8")

    # Invoke pw.x
    env = os.environ.copy()
    env["CUDA_VISIBLE_DEVICES"] = str(gpu_id)
    start = time.time()
    try:
        with open(relax_out, "w") as stdout_f, open(relax_err, "w") as stderr_f:
            proc = subprocess.run(
                [str(pw_bin), "-in", str(relax_in)],
                stdout=stdout_f,
                stderr=stderr_f,
                cwd=workdir,
                env=env,
                timeout=run_timeout_sec,
                check=False,
            )
        wall_time = time.time() - start
        returncode = proc.returncode
        timed_out = False
    except subprocess.TimeoutExpired:
        wall_time = time.time() - start
        returncode = -1
        timed_out = True

    # Parse output
    parsed = analyze.parse_relax_output(relax_out)
    # If timed out, override status
    if timed_out:
        parsed["status"] = "TIMEOUT"
    if returncode != 0 and parsed["status"] == "OK":
        # Mismatch: output says OK but pw.x exited non-zero → call it CRASHED
        parsed["status"] = "CRASHED"

    # If OK, extract geometry
    geom = None
    if parsed["status"] == "OK":
        try:
            species = list({site["element"] for site in init["atomic_positions_crystal"]})
            geom = analyze.extract_geometry_from_relax_out(relax_out, species=species)
        except Exception as e:
            parsed["status"] = "GEOMETRY_EXTRACT_FAILED"
            parsed["error"] = str(e)

    # Build result dict
    result = {
        "material_id": material_id,
        "scan_point": scan_point.key,
        "ecutwfc": scan_point.ecutwfc,
        "ecutrho": scan_point.ecutrho,
        "kmesh_density": scan_point.kmesh_density,
        "kmesh": kmesh,
        "round": scan_point.round,
        "is_metal": metal,
        "gpu_id": gpu_id,
        "wall_time_sec": wall_time,
        "returncode": returncode,
        "status": parsed["status"],
        "final_total_energy_ry": parsed.get("final_total_energy_ry"),
        "final_pressure_kbar": parsed.get("final_pressure_kbar"),
        "n_ionic_steps": parsed.get("n_ionic_steps"),
    }
    if geom is not None:
        result["final_lattice_a"] = geom["a"]
        result["final_lattice_b"] = geom["b"]
        result["final_lattice_c"] = geom["c"]
        result["final_alpha"] = geom["alpha"]
        result["final_beta"] = geom["beta"]
        result["final_gamma"] = geom["gamma"]
        result["space_group_number"] = geom["space_group_number"]
        result["point_group"] = geom["point_group"]
        result["crystal_system"] = geom["crystal_system"]
        result["nat"] = geom["nat"]
        # Check if SG changed vs initial
        expected_sg = bench_json.get("material", {}).get("space_group_number")
        if expected_sg is not None and geom["space_group_number"] != expected_sg:
            result["status"] = "SYMMETRY_BROKEN"
            result["original_sg"] = expected_sg

    # Persist
    result_json.write_text(json.dumps(result, indent=2), encoding="utf-8")
    return result


def _worker_loop(
    task_queue: Queue,
    gpu_id: int,
    results: list,
    results_lock,
    dry_run: bool = False,
) -> None:
    """Worker thread: pulls (bench_json_path, scan_point) tasks and runs them on gpu_id."""
    while True:
        task = task_queue.get()
        if task is None:
            task_queue.task_done()
            break
        bench_json_path, scan_point = task
        material_id = json.loads(bench_json_path.read_text())["id"]
        print(f"[GPU{gpu_id}] {material_id} {scan_point.key} starting…", flush=True)
        try:
            if dry_run:
                result = {"material_id": material_id, "scan_point": scan_point.key,
                          "status": "DRY_RUN", "gpu_id": gpu_id, "wall_time_sec": 0.0}
            else:
                result = run_one_scan_point(bench_json_path, scan_point, gpu_id=gpu_id)
            with results_lock:
                results.append(result)
                _append_run_log(result)
            print(f"[GPU{gpu_id}] {material_id} {scan_point.key}: {result['status']} "
                  f"(wall {result.get('wall_time_sec', 0):.1f}s)", flush=True)
        except Exception as e:
            print(f"[GPU{gpu_id}] {material_id} {scan_point.key}: EXCEPTION {e}",
                  flush=True, file=sys.stderr)
            with results_lock:
                results.append({
                    "material_id": material_id, "scan_point": scan_point.key,
                    "status": "EXCEPTION", "error": str(e), "gpu_id": gpu_id,
                })
        finally:
            task_queue.task_done()


def _append_run_log(result: dict) -> None:
    with open(RUN_LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(result) + "\n")


def batch_run(
    materials: list[Path],
    gpus: list[int] = (0, 1),
    dry_run: bool = False,
) -> list[dict]:
    """Run all (material × scan_point) combinations across the given GPUs.

    Strategy:
        - Phase 1: enqueue round-1 tasks (5 per material)
        - Wait for all to complete
        - Phase 2: enqueue round-2 tasks (3 per material, skipping overlap with round 1)

    Why split: round 2 uses ecutwfc=80 which is also the last round-1 point;
    we want round 2 to share the same ecutwfc=80 converged behavior as round 1's last.
    More importantly, splitting allows progress monitoring by phase.
    """
    import threading
    from tools.gt_selfrun.params import scan_grid

    all_points = scan_grid()
    round1_points = [p for p in all_points if p.round == 1]
    round2_points = [p for p in all_points if p.round == 2]
    results: list[dict] = []
    results_lock = threading.Lock()

    for phase, points in [("round1", round1_points), ("round2", round2_points)]:
        print(f"\n=== Starting {phase} ({len(points)} points × {len(materials)} materials = "
              f"{len(points) * len(materials)} tasks) ===\n", flush=True)
        task_queue: Queue = Queue()
        for mat in materials:
            for p in points:
                task_queue.put((mat, p))
        # Sentinel values to stop workers
        for _ in gpus:
            task_queue.put(None)

        # Launch workers
        with ThreadPoolExecutor(max_workers=len(gpus)) as exec:
            futures = [
                exec.submit(_worker_loop, task_queue, gpu, results, results_lock, dry_run)
                for gpu in gpus
            ]
            task_queue.join()
            for f in as_completed(futures):
                f.result()
        print(f"=== {phase} complete ===\n", flush=True)

    return results


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true",
                        help="Don't actually run pw.x, just print task list")
    parser.add_argument("--only", type=str, default=None,
                        help="Only run this material id (e.g. M1_T1_Si_vc_relax)")
    parser.add_argument("--gpus", type=str, default="0,1",
                        help="Comma-separated GPU ids (default: 0,1)")
    args = parser.parse_args()

    gpus = [int(g) for g in args.gpus.split(",") if g.strip()]
    materials = discover_materials()
    if args.only:
        materials = [m for m in materials if args.only in m.name]
        if not materials:
            print(f"No material matched id fragment '{args.only}'", file=sys.stderr)
            return 1

    print(f"Discovered {len(materials)} materials; using GPUs {gpus}", flush=True)

    # Ensure log dir exists and append fresh session marker
    RUN_LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(RUN_LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps({"_session_start": time.strftime("%Y-%m-%d %H:%M:%S")}) + "\n")

    results = batch_run(materials, gpus=gpus, dry_run=args.dry_run)

    # Summary
    by_status: dict[str, int] = {}
    for r in results:
        by_status[r["status"]] = by_status.get(r["status"], 0) + 1
    print("\n=== batch_run summary ===")
    for k, v in sorted(by_status.items()):
        print(f"  {k}: {v}")
    print(f"  total: {len(results)}")
    return 0 if by_status.get("EXCEPTION", 0) == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
