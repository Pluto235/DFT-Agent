# Paper reference solvers

The harness can run either solver or a third-party adapter. Install the chosen solver's requirements into the benchmark environment. Both implementations are from the paper's [review artifact](https://anonymous.4open.science/r/231e31re91287h/README.md).

| Solver | Python entry point (from repository root) | Source revision in artifact |
| --- | --- | --- |
| Near-bare ReAct baseline | `agent/v1/agent/solve.py` | `82feeec` |
| Engineered QE scaffold | `agent/v5.1/solve.py` | `10f9283` |

For a live DeepSeek run, make the model available and set `DEEPSEEK_API_KEY`. From the repository root after [benchmark setup](../bench/README.md), choose one:

```bash
python -m pip install -r agent/v1/requirements.txt
python bench/harness/run.py --task-id M1_T1_Si_vc_relax --solver agent/v1/agent/solve.py --solver-args="--model deepseek-v4-pro"

python -m pip install -r agent/v5.1/requirements.txt
python bench/harness/run.py --task-id M1_T1_Si_vc_relax --solver agent/v5.1/solve.py --solver-args="--model deepseek-v4-pro --no-experience"
```

v5.1 bundles QE tools, input checks, domain guidance, convergence assistance and verification. The fixed diagnostic comparison disables experience retrieval. [Read the archived comparison](../results/README.md) without a model account or QE installation.
