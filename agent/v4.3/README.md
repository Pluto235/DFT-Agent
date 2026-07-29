# DFT Agent Solver v4.3

Planner-orchestrator solver for the benchmark in this repository. Compared
with v1, v4.3 separates planning, execution, verification, configuration
advice, and T2 pre-relaxation into distinct roles while keeping the same
external subprocess contract.

Supported task types:

- `vc_relax` (`M1_T1`)
- `bands_dos` (`M1_T2`)

## Architecture

| Component | Purpose |
|---|---|
| `solve.py` | CLI entry point and result validation |
| `agent/orchestrator.py` | Planner, executor, and verifier coordination |
| `agent/llm.py` | Anthropic and OpenAI-compatible provider client |
| `agent/providers.yaml` | Provider and per-role model routing |
| `agent/tools/` | Shell, QE parsing, and structured tool support |
| `agent/subagents/` | Configuration, convergence, and pre-relaxation helpers |
| `manifest.yaml` | Benchmark registration metadata |

The solver writes all calculation artifacts inside the supplied workdir and
returns relative paths in `result.json`. Before returning, it checks the same
path, size, and `JOB DONE.` conditions enforced by the benchmark harness.

## Setup

Python 3.10+ and Quantum ESPRESSO are required.

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Set the API key for a configured provider, for example:

```bash
export ANTHROPIC_API_KEY=<your-key>
```

Other supported providers and model patterns are defined in
`agent/providers.yaml`. A custom configuration can be supplied with
`--config` or `DFT_SOLVER_CONFIG`.

## CLI

```bash
python solve.py \
  --prompt  /absolute/path/task_prompt.txt \
  --workdir /absolute/path/workdir \
  --out     /absolute/path/result.json \
  --model   claude-sonnet-4-6
```

Role-specific model overrides are also available:

```text
--planner-model
--executor-model
--verifier-model
--config-advisor-model
--prerelax-model
```

The harness normally supplies these arguments from `manifest.yaml`.

## Tests

```bash
pytest -q
```

The unit tests mock provider calls and do not run full Quantum ESPRESSO
workflows. A full benchmark run additionally requires the GBRV
pseudopotentials prepared by `bench/setup.sh`.

## Example Harness Run

From `bench/`:

```bash
python harness/run.py \
  --task-id M1_T2_Si_bands_dos \
  --solver ../agent/v4.3/solve.py \
  --runs-dir runs
```
