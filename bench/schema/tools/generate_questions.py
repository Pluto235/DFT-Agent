#!/usr/bin/env python3
"""
generate_questions.py — v1.2 bench 题目 JSON 批量生成脚本

流程：
  materials/{M1,M2}.yaml
    -> MP API 按 formula 查询 + expected_space_group 过滤 + energy_above_hull 升序取最低
    -> 提取 primitive structure + symmetry + band_gap + is_gap_direct
    -> 按 v1.2 schema 渲染 T1 (vc_relax) + T2 (bands_dos) 题目 JSON
    -> 写入 {out-dir}/M{i}_T{j}/{material_id}_{task}.json

GT 填写策略（本版本）：
  T1: a / b / c / alpha / beta / gamma / space_group_number / point_group / crystal_system
      均从 MP 的 primitive structure 直接填入（MP 背后是 VASP PBE 弛豫，与我们 QE+GBRV 跑出来会有
      小偏差，tolerance = 0.005 relative 能覆盖）
  T2: band_gap_ev / gap_type 从 MP 直接填入
      vbm_k_label / cbm_k_label / dos_e_fermi / dos_curve 暂填 "TBD_P1_nscf"
      （这些需要本地 QE nscf + seekpath 后处理才能产出；本脚本不负责）

convergence_evidence: 统一 null（v1.2 M1-M2 × T1-T2 策略，见 schema 文档）

使用：
  export MP_API_KEY=...
  python generate_questions.py \\
      --materials doc/reports/bench_schema_v1/materials/M1.yaml \\
      --tier M1 \\
      --out-dir doc/reports/bench_schema_v1/questions \\
      --tasks T1,T2

  可选 flags:
    --only <id1>,<id2>      只生成特定材料
    --skip-existing         跳过已有 JSON
    --dry-run               不写文件，只打印会生成什么
    --verbose               多打印 MP 查询细节

依赖：
  pip install mp-api pymatgen pyyaml
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import OrderedDict
from pathlib import Path
from typing import Any, Optional

import yaml


SCHEMA_VERSION = "1.2"
PSEUDOPOTENTIAL_FAMILY = "GBRV_USPP_PBE_v1.5"
RELATIVISTIC = "scalar"
AUTHOR = "weilin (auto-generated)"
GENERATED_AT = "2026-04-23"

TBD_NSCF = "TBD_P1_nscf"  # T2 需本地 nscf + seekpath 后处理填入的占位

# T1 评分权重与 tolerance（与 examples 保持一致）
T1_SCORING = {
    "aggregate": "all_fields_weighted",
    "field_weights": {
        "a": 0.15, "b": 0.15, "c": 0.15,
        "alpha": 0.05, "beta": 0.05, "gamma": 0.05,
        "space_group_number": 0.25,
        "point_group": 0.10,
        "crystal_system": 0.05,
    },
    "pass_criterion": "all_fields_passed",
    "partial_credit": True,
}

T1_TOL = {
    "a": 0.005, "b": 0.005, "c": 0.005,
    "alpha": 0.5, "beta": 0.5, "gamma": 0.5,
}

# T2 评分
T2_SCORING = {
    "aggregate": "all_fields_weighted",
    "field_weights": {
        "band_gap_ev": 0.40,
        "gap_type": 0.15,
        "vbm_k_label": 0.10,
        "cbm_k_label": 0.10,
        "dos_e_fermi": 0.05,
        "dos_curve": 0.20,
    },
    "pass_criterion": "all_fields_passed",
    "partial_credit": True,
}


# ---------- 工具函数 ----------

def normalize_sg_symbol(sym: Optional[str]) -> str:
    """统一空间群符号：Unicode 连字符 U+2212 → ASCII '-'；去空白。"""
    if sym is None:
        return ""
    return sym.replace("−", "-").replace(" ", "")


def load_materials(path: Path) -> list[dict]:
    with open(path) as f:
        data = yaml.safe_load(f)
    return data["materials"]


def element_pseudo_filename(element: str) -> str:
    """元素 → pseudos/GBRV_USPP_PBE_v1.5/ 下的 UPF 文件名约定。

    约定使用简短名 {Element}.upf（与 examples 保持一致）；实际 GBRV UPF
    文件名如 'si_pbe_v1.uspp.F.UPF'，P1 部署时建好软链或重命名。
    """
    return f"{element}.upf"


def infer_structure_type(mp_doc) -> str:
    """从 MP 的 symmetry.crystal_system + structure 粗粒度推一个 structure_type 字符串。

    用于 prompt 描述（如 'diamond'、'zincblende'、'rocksalt'、...）。
    这里用几条启发式规则，不保证完全对，需 reviewer 审。
    """
    n_sites = mp_doc.nsites  # primitive
    cs = (mp_doc.symmetry.crystal_system or "").lower()
    sg = normalize_sg_symbol(mp_doc.symmetry.symbol)
    elements = sorted(set(str(el) for el in mp_doc.elements))
    n_el = len(elements)

    # 元素固体
    if n_el == 1:
        if sg == "Fd-3m" and n_sites == 2:
            return "diamond"
        if sg == "Fm-3m":
            return "fcc"
        if sg == "Im-3m":
            return "bcc"
        if sg == "P6_3/mmc":
            return "hcp"
        return cs or "elemental"

    # 二元化合物 (1:1)
    if n_el == 2 and n_sites == 2:
        if sg == "Fm-3m":
            return "rocksalt"
        if sg == "F-43m":
            return "zincblende"
        if sg == "Pm-3m":
            return "CsCl-type"
    if n_el == 2 and n_sites == 4 and sg == "P6_3mc":
        return "wurtzite"

    # 钙钛矿
    if n_el == 3 and sg == "Pm-3m" and n_sites == 5:
        return "cubic perovskite"
    if n_el == 3 and sg in ("Pnma", "Pbnm") and n_sites == 20:
        return "orthorhombic perovskite"

    # Spinel
    if sg == "Fd-3m" and n_sites == 14 and n_el == 3:
        return "spinel"

    # Fallback：用 crystal_system 字符串
    return cs or "unspecified"


def render_structure_fields(structure) -> OrderedDict:
    """把 pymatgen Structure 渲染成 JSON schema 的 initial_structure 字段值。"""
    lat = structure.lattice
    atoms = []
    for site in structure:
        atoms.append(OrderedDict([
            ("element", site.specie.symbol),
            ("position", [round(c, 6) for c in site.frac_coords.tolist()]),
        ]))
    return OrderedDict([
        ("lattice_type", "primitive"),
        ("lattice_constants_angstrom", OrderedDict([
            ("a", round(lat.a, 4)),
            ("b", round(lat.b, 4)),
            ("c", round(lat.c, 4)),
        ])),
        ("lattice_angles_degree", OrderedDict([
            ("alpha", round(lat.alpha, 4)),
            ("beta", round(lat.beta, 4)),
            ("gamma", round(lat.gamma, 4)),
        ])),
        ("atomic_positions_crystal", atoms),
    ])


def render_pseudopotential(elements: list[str], note: Optional[str] = None) -> OrderedDict:
    d = OrderedDict([
        ("family", PSEUDOPOTENTIAL_FAMILY),
        ("relativistic", RELATIVISTIC),
        ("files", OrderedDict((el, element_pseudo_filename(el)) for el in elements)),
    ])
    if note:
        d["note"] = note
    return d


# ---------- MP 查询 ----------

def query_mp(mpr, spec: dict, verbose: bool = False):
    """按材料 spec 查询 MP，返回单条 summary doc（pick lowest energy_above_hull）。

    过滤顺序：
      1. 若有 mp_id_hint，直接按 id 查
      2. 按 formula 查，可能多条
      3. 若 spec 给了 expected_space_group：过滤掉不匹配的
      4. 若 spec 给了 expected_atoms_per_primitive_cell：过滤掉不匹配的（警告而不是硬过滤）
      5. 按 energy_above_hull 升序排序，取第一条
    """
    mp_id_hint = spec.get("mp_id_hint")
    formula = spec["formula"]
    expected_sg = normalize_sg_symbol(spec.get("expected_space_group")) or None
    expected_n = spec.get("expected_atoms_per_primitive_cell")

    if mp_id_hint:
        docs = mpr.materials.summary.search(material_ids=[mp_id_hint])
        if docs:
            if verbose:
                print(f"  [hint hit] {mp_id_hint} -> {docs[0].formula_pretty}")
            return docs[0]
        else:
            print(f"  [warn] mp_id_hint {mp_id_hint} 未找到，回退按 formula 查", file=sys.stderr)

    docs = mpr.materials.summary.search(formula=formula)
    if not docs:
        return None

    if expected_sg:
        exact_sg_matches = [d for d in docs if normalize_sg_symbol(d.symmetry.symbol) == expected_sg]
        if exact_sg_matches:
            docs = exact_sg_matches
        else:
            print(f"  [warn] {formula}: 无 MP 条目匹配 expected_space_group={expected_sg}，退回全部 {len(docs)} 条", file=sys.stderr)

    if expected_n:
        n_matches = [d for d in docs if d.nsites == expected_n]
        if n_matches:
            docs = n_matches
        else:
            print(f"  [warn] {formula}: 无条目匹配 expected_atoms_per_primitive_cell={expected_n}（实际 nsites 分布 {sorted(set(d.nsites for d in docs))}）", file=sys.stderr)

    def hull_key(d):
        return d.energy_above_hull if d.energy_above_hull is not None else 9999.0
    docs.sort(key=hull_key)

    picked = docs[0]
    if verbose:
        print(f"  [picked] {formula} -> {picked.material_id} (SG={picked.symmetry.symbol}, nsites={picked.nsites}, E_hull={picked.energy_above_hull})")
    return picked


# ---------- JSON 生成 ----------

def build_material_block(spec: dict, mp_doc, structure) -> OrderedDict:
    elements = sorted({site.specie.symbol for site in structure})
    structure_type = infer_structure_type(mp_doc)
    return OrderedDict([
        ("formula", mp_doc.formula_pretty),
        ("name", f"{mp_doc.formula_pretty} ({structure_type})"),
        ("structure", structure_type),
        ("space_group", normalize_sg_symbol(mp_doc.symmetry.symbol)),
        ("space_group_number", mp_doc.symmetry.number),
        ("atoms_per_primitive_cell", mp_doc.nsites),
        ("advisory_notes", None),
        ("reference_source", OrderedDict([
            ("database", "Materials Project"),
            ("id", mp_doc.material_id),
            ("url", f"https://next-gen.materialsproject.org/materials/{mp_doc.material_id}"),
        ])),
        ("initial_structure", OrderedDict([
            ("source", mp_doc.material_id),
            ("note", spec.get("polymorph_note")),
            *render_structure_fields(structure).items(),
        ])),
        ("pseudopotential", render_pseudopotential(elements)),
    ])


def build_t1_json(spec: dict, mp_doc, tier: str) -> OrderedDict:
    structure = mp_doc.structure  # primitive（MPRester 默认返回 conventional，但 summary 给的是 structure），再做 primitivization
    sga = _spacegroup_analyzer(structure)
    primitive = sga.get_primitive_standard_structure()
    lat = primitive.lattice

    material_block = build_material_block(spec, mp_doc, primitive)

    id_str = f"{tier}_T1_{spec['id']}_vc_relax"

    ground_truth = OrderedDict([
        ("a", {"value": round(lat.a, 4), "unit": "angstrom", "tolerance": T1_TOL["a"], "rule": "relative"}),
        ("b", {"value": round(lat.b, 4), "unit": "angstrom", "tolerance": T1_TOL["b"], "rule": "relative"}),
        ("c", {"value": round(lat.c, 4), "unit": "angstrom", "tolerance": T1_TOL["c"], "rule": "relative"}),
        ("alpha", {"value": round(lat.alpha, 4), "unit": "degree", "tolerance": T1_TOL["alpha"], "rule": "absolute"}),
        ("beta", {"value": round(lat.beta, 4), "unit": "degree", "tolerance": T1_TOL["beta"], "rule": "absolute"}),
        ("gamma", {"value": round(lat.gamma, 4), "unit": "degree", "tolerance": T1_TOL["gamma"], "rule": "absolute"}),
        ("space_group_number", {"value": mp_doc.symmetry.number, "rule": "exact"}),
        ("point_group", {"value": mp_doc.symmetry.point_group, "rule": "exact"}),
        ("crystal_system", {"value": str(mp_doc.symmetry.crystal_system).lower(), "rule": "exact"}),
    ])

    return OrderedDict([
        ("schema_version", SCHEMA_VERSION),
        ("id", id_str),
        ("difficulty", OrderedDict([
            ("material_tier", tier),
            ("task_tier", "T1"),
            ("overall_level", None),
        ])),
        ("material", material_block),
        ("task", OrderedDict([("type", "vc_relax"), ("prompt", None)])),
        ("evaluator", OrderedDict([
            ("type", "qe_vc_relax_output"),
            ("extract_fields", list(ground_truth.keys())),
            ("data_sources", {"primary": "relax.out"}),
            ("extraction_method", "pymatgen_spacegroup_analyzer"),
            ("extraction_config", {"symprec": 1e-3, "angle_tolerance": 5}),
        ])),
        ("ground_truth", ground_truth),
        ("scoring", T1_SCORING),
        ("convergence_evidence", None),
        ("metadata", OrderedDict([
            ("author", AUTHOR),
            ("generated_at", GENERATED_AT),
            ("reviewer", None),
            ("gt_qe_version", None),
            ("gt_machine", None),
            ("gt_wall_time_sec", None),
            ("notes", f"由 generate_questions.py 自动生成；GT 取自 MP {mp_doc.material_id}（PBE 弛豫结构）"),
        ])),
    ])


def build_t2_json(spec: dict, mp_doc, tier: str) -> OrderedDict:
    structure = mp_doc.structure
    sga = _spacegroup_analyzer(structure)
    primitive = sga.get_primitive_standard_structure()

    material_block = build_material_block(spec, mp_doc, primitive)

    id_str = f"{tier}_T2_{spec['id']}_bands_dos"

    gap_type = "direct" if mp_doc.is_gap_direct else "indirect"

    ground_truth = OrderedDict([
        ("band_gap_ev", {
            "value": round(float(mp_doc.band_gap), 4),
            "unit": "eV",
            "tolerance": 0.05,
            "rule": "absolute",
            "note": f"MP {mp_doc.material_id} PBE band_gap"
        }),
        ("gap_type", {"value": gap_type, "rule": "exact"}),
        ("vbm_k_label", {"value": TBD_NSCF, "rule": "exact", "note": "需本地 nscf + seekpath 后处理填入"}),
        ("cbm_k_label", {"value": TBD_NSCF, "rule": "exact", "note": "需本地 nscf + seekpath 后处理填入"}),
        ("dos_e_fermi", {"value": TBD_NSCF, "unit": "eV", "tolerance": 0.1, "rule": "absolute"}),
        ("dos_curve", {
            "value": {"ref_file": f"gt_data/{tier}_T2_{spec['id']}/dos_reference.dat"},
            "unit": "states/eV/fu",
            "tolerance": 0.1 if tier == "M1" else 0.15,
            "rule": "curve_l2",
            "note": "需本地 nscf + dos.x 后处理产出"
        }),
    ])

    return OrderedDict([
        ("schema_version", SCHEMA_VERSION),
        ("id", id_str),
        ("difficulty", OrderedDict([
            ("material_tier", tier),
            ("task_tier", "T2"),
            ("overall_level", None),
        ])),
        ("material", material_block),
        ("task", OrderedDict([("type", "bands_dos"), ("prompt", None)])),
        ("evaluator", OrderedDict([
            ("type", "qe_bands_dos_combined"),
            ("extract_fields", list(ground_truth.keys())),
            ("data_sources", {
                "bands_out": "bands.out",
                "bands_data": "bands.dat",
                "dos_data": "dos.dat",
            }),
            ("extraction_method", "custom"),
            ("extraction_config", {
                "dos_energy_window_ev": [-5, 5],
                "k_label_source": "seekpath",
            }),
        ])),
        ("ground_truth", ground_truth),
        ("scoring", T2_SCORING),
        ("convergence_evidence", None),
        ("metadata", OrderedDict([
            ("author", AUTHOR),
            ("generated_at", GENERATED_AT),
            ("reviewer", None),
            ("gt_qe_version", None),
            ("gt_machine", None),
            ("gt_wall_time_sec", None),
            ("notes", f"由 generate_questions.py 自动生成；band_gap / gap_type 取自 MP {mp_doc.material_id}；vbm/cbm k-label、dos_e_fermi、dos_curve 占位 TBD_P1_nscf，需本地 nscf + seekpath 补"),
        ])),
    ])


def _spacegroup_analyzer(structure):
    # 延迟导入，避免 mp_api 不可用时 pymatgen 也报错
    from pymatgen.symmetry.analyzer import SpacegroupAnalyzer
    return SpacegroupAnalyzer(structure, symprec=1e-3, angle_tolerance=5)


# ---------- main ----------

def write_json(obj, path: Path, dry_run: bool = False):
    if dry_run:
        print(f"  [dry-run] would write {path}")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(obj, f, indent=2, ensure_ascii=False)
        f.write("\n")


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--materials", required=True, type=Path, help="材料 YAML 文件路径")
    p.add_argument("--tier", required=True, choices=["M1", "M2", "M3", "M4", "M5"], help="材料层级标签（写入 difficulty.material_tier）")
    p.add_argument("--out-dir", required=True, type=Path, help="题目输出根目录，下面按 Mi_Tj 分子目录")
    p.add_argument("--tasks", default="T1,T2", help="逗号分隔的任务列表，如 T1,T2")
    p.add_argument("--only", default=None, help="逗号分隔的 material id 白名单（只生成这些）")
    p.add_argument("--skip-existing", action="store_true", help="若目标 JSON 已存在则跳过")
    p.add_argument("--dry-run", action="store_true", help="不实际写文件")
    p.add_argument("--verbose", action="store_true")
    return p.parse_args()


def main():
    args = parse_args()
    api_key = os.environ.get("MP_API_KEY")
    if not api_key:
        print("ERROR: 环境变量 MP_API_KEY 未设置", file=sys.stderr)
        sys.exit(1)

    try:
        from mp_api.client import MPRester
    except ImportError:
        print("ERROR: 需要安装 mp-api: pip install mp-api", file=sys.stderr)
        sys.exit(1)

    materials = load_materials(args.materials)
    tasks = [t.strip() for t in args.tasks.split(",")]
    only = set(s.strip() for s in args.only.split(",")) if args.only else None

    total_written = 0
    total_skipped = 0
    total_failed = 0

    with MPRester(api_key) as mpr:
        for spec in materials:
            if only and spec["id"] not in only:
                continue
            print(f"\n→ {spec['id']} (formula={spec['formula']}, tier={args.tier})")

            mp_doc = query_mp(mpr, spec, verbose=args.verbose)
            if mp_doc is None:
                print(f"  [fail] 无法在 MP 查到 {spec['formula']}")
                total_failed += 1
                continue

            for task in tasks:
                out_path = args.out_dir / f"{args.tier}_{task}" / f"{spec['id']}_{task_suffix(task)}.json"

                if args.skip_existing and out_path.exists():
                    print(f"  [skip] {out_path} 已存在")
                    total_skipped += 1
                    continue

                try:
                    if task == "T1":
                        obj = build_t1_json(spec, mp_doc, args.tier)
                    elif task == "T2":
                        obj = build_t2_json(spec, mp_doc, args.tier)
                    else:
                        print(f"  [skip] unsupported task: {task}")
                        continue
                    write_json(obj, out_path, dry_run=args.dry_run)
                    print(f"  [ok] {task} -> {out_path}")
                    total_written += 1
                except Exception as e:
                    print(f"  [fail] {task} 生成失败：{e}")
                    total_failed += 1

    print(f"\n=== summary: written={total_written} skipped={total_skipped} failed={total_failed} ===")


def task_suffix(task: str) -> str:
    return {"T1": "vc_relax", "T2": "bands_dos", "T3": "dfpt", "T4": "eos"}.get(task, task.lower())


if __name__ == "__main__":
    main()
