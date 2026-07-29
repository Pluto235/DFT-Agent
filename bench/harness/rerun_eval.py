"""Rerun phases 4-5 (extract + score) on an existing run_dir.

Use case: T2 evaluator was added after Run #2 / #4 had been run. Their workdirs
contain valid QE outputs but report.json shows 'extraction not implemented'.
This tool re-runs extract_*_fields + score_fields on the existing workdir,
writing report_rerun.json (preserves the original report.json for comparison).

Usage:
  python harness/rerun_eval.py --run-dir runs/<UTC>_<task_id>/ \
      [--task-outputs-patch '{"bands_data":"runs/.../bands.dat"}']

The patch lets you inject keys missing from result.json (e.g. legacy Run #2
predates the bands_data submission contract).
"""
import argparse
import datetime
import json
import sys
from pathlib import Path

_BENCH_ROOT = Path(__file__).parent.parent.resolve()
if str(_BENCH_ROOT) not in sys.path:
    sys.path.insert(0, str(_BENCH_ROOT))

from harness.lib.task_loader import load_task
from harness.lib.evaluator import (
    extract_t1_fields, extract_t2_fields, extract_t3_fields, extract_t4_fields,
)
from harness.lib.scoring import score_fields


def main() -> int:
    parser = argparse.ArgumentParser(description="Rerun harness eval on existing run_dir")
    parser.add_argument("--run-dir", required=True, type=Path)
    parser.add_argument("--task-outputs-patch", default="",
                        help="JSON string merged into task_outputs (legacy patch)")
    parser.add_argument("--report-name", default="report_rerun.json",
                        help="Output filename in run_dir (default report_rerun.json)")
    parser.add_argument("--legacy-dos-detect", action="store_true",
                        help="Legacy mode for re-scoring old T2 runs whose task_outputs "
                             "may lack nscf_dos_out and whose dos.dat may have been "
                             "agent-mutated. Locates nscf_dos.out heuristically and "
                             "detects pre-shifted dos.dat. NEVER use for new runs.")
    args = parser.parse_args()

    run_dir = args.run_dir.resolve()
    if not run_dir.exists():
        print(f"[rerun_eval] run_dir does not exist: {run_dir}", file=sys.stderr)
        return 1
    workdir = run_dir / "workdir"
    if not workdir.exists():
        print(f"[rerun_eval] workdir not found: {workdir}", file=sys.stderr)
        return 1
    result_path = workdir / "result.json"
    if not result_path.exists():
        print(f"[rerun_eval] result.json not found: {result_path}", file=sys.stderr)
        return 1

    # Determine task_id from run_dir name: '<UTC>_<task_id>'
    parts = run_dir.name.split("_", 1)
    if len(parts) < 2:
        print(f"[rerun_eval] cannot parse task_id from run_dir name {run_dir.name}",
              file=sys.stderr)
        return 1
    task_id = parts[1]

    task = load_task(_BENCH_ROOT, task_id)
    print(f"[rerun_eval] loaded task {task['id']} (type={task['task']['type']})")

    result_data = json.loads(result_path.read_text())
    task_outputs: dict[str, str] = dict(result_data.get("task_outputs", {}))
    if args.task_outputs_patch:
        patch = json.loads(args.task_outputs_patch)
        task_outputs.update(patch)
        print(f"[rerun_eval] patched task_outputs with {patch}")

    print(f"[rerun_eval] task_outputs = {task_outputs}")

    # Phase 4: Extract
    extracted = _extract(task, workdir, task_outputs, legacy_dos_detect=args.legacy_dos_detect)
    print(f"[rerun_eval] extracted = {_brief(extracted)}")

    # Phase 5: Score
    scoring_result = score_fields(
        ground_truth=task["ground_truth"],
        extracted=extracted,
        scoring=task["scoring"],
        space_group_number=task.get("material", {}).get("space_group_number"),
    )
    total = scoring_result["total_score"]
    print(f"[rerun_eval] total_score = {total}")
    print(f"[rerun_eval] per_field_scores = {scoring_result['per_field_scores']}")

    # Phase 7: Report (no audit; preserves original report.json)
    report = {
        "task_id": task["id"],
        "rerun_eval_at": datetime.datetime.utcnow().isoformat() + "Z",
        "rerun_basis_run_dir": str(run_dir),
        "task_outputs": task_outputs,
        "extracted_fields": extracted,
        "per_field_scores": scoring_result["per_field_scores"],
        "total_score": total,
        "ground_truth": task["ground_truth"],
        "notes": "Rerun via harness/rerun_eval.py — extract+score only (no audit).",
    }
    out_path = run_dir / args.report_name
    out_path.write_text(json.dumps(report, indent=2, ensure_ascii=False, default=_json_default),
                        encoding="utf-8")
    print(f"[rerun_eval] wrote {out_path}")
    return 0


