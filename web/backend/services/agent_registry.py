"""Agent registry persistence —— 读写 `agent_registry.yaml` 的 thread-safe 接口。

设计：
- 文件位置：`<REPO_ROOT>/agent_registry.yaml`（缺省路径，可被 settings 覆盖）
- 锁：模块内 `threading.Lock` 串行所有写操作；读用最新落盘版本（不缓存——admin
  endpoint 读完即用，正确性优先）
- 原子写：tempfile + os.replace 防进程在 yaml 半截被中断
- 不存在的文件 = 空 registry，不报错（首次启动 / 重置场景）

并发模型：FastAPI 单进程多线程，模块级 Lock 足够。多进程部署需要换 file-lock
（如 `fcntl.flock`），未来再说。
"""
from __future__ import annotations

import os
import tempfile
import threading
from pathlib import Path
from typing import Optional

import yaml

from ..schemas.agent_registry import AgentRegistry, AgentRegistryEntry


_LOCK = threading.Lock()


def load_registry(path: Path) -> AgentRegistry:
    """从 yaml 加载 registry；文件不存在返空 registry。"""
    p = Path(path)
    if not p.exists():
        return AgentRegistry()
    text = p.read_text(encoding="utf-8")
    if not text.strip():
        return AgentRegistry()
    data = yaml.safe_load(text) or {}
    return AgentRegistry.model_validate(data)


def save_registry(registry: AgentRegistry, path: Path) -> None:
    """原子写 registry 到 yaml。整 file 重写，不增量。"""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    # mode='json' 让 datetime → ISO string，yaml 可序列化
    data = registry.model_dump(mode="json")
    yaml_text = yaml.safe_dump(
        data, sort_keys=False, allow_unicode=True, default_flow_style=False
    )
    # tempfile + os.replace 保原子性
    fd, tmp = tempfile.mkstemp(prefix=p.name + ".", dir=str(p.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(yaml_text)
        os.replace(tmp, p)
    except Exception:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def append_entry(entry: AgentRegistryEntry, path: Path) -> AgentRegistry:
    """加一条 entry。若同 id 已有 active，先切为 inactive（语义：新版上架自动旧版下架）。

    返写后整 registry，调用方可立即用。
    """
    with _LOCK:
        reg = load_registry(path)
        # 同 id 现有 active 切 inactive
        for e in reg.entries:
            if e.manifest.id == entry.manifest.id and e.is_active:
                e.is_active = False
        # 新 entry 默认 active
        entry.is_active = True
        reg.entries.append(entry)
        save_registry(reg, path)
        return reg


def deactivate_entry(agent_id: str, commit_sha: str, path: Path) -> AgentRegistry:
    """把指定 (id, sha) entry 切 inactive。找不到抛 KeyError。"""
    with _LOCK:
        reg = load_registry(path)
        target: Optional[AgentRegistryEntry] = None
        for e in reg.entries:
            if e.manifest.id == agent_id and e.commit_sha == commit_sha:
                target = e
                break
        if target is None:
            raise KeyError(f"no entry found for ({agent_id}, {commit_sha})")
        target.is_active = False
        save_registry(reg, path)
        return reg


def reactivate_entry(agent_id: str, commit_sha: str, path: Path) -> AgentRegistry:
    """把指定 (id, sha) 切 active；同 id 现有 active 自动 deactive。

    用例：注册了 v2 但发现 regression，回滚到 v1 commit。
    """
    with _LOCK:
        reg = load_registry(path)
        target: Optional[AgentRegistryEntry] = None
        for e in reg.entries:
            if e.manifest.id == agent_id and e.commit_sha == commit_sha:
                target = e
                break
        if target is None:
            raise KeyError(f"no entry found for ({agent_id}, {commit_sha})")
        # 切其他 active 为 inactive
        for e in reg.entries:
            if e.manifest.id == agent_id and e is not target:
                e.is_active = False
        target.is_active = True
        save_registry(reg, path)
        return reg


def to_agent_entry_dicts(reg: AgentRegistry) -> list[dict]:
    """把 registry 里 active entries 转成 settings.AgentEntry 兼容 dict 列表。

    调用者：startup 时 settings.py 合并 DEFAULT_AGENTS + registry.list_active()
    → 形成完整 Settings.agents（id 重复时 registry 优先）。
    """
    out: list[dict] = []
    for e in reg.list_active():
        m = e.manifest
        # solver_path = clone_path / entrypoint，绝对路径
        solver_path = str(Path(e.clone_path) / m.entrypoint)
        out.append({
            "id": m.id,
            "name": m.name,
            "solver_path": solver_path,
            "description": m.description or f"External agent (commit {e.short_sha})",
            "available": True,
            "is_stable": False,  # 外部 agent 默认 dev-only 上架，需手动 promote
        })
    return out
