"""Harness CLI: 一次 bench run 的 7-phase 编排。"""
import argparse
import csv
import datetime
import json
import shlex
import shutil
import sys
from pathlib import Path

# 支持 `python harness/run.py ...` 直接调用（sys.path[0] 是 harness/，找不到 harness 包）
_BENCH_ROOT = Path(__file__).parent.parent.resolve()
if str(_BENCH_ROOT) not in sys.path:
    sys.path.insert(0, str(_BENCH_ROOT))

from harness.lib.task_loader import get_network_allowed, load_task, render_prompt
from harness.lib.workdir import prepare_workdir
from harness.lib.solver_runner import run_solver
from harness.lib.evaluator import (
    extract_t1_fields, extract_t2_fields, extract_t3_fields, extract_t4_fields,
)
from harness.lib.scoring import score_fields
from harness.lib.audit import audit_run, audit_passed, GATING_FLAGS


def main() -> int:
    parser = argparse.ArgumentParser(description="Evaluate a solver on a DFT-Agent Bench task.")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--list-tasks", nargs="?", const="all", choices=("all", "stable", "exploratory", "held_out", "frozen"), help="List IDs from the release manifest and exit (default: all)")
    mode.add_argument("--task-id", help="Task ID to evaluate (see --list-tasks)")
    parser.add_argument("--solver", help="Python solver entry point (required with --task-id)")
    parser.add_argument("--solver-args", default="", help="Extra args passed to solver (shell-quoted)")
    parser.add_argument("--runs-dir", default="runs/")
    parser.add_argument("--timeout", type=int, default=7200,
                        help="Total wall clock for solver (seconds; default 2h, "
                             "matches web-fresh backend Pydantic schemas/run.py 7200)")
    args = parser.parse_args()
    if args.list_tasks:
        with (_BENCH_ROOT / "task_manifest.tsv").open() as manifest:
            for task in csv.DictReader(manifest, delimiter="\t"):
                if args.list_tasks in ("all", task["set"]) or (args.list_tasks == "frozen" and task["frozen_100"] == "1"):
                    print(task["task_id"])
        return 0
    if not args.solver:
        parser.error("--task-id requires --solver")
    # solver subprocess 的 cwd 是 workdir；--solver 若传相对路径会在那里解析失败。
    # 立即 resolve 为绝对路径，下游使用安全（e2e Run #1 pre-3 暴露的问题）。
    args.solver = str(Path(args.solver).resolve())

    bench_root = _BENCH_ROOT
    # 关键: runs_dir 立即 resolve 成 absolute
    # （solver subprocess 的 cwd 会是 workdir，若传 relative path 会解析错）
    runs_dir = Path(args.runs_dir).resolve()
    runs_dir.mkdir(parents=True, exist_ok=True)

    # Phase 1: Load
    task = load_task(bench_root, args.task_id)
    print(f"[harness] Phase 1 - Loaded task {task['id']} (type={task['task']['type']})")

    # Phase 2: Prepare workdir
    ts = datetime.datetime.utcnow().strftime("%Y-%m-%dT%H-%M-%SZ")
    run_dir = runs_dir / f"{ts}_{args.task_id}"
    run_dir.mkdir(parents=True)
    rendered_prompt = render_prompt(bench_root, task)
    workdir = prepare_workdir(
        run_dir=run_dir, bench_root=bench_root,
        task=task, rendered_prompt=rendered_prompt,
    )
    print(f"[harness] Phase 2 - workdir = {workdir}")

    # Phase 3: Spawn solver
    solver_stdout = run_dir / "solver_stdout.log"
    solver_stderr = run_dir / "solver_stderr.log"
    result_json_path = workdir / "result.json"

    extra_args = shlex.split(args.solver_args) if args.solver_args else []
    print(f"[harness] Phase 3 - spawning solver (timeout={args.timeout}s)...")
    exit_code, status, solver_wall_s = run_solver(
        solver_path=Path(args.solver),
        solver_args=extra_args,
        workdir=workdir,
        prompt_path=workdir / "task_prompt.txt",
        out_path=result_json_path,
        timeout_s=args.timeout,
        solver_stdout_path=solver_stdout,
        solver_stderr_path=solver_stderr,
        bench_root=bench_root,
    )
    print(f"[harness] Phase 3 - solver exit_code={exit_code} status={status} wall={solver_wall_s:.1f}s")

    # Read result.json
    if not result_json_path.exists():
        _write_failure_report(run_dir, task, args, exit_code, status, solver_wall_s,
                              reason="solver did not produce result.json")
        return 1
    result_data = json.loads(result_json_path.read_text())
    if result_data.get("incomplete"):
        _write_failure_report(run_dir, task, args, exit_code, status, solver_wall_s,
                              reason=f"solver incomplete: {result_data.get('reason', '?')}")
        return 1
    task_outputs: dict[str, str] = result_data.get("task_outputs", {})

    # Phase 4: Extract fields
    print(f"[harness] Phase 4 - extracting fields from {task_outputs} ...")
    try:
        extracted = _extract(task, workdir, task_outputs)
    except Exception as e:
        _write_failure_report(run_dir, task, args, exit_code, status, solver_wall_s,
                              reason=f"extraction failed: {e}",
                              task_outputs=task_outputs)
        return 1
    print(f"[harness] Phase 4 - extracted: {extracted}")

    # Phase 5: Score
    scoring_result = score_fields(
        ground_truth=task["ground_truth"],
        extracted=extracted,
        scoring=task["scoring"],
        space_group_number=task.get("material", {}).get("space_group_number"),
    )
    print(f"[harness] Phase 5 - total_score = {scoring_result['total_score']}")

    # Phase 6: Audit
    audit_flags = audit_run(
        workdir=workdir,
        task_outputs=task_outputs,
        task_type=task["task"]["type"],
        solver_wall_s=solver_wall_s,
        enforce_no_network=not get_network_allowed(task),
    )
    print(f"[harness] Phase 6 - audit_flags = {audit_flags}")

    # Phase 6b: Audit gate（2026-06-12 决议 A）。audit 失败 = 结果不可信/作弊 →
    # 该 run 判 invalid：official total_score 归 0，但 total_score_raw + per_field
    # 原值保留供分析。leaderboard 读 total_score 即自动把作弊/未跑完的 run 当 0。
    raw_score = scoring_result["total_score"]
    passed = audit_passed(audit_flags)
    if not passed:
        failed = [f for f in GATING_FLAGS if not audit_flags.get(f, False)]
        print(f"[harness] Phase 6b - audit FAILED {failed} → run INVALID, "
              f"total_score forced 0 (raw={raw_score})")

    # Phase 7: Report
    report = {
        "task_id": task["id"],
        "model": _extract_model_arg(extra_args),
        "solver_exit_code": exit_code,
        "solver_status": status,
        "solver_wall_clock_sec": round(solver_wall_s, 2),
        "extracted_fields": extracted,
        "per_field_scores": scoring_result["per_field_scores"],
        "total_score": raw_score if passed else 0.0,
        "total_score_raw": raw_score,
        "audit_passed": passed,
        "audit_flags": audit_flags,
        "task_outputs": task_outputs,
        "notes": result_data.get("notes", ""),
        "run_dir": str(run_dir),
    }
    (run_dir / "report.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print(f"[harness] Phase 7 - report written: {run_dir / 'report.json'}")

    # Phase 8: Purge non-essential QE artifacts (.save/wfc/qe_bins/tmp_*).
    # 跑成功之后这些 restart 文件没用，且单 run 可上百 GB；不清积累几十个
    # run 会再次填满磁盘 (2026-05-23 incident, 500GB 被这些占)。
    # 失败不阻塞 harness 主流程 — 仅 print warning。
    purge_script = Path(__file__).resolve().parent.parent / "scripts" / "purge_run_artifacts.sh"
    if purge_script.exists():
        try:
            import subprocess
            r = subprocess.run([str(purge_script), str(run_dir)],
                               capture_output=True, text=True, timeout=300)
            if r.stdout:
                print(r.stdout.strip())
            if r.returncode != 0 and r.stderr:
                print(f"[purge] non-zero exit: {r.stderr.strip()}")
        except Exception as e:
            print(f"[purge] skipped: {type(e).__name__}: {e}")

    return 0


def _extract(task: dict, workdir: Path, task_outputs: dict[str, str]) -> dict:
    task_type = task["task"]["type"]
    if task_type == "vc_relax":
        rel = task_outputs.get("vc_relax_out")
        if not rel:
            raise ValueError("task_outputs missing key 'vc_relax_out'")
        path = workdir / rel if not Path(rel).is_absolute() else Path(rel)
        return extract_t1_fields(
            relax_out_path=path,
            extraction_config=task["evaluator"].get("extraction_config", {}),
        )
    if task_type == "bands_dos":
        required = ("scf_out", "bands_out", "bands_data", "dos_dat", "nscf_dos_out")
        missing = [k for k in required if not task_outputs.get(k)]
        if missing:
            raise ValueError(
                f"task_outputs missing required keys {missing} (need {list(required)})"
            )
        # 杨博 2026-05-18: dos_e_fermi 参考帧按 GT.gap_type 分流
        gt_gap_type = (task.get("ground_truth", {})
                          .get("gap_type", {}).get("value"))
        is_metal_per_gt = (gt_gap_type == "metal") if gt_gap_type else None
        # 晶体学空间群 → seekpath symprec 逐级放松目标（噪声结构防 P1 + 基底对齐 GT）
        target_spacegroup = task.get("material", {}).get("space_group_number")
        return extract_t2_fields(
            scf_out_path=_resolve_workdir_path(workdir, task_outputs["scf_out"]),
            bands_out_path=_resolve_workdir_path(workdir, task_outputs["bands_out"]),
            bands_dat_path=_resolve_workdir_path(workdir, task_outputs["bands_data"]),
            dos_dat_path=_resolve_workdir_path(workdir, task_outputs["dos_dat"]),
            nscf_dos_out_path=_resolve_workdir_path(workdir, task_outputs["nscf_dos_out"]),
            initial_structure=task["material"]["initial_structure"],
            extraction_config=task["evaluator"].get("extraction_config", {}),
            is_metal_per_gt=is_metal_per_gt,
            target_spacegroup=target_spacegroup,
        )
    if task_type == "eos":
        rel = task_outputs.get("eos_points_csv")
        if not rel:
            raise ValueError("task_outputs missing key 'eos_points_csv'")
        eos_path = _resolve_workdir_path(workdir, rel)
        fit_rel = task_outputs.get("fit_json")
        fit_path = _resolve_workdir_path(workdir, fit_rel) if fit_rel else None
        return extract_t3_fields(
            eos_points_csv_path=eos_path,
            fit_json_path=fit_path,
            atoms_per_primitive_cell=task["material"]["atoms_per_primitive_cell"],
            extraction_config=task["evaluator"].get("extraction_config", {}),
        )
    if task_type == "elastic_stiffness":
        rel = task_outputs.get("stress_strain_csv")
        if not rel:
            raise ValueError("task_outputs missing key 'stress_strain_csv'")
        sss_path = _resolve_workdir_path(workdir, rel)
        tensor_rel = task_outputs.get("elastic_tensor_json")
        tensor_path = _resolve_workdir_path(workdir, tensor_rel) if tensor_rel else None
        return extract_t4_fields(
            stress_strain_csv_path=sss_path,
            elastic_tensor_json_path=tensor_path,
            extraction_config=task["evaluator"].get("extraction_config", {}),
        )
    raise NotImplementedError(f"extraction for task_type={task_type!r} not implemented")


def _resolve_workdir_path(workdir: Path, rel_or_abs: str) -> Path:
    p = Path(rel_or_abs)
    return p if p.is_absolute() else workdir / p


def _extract_model_arg(extra_args: list[str]) -> str:
    """从 `--model X` / `--model=X` 中拿 model 名。找不到返回 unknown。"""
    for i, a in enumerate(extra_args):
        if a == "--model" and i + 1 < len(extra_args):
            return extra_args[i + 1]
        if a.startswith("--model="):
            return a.split("=", 1)[1]
    return "unknown"


def _write_failure_report(run_dir: Path, task: dict, args, exit_code, status, solver_wall_s,
                          reason: str, task_outputs: dict | None = None):
    report = {
        "task_id": task["id"],
        "solver_exit_code": exit_code,
        "solver_status": status,
        "solver_wall_clock_sec": round(solver_wall_s, 2),
        "failure_reason": reason,
        "task_outputs": task_outputs or {},
        "run_dir": str(run_dir),
    }
    (run_dir / "report.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print(f"[harness] FAILURE - {reason} (report: {run_dir / 'report.json'})")


if __name__ == "__main__":
    sys.exit(main())
