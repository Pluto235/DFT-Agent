"""Per-material driver for the T2 8-point scan pipeline.

Per scan point (8 points / material):

  1. SCF   — pw.x at (ecutwfc, kmesh) of this point
  2. NSCF  — same (ecutwfc, kmesh), all bands
  3. bands pw + bands.x — sample along seekpath path

After all 8 points complete, run convergence analysis (band_gap + Ef
末两点对比), pick the canonical point (e80_d50), and run a single dos.x there.

Workdir layout:

  workdir_t2/<material_id>/
    e30_d30/{scf, nscf, bands}/
    e40_d30/{scf, nscf, bands}/
    ...
    e80_d50/{scf, nscf, bands}/
    canonical_dos/
    result.json

Idempotent at scan-point + step granularity (each step's "JOB DONE."
output flag).
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

# Ensure dft-agent-bench root on sys.path
_THIS = Path(__file__).resolve()
_BENCH_REPO_ROOT = _THIS.parent.parent.parent.parent  # t2/t2_run.py → tools/gt_selfrun/t2 → tools/gt_selfrun → tools → dft-agent-bench/
if str(_BENCH_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_BENCH_REPO_ROOT))

from tools.gt_selfrun.t2 import t2_params, t2_extract  # noqa: E402

HERE = Path(__file__).resolve().parent  # tools/gt_selfrun/t2/
GT_SELFRUN_ROOT = HERE.parent  # tools/gt_selfrun/
BENCH_ROOT = GT_SELFRUN_ROOT.parent.parent  # → dft-agent-bench/
WORKSPACE_ROOT = BENCH_ROOT.parent.parent  # → ~/workspace/
PSEUDO_DIR = BENCH_ROOT / "pseudos" / "GBRV_USPP_PBE_v1.5"
T2_QUESTIONS_M1_DIR = BENCH_ROOT / "questions" / "M1_T2"
T1_CANONICAL_DIR = GT_SELFRUN_ROOT / "canonical_gt"
WORKDIR_ROOT = GT_SELFRUN_ROOT / "workdir_t2"

QE_WRAPPER_DIR = WORKSPACE_ROOT / "repos" / "TritonDFT" / "tritonDFT-src" / "QuantumE-gpu-wrapper"
QE_CPU_BIN_DIR = WORKSPACE_ROOT / "repos" / "TritonDFT" / "tritonDFT-src" / "QuantumE" / "bin"
# pw.x: GPU 模式用 podman wrapper；CPU 模式用 native binary 通过 mpirun -np N
PW_BIN_GPU = QE_WRAPPER_DIR / "pw.x"
PW_BIN_CPU = QE_CPU_BIN_DIR / "pw.x"
# bands.x / dos.x always use CPU build (GPU build doesn't ship them).
BANDS_BIN = QE_CPU_BIN_DIR / "bands.x"
DOS_BIN = QE_CPU_BIN_DIR / "dos.x"


def _pw_bin(mode: str) -> Path:
    return PW_BIN_CPU if mode == "cpu" else PW_BIN_GPU

# Per-step timeouts
SCF_TIMEOUT_SEC = 2 * 3600
NSCF_TIMEOUT_SEC = 4 * 3600
BANDS_PW_TIMEOUT_SEC = 6 * 3600
PP_TIMEOUT_SEC = 30 * 60


def material_id_to_paths(material_id: str) -> tuple[Path, Path]:
    """Return (T2_question_json, T1_canonical_gt_json) for a given material id.

    The public practice edition accepts M1_T2 identifiers only.
    """
    if material_id.startswith("M1_T2_"):
        prefix = "M1_T2_"
        t1_prefix = "M1_T1_"
        t2_dir = T2_QUESTIONS_M1_DIR
    else:
        raise ValueError(f"unsupported material_id: {material_id}")
    t2 = t2_dir / f"{material_id}.json"
    head = material_id[len(prefix):]
    if head.endswith("_bands_dos"):
        head = head[: -len("_bands_dos")]
    t1 = T1_CANONICAL_DIR / f"{t1_prefix}{head}_vc_relax.json"
    return t2, t1


@dataclass
class StepResult:
    name: str
    status: str            # "OK" | "CRASHED" | "TIMEOUT" | "SKIP"
    wall_time_sec: float
    returncode: int | None
    error: str | None = None


def _run_qe(
    bin_path: Path,
    input_file: Path,
    out_file: Path,
    err_file: Path,
    cwd: Path,
    gpu_id: int,
    timeout_sec: int,
    *,
    mode: str = "gpu",          # "gpu" | "cpu"
    cpu_mpi_ranks: int = 1,     # only used when mode="cpu" and use_mpi=True
    use_mpi: bool = True,       # bands.x / dos.x set False (single process)
) -> StepResult:
    """Run a QE binary; supports both GPU mode (podman wrapper / CUDA_VISIBLE_DEVICES)
    和 CPU mode (mpirun + native binary)。bands.x / dos.x 始终用 CPU build 单进程。

    Uses Popen + start_new_session=True + killpg on timeout 防止 zombie pw.x。
    """
    env = os.environ.copy()
    name = bin_path.name
    if mode == "cpu":
        # CPU 模式：避免 IB UAR exhaustion + 强制 TCP/共享内存 + 不绑核（让 OS 调度）
        env["OMPI_MCA_btl"] = "self,vader,tcp"
        env["OMPI_MCA_pml"] = "ob1"
        env["OMPI_MCA_btl_base_warn_component_unused"] = "0"
        env["CUDA_VISIBLE_DEVICES"] = ""
        if use_mpi:
            cmd = ["mpirun", "--bind-to", "none", "-np", str(cpu_mpi_ranks),
                   str(bin_path), "-in", str(input_file)]
        else:
            cmd = [str(bin_path), "-in", str(input_file)]
    else:
        env["CUDA_VISIBLE_DEVICES"] = str(gpu_id)
        cmd = [str(bin_path), "-in", str(input_file)]

    start = time.time()
    timed_out = False
    rc = -1
    # Popen + killpg: 防止 subprocess.run 超时但不实际杀掉 mpirun + pw.x 子进程
    import signal as _signal
    try:
        with open(out_file, "w") as so, open(err_file, "w") as se:
            proc = subprocess.Popen(
                cmd, stdout=so, stderr=se,
                cwd=str(cwd), env=env,
                start_new_session=True,
            )
            try:
                rc = proc.wait(timeout=timeout_sec)
            except subprocess.TimeoutExpired:
                timed_out = True
                try:
                    os.killpg(os.getpgid(proc.pid), _signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    pass
        wall = time.time() - start
    except Exception as e:  # noqa: BLE001
        wall = time.time() - start
        return StepResult(name=name, status="CRASHED",
                          wall_time_sec=wall, returncode=None,
                          error=f"{name} subprocess exception: {e}")

    if timed_out:
        return StepResult(name=name, status="TIMEOUT",
                          wall_time_sec=wall, returncode=None,
                          error=f"{name} timed out after {timeout_sec}s")
    if rc != 0:
        return StepResult(name=name, status="CRASHED",
                          wall_time_sec=wall, returncode=rc,
                          error=f"{name} exited with {rc}; see {err_file.name}")
    out_text = out_file.read_text(encoding="utf-8", errors="ignore")
    if "JOB DONE" not in out_text:
        return StepResult(name=name, status="CRASHED",
                          wall_time_sec=wall, returncode=rc,
                          error=f"{name} did not print 'JOB DONE.'")
    return StepResult(name=name, status="OK",
                      wall_time_sec=wall, returncode=rc)


def _step_done(out_file: Path) -> bool:
    if not out_file.exists():
        return False
    try:
        text = out_file.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return False
    return "JOB DONE" in text


def _cleanup_tmp_dirs(workdir: Path, *, failed: bool = False) -> int:
    """Cleanup workdir after result.json is written.

    OK case: delete only */tmp/ (charge density + wfc), preserve siblings
    (*.in / *.out / *.err / bands.dat / bands.dat.gnu / dos.dat / extract.json
    are kept for audit/regen).

    FAILED case: nuke the entire workdir except result.json. Reason: keeping
    .out files with stale 'JOB DONE' marks but no tmp/ poisons future re-runs
    (next attempt would skip SCF via _step_done but have no charge density
    for NSCF, causing immediate crash). canonical_gt_t2/<id>.json already
    captures the FAILED audit trail.

    Returns total bytes freed (best-effort).
    """
    freed = 0
    if failed:
        result_json = workdir / "result.json"
        keep = result_json.read_bytes() if result_json.exists() else None
        for child in workdir.iterdir():
            if child == result_json:
                continue
            try:
                if child.is_dir():
                    for f in child.rglob("*"):
                        if f.is_file():
                            try:
                                freed += f.stat().st_size
                            except OSError:
                                pass
                    shutil.rmtree(child, ignore_errors=True)
                else:
                    try:
                        freed += child.stat().st_size
                    except OSError:
                        pass
                    child.unlink()
            except OSError:
                pass
        if keep is not None:
            result_json.write_bytes(keep)
        return freed

    for tmp_dir in workdir.rglob("tmp"):
        if not tmp_dir.is_dir():
            continue
        try:
            for f in tmp_dir.rglob("*"):
                if f.is_file():
                    try:
                        freed += f.stat().st_size
                    except OSError:
                        pass
            shutil.rmtree(tmp_dir, ignore_errors=True)
        except OSError:
            pass
    return freed


def _mirror_tmp(src_tmp: Path, dst_tmp: Path) -> None:
    if not src_tmp.exists():
        return
    if dst_tmp.exists() and any(dst_tmp.iterdir()):
        return
    shutil.copytree(src_tmp, dst_tmp, dirs_exist_ok=True)


# seekpath symprec 逐级放松梯度 —— 必须与 evaluator 侧 _SYMPREC_LADDER 同值，
# 这样 GT 与 evaluator 落到同一标准化原胞（frac 坐标可比）。
_SYMPREC_LADDER = (1e-5, 1e-3, 1e-2, 5e-2)


def run_seekpath_path(geom: t2_params.T2Geometry) -> dict:
    """Compute seekpath path for our geometry.

    AFM 子晶格标号 'Fe1'/'Fe2' 剥尾号到基元素（同 t2_params._atomic_species_block），
    否则 Element('Fe1') 直接 ValueError。symprec 从 1e-5 逐级放松到命中题目已知的
    晶体学空间群 geom.target_spacegroup（噪声坐标在 1e-5 会塌成 P1）。返回值带
    symprec_used / spacegroup_number 供 canonical GT 记录所用基底。
    """
    import re
    import seekpath
    from pymatgen.core import Element
    cell = t2_params.build_pmg_lattice(geom).matrix.tolist()
    frac = [list(p) for p in geom.fractional_positions]
    numbers = [Element(re.sub(r'\d+$', '', s) or s).Z for s in geom.species]
    sp_input = (cell, frac, numbers)
    target = geom.target_spacegroup
    res, symprec_used = None, None
    for sp in _SYMPREC_LADDER:
        res = seekpath.get_path(sp_input, symprec=sp)
        symprec_used = sp
        sg = res.get("spacegroup_number")
        if (target is not None and sg == target) or (target is None and sg and sg > 1):
            break
    return {
        "path": [tuple(seg) for seg in res["path"]],
        "point_coords": {k: list(v) for k, v in res["point_coords"].items()},
        "symprec_used": symprec_used,
        "spacegroup_number": res.get("spacegroup_number"),
        "spacegroup_international": res.get("spacegroup_international"),
    }


def build_flat_seekpath_points(
    seekpath_path: list[tuple[str, str]],
    seekpath_point_coords: dict[str, list[float]],
) -> list[tuple[str, list[float]]]:
    flat: list[tuple[str, list[float]]] = []
    for a, b in seekpath_path:
        if not flat or flat[-1][0] != a:
            flat.append((a, list(seekpath_point_coords[a])))
        flat.append((b, list(seekpath_point_coords[b])))
    return flat


def _build_flat_with_n(
    flat: list[tuple[str, list[float]]],
    points_per_segment: int = 30,
) -> list[tuple[str, list[float], int]]:
    """Augment flat list with n_per_segment for crystal_b semantics."""
    out = []
    M = len(flat)
    for i, (lbl, kp) in enumerate(flat):
        n = points_per_segment if i < M - 1 else 1
        out.append((lbl, kp, n))
    return out


def run_one_scan_point(
    material_id: str,
    point: t2_params.ScanPoint,
    geom: t2_params.T2Geometry,
    pmg_lat,
    sp: dict,
    *,
    gpu_id: int = 0,
    force: bool = False,
    points_per_segment: int = 30,
    mode: str = "gpu",          # "gpu" | "cpu"
    cpu_mpi_ranks: int = 1,
) -> dict:
    """Run SCF + NSCF + bands.x for ONE scan point. Return per-point summary.

    Writes <workdir>/<material_id>/<point.key>/extract.json containing
    band_gap_ev, dos_e_fermi, vbm/cbm labels for that point.

    Idempotent: uses 'JOB DONE.' flag on each step's output.

    Returns:
        {
            "point_key": str,
            "ecutwfc_ry": float, "ecutrho_ry": float,
            "kmesh": [int,int,int], "kmesh_density": float,
            "nbnd": int,
            "round": int,
            "status": "OK" | "FAILED",
            "failed_step": str | None,
            "step_walls": {scf,nscf,bands_pw,bands_pp: float},
            "extracted": { band_gap_ev, vbm_ev, cbm_ev, vbm_k_label, cbm_k_label,
                          gap_type, dos_e_fermi },
        }
    """
    workdir = WORKDIR_ROOT / material_id / point.key
    workdir.mkdir(parents=True, exist_ok=True)
    nbnd = t2_params.estimate_nbnd(geom)
    kmesh = t2_params.kmesh_from_density(pmg_lat, point.kmesh_density)

    summary: dict = {
        "point_key": point.key,
        "ecutwfc_ry": point.ecutwfc,
        "ecutrho_ry": point.ecutrho,
        "kmesh": kmesh,
        "kmesh_density": point.kmesh_density,
        "nbnd": nbnd,
        "round": point.round,
        "status": "OK",
        "failed_step": None,
        "step_walls": {},
        "extracted": {},
    }

    # ---------------- 1. SCF ----------------
    scf_dir = workdir / "scf"
    scf_dir.mkdir(parents=True, exist_ok=True)
    scf_in = scf_dir / "scf.in"
    scf_out = scf_dir / "scf.out"
    scf_err = scf_dir / "scf.err"
    scf_in.write_text(
        t2_params.render_scf_input(
            geom, PSEUDO_DIR, kmesh,
            ecutwfc=point.ecutwfc, ecutrho=point.ecutrho,
        ),
        encoding="utf-8",
    )
    if not force and _step_done(scf_out):
        scf_res = StepResult("scf.x", "SKIP", 0.0, 0)
    else:
        scf_res = _run_qe(_pw_bin(mode), scf_in, scf_out, scf_err, scf_dir, gpu_id,
                          SCF_TIMEOUT_SEC,
                          mode=mode, cpu_mpi_ranks=cpu_mpi_ranks, use_mpi=True)
    summary["step_walls"]["scf"] = scf_res.wall_time_sec
    if scf_res.status not in ("OK", "SKIP"):
        summary["status"] = "FAILED"
        summary["failed_step"] = "scf"
        summary["error"] = scf_res.error
        _persist_extract(workdir, summary)
        return summary

    fermi_ev = t2_extract.extract_fermi_for_point(scf_out)

    # ---------------- 2. NSCF ----------------
    nscf_dir = workdir / "nscf"
    nscf_dir.mkdir(parents=True, exist_ok=True)
    _mirror_tmp(scf_dir / "tmp", nscf_dir / "tmp")
    nscf_in = nscf_dir / "nscf.in"
    nscf_out = nscf_dir / "nscf.out"
    nscf_err = nscf_dir / "nscf.err"
    nscf_in.write_text(
        t2_params.render_nscf_input(
            geom, PSEUDO_DIR, kmesh,
            nbnd=nbnd,
            ecutwfc=point.ecutwfc, ecutrho=point.ecutrho,
        ),
        encoding="utf-8",
    )
    if not force and _step_done(nscf_out):
        nscf_res = StepResult("pw.x(nscf)", "SKIP", 0.0, 0)
    else:
        nscf_res = _run_qe(_pw_bin(mode), nscf_in, nscf_out, nscf_err, nscf_dir,
                           gpu_id, NSCF_TIMEOUT_SEC,
                           mode=mode, cpu_mpi_ranks=cpu_mpi_ranks, use_mpi=True)
    summary["step_walls"]["nscf"] = nscf_res.wall_time_sec
    if nscf_res.status not in ("OK", "SKIP"):
        summary["status"] = "FAILED"
        summary["failed_step"] = "nscf"
        summary["error"] = nscf_res.error
        _persist_extract(workdir, summary)
        return summary

    # ---------------- 3a. bands pw ----------------
    bands_dir = workdir / "bands"
    bands_dir.mkdir(parents=True, exist_ok=True)
    _mirror_tmp(scf_dir / "tmp", bands_dir / "tmp")

    bands_pw_in = bands_dir / "bands_pw.in"
    bands_pw_out = bands_dir / "bands_pw.out"
    bands_pw_err = bands_dir / "bands_pw.err"
    bands_pw_in.write_text(
        t2_params.render_bands_pw_input(
            geom, PSEUDO_DIR,
            seekpath_path=sp["path"],
            seekpath_point_coords=sp["point_coords"],
            nbnd=nbnd,
            points_per_segment=points_per_segment,
            ecutwfc=point.ecutwfc, ecutrho=point.ecutrho,
        ),
        encoding="utf-8",
    )
    if not force and _step_done(bands_pw_out):
        bands_pw_res = StepResult("pw.x(bands)", "SKIP", 0.0, 0)
    else:
        bands_pw_res = _run_qe(_pw_bin(mode), bands_pw_in, bands_pw_out, bands_pw_err,
                               bands_dir, gpu_id, BANDS_PW_TIMEOUT_SEC,
                               mode=mode, cpu_mpi_ranks=cpu_mpi_ranks, use_mpi=True)
    summary["step_walls"]["bands_pw"] = bands_pw_res.wall_time_sec
    if bands_pw_res.status not in ("OK", "SKIP"):
        summary["status"] = "FAILED"
        summary["failed_step"] = "bands_pw"
        summary["error"] = bands_pw_res.error
        _persist_extract(workdir, summary)
        return summary

    # ---------------- 3b. bands.x (CPU) ----------------
    bands_pp_in = bands_dir / "bands_pp.in"
    bands_pp_out = bands_dir / "bands_pp.out"
    bands_pp_err = bands_dir / "bands_pp.err"
    bands_pp_in.write_text(
        t2_params.render_bands_pp_input(filband="bands.dat", lsym=False),
        encoding="utf-8",
    )
    bands_dat = bands_dir / "bands.dat"
    bands_dat_up = bands_dir / "bands.dat.up"  # nspin=2 时 bands.x 写入
    bands_dat_dn = bands_dir / "bands.dat.dn"
    bands_done = (
        _step_done(bands_pp_out)
        or bands_dat.exists()
        or (bands_dat_up.exists() and bands_dat_dn.exists())
    )
    if not force and bands_done:
        bands_pp_res = StepResult("bands.x", "SKIP", 0.0, 0)
    else:
        # bands.x 始终单进程跑（无 mpirun）；mode 影响 env vars 设置
        bands_pp_res = _run_qe(BANDS_BIN, bands_pp_in, bands_pp_out, bands_pp_err,
                               bands_dir, gpu_id, PP_TIMEOUT_SEC,
                               mode=mode, cpu_mpi_ranks=1, use_mpi=False)
    summary["step_walls"]["bands_pp"] = bands_pp_res.wall_time_sec
    if bands_pp_res.status not in ("OK", "SKIP"):
        summary["status"] = "FAILED"
        summary["failed_step"] = "bands_pp"
        summary["error"] = bands_pp_res.error
        _persist_extract(workdir, summary)
        return summary

    # ---------------- 4. Per-point extraction ----------------
    flat = build_flat_seekpath_points(sp["path"], sp["point_coords"])
    flat_with_n = _build_flat_with_n(flat, points_per_segment=points_per_segment)
    homo_lumo = t2_extract.parse_homo_lumo_ev(nscf_out)
    try:
        gap_info = t2_extract.extract_band_gap_from_bands_dat(
            bands_dat, flat_with_n, sp["point_coords"],
            fermi_ev=fermi_ev if fermi_ev is not None else 0.0,
            is_metal=geom.is_metal,
            homo_lumo_ev=homo_lumo,
        )
    except Exception as e:
        summary["status"] = "FAILED"
        summary["failed_step"] = "extract"
        summary["error"] = f"bands.dat parse error: {e}"
        _persist_extract(workdir, summary)
        return summary

    summary["extracted"] = {
        "band_gap_ev": gap_info["band_gap_ev"],
        "vbm_ev": gap_info["vbm_ev"],
        "cbm_ev": gap_info["cbm_ev"],
        "vbm_k_cryst": gap_info["vbm_k_cryst"],
        "cbm_k_cryst": gap_info["cbm_k_cryst"],
        "vbm_k_label": gap_info["vbm_k_label"],
        "cbm_k_label": gap_info["cbm_k_label"],
        "gap_type": gap_info["gap_type"],
        "dos_e_fermi": fermi_ev,
        "is_metal_band": gap_info["is_metal_band"],
    }
    _persist_extract(workdir, summary)
    return summary


def _persist_extract(workdir: Path, summary: dict) -> None:
    """Write the per-point extract.json sidecar."""
    (workdir / "extract.json").write_text(json.dumps(summary, indent=2),
                                          encoding="utf-8")


def run_canonical_dos(
    material_id: str,
    canonical_point_key: str,
    *,
    gpu_id: int = 0,
    force: bool = False,
    mode: str = "gpu",
    cpu_mpi_ranks: int = 1,
) -> dict:
    """Run dos.x once at the canonical point. Returns step summary.

    Reads the canonical point's NSCF tmp/ (charge density + wavefunction)
    and produces canonical_dos/dos.dat.
    """
    workdir = WORKDIR_ROOT / material_id
    canonical_pt_dir = workdir / canonical_point_key
    dos_dir = workdir / "canonical_dos"
    dos_dir.mkdir(parents=True, exist_ok=True)

    # Mirror canonical point's NSCF tmp into canonical_dos/tmp
    nscf_tmp = canonical_pt_dir / "nscf" / "tmp"
    if not nscf_tmp.exists():
        return {"status": "FAILED",
                "error": f"canonical NSCF tmp missing: {nscf_tmp}",
                "wall_time_sec": 0.0}
    _mirror_tmp(nscf_tmp, dos_dir / "tmp")

    dos_in = dos_dir / "dos.in"
    dos_out = dos_dir / "dos.out"
    dos_err = dos_dir / "dos.err"
    dos_in.write_text(
        t2_params.render_dos_input(fildos="dos.dat"),
        encoding="utf-8",
    )
    dos_dat = dos_dir / "dos.dat"
    if not force and dos_dat.exists():
        return {"status": "SKIP", "wall_time_sec": 0.0,
                "dos_dat_path": str(dos_dat)}

    # dos.x 始终单进程跑（无 mpirun）
    res = _run_qe(DOS_BIN, dos_in, dos_out, dos_err, dos_dir, gpu_id,
                  PP_TIMEOUT_SEC,
                  mode=mode, cpu_mpi_ranks=1, use_mpi=False)
    return {
        "status": res.status,
        "wall_time_sec": res.wall_time_sec,
        "returncode": res.returncode,
        "error": res.error,
        "dos_dat_path": str(dos_dat) if res.status == "OK" else None,
    }


def run_one_material(
    material_id: str,
    *,
    gpu_id: int = 0,
    force: bool = False,
    only_point: str | None = None,
    mode: str = "gpu",
    cpu_mpi_ranks: int = 1,
) -> dict:
    """Drive the full 8-point + canonical-dos pipeline for one material.

    Sequence:
      1. Load geometry (T1 canonical + T2 question)
      2. Compute seekpath path (geometry-only, point-independent)
      3. For each scan point (8 in total): SCF → NSCF → bands.x; write
         per-point extract.json
      4. If all 8 OK: run convergence analysis (t2_analyze) → classification
      5. Run dos.x at canonical point (e80_d50)
      6. Assemble result.json with per-point + canonical + classification.

    If only_point is set, only that point's 3 steps run (no canonical
    dos.x, no convergence analysis). Useful for debug / partial reruns.

    Idempotent: each step uses JOB DONE flag.
    """
    from tools.gt_selfrun.t2 import t2_analyze  # noqa: WPS433 — lazy

    t2_path, t1_path = material_id_to_paths(material_id)
    if not t2_path.exists():
        raise FileNotFoundError(t2_path)
    if not t1_path.exists():
        raise FileNotFoundError(t1_path)

    geom = t2_params.load_t2_geometry(t2_path, t1_path)
    pmg_lat = t2_params.build_pmg_lattice(geom)
    sp = run_seekpath_path(geom)

    workdir = WORKDIR_ROOT / material_id
    workdir.mkdir(parents=True, exist_ok=True)

    points = t2_params.scan_grid()
    if only_point is not None:
        points = [p for p in points if p.key == only_point]
        if not points:
            raise ValueError(f"only_point {only_point} not in scan grid")

    point_summaries: list[dict] = []
    overall_status = "OK"
    failed_at: str | None = None
    for p in points:
        s = run_one_scan_point(material_id, p, geom, pmg_lat, sp,
                               gpu_id=gpu_id, force=force,
                               mode=mode, cpu_mpi_ranks=cpu_mpi_ranks)
        point_summaries.append(s)
        if s["status"] != "OK":
            overall_status = "FAILED"
            if failed_at is None:
                failed_at = f"{p.key}/{s.get('failed_step')}"

    if only_point is not None:
        # No canonical step on partial run.
        return {
            "material_id": material_id,
            "status": overall_status,
            "failed_at": failed_at,
            "point_summaries": point_summaries,
            "is_metal": geom.is_metal,
        }

    if overall_status != "OK":
        result = {
            "material_id": material_id,
            "status": "FAILED",
            "failed_at": failed_at,
            "is_metal": geom.is_metal,
            "point_summaries": point_summaries,
        }
        (workdir / "result.json").write_text(json.dumps(result, indent=2),
                                             encoding="utf-8")
        _cleanup_tmp_dirs(workdir, failed=True)
        return result

    # All 8 points OK → convergence analysis
    classification = t2_analyze.classify_convergence(point_summaries)

    # Canonical dos.x
    canonical_key = t2_params.CANONICAL_POINT_KEY
    dos_summary = run_canonical_dos(material_id, canonical_key,
                                    gpu_id=gpu_id, force=force,
                                    mode=mode, cpu_mpi_ranks=cpu_mpi_ranks)
    if dos_summary["status"] not in ("OK", "SKIP"):
        result = {
            "material_id": material_id,
            "status": "FAILED",
            "failed_at": f"canonical_dos/{dos_summary.get('error')}",
            "is_metal": geom.is_metal,
            "point_summaries": point_summaries,
            "classification": classification,
            "dos_summary": dos_summary,
        }
        (workdir / "result.json").write_text(json.dumps(result, indent=2),
                                             encoding="utf-8")
        _cleanup_tmp_dirs(workdir, failed=True)
        return result

    # Pull canonical-point fields (the e80_d50 entry)
    canonical = next(s for s in point_summaries if s["point_key"] == canonical_key)

    total_wall = (
        sum(p["step_walls"].get("scf", 0) +
            p["step_walls"].get("nscf", 0) +
            p["step_walls"].get("bands_pw", 0) +
            p["step_walls"].get("bands_pp", 0)
            for p in point_summaries)
        + dos_summary["wall_time_sec"]
    )

    result = {
        "material_id": material_id,
        "status": "OK",
        "is_metal": geom.is_metal,
        "wall_time_sec": total_wall,
        "scan_points": [
            {
                "key": p["point_key"],
                "round": p["round"],
                "ecutwfc_ry": p["ecutwfc_ry"],
                "ecutrho_ry": p["ecutrho_ry"],
                "kmesh": p["kmesh"],
                "kmesh_density": p["kmesh_density"],
                "nbnd": p["nbnd"],
                "step_walls_sec": p["step_walls"],
                "band_gap_ev": p["extracted"].get("band_gap_ev"),
                "dos_e_fermi": p["extracted"].get("dos_e_fermi"),
                "vbm_k_label": p["extracted"].get("vbm_k_label"),
                "cbm_k_label": p["extracted"].get("cbm_k_label"),
                "gap_type": p["extracted"].get("gap_type"),
            }
            for p in point_summaries
        ],
        "classification": classification,
        "canonical": {
            "point_key": canonical_key,
            "band_gap_ev": canonical["extracted"]["band_gap_ev"],
            "vbm_ev": canonical["extracted"]["vbm_ev"],
            "cbm_ev": canonical["extracted"]["cbm_ev"],
            "vbm_k_cryst": canonical["extracted"]["vbm_k_cryst"],
            "cbm_k_cryst": canonical["extracted"]["cbm_k_cryst"],
            "vbm_k_label": canonical["extracted"]["vbm_k_label"],
            "cbm_k_label": canonical["extracted"]["cbm_k_label"],
            "gap_type": canonical["extracted"]["gap_type"],
            "dos_e_fermi": canonical["extracted"]["dos_e_fermi"],
            "dos_dat_path": dos_summary.get("dos_dat_path"),
            "kmesh": canonical["kmesh"],
        },
        "dos_params": {
            "sigma_ev": t2_params.DOS_SIGMA_EV,
            "de_ev": t2_params.DOS_DE_EV,
        },
    }
    (workdir / "result.json").write_text(json.dumps(result, indent=2),
                                         encoding="utf-8")
    _cleanup_tmp_dirs(workdir)
    return result
