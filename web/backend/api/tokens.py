"""Token CRUD endpoints — dev-only token management UI 后端（F3 Step 3, 2026-05-08）。

设计要点
========

**Gate**：所有 endpoint 走 `Depends(require_dev_role)` —— 这是 root-级 escape hatch。
Token 管理 **不暴露成** capability：如果允许某 normal user 持有 `admin.manage_tokens` cap，
他能给自己授任何 cap + 翻 `is_dev_role=True` → 整个 capability 系统失效。所以这个边界
必须靠 `is_dev_role` boolean 守住，不能由 capability 系统授权。

**持久化**：mutation 后原子写 `tokens.yaml`（先写 `.tmp` 再 `os.replace()`），
然后用新 entries 重建 `app.state.token_registry`。新 lookup 立即生效，不需要重启
backend。

**token_id**：用 token 字符串 last 6 chars 作 stable 短 id（hex-ish；32-char URL-safe
token suffix 6 字符 = 64^6 ≈ 6.8 亿空间，常用 4-50 token 量级碰撞概率 ≈ 0）。明文
token 永远不暴露（list/get 返回 mask 字符串；create 仅 **一次** 返回明文供用户立即记下）。

**is_dev_role 不可通过 API 修改**：避免 dev 自降权后没人能恢复。要授 dev 必须改 yaml +
重启。`TokenCreateRequest` / `TokenUpdateRequest` 都没有 is_dev_role 字段。

**Fail-safe**：
- 自删保护：requester 的 owner == target owner → 400
- 最后一个 dev token 保护：删除后 dev 数 == 0 → 400（避免永久锁死管理面板）
"""
from __future__ import annotations

import logging
import os
import tempfile
import secrets
from pathlib import Path
from typing import Optional

import yaml
from fastapi import APIRouter, Body, Depends, HTTPException, Path as PathParam, Request
from pydantic import BaseModel, Field

from ..auth.capabilities import (
    ALL_CAPABILITIES,
    CAPABILITY_DESCRIPTIONS,
    CAPABILITY_GROUPS,
    DEFAULT_NORMAL_TOKEN_CAPS,
    validate_capabilities,
)
from ..auth.deps import require_dev_role
from ..auth.tokens import TokenRegistry, User


log = logging.getLogger(__name__)


router = APIRouter(prefix="/api/tokens", tags=["tokens"])


# ─── Schemas ────────────────────────────────────────────────────────────────


class TokenView(BaseModel):
    """list / get 返回结构；`token` 字段做 mask 保密。"""

    token_id: str       # last 6 chars of token；UI 用作 stable opaque id
    token_masked: str   # 形如 "abc...xyz"（前 3 + ... + 末 3）
    owner: str
    display_name: str
    is_dev_role: bool
    capabilities: list[str]


class TokenCreateRequest(BaseModel):
    """POST 请求体。is_dev_role **不在这里**——通过 UI 创建的 token 只能 normal。
    要授 dev role 必须人工编辑 yaml + 重启 backend。

    capabilities 字段语义：
      - 不传字段（None）：用 DEFAULT_NORMAL_TOKEN_CAPS = [view.others_runs]，等价
        历史 normal 体验（能看别人的 run）
      - 传 [] 显式空：尊重用户意图，是真空（看不到别人的 run）
      - 传 [...] 列表：用列表内容
    """

    owner: str = Field(min_length=1, max_length=64)
    display_name: str = Field(min_length=1, max_length=128)
    capabilities: Optional[list[str]] = None


class TokenCreateResponse(BaseModel):
    """POST 响应：明文 token 一次性返回，UI 必须立即让用户复制。"""

    token: str          # PLAINTEXT，仅创建时返回一次
    token_id: str
    owner: str
    display_name: str
    is_dev_role: bool   # 总是 False（API 不能造 dev token）
    capabilities: list[str]


class TokenUpdateRequest(BaseModel):
    """PATCH 请求体。is_dev_role **不在这里**：不允许通过 API 切换 dev role。"""

    capabilities: Optional[list[str]] = None
    display_name: Optional[str] = None


# ─── 内部工具 ───────────────────────────────────────────────────────────────


def _mask_token(token: str) -> str:
    """`abc...xyz` 风格的 mask；token 太短（<6）退化为全 mask。"""
    if len(token) < 6:
        return "***"
    return f"{token[:3]}...{token[-3:]}"


def _token_id(token: str) -> str:
    """token last 6 chars 作 stable 短 id；明文不暴露。"""
    return token[-6:] if len(token) >= 6 else token


