"""Named evaluation sets from the benchmark release manifest."""
from __future__ import annotations

import csv
from pathlib import Path
from typing import Optional


def load_subsets(bench_dir: Path) -> dict:
    groups = {name: {"name": name, "name_en": name, "task_ids": []}
              for name in ("stable", "exploratory", "held_out", "frozen")}
    with (bench_dir / "task_manifest.tsv").open() as manifest:
        for row in csv.DictReader(manifest, delimiter="\t"):
            groups[row["set"]]["task_ids"].append(row["task_id"])
            if row["frozen_100"] == "1":
                groups["frozen"]["task_ids"].append(row["task_id"])
    return {"subsets": groups}


def list_subset_keys(bench_dir: Path) -> list[str]:
    """返回所有 subset 键（'fast' / 'standard' / 'full'）按 yaml 出现顺序。"""
    data = load_subsets(bench_dir)
    return list((data.get("subsets") or {}).keys())


def get_subset_meta(bench_dir: Path, subset_key: str) -> Optional[dict]:
    """返回单个 subset 的 metadata（name / description / task_ids / expected_count）；找不到返 None。"""
    data = load_subsets(bench_dir)
    return (data.get("subsets") or {}).get(subset_key)


def get_subset_task_ids(
    bench_dir: Path,
    subset_key: str,
    all_stable_ids: Optional[list[str]] = None,
) -> Optional[set[str]]:
    """返回某子集的 task_id 集合。

    - subset_key 为空 / 'all' / 不认识的键 → 返 None（即不过滤）
    - subset 静态 task_ids 列出 → 返该集合
    - subset task_ids 为 None（如 'full'）→ 用 all_stable_ids 动态展开（caller 提供）

    返回 None 表示「不过滤」（caller 应当作不应用 subset 过滤）；返回空集 set() 表示
    「过滤后无任何题」（caller 应当作没题）。区别重要。
    """
    if not subset_key or subset_key == "all":
        return None
    meta = get_subset_meta(bench_dir, subset_key)
    if meta is None:
        return None
    task_ids = meta.get("task_ids")
    if task_ids is None:
        # full 子集走动态展开
        return set(all_stable_ids or [])
    return set(task_ids)


def reset_cache() -> None:
    """测试用：手动清空缓存让下一次 load 重读文件。"""
    global _CACHE, _CACHE_MTIME
    with _LOCK:
        _CACHE = {}
        _CACHE_MTIME = 0.0