def _extract(task: dict, workdir: Path, task_outputs: dict[str, str],
             legacy_dos_detect: bool = False) -> dict:
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

        def _r(rel):
            p = Path(rel)
            return p if p.is_absolute() else workdir / p

        # --legacy-dos-detect: gracefully fill missing nscf_dos_out via heuristic
        if legacy_dos_detect and not task_outputs.get("nscf_dos_out"):
            dos_dat_abs = _r(task_outputs.get("dos_dat", ""))
            found = _find_nscf_dos_out_legacy(workdir, dos_dat_abs)
            if found:
                # Store relative path for serialization back into report.json's task_outputs
                try:
                    rel = str(found.relative_to(workdir))
                except ValueError:
                    rel = str(found)
                task_outputs["nscf_dos_out"] = rel
                print(f"[rerun_eval] legacy-dos-detect: found nscf_dos_out at {rel}",
                      file=sys.stderr)
            else:
                print(f"[rerun_eval] legacy-dos-detect: no nscf_dos.out found in "
                      f"{dos_dat_abs.parent}; will fail required-keys check",
                      file=sys.stderr)

        missing = [k for k in required if not task_outputs.get(k)]
        if missing:
            raise ValueError(f"task_outputs missing keys {missing}")

        # --legacy-dos-detect: detect if dos.dat already EF-shifted
        dos_dat_already_relative = False
        if legacy_dos_detect:
            dos_dat_abs = _r(task_outputs["dos_dat"])
            dos_dat_already_relative = _detect_dos_dat_relative(dos_dat_abs)
            if dos_dat_already_relative:
                print(f"[rerun_eval] legacy-dos-detect: dos.dat appears EF-shifted "
                      f"(col 1 spans 0); skipping extract's EF subtraction",
                      file=sys.stderr)

        # 杨博 2026-05-18: dos_e_fermi 参考帧按 GT.gap_type 分流
        # ("metal" → nscf EF; else → VBM, 因为绝缘体 gap 内 EF 多解)
        gt_gap_type = (task.get("ground_truth", {})
                          .get("gap_type", {}).get("value"))
        is_metal_per_gt = (gt_gap_type == "metal") if gt_gap_type else None
        target_spacegroup = task.get("material", {}).get("space_group_number")

        return extract_t2_fields(
            scf_out_path=_r(task_outputs["scf_out"]),
            bands_out_path=_r(task_outputs["bands_out"]),
            bands_dat_path=_r(task_outputs["bands_data"]),
            dos_dat_path=_r(task_outputs["dos_dat"]),
            nscf_dos_out_path=_r(task_outputs["nscf_dos_out"]),
            initial_structure=task["material"]["initial_structure"],
            extraction_config=task["evaluator"].get("extraction_config", {}),
            dos_dat_already_relative=dos_dat_already_relative,
            is_metal_per_gt=is_metal_per_gt,
            target_spacegroup=target_spacegroup,
        )
    if task_type == "eos":
        rel = task_outputs.get("eos_points_csv")
        if not rel:
            raise ValueError("task_outputs missing key 'eos_points_csv'")

        def _r(rel_):
            p = Path(rel_)
            return p if p.is_absolute() else workdir / p

        fit_rel = task_outputs.get("fit_json")
        return extract_t3_fields(
            eos_points_csv_path=_r(rel),
            fit_json_path=_r(fit_rel) if fit_rel else None,
            atoms_per_primitive_cell=task["material"]["atoms_per_primitive_cell"],
            extraction_config=task["evaluator"].get("extraction_config", {}),
        )
    if task_type == "elastic_stiffness":
        rel = task_outputs.get("stress_strain_csv")
        if not rel:
            raise ValueError("task_outputs missing key 'stress_strain_csv'")

        def _r(rel_):
            p = Path(rel_)
            return p if p.is_absolute() else workdir / p

        tensor_rel = task_outputs.get("elastic_tensor_json")
        return extract_t4_fields(
            stress_strain_csv_path=_r(rel),
            elastic_tensor_json_path=_r(tensor_rel) if tensor_rel else None,
            extraction_config=task["evaluator"].get("extraction_config", {}),
        )
    raise NotImplementedError(f"task_type={task_type!r}")