def _registry_to_entries(reg: TokenRegistry) -> list[dict]:
    """Registry 内部 dict → yaml-friendly entries 列表（保留字段顺序）。"""
    out: list[dict] = []
    for token, user in reg._by_token.items():  # noqa: SLF001 (intentional)
        out.append({
            "token": token,
            "is_dev_role": user.is_dev_role,
            "capabilities": list(user.capabilities),
            "owner": user.owner,
            "display_name": user.display_name,
        })
    return out


def _atomic_write_yaml(path: Path, entries: list[dict]) -> None:
    """原子写 tokens.yaml：先写 .tmp 再 os.replace()。"""
    text = yaml.safe_dump(
        entries, default_flow_style=False, sort_keys=False, allow_unicode=True,
    )
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, prefix=".tokens-", delete=False) as tmp:
        tmp_path = Path(tmp.name)
        try:
            tmp.write(text)
            tmp.flush()
            os.replace(tmp_path, path)
        finally:
            tmp_path.unlink(missing_ok=True)


def _persist_and_rebuild(request: Request, entries: list[dict]) -> TokenRegistry:
    """新 entries → 原子写 yaml → 重建 registry → 替换 app.state.token_registry。

    顺序：先 build registry 验证 schema（捕错先于落盘），再写文件，再 swap state。
    任一步异常向上抛；caller 转 500。
    """
    new_reg = TokenRegistry(entries)  # 严格校验；schema 错先 ValueError
    yaml_path: Path = request.app.state.settings.tokens_yaml
    yaml_path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write_yaml(yaml_path, entries)
    request.app.state.token_registry = new_reg
    return new_reg


def _find_entry_by_id(entries: list[dict], token_id: str) -> Optional[int]:
    """按 token_id（last 6 chars）找 entry index；未找到返 None。

    碰撞处理：同 token_id 命中多条 → 视作配置错（极小概率），返 -1 表 ambiguous。
    Caller 负责把 -1 转 409。
    """
    matches = [
        i for i, e in enumerate(entries)
        if _token_id(e.get("token", "")) == token_id
    ]
    if not matches:
        return None
    if len(matches) > 1:
        return -1
    return matches[0]


def _entry_to_view(entry: dict) -> TokenView:
    token = entry["token"]
    return TokenView(
        token_id=_token_id(token),
        token_masked=_mask_token(token),
        owner=entry["owner"],
        display_name=entry.get("display_name", entry["owner"]),
        is_dev_role=bool(entry.get("is_dev_role", False)),
        capabilities=list(entry.get("capabilities", [])),
    )


# ─── Endpoints ──────────────────────────────────────────────────────────────


@router.get("/registry")
async def get_capability_registry(
    request: Request,
    _user: User = Depends(require_dev_role),
) -> dict:
    """capability 全集 + 分组 + 中文描述；UI 渲染 token CRUD checkbox grid 用。

    也走 dev_role gate：避免未授权 enumeration capability 注册表（虽然内容
    不敏感，但减少 normal user 可知信息面）。
    """
    return {
        "all_capabilities": list(ALL_CAPABILITIES),
        "groups": {k: list(v) for k, v in CAPABILITY_GROUPS.items()},
        "descriptions": dict(CAPABILITY_DESCRIPTIONS),
    }


@router.get("")
async def list_tokens(
    request: Request,
    _user: User = Depends(require_dev_role),
) -> list[TokenView]:
    """list 所有 token（mask 后）。仅 dev_role 可见。"""
    reg: TokenRegistry = request.app.state.token_registry
    entries = _registry_to_entries(reg)
    return [_entry_to_view(e) for e in entries]


@router.post("")
async def create_token(
    request: Request,
    body: TokenCreateRequest,
    _user: User = Depends(require_dev_role),
) -> TokenCreateResponse:
    """新建 token。caps 不传 = DEFAULT_NORMAL_TOKEN_CAPS（[view.others_runs]，
    等价历史 normal 行为）；传 [] = 真空（看不到别人 run）；传 [...] = 用列表。

    返回明文 token **一次**——UI 必须立即提示用户记下，关闭后无法找回（list 只
    返回 mask）。`is_dev_role` 永远 False，不能通过 API 造 dev token。
    """
    # 0) caps 默认填充：None → DEFAULT_NORMAL_TOKEN_CAPS；传 [] 尊重显式空
    caps = list(DEFAULT_NORMAL_TOKEN_CAPS) if body.capabilities is None else list(body.capabilities)
    # 1) 校验 caps
    unknown = validate_capabilities(caps)
    if unknown:
        raise HTTPException(
            status_code=400,
            detail=f"unknown capabilities: {unknown}",
        )

    # 2) 校验 owner unique（registry 限制：owner 全集唯一）
    reg: TokenRegistry = request.app.state.token_registry
    entries = _registry_to_entries(reg)
    existing_owners = {e["owner"] for e in entries}
    if body.owner in existing_owners:
        raise HTTPException(
            status_code=400,
            detail=f"owner {body.owner!r} already used; owners must be unique",
        )

    # 3) 生成新 token；极小概率碰撞已存在 token → 重 roll
    new_token = secrets.token_urlsafe(32)
    existing_tokens = {e["token"] for e in entries}
    while new_token in existing_tokens:
        new_token = secrets.token_urlsafe(32)

    # 4) 追加 + 持久化 + 重建
    new_entry = {
        "token": new_token,
        "is_dev_role": False,
        "capabilities": caps,
        "owner": body.owner,
        "display_name": body.display_name,
    }
    entries.append(new_entry)
    try:
        _persist_and_rebuild(request, entries)
    except (ValueError, OSError) as exc:
        # 写盘 / 校验失败：回滚 in-memory state（仍是旧 registry）
        log.exception("create_token: persist failed")
        raise HTTPException(status_code=500, detail=f"persist failed: {exc}")

    return TokenCreateResponse(
        token=new_token,
        token_id=_token_id(new_token),
        owner=body.owner,
        display_name=body.display_name,
        is_dev_role=False,
        capabilities=caps,
    )


