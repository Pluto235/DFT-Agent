"""Token registry：从 tokens.yaml 加载 + 常时间 lookup。

format (F3 新 schema, 2026-05-08)：
  - token: <secret>
    is_dev_role: true | false   # root-级 escape hatch；bypass 所有 cap + token CRUD 唯一 gate
    capabilities: []            # 细粒度权限列表（空 = 当前 normal 等价行为）
    owner: <unique key 用作 runs.owner 列>
    display_name: <UI 显示>

迁移：旧 `role: dev | normal` schema 由 `the upstream token migration tool`
一次性升级；本模块 strict-load，看到 `role:` 字段直接 raise（不 silent upgrade）。
"""
from __future__ import annotations

import hmac
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml

from .capabilities import validate_capabilities


log = logging.getLogger(__name__)


@dataclass(frozen=True)
class User:
    """认证用户。

    `is_dev_role` 是 root-级 boolean：bypass 所有 capability check + 是 token CRUD UI
    唯一 gate（不能通过 capability 系统授予，避免提权绕过）。
    `capabilities` 是细粒度权限列表，普通用户用；空列表 = 当前 normal 等价行为。
    """
    owner: str
    display_name: str
    is_dev_role: bool = False
    capabilities: list[str] = field(default_factory=list)

    def has_capability(self, cap: str) -> bool:
        """is_dev_role bypass 所有 cap；否则查列表 membership。"""
        if self.is_dev_role:
            return True
        return cap in self.capabilities


class TokenRegistry:
    """tokens.yaml 加载 + lookup。

    lookup 用 hmac.compare_digest 防 timing attack；为保持 timing 常量，遍历所有
    entry 而不 break early——4 个 token 量级遍历微秒级，无性能顾虑。
    """

    def __init__(self, entries: list[dict]) -> None:
        self._by_token: dict[str, User] = {}
        seen_owners: set[str] = set()
        for idx, e in enumerate(entries):
            token = e.get("token", "")
            owner = e.get("owner", "")
            display_name = e.get("display_name", owner)

            # 老 schema 保护：看到 role: 字段直接 fail，引导用户跑迁移脚本
            if "role" in e:
                raise ValueError(
                    f"tokens entry #{idx} 含老 schema 字段 `role`；"
                    "请跑 `the upstream token migration tool` 升级到 "
                    "`is_dev_role + capabilities` 新 schema 后再启动。"
                )

            is_dev_role_raw = e.get("is_dev_role", False)
            if not isinstance(is_dev_role_raw, bool):
                raise ValueError(
                    f"tokens entry #{idx} is_dev_role 必须是 boolean，得到 "
                    f"{type(is_dev_role_raw).__name__}={is_dev_role_raw!r}"
                )

            caps_raw = e.get("capabilities", [])
            if not isinstance(caps_raw, list):
                raise ValueError(
                    f"tokens entry #{idx} capabilities 必须是 list，得到 "
                    f"{type(caps_raw).__name__}"
                )
            unknown_caps = validate_capabilities(caps_raw)
            if unknown_caps:
                raise ValueError(
                    f"tokens entry #{idx} capabilities 含未知项: {unknown_caps}；"
                    "合法 cap 见 backend/auth/capabilities.py"
                )

            if not token or not owner:
                raise ValueError(
                    f"tokens entry #{idx} 缺必填字段（need: token, owner）"
                )
            if token in self._by_token:
                raise ValueError(f"tokens entry #{idx} token already used")
            if owner in seen_owners:
                raise ValueError(
                    f"tokens entry #{idx} owner={owner!r} already used; "
                    "owners must be unique"
                )
            seen_owners.add(owner)
            self._by_token[token] = User(
                owner=owner,
                display_name=display_name,
                is_dev_role=is_dev_role_raw,
                capabilities=list(caps_raw),
            )

    @classmethod
    def from_yaml(cls, path: Path) -> "TokenRegistry":
        text = path.read_text()
        data = yaml.safe_load(text)
        if not isinstance(data, list):
            raise ValueError(
                f"{path} must be a YAML list of token entries; got {type(data).__name__}"
            )
        return cls(data)

    @classmethod
    def from_single_token(
        cls, token: str, *, owner: str = "default",
        is_dev_role: bool = False, display_name: str = "default",
    ) -> "TokenRegistry":
        """fallback：单 AUTH_TOKEN 模式（.env 老部署不破）。

        默认 is_dev_role=False + caps=[]，等价当前 normal 行为。
        """
        return cls([{
            "token": token, "is_dev_role": is_dev_role,
            "capabilities": [],
            "owner": owner, "display_name": display_name,
        }])

    @classmethod
    def empty(cls) -> "TokenRegistry":
        """无认证模式（dev 本地起，没设 AUTH_TOKEN 也没 tokens.yaml）。"""
        return cls([])

    def lookup(self, raw: str) -> Optional[User]:
        """常时间 lookup。raw 空 → None；不匹配 → None。"""
        if not raw or not isinstance(raw, str):
            return None
        matched: Optional[User] = None
        for stored, user in self._by_token.items():
            # 不 break：遍历完所有 entry，让 timing 不暴露 token 存在性
            if hmac.compare_digest(raw, stored):
                matched = user
        return matched

    @property
    def has_tokens(self) -> bool:
        return bool(self._by_token)

    def __len__(self) -> int:
        return len(self._by_token)


def load_token_registry(
    tokens_yaml: Optional[Path], env_auth_token: Optional[str],
) -> TokenRegistry:
    """启动时调一次。优先级：
      1. tokens.yaml 存在 → 加载 yaml
      2. tokens.yaml 不存在 + AUTH_TOKEN env 设了 → 单 token fallback
      3. 都没有 → empty registry（auth 关闭，所有请求当 default dev）
    """
    if tokens_yaml is not None and tokens_yaml.exists():
        try:
            reg = TokenRegistry.from_yaml(tokens_yaml)
            log.info("token registry loaded from %s (%d entries)", tokens_yaml, len(reg))
            return reg
        except (yaml.YAMLError, ValueError):
            log.exception("failed to load %s; refusing to start", tokens_yaml)
            raise
    if env_auth_token:
        log.info("token registry: single-token fallback from AUTH_TOKEN env")
        return TokenRegistry.from_single_token(env_auth_token)
    log.warning(
        "token registry: empty (no tokens.yaml, no AUTH_TOKEN). "
        "auth disabled — DO NOT expose to untrusted networks."
    )
    return TokenRegistry.empty()