def _detect_dos_dat_relative(dos_dat_path: Path) -> bool:
    """Heuristic: was `dos.dat` already EF-shifted by the agent?

    Used only in --legacy-dos-detect mode for old runs.

    Rule: compare col-1 midpoint to header EFermi.
      - Raw QE dos.dat: col-1 是绝对能量，dos.x 默认窗口 [EF-Δ, EF+Δ] →
        midpoint ≈ EFermi
      - Agent 改过的: col-1 = E - EF → midpoint ≈ 0
    判定：|midpoint − EFermi| < |midpoint| → 绝对 (False)
          |midpoint| < |midpoint − EFermi| → 相对 (True)

    旧规则 `min<0 AND max>0` 对 EF<5 eV 材料（如 KF EF=4.66, dos.dat 范围
    [-0.336,+9.664]）false-positive：跨 0 但其实是 raw 绝对值。

    Fallback：拿不到 EFermi（header 无、值非有限）→ 回退到旧规则
    `min<0 AND max>0`，并且加额外要求 midpoint 离 0 在 ±1 eV 内（避开 KF
    那种 midpoint=4.66 但跨 0 的情况）。

    Edge cases:
      - col 1 < 2 numeric rows: False（保守，让正常流程处理）
    """
    try:
        e = []
        ef_header = None
        for line in dos_dat_path.read_text().splitlines():
            s = line.strip()
            if not s:
                continue
            if s.startswith("#"):
                # QE header e.g. "#  E (eV)   dos(E)     Int dos(E) EFermi =    4.664 eV"
                if ef_header is None and "EFermi" in s:
                    import re
                    m = re.search(r"EFermi\s*=\s*([+-]?\d+\.?\d*)", s)
                    if m:
                        try:
                            ef_header = float(m.group(1))
                        except ValueError:
                            pass
                continue
            parts = s.split()
            if len(parts) >= 2:
                try:
                    e.append(float(parts[0]))
                except ValueError:
                    continue
        if len(e) < 2:
            return False
        mid = (min(e) + max(e)) / 2.0
        if ef_header is not None and ef_header != 0.0:
            # 主路径：midpoint 更靠近 EFermi 还是 0？
            return abs(mid) < abs(mid - ef_header)
        # Fallback：无 EFermi header（罕见），用更严格的旧规则
        return (min(e) < 0 and max(e) > 0 and abs(mid) < 1.0)
    except Exception:
        return False


def _find_nscf_dos_out_legacy(workdir: Path, dos_dat_path: Path) -> Path | None:
    """Heuristic search for the DOS NSCF pw.x output in old runs.

    Walks the directory containing dos.dat for files whose name suggests NSCF
    output AND whose contents contain "the Fermi energy is" line.

    Returns: matching path or None.
    """
    candidates = []
    parent = dos_dat_path.parent
    # Common patterns: dos_nscf.out, nscf_dos.out, dos_nscf_out.out, nscf.out
    for name in ("dos_nscf.out", "nscf_dos.out", "dos_nscf_out.out",
                 "dscf.out", "nscf_dos_out.out", "nscf.out"):
        p = parent / name
        if p.exists():
            candidates.append(p)
    # Fallback: any .out file with "nscf" or "dos" in name
    for p in parent.glob("*.out"):
        if p not in candidates and ("nscf" in p.name.lower() or "dos" in p.name.lower()):
            candidates.append(p)
    # Pick first that has the Fermi line
    for c in candidates:
        try:
            if "the Fermi energy is" in c.read_text(errors="ignore"):
                return c
        except Exception:
            continue
    return None


def _brief(extracted: dict) -> dict:
    """Truncate dos_curve in printout."""
    out = dict(extracted)
    if isinstance(out.get("dos_curve"), dict):
        c = out["dos_curve"]
        out["dos_curve"] = f"<curve: {len(c.get('energy_ev', []))} points>"
    return out


def _json_default(o):
    if isinstance(o, (set, frozenset)):
        return list(o)
    return str(o)


if __name__ == "__main__":
    sys.exit(main())