@router.patch("/{token_id}")
async def update_token(
    request: Request,
    body: TokenUpdateRequest = Body(...),
    token_id: str = PathParam(..., min_length=1, max_length=64),
    _user: User = Depends(require_dev_role),
) -> TokenView:
    """更新 token 的 capabilities / display_name。is_dev_role **不可** 改：
    schema 中无该字段，避免 dev 自降权后没人能恢复。要切 dev 必须改 yaml 重启。
    """
    reg: TokenRegistry = request.app.state.token_registry
    entries = _registry_to_entries(reg)
    idx = _find_entry_by_id(entries, token_id)
    if idx is None:
        raise HTTPException(status_code=404, detail=f"token_id not found: {token_id}")
    if idx == -1:
        raise HTTPException(
            status_code=409,
            detail=f"token_id ambiguous (collision): {token_id}",
        )

    # 校验 caps（如果提供）
    if body.capabilities is not None:
        unknown = validate_capabilities(body.capabilities)
        if unknown:
            raise HTTPException(
                status_code=400,
                detail=f"unknown capabilities: {unknown}",
            )
        entries[idx]["capabilities"] = list(body.capabilities)

    if body.display_name is not None:
        if not body.display_name.strip():
            raise HTTPException(
                status_code=400, detail="display_name must be non-empty",
            )
        if len(body.display_name) > 128:
            raise HTTPException(
                status_code=400, detail="display_name too long (>128 chars)",
            )
        entries[idx]["display_name"] = body.display_name

    try:
        _persist_and_rebuild(request, entries)
    except (ValueError, OSError) as exc:
        log.exception("update_token: persist failed")
        raise HTTPException(status_code=500, detail=f"persist failed: {exc}")

    return _entry_to_view(entries[idx])


@router.delete("/{token_id}")
async def delete_token(
    request: Request,
    token_id: str = PathParam(..., min_length=1, max_length=64),
    user: User = Depends(require_dev_role),
) -> dict:
    """删除 token。

    Fail-safes：
    1. 自删保护：target.owner == requester.owner → 400（防误锁）
    2. 最后一个 dev token 保护：删除后 dev 数 == 0 → 400（防永久锁死管理）
    """
    reg: TokenRegistry = request.app.state.token_registry
    entries = _registry_to_entries(reg)
    idx = _find_entry_by_id(entries, token_id)
    if idx is None:
        raise HTTPException(status_code=404, detail=f"token_id not found: {token_id}")
    if idx == -1:
        raise HTTPException(
            status_code=409,
            detail=f"token_id ambiguous (collision): {token_id}",
        )

    target = entries[idx]
    # 1) 自删保护：用 owner 比较；trusted-localhost owner='localhost' 一般不在 yaml，
    #    所以本机直连不会误判为自删
    if target["owner"] == user.owner:
        raise HTTPException(
            status_code=400,
            detail="cannot delete self (would lock yourself out)",
        )

    # 2) 最后 dev 保护：删除后 dev 数 == 0 → 拒绝
    remaining = [e for i, e in enumerate(entries) if i != idx]
    remaining_dev_count = sum(
        1 for e in remaining if bool(e.get("is_dev_role", False))
    )
    if remaining_dev_count == 0:
        raise HTTPException(
            status_code=400,
            detail="cannot delete last dev_role token (would lock all admin)",
        )

    # 3) 持久化 + 重建
    try:
        _persist_and_rebuild(request, remaining)
    except (ValueError, OSError) as exc:
        log.exception("delete_token: persist failed")
        raise HTTPException(status_code=500, detail=f"persist failed: {exc}")

    return {"ok": True, "deleted_token_id": token_id}
