# DFT-Agent Bench reference solver v1

The near-bare ReAct solver reads the task prompt, calls an LLM and uses four tools: `bash`, `write_file`, `read_file` and `submit`. It submits paths to its QE outputs. The benchmark launches the solver as a subprocess and keeps the reference answers and scoring rules on the evaluator side.

## Usage

Run this example from `agent/v1/` with Python 3.10 or later. For an end-to-end run that prepares the prompt and work directory for you, use the [harness commands](../README.md).

```bash
python -m pip install -r requirements.txt
python -m agent.solve \
  --prompt /path/to/task_prompt.txt \
  --workdir /path/to/workdir \
  --out /path/to/result.json \
  --model claude-sonnet-4-6
```

Use `--max-turns` and `--max-output-tokens` to set the run budget. By default, the solver selects a provider from the model ID using the patterns in [providers.yaml](agent/providers.yaml). Use `--provider` to select a provider explicitly.

## Provider configuration

The solver uses the Anthropic SDK for native Anthropic requests and the OpenAI SDK with a configurable `base_url` for OpenAI-compatible endpoints. PyYAML loads the routing configuration. The following model IDs illustrate the configured routes.

| Provider | API key environment variable | Example model IDs |
| --- | --- | --- |
| Anthropic | `ANTHROPIC_API_KEY` | `claude-sonnet-4-6`, `claude-haiku-4-5` |
| OpenAI | `OPENAI_API_KEY` | `gpt-4o`, `gpt-4o-mini`, `o1`, `o3-mini` |
| Google Gemini | `GOOGLE_API_KEY` | `gemini-2.5-pro`, `gemini-2.0-flash` |
| DeepSeek | `DEEPSEEK_API_KEY` | `deepseek-chat`, `deepseek-reasoner` |
| Moonshot Kimi | `MOONSHOT_API_KEY` | `kimi-latest`, `moonshot-v1-32k` |
| xAI | `XAI_API_KEY` | `grok-2-latest` |
| Mistral | `MISTRAL_API_KEY` | `mistral-large-latest`, `codestral-latest` |
| OpenRouter | `OPENROUTER_API_KEY` | `meta-llama/llama-3.3-70b-instruct` |
| Ollama (local) | — | `ollama/llama3.3:70b` |

Set the environment variable for your provider before starting a run. Local Ollama can run without a key. A literal `api_key` in a private configuration takes precedence over the provider's `key_env`; keep credentials out of version control. The native Anthropic client can also use its SDK credential lookup.

Pass `--config /path/to/my-providers.yaml` or set `DFT_SOLVER_CONFIG` to load another routing file. To add an OpenAI-compatible service, add an entry under its `providers` mapping:

```yaml
providers:
  my-provider:
    type: openai_compat
    key_env: MY_PROVIDER_API_KEY
    base_url: "https://api.example.com/v1"
    model_patterns:
      - "myprov-*"
```

## Code layout

| Path | Purpose |
| --- | --- |
| `agent/solve.py` | CLI entry point |
| `agent/loop.py` | Provider-independent four-tool loop |
| `agent/llm.py` | SDK dispatch, retries and normalized model responses |
| `agent/provider_config.py`, `agent/providers.yaml` | Provider registry, model patterns and endpoint settings |
| `agent/tools/` | Shell execution and command checks, file I/O and submission |
| `agent/prompts/system.txt` | Solver system prompt |
| `tests/` | Provider routing, SDK conversion, loop behavior and tool tests |
