#!/usr/bin/env python3
"""Batch-generate 172 题目 JSON from m1_m2_resolved.yaml + MP electronic structure.

Pure-script, no LLM. Each field derived mechanically from:
- YAML (bench/materials/m1_m2_resolved.yaml)
- Materials Project API (only for T2 electronic structure / DOS)
- pymatgen SpacegroupAnalyzer (for point_group / crystal_system)
- seekpath (for VBM/CBM k-label mapping)
- filesystem (pseudo .upf lookup)

决策来源：bench/materials/review_for_yang_round3.md
  A=A2, B=B1, C=C1, D=D2

用法：
    MP_API_KEY=xxx python bench/questions/generate.py [--limit N] [--task {T1,T2}]

输出：
- bench/questions/M{1,2}_T{1,2}/<id>.json
- bench/questions/M{1,2}_T2/gt_data/<id>/dos_reference.dat
- bench/questions/generation_errors.log
- bench/questions/generation_summary.md

！！！重要后处理（杨博 2026-05-18）：
本脚本对 T2 insulator/semi 仍用 MP 的 efermi 作 dos_curve 参考帧。但 evaluator
现在对 insulator 要求 GT 用 VBM 作参考。所以本脚本跑完后必须再跑一次：
    python tools/migrate_dos_efermi_to_vbm.py
把 53 道 insulator 的 dos_reference.dat 和 ground_truth.dos_e_fermi.value 都
换成 VBM-based（金属不动）。否则 insulator dos_curve F1 会被 EF convention 漂移污染。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import date
from pathlib import Path
from typing import Any

import numpy as np
import yaml

# ------------------------------------------------------------------
# 常量（checklist §3.4 / §5 / §7 / D2, A2 决策）
# ------------------------------------------------------------------
PSEUDO_FAMILY = "GBRV_USPP_PBE_v1.5"
PSEUDO_RELATIVISTIC = "scalar"

LATTICE_TOL = 0.010          # D2: relative 1%
ANGLE_TOL = 1.0              # D2: absolute 1°
BAND_GAP_TOL = 0.10          # A2: 0.10 eV
FERMI_TOL = 0.1
DOS_TOL = 0.1
VBM_CBM_THRESHOLD = 0.05     # B1: 超阈值标 OFF_PATH

T1_EXTRACT_FIELDS = [
    "a", "b", "c", "alpha", "beta", "gamma",
    "space_group_number", "point_group", "crystal_system",
]
T2_EXTRACT_FIELDS = [
    "band_gap_ev", "gap_type", "vbm_k_label", "cbm_k_label",
    "dos_e_fermi", "dos_curve",
]

T1_FIELD_WEIGHTS = {
    "a": 0.20, "b": 0.20, "c": 0.20,
    "alpha": 0.05, "beta": 0.05, "gamma": 0.05,
    "space_group_number": 0.25,
}
T2_FIELD_WEIGHTS_FULL = {
    "band_gap_ev": 0.40,
    "gap_type": 0.15,
    "vbm_k_label": 0.10,
    "cbm_k_label": 0.10,
    "dos_e_fermi": 0.05,
    "dos_curve": 0.20,
}

T1_INITIAL_STRUCT_NOTE = (
    "MP primitive cell (VASP+PAW PBE geometry), provided as initial structure. "
    "Agent should run QE vc-relax on this."
)
T2_INITIAL_STRUCT_NOTE = (
    "Starting geometry provided to agent. Agent should treat it as an initial structure "
    "(relaxed or not is not stated)."
)

T1_METADATA_NOTES = (
    "bench schema v1.2. P1: GT = initial_structure (MP VASP+PAW PBE lattice) + wide tolerance "
    "(D2: 1% relative / 1° absolute) absorbing QE+GBRV-USPP vs VASP+PAW methodology difference. "
    "P2 may self-run QE to replace GT; see review_for_yang_round3.md."
)
T2_METADATA_NOTES = (
    "bench schema v1.2. T2 k-path derived by agent via seekpath; GT k-labels derived by same seekpath "
    "on the initial structure and then matched to MP VBM/CBM frac_coords (B1: nearest, threshold 0.05, "
    "else OFF_PATH). DOS reference curve downloaded from MP (C1: missing → field dropped, weights renormalized)."
)

# ------------------------------------------------------------------
# 路径
# ------------------------------------------------------------------
HERE = Path(__file__).resolve().parent        # bench/questions/
BENCH_ROOT = HERE.parent                        # bench/
REPO_ROOT = BENCH_ROOT.parent                   # workspace/
RESOLVED_YAML = BENCH_ROOT / "materials" / "m1_m2_resolved.yaml"
PSEUDO_DIR = BENCH_ROOT / "pseudos" / PSEUDO_FAMILY
ERROR_LOG = HERE / "generation_errors.log"
SUMMARY = HERE / "generation_summary.md"


# ------------------------------------------------------------------
# 工具函数
# ------------------------------------------------------------------
def round_float(x: float, ndigits: int) -> float:
    """round 但保留 float 类型（避免 numpy scalar 写入 JSON 报错）"""
    return float(round(float(x), ndigits))


def load_resolved_yaml() -> list[dict[str, Any]]:
    """展平 resolved YAML，返回 86 个材料 dict（包含 tier_category 等便捷字段）"""
    doc = yaml.safe_load(RESOLVED_YAML.read_text(encoding="utf-8"))
    materials: list[dict[str, Any]] = []
    for tier_key in ("M1", "M2"):
        tier = doc.get(tier_key, {})
        for cat_key, cat in (tier.get("categories") or {}).items():
            for m in cat.get("materials", []):
                m["_tier"] = tier_key
                m["_category"] = cat_key
                materials.append(m)
    return materials


def build_primitive_structure(mat: dict[str, Any]):
    """从 YAML 的 initial_structure_from_mp 构造 pymatgen.Structure"""
    from pymatgen.core import Lattice, Structure
    s = mat["initial_structure_from_mp"]
    lat = Lattice.from_parameters(
        s["lattice_a"], s["lattice_b"], s["lattice_c"],
        s["alpha"], s["beta"], s["gamma"],
    )
    species = [site["element"] for site in s["sites"]]
    coords = [list(site["position"]) for site in s["sites"]]
    return Structure(lat, species, coords, coords_are_cartesian=False)


def pseudopotential_files_from_formula(formula: str) -> dict[str, str]:
    """从 formula 导出元素集 → 查 bench/pseudos/<family>/<Element>.upf"""
    from pymatgen.core import Composition
    comp = Composition(formula)
    out: dict[str, str] = {}
    for el in comp.elements:
        fname = f"{el.symbol}.upf"
        path = PSEUDO_DIR / fname
        if not path.exists():
            raise FileNotFoundError(f"pseudo missing: {path}")
        out[el.symbol] = fname
    return out


def material_id_split(yaml_id: str) -> tuple[str, str]:
    """'M1_Si' → ('M1', 'Si');  'M2_CaTiO3_tet' → ('M2', 'CaTiO3_tet')"""
    parts = yaml_id.split("_", 1)
    if len(parts) != 2 or parts[0] not in ("M1", "M2"):
        raise ValueError(f"bad YAML id: {yaml_id}")
    return parts[0], parts[1]


def question_id(yaml_id: str, task_tier: str, task_name: str) -> str:
    m_tier, rest = material_id_split(yaml_id)
    return f"{m_tier}_{task_tier}_{rest}_{task_name}"


def build_material_section(mat: dict[str, Any]) -> dict[str, Any]:
    """§3 material 节（T1/T2 共用）"""
    s = mat["initial_structure_from_mp"]
    atomic_positions = [
        {
            "element": site["element"],
            "position": [round_float(v, 6) for v in site["position"]],
        }
        for site in s["sites"]
    ]
    return {
        "formula": mat["formula"],
        "name": mat["name"],
        "structure": mat["structure_hint"],
        "space_group": mat["space_group"],
        "space_group_number": mat["space_group_number"],
        "atoms_per_primitive_cell": mat["atoms_per_primitive_cell"],
        # advisory_notes 设计上「起步全 null，M3+ 再单独维护启用清单」（见 schema/changelog_v1_to_v1.1.md
        # 第 50 行 + schema/prompt_generation_strategy.md 第 187 行）。yaml 里的 mat["notes"] 是材料库
        # 内部备注（Yang 内部审查决策、磁性原子、polymorph 区分等），不应该漏进 AI 看到的 prompt。
        # 之前误用 mat.get("notes") 导致 56 道题的内部备注漏进 prompt（其中 48 道在 baseline 中）。
        # M3+ 启用 advisory_notes 时另外加白名单 + 单独审查每条措辞。
        "advisory_notes": None,
        "reference_source": {
            "database": "Materials Project",
            "id": mat["mp_id"],
            "url": f"https://next-gen.materialsproject.org/materials/{mat['mp_id']}",
        },
        "initial_structure": {
            "source": mat["mp_id"],
            # note 由 task 层在调用前覆盖（T1 / T2 文本不同）
            "note": None,
            "lattice_type": "primitive",
            "lattice_constants_angstrom": {
                "a": round_float(s["lattice_a"], 3),
                "b": round_float(s["lattice_b"], 3),
                "c": round_float(s["lattice_c"], 3),
            },
            "lattice_angles_degree": {
                "alpha": round_float(s["alpha"], 3),
                "beta": round_float(s["beta"], 3),
                "gamma": round_float(s["gamma"], 3),
            },
            "atomic_positions_crystal": atomic_positions,
        },
        "pseudopotential": {
            "family": PSEUDO_FAMILY,
            "relativistic": PSEUDO_RELATIVISTIC,
            "files": pseudopotential_files_from_formula(mat["formula"]),
        },
    }


def build_difficulty(m_tier: str, task_tier: str) -> dict[str, Any]:
    return {
        "material_tier": m_tier,
        "task_tier": task_tier,
        "overall_level": None,
    }


# ------------------------------------------------------------------
# T1 构造
# ------------------------------------------------------------------
def build_t1(mat: dict[str, Any], generated_at: str) -> dict[str, Any]:
    from pymatgen.symmetry.analyzer import SpacegroupAnalyzer
    m_tier, _ = material_id_split(mat["id"])
    qid = question_id(mat["id"], "T1", "vc_relax")

    struct = build_primitive_structure(mat)
    sga = SpacegroupAnalyzer(struct, symprec=1e-3, angle_tolerance=5)
    point_group = sga.get_point_group_symbol()
    crystal_system = sga.get_crystal_system()

    material = build_material_section(mat)
    material["initial_structure"]["note"] = T1_INITIAL_STRUCT_NOTE

    # ground_truth = initial_structure 的 lattice（§6.1 补充：P1 权宜之计）
    init = material["initial_structure"]
    a_val = init["lattice_constants_angstrom"]["a"]
    b_val = init["lattice_constants_angstrom"]["b"]
    c_val = init["lattice_constants_angstrom"]["c"]
    al_val = init["lattice_angles_degree"]["alpha"]
    be_val = init["lattice_angles_degree"]["beta"]
    ga_val = init["lattice_angles_degree"]["gamma"]

    gt = {
        "a": {"value": a_val, "unit": "angstrom", "tolerance": LATTICE_TOL, "rule": "relative"},
        "b": {"value": b_val, "unit": "angstrom", "tolerance": LATTICE_TOL, "rule": "relative"},
        "c": {"value": c_val, "unit": "angstrom", "tolerance": LATTICE_TOL, "rule": "relative"},
        "alpha": {"value": al_val, "unit": "degree", "tolerance": ANGLE_TOL, "rule": "absolute"},
        "beta":  {"value": be_val, "unit": "degree", "tolerance": ANGLE_TOL, "rule": "absolute"},
        "gamma": {"value": ga_val, "unit": "degree", "tolerance": ANGLE_TOL, "rule": "absolute"},
        "space_group_number": {"value": mat["space_group_number"], "rule": "exact"},
        "point_group": {"value": point_group, "rule": "exact"},
        "crystal_system": {"value": crystal_system, "rule": "exact"},
    }

    return {
        "schema_version": "1.3",
        "id": qid,
        "difficulty": build_difficulty(m_tier, "T1"),
        "material": material,
        "task": {
            "type": "vc_relax",
            "prompt": None,
            "policy": {"network_allowed": False},
        },
        "evaluator": {
            "type": "qe_vc_relax_output",
            "extract_fields": T1_EXTRACT_FIELDS,
            "data_sources": {"primary": "relax.out"},
            "extraction_method": "pymatgen_spacegroup_analyzer",
            "extraction_config": {"symprec": 1e-3, "angle_tolerance": 5},
        },
        "ground_truth": gt,
        "scoring": {
            "aggregate": "all_fields_weighted",
            "field_weights": dict(T1_FIELD_WEIGHTS),
            "pass_criterion": "all_fields_passed",
            "partial_credit": True,
        },
        "convergence_evidence": None,
        "metadata": {
            "author": "weilin",
            "generated_at": generated_at,
            "reviewer": None,
            "gt_qe_version": None,
            "gt_machine": None,
            "gt_wall_time_sec": None,
            "notes": T1_METADATA_NOTES,
        },
    }


# ------------------------------------------------------------------
# T2 构造：需要 MP electronic structure
# ------------------------------------------------------------------
def map_k_to_label(frac_coords, seekpath_point_coords: dict[str, list[float]]) -> str:
    """B1：在 seekpath path_points 里找欧氏距离最近的 label，>阈值 → 'OFF_PATH'"""
    fc = np.array(frac_coords, dtype=float)
    best_label = None
    best_dist = float("inf")
    for label, coord in seekpath_point_coords.items():
        d = float(np.linalg.norm(fc - np.array(coord)))
        if d < best_dist:
            best_dist = d
            best_label = label
    if best_dist > VBM_CBM_THRESHOLD:
        return "OFF_PATH"
    return best_label or "OFF_PATH"


def run_seekpath(struct) -> dict[str, list[float]]:
    """seekpath.get_path 的 point_coords 子集（label → frac_coords）"""
    import seekpath
    cell = struct.lattice.matrix.tolist()
    frac = struct.frac_coords.tolist()
    numbers = [site.specie.Z for site in struct.sites]
    res = seekpath.get_path((cell, frac, numbers))
    return res["point_coords"]  # dict: label -> [fx, fy, fz]


def build_t2(
    mat: dict[str, Any],
    mpr,
    generated_at: str,
    dos_base_dir: Path,
) -> tuple[dict[str, Any], list[str]]:
    """返回 (json_dict, warnings)。warnings 是 non-fatal 提示（如 DOS 缺失）"""
    m_tier, _ = material_id_split(mat["id"])
    qid = question_id(mat["id"], "T2", "bands_dos")
    mp_id = mat["mp_id"]
    warnings: list[str] = []

    material = build_material_section(mat)
    material["initial_structure"]["note"] = T2_INITIAL_STRUCT_NOTE
    # NOTE (2026-05-19, 杨博 decision): T2 GT pipeline (canonical_gt_t2) 用
    # T1 vc-relax 弛豫后的 lattice 作输入；为了 prompt 与 GT 结构对齐，T2
    # questions 的 initial_structure.lattice 也要替换成 T1 relaxed 值。
    # build_material_section 默认拉的是 MP raw lattice → 跑完 generate.py 后
    # 必须再跑 tools/migrate_t2_initial_structure_to_t1_relaxed.py 把 lattice
    # 改为 T1 GT relaxed。否则未来 baseline run 的 T2 评分会有 systematic
    # bias（agent 看到 MP raw、GT 用 T1 relaxed，电子结构必然偏）。
    # 详见 schema/changelog_v1.3_to_v1.4.md / notes/2026-05-19-t2-prompt-...md

    # summary doc: band_gap, efermi, is_gap_direct（vbm/cbm 在 summary 里只是能量，非 k 点）
    sdoc = mpr.materials.summary.search(
        material_ids=[mp_id],
        fields=["band_gap", "efermi", "is_gap_direct", "is_metal"],
    )
    if not sdoc:
        raise RuntimeError(f"MP summary returned empty for {mp_id}")
    sdoc = sdoc[0]

    band_gap = getattr(sdoc, "band_gap", None)
    efermi = getattr(sdoc, "efermi", None)
    is_direct = getattr(sdoc, "is_gap_direct", None)
    is_metal = getattr(sdoc, "is_metal", None)

    # VBM / CBM 的 k 点坐标要从 BandStructure 对象拿
    vbm_frac = None
    cbm_frac = None
    if not is_metal:
        try:
            bs = mpr.materials.electronic_structure_bandstructure.get_bandstructure_from_material_id(mp_id)
            if bs is not None:
                vbm_entry = bs.get_vbm()
                cbm_entry = bs.get_cbm()
                if vbm_entry and vbm_entry.get("kpoint") is not None:
                    vbm_frac = list(vbm_entry["kpoint"].frac_coords)
                if cbm_entry and cbm_entry.get("kpoint") is not None:
                    cbm_frac = list(cbm_entry["kpoint"].frac_coords)
        except Exception as e:
            warnings.append(f"BandStructure 下载失败: {type(e).__name__}: {e}")

    # gap_type
    if is_metal or (band_gap is not None and band_gap < 1e-6):
        gap_type_val = "metal"
    elif band_gap is None:
        gap_type_val = None
    elif is_direct is True:
        gap_type_val = "direct"
    elif is_direct is False:
        gap_type_val = "indirect"
    else:
        gap_type_val = None

    # seekpath path points（在 initial structure 上）
    struct = build_primitive_structure(mat)
    point_coords = None
    if gap_type_val != "metal":
        try:
            point_coords = run_seekpath(struct)
        except Exception as e:
            warnings.append(f"seekpath 失败: {type(e).__name__}: {e}")

    if gap_type_val == "metal":
        vbm_label_val = "metal"
        cbm_label_val = "metal"
    else:
        vbm_label_val = map_k_to_label(vbm_frac, point_coords) if (point_coords and vbm_frac is not None) else None
        cbm_label_val = map_k_to_label(cbm_frac, point_coords) if (point_coords and cbm_frac is not None) else None

    # DOS 下载（C1）
    dos_ref_file_rel: str | None = None
    dos_available = False
    if efermi is not None:
        try:
            dos = mpr.materials.electronic_structure_dos.get_dos_from_material_id(mp_id)
            if dos is not None:
                dos_available = True
                dos_dir = dos_base_dir / qid
                dos_dir.mkdir(parents=True, exist_ok=True)
                out_path = dos_dir / "dos_reference.dat"
                _write_dos_reference(dos, efermi, out_path)
                dos_ref_file_rel = str(out_path.relative_to(REPO_ROOT))
        except Exception as e:
            warnings.append(f"DOS 下载失败: {type(e).__name__}: {e}")

    # ground_truth 组装
    gt: dict[str, Any] = {
        "band_gap_ev": {
            "value": float(band_gap) if band_gap is not None else None,
            "unit": "eV",
            "tolerance": BAND_GAP_TOL,
            "rule": "absolute",
        },
        "gap_type": {"value": gap_type_val, "rule": "exact"},
        "vbm_k_label": {"value": vbm_label_val, "rule": "exact"},
        "cbm_k_label": {"value": cbm_label_val, "rule": "exact"},
        "dos_e_fermi": {
            "value": float(efermi) if efermi is not None else None,
            "unit": "eV",
            "tolerance": FERMI_TOL,
            "rule": "absolute",
        },
    }
    if dos_available:
        gt["dos_curve"] = {
            "value": {"ref_file": dos_ref_file_rel},
            "unit": "states/eV/fu",
            "tolerance": DOS_TOL,
            "rule": "curve_f1",
        }
        field_weights = dict(T2_FIELD_WEIGHTS_FULL)
    else:
        # C1：删掉 dos_curve 字段 + field_weights 重归一化
        field_weights = {k: v for k, v in T2_FIELD_WEIGHTS_FULL.items() if k != "dos_curve"}
        ssum = sum(field_weights.values())
        field_weights = {k: round_float(v / ssum, 4) for k, v in field_weights.items()}
        warnings.append("dos_curve 未生成，weights 已重归一化")

    return {
        "schema_version": "1.3",
        "id": qid,
        "difficulty": build_difficulty(m_tier, "T2"),
        "material": material,
        "task": {
            "type": "bands_dos",
            "prompt": None,
            "policy": {"network_allowed": False},
        },
        "evaluator": {
            "type": "qe_bands_dos_combined",
            "extract_fields": T2_EXTRACT_FIELDS,
            "data_sources": {
                "bands_out": "bands.out",
                "bands_data": "bands.dat",
                "dos_data": "dos.dat",
            },
            "extraction_method": "custom",
            "extraction_config": {
                "dos_energy_window_ev": [-5, 5],
                "k_label_source": "seekpath",
            },
        },
        "ground_truth": gt,
        "scoring": {
            "aggregate": "all_fields_weighted",
            "field_weights": field_weights,
            "pass_criterion": "all_fields_passed",
            "partial_credit": True,
        },
        "convergence_evidence": None,
        "metadata": {
            "author": "weilin",
            "generated_at": generated_at,
            "reviewer": None,
            "gt_qe_version": None,
            "gt_machine": None,
            "gt_wall_time_sec": None,
            "notes": T2_METADATA_NOTES,
        },
    }, warnings


def _write_dos_reference(dos, efermi: float, out_path: Path) -> None:
    """裁 E_F ± 5 eV 窗口，写 'E_eV  DOS_states_per_eV_per_fu' 每行一条"""
    energies = np.array(dos.energies) - float(efermi)
    # pymatgen CompleteDos.densities 的 key 是 Spin 枚举；总 DOS = 所有 spin 相加
    total = np.zeros_like(energies, dtype=float)
    for _, arr in dos.densities.items():
        total = total + np.array(arr, dtype=float)
    mask = (energies >= -5.0) & (energies <= 5.0)
    E = energies[mask]
    D = total[mask]
    with out_path.open("w", encoding="utf-8") as f:
        f.write("# E_eV(relative_to_EF)  DOS_states_per_eV_per_fu\n")
        for e, d in zip(E, D):
            f.write(f"{float(e):.6f}  {float(d):.6f}\n")


# ------------------------------------------------------------------
# 主流程
# ------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None, help="仅跑前 N 个材料（调试用）")
    ap.add_argument("--task", choices=["T1", "T2", "both"], default="both")
    ap.add_argument("--only-id", type=str, default=None, help="仅跑指定 YAML id（例 M1_Si）")
    args = ap.parse_args()

    if not os.environ.get("MP_API_KEY") and args.task != "T1":
        print("ERROR: MP_API_KEY 未设，T2 无法下载 MP 电子结构。", file=sys.stderr)
        return 2

    materials = load_resolved_yaml()
    if args.only_id:
        materials = [m for m in materials if m["id"] == args.only_id]
        if not materials:
            print(f"ERROR: YAML id {args.only_id} 未找到", file=sys.stderr)
            return 2
    if args.limit:
        materials = materials[: args.limit]
    print(f"[gen] 处理 {len(materials)} 个材料；task={args.task}")

    generated_at = date.today().isoformat()
    err_lines: list[str] = []
    stats = {"T1_ok": 0, "T1_err": 0, "T2_ok": 0, "T2_err": 0, "T2_warn": 0}

    mpr = None
    if args.task in ("T2", "both"):
        from mp_api.client import MPRester
        mpr = MPRester(os.environ["MP_API_KEY"])

    try:
        for idx, mat in enumerate(materials, 1):
            yaml_id = mat["id"]
            m_tier, _ = material_id_split(yaml_id)
            print(f"[{idx}/{len(materials)}] {yaml_id} …")

            # T1
            if args.task in ("T1", "both"):
                try:
                    j = build_t1(mat, generated_at)
                    out = HERE / f"{m_tier}_T1" / f"{j['id']}.json"
                    out.write_text(json.dumps(j, indent=2, ensure_ascii=False), encoding="utf-8")
                    stats["T1_ok"] += 1
                except Exception as e:
                    stats["T1_err"] += 1
                    err_lines.append(f"T1\t{yaml_id}\t{type(e).__name__}\t{e}")
                    print(f"  T1 ERR: {e}", file=sys.stderr)

            # T2
            if args.task in ("T2", "both"):
                dos_dir = HERE / f"{m_tier}_T2" / "gt_data"
                try:
                    j, warns = build_t2(mat, mpr, generated_at, dos_dir)
                    out = HERE / f"{m_tier}_T2" / f"{j['id']}.json"
                    out.write_text(json.dumps(j, indent=2, ensure_ascii=False), encoding="utf-8")
                    stats["T2_ok"] += 1
                    if warns:
                        stats["T2_warn"] += 1
                        for w in warns:
                            err_lines.append(f"T2\t{yaml_id}\tWARN\t{w}")
                    time.sleep(0.1)  # 轻微 rate-limit
                except Exception as e:
                    stats["T2_err"] += 1
                    err_lines.append(f"T2\t{yaml_id}\t{type(e).__name__}\t{e}")
                    print(f"  T2 ERR: {e}", file=sys.stderr)
    finally:
        if mpr is not None:
            try:
                mpr.session.close()
            except Exception:
                pass

    # 写错误 log + 总结
    if err_lines:
        ERROR_LOG.write_text("\n".join(err_lines) + "\n", encoding="utf-8")
    else:
        if ERROR_LOG.exists():
            ERROR_LOG.unlink()

    summary = [
        f"# Bench 题目生成 summary（{generated_at}）",
        "",
        f"- 总材料数：{len(materials)}",
        f"- T1 成功：{stats['T1_ok']}；失败：{stats['T1_err']}",
        f"- T2 成功：{stats['T2_ok']}；失败：{stats['T2_err']}；有 warning（缺 DOS 等）：{stats['T2_warn']}",
        "",
        "失败 / warning 详情见 `generation_errors.log`" if err_lines else "无错误。",
    ]
    SUMMARY.write_text("\n".join(summary) + "\n", encoding="utf-8")

    print()
    print("\n".join(summary))
    return 0 if stats["T1_err"] == 0 and stats["T2_err"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
