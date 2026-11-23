"""FastAPI dependencies for capability-based gating (F3 Step 2, 2026-05-08)。

提供两类守门：
- `require_capability(cap)` —— factory；返回一个 dependency，用 `Depends()` 挂到
  endpoint 上。User 缺该 cap → 403。`is_dev_role=True` 自动 bypass（has_capability 内部处理）。
- `require_dev_role` —— 硬编码守门；只放 `is_dev_role=True` 的 user 通过。
  **不是** capability —— 用于 token CRUD UI 等"权限管理本身"的入口（如果让 capability
  系统能够 grant token 管理权，则 normal user 可以提权 → 绕过 dev 边界）。

用法示例（Step 2 在 admin / scheduler 端落地）：
```python
from backend.auth.capabilities import ADMIN_VIEW_CLUSTER
from backend.auth.deps import require_capability

@router.get("/cluster_state", dependencies=[Depends(require_capability(ADMIN_VIEW_CLUSTER))])
async def cluster_state(...): ...
```

Step 3 接 token CRUD endpoint 时会用 `require_dev_role`：
```python
@router.post("/api/tokens", dependencies=[Depends(require_dev_role)])
async def create_token(...): ...
```

实现说明：
- 走 `request.state.user`（auth_middleware 已注入）。状态缺失时（无 middleware
  / 路径在白名单）抛 500 而非默认 None —— 让 endpoint 配置错误立即可见。
- HTTPException 用 detail 字段（FastAPI 默认包），与 router 端其他 403 一致。
"""
from __future__ import annotations

from fastapi import HTTPException, Request

from .tokens import User


def require_capability(cap: str):
    """Dependency factory；按 capability 守门 endpoint。

    Args:
        cap: backend.auth.capabilities 中的常量字符串（如 ADMIN_VIEW_CLUSTER）。

    Returns:
        FastAPI dependency callable，用于 `Depends(require_capability(...))`。
        通过时返回 User 对象（endpoint 可继续用 `request.state.user` 或在签名里
        显式接 `user: User = Depends(...)`）；缺 cap 时 raise HTTPException(403)。
    """
    def _dep(request: Request) -> User:
        user = getattr(request.state, "user", None)
        if user is None:
            # auth_middleware 应当已注入；走到这意味着 endpoint 配置错误
            raise HTTPException(status_code=500, detail="auth state missing")
        if not user.has_capability(cap):
            raise HTTPException(
                status_code=403,
                detail=f"missing capability: {cap}",
            )
        return user
    return _dep


def require_dev_role(request: Request) -> User:
    """硬编码守门；仅 `is_dev_role=True` 的 user 通过。

    这不是 capability —— 用于 token CRUD UI 等"权限管理本身"的入口。如果让
    capability 系统能 grant token 管理权，则一个 normal user 拿到该 cap 后能
    给自己授任何 cap → 绕过 dev 边界。所以 `is_dev_role` 的边界必须靠
    tokens.yaml 直接配置，不可由 UI 授权。

    Step 3 接 `/api/tokens` CRUD endpoints 时用此守门：
    `@router.post("/api/tokens", dependencies=[Depends(require_dev_role)])`
    """
    user = getattr(request.state, "user", None)
    if user is None:
        raise HTTPException(status_code=500, detail="auth state missing")
    if not user.is_dev_role:
        raise HTTPException(
            status_code=403,
            detail="dev role required (capability gate not applicable)",
        )
    return user
