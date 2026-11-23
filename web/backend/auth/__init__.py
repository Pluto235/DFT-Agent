"""Multi-tenant token auth：tokens.yaml 加载 + lookup + role-based 视图。

User 数据流：
  request → middleware → token_registry.lookup(bearer) → request.state.user
  → endpoint 读 request.state.user.owner / role 决定行为（filter / cancel 校验）

模型：阿里云集群——所有用户互相可见，但只能操作自己。dev role 是 superuser
（可看 + cancel 全部，访问 dev-only bench / agent）。

backwards 兼容：tokens.yaml 不存在时 fallback 到 .env 单 AUTH_TOKEN，等价于
[{token: AUTH_TOKEN, role: normal, owner: default, display_name: default}]。
"""
from backend.auth.tokens import TokenRegistry, User

__all__ = ["TokenRegistry", "User"]
