"""扫 bench questions/M*_T*/*.json → 缓存 fat TaskListItem 列表。

依赖真 harness.lib.task_loader 的 render_prompt + derive_initial_files（不重写）。
last_score / last_run_at / last_run_count 从 SQLite runs 表派生（dao.runs_aggregate_for_task）。

策略：mount 时一次扫完缓存 ~1MB；每 60s 失效一次（template / 题目改动可被察觉）。
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
import warnings
from pathlib import Path
from typing import Any, Optional

from backend.persistence import dao
from backend.persistence.db import Database
from backend.schemas.task import TaskListItem


# Bonus（audit）：pymatgen.symmetry.groups 用 from_xyz_string 已弃，每次调发警告；
# 我们在 _short_material 每 task 调一次 SpaceGroup.from_int_number → 全量 list 共 ~700+ 警告
# × pytest 跨 22 测试 ≈ 119k 噪声。提前静默仅 module=pymatgen.* 的 DeprecationWarning。
warnings.filterwarnings(
    "ignore", category=DeprecationWarning, module=r"pymatgen\..*",
)


_CACHE_RAW: list[dict] | None = None     # raw task dicts (无 derived)
_CACHE_KEY: tuple[str, str] | None = None  # (bench_dir, bench_questions_dir) — 跨 settings 失效
_CACHE_TS: float = 0.0
_CACHE_TTL_SEC: float = 60.0
_LOAD_LOCK: asyncio.Lock | None = None   # lazy-init in async context


# 真实 task JSON 的 evaluator.extract_fields 是 str list（按 field name），
# 但 prototype data.js EXTRACT_FIELDS_T1/T2 期望 [{name, type, desc, weight}] dict list；
# task_loader 把 str list lift 成 dict list（宪法 2 — backend 适配 frontend，与 prototype 一致）。
#
# Mock 表只保 type + desc；weight 改自 task.scoring.field_weights 实时读取
# （callsite 传入），避免与实际 evaluator 打分通道分叉（详见 0505 fix
# todo/web/task-preview-display-fix.md Bug B）。Scoring 不打分的字段（T1 的
# point_group / crystal_system）weight=0。
_EXTRACT_FIELDS_T1: dict[str, dict] = {
    "a":                  {"type": "float", "desc": "lattice parameter a (Å)"},
    "b":                  {"type": "float", "desc": "lattice parameter b (Å)"},
    "c":                  {"type": "float", "desc": "lattice parameter c (Å)"},
    "alpha":              {"type": "float", "desc": "lattice angle α (deg)"},
    "beta":               {"type": "float", "desc": "lattice angle β (deg)"},
    "gamma":              {"type": "float", "desc": "lattice angle γ (deg)"},
    "space_group_number": {"type": "int",   "desc": "space group number (1–230)"},
    "point_group":        {"type": "str",   "desc": "crystallographic point group"},
    "crystal_system":     {"type": "str",   "desc": "cubic / hexagonal / …"},
}

_EXTRACT_FIELDS_T2: dict[str, dict] = {
    "band_gap_ev":  {"type": "float", "desc": "band gap (eV); 0 for metals"},
    "gap_type":     {"type": "str",   "desc": "direct / indirect / metal"},
    "vbm_k_label":  {"type": "str",   "desc": "VBM k-label (e.g. GAMMA / X / OFF_PATH)"},
    "cbm_k_label":  {"type": "str",   "desc": "CBM k-label (same enum)"},
    "dos_e_fermi":  {"type": "float", "desc": "Fermi energy from NSCF tetra (eV)"},
    "dos_curve":    {"type": "dict",  "desc": "DOS curve, ~700 points"},
}


def _enrich_extract_fields(
    fields: list,
    task_type: Optional[str],
    scoring_weights: dict,
) -> list[dict]:
    """str list → [{name, type?, desc?, weight}] 与 prototype shape 对齐。

    weight 来自 task.scoring.field_weights（实际打分权重源）；scoring 不打分的
    字段 weight=0。task JSON 里已经是 dict 的项直通（让题目自带 weight 时不被
    覆盖，但当前 172 道全是 str list）；str 的项查 mock 表填 type/desc，weight
    实时读 scoring_weights。
    """
    defs = _EXTRACT_FIELDS_T1 if task_type == "T1" else (
        _EXTRACT_FIELDS_T2 if task_type == "T2" else {}
    )
    out: list[dict] = []
    for f in fields:
        if isinstance(f, dict):
            out.append(f)
            continue
        if not isinstance(f, str):
            continue
        d = defs.get(f, {"type": "any", "desc": ""})
        out.append({
            "name": f,
            "type": d.get("type", "any"),
            "desc": d.get("desc", ""),
            "weight": float(scoring_weights.get(f, 0.0)),
        })
    return out


def _ensure_harness_on_path(bench_dir: Path) -> None:
    bd = str(bench_dir)
    if bd not in sys.path:
        sys.path.insert(0, bd)


# ITC 标准空间群编号 → crystal system 区间表（1-230 完整覆盖；不会随时间变化）。
# 替代原 pymatgen.SpaceGroup.from_int_number(sg).crystal_system 调用 —— 单次 ~445ms
# × 172 题约 27s 的性能毒药；查表 < 1µs。
# 参考：International Tables for Crystallography Vol. A 第 7 章。
_CRYSTAL_SYSTEM_BY_SG: tuple[tuple[int, int, str], ...] = (
    (1,   2,   "triclinic"),
    (3,   15,  "monoclinic"),
    (16,  74,  "orthorhombic"),
    (75,  142, "tetragonal"),
    (143, 167, "trigonal"),
    (168, 194, "hexagonal"),
    (195, 230, "cubic"),
)


def _crystal_system_from_sg(sg_number: Any) -> Optional[str]:
    if sg_number is None:
        return None
    try:
        n = int(sg_number)
    except (TypeError, ValueError):
        return None
    for lo, hi, name in _CRYSTAL_SYSTEM_BY_SG:
        if lo <= n <= hi:
            return name
    return None  # 非法 sg（1-230 之外）— 与原 pymatgen except 路径同样返 None


def _short_material(material: dict[str, Any], milestone: Optional[str]) -> dict[str, Any]:
    """与 prototype `data.js` MATERIALS 元素 + `catalogue.jsx` 行 170-175 期望对齐：

        {f, n, sys, sg, fam, m, a}

    其中 `sg` 是 int space-group number（行 175 `#{task.material.sg}`），`a` 是 lattice
    constant float（行 173 `task.material.a.toFixed(3)` 必须 number），`m` 是 milestone
    M1/M2（不是 mass — prototype data.js 行 462 `milestone: material.m`）。
    """
    sg_num = material.get("space_group_number")
    init = material.get("initial_structure", {}) or {}
    lc = init.get("lattice_constants_angstrom")
    # lc 兼容 dict {a,b,c}（M1/M2）与 list [a,b,c]（M3 worker-0 6ed34b2）
    if isinstance(lc, list):
        a_val = lc[0] if lc else None
    elif isinstance(lc, dict):
        a_val = lc.get("a")
    else:
        a_val = None
    return {
        "f": material.get("formula"),
        "n": material.get("name"),
        "sys": _crystal_system_from_sg(sg_num),
        "sg": sg_num,
        "fam": material.get("structure"),
        "m": milestone,
        "a": a_val,
    }


def _scan_questions(bench_questions_dir: Path) -> list[Path]:
    return sorted(bench_questions_dir.glob("M*_T*/*.json"))


def _load_raw_tasks(bench_dir: Path, bench_questions_dir: Path) -> list[dict]:
    _ensure_harness_on_path(bench_dir)
    from harness.lib.task_loader import (
        derive_initial_files,
        render_prompt,
    )
    out: list[dict] = []
    for p in _scan_questions(bench_questions_dir):
        task = json.loads(p.read_text())
        rendered = render_prompt(bench_dir, task)
        initial_files = derive_initial_files(bench_dir, task)
        # difficulty 两种 shape：
        #   dict {material_tier, task_tier} —— M1/M2 原始
        #   str "M3" —— worker-0 6ed34b2 M3 promote 用的简化形
        # 字符串场景下从 task_id "M{N}_T{M}_*" 推 task_tier
        difficulty_raw = task.get("difficulty")
        if isinstance(difficulty_raw, dict):
            difficulty = difficulty_raw
        else:
            tid = str(task.get("id", ""))
            mat_tier = difficulty_raw if isinstance(difficulty_raw, str) else None
            task_tier = None
            if "_" in tid:
                parts = tid.split("_")
                if len(parts) >= 2 and parts[0].startswith("M") and parts[1].startswith("T"):
                    if mat_tier is None:
                        mat_tier = parts[0]
                    task_tier = parts[1]
            difficulty = {"material_tier": mat_tier, "task_tier": task_tier}
        material = task.get("material", {}) or {}
        task_obj = task.get("task", {}) or {}
        evaluator = task.get("evaluator", {}) or {}
        policy = task_obj.get("policy", {}) or {}
        meta = task.get("metadata", {}) or {}
        gt_quality = meta.get("gt_quality") or "self_run_converged"
        # is_stable 缺省 True（兼容存量 172 题，全部按 stable 处理）；
        # dev-only 题目要 explicit metadata.is_stable=false 才会从 normal role 隐藏。
        is_stable = bool(meta.get("is_stable", True))
        out.append({
            "task_id": task["id"],
            "milestone": difficulty.get("material_tier"),
            "task_type": difficulty.get("task_tier"),
            "task_subtype": task_obj.get("type", ""),
            "material_formula": material.get("formula", ""),
            "material_name": material.get("name"),
            "schema_version": str(task.get("schema_version", "")),
            "network_allowed": bool(policy.get("network_allowed", False)),
            "gt_quality": gt_quality,
            "is_stable": is_stable,
            "material": _short_material(material, difficulty.get("material_tier")),
            "rendered_prompt": rendered,
            "initial_files": initial_files,
            "extract_fields": _enrich_extract_fields(
                evaluator.get("extract_fields", []) or [],
                difficulty.get("task_tier"),
                task.get("scoring", {}).get("field_weights", {}) or {},
            ),
            "convergence_evidence": task.get("convergence_evidence"),
        })
    return out


async def load_all_tasks(
    settings: Any,
    db: Optional[Database] = None,
    *,
    force_reload: bool = False,
) -> list[TaskListItem]:
    """一次扫完 + cache（默认 60s TTL）；可选 db 提供 derived last_*。

    audit P1-5：cache key = (bench_dir, bench_questions_dir)。settings 改路径时
    自动失效，避免 stale；测试间也不会跨 settings 拿错数据。
    audit P2-4：last_* 派生改一次性 group-by 查询，不再 172x 串行 fetchone。

    并发保护：_load_raw_tasks 是 CPU-bound sync（172 题 render_prompt + pymatgen
    SpaceGroup ~28s），并发调用会让 GIL bouncing 慢上加慢。加 asyncio.Lock 让第二
    个 caller 等第一个完成；并把 sync 部分放 thread pool，不阻塞 event loop。
    """
    global _CACHE_RAW, _CACHE_KEY, _CACHE_TS, _LOAD_LOCK
    if _LOAD_LOCK is None:
        _LOAD_LOCK = asyncio.Lock()
    key = (str(settings.bench_dir), str(settings.bench_questions_dir))
    needs_reload = (
        force_reload
        or _CACHE_RAW is None
        or _CACHE_KEY != key
        or (time.time() - _CACHE_TS) > _CACHE_TTL_SEC
    )
    if needs_reload:
        async with _LOAD_LOCK:
            # 拿到锁后再 check 一次（double-checked locking）：可能上一个 holder 已 prime
            if (
                force_reload
                or _CACHE_RAW is None
                or _CACHE_KEY != key
                or (time.time() - _CACHE_TS) > _CACHE_TTL_SEC
            ):
                raw = await asyncio.to_thread(
                    _load_raw_tasks, settings.bench_dir, settings.bench_questions_dir,
                )
                _CACHE_RAW = raw
                _CACHE_KEY = key
                _CACHE_TS = time.time()

    if db is not None:
        agg_map = await dao.runs_aggregate_all_tasks(db)
    else:
        agg_map = {}

    items: list[TaskListItem] = []
    for raw in _CACHE_RAW:
        agg = agg_map.get(raw["task_id"], {
            "last_run_count": 0, "last_run_at": None, "last_score": None,
        })
        items.append(TaskListItem(
            **raw,
            last_score=agg.get("last_score"),
            last_run_at=agg.get("last_run_at"),
            last_run_count=int(agg.get("last_run_count") or 0),
        ))
    return items


async def get_task(settings: Any, db: Optional[Database], task_id: str) -> Optional[TaskListItem]:
    items = await load_all_tasks(settings, db)
    for t in items:
        if t.task_id == task_id:
            return t
    return None


def invalidate_cache() -> None:
    """test / live-edit 触发 reload。"""
    global _CACHE_RAW, _CACHE_KEY, _CACHE_TS
    _CACHE_RAW = None
    _CACHE_KEY = None
    _CACHE_TS = 0.0
