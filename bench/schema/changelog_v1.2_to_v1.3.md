# Bench Schema Changelog: v1.2 → v1.3

时间节点：2026-04-30（network policy schema 化）

## 一句话总览

v1.3 把"禁网"从 MVP harness 硬编码升级为 bench schema 字段——`task.policy.network_allowed`，默认 `false`；同时引入 `task.policy` 子字段作为未来 runtime constraint 的容器。向后兼容（v1.2 老 JSON 经 harness 兜底也读到 `false`）。

## 按主题详细清单

### 1. 新增 `task.policy` 子字段

| 改动 | 影响 |
|---|---|
| `task` 下新增 `policy` 子字段（v1.3 起 task 必填） | 当前唯一启用 key `network_allowed: bool` |
| `network_allowed: false`（默认） | agent 不得访问 Claude API 之外的网络资源；harness 在 rendered prompt 末尾注入 "Network Policy" 告知段；audit 扫 `bash.log` 命中黑名单则 flag 违规 |
| `network_allowed: true` | agent 可自由使用网络；harness 不注入禁网段；audit 跳过 no_network_access 检查（flag 直接 true）|
| 占位字段（暂未启用，登记在 `fields/policy.md`） | `gpu_time_budget_s` / `memory_limit_mb` / `max_total_turns` —— 启用前需先在文档登记 + harness 实现 + 加测试 |

### 2. 受影响的 question JSON

| 改动 | 影响 |
|---|---|
| 172 题全部加 `task.policy = {"network_allowed": false}` | 通过 `scripts/inject_policy_v1.3.py` 批量 patch（保守路径，不重跑 generate.py 避免回滚 PR #6 / #7 落地的 self-run GT 数据） |
| 172 题 `schema_version` 字段 `"1.2"` → `"1.3"` | 与字段升级同步 |
| `schema/examples/*.json` 4 份示例同步 | 同一脚本统一处理 |

### 3. Generator 变更（`questions/generate.py`）

| 改动 | 影响 |
|---|---|
| T1 / T2 build 路径里 `task` block 加 `"policy": {"network_allowed": False}` | 未来再跑 generate.py 重新生成时也会带上 v1.3 字段 |
| `schema_version` 常量字面量 `"1.2"` → `"1.3"` | 同上 |
| `questions/field_generation_checklist.md` §4 表新增 `task.policy.network_allowed` 行（来源：常量 false） | 文档同步 |

### 4. Harness 变更（`harness/`）

| 文件 | 改动 |
|---|---|
| `lib/task_loader.py` | 新增 `get_network_allowed(task) -> bool` helper（兜底 false，兼容 v1.2 老 JSON）；`render_prompt` 在 false 时追加 "Network Policy" 文本段；新增 `_render_network_policy_section()` 内置文本（不动 prompts/*.txt 模板，policy 与模板解耦） |
| `lib/audit.py` | `audit_run` 新增 kwarg `enforce_no_network: bool = True`；为 false 时 `no_network_access` flag 强制 true（绕过 bash.log 扫描） |
| `run.py` | Phase 6 调 `audit_run` 时传 `enforce_no_network=not get_network_allowed(task)` |
| `tests/test_task_loader.py` | 新增 5 个 case：v1.3 默认 false / 显式 true / v1.2 legacy 兜底 / render_prompt 含禁网段 / 显式 true 时不含 |
| `tests/test_audit.py` | 新增 2 个 case：enforce_no_network=False 时 flag bypass；enforce_no_network=True 默认行为不变 |

### 5. Schema 文档（`schema/`）

| 文件 | 改动 |
|---|---|
| `schema_overview.md` | 顶层字段表 task 一行加 policy 提示；schema_version 当前值 1.2 → 1.3；新增 v1.3 changelog 段 |
| `fields/task.md` | JSON 片段加 policy 子字段；子字段表加 policy 行（v1.3+ 必填）|
| `fields/policy.md` | **新建**——policy 容器规范、network_allowed 详细语义、v1.2 兼容性说明、harness 实现入口对照、未来增强讨论 |
| `changelog_v1.2_to_v1.3.md` | 本文件 |

## 关键量化

| 指标 | v1.2 → v1.3 |
|---|---|
| JSON schema_version | "1.2" → "1.3" |
| task 子字段数 | 2（type / prompt）→ 3（type / prompt / policy）|
| 受影响 question JSON | 172 题（M1+M2 × T1+T2）+ 4 份示例 |
| 受影响 harness 文件 | 4（task_loader.py / audit.py / run.py + 2 份 test）|
| 新增 schema 文档 | 1（fields/policy.md）|
| 单元测试新增 | 7 case（task_loader 5 + audit 2）|

## 向后兼容性

v1.3 是**向后兼容的 minor bump**：

- v1.2 JSON 经 harness `get_network_allowed()` 兜底也拿到 `false` —— 行为与 MVP 硬编码语义一致
- 旧测试 22 个 + 新测试 7 个全部通过；harness 全套 73 passed + 1 skipped
- generator / inject 脚本均 idempotent，重跑无副作用

## 决策依据

| 决策 | 选 | 理由 |
|---|---|---|
| 字段位置 | `task.policy.*` | 与 task 语义绑定强；policy 作为容器允许未来 platform-wide 字段（gpu_time_budget 等）平滑加入而无需破坏 task 子字段层级 |
| 默认值 | `network_allowed: false` | 与 MVP 硬编码语义一致；防止未来误把 default 当 true 让某些题悄悄"作弊"|
| 172 题 patch 路径 | 独立 inject 脚本（B.1 方案 2）| PR #6 / #7 已把 GT 替换为 self-run 数据；重跑 generate.py 会回滚到 MP 默认值，保守 patch 只改 policy + schema_version 两个字段，不破其他 |
| Audit bypass 实现 | `enforce_no_network` kwarg 默认 True | 默认不变，新增题目显式 true 时调用方主动传 false；与现有 audit_run kwonly 签名一致，破坏面小 |

## 未纳入本版（后续）

- **硬隔离 sandbox**（todo §改动 E）：当前是软约束 + audit。E.1 草案 MITM 代理 / 两进程架构 / 继续软约束 —— 评估在 MVP 跑过 N 题后看 audit 命中率
- **bash 工具预检**：solver 端 bash 工具看到 `curl`/`pip install` 直接 refusal，比 audit 更早拦截 —— 放到下一轮 agent solver 改进议题
- **`network_allowed: true` 的实际题目**：v1.3 起步 172 题全部 false；首道开网题等具体科研议题（"用 MP API 查询稳定相"等）成熟后再加
