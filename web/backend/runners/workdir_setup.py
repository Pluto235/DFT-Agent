"""custom mode workdir helpers：渲染 task_prompt.txt + 生成 spec + run_id。

不复用 harness.lib.task_loader.render_prompt（custom 没有 bench task struct，
构造伪 task 入参反而复杂）；直接 format string 拼接。
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _attr(obj: Any, name: str) -> Any:
    if obj is None:
        return None
    if isinstance(obj, dict):
        return obj.get(name)
    return getattr(obj, name, None)


def render_task_prompt_for_custom(
    workdir: Path, *, prompt: str, material: Any, task_type: str,
) -> Path:
    """把 user prompt + MaterialSpec 拼成 task_prompt.txt，写到 workdir/。
    `material` 接受 dict 或 schemas.material.MaterialSpec 实例。
    """
    workdir.mkdir(parents=True, exist_ok=True)
    src = _attr(material, "source")
    if src == "upload":
        ps = _attr(material, "parsed_summary") or {}
        block = (
            f"- formula: {ps.get('formula')}\n"
            f"- crystal system: {ps.get('lattice_system')}\n"
            f"- space group: {ps.get('space_group')}\n"
            f"- atoms / primitive cell: {ps.get('atoms_count')}"
        )
    elif src == "describe":
        block = f"- description: {_attr(material, 'formula_text')}"
    elif src == "pick":
        block = f"- inherited from bench task: {_attr(material, 'bench_task_id')}"
    else:
        block = "- (none)"
    text = (
        f"# Custom task: {task_type}\n\n"
        f"## User description\n{prompt}\n\n"
        f"## Material\n{block}\n\n"
        f"## Submission\nCall the `submit` tool with the answer dict when done.\n\n"
        f"## Network policy\nnetwork_allowed = false. The agent must not access the network.\n"
    )
    path = workdir / "task_prompt.txt"
    path.write_text(text)
    return path


def build_custom_spec(
    *,
    prompt: str,
    material: Any,
    task_type: str,
    model: str,
    max_turns: int,
    max_output_tokens: int,
) -> dict:
    """custom_task_dir/<run_id>.json 的内容；result_reader 回查时读它。"""
    if hasattr(material, "model_dump"):
        mat_dict = material.model_dump()
    elif isinstance(material, dict):
        mat_dict = material
    else:
        mat_dict = {}
    return {
        "prompt": prompt,
        "material": mat_dict,
        "task_type": task_type,
        "model": model,
        "max_turns": max_turns,
        "max_output_tokens": max_output_tokens,
    }


def write_custom_spec(custom_task_dir: Path, run_id: str, spec: dict) -> Path:
    custom_task_dir.mkdir(parents=True, exist_ok=True)
    path = custom_task_dir / f"{run_id}.json"
    path.write_text(json.dumps(spec, indent=2))
    return path


def generate_custom_run_id(
    *, prompt: str, material: Any = None, task_type: str = "",
) -> str:
    """custom run_id：<ISO_TS>_custom_<hash6>（docs/03 §8.2：sha256(prompt+material+task_type)）。

    audit P0-1：原仅哈希 prompt，同秒内重复 prompt 会让 SQLite UNIQUE 冲突 → 500。
    现在把 material + task_type 也纳入哈希源，保证唯一性贴合实际差异点。
    """
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")
    if hasattr(material, "model_dump"):
        mat_blob = json.dumps(material.model_dump(), sort_keys=True)
    elif isinstance(material, dict):
        mat_blob = json.dumps(material, sort_keys=True)
    else:
        mat_blob = ""
    h = hashlib.sha256((prompt + mat_blob + (task_type or "")).encode()).hexdigest()[:6]
    return f"{ts}_custom_{h}"
