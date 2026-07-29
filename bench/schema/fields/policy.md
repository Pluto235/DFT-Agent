# 字段规范：`task.policy`

> v1.3 引入。作为未来 runtime constraint 的容器，当前仅含 `network_allowed`；后续可平滑加入 `gpu_time_budget_s` / `memory_limit_mb` / `max_total_turns` 等字段，无需再次破坏性改 schema。

## 用途

把 agent 跑题时的 runtime 约束从 harness 硬编码提升为 bench JSON 契约——任何 agent 实现者都能从 JSON 明确读到该题的运行时限制（当前为是否允许联网），harness 据此调整 prompt 注入与 audit 检查。

## Agent 可见性

**部分给 agent**。`network_allowed=false` 时 harness 在 rendered prompt 尾部注入"Network Policy"段告知 agent；`true` 时不注入。policy 容器本身的字段不进 prompt。

## JSON 片段

```json
"task": {
  "type": "vc_relax",
  "prompt": null,
  "policy": {
    "network_allowed": false
  }
}
```

## 子字段规范

### 当前已启用

| 子字段 | 必填 | 类型 | 默认 | 说明 |
|---|---|---|---|---|
| network_allowed | 是 | bool | false | agent 是否允许访问 Claude API 之外的网络资源 |

### 暂未启用（占位，未来扩展）

| 子字段 | 类型 | 含义（草案） |
|---|---|---|
| gpu_time_budget_s | int | GPU 真实墙钟预算上限（秒）；超出则 audit 标记 |
| memory_limit_mb | int | solver subprocess 内存上限；harness 用 ulimit 或 cgroup 限制 |
| max_total_turns | int | agent 工具调用循环最大轮数（替换 solver --max-turns CLI 参数） |

未启用字段在 schema 里**不允许出现**（harness 不识别会忽略，bench 一致性 lint 会警告）。要启用先在本文档登记 + harness 实现 + 加测试。

## `network_allowed` 详细语义

### `network_allowed: false`（默认）

Agent 的 shell 工具不得访问 Claude API 以外的任何网络资源。**具体禁止**：

- 命令：`curl` / `wget` / `nc` / `ssh` / `scp` / `rsync -e ssh`
- 包管理：`pip install` / `conda install` / `apt install` / `brew install`
- 仓库：`git clone`（含 https / git/ssh URL）
- Python 脚本里 `requests` / `urllib` / `http` / `socket` 连外部主机
- 从 MP / arxiv / 任何 HTTP(S) 下载数据

**Harness 检查**：`audit_run(..., enforce_no_network=True)` 会扫 `bash.log` 里 `$ ` 开头的命令行，命中黑名单 → `audit_flags.no_network_access = false`（违规标记，不阻断 run）。

**Prompt 注入**：harness 在 rendered prompt 末尾追加 "=== Network Policy ===" 段告知 agent。

### `network_allowed: true`

agent 可自由使用网络。harness：
- prompt 不注入禁网段
- `audit_run(..., enforce_no_network=False)` → `no_network_access` flag 直接为 true（绕过检查）

**当前 bench 没有 `network_allowed=true` 的题目**——v1.3 起步 172 题全部 false。该字段为未来"显式开网的科研类题目"预留（例：让 agent 用 MP API 查询并选择稳定相）。

### v1.2 兼容性

老 schema JSON 里 `task` 不含 `policy` 字段。harness 通过 `task_loader.get_network_allowed(task)` helper 读，缺字段时兜底 `false`：

```python
def get_network_allowed(task: dict[str, Any]) -> bool:
    return bool(task.get("task", {}).get("policy", {}).get("network_allowed", False))
```

含义：任何老题目自动按"禁网"处理，与 MVP 阶段硬编码语义一致。

## 实现入口（harness 侧）

| 文件 | 角色 |
|---|---|
| `harness/lib/task_loader.py` | `get_network_allowed(task)` helper；`render_prompt` 注入禁网段（仅 false 时） |
| `harness/lib/audit.py` | `audit_run(..., enforce_no_network: bool = True)`；`_check_no_network_access` 扫 bash.log |
| `harness/run.py` | Phase 6 调用 `audit_run(..., enforce_no_network=not get_network_allowed(task))` |
| `harness/tests/test_task_loader.py` | get_network_allowed v1.3 / v1.2 / 显式 true 三路径 + render_prompt 含/不含禁网段 |
| `harness/tests/test_audit.py` | enforce_no_network=False 绕过检查 + True 默认检测 |

## 未来增强（不在 v1.3 范围）

- **硬隔离 sandbox**：当前是软约束 + audit。E.1 草案：MITM 代理只放 `api.anthropic.com` / 两进程架构（LLM client 外置，solver 在 `--network=none` 容器内）/ 继续靠软约束
- **bash 工具预检**：solver 端 bash 工具看到网络命令直接 refusal，比 audit 更早拦截
- **多字段 policy**：补 `gpu_time_budget_s` / `max_total_turns` 等需要新一轮 schema bump
