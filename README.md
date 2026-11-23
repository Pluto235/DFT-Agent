# DFT-Agent Bench

DFT-Agent Bench evaluates how well an agent can carry out Quantum ESPRESSO workflows. You can bring your own solver and use the same tasks, scoring rules and execution checks as the reference agents. [Read the paper on arXiv](https://arxiv.org/abs/2611.10629).

| Module | Role |
| --- | --- |
| [`bench/`](bench/) | **The benchmark.** Supplies task prompts and reference data, runs your solver, scores the properties extracted from its QE outputs, and audits the execution logs. |
| [`agent/`](agent/) | Two optional reference solvers from the paper: near-bare ReAct v1 and the engineered v5.1 scaffold. They use the same solver interface as an external agent. |
| [`web/`](web/) | A local interface for browsing tasks, previewing prompts and inspecting run reports. [Preview the design](#local-web-preview). |

**Start with the 245 stable tasks** for a benchmark comparison. The release also includes 30 exploratory magnetic-material tasks and 41 held-out task definitions for diagnostics. [Choose a task and connect your solver](#start-with-the-benchmark).

## Benchmark design: four material tiers × four workflows

Each task pairs a material with a QE workflow. The **M axis** groups materials by
the physics and crystal structure a solver must handle. The **T axis** specifies
the calculation and the observable to recover. For example, moving from M1/T1
to M1/T2 keeps the material pool fixed and changes the target from a relaxed
structure to electronic properties. The two labels do not imply a single
numerical difficulty score.

![M×T task coverage. M1 and M2 show stable tasks out of all public tasks in each cell. M3 has 15 exploratory tasks each for T1 and T2. A dash marks a cell with no public tasks.](bench/task_coverage.png)

For M1×T1, **52 stable of 58** means that all 58 task files are public. Use the 52 stable tasks for benchmark comparisons. The other 6 are held out for diagnostics. M3 tasks are exploratory and are reported separately.

<details>
<summary>Browse task files by matrix cell (total public counts)</summary>

- **M1** · [T1 · 58](bench/questions/M1_T1/) · [T2 · 58](bench/questions/M1_T2/) · [T3 · 52](bench/questions/M1_T3/) · [T4 · 52](bench/questions/M1_T4/)
- **M2** · [T1 · 28](bench/questions/M2_T1/) · [T2 · 28](bench/questions/M2_T2/) · [T3 · 5](bench/questions/M2_T3/) · [T4 · 5](bench/questions/M2_T4/)
- **M3** · [T1 · 15](bench/questions/M3_T1/) · [T2 · 15](bench/questions/M3_T2/)

</details>

The [task manifest](bench/task_manifest.tsv) lists every task ID, its release set and its membership in the frozen 100-task slice.

T1 relaxes the cell and atomic positions. T2
computes the band structure and density of states. T3 fits an energy–volume
curve to obtain equilibrium volume and bulk modulus. T4 uses finite strains and
their stresses to recover the elastic stiffness tensor.

## Start with the benchmark

### 1. Choose a task

Run these commands in a POSIX shell. Browsing tasks only requires Python 3.10+.

```bash
git clone https://github.com/Pluto235/DFT-Agent.git
cd DFT-Agent
python3 bench/harness/run.py --list-tasks stable | grep '^M1_T1_'
```

Start with `M1_T1_Si_vc_relax` (silicon relaxation). Use `stable` for the 245-task benchmark, `exploratory` for the 30 magnetic tasks, or `frozen` for the paper's 100-task diagnostic comparison. The 41 `held_out` IDs are for diagnosis; the unfiltered list includes all 316. To compare agents, save the same task list for each and record the Git commit.

### 2. Connect your agent

From the repository root, prepare the harness environment. Install your own agent's Python package or adapter dependencies into the same environment.

```bash
python3 -m venv bench/.venv
. bench/.venv/bin/activate
python -m pip install -r bench/requirements.txt
```

To try the evaluator before installing QE, follow the [offline Si scoring example](bench/harness/README.md#score-a-saved-output) from `bench/`. For a live run, install Quantum ESPRESSO, put its binaries on `PATH` (or set `QE_GPU_WRAPPER_DIR`), and fetch the checksummed pseudopotentials:

```bash
bash bench/setup.sh
```

Wrap your agent in a Python script accepting `--prompt`, `--workdir` and `--out`. [Copy the adapter example](bench/harness/README.md#minimal-python-adapter) and connect its `run_agent` call to your implementation. Give the agent the rendered prompt and work directory; keep task JSONs with reference answers on the evaluator side. For T1 the adapter returns a path to the actual QE relaxation log. The [solver interface](bench/harness/README.md#solver-interface) lists the outputs for every workflow.

### 3. Run once and read the report

Replace the solver path below with your adapter. The fresh output directory keeps this invocation's report separate from previous attempts.

```bash
runs_dir="$(mktemp -d)"
python bench/harness/run.py --task-id M1_T1_Si_vc_relax \
  --solver /absolute/path/to/solve.py --runs-dir "$runs_dir"
python -m json.tool "$runs_dir"/*/report.json
```

Use **`total_score`** (0–1) for comparisons. If it is low and `audit_passed` is true, inspect `extracted_fields` and `per_field_scores` for disagreement with the reference. If `audit_passed` is false, the gate sets `total_score` to zero even when `total_score_raw` is high; the flags identify the missing execution evidence. A report with `failure_reason` can lack score fields. For the paper's failed-as-zero convention, keep that attempt in the denominator and count it as zero. See [report fields and troubleshooting](bench/harness/README.md#read-a-live-result).

## Public edition and paper scope

The original [116-task practice release](https://github.com/Pluto235/DFT-Agent/tree/5763997) covered M1/T1 and M1/T2. This release adds 200 task definitions from the [collaborators’ benchmark snapshot](https://github.com/wanweilin/dft-agent-bench/tree/821429331ddfbfbbc43c829797047d7b0c8965d2/questions). Of the 316 IDs, 245 form the paper's stable M1/M2 set, 30 are exploratory M3 tasks, and 41 have `metadata.is_stable=false`. Select a set explicitly when reporting aggregate scores. Include the repository commit, task IDs, solver, model, budget, repeats and treatment of failures.

## Reference agents and paper results

The two [reference solvers](agent/README.md) are the paper's **v1 near-bare ReAct** baseline and **v5.1 engineered scaffold**. Both come from the paper's [frozen review artifact](https://anonymous.4open.science/r/231e31re91287h/README.md).

| Fixed diagnostic slice | Paired tasks | v1 mean | v5.1 mean | Paired difference (95% CI) |
| --- | ---: | ---: | ---: | ---: |
| `deepseek-v4-pro`, retrieval disabled | 100 | 0.706 | 0.876 | +0.170 [0.108, 0.234] |

Scores are provenance-gated, and failed runs count as zero. The 100-task slice contains 58 stable, 30 exploratory and 12 held-out tasks. Use `--list-tasks frozen` to select these IDs.

### Results across workflows and material tiers

The figures show the paper's broader evaluation. Each panel compares v1 and v5.1 on tasks evaluated with the same model. [Read the paper on arXiv](https://arxiv.org/abs/2611.10629).

![Mean execution-gated scores for v1 and v5.1 across four workflows, with separate panels for DeepSeek and Opus](agent/fig_compare.png)

**Figure 1.** On common M1/M2 tasks, both models gain on bands + DOS. Elastic-tensor scores rise for DeepSeek and change little for Opus. Error bars show 95% task-clustered bootstrap intervals.

![Heatmaps of the change in execution-gated score from v1 to v5.1 by material tier and workflow for each model](agent/fig_tierheat.png)

**Figure 2.** Each cell shows the mean paired score difference (v5.1 minus v1). On M3 variable-cell relaxation, both models gain 0.17. Hatched cells contain fewer than ten paired tasks.

## Local web preview

![Local task catalogue with silicon relaxation selected, showing the agent prompt, crystal structure and input files](web/screenshots/catalogue.png)

The local catalogue shows the 245 stable tasks and 30 exploratory tasks. Here, selecting `M1_T1_Si_vc_relax` reveals the prompt and input files supplied to the solver. [Run the interface locally](web/README.md).

## Citation

If you use DFT-Agent Bench in your research, please cite the paper:

```bibtex
@misc{liu2026dftagentbenchscaffoldeffects,
  title={DFT-Agent Bench: Scaffold Effects and Execution Provenance in Autonomous DFT},
  author={Shijie Liu and Jin Yang and Weilin Wan and Jingtao Han and Jia LI and Tianqi Deng},
  year={2026},
  eprint={2611.10629},
  archivePrefix={arXiv},
  primaryClass={cs.AI},
  url={https://arxiv.org/abs/2611.10629}
}
```

## License

Project code is licensed under the [MIT License](LICENSE). Third-party datasets,
pseudopotentials and referenced materials retain their respective terms.
