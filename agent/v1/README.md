# DFT Agent Bench Solver v1

Reference solver agent for the benchmark included in this repository under
`bench/`.

## 设计

TL;DR：
- 独立 CLI agent：读题 prompt → 在 4 工具循环（`bash` / `write_file` / `read_file` / `submit`）里调用 LLM → 最后 `submit` 指向自己产出的 QE output 文件
- Bench harness 以 subprocess 形式拉起本 solver；solver **看不到** ground truth 或 evaluator 规则
- **Multi-provider**：通过 native `anthropic` + `openai`(配 `base_url`)双 SDK 调度,默认覆盖 Anthropic / OpenAI / Google Gemini / DeepSeek / Moonshot Kimi / xAI Grok / Mistral / OpenRouter / 本地 Ollama

## 用法

```bash
python -m agent.solve \
  --prompt   <path_to_task_prompt.txt> \
  --workdir  <path_to_workdir> \
  --out      <path_to_result.json> \
  --model    claude-sonnet-4-6 \
  [--provider auto] \
  [--config providers.yaml] \
  [--max-turns 100] [--max-output-tokens 8192]
```

`--model` 决定走哪家;provider 自动从 model id 前缀检(`agent/providers.yaml` 配置)。

## Multi-provider 配置

路由表在 `agent/providers.yaml`。**修改这个文件**就能加新 provider 或调 base_url。

| Provider | env var | model id 例 |
|---|---|---|
| Anthropic | `ANTHROPIC_API_KEY` | `claude-sonnet-4-6`, `claude-haiku-4-5` |
| OpenAI | `OPENAI_API_KEY` | `gpt-4o`, `gpt-4o-mini`, `o1`, `o3-mini` |
| Google Gemini | `GOOGLE_API_KEY` | `gemini-2.5-pro`, `gemini-2.0-flash` |
| DeepSeek | `DEEPSEEK_API_KEY` | `deepseek-chat`, `deepseek-reasoner` |
| Moonshot Kimi | `MOONSHOT_API_KEY` | `kimi-latest`, `moonshot-v1-32k` |
| xAI | `XAI_API_KEY` | `grok-2-latest` |
| Mistral | `MISTRAL_API_KEY` | `mistral-large-latest`, `codestral-latest` |
| OpenRouter | `OPENROUTER_API_KEY` | `meta-llama/llama-3.3-70b-instruct` |
| Ollama (local) | — | `ollama/llama3.3:70b` |

**API key 配置** 优先级：`providers.yaml` 的 `api_key` 字面值(本地测试,⚠️ 别 commit) > 该 provider 声明的 `key_env` 对应环境变量 > Anthropic 的 SDK 默认 fallback。

**自定义配置文件**：
```bash
# 临时:命令行 flag
python -m agent.solve --config /path/to/my-providers.yaml ...
# 或:env var
export DFT_SOLVER_CONFIG=/path/to/my-providers.yaml
```

**加新 provider**：编辑 `agent/providers.yaml`,加一条:
```yaml
my-provider:
  type: openai_compat
  key_env: MY_PROVIDER_API_KEY
  base_url: "https://api.my-provider.com/v1"
  model_patterns:
    - "myprov-*"
```

## 依赖

- Python 3.10+
- `anthropic` SDK + `openai` SDK + `pyyaml`（见 `requirements.txt`）
- 至少一家的 API key 设到对应 env var

## 仓库布局

```
agent/
├── solve.py             # CLI entry point
├── loop.py              # 4-tool agent loop (provider-agnostic)
├── llm.py               # LLMClient — 双路径 SDK dispatch + NormalizedResponse
├── provider_config.py   # providers.yaml loader + ProviderRegistry
├── providers.yaml       # 默认路由配置
├── tools/
│   ├── bash.py
│   ├── file_io.py       # write_file + read_file
│   └── submit.py
└── prompts/
    └── system.txt

tests/                   # 77 pytest pass
├── test_provider_config.py  # yaml 加载 / pattern 路由 / 自定义配置
├── test_llm.py              # SDK dispatch / 转换 / retry
├── test_loop.py             # agent 主循环 (NormalizedResponse-based mocks)
├── test_bash_tool.py
├── test_file_io_tool.py
└── test_submit_tool.py
```

## Provider 选择指南(security & 维护)

我们 **不用 `litellm`**:它半年内出 5 严重 proxy CVE + 1 PyPI 供应链事件(`1.82.7` / `1.82.8` 含恶意载荷)。改走 native SDK 双路径——anthropic SDK + openai SDK(以 `base_url` 重定向到 OpenAI-兼容 provider)。代价是多 ~80 行转换代码,收益是攻击面收窄到两家亲自维护的 SDK + pyyaml。
