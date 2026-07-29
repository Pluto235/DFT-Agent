# DFT-Agent

A compact public practice edition of DFT-Agent: LLM agents that run Quantum
ESPRESSO workflows, plus a structured benchmark and evaluation harness.

## Included

- `agent/v1/`: baseline multi-provider solver
- `agent/v4.3/`: planner-orchestrator solver with executor and verifier roles
- `bench/questions/M1_T1/`: 58 variable-cell relaxation tasks
- `bench/questions/M1_T2/`: 58 band-structure and DOS tasks
- `bench/harness/`: prompt rendering, solver execution, auditing, and scoring
- `bench/schema/`, `bench/materials/`, and `bench/pseudos/`: benchmark definitions
  and reproducibility data

The retained benchmark contains **58 M1 materials and 116 tasks**. M2 and
later material tiers, T3/T4 tasks, agent v5.1, and the web platform are not
part of this practice edition.

## Quick Start

Python 3.10 or newer is required. Running DFT tasks also requires a Quantum
ESPRESSO installation.

### Agents

```bash
cd agent/v1
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pytest -q
```

Use `agent/v4.3` instead to work with the planner-orchestrator version. See
each version's README for its CLI and provider configuration.

### Benchmark

```bash
cd bench
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
bash setup.sh
python harness/run.py --help
```

`bench/setup.sh` fetches and verifies the GBRV pseudopotentials. See
`bench/README.md` and `bench/harness/README.md` for the task format and solver
contract.

## Repository Scope

Runtime outputs, credentials, local environment files, databases, raw
pseudopotential files, and internal web deployment code are intentionally
excluded.

## License

Project code is licensed under the [MIT License](LICENSE). Third-party
datasets, pseudopotentials, and referenced materials remain subject to their
respective licenses and terms of use.
